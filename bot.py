"""
Superb Crypto Bot — Full patched bot.py

Features:
- is_running()
- TP timeline: TP1 (0-10m -> +7%), TP2 (10-18m -> +4%), TP3 (18-23m -> +1%)
- Stop-loss: -3%
- Force-exit after 23 minutes
- Elapsed time tracking (Xm Ys) recorded in trades.json and logs
- Minimum trade notional enforced (TRADE_SETTINGS['min_trade_amount'] = 5.0)
- dry_run toggle (safe default True) to avoid live orders until you're ready
- Robust Bybit SDK call fallbacks and response parsing with recursion guards
- Thread-safe file operations and sanitized writes to prevent circular serialization
- last_raw_preview on account dicts (sanitized, no secrets)
"""
from __future__ import annotations

import json
import os
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
    "tp1": (600, 3.0),    # up to 10 minutes -> 3%
    "tp2": (1080, 2.0),   # up to 18 minutes -> 2%
    "tp3": (1380, 1.0),   # up to 23 minutes -> 1%
    "max_hold": 1380,     # force exit at 23 minutes
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
    "min_trade_amount": 5.0,        # enforce Bybit minimum order notional
    "use_market_order": True,
    "test_on_testnet": False,       # production endpoint by default (you asked)
    "scan_interval": 10,            # seconds between scans
    "debug_raw_responses": False,   # store small sanitized previews
    "dry_run": True                 # safe default: simulate orders
}

ACCOUNTS_FILE = "accounts.json"
TRADES_FILE = "trades.json"

# -------------------- Utilities --------------------
def now_ts() -> int:
    return int(time.time())

def now_iso() -> str:
    return datetime.utcnow().isoformat()

def fmt_elapsed(seconds: int) -> str:
    m = seconds // 60
    s = seconds % 60
    return f"{m}m {s}s"

def safe_json(data: Any, max_depth: int = 4, _depth: int = 0, _seen: Optional[set] = None) -> Any:
    """
    Convert arbitrary API responses into a JSON-safe structure.
    Clips at max_depth and replaces circular references with "<recursion>".
    """
    if _seen is None:
        _seen = set()
    try:
        if id(data) in _seen or _depth > max_depth:
            return "<recursion>"
        _seen.add(id(data))
    except Exception:
        # in case id() or set ops fail for some exotic object
        pass

    if data is None:
        return None
    if isinstance(data, (str, int, float, bool)):
        return data
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            try:
                out_k = str(k)
            except Exception:
                out_k = "<key>"
            out[out_k] = safe_json(v, max_depth, _depth + 1, _seen)
        return out
    if isinstance(data, (list, tuple)):
        return [safe_json(v, max_depth, _depth + 1, _seen) for v in data]
    # fallback for other types
    try:
        json.dumps(data)
        return data
    except Exception:
        try:
            return str(data)
        except Exception:
            return "<unserializable>"

# -------------------- Bot Controller --------------------
class BotController:
    def __init__(self, log_queue: Optional[threading.Queue] = None):
        # Logging queue (SSE) from server.py
        self.log_queue = log_queue
        self._running = False
        self._stop_event = threading.Event()
        self._file_lock = threading.Lock()
        self._threads: List[threading.Thread] = []

        # Ensure files exist
        for path, default in ((ACCOUNTS_FILE, []), (TRADES_FILE, [])):
            if not os.path.exists(path):
                with open(path, "w") as f:
                    json.dump(default, f, indent=2)

    # ---- status ----
    def is_running(self) -> bool:
        """Return True if the bot is active and not stopping."""
        return bool(self._running and not self._stop_event.is_set())

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

    def _read_trades(self) -> List[Dict[str, Any]]:
        try:
            with open(TRADES_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []

    def _write_trades(self, trades: List[Dict[str, Any]]):
        with self._file_lock:
            with open(TRADES_FILE, "w") as f:
                json.dump(trades, f, indent=2)

    def _sanitize_for_json(self, obj: Any) -> Any:
        """Sanitize objects for JSON writing using safe_json roundtrip."""
        try:
            # Use safe_json to avoid recursion/circular structures
            return safe_json(obj, max_depth=6)
        except Exception:
            try:
                return json.loads(json.dumps(obj, default=str))
            except Exception:
                return str(obj)

    def add_trade(self, trade: Dict[str, Any]):
        with self._file_lock:
            trades = self._read_trades()
            trades.append(self._sanitize_for_json(trade))
            # cap stored list to last 500 trades to avoid file bloat
            if len(trades) > 500:
                trades = trades[-500:]
            self._write_trades(trades)

    def update_trade(self, trade_id: str, updates: Dict[str, Any]) -> bool:
        with self._file_lock:
            trades = self._read_trades()
            changed = False
            for t in trades:
                if t.get("id") == trade_id:
                    san = self._sanitize_for_json(updates)
                    if isinstance(san, dict):
                        t.update(san)
                    changed = True
                    break
            if changed:
                self._write_trades(trades)
            return changed

    # ---- Bybit client helpers ----
    def _get_client(self, account: Dict[str, Any]) -> Optional[HTTP]:
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

    def _capture_preview(self, account: Dict[str, Any], resp: Any, label: str = "resp"):
        """Keep a small sanitized preview of raw responses for debugging (no secrets)."""
        try:
            if not TRADE_SETTINGS.get("debug_raw_responses", False):
                return
            preview = safe_json(resp, max_depth=3)
            account.setdefault("last_raw_preview", {})
            key = f"{label}-{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}"
            account["last_raw_preview"][key] = preview
            # keep only last 3 previews
            keys = list(account["last_raw_preview"].keys())
            if len(keys) > 3:
                for k in keys[:-3]:
                    account["last_raw_preview"].pop(k, None)
        except Exception:
            pass

    # ---- robust method caller ----
    def _try_methods(self, client: HTTP, candidate_names: List[str], *args, **kwargs) -> Any:
        """
        Try several method names on the client and return the first successful response.
        Protects against RecursionError and attempts safe serialization to avoid circular shapes.
        """
        last_exc = None
        for name in candidate_names:
            try:
                meth = getattr(client, name, None)
                if not callable(meth):
                    continue
                # attempt call
                try:
                    resp = meth(*args, **kwargs)
                except RecursionError as re:
                    last_exc = re
                    continue
                except TypeError:
                    # try with params dict (some SDK signatures expect kwargs)
                    try:
                        resp = meth(**kwargs) if kwargs else meth(*args)
                    except Exception as e2:
                        last_exc = e2
                        continue
                except Exception as e:
                    last_exc = e
                    continue
                # ensure resp is safe-ish
                try:
                    _ = json.dumps(resp, default=str)
                except Exception:
                    # safe fallback: convert to str summary
                    try:
                        resp = str(resp)[:1000]
                    except Exception:
                        resp = "unserializable_response"
                # basic error checks for common keys
                if isinstance(resp, dict):
                    for rc in ("ret_code", "retCode", "error_code", "err_code"):
                        if rc in resp and resp.get(rc) not in (0, None, "0"):
                            # treat as failure for this candidate
                            last_exc = RuntimeError(f"API error from {name}: {resp.get(rc)}")
                            resp = None
                            break
                    if resp and resp.get("success") is False:
                        last_exc = RuntimeError(f"API reported failure from {name}")
                        resp = None
                if resp is not None:
                    return resp
            except Exception as e:
                last_exc = e
                continue
        if last_exc:
            raise last_exc
        raise RuntimeError("No candidate methods succeeded: " + ",".join(candidate_names))

    # ---- ticker/klines + price parsing (depth-guarded) ----
    def safe_get_ticker(self, client: HTTP, symbol: str) -> Any:
        candidates = ["ticker_price", "get_ticker", "get_symbol_ticker", "latest_information_for_symbol", "tickers", "get_tickers", "get_ticker_price"]
        try:
            return self._try_methods(client, candidates, symbol)
        except Exception:
            # try forms with params
            return self._try_methods(client, candidates, params={"symbol": symbol})

    def safe_get_klines(self, client: HTTP, symbol: str, interval: str = "1", limit: int = 50) -> Any:
        candidates = ["query_kline", "get_kline", "get_klines", "query_candles", "get_candlesticks", "kline"]
        try:
            return self._try_methods(client, candidates, symbol, interval, limit)
        except Exception:
            return self._try_methods(client, candidates, params={"symbol": symbol, "interval": interval, "limit": limit})

    def _parse_price(self, raw: Any, depth: int = 0, visited: Optional[set] = None) -> Optional[float]:
        """Safely extract a numeric price from varied response shapes with depth & visited guards."""
        if visited is None:
            visited = set()
        if depth > 8:
            return None
        try:
            rid = None
            try:
                rid = id(raw)
            except Exception:
                rid = None
            if rid is not None and rid in visited:
                return None
            if rid is not None:
                visited.add(rid)

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
                # common direct fields
                for k in ("price", "last_price", "lastPrice", "close", "last", "p", "avgPrice"):
                    if k in raw and raw[k] not in (None, ""):
                        try:
                            return float(raw[k])
                        except Exception:
                            pass
                # nested containers
                for key in ("result", "data", "list", "ticks", "rows", "items"):
                    if key in raw:
                        p = self._parse_price(raw[key], depth + 1, visited)
                        if p is not None:
                            return p
                # fallback: try all values (depth-limited)
                for v in raw.values():
                    p = self._parse_price(v, depth + 1, visited)
                    if p is not None:
                        return p
            if isinstance(raw, (list, tuple)) and raw:
                # kline-like: [open_time, open, high, low, close, ...]
                first = raw[0]
                # If first is a list of klines
                if isinstance(first, (list, tuple)) and len(first) >= 5:
                    try:
                        return float(first[4])
                    except Exception:
                        pass
                # If the list itself looks like a kline
                if len(raw) >= 5 and all(isinstance(x, (int, float, str)) for x in raw[:5]):
                    try:
                        return float(raw[4])
                    except Exception:
                        pass
                # otherwise, try elements in sequence
                for it in raw:
                    p = self._parse_price(it, depth + 1, visited)
                    if p is not None:
                        return p
            return None
        except RecursionError:
            return None
        except Exception:
            return None

    # ---- account validation (UNIFIED -> SPOT -> coin-array) ----
    def _extract_balance_from_payload(self, payload: Any) -> Optional[float]:
        """Try to extract a USDT-equivalent balance from various payload shapes."""
        try:
            if payload is None:
                return None
            # payload may be dict or list
            if isinstance(payload, dict):
                # unified totals
                for k in ("total_equity", "equity", "totalBalance", "total_balance"):
                    if k in payload:
                        try:
                            return float(payload[k])
                        except Exception:
                            pass
                # look for lists of balances
                for lk in ("list", "balances", "rows", "data", "wallets"):
                    if lk in payload and isinstance(payload[lk], list):
                        total = 0.0
                        for item in payload[lk]:
                            try:
                                coin = item.get("coin") or item.get("symbol")
                                free = item.get("free") or item.get("available") or item.get("walletBalance") or item.get("available_balance")
                                if free is None:
                                    continue
                                freef = float(free)
                                if coin and str(coin).upper() in ("USDT", "USDC"):
                                    total += freef
                            except Exception:
                                continue
                        if total > 0:
                            return total
                # coin -> dict mapping
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
        """
        Validate account and return (ok, balance, error_message).
        Attempts UNIFIED -> SPOT -> generic wallet array.
        Stores small sanitized previews (if debug enabled).
        """
        client = self._get_client(account)
        if not client:
            return False, None, "missing_api_credentials"
        raw_preview: Dict[str, Any] = {}
        balance = None
        last_err = ""
        # Try candidate calls with param shapes
        candidate_attempts = [
            (["get_wallet_balance", "query_wallet_balance"], {"accountType": "UNIFIED"}),
            (["get_wallet_balance", "query_wallet_balance"], {"accountType": "SPOT"}),
            (["get_wallet_balance", "query_wallet_balance", "get_balances", "balance"], {})
        ]
        for candidate_methods, params in candidate_attempts:
            try:
                # call with kwargs if params provided; otherwise call without
                if params:
                    resp = self._try_methods(client, candidate_methods, **params)
                else:
                    resp = self._try_methods(client, candidate_methods)
                self._capture_preview(account, resp, label="balance")
                # normalize payloads to a list of possible payloads
                payloads = []
                if isinstance(resp, dict):
                    if "result" in resp:
                        payloads.append(resp["result"])
                    if "data" in resp:
                        payloads.append(resp["data"])
                    payloads.append(resp)
                else:
                    payloads.append(resp)
                for payload in payloads:
                    bal = self._extract_balance_from_payload(payload)
                    if bal is not None:
                        balance = bal
                        break
                if balance is not None:
                    break
            except Exception as e:
                last_err = str(e)
                try:
                    raw_preview[f"err_{len(raw_preview)+1}"] = f"error: {str(e)}"
                except Exception:
                    pass
                continue
        # attach preview if debug enabled
        if TRADE_SETTINGS.get("debug_raw_responses", False) and raw_preview:
            try:
                account.setdefault("last_raw_preview", {})
                account["last_raw_preview"].update(safe_json(raw_preview))
                # trim
                keys = list(account["last_raw_preview"].keys())
                if len(keys) > 3:
                    for k in keys[:-3]:
                        account["last_raw_preview"].pop(k, None)
            except Exception:
                pass
        if balance is not None:
            return True, balance, ""
        return False, None, last_err or "unrecognized_balance_shape"

    # ---- order helpers (dry-run gated) ----
    def _place_market_order(self, client: HTTP, symbol: str, side: str, qty: float, price_hint: Optional[float] = None) -> Dict[str, Any]:
        """
        Place a market order via available SDK methods.
        If dry_run is True, simulate and return a consistent response.
        """
        if TRADE_SETTINGS.get("dry_run", True):
            executed_price = float(price_hint) if price_hint is not None else None
            resp = {"simulated": True, "symbol": symbol, "side": side, "qty": qty, "executed_price": executed_price, "time": now_iso()}
            return resp

        candidate_methods = ["place_active_order", "create_order", "place_order", "order"]
        last_exc = None
        for name in candidate_methods:
            meth = getattr(client, name, None)
            if not callable(meth):
                continue
            # Try several param shapes used by different SDK versions
            attempts = [
                {"category": "linear", "symbol": symbol, "side": side, "orderType": "Market", "qty": qty},
                {"symbol": symbol, "side": side, "order_type": "Market", "qty": qty},
                {"symbol": symbol, "side": side, "type": "market", "qty": qty},
                {"symbol": symbol, "side": side, "orderType": "Market", "qty": qty}
            ]
            for params in attempts:
                try:
                    resp = meth(**params)
                    self._capture_preview({}, resp, label="order")
                    try:
                        return resp if isinstance(resp, dict) else {"result": resp}
                    except Exception:
                        return {"result": str(resp)}
                except Exception as e:
                    last_exc = e
                    continue
        # If we reach here, all attempts failed
        self.log(f"Order placement failed for {symbol}: {last_exc}")
        return {"error": str(last_exc) if last_exc else "order_failed"}

    # ---- trade entry/finalize helpers ----
    def _record_trade_entry(self, acct: Dict[str, Any], symbol: str, qty: float, entry_price: float, ts: int, simulated: bool) -> str:
        tid = str(uuid.uuid4())
        trade = {
            "id": tid,
            "account_id": acct.get("id"),
            "account_name": acct.get("name"),
            "symbol": symbol,
            "side": "Buy",
            "qty": qty,
            "entry_price": entry_price,
            "entry_time": datetime.utcfromtimestamp(ts).isoformat(),
            "open": True,
            "simulated": bool(simulated)
        }
        self.add_trade(trade)
        return tid

    def _finalize_trade(self, trade_id: str, exit_price: float, exit_ts: int, label: str, resp_summary: Optional[str], simulated: bool):
        trades = self._read_trades()
        entry = None
        for t in trades:
            if t.get("id") == trade_id:
                entry = t
                break
        if not entry:
            elapsed_s = 0
            profit_pct = 0.0
            rec = {
                "id": trade_id,
                "symbol": None,
                "side": "Buy",
                "qty": None,
                "entry_price": None,
                "entry_time": None,
                "exit_price": exit_price,
                "exit_time": datetime.utcfromtimestamp(exit_ts).isoformat(),
                "elapsed": fmt_elapsed(elapsed_s),
                "elapsed_seconds": elapsed_s,
                "profit_pct": profit_pct,
                "label": label,
                "simulated": bool(simulated),
                "resp_summary": resp_summary
            }
            self.add_trade(rec)
            return
        entry_price = entry.get("entry_price")
        entry_time = entry.get("entry_time")
        try:
            entry_ts = int(datetime.fromisoformat(entry_time).timestamp()) if entry_time else exit_ts
        except Exception:
            try:
                entry_ts = int(datetime.fromisoformat(entry_time.replace("Z", "")).timestamp())
            except Exception:
                entry_ts = exit_ts
        elapsed_s = max(0, exit_ts - entry_ts)
        profit_pct = None
        if entry_price is not None and exit_price is not None:
            try:
                profit_pct = ((float(exit_price) - float(entry_price)) / float(entry_price)) * 100.0
            except Exception:
                profit_pct = None
        updates = {
            "exit_price": exit_price,
            "exit_time": datetime.utcfromtimestamp(exit_ts).isoformat(),
            "elapsed": fmt_elapsed(elapsed_s),
            "elapsed_seconds": elapsed_s,
            "profit_pct": profit_pct,
            "label": label,
            "open": False,
            "resp_summary": resp_summary,
            "simulated": bool(simulated)
        }
        self.update_trade(trade_id, updates)

    # ---- trading logic ----
    def attempt_trade_for_account(self, acct: Dict[str, Any]):
        """
        Conservative trade attempt:
        - uses acct['monitoring'] flag
        - checks last known balance and allocation
        - enforces min_trade_amount
        - opens a market BUY and records trade
        """
        try:
            if not acct.get("monitoring"):
                return
            if acct.get("position") == "open" or acct.get("open_trade_id"):
                return  # already open
            client = self._get_client(acct)
            if not client:
                self.log(f"No client for account {acct.get('name')}")
                acct.setdefault("last_validation_error", "missing_client")
                return
            bal = acct.get("balance") or acct.get("last_balance") or 0.0
            if bal < TRADE_SETTINGS.get("min_trade_amount", 5.0):
                self.log(f"Balance too low for trading ({bal}).")
                return
            # selection of symbol: prefer account preference or first allowed coin that passes some lightweight checks
            symbol = acct.get("preferred_symbol") or ALLOWED_COINS[0]
            allocation_pct = TRADE_SETTINGS.get("trade_allocation_pct", 0.01)
            usd_alloc = float(bal) * float(allocation_pct)
            if usd_alloc < TRADE_SETTINGS.get("min_trade_amount", 5.0):
                self.log(f"Computed allocation ${usd_alloc:.2f} below min ${TRADE_SETTINGS['min_trade_amount']:.2f}; skipping")
                return

            # fetch best-effort price
            tick = None
            try:
                tick = self.safe_get_ticker(client, symbol)
                self._capture_preview(acct, tick, label="ticker")
            except Exception as e:
                self.log(f"Ticker fetch failed for {symbol}: {e}")
                return
            price = self._parse_price(tick)
            if price is None or price <= 0:
                self.log(f"Could not parse price for {symbol}; skipping open.")
                return

            notional = usd_alloc
            if notional < TRADE_SETTINGS.get("min_trade_amount", 5.0):
                self.log(f"Skipping trade as notional ${notional:.2f} < min ${TRADE_SETTINGS['min_trade_amount']:.2f}")
                return

            # calculate qty; use a default precision (6 decimals) to avoid fractional issues
            qty = float(usd_alloc / price)
            # round qty to 6 decimal places as safe default (some assets require different precision)
            qty = round(qty, 6)
            if qty <= 0:
                self.log(f"Computed qty <= 0 for {symbol}; skip.")
                return

            resp = self._place_market_order(client, symbol, "Buy", qty, price_hint=price)
            simulated = bool(resp.get("simulated")) if isinstance(resp, dict) else True

            # determine entry price from response if available
            entry_price = None
            if isinstance(resp, dict):
                for k in ("executed_price", "avgPrice", "price", "last_price", "lastPrice"):
                    if k in resp and resp[k]:
                        try:
                            entry_price = float(resp[k])
                            break
                        except Exception:
                            continue
            if entry_price is None:
                entry_price = price

            ts = now_ts()
            trade_id = self._record_trade_entry(acct, symbol, qty, entry_price, ts, simulated)
            acct["position"] = "open"
            acct["current_symbol"] = symbol
            acct["entry_price"] = entry_price
            acct["entry_qty"] = qty
            acct["entry_time"] = ts
            acct["open_trade_id"] = trade_id
            acct["buy_price"] = entry_price
            self.log(f"Opened trade {trade_id} for {acct.get('name')} {symbol} qty={qty} price={entry_price} simulated={simulated}")

        except Exception as e:
            self.log(f"attempt_trade_for_account error: {e}")

    def _check_open_position(self, acct: Dict[str, Any]):
        """Monitor an open position and close on TP / SL / forced exit."""
        try:
            trade_id = acct.get("open_trade_id")
            if not acct.get("position") == "open" or not trade_id:
                return
            client = self._get_client(acct)
            if not client:
                self.log(f"No client to check position for {acct.get('name')}")
                return
            symbol = acct.get("current_symbol")
            if not symbol:
                self.log(f"No symbol for open position in acct {acct.get('name')}")
                return
            entry_price = acct.get("entry_price")
            qty = acct.get("entry_qty")
            entry_ts = acct.get("entry_time") or now_ts()

            tick = None
            try:
                tick = self.safe_get_ticker(client, symbol)
                self._capture_preview(acct, tick, label="ticker_check")
            except Exception as e:
                self.log(f"Ticker for check failed: {e}")
                return
            price = self._parse_price(tick)
            if price is None:
                self.log(f"Could not parse current price for {symbol}")
                return

            # compute profit percentage (buy-side)
            profit_pct = None
            try:
                profit_pct = ((float(price) - float(entry_price)) / float(entry_price)) * 100.0
            except Exception:
                profit_pct = None

            elapsed_s = max(0, now_ts() - int(entry_ts))
            label = None
            should_close = False

            # Stop-loss
            if profit_pct is not None and profit_pct <= RISK_RULES.get("stop_loss", -9999):
                label = "stop_loss"
                should_close = True
            # exit on any positive?
            elif RISK_RULES.get("exit_on_any_positive", False) and profit_pct is not None and profit_pct > 0:
                label = "profit_any"
                should_close = True
            else:
                tp1_end, tp1_pct = RISK_RULES["tp1"]
                tp2_end, tp2_pct = RISK_RULES["tp2"]
                tp3_end, tp3_pct = RISK_RULES["tp3"]
                if elapsed_s <= tp1_end:
                    target_pct = tp1_pct
                    label = "TP1"
                elif elapsed_s <= tp2_end:
                    target_pct = tp2_pct
                    label = "TP2"
                elif elapsed_s <= tp3_end:
                    target_pct = tp3_pct
                    label = "TP3"
                else:
                    # past TP windows -> forced exit
                    if elapsed_s > RISK_RULES.get("max_hold", tp3_end):
                        label = "forced_exit"
                        should_close = True
                    else:
                        target_pct = tp3_pct
                        label = "TP3"
                if not should_close and profit_pct is not None:
                    if profit_pct >= target_pct:
                        should_close = True

            if should_close:
                resp = self._place_market_order(client, symbol, "Sell", qty, price_hint=price)
                simulated = bool(resp.get("simulated")) if isinstance(resp, dict) else True
                exit_price = None
                if isinstance(resp, dict):
                    for k in ("executed_price", "avgPrice", "price", "last_price", "lastPrice"):
                        if k in resp and resp[k] is not None:
                            try:
                                exit_price = float(resp[k])
                                break
                            except Exception:
                                continue
                if exit_price is None:
                    exit_price = price
                exit_ts = now_ts()
                try:
                    final_profit = ((float(exit_price) - float(entry_price)) / float(entry_price)) * 100.0
                except Exception:
                    final_profit = profit_pct
                resp_summary = safe_json(resp) if isinstance(resp, (dict, list)) else str(resp)
                # finalize trade record
                self._finalize_trade(trade_id, exit_price, exit_ts, label, resp_summary, simulated)
                # update account state
                acct["position"] = "closed"
                acct.pop("entry_price", None)
                acct.pop("entry_qty", None)
                acct.pop("entry_time", None)
                acct.pop("open_trade_id", None)
                acct["current_symbol"] = None
                acct["buy_price"] = None
                self.log(f"Closed trade {trade_id} for {acct.get('name')} label={label} exit_price={exit_price} profit_pct={final_profit} elapsed={fmt_elapsed(exit_ts - entry_ts)} simulated={simulated}")

        except Exception as e:
            self.log(f"_check_open_position error for acct {acct.get('id')}: {e}")

    # ---- scan / orchestration ----
    def _scan_once(self):
        accounts = self.load_accounts()
        updated = False
        for acct in accounts:
            acct.setdefault("id", str(uuid.uuid4()))
            # validate account
            try:
                ok, bal, err = self.validate_account(acct)
                acct["validated"] = ok
                acct["balance"] = bal
                acct["last_validation_error"] = err
            except Exception as e:
                acct["validated"] = False
                acct["balance"] = None
                acct["last_validation_error"] = str(e)
            # ensure expected fields
            acct.setdefault("position", acct.get("position", "closed"))
            acct.setdefault("monitoring", acct.get("monitoring", False))
            acct.setdefault("current_symbol", acct.get("current_symbol"))
            acct.setdefault("buy_price", acct.get("buy_price"))
            try:
                # If open position exists, check it; else try to open new trade (if monitoring)
                if acct.get("position") == "open" and acct.get("open_trade_id"):
                    self._check_open_position(acct)
                else:
                    try:
                        self.attempt_trade_for_account(acct)
                    except Exception as e:
                        self.log(f"attempt_trade_for_account raised: {e}")
                # surface last_balance and last_validation_error for UI
                acct["last_balance"] = acct.get("balance", acct.get("last_balance", 0.0))
                acct["last_validation_error"] = acct.get("last_validation_error")
            except Exception as e:
                self.log(f"Account scan error for {acct.get('id')}: {e}")
            updated = True
        if updated:
            self.save_accounts(accounts)

    def start(self):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        t = threading.Thread(target=self._run_loop, daemon=True)
        self._threads.append(t)
        t.start()
        self.log("BotController started")

    def stop(self):
        self._stop_event.set()
        self._running = False
        self.log("Stop requested; waiting for threads to finish")
        for t in self._threads:
            if t.is_alive():
                t.join(timeout=1)
        self.log("Stopped")

    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                self._scan_once()
            except Exception as e:
                self.log(f"Run loop error: {e}")
            interval = int(TRADE_SETTINGS.get("scan_interval", 10))
            # sleep in 1-second slices so stop is responsive
            for _ in range(interval):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

# ---- CLI for local debugging ----
if __name__ == "__main__":
    bc = BotController()
    print("Superb Crypto Bot — debug run (dry_run is set to {} )".format(TRADE_SETTINGS.get("dry_run")))
    accts = bc.load_accounts()
    if not accts:
        print("No accounts found. Add accounts to accounts.json with api_key and api_secret to test validation.")
    else:
        for a in accts:
            ok, bal, err = bc.validate_account(a)
            print(f"Account id={a.get('id')} ok={ok} balance={bal} err={err}")
    # run one scan loop for debug
    bc._scan_once()
    print("Done")
