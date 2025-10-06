"""
bot.py — Superb Crypto Bot (recursion-safe, production-aware)

Key points:
 - is_running() accessor (server.py calls this)
 - TP timeline (TP1 0-10m @ +7%, TP2 10-18m @ +4%, TP3 18-23m @ +1%)
 - Stop-loss -3%, forced exit after 23 minutes
 - Elapsed time recorded as "Mm Ss" and stored in trades.json with profit_pct and label
 - Robust _parse_price() with visited set & depth cap to prevent RecursionError
 - _try_methods() sanitizes API responses and avoids circular serialization issues
 - add_trade/update_trade sanitize records before writing to disk
 - Thread-safe file I/O
 - Dry-run default ON
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
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
    "stop_loss": -3.0,                 # -3% stop loss
    "tp1": (600, 7.0),                 # 0 -> 10m -> 7%
    "tp2": (1080, 4.0),                # 10m -> 18m -> 4%
    "tp3": (1380, 1.0),                # 18m -> 23m -> 1%
    "max_hold": 1380,                  # 23 minutes
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
    "test_on_testnet": False,
    "scan_interval": 10,
    "debug_raw_responses": False,
    "dry_run": True
}

ACCOUNTS_FILE = "accounts.json"
TRADES_FILE = "trades.json"

# -------------------- helpers --------------------
def now_ts() -> int:
    return int(time.time())

def now_iso() -> str:
    return datetime.utcnow().isoformat()

def fmt_elapsed(seconds: int) -> str:
    m = seconds // 60
    s = seconds % 60
    return f"{m}m {s}s"

# -------------------- Bot Controller --------------------
class BotController:
    def __init__(self, log_queue: Optional[threading.Queue] = None):
        self.log_queue = log_queue
        self._running = False
        self._stop_event = threading.Event()
        self._file_lock = threading.Lock()
        self._threads: List[threading.Thread] = []

        # ensure files exist
        for path in (ACCOUNTS_FILE, TRADES_FILE):
            if not os.path.exists(path):
                with open(path, "w") as f:
                    json.dump([], f)

    # ---- status accessor ----
    def is_running(self) -> bool:
        """Return True if bot is running and not stopping."""
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
        """Sanitize objects for JSON writing using json.dumps(default=str) roundtrip."""
        try:
            j = json.dumps(obj, default=str)
            return json.loads(j)
        except Exception:
            # worst-case fallback: convert to str
            try:
                return str(obj)
            except Exception:
                return None

    def add_trade(self, trade: Dict[str, Any]):
        """Add trade record to trades.json safely (sanitizes to avoid circular refs)."""
        with self._file_lock:
            trades = self._read_trades()
            sanitized = self._sanitize_for_json(trade)
            trades.append(sanitized)
            self._write_trades(trades)

    def update_trade(self, trade_id: str, updates: Dict[str, Any]) -> bool:
        """Update a trade entry with sanitized updates."""
        with self._file_lock:
            trades = self._read_trades()
            changed = False
            for t in trades:
                if t.get("id") == trade_id:
                    sanitized = self._sanitize_for_json(updates)
                    t.update(sanitized)
                    changed = True
                    break
            if changed:
                self._write_trades(trades)
            return changed

    # ---- Bybit client helpers ----
    def _get_client(self, account: Dict[str, Any]) -> Optional[HTTP]:
        try:
            key = account.get("api_key")
            secret = account.get("api_secret")
            if not key or not secret:
                return None
            client = HTTP(api_key=key, api_secret=secret, testnet=TRADE_SETTINGS.get("test_on_testnet", False))
            return client
        except Exception as e:
            self.log(f"_get_client error: {e}")
            return None

    def _capture_preview(self, account: Dict[str, Any], resp: Any):
        """Store a short preview (no secrets) when debug is enabled."""
        try:
            if not TRADE_SETTINGS.get("debug_raw_responses", False):
                return
            try:
                preview = json.dumps(resp, default=str)[:400]
            except Exception:
                preview = str(type(resp))
            account.setdefault("last_raw_preview", {})
            key = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            account["last_raw_preview"][key] = preview
            keys = list(account["last_raw_preview"].keys())
            if len(keys) > 3:
                for k in keys[:-3]:
                    account["last_raw_preview"].pop(k, None)
        except Exception:
            pass

    # ---- robust SDK try/call ----
    def _try_methods(self, client: HTTP, candidate_names: List[str], *args, **kwargs):
        """
        Try candidate method names on the client safely.
        - Catches RecursionError and other exceptions.
        - Sanitizes response via json.dumps(default=str) to avoid circular serialisation.
        - Performs basic Bybit-style error-code checks.
        """
        last_exc = None
        for name in candidate_names:
            meth = getattr(client, name, None)
            if not callable(meth):
                continue
            try:
                resp = meth(*args, **kwargs)
            except RecursionError as re:
                # Protect from unbounded recursion originating inside SDK or user code
                last_exc = re
                continue
            except Exception as e:
                last_exc = e
                continue
            # Try to ensure the response can be represented safely
            try:
                _ = json.dumps(resp, default=str)
            except Exception:
                # fallback to a safe string summary
                try:
                    resp = str(resp)[:1000]
                except Exception:
                    resp = "unserializable_response"
            # Basic Bybit-style error checks
            try:
                if isinstance(resp, dict):
                    for rc in ("ret_code", "retCode", "error_code", "err_code"):
                        if rc in resp and resp.get(rc) not in (0, None, "0"):
                            last_exc = RuntimeError(f"API error from {name}: {resp.get(rc)}")
                            resp = None
                            break
                    if resp and resp.get("success") is False:
                        last_exc = RuntimeError(f"API reported failure from {name}")
                        resp = None
            except Exception:
                # don't fail hard here; continue to next candidate
                last_exc = RuntimeError("response structure check failed")
                resp = None
            if resp is not None:
                return resp
        if last_exc:
            raise last_exc
        raise RuntimeError("No candidate methods succeeded: " + ",".join(candidate_names))

    # ---- ticker/klines + price parsing (safe & depth-limited) ----
    def safe_get_ticker(self, client: HTTP, symbol: str) -> Any:
        candidates = ["ticker_price", "get_ticker", "get_symbol_ticker", "latest_information_for_symbol", "tickers", "get_tickers"]
        try:
            return self._try_methods(client, candidates, symbol)
        except Exception:
            # second attempt using params dict form
            return self._try_methods(client, candidates, params={"symbol": symbol})

    def safe_get_klines(self, client: HTTP, symbol: str, interval: str = "1", limit: int = 50) -> Any:
        candidates = ["query_kline", "get_kline", "get_klines", "query_candles", "get_candlesticks", "kline"]
        try:
            return self._try_methods(client, candidates, symbol, interval, limit)
        except Exception:
            return self._try_methods(client, candidates, params={"symbol": symbol, "interval": interval, "limit": limit})

    def _parse_price(self, raw: Any, depth: int = 0, visited: Optional[set] = None) -> Optional[float]:
        """
        Recursion-safe price parser.
        - visited: set of object ids to prevent revisiting circular structures
        - depth: recursion depth cap
        """
        if visited is None:
            visited = set()
        if depth > 8:
            return None
        try:
            # avoid revisiting same object
            try:
                rid = id(raw)
            except Exception:
                rid = None
            if rid is not None:
                if rid in visited:
                    return None
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
                # common fields
                for k in ("price", "last_price", "lastPrice", "close", "last", "avgPrice"):
                    if k in raw and raw[k] not in (None, ""):
                        try:
                            return float(raw[k])
                        except Exception:
                            pass
                # containers to descend into
                for key in ("result", "data", "list", "ticks", "rows", "items"):
                    if key in raw:
                        val = raw.get(key)
                        p = self._parse_price(val, depth + 1, visited)
                        if p is not None:
                            return p
                # fallback: try values
                for v in raw.values():
                    p = self._parse_price(v, depth + 1, visited)
                    if p is not None:
                        return p
            if isinstance(raw, (list, tuple)) and raw:
                # kline-like: [ts, open, high, low, close, ...]
                # check nested first element if it's a list
                first = raw[0]
                if isinstance(first, (list, tuple)) and len(first) >= 5:
                    try:
                        return float(first[4])
                    except Exception:
                        pass
                # if array directly looks like a kline
                if len(raw) >= 5 and all(isinstance(x, (int, float, str)) for x in raw[:5]):
                    try:
                        return float(raw[4])
                    except Exception:
                        pass
                # otherwise try items sequentially (depth-limited)
                for item in raw:
                    p = self._parse_price(item, depth + 1, visited)
                    if p is not None:
                        return p
            return None
        except RecursionError:
            return None
        except Exception:
            return None

    # ---- account validation ----
    def validate_account(self, account: Dict[str, Any]) -> Tuple[bool, Optional[float], str]:
        client = self._get_client(account)
        if not client:
            return False, None, "missing_api_credentials"
        balance_methods = ["get_wallet_balance", "query_wallet_balance", "get_balances", "wallet_balance", "balance"]
        try:
            resp = self._try_methods(client, balance_methods)
            self._capture_preview(account, resp)
            if isinstance(resp, dict):
                payload = None
                if "result" in resp and isinstance(resp["result"], dict):
                    payload = resp["result"]
                elif "data" in resp:
                    payload = resp["data"]
                else:
                    payload = resp
                # unified totals
                for k in ("total_equity", "equity", "totalBalance", "total_balance"):
                    if isinstance(payload, dict) and k in payload:
                        try:
                            return True, float(payload[k]), ""
                        except Exception:
                            pass
                # list of balances
                if isinstance(payload, list):
                    total = 0.0
                    for item in payload:
                        try:
                            coin = item.get("coin") or item.get("symbol")
                            free = item.get("free") or item.get("available_balance") or item.get("available") or item.get("walletBalance")
                            if free is None:
                                continue
                            freef = float(free)
                            if coin and str(coin).upper() in ("USDT", "USDC"):
                                total += freef
                        except Exception:
                            continue
                    if total > 0:
                        return True, total, ""
                # coin mapping
                if isinstance(payload, dict) and payload and all(isinstance(v, dict) for v in payload.values()):
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
                        return True, total, ""
            return False, None, "unrecognized_balance_shape"
        except Exception as e:
            self._capture_preview(account, str(e))
            return False, None, str(e)

    # ---- order helpers (dry-run gated) ----
    def _place_market_order(self, client: HTTP, symbol: str, side: str, qty: float, price_hint: Optional[float] = None) -> Dict[str, Any]:
        if TRADE_SETTINGS.get("dry_run", True):
            self.log(f"[DRY RUN] simulate market order: {side} {symbol} qty={qty}")
            executed_price = float(price_hint) if price_hint is not None else None
            return {"simulated": True, "symbol": symbol, "side": side, "qty": qty, "executed_price": executed_price, "time": now_iso()}
        candidates = ["place_active_order", "create_order", "order", "place_order"]
        try:
            try:
                resp = self._try_methods(client, candidates, symbol=symbol, side=side, order_type="Market", qty=qty)
            except Exception:
                resp = self._try_methods(client, candidates, symbol=symbol, side=side, qty=qty, order_type="Market")
            self._capture_preview({}, resp)
            return resp if isinstance(resp, dict) else {"result": resp}
        except Exception as e:
            self.log(f"Order placement error: {e}")
            return {"error": str(e)}

    # ---- trade recording / finalization ----
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

    # ---- trading logic: open & monitor ----
    def attempt_trade_for_account(self, acct: Dict[str, Any]):
        """Open a conservative Buy if monitoring and no open position."""
        if not acct.get("monitoring"):
            return
        if acct.get("position") == "open" or acct.get("open_trade_id"):
            return
        client = self._get_client(acct)
        if not client:
            self.log(f"No client for account {acct.get('name')}")
            return
        bal = acct.get("balance") or 0.0
        if bal < 10.0:
            self.log(f"Balance too low for trading for account {acct.get('name')}: {bal}")
            return
        symbol = acct.get("preferred_symbol") or ALLOWED_COINS[0]
        allocation_pct = TRADE_SETTINGS.get("trade_allocation_pct", 0.01)
        usd_alloc = bal * allocation_pct
        if usd_alloc < 1.0:
            self.log(f"Allocation too small ({usd_alloc}) — skipping")
            return
        try:
            tick = self.safe_get_ticker(client, symbol)
            price = self._parse_price(tick)
            if price is None:
                self.log(f"Could not determine price for {symbol} — aborting open")
                return
            qty = max((usd_alloc / price), 0.00000001)
            resp = self._place_market_order(client, symbol, "Buy", qty, price_hint=price)
            simulated = bool(resp.get("simulated")) if isinstance(resp, dict) else True
            entry_price = None
            if isinstance(resp, dict):
                for k in ("executed_price", "avgPrice", "price", "last_price"):
                    if k in resp and resp[k] is not None:
                        try:
                            entry_price = float(resp[k])
                            break
                        except Exception:
                            continue
            entry_price = entry_price if entry_price is not None else price
            ts = now_ts()
            tid = self._record_trade_entry(acct, symbol, qty, entry_price, ts, simulated)
            acct["position"] = "open"
            acct["current_symbol"] = symbol
            acct["entry_price"] = entry_price
            acct["entry_qty"] = qty
            acct["entry_time"] = ts
            acct["open_trade_id"] = tid
            acct["buy_price"] = entry_price
            self.log(f"Opened trade {tid} for {acct.get('name')} {symbol} qty={qty} price={entry_price} simulated={simulated}")
        except Exception as e:
            self.log(f"attempt_trade_for_account error: {e}")

    def _check_open_position(self, acct: Dict[str, Any]):
        """Check an open position and close on TP/SL/force-exit condition."""
        trade_id = acct.get("open_trade_id")
        if not acct.get("position") == "open" or not trade_id:
            return
        client = self._get_client(acct)
        if not client:
            self.log(f"No client to check position for {acct.get('name')}")
            return
        symbol = acct.get("current_symbol")
        entry_price = acct.get("entry_price")
        qty = acct.get("entry_qty")
        entry_ts = acct.get("entry_time") or now_ts()
        try:
            tick = self.safe_get_ticker(client, symbol)
            price = self._parse_price(tick)
            if price is None:
                self.log(f"Could not parse current price for {symbol} — skipping checks")
                return
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
                    for k in ("executed_price", "avgPrice", "price", "last_price"):
                        if k in resp and resp[k] is not None:
                            try:
                                exit_price = float(resp[k])
                                break
                            except Exception:
                                continue
                exit_price = exit_price if exit_price is not None else price
                exit_ts = now_ts()
                try:
                    final_profit = ((float(exit_price) - float(entry_price)) / float(entry_price)) * 100.0
                except Exception:
                    final_profit = profit_pct
                resp_summary = None
                try:
                    resp_summary = str(resp)[:400]
                except Exception:
                    resp_summary = None
                self._finalize_trade(trade_id, exit_price, exit_ts, label, resp_summary, simulated)
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

    # ---- main scanner & run loop ----
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
                acct["last_balance"] = acct.get("balance", acct.get("last_balance"))
                acct["last_validation_error"] = acct.get("last_validation_error")
            except Exception as e:
                self.log(f"Account scan error for {acct.get('id')}: {e}")
            updated = True
        if updated:
            self.save_accounts(accounts)

    def start(self):
        if self._running:
            return
        self._stop_event.clear()
        self._running = True
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
            for _ in range(interval):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

# CLI helper for local debug
if __name__ == "__main__":
    bc = BotController()
    print("Superb Crypto Bot — debug run (dry_run defaults to True)")
    accts = bc.load_accounts()
    if not accts:
        print("No accounts found. Add accounts to accounts.json with api_key and api_secret to test validation.")
    else:
        for a in accts:
            ok, bal, err = bc.validate_account(a)
            print(f"Account id={a.get('id')} ok={ok} balance={bal} err={err}")
    bc._scan_once()
    print("Done")
