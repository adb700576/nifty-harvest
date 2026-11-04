import sqlite3
import pandas as pd
from strategy_vwap_1m import VWAP1MinStrategy

DB_FILE = 'nifty_vault.db'

def test_today_vwap():
    print("==================================================", flush=True)
    print("📊 [VWAP 1-Min Strategy: आजची चाचणी (04-Nov-2026)]", flush=True)
    print("==================================================", flush=True)

    conn = sqlite3.connect(DB_FILE)
    
    # १. आजचा NIFTY Spot / Index 1-min डेटा
    df_fut = pd.read_sql_query(
        "SELECT timestamp, open, high, low, close, volume FROM nifty_index_1m ORDER BY timestamp ASC",
        conn
    )
    
    # २. आजचा Options + Greeks डेटा
    df_options = pd.read_sql_query(
        "SELECT timestamp, symbol, strike, option_type, open, high, low, close, volume, oi, delta, iv FROM nifty_options_1m ORDER BY timestamp ASC",
        conn
    )

    # ३. आजचा PCR
    df_sentiment = pd.read_sql_query(
        "SELECT date, pcr FROM market_sentiment_summary",
        conn
    )
    conn.close()

    if df_fut.empty or df_options.empty:
        print("[-] व्हॉल्टमध्ये आजचा डेटा सापडला नाही.", flush=True)
        return

    print(f"✅ डेटा लोड झाला: Index Candles = {len(df_fut)}, Option Candles = {len(df_options)}", flush=True)
    today_pcr = df_sentiment['pcr'].iloc[-1] if not df_sentiment.empty else 1.0
    print(f"⚖️ आजचा नोंदवलेला PCR: {round(today_pcr, 3)}", flush=True)

    # ४. VWAP स्ट्रॅटेजी एक्झिक्युशन
    strat = VWAP1MinStrategy(risk_reward_ratio=2.0, min_delta=0.30, max_delta=0.40)
    trades = strat.run_backtest(df_fut, df_options, df_sentiment)

    # ५. निकाल
    if not trades.empty:
        print("\n🎯 [आज निघालेले ट्रेड्स]:\n", flush=True)
        print(trades.to_string(index=False), flush=True)
        total_pnl = trades['नफा/तोटा %'].sum()
        print(f"\n💰 एकूण P&L: {round(total_pnl, 2)}%\n", flush=True)
    else:
        print("\nℹ️ आज VWAP लाईनजवळ ०.३०-०.४० डेल्टा नियमांनुसार कोणताही कँडलस्टिक पॅटर्न कन्फर्म झाला नाही.", flush=True)

if __name__ == "__main__":
    test_today_vwap()
