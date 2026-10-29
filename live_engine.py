import io
import math
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
import pandas as pd
import requests

# ==============================================================================
# कॉन्फिगरेशन व क्रेडेंशियल्स
# ==============================================================================
DB_FILE = 'nifty_vault.db'
UPSTOX_TOKEN = os.getenv('UPSTOX_TOKEN', 'a429fb9b-6faf-4c3c-b070-78cae68237c4')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

UPSTOX_HEADERS = {
    'Accept': 'application/json',
    'Authorization': f'Bearer {UPSTOX_TOKEN}',
}

LOT_SIZE = 65
DELTA_TARGET = 0.52
MIN_OPT_SL_PTS = 10.0
IST = timezone(timedelta(hours=5, minutes=30))

def init_database():
    conn = sqlite3.connect(DB_FILE)
    conn.execute('''
    CREATE TABLE IF NOT EXISTS nifty_options_1m (
        date TEXT,
        timestamp TEXT,
        symbol TEXT,
        strike REAL,
        option_type TEXT,
        open REAL,
        high REAL,
        low REAL,
        close REAL,
        volume INTEGER,
        oi INTEGER,
        price_change REAL,
        change_oi INTEGER,
        buildup TEXT,
        delta REAL,
        gamma REAL,
        theta REAL,
        vega REAL,
        PRIMARY KEY (symbol, timestamp)
    )
    ''')
    conn.commit()
    conn.close()

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram Log]:\n{text}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'Markdown'}, timeout=10)
    except Exception as e:
        print(f"[-] Telegram Error: {e}")

# ==============================================================================
# ग्रीक्स आणि बिल्डअप
# ==============================================================================
def norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

def calculate_greeks(spot, strike, expiry_days, volatility, opt_type, r=0.07):
    if expiry_days <= 0 or volatility <= 0 or spot <= 0 or strike <= 0:
        return {'delta': 0.0, 'gamma': 0.0, 'theta': 0.0, 'vega': 0.0}
    t = max(expiry_days, 0.5) / 365.0
    v_sqrt_t = volatility * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * volatility * volatility) * t) / v_sqrt_t
    d2 = d1 - v_sqrt_t
    gamma = norm_pdf(d1) / (spot * v_sqrt_t)
    vega = (spot * norm_pdf(d1) * math.sqrt(t)) / 100.0
    if opt_type.upper() in ['CE', 'CALL']:
        delta = norm_cdf(d1)
        theta = (-(spot * norm_pdf(d1) * volatility) / (2.0 * math.sqrt(t)) - r * strike * math.exp(-r * t) * norm_cdf(d2)) / 365.0
    else:
        delta = norm_cdf(d1) - 1.0
        theta = (-(spot * norm_pdf(d1) * volatility) / (2.0 * math.sqrt(t)) + r * strike * math.exp(-r * t) * norm_cdf(-d2)) / 365.0
    return {'delta': round(delta, 4), 'gamma': round(gamma, 6), 'theta': round(theta, 2), 'vega': round(vega, 2)}

# ==============================================================================
# इन्स्ट्रुमेंट्स लोड करणे
# ==============================================================================
def get_active_instruments(target_date: str):
    try:
        res_m = requests.get('https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz', timeout=30)
        df_m = pd.read_csv(io.BytesIO(res_m.content), compression='gzip')
        strike_col = 'strike_price' if 'strike_price' in df_m.columns else 'strike'
        opt = df_m[(df_m['exchange'] == 'NSE_FO') & (df_m['name'].str.upper() == 'NIFTY') & (df_m['instrument_type'] == 'OPTIDX')].copy()
        opt['expiry'] = pd.to_datetime(opt['expiry'])
        opt['strike'] = opt[strike_col].astype(float)
        
        upcoming = opt[opt['expiry'] >= pd.Timestamp(target_date)].sort_values('expiry')
        current_expiry = upcoming['expiry'].iloc[0] if not upcoming.empty else opt['expiry'].max()
        days_to_expiry = max(1, (current_expiry - pd.Timestamp(target_date)).days)
        
        # मुख्य ATM स्ट्राइक रेंज
        strikes = [24000, 24050, 24100, 24150, 24200, 24250, 24300, 24350, 24400, 24450, 24500]
        selected = opt[(opt['expiry'] == current_expiry) & (opt['strike'].isin(strikes))]
        return selected, days_to_expiry
    except Exception as e:
        print(f"[-] Instrument load error: {e}")
        return pd.DataFrame(), 1

# ==============================================================================
# १-मिनिट लाईव्ह पोलिंग व सेव्ह
# ==============================================================================
def fetch_and_store_latest(selected, today_str, days_to_expiry):
    all_rows = []
    for _, row in selected.iterrows():
        key = quote(row['instrument_key'], safe='')
        url = f"https://api.upstox.com/v2/historical-candle/{key}/1minute/{today_str}/{today_str}"
        r = requests.get(url, headers=UPSTOX_HEADERS)
        if r.status_code != 200 or not r.json().get('data', {}).get('candles', []):
            url = f"https://api.upstox.com/v3/historical-candle/{key}/1minute/{today_str}/{today_str}"
            r = requests.get(url, headers=UPSTOX_HEADERS)
            
        if r.status_code == 200:
            candles = r.json().get('data', {}).get('candles', [])
            if candles:
                c = candles[0] # सर्वात नवीन कॅन्डल
                all_rows.append({
                    'date': today_str,
                    'timestamp': c[0],
                    'symbol': row['tradingsymbol'],
                    'strike': row['strike'],
                    'option_type': 'CE' if 'CE' in str(row['tradingsymbol']).upper() else 'PE',
                    'open': float(c[1]),
                    'high': float(c[2]),
                    'low': float(c[3]),
                    'close': float(c[4]),
                    'volume': int(c[5]),
                    'oi': int(c[6]) if len(c) > 6 and c[6] is not None else 0
                })
        time.sleep(0.03)
        
    if not all_rows:
        return 0

    df_new = pd.DataFrame(all_rows)
    df_new['price_change'] = 0.0
    df_new['change_oi'] = 0
    df_new['buildup'] = 'Neutral'
    
    greeks = [calculate_greeks(24200.0, r['strike'], days_to_expiry, 0.14, r['option_type']) for _, r in df_new.iterrows()]
    df_final = pd.concat([df_new, pd.DataFrame(greeks)], axis=1)

    conn = sqlite3.connect(DB_FILE)
    df_final.to_sql('nifty_options_1m', conn, if_exists='append', index=False)
    conn.commit()
    conn.close()
    return len(df_final)

# ==============================================================================
# मुख्य लाईव्ह लूप (०९:१५ ते १५:३०)
# ==============================================================================
def live_monitor():
    init_database()
    now_ist = datetime.now(IST)
    today_str = now_ist.strftime('%Y-%m-%d')
    
    send_telegram(f"🟢 *NIFTY Live Strategy Engine सुरू झाला!*\n📅 तारीख: `{today_str}`\n⏱️ वेळ: `{now_ist.strftime('%H:%M:%S')}`")
    
    selected_inst, dte = get_active_instruments(today_str)
    if selected_inst.empty:
        send_telegram("❌ इन्स्ट्रुमेंट्स लोड करता आले नाहीत. कृपया क्रेडेंशियल्स तपासा.")
        return

    active_trade = None
    morning_trades = 0
    afternoon_trades = 0
    cooldown_until = None

    while True:
        curr_time = datetime.now(IST)
        hhmm = curr_time.strftime('%H:%M')

        # ३:३० ला बाजार संपताच लूप बंद होईल
        if hhmm >= '15:30':
            if active_trade:
                send_telegram(f"⏱️ *EOD Force Exit:* `{active_trade['symbol']}` बाजार बंद झाला.")
            send_telegram("🏁 *आजचा लाईव्ह ट्रेडिंग दिवस पूर्ण झाला.* EOD रिपोर्ट सेव्ह झाला.")
            break

        # डेटा फेच करा
        fetch_and_store_latest(selected_inst, today_str, dte)

        # डेटाबेसमधून डेटा तपासा
        conn = sqlite3.connect(DB_FILE)
        df_all = pd.read_sql_query(f"SELECT * FROM nifty_options_1m WHERE timestamp LIKE '{today_str}%'", conn)
        conn.close()

        if not df_all.empty:
            df_all['timestamp'] = pd.to_datetime(df_all['timestamp'])
            
            # १. चालू ट्रेड असेल तर SL/Target तपासा
            if active_trade:
                curr_sym_data = df_all[df_all['symbol'] == active_trade['symbol']].sort_values('timestamp')
                if not curr_sym_data.empty:
                    latest_bar = curr_sym_data.iloc[-1]
                    if latest_bar['low'] <= active_trade['opt_sl']:
                        send_telegram(f"🛑 *STOP LOSS HIT!*\n`{active_trade['symbol']}`\nएक्झिट भाव: `₹{active_trade['opt_sl']}` | तोटा: `₹{(active_trade['opt_sl'] - active_trade['entry_price']) * LOT_SIZE:,.2f}`")
                        active_trade = None
                        cooldown_until = curr_time + timedelta(minutes=15)
                    elif latest_bar['high'] >= active_trade['opt_target']:
                        send_telegram(f"🎯 *TARGET HIT!*\n`{active_trade['symbol']}`\nएक्झिट भाव: `₹{active_trade['opt_target']}` | नफा: `₹{(active_trade['opt_target'] - active_trade['entry_price']) * LOT_SIZE:,.2f}`")
                        active_trade = None
                        cooldown_until = curr_time + timedelta(minutes=15)

            # २. नवीन सिग्नल तपासणे
            is_morning = ('09:40' <= hhmm <= '11:30') and (morning_trades < 1)
            is_afternoon = ('12:30' <= hhmm <= '14:30') and (afternoon_trades < 2)

            if not active_trade and (is_morning or is_afternoon):
                if not cooldown_until or curr_time >= cooldown_until:
                    # नवीनतम बार तपासा
                    latest_ts = df_all['timestamp'].max()
                    snap = df_all[df_all['timestamp'] == latest_ts]
                    
                    for _, row in snap.iterrows():
                        sub = df_all[df_all['symbol'] == row['symbol']].sort_values('timestamp')
                        if len(sub) >= 20:
                            sma_vol = sub['volume'].rolling(20).mean().iloc[-1]
                            if (row['volume'] >= 1.30 * sma_vol) and (70.0 <= row['close'] <= 320.0) and (row['close'] > row['open']):
                                ep = float(row['close'])
                                sl_pts = max(MIN_OPT_SL_PTS, 12.0)
                                target_pts = sl_pts * 1.5
                                
                                active_trade = {
                                    'symbol': row['symbol'],
                                    'entry_price': ep,
                                    'opt_sl': round(ep - sl_pts, 2),
                                    'opt_target': round(ep + target_pts, 2)
                                }
                                if is_morning:
                                    morning_trades += 1
                                else:
                                    afternoon_trades += 1

                                send_telegram(
                                    f"🚨 *LIVE ENTRY TRIGGER!*\n\n"
                                    f"📌 *इन्स्ट्रुमेंट:* `{row['symbol']}`\n"
                                    f"💵 *एंट्री भाव:* `₹{ep}`\n"
                                    f"🎯 *टार्गेट:* `₹{active_trade['opt_target']}`\n"
                                    f"🛑 *स्टॉप लॉस:* `₹{active_trade['opt_sl']}`\n"
                                    f"⏰ *वेळ:* `{hhmm}`"
                                )
                                break

        # दर ६० सेकंदांनी पुन्हा तपासा
        time.sleep(60)

if __name__ == '__main__':
    live_monitor()
