"""
Flask dashboard + SSE log stream that controls the trading bot (bot.py).
Usage:
  python server.py
Then open http://127.0.0.1:5000
"""

# ---- eventlet must be patched first ----
import eventlet
eventlet.monkey_patch()

# ---- standard imports ----
import time
import os
import queue
import uuid
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, Response, jsonify

# ---- local imports ----
from bot import BotController, ALLOWED_COINS  # ✅ import allowed coins

# ---- app setup ----
APP_PORT = int(os.environ.get("APP_PORT", "5000"))

app = Flask(__name__, template_folder="templates")
log_queue = queue.Queue(maxsize=1000)

bot_ctrl = BotController(log_queue=log_queue)

# ---------- ROUTES ----------
@app.route("/")
def index():
    running = bot_ctrl.is_running()
    return render_template("index.html", running=running)

@app.route("/accounts_data")
def accounts_data():
    """AJAX endpoint to fetch live account + trade status"""
    accounts = bot_ctrl.load_accounts()
    account_status = []

    for i, acc in enumerate(accounts):
        acct_info = {
            "index": i,
            # ✅ safer key display
            "key": "****" + acc.get("key", "")[-4:],
            "balance": acc.get("last_balance", "N/A"),
            "position": acc.get("position", "idle"),
            "monitoring": acc.get("monitoring", False),
            "trade": None
        }

        # ---- check open trade ----
        if acc.get("position") == "open":
            symbol = acc.get("current_symbol", "")
            if symbol in ALLOWED_COINS:
                buy_price = float(acc.get("buy_price", 0))
                entry_time = float(acc.get("entry_time", 0))
                entry_dt = datetime.utcfromtimestamp(entry_time).strftime("%Y-%m-%d %H:%M:%S")

                try:
                    session = bot_ctrl._make_session(acc)
                    current_price = bot_ctrl.get_ticker_last(session, symbol)
                except Exception:
                    current_price = None

                profit_pct = 0
                if current_price and buy_price > 0:
                    profit_pct = (current_price - buy_price) / buy_price * 100

                acct_info["trade"] = {
                    "symbol": symbol,
                    "buy_price": buy_price,
                    "entry_time": entry_dt,
                    "current_price": current_price if current_price else "N/A",
                    "pnl": round(profit_pct, 2)
                }

        account_status.append(acct_info)

    return jsonify(account_status)

@app.route("/trades_data")
def trades_data():
    """AJAX endpoint to return trade history (with unique IDs)"""
    trades = bot_ctrl.load_trades()
    # ✅ Ensure each trade has an "id" for frontend use
    for t in trades:
        if "id" not in t:
            t["id"] = f"{t.get('account','')}-{t.get('symbol','')}-{t.get('entry_time','')}"
    return jsonify(trades)

@app.route("/summary_data")
def summary_data():
    """Return profit summary across all trades + per-account breakdown"""
    trades = bot_ctrl.load_trades()

    if not trades:
        return jsonify({"total_trades": 0, "total_pnl": 0, "wins": 0, "losses": 0, "accounts": {}})

    total_pnl = sum(t.get("pnl_pct", 0) for t in trades)
    wins = len([t for t in trades if t.get("pnl_pct", 0) > 0])
    losses = len([t for t in trades if t.get("pnl_pct", 0) < 0])

    # --- Per account breakdown ---
    accounts_summary = {}
    for t in trades:
        acc = t.get("account", "unknown")
        if acc not in accounts_summary:
            accounts_summary[acc] = {"total_pnl": 0, "wins": 0, "losses": 0, "trades": 0}
        accounts_summary[acc]["total_pnl"] += t.get("pnl_pct", 0)
        accounts_summary[acc]["trades"] += 1
        if t.get("pnl_pct", 0) > 0:
            accounts_summary[acc]["wins"] += 1
        elif t.get("pnl_pct", 0) < 0:
            accounts_summary[acc]["losses"] += 1

    summary = {
        "total_trades": len(trades),
        "total_pnl": round(total_pnl, 2),
        "wins": wins,
        "losses": losses,
        "accounts": {
            acc: {
                "trades": data["trades"],
                "wins": data["wins"],
                "losses": data["losses"],
                "total_pnl": round(data["total_pnl"], 2),
            }
            for acc, data in accounts_summary.items()
        },
    }
    return jsonify(summary)

@app.route("/add_account", methods=["POST"])
def add_account():
    """Add new trading account (form or JSON supported)"""
    try:
        if request.is_json:
            data = request.get_json()
            key = data.get("key", "").strip()
            secret = data.get("secret", "").strip()
        else:
            key = request.form.get("key", "").strip()
            secret = request.form.get("secret", "").strip()

        if not key or not secret:
            return jsonify({"error": "Missing API key or secret"}), 400

        new_acc = {
            "id": str(uuid.uuid4()),
            "key": key,
            "secret": secret,
            "position": "closed",
            "monitoring": False,
            "last_balance": 0.0
        }

        accounts = bot_ctrl.load_accounts()
        accounts.append(new_acc)
        bot_ctrl.save_accounts(accounts)

        bot_ctrl.log(f"Added new account ending with ****{key[-4:]}")

        # If JSON request → return JSON
        if request.is_json:
            return jsonify({"status": "ok", "account": new_acc}), 200

        # If HTML form → redirect to index
        return redirect(url_for("index"))

    except Exception as e:
        bot_ctrl.log(f"Error adding account: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/remove_account", methods=["POST"])
def remove_account():
    try:
        idx = int(request.form.get("index", "-1"))
    except Exception:
        idx = -1
    if idx >= 0:
        bot_ctrl.remove_account_index(idx)
    return redirect(url_for("index"))

@app.route("/start", methods=["POST"])
def start():
    bot_ctrl.start()
    time.sleep(0.1)
    return redirect(url_for("index"))

@app.route("/stop", methods=["POST"])
def stop():
    bot_ctrl.stop()
    time.sleep(0.1)
    return redirect(url_for("index"))

@app.route("/stream")
def stream():
    def event_stream():
        while True:
            try:
                # ✅ add heartbeat every 15s
                line = log_queue.get(timeout=15)
                yield f"data: {line}\n\n"
            except queue.Empty:
                yield ": keep-alive\n\n"
    return Response(event_stream(), mimetype="text/event-stream")

# ---- run server locally ----
if __name__ == "__main__":
    bot_ctrl.ensure_accounts_file()
    print(f"Starting Flask on 0.0.0.0:{APP_PORT}")
    app.run(host="0.0.0.0", port=APP_PORT, threaded=True)
