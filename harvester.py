import io
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==============================================================================
# १. कॉन्फिगरेशन व क्रेडेंशियल्स
# ==============================================================================
DB_FILE = 'nifty_vault.db'
ACCESS_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN") or os.environ.get("UPSTOX_TOKEN", "").strip()

BASE_URL = "https://api.upstox.com"
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Authorization": f"Bearer {ACCESS_TOKEN}"
}

IST = timezone(timedelta(hours=5, minutes=30))

session = requests.Session()
retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))


def init_vault_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute('''
    CREATE TABLE IF NOT EXISTS nifty_options_1m (
        date TEXT,
        timestamp TEXT,
        symbol TEXT,
        instrument_key TEXT,
        expiry TEXT,
        strike REAL,
        option_type TEXT,
        open REAL,
        high REAL,
        low REAL,
        close REAL,
        volume INTEGER,
        oi INTEGER,
        PRIMARY KEY (symbol, timestamp)
    )''')
    conn.commit()
    conn.close()


def get_live_expiries():
    """ Upstox Expiries API वरून अधिकृत एक्सपायरी लिस्ट मिळवणे """
    url = f"{BASE_URL}/v2/expired-instruments/expiries?instrument_key=NSE_INDEX%7CNifty%2050"
    try:
        res = session.get(url, headers=HEADERS, timeout=8)
        if res.status_code == 200:
            expiries = res.json().get('data', [])
            if expiries:
                return sorted(list(set(expiries)))
    except Exception:
        pass

    # लाइव्ह ऑप्शन चेन बॅकअप
    try:
        res_live = session.get(f"{BASE_URL}/v2/option/contract?instrument_key=NSE_INDEX%7CNifty%2050", headers=HEADERS, timeout=8)
        if res_live.status_code == 200:
            contracts = res_live.json().get('data', [])
            return sorted(list(set(c['expiry'] for c in contracts if 'expiry' in c)))
    except Exception:
        pass
    return []


def get_today_active_expiry(expiries_list):
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    valid = [e for e in expiries_list if e >= today_str]
    return valid[0] if valid else today_str


def get_current_nifty_spot():
    index_key = quote("NSE_INDEX|Nifty 50", safe="")
    url = f"{BASE_URL}/v2/market-quote/ltp?instrument_key={index_key}"
    try:
        res = session.get(url, headers=HEADERS, timeout=4)
        if res.status_code == 200:
            data = res.json().get("data", {})
            first_val = next(iter(data.values()), {})
            return float(first_val.get("last_price", 0.0))
    except Exception:
        pass
    return 0.0


def harvest_and_vault():
    init_vault_db()
    print("🌾 [Harvester] डेटा गोळा करण्याचे काम सुरू होत आहे...", flush=True)

    expiries = get_live_expiries()
    active_expiry = get_today_active_expiry(expiries)
    spot_px = get_current_nifty_spot()

    if spot_px <= 0:
        print("[-] [Harvester] स्पॉट प्राईस मिळाली नाही.", flush=True)
        return

    atm_strike = round(spot_px / 50.0) * 50.0
    strikes_to_track = [atm_strike + (i * 50.0) for i in range(-3, 4)]

    # चालू आठवड्याचे ऑप्शन्स कॉन्ट्रॅक्ट्स मिळवणे
    url_c = f"{BASE_URL}/v2/option/contract?instrument_key=NSE_INDEX%7CNifty%2050&expiry_date={active_expiry}"
    contracts = []
    try:
        res = session.get(url_c, headers=HEADERS, timeout=8)
        if res.status_code == 200:
            contracts = res.json().get('data', [])
    except Exception:
        pass

    if not contracts:
        # Expired API फॉलबॅक
        url_exp = f"{BASE_URL}/v2/expired-instruments/option/contract?instrument_key=NSE_INDEX%7CNifty%2050&expiry_date={active_expiry}"
        try:
            r_exp = session.get(url_exp, headers=HEADERS, timeout=8)
            if r_exp.status_code == 200:
                contracts = r_exp.json().get('data', [])
        except Exception:
            pass

    records_to_insert = []
    today_str = datetime.now(IST).strftime("%Y-%m-%d")

    for c in contracts:
        strike_val = float(c.get('strike_price', 0))
        if strike_val in strikes_to_track:
            inst_key = c.get('instrument_key')
            sym = c.get('trading_symbol')
            opt_type = c.get('instrument_type', '').upper()

            enc_key = quote(inst_key, safe='')
            c_url = f"{BASE_URL}/v2/historical-candle/intraday/{enc_key}/1minute"
            try:
                r_c = session.get(c_url, headers=HEADERS, timeout=6)
                if r_c.status_code == 200:
                    candles = r_c.json().get('data', {}).get('candles', [])
                    for bar in candles:
                        records_to_insert.append((
                            today_str, bar[0], sym, inst_key, active_expiry,
                            strike_val, opt_type, float(bar[1]), float(bar[2]),
                            float(bar[3]), float(bar[4]), int(bar[5]),
                            int(bar[6]) if len(bar) > 6 and bar[6] is not None else 0
                        ))
            except Exception:
                pass
            time.sleep(0.05)

    if records_to_insert:
        conn = sqlite3.connect(DB_FILE)
        conn.executemany('''
        INSERT OR REPLACE INTO nifty_options_1m
        (date, timestamp, symbol, instrument_key, expiry, strike, option_type, open, high, low, close, volume, oi)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', records_to_insert)
        conn.commit()
        conn.close()
        print(f"✅ [Harvester] {len(records_to_insert)} कॅन्डल्स व्हॉल्टमध्ये यशस्वी साठवल्या.", flush=True)
    else:
        print("ℹ️ [Harvester] साठवण्यासाठी नवीन कॅन्डल्स सापडल्या नाहीत.", flush=True)


if __name__ == "__main__":
    harvest_and_vault()
