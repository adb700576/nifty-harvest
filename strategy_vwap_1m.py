import sqlite3
import pandas as pd
import numpy as np

class VWAP1MinStrategy:
    def __init__(self, risk_reward_ratio=2.0, min_delta=0.30, max_delta=0.40):
        self.rr_ratio = risk_reward_ratio  # १:२ रिस्क-रिवॉर्ड
        self.min_delta = min_delta          # ०.३० डेल्टा
        self.max_delta = max_delta          # ०.४० डेल्टा

    def calculate_vwap(self, df):
        """
        NIFTY Futures च्या १-मिनिट कँडल्सवर रोलिंग VWAP काढणे
        सूत्र: Cumulative(Typical Price * Volume) / Cumulative(Volume)
        """
        df = df.copy()
        typical_price = (df['high'] + df['low'] + df['close']) / 3.0
        pv = typical_price * df['volume']
        
        # दिवसाप्रमाणे ग्रुप करून क्युम्युलेटिव्ह व्हॅल्यू काढणे
        df['date'] = df['timestamp'].str.slice(0, 10)
        df['cum_pv'] = df.groupby('date')[pv.name if pv.name else 'close'].cumsum()
        df['cum_vol'] = df.groupby('date')['volume'].cumsum()
        
        df['vwap'] = np.where(df['cum_vol'] > 0, df['cum_pv'] / df['cum_vol'], df['close'])
        return df

    def is_bullish_pattern(self, prev_row, cur_row, vwap_val):
        """
        VWAP जवळ बुलिश पॅटर्न तपासणे (Hammer किंवा Bullish Engulfing)
        """
        c_open, c_high, c_low, c_close = cur_row['open'], cur_row['high'], cur_row['low'], cur_row['close']
        p_open, p_close = prev_row['open'], prev_row['close']

        # कँडल VWAP जवळ किंवा VWAP वर आदळून वर जात आहे का
        is_near_vwap = (abs(c_low - vwap_val) / vwap_val <= 0.0008) or (c_low <= vwap_val <= c_close)
        
        # बुलिश एन्गल्फिंग
        is_engulfing = (p_close < p_open) and (c_close > c_open) and (c_close >= p_open) and (c_open <= p_close)
        
        # हॅमर (मोठी खालची विक / सपोर्ट रिजेक्शन)
        body = abs(c_close - c_open)
        lower_wick = min(c_open, c_close) - c_low
        is_hammer = (lower_wick >= 1.5 * body) and (c_close > c_open) and (c_low <= vwap_val)

        return is_near_vwap and (is_engulfing or is_hammer)

    def is_bearish_pattern(self, prev_row, cur_row, vwap_val):
        """
        VWAP जवळ बेअरिश पॅटर्न तपासणे (Shooting Star किंवा Bearish Engulfing)
        """
        c_open, c_high, c_low, c_close = cur_row['open'], cur_row['high'], cur_row['low'], cur_row['close']
        p_open, p_close = prev_row['open'], prev_row['close']

        # कँडल VWAP जवळ किंवा खाली रिजेक्ट होत आहे का
        is_near_vwap = (abs(c_high - vwap_val) / vwap_val <= 0.0008) or (c_high >= vwap_val >= c_close)

        # बेअरिश एन्गल्फिंग
        is_engulfing = (p_close > p_open) and (c_close < c_open) and (c_close <= p_open) and (c_open >= p_close)

        # शूटिंग स्टार (मोठी वरची विक / रेसिस्टन्स रिजेक्शन)
        body = abs(c_close - c_open)
        upper_wick = c_high - max(c_open, c_close)
        is_shooting_star = (upper_wick >= 1.5 * body) and (c_close < c_open) and (c_high >= vwap_val)

        return is_near_vwap and (is_engulfing or is_shooting_star)

    def select_best_strike(self, df_options, timestamp, opt_type):
        """
        व्हॉल्टमधून ०.३० ते ०.४० Delta मधील सर्वात योग्य स्ट्राइक निवडणे
        """
        snapshot = df_options[(df_options['timestamp'] == timestamp) & 
                              (df_options['option_type'] == opt_type)].copy()
        
        if snapshot.empty:
            return None

        if opt_type == 'CE':
            valid = snapshot[(snapshot['delta'] >= self.min_delta) & (snapshot['delta'] <= self.max_delta)]
        else:
            # पुटसाठी डेल्टा ऋण (-) असल्याने absolute value तपासणे
            valid = snapshot[(snapshot['delta'].abs() >= self.min_delta) & (snapshot['delta'].abs() <= self.max_delta)]

        if not valid.empty:
            target_delta = 0.35 if opt_type == 'CE' else -0.35
            valid['delta_dist'] = (valid['delta'] - target_delta).abs()
            return valid.sort_values('delta_dist').iloc[0]

        return None

    def run_backtest(self, df_fut, df_options, df_sentiment):
        """
        संपूर्ण VWAP स्ट्रॅटेजी रन करून निकाल (Trades) तयार करणे
        """
        df_fut = self.calculate_vwap(df_fut)
        trades = []
        in_trade = False
        current_trade = {}

        pcr_dict = dict(zip(df_sentiment['date'], df_sentiment['pcr']))

        for i in range(1, len(df_fut)):
            prev = df_fut.iloc[i - 1]
            cur = df_fut.iloc[i]
            ts = cur['timestamp']
            d_str = ts[:10]
            cur_pcr = pcr_dict.get(d_str, 1.0)
            vwap = cur['vwap']

            # चालू दिवसाचा Low आणि High
            day_slice = df_fut[df_fut['timestamp'].str.slice(0, 10) == d_str].loc[:i]
            day_low = day_slice['low'].min()
            day_high = day_slice['high'].max()

            # १. लाँग सिग्नल (PCR >= 1.0 आणि VWAP वर बुलिश कँडल)
            if not in_trade and cur_pcr >= 1.0:
                if self.is_bullish_pattern(prev, cur, vwap):
                    entry_fut = cur['close']
                    sl_fut = day_low
                    risk = entry_fut - sl_fut

                    if risk > 5.0:  # किमान वैध स्टॉपलॉस
                        tgt_fut = entry_fut + (risk * self.rr_ratio)
                        picked_opt = self.select_best_strike(df_options, ts, 'CE')

                        if picked_opt is not None:
                            in_trade = True
                            current_trade = {
                                "type": "खरेदी (CE)",
                                "symbol": picked_opt['symbol'],
                                "strike": picked_opt['strike'],
                                "delta": picked_opt['delta'],
                                "entry_time": ts[11:16],
                                "entry_fut": entry_fut,
                                "sl_fut": sl_fut,
                                "tgt_fut": tgt_fut,
                                "entry_opt": picked_opt['close'],
                                "date": d_str
                            }
                            continue

            # २. शॉर्ट सिग्नल (PCR < 1.0 आणि VWAP वर बेअरिश कँडल)
            if not in_trade and cur_pcr < 1.0:
                if self.is_bearish_pattern(prev, cur, vwap):
                    entry_fut = cur['close']
                    sl_fut = day_high
                    risk = sl_fut - entry_fut

                    if risk > 5.0:
                        tgt_fut = entry_fut - (risk * self.rr_ratio)
                        picked_opt = self.select_best_strike(df_options, ts, 'PE')

                        if picked_opt is not None:
                            in_trade = True
                            current_trade = {
                                "type": "खरेदी (PE)",
                                "symbol": picked_opt['symbol'],
                                "strike": picked_opt['strike'],
                                "delta": picked_opt['delta'],
                                "entry_time": ts[11:16],
                                "entry_fut": entry_fut,
                                "sl_fut": sl_fut,
                                "tgt_fut": tgt_fut,
                                "entry_opt": picked_opt['close'],
                                "date": d_str
                            }
                            continue

            # ३. एक्झिट तपासणी
            if in_trade:
                trade_type = current_trade['type']
                exit_triggered = False
                exit_reason = ""

                if trade_type == "खरेदी (CE)":
                    if cur['high'] >= current_trade['tgt_fut']:
                        exit_triggered = True
                        exit_reason = "🎯 टार्गेट पूर्ण (1:2)"
                    elif cur['low'] <= current_trade['sl_fut']:
                        exit_triggered = True
                        exit_reason = "🛑 स्टॉपलॉस हिट (Day Low)"
                elif trade_type == "खरेदी (PE)":
                    if cur['low'] <= current_trade['tgt_fut']:
                        exit_triggered = True
                        exit_reason = "🎯 टार्गेट पूर्ण (1:2)"
                    elif cur['high'] >= current_trade['sl_fut']:
                        exit_triggered = True
                        exit_reason = "🛑 स्टॉपलॉस हिट (Day High)"

                # दिवसअखेर एक्झिट (15:25)
                if not exit_triggered and ts[11:16] >= "15:25":
                    exit_triggered = True
                    exit_reason = "⏱️ दिवसअखेर बंद"

                if exit_triggered:
                    opt_exit = df_options[(df_options['timestamp'] == ts) & 
                                          (df_options['symbol'] == current_trade['symbol'])]
                    exit_opt_px = opt_exit.iloc[0]['close'] if not opt_exit.empty else current_trade['entry_opt']
                    pnl_pct = ((exit_opt_px - current_trade['entry_opt']) / current_trade['entry_opt']) * 100.0

                    trades.append({
                        "तारीख": current_trade['date'],
                        "प्रकार": current_trade['type'],
                        "कॉन्ट्रॅक्ट": current_trade['symbol'],
                        "डेल्टा (Delta)": round(current_trade['delta'], 2),
                        "एन्ट्री वेळ": current_trade['entry_time'],
                        "एक्झिट वेळ": ts[11:16],
                        "फ्युचर्स एन्ट्री": round(current_trade['entry_fut'], 1),
                        "फ्युचर्स एक्झिट": round(cur['close'], 1),
                        "ऑप्शन खरेदी भाव": round(current_trade['entry_opt'], 2),
                        "ऑप्शन विक्री भाव": round(exit_opt_px, 2),
                        "निकाल": exit_reason,
                        "नफा/तोटा %": round(pnl_pct, 2)
                    })
                    in_trade = False
                    current_trade = {}

        return pd.DataFrame(trades)
