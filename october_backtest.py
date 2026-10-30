import io
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==============================================================================
# १. कॉन्फिगरेशन
# ==============================================================================
DB_FILE = "nifty_vault.db"
UPSTOX_TOKEN = os.getenv("UPSTOX_TOKEN", "a429fb9b-6faf-4c3c-b070-78cae68237c4")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

UPSTOX_HEADERS = {
    "Accept": "application/json",
    "Authorization": f"Bearer {UPSTOX_TOKEN}",
}

DELTA_TARGET = 0.52
MIN_OPT_SL_PTS = 10.0
IST = timezone(timedelta(hours=5, minutes=30))

session = requests.Session()
retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retries))


def init_database():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS nifty_options_1m (
        date TEXT, timestamp TEXT, symbol TEXT, strike REAL, option_type TEXT,
        open REAL, high REAL, low REAL, close REAL, volume INTEGER, oi INTEGER,
        price_change REAL, change_oi INTEGER, buildup TEXT,
        delta REAL, gamma REAL, theta REAL, vega REAL,
        PRIMARY KEY (symbol, timestamp)
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS trade_journal (
        date TEXT, side TEXT, symbol TEXT, entry_time TEXT, exit_time TEXT,
        entry_px REAL, opt_sl REAL, opt_target REAL, exit_px REAL, pts REAL,
        lot_size INTEGER, net_pnl REAL, reason TEXT,
        PRIMARY KEY (date, symbol, entry_time)
    )""")
    conn.commit()
    conn.close()


def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"\n{text}\n")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        session.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"[-] Telegram Error: {e}")


# ==============================================================================
# २. गणिते व चार्जेस (Upstox Brokerage API)
# ==============================================================================
def norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def calculate_greeks(spot, strike, expiry_days, volatility, opt_type, r=0.07):
    if expiry_days <= 0 or volatility <= 0 or spot <= 0 or strike <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    t = max(expiry_days, 0.5) / 365.0
    v_sqrt_t = volatility * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * volatility * volatility) * t) / v_sqrt_t
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


def get_upstox_charges(instrument_token, quantity, buy_px, sell_px):
    url = "https://api.upstox.com/v2/charges/brokerage"
    bp = {
        "instrument_token": instrument_token,
        "quantity": str(quantity),
        "product": "I",
        "transaction_type": "BUY",
        "price": str(buy_px),
    }
    sp = {
        "instrument_token": instrument_token,
        "quantity": str(quantity),
        "product": "I",
        "transaction_type": "SELL",
        "price": str(sell_px),
    }
    try:
        rb = session.get(url, headers=UPSTOX_HEADERS, params=bp, timeout=2)
        rs = session.get(url, headers=UPSTOX_HEADERS, params=sp, timeout=2)
        if rb.status_code == 200 and rs.status_code == 200:
            db = rb.json().get("data", {}).get("charges", {})
            ds = rs.json().get("data", {}).get("charges", {})
            b = float(db.get("brokerage", 20.0)) + float(ds.get("brokerage", 20.0))
            s = float(db.get("stt", 0.0)) + float(ds.get("stt", 0.0))
            o = (
                float(db.get("gst", 0.0))
                + float(ds.get("gst", 0.0))
                + float(db.get("stamp_duty", 0.0))
                + float(db.get("other_charges", 0.0))
                + float(ds.get("other_charges", 0.0))
            )
            return round(b + s + o, 2)
    except Exception:
        pass
    return 52.0


# ==============================================================================
# ३. ऐतिहासिक स्पॉट व संपलेली एक्सपायरी मिळवणे (Expired Instruments API)
# ==============================================================================
def get_historical_nifty_spot(target_date: str):
    key = quote("NSE_INDEX|Nifty 50", safe="")
    # V3 Daily Candle द्वारे त्या दिवसाचा स्पॉट
    url = f"https://api.upstox.com/v3/historical-candle/{key}/days/1/{target_date}/{target_date}"
    try:
        r = session.get(url, headers=UPSTOX_HEADERS, timeout=6)
        if r.status_code == 200:
            candles = r.json().get("data", {}).get("candles", [])
            if candles:
                return float(candles[0][4])  # Day Close Price
    except Exception:
        pass

    # V2 Historical Fallback
    try:
        url_v2 = f"https://api.upstox.com/v2/historical-candle/{key}/day/{target_date}/{target_date}"
        r = session.get(url_v2, headers=UPSTOX_HEADERS, timeout=6)
        if r.status_code == 200:
            candles = r.json().get("data", {}).get("candles", [])
            if candles:
                return float(candles[0][4])
    except Exception:
        pass
    return None


def get_all_expiries():
    url = "https://api.upstox.com/v2/expired-instruments/expiries?instrument_key=NSE_INDEX|Nifty%2050"
    try:
        r = session.get(url, headers=UPSTOX_HEADERS, timeout=10)
        if r.status_code == 200:
            return sorted(r.json().get("data", []))
    except Exception as e:
        print(f"[-] Expiries Error: {e}")
    return []


def fetch_historical_contracts(target_date: str, expiry_date: str, spot: float):
    # Expired Option Contracts API
    url = f"https://api.upstox.com/v2/expired-instruments/option/contract?instrument_key=NSE_INDEX|Nifty%2050&expiry_date={expiry_date}"
    try:
        r = session.get(url, headers=UPSTOX_HEADERS, timeout=10)
        if r.status_code != 200:
            return pd.DataFrame()
        contracts = r.json().get("data", [])
        if not contracts:
            return pd.DataFrame()

        df_c = pd.DataFrame(contracts)
        atm_strike = round(spot / 50.0) * 50.0
        active_strikes = [atm_strike + (i * 50.0) for i in range(-5, 6)]

        df_c["strike_price"] = df_c["strike_price"].astype(float)
        filtered = df_c[df_c["strike_price"].isin(active_strikes)].copy()
        return filtered
    except Exception as e:
        print(f"[-] Contract Fetch Error: {e}")
        return pd.DataFrame()


# ==============================================================================
# ४. १ दिवसाचा डेटा डाऊनलोड व प्रोसेस करणे
# ==============================================================================
def process_single_day(target_date: str, expiries_list):
    spot = get_historical_nifty_spot(target_date)
    if not spot:
        return 0, 65

    valid_exp = [exp for exp in expiries_list if exp >= target_date]
    if not valid_exp:
        return 0, 65
    nearest_exp = valid_exp[0]
    dte = max(1, (pd.Timestamp(nearest_exp) - pd.Timestamp(target_date)).days)

    df_contracts = fetch_historical_contracts(target_date, nearest_exp, spot)
    if df_contracts.empty:
        return 0, 65

    lot_size = (
        int(df_contracts["lot_size"].iloc[0])
        if "lot_size" in df_contracts.columns
        else 65
    )

    all_rows = []
    for _, row in df_contracts.iterrows():
        key = quote(row["instrument_key"], safe="")
        # Expired Historical Candle Data API (1 Minute)
        url = f"https://api.upstox.com/v2/expired-instruments/historical-candle/{key}/1minute/{target_date}/{target_date}"
        try:
            r = session.get(url, headers=UPSTOX_HEADERS, timeout=7)
            if r.status_code == 200:
                candles = r.json().get("data", {}).get("candles", [])
                for c in candles:
                    all_rows.append(
                        {
                            "date": target_date,
                            "timestamp": c[0],
                            "symbol": row["trading_symbol"],
                            "strike": row["strike_price"],
                            "option_type": (
                                "CE"
                                if "CE" in str(row["trading_symbol"]).upper()
                                else "PE"
                            ),
                            "open": float(c[1]),
                            "high": float(c[2]),
                            "low": float(c[3]),
                            "close": float(c[4]),
                            "volume": int(c[5]),
                            "oi": (
                                int(c[6])
                                if len(c) > 6 and c[6] is not None
                                else 0
                            ),
                        }
                    )
        except Exception:
            continue
        time.sleep(0.03)

    if not all_rows:
        return 0, lot_size

    df_all = (
        pd.DataFrame(all_rows)
        .sort_values(by=["symbol", "timestamp"])
        .reset_index(drop=True)
    )
    df_all["price_change"] = df_all.groupby("symbol")["close"].diff().fillna(0.0)
    df_all["change_oi"] = (
        df_all.groupby("symbol")["oi"].diff().fillna(0).astype(int)
    )

    def calc_buildup(r):
        p, o = r["price_change"], r["change_oi"]
        if p > 0 and o > 0:
            return "Long Buildup"
        if p < 0 and o > 0:
            return "Short Buildup"
        if p > 0 and o < 0:
            return "Short Covering"
        if p < 0 and o < 0:
            return "Long Unwinding"
        return "Neutral"

    df_all["buildup"] = df_all.apply(calc_buildup, axis=1)

    greeks = [
        calculate_greeks(
            spot, r["strike"], dte, 0.14, r["option_type"]
        )
        for _, r in df_all.iterrows()
    ]
    df_final = pd.concat([df_all, pd.DataFrame(greeks)], axis=1)

    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        f"DELETE FROM nifty_options_1m WHERE timestamp LIKE '{target_date}%'"
    )
    df_final.to_sql("nifty_options_1m", conn, if_exists="append", index=False)
    conn.commit()
    conn.close()

    return len(df_final), lot_size


# ==============================================================================
# ५. बॅकटेस्ट स्ट्रॅटेजी रनर
# ==============================================================================
def run_strategy_for_day(target_date: str, lot_size: int):
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query(
        f"SELECT * FROM nifty_options_1m WHERE timestamp LIKE '{target_date}%' ORDER BY timestamp ASC, symbol ASC",
        conn,
    )
    conn.close()

    if df.empty:
        return []

    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.drop_duplicates(subset=["symbol", "timestamp"]).reset_index(drop=True)

    all_processed = []
    for sym in df["symbol"].unique():
        sub = df[df["symbol"] == sym].copy().sort_values("timestamp")
        if len(sub) < 25:
            continue
        sub["vol_sma20"] = sub["volume"].rolling(20).mean().replace(0, 1.0)
        sub["vol_ratio"] = sub["volume"] / sub["vol_sma20"]
        prev_c = sub["close"].shift(1)
        tr = pd.concat(
            [
                sub["high"] - sub["low"],
                (sub["high"] - prev_c).abs(),
                (sub["low"] - prev_c).abs(),
            ],
            axis=1,
        ).max(axis=1)
        sub["atr14"] = tr.rolling(14).mean().fillna(5.0)
        all_processed.append(sub)

    if not all_processed:
        return []

    df_data = pd.concat(all_processed, axis=0).sort_values(
        by=["timestamp", "symbol"]
    )

    trades = []
    morning_trades = 0
    afternoon_trades = 0
    active = None
    cooldown = None

    for ts in df_data["timestamp"].unique():
        hhmm = ts.strftime("%H:%M")
        snap = df_data[df_data["timestamp"] == ts]

        if active:
            curr = snap[snap["symbol"] == active["symbol"]]
            if not curr.empty:
                bar = curr.iloc[0]
                exit_p, reason = None, ""
                if bar["low"] <= active["opt_sl"]:
                    exit_p, reason = active["opt_sl"], "SL_HIT ❌"
                elif bar["high"] >= active["opt_target"]:
                    exit_p, reason = active["opt_target"], "TARGET_HIT ✅"
                elif hhmm >= "15:20":
                    exit_p, reason = float(bar["close"]), "FORCE_EOD ⏱️"

                if exit_p:
                    pts = round(exit_p - active["entry_price"], 2)
                    gross = round(pts * lot_size, 2)
                    ch = get_upstox_charges(
                        active["symbol"], lot_size, active["entry_price"], exit_p
                    )
                    net = round(gross - ch, 2)

                    trade_item = {
                        "date": target_date,
                        "side": active["side"],
                        "symbol": active["symbol"],
                        "entry_time": active["entry_time"].strftime("%H:%M"),
                        "exit_time": ts.strftime("%H:%M"),
                        "entry_px": active["entry_price"],
                        "opt_sl": active["opt_sl"],
                        "opt_target": active["opt_target"],
                        "exit_px": round(exit_p, 2),
                        "pts": pts,
                        "lot_size": lot_size,
                        "net_pnl": net,
                        "reason": reason,
                    }
                    trades.append(trade_item)

                    conn = sqlite3.connect(DB_FILE)
                    conn.execute(
                        """
                    INSERT OR REPLACE INTO trade_journal 
                    (date, side, symbol, entry_time, exit_time, entry_px, opt_sl, opt_target, exit_px, pts, lot_size, net_pnl, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            trade_item["date"],
                            trade_item["side"],
                            trade_item["symbol"],
                            trade_item["entry_time"],
                            trade_item["exit_time"],
                            trade_item["entry_px"],
                            trade_item["opt_sl"],
                            trade_item["opt_target"],
                            trade_item["exit_px"],
                            trade_item["pts"],
                            trade_item["lot_size"],
                            trade_item["net_pnl"],
                            trade_item["reason"],
                        ),
                    )
                    conn.commit()
                    conn.close()

                    active = None
                    cooldown = ts + pd.Timedelta(minutes=15)
            continue

        if cooldown and ts < cooldown:
            continue

        is_m = ("09:40" <= hhmm <= "11:30") and (morning_trades < 1)
        is_a = ("12:30" <= hhmm <= "14:30") and (afternoon_trades < 2)

        if (active is None) and (is_m or is_a):
            cands = []
            for _, row in snap.iterrows():
                if not (70.0 <= row["close"] <= 320.0):
                    continue
                c_range = row["high"] - row["low"]
                b_ratio = abs(row["close"] - row["open"]) / (
                    c_range if c_range > 0 else 1e-9
                )
                if (
                    (
                        row["vol_ratio"] >= 1.30
                        or c_range >= 1.20 * row["atr14"]
                    )
                    and b_ratio >= 0.45
                    and row["close"] > row["open"]
                ):
                    if row["buildup"] in ["Long Buildup", "Short Covering"]:
                        cands.append(row)

            if cands:
                cdf = pd.DataFrame(cands)
                cdf["atm_dist"] = (cdf["delta"].abs() - 0.50).abs()
                best = cdf.sort_values(by="atm_dist").iloc[0]
                ep = float(best["close"])
                sl_pts = max(MIN_OPT_SL_PTS, best["atr14"] * 1.2 * DELTA_TARGET)
                active = {
                    "symbol": best["symbol"],
                    "side": f"BUY_{best['option_type']}",
                    "entry_time": ts,
                    "entry_price": ep,
                    "opt_sl": max(0.05, round(ep - sl_pts, 2)),
                    "opt_target": round(ep + (sl_pts * 1.50), 2),
                }
                if is_m:
                    morning_trades += 1
                else:
                    afternoon_trades += 1

    return trades


# ==============================================================================
# ६. मुख्य एक्झिक्युशन (१ ऑक्टोबर ते ३० ऑक्टोबर)
# ==============================================================================
if __name__ == "__main__":
    init_database()
    print("🚀 NIFTY October Full Month Backtest सुरू होत आहे...")

    expiries = get_all_expiries()
    # १ ऑक्टोबर ते ३० ऑक्टोबर २०२६ मधील सर्व कामाचे दिवस (सोमवार ते शुक्रवार)
    dates_to_run = pd.bdate_range(
        start="2026-10-01", end="2026-10-30"
    ).strftime("%Y-%m-%d")

    all_month_trades = []

    for d in dates_to_run:
        print(f"\n⏳ प्रोसेसिंग: {d}")
        c_count, lot = process_single_day(d, expiries)
        if c_count > 0:
            trades = run_strategy_for_day(d, lot)
            print(f"   ✅ {d}: {c_count} कॅन्डल्स | {len(trades)} ट्रेड्स")
            all_month_trades.extend(trades)
        else:
            print(f"   ℹ️ {d}: कोणतीही कॅन्डल सापडली नाही (सुट्टी असू शकते).")

    # अंतिम अहवाल
    conn = sqlite3.connect(DB_FILE)
    df_j = pd.read_sql_query(
        "SELECT * FROM trade_journal WHERE date >= '2026-10-01' AND date <= '2026-10-30'",
        conn,
    )
    conn.close()

    total_trades = len(df_j)
    wins = len(df_j[df_j["net_pnl"] > 0])
    losses = total_trades - wins
    total_pnl = df_j["net_pnl"].sum()
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

    report = (
        f"🏆 *NIFTY OCTOBER FULL BACKTEST निकाल (01-30 OCT)*\n\n"
        f"🔹 *एकूण ट्रेडिंग दिवस:* `{len(dates_to_run)}`\n"
        f"🔹 *एकूण ट्रेड्स:* `{total_trades}`\n"
        f"✅ *जिंकलेले ट्रेड्स:* `{wins}`\n"
        f"❌ *तोट्यातील ट्रेड्स:* `{losses}`\n"
        f"🎯 *Win Rate:* `{win_rate:.1f}%`\n"
        f"💰 *महिन्याचा एकूण Net PnL:* `₹ {total_pnl:+,.2f}`\n\n"
        f"📊 *ट्रेड तपशील आणि लॉग SQLite (`trade_journal`) मध्ये सुरक्षित जतन केले आहेत.*"
    )

    print(report)
    send_telegram(report)
