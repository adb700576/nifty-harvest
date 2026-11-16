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

# --- STRATEGY PARAMETERS ---
MIN_FUEL_SCORE = 45.0            # 45 and above for all instruments
INDEX_START_TIME = "09:20"
INDEX_END_TIME = "15:10"
INDEX_EXIT_TIME = "15:15"

MCX_START_TIME = "09:20"
MCX_END_TIME = "23:00"
MCX_EXIT_TIME = "23:15"

GANN_ZONE_ATR_MULT = 0.40        # Allowed proximity above/below Gann level
MAX_SPREAD_PCT = 1.50            # Bid-Ask spread protection (%)
SL_MIN_PCT = 0.12                # Options SL floor (12%)
SL_MAX_PCT = 0.22                # Options SL cap (22%)
FUT_SL_ATR_MULT = 1.00           # Crude Oil Futures SL = 1.0 * ATR(14)
RR_RATIO = 1.50                  # 1 : 1.5 Risk-to-Reward
SETUP_EXPIRY_SEC = 900           # 15-minute setup validity

CAPITAL_PER_TRADE = float(os.getenv("CAPITAL_PER_TRADE", "15000"))
CRUDEOIL_CAPITAL = float(os.getenv("CRUDEOIL_CAPITAL", "350000"))
DAILY_PROFIT_LOCK_PCT = 0.35
DAILY_LOSS_LOCK_PCT = -0.25
MAX_CONSECUTIVE_LOSSES = 2

INDICES = {
    "NIFTY": {
        "type": "INDEX_OPT",
        "spot_key": "NSE_INDEX|Nifty 50",
        "step": 50,
        "lot_size": 75,
        "freeze_qty": 1800,
    },
    "BANKNIFTY": {
        "type": "INDEX_OPT",
        "spot_key": "NSE_INDEX|Nifty Bank",
        "step": 100,
        "lot_size": 30,
        "freeze_qty": 900,
    },
    "FINNIFTY": {
        "type": "INDEX_OPT",
        "spot_key": "NSE_INDEX|Nifty Fin Service",
        "step": 50,
        "lot_size": 65,
        "freeze_qty": 1800,
    },
    "MIDCPNIFTY": {
        "type": "INDEX_OPT",
        "spot_key": "NSE_INDEX|NIFTY MID SELECT",
        "step": 25,
        "lot_size": 120,
        "freeze_qty": 2800,
    },
    "SENSEX": {
        "type": "INDEX_OPT",
        "spot_key": "BSE_INDEX|SENSEX",
        "step": 100,
        "lot_size": 20,
        "freeze_qty": 1000,
    },
    "CRUDEOIL": {
        "type": "MCX_FUT",
        "spot_key": "",          # Resolved dynamically via /v2/instruments/search
        "step": 50,
        "lot_size": 100,
        "freeze_qty": 10000,
    },
}

# ==============================================================================
# 2. TELEGRAM & GITHUB MIRROR LOGGER (WITH 2-ATTEMPT AUTO-RETRY)
# ==============================================================================
def notify(msg: str, silent: bool = False):
    clean_lines = [line.strip() for line in msg.splitlines() if line.strip()]
    mirror_text = " | ".join(clean_lines)
    log.info(f"[MIRROR] {mirror_text}")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
        "disable_notification": silent,
    }
    for attempt in range(2):
        try:
            requests.post(url, json=payload, timeout=8)
            return
        except Exception as e:
            if attempt == 0:
                time.sleep(0.5)
            else:
                log.warning(f"Telegram send error after retry: {e}")


# ==============================================================================
# 3. UPSTOX V3 API HELPERS (INSTRUMENT SEARCH + CANDLES + MARGIN + OPTION CHAIN)
# ==============================================================================
def upstox_headers():
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def get_active_future_contract(idx: str) -> dict:
    """
    Uses Upstox GET /v2/instruments/search API to fetch the exact active
    current-month Future contract (instrument_key, lot_size, trading_symbol, expiry).
    """
    exchange = "BSE" if idx == "SENSEX" else ("MCX" if idx == "CRUDEOIL" else "NSE")
    url = "https://api.upstox.com/v2/instruments/search"

    for expiry_kw in ("current_month", "next_month"):
        try:
            params = {
                "query": idx,
                "exchanges": exchange,
                "segments": "FUT",
                "expiry": expiry_kw,
                "page_number": 1,
                "records": 20,
            }
            resp = requests.get(url, headers=upstox_headers(), params=params, timeout=10)
            res = resp.json()
            items = res.get("data", [])
            if not items:
                continue

            exact = [
                it for it in items
                if str(it.get("instrument_type", "")).upper() == "FUT"
                and (
                    str(it.get("underlying_symbol", "")).upper() == idx
                    or str(it.get("name", "")).upper() == idx
                    or str(it.get("trading_symbol", "")).upper().startswith(f"{idx} ")
                )
            ]
            candidates = exact if exact else [
                it for it in items if str(it.get("instrument_type", "")).upper() == "FUT"
            ]
            if not candidates:
                continue

            candidates.sort(key=lambda x: str(x.get("expiry", "9999-12-31")))
            top = candidates[0]
            return {
                "instrument_key": str(top.get("instrument_key", "")),
                "lot_size": int(top.get("lot_size", 100)),
                "freeze_quantity": int(top.get("freeze_quantity", 10000)),
                "trading_symbol": str(top.get("trading_symbol", idx)),
                "expiry": str(top.get("expiry", "")),
            }
        except Exception as e:
            log.warning(f"[{idx}] Instrument search error ({expiry_kw}): {e}")

    return {}


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


def fetch_required_margin(instrument_key: str, lots: int, side: str, product: str = "I") -> float:
    """
    Calls Upstox POST /v2/charges/margin API.
    For MCX Futures, Upstox expects number of lots (e.g. quantity=1 for 1 lot).
    """
    try:
        url = "https://api.upstox.com/v2/charges/margin"
        payload = {
            "instruments": [
                {
                    "instrument_key": instrument_key,
                    "quantity": int(max(1, lots)),
                    "transaction_type": side,
                    "product": product,
                }
            ]
        }
        resp = requests.post(url, headers=upstox_headers(), json=payload, timeout=10)
        data = resp.json()
        if data.get("status") == "success":
            return float(data.get("data", {}).get("required_margin", 0.0))
        log.warning(f"Margin API warning ({instrument_key}): {data}")
        return 0.0
    except Exception as e:
        log.warning(f"Margin API error ({instrument_key}): {e}")
        return 0.0


def fetch_option_chain(spot_key: str, expiry_date: str) -> list:
    if not spot_key or not expiry_date:
        return []
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


# ==============================================================================
# 4. GANN LEVELS, 6 CANDLE PATTERNS & FUEL SCORE (WICK + 1.2x PREV-20 ATR)
# ==============================================================================
def compute_gann_levels(price: float, step: int) -> dict:
    """Computes Gann 0.50 Square-of-9 lattice levels around current price."""
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
    Bullish (CE / BUY): Morning Star, Hammer, Bullish Engulfing
    Bearish (PE / SELL): Evening Star, Reverse Hammer (Shooting Star), Bearish Engulfing
    """
    if len(df) < 3:
        return {"near_gann": None, "bull_pattern": None, "bear_pattern": None}

    c1 = df.iloc[-3]
    c2 = df.iloc[-2]
    c3 = df.iloc[-1]

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

    # --- 3 BULLISH PATTERNS (BUY / CE) ---
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

    # --- 3 BEARISH PATTERNS (SELL / PE) ---
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
    spot_df["prev20_atr"] = tr.rolling(20, min_periods=1).mean().shift(1).fillna(tr)

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
    is_mcx = instrument_key.startswith("MCX_FO")
    buffer = (1.003 if is_mcx else 1.015) if side == "BUY" else (0.997 if is_mcx else 0.985)
    limit_price = round(ltp * buffer, 1 if not is_mcx else 0)

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
        self.fut_contracts = {}
        self.pending_setups = {idx: None for idx in INDICES}
        self.open_pcr = {idx: None for idx in INDICES}
        self.pcr_history = {idx: deque(maxlen=5) for idx in INDICES}
        self.oi_history = {idx: None for idx in INDICES}
        self.active_positions = {}
        self.day_pnl = 0.0
        self.consecutive_losses = 0
        self.last_pulse_min = -1

    def init_contracts(self):
        for idx, cfg in INDICES.items():
            fut_info = get_active_future_contract(idx)
            self.fut_contracts[idx] = fut_info
            if cfg["type"] == "MCX_FUT":
                cfg["spot_key"] = fut_info.get("instrument_key", "")
                if fut_info.get("lot_size"):
                    cfg["lot_size"] = fut_info["lot_size"]
                if fut_info.get("freeze_quantity"):
                    cfg["freeze_qty"] = fut_info["freeze_quantity"]
                margin_1_lot = (
                    fetch_required_margin(cfg["spot_key"], 1, "BUY", "I")
                    if cfg["spot_key"]
                    else 0.0
                )
                log.info(
                    f"[{idx}] MCX FUT ONLY | Symbol: {fut_info.get('trading_symbol', 'N/A')} "
                    f"| Key: {cfg['spot_key']} | Expiry: {fut_info.get('expiry', 'N/A')} "
                    f"| Lot: {cfg['lot_size']} | 1-Lot Intraday Margin: ₹{margin_1_lot:.2f}"
                )
            else:
                exp = get_nearest_expiry(cfg["spot_key"])
                self.expiries[idx] = exp
                log.info(
                    f"[{idx}] Option Expiry: {exp} | FUT Volume Key: {fut_info.get('instrument_key') or 'SPOT_FALLBACK'} "
                    f"({fut_info.get('trading_symbol', 'N/A')})"
                )

    def is_within_trade_window(self, idx: str, now_ist: datetime) -> bool:
        hhmm = now_ist.strftime("%H:%M")
        if INDICES[idx]["type"] == "MCX_FUT":
            return MCX_START_TIME <= hhmm <= MCX_END_TIME
        return INDEX_START_TIME <= hhmm <= INDEX_END_TIME

    def analyze_oi_and_pcr(self, idx: str, chain: list):
        """
        Tracks BOTH:
        1. Cumulative Day PCR Trend from Open (e.g. 1.45 -> 0.85 Bearish | 0.30 -> 0.75 Bullish)
        2. Short-term PCR Direction (Rising/Falling) + Change in OI (Put OI vs Call OI)
        """
        if not chain:
            return 1.0, 1.0, 0.0, False, False

        total_pe_oi = sum(float(r.get("put_options", {}).get("market_data", {}).get("oi", 0)) for r in chain)
        total_ce_oi = sum(float(r.get("call_options", {}).get("market_data", {}).get("oi", 0)) for r in chain)
        pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 1.0

        if self.open_pcr[idx] is None:
            self.open_pcr[idx] = pcr
        open_p = self.open_pcr[idx]
        cum_pcr_diff = round(pcr - open_p, 2)

        hist = self.pcr_history[idx]
        if not hist or hist[-1] != pcr:
            hist.append(pcr)

        short_rising = (len(hist) >= 2 and hist[-1] >= hist[-2]) or (pcr >= 1.10)
        short_falling = (len(hist) >= 2 and hist[-1] <= hist[-2]) or (pcr <= 0.90)

        cum_bullish = (cum_pcr_diff > 0) or (pcr >= 1.15)
        cum_bearish = (cum_pcr_diff < 0) or (pcr <= 0.85)

        prev_oi = self.oi_history[idx]
        oi_bull_ok = True
        oi_bear_ok = True
        if prev_oi is not None:
            delta_pe_oi = total_pe_oi - prev_oi["pe_oi"]
            delta_ce_oi = total_ce_oi - prev_oi["ce_oi"]
            oi_bull_ok = (delta_pe_oi >= delta_ce_oi) or (delta_ce_oi < 0)
            oi_bear_ok = (delta_ce_oi >= delta_pe_oi) or (delta_pe_oi < 0)

        self.oi_history[idx] = {"pe_oi": total_pe_oi, "ce_oi": total_ce_oi}

        bull_confirmed = cum_bullish and short_rising and oi_bull_ok
        bear_confirmed = cum_bearish and short_falling and oi_bear_ok
        return pcr, open_p, cum_pcr_diff, bull_confirmed, bear_confirmed

    def compute_live_mtm(self) -> float:
        """Computes unrealized Live MTM PnL across all currently open positions."""
        if not self.active_positions:
            return 0.0
        mtm =
