import math
import os
import sys
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
import pandas as pd
import requests

# ==============================================================================
# १. कॉन्फिगरेशन
# ==============================================================================
ACCESS_TOKEN = (
    os.environ.get("UPSTOX_TOKEN") or 
    os.environ.get("UPSTOX_ACCESS_TOKEN") or 
    ""
).strip()

BASE_URL = "https://api.upstox.com"
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Authorization": f"Bearer {ACCESS_TOKEN}"
}

BUFFER_PCT = 0.001          # ०.१% गॅन झोन बफर
MIN_BOX_BARS = 12           # कन्सॉलिडेशन बॉक्ससाठी किमान १२ कॅन्डल्स

# कालची तारीख (सोमवार, ०२ नोव्हेंबर २०२६)
TEST_DATE = "2026-11-02"

# ==============================================================================
# २. गॅन लेव्हल्स कॅल्क्युलेटर
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
# ३. कालचा १-मिनिट डेटा खेचणे (Upstox V3 Historical API)
# ==============================================================================
def fetch_yesterday_candles(target_date: str) -> pd.DataFrame:
    inst_key = quote("NSE_INDEX|Nifty 50", safe="")
    # V3 Historical API: /v3/historical-candle/:instrument_key/minutes/1/:to_date/:from_date
    url = f"{BASE_URL}/v3/historical-candle/{inst_key}/minutes/1/{target_date}/{target_date}"
    
    try:
        res = requests.get(url, headers=HEADERS, timeout=12)
        if res.status_code == 200:
            candles = res.json().get("data", {}).get("candles", [])
            if candles:
                df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df = df.sort_values("timestamp").reset_index(drop=True)
                return df
            else:
                print(f"[-] {target_date} साठी कॅन्डल्स आढळल्या नाहीत.")
        else:
            print(f"[-] API Error ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"[-] Data Fetch त्रुटी: {e}")
    return pd.DataFrame()

# ==============================================================================
# ४. सिग्नल टेस्टिंग सिम्युलेटर
# ==============================================================================
def run_yesterday_test():
    print("====================================================================")
    print(f"   NIFTY GANN 9 + DARVAS BOX SIGNAL BACKTEST: {TEST_DATE}          ")
    print("====================================================================")

    if not ACCESS_TOKEN:
        print("[-] त्रुटी: UPSTOX_TOKEN उपलब्ध नाही.")
        sys.exit(1)

    # कालच्या आधीचा क्लोज (शुक्रवार, ३० ऑक्टोबर २०२६ चा अधिकृत क्लोज)
    prev_close = 23053.10
    gann_levels = calculate_gann_levels(prev_close)

    print(f"[+] मागील अधिकृत क्लोज: {prev_close}")
    print(f"[+] गॅन लेव्हल्स: {gann_levels}")
    print(f"[*] {TEST_DATE} चा १-मिनिट डेटा Upstox कडून आणत आहे...")

    df = fetch_yesterday_candles(TEST_DATE)
    if df.empty:
        print("[-] चाचणी करण्यासाठी डेटा उपलब्ध नाही.")
        return

    print(f"[+] एकूण {len(df)} कॅन्डल्स प्राप्त झाल्या (०९:१५ ते १५:३०).\n")
    print("-" * 75)
    print(f"{'वेळ':<8} | {'प्रकार':<10} | {'गॅन लेव्हल':<10} | {'स्पॉट ब्रेकआउट':<12} | {'बॉक्स हाय-लो':<18}")
    print("-" * 75)

    box_trackers = {lvl: {"state": "IDLE", "start_idx": None} for lvl in gann_levels}
    used_levels = set()
    total_signals = 0

    for idx, row in df.iterrows():
        c_time = row["timestamp"].strftime("%H:%M")
        spot_close = float(row["close"])

        # ९:२० पूर्वी विश्लेषण नाही
        if c_time < "09:20" or c_time >= "15:20":
            continue

        for lvl in gann_levels:
            buf = lvl * BUFFER_PCT
            zone_low = lvl - buf
            zone_high = lvl + buf
            tracker = box_trackers[lvl]

            if tracker["state"] == "IDLE":
                if zone_low <= spot_close <= zone_high:
                    box_trackers[lvl] = {"state": "CONSOLIDATING", "start_idx": idx}

            elif tracker["state"] == "CONSOLIDATING":
                bars_in_box = idx - tracker["start_idx"]

                if bars_in_box >= MIN_BOX_BARS and lvl not in used_levels:
                    box_window = df.iloc[tracker["start_idx"] : idx + 1]
                    box_high = box_window["high"].max()
                    box_low = box_window["low"].min()

                    # BUY CE ब्रेकआउट
                    if spot_close > box_high:
                        total_signals += 1
                        used_levels.add(lvl)
                        box_trackers[lvl] = {"state": "IDLE", "start_idx": None}
                        print(f"{c_time:<8} | {'BUY CE 🟢':<10} | {lvl:<10} | {spot_close:<12.2f} | {box_low:.1f} - {box_high:.1f}")

                    # BUY PE ब्रेकडाऊन
                    elif spot_close < box_low:
                        total_signals += 1
                        used_levels.add(lvl)
                        box_trackers[lvl] = {"state": "IDLE", "start_idx": None}
                        print(f"{c_time:<8} | {'BUY PE 🔴':<10} | {lvl:<10} | {spot_close:<12.2f} | {box_low:.1f} - {box_high:.1f}")

    print("-" * 75)
    if total_signals == 0:
        print(f"ℹ️ {TEST_DATE} रोजी गॅन लेव्हल्सवर १२-कॅन्डल कन्सॉलिडेशन ब्रेकआउटचा कोणताही सिग्नल बनला नाही.")
        print("   (कारण काल निफ्टी प्रामुख्याने २३,२५० ते २३,३३० दरम्यान ट्रेड करत होता, जो २३,१०४ आणि २३,४०९ च्या मधला नो-ट्रेड झोन आहे).")
    else:
        print(f"✅ काल एकूण {total_signals} व्हॅलिड सिग्नल्स मिळाले.")

if __name__ == "__main__":
    run_yesterday_test()
