import json
import os
import random
import threading
import time
import uuid
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


# ---- Bot Controller ----
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
            self.log_queue.put(line)

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
        """Update account state safely"""
        accounts = self.load_accounts()
        for i, a in enumerate(accounts):
            if a["key"] == acc["key"]:
                accounts[i].update(acc)  # safer than overwrite
                break
        self.save_accounts(accounts)

    # ---------------------- Bybit API ----------------------
    def _make_session(self, acc):
        return HTTP(
            testnet=False,
            api_key=acc["key"],
            api_secret=acc["secret"]
        )

    def get_ticker_last(self, session, symbol):
        try:
            data = session.get_tickers(category="spot", symbol=symbol)
            return float(data["result"]["list"][0]["lastPrice"])
        except Exception as e:
            self.log(f"get_ticker_last error: {e}")
            return None

    def get_balance(self, session, acc, retries=3):
        """Fetch and log balance with retry"""
        for attempt in range(retries):
            try:
                balance_data = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]
                if not balance_data:
                    self.log(f"Account {acc['key'][:6]}... has no balance data.")
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

    # ---------------------- Strategy ----------------------
    def pick_allowed_coin(self, session):
        try:
            random.shuffle(ALLOWED_COINS)
            for symbol in ALLOWED_COINS:
                price = self.get_ticker_last(session, symbol)
                if price and price <= 1.0:
                    return symbol, price
            self.log("No allowed coin under $1 available this round.")
            return None, None
        except Exception as e:
            self.log(f"pick_allowed_coin error: {e}")
            return None, None

    def balance_monitor(self, session, acc):
        """Logs balance every minute while trade is open"""
        while not self._stop_event.is_set() and acc.get("position") == "open":
            self.get_balance(session, acc)
            self._stop_event.wait(60)
        acc["monitoring"] = False

    def run_account(self, acc):
        session = self._make_session(acc)

        while self._running and not self._stop_event.is_set():
            try:
                symbol, price = self.pick_allowed_coin(session)
                if not symbol:
                    time.sleep(5)
                    continue

                # ---- Simulate buy ----
                acc["position"] = "open"
                acc["current_symbol"] = symbol
                acc["buy_price"] = price
                acc["entry_time"] = time.time()
                self.update_account(acc)

                self.log(f"Bought {symbol} at {price:.4f}")
                self.get_balance(session, acc)

                # Start balance monitor
                if not acc.get("monitoring"):
                    acc["monitoring"] = True
                    threading.Thread(
                        target=self.balance_monitor, args=(session, acc), daemon=True
                    ).start()

                # ---- Trade management ----
                start_time = time.time()
                sold = False

                while self._running and not sold and not self._stop_event.is_set():
                    elapsed = time.time() - start_time
                    current_price = self.get_ticker_last(session, symbol)
                    if not current_price:
                        time.sleep(5)
                        continue

                    profit_pct = (current_price - price) / price * 100

                    # ---- Stop Loss ----
                    if profit_pct <= RISK_RULES["stop_loss"]:
                        self.log(f"⛔ Stop Loss hit ({profit_pct:.2f}%). Selling {symbol}.")
                        sold = True

                    # ---- Take Profit tiers ----
                    elif elapsed <= RISK_RULES["tp1"][0] and profit_pct >= RISK_RULES["tp1"][1]:
                        self.log(f"✅ TP1 hit ({profit_pct:.2f}%). Selling {symbol}.")
                        sold = True
                    elif elapsed <= RISK_RULES["tp2"][0] and profit_pct >= RISK_RULES["tp2"][1]:
                        self.log(f"✅ TP2 hit ({profit_pct:.2f}%). Selling {symbol}.")
                        sold = True
                    elif elapsed <= RISK_RULES["tp3"][0] and profit_pct >= RISK_RULES["tp3"][1]:
                        self.log(f"✅ TP3 hit ({profit_pct:.2f}%). Selling {symbol}.")
                        sold = True

                    # ---- Max hold exit ----
                    elif elapsed >= RISK_RULES["max_hold"]:
                        self.log(f"⌛ Max hold time reached. Selling {symbol}.")
                        sold = True

                    if sold:
                        acc["position"] = "closed"
                        acc["sell_price"] = current_price
                        profit_pct = (current_price - price) / price * 100
                        self.update_account(acc)

                        exit_time = time.time()
                        hold_seconds = int(exit_time - acc["entry_time"])

                        # ✅ Save trade with unique ID
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

                        self.log(
                            f"Sold {symbol} at {current_price:.4f} | PnL: {profit_pct:.2f}% | Held {hold_seconds}s"
                        )
                        self.get_balance(session, acc)
                        break

                    time.sleep(5)

                # Rest before next trade
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
