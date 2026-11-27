import os
import io
import csv
import sys
import gzip
import json
import time
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
# 1. CONFIGURATION & GOOGLE SHEETS SYNC (INDEX + MCX ONLY — 2 STRATEGIES)
# ==============================================================================
IST = timezone(timedelta(hours=5, minutes=30))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("V3_INDEX_MCX_ENGINE")

UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GSHEET_WEBHOOK_URL = os.getenv("GSHEET_WEBHOOK_URL", "").strip()
PAPER_MODE = os.getenv("PAPER_MODE", "true").strip().lower() == "true"

# Session & Prime Time Slots (IST)
SESSION_MAX_RUNTIME_SEC = 20700
INDEX_WINDOWS = (("09:15", "15:00"),)
INDEX_EXIT_TIME = "15:15"

MCX_WINDOWS = (("09:15", "21:00"),)
MCX_EXIT_TIME = "23:15"

REENTRY_COOLDOWN_SEC = 300
WHIPSAW_COOLDOWN_SEC = 1800     # 30 min pause after 2 consecutive SLs on same symbol
WHIPSAW_MAX_VWAP_FLIPS = 3      # >= 3 VWAP crosses in last 12 x 5m bars (1 hr) = Chop/Whipsaw
NO_MOMENTUM_STALL_BARS = 3      # 3 closed 5m bars (15 min) with no progress + premium decay -> early exit
STARTUP_WARMUP_SEC = 30
DASHBOARD_PULSE_SEC = 10.0

# Strategy 1: 5-Minute VWAP Marubozu Arm + Rejection Entry + Fixed 1:2 Target (1R -> Cost-to-Cost)
MARU_BODY_MIN = 0.60
MARU_VOL_MIN = 1.10
MARU_SL_ATR_BUF = 0.15
MARU_MAX_ARM_BARS = 30          # Up to 2.5 hours (30 x 5m bars)
MARU_RRR_TARGET = 2.00          # Fixed 1:2 Target (e.g., Fut SL 30 * 0.5 Delta = 15 SL -> 30 Target)

# Strategy 2: 5-Minute Contrarian EMA Trend (Requires 1 Opposite Confirmation Candle)
REV_VOL_RATIO_MIN = 1.30
REV_BODY_RATIO_MIN = 0.50
REV_RRR_TARGET = 2.00           # Fixed 1:2 Target (1R -> Cost-to-Cost)

ATR_SL_MULT = 1.20
DEFAULT_ATM_DELTA = 0.50
MIN_OPTION_PREMIUM = 25.0

JOURNAL_FILE = "live_journal/trade_journal.csv"
ACTIVE_STATE_FILE = "live_journal/active_positions.json"
DASHBOARD_STATE_FILE = "live_journal/dashboard_state_clean.json"
LOCK_FILE = "live_journal/engine.lock"

JOURNAL_FIELDNAMES = [
    "trade_no", "date", "entry_time", "exit_time", "symbol", "contract", "side",
    "qty", "pnl_units", "entry_price", "exit_price", "peak_price", "max_r",
    "points", "sl", "target", "exit_reason",
    "gross_pnl", "brokerage", "taxes_other", "charges", "pnl", "day_pnl", "setup",
]

ALL_STRATEGY_KEYS = (
    "5M_VWAP_MARU",
    "5M_CONTRARIAN",
)

INDICES = {
    "NIFTY":      {"type": "INDEX_OPT", "spot_key": "NSE_INDEX|Nifty 50", "default_delta": DEFAULT_ATM_DELTA},
    "BANKNIFTY":  {"type": "INDEX_OPT", "spot_key": "NSE_INDEX|Nifty Bank", "default_delta": DEFAULT_ATM_DELTA},
    "FINNIFTY":   {"type": "INDEX_OPT", "spot_key": "NSE_INDEX|Nifty Fin Service", "default_delta": DEFAULT_ATM_DELTA},
    "MIDCPNIFTY": {"type": "INDEX_OPT", "spot_key": "NSE_INDEX|NIFTY MID SELECT", "default_delta": DEFAULT_ATM_DELTA},
    "SENSEX":     {"type": "INDEX_OPT", "spot_key": "BSE_INDEX|SENSEX", "default_delta": DEFAULT_ATM_DELTA},
    "CRUDEOIL":   {"type": "MCX_FUT"},
    "NATURALGAS": {"type": "MCX_FUT"},
    "GOLD":       {"type": "MCX_FUT"},
    "SILVER":     {"type": "MCX_FUT"},
}

MCX_MINI_SYMBOLS = {
    "CRUDEOIL":   "CRUDEOILM",
    "NATURALGAS": "NATGASMINI",
    "GOLD":       "GOLDM",
    "SILVER":     "SILVERM",
}

_MCX_BOD_CACHE = None


def round_to_tick(price: float, tick_size: float) -> float:
    ts = tick_size if tick_size and tick_size > 0 else 0.05
    decimals = max(0, len(f"{ts:.10f}".rstrip("0").split(".")[1])) if "." in f"{ts:.10f}".rstrip("0") else 0
    return round(round(price / ts) * ts, decimals)


def is_scheduled_handover_window(now_ist: datetime) -> bool:
    hhmm = now_ist.strftime("%H:%M")
    return ("14:35" <= hhmm <= "15:05") or ("16:35" <= hhmm <= "17:05")


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
    if "5M_CONTRARIAN" in s or "CONTRARIAN_REV" in s:
        return "5M_CONTRARIAN"
    return "5M_VWAP_MARU"


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
            ep_v = float(st.get("entry_price", 0.0) or 0.0)
            xp_v = float(st.get("exit_price", 0.0) or 0.0)
            clean_row = {
                "trade_no": t_no, "date": d_str, "entry_time": e_time, "exit_time": x_time,
                "symbol": sym, "contract": str(st.get("contract", "")).strip(),
                "side": str(st.get("side", "")).strip(), "qty": qty_v, "pnl_units": units_v,
                "entry_price": ep_v, "exit_price": xp_v,
                "peak_price": float(st.get("peak_price") or max(ep_v, xp_v)),
                "max_r": float(st.get("max_r", 0.0) or 0.0),
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
# 2. CLEAN 2-PINNED TELEGRAM DASHBOARDS
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
            requests.post(url, json=payload, timeout=10)
            return
        except Exception as e:
            if attempt == 0:
                time.sleep(0.8)
            else:
                log.warning(f"Telegram send error after retry: {e}")


def load_saved_dashboard_ids() -> dict:
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    default_ids = {"master": None, "positions": None}
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
            out.append("\n<i>... (see full list in CSV / Google Sheet) ...</i>")
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
            resp = requests.post(edit_url, json=payload, timeout=10).json()
            desc = str(resp.get("description", "")).lower()
            if resp.get("ok") or "message is not modified" in desc:
                return message_id
        except Exception:
            return message_id

    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": safe_text, "parse_mode": "HTML", "disable_notification": True}
    try:
        resp = requests.post(send_url, json=payload, timeout=10).json()
        if resp.get("ok"):
            new_id = resp["result"]["message_id"]
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/pinChatMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "message_id": new_id, "disable_notification": True},
                timeout=6,
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

    if is_mcx:
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
        log.info(
            f"[JOURNAL] Saved #{row['trade_no']} [{row['setup']}]: {row['contract']} "
            f"Peak ₹{row['peak_price']:.2f} (1:{row['max_r']:.1f}R) | Net PnL ₹{row['pnl']:+.2f}"
        )
    except Exception as e:
        log.error(f"Journal write error: {e}")

    push_trade_to_google_sheet(row)


# ==============================================================================
# 3. UPSTOX API HELPERS (MCX MASTER + FUTURES + OPTIONS)
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
        # If today is expiry day, inspect next expiry contract specs as well
        target_exp = expiries[1] if (expiries and expiries[0] == today_str and len(expiries) > 1) else (expiries[0] if expiries else contracts[0].get("expiry"))
        exp_contracts = [c for c in contracts if c.get("expiry") == target_exp] or contracts

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
# 4. TECHNICAL INDICATORS & 5M VWAP MARUBOZU + WHIPSAW / PREMIUM DECAY GUARDS
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
    x["upper_wick_ratio"] = (x["high"] - x[["open", "close"]].max(axis=1)) / c_range
    x["lower_wick_ratio"] = (x[["open", "close"]].min(axis=1) - x["low"]) / c_range
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


def is_vwap_whipsaw_chop(df_5m: pd.DataFrame) -> tuple:
    """
    Detects sideways VWAP whipsaw chop:
    Counts how many times 5m candle closes crossed above/below VWAP in the last 12 bars (1 hour).
    """
    if df_5m.empty or len(df_5m) < 8:
        return False, 0
    sub = df_5m.iloc[-13:-1]  # Last 12 completed 5m bars
    above = (sub["close"] > sub["vwap"]).astype(int)
    flips = int(above.diff().abs().fillna(0).sum())
    return (flips >= WHIPSAW_MAX_VWAP_FLIPS), flips


class FiveMinVwapMarubozuStrategy:
    """
    5-Minute VWAP Marubozu Arm + Dynamic Arm Flip/Cancel + Pullback Rejection Entry.
    """

    def __init__(self):
        self.armed_setup = {}       # idx -> dict with arm state
        self.last_eval_bar_ts = {}  # idx -> last completed 5m bar timestamp

    def _is_bearish_marubozu_vwap_cut(self, bar: pd.Series, is_mcx: bool = False) -> bool:
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        vwap = float(bar["vwap"])
        body_r = float(bar["body_ratio"])
        vol_r = float(bar["vol_ratio"])
        cut_vwap = (o >= vwap or h >= vwap) and (c < vwap)
        vol_ok = True if is_mcx else (vol_r >= MARU_VOL_MIN or vol_r == 1.0)
        return (c < o) and cut_vwap and (body_r >= MARU_BODY_MIN) and vol_ok

    def _is_bullish_marubozu_vwap_cut(self, bar: pd.Series, is_mcx: bool = False) -> bool:
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        vwap = float(bar["vwap"])
        body_r = float(bar["body_ratio"])
        vol_r = float(bar["vol_ratio"])
        cut_vwap = (o <= vwap or l <= vwap) and (c > vwap)
        vol_ok = True if is_mcx else (vol_r >= MARU_VOL_MIN or vol_r == 1.0)
        return (c > o) and cut_vwap and (body_r >= MARU_BODY_MIN) and vol_ok

    def _is_bearish_rejection_candle(self, curr: pd.Series, prev: pd.Series) -> tuple:
        o, h, l, c = float(curr["open"]), float(curr["high"]), float(curr["low"]), float(curr["close"])
        vwap, ema20, atr = float(curr["vwap"]), float(curr["ema20"]), max(float(curr["atr14"]), 1e-6)
        u_wick = float(curr["upper_wick_ratio"])
        body_r = float(curr["body_ratio"])

        if not (c < vwap and c < ema20):
            return False, ""

        near_vwap_ema = (h >= min(vwap, ema20) - 0.75 * atr) or (float(prev["high"]) >= min(vwap, ema20) - 0.75 * atr)
        if not near_vwap_ema:
            return False, ""

        if u_wick >= 0.35 and c <= o:
            return True, "WICK_REJECT"
        if (float(prev["close"]) > float(prev["open"])) and (c < o) and (c <= float(prev["open"])):
            return True, "BEAR_ENGULF"
        if (c < o) and (body_r >= 0.45) and (c < float(prev["low"])):
            return True, "EMA_VWAP_REJECT"
        if (c < o) and (u_wick >= 0.25):
            return True, "PULLBACK_REJECT"
        return False, ""

    def _is_bullish_rejection_candle(self, curr: pd.Series, prev: pd.Series) -> tuple:
        o, h, l, c = float(curr["open"]), float(curr["high"]), float(curr["low"]), float(curr["close"])
        vwap, ema20, atr = float(curr["vwap"]), float(curr["ema20"]), max(float(curr["atr14"]), 1e-6)
        l_wick = float(curr["lower_wick_ratio"])
        body_r = float(curr["body_ratio"])

        if not (c > vwap and c > ema20):
            return False, ""

        near_vwap_ema = (l <= max(vwap, ema20) + 0.75 * atr) or (float(prev["low"]) <= max(vwap, ema20) + 0.75 * atr)
        if not near_vwap_ema:
            return False, ""

        if l_wick >= 0.35 and c >= o:
            return True, "WICK_REJECT"
        if (float(prev["close"]) < float(prev["open"])) and (c > o) and (c >= float(prev["open"])):
            return True, "BULL_ENGULF"
        if (c > o) and (body_r >= 0.45) and (c > float(prev["high"])):
            return True, "EMA_VWAP_REJECT"
        if (c > o) and (l_wick >= 0.25):
            return True, "PULLBACK_REJECT"
        return False, ""

    def evaluate_5m_signal(
        self,
        idx: str,
        df_5m: pd.DataFrame,
        pcr: float,
        is_mcx: bool,
        has_open_position: bool,
        is_whipsaw: bool,
        tick_size: float = 0.05,
    ) -> dict:
        if df_5m.empty or len(df_5m) < 4:
            return {"action": "NONE", "status": "WARMUP_5M"}

        if is_whipsaw:
            self.armed_setup[idx] = None
            return {"action": "NONE", "status": "WHIPSAW_CHOP"}

        closed_bar = df_5m.iloc[-2]
        prev_bar = df_5m.iloc[-3]
        bar_ts = str(closed_bar["timestamp"])
        bar_hhmm = closed_bar["timestamp"].strftime("%H:%M")

        if self.last_eval_bar_ts.get(idx) == bar_ts:
            arm = self.armed_setup.get(idx)
            st = f"ARMED {arm['side']}@{arm['arm_time']}" if arm else "SCAN 5M VWAP"
            return {"action": "NONE", "status": st}

        self.last_eval_bar_ts[idx] = bar_ts
        close_px = float(closed_bar["close"])
        high_px = float(closed_bar["high"])
        low_px = float(closed_bar["low"])
        atr = max(float(closed_bar["atr14"]), tick_size * 4)
        buf = MARU_SL_ATR_BUF * atr

        arm = self.armed_setup.get(idx)

        # 1. Cancel existing ARM if its SL anchor is breached
        if arm is not None:
            arm["bars_elapsed"] += 1
            if arm["bars_elapsed"] > MARU_MAX_ARM_BARS:
                self.armed_setup[idx] = None
                arm = None
            elif arm["side"] == "PE" and high_px >= arm["spot_sl"]:
                self.armed_setup[idx] = None
                arm = None
            elif arm["side"] == "CE" and low_px <= arm["spot_sl"]:
                self.armed_setup[idx] = None
                arm = None

        # 2. Check if a strong Marubozu cuts VWAP -> ARM
        if self._is_bearish_marubozu_vwap_cut(closed_bar, is_mcx):
            spot_sl = round_to_tick(high_px + buf, tick_size)
            self.armed_setup[idx] = {
                "side": "PE",
                "arm_time": bar_hhmm,
                "arm_bar_ts": bar_ts,
                "spot_sl": spot_sl,
                "bars_elapsed": 0,
            }
            return {"action": "ARMED", "side": "PE", "status": f"ARMED PE@{bar_hhmm}"}

        if self._is_bullish_marubozu_vwap_cut(closed_bar, is_mcx):
            spot_sl = round_to_tick(low_px - buf, tick_size)
            self.armed_setup[idx] = {
                "side": "CE",
                "arm_time": bar_hhmm,
                "arm_bar_ts": bar_ts,
                "spot_sl": spot_sl,
                "bars_elapsed": 0,
            }
            return {"action": "ARMED", "side": "CE", "status": f"ARMED CE@{bar_hhmm}"}

        # 3. Trigger Rejection Entry while ARMED
        if arm is not None and not has_open_position and bar_ts != arm["arm_bar_ts"]:
            if arm["side"] == "PE":
                rej_ok, pat_name = self._is_bearish_rejection_candle(closed_bar, prev_bar)
                pcr_ok = True if is_mcx else (pcr <= 1.15)
                if rej_ok and pcr_ok:
                    spot_risk = arm["spot_sl"] - close_px
                    if spot_risk > 0:
                        self.armed_setup[idx] = None
                        return {
                            "action": "ENTER",
                            "side": "PE",
                            "setup": f"5M_VWAP_{pat_name}",
                            "arm_time": arm["arm_time"],
                            "entry_time": bar_hhmm,
                            "spot_entry": close_px,
                            "spot_sl": arm["spot_sl"],
                            "spot_risk": round(spot_risk, 2),
                            "status": f"ENTER PE@{bar_hhmm}",
                        }

            elif arm["side"] == "CE":
                rej_ok, pat_name = self._is_bullish_rejection_candle(closed_bar, prev_bar)
                pcr_ok = True if is_mcx else (pcr >= 0.85)
                if rej_ok and pcr_ok:
                    spot_risk = close_px - arm["spot_sl"]
                    if spot_risk > 0:
                        self.armed_setup[idx] = None
                        return {
                            "action": "ENTER",
                            "side": "CE",
                            "setup": f"5M_VWAP_{pat_name}",
                            "arm_time": arm["arm_time"],
                            "entry_time": bar_hhmm,
                            "spot_entry": close_px,
                            "spot_sl": arm["spot_sl"],
                            "spot_risk": round(spot_risk, 2),
                            "status": f"ENTER CE@{bar_hhmm}",
                        }

        st = f"ARMED {arm['side']}@{arm['arm_time']}" if arm else "SCAN 5M VWAP"
        return {"action": "NONE", "status": st}


# ==============================================================================
# 5. ORDER EXECUTION
# ==============================================================================
def place_marketable_limit_order(instrument_key: str, qty: int, side: str, ltp: float, tick_size: float = 0.05) -> dict:
    is_mcx = instrument_key.startswith("MCX_FO")
    buffer = (1.003 if side == "BUY" else 0.997) if is_mcx else (1.015 if side == "BUY" else 0.985)
    limit_price = round_to_tick(ltp * buffer, tick_size)

    if PAPER_MODE:
        log.info(f"[PAPER ORDER] {side} {qty} x {instrument_key} @ ₹{ltp:.2f} (Limit ₹{limit_price})")
        return {"status": "success", "fill_price": ltp, "order_id": f"PAPER_{int(time.time())}"}

    try:
        payload = {
            "quantity": qty, "product": "I", "validity": "DAY", "price": limit_price,
            "tag": "V3_INDEX_MCX", "instrument_token": instrument_key,
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
# 6. UNIFIED INDEX + MCX ENGINE (WHOLE EXPIRY DAY -> NEXT EXPIRY)
# ==============================================================================
class IntradayV3Engine:
    def __init__(self):
        self.expiries = {}
        self.fut_contracts = {}
        self.vwap_maru_5m = FiveMinVwapMarubozuStrategy()
        self.last_rev_5m_ts = {idx: "" for idx in INDICES}
        self.open_pcr = {idx: None for idx in INDICES}
        self.pcr_history = {idx: deque(maxlen=6) for idx in INDICES}

        self.last_exit_ts = {}
        self.last_exit_candle_ts = {}
        self.consec_losses = {idx: 0 for idx in INDICES}
        self.whipsaw_pause_until = {idx: 0.0 for idx in INDICES}
        self.active_positions = {}
        self.pos_lock = threading.Lock()

        self.closed_trades = []
        self.trade_counter = 0
        self.day_pnl = 0.0
        self.last_summaries = {}
        self.latest_dfs = {}
        self.latest_5m_dfs = {}

        # 2 Clean Pinned Dashboards
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

    def _update_peak_and_1r_cost_shift(self, pos: dict, ltp: float):
        """
        1. ONLY TRACKS Peak High (peak_price) & Max R-multiple (max_r) for analytics/logging.
        2. Shifts SL to Cost-to-Cost when 1R (+SL pts) is reached.
        3. Main Target remains fixed at 1:2 (TARGET_1_2_HIT).
        """
        is_short = pos["tx_side"] == "SELL"
        entry = float(pos["entry_price"])
        r_unit = max(float(pos.get("r_unit", 1.0)), 1e-6)
        tick_sz = float(pos.get("tick_size", 0.05))

        prev_peak = float(pos.get("peak_price", entry))
        if is_short:
            new_peak = min(prev_peak, ltp)
            max_gain = entry - new_peak
        else:
            new_peak = max(prev_peak, ltp)
            max_gain = new_peak - entry

        pos["peak_price"] = round(new_peak, 2)
        pos["max_r"] = round(max(0.0, max_gain / r_unit), 2)

        curr_gain = (entry - ltp) if is_short else (ltp - entry)
        if curr_gain >= r_unit and not pos.get("trail_stage"):
            be_sl = round_to_tick(entry, tick_sz)
            if (is_short and be_sl < pos["sl"]) or (not is_short and be_sl > pos["sl"]):
                pos["sl"] = be_sl
                pos["trail_stage"] = "1R COST-TO-COST"

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

            with self.pos_lock:
                for pos_key, pos in data.items():
                    if pos.get("type") == "EQUITY_CASH":
                        continue

                    inst_key = pos["instrument_key"]
                    is_short = pos["tx_side"] == "SELL"
                    entry = float(pos["entry_price"])

                    q_data = live_quotes.get(inst_key, {})
                    curr_ltp = float(q_data.get("last_price", pos.get("last_ltp", entry)))
                    pos["last_ltp"] = curr_ltp
                    self._update_peak_and_1r_cost_shift(pos, curr_ltp)

                    self.active_positions[pos_key] = pos
                    self.trade_counter = max(self.trade_counter, int(pos.get("trade_no", 0)), len(self.closed_trades))

                    tgt = float(pos.get("target", 0.0))
                    if tgt > 0 and ((not is_short and curr_ltp >= tgt) or (is_short and curr_ltp <= tgt)):
                        immediate_exits.append((pos_key, curr_ltp, "TARGET_1_2_HIT"))
                    elif (not is_short and curr_ltp <= pos["sl"]) or (is_short and curr_ltp >= pos["sl"]):
                        reason = "COST_TO_COST_SL_HIT" if pos.get("trail_stage") else "STOP_LOSS_HIT"
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
                        ep_v = float(r.get("entry_price", 0.0))
                        xp_v = float(r.get("exit_price", 0.0))
                        self.closed_trades.append({
                            "trade_no": t_no, "date": d_str, "entry_time": r.get("entry_time", ""),
                            "exit_time": r.get("exit_time", ""), "symbol": r.get("symbol", ""),
                            "contract": r.get("contract", ""), "side": r.get("side", ""),
                            "qty": qty_v, "pnl_units": units_v,
                            "entry_price": ep_v, "exit_price": xp_v,
                            "peak_price": float(r.get("peak_price") or max(ep_v, xp_v)),
                            "max_r": float(r.get("max_r", 0.0) or 0.0),
                            "points": float(r.get("points", 0.0)),
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
                    tgt = float(pos.get("target", 0.0))
                    self._update_peak_and_1r_cost_shift(pos, ltp)

                    # Main Target is fixed 1:2
                    if tgt > 0 and ((not is_short and ltp >= tgt) or (is_short and ltp <= tgt)):
                        exits_to_run.append((pos_key, "TARGET_1_2_HIT"))
                    elif (not is_short and ltp <= pos["sl"]) or (is_short and ltp >= pos["sl"]):
                        reason = "COST_TO_COST_SL_HIT" if pos.get("trail_stage") else "STOP_LOSS_HIT"
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

    def get_smart_expiry(self, idx: str, now_ist: datetime) -> tuple:
        valid_exps = self.expiries.get(idx, [])
        if not valid_exps:
            return "", False
        nearest = valid_exps[0]
        is_today_exp = nearest == now_ist.strftime("%Y-%m-%d")
        # On the WHOLE expiry day (09:15 to 15:15), always select NEXT expiry
        if is_today_exp and len(valid_exps) > 1:
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

    def analyze_pcr(self, idx: str, chain: list) -> float:
        if not chain:
            return 1.0
        total_pe_oi = sum(float(r.get("put_options", {}).get("market_data", {}).get("oi", 0)) for r in chain)
        total_ce_oi = sum(float(r.get("call_options", {}).get("market_data", {}).get("oi", 0)) for r in chain)
        pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 1.0
        if self.open_pcr[idx] is None:
            self.open_pcr[idx] = pcr
        hist = self.pcr_history[idx]
        if not hist or hist[-1] != pcr:
            hist.append(pcr)
        return pcr

    def _calc_segment_stats(self, seg_type: str = None) -> dict:
        if seg_type:
            c_list = [
                t for t in self.closed_trades
                if (seg_type == "MCX_FUT" and t.get("symbol") in ("CRUDEOIL", "NATURALGAS", "GOLD", "SILVER"))
                or (seg_type == "INDEX_OPT" and t.get("symbol") in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"))
            ]
        else:
            c_list = [t for t in self.closed_trades if t.get("symbol") in INDICES]

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
            est_c = float(p.get("est_charges", 180.0 if p["type"] == "MCX_FUT" else 65.0))
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
            c_list = [t for t in self.closed_trades if t.get("symbol") in INDICES and map_setup_to_strategy(t.get("setup", "")) == sk]
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
                est_c = float(p.get("est_charges", 180.0 if p["type"] == "MCX_FUT" else 65.0))
                gross = (p["entry_price"] - ltp) * eff_u if is_short else (ltp - p["entry_price"]) * eff_u
                live_mtm += (gross - est_c)

            tot_net = round(realized_pnl + live_mtm, 2)
            icon = "🟢" if tot_net > 0 else ("🔴" if tot_net < 0 else "⚪")
            open_tag = f" | Open: <b>{len(a_list)}</b>" if a_list else ""
            lines.append(
                f"• <b>{sk}</b>: {icon} <b>₹{tot_net:+,.0f}</b>  ({tot}T: {len(wins)}W/{len(losses)}L, WR {wr}%, R:R 1:2.0){open_tag}"
            )
        return lines

    def build_hourly_analysis_lines(self) -> list:
        hourly = {}
        for t in self.closed_trades:
            if t.get("symbol") not in INDICES:
                continue
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
                    est_c = float(p.get("est_charges", 180.0 if p["type"] == "MCX_FUT" else 65.0))
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
            tag = "PROFIT" if d["pnl"] >= 200 else ("LOSS" if d["pnl"] <= -200 else "CONSOL")
            badge = " ★BEST" if (hr == best_hr[0] and best_hr[1]["pnl"] > 0) else (" ▼WORST" if (hr == worst_hr[0] and worst_hr[1]["pnl"] < 0) else "")
            lines.append(f"• <code>{slot_lbl}</code>: <b>₹{d['pnl']:+,.0f}</b>  ({d['trades']}T, {d['wins']}W)  <code>[{tag}{badge}]</code>")
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

        today_fo = [t for t in self.closed_trades if t.get("symbol") in INDICES]
        d_tot = len(today_fo)
        d_wins = sum(1 for x in today_fo if x["pnl"] > 0)
        d_wr = int(round((d_wins / d_tot) * 100.0)) if d_tot > 0 else 0
        d_net = round(sum(x["pnl"] for x in today_fo), 2)

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

    # ==========================================================================
    # RENDER 2 CLEAN PINNED TELEGRAM BOARDS
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

            tot_icon = "🟢" if tot_st["net_pnl"] >= 0 else "🔴"
            idx_icon = "🟢" if idx_st["net_pnl"] >= 0 else "🔴"
            mcx_icon = "🟢" if mcx_st["net_pnl"] >= 0 else "🔴"

            master_lines = [
                f"📌 <b>[1/2] MASTER P&amp;L SUMMARY</b>  |  {status_header}  |  <code>{hhmm} IST</code>  |  {mode_badge}",
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                f"💼 <b>CONSOLIDATED NET P&amp;L:</b>  {tot_icon} <b>₹{tot_st['net_pnl']:+,.2f}</b>",
                f"• Realized: <b>₹{tot_st['realized']:+,.2f}</b>   |   Live MTM: <b>₹{tot_st['live_mtm']:+,.2f}</b>",
                f"• Closed: <b>{tot_st['closed']}T</b> ({tot_st['wins']}W / {tot_st['losses']}L, <b>{tot_st['wr']}% WR</b>)   |   Open: <b>{tot_st['open_count']}</b>",
                "",
                f"📦 <b>SEGMENT-WISE P&amp;L (INDEX + MCX MINI)</b>",
                f"• 📡 <b>Index Options:</b>  {idx_icon} <b>₹{idx_st['net_pnl']:+,.2f}</b>  ({idx_st['closed']}T, {idx_st['wr']}% WR | Open: {idx_st['open_count']})",
                f"• 🛢️ <b>MCX Mini Fut:</b>   {mcx_icon} <b>₹{mcx_st['net_pnl']:+,.2f}</b>  ({mcx_st['closed']}T, {mcx_st['wr']}% WR | Open: {mcx_st['open_count']})",
                "",
                f"🎯 <b>STRATEGY-WISE PERFORMANCE (1:2 TARGET | 1R → COST)</b>",
            ]
            master_lines.extend(self.build_strategy_performance_lines())
            master_lines.extend(["", "⏰ <b>HOURLY SESSION TRACKER (IST)</b>"])
            master_lines.extend(self.build_hourly_analysis_lines())
            master_lines.extend(["", "📊 <b>OVERALL ACCOUNT SUMMARY (NET ₹)</b>"])
            master_lines.extend(self.get_performance_summary_lines(now_ist, tot_st["live_mtm"]))

            with self.pos_lock:
                active_snap = sorted(self.active_positions.values(), key=lambda x: x["trade_no"])

            pos_lines = [
                f"📌 <b>[2/2] ACTIVE TRADES &amp; LIVE FEED</b>  |  <code>{hhmm} IST</code>  |  {mode_badge}",
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                f"🔥 <b>OPEN POSITIONS ({len(active_snap)})</b>",
            ]

            if active_snap:
                for pos in active_snap:
                    ltp = pos.get("last_ltp", pos["entry_price"])
                    peak_px = float(pos.get("peak_price", ltp))
                    max_r = float(pos.get("max_r", 0.0))
                    is_short = pos["tx_side"] == "SELL"
                    seg = pos.get("type", "INDEX_OPT")
                    est_c = float(pos.get("est_charges", 180.0 if seg == "MCX_FUT" else 65.0))
                    eff_u = int(pos.get("pnl_units", pos["qty"]))
                    pts_diff = round((pos["entry_price"] - ltp) if is_short else (ltp - pos["entry_price"]), 2)
                    pnl = round((pts_diff * eff_u) - est_c, 2)
                    sl_gross = (pos["entry_price"] - pos["sl"]) * eff_u if is_short else (pos["sl"] - pos["entry_price"]) * eff_u
                    sl_net = round(sl_gross - est_c, 0)

                    strat_lbl = pos.get("setup_name", pos.get("strategy_key", ""))
                    tgt_val = float(pos.get("target", 0.0))
                    trail_tag = f"<code>{strat_lbl}</code> | 🛡️<code>{pos['trail_stage']}</code>" if pos.get("trail_stage") else f"<code>{strat_lbl}</code>"
                    p_icon = "🟢" if pnl >= 0 else "🔴"
                    sl_lbl = f"Cost SL <b>₹{sl_net:+,.0f}</b>" if pos.get("trail_stage") else f"Risk <b>₹{abs(sl_net):,.0f}</b>"

                    pos_lines.extend([
                        "",
                        f"• <b>#{pos['trade_no']} {pos['symbol']}</b> <code>{pos['strike']} {pos['display_side']}</code> (<code>{pos['qty']} Qty</code>)  |  {trail_tag}",
                        f"  In: <b>₹{pos['entry_price']:.2f}</b> (<code>{pos['entry_time'][:5]}</code>) → LTP: <b>₹{ltp:.2f}</b> | Peak: <b>₹{peak_px:.2f}</b> (<code>1:{max_r:.1f}R</code>) | Net: {p_icon} <b>₹{pnl:+,.2f}</b>",
                        f"  SL: <code>₹{pos['sl']:.2f}</code> ({sl_lbl})  |  Target (1:2): <code>₹{tgt_val:.2f}</code>",
                    ])
            else:
                pos_lines.append("<i>No open positions right now.</i>")

            pos_lines.extend(["", "📒 <b>RECENT CLOSED TRADES (LATEST 5)</b>"])
            fo_closed = [t for t in self.closed_trades if t.get("symbol") in INDICES]
            if fo_closed:
                for t in sorted(fo_closed, key=lambda x: x["trade_no"])[-5:]:
                    icon = "✅" if t["pnl"] >= 0 else "❌"
                    pk_p = float(t.get("peak_price", max(t["entry_price"], t["exit_price"])))
                    mx_r = float(t.get("max_r", 0.0))
                    pos_lines.extend([
                        "",
                        f"• <b>#{t['trade_no']} {t['symbol']}</b> <code>{t['contract']} {t['side']}</code>  {icon} <b>₹{t['pnl']:+,.2f}</b> (Peak <code>₹{pk_p:.2f}</code> | <code>1:{mx_r:.1f}R</code>)",
                        f"  <code>{t.get('setup','')}</code>  |  <code>{t['entry_time'][:5]} → {t['exit_time'][:5]}</code>  |  <i>{t['exit_reason']}</i>",
                    ])
            else:
                pos_lines.append("<i>No closed trades yet today.</i>")

            pos_lines.extend(["", "📡 <b>LIVE MARKET SCANNER (INDEX + MCX MINI)</b>"])
            for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"):
                s = self.last_summaries.get(idx)
                if s:
                    pos_lines.append(f"• <b>{s['idx']}:</b> <code>{s['spot']}</code> | PCR <b>{s['pcr']:.2f}</b> | <code>{s['short_status']}</code>")

            mcx_parts = []
            for idx in ("CRUDEOIL", "NATURALGAS", "GOLD", "SILVER"):
                s = self.last_summaries.get(idx)
                if s:
                    mcx_parts.append(f"{idx[:4]} <code>{s['spot']}</code> (<code>{s['short_status']}</code>)")
            if mcx_parts:
                pos_lines.append(f"• <b>MCX Mini:</b> {' | '.join(mcx_parts)}")

            prev_ids = dict(self.dash_ids)
            self.dash_ids["master"] = upsert_telegram_dashboard("\n".join(master_lines), self.dash_ids.get("master"))
            time.sleep(0.4)
            self.dash_ids["positions"] = upsert_telegram_dashboard("\n".join(pos_lines), self.dash_ids.get("positions"))

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
                                    ltp_q = float(q["last_price"])
                                    pos["last_ltp"] = ltp_q
                                    self._update_peak_and_1r_cost_shift(pos, ltp_q)
                        for idx, cfg in INDICES.items():
                            sk = cfg.get("spot_key")
                            if sk and quotes.get(sk, {}).get("last_price") and idx in self.last_summaries:
                                ltp_val = float(quotes[sk]["last_price"])
                                self.last_summaries[idx]["spot"] = round(ltp_val, 2) if ltp_val < 1000 else int(round(ltp_val))

                    self.render_live_dashboard(datetime.now(IST).strftime("%H:%M"))
                except Exception:
                    pass

        threading.Thread(target=_pulse_loop, daemon=True).start()

    def close_position_now(self, pos_key: str, ltp: float, exit_reason: str, now_ist: datetime):
        with self.pos_lock:
            pos = self.active_positions.pop(pos_key, None)
        if not pos:
            return

        self._update_peak_and_1r_cost_shift(pos, ltp)
        idx = pos["symbol"]
        self.last_exit_ts[pos_key] = time.time()

        df_curr = self.latest_dfs.get(idx)
        if df_curr is not None and not df_curr.empty:
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

        # Whipsaw guard: 2 consecutive stop-losses on same symbol -> pause symbol for 30 mins
        if net_pnl < 0 and "STOP_LOSS" in exit_reason:
            self.consec_losses[idx] = self.consec_losses.get(idx, 0) + 1
            if self.consec_losses[idx] >= 2:
                self.whipsaw_pause_until[idx] = time.time() + WHIPSAW_COOLDOWN_SEC
                self.consec_losses[idx] = 0
                log.warning(f"[WHIPSAW GUARD] {idx} hit 2 consecutive SLs. Pausing entries for 30 mins.")
        elif net_pnl > 0:
            self.consec_losses[idx] = 0

        self.day_pnl = round(self.day_pnl + net_pnl, 2)
        peak_px = float(pos.get("peak_price", max(pos["entry_price"], ltp)))
        max_r = float(pos.get("max_r", 0.0))

        exit_time_str = now_ist.strftime("%H:%M:%S")
        journal_entry = {
            "trade_no": pos["trade_no"], "date": now_ist.strftime("%Y-%m-%d"),
            "entry_time": pos.get("entry_time", ""), "exit_time": exit_time_str,
            "symbol": idx, "contract": str(pos["strike"]), "side": pos["display_side"],
            "qty": pos["qty"], "pnl_units": int(eff_units),
            "entry_price": pos["entry_price"], "exit_price": ltp,
            "peak_price": peak_px, "max_r": max_r,
            "points": pts, "sl": pos["sl"], "target": pos.get("target", 0.0), "exit_reason": exit_reason,
            "gross_pnl": gross_pnl, "brokerage": ch_info["brokerage"],
            "taxes_other": ch_info["taxes_other"], "charges": ch_info["total"],
            "pnl": net_pnl, "day_pnl": self.day_pnl, "setup": pos["setup_name"],
        }
        self.closed_trades.append(journal_entry)
        record_trade_journal(journal_entry)
        icon = "✅" if net_pnl >= 0 else "❌"
        notify(
            f"\n{icon} <b>EXIT #{pos['trade_no']} • {idx}</b> ({pos['setup_name']})\n"
            f"📊 <b>Exit:</b> ₹{ltp:.2f} | <b>Peak High:</b> ₹{peak_px:.2f} (<code>1:{max_r:.1f}R</code>) | <b>Net P&amp;L:</b> <b>₹{net_pnl:+,.2f}</b> | <i>{exit_reason}</i>\n"
        )
        self.render_live_dashboard(now_ist.strftime("%H:%M"))

    def can_trigger_strategy(self, idx: str, strat_key: str, curr_candle_ts: str) -> bool:
        if time.time() < self.whipsaw_pause_until.get(idx, 0.0):
            return False
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
    # INDEX + MCX 2-STRATEGY EVALUATOR (WITH WHIPSAW CHOP GUARD)
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
            pcr = self.analyze_pcr(idx, chain)

        df_5m = build_resampled_5m_df(raw_df)
        self.latest_5m_dfs[idx] = df_5m
        is_whipsaw, flip_cnt = is_vwap_whipsaw_chop(df_5m)

        in_window = self.is_within_trade_window(idx, now_ist)
        if not in_window:
            self.vwap_maru_5m.armed_setup[idx] = None
            with self.pos_lock:
                open_cnt = sum(1 for p in self.active_positions.values() if p["symbol"] == idx)
            slot_txt = "09:15-21:00" if is_mcx else "09:15-15:00"
            st_lbl = f"ACTIVE({open_cnt}) | SLOT {slot_txt}" if open_cnt > 0 else f"SLOT {slot_txt}"
            return {
                "idx": idx, "is_mcx": is_mcx,
                "spot": round(price_now, 2) if price_now < 1000 else int(round(price_now)),
                "vwap": round(vwap_1m, 2) if vwap_1m < 1000 else int(round(vwap_1m)),
                "pcr": pcr, "vol_ratio": vol_ratio_1m, "atr": round(atr14_1m, 1),
                "short_status": st_lbl,
            }

        # STRATEGY 1: 5M_VWAP_MARU
        maru_pos_key = f"{idx}__5M_VWAP_MARU"
        with self.pos_lock:
            has_maru_pos = maru_pos_key in self.active_positions

        maru_sig = self.vwap_maru_5m.evaluate_5m_signal(
            idx=idx,
            df_5m=df_5m,
            pcr=pcr,
            is_mcx=is_mcx,
            has_open_position=has_maru_pos,
            is_whipsaw=is_whipsaw,
            tick_size=cfg.get("tick_size", 0.05),
        )
        if maru_sig["action"] == "ENTER":
            bar_5m_ts = str(df_5m.iloc[-2]["timestamp"]) if len(df_5m) >= 2 else ""
            if self.can_trigger_strategy(idx, "5M_VWAP_MARU", bar_5m_ts):
                self.dispatch_marubozu_entry(
                    idx=idx, cfg=cfg, sig=maru_sig, fut_price=price_now,
                    chain=chain, is_today_exp=is_today_exp, now_ist=now_ist,
                )

        # STRATEGY 2: 5M_CONTRARIAN (Requires 1 Opposite Confirmation Candle)
        if not df_5m.empty and len(df_5m) >= 12 and not is_whipsaw:
            bar_5m = df_5m.iloc[-2]
            prev_5m = df_5m.iloc[-3]
            bar_5m_ts = str(bar_5m["timestamp"])
            if bar_5m_ts != self.last_rev_5m_ts.get(idx, "") and self.can_trigger_strategy(idx, "5M_CONTRARIAN", bar_5m_ts):
                self.last_rev_5m_ts[idx] = bar_5m_ts
                atr14_5m = max(float(bar_5m["atr14"]), cfg.get("tick_size", 0.05) * 2)
                vol_5m = float(bar_5m["vol_ratio"])
                rng_5m = float(bar_5m["high"]) - float(bar_5m["low"])
                body_5m = abs(float(bar_5m["close"]) - float(bar_5m["open"]))
                br_5m = body_5m / (rng_5m if rng_5m > 0 else 1e-9)

                was_bull_trend = (
                    float(prev_5m["close"]) > float(prev_5m["ema20"]) > float(prev_5m["ema50"])
                    and float(prev_5m["close"]) > float(prev_5m["open"])
                )
                was_bear_trend = (
                    float(prev_5m["close"]) < float(prev_5m["ema20"]) < float(prev_5m["ema50"])
                    and float(prev_5m["close"]) < float(prev_5m["open"])
                )

                confirm_red_candle = float(bar_5m["close"]) < float(bar_5m["open"])
                confirm_green_candle = float(bar_5m["close"]) > float(bar_5m["open"])

                if vol_5m >= REV_VOL_RATIO_MIN and br_5m >= REV_BODY_RATIO_MIN:
                    if was_bull_trend and confirm_red_candle:
                        self.dispatch_standard_entry(
                            idx=idx, cfg=cfg, side="PE", fut_price=price_now,
                            atr14=atr14_5m, chain=chain, is_today_exp=is_today_exp,
                            now_ist=now_ist, strat_key="5M_CONTRARIAN",
                            setup_detail="5M_CONTRARIAN_REV", rrr_mult=REV_RRR_TARGET,
                        )
                    elif was_bear_trend and confirm_green_candle:
                        self.dispatch_standard_entry(
                            idx=idx, cfg=cfg, side="CE", fut_price=price_now,
                            atr14=atr14_5m, chain=chain, is_today_exp=is_today_exp,
                            now_ist=now_ist, strat_key="5M_CONTRARIAN",
                            setup_detail="5M_CONTRARIAN_REV", rrr_mult=REV_RRR_TARGET,
                        )

        with self.pos_lock:
            sym_active = [p["strategy_key"] for p in self.active_positions.values() if p["symbol"] == idx]

        if sym_active:
            short_status = f"ACTIVE [{','.join(s.split('_')[0] for s in sym_active)}]"
        elif time.time() < self.whipsaw_pause_until.get(idx, 0.0):
            short_status = "SL PAUSE 30M"
        elif is_whipsaw:
            short_status = f"CHOP({flip_cnt}x VWAP)"
        elif self.vwap_maru_5m.armed_setup.get(idx):
            arm_obj = self.vwap_maru_5m.armed_setup[idx]
            short_status = f"ARMED {arm_obj['side']}@{arm_obj['arm_time']}"
        else:
            short_status = "SCAN 5M"

        return {
            "idx": idx, "is_mcx": is_mcx,
            "spot": round(price_now, 2) if price_now < 1000 else int(round(price_now)),
            "vwap": round(vwap_1m, 2) if vwap_1m < 1000 else int(round(vwap_1m)),
            "pcr": pcr, "vol_ratio": vol_ratio_1m, "atr": round(atr14_1m, 1),
            "short_status": short_status,
        }

    def dispatch_marubozu_entry(self, idx, cfg, sig, fut_price, chain, is_today_exp, now_ist):
        pos_key = f"{idx}__5M_VWAP_MARU"
        with self.pos_lock:
            if pos_key in self.active_positions:
                return

        side = sig["side"]
        spot_sl = float(sig["spot_sl"])
        spot_risk = max(float(sig["spot_risk"]), 1.0)
        setup_lbl = f"{sig['setup']}_{side}"

        if cfg["type"] == "MCX_FUT":
            tx_side = "BUY" if side == "CE" else "SELL"
            inst_key = cfg["spot_key"]
            lot_size = int(cfg["lot_size"])
            pnl_mult = int(cfg.get("pnl_multiplier", lot_size))
            tick_sz = cfg.get("tick_size", 0.05)

            ltp = float(fetch_ltp_quote([inst_key]).get(inst_key, {}).get("last_price", fut_price))
            sl_points = max(round_to_tick(abs(ltp - spot_sl), tick_sz), tick_sz * 4)
            target_points = round_to_tick(sl_points * MARU_RRR_TARGET, tick_sz)

            if tx_side == "BUY":
                sl_price = round_to_tick(ltp - sl_points, tick_sz)
                target_price = round_to_tick(ltp + target_points, tick_sz)
            else:
                sl_price = round_to_tick(ltp + sl_points, tick_sz)
                target_price = round_to_tick(ltp - target_points, tick_sz)

            qty = min(lot_size, cfg["freeze_qty"])
            total_est_ch = fetch_exact_trade_charges(inst_key, ltp, ltp, pnl_mult, "MCX_FUT").get("total", 180.0)

            order_res = place_marketable_limit_order(inst_key, qty, tx_side, ltp, tick_sz)
            if order_res.get("status") == "success":
                fill = order_res["fill_price"]
                sym = self.fut_contracts.get(idx, {}).get("trading_symbol", f"{idx} FUT")
                self.trade_counter = max(self.trade_counter, len(self.closed_trades)) + 1
                t_no = self.trade_counter
                with self.pos_lock:
                    self.active_positions[pos_key] = {
                        "trade_no": t_no, "symbol": idx, "strategy_key": "5M_VWAP_MARU",
                        "type": "MCX_FUT", "instrument_key": inst_key, "strike": sym,
                        "side": side, "tx_side": tx_side, "display_side": tx_side,
                        "lots": 1, "lot_size": lot_size, "qty": qty, "pnl_units": pnl_mult,
                        "tick_size": tick_sz, "entry_price": fill, "r_unit": sl_points,
                        "peak_price": fill, "max_r": 0.0,
                        "est_charges": total_est_ch, "last_ltp": fill,
                        "sl": sl_price, "target": target_price,
                        "trail_stage": None, "setup_name": setup_lbl,
                        "entry_time": now_ist.strftime("%H:%M:%S"),
                    }
                self.subscribe_tick_instrument(inst_key)
                self.save_active_positions_state()
                notify(
                    f"\n🎯 <b>NEW 5M VWAP MARU ENTRY #{t_no} • {idx}</b>\n"
                    f"📌 <b>Setup:</b> <code>{setup_lbl}</code> (Arm <code>{sig['arm_time']}</code> → Entry <code>{sig['entry_time']}</code>)\n"
                    f"📊 <b>Contract:</b> <code>{sym} {tx_side}</code> @ <b>₹{fill:.2f}</b> | SL: <code>₹{sl_price:.2f}</code> | Tgt (1:2): <code>₹{target_price:.2f}</code>\n"
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
            delta = min(0.75, max(0.35, raw_delta)) if raw_delta > 0 else DEFAULT_ATM_DELTA
            sl_points = max(round_to_tick(spot_risk * delta, tick_sz), tick_sz * 4)

            if ltp < MIN_OPTION_PREMIUM or sl_points >= (ltp * 0.65):
                log.info(f"[{idx}] Skipped low/risky option ({target_strike} {side} @ ₹{ltp:.2f}, Calc SL {sl_points:.1f}p)")
                return

            target_points = round_to_tick(sl_points * MARU_RRR_TARGET, tick_sz)
            sl_price = round_to_tick(max(tick_sz, ltp - sl_points), tick_sz)
            target_price = round_to_tick(ltp + target_points, tick_sz)

            qty = min(int(cfg["lot_size"]), cfg["freeze_qty"])
            total_est_ch = fetch_exact_trade_charges(inst_key, ltp, ltp, qty, "INDEX_OPT").get("total", 65.0)

            order_res = place_marketable_limit_order(inst_key, qty, "BUY", ltp, tick_sz)
            if order_res.get("status") == "success":
                fill = order_res["fill_price"]
                self.trade_counter = max(self.trade_counter, len(self.closed_trades)) + 1
                t_no = self.trade_counter
                with self.pos_lock:
                    self.active_positions[pos_key] = {
                        "trade_no": t_no, "symbol": idx, "strategy_key": "5M_VWAP_MARU",
                        "type": "INDEX_OPT", "instrument_key": inst_key,
                        "strike": target_strike, "side": side, "tx_side": "BUY",
                        "display_side": side, "lots": 1, "lot_size": int(cfg["lot_size"]),
                        "qty": qty, "pnl_units": qty, "tick_size": tick_sz,
                        "entry_price": fill, "r_unit": sl_points,
                        "peak_price": fill, "max_r": 0.0,
                        "est_charges": total_est_ch, "last_ltp": fill,
                        "sl": sl_price, "target": target_price,
                        "trail_stage": None, "setup_name": setup_lbl,
                        "entry_time": now_ist.strftime("%H:%M:%S"),
                    }
                self.subscribe_tick_instrument(inst_key)
                self.save_active_positions_state()
                notify(
                    f"\n🎯 <b>NEW 5M VWAP MARU ENTRY #{t_no} • {idx}</b>\n"
                    f"📌 <b>Setup:</b> <code>{setup_lbl}</code> (Arm <code>{sig['arm_time']}</code> → Entry <code>{sig['entry_time']}</code>)\n"
                    f"📊 <b>Option:</b> <code>{target_strike} {side}</code> @ <b>₹{fill:.2f}</b> | SL ({sl_points:.1f}p = {spot_risk:.1f}×{delta:.2f}Δ): <code>₹{sl_price:.2f}</code> | Tgt (1:2): <code>₹{target_price:.2f}</code>\n"
                )
                self.render_live_dashboard(now_ist.strftime("%H:%M"))

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
            sl_points = max(round_to_tick(atr14 * ATR_SL_MULT, tick_sz), tick_sz * 4)
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
                        "peak_price": fill, "max_r": 0.0,
                        "est_charges": total_est_ch, "last_ltp": fill,
                        "sl": sl_price, "target": target_price,
                        "trail_stage": None, "setup_name": full_setup,
                        "entry_time": now_ist.strftime("%H:%M:%S"),
                    }
                self.subscribe_tick_instrument(inst_key)
                self.save_active_positions_state()
                notify(
                    f"\n🚀 <b>NEW MCX ENTRY #{t_no} • {idx}</b>\n"
                    f"📌 <b>Strategy:</b> <code>{full_setup}</code>\n"
                    f"📊 <b>Contract:</b> <code>{sym} {tx_side}</code> @ <b>₹{fill:.2f}</b> | SL: <code>₹{sl_price:.2f}</code> | Tgt (1:2): <code>₹{target_price:.2f}</code>\n"
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
            delta = min(0.75, max(0.35, raw_delta)) if raw_delta > 0 else DEFAULT_ATM_DELTA
            fut_sl_pts = atr14 * ATR_SL_MULT
            sl_points = max(round_to_tick(fut_sl_pts * delta, tick_sz), tick_sz * 4)

            if ltp < MIN_OPTION_PREMIUM or sl_points >= (ltp * 0.65):
                log.info(f"[{idx}] Skipped low/risky option ({target_strike} {side} @ ₹{ltp:.2f}, Calc SL {sl_points:.1f}p)")
                return

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
                        "entry_price": fill, "r_unit": sl_points,
                        "peak_price": fill, "max_r": 0.0,
                        "est_charges": total_est_ch, "last_ltp": fill,
                        "sl": sl_price, "target": target_price,
                        "trail_stage": None, "setup_name": full_setup,
                        "entry_time": now_ist.strftime("%H:%M:%S"),
                    }
                self.subscribe_tick_instrument(inst_key)
                self.save_active_positions_state()
                notify(
                    f"\n🚀 <b>NEW INDEX ENTRY #{t_no} • {idx}</b>\n"
                    f"📌 <b>Strategy:</b> <code>{full_setup}</code>\n"
                    f"📊 <b>Option:</b> <code>{target_strike} {side}</code> @ <b>₹{fill:.2f}</b> | SL: <code>₹{sl_price:.2f}</code> | Tgt (1:2): <code>₹{target_price:.2f}</code>\n"
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
            entry = float(pos["entry_price"])
            r_unit = max(float(pos.get("r_unit", 1.0)), 1e-6)
            tgt = float(pos.get("target", 0.0))
            self._update_peak_and_1r_cost_shift(pos, ltp)

            exit_reason = None
            if pos["type"] == "INDEX_OPT" and hhmm >= nse_exit_t:
                exit_reason = "MUHURAT_EOD_EXIT" if self.session_sched.get("is_muhurat") else "EOD_EXIT_1515"
            elif pos["type"] == "MCX_FUT" and hhmm >= mcx_exit_t:
                exit_reason = "MCX_EOD_EXIT_2315"
            elif tgt > 0 and ((not is_short and ltp >= tgt) or (is_short and ltp <= tgt)):
                exit_reason = "TARGET_1_2_HIT"
            elif not is_short and ltp <= pos["sl"]:
                exit_reason = "COST_TO_COST_SL_HIT" if pos.get("trail_stage") else "STOP_LOSS_HIT"
            elif is_short and ltp >= pos["sl"]:
                exit_reason = "COST_TO_COST_SL_HIT" if pos.get("trail_stage") else "STOP_LOSS_HIT"

            # No-Momentum / Theta Premium Eating Exit
            if not exit_reason and float(pos.get("max_r", 0.0)) < 0.5:
                try:
                    today_s = now_ist.strftime("%Y-%m-%d")
                    ent_dt = datetime.strptime(f"{today_s} {pos['entry_time']}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
                    mins_open = (now_ist - ent_dt).total_seconds() / 60.0
                    df_5m_sym = self.latest_5m_dfs.get(pos["symbol"], pd.DataFrame())
                    if mins_open >= (NO_MOMENTUM_STALL_BARS * 5) and not df_5m_sym.empty and len(df_5m_sym) >= 4:
                        avg_vol_r = float(df_5m_sym.iloc[-4:-1]["vol_ratio"].mean())
                        curr_gain = (entry - ltp) if is_short else (ltp - entry)
                        if avg_vol_r < 0.80 and curr_gain <= (-0.35 * r_unit):
                            exit_reason = "NO_MOMENTUM_THETA_EXIT"
                except Exception:
                    pass

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
                f"\n🔴 <b>SESSION CLOSED ({reason_label})</b>  |  <code>{now_ist.strftime('%H:%M')} IST</code>\n"
                f"────────────────────────────\n"
                f"💼 <b>Day Net P&amp;L:</b> <b>₹{self.day_pnl:+,.2f}</b>  |  <b>Trades:</b> {len(self.closed_trades)}\n"
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
            f"🌅 <b>V3 INDEX + MCX ENGINE • ONLINE (NEXT EXPIRY ON EXPIRY DAY)</b>\n"
            f"────────────────────────────\n"
            f"🔑 <b>Token:</b> Active  |  ⚡ <b>Mode:</b> <code>{'PAPER' if PAPER_MODE else 'LIVE'}</code>\n"
            f"🎯 <b>Active Setups:</b> <code>5M_VWAP_MARU</code> + <code>5M_CONTRARIAN</code>"
        )
        self.dash_ids = {"master": None, "positions": None}
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
                        f"🟡 <b>NSE/BSE HOLIDAY ({today_s}) • MCX ARMED</b>\n"
                        f"────────────────────────────\n"
                        f"🛢️ <i>Index disabled; MCX Mini commodity trading active.</i>"
                    )

            self.manage_open_positions(now_ist)

            if hhmm >= "23:35":
                self.shutdown_cleanly(now_ist, "EOD CLOSED")
                break

            if not self.session_sched["nse_open"] and hhmm < self.session_sched.get("mcx_start", "09:15"):
                self.bot_status_text = f"STANDBY (MCX OPENS {self.session_sched.get('mcx_start', '09:15')})"
            elif self.session_sched["is_muhurat"] and hhmm < self.session_sched.get("nse_start", "18:00") and hhmm < self.session_sched.get("mcx_start", "09:15"):
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

            if not self.dash_ids.get("master"):
                self.render_live_dashboard(hhmm)

            time.sleep(8)


if __name__ == "__main__":
    engine = IntradayV3Engine()
    engine.run()
