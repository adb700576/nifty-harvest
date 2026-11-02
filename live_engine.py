import io
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
import numpy as np
import pandas as pd
import requests

# ==============================================================================
# १. कॉन्फिगरेशन आणि ऑथेंटिकेशन
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

BUFFER_PCT = 0.001          # ०.१% गॅन झोन बफर
MIN_BOX_BARS = 12           # कन्सॉलिडेशन बॉक्ससाठी किमान १२ कॅन्डल्स
CSV_FILE = "paper_trades.csv"

# ==============================================================================
# २. भारतीय वेळ (IST, UTC +5:30)
# ==============================================================================
IST_OFFSET = timezone(timedelta(hours=5, minutes=30))

def get_ist_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(IST_OFFSET)

# ==============================================================================
# ३. टेलिग्राम अलर्ट व्यवस्थापक
# ==============================================================================
def send_telegram_alert(message: str):
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
# ४. गॅन लेव्हल्स कॅल्क्युलेटर
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
# ५. थेट मार्केट डेटा फेचर्स (No Stale Price)
# ==============================================================================
def get_nifty_prev_close_v3() -> float:
    now_ist = get_ist_now()
    to_date = now_ist.strftime("%Y-%m-%d")
    from_date = (now_ist - timedelta(days=7)).strftime("%Y-%m-%d")
    
    inst_key = quote("NSE_INDEX|Nifty 50", safe="")
    url = f"{BASE_URL}/v3/historical-candle/{inst_key}/days/1/{to_date}/{from_date}"
    
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        if res.status_code == 200:
            candles = res.json().get("data", {}).get("candles", [])
            if candles:
                for c in candles:
                    c_date = c[0].split("T")[0]
                    if c_date < to_date:
                        return float(c[4])
                return float(candles[0][4])
    except Exception as e:
        print(f"[-] V3 Daily Candle Fetch त्रुटी: {e}")
    return 0.0

def get_nifty_live_ltp() -> float:
    """थेट टिक-बाय-टिक चालू भाव (Live LTP) मिळवणे"""
    url = f"{BASE_URL}/v3/market-quote/quotes"
    params = {"instrument_key": "NSE_INDEX|Nifty 50"}
    try:
        res = requests.get(url, headers=HEADERS, params=params, timeout=5)
        if res.status_code == 200:
            data = res.json().get("data", {})
            for _, val in data.items():
                return float(val.get("last_price", 0.0))
    except Exception:
        pass
    return 0.0

def get_live_nifty_candles_v3() -> pd.DataFrame:
    """बॉक्स कन्सॉलिडेशन तपासण्यासाठी १-मिनिट कॅन्डल्स"""
    inst_key = quote("NSE_INDEX|Nifty 50", safe="")
    url = f"{BASE_URL}/v3/historical-candle/intraday/{inst_key}/minutes/1"
    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
        if res.status_code == 200:
            candles = res.json().get("data", {}).get("candles", [])
            if candles:
                df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                return df.sort_values("timestamp").reset_index(drop=True)
    except Exception:
        pass
    return pd.DataFrame()

def get_atm_option_contract_v2(opt_type: str = "CE") -> dict:
    url = f"{BASE_URL}/v2/instruments/search"
    params = {
        "query": "NIFTY",
        "exchanges": "NSE",
        "segments": "FO",
        "instrument_types": opt_type,
        "expiry": "current_week",
        "atm_offset": 0,
        "records": 5
    }
    try:
        res = requests.get(url, headers=HEADERS, params=params, timeout=8)
        if res.status_code == 200:
            data = res.json().get("data", [])
            if data:
                c = data[0]
                return {
                    "strike_price": float(c.get("strike_price", 0.0)),
                    "instrument_key": c.get("instrument_key"),
                    "trading_symbol": c.get("trading_symbol"),
                    "lot_size": int(c.get("lot_size", 65))
                }
    except Exception:
        pass
    return {}

def get_live_option_ltp_by_key(instrument_key: str) -> float:
    if not instrument_key:
        return 0.0
    url = f"{BASE_URL}/v3/market-quote/quotes"
    try:
        res = requests.get(url, headers=HEADERS, params={"instrument_key": instrument_key}, timeout=5)
        if res.status_code == 200:
            data = res.json().get("data", {})
            for _, val in data.items():
                return float(val.get("last_price", 0.0))
    except Exception:
        pass
    return 0.0

# ==============================================================================
# ६. लेजर व्यवस्थापक (CSV Logger)
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
# ७. मुख्य लाइव्ह इंजिन
# ==============================================================================
def run_live_paper_bot():
    print("====================================================================")
    print("   NIFTY GANN SQUARE OF 9 (LIVE BOT - DARVAS BOX BREAKOUT)          ")
    print("====================================================================")

    if not ACCESS_TOKEN:
        print("[-] त्रुटी: UPSTOX_TOKEN उपलब्ध नाही. GitHub Secrets तपासा.")
        sys.exit(1)

    init_paper_ledger()
    
    prev_close = get_nifty_prev_close_v3()
    if prev_close <= 0.0:
        print("[-] त्रुटी: Upstox कडून मागील क्लोज भाव मिळाला नाही.")
        sys.exit(1)

    gann_levels = calculate_gann_levels(prev_close)
    sample_c = get_atm_option_contract_v2(opt_type="CE")
    lot_size = sample_c.get("lot_size", 65)

    print(f"[+] API मागील क्लोज: {prev_close} | चालू लॉट साईझ: {lot_size}")
    print(f"[+] गॅन लेव्हल्स: {gann_levels}")

    send_telegram_alert(
        f"🚀 *NIFTY GANN BOT STARTED (Live Scanner)*\n"
        f"• *API Prev Close:* `{prev_close}`\n"
        f"• *Lot Size:* `{lot_size}`\n"
        f"• *Gann Levels:* `{gann_levels}`\n"
        f"• *Status:* Live Market Scanner Active"
    )

    box_trackers = {lvl: {"state": "IDLE", "start_idx": None} for lvl in gann_levels}
    used_levels_today = set()
    daily_trades = 0
    active_position = None

    print("[*] लाइव्ह मार्केट स्कॅनर सुरू झाला आहे...\n")

    while True:
        try:
            now_dt = get_ist_now()
            now_str = now_dt.strftime("%H:%M")
            today_str = now_dt.strftime("%Y-%m-%d")

            # सकाळच्या ९:२० आधी थांबा
            if now_str < "09:20":
                time.sleep(10)
                continue

            # १५:२० वाजता सक्तीने बंद करणे
            if now_str >= "15:20":
                if active_position:
                    exit_price = active_position["current_opt_px"]
                    pts = round(exit_price - active_position["entry_px"], 2)
                    gross = round(pts * active_position["lot_size"], 2)
                    net = round(gross - 52.0, 2)
                    update_trade_in_csv(active_position["trade_id"], {
                        "exit_time": now_str, "exit_px": exit_price,
                        "pts": pts, "gross_pnl": gross, "net_pnl": net,
                        "exit_reason": "FORCE_EOD ⏱️", "status": "CLOSED"
                    })
                    send_telegram_alert(
                        f"⏱️ *FORCE EOD EXIT*\n"
                        f"• *Strike:* `{active_position['strike']}`\n"
                        f"• *Exit:* `₹{exit_price}`\n"
                        f"• *Points:* `{pts:+} pts`\n"
                        f"• *Net PnL:* `₹{net:+,.2f}`"
                    )
                    active_position = None
                print(f"\n[*] {now_str} IST - मार्केट बंद झाले आहे.")
                break

            # थेट टिक-बाय-टिक चालू भाव (Never Stuck)
            live_spot = get_nifty_live_ltp()
            if live_spot <= 0.0:
                time.sleep(3)
                continue

            # चालू पोझिशनचे मूल्यांकन
            if active_position:
                opt_key = active_position["instrument_key"]
                live_opt = get_live_option_ltp_by_key(opt_key)
                
                if live_opt <= 0:
                    diff = (live_spot - active_position["entry_spot"]) if active_position["side"] == "BUY_CE" else (active_position["entry_spot"] - live_spot)
                    cur_opt = round(active_position["entry_px"] + (diff * 0.52), 2)
                else:
                    cur_opt = live_opt

                active_position["current_opt_px"] = cur_opt

                if cur_opt >= active_position["entry_px"] + active_position["sl_pts"]:
                    active_position["sl_px"] = max(active_position["sl_px"], active_position["entry_px"] + 2.0)

                # टार्गेट हिट
                if cur_opt >= active_position["tgt_px"]:
                    pts = round(active_position["tgt_px"] - active_position["entry_px"], 2)
                    gross = round(pts * active_position["lot_size"], 2)
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
                        f"• *Points:* `+{pts} pts`\n"
                        f"• *Net PnL:* `₹{net:+,.2f}`"
                    )
                    active_position = None

                # स्टॉप-लॉस हिट
                elif cur_opt <= active_position["sl_px"]:
                    pts = round(active_position["sl_px"] - active_position["entry_px"], 2)
                    gross = round(pts * active_position["lot_size"], 2)
                    net = round(gross - 52.0, 2)
                    update_trade_in_csv(active_position["trade_id"], {
                        "exit_time": now_str, "exit_px": active_position["sl_px"],
                        "pts": pts, "gross_pnl": gross, "net_pnl": net,
                        "exit_reason": "SL_HIT ❌", "status": "CLOSED"
                    })
                    send_telegram_alert(
                        f"🛑 *SL EXIT*\n"
                        f"• *Strike:* `{active_position['strike']}`\n"
                        f"• *Exit:* `₹{active_position['sl_px']}`\n"
                        f"• *Points:* `{pts} pts`\n"
                        f"• *Net PnL:* `₹{net:+,.2f}`"
                    )
                    active_position = None

            # कन्सॉलिडेशन बॉक्स तपासणी
            df_curr = get_live_nifty_candles_v3()
            c_len = len(df_curr)

            if not df_curr.empty and daily_trades < 3 and not active_position:
                for lvl in gann_levels:
                    buf = lvl * BUFFER_PCT
                    zone_low = lvl - buf
                    zone_high = lvl + buf
                    tracker = box_trackers[lvl]

                    if tracker["state"] == "IDLE":
                        if zone_low <= live_spot <= zone_high:
                            box_trackers[lvl] = {"state": "CONSOLIDATING", "start_idx": c_len - 1}
                            print(f"\n[*] {now_str} IST - निफ्टी {lvl} जवळ आला. बॉक्स कन्सॉलिडेशन सुरू.")

                    elif tracker["state"] == "CONSOLIDATING":
                        bars_in_box = (c_len - 1) - tracker["start_idx"]

                        if bars_in_box >= MIN_BOX_BARS and lvl not in used_levels_today:
                            box_window = df_curr.iloc[tracker["start_idx"] : c_len]
                            box_high = box_window['high'].max()
                            box_low = box_window['low'].min()

                            # BUY CE ब्रेकआउट
                            if live_spot > box_high:
                                contract = get_atm_option_contract_v2(opt_type="CE")
                                atm_strike = contract.get("strike_price", round(live_spot / 50.0) * 50)
                                opt_key = contract.get("instrument_key", "")
                                trade_lot = contract.get("lot_size", 65)

                                real_opt_px = get_live_option_ltp_by_key(opt_key)
                                if real_opt_px <= 0:
                                    real_opt_px = round(max(40.0, live_spot * 0.0075), 2)

                                index_sl = box_low - 6.0
                                opt_sl_pts = min(35.0, max(8.0, round(abs(live_spot - index_sl) * 0.52, 2)))
                                opt_tgt_pts = round(opt_sl_pts * 2.0, 2)

                                trade_id = f"T_{now_dt.strftime('%Y%m%d_%H%M%S')}"
                                active_position = {
                                    "trade_id": trade_id, "side": "BUY_CE",
                                    "strike": f"NIFTY {int(atm_strike)} CE",
                                    "instrument_key": opt_key,
                                    "lot_size": trade_lot,
                                    "entry_spot": live_spot, "entry_px": real_opt_px,
                                    "current_opt_px": real_opt_px, "sl_pts": opt_sl_pts,
                                    "sl_px": max(5.0, round(real_opt_px - opt_sl_pts, 2)),
                                    "tgt_px": round(real_opt_px + opt_tgt_pts, 2)
                                }

                                log_trade_to_csv({
                                    "trade_id": trade_id, "date": today_str,
                                    "side": "BUY_CE", "strike": f"NIFTY {int(atm_strike)} CE",
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
                                    f"• *Strike:* `{int(atm_strike)} CE` (Lot: {trade_lot})\n"
                                    f"• *Box Range:* `{round(box_low, 1)} - {round(box_high, 1)}`\n"
                                    f"• *Live Entry:* `₹{real_opt_px}`\n"
                                    f"• *Dynamic SL:* `₹{active_position['sl_px']}` ({opt_sl_pts} pts)\n"
                                    f"• *Target (1:2):* `₹{active_position['tgt_px']}` ({opt_tgt_pts} pts)"
                                )
                                break

                            # BUY PE ब्रेकडाऊन
                            elif live_spot < box_low:
                                contract = get_atm_option_contract_v2(opt_type="PE")
                                atm_strike = contract.get("strike_price", round(live_spot / 50.0) * 50)
                                opt_key = contract.get("instrument_key", "")
                                trade_lot = contract.get("lot_size", 65)

                                real_opt_px = get_live_option_ltp_by_key(opt_key)
                                if real_opt_px <= 0:
                                    real_opt_px = round(max(40.0, live_spot * 0.0075), 2)

                                index_sl = box_high + 6.0
                                opt_sl_pts = min(35.0, max(8.0, round(abs(index_sl - live_spot) * 0.52, 2)))
                                opt_tgt_pts = round(opt_sl_pts * 2.0, 2)

                                trade_id = f"T_{now_dt.strftime('%Y%m%d_%H%M%S')}"
                                active_position = {
                                    "trade_id": trade_id, "side": "BUY_PE",
                                    "strike": f"NIFTY {int(atm_strike)} PE",
                                    "instrument_key": opt_key,
                                    "lot_size": trade_lot,
                                    "entry_spot": live_spot, "entry_px": real_opt_px,
                                    "current_opt_px": real_opt_px, "sl_pts": opt_sl_pts,
                                    "sl_px": max(5.0, round(real_opt_px - opt_sl_pts, 2)),
                                    "tgt_px": round(real_opt_px + opt_tgt_pts, 2)
                                }

                                log_trade_to_csv({
                                    "trade_id": trade_id, "date": today_str,
                                    "side": "BUY_PE", "strike": f"NIFTY {int(atm_strike)} PE",
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
                                    f"• *Strike:* `{int(atm_strike)} PE` (Lot: {trade_lot})\n"
                                    f"• *Box Range:* `{round(box_low, 1)} - {round(box_high, 1)}`\n"
                                    f"• *Live Entry:* `₹{real_opt_px}`\n"
                                    f"• *Dynamic SL:* `₹{active_position['sl_px']}` ({opt_sl_pts} pts)\n"
                                    f"• *Target (1:2):* `₹{active_position['tgt_px']}` ({opt_tgt_pts} pts)"
                                )
                                break

            # एकाखाली एक न येता तिथेच ओळ पुसून अपडेट करणे:
            status_line = f"[*] IST वेळ: {now_str} | स्पॉट: {live_spot:.2f} | आजचे ट्रेड्स: {daily_trades}/3"
            sys.stdout.write(f"\r\033[K{status_line}")
            sys.stdout.flush()

            time.sleep(3)

        except KeyboardInterrupt:
            print("\n[!] बॉट थांबवला.")
            break
        except Exception as e:
            print(f"\n[-] त्रुटी: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run_live_paper_bot()
