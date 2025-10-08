"""
Superb Crypto Bot — robust version with $5 minimum trade enforcement
and clean Bybit API handling.

Key updates:
 - Real Bybit trading (test_on_testnet=False)
 - $5 min trade enforcement to prevent Bybit API rejections
 - TP1/TP2/TP3 logic: 10m / 8m / 5m with elapsed time tracking
 - Stop loss -3%, forced exit after 23 minutes
 - Recursion-free design and clear logging
"""

from __future__ import annotations
import json, os, queue, threading, time, uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from pybit.unified_trading import HTTP

# -------------------- CONFIG --------------------
ALLOWED_COINS = [
    "ADAUSDT","XRPUSDT","TRXUSDT","DOGEUSDT","CHZUSDT","VETUSDT","BTTUSDT","HOTUSDT","XLMUSDT","ZILUSDT",
    "IOTAUSDT","SCUSDT","DENTUSDT","KEYUSDT","WINUSDT","CVCUSDT","MTLUSDT","CELRUSDT","FUNUSDT","STMXUSDT",
    "REEFUSDT","ANKRUSDT","ONEUSDT","OGNUSDT","CTSIUSDT","DGBUSDT","CKBUSDT","ARPAUSDT","MBLUSDT","TROYUSDT",
    "PERLUSDT","DOCKUSDT","RENUSDT","COTIUSDT","MDTUSDT","OXTUSDT","PHAUSDT","BANDUSDT","GTOUSDT","LOOMUSDT",
    "PONDUSDT","FETUSDT","SYSUSDT","TLMUSDT","NKNUSDT","LINAUSDT","ORNUSDT","COSUSDT","FLMUSDT","ALICEUSDT"
]

RISK_RULES = {
    "stop_loss": -3.0,  # -3%
    "tp1": (600, 7.0),  # 10 min → 7%
    "tp2": (1080, 4.0), # next 8 min → 4%
    "tp3": (1380, 1.0), # last 5 min → 1%
    "max_hold": 1380,   # force exit at 23 min
    "exit_on_any_positive": False
}

SCORE_SETTINGS = {
    "momentum_scale": 1.0,
    "rsi_oversold_threshold": 35,
    "rsi_overbought_threshold": 65,
    "rsi_oversold_bonus_multiplier": 1.0,
    "momentum_entry_threshold_pct": 0.1,
    "max_price_allowed": 1.2
}

TRADE_SETTINGS = {
    "trade_allocation_pct": 0.01,
    "min_trade_amount": 5.0,     # enforce Bybit minimum
    "use_market_order": True,
    "test_on_testnet": False,    # REAL trading
    "scan_interval": 10,
    "debug_raw_responses": False
}

ACCOUNTS_FILE = "accounts.json"
TRADES_FILE = "trades.json"

# -------------------- BOT CONTROLLER --------------------
class BotController:
    def __init__(self, log_queue: Optional[queue.Queue] = None):
        self.log_queue = log_queue
        self._running = False
        self._stop_event = threading.Event()
        self._file_lock = threading.Lock()
        self._threads: List[threading.Thread] = []

        for path in (ACCOUNTS_FILE, TRADES_FILE):
            if not os.path.exists(path):
                with open(path, "w") as f:
                    json.dump([], f)

    # -------- logging --------
    def log(self, msg: str):
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line)
        if self.log_queue:
            try: self.log_queue.put(line, block=False)
            except Exception: pass

    # -------- file helpers --------
    def load_accounts(self) -> List[Dict[str, Any]]:
        try:
            with open(ACCOUNTS_FILE, "r") as f: return json.load(f)
        except: return []

    def save_accounts(self, accs: List[Dict[str, Any]]):
        with self._file_lock:
            with open(ACCOUNTS_FILE, "w") as f: json.dump(accs, f, indent=2)

    def add_trade(self, trade: Dict[str, Any]):
        with self._file_lock:
            try:
                trades = json.load(open(TRADES_FILE))
            except: trades = []
            trades.append(trade)
            json.dump(trades, open(TRADES_FILE, "w"), indent=2)

    # -------- start/stop --------
    def start(self):
        if self._running:
            self.log("⚠️ Bot already running.")
            return
        self._stop_event.clear()
        self._running = True
        t = threading.Thread(target=self.run, daemon=True)
        self._threads.append(t)
        t.start()
        self.log("✅ Bot started")

    def stop(self):
        self._stop_event.set()
        self._running = False
        self.log("🛑 Bot stopped")

    def is_running(self) -> bool:
        return self._running

    # -------- API helpers --------
    def validate_account(self, acc: Dict[str, Any]) -> Tuple[bool, Optional[float], str]:
        try:
            session = HTTP(
                testnet=TRADE_SETTINGS["test_on_testnet"],
                api_key=acc["key"],
                api_secret=acc["secret"]
            )
            balance = None
            for method in ["get_wallet_balance", "get_balance"]:
                try:
                    resp = session.get_wallet_balance(accountType="UNIFIED")
                    balance = float(resp["result"]["list"][0]["totalEquity"])
                    break
                except Exception:
                    continue
            if balance is None:
                raise ValueError("Unable to fetch balance.")
            return True, balance, ""
        except Exception as e:
            return False, None, str(e)

    # -------- trade engine --------
    def run(self):
        while not self._stop_event.is_set():
            accounts = self.load_accounts()
            if not accounts:
                self.log("⚠️ No accounts loaded. Waiting...")
                time.sleep(TRADE_SETTINGS["scan_interval"])
                continue

            for acc in accounts:
                ok, balance, err = self.validate_account(acc)
                if not ok:
                    self.log(f"❌ Account {acc['name']} invalid: {err}")
                    acc["last_error"] = err
                    self.save_accounts(accounts)
                    continue

                acc["last_balance"] = balance
                self.save_accounts(accounts)

                symbol = self.pick_symbol()
                if not symbol:
                    self.log("⚠️ No valid symbol found this round.")
                    continue

                self.trade_cycle(acc, symbol)
            time.sleep(TRADE_SETTINGS["scan_interval"])

    def pick_symbol(self) -> Optional[str]:
        try:
            return ALLOWED_COINS[int(time.time()) % len(ALLOWED_COINS)]
        except Exception:
            return None

    def trade_cycle(self, acc: Dict[str, Any], symbol: str):
        try:
            allocation = acc["last_balance"] * TRADE_SETTINGS["trade_allocation_pct"]
            if allocation < TRADE_SETTINGS["min_trade_amount"]:
                self.log(f"⚠️ Skipping {symbol} for {acc['name']}: below $5 minimum (${allocation:.2f}).")
                return

            session = HTTP(
                testnet=TRADE_SETTINGS["test_on_testnet"],
                api_key=acc["key"],
                api_secret=acc["secret"]
            )

            price = self.get_last_price(session, symbol)
            if not price: return

            qty = round(allocation / price, 2)
            order = session.place_order(
                category="linear",
                symbol=symbol,
                side="Buy",
                orderType="Market",
                qty=qty
            )
            self.log(f"🟢 Buy {symbol} @ {price} qty={qty}")

            trade = {
                "id": str(uuid.uuid4())[:8],
                "symbol": symbol,
                "buy_price": price,
                "start_time": time.time(),
                "status": "open",
                "elapsed": 0,
                "profit_pct": 0
            }

            while not self._stop_event.is_set():
                now_price = self.get_last_price(session, symbol)
                if not now_price:
                    time.sleep(5)
                    continue

                elapsed = time.time() - trade["start_time"]
                profit = ((now_price - price) / price) * 100
                trade["elapsed"] = round(elapsed, 1)
                trade["profit_pct"] = round(profit, 2)

                # --- STOP LOSS ---
                if profit <= RISK_RULES["stop_loss"]:
                    self.exit_trade(session, symbol, qty, now_price, trade, "Stop loss hit")
                    break

                # --- TAKE PROFITS ---
                if elapsed <= RISK_RULES["tp1"][0] and profit >= RISK_RULES["tp1"][1]:
                    self.exit_trade(session, symbol, qty, now_price, trade,
                                    f"TP1 hit after {elapsed//60:.0f}m {elapsed%60:.0f}s (+{profit:.2f}%)")
                    break
                elif elapsed <= RISK_RULES["tp2"][0] and profit >= RISK_RULES["tp2"][1]:
                    self.exit_trade(session, symbol, qty, now_price, trade,
                                    f"TP2 hit after {elapsed//60:.0f}m {elapsed%60:.0f}s (+{profit:.2f}%)")
                    break
                elif elapsed <= RISK_RULES["tp3"][0] and profit >= RISK_RULES["tp3"][1]:
                    self.exit_trade(session, symbol, qty, now_price, trade,
                                    f"TP3 hit after {elapsed//60:.0f}m {elapsed%60:.0f}s (+{profit:.2f}%)")
                    break
                elif elapsed >= RISK_RULES["max_hold"]:
                    self.exit_trade(session, symbol, qty, now_price, trade, "Max hold time reached")
                    break

                time.sleep(5)

        except Exception as e:
            self.log(f"❌ Trade error for {acc['name']}: {e}")

    def get_last_price(self, session, symbol: str) -> Optional[float]:
        try:
            data = session.get_tickers(category="linear", symbol=symbol)
            return float(data["result"]["list"][0]["lastPrice"])
        except Exception:
            return None

    def exit_trade(self, session, symbol: str, qty: float, price: float, trade: Dict[str, Any], reason: str):
        try:
            session.place_order(category="linear", symbol=symbol, side="Sell", orderType="Market", qty=qty)
            self.log(f"🔴 Exit {symbol} @ {price} | {reason}")
        except Exception as e:
            self.log(f"⚠️ Exit failed for {symbol}: {e}")
        finally:
            trade["status"] = "closed"
            trade["end_time"] = time.time()
            trade["close_price"] = price
            trade["reason"] = reason
            self.add_trade(trade)

# -------------------- MAIN --------------------
if __name__ == "__main__":
    q = queue.Queue()
    bot = BotController(log_queue=q)
    bot.start()
    while True:
        try: print(q.get(timeout=1))
        except queue.Empty: pass
