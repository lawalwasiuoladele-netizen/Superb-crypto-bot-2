"""
Flask server controlling BotController, SSE logs, account management endpoints.
"""

import os
import json
import queue
from flask import Flask, Response, request, jsonify, render_template, redirect, url_for
from bot import BotController

app = Flask(__name__, template_folder="templates")
log_queue = queue.Queue()
bot = BotController(log_queue=log_queue)

ACCOUNTS_FILE = "accounts.json"
TRADES_FILE = "trades.json"

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

@app.route("/")
def index():
    try:
        accounts = bot.load_accounts()
        return render_template("index.html", running=bot.is_running(), accounts=accounts)
    except Exception:
        # fallback minimal
        accounts = bot.load_accounts()
        rows = "".join(f"<tr><td>{i+1}</td><td>{a.get('name')}</td><td>{(a.get('key') or '')[:6]}****</td><td>{a.get('last_balance',0)}</td></tr>" for i,a in enumerate(accounts))
        return f"<html><body><h2>Bot</h2><table>{rows}</table></body></html>"

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

@app.route("/start", methods=["POST"])
def start():
    if bot.is_running():
        return jsonify({"status":"already_running"}), 200
    bot.start()
    return jsonify({"status":"started"}), 200

@app.route("/stop", methods=["POST"])
def stop():
    if bot.is_running():
        bot.stop()
    return jsonify({"status":"stopped"}), 200

@app.route("/add_account", methods=["POST"])
def add_account():
    # accept JSON or form
    data = request.get_json(silent=True) or request.form.to_dict()
    name = data.get("name") or f"Account {int(time.time())}"
    key = data.get("key")
    secret = data.get("secret")
    if not key or not secret:
        return jsonify({"error":"missing key/secret"}), 400
    accounts = bot.load_accounts()
    accounts.append({
        "id": str(len(accounts)+1),
        "name": name,
        "key": key,
        "secret": secret,
        "last_balance": 0.0,
        "position": "closed",
        "monitoring": False,
        "last_trade_start": None,
        "current_symbol": None,
        "buy_price": None
    })
    bot.save_accounts(accounts)
    return jsonify({"status":"added","name":name}), 200

@app.route("/remove_account", methods=["POST"])
def remove_account():
    data = request.get_json(silent=True) or request.form.to_dict()
    try:
        idx = int(data.get("index"))
    except Exception:
        return jsonify({"error":"invalid index"}), 400
    accounts = bot.load_accounts()
    if 0 <= idx < len(accounts):
        removed = accounts.pop(idx)
        bot.save_accounts(accounts)
        bot.log(f"Removed account {removed.get('name')}")
    return jsonify({"status":"ok"}), 200

@app.route("/accounts_data")
def accounts_data():
    accounts = bot.load_accounts()
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
            "key": (acc.get("key","")[:6] + "****") if acc.get("key") else "",
            "balance": acc.get("last_balance", 0.0),
            "position": acc.get("position", "closed"),
            "monitoring": acc.get("monitoring", False),
            "trade": trade_info
        })
    return jsonify(out)

@app.route("/trades_data")
def trades_data():
    trades = load_json_file(TRADES_FILE, [])
    return jsonify(trades)

@app.route("/summary_data")
def summary_data():
    trades = load_json_file(TRADES_FILE, [])
    def get_pnl(t):
        return float(t.get("profit_pct", t.get("pnl_pct", 0) or 0))
    total_trades = len(trades)
    wins = sum(1 for t in trades if get_pnl(t) > 0)
    losses = sum(1 for t in trades if get_pnl(t) < 0)
    total_pnl = sum(get_pnl(t) for t in trades)
    accounts_summary = {}
    for t in trades:
        acc = t.get("account","Unknown")
        if acc not in accounts_summary:
            accounts_summary[acc] = {"trades":0,"wins":0,"losses":0,"total_pnl":0.0}
        accounts_summary[acc]["trades"] += 1
        pnl = get_pnl(t)
        if pnl > 0:
            accounts_summary[acc]["wins"] += 1
        elif pnl < 0:
            accounts_summary[acc]["losses"] += 1
        accounts_summary[acc]["total_pnl"] += pnl
    return jsonify({
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "total_pnl": total_pnl,
        "accounts": accounts_summary
    })

@app.route("/validate_account", methods=["POST"])
def validate_account():
    data = request.get_json(silent=True) or request.form.to_dict()
    key = data.get("key")
    secret = data.get("secret")
    name = data.get("name") or "tmp"
    if not key or not secret:
        return jsonify({"ok": False, "error": "missing key/secret"}), 400
    acc = {"name": name, "key": key, "secret": secret}
    ok, bal, err = bot.validate_account(acc)
    return jsonify({"ok": ok, "balance": bal, "error": err}), 200

@app.route("/status")
def status():
    return jsonify({"running": bot.is_running(), "threads": len(getattr(bot, "_threads", []))})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # recommended: use gunicorn with -k gthread or default sync workers (avoid eventlet)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
