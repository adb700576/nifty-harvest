import os
import glob
from pathlib import Path
from datetime import datetime, timezone, timedelta
import pandas as pd
import requests

JOURNAL_DIR = Path("live_journal")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

def now_ist() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"[-] Telegram dispatch error: {e}")

def run_journal_analytics():
    csv_files = glob.glob(str(JOURNAL_DIR / "trades_journal_*.csv"))
    if not csv_files:
        print("[-] कोणतीही ट्रेड जर्नल्स सापडली नाहीत.")
        return

    dfs = []
    for f in csv_files:
        if "master_trading_journal.csv" in f:
            continue
        try:
            temp = pd.read_csv(f)
            if not temp.empty:
                dfs.append(temp)
        except Exception:
            pass

    if not dfs:
        print("[-] जर्नल्समध्ये ट्रेड्स सापडले नाहीत.")
        return

    df = pd.concat(dfs, ignore_index=True)
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df = df.sort_values("entry_time").reset_index(drop=True)

    # मास्टर CSV सेव्ह करणे
    master_path = JOURNAL_DIR / "master_trading_journal.csv"
    df.to_csv(master_path, index=False)

    today_str = now_ist().strftime("%Y-%m-%d")
    df["date"] = df["entry_time"].dt.strftime("%Y-%m-%d")
    df["week_num"] = df["entry_time"].dt.isocalendar().week
    df["month_str"] = df["entry_time"].dt.strftime("%Y-%m")
    df["year"] = df["entry_time"].dt.strftime("%Y")

    def calc_stats(sub_df):
        total = len(sub_df)
        if total == 0:
            return 0, 0, 0, 0.0, 0.0, 0.0
        wins = len(sub_df[sub_df["net_pnl"] > 0])
        losses = len(sub_df[sub_df["net_pnl"] < 0])
        win_rate = (wins / total) * 100
        net_pnl = sub_df["net_pnl"].sum()
        pts = sub_df["pts"].sum() if "pts" in sub_df.columns else 0.0
        return total, wins, losses, win_rate, net_pnl, pts

    # १. आजचे निकाल
    df_today = df[df["date"] == today_str]
    t_tot, t_win, t_loss, t_wr, t_pnl, t_pts = calc_stats(df_today)

    # २. चालू आठवड्याचे निकाल
    current_week = now_ist().isocalendar().week
    df_week = df[df["week_num"] == current_week]
    w_tot, w_win, w_loss, w_wr, w_pnl, _ = calc_stats(df_week)

    # ३. चालू महिन्याचे निकाल (आठवड्यानुसार विभागणी)
    current_month_str = now_ist().strftime("%Y-%m")
    df_month = df[df["month_str"] == current_month_str]
    m_tot, m_win, m_loss, m_wr, m_pnl, _ = calc_stats(df_month)

    # ४. एकूण ऑल-टाइम / वार्षिक निकाल
    all_tot, all_win, all_loss, all_wr, all_pnl, _ = calc_stats(df)
    total_charges = df["charges"].sum() if "charges" in df.columns else 0.0

    msg = (
        f"📊 <b>NIFTY 5M ALGO: मास्टर ट्रेड जर्नल रिपोर्ट</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 <b>आजचा निकाल ({today_str})</b>\n"
        f"• ट्रेड्स: <code>{t_tot}</code> (🟢 {t_win} | 🔴 {t_loss})\n"
        f"• Win Rate: <code>{t_wr:.1f}%</code> | निव्वळ पॉईंट्स: <code>{t_pts:+.2f} pts</code>\n"
        f"• निव्वळ PnL (Net): <b>₹ {t_pnl:+,.2f}</b>\n\n"
        f"📈 <b>चालू आठवडा (W{current_week})</b>\n"
        f"• एकूण ट्रेड्स: <code>{w_tot}</code> | Win Rate: <code>{w_wr:.1f}%</code>\n"
        f"• आठवड्याचा Net PnL: <b>₹ {w_pnl:+,.2f}</b>\n\n"
        f"🗓️ <b>चालू महिना ({now_ist().strftime('%B %Y')})</b>\n"
    )

    month_weeks = df_month.groupby("week_num")
    idx = 1
    for w_num, group in month_weeks:
        _, _, _, sub_wr, sub_pnl, _ = calc_stats(group)
        msg += f"• Week {idx} (W{w_num}): {len(group)} ट्रेड्स | {sub_wr:.0f}% Win | ₹ {sub_pnl:+,.2f}\n"
        idx += 1

    msg += (
        f"• <b>महिन्याचा एकूण निकाल: ₹ {m_pnl:+,.2f}</b> (Win Rate: {m_wr:.1f}%)\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 <b>वार्षिक / ऑल-टाइम निकाल ({now_ist().strftime('%Y')})</b>\n"
        f"• एकूण ट्रेड्स: <code>{all_tot}</code> (🟢 {all_win} | 🔴 {all_loss})\n"
        f"• एकूण Win Rate: <code>{all_wr:.1f}%</code>\n"
        f"• ब्रोकरेज व टॅक्स: <code>₹ {total_charges:,.2f}</code>\n"
        f"• <b>एकूण निव्वळ नफा (Net PnL): ₹ {all_pnl:+,.2f}</b>"
    )

    send_telegram(msg)
    print("[+] मास्टर जर्नल ॲनालिटिक्स यशस्वीरीत्या पाठवले!")

if __name__ == "__main__":
    run_journal_analytics()
