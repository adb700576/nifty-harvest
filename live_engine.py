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
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

BASE_URL = "https://api.upstox.com"
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Authorization": f"Bearer {ACCESS_TOKEN}"
}

DELTA = 0.52
IST = timezone(timedelta(hours=5, minutes=30))

session = requests.Session()
retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))

# NSE अधिकृत सायकल (सुट्टी किंवा विकेंडच्या वेळी API रिकामे आल्यास फॉलबॅक)
NSE_FALLBACK_EXPIRIES = [
    "2026-11-03", "2026-11-09", "2026-11-17", "2026-11-23",
    "2026-12-01", "2026-12-29", "2026-12-31"
]


def init_database():
    conn = sqlite3.connect(DB_FILE)
    conn.execute('''
    CREATE TABLE IF NOT EXISTS live_trade_journal (
        trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        side TEXT,
        option_symbol TEXT,
        instrument_token TEXT,
        expiry TEXT,
        strike REAL,
        entry_time TEXT,
        exit_time TEXT,
        entry_px REAL,
        opt_sl REAL,
        opt_target REAL,
        exit_px REAL,
        pts REAL,
        lot_size INTEGER,
        order_id_entry TEXT,
        order_id_exit TEXT,
        charges REAL,
        net_pnl REAL,
        exit_reason TEXT
    )''')
    conn.commit()
    conn.close()


def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"\n[Telegram Alert]:\n{text}\n")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        session.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'Markdown'}, timeout=6)
    except Exception as e:
        print(f"[-] Telegram Error: {e}")


# ==============================================================================
# २. UPSTOX LIVE API & ORDER EXECUTION
# ==============================================================================
def get_upstox_charges(instrument_token: str, quantity: int, buy_price: float, sell_price: float) -> dict:
    url = f"{BASE_URL}/v2/charges/brokerage"
    total_brokerage = 0.0
    total_stt = 0.0
    total_other = 0.0

    bp = {'instrument_token': instrument_token, 'quantity': str(quantity), 'product': 'I', 'transaction_type': 'BUY', 'price': str(buy_price)}
    sp = {'instrument_token': instrument_token, 'quantity': str(quantity), 'product': 'I', 'transaction_type': 'SELL', 'price': str(sell_price)}

    try:
        rb = session.get(url, headers=HEADERS, params=bp, timeout=3)
        if rb.status_code == 200:
            db = rb.json().get("data", {}).get("charges", {})
            total_brokerage += float(db.get("brokerage", 20.0))
            total_stt += float(db.get("stt", 0.0))
            total_other += float(db.get("gst", 0.0)) + float(db.get("stamp_duty", 0.0)) + float(db.get("other_charges", 0.0))

        rs = session.get(url, headers=HEADERS, params=sp, timeout=3)
        if rs.status_code == 200:
            ds = rs.json().get("data", {}).get("charges", {})
            total_brokerage += float(ds.get("brokerage", 20.0))
            total_stt += float(ds.get("stt", 0.0))
            total_other += float(ds.get("gst", 0.0)) + float(ds.get("other_charges", 0.0))

        total_taxes = round(total_brokerage + total_stt + total_other, 2)
        if total_taxes > 0:
            return {"total_charges": total_taxes}
    except Exception:
        pass
    return {"total_charges": 52.0}


def place_upstox_order(instrument_token: str, quantity: int, transaction_type: str = "BUY") -> str:
    url = f"{BASE_URL}/v2/order/place"
    payload = {
        "quantity": quantity,
        "product": "I",
        "validity": "DAY",
        "price": 0,
        "tag": "ALGO_HARVEST",
        "instrument_token": instrument_token,
        "order_type": "MARKET",
        "transaction_type": transaction_type,
        "disclosed_quantity": 0,
        "trigger_price": 0,
        "is_amo": False
    }
    try:
        res = session.post(url, headers=HEADERS, json=payload, timeout=5)
        if res.status_code == 200:
            return res.json().get("data", {}).get("order_id", "ORDER_SUCCESS")
        else:
            print(f"[-] Live Order Reject: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"[-] Live Order Error: {e}")
    return f"SIM_{int(time.time())}"


def get_live_market_quote(instrument_token: str) -> float:
    enc_key = quote(instrument_token, safe='')
    url = f"{BASE_URL}/v2/market-quote/ltp?instrument_key={enc_key}"
    try:
        res = session.get(url, headers=HEADERS, timeout=4)
        if res.status_code == 200:
            data = res.json().get("data", {})
            first_val = next(iter(data.values()), {})
            return float(first_val.get("last_price", 0.0))
    except Exception:
        pass
    return 0.0


# ==============================================================================
# ३. डायनॅमिक एक्सपायरी आणि कॉन्ट्रॅक्ट्स रिझॉल्व्हर (सुधारित)
# ==============================================================================
def fetch_live_expiries():
    url = f"{BASE_URL}/v2/expired-instruments/expiries?instrument_key=NSE_INDEX%7CNifty%2050"
    try:
        res = session.get(url, headers=HEADERS, timeout=8)
        if res.status_code == 200:
            expiries = res.json().get('data', [])
            if expiries:
                return sorted(list(set(expiries)))
    except Exception:
        pass

    try:
        res_live = session.get(f"{BASE_URL}/v2/option/contract?instrument_key=NSE_INDEX%7CNifty%2050", headers=HEADERS, timeout=8)
        if res_live.status_code == 200:
            contracts = res_live.json().get('data', [])
            live_exp = sorted(list(set(c['expiry'] for c in contracts if 'expiry' in c)))
            if live_exp:
                return live_exp
    except Exception:
        pass

    # API कडून रिकामे आल्यास NSE च्या अधिकृत यादीचा वापर
    return NSE_FALLBACK_EXPIRIES


def get_active_expiry_today(expiries_list):
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    # आजच्या नंतरची किंवा आजची (>=) पहिली उपलब्ध एक्सपायरी निवडणे
    valid = [e for e in expiries_list if e >= today_str]
    if valid:
        return valid[0]
    return "2026-11-03"


def get_live_option_contract_and_lot(expiry_date: str, spot_price: float, option_type: str):
    url = f"{BASE_URL}/v2/option/contract?instrument_key=NSE_INDEX%7CNifty%2050&expiry_date={expiry_date}"
    try:
        res = session.get(url, headers=HEADERS, timeout=6)
        if res.status_code == 200:
            contracts = res.json().get('data', [])
            filtered = [c for c in contracts if c.get('instrument_type', '').upper() == option_type.upper()]
            if filtered:
                best = min(filtered, key=lambda x: abs(float(x.get('strike_price', 0)) - spot_price))
                return (
                    best.get('instrument_key'),
                    best.get('trading_symbol'),
                    float(best.get('strike_price')),
                    int(best.get('lot_size'))
                )
    except Exception:
        pass

    # Upstox Search API (atm_offset=0) फॉलबॅक
    try:
        s_url = f"{BASE_URL}/v2/instruments/search"
        p = {'query': 'NIFTY', 'exchanges': 'NSE', 'segments': 'FO', 'instrument_types': option_type, 'expiry': expiry_date, 'atm_offset': 0, 'records': 5}
        r = session.get(s_url, headers=HEADERS, params=p, timeout=5)
        if r.status_code == 200:
            items = r.json().get('data', [])
            if items:
                it = items[0]
                return (it.get('instrument_key'), it.get('trading_symbol'), float(it.get('strike_price', spot_price)), int(it.get('lot_size', 65)))
    except Exception:
        pass

    strike_round = round(spot_price / 50.0) * 50.0
    exp_dt = datetime.strptime(expiry_date, "%Y-%m-%d")
    sym = f"NIFTY26{exp_dt.strftime('%d%b').upper()}{int(strike_round)}{option_type}"
    return f"NSE_FO|{sym}", sym, strike_round, 65


def fetch_intraday_candles() -> pd.DataFrame:
    index_key = quote("NSE_INDEX|Nifty 50", safe="")
    url = f"{BASE_URL}/v2/historical-candle/intraday/{index_key}/1minute"
    try:
        res = session.get(url, headers=HEADERS, timeout=6)
        if res.status_code == 200:
            candles = res.json().get("data", {}).get("candles", [])
            if candles:
                df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                return df.sort_values("timestamp").reset_index(drop=True)
    except Exception as e:
        print(f"[-] Intraday Candle Error: {e}")
    return pd.DataFrame()


# ==============================================================================
# ४. मुख्य लाइव्ह एक्झिक्युशन लूप
# ==============================================================================
def run_live_trading_engine():
    init_database()
    print("==========================================================", flush=True)
    print("  NIFTY 50 LIVE TRADING ENGINE - FULL DYNAMIC SCALING     ", flush=True)
    print("==========================================================", flush=True)

    expiries = fetch_live_expiries()
    today_expiry = get_active_expiry_today(expiries)
    send_telegram(f"🚀 *Nifty Live Trading Bot सुरू झाला आहे!*\n📅 सक्रिय वीकली एक्सपायरी: `{today_expiry}`")

    daily_trade_count = 0
    active_position = None

    while True:
        now_ist = datetime.now(IST)
        hhmm = now_ist.strftime("%H:%M")

        if hhmm < "09:15":
            time.sleep(30)
            continue
        if hhmm >= "15:25":
            if active_position:
                exit_price = get_live_market_quote(active_position['instrument_token']) or active_position['entry_px']
                order_id_exit = place_upstox_order(active_position['instrument_token'], active_position['lot_size'], "SELL")
                pts = round(exit_price - active_position['entry_px'], 2)
                gross = round(pts * active_position['lot_size'], 2)
                charges_dict = get_upstox_charges(active_position['instrument_token'], active_position['lot_size'], active_position['entry_px'], exit_price)
                net = round(gross - charges_dict["total_charges"], 2)

                conn = sqlite3.connect(DB_FILE)
                conn.execute('''
                INSERT INTO live_trade_journal 
                (date, side, option_symbol, instrument_token, expiry, strike, entry_time, exit_time, entry_px, opt_sl, opt_target, exit_px, pts, lot_size, order_id_entry, order_id_exit, charges, net_pnl, exit_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    now_ist.strftime("%Y-%m-%d"), active_position['side'], active_position['symbol'],
                    active_position['instrument_token'], today_expiry, active_position['strike'],
                    active_position['entry_time'], hhmm, active_position['entry_px'],
                    active_position['opt_sl'], active_position['opt_target'], exit_price,
                    pts, active_position['lot_size'], active_position['order_id'], order_id_exit,
                    charges_dict["total_charges"], net, "FORCE_EOD ⏱️"
                ))
                conn.commit()
                conn.close()

                send_telegram(f"⏱️ *EOD Force Exit Triggered!*\nLTP: `₹{exit_price}` | Net PnL: `₹ {net:+,.2f}`")
            print("[*] मार्केट बंद झाले. Engine थांबत आहे.")
            break

        # १. ओपन पोझिशन मॉनिटरिंग (Dynamic SL & Target)
        if active_position:
            curr_price = get_live_market_quote(active_position['instrument_token'])
            if curr_price > 0:
                is_sl = curr_price <= active_position['opt_sl']
                is_tgt = curr_price >= active_position['opt_target']

                if is_sl or is_tgt:
                    exit_reason = "SL_HIT ❌" if is_sl else "TARGET_HIT ✅"
                    order_id_exit = place_upstox_order(active_position['instrument_token'], active_position['lot_size'], "SELL")
                    
                    pts = round(curr_price - active_position['entry_px'], 2)
                    gross = round(pts * active_position['lot_size'], 2)
                    charges_dict = get_upstox_charges(active_position['instrument_token'], active_position['lot_size'], active_position['entry_px'], curr_price)
                    net = round(gross - charges_dict["total_charges"], 2)

                    conn = sqlite3.connect(DB_FILE)
                    conn.execute('''
                    INSERT INTO live_trade_journal 
                    (date, side, option_symbol, instrument_token, expiry, strike, entry_time, exit_time, entry_px, opt_sl, opt_target, exit_px, pts, lot_size, order_id_entry, order_id_exit, charges, net_pnl, exit_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        now_ist.strftime("%Y-%m-%d"), active_position['side'], active_position['symbol'],
                        active_position['instrument_token'], today_expiry, active_position['strike'],
                        active_position['entry_time'], hhmm, active_position['entry_px'],
                        active_position['opt_sl'], active_position['opt_target'], curr_price,
                        pts, active_position['lot_size'], active_position['order_id'], order_id_exit,
                        charges_dict["total_charges"], net, exit_reason
                    ))
                    conn.commit()
                    conn.close()

                    send_telegram(
                        f"🔔 *LIVE TRADE EXIT: {exit_reason}*\n\n"
                        f"🔹 सिम्बॉल: `{active_position['symbol']}`\n"
                        f"💵 एंट्री: `₹{active_position['entry_px']}` ➡️ एक्झिट: `₹{curr_price}`\n"
                        f"🎯 पॉइंट्स: `{pts:+.2f}` | 💰 Net PnL: `₹ {net:+,.2f}`"
                    )
                    active_position = None
                    time.sleep(60)
                    continue

        # २. नवीन ब्रेकआउट व रिटेस्ट शोधणे
        is_trading_window = ("09:20" <= hhmm <= "12:00") or ("13:30" <= hhmm <= "14:45")

        if active_position is None and is_trading_window and daily_trade_count < 3:
            df = fetch_intraday_candles()
            if len(df) >= 25:
                df['vol_sma20'] = df['volume'].rolling(20).mean().replace(0, 1.0)
                df['vol_ratio'] = df['volume'] / df['vol_sma20']
                prev_c = df['close'].shift(1)
                tr = pd.concat([df['high'] - df['low'], (df['high'] - prev_c).abs(), (df['low'] - prev_c).abs()], axis=1).max(axis=1)
                df['atr14'] = tr.rolling(14).mean().fillna(10.0)

                bar = df.iloc[-2]
                c_range = bar['high'] - bar['low']
                c_body = abs(bar['close'] - bar['open'])
                b_ratio = c_body / (c_range if c_range > 0 else 1e-9)

                has_vol = bar['vol_ratio'] >= 1.20
                is_expansion = c_range >= (1.20 * bar['atr14'])
                is_solid = b_ratio >= 0.45

                action = "NO_TRADE"
                if (has_vol or is_expansion) and is_solid:
                    if bar['close'] > bar['open']: action = "BUY_CE"
                    elif bar['close'] < bar['open']: action = "BUY_PE"

                if action in ["BUY_CE", "BUY_PE"]:
                    spot_px = float(bar['close'])
                    opt_type = "CE" if action == "BUY_CE" else "PE"

                    opt_token, opt_sym, strike_v, lot = get_live_option_contract_and_lot(today_expiry, spot_px, opt_type)
                    opt_entry_px = get_live_market_quote(opt_token)

                    if 70.0 <= opt_entry_px <= 320.0:
                        min_scaled_sl = max(4.0, round(spot_px * 0.00045, 1))
                        calc_sl_pts = bar['atr14'] * 1.2 * DELTA
                        opt_sl_pts = max(min_scaled_sl, calc_sl_pts)
                        opt_target_pts = round(opt_sl_pts * 1.5, 2)

                        opt_sl = max(0.05, round(opt_entry_px - opt_sl_pts, 2))
                        opt_target = round(opt_entry_px + opt_target_pts, 2)

                        order_id = place_upstox_order(opt_token, lot, "BUY")
                        daily_trade_count += 1

                        active_position = {
                            "symbol": opt_sym,
                            "instrument_token": opt_token,
                            "side": action,
                            "strike": strike_v,
                            "entry_time": now_ist.strftime("%H:%M"),
                            "entry_px": opt_entry_px,
                            "opt_sl": opt_sl,
                            "opt_target": opt_target,
                            "lot_size": lot,
                            "order_id": order_id
                        }

                        send_telegram(
                            f"⚡ *LIVE TRADE ENTRY EXECUTED!*\n\n"
                            f"🔹 बाजू: *{action}*\n"
                            f"🔹 सिम्बॉल: `{opt_sym}`\n"
                            f"📅 एक्सपायरी: `{today_expiry}`\n"
                            f"💵 भाव: `₹{opt_entry_px}`\n"
                            f"🛑 डायनॅमिक SL: `₹{opt_sl}` (Pts: {opt_sl_pts})\n"
                            f"🎯 टार्गेट: `₹{opt_target}` (Pts: {opt_target_pts})\n"
                            f"📦 लॉट: `{lot}` | ऑर्डर आयडी: `{order_id}`"
                        )

        time.sleep(60 - (datetime.now().second % 60))


if __name__ == "__main__":
    run_live_trading_engine()
