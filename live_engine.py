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
# 1. CONFIGURATION & ENVIRONMENT (100% DYNAMIC CONTRACT RESOLUTION)
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
PAPER_MODE = os.getenv("PAPER_MODE", "true").strip().lower() == "true"

# --- STRATEGY & RISK PARAMETERS ---
MIN_FUEL_SCORE = 45.0             # Base minimum fuel score (Indices & Morning MCX)
MCX_EVENING_FUEL_SCORE = 55.0     # Stricter fuel filter for volatile evening MCX (>= 15:30 IST)
MCX_MAX_RISK_INR = 2000.0         # Maximum allowed SL risk (₹) per MCX trade
SESSION_MAX_RUNTIME_SEC = 20700   # 5h 45m graceful exit before GitHub 6h hard kill

INDEX_START_TIME = "09:20"
INDEX_END_TIME = "15:10"
INDEX_EXIT_TIME = "15:15"
EXPIRY_NEXT_SWITCH_TIME = "13:30" # Switch to next expiry after 1:30 PM on 0DTE

MCX_START_TIME = "09:20"
MCX_END_TIME = "23:00"
MCX_EXIT_TIME = "23:15"

GANN_ZONE_ATR_MULT = 0.40         # Allowed proximity above/below Gann level
MAX_SPREAD_PCT = 1.50             # Bid-Ask spread protection (%)
SWING_BUFFER_ATR_MULT = 0.25      # Buffer beyond 3-candle Swing Low/High
RR_RATIO = 2.00                   # Fixed 1 : 2.0 Risk-to-Reward Target
SETUP_EXPIRY_SEC = 900            # 15-minute setup validity
DASHBOARD_REFRESH_SEC = 30        # Self-updating pinned Telegram Live Dashboard interval
JOURNAL_FILE = "live_journal/trade_journal.csv"
ACTIVE_STATE_FILE = "live_journal/active_positions.json"

# --- TRAILING SL, FUEL FINISH & NO-MOMENTUM EXIT PARAMETERS ---
TRAIL_BE_PROGRESS = 0.45          # Move SL to Cost-to-Cost at 45% of Target
TRAIL_LOCK_PROGRESS = 0.70        # Activate Swing + 50% Profit Lock at 70% of Target
TRAIL_PROFIT_FLOOR = 0.50         # Lock at least 50% of target gain once 70% reached

FUEL_FINISH_MIN_PROGRESS = 0.25   # Check Fuel Finish once >= 25% of Target reached
FUEL_FINISH_THRESHOLD = 22.0      # Fuel drop threshold signaling movement finish

FLAT_LTP_NORMAL_SEC = 900         # 15 mins flat price -> No-Momentum Exit
FLAT_LTP_EXPIRY_SEC = 600         # 10 mins flat price on expiry day -> Fast Exit

CAPITAL_PER_TRADE = float(os.getenv("CAPITAL_PER_TRADE", "15000"))
MCX_CAPITAL_PER_TRADE = float(os.getenv("MCX_CAPITAL_PER_TRADE", os.getenv("CRUDEOIL_CAPITAL", "150000")))
DAILY_PROFIT_LOCK_PCT = 0.50
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


# ==============================================================================
# 2. DESIGNER TELEGRAM UI (🟢 LIVE / 🔴 STOPPED HEARTBEAT + SINGLE ALERTS)
# ==============================================================================
def notify(msg: str, silent: bool = False):
    clean_lines = [line.strip() for line in msg.splitlines() if line.strip()]
    mirror_text = " | ".join(clean_lines)
    log.info(f"[MIRROR] {mirror_text}")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
        "disable_notification": silent,
    }

    for attempt in range(2):
        try:
            requests.post(url, json=payload, timeout=8)
            return
        except Exception as e:
            if attempt == 0:
                time.sleep(0.5)
            else:
                log.warning(f"Telegram send error after retry: {e}")


def make_progress_bar(progress_ratio: float, length: int = 8) -> str:
    clamped = max(0.0, min(1.0, progress_ratio))
    filled = int(round(clamped * length))
    return "▰" * filled + "▱" * (length - filled)


def send_trade_alert_with_chart(df: pd.DataFrame, title: str, caption: str, gann_lvl: float, spot_entry: float, spot_sl: float, spot_trg: float, is_exit: bool = False, spot_exit: float = None):
    """Sends a single combined Photo + Caption alert (falls back to text-only if chart fails)."""
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
            ax.axhline(spot_sl, color="#f43f5e", linestyle=":", linewidth=1.6, label=f"Swing SL: {spot_sl:.1f}")
            ax.axhline(spot_trg, color="#a855f7", linestyle=":", linewidth=1.6, label=f"1:2 Target: {spot_trg:.1f}")

            ax.axhspan(min(spot_entry, spot_sl), max(spot_entry, spot_sl), color="#f43f5e", alpha=0.08)
            ax.axhspan(min(spot_entry, spot_trg), max(spot_entry, spot_trg), color="#22c55e", alpha=0.08)

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

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": caption, "parse_mode": "HTML"}, timeout=8)
    except Exception as e:
        log.warning(f"Text alert fallback error: {e}")


def upsert_telegram_dashboard(text: str, message_id: int = None) -> int:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return message_id

    if message_id:
        edit_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            resp = requests.post(edit_url, json=payload, timeout=8).json()
            if resp.get("ok") or "message is not modified" in str(resp.get("description", "")).lower():
                return message_id
        except Exception as e:
            log.warning(f"Dashboard edit error: {e}")

    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": True,
    }
    try:
        resp = requests.post(send_url, json=payload, timeout=8).json()
        if resp.get("ok"):
            new_id = resp["result"]["message_id"]
            pin_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/pinChatMessage"
            requests.post(
                pin_url,
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
                buy_resp = charge_api.get_brokerage(
                    instrument_key, int(qty), "I", "BUY", round(float(buy_price), 2), "2.0"
                )
                sell_resp = charge_api.get_brokerage(
                    instrument_key, int(qty), "I", "SELL", round(float(sell_price), 2), "2.0"
                )
                b_brk = float(buy_resp.data.charges.brokerage or 0.0)
                s_brk = float(sell_resp.data.charges.brokerage or 0.0)
                b_tot = float(buy_resp.data.charges.total or 0.0)
                s_tot = float(sell_resp.data.charges.total or 0.0)
                if (b_tot + s_tot) > 0:
                    tot = round(b_tot + s_tot, 2)
                    brk = round(b_brk + s_brk, 2)
                    return {"brokerage": brk, "taxes_other": round(tot - brk, 2), "total": tot}
            except Exception as e:
                log.debug(f"SDK ChargeApi fallback to REST ({instrument_key}): {e}")

        url = "https://api.upstox.com/v2/charges/brokerage"
        tot_sum = 0.0
        brk_sum = 0.0
        api_ok = True
        for tx_type, price in (("BUY", buy_price), ("SELL", sell_price)):
            params = {
                "instrument_token": instrument_key,
                "quantity": int(qty),
                "product": "I",
                "transaction_type": tx_type,
                "price": round(float(price), 2),
            }
            try:
                resp = requests.get(url, headers=upstox_headers(), params=params, timeout=8)
                res = resp.json()
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
        "trade_no",
        "date",
        "entry_time",
        "exit_time",
        "symbol",
        "contract",
        "side",
        "qty",
        "entry_price",
        "exit_price",
        "points",
        "sl",
        "target",
        "exit_reason",
        "gross_pnl",
        "brokerage",
        "taxes_other",
        "charges",
        "pnl",
        "day_pnl",
        "setup",
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


def build_master_journal_report(now_ist: datetime) -> str:
    today_str = now_ist.strftime("%Y-%m-%d")
    curr_year, curr_week, _ = now_ist.isocalendar()
    curr_month_prefix = now_ist.strftime("%Y-%m")
    month_label = now_ist.strftime("%B %Y")

    rows = []
    if os.path.isfile(JOURNAL_FILE):
        try:
            with open(JOURNAL_FILE, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    try:
                        d_obj = datetime.strptime(r["date"], "%Y-%m-%d")
                        y_iso, w_iso, _ = d_obj.isocalendar()
                        rows.append({
                            "date": r["date"],
                            "year": y_iso,
                            "week": w_iso,
                            "points": float(r.get("points", 0.0)),
                            "brokerage": float(r.get("brokerage", 0.0)),
                            "taxes_other": float(r.get("taxes_other", 0.0)),
                            "charges": float(r.get("charges", 0.0)),
                            "pnl": float(r.get("pnl", 0.0)),
                        })
                    except Exception:
                        continue
        except Exception as e:
            log.warning(f"Journal read warning: {e}")

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

    report = [
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
    ]
    return "\n".join(report)


# ==============================================================================
# 3. UPSTOX V3 API HELPERS (DYNAMIC CONTRACTS + CANDLES + MARGIN + OPTION CHAIN)
# ==============================================================================
def get_active_future_contract(idx: str) -> dict:
    exchange = "BSE" if idx == "SENSEX" else ("MCX" if INDICES[idx]["type"] == "MCX_FUT" else "NSE")
    url = "https://api.upstox.com/v2/instruments/search"

    for expiry_kw in ("current_month", "next_month"):
        try:
            params = {
                "query": idx,
                "exchanges": exchange,
                "segments": "FUT",
                "expiry": expiry_kw,
                "page_number": 1,
                "records": 30,
            }
            resp = requests.get(url, headers=upstox_headers(), params=params, timeout=10)
            res = resp.json()
            items = res.get("data", [])
            if not items:
                continue

            exact = [
                it for it in items
                if str(it.get("instrument_type", "")).upper() == "FUT"
                and str(it.get("trading_symbol", "")).upper().startswith(f"{idx} FUT")
            ]
            candidates = exact if exact else [
                it for it in items if str(it.get("instrument_type", "")).upper() == "FUT"
            ]
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
        url = "https://api.upstox.com/v2/option/contract"
        params = {"instrument_key": spot_key}
        resp = requests.get(url, headers=upstox_headers(), params=params, timeout=10)
        contracts = resp.json().get("data", [])
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
    except Exception as e:
        log.error(f"Dynamic option contract fetch error ({spot_key}): {e}")
        return {"expiries": [], "lot_size": 1, "freeze_quantity": 1800, "tick_size": 0.05, "step": 50}


def fetch_intraday_5m(instrument_key: str) -> pd.DataFrame:
    if not instrument_key:
        return pd.DataFrame()
    encoded_key = requests.utils.quote(instrument_key, safe="")
    url = f"https://api.upstox.com/v3/historical-candle/intraday/{encoded_key}/minutes/5"
    try:
        resp = requests.get(url, headers=upstox_headers(), timeout=10)
        data = resp.json()
        candles = data.get("data", {}).get("candles", [])
        if not candles:
            return pd.DataFrame()
        df = pd.DataFrame(
            candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        for col in ["open", "high", "low", "close", "volume", "oi"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        return df
    except Exception as e:
        log.error(f"Candle fetch error ({instrument_key}): {e}")
        return pd.DataFrame()


def fetch_ltp_quote(instrument_keys: list) -> dict:
    if not instrument_keys:
        return {}
    try:
        url = "https://api.upstox.com/v2/market-quote/quotes"
        params = {"instrument_key": ",".join(instrument_keys)}
        resp = requests.get(url, headers=upstox_headers(), params=params, timeout=10)
        raw_data = resp.json().get("data", {})
        normalized = {}
        for k, v in raw_data.items():
            normalized[k] = v
            normalized[k.replace(":", "|")] = v
            inst_tok = v.get("instrument_token")
            if inst_tok:
                normalized[inst_tok] = v
        return normalized
    except Exception as e:
        log.error(f"Quote fetch error: {e}")
        return {}


def fetch_required_margin(instrument_key: str, lots_or_qty: int, side: str, product: str = "I") -> float:
    try:
        url = "https://api.upstox.com/v2/charges/margin"
        payload = {
            "instruments": [
                {
                    "instrument_key": instrument_key,
                    "quantity": int(lots_or_qty),
                    "transaction_type": side,
                    "product": product,
                }
            ]
        }
        resp = requests.post(url, headers=upstox_headers(), json=payload, timeout=10)
        data = resp.json()
        if data.get("status") == "success":
            return float(data.get("data", {}).get("required_margin", 0.0))
        return 0.0
    except Exception as e:
        log.warning(f"Margin API error ({instrument_key}): {e}")
        return 0.0


def fetch_option_chain(spot_key: str, expiry_date: str) -> list:
    if not spot_key or not expiry_date:
        return []
    try:
        url = "https://api.upstox.com/v2/option/chain"
        params = {"instrument_key": spot_key, "expiry_date": expiry_date}
        resp = requests.get(url, headers=upstox_headers(), params=params, timeout=10)
        return resp.json().get("data", [])
    except Exception as e:
        log.error(f"Option chain fetch error ({spot_key}): {e}")
        return []


# ==============================================================================
# 4. GANN LEVELS, 6 CANDLE PATTERNS & FUEL SCORE (WICK + 1.2x PREV-20 ATR)
# ==============================================================================
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

    c1 = df.iloc[-3]
    c2 = df.iloc[-2]
    c3 = df.iloc[-1]

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
    is_bull_engulf = (cl3 > o3) and (cl2 < o2) and (cl3 >= o2) and (o3 <= cl2)
    is_hammer = (lower_wick3 >= 1.8 * max(b3, r3 * 0.1)) and (upper_wick3 <= 0.25 * r3) and ((cl3 - l3) / r3 >= 0.60)
    is_morning_star = (
        (cl1 < o1) and (b1 / r1 >= 0.45)
        and (b2 / r2 <= 0.40)
        and (cl3 > o3) and (cl3 > mid1)
    )

    if is_bull_engulf:
        bull_pattern = "BULLISH_ENGULFING"
    elif is_morning_star:
        bull_pattern = "MORNING_STAR"
    elif is_hammer:
        bull_pattern = "HAMMER"

    bear_pattern = None
    is_bear_engulf = (cl3 < o3) and (cl2 > o2) and (cl3 <= o2) and (o3 >= cl2)
    is_reverse_hammer = (upper_wick3 >= 1.8 * max(b3, r3 * 0.1)) and (lower_wick3 <= 0.25 * r3) and ((h3 - cl3) / r3 >= 0.60)
    is_evening_star = (
        (cl1 > o1) and (b1 / r1 >= 0.45)
        and (b2 / r2 <= 0.40)
        and (cl3 < o3) and (cl3 < mid1)
    )

    if is_bear_engulf:
        bear_pattern = "BEARISH_ENGULFING"
    elif is_evening_star:
        bear_pattern = "EVENING_STAR"
    elif is_reverse_hammer:
        bear_pattern = "REVERSE_HAMMER"

    return {
        "near_gann": near_gann,
        "bull_pattern": bull_pattern,
        "bear_pattern": bear_pattern,
    }


def compute_fuel_with_fut_volume(spot_df: pd.DataFrame, fut_df: pd.DataFrame) -> pd.DataFrame:
    if spot_df.empty or len(spot_df) < 3:
        return spot_df

    prev_close = spot_df["close"].shift(1).fillna(spot_df["open"])
    tr = pd.concat(
        [
            spot_df["high"] - spot_df["low"],
            (spot_df["high"] - prev_close).abs(),
            (spot_df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    spot_df["tr"] = tr
    spot_df["atr14"] = tr.rolling(14, min_periods=1).mean()
    spot_df["prev20_atr"] = tr.rolling(20, min_periods=1).mean().shift(1).fillna(tr)

    if not fut_df.empty and "volume" in fut_df.columns and fut_df["volume"].sum() > 0:
        fut_vol = fut_df[["timestamp", "volume"]].rename(columns={"volume": "fut_vol"})
        spot_df = pd.merge_asof(
            spot_df.sort_values("timestamp"),
            fut_vol.sort_values("timestamp"),
            on="timestamp",
            direction="nearest",
        )
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
        if atr_ratio >= 1.2:
            atr_pts = 30.0
        else:
            atr_pts = min(29.0, max(0.0, (atr_ratio / 1.2) * 28.0))

        c_range = max(row["high"] - row["low"], 1e-6)
        body = abs(row["close"] - row["open"])
        upper_wick = row["high"] - max(row["open"], row["close"])
        lower_wick = min(row["open"], row["close"]) - row["low"]

        pa_strength = max(body / c_range, max(upper_wick, lower_wick) / c_range)
        eff_pts = min(20.0, max(0.0, pa_strength * 20.0))

        raw_fuel = 10.0 + vol_pts + atr_pts + eff_pts
        fuel_scores.append(round(min(100.0, max(0.0, raw_fuel)), 1))

    spot_df["fuel"] = fuel_scores
    return spot_df


# ==============================================================================
# 5. ORDER EXECUTION & POSITION MANAGEMENT
# ==============================================================================
def place_marketable_limit_order(instrument_key: str, qty: int, side: str, ltp: float, tick_size: float = 0.05) -> dict:
    is_mcx = instrument_key.startswith("MCX_FO")
    buffer = (1.003 if is_mcx else 1.015) if side == "BUY" else (0.997 if is_mcx else 0.985)
    limit_price = round_to_tick(ltp * buffer, tick_size)

    if PAPER_MODE:
        log.info(f"[PAPER ORDER] {side} {qty} x {instrument_key} @ ₹{ltp:.2f} (Limit ₹{limit_price})")
        return {"status": "success", "fill_price": ltp, "order_id": f"PAPER_{int(time.time())}"}

    try:
        url = "https://api.upstox.com/v2/order/place"
        payload = {
            "quantity": qty,
            "product": "I",
            "validity": "DAY",
            "price": limit_price,
            "tag": "V3_GANN_ENGINE",
            "instrument_token": instrument_key,
            "order_type": "LIMIT",
            "transaction_type": side,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
        }
        resp = requests.post(url, headers=upstox_headers(), json=payload, timeout=10)
        res = resp.json()
        if res.get("status") == "success":
            return {"status": "success", "fill_price": ltp, "order_id": res["data"]["order_id"]}
        return {"status": "error", "message": str(res)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ==============================================================================
# 6. MAIN TRADING ENGINE LOOP (WITH ACTIVE_POSITIONS.JSON SESSION HANDOVER)
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

    def save_active_positions_state(self):
        """Persists currently open positions to JSON for seamless session handover."""
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
        """Restores any open position from the previous session and resumes Tick-WS."""
        if not os.path.isfile(ACTIVE_STATE_FILE):
            return
        try:
            with open(ACTIVE_STATE_FILE, mode="r", encoding="utf-8") as f:
                data = json.load(f)
            if not data:
                return
            with self.pos_lock:
                for idx, pos in data.items():
                    pos["ltp_window"] = deque(pos.get("ltp_window", []))
                    self.active_positions[idx] = pos
                    self.trade_counter = max(self.trade_counter, int(pos.get("trade_no", 0)))
            for idx, pos in list(self.active_positions.items()):
                self.subscribe_tick_instrument(pos["instrument_key"])
                log.info(
                    f"[HANDOVER] Resumed open position #{pos['trade_no']} {idx} {pos['strike']} "
                    f"@ ₹{pos['entry_price']:.2f} (SL: ₹{pos['sl']:.2f} | TRG: ₹{pos['tp']:.2f})"
                )
        except Exception as e:
            log.warning(f"Active positions restore error: {e}")

    def load_today_state_from_journal(self):
        """Restores today's closed trades and cumulative day_pnl across session restarts."""
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        if not os.path.isfile(JOURNAL_FILE):
            return
        try:
            with open(JOURNAL_FILE, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    if r.get("date") == today_str:
                        pnl_val = float(r.get("pnl", 0.0))
                        t_no = int(float(r.get("trade_no", len(self.closed_trades) + 1)))
                        self.closed_trades.append({
                            "trade_no": t_no,
                            "date": r["date"],
                            "entry_time": r.get("entry_time", ""),
                            "exit_time": r.get("exit_time", ""),
                            "symbol": r.get("symbol", ""),
                            "contract": r.get("contract", ""),
                            "side": r.get("side", ""),
                            "qty": int(float(r.get("qty", 0))),
                            "entry_price": float(r.get("entry_price", 0.0)),
                            "exit_price": float(r.get("exit_price", 0.0)),
                            "points": float(r.get("points", 0.0)),
                            "sl": float(r.get("sl", 0.0)),
                            "target": float(r.get("target", 0.0)),
                            "exit_reason": r.get("exit_reason", ""),
                            "gross_pnl": float(r.get("gross_pnl", 0.0)),
                            "brokerage": float(r.get("brokerage", 0.0)),
                            "taxes_other": float(r.get("taxes_other", 0.0)),
                            "charges": float(r.get("charges", 0.0)),
                            "pnl": pnl_val,
                            "day_pnl": float(r.get("day_pnl", 0.0)),
                            "setup": r.get("setup", ""),
                        })
                        self.day_pnl = round(self.day_pnl + pnl_val, 2)
                        self.trade_counter = max(self.trade_counter, t_no)
            self.closed_trades.sort(key=lambda x: x["trade_no"])
            if self.closed_trades:
                log.info(f"[JOURNAL] Restored {len(self.closed_trades)} trades for {today_str} | Day Net: ₹{self.day_pnl:+.2f}")
        except Exception as e:
            log.warning(f"Journal state restore warning: {e}")

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
                log.info("[WS_V3] Connected to Upstox MarketDataStreamerV3 (On-Demand Tick-by-Tick active)")
                with self.pos_lock:
                    active_keys = [p["instrument_key"] for p in self.active_positions.values()]
                if active_keys:
                    self.streamer.subscribe(active_keys, "ltpc")
                    self.subscribed_keys.update(active_keys)

            def on_message(message):
                try:
                    data = json.loads(message) if isinstance(message, str) else message
                    feeds = data.get("feeds", {})
                    if not feeds:
                        return
                    for inst_key, feed_obj in feeds.items():
                        ltpc = (
                            feed_obj.get("ltpc")
                            or feed_obj.get("fullFeed", {}).get("marketFF", {}).get("ltpc")
                            or feed_obj.get("firstLevelWithGreeks", {}).get("ltpc")
                        )
                        if not ltpc or "ltp" not in ltpc:
                            continue
                        tick_ltp = float(ltpc["ltp"])
                        if tick_ltp > 0:
                            self.on_tick_price(inst_key, tick_ltp)
                except Exception as e:
                    log.debug(f"[WS_V3] Parse skip: {e}")

            def on_error(err):
                log.warning(f"[WS_V3] Stream error: {err}")

            def on_close():
                self.ws_connected = False
                log.info("[WS_V3] Stream closed.")

            self.streamer.on("open", on_open)
            self.streamer.on("message", on_message)
            self.streamer.on("error", on_error)
            self.streamer.on("close", on_close)

            ws_thread = threading.Thread(target=self.streamer.connect, daemon=True)
            ws_thread.start()
        except Exception as e:
            log.warning(f"[WS_V3] Init failed (using REST fallback): {e}")

    def subscribe_tick_instrument(self, instrument_key: str):
        if not HAS_UPSTOX_SDK:
            return
        self.subscribed_keys.add(instrument_key)
        if not self.streamer:
            self.init_websocket_streamer()
        elif self.ws_connected:
            try:
                self.streamer.subscribe([instrument_key], "ltpc")
                log.info(f"[WS_V3] Subscribed tick-by-tick LTPC: {instrument_key}")
            except Exception as e:
                log.warning(f"[WS_V3] Subscribe error ({instrument_key}): {e}")

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
            log.info("[WS_V3] All trades closed — Tick-by-Tick WebSocket disconnected (Scanning Mode).")

    def on_tick_price(self, instrument_key: str, ltp: float):
        norm_key = instrument_key.replace(":", "|")
        target_idx = None
        exit_reason = None

        with self.pos_lock:
            for idx, pos in self.active_positions.items():
                if pos["instrument_key"].replace(":", "|") == norm_key:
                    pos["last_ltp"] = ltp
                    is_short = pos["tx_side"] == "SELL"
                    curr_gain = (pos["entry_price"] - ltp) if is_short else (ltp - pos["entry_price"])
                    if curr_gain > pos.get("peak_gain", 0.0):
                        pos["peak_gain"] = curr_gain
                        pos["last_peak_ts"] = time.time()

                    if not is_short and ltp <= pos["sl"]:
                        exit_reason = "TRAIL_SL_HIT" if pos.get("trail_stage") else "STOP_LOSS_HIT"
                        target_idx = idx
                    elif not is_short and ltp >= pos["tp"]:
                        exit_reason = "TARGET_1_HIT"
                        target_idx = idx
                    elif is_short and ltp >= pos["sl"]:
                        exit_reason = "TRAIL_SL_HIT" if pos.get("trail_stage") else "STOP_LOSS_HIT"
                        target_idx = idx
                    elif is_short and ltp <= pos["tp"]:
                        exit_reason = "TARGET_1_HIT"
                        target_idx = idx
                    break

        if target_idx and exit_reason:
            log.info(f"[TICK EXIT] {target_idx} hit {exit_reason} @ ₹{ltp:.2f}")
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
                margin_1_lot = (
                    fetch_required_margin(cfg["spot_key"], 1, "BUY", "I")
                    if cfg["spot_key"]
                    else 0.0
                )
                log.info(
                    f"[{idx}] MCX FUT | Symbol: {fut_info.get('trading_symbol', 'N/A')} "
                    f"| Key: {cfg['spot_key']} | Expiry: {fut_info.get('expiry', 'N/A')} "
                    f"| Lot: {cfg['lot_size']} | Tick: {cfg['tick_size']} | 1-Lot Margin: ₹{margin_1_lot:.2f}"
                )
            else:
                opt_specs = get_dynamic_option_specs(cfg["spot_key"])
                valid_exps = opt_specs["expiries"]
                self.expiries[idx] = valid_exps
                cfg["lot_size"] = opt_specs["lot_size"]
                cfg["freeze_qty"] = opt_specs["freeze_quantity"]
                cfg["tick_size"] = opt_specs["tick_size"]
                cfg["step"] = opt_specs["step"]
                exp_display = valid_exps[0] if valid_exps else "N/A"
                next_display = valid_exps[1] if len(valid_exps) > 1 else exp_display
                log.info(
                    f"[{idx}] Expiries: Current={exp_display}, Next={next_display} "
                    f"| Lot: {cfg['lot_size']} | Step: {cfg['step']} | Tick: {cfg['tick_size']} "
                    f"| FUT Vol Key: {fut_info.get('instrument_key') or 'SPOT_FALLBACK'}"
                )

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
        if INDICES[idx]["type"] == "MCX_FUT":
            return MCX_START_TIME <= hhmm <= MCX_END_TIME
        return INDEX_START_TIME <= hhmm <= INDEX_END_TIME

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
        oi_bull_ok = True
        oi_bear_ok = True
        if prev_oi is not None:
            delta_pe_oi = total_pe_oi - prev_oi["pe_oi"]
            delta_ce_oi = total_ce_oi - prev_oi["ce_oi"]
            oi_bull_ok = (delta_pe_oi >= delta_ce_oi) or (delta_ce_oi < 0)
            oi_bear_ok = (delta_ce_oi >= delta_pe_oi) or (delta_pe_oi < 0)

        self.oi_history[idx] = {"pe_oi": total_pe_oi, "ce_oi": total_ce_oi}

        bull_confirmed = (cum_bullish or short_rising) and oi_bull_ok
        bear_confirmed = (cum_bearish or short_falling) and oi_bear_ok
        return pcr, open_p, cum_pcr_diff, bull_confirmed, bear_confirmed

    def compute_live_mtm(self) -> float:
        with self.pos_lock:
            if not self.active_positions:
                return 0.0
            positions_snap = list(self.active_positions.values())

        if not self.ws_connected:
            keys = [p["instrument_key"] for p in positions_snap]
            quotes = fetch_ltp_quote(keys)
            for pos in positions_snap:
                q_data = quotes.get(pos["instrument_key"], {})
                pos["last_ltp"] = float(q_data.get("last_price", pos.get("last_ltp", pos["entry_price"])))

        total_mtm = 0.0
        for pos in positions_snap:
            ltp = pos.get("last_ltp", pos["entry_price"])
            is_short = pos["tx_side"] == "SELL"
            pnl = (pos["entry_price"] - ltp) * pos["qty"] if is_short else (ltp - pos["entry_price"]) * pos["qty"]
            total_mtm += pnl
        return round(total_mtm, 2)

    def render_live_dashboard(self, hhmm: str):
        """Pinned Telegram Dashboard with clear 🟢 BOT LIVE / 🔴 BOT STOPPED status & #1..#N ordered ledger."""
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
            f"   └ Today Trades: <b>{len(self.closed_trades)}</b> (🟢 {total_wins}W) │ Index: <b>{len(idx_trades)}</b> │ MCX: <b>{len(mcx_trades)}</b>",
        ]

        with self.pos_lock:
            active_snap = sorted(self.active_positions.items(), key=lambda item: item[1]["trade_no"])

        if active_snap:
            lines.append("\n🔥 <b>ACTIVE POSITIONS</b>")
            lines.append("──────────────────────────────")
            for p_idx, pos in active_snap:
                ltp = pos.get("last_ltp", pos["entry_price"])
                is_short = pos["tx_side"] == "SELL"
                pnl = (pos["entry_price"] - ltp) * pos["qty"] if is_short else (ltp - pos["entry_price"]) * pos["qty"]
                curr_gain = (pos["entry_price"] - ltp) if is_short else (ltp - pos["entry_price"])
                trg_dist = max(abs(pos["tp"] - pos["entry_price"]), 1e-6)
                prog = curr_gain / trg_dist
                bar = make_progress_bar(prog, 8)
                trail_tag = " 🛡️<code>TRAIL</code>" if pos.get("trail_stage") else ""
                in_t = pos["entry_time"][:5]
                p_icon = "🟢" if pnl >= 0 else "🔴"
                lines.append(
                    f"<b>#{pos['trade_no']} {p_idx}</b> <code>{pos['strike']} {pos['display_side']}</code>{trail_tag}\n"
                    f"  ├ 🕒 <code>{in_t}</code> │ In <b>₹{pos['entry_price']:.2f}</b> → LTP <b>₹{ltp:.2f}</b>\n"
                    f"  ├ 🛡️ SL <code>₹{pos['sl']:.2f}</code> │ 🎯 TRG <code>₹{pos['tp']:.2f}</code>\n"
                    f"  └ {bar} <b>{int(round(prog * 100))}%</b> │ {p_icon} <b>₹{pnl:+,.2f}</b>"
                )

        lines.append("\n📒 <b>TODAY'S TRADE LEDGER (#1 → #N)</b>")
        lines.append("──────────────────────────────")
        if self.closed_trades:
            ordered_trades = sorted(self.closed_trades, key=lambda x: x["trade_no"])
            for t in ordered_trades:
                icon = "✅" if t["pnl"] >= 0 else "❌"
                in_t = t["entry_time"][:5]
                out_t = t["exit_time"][:5]
                lines.append(
                    f"<b>#{t['trade_no']}</b> {icon} <b>{t['symbol']}</b> <code>{t['contract']} {t['side']}</code>\n"
                    f"  ├ 🕒 <code>{in_t}→{out_t}</code> │ ₹{t['entry_price']:.2f}→₹{t['exit_price']:.2f} (<b>{t['points']:+.2f}p</b>)\n"
                    f"  └ SL:<code>{t['sl']:.2f}</code> │ TRG:<code>{t['target']:.2f}</code> │ {t['exit_reason']} │ <b>₹{t['pnl']:+,.2f}</b>"
                )
        elif not active_snap:
            lines.append("  <i>Scanning Gann + Fuel setups across 10 markets...</i>")

        if self.last_summaries:
            lines.append("\n📡 <b>5 INDICES RADAR</b>")
            lines.append("──────────────────────────────")
            for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"):
                s = self.last_summaries.get(idx)
                if not s:
                    continue
                lines.append(
                    f"• <b>{s['idx']:<10}</b> <code>{s['spot']}</code> │ Fuel <b>{s['fuel']}</b> │ PCR {s['pcr']} │ {s['short_status']}"
                )

            lines.append("\n🛢️ <b>5 MCX COMMODITIES RADAR</b>")
            lines.append("──────────────────────────────")
            for idx in ("CRUDEOIL", "NATURALGAS", "GOLD", "SILVER", "COPPER"):
                s = self.last_summaries.get(idx)
                if not s:
                    continue
                lines.append(
                    f"• <b>{s['idx']:<10}</b> <code>{s['spot']}</code> │ Fuel <b>{s['fuel']}</b> │ ΔOI {s['cum_pcr_diff']:+.0f} │ {s['short_status']}"
                )

        dashboard_text = "\n".join(lines)
        log.info(f"[DASHBOARD] {self.bot_status_text} | Realized ₹{self.day_pnl:+.2f} | Live MTM ₹{live_mtm:+.2f} | Closed: {len(self.closed_trades)} | Active: {len(active_snap)}")
        self.dashboard_msg_id = upsert_telegram_dashboard(dashboard_text, self.dashboard_msg_id)

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
        tick_sz = pos.get("tick_size", 0.05)
        place_marketable_limit_order(pos["instrument_key"], pos["qty"], close_side, ltp, tick_sz)

        pts = round((pos["entry_price"] - ltp) if is_short else (ltp - pos["entry_price"]), 2)
        gross_pnl = round(pts * pos["qty"], 2)

        buy_px = ltp if is_short else pos["entry_price"]
        sell_px = pos["entry_price"] if is_short else ltp
        ch_info = fetch_exact_trade_charges(pos["instrument_key"], buy_px, sell_px, pos["qty"], is_mcx)
        total_charges = ch_info["total"]
        net_pnl = round(gross_pnl - total_charges, 2)

        self.day_pnl = round(self.day_pnl + net_pnl, 2)
        if net_pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        exit_time_str = now_ist.strftime("%H:%M:%S")
        journal_entry = {
            "trade_no": pos["trade_no"],
            "date": now_ist.strftime("%Y-%m-%d"),
            "entry_time": pos.get("entry_time", ""),
            "exit_time": exit_time_str,
            "symbol": idx,
            "contract": str(pos["strike"]),
            "side": pos["display_side"],
            "qty": pos["qty"],
            "entry_price": pos["entry_price"],
            "exit_price": ltp,
            "points": pts,
            "sl": pos["sl"],
            "target": pos["tp"],
            "exit_reason": exit_reason,
            "gross_pnl": gross_pnl,
            "brokerage": ch_info["brokerage"],
            "taxes_other": ch_info["taxes_other"],
            "charges": total_charges,
            "pnl": net_pnl,
            "day_pnl": self.day_pnl,
            "setup": pos["setup_name"],
        }
        self.closed_trades.append(journal_entry)
        record_trade_journal(journal_entry)

        icon = "✅" if net_pnl >= 0 else "❌"
        exit_caption = (
            f"🏁 {icon} <b>TRADE #{pos['trade_no']} CLOSED • {idx}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>Contract:</b> <code>{pos['strike']} {pos['display_side']}</code> │ <b>{exit_reason}</b>\n"
            f"🕒 <b>Time:</b> <code>{pos['entry_time'][:5]} → {exit_time_str[:5]}</code>\n"
            f"💵 <b>Price:</b> ₹{pos['entry_price']:.2f} → ₹{ltp:.2f} (<b>{pts:+.2f} pts</b>)\n"
            f"📊 <b>Gross PnL:</b> ₹{gross_pnl:+,.2f}\n"
            f"🧾 <b>Charges:</b> -₹{total_charges:.2f} <i>(Brk ₹{ch_info['brokerage']:.2f} + Tax/STT ₹{ch_info['taxes_other']:.2f})</i>\n"
            f"💰 <b>Net PnL:</b> <b>₹{net_pnl:+,.2f}</b>  │  <b>Day Net:</b> <b>₹{self.day_pnl:+,.2f}</b>"
        )

        df_plot = self.latest_dfs.get(idx, pd.DataFrame())
        spot_now = float(df_plot["close"].iloc[-1]) if not df_plot.empty else pos.get("spot_entry", 0.0)
        send_trade_alert_with_chart(
            df_plot,
            title=f"{idx} 5M EXIT │ #{pos['trade_no']} {pos['strike']} {pos['display_side']} ({exit_reason})",
            caption=exit_caption,
            gann_lvl=pos.get("gann_level", spot_now),
            spot_entry=pos.get("spot_entry", spot_now),
            spot_sl=pos.get("spot_sl", spot_now),
            spot_trg=pos.get("spot_t1", spot_now),
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
        tp = pos["tp"]
        ltp = pos.get("last_ltp", entry)
        tick_sz = pos.get("tick_size", 0.05)
        eff_delta = pos.get("delta", 0.50)

        total_target_dist = abs(tp - entry)
        if total_target_dist <= 0:
            return

        curr_gain = (entry - ltp) if is_short else (ltp - entry)
        peak_gain = max(pos.get("peak_gain", 0.0), curr_gain)
        if peak_gain > pos.get("peak_gain", 0.0):
            pos["peak_gain"] = peak_gain
            pos["last_peak_ts"] = time.time()

        progress = peak_gain / total_target_dist
        curr_progress = curr_gain / total_target_dist

        recent_3 = df.iloc[-4:-1]
        swing_low = float(recent_3["low"].min())
        swing_high = float(recent_3["high"].max())

        new_sl = pos["sl"]
        trail_label = None

        if progress >= TRAIL_LOCK_PROGRESS:
            floor_sl = round_to_tick(
                (entry - total_target_dist * TRAIL_PROFIT_FLOOR) if is_short else (entry + total_target_dist * TRAIL_PROFIT_FLOOR),
                tick_sz,
            )

            if is_mcx:
                swing_sl = round_to_tick(
                    (swing_high + SWING_BUFFER_ATR_MULT * atr14) if is_short else (swing_low - SWING_BUFFER_ATR_MULT * atr14),
                    tick_sz,
                )
                candidate_sl = min(floor_sl, swing_sl) if is_short else max(floor_sl, swing_sl)
            else:
                spot_entry = pos.get("spot_entry", spot_close)
                if pos["side"] == "CE":
                    spot_swing_buf = swing_low - SWING_BUFFER_ATR_MULT * atr14
                    delta_spot = spot_swing_buf - spot_entry
                else:
                    spot_swing_buf = swing_high + SWING_BUFFER_ATR_MULT * atr14
                    delta_spot = spot_entry - spot_swing_buf
                swing_opt_sl = round_to_tick(entry + delta_spot * eff_delta, tick_sz)
                candidate_sl = max(floor_sl, swing_opt_sl)

            min_gap = max(atr14 * 0.15, tick_sz * 2) if is_mcx else max(tick_sz * 4, 0.5)
            if is_short and candidate_sl < new_sl and candidate_sl > ltp + min_gap:
                new_sl = candidate_sl
                trail_label = "70% PROFIT + SWING TRAIL"
            elif not is_short and candidate_sl > new_sl and candidate_sl < ltp - min_gap:
                new_sl = candidate_sl
                trail_label = "70% PROFIT + SWING TRAIL"

        elif progress >= TRAIL_BE_PROGRESS:
            be_offset = max(round_to_tick(entry * 0.0001, tick_sz), tick_sz * 2)
            be_sl = round_to_tick((entry - be_offset) if is_short else (entry + be_offset), tick_sz)
            min_gap = max(atr14 * 0.15, tick_sz * 2) if is_mcx else max(tick_sz * 4, 0.5)
            if is_short and be_sl < new_sl and be_sl > ltp + min_gap:
                new_sl = be_sl
                trail_label = "COST-TO-COST (45% TRG)"
            elif not is_short and be_sl > new_sl and be_sl < ltp - min_gap:
                new_sl = be_sl
                trail_label = "COST-TO-COST (45% TRG)"

        if trail_label and new_sl != pos["sl"]:
            with self.pos_lock:
                if idx in self.active_positions:
                    self.active_positions[idx]["sl"] = new_sl
                    self.active_positions[idx]["trail_stage"] = trail_label
            self.save_active_positions_state()
            notify(
                f"🛡️📈 <b>TRAIL SL LOCKED • TRADE #{pos['trade_no']} [{idx}]</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Stage:</b> {trail_label}\n"
                f"🎯 <b>Contract:</b> <code>{pos['strike']}</code> │ <b>New SL: ₹{new_sl:.2f}</b>\n"
                f"📊 <b>LTP:</b> ₹{ltp:.2f} │ <b>Target:</b> ₹{tp:.2f}",
                silent=False,
            )

        low_fuel_streak = pos.get("low_fuel_streak", 0)
        if fuel < 25.0:
            low_fuel_streak += 1
        else:
            low_fuel_streak = 0
        pos["low_fuel_streak"] = low_fuel_streak

        opposing_move = (price_change > 0) if (pos["side"] == "PE") else (price_change < 0)
        if curr_progress >= FUEL_FINISH_MIN_PROGRESS and (
            (fuel < FUEL_FINISH_THRESHOLD and opposing_move) or low_fuel_streak >= 2
        ):
            self.close_position_now(idx, ltp, f"FUEL_FINISH (Fuel {int(fuel)})", now_ist)
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
            flat_threshold = max(total_target_dist * 0.25, entry * (0.002 if is_mcx else 0.03))
            if win_range <= flat_threshold and curr_progress < TRAIL_LOCK_PROGRESS:
                mins_stalled = int(round(window_span / 60.0))
                self.close_position_now(
                    idx,
                    ltp,
                    f"NO_MOMENTUM_EXIT ({mins_stalled}m)",
                    now_ist,
                )

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

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        spot_close = float(curr["close"])
        prev_close = float(prev["close"])
        price_change = round(spot_close - prev_close, 2)
        atr14 = max(float(curr["atr14"]), cfg.get("tick_size", 0.05) * 2)
        fuel = float(curr["fuel"])

        gann = compute_gann_levels(spot_close, cfg.get("step", 0))
        sup = gann["support"]
        res = gann["resistance"]
        gann_levels = gann["all"]

        chain = []
        active_expiry = ""
        is_today_exp = False
        if is_mcx:
            curr_oi = float(curr["oi"])
            prev_oi = float(prev["oi"])
            oi_change = curr_oi - prev_oi
            oi_pcr_bull_ok = price_change > 0
            oi_pcr_bear_ok = price_change < 0
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
                "idx": idx,
                "is_mcx": is_mcx,
                "spot": round(spot_close, 2) if spot_close < 1000 else int(round(spot_close)),
                "fuel": int(round(fuel)),
                "pcr": pcr,
                "cum_pcr_diff": cum_pcr_diff,
                "short_status": f"#{pos['trade_no']} IN TRADE (₹{pos_mtm:+.0f})🔥",
            }

        pat_info = detect_6_gann_patterns(df, gann_levels, atr14)
        near_gann = pat_info["near_gann"]
        bull_pattern = pat_info["bull_pattern"]
        bear_pattern = pat_info["bear_pattern"]

        pending = self.pending_setups.get(idx)
        if pending:
            elapsed = (now_ist - pending["armed_at"]).total_seconds()
            if elapsed > SETUP_EXPIRY_SEC:
                log.info(f"[{idx}] CANCELLED_SETUP_EXPIRED ({pending['name']})")
                self.pending_setups[idx] = None
                pending = None
            elif pending["side"] == "CE" and spot_close >= pending["spot_t1"]:
                log.info(f"[{idx}] CANCELLED_T1_ALREADY_HIT ({pending['name']})")
                self.pending_setups[idx] = None
                pending = None
            elif pending["side"] == "PE" and spot_close <= pending["spot_t1"]:
                log.info(f"[{idx}] CANCELLED_T1_ALREADY_HIT ({pending['name']})")
                self.pending_setups[idx] = None
                pending = None

        new_signal = None
        if near_gann is not None and bull_pattern is not None and price_change > 0 and oi_pcr_bull_ok:
            next_res = next((lvl for lvl in gann_levels if lvl > spot_close), res)
            t1_target = round(max(spot_close + 2.0 * atr14, next_res), 2)
            side_label = "BUY" if is_mcx else "CE"
            new_signal = {
                "side": "CE",
                "display_side": side_label,
                "name": f"GANN_{int(near_gann)}_{bull_pattern}_{side_label}",
                "gann_level": near_gann,
                "pattern": bull_pattern,
                "trigger": round(spot_close, 2),
                "spot_t1": t1_target,
                "armed_at": now_ist,
            }
        elif near_gann is not None and bear_pattern is not None and price_change < 0 and oi_pcr_bear_ok:
            next_sup = next((lvl for lvl in reversed(gann_levels) if lvl < spot_close), sup)
            t1_target = round(min(spot_close - 2.0 * atr14, next_sup), 2)
            side_label = "SELL" if is_mcx else "PE"
            new_signal = {
                "side": "PE",
                "display_side": side_label,
                "name": f"GANN_{int(near_gann)}_{bear_pattern}_{side_label}",
                "gann_level": near_gann,
                "pattern": bear_pattern,
                "trigger": round(spot_close, 2),
                "spot_t1": t1_target,
                "armed_at": now_ist,
            }

        if new_signal and not pending:
            self.pending_setups[idx] = new_signal
            pending = new_signal
            log.info(
                f"[{idx}] {pending['display_side']} Armed ({pending['name']}) @ {pending['trigger']} (Fuel {int(fuel)})"
            )

        if pending and idx not in self.active_positions and self.is_within_trade_window(idx, now_ist):
            hhmm_now = now_ist.strftime("%H:%M")
            req_fuel = (
                MCX_EVENING_FUEL_SCORE
                if (is_mcx and hhmm_now >= "15:30")
                else MIN_FUEL_SCORE
            )
            dir_confirmed = (
                (pending["side"] == "CE" and price_change > 0 and oi_pcr_bull_ok and spot_close >= pending["trigger"])
                or (pending["side"] == "PE" and price_change < 0 and oi_pcr_bear_ok and spot_close <= pending["trigger"])
            )
            if dir_confirmed and fuel >= req_fuel:
                if is_mcx:
                    self.try_execute_mcx_fut_entry(idx, cfg, pending, df, spot_close, price_change, fuel, cum_pcr_diff, atr14, now_ist)
                else:
                    self.try_execute_option_entry(
                        idx, cfg, pending, df, spot_close, price_change, fuel, pcr, open_p, cum_pcr_diff,
                        chain, atr14, now_ist, active_expiry, is_today_exp
                    )

        short_status = f"Gann {sup:.1f}-{res:.1f} 👀" if spot_close < 1000 else f"Gann {int(sup)}-{int(res)} 👀"
        with self.pos_lock:
            pos = self.active_positions.get(idx)
        if pos:
            short_status = f"#{pos['trade_no']} IN TRADE 🔥"
        elif pending:
            short_status = f"ARMED {pending['display_side']} 🎯"
        elif price_change > 0:
            short_status = f"PRICE+ ({price_change:+.1f}) 🟢"
        elif price_change < 0:
            short_status = f"PRICE- ({price_change:+.1f}) 🔴"

        return {
            "idx": idx,
            "is_mcx": is_mcx,
            "spot": round(spot_close, 2) if spot_close < 1000 else int(round(spot_close)),
            "fuel": int(round(fuel)),
            "pcr": pcr,
            "cum_pcr_diff": cum_pcr_diff,
            "short_status": short_status,
        }

    def compute_swing_sl_and_rr(self, df: pd.DataFrame, side: str, spot_price: float, atr14: float) -> tuple:
        recent_3 = df.iloc[-3:]
        buffer = SWING_BUFFER_ATR_MULT * atr14
        min_spot_risk = 0.60 * atr14
        max_spot_risk = 1.50 * atr14

        if side == "CE":
            swing_low = float(recent_3["low"].min())
            raw_risk = spot_price - (swing_low - buffer)
            spot_risk = min(max_spot_risk, max(min_spot_risk, raw_risk))
            spot_sl = round(spot_price - spot_risk, 2)
            spot_trg = round(spot_price + spot_risk * RR_RATIO, 2)
        else:
            swing_high = float(recent_3["high"].max())
            raw_risk = (swing_high + buffer) - spot_price
            spot_risk = min(max_spot_risk, max(min_spot_risk, raw_risk))
            spot_sl = round(spot_price + spot_risk, 2)
            spot_trg = round(spot_price - spot_risk * RR_RATIO, 2)

        return round(spot_risk, 2), spot_sl, spot_trg

    def try_execute_mcx_fut_entry(self, idx, cfg, setup, df, fut_price, price_change, fuel, oi_change, atr14, now_ist):
        inst_key = cfg["spot_key"]
        lot_size = cfg["lot_size"]
        tick_sz = cfg.get("tick_size", 0.05)
        tx_side = "BUY" if setup["side"] == "CE" else "SELL"

        quotes = fetch_ltp_quote([inst_key])
        q_data = quotes.get(inst_key, {})
        ltp = float(q_data.get("last_price", fut_price))
        depth = q_data.get("depth", {})
        best_bid = float(depth.get("buy", [{}])[0].get("price", 0)) if depth.get("buy") else 0.0
        best_ask = float(depth.get("sell", [{}])[0].get("price", 0)) if depth.get("sell") else 0.0

        if best_bid > 0 and best_ask > 0 and ltp > 0:
            spread_pct = ((best_ask - best_bid) / ltp) * 100.0
            if spread_pct > MAX_SPREAD_PCT:
                log.info(f"[{idx}] Entry skipped due to high spread: {spread_pct:.2f}%")
                return

        spot_risk, _, _ = self.compute_swing_sl_and_rr(df, setup["side"], ltp, atr14)
        sl_points = max(round_to_tick(spot_risk, tick_sz), tick_sz * 4)

        risk_1_lot = sl_points * lot_size
        if risk_1_lot > MCX_MAX_RISK_INR:
            log.info(f"[{idx}] RISK_CAP_EXCEEDED: 1-Lot SL Risk ₹{risk_1_lot:.0f} > ₹{MCX_MAX_RISK_INR:.0f} cap. Skipping.")
            self.pending_setups[idx] = None
            return

        req_margin_1lot = fetch_required_margin(inst_key, 1, tx_side, "I")
        margin_lots = max(1, int(MCX_CAPITAL_PER_TRADE // req_margin_1lot)) if req_margin_1lot > 0 else 1
        risk_lots = max(1, int(MCX_MAX_RISK_INR // max(risk_1_lot, 1.0)))
        lots = min(margin_lots, risk_lots)
        qty = min(lots * lot_size, cfg["freeze_qty"])

        tp_points = round_to_tick(sl_points * RR_RATIO, tick_sz)

        if tx_side == "BUY":
            sl_price = round_to_tick(ltp - sl_points, tick_sz)
            tp_price = round_to_tick(ltp + tp_points, tick_sz)
        else:
            sl_price = round_to_tick(ltp + sl_points, tick_sz)
            tp_price = round_to_tick(ltp - tp_points, tick_sz)

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
                    "trade_no": t_no,
                    "type": "MCX_FUT",
                    "instrument_key": inst_key,
                    "strike": sym,
                    "side": setup["side"],
                    "tx_side": tx_side,
                    "display_side": tx_side,
                    "qty": qty,
                    "tick_size": tick_sz,
                    "delta": 1.0,
                    "entry_price": fill,
                    "spot_entry": fut_price,
                    "spot_sl": sl_price,
                    "spot_t1": tp_price,
                    "gann_level": setup["gann_level"],
                    "last_ltp": fill,
                    "sl": sl_price,
                    "tp": tp_price,
                    "peak_gain": 0.0,
                    "entry_ts": now_ts,
                    "last_peak_ts": now_ts,
                    "trail_stage": None,
                    "low_fuel_streak": 0,
                    "is_expiry_day": False,
                    "ltp_window": deque([(now_ts, fill)]),
                    "setup_name": setup["name"],
                    "entry_time": entry_time_str,
                }
            self.pending_setups[idx] = None
            self.subscribe_tick_instrument(inst_key)
            self.save_active_positions_state()
            entry_msg = (
                f"🛢️🚀 <b>NEW ENTRY • TRADE #{t_no} [{idx}]</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Contract:</b> <code>{sym}</code> (<b>{tx_side}</b>) │ 🕒 <code>{entry_time_str[:5]} IST</code>\n"
                f"📦 <b>Qty:</b> {qty} ({lots} Lot) │ <b>Entry:</b> <b>₹{fill:.2f}</b>\n"
                f"🛡️ <b>Swing SL:</b> ₹{sl_price:.2f} (<code>-{sl_points:.2f}p</code>) │ 🎯 <b>Target (1:2):</b> ₹{tp_price:.2f} (<code>+{tp_points:.2f}p</code>)\n"
                f"📐 <b>Setup:</b> {setup['name']} │ 🔥 <b>Fuel:</b> {fuel} │ 📊 <b>ΔOI:</b> {oi_change:+.0f}"
            )
            send_trade_alert_with_chart(
                df,
                title=f"{idx} 5M ENTRY │ #{t_no} {sym} {tx_side} (RR 1:2)",
                caption=entry_msg,
                gann_lvl=setup["gann_level"],
                spot_entry=fut_price,
                spot_sl=sl_price,
                spot_trg=tp_price,
            )
            self.render_live_dashboard(now_ist.strftime("%H:%M"))

    def try_execute_option_entry(self, idx, cfg, setup, df, spot_close, price_change, fuel, pcr, open_p, cum_pcr_diff, chain, atr14, now_ist, active_expiry, is_today_exp):
        if not chain:
            return
        step = cfg.get("step", 50)
        tick_sz = cfg.get("tick_size", 0.05)
        atm_strike = round(spot_close / step) * step
        opt_field = "call_options" if setup["side"] == "CE" else "put_options"

        chosen = None
        for row in chain:
            if row.get("strike_price") == atm_strike:
                chosen = row.get(opt_field, {})
                break
        if not chosen:
            return

        inst_key = chosen.get("instrument_key")
        mkt = chosen.get("market_data", {})
        greeks = chosen.get("option_greeks", {})
        ltp = float(mkt.get("ltp", 0))
        bid = float(mkt.get("bid_price", 0))
        ask = float(mkt.get("ask_price", 0))

        raw_delta = abs(float(greeks.get("delta", 0.0)))
        live_delta = min(0.75, max(0.30, raw_delta)) if raw_delta > 0 else 0.50

        if ltp <= 5.0:
            return

        if bid > 0 and ask > 0:
            spread_pct = ((ask - bid) / ltp) * 100.0
            if spread_pct > MAX_SPREAD_PCT:
                log.info(f"[{idx}] Entry skipped due to high spread: {spread_pct:.2f}%")
                return

        lots = max(1, int(CAPITAL_PER_TRADE // (ltp * cfg["lot_size"])))
        qty = min(lots * cfg["lot_size"], cfg["freeze_qty"])

        spot_risk, spot_sl_equiv, spot_t1_equiv = self.compute_swing_sl_and_rr(
            df, setup["side"], spot_close, atr14
        )
        raw_opt_sl = max(spot_risk * live_delta, ltp * 0.08)
        raw_opt_sl = min(raw_opt_sl, ltp * 0.35)
        sl_points = max(round_to_tick(raw_opt_sl, tick_sz), tick_sz * 4)
        tp_points = round_to_tick(sl_points * RR_RATIO, tick_sz)

        sl_price = round_to_tick(ltp - sl_points, tick_sz)
        tp_price = round_to_tick(ltp + tp_points, tick_sz)

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
                    "trade_no": t_no,
                    "type": "INDEX_OPT",
                    "instrument_key": inst_key,
                    "strike": atm_strike,
                    "side": setup["side"],
                    "tx_side": "BUY",
                    "display_side": setup["side"],
                    "qty": qty,
                    "tick_size": tick_sz,
                    "delta": live_delta,
                    "entry_price": fill,
                    "spot_entry": spot_close,
                    "spot_sl": spot_sl_equiv,
                    "spot_t1": spot_t1_equiv,
                    "gann_level": setup["gann_level"],
                    "last_ltp": fill,
                    "sl": sl_price,
                    "tp": tp_price,
                    "peak_gain": 0.0,
                    "entry_ts": now_ts,
                    "last_peak_ts": now_ts,
                    "trail_stage": None,
                    "low_fuel_streak": 0,
                    "is_expiry_day": is_today_exp,
                    "ltp_window": deque([(now_ts, fill)]),
                    "setup_name": setup["name"],
                    "entry_time": entry_time_str,
                }
            self.pending_setups[idx] = None
            self.subscribe_tick_instrument(inst_key)
            self.save_active_positions_state()
            entry_msg = (
                f"🚀 <b>NEW ENTRY • TRADE #{t_no} [{idx}]</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Strike:</b> <code>{atm_strike} {setup['side']}</code> ({exp_label}) │ 🕒 <code>{entry_time_str[:5]} IST</code>\n"
                f"📦 <b>Qty:</b> {qty} ({lots} Lot) │ <b>Entry:</b> <b>₹{fill:.2f}</b> (Δ {live_delta:.2f})\n"
                f"🛡️ <b>Swing SL:</b> ₹{sl_price:.2f} (<code>-{sl_points:.2f}p</code>) │ 🎯 <b>Target (1:2):</b> ₹{tp_price:.2f} (<code>+{tp_points:.2f}p</code>)\n"
                f"📐 <b>Setup:</b> {setup['name']} │ 🔥 <b>Fuel:</b> {fuel} │ 📊 <b>PCR:</b> {pcr}"
            )
            send_trade_alert_with_chart(
                df,
                title=f"{idx} 5M ENTRY │ #{t_no} {atm_strike} {setup['side']} (RR 1:2)",
                caption=entry_msg,
                gann_lvl=setup["gann_level"],
                spot_entry=spot_close,
                spot_sl=spot_sl_equiv,
                spot_trg=spot_t1_equiv,
            )
            self.render_live_dashboard(now_ist.strftime("%H:%M"))

    def manage_open_positions(self, now_ist: datetime):
        with self.pos_lock:
            if not self.active_positions:
                return
            pos_items = list(self.active_positions.items())

        hhmm = now_ist.strftime("%H:%M")
        quotes = {}
        if not self.ws_connected:
            keys = [p["instrument_key"] for _, p in pos_items]
            quotes = fetch_ltp_quote(keys)

        for idx, pos in pos_items:
            if not self.ws_connected:
                q_data = quotes.get(pos["instrument_key"], {})
                ltp = float(q_data.get("last_price", pos.get("last_ltp", pos["entry_price"])))
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
            elif not is_short and ltp >= pos["tp"]:
                exit_reason = "TARGET_1_HIT"
            elif is_short and ltp >= pos["sl"]:
                exit_reason = "TRAIL_SL_HIT" if pos.get("trail_stage") else "STOP_LOSS_HIT"
            elif is_short and ltp <= pos["tp"]:
                exit_reason = "TARGET_1_HIT"

            if exit_reason:
                self.close_position_now(idx, ltp, exit_reason, now_ist)

    def shutdown_cleanly(self, now_ist: datetime, reason_label: str):
        """Saves any open positions to JSON and updates pinned dashboard with 🔴 BOT STOPPED."""
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
        notify(
            f"🔴 <b>BOT STOPPED ({reason_label})</b> │ Open Carried: <b>{open_cnt}</b> │ "
            f"Closed Today: <b>{len(self.closed_trades)}</b> │ Day Net PnL: <b>₹{self.day_pnl:+,.2f}</b>"
        )

    def run(self):
        if not UPSTOX_ACCESS_TOKEN:
            log.error("UPSTOX_ACCESS_TOKEN missing! Exiting.")
            sys.exit(1)

        def _handle_sigterm(signum, frame):
            self.stop_requested = True

        signal.signal(signal.SIGTERM, _handle_sigterm)
        signal.signal(signal.SIGINT, _handle_sigterm)

        self.load_today_state_from_journal()
        self.init_contracts()
        self.load_active_positions_state()
        self.bot_status_text = "🟢 BOT LIVE • RUNNING"
        session_start_ts = time.time()

        with self.pos_lock:
            carried_cnt = len(self.active_positions)

        notify(
            f"🟢 <b>V3 QUANT ENGINE LIVE</b> ({'PAPER' if PAPER_MODE else 'LIVE'})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📡 <b>5 Indices:</b> {INDEX_START_TIME}–{INDEX_END_TIME} IST (Fuel ≥{int(MIN_FUEL_SCORE)})\n"
            f"🛢️ <b>5 MCX Futures:</b> Crude, NG, Gold, Silver, Copper (Eve Fuel ≥{int(MCX_EVENING_FUEL_SCORE)} │ Risk Cap ₹{int(MCX_MAX_RISK_INR)})\n"
            f"🔄 <b>Handover:</b> {carried_cnt} Active Carried │ Singleton Lock │ RR 1:2"
        )

        while True:
            now_ist = datetime.now(IST)
            hhmm = now_ist.strftime("%H:%M")

            if self.stop_requested:
                self.shutdown_cleanly(now_ist, "MANUAL STOP")
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
            risk_locked = (
                self.day_pnl >= max_profit
                or self.day_pnl <= max_loss
                or self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES
            )

            if risk_locked:
                self.bot_status_text = "🟡 BOT PAUSED • RISK LIMIT"
            else:
                self.bot_status_text = "🟢 BOT LIVE • RUNNING"
                for idx, cfg in INDICES.items():
                    if cfg["type"] == "INDEX_OPT" and hhmm > "15:20":
                        if idx not in self.last_summaries:
                            spot_df = fetch_intraday_5m(cfg["spot_key"])
                            if not spot_df.empty:
                                last_cl = float(spot_df["close"].iloc[-1])
                                self.last_summaries[idx] = {
                                    "idx": idx,
                                    "is_mcx": False,
                                    "spot": int(round(last_cl)),
                                    "fuel": 0,
                                    "pcr": 0.0,
                                    "cum_pcr_diff": 0.0,
                                    "short_status": "CLOSED 🏁",
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
