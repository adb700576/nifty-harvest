import os
import sys
import json
import time
import math
import hmac
import hashlib
import logging
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

# ==============================================================================
# 1. CONFIGURATION & ENVIRONMENT
# ==============================================================================
IST = timezone(timedelta(hours=5, minutes=30))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("V3_ENGINE")

UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
PAPER_MODE = os.getenv("PAPER_MODE", "true").strip().lower() == "true"

# --- UPDATED STRATEGY PARAMETERS ---
MIN_FUEL_SCORE = 55.0            # Updated from 70.0 -> 55.0
TRADE_START_TIME = "09:20"       # Continuous window start
TRADE_END_TIME = "15:10"         # Continuous window end (no new entries after 3:10 PM)
FORCE_EXIT_TIME = "15:15"        # Auto square-off all open positions at 3:15 PM

MAX_SPREAD_PCT = 1.50            # Bid-Ask spread protection (%)
SL_MIN_PCT = 0.12                # Dynamic SL floor (12%)
SL_MAX_PCT = 0.22                # Dynamic SL cap (22%)
RR_RATIO = 1.50                  # 1 : 1.5 Risk-to-Reward
SETUP_EXPIRY_SEC = 900           # 15-minute setup validity

CAPITAL_PER_TRADE = float(os.getenv("CAPITAL_PER_TRADE", "15000"))
DAILY_PROFIT_LOCK_PCT = 0.35     # +35% daily profit lock
DAILY_LOSS_LOCK_PCT = -0.25      # -25% daily loss lock
MAX_CONSECUTIVE_LOSSES = 2       # Stop trading after 2 consecutive losses

INDICES = {
    "NIFTY": {
        "spot_key": "NSE_INDEX|Nifty 50",
        "step": 50,
        "lot_size": 75,
        "freeze_qty": 1800,
    },
    "BANKNIFTY": {
        "spot_key": "NSE_INDEX|Nifty Bank",
        "step": 100,
        "lot_size": 30,
        "freeze_qty": 900,
    },
    "FINNIFTY": {
        "spot_key": "NSE_INDEX|Nifty Fin Service",
        "step": 50,
        "lot_size": 65,
        "freeze_qty": 1800,
    },
    "MIDCPNIFTY": {
        "spot_key": "NSE_INDEX|NIFTY MID SELECT",
        "step": 25,
        "lot_size": 120,
        "freeze_qty": 2800,
    },
    "SENSEX": {
        "spot_key": "BSE_INDEX|SENSEX",
        "step": 100,
        "lot_size": 20,
        "freeze_qty": 1000,
    },
}

# ==============================================================================
# 2. TELEGRAM & GITHUB MIRROR LOGGER
# ==============================================================================
def notify(msg: str, silent: bool = False):
    """Sends alert to Telegram AND mirrors clean log directly to GitHub Actions console."""
    clean_lines = [line.strip() for line in msg.splitlines() if line.strip()]
    mirror_text = " | ".join(clean_lines)
    log.info(f"[MIRROR] {mirror_text}")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
            "disable_notification": silent,
        }
        requests.post(url, json=payload, timeout=8)
    except Exception as e:
        log.warning(f"Telegram send error: {e}")


# ==============================================================================
# 3. UPSTOX V3 API HELPERS
# ==============================================================================
def upstox_headers():
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
    }


def fetch_intraday_5m(instrument_key: str) -> pd.DataFrame:
    """Fetches 5-minute intraday candles from Upstox V3."""
    encoded_key = requests.utils.quote(instrument_key, safe="")
    url = f"https://api.upstox.com/v3/historical-candle/intraday/{encoded_key}/minutes/5"
    try:
        resp = requests.get(url, headers=upstox_headers(), timeout=10)
        data = resp.json()
        candles = data.get("data", {}).get("candles", [])
        if not candles:
            return pd.DataFrame()
        df = pd.DataFrame(
            candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        for col in ["open", "high", "low", "close", "volume", "oi"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        return df
    except Exception as e:
        log.error(f"Candle fetch error ({instrument_key}): {e}")
        return pd.DataFrame()


def fetch_ltp_quote(instrument_keys: list) -> dict:
    """Fetches live LTP and depth quotes from Upstox."""
    if not instrument_keys:
        return {}
    try:
        url = "https://api.upstox.com/v2/market-quote/quotes"
        params = {"instrument_key": ",".join(instrument_keys)}
        resp = requests.get(url, headers=upstox_headers(), params=params, timeout=10)
        return resp.json().get("data", {})
    except Exception as e:
        log.error(f"Quote fetch error: {e}")
        return {}


def fetch_option_chain(spot_key: str, expiry_date: str) -> list:
    """Fetches live option chain for PCR & strike selection."""
    try:
        url = "https://api.upstox.com/v2/option/chain"
        params = {"instrument_key": spot_key, "expiry_date": expiry_date}
        resp = requests.get(url, headers=upstox_headers(), params=params, timeout=10)
        return resp.json().get("data", [])
    except Exception as e:
        log.error(f"Option chain fetch error ({spot_key}): {e}")
        return []


def get_nearest_expiry(spot_key: str) -> str:
    """Fetches nearest active expiry date for the index."""
    try:
        url = "https://api.upstox.com/v2/option/contract"
        params = {"instrument_key": spot_key}
        resp = requests.get(url, headers=upstox_headers(), params=params, timeout=10)
        contracts = resp.json().get("data", [])
        expiries = sorted({c.get("expiry") for c in contracts if c.get("expiry")})
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        valid = [e for e in expiries if e >= today_str]
        return valid[0] if valid else ""
    except Exception as e:
        log.error(f"Expiry fetch error ({spot_key}): {e}")
        return ""


# ==============================================================================
# 4. INDICATORS, GANN LATTICE & 5M EMA20 + FUEL SCORE (0-100)
# ==============================================================================
def compute_gann_levels(price: float, step: int) -> dict:
    """Computes Gann 0.50 Square-of-9 lattice levels around current spot."""
    root = math.sqrt(price)
    base_floor = math.floor(root * 2) / 2.0
    levels = [round((base_floor + k * 0.5) ** 2, 2) for k in range(-2, 4)]
    supports = [lvl for lvl in levels if lvl <= price]
    resistances = [lvl for lvl in levels if lvl > price]
    return {
        "support": supports[-1] if supports else round(price - step, 2),
        "resistance": resistances[0] if resistances else round(price + step, 2),
        "all": levels,
    }


def add_indicators_5m(df: pd.DataFrame) -> pd.DataFrame:
    """Computes 5M EMA20, VWAP, ATR(14), and 0-100 Fuel Score."""
    if df.empty or len(df) < 15:
        return df

    # 5-Minute 20 EMA (Replaced 15M EMA20 as requested)
    df["ema20_5m"] = df["close"].ewm(span=20, adjust=False).mean()

    # Session VWAP (uses typical price * volume; fallback to cumulative mean if index vol=0)
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    if df["volume"].sum() > 0:
        df["vwap_5m"] = (tp * df["volume"]).cumsum() / df["volume"].replace(0, np.nan).cumsum()
        df["vwap_5m"] = df["vwap_5m"].bfill().fillna(tp)
    else:
        df["vwap_5m"] = tp.expanding().mean()

    # True Range & ATR(14) / ATR(3)
    prev_close = df["close"].shift(1).fillna(df["open"])
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr14"] = tr.rolling(14, min_periods=1).mean()
    df["atr3"] = tr.rolling(3, min_periods=1).mean()

    # --- FUEL SCORE CALCULATION (0 to 100) ---
    fuel_scores = []
    for i in range(len(df)):
        row = df.iloc[i]
        atr14 = max(row["atr14"], 1e-6)
        atr3 = row["atr3"]

        # 1. Volume Surge (max 30 pts)
        if i >= 8 and df["volume"].iloc[i - 8 : i].median() > 0:
            med_vol = df["volume"].iloc[i - 8 : i].median()
            vol_ratio = row["volume"] / med_vol
            vol_pts = min(30.0, max(0.0, (vol_ratio - 0.8) * 25.0))
        else:
            # Index spot without volume gets proportional range-velocity credit
            vol_pts = min(30.0, max(0.0, ((atr3 / atr14) - 0.7) * 35.0))

        # 2. ATR / Range Expansion (max 25 pts)
        atr_ratio = atr3 / atr14
        atr_pts = min(25.0, max(0.0, (atr_ratio - 0.8) * 35.7))

        # 3. 5M EMA20 Slope Velocity (max 20 pts)
        ema_prev = df["ema20_5m"].iloc[max(0, i - 2)]
        slope_ratio = abs(row["ema20_5m"] - ema_prev) / atr14
        slope_pts = min(20.0, max(0.0, slope_ratio * 25.0))

        # 4. Candle Body Efficiency (max 15 pts)
        c_range = max(row["high"] - row["low"], 1e-6)
        body_eff = abs(row["close"] - row["open"]) / c_range
        eff_pts = min(15.0, max(0.0, body_eff * 15.0))

        # 5. Base (+10) & VWAP Over-Extension Penalty (up to -25)
        dist_vwap = abs(row["close"] - row["vwap_5m"]) / atr14
        penalty = min(25.0, max(0.0, (dist_vwap - 1.5) * 15.0))

        raw_fuel = 10.0 + vol_pts + atr_pts + slope_pts + eff_pts - penalty
        fuel_scores.append(round(min(100.0, max(0.0, raw_fuel)), 1))

    df["fuel"] = fuel_scores
    return df


# ==============================================================================
# 5. ORDER EXECUTION & POSITION MANAGEMENT
# ==============================================================================
def place_marketable_limit_order(instrument_key: str, qty: int, side: str, ltp: float) -> dict:
    """Places marketable limit order with 1.5% buffer to avoid freak fills."""
    buffer = 1.015 if side == "BUY" else 0.985
    limit_price = round(ltp * buffer, 1)

    if PAPER_MODE:
        log.info(f"[PAPER ORDER] {side} {qty} x {instrument_key} @ ₹{ltp:.2f} (Limit ₹{limit_price})")
        return {"status": "success", "fill_price": ltp, "order_id": f"PAPER_{int(time.time())}"}

    try:
        url = "https://api.upstox.com/v2/order/place"
        payload = {
            "quantity": qty,
            "product": "I",
            "validity": "DAY",
            "price": limit_price,
            "tag": "V3_GANN_ENGINE",
            "instrument_token": instrument_key,
            "order_type": "LIMIT",
            "transaction_type": side,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
        }
        resp = requests.post(url, headers=upstox_headers(), json=payload, timeout=10)
        res = resp.json()
        if res.get("status") == "success":
            return {"status": "success", "fill_price": ltp, "order_id": res["data"]["order_id"]}
        return {"status": "error", "message": str(res)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ==============================================================================
# 6. MAIN TRADING ENGINE LOOP
# ==============================================================================
class IntradayV3Engine:
    def __init__(self):
        self.expiries = {}
        self.pending_setups = {idx: None for idx in INDICES}
        self.active_positions = {}
        self.day_pnl = 0.0
        self.consecutive_losses = 0
        self.last_pulse_min = -1

    def init_expiries(self):
        for idx, cfg in INDICES.items():
            exp = get_nearest_expiry(cfg["spot_key"])
            self.expiries[idx] = exp
            log.info(f"[{idx}] Active Expiry: {exp}")

    def is_within_trade_window(self, now_ist: datetime) -> bool:
        hhmm = now_ist.strftime("%H:%M")
        return TRADE_START_TIME <= hhmm <= TRADE_END_TIME

    def evaluate_index(self, idx: str, cfg: dict, now_ist: datetime):
        df = fetch_intraday_5m(cfg["spot_key"])
        if df.empty or len(df) < 10:
            return None

        df = add_indicators_5m(df)
        curr = df.iloc[-1]
        prev = df.iloc[-2]

        spot_close = float(curr["close"])
        ema20_5m = float(curr["ema20_5m"])
        vwap_5m = float(curr["vwap_5m"])
        atr14 = max(float(curr["atr14"]), 1.0)
        fuel = float(curr["fuel"])

        gann = compute_gann_levels(spot_close, cfg["step"])
        sup = gann["support"]
        res = gann["resistance"]

        # Option Chain PCR
        pcr = 1.0
        chain = fetch_option_chain(cfg["spot_key"], self.expiries.get(idx, ""))
        if chain:
            total_pe_oi = sum(row.get("put_options", {}).get("market_data", {}).get("oi", 0) for row in chain)
            total_ce_oi = sum(row.get("call_options", {}).get("market_data", {}).get("oi", 0) for row in chain)
            if total_ce_oi > 0:
                pcr = round(total_pe_oi / total_ce_oi, 2)

        # --- CHECK PENDING SETUP EXPIRY OR TRIGGER ---
        pending = self.pending_setups.get(idx)
        if pending:
            elapsed = (now_ist - pending["armed_at"]).total_seconds()
            if elapsed > SETUP_EXPIRY_SEC:
                notify(f"[X] [{idx}] CANCELLED_SETUP_EXPIRED ({pending['name']})", silent=True)
                self.pending_setups[idx] = None
                pending = None
            elif pending["side"] == "CE" and spot_close >= pending["spot_t1"]:
                notify(f"[X] [{idx}] CANCELLED_T1_ALREADY_HIT ({pending['name']})", silent=True)
                self.pending_setups[idx] = None
                pending = None
            elif pending["side"] == "PE" and spot_close <= pending["spot_t1"]:
                notify(f"[X] [{idx}] CANCELLED_T1_ALREADY_HIT ({pending['name']})", silent=True)
                self.pending_setups[idx] = None
                pending = None

        # --- 5M EMA20 + VWAP + GANN SIGNAL DETECTION (NO 15M EMA20) ---
        bull_5m_ok = (spot_close > ema20_5m) and (spot_close > vwap_5m)
        bear_5m_ok = (spot_close < ema20_5m) and (spot_close < vwap_5m)

        new_signal = None
        if bull_5m_ok and prev["close"] <= res < spot_close:
            new_signal = {
                "side": "CE",
                "name": f"GANN_{int(res)}_CE",
                "trigger": round(spot_close, 2),
                "spot_t1": round(spot_close + 1.5 * atr14, 2),
                "armed_at": now_ist,
            }
        elif bear_5m_ok and prev["close"] >= sup > spot_close:
            new_signal = {
                "side": "PE",
                "name": f"GANN_{int(sup)}_PE",
                "trigger": round(spot_close, 2),
                "spot_t1": round(spot_close - 1.5 * atr14, 2),
                "armed_at": now_ist,
            }

        if new_signal and not pending:
            self.pending_setups[idx] = new_signal
            pending = new_signal
            if fuel < MIN_FUEL_SCORE:
                notify(
                    f"[!] [{idx}] {pending['side']} Armed "
                    f"[LOW_FUEL({int(fuel)}<{int(MIN_FUEL_SCORE)}) WAIT_RECLAIM]: "
                    f"{pending['name']} @ {pending['trigger']} (Spot T1: {pending['spot_t1']})",
                    silent=True,
                )

        # --- EXECUTE ENTRY IF ALL CONDITIONS SATISFIED (INCLUDING FUEL >= 55 & 5M EMA20) ---
        if pending and idx not in self.active_positions and self.is_within_trade_window(now_ist):
            dir_confirmed = (
                (pending["side"] == "CE" and bull_5m_ok and spot_close >= pending["trigger"])
                or (pending["side"] == "PE" and bear_5m_ok and spot_close <= pending["trigger"])
            )
            if dir_confirmed and fuel >= MIN_FUEL_SCORE:
                self.try_execute_entry(idx, cfg, pending, spot_close, fuel, pcr, chain, atr14)

        status_tag = "WATCHING👀"
        if idx in self.active_positions:
            status_tag = f"IN TRADE ({self.active_positions[idx]['side']})🔥"
        elif pending:
            status_tag = f"ARMED {pending['side']}🎯"
        elif bull_5m_ok:
            status_tag = "BULLISH🟢"
        elif bear_5m_ok:
            status_tag = "BEARISH🔴"

        return {
            "idx": idx,
            "spot": int(round(spot_close)),
            "fuel": int(round(fuel)),
            "pcr": pcr,
            "status": status_tag,
        }

    def try_execute_entry(self, idx, cfg, setup, spot_close, fuel, pcr, chain, atr14):
        if not chain:
            return
        atm_strike = round(spot_close / cfg["step"]) * cfg["step"]
        opt_field = "call_options" if setup["side"] == "CE" else "put_options"

        chosen = None
        for row in chain:
            if row.get("strike_price") == atm_strike:
                chosen = row.get(opt_field, {})
                break
        if not chosen:
            return

        inst_key = chosen.get("instrument_key")
        mkt = chosen.get("market_data", {})
        ltp = float(mkt.get("ltp", 0))
        bid = float(mkt.get("bid_price", 0))
        ask = float(mkt.get("ask_price", 0))

        if ltp <= 5.0:
            return

        # Bid-Ask Spread Protection
        if bid > 0 and ask > 0:
            spread_pct = ((ask - bid) / ltp) * 100.0
            if spread_pct > MAX_SPREAD_PCT:
                log.info(f"[{idx}] Entry skipped due to high spread: {spread_pct:.2f}%")
                return

        lots = max(1, int(CAPITAL_PER_TRADE // (ltp * cfg["lot_size"])))
        qty = min(lots * cfg["lot_size"], cfg["freeze_qty"])

        # Dynamic SL (12% to 22%) & 1:1.5 Target
        raw_sl_pct = min(SL_MAX_PCT, max(SL_MIN_PCT, (atr14 / spot_close) * 18.0))
        sl_price = round(ltp * (1.0 - raw_sl_pct), 1)
        tp_price = round(ltp * (1.0 + raw_sl_pct * RR_RATIO), 1)

        order_res = place_marketable_limit_order(inst_key, qty, "BUY", ltp)
        if order_res.get("status") == "success":
            fill = order_res["fill_price"]
            self.active_positions[idx] = {
                "instrument_key": inst_key,
                "strike": atm_strike,
                "side": setup["side"],
                "qty": qty,
                "entry_price": fill,
                "sl": sl_price,
                "tp": tp_price,
                "setup_name": setup["name"],
            }
            self.pending_setups[idx] = None
            notify(
                f"🚀 <b>[{idx}] ENTRY TRIGGERED ({setup['name']})</b>\n"
                f"Strike: <b>{atm_strike} {setup['side']}</b> | Qty: {qty}\n"
                f"Entry: ₹{fill:.2f} | SL: ₹{sl_price:.2f} | Target: ₹{tp_price:.2f}\n"
                f"Spot: {spot_close:.2f} | Fuel: {fuel} | PCR: {pcr}"
            )

    def manage_open_positions(self, force_exit: bool = False):
        if not self.active_positions:
            return
        keys = [p["instrument_key"] for p in self.active_positions.values()]
        quotes = fetch_ltp_quote(keys)

        for idx in list(self.active_positions.keys()):
            pos = self.active_positions[idx]
            q_data = quotes.get(pos["instrument_key"], {})
            ltp = float(q_data.get("last_price", pos["entry_price"]))

            exit_reason = None
            if force_exit:
                exit_reason = "EOD_AUTO_EXIT (15:15)"
            elif ltp <= pos["sl"]:
                exit_reason = "STOP_LOSS_HIT"
            elif ltp >= pos["tp"]:
                exit_reason = "TARGET_1_HIT"

            if exit_reason:
                place_marketable_limit_order(pos["instrument_key"], pos["qty"], "SELL", ltp)
                pnl = (ltp - pos["entry_price"]) * pos["qty"]
                self.day_pnl += pnl
                if pnl < 0:
                    self.consecutive_losses += 1
                else:
                    self.consecutive_losses = 0

                del self.active_positions[idx]
                notify(
                    f"🏁 <b>[{idx}] EXIT ({exit_reason})</b>\n"
                    f"Strike: {pos['strike']} {pos['side']} | Exit: ₹{ltp:.2f}\n"
                    f"Trade PnL: <b>₹{pnl:+.2f}</b> | Day PnL: <b>₹{self.day_pnl:+.2f}</b>"
                )

    def run(self):
        if not UPSTOX_ACCESS_TOKEN:
            log.error("UPSTOX_ACCESS_TOKEN missing! Exiting.")
            sys.exit(1)

        self.init_expiries()
        notify(
            f"⚡ <b>V3 ENGINE STARTED</b> ({'PAPER' if PAPER_MODE else 'LIVE'})\n"
            f"Window: {TRADE_START_TIME}–{TRADE_END_TIME} IST | 5M EMA20 | Min Fuel: {int(MIN_FUEL_SCORE)}"
        )

        while True:
            now_ist = datetime.now(IST)
            hhmm = now_ist.strftime("%H:%M")

            # Force square-off at 15:15 IST and stop engine at 15:25 IST
            if hhmm >= FORCE_EXIT_TIME:
                self.manage_open_positions(force_exit=True)
                if hhmm >= "15:25":
                    notify(f"🛑 <b>SESSION CLOSED</b> | Final Day PnL: <b>₹{self.day_pnl:+.2f}</b>")
                    break

            # Daily Risk Locks
            max_profit = CAPITAL_PER_TRADE * DAILY_PROFIT_LOCK_PCT
            max_loss = CAPITAL_PER_TRADE * DAILY_LOSS_LOCK_PCT
            risk_locked = (
                self.day_pnl >= max_profit
                or self.day_pnl <= max_loss
                or self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES
            )

            self.manage_open_positions(force_exit=False)

            summaries = []
            if not risk_locked:
                for idx, cfg in INDICES.items():
                    res = self.evaluate_index(idx, cfg, now_ist)
                    if res:
                        summaries.append(res)

            # 5-Minute Heartbeat Pulse
            if now_ist.minute % 5 == 0 and now_ist.minute != self.last_pulse_min and summaries:
                self.last_pulse_min = now_ist.minute
                win_label = "ACTIVE 🟢" if self.is_within_trade_window(now_ist) else "OUTSIDE WINDOW ⏸️"
                lines = [
                    f"📊 <b>5-INDEX V3 PULSE ({hhmm} IST | {win_label})</b>",
                    f"💰 Day PnL: <b>₹{self.day_pnl:+.2f}</b> | Fuel Gate: ≥{int(MIN_FUEL_SCORE)} (5M EMA20)",
                ]
                for s in summaries:
                    lines.append(
                        f"• <b>{s['idx']}</b>: {s['spot']} | Fuel {s['fuel']} | PCR {s['pcr']} | {s['status']}"
                    )
                notify("\n".join(lines), silent=True)

            time.sleep(20)


if __name__ == "__main__":
    engine = IntradayV3Engine()
    engine.run()
