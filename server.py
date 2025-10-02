"""
Flask dashboard + SSE log stream that controls the trading bot (bot.py).
Usage:
  python server.py
Then open http://127.0.0.1:5000 in your browser.
"""

import os
import json
import queue
import threading
import time
from flask import Flask, Response, request, jsonify, render_template, redirect, url_for
from jinja2 import TemplateNotFound

# Import your bot module and controller
import bot as bot_module
from bot import BotController

# ----------------- Setup -----------------
app = Flask(__name__)
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
    # Try to show the template if it exists, otherwise fall back to a minimal page
    accounts = bot.load_accounts()
    try:
        return render_template("index.html", running=bot.is_running(), accounts=accounts)
    except TemplateNotFound:
        # Minimal fallback UI (so the app doesn't 500 when index.html missing)
        rows = "".join(
            f"<tr><td>{i}</td><td>{a.get('name')}</td><td>{(a.get('key') or '')[:6]+'****'}</td>"
            f"<td>{a.get('last_balance',0)}</td><td>{a.get('position','closed')}</td></tr>"
            for i, a in enumerate(accounts)
        )
        html = f"""
        <html>
          <head>
            <title>Trading Bot</title>
            <style>body{{font-family:Arial}}table{{border-collapse:collapse}}td,th{{border:1px solid #ddd;padding:6px}}</style>
          </head>
          <body>
            <h2>Trading Bot — Minimal UI</h2>
            <form method="post" action="/start"><button type="submit">Start Bot (all accounts)</button></form>
            <form method="post" action="/stop"><button type="submit">Stop Bot</button></form>
            <h3>Add Account</h3>
            <form method="post" action="/add_account">
              <input name="name" placeholder="Name" />
              <input name="key" placeholder="API Key" />
              <input name="secret" placeholder="API Secret" />
              <button type="submit">Add</button>
            </form>
            <h3>Accounts</h3>
            <table><thead><tr><th>#</th><th>Name</th><th>Key</th><th>Balance</th><th>Position</th></tr></thead><tbody>{rows}</tbody></table>
            <h3>Logs</h3>
            <pre id="log" style="height:300px;overflow:auto;border:1px solid #ccc;padding:8px"></pre>
            <script>
              var es = new EventSource('/stream');
              es.onmessage = function(e) {{
                var pre = document.getElementById('log');
                pre.textContent += e.data + "\\n";
                pre.scrollTop = pre.scrollHeight;
              }};
            </script>
          </body>
        </html>
        """
        return html


# ---- Logs stream ----
@app.route("/stream")
def stream():
    def event_stream():
        # Send server-sent events with a keepalive
        while True:
            try:
                line = log_queue.get(timeout=1)
                # make sure newlines are safe in SSE payload
                safe_line = line.replace("\n", " ")
                yield f"data: {safe_line}\n\n"
            except queue.Empty:
                # keep connection alive
                yield ": keep-alive\n\n"
    return Response(event_stream(), mimetype="text/event-stream")


# ---- Bot control ----
@app.route("/start", methods=["POST"])
def start():
    if bot.is_running():
        return jsonify({"status": "already_running"}), 200

    accounts = bot.load_accounts()
    if not accounts:
        return jsonify({"error": "No accounts configured. Add one via /add_account."}), 400

    # BotController.start() (in your long bot.py) will spawn one thread per account and trade them concurrently
    bot.start()
    return jsonify({"status": "started", "accounts": len(accounts)}), 200


@app.route("/stop", methods=["POST"])
def stop():
    if not bot.is_running():
        return jsonify({"status": "not_running"}), 200
    bot.stop()
    return jsonify({"status": "stopped"}), 200


# ---- Accounts ----
@app.route("/add_account", methods=["POST"])
def add_account():
    # Accept form-encoded or JSON
    data = request.form if request.form else (request.get_json(silent=True) or {})
    key = data.get("key")
    secret = data.get("secret")
    name = data.get("name") or f"Account"

    if not key or not secret:
        return jsonify({"error": "Missing key or secret"}), 400

    accounts = bot.load_accounts()
    acc = {
        "id": str(len(accounts) + 1),
        "name": name,
        "key": key,
        "secret": secret,
        "last_balance": 0.0,
        "position": "closed",
        "monitoring": False,
        "last_trade_start": None,
        "current_symbol": None,
        "buy_price": None,
    }

    # quick validation: try creating a session and pulling wallet balance (non-fatal)
    try:
        session = bot._make_session(acc)
        resp = session.get_wallet_balance(accountType="UNIFIED")
        balance_list = resp.get("result", {}).get("list", [])
        if balance_list:
            first = balance_list[0]
            acc["last_balance"] = float(first.get("totalEquity", 0) or 0.0)
    except Exception as e:
        # validation failed; still save but log a warning
        bot.log(f"⚠️ Account validation failed for {name}: {e}")
        acc["last_balance"] = 0.0

    accounts.append(acc)
    # use bot.save_accounts to maintain same locking logic as bot.py
    bot.save_accounts(accounts)
    return redirect(url_for("index"))


@app.route("/remove_account", methods=["POST"])
def remove_account():
    data = request.form if request.form else (request.get_json(silent=True) or {})
    try:
        idx = int(data.get("index"))
    except Exception:
        return jsonify({"error": "invalid index"}), 400

    accounts = bot.load_accounts()
    if 0 <= idx < len(accounts):
        removed = accounts.pop(idx)
        bot.save_accounts(accounts)
        bot.log(f"Removed account {removed.get('name')}")
    return redirect(url_for("index"))


@app.route("/accounts_data")
def accounts_data():
    accounts = bot.load_accounts()
    data = []
    for i, acc in enumerate(accounts):
        trade_info = None
        if acc.get("position") == "open":
            trade_info = {
                "symbol": acc.get("current_symbol"),
                "buy_price": acc.get("buy_price"),
                "entry_time": acc.get("last_trade_start"),
                "current_price": acc.get("current_price", "N/A"),
                "pnl": acc.get("current_pnl", 0.0),
            }
        data.append(
            {
                "index": i,
                "key": (acc.get("key", "")[:6] + "****") if acc.get("key") else "",
                "balance": acc.get("last_balance", 0.0),
                "position": acc.get("position", "closed"),
                "monitoring": acc.get("monitoring", False),
                "trade": trade_info,
            }
        )
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
    # trades from bot.py use "profit_pct" for exits; some older entries might use "pnl_pct"
    def get_pnl(t):
        return float(t.get("profit_pct", t.get("pnl_pct", 0) or 0))

    total_trades = len(trades)
    wins = sum(1 for t in trades if get_pnl(t) > 0)
    losses = sum(1 for t in trades if get_pnl(t) < 0)
    total_pnl = sum(get_pnl(t) for t in trades)

    accounts_summary = {}
    for t in trades:
        acc = t.get("account", "Unknown")
        if acc not in accounts_summary:
            accounts_summary[acc] = {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
        accounts_summary[acc]["trades"] += 1
        pnl = get_pnl(t)
        if pnl > 0:
            accounts_summary[acc]["wins"] += 1
        elif pnl < 0:
            accounts_summary[acc]["losses"] += 1
        accounts_summary[acc]["total_pnl"] += pnl

    return jsonify(
        {
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "total_pnl": total_pnl,
            "accounts": accounts_summary,
        }
    )


# ---- Health/status ----
@app.route("/status")
def status():
    return jsonify({"running": bot.is_running(), "threads": len(getattr(bot, "_threads", []))})


# ----------------- Run -----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True)
