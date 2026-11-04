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
DELTA_TARGET = 0.52
MIN_OPT_SL_PTS = 10.0


def send_telegram(text: str):
    """टेलिग्राम बॉटवर थेट निकाल पाठवणे"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] Telegram Secrets (TELEGRAM_BOT_TOKEN किंवा TELEGRAM_CHAT_ID) सापडले नाहीत.", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            print("✅ टेलिग्रामवर निकाल यशस्वीरीत्या पाठवला!", flush=True)
        else:
            print(f"[-] Telegram API त्रुटी: {res.text}", flush=True)
    except Exception as e:
        print(f"[-] Telegram Network त्रुटी: {e}", flush=True)


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


def run_harvest_pnl_engine():
    if not os.path.exists(DB_PATH):
        print(f"[-] {DB_PATH} सापडला नाही. आधी Harvester चालवून डेटा गोळा करा.", flush=True)
        return

    conn = sqlite3.connect(DB_PATH)
    avail_dates = pd.read_sql_query(
        "SELECT DISTINCT substr(timestamp, 1, 10) as t_date FROM nifty_options_1m ORDER BY t_date DESC",
        conn,
    )
    if avail_dates.empty:
        print("[-] व्हॉल्टमध्ये कँडल्स सापडल्या नाहीत.", flush=True)
        conn.close()
        return

    dates_list = avail_dates["t_date"].tolist()
    target_date = dates_list[0]

    print("=" * 80, flush=True)
    print(f"🚀 [Auto PnL Engine] चाचणी सुरू: {target_date} (Paper Trade Mode)", flush=True)
    print("=" * 80, flush=True)

    query = f"""
        SELECT timestamp, symbol, strike, option_type, open, high, low, close, volume, oi, delta
        FROM nifty_options_1m
        WHERE timestamp LIKE '{target_date}%'
    """
    df = pd.read_sql_query(query, conn)
    conn.close()

    if df.empty:
        print(f"[-] {target_date} साठी डेटा उपलब्ध नाही.", flush=True)
        return

    # १. अचूक IST टाईमस्टॅम्प पार्सिंग व सॉर्टिंग
    df["dt"] = pd.to_datetime(df["timestamp"])
    if df["dt"].dt.tz is not None:
        df["dt"] = df["dt"].dt.tz_convert("Asia/Kolkata")
    df["hhmm"] = df["dt"].dt.strftime("%H:%M")
    df = df.drop_duplicates(subset=["symbol", "dt"]).sort_values(by=["symbol", "dt"]).reset_index(drop=True)

    all_processed = []
    for sym in df["symbol"].unique():
        sub = df[df["symbol"] == sym].copy().sort_values("dt").reset_index(drop=True)
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
        all_processed.append(sub)

    if not all_processed:
        print("[-] तांत्रिक विश्लेषणासाठी पुरेशा कँडल्स नाहीत.", flush=True)
        return

    df_data = pd.concat(all_processed, axis=0).sort_values(by=["dt", "symbol"]).reset_index(drop=True)

    all_trades = []
    morning_trades = 0
    afternoon_trades = 0
    active_trade = None
    cooldown_until = None
    all_timestamps = df_data["dt"].unique()

    for ts in all_timestamps:
        ts_pd = pd.to_datetime(ts)
        hhmm = ts_pd.strftime("%H:%M")
        snap = df_data[df_data["dt"] == ts_pd]

        # चालू ट्रेड मॉनिटरिंग
        if active_trade:
            curr_match = snap[snap["symbol"] == active_trade["symbol"]]
            if not curr_match.empty:
                bar = curr_match.iloc[0]
                exit_p, exit_reason = None, ""

                if bar["low"] <= active_trade["opt_sl"]:
                    exit_p = active_trade["opt_sl"]
                    exit_reason = "SL_HIT ❌"
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
                    total_charges = charges_dict["total_charges"]
                    net_pnl = round(gross_pnl - total_charges, 2)

                    all_trades.append({
                        "date": target_date,
                        "side": active_trade["side"],
                        "symbol": active_trade["symbol"],
                        "entry_time": active_trade["entry_time"].strftime("%H:%M"),
                        "exit_time": hhmm,
                        "entry_px": active_trade["entry_price"],
                        "sl_px": active_trade["opt_sl"],
                        "trg_px": active_trade["opt_target"],
                        "exit_px": round(exit_p, 2),
                        "pts_captured": captured_pts,
                        "net_pnl": net_pnl,
                        "exit_reason": exit_reason,
                    })
                    active_trade = None
                    cooldown_until = ts_pd + pd.Timedelta(minutes=15)
            continue

        if cooldown_until and ts_pd < cooldown_until:
            continue

        is_morning = ("09:30" <= hhmm <= "11:30") and (morning_trades < 2)
        is_afternoon = ("12:30" <= hhmm <= "14:45") and (afternoon_trades < 2)

        if (active_trade is None) and (is_morning or is_afternoon):
            candidates = []
            for _, row in snap.iterrows():
                # लिक्विड प्रीमियम रेंज (₹७० ते ₹३२०)
                if not (70.0 <= row["close"] <= 320.0):
                    continue

                c_range = row["high"] - row["low"]
                c_body = abs(row["close"] - row["open"])
                b_ratio = c_body / (c_range if c_range > 0 else 1e-9)

                has_vol = row["vol_ratio"] >= 1.25
                is_range_exp = c_range >= (1.20 * row["atr14"])
                is_solid = b_ratio >= 0.40

                if (has_vol or is_range_exp) and is_solid and (row["close"] > row["open"]):
                    candidates.append(row)

            if candidates:
                cand_df = pd.DataFrame(candidates)
                cand_df["atm_dist"] = (cand_df["delta"].abs() - 0.50).abs()
                best = cand_df.sort_values(by="atm_dist").iloc[0]

                entry_opt_price = float(best["close"])
                calc_sl_pts = best["atr14"] * 1.2 * DELTA_TARGET
                opt_sl_pts = max(MIN_OPT_SL_PTS, calc_sl_pts)
                opt_target_pts = opt_sl_pts * 1.50

                active_trade = {
                    "symbol": best["symbol"],
                    "side": f"BUY_{best['option_type']}",
                    "entry_time": ts_pd,
                    "entry_price": entry_opt_price,
                    "opt_sl": max(0.05, round(entry_opt_price - opt_sl_pts, 2)),
                    "opt_target": round(entry_opt_price + opt_target_pts, 2),
                }

                if is_morning:
                    morning_trades += 1
                else:
                    afternoon_trades += 1

    df_all = pd.DataFrame(all_trades)
    print("\n" + "=" * 80, flush=True)
    print(f"📊 *NIFTY ALGO निकाल ({target_date})*", flush=True)
    print("=" * 80, flush=True)

    if not df_all.empty:
        total_trades = len(df_all)
        wins = len(df_all[df_all["net_pnl"] > 0])
        losses = len(df_all[df_all["net_pnl"] <= 0])
        net_total = df_all["net_pnl"].sum()

        print(f"🔹 एकूण ट्रेड्स: {total_trades} (जिंकले: {wins}, तोटा: {losses})", flush=True)
        print(f"💰 निव्वळ नफा (Net PnL): ₹ {net_total:+,.2f}\n", flush=True)
        print("📋 चार्ट लेव्हल्स व अचूक किमती:", flush=True)

        tg_lines = [
            f"📊 *NIFTY ALGO निकाल ({target_date})*\n",
            f"🔹 *एकूण ट्रेड्स:* `{total_trades}` (जिंकले: `{wins}`, तोटा: `{losses}`)",
            f"💰 *निव्वळ नफा (Net PnL):* `₹ {net_total:+,.2f}`\n",
            "📋 *ट्रेड तपशील:*",
        ]

        for idx, tr in df_all.iterrows():
            print(f"{idx + 1}. {tr['side']} | {tr['symbol']}", flush=True)
            print(f"   ⏱️ वेळ: {tr['entry_time']} ➡️ {tr['exit_time']}", flush=True)
            print(f"   🎯 Entry: ₹{tr['entry_px']:.2f} | 🛑 SL: ₹{tr['sl_px']:.2f} | 🏁 TRG: ₹{tr['trg_px']:.2f} | Exit: ₹{tr['exit_px']:.2f}", flush=True)
            print(f"   💵 पॉईंट्स: {tr['pts_captured']:+.2f} | Net PnL: ₹{tr['net_pnl']:.2f} ({tr['exit_reason']})\n", flush=True)

            tg_lines.append(
                f"{idx + 1}. *{tr['side']}* | `{tr['symbol']}`\n"
                f"   ⏱️ {tr['entry_time']} ➡️ {tr['exit_time']} | पॉईंट्स: `{tr['pts_captured']:+.2f}`\n"
                f"   🎯 Buy: `{tr['entry_px']:.2f}` | 🛑 SL: `{tr['sl_px']:.2f}` | 🏁 TRG: `{tr['trg_px']:.2f}`\n"
                f"   💵 PnL: `₹ {tr['net_pnl']:.2f}` ({tr['exit_reason']})\n"
            )

        print("=" * 80, flush=True)
        send_telegram("\n".join(tg_lines))
    else:
        print("ℹ️ आजच्या सत्रात कोणताही ट्रेड ट्रिगर झाला नाही.", flush=True)


if __name__ == "__main__":
    run_harvest_pnl_engine()
