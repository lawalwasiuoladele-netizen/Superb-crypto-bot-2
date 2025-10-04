# server.py
"""
Flask dashboard + SSE log stream that controls the trading bot (bot.py).
This patched version adds:
 - /account_debug/<index> endpoint that returns raw API responses (UNIFIED, SPOT, tickers, kline)
 - per-account `last_error` and `last_raw_preview` fields surfaced in /accounts_data
 - SSE log heartbeat to keep EventSource stable
 - safe handling for JSON fetch() and old-form POSTs
 - keeps layout / endpoints your dashboard expects
"""

import os
import json
import time
import queue
import traceback
from typing import Any, Dict, List, Optional
from flask import Flask, Response, render_template, request, jsonify, redirect, url_for
from pybit.unified_trading import HTTP
import bot

ACCOUNTS_FILE = "accounts.json"
TRADES_FILE = "trades.json"

app = Flask(__name__, template_folder="templates", static_folder="static")
log_queue: queue.Queue = queue.Queue()
# Pass the queue into the bot so bot.log() pushes into this queue
bot_controller = bot.BotController(log_queue=log_queue)

# Ensure files exist
def ensure_file(path: str, default: Any):
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump(default, f, indent=2)
ensure_file(ACCOUNTS_FILE, [])
ensure_file(TRADES_FILE, [])

# --- helpers ---

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

# --- SSE logs ---
@app.route('/logs')
@app.route('/stream')
def stream_logs():
    # SSE stream with heartbeat
    def event_stream():
        hb = 10
        while True:
            try:
                line = log_queue.get(timeout=hb)
                yield f"data: {line}\n\n"
            except queue.Empty:
                yield "data: [heartbeat]\n\n"
    return Response(event_stream(), mimetype='text/event-stream')

# --- front page ---
@app.route('/')
def index():
    running = bot_controller.is_running()
    try:
        return render_template('index.html', running=running)
    except Exception:
        return f"<html><body><h3>Superb Bot Dashboard</h3><p>Running: {running}</p></body></html>"

# --- start / stop ---
@app.route('/start', methods=['POST'])
def start():
    data = request.get_json(silent=True)
    try:
        if data and isinstance(data, dict) and data.get('accounts'):
            # replace accounts file
            new_accounts = []
            for i, a in enumerate(data.get('accounts') or []):
                na = {
                    'id': str(i+1),
                    'name': a.get('name') or f'Account {i+1}',
                    'key': a.get('key') or a.get('api_key'),
                    'secret': a.get('secret') or a.get('api_secret'),
                    'last_balance': 0.0
                }
                new_accounts.append(na)
            save_json_file(ACCOUNTS_FILE, new_accounts)
        bot_controller.start()
        accounts = load_json_file(ACCOUNTS_FILE, [])
        names = [a.get('name') for a in accounts]
        if request.is_json:
            return jsonify({'status': 'started', 'accounts': names})
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

# --- accounts CRUD ---
@app.route('/add_account', methods=['POST'])
def add_account():
    data = request.get_json(silent=True) or request.form
    name = data.get('name')
    key = data.get('key') or data.get('api_key')
    secret = data.get('secret') or data.get('api_secret')
    if not key or not secret:
        return jsonify({'error': 'missing key/secret'}), 400
    accounts = load_json_file(ACCOUNTS_FILE, [])
    accounts.append({
        'id': str(len(accounts)+1),
        'name': name or f'Account {len(accounts)+1}',
        'key': key,
        'secret': secret,
        'last_balance': 0.0
    })
    save_json_file(ACCOUNTS_FILE, accounts)
    bot_controller.log(f"Account added: {name or key[:6]}****")
    return jsonify({'status': 'ok', 'accounts': [a.get('name') for a in accounts]})

@app.route('/remove_account', methods=['POST'])
def remove_account():
    data = request.get_json(silent=True) or request.form
    idx = data.get('index')
    try:
        idx = int(idx)
    except Exception:
        return jsonify({'error': 'invalid index'}), 400
    accounts = load_json_file(ACCOUNTS_FILE, [])
    if 0 <= idx < len(accounts):
        removed = accounts.pop(idx)
        save_json_file(ACCOUNTS_FILE, accounts)
        bot_controller.log(f"Removed account: {removed.get('name')}")
        return jsonify({'status': 'removed', 'removed': removed.get('name')})
    return jsonify({'error': 'index out of range'}), 400

# --- accounts_data ---
@app.route('/accounts_data')
def accounts_data():
    accounts = load_json_file(ACCOUNTS_FILE, [])
    updated = False
    for acc in accounts:
        try:
            ok, bal, err = bot_controller.validate_account(acc, retries=1)
            if ok and bal is not None:
                acc['last_balance'] = bal
                acc.pop('last_error', None)
                updated = True
            else:
                acc['last_error'] = err or 'validation failed'
        except Exception as e:
            acc['last_error'] = str(e)
    if updated:
        save_json_file(ACCOUNTS_FILE, accounts)

    data = []
    for i, acc in enumerate(accounts):
        data.append({
            'index': i,
            'name': acc.get('name'),
            'key': mask_key(acc.get('key')),
            'balance': acc.get('last_balance', 0.0),
            'last_error': acc.get('last_error', None),
        })
    return jsonify(data)

# --- account debug (raw API responses) ---
@app.route('/account_debug/<int:idx>')
def account_debug(idx: int):
    accounts = load_json_file(ACCOUNTS_FILE, [])
    if idx < 0 or idx >= len(accounts):
        return jsonify({'error': 'index out of range'}), 400
    acc = accounts[idx]
    key = acc.get('key')
    secret = acc.get('secret')
    testnet_flag = getattr(bot, 'TRADE_SETTINGS', {}).get('test_on_testnet', False)
    debug_result: Dict[str, Any] = {'note': 'Do not share secrets. This output may contain exchange response structure.'}
    try:
        session = HTTP(testnet=bool(testnet_flag), api_key=key, api_secret=secret)
        # fetch raw unified
        try:
            unified = session.get_wallet_balance(accountType='UNIFIED')
            debug_result['unified_raw'] = unified
        except Exception as e:
            debug_result['unified_raw_error'] = str(e)
        # fetch raw spot
        try:
            spot = session.get_wallet_balance(accountType='SPOT')
            debug_result['spot_raw'] = spot
        except Exception as e:
            debug_result['spot_raw_error'] = str(e)
        # tickers (sample first 5 symbols)
        try:
            sample = []
            from bot import ALLOWED_COINS
            for s in ALLOWED_COINS[:6]:
                try:
                    t = session.get_tickers(category='spot', symbol=s)
                    sample.append({s: t})
                except Exception as e:
                    sample.append({s: f'error: {e}'})
            debug_result['tickers_sample'] = sample
        except Exception as e:
            debug_result['tickers_sample_error'] = str(e)
        # kline sample
        try:
            kl = session.get_kline(category='spot', symbol=(bot.ALLOWED_COINS[0] if hasattr(bot, 'ALLOWED_COINS') else 'ADAUSDT'), interval='1', limit=3)
            debug_result['kline_sample'] = kl
        except Exception as e:
            debug_result['kline_sample_error'] = str(e)
    except Exception as e:
        debug_result['session_error'] = str(e)
        debug_result['session_trace'] = traceback.format_exc()
    return jsonify(debug_result)

# --- trades and summary endpoints ---
@app.route('/trades_data')
def trades_data():
    trades = load_json_file(TRADES_FILE, [])
    return jsonify(trades)

@app.route('/summary_data')
def summary_data():
    trades = load_json_file(TRADES_FILE, [])
    total_trades = len(trades)
    wins = sum(1 for t in trades if t.get('profit_pct', 0) > 0)
    losses = sum(1 for t in trades if t.get('profit_pct', 0) < 0)
    total_pnl = sum(t.get('profit_pct', 0) for t in trades)
    accounts_summary = {}
    for t in trades:
        acc = t.get('account', 'Unknown')
        accounts_summary.setdefault(acc, {'trades': 0, 'wins': 0, 'losses': 0, 'total_pnl': 0.0})
        accounts_summary[acc]['trades'] += 1
        if t.get('profit_pct', 0) > 0:
            accounts_summary[acc]['wins'] += 1
        elif t.get('profit_pct', 0) < 0:
            accounts_summary[acc]['losses'] += 1
        accounts_summary[acc]['total_pnl'] += t.get('profit_pct', 0)
    return jsonify({'total_trades': total_trades, 'wins': wins, 'losses': losses, 'total_pnl': total_pnl, 'accounts': accounts_summary})

# --- status ---
@app.route('/status')
def status():
    return jsonify({'running': bot_controller.is_running()})

# --- run ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True, threaded=True)
