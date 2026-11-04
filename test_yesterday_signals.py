import sqlite3
import pandas as pd
from strategy_vwap_1m import VWAP1MinStrategy

DB_FILE = 'nifty_vault.db'

def test_today_vwap():
    print("==================================================", flush=True)
    print("📊 [VWAP 1-Min Strategy: Practical Backtest]", flush=True)
    print("==================================================", flush=True)

    conn = sqlite3.connect(DB_FILE)
    df_fut = pd.read_sql_query("SELECT timestamp, open, high, low, close, volume FROM nifty_index_1m ORDER BY timestamp ASC", conn)
    df_options = pd.read_sql_query("SELECT timestamp, symbol, strike, option_type, open, high, low, close, volume, oi FROM nifty_options_1m ORDER BY timestamp ASC", conn)
    df_sentiment = pd.read_sql_query("SELECT date, pcr FROM market_sentiment_summary", conn)
    conn.close()

    if df_fut.empty or df_options.empty:
        print("[-] व्हॉल्टमध्ये डेटा सापडला नाही.", flush=True)
        return

    print(f"✅ डेटा लोड झाला: Index Candles = {len(df_fut)}, Option Candles = {len(df_options)}", flush=True)

    strat = VWAP1MinStrategy(risk_reward_ratio=2.0)
    trades = strat.run_backtest(df_fut, df_options, df_sentiment)

    if not trades.empty:
        print("\n🎯 [आज निघालेले ट्रेड्स]:\n", flush=True)
        print(trades.to_string(index=False), flush=True)
        total_pnl = trades['P&L %'].sum()
        print(f"\n💰 एकूण P&L: {round(total_pnl, 2)}%\n", flush=True)
    else:
        print("\nℹ️ आजच्या सत्रात VWAP पुलबॅक किंवा रीव्हर्सल ट्रिगर झाला नाही.", flush=True)

if __name__ == "__main__":
    test_today_vwap()
