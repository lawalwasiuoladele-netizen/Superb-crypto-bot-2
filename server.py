"""
Flask dashboard + SSE log stream that controls the trading bot (bot.py).
Patched to add:
 - /account_debug/<index> endpoint returning raw API responses (UNIFIED, SPOT, tickers, klines)
 - accounts_data now surfaces last_error and last_balance per-account
 - stable SSE logs with heartbeat
 - accepts JSON start payload to replace accounts if provided
 - save_json_file uses bot_controller._file_lock to avoid races
"""
import os
import json
import queue
import traceback
from typing import Any, Optional
from flask import Flask, Response, render_template, request, jsonify, redirect, url_for

# import your bot controller
import bot
from pybit.unified_trading import HTTP

# Files
ACCOUNTS_FILE = "accounts.json"
TRADES_FILE = "trades.json"

# App
app = Flask(__name__, template_folder="templates")
log_queue: queue.Queue = queue.Queue()
bot_controller = bot.BotController(log_queue=log_queue)

# Ensure files exist
for path, default in ((ACCOUNTS_FILE, []), (TRADES_FILE, [])):
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump(default, f, indent=2)

# Helpers
def load_json_file(path: str, default: Optional[Any] = None) -> Any:
    if default is None:
        default = []
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default

def save_json_file(path: str, data: Any):
    # use bot_controller's lock to avoid races
    with getattr(bot_controller, '_file_lock'):
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)

def mask_key(k: Optional[str]) -> str:
    if not k:
        return ''
    s = str(k)
    if len(s) <= 8:
        return s[:3] + '****'
    return s[:6] + '****'

# SSE logs (both /logs and /stream for compatibility)
@app.route('/logs')
@app.route('/stream')
def stream_logs():
    def event_stream():
        heartbeat = 10
        while True:
            try:
                line = log_queue.get(timeout=heartbeat)
                yield f"data: {line}\n\n"
            except queue.Empty:
                # heartbeat to keep EventSource alive
                yield f": keep-alive\n\n"
    return Response(event_stream(), mimetype='text/event-stream')

# Index
@app.route('/')
def index():
    running = bot_controller.is_running()
    try:
        return render_template('index.html', running=running)
    except Exception:
        # fallback minimal page if template fails
        return f"<html><body><h3>Superb Bot Dashboard</h3><p>Running: {running}</p></body></html>"

# Start/Stop
@app.route('/start', methods=['POST'])
def start():
    # Allow starting with a JSON payload { accounts: [ {name, key, secret}, ... ] }
    data = request.get_json(silent=True)
    try:
        if data and isinstance(data, dict) and data.get('accounts'):
            new_accounts = []
            for i, a in enumerate(data.get('accounts') or []):
                na = {
                    'id': str(len(new_accounts) + 1),
                    'name': a.get('name') or f'Account {len(new_accounts)+1}',
                    'api_key': a.get('key') or a.get('api_key'),
                    'api_secret': a.get('secret') or a.get('api_secret'),
                    'balance': 0.0,
                    'last_validation_error': None,
                    'position': 'closed',
                    'monitoring': False,
                    'current_symbol': None,
                    'buy_price': None
                }
                new_accounts.append(na)
            save_json_file(ACCOUNTS_FILE, new_accounts)
        bot_controller.start()
        accounts_list = [a.get('name') for a in load_json_file(ACCOUNTS_FILE, [])]
        if request.is_json:
            return jsonify({'status': 'started', 'accounts': accounts_list})
        return redirect(url_for('index'))
    except Exception as e:
        bot_controller.log(f"❌ Failed to start bot: {e}")
        if request.is_json:
            return jsonify({'error': str(e)}), 500
        return redirect(url_for('index'))

@app.route('/stop', methods=['POST'])
def stop():
    try:
        bot_controller.stop()
        if request.is_json:
            return jsonify({'status': 'stopping'})
        return redirect(url_for('index'))
    except Exception as e:
        bot_controller.log(f"❌ Failed to stop bot: {e}")
        if request.is_json:
            return jsonify({'error': str(e)}), 500
        return redirect(url_for('index'))

@app.route('/accounts_data')
def accounts_data():
    accounts = load_json_file(ACCOUNTS_FILE, [])
    for a in accounts:
        a['key_masked'] = mask_key(a.get('api_key'))
        a['secret_masked'] = mask_key(a.get('api_secret'))
    return jsonify(accounts)

@app.route('/account_debug/<int:index>')
def account_debug(index: int):
    try:
        accounts = load_json_file(ACCOUNTS_FILE, [])
        if index < 0 or index >= len(accounts):
            return jsonify({'error': 'invalid_index'}), 400
        acct = accounts[index]
        client = bot_controller._get_client(acct)
        if not client:
            return jsonify({'error': 'no_client'}), 400

        out = {}
        try:
            out['UNIFIED'] = client.get_wallet_balance()
        except Exception as e:
            out['UNIFIED'] = f"error: {e}"
        try:
            out['SPOT'] = client.get_spot_wallet_balance() if hasattr(client, 'get_spot_wallet_balance') else None
        except Exception as e:
            out['SPOT'] = f"error: {e}"
        try:
            out['TICKERS'] = bot_controller.safe_get_ticker(client, 'BTCUSDT')
        except Exception as e:
            out['TICKERS'] = f"error: {e}"
        try:
            out['KLINES'] = bot_controller.safe_get_klines(client, 'BTCUSDT', '1', 5)
        except Exception as e:
            out['KLINES'] = f"error: {e}"

        return jsonify(out)
    except Exception as e:
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
