import os
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
MAX_SIMULTANEOUS_POSITIONS = 2     # एकाच वेळी कमाल २ वेगवेगळ्या इंडेक्समध्ये पोझिशन्स

# Gann 0.50 Lattice, VWAP & Price Action Parameters
GANN_PROXIMITY_PCT = 0.0012        # Gann Level (1.0 & 0.5 step) जवळचा झोन = 0.12%
GANN_RETEST_BAND_PCT = 0.0018      # Gann Retest झोन = 0.18%
VWAP_TOUCH_BAND_PCT = 0.0008       # VWAP Rejection स्पर्श झोन = 0.08%
ENTRY_BUFFER_PCT = 0.00020         # Breakout Buffer = 0.020%
MIN_REMAINING_FUEL_PCT = 0.0010    # दिवसाचे किमान शिल्लक इंधन = 0.10%
STRIKE_SCAN_RANGE_PCT = 0.020      # Spot च्या ±2.0% रेंजमधील सर्व स्ट्राइक्स

# Price Action & Momentum Thresholds
MIN_ADX_MOMENTUM = 18.0            # ट्रेंडिंग मोमेंटमसाठी किमान ADX
MIN_FUT_VOL_RATIO = 1.15           # Futures Volume >= 1.15x of 20-bar SMA
MIN_RANGE_ATR_MULT = 1.10          # Candle Range >= 1.10x of ATR14
MIN_BODY_RATIO = 0.45              # Solid Candle Body >= 45%
MIN_REJECTION_WICK_RATIO = 0.45    # Reversal Rejection Wick >= 45%

# PCR Velocity (Slope) Thresholds
PCR_SLOPE_BEAR_THRESHOLD = -0.22
PCR_SLOPE_BULL_THRESHOLD = 0.20

# Option Premium Percentage & ATR*Delta SL/Target (1 : 1.50 R:R)
MIN_OPT_SL_PCT = 0.10              # ऑप्शन प्रीमियमच्या किमान 10% SL
MAX_OPT_SL_PCT = 0.22              # ऑप्शन प्रीमियमच्या कमाल 22% SL
MAX_OPT_TRG_PCT = 0.35             # ऑप्शन प्रीमियमच्या कमाल 35% Target
REALISTIC_RRR = 1.50               # 1 : 1.50 रिस्क-रिवॉर्ड

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

def send_telegram_chart_alert(index_code: str, df_5m: pd.DataFrame, gann_level: float, sl_price: float, trg_price: float, caption_text: str = ""):
    if not HAS_MATPLOTLIB or df_5m.empty or len(df_5m) < 5:
        send_telegram(caption_text)
        return

    try:
        recent = df_5m.tail(28)
        plt.figure(figsize=(10, 5))
        plt.plot(recent['dt'], recent['close'], label=f'{index_code} 5M Spot', color='black', linewidth=1.5)
        if 'vwap' in recent.columns:
            plt.plot(recent['dt'], recent['vwap'], label='Spot VWAP', color='orange', linestyle='-.', linewidth=1.2)
        if 'ema20' in recent.columns:
            plt.plot(recent['dt'], recent['ema20'], label='EMA 20', color='green', linestyle='--', linewidth=1.0)
        plt.axhline(y=gann_level, color='blue', linestyle='--', label=f'Gann Pivot: {gann_level:.0f}')
        plt.axhline(y=sl_price, color='red', linestyle=':', label=f'Spot SL: {sl_price:.1f}')
        plt.axhline(y=trg_price, color='purple', linestyle=':', label=f'Spot Target: {trg_price:.1f}')
        plt.title(f"{index_code} 5M Levels + Price Action Confluence Setup", fontsize=12, fontweight='bold')
        plt.legend(loc='upper left', fontsize=8)
        plt.grid(True, linestyle=":", alpha=0.5)
        plt.tight_layout()

        chart_path = f"live_gann_alert_{index_code.lower()}.png"
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


# ========================= GANN 0.50 HALF-STEP LATTICE =========================
def get_gann_levels(current_price: float, steps_each_side: int = 6) -> dict:
    """
    0.50 Gann Square of 9 लेव्हल्स (उदा. 158.0^2 = 24964, 158.5^2 = 25122, 159.0^2 = 25281):
    ज्यामुळे दोन पूर्णांक गॅन लेव्हल्सच्या मधला मोठा गॅप भरून अचूक मिड-पिव्होट मिळतो.
    """
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

    def get_daily_expected_range_pct(self, spot_key: str) -> float:
        try:
            end_dt = now_ist().strftime("%Y-%m-%d")
            start_dt = (now_ist() - timedelta(days=30)).strftime("%Y-%m-%d")
            encoded_key = quote(spot_key, safe="")
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
        except Exception:
            pass
        return 0.0095

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

    def get_spot_candles_5m(self, spot_key: str) -> pd.DataFrame:
        df_1m = self.get_candles_1m(spot_key)
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
            "fut_buildup_label": "⚪ FUT N/A"
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
            vol_surge = (cur_vol > 0) and (vol_ratio >= MIN_FUT_VOL_RATIO)

            cur_oi = int(cur_bar["oi"])
            prev_oi = int(prev_bar["oi"])
            oi_5m_chg = cur_oi - prev_oi

            price_up = fut_close > fut_open
            price_dn = fut_close < fut_open

            if price_up and oi_5m_chg > 0:
                b_code = "FUT_LONG_BUILDUP"
                b_label = "🟢 FUT LONG BUILD-UP"
            elif price_dn and oi_5m_chg > 0:
                b_code = "FUT_SHORT_BUILDUP"
                b_label = "🔴 FUT SHORT BUILD-UP"
            elif price_up and oi_5m_chg < 0:
                b_code = "FUT_SHORT_COVERING"
                b_label = "🚀 FUT SHORT COVERING"
            elif price_dn and oi_5m_chg < 0:
                b_code = "FUT_LONG_UNWINDING"
                b_label = "🔻 FUT LONG UNWINDING"
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
        except Exception:
            pass
        return result

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

    def get_official_max_pain(self, spot_key: str, expiry: str) -> Optional[float]:
        try:
            params = {
                "instrument_key": spot_key,
                "expiry": expiry,
                "date": TODAY_STR,
                "bucket_interval": 15
            }
            res = self.get("/v2/market/max-pain", params=params)
            val = res.get("data", {}).get("max_pain")
            return float(val) if val is not None else None
        except Exception:
            return None

    def get_live_option_chain_memory(self, spot_key: str, expiry: str, target_strike: float, spot_price: float) -> dict:
        strike_band = round(spot_price * STRIKE_SCAN_RANGE_PCT)
        memory = {
            "atm_strike": target_strike,
            "strike_range": f"{int(target_strike - strike_band)}-{int(target_strike + strike_band)}",
            "total_call_oi": 0,
            "total_put_oi": 0,
            "total_call_chg": 0,
            "total_put_chg": 0,
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


def get_live_market_session_timings() -> dict:
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
    x["ema20"] = c.ewm(span=20, adjust=False).mean()

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
            return "MAJOR_SHORT_COVERING", "🚀 MAJOR SHORT COVERING"
        if shift_put_oi >= MIN_5M_OI_SHIFT and shift_call_oi <= 0:
            return "STRONG_PUT_WRITING", "🟢 STRONG PUT WRITING"
        if shift_call_oi >= MIN_5M_OI_SHIFT and shift_put_oi <= -MIN_5M_OI_SHIFT:
            return "BULL_TRAP", "⚠️ BULL TRAP"
    elif is_red:
        if shift_put_oi <= -MIN_5M_OI_SHIFT:
            return "MAJOR_PUT_UNWINDING", "🔻 MAJOR PUT UNWINDING"
        if shift_call_oi >= MIN_5M_OI_SHIFT and shift_put_oi <= 0:
            return "STRONG_CALL_WRITING", "🔴 STRONG CALL WRITING"
        if shift_put_oi >= MIN_5M_OI_SHIFT and shift_call_oi <= -MIN_5M_OI_SHIFT:
            return "BEAR_TRAP", "⚠️ BEAR TRAP"

    return "STEADY_TREND_FLOW", "⚪ STEADY OI FLOW"


# ========================= SENIOR ANALYST MULTI-INDEX LIVE ENGINE =========================
class MultiIndexPaperTrader:
    def __init__(self, api: UpstoxClient, timings: dict):
        self.api = api
        self.timings = timings
        self.trade_counter = 0
        self.cumulative_pnl = 0.0
        self.daily_locked = False
        self.consecutive_losses = 0
        self.last_pulse_bar_time = None

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
                "buildup_label": "⚪ STEADY OI FLOW",
                "latest_summary": None
            }

        self.load_all_indices_contracts()
        print("[*] Senior Analyst 5-Index Confluence Engine Ready!", flush=True)

    def load_all_indices_contracts(self):
        try:
            url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
            df = pd.read_csv(url, compression="gzip", low_memory=False)
            df.columns = [c.strip().lower() for c in df.columns]
            sym_col = "tradingsymbol" if "tradingsymbol" in df.columns else "trading_symbol"
            df["name_upper"] = df["name"].astype(str).str.strip().str.upper()

            for idx_code in ACTIVE_INDICES:
                cfg = INDEX_CONFIGS[idx_code]
                st = self.states[idx_code]
                exch = cfg["fo_exchange"]
                names = [n.upper() for n in cfg["master_names"]]

                futs = df[(df["exchange"] == exch) & (df["name_upper"].isin(names)) & (df["instrument_type"] == "FUTIDX")].copy()
                if not futs.empty:
                    futs["expiry"] = pd.to_datetime(futs["expiry"]).dt.strftime("%Y-%m-%d")
                    valid_futs = futs[futs["expiry"] >= TODAY_STR].sort_values("expiry")
                    if not valid_futs.empty:
                        top_fut = valid_futs.iloc[0]
                        st["fut_instrument_key"] = str(top_fut.get("instrument_key", "")).strip()
                        st["fut_symbol"] = str(top_fut.get(sym_col, "")).strip()
                        if "lot_size" in top_fut and pd.notna(top_fut["lot_size"]):
                            st["lot_size"] = int(float(top_fut["lot_size"]))

                opts = df[(df["exchange"] == exch) & (df["name_upper"].isin(names)) & (df["instrument_type"] == "OPTIDX")].copy()
                if not opts.empty:
                    opts["expiry"] = pd.to_datetime(opts["expiry"]).dt.strftime("%Y-%m-%d")
                    valid_exps = sorted([x for x in opts["expiry"].unique() if x >= TODAY_STR])
                    if valid_exps:
                        st["expiry"] = valid_exps[0]
                        active_opts = opts[opts["expiry"] == st["expiry"]].copy()
                        if "lot_size" in active_opts.columns and not active_opts["lot_size"].dropna().empty:
                            st["lot_size"] = int(float(active_opts["lot_size"].dropna().iloc[0]))

                        strikes_sorted = sorted(active_opts["strike"].dropna().unique())
                        if len(strikes_sorted) >= 2:
                            diffs = np.diff(strikes_sorted)
                            min_step = float(np.min(diffs[diffs > 0]))
                            if min_step > 0:
                                st["strike_step"] = min_step

                        for _, r in active_opts.iterrows():
                            strike = float(r.get("strike", 0))
                            opt_type = str(r.get("option_type", "")).strip().upper()
                            ikey = str(r.get("instrument_key", "")).strip()
                            sym = str(r.get(sym_col, "")).strip()
                            if strike and opt_type and ikey:
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
        except Exception as e:
            print(f"[-] Multi-Index Contracts Loading Error: {e}", flush=True)

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
            st["buildup_label"] = "⏳ SYNCED"

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

    def send_consolidated_5m_pulse(self, bar_time_str: str, vix_info: dict):
        if self.last_pulse_bar_time == bar_time_str:
            return
        self.last_pulse_bar_time = bar_time_str

        vix_val = vix_info.get("vix")
        vix_str = f"{vix_val:.2f} ({vix_info.get('regime')})" if vix_val else "N/A"

        blocks = []
        for idx_code in ACTIVE_INDICES:
            s = self.states[idx_code].get("latest_summary")
            if not s:
                blocks.append(f"• <b>{idx_code}:</b> <i>Waiting for candles...</i>")
                continue

            blocks.append(
                f"🔹 <b>{idx_code}</b> (Lot:{s['lot_size']}): <code>{s['spot']:.1f}</code> | EMA20: <code>{s['ema20']:.1f}</code>\n"
                f"   • <b>Gann 0.5:</b> <code>{int(s['sup'])}</code> ⬅️ ➡️ <code>{int(s['res'])}</code> | <b>{s['gann_zone']}</b>\n"
                f"   • <b>ΔPCR:</b> <code>{s['delta_pcr']:.2f}</code> (Slope: <code>{s['pcr_slope']:+.2f}</code>) | <b>ADX:</b> <code>{s['adx']:.1f}</code> | <b>Fuel:</b> <code>{s['fuel_pct']}%</code>\n"
                f"   • <b>Fut VWAP:</b> <code>{s['fut_vwap_status']}</code> | <b>Vol:</b> <code>{s['vol_ratio']}x</code> | <b>MaxPain:</b> <code>{s['max_pain']}</code>"
            )

        msg = (
            f"📊 <b>5-INDEX LEVELS + PRICE ACTION PULSE ({bar_time_str} IST)</b>\n"
            f"🌡️ <b>India VIX:</b> <code>{vix_str}</code> | 💰 <b>Day PnL:</b> <code>₹{self.cumulative_pnl:+,.0f}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            + "\n\n".join(blocks) +
            f"\n━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>CONFLUENCE:</b> <i>Gann 0.5 Lattice | PA Swing Momentum | VWAP Reject | PCR Slope</i>"
        )
        send_telegram(msg)
        print(f"[{bar_time_str}] 5-Index Confluence Pulse Sent | DayPnL: ₹{self.cumulative_pnl:+,.2f}", flush=True)

    def send_entry_alert(self, pos: dict, df_5m: pd.DataFrame):
        caption = (
            f"🚀 <b>[{pos['index_code']}] ALGO एन्ट्री (#{pos['trade_id']})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>ऑर्डर:</b> <b>BUY {pos['clean_name']}</b>\n"
            f"🏷️ <b>सिम्बॉल:</b> <code>{pos['symbol']}</code>\n"
            f"📦 <b>लॉट:</b> <code>1 Lot</code> (Dynamic Qty: <code>{pos['lot_size']}</code>)\n"
            f"💵 <b>खरेदी किंमत:</b> <code>₹{pos['entry_px']:.2f}</code>\n"
            f"🛑 <b>स्टॉपलॉस:</b> <code>₹{pos['sl_px']:.2f}</code> (-{pos['opt_sl_pts']:.1f} pts / -{pos['opt_sl_pct']:.1f}% | Spot SL: {pos['spot_sl']})\n"
            f"🏁 <b>टार्गेट (1:1.5):</b> <code>₹{pos['trg_px']:.2f}</code> (+{pos['opt_trg_pts']:.1f} pts / +{pos['opt_trg_pct']:.1f}% | Spot Trg: {pos['spot_target']})\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📐 <b>Gann Pivot:</b> <code>{int(pos['gann_level'])}</code> | ⛽ <b>Fuel Left:</b> <code>{pos['rem_fuel_pts']:.1f} pts</code>\n"
            f"🧬 <b>Live Greeks & OI Data:</b>\n"
            f"• <b>Delta (Δ):</b> <code>{pos['entry_delta']:.2f}</code> | <b>IV:</b> <code>{pos['entry_iv']:.1f}%</code> | <b>Theta:</b> <code>{pos['entry_theta']:.1f}</code>\n"
            f"• <b>Strike OI:</b> <code>{pos['strike_oi']:,}</code> | <b>Chg OI:</b> <code>{pos['change_oi']:+,}</code> | <b>Day ΔPCR:</b> <code>{pos['delta_pcr']:.2f}</code>\n"
            f"• <b>Fut Build-up:</b> <code>{pos['fut_buildup']}</code> | <b>Max Pain:</b> <code>{pos['max_pain']}</code>\n"
            f"📍 <b>{pos['index_code']} Spot:</b> <code>{pos['spot_px']:.2f}</code> | <b>India VIX:</b> <code>{pos['india_vix']}</code>\n"
            f"⚡ <b>सेटअप:</b> <code>{pos['setup_type']}</code>\n"
            f"⏱️ <b>वेळ:</b> <code>{pos['entry_time']}</code>"
        )
        send_telegram_chart_alert(pos["index_code"], df_5m, pos["gann_level"], pos["spot_sl"], pos["spot_target"], caption)

    def send_periodic_update(self, pos: dict, current_ltp: float):
        cur_pts = round(current_ltp - pos["entry_px"], 2)
        cur_pnl = round(cur_pts * pos["lot_size"], 2)
        pnl_symbol = "🟢" if cur_pnl >= 0 else "🔴"

        msg = (
            f"⏱️ <b>[{pos['index_code']}] ट्रेड #{pos['trade_id']} चालू अपडेट (3-Min Ping)</b>\n"
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
            f"{'🎉' if pos['net_pnl'] > 0 else '⚠️'} <b>[{pos['index_code']}] ट्रेड पूर्ण ({status_emoji})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>ट्रेड #{pos['trade_id']}: BUY {pos['clean_name']}</b> (Qty: {pos['lot_size']})\n"
            f"⏱️ <b>कालावधी:</b> <code>{pos['entry_time']} ➡️ {pos['exit_time']}</code>\n"
            f"💵 <b>एन्ट्री:</b> <code>₹{pos['entry_px']:.2f}</code> ➡️ <b>एक्झिट:</b> <code>₹{pos['exit_px']:.2f}</code>\n"
            f"📊 <b>पॉईंट्स:</b> <code>{pos['pts']:+.2f} pts</code>\n"
            f"🛑 <b>कारण:</b> <code>{pos['exit_reason']}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>या ट्रेडचा निव्वळ निकाल:</b> <b>₹ {pos['net_pnl']:+,.2f}</b>\n"
            f"📈 <b>एकूण संचित नफा (All Indices):</b> <b>₹ {self.cumulative_pnl:+,.2f}</b>"
        )
        send_telegram(msg)

    def execute_entry(self, st: dict, side: str, strike: float, ltp: float, instrument_key: str, symbol: str,
                      spot_px: float, gann_level: float, trigger_level: float, raw_spot_sl: float, gann_barrier: float,
                      atr_5m: float, fuel_info: dict, vix_info: dict, delta_pcr: float, max_pain_display: Any, greeks: dict,
                      oi_buildup: str, fut_mem: dict, setup_type: str, df_5m: pd.DataFrame):
        entry_price = ltp
        live_abs_delta = max(0.35, min(0.75, abs(float(greeks.get("delta", 0.52)))))

        atr_opt_sl = atr_5m * 1.2 * live_abs_delta
        min_opt_sl = entry_price * MIN_OPT_SL_PCT
        max_opt_sl = entry_price * MAX_OPT_SL_PCT
        opt_sl_pts = round(min(max_opt_sl, max(min_opt_sl, atr_opt_sl)), 2)

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

        st["active_position"] = pos
        print(f"\n[एन्ट्री ट्रिगर] {clean_name} (Lot:{st['lot_size']}) @ ₹{entry_price} | Setup:{setup_type} | SL: ₹{sl} | TRG: ₹{trg}", flush=True)
        self.send_entry_alert(pos, df_5m)

    def execute_exit(self, st: dict, exit_price: float, reason: str):
        pos = st["active_position"]
        if not pos:
            return
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
            st["failed_strikes"][pos["strike"]] = time.monotonic() + 2700
        else:
            self.consecutive_losses = 0

        if self.cumulative_pnl >= GLOBAL_DAILY_PROFIT_GOAL:
            self.daily_locked = True
            send_telegram(f"🔒 <b>एकत्रित दैनिक उद्दिष्ट पूर्ण!</b>\nसर्व इंडेक्सचा एकूण नफा (₹{self.cumulative_pnl:,.2f}) लॉक केला आहे.")
        elif self.cumulative_pnl <= GLOBAL_DAILY_MAX_LOSS:
            self.daily_locked = True
            send_telegram(f"🛑 <b>मॅक्स लॉस मर्यादा आली!</b>\nदैनिक तोटा मर्यादा (₹{GLOBAL_DAILY_MAX_LOSS:,.0f}) ओलांडली. सिस्टीम सुरक्षित बंद केली आहे.")
        elif self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            self.daily_locked = True
            send_telegram(f"🔒 <b>कॅपिटल प्रोटेक्शन लॉक:</b>\nसलग २ स्टॉपलॉस हिट झाल्याने ट्रेड्स थांबवले आहेत.")

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

            ltp = self.api.get_ltp(pos["instrument_key"])
            if ltp is None:
                continue

            pos["peak_price"] = max(pos["peak_price"], ltp)
            gain = ltp - pos["entry_px"]

            if gain >= pos["opt_sl_pts"] and not pos["runner_mode"]:
                pos["runner_mode"] = True
                pos["sl_px"] = max(pos["sl_px"], pos["entry_px"] + 0.15 * pos["opt_sl_pts"])
                send_telegram(f"🛡️ <b>[{idx_code}] Runner Mode ON: SL ट्रेल केला (₹{pos['sl_px']:.2f})</b>")

            if gain >= (pos["opt_sl_pts"] * 1.5):
                new_sl = round(ltp - (pos["opt_sl_pts"] * 0.5), 2)
                if new_sl > pos["sl_px"]:
                    pos["sl_px"] = new_sl

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

        df_5m = self.api.get_spot_candles_5m(st["spot_key"])
        if df_5m.empty or len(df_5m) < 4:
            return None

        df_5m = add_indicators_5m(df_5m)
        completed_bar = df_5m.iloc[-1]
        prev_bar = df_5m.iloc[-2]
        bar_time_str = completed_bar["dt"].strftime("%H:%M")

        spot_close = float(completed_bar["close"])
        spot_open = float(completed_bar["open"])
        spot_high = float(completed_bar["high"])
        spot_low = float(completed_bar["low"])
        spot_vwap_5m = float(completed_bar["vwap"])
        ema20_5m = float(completed_bar.get("ema20", spot_close))
        adx_val = float(completed_bar.get("adx14", 0.0)) if pd.notna(completed_bar.get("adx14")) else 0.0
        atr_val = float(completed_bar.get("atr14", spot_close * 0.0006)) if pd.notna(completed_bar.get("atr14")) else (spot_close * 0.0006)

        fuel_info = self.calculate_remaining_daily_fuel(st, df_5m, spot_close, vix_info)
        is_green = spot_close > spot_open
        is_red = spot_close < spot_open

        max_pain = self.api.get_official_max_pain(st["spot_key"], st["expiry"])
        max_pain_display = max_pain if max_pain is not None else "N/A"

        atm_strike = round(spot_close / st["strike_step"]) * st["strike_step"]
        mem = self.api.get_live_option_chain_memory(st["spot_key"], st["expiry"], atm_strike, spot_close)
        delta_pcr = mem.get("delta_oi_pcr") or 1.00

        fut_mem = self.api.get_futures_5m_metrics(st["fut_instrument_key"])

        # Gann 0.50 Lattice (उदा. 24964, 25122, 25281)
        gann = get_gann_levels(spot_close, steps_each_side=6)
        sup_level = gann["immediate_support"]
        res_level = gann["immediate_resistance"]
        gann_prox_pts = spot_close * GANN_PROXIMITY_PCT
        gann_retest_pts = spot_close * GANN_RETEST_BAND_PCT
        vwap_band_pts = spot_close * VWAP_TOUCH_BAND_PCT

        near_sup_gann = (spot_low <= sup_level + gann_prox_pts) and (spot_close >= sup_level - gann_prox_pts)
        near_res_gann = (spot_high >= res_level - gann_prox_pts) and (spot_close <= res_level + gann_prox_pts)
        broke_res_gann = (float(prev_bar["close"]) <= res_level) and (spot_close > res_level)
        broke_sup_gann = (float(prev_bar["close"]) >= sup_level) and (spot_close < sup_level)

        if near_sup_gann or broke_sup_gann:
            gann_zone_status = f"NEAR_{int(sup_level)}"
        elif near_res_gann or broke_res_gann:
            gann_zone_status = f"NEAR_{int(res_level)}"
        else:
            gann_zone_status = "PA_SCAN"

        self.update_5m_registers(st, bar_time_str, is_green, is_red, mem, delta_pcr)
        pcr_slope = st["pcr_slope"]

        fut_vwap_status = "N/A"
        if fut_mem.get("valid"):
            fut_vwap_status = "Above 🟢" if fut_mem.get("above_fut_vwap") else "Below 🔴"

        st["latest_summary"] = {
            "lot_size": st["lot_size"],
            "spot": spot_close,
            "ema20": ema20_5m,
            "sup": sup_level,
            "res": res_level,
            "gann_zone": gann_zone_status,
            "delta_pcr": delta_pcr,
            "pcr_slope": pcr_slope,
            "adx": adx_val,
            "max_pain": max_pain_display,
            "fuel_pct": fuel_info["fuel_left_pct_of_day"],
            "fut_vwap_status": fut_vwap_status,
            "vol_ratio": fut_mem.get("fut_vol_ratio", 1.0),
            "atr": atr_val
        }

        if st["active_position"] or self.daily_locked:
            return bar_time_str
        if st["cooldown_until"] and time.monotonic() < st["cooldown_until"]:
            return bar_time_str
        if self.count_active_positions() >= MAX_SIMULTANEOUS_POSITIONS:
            return bar_time_str
        if len(df_5m) < 6 or fuel_info["remaining_pct"] < MIN_REMAINING_FUEL_PCT:
            return bar_time_str
        if st["buildup_code"] in ("BULL_TRAP", "BEAR_TRAP"):
            st["pending_setup"] = None
            return bar_time_str

        # =========================================================================
        # SENIOR ANALYST CONFLUENCE ENGINE (4 PROVEN SETUPS):
        # 1) Gann 0.50 Lattice Breakout / Retest (24964, 25122, 25281...)
        # 2) PA Swing Momentum Breakdown / Breakout (10:50 AM सारखी मोठी चाल)
        # 3) PA Rejection Wick Reversal at Day Extreme (14:20 PM सारखा रिव्हर्सल)
        # 4) SMC VWAP Rejection + PCR Velocity Slope
        # =========================================================================
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

        # मागील ३ कॅन्डल्सचा Swing High / Low आणि दिवसाचा High / Low
        prior_3 = df_5m.iloc[-4:-1]
        swing_3_high = float(prior_3["high"].max())
        swing_3_low = float(prior_3["low"].min())
        day_high_so_far = float(df_5m["high"].max())
        day_low_so_far = float(df_5m["low"].min())

        swing_pad_pts = round(spot_close * 0.0002, 2)
        retest_buf_pts = round(0.28 * atr_val, 2)

        if not st["pending_setup"]:
            # ---------------------- 🟢 CE SETUPS ----------------------
            # A. Gann 0.50 Lattice Setup
            gann_sup_bounce_ce = is_green and is_body_solid and (spot_low <= sup_level + gann_prox_pts) and (spot_close >= sup_level - (gann_prox_pts * 0.5))
            gann_res_breakout_ce = is_green and is_body_solid and (float(prev_bar["close"]) <= res_level) and (spot_close > res_level)
            has_gann_ce = (fut_vol_surge or is_range_expansion) and (gann_sup_bounce_ce or gann_res_breakout_ce) and (spot_close > spot_vwap_5m) and above_fut_vwap

            # B. Price Action Swing Momentum Breakout (EMA20 + VWAP + ADX >= 18 + 3-Bar High Break)
            has_pa_momentum_ce = (
                is_green and is_body_solid and (adx_val >= MIN_ADX_MOMENTUM)
                and (spot_close > spot_vwap_5m) and above_fut_vwap and (spot_close > ema20_5m)
                and (spot_close > swing_3_high) and (fut_vol_surge or is_range_expansion)
            )

            # C. Price Action Bottom Rejection Reversal (14:20 PM सारखा Intraday Low वर मोठी खालची विक + EMA20 क्रॉस)
            near_day_low = (spot_low <= day_low_so_far + (0.35 * atr_val))
            has_pa_reversal_ce = (
                is_green and near_day_low and (lower_wick_ratio >= MIN_REJECTION_WICK_RATIO)
                and (spot_close > ema20_5m) and (delta_pcr >= 0.65 or pcr_slope > 0)
            )

            # D. SMC VWAP Bounce / Early PCR Velocity
            touched_vwap_ce = is_green and is_body_solid and (spot_low <= spot_vwap_5m + vwap_band_pts) and (spot_close > spot_vwap_5m) and above_fut_vwap
            has_vwap_or_pcr_ce = (delta_pcr > 1.15 and touched_vwap_ce) or (
                is_green and is_body_solid and (pcr_slope >= PCR_SLOPE_BULL_THRESHOLD) and (float(prev_bar["close"]) <= spot_vwap_5m) and (spot_close > spot_vwap_5m)
            )

            if has_gann_ce or has_pa_momentum_ce or has_pa_reversal_ce or has_vwap_or_pcr_ce:
                if has_gann_ce:
                    setup_name = f"{idx_code}_GANN_{int(res_level if gann_res_breakout_ce else sup_level)}_CE"
                    anchor_gann = res_level if gann_res_breakout_ce else sup_level
                    next_barrier = gann["target_ce"] if gann_res_breakout_ce else res_level
                    needs_retest = True
                elif has_pa_momentum_ce:
                    setup_name = f"{idx_code}_PA_SWING_MOMENTUM_CE"
                    anchor_gann = sup_level
                    next_barrier = res_level
                    needs_retest = False
                elif has_pa_reversal_ce:
                    setup_name = f"{idx_code}_PA_BOTTOM_REVERSAL_CE"
                    anchor_gann = sup_level
                    next_barrier = res_level
                    needs_retest = False
                else:
                    setup_name = f"{idx_code}_SMC_VWAP_PCR_CE"
                    anchor_gann = sup_level
                    next_barrier = res_level
                    needs_retest = False

                st["pending_setup"] = {
                    "side": "CE",
                    "setup_type": setup_name,
                    "oi_buildup": st["buildup_code"],
                    "gann_level": anchor_gann,
                    "gann_barrier": next_barrier,
                    "breakout_level": spot_high,
                    "retest_min": round(spot_high - retest_buf_pts, 2),
                    "trigger_level": round(spot_high + (spot_close * ENTRY_BUFFER_PCT), 2),
                    "retest_touched": (not needs_retest),
                    "raw_spot_sl": round(spot_low - swing_pad_pts, 2),
                    "atr_5m": atr_val,
                    "armed_time": time.monotonic()
                }
                print(f"[!] [{idx_code}] CE Armed: {setup_name} | Trigger @ {st['pending_setup']['trigger_level']}", flush=True)

            # ---------------------- 🔴 PE SETUPS ----------------------
            else:
                # A. Gann 0.50 Lattice Setup
                gann_res_reject_pe = is_red and is_body_solid and (spot_high >= res_level - gann_prox_pts) and (spot_close <= res_level + (gann_prox_pts * 0.5))
                gann_sup_breakdown_pe = is_red and is_body_solid and (float(prev_bar["close"]) >= sup_level) and (spot_close < sup_level)
                has_gann_pe = (fut_vol_surge or is_range_expansion) and (gann_res_reject_pe or gann_sup_breakdown_pe) and (spot_close < spot_vwap_5m) and below_fut_vwap

                # B. Price Action Swing Momentum Breakdown (10:50 AM सारखा VWAP + EMA20 + 3-Bar Swing Low तुटल्यावर)
                has_pa_momentum_pe = (
                    is_red and is_body_solid and (adx_val >= MIN_ADX_MOMENTUM)
                    and (spot_close < spot_vwap_5m) and below_fut_vwap and (spot_close < ema20_5m)
                    and (spot_close < swing_3_low) and (fut_vol_surge or is_range_expansion)
                )

                # C. Price Action Top Rejection Reversal (Intraday High वर मोठी वरची विक + EMA20 खाली क्लोज)
                near_day_high = (spot_high >= day_high_so_far - (0.35 * atr_val))
                has_pa_reversal_pe = (
                    is_red and near_day_high and (upper_wick_ratio >= MIN_REJECTION_WICK_RATIO)
                    and (spot_close < ema20_5m) and (delta_pcr <= 1.25 or pcr_slope < 0)
                )

                # D. SMC VWAP Rejection / Early PCR Velocity
                touched_vwap_pe = is_red and is_body_solid and (spot_high >= spot_vwap_5m - vwap_band_pts) and (spot_close < spot_vwap_5m) and below_fut_vwap
                has_vwap_or_pcr_pe = (delta_pcr < 0.85 and touched_vwap_pe) or (
                    is_red and is_body_solid and (pcr_slope <= PCR_SLOPE_BEAR_THRESHOLD) and (float(prev_bar["close"]) >= spot_vwap_5m) and (spot_close < spot_vwap_5m)
                )

                if has_gann_pe or has_pa_momentum_pe or has_pa_reversal_pe or has_vwap_or_pcr_pe:
                    if has_gann_pe:
                        setup_name = f"{idx_code}_GANN_{int(sup_level if gann_sup_breakdown_pe else res_level)}_PE"
                        anchor_gann = sup_level if gann_sup_breakdown_pe else res_level
                        next_barrier = gann["target_pe"] if gann_sup_breakdown_pe else sup_level
                        needs_retest = True
                    elif has_pa_momentum_pe:
                        setup_name = f"{idx_code}_PA_SWING_MOMENTUM_PE"
                        anchor_gann = res_level
                        next_barrier = sup_level
                        needs_retest = False
                    elif has_pa_reversal_pe:
                        setup_name = f"{idx_code}_PA_TOP_REVERSAL_PE"
                        anchor_gann = res_level
                        next_barrier = sup_level
                        needs_retest = False
                    else:
                        setup_name = f"{idx_code}_SMC_VWAP_PCR_PE"
                        anchor_gann = res_level
                        next_barrier = sup_level
                        needs_retest = False

                    st["pending_setup"] = {
                        "side": "PE",
                        "setup_type": setup_name,
                        "oi_buildup": st["buildup_code"],
                        "gann_level": anchor_gann,
                        "gann_barrier": next_barrier,
                        "breakout_level": spot_low,
                        "retest_max": round(spot_low + retest_buf_pts, 2),
                        "trigger_level": round(spot_low - (spot_close * ENTRY_BUFFER_PCT), 2),
                        "retest_touched": (not needs_retest),
                        "raw_spot_sl": round(spot_high + swing_pad_pts, 2),
                        "atr_5m": atr_val,
                        "armed_time": time.monotonic()
                    }
                    print(f"[!] [{idx_code}] PE Armed: {setup_name} | Trigger @ {st['pending_setup']['trigger_level']}", flush=True)

        # STAGE 2: TICK TRIGGER CONFIRMATION & LIVE ENTRY
        if st["pending_setup"]:
            setup = st["pending_setup"]
            if time.monotonic() - setup["armed_time"] > 900:
                st["pending_setup"] = None
                return bar_time_str

            current_spot_tick = self.api.get_ltp(st["spot_key"]) or spot_close

            if setup["side"] == "CE":
                if setup["retest_min"] <= current_spot_tick <= setup["breakout_level"]:
                    setup["retest_touched"] = True
                if current_spot_tick < setup["raw_spot_sl"]:
                    st["pending_setup"] = None
                    return bar_time_str
                triggered = setup["retest_touched"] and (current_spot_tick >= setup["trigger_level"])
            else:
                if setup["breakout_level"] <= current_spot_tick <= setup["retest_max"]:
                    setup["retest_touched"] = True
                if current_spot_tick > setup["raw_spot_sl"]:
                    st["pending_setup"] = None
                    return bar_time_str
                triggered = setup["retest_touched"] and (current_spot_tick <= setup["trigger_level"])

            if triggered:
                atm_strike = round(current_spot_tick / st["strike_step"]) * st["strike_step"]

                if atm_strike in st["failed_strikes"]:
                    if time.monotonic() < st["failed_strikes"][atm_strike]:
                        st["pending_setup"] = None
                        return bar_time_str
                    else:
                        del st["failed_strikes"][atm_strike]

                fresh_mem = self.api.get_live_option_chain_memory(st["spot_key"], st["expiry"], atm_strike, current_spot_tick)
                opt_greeks = fresh_mem["ce"] if setup["side"] == "CE" else fresh_mem["pe"]

                live_iv = float(opt_greeks.get("iv", 0.0))
                live_abs_delta = abs(float(opt_greeks.get("delta", 0.52)))
                max_iv_limit = vix_info.get("max_iv", MAX_ALLOWED_IV)

                if live_iv > max_iv_limit or live_abs_delta < MIN_ABS_DELTA:
                    st["pending_setup"] = None
                    return bar_time_str

                contract_info = st["contracts_cache"].get((atm_strike, setup["side"]))
                if not contract_info:
                    return bar_time_str

                inst_key = contract_info["instrument_key"]
                trad_sym = contract_info["trading_symbol"]
                ltp = opt_greeks.get("ltp") or self.api.get_ltp(inst_key)

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
            self.send_consolidated_5m_pulse(latest_bar_time, vix_info)


# ========================= MAIN =========================
def main():
    global TOKEN
    if not TOKEN:
        TOKEN = fetch_token()

    if not TOKEN:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN उपलब्ध नाही. कृपया ACCESS_TOKEN.txt तपासा.")

    api = UpstoxClient(TOKEN)
    timings = get_live_market_session_timings()
    trader = MultiIndexPaperTrader(api, timings)

    lot_summary = " | ".join([f"{k}:{trader.states[k]['lot_size']}" for k in ACTIVE_INDICES])
    send_telegram(
        f"🟢 <b>5-INDEX SENIOR ANALYST CONFLUENCE ALGO: सुरू झाले</b>\n"
        f"तारीख: <code>{TODAY_STR}</code>\n"
        f"इंडेक्सेस: <b>NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX</b>\n"
        f"Dynamic Lots: <code>{lot_summary}</code>\n"
        f"सेटअप्स: <b>Gann 0.50 Lattice + PA Swing Momentum + Bottom/Top Reversal + VWAP/PCR</b>"
    )
    print(f"[*] Senior Analyst 5-Index Engine Active ({timings['session_start']} - {timings['entry_end']}) for {TODAY_STR}...", flush=True)

    last_strat = 0.0
    try:
        while True:
            hhmm = now_ist().strftime("%H:%M")
            if hhmm >= timings["market_end"]:
                print(f"[*] {hhmm}: Market session संपले. Live Engine थांबवत आहे...", flush=True)
                send_telegram(f"⏱️ <b>मार्केट सेशन समाप्त ({hhmm}):</b>\nआजचे 5-Index इंजिन सुरक्षित बंद केले आहे.")
                break

            trader.check_all_positions_management()

            if timings["session_start"] <= hhmm <= timings["entry_end"]:
                if time.monotonic() - last_strat >= STRATEGY_INTERVAL:
                    trader.run_all_indices_scan()
                    last_strat = time.monotonic()

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n[!] Manually stopped.", flush=True)
        send_telegram("⚠️ <b>5-INDEX ALGO इंजिन: मॅन्युअली थांबवले</b>")


if __name__ == "__main__":
    main()
