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

try:
    from macro_news_radar import evaluate_macro_context
    HAS_MACRO_RADAR = True
except ImportError:
    HAS_MACRO_RADAR = False

try:
    from auto_tuner import get_market_closed_reason, detect_expiry_for_date, record_learned_holiday
    HAS_AUTO_TUNER = True
except ImportError:
    HAS_AUTO_TUNER = False

try:
    from ai_trading_agent import TickByTickAITradingAgent
    HAS_AI_AGENT = True
except ImportError:
    HAS_AI_AGENT = False

# ==============================================================================
# 1. CONFIGURATION, STORAGE & GOOGLE SHEETS SYNC
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

# Core Signal & Entry/Exit Parameters (1m 20-Candle Breakout + Retest + 5m Trend/Fuel)
MIN_FUEL_SCORE = 45.0
MCX_MIN_FUEL_SCORE = 44.0
MIN_ADX_TREND = 18.0
MIN_VOLUME_RATIO_1M = 1.15
BREAKOUT_EXPANSION_ATR = 1.15
RETEST_MAX_WAIT_SEC = 1200          # Wait up to 20 mins for 1m retest confirmation
RETEST_TOLERANCE_ATR = 0.35         # Price pullback within 0.35 * ATR of breakout level
RETEST_MAX_DEPTH_ATR = 0.75         # Cancel setup if pullback goes deeper than 0.75 * ATR
MICRO_BUFFER_ATR = 0.12

# Exit Parameters (1R Runner + 2R Target + No-Momentum Decay)
T1_RUNNER_R = 1.00
T2_TARGET_R = 2.00
NO_MOMENTUM_BARS_1M = 5
NO_MOMENTUM_MOVE_ATR = 0.25

# Session & Execution Controls
SESSION_MAX_RUNTIME_SEC = 20700
INDEX_START_TIME = "09:15"
INDEX_END_TIME = "15:10"
INDEX_EXIT_TIME = "15:15"

MCX_START_TIME = "09:15"
MCX_END_TIME = "22:30"
MCX_EXIT_TIME = "23:25"

REENTRY_COOLDOWN_SEC = 600          # 10-min cooldown per symbol after exit
MAX_ACTIVE_TRADES_TOTAL = 5         # Max 1 active trade per symbol, up to 5 total
STARTUP_WARMUP_SEC = 60
DASHBOARD_PULSE_SEC = 5.0
MACRO_REFRESH_SEC = 180
ENFORCE_RISK_CAPS = False           # Unrestricted 1-lot ATM execution (no max loss / 3-loss pause)

JOURNAL_FILE = "live_journal/trade_journal.csv"
ACTIVE_STATE_FILE = "live_journal/active_positions.json"
PARAMS_FILE = "live_journal/strategy_params.json"
MACRO_FILE = "live_journal/macro_context.json"
DASHBOARD_STATE_FILE = "live_journal/dashboard_state.json"
LOCK_FILE = "live_journal/engine.lock"

JOURNAL_FIELDNAMES = [
    "trade_no", "date", "entry_time", "exit_time", "symbol", "contract", "side",
    "qty", "pnl_units", "entry_price", "exit_price", "points", "sl", "target", "exit_reason",
    "gross_pnl", "brokerage", "taxes_other", "charges", "pnl", "day_pnl", "setup",
]

INDICES = {
    "NIFTY":      {"type": "INDEX_OPT", "spot_key": "NSE_INDEX|Nifty 50", "tf_min": 5},
    "BANKNIFTY":  {"type": "INDEX_OPT", "spot_key": "NSE_INDEX|Nifty Bank", "tf_min": 5},
    "FINNIFTY":   {"type": "INDEX_OPT", "spot_key": "NSE_INDEX|Nifty Fin Service", "tf_min": 5},
    "MIDCPNIFTY": {"type": "INDEX_OPT", "spot_key": "NSE_INDEX|NIFTY MID SELECT", "tf_min": 5},
    "SENSEX":     {"type": "INDEX_OPT", "spot_key": "BSE_INDEX|SENSEX", "tf_min": 5},
    "CRUDEOIL":   {"type": "MCX_FUT", "tf_min": 5},
    "NATURALGAS": {"type": "MCX_FUT", "tf_min": 5},
    "GOLD":       {"type": "MCX_FUT", "tf_min": 5},
    "SILVER":     {"type": "MCX_FUT", "tf_min": 5},
    "COPPER":     {"type": "MCX_FUT", "tf_min": 5},
}

MCX_MINI_SYMBOLS = {
    "CRUDEOIL":   "CRUDEOILM",
    "NATURALGAS": "NATGASMINI",
    "GOLD":       "GOLDM",
    "SILVER":     "SILVERM",
    "COPPER":     "COPPER",
}

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


def inspect_special_session_schedule(now_ist: datetime) -> dict:
    d_str = now_ist.strftime("%Y-%m-%d")
    info = {
        "is_holiday": False,
        "description": "",
        "nse_open": True,
        "nse_start": INDEX_START_TIME,
        "nse_end": INDEX_END_TIME,
        "nse_exit": INDEX_EXIT_TIME,
        "mcx_open": True,
        "mcx_start": MCX_START_TIME,
        "mcx_end": MCX_END_TIME,
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
            info["mcx_start"] = "17:00"

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
        log.info(f"[GSHEET] Synced Trade #{trade_row['trade_no']} to Google Sheets.")
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
                "trade_no": t_no,
                "date": d_str,
                "entry_time": e_time,
                "exit_time": x_time,
                "symbol": sym,
                "contract": str(st.get("contract", "")).strip(),
                "side": str(st.get("side", "")).strip(),
                "qty": qty_v,
                "pnl_units": units_v,
                "entry_price": float(st.get("entry_price", 0.0) or 0.0),
                "exit_price": float(st.get("exit_price", 0.0) or 0.0),
                "points": float(st.get("points", 0.0) or 0.0),
                "sl": float(st.get("sl", 0.0) or 0.0),
                "target": float(st.get("target", 0.0) or 0.0),
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
# 2. TELEGRAM UI (EXACT DASHBOARD PRESERVED) & BROKERAGE CHARGES
# ==============================================================================
def notify(msg: str, silent: bool = False):
    clean_lines = [line.strip() for line in msg.splitlines() if line.strip()]
    log.info(f"[MIRROR] {' | '.join(clean_lines)}")

    if is_overnight_dead_hours(datetime.now(IST)):
        return

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


def load_saved_dashboard_id() -> int:
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    if os.path.isfile(DASHBOARD_STATE_FILE):
        try:
            with open(DASHBOARD_STATE_FILE, "r", encoding="utf-8") as f:
                st = json.load(f)
            if st.get("date") == today_str and st.get("message_id"):
                return int(st["message_id"])
        except Exception:
            pass
    return None


def save_dashboard_id(msg_id: int):
    if not msg_id:
        return
    os.makedirs("live_journal", exist_ok=True)
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        with open(DASHBOARD_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": today_str, "message_id": int(msg_id)}, f)
    except Exception:
        pass


def upsert_telegram_dashboard(text: str, message_id: int = None) -> int:
    if is_overnight_dead_hours(datetime.now(IST)):
        return message_id

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return message_id

    if message_id:
        edit_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id, "text": text, "parse_mode": "HTML"}
        try:
            resp = requests.post(edit_url, json=payload, timeout=8).json()
            desc = str(resp.get("description", "")).lower()
            if resp.get("ok") or "message is not modified" in desc:
                return message_id
        except Exception:
            pass
    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_notification": True}
    try:
        resp = requests.post(send_url, json=payload, timeout=8).json()
        if resp.get("ok"):
            new_id = resp["result"]["message_id"]
            save_dashboard_id(new_id)
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


def fetch_exact_trade_charges(instrument_key: str, buy_price: float, sell_price: float, eff_qty: int, is_mcx: bool) -> dict:
    if buy_price > 0 and sell_price > 0 and eff_qty > 0:
        if HAS_UPSTOX_SDK:
            try:
                cfg = upstox_client.Configuration()
                cfg.access_token = UPSTOX_ACCESS_TOKEN
                charge_api = upstox_client.ChargeApi(upstox_client.ApiClient(cfg))
                buy_resp = charge_api.get_brokerage(instrument_key, int(eff_qty), "I", "BUY", round(float(buy_price), 2), "2.0")
                sell_resp = charge_api.get_brokerage(instrument_key, int(eff_qty), "I", "SELL", round(float(sell_price), 2), "2.0")
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
                "quantity": int(eff_qty),
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

    turnover = (buy_price + sell_price) * eff_qty
    brokerage = 40.0
    if is_mcx:
        stt_ctt = (sell_price * eff_qty) * 0.0001
        txn_charges = turnover * 0.000021
        stamp_duty = (buy_price * eff_qty) * 0.00002
    else:
        stt_ctt = (sell_price * eff_qty) * 0.000625
        txn_charges = turnover * 0.0005
        stamp_duty = (buy_price * eff_qty) * 0.00003
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
        log.info(f"[JOURNAL] Saved #{row['trade_no']} to {JOURNAL_FILE}: {row['contract']} Net PnL ₹{row['pnl']:+.2f}")
    except Exception as e:
        log.error(f"Journal write error: {e}")

    push_trade_to_google_sheet(row)


# ==============================================================================
# 3. UPSTOX V3 API HELPERS (DYNAMIC BOD MCX MASTER + 7-DAY AUTO-ROLLOVER)
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
            log.info(f"[MCX BOD MASTER] Loaded {len(_MCX_BOD_CACHE)} active MCX_FO FUT contracts dynamically.")
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

    contract_info = {
        "instrument_key": str(top.get("instrument_key", "")),
        "lot_size": order_lot_size,
        "pnl_multiplier": pnl_per_lot_pt,
        "minimum_lot": min_lot,
        "freeze_quantity": int(top.get("freeze_quantity", 10000) or 10000),
        "tick_size": tick_size if tick_size > 0 else 0.05,
        "trading_symbol": str(top.get("trading_symbol", target_sym)),
        "expiry": _norm_contract_expiry(top.get("expiry")),
    }
    if is_mcx:
        log.info(
            f"[MCX MINI] {idx} -> {contract_info['trading_symbol']} | "
            f"1 Lot = {contract_info['lot_size']} Qty (₹{contract_info['pnl_multiplier']}/pt) | Tick = ₹{contract_info['tick_size']}"
        )
    return contract_info


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


def fetch_intraday_candles(instrument_key: str, interval_min: int = 5) -> pd.DataFrame:
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
# 4. CLEAN INDICATORS (1M 20-CANDLE BREAKOUT + 5M TREND & FUEL SCORE)
# ==============================================================================
def add_1m_breakout_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    x = df.copy()
    prev_close = x["close"].shift(1).fillna(x["open"])
    tr = pd.concat([
        x["high"] - x["low"],
        (x["high"] - prev_close).abs(),
        (x["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    x["tr"] = tr
    x["atr14"] = tr.rolling(14, min_periods=1).mean()
    x["ema20"] = x["close"].ewm(span=20, adjust=False).mean()
    x["ema50"] = x["close"].ewm(span=50, adjust=False).mean()

    tp = (x["high"] + x["low"] + x["close"]) / 3.0
    day_ser = x["timestamp"].dt.date
    vol_safe = x["volume"].replace(0, 1.0)
    x["vwap"] = (tp * vol_safe).groupby(day_ser).cumsum() / vol_safe.groupby(day_ser).cumsum()
    vol_avg = x["volume"].rolling(20, min_periods=1).mean().replace(0, np.nan)
    x["volume_ratio"] = (x["volume"] / vol_avg).fillna(1.0)

    # 20-Candle Breakout Levels on 1-minute chart
    x["high20"] = x["high"].rolling(20, min_periods=10).max().shift(1)
    x["low20"] = x["low"].rolling(20, min_periods=10).min().shift(1)
    return x


def compute_5m_trend_and_fuel(spot_df: pd.DataFrame, fut_df: pd.DataFrame) -> pd.DataFrame:
    if spot_df.empty:
        return spot_df

    prev_close = spot_df["close"].shift(1).fillna(spot_df["open"])
    tr = pd.concat([spot_df["high"] - spot_df["low"], (spot_df["high"] - prev_close).abs(), (spot_df["low"] - prev_close).abs()], axis=1).max(axis=1)
    spot_df["tr"] = tr
    spot_df["atr14"] = tr.rolling(14, min_periods=1).mean()
    spot_df["prev20_atr"] = tr.rolling(20, min_periods=1).mean().shift(1).fillna(tr)

    up = spot_df["high"].diff()
    dn = -spot_df["low"].diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr_sum = tr.rolling(14, min_periods=1).sum().replace(0, np.nan)
    plus_di = 100.0 * pd.Series(plus_dm).rolling(14, min_periods=1).sum() / tr_sum
    minus_di = 100.0 * pd.Series(minus_dm).rolling(14, min_periods=1).sum() / tr_sum
    dx = (100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0.0)
    spot_df["adx14"] = dx.rolling(14, min_periods=1).mean().fillna(20.0)

    if not fut_df.empty and "volume" in fut_df.columns and fut_df["volume"].sum() > 0:
        fut_vol = fut_df[["timestamp", "volume"]].rename(columns={"volume": "fut_vol"})
        spot_df = pd.merge_asof(spot_df.sort_values("timestamp"), fut_vol.sort_values("timestamp"), on="timestamp", direction="nearest")
        spot_df["active_vol"] = spot_df["fut_vol"].fillna(0.0)
    else:
        spot_df["active_vol"] = spot_df["volume"]

    spot_df["ema20"] = spot_df["close"].ewm(span=20, adjust=False).mean()
    spot_df["ema50"] = spot_df["close"].ewm(span=50, adjust=False).mean()
    tp = (spot_df["high"] + spot_df["low"] + spot_df["close"]) / 3.0
    day_ser = spot_df["timestamp"].dt.date
    vol_safe = spot_df["active_vol"].replace(0, 1.0)
    spot_df["vwap"] = (tp * vol_safe).groupby(day_ser).cumsum() / vol_safe.groupby(day_ser).cumsum()

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

    spot_df["fuel"] = pd.Series(fuel_scores).ewm(span=3, adjust=False).mean().round(1)
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
            "tag": "V3_20BO_RETEST", "instrument_token": instrument_key,
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
# 6. CLEAN 1M 20-CANDLE BREAKOUT + RETEST ENGINE (INDEX & MCX)
# ==============================================================================
class IntradayV3Engine:
    def __init__(self):
        self.expiries = {}
        self.fut_contracts = {}
        self.pending_setups = {idx: None for idx in INDICES}
        self.last_exit_ts = {idx: 0.0 for idx in INDICES}
        self.last_exit_candle_ts = {idx: "" for idx in INDICES}
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
        self.latest_1m_dfs = {}
        self.dashboard_msg_id = load_saved_dashboard_id()
        self.dashboard_pulse = False
        self.dashboard_lock = threading.Lock()
        self.last_macro_ts = 0.0
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
            "nse_start": INDEX_START_TIME,
            "nse_end": INDEX_END_TIME,
            "nse_exit": INDEX_EXIT_TIME,
            "mcx_open": True,
            "mcx_start": MCX_START_TIME,
            "mcx_end": MCX_END_TIME,
            "mcx_exit": MCX_EXIT_TIME,
            "is_muhurat": False,
        }
        self.ai_agent = TickByTickAITradingAgent(self) if HAS_AI_AGENT else None

        init_cap = max(float(os.getenv("CAPITAL_PER_TRADE") or "25000"), 25000.0)
        init_mcx_cap = max(float(os.getenv("MCX_CAPITAL_PER_TRADE") or "150000"), 150000.0)
        self.tuned_params = {
            "brain_version": "v2.0-20BO",
            "driver_gear": 1,
            "capital_per_trade": init_cap,
            "mcx_capital_per_trade": init_mcx_cap,
            "min_fuel_score": MIN_FUEL_SCORE,
            "mcx_min_fuel_score": MCX_MIN_FUEL_SCORE,
            "min_adx_trend": MIN_ADX_TREND,
            "bullish_pcr_min": 1.15,
            "bearish_pcr_max": 0.85,
            "symbol_overrides": {},
        }
        self.macro_context = {
            "threat_level": "NORMAL",
            "stance_label": "1M_20BO_RETEST",
            "blocked_sides": [],
            "eject_sides": [],
            "blocked_pairs": [],
            "priority_pairs": [],
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

    def load_self_tuned_intelligence(self):
        if os.path.isfile(PARAMS_FILE):
            try:
                with open(PARAMS_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self.tuned_params.update(loaded)
                self.tuned_params["min_fuel_score"] = MIN_FUEL_SCORE
                self.tuned_params["mcx_min_fuel_score"] = MCX_MIN_FUEL_SCORE
                log.info(
                    f"[ENGINE LOADED] {self.tuned_params.get('brain_version')} | "
                    f"IdxFuel(5m)={MIN_FUEL_SCORE} | McxFuel(5m)={MCX_MIN_FUEL_SCORE} | "
                    f"Strategy=1M_20CANDLE_BREAKOUT_RETEST | Mode=UNRESTRICTED_1LOT_ATM"
                )
            except Exception as e:
                log.warning(f"Failed to load strategy_params.json: {e}")
        if os.path.isfile(MACRO_FILE):
            try:
                with open(MACRO_FILE, "r", encoding="utf-8") as f:
                    self.macro_context.update(json.load(f))
            except Exception:
                pass

    def refresh_live_macro_news(self, force: bool = False):
        now_ts = time.time()
        if not force and (now_ts - self.last_macro_ts) < MACRO_REFRESH_SEC:
            return
        self.last_macro_ts = now_ts
        live_pcrs = {
            idx: s["pcr"]
            for idx, s in self.last_summaries.items()
            if not s.get("is_mcx") and s.get("pcr", 0) > 0
        }
        if HAS_MACRO_RADAR:
            try:
                ctx = evaluate_macro_context(notify_user=False, live_pcrs=live_pcrs)
                if isinstance(ctx, dict):
                    self.macro_context.update(ctx)
                    self.macro_context["stance_label"] = "1M_20BO_RETEST"
                return
            except Exception:
                pass

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

                    q_data = live_quotes.get(inst_key, {})
                    curr_ltp = float(q_data.get("last_price", pos.get("last_ltp", entry)))
                    pos["last_ltp"] = curr_ltp

                    curr_gain = (entry - curr_ltp) if is_short else (curr_ltp - entry)
                    if curr_gain > pos.get("peak_gain", 0.0):
                        pos["peak_gain"] = curr_gain

                    peak_gain = pos.get("peak_gain", 0.0)
                    peak_r = peak_gain / r_unit

                    if peak_r >= T1_RUNNER_R:
                        pos["runner_mode"] = True
                        be_lock = round_to_tick((entry - 0.15 * r_unit) if is_short else (entry + 0.15 * r_unit), tick_sz)
                        trail_gap = max(0.50 * r_unit, 0.35 * peak_gain)
                        cand_sl = round_to_tick((entry - peak_gain + trail_gap) if is_short else (entry + peak_gain - trail_gap), tick_sz)
                        if is_short:
                            best_sl = min(be_lock, cand_sl)
                            if best_sl < pos["sl"]:
                                pos["sl"] = best_sl
                                pos["trail_stage"] = "RUNNER TRAIL"
                        else:
                            best_sl = max(be_lock, cand_sl)
                            if best_sl > pos["sl"]:
                                pos["sl"] = best_sl
                                pos["trail_stage"] = "RUNNER TRAIL"

                    self.active_positions[idx] = pos
                    self.trade_counter = max(self.trade_counter, int(pos.get("trade_no", 0)), len(self.closed_trades))

                    t2_price = float(pos.get("t2_price", 0.0))
                    if t2_price > 0 and ((not is_short and curr_ltp >= t2_price) or (is_short and curr_ltp <= t2_price)):
                        immediate_exits.append((idx, curr_ltp, "TARGET_2R_HIT"))
                    elif (not is_short and curr_ltp <= pos["sl"]) or (is_short and curr_ltp >= pos["sl"]):
                        reason = "RUNNER_TRAIL_SL" if pos.get("runner_mode") else "STOP_LOSS_HIT"
                        immediate_exits.append((idx, curr_ltp, reason))

            for idx, pos in list(self.active_positions.items()):
                self.subscribe_tick_instrument(pos["instrument_key"])

            for idx, exit_ltp, reason in immediate_exits:
                self.close_position_now(idx, exit_ltp, reason, now_ist)

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
                        self.last_exit_ts[t["symbol"]] = max(self.last_exit_ts.get(t["symbol"], 0.0), ex_dt.timestamp())
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
                    active_keys = [p["instrument_key"] for p in self.active_positions.values()]
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
                            if self.ai_agent:
                                self.ai_agent.on_market_tick(inst_key, float(ltpc["ltp"]))
                            else:
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
        """Real-time tick exit engine: 2R Target, 1R Runner Mode + Trailing SL, and Stop-Loss."""
        norm_key = instrument_key.replace(":", "|")
        target_idx, exit_reason = None, None

        with self.pos_lock:
            for idx, pos in self.active_positions.items():
                if pos["instrument_key"].replace(":", "|") == norm_key:
                    pos["last_ltp"] = ltp
                    is_short = pos["tx_side"] == "SELL"
                    entry = pos["entry_price"]
                    curr_gain = (entry - ltp) if is_short else (ltp - entry)
                    if curr_gain > pos.get("peak_gain", 0.0):
                        pos["peak_gain"] = curr_gain
                        pos["last_peak_ts"] = time.time()

                    r_unit = max(pos.get("r_unit", 1.0), 1e-6)
                    tick_sz = pos.get("tick_size", 0.05)
                    t1_price = float(pos.get("t1_price", 0.0))
                    t2_price = float(pos.get("t2_price", 0.0))

                    # 1. Full Exit at 2R Target
                    if t2_price > 0 and ((not is_short and ltp >= t2_price) or (is_short and ltp <= t2_price)):
                        exit_reason = "TARGET_2R_HIT"
                        target_idx = idx
                        break

                    # 2. Arm Runner Mode at 1R (lock +0.15R profit immediately)
                    if t1_price > 0 and not pos.get("runner_mode", False):
                        if (not is_short and ltp >= t1_price) or (is_short and ltp <= t1_price):
                            pos["runner_mode"] = True
                            lock_sl = round_to_tick((entry - 0.15 * r_unit) if is_short else (entry + 0.15 * r_unit), tick_sz)
                            if (is_short and lock_sl < pos["sl"]) or (not is_short and lock_sl > pos["sl"]):
                                pos["sl"] = lock_sl
                            pos["trail_stage"] = "RUNNER 1R+"

                    # 3. Trail behind peak once in Runner Mode (never loosen stop)
                    if pos.get("runner_mode", False):
                        peak_gain = pos.get("peak_gain", 0.0)
                        trail_gap = max(0.50 * r_unit, 0.35 * peak_gain)
                        cand_sl = round_to_tick((entry - peak_gain + trail_gap) if is_short else (entry + peak_gain - trail_gap), tick_sz)
                        if (is_short and cand_sl < pos["sl"]) or (not is_short and cand_sl > pos["sl"]):
                            pos["sl"] = cand_sl
                            pos["trail_stage"] = "RUNNER TRAIL"

                    # 4. Stop-Loss / Trailing Stop Hit
                    if not is_short and ltp <= pos["sl"]:
                        exit_reason = "RUNNER_TRAIL_SL" if pos.get("runner_mode") else "STOP_LOSS_HIT"
                        target_idx = idx
                    elif is_short and ltp >= pos["sl"]:
                        exit_reason = "RUNNER_TRAIL_SL" if pos.get("runner_mode") else "STOP_LOSS_HIT"
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
        hhmm = now_ist.strftime("%H:%M")
        nearest = valid_exps[0]
        is_today_exp = detect_expiry_for_date(now_ist, nearest) if HAS_AUTO_TUNER else (nearest == now_ist.strftime("%Y-%m-%d"))
        if is_today_exp and hhmm > "14:45" and len(valid_exps) > 1:
            return valid_exps[1], False
        return nearest, is_today_exp

    def is_within_trade_window(self, idx: str, now_ist: datetime) -> bool:
        hhmm = now_ist.strftime("%H:%M")
        if INDICES[idx]["type"] == "MCX_FUT":
            if not self.session_sched.get("mcx_open", True):
                return False
            return self.session_sched.get("mcx_start", MCX_START_TIME) <= hhmm <= self.session_sched.get("mcx_end", MCX_END_TIME)
        if not self.session_sched.get("nse_open", True):
            return False
        return self.session_sched.get("nse_start", INDEX_START_TIME) <= hhmm <= self.session_sched.get("nse_end", INDEX_END_TIME)

    def analyze_oi_and_pcr(self, idx: str, chain: list):
        if not chain:
            return 1.0, 1.0, 0.0, True, True
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

        total_mtm = 0.0
        for pos in positions_snap:
            ltp = pos.get("last_ltp", pos["entry_price"])
            is_short = pos["tx_side"] == "SELL"
            eff_units = float(pos.get("pnl_units", pos["qty"]))
            total_mtm += (pos["entry_price"] - ltp) * eff_units if is_short else (ltp - pos["entry_price"]) * eff_units
        return round(total_mtm, 2)

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
                            past_rows.append({
                                "date": d_str,
                                "year": y_iso,
                                "week": w_iso,
                                "pnl": float(r.get("pnl", 0.0)),
                            })
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

    # ==========================================================================
    # TELEGRAM DASHBOARD (KEPT EXACTLY AS ORIGINAL)
    # ==========================================================================
    def render_live_dashboard(self, hhmm: str):
        with self.dashboard_lock:
            now_ist = datetime.now(IST)
            live_gross_mtm = self.compute_live_mtm()
            with self.pos_lock:
                open_charges = sum(float(p.get("est_charges", 180.0 if p["type"] == "MCX_FUT" else 65.0)) for p in self.active_positions.values())
                active_snap = sorted(self.active_positions.items(), key=lambda item: item[1]["trade_no"])

            live_net_mtm = round(live_gross_mtm - open_charges, 2) if active_snap else 0.0
            net_pnl = round(self.day_pnl + live_net_mtm, 2)
            mode_badge = "<code>PAPER</code>" if PAPER_MODE else "<code>LIVE</code>"
            gear_badge = f"<code>G{self.tuned_params.get('driver_gear', 1)}</code>"
            net_icon = "🟢" if net_pnl >= 0 else "🔴"

            self.dashboard_pulse = not self.dashboard_pulse
            if "CLOSED" in self.bot_status_text or "STOPPED" in self.bot_status_text:
                status_header = "🔴 <b>MARKET CLOSED</b>"
            elif "PAUSED" in self.bot_status_text:
                status_header = f"🟡 <b>{self.bot_status_text}</b>"
            elif "STANDBY" in self.bot_status_text:
                status_header = f"🟠 <b>{self.bot_status_text}</b>"
            else:
                pulse_dot = "🟢" if self.dashboard_pulse else "⚪"
                status_header = f"{pulse_dot} <b>BOT LIVE</b>"

            tot_closed = len(self.closed_trades)
            total_wins = sum(1 for t in self.closed_trades if t["pnl"] > 0)
            total_losses = sum(1 for t in self.closed_trades if t["pnl"] <= 0)
            day_wr = int(round((total_wins / tot_closed) * 100.0)) if tot_closed > 0 else 0

            lines = [
                f"{status_header}  |  <code>{hhmm} IST</code>  |  {mode_badge} {gear_badge}",
                f"────────────────────────────",
                f"💼 <b>Day Net P&amp;L:</b> {net_icon} <b>₹{net_pnl:+,.2f}</b>",
                f"• Realized: <b>₹{self.day_pnl:+,.2f}</b>  |  Live MTM: <b>₹{live_net_mtm:+,.2f}</b>",
                f"• Trades: <b>{tot_closed}</b> ({total_wins}W / {total_losses}L)  |  Win Rate: <b>{day_wr}%</b>",
            ]

            if active_snap:
                lines.append(f"\n🔥 <b>ACTIVE POSITIONS ({len(active_snap)})</b>")
                for p_idx, pos in active_snap:
                    ltp = pos.get("last_ltp", pos["entry_price"])
                    is_short = pos["tx_side"] == "SELL"
                    est_charges = float(pos.get("est_charges", 180.0 if pos["type"] == "MCX_FUT" else 65.0))
                    eff_units = int(pos.get("pnl_units", pos["qty"]))
                    pts_diff = round((pos["entry_price"] - ltp) if is_short else (ltp - pos["entry_price"]), 2)
                    gross_val = round(pts_diff * eff_units, 2)
                    pnl = round(gross_val - est_charges, 2)
                    sl_gross = (pos["entry_price"] - pos["sl"]) * eff_units if is_short else (pos["sl"] - pos["entry_price"]) * eff_units
                    sl_net_pnl = round(sl_gross - est_charges, 0)
                    t_mode = pos.get("trade_mode", "20BO-RT")
                    tgt_txt = pos.get("target_label", "1R RUN / 2R TGT")
                    trail_tag = f"🛡️ <code>{pos['trail_stage']}</code>" if pos.get("trail_stage") else f"<code>{t_mode}</code>"
                    p_icon = "🟢" if pnl >= 0 else "🔴"
                    sl_label = f"Locked <b>₹{sl_net_pnl:+,.0f}</b>" if sl_net_pnl >= 0 else f"Max Risk <b>₹{abs(sl_net_pnl):,.0f}</b>"

                    lot_sz = int(pos.get("lot_size", pos["qty"]))
                    num_lots = int(pos.get("lots", max(1, pos["qty"] // max(lot_sz, 1))))
                    lot_spec_txt = (
                        f"{num_lots} Lot = {pos['qty']} Qty"
                        if num_lots == 1
                        else f"{num_lots} Lots × {lot_sz} = {pos['qty']} Qty"
                    )

                    lines.extend([
                        f"• <b>#{pos['trade_no']} {p_idx}</b> <code>{pos['strike']} {pos['display_side']}</code> (<code>{lot_spec_txt}</code>)  |  {trail_tag}",
                        f"  In: <b>₹{pos['entry_price']:.2f}</b> (<code>{pos['entry_time'][:5]}</code>) → LTP: <b>₹{ltp:.2f}</b>",
                        f"  PnL: <code>{pts_diff:.2f} pt × {eff_units} = ₹{gross_val:,.0f}</code>",
                        f"  SL: <code>₹{pos['sl']:.2f}</code> ({sl_label})  |  Target: <code>{tgt_txt}</code>",
                        f"  Live Net P&amp;L: {p_icon} <b>₹{pnl:+,.2f}</b>",
                    ])

            lines.append("\n📒 <b>TODAY'S CLOSED TRADES</b>")
            if self.closed_trades:
                for t in sorted(self.closed_trades, key=lambda x: x["trade_no"]):
                    icon = "✅" if t["pnl"] >= 0 else "❌"
                    pts_t = float(t.get("points", 0.0))
                    units_t = int(float(t.get("pnl_units") or t.get("qty", 1)))
                    gross_t = float(t.get("gross_pnl", round(pts_t * units_t, 2)))
                    lines.append(
                        f"• <b>#{t['trade_no']} {t['symbol']}</b> <code>{t['contract']} {t['side']}</code>  {icon} Net <b>₹{t['pnl']:+,.2f}</b>\n"
                        f"  <code>{t['entry_time'][:5]}→{t['exit_time'][:5]}</code> | In: ₹{t['entry_price']:.2f} → Exit: ₹{t['exit_price']:.2f}\n"
                        f"  PnL: <code>{pts_t:.2f} pt × {units_t} = ₹{gross_t:,.0f}</code> | <i>{t['exit_reason']}</i>"
                    )
            elif not active_snap:
                lines.append("<i>No closed trades yet today.</i>")

            if self.last_summaries:
                ai_stance = self.macro_context.get("stance_label", "1M_20BO_RETEST")
                lines.append(f"\n📡 <b>INDICES LIVE (5m)</b>  |  🧠 <code>{ai_stance}</code>")
                for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"):
                    s = self.last_summaries.get(idx)
                    if s:
                        lines.append(f"• <b>{s['idx']}:</b> <code>{s['spot']}</code> | PCR <b>{s['pcr']:.2f}</b> | Fuel <b>{s['fuel']}</b> | <code>{s['short_status']}</code>")

                lines.append("\n🛢️ <b>MCX MINI LIVE (5m)</b>")
                for idx in ("CRUDEOIL", "NATURALGAS", "GOLD", "SILVER", "COPPER"):
                    s = self.last_summaries.get(idx)
                    if s:
                        lines.append(f"• <b>{s['idx']}:</b> <code>{s['spot']}</code> | Fuel <b>{s['fuel']}</b> | <code>{s['short_status']}</code>")

            lines.append("\n📊 <b>PERFORMANCE SUMMARY (NET ₹)</b>")
            lines.extend(self.get_performance_summary_lines(now_ist, live_net_mtm))

            self.dashboard_msg_id = upsert_telegram_dashboard("\n".join(lines), self.dashboard_msg_id)

    def start_heartbeat_thread(self):
        def _pulse_loop():
            while not self.stop_requested:
                time.sleep(DASHBOARD_PULSE_SEC)
                if self.stop_requested or not self.dashboard_msg_id or not self.last_summaries:
                    continue
                if "CLOSED" in self.bot_status_text or "STOPPED" in self.bot_status_text:
                    break
                try:
                    with self.pos_lock:
                        active_keys = [p["instrument_key"] for p in self.active_positions.values()]
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

    def close_position_now(self, idx: str, ltp: float, exit_reason: str, now_ist: datetime):
        with self.pos_lock:
            pos = self.active_positions.pop(idx, None)
        if not pos:
            return

        self.last_exit_ts[idx] = time.time()
        self.pending_setups[idx] = None
        df_curr = self.latest_1m_dfs.get(idx)
        if df_curr is not None and not df_curr.empty:
            self.last_exit_candle_ts[idx] = str(df_curr["timestamp"].iloc[-1])

        self.unsubscribe_tick_instrument(pos["instrument_key"])
        self.save_active_positions_state()

        is_short = pos["tx_side"] == "SELL"
        is_mcx = pos["type"] == "MCX_FUT"
        close_side = "BUY" if is_short else "SELL"
        place_marketable_limit_order(pos["instrument_key"], pos["qty"], close_side, ltp, pos.get("tick_size", 0.05))

        eff_units = float(pos.get("pnl_units", pos["qty"]))
        pts = round((pos["entry_price"] - ltp) if is_short else (ltp - pos["entry_price"]), 2)
        gross_pnl = round(pts * eff_units, 2)

        buy_px = ltp if is_short else pos["entry_price"]
        sell_px = pos["entry_price"] if is_short else ltp
        ch_info = fetch_exact_trade_charges(pos["instrument_key"], buy_px, sell_px, int(eff_units), is_mcx)
        net_pnl = round(gross_pnl - ch_info["total"], 2)

        self.day_pnl = round(self.day_pnl + net_pnl, 2)
        self.consecutive_losses = (self.consecutive_losses + 1) if net_pnl < 0 else 0

        exit_time_str = now_ist.strftime("%H:%M:%S")
        journal_entry = {
            "trade_no": pos["trade_no"], "date": now_ist.strftime("%Y-%m-%d"),
            "entry_time": pos.get("entry_time", ""), "exit_time": exit_time_str,
            "symbol": idx, "contract": str(pos["strike"]), "side": pos["display_side"],
            "qty": pos["qty"], "pnl_units": int(eff_units),
            "entry_price": pos["entry_price"], "exit_price": ltp,
            "points": pts, "sl": pos["sl"], "target": pos.get("t2_price", 0.0), "exit_reason": exit_reason,
            "gross_pnl": gross_pnl, "brokerage": ch_info["brokerage"],
            "taxes_other": ch_info["taxes_other"], "charges": ch_info["total"],
            "pnl": net_pnl, "day_pnl": self.day_pnl, "setup": pos["setup_name"],
        }
        self.closed_trades.append(journal_entry)
        record_trade_journal(journal_entry)
        self.render_live_dashboard(now_ist.strftime("%H:%M"))

    # ==========================================================================
    # EXIT LOGIC (1M CANDLE CHECKS: UNDERLYING SL, RUNNER STRUCTURE, NO-MOMENTUM)
    # ==========================================================================
    def evaluate_active_candle_exits(self, idx: str, df_1m: pd.DataFrame, spot_close: float, atr14_1m: float, now_ist: datetime):
        with self.pos_lock:
            pos = self.active_positions.get(idx)
        if not pos or len(df_1m) < 3:
            return

        is_short = pos["tx_side"] == "SELL"
        entry = pos["entry_price"]
        ltp = pos.get("last_ltp", entry)
        tick_sz = pos.get("tick_size", 0.05)

        # 1. Underlying Thesis Invalidation (Spot/Future SL hit)
        spot_sl = float(pos.get("spot_sl", 0.0))
        if spot_sl > 0:
            if pos["side"] == "CE" and spot_close <= spot_sl:
                self.close_position_now(idx, ltp, "UNDERLYING_SL_HIT", now_ist)
                return
            if pos["side"] == "PE" and spot_close >= spot_sl:
                self.close_position_now(idx, ltp, "UNDERLYING_SL_HIT", now_ist)
                return

        curr_vwap = float(df_1m.iloc[-1].get("vwap", spot_close))

        # 2. Runner Mode Underlying Trail & VWAP Structure Failure
        if pos.get("runner_mode", False) and len(df_1m) >= 5:
            if pos["side"] == "CE":
                swing_low = float(df_1m["low"].tail(5).min())
                new_u_sl = max(swing_low, curr_vwap) - (MICRO_BUFFER_ATR * atr14_1m)
                if new_u_sl > spot_sl:
                    pos["spot_sl"] = round_to_tick(new_u_sl, tick_sz)
                if spot_close < curr_vwap and spot_close < float(df_1m.iloc[-2]["close"]):
                    self.close_position_now(idx, ltp, "RUNNER_VWAP_FAILURE", now_ist)
                    return
            else:
                swing_high = float(df_1m["high"].tail(5).max())
                new_u_sl = min(swing_high, curr_vwap) + (MICRO_BUFFER_ATR * atr14_1m)
                if spot_sl <= 0 or new_u_sl < spot_sl:
                    pos["spot_sl"] = round_to_tick(new_u_sl, tick_sz)
                if spot_close > curr_vwap and spot_close > float(df_1m.iloc[-2]["close"]):
                    self.close_position_now(idx, ltp, "RUNNER_VWAP_FAILURE", now_ist)
                    return

        # 3. 1m No-Momentum Premium Decay Guard (5 consecutive 1m bars stalled & underwater)
        if len(df_1m) >= NO_MOMENTUM_BARS_1M + 1 and atr14_1m > 0:
            recent = df_1m.tail(NO_MOMENTUM_BARS_1M + 1)
            move_1m = abs(float(recent.iloc[-1]["close"]) - float(recent.iloc[0]["close"]))
            underwater = (ltp >= entry * 1.005) if is_short else (ltp <= entry * 0.99)
            if move_1m < (NO_MOMENTUM_MOVE_ATR * atr14_1m) and underwater:
                self.close_position_now(idx, ltp, "NO_MOMENTUM_DECAY_1M", now_ist)

    # ==========================================================================
    # ENTRY LOGIC (5M TREND/FUEL + 1M 20-CANDLE BREAKOUT + 1M RETEST CONFIRM)
    # ==========================================================================
    def evaluate_instrument(self, idx: str, cfg: dict, now_ist: datetime):
        is_mcx = cfg["type"] == "MCX_FUT"
        if not cfg.get("spot_key"):
            return None

        # Fetch 5m candles for Trend/Fuel & 1m candles for 20-Candle Breakout + Retest
        spot_5m = fetch_intraday_candles(cfg["spot_key"], 5)
        spot_1m = fetch_intraday_candles(cfg["spot_key"], 1)
        if spot_5m.empty or spot_1m.empty or len(spot_1m) < 22:
            return None

        fut_key = self.fut_contracts.get(idx, {}).get("instrument_key", "")
        fut_5m = spot_5m if is_mcx else fetch_intraday_candles(fut_key, 5)
        df_5m = compute_5m_trend_and_fuel(spot_5m, fut_5m)
        df_1m = add_1m_breakout_indicators(spot_1m)

        self.latest_dfs[idx] = df_5m
        self.latest_1m_dfs[idx] = df_1m

        c5 = df_5m.iloc[-1]
        c1 = df_1m.iloc[-1]
        p1 = df_1m.iloc[-2]

        spot_close = float(c1["close"])
        atr14_1m = max(float(c1["atr14"]), cfg.get("tick_size", 0.05) * 2)
        curr_tr_1m = float(c1["tr"])
        vol_ratio_1m = float(c1["volume_ratio"])
        vwap_1m = float(c1["vwap"])
        ema20_1m = float(c1["ema20"])
        ema50_1m = float(c1["ema50"])
        high20_1m = c1["high20"]
        low20_1m = c1["low20"]

        fuel_5m = float(c5["fuel"])
        adx_5m = float(c5["adx14"])
        ema20_5m = float(c5["ema20"])
        ema50_5m = float(c5["ema50"])

        chain, active_expiry, is_today_exp = [], "", False
        if is_mcx:
            oi_bull_ok = spot_close > float(p1["close"])
            oi_bear_ok = spot_close < float(p1["close"])
            pcr, cum_pcr_diff = 0.0, 0.0
        else:
            active_expiry, is_today_exp = self.get_smart_expiry(idx, now_ist)
            chain = fetch_option_chain(cfg["spot_key"], active_expiry)
            pcr, _, cum_pcr_diff, oi_bull_ok, oi_bear_ok = self.analyze_oi_and_pcr(idx, chain)

        with self.pos_lock:
            in_trade = idx in self.active_positions

        if in_trade:
            self.pending_setups[idx] = None
            self.evaluate_active_candle_exits(idx, df_1m, spot_close, atr14_1m, now_ist)

            with self.pos_lock:
                pos = self.active_positions.get(idx)

            if pos:
                ltp_now = pos.get("last_ltp", spot_close if is_mcx else pos["entry_price"])
                is_short = pos["tx_side"] == "SELL"
                est_ch = float(pos.get("est_charges", 180.0 if is_mcx else 65.0))
                eff_u = float(pos.get("pnl_units", pos["qty"]))
                pos_mtm = ((pos["entry_price"] - ltp_now) * eff_u if is_short else (ltp_now - pos["entry_price"]) * eff_u) - est_ch
                return {
                    "idx": idx, "is_mcx": is_mcx,
                    "spot": round(spot_close, 2) if spot_close < 1000 else int(round(spot_close)),
                    "fuel": int(round(fuel_5m)), "pcr": pcr, "cum_pcr_diff": cum_pcr_diff,
                    "short_status": f"ACTIVE #{pos['trade_no']} (₹{pos_mtm:+.0f})",
                }
            return {
                "idx": idx, "is_mcx": is_mcx,
                "spot": round(spot_close, 2) if spot_close < 1000 else int(round(spot_close)),
                "fuel": int(round(fuel_5m)), "pcr": pcr, "cum_pcr_diff": cum_pcr_diff,
                "short_status": "COOLDOWN (10m)",
            }

        uptime_sec = time.time() - self.engine_boot_ts
        if uptime_sec < STARTUP_WARMUP_SEC:
            rem_w = max(1, int(STARTUP_WARMUP_SEC - uptime_sec))
            return {
                "idx": idx, "is_mcx": is_mcx,
                "spot": round(spot_close, 2) if spot_close < 1000 else int(round(spot_close)),
                "fuel": int(round(fuel_5m)), "pcr": pcr, "cum_pcr_diff": cum_pcr_diff,
                "short_status": f"WARMUP ({rem_w}s)",
            }

        sec_since_exit = time.time() - self.last_exit_ts.get(idx, 0.0)
        curr_candle_ts = str(c1["timestamp"])
        if sec_since_exit < REENTRY_COOLDOWN_SEC or curr_candle_ts == self.last_exit_candle_ts.get(idx, ""):
            rem_min = max(1, int(math.ceil((REENTRY_COOLDOWN_SEC - sec_since_exit) / 60.0)))
            self.pending_setups[idx] = None
            return {
                "idx": idx, "is_mcx": is_mcx,
                "spot": round(spot_close, 2) if spot_close < 1000 else int(round(spot_close)),
                "fuel": int(round(fuel_5m)), "pcr": pcr, "cum_pcr_diff": cum_pcr_diff,
                "short_status": f"COOLDOWN ({rem_min}m)",
            }

        with self.pos_lock:
            total_open_trades = len(self.active_positions)
        if total_open_trades >= MAX_ACTIVE_TRADES_TOTAL:
            b_lo = int(round(float(low20_1m))) if pd.notna(low20_1m) else int(spot_close)
            b_hi = int(round(float(high20_1m))) if pd.notna(high20_1m) else int(spot_close)
            return {
                "idx": idx, "is_mcx": is_mcx,
                "spot": round(spot_close, 2) if spot_close < 1000 else int(round(spot_close)),
                "fuel": int(round(fuel_5m)), "pcr": pcr, "cum_pcr_diff": cum_pcr_diff,
                "short_status": f"20BO {b_lo}-{b_hi}",
            }

        # Expire stale pending retest setup (> 20 mins)
        pending = self.pending_setups.get(idx)
        if pending and (now_ist - pending["armed_at"]).total_seconds() > RETEST_MAX_WAIT_SEC:
            self.pending_setups[idx] = None
            pending = None

        req_fuel = MCX_MIN_FUEL_SCORE if is_mcx else MIN_FUEL_SCORE

        # STEP A: ARM 1M 20-CANDLE BREAKOUT WHEN 5M FUEL & TREND ALIGN
        if pending is None and self.is_within_trade_window(idx, now_ist) and fuel_5m >= req_fuel and adx_5m >= MIN_ADX_TREND:
            bull_aligned = (spot_close > vwap_1m) and (ema20_1m > ema50_1m) and (ema20_5m >= ema50_5m) and oi_bull_ok
            bear_aligned = (spot_close < vwap_1m) and (ema20_1m < ema50_1m) and (ema20_5m <= ema50_5m) and oi_bear_ok

            if (
                pd.notna(high20_1m)
                and spot_close > float(high20_1m)
                and curr_tr_1m >= (BREAKOUT_EXPANSION_ATR * atr14_1m)
                and vol_ratio_1m >= MIN_VOLUME_RATIO_1M
                and bull_aligned
            ):
                side_label = "BUY" if is_mcx else "CE"
                pending = {
                    "side": "CE",
                    "display_side": side_label,
                    "name": f"1M_20BO_RETEST_{side_label}",
                    "breakout_level": round(float(high20_1m), 2),
                    "atr": atr14_1m,
                    "armed_at": now_ist,
                }
                self.pending_setups[idx] = pending

            elif (
                pd.notna(low20_1m)
                and spot_close < float(low20_1m)
                and curr_tr_1m >= (BREAKOUT_EXPANSION_ATR * atr14_1m)
                and vol_ratio_1m >= MIN_VOLUME_RATIO_1M
                and bear_aligned
            ):
                side_label = "SELL" if is_mcx else "PE"
                pending = {
                    "side": "PE",
                    "display_side": side_label,
                    "name": f"1M_20BO_RETEST_{side_label}",
                    "breakout_level": round(float(low20_1m), 2),
                    "atr": atr14_1m,
                    "armed_at": now_ist,
                }
                self.pending_setups[idx] = pending

        # STEP B: CONFIRM 1M RETEST & TRIGGER UNRESTRICTED 1-LOT ENTRY
        if pending and idx not in self.active_positions and self.is_within_trade_window(idx, now_ist):
            lvl = float(pending["breakout_level"])
            atr_ref = max(float(pending.get("atr", atr14_1m)), atr14_1m)
            tol = max(RETEST_TOLERANCE_ATR * atr_ref, cfg.get("tick_size", 0.05) * 4)
            max_depth = max(RETEST_MAX_DEPTH_ATR * atr_ref, cfg.get("tick_size", 0.05) * 8)

            if pending["side"] == "CE":
                depth = lvl - float(c1["low"])
                if depth > max_depth:
                    self.pending_setups[idx] = None
                    pending = None
                else:
                    retest_touched = (float(c1["low"]) <= lvl + tol) or (float(p1["low"]) <= lvl + tol)
                    confirmed = retest_touched and (spot_close > lvl) and (spot_close > float(p1["high"])) and (spot_close > vwap_1m)
                    if confirmed:
                        if is_mcx:
                            self.try_execute_mcx_fut_entry(idx, cfg, pending, df_1m, spot_close, atr_ref, now_ist)
                        else:
                            self.try_execute_option_entry(idx, cfg, pending, df_1m, spot_close, chain, atr_ref, now_ist, is_today_exp)
            else:
                depth = float(c1["high"]) - lvl
                if depth > max_depth:
                    self.pending_setups[idx] = None
                    pending = None
                else:
                    retest_touched = (float(c1["high"]) >= lvl - tol) or (float(p1["high"]) >= lvl - tol)
                    confirmed = retest_touched and (spot_close < lvl) and (spot_close < float(p1["low"])) and (spot_close < vwap_1m)
                    if confirmed:
                        if is_mcx:
                            self.try_execute_mcx_fut_entry(idx, cfg, pending, df_1m, spot_close, atr_ref, now_ist)
                        else:
                            self.try_execute_option_entry(idx, cfg, pending, df_1m, spot_close, chain, atr_ref, now_ist, is_today_exp)

        hhmm_now = now_ist.strftime("%H:%M")
        if pending:
            short_status = f"ARMED {pending['display_side']} RETEST"
        elif not self.is_within_trade_window(idx, now_ist) and hhmm_now < (self.session_sched.get("mcx_start", MCX_START_TIME) if is_mcx else self.session_sched.get("nse_start", INDEX_START_TIME)):
            st_t = self.session_sched.get("mcx_start", MCX_START_TIME) if is_mcx else self.session_sched.get("nse_start", INDEX_START_TIME)
            short_status = f"OPENS {st_t}"
        else:
            b_lo = int(round(float(low20_1m))) if pd.notna(low20_1m) else int(spot_close)
            b_hi = int(round(float(high20_1m))) if pd.notna(high20_1m) else int(spot_close)
            short_status = f"20BO {b_lo}-{b_hi}"

        return {
            "idx": idx, "is_mcx": is_mcx,
            "spot": round(spot_close, 2) if spot_close < 1000 else int(round(spot_close)),
            "fuel": int(round(fuel_5m)), "pcr": pcr, "cum_pcr_diff": cum_pcr_diff,
            "short_status": short_status,
        }

    # ==========================================================================
    # UNRESTRICTED 1-LOT EXECUTION (MCX FUTURES & INDEX ATM OPTIONS)
    # ==========================================================================
    def try_execute_mcx_fut_entry(self, idx, cfg, setup, df_1m, fut_price, atr14, now_ist):
        with self.pos_lock:
            if idx in self.active_positions or len(self.active_positions) >= MAX_ACTIVE_TRADES_TOTAL:
                return

        inst_key = cfg["spot_key"]
        lot_size = int(cfg["lot_size"])
        pnl_mult = int(cfg.get("pnl_multiplier", lot_size))
        tick_sz = cfg.get("tick_size", 0.05)
        tx_side = "BUY" if setup["side"] == "CE" else "SELL"

        ltp = float(fetch_ltp_quote([inst_key]).get(inst_key, {}).get("last_price", fut_price))
        lvl = float(setup["breakout_level"])
        micro_low = float(df_1m["low"].tail(5).min())
        micro_high = float(df_1m["high"].tail(5).max())

        if tx_side == "BUY":
            u_sl = min(lvl, micro_low) - (MICRO_BUFFER_ATR * atr14)
            sl_points = max(round_to_tick(ltp - u_sl, tick_sz), round_to_tick(ltp * 0.0012, tick_sz), tick_sz * 4)
            sl_price = round_to_tick(ltp - sl_points, tick_sz)
            t1_price = round_to_tick(ltp + (T1_RUNNER_R * sl_points), tick_sz)
            t2_price = round_to_tick(ltp + (T2_TARGET_R * sl_points), tick_sz)
        else:
            u_sl = max(lvl, micro_high) + (MICRO_BUFFER_ATR * atr14)
            sl_points = max(round_to_tick(u_sl - ltp, tick_sz), round_to_tick(ltp * 0.0012, tick_sz), tick_sz * 4)
            sl_price = round_to_tick(ltp + sl_points, tick_sz)
            t1_price = round_to_tick(ltp - (T1_RUNNER_R * sl_points), tick_sz)
            t2_price = round_to_tick(ltp - (T2_TARGET_R * sl_points), tick_sz)

        eff_lots = 1
        qty = min(lot_size, cfg["freeze_qty"])
        pnl_units = eff_lots * pnl_mult
        total_est_ch = fetch_exact_trade_charges(inst_key, ltp, ltp, pnl_units, True).get("total", 180.0)

        order_res = place_marketable_limit_order(inst_key, qty, tx_side, ltp, tick_sz)
        if order_res.get("status") == "success":
            fill = order_res["fill_price"]
            sym = self.fut_contracts.get(idx, {}).get("trading_symbol", f"{idx} FUT")
            now_ts = time.time()
            self.trade_counter = max(self.trade_counter, len(self.closed_trades)) + 1
            t_no = self.trade_counter
            entry_time_str = now_ist.strftime("%H:%M:%S")
            with self.pos_lock:
                self.active_positions[idx] = {
                    "trade_no": t_no, "type": "MCX_FUT", "instrument_key": inst_key, "strike": sym,
                    "side": setup["side"], "tx_side": tx_side, "display_side": tx_side,
                    "lots": eff_lots, "lot_size": lot_size, "qty": qty, "pnl_units": pnl_units,
                    "tick_size": tick_sz, "delta": 1.0, "entry_price": fill,
                    "spot_entry": fut_price, "spot_sl": sl_price, "r_unit": sl_points,
                    "t1_price": t1_price, "t2_price": t2_price, "runner_mode": False,
                    "est_charges": total_est_ch, "gann_level": lvl, "last_ltp": fill,
                    "sl": sl_price, "peak_gain": 0.0, "entry_ts": now_ts, "last_peak_ts": now_ts,
                    "trail_stage": None, "is_expiry_day": False,
                    "trade_mode": "20BO-RT", "target_label": "1R RUN / 2R TGT",
                    "ltp_window": deque([(now_ts, fill)]), "setup_name": setup["name"],
                    "entry_time": entry_time_str,
                }
            self.pending_setups[idx] = None
            self.subscribe_tick_instrument(inst_key)
            self.save_active_positions_state()
            self.render_live_dashboard(now_ist.strftime("%H:%M"))

    def try_execute_option_entry(self, idx, cfg, setup, df_1m, spot_close, chain, atr14, now_ist, is_today_exp):
        with self.pos_lock:
            if idx in self.active_positions or len(self.active_positions) >= MAX_ACTIVE_TRADES_TOTAL:
                return

        if not chain:
            return
        step = cfg.get("step", 50)
        tick_sz = cfg.get("tick_size", 0.05)

        opt_field = "call_options" if setup["side"] == "CE" else "put_options"
        target_strike = round(spot_close / step) * step
        chosen = next((row.get(opt_field, {}) for row in chain if row.get("strike_price") == target_strike), None)
        if not chosen:
            valid_rows = [row for row in chain if row.get(opt_field, {}).get("instrument_key")]
            if not valid_rows:
                self.pending_setups[idx] = None
                return
            nearest_row = min(valid_rows, key=lambda r: abs(float(r.get("strike_price", 0)) - spot_close))
            target_strike = nearest_row.get("strike_price", target_strike)
            chosen = nearest_row.get(opt_field, {})

        inst_key = chosen.get("instrument_key")
        if not inst_key:
            self.pending_setups[idx] = None
            return

        mkt = chosen.get("market_data", {})
        greeks = chosen.get("option_greeks", {})
        ltp = float(mkt.get("ltp", 0))
        if ltp <= 0:
            ltp = float(fetch_ltp_quote([inst_key]).get(inst_key, {}).get("last_price", 0.0))
        if ltp <= 0:
            self.pending_setups[idx] = None
            return

        lvl = float(setup["breakout_level"])
        micro_low = float(df_1m["low"].tail(5).min())
        micro_high = float(df_1m["high"].tail(5).max())
        if setup["side"] == "CE":
            spot_sl_equiv = round(min(lvl, micro_low) - (MICRO_BUFFER_ATR * atr14), 2)
            spot_risk = max(spot_close - spot_sl_equiv, 0.5 * atr14)
        else:
            spot_sl_equiv = round(max(lvl, micro_high) + (MICRO_BUFFER_ATR * atr14), 2)
            spot_risk = max(spot_sl_equiv - spot_close, 0.5 * atr14)

        raw_delta = abs(float(greeks.get("delta", 0.0)))
        live_delta = min(0.75, max(0.35, raw_delta)) if raw_delta > 0 else 0.50
        raw_opt_sl = min(max(spot_risk * live_delta, ltp * 0.10), ltp * 0.35)
        sl_points = max(round_to_tick(raw_opt_sl, tick_sz), tick_sz * 4)
        sl_price = round_to_tick(max(tick_sz, ltp - sl_points), tick_sz)
        t1_price = round_to_tick(ltp + (T1_RUNNER_R * sl_points), tick_sz)
        t2_price = round_to_tick(ltp + (T2_TARGET_R * sl_points), tick_sz)

        eff_lots = 1
        qty = min(int(cfg["lot_size"]), cfg["freeze_qty"])
        total_est_ch = fetch_exact_trade_charges(inst_key, ltp, ltp, qty, False).get("total", 65.0)

        order_res = place_marketable_limit_order(inst_key, qty, "BUY", ltp, tick_sz)
        if order_res.get("status") == "success":
            fill = order_res["fill_price"]
            now_ts = time.time()
            self.trade_counter = max(self.trade_counter, len(self.closed_trades)) + 1
            t_no = self.trade_counter
            entry_time_str = now_ist.strftime("%H:%M:%S")
            with self.pos_lock:
                self.active_positions[idx] = {
                    "trade_no": t_no, "type": "INDEX_OPT", "instrument_key": inst_key,
                    "strike": target_strike, "side": setup["side"], "tx_side": "BUY",
                    "display_side": setup["side"], "lots": eff_lots, "lot_size": int(cfg["lot_size"]),
                    "qty": qty, "pnl_units": qty, "tick_size": tick_sz,
                    "delta": live_delta, "entry_price": fill, "spot_entry": spot_close,
                    "spot_sl": spot_sl_equiv, "r_unit": sl_points,
                    "t1_price": t1_price, "t2_price": t2_price, "runner_mode": False,
                    "est_charges": total_est_ch, "gann_level": lvl, "last_ltp": fill, "sl": sl_price,
                    "peak_gain": 0.0, "entry_ts": now_ts, "last_peak_ts": now_ts,
                    "trail_stage": None, "is_expiry_day": is_today_exp,
                    "trade_mode": "20BO-RT", "target_label": "1R RUN / 2R TGT",
                    "ltp_window": deque([(now_ts, fill)]), "setup_name": f"{setup['name']}|ATM",
                    "entry_time": entry_time_str,
                }
            self.pending_setups[idx] = None
            self.subscribe_tick_instrument(inst_key)
            self.save_active_positions_state()
            self.render_live_dashboard(now_ist.strftime("%H:%M"))

    def manage_open_positions(self, now_ist: datetime):
        with self.pos_lock:
            if not self.active_positions:
                return
            pos_items = list(self.active_positions.items())

        hhmm = now_ist.strftime("%H:%M")
        nse_exit_t = self.session_sched.get("nse_exit", INDEX_EXIT_TIME)
        mcx_exit_t = self.session_sched.get("mcx_exit", MCX_EXIT_TIME)
        quotes = fetch_ltp_quote([p["instrument_key"] for _, p in pos_items]) if not self.ws_connected else {}

        for idx, pos in pos_items:
            if not self.ws_connected:
                ltp = float(quotes.get(pos["instrument_key"], {}).get("last_price", pos.get("last_ltp", pos["entry_price"])))
                pos["last_ltp"] = ltp
            else:
                ltp = pos.get("last_ltp", pos["entry_price"])

            is_short = pos["tx_side"] == "SELL"
            t2_price = float(pos.get("t2_price", 0.0))
            exit_reason = None

            if pos["type"] == "INDEX_OPT" and hhmm >= nse_exit_t:
                exit_reason = "MUHURAT_EOD_EXIT" if self.session_sched.get("is_muhurat") else "EOD_EXIT"
            elif pos["type"] == "MCX_FUT" and hhmm >= mcx_exit_t:
                exit_reason = "MCX_EOD_EXIT"
            elif t2_price > 0 and ((not is_short and ltp >= t2_price) or (is_short and ltp <= t2_price)):
                exit_reason = "TARGET_2R_HIT"
            elif not is_short and ltp <= pos["sl"]:
                exit_reason = "RUNNER_TRAIL_SL" if pos.get("runner_mode") else "STOP_LOSS_HIT"
            elif is_short and ltp >= pos["sl"]:
                exit_reason = "RUNNER_TRAIL_SL" if pos.get("runner_mode") else "STOP_LOSS_HIT"

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

        self.load_self_tuned_intelligence()

        today_clean = now_ist.strftime("%Y-%m-%d")
        self.session_sched = inspect_special_session_schedule(now_ist)
        live_nse_status = check_live_exchange_status("NSE")

        nifty_live_df = fetch_intraday_candles(INDICES["NIFTY"]["spot_key"], 5)
        nse_has_live_candles = (
            not nifty_live_df.empty
            and str(nifty_live_df["timestamp"].iloc[-1])[:10] == today_clean
        )

        if HAS_AUTO_TUNER and not nse_has_live_candles and live_nse_status not in ("OPEN", "NORMAL_OPEN", "PRE_OPEN"):
            learned_reason = get_market_closed_reason(now_ist, live_nse_status)
            if learned_reason and not self.session_sched["is_holiday"]:
                self.session_sched["is_holiday"] = True
                self.session_sched["description"] = learned_reason
                self.session_sched["nse_open"] = False
                self.session_sched["mcx_open"] = True
                self.session_sched["mcx_start"] = "17:00"

        if self.session_sched["is_holiday"] and not nse_has_live_candles:
            if HAS_AUTO_TUNER:
                record_learned_holiday(now_ist.strftime("%Y-%m-%d"))

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
            self.session_sched["mcx_start"] = MCX_START_TIME

        signal.signal(signal.SIGTERM, lambda s, f: setattr(self, "stop_requested", True))
        signal.signal(signal.SIGINT, lambda s, f: setattr(self, "stop_requested", True))

        self.refresh_live_macro_news(force=True)
        self.load_today_state_from_journal()
        self.init_contracts()
        self.load_active_positions_state()
        self.bot_status_text = "BOT LIVE"
        session_start_ts = time.time()
        self.engine_boot_ts = session_start_ts
        holiday_checked_0922 = False

        sess_banner = "1m 20-BO RETEST + 5m FUEL (IDX & MCX MINI)"

        notify(
            f"🌅 <b>V3 QUANT ENGINE • ONLINE</b>\n"
            f"────────────────────────────\n"
            f"🔑 <b>Token:</b> Active  |  ⚡ <b>Mode:</b> <code>{'PAPER' if PAPER_MODE else 'LIVE'}</code>\n"
            f"🧠 <b>Brain:</b> <code>{self.tuned_params.get('brain_version')}</code>  |  ⚙️ <b>Gear:</b> <code>{self.tuned_params.get('driver_gear')}</code>\n"
            f"🕒 <b>Session:</b> <code>{sess_banner}</code>"
        )
        self.dashboard_msg_id = None
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
                has_today_candle = (
                    not nifty_check_df.empty
                    and str(nifty_check_df["timestamp"].iloc[-1])[:10] == today_s
                )
                nse_live_now = check_live_exchange_status("NSE")
                if not has_today_candle and nse_live_now in ("CLOSED", "CLOSING", ""):
                    self.session_sched["is_holiday"] = True
                    self.session_sched["nse_open"] = False
                    self.session_sched["mcx_open"] = True
                    self.session_sched["mcx_start"] = "17:00"
                    if HAS_AUTO_TUNER:
                        record_learned_holiday(today_s)
                    notify(
                        f"🟡 <b>NSE/BSE HOLIDAY ({today_s}) • MCX EVENING ARMED</b>\n"
                        f"────────────────────────────\n"
                        f"🛢️ <i>Index trading disabled; MCX commodity trading will auto-start at 17:00 IST.</i>"
                    )

            self.refresh_live_macro_news(force=False)
            self.manage_open_positions(now_ist)

            if hhmm >= "23:35":
                self.shutdown_cleanly(now_ist, "EOD CLOSED")
                break

            if not self.session_sched["nse_open"] and hhmm < self.session_sched.get("mcx_start", "17:00"):
                self.bot_status_text = f"STANDBY (MCX OPENS {self.session_sched.get('mcx_start', '17:00')})"
            elif self.session_sched["is_muhurat"] and hhmm < self.session_sched.get("nse_start", "18:00") and hhmm < self.session_sched.get("mcx_start", "17:00"):
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
                                self.last_summaries[idx] = {
                                    "idx": idx, "is_mcx": False, "spot": int(round(float(spot_df["close"].iloc[-1]))),
                                    "fuel": 0, "pcr": 0.0, "cum_pcr_diff": 0.0,
                                    "short_status": "HOLIDAY" if not self.session_sched["nse_open"] else "CLOSED",
                                }
                        continue
                res = self.evaluate_instrument(idx, cfg, now_ist)
                if res:
                    self.last_summaries[idx] = res

            if not self.dashboard_msg_id and self.last_summaries:
                self.render_live_dashboard(hhmm)

            time.sleep(10)


if __name__ == "__main__":
    engine = IntradayV3Engine()
    engine.run()
