from __future__ import annotations

import csv
import gzip
import io
import json
import math
import os
import re
import ssl
import threading
import time
import traceback
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests

try:
    import websocket
except Exception:
    websocket = None

# ==============================================================================
# 1. GLOBAL CONFIGURATION & ENVIRONMENT
# ==============================================================================
IST = timezone(timedelta(hours=5, minutes=30))
BASE_URL = "https://api.upstox.com"

ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GOOGLE_SHEET_WEBHOOK = os.getenv("GOOGLE_SHEET_WEBHOOK", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

TRADING_MODE = os.getenv("TRADING_MODE", "PAPER").strip().upper()  # PAPER or LIVE
if TRADING_MODE not in {"PAPER", "LIVE"}:
    TRADING_MODE = "PAPER"

CAPITAL = float(os.getenv("TRADING_CAPITAL", "25000"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "1"))
MAX_DAILY_LOSS_INR = float(os.getenv("MAX_DAILY_LOSS_INR", str(round(CAPITAL * 0.06, 2))))
REENTRY_COOLDOWN_SEC = int(os.getenv("REENTRY_COOLDOWN_SEC", "420"))

# Hybrid Filter & Greeks Thresholds
MAX_OPTION_SPREAD_PCT = float(os.getenv("MAX_OPTION_SPREAD_PCT", "1.50"))
THETA_BURN_RATIO_MAX = float(os.getenv("THETA_BURN_RATIO_MAX", "0.30"))
RETEST_MAX_WAIT_SEC = int(os.getenv("RETEST_MAX_WAIT_SEC", "900"))
RETEST_TOLERANCE_ATR = 0.35
BREAKOUT_EXPANSION_ATR = 1.20
CRASH_FASTTRACK_ATR = 1.50
CRASH_DAY_PCT_THRESHOLD = 1.15

# File Paths
STATE_DIR = Path("state")
STATE_DIR.mkdir(exist_ok=True)
JOURNAL_CSV = Path("trade_journal.csv")
STRATEGY_PARAMS_FILE = Path("strategy_params.json")
MARKET_MEMORY_FILE = Path("market_memory.json")
MACRO_BIAS_FILE = STATE_DIR / "macro_bias.json"
HEARTBEAT_FILE = STATE_DIR / "engine_heartbeat.json"

# ==============================================================================
# 2. NSE HOLIDAYS & DYNAMIC SESSION / MUHURAT DETECTOR
# ==============================================================================
NSE_HOLIDAYS_2026 = {
    "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
    "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28",
    "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20",
    "2026-11-10", "2026-11-24", "2026-12-25",
}

MUHURAT_DAYS_2026 = {
    "2026-11-08",  # Diwali Muhurat Trading
}


def now_ist() -> datetime:
    return datetime.now(IST)


def ts_str(dt: Optional[datetime] = None) -> str:
    return (dt or now_ist()).strftime("%Y-%m-%d %H:%M:%S")


def is_overnight_dead_hours(dt: Optional[datetime] = None) -> bool:
    hhmm = (dt or now_ist()).strftime("%H:%M")
    return hhmm >= "23:20" or hhmm < "07:30"


def get_session_schedule(dt: Optional[datetime] = None) -> dict:
    cur = dt or now_ist()
    d_str = cur.strftime("%Y-%m-%d")
    wd = cur.weekday()  # 5=Sat, 6=Sun

    if d_str in MUHURAT_DAYS_2026:
        return {
            "session_type": "MUHURAT",
            "nse_open": True,
            "mcx_open": True,
            "nse_start": "18:00",
            "nse_end": "19:15",
            "nse_exit": "19:10",
            "mcx_start": "18:00",
            "mcx_end": "19:15",
            "mcx_exit": "19:10",
        }

    is_weekend = wd >= 5
    is_nse_holiday = is_weekend or (d_str in NSE_HOLIDAYS_2026)

    # MCX opens at 17:00 IST on weekday NSE holidays (morning session closed)
    if is_weekend:
        return {
            "session_type": "WEEKEND_CLOSED",
            "nse_open": False,
            "mcx_open": False,
            "nse_start": "09:15",
            "nse_end": "15:15",
            "nse_exit": "15:15",
            "mcx_start": "09:00",
            "mcx_end": "23:15",
            "mcx_exit": "23:15",
        }

    if is_nse_holiday:
        return {
            "session_type": "NSE_HOLIDAY_MCX_EVENING",
            "nse_open": False,
            "mcx_open": True,
            "nse_start": "09:15",
            "nse_end": "15:15",
            "nse_exit": "15:15",
            "mcx_start": "17:00",
            "mcx_end": "23:15",
            "mcx_exit": "23:15",
        }

    return {
        "session_type": "REGULAR",
        "nse_open": True,
        "mcx_open": True,
        "nse_start": "09:18",
        "nse_end": "15:05",
        "nse_exit": "15:15",
        "mcx_start": "09:15",
        "mcx_end": "23:00",
        "mcx_exit": "23:15",
    }


# ==============================================================================
# 3. SAFE HELPERS & DEFAULT PARAMETERS
# ==============================================================================
def safe_float(val: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return default
        v = float(val)
        return default if (math.isnan(v) or math.isinf(v)) else v
    except Exception:
        return default


def load_strategy_params() -> dict:
    defaults = {
        "gear": 1,
        "index_min_fuel": 48.0,
        "mcx_min_fuel": 52.0,
        "index_max_risk_inr": 1250.0,
        "mcx_max_risk_inr": 1500.0,
        "min_delta": 0.45,
        "otm_fallback_min_delta": 0.32,
        "swing_lookback_candles": 75,
        "stage1_r": 1.20,
        "stage2_r": 2.50,
        "stage3_r": 4.00,
        "stage4_r": 6.50,
    }
    if STRATEGY_PARAMS_FILE.exists():
        try:
            loaded = json.loads(STRATEGY_PARAMS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                defaults.update(loaded)
        except Exception:
            pass
    return defaults


def load_macro_bias() -> dict:
    if MACRO_BIAS_FILE.exists():
        try:
            return json.loads(MACRO_BIAS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"blocked_sides": [], "blocked_pairs": [], "shock_alert": False, "notes": ""}


def load_market_memory() -> dict:
    if MARKET_MEMORY_FILE.exists():
        try:
            return json.loads(MARKET_MEMORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"blocked_setups": []}


# ==============================================================================
# 4. TECHNICAL INDICATORS, GANN LEVELS & 6 CHART PATTERNS
# ==============================================================================
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy().sort_values("timestamp").reset_index(drop=True)

    # EMAs
    x["ema20"] = x["close"].ewm(span=20, adjust=False).mean()
    x["ema50"] = x["close"].ewm(span=50, adjust=False).mean()

    # RSI 14
    delta = x["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    x["rsi14"] = 100 - (100 / (1 + rs))

    # True Range & ATR 14 / ATR 20
    prev_close = x["close"].shift(1)
    tr = pd.concat([
        x["high"] - x["low"],
        (x["high"] - prev_close).abs(),
        (x["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    x["tr"] = tr
    x["atr14"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    x["atr20"] = tr.rolling(20).mean().bfill()

    # ADX 14
    up = x["high"].diff()
    dn = -x["low"].diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
    plus_di = 100 * plus_dm.ewm(alpha=1 / 14, adjust=False).mean() / x["atr14"].replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / 14, adjust=False).mean() / x["atr14"].replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    x["adx14"] = dx.ewm(alpha=1 / 14, adjust=False).mean().fillna(20.0)

    # Choppiness Index (14)
    high_14 = x["high"].rolling(14).max()
    low_14 = x["low"].rolling(14).min()
    tr_sum_14 = tr.rolling(14).sum()
    denom = (high_14 - low_14).replace(0, np.nan)
    x["chop14"] = (100 * np.log10(tr_sum_14 / denom) / np.log10(14)).fillna(50.0)

    # Volume Ratio
    vol_mean = x["volume"].rolling(20).mean().replace(0, np.nan)
    x["vol_ratio"] = (x["volume"] / vol_mean).fillna(1.0)

    # Intraday VWAP (resets per trading day)
    tp = (x["high"] + x["low"] + x["close"]) / 3.0
    day = pd.to_datetime(x["timestamp"]).dt.date
    vol_safe = x["volume"].replace(0, 1.0)
    x["vwap"] = (tp * vol_safe).groupby(day).cumsum() / vol_safe.groupby(day).cumsum()

    # 20-Bar High / Low (shifted by 1 so current bar tests breakout)
    x["high20"] = x["high"].rolling(20).max().shift(1)
    x["low20"] = x["low"].rolling(20).min().shift(1)

    return x


def gann_square_of_9_levels(price: float, count: int = 8) -> List[float]:
    if price <= 0:
        return []
    root = math.sqrt(price)
    base = math.floor(root)
    levels = []
    for k in range(-count, count + 1):
        for step in (0.0, 0.25, 0.5, 0.75):
            r = base + k + step
            if r > 0:
                levels.append(round(r * r, 2))
    return sorted(set(levels))


def compute_fuel_score(row: pd.Series) -> float:
    vol_ratio = safe_float(row.get("vol_ratio"), 1.0) or 1.0
    tr = safe_float(row.get("tr"), 0.0) or 0.0
    atr20 = safe_float(row.get("atr20"), 1.0) or 1.0
    hi = safe_float(row.get("high"), 0.0) or 0.0
    lo = safe_float(row.get("low"), 0.0) or 0.0
    op = safe_float(row.get("open"), 0.0) or 0.0
    cl = safe_float(row.get("close"), 0.0) or 0.0

    vol_pts = min(40.0, max(0.0, (vol_ratio - 0.6) * 25.0))
    atr_pts = min(30.0, max(0.0, (tr / max(atr20, 1e-6)) * 20.0))
    rng = max(hi - lo, 1e-6)
    body_eff = abs(cl - op) / rng
    body_pts = min(20.0, body_eff * 25.0)
    return round(10.0 + vol_pts + atr_pts + body_pts, 1)


def detect_6_gann_patterns(df: pd.DataFrame, gann_levels: List[float]) -> Optional[dict]:
    if len(df) < 4 or not gann_levels:
        return None
    c1 = df.iloc[-3]
    c2 = df.iloc[-2]
    c3 = df.iloc[-1]
    atr = safe_float(c3.get("atr14"), 10.0) or 10.0
    close = float(c3["close"])
    open_ = float(c3["open"])
    high = float(c3["high"])
    low = float(c3["low"])
    rng = max(high - low, 1e-6)
    body = abs(close - open_)

    nearest_gann = min(gann_levels, key=lambda lv: abs(lv - close))
    if abs(close - nearest_gann) > 0.40 * atr:
        return None

    # Bullish Patterns
    c2_bear = c2["close"] < c2["open"]
    c3_bull = close > open_
    if c2_bear and c3_bull and open_ <= c2["close"] and close >= c2["open"]:
        return {"side": "BULL", "pattern": "BULLISH_ENGULFING", "gann_level": nearest_gann}

    lower_wick = min(open_, close) - low
    upper_wick = high - max(open_, close)
    if lower_wick >= 2.0 * max(body, 0.1 * rng) and upper_wick <= 0.35 * rng and close >= (low + 0.6 * rng):
        return {"side": "BULL", "pattern": "HAMMER", "gann_level": nearest_gann}

    if (c1["close"] < c1["open"]) and (abs(c2["close"] - c2["open"]) < 0.4 * (c1["open"] - c1["close"])) and c3_bull and close > (c1["open"] + c1["close"]) / 2.0:
        return {"side": "BULL", "pattern": "MORNING_STAR", "gann_level": nearest_gann}

    if c3_bull and body >= 0.65 * rng and close > nearest_gann and c2["close"] <= nearest_gann:
        return {"side": "BULL", "pattern": "MOMENTUM_BREAKOUT_BULL", "gann_level": nearest_gann}

    # Bearish Patterns
    c2_bull = c2["close"] > c2["open"]
    c3_bear = close < open_
    if c2_bull and c3_bear and open_ >= c2["close"] and close <= c2["open"]:
        return {"side": "BEAR", "pattern": "BEARISH_ENGULFING", "gann_level": nearest_gann}

    if upper_wick >= 2.0 * max(body, 0.1 * rng) and lower_wick <= 0.35 * rng and close <= (high - 0.6 * rng):
        return {"side": "BEAR", "pattern": "REVERSE_HAMMER", "gann_level": nearest_gann}

    if (c1["close"] > c1["open"]) and (abs(c2["close"] - c2["open"]) < 0.4 * (c1["close"] - c1["open"])) and c3_bear and close < (c1["open"] + c1["close"]) / 2.0:
        return {"side": "BEAR", "pattern": "EVENING_STAR", "gann_level": nearest_gann}

    if c3_bear and body >= 0.65 * rng and close < nearest_gann and c2["close"] >= nearest_gann:
        return {"side": "BEAR", "pattern": "MOMENTUM_BREAKDOWN_BEAR", "gann_level": nearest_gann}

    return None


# ==============================================================================
# 5. UPSTOX API & INSTRUMENT RESOLVER
# ==============================================================================
class UpstoxClient:
    def __init__(self, token: str):
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        })

    def get(self, path: str, params: Optional[dict] = None, timeout: int = 12) -> dict:
        if not self.token:
            return {}
        try:
            r = self.session.get(BASE_URL + path, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return {}

    def post(self, path: str, payload: dict, timeout: int = 12) -> dict:
        if not self.token:
            return {}
        try:
            r = self.session.post(BASE_URL + path, json=payload, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return {}

    def fetch_candles(self, instrument_key: str, interval_min: int) -> pd.DataFrame:
        enc = quote(instrument_key, safe="")
        data = self.get(f"/v3/historical-candle/intraday/{enc}/minutes/{interval_min}")
        rows = (data.get("data") or {}).get("candles", [])
        if not rows:
            to_d = now_ist().date()
            from_d = to_d - timedelta(days=5)
            data = self.get(f"/v3/historical-candle/{enc}/minutes/{interval_min}/{to_d}/{from_d}")
            rows = (data.get("data") or {}).get("candles", [])
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        cols = ["timestamp", "open", "high", "low", "close", "volume", "oi"]
        df = df.iloc[:, :min(len(cols), df.shape[1])]
        df.columns = cols[:df.shape[1]]
        for c in ["open", "high", "low", "close", "volume", "oi"]:
            if c in df:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        return df.dropna(subset=["timestamp", "close"]).sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)

    def fetch_ltp(self, instrument_key: str) -> Optional[float]:
        data = self.get("/v3/market-quote/ltp", {"instrument_key": instrument_key})
        items = data.get("data") or {}
        if not items:
            return None
        first = items.get(instrument_key) or next(iter(items.values()))
        return safe_float((first or {}).get("last_price"))

    def fetch_option_chain(self, index_key: str, expiry: str) -> List[dict]:
        data = self.get("/v2/option/chain", {"instrument_key": index_key, "expiry_date": expiry})
        rows = data.get("data") or []
        return rows if isinstance(rows, list) else []


# ==============================================================================
# 6. TRADE POSITION & STATE DATA STRUCTURES
# ==============================================================================
@dataclass
class TradePosition:
    trade_id: int
    symbol: str
    segment: str               # INDEX_OPT or MCX_FUT
    direction: str             # CE / PE or BUY / SELL
    instrument_key: str
    trading_symbol: str
    qty: int
    lot_size: int
    entry_time: str
    entry_price: float
    stop_loss: float
    initial_sl: float
    target_price: float
    underlying_entry: float
    underlying_sl: float
    setup: str                 # Exact Entry Logic Tag
    fuel_score: float
    peak_price: float
    runner_mode: bool = False
    stage_locked: int = 0
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl_inr: float = 0.0


# ==============================================================================
# 7. HYBRID LIVE TRADING ENGINE
# ==============================================================================
SYMBOL_CONFIGS = [
    {"symbol": "NIFTY", "segment": "INDEX_OPT", "spot_key": "NSE_INDEX|Nifty 50", "tf": 5, "step": 50},
    {"symbol": "BANKNIFTY", "segment": "INDEX_OPT", "spot_key": "NSE_INDEX|Nifty Bank", "tf": 5, "step": 100},
    {"symbol": "FINNIFTY", "segment": "INDEX_OPT", "spot_key": "NSE_INDEX|Nifty Fin Service", "tf": 5, "step": 50},
    {"symbol": "MIDCPNIFTY", "segment": "INDEX_OPT", "spot_key": "NSE_INDEX|NIFTY MID SELECT", "tf": 5, "step": 25},
    {"symbol": "SENSEX", "segment": "INDEX_OPT", "spot_key": "BSE_INDEX|SENSEX", "tf": 5, "step": 100},
    {"symbol": "CRUDEOIL", "segment": "MCX_FUT", "spot_key": "", "tf": 15, "step": 10},
    {"symbol": "NATURALGAS", "segment": "MCX_FUT", "spot_key": "", "tf": 15, "step": 1},
    {"symbol": "GOLD", "segment": "MCX_FUT", "spot_key": "", "tf": 15, "step": 100},
    {"symbol": "SILVER", "segment": "MCX_FUT", "spot_key": "", "tf": 15, "step": 500},
    {"symbol": "COPPER", "segment": "MCX_FUT", "spot_key": "", "tf": 15, "step": 5},
]


class HybridLiveEngine:
    def __init__(self):
        self.api = UpstoxClient(ACCESS_TOKEN)
        self.params = load_strategy_params()
        self.positions: Dict[str, TradePosition] = {}
        self.closed_trades: List[TradePosition] = []
        self.armed_setups: Dict[str, dict] = {}
        self.last_exit_ts: Dict[str, float] = {}
        self.trade_counter = 0
        self.realized_pnl = 0.0
        self.master_df = self._load_master()
        self.resolved_contracts = self._resolve_contracts()
        self.telegram_msg_id: Optional[int] = None

    def _load_master(self) -> pd.DataFrame:
        url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
        try:
            r = requests.get(url, timeout=45)
            r.raise_for_status()
            raw = gzip.decompress(r.content)
            df = pd.DataFrame(json.loads(raw.decode("utf-8")))
            df.columns = [str(c).strip().lower() for c in df.columns]
            return df
        except Exception as e:
            print(f"[WARN] Master download failed: {e}")
            return pd.DataFrame()

    def _resolve_contracts(self) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        if self.master_df.empty:
            return out
        df = self.master_df.copy()
        seg = df.get("segment", pd.Series("", index=df.index)).astype(str).str.upper()
        typ = df.get("instrument_type", pd.Series("", index=df.index)).astype(str).str.upper()
        sym = df.get("trading_symbol", pd.Series("", index=df.index)).astype(str).str.upper()
        und = df.get("underlying_symbol", pd.Series("", index=df.index)).astype(str).str.upper()
        exp_raw = pd.to_numeric(df.get("expiry"), errors="coerce")
        exp_dt = pd.to_datetime(exp_raw, unit="ms", errors="coerce").fillna(
            pd.to_datetime(df.get("expiry"), errors="coerce")
        )
        df["_exp"] = exp_dt.dt.date
        today = now_ist().date()

        for cfg in SYMBOL_CONFIGS:
            s = cfg["symbol"]
            if cfg["segment"] == "MCX_FUT":
                m = seg.str.contains("MCX") & typ.isin(["FUT", "FUTCOM", "F"]) & (
                    und.eq(s) | sym.str.startswith(s)
                ) & (~sym.str.contains(f"{s}M")) & (df["_exp"] >= today)
                sub = df[m].sort_values("_exp")
                if not sub.empty:
                    row = sub.iloc[0]
                    out[s] = {
                        "instrument_key": str(row["instrument_key"]),
                        "trading_symbol": str(row["trading_symbol"]),
                        "expiry": str(row["_exp"]),
                        "lot_size": int(safe_float(row.get("lot_size"), 1) or 1),
                    }
            else:
                # Nearest Option Expiry for Index
                m = seg.str.contains("FO") & typ.isin(["OPTIDX", "CE", "PE"]) & (
                    und.eq(s) | sym.str.startswith(s)
                ) & (df["_exp"] >= today)
                sub = df[m].sort_values("_exp")
                if not sub.empty:
                    row = sub.iloc[0]
                    out[s] = {
                        "spot_key": cfg["spot_key"],
                        "expiry": str(row["_exp"]),
                        "lot_size": int(safe_float(row.get("lot_size"), 25) or 25),
                    }
        return out

    # --------------------------------------------------------------------------
    # OPTION CHAIN SELECTOR (WITH SPREAD, THETA BURN & SMART OTM RISK-FIT)
    # --------------------------------------------------------------------------
    def select_best_option(
        self, symbol: str, direction: str, spot_price: float, expected_move: float, max_risk_inr: float
    ) -> Optional[dict]:
        info = self.resolved_contracts.get(symbol)
        if not info or not info.get("expiry"):
            return None
        rows = self.api.fetch_option_chain(info["spot_key"], info["expiry"])
        if not rows:
            return None

        side_key = "call_options" if direction == "CE" else "put_options"
        candidates = []
        min_delta = float(self.params.get("min_delta", 0.45))
        otm_min_delta = float(self.params.get("otm_fallback_min_delta", 0.32))

        for r in rows:
            strike = safe_float(r.get("strike_price"))
            opt = r.get(side_key) or {}
            md = opt.get("market_data") or {}
            gk = opt.get("option_greeks") or opt.get("greeks") or {}
            ltp = safe_float(md.get("ltp") or md.get("last_price"))
            bid = safe_float(md.get("bid_price"))
            ask = safe_float(md.get("ask_price"))
            oi = safe_float(md.get("oi"), 0.0) or 0.0
            delta = abs(safe_float(gk.get("delta"), 0.0) or 0.0)
            gamma = abs(safe_float(gk.get("gamma"), 0.0) or 0.0)
            theta = abs(safe_float(gk.get("theta"), 0.0) or 0.0)

            if not ltp or ltp <= 2.0 or oi <= 0 or delta < otm_min_delta:
                continue

            spread_pct = ((ask - bid) / ltp * 100.0) if (bid and ask and ask >= bid) else 0.0
            if spread_pct > MAX_OPTION_SPREAD_PCT:
                continue

            # 20-minute Theta Burn vs Expected Directional Gain
            directional_gain = delta * abs(expected_move) + 0.5 * gamma * (expected_move ** 2)
            theta_burn = theta * (20.0 / 390.0)
            if directional_gain > 0 and (theta_burn / directional_gain) > THETA_BURN_RATIO_MAX:
                continue

            lot = info["lot_size"]
            est_sl_pts = max(ltp * 0.18, expected_move * 0.45 * delta)
            est_risk_inr = est_sl_pts * lot + 55.0

            candidates.append({
                "instrument_key": opt.get("instrument_key"),
                "trading_symbol": opt.get("trading_symbol") or f"{symbol}_{int(strike)}_{direction}",
                "strike": strike,
                "ltp": ltp,
                "delta": delta,
                "gamma": gamma,
                "theta": theta,
                "oi": oi,
                "lot_size": lot,
                "est_sl_pts": est_sl_pts,
                "est_risk_inr": est_risk_inr,
                "is_primary_delta": delta >= min_delta,
            })

        if not candidates:
            return None

        # Prefer primary delta (>=0.45) within risk cap; fallback to Smart OTM Risk-Fit
        fitting = [c for c in candidates if c["est_risk_inr"] <= max_risk_inr]
        if fitting:
            primary_fit = [c for c in fitting if c["is_primary_delta"]]
            pool = primary_fit if primary_fit else fitting
            pool.sort(key=lambda x: (-x["delta"], -x["oi"]))
            return pool[0]

        # If all exceed risk cap slightly, pick lowest risk OTM candidate
        candidates.sort(key=lambda x: x["est_risk_inr"])
        if candidates[0]["est_risk_inr"] <= max_risk_inr * 1.25:
            return candidates[0]
        return None

    # --------------------------------------------------------------------------
    # 4-GATE HYBRID ENTRY ENGINE
    # --------------------------------------------------------------------------
    def evaluate_symbol(self, cfg: dict, sched: dict):
        symbol = cfg["symbol"]
        segment = cfg["segment"]
        cur = now_ist()
        hhmm = cur.strftime("%H:%M")

        # Check active session window
        if segment == "INDEX_OPT":
            if not sched["nse_open"] or not (sched["nse_start"] <= hhmm <= sched["nse_end"]):
                return
            key = cfg["spot_key"]
        else:
            if not sched["mcx_open"] or not (sched["mcx_start"] <= hhmm <= sched["mcx_end"]):
                return
            cinfo = self.resolved_contracts.get(symbol)
            if not cinfo:
                return
            key = cinfo["instrument_key"]

        raw_df = self.api.fetch_candles(key, cfg["tf"])
        if raw_df is None or len(raw_df) < 25:
            return
        df = compute_indicators(raw_df)
        q = df.iloc[-1]
        prev = df.iloc[-2]

        price = float(q["close"])
        vwap = safe_float(q["vwap"], price) or price
        ema20 = safe_float(q["ema20"], price) or price
        ema50 = safe_float(q["ema50"], price) or price
        atr = safe_float(q["atr14"], price * 0.002) or (price * 0.002)
        tr = safe_float(q["tr"], atr) or atr
        adx = safe_float(q["adx14"], 20.0) or 20.0
        chop = safe_float(q["chop14"], 50.0) or 50.0
        high20 = safe_float(q["high20"])
        low20 = safe_float(q["low20"])

        # Today's intraday bars & 15m Opening Range (ORB)
        today_df = df[pd.to_datetime(df["timestamp"]).dt.date == cur.date()]
        orb_bars = 3 if cfg["tf"] == 5 else 1
        orb_high = float(today_df.iloc[:orb_bars]["high"].max()) if len(today_df) >= orb_bars else None
        orb_low = float(today_df.iloc[:orb_bars]["low"].min()) if len(today_df) >= orb_bars else None
        day_open = float(today_df.iloc[0]["open"]) if not today_df.empty else price
        day_chg_pct = ((price - day_open) / max(day_open, 1e-6)) * 100.0

        # Fuel Score & Dynamic Regime Adjustment
        fuel = compute_fuel_score(q)
        min_fuel = float(self.params["index_min_fuel"] if segment == "INDEX_OPT" else self.params["mcx_min_fuel"])
        if adx < 19.0 or chop > 60.0:
            min_fuel += 3.0

        # Already open position or cooldown check
        if symbol in self.positions or len(self.positions) >= MAX_OPEN_POSITIONS:
            return
        if self.realized_pnl <= -MAX_DAILY_LOSS_INR:
            return
        if time.time() - self.last_exit_ts.get(symbol, 0.0) < REENTRY_COOLDOWN_SEC:
            return

        macro = load_macro_bias()
        if macro.get("shock_alert"):
            return

        gann_levels = gann_square_of_9_levels(price)
        entry_signal: Optional[dict] = None

        # ----------------------------------------------------------------------
        # GATE 1A: CHECK PENDING RETEST (20BAR_BREAKOUT_RETEST or GANN_REVERSAL)
        # ----------------------------------------------------------------------
        armed = self.armed_setups.get(symbol)
        if armed:
            if time.time() - armed["armed_ts"] > RETEST_MAX_WAIT_SEC:
                self.armed_setups.pop(symbol, None)
            else:
                lvl = armed["level"]
                tol = RETEST_TOLERANCE_ATR * atr
                if armed["bias"] == "BULL":
                    retested = float(q["low"]) <= (lvl + tol) and price > lvl and price > float(prev["high"])
                    if retested and price > vwap and price > ema20:
                        entry_signal = {
                            "bias": "BULL",
                            "setup": armed["setup_tag"],
                            "u_sl": min(float(q["low"]), lvl) - 0.25 * atr,
                        }
                        self.armed_setups.pop(symbol, None)
                else:
                    retested = float(q["high"]) >= (lvl - tol) and price < lvl and price < float(prev["low"])
                    if retested and price < vwap and price < ema20:
                        entry_signal = {
                            "bias": "BEAR",
                            "setup": armed["setup_tag"],
                            "u_sl": max(float(q["high"]), lvl) + 0.25 * atr,
                        }
                        self.armed_setups.pop(symbol, None)

        # ----------------------------------------------------------------------
        # GATE 1B: CRASH / RALLY FAST-TRACK (Immediate Entry on >1.15% / ORB + 1.5x ATR)
        # ----------------------------------------------------------------------
        if entry_signal is None and len(today_df) > orb_bars:
            if (
                orb_low is not None
                and price < orb_low
                and price < vwap
                and ema20 <= ema50
                and (day_chg_pct <= -CRASH_DAY_PCT_THRESHOLD or tr >= CRASH_FASTTRACK_ATR * atr)
            ):
                entry_signal = {
                    "bias": "BEAR",
                    "setup": "CRASH_RALLY_FASTTRACK",
                    "u_sl": min(orb_low + 0.4 * atr, price + 1.1 * atr),
                }
            elif (
                orb_high is not None
                and price > orb_high
                and price > vwap
                and ema20 >= ema50
                and (day_chg_pct >= CRASH_DAY_PCT_THRESHOLD or tr >= CRASH_FASTTRACK_ATR * atr)
            ):
                entry_signal = {
                    "bias": "BULL",
                    "setup": "CRASH_RALLY_FASTTRACK",
                    "u_sl": max(orb_high - 0.4 * atr, price - 1.1 * atr),
                }

        # ----------------------------------------------------------------------
        # GATE 1C: 20-BAR BREAKOUT WITH TR >= 1.2x ATR (Arms Retest Setup)
        # ----------------------------------------------------------------------
        if entry_signal is None and symbol not in self.armed_setups:
            if high20 and price > high20 and tr >= BREAKOUT_EXPANSION_ATR * atr and price > vwap and ema20 > ema50:
                self.armed_setups[symbol] = {
                    "bias": "BULL",
                    "level": high20,
                    "armed_ts": time.time(),
                    "setup_tag": "20BAR_BREAKOUT_RETEST",
                }
            elif low20 and price < low20 and tr >= BREAKOUT_EXPANSION_ATR * atr and price < vwap and ema20 < ema50:
                self.armed_setups[symbol] = {
                    "bias": "BEAR",
                    "level": low20,
                    "armed_ts": time.time(),
                    "setup_tag": "20BAR_BREAKOUT_RETEST",
                }

        # ----------------------------------------------------------------------
        # GATE 1D: GANN + 6 CHART PATTERNS + VWAP/EMA ALIGNMENT
        # ----------------------------------------------------------------------
        if entry_signal is None:
            pat = detect_6_gann_patterns(df, gann_levels)
            if pat:
                if pat["side"] == "BULL" and price > vwap and price > ema20:
                    entry_signal = {
                        "bias": "BULL",
                        "setup": f"GANN_REVERSAL_ENTRY|{pat['pattern']}",
                        "u_sl": float(df.tail(3)["low"].min()) - 0.25 * atr,
                    }
                elif pat["side"] == "BEAR" and price < vwap and price < ema20:
                    entry_signal = {
                        "bias": "BEAR",
                        "setup": f"GANN_REVERSAL_ENTRY|{pat['pattern']}",
                        "u_sl": float(df.tail(3)["high"].max()) + 0.25 * atr,
                    }

        # ----------------------------------------------------------------------
        # GATE 1E: EXPIRY SLOPE BREAKOUT (75-Candle Range on Expiry 13:45-14:45)
        # ----------------------------------------------------------------------
        if entry_signal is None and segment == "INDEX_OPT" and "13:45" <= hhmm <= "14:45":
            info = self.resolved_contracts.get(symbol, {})
            if info.get("expiry") == cur.strftime("%Y-%m-%d") and len(df) >= 75:
                lookback = int(self.params.get("swing_lookback_candles", 75))
                rng_hi = float(df.iloc[-lookback:-1]["high"].max())
                rng_lo = float(df.iloc[-lookback:-1]["low"].min())
                if price > rng_hi and price > vwap:
                    entry_signal = {
                        "bias": "BULL",
                        "setup": "EXPIRY_SLOPE_BREAKOUT",
                        "u_sl": rng_hi - 0.4 * atr,
                    }
                elif price < rng_lo and price < vwap:
                    entry_signal = {
                        "bias": "BEAR",
                        "setup": "EXPIRY_SLOPE_BREAKOUT",
                        "u_sl": rng_lo + 0.4 * atr,
                    }

        if entry_signal is None:
            return

        # ----------------------------------------------------------------------
        # GATE 2: FUEL & MACRO/MEMORY VETO CHECK
        # ----------------------------------------------------------------------
        if fuel < min_fuel:
            return

        direction = ("CE" if entry_signal["bias"] == "BULL" else "PE") if segment == "INDEX_OPT" else (
            "BUY" if entry_signal["bias"] == "BULL" else "SELL"
        )
        if direction in macro.get("blocked_sides", []) or f"{symbol}_{direction}" in macro.get("blocked_pairs", []):
            return

        # ----------------------------------------------------------------------
        # GATE 3 & 4: EXECUTION & SIZING
        # ----------------------------------------------------------------------
        self.execute_entry(symbol, segment, direction, price, atr, entry_signal, fuel)

    def execute_entry(
        self, symbol: str, segment: str, direction: str, spot_price: float, atr: float, sig: dict, fuel: float
    ):
        u_sl = float(sig["u_sl"])
        u_risk = max(abs(spot_price - u_sl), 0.35 * atr)
        expected_move = u_risk * 2.5

        if segment == "INDEX_OPT":
            max_risk = float(self.params.get("index_max_risk_inr", 1250.0))
            opt = self.select_best_option(symbol, direction, spot_price, expected_move, max_risk)
            if not opt:
                return
            entry_px = float(opt["ltp"])
            sl_pts = float(opt["est_sl_pts"])
            stop_loss = round(max(1.0, entry_px - sl_pts), 2)
            target_px = round(entry_px + sl_pts * 3.0, 2)
            lot = int(opt["lot_size"])
            qty = lot
            inst_key = str(opt["instrument_key"])
            t_sym = str(opt["trading_symbol"])
        else:
            max_risk = float(self.params.get("mcx_max_risk_inr", 1500.0))
            cinfo = self.resolved_contracts.get(symbol)
            if not cinfo:
                return
            lot = int(cinfo["lot_size"])
            sl_pts = min(u_risk, max_risk / max(lot, 1))
            if sl_pts <= 0:
                return
            entry_px = spot_price
            stop_loss = round(entry_px - sl_pts if direction == "BUY" else entry_px + sl_pts, 2)
            target_px = round(entry_px + sl_pts * 3.0 if direction == "BUY" else entry_px - sl_pts * 3.0, 2)
            qty = lot
            inst_key = str(cinfo["instrument_key"])
            t_sym = str(cinfo["trading_symbol"])

        self.trade_counter += 1
        pos = TradePosition(
            trade_id=self.trade_counter,
            symbol=symbol,
            segment=segment,
            direction=direction,
            instrument_key=inst_key,
            trading_symbol=t_sym,
            qty=qty,
            lot_size=lot,
            entry_time=ts_str(),
            entry_price=entry_px,
            stop_loss=stop_loss,
            initial_sl=stop_loss,
            target_price=target_px,
            underlying_entry=spot_price,
            underlying_sl=u_sl,
            setup=sig["setup"],
            fuel_score=fuel,
            peak_price=entry_px,
        )
        self.positions[symbol] = pos
        self.send_telegram(
            f"🚀 *ENTRY #{pos.trade_id}* ({TRADING_MODE})\n"
            f"• `{pos.trading_symbol}` ({pos.direction})\n"
            f"• Setup: `{pos.setup}` | Fuel: `{pos.fuel_score}`\n"
            f"• Entry: `₹{pos.entry_price:.2f}` | SL: `₹{pos.stop_loss:.2f}` | Tgt: `₹{pos.target_price:.2f}`"
        )

    # --------------------------------------------------------------------------
    # HIGH R:R EXIT & 4-STAGE RUNNER MANAGEMENT
    # --------------------------------------------------------------------------
    def manage_positions(self, sched: dict):
        hhmm = now_ist().strftime("%H:%M")
        macro = load_macro_bias()

        for symbol in list(self.positions.keys()):
            pos = self.positions[symbol]
            ltp = self.api.fetch_ltp(pos.instrument_key)
            if ltp is None:
                continue

            spot_key = SYMBOL_CONFIGS[[c["symbol"] for c in SYMBOL_CONFIGS].index(symbol)]["spot_key"] or pos.instrument_key
            spot_ltp = self.api.fetch_ltp(spot_key) or pos.underlying_entry

            is_short = (pos.segment == "MCX_FUT" and pos.direction == "SELL")
            risk = max(abs(pos.entry_price - pos.initial_sl), 0.05)

            if not is_short:
                pos.peak_price = max(pos.peak_price, ltp)
                cur_r = (ltp - pos.entry_price) / risk
                peak_r = (pos.peak_price - pos.entry_price) / risk
            else:
                pos.peak_price = min(pos.peak_price, ltp)
                cur_r = (pos.entry_price - ltp) / risk
                peak_r = (pos.entry_price - pos.peak_price) / risk

            # 4-Stage Profit Lock & Runner Mode
            s1 = float(self.params.get("stage1_r", 1.20))
            s2 = float(self.params.get("stage2_r", 2.50))
            s3 = float(self.params.get("stage3_r", 4.00))
            s4 = float(self.params.get("stage4_r", 6.50))

            if peak_r >= s4 and pos.stage_locked < 4:
                pos.stage_locked = 4
                lock_r = peak_r * 0.80
                pos.stop_loss = pos.entry_price + lock_r * risk if not is_short else pos.entry_price - lock_r * risk
            elif peak_r >= s3 and pos.stage_locked < 3:
                pos.stage_locked = 3
                lock_r = peak_r * 0.70
                pos.stop_loss = pos.entry_price + lock_r * risk if not is_short else pos.entry_price - lock_r * risk
            elif peak_r >= s2 and pos.stage_locked < 2:
                pos.stage_locked = 2
                pos.runner_mode = True
                pos.stop_loss = pos.entry_price + 1.50 * risk if not is_short else pos.entry_price - 1.50 * risk
            elif peak_r >= s1 and pos.stage_locked < 1:
                pos.stage_locked = 1
                pos.stop_loss = pos.entry_price + 0.15 * risk if not is_short else pos.entry_price - 0.15 * risk

            # Exit Checks (All 8 Exit Reasons)
            exit_reason: Optional[str] = None
            eod_cut = sched["nse_exit"] if pos.segment == "INDEX_OPT" else sched["mcx_exit"]

            if hhmm >= eod_cut:
                exit_reason = "EOD_EXIT"
            elif macro.get("shock_alert"):
                exit_reason = "AI_SHOCK_EJECT"
            elif (not is_short and ltp <= pos.stop_loss) or (is_short and ltp >= pos.stop_loss):
                exit_reason = "TRAIL_SL_HIT" if pos.stage_locked >= 1 else "STOP_LOSS_HIT"
            elif pos.direction in ("CE", "BUY") and spot_ltp <= pos.underlying_sl:
                exit_reason = "UNDERLYING_SL_HIT"
            elif pos.direction in ("PE", "SELL") and spot_ltp >= pos.underlying_sl:
                exit_reason = "UNDERLYING_SL_HIT"
            else:
                # Check VWAP Reversal or No-Momentum Decay on recent candles
                tf = 5 if pos.segment == "INDEX_OPT" else 15
                df = compute_indicators(self.api.fetch_candles(spot_key, tf))
                if len(df) >= 6:
                    q = df.iloc[-1]
                    vw = safe_float(q["vwap"], spot_ltp) or spot_ltp
                    atr = safe_float(q["atr14"], 10.0) or 10.0
                    if cur_r >= 1.80:
                        if (pos.direction in ("CE", "BUY") and spot_ltp < vw) or (
                            pos.direction in ("PE", "SELL") and spot_ltp > vw
                        ):
                            exit_reason = "VWAP_REVERSAL_EXIT"
                    # No-Momentum Decay after 15 mins
                    ent_dt = datetime.strptime(pos.entry_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
                    held_min = (now_ist() - ent_dt).total_seconds() / 60.0
                    if held_min >= 15.0 and cur_r < 0.10:
                        move_5 = abs(float(df.iloc[-1]["close"]) - float(df.iloc[-4]["close"]))
                        if move_5 < 0.25 * atr:
                            exit_reason = "NO_MOMENTUM_EXIT"

            if exit_reason:
                self.close_position(symbol, ltp, exit_reason)

    def close_position(self, symbol: str, exit_price: float, reason: str):
        pos = self.positions.pop(symbol, None)
        if not pos:
            return
        is_short = (pos.segment == "MCX_FUT" and pos.direction == "SELL")
        gross = ((pos.entry_price - exit_price) if is_short else (exit_price - pos.entry_price)) * pos.qty
        charges = 55.0
        net = round(gross - charges, 2)

        pos.exit_time = ts_str()
        pos.exit_price = round(exit_price, 2)
        pos.exit_reason = reason
        pos.pnl_inr = net
        self.realized_pnl = round(self.realized_pnl + net, 2)
        self.closed_trades.append(pos)
        self.last_exit_ts[symbol] = time.time()

        self._append_journal(pos)
        icon = "✅" if net >= 0 else "❌"
        self.send_telegram(
            f"{icon} *EXIT #{pos.trade_id}* | `{pos.trading_symbol}`\n"
            f"• Entry: `{pos.setup}`\n"
            f"• Exit: `{pos.exit_reason}`\n"
            f"• `₹{pos.entry_price:.2f} → ₹{pos.exit_price:.2f}` | Net P&L: *₹{net:+,.2f}*"
        )

    def _append_journal(self, pos: TradePosition):
        exists = JOURNAL_CSV.exists()
        row = asdict(pos)
        with JOURNAL_CSV.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if not exists:
                w.writeheader()
            w.writerow(row)
        if GOOGLE_SHEET_WEBHOOK:
            try:
                requests.post(GOOGLE_SHEET_WEBHOOK, json=row, timeout=6)
            except Exception:
                pass

    def send_telegram(self, text: str):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print(text)
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
                timeout=8,
            )
        except Exception:
            pass

    # --------------------------------------------------------------------------
    # MAIN ENGINE LOOP
    # --------------------------------------------------------------------------
    def run(self):
        cur = now_ist()
        if is_overnight_dead_hours(cur):
            print(f"[{ts_str()}] Outside market hours ({cur.strftime('%H:%M')} IST). Standing down cleanly.")
            return

        sched = get_session_schedule(cur)
        print(f"[{ts_str()}] HybridLiveEngine Started | Mode={TRADING_MODE} | Session={sched['session_type']}")
        self.send_telegram(
            f"🤖 *BOT LIVE ({TRADING_MODE})*\n"
            f"Session: `{sched['session_type']}` | Gear: `{self.params.get('gear', 1)}`\n"
            f"Active Triggers: `GANN + VWAP/EMA`, `20BAR_RETEST (1.2x ATR)`, `CRASH_FASTTRACK`, `EXPIRY_SLOPE`"
        )

        while True:
            now = now_ist()
            hhmm = now.strftime("%H:%M")
            sched = get_session_schedule(now)

            if is_overnight_dead_hours(now) or (not sched["nse_open"] and not sched["mcx_open"]):
                for sym in list(self.positions.keys()):
                    ltp = self.api.fetch_ltp(self.positions[sym].instrument_key) or self.positions[sym].entry_price
                    self.close_position(sym, ltp, "EOD_EXIT")
                print(f"[{ts_str()}] Session complete. Exiting cleanly.")
                break

            # Manage open trades every cycle
            self.manage_positions(sched)

            # Evaluate all 10 symbols for 4-Gate Hybrid Entries
            for cfg in SYMBOL_CONFIGS:
                try:
                    self.evaluate_symbol(cfg, sched)
                except Exception as e:
                    print(f"[ERR] {cfg['symbol']}: {e}")

            HEARTBEAT_FILE.write_text(
                json.dumps({"ts": ts_str(), "realized_pnl": self.realized_pnl, "open": len(self.positions)}),
                encoding="utf-8",
            )
            time.sleep(10)


if __name__ == "__main__":
    engine = HybridLiveEngine()
    engine.run()
