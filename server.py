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
from flask import Flask, Response, request, jsonify, render_template
from bot import BotController

# ----------------- Setup -----------------
app = Flask(__name__)
log_queue = queue.Queue()
bot = BotController(log_queue=log_queue)

# ----------------- Routes -----------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/logs")
def logs():
    def stream():
        while True:
            try:
                line = log_queue.get(timeout=1)
                yield f"data: {line}\n\n"
            except queue.Empty:
                yield ": keep-alive\n\n"
    return Response(stream(), mimetype="text/event-stream")

@app.route("/start", methods=["POST"])
def start():
    if bot.is_running():
        return jsonify({"status": "already running"})
    try:
        bot.start()
        return jsonify({"status": "started"})
    except Exception as e:
        bot.log(f"❌ Bot failed to start: {e}")
        return jsonify({"status": "error", "msg": str(e)}), 500

@app.route("/stop", methods=["POST"])
def stop():
    if not bot.is_running():
        return jsonify({"status": "already stopped"})
    bot.stop()
    return jsonify({"status": "stopping"})

@app.route("/status")
def status():
    return jsonify({"running": bot.is_running()})

@app.route("/accounts", methods=["GET", "POST"])
def accounts():
    if request.method == "GET":
        return jsonify(bot.load_accounts())
    else:
        data = request.json
        accounts = bot.load_accounts()
        accounts.append({
            "id": str(len(accounts) + 1),
            "name": data.get("name", f"Account {len(accounts) + 1}"),
            "key": data.get("key"),
            "secret": data.get("secret"),
            "last_balance": 0.0,
            "position": "closed",
            "monitoring": False
        })
        bot.save_accounts(accounts)
        return jsonify({"status": "added"})

@app.route("/balance")
def balance():
    accounts = bot.load_accounts()
    balances = {}
    for acc in accounts:
        try:
            session = bot._make_session(acc)
            bal = bot.get_balance(session, acc)
            balances[acc["name"]] = bal
        except Exception as e:
            balances[acc["name"]] = f"error: {e}"
    return jsonify(balances)

# ----------------- Run -----------------
if __name__ == "__main__":
    app.run(debug=True, threaded=True)
