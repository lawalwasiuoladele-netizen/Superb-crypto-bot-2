# bot.py
"""
LIVE SPOT TRADING BOT (Bybit via pybit.unified_trading)

⚠️ IMPORTANT SAFETY & SETUP NOTES (READ BEFORE RUNNING LIVE)
 - This bot places REAL spot market orders on Bybit when TRADE_SETTINGS["test_on_testnet"] is False.
 - Strongly recommended: test thoroughly on Bybit TESTNET first:
     * Set TRADE_SETTINGS["test_on_testnet"] = True
     * Use testnet API keys and small allocation.
 - Ensure your API key has Spot trading and wallet read permissions.
 - Ensure USDT is available in your Spot/Unified wallet (bot uses Unified wallet read).
 - Start with a tiny allocation (TRADE_SETTINGS["trade_allocation_pct"] = 0.005 or similar).
 - Market orders execute immediately and can slip; consider limit orders in future.
 - Monitor logs and your exchange dashboard closely on first runs.
"""

import json
import os
import threading
import time
import uuid
import math
import random
from datetime import datetime
from pybit.unified_trading import HTTP

# ---------------------- CONFIG ----------------------
ALLOWED_COINS = [
    # 50 coins - verify availability/liquidity on Bybit
    "ADAUSDT","XRPUSDT","TRXUSDT","DOGEUSDT","CHZUSDT","VETUSDT","BTTUSDT","HOTUSDT","XLMUSDT","ZILUSDT",
    "IOTAUSDT","SCUSDT","DENTUSDT","KEYUSDT","WINUSDT","CVCUSDT","MTLUSDT","CELRUSDT","FUNUSDT","STMXUSDT",
    "REEFUSDT","ANKRUSDT","ONEUSDT","OGNUSDT","CTSIUSDT","DGBUSDT","CKBUSDT","ARPAUSDT","MBLUSDT","TROYUSDT",
    "PERLUSDT","DOCKUSDT","RENUSDT","COTIUSDT","MDTUSDT","OXTUSDT","PHAUSDT","BANDUSDT","GTOUSDT","LOOMUSDT",
    "PONDUSDT","FETUSDT","SYSUSDT","TLMUSDT","NKNUSDT","LINAUSDT","ORNUSDT","COSUSDT","FLMUSDT","ALICEUSDT"
]

RISK_RULES = {
    "stop_loss": -3.0,    # -3% stop loss
    # TP windows expressed as (elapsed_seconds_upper_bound, pct)
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
    "momentum_entry_threshold_pct": 0.1,  # lowered for more trades
    "max_price_allowed": 1.2
}

TRADE_SETTINGS = {
    "trade_allocation_pct": 0.01,   # fraction of available USDT to spend per trade (change as desired)
    "use_market_order": True,       # market orders
    "test_on_testnet": False        # set True if you want to test on Bybit testnet
}

ACCOUNTS_FILE = "accounts.json"
TRADES_FILE = "trades.json"

# ---------------------- BotController ----------------------
class BotController:
    def __init__(self, log_queue=None):
        self.log_queue = log_queue
        self._running = False
        self._stop_event = threading.Event()
        self._file_lock = threading.Lock()

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
        print(line)
        if self.log_queue:
            try:
                self.log_queue.put(line, block=False)
            except Exception:
                pass

    # --------- File helpers ----------
    def load_accounts(self):
        try:
            with open(ACCOUNTS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []

    def save_accounts(self, accounts):
        with self._file_lock:
            with open(ACCOUNTS_FILE, "w") as f:
                json.dump(accounts, f, indent=2)

    def add_trade(self, trade):
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
    def _make_session(self, acc):
        return HTTP(
            testnet=TRADE_SETTINGS["test_on_testnet"],
            api_key=acc["key"],
            api_secret=acc["secret"]
        )

    def get_ticker_last(self, session, symbol):
        try:
            data = session.get_tickers(category="spot", symbol=symbol)
            return float(data["result"]["list"][0]["lastPrice"])
        except Exception as e:
            self.log(f"get_ticker_last error for {symbol}: {e}")
            return None

    def get_klines(self, session, symbol, interval="1", limit=30):
        try:
            resp = session.get_kline(category="spot", symbol=symbol, interval=str(interval), limit=limit)
            candles = resp.get("result", {}).get("list", [])
            if not candles:
                return []
            return list(reversed(candles))  # oldest -> newest
        except Exception as e:
            self.log(f"get_klines error for {symbol}: {e}")
            return []

    def get_balance(self, session, acc, retries=2):
        for attempt in range(retries):
            try:
                resp = session.get_wallet_balance(accountType="UNIFIED")
                balance_list = resp.get("result", {}).get("list", [])
                if balance_list:
                    total = float(balance_list[0].get("totalEquity", 0))
                    acc["last_balance"] = total
                    self._update_account(acc)
                    return total
                else:
                    acc["last_balance"] = 0.0
                    self._update_account(acc)
                    return 0.0
            except Exception as e:
                self.log(f"get_balance error for {acc.get('key')[:6]}...: {e}")
                time.sleep(1)
        return None

    # --------- Indicators ----------
    def compute_rsi(self, closes, period=14):
        if len(closes) < period + 1:
            return None
        gains, losses = [], []
        for i in range(-period, 0):
            diff = closes[i] - closes[i - 1]
            if diff > 0:
                gains.append(diff)
            else:
                losses.append(abs(diff))
        avg_gain = sum(gains) / period if gains else 0.0001
        avg_loss = sum(losses) / period if losses else 0.0001
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def compute_momentum_pct(self, closes, lookback=5):
        if len(closes) < lookback + 1:
            return 0.0
        recent_avg = sum(closes[-(lookback + 1):-1]) / lookback
        if recent_avg == 0:
            return 0.0
        return (closes[-1] - recent_avg) / recent_avg * 100

    def compute_sma(self, prices, period):
        if len(prices) < period:
            return None
        return sum(prices[-period:]) / period

    # --------- Entry logic ----------
    def check_entry_condition(self, symbol, closes, current_price, volumes=None):
        try:
            if current_price is None:
                return False
            if current_price > SCORE_SETTINGS["max_price_allowed"]:
                return False

            rsi = self.compute_rsi(closes, 14)
            momentum = self.compute_momentum_pct(closes, 5)

            rsi_signal = (rsi is not None) and (rsi < SCORE_SETTINGS["rsi_oversold_threshold"] or rsi > SCORE_SETTINGS["rsi_overbought_threshold"])

            # volume spike: user asked for looser threshold earlier (1.1x)
            vol_spike = True
            if volumes and len(volumes) >= 11:
                avg_vol = sum(volumes[-11:-1]) / 10
                last_vol = volumes[-1]
                vol_spike = last_vol > 1.1 * avg_vol

            # MA cross
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

    # --------- Exit logic (safe) ----------
    def check_exit_condition(self, acc, current_price):
        try:
            entry_price = acc.get("buy_price", 0.0) or 0.0
            entry_time = acc.get("entry_time", time.time())
            if entry_price <= 0:
                self.log(f"⚠️ Invalid entry price for {acc.get('current_symbol')}, skipping exit check.")
                return False, 0.0, 0

            profit_pct = (current_price - entry_price) / (entry_price if entry_price != 0 else 0.0001) * 100
            elapsed = time.time() - entry_time

            # Stop loss first
            if profit_pct <= RISK_RULES["stop_loss"]:
                return True, profit_pct, elapsed

            # TP windows (TP1 first)
            if elapsed <= RISK_RULES["tp1"][0] and profit_pct >= RISK_RULES["tp1"][1]:
                return True, profit_pct, elapsed
            elif elapsed <= RISK_RULES["tp2"][0] and profit_pct >= RISK_RULES["tp2"][1]:
                return True, profit_pct, elapsed
            elif elapsed <= RISK_RULES["tp3"][0] and profit_pct >= RISK_RULES["tp3"][1]:
                return True, profit_pct, elapsed

            # Max hold
            if elapsed >= RISK_RULES["max_hold"]:
                return True, profit_pct, elapsed

            return False, profit_pct, elapsed
        except Exception as e:
            self.log(f"⚠️ Error in check_exit_condition {acc.get('current_symbol')}: {e}")
            return False, 0.0, 0

    # --------- Order helpers ----------
    def _get_symbol_info(self, session, symbol):
        try:
            resp = session.get_instruments_info(category="spot", symbol=symbol)
            res = resp.get("result", {})
            if isinstance(res, list) and res:
                return res[0]
            if isinstance(res, dict):
                return res
        except Exception:
            pass
        return None

    def _round_qty(self, qty, step=None):
        try:
            if not step or float(step) == 0:
                return round(qty, 6)
            step = float(step)
            decimals = max(0, int(round(-math.log10(step))))
            rounded = math.floor(qty / step) * step
            return round(rounded, decimals)
        except Exception:
            return round(qty, 6)

    def _place_market_buy(self, session, symbol, usdt_amount):
        try:
            price = self.get_ticker_last(session, symbol)
            if price is None or price <= 0:
                return False, {"error": "price unavailable"}, 0.0

            raw_qty = usdt_amount / price
            info = self._get_symbol_info(session, symbol)
            qty_step = None
            if info:
                qty_step = info.get("qtyStep") or info.get("lotSize") or info.get("basePrecision") or info.get("minOrderQty")
            qty = self._round_qty(raw_qty, qty_step)
            if qty <= 0:
                return False, {"error": "qty too small after rounding"}, 0.0

            resp = session.place_order(
                category="spot",
                symbol=symbol,
                orderType="Market",
                side="Buy",
                qty=str(qty)
            )
            return True, resp, float(qty)
        except Exception as e:
            self.log(f"⚠️ Error placing buy order {symbol}: {e}")
            return False, {"error": str(e)}, 0.0

    def _place_market_sell(self, session, symbol, qty):
        try:
            if qty <= 0:
                return False, {"error": "qty <= 0"}
            resp = session.place_order(
                category="spot",
                symbol=symbol,
                orderType="Market",
                side="Sell",
                qty=str(qty)
            )
            return True, resp
        except Exception as e:
            self.log(f"⚠️ Error placing sell order {symbol}: {e}")
            return False, {"error": str(e)}

    # --------- Selection ----------
    def select_best_coin(self, session):
        best_symbol = None
        best_price = None
        best_score = -math.inf

        for symbol in ALLOWED_COINS:
            try:
                current_price = self.get_ticker_last(session, symbol)
                if current_price is None:
                    continue
                if current_price > SCORE_SETTINGS["max_price_allowed"]:
                    continue

                candles = self.get_klines(session, symbol, interval="1", limit=30)
                if not candles or len(candles) < 6:
                    continue

                closes = [float(c[4]) for c in candles]
                volumes = [float(c[5]) for c in candles]

                momentum = self.compute_momentum_pct(closes, lookback=5) * SCORE_SETTINGS["momentum_scale"]
                rsi = self.compute_rsi(closes, period=14)
                rsi_score = 0.0
                if rsi is not None:
                    if rsi < SCORE_SETTINGS["rsi_oversold_threshold"]:
                        rsi_score = (SCORE_SETTINGS["rsi_oversold_threshold"] - rsi) * SCORE_SETTINGS["rsi_oversold_bonus_multiplier"]
                    elif rsi > SCORE_SETTINGS.get("rsi_overbought_threshold", 65):
                        rsi_score = - (rsi - SCORE_SETTINGS.get("rsi_overbought_threshold", 65)) * 0.5

                profit_potential = 10 if self.check_entry_condition(symbol, closes, current_price, volumes) else 0
                score = momentum + rsi_score + profit_potential

                self.log(f"Score {symbol}: momentum={momentum:.3f}, rsi={rsi if rsi is not None else 'N/A'}, rsi_score={rsi_score:.3f}, profit_pot={profit_potential} => total={score:.3f}")

                if score > best_score:
                    best_score = score
                    best_symbol = symbol
                    best_price = current_price
            except Exception as e:
                self.log(f"⚠️ Error scoring {symbol}: {e}")
                continue

        if best_symbol:
            self.log(f"✅ Best coin: {best_symbol} (score {round(best_score,2)}) at {best_price}")
            return best_symbol, best_price
        self.log("❌ No strong signals found across allowed coins.")
        return None, None

    # --------- Balance monitor ----------
    def balance_monitor(self, session, acc):
        while not self._stop_event.is_set() and acc.get("position") == "open":
            self.get_balance(session, acc)
            self._stop_event.wait(60)
        acc["monitoring"] = False
        self._update_account(acc)

    # --------- Account persistence helpers ----------
    def _update_account(self, acc):
        accounts = self.load_accounts()
        for i, a in enumerate(accounts):
            if a.get("key") == acc.get("key"):
                accounts[i] = acc
                break
        else:
            accounts.append(acc)
        self.save_accounts(accounts)

    # --------- Runner per account (real trading) ----------
    def run_account(self, acc):
        session = self._make_session(acc)
        while not self._stop_event.is_set():
            try:
                symbol, price = self.select_best_coin(session)
                if not symbol:
                    time.sleep(5)
                    continue

                balance = self.get_balance(session, acc) or 0.0
                allocation = TRADE_SETTINGS["trade_allocation_pct"]
                usdt_to_spend = max(0.0, balance * allocation)

                if usdt_to_spend <= 0:
                    self.log(f"⚠️ No USDT available for account {acc.get('key')[:6]}...")
                    time.sleep(10)
                    continue

                ok, resp, qty = self._place_market_buy(session, symbol, usdt_to_spend)
                if not ok:
                    self.log(f"❌ Buy failed for {symbol}: {resp}")
                    time.sleep(5)
                    continue

                # record position
                acc["position"] = "open"
                acc["current_symbol"] = symbol
                acc["buy_price"] = price
                acc["buy_qty"] = qty
                acc["entry_time"] = time.time()
                self._update_account(acc)
                self.log(f"🟢 Bought {symbol} qty={qty} (~{usdt_to_spend:.4f} USDT) at price {price:.6f}")

                # start balance monitor thread
                if not acc.get("monitoring"):
                    acc["monitoring"] = True
                    self._update_account(acc)
                    threading.Thread(target=self.balance_monitor, args=(session, acc), daemon=True).start()

                # hold loop
                while not self._stop_event.is_set() and acc.get("position") == "open":
                    current_price = self.get_ticker_last(session, symbol)
                    if current_price is None:
                        time.sleep(5)
                        continue

                    should_exit, profit_pct, elapsed = self.check_exit_condition(acc, current_price)
                    if should_exit:
                        sell_qty = acc.get("buy_qty", 0.0)
                        sell_ok, sell_resp = self._place_market_sell(session, symbol, sell_qty)
                        if not sell_ok:
                            self.log(f"⚠️ Sell failed for {symbol}: {sell_resp}. Will retry.")
                            time.sleep(3)
                            continue

                        acc["position"] = "closed"
                        acc["sell_price"] = current_price
                        self._update_account(acc)

                        exit_time = time.time()
                        hold_seconds = int(exit_time - acc["entry_time"])
                        trade = {
                            "id": str(uuid.uuid4()),
                            "account": acc.get("key")[:6] + "...",
                            "symbol": symbol,
                            "buy_price": acc.get("buy_price"),
                            "sell_price": current_price,
                            "pnl_pct": round(profit_pct, 2),
                            "entry_time": datetime.utcfromtimestamp(acc.get("entry_time")).strftime("%Y-%m-%d %H:%M:%S"),
                            "exit_time": datetime.utcfromtimestamp(exit_time).strftime("%Y-%m-%d %H:%M:%S"),
                            "hold_seconds": hold_seconds,
                            "qty": sell_qty
                        }
                        self.add_trade(trade)
                        self.log(f"🔴 Sold {symbol} qty={sell_qty} at {current_price:.6f} | PnL: {profit_pct:.2f}% | Held {hold_seconds}s")
                        break

                    time.sleep(5)

                # rest before next trade
                time.sleep(random.randint(60, 180))

            except Exception as e:
                self.log(f"run_account error: {e}")
                time.sleep(5)

    # --------- Control ----------
    def start(self):
        accounts = self.load_accounts()
        if not accounts:
            self.log("No accounts configured in accounts.json.")
            return
        self._stop_event.clear()
        for acc in accounts:
            acc.setdefault("position", "closed")
            acc.setdefault("monitoring", False)
            threading.Thread(target=self.run_account, args=(acc,), daemon=True).start()
        self.log("Bot started for all accounts.")

    def stop(self):
        self._stop_event.set()
        self.log("Stop signal set. Threads will exit shortly.")

# ---------------------- USAGE ----------------------
if __name__ == "__main__":
    bot = BotController()
    bot.log("Bot ready. Edit config at top of file before running live.")
    # call bot.start() from your server/dashboard when you want to run
