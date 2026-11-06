import os
import time
import math
import json
import csv
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Any
import requests
import pandas as pd
import numpy as np

# ========================= कॉन्फिगरेशन व टोकन =========================
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
                    print(f"[+] Token successfully loaded from file: {p}", flush=True)
                    return content
            except Exception as e:
                print(f"[-] Could not read {p}: {e}", flush=True)
    return ""

TOKEN = fetch_token()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

LOT_SIZE = 65
MIN_OPT_SL_PTS = 12.0
TARGET_RRR = 2.0  # 1:2 RRR (२४ पॉईंट्स)
POLL_INTERVAL = 1.0  # १ सेकंद टिक ट्रॅकिंग
STRATEGY_INTERVAL = 30.0  # ३० सेकंद सेटअप स्कॅन
POSITION_UPDATE_INTERVAL = 180.0  # दर ३ मिनिटांनी लाईव्ह PnL अपडेट

DAILY_PROFIT_GOAL = 1200.0   # दैनिक नफा लॉक मर्यादा (₹)
DAILY_MAX_LOSS = -1500.0    # कमाल दैनिक तोटा मर्यादा (₹)
MAX_CONSECUTIVE_LOSSES = 2   # सलग २ तोट्यांनंतर सर्किट ब्रेक

SESSION_START = "09:20"
ENTRY_END = "15:00"
FORCE_EXIT = "15:20"
MARKET_END = "15:30"

JOURNAL_DIR = Path("live_journal")
JOURNAL_DIR.mkdir(exist_ok=True)
TODAY_STR = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")
JOURNAL_CSV = JOURNAL_DIR / f"trades_journal_{TODAY_STR}.csv"


# ========================= टेलिग्राम मेसेजिंग =========================
def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram Disconnected] {text[:100]}...", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[-] Telegram Error: {e}", flush=True)


def now_ist() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def ts_now() -> str:
    return now_ist().strftime("%Y-%m-%d %H:%M:%S")


# ========================= UPSTOX क्लायंट =========================
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

    def get_spot_candles(self) -> pd.DataFrame:
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

    def get_official_pcr(self, expiry: str) -> Optional[float]:
        """Upstox Official PCR API (सर्व Required Parameters सह)"""
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
        except Exception as e:
            print(f"[-] PCR fetch fail: {e}", flush=True)
            return None

    def get_official_max_pain(self, expiry: str) -> Optional[float]:
        """Upstox Official Max Pain API (सर्व Required Parameters सह)"""
        try:
            params = {
                "instrument_key": "NSE_INDEX|Nifty 50",
                "expiry": expiry,
                "date": TODAY_STR,
                "bucket_interval": 15
            }
            res = self.get("/v2/market/max-pain", params=params)
            data = res.get("data", {})
            val = data.get("max_pain")
            return float(val) if val is not None else None
        except Exception as e:
            print(f"[-] Max Pain fetch fail: {e}", flush=True)
            return None

    def get_strike_oi_and_change(self, expiry: str, target_strike: float) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
        """Upstox Official OI & Change in OI API (स्ट्राइक पातळीवर)"""
        call_oi, put_oi = None, None
        call_chg, put_chg = None, None
        try:
            # 1. Total OI
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

            # 2. Change in OI (Required interval=1)
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
        except Exception as e:
            print(f"[-] OI / Change in OI fetch fail: {e}", flush=True)

        return call_oi, put_oi, call_chg, put_chg


# ========================= लाईव्ह ट्रेडिंग इंजिन =========================
class LivePaperTrader:
    def __init__(self, api: UpstoxClient):
        self.api = api
        self.active_position = None
        self.trade_counter = 0
        self.cumulative_pnl = 0.0
        self.consecutive_losses = 0
        self.cooldown_until = None
        self.daily_locked = False
        self.expiry = None
        self.contracts_cache = {}
        self.failed_strikes = {}  # {strike: unblock_timestamp}
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
            print(f"[+] {len(self.contracts_cache)} चालू आठवड्याचे ऑप्शन कॉन्ट्रॅक्ट्स अचूक मॅप झाले.", flush=True)
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

    def send_entry_alert(self, pos: dict):
        msg = (
            f"🚀 <b>NIFTY ALGO: नवीन ट्रेड एन्ट्री (#{pos['trade_id']})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>ऑर्डर:</b> <b>BUY {pos['clean_name']}</b>\n"
            f"🏷️ <b>ट्रेडिंग सिम्बॉल:</b> <code>{pos['symbol']}</code>\n"
            f"📦 <b>लॉट:</b> <code>1 Lot</code> (एकूण Qty: <code>{LOT_SIZE}</code>)\n"
            f"💵 <b>खरेदी किंमत:</b> <code>₹{pos['entry_px']:.2f}</code>\n"
            f"🛑 <b>स्टॉपलॉस (SL):</b> <code>₹{pos['sl_px']:.2f}</code> (-{MIN_OPT_SL_PTS:.1f} pts)\n"
            f"🏁 <b>टार्गेट (TRG 1:2):</b> <code>₹{pos['trg_px']:.2f}</code> (+{MIN_OPT_SL_PTS*TARGET_RRR:.1f} pts)\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 <b>संस्थात्मक कन्फर्मेशन:</b>\n"
            f"📍 <b>NIFTY Spot:</b> <code>{pos['spot_px']:.2f}</code> (VWAP: {pos['vwap']:.1f} | EMA: {pos['ema20']:.1f})\n"
            f"📊 <b>Official PCR:</b> <code>{pos['pcr']:.2f}</code> | <b>Max Pain:</b> <code>{pos['max_pain']}</code>\n"
            f"📈 <b>स्ट्राइक OI:</b> <code>{pos['strike_oi']:,}</code> | <b>ΔOI:</b> <code>{pos['change_oi']:+,}</code>\n"
            f"⏱️ <b>वेळ:</b> <code>{pos['entry_time']}</code>\n"
            f"💡 <i>कन्फर्मेशन: Strict Multi-Pillar Confluence Validated</i>"
        )
        send_telegram(msg)

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
            f"🏷️ <b>सिम्बॉल:</b> <code>{pos['symbol']}</code>\n"
            f"⏱️ <b>कालावधी:</b> <code>{pos['entry_time']} ➡️ {pos['exit_time']}</code>\n"
            f"💵 <b>एन्ट्री:</b> <code>₹{pos['entry_px']:.2f}</code> ➡️ <b>एक्झिट:</b> <code>₹{pos['exit_px']:.2f}</code>\n"
            f"📊 <b>पॉईंट्स:</b> <code>{pos['pts']:+.2f} pts</code>\n"
            f"🛑 <b>कारण:</b> <code>{pos['exit_reason']}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>या ट्रेडचा निव्वळ निकाल:</b> <b>₹ {pos['net_pnl']:+,.2f}</b>\n"
            f"📈 <b>एकूण संचित नफा (Cumulative PnL):</b> <b>₹ {self.cumulative_pnl:+,.2f}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📝 <i>दैनिक ट्रेड जर्नलमध्ये जतन केले आहे (Trade #{pos['trade_id']}).</i>"
        )
        send_telegram(msg)

    def execute_entry(self, side: str, strike: float, ltp: float, instrument_key: str, symbol: str, spot_px: float, vwap: float, ema20: float, pcr: float, max_pain: float, strike_oi: int, chg_oi: int):
        entry_price = ltp
        sl = max(0.05, round(entry_price - MIN_OPT_SL_PTS, 2))
        trg = round(entry_price + (MIN_OPT_SL_PTS * TARGET_RRR), 2)
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
            "sl_px": sl,
            "trg_px": trg,
            "spot_px": spot_px,
            "vwap": vwap,
            "ema20": ema20,
            "pcr": pcr,
            "max_pain": max_pain if max_pain else "N/A",
            "strike_oi": strike_oi,
            "change_oi": chg_oi,
            "peak_price": entry_price,
            "last_periodic_update": time.monotonic()
        }

        print(f"\n[एन्ट्री ट्रिगर] {clean_name} ({symbol}) @ ₹{entry_price}", flush=True)
        self.send_entry_alert(self.active_position)

    def execute_exit(self, exit_price: float, reason: str):
        pos = self.active_position
        pts = round(exit_price - pos["entry_px"], 2)
        gross_pnl = round(pts * LOT_SIZE, 2)
        net_pnl = round(gross_pnl - 52.0, 2)
        self.cumulative_pnl += net_pnl

        pos["exit_time"] = ts_now()
        pos["exit_px"] = round(exit_price, 2)
        pos["pts"] = pts
        pos["net_pnl"] = net_pnl
        pos["exit_reason"] = reason
        pos["cumulative_pnl"] = self.cumulative_pnl

        print(f"\n[एक्झिट ट्रिगर] {pos['clean_name']} Exit @ ₹{exit_price} | Net PnL: ₹{net_pnl} ({reason})", flush=True)
        self.send_exit_alert(pos)
        self.log_journal_csv(pos)

        # १. सलग तोटा व स्ट्राइक ब्लॅकलिस्ट लॉजिक
        if net_pnl < 0:
            self.consecutive_losses += 1
            # ज्या स्ट्राइकवर तोटा झाला ती ४५ मिनिटे ब्लॉक करा
            self.failed_strikes[pos["strike"]] = time.monotonic() + 2700
            print(f"[!] स्ट्राइक {pos['strike']} ४५ मिनिटांसाठी ब्लॅकलिस्ट केली.", flush=True)
        else:
            self.consecutive_losses = 0

        # २. सुरक्षा सर्किट ब्रेकर
        if self.cumulative_pnl >= DAILY_PROFIT_GOAL:
            self.daily_locked = True
            send_telegram(f"🔒 <b>दैनिक टार्गेट पूर्ण!</b>\nलक्ष्यांक (₹{self.cumulative_pnl:,.2f}) पूर्ण झाला. आजचे नवीन ट्रेड्स थांबवले आहेत.")
        elif self.cumulative_pnl <= DAILY_MAX_LOSS:
            self.daily_locked = True
            send_telegram(f"🛑 <b>मॅक्स लॉस मर्यादा आली!</b>\nदैनिक तोटा मर्यादा (₹{self.cumulative_pnl:,.2f}) आली आहे. सिस्टीम बंद केली आहे.")
        elif self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            self.daily_locked = True
            send_telegram(f"🔒 <b>कॅपिटल प्रोटेक्शन लॉक:</b>\nसलग २ स्टॉपलॉस हिट झाल्याने आजचे उर्वरित ट्रेडिंग पूर्णपणे थांबवले आहे.")

        self.active_position = None
        self.cooldown_until = time.monotonic() + 900

    def check_position_management(self):
        pos = self.active_position
        if not pos:
            return

        ltp = self.api.get_ltp(pos["instrument_key"])
        if ltp is None:
            return

        pos["peak_price"] = max(pos["peak_price"], ltp)
        gain = ltp - pos["entry_px"]

        # डी-रिस्किंग: +१२ पॉईंट्सवर स्टॉपलॉस थेट खरेदी भावावर आणणे
        if gain >= 12.0 and pos["sl_px"] < pos["entry_px"]:
            pos["sl_px"] = pos["entry_px"]
            send_telegram(f"🛡️ <b>SL कॉस्टवर आणला (₹{pos['entry_px']:.2f})</b> | ट्रेड #{pos['trade_id']} ({pos['clean_name']})")

        # ट्रेलिंग स्टॉपलॉस (+१८ पॉईंट्सच्या वर)
        if gain >= 18.0:
            new_sl = round(ltp - 8.0, 2)
            if new_sl > pos["sl_px"]:
                pos["sl_px"] = new_sl

        # दर ३ मिनिटांनी PnL अपडेट पाठवणे
        if time.monotonic() - pos["last_periodic_update"] >= POSITION_UPDATE_INTERVAL:
            self.send_periodic_update(pos, ltp)
            pos["last_periodic_update"] = time.monotonic()

        # प्रत्यक्ष एक्झिट
        hhmm = now_ist().strftime("%H:%M")
        if ltp <= pos["sl_px"]:
            reason = "TRAILED_EXIT ✅" if pos["sl_px"] >= pos["entry_px"] else "SL_HIT ❌"
            self.execute_exit(pos["sl_px"], reason)
        elif ltp >= pos["trg_px"]:
            self.execute_exit(pos["trg_px"], "TARGET_HIT 🎯")
        elif hhmm >= FORCE_EXIT:
            self.execute_exit(ltp, "FORCE_EOD_EXIT ⏱️")

    def run_strategy_scan(self):
        if self.active_position or self.daily_locked:
            return
        if self.cooldown_until and time.monotonic() < self.cooldown_until:
            return

        hhmm = now_ist().strftime("%H:%M")
        if not (SESSION_START <= hhmm <= ENTRY_END):
            return

        df_spot = self.api.get_spot_candles()
        if len(df_spot) < 30:
            return

        df_spot["tp"] = (df_spot["high"] + df_spot["low"] + df_spot["close"]) / 3.0
        v = df_spot["volume"].replace(0, 1.0)
        df_spot["vwap"] = (df_spot["tp"] * v).cumsum() / v.cumsum()
        df_spot["ema20"] = df_spot["close"].ewm(span=20, adjust=False).mean()

        last_bar = df_spot.iloc[-1]
        spot_px = float(last_bar["close"])
        vwap = float(last_bar["vwap"])
        ema20 = float(last_bar["ema20"])

        # ओव्हर-एक्सटेंशन फिल्टर (FOMO टाळणे)
        if abs(spot_px - vwap) > 75.0:
            return

        # Upstox Official Derivative APIs कडून थेट डेटा
        pcr = self.api.get_official_pcr(self.expiry)
        max_pain = self.api.get_official_max_pain(self.expiry)

        # डेटा व्हॅलिडेशन: जर API फेल असेल तर ट्रेड ब्लॉक
        if pcr is None or max_pain is None:
            return

        signal_side = None
        if spot_px > vwap and spot_px > ema20 and pcr >= 0.95:
            if spot_px >= (max_pain - 100.0):
                signal_side = "CE"
        elif spot_px < vwap and spot_px < ema20 and pcr <= 0.85:
            if spot_px <= (max_pain + 100.0):
                signal_side = "PE"

        if not signal_side:
            return

        atm_strike = round(spot_px / 50.0) * 50.0

        # स्ट्राइक ब्लॅकलिस्ट तपासणे (४५ मिनिटांचा कूलडाउन)
        if atm_strike in self.failed_strikes:
            if time.monotonic() < self.failed_strikes[atm_strike]:
                return
            else:
                del self.failed_strikes[atm_strike]

        call_oi, put_oi, call_chg, put_chg = self.api.get_strike_oi_and_change(self.expiry, atm_strike)
        
        # डेटा व्हॅलिडेशन: जर OI डेटा मिळाला नाही तर ट्रेड ब्लॉक
        if call_oi is None or call_chg is None:
            return

        target_chg_oi = call_chg if signal_side == "CE" else put_chg
        target_strike_oi = call_oi if signal_side == "CE" else put_oi

        # अनवाइंडिंग असताना ट्रेड टाळणे आणि किमान लिक्विडिटी असणे
        if target_chg_oi < 0 or target_strike_oi == 0:
            return

        contract_info = self.contracts_cache.get((atm_strike, signal_side))
        if not contract_info:
            return

        inst_key = contract_info["instrument_key"]
        trad_sym = contract_info["trading_symbol"]
        ltp = self.api.get_ltp(inst_key)

        if ltp and 80.0 <= ltp <= 320.0:
            self.execute_entry(
                side=signal_side,
                strike=atm_strike,
                ltp=ltp,
                instrument_key=inst_key,
                symbol=trad_sym,
                spot_px=spot_px,
                vwap=vwap,
                ema20=ema20,
                pcr=pcr,
                max_pain=max_pain,
                strike_oi=target_strike_oi,
                chg_oi=target_chg_oi
            )


# ========================= मुख्य लूप =========================
def main():
    global TOKEN
    if not TOKEN:
        TOKEN = fetch_token()

    if not TOKEN:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN उपलब्ध नाही. कृपया ACCESS_TOKEN.txt तपासा.")

    api = UpstoxClient(TOKEN)
    trader = LivePaperTrader(api)

    send_telegram(
        f"🟢 <b>NIFTY ALGO इंजिन: सुरू झाले</b>\n"
        f"तारीख: <code>{TODAY_STR}</code>\n"
        f"मोड: <b>Live Institutional Tracker (Strict Validation)</b>\n"
        f"सुरक्षा: <b>Max Loss Guard + 2-Loss Lock + Strike Cooldown</b>"
    )
    print(f"[*] Engine Started with Strict Derivative Confluence for {TODAY_STR}...", flush=True)

    last_strat = 0.0
    try:
        while True:
            hhmm = now_ist().strftime("%H:%M")
            if hhmm >= MARKET_END:
                print(f"[*] {hhmm}: Market session sampla. Live Engine safe stop karat ahe...", flush=True)
                send_telegram(f"⏱️ <b>मार्केट सेशन समाप्त ({hhmm}):</b>\nआजचे लाईव्ह ट्रेडिंग इंजिन पूर्ण झाले. ट्रेड जर्नल जतन केले आहे.")
                break

            trader.check_position_management()

            if time.monotonic() - last_strat >= STRATEGY_INTERVAL:
                trader.run_strategy_scan()
                last_strat = time.monotonic()

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n[!] Manually thambavle.", flush=True)
        send_telegram("⚠️ <b>NIFTY ALGO इंजिन: मॅन्युअली थांबवले</b>")


if __name__ == "__main__":
    main()
