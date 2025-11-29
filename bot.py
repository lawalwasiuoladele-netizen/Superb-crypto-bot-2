"""
Rewritten and cleaned strategy controller for Superb Crypto Bot.

Key features:
- 1-minute timeframe (interval = '1')
- Max hold time = 30 minutes (1800 seconds)
- Wilder RSI, smoothed momentum, EMA trend filter
- Safer Fibonacci pivots (exclude last candle from pivot calc)
- Take profit on Fibonacci extensions (1.272 -> 1.618 -> 2.618)
- Stop loss = 1% below swing low (ensures SL below entry)
- Order response validation, retry wrapper, file locking
- Dry-run support
- Daily trade limit (30 trades/24h cycle)
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Callable

# Exchange client used in original repo
from pybit.unified_trading import HTTP

# -------------------- CONFIG --------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Use config.json if present for override; otherwise defaults below
def load_config() -> Dict[str, Any]:
    cfg_path = os.path.join(BASE_DIR, "config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

CONFIG = load_config()

# Timeframe: '1' means 1 minute
TIMEFRAME = CONFIG.get("timeframe", "1")

# Keep allowed coins as before
ALLOWED_COINS = CONFIG.get("allowed_coins", [
    "ADAUSDT", "XRPUSDT", "TRXUSDT", "DOGEUSDT", "CHZUSDT", "VETUSDT", "BTTUSDT", "HOTUSDT",
    "XLMUSDT", "ZILUSDT", "IOTAUSDT", "SCUSDT", "DENTUSDT", "KEYUSDT", "WINUSDT", "CVCUSDT",
    "MTLUSDT", "CELRUSDT", "FUNUSDT", "STMXUSDT", "REEFUSDT", "ANKRUSDT", "ONEUSDT", "OGNUSDT",
    "CTSIUSDT", "DGBUSDT", "CKBUSDT", "ARPAUSDT", "MBLUSDT", "TROYUSDT", "PERLUSDT", "DOCKUSDT",
    "RENUSDT", "COTIUSDT", "MDTUSDT", "OXTUSDT", "PHAUSDT", "BANDUSDT", "GTOUSDT", "LOOMUSDT",
    "PONDUSDT", "FETUSDT", "SYSUSDT", "TLMUSDT", "NKNUSDT", "LINAUSDT", "ORNUSDT", "COSUSDT",
    "FLMUSDT", "ALICEUSDT",
])

# Risk rules
RISK_RULES = {
    # stop_loss percent is not used directly for SL price; we use swing low * (1 - 0.01)
    "stop_loss_pct": CONFIG.get("stopLossPct", 1.0),  # percent
    "max_hold": int(CONFIG.get("maxHoldSeconds", 30 * 60)),  # 30 minutes default (1800 s)
}

# Score and strategy settings
SCORE_SETTINGS = {
    "rsi_period": int(CONFIG.get("rsiPeriod", 14)),
    "rsi_oversold_threshold": float(CONFIG.get("rsiOversold", 35.0)),
    "momentum_entry_threshold_pct": float(CONFIG.get("momentumEntryThreshold", 0.1)),
    "momentum_strong_pct": float(CONFIG.get("momentumStrong", 0.5)),
    "momentum_very_strong_pct": float(CONFIG.get("momentumVeryStrong", 1.5)),
    "fib_lookback": int(CONFIG.get("fibLookback", 50)),
    "score_weights": {
        "rsi": int(CONFIG.get("scoreWeightRsi", 1)),
        "momentum": int(CONFIG.get("scoreWeightMomentum", 1)),
        "ema": int(CONFIG.get("scoreWeightEma", 1)),
        "candle": int(CONFIG.get("scoreWeightCandle", 1)),
        "fib_zone": int(CONFIG.get("scoreWeightFibZone", 1)),
    }
}

TRADE_SETTINGS = {
    "trade_allocation_pct": CONFIG.get("tradeAllocation", 100),
    "min_trade_amount": float(CONFIG.get("minTradeAmount", 5.0)),
    "use_market_order": CONFIG.get("useMarketOrder", True),
    "test_on_testnet": CONFIG.get("testOnTestnet", False),
    "scan_interval": int(CONFIG.get("scanInterval", 10)),
    "debug_raw_responses": CONFIG.get("debugRawResponses", False),
    "dry_run": CONFIG.get("dryRun", False),
    "max_trades_per_day": int(CONFIG.get("maxTradesPerDay", 30)), # New limit
}

# Adjusted paths to match existing project structure (app/ instead of accounts/)
ACCOUNTS_FILE = os.path.join(BASE_DIR, "var/tmp/accounts.json")
TRADES_FILE = os.path.join(BASE_DIR, "var/tmp/trades.json")

# Ensure app directory exists
os.makedirs(os.path.join(BASE_DIR, "var/tmp"), exist_ok=True)

# Ensure files exist
for f in (ACCOUNTS_FILE, TRADES_FILE):
    if not os.path.exists(f):
        with open(f, "w") as fh:
            fh.write("[]")

# -------------------- Utilities --------------------
def now_ts() -> int:
    return int(time.time())

def now_iso() -> str:
    return datetime.utcnow().isoformat()

def fmt_elapsed(sec: int) -> str:
    m = sec // 60
    s = sec % 60
    return f"{m}m {s}s"

def safe_json(obj: Any, max_depth: int = 4, _depth: int = 0, _seen: Optional[set] = None) -> Any:
    if _seen is None:
        _seen = set()
    try:
        if id(obj) in _seen or _depth > max_depth:
            return "<recursion>"
        _seen.add(id(obj))
    except Exception:
        pass
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            try:
                key = str(k)
            except Exception:
                key = "<key>"
            out[key] = safe_json(v, max_depth, _depth + 1, _seen)
        return out
    if isinstance(obj, (list, tuple)):
        return [safe_json(v, max_depth, _depth + 1, _seen) for v in obj]
    try:
        json.dumps(obj)
        return obj
    except Exception:
        return str(obj)

# -------------------- Indicator helpers --------------------
def calc_ema(values: List[float], period: int) -> Optional[float]:
    if not values or len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for p in values[period:]:
        ema = p * k + ema * (1 - k)
    return ema

def wilder_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if not closes or len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    # initial window
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses += -d
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    # Wilder smoothing
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        gain = d if d > 0 else 0.0
        loss = -d if d < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def smoothed_momentum_pct(closes: List[float], lookback: int = 5, smooth_span: int = 3) -> float:
    if not closes or len(closes) < lookback + 1:
        return 0.0
    raw = []
    for i in range(lookback, len(closes)):
        denom = closes[i - lookback] if closes[i - lookback] != 0 else 1.0
        raw.append(((closes[i] - closes[i - lookback]) / denom) * 100.0)
    if not raw:
        return 0.0
    alpha = 2.0 / (smooth_span + 1.0)
    ema = raw[0]
    for v in raw[1:]:
        ema = v * alpha + ema * (1.0 - alpha)
    return float(ema)

def calc_fib_levels(high: float, low: float) -> Dict[str, float]:
    diff = high - low
    return {
        '0.0': high,
        '0.236': high - 0.236 * diff,
        '0.382': high - 0.382 * diff,
        '0.5': high - 0.5 * diff,
        '0.618': high - 0.618 * diff,
        '0.786': high - 0.786 * diff,
        '1.0': low,
        '1.272_ext': high + 1.272 * diff,
        '1.618_ext': high + 1.618 * diff,
        '2.0_ext': high + 2.0 * diff,
        '2.618_ext': high + 2.618 * diff,
    }

def price_in_zone(price: float, levels: Dict[str, float], lo_key: str = '0.382', hi_key: str = '0.618') -> bool:
    try:
        lo = levels[lo_key]; hi = levels[hi_key]
        return min(lo, hi) <= price <= max(lo, hi)
    except Exception:
        return False

def detect_bullish_candle(candles: List[Dict[str, float]]) -> bool:
    if not candles:
        return False
    last = candles[-1]
    if len(candles) >= 2:
        prev = candles[-2]
        if prev['close'] < prev['open'] and last['close'] > last['open'] and last['close'] > prev['open'] and last['open'] < prev['close']:
            return True
    body = abs(last['close'] - last['open'])
    lower_wick = (last['open'] - last['low']) if last['open'] > last['close'] else (last['close'] - last['low'])
    upper_wick = last['high'] - max(last['open'], last['close'])
    if body > 0 and lower_wick / body >= 2 and upper_wick / body <= 0.5:
        return True
    return False

def pivot_fib_levels_from_confirmed_window(highs: List[float], lows: List[float], lookback: int = 50) -> Dict[str, float]:
    """
    Safer pivot: use lookback window but exclude most recent candle.
    """
    if not highs or not lows:
        return {}
    if len(highs) < lookback:
        lookback = len(highs)
    # exclude last candle
    slice_highs = highs[-lookback - 1:-1] if len(highs) >= lookback + 1 else highs[:-1] if len(highs) > 1 else highs
    slice_lows = lows[-lookback - 1:-1] if len(lows) >= lookback + 1 else lows[:-1] if len(lows) > 1 else lows
    if not slice_highs or not slice_lows:
        slice_highs = highs[-lookback:]
        slice_lows = lows[-lookback:]
    swing_high = max(slice_highs)
    swing_low = min(slice_lows)
    return calc_fib_levels(swing_high, swing_low)

# -------------------- Bot Controller (clean rewrite) --------------------
class BotController:
    def __init__(self, log_queue: Optional[threading.Queue] = None):
        self.log_queue = log_queue
        self._running = False
        self._stop = threading.Event()
        self._file_lock = threading.Lock()
        self._threads: List[threading.Thread] = []

        # Daily limit tracking
        self.trades_today = 0
        self.day_start_time = time.time()
        self.MAX_TRADES_DAILY = TRADE_SETTINGS.get("max_trades_per_day", 30)

        # ensure account files exist
        for path, default in ((ACCOUNTS_FILE, []), (TRADES_FILE, [])):
            if not os.path.exists(path):
                try:
                    with open(path, "w") as fh:
                        json.dump(default, fh, indent=2)
                except Exception as e:
                    print(f"Error creating {path}: {e}")

    # ------------------ Logging ------------------
    def log(self, msg: str):
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line)
        if self.log_queue:
            try:
                self.log_queue.put(line, block=False)
            except Exception:
                pass

    # ------------------ Daily Limit Check ------------------
    def _check_daily_limit(self) -> bool:
        """
        Returns True if daily limit is reached.
        Resets counter if 24 hours have passed since start/last reset.
        """
        if time.time() - self.day_start_time > 86400:
            self.log("24 hours passed. Resetting daily trade counter.")
            self.day_start_time = time.time()
            self.trades_today = 0
        
        if self.trades_today >= self.MAX_TRADES_DAILY:
            return True
        return False

    # ------------------ Account file helpers (locked) ------------------
    def load_accounts(self) -> List[Dict[str, Any]]:
        try:
            with self._file_lock:
                with open(ACCOUNTS_FILE, "r") as fh:
                    return json.load(fh)
        except Exception as e:
            self.log(f"load_accounts error: {e}")
            return []

    def save_accounts(self, accounts: List[Dict[str, Any]]):
        try:
            with self._file_lock:
                with open(ACCOUNTS_FILE, "w") as fh:
                    json.dump(accounts, fh, indent=2)
        except Exception as e:
            self.log(f"save_accounts error: {e}")
            # Raise exception so API knows it failed (matching previous logic)
            raise RuntimeError(f"Failed to save accounts: {e}")

    def _read_trades(self) -> List[Dict[str, Any]]:
        try:
            with self._file_lock:
                with open(TRADES_FILE, "r") as fh:
                    return json.load(fh)
        except Exception as e:
            self.log(f"_read_trades error: {e}")
            return []

    def _write_trades(self, trades: List[Dict[str, Any]]):
        try:
            with self._file_lock:
                with open(TRADES_FILE, "w") as fh:
                    json.dump(trades, fh, indent=2)
        except Exception as e:
            self.log(f"_write_trades error: {e}")

    def add_trade(self, trade: Dict[str, Any]):
        try:
            trades = self._read_trades()
            trades.append(safe_json(trade, max_depth=6))
            if len(trades) > 500:
                trades = trades[-500:]
            self._write_trades(trades)
        except Exception as e:
            self.log(f"add_trade error: {e}")

    def update_trade(self, trade_id: str, updates: Dict[str, Any]) -> bool:
        try:
            changed = False
            trades = self._read_trades()
            for t in trades:
                if t.get("id") == trade_id:
                    t.update(safe_json(updates, max_depth=6))
                    changed = True
                    break
            if changed:
                self._write_trades(trades)
            return changed
        except Exception as e:
            self.log(f"update_trade error: {e}")
            return False

    # ------------------ Client wrapper ------------------
    def _get_client(self, account: Dict[str, Any]) -> Optional[HTTP]:
        """
        Return a pybit HTTP client or None if credentials missing.
        """
        try:
            key = account.get("api_key") or account.get("key") or account.get("apiKey")
            secret = account.get("api_secret") or account.get("secret") or account.get("apiSecret")
            if not key or not secret:
                return None
            client = HTTP(api_key=key, api_secret=secret, testnet=TRADE_SETTINGS.get("test_on_testnet", False))
            return client
        except Exception as e:
            self.log(f"_get_client error: {e}")
            return None

    # ------------------ API retry wrapper ------------------
    def _retry(self, fn: Callable[..., Any], attempts: int = 3, base_delay: float = 0.5, *args, **kwargs):
        last_exc = None
        for i in range(attempts):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                last_exc = e
                delay = base_delay * (i + 1)
                time.sleep(delay)
        raise last_exc

    # ------------------ Kline normalization ------------------
    def _normalize_klines_payload(self, raw_klines: Any) -> Tuple[List[float], List[float], List[float], List[Dict[str, float]]]:
        closes: List[float] = []
        highs: List[float] = []
        lows: List[float] = []
        ohlc: List[Dict[str, float]] = []
        try:
            if isinstance(raw_klines, dict) and "result" in raw_klines:
                payload = raw_klines["result"]
            elif isinstance(raw_klines, dict) and "data" in raw_klines:
                payload = raw_klines["data"]
            else:
                payload = raw_klines

            if isinstance(payload, list):
                for item in payload:
                    try:
                        if isinstance(item, (list, tuple)) and len(item) >= 5:
                            o = float(item[1]); h = float(item[2]); l = float(item[3]); c = float(item[4])
                        elif isinstance(item, dict):
                            o = float(item.get("open") or item.get("Open") or item.get("o"))
                            h = float(item.get("high") or item.get("High") or item.get("h"))
                            l = float(item.get("low") or item.get("Low") or item.get("l"))
                            c = float(item.get("close") or item.get("Close") or item.get("c"))
                        else:
                            continue
                        closes.append(c); highs.append(h); lows.append(l)
                        ohlc.append({"open": o, "high": h, "low": l, "close": c})
                    except Exception:
                        continue
        except Exception as e:
            self.log(f"_normalize_klines_payload error: {e}")
        return closes, highs, lows, ohlc

    # ------------------ Price parsing ------------------
    def _parse_price(self, raw: Any) -> Optional[float]:
        """Robustly parse price values from various API response shapes."""
        try:
            if raw is None:
                return None
            if isinstance(raw, (int, float)):
                return float(raw)
            if isinstance(raw, str):
                try:
                    return float(raw)
                except Exception:
                    return None
            if isinstance(raw, dict):
                # common fields
                for k in ("executed_price", "price", "avgPrice", "last_price", "lastPrice", "close", "last"):
                    if k in raw and raw[k] not in (None, ""):
                        try:
                            return float(raw[k])
                        except Exception:
                            pass
                # dive deeper into typical containers
                for key in ("result", "data", "tick", "ticker"):
                    if key in raw:
                        p = self._parse_price(raw[key])
                        if p is not None:
                            return p
                # check values
                for v in raw.values():
                    p = self._parse_price(v)
                    if p is not None:
                        return p
            if isinstance(raw, (list, tuple)) and raw:
                first = raw[0]
                # kline arrays often: [ts, o, h, l, c, v]
                if isinstance(first, (list, tuple)) and len(first) >= 5:
                    try:
                        return float(first[4])
                    except Exception:
                        pass
                # fallback: check elements
                for it in raw:
                    p = self._parse_price(it)
                    if p is not None:
                        return p
            return None
        except Exception:
            return None

    # ------------------ Scoring (improved) ------------------
    def score_symbol(self, client: HTTP, symbol: str) -> Tuple[int, Dict[str, Any]]:
        diagnostics: Dict[str, Any] = {}
        try:
            raw_klines = self._retry(lambda: self.safe_get_klines(client, symbol, interval=TIMEFRAME, limit=300))
            self._capture_preview({}, raw_klines, label="klines")
        except Exception as e:
            return 0, {"error": f"klines_fetch_failed: {e}"}

        closes, highs, lows, ohlc = self._normalize_klines_payload(raw_klines)
        if not closes:
            return 0, {"error": "no_closes"}

        # indicators
        rsi = wilder_rsi(closes[-(SCORE_SETTINGS["rsi_period"] + 50):], SCORE_SETTINGS["rsi_period"])
        ema50 = calc_ema(closes[-220:], 50) if len(closes) >= 50 else None
        ema200 = calc_ema(closes[-500:], 200) if len(closes) >= 200 else None
        momentum = smoothed_momentum_pct(closes, lookback=5, smooth_span=3)
        candle_ok = detect_bullish_candle(ohlc[-5:]) if len(ohlc) >= 5 else False

        fib = pivot_fib_levels_from_confirmed_window(highs, lows, lookback=SCORE_SETTINGS.get("fib_lookback", 50))
        current_price = closes[-1]

        score = 0
        breakdown = {"rsi": rsi, "momentum_pct": momentum, "ema50": ema50, "ema200": ema200, "candle_ok": candle_ok, "fib_levels": fib, "current_price": current_price}

        # RSI scoring: prefer not overbought & dipping values in uptrend
        if rsi is not None:
            if rsi <= SCORE_SETTINGS["rsi_oversold_threshold"]:
                score += int(SCORE_SETTINGS["score_weights"]["rsi"] * 1.5)
            elif rsi <= 55:
                score += SCORE_SETTINGS["score_weights"]["rsi"]

        # momentum
        if momentum > SCORE_SETTINGS["momentum_entry_threshold_pct"]:
            score += SCORE_SETTINGS["score_weights"]["momentum"]
        if momentum >= SCORE_SETTINGS["momentum_strong_pct"]:
            score += 1
        if momentum >= SCORE_SETTINGS["momentum_very_strong_pct"]:
            score += 1

        # EMA trend
        if ema50 is not None and ema200 is not None and ema50 > ema200:
            score += SCORE_SETTINGS["score_weights"]["ema"]
            breakdown["trend"] = "bull"
        else:
            breakdown["trend"] = "not_bull"

        # candle
        if candle_ok:
            score += SCORE_SETTINGS["score_weights"]["candle"]

        # fib zone
        in_fib = fib and price_in_zone(current_price, fib, "0.382", "0.618")
        if in_fib:
            score += SCORE_SETTINGS["score_weights"]["fib_zone"]
        breakdown["in_fib_zone"] = bool(in_fib)

        diagnostics.update(breakdown)
        diagnostics["score"] = int(score)
        return int(score), diagnostics

    # ------------------ Entry / Exit logic (clean) ------------------
    def should_enter_trade(self, closes: List[float], candles: List[Dict[str, float]]) -> Tuple[bool, Dict[str, Any]]:
        """
        Return (True, plan) if entry rules pass.
        plan includes 'fib_levels' and other meta used later to compute SL/TP.
        """
        if len(closes) < 50 or len(candles) < 50:
            return False, {"reason": "insufficient_history"}

        latest_price = closes[-1]
        rsi = wilder_rsi(closes, SCORE_SETTINGS["rsi_period"])
        if rsi is None or rsi > SCORE_SETTINGS["rsi_oversold_threshold"]:
            return False, {"reason": "rsi_not_low", "rsi": rsi}

        momentum = smoothed_momentum_pct(closes, lookback=5, smooth_span=3)
        if momentum < SCORE_SETTINGS["momentum_entry_threshold_pct"]:
            return False, {"reason": "momentum_too_weak", "momentum": momentum}

        bullish = detect_bullish_candle(candles[-5:])
        if not bullish:
            return False, {"reason": "no_bullish_candle"}

        ema50 = calc_ema(closes, 50)
        ema200 = calc_ema(closes, 200)
        if ema50 is None or ema200 is None or not (ema50 > ema200) or latest_price < ema50:
            return False, {"reason": "no_trend_or_below_ema", "ema50": ema50, "ema200": ema200}

        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        fib = pivot_fib_levels_from_confirmed_window(highs, lows, lookback=SCORE_SETTINGS.get("fib_lookback", 50))
        if not fib or not price_in_zone(latest_price, fib, "0.382", "0.618"):
            return False, {"reason": "not_in_fib_zone"}

        # All checks passed; return plan
        return True, {
            "rsi": rsi,
            "momentum_pct": momentum,
            "ema50": ema50,
            "ema200": ema200,
            "fib_levels": fib,
        }

    # ------------------ Order placement with validation ------------------
    def _place_market_order(self, client: HTTP, symbol: str, side: str, qty: float, price_hint: Optional[float] = None) -> Dict[str, Any]:
        """
        Wrapper that attempts several possible pybit order methods.
        Returns dict with either order result or {'error': msg}. If dry_run, returns simulated result.
        """
        if TRADE_SETTINGS.get("dry_run", True):
            # Simulated order fill at price_hint
            return {"simulated": True, "symbol": symbol, "side": side, "qty": qty, "executed_price": price_hint, "time": now_iso()}

        candidate_methods = ["place_active_order", "create_order", "place_order", "order"]
        last_exc = None
        for name in candidate_methods:
            meth = getattr(client, name, None)
            if not callable(meth):
                continue
            attempts = [
                {"category": "linear", "symbol": symbol, "side": side, "orderType": "Market", "qty": qty},
                {"symbol": symbol, "side": side, "order_type": "Market", "qty": qty},
                {"symbol": symbol, "side": side, "type": "market", "qty": qty},
                {"symbol": symbol, "side": side, "orderType": "Market", "qty": qty},
            ]
            for params in attempts:
                try:
                    resp = meth(**params)
                    # normalize resp
                    if isinstance(resp, dict):
                        if any(k in resp for k in ("ret_code", "retCode", "error_code", "err_code")):
                            # some exchanges embed error codes
                            for rc in ("ret_code", "retCode", "error_code", "err_code"):
                                if rc in resp and resp.get(rc) not in (0, None, "0"):
                                    last_exc = RuntimeError(f"API error {rc}: {resp.get(rc)}")
                                    resp = {"error": str(resp)}
                                    break
                        return resp if isinstance(resp, dict) else {"result": resp}
                    else:
                        return {"result": str(resp)}
                except Exception as e:
                    last_exc = e
                    continue
        if last_exc:
            return {"error": str(last_exc)}
        return {"error": "order_failed_unknown"}

    # ------------------ High-level attempt to open trade ------------------
    def attempt_trade_for_account(self, acct: Dict[str, Any]):
        try:
            if not acct.get("monitoring"):
                return
            if acct.get("position") == "open" or acct.get("open_trade_id"):
                return
            
            # Check Daily Limit
            if self._check_daily_limit():
                self.log(f"Daily trade limit ({self.MAX_TRADES_DAILY}) reached. Skipping trade for {acct.get('name')}.")
                return

            client = self._get_client(acct)
            if not client:
                self.log(f"No client for account {acct.get('name')}")
                acct.setdefault("last_validation_error", "missing_client")
                return

            ok, bal, err = self.validate_account(acct)
            acct["validated"] = ok
            acct["balance"] = bal
            acct["last_validation_error"] = err
            if not ok or not bal or float(bal) <= 0.0:
                return

            # allocation percentage (support percent expressed as 100)
            alloc_pct = float(TRADE_SETTINGS.get("trade_allocation_pct", 100))
            if alloc_pct > 1.0:
                alloc_pct = alloc_pct / 100.0
            usd_alloc = float(bal) * alloc_pct
            if usd_alloc < TRADE_SETTINGS.get("min_trade_amount", 5.0):
                self.log(f"Computed allocation ${usd_alloc:.2f} below min; skipping")
                return

            # score all coins
            candidates: List[Tuple[str, int, Dict[str, Any]]] = []
            for symbol in ALLOWED_COINS:
                try:
                    sc, diag = self.score_symbol(client, symbol)
                    if sc >= 3:
                        candidates.append((symbol, sc, diag))
                except Exception as e:
                    self.log(f"score_symbol error {symbol}: {e}")
                    continue

            if not candidates:
                self.log("No candidates found this cycle.")
                return

            candidates.sort(key=lambda x: x[1], reverse=True)
            best_symbol, best_score, best_diag = candidates[0]
            self.log(f"Top candidate: {best_symbol} score={best_score}")

            price = best_diag.get("current_price")
            if not price or price <= 0:
                try:
                    tick = self.safe_get_ticker(client, best_symbol)
                    price = self._parse_price(tick) or price
                except Exception:
                    pass
            if not price or price <= 0:
                self.log(f"Invalid price for {best_symbol}; skipping")
                return

            # refresh klines to produce arrays for should_enter_trade
            try:
                raw_klines = self._retry(lambda: self.safe_get_klines(client, best_symbol, interval=TIMEFRAME, limit=300))
            except Exception as e:
                self.log(f"Failed to fetch klines for entry planning {best_symbol}: {e}")
                return

            closes, highs, lows, ohlc = self._normalize_klines_payload(raw_klines)
            should_enter, plan = self.should_enter_trade(closes, ohlc)
            if not should_enter:
                self.log(f"should_enter_trade failed for {best_symbol}: {plan.get('reason') if isinstance(plan, dict) else plan}")
                return

            fib_levels = plan.get("fib_levels", {})
            # compute swing_low (fib 1.0) if available else fallback
            swing_low = None
            if fib_levels and "1.0" in fib_levels:
                swing_low = float(fib_levels["1.0"])
            elif lows:
                swing_low = min(lows[-SCORE_SETTINGS.get("fib_lookback", 50):])
            else:
                swing_low = price

            # stop loss = 1% below swing low (ensures SL < entry)
            sl_price = round(float(swing_low) * (1.0 - (RISK_RULES.get("stop_loss_pct", 1.0) / 100.0)), 8)
            # take profit: pick first available fib extension in order
            tp_price = None
            if fib_levels:
                for k in ("1.272_ext", "1.618_ext", "2.618_ext"):
                    if k in fib_levels and fib_levels[k] > price:
                        tp_price = float(fib_levels[k])
                        break
            if not tp_price:
                # fallback 4% extension if fib not available
                tp_price = round(price * 1.04, 8)

            notional = usd_alloc
            if notional < TRADE_SETTINGS.get("min_trade_amount", 5.0):
                self.log(f"Notional ${notional:.2f} below min; skipping")
                return

            qty = round(notional / price, 6)
            if qty <= 0:
                self.log(f"Computed qty <= 0 for {best_symbol}; skip")
                return

            # place order
            resp = self._place_market_order(client, best_symbol, "Buy", qty, price_hint=price)
            if isinstance(resp, dict) and resp.get("error"):
                self.log(f"Order error for {best_symbol}: {resp.get('error')}; skipping.")
                return
            simulated = bool(resp.get("simulated")) if isinstance(resp, dict) else False

            # extract entry price from response if present
            entry_price = None
            if isinstance(resp, dict):
                for k in ("executed_price", "avgPrice", "price", "last_price", "lastPrice"):
                    if k in resp and resp[k] is not None:
                        try:
                            entry_price = float(resp[k])
                            break
                        except Exception:
                            continue
            if entry_price is None:
                entry_price = float(price)

            ts = now_ts()
            tid = self._record_trade_entry(acct, best_symbol, qty, entry_price, ts, simulated, sl_price, tp_price)

            # Increment daily trade count
            self.trades_today += 1
            self.log(f"Trade count for today: {self.trades_today}/{self.MAX_TRADES_DAILY}")

            acct["position"] = "open"
            acct["current_symbol"] = best_symbol
            acct["entry_price"] = entry_price
            acct["entry_qty"] = qty
            acct["entry_time"] = ts
            acct["open_trade_id"] = tid
            acct["buy_price"] = entry_price
            acct["stop_loss_price"] = sl_price
            acct["take_profit_price"] = tp_price
            acct["score"] = best_score

            self.log(f"Opened trade {tid} {best_symbol} qty={qty} entry={entry_price} SL={sl_price} TP={tp_price} simulated={simulated}")

        except Exception as e:
            self.log(f"attempt_trade_for_account exception: {e}")

    # ------------------ position monitor & exit ------------------
    def _check_open_position(self, acct: Dict[str, Any]):
        try:
            trade_id = acct.get("open_trade_id")
            if not acct.get("position") == "open" or not trade_id:
                return

            client = self._get_client(acct)
            if not client:
                self.log(f"No client for checking position {acct.get('name')}")
                return

            symbol = acct.get("current_symbol")
            if not symbol:
                return

            entry_price = acct.get("entry_price")
            qty = acct.get("entry_qty")
            entry_ts = acct.get("entry_time") or now_ts()

            tick = None
            try:
                tick = self.safe_get_ticker(client, symbol)
                self._capture_preview(acct, tick, label="ticker_check")
            except Exception as e:
                self.log(f"safe_get_ticker error: {e}")
                return

            current_price = self._parse_price(tick)
            if current_price is None:
                self.log(f"Could not parse ticker price for {symbol}")
                return

            elapsed = max(0, now_ts() - int(entry_ts))

            sl_price = acct.get("stop_loss_price")
            tp_price = acct.get("take_profit_price")

            label = None
            should_close = False

            # SL check (SL is below entry by design)
            if sl_price is not None and current_price <= sl_price:
                label = "stop_loss"
                should_close = True

            # TP (fib) check
            if not should_close and tp_price is not None and current_price >= tp_price:
                label = "fib_take_profit"
                should_close = True

            # max hold time (30 minutes)
            if not should_close and elapsed >= RISK_RULES.get("max_hold", 30 * 60):
                label = "max_hold_expired"
                should_close = True

            if should_close:
                resp = self._place_market_order(client, symbol, "Sell", qty, price_hint=current_price)
                simulated = bool(resp.get("simulated")) if isinstance(resp, dict) else True

                exit_price = None
                if isinstance(resp, dict):
                    for k in ("executed_price", "avgPrice", "price", "last_price", "lastPrice"):
                        if k in resp and resp[k] is not None:
                            try:
                                exit_price = float(resp[k]); break
                            except Exception:
                                continue
                if exit_price is None:
                    exit_price = float(current_price)

                exit_ts = now_ts()
                try:
                    profit_pct = ((float(exit_price) - float(entry_price)) / float(entry_price)) * 100.0
                except Exception:
                    profit_pct = None

                resp_summary = safe_json(resp) if isinstance(resp, (dict, list)) else str(resp)
                self._finalize_trade(trade_id, exit_price, exit_ts, label, resp_summary, simulated)

                acct["position"] = "closed"
                acct.pop("entry_price", None)
                acct.pop("entry_qty", None)
                acct.pop("entry_time", None)
                acct.pop("open_trade_id", None)
                acct["current_symbol"] = None
                acct["buy_price"] = None
                acct.pop("stop_loss_price", None)
                acct.pop("take_profit_price", None)

                self.log(f"Closed trade {trade_id} for {acct.get('name')} label={label} exit_price={exit_price} profit_pct={profit_pct} elapsed={fmt_elapsed(exit_ts - entry_ts)} simulated={simulated}")

        except Exception as e:
            self.log(f"_check_open_position error: {e}")

    # ------------------ helpers: capture raw responses for debugging ------------------
    def _capture_preview(self, account: Dict[str, Any], resp: Any, label: str = "resp"):
        try:
            if not TRADE_SETTINGS.get("debug_raw_responses", False):
                return
            preview = safe_json(resp, max_depth=3)
            account.setdefault("last_raw_preview", {})
            key = f"{label}-{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}"
            account["last_raw_preview"][key] = preview
            keys = list(account["last_raw_preview"].keys())
            if len(keys) > 3:
                for k in keys[:-3]:
                    account["last_raw_preview"].pop(k, None)
        except Exception:
            pass

    # ------------------ API compatibility wrappers ------------------
    def _try_methods(self, client: HTTP, candidate_names: List[str], *args, **kwargs) -> Any:
        last_exc = None
        for name in candidate_names:
            try:
                meth = getattr(client, name, None)
                if not callable(meth):
                    continue
                try:
                    return meth(*args, **kwargs)
                except TypeError:
                    return meth(**kwargs) if kwargs else meth(*args)
            except Exception as e:
                last_exc = e
                continue
        if last_exc:
            raise last_exc
        raise RuntimeError(f"No candidate methods succeeded: {candidate_names}")

    def safe_get_ticker(self, client: HTTP, symbol: str) -> Any:
        candidates = ["ticker_price", "get_ticker", "get_symbol_ticker", "latest_information_for_symbol", "tickers", "get_tickers", "get_ticker_price"]
        try:
            return self._try_methods(client, candidates, symbol)
        except Exception:
            return self._try_methods(client, candidates, params={"symbol": symbol})

    def safe_get_klines(self, client: HTTP, symbol: str, interval: str = TIMEFRAME, limit: int = 200) -> Any:
        candidates = ["query_kline", "get_kline", "get_klines", "query_candles", "get_candlesticks", "kline"]
        try:
            return self._try_methods(client, candidates, symbol, interval, limit)
        except Exception:
            return self._try_methods(client, candidates, params={"symbol": symbol, "interval": interval, "limit": limit})

    # ------------------ account validation (attempts multiple methods) ------------------
    def _extract_balance_from_payload(self, payload: Any) -> Optional[float]:
        try:
            if payload is None:
                return None
            if isinstance(payload, dict):
                for k in ("total_equity", "equity", "totalBalance", "total_balance"):
                    if k in payload:
                        try:
                            return float(payload[k])
                        except Exception:
                            pass
                for lk in ("list", "balances", "rows", "data", "wallets"):
                    if lk in payload and isinstance(payload[lk], list):
                        total = 0.0
                        for item in payload[lk]:
                            try:
                                coin = item.get("coin") or item.get("symbol")
                                free = item.get("free") or item.get("available") or item.get("walletBalance")
                                if free is None:
                                    continue
                                freef = float(free)
                                if coin and str(coin).upper() in ("USDT", "USDC"):
                                    total += freef
                            except Exception:
                                continue
                        if total > 0:
                            return total
                # fallback when payload is dict of coin->info
                if payload and all(isinstance(v, dict) for v in payload.values()):
                    total = 0.0
                    for coin, info in payload.items():
                        try:
                            available = info.get("available") or info.get("free") or info.get("walletBalance")
                            if available is None:
                                continue
                            if str(coin).upper() in ("USDT", "USDC"):
                                total += float(available)
                        except Exception:
                            continue
                    if total > 0:
                        return total
            if isinstance(payload, list):
                total = 0.0
                for item in payload:
                    try:
                        coin = item.get("coin") or item.get("symbol")
                        free = item.get("free") or item.get("available") or item.get("walletBalance")
                        if free is None:
                            continue
                        freef = float(free)
                        if coin and str(coin).upper() in ("USDT", "USDC"):
                            total += freef
                    except Exception:
                        continue
                if total > 0:
                    return total
            return None
        except Exception:
            return None

    def validate_account(self, account: Dict[str, Any]) -> Tuple[bool, Optional[float], str]:
        client = self._get_client(account)
        if not client:
            return False, None, "missing_api_credentials"
        last_err = ""
        balance = None
        candidate_attempts = [
            (["get_wallet_balance", "query_wallet_balance"], {"accountType": "UNIFIED"}),
            (["get_wallet_balance", "query_wallet_balance"], {"accountType": "SPOT"}),
            (["get_balances", "balance", "get_wallet_balance"], {}),
        ]
        raw_preview = {}
        for methods, params in candidate_attempts:
            try:
                resp = None
                if params:
                    resp = self._try_methods(client, methods, **params)
                else:
                    resp = self._try_methods(client, methods)
                self._capture_preview(account, resp, label="balance")
                payloads = [resp] if not isinstance(resp, dict) else ([resp.get("result")] if resp.get("result") else []) + ([resp.get("data")] if resp.get("data") else []) + [resp]
                for payload in payloads:
                    bal = self._extract_balance_from_payload(payload)
                    if bal is not None:
                        balance = bal
                        break
                if balance is not None:
                    break
            except Exception as e:
                last_err = str(e)
                raw_preview[f"err_{len(raw_preview)+1}"] = last_err
                continue
        if TRADE_SETTINGS.get("debug_raw_responses", False) and raw_preview:
            account.setdefault("last_raw_preview", {}); account["last_raw_preview"].update(safe_json(raw_preview))
        if balance is not None:
            return True, balance, ""
        return False, None, last_err or "unrecognized_balance_shape"

    # ------------------ trade record helpers ------------------
    def _record_trade_entry(self, acct: Dict[str, Any], symbol: str, qty: float, entry_price: float, ts: int, simulated: bool, sl_price: Optional[float], tp_price: Optional[float]) -> str:
        tid = str(uuid.uuid4())
        rec = {
            "id": tid,
            "account_id": acct.get("id"),
            "account_name": acct.get("name"),
            "symbol": symbol,
            "side": "Buy",
            "qty": qty,
            "entry_price": entry_price,
            "entry_time": datetime.utcfromtimestamp(ts).isoformat(),
            "open": True,
            "simulated": bool(simulated),
            "stop_loss_price": sl_price,
            "take_profit_price": tp_price,
        }
        self.add_trade(rec)
        return tid

    def _finalize_trade(self, trade_id: str, exit_price: float, exit_ts: int, label: str, resp_summary: Optional[str], simulated: bool):
        trades = self._read_trades()
        entry = None
        for t in trades:
            if t.get("id") == trade_id:
                entry = t
                break
        if not entry:
            self.add_trade({
                "id": trade_id,
                "symbol": None,
                "side": "Buy",
                "qty": None,
                "entry_price": None,
                "entry_time": None,
                "exit_price": exit_price,
                "exit_time": datetime.utcfromtimestamp(exit_ts).isoformat(),
                "elapsed": fmt_elapsed(0),
                "elapsed_seconds": 0,
                "profit_pct": 0.0,
                "label": label,
                "simulated": bool(simulated),
                "resp_summary": resp_summary,
            })
            return
        entry_price = entry.get("entry_price")
        entry_time = entry.get("entry_time")
        try:
            entry_ts = int(datetime.fromisoformat(entry_time).timestamp()) if entry_time else exit_ts
        except Exception:
            entry_ts = exit_ts
        elapsed = max(0, exit_ts - entry_ts)
        profit_pct = None
        try:
            if entry_price is not None:
                profit_pct = ((float(exit_price) - float(entry_price)) / float(entry_price)) * 100.0
        except Exception:
            profit_pct = None
        updates = {
            "exit_price": exit_price,
            "exit_time": datetime.utcfromtimestamp(exit_ts).isoformat(),
            "elapsed": fmt_elapsed(elapsed),
            "elapsed_seconds": elapsed,
            "profit_pct": profit_pct,
            "label": label,
            "open": False,
            "resp_summary": resp_summary,
            "simulated": bool(simulated),
        }
        self.update_trade(trade_id, updates)

    # ------------------ main scan loop helpers ------------------
    def _scan_once(self):
        accounts = self.load_accounts()
        updated = False
        for acct in accounts:
            acct.setdefault("id", str(uuid.uuid4()))
            try:
                ok, bal, err = self.validate_account(acct)
                acct["validated"] = ok
                acct["balance"] = bal
                acct["last_validation_error"] = err
            except Exception as e:
                acct["validated"] = False
                acct["balance"] = None
                acct["last_validation_error"] = str(e)

            acct.setdefault("position", acct.get("position", "closed"))
            acct.setdefault("monitoring", acct.get("monitoring", False))
            acct.setdefault("current_symbol", acct.get("current_symbol"))
            acct.setdefault("buy_price", acct.get("buy_price"))

            try:
                if acct.get("position") == "open" and acct.get("open_trade_id"):
                    self._check_open_position(acct)
                else:
                    try:
                        self.attempt_trade_for_account(acct)
                    except Exception as e:
                        self.log(f"attempt_trade_for_account raised: {e}")
                acct["last_balance"] = acct.get("balance", acct.get("last_balance", 0.0))
                acct["last_validation_error"] = acct.get("last_validation_error")
            except Exception as e:
                self.log(f"Account scan error for {acct.get('id')}: {e}")
            updated = True
        if updated:
            self.save_accounts(accounts)

    # ------------------ start / stop / run loop ------------------
    def start(self):
        if self._running:
            return
        self._running = True
        self._stop.clear()
        t = threading.Thread(target=self._run_loop, daemon=True)
        self._threads.append(t)
        t.start()
        self.log("BotController started")

    def stop(self):
        self._stop.set()
        self._running = False
        self.log("Stop requested")
        for t in self._threads:
            if t.is_alive():
                t.join(timeout=1)
        self.log("Stopped")

    def _run_loop(self):
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception as e:
                self.log(f"run loop error: {e}")
            interval = int(TRADE_SETTINGS.get("scan_interval", 10))
            for _ in range(interval):
                if self._stop.is_set():
                    break
                time.sleep(1)

# ------------------ CLI debug run ------------------
if __name__ == "__main__":
    bc = BotController()
    print(f"Starting debug run (dry_run={TRADE_SETTINGS.get('dry_run')})")
    accounts = bc.load_accounts()
    if not accounts:
        print("No accounts found in app/accounts.json — add a test account with api_key/api_secret (dry_run=True recommended).")
    else:
        for a in accounts:
            ok, bal, err = bc.validate_account(a)
            print(f"Account id={a.get('id')} ok={ok} balance={bal} err={err}")
    # run one scan
    bc._scan_once()
    print("Done")
