"""
bot.py — Robust Multi-account Bybit Spot Trading Controller

- Trades ALL accounts together from a single scanning loop.
- Uses pybit.unified_trading.HTTP for authenticated actions (orders, wallet).
- Uses requests -> Bybit V5 public endpoints for market data (tickers, kline) as fallback to avoid pybit RecursionError.
- Safe logging via queue.Queue for SSE + console.
- Persist accounts/trades to accounts.json and trades.json with RLock protection.

USAGE:
 - Put your MAINNET API keys into accounts.json or add them via your dashboard (/add_account).
 - Ensure TRADE_SETTINGS["test_on_testnet"] = False if your keys are mainnet.
 - Start the Flask server (server.py) which uses this bot controller.
"""

import json
import os
import threading
import time
import uuid
import queue
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
from pybit.unified_trading import HTTP

# ---------------------- CONFIG ----------------------
ACCOUNTS_FILE = "accounts.json"
TRADES_FILE = "trades.json"

# default allowed coins (adjust as needed)
ALLOWED_COINS = [
    "ADAUSDT","XRPUSDT","TRXUSDT","DOGEUSDT","CHZUSDT","VETUSDT","BTTUSDT","HOTUSDT","XLMUSDT","ZILUSDT",
    "IOTAUSDT","SCUSDT","DENTUSDT","KEYUSDT","WINUSDT","CVCUSDT","MTLUSDT","CELRUSDT","FUNUSDT","STMXUSDT",
    "REEFUSDT","ANKRUSDT","ONEUSDT","OGNUSDT","CTSIUSDT","DGBUSDT","CKBUSDT","ARPAUSDT","MBLUSDT","TROYUSDT",
    "PERLUSDT","DOCKUSDT","RENUSDT","COTIUSDT","MDTUSDT","OXTUSDT","PHAUSDT","BANDUSDT","GTOUSDT","LOOMUSDT",
    "PONDUSDT","FETUSDT","SYSUSDT","TLMUSDT","NKNUSDT","LINAUSDT","ORNUSDT","COSUSDT","FLMUSDT","ALICEUSDT"
]

RISK_RULES = {
    "stop_loss": -3.0,    # -3% stop loss
    "tp1": (600, 7.0),
    "tp2": (1020, 4.0),
    "tp3": (1200, 1.0),
    "max_hold": 1320
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
    # For production with your MAINNET keys set to False
    "test_on_testnet": False,
    "trade_allocation_pct": 0.01,
    "use_market_order": True,
    "scan_interval": 10,  # seconds between scans
    "symbol_failure_cooldown": 30  # seconds to skip a symbol after repeated failures
}

# ---------------------- BotController ----------------------
class BotController:
    def __init__(self, log_queue: Optional[queue.Queue] = None):
        # Use queue.Queue for inter-thread SSE logging
        self.log_queue: queue.Queue = log_queue or queue.Queue()
        self._running = False
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._main_thread: Optional[threading.Thread] = None

        # session cache per account keytail (not persisted)
        self._sessions: Dict[str, HTTP] = {}
        # recent symbol failure map -> timestamp of last failure
        self._symbol_failures: Dict[str, float] = {}
        # ensure files exist
        if not os.path.exists(ACCOUNTS_FILE):
            with open(ACCOUNTS_FILE, "w") as f:
                json.dump([], f)
        if not os.path.exists(TRADES_FILE):
            with open(TRADES_FILE, "w") as f:
                json.dump([], f)

    # ---------- Logging ----------
    def _enqueue_log(self, text: str):
        try:
            if not isinstance(text, str):
                text = str(text)
            # single-line for SSE
            line = f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] {text.replace(chr(10),' ')}"
            print(line, flush=True)
            try:
                self.log_queue.put_nowait(line)
            except Exception:
                pass
        except Exception:
            try:
                print("[_enqueue_log] logging failed", flush=True)
            except Exception:
                pass

    def log(self, msg: Any):
        """Public logging wrapper - safe conversion then enqueue."""
        try:
            if isinstance(msg, str):
                self._enqueue_log(msg)
            else:
                self._enqueue_log(self._safe_to_string(msg))
        except Exception:
            self._enqueue_log("log conversion failed")

    def _safe_to_string(self, obj: Any, max_len: int = 800) -> str:
        try:
            if obj is None:
                return "None"
            if isinstance(obj, (str, int, float, bool)):
                s = str(obj)
                return s if len(s) <= max_len else s[:max_len] + "..."
            if isinstance(obj, dict):
                sample = {}
                for k, v in list(obj.items())[:20]:
                    if isinstance(v, (str, int, float, bool)) or v is None:
                        sample[k] = v
                    elif isinstance(v, (list, tuple)):
                        sample[k] = f"<{type(v).__name__} len={len(v)}>"
                    else:
                        sample[k] = f"<{type(v).__name__}>"
                try:
                    return json.dumps(sample)[:max_len]
                except Exception:
                    return f"<dict keys={len(obj)}>"
            if isinstance(obj, (list, tuple)):
                return f"<{type(obj).__name__} len={len(obj)}>"
            return f"<{type(obj).__name__}>"
        except Exception:
            return f"<{type(obj).__name__}?>"

    # ---------- File helpers ----------
    def load_accounts(self) -> List[Dict[str, Any]]:
        try:
            with self._lock:
                with open(ACCOUNTS_FILE, "r") as f:
                    return json.load(f)
        except Exception:
            return []

    def save_accounts(self, accounts: List[Dict[str, Any]]):
        try:
            with self._lock:
                with open(ACCOUNTS_FILE, "w") as f:
                    json.dump(accounts, f, indent=2)
        except Exception as e:
            self._enqueue_log(f"save_accounts failed: {str(e)[:200]}")

    def add_trade(self, trade: Dict[str, Any]):
        try:
            with self._lock:
                trades = []
                try:
                    with open(TRADES_FILE, "r") as f:
                        trades = json.load(f)
                except Exception:
                    trades = []
                trades.append(trade)
                with open(TRADES_FILE, "w") as f:
                    json.dump(trades, f, indent=2)
        except Exception as e:
            self._enqueue_log(f"add_trade failed: {str(e)[:200]}")

    # ---------- Bybit session helper ----------
    def _make_session(self, acc: Dict[str, Any]) -> HTTP:
        """
        Return a pybit HTTP session for the account.
        Do not call other bot methods here (keeps this non-recursive).
        """
        return HTTP(
            testnet=TRADE_SETTINGS.get("test_on_testnet", False),
            api_key=acc["key"],
            api_secret=acc["secret"]
        )

    # validate account using authenticated call - returns (ok, balance, error_msg)
    def validate_account(self, acc: Dict[str, Any], retries: int = 1) -> Tuple[bool, float, Optional[str]]:
        try:
            session = self._make_session(acc)
            last_err = None
            for _ in range(max(1, retries)):
                try:
                    resp = session.get_wallet_balance(accountType="UNIFIED")
                    lst = resp.get("result", {}).get("list", [])
                    if lst:
                        total = float(lst[0].get("totalEquity", 0) or 0.0)
                        return True, total, None
                    return True, 0.0, None
                except Exception as e:
                    last_err = str(e)
                    time.sleep(0.2)
            return False, 0.0, last_err
        except Exception as e:
            return False, 0.0, str(e)[:400]

    # ---------- Market data helpers (pybit first, fallback to REST v5) ----------
    def _rest_base(self) -> str:
        return "https://api-testnet.bybit.com" if TRADE_SETTINGS.get("test_on_testnet", False) else "https://api.bybit.com"

    def _symbol_on_cooldown(self, symbol: str) -> bool:
        ts = self._symbol_failures.get(symbol)
        if ts is None:
            return False
        return (time.time() - ts) < TRADE_SETTINGS.get("symbol_failure_cooldown", 30)

    def _record_symbol_failure(self, symbol: str):
        self._symbol_failures[symbol] = time.time()

    def get_ticker_last(self, session: HTTP, symbol: str) -> Optional[float]:
        """
        Try to use pybit first. If pybit raises or hits RecursionError, fallback to public V5 endpoint.
        """
        # avoid repeated attempts if symbol failed recently
        if self._symbol_on_cooldown(symbol):
            return None

        try:
            data = session.get_tickers(category="spot", symbol=symbol)
            last = float(data["result"]["list"][0]["lastPrice"])
            return last
        except RecursionError as re:
            self._enqueue_log(f"get_ticker_last RecursionError for {symbol}: {str(re)} - using REST fallback")
            self._record_symbol_failure(symbol)
        except Exception as e:
            # log minimal info and fallback
            self._enqueue_log(f"get_ticker_last pybit error for {symbol}: {str(e)[:200]} - fallback to REST")
            # record failure once to reduce spam
            self._record_symbol_failure(symbol)

        # Fallback to Bybit V5 public API
        try:
            base = self._rest_base()
            url = f"{base}/v5/market/tickers?category=spot&symbol={symbol}"
            resp = requests.get(url, timeout=6)
            resp.raise_for_status()
            j = resp.json()
            lst = j.get("result", {}).get("list", [])
            if not lst:
                return None
            first = lst[0]
            # typical v5: dict with "lastPrice"
            last = first.get("lastPrice") if isinstance(first, dict) else None
            return float(last) if last is not None else None
        except Exception as e2:
            self._enqueue_log(f"get_ticker_last fallback failed for {symbol}: {str(e2)[:180]}")
            self._record_symbol_failure(symbol)
            return None

    def get_klines(self, session: HTTP, symbol: str, interval: str = "1", limit: int = 50) -> List[Dict[str, Any]]:
        """
        Try pybit kline first; fallback to v5 /market/kline endpoint.
        Returns list of candles (oldest first) with keys: open, high, low, close, volume, ts
        """
        if self._symbol_on_cooldown(symbol):
            return []

        try:
            resp = session.get_kline(category="spot", symbol=symbol, interval=str(interval), limit=limit)
            candles = resp.get("result", {}).get("list", [])
            if not candles:
                return []
            return list(reversed(candles))
        except RecursionError as re:
            self._enqueue_log(f"get_klines RecursionError for {symbol}: {str(re)} - using REST fallback")
            self._record_symbol_failure(symbol)
        except Exception as e:
            self._enqueue_log(f"get_klines pybit error for {symbol}: {str(e)[:200]} - fallback to REST")
            self._record_symbol_failure(symbol)

        # fallback v5
        try:
            base = self._rest_base()
            url = f"{base}/v5/market/kline?category=spot&symbol={symbol}&interval={interval}&limit={limit}"
            resp = requests.get(url, timeout=8)
            resp.raise_for_status()
            j = resp.json()
            lst = j.get("result", {}).get("list", [])
            if not lst:
                return []
            candles = []
            for item in lst:
                if isinstance(item, dict):
                    c = {
                        "open": float(item.get("open", 0)),
                        "high": float(item.get("high", 0)),
                        "low": float(item.get("low", 0)),
                        "close": float(item.get("close", 0)),
                        "volume": float(item.get("volume", 0)),
                        "ts": item.get("start") or item.get("t")
                    }
                elif isinstance(item, (list, tuple)):
                    ts = int(item[0]) if len(item) > 0 else None
                    open_p = float(item[1]) if len(item) > 1 else 0.0
                    high_p = float(item[2]) if len(item) > 2 else 0.0
                    low_p = float(item[3]) if len(item) > 3 else 0.0
                    close_p = float(item[4]) if len(item) > 4 else 0.0
                    vol = float(item[5]) if len(item) > 5 else 0.0
                    c = {"open": open_p, "high": high_p, "low": low_p, "close": close_p, "volume": vol, "ts": ts}
                else:
                    continue
                candles.append(c)
            return list(reversed(candles))
        except Exception as e2:
            self._enqueue_log(f"get_klines fallback failed for {symbol}: {str(e2)[:200]}")
            self._record_symbol_failure(symbol)
            return []

    # ---------- Wallet / balance ----------
    def get_balance(self, session: HTTP, acc: Dict[str, Any], retries: int = 2) -> Optional[float]:
        last_err = None
        for _ in range(retries):
            try:
                resp = session.get_wallet_balance(accountType="UNIFIED")
                lst = resp.get("result", {}).get("list", [])
                if lst:
                    total = float(lst[0].get("totalEquity", 0) or 0.0)
                    acc["last_balance"] = total
                    # persist minimal info
                    try:
                        self._update_account(acc)
                    except Exception:
                        pass
                    return total
                acc["last_balance"] = 0.0
                try:
                    self._update_account(acc)
                except Exception:
                    pass
                return 0.0
            except Exception as e:
                last_err = str(e)
                self._enqueue_log(f"get_balance error for {acc.get('key','')[:6]}...: {last_err[:200]}")
                time.sleep(0.4)
        return None

    def _update_account(self, acc: Dict[str, Any]):
        """Shallow merge of account changes into accounts.json (no logging here)."""
        try:
            with self._lock:
                accounts = self.load_accounts()
                updated = False
                for a in accounts:
                    if a.get("key") == acc.get("key"):
                        for k, v in acc.items():
                            if not callable(v):
                                a[k] = v
                        updated = True
                        break
                if not updated:
                    accounts.append(acc)
                with open(ACCOUNTS_FILE, "w") as f:
                    json.dump(accounts, f, indent=2)
        except Exception as e:
            self._enqueue_log(f"_update_account error: {str(e)[:200]}")

    # ---------- Indicators & scoring ----------
    def compute_rsi(self, closes: List[float], period: int = 14) -> Optional[float]:
        if len(closes) < period + 1:
            return None
        gains = losses = 0.0
        for i in range(-period, 0):
            diff = closes[i] - closes[i - 1]
            if diff > 0:
                gains += diff
            else:
                losses += abs(diff)
        avg_gain = gains / period if gains != 0 else 1e-6
        avg_loss = losses / period if losses != 0 else 1e-6
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

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
                score += abs(50 - rsi) * 0.2
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
            if current_price is None or current_price > SCORE_SETTINGS["max_price_allowed"]:
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
            self._enqueue_log(f"Entry check {symbol}: RSI={rsi}, mom={round(momentum,3)}, vol_spike={vol_spike}, ma_cross={ma_cross}, price={current_price}, pass={ok}")
            return ok
        except Exception as e:
            self._enqueue_log(f"check_entry_condition error {symbol}: {str(e)[:200]}")
            return False

    # ---------- Order placement ----------
    def place_market_buy(self, session: HTTP, symbol: str, usdt_amount: float):
        price = self.get_ticker_last(session, symbol)
        if price is None or price == 0:
            raise RuntimeError("Invalid price for market buy.")
        qty = float(round(usdt_amount / price, 6))
        params = {
            "category": "spot",
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Market",
            "qty": str(qty)
        }
        if not TRADE_SETTINGS.get("use_market_order", True):
            params["orderType"] = "Limit"
            params["price"] = str(price)
        res = session.place_spot_order(**params)
        return {"price": price, "qty": qty, "resp_type": type(res).__name__}

    def place_market_sell(self, session: HTTP, symbol: str, qty: float):
        params = {
            "category": "spot",
            "symbol": symbol,
            "side": "Sell",
            "orderType": "Market",
            "qty": str(qty)
        }
        res = session.place_spot_order(**params)
        return {"resp_type": type(res).__name__}

    # ---------- Main loop (trades all accounts together) ----------
    def _ensure_session_for_account(self, acc: Dict[str, Any]) -> Optional[HTTP]:
        keytail = acc.get("key", "")[-6:]
        sess = self._sessions.get(keytail)
        if sess:
            return sess
        try:
            sess = self._make_session(acc)
            self._sessions[keytail] = sess
            return sess
        except Exception as e:
            self._enqueue_log(f"Session create failed for ****{keytail}: {str(e)[:200]}")
            return None

    def _main_loop(self):
        self._enqueue_log("Main scan loop started.")
        while not self._stop_event.is_set():
            accounts = self.load_accounts()

            # normalize accounts fields
            for acc in accounts:
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

            # 1) Monitor and exit positions for all accounts
            for acc in accounts:
                if acc.get("position") == "open" and acc.get("current_symbol"):
                    session = self._ensure_session_for_account(acc)
                    if not session:
                        continue
                    symbol = acc["current_symbol"]
                    price = self.get_ticker_last(session, symbol)
                    if price is None:
                        continue
                    acc["current_price"] = price
                    profit_pct = (price - acc.get("buy_price", 0.0)) / (acc.get("buy_price", 0.000001)) * 100
                    acc["current_pnl"] = profit_pct
                    acc["entry_time"] = acc.get("entry_time", acc.get("last_trade_start", time.time()))
                    try:
                        self._update_account(acc)
                    except Exception:
                        pass
                    should_exit, profit_pct, elapsed = self.check_exit_condition(acc, price)
                    # periodic logging
                    if int(elapsed) % 60 == 0:
                        self._enqueue_log(f"Account ****{acc.get('key')[-4:]} monitoring {symbol} elapsed={int(elapsed)}s profit={round(profit_pct,2)}%")
                    if should_exit:
                        self._enqueue_log(f"Exiting {symbol} for account ****{acc.get('key')[-4:]} profit={round(profit_pct,2)}% elapsed={int(elapsed)}s")
                        qty = acc.get("qty")
                        if qty is None and acc.get("allocated_usdt") and acc.get("buy_price"):
                            qty = float(round(acc.get("allocated_usdt") / acc.get("buy_price"), 6))
                        try:
                            if qty:
                                sell_resp = self.place_market_sell(session, symbol, qty)
                                self._enqueue_log(f"Sell resp type: {sell_resp.get('resp_type')}")
                            else:
                                self._enqueue_log("No qty known for sell, skipping sell call.")
                        except Exception as e:
                            self._enqueue_log(f"Sell order failed: {str(e)[:300]}")
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
                        # clear
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

            # 2) Scan market once, then attempt entry for each eligible account (trade accounts together)
            best_score = -9999.0
            best_symbol = None
            best_meta = None

            # scan allowed coins
            for symbol in ALLOWED_COINS:
                # skip if cooling down
                if self._symbol_on_cooldown(symbol):
                    continue
                # choose any session (use first account's session) - create a temporary session for public requests if needed
                accounts_list = accounts
                if not accounts_list:
                    break
                # Prefer a valid session; else create a dummy session from the first account to keep pybit attempts
                sample_acc = accounts_list[0]
                session = self._ensure_session_for_account(sample_acc) or self._make_session(sample_acc)
                try:
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
                except Exception as e:
                    # don't let one symbol kill the scan
                    self._enqueue_log(f"symbol scan error {symbol}: {str(e)[:200]}")
                    continue

            if best_symbol:
                self._enqueue_log(f"Best market-wide candidate: {best_symbol} score={round(best_score,3)}")
                candidate_ok = self.check_entry_condition(best_symbol, best_meta["closes"], best_meta["price"], best_meta["volumes"])
                if candidate_ok:
                    # place buys for each account that is 'closed'
                    for acc in accounts:
                        if acc.get("position") == "closed":
                            session = self._ensure_session_for_account(acc)
                            if not session:
                                continue
                            # ensure balance
                            equity = acc.get("last_balance", None)
                            if equity is None:
                                equity = self.get_balance(session, acc) or 0.0
                            allocated = equity * TRADE_SETTINGS.get("trade_allocation_pct", 0.01)
                            if allocated <= 0 or equity <= 0:
                                self._enqueue_log(f"Skipping buy for ****{acc.get('key')[-4:]} due to zero allocation or missing balance.")
                                continue
                            try:
                                self._enqueue_log(f"Placing buy for {best_symbol} on account ****{acc.get('key')[-4:]} using ${round(allocated,4)}")
                                buy_resp = self.place_market_buy(session, best_symbol, allocated)
                                buy_price = buy_resp.get("price", best_meta["price"])
                                qty = buy_resp.get("qty", None)
                                self._enqueue_log(f"Buy result: price={buy_price}, qty={qty}")
                                # set account state
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
                                # record entry
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
                                self._enqueue_log(f"Buy failed for account ****{acc.get('key')[-4:]}: {str(e)[:300]}")
                else:
                    self._enqueue_log(f"Candidate {best_symbol} failed entry checks.")
            else:
                self._enqueue_log("No suitable candidate found in this scan.")

            # sleep until next scan, early stop allowed
            interval = int(TRADE_SETTINGS.get("scan_interval", 10))
            for _ in range(interval):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

        self._enqueue_log("Main scan loop exiting.")

    # ---------- Control API (start/stop/status) ----------
    def start(self):
        if self._running:
            self._enqueue_log("Bot already running.")
            return
        self._stop_event.clear()
        self._running = True
        self._main_thread = threading.Thread(target=self._main_loop, daemon=True)
        self._main_thread.start()
        self._enqueue_log("Bot start requested (main loop launched).")

    def stop(self):
        if not self._running:
            self._enqueue_log("Bot not running.")
            return
        self._stop_event.set()
        self._running = False
        self._enqueue_log("Stop requested; waiting for main loop to exit...")

    def is_running(self) -> bool:
        return self._running

# If run directly for quick smoke test
if __name__ == "__main__":
    bc = BotController()
    bc.log("BotController ready. Edit TRADE_SETTINGS in file for testnet vs mainnet before running live.")
