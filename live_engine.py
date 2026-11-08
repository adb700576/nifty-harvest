import os
import time
import math
import json
import csv
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Any, Tuple
import requests
import pandas as pd
import numpy as np

# ========================= OPTIONAL MATPLOTLIB IMPORT =========================
try:
    import matplotlib
    matplotlib.use("Agg")  # हेडलेस बॅकएंड
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False
    print("[-] Matplotlib सापडले नाही. व्हिज्युअल चार्ट ऐवजी साधा टेक्स्ट अलर्ट वापरला जाईल.", flush=True)

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

LOT_SIZE = 65
MIN_OPT_SL_PTS = 10.0
TARGET_RRR = 1.80

POLL_INTERVAL = 1.0               # १-सेकंद फास्ट टिक मॉनिटर
STRATEGY_INTERVAL = 15.0           # ५-मिनिट स्कॅनर
POSITION_UPDATE_INTERVAL = 180.0   # ३-मिनिट PnL अपडेट

# कॅपिटल प्रोटेक्शन
DAILY_PROFIT_GOAL = 1500.0
DAILY_MAX_LOSS = -1500.0
MAX_CONSECUTIVE_LOSSES = 2

# अँटी-चॉपी थ्रेशोल्ड्स
MIN_ADX_TREND = 18.0
MIN_VOLUME_RATIO = 1.15
MIN_BODY_RATIO = 0.45
ENTRY_BUFFER_PCT = 0.00025         # ०.०२५% बफर (~६.२५ पॉईंट्स @ २५,०००)

JOURNAL_DIR = Path("live_journal")
JOURNAL_DIR.mkdir(exist_ok=True)
TODAY_STR = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")
JOURNAL_CSV = JOURNAL_DIR / f"trades_journal_{TODAY_STR}.csv"


# ========================= TELEGRAM & VISUAL ALERTS =========================
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
    # जर matplotlib नसेल किंवा कॅन्डल डेटा कमी असेल तर थेट साधा टेक्स्ट मेसेज पाठवणे
    if not HAS_MATPLOTLIB or df_5m.empty or len(df_5m) < 5:
        send_telegram(caption_text)
        return

    try:
        recent = df_5m.tail(25)
        plt.figure(figsize=(10, 5))
        plt.plot(recent['dt'], recent['close'], label='NIFTY 5M Spot', color='black', linewidth=1.5)
        
        plt.axhline(y=gann_level, color='blue', linestyle='--', label=f'Gann Pivot: {gann_level:.1f}')
        plt.axhline(y=sl_price, color='red', linestyle=':', label=f'Swing SL: {sl_price:.1f}')
        plt.axhline(y=trg_price, color='purple', linestyle=':', label=f'Target: {trg_price:.1f}')
        
        plt.title("NIFTY 5M Gann Setup Alert", fontsize=12, fontweight='bold')
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
    except Exception as e:
        print(f"[-] Chart Alert Error: {e}. साधा मेसेज पाठवत आहे...", flush=True)
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


# ========================= GANN SQUARE OF 9 LEVELS =========================
def get_gann_levels(current_price: float, count: int = 5) -> dict:
    base_root = int(math.floor(math.sqrt(current_price)))
    
    lower_levels = [((base_root - i) ** 2) for i in range(count, 0, -1)]
    base_level = [base_root ** 2]
    upper_levels = [((base_root + i) ** 2) for i in range(1, count + 1)]
    
    all_levels = sorted(lower_levels + base_level + upper_levels)
    
    supports = [lvl for lvl in all_levels if lvl <= current_price]
    resistances = [lvl for lvl in all_levels if lvl > current_price]
    
    immediate_sup = max(supports) if supports else current_price - 100.0
    immediate_res = min(resistances) if resistances else current_price + 100.0
    
    next_res = [lvl for lvl in resistances if lvl > immediate_res]
    next_sup = [lvl for lvl in supports if lvl < immediate_sup]
    
    return {
        "all_levels": all_levels,
        "immediate_support": immediate_sup,
        "immediate_resistance": immediate_res,
        "target_ce": next_res[0] if next_res else immediate_res + 300.0,
        "target_pe": next_sup[-1] if next_sup else immediate_sup - 300.0
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

    def get_spot_candles_1m(self) -> pd.DataFrame:
        try:
            url = "/v2/historical-candle/intraday/NSE_INDEX%7CNifty%2050/1minute"
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
        df_1m = self.get_spot_candles_1m()
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

    def get_official_charges(self, instrument_key: str, qty: int, buy_price: float, sell_price: float) -> dict:
        url = "/v2/charges/brokerage"
        total_brokerage = 0.0
        total_stt = 0.0
        total_other = 0.0

        try:
            b_res = self.get(url, {
                "instrument_token": instrument_key,
                "quantity": str(qty),
                "product": "I",
                "transaction_type": "BUY",
                "price": str(buy_price)
            })
            b_chg = b_res.get("data", {}).get("charges", {})
            total_brokerage += float(b_chg.get("brokerage", 20.0))
            total_stt += float(b_chg.get("stt", 0.0))
            total_other += float(b_chg.get("gst", 0.0)) + float(b_chg.get("stamp_duty", 0.0)) + float(b_chg.get("other_charges", 0.0))

            s_res = self.get(url, {
                "instrument_token": instrument_key,
                "quantity": str(qty),
                "product": "I",
                "transaction_type": "SELL",
                "price": str(sell_price)
            })
            s_chg = s_res.get("data", {}).get("charges", {})
            total_brokerage += float(s_chg.get("brokerage", 20.0))
            total_stt += float(s_chg.get("stt", 0.0))
            total_other += float(s_chg.get("gst", 0.0)) + float(s_chg.get("other_charges", 0.0))

            tot = round(total_brokerage + total_stt + total_other, 2)
            if tot > 0:
                return {"total_charges": tot, "brokerage": round(total_brokerage, 2), "stt": round(total_stt, 2)}
        except Exception:
            pass

        return {"total_charges": 52.0, "brokerage": 40.0, "stt": 6.5}

    def get_official_pcr(self, expiry: str) -> Optional[float]:
        try:
            params = {
                "instrument_key": "NSE_INDEX|Nifty 50",
                "expiry": expiry,
                "date": TODAY_STR,
                "bucket_interval": 15
            }
            res = self.get("/v2/market/pcr", params=params)
            data = res.get("data", {})
            insights = data.get("insights", [])
            if insights:
                return round(float(insights[-1].get("pcr", data.get("pcr", 0.0))), 2)
            val = data.get("pcr")
            return round(float(val), 2) if val is not None else None
        except Exception:
            return None

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

    def get_strike_oi_and_change(self, expiry: str, target_strike: float) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
        call_oi, put_oi = None, None
        call_chg, put_chg = None, None
        try:
            res_oi = self.get("/v2/market/oi", {
                "instrument_key": "NSE_INDEX|Nifty 50",
                "expiry": expiry,
                "date": TODAY_STR
            })
            for item in res_oi.get("data", {}).get("call_put_oi_data_list", []):
                if float(item.get("strike_price", 0)) == target_strike:
                    call_oi = int(item.get("call_oi", 0))
                    put_oi = int(item.get("put_oi", 0))
                    break

            res_chg = self.get("/v2/market/change-oi", {
                "instrument_key": "NSE_INDEX|Nifty 50",
                "expiry": expiry,
                "date": TODAY_STR,
                "interval": 1
            })
            for item in res_chg.get("data", {}).get("call_put_oi_data_list", []):
                if float(item.get("strike_price", 0)) == target_strike:
                    call_chg = int(item.get("call_change_oi", 0))
                    put_chg = int(item.get("put_change_oi", 0))
                    break
        except Exception:
            pass
        return call_oi, put_oi, call_chg, put_chg


# ========================= DYNAMIC TIMINGS & STATUS =========================
def get_live_market_session_timings(api: UpstoxClient, target_date: str) -> dict:
    try:
        res = api.get(f"/v2/market/timings/{target_date}")
        exchanges = res.get("data", [])
        nfo_nse = next((item for item in exchanges if item.get("exchange") in ["NFO", "NSE"]), None)
        
        if nfo_nse:
            start_ms = nfo_nse["start_time"]
            end_ms = nfo_nse["end_time"]
            
            start_dt = datetime.fromtimestamp(start_ms / 1000.0, timezone.utc) + timedelta(hours=5, minutes=30)
            end_dt = datetime.fromtimestamp(end_ms / 1000.0, timezone.utc) + timedelta(hours=5, minutes=30)
            
            session_start = (start_dt + timedelta(minutes=5)).strftime("%H:%M")
            entry_end     = (end_dt - timedelta(minutes=15)).strftime("%H:%M")
            force_exit    = (end_dt - timedelta(minutes=4)).strftime("%H:%M")
            market_end    = end_dt.strftime("%H:%M")
            
            print(f"[+] [Market Timings API] अधिकृत सत्र: {start_dt.strftime('%H:%M')} ते {market_end} IST", flush=True)
            return {
                "session_start": session_start,
                "entry_end": entry_end,
                "force_exit": force_exit,
                "market_end": market_end,
                "raw_start": start_dt.strftime("%H:%M"),
                "raw_end": market_end
            }
    except Exception as e:
        print(f"[-] Market Timings API त्रुटी: {e}. फॉलबॅक मुहूर्त वेळा वापरत आहे...", flush=True)

    # मुहूर्त ट्रेडिंग फॉलबॅक
    return {
        "session_start": "14:25",
        "entry_end": "15:15",
        "force_exit": "15:26",
        "market_end": "15:30",
        "raw_start": "14:20",
        "raw_end": "15:30"
    }

def is_exchange_open(api: UpstoxClient) -> bool:
    try:
        res = api.get("/v2/market/status/NSE")
        status = res.get("data", {}).get("status", "")
        return status == "NORMAL_OPEN"
    except Exception:
        return True


# ========================= TECHNICAL INDICATORS & ANTI-CHOP =========================
def add_indicators_5m(df_5m: pd.DataFrame) -> pd.DataFrame:
    x = df_5m.copy()
    if x.empty:
        return x
    
    c = x["close"]
    prev_close = c.shift(1)
    
    tp = (x["high"] + x["low"] + c) / 3.0
    v = x["volume"].replace(0, 1.0)
    x["vwap"] = (tp * v).cumsum() / v.cumsum()
    
    vol_mean = x["volume"].rolling(20).mean().replace(0, 1.0)
    x["volume_ratio"] = x["volume"] / vol_mean
    
    tr = pd.concat([
        x["high"] - x["low"],
        (x["high"] - prev_close).abs(),
        (x["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    x["atr14"] = tr.rolling(14).mean()
    
    up = x["high"].diff()
    dn = -x["low"].diff()
    plus = up.where((up > dn) & (up > 0), 0.0).rolling(14).mean()
    minus = dn.where((dn > up) & (dn > 0), 0.0).rolling(14).mean()
    dx = ((plus - minus).abs() / (plus + minus).replace(0, np.nan)) * 100.0
    x["adx14"] = dx.rolling(14).mean()
    
    x["ema20"] = c.ewm(span=20, adjust=False).mean()
    x["ema50"] = c.ewm(span=50, adjust=False).mean()
    
    return x

def is_market_choppy(df_5m: pd.DataFrame, pcr: float) -> Tuple[bool, str]:
    if len(df_5m) < 15:
        return True, "INSUFFICIENT_DATA"

    last_bar = df_5m.iloc[-1]
    adx = safe_float(last_bar.get("adx14"))
    
    if adx is not None and adx < MIN_ADX_TREND:
        return True, f"CHOPPY_REGIME_ADX_LOW ({adx:.1f} < {MIN_ADX_TREND})"

    if 0.86 <= pcr <= 0.94:
        return True, f"NEUTRAL_DEAD_ZONE_PCR ({pcr:.2f})"

    recent_6 = df_5m.tail(6)
    rng_30m = recent_6["high"].max() - recent_6["low"].min()
    atr = safe_float(last_bar.get("atr14")) or 15.0
    if rng_30m < (1.3 * atr):
        return True, f"RANGE_COMPRESSION_CHOP ({rng_30m:.1f} pts < {1.3*atr:.1f})"

    return False, "TRENDING_CLEAR"


# ========================= LIVE TRADING ENGINE =========================
class LivePaperTrader:
    def __init__(self, api: UpstoxClient, timings: dict):
        self.api = api
        self.timings = timings
        self.active_position = None
        self.trade_counter = 0
        self.cumulative_pnl = 0.0
        self.consecutive_losses = 0
        self.cooldown_until = None
        self.daily_locked = False
        self.expiry = None
        self.contracts_cache = {}
        self.failed_strikes = {}
        self.pending_gann_setup = None
        self.load_expiry_and_contracts()

    def load_expiry_and_contracts(self):
        try:
            url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
            df = pd.read_csv(url, compression="gzip", low_memory=False)
            df.columns = [c.strip().lower() for c in df.columns]
            sym_col = "tradingsymbol" if "tradingsymbol" in df.columns else "trading_symbol"

            nifty_opts = df[(df["exchange"] == "NSE_FO") & (df["name"] == "NIFTY") & (df["instrument_type"] == "OPTIDX")].copy()
            nifty_opts["expiry"] = pd.to_datetime(nifty_opts["expiry"]).dt.strftime("%Y-%m-%d")
            valid = sorted([x for x in nifty_opts["expiry"].unique() if x >= TODAY_STR])
            self.expiry = valid[0]
            print(f"[+] निवडलेली चालू एक्सपायरी: {self.expiry}", flush=True)

            active_df = nifty_opts[nifty_opts["expiry"] == self.expiry].copy()
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
            print(f"[+] {len(self.contracts_cache)} चालू आठवड्याचे ऑप्शन कॉन्ट्रॅक्ट्स मॅप झाले.", flush=True)
        except Exception as e:
            print(f"[-] Contracts loading error: {e}", flush=True)
            self.expiry = TODAY_STR

    def log_journal_csv(self, row: dict):
        exists = JOURNAL_CSV.exists()
        with open(JOURNAL_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            if not exists:
                writer.writeheader()
            writer.writerow(row)

    def send_entry_alert(self, pos: dict, df_5m: pd.DataFrame):
        caption = (
            f"🚀 <b>NIFTY 5M GANN ALGO: नवीन ट्रेड एन्ट्री (#{pos['trade_id']})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>ऑर्डर:</b> <b>BUY {pos['clean_name']}</b>\n"
            f"🏷️ <b>सिम्बॉल:</b> <code>{pos['symbol']}</code>\n"
            f"📦 <b>लॉट:</b> <code>1 Lot</code> (Qty: <code>{LOT_SIZE}</code>)\n"
            f"💵 <b>खरेदी किंमत:</b> <code>₹{pos['entry_px']:.2f}</code>\n"
            f"🛑 <b>स्टॉपलॉस (5M Low):</b> <code>₹{pos['sl_px']:.2f}</code> (-{pos['opt_sl_pts']:.1f} pts)\n"
            f"🏁 <b>टार्गेट:</b> <code>₹{pos['trg_px']:.2f}</code> (+{pos['opt_trg_pts']:.1f} pts)\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 <b>5-Min Setup Details:</b>\n"
            f"📍 <b>NIFTY Spot:</b> <code>{pos['spot_px']:.2f}</code> (Gann Pivot: {pos['gann_level']:.1f})\n"
            f"🎯 <b>Trigger (0.025% Buffer):</b> <code>₹{pos['trigger_level']:.2f}</code>\n"
            f"📊 <b>PCR:</b> <code>{pos['pcr']:.2f}</code> | <b>Max Pain:</b> <code>{pos['max_pain']}</code>\n"
            f"📈 <b>स्ट्राइक OI:</b> <code>{pos['strike_oi']:,}</code> | <b>ΔOI:</b> <code>{pos['change_oi']:+,}</code>\n"
            f"⚡ <b>सेटअप:</b> <code>{pos['setup_type']}</code>\n"
            f"⏱️ <b>वेळ:</b> <code>{pos['entry_time']}</code>"
        )
        send_telegram_chart_alert(
            df_5m, 
            gann_level=pos["gann_level"], 
            sl_price=pos["spot_sl"], 
            trg_price=pos["spot_target"], 
            caption_text=caption
        )

    def send_periodic_update(self, pos: dict, current_ltp: float):
        cur_pts = round(current_ltp - pos["entry_px"], 2)
        cur_pnl = round(cur_pts * LOT_SIZE, 2)
        pnl_symbol = "🟢" if cur_pnl >= 0 else "🔴"

        msg = (
            f"⏱️ <b>ट्रेड #{pos['trade_id']} चालू अपडेट (3-Min Ping)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷️ <b>{pos['clean_name']} @ ₹{current_ltp:.2f}</b>\n"
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
            f"📌 <b>ट्रेड #{pos['trade_id']}: BUY {pos['clean_name']}</b>\n"
            f"⏱️ <b>कालावधी:</b> <code>{pos['entry_time']} ➡️ {pos['exit_time']}</code>\n"
            f"💵 <b>एन्ट्री:</b> <code>₹{pos['entry_px']:.2f}</code> ➡️ <b>एक्झिट:</b> <code>₹{pos['exit_px']:.2f}</code>\n"
            f"📊 <b>पॉईंट्स:</b> <code>{pos['pts']:+.2f} pts</code>\n"
            f"🛑 <b>कारण:</b> <code>{pos['exit_reason']}</code>\n"
            f"💳 <b>Upstox Charges (Brokerage+Taxes):</b> <code>₹{pos['charges']:.2f}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>या ट्रेडचा निव्वळ निकाल:</b> <b>₹ {pos['net_pnl']:+,.2f}</b>\n"
            f"📈 <b>एकूण संचित नफा (Cumulative PnL):</b> <b>₹ {self.cumulative_pnl:+,.2f}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📝 <i>दैनिक ट्रेड जर्नलमध्ये जतन केले आहे.</i>"
        )
        send_telegram(msg)

    def execute_entry(self, side: str, strike: float, ltp: float, instrument_key: str, symbol: str,
                      spot_px: float, gann_level: float, trigger_level: float, spot_sl: float, spot_target: float,
                      pcr: float, max_pain: float, strike_oi: int, chg_oi: int, setup_type: str, df_5m: pd.DataFrame):
        entry_price = ltp
        delta = 0.52
        
        spot_risk = abs(spot_px - spot_sl)
        opt_sl_pts = round(max(MIN_OPT_SL_PTS, spot_risk * delta), 2)
        opt_trg_pts = round(opt_sl_pts * TARGET_RRR, 2)

        sl = max(0.05, round(entry_price - opt_sl_pts, 2))
        trg = round(entry_price + opt_trg_pts, 2)
        self.trade_counter += 1

        clean_name = f"{int(strike)} {side}"

        self.active_position = {
            "trade_id": self.trade_counter,
            "side": f"BUY_{side}",
            "clean_name": clean_name,
            "symbol": symbol,
            "instrument_key": instrument_key,
            "strike": strike,
            "entry_time": ts_now(),
            "entry_px": entry_price,
            "opt_sl_pts": opt_sl_pts,
            "opt_trg_pts": opt_trg_pts,
            "sl_px": sl,
            "trg_px": trg,
            "spot_px": spot_px,
            "gann_level": gann_level,
            "trigger_level": trigger_level,
            "spot_sl": spot_sl,
            "spot_target": spot_target,
            "pcr": pcr,
            "max_pain": max_pain if max_pain else "N/A",
            "strike_oi": strike_oi,
            "change_oi": chg_oi,
            "setup_type": setup_type,
            "runner_mode": False,
            "peak_price": entry_price,
            "last_periodic_update": time.monotonic()
        }

        print(f"\n[एन्ट्री ट्रिगर] {clean_name} @ ₹{entry_price} | SL: ₹{sl} | TRG: ₹{trg}", flush=True)
        self.send_entry_alert(self.active_position, df_5m)

    def execute_exit(self, exit_price: float, reason: str):
        pos = self.active_position
        pts = round(exit_price - pos["entry_px"], 2)
        gross_pnl = round(pts * LOT_SIZE, 2)

        chg_info = self.api.get_official_charges(pos["instrument_key"], LOT_SIZE, pos["entry_px"], exit_price)
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

        if self.cumulative_pnl >= DAILY_PROFIT_GOAL:
            self.daily_locked = True
            send_telegram(f"🔒 <b>दैनिक उद्दिष्ट पूर्ण!</b>\nदिवसाचा नफा (₹{self.cumulative_pnl:,.2f}) लॉक केला आहे. आजचे नवीन ट्रेड्स थांबवले आहेत.")
        elif self.cumulative_pnl <= DAILY_MAX_LOSS:
            self.daily_locked = True
            send_telegram(f"🛑 <b>मॅक्स लॉस मर्यादा आली!</b>\nदैनिक तोटा मर्यादा (₹{self.cumulative_pnl:,.2f}) ओलांडली. सिस्टीम सुरक्षित बंद केली आहे.")
        elif self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            self.daily_locked = True
            send_telegram(f"🔒 <b>कॅपिटल प्रोटेक्शन लॉक:</b>\nसलग २ स्टॉपलॉस हिट झाल्याने उर्वरित सत्रासाठी ट्रेड्स थांबवले आहेत.")

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

        # De-risking: 1R नफ्यावर स्टॉपलॉस खरेदी किमतीवर ट्रेल
        if gain >= pos["opt_sl_pts"] and not pos["runner_mode"]:
            pos["runner_mode"] = True
            pos["sl_px"] = max(pos["sl_px"], pos["entry_px"] + 0.15 * pos["opt_sl_pts"])
            send_telegram(f"🛡️ <b>Runner Mode ON: SL ट्रेल केला (₹{pos['sl_px']:.2f})</b> | {pos['clean_name']}")

        # डायनॅमिक ट्रेलिंग
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
        if len(df_5m) < 15:
            return

        df_5m = add_indicators_5m(df_5m)
        completed_bar = df_5m.iloc[-1]
        prev_bar = df_5m.iloc[-2]

        spot_close = float(completed_bar["close"])
        spot_open = float(completed_bar["open"])
        spot_high = float(completed_bar["high"])
        spot_low = float(completed_bar["low"])
        vwap_5m = float(completed_bar["vwap"])
        vol_ratio = float(completed_bar["volume_ratio"])

        pcr = self.api.get_official_pcr(self.expiry)
        max_pain = self.api.get_official_max_pain(self.expiry)

        if pcr is None or max_pain is None:
            return

        # चॉपी मार्केट फिल्टर
        choppy, chop_reason = is_market_choppy(df_5m, pcr)
        if choppy:
            if self.pending_gann_setup:
                self.pending_gann_setup = None
            return

        gann = get_gann_levels(spot_close, count=5)
        sup_level = gann["immediate_support"]
        res_level = gann["immediate_resistance"]

        # मध्यभागी नो-ट्रेड झोन (दोन लेव्हल्सच्या मधोमध ३५% अंतरावर असल्यास एन्ट्री ब्लॉक)
        gap = res_level - sup_level
        if (spot_close - sup_level > 0.35 * gap) and (res_level - spot_close > 0.35 * gap):
            return

        c_range = max(spot_high - spot_low, 1e-9)
        c_body = abs(spot_close - spot_open)
        body_ratio = c_body / c_range

        if body_ratio < MIN_BODY_RATIO:
            return

        buffer_pts = round(spot_close * ENTRY_BUFFER_PCT, 2)
        is_green = spot_close > spot_open
        is_red = spot_close < spot_open

        bullish_sentiment = (pcr >= 0.95) and (spot_close > vwap_5m) and (vol_ratio >= MIN_VOLUME_RATIO)
        bearish_sentiment = (pcr <= 0.85) and (spot_close < vwap_5m) and (vol_ratio >= MIN_VOLUME_RATIO)

        # ५-मिनिट कॅन्डलवर गॅन पॅटर्न ट्रिगर तयार करणे
        if not self.pending_gann_setup:
            if bullish_sentiment and is_green:
                bottom_sweep_ce = (spot_low <= sup_level + 15.0) and (spot_close >= sup_level - 10.0)
                retest_ce = (float(prev_bar["high"]) > sup_level) and (sup_level - 15.0 <= spot_low <= sup_level + 35.0)

                if bottom_sweep_ce or retest_ce:
                    trigger_px = round(spot_high + buffer_pts, 2)
                    self.pending_gann_setup = {
                        "side": "CE",
                        "setup_type": "BOTTOM_BOUNCE_CE" if bottom_sweep_ce else "5M_RETEST_CE",
                        "gann_level": sup_level,
                        "trigger_level": trigger_px,
                        "spot_sl": round(spot_low - 5.0, 2),
                        "spot_target": res_level,
                        "armed_time": time.monotonic()
                    }
                    print(f"[!] 5M CE Armed: Trigger @ {trigger_px} (0.025% Buffer)", flush=True)

            elif bearish_sentiment and is_red:
                top_sweep_pe = (spot_high >= res_level - 15.0) and (spot_close <= res_level + 10.0)
                retest_pe = (float(prev_bar["low"]) < res_level) and (res_level - 35.0 <= spot_high <= res_level + 15.0)

                if top_sweep_pe or retest_pe:
                    trigger_px = round(spot_low - buffer_pts, 2)
                    self.pending_gann_setup = {
                        "side": "PE",
                        "setup_type": "TOP_REJECT_PE" if top_sweep_pe else "5M_RETEST_PE",
                        "gann_level": res_level,
                        "trigger_level": trigger_px,
                        "spot_sl": round(spot_high + 5.0, 2),
                        "spot_target": sup_level,
                        "armed_time": time.monotonic()
                    }
                    print(f"[!] 5M PE Armed: Trigger @ {trigger_px} (0.025% Buffer)", flush=True)

        # चालू टिकवर ०.०२५% बफर पार झाला का ते पाहणे
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
                atm_strike = round(current_spot_tick / 50.0) * 50.0

                if atm_strike in self.failed_strikes:
                    if time.monotonic() < self.failed_strikes[atm_strike]:
                        self.pending_gann_setup = None
                        return
                    else:
                        del self.failed_strikes[atm_strike]

                call_oi, put_oi, call_chg, put_chg = self.api.get_strike_oi_and_change(self.expiry, atm_strike)
                target_chg_oi = call_chg if setup["side"] == "CE" else put_chg
                target_strike_oi = call_oi if setup["side"] == "CE" else put_oi

                if target_chg_oi is not None and target_chg_oi < 0:
                    self.pending_gann_setup = None
                    return

                contract_info = self.contracts_cache.get((atm_strike, setup["side"]))
                if not contract_info:
                    return

                inst_key = contract_info["instrument_key"]
                trad_sym = contract_info["trading_symbol"]
                ltp = self.api.get_ltp(inst_key)

                if ltp and 80.0 <= ltp <= 350.0:
                    self.execute_entry(
                        side=setup["side"],
                        strike=atm_strike,
                        ltp=ltp,
                        instrument_key=inst_key,
                        symbol=trad_sym,
                        spot_px=current_spot_tick,
                        gann_level=setup["gann_level"],
                        trigger_level=setup["trigger_level"],
                        spot_sl=setup["spot_sl"],
                        spot_target=setup["spot_target"],
                        pcr=pcr,
                        max_pain=max_pain,
                        strike_oi=target_strike_oi or 0,
                        chg_oi=target_chg_oi or 0,
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
    
    # थेट Upstox API वरून आजचे मार्केट टायमिंग्स मिळवणे
    timings = get_live_market_session_timings(api, TODAY_STR)
    
    trader = LivePaperTrader(api, timings)

    send_telegram(
        f"🟢 <b>NIFTY 5M GANN ALGO: सुरू झाले</b>\n"
        f"तारीख: <code>{TODAY_STR}</code>\n"
        f"सत्र वेळ: <b>{timings['raw_start']} ते {timings['raw_end']} IST</b>\n"
        f"ट्रेडिंग विंडो: <code>{timings['session_start']} ➡️ {timings['entry_end']}</code>\n"
        f"पद्धत: <b>Gann 9 + 0.025% Buffer + Anti-Chop Guard</b>"
    )
    print(f"[*] 5M Gann Engine Active ({timings['raw_start']} - {timings['raw_end']}) for {TODAY_STR}...", flush=True)

    last_strat = 0.0
    try:
        while True:
            hhmm = now_ist().strftime("%H:%M")
            
            # सेशन संपताच सुरक्षित बंद
            if hhmm >= timings["market_end"]:
                print(f"[*] {hhmm}: Market session संपले. Live Engine सुरक्षित थांबवत आहे...", flush=True)
                send_telegram(f"⏱️ <b>मार्केट सेशन समाप्त ({hhmm}):</b>\nआजचे इंजिन सुरक्षित बंद केले आहे.")
                break

            trader.check_position_management()

            # स्कॅनर फक्त अधिकृत वेळेतच चालेल
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
