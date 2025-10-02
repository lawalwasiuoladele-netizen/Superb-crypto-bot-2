# server.py
"""
Flask dashboard + SSE log stream that controls the trading bot (bot.py).
Usage:
  python server.py
Then open http://127.0.0.1:5000 in your browser.
"""

import json
import queue
import threading
import time
from flask import Flask, Response, request, jsonify, render_template, redirect, url_for
from bot import BotController

# ----------------- Setup -----------------
app = Flask(__name__)
log_queue = queue.Queue()
bot = BotController(log_queue=log_queue)

ACCOUNTS_FILE = "accounts.json"
TRADES_FILE = "trades.json"

# ----------------- Helpers -----------------
def load_json_file(path, default=[]):
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

# ---- Logs stream ----
@app.route("/stream")
def stream():
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
    if bot.is_running():
        return redirect(url_for("index"))
    bot.start()
    return redirect(url_for("index"))

@app.route("/stop", methods=["POST"])
def stop():
    if bot.is_running():
        bot.stop()
    return redirect(url_for("index"))

# ---- Accounts ----
@app.route("/add_account", methods=["POST"])
def add_account():
    key = request.form.get("key")
    secret = request.form.get("secret")
    accounts = load_json_file(ACCOUNTS_FILE, [])
    accounts.append({
        "id": str(len(accounts) + 1),
        "name": f"Account {len(accounts)+1}",
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
    return redirect(url_for("index"))

@app.route("/remove_account", methods=["POST"])
def remove_account():
    idx = int(request.form.get("index"))
    accounts = load_json_file(ACCOUNTS_FILE, [])
    if 0 <= idx < len(accounts):
        accounts.pop(idx)
        save_json_file(ACCOUNTS_FILE, accounts)
    return redirect(url_for("index"))

@app.route("/accounts_data")
def accounts_data():
    accounts = load_json_file(ACCOUNTS_FILE, [])
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
            "key": acc.get("key", "")[:6] + "****",
            "balance": acc.get("last_balance", 0.0),
            "trade": trade_info
        })
    return jsonify(data)

# ---- Trades ----
@app.route("/trades_data")
def trades_data():
    trades = load_json_file(TRADES_FILE, [])
    return jsonify(trades)

# ---- Summary ----
@app.route("/summary_data")
def summary_data():
    trades = load_json_file(TRADES_FILE, [])
    total_trades = len(trades)
    wins = sum(1 for t in trades if t.get("pnl_pct", 0) > 0)
    losses = sum(1 for t in trades if t.get("pnl_pct", 0) < 0)
    total_pnl = sum(t.get("pnl_pct", 0) for t in trades)

    accounts_summary = {}
    for t in trades:
        acc = t.get("account", "Unknown")
        if acc not in accounts_summary:
            accounts_summary[acc] = {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
        accounts_summary[acc]["trades"] += 1
        if t.get("pnl_pct", 0) > 0:
            accounts_summary[acc]["wins"] += 1
        elif t.get("pnl_pct", 0) < 0:
            accounts_summary[acc]["losses"] += 1
        accounts_summary[acc]["total_pnl"] += t.get("pnl_pct", 0)

    return jsonify({
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "total_pnl": total_pnl,
        "accounts": accounts_summary
    })

# ----------------- Run -----------------
if __name__ == "__main__":
    app.run(debug=True, threaded=True)
