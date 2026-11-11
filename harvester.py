import os
import math
import sqlite3
import datetime
import urllib.parse
import pytz
import requests
import pandas as pd
import numpy as np

DB_PATH = "nifty_vault.db"
BASE_URL = "https://api.upstox.com"

ACCESS_TOKEN = ""
if os.path.exists("ACCESS_TOKEN.txt"):
    with open("ACCESS_TOKEN.txt", "r") as f:
        ACCESS_TOKEN = f.read().strip()
if not ACCESS_TOKEN:
    ACCESS_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()


# ========================= BLACK-SCHOLES FORMULA =========================
def norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def norm_pdf(x):
    return (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-0.5 * x * x)

def calculate_greeks(spot, strike, t_years, opt_price, opt_type="CE", r=0.07):
    """Black-Scholes IV आणि Delta काढणे"""
    if spot <= 0 or strike <= 0 or t_years <= 0 or opt_price <= 0.05:
        return 0.0, 0.0

    # IV शोधण्यासाठी Bisection पद्धत
    low_vol, high_vol = 0.01, 3.0
    iv = 0.20  # फॉलबॅक

    for _ in range(25):
        mid_vol = (low_vol + high_vol) / 2.0
        d1 = (math.log(spot / strike) + (r + 0.5 * mid_vol ** 2) * t_years) / (mid_vol * math.sqrt(t_years))
        d2 = d1 - mid_vol * math.sqrt(t_years)

        if opt_type == "CE":
            theo_p = spot * norm_cdf(d1) - strike * math.exp(-r * t_years) * norm_cdf(d2)
        else:
            theo_p = strike * math.exp(-r * t_years) * norm_cdf(-d2) - spot * norm_cdf(-d1)

        diff = theo_p - opt_price
        if abs(diff) < 0.05:
            iv = mid_vol
            break
        if diff > 0:
            high_vol = mid_vol
        else:
            low_vol = mid_vol
    else:
        iv = (low_vol + high_vol) / 2.0

    # अंतिम डेल्टा
    d1 = (math.log(spot / strike) + (r + 0.5 * iv ** 2) * t_years) / (iv * math.sqrt(t_years))
    if opt_type == "CE":
        delta = norm_cdf(d1)
    else:
        delta = norm_cdf(d1) - 1.0

    return round(float(iv), 4), round(float(delta), 4)


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

    instrument_key = urllib.parse.quote("NSE_INDEX|Nifty 50")
    url = f"{BASE_URL}/v2/historical-candle/{instrument_key}/1minute/{target_date}/{target_date}"
    headers = {"Accept": "application/json"}
    if ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {ACCESS_TOKEN}"

    res = requests.get(url, headers=headers, timeout=15)
    candles = []

    if res.status_code == 200:
        candles = res.json().get("data", {}).get("candles", [])

    if not candles:
        url_alt = f"{BASE_URL}/v2/historical-candle/{instrument_key}/1minute/{target_date}"
        res_alt = requests.get(url_alt, headers=headers, timeout=15)
        if res_alt.status_code == 200:
            candles = res_alt.json().get("data", {}).get("candles", [])

    if not candles:
        print(f"[-] NIFTY 50 स्पॉट कॅन्डल्स मिळाल्या नाहीत. API Status: {res.status_code}", flush=True)
        return None, {}

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    spot_close_latest = float(candles[0][4])
    spot_map = {}

    for c in candles:
        ts = str(c[0])
        cp = float(c[4])
        spot_map[ts] = cp
        cur.execute("""
            INSERT OR REPLACE INTO nifty_index_1m (timestamp, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (ts, float(c[1]), float(c[2]), float(c[3]), cp, float(c[5])))

    conn.commit()
    conn.close()
    print(f"[+] [Harvester] चालू NIFTY 50 स्पॉट: {spot_close_latest}", flush=True)
    return spot_close_latest, spot_map


def harvest_options_for_date(target_date: str, spot_price: float, spot_map: dict):
    contracts_url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
    print("[+] Options Instruments मास्टर डाऊनलोड होत आहे...", flush=True)
    try:
        df_inst = pd.read_csv(contracts_url, compression="gzip", low_memory=False)
    except Exception as e:
        print(f"[-] Instruments लोड करता आले नाहीत: {e}", flush=True)
        return

    df_inst.columns = [str(c).strip().lower() for c in df_inst.columns]
    sym_col = "tradingsymbol" if "tradingsymbol" in df_inst.columns else "trading_symbol"

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
        valid_expiries = sorted(df_opt["expiry"].unique(), reverse=True)

    active_expiry = valid_expiries[0]
    print(f"[+] [Expiry Selector] निवडलेली वैध आगामी एक्सपायरी: {active_expiry}", flush=True)

    # एक्सपायरीसाठी शिल्लक दिवस
    exp_dt = datetime.datetime.strptime(active_expiry, "%Y-%m-%d")
    cur_dt = datetime.datetime.strptime(target_date, "%Y-%m-%d")
    days_to_exp = max((exp_dt - cur_dt).days, 0.5)
    t_years = days_to_exp / 365.0

    atm_strike = round(spot_price / 50.0) * 50.0
    strikes_to_track = [atm_strike + (i * 50) for i in range(-8, 9)]
    print(f"[+] [Harvester] ट्रॅक करायचे स्ट्राइक्स (ATM {atm_strike}): {strikes_to_track}", flush=True)

    df_target = df_opt[
        (df_opt["expiry"] == active_expiry) & 
        (df_opt["strike"].isin(strikes_to_track))
    ].copy()

    headers = {"Accept": "application/json"}
    if ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {ACCESS_TOKEN}"

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    total_opt_candles = 0

    for _, row in df_target.iterrows():
        raw_key = row["instrument_key"]
        inst_key = urllib.parse.quote(str(raw_key))
        sym = row.get(sym_col, row.get("symbol", str(raw_key)))
        strike = float(row["strike"])
        opt_type = str(row["option_type"]).upper()

        url = f"{BASE_URL}/v2/historical-candle/{inst_key}/1minute/{target_date}/{target_date}"
        res = requests.get(url, headers=headers, timeout=10)
        c_list = []
        if res.status_code == 200:
            c_list = res.json().get("data", {}).get("candles", [])

        if not c_list:
            url_alt = f"{BASE_URL}/v2/historical-candle/{inst_key}/1minute/{target_date}"
            res_alt = requests.get(url_alt, headers=headers, timeout=10)
            if res_alt.status_code == 200:
                c_list = res_alt.json().get("data", {}).get("candles", [])

        for c in c_list:
            ts = str(c[0])
            opt_close = float(c[4])
            spot_at_ts = spot_map.get(ts, spot_price)

            # ग्रीक्स कॅल्क्युलेट करणे
            iv, delta = calculate_greeks(spot_at_ts, strike, t_years, opt_close, opt_type)

            cur.execute("""
                INSERT OR REPLACE INTO nifty_options_1m 
                (timestamp, symbol, strike, option_type, open, high, low, close, volume, oi, iv, delta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ts, sym, strike, opt_type, float(c[1]), float(c[2]), float(c[3]), opt_close, float(c[5]), float(c[6]) if len(c) > 6 else 0.0, iv, delta))
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

    if now_ist.weekday() == 5:
        now_ist -= datetime.timedelta(days=1)
    elif now_ist.weekday() == 6:
        now_ist -= datetime.timedelta(days=2)

    t_date = now_ist.strftime("%Y-%m-%d")
    spot, spot_map = get_nifty_spot_history(t_date)
    if spot:
        harvest_options_for_date(t_date, spot, spot_map)
