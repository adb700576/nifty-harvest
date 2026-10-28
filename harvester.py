import io
import math
import os
import sqlite3
import time
from datetime import datetime
from urllib.parse import quote
import pandas as pd
import requests

CONFIG = {
    "API_KEY": "a429fb9b-6faf-4c3c-b070-78cae68237c4",
    "API_SECRET": "ecskfmr33z",
    "REDIRECT_URI": "https://127.0.0.1",
    "USER_ID": "125698",
    "PIN": "700577",
    "TOTP_KEY": "L23PMOD62UXQGIMFJWC5DXUJVEBKNV7P",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "DB_PATH": "nifty_vault.db",
}

BASE_URL = "https://api.upstox.com"


def send_telegram(message: str):
  token = CONFIG.get("TELEGRAM_BOT_TOKEN")
  chat_id = CONFIG.get("TELEGRAM_CHAT_ID")
  if not token or not chat_id:
    print(f"[Telegram]: {message}")
    return
  url = f"https://api.telegram.org/bot{token}/sendMessage"
  try:
    requests.post(
        url,
        data={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
        timeout=10,
    )
  except Exception as e:
    print(f"[-] Telegram Error: {e}")


def calculate_buildup(row):
  p_diff = row["price_change"]
  oi_diff = row["change_oi"]
  if p_diff > 0 and oi_diff > 0:
    return "Long Buildup"
  elif p_diff < 0 and oi_diff > 0:
    return "Short Buildup"
  elif p_diff > 0 and oi_diff < 0:
    return "Short Covering"
  elif p_diff < 0 and oi_diff < 0:
    return "Long Unwinding"
  return "Neutral"


def norm_cdf(x):
  return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def norm_pdf(x):
  return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def calculate_greeks(
    spot: float,
    strike: float,
    expiry_days: float,
    volatility: float,
    opt_type: str,
    r: float = 0.07,
):
  if (
      expiry_days <= 0
      or volatility <= 0
      or spot <= 0
      or strike <= 0
      or math.isnan(volatility)
  ):
    return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
  t = max(expiry_days, 0.5) / 365.0
  v_sqrt_t = volatility * math.sqrt(t)
  d1 = (
      math.log(spot / strike) + (r + 0.5 * volatility * volatility) * t
  ) / v_sqrt_t
  d2 = d1 - v_sqrt_t
  gamma = norm_pdf(d1) / (spot * v_sqrt_t)
  vega = (spot * norm_pdf(d1) * math.sqrt(t)) / 100.0
  if opt_type.upper() in ["CE", "CALL"]:
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
      "delta": round(delta, 4),
      "gamma": round(gamma, 6),
      "theta": round(theta, 2),
      "vega": round(vega, 2),
  }


def run_nifty_daily_harvest():
  today_str = datetime.now().strftime("%Y-%m-%d")
  print(f"🚀 NIFTY Data Harvest सुरू: {today_str}")

  token = CONFIG["API_KEY"]
  headers = {
      "Content-Type": "application/json",
      "Accept": "application/json",
      "Authorization": f"Bearer {token}",
  }

  nifty_spot_key = quote("NSE_INDEX|Nifty 50", safe="")
  spot_url = f"https://api.upstox.com/v3/historical-candle/intraday/{nifty_spot_key}/minutes/1"

  day_close = 22760.0
  try:
    spot_res = requests.get(spot_url, headers=headers, timeout=10)
    if spot_res.status_code == 200:
      c = spot_res.json().get("data", {}).get("candles", [])
      if c:
        day_close = float(c[0][4])
        print(f"📍 NIFTY 50 Spot Level: {day_close:.2f}")
  except Exception as e:
    print(f"[-] स्पॉट एरर: {e}")

  atm_strike = round(day_close / 50.0) * 50
  required_strikes = list(
      range(int(atm_strike - 250), int(atm_strike + 300), 50)
  )

  res = requests.get(
      "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz",
      timeout=40,
  )
  df_m = pd.read_csv(io.BytesIO(res.content), compression="gzip")
  strike_col = "strike_price" if "strike_price" in df_m.columns else "strike"

  opt = df_m[
      (df_m["exchange"] == "NSE_FO")
      & (df_m["name"].str.upper() == "NIFTY")
      & (df_m["instrument_type"] == "OPTIDX")
  ].copy()
  opt["expiry"] = pd.to_datetime(opt["expiry"])
  opt["strike"] = opt[strike_col].astype(float)

  today = pd.Timestamp.now().floor("D")
  upcoming = opt[opt["expiry"] >= today].sort_values("expiry")
  current_expiry = (
      upcoming["expiry"].iloc[0] if not upcoming.empty else opt["expiry"].max()
  )
  days_to_expiry = max(1, (current_expiry - today).days)

  selected_opts = opt[
      (opt["expiry"] == current_expiry) & (opt["strike"].isin(required_strikes))
  ]

  all_rows = []
  for _, row in selected_opts.iterrows():
    key = quote(row["instrument_key"], safe="")
    fetch_url = (
        f"https://api.upstox.com/v3/historical-candle/intraday/{key}/minutes/1"
    )
    try:
      r = requests.get(fetch_url, headers=headers, timeout=10)
      if r.status_code == 200:
        candles = r.json().get("data", {}).get("candles", [])
        for c in candles:
          all_rows.append({
              "date": today_str,
              "timestamp": c[0],
              "symbol": row["tradingsymbol"],
              "strike": row["strike"],
              "option_type": (
                  "CE" if "CE" in str(row["tradingsymbol"]).upper() else "PE"
              ),
              "open": float(c[1]),
              "high": float(c[2]),
              "low": float(c[3]),
              "close": float(c[4]),
              "volume": int(c[5]),
              "oi": int(c[6]) if len(c) > 6 and c[6] is not None else 0,
          })
    except Exception:
      pass
    time.sleep(0.04)

  if not all_rows:
    print("[-] कॅन्डल्स मिळाल्या नाहीत.")
    return

  df_all = (
      pd.DataFrame(all_rows)
      .sort_values(by=["symbol", "timestamp"])
      .reset_index(drop=True)
  )
  df_all["price_change"] = df_all.groupby("symbol")["close"].diff().fillna(0.0)
  df_all["change_oi"] = (
      df_all.groupby("symbol")["oi"].diff().fillna(0).astype(int)
  )
  df_all["buildup"] = df_all.apply(calculate_buildup, axis=1)

  greeks_list = [
      calculate_greeks(
          day_close,
          r["strike"],
          days_to_expiry,
          0.14,
          r["option_type"],
      )
      for _, r in df_all.iterrows()
  ]
  df_final = pd.concat([df_all, pd.DataFrame(greeks_list)], axis=1)

  conn = sqlite3.connect(CONFIG["DB_PATH"])
  df_final.to_sql("nifty_options_1m", conn, if_exists="append", index=False)
  conn.commit()
  conn.close()

  print(f"✅ {len(df_final):,} कॅन्डल्स DB मध्ये सेव्ह झाल्या!")
  send_telegram(f"✅ NIFTY EOD Harvest पूर्ण! {len(df_final):,} कॅन्डल्स सेव्ह.")


if __name__ == "__main__":
  run_nifty_daily_harvest()
