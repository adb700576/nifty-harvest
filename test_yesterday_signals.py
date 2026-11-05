import os
import sqlite3
import pandas as pd
import requests

DB_PATH = "nifty_vault.db"
BASE_URL = "https://api.upstox.com"
ACCESS_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN") or ""
if not ACCESS_TOKEN and os.path.exists("ACCESS_TOKEN.txt"):
    with open("ACCESS_TOKEN.txt", "r", encoding="utf-8") as f:
        ACCESS_TOKEN = f.read().strip()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

LOT_SIZE = 65
MIN_OPT_SL_PTS = 12.0
DAILY_PROFIT_GOAL = 450.0  # ₹450+ नफा झाला की दिवस ग्रीनमध्ये लॉक (Capital Protection)

def send_telegram(text: str):
    """टेलिग्रामवर थेट निकाल पाठवणे आणि एरर तपासणे"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[-] Telegram Credentials गहाळ आहेत (TELEGRAM_BOT_TOKEN किंवा TELEGRAM_CHAT_ID नाही).", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            print("[+] टेलिग्रामवर निकाल यशस्वीरीत्या पाठवला!", flush=True)
        else:
            print(f"[-] Telegram API Error ({r.status_code}): {r.text}", flush=True)
    except Exception as e:
        print(f"[-] Telegram Request Exception: {e}", flush=True)

def get_upstox_charges(instrument_token: str, quantity: int, buy_price: float, sell_price: float) -> dict:
    url = f"{BASE_URL}/v2/charges/brokerage"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {ACCESS_TOKEN}",
    }
    buy_params = {
        "instrument_token": instrument_token,
        "quantity": str(quantity),
        "product": "I",
        "transaction_type": "BUY",
        "price": str(buy_price),
    }
    sell_params = {
        "instrument_token": instrument_token,
        "quantity": str(quantity),
        "product": "I",
        "transaction_type": "SELL",
        "price": str(sell_price),
    }

    if ACCESS_TOKEN:
        try:
            total_b, total_s, total_o = 0.0, 0.0, 0.0
            rb = requests.get(url, headers=headers, params=buy_params, timeout=2)
            if rb.status_code == 200:
                d = rb.json().get("data", {}).get("charges", {})
                total_b += float(d.get("brokerage", 20.0))
                total_s += float(d.get("stt", 0.0))
                total_o += float(d.get("gst", 0.0)) + float(d.get("stamp_duty", 0.0)) + float(d.get("other_charges", 0.0))

            rs = requests.get(url, headers=headers, params=sell_params, timeout=2)
            if rs.status_code == 200:
                d = rs.json().get("data", {}).get("charges", {})
                total_b += float(d.get("brokerage", 20.0))
                total_s += float(d.get("stt", 0.0))
                total_o += float(d.get("gst", 0.0)) + float(d.get("other_charges", 0.0))

            tot = round(total_b + total_s + total_o, 2)
            if tot > 0:
                return {"total_charges": tot}
        except Exception:
            pass

    return {"total_charges": 52.0}

def get_atm_strike(spot_price: float, step: int = 50) -> float:
    return round(spot_price / step) * step

def run_harvest_pnl_engine():
    if not os.path.exists(DB_PATH):
        print(f"[-] Database file {DB_PATH} not found.", flush=True)
        return

    conn = sqlite3.connect(DB_PATH)
    avail_dates = pd.read_sql_query(
        "SELECT DISTINCT substr(timestamp, 1, 10) as t_date FROM nifty_options_1m ORDER BY t_date DESC",
        conn,
    )
    if avail_dates.empty:
        print("[-] No option candle records available.", flush=True)
        conn.close()
        return

    target_date = avail_dates["t_date"].tolist()[0]

    print("=" * 80, flush=True)
    print(f"🚀 [High-Accuracy Institutional Engine] Target Date: {target_date} (Paper Trade)", flush=True)
    print("=" * 80, flush=True)

    df_spot = pd.read_sql_query(
        f"SELECT timestamp, open, high, low, close, volume FROM nifty_index_1m WHERE timestamp LIKE '{target_date}%'",
        conn,
    )

    query_opt = f"""
        SELECT timestamp, symbol, strike, option_type, open, high, low, close, volume, oi
        FROM nifty_options_1m
        WHERE timestamp LIKE '{target_date}%'
    """
    df_opt = pd.read_sql_query(query_opt, conn)
    conn.close()

    if df_opt.empty or df_spot.empty:
        print(f"[-] Incomplete data for {target_date}.", flush=True)
        return

    df_spot["dt"] = pd.to_datetime(df_spot["timestamp"])
    if df_spot["dt"].dt.tz is not None:
        df_spot["dt"] = df_spot["dt"].dt.tz_convert("Asia/Kolkata")
    df_spot = df_spot.drop_duplicates(subset=["dt"]).sort_values("dt").reset_index(drop=True)

    typical_price = (df_spot["high"] + df_spot["low"] + df_spot["close"]) / 3.0
    vol = df_spot["volume"].replace(0, 1.0)
    df_spot["cum_pv"] = (typical_price * vol).cumsum()
    df_spot["cum_v"] = vol.cumsum()
    df_spot["vwap"] = df_spot["cum_pv"] / df_spot["cum_v"]
    df_spot["ema20"] = df_spot["close"].ewm(span=20, adjust=False).mean()
    df_spot["dist_vwap"] = df_spot["close"] - df_spot["vwap"]
    spot_records = df_spot.set_index("dt").to_dict("index")

    df_opt["dt"] = pd.to_datetime(df_opt["timestamp"])
    if df_opt["dt"].dt.tz is not None:
        df_opt["dt"] = df_opt["dt"].dt.tz_convert("Asia/Kolkata")
    df_opt = df_opt.drop_duplicates(subset=["symbol", "dt"]).sort_values(by=["symbol", "dt"]).reset_index(drop=True)

    all_processed = []
    for sym in df_opt["symbol"].unique():
        sub = df_opt[df_opt["symbol"] == sym].copy().sort_values("dt").reset_index(drop=True)
        if len(sub) < 25:
            continue
        sub["vol_sma20"] = sub["volume"].rolling(20).mean().replace(0, 1.0)
        sub["vol_ratio"] = sub["volume"] / sub["vol_sma20"]

        prev_c = sub["close"].shift(1)
        tr = pd.concat([
            sub["high"] - sub["low"],
            (sub["high"] - prev_c).abs(),
            (sub["low"] - prev_c).abs(),
        ], axis=1).max(axis=1)
        sub["atr14"] = tr.rolling(14).mean().fillna(5.0)
        sub["change_oi"] = sub["oi"].diff().fillna(0.0)
        all_processed.append(sub)

    df_data = pd.concat(all_processed, axis=0).sort_values(by=["dt", "symbol"]).reset_index(drop=True)

    all_trades = []
    active_trade = None
    cooldown_until = None
    realized_net_pnl = 0.0
    daily_locked = False
    all_timestamps = df_data["dt"].unique()

    for ts in all_timestamps:
        ts_pd = pd.to_datetime(ts)
        hhmm = ts_pd.strftime("%H:%M")
        snap = df_data[df_data["dt"] == ts_pd]

        if active_trade:
            curr_match = snap[snap["symbol"] == active_trade["symbol"]]
            if not curr_match.empty:
                bar = curr_match.iloc[0]
                exit_p, exit_reason = None, ""
                max_gain = bar["high"] - active_trade["entry_price"]

                if max_gain >= 8.0:
                    active_trade["opt_sl"] = max(active_trade["opt_sl"], active_trade["entry_price"])

                if max_gain >= 14.0:
                    trailing_level = bar["close"] - 6.0
                    active_trade["opt_sl"] = max(active_trade["opt_sl"], trailing_level)

                if bar["low"] <= active_trade["opt_sl"]:
                    exit_p = active_trade["opt_sl"]
                    exit_reason = "TRAILED_EXIT ✅" if active_trade["opt_sl"] >= active_trade["entry_price"] else "SL_HIT ❌"
                elif bar["high"] >= active_trade["opt_target"]:
                    exit_p = active_trade["opt_target"]
                    exit_reason = "TARGET_HIT ✅"
                elif hhmm >= "15:20":
                    exit_p = float(bar["close"])
                    exit_reason = "FORCE_EOD ⏱️"

                if exit_p:
                    captured_pts = round(exit_p - active_trade["entry_price"], 2)
                    gross_pnl = round(captured_pts * LOT_SIZE, 2)
                    charges_dict = get_upstox_charges(
                        active_trade["symbol"],
                        LOT_SIZE,
                        active_trade["entry_price"],
                        exit_p,
                    )
                    net_pnl = round(gross_pnl - charges_dict["total_charges"], 2)
                    realized_net_pnl += net_pnl

                    all_trades.append({
                        "date": target_date,
                        "side": active_trade["side"],
                        "symbol": active_trade["symbol"],
                        "spot_px": active_trade["spot_entry"],
                        "entry_time": active_trade["entry_time"].strftime("%H:%M"),
                        "exit_time": hhmm,
                        "entry_px": active_trade["entry_price"],
                        "sl_px": active_trade["opt_sl"],
                        "trg_px": active_trade["opt_target"],
                        "exit_px": round(exit_p, 2),
                        "pts_captured": captured_pts,
                        "net_pnl": net_pnl,
                        "pcr": active_trade["pcr"],
                        "oi": active_trade["oi"],
                        "change_oi": active_trade["change_oi"],
                        "exit_reason": exit_reason,
                    })

                    if realized_net_pnl >= DAILY_PROFIT_GOAL:
                        daily_locked = True
                        print(f"🔒 [PROFIT LOCK] दिवस नफ्यात लॉक केला (₹{realized_net_pnl:+,.2f})! पुढील ट्रेड्स थांबवले.", flush=True)

                    active_trade = None
                    cooldown_until = ts_pd + pd.Timedelta(minutes=30)
            continue

        if daily_locked:
            continue

        if cooldown_until and ts_pd < cooldown_until:
            continue

        is_morning = ("10:15" <= hhmm <= "11:30")
        is_afternoon = ("13:15" <= hhmm <= "14:45")

        if (active_trade is None) and (is_morning or is_afternoon):
            spot_info = spot_records.get(ts_pd)
            if spot_info is None or pd.isna(spot_info.get("vwap")):
                continue

            cur_spot = spot_info["close"]
            vwap = spot_info["vwap"]
            ema20 = spot_info["ema20"]
            dist_vwap = spot_info["dist_vwap"]

            if abs(dist_vwap) > 65.0:
                continue

            pe_oi_sum = snap[snap["option_type"] == "PE"]["oi"].sum()
            ce_oi_sum = snap[snap["option_type"] == "CE"]["oi"].sum()
            pcr = round(pe_oi_sum / (ce_oi_sum if ce_oi_sum > 0 else 1.0), 2)

            allowed_side = None
            if cur_spot > vwap and cur_spot > ema20 and pcr >= 0.95:
                allowed_side = "CE"
            elif cur_spot < vwap and cur_spot < ema20 and pcr <= 0.80:
                allowed_side = "PE"

            if not allowed_side:
                continue

            atm_strike = get_atm_strike(cur_spot, step=50)
            atm_candidates = snap[(snap["strike"] == atm_strike) & (snap["option_type"] == allowed_side)]

            for _, row in atm_candidates.iterrows():
                if not (75.0 <= row["close"] <= 300.0):
                    continue

                c_range = row["high"] - row["low"]
                c_body = abs(row["close"] - row["open"])
                b_ratio = c_body / (c_range if c_range > 0 else 1e-9)

                has_vol = row["vol_ratio"] >= 1.20
                is_solid = b_ratio >= 0.40
                has_oi_buildup = row["change_oi"] >= 0

                if has_vol and is_solid and (row["close"] > row["open"]) and has_oi_buildup:
                    entry_opt_price = float(row["close"])
                    opt_sl_pts = MIN_OPT_SL_PTS
                    opt_target_pts = opt_sl_pts * 2.0

                    active_trade = {
                        "symbol": row["symbol"],
                        "side": f"BUY_{row['option_type']}",
                        "spot_entry": round(cur_spot, 1),
                        "entry_time": ts_pd,
                        "entry_price": entry_opt_price,
                        "opt_sl": max(0.05, round(entry_opt_price - opt_sl_pts, 2)),
                        "opt_target": round(entry_opt_price + opt_target_pts, 2),
                        "pcr": pcr,
                        "oi": int(row["oi"]),
                        "change_oi": int(row["change_oi"]),
                    }
                    break

    df_all = pd.DataFrame(all_trades)
    print("\n" + "=" * 80, flush=True)
    print(f"📊 NIFTY ALGO निकाल ({target_date}) [High-Accuracy Mode]", flush=True)
    print("=" * 80, flush=True)

    if not df_all.empty:
        total_trades = len(df_all)
        wins = len(df_all[df_all["net_pnl"] > 0])
        losses = len(df_all[df_all["net_pnl"] <= 0])
        net_total = df_all["net_pnl"].sum()
        win_rate = (wins / total_trades) * 100

        # कन्सोल प्रिंट
        print(f"🔹 एकूण ट्रेड्स: {total_trades} (जिंकले: {wins}, तोटा: {losses}) | Win Rate: {win_rate:.1f}%", flush=True)
        print(f"💰 निव्वळ नफा (Net PnL): ₹ {net_total:+,.2f}\n", flush=True)

        # टेलिग्रामसाठी HTML मेसेज तयार करणे
        tg_msg = (
            f"📊 <b>NIFTY ALGO बॅकटेस्ट निकाल ({target_date})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔹 <b>एकूण ट्रेड्स:</b> {total_trades} (जिंकले: {wins}, तोटा: {losses})\n"
            f"🎯 <b>Win Rate:</b> {win_rate:.1f}%\n"
            f"💰 <b>निव्वळ नफा (Net PnL):</b> <b>₹ {net_total:+,.2f}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
        )

        for idx, tr in df_all.iterrows():
            trade_row = (
                f"{idx + 1}. <b>{tr['side']}</b> | <code>{tr['symbol']}</code>\n"
                f"   🎯 Spot: {tr['spot_px']} | वेळ: {tr['entry_time']} ➡️ {tr['exit_time']}\n"
                f"   📈 PCR: {tr['pcr']:.2f} | ΔOI: {tr['change_oi']:+,}\n"
                f"   💵 Entry: ₹{tr['entry_px']:.2f} ➡️ Exit: ₹{tr['exit_px']:.2f}\n"
                f"   📊 पॉईंट्स: <b>{tr['pts_captured']:+.2f}</b> | PnL: <b>₹{tr['net_pnl']:+,.2f}</b> ({tr['exit_reason']})\n\n"
            )
            tg_msg += trade_row
            print(f"{idx + 1}. {tr['side']} | {tr['symbol']}", flush=True)
            print(f"   🎯 NIFTY Spot: {tr['spot_px']} | वेळ: {tr['entry_time']} ➡️ {tr['exit_time']}", flush=True)
            print(f"   📈 PCR: {tr['pcr']:.2f} | OI: {tr['oi']:,} | ΔOI: {tr['change_oi']:+,}", flush=True)
            print(f"   💵 Entry: ₹{tr['entry_px']:.2f} | 🛑 SL: ₹{tr['sl_px']:.2f} | 🏁 TRG: ₹{tr['trg_px']:.2f} | Exit: ₹{tr['exit_px']:.2f}", flush=True)
            print(f"   📊 पॉईंट्स: {tr['pts_captured']:+.2f} | Net PnL: ₹{tr['net_pnl']:+,.2f} ({tr['exit_reason']})\n", flush=True)

        tg_msg += "🔒 <i>कॅपिटल प्रोटेक्शन: दैनिक उद्दिष्ट पूर्ण झाल्याने दिवस नफ्यात लॉक केला.</i>"
        
        # थेट टेलिग्रामवर पाठवणे
        send_telegram(tg_msg)
        print("=" * 80, flush=True)
    else:
        print("ℹ️ उच्च अचूकता नियमांनुसार आज कोणताही ट्रेड ट्रिगर झाला नाही.", flush=True)

if __name__ == "__main__":
    run_harvest_pnl_engine()
