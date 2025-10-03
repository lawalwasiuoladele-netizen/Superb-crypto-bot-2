# server.py
"""
Flask dashboard + SSE log stream that controls the trading bot (bot.py).

Usage (dev):
  python server.py

Usage (production):
  gunicorn -w 1 server:app

Notes:
 - SSE streams can be sensitive to your hosting environment. The server here sends heartbeats
   (every ~10s) to keep browser connections alive.
 - If you deploy to Render or Heroku, they may drop idle connections — consider adding a small
   proxy/keepalive or use WebSocket in the future.
"""

import json
import os
import queue
import threading
import time
from flask import Flask, Response, request, jsonify, render_template
from bot import BotController

# ----------------- Setup -----------------
app = Flask(__name__, template_folder="templates", static_folder="static")
log_queue = queue.Queue()
bot = BotController(log_queue=log_queue)

ACCOUNTS_FILE = "accounts.json"
TRADES_FILE = "trades.json"

# ----------------- Helpers -----------------
def load_json_file(path, default=None):
    if default is None:
        default = []
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default

def save_json_file(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

# ----------------- Routes -----------------
@app.route("/")
def index():
    return render_template("index.html", running=bot.is_running())

# ---- Logs stream (SSE) ----
@app.route("/logs")
def logs():
    def event_stream():
        # We'll try to send real log messages as they arrive. If no logs for 10s, emit heartbeat.
        while True:
            try:
                line = log_queue.get(timeout=10)  # 10s heartbeat
                yield f"data: {line}\n\n"
            except queue.Empty:
                # heartbeat to keep SSE connection alive
                yield f"data: [heartbeat]\n\n"
    return Response(event_stream(), mimetype="text/event-stream")

# ---- Bot control ----
@app.route("/start", methods=["POST"])
def start():
    data = request.get_json(silent=True) or {}
    new_accounts = data.get("accounts")
    if new_accounts:
        accounts = []
        for i, a in enumerate(new_accounts):
            accounts.append({
                "id": str(i + 1),
                "name": a.get("name") or f"Account {i+1}",
                "key": a.get("key"),
                "secret": a.get("secret"),
                "last_balance": 0.0,
                "position": "closed",
                "monitoring": False,
                "last_trade_start": None,
                "current_symbol": None,
                "buy_price": None
            })
        save_json_file(ACCOUNTS_FILE, accounts)

    # start the bot (idempotent)
    bot.start()
    accounts = load_json_file(ACCOUNTS_FILE, [])
    return jsonify({"status": "started", "accounts": [a.get("name") for a in accounts]})

@app.route("/stop", methods=["POST"])
def stop():
    bot.stop()
    return jsonify({"status": "stopping"})

# ---- Accounts management ----
@app.route("/add_account", methods=["POST"])
def add_account():
    data = request.get_json(silent=True) or request.form
    name = data.get("name")
    key = data.get("key")
    secret = data.get("secret")
    if not key or not secret:
        return jsonify({"error": "missing key or secret"}), 400
    accounts = load_json_file(ACCOUNTS_FILE, [])
    accounts.append({
        "id": str(len(accounts) + 1),
        "name": name or f"Account {len(accounts)+1}",
        "key": key,
        "secret": secret,
        "last_balance": 0.0,
        "position": "closed",
        "monitoring": False,
        "last_trade_start": None,
        "current_symbol": None,
        "buy_price": None
    })
    save_json_file(ACCOUNTS_FILE, accounts)
    return jsonify({"status": "ok", "accounts": [a.get("name") for a in accounts]})

@app.route("/remove_account", methods=["POST"])
def remove_account():
    data = request.get_json(silent=True) or request.form
    idx = data.get("index")
    try:
        idx = int(idx)
    except Exception:
        return jsonify({"error": "invalid index"}), 400
    accounts = load_json_file(ACCOUNTS_FILE, [])
    if 0 <= idx < len(accounts):
        removed = accounts.pop(idx)
        save_json_file(ACCOUNTS_FILE, accounts)
        return jsonify({"status": "removed", "removed": removed.get("name")})
    return jsonify({"error": "index out of range"}), 400

@app.route("/accounts_data")
def accounts_data():
    """
    Returns the accounts list for the frontend. If last_balance is missing or zero,
    attempt a one-shot balance refresh (using bot.validate_account) to show a live value.
    """
    accounts = load_json_file(ACCOUNTS_FILE, [])
    # Try refreshing balances for accounts that have no stored balance yet (best-effort)
    for acc in accounts:
        if acc.get("last_balance") in (None, 0.0):
            try:
                ok, bal, err = bot.validate_account(acc, retries=1)
                if ok:
                    acc["last_balance"] = bal
                else:
                    bot.log(f"Balance refresh failed for ****{acc.get('key','')[-4:]}: {err}")
            except Exception as e:
                bot.log(f"Balance refresh exception for ****{acc.get('key','')[-4:]}: {e}")
    # persist any updates
    save_json_file(ACCOUNTS_FILE, accounts)

    data = []
    for i, acc in enumerate(accounts):
        trade_info = None
        if acc.get("position") == "open":
            trade_info = {
                "symbol": acc.get("current_symbol"),
                "buy_price": acc.get("buy_price"),
                "entry_time": acc.get("last_trade_start"),
                "current_price": acc.get("current_price", "N/A"),
                "pnl": acc.get("current_pnl", 0.0)
            }
        data.append({
            "index": i,
            "name": acc.get("name"),
            "key_masked": (acc.get("key", "")[:6] + "****") if acc.get("key") else "",
            "balance": acc.get("last_balance", None),
            "trade": trade_info
        })
    return jsonify(data)

# ---- Trades & summary ----
@app.route("/trades_data")
def trades_data():
    trades = load_json_file(TRADES_FILE, [])
    return jsonify(trades)

@app.route("/summary_data")
def summary_data():
    trades = load_json_file(TRADES_FILE, [])
    total_trades = len(trades)
    wins = sum(1 for t in trades if t.get("profit_pct", 0) > 0)
    losses = sum(1 for t in trades if t.get("profit_pct", 0) < 0)
    total_pnl = sum(t.get("profit_pct", 0) for t in trades)

    accounts_summary = {}
    for t in trades:
        acc = t.get("account", "Unknown")
        if acc not in accounts_summary:
            accounts_summary[acc] = {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
        accounts_summary[acc]["trades"] += 1
        if t.get("profit_pct", 0) > 0:
            accounts_summary[acc]["wins"] += 1
        elif t.get("profit_pct", 0) < 0:
            accounts_summary[acc]["losses"] += 1
        accounts_summary[acc]["total_pnl"] += t.get("profit_pct", 0)

    return jsonify({
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "total_pnl": total_pnl,
        "accounts": accounts_summary
    })

@app.route("/status")
def status():
    return jsonify({"running": bot.is_running()})

# ----------------- Run -----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # debug True is convenient for dev. For production use gunicorn.
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True)
