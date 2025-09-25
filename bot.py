# bot.py
"""Superb Crypto Bot Controller
- Multi-account trading
- Compounding with available USDT
- Time-based profit sell windows
- Force-sell at 20m
- 50 trades/day cap
- Accounts persisted in /data/accounts.json (Render) or ./accounts.json (local)
"""

import threading
import time
import json
import os
import datetime
import math
import traceback
from pybit.unified_trading import HTTP

# --- Config ---
DATA_DIR = os.environ.get("DATA_DIR") or ("/data" if os.path.exists("/data") else None)
if DATA_DIR:
    os.makedirs(DATA_DIR, exist_ok=True)
    DEFAULT_ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
else:
    DEFAULT_ACCOUNTS_FILE = "accounts.json"

SYMBOL = os.environ.get("SYMBOL", "BTCUSDT")
DAILY_TRADE_LIMIT = int(os.environ.get("DAILY_TRADE_LIMIT", "50"))
TRADE_TIMEOUT = int(os.environ.get("TRADE_TIMEOUT", str(20*60)))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "10"))  # seconds
MIN_USDT_BALANCE = float(os.environ.get("MIN_USDT_BALANCE", "5.0"))
RATE_LIMIT_SLEEP = float(os.environ.get("RATE_LIMIT_SLEEP", "0.35"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
TESTNET = False  # USER SELECTED: real trading (ensure API keys & permissions are correct)

class BotController:
    def __init__(self, log_queue=None, accounts_file=DEFAULT_ACCOUNTS_FILE):
        self.log_queue = log_queue
        self.accounts_file = accounts_file
        self._stop_event = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._running = False
        self._trade_count_today = 0
        self._trade_count_day = datetime.date.today()

    # ---------- logging ----------
    def log(self, txt):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {txt}"
        if self.log_queue:
            try:
                self.log_queue.put(line, block=False)
            except Exception:
                pass
        print(line)

    # ---------- accounts persistence ----------
    def ensure_accounts_file(self):
        if not os.path.exists(self.accounts_file):
            with open(self.accounts_file, "w") as f:
                json.dump([], f)

    def load_accounts(self):
        self.ensure_accounts_file()
        try:
            with open(self.accounts_file, "r") as f:
                return json.load(f)
        except Exception as e:
            self.log(f"Failed loading accounts: {e}")
            return []

    def save_accounts(self, accounts):
        try:
            with open(self.accounts_file, "w") as f:
                json.dump(accounts, f, indent=2)
        except Exception as e:
            self.log(f"Failed saving accounts: {e}")

    def add_account(self, acc):
        accounts = self.load_accounts()
        accounts.append(acc)
        self.save_accounts(accounts)
        self.log(f"Added account {acc.get('key','')[:8]}...")

    def remove_account_index(self, idx):
        accounts = self.load_accounts()
        if 0 <= idx < len(accounts):
            removed = accounts.pop(idx)
            self.save_accounts(accounts)
            self.log(f"Removed account {removed.get('key','')[:8]}...")
        else:
            self.log("Invalid account index to remove")


    # ---------- state ----------
    def is_running(self):
        return self._running

    def trade_count_today(self):
        return self._trade_count_today

    # ---------- start/stop ----------
    def start(self):
        with self._lock:
            if self._running:
                self.log("Bot already running.")
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._main_loop, daemon=True)
            self._thread.start()
            self._running = True
            self.log("Bot started in background.")
            return True

    def stop(self):
        self.log("Stop requested.")
        self._stop_event.set()
        for _ in range(20):
            if not self._running: break
            time.sleep(0.25)
        if self._running:
            self.log("Bot will stop after current loop.")
        else:
            self.log("Bot stopped.")

    # ---------- Bybit helpers ----------
    def _make_session(self, acc):
        try:
            return HTTP(api_key=acc["key"], api_secret=acc["secret"], testnet=TESTNET)
        except Exception as e:
            self.log(f"Session create error for {acc.get('key','')[:8]}: {e}")
            return None

    def _parse_usdt_balance(self, wallet_resp):
        try:
            for item in wallet_resp.get("result", {}).get("list", []):
                if item.get("coin") == "USDT":
                    return float(item.get("availableToWithdraw", 0.0))
        except Exception:
            pass
        return 0.0

    def _get_balance_usdt(self, session):
        try:
            resp = session.get_wallet_balance(accountType="UNIFIED")
            time.sleep(RATE_LIMIT_SLEEP)
            return self._parse_usdt_balance(resp)
        except Exception as e:
            self.log(f"get_balance error: {e}")
            return 0.0

    def _get_ticker_last(self, session, symbol=SYMBOL):
        try:
            data = session.get_ticker(category="spot", symbol=symbol)
            time.sleep(RATE_LIMIT_SLEEP)
            return float(data["result"]["list"][0]["lastPrice"])
        except Exception as e:
            self.log(f"get_ticker_last error: {e}")
            return None

    def _get_symbol_step(self, session, symbol=SYMBOL):
        try:
            info = session.get_instruments_info(category="spot", symbol=symbol)
            time.sleep(RATE_LIMIT_SLEEP)
            entry = info.get("result", {}).get("list", [])[0]
            # prefer lotSizeFilter.qtyStep/basePrecision
            lot = entry.get("lotSizeFilter") or entry
            if isinstance(lot, dict):
                for k in ("qtyStep", "basePrecision", "stepSize"):
                    if lot.get(k) is not None:
                        try:
                            return float(lot.get(k))
                        except:
                            pass
            return 1e-8
        except Exception as e:
            self.log(f"get_symbol_step error: {e}")
            return 1e-8

    def _floor_to_step(self, x, step):
        try:
            if step <= 0: return x
            mult = math.floor(x / step)
            return max(mult * step, 0.0)
        except Exception:
            return x

    def _place_market_order(self, session, symbol, side, qty):
        params = {
            "category": "spot",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": str(qty)
        }
        for attempt in range(1, MAX_RETRIES+1):
            try:
                session.place_order(**params)
                time.sleep(RATE_LIMIT_SLEEP)
                return True
            except Exception as e:
                self.log(f"{side} attempt {attempt} error: {e}")
                time.sleep(1)
        return False

    # ---------- sell decision ----------
    def _should_sell(self, entry_time, entry_price, current_price):
        profit_pct = (current_price - entry_price) / entry_price * 100.0
        elapsed = (time.time() - entry_time) / 60.0
        if elapsed <= 8: return profit_pct >= 30
        if elapsed <= 11: return profit_pct >= 25
        if elapsed <= 15: return profit_pct >= 20
        if elapsed <= 20: return profit_pct >= 15
        return True

    # ---------- trade loop ----------
    def _run_one_trade(self, sessions):
        try:
            first = sessions[0][0]
            entry_price = self._get_ticker_last(first)
            if not entry_price: return False
            step = self._get_symbol_step(first)
            self.log(f"Entry {entry_price:.4f}, step {step}")

            buys = []
            for sess, acc in sessions:
                usdt = self._get_balance_usdt(sess)
                if usdt < MIN_USDT_BALANCE:
                    self.log(f"{acc['key'][:8]} low balance {usdt:.2f} USDT, skip")
                    continue
                qty = self._floor_to_step(usdt / entry_price, step)
                qty = round(qty, 8)
                if qty <= 0:
                    self.log(f"{acc['key'][:8]} qty 0 after step floor, skip")
                    continue
                if self._place_market_order(sess, SYMBOL, "Buy", qty):
                    buys.append((sess, acc, qty))
                    self.log(f"{acc['key'][:8]} BUY {qty} (~{usdt:.2f} USDT)")

            if not buys:
                self.log("No successful buys this cycle.")
                return False

            entry_time = time.time()
            self.log("Monitoring sells...")

            while not self._stop_event.is_set():
                time.sleep(CHECK_INTERVAL)
                price = self._get_ticker_last(first)
                if not price: continue
                self.log(f"Price {price:.4f}, elapsed {int(time.time()-entry_time)}s")

                if self._should_sell(entry_time, entry_price, price) or time.time()-entry_time > TRADE_TIMEOUT:
                    for sess, acc, qty in buys:
                        self._place_market_order(sess, SYMBOL, "Sell", qty)
                        self.log(f"{acc['key'][:8]} SOLD {qty} at {price:.4f}")
                    return True
            # if stop requested, force sell above loop exits; handled by stop
            return False
        except Exception as e:
            self.log(f"Trade error: {e}")
            self.log(traceback.format_exc())
            return False

    def _main_loop(self):
        self.log("Bot loop started.")
        try:
            while not self._stop_event.is_set():
                accounts = self.load_accounts()
                if not accounts:
                    self.log("No accounts. Waiting...")
                    time.sleep(5)
                    continue
                sessions = []
                for a in accounts:
                    s = self._make_session(a)
                    if s:
                        sessions.append((s, a))
                if not sessions:
                    time.sleep(5)
                    continue

                if datetime.date.today() != self._trade_count_day:
                    self._trade_count_day = datetime.date.today()
                    self._trade_count_today = 0
                    self.log("New day — counter reset.")

                if self._trade_count_today >= DAILY_TRADE_LIMIT:
                    self.log("Daily cap reached. Sleeping...")
                    time.sleep(3600)
                    continue

                if self._run_one_trade(sessions):
                    self._trade_count_today += 1
                    self.log(f"Completed trade #{self._trade_count_today}")
                else:
                    time.sleep(5)
        finally:
            self._running = False
            self.log("Bot loop stopped.")
            self._stop_event.clear()
