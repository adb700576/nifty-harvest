import sqlite3
import pandas as pd
import numpy as np

DB_FILE = 'nifty_vault.db'

def run_signal_test():
    conn = sqlite3.connect(DB_FILE)
    
    # १. ताज्या तारखेचा डेटा वाचणे
    query = """
    SELECT timestamp, symbol, strike, option_type, open, high, low, close, volume, oi, delta, iv
    FROM nifty_options_1m
    ORDER BY timestamp ASC;
    """
    df = pd.read_sql_query(query, conn)
    conn.close()

    if df.empty:
        print("[-] व्हॉल्टमध्ये कोणताही डेटा सापडला नाही. आधी Harvester चालवा.", flush=True)
        return

    # २. उपलब्ध तारखा आणि स्ट्राइक्स तपासणे
    dates = df['timestamp'].str.slice(0, 10).unique()
    latest_date = dates[-1]
    print(f"==================================================")
    print(f"📊 [सिग्नल चाचणी] तारीख: {latest_date}")
    print(f"==================================================")

    df_day = df[df['timestamp'].str.startswith(latest_date)].copy()
    
    # ३. एटीएम (ATM) किंवा २३९०० CE / PE चाचणी
    target_strikes = [23850.0, 23900.0, 23950.0]
    trades = []

    for sym in df_day['symbol'].unique():
        sub = df_day[df_day['symbol'] == sym].sort_values('timestamp').reset_index(drop=True)
        if len(sub) < 30:
            continue

        strike_val = sub['strike'].iloc[0]
        if strike_val not in target_strikes:
            continue

        opt_type = sub['option_type'].iloc[0]
        
        # स्ट्रॅटेजी लॉजिक: पहिल्या १५ मिनिटांचा (09:15-09:30) ब्रेकआउट
        first_15m = sub.iloc[:15]
        breakout_level = first_15m['high'].max()
        support_level = first_15m['low'].min()
        
        in_trade = False
        entry_price = 0.0
        entry_time = ""
        target = 0.0
        sl = 0.0

        for i in range(15, len(sub)):
            row = sub.iloc[i]
            cur_close = row['close']
            cur_time = row['timestamp'][11:16]

            # Buy Signal Trigger (Breakout)
            if not in_trade and cur_close > breakout_level:
                in_trade = True
                entry_price = cur_close
                entry_time = cur_time
                target = entry_price * 1.15  # +१५% टार्गेट
                sl = entry_price * 0.90      # -१०% स्टॉपलॉस
                continue

            # Exit Check
            if in_trade:
                pnl_pct = ((cur_close - entry_price) / entry_price) * 100
                if cur_close >= target:
                    trades.append({
                        "Symbol": sym, "Type": opt_type, "Entry Time": entry_time,
                        "Exit Time": cur_time, "Entry": round(entry_price, 2),
                        "Exit": round(cur_close, 2), "Status": "🎯 TARGET HIT",
                        "P&L %": round(pnl_pct, 2)
                    })
                    in_trade = False
                elif cur_close <= sl:
                    trades.append({
                        "Symbol": sym, "Type": opt_type, "Entry Time": entry_time,
                        "Exit Time": cur_time, "Entry": round(entry_price, 2),
                        "Exit": round(cur_close, 2), "Status": "🛑 SL HIT",
                        "P&L %": round(pnl_pct, 2)
                    })
                    in_trade = False
                elif i == len(sub) - 1: # दिवसअखेर एक्झिट (15:25)
                    trades.append({
                        "Symbol": sym, "Type": opt_type, "Entry Time": entry_time,
                        "Exit Time": cur_time, "Entry": round(entry_price, 2),
                        "Exit": round(cur_close, 2), "Status": "⏱️ EOD CLOSE",
                        "P&L %": round(pnl_pct, 2)
                    })
                    in_trade = False

    # ४. निकाल दाखवणे
    if trades:
        res_df = pd.DataFrame(trades)
        print("\n📈 [निघालेले ट्रेड्स आणि निकाल]:\n")
        print(res_df.to_string(index=False))
        total_pnl = res_df['P&L %'].sum()
        print(f"\n💰 एकूण अंदाजित P&L: {round(total_pnl, 2)}%\n")
    else:
        print("\nℹ️ निवडलेल्या नियमांनुसार या स्ट्राइक्सवर आज कोणताही ब्रेकआउट ट्रेड ट्रिगर झाला नाही.\n")

if __name__ == "__main__":
    run_signal_test()
