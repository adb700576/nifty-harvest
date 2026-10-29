import io
import math
import os
import sqlite3
import time
from datetime import datetime
from urllib.parse import quote
import pandas as pd
import requests

# ==============================================================================
# १. कॉन्फिगरेशन व क्रेडेंशियल्स
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


def init_database():
  conn = sqlite3.connect(DB_FILE)
  conn.execute("""
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
        vega REAL
    )
    """)
  conn.commit()
  conn.close()


# ==============================================================================
# २. टेलिग्राम मेसेज हेल्पर
# ==============================================================================
def send_telegram(text: str):
  if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    print(f'[Telegram Log]:\n{text}')
    return
  url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
  try:
    requests.post(
        url,
        data={
            'chat_id': TELEGRAM_CHAT_ID,
            'text': text,
            'parse_mode': 'Markdown',
        },
        timeout=10,
    )
  except Exception as e:
    print(f'[-] Telegram Error: {e}')


# ==============================================================================
# ३. डेटा संकलन (Data Harvester)
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
  d1 = (
      math.log(spot / strike) + (r + 0.5 * volatility * volatility) * t
  ) / v_sqrt_t
  d2 = d1 - v_sqrt_t
  gamma = norm_pdf(d1) / (spot * v_sqrt_t)
  vega = (spot * norm_pdf(d1) * math.sqrt(t)) / 100.0
  if opt_type.upper() in ['CE', 'CALL']:
    delta = norm_cdf(d1)
    theta = (
        -(spot * norm_pdf(d1) * volatility) / (2.0 * math.sqrt(t))
        - r * strike * math.exp(-r * t) * norm_cdf(d2)
    ) / 365.0
  else:
    delta = norm_cdf(d1) - 1.0
    theta = (
        -(spot * norm_pdf(d1) * volatility) / (2.0 * math.sqrt(t))
        + r * strike * math.exp(-r * t) * norm_cdf(-d2)
    ) / 365.0
  return {
      'delta': round(delta, 4),
      'gamma': round(gamma, 6),
      'theta': round(theta, 2),
      'vega': round(vega, 2),
  }


def harvest_nifty_data(target_date: str):
  print(f'🚀 डेटा डाऊनलोड सुरू झाला: {target_date}')
  try:
    res_m = requests.get(
        'https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz',
        timeout=30,
    )
    df_m = pd.read_csv(io.BytesIO(res_m.content), compression='gzip')
  except Exception as e:
    print(f'[-] मास्टर इन्स्ट्रुमेंट एरर: {e}')
    return 0

  strike_col = 'strike_price' if 'strike_price' in df_m.columns else 'strike'

  opt = df_m[
      (df_m['exchange'] == 'NSE_FO')
      & (df_m['name'].str.upper() == 'NIFTY')
      & (df_m['instrument_type'] == 'OPTIDX')
  ].copy()
  opt['expiry'] = pd.to_datetime(opt['expiry'])
  opt['strike'] = opt[strike_col].astype(float)

  upcoming = opt[opt['expiry'] >= pd.Timestamp(target_date)].sort_values(
      'expiry'
  )
  current_expiry = (
      upcoming['expiry'].iloc[0] if not upcoming.empty else opt['expiry'].max()
  )
  days_to_expiry = max(1, (current_expiry - pd.Timestamp(target_date)).days)

  strikes = [
      22450,
      22500,
      22550,
      22600,
      22650,
      22700,
      22750,
      22800,
      22850,
      22900,
      22950,
  ]
  selected = opt[
      (opt['expiry'] == current_expiry) & (opt['strike'].isin(strikes))
  ]

  all_rows = []
  for _, row in selected.iterrows():
    key = quote(row['instrument_key'], safe='')
    url = f'https://api.upstox.com/v2/historical-candle/{key}/1minute/{target_date}/{target_date}'
    r = requests.get(url, headers=UPSTOX_HEADERS)
    if r.status_code != 200 or not r.json().get('data', {}).get('candles', []):
      url = f'https://api.upstox.com/v3/historical-candle/{key}/1minute/{target_date}/{target_date}'
      r = requests.get(url, headers=UPSTOX_HEADERS)

    if r.status_code == 200:
      for c in r.json().get('data', {}).get('candles', []):
        all_rows.append({
            'date': target_date,
            'timestamp': c[0],
            'symbol': row['tradingsymbol'],
            'strike': row['strike'],
            'option_type': (
                'CE' if 'CE' in str(row['tradingsymbol']).upper() else 'PE'
            ),
            'open': float(c[1]),
            'high': float(c[2]),
            'low': float(c[3]),
            'close': float(c[4]),
            'volume': int(c[5]),
            'oi': int(c[6]) if len(c) > 6 and c[6] is not None else 0,
        })
    time.sleep(0.04)

  if not all_rows:
    print('[-] आजच्या तारखेसाठी अद्याप कोणतीही कॅन्डल उपलब्ध नाही.')
    return 0

  df_all = (
      pd.DataFrame(all_rows)
      .sort_values(by=['symbol', 'timestamp'])
      .reset_index(drop=True)
  )
  df_all['price_change'] = df_all.groupby('symbol')['close'].diff().fillna(0.0)
  df_all['change_oi'] = (
      df_all.groupby('symbol')['oi'].diff().fillna(0).astype(int)
  )

  def calc_buildup(r):
    p, o = r['price_change'], r['change_oi']
    if p > 0 and o > 0:
      return 'Long Buildup'
    if p < 0 and o > 0:
      return 'Short Buildup'
    if p > 0 and o < 0:
      return 'Short Covering'
    if p < 0 and o < 0:
      return 'Long Unwinding'
    return 'Neutral'

  df_all['buildup'] = df_all.apply(calc_buildup, axis=1)

  greeks = [
      calculate_greeks(
          22700.0, r['strike'], days_to_expiry, 0.14, r['option_type']
      )
      for _, r in df_all.iterrows()
  ]
  df_final = pd.concat([df_all, pd.DataFrame(greeks)], axis=1)

  conn = sqlite3.connect(DB_FILE)
  conn.execute(
      f"DELETE FROM nifty_options_1m WHERE timestamp LIKE '{target_date}%'"
  )
  df_final.to_sql('nifty_options_1m', conn, if_exists='append', index=False)
  conn.commit()
  conn.close()

  return len(df_final)


# ==============================================================================
# ४. ब्रोकरेज, टॅक्स व V3 स्ट्रॅटेजी
# ==============================================================================
def get_upstox_charges(
    instrument_token: str, quantity: int, buy_price: float, sell_price: float
) -> dict:
  url = 'https://api.upstox.com/v2/charges/brokerage'
  buy_params = {
      'instrument_token': instrument_token,
      'quantity': str(quantity),
      'product': 'I',
      'transaction_type': 'BUY',
      'price': str(buy_price),
  }
  sell_params = {
      'instrument_token': instrument_token,
      'quantity': str(quantity),
      'product': 'I',
      'transaction_type': 'SELL',
      'price': str(sell_price),
  }
  try:
    res_b = requests.get(
        url, headers=UPSTOX_HEADERS, params=buy_params, timeout=2
    )
    res_s = requests.get(
        url, headers=UPSTOX_HEADERS, params=sell_params, timeout=2
    )
    if res_b.status_code == 200 and res_s.status_code == 200:
      db = res_b.json().get('data', {}).get('charges', {})
      ds = res_s.json().get('data', {}).get('charges', {})
      b = float(db.get('brokerage', 20.0)) + float(ds.get('brokerage', 20.0))
      s = float(db.get('stt', 0.0)) + float(ds.get('stt', 0.0))
      o = (
          float(db.get('gst', 0.0))
          + float(ds.get('gst', 0.0))
          + float(db.get('stamp_duty', 0.0))
          + float(db.get('other_charges', 0.0))
          + float(ds.get('other_charges', 0.0))
      )
      return {'total_charges': round(b + s + o, 2)}
  except Exception:
    pass
  return {'total_charges': 52.0}


def run_strategy(target_date: str):
  conn = sqlite3.connect(DB_FILE)
  df = pd.read_sql_query(
      f"SELECT * FROM nifty_options_1m WHERE timestamp LIKE '{target_date}%'"
      ' ORDER BY timestamp ASC, symbol ASC',
      conn,
  )
  conn.close()

  if df.empty:
    return []

  df['timestamp'] = pd.to_datetime(df['timestamp'])
  df = df.drop_duplicates(subset=['symbol', 'timestamp']).reset_index(drop=True)

  all_processed = []
  for sym in df['symbol'].unique():
    sub = df[df['symbol'] == sym].copy().sort_values('timestamp')
    if len(sub) < 25:
      continue
    sub['vol_sma20'] = sub['volume'].rolling(20).mean().replace(0, 1.0)
    sub['vol_ratio'] = sub['volume'] / sub['vol_sma20']
    prev_c = sub['close'].shift(1)
    tr = pd.concat([
        sub['high'] - sub['low'],
        (sub['high'] - prev_c).abs(),
        (sub['low'] - prev_c).abs(),
    ], axis=1).max(axis=1)
    sub['atr14'] = tr.rolling(14).mean().fillna(5.0)
    all_processed.append(sub)

  if not all_processed:
    return []

  df_data = pd.concat(all_processed, axis=0).sort_values(
      by=['timestamp', 'symbol']
  )

  trades = []
  morning_trades = 0
  afternoon_trades = 0
  active = None
  cooldown = None

  for ts in df_data['timestamp'].unique():
    hhmm = ts.strftime('%H:%M')
    snap = df_data[df_data['timestamp'] == ts]

    if active:
      curr = snap[snap['symbol'] == active['symbol']]
      if not curr.empty:
        bar = curr.iloc[0]
        exit_p, reason = None, ''
        if bar['low'] <= active['opt_sl']:
          exit_p, reason = active['opt_sl'], 'SL_HIT ❌'
        elif bar['high'] >= active['opt_target']:
          exit_p, reason = active['opt_target'], 'TARGET_HIT ✅'
        elif hhmm >= '15:20':
          exit_p, reason = float(bar['close']), 'FORCE_EOD ⏱️'

        if exit_p:
          gross = round((exit_p - active['entry_price']) * LOT_SIZE, 2)
          ch = get_upstox_charges(
              active['symbol'], LOT_SIZE, active['entry_price'], exit_p
          )['total_charges']
          net = round(gross - ch, 2)
          trades.append({
              'side': active['side'],
              'symbol': active['symbol'],
              'entry_time': active['entry_time'].strftime('%H:%M'),
              'exit_time': ts.strftime('%H:%M'),
              'entry_px': active['entry_price'],
              'exit_px': round(exit_p, 2),
              'pts': round(exit_p - active['entry_price'], 2),
              'net_pnl': net,
              'reason': reason,
          })
          active = None
          cooldown = ts + pd.Timedelta(minutes=15)
      continue

    if cooldown and ts < cooldown:
      continue

    is_m = ('09:40' <= hhmm <= '11:30') and (morning_trades < 1)
    is_a = ('12:30' <= hhmm <= '14:30') and (afternoon_trades < 2)

    if (active is None) and (is_m or is_a):
      cands = []
      for _, row in snap.iterrows():
        if not (70.0 <= row['close'] <= 320.0):
          continue
        c_range = row['high'] - row['low']
        b_ratio = abs(row['close'] - row['open']) / (
            c_range if c_range > 0 else 1e-9
        )
        if (
            (row['vol_ratio'] >= 1.30 or c_range >= 1.20 * row['atr14'])
            and b_ratio >= 0.45
            and row['close'] > row['open']
        ):
          if row['buildup'] in ['Long Buildup', 'Short Covering']:
            cands.append(row)

      if cands:
        cdf = pd.DataFrame(cands)
        cdf['atm_dist'] = (cdf['delta'].abs() - 0.50).abs()
        best = cdf.sort_values(by='atm_dist').iloc[0]
        ep = float(best['close'])
        sl_pts = max(MIN_OPT_SL_PTS, best['atr14'] * 1.2 * DELTA_TARGET)
        active = {
            'symbol': best['symbol'],
            'side': f"BUY_{best['option_type']}",
            'entry_time': ts,
            'entry_price': ep,
            'opt_sl': max(0.05, round(ep - sl_pts, 2)),
            'opt_target': round(ep + (sl_pts * 1.50), 2),
        }
        if is_m:
          morning_trades += 1
        else:
          afternoon_trades += 1

  return trades


# ==============================================================================
# ५. मुख्य नियंत्रक (Main Execution)
# ==============================================================================
if __name__ == '__main__':
  init_database()

  # आजची तारीख
  today = datetime.now().strftime('%Y-%m-%d')

  # १. डेटा संकलित करणे
  candles_count = harvest_nifty_data(today)

  # चाचणीसाठी: आज सकाळी जर कॅन्डल्स नसतील, तर कालच्या (२८ ऑक्टोबर) डेटावर चाचणी घेईल
  test_date = today
  if candles_count == 0:
    print(
        'ℹ️ आजचा डेटा उपलब्ध नसल्याने कालच्या (2026-10-28) डेटावर टेस्ट करत'
        ' आहे...'
    )
    test_date = '2026-10-28'
    candles_count = harvest_nifty_data(test_date)

  # २. स्ट्रॅटेजी रन करणे
  trades = run_strategy(test_date)

  # ३. टेलिग्रामवर निकाल पाठवणे
  if trades:
    tot = len(trades)
    wins = len([t for t in trades if t['net_pnl'] > 0])
    losses = tot - wins
    tot_pnl = sum(t['net_pnl'] for t in trades)

    msg = f'📊 *NIFTY ALGO निकाल ({test_date})*\n\n'
    msg += f'🔹 *एकूण कॅन्डल्स:* `{candles_count:,}`\n'
    msg += f'🔹 *एकूण ट्रेड्स:* `{tot}` (जिंकले: `{wins}`, तोटा: `{losses}`)\n'
    msg += f'💰 *निव्वळ नफा (Net PnL):* `₹ {tot_pnl:+,.2f}`\n\n'
    msg += '📋 *ट्रेड तपशील:*\n'
    for idx, t in enumerate(trades, 1):
      msg += (
          f"{idx}. *{t['side']}* | `{t['symbol']}`\n"
          f"   ⏱️ {t['entry_time']} ➡️ {t['exit_time']} | पॉईंट्स:"
          f" `{t['pts']:+}`\n"
          f"   💵 निव्वळ PnL: `₹ {t['net_pnl']:+,.2f}` ({t['reason']})\n"
      )
    send_telegram(msg)
  else:
    send_telegram(
        f'ℹ️ *NIFTY निकाल ({test_date})*\nकॅन्डल्स:'
        f' `{candles_count:,}`\nनिकषांनुसार कोणताही ट्रेड झाला नाही.'
    )
