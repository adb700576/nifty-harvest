import os
import io
import gzip
import time
import math
import json
import csv
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Any, Tuple, Dict, List
from urllib.parse import quote
import requests
import pandas as pd
import numpy as np

# ========================= OPTIONAL MATPLOTLIB IMPORT =========================
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

# ========================= CONFIGURATION & TOKEN =========================
BASE_URL = "https://api.upstox.com"

def fetch_token() -> str:
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if token:
        return token

    search_files = [
        "ACCESS_TOKEN.txt",
        Path(__file__).resolve().parent / "ACCESS_TOKEN.txt",
        Path.cwd() / "ACCESS_TOKEN.txt"
    ]

    for p in search_files:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    print(f"[+] Token लोड झाले: {p}", flush=True)
                    return content
            except Exception as e:
                print(f"[-] {p} वाचताना त्रुटी: {e}", flush=True)
    return ""

TOKEN = fetch_token()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ========================= 5-INDEX REGISTRY CONFIGURATION =========================
INDEX_CONFIGS: Dict[str, dict] = {
    "NIFTY": {
        "display_name": "NIFTY 50",
        "spot_key": "NSE_INDEX|Nifty 50",
        "fo_exchange": "NSE_FO",
        "master_names": ["NIFTY"],
        "default_lot": 65,
        "default_step": 50.0,
    },
    "BANKNIFTY": {
        "display_name": "BANK NIFTY",
        "spot_key": "NSE_INDEX|Nifty Bank",
        "fo_exchange": "NSE_FO",
        "master_names": ["BANKNIFTY"],
        "default_lot": 30,
        "default_step": 100.0,
    },
    "FINNIFTY": {
        "display_name": "FIN NIFTY",
        "spot_key": "NSE_INDEX|Nifty Fin Service",
        "fo_exchange": "NSE_FO",
        "master_names": ["FINNIFTY"],
        "default_lot": 65,
        "default_step": 50.0,
    },
    "MIDCPNIFTY": {
        "display_name": "MIDCAP NIFTY",
        "spot_key": "NSE_INDEX|NIFTY MID SELECT",
        "fo_exchange": "NSE_FO",
        "master_names": ["MIDCPNIFTY"],
        "default_lot": 120,
        "default_step": 25.0,
    },
    "SENSEX": {
        "display_name": "BSE SENSEX",
        "spot_key": "BSE_INDEX|SENSEX",
        "fo_exchange": "BSE_FO",
        "master_names": ["SENSEX", "BSX"],
        "default_lot": 20,
        "default_step": 100.0,
    }
}

ACTIVE_INDICES: List[str] = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]

# ========================= SCALE-INVARIANT PERCENTAGE & STRATEGY PARAMETERS =========================
POLL_INTERVAL = 1.0
STRATEGY_INTERVAL = 12.0
POSITION_UPDATE_INTERVAL = 180.0
MAX_SIMULTANEOUS_POSITIONS = 2
MAX_TRADES_PER_60_MIN = 2

# Prime Trading Windows (Lunch Chop 12:00 - 13:30 Blocked)
MORNING_ENTRY_START   = "09:20"
MORNING_ENTRY_END     = "12:00"
AFTERNOON_ENTRY_START = "13:30"
AFTERNOON_ENTRY_END   = "14:45"

# Gann 0.50 Lattice, VWAP Expansion & Breakout-Retest Parameters
GANN_PROXIMITY_PCT = 0.0012
RETEST_ATR_BUFFER = 0.30           # Breakout नंतर ±0.30 * ATR झोनमध्ये Retest कन्फर्मेशन
VWAP_TOUCH_BAND_PCT = 0.0008
VWAP_EXPANSION_PCT = 0.0035        # VWAP Pullback आधी किमान ±0.35% ट्रेंड विस्तार अनिवार्य
ENTRY_BUFFER_PCT = 0.00020
MIN_REMAINING_FUEL_PCT = 0.0010
STRIKE_SCAN_RANGE_PCT = 0.020

# Causal 0-100 Momentum Fuel Score Threshold
MIN_FUEL_SCORE = 70.0              # >= 70/100 असल्यास त्वरित एन्ट्री; < 70 असल्यास Pending + Reclaim प्रतिक्षा

# Price Action & Breakout Momentum Thresholds
MIN_ADX_MOMENTUM = 18.0
MIN_FUT_VOL_RATIO = 1.20
MIN_RANGE_ATR_MULT = 1.20
MIN_BODY_RATIO = 0.45
MIN_REJECTION_WICK_RATIO = 0.45

# PCR Velocity (Slope) Thresholds
PCR_SLOPE_BEAR_THRESHOLD = -0.22
PCR_SLOPE_BULL_THRESHOLD = 0.20

# Option Premium Dynamic Minimum 10 Pts SL & 1 : 1.50 R:R
ABS_MIN_OPT_SL_PTS = 10.0          # किमान 10 पॉईंट्स Option SL
MIN_OPT_SL_PCT = 0.10
MAX_OPT_SL_PCT = 0.22
MAX_OPT_TRG_PCT = 0.38
REALISTIC_RRR = 1.50

# Institutional Option Quality & Decay Guards
EXPECTED_HOLD_MINUTES = 20
MAX_THETA_BURN_RATIO = 0.30
MAX_SPREAD_PCT = 1.50
NO_MOMENTUM_BARS = 5
NO_MOMENTUM_MOVE_ATR = 0.25

# Global Daily Capital Protection
GLOBAL_DAILY_PROFIT_GOAL = 3500.0
GLOBAL_DAILY_MAX_LOSS = -2500.0
MAX_CONSECUTIVE_LOSSES = 2

# Live Greeks & OI Safety Guards
MAX_ALLOWED_IV = 28.0
MIN_ABS_DELTA = 0.35
MIN_5M_OI_SHIFT = 250000

JOURNAL_DIR = Path("live_journal")
JOURNAL_DIR.mkdir(exist_ok=True)
TODAY_STR = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")
JOURNAL_CSV = JOURNAL_DIR / f"trades_journal_{TODAY_STR}.csv"


# ========================= SIMPLE VISUAL TELEGRAM ALERTS =========================
def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram Disconnected] {text[:90]}...", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=8)
        if r.status_code != 200:
            print(f"[-] Telegram API HTTP {r.status_code}: {r.text[:150]}", flush=True)
    except Exception as e:
        print(f"[-] Telegram Error: {e}", flush=True)

def send_telegram_chart_alert(index_code: str, df_5m: pd.DataFrame, gann_level: float, sl_price: float, trg_price: float, caption_text: str = ""):
    if not HAS_MATPLOTLIB or df_5m.empty or len(df_5m) < 5:
        send_telegram(caption_text)
        return

    try:
        recent = df_5m.tail(25)
        plt.figure(figsize=(9, 4.5))
        plt.plot(recent['dt'], recent['close'], label=f'{index_code} 5M', color='#111111', linewidth=1.8)
        if 'vwap' in recent.columns:
            plt.plot(recent['dt'], recent['vwap'], label='VWAP', color='#f59e0b', linestyle='-.', linewidth=1.2)
        if 'ema20' in recent.columns:
            plt.plot(recent['dt'], recent['ema20'], label='EMA 20', color='#10b981', linestyle='--', linewidth=1.1)
        plt.axhline(y=gann_level, color='#2563eb', linestyle='--', label=f'Gann: {gann_level:.0f}')
        plt.axhline(y=sl_price, color='#dc2626', linestyle=':', label=f'SL: {sl_price:.0f}')
        plt.axhline(y=trg_price, color='#7c3aed', linestyle=':', label=f'Target: {trg_price:.0f}')
        plt.title(f"{index_code} 5M V3 Confluence Setup", fontsize=11, fontweight='bold')
        plt.legend(loc='upper left', fontsize=8)
        plt.grid(True, linestyle=":", alpha=0.4)
        plt.tight_layout()

        chart_path = f"live_gann_alert_{index_code.lower()}.png"
        plt.savefig(chart_path, dpi=115)
        plt.close()

        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            with open(chart_path, "rb") as img:
                requests.post(
                    url,
                    data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption_text, "parse_mode": "HTML"},
                    files={"photo": img},
                    timeout=10
                )
        else:
            send_telegram(caption_text)
    except Exception:
        send_telegram(caption_text)

def now_ist() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)

def ts_now() -> str:
    return now_ist().strftime("%Y-%m-%d %H:%M:%S")

def safe_float(x: Any) -> Optional[float]:
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return None
        v = float(x)
        return None if math.isnan(v) else v
    except Exception:
        return None

def is_prime_entry_window(hhmm: str, timings: dict) -> bool:
    """
    सकाळी 09:20-12:00 आणि दुपारी 13:30-14:45 या सुवर्ण वेळेतच एन्ट्री (Lunch Chop 12:00-13:30 टाळले)
    """
    m_start = timings.get("session_start", MORNING_ENTRY_START)
    a_end = min(timings.get("entry_end", AFTERNOON_ENTRY_END), AFTERNOON_ENTRY_END)
    return (m_start <= hhmm <= MORNING_ENTRY_END) or (AFTERNOON_ENTRY_START <= hhmm <= a_end)


# ========================= GANN 0.50 HALF-STEP LATTICE =========================
def get_gann_levels(current_price: float, steps_each_side: int = 6) -> dict:
    base_half_root = math.floor(math.sqrt(current_price) * 2.0) / 2.0
    all_levels = sorted([
        round((base_half_root + (i * 0.5)) ** 2, 0)
        for i in range(-steps_each_side, steps_each_side + 1)
    ])

    supports = [lvl for lvl in all_levels if lvl <= current_price]
    resistances = [lvl for lvl in all_levels if lvl > current_price]

    immediate_sup = max(supports) if supports else round((base_half_root - 0.5) ** 2, 0)
    immediate_res = min(resistances) if resistances else round((base_half_root + 0.5) ** 2, 0)
    next_res = [lvl for lvl in resistances if lvl > immediate_res]
    next_sup = [lvl for lvl in supports if lvl < immediate_sup]

    return {
        "all_levels": all_levels,
        "immediate_support": immediate_sup,
        "immediate_resistance": immediate_res,
        "target_ce": next_res[0] if next_res else round((base_half_root + 1.0) ** 2, 0),
        "target_pe": next_sup[-1] if next_sup else round((base_half_root - 1.0) ** 2, 0)
    }


# ========================= UPSTOX V3 CLIENT =========================
class UpstoxV3Client:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Authorization": f"Bearer {token}"
        })
        self.last_call = 0.0

    def get(self, endpoint: str, params: Optional[dict] = None, min_interval: float = 0.12) -> dict:
        wait = min_interval - (time.monotonic() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        url = BASE_URL + endpoint
        r = self.session.get(url, params=params, timeout=10)
        self.last_call = time.monotonic()
        if r.status_code == 429:
            time.sleep(1.0)
            r = self.session.get(url, params=params, timeout=10)
            self.last_call = time.monotonic()
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def get_market_timings_and_holiday_status(self) -> dict:
        dt_now = now_ist()
        if dt_now.weekday() >= 5:
            return {
                "is_trading_day": False,
                "reason": "WEEKEND (Saturday/Sunday)",
                "session_start": MORNING_ENTRY_START,
                "entry_end": AFTERNOON_ENTRY_END,
                "force_exit": "15:20",
                "market_end": "15:30"
            }

        try:
            res = self.get(f"/v2/market/timings/{TODAY_STR}")
            rows = res.get("data", [])
            if isinstance(rows, list):
                fo_rows = [
                    x for x in rows
                    if str(x.get("exchange", "")).upper() in ("NSE_FO", "NSE", "NFO", "BSE_FO")
                ]
                if not fo_rows:
                    return {
                        "is_trading_day": False,
                        "reason": "EXCHANGE HOLIDAY (No NSE/BSE F&O Session Today)",
                        "session_start": MORNING_ENTRY_START,
                        "entry_end": AFTERNOON_ENTRY_END,
                        "force_exit": "15:20",
                        "market_end": "15:30"
                    }

                row = fo_rows[0]
                start_ms = int(row["start_time"])
                end_ms = int(row["end_time"])
                start_dt = datetime.fromtimestamp(start_ms / 1000.0, timezone.utc) + timedelta(hours=5, minutes=30)
                end_dt = datetime.fromtimestamp(end_ms / 1000.0, timezone.utc) + timedelta(hours=5, minutes=30)

                sess_start_dt = start_dt + timedelta(minutes=5)
                entry_end_dt = end_dt - timedelta(minutes=45)
                force_exit_dt = end_dt - timedelta(minutes=10)

                return {
                    "is_trading_day": True,
                    "reason": f"LIVE EXCHANGE SESSION ({start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')} IST)",
                    "session_start": sess_start_dt.strftime("%H:%M"),
                    "entry_end": entry_end_dt.strftime("%H:%M"),
                    "force_exit": force_exit_dt.strftime("%H:%M"),
                    "market_end": end_dt.strftime("%H:%M")
                }
        except Exception as e:
            print(f"[-] Market Timings API Warning (using IST fallback): {e}", flush=True)

        return {
            "is_trading_day": True,
            "reason": "STANDARD NSE/BSE SESSION (09:15 - 15:30 IST)",
            "session_start": MORNING_ENTRY_START,
            "entry_end": AFTERNOON_ENTRY_END,
            "force_exit": "15:20",
            "market_end": "15:30"
        }

    def get_ltp_v3(self, instrument_key: str) -> Optional[float]:
        try:
            data = self.get("/v3/market-quote/ltp", {"instrument_key": instrument_key})
            d = data.get("data", {})
            item = d.get(instrument_key) or (next(iter(d.values())) if d else {})
            return safe_float(item.get("last_price"))
        except Exception:
            return None

    def get_full_quote_and_greeks_v3(self, instrument_key: str) -> dict:
        info = {
            "ltp": None, "bid": None, "ask": None, "spread_pct": 0.0,
            "delta": 0.52, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "iv": 0.0,
            "oi": 0, "volume": 0
        }
        try:
            q_res = self.get("/v2/market-quote/quotes", {"instrument_key": instrument_key})
            qd = q_res.get("data", {})
            qitem = qd.get(instrument_key) or (next(iter(qd.values())) if qd else {})
            ltp = safe_float(qitem.get("last_price"))
            depth = qitem.get("depth") or {}
            buy_list = depth.get("buy") or []
            sell_list = depth.get("sell") or []
            bid = safe_float(buy_list[0].get("price")) if buy_list else None
            ask = safe_float(sell_list[0].get("price")) if sell_list else None

            spread_pct = 0.0
            if bid and ask and ltp and ltp > 0 and ask >= bid:
                spread_pct = round(((ask - bid) / ltp) * 100.0, 2)

            info.update({
                "ltp": ltp,
                "bid": bid,
                "ask": ask,
                "spread_pct": spread_pct,
                "oi": int(qitem.get("oi") or 0),
                "volume": int(qitem.get("volume") or 0)
            })
        except Exception:
            pass

        try:
            g_res = self.get("/v3/market-quote/option-greek", {"instrument_key": instrument_key})
            gd = g_res.get("data", {})
            gitem = gd.get(instrument_key) or (next(iter(gd.values())) if gd else {})
            if gitem:
                info["delta"] = safe_float(gitem.get("delta")) or info["delta"]
                info["gamma"] = safe_float(gitem.get("gamma")) or 0.0
                info["theta"] = safe_float(gitem.get("theta")) or 0.0
                info["vega"] = safe_float(gitem.get("vega")) or 0.0
                info["iv"] = safe_float(gitem.get("iv")) or 0.0
                if info["ltp"] is None:
                    info["ltp"] = safe_float(gitem.get("last_price"))
        except Exception:
            pass

        return info

    def get_fii_dii_sentiment(self) -> str:
        try:
            res = self.get("/v2/market/fii-dii-activity", {"date": TODAY_STR})
            d = res.get("data", {})
            fii_net = safe_float(d.get("fii_net_value") or d.get("fii_net"))
            if fii_net is not None:
                if fii_net > 500:
                    return f"FII BUY (+₹{fii_net:,.0f}Cr)"
                elif fii_net < -500:
                    return f"FII SELL (₹{fii_net:,.0f}Cr)"
                return "FII NEUTRAL"
        except Exception:
            pass
        return "N/A"

    def get_live_india_vix(self) -> dict:
        vix_val = self.get_ltp_v3("NSE_INDEX|India VIX")
        if vix_val is None or vix_val <= 0:
            return {"vix": None, "regime": "NORMAL", "max_iv": MAX_ALLOWED_IV, "implied_daily_pct": 0.0085}

        implied_daily_pct = round((vix_val / 100.0) / 15.87, 5)
        if vix_val < 13.0:
            regime, max_iv = "CALM", MAX_ALLOWED_IV
        elif vix_val <= 17.5:
            regime, max_iv = "NORMAL", MAX_ALLOWED_IV
        elif vix_val <= 22.0:
            regime, max_iv = "HIGH", MAX_ALLOWED_IV
        else:
            regime, max_iv = "PANIC", 26.0

        return {"vix": round(vix_val, 2), "regime": regime, "max_iv": max_iv, "implied_daily_pct": implied_daily_pct}

    def get_daily_expected_range_pct(self, spot_key: str) -> float:
        try:
            end_dt = now_ist().strftime("%Y-%m-%d")
            start_dt = (now_ist() - timedelta(days=30)).strftime("%Y-%m-%d")
            encoded_key = quote(spot_key, safe="")
            url = f"/v3/historical-candle/{encoded_key}/days/1/{end_dt}/{start_dt}"
            data = self.get(url)
            candles = data.get("data", {}).get("candles", [])
            if len(candles) >= 5:
                cols = ["timestamp", "open", "high", "low", "close", "volume", "oi"]
                df = pd.DataFrame(candles, columns=cols).sort_values("timestamp").reset_index(drop=True)
                prev_c = df["close"].shift(1)
                tr = pd.concat([
                    df["high"] - df["low"],
                    (df["high"] - prev_c).abs(),
                    (df["low"] - prev_c).abs()
                ], axis=1).max(axis=1)
                atr_pct = (tr / df["close"]).tail(14).mean()
                if pd.notna(atr_pct) and atr_pct > 0.002:
                    return float(atr_pct)
        except Exception:
            pass
        return 0.0095

    def get_intraday_candles_v3(self, instrument_key: str, interval_min: int = 5) -> pd.DataFrame:
        try:
            encoded_key = quote(instrument_key, safe="")
            url = f"/v3/historical-candle/intraday/{encoded_key}/minutes/{interval_min}"
            data = self.get(url)
            candles = data.get("data", {}).get("candles", [])
            if not candles:
                return pd.DataFrame()
            cols = ["timestamp", "open", "high", "low", "close", "volume", "oi"]
            df = pd.DataFrame(candles, columns=cols)
            df["dt"] = pd.to_datetime(df["timestamp"])
            if df["dt"].dt.tz is not None:
                df["dt"] = df["dt"].dt.tz_convert("Asia/Kolkata")
            else:
                df["dt"] = df["dt"].dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata")
            return df.sort_values("dt").reset_index(drop=True)
        except Exception:
            return pd.DataFrame()

    def get_futures_5m_metrics(self, fut_instrument_key: str) -> dict:
        result = {
            "valid": False, "fut_close": None, "fut_vwap": None, "above_fut_vwap": None,
            "fut_5m_vol": 0, "fut_avg_5m_vol": 0, "fut_vol_ratio": 1.0, "fut_vol_surge": False,
            "fut_oi": 0, "fut_5m_oi_chg": 0, "fut_buildup_code": "NEUTRAL", "fut_buildup_label": "NEUTRAL"
        }
        if not fut_instrument_key:
            return result

        try:
            df_5m = self.get_intraday_candles_v3(fut_instrument_key, interval_min=5)
            if df_5m.empty or len(df_5m) < 3:
                return result

            tp = (df_5m["high"] + df_5m["low"] + df_5m["close"]) / 3.0
            vol = df_5m["volume"].astype(float)
            df_5m["true_vwap"] = (tp * vol).cumsum() / vol.cumsum().replace(0, np.nan)

            cur_bar = df_5m.iloc[-1]
            prev_bar = df_5m.iloc[-2]

            fut_close = float(cur_bar["close"])
            fut_open = float(cur_bar["open"])
            fut_vwap = float(cur_bar["true_vwap"]) if pd.notna(cur_bar["true_vwap"]) else fut_close

            cur_vol = int(cur_bar["volume"])
            avg_vol = float(df_5m["volume"].tail(20).mean())
            vol_ratio = round(cur_vol / avg_vol, 2) if avg_vol > 0 else 1.0
            vol_surge = (cur_vol > 0) and (vol_ratio >= MIN_FUT_VOL_RATIO)

            cur_oi = int(cur_bar["oi"])
            prev_oi = int(prev_bar["oi"])
            oi_5m_chg = cur_oi - prev_oi

            if fut_close > fut_open and oi_5m_chg > 0:
                b_code, b_label = "FUT_LONG_BUILDUP", "LONG_BUILDUP"
            elif fut_close < fut_open and oi_5m_chg > 0:
                b_code, b_label = "FUT_SHORT_BUILDUP", "SHORT_BUILDUP"
            elif fut_close > fut_open and oi_5m_chg < 0:
                b_code, b_label = "FUT_SHORT_COVERING", "SHORT_COVERING"
            elif fut_close < fut_open and oi_5m_chg < 0:
                b_code, b_label = "FUT_LONG_UNWINDING", "LONG_UNWINDING"
            else:
                b_code, b_label = "FUT_NEUTRAL", "NEUTRAL"

            result.update({
                "valid": True,
                "fut_close": fut_close,
                "fut_vwap": round(fut_vwap, 2),
                "above_fut_vwap": fut_close >= fut_vwap,
                "fut_5m_vol": cur_vol,
                "fut_avg_5m_vol": int(avg_vol),
                "fut_vol_ratio": vol_ratio,
                "fut_vol_surge": vol_surge,
                "fut_oi": cur_oi,
                "fut_5m_oi_chg": oi_5m_chg,
                "fut_buildup_code": b_code,
                "fut_buildup_label": b_label
            })
        except Exception:
            pass
        return result

    def get_official_charges(self, instrument_key: str, qty: int, buy_price: float) -> dict:
        try:
            res = self.get("/v2/charges/brokerage", {
                "instrument_token": instrument_key,
                "quantity": qty,
                "product": "D",
                "transaction_type": "BUY",
                "price": buy_price
            })
            chg = float(res.get("data", {}).get("charges", {}).get("total", 0.0) or 0.0)
            if chg > 0:
                return {"total_charges": round(chg * 2.1, 2)}
        except Exception:
            pass
        return {"total_charges": round(45.0 + (qty * 0.12), 2)}

    def get_official_max_pain(self, spot_key: str, expiry: str) -> Optional[float]:
        try:
            res = self.get("/v2/market/max-pain", {
                "instrument_key": spot_key,
                "expiry": expiry,
                "date": TODAY_STR,
                "bucket_interval": 15
            })
            val = res.get("data", {}).get("max_pain")
            return float(val) if val is not None else None
        except Exception:
            return None

    def get_live_option_chain_memory(self, spot_key: str, expiry: str, target_strike: float, spot_price: float) -> dict:
        strike_band = round(spot_price * STRIKE_SCAN_RANGE_PCT)
        memory = {
            "atm_strike": target_strike,
            "total_call_oi": 0, "total_put_oi": 0,
            "total_call_chg": 0, "total_put_chg": 0,
            "delta_oi_pcr": 1.00,
            "ce": {"ltp": None, "delta": 0.52, "iv": 0.0, "theta": 0.0, "gamma": 0.0, "oi": 0, "chg_oi": 0},
            "pe": {"ltp": None, "delta": -0.52, "iv": 0.0, "theta": 0.0, "gamma": 0.0, "oi": 0, "chg_oi": 0}
        }
        try:
            res_chain = self.get("/v2/option/chain", {
                "instrument_key": spot_key,
                "expiry_date": expiry
            })
            chain_list = res_chain.get("data", [])

            tot_c_oi = tot_p_oi = tot_c_chg = tot_p_chg = 0
            found_chain_chg = False

            for row in chain_list:
                s_price = float(row.get("strike_price", 0))
                c_opt = row.get("call_options", {})
                p_opt = row.get("put_options", {})

                c_mkt = c_opt.get("market_data", {})
                p_mkt = p_opt.get("market_data", {})
                c_grk = c_opt.get("option_greeks", {})
                p_grk = p_opt.get("option_greeks", {})

                if abs(s_price - target_strike) <= strike_band:
                    c_oi = int(c_mkt.get("oi", 0) or 0)
                    c_prev = int(c_mkt.get("prev_oi", 0) or 0)
                    p_oi = int(p_mkt.get("oi", 0) or 0)
                    p_prev = int(p_mkt.get("prev_oi", 0) or 0)

                    tot_c_oi += c_oi
                    tot_p_oi += p_oi
                    if c_prev > 0 or p_prev > 0:
                        found_chain_chg = True
                        tot_c_chg += (c_oi - c_prev)
                        tot_p_chg += (p_oi - p_prev)

                if s_price == target_strike:
                    memory["ce"] = {
                        "ltp": safe_float(c_mkt.get("ltp")),
                        "delta": safe_float(c_grk.get("delta")) or 0.52,
                        "iv": safe_float(c_grk.get("iv")) or 0.0,
                        "theta": safe_float(c_grk.get("theta")) or 0.0,
                        "gamma": safe_float(c_grk.get("gamma")) or 0.0,
                        "oi": int(c_mkt.get("oi", 0) or 0),
                        "chg_oi": int((c_mkt.get("oi", 0) or 0) - (c_mkt.get("prev_oi", 0) or 0))
                    }
                    memory["pe"] = {
                        "ltp": safe_float(p_mkt.get("ltp")),
                        "delta": safe_float(p_grk.get("delta")) or -0.52,
                        "iv": safe_float(p_grk.get("iv")) or 0.0,
                        "theta": safe_float(p_grk.get("theta")) or 0.0,
                        "gamma": safe_float(p_grk.get("gamma")) or 0.0,
                        "oi": int(p_mkt.get("oi", 0) or 0),
                        "chg_oi": int((p_mkt.get("oi", 0) or 0) - (p_mkt.get("prev_oi", 0) or 0))
                    }

            if not found_chain_chg or (tot_c_chg == 0 and tot_p_chg == 0):
                try:
                    tot_c_chg = tot_p_chg = 0
                    res_chg = self.get("/v2/market/change-oi", {
                        "instrument_key": spot_key,
                        "expiry": expiry,
                        "date": TODAY_STR,
                        "interval": 1
                    })
                    for item in res_chg.get("data", {}).get("call_put_oi_data_list", []):
                        s_price = float(item.get("strike_price", 0))
                        if abs(s_price - target_strike) <= strike_band:
                            tot_c_chg += int(item.get("call_change_oi", 0))
                            tot_p_chg += int(item.get("put_change_oi", 0))
                        if s_price == target_strike:
                            memory["ce"]["chg_oi"] = int(item.get("call_change_oi", 0))
                            memory["pe"]["chg_oi"] = int(item.get("put_change_oi", 0))
                except Exception:
                    pass

            memory["total_call_oi"] = tot_c_oi
            memory["total_put_oi"] = tot_p_oi
            memory["total_call_chg"] = tot_c_chg
            memory["total_put_chg"] = tot_p_chg

            if tot_c_chg > 0:
                memory["delta_oi_pcr"] = round(tot_p_chg / tot_c_chg, 2)
            elif tot_p_chg > 0 and tot_c_chg <= 0:
                memory["delta_oi_pcr"] = 2.50
            elif tot_p_chg <= 0 and tot_c_chg > 0:
                memory["delta_oi_pcr"] = 0.40
            elif tot_c_oi > 0:
                memory["delta_oi_pcr"] = round(tot_p_oi / tot_c_oi, 2)
            else:
                memory["delta_oi_pcr"] = 1.00
        except Exception:
            memory["delta_oi_pcr"] = 1.00
        return memory


# ========================= INDICATORS & CAUSAL 0-100 FUEL SCORE =========================
def add_indicators_5m(df_5m: pd.DataFrame, fut_vol_ratio: float = 1.0) -> pd.DataFrame:
    x = df_5m.copy()
    if x.empty:
        return x

    c = x["close"]
    prev_close = c.shift(1)

    tp = (x["high"] + x["low"] + c) / 3.0
    v = x["volume"].replace(0, np.nan)
    x["vwap"] = (tp * v.fillna(1.0)).cumsum() / v.fillna(1.0).cumsum()
    x["ema20"] = c.ewm(span=20, adjust=False).mean()

    tr = pd.concat([
        x["high"] - x["low"],
        (x["high"] - prev_close).abs(),
        (x["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    x["atr14"] = tr.rolling(14, min_periods=5).mean()
    x["atr3"] = tr.rolling(3, min_periods=2).mean()

    up = x["high"].diff()
    dn = -x["low"].diff()
    plus = up.where((up > dn) & (up > 0), 0.0).rolling(14, min_periods=5).mean()
    minus = dn.where((dn > up) & (dn > 0), 0.0).rolling(14, min_periods=5).mean()
    dx = ((plus - minus).abs() / (plus + minus).replace(0, np.nan)) * 100.0
    x["adx14"] = dx.rolling(14, min_periods=5).mean()

    delta = c.diff()
    gain = (delta.where(delta > 0, 0.0)).rolling(14, min_periods=5).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(14, min_periods=5).mean()
    rs = gain / loss.replace(0, np.nan)
    x["rsi14"] = 100 - (100 / (1 + rs))

    vol_base = x["volume"].shift(1).rolling(8, min_periods=3).median()
    spot_vol_ratio = (x["volume"] / vol_base.replace(0, np.nan)).clip(0, 3)
    eff_vol_ratio = spot_vol_ratio.fillna(fut_vol_ratio)
    if len(x) > 0 and (pd.isna(spot_vol_ratio.iloc[-1]) or spot_vol_ratio.iloc[-1] <= 0):
        eff_vol_ratio.iloc[-1] = min(3.0, max(0.0, fut_vol_ratio))

    atr_safe = x["atr14"].replace(0, np.nan)
    atr_ratio = (x["atr3"] / atr_safe).clip(0, 2).fillna(1.0)
    ema_slope = (x["ema20"].diff(2) / atr_safe).fillna(0.0)
    vwap_dist_atr = ((c - x["vwap"]).abs() / atr_safe).fillna(0.0)
    candle_eff = ((c - x["open"]).abs() / (x["high"] - x["low"]).replace(0, np.nan)).fillna(0.0)

    vol_pts = (eff_vol_ratio / 2.0).clip(0, 1) * 30.0
    expansion_pts = ((atr_ratio - 0.7) / 0.8).clip(0, 1) * 25.0
    slope_pts = (ema_slope.abs() / 0.30).clip(0, 1) * 20.0
    efficiency_pts = candle_eff.clip(0, 1) * 15.0
    extension_penalty = ((vwap_dist_atr - 1.5) / 2.0).clip(0, 1) * 25.0

    x["fuel_score"] = (vol_pts + expansion_pts + slope_pts + efficiency_pts + 10.0 - extension_penalty).clip(0, 100)
    return x


def get_15m_ema20_bias(df_5m: pd.DataFrame) -> Optional[float]:
    if df_5m.empty or len(df_5m) < 12:
        return None
    try:
        df_15m = df_5m.set_index("dt").resample("15min", origin="start_day").agg({
            "close": "last"
        }).dropna()
        if len(df_15m) < 4:
            return None
        ema20_15m = df_15m["close"].ewm(span=20, adjust=False).mean().iloc[-1]
        return float(ema20_15m)
    except Exception:
        return None


def classify_5m_oi_buildup(is_green: bool, is_red: bool, shift_put_oi: int, shift_call_oi: int) -> Tuple[str, str]:
    if is_green:
        if shift_call_oi <= -MIN_5M_OI_SHIFT:
            return "MAJOR_SHORT_COVERING", "SHORT_COVERING"
        if shift_put_oi >= MIN_5M_OI_SHIFT and shift_call_oi <= 0:
            return "STRONG_PUT_WRITING", "PUT_WRITING"
        if shift_call_oi >= MIN_5M_OI_SHIFT and shift_put_oi <= -MIN_5M_OI_SHIFT:
            return "BULL_TRAP", "BULL_TRAP"
    elif is_red:
        if shift_put_oi <= -MIN_5M_OI_SHIFT:
            return "MAJOR_PUT_UNWINDING", "PUT_UNWINDING"
        if shift_call_oi >= MIN_5M_OI_SHIFT and shift_put_oi <= 0:
            return "STRONG_CALL_WRITING", "CALL_WRITING"
        if shift_put_oi >= MIN_5M_OI_SHIFT and shift_call_oi <= -MIN_5M_OI_SHIFT:
            return "BEAR_TRAP", "BEAR_TRAP"

    return "STEADY_TREND_FLOW", "STEADY"


# ========================= V3 5-INDEX CONFLUENCE ENGINE =========================
class MultiIndexPaperTrader:
    def __init__(self, api: UpstoxV3Client, timings: dict):
        self.api = api
        self.timings = timings
        self.trade_counter = 0
        self.cumulative_pnl = 0.0
        self.daily_locked = False
        self.consecutive_losses = 0
        self.last_pulse_bar_time = None
        self.recent_entry_times: List[float] = []
        self.fii_sentiment = self.api.get_fii_dii_sentiment()

        self.states: Dict[str, dict] = {}
        for idx_code in ACTIVE_INDICES:
            cfg = INDEX_CONFIGS[idx_code]
            self.states[idx_code] = {
                "code": idx_code,
                "display_name": cfg["display_name"],
                "spot_key": cfg["spot_key"],
                "fo_exchange": cfg["fo_exchange"],
                "lot_size": cfg["default_lot"],
                "strike_step": cfg["default_step"],
                "daily_expected_range_pct": self.api.get_daily_expected_range_pct(cfg["spot_key"]),
                "expiry": None,
                "fut_instrument_key": "",
                "fut_symbol": "",
                "contracts_cache": {},
                "active_position": None,
                "pending_setup": None,
                "cooldown_until": None,
                "failed_strikes": {},
                "prev_5m_oi_snapshot": None,
                "last_evaluated_5m_bar": None,
                "shift_5m_put_oi": 0,
                "shift_5m_call_oi": 0,
                "pcr_history": [],
                "pcr_slope": 0.0,
                "buildup_code": "STEADY_TREND_FLOW",
                "buildup_label": "STEADY",
                "latest_summary": None,
                "last_df_5m": pd.DataFrame(),
                "bull_vwap_expanded": False,
                "bear_vwap_expanded": False
            }

        self.load_all_indices_contracts_v3()
        print(f"[*] Upstox V3 5-Index Engine Ready (Final Production Build) | FII: {self.fii_sentiment}", flush=True)

    def load_all_indices_contracts_v3(self):
        df = pd.DataFrame()
        urls = [
            "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz",
            "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
        ]
        for url in urls:
            try:
                r = requests.get(url, timeout=45)
                r.raise_for_status()
                raw = gzip.decompress(r.content)
                if url.endswith(".json.gz"):
                    df = pd.DataFrame(json.loads(raw.decode("utf-8")))
                else:
                    df = pd.read_csv(io.BytesIO(raw), low_memory=False)
                if not df.empty:
                    print(f"[+] V3 Instrument Master Loaded ({len(df):,} rows) from {url.split('/')[-1]}", flush=True)
                    break
            except Exception as e:
                print(f"[-] Master load retry ({url.split('/')[-1]}): {e}", flush=True)

        if df.empty:
            print("[-] Warning: Instrument master empty!", flush=True)
            return

        df.columns = [str(c).strip().lower() for c in df.columns]
        sym_col = "trading_symbol" if "trading_symbol" in df.columns else "tradingsymbol"
        exch_col = "segment" if "segment" in df.columns else "exchange"
        name_col = "underlying_symbol" if "underlying_symbol" in df.columns else "name"
        strike_col = "strike_price" if "strike_price" in df.columns else "strike"

        raw_exp = df["expiry"]
        num_exp = pd.to_numeric(raw_exp, errors="coerce")
        parsed_exp = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
        nm = num_exp.notna()
        if nm.any():
            vals = num_exp[nm]
            ms = vals.abs() >= 10**11
            if ms.any():
                parsed_exp.loc[vals.index[ms]] = pd.to_datetime(vals[ms], unit="ms", errors="coerce")
            if (~ms).any():
                parsed_exp.loc[vals.index[~ms]] = pd.to_datetime(vals[~ms], unit="s", errors="coerce")
        if (~nm).any():
            parsed_exp.loc[~nm] = pd.to_datetime(raw_exp[~nm], errors="coerce")
        df["exp_str"] = parsed_exp.dt.strftime("%Y-%m-%d")

        df["exch_u"] = df[exch_col].fillna("").astype(str).str.strip().str.upper()
        df["name_u"] = df[name_col].fillna("").astype(str).str.strip().str.upper()
        df["type_u"] = df["instrument_type"].fillna("").astype(str).str.strip().str.upper()

        for idx_code in ACTIVE_INDICES:
            cfg = INDEX_CONFIGS[idx_code]
            st = self.states[idx_code]
            exch = cfg["fo_exchange"]
            names = [n.upper() for n in cfg["master_names"]]

            futs = df[(df["exch_u"] == exch) & (df["name_u"].isin(names)) & (df["type_u"].isin(["FUT", "FUTIDX"]))].copy()
            if not futs.empty:
                valid_futs = futs[futs["exp_str"].notna() & (futs["exp_str"] >= TODAY_STR)].sort_values("exp_str")
                if not valid_futs.empty:
                    top_fut = valid_futs.iloc[0]
                    st["fut_instrument_key"] = str(top_fut.get("instrument_key", "")).strip()
                    st["fut_symbol"] = str(top_fut.get(sym_col, "")).strip()
                    if "lot_size" in top_fut and pd.notna(top_fut["lot_size"]):
                        st["lot_size"] = int(float(top_fut["lot_size"]))

            opts = df[(df["exch_u"] == exch) & (df["name_u"].isin(names)) & (df["type_u"].isin(["CE", "PE", "OPTIDX"]))].copy()
            if not opts.empty:
                valid_exps = sorted([x for x in opts["exp_str"].dropna().unique() if x >= TODAY_STR])
                if valid_exps:
                    st["expiry"] = valid_exps[0]
                    active_opts = opts[opts["exp_str"] == st["expiry"]].copy()
                    if "lot_size" in active_opts.columns and not active_opts["lot_size"].dropna().empty:
                        st["lot_size"] = int(float(active_opts["lot_size"].dropna().iloc[0]))

                    active_opts["strike_num"] = pd.to_numeric(active_opts[strike_col], errors="coerce")
                    strikes_sorted = sorted(active_opts["strike_num"].dropna().unique())
                    if len(strikes_sorted) >= 2:
                        diffs = np.diff(strikes_sorted)
                        min_step = float(np.min(diffs[diffs > 0]))
                        if min_step > 0:
                            st["strike_step"] = min_step

                    for _, r in active_opts.iterrows():
                        strike = safe_float(r.get("strike_num"))
                        opt_type = str(r.get("option_type") if "option_type" in r and pd.notna(r.get("option_type")) else r.get("type_u", "")).strip().upper()
                        ikey = str(r.get("instrument_key", "")).strip()
                        sym = str(r.get(sym_col, "")).strip()
                        if strike and opt_type in ("CE", "PE") and ikey:
                            st["contracts_cache"][(strike, opt_type)] = {
                                "trading_symbol": sym,
                                "instrument_key": ikey
                            }

            print(
                f"[+] {idx_code:<10} | Fut: {st['fut_symbol'] or 'Spot-Only':<18} | "
                f"Exp: {st['expiry']} | Lot: {st['lot_size']:<4} | Step: {st['strike_step']:<5} | "
                f"Contracts: {len(st['contracts_cache'])}",
                flush=True
            )

    def calculate_remaining_daily_fuel(self, st: dict, df_5m: pd.DataFrame, spot_close: float, vix_info: dict) -> dict:
        today_high = float(df_5m["high"].max())
        today_low = float(df_5m["low"].min())
        used_pts = max(0.0, today_high - today_low)

        base_pct = st["daily_expected_range_pct"]
        blended_daily_pct = (base_pct + vix_info.get("implied_daily_pct", base_pct)) / 2.0
        expected_pts = round(spot_close * blended_daily_pct, 2)
        rem_pts = round(max(0.0, expected_pts - used_pts), 2)
        rem_pct = rem_pts / spot_close
        fuel_left_ratio = round((rem_pts / max(expected_pts, 1.0)) * 100.0, 1)

        return {
            "expected_pts": expected_pts,
            "used_pts": round(used_pts, 2),
            "remaining_pts": rem_pts,
            "remaining_pct": rem_pct,
            "fuel_left_pct_of_day": fuel_left_ratio
        }

    def update_5m_registers(self, st: dict, bar_time_str: str, is_green: bool, is_red: bool, mem: dict, delta_pcr: float):
        if st["last_evaluated_5m_bar"] == bar_time_str:
            return

        cur_put_chg = int(mem.get("total_put_chg", 0))
        cur_call_chg = int(mem.get("total_call_chg", 0))

        if st["prev_5m_oi_snapshot"] is not None:
            st["shift_5m_put_oi"] = cur_put_chg - st["prev_5m_oi_snapshot"]["put_chg"]
            st["shift_5m_call_oi"] = cur_call_chg - st["prev_5m_oi_snapshot"]["call_chg"]
            st["buildup_code"], st["buildup_label"] = classify_5m_oi_buildup(
                is_green, is_red, st["shift_5m_put_oi"], st["shift_5m_call_oi"]
            )
        else:
            st["shift_5m_put_oi"] = 0
            st["shift_5m_call_oi"] = 0
            st["buildup_code"] = "INITIAL_5M_SYNC"
            st["buildup_label"] = "SYNCED"

        st["prev_5m_oi_snapshot"] = {
            "bar_time": bar_time_str,
            "put_chg": cur_put_chg,
            "call_chg": cur_call_chg
        }

        hist = st["pcr_history"]
        hist.append(delta_pcr)
        if len(hist) > 4:
            hist.pop(0)

        st["pcr_slope"] = round(hist[-1] - hist[-2], 2) if len(hist) >= 2 else 0.0
        st["last_evaluated_5m_bar"] = bar_time_str

    def log_journal_csv(self, row: dict):
        exists = JOURNAL_CSV.exists()
        with open(JOURNAL_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            if not exists:
                writer.writeheader()
            writer.writerow(row)

    def count_active_positions(self) -> int:
        return sum(1 for st in self.states.values() if st["active_position"] is not None)

    def send_clean_visual_pulse(self, bar_time_str: str, vix_info: dict):
        if self.last_pulse_bar_time == bar_time_str:
            return
        self.last_pulse_bar_time = bar_time_str

        vix_val = vix_info.get("vix")
        vix_str = f"{vix_val:.1f}" if vix_val else "N/A"
        in_prime = is_prime_entry_window(bar_time_str, self.timings)
        win_tag = "PRIME 🟢" if in_prime else "LUNCH PAUSE ⏸️"

        lines = []
        for idx_code in ACTIVE_INDICES:
            st = self.states[idx_code]
            s = st.get("latest_summary")
            if not s:
                lines.append(f"⚪ <b>{idx_code:<10}</b>: <i>Loading...</i>")
                continue

            if st["active_position"]:
                pos = st["active_position"]
                status_badge = f"🔥 IN {pos['side']}"
                dot = "🔵"
            elif st["pending_setup"]:
                side = st["pending_setup"]["side"]
                wait_tag = "RETEST" if not st["pending_setup"].get("retest_touched") else "ARMED"
                status_badge = f"🎯 {wait_tag} {side}"
                dot = "🟠"
            elif s["delta_pcr"] >= 1.15 and s["spot"] > s["ema20"]:
                status_badge = "BULLISH 🟢"
                dot = "🟢"
            elif s["delta_pcr"] <= 0.85 and s["spot"] < s["ema20"]:
                status_badge = "BEARISH 🔴"
                dot = "🔴"
            else:
                status_badge = "WATCHING 👀"
                dot = "🟡"

            lines.append(
                f"{dot} <b>{idx_code:<10}</b>: <code>{s['spot']:.0f}</code> | "
                f"Fuel <code>{s['fuel_score']:.0f}</code> | "
                f"PCR <code>{s['delta_pcr']:.2f}</code> | <b>{status_badge}</b>"
            )

        msg = (
            f"📊 <b>5-INDEX V3 PULSE ({bar_time_str} IST | {win_tag})</b>\n"
            f"🌡️ VIX: <code>{vix_str}</code> | 💰 Day PnL: <b>₹{self.cumulative_pnl:+,.0f}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(lines)
        )
        send_telegram(msg)
        print(f"[{bar_time_str}] V3 Visual Pulse Sent ({win_tag}) | DayPnL: ₹{self.cumulative_pnl:+,.2f}", flush=True)

    def send_entry_alert(self, pos: dict, df_5m: pd.DataFrame):
        caption = (
            f"🚀 <b>ENTRY #{pos['trade_id']} | BUY {pos['clean_name']}</b> (Qty: {pos['lot_size']})\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💵 <b>Buy:</b> <code>₹{pos['entry_px']:.2f}</code> | 🛑 <b>SL:</b> <code>₹{pos['sl_px']:.2f}</code> (-{pos['opt_sl_pts']:.1f} pts)\n"
            f"🎯 <b>Target:</b> <code>₹{pos['trg_px']:.2f}</code> (+{pos['opt_trg_pts']:.1f} pts) | 📐 <b>Spot:</b> <code>{pos['spot_px']:.0f}</code>\n"
            f"⛽ <b>Fuel:</b> <code>{pos['fuel_score']:.0f}/100</code> | <b>ΔPCR:</b> <code>{pos['delta_pcr']:.2f}</code> | <b>Δ:</b> <code>{pos['entry_delta']:.2f}</code> | <b>IV:</b> <code>{pos['entry_iv']:.1f}%</code>\n"
            f"⚡ <b>Setup:</b> <code>{pos['setup_type']}</code> | ⏱️ <code>{pos['entry_time'].split(' ')[1]}</code>"
        )
        send_telegram_chart_alert(pos["index_code"], df_5m, pos["gann_level"], pos["spot_sl"], pos["spot_target"], caption)

    def send_periodic_update(self, pos: dict, current_ltp: float):
        cur_pts = round(current_ltp - pos["entry_px"], 2)
        cur_pnl = round(cur_pts * pos["lot_size"], 2)
        icon = "🟢" if cur_pnl >= 0 else "🔴"

        msg = (
            f"⏱️ <b>#{pos['trade_id']} {pos['clean_name']}</b> @ <code>₹{current_ltp:.2f}</code>\n"
            f"💵 Buy: <code>₹{pos['entry_px']:.2f}</code> | SL: <code>₹{pos['sl_px']:.2f}</code> | "
            f"{icon} <b>PnL: ₹{cur_pnl:+,.0f}</b> ({cur_pts:+.1f} pts)"
        )
        send_telegram(msg)

    def send_exit_alert(self, pos: dict):
        icon = "🎉" if pos["net_pnl"] > 0 else "⚠️"
        msg = (
            f"{icon} <b>EXIT #{pos['trade_id']} | {pos['clean_name']}</b> ({pos['exit_reason']})\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💵 <b>₹{pos['entry_px']:.2f} ➡️ ₹{pos['exit_px']:.2f}</b> ({pos['pts']:+.1f} pts)\n"
            f"💰 <b>Net PnL: ₹{pos['net_pnl']:+,.2f}</b> | 📈 <b>Day Total: ₹{self.cumulative_pnl:+,.2f}</b>"
        )
        send_telegram(msg)

    def execute_entry(self, st: dict, side: str, strike: float, ltp: float, instrument_key: str, symbol: str,
                      spot_px: float, gann_level: float, trigger_level: float, raw_spot_sl: float, gann_barrier: float,
                      atr_5m: float, fuel_info: dict, fuel_score: float, vix_info: dict, delta_pcr: float,
                      max_pain_display: Any, greeks: dict, oi_buildup: str, fut_mem: dict, setup_type: str, df_5m: pd.DataFrame):
        entry_price = ltp
        live_abs_delta = max(0.35, min(0.75, abs(float(greeks.get("delta", 0.52)))))
        gamma_val = abs(float(greeks.get("gamma", 0.0)))
        theta_val = abs(float(greeks.get("theta", 0.0)))
        spread_pct = float(greeks.get("spread_pct", 0.0))

        if spread_pct > MAX_SPREAD_PCT:
            print(f"[-] [{st['code']}] Skipped wide spread: {spread_pct:.2f}% > {MAX_SPREAD_PCT}%", flush=True)
            return

        atr_opt_sl = atr_5m * 1.2 * live_abs_delta
        min_opt_sl = max(ABS_MIN_OPT_SL_PTS, entry_price * MIN_OPT_SL_PCT)
        max_opt_sl = max(ABS_MIN_OPT_SL_PTS + 2.0, entry_price * MAX_OPT_SL_PCT)
        opt_sl_pts = round(min(max_opt_sl, max(min_opt_sl, atr_opt_sl)), 2)

        fuel_opt_cap = max(opt_sl_pts * 1.25, (fuel_info["remaining_pts"] * 0.70) * live_abs_delta)
        max_opt_trg = max(opt_sl_pts * 1.25, entry_price * MAX_OPT_TRG_PCT)
        opt_trg_pts = round(min(max_opt_trg, fuel_opt_cap, opt_sl_pts * REALISTIC_RRR), 2)

        expected_spot_move = opt_trg_pts / live_abs_delta
        directional_gain = (live_abs_delta * expected_spot_move) + (0.5 * gamma_val * (expected_spot_move ** 2))
        theta_burn_20m = theta_val * (EXPECTED_HOLD_MINUTES / 390.0)
        theta_ratio = theta_burn_20m / max(directional_gain, 0.01)
        if theta_ratio > MAX_THETA_BURN_RATIO:
            print(f"[-] [{st['code']}] Skipped high Theta decay ratio: {theta_ratio:.2f}", flush=True)
            return

        spot_risk = round(opt_sl_pts / live_abs_delta, 2)
        spot_reward = round(opt_trg_pts / live_abs_delta, 2)
        realistic_spot_sl = round((spot_px - spot_risk) if side == "CE" else (spot_px + spot_risk), 2)
        realistic_spot_target = round((spot_px + spot_reward) if side == "CE" else (spot_px - spot_reward), 2)

        opt_sl_pct = round((opt_sl_pts / entry_price) * 100.0, 1)
        opt_trg_pct = round((opt_trg_pts / entry_price) * 100.0, 1)

        sl = max(0.05, round(entry_price - opt_sl_pts, 2))
        trg = round(entry_price + opt_trg_pts, 2)
        self.trade_counter += 1
        self.recent_entry_times.append(time.monotonic())

        clean_name = f"{st['code']} {int(strike)} {side}"
        vix_val = vix_info.get("vix")

        pos = {
            "trade_id": self.trade_counter,
            "index_code": st["code"],
            "side": f"BUY_{side}",
            "clean_name": clean_name,
            "symbol": symbol,
            "instrument_key": instrument_key,
            "strike": strike,
            "lot_size": st["lot_size"],
            "entry_time": ts_now(),
            "entry_px": entry_price,
            "opt_sl_pts": opt_sl_pts,
            "opt_trg_pts": opt_trg_pts,
            "opt_sl_pct": opt_sl_pct,
            "opt_trg_pct": opt_trg_pct,
            "sl_px": sl,
            "trg_px": trg,
            "spot_px": spot_px,
            "gann_level": gann_level,
            "trigger_level": trigger_level,
            "spot_sl": realistic_spot_sl,
            "spot_target": realistic_spot_target,
            "fuel_score": round(fuel_score, 1),
            "rem_fuel_pts": fuel_info["remaining_pts"],
            "india_vix": f"{vix_val:.2f}" if vix_val else "N/A",
            "pcr": delta_pcr,
            "delta_pcr": delta_pcr,
            "max_pain": max_pain_display,
            "entry_delta": round(float(greeks.get("delta", 0.52)), 3),
            "entry_gamma": round(gamma_val, 5),
            "entry_iv": round(float(greeks.get("iv", 0.0)), 2),
            "entry_theta": round(float(greeks.get("theta", 0.0)), 2),
            "spread_pct": spread_pct,
            "theta_burn_20m": round(theta_burn_20m, 2),
            "oi_buildup": oi_buildup,
            "fut_buildup": fut_mem.get("fut_buildup_code", "N/A"),
            "fut_vwap": fut_mem.get("fut_vwap", "N/A"),
            "strike_oi": greeks.get("oi", 0),
            "change_oi": greeks.get("chg_oi", 0),
            "setup_type": setup_type,
            "runner_mode": False,
            "peak_price": entry_price,
            "last_periodic_update": time.monotonic()
        }

        st["active_position"] = pos
        print(f"\n[ENTRY] {clean_name} @ ₹{entry_price} | Fuel: {fuel_score:.0f}/100 | SL: ₹{sl} | TRG: ₹{trg}", flush=True)
        self.send_entry_alert(pos, df_5m)

    def execute_exit(self, st: dict, exit_price: float, reason: str):
        pos = st["active_position"]
        if not pos:
            return
        qty = pos["lot_size"]
        pts = round(exit_price - pos["entry_px"], 2)
        gross_pnl = round(pts * qty, 2)

        chg_info = self.api.get_official_charges(pos["instrument_key"], qty, pos["entry_px"])
        total_charges = chg_info["total_charges"]
        net_pnl = round(gross_pnl - total_charges, 2)
        self.cumulative_pnl += net_pnl

        pos["exit_time"] = ts_now()
        pos["exit_px"] = round(exit_price, 2)
        pos["pts"] = pts
        pos["gross_pnl"] = gross_pnl
        pos["charges"] = total_charges
        pos["net_pnl"] = net_pnl
        pos["exit_reason"] = reason
        pos["cumulative_pnl"] = self.cumulative_pnl

        print(f"\n[EXIT] {pos['clean_name']} @ ₹{exit_price} | Net: ₹{net_pnl} ({reason})", flush=True)
        self.send_exit_alert(pos)
        self.log_journal_csv(pos)

        if net_pnl < 0:
            self.consecutive_losses += 1
            st["failed_strikes"][pos["strike"]] = time.monotonic() + 2700
        else:
            self.consecutive_losses = 0

        if self.cumulative_pnl >= GLOBAL_DAILY_PROFIT_GOAL:
            self.daily_locked = True
            send_telegram(f"🔒 <b>DAILY GOAL HIT!</b> Profit Locked: <b>₹{self.cumulative_pnl:+,.2f}</b>")
        elif self.cumulative_pnl <= GLOBAL_DAILY_MAX_LOSS:
            self.daily_locked = True
            send_telegram(f"🛑 <b>MAX DAILY LOSS HIT!</b> Stopped for today: <b>₹{self.cumulative_pnl:+,.2f}</b>")
        elif self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            self.daily_locked = True
            send_telegram("🔒 <b>2 CONSECUTIVE SL LOCK:</b> Trading paused to protect capital.")

        st["active_position"] = None
        st["pending_setup"] = None
        st["cooldown_until"] = time.monotonic() + 300

    def check_all_positions_management(self):
        hhmm = now_ist().strftime("%H:%M")
        for idx_code in ACTIVE_INDICES:
            st = self.states[idx_code]
            pos = st["active_position"]
            if not pos:
                continue

            ltp = self.api.get_ltp_v3(pos["instrument_key"])
            if ltp is None:
                continue

            pos["peak_price"] = max(pos["peak_price"], ltp)
            gain = ltp - pos["entry_px"]

            if gain >= pos["opt_sl_pts"] and not pos["runner_mode"]:
                pos["runner_mode"] = True
                pos["sl_px"] = max(pos["sl_px"], pos["entry_px"] + 0.15 * pos["opt_sl_pts"])
                send_telegram(f"🛡️ <b>[{idx_code}] Runner Mode ON:</b> SL trailed to <code>₹{pos['sl_px']:.2f}</code>")

            if gain >= (pos["opt_sl_pts"] * 1.5):
                new_sl = round(ltp - (pos["opt_sl_pts"] * 0.5), 2)
                if new_sl > pos["sl_px"]:
                    pos["sl_px"] = new_sl

            df_5m = st.get("last_df_5m", pd.DataFrame())
            if not df_5m.empty and len(df_5m) >= NO_MOMENTUM_BARS + 1 and not pos["runner_mode"]:
                recent = df_5m.tail(NO_MOMENTUM_BARS + 1)
                atr_now = safe_float(recent.iloc[-1].get("atr14")) or 0.0
                if atr_now > 0:
                    spot_move = abs(float(recent.iloc[-1]["close"]) - float(recent.iloc[0]["close"]))
                    if spot_move < (NO_MOMENTUM_MOVE_ATR * atr_now) and ltp <= pos["entry_px"] * 0.985:
                        self.execute_exit(st, ltp, "NO_MOMENTUM_EXIT ⏳")
                        continue

            if time.monotonic() - pos["last_periodic_update"] >= POSITION_UPDATE_INTERVAL:
                self.send_periodic_update(pos, ltp)
                pos["last_periodic_update"] = time.monotonic()

            if ltp <= pos["sl_px"]:
                reason = "TRAILED_EXIT ✅" if pos["sl_px"] >= pos["entry_px"] else "OPTION_SL_HIT ❌"
                self.execute_exit(st, pos["sl_px"], reason)
            elif ltp >= pos["trg_px"]:
                self.execute_exit(st, pos["trg_px"], "TARGET_HIT 🎯")
            elif hhmm >= self.timings["force_exit"]:
                self.execute_exit(st, ltp, "FORCE_EXIT_SESSION ⏱️")

    def scan_single_index(self, idx_code: str, hhmm: str, vix_info: dict) -> Optional[str]:
        st = self.states[idx_code]
        if not st["expiry"]:
            return None

        df_5m = self.api.get_intraday_candles_v3(st["spot_key"], interval_min=5)
        if df_5m.empty or len(df_5m) < 4:
            return None

        fut_mem = self.api.get_futures_5m_metrics(st["fut_instrument_key"])
        fut_vol_ratio = float(fut_mem.get("fut_vol_ratio", 1.0))

        df_5m = add_indicators_5m(df_5m, fut_vol_ratio=fut_vol_ratio)
        st["last_df_5m"] = df_5m

        completed_bar = df_5m.iloc[-1]
        prev_bar = df_5m.iloc[-2]
        bar_time_str = completed_bar["dt"].strftime("%H:%M")

        spot_close = float(completed_bar["close"])
        spot_open = float(completed_bar["open"])
        spot_high = float(completed_bar["high"])
        spot_low = float(completed_bar["low"])
        prev_close = float(prev_bar["close"])
        spot_vwap_5m = float(completed_bar["vwap"])
        ema20_5m = float(completed_bar.get("ema20", spot_close))
        ema20_15m = get_15m_ema20_bias(df_5m)
        adx_val = float(completed_bar.get("adx14", 0.0)) if pd.notna(completed_bar.get("adx14")) else 0.0
        atr_val = float(completed_bar.get("atr14", spot_close * 0.0006)) if pd.notna(completed_bar.get("atr14")) else (spot_close * 0.0006)
        fuel_score = float(completed_bar.get("fuel_score", 0.0)) if pd.notna(completed_bar.get("fuel_score")) else 0.0

        if (df_5m["high"] >= df_5m["vwap"] * (1.0 + VWAP_EXPANSION_PCT)).any():
            st["bull_vwap_expanded"] = True
        if (df_5m["low"] <= df_5m["vwap"] * (1.0 - VWAP_EXPANSION_PCT)).any():
            st["bear_vwap_expanded"] = True

        fuel_info = self.calculate_remaining_daily_fuel(st, df_5m, spot_close, vix_info)
        is_green = spot_close > spot_open
        is_red = spot_close < spot_open

        max_pain = self.api.get_official_max_pain(st["spot_key"], st["expiry"])
        max_pain_display = max_pain if max_pain is not None else "N/A"

        atm_strike = round(spot_close / st["strike_step"]) * st["strike_step"]
        mem = self.api.get_live_option_chain_memory(st["spot_key"], st["expiry"], atm_strike, spot_close)
        delta_pcr = mem.get("delta_oi_pcr") or 1.00

        gann = get_gann_levels(spot_close, steps_each_side=6)
        sup_level = gann["immediate_support"]
        res_level = gann["immediate_resistance"]
        gann_prox_pts = spot_close * GANN_PROXIMITY_PCT
        vwap_band_pts = spot_close * VWAP_TOUCH_BAND_PCT

        self.update_5m_registers(st, bar_time_str, is_green, is_red, mem, delta_pcr)
        pcr_slope = st["pcr_slope"]

        st["latest_summary"] = {
            "lot_size": st["lot_size"],
            "spot": spot_close,
            "ema20": ema20_5m,
            "sup": sup_level,
            "res": res_level,
            "fuel_score": fuel_score,
            "delta_pcr": delta_pcr,
            "pcr_slope": pcr_slope,
            "adx": adx_val,
            "max_pain": max_pain_display,
            "fuel_pct": fuel_info["fuel_left_pct_of_day"],
            "vol_ratio": fut_vol_ratio,
            "atr": atr_val
        }

        if st["active_position"] or self.daily_locked:
            return bar_time_str

        # Prime Entry Time Window Filter (12:00 - 13:30 Lunch Chop Blocked)
        if not is_prime_entry_window(hhmm, self.timings):
            st["pending_setup"] = None
            return bar_time_str

        if st["cooldown_until"] and time.monotonic() < st["cooldown_until"]:
            return bar_time_str
        if self.count_active_positions() >= MAX_SIMULTANEOUS_POSITIONS:
            return bar_time_str

        now_m = time.monotonic()
        self.recent_entry_times = [t for t in self.recent_entry_times if (now_m - t) < 3600]
        if len(self.recent_entry_times) >= MAX_TRADES_PER_60_MIN:
            return bar_time_str

        if len(df_5m) < 6 or fuel_info["remaining_pct"] < MIN_REMAINING_FUEL_PCT:
            return bar_time_str
        if st["buildup_code"] in ("BULL_TRAP", "BEAR_TRAP"):
            st["pending_setup"] = None
            return bar_time_str

        c_range = max(spot_high - spot_low, 1e-9)
        c_body = abs(spot_close - spot_open)
        body_ratio = c_body / c_range
        lower_wick_ratio = (min(spot_open, spot_close) - spot_low) / c_range
        upper_wick_ratio = (spot_high - max(spot_open, spot_close)) / c_range

        fut_vol_surge = fut_mem.get("fut_vol_surge", False)
        is_range_expansion = c_range >= (MIN_RANGE_ATR_MULT * atr_val)
        is_body_solid = body_ratio >= MIN_BODY_RATIO

        fut_valid = fut_mem.get("valid", False)
        above_fut_vwap = fut_mem.get("above_fut_vwap", True) if fut_valid else True
        below_fut_vwap = (not fut_mem.get("above_fut_vwap", False)) if fut_valid else True

        htf_bull_ok = (ema20_15m is None) or (spot_close >= ema20_15m * 0.9992)
        htf_bear_ok = (ema20_15m is None) or (spot_close <= ema20_15m * 1.0008)

        prior_3 = df_5m.iloc[-4:-1]
        swing_3_high = float(prior_3["high"].max())
        swing_3_low = float(prior_3["low"].min())
        day_high_so_far = float(df_5m["high"].max())
        day_low_so_far = float(df_5m["low"].min())

        swing_pad_pts = round(spot_close * 0.0002, 2)
        retest_buf_pts = round(RETEST_ATR_BUFFER * atr_val, 2)

        # =========================================================================
        # STAGE 1: BREAKOUT + RETEST SETUP DETECTION (WITH FUEL >= 70 & T1 GUARD)
        # =========================================================================
        if not st["pending_setup"]:
            # ---------------------- 🟢 CE SETUPS ----------------------
            gann_sup_bounce_ce = is_green and is_body_solid and (spot_low <= sup_level + gann_prox_pts) and (spot_close >= sup_level - (gann_prox_pts * 0.5))
            gann_res_breakout_ce = is_green and is_body_solid and (prev_close <= res_level) and (spot_close > res_level)
            has_gann_ce = (fut_vol_surge or is_range_expansion) and (gann_sup_bounce_ce or gann_res_breakout_ce) and (spot_close > spot_vwap_5m) and above_fut_vwap and htf_bull_ok

            has_pa_momentum_ce = (
                is_green and is_body_solid and (adx_val >= MIN_ADX_MOMENTUM)
                and (spot_close > spot_vwap_5m) and above_fut_vwap and (spot_close > ema20_5m) and htf_bull_ok
                and (spot_close > swing_3_high) and (fut_vol_surge or is_range_expansion)
            )

            near_day_low = (spot_low <= day_low_so_far + (0.35 * atr_val))
            has_pa_reversal_ce = (
                is_green and near_day_low and (lower_wick_ratio >= MIN_REJECTION_WICK_RATIO)
                and (spot_close > ema20_5m) and (delta_pcr >= 0.65 or pcr_slope > 0)
            )

            touched_vwap_ce = (
                st["bull_vwap_expanded"]
                and is_green and is_body_solid
                and (spot_low <= spot_vwap_5m + vwap_band_pts) and (spot_close > spot_vwap_5m)
                and above_fut_vwap and htf_bull_ok
            )
            has_vwap_or_pcr_ce = (delta_pcr > 1.15 and touched_vwap_ce) or (
                is_green and is_body_solid and (pcr_slope >= PCR_SLOPE_BULL_THRESHOLD) and (prev_close <= spot_vwap_5m) and (spot_close > spot_vwap_5m)
            )

            if has_gann_ce or has_pa_momentum_ce or has_pa_reversal_ce or has_vwap_or_pcr_ce:
                if has_gann_ce:
                    setup_name = f"GANN_{int(res_level if gann_res_breakout_ce else sup_level)}_CE"
                    anchor_gann = res_level if gann_res_breakout_ce else sup_level
                    next_barrier = gann["target_ce"] if gann_res_breakout_ce else res_level
                    needs_retest = True
                elif has_pa_momentum_ce:
                    setup_name = "FUT_BREAKOUT_RETEST_CE"
                    anchor_gann, next_barrier, needs_retest = sup_level, res_level, True
                elif has_pa_reversal_ce:
                    setup_name = "PA_REVERSAL_CE"
                    anchor_gann, next_barrier, needs_retest = sup_level, res_level, False
                else:
                    setup_name = "VWAP_PCR_CE"
                    anchor_gann, next_barrier, needs_retest = sup_level, res_level, False

                trig_lvl = round(spot_high + (spot_close * ENTRY_BUFFER_PCT), 2)
                raw_sl_lvl = round(spot_low - swing_pad_pts, 2)
                spot_t1_lvl = round(trig_lvl + max(1.5 * atr_val, (trig_lvl - raw_sl_lvl) * 1.5), 2)

                if spot_high >= spot_t1_lvl:
                    print(f"[X] [{idx_code}] CANCELLED_T1_HIT_ON_FIRST_TRIGGER_CANDLE (CE)", flush=True)
                    return bar_time_str

                low_fuel_mode = (fuel_score < MIN_FUEL_SCORE)
                st["pending_setup"] = {
                    "side": "CE",
                    "setup_type": setup_name,
                    "oi_buildup": st["buildup_code"],
                    "gann_level": anchor_gann,
                    "gann_barrier": next_barrier,
                    "breakout_level": spot_high,
                    "retest_min": round(spot_high - retest_buf_pts, 2),
                    "retest_max": round(spot_high + retest_buf_pts, 2),
                    "trigger_level": trig_lvl,
                    "spot_t1": spot_t1_lvl,
                    "retest_touched": (not needs_retest),
                    "needs_fuel_reclaim": low_fuel_mode,
                    "fuel_reclaimed": (not low_fuel_mode),
                    "armed_bar_time": bar_time_str,
                    "raw_spot_sl": raw_sl_lvl,
                    "atr_5m": atr_val,
                    "armed_time": time.monotonic()
                }
                mode_lbl = f"LOW_FUEL({fuel_score:.0f}<70) WAIT_RECLAIM" if low_fuel_mode else f"RETEST_ARMED(Fuel:{fuel_score:.0f})"
                print(f"[!] [{idx_code}] CE Armed [{mode_lbl}]: {setup_name} @ {trig_lvl} (Spot T1: {spot_t1_lvl})", flush=True)

            # ---------------------- 🔴 PE SETUPS ----------------------
            else:
                gann_res_reject_pe = is_red and is_body_solid and (spot_high >= res_level - gann_prox_pts) and (spot_close <= res_level + (gann_prox_pts * 0.5))
                gann_sup_breakdown_pe = is_red and is_body_solid and (prev_close >= sup_level) and (spot_close < sup_level)
                has_gann_pe = (fut_vol_surge or is_range_expansion) and (gann_res_reject_pe or gann_sup_breakdown_pe) and (spot_close < spot_vwap_5m) and below_fut_vwap and htf_bear_ok

                has_pa_momentum_pe = (
                    is_red and is_body_solid and (adx_val >= MIN_ADX_MOMENTUM)
                    and (spot_close < spot_vwap_5m) and below_fut_vwap and (spot_close < ema20_5m) and htf_bear_ok
                    and (spot_close < swing_3_low) and (fut_vol_surge or is_range_expansion)
                )

                near_day_high = (spot_high >= day_high_so_far - (0.35 * atr_val))
                has_pa_reversal_pe = (
                    is_red and near_day_high and (upper_wick_ratio >= MIN_REJECTION_WICK_RATIO)
                    and (spot_close < ema20_5m) and (delta_pcr <= 1.25 or pcr_slope < 0)
                )

                touched_vwap_pe = (
                    st["bear_vwap_expanded"]
                    and is_red and is_body_solid
                    and (spot_high >= spot_vwap_5m - vwap_band_pts) and (spot_close < spot_vwap_5m)
                    and below_fut_vwap and htf_bear_ok
                )
                has_vwap_or_pcr_pe = (delta_pcr < 0.85 and touched_vwap_pe) or (
                    is_red and is_body_solid and (pcr_slope <= PCR_SLOPE_BEAR_THRESHOLD) and (prev_close >= spot_vwap_5m) and (spot_close < spot_vwap_5m)
                )

                if has_gann_pe or has_pa_momentum_pe or has_pa_reversal_pe or has_vwap_or_pcr_pe:
                    if has_gann_pe:
                        setup_name = f"GANN_{int(sup_level if gann_sup_breakdown_pe else res_level)}_PE"
                        anchor_gann = sup_level if gann_sup_breakdown_pe else res_level
                        next_barrier = gann["target_pe"] if gann_sup_breakdown_pe else sup_level
                        needs_retest = True
                    elif has_pa_momentum_pe:
                        setup_name = "FUT_BREAKOUT_RETEST_PE"
                        anchor_gann, next_barrier, needs_retest = res_level, sup_level, True
                    elif has_pa_reversal_pe:
                        setup_name = "PA_REVERSAL_PE"
                        anchor_gann, next_barrier, needs_retest = res_level, sup_level, False
                    else:
                        setup_name = "VWAP_PCR_PE"
                        anchor_gann, next_barrier, needs_retest = res_level, sup_level, False

                    trig_lvl = round(spot_low - (spot_close * ENTRY_BUFFER_PCT), 2)
                    raw_sl_lvl = round(spot_high + swing_pad_pts, 2)
                    spot_t1_lvl = round(trig_lvl - max(1.5 * atr_val, (raw_sl_lvl - trig_lvl) * 1.5), 2)

                    if spot_low <= spot_t1_lvl:
                        print(f"[X] [{idx_code}] CANCELLED_T1_HIT_ON_FIRST_TRIGGER_CANDLE (PE)", flush=True)
                        return bar_time_str

                    low_fuel_mode = (fuel_score < MIN_FUEL_SCORE)
                    st["pending_setup"] = {
                        "side": "PE",
                        "setup_type": setup_name,
                        "oi_buildup": st["buildup_code"],
                        "gann_level": anchor_gann,
                        "gann_barrier": next_barrier,
                        "breakout_level": spot_low,
                        "retest_min": round(spot_low - retest_buf_pts, 2),
                        "retest_max": round(spot_low + retest_buf_pts, 2),
                        "trigger_level": trig_lvl,
                        "spot_t1": spot_t1_lvl,
                        "retest_touched": (not needs_retest),
                        "needs_fuel_reclaim": low_fuel_mode,
                        "fuel_reclaimed": (not low_fuel_mode),
                        "armed_bar_time": bar_time_str,
                        "raw_spot_sl": raw_sl_lvl,
                        "atr_5m": atr_val,
                        "armed_time": time.monotonic()
                    }
                    mode_lbl = f"LOW_FUEL({fuel_score:.0f}<70) WAIT_RECLAIM" if low_fuel_mode else f"RETEST_ARMED(Fuel:{fuel_score:.0f})"
                    print(f"[!] [{idx_code}] PE Armed [{mode_lbl}]: {setup_name} @ {trig_lvl} (Spot T1: {spot_t1_lvl})", flush=True)

        # =========================================================================
        # STAGE 2: T1 GUARD + ±0.30*ATR RETEST CONFIRMATION + FUEL RECLAIM + ENTRY
        # =========================================================================
        if st["pending_setup"]:
            setup = st["pending_setup"]
            if time.monotonic() - setup["armed_time"] > 900:
                print(f"[X] [{idx_code}] CANCELLED_SETUP_EXPIRED ({setup['setup_type']})", flush=True)
                st["pending_setup"] = None
                return bar_time_str

            current_spot_tick = self.api.get_ltp_v3(st["spot_key"]) or spot_close

            if setup["side"] == "CE":
                if spot_high >= setup["spot_t1"] or current_spot_tick >= setup["spot_t1"]:
                    print(f"[X] [{idx_code}] CANCELLED_T1_HIT_BEFORE_ENTRY (CE) | Spot: {current_spot_tick} >= T1: {setup['spot_t1']}", flush=True)
                    st["pending_setup"] = None
                    return bar_time_str

                if current_spot_tick < setup["raw_spot_sl"]:
                    st["pending_setup"] = None
                    return bar_time_str

                if setup["retest_min"] <= current_spot_tick <= setup["retest_max"]:
                    setup["retest_touched"] = True

                if setup.get("needs_fuel_reclaim") and not setup.get("fuel_reclaimed"):
                    if bar_time_str != setup.get("armed_bar_time"):
                        if fuel_score >= MIN_FUEL_SCORE and (prev_close <= setup["trigger_level"] and spot_close > setup["trigger_level"]):
                            setup["fuel_reclaimed"] = True
                            print(f"[+] [{idx_code}] CE Fuel Recovered ({fuel_score:.0f}>=70) & Reclaimed!", flush=True)

                triggered = (
                    setup["retest_touched"]
                    and setup.get("fuel_reclaimed", True)
                    and (fuel_score >= MIN_FUEL_SCORE)
                    and (current_spot_tick >= setup["trigger_level"])
                )
            else:
                if spot_low <= setup["spot_t1"] or current_spot_tick <= setup["spot_t1"]:
                    print(f"[X] [{idx_code}] CANCELLED_T1_HIT_BEFORE_ENTRY (PE) | Spot: {current_spot_tick} <= T1: {setup['spot_t1']}", flush=True)
                    st["pending_setup"] = None
                    return bar_time_str

                if current_spot_tick > setup["raw_spot_sl"]:
                    st["pending_setup"] = None
                    return bar_time_str

                if setup["retest_min"] <= current_spot_tick <= setup["retest_max"]:
                    setup["retest_touched"] = True

                if setup.get("needs_fuel_reclaim") and not setup.get("fuel_reclaimed"):
                    if bar_time_str != setup.get("armed_bar_time"):
                        if fuel_score >= MIN_FUEL_SCORE and (prev_close >= setup["trigger_level"] and spot_close < setup["trigger_level"]):
                            setup["fuel_reclaimed"] = True
                            print(f"[+] [{idx_code}] PE Fuel Recovered ({fuel_score:.0f}>=70) & Reclaimed!", flush=True)

                triggered = (
                    setup["retest_touched"]
                    and setup.get("fuel_reclaimed", True)
                    and (fuel_score >= MIN_FUEL_SCORE)
                    and (current_spot_tick <= setup["trigger_level"])
                )

            if triggered:
                atm_strike = round(current_spot_tick / st["strike_step"]) * st["strike_step"]

                if atm_strike in st["failed_strikes"]:
                    if time.monotonic() < st["failed_strikes"][atm_strike]:
                        st["pending_setup"] = None
                        return bar_time_str
                    else:
                        del st["failed_strikes"][atm_strike]

                contract_info = st["contracts_cache"].get((atm_strike, setup["side"]))
                if not contract_info:
                    return bar_time_str

                inst_key = contract_info["instrument_key"]
                trad_sym = contract_info["trading_symbol"]

                v3_opt_data = self.api.get_full_quote_and_greeks_v3(inst_key)
                chain_opt = mem["ce"] if setup["side"] == "CE" else mem["pe"]

                opt_greeks = {
                    "ltp": v3_opt_data.get("ltp") or chain_opt.get("ltp"),
                    "delta": v3_opt_data.get("delta") or chain_opt.get("delta", 0.52),
                    "gamma": v3_opt_data.get("gamma") or chain_opt.get("gamma", 0.0),
                    "theta": v3_opt_data.get("theta") or chain_opt.get("theta", 0.0),
                    "vega": v3_opt_data.get("vega") or chain_opt.get("vega", 0.0),
                    "iv": v3_opt_data.get("iv") or chain_opt.get("iv", 0.0),
                    "spread_pct": v3_opt_data.get("spread_pct", 0.0),
                    "oi": v3_opt_data.get("oi") or chain_opt.get("oi", 0),
                    "chg_oi": chain_opt.get("chg_oi", 0)
                }

                live_iv = float(opt_greeks.get("iv", 0.0))
                live_abs_delta = abs(float(opt_greeks.get("delta", 0.52)))
                max_iv_limit = vix_info.get("max_iv", MAX_ALLOWED_IV)

                if live_iv > max_iv_limit or live_abs_delta < MIN_ABS_DELTA:
                    st["pending_setup"] = None
                    return bar_time_str

                ltp = opt_greeks.get("ltp")
                min_valid_prem = current_spot_tick * 0.0010
                max_valid_prem = current_spot_tick * 0.0250

                if ltp and min_valid_prem <= ltp <= max_valid_prem:
                    self.execute_entry(
                        st=st,
                        side=setup["side"],
                        strike=atm_strike,
                        ltp=ltp,
                        instrument_key=inst_key,
                        symbol=trad_sym,
                        spot_px=current_spot_tick,
                        gann_level=setup["gann_level"],
                        trigger_level=setup["trigger_level"],
                        raw_spot_sl=setup["raw_spot_sl"],
                        gann_barrier=setup["gann_barrier"],
                        atr_5m=setup["atr_5m"],
                        fuel_info=fuel_info,
                        fuel_score=fuel_score,
                        vix_info=vix_info,
                        delta_pcr=delta_pcr,
                        max_pain_display=max_pain_display,
                        greeks=opt_greeks,
                        oi_buildup=setup["oi_buildup"],
                        fut_mem=fut_mem,
                        setup_type=setup["setup_type"],
                        df_5m=df_5m
                    )
                    st["pending_setup"] = None

        return bar_time_str

    def run_all_indices_scan(self):
        hhmm = now_ist().strftime("%H:%M")
        if not (self.timings["session_start"] <= hhmm <= self.timings["entry_end"]):
            return

        vix_info = self.api.get_live_india_vix()
        latest_bar_time = None

        for idx_code in ACTIVE_INDICES:
            try:
                bt = self.scan_single_index(idx_code, hhmm, vix_info)
                if bt:
                    latest_bar_time = bt
            except Exception as e:
                print(f"[-] Scan Error [{idx_code}]: {e}", flush=True)

        if latest_bar_time:
            self.send_clean_visual_pulse(latest_bar_time, vix_info)


# ========================= MAIN =========================
def main():
    global TOKEN
    if not TOKEN:
        TOKEN = fetch_token()

    if not TOKEN:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN उपलब्ध नाही. कृपया ACCESS_TOKEN.txt तपासा.")

    api = UpstoxV3Client(TOKEN)
    timings = api.get_market_timings_and_holiday_status()

    if not timings.get("is_trading_day", True):
        msg = f"📅 <b>MARKET CLOSED / HOLIDAY ({TODAY_STR})</b>\nकारण: <code>{timings.get('reason')}</code>\nसिस्टीम सुरक्षित थांबवली आहे."
        print(f"[*] {timings.get('reason')} — Exiting cleanly.", flush=True)
        send_telegram(msg)
        return

    trader = MultiIndexPaperTrader(api, timings)

    send_telegram(
        f"🟢 <b>5-INDEX V3 CONFLUENCE ALGO STARTED</b> ({TODAY_STR})\n"
        f"📊 NIFTY · BANKNIFTY · FINNIFTY · MIDCPNIFTY · SENSEX\n"
        f"⏰ Windows: <b>09:20–12:00 & 13:30–14:45 IST</b> | 🛡️ Breakout-Retest + Min 10pt SL"
    )
    print(f"[*] Upstox V3 5-Index Engine Active (Prime Windows: 09:20-12:00 & 13:30-14:45 IST)...", flush=True)

    last_strat = 0.0
    try:
        while True:
            hhmm = now_ist().strftime("%H:%M")
            if hhmm >= timings["market_end"]:
                print(f"[*] {hhmm}: Market session संपले. Live Engine थांबवत आहे...", flush=True)
                send_telegram(f"⏱️ <b>SESSION ENDED ({hhmm})</b> | Day PnL: <b>₹{trader.cumulative_pnl:+,.2f}</b>")
                break

            trader.check_all_positions_management()

            if timings["session_start"] <= hhmm <= timings["entry_end"]:
                if time.monotonic() - last_strat >= STRATEGY_INTERVAL:
                    trader.run_all_indices_scan()
                    last_strat = time.monotonic()

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n[!] Manually stopped.", flush=True)
        send_telegram("⚠️ <b>5-INDEX ALGO: Manually Stopped</b>")


if __name__ == "__main__":
    main()
