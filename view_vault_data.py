import sqlite3
import pandas as pd

DB_FILE = 'nifty_vault.db'

def inspect_vault():
    conn = sqlite3.connect(DB_FILE)
    
    # १. एकूण कँडल्स आणि स्ट्राइक्सची आकडेवारी
    summary_query = """
    SELECT symbol, option_type, strike, COUNT(*) as total_candles,
           MIN(open) as min_price, MAX(high) as max_price,
           AVG(iv) as avg_iv, AVG(delta) as avg_delta
    FROM nifty_options_1m
    GROUP BY symbol
    ORDER BY strike, option_type;
    """
    df_summary = pd.read_sql_query(summary_query, conn)
    print("\n========== व्हॉल्टमधील स्ट्राइकनिहाय सारांश ==========\n")
    print(df_summary.to_string(index=False))
    
    # २. प्रातिनिधिक ५ कँडल्स (OHLC + Greeks)
    sample_query = """
    SELECT timestamp, symbol, strike, option_type, open, high, low, close, volume, oi, iv, delta, theta
    FROM nifty_options_1m
    LIMIT 10;
    """
    df_sample = pd.read_sql_query(sample_query, conn)
    print("\n========== कँडल्सचा नमुना (OHLC + Greeks) ==========\n")
    print(df_sample.to_string(index=False))
    
    conn.close()

if __name__ == "__main__":
    inspect_vault()
