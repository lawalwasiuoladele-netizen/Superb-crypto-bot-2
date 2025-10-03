# server.py
"""
Flask dashboard + SSE log stream that controls the trading bot (bot.py).
- Keeps layout unchanged for your existing front-end.
- Mainnet-only by default (no testnet confusion).
- Heartbeat-enabled SSE so the browser won't constantly reconnect.
"""

import os
import json
import time
import queue
from typing import Any, Dict, List, Optional, Tuple

from flask import (
    Flask,
    Response,
    render_template,
    request,
    jsonify,
    redirect,
    url_for,
)

# Import your BotController from bot.py (expects class BotController(log_queue=...))
from bot import BotController

# ----------------- Config / Files -----------------
ACCOUNTS_FILE = "accounts.json"
TRADES_FILE = "trades.json"

# ----------------- App + Bot -----------------
app = Flask(__name__, template_folder="templates", static_folder="static")
log_queue: queue.Queue = queue.Queue()
bot = BotController(log_queue=log_queue)

# Ensure storage files exist
def ensure_file(path: str, default: Any):
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump(default, f, indent=2)

ensure_file(ACCOUNTS_FILE, [])
ensure_file(TRADES_FILE, [])

# ----------------- Helpers -----------------
def load_json_file(path: str, default: Optional[Any] = None) -> Any:
    if default is None:
        default = []
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default

def save_json_file(path: str, data: Any):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def mask_key(key: Optional[str]) -> str:
    if not key:
        return ""
    k = str(key)
    if len(k) <= 6:
        return k[:3] + "****"
    return k[:6] + "****"

def normalize_account_fields(acc: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize historical naming differences: api_key/api_secret vs key/secret."""
    # copy to avoid accidental mutation of caller
    a = dict(acc)
    if "api_key" in a and "key" not in a:
        a["key"] = a.get("api_key")
    if "api_secret" in a and "secret" not in a:
        a["secret"] = a.get("api_secret")
    # defaults
    a.setdefault("name", a.get("name") or a.get("id") or "Account")
    a.setdefault("last_balance", a.get("last_balance", 0.0))
    a.setdefault("position", a.get("position", "closed"))
    a.setdefault("monitoring", a.get("monitoring", False))
    a.setdefault("last_trade_start", a.get("last_trade_start", None))
    a.setdefault("current_symbol", a.get("current_symbol", None))
    a.setdefault("buy_price", a.get("buy_price", None))
    return a

# ----------------- Balance helpers -----------------
def _extract_key_secret(acc: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    # Accept both shapes
    key = acc.get("key") or acc.get("api_key") or acc.get("k") or None
    secret = acc.get("secret") or acc.get("api_secret") or acc.get("s") or None
    return key, secret

def refresh_balance_for_account(acc: Dict[str, Any]) -> Tuple[bool, Optional[float], Optional[str]]:
    """
    Attempt a one-shot balance fetch using bot.validate_account (which uses pybit).
    Returns (ok, balance_or_none, error_message_or_none)
    """
    try:
        # Ensure fields normalized so BotController.validate_account sees 'key'/'secret'
        acc_norm = normalize_account_fields(acc)
        ok, bal, err = bot.validate_account(acc_norm, retries=1)
        if ok:
            # update file-backed account with last_balance
            acc["last_balance"] = bal
            # persist change
            accounts = load_json_file(ACCOUNTS_FILE, [])
            updated = False
            for a in accounts:
                # match by key if possible else by name
                if (a.get("key") and acc.get("key") and a.get("key") == acc.get("key")) or (a.get("name") == acc.get("name")):
                    a.update(acc)
                    updated = True
                    break
            if not updated:
                accounts.append(acc)
            save_json_file(ACCOUNTS_FILE, accounts)
            return True, bal, None
        else:
            return False, None, err
    except Exception as e:
        return False, None, str(e)

# ----------------- SSE logs -----------------
@app.route("/logs")
@app.route("/stream")  # alias for older UIs
def logs():
    """
    Server-Sent Events endpoint that streams log_queue.
    Sends a heartbeat every 10 seconds if no logs to keep connection alive.
    """
    def event_stream():
        heartbeat_interval = 10
        while True:
            try:
                line = log_queue.get(timeout=heartbeat_interval)
                # yield actual log line
                yield f"data: {line}\n\n"
            except queue.Empty:
                # heartbeat to keep connection open
                yield "data: [heartbeat]\n\n"
    return Response(event_stream(), mimetype="text/event-stream")

# ----------------- Front page -----------------
@app.route("/")
def index():
    # Keep layout the same: the template you already use will be rendered.
    # template should call /accounts_data, /trades_data, /summary_data, /logs as before.
    running = bot.is_running()
    # try to render user's existing index.html in templates folder
    try:
        return render_template("index.html", running=running)
    except Exception:
        # fallback minimal page if template missing (safe)
        return f"<html><body><h3>Superb Bot Dashboard</h3><p>Running: {running}</p></body></html>"

# ----------------- Bot control -----------------
@app.route("/start", methods=["POST"])
def start():
    """
    Start the bot. Accepts either:
    - JSON with {"accounts": [{name,key,secret}, ...]} to replace accounts.json before start
    - or starts using accounts.json as-is.
    Returns JSON for fetch() clients. If called from a classic form, redirects to index.
    """
    data = request.get_json(silent=True)
    try:
        if data and isinstance(data, dict) and data.get("accounts"):
            # Replace accounts file (safe normalized structure)
            new_accounts = []
            for i, a in enumerate(data.get("accounts") or []):
                na = {
                    "id": str(i + 1),
                    "name": a.get("name") or f"Account {i+1}",
                    "key": a.get("key") or a.get("api_key"),
                    "secret": a.get("secret") or a.get("api_secret"),
                    "last_balance": 0.0,
                    "position": "closed",
                    "monitoring": False,
                    "last_trade_start": None,
                    "current_symbol": None,
                    "buy_price": None
                }
                new_accounts.append(na)
            save_json_file(ACCOUNTS_FILE, new_accounts)
        # Start the bot (BotController.start is idempotent)
        bot.start()
        accounts = load_json_file(ACCOUNTS_FILE, [])
        names = [a.get("name", a.get("key", "")[:6]) for a in accounts]
        # return JSON for JS frontends
        if request.is_json:
            return jsonify({"status": "started", "accounts": names})
        # otherwise redirect back
        return redirect(url_for("index"))
    except Exception as e:
        bot.log(f"❌ Failed to start bot: {e}")
        if request.is_json:
            return jsonify({"error": str(e)}), 500
        return redirect(url_for("index"))

@app.route("/stop", methods=["POST"])
def stop():
    try:
        bot.stop()
        if request.is_json:
            return jsonify({"status": "stopping"})
        return redirect(url_for("index"))
    except Exception as e:
        bot.log(f"❌ Failed to stop bot: {e}")
        if request.is_json:
            return jsonify({"error": str(e)}), 500
        return redirect(url_for("index"))

# ----------------- Accounts API -----------------
@app.route("/add_account", methods=["POST"])
def add_account():
    """
    Accepts JSON body or form data with name/key/secret
    """
    data = request.get_json(silent=True) or request.form
    name = data.get("name")
    key = data.get("key") or data.get("api_key")
    secret = data.get("secret") or data.get("api_secret")
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
    bot.log(f"Account added: {name or key[:6]}****")
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
        bot.log(f"Removed account: {removed.get('name')}")
        return jsonify({"status": "removed", "removed": removed.get("name")})
    return jsonify({"error": "index out of range"}), 400

@app.route("/accounts_data")
def accounts_data():
    """
    Returns accounts for the frontend. If last_balance is missing or zero,
    attempt a one-shot balance refresh (best-effort).
    """
    accounts = load_json_file(ACCOUNTS_FILE, [])
    updated = False
    for acc in accounts:
        # normalize legacy fields if any
        acc = normalize_account_fields(acc)
        if acc.get("last_balance") in (None, 0.0):
            try:
                ok, bal, err = refresh_balance_for_account(acc)
                if ok and bal is not None:
                    acc["last_balance"] = bal
                    updated = True
                else:
                    # keep previous value but log error
                    bot.log(f"Balance refresh failed for ****{(acc.get('key') or '')[-4:]}: {err}")
            except Exception as e:
                bot.log(f"Balance refresh exception for ****{(acc.get('key') or '')[-4:]}: {e}")
    if updated:
        save_json_file(ACCOUNTS_FILE, accounts)

    # prepare data for frontend (keep layout same)
    data = []
    for i, acc in enumerate(accounts):
        data.append({
            "index": i,
            "name": acc.get("name"),
            "key": mask_key(acc.get("key")),
            "balance": acc.get("last_balance", None),
            "trade": {
                "symbol": acc.get("current_symbol"),
                "buy_price": acc.get("buy_price"),
                "entry_time": acc.get("last_trade_start"),
                "current_price": acc.get("current_price", "N/A"),
                "pnl": acc.get("current_pnl", 0.0)
            } if acc.get("position") == "open" else None
        })
    return jsonify(data)

# ----------------- Trades / Summary -----------------
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

    accounts_summary: Dict[str, Dict[str, Any]] = {}
    for t in trades:
        acc_name = t.get("account", "Unknown")
        if acc_name not in accounts_summary:
            accounts_summary[acc_name] = {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
        accounts_summary[acc_name]["trades"] += 1
        if t.get("profit_pct", 0) > 0:
            accounts_summary[acc_name]["wins"] += 1
        elif t.get("profit_pct", 0) < 0:
            accounts_summary[acc_name]["losses"] += 1
        accounts_summary[acc_name]["total_pnl"] += t.get("profit_pct", 0)

    return jsonify({
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "total_pnl": total_pnl,
        "accounts": accounts_summary
    })

# ----------------- Status -----------------
@app.route("/status")
def status():
    return jsonify({"running": bot.is_running()})

# ----------------- Run -----------------
if __name__ == "__main__":
    # Note: threaded=True helps with SSE. If you run under gunicorn,
    # use a single worker: `gunicorn -w 1 server:app` (avoid eventlet/gevent unless tested).
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True)
