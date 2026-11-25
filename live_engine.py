import os
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
# 1. CONFIGURATION, SELF-TUNED PARAMS & GOOGLE SHEETS SYNC + AUTO-RESTORE
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

MIN_FUEL_SCORE = 45.0
MCX_MORNING_FUEL_SCORE = 46.0
MCX_EVENING_FUEL_SCORE = 44.0
MIN_ADX_TREND = 19.0
MAX_CHOP_INDEX = 60.0
SESSION_MAX_RUNTIME_SEC = 20700

INDEX_START_TIME = "09:15"
INDEX_END_TIME = "15:10"
INDEX_EXIT_TIME = "15:15"

MCX_START_TIME = "09:15"
MCX_END_TIME = "22:30"              # Stop late-night entries after 22:30 IST; trail open trades till 23:25
MCX_EXIT_TIME = "23:25"

GANN_ZONE_ATR_MULT = 0.40
PIVOT_BUFFER_ATR_MULT = 0.12
SETUP_EXPIRY_SEC = 900
REENTRY_COOLDOWN_SEC = 900          # 15-min cooldown per symbol after closing a trade
MAX_ACTIVE_TRADES_TOTAL = 5         # Allow up to 5 concurrent symbols (max 1 per symbol)
STARTUP_WARMUP_SEC = 90             # Wait 90s on boot before taking fresh signals
DASHBOARD_PULSE_SEC = 5.0
MACRO_REFRESH_SEC = 180

# Hybrid Multi-Trigger & Greeks Filter Constants
BREAKOUT_EXPANSION_ATR = 1.20
CRASH_FASTTRACK_ATR = 1.45
CRASH_DAY_PCT_THRESHOLD = 1.15
RETEST_TOLERANCE_ATR = 0.35
MAX_EMA20_EXTENSION_ATR = 4.20
SILVER_MAX_EMA20_EXT_ATR = 2.60     # Anti-trap guard: block Silver bottom traps when > 2.6x ATR from 20 EMA
MAX_PULLBACK_BASE_DIST_ATR = 2.50   # Prevent chasing late moves > 2.5x ATR away from Gann Support/VWAP
MAX_OPTION_SPREAD_PCT = 1.50
THETA_BURN_RATIO_MAX = 0.30

JOURNAL_FILE = "live_journal/trade_journal.csv"
ACTIVE_STATE_FILE = "live_journal/active_positions.json"
PARAMS_FILE = "live_journal/strategy_params.json"
MEMORY_FILE = "live_journal/market_memory.json"
MACRO_FILE = "live_journal/macro_context.json"
DASHBOARD_STATE_FILE = "live_journal/dashboard_state.json"

JOURNAL_FIELDNAMES = [
    "trade_no", "date", "entry_time", "exit_time", "symbol", "contract", "side",
    "qty", "entry_price", "exit_price", "points", "sl", "target", "exit_reason",
    "gross_pnl", "brokerage", "taxes_other", "charges", "pnl", "day_pnl", "setup",
]

STAGE1_BE_R = 1.5
STAGE2_LOCK_R = 2.5
STAGE3_TREND_R = 4.0
STAGE4_MOONSHOT_R = 8.0

FUEL_FINISH_MIN_R = 1.5
FUEL_FINISH_THRESHOLD = 18.0

FLAT_LTP_NORMAL_SEC = 900
FLAT_LTP_EXPIRY_SEC = 600

DAILY_PROFIT_LOCK_PCT = 1.00
DAILY_LOSS_LOCK_PCT = -0.15
MAX_CONSECUTIVE_LOSSES = 3

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

            clean_row = {
                "trade_no": t_no,
                "date": d_str,
                "entry_time": e_time,
                "exit_time": x_time,
                "symbol": sym,
                "contract": str(st.get("contract", "")).strip(),
                "side": str(st.get("side", "")).strip(),
                "qty": int(float(st.get("qty", 0) or 0)),
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
# 2. TELEGRAM UI (SINGLE PINNED MESSAGE + SILENT NIGHT MODE) & AUTO-CHARGES
# ==============================================================================
def notify(msg: str, silent: bool = False):
    clean_lines = [line.strip() for line in msg.splitlines() if line.strip()]
    log.info(f"[MIRROR] {' | '.join(clean_lines)}")

    # Do not disturb sleep between 23:36 IST and 08:30 IST
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
# 3. UPSTOX V3 API HELPERS (7-DAY EXPIRY AUTO-ROLLOVER FOR LIQUID FUTURES)
# ==============================================================================
def get_active_future_contract(idx: str) -> dict:
    exchange = "BSE" if idx == "SENSEX" else ("MCX" if INDICES[idx]["type"] == "MCX_FUT" else "NSE")
    url = "https://api.upstox.com/v2/instruments/search"
    today_date = datetime.now(IST).date()
    safe_min_expiry = (today_date + timedelta(days=7)).strftime("%Y-%m-%d")

    all_candidates = []
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
            cands = exact if exact else [it for it in items if str(it.get("instrument_type", "")).upper() == "FUT"]
            all_candidates.extend(cands)
        except Exception as e:
            log.warning(f"[{idx}] Instrument search error ({expiry_kw}): {e}")

    if not all_candidates:
        return {}

    dedup = {}
    for c in all_candidates:
        ik = str(c.get("instrument_key", ""))
        if ik:
            dedup[ik] = c
    sorted_cands = sorted(dedup.values(), key=lambda x: str(x.get("expiry", "9999-12-31")))

    # Auto-rollover to next_month if nearest contract expires within 7 days
    liquid_cands = [c for c in sorted_cands if str(c.get("expiry", "")) >= safe_min_expiry]
    top = liquid_cands[0] if liquid_cands else sorted_cands[0]

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
# 4. REGIME INTELLIGENCE, DYNAMIC GANN LEVELS, VWAP/EMA & GRADUAL FUEL SCORE
# ==============================================================================
def compute_market_regime(df: pd.DataFrame, min_adx: float, max_chop: float) -> dict:
    if df.empty or len(df) < 6:
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
    is_strong_trend = (adx >= 24.0 and chop < 54.0)
    regime = "SIDEWAYS" if is_sideways else ("HIGHWAY_TREND" if is_strong_trend else "MODERATE_TREND")
    return {
        "regime": regime,
        "adx": round(adx, 1),
        "chop": round(chop, 1),
        "is_sideways": is_sideways,
        "is_strong_trend": is_strong_trend,
    }


def compute_gann_levels(price: float, step: float) -> dict:
    # Scale root step by price magnitude so high-priced contracts (Silver ~145k, Sensex ~82k)
    # get clean ~0.8%-1.0% structural Gann spacing instead of crowded 0.25% micro-lines.
    if price >= 100000:
        root_inc = 1.50
    elif price >= 50000:
        root_inc = 1.00
    else:
        root_inc = 0.50

    root = math.sqrt(price)
    base_floor = math.floor(root / root_inc) * root_inc
    levels = [round((base_floor + k * root_inc) ** 2, 2) for k in range(-3, 5)]
    supports = [lvl for lvl in levels if lvl <= price]
    resistances = [lvl for lvl in levels if lvl > price]
    fallback_step = step if step and step > 0 else max(round(price * 0.005, 2), 1.0)
    return {
        "support": supports[-1] if supports else round(price - fallback_step, 2),
        "resistance": resistances[0] if resistances else round(price + fallback_step, 2),
        "all": levels,
    }


def detect_6_gann_patterns(df: pd.DataFrame, gann_levels: list, atr14: float) -> dict:
    if len(df) < 1:
        return {"near_gann": None, "bull_pattern": None, "bear_pattern": None}

    zone_band = atr14 * GANN_ZONE_ATR_MULT
    if len(df) < 3:
        c = df.iloc[-1]
        o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
        near_gann = next((lvl for lvl in gann_levels if min(abs(cl - lvl), abs(l - lvl), abs(h - lvl)) <= zone_band), None)
        if near_gann is None:
            return {"near_gann": None, "bull_pattern": None, "bear_pattern": None}
        r = max(h - l, 1e-6)
        b = abs(cl - o)
        bull = "OPEN_DRIVE_BULL" if (cl > o and b / r >= 0.55) else None
        bear = "OPEN_DRIVE_BEAR" if (cl < o and b / r >= 0.55) else None
        return {"near_gann": near_gann, "bull_pattern": bull, "bear_pattern": bear}

    c1, c2, c3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    o1, h1, l1, cl1 = float(c1["open"]), float(c1["high"]), float(c1["low"]), float(c1["close"])
    o2, h2, l2, cl2 = float(c2["open"]), float(c2["high"]), float(c2["low"]), float(c2["close"])
    o3, h3, l3, cl3 = float(c3["open"]), float(c3["high"]), float(c3["low"]), float(c3["close"])

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
    elif (cl3 > o3) and (b3 / r3 >= 0.65) and (cl3 > h2):
        bull_pattern = "MOMENTUM_BREAKOUT_BULL"

    bear_pattern = None
    if (cl3 < o3) and (cl2 > o2) and (cl3 <= o2) and (o3 >= cl2):
        bear_pattern = "BEARISH_ENGULFING"
    elif (cl1 > o1) and (b1 / r1 >= 0.45) and (b2 / r2 <= 0.40) and (cl3 < o3) and (cl3 < mid1):
        bear_pattern = "EVENING_STAR"
    elif (upper_wick3 >= 1.8 * max(b3, r3 * 0.1)) and (lower_wick3 <= 0.25 * r3) and ((h3 - cl3) / r3 >= 0.60):
        bear_pattern = "REVERSE_HAMMER"
    elif (cl3 < o3) and (b3 / r3 >= 0.65) and (cl3 < l2):
        bear_pattern = "MOMENTUM_BREAKDOWN_BEAR"

    return {"near_gann": near_gann, "bull_pattern": bull_pattern, "bear_pattern": bear_pattern}


def compute_fuel_with_fut_volume(spot_df: pd.DataFrame, fut_df: pd.DataFrame) -> pd.DataFrame:
    if spot_df.empty:
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

    spot_df["ema20"] = spot_df["close"].ewm(span=20, adjust=False).mean()
    spot_df["ema50"] = spot_df["close"].ewm(span=50, adjust=False).mean()
    tp = (spot_df["high"] + spot_df["low"] + spot_df["close"]) / 3.0
    day_ser = spot_df["timestamp"].dt.date
    vol_safe = spot_df["active_vol"].replace(0, 1.0)
    spot_df["vwap"] = (tp * vol_safe).groupby(day_ser).cumsum() / vol_safe.groupby(day_ser).cumsum()
    spot_df["high20"] = spot_df["high"].rolling(20, min_periods=5).max().shift(1)
    spot_df["low20"] = spot_df["low"].rolling(20, min_periods=5).min().shift(1)

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
# 6. SELF-MATURING V3 ENGINE
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

        init_cap = float(os.getenv("CAPITAL_PER_TRADE") or "25000")
        init_mcx_cap = float(os.getenv("MCX_CAPITAL_PER_TRADE") or "150000")
        self.tuned_params = {
            "brain_version": "v1.0",
            "driver_gear": 3,
            "capital_per_trade": init_cap,
            "mcx_capital_per_trade": init_mcx_cap,
            "max_daily_loss_inr": round(init_cap * 0.15, 2),
            "index_max_risk_inr": round(init_cap * 0.05, 2),
            "mcx_max_risk_inr": round(init_mcx_cap * 0.025, 2),
            "min_fuel_score": MIN_FUEL_SCORE,
            "expiry_slope_fuel_score": 32.0,
            "expiry_slope_start": "13:45",
            "expiry_slope_end": "14:45",
            "expiry_strike_offset": 1,
            "strike_offset": 0,
            "mcx_morning_fuel_score": MCX_MORNING_FUEL_SCORE,
            "mcx_evening_fuel_score": MCX_EVENING_FUEL_SCORE,
            "min_adx_trend": MIN_ADX_TREND,
            "max_chop_index": MAX_CHOP_INDEX,
            "swing_lookback_candles": 75,
            "atr_sl_buffer_mult": PIVOT_BUFFER_ATR_MULT,
            "trail_cushion_mult": 1.0,
            "scalp_target_r": 1.80,
            "no_momentum_minutes": 15,
            "morning_wait_until": "09:15",
            "news_ignition_atr_mult": 2.2,
            "news_ignition_vol_mult": 2.5,
            "min_option_delta": 0.45,
            "expiry_slope_min_delta": 0.30,
            "max_theta_decay_pct": 8.0,
            "max_iv_spike_pct": 25.0,
            "require_oi_short_covering": True,
            "bullish_pcr_min": 1.15,
            "bearish_pcr_max": 0.85,
            "symbol_overrides": {},
        }
        self.market_memory = []
        self.macro_context = {
            "threat_level": "NORMAL",
            "stance_label": "ADAPTIVE_TWO_WAY",
            "sector_verdict": "BALANCED",
            "blocked_sides": [],
            "eject_sides": [],
            "blocked_pairs": [],
            "priority_pairs": [],
            "force_scalp": False,
            "event_caution": False,
            "india_vix": 15.0,
            "summary": "Normal Market",
        }

    def get_active_capital(self) -> float:
        return float(os.getenv("CAPITAL_PER_TRADE") or self.tuned_params.get("capital_per_trade", 25000.0))

    def get_active_mcx_capital(self) -> float:
        return float(os.getenv("MCX_CAPITAL_PER_TRADE") or self.tuned_params.get("mcx_capital_per_trade", 150000.0))

    def load_self_tuned_intelligence(self):
        if os.path.isfile(PARAMS_FILE):
            try:
                with open(PARAMS_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self.tuned_params.update(loaded)
                self.tuned_params["morning_wait_until"] = "09:15"
                if float(self.tuned_params.get("min_fuel_score", 45.0)) > 48.0:
                    self.tuned_params["min_fuel_score"] = 45.0
                if float(self.tuned_params.get("mcx_evening_fuel_score", 44.0)) > 46.0:
                    self.tuned_params["mcx_evening_fuel_score"] = 44.0
                min_mcx_risk = round(self.get_active_mcx_capital() * 0.025, 2)
                if float(self.tuned_params.get("mcx_max_risk_inr", 1500.0)) < min_mcx_risk:
                    self.tuned_params["mcx_max_risk_inr"] = min_mcx_risk
                log.info(
                    f"[BRAIN LOADED] {self.tuned_params.get('brain_version')} | "
                    f"Gear={self.tuned_params.get('driver_gear')} | "
                    f"IdxFuel(5m)={self.tuned_params.get('min_fuel_score')} | "
                    f"McxFuel(5m)={self.tuned_params.get('mcx_evening_fuel_score')} | "
                    f"McxRiskCap=₹{self.tuned_params.get('mcx_max_risk_inr')}"
                )
            except Exception as e:
                log.warning(f"Failed to load strategy_params.json: {e}")
        if os.path.isfile(MEMORY_FILE):
            try:
                with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                    self.market_memory = json.load(f)
            except Exception:
                pass
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
                self.macro_context = evaluate_macro_context(notify_user=False, live_pcrs=live_pcrs)
                return
            except Exception:
                pass
        if os.path.isfile(MACRO_FILE):
            try:
                with open(MACRO_FILE, "r", encoding="utf-8") as f:
                    self.macro_context.update(json.load(f))
            except Exception:
                pass

    def consult_market_memory(self, idx: str, side: str, regime_info: dict, fuel: float) -> tuple:
        pair_key = f"{idx}:{side}".upper()
        if side.upper() in self.macro_context.get("blocked_sides", []):
            return False, f"AI_SIDE_VETO_{side}"
        if pair_key in self.macro_context.get("blocked_pairs", []):
            return False, f"AI_PAIR_VETO_{idx}_{side}"

        sym_ov = self.tuned_params.get("symbol_overrides", {}).get(idx, {})
        if sym_ov.get("cooldown") and fuel < 62.0:
            return False, "COOLDOWN_LOW_FUEL"

        similar = [
            m for m in self.market_memory
            if m.get("symbol") == idx
            and m.get("regime") == regime_info["regime"]
            and abs(float(m.get("adx", 20.0)) - regime_info["adx"]) <= 5.0
        ]
        if len(similar) >= 3:
            bad_entries = sum(1 for m in similar if m.get("outcome") in ("SIDEWAYS_TRAP", "WRONG_ENTRY_AGAINST_TREND"))
            if (bad_entries / len(similar)) >= 0.65 and fuel < 58.0:
                return False, "MEMORY_VETO_TRAP"

        if pair_key in self.macro_context.get("priority_pairs", []):
            return True, "AI_PRIORITY_HYBRID"
        return True, "ADAPTIVE_HYBRID"

    def compute_true_fractal_swing_sl(self, df: pd.DataFrame, side: str, spot_price: float, atr14: float) -> tuple:
        lookback = df.iloc[-10:] if len(df) >= 10 else df
        sl_mult = float(self.tuned_params.get("atr_sl_buffer_mult", PIVOT_BUFFER_ATR_MULT))
        buffer = sl_mult * atr14
        min_spot_risk = 0.45 * atr14
        max_spot_risk = 1.45 * atr14

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
            cushion = float(self.tuned_params.get("trail_cushion_mult", 1.0))

            with self.pos_lock:
                for idx, pos in data.items():
                    pos["ltp_window"] = deque(pos.get("ltp_window", []))
                    inst_key = pos["instrument_key"]
                    is_short = pos["tx_side"] == "SELL"
                    entry = float(pos["entry_price"])
                    tick_sz = float(pos.get("tick_size", 0.05))
                    r_unit = max(float(pos.get("r_unit", 1.0)), 1e-6)
                    entry_time_str = pos.get("entry_time", "09:15:00")
                    tf_m = INDICES.get(idx, {}).get("tf_min", 5)

                    inst_df = fetch_intraday_candles(inst_key, tf_m)
                    if not inst_df.empty:
                        inst_df["hhmmss"] = inst_df["timestamp"].dt.strftime("%H:%M:%S")
                        since_entry = inst_df[inst_df["hhmmss"] >= entry_time_str]
                        if not since_entry.empty:
                            best_fav = float(since_entry["low"].min()) if is_short else float(since_entry["high"].max())
                            gap_peak_gain = max(0.0, (entry - best_fav) if is_short else (best_fav - entry))
                            if gap_peak_gain > pos.get("peak_gain", 0.0):
                                pos["peak_gain"] = gap_peak_gain
                                pos["last_peak_ts"] = time.time()

                    q_data = live_quotes.get(inst_key, {})
                    curr_ltp = float(q_data.get("last_price", pos.get("last_ltp", entry)))
                    pos["last_ltp"] = curr_ltp

                    curr_gain = (entry - curr_ltp) if is_short else (curr_ltp - entry)
                    if curr_gain > pos.get("peak_gain", 0.0):
                        pos["peak_gain"] = curr_gain

                    peak_gain = pos.get("peak_gain", 0.0)
                    peak_r = peak_gain / r_unit

                    lock_80 = max(0.68, min(0.82, 0.80 / max(cushion, 0.85)))
                    lock_70 = max(0.58, min(0.72, 0.70 / max(cushion, 0.85)))

                    if peak_r >= STAGE4_MOONSHOT_R:
                        lock_gain = peak_gain * lock_80
                        cand_sl = round_to_tick((entry - lock_gain) if is_short else (entry + lock_gain), tick_sz)
                        if (is_short and cand_sl < pos["sl"]) or (not is_short and cand_sl > pos["sl"]):
                            pos["sl"] = cand_sl
                            pos["trail_stage"] = f"MOONSHOT {int(lock_80*100)}%"
                    elif peak_r >= STAGE3_TREND_R:
                        lock_gain = peak_gain * lock_70
                        cand_sl = round_to_tick((entry - lock_gain) if is_short else (entry + lock_gain), tick_sz)
                        if (is_short and cand_sl < pos["sl"]) or (not is_short and cand_sl > pos["sl"]):
                            pos["sl"] = cand_sl
                            pos["trail_stage"] = f"TREND {int(lock_70*100)}%"
                    elif peak_r >= STAGE2_LOCK_R:
                        cand_sl = round_to_tick((entry - 1.5 * r_unit) if is_short else (entry + 1.5 * r_unit), tick_sz)
                        if (is_short and cand_sl < pos["sl"]) or (not is_short and cand_sl > pos["sl"]):
                            pos["sl"] = cand_sl
                            pos["trail_stage"] = "LOCK 1.5R"
                    elif peak_r >= STAGE1_BE_R:
                        be_off = max(round_to_tick(entry * 0.0001, tick_sz), tick_sz * 2)
                        cand_sl = round_to_tick((entry - be_off) if is_short else (entry + be_off), tick_sz)
                        if (is_short and cand_sl < pos["sl"]) or (not is_short and cand_sl > pos["sl"]):
                            pos["sl"] = cand_sl
                            pos["trail_stage"] = "COST-TO-COST"

                    self.active_positions[idx] = pos
                    self.trade_counter = max(self.trade_counter, int(pos.get("trade_no", 0)))

                    if (not is_short and curr_ltp <= pos["sl"]) or (is_short and curr_ltp >= pos["sl"]):
                        reason = "TRAIL_SL_HIT" if pos.get("trail_stage") else "STOP_LOSS_HIT"
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
                        self.closed_trades.append({
                            "trade_no": t_no, "date": d_str, "entry_time": r.get("entry_time", ""),
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
        norm_key = instrument_key.replace(":", "|")
        target_idx, exit_reason = None, None
        cushion = float(self.tuned_params.get("trail_cushion_mult", 1.0))

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

                    if pos["side"].upper() in self.macro_context.get("eject_sides", []):
                        exit_reason = f"AI_SHOCK_EJECT_{pos['side']}"
                        target_idx = idx
                        break

                    lock_80 = max(0.68, min(0.82, 0.80 / max(cushion, 0.85)))
                    lock_70 = max(0.58, min(0.72, 0.70 / max(cushion, 0.85)))

                    if peak_r >= STAGE4_MOONSHOT_R:
                        lock_gain = pos["peak_gain"] * lock_80
                        cand_sl = round_to_tick((pos["entry_price"] - lock_gain) if is_short else (pos["entry_price"] + lock_gain), tick_sz)
                        if (is_short and cand_sl < pos["sl"]) or (not is_short and cand_sl > pos["sl"]):
                            pos["sl"] = cand_sl
                            pos["trail_stage"] = f"MOONSHOT {int(lock_80*100)}%"
                    elif peak_r >= STAGE3_TREND_R:
                        lock_gain = pos["peak_gain"] * lock_70
                        cand_sl = round_to_tick((pos["entry_price"] - lock_gain) if is_short else (pos["entry_price"] + lock_gain), tick_sz)
                        if (is_short and cand_sl < pos["sl"]) or (not is_short and cand_sl > pos["sl"]):
                            pos["sl"] = cand_sl
                            pos["trail_stage"] = f"TREND {int(lock_70*100)}%"
                    elif peak_r >= STAGE2_LOCK_R:
                        cand_sl = round_to_tick((pos["entry_price"] - 1.5 * r_unit) if is_short else (pos["entry_price"] + 1.5 * r_unit), tick_sz)
                        if (is_short and cand_sl < pos["sl"]) or (not is_short and cand_sl > pos["sl"]):
                            pos["sl"] = cand_sl
                            pos["trail_stage"] = "LOCK 1.5R"
                    elif peak_r >= STAGE1_BE_R:
                        be_off = max(round_to_tick(pos["entry_price"] * 0.0001, tick_sz), tick_sz * 2)
                        cand_sl = round_to_tick((pos["entry_price"] - be_off) if is_short else (pos["entry_price"] + be_off), tick_sz)
                        if (is_short and cand_sl < pos["sl"]) or (not is_short and cand_sl > pos["sl"]):
                            pos["sl"] = cand_sl
                            pos["trail_stage"] = "COST-TO-COST"

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

        nifty_exps = self.expiries.get("NIFTY", [])
        if nifty_exps:
            self.tuned_params["nearest_option_expiry"] = nifty_exps[0]
            os.makedirs(os.path.dirname(PARAMS_FILE), exist_ok=True)
            try:
                with open(PARAMS_FILE, "w", encoding="utf-8") as f:
                    json.dump(self.tuned_params, f, indent=2)
            except Exception:
                pass

    def get_smart_expiry(self, idx: str, now_ist: datetime) -> tuple:
        valid_exps = self.expiries.get(idx, [])
        if not valid_exps:
            return "", False
        hhmm = now_ist.strftime("%H:%M")
        nearest = valid_exps[0]
        is_today_exp = detect_expiry_for_date(now_ist, nearest) if HAS_AUTO_TUNER else (nearest == now_ist.strftime("%Y-%m-%d"))
        slope_end = str(self.tuned_params.get("expiry_slope_end", "14:45"))
        if is_today_exp and hhmm > slope_end and len(valid_exps) > 1:
            return valid_exps[1], False
        return nearest, is_today_exp

    def is_within_trade_window(self, idx: str, now_ist: datetime) -> bool:
        hhmm = now_ist.strftime("%H:%M")
        if INDICES[idx]["type"] == "MCX_FUT":
            if not self.session_sched.get("mcx_open", True):
                return False
            st = self.session_sched.get("mcx_start", MCX_START_TIME)
            en = self.session_sched.get("mcx_end", MCX_END_TIME)
            return st <= hhmm <= en
        else:
            if not self.session_sched.get("nse_open", True):
                return False
            st = self.session_sched.get("nse_start", INDEX_START_TIME)
            en = self.session_sched.get("nse_end", INDEX_END_TIME)
            return st <= hhmm <= en

    def analyze_oi_and_pcr(self, idx: str, chain: list):
        if not chain:
            return 1.0, 1.0, 0.0, False, False, 0.0, 0.0
        total_pe_oi = sum(float(r.get("put_options", {}).get("market_data", {}).get("oi", 0)) for r in chain)
        total_ce_oi = sum(float(r.get("call_options", {}).get("market_data", {}).get("oi", 0)) for r in chain)
        pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 1.0

        if self.open_pcr[idx] is None:
            self.open_pcr[idx] = pcr
        open_p = self.open_pcr[idx]
        cum_pcr_diff = round(pcr - open_p, 2)

        bull_pcr_min = float(self.tuned_params.get("bullish_pcr_min", 1.15))
        bear_pcr_max = float(self.tuned_params.get("bearish_pcr_max", 0.85))

        hist = self.pcr_history[idx]
        if not hist or hist[-1] != pcr:
            hist.append(pcr)

        short_rising = (len(hist) >= 2 and hist[-1] >= hist[-2]) or (pcr >= 1.10)
        short_falling = (len(hist) >= 2 and hist[-1] <= hist[-2]) or (pcr <= 0.90)
        cum_bullish = (cum_pcr_diff > 0) or (pcr >= bull_pcr_min)
        cum_bearish = (cum_pcr_diff < 0) or (pcr <= bear_pcr_max)

        prev_oi = self.oi_history[idx]
        oi_bull_ok, oi_bear_ok = True, True
        d_pe, d_ce = 0.0, 0.0
        if prev_oi is not None:
            d_pe = total_pe_oi - prev_oi["pe_oi"]
            d_ce = total_ce_oi - prev_oi["ce_oi"]
            oi_bull_ok = (d_pe >= d_ce) or (d_ce < 0)
            oi_bear_ok = (d_ce >= d_pe) or (d_pe < 0)

        self.oi_history[idx] = {"pe_oi": total_pe_oi, "ce_oi": total_ce_oi}
        return pcr, open_p, cum_pcr_diff, ((cum_bullish or short_rising) and oi_bull_ok), ((cum_bearish or short_falling) and oi_bear_ok), d_pe, d_ce

    def compute_live_mtm(self) -> float:
        with self.pos_lock:
            if not self.active_positions:
                return 0.0
            positions_snap = list(self.active_positions.values())

        total_mtm = 0.0
        for pos in positions_snap:
            ltp = pos.get("last_ltp", pos["entry_price"])
            is_short = pos["tx_side"] == "SELL"
            total_mtm += (pos["entry_price"] - ltp) * pos["qty"] if is_short else (ltp - pos["entry_price"]) * pos["qty"]
        return round(total_mtm, 2)

    def get_performance_summary_lines(self, now_ist: datetime, live_mtm: float) -> list:
        today_str = now_ist.strftime("%Y-%m-%d")
        curr_year, curr_week, _ = now_ist.isocalendar()
        curr_month_prefix = now_ist.strftime("%Y-%m")

        rows = []
        if os.path.isfile(JOURNAL_FILE):
            try:
                with open(JOURNAL_FILE, mode="r", encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        try:
                            d_str = _normalize_date_str(r.get("date"))
                            d_obj = datetime.strptime(d_str, "%Y-%m-%d")
                            y_iso, w_iso, _ = d_obj.isocalendar()
                            rows.append({
                                "date": d_str,
                                "year": y_iso,
                                "week": w_iso,
                                "pnl": float(r.get("pnl", 0.0)),
                            })
                        except Exception:
                            continue
            except Exception:
                pass

        def calc(sub):
            tot = len(sub)
            wins = sum(1 for x in sub if x["pnl"] > 0)
            wr = int(round((wins / tot) * 100.0)) if tot > 0 else 0
            net = round(sum(x["pnl"] for x in sub), 2)
            return tot, wr, net

        d_tot, d_wr, d_net = calc([r for r in rows if r["date"] == today_str])
        w_tot, w_wr, w_net = calc([r for r in rows if r["year"] == curr_year and r["week"] == curr_week])
        m_tot, m_wr, m_net = calc([r for r in rows if r["date"].startswith(curr_month_prefix)])
        y_tot, y_wr, y_net = calc([r for r in rows if r["year"] == curr_year])

        d_live = round(d_net + live_mtm, 2)
        w_live = round(w_net + live_mtm, 2)
        m_live = round(m_net + live_mtm, 2)
        y_live = round(y_net + live_mtm, 2)

        return [
            f"• <b>Daily:</b> <b>₹{d_live:+,.2f}</b> ({d_tot}T | {d_wr}% Win)",
            f"• <b>Weekly:</b> <b>₹{w_live:+,.2f}</b> ({w_tot}T | {w_wr}% Win)",
            f"• <b>Monthly:</b> <b>₹{m_live:+,.2f}</b> ({m_tot}T | {m_wr}% Win)",
            f"• <b>Yearly:</b> <b>₹{y_live:+,.2f}</b> ({y_tot}T | {y_wr}% Win)",
        ]

    def render_live_dashboard(self, hhmm: str):
        with self.dashboard_lock:
            now_ist = datetime.now(IST)
            live_mtm = self.compute_live_mtm()
            net_pnl = round(self.day_pnl + live_mtm, 2)
            mode_badge = "<code>PAPER</code>" if PAPER_MODE else "<code>LIVE</code>"
            gear_badge = f"<code>G{self.tuned_params.get('driver_gear', 3)}</code>"
            net_icon = "🟢" if net_pnl >= 0 else "🔴"

            self.dashboard_pulse = not self.dashboard_pulse
            if "CLOSED" in self.bot_status_text or "STOPPED" in self.bot_status_text:
                status_header = "🔴 <b>MARKET CLOSED</b>"
            elif "PAUSED" in self.bot_status_text:
                status_header = "🟡 <b>BOT PAUSED</b>"
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
                f"• Realized: <b>₹{self.day_pnl:+,.2f}</b>  |  Live MTM: <b>₹{live_mtm:+,.2f}</b>",
                f"• Trades: <b>{tot_closed}</b> ({total_wins}W / {total_losses}L)  |  Win Rate: <b>{day_wr}%</b>",
            ]

            with self.pos_lock:
                active_snap = sorted(self.active_positions.items(), key=lambda item: item[1]["trade_no"])

            if active_snap:
                lines.append(f"\n🔥 <b>ACTIVE POSITIONS ({len(active_snap)})</b>")
                for p_idx, pos in active_snap:
                    ltp = pos.get("last_ltp", pos["entry_price"])
                    is_short = pos["tx_side"] == "SELL"
                    est_charges = float(pos.get("est_charges", 180.0 if pos["type"] == "MCX_FUT" else 65.0))
                    pnl = round(((pos["entry_price"] - ltp) * pos["qty"] if is_short else (ltp - pos["entry_price"]) * pos["qty"]) - est_charges, 2)
                    sl_gross = (pos["entry_price"] - pos["sl"]) * pos["qty"] if is_short else (pos["sl"] - pos["entry_price"]) * pos["qty"]
                    sl_net_pnl = round(sl_gross - est_charges, 0)
                    t_mode = pos.get("trade_mode", "HYBRID")
                    tgt_txt = pos.get("target_label", "SMART HYBRID ∞")
                    trail_tag = f"🛡️ <code>{pos['trail_stage']}</code>" if pos.get("trail_stage") else f"<code>{t_mode}</code>"
                    p_icon = "🟢" if pnl >= 0 else "🔴"
                    sl_label = f"Locked <b>₹{sl_net_pnl:+,.0f}</b>" if sl_net_pnl >= 0 else f"Max Risk <b>₹{abs(sl_net_pnl):,.0f}</b>"
                    lines.extend([
                        f"• <b>#{pos['trade_no']} {p_idx}</b> <code>{pos['strike']} {pos['display_side']}</code>  |  {trail_tag}",
                        f"  In: <b>₹{pos['entry_price']:.2f}</b> (<code>{pos['entry_time'][:5]}</code>) → LTP: <b>₹{ltp:.2f}</b>",
                        f"  SL: <code>₹{pos['sl']:.2f}</code> ({sl_label})  |  Target: <code>{tgt_txt}</code>",
                        f"  Live Net P&amp;L: {p_icon} <b>₹{pnl:+,.2f}</b>",
                    ])

            lines.append("\n📒 <b>TODAY'S CLOSED TRADES</b>")
            if self.closed_trades:
                for t in sorted(self.closed_trades, key=lambda x: x["trade_no"]):
                    icon = "✅" if t["pnl"] >= 0 else "❌"
                    lines.append(
                        f"• <b>#{t['trade_no']} {t['symbol']}</b> <code>{t['contract']} {t['side']}</code>  {icon} <b>₹{t['pnl']:+,.2f}</b>\n"
                        f"  <code>{t['entry_time'][:5]}→{t['exit_time'][:5]}</code>  |  ₹{t['entry_price']:.2f}→₹{t['exit_price']:.2f}  |  <i>{t['exit_reason']}</i>"
                    )
            elif not active_snap:
                lines.append("<i>No closed trades yet today.</i>")

            if self.last_summaries:
                ai_stance = self.macro_context.get("stance_label", "ADAPTIVE_TWO_WAY")
                lines.append(f"\n📡 <b>INDICES LIVE (5m)</b>  |  🧠 <code>{ai_stance}</code>")
                for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"):
                    s = self.last_summaries.get(idx)
                    if s:
                        lines.append(f"• <b>{s['idx']}:</b> <code>{s['spot']}</code> | PCR <b>{s['pcr']:.2f}</b> | Fuel <b>{s['fuel']}</b> | <code>{s['short_status']}</code>")

                lines.append("\n🛢️ <b>MCX LIVE (5m)</b>")
                for idx in ("CRUDEOIL", "NATURALGAS", "GOLD", "SILVER", "COPPER"):
                    s = self.last_summaries.get(idx)
                    if s:
                        lines.append(f"• <b>{s['idx']}:</b> <code>{s['spot']}</code> | Fuel <b>{s['fuel']}</b> | <code>{s['short_status']}</code>")

            lines.append("\n📊 <b>PERFORMANCE SUMMARY (NET ₹)</b>")
            lines.extend(self.get_performance_summary_lines(now_ist, live_mtm))

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
        df_curr = self.latest_dfs.get(idx)
        if df_curr is not None and not df_curr.empty:
            self.last_exit_candle_ts[idx] = str(df_curr["timestamp"].iloc[-1])

        self.unsubscribe_tick_instrument(pos["instrument_key"])
        self.save_active_positions_state()

        is_short = pos["tx_side"] == "SELL"
        is_mcx = pos["type"] == "MCX_FUT"
        close_side = "BUY" if is_short else "SELL"
        place_marketable_limit_order(pos["instrument_key"], pos["qty"], close_side, ltp, pos.get("tick_size", 0.05))

        pts = round((pos["entry_price"] - ltp) if is_short else (ltp - pos["entry_price"]), 2)
        gross_pnl = round(pts * pos["qty"], 2)

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
            "points": pts, "sl": pos["sl"], "target": 0.0, "exit_reason": exit_reason,
            "gross_pnl": gross_pnl, "brokerage": ch_info["brokerage"],
            "taxes_other": ch_info["taxes_other"], "charges": ch_info["total"],
            "pnl": net_pnl, "day_pnl": self.day_pnl, "setup": pos["setup_name"],
        }
        self.closed_trades.append(journal_entry)
        record_trade_journal(journal_entry)
        self.render_live_dashboard(now_ist.strftime("%H:%M"))

    def evaluate_active_candle_exits(
        self, idx: str, df: pd.DataFrame, spot_close: float, price_change: float,
        fuel: float, atr14: float, now_ist: datetime, gann_sup: float, gann_res: float,
        d_pe: float, d_ce: float
    ):
        with self.pos_lock:
            pos = self.active_positions.get(idx)
        if not pos or len(df) < 3:
            return

        is_short = pos["tx_side"] == "SELL"
        is_mcx = pos["type"] == "MCX_FUT"
        entry = pos["entry_price"]
        ltp = pos.get("last_ltp", entry)
        tick_sz = pos.get("tick_size", 0.05)
        eff_delta = pos.get("delta", 0.50)
        r_unit = max(pos.get("r_unit", 1.0), 1e-6)
        cushion = float(self.tuned_params.get("trail_cushion_mult", 1.0))

        spot_sl = float(pos.get("spot_sl", 0.0))
        if spot_sl > 0:
            if pos["side"] == "CE" and spot_close <= spot_sl:
                self.close_position_now(idx, ltp, "UNDERLYING_SL_HIT", now_ist)
                return
            if pos["side"] == "PE" and spot_close >= spot_sl:
                self.close_position_now(idx, ltp, "UNDERLYING_SL_HIT", now_ist)
                return

        curr_gain = (entry - ltp) if is_short else (ltp - entry)
        peak_gain = max(pos.get("peak_gain", 0.0), curr_gain)
        if peak_gain > pos.get("peak_gain", 0.0):
            pos["peak_gain"] = peak_gain
            pos["last_peak_ts"] = time.time()

        peak_r = peak_gain / r_unit
        curr_r = curr_gain / r_unit
        curr_vwap = float(df.iloc[-1].get("vwap", spot_close))

        scalp_gate_r = float(self.tuned_params.get("scalp_target_r", 1.80))
        if curr_r >= scalp_gate_r:
            c_curr = df.iloc[-1]
            c_range = max(float(c_curr["high"]) - float(c_curr["low"]), 1e-6)
            upper_wick = float(c_curr["high"]) - max(float(c_curr["open"]), float(c_curr["close"]))
            lower_wick = min(float(c_curr["open"]), float(c_curr["close"])) - float(c_curr["low"])

            if (pos["side"] == "CE" and spot_close < curr_vwap and price_change < 0) or (
                pos["side"] == "PE" and spot_close > curr_vwap and price_change > 0
            ):
                self.close_position_now(idx, ltp, "VWAP_REVERSAL_EXIT", now_ist)
                return

            if pos["side"] == "CE" and abs(spot_close - gann_res) <= (atr14 * 0.35) and (upper_wick / c_range) >= 0.45:
                self.close_position_now(idx, ltp, "GANN_RES_PROFIT_BOOK", now_ist)
                return
            if pos["side"] == "PE" and abs(spot_close - gann_sup) <= (atr14 * 0.35) and (lower_wick / c_range) >= 0.45:
                self.close_position_now(idx, ltp, "GANN_SUP_PROFIT_BOOK", now_ist)
                return

            if not is_mcx:
                hist = list(self.pcr_history.get(idx, []))
                pcr_turned_bull = len(hist) >= 3 and hist[-1] > hist[-2] > hist[-3]
                pcr_turned_bear = len(hist) >= 3 and hist[-1] < hist[-2] < hist[-3]

                if pos["side"] == "PE" and ((d_pe < 0 and d_ce > 0 and price_change > 0) or (pcr_turned_bull and price_change > 0)):
                    self.close_position_now(idx, ltp, "OI_PCR_REVERSAL_EXIT", now_ist)
                    return
                if pos["side"] == "CE" and ((d_ce < 0 and d_pe < 0 and price_change < 0) or (pcr_turned_bear and price_change < 0)):
                    self.close_position_now(idx, ltp, "OI_PCR_REVERSAL_EXIT", now_ist)
                    return

            opposing_tick = (price_change > 0) if (pos["side"] == "PE") else (price_change < 0)
            if fuel < 32.0 and opposing_tick:
                self.close_position_now(idx, ltp, "PEAK_MOMENTUM_LOCK", now_ist)
                return

        if len(df) >= 4:
            recent_3 = df.iloc[-4:-1]
            swing_low = float(recent_3["low"].min())
            swing_high = float(recent_3["high"].max())

            new_sl = pos["sl"]
            trail_label = None

            if peak_r >= STAGE2_LOCK_R:
                floor_sl = round_to_tick((entry - 1.5 * r_unit) if is_short else (entry + 1.5 * r_unit), tick_sz)
                buf = float(self.tuned_params.get("atr_sl_buffer_mult", PIVOT_BUFFER_ATR_MULT)) * atr14 * cushion
                if is_mcx:
                    swing_sl = round_to_tick((swing_high + buf) if is_short else (swing_low - buf), tick_sz)
                    candidate_sl = min(floor_sl, swing_sl) if is_short else max(floor_sl, swing_sl)
                else:
                    spot_entry = pos.get("spot_entry", spot_close)
                    delta_spot = (swing_low - buf - spot_entry) if pos["side"] == "CE" else (spot_entry - (swing_high + buf))
                    candidate_sl = max(floor_sl, round_to_tick(entry + delta_spot * eff_delta, tick_sz))

                min_gap = max(atr14 * 0.15, tick_sz * 2) if is_mcx else max(tick_sz * 4, 0.5)
                if (is_short and candidate_sl < new_sl and candidate_sl > ltp + min_gap) or (not is_short and candidate_sl > new_sl and candidate_sl < ltp - min_gap):
                    new_sl = candidate_sl
                    trail_label = "SWING + 1.5R LOCK"

            if trail_label and new_sl != pos["sl"]:
                with self.pos_lock:
                    if idx in self.active_positions:
                        self.active_positions[idx]["sl"] = new_sl
                        self.active_positions[idx]["trail_stage"] = trail_label
                self.save_active_positions_state()

        low_fuel_streak = (pos.get("low_fuel_streak", 0) + 1) if fuel < 20.0 else 0
        pos["low_fuel_streak"] = low_fuel_streak
        opposing_move = (price_change > 0) if (pos["side"] == "PE") else (price_change < 0)
        if curr_r >= FUEL_FINISH_MIN_R and (fuel < FUEL_FINISH_THRESHOLD and opposing_move and low_fuel_streak >= 2):
            self.close_position_now(idx, ltp, "FUEL_EXHAUSTED", now_ist)
            return

        now_ts = time.time()
        ltp_hist = pos.setdefault("ltp_window", deque())
        ltp_hist.append((now_ts, ltp))
        brain_no_mom_sec = int(self.tuned_params.get("no_momentum_minutes", 15)) * 60
        flat_limit_sec = min(FLAT_LTP_EXPIRY_SEC, brain_no_mom_sec) if pos.get("is_expiry_day", False) else brain_no_mom_sec
        while ltp_hist and (now_ts - ltp_hist[0][0]) > flat_limit_sec:
            ltp_hist.popleft()

        window_span = now_ts - ltp_hist[0][0] if ltp_hist else 0.0
        if window_span >= (flat_limit_sec - 20):
            prices_in_win = [p for _, p in ltp_hist]
            win_range = max(prices_in_win) - min(prices_in_win)
            flat_threshold = max(r_unit * 0.50, entry * (0.002 if is_mcx else 0.03))
            if win_range <= flat_threshold and curr_r < 0.30:
                self.close_position_now(idx, ltp, "NO_MOMENTUM_EXIT", now_ist)

    def evaluate_instrument(self, idx: str, cfg: dict, now_ist: datetime):
        is_mcx = cfg["type"] == "MCX_FUT"
        tf_min = int(cfg.get("tf_min", 5))
        if not cfg.get("spot_key"):
            return None

        spot_df = fetch_intraday_candles(cfg["spot_key"], tf_min)
        if spot_df.empty or len(spot_df) < 1:
            return None

        fut_key = self.fut_contracts.get(idx, {}).get("instrument_key", "")
        fut_df = spot_df if is_mcx else fetch_intraday_candles(fut_key, tf_min)
        df = compute_fuel_with_fut_volume(spot_df, fut_df)
        self.latest_dfs[idx] = df

        curr = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else curr
        curr_candle_ts = str(curr["timestamp"])
        spot_close = float(curr["close"])
        prev_close = float(prev["close"]) if len(df) >= 2 else float(curr["open"])
        price_change = round(spot_close - prev_close, 2)
        atr14 = max(float(curr["atr14"]), cfg.get("tick_size", 0.05) * 2)
        curr_tr = float(curr.get("tr", atr14))
        fuel = float(curr["fuel"])
        vwap = float(curr.get("vwap", spot_close))
        ema20 = float(curr.get("ema20", spot_close))
        ema50 = float(curr.get("ema50", spot_close))
        high20 = curr.get("high20")
        low20 = curr.get("low20")

        today_s = now_ist.strftime("%Y-%m-%d")
        today_df = df[df["timestamp"].dt.strftime("%Y-%m-%d") == today_s]
        orb_bars = 3 if tf_min == 5 else 1
        orb_high = float(today_df.iloc[:orb_bars]["high"].max()) if len(today_df) >= orb_bars else None
        orb_low = float(today_df.iloc[:orb_bars]["low"].min()) if len(today_df) >= orb_bars else None
        day_open = float(today_df.iloc[0]["open"]) if not today_df.empty else spot_close
        day_chg_pct = ((spot_close - day_open) / max(day_open, 1e-6)) * 100.0

        regime_info = compute_market_regime(
            df,
            min_adx=float(self.tuned_params.get("min_adx_trend", MIN_ADX_TREND)),
            max_chop=float(self.tuned_params.get("max_chop_index", MAX_CHOP_INDEX)),
        )

        gann = compute_gann_levels(spot_close, cfg.get("step", 0))
        sup, res, gann_levels = gann["support"], gann["resistance"], gann["all"]

        chain, active_expiry, is_today_exp = [], "", False
        d_pe, d_ce = 0.0, 0.0
        if is_mcx:
            oi_change = float(curr["oi"]) - float(prev["oi"])
            oi_pcr_bull_ok, oi_pcr_bear_ok = (price_change > 0), (price_change < 0)
            pcr, open_p, cum_pcr_diff = 0.0, 0.0, round(oi_change, 0)
        else:
            active_expiry, is_today_exp = self.get_smart_expiry(idx, now_ist)
            chain = fetch_option_chain(cfg["spot_key"], active_expiry)
            pcr, open_p, cum_pcr_diff, oi_pcr_bull_ok, oi_pcr_bear_ok, d_pe, d_ce = self.analyze_oi_and_pcr(idx, chain)

        with self.pos_lock:
            in_trade = idx in self.active_positions

        if in_trade:
            self.pending_setups[idx] = None
            self.evaluate_active_candle_exits(idx, df, spot_close, price_change, fuel, atr14, now_ist, sup, res, d_pe, d_ce)

            with self.pos_lock:
                pos = self.active_positions.get(idx)

            if pos:
                ltp_now = pos.get("last_ltp", spot_close if is_mcx else pos["entry_price"])
                is_short = pos["tx_side"] == "SELL"
                est_ch = float(pos.get("est_charges", 180.0 if is_mcx else 65.0))
                pos_mtm = ((pos["entry_price"] - ltp_now) * pos["qty"] if is_short else (ltp_now - pos["entry_price"]) * pos["qty"]) - est_ch
                return {
                    "idx": idx, "is_mcx": is_mcx,
                    "spot": round(spot_close, 2) if spot_close < 1000 else int(round(spot_close)),
                    "fuel": int(round(fuel)), "pcr": pcr, "cum_pcr_diff": cum_pcr_diff,
                    "short_status": f"ACTIVE #{pos['trade_no']} (₹{pos_mtm:+.0f})",
                }
            return {
                "idx": idx, "is_mcx": is_mcx,
                "spot": round(spot_close, 2) if spot_close < 1000 else int(round(spot_close)),
                "fuel": int(round(fuel)), "pcr": pcr, "cum_pcr_diff": cum_pcr_diff,
                "short_status": "COOLDOWN (15m)",
            }

        uptime_sec = time.time() - self.engine_boot_ts
        if uptime_sec < STARTUP_WARMUP_SEC:
            rem_w = max(1, int(STARTUP_WARMUP_SEC - uptime_sec))
            return {
                "idx": idx, "is_mcx": is_mcx,
                "spot": round(spot_close, 2) if spot_close < 1000 else int(round(spot_close)),
                "fuel": int(round(fuel)), "pcr": pcr, "cum_pcr_diff": cum_pcr_diff,
                "short_status": f"WARMUP ({rem_w}s)",
            }

        sec_since_exit = time.time() - self.last_exit_ts.get(idx, 0.0)
        same_candle_as_exit = (curr_candle_ts == self.last_exit_candle_ts.get(idx, ""))
        if sec_since_exit < REENTRY_COOLDOWN_SEC or same_candle_as_exit:
            rem_min = max(1, int(math.ceil((REENTRY_COOLDOWN_SEC - sec_since_exit) / 60.0)))
            self.pending_setups[idx] = None
            return {
                "idx": idx, "is_mcx": is_mcx,
                "spot": round(spot_close, 2) if spot_close < 1000 else int(round(spot_close)),
                "fuel": int(round(fuel)), "pcr": pcr, "cum_pcr_diff": cum_pcr_diff,
                "short_status": f"COOLDOWN ({rem_min}m)",
            }

        with self.pos_lock:
            total_open_trades = len(self.active_positions)
        if total_open_trades >= MAX_ACTIVE_TRADES_TOTAL:
            return {
                "idx": idx, "is_mcx": is_mcx,
                "spot": round(spot_close, 2) if spot_close < 1000 else int(round(spot_close)),
                "fuel": int(round(fuel)), "pcr": pcr, "cum_pcr_diff": cum_pcr_diff,
                "short_status": f"Gann {int(sup)}-{int(res)}",
            }

        hhmm_now = now_ist.strftime("%H:%M")
        slope_start = str(self.tuned_params.get("expiry_slope_start", "13:45"))
        slope_end = str(self.tuned_params.get("expiry_slope_end", "14:45"))
        in_expiry_slope = (not is_mcx) and is_today_exp and (slope_start <= hhmm_now <= slope_end)

        trade_mode = "HYBRID"

        pat_info = detect_6_gann_patterns(df, gann_levels, atr14)
        near_gann, bull_pattern, bear_pattern = pat_info["near_gann"], pat_info["bull_pattern"], pat_info["bear_pattern"]

        lookback_n = int(self.tuned_params.get("swing_lookback_candles", 75))
        box_df = df.iloc[-(lookback_n + 1):-1] if len(df) > lookback_n else df.iloc[:-1]
        broke_box_up = (not box_df.empty) and (spot_close > float(box_df["high"].max()))
        broke_box_dn = (not box_df.empty) and (spot_close < float(box_df["low"].min()))

        pending = self.pending_setups.get(idx)
        if pending:
            if (now_ist - pending["armed_at"]).total_seconds() > SETUP_EXPIRY_SEC:
                self.pending_setups[idx] = None
                pending = None

        # VWAP + 20-EMA Trend Alignment & Silver Anti-Trap Extension Guard
        vwap_ema_bull = (spot_close > vwap) and (spot_close > ema20)
        vwap_ema_bear = (spot_close < vwap) and (spot_close < ema20)
        dist_from_ema20 = abs(spot_close - ema20)
        max_ext_atr = SILVER_MAX_EMA20_EXT_ATR if idx == "SILVER" else MAX_EMA20_EXTENSION_ATR
        not_overextended = dist_from_ema20 <= (max_ext_atr * atr14)

        # Gann Support / VWAP Proximity Guard (<= 2.5 x ATR from structural base)
        near_bull_base = min(abs(spot_close - sup), abs(spot_close - vwap)) <= (MAX_PULLBACK_BASE_DIST_ATR * atr14)
        near_bear_base = min(abs(res - spot_close), abs(vwap - spot_close)) <= (MAX_PULLBACK_BASE_DIST_ATR * atr14)

        new_signal = None
        immediate_fasttrack = False

        # Trigger 1: Fresh Crash / Rally Fast-Track
        if len(today_df) > orb_bars and not_overextended:
            fresh_orb_dn = (orb_low is not None) and (spot_close < orb_low) and (prev_close >= orb_low - 0.15 * atr14)
            fresh_orb_up = (orb_high is not None) and (spot_close > orb_high) and (prev_close <= orb_high + 0.15 * atr14)

            if (
                fresh_orb_dn
                and vwap_ema_bear
                and ema20 <= ema50
                and (day_chg_pct <= -CRASH_DAY_PCT_THRESHOLD or curr_tr >= CRASH_FASTTRACK_ATR * atr14)
                and oi_pcr_bear_ok
            ):
                side_label = "SELL" if is_mcx else "PE"
                new_signal = {
                    "side": "PE", "display_side": side_label,
                    "name": f"CRASH_RALLY_FASTTRACK_{side_label}",
                    "gann_level": round(orb_low, 2), "trigger": round(spot_close, 2), "armed_at": now_ist,
                    "is_retest": False,
                }
                immediate_fasttrack = True
            elif (
                fresh_orb_up
                and vwap_ema_bull
                and ema20 >= ema50
                and (day_chg_pct >= CRASH_DAY_PCT_THRESHOLD or curr_tr >= CRASH_FASTTRACK_ATR * atr14)
                and oi_pcr_bull_ok
            ):
                side_label = "BUY" if is_mcx else "CE"
                new_signal = {
                    "side": "CE", "display_side": side_label,
                    "name": f"CRASH_RALLY_FASTTRACK_{side_label}",
                    "gann_level": round(orb_high, 2), "trigger": round(spot_close, 2), "armed_at": now_ist,
                    "is_retest": False,
                }
                immediate_fasttrack = True

        # Trigger 2: 20-EMA / VWAP Pullback Touch & Rejection (Only within <= 2.5 x ATR of Gann/VWAP base)
        if new_signal is None and not pending:
            touched_ema20_or_vwap_up = (
                float(curr["high"]) >= (ema20 - 0.35 * atr14)
                or float(prev["high"]) >= (ema20 - 0.25 * atr14)
                or float(curr["high"]) >= (vwap - 0.35 * atr14)
            )
            touched_ema20_or_vwap_dn = (
                float(curr["low"]) <= (ema20 + 0.35 * atr14)
                or float(prev["low"]) <= (ema20 + 0.25 * atr14)
                or float(curr["low"]) <= (vwap + 0.35 * atr14)
            )

            if (
                vwap_ema_bear
                and ema20 <= ema50
                and near_bear_base
                and touched_ema20_or_vwap_up
                and float(curr["close"]) < float(curr["open"])
                and price_change < 0
                and oi_pcr_bear_ok
            ):
                side_label = "SELL" if is_mcx else "PE"
                new_signal = {
                    "side": "PE", "display_side": side_label,
                    "name": f"EMA20_VWAP_PULLBACK_{side_label}",
                    "gann_level": round(ema20, 2), "trigger": round(spot_close, 2), "armed_at": now_ist,
                    "is_retest": False,
                }
                immediate_fasttrack = True
            elif (
                vwap_ema_bull
                and ema20 >= ema50
                and near_bull_base
                and touched_ema20_or_vwap_dn
                and float(curr["close"]) > float(curr["open"])
                and price_change > 0
                and oi_pcr_bull_ok
            ):
                side_label = "BUY" if is_mcx else "CE"
                new_signal = {
                    "side": "CE", "display_side": side_label,
                    "name": f"EMA20_VWAP_PULLBACK_{side_label}",
                    "gann_level": round(ema20, 2), "trigger": round(spot_close, 2), "armed_at": now_ist,
                    "is_retest": False,
                }
                immediate_fasttrack = True

        # Trigger 3: 20-Bar Breakout with TR >= 1.2x ATR (Blocked on Silver if > 2.6x ATR from 20 EMA)
        if new_signal is None and not pending and not_overextended:
            if pd.notna(high20) and spot_close > float(high20) and curr_tr >= BREAKOUT_EXPANSION_ATR * atr14 and vwap_ema_bull and ema20 > ema50 and oi_pcr_bull_ok:
                side_label = "BUY" if is_mcx else "CE"
                new_signal = {
                    "side": "CE", "display_side": side_label,
                    "name": f"20BAR_BREAKOUT_RETEST_{side_label}",
                    "gann_level": round(float(high20), 2), "trigger": round(spot_close, 2), "armed_at": now_ist,
                    "is_retest": True, "retest_level": float(high20),
                }
            elif pd.notna(low20) and spot_close < float(low20) and curr_tr >= BREAKOUT_EXPANSION_ATR * atr14 and vwap_ema_bear and ema20 < ema50 and oi_pcr_bear_ok:
                side_label = "SELL" if is_mcx else "PE"
                new_signal = {
                    "side": "PE", "display_side": side_label,
                    "name": f"20BAR_BREAKOUT_RETEST_{side_label}",
                    "gann_level": round(float(low20), 2), "trigger": round(spot_close, 2), "armed_at": now_ist,
                    "is_retest": True, "retest_level": float(low20),
                }

        # Trigger 4: Gann Level + 6 Chart Patterns + VWAP/EMA Confirmation
        if new_signal is None and (idx != "SILVER" or not_overextended):
            if near_gann is not None and bull_pattern is not None and price_change > 0 and vwap_ema_bull and oi_pcr_bull_ok:
                side_label = "BUY" if is_mcx else "CE"
                new_signal = {
                    "side": "CE", "display_side": side_label,
                    "name": f"GANN_REVERSAL_ENTRY|{int(near_gann)}_{bull_pattern}_{side_label}",
                    "gann_level": near_gann, "trigger": round(spot_close, 2), "armed_at": now_ist,
                    "is_retest": False,
                }
            elif near_gann is not None and bear_pattern is not None and price_change < 0 and vwap_ema_bear and oi_pcr_bear_ok:
                side_label = "SELL" if is_mcx else "PE"
                new_signal = {
                    "side": "PE", "display_side": side_label,
                    "name": f"GANN_REVERSAL_ENTRY|{int(near_gann)}_{bear_pattern}_{side_label}",
                    "gann_level": near_gann, "trigger": round(spot_close, 2), "armed_at": now_ist,
                    "is_retest": False,
                }
            # Trigger 5: Expiry Slope 75-Candle Breakout
            elif in_expiry_slope and (broke_box_up or broke_box_dn):
                if broke_box_up and price_change > 0 and spot_close > vwap and oi_pcr_bull_ok:
                    new_signal = {
                        "side": "CE", "display_side": "CE",
                        "name": "EXPIRY_SLOPE_BREAKOUT_CE",
                        "gann_level": res, "trigger": round(spot_close, 2), "armed_at": now_ist,
                        "is_retest": False,
                    }
                elif broke_box_dn and price_change < 0 and spot_close < vwap and oi_pcr_bear_ok:
                    new_signal = {
                        "side": "PE", "display_side": "PE",
                        "name": "EXPIRY_SLOPE_BREAKDOWN_PE",
                        "gann_level": sup, "trigger": round(spot_close, 2), "armed_at": now_ist,
                        "is_retest": False,
                    }

        if new_signal and (not pending or immediate_fasttrack):
            self.pending_setups[idx] = new_signal
            pending = new_signal

        if pending and idx not in self.active_positions and self.is_within_trade_window(idx, now_ist):
            sym_ov = self.tuned_params.get("symbol_overrides", {}).get(idx, {})
            in_open_window = ("09:15" <= hhmm_now <= "09:45") or ("17:00" <= hhmm_now <= "17:35")
            if in_expiry_slope:
                req_fuel = float(self.tuned_params.get("expiry_slope_fuel_score", 32.0))
            elif in_open_window:
                req_fuel = 40.0
            elif is_mcx:
                morn_mcx = float(self.tuned_params.get("mcx_morning_fuel_score", MCX_MORNING_FUEL_SCORE))
                eve_mcx = float(self.tuned_params.get("mcx_evening_fuel_score", MCX_EVENING_FUEL_SCORE))
                req_fuel = eve_mcx if hhmm_now >= "15:30" else morn_mcx
            else:
                req_fuel = float(sym_ov.get("min_fuel", self.tuned_params.get("min_fuel_score", MIN_FUEL_SCORE)))

            if (regime_info["is_sideways"] or self.macro_context.get("event_caution", False)) and not in_expiry_slope and not immediate_fasttrack:
                req_fuel += 3.0

            if pending.get("is_retest"):
                rlvl = float(pending["retest_level"])
                tol = RETEST_TOLERANCE_ATR * atr14
                if pending["side"] == "CE":
                    dir_confirmed = (float(curr["low"]) <= rlvl + tol) and (spot_close > rlvl) and (spot_close > float(prev["high"])) and vwap_ema_bull and oi_pcr_bull_ok
                else:
                    dir_confirmed = (float(curr["high"]) >= rlvl - tol) and (spot_close < rlvl) and (spot_close < float(prev["low"])) and vwap_ema_bear and oi_pcr_bear_ok
            elif immediate_fasttrack:
                dir_confirmed = True
            else:
                dir_confirmed = (
                    (pending["side"] == "CE" and price_change > 0 and vwap_ema_bull and oi_pcr_bull_ok and spot_close >= pending["trigger"])
                    or (pending["side"] == "PE" and price_change < 0 and vwap_ema_bear and oi_pcr_bear_ok and spot_close <= pending["trigger"])
                )

            if dir_confirmed and fuel >= req_fuel:
                mem_ok, mem_tag = self.consult_market_memory(idx, pending["side"], regime_info, fuel)
                if not mem_ok:
                    self.pending_setups[idx] = None
                elif is_mcx:
                    self.try_execute_mcx_fut_entry(idx, cfg, pending, df, spot_close, atr14, now_ist, mem_tag, trade_mode)
                else:
                    self.try_execute_option_entry(idx, cfg, pending, df, spot_close, chain, atr14, now_ist, is_today_exp, mem_tag, trade_mode, in_expiry_slope)

        slope_tag = " [SLOPE]" if in_expiry_slope else ""
        if pending:
            r_tag = " RETEST" if pending.get("is_retest") else ""
            short_status = f"ARMED {pending['display_side']}{r_tag}{slope_tag}"
        elif not self.is_within_trade_window(idx, now_ist) and hhmm_now < (self.session_sched.get("mcx_start", MCX_START_TIME) if is_mcx else self.session_sched.get("nse_start", INDEX_START_TIME)):
            st_t = self.session_sched.get("mcx_start", MCX_START_TIME) if is_mcx else self.session_sched.get("nse_start", INDEX_START_TIME)
            short_status = f"OPENS {st_t}"
        else:
            short_status = f"Gann {int(sup)}-{int(res)}{slope_tag}"

        return {
            "idx": idx, "is_mcx": is_mcx,
            "spot": round(spot_close, 2) if spot_close < 1000 else int(round(spot_close)),
            "fuel": int(round(fuel)), "pcr": pcr, "cum_pcr_diff": cum_pcr_diff,
            "short_status": short_status,
        }

    def try_execute_mcx_fut_entry(self, idx, cfg, setup, df, fut_price, atr14, now_ist, mem_tag, trade_mode):
        with self.pos_lock:
            if idx in self.active_positions or len(self.active_positions) >= MAX_ACTIVE_TRADES_TOTAL:
                return

        inst_key = cfg["spot_key"]
        lot_size = cfg["lot_size"]
        tick_sz = cfg.get("tick_size", 0.05)
        tx_side = "BUY" if setup["side"] == "CE" else "SELL"

        ltp = float(fetch_ltp_quote([inst_key]).get(inst_key, {}).get("last_price", fut_price))
        spot_risk, _ = self.compute_true_fractal_swing_sl(df, setup["side"], ltp, atr14)
        min_mcx_sl = round_to_tick(ltp * 0.0012, tick_sz)
        sl_points = max(round_to_tick(spot_risk, tick_sz), min_mcx_sl, tick_sz * 4)

        mcx_cap = self.get_active_mcx_capital()
        max_risk_cap = float(self.tuned_params.get("mcx_max_risk_inr", round(mcx_cap * 0.025, 2)))
        est_ch = fetch_exact_trade_charges(inst_key, ltp, ltp, lot_size, True).get("total", 180.0)
        risk_1_lot = (sl_points * lot_size) + est_ch
        if risk_1_lot > max_risk_cap:
            self.pending_setups[idx] = None
            return

        req_margin_1lot = fetch_required_margin(inst_key, lot_size, tx_side, "I")
        margin_lots = max(1, int(mcx_cap // req_margin_1lot)) if req_margin_1lot > 0 else 1
        risk_lots = max(1, int(max_risk_cap // max(risk_1_lot, 1.0)))
        lots = min(margin_lots, risk_lots)
        qty = min(lots * lot_size, cfg["freeze_qty"])
        total_est_ch = fetch_exact_trade_charges(inst_key, ltp, ltp, qty, True).get("total", 180.0)

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
                    "est_charges": total_est_ch, "gann_level": setup["gann_level"], "last_ltp": fill,
                    "sl": sl_price, "peak_gain": 0.0, "entry_ts": now_ts, "last_peak_ts": now_ts,
                    "trail_stage": None, "low_fuel_streak": 0, "is_expiry_day": False,
                    "trade_mode": trade_mode, "target_label": "SMART HYBRID ∞",
                    "ltp_window": deque([(now_ts, fill)]), "setup_name": f"{setup['name']}|{mem_tag}",
                    "entry_time": entry_time_str,
                }
            self.pending_setups[idx] = None
            self.subscribe_tick_instrument(inst_key)
            self.save_active_positions_state()
            self.render_live_dashboard(now_ist.strftime("%H:%M"))

    def try_execute_option_entry(self, idx, cfg, setup, df, spot_close, chain, atr14, now_ist, is_today_exp, mem_tag, trade_mode, in_expiry_slope=False):
        with self.pos_lock:
            if idx in self.active_positions or len(self.active_positions) >= MAX_ACTIVE_TRADES_TOTAL:
                return

        if not chain:
            return
        step = cfg.get("step", 50)
        tick_sz = cfg.get("tick_size", 0.05)

        base_offset = int(
            self.tuned_params.get("expiry_strike_offset", 1)
            if in_expiry_slope
            else self.tuned_params.get("strike_offset", 0)
        )
        opt_field = "call_options" if setup["side"] == "CE" else "put_options"
        spot_risk, spot_sl_equiv = self.compute_true_fractal_swing_sl(df, setup["side"], spot_close, atr14)
        cap = self.get_active_capital()
        max_idx_risk = float(self.tuned_params.get("index_max_risk_inr", round(cap * 0.05, 2)))
        min_req_delta = float(
            self.tuned_params.get("expiry_slope_min_delta", 0.30)
            if in_expiry_slope
            else self.tuned_params.get("min_option_delta", 0.45)
        )
        max_theta_pct = float(self.tuned_params.get("max_theta_decay_pct", 8.0))

        selected_candidate = None
        for extra_otm in (0, 1, 2):
            eff_offset = base_offset + extra_otm
            strike_shift = (eff_offset * step) if setup["side"] == "CE" else (-eff_offset * step)
            cand_strike = (round(spot_close / step) * step) + strike_shift
            chosen = next((row.get(opt_field, {}) for row in chain if row.get("strike_price") == cand_strike), None)
            if not chosen:
                continue

            inst_key = chosen.get("instrument_key")
            mkt = chosen.get("market_data", {})
            greeks = chosen.get("option_greeks", {})
            ltp = float(mkt.get("ltp", 0))
            bid = float(mkt.get("bid_price", 0) or 0)
            ask = float(mkt.get("ask_price", 0) or 0)

            min_opt_premium = max(tick_sz * 20, round(spot_close * 0.00012, 2))
            if ltp < min_opt_premium:
                continue

            if bid > 0 and ask >= bid and ((ask - bid) / ltp * 100.0) > MAX_OPTION_SPREAD_PCT:
                continue

            raw_delta = abs(float(greeks.get("delta", 0.0)))
            eff_min_delta = 0.32 if extra_otm > 0 else min_req_delta
            if raw_delta > 0 and raw_delta < eff_min_delta:
                continue

            theta_val = abs(float(greeks.get("theta", 0.0)))
            if theta_val > 0 and ((theta_val / 6.25) / max(ltp, 1.0)) * 100.0 > max_theta_pct:
                continue

            live_delta = min(0.75, max(0.25, raw_delta)) if raw_delta > 0 else eff_min_delta
            raw_opt_sl = min(max(spot_risk * live_delta, ltp * 0.12), ltp * 0.35)
            sl_points = max(round_to_tick(raw_opt_sl, tick_sz), tick_sz * 4)
            est_ch_1 = fetch_exact_trade_charges(inst_key, ltp, ltp, cfg["lot_size"], False).get("total", 65.0)
            risk_1_lot = (sl_points * cfg["lot_size"]) + est_ch_1

            if risk_1_lot <= max_idx_risk:
                selected_candidate = {
                    "strike": cand_strike,
                    "inst_key": inst_key,
                    "ltp": ltp,
                    "live_delta": live_delta,
                    "sl_points": sl_points,
                    "risk_1_lot": risk_1_lot,
                    "otm_fit": extra_otm > 0,
                }
                break

        if not selected_candidate:
            self.pending_setups[idx] = None
            return

        target_strike = selected_candidate["strike"]
        inst_key = selected_candidate["inst_key"]
        ltp = selected_candidate["ltp"]
        live_delta = selected_candidate["live_delta"]
        sl_points = selected_candidate["sl_points"]
        risk_1_lot = selected_candidate["risk_1_lot"]
        sl_price = round_to_tick(ltp - sl_points, tick_sz)

        gear = int(self.tuned_params.get("driver_gear", 3))
        eff_cap = cap * (0.65 if gear <= 2 else 1.0)
        cap_lots = max(1, int(eff_cap // (ltp * cfg["lot_size"])))
        risk_lots = max(1, int(max_idx_risk // max(risk_1_lot, 1.0)))
        lots = min(cap_lots, risk_lots)
        qty = min(lots * cfg["lot_size"], cfg["freeze_qty"])
        total_est_ch = fetch_exact_trade_charges(inst_key, ltp, ltp, qty, False).get("total", 65.0)

        order_res = place_marketable_limit_order(inst_key, qty, "BUY", ltp, tick_sz)
        if order_res.get("status") == "success":
            fill = order_res["fill_price"]
            now_ts = time.time()
            self.trade_counter += 1
            t_no = self.trade_counter
            entry_time_str = now_ist.strftime("%H:%M:%S")
            mode_suffix = "EXPIRY_SLOPE" if in_expiry_slope else ("OTM_RISK_FIT" if selected_candidate["otm_fit"] else mem_tag)
            with self.pos_lock:
                self.active_positions[idx] = {
                    "trade_no": t_no, "type": "INDEX_OPT", "instrument_key": inst_key,
                    "strike": target_strike, "side": setup["side"], "tx_side": "BUY",
                    "display_side": setup["side"], "qty": qty, "tick_size": tick_sz,
                    "delta": live_delta, "entry_price": fill, "spot_entry": spot_close,
                    "spot_sl": spot_sl_equiv, "r_unit": sl_points, "est_charges": total_est_ch,
                    "gann_level": setup["gann_level"], "last_ltp": fill, "sl": sl_price,
                    "peak_gain": 0.0, "entry_ts": now_ts, "last_peak_ts": now_ts,
                    "trail_stage": None, "low_fuel_streak": 0, "is_expiry_day": is_today_exp,
                    "trade_mode": trade_mode, "target_label": "SMART HYBRID ∞",
                    "ltp_window": deque([(now_ts, fill)]), "setup_name": f"{setup['name']}|{mode_suffix}",
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
            exit_reason = None

            if pos["side"].upper() in self.macro_context.get("eject_sides", []):
                exit_reason = f"AI_SHOCK_EJECT_{pos['side']}"
            elif pos["type"] == "INDEX_OPT" and hhmm >= nse_exit_t:
                exit_reason = "MUHURAT_EOD_EXIT" if self.session_sched.get("is_muhurat") else "EOD_EXIT"
            elif pos["type"] == "MCX_FUT" and hhmm >= mcx_exit_t:
                exit_reason = "MCX_EOD_EXIT"
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

    def run(self):
        if not UPSTOX_ACCESS_TOKEN:
            log.error("UPSTOX_ACCESS_TOKEN missing! Exiting.")
            sys.exit(1)

        now_ist = datetime.now(IST)

        if is_overnight_dead_hours(now_ist):
            log.info(f"Outside market hours ({now_ist.strftime('%H:%M')} IST). Standing down cleanly.")
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
            return

        self.session_sched = inspect_special_session_schedule(now_ist)
        live_nse_status = check_live_exchange_status("NSE")
        if HAS_AUTO_TUNER:
            learned_reason = get_market_closed_reason(now_ist, live_nse_status)
            if learned_reason and not self.session_sched["is_holiday"]:
                self.session_sched["is_holiday"] = True
                self.session_sched["description"] = learned_reason
                self.session_sched["nse_open"] = False
                self.session_sched["mcx_open"] = True
                self.session_sched["mcx_start"] = "17:00"

        if self.session_sched["is_holiday"]:
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
                return

        signal.signal(signal.SIGTERM, lambda s, f: setattr(self, "stop_requested", True))
        signal.signal(signal.SIGINT, lambda s, f: setattr(self, "stop_requested", True))

        self.load_self_tuned_intelligence()
        self.refresh_live_macro_news(force=True)
        self.load_today_state_from_journal()
        self.init_contracts()
        self.load_active_positions_state()
        self.bot_status_text = "BOT LIVE"
        session_start_ts = time.time()
        self.engine_boot_ts = session_start_ts
        holiday_checked_0916 = False

        if self.session_sched["is_muhurat"]:
            sess_banner = f"🪔 MUHURAT TRADING • NSE {self.session_sched['nse_start']}–{self.session_sched['nse_exit']} | MCX {self.session_sched['mcx_start']}–{self.session_sched['mcx_exit']}"
        elif self.session_sched["is_holiday"] and not self.session_sched["nse_open"] and self.session_sched["mcx_open"]:
            sess_banner = f"🛢️ NSE HOLIDAY • MCX EVENING SESSION ({self.session_sched['mcx_start']}–{self.session_sched['mcx_exit']} IST)"
        else:
            sess_banner = "5m IDX / 5m MCX (09:15 AM – 11:30 PM IST)"

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

            if not holiday_checked_0916 and self.session_sched["nse_open"] and not self.session_sched["is_muhurat"] and "09:16" <= hhmm <= "09:30":
                holiday_checked_0916 = True
                nifty_check_df = fetch_intraday_candles(INDICES["NIFTY"]["spot_key"], 5)
                today_s = now_ist.strftime("%Y-%m-%d")
                has_today_candle = (
                    not nifty_check_df.empty
                    and str(nifty_check_df["timestamp"].iloc[-1])[:10] == today_s
                )
                if not has_today_candle:
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
            if self.ai_agent:
                self.ai_agent.think_and_adapt_live_strategy()
            self.manage_open_positions(now_ist)

            if hhmm >= "23:35":
                self.shutdown_cleanly(now_ist, "EOD CLOSED")
                break

            cap = self.get_active_capital()
            max_profit = cap * DAILY_PROFIT_LOCK_PCT
            max_loss = -abs(float(self.tuned_params.get("max_daily_loss_inr", round(cap * abs(DAILY_LOSS_LOCK_PCT), 2))))
            risk_locked = (self.day_pnl >= max_profit or self.day_pnl <= max_loss or self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES)

            if risk_locked:
                self.bot_status_text = "BOT PAUSED"
            elif not self.session_sched["nse_open"] and hhmm < self.session_sched.get("mcx_start", "17:00"):
                self.bot_status_text = f"STANDBY (MCX OPENS {self.session_sched.get('mcx_start', '17:00')})"
            elif self.session_sched["is_muhurat"] and hhmm < self.session_sched.get("nse_start", "18:00") and hhmm < self.session_sched.get("mcx_start", "17:00"):
                self.bot_status_text = f"STANDBY (MUHURAT {self.session_sched.get('nse_start', '18:00')})"
            else:
                self.bot_status_text = "BOT LIVE"

            if not risk_locked:
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
