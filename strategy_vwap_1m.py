import sqlite3
import pandas as pd
import numpy as np

class VWAP1MinStrategy:
    def __init__(self, risk_reward_ratio=2.0):
        self.rr_ratio = risk_reward_ratio

    def calculate_vwap(self, df):
        """१-मिनिट डेटावर शुद्ध सत्राचा VWAP काढणे"""
        df = df.copy()
        typical_price = (df['high'] + df['low'] + df['close']) / 3.0
        pv = typical_price * df['volume']
        
        df['date'] = df['timestamp'].str.slice(0, 10)
        df['cum_pv'] = df.groupby('date')[pv.name if pv.name else 'close'].cumsum()
        df['cum_vol'] = df.groupby('date')['volume'].cumsum()
        
        df['vwap'] = np.where(df['cum_vol'] > 0, df['cum_pv'] / df['cum_vol'], df['close'])
        return df

    def run_backtest(self, df_fut, df_options, df_sentiment):
        df_fut = self.calculate_vwap(df_fut)
        
        # स्विंग लेव्हल्स शोधणे (मागील ५ कँडल्सचा रोलिंग हाय आणि लो)
        df_fut['swing_low'] = df_fut['low'].rolling(window=5, min_periods=1).min()
        df_fut['swing_high'] = df_fut['high'].rolling(window=5, min_periods=1).max()

        trades = []
        in_trade = False
        current_trade = {}

        for i in range(1, len(df_fut)):
            prev = df_fut.iloc[i - 1]
            cur = df_fut.iloc[i]
            ts = cur['timestamp']
            cur_vwap = cur['vwap']
            cur_close = cur['close']
            cur_open = cur['open']
            cur_low = cur['low']
            cur_high = cur['high']

            # १. लाँग ट्रिगर्स (CE खरेदी)
            # A. बुलिश रीव्हर्सल (खालून वर क्रॉस करून हिरवा क्लोज)
            bull_reversal = (prev['close'] <= cur_vwap) and (cur_close > cur_vwap) and (cur_close > cur_open)
            # B. बुलिश पुलबॅक (वर असताना VWAP ला स्पर्श करून बाउन्स/सपोर्ट घेतला)
            bull_pullback = (prev['low'] <= cur_vwap * 1.001) and (cur_close > cur_vwap) and (cur_close > cur_open) and (prev['close'] > cur_vwap)

            # २. शॉर्ट ट्रिगर्स (PE खरेदी)
            # A. बेअरिश रीव्हर्सल (वरून खाली तोडून लाल क्लोज)
            bear_reversal = (prev['close'] >= cur_vwap) and (cur_close < cur_vwap) and (cur_close < cur_open)
            # B. बेअरिश पुलबॅक (खाली असताना उसळी मारून VWAP वर रेसिस्टन्स घेऊन खाली लाल क्लोज)
            bear_pullback = (prev['high'] >= cur_vwap * 0.999) and (cur_close < cur_vwap) and (cur_close < cur_open) and (prev['close'] < cur_vwap)

            # --- CE Trade Entry ---
            if not in_trade and (bull_reversal or bull_pullback):
                atm_strike = round(cur_close / 50.0) * 50.0
                opt = df_options[(df_options['timestamp'] == ts) & 
                                 (df_options['strike'] == atm_strike) & 
                                 (df_options['option_type'] == 'CE')]
                
                if not opt.empty:
                    sl_fut = cur['swing_low']
                    risk = cur_close - sl_fut
                    if risk > 4.0:
                        in_trade = True
                        current_trade = {
                            "type": "BUY CE (" + ("Reversal" if bull_reversal else "Pullback") + ")",
                            "symbol": opt.iloc[0]['symbol'],
                            "strike": atm_strike,
                            "entry_time": ts[11:16],
                            "entry_fut": cur_close,
                            "sl_fut": sl_fut,
                            "tgt_fut": cur_close + (risk * self.rr_ratio),
                            "entry_opt": opt.iloc[0]['close'],
                            "date": ts[:10]
                        }
                        continue

            # --- PE Trade Entry ---
            if not in_trade and (bear_reversal or bear_pullback):
                atm_strike = round(cur_close / 50.0) * 50.0
                opt = df_options[(df_options['timestamp'] == ts) & 
                                 (df_options['strike'] == atm_strike) & 
                                 (df_options['option_type'] == 'PE')]
                
                if not opt.empty:
                    sl_fut = cur['swing_high']
                    risk = sl_fut - cur_close
                    if risk > 4.0:
                        in_trade = True
                        current_trade = {
                            "type": "BUY PE (" + ("Reversal" if bear_reversal else "Pullback") + ")",
                            "symbol": opt.iloc[0]['symbol'],
                            "strike": atm_strike,
                            "entry_time": ts[11:16],
                            "entry_fut": cur_close,
                            "sl_fut": sl_fut,
                            "tgt_fut": cur_close - (risk * self.rr_ratio),
                            "entry_opt": opt.iloc[0]['close'],
                            "date": ts[:10]
                        }
                        continue

            # --- Trade Exit Logic ---
            if in_trade:
                trade_type = current_trade['type']
                exit_triggered = False
                exit_reason = ""

                if "BUY CE" in trade_type:
                    if cur_high >= current_trade['tgt_fut']:
                        exit_triggered = True
                        exit_reason = "🎯 TARGET (1:2)"
                    elif cur_low <= current_trade['sl_fut']:
                        exit_triggered = True
                        exit_reason = "🛑 SL HIT (Swing Low)"
                elif "BUY PE" in trade_type:
                    if cur_low <= current_trade['tgt_fut']:
                        exit_triggered = True
                        exit_reason = "🎯 TARGET (1:2)"
                    elif cur_high >= current_trade['sl_fut']:
                        exit_triggered = True
                        exit_reason = "🛑 SL HIT (Swing High)"

                if not exit_triggered and ts[11:16] >= "15:25":
                    exit_triggered = True
                    exit_reason = "⏱️ EOD CLOSE"

                if exit_triggered:
                    opt_exit = df_options[(df_options['timestamp'] == ts) & 
                                          (df_options['symbol'] == current_trade['symbol'])]
                    exit_opt_px = opt_exit.iloc[0]['close'] if not opt_exit.empty else current_trade['entry_opt']
                    pnl_pct = ((exit_opt_px - current_trade['entry_opt']) / current_trade['entry_opt']) * 100.0

                    trades.append({
                        "तारीख": current_trade['date'],
                        "प्रकार": current_trade['type'],
                        "स्ट्राइक": current_trade['strike'],
                        "एन्ट्री वेळ": current_trade['entry_time'],
                        "एक्झिट वेळ": ts[11:16],
                        "फ्युचर्स एन्ट्री": round(current_trade['entry_fut'], 1),
                        "फ्युचर्स एक्झिट": round(cur_close, 1),
                        "ऑप्शन खरेदी भाव": round(current_trade['entry_opt'], 2),
                        "ऑप्शन विक्री भाव": round(exit_opt_px, 2),
                        "निकाल": exit_reason,
                        "P&L %": round(pnl_pct, 2)
                    })
                    in_trade = False
                    current_trade = {}

        return pd.DataFrame(trades)
