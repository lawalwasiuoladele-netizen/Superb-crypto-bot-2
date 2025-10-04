# bot.py
"""
LIVE SPOT TRADING BOT (Bybit via pybit.unified_trading)

- Fixes recursion and threading.Queue issues.
- Trades all configured accounts together (single scanning loop).
- Safe, defensive network/API handling and file writes.
- Exposes methods used by the dashboard server:
    BotController(log_queue=...), start(), stop(), is_running(), validate_account()
"""

import json
import os
import time
import uuid
import math
import threading
import traceback
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
import queue

# pybit unified trading HTTP client
from pybit.unified_trading import HTTP

# ---------------------- CONFIG ----------------------
ALLOWED_COINS = [
    "ADAUSDT","XRPUSDT","TRXUSDT","DOGEUSDT","CHZUSDT","VETUSDT","BTTUSDT","HOTUSDT","XLMUSDT","ZILUSDT",
    "IOTAUSDT","SCUSDT","DENTUSDT","KEYUSDT","WINUSDT","CVCUSDT","MTLUSDT","CELRUSDT","FUNUSDT","STMXUSDT",
    "REEFUSDT","ANKRUSDT","ONEUSDT","OGNUSDT","CTSIUSDT","DGBUSDT","CKBUSDT","ARPAUSDT","MBLUSDT","TROYUSDT",
    "PERLUSDT","DOCKUSDT","RENUSDT","COTIUSDT","MDTUSDT","OXTUSDT","PHAUSDT","BANDUSDT","GTOUSDT","LOOMUSDT",
    "PONDUSDT","FETUSDT","SYSUSDT","TLMUSDT","NKNUSDT","LINAUSDT","ORNUSDT","COSUSDT","FLMUSDT","ALICEUSDT"
]

RISK_RULES = {
    "stop_loss": -3.0,    # -3% stop loss
    # (time_seconds, pct)
    "tp1": (600, 7.0),    # within 10 minutes -> 7%
    "tp2": (1020, 4.0),   # within 17 minutes -> 4%
    "tp3": (1200, 1.0),   # within 20 minutes -> 1%
    "max_hold": 1320      # 22 minutes force exit
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
    "test_on_testnet": False,       # Set False for mainnet (change to True only for testnet keys)
    "scan_interval": 10,            # seconds between scans
    "min_usdt_allocation": 1.0,     # don't place buys smaller than this
    "exit_on_any_profit": False     # If True, exit when profit > 0%
}

ACCOUNTS_FILE = "accounts.json"
TRADES_FILE = "trades.json"

# ---------------------- BotController ----------------------
class BotController:
    def __init__(self, log_queue: Optional[queue.Queue] = None):
        # Use queue.Queue (not threading.Queue) to avoid AttributeError
        self.log_queue = log_queue
        self._running = False
        self._stop_event = threading.Event()
        self._file_lock = threading.Lock()
        self._main_thread: Optional[threading.Thread] = None

        # ensure files exist
        if not os.path.exists(ACCOUNTS_FILE):
            with open(ACCOUNTS_FILE, "w") as f:
                json.dump([], f)
        if not os.path.exists(TRADES_FILE):
            with open(TRADES_FILE, "w") as f:
                json.dump([], f)

    # --------- Logging ----------
    def log(self, msg: str):
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        if self.log_queue:
            try:
                # non-blocking put
                self.log_queue.put_nowait(line)
            except Exception:
                # ignore log push errors
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

    def _update_account(self, acc: Dict[str, Any]):
        """
        Write back small changes to the accounts file safely.
        """
        try:
            accounts = self.load_accounts()
            updated = False
            for a in accounts:
                if a.get("key") and acc.get("key") and a.get("key") == acc.get("key"):
                    a.update(acc)
                    updated = True
                    break
                # fallback match by name
                if a.get("name") and acc.get("name") and a.get("name") == acc.get("name"):
                    a.update(acc)
                    updated = True
                    break
            if not updated:
                accounts.append(acc)
            self.save_accounts(accounts)
        except Exception as e:
            self.log(f"_update_account error: {e}")

    # --------- Bybit session ----------
    def _make_session(self, acc: Dict[str, Any]) -> HTTP:
        # session is per-account (authenticated). pybit accepts testnet flag.
        return HTTP(
            testnet=bool(TRADE_SETTINGS["test_on_testnet"]),
            api_key=acc.get("key"),
            api_secret=acc.get("secret")
        )

    # A small helper to create a 'public' session (without credentials) for market data if needed.
    def _make_public_session(self) -> HTTP:
        # Some pybit versions allow HTTP(testnet=...) without keys for public endpoints.
        try:
            return HTTP(testnet=bool(TRADE_SETTINGS["test_on_testnet"]))
        except Exception:
            # fallback: create using first account keys later
            return None

    # --------- Account validation (used by server.py) ----------
    def validate_account(self, acc: Dict[str, Any], retries: int = 2) -> Tuple[bool, Optional[float], Optional[str]]:
        """
        Validate credentials and return (ok, balance, error_message)
        Tries UNIFIED balance first, then SPOT as a fallback.
        """
        try:
            # normalize input
            key = acc.get("key") or acc.get("api_key")
            secret = acc.get("secret") or acc.get("api_secret")
            if not key or not secret:
                return False, None, "missing key/secret"

            tmp_acc = {"key": key, "secret": secret}
            session = self._make_session(tmp_acc)

            # Try UNIFIED
            for attempt in range(retries):
                try:
                    res = session.get_wallet_balance(accountType="UNIFIED")
                    # expected: res["result"]["list"][0]["totalEquity"] or coin list
                    if res and isinstance(res.get("result"), dict):
                        # try structure with totalEquity
                        rl = res["result"].get("list", [])
                        if rl:
                            first = rl[0]
                            # attempt: totalEquity
                            if first.get("totalEquity") is not None:
                                bal = float(first.get("totalEquity", 0.0))
                                return True, round(bal, 6), None
                            # else, try coin array -> find USDT
                            coin_list = first.get("coin") or first.get("wallet", []) or []
                            for c in coin_list:
                                if str(c.get("coin", "")).upper() in ("USDT", "USDC", "USD"):
                                    val = c.get("walletBalance") or c.get("balance") or c.get("availableBalance")
                                    if val is not None:
                                        return True, round(float(val), 6), None
                    break
                except Exception as e:
                    # retry
                    last_err = str(e)
                    time.sleep(0.5)
            # UNIFIED failed -> try SPOT
            for attempt in range(retries):
                try:
                    res = session.get_wallet_balance(accountType="SPOT")
                    rl = res.get("result", {}).get("list", [])
                    if rl:
                        # structure varies; try to find USDT wallet
                        first = rl[0]
                        # if coin list present
                        coin_list = first.get("coin") or first.get("wallet", []) or []
                        for c in coin_list:
                            if str(c.get("coin", "")).upper() in ("USDT", "USDC", "USD"):
                                val = c.get("walletBalance") or c.get("balance") or c.get("availableBalance")
                                if val is not None:
                                    return True, round(float(val), 6), None
                        # fallback: totalEquity
                        if first.get("totalEquity") is not None:
                            return True, round(float(first.get("totalEquity")), 6), None
                    break
                except Exception as e:
                    last_err = str(e)
                    time.sleep(0.5)
            return False, None, last_err if 'last_err' in locals() else "unable to parse balance"
        except Exception as e:
            tb = traceback.format_exc()
            return False, None, f"validate_account exception: {e} | {tb}"

    # --------- Market helpers (safe, non-recursive) ----------
    def get_ticker_last(self, session: HTTP, symbol: str) -> Optional[float]:
        """
        Get last traded price for symbol. Use get_tickers first; fallback to kline close.
        No recursion here.
        """
        try:
            # primary
            try:
                resp = session.get_tickers(category="spot", symbol=symbol)
                # expect resp["result"]["list"][0]["lastPrice"]
                lst = resp.get("result", {}).get("list", [])
                if lst and lst[0].get("lastPrice") is not None:
                    return float(lst[0]["lastPrice"])
            except Exception as e:
                # not fatal — we'll attempt fallback
                self.log(f"get_ticker_last primary failed for {symbol}: {e}")

            # fallback: single kline close
            try:
                k = session.get_kline(category="spot", symbol=symbol, interval="1", limit=1)
                candles = k.get("result", {}).get("list", [])
                if candles:
                    c = candles[0]
                    if c.get("close") is not None:
                        return float(c["close"])
            except Exception as e:
                self.log(f"get_ticker_last fallback failed for {symbol}: {e}")

            return None
        except Exception as e:
            self.log(f"get_ticker_last unexpected error for {symbol}: {e}")
            return None

    def get_klines(self, session: HTTP, symbol: str, interval: str = "1", limit: int = 50) -> List[Dict[str, Any]]:
        try:
            resp = session.get_kline(category="spot", symbol=symbol, interval=str(interval), limit=limit)
            candles = resp.get("result", {}).get("list", [])
            if not candles:
                return []
            # keep chronological order ascending
            return list(reversed(candles))
        except Exception as e:
            self.log(f"get_klines error for {symbol}: {e}")
            return []

    def get_balance(self, session: HTTP, acc: Dict[str, Any], retries: int = 2) -> Optional[float]:
        """
        Try UNIFIED then SPOT; return float balance or None on error.
        """
        try:
            # attempt UNIFIED
            for attempt in range(retries):
                try:
                    resp = session.get_wallet_balance(accountType="UNIFIED")
                    rl = resp.get("result", {}).get("list", [])
                    if rl:
                        first = rl[0]
                        if first.get("totalEquity") is not None:
                            return float(first.get("totalEquity", 0.0))
                        coin_list = first.get("coin") or first.get("wallet", []) or []
                        for c in coin_list:
                            if str(c.get("coin", "")).upper() in ("USDT", "USDC", "USD"):
                                val = c.get("walletBalance") or c.get("balance") or c.get("availableBalance")
                                if val is not None:
                                    return float(val)
                    # if no data, retry
                except Exception as e:
                    self.log(f"get_balance attempt {attempt+1} error for ****{(acc.get('key') or '')[-6:]}: {e}")
                    last_err = str(e)
                    time.sleep(0.5)
            # fallback to SPOT
            for attempt in range(retries):
                try:
                    resp = session.get_wallet_balance(accountType="SPOT")
                    rl = resp.get("result", {}).get("list", [])
                    if rl:
                        first = rl[0]
                        coin_list = first.get("coin") or first.get("wallet", []) or []
                        for c in coin_list:
                            if str(c.get("coin", "")).upper() in ("USDT", "USDC", "USD"):
                                val = c.get("walletBalance") or c.get("balance") or c.get("availableBalance")
                                if val is not None:
                                    return float(val)
                        if first.get("totalEquity") is not None:
                            return float(first.get("totalEquity"))
                except Exception as e:
                    self.log(f"get_balance SPOT attempt {attempt+1} error for ****{(acc.get('key') or '')[-6:]}: {e}")
                    last_err = str(e)
                    time.sleep(0.5)
            self.log(f"get_balance failed for ****{(acc.get('key') or '')[-6:]}: {last_err if 'last_err' in locals() else 'no data'}")
            return None
        except Exception as e:
            self.log(f"get_balance unexpected error for ****{(acc.get('key') or '')[-6:]}: {e}")
            return None

    # --------- Indicators ----------
    def compute_rsi(self, closes: List[float], period: int = 14) -> Optional[float]:
        if len(closes) < period + 1:
            return None
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [d for d in deltas[-period:] if d > 0]
        losses = [-d for d in deltas[-period:] if d < 0]
        avg_gain = sum(gains) / period if gains else 0.0
        avg_loss = sum(losses) / period if losses else 0.0
        if avg_loss == 0 and avg_gain == 0:
            return 50.0
        if avg_loss == 0:
            return 100.0
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

    # --------- Scoring & Entry ----------
    def score_symbol(self, closes: List[float], volumes: List[float]) -> float:
        try:
            if not closes:
                return -999.0
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
        except Exception as e:
            self.log(f"score_symbol error: {e}")
            return -999.0

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

    # --------- Exit logic ----------
    def check_exit_condition(self, acc: Dict[str, Any], current_price: float) -> Tuple[bool, float, float]:
        try:
            entry_price = float(acc.get("buy_price", 0.0) or 0.0)
            entry_time = float(acc.get("entry_time", time.time()))
            if entry_price <= 0:
                return False, 0.0, 0.0

            profit_pct = (current_price - entry_price) / (entry_price if entry_price != 0 else 0.000001) * 100
            elapsed = time.time() - entry_time

            # Immediate exit if configured and any profit > 0
            if TRADE_SETTINGS.get("exit_on_any_profit", False) and profit_pct > 0:
                return True, profit_pct, elapsed

            # stop loss
            if profit_pct <= RISK_RULES["stop_loss"]:
                return True, profit_pct, elapsed

            # TPs
            if elapsed <= RISK_RULES["tp1"][0] and profit_pct >= RISK_RULES["tp1"][1]:
                return True, profit_pct, elapsed
            if elapsed <= RISK_RULES["tp2"][0] and profit_pct >= RISK_RULES["tp2"][1]:
                return True, profit_pct, elapsed
            if elapsed <= RISK_RULES["tp3"][0] and profit_pct >= RISK_RULES["tp3"][1]:
                return True, profit_pct, elapsed

            # force exit at max_hold
            if elapsed >= RISK_RULES["max_hold"]:
                return True, profit_pct, elapsed

            return False, profit_pct, elapsed
        except Exception as e:
            self.log(f"⚠️ Error in check_exit_condition {acc.get('current_symbol')}: {e}")
            return False, 0.0, 0.0

    # --------- Trading helpers ----------
    def place_market_buy(self, session: HTTP, symbol: str, usdt_amount: float):
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
            "orderType": "Market" if TRADE_SETTINGS["use_market_order"] else "Limit",
            "qty": str(qty)
        }
        if not TRADE_SETTINGS["use_market_order"]:
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

    # --------- Main loop (trade all accounts together) ----------
    def _main_loop(self):
        self.log("Main scan loop started.")
        scan_interval = int(TRADE_SETTINGS.get("scan_interval", 10))
        while not self._stop_event.is_set():
            try:
                accounts = self.load_accounts()
                if not accounts:
                    self.log("⚠️ No accounts configured in accounts.json. Sleeping...")
                    time.sleep(scan_interval)
                    continue

                # normalize accounts and ensure defaults
                for a in accounts:
                    a.setdefault("position", "closed")
                    a.setdefault("monitoring", False)
                    a.setdefault("current_symbol", None)
                    a.setdefault("buy_price", 0.0)
                    a.setdefault("entry_time", 0)
                    a.setdefault("last_trade_start", None)
                    a.setdefault("last_balance", 0.0)
                    a.setdefault("qty", None)
                    a.setdefault("allocated_usdt", 0.0)
                    a.setdefault("current_price", None)
                    a.setdefault("current_pnl", None)

                # choose a session for market scans: try public session, else first account session
                public_session = None
                try:
                    public_session = self._make_public_session()
                except Exception:
                    public_session = None

                if public_session is None:
                    # fallback: create a session from the first account (authenticated but ok for market calls)
                    first_acc = accounts[0]
                    public_session = self._make_session(first_acc)

                # Scan all symbols once and compute scores
                best_score = -9999.0
                best_symbol = None
                best_meta = None

                for symbol in ALLOWED_COINS:
                    price = self.get_ticker_last(public_session, symbol)
                    if price is None:
                        continue
                    if price > SCORE_SETTINGS["max_price_allowed"]:
                        continue
                    candles = self.get_klines(public_session, symbol, interval="1", limit=50)
                    if not candles:
                        continue
                    closes = [float(c["close"]) for c in candles if "close" in c]
                    volumes = [float(c.get("volume", 0) or 0) for c in candles]
                    score = self.score_symbol(closes, volumes)
                    if score > best_score:
                        best_score = score
                        best_symbol = symbol
                        best_meta = {"price": price, "closes": closes, "volumes": volumes}

                if best_symbol:
                    self.log(f"Best candidate: {best_symbol} score={best_score:.3f}")
                    candidate_ok = self.check_entry_condition(best_symbol, best_meta["closes"], best_meta["price"], best_meta["volumes"])
                    if candidate_ok:
                        # For each account that is closed, attempt an entry (trade all together)
                        for acc in accounts:
                            try:
                                # don't attempt entry if already in position
                                if acc.get("position") == "open":
                                    continue

                                session = self._make_session(acc)

                                # Update balance if unknown or stale (best-effort)
                                balance = acc.get("last_balance", None)
                                if balance is None or balance == 0.0:
                                    bal = self.get_balance(session, acc)
                                    if bal is not None:
                                        acc["last_balance"] = bal
                                        self._update_account(acc)
                                    else:
                                        self.log(f"Skipping account ****{(acc.get('key') or '')[-6:]} due to missing balance.")
                                        continue

                                allocated = (acc.get("last_balance", 0.0) or 0.0) * float(TRADE_SETTINGS["trade_allocation_pct"])
                                if allocated < float(TRADE_SETTINGS.get("min_usdt_allocation", 1.0)):
                                    self.log(f"Skipping account ****{(acc.get('key') or '')[-6:]}: allocated ${allocated:.4f} < min allocation")
                                    continue

                                # Place buy
                                self.log(f"Placing buy for {best_symbol} for account ****{(acc.get('key') or '')[-6:]} using ${allocated:.4f}")
                                try:
                                    buy_resp = self.place_market_buy(session, best_symbol, allocated)
                                    buy_price = buy_resp.get("price") or best_meta["price"]
                                    qty = buy_resp.get("qty")
                                    self.log(f"Buy response for ****{(acc.get('key') or '')[-6:]}: price={buy_price}, qty={qty}, resp={buy_resp.get('resp')}")
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
                                    # record entry
                                    entry_trade = {
                                        "id": str(uuid.uuid4()),
                                        "account": acc.get("name", acc.get("key","")[:6]),
                                        "account_key_tail": (acc.get("key") or "")[-6:],
                                        "symbol": best_symbol,
                                        "entry_price": buy_price,
                                        "qty": qty,
                                        "entry_time": acc.get("last_trade_start")
                                    }
                                    self.add_trade(entry_trade)
                                except Exception as e:
                                    self.log(f"Buy failed for ****{(acc.get('key') or '')[-6:]} on {best_symbol}: {e}")
                                    continue

                            except Exception as e:
                                self.log(f"Unexpected error while trying to enter for acc ****{(acc.get('key') or '')[-6:]}: {e}")

                else:
                    self.log("No suitable candidate found in this scan.")

                # --- monitor open positions for exits ---
                for acc in accounts:
                    if acc.get("position") == "open" and acc.get("current_symbol"):
                        try:
                            session = self._make_session(acc)
                            symbol = acc["current_symbol"]
                            price = self.get_ticker_last(session, symbol)
                            if price is None:
                                # skip this round
                                continue
                            acc["current_price"] = price
                            profit_pct = (price - float(acc.get("buy_price", 0.0))) / (float(acc.get("buy_price", 0.000001))) * 100
                            acc["current_pnl"] = profit_pct
                            self._update_account(acc)

                            should_exit, p_pct, elapsed = self.check_exit_condition(acc, price)
                            if should_exit:
                                self.log(f"Exiting {symbol} for account ****{(acc.get('key') or '')[-6:]} | profit={p_pct:.2f}% | elapsed={int(elapsed)}s")
                                qty = acc.get("qty", None)
                                if qty is None and acc.get("allocated_usdt") and acc.get("buy_price"):
                                    qty = float(round(float(acc.get("allocated_usdt")) / float(acc.get("buy_price")), 6))
                                try:
                                    if qty:
                                        sell_resp = self.place_market_sell(session, symbol, qty)
                                        self.log(f"Sell resp for ****{(acc.get('key') or '')[-6:]}: {sell_resp}")
                                    else:
                                        self.log("No qty known for sell, skipping sell API call (manual intervention needed).")
                                except Exception as e:
                                    self.log(f"Sell order failed for {symbol} on ****{(acc.get('key') or '')[-6:]}: {e}")

                                trade = {
                                    "id": str(uuid.uuid4()),
                                    "account": acc.get("name", acc.get("key","")[:6]),
                                    "account_key_tail": (acc.get("key") or "")[-6:],
                                    "symbol": symbol,
                                    "entry_price": acc.get("buy_price"),
                                    "exit_price": price,
                                    "profit_pct": p_pct,
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

                        except Exception as e:
                            self.log(f"Error monitoring account ****{(acc.get('key') or '')[-6:]}: {e}")

                # save accounts (in case balances changed)
                self.save_accounts(accounts)

                # sleep until next scan
                for _ in range(int(scan_interval)):
                    if self._stop_event.is_set():
                        break
                    time.sleep(1)

            except Exception as e:
                # Log and continue (do not kill the main loop)
                self.log(f"Unexpected error in main loop: {e}\n{traceback.format_exc()}")
                # short sleep to avoid tight crash loop
                time.sleep(2)

        self._running = False
        self.log("Main scan loop exited.")

    # --------- Control ----------
    def start(self):
        if self._running:
            self.log("Bot already running.")
            return
        # Clear stop event
        self._stop_event.clear()
        self._main_thread = threading.Thread(target=self._main_loop, daemon=True)
        self._main_thread.start()
        self._running = True
        self.log("Bot start requested (main loop launched).")

    def stop(self):
        self._stop_event.set()
        self._running = False
        self.log("Stop signal set. Main loop will exit shortly.")

    def is_running(self) -> bool:
        return self._running

# ---------------------- USAGE ----------------------
if __name__ == "__main__":
    bot = BotController()
    bot.log("Bot ready. Edit config at top of file before running live.")
    # Do NOT autostart here; call bot.start() from your server/process runner.
