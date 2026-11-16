import os
import sys
import time
import math
import logging
import requests
import pandas as pd
import numpy as np
from collections import deque
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

# --- STRATEGY PARAMETERS (6 CORE FILTERS + WICK BONUS + 1.2x PREV-20 ATR) ---
MIN_FUEL_SCORE = 50.0            # Optimized for Hammer / Reverse Hammer & Star reversals
TRADE_START_TIME = "09:20"       # Continuous window start
TRADE_END_TIME = "15:10"         # Continuous window end
FORCE_EXIT_TIME = "15:15"        # Auto square-off at 3:15 PM

GANN_ZONE_ATR_MULT = 0.40        # Allowed proximity above/below Gann level
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
# 3. UPSTOX V3 API HELPERS (SPOT + FUTURES VOLUME + OPTION CHAIN OI)
# ==============================================================================
def upstox_headers():
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
    }


def fetch_intraday_5m(instrument_key: str) -> pd.DataFrame:
    if not instrument_key:
        return pd.DataFrame()
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
    try:
        url = "https://api.upstox.com/v2/option/chain"
        params = {"instrument_key": spot_key, "expiry_date": expiry_date}
        resp = requests.get(url, headers=upstox_headers(), params=params, timeout=10)
        return resp.json().get("data", [])
    except Exception as e:
        log.error(f"Option chain fetch error ({spot_key}): {e}")
        return []


def get_nearest_expiry(spot_key: str) -> str:
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


def get_active_future_key(idx: str) -> str:
    """Resolves current month active Future instrument_key for real Volume in Fuel Score."""
    symbol_map = {
        "NIFTY": "NIFTY",
        "BANKNIFTY": "BANKNIFTY",
        "FINNIFTY": "FINNIFTY",
        "MIDCPNIFTY": "MIDCPNIFTY",
        "SENSEX": "SENSEX",
    }
    sym = symbol_map.get(idx, idx)
    exchange = "BSE" if idx == "SENSEX" else "NSE"
    try:
        inst_url = f"https://assets.upstox.com/market-quote/instruments/exchange/{exchange}.json.gz"
        df_inst = pd.read_json(inst_url)
        futs = df_inst[
            (df_inst["instrument_type"] == "FUT")
            & (df_inst["name"].str.upper().str.contains(sym, na=False))
        ].copy()
        if futs.empty:
            return ""
        futs = futs.sort_values("expiry")
        return str(futs.iloc[0]["instrument_key"])
    except Exception as e:
        log.warning(f"[{idx}] Future key lookup fallback ({e})")
        return ""


# ==============================================================================
# 4. GANN LEVELS, 6 CANDLE PATTERNS & FUEL SCORE (WICK + 1.2x PREV-20 ATR)
# ==============================================================================
def compute_gann_levels(price: float, step: int) -> dict:
    """Computes Gann 0.50 Square-of-9 lattice levels around current spot."""
    root = math.sqrt(price)
    base_floor = math.floor(root * 2) / 2.0
    levels = [round((base_floor + k * 0.5) ** 2, 2) for k in range(-3, 5)]
    supports = [lvl for lvl in levels if lvl <= price]
    resistances = [lvl for lvl in levels if lvl > price]
    return {
        "support": supports[-1] if supports else round(price - step, 2),
        "resistance": resistances[0] if resistances else round(price + step, 2),
        "all": levels,
    }


def detect_6_gann_patterns(df: pd.DataFrame, gann_levels: list, atr14: float) -> dict:
    """
    Checks ONLY the 6 requested patterns near Gann levels (above or below within 0.40 ATR):
    Bullish (CE): Morning Star, Hammer, Bullish Engulfing
    Bearish (PE): Evening Star, Reverse Hammer (Shooting Star), Bearish Engulfing
    """
    if len(df) < 3:
        return {"near_gann": None, "bull_pattern": None, "bear_pattern": None}

    c1 = df.iloc[-3]  # 2 candles ago (for Morning/Evening Star)
    c2 = df.iloc[-2]  # Previous candle
    c3 = df.iloc[-1]  # Current candle

    o1, h1, l1, cl1 = float(c1["open"]), float(c1["high"]), float(c1["low"]), float(c1["close"])
    o2, h2, l2, cl2 = float(c2["open"]), float(c2["high"]), float(c2["low"]), float(c2["close"])
    o3, h3, l3, cl3 = float(c3["open"]), float(c3["high"]), float(c3["low"]), float(c3["close"])

    zone_band = atr14 * GANN_ZONE_ATR_MULT
    near_gann = None
    min_dist = float("inf")
    for lvl in gann_levels:
        dist = min(abs(cl3 - lvl), abs(l3 - lvl), abs(h3 - lvl), abs(l2 - lvl), abs(h2 - lvl))
        if dist <= zone_band and dist < min_dist:
            min_dist = dist
            near_gann = lvl

    if near_gann is None:
        return {"near_gann": None, "bull_pattern": None, "bear_pattern": None}

    r1, b1 = max(h1 - l1, 1e-6), abs(cl1 - o1)
    r2, b2 = max(h2 - l2, 1e-6), abs(cl2 - o2)
    r3, b3 = max(h3 - l3, 1e-6), abs(cl3 - o3)

    upper_wick3 = h3 - max(o3, cl3)
    lower_wick3 = min(o3, cl3) - l3
    mid1 = (o1 + cl1) / 2.0

    # --- 3 BULLISH PATTERNS (CE) ---
    bull_pattern = None
    is_bull_engulf = (cl3 > o3) and (cl2 < o2) and (cl3 >= o2) and (o3 <= cl2)
    is_hammer = (lower_wick3 >= 1.8 * max(b3, r3 * 0.1)) and (upper_wick3 <= 0.25 * r3) and ((cl3 - l3) / r3 >= 0.60)
    is_morning_star = (
        (cl1 < o1) and (b1 / r1 >= 0.45)
        and (b2 / r2 <= 0.40)
        and (cl3 > o3) and (cl3 > mid1)
    )

    if is_bull_engulf:
        bull_pattern = "BULLISH_ENGULFING"
    elif is_morning_star:
        bull_pattern = "MORNING_STAR"
    elif is_hammer:
        bull_pattern = "HAMMER"

    # --- 3 BEARISH PATTERNS (PE) ---
    bear_pattern = None
    is_bear_engulf = (cl3 < o3) and (cl2 > o2) and (cl3 <= o2) and (o3 >= cl2)
    is_reverse_hammer = (upper_wick3 >= 1.8 * max(b3, r3 * 0.1)) and (lower_wick3 <= 0.25 * r3) and ((h3 - cl3) / r3 >= 0.60)
    is_evening_star = (
        (cl1 > o1) and (b1 / r1 >= 0.45)
        and (b2 / r2 <= 0.40)
        and (cl3 < o3) and (cl3 < mid1)
    )

    if is_bear_engulf:
        bear_pattern = "BEARISH_ENGULFING"
    elif is_evening_star:
        bear_pattern = "EVENING_STAR"
    elif is_reverse_hammer:
        bear_pattern = "REVERSE_HAMMER"

    return {
        "near_gann": near_gann,
        "bull_pattern": bull_pattern,
        "bear_pattern": bear_pattern,
    }


def compute_fuel_with_fut_volume(spot_df: pd.DataFrame, fut_df: pd.DataFrame) -> pd.DataFrame:
    """
    Computes 0-100 Fuel Score using:
    1. Real Futures Volume Surge (max 40 pts)
    2. Current Candle TR >= 1.2 * Previous 20-Candle ATR Expansion (max 30 pts)
    3. Wick Rejection Bonus (Hammer/Reverse Hammer) OR Strong Body (Engulfing) (max 20 pts)
    """
    if spot_df.empty or len(spot_df) < 10:
        return spot_df

    prev_close = spot_df["close"].shift(1).fillna(spot_df["open"])
    tr = pd.concat(
        [
            spot_df["high"] - spot_df["low"],
            (spot_df["high"] - prev_close).abs(),
            (spot_df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    spot_df["tr"] = tr
    spot_df["atr14"] = tr.rolling(14, min_periods=1).mean()
    # Previous 20 candles average ATR (shifted by 1 so current candle is compared against prior 20)
    spot_df["prev20_atr"] = tr.rolling(20, min_periods=1).mean().shift(1).fillna(tr)

    # Merge real Futures volume if available
    if not fut_df.empty and "volume" in fut_df.columns and fut_df["volume"].sum() > 0:
        fut_vol = fut_df[["timestamp", "volume"]].rename(columns={"volume": "fut_vol"})
        spot_df = pd.merge_asof(
            spot_df.sort_values("timestamp"),
            fut_vol.sort_values("timestamp"),
            on="timestamp",
            direction="nearest",
        )
        spot_df["active_vol"] = spot_df["fut_vol"].fillna(0.0)
    else:
        spot_df["active_vol"] = spot_df["volume"]

    fuel_scores = []
    for i in range(len(spot_df)):
        row = spot_df.iloc[i]
        prev20_atr = max(row["prev20_atr"], 1e-6)
        curr_tr = row["tr"]

        # 1. Real Futures Volume Surge (max 40 pts)
        if i >= 6 and spot_df["active_vol"].iloc[i - 6 : i].median() > 0:
            med_vol = spot_df["active_vol"].iloc[i - 6 : i].median()
            vol_ratio = row["active_vol"] / med_vol
            vol_pts = min(40.0, max(0.0, (vol_ratio - 0.75) * 32.0))
        else:
            vol_pts = min(35.0, max(0.0, ((curr_tr / prev20_atr) - 0.7) * 35.0))

        # 2. ATR Expansion: Full 30 pts when Current Candle TR >= 1.2 * Previous 20-Candle ATR
        atr_ratio = curr_tr / prev20_atr
        if atr_ratio >= 1.2:
            atr_pts = 30.0
        else:
            atr_pts = min(29.0, max(0.0, (atr_ratio / 1.2) * 28.0))

        # 3. Wick Rejection Bonus (Hammer/Reverse Hammer) OR Strong Body (Engulfing) (max 20 pts)
        c_range = max(row["high"] - row["low"], 1e-6)
        body = abs(row["close"] - row["open"])
        upper_wick = row["high"] - max(row["open"], row["close"])
        lower_wick = min(row["open"], row["close"]) - row["low"]

        pa_strength = max(body / c_range, max(upper_wick, lower_wick) / c_range)
        eff_pts = min(20.0, max(0.0, pa_strength * 20.0))

        raw_fuel = 10.0 + vol_pts + atr_pts + eff_pts
        fuel_scores.append(round(min(100.0, max(0.0, raw_fuel)), 1))

    spot_df["fuel"] = fuel_scores
    return spot_df


# ==============================================================================
# 5. ORDER EXECUTION & POSITION MANAGEMENT
# ==============================================================================
def place_marketable_limit_order(instrument_key: str, qty: int, side: str, ltp: float) -> dict:
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
        self.fut_keys = {}
        self.pending_setups = {idx: None for idx in INDICES}
        self.pcr_history = {idx: deque(maxlen=5) for idx in INDICES}
        self.oi_history = {idx: None for idx in INDICES}
        self.active_positions = {}
        self.day_pnl = 0.0
        self.consecutive_losses = 0
        self.last_pulse_min = -1

    def init_contracts(self):
        for idx, cfg in INDICES.items():
            exp = get_nearest_expiry(cfg["spot_key"])
            fut = get_active_future_key(idx)
            self.expiries[idx] = exp
            self.fut_keys[idx] = fut
            log.info(f"[{idx}] Expiry: {exp} | Future Key: {fut or 'SPOT_FALLBACK'}")

    def is_within_trade_window(self, now_ist: datetime) -> bool:
        hhmm = now_ist.strftime("%H:%M")
        return TRADE_START_TIME <= hhmm <= TRADE_END_TIME

    def analyze_oi_and_pcr(self, idx: str, chain: list):
        """
        Tracks:
        1. PCR gradually increasing (CE) or decreasing (PE)
        2. Change in OI (Put OI build-up for CE vs Call OI build-up for PE)
        """
        if not chain:
            return 1.0, 0.0, False, False

        total_pe_oi = sum(float(r.get("put_options", {}).get("market_data", {}).get("oi", 0)) for r in chain)
        total_ce_oi = sum(float(r.get("call_options", {}).get("market_data", {}).get("oi", 0)) for r in chain)
        pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 1.0

        hist = self.pcr_history[idx]
        if not hist or hist[-1] != pcr:
            hist.append(pcr)

        pcr_slope = round(pcr - hist[0], 3) if len(hist) >= 2 else 0.0
        pcr_increasing = (len(hist) >= 2 and hist[-1] > hist[-2]) or (pcr >= 1.05)
        pcr_decreasing = (len(hist) >= 2 and hist[-1] < hist[-2]) or (pcr <= 0.95)

        prev_oi = self.oi_history[idx]
        oi_bull_ok = True
        oi_bear_ok = True
        if prev_oi is not None:
            delta_pe_oi = total_pe_oi - prev_oi["pe_oi"]
            delta_ce_oi = total_ce_oi - prev_oi["ce_oi"]
            oi_bull_ok = (delta_pe_oi >= delta_ce_oi) or (delta_ce_oi < 0)
            oi_bear_ok = (delta_ce_oi >= delta_pe_oi) or (delta_pe_oi < 0)

        self.oi_history[idx] = {"pe_oi": total_pe_oi, "ce_oi": total_ce_oi}
        return pcr, pcr_slope, (pcr_increasing and oi_bull_ok), (pcr_decreasing and oi_bear_ok)

    def evaluate_index(self, idx: str, cfg: dict, now_ist: datetime):
        spot_df = fetch_intraday_5m(cfg["spot_key"])
        if spot_df.empty or len(spot_df) < 10:
            return None

        fut_df = fetch_intraday_5m(self.fut_keys.get(idx, ""))
        df = compute_fuel_with_fut_volume(spot_df, fut_df)

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        spot_close = float(curr["close"])
        prev_close = float(prev["close"])
        price_change = round(spot_close - prev_close, 2)
        atr14 = max(float(curr["atr14"]), 1.0)
        fuel = float(curr["fuel"])

        # 1. Gann Levels & 6 Candle Patterns
        gann = compute_gann_levels(spot_close, cfg["step"])
        sup = gann["support"]
        res = gann["resistance"]
        gann_levels = gann["all"]

        pat_info = detect_6_gann_patterns(df, gann_levels, atr14)
        near_gann = pat_info["near_gann"]
        bull_pattern = pat_info["bull_pattern"]
        bear_pattern = pat_info["bear_pattern"]

        # 2. Option Chain: Change in OI + PCR Increase/Decrease
        chain = fetch_option_chain(cfg["spot_key"], self.expiries.get(idx, ""))
        pcr, pcr_slope, oi_pcr_bull_ok, oi_pcr_bear_ok = self.analyze_oi_and_pcr(idx, chain)

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

        # --- PURE 6-FACTOR SIGNAL GENERATION ---
        new_signal = None
        if near_gann is not None and bull_pattern is not None and price_change > 0 and oi_pcr_bull_ok:
            next_res = next((lvl for lvl in gann_levels if lvl > spot_close), res)
            t1_target = round(max(spot_close + 1.2 * atr14, next_res), 2)
            new_signal = {
                "side": "CE",
                "name": f"GANN_{int(near_gann)}_{bull_pattern}_CE",
                "gann_level": near_gann,
                "pattern": bull_pattern,
                "trigger": round(spot_close, 2),
                "spot_t1": t1_target,
                "armed_at": now_ist,
            }
        elif near_gann is not None and bear_pattern is not None and price_change < 0 and oi_pcr_bear_ok:
            next_sup = next((lvl for lvl in reversed(gann_levels) if lvl < spot_close), sup)
            t1_target = round(min(spot_close - 1.2 * atr14, next_sup), 2)
            new_signal = {
                "side": "PE",
                "name": f"GANN_{int(near_gann)}_{bear_pattern}_PE",
                "gann_level": near_gann,
                "pattern": bear_pattern,
                "trigger": round(spot_close, 2),
                "spot_t1": t1_target,
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

        if pending and idx not in self.active_positions and self.is_within_trade_window(now_ist):
            dir_confirmed = (
                (pending["side"] == "CE" and price_change > 0 and oi_pcr_bull_ok and spot_close >= pending["trigger"])
                or (pending["side"] == "PE" and price_change < 0 and oi_pcr_bear_ok and spot_close <= pending["trigger"])
            )
            if dir_confirmed and fuel >= MIN_FUEL_SCORE:
                self.try_execute_entry(idx, cfg, pending, spot_close, price_change, fuel, pcr, pcr_slope, chain, atr14)

        status_tag = f"WATCHING👀 (Gann {int(sup)}-{int(res)})"
        if idx in self.active_positions:
            status_tag = f"IN TRADE ({self.active_positions[idx]['side']})🔥"
        elif pending:
            status_tag = f"ARMED {pending['side']} ({pending['pattern']})🎯"
        elif price_change > 0:
            status_tag = f"PRICE+ ({price_change:+.1f} | Gann {int(sup)}-{int(res)})🟢"
        elif price_change < 0:
            status_tag = f"PRICE- ({price_change:+.1f} | Gann {int(sup)}-{int(res)})🔴"

        return {
            "idx": idx,
            "spot": int(round(spot_close)),
            "fuel": int(round(fuel)),
            "pcr": pcr,
            "pcr_slope": pcr_slope,
            "status": status_tag,
        }

    def try_execute_entry(self, idx, cfg, setup, spot_close, price_change, fuel, pcr, pcr_slope, chain, atr14):
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

        if bid > 0 and ask > 0:
            spread_pct = ((ask - bid) / ltp) * 100.0
            if spread_pct > MAX_SPREAD_PCT:
                log.info(f"[{idx}] Entry skipped due to high spread: {spread_pct:.2f}%")
                return

        lots = max(1, int(CAPITAL_PER_TRADE // (ltp * cfg["lot_size"])))
        qty = min(lots * cfg["lot_size"], cfg["freeze_qty"])

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
                f"Spot: {spot_close:.2f} ({price_change:+.1f}) | Gann: {setup['gann_level']} ({setup['pattern']})\n"
                f"Fuel (FUT+Wick+ATR): {fuel} | PCR: {pcr} ({pcr_slope:+.2f})"
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

        self.init_contracts()
        notify(
            f"⚡ <b>V3 ENGINE STARTED</b> ({'PAPER' if PAPER_MODE else 'LIVE'})\n"
            f"Window: {TRADE_START_TIME}–{TRADE_END_TIME} IST | Gann + 6 Patterns + Fuel(Wick+1.2xATR) ≥{int(MIN_FUEL_SCORE)} + OI/PCR"
        )

        while True:
            now_ist = datetime.now(IST)
            hhmm = now_ist.strftime("%H:%M")

            if hhmm >= FORCE_EXIT_TIME:
                self.manage_open_positions(force_exit=True)
                if hhmm >= "15:25":
                    notify(f"🛑 <b>SESSION CLOSED</b> | Final Day PnL: <b>₹{self.day_pnl:+.2f}</b>")
                    break

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

            if now_ist.minute % 5 == 0 and now_ist.minute != self.last_pulse_min and summaries:
                self.last_pulse_min = now_ist.minute
                win_label = "ACTIVE 🟢" if self.is_within_trade_window(now_ist) else "OUTSIDE WINDOW ⏸️"
                lines = [
                    f"📊 <b>5-INDEX V3 PULSE ({hhmm} IST | {win_label})</b>",
                    f"💰 Day PnL: <b>₹{self.day_pnl:+.2f}</b> | Gann + 6 Patterns + Fuel ≥{int(MIN_FUEL_SCORE)} + OI/PCR",
                ]
                for s in summaries:
                    lines.append(
                        f"• <b>{s['idx']}</b>: {s['spot']} | Fuel {s['fuel']} | PCR {s['pcr']} ({s['pcr_slope']:+.2f}) | {s['status']}"
                    )
                notify("\n".join(lines), silent=True)

            time.sleep(20)


if __name__ == "__main__":
    engine = IntradayV3Engine()
    engine.run()
