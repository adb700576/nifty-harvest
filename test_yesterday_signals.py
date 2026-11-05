import os
import sqlite3
import pandas as pd
import requests

DB_PATH = "nifty_vault.db"
BASE_URL = "https://api.upstox.com"
ACCESS_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN") or os.environ.get("UPSTOX_TOKEN") or ""
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

LOT_SIZE = 65
MIN_OPT_SL_PTS = 12.0


def send_telegram(text: str):
    """टेलिग्राम बॉटवर सुरक्षित HTML फॉरमॅटमध्ये निकाल पाठवणे"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] Telegram Secrets (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) उपलब्ध नाहीत.", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            print("✅ Telegram notification delivered successfully.", flush=True)
        else:
            print(f"[-] Telegram API Error: {res.text}", flush=True)
    except Exception as e:
        print(f"[-] Telegram Network Error: {e}", flush=True)


def get_upstox_charges(
    instrument_token: str,
    quantity: int,
    buy_price: float,
    sell_price: float,
    token: str = None,
) -> dict:
    url = f"{BASE_URL}/v2/charges/brokerage"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token or ACCESS_TOKEN}",
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

    total_brokerage = 0.0
    total_stt = 0.0
    total_other = 0.0

    if ACCESS_TOKEN:
        try:
            res_b = requests.get(url, headers=headers, params=buy_params, timeout=2)
            if res_b.status_code == 200:
                db = res_b.json().get("data", {}).get("charges", {})
                total_brokerage += float(db.get("brokerage", 20.0))
                total_stt += float(db.get("stt", 0.0))
                total_other += (
                    float(db.get("gst", 0.0))
                    + float(db.get("stamp_duty", 0.0))
                    + float(db.get("other_charges", 0.0))
                )

            res_s = requests.get(url, headers=headers, params=sell_params, timeout=2)
            if res_s.status_code == 200:
                ds = res_s.json().get("data", {}).get("charges", {})
                total_brokerage += float(ds.get("brokerage", 20.0))
                total_stt += float(ds.get("stt", 0.0))
                total_other += float(ds.get("gst", 0.0)) + float(
                    ds.get("other_charges", 0.0)
                )

            total_taxes = round(total_brokerage + total_stt + total_other, 2)
            if total_taxes > 0:
                return {
                    "total_charges": total_taxes,
                    "brokerage": round(total_brokerage, 2),
                    "stt": round(total_stt, 2),
                }
        except Exception:
            pass

    return {"total_charges": 52.0, "brokerage": 40.0, "stt": 6.5}


def get_atm_strike(spot_price: float, step: int = 50) -> float:
    return round(spot_price / step) * step


def run_harvest_pnl_engine():
    if not os.path.exists(DB_PATH):
        print(f"[-] Database file {DB_PATH} not found.", flush=True)
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='nifty_options_1m'")
    if not cursor.fetchone():
        print("[-] nifty_options_1m टेबल तयार नाही.", flush=True)
        conn.close()
        return

    avail_dates = pd.read_sql_query(
        "SELECT DISTINCT substr(timestamp, 1, 10) as t_date FROM nifty_options_1m ORDER BY t_date DESC",
        conn,
    )
    if avail_dates.empty:
        print("[-] No option candle records available.", flush=True)
        conn.close()
        return

    dates_list = avail_dates["t_date"].tolist()
    target_date = dates_list[0]

    print("=" * 80, flush=True)
    print(f"🚀 [Institutional VWAP + PCR + ΔOI Engine] Target Date: {target_date} (Paper Trade)", flush=True)
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

    # NIFTY 1-मिनिट VWAP आणि EMA गणना
    typical_price = (df_spot["high"] + df_spot["low"] + df_spot["close"]) / 3.0
    vol = df_spot["volume"].replace(0, 1.0)
    df_spot["cum_pv"] = (typical_price * vol).cumsum()
    df_spot["cum_v"] = vol.cumsum()
    df_spot["vwap"] = df_spot["cum_pv"] / df_spot["cum_v"]
    df_spot["ema15"] = df_spot["close"].ewm(span=15, adjust=False).mean()
    spot_records = df_spot.set_index("dt").to_dict("index")

    df_opt["dt"] = pd.to_datetime(df_opt["timestamp"])
    if df_opt["dt"].dt.tz is not None:
        df_opt["dt"] = df_opt["dt"].dt.tz_convert("Asia/Kolkata")
    df_opt = df_opt.drop_duplicates(subset=["symbol", "dt"]).sort_values(by=["symbol", "dt"]).reset_index(drop=True)

    # व्हॉल्यूम, ATR आणि Change in OI (ΔOI) गणना
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

        # Change in OI (मागील कँडलपेक्षा OI मधील बदल)
        sub["change_oi"] = sub["oi"].diff().fillna(0.0)
        all_processed.append(sub)

    if not all_processed:
        print("[-] Insufficient candles for indicator calculation.", flush=True)
        return

    df_data = pd.concat(all_processed, axis=0).sort_values(by=["dt", "symbol"]).reset_index(drop=True)

    all_trades = []
    morning_trades = 0
    afternoon_trades = 0
    active_trade = None
    cooldown_until = None
    daily_target_locked = False
    all_timestamps = df_data["dt"].unique()

    for ts in all_timestamps:
        ts_pd = pd.to_datetime(ts)
        hhmm = ts_pd.strftime("%H:%M")
        snap = df_data[df_data["dt"] == ts_pd]

        # चालू ट्रेड व्यवस्थापन
        if active_trade:
            curr_match = snap[snap["symbol"] == active_trade["symbol"]]
            if not curr_match.empty:
                bar = curr_match.iloc[0]
                exit_p, exit_reason = None, ""
                max_gain = bar["high"] - active_trade["entry_price"]

                # डी-रिस्किंग: +15 पॉईंट्सवर SL थेट कॉस्टवर
                if max_gain >= 15.0:
                    active_trade["opt_sl"] = max(active_trade["opt_sl"], active_trade["entry_price"])

                # ट्रेलिंग: +20 पॉईंट्सच्या पुढे SL ट्रेल करणे
                if max_gain >= 20.0:
                    trailing_level = bar["close"] - 10.0
                    active_trade["opt_sl"] = max(active_trade["opt_sl"], trailing_level)

                if bar["low"] <= active_trade["opt_sl"]:
                    exit_p = active_trade["opt_sl"]
                    exit_reason = "TRAILED_EXIT ✅" if active_trade["opt_sl"] >= active_trade["entry_price"] else "SL_HIT ❌"
                elif bar["high"] >= active_trade["opt_target"]:
                    exit_p = active_trade["opt_target"]
                    exit_reason = "TARGET_HIT ✅"
                    daily_target_locked = True  # मोठा टार्गेट हिट झाल्यानंतर नफा लॉक
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
                    total_charges = charges_dict["total_charges"]
                    net_pnl = round(gross_pnl - total_charges, 2)

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
                    active_trade = None
                    cooldown_until = ts_pd + pd.Timedelta(minutes=15)
            continue

        if daily_target_locked:
            continue

        if cooldown_until and ts_pd < cooldown_until:
            continue

        # ओपनिंग चॉप फिल्टर: 10:15 नंतरच पहिली एन्ट्री शोधणे
        is_morning = ("10:15" <= hhmm <= "11:30") and (morning_trades < 2)
        is_afternoon = ("12:15" <= hhmm <= "14:45") and (afternoon_trades < 2)

        if (active_trade is None) and (is_morning or is_afternoon):
            spot_info = spot_records.get(ts_pd)
            if spot_info is None or pd.isna(spot_info.get("vwap")):
                continue

            cur_spot = spot_info["close"]
            vwap = spot_info["vwap"]
            ema15 = spot_info["ema15"]

            # १. रिअल-टाईम PCR गणना (Put OI / Call OI)
            pe_oi_sum = snap[snap["option_type"] == "PE"]["oi"].sum()
            ce_oi_sum = snap[snap["option_type"] == "CE"]["oi"].sum()
            pcr = round(pe_oi_sum / (ce_oi_sum if ce_oi_sum > 0 else 1.0), 2)

            # २. संस्थात्मक कल (VWAP + EMA + PCR Confluence)
            allowed_side = None
            if (cur_spot > vwap or (hhmm >= "12:15" and cur_spot > ema15)) and pcr >= 0.90:
                allowed_side = "CE"  # बुलिश कल आणि मजबूत पुट बेस
            elif cur_spot < vwap and cur_spot < ema15 and hhmm < "12:15" and pcr <= 1.10:
                allowed_side = "PE"  # बेअरिश कल आणि कॉल रायटिंग

            if not allowed_side:
                continue

            atm_strike = get_atm_strike(cur_spot, step=50)
            atm_candidates = snap[(snap["strike"] == atm_strike) & (snap["option_type"] == allowed_side)]

            for _, row in atm_candidates.iterrows():
                if not (70.0 <= row["close"] <= 320.0):
                    continue

                c_range = row["high"] - row["low"]
                c_body = abs(row["close"] - row["open"])
                b_ratio = c_body / (c_range if c_range > 0 else 1e-9)

                has_vol = row["vol_ratio"] >= 1.15
                is_range_exp = c_range >= (1.10 * row["atr14"])
                is_solid = b_ratio >= 0.40

                # ३. Change in OI फिल्टर: ऑप्शनमध्ये OI वाढलेला असावा (Fresh Buying / Buildup)
                has_oi_buildup = row["change_oi"] >= 0

                if (has_vol or is_range_exp) and is_solid and (row["close"] > row["open"]) and has_oi_buildup:
                    entry_opt_price = float(row["close"])
                    opt_sl_pts = MIN_OPT_SL_PTS
                    opt_target_pts = opt_sl_pts * 2.0  # 1:2 RRR (24 पॉईंट्स)

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

                    if is_morning:
                        morning_trades += 1
                    else:
                        afternoon_trades += 1
                    break

    df_all = pd.DataFrame(all_trades)
    print("\n" + "=" * 80, flush=True)
    print(f"📊 NIFTY ALGO निकाल ({target_date}) [PCR & ΔOI Filtered]", flush=True)
    print("=" * 80, flush=True)

    if not df_all.empty:
        total_trades = len(df_all)
        wins = len(df_all[df_all["net_pnl"] > 0])
        losses = len(df_all[df_all["net_pnl"] <= 0])
        net_total = df_all["net_pnl"].sum()

        print(f"🔹 एकूण ट्रेड्स: {total_trades} (जिंकले: {wins}, तोटा: {losses})", flush=True)
        print(f"💰 निव्वळ नफा (Net PnL): ₹ {net_total:+,.2f}\n", flush=True)
        print("📋 चार्ट लेव्हल्स, PCR व OI आकडेवारी:", flush=True)

        tg_lines = [
            f"📊 <b>NIFTY ALGO निकाल ({target_date})</b>\n",
            f"🔹 एकूण ट्रेड्स: <b>{total_trades}</b> (जिंकले: {wins}, तोटा: {losses})",
            f"💰 निव्वळ नफा (Net PnL): <b>₹ {net_total:+,.2f}</b>\n",
            "📋 <b>ट्रेड तपशील:</b>\n",
        ]

        for idx, tr in df_all.iterrows():
            print(f"{idx + 1}. {tr['side']} | {tr['symbol']}", flush=True)
            print(f"   🎯 NIFTY Spot: {tr['spot_px']} | वेळ: {tr['entry_time']} ➡️ {tr['exit_time']}", flush=True)
            print(f"   📈 PCR: {tr['pcr']:.2f} | OI: {tr['oi']:,} | ΔOI: {tr['change_oi']:+,}", flush=True)
            print(f"   💵 Entry: ₹{tr['entry_px']:.2f} | 🛑 SL: ₹{tr['sl_px']:.2f} | 🏁 TRG: ₹{tr['trg_px']:.2f} | Exit: ₹{tr['exit_px']:.2f}", flush=True)
            print(f"   📊 पॉईंट्स: {tr['pts_captured']:+.2f} | Net PnL: ₹{tr['net_pnl']:+,.2f} ({tr['exit_reason']})\n", flush=True)

            tg_lines.append(
                f"{idx + 1}. <b>{tr['side']}</b> | <code>{tr['symbol']}</code>\n"
                f"   📍 Spot: <code>{tr['spot_px']}</code> | ⏱️ <code>{tr['entry_time']} ➡️ {tr['exit_time']}</code>\n"
                f"   📈 PCR: <b>{tr['pcr']:.2f}</b> | OI: <b>{tr['oi']:,}</b> (ΔOI: <b>{tr['change_oi']:+,}</b>)\n"
                f"   🎯 Buy: <code>₹{tr['entry_px']:.2f}</code> | 🛑 SL: <code>₹{tr['sl_px']:.2f}</code> | 🏁 TRG: <code>₹{tr['trg_px']:.2f}</code>\n"
                f"   📊 पॉईंट्स: <b>{tr['pts_captured']:+.2f}</b> | PnL: <b>₹ {tr['net_pnl']:+,.2f}</b> ({tr['exit_reason']})\n"
            )

        print("=" * 80, flush=True)
        send_telegram("\n".join(tg_lines))
    else:
        print("ℹ️ आजच्या सत्रात PCR व ΔOI नियमांनुसार कोणताही ट्रेड ट्रिगर झाला नाही.", flush=True)


if __name__ == "__main__":
    run_harvest_pnl_engine()
