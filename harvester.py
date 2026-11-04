import os
import sqlite3
import datetime
import pytz
import requests
import pandas as pd

DB_PATH = "nifty_vault.db"
BASE_URL = "https://api.upstox.com"

# टोकन तपासणी
ACCESS_TOKEN = ""
if os.path.exists("ACCESS_TOKEN.txt"):
    with open("ACCESS_TOKEN.txt", "r") as f:
        ACCESS_TOKEN = f.read().strip()
if not ACCESS_TOKEN:
    ACCESS_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()


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
    
    # Upstox V2 Historical API
    # NSE_INDEX|Nifty 50 key url-encode: NSE_INDEX%7CNifty%2050
    instrument_key = "NSE_INDEX|Nifty 50"
    
    # Upstox API साठी to_date ही किमान १ दिवस पुढे किंवा चालू तारीख असावी लागते
    dt_obj = datetime.datetime.strptime(target_date, "%Y-%m-%d")
    next_day = (dt_obj + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    
    url = f"{BASE_URL}/v2/historical-candle/{instrument_key}/1minute/{next_day}/{target_date}"
    headers = {"Accept": "application/json"}
    if ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {ACCESS_TOKEN}"

    res = requests.get(url, headers=headers, timeout=15)
    candles = []
    if res.status_code == 200:
        candles = res.json().get("data", {}).get("candles", [])
    
    # बॅकअप ट्राय जर नेक्स्ट डे ने डेटा नाही आला तर थेट target_date ट्राय करणे
    if not candles:
        url_alt = f"{BASE_URL}/v2/historical-candle/{instrument_key}/1minute/{target_date}"
        res_alt = requests.get(url_alt, headers=headers, timeout=15)
        if res_alt.status_code == 200:
            candles = res_alt.json().get("data", {}).get("candles", [])

    # फक्त target_date च्याच कॅन्डल्स ठेवणे
    candles = [c for c in candles if str(c[0]).startswith(target_date)]

    if not candles:
        print(f"[-] NIFTY 50 स्पॉट कॅन्डल्स मिळाल्या नाहीत. API Status: {res.status_code}", flush=True)
        return None

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    spot_close_latest = float(candles[0][4])

    for c in candles:
        cur.execute("""
            INSERT OR REPLACE INTO nifty_index_1m (timestamp, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])))

    conn.commit()
    conn.close()
    print(f"[+] [Harvester] चालू NIFTY 50 स्पॉट: {spot_close_latest}", flush=True)
    return spot_close_latest


def harvest_options_for_date(target_date: str, spot_price: float):
    contracts_url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
    print("[+] Options Instruments मास्टर डाऊनलोड होत आहे...", flush=True)
    try:
        df_inst = pd.read_csv(contracts_url)
    except Exception as e:
        print(f"[-] Instruments लोड करता आले नाहीत: {e}", flush=True)
        return

    # NIFTY ऑप्शन्स
    df_opt = df_inst[
        (df_inst["exchange"] == "NSE_FO") & 
        (df_inst["name"] == "NIFTY") & 
        (df_inst["instrument_type"] == "OPTIDX")
    ].copy()

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

    df_target = df_opt[
        (df_opt["expiry"] == active_expiry) & 
        (df_opt["strike"].isin(strikes_to_track))
    ].copy()

    dt_obj = datetime.datetime.strptime(target_date, "%Y-%m-%d")
    next_day = (dt_obj + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    headers = {"Accept": "application/json"}
    if ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {ACCESS_TOKEN}"

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    total_opt_candles = 0

    for _, row in df_target.iterrows():
        inst_key = row["instrument_key"]
        sym = row["trading_symbol"]
        strike = float(row["strike"])
        opt_type = row["option_type"]

        url = f"{BASE_URL}/v2/historical-candle/{inst_key}/1minute/{next_day}/{target_date}"
        res = requests.get(url, headers=headers, timeout=10)
        c_list = []
        if res.status_code == 200:
            c_list = res.json().get("data", {}).get("candles", [])

        if not c_list:
            url_alt = f"{BASE_URL}/v2/historical-candle/{inst_key}/1minute/{target_date}"
            res_alt = requests.get(url_alt, headers=headers, timeout=10)
            if res_alt.status_code == 200:
                c_list = res_alt.json().get("data", {}).get("candles", [])

        c_list = [c for c in c_list if str(c[0]).startswith(target_date)]

        for c in c_list:
            cur.execute("""
                INSERT OR REPLACE INTO nifty_options_1m 
                (timestamp, symbol, strike, option_type, open, high, low, close, volume, oi, iv, delta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (c[0], sym, strike, opt_type, float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5]), float(c[6]) if len(c) > 6 else 0.0, 0.0, 0.0))
            total_opt_candles += 1

    conn.commit()

    cur.execute("SELECT COUNT(*) FROM nifty_index_1m")
    spot_count = cur.fetchone()[0]
    conn.close()

    print(f"✅ व्हॉल्टमध्ये एकूण {spot_count + total_opt_candles} रेकॉर्ड्स साठवले (Spot: {spot_count}, Options: {total_opt_candles}).", flush=True)


if __name__ == "__main__":
    init_db()

    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.datetime.now(ist)

    # वीकेंड असल्यास शुक्रवारची तारीख
    if now_ist.weekday() == 5:
        now_ist -= datetime.timedelta(days=1)
    elif now_ist.weekday() == 6:
        now_ist -= datetime.timedelta(days=2)

    t_date = now_ist.strftime("%Y-%m-%d")

    spot = get_nifty_spot_history(t_date)
    if spot:
        harvest_options_for_date(t_date, spot)
