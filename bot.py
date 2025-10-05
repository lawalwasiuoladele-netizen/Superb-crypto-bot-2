"""
Superb Crypto Bot — full updated version with TP timing and elapsed tracking

Features:
 - TP timeline: TP1 (0-10m -> +7%), TP2 (10-18m -> +4%), TP3 (18-23m -> +1%)
 - Stop-loss: -3%
 - Force-exit after 23 minutes
 - Records trades to trades.json with elapsed time (MMm SSs), profit %, TP label, simulated flag
 - Dry-run gating (TRADE_SETTINGS['dry_run']) prevents live orders by default
 - test_on_testnet default False (production endpoint) — dry_run True by default
 - Thread-safe file writes and consistent account fields
 - Safe ticker/klines with candidate fallbacks and response parsing
 - last_raw_preview capture when debug enabled (no secrets included)
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# pybit unified_trading HTTP client
from pybit.unified_trading import HTTP

# -------------------- CONFIG --------------------
ALLOWED_COINS = [
    "ADAUSDT","XRPUSDT","TRXUSDT","DOGEUSDT","CHZUSDT","VETUSDT","BTTUSDT","HOTUSDT","XLMUSDT","ZILUSDT",
    "IOTAUSDT","SCUSDT","DENTUSDT","KEYUSDT","WINUSDT","CVCUSDT","MTLUSDT","CELRUSDT","FUNUSDT","STMXUSDT",
    "REEFUSDT","ANKRUSDT","ONEUSDT","OGNUSDT","CTSIUSDT","DGBUSDT","CKBUSDT","ARPAUSDT","MBLUSDT","TROYUSDT",
    "PERLUSDT","DOCKUSDT","RENUSDT","COTIUSDT","MDTUSDT","OXTUSDT","PHAUSDT","BANDUSDT","GTOUSDT","LOOMUSDT",
    "PONDUSDT","FETUSDT","SYSUSDT","TLMUSDT","NKNUSDT","LINAUSDT","ORNUSDT","COSUSDT","FLMUSDT","ALICEUSDT"
]

# TP durations/targets and stop/force rules
RISK_RULES = {
    "stop_loss": -3.0,                 # -3% stop loss
    "tp1": (600, 7.0),                 # up to 10 minutes -> 7%
    "tp2": (1080, 4.0),                # up to 18 minutes -> 4%
    "tp3": (1380, 1.0),                # up to 23 minutes -> 1%
    "max_hold": 1380,                  # force exit at 23 minutes
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
    "test_on_testnet": False,       # production endpoint by default (use dry_run for safety)
    "scan_interval": 10,
    "debug_raw_responses": False,
    "dry_run": True                 # default safe: simulated orders only
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

    def add_trade(self, trade: Dict[str, Any]):
        with self._file_lock:
            trades = self._read_trades()
            trades.append(trade)
            self._write_trades(trades)

    def update_trade(self, trade_id: str, updates: Dict[str, Any]) -> bool:
        """Update a trade in-place by id. Returns True if updated."""
        with self._file_lock:
            trades = self._read_trades()
            changed = False
            for t in trades:
                if t.get("id") == trade_id:
                    t.update(updates)
                    changed = True
                    break
            if changed:
                self._write_trades(trades)
            return changed

    # ---- Bybit client helpers ----
    def _get_client(self, account: Dict[str, Any]) -> Optional[HTTP]:
        """Create a pybit client. Requires api_key and api_secret present in account."""
        try:
            key = account.get("api_key")
            secret = account.get("api_secret")
            if not key or not secret:
                return None
            # pybit unified_trading.HTTP will use testnet flag, but also allow endpoint override if needed
            client = HTTP(api_key=key, api_secret=secret, testnet=TRADE_SETTINGS.get("test_on_testnet", False))
            return client
        except Exception as e:
            self.log(f"_get_client error: {e}")
            return None

    def _capture_preview(self, account: Dict[str, Any], resp: Any):
        """Keep tiny preview of raw responses for debugging (no secrets)."""
        try:
            if not TRADE_SETTINGS.get("debug_raw_responses", False):
                return
            try:
                preview = json.dumps(resp)[:300]
            except Exception:
                preview = str(type(resp))
            account.setdefault("last_raw_preview", {})
            key = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            account["last_raw_preview"][key] = preview
            # keep only last 3 previews
            keys = list(account["last_raw_preview"].keys())
            if len(keys) > 3:
                for k in keys[:-3]:
                    account["last_raw_preview"].pop(k, None)
        except Exception:
            pass

    # ---- robust SDK method caller ----
    def _try_methods(self, client: HTTP, candidate_names: List[str], *args, **kwargs):
        """Try SDK method names and return first non-error response."""
        last_exc = None
        for name in candidate_names:
            try:
                meth = getattr(client, name, None)
                if not meth:
                    continue
                resp = meth(*args, **kwargs)
                if resp is None:
                    continue
                # basic normalization check for error codes or success flags
                if isinstance(resp, dict):
                    for rc in ("ret_code", "retCode", "error_code", "err_code"):
                        if rc in resp and resp.get(rc) not in (0, None, "0"):
                            raise RuntimeError(f"API error from {name}: {resp.get(rc)}")
                    if "success" in resp and resp["success"] is False:
                        raise RuntimeError(f"API reported failure from {name}")
                return resp
            except Exception as e:
                last_exc = e
                continue
        if last_exc:
            raise last_exc
        raise RuntimeError("No candidate methods succeeded: " + ",".join(candidate_names))

    # ---- tickers/klines + price parsing ----
    def safe_get_ticker(self, client: HTTP, symbol: str) -> Any:
        candidates = ["ticker_price", "get_ticker", "get_symbol_ticker", "latest_information_for_symbol", "tickers", "symbols", "get_tickers"]
        try:
            return self._try_methods(client, candidates, symbol)
        except Exception:
            # some SDKs accept kwargs
            return self._try_methods(client, candidates, params={"symbol": symbol})

    def safe_get_klines(self, client: HTTP, symbol: str, interval: str = "1", limit: int = 50) -> Any:
        candidates = ["query_kline", "get_kline", "get_klines", "query_candles", "get_candlesticks", "kline"]
        try:
            return self._try_methods(client, candidates, symbol, interval, limit)
        except Exception:
            return self._try_methods(client, candidates, params={"symbol": symbol, "interval": interval, "limit": limit})

    def _parse_price(self, raw: Any) -> Optional[float]:
        """Extract a sensible numeric price from common response shapes."""
        try:
            if raw is None:
                return None
            # if direct numeric or string numeric
            if isinstance(raw, (int, float)):
                return float(raw)
            if isinstance(raw, str):
                try:
                    return float(raw)
                except Exception:
                    return None
            # dict shapes
            if isinstance(raw, dict):
                # common top-level fields
                for k in ("price", "last_price", "lastPrice", "last", "close", "p"):
                    if k in raw:
                        try:
                            return float(raw[k])
                        except Exception:
                            continue
                # nested 'result' or 'data'
                for container in ("result", "data"):
                    v = raw.get(container)
                    if v is None:
                        continue
                    # if result is dict with 'last_price' etc.
                    if isinstance(v, dict):
                        for k in ("price", "last_price", "lastPrice", "last", "close"):
                            if k in v:
                                try:
                                    return float(v[k])
                                except Exception:
                                    continue
                        # sometimes result contains 'list' or 'ticks'
                        for subk in ("list", "ticks", "rows", "data"):
                            s = v.get(subk)
                            if isinstance(s, list) and s:
                                # try first item
                                item = s[0]
                                if isinstance(item, (dict, list, tuple)):
                                    p = self._parse_price(item)
                                    if p is not None:
                                        return p
                    # if result is a list
                    if isinstance(v, list) and v:
                        item = v[0]
                        p = self._parse_price(item)
                        if p is not None:
                            return p
                # sometimes tickers return a list under 'result' directly
            # list/tuple shapes
            if isinstance(raw, (list, tuple)) and raw:
                # kline-like: [open_time, open, high, low, close, volume]
                first = raw[0]
                if isinstance(first, (int, float, str)):
                    # maybe closing price at index 4
                    if len(raw) >= 5:
                        try:
                            return float(raw[4])
                        except Exception:
                            pass
                    # fallback: try first element numeric
                    try:
                        return float(first)
                    except Exception:
                        pass
                # if list of dicts
                if isinstance(first, dict):
                    p = self._parse_price(first)
                    if p is not None:
                        return p
            # dict-like in list
            return None
        except Exception:
            return None

    # ---- account validation with multi-step balance parsing ----
    def validate_account(self, account: Dict[str, Any]) -> Tuple[bool, Optional[float], str]:
        client = self._get_client(account)
        if not client:
            return False, None, "missing_api_credentials"
        # candidate balance methods
        balance_methods = ["get_wallet_balance", "query_wallet_balance", "get_balances", "wallet_balance", "balance"]
        try:
            resp = self._try_methods(client, balance_methods)
            # capture small preview for debugging
            self._capture_preview(account, resp)
            # try common shapes
            if isinstance(resp, dict):
                payload = None
                if "result" in resp and isinstance(resp["result"], dict):
                    payload = resp["result"]
                elif "data" in resp:
                    payload = resp["data"]
                else:
                    payload = resp
                # unified total equity
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
                            else:
                                # best-effort: skip non-stable valuation
                                total += 0.0
                        except Exception:
                            continue
                    if total > 0:
                        return True, total, ""
                # coin-array: {'BTC': {'available': '0.1', ...}, ...}
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
        """Place a market order. If dry_run or EMERGENCY_STOP, simulate and return a simulated response."""
        if RISK_RULES.get("exit_on_any_positive") and side not in ("Buy", "Sell"):
            pass  # placeholder for policies
        if TRADE_SETTINGS.get("dry_run", True):
            self.log(f"[DRY RUN] market order simulated: {side} {symbol} qty={qty}")
            # simulated executed price uses price_hint if provided
            executed_price = float(price_hint) if price_hint else None
            resp = {"simulated": True, "symbol": symbol, "side": side, "qty": qty, "executed_price": executed_price, "time": now_iso()}
            return resp
        # Real order attempt via common SDK methods
        candidates = ["place_active_order", "create_order", "order", "place_order"]
        try:
            # Note: SDKs have varying param names; we try a few shapes
            try:
                resp = self._try_methods(client, candidates, symbol=symbol, side=side, order_type="Market", qty=qty)
            except Exception:
                resp = self._try_methods(client, candidates, symbol=symbol, side=side, qty=qty, order_type="Market")
            # capture preview but avoid storing secrets
            self._capture_preview({}, resp)
            return resp if isinstance(resp, dict) else {"result": resp}
        except Exception as e:
            self.log(f"Order placement error: {e}")
            return {"error": str(e)}

    # ---- trade lifecycle helpers ----
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
        # compute profit% and elapsed
        # find original entry by id
        trades = self._read_trades()
        entry = None
        for t in trades:
            if t.get("id") == trade_id:
                entry = t
                break
        if not entry:
            # fallback: create a closed trade record
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
            entry_ts = exit_ts
        elapsed_s = max(0, exit_ts - entry_ts)
        profit_pct = None
        if entry_price and exit_price:
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
        # write update
        self.update_trade(trade_id, updates)

    # ---- simple (safe) trade attempt and open/close logic ----
    def attempt_trade_for_account(self, acct: Dict[str, Any]):
        """Conservative trade attempt (demonstration). Opens a Buy spot position if monitoring is enabled
        and no open position exists for the account."""
        if not acct.get("monitoring"):
            return
        if acct.get("position") == "open" or acct.get("open_trade_id"):
            return  # already open
        client = self._get_client(acct)
        if not client:
            self.log(f"No client for account {acct.get('name')}")
            return
        bal = acct.get("balance") or 0.0
        if bal < 10.0:
            self.log(f"Balance too low for trading for account {acct.get('name')}: {bal}")
            return
        # choose symbol (simple selection: first allowed)
        symbol = acct.get("preferred_symbol") or ALLOWED_COINS[0]
        # compute USD allocation
        allocation_pct = TRADE_SETTINGS.get("trade_allocation_pct", 0.01)
        usd_alloc = bal * allocation_pct
        if usd_alloc < 1.0:
            self.log(f"Computed allocation too small ({usd_alloc}) — skipping")
            return
        # fetch price
        try:
            tick = self.safe_get_ticker(client, symbol)
            price = self._parse_price(tick)
            if price is None:
                self.log(f"Could not fetch price for {symbol} — aborting open")
                return
            # compute qty = usd_alloc / price (spot)
            qty = max((usd_alloc / price), 0.00000001)
            # place order (market)
            resp = self._place_market_order(client, symbol, "Buy", qty, price_hint=price)
            simulated = bool(resp.get("simulated")) if isinstance(resp, dict) else True
            # define actual entry_price: prefer executed price in resp, fallback to price
            entry_price = None
            if isinstance(resp, dict):
                for k in ("executed_price", "avgPrice", "price", "last_price"):
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
            # update account state
            acct["position"] = "open"
            acct["current_symbol"] = symbol
            acct["entry_price"] = entry_price
            acct["entry_qty"] = qty
            acct["entry_time"] = ts
            acct["open_trade_id"] = trade_id
            acct["buy_price"] = entry_price
            self.log(f"Opened trade for {acct.get('name')} {symbol} qty={qty} price={entry_price} trade_id={trade_id} simulated={simulated}")
        except Exception as e:
            self.log(f"attempt_trade_for_account error: {e}")

    def _check_open_position(self, acct: Dict[str, Any]):
        """Check an open position and close on TP/SL/force-exit/any-positive (config)."""
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
        # get current price
        try:
            tick = self.safe_get_ticker(client, symbol)
            price = self._parse_price(tick)
            if price is None:
                self.log(f"Could not parse current price for {symbol} — skipping checks")
                return
            # compute profit % (Buy)
            profit_pct = None
            try:
                profit_pct = ((float(price) - float(entry_price)) / float(entry_price)) * 100.0
            except Exception:
                profit_pct = None
            elapsed_s = max(0, now_ts() - int(entry_ts))
            label = None
            should_close = False
            # Check stop loss
            if profit_pct is not None and profit_pct <= RISK_RULES.get("stop_loss", -9999):
                label = "stop_loss"
                should_close = True
            # exit if any positive profit and configured
            elif RISK_RULES.get("exit_on_any_positive", False) and profit_pct is not None and profit_pct > 0:
                label = "profit_any"
                should_close = True
            else:
                # determine active TP target by elapsed
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
                # if not forced_exit, check TP reached
                if not should_close and profit_pct is not None:
                    if profit_pct >= target_pct:
                        should_close = True
            # if should_close is true, execute close and finalize trade
            if should_close:
                resp = self._place_market_order(client, symbol, "Sell", qty, price_hint=price)
                simulated = bool(resp.get("simulated")) if isinstance(resp, dict) else True
                exit_price = None
                if isinstance(resp, dict):
                    for k in ("executed_price", "avgPrice", "price", "last_price"):
                        if k in resp and resp[k]:
                            try:
                                exit_price = float(resp[k])
                                break
                            except Exception:
                                continue
                if exit_price is None:
                    exit_price = price
                exit_ts = now_ts()
                # compute final profit pct using exit_price if available
                try:
                    final_profit = ((float(exit_price) - float(entry_price)) / float(entry_price)) * 100.0
                except Exception:
                    final_profit = profit_pct
                # finalize trade record
                resp_summary = None
                try:
                    resp_summary = str(resp)[:400]
                except Exception:
                    resp_summary = None
                self._finalize_trade(trade_id, exit_price, exit_ts, label, resp_summary, simulated)
                # update account state (close)
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

    # ---- main loop ----
    def _scan_once(self):
        accounts = self.load_accounts()
        updated = False
        for acct in accounts:
            acct.setdefault("id", str(uuid.uuid4()))
            # validate account balances (best-effort)
            try:
                ok, bal, err = self.validate_account(acct)
                acct["validated"] = ok
                acct["balance"] = bal
                acct["last_validation_error"] = err
            except Exception as e:
                acct["validated"] = False
                acct["balance"] = None
                acct["last_validation_error"] = str(e)
            # ensure monitoring/position fields exist
            acct.setdefault("position", acct.get("position", "closed"))
            acct.setdefault("monitoring", acct.get("monitoring", False))
            acct.setdefault("current_symbol", acct.get("current_symbol"))
            acct.setdefault("buy_price", acct.get("buy_price"))
            # If there's an open position, check TP/SL/force-exit
            try:
                if acct.get("position") == "open" and acct.get("open_trade_id"):
                    self._check_open_position(acct)
                else:
                    # attempt new trade if monitoring enabled
                    try:
                        self.attempt_trade_for_account(acct)
                    except Exception as e:
                        self.log(f"attempt_trade_for_account raised: {e}")
                # surface last_balance and last_error for the server UI
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
            # sleep with small granularity to allow fast stop
            interval = int(TRADE_SETTINGS.get("scan_interval", 10))
            for _ in range(interval):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

# Minimal CLI for debugging
if __name__ == "__main__":
    bc = BotController()
    print("Superb Crypto Bot — debug run (no live orders by default; dry_run=True)")
    accts = bc.load_accounts()
    if not accts:
        print("No accounts found. Add accounts to accounts.json with api_key and api_secret to test validation.")
    else:
        for a in accts:
            ok, bal, err = bc.validate_account(a)
            print(f"Account id={a.get('id')} ok={ok} balance={bal} err={err}")
    # single loop example
    bc._scan_once()
    print("Done")
