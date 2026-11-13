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

# ========================= SCALE-INVARIANT PERCENTAGE & STRATEGY PARAMETERS =========================
POLL_INTERVAL = 1.0
STRATEGY_INTERVAL = 10.0
POSITION_UPDATE_INTERVAL = 180.0

# Pure Integer Gann & Breakout-Retest Parameters
GANN_PROXIMITY_PCT = 0.0014        # शुद्ध पूर्णांक Gann Level जवळचा झोन = 0.14% (~35 pts on 25k)
GANN_RETEST_BAND_PCT = 0.0020      # Gann Retest झोन = 0.20% (~50 pts on 25k)
ENTRY_BUFFER_PCT = 0.00025         # Breakout Buffer = 0.025%
MIN_REMAINING_FUEL_PCT = 0.0010    # दिवसाचे किमान शिल्लक इंधन = 0.10%
STRIKE_SCAN_RANGE_PCT = 0.020      # Spot च्या ±2.0% रेंजमधील सर्व स्ट्राइक्स

# Backtest-Proven Candle & Volume Expansion Rules
MIN_FUT_VOL_RATIO = 1.20           # Futures Volume >= 1.20x of 20-bar SMA
MIN_RANGE_ATR_MULT = 1.15          # Candle Range >= 1.15x of ATR14
MIN_BODY_RATIO = 0.45              # Solid Candle Body >= 45%

# Option Premium Percentage & ATR*Delta SL/Target (1 : 1.50 R:R)
MIN_OPT_SL_PCT = 0.10              # ऑप्शन प्रीमियमच्या किमान 10% SL
MAX_OPT_SL_PCT = 0.22              # ऑप्शन प्रीमियमच्या कमाल 22% SL
MAX_OPT_TRG_PCT = 0.35             # ऑप्शन प्रीमियमच्या कमाल 35% Target
REALISTIC_RRR = 1.50               # बॅकटेस्ट सिद्ध 1 : 1.50 रिस्क-रिवॉर्ड

# Daily Capital Protection (1 Lot Risk Multiplier)
DAILY_GOAL_LOT_RISK_MULT = 1.50
DAILY_LOSS_LOT_RISK_MULT = -1.50
MAX_CONSECUTIVE_LOSSES = 2

# Live Greeks & OI Safety Guards
MAX_ALLOWED_IV = 28.0
MIN_ABS_DELTA = 0.35
MIN_5M_OI_SHIFT = 250000

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
        r = requests.post(url, json=payload, timeout=8)
        if r.status_code != 200:
            print(f"[-] Telegram API HTTP {r.status_code}: {r.text[:150]}", flush=True)
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
        plt.axhline(y=gann_level, color='blue', linestyle='--', label=f'Integer Gann Pivot: {int(gann_level)}')
        plt.axhline(y=sl_price, color='red', linestyle=':', label=f'Spot SL: {sl_price:.1f}')
        plt.axhline(y=trg_price, color='purple', linestyle=':', label=f'Spot Target: {trg_price:.1f}')
        plt.title("NIFTY 5M Integer Gann + Fut Vol Breakout-Retest Setup", fontsize=12, fontweight='bold')
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


# ========================= PURE INTEGER GANN LEVELS =========================
def get_gann_levels(current_price: float, count: int = 5) -> dict:
    """
    फक्त शुद्ध पूर्णांक (Integer) Gann Square of 9 लेव्हल्स:
    उदा. 158^2 = 24964, 159^2 = 25281 (कोणतेही दशांश/डेसिमल लेव्हल्स नाहीत).
    """
    base_root = int(math.floor(math.sqrt(current_price)))
    lower_levels = [float((base_root - i) ** 2) for i in range(count, 0, -1)]
    base_level = [float(base_root ** 2)]
    upper_levels = [float((base_root + i) ** 2) for i in range(1, count + 1)]
    all_levels = sorted(lower_levels + base_level + upper_levels)
    
    supports = [lvl for lvl in all_levels if lvl <= current_price]
    resistances = [lvl for lvl in all_levels if lvl > current_price]
    
    immediate_sup = max(supports) if supports else float((base_root - 1) ** 2)
    immediate_res = min(resistances) if resistances else float((base_root + 1) ** 2)
    next_res = [lvl for lvl in resistances if lvl > immediate_res]
    next_sup = [lvl for lvl in supports if lvl < immediate_sup]
    
    return {
        "all_levels": all_levels,
        "immediate_support": immediate_sup,
        "immediate_resistance": immediate_res,
        "target_ce": next_res[0] if next_res else float((base_root + 2) ** 2),
        "target_pe": next_sup[-1] if next_sup else float((base_root - 2) ** 2)
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
        vix_val = self.get_ltp("NSE_INDEX|India VIX")
        if vix_val is None or vix_val <= 0:
            return {
                "vix": None,
                "regime": "NORMAL (N/A)",
                "max_iv": MAX_ALLOWED_IV,
                "implied_daily_pct": 0.0085
            }

        implied_daily_pct = round((vix_val / 100.0) / 15.87, 5)

        if vix_val < 13.0:
            regime = "CALM (<13 · Slow)"
            max_iv = MAX_ALLOWED_IV
        elif vix_val <= 17.5:
            regime = "NORMAL (13-17.5)"
            max_iv = MAX_ALLOWED_IV
        elif vix_val <= 22.0:
            regime = "ELEVATED (17.5-22)"
            max_iv = MAX_ALLOWED_IV
        else:
            regime = "PANIC (>22 · High Vol)"
            max_iv = 26.0

        return {
            "vix": round(vix_val, 2),
            "regime": regime,
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
            "fut_vol_ratio": 1.0,
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
            avg_vol = float(df_5m["volume"].tail(20).mean())
            vol_ratio = round(cur_vol / avg_vol, 2) if avg_vol > 0 else 1.0
            vol_surge = vol_ratio >= MIN_FUT_VOL_RATIO

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
                "fut_avg_5m_vol": int(avg_vol),
                "fut_vol_ratio": vol_ratio,
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
                try:
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
        except Exception as e:
            print(f"[-] Live Memory Fetch Error: {e}", flush=True)
            memory["delta_oi_pcr"] = 1.00
        return memory


def get_live_market_session_timings(api: UpstoxClient, target_date: str) -> dict:
    # सकाळी 09:20 ते दुपारी 15:10 (3:10 PM) पर्यंत सलग ट्रेडिंग विंडो
    return {
        "session_start": "09:20",
        "entry_end": "15:10",
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
            return "MAJOR_SHORT_COVERING", "🚀 MAJOR SHORT COVERING (Call Exit)"
        if shift_put_oi >= MIN_5M_OI_SHIFT and shift_call_oi <= 0:
            return "STRONG_PUT_WRITING", "🟢 STRONG PUT WRITING"
        if shift_call_oi >= MIN_5M_OI_SHIFT and shift_put_oi <= -MIN_5M_OI_SHIFT:
            return "BULL_TRAP", "⚠️ BULL TRAP (Heavy Call Writing on Rise)"
    elif is_red:
        if shift_put_oi <= -MIN_5M_OI_SHIFT:
            return "MAJOR_PUT_UNWINDING", "🔻 MAJOR PUT UNWINDING (Put Exit)"
        if shift_call_oi >= MIN_5M_OI_SHIFT and shift_put_oi <= 0:
            return "STRONG_CALL_WRITING", "🔴 STRONG CALL WRITING"
        if shift_put_oi >= MIN_5M_OI_SHIFT and shift_call_oi <= -MIN_5M_OI_SHIFT:
            return "BEAR_TRAP", "⚠️ BEAR TRAP (Heavy Put Writing on Fall)"

    return "STEADY_TREND_FLOW", "⚪ STEADY OI FLOW"


# ========================= LIVE TRADING ENGINE =========================
class LivePaperTrader:
    def __init__(self, api: UpstoxClient, timings: dict):
        self.api = api
        self.timings = timings
        self.active_position = None
        self.trade_counter = 0

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
        self.current_5m_buildup_code = "STEADY_TREND_FLOW"
        self.current_5m_buildup_label = "⚪ STEADY OI FLOW"
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
        print(f"[*] Integer Gann + Continuous (09:20-15:10) Engine Ready | LotSize: {self.lot_size} | StrikeStep: {self.strike_step}", flush=True)

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
        self.daily_profit_goal = round(max(1200.0, one_trade_std_risk_rupees * DAILY_GOAL_LOT_RISK_MULT), 0)
        self.daily_max_loss = round(min(-1200.0, one_trade_std_risk_rupees * DAILY_LOSS_LOT_RISK_MULT), 0)

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

    def send_5m_sentiment_pulse(self, df_5m: pd.DataFrame, delta_pcr: float, max_pain_display: Any, mem: dict, fut_mem: dict, smart_info: dict, fuel_info: dict, vix_info: dict, gann_zone_status: str):
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
        fut_vol_ratio = fut_mem.get("fut_vol_ratio", 1.0)
        fut_oi_chg = fut_mem.get("fut_5m_oi_chg", 0)
        fut_label = fut_mem.get("fut_buildup_label", "⚪ FUT N/A")
        smart_tag = smart_info.get("smartlist_tag", "N/A")

        s_range = mem.get("strike_range", "")
        tot_pe_chg = mem.get("total_put_chg", 0)
        tot_ce_chg = mem.get("total_call_chg", 0)
        ce_g = mem.get("ce", {})
        pe_g = mem.get("pe", {})

        gann = get_gann_levels(spot, count=3)
        sup = gann["immediate_support"]
        res = gann["immediate_resistance"]

        if fuel_info["remaining_pct"] < MIN_REMAINING_FUEL_PCT:
            total_sentiment = "⛽ DAILY FUEL EXHAUSTED"
            focus_action = "🛑 NO NEW TRADES (Daily Range Done)"
        elif gann_zone_status == "NO_GANN_ZONE":
            total_sentiment = f"📐 MID-ZONE ({int(sup)} ↔ {int(res)})"
            focus_action = f"⏳ WAIT FOR INTEGER GANN LEVEL ({int(sup)} or {int(res)})"
        else:
            total_sentiment = f"🎯 AT {gann_zone_status} (VolRatio: {fut_vol_ratio}x)"
            focus_action = "🔥 WATCHING CANDLE BREAKOUT + RETEST AT GANN"

        fut_vwap_status = "Above Fut-VWAP 🟢" if fut_px >= fut_vwap else "Below Fut-VWAP 🔴"

        msg = (
            f"📊 <b>NIFTY 5M INTEGER GANN PULSE ({bar_time_str} IST)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 <b>Spot:</b> <code>{spot:.2f}</code> | <b>Gann:</b> <code>{int(sup)}</code> ⬅️ ➡️ <code>{int(res)}</code> ({gann_zone_status})\n"
            f"🌡️ <b>India VIX:</b> <code>{vix_str}</code> | ⛽ <b>Fuel Left:</b> <code>{fuel_info['remaining_pts']:.1f} pts</code> ({fuel_info['fuel_left_pct_of_day']}%)\n"
            f"📈 <b>Futures ({self.fut_symbol}):</b> <code>{fut_px:.2f}</code> (Basis: <code>{fut_basis:+.1f}</code>)\n"
            f"• <b>True Fut VWAP:</b> <code>{fut_vwap:.2f}</code> ({fut_vwap_status})\n"
            f"• <b>Fut 5M Vol:</b> <code>{fut_vol:,}</code> (<code>{fut_vol_ratio}x</code>) | <b>5M OI Chg:</b> <code>{fut_oi_chg:+,}</code>\n"
            f"• <b>Fut Build-up:</b> <b>{fut_label}</b> | <b>Smartlist:</b> <code>{smart_tag}</code>\n\n"
            f"🔹 <b>SMC ±2% STRIKES OI & ΔOI ({s_range}):</b>\n"
            f"• <b>Day ΔOI PCR:</b> <code>{delta_pcr:.2f}</code> | <b>Max Pain:</b> <code>{max_pain_display}</code>\n"
            f"• <b>Day Chg OI:</b> <code>PE {tot_pe_chg:+,}</code> | <code>CE {tot_ce_chg:+,}</code>\n"
            f"• <b>5M Shift:</b> <code>PE {self.shift_5m_put_oi:+,}</code> | <code>CE {self.shift_5m_call_oi:+,}</code> ({self.current_5m_buildup_label})\n\n"
            f"🔹 <b>LIVE ATM GREEKS ({int(mem.get('atm_strike', 0))}):</b>\n"
            f"• <b>CE:</b> Δ <code>{ce_g.get('delta', 0):.2f}</code> | IV <code>{ce_g.get('iv', 0):.1f}%</code> | θ <code>{ce_g.get('theta', 0):.1f}</code> | OI <code>{ce_g.get('oi', 0):,}</code>\n"
            f"• <b>PE:</b> Δ <code>{pe_g.get('delta', 0):.2f}</code> | IV <code>{pe_g.get('iv', 0):.1f}%</code> | θ <code>{pe_g.get('theta', 0):.1f}</code> | OI <code>{pe_g.get('oi', 0):,}</code>\n\n"
            f"🔹 <b>MOMENTUM:</b> RSI <code>{rsi:.1f}</code> | ADX <code>{adx:.1f}</code> | ATR <code>{atr:.1f}</code> | Lot: <code>{self.lot_size}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏁 <b>STATUS:</b> <b>{total_sentiment}</b>\n"
            f"🎯 <b>ACTION:</b> <b>{focus_action}</b>"
        )
        send_telegram(msg)
        print(f"[{bar_time_str}] Pulse: {total_sentiment} | VolRatio:{fut_vol_ratio}x | DayΔPCR:{delta_pcr:.2f}", flush=True)

    def send_entry_alert(self, pos: dict, df_5m: pd.DataFrame):
        caption = (
            f"🚀 <b>NIFTY 5M INTEGER GANN BREAKOUT-RETEST: एन्ट्री (#{pos['trade_id']})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>ऑर्डर:</b> <b>BUY {pos['clean_name']}</b>\n"
            f"🏷️ <b>सिम्बॉल:</b> <code>{pos['symbol']}</code>\n"
            f"📦 <b>लॉट:</b> <code>1 Lot</code> (Dynamic Qty: <code>{pos['lot_size']}</code>)\n"
            f"💵 <b>खरेदी किंमत:</b> <code>₹{pos['entry_px']:.2f}</code>\n"
            f"🛑 <b>स्टॉपलॉस:</b> <code>₹{pos['sl_px']:.2f}</code> (-{pos['opt_sl_pts']:.1f} pts / -{pos['opt_sl_pct']:.1f}% | Spot SL: {pos['spot_sl']})\n"
            f"🏁 <b>टार्गेट (1:1.5):</b> <code>₹{pos['trg_px']:.2f}</code> (+{pos['opt_trg_pts']:.1f} pts / +{pos['opt_trg_pct']:.1f}% | Spot Trg: {pos['spot_target']})\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📐 <b>Integer Gann Anchor:</b> <code>{int(pos['gann_level'])}</code> | ⛽ <b>Fuel Left:</b> <code>{pos['rem_fuel_pts']:.1f} pts</code>\n"
            f"🧬 <b>Live Option Greeks & OI Data:</b>\n"
            f"• <b>Delta (Δ):</b> <code>{pos['entry_delta']:.2f}</code> | <b>IV:</b> <code>{pos['entry_iv']:.1f}%</code> | <b>Theta:</b> <code>{pos['entry_theta']:.1f}</code>\n"
            f"• <b>Strike OI:</b> <code>{pos['strike_oi']:,}</code> | <b>Chg OI:</b> <code>{pos['change_oi']:+,}</code> | <b>Day ΔPCR:</b> <code>{pos['delta_pcr']:.2f}</code>\n"
            f"• <b>Fut Build-up:</b> <code>{pos['fut_buildup']}</code> | <b>Max Pain:</b> <code>{pos['max_pain']}</code>\n"
            f"📍 <b>NIFTY Spot:</b> <code>{pos['spot_px']:.2f}</code> | <b>India VIX:</b> <code>{pos['india_vix']}</code>\n"
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
                      atr_5m: float, fuel_info: dict, vix_info: dict, delta_pcr: float, max_pain_display: Any, greeks: dict,
                      oi_buildup: str, fut_mem: dict, setup_type: str, df_5m: pd.DataFrame):
        entry_price = ltp
        live_abs_delta = max(0.35, min(0.75, abs(float(greeks.get("delta", 0.52)))))

        # बॅकटेस्ट सिद्ध सूत्र: calc_sl_pts = atr * 1.2 * Live_Delta (किमान 10% आणि कमाल 22% ऑफ प्रीमियम)
        atr_opt_sl = atr_5m * 1.2 * live_abs_delta
        min_opt_sl = entry_price * MIN_OPT_SL_PCT
        max_opt_sl = entry_price * MAX_OPT_SL_PCT
        opt_sl_pts = round(min(max_opt_sl, max(min_opt_sl, atr_opt_sl)), 2)

        # 1 : 1.50 Risk-to-Reward (शिल्लक दैनिक इंधनाच्या मर्यादेत)
        fuel_opt_cap = max(opt_sl_pts * 1.25, (fuel_info["remaining_pts"] * 0.70) * live_abs_delta)
        max_opt_trg = entry_price * MAX_OPT_TRG_PCT
        opt_trg_pts = round(min(max_opt_trg, fuel_opt_cap, opt_sl_pts * REALISTIC_RRR), 2)

        spot_risk = round(opt_sl_pts / live_abs_delta, 2)
        spot_reward = round(opt_trg_pts / live_abs_delta, 2)
        realistic_spot_sl = round((spot_px - spot_risk) if side == "CE" else (spot_px + spot_risk), 2)
        realistic_spot_target = round((spot_px + spot_reward) if side == "CE" else (spot_px - spot_reward), 2)

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
            "max_pain": max_pain_display,
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

        print(f"\n[एन्ट्री ट्रिगर] {clean_name} (Lot:{self.lot_size}) @ ₹{entry_price} | Gann:{int(gann_level)} | SL: ₹{sl} (-{opt_sl_pct}%) | TRG: ₹{trg} (+{opt_trg_pct}%)", flush=True)
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
        self.cooldown_until = time.monotonic() + 300

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
        if df_5m.empty or len(df_5m) < 2:
            print(f"[{hhmm}] Waiting for 5M spot candles...", flush=True)
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
        atr_val = float(completed_bar.get("atr14", spot_close * 0.0006)) if pd.notna(completed_bar.get("atr14")) else (spot_close * 0.0006)

        self.vix_register = self.api.get_live_india_vix()
        self.update_dynamic_daily_limits(spot_close)
        fuel_info = self.calculate_remaining_daily_fuel(df_5m, spot_close, self.vix_register)

        is_green = spot_close > spot_open
        is_red = spot_close < spot_open

        max_pain = self.api.get_official_max_pain(self.expiry)
        max_pain_display = max_pain if max_pain is not None else "Calculating..."

        atm_strike = round(spot_close / self.strike_step) * self.strike_step
        self.live_memory_register = self.api.get_live_option_chain_memory(self.expiry, atm_strike, spot_close)
        delta_pcr = self.live_memory_register.get("delta_oi_pcr") or 1.00

        self.futures_memory_register = self.api.get_futures_5m_metrics(self.fut_instrument_key)
        smartlist_info = self.api.get_futures_smartlist_signal(self.fut_instrument_key)

        gann = get_gann_levels(spot_close, count=5)
        sup_level = gann["immediate_support"]
        res_level = gann["immediate_resistance"]
        gann_prox_pts = spot_close * GANN_PROXIMITY_PCT
        gann_retest_pts = spot_close * GANN_RETEST_BAND_PCT

        near_sup_gann = (spot_low <= sup_level + gann_prox_pts) and (spot_close >= sup_level - gann_prox_pts)
        near_res_gann = (spot_high >= res_level - gann_prox_pts) and (spot_close <= res_level + gann_prox_pts)
        broke_res_gann = (float(prev_bar["close"]) <= res_level) and (spot_close > res_level)
        broke_sup_gann = (float(prev_bar["close"]) >= sup_level) and (spot_close < sup_level)

        if near_sup_gann or broke_sup_gann:
            gann_zone_status = f"NEAR_GANN_{int(sup_level)}"
        elif near_res_gann or broke_res_gann:
            gann_zone_status = f"NEAR_GANN_{int(res_level)}"
        else:
            gann_zone_status = "NO_GANN_ZONE"

        self.update_5m_oi_shift_register(bar_time_str, is_green, is_red, self.live_memory_register)
        self.send_5m_sentiment_pulse(df_5m, delta_pcr, max_pain_display, self.live_memory_register, self.futures_memory_register, smartlist_info, fuel_info, self.vix_register, gann_zone_status)

        if len(df_5m) < 6:
            return

        if fuel_info["remaining_pct"] < MIN_REMAINING_FUEL_PCT:
            if self.pending_gann_setup:
                self.pending_gann_setup = None
            return

        # जर खूप मोठा Trap (2.5L+ विरुद्ध OI लेखन) असेल तरच ब्लॉक करा
        if self.current_5m_buildup_code in ("BULL_TRAP", "BEAR_TRAP"):
            self.pending_gann_setup = None
            return

        # =========================================================================
        # STAGE 1: PURE INTEGER GANN REACH + CANDLE EXPANSION / VOLUME BREAKOUT
        # बॅकटेस्ट नियम: (has_volume >= 1.20x OR range >= 1.15x ATR) AND solid body >= 0.45
        # =========================================================================
        c_range = max(spot_high - spot_low, 1e-9)
        c_body = abs(spot_close - spot_open)
        body_ratio = c_body / c_range

        fut_vol_surge = self.futures_memory_register.get("fut_vol_surge", False)
        is_range_expansion = c_range >= (MIN_RANGE_ATR_MULT * atr_val)
        is_body_solid = body_ratio >= MIN_BODY_RATIO

        fut_valid = self.futures_memory_register.get("valid", False)
        above_fut_vwap = self.futures_memory_register.get("above_fut_vwap", True) if fut_valid else True
        below_fut_vwap = (not self.futures_memory_register.get("above_fut_vwap", False)) if fut_valid else True

        swing_pad_pts = round(spot_close * 0.0002, 2)
        retest_buf_pts = round(0.30 * atr_val, 2)

        if not self.pending_gann_setup and (fut_vol_surge or is_range_expansion) and is_body_solid:
            # 🟢 CE SETUP: शुद्ध पूर्णांक Gann Support Bounce किंवा Gann Resistance Breakout नंतर मजबूत हिरवी कॅन्डल
            if is_green and (spot_close > spot_vwap_5m) and above_fut_vwap:
                gann_sup_bounce_ce = (spot_low <= sup_level + gann_prox_pts) and (spot_close >= sup_level - (gann_prox_pts * 0.5))
                gann_sup_retest_ce = (float(prev_bar["high"]) > sup_level) and (sup_level - gann_prox_pts <= spot_low <= sup_level + gann_retest_pts)
                gann_res_breakout_ce = (float(prev_bar["close"]) <= res_level) and (spot_close > res_level)

                if gann_sup_bounce_ce or gann_sup_retest_ce or gann_res_breakout_ce:
                    if gann_res_breakout_ce:
                        setup_name = f"GANN_{int(res_level)}_VOL_BREAKOUT_CE"
                        anchor_gann = res_level
                        next_barrier = gann["target_ce"]
                    elif gann_sup_bounce_ce:
                        setup_name = f"GANN_{int(sup_level)}_VOL_BOUNCE_CE"
                        anchor_gann = sup_level
                        next_barrier = res_level
                    else:
                        setup_name = f"GANN_{int(sup_level)}_VOL_RETEST_CE"
                        anchor_gann = sup_level
                        next_barrier = res_level

                    self.pending_gann_setup = {
                        "side": "CE",
                        "setup_type": setup_name,
                        "oi_buildup": self.current_5m_buildup_code,
                        "gann_level": anchor_gann,
                        "gann_barrier": next_barrier,
                        "breakout_level": spot_high,
                        "retest_min": round(spot_high - retest_buf_pts, 2),
                        "trigger_level": round(spot_high + (spot_close * ENTRY_BUFFER_PCT), 2),
                        "retest_touched": False,
                        "raw_spot_sl": round(spot_low - swing_pad_pts, 2),
                        "atr_5m": atr_val,
                        "armed_time": time.monotonic()
                    }
                    print(f"[!] Gann Breakout Armed (CE): {setup_name} | Retest Zone: {self.pending_gann_setup['retest_min']} | Trigger: {self.pending_gann_setup['trigger_level']}", flush=True)

            # 🔴 PE SETUP: शुद्ध पूर्णांक Gann Resistance Reject किंवा Gann Support Breakdown नंतर मजबूत लाल कॅन्डल
            elif is_red and (spot_close < spot_vwap_5m) and below_fut_vwap:
                gann_res_reject_pe = (spot_high >= res_level - gann_prox_pts) and (spot_close <= res_level + (gann_prox_pts * 0.5))
                gann_res_retest_pe = (float(prev_bar["low"]) < res_level) and (res_level - gann_retest_pts <= spot_high <= res_level + gann_prox_pts)
                gann_sup_breakdown_pe = (float(prev_bar["close"]) >= sup_level) and (spot_close < sup_level)

                if gann_res_reject_pe or gann_res_retest_pe or gann_sup_breakdown_pe:
                    if gann_sup_breakdown_pe:
                        setup_name = f"GANN_{int(sup_level)}_VOL_BREAKDOWN_PE"
                        anchor_gann = sup_level
                        next_barrier = gann["target_pe"]
                    elif gann_res_reject_pe:
                        setup_name = f"GANN_{int(res_level)}_VOL_REJECT_PE"
                        anchor_gann = res_level
                        next_barrier = sup_level
                    else:
                        setup_name = f"GANN_{int(res_level)}_VOL_RETEST_PE"
                        anchor_gann = res_level
                        next_barrier = sup_level

                    self.pending_gann_setup = {
                        "side": "PE",
                        "setup_type": setup_name,
                        "oi_buildup": self.current_5m_buildup_code,
                        "gann_level": anchor_gann,
                        "gann_barrier": next_barrier,
                        "breakout_level": spot_low,
                        "retest_max": round(spot_low + retest_buf_pts, 2),
                        "trigger_level": round(spot_low - (spot_close * ENTRY_BUFFER_PCT), 2),
                        "retest_touched": False,
                        "raw_spot_sl": round(spot_high + swing_pad_pts, 2),
                        "atr_5m": atr_val,
                        "armed_time": time.monotonic()
                    }
                    print(f"[!] Gann Breakout Armed (PE): {setup_name} | Retest Zone: {self.pending_gann_setup['retest_max']} | Trigger: {self.pending_gann_setup['trigger_level']}", flush=True)

        # =========================================================================
        # STAGE 2: RETEST CONFIRMATION & LIVE ENTRY (बॅकटेस्टची 15-मिनिट री-टेस्ट कन्फर्मेशन)
        # =========================================================================
        if self.pending_gann_setup:
            setup = self.pending_gann_setup
            if time.monotonic() - setup["armed_time"] > 900:
                self.pending_gann_setup = None
                return

            current_spot_tick = self.api.get_ltp("NSE_INDEX|Nifty 50") or spot_close

            if setup["side"] == "CE":
                if current_spot_tick <= setup["breakout_level"] and current_spot_tick >= setup["retest_min"]:
                    setup["retest_touched"] = True
                if current_spot_tick < setup["raw_spot_sl"]:
                    self.pending_gann_setup = None
                    return
                triggered = setup["retest_touched"] and (current_spot_tick >= setup["trigger_level"])
            else:
                if current_spot_tick >= setup["breakout_level"] and current_spot_tick <= setup["retest_max"]:
                    setup["retest_touched"] = True
                if current_spot_tick > setup["raw_spot_sl"]:
                    self.pending_gann_setup = None
                    return
                triggered = setup["retest_touched"] and (current_spot_tick <= setup["trigger_level"])

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

                live_iv = float(opt_greeks.get("iv", 0.0))
                live_abs_delta = abs(float(opt_greeks.get("delta", 0.52)))
                max_iv_limit = self.vix_register.get("max_iv", MAX_ALLOWED_IV)

                if live_iv > max_iv_limit or live_abs_delta < MIN_ABS_DELTA:
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
                        max_pain_display=max_pain_display,
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
        f"🟢 <b>NIFTY 5M INTEGER GANN CONTINUOUS ALGO: सुरू झाले</b>\n"
        f"तारीख: <code>{TODAY_STR}</code> | Dynamic Lot: <code>{trader.lot_size}</code>\n"
        f"ट्रेडिंग विंडो: <b>{timings['session_start']} ते {timings['entry_end']} IST</b> (Force Exit: {timings['force_exit']})\n"
        f"नियम: <b>Pure Integer Gann + Fut Vol Breakout-Retest + Live Greeks/OI + 1:1.5 R:R</b>"
    )
    print(f"[*] Integer Gann Continuous Engine Active ({timings['session_start']} - {timings['entry_end']}) for {TODAY_STR}...", flush=True)

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
