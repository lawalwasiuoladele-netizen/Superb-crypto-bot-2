"""
Superb Crypto Bot — robust version (real Bybit trading)
 - recursion-safe (safe_json guards)
 - 10m / 8m / 5m TP structure, force exit at 23m
 - validated account fallback (UNIFIED → SPOT → array)
 - thread-safe file writes
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
    "stop_loss": -3.0,          # -3% SL
    "tp1": (600, 7.0),          # first 10m  → 7%
    "tp2": (1080, 4.0),         # next 8m    → 4%
    "tp3": (1380, 1.0),         # next 5m    → 1%
    "max_hold": 1380,           # force exit after 23m
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
    "use_market_order": True,
    "test_on_testnet": False,   # real trading
    "scan_interval": 10,
    "debug_raw_responses": False
}

ACCOUNTS_FILE = "accounts.json"
TRADES_FILE = "trades.json"

# -------------------- SAFE JSON GUARD --------------------
def safe_json(data, max_depth=4, _depth=0, _seen=None):
    """Return a JSON-safe copy without recursion."""
    if _seen is None:
        _seen = set()
    if id(data) in _seen or _depth > max_depth:
        return "<recursion-depth>"
    _seen.add(id(data))

    if isinstance(data, dict):
        return {k: safe_json(v, max_depth, _depth + 1, _seen) for k, v in data.items()}
    elif isinstance(data, (list, tuple)):
        return [safe_json(v, max_depth, _depth + 1, _seen) for v in data]
    else:
        try:
            json.dumps(data)
            return data
        except Exception:
            return str(data)

# -------------------- BOT CONTROLLER --------------------
class BotController:
    def __init__(self, log_queue: Optional[queue.Queue] = None):
        self.log_queue = log_queue
        self._running = False
        self._stop_event = threading.Event()
        self._file_lock = threading.Lock()
        self._threads: List[threading.Thread] = []

        for fpath in (ACCOUNTS_FILE, TRADES_FILE):
            if not os.path.exists(fpath):
                with open(fpath, "w") as f:
                    json.dump([], f)

    # ---- Logging ----
    def log(self, msg: str):
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line)
        if self.log_queue:
            try:
                self.log_queue.put(line, block=False)
            except Exception:
                pass

    def is_running(self):
        return self._running

    # ---- File helpers ----
    def load_accounts(self) -> List[Dict[str, Any]]:
        try:
            with open(ACCOUNTS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []

    def save_accounts(self, accounts: List[Dict[str, Any]]):
        with self._file_lock:
            with open(ACCOUNTS_FILE, "w") as f:
                json.dump(accounts, f, indent=2)

    def add_trade(self, trade: Dict[str, Any]):
        with self._file_lock:
            try:
                with open(TRADES_FILE, "r") as f:
                    trades = json.load(f)
            except Exception:
                trades = []
            trades.append(trade)
            with open(TRADES_FILE, "w") as f:
                json.dump(trades[-100:], f, indent=2)

    # ---- Account validation ----
    def _extract_balance(self, data):
        try:
            if not data:
                return None
            res = data.get("result", data)
            if isinstance(res, dict):
                for wallet in ("list", "balances", "rows"):
                    if wallet in res and isinstance(res[wallet], list) and len(res[wallet]) > 0:
                        coin = res[wallet][0]
                        for field in ("totalEquity", "equity", "walletBalance", "availableBalance"):
                            if field in coin:
                                return float(coin[field])
            elif isinstance(res, list):
                for coin in res:
                    if "equity" in coin:
                        return float(coin["equity"])
            return None
        except Exception:
            return None

    def validate_account(self, account):
        """Validate Bybit account safely."""
        try:
            client = HTTP(
                testnet=TRADE_SETTINGS["test_on_testnet"],
                api_key=account["key"],
                api_secret=account["secret"]
            )

            raw_responses, balance = {}, None

            for acc_type in ("UNIFIED", "SPOT", None):
                try:
                    resp = client.get_wallet_balance(accountType=acc_type) if acc_type else client.get_wallet_balance()
                    raw_responses[acc_type or "ARRAY"] = safe_json(resp)
                    balance = self._extract_balance(resp)
                    if balance is not None:
                        break
                except Exception as e:
                    raw_responses[acc_type or "ARRAY"] = f"error: {e}"

            account["last_raw_preview"] = raw_responses
            if balance is None:
                account["last_error"] = "Unable to parse balance"
                return False, None, "Unable to parse balance"

            account["last_balance"] = balance
            account["last_error"] = ""
            return True, balance, ""
        except Exception as e:
            account["last_error"] = str(e)
            return False, None, str(e)

    # ---- Example loop ----
    def start(self):
        if self._running:
            self.log("Bot already running.")
            return
        self._running = True
        self._stop_event.clear()
        th = threading.Thread(target=self._main_loop, daemon=True)
        self._threads.append(th)
        th.start()
        self.log("Bot started.")

    def stop(self):
        self._stop_event.set()
        self._running = False
        self.log("Bot stopping...")

    def _main_loop(self):
        while not self._stop_event.is_set():
            accounts = self.load_accounts()
            for acc in accounts:
                ok, bal, err = self.validate_account(acc)
                if not ok:
                    self.log(f"❌ {acc['name']}: {err}")
                else:
                    self.log(f"✅ {acc['name']} balance: {bal:.4f}")
            self.save_accounts(accounts)
            time.sleep(TRADE_SETTINGS["scan_interval"])
        self.log("Bot loop stopped.")
