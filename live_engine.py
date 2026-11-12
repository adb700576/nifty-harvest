import os
import time
import math
import json
import csv
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Any, Tuple
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

# ========================= SCALE-INVARIANT PERCENTAGE PARAMETERS =========================
POLL_INTERVAL = 1.0
STRATEGY_INTERVAL = 10.0
POSITION_UPDATE_INTERVAL = 180.0

# Spot Percentage Parameters
MIN_SPOT_SL_PCT = 0.0005           # किमान Spot SL = 0.05%
MAX_SPOT_SL_PCT = 0.0014           # कमाल Spot SL = 0.14%
GANN_PROXIMITY_PCT = 0.0010        # Gann Level जवळचा झोन = 0.10%
GANN_RETEST_BAND_PCT = 0.0018      # Retest झोन = 0.18%
ENTRY_BUFFER_PCT = 0.00025         # Breakout Buffer = 0.025%
MIN_REMAINING_FUEL_PCT = 0.0010    # दिवसाचे किमान शिल्लक इंधन = 0.10%
STRIKE_SCAN_RANGE_PCT = 0.020      # Spot च्या ±2.0% रेंजमधील सर्व स्ट्राइक्स

# Option Premium Percentage Parameters
MIN_OPT_SL_PCT = 0.10              # ऑप्शन प्रीमियमच्या किमान 10% SL
MAX_OPT_SL_PCT = 0.18              # ऑप्शन प्रीमियमच्या कमाल 18% SL
MAX_OPT_TRG_PCT = 0.28             # ऑप्शन प्रीमियमच्या कमाल 28% वास्तववादी Target
REALISTIC_RRR = 1.50               # 1 : 1.50 रिस्क-रिवॉर्ड

# Daily Capital Protection (1 Lot Risk Multiplier)
DAILY_GOAL_LOT_RISK_MULT = 1.35
DAILY_LOSS_LOT_RISK_MULT = -1.35
MAX_CONSECUTIVE_LOSSES = 2

# Technical, VIX & Greeks Filters
BASE_MIN_ADX_TREND = 18.0
MIN_BODY_RATIO = 0.40
MAX_ALLOWED_IV = 28.0
MIN_ABS_DELTA = 0.35
MIN_5M_OI_SHIFT = 50000

JOURNAL_DIR = Path("live_journal")
JOURNAL_DIR.mkdir(exist_ok=True)
TODAY_STR = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")
JOURNAL_CSV = JOURNAL_DIR / f"trades_journal_{TODAY_STR}.csv"


# ========================= TELEGRAM & ALERTS =========================
def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram Disconnected] {text[:90]}...", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[-] Telegram Error: {e}", flush=True)

def send_telegram_chart_alert(df_5m: pd.DataFrame, gann_level: float, sl_price: float, trg_price: float, caption_text: str = ""):
    if not HAS_MATPLOTLIB or df_5m.empty or len(df_5m) < 5:
        send_telegram(caption_text)
        return

    try:
        recent = df_5m.tail(25)
        plt.figure(figsize=(10, 5))
        plt.plot(recent['dt'], recent['close'], label='NIFTY 5M Spot', color='black', linewidth=1.5)
        plt.axhline(y=gann_level, color='blue', linestyle='--', label=f'Gann Pivot: {gann_level:.1f}')
        plt.axhline(y=sl_price, color='red', linestyle=':', label=f'Dynamic Spot SL: {sl_price:.1f}')
        plt.axhline(y=trg_price, color='purple', linestyle=':', label=f'Fuel-Capped Target: {trg_price:.1f}')
        plt.title("NIFTY 5M Dynamic % + Live India VIX + Fuel Setup", fontsize=12, fontweight='bold')
        plt.legend(loc='upper left', fontsize=9)
        plt.grid(True, linestyle=":", alpha=0.5)
        plt.tight_layout()
        
        chart_path = "live_gann_alert.png"
        plt.savefig(chart_path, dpi=120)
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


# ========================= GANN LEVELS =========================
def get_gann_levels(current_price: float, count: int = 5) -> dict:
    base_root = int(math.floor(math.sqrt(current_price)))
    lower_levels = [((base_root - i) ** 2) for i in range(count, 0, -1)]
    base_level = [base_root ** 2]
    upper_levels = [((base_root + i) ** 2) for i in range(1, count + 1)]
    all_levels = sorted(lower_levels + base_level + upper_levels)
    
    supports = [lvl for lvl in all_levels if lvl <= current_price]
    resistances = [lvl for lvl in all_levels if lvl > current_price]
    
    immediate_sup = max(supports) if supports else current_price * 0.995
    immediate_res = min(resistances) if resistances else current_price * 1.005
    next_res = [lvl for lvl in resistances if lvl > immediate_res]
    next_sup = [lvl for lvl in supports if lvl < immediate_sup]
    
    return {
        "all_levels": all_levels,
        "immediate_support": immediate_sup,
        "immediate_resistance": immediate_res,
        "target_ce": next_res[0] if next_res else immediate_res * 1.01,
        "target_pe": next_sup[-1] if next_sup else immediate_sup * 0.99
    }


# ========================= UPSTOX CLIENT =========================
class UpstoxClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Authorization": f"Bearer {token}"
        })

    def get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        url = BASE_URL + endpoint
        r = self.session.get(url, params=params, timeout=10)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def get_ltp(self, instrument_key: str) -> Optional[float]:
        try:
            data = self.get("/v3/market-quote/ltp", {"instrument_key": instrument_key})
            d = data.get("data", {})
            item = d.get(instrument_key) or (next(iter(d.values())) if d else {})
            val = item.get("last_price")
            return float(val) if val is not None else None
        except Exception:
            return None

    def get_live_india_vix(self) -> dict:
        """
        Upstox मधून थेट Live India VIX (NSE_INDEX|India VIX) फॅच करून Volatility Regime ठरवणे:
        - Calm (< 13.0): संथ बाजार -> कडक ADX (21.0)
        - Normal (13.0 - 17.5): आदर्श वातावरण -> ADX (18.0)
        - Elevated (17.5 - 22.0): वेगवान चाल -> ADX (18.0)
        - Panic (> 22.0): अत्यंत अस्थिर -> कडक IV Cap (26.0%)
        """
        vix_val = self.get_ltp("NSE_INDEX|India VIX")
        if vix_val is None or vix_val <= 0:
            return {
                "vix": None,
                "regime": "NORMAL (N/A)",
                "min_adx": BASE_MIN_ADX_TREND,
                "max_iv": MAX_ALLOWED_IV,
                "implied_daily_pct": 0.0085
            }

        # Rule of 16: VIX / sqrt(252) ≈ VIX / 15.87 = एका दिवसाची अपेक्षित हालचाल %
        implied_daily_pct = round((vix_val / 100.0) / 15.87, 5)

        if vix_val < 13.0:
            regime = "CALM (<13 · Slow)"
            min_adx = 21.0
            max_iv = MAX_ALLOWED_IV
        elif vix_val <= 17.5:
            regime = "NORMAL (13-17.5)"
            min_adx = BASE_MIN_ADX_TREND
            max_iv = MAX_ALLOWED_IV
        elif vix_val <= 22.0:
            regime = "ELEVATED (17.5-22)"
            min_adx = BASE_MIN_ADX_TREND
            max_iv = MAX_ALLOWED_IV
        else:
            regime = "PANIC (>22 · High Vol)"
            min_adx = 19.0
            max_iv = 26.0

        return {
            "vix": round(vix_val, 2),
            "regime": regime,
            "min_adx": min_adx,
            "max_iv": max_iv,
            "implied_daily_pct": implied_daily_pct
        }

    def get_daily_expected_range_pct(self) -> float:
        try:
            end_dt = now_ist().strftime("%Y-%m-%d")
            start_dt = (now_ist() - timedelta(days=30)).strftime("%Y-%m-%d")
            encoded_key = quote("NSE_INDEX|Nifty 50", safe="")
            url = f"/v2/historical-candle/{encoded_key}/day/{end_dt}/{start_dt}"
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
        except Exception as e:
            print(f"[-] Daily ATR% Fetch Fallback: {e}", flush=True)
        return 0.0085

    def get_candles_1m(self, instrument_key: str) -> pd.DataFrame:
        try:
            encoded_key = quote(instrument_key, safe="")
            url = f"/v2/historical-candle/intraday/{encoded_key}/1minute"
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

    def get_spot_candles_5m(self) -> pd.DataFrame:
        df_1m = self.get_candles_1m("NSE_INDEX|Nifty 50")
        if df_1m.empty:
            return pd.DataFrame()
        df_5m = df_1m.set_index("dt").resample("5min", origin="start_day").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum"
        }).dropna().reset_index()
        return df_5m

    def get_futures_5m_metrics(self, fut_instrument_key: str) -> dict:
        result = {
            "valid": False,
            "fut_close": None,
            "fut_vwap": None,
            "above_fut_vwap": None,
            "fut_5m_vol": 0,
            "fut_avg_5m_vol": 0,
            "fut_vol_surge": False,
            "fut_oi": 0,
            "fut_5m_oi_chg": 0,
            "fut_buildup_code": "NEUTRAL",
            "fut_buildup_label": "⚪ FUT NEUTRAL"
        }
        if not fut_instrument_key:
            return result

        try:
            df_1m = self.get_candles_1m(fut_instrument_key)
            if df_1m.empty or len(df_1m) < 10:
                return result

            tp_1m = (df_1m["high"] + df_1m["low"] + df_1m["close"]) / 3.0
            vol_1m = df_1m["volume"].astype(float)
            cum_vol = vol_1m.cumsum()
            df_1m["true_vwap"] = (tp_1m * vol_1m).cumsum() / cum_vol.replace(0, np.nan)

            df_5m = df_1m.set_index("dt").resample("5min", origin="start_day").agg({
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
                "oi": "last",
                "true_vwap": "last"
            }).dropna().reset_index()

            if len(df_5m) < 3:
                return result

            cur_bar = df_5m.iloc[-1]
            prev_bar = df_5m.iloc[-2]

            fut_close = float(cur_bar["close"])
            fut_open = float(cur_bar["open"])
            fut_vwap = float(cur_bar["true_vwap"]) if pd.notna(cur_bar["true_vwap"]) else fut_close

            cur_vol = int(cur_bar["volume"])
            avg_vol = int(df_5m["volume"].tail(12).mean())
            vol_surge = cur_vol >= (avg_vol * 1.15) if avg_vol > 0 else False

            cur_oi = int(cur_bar["oi"])
            prev_oi = int(prev_bar["oi"])
            oi_5m_chg = cur_oi - prev_oi

            price_up = fut_close > fut_open
            price_dn = fut_close < fut_open

            if price_up and oi_5m_chg > 0:
                b_code = "FUT_LONG_BUILDUP"
                b_label = "🟢 FUT LONG BUILD-UP (Price↑ OI↑)"
            elif price_dn and oi_5m_chg > 0:
                b_code = "FUT_SHORT_BUILDUP"
                b_label = "🔴 FUT SHORT BUILD-UP (Price↓ OI↑)"
            elif price_up and oi_5m_chg < 0:
                b_code = "FUT_SHORT_COVERING"
                b_label = "🚀 FUT SHORT COVERING (Price↑ OI↓)"
            elif price_dn and oi_5m_chg < 0:
                b_code = "FUT_LONG_UNWINDING"
                b_label = "🔻 FUT LONG UNWINDING (Price↓ OI↓)"
            else:
                b_code = "FUT_NEUTRAL"
                b_label = "⚪ FUT NEUTRAL"

            result.update({
                "valid": True,
                "fut_close": fut_close,
                "fut_vwap": round(fut_vwap, 2),
                "above_fut_vwap": fut_close >= fut_vwap,
                "fut_5m_vol": cur_vol,
                "fut_avg_5m_vol": avg_vol,
                "fut_vol_surge": vol_surge,
                "fut_oi": cur_oi,
                "fut_5m_oi_chg": oi_5m_chg,
                "fut_buildup_code": b_code,
                "fut_buildup_label": b_label
            })
        except Exception as e:
            print(f"[-] Futures 5M Metrics Error: {e}", flush=True)
        return result

    def get_futures_smartlist_signal(self, fut_instrument_key: str) -> dict:
        info = {"found": False, "oi_chg_pct": 0.0, "price_chg_pct": 0.0, "smartlist_tag": "N/A"}
        if not fut_instrument_key:
            return info
        try:
            res = self.get("/v2/market/smartlist/futures", {
                "asset_type": "INDEX",
                "category": "OI_GAINERS",
                "page_number": 1,
                "page_size": 10
            })
            for idx, item in enumerate(res.get("data", {}).get("smartlist", []), start=1):
                if item.get("instrument_key") == fut_instrument_key:
                    m = item.get("metric", {})
                    p = item.get("price", {})
                    oi_pct = float(m.get("change_pct", 0.0) or 0.0)
                    p_pct = float(p.get("change_pct", 0.0) or 0.0)
                    info.update({
                        "found": True,
                        "oi_chg_pct": oi_pct,
                        "price_chg_pct": p_pct,
                        "smartlist_tag": f"Rank #{idx} OI Gainer ({oi_pct:+.1f}%)"
                    })
                    break
        except Exception:
            pass
        return info

    def get_official_charges(self, instrument_key: str, qty: int, buy_price: float, sell_price: float) -> dict:
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

    def get_official_max_pain(self, expiry: str) -> Optional[float]:
        try:
            params = {
                "instrument_key": "NSE_INDEX|Nifty 50",
                "expiry": expiry,
                "date": TODAY_STR,
                "bucket_interval": 15
            }
            res = self.get("/v2/market/max-pain", params=params)
            val = res.get("data", {}).get("max_pain")
            return float(val) if val is not None else None
        except Exception:
            return None

    def get_live_option_chain_memory(self, expiry: str, target_strike: float, spot_price: float) -> dict:
        strike_band = round(spot_price * STRIKE_SCAN_RANGE_PCT)
        memory = {
            "atm_strike": target_strike,
            "strike_range": f"{int(target_strike - strike_band)}-{int(target_strike + strike_band)}",
            "total_call_oi": 0,
            "total_put_oi": 0,
            "total_call_chg": 0,
            "total_put_chg": 0,
            "delta_oi_pcr": None,
            "ce": {"ltp": None, "delta": 0.52, "iv": 0.0, "theta": 0.0, "gamma": 0.0, "oi": 0, "chg_oi": 0},
            "pe": {"ltp": None, "delta": -0.52, "iv": 0.0, "theta": 0.0, "gamma": 0.0, "oi": 0, "chg_oi": 0}
        }
        try:
            res_chain = self.get("/v2/option/chain", {
                "instrument_key": "NSE_INDEX|Nifty 50",
                "expiry_date": expiry
            })
            chain_list = res_chain.get("data", [])
            
            tot_c_oi = 0
            tot_p_oi = 0
            tot_c_chg = 0
            tot_p_chg = 0
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
                tot_c_chg = 0
                tot_p_chg = 0
                res_chg = self.get("/v2/market/change-oi", {
                    "instrument_key": "NSE_INDEX|Nifty 50",
                    "expiry": expiry,
                    "date": TODAY_STR,
                    "interval": 1
                })
                for item in res_chg.get("data", {}).get("call_put_oi_data_list", []):
                    s_price = float(item.get("strike_price", 0))
                    if abs(s_price - target_strike) <= strike_band:
                        c_chg = int(item.get("call_change_oi", 0))
                        p_chg = int(item.get("put_change_oi", 0))
                        tot_c_chg += c_chg
                        tot_p_chg += p_chg
                    if s_price == target_strike:
                        memory["ce"]["chg_oi"] = int(item.get("call_change_oi", 0))
                        memory["pe"]["chg_oi"] = int(item.get("put_change_oi", 0))

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
        except Exception as e:
            print(f"[-] Live Memory Fetch Error: {e}", flush=True)
        return memory


def get_live_market_session_timings(api: UpstoxClient, target_date: str) -> dict:
    return {
        "session_start": "09:20",
        "entry_end": "15:00",
        "force_exit": "15:20",
        "market_end": "15:30",
        "raw_start": "09:15",
        "raw_end": "15:30"
    }


def add_indicators_5m(df_5m: pd.DataFrame) -> pd.DataFrame:
    x = df_5m.copy()
    if x.empty:
        return x
    
    c = x["close"]
    prev_close = c.shift(1)
    
    tp = (x["high"] + x["low"] + c) / 3.0
    v = x["volume"].replace(0, 1.0)
    x["vwap"] = (tp * v).cumsum() / v.cumsum()
    
    tr = pd.concat([
        x["high"] - x["low"],
        (x["high"] - prev_close).abs(),
        (x["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    x["atr14"] = tr.rolling(14, min_periods=5).mean()
    
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
    return x

def classify_5m_oi_buildup(is_green: bool, is_red: bool, shift_put_oi: int, shift_call_oi: int) -> Tuple[str, str]:
    if is_green:
        if shift_call_oi <= -MIN_5M_OI_SHIFT:
            return "SHORT_COVERING_SQUEEZE", "🚀 SHORT COVERING SQUEEZE (Call Writers Exit)"
        if shift_put_oi >= MIN_5M_OI_SHIFT and shift_call_oi <= MIN_5M_OI_SHIFT:
            return "FRESH_PUT_WRITING", "🟢 FRESH PUT WRITING (Bullish Support)"
        if shift_call_oi >= MIN_5M_OI_SHIFT and shift_put_oi <= -MIN_5M_OI_SHIFT:
            return "BULL_TRAP", "⚠️ BULL TRAP (Call Writing on Rise)"
    elif is_red:
        if shift_put_oi <= -MIN_5M_OI_SHIFT:
            return "PUT_UNWINDING_SQUEEZE", "🔻 PUT/LONG UNWINDING (Put Writers Exit)"
        if shift_call_oi >= MIN_5M_OI_SHIFT and shift_put_oi <= MIN_5M_OI_SHIFT:
            return "FRESH_CALL_WRITING", "🔴 FRESH CALL WRITING (Bearish Resistance)"
        if shift_put_oi >= MIN_5M_OI_SHIFT and shift_call_oi <= -MIN_5M_OI_SHIFT:
            return "BEAR_TRAP", "⚠️ BEAR TRAP (Put Writing on Fall)"

    return "NEUTRAL_OI_SHIFT", "⚪ NEUTRAL 5M OI SHIFT"


def is_market_choppy(df_5m: pd.DataFrame, delta_pcr: float, buildup_code: str, dynamic_min_adx: float) -> Tuple[bool, str]:
    if len(df_5m) < 10:
        return True, "INSUFFICIENT_DATA"

    last_bar = df_5m.iloc[-1]
    adx = safe_float(last_bar.get("adx14"))
    c_range = max(last_bar["high"] - last_bar["low"], 1e-9)
    c_body = abs(last_bar["close"] - last_bar["open"])
    is_strong_bar = (c_body / c_range) >= 0.50

    if buildup_code in ("BULL_TRAP", "BEAR_TRAP"):
        return True, f"OI_TRAP_DETECTED ({buildup_code})"

    if adx is not None and adx < dynamic_min_adx and not is_strong_bar:
        return True, f"CHOPPY_REGIME_ADX_LOW ({adx:.1f} < {dynamic_min_adx})"

    is_squeeze = buildup_code in ("SHORT_COVERING_SQUEEZE", "PUT_UNWINDING_SQUEEZE")
    if 0.85 <= delta_pcr <= 1.15 and not is_strong_bar and not is_squeeze:
        return True, f"NEUTRAL_DEAD_ZONE_DELTA_PCR (±2% ΔOI:{delta_pcr:.2f})"

    return False, "TRENDING_CLEAR"


# ========================= LIVE TRADING ENGINE =========================
class LivePaperTrader:
    def __init__(self, api: UpstoxClient, timings: dict):
        self.api = api
        self.timings = timings
        self.active_position = None
        self.trade_counter = 0

        # Dynamic Lot Size, Strike Step & Daily Fuel Capacity
        self.lot_size = 65
        self.strike_step = 50.0
        self.daily_expected_range_pct = self.api.get_daily_expected_range_pct()
        self.daily_profit_goal = 1500.0
        self.daily_max_loss = -1500.0

        self.live_memory_register = {}
        self.futures_memory_register = {}
        self.vix_register = {}
        self.prev_5m_oi_snapshot = None
        self.last_evaluated_5m_bar = None
        self.current_5m_buildup_code = "NEUTRAL_OI_SHIFT"
        self.current_5m_buildup_label = "⚪ NEUTRAL 5M OI SHIFT"
        self.shift_5m_put_oi = 0
        self.shift_5m_call_oi = 0

        self.cumulative_pnl = 0.0
        self.daily_locked = False
        self.consecutive_losses = 0
        self.cooldown_until = None
        self.pending_gann_setup = None
        self.last_sent_5m_bar_time = None

        self.expiry = None
        self.fut_instrument_key = ""
        self.fut_symbol = ""
        self.contracts_cache = {}
        self.failed_strikes = {}
        self.load_expiry_and_contracts()
        print(f"[*] Scale-Invariant + India VIX Engine Ready | LotSize: {self.lot_size} | StrikeStep: {self.strike_step}", flush=True)

    def load_expiry_and_contracts(self):
        try:
            url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
            df = pd.read_csv(url, compression="gzip", low_memory=False)
            df.columns = [c.strip().lower() for c in df.columns]
            sym_col = "tradingsymbol" if "tradingsymbol" in df.columns else "trading_symbol"

            nifty_futs = df[(df["exchange"] == "NSE_FO") & (df["name"] == "NIFTY") & (df["instrument_type"] == "FUTIDX")].copy()
            nifty_futs["expiry"] = pd.to_datetime(nifty_futs["expiry"]).dt.strftime("%Y-%m-%d")
            valid_futs = nifty_futs[nifty_futs["expiry"] >= TODAY_STR].sort_values("expiry")
            if not valid_futs.empty:
                top_fut = valid_futs.iloc[0]
                self.fut_instrument_key = str(top_fut.get("instrument_key", "")).strip()
                self.fut_symbol = str(top_fut.get(sym_col, "")).strip()
                if "lot_size" in top_fut and pd.notna(top_fut["lot_size"]):
                    self.lot_size = int(float(top_fut["lot_size"]))
                print(f"[+] चालू NIFTY Futures मॅप झाले: {self.fut_symbol} | Dynamic Lot Size: {self.lot_size}", flush=True)

            nifty_opts = df[(df["exchange"] == "NSE_FO") & (df["name"] == "NIFTY") & (df["instrument_type"] == "OPTIDX")].copy()
            nifty_opts["expiry"] = pd.to_datetime(nifty_opts["expiry"]).dt.strftime("%Y-%m-%d")
            valid = sorted([x for x in nifty_opts["expiry"].unique() if x >= TODAY_STR])
            self.expiry = valid[0]

            active_df = nifty_opts[nifty_opts["expiry"] == self.expiry].copy()
            if "lot_size" in active_df.columns and not active_df["lot_size"].dropna().empty:
                self.lot_size = int(float(active_df["lot_size"].dropna().iloc[0]))

            strikes_sorted = sorted(active_df["strike"].dropna().unique())
            if len(strikes_sorted) >= 2:
                diffs = np.diff(strikes_sorted)
                min_step = float(np.min(diffs[diffs > 0]))
                if min_step > 0:
                    self.strike_step = min_step

            for _, r in active_df.iterrows():
                strike = float(r.get("strike", 0))
                opt_type = str(r.get("option_type", "")).strip().upper()
                ikey = str(r.get("instrument_key", "")).strip()
                sym = str(r.get(sym_col, "")).strip()
                if strike and opt_type and ikey:
                    self.contracts_cache[(strike, opt_type)] = {
                        "trading_symbol": sym,
                        "instrument_key": ikey
                    }
            print(f"[+] {len(self.contracts_cache)} ऑप्शन कॉन्ट्रॅक्ट्स मॅप झाले (Expiry: {self.expiry} | Lot: {self.lot_size} | Step: {self.strike_step}).", flush=True)
        except Exception as e:
            print(f"[-] Contracts loading error: {e}", flush=True)
            self.expiry = TODAY_STR

    def calculate_remaining_daily_fuel(self, df_5m: pd.DataFrame, spot_close: float, vix_info: dict) -> dict:
        today_high = float(df_5m["high"].max())
        today_low = float(df_5m["low"].min())
        used_pts = max(0.0, today_high - today_low)

        # 14-Day ATR% आणि Live India VIX Implied Daily% यांचा समतोल घेणे
        blended_daily_pct = (self.daily_expected_range_pct + vix_info.get("implied_daily_pct", self.daily_expected_range_pct)) / 2.0
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

    def update_dynamic_daily_limits(self, spot_close: float):
        one_trade_std_risk_rupees = (spot_close * 0.0012 * 0.52) * self.lot_size
        self.daily_profit_goal = round(max(1000.0, one_trade_std_risk_rupees * DAILY_GOAL_LOT_RISK_MULT), 0)
        self.daily_max_loss = round(min(-1000.0, one_trade_std_risk_rupees * DAILY_LOSS_LOT_RISK_MULT), 0)

    def update_5m_oi_shift_register(self, bar_time_str: str, is_green: bool, is_red: bool, mem: dict):
        if self.last_evaluated_5m_bar == bar_time_str:
            return

        cur_put_chg = int(mem.get("total_put_chg", 0))
        cur_call_chg = int(mem.get("total_call_chg", 0))

        if self.prev_5m_oi_snapshot is not None:
            self.shift_5m_put_oi = cur_put_chg - self.prev_5m_oi_snapshot["put_chg"]
            self.shift_5m_call_oi = cur_call_chg - self.prev_5m_oi_snapshot["call_chg"]
            self.current_5m_buildup_code, self.current_5m_buildup_label = classify_5m_oi_buildup(
                is_green, is_red, self.shift_5m_put_oi, self.shift_5m_call_oi
            )
        else:
            self.shift_5m_put_oi = 0
            self.shift_5m_call_oi = 0
            self.current_5m_buildup_code = "INITIAL_5M_SYNC"
            self.current_5m_buildup_label = "⏳ INITIAL 5M SNAPSHOT SYNCED"

        self.prev_5m_oi_snapshot = {
            "bar_time": bar_time_str,
            "put_chg": cur_put_chg,
            "call_chg": cur_call_chg
        }
        self.last_evaluated_5m_bar = bar_time_str

    def log_journal_csv(self, row: dict):
        exists = JOURNAL_CSV.exists()
        with open(JOURNAL_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            if not exists:
                writer.writeheader()
            writer.writerow(row)

    def send_5m_sentiment_pulse(self, df_5m: pd.DataFrame, delta_pcr: float, max_pain: float, mem: dict, fut_mem: dict, smart_info: dict, fuel_info: dict, vix_info: dict):
        if df_5m.empty:
            return

        last_bar = df_5m.iloc[-1]
        bar_time_str = last_bar["dt"].strftime("%H:%M")
        
        if self.last_sent_5m_bar_time == bar_time_str:
            return
        self.last_sent_5m_bar_time = bar_time_str

        spot = float(last_bar["close"])
        spot_vwap = float(last_bar.get("vwap", spot))
        rsi = float(last_bar.get("rsi14", 50.0))
        adx = float(last_bar.get("adx14", 0.0))
        atr = float(last_bar.get("atr14", 0.0))

        vix_val = vix_info.get("vix")
        vix_str = f"{vix_val:.2f} ({vix_info.get('regime')})" if vix_val else "N/A"

        fut_valid = fut_mem.get("valid", False)
        fut_px = fut_mem.get("fut_close") or spot
        fut_vwap = fut_mem.get("fut_vwap") or spot_vwap
        fut_basis = round(fut_px - spot, 1) if fut_valid else 0.0
        fut_vol = fut_mem.get("fut_5m_vol", 0)
        fut_oi_chg = fut_mem.get("fut_5m_oi_chg", 0)
        fut_label = fut_mem.get("fut_buildup_label", "⚪ FUT N/A")
        smart_tag = smart_info.get("smartlist_tag", "N/A")

        s_range = mem.get("strike_range", "")
        ce_g = mem.get("ce", {})
        pe_g = mem.get("pe", {})

        is_bullish = ((delta_pcr > 1.15) or (self.current_5m_buildup_code == "SHORT_COVERING_SQUEEZE")) and (spot > spot_vwap)
        is_bearish = ((delta_pcr < 0.85) or (self.current_5m_buildup_code == "PUT_UNWINDING_SQUEEZE")) and (spot < spot_vwap)

        gann = get_gann_levels(spot, count=3)
        sup = gann["immediate_support"]
        res = gann["immediate_resistance"]

        if fuel_info["remaining_pct"] < MIN_REMAINING_FUEL_PCT:
            total_sentiment = "⛽ DAILY FUEL EXHAUSTED (Range Done)"
            focus_action = "🛑 NO NEW TRADES (Insufficient Daily Fuel Left)"
        elif self.current_5m_buildup_code in ("BULL_TRAP", "BEAR_TRAP"):
            total_sentiment = f"{self.current_5m_buildup_label}"
            focus_action = "🚫 TRAP BLOCKED (Wait for Clean Confirmation)"
        elif adx < vix_info.get("min_adx", BASE_MIN_ADX_TREND) and (0.85 <= delta_pcr <= 1.15) and self.current_5m_buildup_code not in ("SHORT_COVERING_SQUEEZE", "PUT_UNWINDING_SQUEEZE"):
            total_sentiment = "⚪ CHOPPY / RANGEBOUND 💤"
            focus_action = "🚫 NO TRADE (Wait for Breakout / Capital Guard)"
        elif is_bullish:
            total_sentiment = "🟢 VERY BULLISH (+VE) 🚀"
            focus_action = "🟢 FOCUS ON CE SIDE (Spot+Fut VWAP + VIX Regime)"
        elif is_bearish:
            total_sentiment = "🔴 VERY BEARISH (-VE) 🔻"
            focus_action = "🔴 FOCUS ON PE SIDE (Spot+Fut VWAP + VIX Regime)"
        else:
            total_sentiment = "🟡 MIXED BIAS ⏳"
            focus_action = "⏳ WAIT & WATCH (Selective Levels Only)"

        fut_vwap_status = "Above Fut-VWAP 🟢" if fut_px >= fut_vwap else "Below Fut-VWAP 🔴"

        msg = (
            f"📊 <b>NIFTY 5M DYNAMIC PULSE ({bar_time_str} IST)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 <b>Spot:</b> <code>{spot:.2f}</code> | <b>Gann:</b> <code>{sup:.1f}</code> ⬅️ ➡️ <code>{res:.1f}</code>\n"
            f"🌡️ <b>India VIX:</b> <code>{vix_str}</code>\n"
            f"⛽ <b>Remaining Daily Fuel:</b> <code>{fuel_info['remaining_pts']:.1f} pts</code> ({fuel_info['fuel_left_pct_of_day']}% left of {fuel_info['expected_pts']:.0f} pts)\n"
            f"📈 <b>Futures ({self.fut_symbol}):</b> <code>{fut_px:.2f}</code> (Basis: <code>{fut_basis:+.1f}</code>)\n"
            f"• <b>True Fut VWAP:</b> <code>{fut_vwap:.2f}</code> ({fut_vwap_status})\n"
            f"• <b>Fut 5M Vol:</b> <code>{fut_vol:,}</code> | <b>5M OI Chg:</b> <code>{fut_oi_chg:+,}</code>\n"
            f"• <b>Fut Build-up:</b> <b>{fut_label}</b> | <b>Smartlist:</b> <code>{smart_tag}</code>\n\n"
            f"🔹 <b>SMC ±2% STRIKES ΔOI & 5M SHIFT ({s_range}):</b>\n"
            f"• <b>Intraday ΔOI PCR:</b> <code>{delta_pcr:.2f}</code> | <b>Max Pain:</b> <code>{max_pain}</code>\n"
            f"• <b>Last 5M Shift:</b> <code>PE {self.shift_5m_put_oi:+,}</code> | <code>CE {self.shift_5m_call_oi:+,}</code>\n"
            f"• <b>Opt 5M Build-up:</b> <b>{self.current_5m_buildup_label}</b>\n\n"
            f"🔹 <b>LIVE ATM GREEKS ({int(mem.get('atm_strike', 0))}):</b>\n"
            f"• <b>CE:</b> Δ <code>{ce_g.get('delta', 0):.2f}</code> | IV <code>{ce_g.get('iv', 0):.1f}%</code> | θ <code>{ce_g.get('theta', 0):.1f}</code>\n"
            f"• <b>PE:</b> Δ <code>{pe_g.get('delta', 0):.2f}</code> | IV <code>{pe_g.get('iv', 0):.1f}%</code> | θ <code>{pe_g.get('theta', 0):.1f}</code>\n\n"
            f"🔹 <b>MOMENTUM:</b> RSI <code>{rsi:.1f}</code> | ADX <code>{adx:.1f}</code> | ATR <code>{atr:.1f}</code> | Lot: <code>{self.lot_size}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏁 <b>TOTAL SENTIMENT:</b> <b>{total_sentiment}</b>\n"
            f"🎯 <b>STRATEGY FOCUS:</b> <b>{focus_action}</b>"
        )
        send_telegram(msg)
        print(f"[{bar_time_str}] Pulse: {total_sentiment} | VIX:{vix_val} | FuelLeft:{fuel_info['remaining_pts']}pts", flush=True)

    def send_entry_alert(self, pos: dict, df_5m: pd.DataFrame):
        caption = (
            f"🚀 <b>NIFTY 5M DYNAMIC % ALGO: नवीन एन्ट्री (#{pos['trade_id']})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>ऑर्डर:</b> <b>BUY {pos['clean_name']}</b>\n"
            f"🏷️ <b>सिम्बॉल:</b> <code>{pos['symbol']}</code>\n"
            f"📦 <b>लॉट:</b> <code>1 Lot</code> (Dynamic Qty: <code>{pos['lot_size']}</code>)\n"
            f"💵 <b>खरेदी किंमत:</b> <code>₹{pos['entry_px']:.2f}</code>\n"
            f"🛑 <b>स्टॉपलॉस:</b> <code>₹{pos['sl_px']:.2f}</code> (-{pos['opt_sl_pts']:.1f} pts / -{pos['opt_sl_pct']:.1f}% | Spot SL: {pos['spot_sl']})\n"
            f"🏁 <b>टार्गेट:</b> <code>₹{pos['trg_px']:.2f}</code> (+{pos['opt_trg_pts']:.1f} pts / +{pos['opt_trg_pct']:.1f}% | Spot Trg: {pos['spot_target']})\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🌡️ <b>India VIX:</b> <code>{pos['india_vix']}</code> | ⛽ <b>Fuel Left:</b> <code>{pos['rem_fuel_pts']:.1f} pts</code>\n"
            f"🧬 <b>Live Option Greeks & Dual Build-up:</b>\n"
            f"• <b>Delta (Δ):</b> <code>{pos['entry_delta']:.2f}</code> | <b>IV:</b> <code>{pos['entry_iv']:.1f}%</code> | <b>Theta:</b> <code>{pos['entry_theta']:.1f}</code>\n"
            f"• <b>Opt Build-up:</b> <code>{pos['oi_buildup']}</code> | <b>Fut:</b> <code>{pos['fut_buildup']}</code>\n"
            f"📍 <b>NIFTY Spot:</b> <code>{pos['spot_px']:.2f}</code> (Gann Level: {pos['gann_level']:.1f})\n"
            f"📊 <b>±2% ΔOI PCR:</b> <code>{pos['delta_pcr']:.2f}</code> | <b>Max Pain:</b> <code>{pos['max_pain']}</code>\n"
            f"⚡ <b>सेटअप:</b> <code>{pos['setup_type']}</code>\n"
            f"⏱️ <b>वेळ:</b> <code>{pos['entry_time']}</code>"
        )
        send_telegram_chart_alert(df_5m, pos["gann_level"], pos["spot_sl"], pos["spot_target"], caption)

    def send_periodic_update(self, pos: dict, current_ltp: float):
        cur_pts = round(current_ltp - pos["entry_px"], 2)
        cur_pnl = round(cur_pts * pos["lot_size"], 2)
        pnl_symbol = "🟢" if cur_pnl >= 0 else "🔴"

        msg = (
            f"⏱️ <b>ट्रेड #{pos['trade_id']} चालू अपडेट (3-Min Ping)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷️ <b>{pos['clean_name']} @ ₹{current_ltp:.2f}</b> (Qty: {pos['lot_size']})\n"
            f"💵 खरेदी: <code>₹{pos['entry_px']:.2f}</code> | SL: <code>₹{pos['sl_px']:.2f}</code>\n"
            f"📊 चालू पॉईंट्स: <code>{cur_pts:+.2f} pts</code>\n"
            f"{pnl_symbol} <b>चालू PnL:</b> <b>₹ {cur_pnl:+,.2f}</b>"
        )
        send_telegram(msg)

    def send_exit_alert(self, pos: dict):
        status_emoji = "🟢 नफा" if pos["net_pnl"] > 0 else "🔴 तोटा"
        msg = (
            f"{'🎉' if pos['net_pnl'] > 0 else '⚠️'} <b>NIFTY ALGO: ट्रेड पूर्ण ({status_emoji})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>ट्रेड #{pos['trade_id']}: BUY {pos['clean_name']}</b> (Qty: {pos['lot_size']})\n"
            f"⏱️ <b>कालावधी:</b> <code>{pos['entry_time']} ➡️ {pos['exit_time']}</code>\n"
            f"💵 <b>एन्ट्री:</b> <code>₹{pos['entry_px']:.2f}</code> ➡️ <b>एक्झिट:</b> <code>₹{pos['exit_px']:.2f}</code>\n"
            f"📊 <b>पॉईंट्स:</b> <code>{pos['pts']:+.2f} pts</code>\n"
            f"🛑 <b>कारण:</b> <code>{pos['exit_reason']}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>या ट्रेडचा निव्वळ निकाल:</b> <b>₹ {pos['net_pnl']:+,.2f}</b>\n"
            f"📈 <b>एकूण संचित नफा:</b> <b>₹ {self.cumulative_pnl:+,.2f}</b>"
        )
        send_telegram(msg)

    def execute_entry(self, side: str, strike: float, ltp: float, instrument_key: str, symbol: str,
                      spot_px: float, gann_level: float, trigger_level: float, raw_spot_sl: float, gann_barrier: float,
                      atr_5m: float, fuel_info: dict, vix_info: dict, delta_pcr: float, max_pain: float, greeks: dict,
                      oi_buildup: str, fut_mem: dict, setup_type: str, df_5m: pd.DataFrame):
        entry_price = ltp
        live_abs_delta = max(0.35, min(0.75, abs(float(greeks.get("delta", 0.52)))))

        min_spot_sl_pts = spot_px * MIN_SPOT_SL_PCT
        max_spot_sl_pts = spot_px * MAX_SPOT_SL_PCT
        raw_spot_risk = abs(spot_px - raw_spot_sl)
        capped_spot_risk = round(min(max_spot_sl_pts, max(min_spot_sl_pts, raw_spot_risk)), 2)

        if side == "CE":
            realistic_spot_sl = round(spot_px - capped_spot_risk, 2)
        else:
            realistic_spot_sl = round(spot_px + capped_spot_risk, 2)

        base_spot_reward = capped_spot_risk * REALISTIC_RRR
        atr_reach_cap = (atr_5m * 3.5) if (atr_5m and atr_5m > 0) else base_spot_reward
        fuel_reach_cap = max(min_spot_sl_pts * 1.2, fuel_info["remaining_pts"] * 0.70)
        gann_dist = abs(gann_barrier - spot_px)

        candidate_rewards = [base_spot_reward, atr_reach_cap, fuel_reach_cap]
        if gann_dist >= min_spot_sl_pts * 1.5:
            candidate_rewards.append(max(min_spot_sl_pts * 1.2, gann_dist * 0.92))

        realistic_spot_reward = round(max(min_spot_sl_pts * 1.2, min(candidate_rewards)), 2)

        if side == "CE":
            realistic_spot_target = round(spot_px + realistic_spot_reward, 2)
        else:
            realistic_spot_target = round(spot_px - realistic_spot_reward, 2)

        delta_opt_sl = capped_spot_risk * live_abs_delta
        min_opt_sl = entry_price * MIN_OPT_SL_PCT
        max_opt_sl = entry_price * MAX_OPT_SL_PCT
        opt_sl_pts = round(min(max_opt_sl, max(min_opt_sl, delta_opt_sl)), 2)

        delta_opt_trg = realistic_spot_reward * live_abs_delta
        max_opt_trg = entry_price * MAX_OPT_TRG_PCT
        opt_trg_pts = round(min(max_opt_trg, max(opt_sl_pts * 1.35, delta_opt_trg)), 2)

        opt_sl_pct = round((opt_sl_pts / entry_price) * 100.0, 1)
        opt_trg_pct = round((opt_trg_pts / entry_price) * 100.0, 1)

        sl = max(0.05, round(entry_price - opt_sl_pts, 2))
        trg = round(entry_price + opt_trg_pts, 2)
        self.trade_counter += 1

        clean_name = f"{int(strike)} {side}"
        vix_val = vix_info.get("vix")

        self.active_position = {
            "trade_id": self.trade_counter,
            "side": f"BUY_{side}",
            "clean_name": clean_name,
            "symbol": symbol,
            "instrument_key": instrument_key,
            "strike": strike,
            "lot_size": self.lot_size,
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
            "rem_fuel_pts": fuel_info["remaining_pts"],
            "india_vix": f"{vix_val:.2f}" if vix_val else "N/A",
            "pcr": delta_pcr,
            "delta_pcr": delta_pcr,
            "max_pain": max_pain if max_pain else "N/A",
            "entry_delta": round(float(greeks.get("delta", 0.52)), 3),
            "entry_iv": round(float(greeks.get("iv", 0.0)), 2),
            "entry_theta": round(float(greeks.get("theta", 0.0)), 2),
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

        print(f"\n[एन्ट्री ट्रिगर] {clean_name} (Lot:{self.lot_size}) @ ₹{entry_price} | VIX:{vix_val} | SL: ₹{sl} (-{opt_sl_pct}%) | TRG: ₹{trg} (+{opt_trg_pct}%)", flush=True)
        self.send_entry_alert(self.active_position, df_5m)

    def execute_exit(self, exit_price: float, reason: str):
        pos = self.active_position
        qty = pos["lot_size"]
        pts = round(exit_price - pos["entry_px"], 2)
        gross_pnl = round(pts * qty, 2)

        chg_info = self.api.get_official_charges(pos["instrument_key"], qty, pos["entry_px"], exit_price)
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

        print(f"\n[एक्झिट ट्रिगर] {pos['clean_name']} Exit @ ₹{exit_price} | Net: ₹{net_pnl} ({reason})", flush=True)
        self.send_exit_alert(pos)
        self.log_journal_csv(pos)

        if net_pnl < 0:
            self.consecutive_losses += 1
            self.failed_strikes[pos["strike"]] = time.monotonic() + 2700
        else:
            self.consecutive_losses = 0

        if self.cumulative_pnl >= self.daily_profit_goal:
            self.daily_locked = True
            send_telegram(f"🔒 <b>दैनिक उद्दिष्ट पूर्ण!</b>\nदिवसाचा नफा (₹{self.cumulative_pnl:,.2f} / Goal ₹{self.daily_profit_goal:,.0f}) लॉक केला आहे.")
        elif self.cumulative_pnl <= self.daily_max_loss:
            self.daily_locked = True
            send_telegram(f"🛑 <b>मॅक्स लॉस मर्यादा आली!</b>\nदैनिक तोटा मर्यादा (₹{self.daily_max_loss:,.0f}) ओलांडली. सिस्टीम सुरक्षित बंद केली आहे.")
        elif self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            self.daily_locked = True
            send_telegram(f"🔒 <b>कॅपिटल प्रोटेक्शन लॉक:</b>\nसलग २ स्टॉपलॉस हिट झाल्याने ट्रेड्स थांबवले आहेत.")

        self.active_position = None
        self.pending_gann_setup = None
        self.cooldown_until = time.monotonic() + 600

    def check_position_management(self):
        pos = self.active_position
        if not pos:
            return

        ltp = self.api.get_ltp(pos["instrument_key"])
        if ltp is None:
            return

        pos["peak_price"] = max(pos["peak_price"], ltp)
        gain = ltp - pos["entry_px"]

        if gain >= pos["opt_sl_pts"] and not pos["runner_mode"]:
            pos["runner_mode"] = True
            pos["sl_px"] = max(pos["sl_px"], pos["entry_px"] + 0.15 * pos["opt_sl_pts"])
            send_telegram(f"🛡️ <b>Runner Mode ON: SL ट्रेल केला (₹{pos['sl_px']:.2f})</b> | {pos['clean_name']}")

        if gain >= (pos["opt_sl_pts"] * 1.5):
            new_sl = round(ltp - (pos["opt_sl_pts"] * 0.5), 2)
            if new_sl > pos["sl_px"]:
                pos["sl_px"] = new_sl

        if time.monotonic() - pos["last_periodic_update"] >= POSITION_UPDATE_INTERVAL:
            self.send_periodic_update(pos, ltp)
            pos["last_periodic_update"] = time.monotonic()

        hhmm = now_ist().strftime("%H:%M")
        if ltp <= pos["sl_px"]:
            reason = "TRAILED_EXIT ✅" if pos["sl_px"] >= pos["entry_px"] else "OPTION_SL_HIT ❌"
            self.execute_exit(pos["sl_px"], reason)
        elif ltp >= pos["trg_px"]:
            self.execute_exit(pos["trg_px"], "TARGET_HIT 🎯")
        elif hhmm >= self.timings["force_exit"]:
            self.execute_exit(ltp, "FORCE_EXIT_SESSION ⏱️")

    def run_strategy_scan(self):
        if self.active_position or self.daily_locked:
            return
        if self.cooldown_until and time.monotonic() < self.cooldown_until:
            return

        hhmm = now_ist().strftime("%H:%M")
        if not (self.timings["session_start"] <= hhmm <= self.timings["entry_end"]):
            return

        df_5m = self.api.get_spot_candles_5m()
        if len(df_5m) < 10:
            return

        df_5m = add_indicators_5m(df_5m)
        completed_bar = df_5m.iloc[-1]
        prev_bar = df_5m.iloc[-2]
        bar_time_str = completed_bar["dt"].strftime("%H:%M")

        spot_close = float(completed_bar["close"])
        spot_open = float(completed_bar["open"])
        spot_high = float(completed_bar["high"])
        spot_low = float(completed_bar["low"])
        spot_vwap_5m = float(completed_bar["vwap"])
        adx_val = float(completed_bar.get("adx14", 0.0))
        atr_val = float(completed_bar.get("atr14", spot_close * 0.0006))

        # 1. Live India VIX Regime + Dynamic Daily Limits + Remaining Fuel
        self.vix_register = self.api.get_live_india_vix()
        self.update_dynamic_daily_limits(spot_close)
        fuel_info = self.calculate_remaining_daily_fuel(df_5m, spot_close, self.vix_register)

        is_green = spot_close > spot_open
        is_red = spot_close < spot_open

        max_pain = self.api.get_official_max_pain(self.expiry)

        atm_strike = round(spot_close / self.strike_step) * self.strike_step
        self.live_memory_register = self.api.get_live_option_chain_memory(self.expiry, atm_strike, spot_close)
        delta_pcr = self.live_memory_register.get("delta_oi_pcr")

        self.futures_memory_register = self.api.get_futures_5m_metrics(self.fut_instrument_key)
        smartlist_info = self.api.get_futures_smartlist_signal(self.fut_instrument_key)

        if max_pain is None or delta_pcr is None:
            return

        self.update_5m_oi_shift_register(bar_time_str, is_green, is_red, self.live_memory_register)
        self.send_5m_sentiment_pulse(df_5m, delta_pcr, max_pain, self.live_memory_register, self.futures_memory_register, smartlist_info, fuel_info, self.vix_register)

        if fuel_info["remaining_pct"] < MIN_REMAINING_FUEL_PCT:
            if self.pending_gann_setup:
                self.pending_gann_setup = None
            return

        dynamic_min_adx = self.vix_register.get("min_adx", BASE_MIN_ADX_TREND)
        choppy, chop_reason = is_market_choppy(df_5m, delta_pcr, self.current_5m_buildup_code, dynamic_min_adx)
        if choppy:
            if self.pending_gann_setup:
                self.pending_gann_setup = None
            return

        gann = get_gann_levels(spot_close, count=5)
        sup_level = gann["immediate_support"]
        res_level = gann["immediate_resistance"]

        c_range = max(spot_high - spot_low, 1e-9)
        c_body = abs(spot_close - spot_open)
        body_ratio = c_body / c_range

        buffer_pts = round(spot_close * ENTRY_BUFFER_PCT, 2)
        gann_prox_pts = spot_close * GANN_PROXIMITY_PCT
        gann_retest_pts = spot_close * GANN_RETEST_BAND_PCT
        swing_pad_pts = round(spot_close * 0.0002, 2)

        prior_6 = df_5m.iloc[-7:-1]
        consolidation_high = float(prior_6["high"].max())
        consolidation_low = float(prior_6["low"].min())

        is_short_covering = (self.current_5m_buildup_code == "SHORT_COVERING_SQUEEZE")
        is_put_unwinding = (self.current_5m_buildup_code == "PUT_UNWINDING_SQUEEZE")

        fut_valid = self.futures_memory_register.get("valid", False)
        above_fut_vwap = self.futures_memory_register.get("above_fut_vwap", True) if fut_valid else True
        below_fut_vwap = (not self.futures_memory_register.get("above_fut_vwap", False)) if fut_valid else True

        bullish_sentiment = ((delta_pcr > 1.15) or is_short_covering) and (spot_close > spot_vwap_5m) and above_fut_vwap
        bearish_sentiment = ((delta_pcr < 0.85) or is_put_unwinding) and (spot_close < spot_vwap_5m) and below_fut_vwap

        if self.pending_gann_setup:
            if self.pending_gann_setup["side"] == "CE" and not bullish_sentiment:
                self.pending_gann_setup = None
            elif self.pending_gann_setup["side"] == "PE" and not bearish_sentiment:
                self.pending_gann_setup = None

        if not self.pending_gann_setup:
            # 🟢 CE SETUP
            if bullish_sentiment and is_green and (body_ratio >= MIN_BODY_RATIO):
                bottom_sweep_ce = (spot_low <= sup_level + gann_prox_pts) and (spot_close >= sup_level - (gann_prox_pts * 0.6))
                retest_ce = (float(prev_bar["high"]) > sup_level) and (sup_level - gann_prox_pts <= spot_low <= sup_level + gann_retest_pts)
                breakout_ce = (spot_close > consolidation_high)

                if bottom_sweep_ce or retest_ce or breakout_ce or is_short_covering:
                    trigger_px = round(spot_high + buffer_pts, 2)
                    if is_short_covering:
                        setup_name = "SHORT_COVERING_SQUEEZE_CE"
                    elif breakout_ce:
                        setup_name = "CONSOLIDATION_BREAKOUT_CE"
                    elif bottom_sweep_ce:
                        setup_name = "BOTTOM_BOUNCE_CE"
                    else:
                        setup_name = "5M_RETEST_CE"

                    self.pending_gann_setup = {
                        "side": "CE",
                        "setup_type": setup_name,
                        "oi_buildup": self.current_5m_buildup_code,
                        "gann_level": sup_level,
                        "gann_barrier": res_level,
                        "trigger_level": trigger_px,
                        "raw_spot_sl": round(spot_low - swing_pad_pts, 2),
                        "atr_5m": atr_val,
                        "armed_time": time.monotonic()
                    }
                    print(f"[!] 5M Dual CE Armed: {setup_name} | Trigger @ {trigger_px} | VIX: {self.vix_register.get('vix')}", flush=True)

            # 🔴 PE SETUP
            elif bearish_sentiment and is_red and (body_ratio >= MIN_BODY_RATIO):
                top_sweep_pe = (spot_high >= res_level - gann_prox_pts) and (spot_close <= res_level + (gann_prox_pts * 0.6))
                retest_pe = (float(prev_bar["low"]) < res_level) and (res_level - gann_retest_pts <= spot_high <= res_level + gann_prox_pts)
                breakdown_pe = (spot_close < consolidation_low)

                if top_sweep_pe or retest_pe or breakdown_pe or is_put_unwinding:
                    trigger_px = round(spot_low - buffer_pts, 2)
                    if is_put_unwinding:
                        setup_name = "PUT_UNWINDING_SQUEEZE_PE"
                    elif breakdown_pe:
                        setup_name = "CONSOLIDATION_BREAKDOWN_PE"
                    elif top_sweep_pe:
                        setup_name = "TOP_REJECT_PE"
                    else:
                        setup_name = "5M_RETEST_PE"

                    self.pending_gann_setup = {
                        "side": "PE",
                        "setup_type": setup_name,
                        "oi_buildup": self.current_5m_buildup_code,
                        "gann_level": res_level,
                        "gann_barrier": sup_level,
                        "trigger_level": trigger_px,
                        "raw_spot_sl": round(spot_high + swing_pad_pts, 2),
                        "atr_5m": atr_val,
                        "armed_time": time.monotonic()
                    }
                    print(f"[!] 5M Dual PE Armed: {setup_name} | Trigger @ {trigger_px} | VIX: {self.vix_register.get('vix')}", flush=True)

        if self.pending_gann_setup:
            setup = self.pending_gann_setup
            if time.monotonic() - setup["armed_time"] > 900:
                self.pending_gann_setup = None
                return

            current_spot_tick = self.api.get_ltp("NSE_INDEX|Nifty 50") or spot_close

            triggered = False
            if setup["side"] == "CE" and current_spot_tick >= setup["trigger_level"]:
                triggered = True
            elif setup["side"] == "PE" and current_spot_tick <= setup["trigger_level"]:
                triggered = True

            if triggered:
                atm_strike = round(current_spot_tick / self.strike_step) * self.strike_step

                if atm_strike in self.failed_strikes:
                    if time.monotonic() < self.failed_strikes[atm_strike]:
                        self.pending_gann_setup = None
                        return
                    else:
                        del self.failed_strikes[atm_strike]

                fresh_mem = self.api.get_live_option_chain_memory(self.expiry, atm_strike, current_spot_tick)
                opt_greeks = fresh_mem["ce"] if setup["side"] == "CE" else fresh_mem["pe"]

                target_chg_oi = opt_greeks.get("chg_oi", 0)
                live_iv = float(opt_greeks.get("iv", 0.0))
                live_abs_delta = abs(float(opt_greeks.get("delta", 0.52)))
                live_theta = float(opt_greeks.get("theta", 0.0))
                max_iv_limit = self.vix_register.get("max_iv", MAX_ALLOWED_IV)

                if target_chg_oi < 0 and setup["oi_buildup"] not in ("SHORT_COVERING_SQUEEZE", "PUT_UNWINDING_SQUEEZE"):
                    self.pending_gann_setup = None
                    return

                if live_iv > max_iv_limit:
                    self.pending_gann_setup = None
                    return

                if live_abs_delta < MIN_ABS_DELTA:
                    self.pending_gann_setup = None
                    return

                if hhmm >= "14:00" and abs(live_theta) > 15.0 and adx_val < 22.0:
                    self.pending_gann_setup = None
                    return

                contract_info = self.contracts_cache.get((atm_strike, setup["side"]))
                if not contract_info:
                    return

                inst_key = contract_info["instrument_key"]
                trad_sym = contract_info["trading_symbol"]
                ltp = opt_greeks.get("ltp") or self.api.get_ltp(inst_key)

                min_valid_prem = current_spot_tick * 0.0015
                max_valid_prem = current_spot_tick * 0.0220

                if ltp and min_valid_prem <= ltp <= max_valid_prem:
                    self.execute_entry(
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
                        vix_info=self.vix_register,
                        delta_pcr=delta_pcr,
                        max_pain=max_pain,
                        greeks=opt_greeks,
                        oi_buildup=setup["oi_buildup"],
                        fut_mem=self.futures_memory_register,
                        setup_type=setup["setup_type"],
                        df_5m=df_5m
                    )
                    self.pending_gann_setup = None


# ========================= MAIN =========================
def main():
    global TOKEN
    if not TOKEN:
        TOKEN = fetch_token()

    if not TOKEN:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN उपलब्ध नाही. कृपया ACCESS_TOKEN.txt तपासा.")

    api = UpstoxClient(TOKEN)
    timings = get_live_market_session_timings(api, TODAY_STR)
    trader = LivePaperTrader(api, timings)

    send_telegram(
        f"🟢 <b>NIFTY 5M SCALE-INVARIANT + INDIA VIX ALGO: सुरू झाले</b>\n"
        f"तारीख: <code>{TODAY_STR}</code> | Dynamic Lot: <code>{trader.lot_size}</code>\n"
        f"सत्र वेळ: <b>{timings['raw_start']} ते {timings['raw_end']} IST</b>\n"
        f"सिस्टीम: <b>Live India VIX + Remaining Fuel + % SL/Target + Dual VWAP + Greeks</b>"
    )
    print(f"[*] Scale-Invariant + India VIX Engine Active ({timings['raw_start']} - {timings['raw_end']}) for {TODAY_STR}...", flush=True)

    last_strat = 0.0
    try:
        while True:
            hhmm = now_ist().strftime("%H:%M")
            if hhmm >= timings["market_end"]:
                print(f"[*] {hhmm}: Market session संपले. Live Engine थांबवत आहे...", flush=True)
                send_telegram(f"⏱️ <b>मार्केट सेशन समाप्त ({hhmm}):</b>\nआजचे इंजिन सुरक्षित बंद केले आहे.")
                break

            trader.check_position_management()

            if timings["session_start"] <= hhmm <= timings["entry_end"]:
                if time.monotonic() - last_strat >= STRATEGY_INTERVAL:
                    trader.run_strategy_scan()
                    last_strat = time.monotonic()

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n[!] Manually stopped.", flush=True)
        send_telegram("⚠️ <b>NIFTY ALGO इंजिन: मॅन्युअली थांबवले</b>")


if __name__ == "__main__":
    main()
