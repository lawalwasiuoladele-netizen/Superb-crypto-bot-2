# bot.py
import json
import os
import random
import threading
import time
import uuid
import math
from datetime import datetime
from pybit.unified_trading import HTTP

# ---- Allowed trading pairs (only coins under $1 or close to it) ----
ALLOWED_COINS = [
    "XRPUSDT",
    "DOGEUSDT",
    "TRXUSDT",
    "ADAUSDT",
    "MATICUSDT",
    "STXUSDT",
    "VETUSDT",
    "XLMUSDT",
    "SHIBUSDT"
]

# ---- Risk Parameters (configurable) ----
RISK_RULES = {
    "stop_loss": -5,        # -5% SL
    "tp1": (600, 8),        # First 10 min, 8% TP
    "tp2": (1020, 5),       # Next 7 min, 5% TP
    "tp3": (1200, 3),       # Last 3 min, 3% TP
    "max_hold": 1200        # 20 minutes max
}

class BotController:
    def __init__(self, log_queue=None):
        self.log_queue = log_queue
        self._thread = None
        self._running = False
        self._stop_event = threading.Event()
        self.accounts_file = "accounts.json"
        self.trades_file = "trades.json"
        self._file_lock = threading.Lock()

    # ---------------------- Logging ----------------------
    def log(self, msg: str):
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line)
        if self.log_queue:
            try:
                self.log_queue.put(line, block=False)
            except Exception:
                pass

    # ---------------------- File Helpers ----------------------
    def ensure_accounts_file(self):
        if not os.path.exists(self.accounts_file):
            with open(self.accounts_file, "w") as f:
                json.dump([], f)

    def ensure_trades_file(self):
        if not os.path.exists(self.trades_file):
            with open(self.trades_file, "w") as f:
                json.dump([], f)

    def load_accounts(self):
        self.ensure_accounts_file()
        try:
            with open(self.accounts_file, "r") as f:
                return json.load(f)
        except Exception:
            return []

    def save_accounts(self, accounts):
        with self._file_lock:
            with open(self.accounts_file, "w") as f:
                json.dump(accounts, f, indent=2)

    def load_trades(self):
        self.ensure_trades_file()
        try:
            with open(self.trades_file, "r") as f:
                return json.load(f)
        except Exception:
            return []

    def save_trades(self, trades):
        with self._file_lock:
            with open(self.trades_file, "w") as f:
                json.dump(trades, f, indent=2)

    def add_trade(self, trade):
        trades = self.load_trades()
        trades.append(trade)
        self.save_trades(trades)

    # ---------------------- Account Handling ----------------------
    def add_account(self, account):
        accounts = self.load_accounts()
        accounts.append(account)
        self.save_accounts(accounts)

    def remove_account_index(self, idx):
        accounts = self.load_accounts()
        if 0 <= idx < len(accounts):
            accounts.pop(idx)
            self.save_accounts(accounts)

    def update_account(self, acc):
        accounts = self.load_accounts()
        for i, a in enumerate(accounts):
            if a.get("key") == acc.get("key"):
                accounts[i].update(acc)
                break
        else:
            accounts.append(acc)
        self.save_accounts(accounts)

    # ---------------------- Bybit API ----------------------
    def _make_session(self, acc):
        return HTTP(testnet=False, api_key=acc["key"], api_secret=acc["secret"])

    def get_ticker_last(self, session, symbol):
        try:
            data = session.get_tickers(category="spot", symbol=symbol)
            return float(data["result"]["list"][0]["lastPrice"])
        except Exception as e:
            self.log(f"get_ticker_last error for {symbol}: {e}")
            return None

    def get_klines(self, session, symbol, interval="1", limit=30):
        try:
            resp = session.get_kline(category="spot", symbol=symbol, interval=str(interval), limit=limit)
            candles = resp.get("result", {}).get("list", [])
            if not candles:
                return []
            return list(reversed(candles))  # oldest → newest
        except Exception as e:
            self.log(f"get_klines error for {symbol}: {e}")
            return []

    def get_balance(self, session, acc, retries=3):
        for attempt in range(retries):
            try:
                balance_data = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]
                if not balance_data:
                    acc["last_balance"] = 0.0
                    self.update_account(acc)
                    return 0.0
                balance = float(balance_data[0].get("totalEquity", 0))
                acc["last_balance"] = balance
                self.update_account(acc)
                self.log(f"Account {acc['key'][:6]}... Balance: {balance}")
                return balance
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(2)
                    continue
                self.log(f"Balance fetch error for {acc['key'][:6]}...: {e}")
                return None

    # ---------------------- Indicators ----------------------
    def compute_rsi(self, closes, period=14):
        if len(closes) < period + 1:
            return None
        gains, losses = [], []
        for i in range(-period, 0):
            diff = closes[i] - closes[i - 1]
            if diff > 0:
                gains.append(diff)
            else:
                losses.append(abs(diff))
        avg_gain = sum(gains) / period if gains else 0.0001
        avg_loss = sum(losses) / period if losses else 0.0001
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    # ---------------------- Entry condition ----------------------
    def check_entry_condition(self, symbol, closes, current_price, volumes=None):
        try:
            rsi = self.compute_rsi(closes, 14)
            if rsi is None:
                return False
            rsi_signal = (rsi < 30) or (rsi > 70)

            if volumes and len(volumes) >= 11:
                avg_vol = sum(volumes[-11:-1]) / 10
                last_vol = volumes[-1]
                vol_spike = last_vol > 1.5 * avg_vol
            else:
                vol_spike = True

            short_ma = sum(closes[-9:]) / 9
            long_ma = sum(closes[-21:]) / 21 if len(closes) >= 21 else sum(closes) / len(closes)
            ma_cross = short_ma > long_ma

            if rsi_signal and vol_spike and ma_cross:
                self.log(f"📈 Entry OK {symbol} | RSI={round(rsi,2)} | ShortMA={round(short_ma,4)} > LongMA={round(long_ma,4)} | Vol spike={vol_spike}")
                return True
            else:
                self.log(f"❌ Entry FAIL {symbol} | RSI={round(rsi,2)} | ShortMA={round(short_ma,4)} vs LongMA={round(long_ma,4)} | Vol spike={vol_spike}")
                return False
        except Exception as e:
            self.log(f"⚠️ Error in check_entry_condition {symbol}: {e}")
            return False

    # ---------------------- Exit condition ----------------------
    def check_exit_condition(self, symbol, closes, current_price, volumes=None, short_window=9, long_window=21, momentum_window=5):
        try:
            rsi = self.compute_rsi(closes, 14)
            if rsi is None:
                return False
            rsi_signal = (rsi > 70) or (rsi < 30)

            import statistics
            if volumes and len(volumes) >= 11:
                base_vol = statistics.median(volumes[-11:-1])
                last_vol = volumes[-1]
                vol_drop = last_vol < 0.7 * base_vol
            else:
                vol_drop = True

            short_ma = sum(closes[-short_window:]) / short_window
            long_ma = sum(closes[-long_window:]) / long_window
            ma_cross = short_ma < long_ma

            momentum_weak = current_price < (sum(closes[-momentum_window:]) / momentum_window)

            if rsi_signal and (vol_drop or ma_cross or momentum_weak):
                self.log(f"📉 Exit OK {symbol} | RSI={round(rsi,2)} | ShortMA={round(short_ma,4)} < LongMA={round(long_ma,4)} | Vol drop={vol_drop} | MomentumWeak={momentum_weak}")
                return True
            else:
                self.log(f"❌ Exit FAIL {symbol} | RSI={round(rsi,2)} | ShortMA={round(short_ma,4)} vs LongMA={round(long_ma,4)} | Vol drop={vol_drop} | MomentumWeak={momentum_weak}")
                return False
        except Exception as e:
            self.log(f"⚠️ Error in check_exit_condition {symbol}: {e}")
            return False

    # ---------------------- Coin selection ----------------------
    def select_best_coin(self, session):
        try:
            candidates = []
            for sym in ALLOWED_COINS:
                candles = self.get_klines(session, sym, interval="1", limit=30)
                if not candles or len(candles) < 22:
                    continue
                closes = [float(c[4]) for c in candles]
                volumes = [float(c[5]) for c in candles]
                current_price = float(candles[-1][4])

                if current_price > 1.0:
                    continue

                if self.check_entry_condition(sym, closes, current_price, volumes):
                    short_ma = sum(closes[-9:]) / 9
                    long_ma = sum(closes[-21:]) / 21
                    momentum = (short_ma - long_ma) / long_ma
                    candidates.append((sym, current_price, momentum))

            if not candidates:
                self.log("⚠️ No coin passed entry filters")
                return None, None

            best = max(candidates, key=lambda x: x[2])
            self.log(f"🎯 Best coin = {best[0]} (score {best[2]:.4f}, price {best[1]:.6f})")
            return best[0], best[1]
        except Exception as e:
            self.log(f"⚠️ Error in select_best_coin: {e}")
            return None, None

    # ---------------------- Balance monitor ----------------------
    def balance_monitor(self, session, acc):
        while not self._stop_event.is_set() and acc.get("position") == "open":
            self.get_balance(session, acc)
            self._stop_event.wait(60)
        acc["monitoring"] = False
        self.update_account(acc)

    # ---------------------- Account runner ----------------------
    def run_account(self, acc):
        session = self._make_session(acc)
        while self._running and not self._stop_event.is_set():
            try:
                symbol, price = self.select_best_coin(session)
                if not symbol:
                    time.sleep(5)
                    continue

                acc["position"] = "open"
                acc["current_symbol"] = symbol
                acc["buy_price"] = price
                acc["entry_time"] = time.time()
                self.update_account(acc)

                self.log(f"Bought {symbol} at {price:.6f}")
                self.get_balance(session, acc)

                if not acc.get("monitoring"):
                    acc["monitoring"] = True
                    self.update_account(acc)
                    threading.Thread(target=self.balance_monitor, args=(session, acc), daemon=True).start()

                start_time = time.time()
                max_hold = RISK_RULES["max_hold"]
                sold = False

                while self._running and not sold and not self._stop_event.is_set():
                    elapsed = time.time() - start_time
                    current_price = self.get_ticker_last(session, symbol)
                    if not current_price:
                        time.sleep(5)
                        continue

                    candles = self.get_klines(session, symbol, interval="1", limit=30)
                    closes = [float(c[4]) for c in candles] if candles else []
                    volumes = [float(c[5]) for c in candles] if candles else []

                    profit_pct = (current_price - price) / price * 100

                    if profit_pct <= RISK_RULES["stop_loss"]:
                        self.log(f"⛔ Stop Loss hit ({profit_pct:.2f}%). Selling {symbol}.")
                        sold = True
                    elif elapsed <= RISK_RULES["tp1"][0] and profit_pct >= RISK_RULES["tp1"][1]:
                        self.log(f"✅ TP1 hit ({profit_pct:.2f}%). Selling {symbol}.")
                        sold = True
                    elif elapsed <= RISK_RULES["tp2"][0] and profit_pct >= RISK_RULES["tp2"][1]:
                        self.log(f"✅ TP2 hit ({profit_pct:.2f}%). Selling {symbol}.")
                        sold = True
                    elif elapsed <= RISK_RULES["tp3"][0] and profit_pct >= RISK_RULES["tp3"][1]:
                        self.log(f"✅ TP3 hit ({profit_pct:.2f}%). Selling {symbol}.")
                        sold = True
                    elif closes and self.check_exit_condition(symbol, closes, current_price, volumes):
                        self.log(f"📉 Exit condition met. Selling {symbol}.")
                        sold = True
                    elif elapsed >= max_hold:
                        self.log(f"⌛ Max hold time reached. Selling {symbol}.")
                        sold = True

                    if sold:
                        acc["position"] = "closed"
                        acc["sell_price"] = current_price
                        profit_pct = (current_price - price) / price * 100
                        self.update_account(acc)

                        exit_time = time.time()
                        hold_seconds = int(exit_time - acc["entry_time"])
                        trade_record = {
                            "id": str(uuid.uuid4()),
                            "account": acc["key"][:6] + "...",
                            "symbol": symbol,
                            "buy_price": price,
                            "sell_price": current_price,
                            "pnl_pct": round(profit_pct, 2),
                            "entry_time": datetime.utcfromtimestamp(acc["entry_time"]).strftime("%Y-%m-%d %H:%M:%S"),
                            "exit_time": datetime.utcfromtimestamp(exit_time).strftime("%Y-%m-%d %H:%M:%S"),
                            "hold_seconds": hold_seconds,
                        }
                        self.add_trade(trade_record)
                        self.log(f"Sold {symbol} at {current_price:.6f} | PnL: {profit_pct:.2f}% | Held {hold_seconds}s")
                        self.get_balance(session, acc)
                        break

                    time.sleep(5)
                time.sleep(random.randint(60, 180))
            except Exception as e:
                self.log(f"run_account error: {e}")
                time.sleep(5)

    # ---------------------- Control ----------------------
    def start(self):
        if self._running:
            self.log("Bot already running.")
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()
        self.log("Bot started.")

    def stop(self):
        self._stop_event.set()
        self._running = False
        self.log("Bot stopping...")

    def is_running(self):
        return self._running

    def run(self):
        accounts = self.load_accounts()
        if not accounts:
            self.log("No accounts configured.")
            return
        for acc in accounts:
            threading.Thread(target=self.run_account, args=(acc,), daemon=True).start()
