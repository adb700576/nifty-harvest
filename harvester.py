import os
import sqlite3
import datetime
import pytz
import requests
import pandas as pd

DB_PATH = "nifty_vault.db"
BASE_URL = "https://api.upstox.com"

# टोकन फाईलमधून किंवा एन्व्हायर्नमेंटमधून वाचणे
ACCESS_TOKEN = ""
if os.path.exists("ACCESS_TOKEN.txt"):
    with open("ACCESS_TOKEN.txt", "r") as f:
        ACCESS_TOKEN = f.read().strip()
if not ACCESS_TOKEN:
    ACCESS_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS nifty_index_1m (
            timestamp TEXT PRIMARY KEY,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS nifty_options_1m (
            timestamp TEXT,
            symbol TEXT,
            strike REAL,
            option_type TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            oi REAL,
            iv REAL,
            delta REAL,
            PRIMARY KEY (timestamp, symbol)
        )
    """)
    conn.commit()
    conn.close()


def get_nifty_spot_history(target_date: str):
    print(f"🌾 [Vault Harvester] {target_date} चा डेटा गोळा करण्याचे काम सुरू होत आहे...", flush=True)
    instrument_key = "NSE_INDEX|Nifty 50"
    url = f"{BASE_URL}/v2/historical-candle/{instrument_key}/1minute/{target_date}/{target_date}"
    headers = {"Accept": "application/json"}
    
    res = requests.get(url, headers=headers, timeout=15)
    if res.status_code != 200:
        print(f"[-] NIFTY 50 स्पॉट डेटा मिळाला नाही: {res.text}", flush=True)
        return None
    
    candles = res.json().get("data", {}).get("candles", [])
    if not candles:
        print("[-] कॅन्डल्स रिकाम्या आहेत (मार्केट सुट्टी असू शकते).", flush=True)
        return None
    
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    spot_close_latest = candles[0][4]
    
    for c in candles:
        # Upstox format: [timestamp, open, high, low, close, volume, oi]
        cur.execute("""
            INSERT OR REPLACE INTO nifty_index_1m (timestamp, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (c[0], c[1], c[2], c[3], c[4], c[5]))
    
    conn.commit()
    conn.close()
    print(f"[+] [Harvester] चालू NIFTY 50 स्पॉट: {spot_close_latest}", flush=True)
    return spot_close_latest


def harvest_options_for_date(target_date: str, spot_price: float):
    # Upstox Options Contract Master शोधणे
    contracts_url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
    print("[+] Options Instruments मास्टर डाऊनलोड होत आहे...", flush=True)
    try:
        df_inst = pd.read_csv(contracts_url)
    except Exception as e:
        print(f"[-] Instruments लोड करता आले नाहीत: {e}", flush=True)
        return

    # NIFTY ऑप्शन्स फिल्टर
    df_opt = df_inst[(df_inst["exchange"] == "NSE_FO") & (df_inst["name"] == "NIFTY") & (df_inst["instrument_type"] == "OPTIDX")].copy()
    if df_opt.empty:
        print("[-] NIFTY ऑप्शन्स सापडले नाहीत.", flush=True)
        return

    df_opt["expiry"] = pd.to_datetime(df_opt["expiry"]).dt.strftime("%Y-%m-%d")
    valid_expiries = sorted([x for x in df_opt["expiry"].unique() if x >= target_date])
    if not valid_expiries:
        print("[-] वैध आगामी एक्सपायरी सापडली नाही.", flush=True)
        return

    active_expiry = valid_expiries[0]
    print(f"[+] [Expiry Selector] निवडलेली वैध आगामी एक्सपायरी: {active_expiry}", flush=True)

    atm_strike = round(spot_price / 50.0) * 50.0
    strikes_to_track = [atm_strike + (i * 50) for i in range(-8, 9)]
    print(f"[+] [Harvester] ट्रॅक करायचे स्ट्राइक्स (ATM {atm_strike}): {strikes_to_track}", flush=True)

    df_target = df_opt[(df_opt["expiry"] == active_expiry) & (df_opt["strike"].isin(strikes_to_track))].copy()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    total_opt_candles = 0

    for _, row in df_target.iterrows():
        inst_key = row["instrument_key"]
        sym = row["trading_symbol"]
        strike = float(row["strike"])
        opt_type = row["option_type"]

        url = f"{BASE_URL}/v2/historical-candle/{inst_key}/1minute/{target_date}/{target_date}"
        res = requests.get(url, headers={"Accept": "application/json"}, timeout=10)
        if res.status_code == 200:
            c_list = res.json().get("data", {}).get("candles", [])
            for c in c_list:
                cur.execute("""
                    INSERT OR REPLACE INTO nifty_options_1m 
                    (timestamp, symbol, strike, option_type, open, high, low, close, volume, oi, iv, delta)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (c[0], sym, strike, opt_type, c[1], c[2], c[3], c[4], c[5], c[6] if len(c) > 6 else 0, 0.0, 0.0))
                total_opt_candles += 1

    conn.commit()
    
    cur.execute("SELECT COUNT(*) FROM nifty_index_1m")
    spot_count = cur.fetchone()[0]
    conn.close()

    print(f"✅ व्हॉल्टमध्ये एकूण {spot_count + total_opt_candles} रेकॉर्ड्स साठवले (Spot: {spot_count}, Options: {total_opt_candles}).", flush=True)


if __name__ == "__main__":
    init_db()
    
    # IST नुसार चालू तारीख
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.datetime.now(ist)
    
    # जर शनिवार/रविवार असेल तर शुक्रवारचा डेटा
    if now_ist.weekday() == 5:
        now_ist -= datetime.timedelta(days=1)
    elif now_ist.weekday() == 6:
        now_ist -= datetime.timedelta(days=2)
        
    t_date = now_ist.strftime("%Y-%m-%d")
    
    spot = get_nifty_spot_history(t_date)
    if spot:
        harvest_options_for_date(t_date, spot)
