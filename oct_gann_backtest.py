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
    "Authorization": f"Bearer {ACCESS_TOKEN}"
}

DELTA = 0.52
LOT_SIZE = 65
BUFFER_PCT = 0.001          # ०.१% बफर
MIN_AWAY_BARS = 15          # ब्रेकआउटनंतर किमान १५ मिनिटे वाट पाहणे

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

def get_next_gann_target(current_level: float, levels: list, direction: str):
    if direction == "BUY_CE":
        for lvl in levels:
            if lvl > current_level:
                return lvl
        return current_level + 300.0
    else:
        for lvl in reversed(levels):
            if lvl < current_level:
                return lvl
        return current_level - 300.0

# ==============================================================================
# ३. १-मिनिट कॅन्डल फेचर
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
                return df.sort_values("timestamp").reset_index(drop=True)
    except Exception as e:
        print(f"[-] Fetch त्रुटी: {e}")
    return pd.DataFrame()

# ==============================================================================
# ४. गॅन बॅकटेस्ट इंजिन (नो थीटा डिके - ओन्ली SL / TARGET / EOD)
# ==============================================================================
def run_gann_october_backtest():
    print("====================================================================")
    print("   NIFTY GANN SQUARE OF 9 + 15-MIN RETEST (NO THETA EXIT - OCT 2026) ")
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

        if df_prev.empty or len(df_curr) < 30:
            continue

        prev_close = float(df_prev.iloc[-1]['close'])
        gann_levels = calculate_gann_levels(prev_close)

        daily_trades = 0
        active_trackers = {lvl: {"state": "IDLE", "break_idx": None, "type": None} for lvl in gann_levels}

        c_len = len(df_curr)
        i = 0
        while i < c_len - 1:
            if daily_trades >= 3:
                break

            bar = df_curr.iloc[i]
            hhmm = bar['timestamp'].strftime("%H:%M")

            if not (("09:20" <= hhmm <= "12:00") or ("13:30" <= hhmm <= "14:45")):
                i += 1
                continue

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

                    if bars_away >= MIN_AWAY_BARS:
                        # बुलिश रिटेस्ट एंट्री
                        if tracker["type"] == "BULL" and (zone_low <= bar['low'] <= zone_high) and bar['close'] > bar['open']:
                            entry_spot = float(bar['close'])
                            entry_time = bar['timestamp']

                            swing_low = df_curr.iloc[tracker["break_idx"]:i+1]['low'].min()
                            index_sl = swing_low - buf
                            index_target = get_next_gann_target(lvl, gann_levels, "BUY_CE")

                            est_entry_opt = 160.0
                            opt_sl_pts = max(10.0, round((entry_spot - index_sl) * DELTA, 2))
                            opt_tgt_pts = round((index_target - entry_spot) * DELTA, 2)
                            opt_sl = max(5.0, round(est_entry_opt - opt_sl_pts, 2))
                            opt_tgt = round(est_entry_opt + opt_tgt_pts, 2)

                            exit_time, exit_opt_px, exit_reason = None, 0.0, None

                            # फॉरवर्ड लूप - फक्त SL, Target किंवा 15:20 EOD
                            for f_idx in range(i + 1, c_len):
                                f_bar = df_curr.iloc[f_idx]
                                f_hhmm = f_bar['timestamp'].strftime("%H:%M")

                                cur_opt = round(est_entry_opt + ((f_bar['close'] - entry_spot) * DELTA), 2)

                                if cur_opt >= opt_tgt:
                                    exit_time, exit_opt_px, exit_reason = f_bar['timestamp'], opt_tgt, "TARGET_HIT"
                                    break
                                elif cur_opt <= opt_sl:
                                    exit_time, exit_opt_px, exit_reason = f_bar['timestamp'], opt_sl, "SL_HIT"
                                    break
                                elif f_hhmm >= "15:20":
                                    exit_time, exit_opt_px, exit_reason = f_bar['timestamp'], cur_opt, "FORCE_EOD"
                                    break

                            if exit_reason:
                                pts = round(exit_opt_px - est_entry_opt, 2)
                                gross = round(pts * LOT_SIZE, 2)
                                net = round(gross - 52.0, 2)

                                trade_logs.append({
                                    "date": curr_date,
                                    "side": "BUY_CE",
                                    "level": lvl,
                                    "entry_time": entry_time.strftime("%H:%M"),
                                    "exit_time": exit_time.strftime("%H:%M"),
                                    "entry_px": est_entry_opt,
                                    "exit_px": exit_opt_px,
                                    "pts": pts,
                                    "gross_pnl": gross,
                                    "charges": 52.0,
                                    "net_pnl": net,
                                    "exit_reason": exit_reason
                                })
                                daily_trades += 1
                                active_trackers[lvl] = {"state": "IDLE", "break_idx": None, "type": None}
                                i = f_idx
                                break

                        # बेअरिश रिटेस्ट एंट्री
                        elif tracker["type"] == "BEAR" and (zone_low <= bar['high'] <= zone_high) and bar['close'] < bar['open']:
                            entry_spot = float(bar['close'])
                            entry_time = bar['timestamp']

                            swing_high = df_curr.iloc[tracker["break_idx"]:i+1]['high'].max()
                            index_sl = swing_high + buf
                            index_target = get_next_gann_target(lvl, gann_levels, "BUY_PE")

                            est_entry_opt = 160.0
                            opt_sl_pts = max(10.0, round((index_sl - entry_spot) * DELTA, 2))
                            opt_tgt_pts = round((entry_spot - index_target) * DELTA, 2)
                            opt_sl = max(5.0, round(est_entry_opt - opt_sl_pts, 2))
                            opt_tgt = round(est_entry_opt + opt_tgt_pts, 2)

                            exit_time, exit_opt_px, exit_reason = None, 0.0, None

                            # फॉरवर्ड लूप - फक्त SL, Target किंवा 15:20 EOD
                            for f_idx in range(i + 1, c_len):
                                f_bar = df_curr.iloc[f_idx]
                                f_hhmm = f_bar['timestamp'].strftime("%H:%M")

                                cur_opt = round(est_entry_opt + ((entry_spot - f_bar['close']) * DELTA), 2)

                                if cur_opt >= opt_tgt:
                                    exit_time, exit_opt_px, exit_reason = f_bar['timestamp'], opt_tgt, "TARGET_HIT"
                                    break
                                elif cur_opt <= opt_sl:
                                    exit_time, exit_opt_px, exit_reason = f_bar['timestamp'], opt_sl, "SL_HIT"
                                    break
                                elif f_hhmm >= "15:20":
                                    exit_time, exit_opt_px, exit_reason = f_bar['timestamp'], cur_opt, "FORCE_EOD"
                                    break

                            if exit_reason:
                                pts = round(exit_opt_px - est_entry_opt, 2)
                                gross = round(pts * LOT_SIZE, 2)
                                net = round(gross - 52.0, 2)

                                trade_logs.append({
                                    "date": curr_date,
                                    "side": "BUY_PE",
                                    "level": lvl,
                                    "entry_time": entry_time.strftime("%H:%M"),
                                    "exit_time": exit_time.strftime("%H:%M"),
                                    "entry_px": est_entry_opt,
                                    "exit_px": exit_opt_px,
                                    "pts": pts,
                                    "gross_pnl": gross,
                                    "charges": 52.0,
                                    "net_pnl": net,
                                    "exit_reason": exit_reason
                                })
                                daily_trades += 1
                                active_trackers[lvl] = {"state": "IDLE", "break_idx": None, "type": None}
                                i = f_idx
                                break
            i += 1

    df_res = pd.DataFrame(trade_logs)
    if df_res.empty:
        print("[!] कोणतेही ट्रेड्स ट्रिगर झाले नाहीत.")
        return

    print("\n==================================== GANN SQUARE OF 9 (01-30 OCT 2026) REPORT ====================================")
    print(df_res[['date', 'side', 'level', 'entry_time', 'exit_time', 'entry_px', 'exit_px', 'pts', 'gross_pnl', 'net_pnl', 'exit_reason']].to_string(index=False))
    print("==================================================================================================================")

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
