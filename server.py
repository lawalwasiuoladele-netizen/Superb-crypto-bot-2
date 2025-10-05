"""
server.py — Dashboard server with per-account debug endpoint and SSE logs.
Replace existing server.py with this file.
"""
import os
import json
import queue
import traceback
from typing import Any, Dict, Optional
from flask import Flask, Response, render_template, request, jsonify, redirect, url_for

import bot
from pybit.unified_trading import HTTP

ACCOUNTS_FILE = "accounts.json"
TRADES_FILE = "trades.json"

app = Flask(__name__, template_folder="templates")
log_queue: queue.Queue = queue.Queue()
bot_controller = bot.BotController(log_queue=log_queue)

# Ensure files exist
for path, default in ((ACCOUNTS_FILE, []), (TRADES_FILE, [])):
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump(default, f, indent=2)

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
    s = str(key)
    if len(s) <= 8:
        return s[:3] + "****"
    return s[:6] + "****"

# SSE logs
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
                yield ": keep-alive\n\n"
    return Response(event_stream(), mimetype='text/event-stream')

@app.route('/')
def index():
    running = bot_controller.is_running()
    try:
        return render_template('index.html', running=running)
    except Exception:
        return f"<html><body><h3>Superb Bot Dashboard</h3><p>Running: {running}</p></body></html>"

# Start (accept JSON {accounts: [...] } to replace accounts if provided)
@app.route('/start', methods=['POST'])
def start():
    data = request.get_json(silent=True)
    try:
        if data and isinstance(data, dict) and data.get('accounts'):
            new_accounts = []
            for a in data.get('accounts') or []:
                na = {
                    'id': str(len(new_accounts) + 1),
                    'name': a.get('name') or f'Account {len(new_accounts)+1}',
                    'key': a.get('key') or a.get('api_key'),
                    'secret': a.get('secret') or a.get('api_secret'),
                    'last_balance': 0.0,
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

# Accounts
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
        'id': str(len(accounts) + 1),
        'name': name or f'Account {len(accounts)+1}',
        'key': key,
        'secret': secret,
        'last_balance': 0.0,
        'position': 'closed',
        'monitoring': False,
        'current_symbol': None,
        'buy_price': None
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

@app.route('/accounts_data')
def accounts_data():
    accounts = load_json_file(ACCOUNTS_FILE, [])
    updated = False

    for acc in accounts:
        try:
            ok, bal, err = bot_controller.validate_account(acc, retries=1)
            if ok and bal is not None:
                if acc.get('last_balance') != bal:
                    acc['last_balance'] = bal
                    updated = True
                acc.pop('last_error', None)
            else:
                acc['last_error'] = err or 'validation failed'
        except Exception as e:
            acc['last_error'] = str(e)

    if updated:
        save_json_file(ACCOUNTS_FILE, accounts)

    data = []
    for i, acc in enumerate(accounts):
        trade_info = None
        if acc.get('position') == 'open':
            trade_info = {
                'symbol': acc.get('current_symbol'),
                'buy_price': acc.get('buy_price'),
                'entry_time': acc.get('last_trade_start'),
                'current_price': acc.get('current_price', 'N/A'),
                'pnl': acc.get('current_pnl', 0.0)
            }
        data.append({
            'index': i,
            'name': acc.get('name'),
            'key': mask_key(acc.get('key')),
            'balance': acc.get('last_balance', 0.0),
            'pos': acc.get('position', 'closed'),
            'trade': trade_info,
            'last_error': acc.get('last_error'),
            'last_raw_preview': acc.get('last_raw_preview', {})
        })
    return jsonify(data)

@app.route('/account_debug/<int:idx>')
def account_debug(idx: int):
    accounts = load_json_file(ACCOUNTS_FILE, [])
    if idx < 0 or idx >= len(accounts):
        return jsonify({'error': 'index out of range'}), 400
    acc = accounts[idx]
    key = acc.get('key')
    secret = acc.get('secret')
    debug_result: Dict[str, Any] = {'note': 'Do not share secrets. This output may contain exchange response structure.'}
    try:
        testnet_flag = acc.get('test_on_testnet', TRADE_SETTINGS["test_on_testnet"])
        session = HTTP(testnet=bool(testnet_flag), api_key=key, api_secret=secret)

        # unified
        try:
            unified = session.get_wallet_balance(accountType='UNIFIED')
            debug_result['unified_raw'] = unified
        except Exception as e:
            debug_result['unified_raw_error'] = str(e)

        # spot
        try:
            spot = session.get_wallet_balance(accountType='SPOT')
            debug_result['spot_raw'] = spot
        except Exception as e:
            debug_result['spot_raw_error'] = str(e)

        # tickers (small sample)
        try:
            sample = []
            allowed = getattr(bot, 'ALLOWED_COINS', [])[:6]
            for s in allowed:
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
            first_sym = getattr(bot, 'ALLOWED_COINS', ['ADAUSDT'])[0]
            kl = session.get_kline(category='spot', symbol=first_sym, interval='1', limit=3)
            debug_result['kline_sample'] = kl
        except Exception as e:
            debug_result['kline_sample_error'] = str(e)

    except Exception as e:
        debug_result['session_error'] = str(e)
        debug_result['session_trace'] = traceback.format_exc()

    return jsonify(debug_result)

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
        if acc not in accounts_summary:
            accounts_summary[acc] = {'trades': 0, 'wins': 0, 'losses': 0, 'total_pnl': 0.0}
        accounts_summary[acc]['trades'] += 1
        if t.get('profit_pct', 0) > 0:
            accounts_summary[acc]['wins'] += 1
        elif t.get('profit_pct', 0) < 0:
            accounts_summary[acc]['losses'] += 1
        accounts_summary[acc]['total_pnl'] += t.get('profit_pct', 0)
    return jsonify({'total_trades': total_trades, 'wins': wins, 'losses': losses, 'total_pnl': total_pnl, 'accounts': accounts_summary})

@app.route('/status')
def status():
    return jsonify({'running': bot_controller.is_running()})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True, threaded=True)
