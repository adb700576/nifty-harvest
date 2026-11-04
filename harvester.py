import io
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
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
        return

    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown"
        }
        session.post(url, json=payload, timeout=10)
    except Exception:
        pass


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
    """व्हॉल्टमधील सर्व टेबल्स तयार करणे"""
    conn = sqlite3.connect(DB_FILE)
    
    # १. ऑप्शन्स टेबल (OHLC + Greeks + OI)
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

    # २. Spot आणि Futures टेबल
    conn.execute('''
    CREATE TABLE IF NOT EXISTS nifty_index_1m (
        date TEXT,
        timestamp TEXT,
        instrument_type TEXT,
        symbol TEXT,
        open REAL,
        high REAL,
        low REAL,
        close REAL,
        volume INTEGER,
        oi INTEGER,
        PRIMARY KEY (symbol, timestamp)
    )''')

    # ३. PCR आणि मार्केट समरी टेबल
    conn.execute('''
    CREATE TABLE IF NOT EXISTS market_sentiment_summary (
        date TEXT PRIMARY KEY,
        expiry TEXT,
        spot_price REAL,
        pcr REAL,
        total_call_oi INTEGER,
        total_put_oi INTEGER
    )''')
    
    conn.commit()
    conn.close()


def get_live_expiries():
    """
    1. चालू व आगामी सक्रिय एक्सपायरी शोधणे (Option Contracts API)
    2. बॅकअप म्हणून Expired Instruments API तपासणे
    """
    try:
        url_live = f"{BASE_URL}/v2/option/contract?instrument_key=NSE_INDEX%7CNifty%2050"
        res_live = session.get(url_live, headers=HEADERS, timeout=8)
        if res_live.status_code == 200:
            contracts = res_live.json().get('data', [])
            live_expiries = sorted(list(set(c['expiry'] for c in contracts if 'expiry' in c)))
            if live_expiries:
                return live_expiries
    except Exception as e:
        print(f"[-] Live Option Contracts त्रुटी: {e}", flush=True)

    try:
        url_exp = f"{BASE_URL}/v2/expired-instruments/expiries?instrument_key=NSE_INDEX%7CNifty%2050"
        res_exp = session.get(url_exp, headers=HEADERS, timeout=8)
        if res_exp.status_code == 200:
            exp_data = res_exp.json().get('data', [])
            if exp_data:
                return sorted(list(set(exp_data)))
    except Exception as e:
        print(f"[-] Expired Instruments API त्रुटी: {e}", flush=True)

    return []


def get_today_active_expiry(expiries_list):
    """
    आजची किंवा पुढील सर्वात जवळच्या मंगळवारची खरी सक्रिय एक्सपायरी निवडणे
    """
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    
    # आज किंवा आजनंतरच्या सक्रिय एक्सपायरी
    valid_future = [e for e in expiries_list if e >= today_str]
    
    if valid_future:
        selected_expiry = valid_future[0]
        print(f"[+] [Expiry Selector] निवडलेली वैध आगामी एक्सपायरी: {selected_expiry}", flush=True)
        return selected_expiry

    fallback = expiries_list[-1] if expiries_list else today_str
    print(f"[!] [Expiry Selector] फॉलबॅक एक्सपायरी: {fallback}", flush=True)
    return fallback


def get_current_nifty_spot() -> float:
    """NIFTY 50 चा थेट स्पॉट भाव मिळवणे"""
    url = f"{BASE_URL}/v3/market-quote/quotes"
    try:
        res = session.get(url, headers=HEADERS, params={"instrument_key": "NSE_INDEX|Nifty 50"}, timeout=6)
        if res.status_code == 200:
            data = res.json().get("data", {})
            for _, val in data.items():
                lp = float(val.get("last_price", 0.0))
                if lp > 0.0:
                    return lp
    except Exception:
        pass

    try:
        inst_key = quote("NSE_INDEX|Nifty 50", safe="")
        c_url = f"{BASE_URL}/v3/historical-candle/intraday/{inst_key}/minutes/1"
        res_c = session.get(c_url, headers=HEADERS, timeout=6)
        if res_c.status_code == 200:
            candles = res_c.json().get("data", {}).get("candles", [])
            if candles:
                return float(candles[0][4])
    except Exception:
        pass

    return 0.0


def get_nifty_pcr(active_expiry: str) -> float:
    """PCR (Put-Call Ratio) मिळवणे"""
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
    """सर्व ट्रॅक केलेल्या स्ट्राइक्सचे Greeks मिळवणे"""
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
    """Intraday आणि Historical V3 दोन्ही कॉल्स एकत्र करून १-मिनिट कँडल्स मिळवणे"""
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

    # २. दुसरी पसंती: Fallback Historical V3 API
    if not candles:
        hist_url = f"{BASE_URL}/v3/historical-candle/{enc_key}/minutes/1/{today_str}/{today_str}"
        try:
            r_hist = session.get(hist_url, headers=HEADERS, timeout=6)
            if r_hist.status_code == 200:
                candles = r_hist.json().get('data', {}).get('candles', [])
        except Exception:
            pass

    return candles


def harvest_all_to_vault():
    init_vault_db()
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    print(f"🌾 [Vault Harvester] {today_str} चा डेटा गोळा करण्याचे काम सुरू होत आहे...", flush=True)

    if is_market_holiday_today():
        print("🛑 [Harvester] आज मार्केट सुट्टी असल्यामुळे डेटा हार्वेस्टिंग थांबवले आहे.", flush=True)
        return 0, 0.0, 0.0

    if not ACCESS_TOKEN:
        print("[-] [Harvester] UPSTOX_TOKEN आढळले नाही. Secrets तपासा.", flush=True)
        return 0, 0.0, 0.0

    spot_px = get_current_nifty_spot()
    if spot_px <= 0:
        print("[-] [Harvester] स्पॉट प्राईस मिळाली नाही.", flush=True)
        return 0, 0.0, 0.0

    print(f"[+] [Harvester] चालू NIFTY 50 स्पॉट: {spot_px}", flush=True)

    # १. NIFTY Spot च्या 1-Min कँडल्स सेव्ह करणे
    spot_candles = fetch_candles_v3_combined("NSE_INDEX|Nifty 50", today_str)
    spot_records = [
        (today_str, b[0], "SPOT", "NIFTY 50", float(b[1]), float(b[2]), float(b[3]), float(b[4]), int(b[5]), 0)
        for b in spot_candles
    ]

    # २. सक्रिय मंगळवार एक्सपायरी निवडणे
    expiries = get_live_expiries()
    active_expiry = get_today_active_expiry(expiries)
    print(f"[+] [Harvester] ॲक्टिव्ह एक्सपायरी: {active_expiry}", flush=True)

    # ३. PCR मिळवणे
    pcr_value = get_nifty_pcr(active_expiry)
    print(f"[+] [Harvester] चालू PCR: {pcr_value}", flush=True)

    # ४. स्ट्राइक्स फिल्टर करणे (ATM +- 8 स्ट्राइक्स, ज्यात 23600 नक्की येईल)
    atm_strike = round(spot_px / 50.0) * 50.0
    strikes_to_track = [atm_strike + (i * 50.0) for i in range(-8, 9)]
    print(f"[+] [Harvester] ट्रॅक करायचे स्ट्राइक्स (ATM {atm_strike}): {strikes_to_track}", flush=True)

    # ५. निवडलेल्या एक्सपायरीचे Option Contracts मिळवणे
    url_c = f"{BASE_URL}/v2/option/contract?instrument_key=NSE_INDEX%7CNifty%2050&expiry_date={active_expiry}"
    contracts = []
    try:
        res = session.get(url_c, headers=HEADERS, timeout=8)
        if res.status_code == 200:
            contracts = res.json().get('data', [])
    except Exception as e:
        print(f"[-] Option Contracts त्रुटी: {e}", flush=True)

    selected_contracts = [c for c in contracts if float(c.get('strike_price', 0)) in strikes_to_track]
    tracked_keys = [c.get('instrument_key') for c in selected_contracts if c.get('instrument_key')]

    # ६. ग्रीक्स एकाच कॉलमध्ये आणणे
    greeks_data = fetch_option_greeks_map(tracked_keys)

    # ७. सर्व कँडल्स गोळा करणे
    options_records = []
    for c in selected_contracts:
        strike_val = float(c.get('strike_price', 0))
        inst_key = c.get('instrument_key')
        sym = c.get('trading_symbol')
        opt_type = c.get('instrument_type', '').upper()

        grk = greeks_data.get(inst_key, {"iv": 0.0, "delta": 0.0, "theta": 0.0, "vega": 0.0, "gamma": 0.0})
        candles = fetch_candles_v3_combined(inst_key, today_str)

        for bar in candles:
            options_records.append((
                today_str, bar[0], sym, inst_key, active_expiry,
                strike_val, opt_type, float(bar[1]), float(bar[2]),
                float(bar[3]), float(bar[4]), int(bar[5]),
                int(bar[6]) if len(bar) > 6 and bar[6] is not None else 0,
                grk["iv"], grk["delta"], grk["theta"], grk["vega"], grk["gamma"]
            ))
        time.sleep(0.03)

    # ८. डेटाबेसमध्ये सर्व साठवणे
    conn = sqlite3.connect(DB_FILE)
    if spot_records:
        conn.executemany("INSERT OR REPLACE INTO nifty_index_1m VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", spot_records)
    if options_records:
        conn.executemany('''
        INSERT OR REPLACE INTO nifty_options_1m 
        (date, timestamp, symbol, instrument_key, expiry, strike, option_type, 
         open, high, low, close, volume, oi, iv, delta, theta, vega, gamma)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', options_records)
    conn.execute("INSERT OR REPLACE INTO market_sentiment_summary VALUES (?, ?, ?, ?, ?, ?)",
                 (today_str, active_expiry, spot_px, pcr_value, 0, 0))
    conn.commit()
    conn.close()

    total_saved = len(options_records) + len(spot_records)
    if total_saved > 0:
        print(f"✅ व्हॉल्टमध्ये एकूण {total_saved} रेकॉर्ड्स साठवले (Spot: {len(spot_records)}, Options: {len(options_records)}).", flush=True)
    else:
        print("ℹ️ [Harvester] साठवण्यासाठी नवीन कँडल्स सापडल्या नाहीत.", flush=True)

    return total_saved, spot_px, pcr_value


if __name__ == "__main__":
    count, spot, pcr = harvest_all_to_vault()
    current_time = datetime.now(IST).strftime("%d-%m-%Y %I:%M %p")

    if count > 0:
        msg = (
            f"🌾 *NIFTY Vault Complete Harvest*\n\n"
            f"✅ *23600 व सर्व स्ट्राइक्स साठवले!*\n"
            f"📊 *एकूण कँडल्स:* `{count}`\n"
            f"🎯 *NIFTY Spot:* `{spot}`\n"
            f"⚖️ *PCR:* `{round(pcr, 2)}`\n"
            f"⏰ *वेळ:* `{current_time} IST`\n"
            f"📁 Spot, Options, Greeks, PCR सर्व `nifty_vault.db` मध्ये सुरक्षित!"
        )
    else:
        msg = (
            f"ℹ️ *NIFTY Harvester Alert*\n\n"
            f"हार्वेस्टर रन झाला, परंतु डेटा गोळा झाला नाही.\n"
            f"⏰ *वेळ:* `{current_time} IST`"
        )

    send_telegram_alert(msg)
