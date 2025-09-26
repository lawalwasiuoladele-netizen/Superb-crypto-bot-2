# server.py
"""Flask dashboard + SSE log stream that controls the trading bot (bot.py).
Usage:
  python server.py
Then open http://127.0.0.1:5000
"""
import threading
import time
import json
import os
import queue
from flask import Flask, render_template, request, redirect, url_for, Response

# local import of the bot logic
from bot import BotController  # BotController expects a log_queue and uses accounts.json

APP_PORT = int(os.environ.get("APP_PORT", "5000"))

app = Flask(__name__, template_folder="templates")
log_queue = queue.Queue(maxsize=1000)

# BotController will manage sessions, start/stop, and use log_queue
bot_ctrl = BotController(log_queue=log_queue)

# ---------- ROUTES ----------
@app.route("/")
def index():
    # load accounts for display
    accounts = bot_ctrl.load_accounts()
    # present only first 8 chars of key for UI
    acct_view = [{"key": (a.get("key","")[:8]+"...") if "key" in a else "", "index": i} for i,a in enumerate(accounts)]
    running = bot_ctrl.is_running()
    return render_template("index.html", accounts=acct_view, running=running)

@app.route("/add_account", methods=["POST"])
def add_account():
    key = request.form.get("key","").strip()
    secret = request.form.get("secret","").strip()
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
    # start bot in background (keeps running even if the request ends)
    started = bot_ctrl.start()
    time.sleep(0.1)
    return redirect(url_for("index"))

@app.route("/stop", methods=["POST"])
def stop():
    bot_ctrl.stop()
    time.sleep(0.1)
    return redirect(url_for("index"))

@app.route("/stream")
def stream():
    # Server-Sent Events (SSE)
    def event_stream():
        # Send a small header/history
        try:
            # non-blocking initial drain (best-effort)
            while not log_queue.empty():
                yield f"data: {log_queue.get_nowait()}\n\n"
        except Exception:
            pass
        # Then block on new logs
        while True:
            try:
                line = log_queue.get()
                yield f"data: {line}\n\n"
            except GeneratorExit:
                break
            except Exception:
                break
    return Response(event_stream(), mimetype="text/event-stream")

# ----------------- simple status endpoint -----------------
@app.route("/status")
def status():
    return {"running": bot_ctrl.is_running(), "trade_count_today": bot_ctrl.trade_count_today()}

# ----------------- run server -----------------
if __name__ == "__main__":
    # ensure accounts.json exists
    bot_ctrl.ensure_accounts_file()
    print(f"Starting Flask on 0.0.0.0:{APP_PORT}")
    app.run(host="0.0.0.0", port=APP_PORT, threaded=True)
from flask import Flask, render_template

app = Flask(__name__, template_folder="templates")

@app.route("/")
def home():
    try:
        return render_template("index.html")
    except Exception as e:
        return f"Error rendering template: {e}", 500
