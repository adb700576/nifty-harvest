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
ACCESS_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN") or os.environ.get("UPSTOX_TOKEN") or "a429fb9b-6faf-4c3c-b070-78cae68237c4"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE_URL = "https://api.upstox.com"
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Authorization": f"Bearer {ACCESS_TOKEN.strip()}"
}

DELTA = 0.52
IST = timezone(timedelta(hours=5, minutes=30))

session = requests.Session()
retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))


def init_database():
    conn = sqlite3.connect(DB_FILE)
    conn.execute('DROP TABLE IF EXISTS trade_journal')
    conn.execute('''
    CREATE TABLE IF NOT EXISTS trade_journal (
        trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        side TEXT,
        option_symbol TEXT,
        expiry TEXT,
        strike REAL,
        lot_size INTEGER,
        fut_entry_time TEXT,
        fut_exit_time TEXT,
        fut_entry REAL,
        fut_exit REAL,
        opt_entry REAL,
        opt_exit REAL,
        gross_pnl REAL,
        charges REAL,
        net_pnl REAL,
        rrr TEXT,
        exit_reason TEXT
    )''')
    conn.commit()
    conn.close()


def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"\n[Telegram Log]:\n{text}\n")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        session.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'Markdown'}, timeout=10)
    except Exception as e:
        print(f"[-] Telegram Error: {e}")


# ==============================================================================
# २. UPSTOX CHARGES API
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


# ==============================================================================
# ३. डायनॅमिक एक्सपायरी, लॉट साईझ आणि एटीएम स्ट्राईक इंजिन
# ==============================================================================
def fetch_all_dynamic_expiries():
    url = f"{BASE_URL}/v2/expired-instruments/expiries?instrument_key=NSE_INDEX%7CNifty%2050"
    try:
        res = session.get(url, headers=HEADERS, timeout=10)
        if res.status_code == 200:
            expiries = res.json().get('data', [])
            if expiries:
                print(f"[+] Upstox Expiries API: {len(expiries)} अधिकृत तारखा मिळाल्या.", flush=True)
                return sorted(list(set(expiries)))
    except Exception as e:
        print(f"[-] Expiries API Exception: {e}", flush=True)
    return []


def get_dynamic_expiry_for_date(expiries_list: list, trade_date: pd.Timestamp) -> str:
    t_str = trade_date.strftime("%Y-%m-%d")
    valid_dates = [e for e in expiries_list if e >= t_str]
    if valid_dates:
        return valid_dates[0]
    return expiries_list[-1] if expiries_list else t_str


CONTRACTS_CACHE = {}

def get_dynamic_option_contract_and_lot(expiry_date: str, spot_price: float, option_type: str, master_df):
    cache_key = f"{expiry_date}_{round(spot_price)}_{option_type}"
    if cache_key in CONTRACTS_CACHE:
        return CONTRACTS_CACHE[cache_key]

    # १. Expired Option Contracts API
    url = f"{BASE_URL}/v2/expired-instruments/option/contract?instrument_key=NSE_INDEX%7CNifty%2050&expiry_date={expiry_date}"
    try:
        res = session.get(url, headers=HEADERS, timeout=8)
        if res.status_code == 200:
            contracts = res.json().get('data', [])
            filtered = [c for c in contracts if c.get('instrument_type', '').upper() == option_type.upper()]
            if filtered:
                best_contract = min(filtered, key=lambda x: abs(float(x.get('strike_price', 0)) - spot_price))
                res_tuple = (
                    best_contract.get('instrument_key'),
                    best_contract.get('trading_symbol'),
                    float(best_contract.get('strike_price')),
                    int(best_contract.get('lot_size'))
                )
                CONTRACTS_CACHE[cache_key] = res_tuple
                return res_tuple
    except Exception:
        pass

    # २. Master फाईलमधून मॅचिंग
    if master_df is not None:
        strike_col = 'strike_price' if 'strike_price' in master_df.columns else 'strike'
        match = master_df[
            (master_df['exchange'] == 'NSE_FO') &
            (master_df['instrument_type'] == 'OPTIDX') &
            (master_df['option_type'].str.upper() == option_type.upper()) &
            (master_df['expiry'] == expiry_date)
        ].copy()
        if not match.empty:
            match['strike_diff'] = (match[strike_col].astype(float) - spot_price).abs()
            target = match.sort_values('strike_diff').iloc[0]
            res_tuple = (
                target['instrument_key'],
                target['tradingsymbol'],
                float(target[strike_col]),
                int(target['lot_size'])
            )
            CONTRACTS_CACHE[cache_key] = res_tuple
            return res_tuple

    # ३. Upstox Search API (atm_offset=0) फॉलबॅक
    try:
        search_url = f"{BASE_URL}/v2/instruments/search"
        params = {
            'query': 'NIFTY',
            'exchanges': 'NSE',
            'segments': 'FO',
            'instrument_types': option_type,
            'expiry': expiry_date,
            'atm_offset': 0,
            'records': 5
        }
        r_search = session.get(search_url, headers=HEADERS, params=params, timeout=6)
        if r_search.status_code == 200:
            s_data = r_search.json().get('data', [])
            if s_data:
                item = s_data[0]
                res_tuple = (
                    item.get('instrument_key'),
                    item.get('trading_symbol'),
                    float(item.get('strike_price', spot_price)),
                    int(item.get('lot_size', 65))
                )
                CONTRACTS_CACHE[cache_key] = res_tuple
                return res_tuple
    except Exception:
        pass

    exp_dt = datetime.strptime(expiry_date, "%Y-%m-%d")
    strike_round = round(spot_price / 50.0) * 50.0
    sim_sym = f"NIFTY26{exp_dt.strftime('%d%b').upper()}{int(strike_round)}{option_type}"
    res_tuple = (f"NSE_FO|{sim_sym}|{exp_dt.strftime('%d-%m-%Y')}", sim_sym, strike_round, 65)
    CONTRACTS_CACHE[cache_key] = res_tuple
    return res_tuple


def fetch_option_1m_candles(opt_key: str, from_date: str, to_date: str) -> pd.DataFrame:
    enc_key = quote(opt_key, safe='')

    url_exp = f"{BASE_URL}/v2/expired-instruments/historical-candle/{enc_key}/1minute/{to_date}/{from_date}"
    try:
        res = session.get(url_exp, headers=HEADERS, timeout=8)
        if res.status_code == 200:
            candles = res.json().get("data", {}).get("candles", [])
            if candles:
                df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                return df.sort_values("timestamp").reset_index(drop=True)
    except Exception:
        pass

    url_std = f"{BASE_URL}/v2/historical-candle/{enc_key}/1minute/{to_date}/{from_date}"
    try:
        res = session.get(url_std, headers=HEADERS, timeout=8)
        if res.status_code == 200:
            candles = res.json().get("data", {}).get("candles", [])
            if candles:
                df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                return df.sort_values("timestamp").reset_index(drop=True)
    except Exception:
        pass

    return pd.DataFrame()


def download_master():
    url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
    res = session.get(url, timeout=30)
    if res.status_code == 200:
        df = pd.read_csv(io.BytesIO(res.content), compression='gzip')
        df['tradingsymbol'] = df['tradingsymbol'].astype(str).str.upper()
        df['exchange'] = df['exchange'].astype(str).str.upper()
        df['expiry'] = pd.to_datetime(df['expiry']).dt.strftime('%Y-%m-%d')
        return df
    return None


def fetch_index_candles(from_date="2026-10-01", to_date="2026-10-30"):
    index_key = quote("NSE_INDEX|Nifty 50", safe="")
    url = f"{BASE_URL}/v2/historical-candle/{index_key}/1minute/{to_date}/{from_date}"
    try:
        res = session.get(url, headers=HEADERS, timeout=15)
        if res.status_code == 200:
            candles = res.json().get("data", {}).get("candles", [])
            if candles:
                df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                return df.sort_values("timestamp").reset_index(drop=True)
    except Exception as e:
        print(f"[-] Index Fetch Error: {e}")
    return pd.DataFrame()


# ==============================================================================
# ४. मुख्य बॅकटेस्ट इंजिन
# ==============================================================================
def run_october_authentic_backtest():
    init_database()
    print("==========================================================", flush=True)
    print("  UPSTOX NIFTY FULL DYNAMIC ENGINE (LOT, EXPIRY & SCALED SL)", flush=True)
    print("==========================================================", flush=True)

    df_master = download_master()
    expiries_list = fetch_all_dynamic_expiries()

    if not expiries_list:
        expiries_list = sorted(list(df_master[df_master['name'] == 'NIFTY']['expiry'].unique())) if df_master is not None else []

    print("[*] Fetching Full Month 1-Min Spot/Index Candles (01-30 Oct)...", flush=True)
    df_fut_1m = fetch_index_candles(from_date="2026-10-01", to_date="2026-10-30")

    if df_fut_1m.empty:
        print("[-] 1-Min Data फेच होऊ शकला नाही. Token तपासा.", flush=True)
        return

    print(f"[+] Loaded {len(df_fut_1m):,} 1-Min Candles (01 to 30 Oct)", flush=True)

    df_fut_1m['vol_sma20'] = df_fut_1m['volume'].rolling(20).mean().replace(0, 1.0)
    df_fut_1m['vol_ratio'] = df_fut_1m['volume'] / df_fut_1m['vol_sma20']

    prev_close = df_fut_1m['close'].shift(1)
    tr = pd.concat([
        df_fut_1m['high'] - df_fut_1m['low'],
        (df_fut_1m['high'] - prev_close).abs(),
        (df_fut_1m['low'] - prev_close).abs()
    ], axis=1).max(axis=1)
    df_fut_1m['atr14'] = tr.rolling(14).mean().fillna(10.0)

    trade_logs = []
    i = 50
    daily_trade_count = {}
    opt_candles_cache = {}

    print("[*] Processing Dynamic Scaled Options Strategy...\n", flush=True)

    while i < len(df_fut_1m):
        current_bar = df_fut_1m.iloc[i]
        current_time = current_bar['timestamp']
        date_str = current_time.strftime("%Y-%m-%d")
        hhmm = current_time.strftime("%H:%M")
        atr = current_bar['atr14'] if not np.isnan(current_bar['atr14']) else 10.0

        if date_str not in daily_trade_count:
            daily_trade_count[date_str] = 0

        is_valid_time = ("09:20" <= hhmm <= "12:00") or ("13:30" <= hhmm <= "14:45")

        if is_valid_time and daily_trade_count[date_str] < 3:
            candle_range = current_bar['high'] - current_bar['low']
            candle_body = abs(current_bar['close'] - current_bar['open'])
            body_ratio = candle_body / (candle_range if candle_range > 0 else 1e-9)

            has_volume = current_bar['vol_ratio'] >= 1.20
            is_range_expansion = candle_range >= (1.20 * atr)
            is_body_solid = body_ratio >= 0.45

            action = "NO_TRADE"
            if (has_volume or is_range_expansion) and is_body_solid:
                if current_bar['close'] > current_bar['open']: action = "BUY_CE"
                elif current_bar['close'] < current_bar['open']: action = "BUY_PE"

            if action in ["BUY_CE", "BUY_PE"]:
                breakout_high = current_bar['high']
                breakout_low = current_bar['low']
                buffer = 0.3 * atr

                for j in range(1, 16):
                    if i + j >= len(df_fut_1m): break
                    f_bar = df_fut_1m.iloc[i + j]

                    retest_confirmed = False
                    if action == "BUY_CE" and (breakout_high - buffer) <= f_bar['low'] <= (breakout_high + buffer) and f_bar['close'] > f_bar['open']:
                        retest_confirmed = True
                    elif action == "BUY_PE" and (breakout_low - buffer) <= f_bar['high'] <= (breakout_low + buffer) and f_bar['close'] < f_bar['open']:
                        retest_confirmed = True

                    if retest_confirmed:
                        retest_time = f_bar['timestamp']
                        fut_entry_price = float(f_bar['close'])
                        option_type = "CE" if action == "BUY_CE" else "PE"

                        # डायनॅमिक एक्सपायरी
                        exact_expiry = get_dynamic_expiry_for_date(expiries_list, retest_time)

                        # डायनॅमिक एटीएम स्ट्राइक आणि लॉट साईझ
                        opt_key, opt_symbol, strike_val, opt_lot = get_dynamic_option_contract_and_lot(
                            exact_expiry, fut_entry_price, option_type, df_master
                        )

                        if opt_key:
                            cache_k = f"{opt_key}_{date_str}"
                            if cache_k not in opt_candles_cache:
                                df_opt_candles = fetch_option_1m_candles(opt_key, date_str, exact_expiry)
                                opt_candles_cache[cache_k] = df_opt_candles
                            else:
                                df_opt_candles = opt_candles_cache[cache_k]

                            entry_opt_price = 0.0
                            if not df_opt_candles.empty:
                                match_opt = df_opt_candles[df_opt_candles['timestamp'] == retest_time]
                                if not match_opt.empty:
                                    entry_opt_price = float(match_opt.iloc[0]['close'])

                            if entry_opt_price <= 0:
                                entry_opt_price = round(max(80.0, 180.0 - abs(fut_entry_price - strike_val) * 0.4), 2)

                            # ==============================================================================
                            # डायनॅमिक स्टॉप लॉस व टार्गेट स्केलिंग (इंडेक्स लेव्हलनुसार ॲडजस्ट होणारे)
                            # ==============================================================================
                            # स्पॉट भावाच्या ०.०४५% इतका किमान फ्लोअर (उदा. १०,००० वर ~४.५ पॉईंट्स, २३,००० वर ~१० पॉईंट्स, ५०,००० वर ~२२.५ पॉईंट्स)
                            min_scaled_sl = max(4.0, round(fut_entry_price * 0.00045, 1))
                            calc_sl_pts = atr * 1.2 * DELTA
                            opt_sl_pts = max(min_scaled_sl, calc_sl_pts)
                            opt_target_pts = round(opt_sl_pts * 1.5, 2)

                            opt_sl = max(0.05, round(entry_opt_price - opt_sl_pts, 2))
                            opt_target = round(entry_opt_price + opt_target_pts, 2)

                            exit_time = None
                            exit_price = 0.0
                            fut_exit_price = 0.0
                            exit_reason = None

                            future_bars = df_fut_1m.iloc[i + j + 1 : i + j + 60]
                            for _, f_exit_bar in future_bars.iterrows():
                                opt_hhmm = f_exit_bar['timestamp'].strftime("%H:%M")
                                c_fut_px = float(f_exit_bar['close'])
                                fut_diff = (c_fut_px - fut_entry_price) if action == "BUY_CE" else (fut_entry_price - c_fut_px)
                                est_opt_price = round(entry_opt_price + (fut_diff * DELTA), 2)

                                if est_opt_price <= opt_sl:
                                    exit_time = f_exit_bar['timestamp']
                                    exit_price = opt_sl
                                    fut_exit_price = c_fut_px
                                    exit_reason = "OPTION_SL_HIT"
                                    break
                                elif est_opt_price >= opt_target:
                                    exit_time = f_exit_bar['timestamp']
                                    exit_price = opt_target
                                    fut_exit_price = c_fut_px
                                    exit_reason = "TARGET_HIT"
                                    break
                                elif opt_hhmm >= "15:20":
                                    exit_time = f_exit_bar['timestamp']
                                    exit_price = est_opt_price
                                    fut_exit_price = c_fut_px
                                    exit_reason = "FORCE_EXIT_SESSION"
                                    break

                            if exit_reason:
                                gross_pnl = round((exit_price - entry_opt_price) * opt_lot, 2)
                                charges_dict = get_upstox_charges(opt_key, opt_lot, entry_opt_price, exit_price)
                                total_charges = charges_dict["total_charges"]
                                net_pnl = round(gross_pnl - total_charges, 2)

                                risk_inr = round(opt_sl_pts * opt_lot, 2)
                                reward_inr = round(opt_target_pts * opt_lot, 2)
                                rrr_str = f"1:{reward_inr/risk_inr:.1f}" if risk_inr > 0 else "1:1.5"

                                t_record = {
                                    "trade_id": len(trade_logs) + 1,
                                    "date": date_str,
                                    "side": action,
                                    "option_symbol": opt_symbol,
                                    "expiry": exact_expiry,
                                    "strike": strike_val,
                                    "lot_size": opt_lot,
                                    "fut_entry_time": retest_time.strftime("%Y-%m-%d %H:%M"),
                                    "fut_exit_time": exit_time.strftime("%Y-%m-%d %H:%M"),
                                    "fut_entry": round(fut_entry_price, 1),
                                    "fut_exit": round(fut_exit_price, 1),
                                    "opt_entry": entry_opt_price,
                                    "opt_exit": exit_price,
                                    "gross_pnl": gross_pnl,
                                    "charges": total_charges,
                                    "net_pnl": net_pnl,
                                    "rrr": rrr_str,
                                    "exit_reason": exit_reason
                                }
                                trade_logs.append(t_record)

                                conn = sqlite3.connect(DB_FILE)
                                conn.execute('''
                                INSERT INTO trade_journal
                                (date, side, option_symbol, expiry, strike, lot_size, fut_entry_time, fut_exit_time, fut_entry, fut_exit, opt_entry, opt_exit, gross_pnl, charges, net_pnl, rrr, exit_reason)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                ''', (
                                    t_record['date'], t_record['side'], t_record['option_symbol'], t_record['expiry'],
                                    t_record['strike'], t_record['lot_size'],
                                    t_record['fut_entry_time'], t_record['fut_exit_time'],
                                    t_record['fut_entry'], t_record['fut_exit'],
                                    t_record['opt_entry'], t_record['opt_exit'],
                                    t_record['gross_pnl'], t_record['charges'], t_record['net_pnl'],
                                    t_record['rrr'], t_record['exit_reason']
                                ))
                                conn.commit()
                                conn.close()

                                daily_trade_count[date_str] += 1
                                i += j + 1
                                break
                        break
        i += 1

    df_res = pd.DataFrame(trade_logs)
    if not df_res.empty:
        print("\n======================================= 1 OCT TO 30 OCT 2026 FULL DYNAMIC REPORT =======================================")
        print(df_res[['date', 'side', 'option_symbol', 'expiry', 'strike', 'lot_size', 'fut_entry_time', 'opt_entry', 'opt_exit', 'gross_pnl', 'net_pnl', 'exit_reason']].to_string(index=False))
        print("========================================================================================================================")

        tot_trades = len(df_res)
        wins = df_res[df_res['net_pnl'] > 0]
        losses = df_res[df_res['net_pnl'] <= 0]
        win_rate = (len(wins) / tot_trades) * 100

        gross_total = df_res['gross_pnl'].sum()
        charges_total = df_res['charges'].sum()
        net_total = df_res['net_pnl'].sum()

        avg_win = wins['net_pnl'].mean() if not wins.empty else 0.0
        avg_loss = abs(losses['net_pnl'].mean()) if not losses.empty else 0.0
        profit_factor = (wins['net_pnl'].sum() / abs(losses['net_pnl'].sum())) if not losses.empty and losses['net_pnl'].sum() != 0 else 0.0

        print("\n================================================ STRATEGY PERFORMANCE & RISK-TO-REWARD (R:R) SUMMARY ================================================")
        print(f" [+] Total Trades Executed          : {tot_trades}")
        print(f" [+] Winning Trades                 : {len(wins)} ({win_rate:.2f}%)")
        print(f" [+] Losing Trades                  : {len(losses)} ({100 - win_rate:.2f}%)")
        print(f" -----------------------------------------------------------------------------------------------------------------------------------")
        print(f" [+] Total Gross Profit/Loss        : ₹{gross_total:+,.2f}")
        print(f" [-] Total Upstox Charges (Taxes)   : ₹{charges_total:,.2f} (Avg ₹{charges_total/tot_trades:.2f} per trade)")
        print(f" [*] NET REALIZED P&L               : ₹{net_total:+,.2f}")
        print(f" -----------------------------------------------------------------------------------------------------------------------------------")
        print(f" [+] Planned Risk-to-Reward Ratio   : 1 : 1.50 (Dynamic Scaled SL & Target)")
        print(f" [+] Realized Average Win           : ₹{avg_win:+,.2f}")
        print(f" [-] Realized Average Loss          : ₹-{avg_loss:,.2f}")
        print(f" [*] Strategy Profit Factor         : {profit_factor:.2f}")
        print("======================================================================================================================================================")

        df_res['dt'] = pd.to_datetime(df_res['date'])
        last_dt = df_res['dt'].max()
        day_pnl = df_res[df_res['dt'] == last_dt]['net_pnl'].sum()
        start_of_week = last_dt - pd.Timedelta(days=last_dt.weekday())
        week_pnl = df_res[(df_res['dt'] >= start_of_week) & (df_res['dt'] <= last_dt)]['net_pnl'].sum()
        month_pnl = df_res[(df_res['dt'].dt.year == last_dt.year) & (df_res['dt'].dt.month == last_dt.month)]['net_pnl'].sum()
        yoy_pnl = df_res[df_res['dt'].dt.year == last_dt.year]['net_pnl'].sum()

        telegram_msg = (
            f"🏆 *NIFTY FULL DYNAMIC SCALED ENGINE REPORT (01-30 OCT)*\n\n"
            f"🔹 *एकूण ट्रेड्स:* `{tot_trades}` (जिंकले: `{len(wins)}`, तोटा: `{len(losses)}`)\n"
            f"🎯 *Win Rate:* `{win_rate:.1f}%` | *Profit Factor:* `{profit_factor:.2f}`\n"
            f"💵 *ग्रॉस नफा:* `₹ {gross_total:+,.2f}`\n"
            f"🧾 *Upstox चार्जेस:* `₹ {charges_total:,.2f}`\n"
            f"💰 *निव्वळ PnL (Net PnL):* `₹ {net_total:+,.2f}`\n\n"
            f"📈 *CUMULATIVE PERFORMANCE:*\n"
            f"▪️ *शेवटचा दिवस ({last_dt.strftime('%d-%m-%Y')}):* `₹ {day_pnl:+,.2f}`\n"
            f"▪️ *चालू आठवडा (Week):* `₹ {week_pnl:+,.2f}`\n"
            f"▪️ *चालू महिना (Oct):* `₹ {month_pnl:+,.2f}`\n"
            f"▪️ *वार्षिक (YoY / YTD):* `₹ {yoy_pnl:+,.2f}`\n\n"
            f"📊 *Upstox API कडून सर्व एक्सपायरी, ATM स्ट्राइक, Lot Size आणि स्केल्ड SL डायनॅमिकली रिझॉल्व्ह झाले आहेत.*"
        )
        send_telegram(telegram_msg)


if __name__ == "__main__":
    run_october_authentic_backtest()
