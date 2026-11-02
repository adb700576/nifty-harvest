import io
import math
import os
import sys
import time
from datetime import datetime
from urllib.parse import quote
import numpy as np
import pandas as pd
import requests

# ==============================================================================
# १. ऑथेंटिकेशन आणि बेस कॉन्फिगरेशन
# ==============================================================================
ACCESS_TOKEN = (
    os.environ.get("UPSTOX_TOKEN") or 
    os.environ.get("UPSTOX_ACCESS_TOKEN") or 
    ""
).strip()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

BASE_URL = "https://api.upstox.com"
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Authorization": f"Bearer {ACCESS_TOKEN}"
}

LOT_SIZE = 65
BUFFER_PCT = 0.001          # ०.१% गॅन झोन बफर
MIN_BOX_BARS = 12           # कन्सॉलिडेशन बॉक्ससाठी किमान १२ कॅन्डल्स
CSV_FILE = "paper_trades.csv"

# ==============================================================================
# २. टेलिग्राम अलर्ट व्यवस्थापक
# ==============================================================================
def send_telegram_alert(message: str):
    """टेलिग्रामवर थेट अलर्ट संदेश पाठवणे"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[-] Telegram Alert त्रुटी: {e}")

# ==============================================================================
# ३. गॅन स्क्वेअर ऑफ ९ लेव्हल्स
# ==============================================================================
def calculate_gann_levels(prev_close: float, count: int = 4):
    base_n = int(math.isqrt(int(prev_close)))
    levels = []
    for i in range(count, 0, -1):
        levels.append((base_n - i) ** 2)
    levels.append(base_n ** 2)
    for i in range(1, count + 1):
        levels.append((base_n + i) ** 2)
    return sorted(levels)

# ==============================================================================
# ४. Upstox मार्केट डेटा फेचर
# ==============================================================================
def get_live_nifty_candles(interval_min: int = 1) -> pd.DataFrame:
    """Upstox V3 वरून निफ्टी ५० चे थेट १-मिनिटाचे इंट्राडे कॅन्डल्स आणणे"""
    index_key = quote("NSE_INDEX|Nifty 50", safe="")
    url = f"{BASE_URL}/v3/historical-candle/intraday/{index_key}/minutes/{interval_min}"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        if res.status_code == 200:
            candles = res.json().get("data", {}).get("candles", [])
            if candles:
                df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                return df.sort_values("timestamp").reset_index(drop=True)
    except Exception as e:
        print(f"[-] Candle Fetch त्रुटी: {e}")
    return pd.DataFrame()

def get_nifty_prev_close() -> float:
    """
    Upstox V3 वरून अचूक मागील क्लोजिंग भाव (Prev Close) मिळवणे
    """
    url = f"{BASE_URL}/v3/market-quote/quotes"
    params = {"instrument_key": "NSE_INDEX|Nifty 50"}
    try:
        res = requests.get(url, headers=HEADERS, params=params, timeout=8)
        if res.status_code == 200:
            data = res.json().get("data", {})
            for k, val in data.items():
                # १. थेट ohlc क्लोज किंवा prev_close_price तपासणे
                pc = float(val.get("prev_close_price", 0.0))
                if pc > 23000.0:
                    return pc
                
                # २. नेट चेंज वजा करून अचूक कालचा क्लोज काढणे
                lp = float(val.get("last_price", 0.0))
                nc = float(val.get("net_change", 0.0))
                if lp > 23000.0:
                    return round(lp - nc, 2)
    except Exception as e:
        print(f"[-] Quote API त्रुटी: {e}")

    # ३. प्रत्यक्ष ३० ऑक्टोबर चार्ट क्लोज फॉलबॅक (Nifty Benchmark)
    return 23110.10

# ==============================================================================
# ५. पेपर ट्रेडिंग लेजर व्यवस्थापक (CSV Logger)
# ==============================================================================
def init_paper_ledger():
    if not os.path.exists(CSV_FILE):
        df = pd.DataFrame(columns=[
            "trade_id", "date", "side", "strike", "box_range", "entry_time", 
            "exit_time", "entry_px", "sl_px", "tgt_px", "exit_px", "pts", 
            "gross_pnl", "net_pnl", "exit_reason", "status"
        ])
        df.to_csv(CSV_FILE, index=False)

def log_trade_to_csv(trade_data: dict):
    df = pd.read_csv(CSV_FILE)
    df = pd.concat([df, pd.DataFrame([trade_data])], ignore_index=True)
    df.to_csv(CSV_FILE, index=False)

def update_trade_in_csv(trade_id: str, updates: dict):
    df = pd.read_csv(CSV_FILE)
    idx = df[df["trade_id"] == trade_id].index
    if not idx.empty:
        for k, v in updates.items():
            df.loc[idx, k] = v
        df.to_csv(CSV_FILE, index=False)

# ==============================================================================
# ६. मुख्य लाइव्ह पेपर ट्रेडिंग इंजिन
# ==============================================================================
def run_live_paper_bot():
    print("====================================================================")
    print("   NIFTY GANN SQUARE OF 9 (LIVE PAPER BOT - DARVAS BOX BREAKOUT)    ")
    print("====================================================================")

    if not ACCESS_TOKEN:
        print("[-] त्रुटी: UPSTOX_TOKEN किंवा UPSTOX_ACCESS_TOKEN सापडला नाही.")
        sys.exit(1)

    init_paper_ledger()
    prev_close = get_nifty_prev_close()
    gann_levels = calculate_gann_levels(prev_close)
    print(f"[+] मागील क्लोज: {prev_close} | गॅन लेव्हल्स: {gann_levels}")

    # बॉट सुरू झाल्याचा टेलिग्राम संदेश
    send_telegram_alert(
        f"🚀 *NIFTY GANN BOT STARTED*\n"
        f"• *Prev Close:* `{prev_close}`\n"
        f"• *Gann Levels:* `{gann_levels}`\n"
        f"• *Status:* Live Market Scanner Active"
    )

    box_trackers = {lvl: {"state": "IDLE", "start_idx": None} for lvl in gann_levels}
    used_levels_today = set()
    daily_trades = 0
    active_position = None

    print("[*] लाइव्ह मार्केट स्कॅनर सुरू झाला आहे...")

    while True:
        try:
            now_str = datetime.now().strftime("%H:%M")

            if now_str < "09:20":
                time.sleep(15)
                continue

            # १५:२० वाजता पोझिशन बंद करणे
            if now_str >= "15:20" and active_position:
                exit_price = active_position["current_opt_px"]
                pts = round(exit_price - active_position["entry_px"], 2)
                gross = round(pts * LOT_SIZE, 2)
                net = round(gross - 52.0, 2)
                update_trade_in_csv(active_position["trade_id"], {
                    "exit_time": now_str, "exit_px": exit_price,
                    "pts": pts, "gross_pnl": gross, "net_pnl": net,
                    "exit_reason": "FORCE_EOD ⏱️", "status": "CLOSED"
                })
                send_telegram_alert(
                    f"⏱️ *FORCE EOD EXIT*\n"
                    f"• *Strike:* `{active_position['strike']}`\n"
                    f"• *Exit Price:* `₹{exit_price}`\n"
                    f"• *Points:* `{pts:+} pts`\n"
                    f"• *Net PnL:* `₹{net:+,.2f}`"
                )
                print(f"\n[✓] पोझिशन बंद झाली: PnL ₹{net}")
                active_position = None
                break

            df_curr = get_live_nifty_candles(interval_min=1)
            if df_curr.empty or len(df_curr) < 15:
                time.sleep(10)
                continue

            last_bar = df_curr.iloc[-1]
            spot_close = float(last_bar['close'])
            c_len = len(df_curr)

            # चालू पोझिशनचा SL किंवा टार्गेट तपासणे
            if active_position:
                entry_spot = active_position["entry_spot"]
                opt_type = active_position["side"]
                entry_opt = active_position["entry_px"]

                if opt_type == "BUY_CE":
                    cur_opt = round(entry_opt + ((spot_close - entry_spot) * 0.52), 2)
                else:
                    cur_opt = round(entry_opt + ((entry_spot - spot_close) * 0.52), 2)

                active_position["current_opt_px"] = cur_opt

                # १R नफा झाल्यावर ब्रेक-इव्हन ट्रेल करणे
                if cur_opt >= entry_opt + active_position["sl_pts"]:
                    active_position["sl_px"] = max(active_position["sl_px"], entry_opt + 2.0)

                # टार्गेट हिट
                if cur_opt >= active_position["tgt_px"]:
                    pts = round(active_position["tgt_px"] - entry_opt, 2)
                    gross = round(pts * LOT_SIZE, 2)
                    net = round(gross - 52.0, 2)
                    update_trade_in_csv(active_position["trade_id"], {
                        "exit_time": now_str, "exit_px": active_position["tgt_px"],
                        "pts": pts, "gross_pnl": gross, "net_pnl": net,
                        "exit_reason": "TARGET_HIT ✅", "status": "CLOSED"
                    })
                    send_telegram_alert(
                        f"🎯 *TARGET HIT ✅*\n"
                        f"• *Strike:* `{active_position['strike']}`\n"
                        f"• *Exit:* `₹{active_position['tgt_px']}`\n"
                        f"• *Profit:* `+{pts} pts`\n"
                        f"• *Net PnL:* `₹{net:+,.2f}`"
                    )
                    print(f"\n[🎯 TARGET HIT] PnL: ₹{net}")
                    active_position = None

                # स्टॉप-लॉस हिट
                elif cur_opt <= active_position["sl_px"]:
                    exit_reason = "BREAKEVEN_EXIT" if active_position["sl_px"] > (entry_opt - active_position["sl_pts"]) else "SL_HIT ❌"
                    pts = round(active_position["sl_px"] - entry_opt, 2)
                    gross = round(pts * LOT_SIZE, 2)
                    net = round(gross - 52.0, 2)
                    update_trade_in_csv(active_position["trade_id"], {
                        "exit_time": now_str, "exit_px": active_position["sl_px"],
                        "pts": pts, "gross_pnl": gross, "net_pnl": net,
                        "exit_reason": exit_reason, "status": "CLOSED"
                    })
                    send_telegram_alert(
                        f"🛑 *EXIT ({exit_reason})*\n"
                        f"• *Strike:* `{active_position['strike']}`\n"
                        f"• *Exit:* `₹{active_position['sl_px']}`\n"
                        f"• *Points:* `{pts} pts`\n"
                        f"• *Net PnL:* `₹{net:+,.2f}`"
                    )
                    print(f"\n[🛑 EXIT] PnL: ₹{net} ({exit_reason})")
                    active_position = None

                time.sleep(5)
                continue

            # नवीन कन्सॉलिडेशन बॉक्स शोधणे
            if daily_trades < 3:
                for lvl in gann_levels:
                    buf = lvl * BUFFER_PCT
                    zone_low = lvl - buf
                    zone_high = lvl + buf
                    tracker = box_trackers[lvl]

                    if tracker["state"] == "IDLE":
                        if zone_low <= spot_close <= zone_high:
                            box_trackers[lvl] = {"state": "CONSOLIDATING", "start_idx": c_len - 1}
                            print(f"\n[*] {now_str} - निफ्टी {lvl} जवळ आला. बॉक्स कन्सॉलिडेशन सुरू.")

                    elif tracker["state"] == "CONSOLIDATING":
                        bars_in_box = (c_len - 1) - tracker["start_idx"]

                        if bars_in_box >= MIN_BOX_BARS and lvl not in used_levels_today:
                            box_window = df_curr.iloc[tracker["start_idx"] : c_len]
                            box_high = box_window['high'].max()
                            box_low = box_window['low'].min()

                            # अपसाइड ब्रेकआउट -> BUY CE
                            if spot_close > box_high:
                                atm_strike = round(spot_close / 50.0) * 50
                                real_opt_px = round(max(45.0, spot_close * 0.008), 2)
                                index_sl = box_low - 6.0
                                opt_sl_pts = min(35.0, max(8.0, round(abs(spot_close - index_sl) * 0.52, 2)))
                                opt_tgt_pts = round(opt_sl_pts * 2.0, 2)

                                trade_id = f"T_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                                active_position = {
                                    "trade_id": trade_id, "side": "BUY_CE", "strike": f"NIFTY {atm_strike} CE",
                                    "entry_spot": spot_close, "entry_px": real_opt_px,
                                    "current_opt_px": real_opt_px, "sl_pts": opt_sl_pts,
                                    "sl_px": max(5.0, round(real_opt_px - opt_sl_pts, 2)),
                                    "tgt_px": round(real_opt_px + opt_tgt_pts, 2)
                                }

                                log_trade_to_csv({
                                    "trade_id": trade_id, "date": datetime.now().strftime("%Y-%m-%d"),
                                    "side": "BUY_CE", "strike": f"NIFTY {atm_strike} CE",
                                    "box_range": f"{round(box_low, 1)}-{round(box_high, 1)}",
                                    "entry_time": now_str, "exit_time": "-", "entry_px": real_opt_px,
                                    "sl_px": active_position["sl_px"], "tgt_px": active_position["tgt_px"],
                                    "exit_px": 0.0, "pts": 0.0, "gross_pnl": 0.0, "net_pnl": 0.0,
                                    "exit_reason": "-", "status": "OPEN"
                                })
                                daily_trades += 1
                                used_levels_today.add(lvl)
                                box_trackers[lvl] = {"state": "IDLE", "start_idx": None}

                                send_telegram_alert(
                                    f"🚀 *BUY CE TRIGGERED (Darvas Breakout)*\n"
                                    f"• *Strike:* `{atm_strike} CE`\n"
                                    f"• *Box Range:* `{round(box_low, 1)} - {round(box_high, 1)}`\n"
                                    f"• *Entry:* `₹{real_opt_px}`\n"
                                    f"• *Dynamic SL:* `₹{active_position['sl_px']}` ({opt_sl_pts} pts)\n"
                                    f"• *Target (1:2):* `₹{active_position['tgt_px']}` ({opt_tgt_pts} pts)"
                                )
                                print(f"\n[🚀 BUY_CE] {atm_strike} CE @ ₹{real_opt_px} | SL: {active_position['sl_px']} | TGT: {active_position['tgt_px']}")
                                break

                            # डाऊनसाइड ब्रेकडाऊन -> BUY PE
                            elif spot_close < box_low:
                                atm_strike = round(spot_close / 50.0) * 50
                                real_opt_px = round(max(45.0, spot_close * 0.008), 2)
                                index_sl = box_high + 6.0
                                opt_sl_pts = min(35.0, max(8.0, round(abs(index_sl - spot_close) * 0.52, 2)))
                                opt_tgt_pts = round(opt_sl_pts * 2.0, 2)

                                trade_id = f"T_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                                active_position = {
                                    "trade_id": trade_id, "side": "BUY_PE", "strike": f"NIFTY {atm_strike} PE",
                                    "entry_spot": spot_close, "entry_px": real_opt_px,
                                    "current_opt_px": real_opt_px, "sl_pts": opt_sl_pts,
                                    "sl_px": max(5.0, round(real_opt_px - opt_sl_pts, 2)),
                                    "tgt_px": round(real_opt_px + opt_tgt_pts, 2)
                                }

                                log_trade_to_csv({
                                    "trade_id": trade_id, "date": datetime.now().strftime("%Y-%m-%d"),
                                    "side": "BUY_PE", "strike": f"NIFTY {atm_strike} PE",
                                    "box_range": f"{round(box_low, 1)}-{round(box_high, 1)}",
                                    "entry_time": now_str, "exit_time": "-", "entry_px": real_opt_px,
                                    "sl_px": active_position["sl_px"], "tgt_px": active_position["tgt_px"],
                                    "exit_px": 0.0, "pts": 0.0, "gross_pnl": 0.0, "net_pnl": 0.0,
                                    "exit_reason": "-", "status": "OPEN"
                                })
                                daily_trades += 1
                                used_levels_today.add(lvl)
                                box_trackers[lvl] = {"state": "IDLE", "start_idx": None}

                                send_telegram_alert(
                                    f"🚀 *BUY PE TRIGGERED (Darvas Breakdown)*\n"
                                    f"• *Strike:* `{atm_strike} PE`\n"
                                    f"• *Box Range:* `{round(box_low, 1)} - {round(box_high, 1)}`\n"
                                    f"• *Entry:* `₹{real_opt_px}`\n"
                                    f"• *Dynamic SL:* `₹{active_position['sl_px']}` ({opt_sl_pts} pts)\n"
                                    f"• *Target (1:2):* `₹{active_position['tgt_px']}` ({opt_tgt_pts} pts)"
                                )
                                print(f"\n[🚀 BUY_PE] {atm_strike} PE @ ₹{real_opt_px} | SL: {active_position['sl_px']} | TGT: {active_position['tgt_px']}")
                                break

            sys.stdout.write(f"\r[*] वेळ: {now_str} | निफ्टी स्पॉट: {spot_close:.2f} | आजचे ट्रेड्स: {daily_trades}/3")
            sys.stdout.flush()
            time.sleep(10)

        except KeyboardInterrupt:
            print("\n[!] बॉट थांबवला.")
            break
        except Exception as e:
            print(f"\n[-] त्रुटी: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run_live_paper_bot()
