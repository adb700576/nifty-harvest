import os
import io
import csv
import sys
import gzip
import json
import time
import math
import signal
import threading
import logging
import requests
import pandas as pd
import numpy as np
from collections import deque
from datetime import datetime, timedelta, timezone

try:
    import upstox_client
    HAS_UPSTOX_SDK = True
except ImportError:
    HAS_UPSTOX_SDK = False

# ==============================================================================
# 1. CONFIGURATION & GOOGLE SHEETS SYNC
# ==============================================================================
IST = timezone(timedelta(hours=5, minutes=30))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("V3_MULTI_ASSET_ENGINE")

UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GSHEET_WEBHOOK_URL = os.getenv("GSHEET_WEBHOOK_URL", "").strip()
PAPER_MODE = os.getenv("PAPER_MODE", "true").strip().lower() == "true"

# Session & Prime Time Slots (IST)
SESSION_MAX_RUNTIME_SEC = 20700
INDEX_WINDOWS = (("09:10", "15:00"),)
INDEX_EXIT_TIME = "15:15"

MCX_WINDOWS = (("21:00", "22:00"),)
MCX_EXIT_TIME = "23:15"

# Equity Cash Session Controls (IST) — Strict Top 5 Stocks & Max 5 Equity Trades/Day
EQ_ENTRY_START = "09:30"
EQ_ORB_ATR_CUTOFF = "13:30"
EQ_GANN_CUTOFF = "15:00"
EQ_EXIT_TIME = "15:15"
EQ_MAX_RISK_INR = 500.0
EQ_MIN_FUEL_SCORE = 50.0
EQ_TOP_N_STOCKS = 5
EQ_MAX_TRADES_PER_DAY = 5
EQ_MIN_RR = 2.0
EQ_SCAN_INTERVAL_SEC = 20.0

REENTRY_COOLDOWN_SEC = 300
STARTUP_WARMUP_SEC = 30
DASHBOARD_PULSE_SEC = 6.0

# F&O Strategy 1: Trend-Following Breakout + Retest (1-Minute Candles)
BO_VOL_RATIO_MIN = 1.20
BO_RANGE_EXPANSION_ATR = 1.20
BO_BODY_RATIO_MIN = 0.45
BO_RETEST_BUFFER_ATR = 0.30
BO_RETEST_MAX_BARS = 15
BO_RRR_TARGET = 1.50

# F&O Strategy 2: Reversed / Contrarian EMA Trend (5-Minute Closed Candles)
REV_VOL_RATIO_MIN = 1.30
REV_BODY_RATIO_MIN = 0.50
REV_RRR_TARGET = 2.00

# F&O Strategy 3: Reversed VWAP + PCR + Candlestick Pattern
VWAP_MIN_VOL_RATIO = 1.20
VWAP_MIN_BODY_RATIO = 0.50
VWAP_TOUCH_BAND_ATR = 0.30
SWING_BUFFER_ATR = 0.15
MIN_PCR_STEP_DIFF = 0.02

ATR_SL_MULT = 1.20

JOURNAL_FILE = "live_journal/trade_journal.csv"
ACTIVE_STATE_FILE = "live_journal/active_positions.json"
DASHBOARD_STATE_FILE = "live_journal/dashboard_state_v4.json"
LOCK_FILE = "live_journal/engine.lock"

JOURNAL_FIELDNAMES = [
    "trade_no", "date", "entry_time", "exit_time", "symbol", "contract", "side",
    "qty", "pnl_units", "entry_price", "exit_price", "points", "sl", "target", "exit_reason",
    "gross_pnl", "brokerage", "taxes_other", "charges", "pnl", "day_pnl", "setup",
]

ALL_STRATEGY_KEYS = (
    "1M_BO_RETEST",
    "5M_CONTRARIAN",
    "VWAP_PCR_REV",
    "EQ_GANN_15M",
    "EQ_ATR_RANGE",
    "EQ_MOTHER_ORB",
)

INDICES = {
    "NIFTY":      {"type": "INDEX_OPT", "spot_key": "NSE_INDEX|Nifty 50", "min_sl_pts": 10.0, "default_delta": 0.52},
    "BANKNIFTY":  {"type": "INDEX_OPT", "spot_key": "NSE_INDEX|Nifty Bank", "min_sl_pts": 20.0, "default_delta": 0.52},
    "FINNIFTY":   {"type": "INDEX_OPT", "spot_key": "NSE_INDEX|Nifty Fin Service", "min_sl_pts": 10.0, "default_delta": 0.52},
    "MIDCPNIFTY": {"type": "INDEX_OPT", "spot_key": "NSE_INDEX|NIFTY MID SELECT", "min_sl_pts": 8.0,  "default_delta": 0.50},
    "SENSEX":     {"type": "INDEX_OPT", "spot_key": "BSE_INDEX|SENSEX", "min_sl_pts": 25.0, "default_delta": 0.52},
    "CRUDEOIL":   {"type": "MCX_FUT"},
    "NATURALGAS": {"type": "MCX_FUT"},
    "GOLD":       {"type": "MCX_FUT"},
    "SILVER":     {"type": "MCX_FUT"},
    "COPPER":     {"type": "MCX_FUT"},
}

MCX_MINI_SYMBOLS = {
    "CRUDEOIL":   "CRUDEOILM",
    "NATURALGAS": "NATGASMINI",
    "GOLD":       "GOLDM",
    "SILVER":     "SILVERM",
    "COPPER":     "COPPER",
}

EQUITY_UNIVERSE = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "GRASIM",
    "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO", "HINDALCO",
    "HINDUNILVR", "ICICIBANK", "INDUSINDBK", "INFY", "ITC",
    "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M",
    "MARUTI", "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
    "SUNPHARMA", "TATACONSUM", "TATASTEEL", "TCS", "TECHM",
    "TITAN", "TRENT", "ULTRACEMCO",
]

_MCX_BOD_CACHE = None


def round_to_tick(price: float, tick_size: float) -> float:
    ts = tick_size if tick_size and tick_size > 0 else 0.05
    decimals = max(0, len(f"{ts:.10f}".rstrip("0").split(".")[1])) if "." in f"{ts:.10f}".rstrip("0") else 0
    return round(round(price / ts) * ts, decimals)


def is_scheduled_handover_window(now_ist: datetime) -> bool:
    hhmm = now_ist.strftime("%H:%M")
    return ("14:35" <= hhmm <= "15:05") or ("20:30" <= hhmm <= "21:05")


def is_overnight_dead_hours(now_ist: datetime) -> bool:
    hhmm = now_ist.strftime("%H:%M")
    return hhmm >= "23:36" or hhmm < "08:30"


def ms_to_ist_hhmm(epoch_ms) -> str:
    try:
        val = int(epoch_ms)
        if val <= 0:
            return ""
        dt = datetime.fromtimestamp(val / 1000.0, tz=timezone.utc).astimezone(IST)
        return dt.strftime("%H:%M")
    except Exception:
        return ""


def map_setup_to_strategy(setup_str: str) -> str:
    s = str(setup_str or "").upper()
    if "EQ_GANN" in s:
        return "EQ_GANN_15M"
    if "EQ_ATR" in s:
        return "EQ_ATR_RANGE"
    if "EQ_MOTHER" in s or "EQ_ORB" in s:
        return "EQ_MOTHER_ORB"
    if "VWAP_PCR_REV" in s or "REV_VWAP" in s:
        return "VWAP_PCR_REV"
    if "5M_CONTRARIAN" in s or "CONTRARIAN_REV" in s:
        return "5M_CONTRARIAN"
    return "1M_BO_RETEST"


def inspect_special_session_schedule(now_ist: datetime) -> dict:
    d_str = now_ist.strftime("%Y-%m-%d")
    info = {
        "is_holiday": False,
        "description": "",
        "nse_open": True,
        "nse_start": INDEX_WINDOWS[0][0],
        "nse_end": INDEX_WINDOWS[-1][1],
        "nse_exit": INDEX_EXIT_TIME,
        "mcx_open": True,
        "mcx_start": MCX_WINDOWS[0][0],
        "mcx_end": MCX_WINDOWS[-1][1],
        "mcx_exit": MCX_EXIT_TIME,
        "is_muhurat": False,
    }

    try:
        res = requests.get(
            f"https://api.upstox.com/v2/market/holidays/{d_str}",
            headers=upstox_headers(),
            timeout=8,
        ).json()
        data = res.get("data", [])
        if isinstance(data, list) and data:
            item = data[0]
            info["is_holiday"] = True
            info["description"] = str(item.get("description", "EXCHANGE HOLIDAY")).strip()
            closed_ex = {str(x).upper() for x in item.get("closed_exchanges", [])}
            open_ex = item.get("open_exchanges", [])

            if "NSE" in closed_ex or "NFO" in closed_ex:
                info["nse_open"] = False
            if "MCX" in closed_ex:
                info["mcx_open"] = False

            for o in open_ex:
                if not isinstance(o, dict):
                    continue
                ex = str(o.get("exchange", "")).upper()
                st_hhmm = ms_to_ist_hhmm(o.get("start_time"))
                en_hhmm = ms_to_ist_hhmm(o.get("end_time"))

                if ex in ("NSE", "NFO"):
                    info["nse_open"] = True
                    if st_hhmm and en_hhmm:
                        info["nse_start"] = st_hhmm
                        en_dt = datetime.strptime(f"{d_str} {en_hhmm}", "%Y-%m-%d %H:%M")
                        info["nse_end"] = (en_dt - timedelta(minutes=8)).strftime("%H:%M")
                        info["nse_exit"] = (en_dt - timedelta(minutes=3)).strftime("%H:%M")
                        if st_hhmm >= "16:00":
                            info["is_muhurat"] = True
                elif ex == "MCX":
                    info["mcx_open"] = True
                    if st_hhmm and en_hhmm:
                        info["mcx_start"] = st_hhmm
                        en_dt = datetime.strptime(f"{d_str} {en_hhmm}", "%Y-%m-%d %H:%M")
                        info["mcx_end"] = (en_dt - timedelta(minutes=35)).strftime("%H:%M")
                        info["mcx_exit"] = (en_dt - timedelta(minutes=15)).strftime("%H:%M")
                        if st_hhmm >= "16:30" and "MUHURAT" in info["description"].upper():
                            info["is_muhurat"] = True
    except Exception as e:
        log.warning(f"Holiday/Muhurat schedule check warning: {e}")

    if info["is_holiday"] and not info["mcx_open"]:
        mcx_live = check_live_exchange_status("MCX")
        if mcx_live in ("NORMAL_OPEN", "OPEN", "PRE_OPEN"):
            info["mcx_open"] = True
            info["mcx_start"] = MCX_WINDOWS[0][0]

    return info


def push_trade_to_google_sheet(trade_row: dict):
    if not GSHEET_WEBHOOK_URL:
        return
    try:
        requests.post(
            GSHEET_WEBHOOK_URL,
            json={"action": "LOG_TRADE", "trade": trade_row},
            timeout=8,
        )
        log.info(f"[GSHEET] Synced Trade #{trade_row['trade_no']} ({trade_row['setup']}) to Google Sheets.")
    except Exception as e:
        log.warning(f"[GSHEET] Sync warning: {e}")


def _normalize_date_str(val) -> str:
    s = str(val or "").strip()
    if not s:
        return ""
    if "T" in s:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(IST)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return s[:10]
    return s[:10]


def _normalize_time_str(val) -> str:
    s = str(val or "").strip()
    if not s:
        return ""
    if "T" in s:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(IST)
            return dt.strftime("%H:%M:%S")
        except Exception:
            return s[11:19]
    return s


def restore_journal_from_google_sheet():
    if not GSHEET_WEBHOOK_URL:
        return
    try:
        resp = requests.get(GSHEET_WEBHOOK_URL, params={"action": "GET_TRADES"}, timeout=12).json()
        sheet_trades = resp.get("trades", []) if isinstance(resp, dict) else []
        if not sheet_trades:
            return

        os.makedirs(os.path.dirname(JOURNAL_FILE), exist_ok=True)
        existing_rows = []
        seen_keys = set()

        if os.path.isfile(JOURNAL_FILE):
            with open(JOURNAL_FILE, mode="r", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    d_str = _normalize_date_str(r.get("date"))
                    t_no = int(float(r.get("trade_no", 0) or 0))
                    key = (d_str, t_no, str(r.get("symbol", "")).strip(), str(r.get("entry_time", "")).strip())
                    seen_keys.add(key)
                    existing_rows.append(r)

        added_count = 0
        for st in sheet_trades:
            d_str = _normalize_date_str(st.get("date"))
            if not d_str:
                continue
            t_no = int(float(st.get("trade_no", 0) or 0))
            sym = str(st.get("symbol", "")).strip()
            e_time = _normalize_time_str(st.get("entry_time"))
            x_time = _normalize_time_str(st.get("exit_time"))
            key = (d_str, t_no, sym, e_time)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            qty_v = int(float(st.get("qty", 0) or 0))
            units_v = int(float(st.get("pnl_units") or qty_v))
            clean_row = {
                "trade_no": t_no, "date": d_str, "entry_time": e_time, "exit_time": x_time,
                "symbol": sym, "contract": str(st.get("contract", "")).strip(),
                "side": str(st.get("side", "")).strip(), "qty": qty_v, "pnl_units": units_v,
                "entry_price": float(st.get("entry_price", 0.0) or 0.0),
                "exit_price": float(st.get("exit_price", 0.0) or 0.0),
                "points": float(st.get("points", 0.0) or 0.0),
                "sl": float(st.get("sl", 0.0) or 0.0), "target": float(st.get("target", 0.0) or 0.0),
                "exit_reason": str(st.get("exit_reason", "")).strip(),
                "gross_pnl": float(st.get("gross_pnl", 0.0) or 0.0),
                "brokerage": float(st.get("brokerage", 0.0) or 0.0),
                "taxes_other": float(st.get("taxes_other", 0.0) or 0.0),
                "charges": float(st.get("charges", 0.0) or 0.0),
                "pnl": float(st.get("pnl", 0.0) or 0.0),
                "day_pnl": float(st.get("day_pnl", 0.0) or 0.0),
                "setup": str(st.get("setup", "")).strip(),
            }
            existing_rows.append(clean_row)
            added_count += 1

        if added_count > 0:
            existing_rows.sort(key=lambda r: (_normalize_date_str(r.get("date")), int(float(r.get("trade_no", 0) or 0))))
            with open(JOURNAL_FILE, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=JOURNAL_FIELDNAMES)
                writer.writeheader()
                for r in existing_rows:
                    writer.writerow({k: r.get(k, "") for k in JOURNAL_FIELDNAMES})
            log.info(f"[GSHEET RESTORE] Restored {added_count} trade(s) from Google Sheets into {JOURNAL_FILE}.")
    except Exception as e:
        log.warning(f"[GSHEET RESTORE] Sync skipped: {e}")


# ==============================================================================
# 2. 4-PINNED TELEGRAM DASHBOARDS (MASTER + INDEX + MCX + EQUITY) & CHARGES
# ==============================================================================
def notify(msg: str, silent: bool = False):
    clean_lines = [line.strip() for line in msg.splitlines() if line.strip()]
    log.info(f"[MIRROR] {' | '.join(clean_lines)}")

    if is_overnight_dead_hours(datetime.now(IST)) or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
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


def load_saved_dashboard_ids() -> dict:
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    default_ids = {"master": None, "index": None, "mcx": None, "equity": None}
    if os.path.isfile(DASHBOARD_STATE_FILE):
        try:
            with open(DASHBOARD_STATE_FILE, "r", encoding="utf-8") as f:
                st = json.load(f)
            if st.get("date") == today_str and isinstance(st.get("ids"), dict):
                for k in default_ids:
                    if st["ids"].get(k):
                        default_ids[k] = int(st["ids"][k])
        except Exception:
            pass
    return default_ids


def save_dashboard_ids(ids_map: dict):
    os.makedirs("live_journal", exist_ok=True)
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        with open(DASHBOARD_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": today_str, "ids": ids_map}, f)
    except Exception:
        pass


def _safe_telegram_html(text: str, max_len: int = 3950) -> str:
    if len(text) <= max_len:
        return text
    lines = text.splitlines()
    out = []
    curr_len = 0
    for ln in lines:
        if curr_len + len(ln) + 1 > max_len - 50:
            out.append("<i>... (more items in journal) ...</i>")
            break
        out.append(ln)
        curr_len += len(ln) + 1
    return "\n".join(out)


def upsert_telegram_dashboard(text: str, message_id: int = None) -> int:
    if is_overnight_dead_hours(datetime.now(IST)) or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return message_id

    safe_text = _safe_telegram_html(text)

    if message_id:
        edit_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id, "text": safe_text, "parse_mode": "HTML"}
        try:
            resp = requests.post(edit_url, json=payload, timeout=8).json()
            desc = str(resp.get("description", "")).lower()
            if resp.get("ok") or "message is not modified" in desc:
                return message_id
        except Exception:
            pass

    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": safe_text, "parse_mode": "HTML", "disable_notification": True}
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


def check_live_exchange_status(exchange: str = "NSE") -> str:
    try:
        res = requests.get(
            f"https://api.upstox.com/v2/market/status/{exchange}",
            headers=upstox_headers(),
            timeout=8,
        ).json()
        return str(res.get("data", {}).get("status", "")).upper()
    except Exception:
        return ""


def fetch_exact_trade_charges(instrument_key: str, buy_price: float, sell_price: float, qty: int, seg_type: str) -> dict:
    is_mcx = seg_type == "MCX_FUT"
    is_eq = seg_type == "EQUITY_CASH"

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

    buy_val = buy_price * qty
    sell_val = sell_price * qty
    turnover = buy_val + sell_val

    if is_eq:
        brokerage = round(min(20.0, buy_val * 0.0005) + min(20.0, sell_val * 0.0005), 2)
        stt_ctt = sell_val * 0.00025
        txn_charges = turnover * 0.0000297
        stamp_duty = buy_val * 0.00003
    elif is_mcx:
        brokerage = 40.0
        stt_ctt = sell_val * 0.0001
        txn_charges = turnover * 0.000021
        stamp_duty = buy_val * 0.00002
    else:
        brokerage = 40.0
        stt_ctt = sell_val * 0.000625
        txn_charges = turnover * 0.0005
        stamp_duty = buy_val * 0.00003

    sebi_charges = turnover * 0.000001
    gst = (brokerage + txn_charges + sebi_charges) * 0.18
    taxes_other = round(stt_ctt + txn_charges + stamp_duty + sebi_charges + gst, 2)
    return {"brokerage": brokerage, "taxes_other": taxes_other, "total": round(brokerage + taxes_other, 2)}


def record_trade_journal(row: dict):
    os.makedirs(os.path.dirname(JOURNAL_FILE), exist_ok=True)
    file_exists = os.path.isfile(JOURNAL_FILE)
    try:
        with open(JOURNAL_FILE, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=JOURNAL_FIELDNAMES)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
        log.info(f"[JOURNAL] Saved #{row['trade_no']} [{row['setup']}]: {row['contract']} Net PnL ₹{row['pnl']:+.2f}")
    except Exception as e:
        log.error(f"Journal write error: {e}")

    push_trade_to_google_sheet(row)


# ==============================================================================
# 3. UPSTOX API HELPERS (MCX MASTER + NSE EQUITY MASTER + FUTURES + OPTIONS)
# ==============================================================================
def load_upstox_mcx_master() -> list:
    global _MCX_BOD_CACHE
    if _MCX_BOD_CACHE is not None:
        return _MCX_BOD_CACHE
    url = "https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            with gzip.GzipFile(fileobj=io.BytesIO(resp.content)) as gz:
                raw_list = json.loads(gz.read().decode("utf-8"))
            _MCX_BOD_CACHE = [
                item for item in raw_list
                if str(item.get("segment", "")).upper() == "MCX_FO"
                and str(item.get("instrument_type", "")).upper() == "FUT"
            ]
            return _MCX_BOD_CACHE
    except Exception as e:
        log.warning(f"[MCX BOD MASTER] Fallback to Search API ({e})")
    _MCX_BOD_CACHE = []
    return _MCX_BOD_CACHE


def resolve_equity_instruments(symbols: list) -> dict:
    url = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
    try:
        resp = requests.get(url, timeout=25)
        if resp.status_code == 200:
            with gzip.GzipFile(fileobj=io.BytesIO(resp.content)) as gz:
                raw_list = json.loads(gz.read().decode("utf-8"))
            df = pd.DataFrame(raw_list)
            x = df[df["segment"].astype(str).str.upper().str.contains("NSE_EQ", na=False)]
            resolved = {}
            for sym in symbols:
                m = x[x["trading_symbol"].astype(str).str.upper().eq(sym.upper())]
                if not m.empty:
                    resolved[sym] = str(m.iloc[0]["instrument_key"])
            log.info(f"[EQUITY MASTER] Resolved {len(resolved)}/{len(symbols)} NSE Equity stocks.")
            return resolved
    except Exception as e:
        log.warning(f"[EQUITY MASTER] Master download warning: {e}")
    return {}


def _norm_contract_expiry(exp_val) -> str:
    if isinstance(exp_val, (int, float)) and exp_val > 100000:
        return datetime.fromtimestamp(exp_val / 1000.0, tz=timezone.utc).astimezone(IST).strftime("%Y-%m-%d")
    return str(exp_val or "9999-12-31")[:10]


def get_active_future_contract(idx: str) -> dict:
    is_mcx = INDICES[idx]["type"] == "MCX_FUT"
    target_sym = MCX_MINI_SYMBOLS.get(idx, idx) if is_mcx else idx
    today_date = datetime.now(IST).date()
    safe_min_expiry = (today_date + timedelta(days=7)).strftime("%Y-%m-%d")

    all_candidates = []
    if is_mcx:
        for item in load_upstox_mcx_master():
            name_u = str(item.get("name", "")).upper()
            tsym_u = str(item.get("trading_symbol", "")).upper()
            asset_u = str(item.get("asset_symbol", "")).upper()
            if asset_u == target_sym or name_u == target_sym or tsym_u.startswith(f"{target_sym} FUT"):
                all_candidates.append(item)

    if not all_candidates:
        exchange = "BSE" if idx == "SENSEX" else ("MCX" if is_mcx else "NSE")
        url = "https://api.upstox.com/v2/instruments/search"
        for expiry_kw in ("current_month", "next_month"):
            try:
                params = {
                    "query": target_sym,
                    "exchanges": exchange,
                    "segments": "FUT",
                    "expiry": expiry_kw,
                    "page_number": 1,
                    "records": 30,
                }
                items = requests.get(url, headers=upstox_headers(), params=params, timeout=10).json().get("data", [])
                for it in items:
                    if str(it.get("instrument_type", "")).upper() != "FUT":
                        continue
                    tsym_u = str(it.get("trading_symbol", "")).upper()
                    if tsym_u.startswith(f"{target_sym} FUT"):
                        all_candidates.append(it)
            except Exception as e:
                log.warning(f"[{idx}] Instrument search error ({expiry_kw}): {e}")

    if not all_candidates:
        return {}

    dedup = {}
    for c in all_candidates:
        ik = str(c.get("instrument_key", ""))
        if ik:
            dedup[ik] = c

    sorted_cands = sorted(dedup.values(), key=lambda x: _norm_contract_expiry(x.get("expiry")))
    liquid_cands = [c for c in sorted_cands if _norm_contract_expiry(c.get("expiry")) >= safe_min_expiry]
    top = liquid_cands[0] if liquid_cands else sorted_cands[0]

    raw_lot = int(top.get("lot_size", 1) or 1)
    min_lot = int(top.get("minimum_lot", 1) or 1)
    order_lot_size = raw_lot if raw_lot > 1 else (min_lot if min_lot > 1 else 1)

    quote_unit = str(top.get("price_quote_unit", "")).upper()
    tsym_top = str(top.get("trading_symbol", "")).upper()
    quote_denom = 1
    if "10 GRM" in quote_unit or "10GM" in quote_unit or "10 G" in quote_unit or (tsym_top.startswith("GOLDM") and order_lot_size == 100):
        quote_denom = 10
    elif "100 GRM" in quote_unit or "100GM" in quote_unit:
        quote_denom = 100
    pnl_per_lot_pt = max(1, order_lot_size // quote_denom)

    raw_tick = float(top.get("tick_size", 0.05) or 0.05)
    tick_size = round(raw_tick / 100.0, 4) if raw_tick >= 5.0 else raw_tick

    return {
        "instrument_key": str(top.get("instrument_key", "")),
        "lot_size": order_lot_size,
        "pnl_multiplier": pnl_per_lot_pt,
        "minimum_lot": min_lot,
        "freeze_quantity": int(top.get("freeze_quantity", 10000) or 10000),
        "tick_size": tick_size if tick_size > 0 else 0.05,
        "trading_symbol": str(top.get("trading_symbol", target_sym)),
        "expiry": _norm_contract_expiry(top.get("expiry")),
    }


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


def fetch_intraday_candles(instrument_key: str, interval_min: int = 1) -> pd.DataFrame:
    if not instrument_key:
        return pd.DataFrame()
    encoded_key = requests.utils.quote(instrument_key, safe="")
    url = f"https://api.upstox.com/v3/historical-candle/intraday/{encoded_key}/minutes/{int(interval_min)}"
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
    normalized = {}
    for i in range(0, len(instrument_keys), 100):
        chunk = instrument_keys[i:i + 100]
        try:
            raw_data = requests.get(
                "https://api.upstox.com/v2/market-quote/quotes",
                headers=upstox_headers(),
                params={"instrument_key": ",".join(chunk)},
                timeout=10,
            ).json().get("data", {})
            for k, v in raw_data.items():
                normalized[k] = v
                normalized[k.replace(":", "|")] = v
                if v.get("instrument_token"):
                    normalized[v["instrument_token"]] = v
        except Exception:
            pass
    return normalized


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
# 4. TECHNICAL INDICATORS & EQUITY GANN / ATR / MOTHER CANDLE HELPERS
# ==============================================================================
def add_full_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    x = df.copy()
    x["ema20"] = x["close"].ewm(span=20, adjust=False).mean()
    x["ema50"] = x["close"].ewm(span=50, adjust=False).mean()

    prev_close = x["close"].shift(1).fillna(x["open"])
    tr = pd.concat([
        x["high"] - x["low"],
        (x["high"] - prev_close).abs(),
        (x["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    x["tr"] = tr
    x["atr14"] = tr.rolling(14, min_periods=1).mean()

    tp = (x["high"] + x["low"] + x["close"]) / 3.0
    day_ser = x["timestamp"].dt.date
    vol_safe = x["volume"].replace(0, 1.0)
    x["vwap"] = (tp * vol_safe).groupby(day_ser).cumsum() / vol_safe.groupby(day_ser).cumsum()

    vol_sma20 = x["volume"].rolling(20, min_periods=1).mean().replace(0, np.nan)
    x["vol_ratio"] = (x["volume"] / vol_sma20).fillna(1.0)

    c_range = (x["high"] - x["low"]).replace(0, 1e-6)
    x["body_ratio"] = (x["close"] - x["open"]).abs() / c_range
    return x


def build_resampled_15m_df(df_1m: pd.DataFrame) -> pd.DataFrame:
    if df_1m.empty or len(df_1m) < 15:
        return pd.DataFrame()
    tmp = df_1m.copy().set_index("timestamp")
    df_15m = tmp.resample("15min").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna().reset_index()
    if df_15m.empty:
        return df_15m
    x = add_full_indicators(df_15m)
    vol_base = x["volume"].shift(1).rolling(5, min_periods=1).median().replace(0, np.nan)
    vol_ratio = (x["volume"] / vol_base).clip(0, 3).fillna(1.0)
    ema_slope = (x["ema20"].diff(2) / x["atr14"].replace(0, np.nan)).abs().fillna(0.0)
    vol_pts = (vol_ratio / 2.0).clip(0, 1) * 40.0
    slope_pts = (ema_slope / 0.30).clip(0, 1) * 30.0
    eff_pts = x["body_ratio"].clip(0, 1) * 30.0
    x["fuel_score"] = (vol_pts + slope_pts + eff_pts).clip(0, 100)
    x["buy_threshold"] = x["open"] + (x["atr14"] * 0.5)
    x["sell_threshold"] = x["open"] - (x["atr14"] * 0.5)
    return x


def build_resampled_5m_df(df_1m: pd.DataFrame) -> pd.DataFrame:
    if df_1m.empty or len(df_1m) < 5:
        return pd.DataFrame()
    tmp = df_1m.copy().set_index("timestamp")
    df_5m = tmp.resample("5min").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna().reset_index()
    if df_5m.empty:
        return df_5m
    return add_full_indicators(df_5m)


def compute_gann_levels(reference_price: float) -> dict:
    if reference_price <= 0:
        return {}
    step = 0.125
    base_root = round(math.sqrt(float(reference_price)) / step) * step
    buy_trigger = round(base_root ** 2, 2)
    sell_trigger = round((base_root - step) ** 2, 2)
    increments = [0.100, 0.225, 0.350, 0.475]
    return {
        "BUY": buy_trigger,
        "SELL": sell_trigger,
        "BUY_TARGETS": [round((base_root + inc) ** 2, 2) for inc in increments],
        "SELL_TARGETS": [round((base_root - step - inc) ** 2, 2) for inc in increments],
        "BUY_SL": sell_trigger,
        "SELL_SL": buy_trigger,
    }


def choose_gann_target(entry: float, sl: float, targets: list, side: str):
    risk = (entry - sl) if side == "BUY" else (sl - entry)
    if risk <= 0:
        return None
    for idx_t, tgt in enumerate(targets, 1):
        reward = (tgt - entry) if side == "BUY" else (entry - tgt)
        if reward > 0 and (reward / risk) >= EQ_MIN_RR:
            return f"T{idx_t}", tgt
    return None


def find_latest_swing_levels(df: pd.DataFrame, lookback: int = 12) -> tuple:
    sub = df.iloc[-lookback:] if len(df) >= lookback else df
    lows = sub["low"].values
    highs = sub["high"].values

    swing_low = None
    for i in range(len(lows) - 2, 0, -1):
        if lows[i] <= lows[i - 1] and lows[i] <= lows[i + 1]:
            swing_low = float(lows[i])
            break
    if swing_low is None:
        swing_low = float(sub["low"].min())

    swing_high = None
    for i in range(len(highs) - 2, 0, -1):
        if highs[i] >= highs[i - 1] and highs[i] >= highs[i + 1]:
            swing_high = float(highs[i])
            break
    if swing_high is None:
        swing_high = float(sub["high"].max())

    return swing_low, swing_high


def detect_vwap_pattern_with_momentum(df: pd.DataFrame) -> dict:
    if len(df) < 5:
        return {"signal": None, "pattern": None}

    c1, c2, c3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    o1, h1, l1, cl1 = float(c1["open"]), float(c1["high"]), float(c1["low"]), float(c1["close"])
    o2, h2, l2, cl2 = float(c2["open"]), float(c2["high"]), float(c2["low"]), float(c2["close"])
    o3, h3, l3, cl3 = float(c3["open"]), float(c3["high"]), float(c3["low"]), float(c3["close"])

    vwap3 = float(c3["vwap"])
    vwap2 = float(c2["vwap"])
    atr = max(float(c3["atr14"]), 1e-6)
    vol_ratio = float(c3["vol_ratio"])
    body_ratio = float(c3["body_ratio"])

    r1, b1 = max(h1 - l1, 1e-6), abs(cl1 - o1)
    r2, b2 = max(h2 - l2, 1e-6), abs(cl2 - o2)
    r3, b3 = max(h3 - l3, 1e-6), abs(cl3 - o3)

    upper_wick3 = h3 - max(o3, cl3)
    lower_wick3 = min(o3, cl3) - l3
    mid1 = (o1 + cl1) / 2.0

    if vol_ratio < VWAP_MIN_VOL_RATIO or body_ratio < VWAP_MIN_BODY_RATIO:
        return {"signal": None, "pattern": None}

    touch_band = VWAP_TOUCH_BAND_ATR * atr

    touched_vwap_from_above = (
        abs(l3 - vwap3) <= touch_band
        or abs(l2 - vwap2) <= touch_band
        or (l3 <= vwap3 and cl3 > vwap3)
    )
    bull_pattern = None
    if touched_vwap_from_above and cl3 > vwap3 and cl3 > o3:
        if (cl2 < o2) and (cl3 >= o2) and (o3 <= cl2):
            bull_pattern = "BULL_ENGULF"
        elif (cl1 < o1) and (b1 / r1 >= 0.45) and (b2 / r2 <= 0.40) and (cl3 > mid1):
            bull_pattern = "MORN_STAR"
        elif (lower_wick3 >= 1.5 * max(b3, r3 * 0.1)) and ((cl3 - l3) / r3 >= 0.60):
            bull_pattern = "HAMMER"
        elif (cl3 > h2) and (body_ratio >= 0.60):
            bull_pattern = "VWAP_BOUNCE"

    if bull_pattern:
        return {"signal": "CE", "pattern": bull_pattern}

    touched_vwap_from_below = (
        abs(h3 - vwap3) <= touch_band
        or abs(h2 - vwap2) <= touch_band
        or (h3 >= vwap3 and cl3 < vwap3)
    )
    bear_pattern = None
    if touched_vwap_from_below and cl3 < vwap3 and cl3 < o3:
        if (cl2 > o2) and (cl3 <= o2) and (o3 >= cl2):
            bear_pattern = "BEAR_ENGULF"
        elif (cl1 > o1) and (b1 / r1 >= 0.45) and (b2 / r2 <= 0.40) and (cl3 < mid1):
            bear_pattern = "EVE_STAR"
        elif (upper_wick3 >= 1.5 * max(b3, r3 * 0.1)) and ((h3 - cl3) / r3 >= 0.60):
            bear_pattern = "SHOOT_STAR"
        elif (cl3 < l2) and (body_ratio >= 0.60):
            bear_pattern = "VWAP_REJECT"

    if bear_pattern:
        return {"signal": "PE", "pattern": bear_pattern}

    return {"signal": None, "pattern": None}


# ==============================================================================
# 5. ORDER EXECUTION
# ==============================================================================
def place_marketable_limit_order(instrument_key: str, qty: int, side: str, ltp: float, tick_size: float = 0.05) -> dict:
    is_mcx = instrument_key.startswith("MCX_FO")
    is_eq = instrument_key.startswith("NSE_EQ")
    if is_eq:
        buffer = 1.002 if side == "BUY" else 0.998
    elif is_mcx:
        buffer = 1.003 if side == "BUY" else 0.997
    else:
        buffer = 1.015 if side == "BUY" else 0.985

    limit_price = round_to_tick(ltp * buffer, tick_size)

    if PAPER_MODE:
        log.info(f"[PAPER ORDER] {side} {qty} x {instrument_key} @ ₹{ltp:.2f} (Limit ₹{limit_price})")
        return {"status": "success", "fill_price": ltp, "order_id": f"PAPER_{int(time.time())}"}

    try:
        payload = {
            "quantity": qty, "product": "I", "validity": "DAY", "price": limit_price,
            "tag": "V3_MULTI_ENGINE", "instrument_token": instrument_key,
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
# 6. UNIFIED MULTI-ASSET ENGINE WITH 4 PINNED TELEGRAM DASHBOARDS
# ==============================================================================
class IntradayV3Engine:
    def __init__(self):
        self.expiries = {}
        self.fut_contracts = {}
        self.pending_retests = {idx: None for idx in INDICES}
        self.last_rev_5m_ts = {idx: "" for idx in INDICES}
        self.last_vwap_bar_ts = {idx: "" for idx in INDICES}
        self.open_pcr = {idx: None for idx in INDICES}
        self.pcr_history = {idx: deque(maxlen=6) for idx in INDICES}

        # Equity Cash State (Strict Top 5 Stocks & Max 5 Trades/Day)
        self.eq_instruments = {}
        self.eq_morning_ready = False
        self.eq_levels = {}
        self.eq_top5_stocks = []
        self.eq_traded_today = set()
        self.last_eq_scan_ts = 0.0
        self.eq_scan_summary = "WAITING 09:30"

        self.last_exit_ts = {}
        self.last_exit_candle_ts = {}
        self.active_positions = {}
        self.pos_lock = threading.Lock()

        self.closed_trades = []
        self.trade_counter = 0
        self.day_pnl = 0.0
        self.last_summaries = {}
        self.latest_dfs = {}

        # 4 Separate Pinned Dashboards: master (consolidated), index, mcx, equity
        self.dash_ids = load_saved_dashboard_ids()
        self.dashboard_pulse = False
        self.dashboard_lock = threading.Lock()

        self.engine_boot_ts = time.time()
        self.streamer = None
        self.ws_connected = False
        self.subscribed_keys = set()
        self.bot_status_text = "BOT LIVE"
        self.stop_requested = False
        self.session_sched = {
            "is_holiday": False,
            "description": "",
            "nse_open": True,
            "nse_start": INDEX_WINDOWS[0][0],
            "nse_end": INDEX_WINDOWS[-1][1],
            "nse_exit": INDEX_EXIT_TIME,
            "mcx_open": True,
            "mcx_start": MCX_WINDOWS[0][0],
            "mcx_end": MCX_WINDOWS[-1][1],
            "mcx_exit": MCX_EXIT_TIME,
            "is_muhurat": False,
        }

    def acquire_single_instance_lock(self) -> bool:
        os.makedirs("live_journal", exist_ok=True)
        curr_run_id = os.getenv("GITHUB_RUN_ID", str(os.getpid()))
        curr_pid = os.getpid()

        if os.path.isfile(LOCK_FILE):
            try:
                with open(LOCK_FILE, "r", encoding="utf-8") as f:
                    st = json.load(f)
                old_pid = int(st.get("pid", 0))
                old_ts = float(st.get("ts", 0))
                if old_pid > 0 and old_pid != curr_pid and (time.time() - old_ts) < 21600:
                    try:
                        os.kill(old_pid, 0)
                        log.warning(f"[OVERLAP GUARD] Another engine instance (PID {old_pid}) is already running. Exiting cleanly.")
                        return False
                    except OSError:
                        pass
            except Exception:
                pass

        try:
            with open(LOCK_FILE, "w", encoding="utf-8") as f:
                json.dump({"pid": curr_pid, "run_id": curr_run_id, "ts": time.time()}, f)
        except Exception:
            pass
        return True

    def release_single_instance_lock(self):
        try:
            if os.path.isfile(LOCK_FILE):
                os.remove(LOCK_FILE)
        except Exception:
            pass

    def save_active_positions_state(self):
        os.makedirs(os.path.dirname(ACTIVE_STATE_FILE), exist_ok=True)
        serializable = {}
        with self.pos_lock:
            for pos_key, pos in self.active_positions.items():
                serializable[pos_key] = dict(pos)
        try:
            with open(ACTIVE_STATE_FILE, mode="w", encoding="utf-8") as f:
                json.dump(serializable, f, indent=2)
        except Exception as e:
            log.warning(f"Active positions save error: {e}")

    def load_active_positions_state(self):
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
            restored_eq_count = 0

            with self.pos_lock:
                for pos_key, pos in data.items():
                    if pos.get("type") == "EQUITY_CASH" and restored_eq_count >= EQ_MAX_TRADES_PER_DAY:
                        continue

                    inst_key = pos["instrument_key"]
                    is_short = pos["tx_side"] == "SELL"
                    entry = float(pos["entry_price"])
                    tick_sz = float(pos.get("tick_size", 0.05))
                    r_unit = max(float(pos.get("r_unit", 1.0)), 1e-6)
                    allow_be = bool(pos.get("allow_be_trail", True))

                    q_data = live_quotes.get(inst_key, {})
                    curr_ltp = float(q_data.get("last_price", pos.get("last_ltp", entry)))
                    pos["last_ltp"] = curr_ltp

                    if allow_be:
                        curr_gain = (entry - curr_ltp) if is_short else (curr_ltp - entry)
                        if curr_gain >= r_unit:
                            be_sl = round_to_tick((entry - 0.10 * r_unit) if is_short else (entry + 0.10 * r_unit), tick_sz)
                            if (is_short and be_sl < pos["sl"]) or (not is_short and be_sl > pos["sl"]):
                                pos["sl"] = be_sl
                                pos["trail_stage"] = "1R BREAK-EVEN"

                    self.active_positions[pos_key] = pos
                    if pos.get("type") == "EQUITY_CASH":
                        restored_eq_count += 1
                        self.eq_traded_today.add(pos_key)
                    self.trade_counter = max(self.trade_counter, int(pos.get("trade_no", 0)), len(self.closed_trades))

                    tgt = float(pos.get("target", 0.0))
                    if tgt > 0 and ((not is_short and curr_ltp >= tgt) or (is_short and curr_ltp <= tgt)):
                        immediate_exits.append((pos_key, curr_ltp, "TARGET_HIT"))
                    elif (not is_short and curr_ltp <= pos["sl"]) or (is_short and curr_ltp >= pos["sl"]):
                        reason = "TRAIL_BE_SL_HIT" if pos.get("trail_stage") else "STOP_LOSS_HIT"
                        immediate_exits.append((pos_key, curr_ltp, reason))

            for pos in list(self.active_positions.values()):
                self.subscribe_tick_instrument(pos["instrument_key"])

            for pos_key, exit_ltp, reason in immediate_exits:
                self.close_position_now(pos_key, exit_ltp, reason, now_ist)

            self.save_active_positions_state()
        except Exception as e:
            log.warning(f"Active positions restore error: {e}")

    def load_today_state_from_journal(self):
        restore_journal_from_google_sheet()
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        if not os.path.isfile(JOURNAL_FILE):
            return
        try:
            self.closed_trades = []
            self.day_pnl = 0.0
            with open(JOURNAL_FILE, mode="r", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    d_str = _normalize_date_str(r.get("date"))
                    if d_str == today_str:
                        pnl_val = float(r.get("pnl", 0.0))
                        t_no = int(float(r.get("trade_no", len(self.closed_trades) + 1)))
                        qty_v = int(float(r.get("qty", 0)))
                        units_v = int(float(r.get("pnl_units") or qty_v))
                        self.closed_trades.append({
                            "trade_no": t_no, "date": d_str, "entry_time": r.get("entry_time", ""),
                            "exit_time": r.get("exit_time", ""), "symbol": r.get("symbol", ""),
                            "contract": r.get("contract", ""), "side": r.get("side", ""),
                            "qty": qty_v, "pnl_units": units_v,
                            "entry_price": float(r.get("entry_price", 0.0)),
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
            self.trade_counter = max(self.trade_counter, len(self.closed_trades))
            if self.closed_trades:
                for t in self.closed_trades:
                    try:
                        ex_dt = datetime.strptime(f"{today_str} {t['exit_time']}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
                        strat_k = map_setup_to_strategy(t.get("setup", ""))
                        pk = f"{t['symbol']}__{strat_k}"
                        self.last_exit_ts[pk] = max(self.last_exit_ts.get(pk, 0.0), ex_dt.timestamp())
                        if strat_k.startswith("EQ_"):
                            self.eq_traded_today.add(pk)
                    except Exception:
                        pass
                log.info(f"[STATE LOADED] {len(self.closed_trades)} today's trade(s) loaded | Net PnL: ₹{self.day_pnl:+.2f}")
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

            def on_open(*args):
                self.ws_connected = True
                with self.pos_lock:
                    active_keys = list({p["instrument_key"] for p in self.active_positions.values()})
                if active_keys:
                    self.streamer.subscribe(active_keys, "ltpc")
                    self.subscribed_keys.update(active_keys)

            def on_message(message):
                try:
                    data = json.loads(message) if isinstance(message, str) else message
                    for inst_key, feed_obj in data.get("feeds", {}).items():
                        full_feed = feed_obj.get("fullFeed", {})
                        ltpc = (
                            feed_obj.get("ltpc")
                            or full_feed.get("marketFF", {}).get("ltpc")
                            or full_feed.get("indexFF", {}).get("ltpc")
                            or feed_obj.get("firstLevelWithGreeks", {}).get("ltpc")
                        )
                        if ltpc and "ltp" in ltpc and float(ltpc["ltp"]) > 0:
                            self.on_tick_price(inst_key, float(ltpc["ltp"]))
                except Exception:
                    pass

            def on_close(*args):
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

    def unsubscribe_tick_instrument_if_unused(self, instrument_key: str):
        with self.pos_lock:
            still_used = any(p["instrument_key"] == instrument_key for p in self.active_positions.values())
            no_active = len(self.active_positions) == 0

        if not still_used:
            self.subscribed_keys.discard(instrument_key)
            if self.streamer and self.ws_connected:
                try:
                    self.streamer.unsubscribe([instrument_key])
                except Exception:
                    pass

        if no_active and self.streamer:
            try:
                self.streamer.disconnect()
            except Exception:
                pass
            self.streamer = None
            self.ws_connected = False

    def on_tick_price(self, instrument_key: str, ltp: float):
        norm_key = instrument_key.replace(":", "|")
        exits_to_run = []

        with self.pos_lock:
            for pos_key, pos in self.active_positions.items():
                if pos["instrument_key"].replace(":", "|") == norm_key:
                    pos["last_ltp"] = ltp
                    is_short = pos["tx_side"] == "SELL"
                    entry = pos["entry_price"]
                    curr_gain = (entry - ltp) if is_short else (ltp - entry)
                    r_unit = max(pos.get("r_unit", 1.0), 1e-6)
                    tick_sz = pos.get("tick_size", 0.05)
                    tgt = float(pos.get("target", 0.0))

                    if pos.get("allow_be_trail", True) and curr_gain >= r_unit:
                        be_sl = round_to_tick((entry - 0.10 * r_unit) if is_short else (entry + 0.10 * r_unit), tick_sz)
                        if (is_short and be_sl < pos["sl"]) or (not is_short and be_sl > pos["sl"]):
                            pos["sl"] = be_sl
                            pos["trail_stage"] = "1R BREAK-EVEN"

                    if tgt > 0 and ((not is_short and ltp >= tgt) or (is_short and ltp <= tgt)):
                        exits_to_run.append((pos_key, "TARGET_HIT"))
                    elif (not is_short and ltp <= pos["sl"]) or (is_short and ltp >= pos["sl"]):
                        reason = "TRAIL_BE_SL_HIT" if pos.get("trail_stage") else "STOP_LOSS_HIT"
                        exits_to_run.append((pos_key, reason))

        for pos_key, reason in exits_to_run:
            self.close_position_now(pos_key, ltp, reason, datetime.now(IST))

    def init_contracts(self):
        for idx, cfg in INDICES.items():
            fut_info = get_active_future_contract(idx)
            self.fut_contracts[idx] = fut_info
            if cfg["type"] == "MCX_FUT":
                cfg["spot_key"] = fut_info.get("instrument_key", "")
                cfg["lot_size"] = fut_info.get("lot_size", 1)
                cfg["pnl_multiplier"] = fut_info.get("pnl_multiplier", cfg["lot_size"])
                cfg["freeze_qty"] = fut_info.get("freeze_quantity", 10000)
                cfg["tick_size"] = fut_info.get("tick_size", 0.05)
                cfg["step"] = 0
            else:
                opt_specs = get_dynamic_option_specs(cfg["spot_key"])
                self.expiries[idx] = opt_specs["expiries"]
                cfg["lot_size"] = opt_specs["lot_size"]
                cfg["pnl_multiplier"] = opt_specs["lot_size"]
                cfg["freeze_qty"] = opt_specs["freeze_quantity"]
                cfg["tick_size"] = opt_specs["tick_size"]
                cfg["step"] = opt_specs["step"]

        self.eq_instruments = resolve_equity_instruments(EQUITY_UNIVERSE)

    def get_smart_expiry(self, idx: str, now_ist: datetime) -> tuple:
        valid_exps = self.expiries.get(idx, [])
        if not valid_exps:
            return "", False
        hhmm = now_ist.strftime("%H:%M")
        nearest = valid_exps[0]
        is_today_exp = nearest == now_ist.strftime("%Y-%m-%d")
        if is_today_exp and hhmm > "14:45" and len(valid_exps) > 1:
            return valid_exps[1], False
        return nearest, is_today_exp

    def is_within_trade_window(self, idx: str, now_ist: datetime) -> bool:
        hhmm = now_ist.strftime("%H:%M")
        if INDICES[idx]["type"] == "MCX_FUT":
            if not self.session_sched.get("mcx_open", True):
                return False
            if self.session_sched.get("is_muhurat"):
                return self.session_sched["mcx_start"] <= hhmm <= self.session_sched["mcx_end"]
            return any(st <= hhmm <= en for st, en in MCX_WINDOWS)

        if not self.session_sched.get("nse_open", True):
            return False
        if self.session_sched.get("is_muhurat"):
            return self.session_sched["nse_start"] <= hhmm <= self.session_sched["nse_end"]
        return any(st <= hhmm <= en for st, en in INDEX_WINDOWS)

    def analyze_strong_pcr(self, idx: str, chain: list) -> tuple:
        if not chain:
            return 1.0, 0.0, False, False

        total_pe_oi = sum(float(r.get("put_options", {}).get("market_data", {}).get("oi", 0)) for r in chain)
        total_ce_oi = sum(float(r.get("call_options", {}).get("market_data", {}).get("oi", 0)) for r in chain)
        pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 1.0

        if self.open_pcr[idx] is None:
            self.open_pcr[idx] = pcr
        cum_diff = round(pcr - self.open_pcr[idx], 2)

        hist = self.pcr_history[idx]
        if not hist or hist[-1] != pcr:
            hist.append(pcr)

        step_diff = round(hist[-1] - hist[-2], 2) if len(hist) >= 2 else 0.0
        strong_bull_pcr = (step_diff >= MIN_PCR_STEP_DIFF and cum_diff > 0) or (pcr >= 1.15 and step_diff >= 0)
        strong_bear_pcr = (step_diff <= -MIN_PCR_STEP_DIFF and cum_diff < 0) or (pcr <= 0.85 and step_diff <= 0)
        return pcr, cum_diff, strong_bull_pcr, strong_bear_pcr

    def _calc_segment_stats(self, seg_type: str = None) -> dict:
        if seg_type:
            c_list = [
                t for t in self.closed_trades
                if (seg_type == "EQUITY_CASH" and map_setup_to_strategy(t.get("setup", "")).startswith("EQ_"))
                or (seg_type == "MCX_FUT" and t.get("symbol") in ("CRUDEOIL", "NATURALGAS", "GOLD", "SILVER", "COPPER"))
                or (seg_type == "INDEX_OPT" and t.get("symbol") in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"))
            ]
        else:
            c_list = list(self.closed_trades)

        realized = round(sum(float(t.get("pnl", 0.0)) for t in c_list), 2)
        wins = sum(1 for t in c_list if float(t.get("pnl", 0.0)) > 0)
        losses = sum(1 for t in c_list if float(t.get("pnl", 0.0)) <= 0)
        wr = int(round((wins / len(c_list)) * 100.0)) if c_list else 0

        with self.pos_lock:
            a_list = [p for p in self.active_positions.values() if (not seg_type or p.get("type") == seg_type)]

        live_net = 0.0
        for p in a_list:
            ltp = p.get("last_ltp", p["entry_price"])
            is_short = p["tx_side"] == "SELL"
            eff_u = float(p.get("pnl_units", p["qty"]))
            est_c = float(p.get("est_charges", 45.0 if p["type"] == "EQUITY_CASH" else (180.0 if p["type"] == "MCX_FUT" else 65.0)))
            gross = (p["entry_price"] - ltp) * eff_u if is_short else (ltp - p["entry_price"]) * eff_u
            live_net += (gross - est_c)

        live_net = round(live_net, 2)
        return {
            "realized": realized,
            "live_mtm": live_net,
            "net_pnl": round(realized + live_net, 2),
            "closed": len(c_list),
            "wins": wins,
            "losses": losses,
            "wr": wr,
            "open_count": len(a_list),
        }

    def build_strategy_performance_lines(self) -> list:
        with self.pos_lock:
            active_vals = list(self.active_positions.values())

        lines = []
        for sk in ALL_STRATEGY_KEYS:
            c_list = [t for t in self.closed_trades if map_setup_to_strategy(t.get("setup", "")) == sk]
            a_list = [p for p in active_vals if p.get("strategy_key") == sk]

            tot = len(c_list)
            wins = [t for t in c_list if float(t.get("pnl", 0.0)) > 0]
            losses = [t for t in c_list if float(t.get("pnl", 0.0)) <= 0]
            wr = int(round((len(wins) / tot) * 100.0)) if tot > 0 else 0
            realized_pnl = round(sum(float(t.get("pnl", 0.0)) for t in c_list), 2)

            live_mtm = 0.0
            for p in a_list:
                ltp = p.get("last_ltp", p["entry_price"])
                is_short = p["tx_side"] == "SELL"
                eff_u = float(p.get("pnl_units", p["qty"]))
                est_c = float(p.get("est_charges", 45.0 if p["type"] == "EQUITY_CASH" else (180.0 if p["type"] == "MCX_FUT" else 65.0)))
                gross = (p["entry_price"] - ltp) * eff_u if is_short else (ltp - p["entry_price"]) * eff_u
                live_mtm += (gross - est_c)

            avg_win = (sum(float(t["pnl"]) for t in wins) / len(wins)) if wins else 0.0
            avg_loss = (abs(sum(float(t["pnl"]) for t in losses)) / len(losses)) if losses else 0.0
            if avg_loss > 0 and avg_win > 0:
                rr_txt = f"1:{avg_win / avg_loss:.1f}"
            elif sk == "1M_BO_RETEST":
                rr_txt = "1:1.5"
            elif sk in ("5M_CONTRARIAN", "EQ_GANN_15M"):
                rr_txt = "1:2.0+"
            elif sk == "VWAP_PCR_REV":
                rr_txt = "1:0.5(REV)"
            else:
                rr_txt = "EOD_15:15"

            tot_net = round(realized_pnl + live_mtm, 2)
            icon = "🟢" if tot_net > 0 else ("🔴" if tot_net < 0 else "⚪")
            open_tag = f" | Open:{len(a_list)}" if a_list else ""
            lines.append(
                f"• <b>{sk}:</b> {icon} <b>₹{tot_net:+,.0f}</b> | {tot}T ({len(wins)}W/{len(losses)}L) | WR <b>{wr}%</b> | R:R <b>{rr_txt}</b>{open_tag}"
            )
        return lines

    def build_hourly_analysis_lines(self) -> list:
        hourly = {}
        for t in self.closed_trades:
            et = str(t.get("entry_time", ""))
            if len(et) >= 2 and et[:2].isdigit():
                hr = int(et[:2])
                rec = hourly.setdefault(hr, {"trades": 0, "wins": 0, "pnl": 0.0})
                rec["trades"] += 1
                pnl_v = float(t.get("pnl", 0.0))
                if pnl_v > 0:
                    rec["wins"] += 1
                rec["pnl"] = round(rec["pnl"] + pnl_v, 2)

        with self.pos_lock:
            for p in self.active_positions.values():
                et = str(p.get("entry_time", ""))
                if len(et) >= 2 and et[:2].isdigit():
                    hr = int(et[:2])
                    rec = hourly.setdefault(hr, {"trades": 0, "wins": 0, "pnl": 0.0})
                    ltp = p.get("last_ltp", p["entry_price"])
                    is_short = p["tx_side"] == "SELL"
                    eff_u = float(p.get("pnl_units", p["qty"]))
                    est_c = float(p.get("est_charges", 45.0 if p["type"] == "EQUITY_CASH" else (180.0 if p["type"] == "MCX_FUT" else 65.0)))
                    gross = (p["entry_price"] - ltp) * eff_u if is_short else (ltp - p["entry_price"]) * eff_u
                    rec["pnl"] = round(rec["pnl"] + gross - est_c, 2)

        if not hourly:
            return ["<i>No hourly trade data yet today.</i>"]

        best_hr = max(hourly.items(), key=lambda kv: kv[1]["pnl"])
        worst_hr = min(hourly.items(), key=lambda kv: kv[1]["pnl"])
        lines = []

        for hr in sorted(hourly.keys()):
            d = hourly[hr]
            slot_lbl = f"{hr:02d}:00-{hr + 1:02d}:00"
            if d["pnl"] >= 200:
                tag = "🔥 PROFIT"
            elif d["pnl"] <= -200:
                tag = "⚠️ LOSS"
            else:
                tag = "➖ CONSOL/FLAT"

            badge = ""
            if hr == best_hr[0] and best_hr[1]["pnl"] > 0:
                badge = " [BEST HR]"
            elif hr == worst_hr[0] and worst_hr[1]["pnl"] < 0:
                badge = " [WORST HR]"

            lines.append(
                f"• <code>{slot_lbl}</code>: <b>₹{d['pnl']:+,.0f}</b> ({d['trades']}T, {d['wins']}W) | <code>{tag}{badge}</code>"
            )
        return lines

    def get_performance_summary_lines(self, now_ist: datetime, live_mtm: float) -> list:
        today_str = now_ist.strftime("%Y-%m-%d")
        curr_year, curr_week, _ = now_ist.isocalendar()
        curr_month_prefix = now_ist.strftime("%Y-%m")

        past_rows = []
        if os.path.isfile(JOURNAL_FILE):
            try:
                with open(JOURNAL_FILE, mode="r", encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        try:
                            d_str = _normalize_date_str(r.get("date"))
                            if not d_str or d_str == today_str:
                                continue
                            d_obj = datetime.strptime(d_str, "%Y-%m-%d")
                            y_iso, w_iso, _ = d_obj.isocalendar()
                            past_rows.append({"date": d_str, "year": y_iso, "week": w_iso, "pnl": float(r.get("pnl", 0.0))})
                        except Exception:
                            continue
            except Exception:
                pass

        d_tot = len(self.closed_trades)
        d_wins = sum(1 for x in self.closed_trades if x["pnl"] > 0)
        d_wr = int(round((d_wins / d_tot) * 100.0)) if d_tot > 0 else 0
        d_net = round(self.day_pnl, 2)

        def combine_period(sub):
            tot = len(sub) + d_tot
            wins = sum(1 for x in sub if x["pnl"] > 0) + d_wins
            wr = int(round((wins / tot) * 100.0)) if tot > 0 else 0
            net = round(sum(x["pnl"] for x in sub) + d_net + live_mtm, 2)
            return tot, wr, net

        w_tot, w_wr, w_live = combine_period([r for r in past_rows if r["year"] == curr_year and r["week"] == curr_week])
        m_tot, m_wr, m_live = combine_period([r for r in past_rows if r["date"].startswith(curr_month_prefix)])
        y_tot, y_wr, y_live = combine_period([r for r in past_rows if r["year"] == curr_year])
        d_live = round(d_net + live_mtm, 2)

        return [
            f"• <b>Daily:</b> <b>₹{d_live:+,.2f}</b> ({d_tot}T | {d_wr}% Win)",
            f"• <b>Weekly:</b> <b>₹{w_live:+,.2f}</b> ({w_tot}T | {w_wr}% Win)",
            f"• <b>Monthly:</b> <b>₹{m_live:+,.2f}</b> ({m_tot}T | {m_wr}% Win)",
            f"• <b>Yearly:</b> <b>₹{y_live:+,.2f}</b> ({y_tot}T | {y_wr}% Win)",
        ]

    def _format_segment_positions(self, seg_type: str) -> list:
        with self.pos_lock:
            seg_active = sorted(
                [p for p in self.active_positions.values() if p.get("type") == seg_type],
                key=lambda x: x["trade_no"],
            )

        out = []
        if seg_active:
            out.append(f"\n🔥 <b>ACTIVE ({len(seg_active)})</b>")
            for pos in seg_active:
                ltp = pos.get("last_ltp", pos["entry_price"])
                is_short = pos["tx_side"] == "SELL"
                est_c = float(pos.get("est_charges", 45.0 if seg_type == "EQUITY_CASH" else (180.0 if seg_type == "MCX_FUT" else 65.0)))
                eff_u = int(pos.get("pnl_units", pos["qty"]))
                pts_diff = round((pos["entry_price"] - ltp) if is_short else (ltp - pos["entry_price"]), 2)
                gross_val = round(pts_diff * eff_u, 2)
                pnl = round(gross_val - est_c, 2)
                sl_gross = (pos["entry_price"] - pos["sl"]) * eff_u if is_short else (pos["sl"] - pos["entry_price"]) * eff_u
                sl_net = round(sl_gross - est_c, 0)
                strat_lbl = pos.get("setup_name", pos.get("strategy_key", ""))
                tgt_val = float(pos.get("target", 0.0))
                tgt_txt = f"₹{tgt_val:.2f}" if tgt_val > 0 else "15:15 EOD"
                trail_tag = f"<code>{strat_lbl}</code> | 🛡️<code>{pos['trail_stage']}</code>" if pos.get("trail_stage") else f"<code>{strat_lbl}</code>"
                p_icon = "🟢" if pnl >= 0 else "🔴"
                sl_lbl = f"Lock <b>₹{sl_net:+,.0f}</b>" if sl_net >= 0 else f"Risk <b>₹{abs(sl_net):,.0f}</b>"
                qty_txt = f"{pos['qty']} Sh" if seg_type == "EQUITY_CASH" else f"{pos['qty']} Qty"

                out.extend([
                    f"• <b>#{pos['trade_no']} {pos['symbol']}</b> <code>{pos['strike']} {pos['display_side']}</code> (<code>{qty_txt}</code>) | {trail_tag}",
                    f"  In: <b>₹{pos['entry_price']:.2f}</b> (<code>{pos['entry_time'][:5]}</code>) → LTP: <b>₹{ltp:.2f}</b> | SL: <code>₹{pos['sl']:.2f}</code> ({sl_lbl}) | Tgt: <code>{tgt_txt}</code> | Net: {p_icon} <b>₹{pnl:+,.2f}</b>",
                ])

        seg_closed = [
            t for t in self.closed_trades
            if (seg_type == "EQUITY_CASH" and map_setup_to_strategy(t.get("setup", "")).startswith("EQ_"))
            or (seg_type == "MCX_FUT" and t.get("symbol") in ("CRUDEOIL", "NATURALGAS", "GOLD", "SILVER", "COPPER"))
            or (seg_type == "INDEX_OPT" and t.get("symbol") in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"))
        ]
        out.append("\n📒 <b>CLOSED TRADES (LATEST 6)</b>")
        if seg_closed:
            for t in sorted(seg_closed, key=lambda x: x["trade_no"])[-6:]:
                icon = "✅" if t["pnl"] >= 0 else "❌"
                out.append(
                    f"• <b>#{t['trade_no']} {t['symbol']}</b> <code>{t['contract']} {t['side']}</code> {icon} <b>₹{t['pnl']:+,.2f}</b> | <code>{t.get('setup','')}</code> (<code>{t['entry_time'][:5]}→{t['exit_time'][:5]}</code>) <i>{t['exit_reason']}</i>"
                )
        elif not seg_active:
            out.append("<i>No trades yet today.</i>")

        return out

    # ==========================================================================
    # RENDER & PIN ALL 4 LIVE DASHBOARDS (CONSOLIDATED + INDEX + MCX + EQUITY)
    # ==========================================================================
    def render_live_dashboard(self, hhmm: str):
        with self.dashboard_lock:
            now_ist = datetime.now(IST)
            self.dashboard_pulse = not self.dashboard_pulse
            if "CLOSED" in self.bot_status_text or "STOPPED" in self.bot_status_text:
                status_header = "🔴 <b>MARKET CLOSED</b>"
            elif "STANDBY" in self.bot_status_text:
                status_header = f"🟠 <b>{self.bot_status_text}</b>"
            else:
                pulse_dot = "🟢" if self.dashboard_pulse else "⚪"
                status_header = f"{pulse_dot} <b>BOT LIVE</b>"

            mode_badge = "<code>PAPER</code>" if PAPER_MODE else "<code>LIVE</code>"
            tot_st = self._calc_segment_stats(None)
            idx_st = self._calc_segment_stats("INDEX_OPT")
            mcx_st = self._calc_segment_stats("MCX_FUT")
            eq_st = self._calc_segment_stats("EQUITY_CASH")

            # ------------------------------------------------------------------
            # PINNED CARD 1: CONSOLIDATED MASTER (INDEX + MCX + EQUITY + 6 STRAT)
            # ------------------------------------------------------------------
            tot_icon = "🟢" if tot_st["net_pnl"] >= 0 else "🔴"
            idx_icon = "🟢" if idx_st["net_pnl"] >= 0 else "🔴"
            mcx_icon = "🟢" if mcx_st["net_pnl"] >= 0 else "🔴"
            eq_icon = "🟢" if eq_st["net_pnl"] >= 0 else "🔴"

            master_lines = [
                f"📌 <b>[1/4] CONSOLIDATED MASTER P&amp;L</b>  |  {status_header}  |  <code>{hhmm} IST</code>  |  {mode_badge}",
                f"────────────────────────────",
                f"💼 <b>TOTAL DAY NET P&amp;L:</b> {tot_icon} <b>₹{tot_st['net_pnl']:+,.2f}</b>",
                f"• Realized: <b>₹{tot_st['realized']:+,.2f}</b>  |  Live MTM: <b>₹{tot_st['live_mtm']:+,.2f}</b>",
                f"• Closed: <b>{tot_st['closed']}T</b> ({tot_st['wins']}W/{tot_st['losses']}L, <b>{tot_st['wr']}%</b>)  |  Open: <b>{tot_st['open_count']}</b>",
                f"\n📦 <b>ASSET-WISE BREAKDOWN (NET ₹)</b>",
                f"• 📡 <b>Index Options:</b> {idx_icon} <b>₹{idx_st['net_pnl']:+,.2f}</b> ({idx_st['closed']}T, {idx_st['wr']}% WR | Open: {idx_st['open_count']})",
                f"• 🛢️ <b>MCX Mini Fut:</b> {mcx_icon} <b>₹{mcx_st['net_pnl']:+,.2f}</b> ({mcx_st['closed']}T, {mcx_st['wr']}% WR | Open: {mcx_st['open_count']})",
                f"• 🏛️ <b>NSE Top-5 Eq:</b> {eq_icon} <b>₹{eq_st['net_pnl']:+,.2f}</b> ({eq_st['closed']}T, {eq_st['wr']}% WR | Open: {eq_st['open_count']})",
                f"\n🎯 <b>ALL 6 STRATEGIES PERFORMANCE</b>",
            ]
            master_lines.extend(self.build_strategy_performance_lines())
            master_lines.append("\n⏰ <b>HOURLY SESSION TRACKER (IST)</b>")
            master_lines.extend(self.build_hourly_analysis_lines())
            master_lines.append("\n📊 <b>CUMULATIVE SUMMARY (NET ₹)</b>")
            master_lines.extend(self.get_performance_summary_lines(now_ist, tot_st["live_mtm"]))

            # ------------------------------------------------------------------
            # PINNED CARD 2: INDEX OPTIONS DASHBOARD
            # ------------------------------------------------------------------
            idx_lines = [
                f"📌 <b>[2/4] INDEX OPTIONS BOARD</b>  |  <code>{hhmm} IST</code>  |  {mode_badge}",
                f"────────────────────────────",
                f"📡 <b>Index Net P&amp;L:</b> {idx_icon} <b>₹{idx_st['net_pnl']:+,.2f}</b> (Real: ₹{idx_st['realized']:+,.2f} | MTM: ₹{idx_st['live_mtm']:+,.2f})",
                f"• Trades: <b>{idx_st['closed']}</b> ({idx_st['wins']}W/{idx_st['losses']}L | WR: <b>{idx_st['wr']}%</b>)  |  Slot: <code>09:10-15:00</code>",
                f"\n📈 <b>LIVE INDICES FEED</b>",
            ]
            for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"):
                s = self.last_summaries.get(idx)
                if s:
                    idx_lines.append(f"• <b>{s['idx']}:</b> <code>{s['spot']}</code> | PCR <b>{s['pcr']:.2f}</b> | Vol <b>{s['vol_ratio']:.1f}x</b> | <code>{s['short_status']}</code>")
                else:
                    idx_lines.append(f"• <b>{idx}:</b> <code>SYNCING...</code>")
            idx_lines.extend(self._format_segment_positions("INDEX_OPT"))

            # ------------------------------------------------------------------
            # PINNED CARD 3: MCX MINI FUTURES DASHBOARD
            # ------------------------------------------------------------------
            mcx_lines = [
                f"📌 <b>[3/4] MCX MINI FUTURES BOARD</b>  |  <code>{hhmm} IST</code>  |  {mode_badge}",
                f"────────────────────────────",
                f"🛢️ <b>MCX Net P&amp;L:</b> {mcx_icon} <b>₹{mcx_st['net_pnl']:+,.2f}</b> (Real: ₹{mcx_st['realized']:+,.2f} | MTM: ₹{mcx_st['live_mtm']:+,.2f})",
                f"• Trades: <b>{mcx_st['closed']}</b> ({mcx_st['wins']}W/{mcx_st['losses']}L | WR: <b>{mcx_st['wr']}%</b>)  |  Slot: <code>21:00-22:00</code>",
                f"\n⚡ <b>LIVE MCX MINI FEED</b>",
            ]
            for idx in ("CRUDEOIL", "NATURALGAS", "GOLD", "SILVER", "COPPER"):
                s = self.last_summaries.get(idx)
                if s:
                    mcx_lines.append(f"• <b>{s['idx']}:</b> <code>{s['spot']}</code> | VWAP <b>{s['vwap']}</b> | Vol <b>{s['vol_ratio']:.1f}x</b> | <code>{s['short_status']}</code>")
                else:
                    mcx_lines.append(f"• <b>{idx}:</b> <code>SYNCING...</code>")
            mcx_lines.extend(self._format_segment_positions("MCX_FUT"))

            # ------------------------------------------------------------------
            # PINNED CARD 4: NSE TOP-5 EQUITY CASH DASHBOARD
            # ------------------------------------------------------------------
            top5_disp = ", ".join(self.eq_top5_stocks) if self.eq_top5_stocks else "WAITING 09:30"
            eq_lines = [
                f"📌 <b>[4/4] NSE TOP-5 EQUITY BOARD</b>  |  <code>{hhmm} IST</code>  |  {mode_badge}",
                f"────────────────────────────",
                f"🏛️ <b>Equity Net P&amp;L:</b> {eq_icon} <b>₹{eq_st['net_pnl']:+,.2f}</b> (Real: ₹{eq_st['realized']:+,.2f} | MTM: ₹{eq_st['live_mtm']:+,.2f})",
                f"• Trades: <b>{eq_st['closed']}</b> ({eq_st['wins']}W/{eq_st['losses']}L | WR: <b>{eq_st['wr']}%</b>)  |  Cap: <code>{len(self.eq_traded_today)}/{EQ_MAX_TRADES_PER_DAY}T</code>",
                f"• <b>Top-5 Basket:</b> <code>{top5_disp}</code>",
            ]
            eq_lines.extend(self._format_segment_positions("EQUITY_CASH"))

            # Update & Pin all 4 dashboards
            prev_ids = dict(self.dash_ids)
            self.dash_ids["master"] = upsert_telegram_dashboard("\n".join(master_lines), self.dash_ids.get("master"))
            self.dash_ids["index"] = upsert_telegram_dashboard("\n".join(idx_lines), self.dash_ids.get("index"))
            self.dash_ids["mcx"] = upsert_telegram_dashboard("\n".join(mcx_lines), self.dash_ids.get("mcx"))
            self.dash_ids["equity"] = upsert_telegram_dashboard("\n".join(eq_lines), self.dash_ids.get("equity"))

            if self.dash_ids != prev_ids:
                save_dashboard_ids(self.dash_ids)

    def start_heartbeat_thread(self):
        def _pulse_loop():
            while not self.stop_requested:
                time.sleep(DASHBOARD_PULSE_SEC)
                if self.stop_requested or not self.dash_ids.get("master"):
                    continue
                if "CLOSED" in self.bot_status_text or "STOPPED" in self.bot_status_text:
                    break
                try:
                    with self.pos_lock:
                        active_keys = list({p["instrument_key"] for p in self.active_positions.values()})
                    spot_keys = [cfg["spot_key"] for cfg in INDICES.values() if cfg.get("spot_key")]
                    all_live_keys = list(dict.fromkeys(active_keys + spot_keys))

                    quotes = fetch_ltp_quote(all_live_keys)
                    if quotes:
                        with self.pos_lock:
                            for pos in self.active_positions.values():
                                q = quotes.get(pos["instrument_key"], {})
                                if q.get("last_price"):
                                    pos["last_ltp"] = float(q["last_price"])
                        for idx, cfg in INDICES.items():
                            sk = cfg.get("spot_key")
                            if sk and quotes.get(sk, {}).get("last_price") and idx in self.last_summaries:
                                ltp_val = float(quotes[sk]["last_price"])
                                self.last_summaries[idx]["spot"] = round(ltp_val, 2) if ltp_val < 1000 else int(round(ltp_val))

                    self.render_live_dashboard(datetime.now(IST).strftime("%H:%M"))
                except Exception:
                    pass

        threading.Thread(target=_pulse_loop, daemon=True).start()

    def close_position_now(self, pos_key: str, ltp: float, exit_reason: str, now_ist: datetime, is_reversal: bool = False):
        with self.pos_lock:
            pos = self.active_positions.pop(pos_key, None)
        if not pos:
            return

        idx = pos["symbol"]
        self.last_exit_ts[pos_key] = 0.0 if is_reversal else time.time()
        if pos.get("strategy_key") == "1M_BO_RETEST":
            self.pending_retests[idx] = None

        df_curr = self.latest_dfs.get(idx)
        if not is_reversal and df_curr is not None and not df_curr.empty:
            self.last_exit_candle_ts[pos_key] = str(df_curr["timestamp"].iloc[-1])

        self.unsubscribe_tick_instrument_if_unused(pos["instrument_key"])
        self.save_active_positions_state()

        is_short = pos["tx_side"] == "SELL"
        close_side = "BUY" if is_short else "SELL"
        place_marketable_limit_order(pos["instrument_key"], pos["qty"], close_side, ltp, pos.get("tick_size", 0.05))

        eff_units = float(pos.get("pnl_units", pos["qty"]))
        pts = round((pos["entry_price"] - ltp) if is_short else (ltp - pos["entry_price"]), 2)
        gross_pnl = round(pts * eff_units, 2)

        buy_px = ltp if is_short else pos["entry_price"]
        sell_px = pos["entry_price"] if is_short else ltp
        ch_info = fetch_exact_trade_charges(pos["instrument_key"], buy_px, sell_px, int(eff_units), pos["type"])
        net_pnl = round(gross_pnl - ch_info["total"], 2)

        self.day_pnl = round(self.day_pnl + net_pnl, 2)

        exit_time_str = now_ist.strftime("%H:%M:%S")
        journal_entry = {
            "trade_no": pos["trade_no"], "date": now_ist.strftime("%Y-%m-%d"),
            "entry_time": pos.get("entry_time", ""), "exit_time": exit_time_str,
            "symbol": idx, "contract": str(pos["strike"]), "side": pos["display_side"],
            "qty": pos["qty"], "pnl_units": int(eff_units),
            "entry_price": pos["entry_price"], "exit_price": ltp,
            "points": pts, "sl": pos["sl"], "target": pos.get("target", 0.0), "exit_reason": exit_reason,
            "gross_pnl": gross_pnl, "brokerage": ch_info["brokerage"],
            "taxes_other": ch_info["taxes_other"], "charges": ch_info["total"],
            "pnl": net_pnl, "day_pnl": self.day_pnl, "setup": pos["setup_name"],
        }
        self.closed_trades.append(journal_entry)
        record_trade_journal(journal_entry)
        self.render_live_dashboard(now_ist.strftime("%H:%M"))

    def can_trigger_strategy(self, idx: str, strat_key: str, curr_candle_ts: str) -> bool:
        pos_key = f"{idx}__{strat_key}"
        with self.pos_lock:
            if pos_key in self.active_positions:
                return False
        sec_since = time.time() - self.last_exit_ts.get(pos_key, 0.0)
        if sec_since < REENTRY_COOLDOWN_SEC:
            return False
        if curr_candle_ts and curr_candle_ts == self.last_exit_candle_ts.get(pos_key, ""):
            return False
        return True

    # ==========================================================================
    # EQUITY CASH ENGINE (STRICT TOP 5 STOCKS ONLY & MAX 5 TRADES/DAY)
    # ==========================================================================
    def evaluate_equity_strategies(self, now_ist: datetime):
        if not self.session_sched.get("nse_open", True) or not self.eq_instruments:
            self.eq_scan_summary = "HOLIDAY / NO UNIVERSE"
            return

        hhmm = now_ist.strftime("%H:%M")
        if hhmm < EQ_ENTRY_START:
            self.eq_scan_summary = "WAITING 09:30 MOTHER"
            return
        if hhmm > EQ_EXIT_TIME:
            self.eq_scan_summary = "EQUITY CLOSED"
            return

        if (time.time() - self.last_eq_scan_ts) < EQ_SCAN_INTERVAL_SEC:
            return
        self.last_eq_scan_ts = time.time()

        if not self.eq_morning_ready:
            fuel_ranking = []
            for sym, ikey in self.eq_instruments.items():
                df_1m = fetch_intraday_candles(ikey, 1)
                if df_1m.empty:
                    continue
                df_1m["hhmm"] = df_1m["timestamp"].dt.strftime("%H:%M")
                morn = df_1m[(df_1m["hhmm"] >= "09:15") & (df_1m["hhmm"] < "09:30")]
                if morn.empty:
                    continue

                m_open = float(morn.iloc[0]["open"])
                m_high = float(morn["high"].max())
                m_low = float(morn["low"].min())
                m_close = float(morn.iloc[-1]["close"])
                m_rng = max(m_high - m_low, 0.05)
                eff = abs(m_close - m_open) / m_rng
                fuel = round(min(max(55.0 + min(eff, 1.0) * 45.0, 0.0), 100.0), 2)

                gann = compute_gann_levels(m_close)
                self.eq_levels[sym] = {
                    "key": ikey,
                    "mother_high": m_high,
                    "mother_low": m_low,
                    "fuel": fuel,
                    "gann": gann,
                }
                fuel_ranking.append((sym, fuel))

            if fuel_ranking:
                fuel_ranking.sort(key=lambda x: x[1], reverse=True)
                self.eq_top5_stocks = [s for s, _ in fuel_ranking[:EQ_TOP_N_STOCKS]]
                self.eq_morning_ready = True
                log.info(f"[EQUITY TOP-5 LOCKED] Selected Stocks for all 3 Equity Strategies: {self.eq_top5_stocks}")

        if not self.eq_morning_ready:
            self.eq_scan_summary = "SYNCING 09:30 DATA"
            return

        if len(self.eq_traded_today) >= EQ_MAX_TRADES_PER_DAY:
            with self.pos_lock:
                eq_open = sum(1 for p in self.active_positions.values() if p["type"] == "EQUITY_CASH")
            self.eq_scan_summary = f"TOP-5 ({','.join(self.eq_top5_stocks)}) | {eq_open} Open / {len(self.eq_traded_today)}/{EQ_MAX_TRADES_PER_DAY} Done"
            return

        if hhmm >= EQ_GANN_CUTOFF:
            self.eq_scan_summary = f"TOP-5 ({','.join(self.eq_top5_stocks)}) | CUTOFF 15:00"
            return

        top5_keys = [self.eq_levels[s]["key"] for s in self.eq_top5_stocks if s in self.eq_levels]
        quotes = fetch_ltp_quote(top5_keys)

        for sym in self.eq_top5_stocks:
            if len(self.eq_traded_today) >= EQ_MAX_TRADES_PER_DAY:
                break

            meta = self.eq_levels.get(sym)
            if not meta:
                continue
            ikey = meta["key"]
            q = quotes.get(ikey, {})
            ltp = float(q.get("last_price", 0.0))
            if ltp <= 0:
                continue

            with self.pos_lock:
                sym_open = any(p["type"] == "EQUITY_CASH" and p["symbol"] == sym for p in self.active_positions.values())
            if sym_open:
                continue

            # --- EQUITY STRATEGY 1: EQ_GANN_15M (Top 5 Stocks, 09:30 - 15:00) ---
            gann_pk = f"{sym}__EQ_GANN_15M"
            if gann_pk not in self.eq_traded_today and meta["gann"]:
                g = meta["gann"]
                if ltp >= g["BUY"]:
                    chosen = choose_gann_target(g["BUY"], g["BUY_SL"], g["BUY_TARGETS"], "BUY")
                    if chosen:
                        t_lvl, tgt_px = chosen
                        self.execute_equity_entry(sym, ikey, "BUY", ltp, g["BUY_SL"], tgt_px, now_ist, "EQ_GANN_15M", f"EQ_GANN_{t_lvl}_BUY")
                        continue
                elif ltp <= g["SELL"]:
                    chosen = choose_gann_target(g["SELL"], g["SELL_SL"], g["SELL_TARGETS"], "SELL")
                    if chosen:
                        t_lvl, tgt_px = chosen
                        self.execute_equity_entry(sym, ikey, "SELL", ltp, g["SELL_SL"], tgt_px, now_ist, "EQ_GANN_15M", f"EQ_GANN_{t_lvl}_SELL")
                        continue

            if hhmm >= EQ_ORB_ATR_CUTOFF:
                continue

            # --- EQUITY STRATEGY 2: EQ_MOTHER_ORB (Top 5 Stocks, 09:30 - 13:30) ---
            orb_pk = f"{sym}__EQ_MOTHER_ORB"
            if orb_pk not in self.eq_traded_today:
                if ltp > meta["mother_high"]:
                    self.execute_equity_entry(sym, ikey, "BUY", ltp, meta["mother_low"], 0.0, now_ist, "EQ_MOTHER_ORB", "EQ_MOTHER_ORB_BUY")
                    continue
                elif ltp < meta["mother_low"]:
                    self.execute_equity_entry(sym, ikey, "SELL", ltp, meta["mother_high"], 0.0, now_ist, "EQ_MOTHER_ORB", "EQ_MOTHER_ORB_SELL")
                    continue

            # --- EQUITY STRATEGY 3: EQ_ATR_RANGE (Top 5 Stocks, Fuel >= 50, 09:30 - 13:30) ---
            atr_pk = f"{sym}__EQ_ATR_RANGE"
            if meta["fuel"] >= EQ_MIN_FUEL_SCORE and atr_pk not in self.eq_traded_today:
                df_1m_s = fetch_intraday_candles(ikey, 1)
                df_15_s = build_resampled_15m_df(df_1m_s)
                if not df_15_s.empty and len(df_15_s) >= 2:
                    last_closed_15 = df_15_s.iloc[-2]
                    day_low = float(df_1m_s["low"].min())
                    day_high = float(df_1m_s["high"].max())
                    if float(last_closed_15["close"]) > float(last_closed_15["buy_threshold"]):
                        self.execute_equity_entry(sym, ikey, "BUY", ltp, day_low, 0.0, now_ist, "EQ_ATR_RANGE", "EQ_ATR_RANGE_BUY")
                    elif float(last_closed_15["close"]) < float(last_closed_15["sell_threshold"]):
                        self.execute_equity_entry(sym, ikey, "SELL", ltp, day_high, 0.0, now_ist, "EQ_ATR_RANGE", "EQ_ATR_RANGE_SELL")

        with self.pos_lock:
            eq_open = sum(1 for p in self.active_positions.values() if p["type"] == "EQUITY_CASH")
        self.eq_scan_summary = f"TOP-5 ({','.join(self.eq_top5_stocks)}) | {eq_open} Open / {len(self.eq_traded_today)}/{EQ_MAX_TRADES_PER_DAY} Done"

    def execute_equity_entry(self, sym: str, ikey: str, tx_side: str, ltp: float, sl_raw: float, tgt_raw: float, now_ist: datetime, strat_key: str, setup_label: str):
        pos_key = f"{sym}__{strat_key}"
        with self.pos_lock:
            if pos_key in self.active_positions or pos_key in self.eq_traded_today or len(self.eq_traded_today) >= EQ_MAX_TRADES_PER_DAY:
                return

        tick_sz = 0.05
        sl_price = round_to_tick(sl_raw, tick_sz)
        risk_per_share = (ltp - sl_price) if tx_side == "BUY" else (sl_price - ltp)
        if risk_per_share <= 0:
            return

        qty = int(EQ_MAX_RISK_INR // risk_per_share)
        if qty < 1:
            return

        target_price = round_to_tick(tgt_raw, tick_sz) if tgt_raw > 0 else 0.0
        total_est_ch = fetch_exact_trade_charges(ikey, ltp, ltp, qty, "EQUITY_CASH").get("total", 45.0)

        order_res = place_marketable_limit_order(ikey, qty, tx_side, ltp, tick_sz)
        if order_res.get("status") == "success":
            fill = order_res["fill_price"]
            self.eq_traded_today.add(pos_key)
            self.trade_counter = max(self.trade_counter, len(self.closed_trades)) + 1
            t_no = self.trade_counter
            with self.pos_lock:
                self.active_positions[pos_key] = {
                    "trade_no": t_no, "symbol": sym, "strategy_key": strat_key,
                    "type": "EQUITY_CASH", "instrument_key": ikey, "strike": "EQ",
                    "side": "CE" if tx_side == "BUY" else "PE",
                    "tx_side": tx_side, "display_side": tx_side,
                    "lots": 1, "lot_size": qty, "qty": qty, "pnl_units": qty,
                    "tick_size": tick_sz, "entry_price": fill, "r_unit": round(risk_per_share, 2),
                    "est_charges": total_est_ch, "last_ltp": fill,
                    "sl": sl_price, "target": target_price, "allow_be_trail": False,
                    "trail_stage": None, "setup_name": setup_label,
                    "entry_time": now_ist.strftime("%H:%M:%S"),
                }
            self.subscribe_tick_instrument(ikey)
            self.save_active_positions_state()
            tgt_disp = f"₹{target_price:.2f}" if target_price > 0 else "15:15 EOD"
            notify(
                f"📈 <b>NEW EQUITY ENTRY #{t_no} • {sym}</b>\n"
                f"📌 <b>Strategy:</b> <code>{setup_label}</code>\n"
                f"📊 <b>Order:</b> <code>{tx_side} {qty} Sh</code> @ <b>₹{fill:.2f}</b> | SL: <code>₹{sl_price:.2f}</code> | Tgt: <code>{tgt_disp}</code>"
            )
            self.render_live_dashboard(now_ist.strftime("%H:%M"))

    # ==========================================================================
    # F&O + MCX 3-STRATEGY EVALUATOR (1M_BO_RETEST, 5M_CONTRARIAN, VWAP_PCR_REV)
    # ==========================================================================
    def evaluate_instrument(self, idx: str, cfg: dict, now_ist: datetime):
        is_mcx = cfg["type"] == "MCX_FUT"

        fut_key = self.fut_contracts.get(idx, {}).get("instrument_key", "")
        raw_df = fetch_intraday_candles(fut_key, 1) if fut_key else pd.DataFrame()
        if raw_df.empty and not is_mcx and cfg.get("spot_key"):
            raw_df = fetch_intraday_candles(cfg["spot_key"], 1)
        if raw_df.empty:
            return None

        df_1m = add_full_indicators(raw_df)
        self.latest_dfs[idx] = df_1m

        curr_1m = df_1m.iloc[-1]
        curr_1m_ts = str(curr_1m["timestamp"])
        price_now = float(curr_1m["close"])
        vwap_1m = float(curr_1m["vwap"])
        atr14_1m = max(float(curr_1m["atr14"]), cfg.get("tick_size", 0.05) * 2)
        vol_ratio_1m = float(curr_1m["vol_ratio"])

        uptime_sec = time.time() - self.engine_boot_ts
        if uptime_sec < STARTUP_WARMUP_SEC:
            rem_w = max(1, int(STARTUP_WARMUP_SEC - uptime_sec))
            return {
                "idx": idx, "is_mcx": is_mcx,
                "spot": round(price_now, 2) if price_now < 1000 else int(round(price_now)),
                "vwap": round(vwap_1m, 2) if vwap_1m < 1000 else int(round(vwap_1m)),
                "pcr": 0.0, "vol_ratio": vol_ratio_1m, "atr": round(atr14_1m, 1),
                "short_status": f"WARMUP ({rem_w}s)",
            }

        chain, active_expiry, is_today_exp = [], "", False
        pcr = 0.0
        if not is_mcx:
            active_expiry, is_today_exp = self.get_smart_expiry(idx, now_ist)
            chain = fetch_option_chain(cfg["spot_key"], active_expiry)
            pcr, _, strong_bull_pcr, strong_bear_pcr = self.analyze_strong_pcr(idx, chain)
        else:
            strong_bull_pcr = (price_now > vwap_1m) and (vol_ratio_1m >= VWAP_MIN_VOL_RATIO)
            strong_bear_pcr = (price_now < vwap_1m) and (vol_ratio_1m >= VWAP_MIN_VOL_RATIO)

        df_5m = build_resampled_5m_df(raw_df)

        vwap_pos_key = f"{idx}__VWAP_PCR_REV"
        with self.pos_lock:
            active_vwap_pos = self.active_positions.get(vwap_pos_key)

        df_vwap_src = df_5m if (is_mcx and not df_5m.empty and len(df_5m) >= 5) else df_1m
        pat_res = detect_vwap_pattern_with_momentum(df_vwap_src)
        raw_pat_sig = pat_res["signal"]
        pat_name = pat_res["pattern"]

        if active_vwap_pos is not None:
            ltp_v = active_vwap_pos.get("last_ltp", price_now if is_mcx else active_vwap_pos["entry_price"])
            vwap_ref = float(df_vwap_src.iloc[-1]["vwap"])
            price_ref = float(df_vwap_src.iloc[-1]["close"])
            if active_vwap_pos["side"] == "PE" and raw_pat_sig == "PE" and strong_bear_pcr and price_ref < vwap_ref:
                self.close_position_now(vwap_pos_key, ltp_v, "REVERSAL_TO_CE", now_ist, is_reversal=True)
            elif active_vwap_pos["side"] == "CE" and raw_pat_sig == "CE" and strong_bull_pcr and price_ref > vwap_ref:
                self.close_position_now(vwap_pos_key, ltp_v, "REVERSAL_TO_PE", now_ist, is_reversal=True)

        in_window = self.is_within_trade_window(idx, now_ist)
        if not in_window:
            self.pending_retests[idx] = None
            with self.pos_lock:
                open_cnt = sum(1 for p in self.active_positions.values() if p["symbol"] == idx)
            slot_txt = "21:00-22:00" if is_mcx else "09:10-15:00"
            st_lbl = f"ACTIVE({open_cnt}) | SLOT {slot_txt}" if open_cnt > 0 else f"SLOT {slot_txt}"
            return {
                "idx": idx, "is_mcx": is_mcx,
                "spot": round(price_now, 2) if price_now < 1000 else int(round(price_now)),
                "vwap": round(vwap_1m, 2) if vwap_1m < 1000 else int(round(vwap_1m)),
                "pcr": pcr, "vol_ratio": vol_ratio_1m, "atr": round(atr14_1m, 1),
                "short_status": st_lbl,
            }

        # STRATEGY 1: 1M_BO_RETEST
        if self.can_trigger_strategy(idx, "1M_BO_RETEST", curr_1m_ts):
            pending = self.pending_retests.get(idx)
            if pending:
                bars_waited = int((now_ist - pending["armed_at"]).total_seconds() // 60)
                if bars_waited > BO_RETEST_MAX_BARS:
                    self.pending_retests[idx] = None
                    pending = None

            if pending is not None and curr_1m_ts != pending["armed_ts"]:
                lvl = float(pending["level"])
                buf = BO_RETEST_BUFFER_ATR * float(pending["atr"])
                retest_ok = False
                if pending["side"] == "CE" and (lvl - buf) <= float(curr_1m["low"]) <= (lvl + buf) and float(curr_1m["close"]) > float(curr_1m["open"]):
                    retest_ok = True
                elif pending["side"] == "PE" and (lvl - buf) <= float(curr_1m["high"]) <= (lvl + buf) and float(curr_1m["close"]) < float(curr_1m["open"]):
                    retest_ok = True

                if retest_ok:
                    self.dispatch_standard_entry(
                        idx=idx, cfg=cfg, side=pending["side"], fut_price=price_now,
                        atr14=float(pending["atr"]), chain=chain, is_today_exp=is_today_exp,
                        now_ist=now_ist, strat_key="1M_BO_RETEST",
                        setup_detail="1M_BO_RETEST", rrr_mult=BO_RRR_TARGET,
                    )
            elif pending is None:
                c_range_1m = float(curr_1m["high"]) - float(curr_1m["low"])
                c_body_1m = abs(float(curr_1m["close"]) - float(curr_1m["open"]))
                b_ratio_1m = c_body_1m / (c_range_1m if c_range_1m > 0 else 1e-9)
                if (vol_ratio_1m >= BO_VOL_RATIO_MIN or c_range_1m >= BO_RANGE_EXPANSION_ATR * atr14_1m) and b_ratio_1m >= BO_BODY_RATIO_MIN:
                    if float(curr_1m["close"]) > float(curr_1m["open"]):
                        self.pending_retests[idx] = {"side": "CE", "level": float(curr_1m["high"]), "atr": atr14_1m, "armed_at": now_ist, "armed_ts": curr_1m_ts}
                    elif float(curr_1m["close"]) < float(curr_1m["open"]):
                        self.pending_retests[idx] = {"side": "PE", "level": float(curr_1m["low"]), "atr": atr14_1m, "armed_at": now_ist, "armed_ts": curr_1m_ts}
        else:
            self.pending_retests[idx] = None

        # STRATEGY 2: 5M_CONTRARIAN
        if not df_5m.empty and len(df_5m) >= 10:
            bar_5m = df_5m.iloc[-2]
            bar_5m_ts = str(bar_5m["timestamp"])
            if bar_5m_ts != self.last_rev_5m_ts.get(idx, "") and self.can_trigger_strategy(idx, "5M_CONTRARIAN", bar_5m_ts):
                self.last_rev_5m_ts[idx] = bar_5m_ts
                atr14_5m = max(float(bar_5m["atr14"]), cfg.get("tick_size", 0.05) * 2)
                vol_5m = float(bar_5m["vol_ratio"])
                rng_5m = float(bar_5m["high"]) - float(bar_5m["low"])
                body_5m = abs(float(bar_5m["close"]) - float(bar_5m["open"]))
                br_5m = body_5m / (rng_5m if rng_5m > 0 else 1e-9)

                bull_5m = float(bar_5m["close"]) > float(bar_5m["ema20"]) > float(bar_5m["ema50"])
                bear_5m = float(bar_5m["close"]) < float(bar_5m["ema20"]) < float(bar_5m["ema50"])

                if vol_5m >= REV_VOL_RATIO_MIN and br_5m >= REV_BODY_RATIO_MIN:
                    if bull_5m and float(bar_5m["close"]) > float(bar_5m["open"]):
                        self.dispatch_standard_entry(
                            idx=idx, cfg=cfg, side="PE", fut_price=price_now,
                            atr14=atr14_5m, chain=chain, is_today_exp=is_today_exp,
                            now_ist=now_ist, strat_key="5M_CONTRARIAN",
                            setup_detail="5M_CONTRARIAN_REV", rrr_mult=REV_RRR_TARGET,
                        )
                    elif bear_5m and float(bar_5m["close"]) < float(bar_5m["open"]):
                        self.dispatch_standard_entry(
                            idx=idx, cfg=cfg, side="CE", fut_price=price_now,
                            atr14=atr14_5m, chain=chain, is_today_exp=is_today_exp,
                            now_ist=now_ist, strat_key="5M_CONTRARIAN",
                            setup_detail="5M_CONTRARIAN_REV", rrr_mult=REV_RRR_TARGET,
                        )

        # STRATEGY 3: VWAP_PCR_REV
        vwap_bar_ts = str(df_vwap_src.iloc[-1]["timestamp"])
        if raw_pat_sig in ("CE", "PE") and vwap_bar_ts != self.last_vwap_bar_ts.get(idx, ""):
            if self.can_trigger_strategy(idx, "VWAP_PCR_REV", vwap_bar_ts):
                atr_vwap = max(float(df_vwap_src.iloc[-1]["atr14"]), cfg.get("tick_size", 0.05) * 2)
                if raw_pat_sig == "CE" and strong_bull_pcr:
                    self.last_vwap_bar_ts[idx] = vwap_bar_ts
                    self.dispatch_vwap_reversed_entry(
                        idx=idx, cfg=cfg, flipped_side="PE", pattern_name=pat_name,
                        df_src=df_vwap_src, fut_price=price_now, atr14=atr_vwap,
                        chain=chain, is_today_exp=is_today_exp, now_ist=now_ist,
                    )
                elif raw_pat_sig == "PE" and strong_bear_pcr:
                    self.last_vwap_bar_ts[idx] = vwap_bar_ts
                    self.dispatch_vwap_reversed_entry(
                        idx=idx, cfg=cfg, flipped_side="CE", pattern_name=pat_name,
                        df_src=df_vwap_src, fut_price=price_now, atr14=atr_vwap,
                        chain=chain, is_today_exp=is_today_exp, now_ist=now_ist,
                    )

        with self.pos_lock:
            sym_active = [p["strategy_key"] for p in self.active_positions.values() if p["symbol"] == idx]

        if sym_active:
            short_status = f"ACTIVE [{','.join(s.split('_')[0] for s in sym_active)}]"
        elif self.pending_retests.get(idx):
            short_status = f"ARMED 1m {self.pending_retests[idx]['side']} RT"
        else:
            short_status = "SCAN ALL 3"

        return {
            "idx": idx, "is_mcx": is_mcx,
            "spot": round(price_now, 2) if price_now < 1000 else int(round(price_now)),
            "vwap": round(vwap_1m, 2) if vwap_1m < 1000 else int(round(vwap_1m)),
            "pcr": pcr, "vol_ratio": vol_ratio_1m, "atr": round(atr14_1m, 1),
            "short_status": short_status,
        }

    def dispatch_standard_entry(self, idx, cfg, side, fut_price, atr14, chain, is_today_exp, now_ist, strat_key, setup_detail, rrr_mult):
        pos_key = f"{idx}__{strat_key}"
        with self.pos_lock:
            if pos_key in self.active_positions:
                return

        if cfg["type"] == "MCX_FUT":
            tx_side = "BUY" if side == "CE" else "SELL"
            inst_key = cfg["spot_key"]
            lot_size = int(cfg["lot_size"])
            pnl_mult = int(cfg.get("pnl_multiplier", lot_size))
            tick_sz = cfg.get("tick_size", 0.05)

            ltp = float(fetch_ltp_quote([inst_key]).get(inst_key, {}).get("last_price", fut_price))
            min_sl = round_to_tick(ltp * 0.0015, tick_sz)
            sl_points = max(round_to_tick(atr14 * ATR_SL_MULT, tick_sz), min_sl, tick_sz * 4)
            target_points = round_to_tick(sl_points * rrr_mult, tick_sz)

            if tx_side == "BUY":
                sl_price = round_to_tick(ltp - sl_points, tick_sz)
                target_price = round_to_tick(ltp + target_points, tick_sz)
                opt_side = "CE"
            else:
                sl_price = round_to_tick(ltp + sl_points, tick_sz)
                target_price = round_to_tick(ltp - target_points, tick_sz)
                opt_side = "PE"

            qty = min(lot_size, cfg["freeze_qty"])
            total_est_ch = fetch_exact_trade_charges(inst_key, ltp, ltp, pnl_mult, "MCX_FUT").get("total", 180.0)
            full_setup = f"{setup_detail}_{tx_side}"

            order_res = place_marketable_limit_order(inst_key, qty, tx_side, ltp, tick_sz)
            if order_res.get("status") == "success":
                fill = order_res["fill_price"]
                sym = self.fut_contracts.get(idx, {}).get("trading_symbol", f"{idx} FUT")
                self.trade_counter = max(self.trade_counter, len(self.closed_trades)) + 1
                t_no = self.trade_counter
                with self.pos_lock:
                    self.active_positions[pos_key] = {
                        "trade_no": t_no, "symbol": idx, "strategy_key": strat_key,
                        "type": "MCX_FUT", "instrument_key": inst_key, "strike": sym,
                        "side": opt_side, "tx_side": tx_side, "display_side": tx_side,
                        "lots": 1, "lot_size": lot_size, "qty": qty, "pnl_units": pnl_mult,
                        "tick_size": tick_sz, "entry_price": fill, "r_unit": sl_points,
                        "est_charges": total_est_ch, "last_ltp": fill,
                        "sl": sl_price, "target": target_price, "allow_be_trail": True,
                        "trail_stage": None, "setup_name": full_setup,
                        "entry_time": now_ist.strftime("%H:%M:%S"),
                    }
                if strat_key == "1M_BO_RETEST":
                    self.pending_retests[idx] = None
                self.subscribe_tick_instrument(inst_key)
                self.save_active_positions_state()
                notify(
                    f"🚀 <b>NEW MCX ENTRY #{t_no} • {idx}</b>\n"
                    f"📌 <b>Strategy:</b> <code>{full_setup}</code>\n"
                    f"📊 <b>Contract:</b> <code>{sym} {tx_side}</code> @ <b>₹{fill:.2f}</b> | SL: <code>₹{sl_price:.2f}</code> | Tgt: <code>₹{target_price:.2f}</code>"
                )
                self.render_live_dashboard(now_ist.strftime("%H:%M"))
        else:
            if not chain:
                return
            step = cfg.get("step", 50)
            tick_sz = cfg.get("tick_size", 0.05)
            opt_field = "call_options" if side == "CE" else "put_options"
            target_strike = round(fut_price / step) * step
            chosen = next((row.get(opt_field, {}) for row in chain if row.get("strike_price") == target_strike), None)
            if not chosen:
                valid_rows = [row for row in chain if row.get(opt_field, {}).get("instrument_key")]
                if not valid_rows:
                    return
                nearest_row = min(valid_rows, key=lambda r: abs(float(r.get("strike_price", 0)) - fut_price))
                target_strike = nearest_row.get("strike_price", target_strike)
                chosen = nearest_row.get(opt_field, {})

            inst_key = chosen.get("instrument_key")
            if not inst_key:
                return

            mkt = chosen.get("market_data", {})
            greeks = chosen.get("option_greeks", {})
            ltp = float(mkt.get("ltp", 0))
            if ltp <= 0:
                ltp = float(fetch_ltp_quote([inst_key]).get(inst_key, {}).get("last_price", 0.0))
            if ltp <= 0:
                return

            raw_delta = abs(float(greeks.get("delta", 0.0)))
            delta = min(0.75, max(0.35, raw_delta)) if raw_delta > 0 else cfg.get("default_delta", 0.52)
            calc_sl_pts = atr14 * ATR_SL_MULT * delta
            min_sl_pts = float(cfg.get("min_sl_pts", 10.0))
            sl_points = max(round_to_tick(max(min_sl_pts, calc_sl_pts), tick_sz), tick_sz * 4)
            target_points = round_to_tick(sl_points * rrr_mult, tick_sz)

            sl_price = round_to_tick(max(tick_sz, ltp - sl_points), tick_sz)
            target_price = round_to_tick(ltp + target_points, tick_sz)

            qty = min(int(cfg["lot_size"]), cfg["freeze_qty"])
            total_est_ch = fetch_exact_trade_charges(inst_key, ltp, ltp, qty, "INDEX_OPT").get("total", 65.0)
            full_setup = f"{setup_detail}_{side}"

            order_res = place_marketable_limit_order(inst_key, qty, "BUY", ltp, tick_sz)
            if order_res.get("status") == "success":
                fill = order_res["fill_price"]
                self.trade_counter = max(self.trade_counter, len(self.closed_trades)) + 1
                t_no = self.trade_counter
                with self.pos_lock:
                    self.active_positions[pos_key] = {
                        "trade_no": t_no, "symbol": idx, "strategy_key": strat_key,
                        "type": "INDEX_OPT", "instrument_key": inst_key,
                        "strike": target_strike, "side": side, "tx_side": "BUY",
                        "display_side": side, "lots": 1, "lot_size": int(cfg["lot_size"]),
                        "qty": qty, "pnl_units": qty, "tick_size": tick_sz,
                        "entry_price": fill, "r_unit": sl_points, "est_charges": total_est_ch,
                        "last_ltp": fill, "sl": sl_price, "target": target_price,
                        "allow_be_trail": True, "trail_stage": None,
                        "setup_name": full_setup, "entry_time": now_ist.strftime("%H:%M:%S"),
                    }
                if strat_key == "1M_BO_RETEST":
                    self.pending_retests[idx] = None
                self.subscribe_tick_instrument(inst_key)
                self.save_active_positions_state()
                notify(
                    f"🚀 <b>NEW INDEX ENTRY #{t_no} • {idx}</b>\n"
                    f"📌 <b>Strategy:</b> <code>{full_setup}</code>\n"
                    f"📊 <b>Option:</b> <code>{target_strike} {side}</code> @ <b>₹{fill:.2f}</b> | SL: <code>₹{sl_price:.2f}</code> | Tgt: <code>₹{target_price:.2f}</code>"
                )
                self.render_live_dashboard(now_ist.strftime("%H:%M"))

    def dispatch_vwap_reversed_entry(self, idx, cfg, flipped_side, pattern_name, df_src, fut_price, atr14, chain, is_today_exp, now_ist):
        pos_key = f"{idx}__VWAP_PCR_REV"
        with self.pos_lock:
            if pos_key in self.active_positions:
                return

        swing_low, swing_high = find_latest_swing_levels(df_src, lookback=10)
        buf = SWING_BUFFER_ATR * atr14

        if cfg["type"] == "MCX_FUT":
            inst_key = cfg["spot_key"]
            lot_size = int(cfg["lot_size"])
            pnl_mult = int(cfg.get("pnl_multiplier", lot_size))
            tick_sz = cfg.get("tick_size", 0.05)
            tx_side = "BUY" if flipped_side == "CE" else "SELL"

            ltp = float(fetch_ltp_quote([inst_key]).get(inst_key, {}).get("last_price", fut_price))
            min_dist = max(round_to_tick(ltp * 0.0012, tick_sz), tick_sz * 4)

            if tx_side == "BUY":
                raw_swing_dist = max(round_to_tick((swing_high + buf) - ltp, tick_sz), min_dist)
                target_points = raw_swing_dist
                sl_points = round_to_tick(raw_swing_dist * 2.0, tick_sz)
                sl_price = round_to_tick(ltp - sl_points, tick_sz)
                target_price = round_to_tick(ltp + target_points, tick_sz)
            else:
                raw_swing_dist = max(round_to_tick(ltp - (swing_low - buf), tick_sz), min_dist)
                target_points = raw_swing_dist
                sl_points = round_to_tick(raw_swing_dist * 2.0, tick_sz)
                sl_price = round_to_tick(ltp + sl_points, tick_sz)
                target_price = round_to_tick(ltp - target_points, tick_sz)

            qty = min(lot_size, cfg["freeze_qty"])
            total_est_ch = fetch_exact_trade_charges(inst_key, ltp, ltp, pnl_mult, "MCX_FUT").get("total", 180.0)
            full_setup = f"VWAP_PCR_REV_{pattern_name}_{tx_side}"

            order_res = place_marketable_limit_order(inst_key, qty, tx_side, ltp, tick_sz)
            if order_res.get("status") == "success":
                fill = order_res["fill_price"]
                sym = self.fut_contracts.get(idx, {}).get("trading_symbol", f"{idx} FUT")
                self.trade_counter = max(self.trade_counter, len(self.closed_trades)) + 1
                t_no = self.trade_counter
                with self.pos_lock:
                    self.active_positions[pos_key] = {
                        "trade_no": t_no, "symbol": idx, "strategy_key": "VWAP_PCR_REV",
                        "type": "MCX_FUT", "instrument_key": inst_key, "strike": sym,
                        "side": flipped_side, "tx_side": tx_side, "display_side": tx_side,
                        "lots": 1, "lot_size": lot_size, "qty": qty, "pnl_units": pnl_mult,
                        "tick_size": tick_sz, "entry_price": fill, "r_unit": target_points,
                        "est_charges": total_est_ch, "last_ltp": fill,
                        "sl": sl_price, "target": target_price, "allow_be_trail": False,
                        "trail_stage": None, "setup_name": full_setup,
                        "entry_time": now_ist.strftime("%H:%M:%S"),
                    }
                self.subscribe_tick_instrument(inst_key)
                self.save_active_positions_state()
                notify(
                    f"🔄 <b>NEW MCX ENTRY #{t_no} • {idx}</b>\n"
                    f"📌 <b>Strategy:</b> <code>{full_setup}</code>\n"
                    f"📊 <b>Contract:</b> <code>{sym} {tx_side}</code> @ <b>₹{fill:.2f}</b> | SL (2R): <code>₹{sl_price:.2f}</code> | Tgt (1R): <code>₹{target_price:.2f}</code>"
                )
                self.render_live_dashboard(now_ist.strftime("%H:%M"))
        else:
            if not chain:
                return
            step = cfg.get("step", 50)
            tick_sz = cfg.get("tick_size", 0.05)
            opt_field = "call_options" if flipped_side == "CE" else "put_options"
            target_strike = round(fut_price / step) * step
            chosen = next((row.get(opt_field, {}) for row in chain if row.get("strike_price") == target_strike), None)
            if not chosen:
                valid_rows = [row for row in chain if row.get(opt_field, {}).get("instrument_key")]
                if not valid_rows:
                    return
                nearest_row = min(valid_rows, key=lambda r: abs(float(r.get("strike_price", 0)) - fut_price))
                target_strike = nearest_row.get("strike_price", target_strike)
                chosen = nearest_row.get(opt_field, {})

            inst_key = chosen.get("instrument_key")
            if not inst_key:
                return

            mkt = chosen.get("market_data", {})
            greeks = chosen.get("option_greeks", {})
            ltp = float(mkt.get("ltp", 0))
            if ltp <= 0:
                ltp = float(fetch_ltp_quote([inst_key]).get(inst_key, {}).get("last_price", 0.0))
            if ltp <= 0:
                return

            if flipped_side == "PE":
                fut_old_sl = round(swing_low - buf, 2)
                fut_risk = max(fut_price - fut_old_sl, 0.5 * atr14)
            else:
                fut_old_sl = round(swing_high + buf, 2)
                fut_risk = max(fut_old_sl - fut_price, 0.5 * atr14)

            raw_delta = abs(float(greeks.get("delta", 0.0)))
            live_delta = min(0.75, max(0.35, raw_delta)) if raw_delta > 0 else cfg.get("default_delta", 0.52)

            base_opt_pts = min(max(fut_risk * live_delta, ltp * 0.10), ltp * 0.30)
            target_points = max(round_to_tick(base_opt_pts, tick_sz), tick_sz * 4)
            sl_points = round_to_tick(target_points * 2.0, tick_sz)

            sl_price = round_to_tick(max(tick_sz, ltp - sl_points), tick_sz)
            target_price = round_to_tick(ltp + target_points, tick_sz)

            qty = min(int(cfg["lot_size"]), cfg["freeze_qty"])
            total_est_ch = fetch_exact_trade_charges(inst_key, ltp, ltp, qty, "INDEX_OPT").get("total", 65.0)
            full_setup = f"VWAP_PCR_REV_{pattern_name}_{flipped_side}"

            order_res = place_marketable_limit_order(inst_key, qty, "BUY", ltp, tick_sz)
            if order_res.get("status") == "success":
                fill = order_res["fill_price"]
                self.trade_counter = max(self.trade_counter, len(self.closed_trades)) + 1
                t_no = self.trade_counter
                with self.pos_lock:
                    self.active_positions[pos_key] = {
                        "trade_no": t_no, "symbol": idx, "strategy_key": "VWAP_PCR_REV",
                        "type": "INDEX_OPT", "instrument_key": inst_key,
                        "strike": target_strike, "side": flipped_side, "tx_side": "BUY",
                        "display_side": flipped_side, "lots": 1, "lot_size": int(cfg["lot_size"]),
                        "qty": qty, "pnl_units": qty, "tick_size": tick_sz,
                        "entry_price": fill, "r_unit": target_points, "est_charges": total_est_ch,
                        "last_ltp": fill, "sl": sl_price, "target": target_price,
                        "allow_be_trail": False, "trail_stage": None,
                        "setup_name": full_setup, "entry_time": now_ist.strftime("%H:%M:%S"),
                    }
                self.subscribe_tick_instrument(inst_key)
                self.save_active_positions_state()
                notify(
                    f"🔄 <b>NEW INDEX ENTRY #{t_no} • {idx}</b>\n"
                    f"📌 <b>Strategy:</b> <code>{full_setup}</code>\n"
                    f"📊 <b>Option:</b> <code>{target_strike} {flipped_side}</code> @ <b>₹{fill:.2f}</b> | SL (2R): <code>₹{sl_price:.2f}</code> | Tgt (1R): <code>₹{target_price:.2f}</code>"
                )
                self.render_live_dashboard(now_ist.strftime("%H:%M"))

    def manage_open_positions(self, now_ist: datetime):
        with self.pos_lock:
            if not self.active_positions:
                return
            pos_items = list(self.active_positions.items())

        hhmm = now_ist.strftime("%H:%M")
        nse_exit_t = self.session_sched.get("nse_exit", INDEX_EXIT_TIME)
        mcx_exit_t = self.session_sched.get("mcx_exit", MCX_EXIT_TIME)
        quotes = fetch_ltp_quote(list({p["instrument_key"] for _, p in pos_items})) if not self.ws_connected else {}

        for pos_key, pos in pos_items:
            if not self.ws_connected:
                ltp = float(quotes.get(pos["instrument_key"], {}).get("last_price", pos.get("last_ltp", pos["entry_price"])))
                pos["last_ltp"] = ltp
            else:
                ltp = pos.get("last_ltp", pos["entry_price"])

            is_short = pos["tx_side"] == "SELL"
            tgt = float(pos.get("target", 0.0))
            exit_reason = None

            if pos["type"] in ("INDEX_OPT", "EQUITY_CASH") and hhmm >= nse_exit_t:
                exit_reason = "MUHURAT_EOD_EXIT" if self.session_sched.get("is_muhurat") else "EOD_EXIT_1515"
            elif pos["type"] == "MCX_FUT" and hhmm >= mcx_exit_t:
                exit_reason = "MCX_EOD_EXIT_2315"
            elif tgt > 0 and ((not is_short and ltp >= tgt) or (is_short and ltp <= tgt)):
                exit_reason = "TARGET_HIT"
            elif not is_short and ltp <= pos["sl"]:
                exit_reason = "TRAIL_BE_SL_HIT" if pos.get("trail_stage") else "STOP_LOSS_HIT"
            elif is_short and ltp >= pos["sl"]:
                exit_reason = "TRAIL_BE_SL_HIT" if pos.get("trail_stage") else "STOP_LOSS_HIT"

            if exit_reason:
                self.close_position_now(pos_key, ltp, exit_reason, now_ist)

    def shutdown_cleanly(self, now_ist: datetime, reason_label: str):
        if reason_label == "MANUAL STOP":
            with self.pos_lock:
                open_items = list(self.active_positions.items())
            if open_items:
                quotes = fetch_ltp_quote(list({p["instrument_key"] for _, p in open_items}))
                for pos_key, pos in open_items:
                    exit_ltp = float(quotes.get(pos["instrument_key"], {}).get("last_price", pos.get("last_ltp", pos["entry_price"])))
                    self.close_position_now(pos_key, exit_ltp, "MANUAL_STOP_EXIT", now_ist)

        self.save_active_positions_state()
        self.bot_status_text = "MARKET CLOSED"
        if self.streamer:
            try:
                self.streamer.disconnect()
            except Exception:
                pass
            self.streamer = None
            self.ws_connected = False

        self.render_live_dashboard(now_ist.strftime("%H:%M"))
        if reason_label != "SESSION HANDOVER":
            notify(
                f"🔴 <b>SESSION CLOSED ({reason_label})</b>  |  <code>{now_ist.strftime('%H:%M')} IST</code>\n"
                f"────────────────────────────\n"
                f"💼 <b>Day Net P&amp;L:</b> <b>₹{self.day_pnl:+,.2f}</b>  |  <b>Trades:</b> {len(self.closed_trades)}"
            )
        self.release_single_instance_lock()

    def run(self):
        if not UPSTOX_ACCESS_TOKEN:
            log.error("UPSTOX_ACCESS_TOKEN missing! Exiting.")
            sys.exit(1)

        if not self.acquire_single_instance_lock():
            return

        now_ist = datetime.now(IST)
        if is_overnight_dead_hours(now_ist):
            log.info(f"Outside market hours ({now_ist.strftime('%H:%M')} IST). Standing down cleanly.")
            self.release_single_instance_lock()
            return

        if now_ist.weekday() >= 5:
            wk_label = "SATURDAY (WEEKEND)" if now_ist.weekday() == 5 else "SUNDAY (WEEKEND)"
            self.bot_status_text = f"MARKET CLOSED ({wk_label})"
            notify(
                f"🔴 <b>MARKET CLOSED • {wk_label}</b>\n"
                f"────────────────────────────\n"
                f"📅 <b>Date:</b> <code>{now_ist.strftime('%d-%b-%Y (%A)')}</code>\n"
                f"💤 <i>No live trading session today. Engine standing down cleanly.</i>"
            )
            self.release_single_instance_lock()
            return

        today_clean = now_ist.strftime("%Y-%m-%d")
        self.session_sched = inspect_special_session_schedule(now_ist)
        nifty_live_df = fetch_intraday_candles(INDICES["NIFTY"]["spot_key"], 5)
        nse_has_live_candles = not nifty_live_df.empty and str(nifty_live_df["timestamp"].iloc[-1])[:10] == today_clean

        if self.session_sched["is_holiday"] and not nse_has_live_candles:
            if not self.session_sched["nse_open"] and not self.session_sched["mcx_open"]:
                hol_txt = self.session_sched["description"] or "EXCHANGE HOLIDAY"
                self.bot_status_text = f"MARKET CLOSED ({hol_txt})"
                notify(
                    f"🔴 <b>MARKET CLOSED • {hol_txt}</b>\n"
                    f"────────────────────────────\n"
                    f"📅 <b>Date:</b> <code>{now_ist.strftime('%d-%b-%Y (%A)')}</code>\n"
                    f"💤 <i>Both NSE/BSE and MCX closed today. Standing down cleanly.</i>"
                )
                self.release_single_instance_lock()
                return
        elif nse_has_live_candles:
            self.session_sched["is_holiday"] = False
            self.session_sched["nse_open"] = True
            self.session_sched["mcx_open"] = True
            self.session_sched["mcx_start"] = MCX_WINDOWS[0][0]

        signal.signal(signal.SIGTERM, lambda s, f: setattr(self, "stop_requested", True))
        signal.signal(signal.SIGINT, lambda s, f: setattr(self, "stop_requested", True))

        self.load_today_state_from_journal()
        self.init_contracts()
        self.load_active_positions_state()
        self.bot_status_text = "BOT LIVE"
        session_start_ts = time.time()
        self.engine_boot_ts = session_start_ts
        holiday_checked_0922 = False

        notify(
            f"🌅 <b>V3 MULTI-ASSET ENGINE • ONLINE (4 PINNED BOARDS)</b>\n"
            f"────────────────────────────\n"
            f"🔑 <b>Token:</b> Active  |  ⚡ <b>Mode:</b> <code>{'PAPER' if PAPER_MODE else 'LIVE'}</code>\n"
            f"📌 <b>Boards:</b> <code>[1/4] Consolidated Master</code> + <code>[2/4] Index</code> + <code>[3/4] MCX</code> + <code>[4/4] Top-5 Equity</code>"
        )
        self.dash_ids = {"master": None, "index": None, "mcx": None, "equity": None}
        self.start_heartbeat_thread()

        while True:
            now_ist = datetime.now(IST)
            hhmm = now_ist.strftime("%H:%M")

            if self.stop_requested:
                stop_type = "SESSION HANDOVER" if is_scheduled_handover_window(now_ist) else "MANUAL STOP"
                self.shutdown_cleanly(now_ist, stop_type)
                break

            if (time.time() - session_start_ts) >= SESSION_MAX_RUNTIME_SEC:
                self.shutdown_cleanly(now_ist, "SESSION HANDOVER")
                break

            if not holiday_checked_0922 and self.session_sched["nse_open"] and not self.session_sched["is_muhurat"] and "09:22" <= hhmm <= "09:35":
                holiday_checked_0922 = True
                nifty_check_df = fetch_intraday_candles(INDICES["NIFTY"]["spot_key"], 5)
                today_s = now_ist.strftime("%Y-%m-%d")
                has_today_candle = not nifty_check_df.empty and str(nifty_check_df["timestamp"].iloc[-1])[:10] == today_s
                nse_live_now = check_live_exchange_status("NSE")
                if not has_today_candle and nse_live_now in ("CLOSED", "CLOSING", ""):
                    self.session_sched["is_holiday"] = True
                    self.session_sched["nse_open"] = False
                    self.session_sched["mcx_open"] = True
                    self.session_sched["mcx_start"] = MCX_WINDOWS[0][0]
                    notify(
                        f"🟡 <b>NSE/BSE HOLIDAY ({today_s}) • MCX EVENING ARMED</b>\n"
                        f"────────────────────────────\n"
                        f"🛢️ <i>Index &amp; Equity disabled; MCX commodity trading will auto-start at 21:00 IST.</i>"
                    )

            self.manage_open_positions(now_ist)

            if hhmm >= "23:35":
                self.shutdown_cleanly(now_ist, "EOD CLOSED")
                break

            if not self.session_sched["nse_open"] and hhmm < self.session_sched.get("mcx_start", "21:00"):
                self.bot_status_text = f"STANDBY (MCX OPENS {self.session_sched.get('mcx_start', '21:00')})"
            elif self.session_sched["is_muhurat"] and hhmm < self.session_sched.get("nse_start", "18:00") and hhmm < self.session_sched.get("mcx_start", "21:00"):
                self.bot_status_text = f"STANDBY (MUHURAT {self.session_sched.get('nse_start', '18:00')})"
            else:
                self.bot_status_text = "BOT LIVE"

            for idx, cfg in INDICES.items():
                is_mcx = cfg["type"] == "MCX_FUT"
                if not is_mcx:
                    nse_closed_now = (not self.session_sched["nse_open"]) or (hhmm > self.session_sched.get("nse_exit", "15:20"))
                    if nse_closed_now:
                        if idx not in self.last_summaries:
                            spot_df = fetch_intraday_candles(cfg["spot_key"], 5)
                            if not spot_df.empty:
                                close_v = int(round(float(spot_df["close"].iloc[-1])))
                                self.last_summaries[idx] = {
                                    "idx": idx, "is_mcx": False, "spot": close_v, "vwap": close_v,
                                    "pcr": 0.0, "vol_ratio": 0.0, "atr": 0.0,
                                    "short_status": "HOLIDAY" if not self.session_sched["nse_open"] else "CLOSED",
                                }
                        continue
                res = self.evaluate_instrument(idx, cfg, now_ist)
                if res:
                    self.last_summaries[idx] = res

            self.evaluate_equity_strategies(now_ist)

            if not self.dash_ids.get("master"):
                self.render_live_dashboard(hhmm)

            time.sleep(8)


if __name__ == "__main__":
    engine = IntradayV3Engine()
    engine.run()
