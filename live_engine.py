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

# ========================= कॉन्फिगरेशन व टोकन लोडर =========================
BASE_URL = "https://api.upstox.com"

def fetch_token() -> str:
    """टोकन एन्व्हायर्नमेंट किंवा सर्व संभाव्य फाईल पाथवरून शोधणे"""
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if token:
        return token

    possible_paths = [
        "ACCESS_TOKEN.txt",
        Path(__file__).resolve().parent / "ACCESS_TOKEN.txt",
        Path.cwd() / "ACCESS_TOKEN.txt"
    ]

    for p in possible_paths:
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
STRATEGY_INTERVAL = 30.0  # ३० सेकंद सेटअप स्कॅनिंग

DAILY_PROFIT_GOAL = 600.0   # दैनिक नफा लॉक मर्यादा (₹)
DAILY_MAX_LOSS = -1500.0    # कमाल दैनिक तोटा मर्यादा (₹)

SESSION_START = "09:20"
ENTRY_END = "15:00"
FORCE_EXIT = "15:20"

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

    def get_option_chain(self, expiry: str) -> list[dict]:
        try:
            data = self.get("/v2/option/chain", {
                "instrument_key": "NSE_INDEX|Nifty 50",
                "expiry_date": expiry
            })
            return data.get("data", [])
        except Exception:
            return []


# ========================= लाईव्ह ट्रेडिंग इंजिन =========================
class LivePaperTrader:
    def __init__(self, api: UpstoxClient):
        self.api = api
        self.active_position = None
        self.trade_counter = 0
        self.cumulative_pnl = 0.0
        self.cooldown_until = None
        self.daily_locked = False
        self.expiry = None
        self.load_expiry()

    def load_expiry(self):
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
        except Exception as e:
            print(f"[-] एक्सपायरी लोड एरर: {e}", flush=True)
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
            f"🚀 <b>NIFTY ALGO: नवीन ट्रेड एन्ट्री</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>ट्रेड #{pos['trade_id']}: {pos['side']}</b>\n"
            f"🏷️ <b>कॉन्ट्रॅक्ट:</b> <code>{pos['symbol']}</code>\n"
            f"📍 <b>NIFTY स्पॉट:</b> <code>{pos['spot_px']:.2f}</code>\n"
            f"💵 <b>खरेदी किंमत:</b> <code>₹{pos['entry_px']:.2f}</code>\n"
            f"🛑 <b>स्टॉपलॉस:</b> <code>₹{pos['sl_px']:.2f}</code> (-{MIN_OPT_SL_PTS} pts)\n"
            f"🏁 <b>टार्गेट (१:२):</b> <code>₹{pos['trg_px']:.2f}</code> (+{MIN_OPT_SL_PTS*TARGET_RRR} pts)\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>PCR:</b> <code>{pos['pcr']:.2f}</code> | <b>ΔOI:</b> <code>{pos['change_oi']:+,}</code>\n"
            f"⏱️ <b>वेळ:</b> <code>{pos['entry_time']}</code>\n"
            f"💡 <i>सेटअप: VWAP + PCR Confluence</i>"
        )
        send_telegram(msg)

    def send_exit_alert(self, pos: dict):
        status_emoji = "🟢 नफा" if pos["net_pnl"] > 0 else "🔴 तोटा"
        msg = (
            f"{'🎉' if pos['net_pnl'] > 0 else '⚠️'} <b>NIFTY ALGO: ट्रेड पूर्ण ({status_emoji})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>ट्रेड #{pos['trade_id']}: {pos['side']}</b> | <code>{pos['symbol']}</code>\n"
            f"⏱️ <b>कालावधी:</b> <code>{pos['entry_time']} ➡️ {pos['exit_time']}</code>\n"
            f"💵 <b>एन्ट्री:</b> <code>₹{pos['entry_px']:.2f}</code> ➡️ <b>एक्झिट:</b> <code>₹{pos['exit_px']:.2f}</code>\n"
            f"📊 <b>पॉईंट्स:</b> <code>{pos['pts']:+.2f} pts</code>\n"
            f"🛑 <b>कारण:</b> <code>{pos['exit_reason']}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>या ट्रेडचा निव्वळ निकाल:</b> <b>₹ {pos['net_pnl']:+,.2f}</b>\n"
            f"📈 <b>एकूण संचित नफा (Cumulative PnL):</b> <b>₹ {self.cumulative_pnl:+,.2f}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📝 <i>दैनिक ट्रेड जर्नलमध्ये जतन केले आहे.</i>"
        )
        send_telegram(msg)

    def execute_entry(self, side: str, opt_data: dict, spot_px: float, pcr: float):
        entry_price = opt_data["ltp"]
        sl = max(0.05, round(entry_price - MIN_OPT_SL_PTS, 2))
        trg = round(entry_price + (MIN_OPT_SL_PTS * TARGET_RRR), 2)
        self.trade_counter += 1

        self.active_position = {
            "trade_id": self.trade_counter,
            "side": f"BUY_{side}",
            "symbol": opt_data["trading_symbol"],
            "instrument_key": opt_data["instrument_key"],
            "strike": opt_data["strike"],
            "entry_time": ts_now(),
            "entry_px": entry_price,
            "sl_px": sl,
            "trg_px": trg,
            "spot_px": spot_px,
            "pcr": pcr,
            "change_oi": opt_data.get("change_oi", 0),
            "peak_price": entry_price
        }

        print(f"\n[एन्ट्री ट्रिगर] {self.active_position['side']} @ ₹{entry_price}", flush=True)
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

        print(f"\n[एक्झिट ट्रिगर] {pos['side']} Exit @ ₹{exit_price} | PnL: ₹{net_pnl} ({reason})", flush=True)
        self.send_exit_alert(pos)
        self.log_journal_csv(pos)

        if self.cumulative_pnl >= DAILY_PROFIT_GOAL:
            self.daily_locked = True
            send_telegram(f"🔒 <b>दैनिक टार्गेट पूर्ण!</b>\nलक्ष्यांक (₹{self.cumulative_pnl:,.2f}) पूर्ण झाला. आजचे नवीन ट्रेड्स थांबवले आहेत.")
        elif self.cumulative_pnl <= DAILY_MAX_LOSS:
            self.daily_locked = True
            send_telegram(f"🛑 <b>मॅक्स लॉस मर्यादा आली!</b>\nदैनिक तोटा मर्यादा (₹{self.cumulative_pnl:,.2f}) आली आहे. सिस्टीम बंद केली आहे.")

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

        if gain >= 12.0 and pos["sl_px"] < pos["entry_px"]:
            pos["sl_px"] = pos["entry_px"]
            send_telegram(f"🛡️ <b>SL कॉस्टवर आणला (₹{pos['entry_px']:.2f})</b> | ट्रेड #{pos['trade_id']}")

        if gain >= 18.0:
            new_sl = round(ltp - 8.0, 2)
            if new_sl > pos["sl_px"]:
                pos["sl_px"] = new_sl

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

        if abs(spot_px - vwap) > 75.0:
            return

        chain_rows = self.api.get_option_chain(self.expiry)
        if not chain_rows:
            return

        pe_oi_sum = sum([r.get("put_options", {}).get("market_data", {}).get("oi", 0) for r in chain_rows])
        ce_oi_sum = sum([r.get("call_options", {}).get("market_data", {}).get("oi", 0) for r in chain_rows])
        pcr = round(pe_oi_sum / (ce_oi_sum if ce_oi_sum > 0 else 1.0), 2)

        signal_side = None
        if spot_px > vwap and spot_px > ema20 and pcr >= 0.95:
            signal_side = "CE"
        elif spot_px < vwap and spot_px < ema20 and pcr <= 0.85:
            signal_side = "PE"

        if not signal_side:
            return

        atm_strike = round(spot_px / 50.0) * 50.0
        for r in chain_rows:
            if float(r.get("strike_price", 0)) == atm_strike:
                opt = r.get("call_options" if signal_side == "CE" else "put_options", {})
                md = opt.get("market_data", {})
                ltp = float(md.get("ltp", 0))
                oi = int(md.get("oi", 0))
                prev_oi = int(md.get("prev_oi", oi))
                chg_oi = oi - prev_oi

                if 80.0 <= ltp <= 320.0 and chg_oi >= 0:
                    opt_data = {
                        "trading_symbol": opt.get("trading_symbol"),
                        "instrument_key": opt.get("instrument_key"),
                        "strike": atm_strike,
                        "ltp": ltp,
                        "change_oi": chg_oi
                    }
                    self.execute_entry(signal_side, opt_data, spot_px, pcr)
                    break


# ========================= मुख्य लूप =========================
def main():
    global TOKEN
    if not TOKEN:
        TOKEN = fetch_token()

    if not TOKEN:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN उपलब्ध नाही. कृपया ACCESS_TOKEN.txt तपासा.")

    api = UpstoxClient(TOKEN)
    trader = LivePaperTrader(api)

    send_telegram(f"🟢 <b>NIFTY ALGO इंजिन: सुरू झाले</b>\nतारीख: <code>{TODAY_STR}</code>\nमोड: <b>Live Paper Trading + Telegram Journal</b>")
    print(f"[*] Engine Started. Tracking live market for {TODAY_STR}...", flush=True)

    last_strat = 0.0
    try:
        while True:
            trader.check_position_management()

            if time.monotonic() - last_strat >= STRATEGY_INTERVAL:
                trader.run_strategy_scan()
                last_strat = time.monotonic()

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n[!] मॅन्युअली थांबवले.", flush=True)
        send_telegram("⚠️ <b>NIFTY ALGO इंजिन: मॅन्युअली थांबवले</b>")


if __name__ == "__main__":
    main()
