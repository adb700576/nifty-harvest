import os
import io
import csv
import sys
import json
import time
import math
import signal
import threading
import logging
import requests
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from collections import deque
from datetime import datetime, timedelta, timezone

try:
    import upstox_client
    HAS_UPSTOX_SDK = True
except ImportError:
    HAS_UPSTOX_SDK = False

# ==============================================================================
# 1. CONFIGURATION, SELF-TUNED PARAMS & GOOGLE SHEETS SYNC
# ==============================================================================
IST = timezone(timedelta(hours=5, minutes=30))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("V3_ENGINE")

UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GSHEET_WEBHOOK_URL = os.getenv("GSHEET_WEBHOOK_URL", "").strip()
PAPER_MODE = os.getenv("PAPER_MODE", "true").strip().lower() == "true"

# --- STRATEGY & RISK PARAMETERS ---
MIN_FUEL_SCORE = 45.0             # Base minimum fuel score
MCX_EVENING_FUEL_SCORE = 55.0     # Stricter fuel filter for volatile evening MCX (>= 15:30 IST)
MCX_MAX_RISK_INR = 2000.0         # Maximum allowed SL risk (₹) per MCX trade
MIN_ADX_TREND = 18.0              # Below this ADX = Sideways Market (Sleep Mode)
MAX_CHOP_INDEX = 61.8             # Above this Choppiness Index = Sideways Market
SESSION_MAX_RUNTIME_SEC = 21100   # 5h 51m 40s Zero-Gap overlap right up to next cron trigger

INDEX_START_TIME = "09:20"
INDEX_END_TIME = "15:10"
INDEX_EXIT_TIME = "15:15"
EXPIRY_NEXT_SWITCH_TIME = "13:30"

MCX_START_TIME = "09:20"
MCX_END_TIME = "23:00"
MCX_EXIT_TIME = "23:15"

GANN_ZONE_ATR_MULT = 0.40
MAX_SPREAD_PCT = 1.50
PIVOT_BUFFER_ATR_MULT = 0.12      # Tight buffer beyond true Fractal Swing Low/High
SETUP_EXPIRY_SEC = 900
DASHBOARD_REFRESH_SEC = 30

JOURNAL_FILE = "live_journal/trade_journal.csv"
ACTIVE_STATE_FILE = "live_journal/active_positions.json"
PARAMS_FILE = "live_journal/strategy_params.json"
MEMORY_FILE = "live_journal/market_memory.json"

# --- UNCAPPED MOMENTUM RUNNER & TRAILING SL STAGES (1R = Initial SL Risk) ---
STAGE1_BE_R = 1.5                 # At 1.5R gain -> Move SL to Cost-to-Cost
STAGE2_LOCK_R = 2.5               # At 2.5R gain -> Lock 1.5R floor + Swing Trail
STAGE3_TREND_R = 4.0              # At 4.0R gain -> Trail at 70% of Peak Gain
STAGE4_MOONSHOT_R = 8.0           # At 8.0R+ (e.g. ₹5 -> ₹100/₹400) -> Trail at 80% of Peak Gain

FUEL_FINISH_MIN_R = 1.5           # Allow momentum exhaustion exit only after >= 1.5R secured
FUEL_FINISH_THRESHOLD = 18.0      # Exit only when momentum truly dies (Fuel < 18)

FLAT_LTP_NORMAL_SEC = 900
FLAT_LTP_EXPIRY_SEC = 600

CAPITAL_PER_TRADE = float(os.getenv("CAPITAL_PER_TRADE", "15000"))
MCX_CAPITAL_PER_TRADE = float(os.getenv("MCX_CAPITAL_PER_TRADE", os.getenv("CRUDEOIL_CAPITAL", "150000")))
DAILY_PROFIT_LOCK_PCT = 1.00
DAILY_LOSS_LOCK_PCT = -0.35
MAX_CONSECUTIVE_LOSSES = 3

INDICES = {
    "NIFTY":      {"type": "INDEX_OPT", "spot_key": "NSE_INDEX|Nifty 50"},
    "BANKNIFTY":  {"type": "INDEX_OPT", "spot_key": "NSE_INDEX|Nifty Bank"},
    "FINNIFTY":   {"type": "INDEX_OPT", "spot_key": "NSE_INDEX|Nifty Fin Service"},
    "MIDCPNIFTY": {"type": "INDEX_OPT", "spot_key": "NSE_INDEX|NIFTY MID SELECT"},
    "SENSEX":     {"type": "INDEX_OPT", "spot_key": "BSE_INDEX|SENSEX"},
    "CRUDEOIL":   {"type": "MCX_FUT"},
    "NATURALGAS": {"type": "MCX_FUT"},
    "GOLD":       {"type": "MCX_FUT"},
    "SILVER":     {"type": "MCX_FUT"},
    "COPPER":     {"type": "MCX_FUT"},
}


def round_to_tick(price: float, tick_size: float) -> float:
    ts = tick_size if tick_size and tick_size > 0 else 0.05
    decimals = max(0, len(f"{ts:.10f}".rstrip("0").split(".")[1])) if "." in f"{ts:.10f}".rstrip("0") else 0
    return round(round(price / ts) * ts, decimals)


def is_scheduled_handover_window(now_ist: datetime) -> bool:
    """
    Returns True during scheduled GitHub cron handover windows:
    - Afternoon handover: 14:45 to 14:58 IST
    - Evening handover:   20:40 to 20:55 IST
    Allows SIGTERM triggered by the incoming session cron to hand over open positions
    cleanly instead of treating it as a manual emergency stop.
    """
    hhmm = now_ist.strftime("%H:%M")
    return ("14:45" <= hhmm <= "14:58") or ("20:40" <= hhmm <= "20:55")


def push_trade_to_google_sheet(trade_row: dict):
    if not GSHEET_WEBHOOK_URL:
        return
    try:
        requests.post(
            GSHEET_WEBHOOK_URL,
            json={"action": "LOG_TRADE", "trade": trade_row},
            timeout=8,
        )
        log.info(f"[GSHEET] Synced Trade #{trade_row['trade_no']} to Google Sheets.")
    except Exception as e:
        log.warning(f"[GSHEET] Sync warning: {e}")


# ==============================================================================
# 2. TELEGRAM UI, CHARTS & CHARGES
# ==============================================================================
def notify(msg: str, silent: bool = False):
    clean_lines = [line.strip() for line in msg.splitlines() if line.strip()]
    log.info(f"[MIRROR] {' | '.join(clean_lines)}")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_notification": silent}
    for attempt in range(2):
        try:
            requests.post(url, json=payload, timeout=8)
            return
        except Exception as e:
            if attempt == 0:
                time.sleep(0.5)
            else:
                log.warning(f"Telegram send error after retry: {e}")


def make_r_bar(r_mult: float, length: int = 8) -> str:
    clamped = max(0.0, min(1.0, r_mult / 8.0))
    filled = int(round(clamped * length))
    return "▰" * filled + "▱" * (length - filled)


def send_trade_alert_with_chart(df: pd.DataFrame, title: str, caption: str, gann_lvl: float, spot_entry: float, spot_sl: float, is_exit: bool = False, spot_exit: float = None):
    clean_lines = [line.strip() for line in caption.splitlines() if line.strip()]
    log.info(f"[MIRROR] {' | '.join(clean_lines)}")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    if not df.empty:
        try:
            plot_df = df.tail(36).copy()
            fig, ax = plt.subplots(figsize=(10.5, 5.0), dpi=140)
            fig.patch.set_facecolor("#0f172a")
            ax.set_facecolor("#1e293b")

            ax.plot(plot_df["timestamp"], plot_df["close"], color="#f8fafc", linewidth=2.1, label="Price 5M")
            ax.axhline(gann_lvl, color="#38bdf8", linestyle="--", linewidth=1.3, label=f"Gann Pivot: {gann_lvl:.1f}")
            ax.axhline(spot_sl, color="#f43f5e", linestyle=":", linewidth=1.6, label=f"Pivot Swing SL: {spot_sl:.1f}")
            ax.axhspan(min(spot_entry, spot_sl), max(spot_entry, spot_sl), color="#f43f5e", alpha=0.08)

            entry_ts = plot_df["timestamp"].iloc[-1] if not is_exit else plot_df["timestamp"].iloc[max(0, len(plot_df) - 4)]
            ax.scatter([entry_ts], [spot_entry], color="#22c55e", edgecolors="#ffffff", linewidth=1.2, s=85, zorder=5, label=f"Entry: {spot_entry:.1f}")

            if is_exit and spot_exit is not None:
                exit_ts = plot_df["timestamp"].iloc[-1]
                ax.scatter([exit_ts], [spot_exit], color="#f59e0b", edgecolors="#ffffff", linewidth=1.2, marker="X", s=100, zorder=6, label=f"Exit: {spot_exit:.1f}")

            ax.set_title(title, fontsize=11.5, fontweight="bold", color="#f8fafc", pad=12)
            ax.tick_params(colors="#cbd5e1", labelsize=8.5)
            for spine in ax.spines.values():
                spine.set_color("#334155")
            ax.grid(True, linestyle="--", color="#334155", alpha=0.55)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %H:%M", tz=IST))
            leg = ax.legend(loc="upper left", fontsize=8.5, facecolor="#0f172a", edgecolor="#334155", labelcolor="#f8fafc")
            leg.get_frame().set_alpha(0.92)
            plt.tight_layout()

            buf = io.BytesIO()
            plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
            plt.close(fig)
            buf.seek(0)

            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            files = {"photo": ("setup_chart.png", buf, "image/png")}
            data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
            resp = requests.post(url, data=data, files=files, timeout=12).json()
            if resp.get("ok"):
                return
        except Exception as e:
            log.warning(f"Chart send failed, falling back to text alert: {e}")

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": caption, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception as e:
        log.warning(f"Text alert fallback error: {e}")


def upsert_telegram_dashboard(text: str, message_id: int = None) -> int:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return message_id

    if message_id:
        edit_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id, "text": text, "parse_mode": "HTML"}
        try:
            resp = requests.post(edit_url, json=payload, timeout=8).json()
            if resp.get("ok") or "message is not modified" in str(resp.get("description", "")).lower():
                return message_id
        except Exception as e:
            log.warning(f"Dashboard edit error: {e}")

    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_notification": True}
    try:
        resp = requests.post(send_url, json=payload, timeout=8).json()
        if resp.get("ok"):
            new_id = resp["result"]["message_id"]
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/pinChatMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "message_id": new_id, "disable_notification": True},
                timeout=5,
            )
            return new_id
    except Exception as e:
        log.warning(f"Dashboard send error: {e}")
    return message_id


def upstox_headers():
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def fetch_exact_trade_charges(instrument_key: str, buy_price: float, sell_price: float, qty: int, is_mcx: bool) -> dict:
    if buy_price > 0 and sell_price > 0 and qty > 0:
        if HAS_UPSTOX_SDK:
            try:
                cfg = upstox_client.Configuration()
                cfg.access_token = UPSTOX_ACCESS_TOKEN
                charge_api = upstox_client.ChargeApi(upstox_client.ApiClient(cfg))
                buy_resp = charge_api.get_brokerage(instrument_key, int(qty), "I", "BUY", round(float(buy_price), 2), "2.0")
                sell_resp = charge_api.get_brokerage(instrument_key, int(qty), "I", "SELL", round(float(sell_price), 2), "2.0")
                b_brk = float(buy_resp.data.charges.brokerage or 0.0)
                s_brk = float(sell_resp.data.charges.brokerage or 0.0)
                b_tot = float(buy_resp.data.charges.total or 0.0)
                s_tot = float(sell_resp.data.charges.total or 0.0)
                if (b_tot + s_tot) > 0:
                    tot = round(b_tot + s_tot, 2)
                    brk = round(b_brk + s_brk, 2)
                    return {"brokerage": brk, "taxes_other": round(tot - brk, 2), "total": tot}
            except Exception:
                pass

        url = "https://api.upstox.com/v2/charges/brokerage"
        tot_sum, brk_sum, api_ok = 0.0, 0.0, True
        for tx_type, price in (("BUY", buy_price), ("SELL", sell_price)):
            params = {
                "instrument_token": instrument_key,
                "quantity": int(qty),
                "product": "I",
                "transaction_type": tx_type,
                "price": round(float(price), 2),
            }
            try:
                res = requests.get(url, headers=upstox_headers(), params=params, timeout=8).json()
                if res.get("status") == "success":
                    ch_obj = res.get("data", {}).get("charges", {})
                    tot_sum += float(ch_obj.get("total", 0.0))
                    brk_sum += float(ch_obj.get("brokerage", 0.0))
                else:
                    api_ok = False
                    break
            except Exception:
                api_ok = False
                break

        if api_ok and tot_sum > 0:
            tot = round(tot_sum, 2)
            brk = round(brk_sum, 2)
            return {"brokerage": brk, "taxes_other": round(tot - brk, 2), "total": tot}

    turnover = (buy_price + sell_price) * qty
    brokerage = 40.0
    if is_mcx:
        stt_ctt = (sell_price * qty) * 0.0001
        txn_charges = turnover * 0.000021
        stamp_duty = (buy_price * qty) * 0.00002
    else:
        stt_ctt = (sell_price * qty) * 0.000625
        txn_charges = turnover * 0.0005
        stamp_duty = (buy_price * qty) * 0.00003
    sebi_charges = turnover * 0.000001
    gst = (brokerage + txn_charges + sebi_charges) * 0.18
    taxes_other = round(stt_ctt + txn_charges + stamp_duty + sebi_charges + gst, 2)
    return {"brokerage": brokerage, "taxes_other": taxes_other, "total": round(brokerage + taxes_other, 2)}


def record_trade_journal(row: dict):
    os.makedirs(os.path.dirname(JOURNAL_FILE), exist_ok=True)
    file_exists = os.path.isfile(JOURNAL_FILE)
    fieldnames = [
        "trade_no", "date", "entry_time", "exit_time", "symbol", "contract", "side",
        "qty", "entry_price", "exit_price", "points", "sl", "target", "exit_reason",
        "gross_pnl", "brokerage", "taxes_other", "charges", "pnl", "day_pnl", "setup",
    ]
    try:
        with open(JOURNAL_FILE, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
        log.info(f"[JOURNAL] Saved #{row['trade_no']} to {JOURNAL_FILE}: {row['contract']} Net PnL ₹{row['pnl']:+.2f}")
    except Exception as e:
        log.error(f"Journal write error: {e}")

    push_trade_to_google_sheet(row)


def build_master_journal_report(now_ist: datetime) -> str:
    today_str = now_ist.strftime("%Y-%m-%d")
    curr_year, curr_week, _ = now_ist.isocalendar()
    curr_month_prefix = now_ist.strftime("%Y-%m")
    month_label = now_ist.strftime("%B %Y")

    rows = []
    if os.path.isfile(JOURNAL_FILE):
        try:
            with open(JOURNAL_FILE, mode="r", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    try:
                        d_obj = datetime.strptime(r["date"], "%Y-%m-%d")
                        y_iso, w_iso, _ = d_obj.isocalendar()
                        rows.append({
                            "date": r["date"], "year": y_iso, "week": w_iso,
                            "points": float(r.get("points", 0.0)),
                            "brokerage": float(r.get("brokerage", 0.0)),
                            "taxes_other": float(r.get("taxes_other", 0.0)),
                            "charges": float(r.get("charges", 0.0)),
                            "pnl": float(r.get("pnl", 0.0)),
                        })
                    except Exception:
                        continue
        except Exception:
            pass

    def summarize(sub_rows):
        total = len(sub_rows)
        wins = sum(1 for x in sub_rows if x["pnl"] > 0)
        losses = sum(1 for x in sub_rows if x["pnl"] <= 0)
        wr = round((wins / total) * 100.0, 1) if total > 0 else 0.0
        pts = round(sum(x["points"] for x in sub_rows), 2)
        brk = round(sum(x["brokerage"] for x in sub_rows), 2)
        tax = round(sum(x["taxes_other"] for x in sub_rows), 2)
        chg = round(sum(x["charges"] for x in sub_rows), 2)
        net = round(sum(x["pnl"] for x in sub_rows), 2)
        return total, wins, losses, wr, pts, brk, tax, chg, net

    d_tot, d_win, d_loss, d_wr, d_pts, d_brk, d_tax, d_chg, d_net = summarize([r for r in rows if r["date"] == today_str])
    w_tot, _, _, w_wr, _, _, _, _, w_net = summarize([r for r in rows if r["year"] == curr_year and r["week"] == curr_week])

    month_rows = [r for r in rows if r["date"].startswith(curr_month_prefix)]
    m_tot, _, _, m_wr, _, _, _, _, m_net = summarize(month_rows)

    weeks_in_month = sorted({r["week"] for r in month_rows})
    week_lines = []
    for idx_w, wk in enumerate(weeks_in_month, start=1):
        wt, _, _, wwr, _, _, _, _, wnet = summarize([r for r in month_rows if r["week"] == wk])
        week_lines.append(f"  ├ Week {idx_w} (W{wk}): {wt} ट्रेड्स │ {wwr:.0f}% Win │ <b>₹ {wnet:+,.2f}</b>")
    if not week_lines:
        week_lines.append(f"  └ Week 1 (W{curr_week}): 0 ट्रेड्स │ 0% Win │ ₹ +0.00")

    y_rows = [r for r in rows if r["year"] == curr_year]
    y_tot, y_win, y_loss, y_wr, _, y_brk, y_tax, y_chg, y_net = summarize(y_rows)

    return "\n".join([
        "╔══════════════════════════════╗",
        "  📊 <b>V3 ALGO • मास्टर ट्रेड जर्नल रिपोर्ट</b>",
        "╚══════════════════════════════╝",
        f"📅 <b>आजचा निकाल ({today_str})</b>",
        f"  • ट्रेड्स: <b>{d_tot}</b> (🟢 {d_win} │ 🔴 {d_loss}) │ Win Rate: <b>{d_wr:.1f}%</b>",
        f"  • पॉईंट्स: <b>{d_pts:+.2f} pts</b> │ चार्जेस: ₹{d_chg:,.2f} (Brk ₹{d_brk:,.0f} + Tax ₹{d_tax:,.0f})",
        f"  • आजचा निव्वळ नफा (Net PnL): <b>₹ {d_net:+,.2f}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📈 <b>चालू आठवडा (W{curr_week})</b>",
        f"  • एकूण ट्रेड्स: <b>{w_tot}</b> │ Win Rate: <b>{w_wr:.1f}%</b> │ Net: <b>₹ {w_net:+,.2f}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🗓️ <b>चालू महिना ({month_label})</b>",
        *week_lines,
        f"  └ एकूण महिना: <b>₹ {m_net:+,.2f}</b> (Win Rate: {m_wr:.1f}%)",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🏆 <b>वार्षिक / ऑल-टाइम निकाल ({curr_year})</b>",
        f"  • एकूण ट्रेड्स: <b>{y_tot}</b> (🟢 {y_win} │ 🔴 {y_loss}) │ Win Rate: <b>{y_wr:.1f}%</b>",
        f"  • एकूण ब्रोकरेज व टॅक्स: <b>₹ {y_chg:,.2f}</b> (Brk ₹{y_brk:,.0f} + Tax ₹{y_tax:,.0f})",
        f"  • एकूण निव्वळ नफा (Net PnL): <b>₹ {y_net:+,.2f}</b>",
    ])


# ==============================================================================
# 3. UPSTOX V3 API HELPERS
# ==============================================================================
def get_active_future_contract(idx: str) -> dict:
    exchange = "BSE" if idx == "SENSEX" else ("MCX" if INDICES[idx]["type"] == "MCX_FUT" else "NSE")
    url = "https://api.upstox.com/v2/instruments/search"

    for expiry_kw in ("current_month", "next_month"):
        try:
            params = {"query": idx, "exchanges": exchange, "segments": "FUT", "expiry": expiry_kw, "page_number": 1, "records": 30}
            items = requests.get(url, headers=upstox_headers(), params=params, timeout=10).json().get("data", [])
            if not items:
                continue
            exact = [
                it for it in items
                if str(it.get("instrument_type", "")).upper() == "FUT"
                and str(it.get("trading_symbol", "")).upper().startswith(f"{idx} FUT")
            ]
            candidates = exact if exact else [it for it in items if str(it.get("instrument_type", "")).upper() == "FUT"]
            if not candidates:
                continue
            candidates.sort(key=lambda x: str(x.get("expiry", "9999-12-31")))
            top = candidates[0]
            raw_tick = float(top.get("tick_size", 0.05))
            tick_size = round(raw_tick / 100.0, 4) if raw_tick >= 5.0 else raw_tick
            return {
                "instrument_key": str(top.get("instrument_key", "")),
                "lot_size": int(top.get("lot_size", 1)),
                "freeze_quantity": int(top.get("freeze_quantity", 10000)),
                "tick_size": tick_size if tick_size > 0 else 0.05,
                "trading_symbol": str(top.get("trading_symbol", idx)),
                "expiry": str(top.get("expiry", "")),
            }
        except Exception as e:
            log.warning(f"[{idx}] Instrument search error ({expiry_kw}): {e}")
    return {}


def get_dynamic_option_specs(spot_key: str) -> dict:
    try:
        contracts = requests.get(
            "https://api.upstox.com/v2/option/contract",
            headers=upstox_headers(),
            params={"instrument_key": spot_key},
            timeout=10,
        ).json().get("data", [])
        if not contracts:
            return {"expiries": [], "lot_size": 1, "freeze_quantity": 1800, "tick_size": 0.05, "step": 50}

        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        expiries = sorted({c.get("expiry") for c in contracts if c.get("expiry") and c.get("expiry") >= today_str})
        nearest_exp = expiries[0] if expiries else contracts[0].get("expiry")
        exp_contracts = [c for c in contracts if c.get("expiry") == nearest_exp] or contracts

        sample = exp_contracts[0]
        lot_size = int(sample.get("lot_size", 1))
        freeze_qty = int(sample.get("freeze_quantity", lot_size * 24))
        raw_tick = float(sample.get("tick_size", 0.05))
        tick_size = round(raw_tick / 100.0, 4) if raw_tick >= 5.0 else raw_tick

        strikes = sorted({float(c.get("strike_price", 0)) for c in exp_contracts if float(c.get("strike_price", 0)) > 0})
        diffs = [round(strikes[i + 1] - strikes[i], 2) for i in range(len(strikes) - 1) if (strikes[i + 1] - strikes[i]) > 0]
        step = int(min(diffs)) if diffs else 50
        return {
            "expiries": expiries,
            "lot_size": lot_size,
            "freeze_quantity": freeze_qty,
            "tick_size": tick_size if tick_size > 0 else 0.05,
            "step": max(step, 1),
        }
    except Exception:
        return {"expiries": [], "lot_size": 1, "freeze_quantity": 1800, "tick_size": 0.05, "step": 50}


def fetch_intraday_5m(instrument_key: str) -> pd.DataFrame:
    if not instrument_key:
        return pd.DataFrame()
    encoded_key = requests.utils.quote(instrument_key, safe="")
    url = f"https://api.upstox.com/v3/historical-candle/intraday/{encoded_key}/minutes/5"
    try:
        candles = requests.get(url, headers=upstox_headers(), timeout=10).json().get("data", {}).get("candles", [])
        if not candles:
            return pd.DataFrame()
        df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        for col in ["open", "high", "low", "close", "volume", "oi"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        return df
    except Exception:
        return pd.DataFrame()


def fetch_ltp_quote(instrument_keys: list) -> dict:
    if not instrument_keys:
        return {}
    try:
        raw_data = requests.get(
            "https://api.upstox.com/v2/market-quote/quotes",
            headers=upstox_headers(),
            params={"instrument_key": ",".join(instrument_keys)},
            timeout=10,
        ).json().get("data", {})
        normalized = {}
        for k, v in raw_data.items():
            normalized[k] = v
            normalized[k.replace(":", "|")] = v
            if v.get("instrument_token"):
                normalized[v["instrument_token"]] = v
        return normalized
    except Exception:
        return {}


def fetch_required_margin(instrument_key: str, lots_or_qty: int, side: str, product: str = "I") -> float:
    try:
        payload = {"instruments": [{"instrument_key": instrument_key, "quantity": int(lots_or_qty), "transaction_type": side, "product": product}]}
        data = requests.post("https://api.upstox.com/v2/charges/margin", headers=upstox_headers(), json=payload, timeout=10).json()
        return float(data.get("data", {}).get("required_margin", 0.0)) if data.get("status") == "success" else 0.0
    except Exception:
        return 0.0


def fetch_option_chain(spot_key: str, expiry_date: str) -> list:
    if not spot_key or not expiry_date:
        return []
    try:
        return requests.get(
            "https://api.upstox.com/v2/option/chain",
            headers=upstox_headers(),
            params={"instrument_key": spot_key, "expiry_date": expiry_date},
            timeout=10,
        ).json().get("data", [])
    except Exception:
        return []


# ==============================================================================
# 4. REGIME INTELLIGENCE (ADX + CHOPPINESS), GANN LEVELS, PATTERNS & FUEL SCORE
# ==============================================================================
def compute_market_regime(df: pd.DataFrame, min_adx: float, max_chop: float) -> dict:
    if df.empty or len(df) < 12:
        return {"regime": "MODERATE_TREND", "adx": 20.0, "chop": 50.0, "is_sideways": False, "is_strong_trend": False}

    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1).fillna(df["open"])
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr_sum = tr.rolling(14, min_periods=1).sum().replace(0, np.nan)
    plus_di = 100.0 * pd.Series(plus_dm).rolling(14, min_periods=1).sum() / tr_sum
    minus_di = 100.0 * pd.Series(minus_dm).rolling(14, min_periods=1).sum() / tr_sum
    dx = (100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0.0)
    adx = float(dx.rolling(14, min_periods=1).mean().iloc[-1])

    tr_14_sum = float(tr.tail(14).sum())
    hh_14 = float(high.tail(14).max())
    ll_14 = float(low.tail(14).min())
    hl_span = max(hh_14 - ll_14, 1e-6)
    chop = float(100.0 * math.log10(max(tr_14_sum / hl_span, 1.0)) / math.log10(14.0))

    curr_tr = float(tr.iloc[-1])
    avg_atr = max(float(tr.rolling(14, min_periods=1).mean().iloc[-1]), 1e-6)
    breakout_wake = (curr_tr / avg_atr) >= 1.35

    is_sideways = (adx < min_adx or chop > max_chop) and not breakout_wake
    is_strong_trend = (adx >= 25.0 and chop < 52.0)
    regime = "SIDEWAYS" if is_sideways else ("STRONG_TREND" if is_strong_trend else "MODERATE_TREND")
    return {
        "regime": regime,
        "adx": round(adx, 1),
        "chop": round(chop, 1),
        "is_sideways": is_sideways,
        "is_strong_trend": is_strong_trend,
    }


def compute_gann_levels(price: float, step: float) -> dict:
    root = math.sqrt(price)
    base_floor = math.floor(root * 2) / 2.0
    levels = [round((base_floor + k * 0.5) ** 2, 2) for k in range(-3, 5)]
    supports = [lvl for lvl in levels if lvl <= price]
    resistances = [lvl for lvl in levels if lvl > price]
    fallback_step = step if step and step > 0 else max(round(price * 0.002, 2), 1.0)
    return {
        "support": supports[-1] if supports else round(price - fallback_step, 2),
        "resistance": resistances[0] if resistances else round(price + fallback_step, 2),
        "all": levels,
    }


def detect_6_gann_patterns(df: pd.DataFrame, gann_levels: list, atr14: float) -> dict:
    if len(df) < 3:
        return {"near_gann": None, "bull_pattern": None, "bear_pattern": None}

    c1, c2, c3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    o1, h1, l1, cl1 = float(c1["open"]), float(c1["high"]), float(c1["low"]), float(c1["close"])
    o2, h2, l2, cl2 = float(c2["open"]), float(c2["high"]), float(c2["low"]), float(c2["close"])
    o3, h3, l3, cl3 = float(c3["open"]), float(c3["high"]), float(c3["low"]), float(c3["close"])

    zone_band = atr14 * GANN_ZONE_ATR_MULT
    near_gann = None
    min_dist = float("inf")
    for lvl in gann_levels:
        dist = min(abs(cl3 - lvl), abs(l3 - lvl), abs(h3 - lvl), abs(l2 - lvl), abs(h2 - lvl))
        if dist <= zone_band and dist < min_dist:
            min_dist = dist
            near_gann = lvl

    if near_gann is None:
        return {"near_gann": None, "bull_pattern": None, "bear_pattern": None}

    r1, b1 = max(h1 - l1, 1e-6), abs(cl1 - o1)
    r2, b2 = max(h2 - l2, 1e-6), abs(cl2 - o2)
    r3, b3 = max(h3 - l3, 1e-6), abs(cl3 - o3)

    upper_wick3 = h3 - max(o3, cl3)
    lower_wick3 = min(o3, cl3) - l3
    mid1 = (o1 + cl1) / 2.0

    bull_pattern = None
    if (cl3 > o3) and (cl2 < o2) and (cl3 >= o2) and (o3 <= cl2):
        bull_pattern = "BULLISH_ENGULFING"
    elif (cl1 < o1) and (b1 / r1 >= 0.45) and (b2 / r2 <= 0.40) and (cl3 > o3) and (cl3 > mid1):
        bull_pattern = "MORNING_STAR"
    elif (lower_wick3 >= 1.8 * max(b3, r3 * 0.1)) and (upper_wick3 <= 0.25 * r3) and ((cl3 - l3) / r3 >= 0.60):
        bull_pattern = "HAMMER"

    bear_pattern = None
    if (cl3 < o3) and (cl2 > o2) and (cl3 <= o2) and (o3 >= cl2):
        bear_pattern = "BEARISH_ENGULFING"
    elif (cl1 > o1) and (b1 / r1 >= 0.45) and (b2 / r2 <= 0.40) and (cl3 < o3) and (cl3 < mid1):
        bear_pattern = "EVENING_STAR"
    elif (upper_wick3 >= 1.8 * max(b3, r3 * 0.1)) and (lower_wick3 <= 0.25 * r3) and ((h3 - cl3) / r3 >= 0.60):
        bear_pattern = "REVERSE_HAMMER"

    return {"near_gann": near_gann, "bull_pattern": bull_pattern, "bear_pattern": bear_pattern}


def compute_fuel_with_fut_volume(spot_df: pd.DataFrame, fut_df: pd.DataFrame) -> pd.DataFrame:
    if spot_df.empty or len(spot_df) < 3:
        return spot_df

    prev_close = spot_df["close"].shift(1).fillna(spot_df["open"])
    tr = pd.concat([spot_df["high"] - spot_df["low"], (spot_df["high"] - prev_close).abs(), (spot_df["low"] - prev_close).abs()], axis=1).max(axis=1)
    spot_df["tr"] = tr
    spot_df["atr14"] = tr.rolling(14, min_periods=1).mean()
    spot_df["prev20_atr"] = tr.rolling(20, min_periods=1).mean().shift(1).fillna(tr)

    if not fut_df.empty and "volume" in fut_df.columns and fut_df["volume"].sum() > 0:
        fut_vol = fut_df[["timestamp", "volume"]].rename(columns={"volume": "fut_vol"})
        spot_df = pd.merge_asof(spot_df.sort_values("timestamp"), fut_vol.sort_values("timestamp"), on="timestamp", direction="nearest")
        spot_df["active_vol"] = spot_df["fut_vol"].fillna(0.0)
    else:
        spot_df["active_vol"] = spot_df["volume"]

    fuel_scores = []
    for i in range(len(spot_df)):
        row = spot_df.iloc[i]
        prev20_atr = max(row["prev20_atr"], 1e-6)
        curr_tr = row["tr"]

        lookback = min(i, 6)
        if lookback >= 1 and spot_df["active_vol"].iloc[i - lookback : i].median() > 0:
            med_vol = spot_df["active_vol"].iloc[i - lookback : i].median()
            vol_ratio = row["active_vol"] / med_vol
            vol_pts = min(40.0, max(0.0, (vol_ratio - 0.75) * 32.0))
        else:
            vol_pts = min(35.0, max(0.0, ((curr_tr / prev20_atr) - 0.7) * 35.0))

        atr_ratio = curr_tr / prev20_atr
        atr_pts = 30.0 if atr_ratio >= 1.2 else min(29.0, max(0.0, (atr_ratio / 1.2) * 28.0))

        c_range = max(row["high"] - row["low"], 1e-6)
        body = abs(row["close"] - row["open"])
        upper_wick = row["high"] - max(row["open"], row["close"])
        lower_wick = min(row["open"], row["close"]) - row["low"]

        pa_strength = max(body / c_range, max(upper_wick, lower_wick) / c_range)
        eff_pts = min(20.0, max(0.0, pa_strength * 20.0))

        fuel_scores.append(round(min(100.0, max(0.0, 10.0 + vol_pts + atr_pts + eff_pts)), 1))

    spot_df["fuel"] = fuel_scores
    return spot_df


# ==============================================================================
# 5. ORDER EXECUTION
# ==============================================================================
def place_marketable_limit_order(instrument_key: str, qty: int, side: str, ltp: float, tick_size: float = 0.05) -> dict:
    is_mcx = instrument_key.startswith("MCX_FO")
    buffer = (1.003 if is_mcx else 1.015) if side == "BUY" else (0.997 if is_mcx else 0.985)
    limit_price = round_to_tick(ltp * buffer, tick_size)

    if PAPER_MODE:
        log.info(f"[PAPER ORDER] {side} {qty} x {instrument_key} @ ₹{ltp:.2f} (Limit ₹{limit_price})")
        return {"status": "success", "fill_price": ltp, "order_id": f"PAPER_{int(time.time())}"}

    try:
        payload = {
            "quantity": qty, "product": "I", "validity": "DAY", "price": limit_price,
            "tag": "V3_GANN_ENGINE", "instrument_token": instrument_key,
            "order_type": "LIMIT", "transaction_type": side,
            "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False,
        }
        res = requests.post("https://api.upstox.com/v2/order/place", headers=upstox_headers(), json=payload, timeout=10).json()
        if res.get("status") == "success":
            return {"status": "success", "fill_price": ltp, "order_id": res["data"]["order_id"]}
        return {"status": "error", "message": str(res)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ==============================================================================
# 6. UNCAPPED MOMENTUM V3 ENGINE WITH ZERO-GAP HANDOVER & GAP CANDLE FORENSICS
# ==============================================================================
class IntradayV3Engine:
    def __init__(self):
        self.expiries = {}
        self.fut_contracts = {}
        self.pending_setups = {idx: None for idx in INDICES}
        self.open_pcr = {idx: None for idx in INDICES}
        self.pcr_history = {idx: deque(maxlen=5) for idx in INDICES}
        self.oi_history = {idx: None for idx in INDICES}
        self.active_positions = {}
        self.pos_lock = threading.Lock()
        self.closed_trades = []
        self.trade_counter = 0
        self.day_pnl = 0.0
        self.consecutive_losses = 0
        self.last_summaries = {}
        self.latest_dfs = {}
        self.dashboard_msg_id = None
        self.last_dashboard_ts = 0.0
        self.streamer = None
        self.ws_connected = False
        self.subscribed_keys = set()
        self.bot_status_text = "🟢 BOT LIVE • RUNNING"
        self.stop_requested = False

        self.tuned_params = {
            "min_fuel_score": MIN_FUEL_SCORE,
            "mcx_evening_fuel_score": MCX_EVENING_FUEL_SCORE,
            "mcx_max_risk_inr": MCX_MAX_RISK_INR,
            "min_adx_trend": MIN_ADX_TREND,
            "max_chop_index": MAX_CHOP_INDEX,
            "symbol_overrides": {},
        }
        self.market_memory = []

    def load_self_tuned_intelligence(self):
        if os.path.isfile(PARAMS_FILE):
            try:
                with open(PARAMS_FILE, "r", encoding="utf-8") as f:
                    self.tuned_params.update(json.load(f))
            except Exception:
                pass
        if os.path.isfile(MEMORY_FILE):
            try:
                with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                    self.market_memory = json.load(f)
            except Exception:
                pass

    def consult_market_memory(self, idx: str, regime_info: dict, fuel: float) -> tuple:
        sym_ov = self.tuned_params.get("symbol_overrides", {}).get(idx, {})
        if sym_ov.get("cooldown") and fuel < 65.0:
            return False, "COOLDOWN_LOW_FUEL"

        similar = [
            m for m in self.market_memory
            if m.get("symbol") == idx
            and m.get("regime") == regime_info["regime"]
            and abs(float(m.get("adx", 20.0)) - regime_info["adx"]) <= 5.0
        ]
        if len(similar) >= 3:
            traps = sum(1 for m in similar if m.get("outcome") == "SIDEWAYS_TRAP")
            moonshots = sum(1 for m in similar if m.get("outcome") == "EXTENDED_TREND_MISSED")
            if (traps / len(similar)) >= 0.65 and fuel < 62.0:
                return False, "MEMORY_VETO_TRAP"
            if moonshots >= 1:
                return True, "MEMORY_MOONSHOT_ZONE"

        return True, ("STRONG_TREND_RUNNER" if regime_info["is_strong_trend"] else "MOMENTUM_RUNNER")

    def compute_true_fractal_swing_sl(self, df: pd.DataFrame, side: str, spot_price: float, atr14: float) -> tuple:
        lookback = df.iloc[-10:] if len(df) >= 10 else df
        buffer = PIVOT_BUFFER_ATR_MULT * atr14
        min_spot_risk = 0.35 * atr14
        max_spot_risk = 1.25 * atr14

        if side == "CE":
            lows = lookback["low"].values
            pivots = [lows[i] for i in range(1, len(lows) - 1) if lows[i] <= lows[i - 1] and lows[i] <= lows[i + 1] and lows[i] < spot_price]
            true_swing_low = float(pivots[-1]) if pivots else float(lookback["low"].min())
            spot_risk = min(max_spot_risk, max(min_spot_risk, spot_price - (true_swing_low - buffer)))
            spot_sl = round(spot_price - spot_risk, 2)
        else:
            highs = lookback["high"].values
            pivots = [highs[i] for i in range(1, len(highs) - 1) if highs[i] >= highs[i - 1] and highs[i] >= highs[i + 1] and highs[i] > spot_price]
            true_swing_high = float(pivots[-1]) if pivots else float(lookback["high"].max())
            spot_risk = min(max_spot_risk, max(min_spot_risk, (true_swing_high + buffer) - spot_price))
            spot_sl = round(spot_price + spot_risk, 2)

        return round(spot_risk, 2), spot_sl

    def save_active_positions_state(self):
        os.makedirs(os.path.dirname(ACTIVE_STATE_FILE), exist_ok=True)
        serializable = {}
        with self.pos_lock:
            for idx, pos in self.active_positions.items():
                pos_copy = dict(pos)
                pos_copy["ltp_window"] = list(pos.get("ltp_window", []))
                serializable[idx] = pos_copy
        try:
            with open(ACTIVE_STATE_FILE, mode="w", encoding="utf-8") as f:
                json.dump(serializable, f, indent=2)
        except Exception as e:
            log.warning(f"Active positions save error: {e}")

    def load_active_positions_state(self):
        """
        Restores open positions from previous session AND performs Gap Candle Forensics:
        - Checks 5M candles of the traded instrument since entry_time to detect if price
          spiked during the handover gap and then reversed.
        - Retroactively updates peak_gain & Trail SL (1.5R / 2.5R / 4R / 8R+), and if
          live LTP has already breached the updated Trail SL / SL, closes immediately.
        """
        if not os.path.isfile(ACTIVE_STATE_FILE):
            return
        try:
            with open(ACTIVE_STATE_FILE, mode="r", encoding="utf-8") as f:
                data = json.load(f)
            if not data:
                return

            now_ist = datetime.now(IST)
            inst_keys = [pos["instrument_key"] for pos in data.values() if pos.get("instrument_key")]
            live_quotes = fetch_ltp_quote(inst_keys)
            immediate_exits = []

            with self.pos_lock:
                for idx, pos in data.items():
                    pos["ltp_window"] = deque(pos.get("ltp_window", []))
                    inst_key = pos["instrument_key"]
                    is_short = pos["tx_side"] == "SELL"
                    entry = float(pos["entry_price"])
                    tick_sz = float(pos.get("tick_size", 0.05))
                    r_unit = max(float(pos.get("r_unit", 1.0)), 1e-6)
                    entry_time_str = pos.get("entry_time", "09:20:00")

                    # 1. Inspect contract 5M candles since entry_time for handover gap spikes
                    inst_df = fetch_intraday_5m(inst_key)
                    if not inst_df.empty:
                        inst_df["hhmmss"] = inst_df["timestamp"].dt.strftime("%H:%M:%S")
                        since_entry = inst_df[inst_df["hhmmss"] >= entry_time_str]
                        if not since_entry.empty:
                            if is_short:
                                best_favorable = float(since_entry["low"].min())
                                gap_peak_gain = max(0.0, entry - best_favorable)
                            else:
                                best_favorable = float(since_entry["high"].max())
                                gap_peak_gain = max(0.0, best_favorable - entry)

                            if gap_peak_gain > pos.get("peak_gain", 0.0):
                                pos["peak_gain"] = gap_peak_gain
                                pos["last_peak_ts"] = time.time()
                                log.info(
                                    f"[GAP FORENSICS] {idx} reached peak gain ₹{gap_peak_gain:.2f} "
                                    f"({gap_peak_gain / r_unit:.1f}R) during/before handover!"
                                )

                    # 2. Check live LTP & retroactively tighten Trail SL based on true peak_gain
                    q_data = live_quotes.get(inst_key, {})
                    curr_ltp = float(q_data.get("last_price", pos.get("last_ltp", entry)))
                    pos["last_ltp"] = curr_ltp

                    curr_gain = (entry - curr_ltp) if is_short else (curr_ltp - entry)
                    if curr_gain > pos.get("peak_gain", 0.0):
                        pos["peak_gain"] = curr_gain

                    peak_gain = pos.get("peak_gain", 0.0)
                    peak_r = peak_gain / r_unit

                    if peak_r >= STAGE4_MOONSHOT_R:
                        lock_gain = peak_gain * 0.80
                        cand_sl = round_to_tick((entry - lock_gain) if is_short else (entry + lock_gain), tick_sz)
                        if (is_short and cand_sl < pos["sl"]) or (not is_short and cand_sl > pos["sl"]):
                            pos["sl"] = cand_sl
                            pos["trail_stage"] = f"GAP_MOONSHOT_80% ({peak_r:.1f}R)"
                    elif peak_r >= STAGE3_TREND_R:
                        lock_gain = peak_gain * 0.70
                        cand_sl = round_to_tick((entry - lock_gain) if is_short else (entry + lock_gain), tick_sz)
                        if (is_short and cand_sl < pos["sl"]) or (not is_short and cand_sl > pos["sl"]):
                            pos["sl"] = cand_sl
                            pos["trail_stage"] = f"GAP_TREND_70% ({peak_r:.1f}R)"
                    elif peak_r >= STAGE2_LOCK_R:
                        cand_sl = round_to_tick((entry - 1.5 * r_unit) if is_short else (entry + 1.5 * r_unit), tick_sz)
                        if (is_short and cand_sl < pos["sl"]) or (not is_short and cand_sl > pos["sl"]):
                            pos["sl"] = cand_sl
                            pos["trail_stage"] = f"GAP_LOCK_1.5R ({peak_r:.1f}R)"
                    elif peak_r >= STAGE1_BE_R:
                        be_off = max(round_to_tick(entry * 0.0001, tick_sz), tick_sz * 2)
                        cand_sl = round_to_tick((entry - be_off) if is_short else (entry + be_off), tick_sz)
                        if (is_short and cand_sl < pos["sl"]) or (not is_short and cand_sl > pos["sl"]):
                            pos["sl"] = cand_sl
                            pos["trail_stage"] = "GAP_BE_1.5R"

                    self.active_positions[idx] = pos
                    self.trade_counter = max(self.trade_counter, int(pos.get("trade_no", 0)))

                    # 3. If live LTP already breached updated Trail SL / SL during handover, queue immediate exit
                    if (not is_short and curr_ltp <= pos["sl"]) or (is_short and curr_ltp >= pos["sl"]):
                        reason = "GAP_TRAIL_SL_HIT" if pos.get("trail_stage") else "GAP_STOP_LOSS_HIT"
                        immediate_exits.append((idx, curr_ltp, reason))

            for idx, pos in list(self.active_positions.items()):
                self.subscribe_tick_instrument(pos["instrument_key"])
                log.info(
                    f"[HANDOVER RESUME] #{pos['trade_no']} {idx} {pos['strike']} | "
                    f"LTP ₹{pos['last_ltp']:.2f} | Effective SL ₹{pos['sl']:.2f} ({pos.get('trail_stage') or 'INITIAL'})"
                )

            for idx, exit_ltp, reason in immediate_exits:
                log.warning(f"[GAP EXIT] {idx} breached SL during handover ({reason}) @ ₹{exit_ltp:.2f} -> Closing now!")
                self.close_position_now(idx, exit_ltp, reason, now_ist)

            self.save_active_positions_state()
        except Exception as e:
            log.warning(f"Active positions restore/forensics error: {e}")

    def load_today_state_from_journal(self):
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        if not os.path.isfile(JOURNAL_FILE):
            return
        try:
            with open(JOURNAL_FILE, mode="r", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if r.get("date") == today_str:
                        pnl_val = float(r.get("pnl", 0.0))
                        t_no = int(float(r.get("trade_no", len(self.closed_trades) + 1)))
                        self.closed_trades.append({
                            "trade_no": t_no, "date": r["date"], "entry_time": r.get("entry_time", ""),
                            "exit_time": r.get("exit_time", ""), "symbol": r.get("symbol", ""),
                            "contract": r.get("contract", ""), "side": r.get("side", ""),
                            "qty": int(float(r.get("qty", 0))), "entry_price": float(r.get("entry_price", 0.0)),
                            "exit_price": float(r.get("exit_price", 0.0)), "points": float(r.get("points", 0.0)),
                            "sl": float(r.get("sl", 0.0)), "target": float(r.get("target", 0.0)),
                            "exit_reason": r.get("exit_reason", ""), "gross_pnl": float(r.get("gross_pnl", 0.0)),
                            "brokerage": float(r.get("brokerage", 0.0)), "taxes_other": float(r.get("taxes_other", 0.0)),
                            "charges": float(r.get("charges", 0.0)), "pnl": pnl_val,
                            "day_pnl": float(r.get("day_pnl", 0.0)), "setup": r.get("setup", ""),
                        })
                        self.day_pnl = round(self.day_pnl + pnl_val, 2)
                        self.trade_counter = max(self.trade_counter, t_no)
            self.closed_trades.sort(key=lambda x: x["trade_no"])
        except Exception:
            pass

    def init_websocket_streamer(self):
        if not HAS_UPSTOX_SDK or self.streamer is not None:
            return
        try:
            configuration = upstox_client.Configuration()
            configuration.access_token = UPSTOX_ACCESS_TOKEN
            api_client = upstox_client.ApiClient(configuration)
            self.streamer = upstox_client.MarketDataStreamerV3(api_client)
            self.streamer.auto_reconnect(True, 5, 20)

            def on_open():
                self.ws_connected = True
                with self.pos_lock:
                    active_keys = [p["instrument_key"] for p in self.active_positions.values()]
                if active_keys:
                    self.streamer.subscribe(active_keys, "ltpc")
                    self.subscribed_keys.update(active_keys)

            def on_message(message):
                try:
                    data = json.loads(message) if isinstance(message, str) else message
                    for inst_key, feed_obj in data.get("feeds", {}).items():
                        ltpc = (
                            feed_obj.get("ltpc")
                            or feed_obj.get("fullFeed", {}).get("marketFF", {}).get("ltpc")
                            or feed_obj.get("firstLevelWithGreeks", {}).get("ltpc")
                        )
                        if ltpc and "ltp" in ltpc and float(ltpc["ltp"]) > 0:
                            self.on_tick_price(inst_key, float(ltpc["ltp"]))
                except Exception:
                    pass

            def on_close():
                self.ws_connected = False

            self.streamer.on("open", on_open)
            self.streamer.on("message", on_message)
            self.streamer.on("close", on_close)
            threading.Thread(target=self.streamer.connect, daemon=True).start()
        except Exception:
            pass

    def subscribe_tick_instrument(self, instrument_key: str):
        if not HAS_UPSTOX_SDK:
            return
        self.subscribed_keys.add(instrument_key)
        if not self.streamer:
            self.init_websocket_streamer()
        elif self.ws_connected:
            try:
                self.streamer.subscribe([instrument_key], "ltpc")
            except Exception:
                pass

    def unsubscribe_tick_instrument(self, instrument_key: str):
        self.subscribed_keys.discard(instrument_key)
        if self.streamer and self.ws_connected:
            try:
                self.streamer.unsubscribe([instrument_key])
            except Exception:
                pass
        with self.pos_lock:
            no_active = len(self.active_positions) == 0
        if no_active and self.streamer:
            try:
                self.streamer.disconnect()
            except Exception:
                pass
            self.streamer = None
            self.ws_connected = False

    def on_tick_price(self, instrument_key: str, ltp: float):
        norm_key = instrument_key.replace(":", "|")
        target_idx, exit_reason = None, None

        with self.pos_lock:
            for idx, pos in self.active_positions.items():
                if pos["instrument_key"].replace(":", "|") == norm_key:
                    pos["last_ltp"] = ltp
                    is_short = pos["tx_side"] == "SELL"
                    curr_gain = (pos["entry_price"] - ltp) if is_short else (ltp - pos["entry_price"])
                    if curr_gain > pos.get("peak_gain", 0.0):
                        pos["peak_gain"] = curr_gain
                        pos["last_peak_ts"] = time.time()

                    r_unit = max(pos.get("r_unit", 1.0), 1e-6)
                    peak_r = pos.get("peak_gain", 0.0) / r_unit
                    tick_sz = pos.get("tick_size", 0.05)
                    if peak_r >= STAGE4_MOONSHOT_R:
                        lock_gain = pos["peak_gain"] * 0.80
                        moon_sl = round_to_tick((pos["entry_price"] - lock_gain) if is_short else (pos["entry_price"] + lock_gain), tick_sz)
                        if (is_short and moon_sl < pos["sl"]) or (not is_short and moon_sl > pos["sl"]):
                            pos["sl"] = moon_sl
                            pos["trail_stage"] = f"MOONSHOT 80% LOCK ({peak_r:.1f}R)"

                    if not is_short and ltp <= pos["sl"]:
                        exit_reason = "TRAIL_SL_HIT" if pos.get("trail_stage") else "STOP_LOSS_HIT"
                        target_idx = idx
                    elif is_short and ltp >= pos["sl"]:
                        exit_reason = "TRAIL_SL_HIT" if pos.get("trail_stage") else "STOP_LOSS_HIT"
                        target_idx = idx
                    break

        if target_idx and exit_reason:
            self.close_position_now(target_idx, ltp, exit_reason, datetime.now(IST))

    def init_contracts(self):
        for idx, cfg in INDICES.items():
            fut_info = get_active_future_contract(idx)
            self.fut_contracts[idx] = fut_info
            if cfg["type"] == "MCX_FUT":
                cfg["spot_key"] = fut_info.get("instrument_key", "")
                cfg["lot_size"] = fut_info.get("lot_size", 1)
                cfg["freeze_qty"] = fut_info.get("freeze_quantity", 10000)
                cfg["tick_size"] = fut_info.get("tick_size", 0.05)
                cfg["step"] = 0
            else:
                opt_specs = get_dynamic_option_specs(cfg["spot_key"])
                self.expiries[idx] = opt_specs["expiries"]
                cfg["lot_size"] = opt_specs["lot_size"]
                cfg["freeze_qty"] = opt_specs["freeze_quantity"]
                cfg["tick_size"] = opt_specs["tick_size"]
                cfg["step"] = opt_specs["step"]

    def get_smart_expiry(self, idx: str, now_ist: datetime) -> tuple:
        valid_exps = self.expiries.get(idx, [])
        if not valid_exps:
            return "", False
        today_str = now_ist.strftime("%Y-%m-%d")
        hhmm = now_ist.strftime("%H:%M")
        nearest = valid_exps[0]
        is_today_exp = (nearest == today_str)
        if is_today_exp and hhmm >= EXPIRY_NEXT_SWITCH_TIME and len(valid_exps) > 1:
            return valid_exps[1], False
        return nearest, is_today_exp

    def is_within_trade_window(self, idx: str, now_ist: datetime) -> bool:
        hhmm = now_ist.strftime("%H:%M")
        return (MCX_START_TIME <= hhmm <= MCX_END_TIME) if INDICES[idx]["type"] == "MCX_FUT" else (INDEX_START_TIME <= hhmm <= INDEX_END_TIME)

    def analyze_oi_and_pcr(self, idx: str, chain: list):
        if not chain:
            return 1.0, 1.0, 0.0, False, False
        total_pe_oi = sum(float(r.get("put_options", {}).get("market_data", {}).get("oi", 0)) for r in chain)
        total_ce_oi = sum(float(r.get("call_options", {}).get("market_data", {}).get("oi", 0)) for r in chain)
        pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 1.0

        if self.open_pcr[idx] is None:
            self.open_pcr[idx] = pcr
        open_p = self.open_pcr[idx]
        cum_pcr_diff = round(pcr - open_p, 2)

        hist = self.pcr_history[idx]
        if not hist or hist[-1] != pcr:
            hist.append(pcr)

        short_rising = (len(hist) >= 2 and hist[-1] >= hist[-2]) or (pcr >= 1.10)
        short_falling = (len(hist) >= 2 and hist[-1] <= hist[-2]) or (pcr <= 0.90)
        cum_bullish = (cum_pcr_diff > 0) or (pcr >= 1.15)
        cum_bearish = (cum_pcr_diff < 0) or (pcr <= 0.85)

        prev_oi = self.oi_history[idx]
        oi_bull_ok, oi_bear_ok = True, True
        if prev_oi is not None:
            d_pe = total_pe_oi - prev_oi["pe_oi"]
            d_ce = total_ce_oi - prev_oi["ce_oi"]
            oi_bull_ok = (d_pe >= d_ce) or (d_ce < 0)
            oi_bear_ok = (d_ce >= d_pe) or (d_pe < 0)

        self.oi_history[idx] = {"pe_oi": total_pe_oi, "ce_oi": total_ce_oi}
        return pcr, open_p, cum_pcr_diff, ((cum_bullish or short_rising) and oi_bull_ok), ((cum_bearish or short_falling) and oi_bear_ok)

    def compute_live_mtm(self) -> float:
        with self.pos_lock:
            if not self.active_positions:
                return 0.0
            positions_snap = list(self.active_positions.values())

        if not self.ws_connected:
            quotes = fetch_ltp_quote([p["instrument_key"] for p in positions_snap])
            for pos in positions_snap:
                q_data = quotes.get(pos["instrument_key"], {})
                pos["last_ltp"] = float(q_data.get("last_price", pos.get("last_ltp", pos["entry_price"])))

        total_mtm = 0.0
        for pos in positions_snap:
            ltp = pos.get("last_ltp", pos["entry_price"])
            is_short = pos["tx_side"] == "SELL"
            total_mtm += (pos["entry_price"] - ltp) * pos["qty"] if is_short else (ltp - pos["entry_price"]) * pos["qty"]
        return round(total_mtm, 2)

    def render_live_dashboard(self, hhmm: str):
        live_mtm = self.compute_live_mtm()
        net_pnl = round(self.day_pnl + live_mtm, 2)
        mode_badge = "🧪 PAPER" if PAPER_MODE else "⚡ LIVE"
        feed_badge = "📡 TICK-WS" if self.ws_connected else "🔍 SCANNING"
        net_icon = "🟢" if net_pnl >= 0 else "🔴"

        idx_trades = [t for t in self.closed_trades if t["symbol"] in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")]
        mcx_trades = [t for t in self.closed_trades if t["symbol"] not in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")]
        total_wins = sum(1 for t in self.closed_trades if t["pnl"] > 0)

        lines = [
            f"╔══════════════════════════════╗",
            f"  {self.bot_status_text} │ <code>{hhmm} IST</code>",
            f"╚══════════════════════════════╝",
            f"💼 <b>Day Net:</b> {net_icon} <b>₹{net_pnl:+,.2f}</b> │ {mode_badge} │ <i>{feed_badge}</i>",
            f"   ├ Realized: <b>₹{self.day_pnl:+,.2f}</b> │ Live MTM: <b>₹{live_mtm:+,.2f}</b>",
            f"   └ Today: <b>{len(self.closed_trades)}</b> ({total_wins}W) │ Index: <b>{len(idx_trades)}</b> │ MCX: <b>{len(mcx_trades)}</b>",
        ]

        with self.pos_lock:
            active_snap = sorted(self.active_positions.items(), key=lambda item: item[1]["trade_no"])

        if active_snap:
            lines.append("\n🔥 <b>ACTIVE UNCAPPED RUNNERS</b>")
            lines.append("──────────────────────────────")
            for p_idx, pos in active_snap:
                ltp = pos.get("last_ltp", pos["entry_price"])
                is_short = pos["tx_side"] == "SELL"
                pnl = (pos["entry_price"] - ltp) * pos["qty"] if is_short else (ltp - pos["entry_price"]) * pos["qty"]
                curr_gain = (pos["entry_price"] - ltp) if is_short else (ltp - pos["entry_price"])
                r_unit = max(pos.get("r_unit", 1.0), 1e-6)
                curr_r = curr_gain / r_unit
                bar = make_r_bar(curr_r, 8)
                trail_tag = f" 🛡️<code>{pos['trail_stage']}</code>" if pos.get("trail_stage") else " 🚀<code>UNCAPPED</code>"
                p_icon = "🟢" if pnl >= 0 else "🔴"
                lines.append(
                    f"<b>#{pos['trade_no']} {p_idx}</b> <code>{pos['strike']} {pos['display_side']}</code>{trail_tag}\n"
                    f"  ├ 🕒 <code>{pos['entry_time'][:5]}</code> │ In <b>₹{pos['entry_price']:.2f}</b> → LTP <b>₹{ltp:.2f}</b>\n"
                    f"  ├ 🛡️ Trail SL <code>₹{pos['sl']:.2f}</code> │ 🎯 Target: <b>UNCAPPED ∞</b>\n"
                    f"  └ {bar} <b>{curr_r:+.1f}R</b> │ {p_icon} <b>₹{pnl:+,.2f}</b>"
                )

        lines.append("\n📒 <b>TODAY'S TRADE LEDGER (#1 → #N)</b>")
        lines.append("──────────────────────────────")
        if self.closed_trades:
            for t in sorted(self.closed_trades, key=lambda x: x["trade_no"]):
                icon = "✅" if t["pnl"] >= 0 else "❌"
                lines.append(
                    f"<b>#{t['trade_no']}</b> {icon} <b>{t['symbol']}</b> <code>{t['contract']} {t['side']}</code>\n"
                    f"  ├ 🕒 <code>{t['entry_time'][:5]}→{t['exit_time'][:5]}</code> │ ₹{t['entry_price']:.2f}→₹{t['exit_price']:.2f} (<b>{t['points']:+.2f}p</b>)\n"
                    f"  └ SL:<code>{t['sl']:.2f}</code> │ {t['exit_reason']} │ <b>₹{t['pnl']:+,.2f}</b>"
                )
        elif not active_snap:
            lines.append("  <i>Monitoring 5M candles, ADX regimes & Gann levels...</i>")

        if self.last_summaries:
            lines.append("\n📡 <b>5 INDICES RADAR</b>")
            lines.append("──────────────────────────────")
            for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"):
                s = self.last_summaries.get(idx)
                if s:
                    lines.append(f"• <b>{s['idx']:<10}</b> <code>{s['spot']}</code> │ Fuel <b>{s['fuel']}</b> │ {s['short_status']}")

            lines.append("\n🛢️ <b>5 MCX COMMODITIES RADAR</b>")
            lines.append("──────────────────────────────")
            for idx in ("CRUDEOIL", "NATURALGAS", "GOLD", "SILVER", "COPPER"):
                s = self.last_summaries.get(idx)
                if s:
                    lines.append(f"• <b>{s['idx']:<10}</b> <code>{s['spot']}</code> │ Fuel <b>{s['fuel']}</b> │ {s['short_status']}")

        self.dashboard_msg_id = upsert_telegram_dashboard("\n".join(lines), self.dashboard_msg_id)

    def close_position_now(self, idx: str, ltp: float, exit_reason: str, now_ist: datetime):
        with self.pos_lock:
            pos = self.active_positions.pop(idx, None)
        if not pos:
            return

        self.unsubscribe_tick_instrument(pos["instrument_key"])
        self.save_active_positions_state()

        is_short = pos["tx_side"] == "SELL"
        is_mcx = pos["type"] == "MCX_FUT"
        close_side = "BUY" if is_short else "SELL"
        place_marketable_limit_order(pos["instrument_key"], pos["qty"], close_side, ltp, pos.get("tick_size", 0.05))

        pts = round((pos["entry_price"] - ltp) if is_short else (ltp - pos["entry_price"]), 2)
        gross_pnl = round(pts * pos["qty"], 2)
        r_unit = max(pos.get("r_unit", 1.0), 1e-6)
        peak_r = round(pos.get("peak_gain", 0.0) / r_unit, 2)
        exit_r = round(pts / r_unit, 2)

        buy_px = ltp if is_short else pos["entry_price"]
        sell_px = pos["entry_price"] if is_short else ltp
        ch_info = fetch_exact_trade_charges(pos["instrument_key"], buy_px, sell_px, pos["qty"], is_mcx)
        net_pnl = round(gross_pnl - ch_info["total"], 2)

        self.day_pnl = round(self.day_pnl + net_pnl, 2)
        self.consecutive_losses = (self.consecutive_losses + 1) if net_pnl < 0 else 0

        exit_time_str = now_ist.strftime("%H:%M:%S")
        journal_entry = {
            "trade_no": pos["trade_no"], "date": now_ist.strftime("%Y-%m-%d"),
            "entry_time": pos.get("entry_time", ""), "exit_time": exit_time_str,
            "symbol": idx, "contract": str(pos["strike"]), "side": pos["display_side"],
            "qty": pos["qty"], "entry_price": pos["entry_price"], "exit_price": ltp,
            "points": pts, "sl": pos["sl"], "target": 0.0, "exit_reason": f"{exit_reason} ({exit_r:+.1f}R)",
            "gross_pnl": gross_pnl, "brokerage": ch_info["brokerage"],
            "taxes_other": ch_info["taxes_other"], "charges": ch_info["total"],
            "pnl": net_pnl, "day_pnl": self.day_pnl, "setup": pos["setup_name"],
            "peak_r": f"{peak_r:.1f}R",
        }
        self.closed_trades.append(journal_entry)
        record_trade_journal({k: v for k, v in journal_entry.items() if k != "peak_r"})

        icon = "✅" if net_pnl >= 0 else "❌"
        exit_caption = (
            f"🏁 {icon} <b>TRADE #{pos['trade_no']} CLOSED • {idx}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>Contract:</b> <code>{pos['strike']} {pos['display_side']}</code> │ <b>{exit_reason}</b>\n"
            f"🕒 <b>Time:</b> <code>{pos['entry_time'][:5]} → {exit_time_str[:5]}</code> │ Peak: <b>{peak_r:.1f}R</b>\n"
            f"💵 <b>Price:</b> ₹{pos['entry_price']:.2f} → ₹{ltp:.2f} (<b>{pts:+.2f} pts</b>)\n"
            f"📊 <b>Gross PnL:</b> ₹{gross_pnl:+,.2f} │ 🧾 <b>Charges:</b> -₹{ch_info['total']:.2f}\n"
            f"💰 <b>Net PnL:</b> <b>₹{net_pnl:+,.2f}</b>  │  <b>Day Net:</b> <b>₹{self.day_pnl:+,.2f}</b>"
        )

        df_plot = self.latest_dfs.get(idx, pd.DataFrame())
        spot_now = float(df_plot["close"].iloc[-1]) if not df_plot.empty else pos.get("spot_entry", 0.0)
        send_trade_alert_with_chart(
            df_plot,
            title=f"{idx} 5M EXIT │ #{pos['trade_no']} {pos['strike']} {pos['display_side']} ({exit_r:+.1f}R)",
            caption=exit_caption,
            gann_lvl=pos.get("gann_level", spot_now),
            spot_entry=pos.get("spot_entry", spot_now),
            spot_sl=pos.get("spot_sl", spot_now),
            is_exit=True,
            spot_exit=spot_now,
        )

        notify(build_master_journal_report(now_ist), silent=True)
        self.render_live_dashboard(now_ist.strftime("%H:%M"))

    def evaluate_active_candle_exits(self, idx: str, df: pd.DataFrame, spot_close: float, price_change: float, fuel: float, atr14: float, now_ist: datetime):
        with self.pos_lock:
            pos = self.active_positions.get(idx)
        if not pos or len(df) < 4:
            return

        is_short = pos["tx_side"] == "SELL"
        is_mcx = pos["type"] == "MCX_FUT"
        entry = pos["entry_price"]
        ltp = pos.get("last_ltp", entry)
        tick_sz = pos.get("tick_size", 0.05)
        eff_delta = pos.get("delta", 0.50)
        r_unit = max(pos.get("r_unit", 1.0), 1e-6)

        curr_gain = (entry - ltp) if is_short else (ltp - entry)
        peak_gain = max(pos.get("peak_gain", 0.0), curr_gain)
        if peak_gain > pos.get("peak_gain", 0.0):
            pos["peak_gain"] = peak_gain
            pos["last_peak_ts"] = time.time()

        peak_r = peak_gain / r_unit
        curr_r = curr_gain / r_unit

        recent_3 = df.iloc[-4:-1]
        swing_low = float(recent_3["low"].min())
        swing_high = float(recent_3["high"].max())

        new_sl = pos["sl"]
        trail_label = None

        if peak_r >= STAGE4_MOONSHOT_R:
            lock_gain = peak_gain * 0.80
            candidate_sl = round_to_tick((entry - lock_gain) if is_short else (entry + lock_gain), tick_sz)
            if (is_short and candidate_sl < new_sl and candidate_sl > ltp) or (not is_short and candidate_sl > new_sl and candidate_sl < ltp):
                new_sl = candidate_sl
                trail_label = f"MOONSHOT 80% ({peak_r:.1f}R)"

        elif peak_r >= STAGE3_TREND_R:
            lock_gain = peak_gain * 0.70
            candidate_sl = round_to_tick((entry - lock_gain) if is_short else (entry + lock_gain), tick_sz)
            if (is_short and candidate_sl < new_sl and candidate_sl > ltp) or (not is_short and candidate_sl > new_sl and candidate_sl < ltp):
                new_sl = candidate_sl
                trail_label = f"TREND 70% ({peak_r:.1f}R)"

        elif peak_r >= STAGE2_LOCK_R:
            floor_sl = round_to_tick((entry - 1.5 * r_unit) if is_short else (entry + 1.5 * r_unit), tick_sz)
            if is_mcx:
                swing_sl = round_to_tick((swing_high + PIVOT_BUFFER_ATR_MULT * atr14) if is_short else (swing_low - PIVOT_BUFFER_ATR_MULT * atr14), tick_sz)
                candidate_sl = min(floor_sl, swing_sl) if is_short else max(floor_sl, swing_sl)
            else:
                spot_entry = pos.get("spot_entry", spot_close)
                delta_spot = (swing_low - PIVOT_BUFFER_ATR_MULT * atr14 - spot_entry) if pos["side"] == "CE" else (spot_entry - (swing_high + PIVOT_BUFFER_ATR_MULT * atr14))
                candidate_sl = max(floor_sl, round_to_tick(entry + delta_spot * eff_delta, tick_sz))

            min_gap = max(atr14 * 0.15, tick_sz * 2) if is_mcx else max(tick_sz * 4, 0.5)
            if (is_short and candidate_sl < new_sl and candidate_sl > ltp + min_gap) or (not is_short and candidate_sl > new_sl and candidate_sl < ltp - min_gap):
                new_sl = candidate_sl
                trail_label = f"SWING + 1.5R LOCK ({peak_r:.1f}R)"

        elif peak_r >= STAGE1_BE_R:
            be_offset = max(round_to_tick(entry * 0.0001, tick_sz), tick_sz * 2)
            be_sl = round_to_tick((entry - be_offset) if is_short else (entry + be_offset), tick_sz)
            min_gap = max(atr14 * 0.15, tick_sz * 2) if is_mcx else max(tick_sz * 4, 0.5)
            if (is_short and be_sl < new_sl and be_sl > ltp + min_gap) or (not is_short and be_sl > new_sl and be_sl < ltp - min_gap):
                new_sl = be_sl
                trail_label = "COST-TO-COST (1.5R)"

        if trail_label and new_sl != pos["sl"]:
            with self.pos_lock:
                if idx in self.active_positions:
                    self.active_positions[idx]["sl"] = new_sl
                    self.active_positions[idx]["trail_stage"] = trail_label
            self.save_active_positions_state()
            notify(
                f"🛡️🚀 <b>MOMENTUM TRAIL LOCKED • #{pos['trade_no']} [{idx}]</b>\n"
                f"📌 <b>Stage:</b> {trail_label} │ <b>New Trail SL: ₹{new_sl:.2f}</b> (LTP ₹{ltp:.2f})",
                silent=False,
            )

        low_fuel_streak = (pos.get("low_fuel_streak", 0) + 1) if fuel < 20.0 else 0
        pos["low_fuel_streak"] = low_fuel_streak
        opposing_move = (price_change > 0) if (pos["side"] == "PE") else (price_change < 0)
        if curr_r >= FUEL_FINISH_MIN_R and ((fuel < FUEL_FINISH_THRESHOLD and opposing_move and low_fuel_streak >= 2)):
            self.close_position_now(idx, ltp, f"FUEL_EXHAUSTED (Fuel {int(fuel)})", now_ist)
            return

        now_ts = time.time()
        ltp_hist = pos.setdefault("ltp_window", deque())
        ltp_hist.append((now_ts, ltp))
        flat_limit_sec = FLAT_LTP_EXPIRY_SEC if pos.get("is_expiry_day", False) else FLAT_LTP_NORMAL_SEC
        while ltp_hist and (now_ts - ltp_hist[0][0]) > flat_limit_sec:
            ltp_hist.popleft()

        window_span = now_ts - ltp_hist[0][0] if ltp_hist else 0.0
        if window_span >= (flat_limit_sec - 20):
            prices_in_win = [p for _, p in ltp_hist]
            win_range = max(prices_in_win) - min(prices_in_win)
            flat_threshold = max(r_unit * 0.50, entry * (0.002 if is_mcx else 0.03))
            if win_range <= flat_threshold and curr_r < STAGE1_BE_R:
                self.close_position_now(idx, ltp, f"NO_MOMENTUM_EXIT ({int(round(window_span / 60.0))}m)", now_ist)

    def evaluate_instrument(self, idx: str, cfg: dict, now_ist: datetime):
        is_mcx = cfg["type"] == "MCX_FUT"
        if not cfg.get("spot_key"):
            return None

        spot_df = fetch_intraday_5m(cfg["spot_key"])
        if spot_df.empty or len(spot_df) < 3:
            return None

        fut_key = self.fut_contracts.get(idx, {}).get("instrument_key", "")
        fut_df = spot_df if is_mcx else fetch_intraday_5m(fut_key)
        df = compute_fuel_with_fut_volume(spot_df, fut_df)
        self.latest_dfs[idx] = df

        curr, prev = df.iloc[-1], df.iloc[-2]
        spot_close = float(curr["close"])
        price_change = round(spot_close - float(prev["close"]), 2)
        atr14 = max(float(curr["atr14"]), cfg.get("tick_size", 0.05) * 2)
        fuel = float(curr["fuel"])

        regime_info = compute_market_regime(
            df,
            min_adx=float(self.tuned_params.get("min_adx_trend", MIN_ADX_TREND)),
            max_chop=float(self.tuned_params.get("max_chop_index", MAX_CHOP_INDEX)),
        )

        gann = compute_gann_levels(spot_close, cfg.get("step", 0))
        sup, res, gann_levels = gann["support"], gann["resistance"], gann["all"]

        chain, active_expiry, is_today_exp = [], "", False
        if is_mcx:
            oi_change = float(curr["oi"]) - float(prev["oi"])
            oi_pcr_bull_ok, oi_pcr_bear_ok = (price_change > 0), (price_change < 0)
            pcr, open_p, cum_pcr_diff = 0.0, 0.0, round(oi_change, 0)
        else:
            active_expiry, is_today_exp = self.get_smart_expiry(idx, now_ist)
            chain = fetch_option_chain(cfg["spot_key"], active_expiry)
            pcr, open_p, cum_pcr_diff, oi_pcr_bull_ok, oi_pcr_bear_ok = self.analyze_oi_and_pcr(idx, chain)

        with self.pos_lock:
            in_trade = idx in self.active_positions

        if in_trade:
            self.pending_setups[idx] = None
            self.evaluate_active_candle_exits(idx, df, spot_close, price_change, fuel, atr14, now_ist)

        with self.pos_lock:
            pos = self.active_positions.get(idx)

        if pos:
            ltp_now = pos.get("last_ltp", spot_close if is_mcx else pos["entry_price"])
            is_short = pos["tx_side"] == "SELL"
            pos_mtm = (pos["entry_price"] - ltp_now) * pos["qty"] if is_short else (ltp_now - pos["entry_price"]) * pos["qty"]
            return {
                "idx": idx, "is_mcx": is_mcx,
                "spot": round(spot_close, 2) if spot_close < 1000 else int(round(spot_close)),
                "fuel": int(round(fuel)), "pcr": pcr, "cum_pcr_diff": cum_pcr_diff,
                "short_status": f"#{pos['trade_no']} RUNNER (₹{pos_mtm:+.0f})🔥",
            }

        if regime_info["is_sideways"]:
            self.pending_setups[idx] = None
            return {
                "idx": idx, "is_mcx": is_mcx,
                "spot": round(spot_close, 2) if spot_close < 1000 else int(round(spot_close)),
                "fuel": int(round(fuel)), "pcr": pcr, "cum_pcr_diff": cum_pcr_diff,
                "short_status": f"SLEEP (ADX {regime_info['adx']:.0f}) 😴",
            }

        pat_info = detect_6_gann_patterns(df, gann_levels, atr14)
        near_gann, bull_pattern, bear_pattern = pat_info["near_gann"], pat_info["bull_pattern"], pat_info["bear_pattern"]

        pending = self.pending_setups.get(idx)
        if pending:
            if (now_ist - pending["armed_at"]).total_seconds() > SETUP_EXPIRY_SEC:
                self.pending_setups[idx] = None
                pending = None

        new_signal = None
        if near_gann is not None and bull_pattern is not None and price_change > 0 and oi_pcr_bull_ok:
            side_label = "BUY" if is_mcx else "CE"
            new_signal = {
                "side": "CE", "display_side": side_label,
                "name": f"GANN_{int(near_gann)}_{bull_pattern}_{side_label}",
                "gann_level": near_gann, "trigger": round(spot_close, 2), "armed_at": now_ist,
            }
        elif near_gann is not None and bear_pattern is not None and price_change < 0 and oi_pcr_bear_ok:
            side_label = "SELL" if is_mcx else "PE"
            new_signal = {
                "side": "PE", "display_side": side_label,
                "name": f"GANN_{int(near_gann)}_{bear_pattern}_{side_label}",
                "gann_level": near_gann, "trigger": round(spot_close, 2), "armed_at": now_ist,
            }

        if new_signal and not pending:
            self.pending_setups[idx] = new_signal
            pending = new_signal

        if pending and idx not in self.active_positions and self.is_within_trade_window(idx, now_ist):
            hhmm_now = now_ist.strftime("%H:%M")
            sym_ov = self.tuned_params.get("symbol_overrides", {}).get(idx, {})
            base_req_fuel = float(sym_ov.get("min_fuel", self.tuned_params.get("min_fuel_score", MIN_FUEL_SCORE)))
            eve_req_fuel = float(self.tuned_params.get("mcx_evening_fuel_score", MCX_EVENING_FUEL_SCORE))
            req_fuel = max(base_req_fuel, eve_req_fuel) if (is_mcx and hhmm_now >= "15:30") else base_req_fuel

            dir_confirmed = (
                (pending["side"] == "CE" and price_change > 0 and oi_pcr_bull_ok and spot_close >= pending["trigger"])
                or (pending["side"] == "PE" and price_change < 0 and oi_pcr_bear_ok and spot_close <= pending["trigger"])
            )
            if dir_confirmed and fuel >= req_fuel:
                mem_ok, mem_tag = self.consult_market_memory(idx, regime_info, fuel)
                if not mem_ok:
                    self.pending_setups[idx] = None
                elif is_mcx:
                    self.try_execute_mcx_fut_entry(idx, cfg, pending, df, spot_close, fuel, atr14, now_ist, mem_tag)
                else:
                    self.try_execute_option_entry(idx, cfg, pending, df, spot_close, fuel, chain, atr14, now_ist, active_expiry, is_today_exp, mem_tag)

        short_status = f"TREND (ADX {regime_info['adx']:.0f}) ⚡" if regime_info["is_strong_trend"] else f"Gann {int(sup)}-{int(res)} 👀"
        if pending:
            short_status = f"ARMED {pending['display_side']} 🎯"

        return {
            "idx": idx, "is_mcx": is_mcx,
            "spot": round(spot_close, 2) if spot_close < 1000 else int(round(spot_close)),
            "fuel": int(round(fuel)), "pcr": pcr, "cum_pcr_diff": cum_pcr_diff,
            "short_status": short_status,
        }

    def try_execute_mcx_fut_entry(self, idx, cfg, setup, df, fut_price, fuel, atr14, now_ist, mem_tag):
        inst_key = cfg["spot_key"]
        lot_size = cfg["lot_size"]
        tick_sz = cfg.get("tick_size", 0.05)
        tx_side = "BUY" if setup["side"] == "CE" else "SELL"

        ltp = float(fetch_ltp_quote([inst_key]).get(inst_key, {}).get("last_price", fut_price))
        spot_risk, _ = self.compute_true_fractal_swing_sl(df, setup["side"], ltp, atr14)
        sl_points = max(round_to_tick(spot_risk, tick_sz), tick_sz * 4)

        max_risk_cap = float(self.tuned_params.get("mcx_max_risk_inr", MCX_MAX_RISK_INR))
        risk_1_lot = sl_points * lot_size
        if risk_1_lot > max_risk_cap:
            self.pending_setups[idx] = None
            return

        req_margin_1lot = fetch_required_margin(inst_key, 1, tx_side, "I")
        margin_lots = max(1, int(MCX_CAPITAL_PER_TRADE // req_margin_1lot)) if req_margin_1lot > 0 else 1
        risk_lots = max(1, int(max_risk_cap // max(risk_1_lot, 1.0)))
        lots = min(margin_lots, risk_lots)
        qty = min(lots * lot_size, cfg["freeze_qty"])

        sl_price = round_to_tick((ltp - sl_points) if tx_side == "BUY" else (ltp + sl_points), tick_sz)

        order_res = place_marketable_limit_order(inst_key, qty, tx_side, ltp, tick_sz)
        if order_res.get("status") == "success":
            fill = order_res["fill_price"]
            sym = self.fut_contracts.get(idx, {}).get("trading_symbol", f"{idx} FUT")
            now_ts = time.time()
            self.trade_counter += 1
            t_no = self.trade_counter
            entry_time_str = now_ist.strftime("%H:%M:%S")
            with self.pos_lock:
                self.active_positions[idx] = {
                    "trade_no": t_no, "type": "MCX_FUT", "instrument_key": inst_key, "strike": sym,
                    "side": setup["side"], "tx_side": tx_side, "display_side": tx_side,
                    "qty": qty, "tick_size": tick_sz, "delta": 1.0, "entry_price": fill,
                    "spot_entry": fut_price, "spot_sl": sl_price, "r_unit": sl_points,
                    "gann_level": setup["gann_level"], "last_ltp": fill, "sl": sl_price,
                    "peak_gain": 0.0, "entry_ts": now_ts, "last_peak_ts": now_ts,
                    "trail_stage": None, "low_fuel_streak": 0, "is_expiry_day": False,
                    "ltp_window": deque([(now_ts, fill)]), "setup_name": f"{setup['name']}|{mem_tag}",
                    "entry_time": entry_time_str,
                }
            self.pending_setups[idx] = None
            self.subscribe_tick_instrument(inst_key)
            self.save_active_positions_state()
            entry_msg = (
                f"🛢️🚀 <b>NEW RUNNER • TRADE #{t_no} [{idx}]</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Contract:</b> <code>{sym}</code> (<b>{tx_side}</b>) │ 🕒 <code>{entry_time_str[:5]} IST</code>\n"
                f"📦 <b>Qty:</b> {qty} ({lots} Lot) │ <b>Entry:</b> <b>₹{fill:.2f}</b>\n"
                f"🛡️ <b>Pivot SL (1R):</b> ₹{sl_price:.2f} (<code>-{sl_points:.2f}p</code>) │ 🎯 <b>Target:</b> <b>UNCAPPED ∞ (Trail)</b>\n"
                f"📐 <b>Setup:</b> {setup['name']} │ 🧠 <code>{mem_tag}</code> │ 🔥 <b>Fuel:</b> {fuel}"
            )
            send_trade_alert_with_chart(df, f"{idx} 5M ENTRY │ #{t_no} {sym} {tx_side}", entry_msg, setup["gann_level"], fut_price, sl_price)
            self.render_live_dashboard(now_ist.strftime("%H:%M"))

    def try_execute_option_entry(self, idx, cfg, setup, df, spot_close, fuel, chain, atr14, now_ist, active_expiry, is_today_exp, mem_tag):
        if not chain:
            return
        step = cfg.get("step", 50)
        tick_sz = cfg.get("tick_size", 0.05)
        atm_strike = round(spot_close / step) * step
        opt_field = "call_options" if setup["side"] == "CE" else "put_options"

        chosen = next((row.get(opt_field, {}) for row in chain if row.get("strike_price") == atm_strike), None)
        if not chosen:
            return

        inst_key = chosen.get("instrument_key")
        mkt = chosen.get("market_data", {})
        greeks = chosen.get("option_greeks", {})
        ltp = float(mkt.get("ltp", 0))
        if ltp < 3.0:
            return

        raw_delta = abs(float(greeks.get("delta", 0.0)))
        live_delta = min(0.75, max(0.25, raw_delta)) if raw_delta > 0 else 0.50

        lots = max(1, int(CAPITAL_PER_TRADE // (ltp * cfg["lot_size"])))
        qty = min(lots * cfg["lot_size"], cfg["freeze_qty"])

        spot_risk, spot_sl_equiv = self.compute_true_fractal_swing_sl(df, setup["side"], spot_close, atr14)
        raw_opt_sl = min(max(spot_risk * live_delta, ltp * 0.08), ltp * 0.35)
        sl_points = max(round_to_tick(raw_opt_sl, tick_sz), tick_sz * 3)
        sl_price = round_to_tick(ltp - sl_points, tick_sz)

        order_res = place_marketable_limit_order(inst_key, qty, "BUY", ltp, tick_sz)
        if order_res.get("status") == "success":
            fill = order_res["fill_price"]
            now_ts = time.time()
            self.trade_counter += 1
            t_no = self.trade_counter
            entry_time_str = now_ist.strftime("%H:%M:%S")
            exp_label = "0DTE" if is_today_exp else active_expiry
            with self.pos_lock:
                self.active_positions[idx] = {
                    "trade_no": t_no, "type": "INDEX_OPT", "instrument_key": inst_key,
                    "strike": atm_strike, "side": setup["side"], "tx_side": "BUY",
                    "display_side": setup["side"], "qty": qty, "tick_size": tick_sz,
                    "delta": live_delta, "entry_price": fill, "spot_entry": spot_close,
                    "spot_sl": spot_sl_equiv, "r_unit": sl_points,
                    "gann_level": setup["gann_level"], "last_ltp": fill, "sl": sl_price,
                    "peak_gain": 0.0, "entry_ts": now_ts, "last_peak_ts": now_ts,
                    "trail_stage": None, "low_fuel_streak": 0, "is_expiry_day": is_today_exp,
                    "ltp_window": deque([(now_ts, fill)]), "setup_name": f"{setup['name']}|{mem_tag}",
                    "entry_time": entry_time_str,
                }
            self.pending_setups[idx] = None
            self.subscribe_tick_instrument(inst_key)
            self.save_active_positions_state()
            entry_msg = (
                f"🚀 <b>NEW RUNNER • TRADE #{t_no} [{idx}]</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Strike:</b> <code>{atm_strike} {setup['side']}</code> ({exp_label}) │ 🕒 <code>{entry_time_str[:5]} IST</code>\n"
                f"📦 <b>Qty:</b> {qty} ({lots} Lot) │ <b>Entry:</b> <b>₹{fill:.2f}</b> (Δ {live_delta:.2f})\n"
                f"🛡️ <b>Pivot SL (1R):</b> ₹{sl_price:.2f} (<code>-{sl_points:.2f}p</code>) │ 🎯 <b>Target:</b> <b>UNCAPPED ∞ (Trail)</b>\n"
                f"📐 <b>Setup:</b> {setup['name']} │ 🧠 <code>{mem_tag}</code> │ 🔥 <b>Fuel:</b> {fuel}"
            )
            send_trade_alert_with_chart(df, f"{idx} 5M ENTRY │ #{t_no} {atm_strike} {setup['side']}", entry_msg, setup["gann_level"], spot_close, spot_sl_equiv)
            self.render_live_dashboard(now_ist.strftime("%H:%M"))

    def manage_open_positions(self, now_ist: datetime):
        with self.pos_lock:
            if not self.active_positions:
                return
            pos_items = list(self.active_positions.items())

        hhmm = now_ist.strftime("%H:%M")
        quotes = fetch_ltp_quote([p["instrument_key"] for _, p in pos_items]) if not self.ws_connected else {}

        for idx, pos in pos_items:
            if not self.ws_connected:
                ltp = float(quotes.get(pos["instrument_key"], {}).get("last_price", pos.get("last_ltp", pos["entry_price"])))
                pos["last_ltp"] = ltp
            else:
                ltp = pos.get("last_ltp", pos["entry_price"])

            is_short = pos["tx_side"] == "SELL"
            exit_reason = None
            if pos["type"] == "INDEX_OPT" and hhmm >= INDEX_EXIT_TIME:
                exit_reason = f"EOD_EXIT ({INDEX_EXIT_TIME})"
            elif pos["type"] == "MCX_FUT" and hhmm >= MCX_EXIT_TIME:
                exit_reason = f"MCX_EXIT ({MCX_EXIT_TIME})"
            elif not is_short and ltp <= pos["sl"]:
                exit_reason = "TRAIL_SL_HIT" if pos.get("trail_stage") else "STOP_LOSS_HIT"
            elif is_short and ltp >= pos["sl"]:
                exit_reason = "TRAIL_SL_HIT" if pos.get("trail_stage") else "STOP_LOSS_HIT"

            if exit_reason:
                self.close_position_now(idx, ltp, exit_reason, now_ist)

    def shutdown_cleanly(self, now_ist: datetime, reason_label: str):
        if reason_label == "MANUAL STOP":
            with self.pos_lock:
                open_items = list(self.active_positions.items())
            if open_items:
                quotes = fetch_ltp_quote([p["instrument_key"] for _, p in open_items])
                for idx, pos in open_items:
                    exit_ltp = float(quotes.get(pos["instrument_key"], {}).get("last_price", pos.get("last_ltp", pos["entry_price"])))
                    self.close_position_now(idx, exit_ltp, "MANUAL_STOP_EXIT", now_ist)

        self.save_active_positions_state()
        with self.pos_lock:
            open_cnt = len(self.active_positions)

        self.bot_status_text = (
            f"🟡 HANDOVER ({open_cnt} OPEN)"
            if (reason_label == "SESSION HANDOVER" and open_cnt > 0)
            else f"🔴 BOT STOPPED • {reason_label}"
        )
        if self.streamer:
            try:
                self.streamer.disconnect()
            except Exception:
                pass
            self.streamer = None
            self.ws_connected = False

        self.render_live_dashboard(now_ist.strftime("%H:%M"))
        notify(build_master_journal_report(now_ist), silent=True)
        notify(f"🔴 <b>BOT STOPPED ({reason_label})</b> │ Open Carried: <b>{open_cnt}</b> │ Closed Today: <b>{len(self.closed_trades)}</b> │ Day Net: <b>₹{self.day_pnl:+,.2f}</b>")

    def run(self):
        if not UPSTOX_ACCESS_TOKEN:
            log.error("UPSTOX_ACCESS_TOKEN missing! Exiting.")
            sys.exit(1)

        signal.signal(signal.SIGTERM, lambda s, f: setattr(self, "stop_requested", True))
        signal.signal(signal.SIGINT, lambda s, f: setattr(self, "stop_requested", True))

        self.load_self_tuned_intelligence()
        self.load_today_state_from_journal()
        self.init_contracts()
        self.load_active_positions_state()
        self.bot_status_text = "🟢 BOT LIVE • RUNNING"
        session_start_ts = time.time()

        with self.pos_lock:
            carried_cnt = len(self.active_positions)

        notify(
            f"🟢 <b>V3 UNCAPPED MOMENTUM ENGINE LIVE</b> ({'PAPER' if PAPER_MODE else 'LIVE'})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🧠 <b>Memory Vault:</b> {len(self.market_memory)} experiences │ <b>ADX Gate:</b> ≥{self.tuned_params['min_adx_trend']}\n"
            f"🚀 <b>Target Mode:</b> UNCAPPED (Zero-Gap Handover + Gap Forensics) │ Carried: {carried_cnt}"
        )

        while True:
            now_ist = datetime.now(IST)
            hhmm = now_ist.strftime("%H:%M")

            if self.stop_requested:
                # If SIGTERM arrived during scheduled session overlap window, hand over cleanly instead of squaring off!
                stop_type = "SESSION HANDOVER" if is_scheduled_handover_window(now_ist) else "MANUAL STOP"
                self.shutdown_cleanly(now_ist, stop_type)
                break

            if (time.time() - session_start_ts) >= SESSION_MAX_RUNTIME_SEC:
                self.shutdown_cleanly(now_ist, "SESSION HANDOVER")
                break

            self.manage_open_positions(now_ist)

            if hhmm >= "23:20":
                self.shutdown_cleanly(now_ist, "EOD CLOSED")
                break

            max_profit = CAPITAL_PER_TRADE * DAILY_PROFIT_LOCK_PCT
            max_loss = CAPITAL_PER_TRADE * DAILY_LOSS_LOCK_PCT
            risk_locked = (self.day_pnl >= max_profit or self.day_pnl <= max_loss or self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES)

            if risk_locked:
                self.bot_status_text = "🟡 BOT PAUSED • RISK LIMIT"
            else:
                self.bot_status_text = "🟢 BOT LIVE • RUNNING"
                for idx, cfg in INDICES.items():
                    if cfg["type"] == "INDEX_OPT" and hhmm > "15:20":
                        if idx not in self.last_summaries:
                            spot_df = fetch_intraday_5m(cfg["spot_key"])
                            if not spot_df.empty:
                                self.last_summaries[idx] = {
                                    "idx": idx, "is_mcx": False, "spot": int(round(float(spot_df["close"].iloc[-1]))),
                                    "fuel": 0, "pcr": 0.0, "cum_pcr_diff": 0.0, "short_status": "CLOSED 🏁",
                                }
                        continue
                    res = self.evaluate_instrument(idx, cfg, now_ist)
                    if res:
                        self.last_summaries[idx] = res

            now_ts = time.time()
            if self.last_summaries and (now_ts - self.last_dashboard_ts >= DASHBOARD_REFRESH_SEC):
                self.last_dashboard_ts = now_ts
                self.render_live_dashboard(hhmm)

            time.sleep(10)


if __name__ == "__main__":
    engine = IntradayV3Engine()
    engine.run()
