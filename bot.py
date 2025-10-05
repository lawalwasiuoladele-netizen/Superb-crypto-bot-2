"""
LIVE SPOT TRADING BOT (Patched)

Key fixes:
 - Use queue.Queue for log_queue typing (fixes AttributeError).
 - Added validate_account(acc, retries=...) helper for server debug.
 - Hardened API parsing for common pybit response formats.
 - Defensive error handling and clearer logging.
 - No autostart at import.
"""

import json
import os
import threading
import time
import uuid
import math
import random
from datetime import datetime
from typing import List, Dict, Any, Optional
import queue

# pybit HTTP client
try:
    from pybit.unified_trading import HTTP
except Exception:
    HTTP = None  # server/debug will surface this error

# ---------------------- CONFIG ----------------------
ALLOWED_COINS = [
    # verify availability/liquidity on Bybit
    "ADAUSDT","XRPUSDT","TRXUSDT","DOGEUSDT","CHZUSDT","VETUSDT","BTTUSDT","HOTUSDT","XLMUSDT","ZILUSDT",
    "IOTAUSDT","SCUSDT","DENTUSDT","KEYUSDT","WINUSDT","CVCUSDT","MTLUSDT","CELRUSDT","FUNUSDT","STMXUSDT",
    "REEFUSDT","ANKRUSDT","ONEUSDT","OGNUSDT","CTSIUSDT","DGBUSDT","CKBUSDT","ARPAUSDT","MBLUSDT","TROYUSDT",
    "PERLUSDT","DOCKUSDT","RENUSDT","COTIUSDT","MDTUSDT","OXTUSDT","PHAUSDT","BANDUSDT","GTOUSDT","LOOMUSDT",
    "PONDUSDT","FETUSDT","SYSUSDT","TLMUSDT","NKNUSDT","LINAUSDT","ORNUSDT","COSUSDT","FLMUSDT","ALICEUSDT"
]

RISK_RULES = {
    "stop_loss": -3.0,    # -3% stop loss
    "tp1": (600, 7.0),    # first 10 minutes -> 7%
    "tp2": (1020, 4.0),   # up to 17 minutes -> 4%
    "tp3": (1200, 1.0),   # up to 20 minutes -> 1%
    "max_hold": 1320      # force exit at 22 minutes (1320s)
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
    "trade_allocation_pct": 0.01,   # fraction of account equity to allocate per trade
    "use_market_order": True,
    "test_on_testnet": True,        # Change to False when ready to run live
    "scan_interval": 10,            # seconds between scans when idle
}

ACCOUNTS_FILE = "accounts.json"
TRADES_FILE = "trades.json"


# ---------------------- BotController ----------------------
class BotController:
    def __init__(self, log_queue: Optional[queue.Queue] = None):
        """
        log_queue: optional queue.Queue where text log lines will be put
        """
        self.log_queue = log_queue
        self._running = False
        self._stop_event = threading.Event()
        self._file_lock = threading.Lock()
        self._threads: List[threading.Thread] = []

        # Ensure files exist
        if not os.path.exists(ACCOUNTS_FILE):
            with open(ACCOUNTS_FILE, "w") as f:
                json.dump([], f)
        if not os.path.exists(TRADES_FILE):
            with open(TRADES_FILE, "w") as f:
                json.dump([], f)

    # --------- Logging ----------
    def log(self, msg: str):
        line = f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        try:
            print(line, flush=True)
        except Exception:
            pass
        if self.log_queue:
            try:
                self.log_queue.put(line, block=False)
            except Exception:
                # never raise from logging
                pass

    # --------- File helpers ----------
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
            trades = []
            try:
                with open(TRADES_FILE, "r") as f:
                    trades = json.load(f)
            except Exception:
                trades = []
            trades.append(trade)
            with open(TRADES_FILE, "w") as f:
                json.dump(trades, f, indent=2)

    # --------- Bybit session ----------
    def _make_session(self, acc: Dict[str, Any]) -> Any:
        if HTTP is None:
            raise RuntimeError("pybit.unified_trading.HTTP unavailable (import failed).")
        testnet_flag = acc.get("test_on_testnet", TRADE_SETTINGS.get("test_on_testnet", True))
        return HTTP(testnet=bool(testnet_flag), api_key=acc.get("key"), api_secret=acc.get("secret"))

    def _parse_last_price_from_tickers(self, resp: Any, symbol: str) -> Optional[float]:
        """
        Handle a few common pybit response shapes. Return float or None.
        """
        try:
            if not resp:
                return None
            # expected shape: {"retCode":0,"retMsg":"OK","result":{"list":[{"symbol":"ADAUSDT","lastPrice":"0.XXX", ...}]}}
            r = resp.get("result")
            if isinstance(r, dict):
                lst = r.get("list") or r.get("tickers") or r.get("rows") or []
                if isinstance(lst, list) and lst:
                    first = lst[0]
                    price = first.get("lastPrice") or first.get("price") or first.get("close")
                    if price is None:
                        return None
                    return float(price)
            # fallback: resp["result"] might be a list directly
            if isinstance(resp.get("result"), list) and resp["result"]:
                price = resp["result"][0].get("lastPrice")
                if price:
                    return float(price)
        except Exception:
            return None
        return None

    def get_ticker_last(self, session: Any, symbol: str) -> Optional[float]:
        """
        Get last ticker price. Return None on error.
        Defensive: don't throw except; log and return None.
        """
        try:
            resp = session.get_tickers(category="spot", symbol=symbol)
            price = self._parse_last_price_from_tickers(resp, symbol)
            if price is None:
                # try alternate direct field
                try:
                    rr = resp.get("result") or {}
                    if isinstance(rr, dict) and "list" in rr and rr["list"]:
                        price = float(rr["list"][0].get("lastPrice", 0))
                except Exception:
                    pass
            return price
        except Exception as e:
            # very defensive: catch RecursionError separately but do not re-raise
            try:
                self.log(f"get_ticker_last error for {symbol}: {repr(e)}")
            except Exception:
                pass
            return None

    def get_klines(self, session: Any, symbol: str, interval: str = "1", limit: int = 50) -> List[Dict[str, Any]]:
        try:
            resp = session.get_kline(category="spot", symbol=symbol, interval=str(interval), limit=limit)
            candles = resp.get("result", {}).get("list", []) if isinstance(resp.get("result"), dict) else resp.get("result") or []
            if not candles:
                return []
            # ensure list of dicts and return oldest->newest order
            return list(reversed(candles)) if isinstance(candles, list) else []
        except Exception as e:
            self.log(f"get_klines error for {symbol}: {e}")
            return []

    def get_balance(self, session: Any, acc: Dict[str, Any], retries: int = 2) -> Optional[float]:
        """
        Return total equity/balance (float) or None. Try UNIFIED, then SPOT.
        """
        last_err = None
        for attempt in range(retries):
            try:
                # try unified first (modern unified wallet)
                try:
                    resp = session.get_wallet_balance(accountType="UNIFIED")
                    # parse a few shapes
                    result = resp.get("result")
                    if isinstance(result, dict) and "list" in result and result["list"]:
                        first = result["list"][0]
                        total = float(first.get("totalEquity") or first.get("equity") or first.get("total") or 0.0)
                        acc["last_balance"] = total
                        self._update_account(acc)
                        return total
                    elif isinstance(result, list) and result:
                        # list-of-wallets shape
                        first = result[0]
                        total = float(first.get("totalEquity") or first.get("equity") or 0.0)
                        acc["last_balance"] = total
                        self._update_account(acc)
                        return total
                except Exception as e_unified:
                    last_err = f"UNIFIED error: {e_unified}"
                    # try SPOT next
                    try:
                        resp2 = session.get_wallet_balance(accountType="SPOT")
                        result2 = resp2.get("result") or {}
                        # for SPOT the exact field may vary
                        # attempt to sum balances if detailed
                        total = 0.0
                        if isinstance(result2, dict):
                            parts = result2.get("list") or result2.get("balances") or []
                            if isinstance(parts, list) and parts:
                                for p in parts:
                                    try:
                                        total += float(p.get("equity") or p.get("available") or p.get("balance") or 0.0)
                                    except Exception:
                                        continue
                            else:
                                total = float(result2.get("totalEquity", 0.0) or result2.get("total", 0.0))
                        elif isinstance(result2, list):
                            for p in result2:
                                try:
                                    total += float(p.get("equity") or p.get("available") or p.get("balance") or 0.0)
                                except Exception:
                                    continue
                        acc["last_balance"] = total
                        self._update_account(acc)
                        return total
                    except Exception as e_spot:
                        last_err = f"SPOT error: {e_spot}"
                        self.log(f"get_balance try {attempt+1} failed: {last_err}")
                        time.sleep(0.5)
                        continue
            except Exception as e:
                last_err = str(e)
                self.log(f"get_balance unexpected error: {last_err}")
                time.sleep(0.5)
        # after retries
        acc["last_balance"] = acc.get("last_balance", 0.0)
        acc["last_error"] = last_err
        self._update_account(acc)
        return None

    def _update_account(self, acc: Dict[str, Any]):
        """
        Write back small changes to the accounts file safely.
        """
        try:
            accounts = self.load_accounts()
            updated = False
            for a in accounts:
                if a.get("key") == acc.get("key"):
                    a.update(acc)
                    updated = True
                    break
            if not updated:
                accounts.append(acc)
            self.save_accounts(accounts)
        except Exception as e:
            self.log(f"_update_account error: {e}")

    # --------- Indicators ----------
    def compute_rsi(self, closes: List[float], period: int = 14) -> Optional[float]:
        if len(closes) < period + 1:
            return None
        gains = 0.0
        losses = 0.0
        # calculate simple gains/losses over period
        for i in range(-period, 0):
            diff = closes[i] - closes[i - 1]
            if diff > 0:
                gains += diff
            else:
                losses += abs(diff)
        avg_gain = gains / period if gains != 0 else 0.000001
        avg_loss = losses / period if losses != 0 else 0.000001
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def compute_momentum_pct(self, closes: List[float], lookback: int = 5) -> float:
        if len(closes) < lookback + 1:
            return 0.0
        recent_avg = sum(closes[-(lookback + 1):-1]) / lookback
        if recent_avg == 0:
            return 0.0
        return (closes[-1] - recent_avg) / recent_avg * 100

    def compute_sma(self, prices: List[float], period: int) -> Optional[float]:
        if len(prices) < period:
            return None
        return sum(prices[-period:]) / period

    # --------- Entry logic ----------
    def check_entry_condition(self, symbol: str, closes: List[float], current_price: float, volumes: Optional[List[float]] = None) -> bool:
        try:
            if current_price is None:
                return False
            if current_price > SCORE_SETTINGS["max_price_allowed"]:
                return False

            rsi = self.compute_rsi(closes, 14)
            momentum = self.compute_momentum_pct(closes, 5)

            rsi_signal = (rsi is not None) and (rsi < SCORE_SETTINGS["rsi_oversold_threshold"] or rsi > SCORE_SETTINGS["rsi_overbought_threshold"])

            vol_spike = True
            if volumes and len(volumes) >= 11:
                avg_vol = sum(volumes[-11:-1]) / 10
                last_vol = volumes[-1]
                vol_spike = last_vol > 1.1 * avg_vol

            short_ma = self.compute_sma(closes, 9)
            long_ma = self.compute_sma(closes, 21) or self.compute_sma(closes, len(closes))
            ma_cross = (short_ma is not None and long_ma is not None and short_ma > long_ma)

            momentum_ok = momentum >= SCORE_SETTINGS["momentum_entry_threshold_pct"]

            ok = rsi_signal and vol_spike and ma_cross and momentum_ok
            self.log(f"Entry check {symbol}: RSI={rsi}, mom={momentum:.3f}, vol_spike={vol_spike}, ma_cross={ma_cross}, price={current_price}, pass={ok}")
            return ok
        except Exception as e:
            self.log(f"⚠️ Error in check_entry_condition {symbol}: {e}")
            return False

    # Scoring helper
    def score_symbol(self, closes: List[float], volumes: List[float]) -> float:
        try:
            if not closes:
                return -999
            score = 0.0
            rsi = self.compute_rsi(closes, 14)
            momentum = self.compute_momentum_pct(closes, 5)
            short_ma = self.compute_sma(closes, 9)
            long_ma = self.compute_sma(closes, 21) or self.compute_sma(closes, 50)
            ma_cross = (short_ma is not None and long_ma is not None and short_ma > long_ma)

            if rsi is not None:
                rsi_dist = abs(50 - rsi)
                score += rsi_dist * 0.2
                if rsi < SCORE_SETTINGS["rsi_oversold_threshold"]:
                    score += SCORE_SETTINGS["rsi_oversold_bonus_multiplier"] * 2.0

            score += max(0.0, momentum) * (SCORE_SETTINGS["momentum_scale"] * 0.5)

            if ma_cross:
                score += 2.0

            if volumes and len(volumes) >= 11:
                avg_vol = sum(volumes[-11:-1]) / 10
                last_vol = volumes[-1]
                if last_vol > 1.1 * avg_vol:
                    score += 1.5

            return score
        except Exception:
            return -999

    # --------- Exit logic ----------
    def check_exit_condition(self, acc: Dict[str, Any], current_price: float):
        try:
            entry_price = acc.get("buy_price", 0.0) or 0.0
            entry_time = acc.get("entry_time", time.time())
            if entry_price <= 0:
                self.log(f"⚠️ Invalid entry price for {acc.get('current_symbol')}, skipping exit check.")
                return False, 0.0, 0

            profit_pct = (current_price - entry_price) / (entry_price if entry_price != 0 else 0.0001) * 100
            elapsed = time.time() - entry_time

            if profit_pct <= RISK_RULES["stop_loss"]:
                return True, profit_pct, elapsed
            if elapsed <= RISK_RULES["tp1"][0] and profit_pct >= RISK_RULES["tp1"][1]:
                return True, profit_pct, elapsed
            elif elapsed <= RISK_RULES["tp2"][0] and profit_pct >= RISK_RULES["tp2"][1]:
                return True, profit_pct, elapsed
            elif elapsed <= RISK_RULES["tp3"][0] and profit_pct >= RISK_RULES["tp3"][1]:
                return True, profit_pct, elapsed
            if elapsed >= RISK_RULES["max_hold"]:
                return True, profit_pct, elapsed

            return False, profit_pct, elapsed
        except Exception as e:
            self.log(f"⚠️ Error in check_exit_condition {acc.get('current_symbol')}: {e}")
            return False, 0.0, 0

    # --------- Trading helpers ----------
    def place_market_buy(self, session: Any, symbol: str, usdt_amount: float):
        price = self.get_ticker_last(session, symbol)
        if price is None or price == 0:
            raise RuntimeError("Invalid price for market buy.")
        qty = usdt_amount / price
        # round qty to 6 decimals (adjust if your exchange requires different precision)
        qty = float(round(qty, 6))
        params = {
            "category": "spot",
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Market",
            "qty": str(qty)
        }
        if not TRADE_SETTINGS["use_market_order"]:
            params["orderType"] = "Limit"
            params["price"] = str(price)
        res = session.place_spot_order(**params)
        return {"price": price, "qty": qty, "resp": res}

    def place_market_sell(self, session: Any, symbol: str, qty: float):
        params = {
            "category": "spot",
            "symbol": symbol,
            "side": "Sell",
            "orderType": "Market",
            "qty": str(qty)
        }
        res = session.place_spot_order(**params)
        return res

    # --------- Account runner (main logic for each account) ----------
    def run_account(self, acc: Dict[str, Any]):
        """
        Thread target that continuously scans allowed coins and executes trades for given account.
        """
        # Ensure defaults
        acc.setdefault("position", "closed")
        acc.setdefault("monitoring", False)
        acc.setdefault("current_symbol", None)
        acc.setdefault("buy_price", 0.0)
        acc.setdefault("entry_time", 0)
        acc.setdefault("last_trade_start", None)
        acc.setdefault("last_balance", 0.0)
        acc.setdefault("qty", None)
        acc.setdefault("allocated_usdt", 0.0)
        acc.setdefault("current_price", None)
        acc.setdefault("current_pnl", None)

        try:
            session = self._make_session(acc)
        except Exception as e:
            self.log(f"Failed creating session for account ****{acc.get('key','')[-4:]}: {e}")
            return

        last_balance_fetch = 0
        scan_interval = TRADE_SETTINGS.get("scan_interval", 10)

        while not self._stop_event.is_set():
            try:
                # update balance every 60s
                if time.time() - last_balance_fetch > 60:
                    self.get_balance(session, acc)
                    last_balance_fetch = time.time()

                # If position open -> monitor
                if acc.get("position")
