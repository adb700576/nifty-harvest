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
ACCESS_TOKEN = ""
if os.path.exists("token.txt"):
    with open("token.txt", "r") as f:
        ACCESS_TOKEN = f.read().strip()

if not ACCESS_TOKEN:
    ACCESS_TOKEN = (os.environ.get("UPSTOX_TOKEN") or os.environ.get("UPSTOX_ACCESS_TOKEN") or "").strip()

DB_FILE = 'nifty_vault.db'
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


def send_telegram_alert(message: str):
    """टेलिग्रामवर थेट मराठीत अलर्ट पाठवणे"""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not bot_token or not chat_id:
        print("[-] [Telegram] Secrets सापडले नाहीत. मेसेज पाठवला नाही.", flush=True)
        return

    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown"
        }
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            print("[+] [Telegram] अलर्ट यशस्वीरीत्या पाठवला!", flush=True)
        else:
            print(f"[-] [Telegram] त्रुटी ({res.status_code}): {res.text}", flush=True)
    except Exception as e:
        print(f"[-] [Telegram] अपवाद: {e}", flush=True)


def is_market_holiday_today() -> bool:
    """आज मार्केटला सुट्टी आहे का हे तपासणे"""
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    url = f"{BASE_URL}/v2/market/holidays/{today_str}"
    try:
        res = session.get(url, headers=HEADERS, timeout=6)
        if res.status_code == 200:
            data = res.json().get("data", [])
            for hol in data:
                if hol.get("holiday_type") == "TRADING_HOLIDAY":
                    closed_ex = hol.get("closed_exchanges", [])
                    if "NSE" in closed_ex or "NFO" in closed_ex:
                        print(f"ℹ️ [Holiday] आज सुट्टी आहे: {hol.get('description')}", flush=True)
                        return True
    except Exception:
        pass
    return False


def init_vault_db():
    """डेटाबेस इनिशियलाइझ करणे (कँडल्स + Greeks + PCR सपोर्टसह)"""
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
        iv REAL,
        delta REAL,
        theta REAL,
        vega REAL,
        gamma REAL,
        PRIMARY KEY (symbol, timestamp)
    )''')
    conn.commit()
    conn.close()


def get_live_expiries():
    """Upstox Expiries API वरून चालू एक्सपायरी लिस्ट मिळवणे"""
    url = f"{BASE_URL}/v2/expired-instruments/expiries?instrument_key=NSE_INDEX%7CNifty%2050"
    try:
        res = session.get(url, headers=HEADERS, timeout=8)
        if res.status_code == 200:
            expiries = res.json().get('data', [])
            if expiries:
                return sorted(list(set(expiries)))
    except Exception as e:
        print(f"[-] Expiries API त्रुटी: {e}", flush=True)

    # फॉलबॅक पर्याय (Live Option Chain वरून)
    try:
        res_live = session.get(f"{BASE_URL}/v2/option/contract?instrument_key=NSE_INDEX%7CNifty%2050", headers=HEADERS, timeout=8)
        if res_live.status_code == 200:
            contracts = res_live.json().get('data', [])
            return sorted(list(set(c['expiry'] for c in contracts if 'expiry' in c)))
    except Exception as e:
        print(f"[-] Live Option Chain त्रुटी: {e}", flush=True)
    return []


def get_today_active_expiry(expiries_list):
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    valid = [e for e in expiries_list if e >= today_str]
    return valid[0] if valid else today_str


def get_current_nifty_spot() -> float:
    """NIFTY 50 चा लाईव्ह स्पॉट भाव मिळवणे"""
    url = f"{BASE_URL}/v3/market-quote/quotes"
    params = {"instrument_key": "NSE_INDEX|Nifty 50"}
    try:
        res = session.get(url, headers=HEADERS, params=params, timeout=6)
        if res.status_code == 200:
            data = res.json().get("data", {})
            for _, val in data.items():
                lp = float(val.get("last_price", 0.0))
                if lp > 0.0:
                    return lp
    except Exception as e:
        print(f"[-] Spot Fetch त्रुटी: {e}", flush=True)

    try:
        inst_key = quote("NSE_INDEX|Nifty 50", safe="")
        c_url = f"{BASE_URL}/v3/historical-candle/intraday/{inst_key}/minutes/1"
        res_c = session.get(c_url, headers=HEADERS, timeout=6)
        if res_c.status_code == 200:
            candles = res_c.json().get("data", {}).get("candles", [])
            if candles:
                return float(candles[0][4])
    except Exception as e:
        print(f"[-] Spot Candle Backup त्रुटी: {e}", flush=True)

    return 0.0


def get_nifty_pcr(active_expiry: str) -> float:
    """आजचा Put-Call Ratio (PCR) मिळवणे"""
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    url = f"{BASE_URL}/v2/market/pcr"
    params = {
        "instrument_key": "NSE_INDEX|Nifty 50",
        "expiry": active_expiry,
        "date": today_str,
        "bucket_interval": 60
    }
    try:
        res = session.get(url, headers=HEADERS, params=params, timeout=6)
        if res.status_code == 200:
            return float(res.json().get("data", {}).get("pcr", 0.0))
    except Exception:
        pass
    return 0.0


def fetch_option_greeks_map(tracked_keys: list):
    """सर्व ट्रॅक केलेल्या स्ट्राइक्सचे Option Greeks एकाच कॉलमध्ये मिळवणे"""
    if not tracked_keys:
        return {}
    keys_str = ",".join(tracked_keys)
    url = f"{BASE_URL}/v3/market-quote/option-greek"
    params = {"instrument_key": keys_str}
    greeks_map = {}
    try:
        res = session.get(url, headers=HEADERS, params=params, timeout=8)
        if res.status_code == 200:
            data = res.json().get("data", {})
            for key, val in data.items():
                inst_token = val.get("instrument_token", "")
                greeks_map[inst_token] = {
                    "iv": float(val.get("iv", 0.0) or 0.0),
                    "delta": float(val.get("delta", 0.0) or 0.0),
                    "theta": float(val.get("theta", 0.0) or 0.0),
                    "vega": float(val.get("vega", 0.0) or 0.0),
                    "gamma": float(val.get("gamma", 0.0) or 0.0)
                }
    except Exception as e:
        print(f"[-] Option Greeks त्रुटी: {e}", flush=True)
    return greeks_map


def fetch_candles_v3_combined(inst_key: str, today_str: str):
    """दोन्ही V3 APIs (Intraday + Historical Fallback) चा वापर करून १-मिनिटांचा डेटा आणणे"""
    enc_key = quote(inst_key, safe='')
    candles = []

    # १. पहिली पसंती: Intraday V3 API
    intraday_url = f"{BASE_URL}/v3/historical-candle/intraday/{enc_key}/minutes/1"
    try:
        r_intra = session.get(intraday_url, headers=HEADERS, timeout=6)
        if r_intra.status_code == 200:
            candles = r_intra.json().get('data', {}).get('candles', [])
    except Exception:
        pass

    # २. दुसरी पसंती (Fallback): Historical V3 API
    if not candles:
        hist_url = f"{BASE_URL}/v3/historical-candle/{enc_key}/minutes/1/{today_str}/{today_str}"
        try:
            r_hist = session.get(hist_url, headers=HEADERS, timeout=6)
            if r_hist.status_code == 200:
                candles = r_hist.json().get('data', {}).get('candles', [])
        except Exception:
            pass

    return candles


def harvest_and_vault():
    init_vault_db()
    print("🌾 [Harvester] प्रगत डेटा गोळा करण्याचे काम सुरू होत आहे...", flush=True)

    if is_market_holiday_today():
        print("🛑 [Harvester] आज मार्केट सुट्टी असल्यामुळे डेटा हार्वेस्टिंग थांबवले आहे.", flush=True)
        return 0, 0.0, 0.0

    if not ACCESS_TOKEN:
        print("[-] [Harvester] UPSTOX_TOKEN आढळले नाही. GitHub Secrets तपासा.", flush=True)
        return 0, 0.0, 0.0

    spot_px = get_current_nifty_spot()
    if spot_px <= 0:
        print("[-] [Harvester] स्पॉट प्राईस मिळाली नाही.", flush=True)
        return 0, 0.0, 0.0

    print(f"[+] [Harvester] चालू NIFTY 50 स्पॉट: {spot_px}", flush=True)

    expiries = get_live_expiries()
    active_expiry = get_today_active_expiry(expiries)
    print(f"[+] [Harvester] ॲक्टिव्ह एक्सपायरी: {active_expiry}", flush=True)

    pcr_value = get_nifty_pcr(active_expiry)
    print(f"[+] [Harvester] चालू PCR: {pcr_value}", flush=True)

    atm_strike = round(spot_px / 50.0) * 50.0
    strikes_to_track = [atm_strike + (i * 50.0) for i in range(-3, 4)]
    print(f"[+] [Harvester] ट्रॅक करायचे स्ट्राइक्स (ATM {atm_strike}): {strikes_to_track}", flush=True)

    # ऑप्शन कॉन्ट्रॅक्ट्स मिळवणे
    url_c = f"{BASE_URL}/v2/option/contract?instrument_key=NSE_INDEX%7CNifty%2050&expiry_date={active_expiry}"
    contracts = []
    try:
        res = session.get(url_c, headers=HEADERS, timeout=8)
        if res.status_code == 200:
            contracts = res.json().get('data', [])
    except Exception as e:
        print(f"[-] Option Contracts त्रुटी: {e}", flush=True)

    if not contracts:
        url_exp = f"{BASE_URL}/v2/expired-instruments/option/contract?instrument_key=NSE_INDEX%7CNifty%2050&expiry_date={active_expiry}"
        try:
            r_exp = session.get(url_exp, headers=HEADERS, timeout=8)
            if r_exp.status_code == 200:
                contracts = r_exp.json().get('data', [])
        except Exception:
            pass

    selected_contracts = [c for c in contracts if float(c.get('strike_price', 0)) in strikes_to_track]
    tracked_keys = [c.get('instrument_key') for c in selected_contracts if c.get('instrument_key')]

    # सर्व स्ट्राइक्सचे Option Greeks गोळा करणे
    greeks_data = fetch_option_greeks_map(tracked_keys)

    records_to_insert = []
    today_str = datetime.now(IST).strftime("%Y-%m-%d")

    for c in selected_contracts:
        strike_val = float(c.get('strike_price', 0))
        inst_key = c.get('instrument_key')
        sym = c.get('trading_symbol')
        opt_type = c.get('instrument_type', '').upper()

        grk = greeks_data.get(inst_key, {"iv": 0.0, "delta": 0.0, "theta": 0.0, "vega": 0.0, "gamma": 0.0})
        candles = fetch_candles_v3_combined(inst_key, today_str)

        for bar in candles:
            records_to_insert.append((
                today_str, bar[0], sym, inst_key, active_expiry,
                strike_val, opt_type, float(bar[1]), float(bar[2]),
                float(bar[3]), float(bar[4]), int(bar[5]),
                int(bar[6]) if len(bar) > 6 and bar[6] is not None else 0,
                grk["iv"], grk["delta"], grk["theta"], grk["vega"], grk["gamma"]
            ))
        time.sleep(0.04)

    if records_to_insert:
        conn = sqlite3.connect(DB_FILE)
        conn.executemany('''
        INSERT OR REPLACE INTO nifty_options_1m 
        (date, timestamp, symbol, instrument_key, expiry, strike, option_type, 
         open, high, low, close, volume, oi, iv, delta, theta, vega, gamma)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', records_to_insert)
        conn.commit()
        conn.close()
        print(f"✅ [Harvester] {len(records_to_insert)} कँडल्स (सहित Greeks) व्हॉल्टमध्ये यशस्वीरीत्या साठवल्या.", flush=True)
        return len(records_to_insert), spot_px, pcr_value
    else:
        print("ℹ️ [Harvester] साठवण्यासाठी नवीन कँडल्स सापडल्या नाहीत.", flush=True)
        return 0, spot_px, pcr_value


if __name__ == "__main__":
    count, spot, pcr = harvest_and_vault()
    current_time = datetime.now(IST).strftime("%d-%m-%Y %I:%M %p")

    if count > 0:
        msg = (
            f"🌾 *NIFTY Harvester V3 Complete Report*\n\n"
            f"✅ *स्थिती:* डेटा आणि ग्रीक्स यशस्वी गोळा झाले!\n"
            f"📊 *साठवलेल्या कँडल्स:* `{count}`\n"
            f"🎯 *NIFTY स्पॉट:* `{spot}`\n"
            f"⚖️ *PCR (Put-Call Ratio):* `{round(pcr, 2)}`\n"
            f"⏰ *वेळ:* `{current_time} IST`\n"
            f"📁 *डेटाबेस:* `nifty_vault.db` (OHLC + Greeks) अद्ययावत."
        )
    else:
        msg = (
            f"ℹ️ *NIFTY Harvester Alert*\n\n"
            f"हार्वेस्टर रन झाला, परंतु डेटा गोळा झाला नाही (सुट्टी किंवा मार्केट बंद असू शकते).\n"
            f"⏰ *वेळ:* `{current_time} IST`"
        )

    send_telegram_alert(msg)
