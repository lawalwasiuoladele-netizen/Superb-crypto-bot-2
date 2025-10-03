"""
server.py — Flask dashboard for Superb Crypto Bot (matches the lean bot.py)

Place the HTML below at `templates/index.html` (create a `templates/` folder).
Run with `python server.py` (dev) or `gunicorn -w 1 server:app` for production.
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
        while True:
            try:
                line = log_queue.get(timeout=1)
                yield f"data: {line}\n\n"
            except queue.Empty:
                yield ": keep-alive\n\n"
    return Response(event_stream(), mimetype="text/event-stream")

# ---- Bot control ----
@app.route("/start", methods=["POST"])
def start():
    # Optionally accept JSON {"accounts": [{"name":"...","key":"...","secret":"..."}, ...]}
    data = request.get_json(silent=True) or {}
    new_accounts = data.get("accounts")
    if new_accounts:
        # Replace accounts.json with provided list (safer to be explicit)
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
    # start the bot
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
    accounts = load_json_file(ACCOUNTS_FILE, [])
    out = []
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
        out.append({
            "index": i,
            "name": acc.get("name"),
            "key_masked": (acc.get("key", "")[:6] + "****") if acc.get("key") else "",
            "balance": acc.get("last_balance", 0.0),
            "trade": trade_info
        })
    return jsonify(out)

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
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True)
