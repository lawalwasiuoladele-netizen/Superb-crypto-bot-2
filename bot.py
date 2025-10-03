"""
LIVE SPOT TRADING BOT (Bybit via pybit.unified_trading)

This is a hardened version of your long bot.py:
- Uses queue.Queue (not threading.Queue).
- Safe, non-recursive logging and minimal enqueueing for SSE.
- validate_account() returns (ok, balance, error_msg) and does not log internally.
- Re-entrant file lock to avoid nested-lock problems.
- Keeps your scanning, scoring, entry/exit and per-account threads intact.

SAFETY: Test with TRADE_SETTINGS["test_on_testnet"] = True and testnet keys before running live.
"""

import json
import os
import threading
import time
import uuid
import math
import random
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
import queue    # correct module for Queue
from pybit.unified_trading import HTTP

# ---------------------- CONFIG ----------------------
ALLOWED_COINS = [
    # example list (keep as you had it)
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
    "test_on_testnet": True,        # set to False when ready live
    "scan_interval": 10,            # seconds between scans when idle
}

ACCOUNTS_FILE = "accounts.json"
TRADES_FILE = "trades.json"

# ---------------------- BotController ----------------------
class BotController:
    def __init__(self, log_queue: Optional[queue.Queue] = None):
        # Use queue.Queue for inter-thread logging
        self.log_queue: queue.Queue = log_queue or queue.Queue()
        self._running = False
        self._stop_event = threading.Event()
        # Use RLock so nested file operations from same thread don't deadlock
        self._file_lock = threading.RLock()
        self._threads: List[threading.Thread] = []

        # Ensure files exist
        if not os.path.exists(ACCOUNTS_FILE):
            try:
                with open(ACCOUNTS_FILE, "w") as f:
                    json.dump([], f)
            except Exception:
                # Last resort: use minimal print but avoid complex logging
                print("[init] failed creating accounts.json", flush=True)
        if not os.path.exists(TRADES_FILE):
            try:
                with open(TRADES_FILE, "w") as f:
                    json.dump([], f)
            except Exception:
                print("[init] failed creating trades.json", flush=True)

    # ---------- Internal minimal enqueue log (very safe) ----------
    def _enqueue_log(self, line: str):
        """Print and try to put into queue without any additional logic that could recurse."""
        try:
            # limit size of message: prevents huge objects causing issues
            if not isinstance(line, str):
                line = str(line)
            # ensure single-line messages for SSE
            line = line.replace("\n", " ")
            print(line, flush=True)
            # non-blocking queue put so this never stalls critical code
            try:
                self.log_queue.put_nowait(line)
            except Exception:
                # ignore queue errors, do not call log() or anything else here
                pass
        except Exception:
            # absolute last resort: avoid raising
            try:
                print("[_enqueue_log] logging failed", flush=True)
            except Exception:
                pass

    # ---------- Safe public logging ----------
    def log(self, msg: Any):
        """
        Public logger used throughout the bot. Converts objects safely to string
        without risking recursive repr() calls.
        """
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        message = self._safe_to_string(msg)
        line = f"[{timestamp}] {message}"
        self._enqueue_log(line)

    def _safe_to_string(self, obj: Any, max_len: int = 1000) -> str:
        """
        Convert obj to a safe short string. Avoid json.dumps on objects that might contain
        circular references. Falls back to type-name and id if necessary.
        """
        if isinstance(obj, str):
            # truncate if excessively long
            return obj if len(obj) <= max_len else obj[:max_len] + "..."
        try:
            # try JSON first for common types (dict/list)
            return json.dumps(obj, default=lambda o: f"<{type(o).__name__}>")[:max_len]
        except Exception:
            # fallback to str, but guard against RecursionError
            try:
                s = str(obj)
                return s if len(s) <= max_len else s[:max_len] + "..."
            except RecursionError:
                return f"<{type(obj).__name__} at {hex(id(obj))}>"
            except Exception:
                return f"<{type(obj).__name__}>"

    # --------- File helpers ----------
    def load_accounts(self) -> List[Dict[str, Any]]:
        try:
            with self._file_lock:
                with open(ACCOUNTS_FILE, "r") as f:
                    return json.load(f)
        except Exception:
            # return empty list on any read error
            return []

    def save_accounts(self, accounts: List[Dict[str, Any]]):
        # Don't call self.log() from here to avoid possible nested logging loops.
        try:
            with self._file_lock:
                with open(ACCOUNTS_FILE, "w") as f:
                    json.dump(accounts, f, indent=2)
        except Exception as e:
            # minimal enqueue log to report problem
            self._enqueue_log(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] ⚠️ save_accounts failed: {str(e)[:300]}")

    def add_trade(self, trade: Dict[str, Any]):
        try:
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
        except Exception as e:
            self._enqueue_log(f"⚠️ add_trade failed: {str(e)[:300]}")

    # --------- Bybit session ----------
    def _make_session(self, acc: Dict[str, Any]) -> HTTP:
        return HTTP(
            testnet=TRADE_SETTINGS["test_on_testnet"],
            api_key=acc["key"],
            api_secret=acc["secret"]
        )

    def validate_account(self, acc: Dict[str, Any], retries: int = 1) -> Tuple[bool, float, Optional[str]]:
        """
        Validate an account by attempting a simple wallet balance read.
        Returns (ok: bool, balance: float, error_msg: Optional[str]).
        Important: this method does NOT call self.log() to avoid recursive logging issues;
        callers should log the result as needed.
        """
        try:
            session = self._make_session(acc)
            for attempt in range(max(1, retries)):
                try:
                    resp = session.get_wallet_balance(accountType="UNIFIED")
                    balance_list = resp.get("result", {}).get("list", [])
                    if balance_list:
                        first = balance_list[0]
                        total = float(first.get("totalEquity", 0) or 0.0)
                        return True, total, None
                    return True, 0.0, None
                except RecursionError as re:
                    # protect against unexpected recursion in external libs - return failure
                    return False, 0.0, f"RecursionError during validate: {str(re)}"
                except Exception as e:
                    # try again if allowed
                    last_err = str(e)
                    time.sleep(0.2)
            return False, 0.0, last_err[:500]
        except RecursionError as re_outer:
            return False, 0.0, f"RecursionError (outer) - {str(re_outer)}"
        except Exception as e_outer:
            return False, 0.0, str(e_outer)[:500]

    def get_ticker_last(self, session: HTTP, symbol: str) -> Optional[float]:
        try:
            data = session.get_tickers(category="spot", symbol=symbol)
            return float(data["result"]["list"][0]["lastPrice"])
        except Exception as e:
            self._enqueue_log(f"get_ticker_last error for {symbol}: {str(e)[:200]}")
            return None

    def get_klines(self, session: HTTP, symbol: str, interval: str = "1", limit: int = 50) -> List[Dict[str, Any]]:
        try:
            resp = session.get_kline(category="spot", symbol=symbol, interval=str(interval), limit=limit)
            candles = resp.get("result", {}).get("list", [])
            if not candles:
                return []
            return list(reversed(candles))
        except Exception as e:
            self._enqueue_log(f"get_klines error for {symbol}: {str(e)[:200]}")
            return []

    def get_balance(self, session: HTTP, acc: Dict[str, Any], retries: int = 2) -> Optional[float]:
        for attempt in range(retries):
            try:
                resp = session.get_wallet_balance(accountType="UNIFIED")
                balance_list = resp.get("result", {}).get("list", [])
                if balance_list:
                    first = balance_list[0]
                    total = float(first.get("totalEquity", 0) or 0.0)
                    # update account dict locally, but use _update_account to persist
                    acc["last_balance"] = total
                    try:
                        self._update_account(acc)
                    except Exception:
                        # avoid logging here (could cause nested logs) — do safe enqueue
                        self._enqueue_log(f"_update_account failed while get_balance (non-fatal).")
                    return total
                else:
                    acc["last_balance"] = 0.0
                    try:
                        self._update_account(acc)
                    except Exception:
                        self._enqueue_log(f"_update_account failed while set zero balance.")
                    return 0.0
            except Exception as e:
                self._enqueue_log(f"get_balance error for {acc.get('key','')[:6]}...: {str(e)[:200]}")
                time.sleep(0.5)
        return None

    def _update_account(self, acc: Dict[str, Any]):
        """
        Write back small changes to the accounts file safely.
        Avoid calling self.log() here to prevent nested logging recursion.
        """
        try:
            with self._file_lock:
                accounts = self.load_accounts()
                updated = False
                for a in accounts:
                    if a.get("key") == acc.get("key"):
                        # merge only shallow fields
                        a.update({k: v for k, v in acc.items() if not callable(v)})
                        updated = True
                        break
                if not updated:
                    accounts.append(acc)
                # persist
                with open(ACCOUNTS_FILE, "w") as f:
                    json.dump(accounts, f, indent=2)
        except RecursionError as re:
            # last-resort safe enqueue
            self._enqueue_log(f"_update_account RecursionError: {str(re)}")
        except Exception as e:
            self._enqueue_log(f"_update_account error: {str(e)[:300]}")

    # --------- Indicators ----------
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
            self._enqueue_log(f"Entry check {symbol}: RSI={rsi}, mom={momentum:.3f}, vol_spike={vol_spike}, ma_cross={ma_cross}, price={current_price}, pass={ok}")
            return ok
        except RecursionError as re:
            self._enqueue_log(f"⚠️ RecursionError in check_entry_condition {symbol}: {str(re)}")
            return False
        except Exception as e:
            self._enqueue_log(f"⚠️ Error in check_entry_condition {symbol}: {str(e)[:300]}")
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
        except RecursionError:
            return -999
        except Exception:
            return -999

    # --------- Exit logic ----------
    def check_exit_condition(self, acc: Dict[str, Any], current_price: float):
        try:
            entry_price = acc.get("buy_price", 0.0) or 0.0
            entry_time = acc.get("entry_time", time.time())
            if entry_price <= 0:
                self._enqueue_log(f"⚠️ Invalid entry price for {acc.get('current_symbol')}, skipping exit check.")
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
        except RecursionError as re:
            self._enqueue_log(f"⚠️ RecursionError in check_exit_condition {acc.get('current_symbol')}: {str(re)}")
            return True, 0.0, 0
        except Exception as e:
            self._enqueue_log(f"⚠️ Error in check_exit_condition {acc.get('current_symbol')}: {str(e)[:300]}")
            return False, 0.0, 0

    # --------- Trading helpers ----------
    def place_market_buy(self, session: HTTP, symbol: str, usdt_amount: float):
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
        res = session.place_spot_order(**params)
        return {"price": price, "qty": qty, "resp": res}

    def place_market_sell(self, session: HTTP, symbol: str, qty: float):
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
            self._enqueue_log(f"Failed creating session for account ****{acc.get('key','')[-4:]}: {str(e)[:200]}")
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
                    try:
                        self._update_account(acc)
                    except Exception:
                        self._enqueue_log("Warning: _update_account failed while monitoring (non-fatal).")

                    should_exit, profit_pct, elapsed = self.check_exit_condition(acc, price)

                    # Log per minute while trade active (avoid spamming)
                    if int(elapsed) % 60 == 0:
                        self._enqueue_log(f"Account ****{acc.get('key')[-4:]} | Monitoring {symbol} | elapsed={int(elapsed)}s | profit={profit_pct:.2f}% | price={price}")

                    if should_exit:
                        self._enqueue_log(f"Exiting {symbol} for account ****{acc.get('key')[-4:]} | profit={profit_pct:.2f}% | elapsed={int(elapsed)}s")
                        qty = acc.get("qty", None)
                        if qty is None and acc.get("allocated_usdt") and acc.get("buy_price"):
                            qty = float(round(acc.get("allocated_usdt") / acc.get("buy_price"), 6))
                        try:
                            if qty:
                                sell_resp = self.place_market_sell(session, symbol, qty)
                                self._enqueue_log(f"Sell resp: {self._safe_to_string(sell_resp,300)}")
                            else:
                                self._enqueue_log("No qty known for sell, skipping sell API call (manual intervention needed).")
                        except Exception as e:
                            self._enqueue_log(f"Sell order failed for {symbol}: {str(e)[:300]}")

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

                        # clear position fields
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

                # Not in a position -> scan allowed coins and attempt entry
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
                    self._enqueue_log(f"Account ****{acc.get('key')[-4:]} | Best candidate: {best_symbol} score={best_score:.3f}")
                    candidate_ok = self.check_entry_condition(best_symbol, best_meta["closes"], best_meta["price"], best_meta["volumes"])
                    if candidate_ok:
                        equity = acc.get("last_balance", None)
                        if equity is None:
                            equity = self.get_balance(session, acc) or 0.0
                        allocated = equity * TRADE_SETTINGS["trade_allocation_pct"]
                        if allocated <= 0 or equity <= 0:
                            self._enqueue_log("Allocated amount <= 0 or balance missing, skipping trade.")
                        else:
                            try:
                                self._enqueue_log(f"Placing buy for {best_symbol} using ${allocated:.4f} allocation.")
                                buy_resp = self.place_market_buy(session, best_symbol, allocated)
                                buy_price = buy_resp.get("price", best_meta["price"])
                                qty = buy_resp.get("qty", None)
                                self._enqueue_log(f"Buy resp price={buy_price}, qty={qty}")
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
                                self._enqueue_log(f"Buy order failed for {best_symbol}: {str(e)[:300]}")
                    else:
                        self._enqueue_log(f"Candidate {best_symbol} failed strict entry checks.")
                else:
                    self._enqueue_log("No suitable candidate found in this scan.")

                # sleep until next scan with early stop checks
                for _ in range(int(scan_interval)):
                    if self._stop_event.is_set():
                        break
                    time.sleep(1)

            except RecursionError as re_outer:
                # If recursion arises, log minimal info and break the account thread to avoid repeated crashes
                self._enqueue_log(f"⚠️ RecursionError in run_account for ****{acc.get('key','')[-4:]}: {str(re_outer)}")
                break
            except Exception as e:
                self._enqueue_log(f"Unexpected error in run_account for ****{acc.get('key','')[-4:]}: {str(e)[:300]}")
                time.sleep(2)

        self._enqueue_log(f"Exiting run loop for account ****{acc.get('key','')[-4:]}")

    # --------- Control ----------
    def start(self):
        """
        Start trading for all accounts present in accounts.json.
        Spawns one thread per account (daemon).
        """
        accounts = self.load_accounts()
        if not accounts:
            self._enqueue_log("⚠️ No accounts configured in accounts.json.")
            return

        self._stop_event.clear()
        self._running = True

        for acc in accounts:
            acc.setdefault("position", "closed")
            acc.setdefault("monitoring", False)
            try:
                # Test connectivity quickly but keep it minimal
                session = self._make_session(acc)
                try:
                    _ = session.get_wallet_balance(accountType="UNIFIED")
                except Exception as e:
                    # Log but continue to start thread (account might still be usable)
                    self._enqueue_log(f"Warning: quick connectivity test failed for ****{acc.get('key','')[-4:]}: {str(e)[:200]}")
                t = threading.Thread(target=self.run_account, args=(acc,), daemon=True)
                t.start()
                self._threads.append(t)
                self._enqueue_log(f"✅ Bot started thread for account ****{acc.get('key')[-4:]}")
            except Exception as e:
                self._enqueue_log(f"❌ Failed to start account ****{acc.get('key','')[-4:]}: {str(e)[:200]}")

        self._enqueue_log("Bot startup attempt complete.")

    def stop(self):
        self._stop_event.set()
        self._running = False
        self._enqueue_log("Stop signal set. Threads will exit shortly.")
        # threads are daemon so they'll exit with the process

    def is_running(self) -> bool:
        return self._running
