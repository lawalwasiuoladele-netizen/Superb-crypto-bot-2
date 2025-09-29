# server.py
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
from datetime import timedelta
from flask import Flask, render_template, request, redirect, url_for, Response

# ---- local imports ----
from bot import BotController  # expects a log_queue and uses accounts.json

# ---- app setup ----
APP_PORT = int(os.environ.get("APP_PORT", "5000"))

app = Flask(__name__, template_folder="templates")
log_queue = queue.Queue(maxsize=1000)

bot_ctrl = BotController(log_queue=log_queue)

# ---------- ROUTES ----------
@app.route("/")
def index():
    accounts = bot_ctrl.load_accounts()
    acct_view = []

    for i, a in enumerate(accounts):
        session = bot_ctrl._make_session(a)
        balance = 0.0
        if session:
            balance = bot_ctrl._get_balance_usdt(session)

        current_symbol = a.get("current_symbol", None)
        if a.get("position") == "open" and current_symbol:
            trade_info = f"Open: {current_symbol}"
        else:
            trade_info = "No open trade"

        acct_view.append({
            "key": (a.get("key", "")[:8] + "...") if "key" in a else "",
            "balance": f"{balance:.2f} USDT",
            "trade": trade_info,
            "index": i
        })

    running = bot_ctrl.is_running()
    return render_template("index.html", accounts=acct_view, running=running)


@app.route("/add_account", methods=["POST"])
def add_account():
    key = request.form.get("key", "").strip()
    secret = request.form.get("secret", "").strip()
    if key and secret:
        bot_ctrl.add_account({"key": key, "secret": secret})
    return redirect(url_for("index"))


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
        try:
            while not log_queue.empty():
                yield f"data: {log_queue.get_nowait()}\n\n"
        except Exception:
            pass
        while True:
            try:
                line = log_queue.get()
                yield f"data: {line}\n\n"
            except GeneratorExit:
                break
            except Exception:
                break
    return Response(event_stream(), mimetype="text/event-stream")


# ---- STATUS ROUTE ----
@app.route("/status")
def status():
    accounts = bot_ctrl.load_accounts()
    messages = []

    for acc in accounts:
        session = bot_ctrl._make_session(acc)
        balance = 0.0
        if session:
            balance = bot_ctrl._get_balance_usdt(session)

        if acc.get("position") == "open":
            symbol = acc.get("current_symbol", "N/A")
            current_price = None
            if session and symbol != "N/A":
                current_price = bot_ctrl._get_ticker_last(session, symbol)

            # Elapsed time since entry
            elapsed_str = "N/A"
            if acc.get("entry_time"):
                elapsed = time.time() - float(acc["entry_time"])
                elapsed_str = str(timedelta(seconds=int(elapsed)))

            if current_price and acc.get("buy_price"):
                profit_pct = (current_price - float(acc["buy_price"])) / float(acc["buy_price"]) * 100
                messages.append(
                    f"Acct {acc.get('key','')[:6]}... | Bal: {balance:.2f} USDT | {symbol} | "
                    f"Buy: {acc['buy_price']} | Now: {current_price:.4f} | "
                    f"PnL: {profit_pct:.2f}% | Elapsed: {elapsed_str}"
                )
            else:
                messages.append(
                    f"Acct {acc.get('key','')[:6]}... | Bal: {balance:.2f} USDT | {symbol} | "
                    f"Buy: {acc.get('buy_price','N/A')} | Now: N/A | Elapsed: {elapsed_str}"
                )
        else:
            messages.append(
                f"Acct {acc.get('key','')[:6]}... | Bal: {balance:.2f} USDT | no open trade."
            )

    if not messages:
        messages.append("No accounts found.")

    return Response("\n".join(messages), mimetype="text/plain")


# ---- run server locally ----
if __name__ == "__main__":
    bot_ctrl.ensure_accounts_file()
    print(f"Starting Flask on 0.0.0.0:{APP_PORT}")
    app.run(host="0.0.0.0", port=APP_PORT, threaded=True)
