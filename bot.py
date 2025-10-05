"""
Superb Crypto Bot - robust bot.py

Drop in place of your existing bot.py. This file:
 - uses queue.Queue for logs
 - defensive pybit import + parsing
 - TP timing is preserved (tp1/tp2/tp3 in seconds)
 - default to MAINNET (TRADE_SETTINGS["test_on_testnet"] = False)
 - added exit_on_positive_profit (set True to exit on any >0% profit)
 - validate_account() returns debug info suitable for server-side display
 - safe file writes and last_error stored on accounts
"""

import json
import os
import threading
import time
import uuid
import math
import traceback
from datetime import datetime
from typing import List, Dict, Any, Optional
import queue

# Attempt pybit import (defensive)
try:
    from pybit.unified_trading import HTTP as PYBIT_HTTP
except Exception:
    PYBIT_HTTP = None

# ---------------------- CONFIG ----------------------
ALLOWED_COINS = [
    "ADAUSDT","XRPUSDT","TRXUSDT","DOGEUSDT","CHZUSDT","VETUSDT","BTTUSDT","HOTUSDT","XLMUSDT","ZILUSDT",
    "IOTAUSDT","SCUSDT","DENTUSDT","KEYUSDT","WINUSDT","CVCUSDT","MTLUSDT","CELRUSDT","FUNUSDT","STMXUSDT",
    "REEFUSDT","ANKRUSDT","ONEUSDT","OGNUSDT","CTSIUSDT","DGBUSDT","CKBUSDT","ARPAUSDT","MBLUSDT","TROYUSDT",
    "PERLUSDT","DOCKUSDT","RENUSDT","COTIUSDT","MDTUSDT","OXTUSDT","PHAUSDT","BANDUSDT","GTOUSDT","LOOMUSDT",
    "PONDUSDT","FETUSDT","SYSUSDT","TLMUSDT","NKNUSDT","LINAUSDT","ORNUSDT","COSUSDT","FLMUSDT","ALICEUSDT"
]

# Risk / TP settings (timings in seconds)
RISK_RULES = {
    "stop_loss": -3.0,           # -3% stop loss
    "tp1": (600, 7.0),           # within 10 minutes -> take 7% profit
    "tp2": (1020, 4.0),          # within 17 minutes -> take 4%
    "tp3": (1200, 1.0),          # within 20 minutes -> take 1%
    "max_hold": 1320             # force exit at 22 minutes
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
    # fraction of account equity to allocate per trade
    "trade_allocation_pct": 0.01,
    # market vs limit
    "use_market_order": True,
    # default to MAINNET as requested
    "test_on_testnet": False,
    # seconds between scans when idle
    "scan_interval": 10,
    # immediate exit if profit > 0 (set True to exit on any positive profit)
    "exit_on_positive_profit": True,
    # number of decimals for quantity rounding (adjust by exchange)
    "qty_precision": 6
}

ACCOUNTS_FILE = "accounts.json"
TRADES_FILE = "trades.json"

# ---------------------- BotController ----------------------
class BotController:
    def __init__(self, log_queue: Optional[queue.Queue] = None):
        # if no log_queue provided, create one (useful for server SSE)
        self.log_queue = log_queue or queue.Queue()
        self._running = False
        self._stop_event = threading.Event()
        self._file_lock = threading.Lock()
        self._threads: List[threading.Thread] = []

        # ensure storage files exist
        if not os.path.exists(ACCOUNTS_FILE):
            with open(ACCOUNTS_FILE, "w") as f:
                json.dump([], f)
        if not os.path.exists(TRADES_FILE):
            with open(TRADES_FILE, "w") as f:
                json.dump([], f)

    # ---------- Logging ----------
    def log(self, msg: str):
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        try:
            # print to stdout (Render/Heroku logs)
            print(line, flush=True)
        except Exception:
            pass
        # put to queue non-blocking
        if self.log_queue:
            try:
                self.log_queue.put_nowait(line)
            except Exception:
                pass

    # ---------- File helpers ----------
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

    # ---------- Pybit session ----------
    def _make_session(self, acc: Dict[str, Any]):
        if PYBIT_HTTP is None:
            raise RuntimeError("pybit.unified_trading.HTTP not available. Install pybit.")
        # account-level override allowed
        testnet_flag = bool(acc.get("test_on_testnet", TRADE_SETTINGS.get("test_on_testnet", False)))
        api_key = acc.get("key")
        api_secret = acc.get("secret")
        return PYBIT_HTTP(testnet=testnet_flag, api_key=api_key, api_secret=api_secret)

    # ---------- Defensive parsers ----------
    def _safe_repr(self, obj: Any) -> str:
        try:
            return json.dumps(obj, default=str, indent=2)[:2000]
        except Exception:
            try:
                return repr(obj)[:2000]
            except Exception:
                return "<unserializable>"

    def _parse_last_price_from_tickers(self, resp: Any, symbol: str) -> Optional[float]:
        try:
            if not resp:
                return None
            r = resp.get("result") if isinstance(resp, dict) else None
            # common shapes: {"result": {"list":[{"lastPrice": "..."}]}} or {"result":[{...}]}
            if isinstance(r, dict):
                lst = r.get("list") or r.get("tickers") or []
                if lst and isinstance(lst, list):
                    first = lst[0]
                    price = first.get("lastPrice") or first.get("price") or first.get("close")
                    if price is not None:
                        return float(price)
            if isinstance(r, list) and r:
                first = r[0]
                price = first.get("lastPrice") or first.get("price")
                if price is not None:
                    return float(price)
            # fallback: top-level fields
            if isinstance(resp, dict):
                price = resp.get("lastPrice") or resp.get("price")
                if price is not None:
                    return float(price)
        except Exception:
            return None
        return None

    def get_ticker_last(self, session, symbol: str) -> Optional[float]:
        try:
            resp = session.get_tickers(category="spot", symbol=symbol)
            price = self._parse_last_price_from_tickers(resp, symbol)
            if price is None:
                raise RuntimeError(f"failed to parse price for {symbol} from response")
            return price
        except Exception as e:
            self.log(f"get_ticker_last error for {symbol}: {e}")
            return None

    def get_klines(self, session, symbol: str, interval: str = "1", limit: int = 50) -> List[Dict[str, Any]]:
        try:
            resp = session.get_kline(category="spot", symbol=symbol, interval=str(interval), limit=limit)
            result = resp.get("result") if isinstance(resp, dict) else resp
            candles = []
            if isinstance(result, dict):
                candles = result.get("list") or result.get("rows") or []
            elif isinstance(result, list):
                candles = result
            if not candles:
                return []
            # return in chronological order (old->new)
            return list(reversed(candles))
        except Exception as e:
            self.log(f"get_klines error for {symbol}: {e}")
            return []

    def get_balance(self, session, acc: Dict[str, Any], retries: int = 2) -> Optional[float]:
        last_err = None
        for attempt in range(retries):
            try:
                # Try unified first
                try:
                    resp = session.get_wallet_balance(accountType="UNIFIED")
                    result = resp.get("result") if isinstance(resp, dict) else None
                    if isinstance(result, dict) and result.get("list"):
                        first = result["list"][0]
                        total = float(first.get("totalEquity") or first.get("equity") or 0.0)
                        acc["last_balance"] = total
                        self._update_account(acc)
                        return total
                    if isinstance(result, list) and result:
                        first = result[0]
                        total = float(first.get("totalEquity") or first.get("equity") or 0.0)
                        acc["last_balance"] = total
                        self._update_account(acc)
                        return total
                except Exception as e_unified:
                    last_err = f"UNIFIED error: {e_unified}"
                    # try SPOT fallback
                    try:
                        resp2 = session.get_wallet_balance(accountType="SPOT")
                        res2 = resp2.get("result") if isinstance(resp2, dict) else resp2
                        total = 0.0
                        # res2 may contain list of balances
                        if isinstance(res2, dict):
                            parts = res2.get("list") or res2.get("balances") or []
                            if isinstance(parts, list) and parts:
                                for p in parts:
                                    try:
                                        total += float(p.get("equity") or p.get("available") or p.get("balance") or 0.0)
                                    except Exception:
                                        continue
                            else:
                                total = float(res2.get("totalEquity", 0.0) or res2.get("total", 0.0))
                        elif isinstance(res2, list):
                            for p in res2:
                                try:
                                    total += float(p.get("equity") or p.get("available") or p.get("balance") or 0.0)
                                except Exception:
                                    continue
                        acc["last_balance"] = total
                        self._update_account(acc)
                        return total
                    except Exception as e_spot:
                        last_err = f"SPOT error: {e_spot}"
                        self.log(f"get_balance fallback failed: {last_err}")
                        time.sleep(0.25)
                        continue
            except Exception as e:
                last_err = str(e)
                self.log(f"get_balance unexpected error: {last_err}")
                time.sleep(0.25)
        acc["last_error"] = last_err
        self._update_account(acc)
        return None

    def _update_account(self, acc: Dict[str, Any]):
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

    # ---------- Indicators ----------
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

    # ---------- Entry / Scoring ----------
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
            self.log(f"Error in check_entry_condition {symbol}: {e}")
            return False

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

    # ---------- Exit ----------
    def check_exit_condition(self, acc: Dict[str, Any], current_price: float):
        try:
            entry_price = float(acc.get("buy_price", 0.0) or 0.0)
            entry_time = acc.get("entry_time", time.time())
            if entry_price <= 0:
                self.log(f"Invalid entry price for {acc.get('current_symbol')}, skip exit check.")
                return False, 0.0, 0

            profit_pct = (current_price - entry_price) / (entry_price if entry_price != 0 else 0.000001) * 100
            elapsed = time.time() - entry_time

            # Optionally exit on any positive profit if configured
            if TRADE_SETTINGS.get("exit_on_positive_profit", False) and profit_pct > 0:
                return True, profit_pct, elapsed

            # stop loss
            if profit_pct <= RISK_RULES["stop_loss"]:
                return True, profit_pct, elapsed
            # take profit tiers
            if elapsed <= RISK_RULES["tp1"][0] and profit_pct >= RISK_RULES["tp1"][1]:
                return True, profit_pct, elapsed
            if elapsed <= RISK_RULES["tp2"][0] and profit_pct >= RISK_RULES["tp2"][1]:
                return True, profit_pct, elapsed
            if elapsed <= RISK_RULES["tp3"][0] and profit_pct >= RISK_RULES["tp3"][1]:
                return True, profit_pct, elapsed
            if elapsed >= RISK_RULES["max_hold"]:
                return True, profit_pct, elapsed

            return False, profit_pct, elapsed
        except Exception as e:
            self.log(f"Error in check_exit_condition {acc.get('current_symbol')}: {e}")
            return False, 0.0, 0

    # ---------- Orders ----------
    def place_market_buy(self, session, symbol: str, usdt_amount: float):
        price = self.get_ticker_last(session, symbol)
        if price is None or price == 0:
            raise RuntimeError("Invalid price for market buy.")
        qty = usdt_amount / price
        precision = int(TRADE_SETTINGS.get("qty_precision", 6))
        qty = float(round(qty, precision))
        params = {
            "category": "spot",
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Market" if TRADE_SETTINGS.get("use_market_order", True) else "Limit",
            "qty": str(qty)
        }
        if not TRADE_SETTINGS.get("use_market_order", True):
            params["price"] = str(price)
        res = session.place_spot_order(**params)
        return {"price": price, "qty": qty, "resp": res}

    def place_market_sell(self, session, symbol: str, qty: float):
        params = {
            "category": "spot",
            "symbol": symbol,
            "side": "Sell",
            "orderType": "Market",
            "qty": str(qty)
        }
        res = session.place_spot_order(**params)
        return res

    # ---------- Per-account runner ----------
    def run_account(self, acc: Dict[str, Any]):
        # ensure defaults exist
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
            self.log(f"Failed creating session for account ****{(acc.get('key') or '')[-4:]}: {e}")
            acc["last_error"] = str(e)
            self._update_account(acc)
            return

        last_balance_fetch = 0.0
        scan_interval = TRADE_SETTINGS.get("scan_interval", 10)

        while not self._stop_event.is_set():
            try:
                # refresh balance periodically
                if time.time() - last_balance_fetch > 60:
                    self.get_balance(session, acc)
                    last_balance_fetch = time.time()

                # monitoring an open position
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

                    # periodic log (minute aligned)
                    if int(elapsed) % 60 == 0:
                        self.log(f"Account ****{(acc.get('key') or '')[-4:]} | Monitoring {symbol} | elapsed={int(elapsed)}s | profit={profit_pct:.2f}% | price={price}")

                    if should_exit:
                        self.log(f"Exiting {symbol} for account ****{(acc.get('key') or '')[-4:]} | profit={profit_pct:.2f}% | elapsed={int(elapsed)}s")
                        qty = acc.get("qty")
                        if qty is None and acc.get("allocated_usdt") and acc.get("buy_price"):
                            qty = float(round(acc.get("allocated_usdt") / float(acc.get("buy_price")), TRADE_SETTINGS.get("qty_precision", 6)))
                        try:
                            if qty:
                                sell_resp = self.place_market_sell(session, symbol, qty)
                                self.log(f"Sell resp: {self._safe_repr(sell_resp)}")
                            else:
                                self.log("No qty known for sell, skipping sell API call (manual intervention needed).")
                        except Exception as e:
                            self.log(f"Sell order failed for {symbol}: {e}")

                        trade = {
                            "id": str(uuid.uuid4()),
                            "account": acc.get("name", (acc.get("key") or "")[:6]),
                            "account_key_tail": (acc.get("key') or "")[-6:] if acc.get("key") else "",
                            "symbol": symbol,
                            "entry_price": acc.get("buy_price"),
                            "exit_price": price,
                            "profit_pct": profit_pct,
                            "entry_time": acc.get("last_trade_start"),
                            "exit_time": datetime.utcnow().isoformat(),
                            "hold_seconds": int(elapsed)
                        }
                        self.add_trade(trade)

                        # clear trade fields
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

                # scan for entry when not in position
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
                    self.log(f"Account ****{(acc.get('key') or '')[-4:]} | Best candidate: {best_symbol} score={best_score:.3f}")
                    candidate_ok = self.check_entry_condition(best_symbol, best_meta["closes"], best_meta["price"], best_meta["volumes"])
                    if candidate_ok:
                        equity = acc.get("last_balance")
                        if equity is None:
                            equity = self.get_balance(session, acc) or 0.0
                        allocated = equity * TRADE_SETTINGS.get("trade_allocation_pct", 0.01)
                        if allocated <= 0 or equity <= 0:
                            self.log("Allocated amount <= 0 or balance missing, skipping trade.")
                        else:
                            try:
                                self.log(f"Placing buy for {best_symbol} using ${allocated:.4f} allocation.")
                                buy_resp = self.place_market_buy(session, best_symbol, allocated)
                                buy_price = buy_resp.get("price", best_meta["price"])
                                qty = buy_resp.get("qty", None)
                                self.log(f"Buy resp price={buy_price}, qty={qty}, raw={self._safe_repr(buy_resp.get('resp'))}")
                                # set position
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
                                entry_trade = {
                                    "id": str(uuid.uuid4()),
                                    "account": acc.get("name", (acc.get("key") or "")[:6]),
                                    "account_key_tail": (acc.get("key") or "")[-6:],
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

                # sleep with stop-check
                for _ in range(int(scan_interval)):
                    if self._stop_event.is_set():
                        break
                    time.sleep(1)

            except Exception as e:
                self.log(f"Unexpected error in run_account for ****{(acc.get('key') or '')[-4:]}: {e}")
                self.log(traceback.format_exc())
                time.sleep(2)

        self.log(f"Exiting run loop for account ****{(acc.get('key') or '')[-4:]}")

    # ---------- Control ----------
    def start(self):
        accounts = self.load_accounts()
        if not accounts:
            self.log("⚠️ No accounts configured in accounts.json.")
            return

        self._stop_event.clear()
        self._running = True

        for acc in accounts:
            acc.setdefault("position", "closed")
            acc.setdefault("monitoring", False)
            try:
                # quick connectivity test but do not fail start if it errors
                try:
                    _ = self._make_session(acc)
                except Exception as e:
                    self.log(f"Quick connectivity test warning for ****{(acc.get('key') or '')[-4:]}: {e}")
                t = threading.Thread(target=self.run_account, args=(acc,), daemon=True)
                t.start()
                self._threads.append(t)
                self.log(f"✅ Bot started for account ****{(acc.get('key') or '')[-4:]}")
            except Exception as e:
                self.log(f"❌ Failed to start account ****{(acc.get('key') or '')[-4:]}: {e}")

        self.log("Bot startup attempt complete.")

    def stop(self, wait_seconds: float = 3.0):
        self._stop_event.set()
        self._running = False
        self.log("Stop signal set. Threads will exit shortly.")
        # attempt to join threads briefly
        for t in list(self._threads):
            try:
                t.join(timeout=wait_seconds)
            except Exception:
                pass
        self._threads = []

    def is_running(self) -> bool:
        return self._running

    # ---------- Validation helper (useful for server debug) ----------
    def validate_account(self, acc: Dict[str, Any], sample_symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Return a debug dict: {ok: bool, balance: float|None, errors: [...], sample_ticker: ..., sample_kline: ...}
        This is safe to call from server and display in the debug panel (do NOT print secrets).
        """
        debug = {"ok": False, "balance": None, "errors": [], "sample_ticker": None, "sample_kline": None}

        if PYBIT_HTTP is None:
            debug["errors"].append("pybit.unified_trading.HTTP not installed/available")
            return debug

        try:
            session = self._make_session(acc)
        except Exception as e:
            debug["errors"].append(f"session creation failed: {e}")
            return debug

        # try balance
        try:
            bal = self.get_balance(session, acc, retries=1)
            debug["balance"] = bal
        except Exception as e:
            debug["errors"].append(f"balance check failed: {e}")

        # sample ticker(s)
        try:
            syms = sample_symbols or (ALLOWED_COINS[:3] if ALLOWED_COINS else [])
            t_sample = {}
            for s in syms:
                try:
                    resp = session.get_tickers(category="spot", symbol=s)
                    t_sample[s] = {"parsed_price": self._parse_last_price_from_tickers(resp, s), "raw": str(resp)[:1000]}
                except Exception as e:
                    t_sample[s] = {"error": str(e)}
            debug["sample_ticker"] = t_sample
        except Exception as e:
            debug["errors"].append(f"ticker sample failed: {e}")

        # sample kline
        try:
            if sample_symbols and len(sample_symbols) > 0:
                s = sample_symbols[0]
            else:
                s = ALLOWED_COINS[0] if ALLOWED_COINS else None
            if s:
                k = session.get_kline(category="spot", symbol=s, interval="1", limit=10)
                debug["sample_kline"] = str(k)[:2000]
        except Exception as e:
            debug["errors"].append(f"kline sample failed: {e}")

        debug["ok"] = (debug["balance"] is not None)
        return debug


if __name__ == "__main__":
    bot = BotController()
    bot.log("Bot module loaded. Call bot.start() from your server to run.")
