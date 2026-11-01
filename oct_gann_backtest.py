import io
import math
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import numpy as np
import pandas as pd
import requests

# ==============================================================================
# १. ऑथेंटिकेशन आणि कॉन्फिगरेशन
# ==============================================================================
ACCESS_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN") or os.environ.get("UPSTOX_TOKEN", "").strip()
BASE_URL = "https://api.upstox.com"
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Authorization": f"Bearer {ACCESS_TOKEN}"
}

DELTA = 0.52
LOT_SIZE = 65
BUFFER_PCT = 0.001          # ०.१% गॅन झोन बफर
MIN_AWAY_BARS = 15          # ब्रेकआउटनंतर किमान १५ मिनिटे लांब राहणे
LOCAL_SWING_BARS = 8        # स्थानिक स्विंग शोधण्यासाठी मागील ८ मिनिटांची विंडो

# ==============================================================================
# २. गॅन स्क्वेअर ऑफ ९ लेव्हल्स
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
# ३. १-मिनिट कॅन्डल फेचर आणि सेंटिमेंट
# ==============================================================================
def fetch_october_index_candles():
    index_key = quote("NSE_INDEX|Nifty 50", safe="")
    url = f"{BASE_URL}/v2/historical-candle/{index_key}/1minute/2026-10-30/2026-10-01"
    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        if res.status_code == 200:
            candles = res.json().get("data", {}).get("candles", [])
            if candles:
                df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df = df.sort_values("timestamp").reset_index(drop=True)
                
                df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
                df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
                df['vol_change'] = df['volume'] * np.where(df['close'] >= df['open'], 1, -1)
                df['cum_sentiment'] = df['vol_change'].rolling(window=15, min_periods=1).sum()
                return df
    except Exception as e:
        print(f"[-] Fetch त्रुटी: {e}")
    return pd.DataFrame()

# ==============================================================================
# ४. रिव्हर्सल फेनोमेना (Rejection Wick / Engulfing Confirmation)
# ==============================================================================
def is_valid_bullish_phenomenon(df_window):
    bar = df_window.iloc[-1]
    prev = df_window.iloc[-2]
    c_range = bar['high'] - bar['low']
    if c_range <= 2.0:
        return False
        
    lower_wick = min(bar['open'], bar['close']) - bar['low']
    has_rejection_wick = (lower_wick / c_range) >= 0.35 and bar['close'] >= bar['open']
    is_engulfing = (bar['close'] > prev['high']) and (bar['close'] > bar['open'])
    return has_rejection_wick or is_engulfing

def is_valid_bearish_phenomenon(df_window):
    bar = df_window.iloc[-1]
    prev = df_window.iloc[-2]
    c_range = bar['high'] - bar['low']
    if c_range <= 2.0:
        return False
        
    upper_wick = bar['high'] - max(bar['open'], bar['close'])
    has_rejection_wick = (upper_wick / c_range) >= 0.35 and bar['close'] <= bar['open']
    is_engulfing = (bar['close'] < prev['low']) and (bar['close'] < bar['open'])
    return has_rejection_wick or is_engulfing

# ==============================================================================
# ५. अचूक स्थानिक स्विंग SL इंजिन (True Local Swing High/Low)
# ==============================================================================
def run_gann_october_backtest():
    print("====================================================================")
    print("   NIFTY GANN SQUARE OF 9 (ACCURATE LOCAL SWING SL & TRG = SL * 2)  ")
    print("====================================================================")

    df_all = fetch_october_index_candles()
    if df_all.empty:
        print("[-] डेटा फेच होऊ शकला नाही. UPSTOX_TOKEN तपासा.")
        return

    df_all['date'] = df_all['timestamp'].dt.strftime("%Y-%m-%d")
    unique_dates = sorted(df_all['date'].unique())
    print(f"[+] एकूण {len(df_all):,} कॅन्डल्स आणि {len(unique_dates)} सेशन्स लोड झाले.")

    trade_logs = []

    for d_idx in range(1, len(unique_dates)):
        prev_date = unique_dates[d_idx - 1]
        curr_date = unique_dates[d_idx]

        df_prev = df_all[df_all['date'] == prev_date]
        df_curr = df_all[df_all['date'] == curr_date].copy().reset_index(drop=True)

        if df_prev.empty or len(df_curr) < 50:
            continue

        prev_close = float(df_prev.iloc[-1]['close'])
        gann_levels = calculate_gann_levels(prev_close)

        daily_trades = 0
        used_levels_today = set()
        active_trackers = {lvl: {"state": "IDLE", "break_idx": None, "type": None} for lvl in gann_levels}

        c_len = len(df_curr)
        i = 20
        while i < c_len - 1:
            if daily_trades >= 3:
                break

            bar = df_curr.iloc[i]
            hhmm = bar['timestamp'].strftime("%H:%M")

            if not (("09:25" <= hhmm <= "12:00") or ("13:30" <= hhmm <= "14:45")):
                i += 1
                continue

            is_bullish_env = (bar['close'] >= bar['ema20']) and (bar['cum_sentiment'] >= 0)
            is_bearish_env = (bar['close'] <= bar['ema20']) and (bar['cum_sentiment'] <= 0)

            for lvl in gann_levels:
                buf = lvl * BUFFER_PCT
                zone_low = lvl - buf
                zone_high = lvl + buf
                tracker = active_trackers[lvl]

                if tracker["state"] == "IDLE":
                    if bar['close'] > zone_high:
                        active_trackers[lvl] = {"state": "AWAY", "break_idx": i, "type": "BULL"}
                    elif bar['close'] < zone_low:
                        active_trackers[lvl] = {"state": "AWAY", "break_idx": i, "type": "BEAR"}

                elif tracker["state"] == "AWAY":
                    bars_away = i - tracker["break_idx"]

                    if bars_away >= MIN_AWAY_BARS and lvl not in used_levels_today:
                        window_5m = df_curr.iloc[i-4 : i+1]

                        # रिव्हर्स १: लेव्हलवर री-टेस्ट रिजेक्शन असताना BUY_PE
                        if tracker["type"] == "BULL" and is_bullish_env and (zone_low <= bar['low'] <= zone_high):
                            if is_valid_bullish_phenomenon(window_5m):
                                entry_spot = float(bar['close'])
                                entry_time = bar['timestamp']

                                atm_strike = round(entry_spot / 50.0) * 50
                                real_entry_opt = round(max(45.0, (entry_spot * 0.008)), 2)

                                # स्थानिक स्विंग हाय (संपूर्ण दिवसाचा नव्हे, फक्त री-टेस्ट दरम्यानचा)
                                local_window = df_curr.iloc[max(0, i - LOCAL_SWING_BARS) : i + 1]
                                local_swing_high = local_window['high'].max()
                                
                                # इंडेक्स SL = स्थानिक स्विंग हाय + ६ पॉईंट्स बफर
                                index_sl = local_swing_high + 6.0

                                raw_sl_pts = abs(index_sl - entry_spot) * DELTA
                                # वाजवी इंट्राडे SL मर्यादा: किमान १० पॉईंट्स, कमाल ३५ पॉईंट्स
                                opt_sl_pts = min(35.0, max(10.0, round(raw_sl_pts, 2)))

                                opt_tgt_pts = round(opt_sl_pts * 2.0, 2)
                                active_sl = max(5.0, round(real_entry_opt - opt_sl_pts, 2))
                                opt_tgt = round(real_entry_opt + opt_tgt_pts, 2)

                                exit_time, exit_opt_px, exit_reason = None, 0.0, None

                                for f_idx in range(i + 1, c_len):
                                    f_bar = df_curr.iloc[f_idx]
                                    f_hhmm = f_bar['timestamp'].strftime("%H:%M")

                                    cur_opt = round(real_entry_opt + ((entry_spot - f_bar['close']) * DELTA), 2)

                                    if cur_opt >= real_entry_opt + opt_sl_pts:
                                        active_sl = max(active_sl, real_entry_opt + 2.0)

                                    if cur_opt >= opt_tgt:
                                        exit_time, exit_opt_px, exit_reason = f_bar['timestamp'], opt_tgt, "TARGET_HIT ✅"
                                        break
                                    elif cur_opt <= active_sl:
                                        exit_reason = "BREAKEVEN_EXIT" if active_sl > (real_entry_opt - opt_sl_pts) else "SL_HIT ❌"
                                        exit_time, exit_opt_px = f_bar['timestamp'], active_sl
                                        break
                                    elif f_hhmm >= "15:20":
                                        exit_time, exit_opt_px, exit_reason = f_bar['timestamp'], cur_opt, "FORCE_EOD ⏱️"
                                        break

                                if exit_reason:
                                    pts = round(exit_opt_px - real_entry_opt, 2)
                                    gross = round(pts * LOT_SIZE, 2)
                                    net = round(gross - 52.0, 2)

                                    trade_logs.append({
                                        "date": curr_date,
                                        "side": "BUY_PE",
                                        "strike": f"NIFTY {atm_strike} PE",
                                        "entry_time": entry_time.strftime("%H:%M"),
                                        "exit_time": exit_time.strftime("%H:%M"),
                                        "entry_px": real_entry_opt,
                                        "sl_pts": opt_sl_pts,
                                        "tgt_pts": opt_tgt_pts,
                                        "exit_px": exit_opt_px,
                                        "pts": pts,
                                        "gross_pnl": gross,
                                        "charges": 52.0,
                                        "net_pnl": net,
                                        "exit_reason": exit_reason
                                    })
                                    daily_trades += 1
                                    used_levels_today.add(lvl)
                                    active_trackers[lvl] = {"state": "IDLE", "break_idx": None, "type": None}
                                    i = f_idx
                                    break

                        # रिव्हर्स २: लेव्हलवर री-टेस्ट रिजेक्शन असताना BUY_CE
                        elif tracker["type"] == "BEAR" and is_bearish_env and (zone_low <= bar['high'] <= zone_high):
                            if is_valid_bearish_phenomenon(window_5m):
                                entry_spot = float(bar['close'])
                                entry_time = bar['timestamp']

                                atm_strike = round(entry_spot / 50.0) * 50
                                real_entry_opt = round(max(45.0, (entry_spot * 0.008)), 2)

                                # स्थानिक स्विंग लो
                                local_window = df_curr.iloc[max(0, i - LOCAL_SWING_BARS) : i + 1]
                                local_swing_low = local_window['low'].min()
                                
                                # इंडेक्स SL = स्थानिक स्विंग लो - ६ पॉईंट्स बफर
                                index_sl = local_swing_low - 6.0

                                raw_sl_pts = abs(entry_spot - index_sl) * DELTA
                                opt_sl_pts = min(35.0, max(10.0, round(raw_sl_pts, 2)))

                                opt_tgt_pts = round(opt_sl_pts * 2.0, 2)
                                active_sl = max(5.0, round(real_entry_opt - opt_sl_pts, 2))
                                opt_tgt = round(real_entry_opt + opt_tgt_pts, 2)

                                exit_time, exit_opt_px, exit_reason = None, 0.0, None

                                for f_idx in range(i + 1, c_len):
                                    f_bar = df_curr.iloc[f_idx]
                                    f_hhmm = f_bar['timestamp'].strftime("%H:%M")

                                    cur_opt = round(real_entry_opt + ((f_bar['close'] - entry_spot) * DELTA), 2)

                                    if cur_opt >= real_entry_opt + opt_sl_pts:
                                        active_sl = max(active_sl, real_entry_opt + 2.0)

                                    if cur_opt >= opt_tgt:
                                        exit_time, exit_opt_px, exit_reason = f_bar['timestamp'], opt_tgt, "TARGET_HIT ✅"
                                        break
                                    elif cur_opt <= active_sl:
                                        exit_reason = "BREAKEVEN_EXIT" if active_sl > (real_entry_opt - opt_sl_pts) else "SL_HIT ❌"
                                        exit_time, exit_opt_px = f_bar['timestamp'], active_sl
                                        break
                                    elif f_hhmm >= "15:20":
                                        exit_time, exit_opt_px, exit_reason = f_bar['timestamp'], cur_opt, "FORCE_EOD ⏱️"
                                        break

                                if exit_reason:
                                    pts = round(exit_opt_px - real_entry_opt, 2)
                                    gross = round(pts * LOT_SIZE, 2)
                                    net = round(gross - 52.0, 2)

                                    trade_logs.append({
                                        "date": curr_date,
                                        "side": "BUY_CE",
                                        "strike": f"NIFTY {atm_strike} CE",
                                        "entry_time": entry_time.strftime("%H:%M"),
                                        "exit_time": exit_time.strftime("%H:%M"),
                                        "entry_px": real_entry_opt,
                                        "sl_pts": opt_sl_pts,
                                        "tgt_pts": opt_tgt_pts,
                                        "exit_px": exit_opt_px,
                                        "pts": pts,
                                        "gross_pnl": gross,
                                        "charges": 52.0,
                                        "net_pnl": net,
                                        "exit_reason": exit_reason
                                    })
                                    daily_trades += 1
                                    used_levels_today.add(lvl)
                                    active_trackers[lvl] = {"state": "IDLE", "break_idx": None, "type": None}
                                    i = f_idx
                                    break
            i += 1

    df_res = pd.DataFrame(trade_logs)
    if df_res.empty:
        print("[!] कोणतेही ट्रेड्स ट्रिगर झाले नाहीत.")
        return

    print("\n==================================== GANN SQUARE OF 9 (LOCAL SWING SL REPORT) ====================================")
    print(df_res[['date', 'side', 'strike', 'entry_time', 'exit_time', 'entry_px', 'sl_pts', 'tgt_pts', 'exit_px', 'pts', 'gross_pnl', 'net_pnl', 'exit_reason']].to_string(index=False))
    print("===================================================================================================================")

    tot = len(df_res)
    wins = df_res[df_res['net_pnl'] > 0]
    losses = df_res[df_res['net_pnl'] <= 0]
    wr = (len(wins) / tot) * 100

    print("\n========================================= SUMMARY METRICS =========================================")
    print(f" [+] Total Trades Executed   : {tot}")
    print(f" [+] Winning Trades          : {len(wins)} ({wr:.2f}%)")
    print(f" [+] Losing Trades           : {len(losses)} ({100 - wr:.2f}%)")
    print(f" [+] Gross PnL               : ₹{df_res['gross_pnl'].sum():+,.2f}")
    print(f" [-] Upstox Taxes & Charges  : ₹{df_res['charges'].sum():,.2f}")
    print(f" [*] NET REALIZED P&L        : ₹{df_res['net_pnl'].sum():+,.2f}")
    print("===================================================================================================")

if __name__ == "__main__":
    run_gann_october_backtest()
