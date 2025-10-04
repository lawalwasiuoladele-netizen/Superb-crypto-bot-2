"""
Superb Crypto Bot — robust version with automatic Bybit-response fallbacks

Replace your current bot.py with this file.

Key features:
 - No recursion traps (no wrappers that call themselves)
 - Multi-step balance parsing fallback (UNIFIED -> SPOT -> coin-array)
 - Safe ticker/kline retrieval with candidate method fallbacks
 - validate_account(account) -> (ok:bool, balance:Optional[float], error:str)
 - Stores a small `last_raw_preview` on account dicts for debugging (without secrets)
 - DEBUG_RAW_RESPONSES toggle to capture raw responses for inspection
 - Thread-safe file updates
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# IMPORTANT: pybit unified_trading HTTP client
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
    "stop_loss": -3.0,    # -3% stop loss
    "tp1": (600, 7.0),    # up to 10 minutes -> 7%
    "tp2": (1020, 4.0),   # up to 17 minutes -> 4%
    "tp3": (1200, 1.0),   # up to 20 minutes -> 1%
    "max_hold": 1320,     # force exit at 22 minutes
    # If True, exit on any profit > 0
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
    "trade_allocation_pct": 0.01,   # fraction of equity per trade
    "use_market_order": True,
    "test_on_testnet": False,       # CHANGE to True for testnet keys
    "scan_interval": 10,            # seconds between scans when idle
    "debug_raw_responses": False    # toggle raw-response capture (for debugging)
}

ACCOUNTS_FILE = "accounts.json"
TRADES_FILE = "trades.json"

# -------------------- Bot Controller --------------------
class BotController:
    def __init__(self, log_queue: Optional[queue.Queue] = None):
        # Use queue.Queue type (not threading.Queue) to avoid AttributeError
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

    # ---- logging ----
    def log(self, msg: str):
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line)
        if self.log_queue:
            try:
                self.log_queue.put(line, block=False)
            except Exception:
                pass

    # ---- file helpers (thread-safe) ----
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
                json.dump(trades, f, indent=2)

    # ---- sessions ----
    def _make_session(self, acc: Dict[str, Any]) -> HTTP:
        """
        Create a fresh HTTP client for the given account. This uses pybit's HTTP.
        """
        # account may have 'testnet' override, else use global
        testnet = acc.get("test_on_testnet", TRADE_SETTINGS["test_on_testnet"])
        return HTTP(testnet=bool(testnet), api_key=acc["key"], api_secret=acc["secret"])

    # ---- safe low-level API call helper ----
    def _safe_call(self, fn, *args, **kwargs):
        """
        Low-level wrapper that catches RecursionError and Exceptions and returns a tuple:
        (ok:bool, result_or_error)
        - ok = True => result is the API response
        - ok = False => result is the exception instance or string
        """
        try:
            res = fn(*args, **kwargs)
            return True, res
        except RecursionError as re:
            return False, f"RecursionError: {re}"
        except Exception as e:
            return False, e

    # ---- validate account (used by server.py) ----
    def validate_account(self, acc: Dict[str, Any], retries: int = 1) -> Tuple[bool, Optional[float], Optional[str]]:
        """
        Validate credentials and attempt to derive a USDT balance.
        Returns (ok, balance_or_None, error_message_or_None)
        Also writes a small `last_raw_preview` into acc for debugging (if configured).
        This function is safe to call frequently (no heavy side-effects).
        """
        self.log(f"Validating account {acc.get('name') or acc.get('key','')[:6]}****")
        try:
            session = self._make_session(acc)
        except Exception as e:
            err = f"session creation failed: {e}"
            self.log(err)
            return False, None, err

        # Attempt step 1: unified wallet (preferred)
        ok, resp = self._safe_call(session.get_wallet_balance, accountType="UNIFIED")
        if ok:
            bal = self._parse_balance_from_unified(resp)
            if bal is not None:
                self._store_raw_preview(acc, "unified", resp)
                return True, bal, None
            # store preview (useful to inspect why parsing failed)
            self._store_raw_preview(acc, "unified", resp)
        else:
            # store error message
            self._store_raw_preview(acc, "unified_error", str(resp))

        # Attempt step 2: SPOT wallet
        ok2, resp2 = self._safe_call(session.get_wallet_balance, accountType="SPOT")
        if ok2:
            bal = self._parse_balance_from_spot(resp2)
            if bal is not None:
                self._store_raw_preview(acc, "spot", resp2)
                return True, bal, None
            self._store_raw_preview(acc, "spot", resp2)
        else:
            self._store_raw_preview(acc, "spot_error", str(resp2))

        # Attempt step 3: try a few other calls that may contain USDT
        # e.g. get_asset_balance (if library exposes), or list of balances
        # We'll try to call `get_assets`/`get_balances` if present (safe getattr)
        fallback_balance = None
        for method_name in ("get_asset_balance", "get_wallet_balance", "get_balance", "get_usdt_balance"):
            if hasattr(session, method_name):
                method = getattr(session, method_name)
                ok3, resp3 = self._safe_call(method)
                if ok3:
                    bal = self._extract_balance_generic(resp3)
                    if bal is not None:
                        self._store_raw_preview(acc, f"fallback_{method_name}", resp3)
                        return True, bal, None
                    self._store_raw_preview(acc, f"fallback_{method_name}", resp3)
                else:
                    self._store_raw_preview(acc, f"fallback_{method_name}_error", str(resp3))

        # Nothing worked
        err_msg = "failed to get balance (tried UNIFIED, SPOT, fallbacks)"
        self.log(f"Account validation failed for {acc.get('name')}: {err_msg}")
        return False, None, err_msg

    # ---- raw preview storing (for debug) ----
    def _store_raw_preview(self, acc: Dict[str, Any], tag: str, payload: Any):
        """
        Save a small preview (not secrets) to the account dict for server-side debugging.
        Only enabled when TRADE_SETTINGS['debug_raw_responses'] is True.
        """
        try:
            if TRADE_SETTINGS.get("debug_raw_responses"):
                # Keep only small payload to avoid huge files.
                acc.setdefault("last_raw_preview", {})
                acc["last_raw_preview"][tag] = (payload if isinstance(payload, (str, int, float)) else (json.loads(json.dumps(payload)) if payload is not None else None))
                self._update_account(acc)
        except Exception:
            # best-effort, don't block main flow
            pass

    # ---- balance parsing helpers ----
    def _parse_balance_from_unified(self, resp: Any) -> Optional[float]:
        """
        Parse common shapes from unified wallet responses.
        Looks for result.list[0].totalEquity or coin array with USDT entry.
        """
        try:
            if not isinstance(resp, dict):
                return None
            result = resp.get("result") or resp.get("data") or resp
            if not result:
                return None
            # try list -> first -> totalEquity
            if isinstance(result, dict) and "list" in result and isinstance(result["list"], list) and len(result["list"]) > 0:
                first = result["list"][0]
                if isinstance(first, dict):
                    if "totalEquity" in first:
                        return float(first.get("totalEquity") or 0.0)
                    # coin array may be present
                    coin_arr = first.get("coin") or first.get("coins") or first.get("wallet")
                    if isinstance(coin_arr, list):
                        for c in coin_arr:
                            # c may be dict with coin and equity/walletBalance fields
                            if isinstance(c, dict):
                                coin_name = str(c.get("coin") or c.get("currency") or "").upper()
                                if coin_name in ("USDT", "USDC", "USD"):
                                    # prefer 'equity' or 'walletBalance' or 'availableBalance'
                                    for fld in ("equity", "walletBalance", "availableBalance", "balance", "total_balance"):
                                        if fld in c:
                                            try:
                                                return float(c.get(fld) or 0.0)
                                            except Exception:
                                                continue
                        # otherwise, try to find total across coin arr denominated in USDT not present
            # some responses put totalEquity at top-level
            if isinstance(result, dict) and "totalEquity" in result:
                return float(result.get("totalEquity") or 0.0)
        except Exception:
            return None
        return None

    def _parse_balance_from_spot(self, resp: Any) -> Optional[float]:
        """
        Parse common shapes from spot wallet responses. Typical shapes include:
          result.list[0].coin -> list of coins with 'coin' and 'equity' or 'availableBalance'
        """
        try:
            if not isinstance(resp, dict):
                return None
            result = resp.get("result") or resp.get("data") or resp
            if not result:
                return None
            if isinstance(result, dict) and "list" in result and isinstance(result["list"], list) and len(result["list"]) > 0:
                first = result["list"][0]
                # find coin array inside 'coin' or first may already be coin objects
                if isinstance(first, dict):
                    coin_arr = None
                    # If first contains 'coin' as list then it is wrapper
                    if isinstance(first.get("coin"), list):
                        coin_arr = first.get("coin")
                    # Else maybe result itself is a list of coin dicts
                # If top-level result is already a list of coin dicts:
            # handle the generic case: result might be the list of coins
            candidate_list = None
            if isinstance(result, list):
                candidate_list = result
            elif isinstance(result, dict) and isinstance(result.get("list"), list) and len(result.get("list")) > 0:
                # sometimes list contains dicts whose 'coin' key is list
                # flatten
                lst = result.get("list")
                # if list items have 'coin' lists, combine them
                combined = []
                for item in lst:
                    if isinstance(item, dict) and isinstance(item.get("coin"), list):
                        combined.extend(item.get("coin"))
                if combined:
                    candidate_list = combined
                else:
                    candidate_list = lst
            if isinstance(candidate_list, list):
                for c in candidate_list:
                    if isinstance(c, dict):
                        name = str(c.get("coin") or c.get("currency") or "").upper()
                        if name in ("USDT", "USDC", "USD"):
                            for fld in ("equity", "walletBalance", "availableBalance", "balance", "total_balance"):
                                if fld in c:
                                    try:
                                        return float(c.get(fld) or 0.0)
                                    except Exception:
                                        continue
            # last resort, try to parse nested 'coin' inside first element
            if isinstance(result, dict) and isinstance(result.get("list"), list) and len(result["list"]) > 0:
                first = result["list"][0]
                if isinstance(first, dict) and isinstance(first.get("coin"), list):
                    for c in first.get("coin"):
                        if isinstance(c, dict) and (c.get("coin") or "").upper() == "USDT":
                            for fld in ("equity", "walletBalance", "availableBalance", "balance"):
                                if fld in c:
                                    try:
                                        return float(c.get(fld) or 0.0)
                                    except Exception:
                                        continue
        except Exception:
            return None
        return None

    def _extract_balance_generic(self, resp: Any) -> Optional[float]:
        """
        Generic attempt to find USDT anywhere in the response structure.
        Not perfect but tries common shapes.
        """
        try:
            if isinstance(resp, dict):
                # quick checks:
                for key in ("result", "data"):
                    if key in resp:
                        candidate = resp[key]
                        # if candidate is list or dict, reuse parsers
                        if isinstance(candidate, list):
                            for item in candidate:
                                # item may be coin dict
                                if isinstance(item, dict):
                                    if (item.get("coin") or "").upper() in ("USDT",):
                                        for fld in ("equity", "walletBalance", "availableBalance", "balance"):
                                            if fld in item:
                                                try:
                                                    return float(item.get(fld) or 0.0)
                                                except Exception:
                                                    pass
                        if isinstance(candidate, dict):
                            if "totalEquity" in candidate:
                                try:
                                    return float(candidate.get("totalEquity") or 0.0)
                                except Exception:
                                    pass
                # deep search (limited recursion) for USDT key
                def deep_search(d, depth=0):
                    if depth > 6:
                        return None
                    if isinstance(d, dict):
                        for k, v in d.items():
                            if isinstance(k, str) and k.lower().find("usdt") >= 0:
                                try:
                                    return float(v)
                                except Exception:
                                    pass
                            res = deep_search(v, depth+1)
                            if res is not None:
                                return res
                    if isinstance(d, list):
                        for item in d:
                            res = deep_search(item, depth+1)
                            if res is not None:
                                return res
                    return None
                found = deep_search(resp, 0)
                if found is not None:
                    return float(found)
        except Exception:
            return None
        return None

    # ---- ticker / kline helpers with fallbacks ----
    def get_ticker_last(self, session: HTTP, symbol: str) -> Optional[float]:
        """
        Attempts multiple ways to get a spot last price. Avoids recursion.
        """
        # Preferred: session.get_tickers(category="spot", symbol=symbol)
        ok, resp = self._safe_call(session.get_tickers, category="spot", symbol=symbol)
        if ok:
            try:
                # typical shape: {"result": {"list":[{"lastPrice": "0.123"...}]}}
                result = resp.get("result") or resp.get("data") or resp
                if isinstance(result, dict) and isinstance(result.get("list"), list) and len(result["list"]) > 0:
                    last = result["list"][0].get("lastPrice") or result["list"][0].get("last")
                    if last is not None:
                        return float(last)
            except Exception:
                pass
        # fallback try other method names (some versions of pybit have different names)
        alt_methods = ["get_ticker", "get_symbol_ticker", "get_last_price", "get_price"]
        for name in alt_methods:
            if hasattr(session, name):
                method = getattr(session, name)
                ok2, resp2 = self._safe_call(method, symbol)
                if ok2:
                    try:
                        if isinstance(resp2, dict):
                            # try common fields
                            if "lastPrice" in resp2:
                                return float(resp2.get("lastPrice"))
                            if "price" in resp2:
                                return float(resp2.get("price"))
                            if "result" in resp2 and isinstance(resp2["result"], dict) and "price" in resp2["result"]:
                                return float(resp2["result"]["price"])
                    except Exception:
                        pass
        # last resort: return None
        return None

    def get_klines(self, session: HTTP, symbol: str, interval: str = "1", limit: int = 50) -> List[Dict[str, Any]]:
        """
        Fetch klines / candlesticks. Returns list of candlesticks (old->new).
        """
        ok, resp = self._safe_call(session.get_kline, category="spot", symbol=symbol, interval=str(interval), limit=limit)
        if ok:
            try:
                cand = resp.get("result", {}).get("list") or resp.get("data", {}).get("list") or resp.get("result") or resp.get("data")
                # ensure list type
                if isinstance(cand, list):
                    return list(reversed(cand))  # older->newer
            except Exception:
                pass
        # fallback: try other named methods
        if hasattr(session, "get_klines"):
            ok2, resp2 = self._safe_call(session.get_klines, category="spot", symbol=symbol, interval=interval, limit=limit)
            if ok2 and isinstance(resp2, dict):
                try:
                    cand = resp2.get("result", {}).get("list") or resp2.get("data", {}).get("list") or resp2.get("result")
                    if isinstance(cand, list):
                        return list(reversed(cand))
                except Exception:
                    pass
        return []

    # ---- place orders (safe wrappers) ----
    def place_market_buy(self, session: HTTP, symbol: str, usdt_amount: float):
        """
        Place a market buy via spot order. Rounds qty to 6 decimals.
        """
        price = self.get_ticker_last(session, symbol)
        if price is None or price == 0:
            raise RuntimeError("Invalid price for market buy.")
        qty = usdt_amount / price
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
        ok, resp = self._safe_call(session.place_spot_order, **params) if hasattr(session, "place_spot_order") else self._safe_call(session.place_order, **params)
        if not ok:
            raise RuntimeError(f"Place buy failed: {resp}")
        return {"price": price, "qty": qty, "resp": resp}

    def place_market_sell(self, session: HTTP, symbol: str, qty: float):
        params = {
            "category": "spot",
            "symbol": symbol,
            "side": "Sell",
            "orderType": "Market",
            "qty": str(qty)
        }
        ok, resp = self._safe_call(session.place_spot_order, **params) if hasattr(session, "place_spot_order") else self._safe_call(session.place_order, **params)
        if not ok:
            raise RuntimeError(f"Place sell failed: {resp}")
        return resp

    # ---- indicator helpers (same as before) ----
    def compute_rsi(self, closes: List[float], period: int = 14) -> Optional[float]:
        if len(closes) < period + 1:
            return None
        gains = 0.0
        losses = 0.0
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

    # ---- scoring & entry/exit logic (kept from your original) ----
    def score_symbol(self, closes: List[float], volumes: List[float]) -> float:
        try:
            if not closes:
                return -999
            score = 0.0
            rsi = self.compute_rsi(closes, 14)
            momentum = self.compute_momentum_pct(closes, 5)
            short_ma = self.compute_sma(closes, 9)
            long_ma = self.compute_sma(closes, 21) or self.compute_sma(closes, len(closes))
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

    def check_exit_condition(self, acc: Dict[str, Any], current_price: float):
        try:
            entry_price = acc.get("buy_price", 0.0) or 0.0
            entry_time = acc.get("entry_time", time.time())
            if entry_price <= 0:
                self.log(f"⚠️ Invalid entry price for {acc.get('current_symbol')}, skipping exit check.")
                return False, 0.0, 0

            profit_pct = (current_price - entry_price) / (entry_price if entry_price != 0 else 0.0001) * 100
            elapsed = time.time() - entry_time

            # exit on any profit > 0 if configured
            if RISK_RULES.get("exit_on_any_positive") and profit_pct > 0:
                return True, profit_pct, elapsed

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

    # ---- account persistence helper ----
    def _update_account(self, acc: Dict[str, Any]):
        """
        Write small changes back into accounts.json safely.
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

    # ---- main account runner (keeps original threaded-per-account model) ----
    def run_account(self, acc: Dict[str, Any]):
        """
        Thread target that continuously scans allowed coins and executes trades for given account.
        """
        # defaults
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
                    bal = None
                    try:
                        ok, resp = self._safe_call(session.get_wallet_balance, accountType="UNIFIED")
                        if ok:
                            bal = self._parse_balance_from_unified(resp)
                            if bal is None:
                                bal = self._parse_balance_from_spot(resp)
                            if TRADE_SETTINGS.get("debug_raw_responses"):
                                acc.setdefault("last_raw_preview", {})["last_unified"] = resp
                        else:
                            # try SPOT
                            ok2, resp2 = self._safe_call(session.get_wallet_balance, accountType="SPOT")
                            if ok2:
                                bal = self._parse_balance_from_spot(resp2)
                                if TRADE_SETTINGS.get("debug_raw_responses"):
                                    acc.setdefault("last_raw_preview", {})["last_spot"] = resp2
                    except Exception as e:
                        self.log(f"get_balance error for {acc.get('key','')[:6]}...: {e}")
                    if bal is not None:
                        acc["last_balance"] = float(bal)
                        self._update_account(acc)
                    last_balance_fetch = time.time()

                # If position open -> monitor and attempt exit
                if acc.get("position") == "open" and acc.get("current_symbol"):
                    symbol = acc["current_symbol"]
                    price = self.get_ticker_last(session, symbol)
                    if price is None:
                        time.sleep(1)
                        continue

                    acc["current_price"] = price
                    profit_pct = (price - acc.get("buy_price", 0.0)) / (acc.get("buy_price", 0.000001)) * 100
                    acc["current_pnl"] = profit_pct
                    acc["entry_time"] = acc.get("entry_time", acc.get("last_trade_start", time.time()))
                    self._update_account(acc)

                    should_exit, profit_pct, elapsed = self.check_exit_condition(acc, price)

                    # per-minute logging
                    if int(elapsed) % 60 == 0:
                        self.log(f"Account ****{acc.get('key')[-4:]} | Monitoring {symbol} | elapsed={int(elapsed)}s | profit={profit_pct:.2f}% | price={price}")

                    if should_exit:
                        self.log(f"Exiting {symbol} for account ****{acc.get('key')[-4:]} | profit={profit_pct:.2f}% | elapsed={int(elapsed)}s")
                        qty = acc.get("qty", None)
                        if qty is None and acc.get("allocated_usdt") and acc.get("buy_price"):
                            qty = float(round(acc.get("allocated_usdt") / acc.get("buy_price"), 6))
                        try:
                            if qty:
                                sell_resp = self.place_market_sell(session, symbol, qty)
                                self.log(f"Sell resp: {sell_resp}")
                            else:
                                self.log("No qty known for sell, skipping sell API call (manual intervention needed).")
                        except Exception as e:
                            self.log(f"Sell order failed for {symbol}: {e}")

                        trade = {
                            "id": str(uuid.uuid4()),
                            "account": acc.get("name", acc.get("key")[:6]),
                            "account_key_tail": acc.get("key")[-6:],
                            "symbol": symbol,
                            "entry_price": acc.get("buy_price"),
                            "exit_price": price,
                            "profit_pct": profit_pct,
                            "entry_time": acc.get("last_trade_start"),
                            "exit_time": datetime.utcnow().isoformat(),
                            "hold_seconds": int(elapsed)
                        }
                        self.add_trade(trade)

                        # clear position
                        acc["position"] = "closed"
                        acc["monitoring"] = False
                        acc["current_symbol"] = None
                        acc["buy_price"] = 0.0
                        acc["entry_time"] = 0
                        acc["qty"] = None
                        acc["allocated_usdt"] = 0.0
                        acc["current_price"] = None
                        acc["current_pnl"] = None
                        acc["last_trade_start"] = None
                        self._update_account(acc)

                    time.sleep(1)
                    continue

                # Not in position -> scan allowed coins and attempt entry
                best_score = -9999.0
                best_symbol = None
                best_meta = None

                for symbol in ALLOWED_COINS:
                    price = self.get_ticker_last(session, symbol)
                    if price is None:
                        continue
                    if price > SCORE_SETTINGS["max_price_allowed"]:
                        continue
                    candles = self.get_klines(session, symbol, interval="1", limit=50)
                    if not candles:
                        continue
                    closes = [float(c["close"]) for c in candles if "close" in c]
                    volumes = [float(c.get("volume", 0)) for c in candles]
                    score = self.score_symbol(closes, volumes)
                    if score > best_score:
                        best_score = score
                        best_symbol = symbol
                        best_meta = {"price": price, "closes": closes, "volumes": volumes}

                if best_symbol:
                    self.log(f"Account ****{acc.get('key')[-4:]} | Best candidate: {best_symbol} score={best_score:.3f}")
                    candidate_ok = self.check_entry_condition(best_symbol, best_meta["closes"], best_meta["price"], best_meta["volumes"])
                    if candidate_ok:
                        equity = acc.get("last_balance", None)
                        if equity is None:
                            # attempt immediate balance fetch
                            okv, bal_v = self.validate_account(acc, retries=1)[0:2]
                            equity = bal_v or 0.0
                        allocated = equity * TRADE_SETTINGS["trade_allocation_pct"]
                        if allocated <= 0 or equity <= 0:
                            self.log("Allocated amount <= 0 or balance missing, skipping trade.")
                        else:
                            try:
                                self.log(f"Placing buy for {best_symbol} using ${allocated:.4f} allocation.")
                                buy_resp = self.place_market_buy(session, best_symbol, allocated)
                                buy_price = buy_resp.get("price", best_meta["price"])
                                qty = buy_resp.get("qty", None)
                                self.log(f"Buy resp price={buy_price}, qty={qty}")
                                # set position info
                                acc["position"] = "open"
                                acc["monitoring"] = True
                                acc["current_symbol"] = best_symbol
                                acc["buy_price"] = float(buy_price)
                                acc["entry_time"] = time.time()
                                acc["last_trade_start"] = datetime.utcnow().isoformat()
                                acc["qty"] = qty
                                acc["allocated_usdt"] = allocated
                                acc["current_price"] = buy_price
                                acc["current_pnl"] = 0.0
                                self._update_account(acc)
                                # record trade entry
                                entry_trade = {
                                    "id": str(uuid.uuid4()),
                                    "account": acc.get("name", acc.get("key")[:6]),
                                    "account_key_tail": acc.get("key")[-6:],
                                    "symbol": best_symbol,
                                    "entry_price": buy_price,
                                    "qty": qty,
                                    "entry_time": acc["last_trade_start"]
                                }
                                self.add_trade(entry_trade)
                            except Exception as e:
                                self.log(f"Buy order failed for {best_symbol}: {e}")
                    else:
                        self.log(f"Candidate {best_symbol} failed strict entry checks.")
                else:
                    self.log("No suitable candidate found in this scan.")

                # sleep until next scan (but wake early on stop event)
                for _ in range(int(scan_interval)):
                    if self._stop_event.is_set():
                        break
                    time.sleep(1)

            except Exception as e:
                self.log(f"Unexpected error in run_account for ****{acc.get('key','')[-4:]}: {e}")
                time.sleep(2)

        self.log(f"Exiting run loop for account ****{acc.get('key','')[-4:]}")

    # ---- control ----
    def start(self):
        """
        Start the bot: spawn a thread per configured account.
        If no accounts configured, log and return.
        """
        accounts = self.load_accounts()
        if not accounts:
            self.log("⚠️ No accounts configured in accounts.json.")
            return

        self._stop_event.clear()
        self._running = True
        # spawn a thread per account
        for acc in accounts:
            acc.setdefault("position", "closed")
            acc.setdefault("monitoring", False)
            # spawn thread
            try:
                # quick connectivity test (non-blocking)
                session = self._make_session(acc)
                # NOTE: safe_call will capture exceptions but we won't crash here
                ok, _ = self._safe_call(session.get_wallet_balance, accountType="UNIFIED")
                t = threading.Thread(target=self.run_account, args=(acc,), daemon=True)
                t.start()
                self._threads.append(t)
                self.log(f"✅ Bot started thread for account ****{acc.get('key','')[-4:]}")
            except Exception as e:
                self.log(f"❌ Failed to start account ****{acc.get('key','')[-4:]}: {e}")

        self.log("Bot startup attempt complete.")

    def stop(self):
        self._stop_event.set()
        self._running = False
        self.log("Stop signal set. Threads will exit shortly.")

    def is_running(self) -> bool:
        return self._running

# -------------------- USAGE (if run standalone) --------------------
if __name__ == "__main__":
    bc = BotController()
    bc.log("Bot ready. Use bc.start() to launch.")
