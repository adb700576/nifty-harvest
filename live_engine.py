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
# 1. CONFIGURATION & STORAGE
# ==============================================================================
IST = timezone(timedelta(hours=5, minutes=30))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("VWAP_PCR_ENGINE")

UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GSHEET_WEBHOOK_URL = os.getenv("GSHEET_WEBHOOK_URL", "").strip()
PAPER_MODE = os.getenv("PAPER_MODE", "true").strip().lower() == "true"

# Momentum & VWAP Retest Thresholds (Anti-Whipsaw Filter)
MIN_FUT_VOL_RATIO = 1.20            # Futures candle volume must be >= 1.20x 20-bar average
MIN_CANDLE_BODY_RATIO = 0.50        # Candle body must be >= 50% of total high-low range
VWAP_TOUCH_BAND_ATR = 0.30          # Price must touch/retest within 0.30 * ATR of VWAP
SWING_BUFFER_ATR = 0.15             # Buffer below Swing Low / above Swing High
MIN_PCR_STEP_DIFF = 0.02            # Minimum PCR rise/fall to confirm strong directional shift

# Session & Execution Controls
SESSION_MAX_RUNTIME_SEC = 20700
INDEX_START_TIME = "09:15"
INDEX_END_TIME = "15:10"
INDEX_EXIT_TIME = "15:15"

MCX_START_TIME = "09:15"
MCX_END_TIME = "22:30"
MCX_EXIT_TIME = "23:25"

REENTRY_COOLDOWN_SEC = 300          # 5-min cooldown after normal SL (instant bypass on reversal)
MAX_ACTIVE_TRADES_TOTAL = 5
STARTUP_WARMUP_SEC = 45
DASHBOARD_PULSE_SEC = 5.0

JOURNAL_FILE = "live_journal/trade_journal.csv"
ACTIVE_STATE_FILE = "live_journal/active_positions.json"
DASHBOARD_STATE_FILE = "live_journal/dashboard_state.json"
LOCK_FILE = "live_journal/engine.lock"

JOURNAL_FIELDNAMES = [
    "trade_no", "date", "entry_time", "exit_time", "symbol", "contract", "side",
    "qty", "pnl_units", "entry_price", "exit_price", "points", "sl", "target", "exit_reason",
    "gross_pnl", "brokerage", "taxes_other", "charges", "pnl", "day_pnl", "setup",
]

INDICES = {
    "NIFTY":      {"type": "INDEX_OPT", "spot_key": "NSE_INDEX|Nifty 50", "tf_min": 1},
    "BANKNIFTY":  {"type": "INDEX_OPT", "spot_key": "NSE_INDEX|Nifty Bank", "tf_min": 1},
    "FINNIFTY":   {"type": "INDEX_OPT", "spot_key": "NSE_INDEX|Nifty Fin Service", "tf_min": 1},
    "MIDCPNIFTY": {"type": "INDEX_OPT", "spot_key": "NSE_INDEX|NIFTY MID SELECT", "tf_min": 1},
    "SENSEX":     {"type": "INDEX_OPT", "spot_key": "BSE_INDEX|SENSEX", "tf_min": 1},
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
            log.info(f"[GSHEET RESTORE] Restored {added_count} trade(s) from Google Sheets.")
    except Exception as e:
        log.warning(f"[GSHEET RESTORE] Sync skipped: {e}")


# ==============================================================================
# 2. TELEGRAM UI (EXACT DASHBOARD PRESERVED) & CHARGES
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
        except Exception:
            if attempt == 0:
                time.sleep(0.5)


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
    if is_overnight_dead_hours(datetime.now(IST)) or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
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
    if buy_price > 0 and sell_price > 0 and eff_qty > 0 and HAS_UPSTOX_SDK:
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
# 3. UPSTOX API HELPERS (FUTURES + OPTION CHAIN + INTRADAY CANDLES)
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
                    "query": target_sym, "exchanges": exchange, "segments": "FUT",
                    "expiry": expiry_kw, "page_number": 1, "records": 30,
                }
                items = requests.get(url, headers=upstox_headers(), params=params, timeout=10).json().get("data", [])
                for it in items:
                    if str(it.get("instrument_type", "")).upper() == "FUT" and str(it.get("trading_symbol", "")).upper().startswith(f"{target_sym} FUT"):
                        all_candidates.append(it)
            except Exception as e:
                log.warning(f"[{idx}] Instrument search error ({expiry_kw}): {e}")

    if not all_candidates:
        return {}

    dedup = {str(c.get("instrument_key", "")): c for c in all_candidates if c.get("instrument_key")}
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
            "expiries": expiries, "lot_size": lot_size, "freeze_quantity": freeze_qty,
            "tick_size": tick_size if tick_size > 0 else 0.05, "step": max(step, 1),
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
# 4. PURE VWAP + FUTURES VOLUME + CANDLESTICK PATTERN + SWING SL ENGINE
# ==============================================================================
def build_fut_vwap_df(fut_df: pd.DataFrame, spot_df: pd.DataFrame = None) -> pd.DataFrame:
    """Builds VWAP, ATR14, Volume Ratio & Swing High/Low strictly from Futures data (or spot fallback)."""
    df = fut_df.copy() if (fut_df is not None and not fut_df.empty) else (spot_df.copy() if spot_df is not None else pd.DataFrame())
    if df.empty:
        return df

    prev_close = df["close"].shift(1).fillna(df["open"])
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["tr"] = tr
    df["atr14"] = tr.rolling(14, min_periods=1).mean()

    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    day_ser = df["timestamp"].dt.date
    vol_safe = df["volume"].replace(0, 1.0)
    df["vwap"] = (tp * vol_safe).groupby(day_ser).cumsum() / vol_safe.groupby(day_ser).cumsum()

    vol_mean = df["volume"].rolling(20, min_periods=1).mean().replace(0, np.nan)
    df["vol_ratio"] = (df["volume"] / vol_mean).fillna(1.0)

    c_range = (df["high"] - df["low"]).replace(0, 1e-6)
    df["body_ratio"] = (df["close"] - df["open"]).abs() / c_range
    return df


def find_latest_swing_levels(df: pd.DataFrame, lookback: int = 12) -> tuple:
    """Finds the latest confirmed fractal Swing Low and Swing High on Futures candles."""
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
    """
    Detects Bullish or Bearish Candlestick Pattern near/retesting VWAP
    ONLY when Futures Volume & Solid Candle Body confirm true momentum (blocks VWAP whipsaws).
    """
    if len(df) < 5:
        return {"signal": None, "pattern": None, "vol_ratio": 0.0, "body_ratio": 0.0}

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

    # Anti-whipsaw Momentum Gate: Volume expansion + solid candle body
    has_momentum = (vol_ratio >= MIN_FUT_VOL_RATIO) and (body_ratio >= MIN_CANDLE_BODY_RATIO)
    if not has_momentum:
        return {"signal": None, "pattern": None, "vol_ratio": vol_ratio, "body_ratio": body_ratio}

    touch_band = VWAP_TOUCH_BAND_ATR * atr

    # ---------------- BULLISH PATTERN NEAR / RESTING ON VWAP ----------------
    touched_vwap_from_above = (
        abs(l3 - vwap3) <= touch_band
        or abs(l2 - vwap2) <= touch_band
        or (l3 <= vwap3 and cl3 > vwap3)
    )
    bull_pattern = None
    if touched_vwap_from_above and cl3 > vwap3 and cl3 > o3:
        if (cl2 < o2) and (cl3 >= o2) and (o3 <= cl2):
            bull_pattern = "BULLISH_ENGULFING"
        elif (cl1 < o1) and (b1 / r1 >= 0.45) and (b2 / r2 <= 0.40) and (cl3 > mid1):
            bull_pattern = "MORNING_STAR"
        elif (lower_wick3 >= 1.5 * max(b3, r3 * 0.1)) and ((cl3 - l3) / r3 >= 0.60):
            bull_pattern = "BULL_HAMMER"
        elif (cl3 > h2) and (body_ratio >= 0.60):
            bull_pattern = "VWAP_BOUNCE_MOMENTUM"

    if bull_pattern:
        return {"signal": "CE", "pattern": bull_pattern, "vol_ratio": vol_ratio, "body_ratio": body_ratio}

    # ---------------- BEARISH PATTERN NEAR / REJECTING VWAP ----------------
    touched_vwap_from_below = (
        abs(h3 - vwap3) <= touch_band
        or abs(h2 - vwap2) <= touch_band
        or (h3 >= vwap3 and cl3 < vwap3)
    )
    bear_pattern = None
    if touched_vwap_from_below and cl3 < vwap3 and cl3 < o3:
        if (cl2 > o2) and (cl3 <= o2) and (o3 >= cl2):
            bear_pattern = "BEARISH_ENGULFING"
        elif (cl1 > o1) and (b1 / r1 >= 0.45) and (b2 / r2 <= 0.40) and (cl3 < mid1):
            bear_pattern = "EVENING_STAR"
        elif (upper_wick3 >= 1.5 * max(b3, r3 * 0.1)) and ((h3 - cl3) / r3 >= 0.60):
            bear_pattern = "SHOOTING_STAR"
        elif (cl3 < l2) and (body_ratio >= 0.60):
            bear_pattern = "VWAP_REJECT_MOMENTUM"

    if bear_pattern:
        return {"signal": "PE", "pattern": bear_pattern, "vol_ratio": vol_ratio, "body_ratio": body_ratio}

    return {"signal": None, "pattern": None, "vol_ratio": vol_ratio, "body_ratio": body_ratio}


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
            "tag": "VWAP_PCR_ENGINE", "instrument_token": instrument_key,
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
# 6. PURE PCR + FUTURES VWAP ENGINE (1M INDEX / 5M MCX + INFINITY SWING TRAIL)
# ==============================================================================
class IntradayV3Engine:
    def __init__(self):
        self.expiries = {}
        self.fut_contracts = {}
        self.last_exit_ts = {idx: 0.0 for idx in INDICES}
        self.last_exit_candle_ts = {idx: "" for idx in INDICES}
        self.open_pcr = {idx: None for idx in INDICES}
        self.pcr_history = {idx: deque(maxlen=6) for idx in INDICES}
        self.active_positions = {}
        self.pos_lock = threading.Lock()
        self.closed_trades = []
        self.trade_counter = 0
        self.day_pnl = 0.0
        self.last_summaries = {}
        self.latest_fut_dfs = {}
        self.dashboard_msg_id = load_saved_dashboard_id()
        self.dashboard_pulse = False
        self.dashboard_lock = threading.Lock()
        self.engine_boot_ts = time.time()
        self.streamer = None
        self.ws_connected = False
        self.subscribed_keys = set()
        self.bot_status_text = "BOT LIVE"
        self.stop_requested = False

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
            for idx, pos in self.active_positions.items():
                serializable[idx] = dict(pos)
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
                    inst_key = pos["instrument_key"]
                    is_short = pos["tx_side"] == "SELL"
                    entry = float(pos["entry_price"])
                    q_data = live_quotes.get(inst_key, {})
                    curr_ltp = float(q_data.get("last_price", pos.get("last_ltp", entry)))
                    pos["last_ltp"] = curr_ltp

                    self.active_positions[idx] = pos
                    self.trade_counter = max(self.trade_counter, int(pos.get("trade_no", 0)), len(self.closed_trades))

                    if (not is_short and curr_ltp <= pos["sl"]) or (is_short and curr_ltp >= pos["sl"]):
                        reason = "SWING_TRAIL_SL" if pos.get("trail_stage") else "SWING_SL_HIT"
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
        """Real-time tick protection: locks 1:1 -> 1:2 -> Infinity Runner & triggers SL."""
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

                    r_unit = max(pos.get("r_unit", 1.0), 1e-6)
                    tick_sz = pos.get("tick_size", 0.05)
                    curr_r = curr_gain / r_unit

                    # 1:1 reached -> lock +0.15R profit; 1:2 reached -> lock 1.0R & ride to Infinity
                    if curr_r >= 2.0:
                        lock_sl = round_to_tick((entry - 1.0 * r_unit) if is_short else (entry + 1.0 * r_unit), tick_sz)
                        if (is_short and lock_sl < pos["sl"]) or (not is_short and lock_sl > pos["sl"]):
                            pos["sl"] = lock_sl
                        pos["trail_stage"] = "1:2 → ∞ RUNNER"
                    elif curr_r >= 1.0:
                        lock_sl = round_to_tick((entry - 0.15 * r_unit) if is_short else (entry + 0.15 * r_unit), tick_sz)
                        if (is_short and lock_sl < pos["sl"]) or (not is_short and lock_sl > pos["sl"]):
                            pos["sl"] = lock_sl
                        if not pos.get("trail_stage"):
                            pos["trail_stage"] = "1:1 LOCK → ∞"

                    if not is_short and ltp <= pos["sl"]:
                        exit_reason = "SWING_TRAIL_SL" if pos.get("trail_stage") else "SWING_SL_HIT"
                        target_idx = idx
                    elif is_short and ltp >= pos["sl"]:
                        exit_reason = "SWING_TRAIL_SL" if pos.get("trail_stage") else "SWING_SL_HIT"
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
        is_today_exp = nearest == now_ist.strftime("%Y-%m-%d")
        if is_today_exp and hhmm > "14:45" and len(valid_exps) > 1:
            return valid_exps[1], False
        return nearest, is_today_exp

    def is_within_trade_window(self, idx: str, now_ist: datetime) -> bool:
        hhmm = now_ist.strftime("%H:%M")
        if INDICES[idx]["type"] == "MCX_FUT":
            return MCX_START_TIME <= hhmm <= MCX_END_TIME
        return INDEX_START_TIME <= hhmm <= INDEX_END_TIME

    def analyze_strong_pcr(self, idx: str, chain: list) -> tuple:
        """Checks if PCR is rising strongly (bullish) or falling strongly (bearish)."""
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
            net_icon = "🟢" if net_pnl >= 0 else "🔴"

            self.dashboard_pulse = not self.dashboard_pulse
            if "CLOSED" in self.bot_status_text or "STOPPED" in self.bot_status_text:
                status_header = "🔴 <b>MARKET CLOSED</b>"
            else:
                pulse_dot = "🟢" if self.dashboard_pulse else "⚪"
                status_header = f"{pulse_dot} <b>BOT LIVE</b>"

            tot_closed = len(self.closed_trades)
            total_wins = sum(1 for t in self.closed_trades if t["pnl"] > 0)
            total_losses = sum(1 for t in self.closed_trades if t["pnl"] <= 0)
            day_wr = int(round((total_wins / tot_closed) * 100.0)) if tot_closed > 0 else 0

            lines = [
                f"{status_header}  |  <code>{hhmm} IST</code>  |  {mode_badge} <code>VWAP+PCR</code>",
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
                    trail_tag = f"🛡️ <code>{pos['trail_stage']}</code>" if pos.get("trail_stage") else "<code>SWING SL</code>"
                    p_icon = "🟢" if pnl >= 0 else "🔴"
                    sl_label = f"Locked <b>₹{sl_net_pnl:+,.0f}</b>" if sl_net_pnl >= 0 else f"Max Risk <b>₹{abs(sl_net_pnl):,.0f}</b>"

                    lot_sz = int(pos.get("lot_size", pos["qty"]))
                    num_lots = int(pos.get("lots", max(1, pos["qty"] // max(lot_sz, 1))))
                    lot_spec_txt = f"{num_lots} Lot = {pos['qty']} Qty"

                    lines.extend([
                        f"• <b>#{pos['trade_no']} {p_idx}</b> <code>{pos['strike']} {pos['display_side']}</code> (<code>{lot_spec_txt}</code>)  |  {trail_tag}",
                        f"  In: <b>₹{pos['entry_price']:.2f}</b> (<code>{pos['entry_time'][:5]}</code>) → LTP: <b>₹{ltp:.2f}</b>",
                        f"  PnL: <code>{pts_diff:.2f} pt × {eff_units} = ₹{gross_val:,.0f}</code>",
                        f"  SL: <code>₹{pos['sl']:.2f}</code> ({sl_label})  |  Target: <code>1:2 → ∞ RUNNER</code>",
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
                lines.append(f"\n📡 <b>INDICES FUT + VWAP (1m)</b>")
                for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"):
                    s = self.last_summaries.get(idx)
                    if s:
                        lines.append(f"• <b>{s['idx']}:</b> <code>{s['spot']}</code> | PCR <b>{s['pcr']:.2f}</b> | VWAP <b>{s['vwap']}</b> | <code>{s['short_status']}</code>")

                lines.append("\n🛢️ <b>MCX MINI FUT + VWAP (5m)</b>")
                for idx in ("CRUDEOIL", "NATURALGAS", "GOLD", "SILVER", "COPPER"):
                    s = self.last_summaries.get(idx)
                    if s:
                        lines.append(f"• <b>{s['idx']}:</b> <code>{s['spot']}</code> | VWAP <b>{s['vwap']}</b> | <code>{s['short_status']}</code>")

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
                    fut_keys = [self.fut_contracts.get(idx, {}).get("instrument_key") for idx in INDICES if self.fut_contracts.get(idx, {}).get("instrument_key")]
                    all_live_keys = list(dict.fromkeys(active_keys + fut_keys))

                    quotes = fetch_ltp_quote(all_live_keys)
                    if quotes:
                        with self.pos_lock:
                            for pos in self.active_positions.values():
                                q = quotes.get(pos["instrument_key"], {})
                                if q.get("last_price"):
                                    pos["last_ltp"] = float(q["last_price"])
                        for idx in INDICES:
                            fk = self.fut_contracts.get(idx, {}).get("instrument_key")
                            if fk and quotes.get(fk, {}).get("last_price") and idx in self.last_summaries:
                                ltp_val = float(quotes[fk]["last_price"])
                                self.last_summaries[idx]["spot"] = round(ltp_val, 2) if ltp_val < 1000 else int(round(ltp_val))

                    self.render_live_dashboard(datetime.now(IST).strftime("%H:%M"))
                except Exception:
                    pass

        threading.Thread(target=_pulse_loop, daemon=True).start()

    def close_position_now(self, idx: str, ltp: float, exit_reason: str, now_ist: datetime, is_reversal: bool = False):
        with self.pos_lock:
            pos = self.active_positions.pop(idx, None)
        if not pos:
            return

        # If reversing to opposite side, allow immediate re-entry (0 cooldown)
        self.last_exit_ts[idx] = 0.0 if is_reversal else time.time()
        df_curr = self.latest_fut_dfs.get(idx)
        if not is_reversal and df_curr is not None and not df_curr.empty:
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

        exit_time_str = now_ist.strftime("%H:%M:%S")
        journal_entry = {
            "trade_no": pos["trade_no"], "date": now_ist.strftime("%Y-%m-%d"),
            "entry_time": pos.get("entry_time", ""), "exit_time": exit_time_str,
            "symbol": idx, "contract": str(pos["strike"]), "side": pos["display_side"],
            "qty": pos["qty"], "pnl_units": int(eff_units),
            "entry_price": pos["entry_price"], "exit_price": ltp,
            "points": pts, "sl": pos["sl"], "target": 0.0, "exit_reason": exit_reason,
            "gross_pnl": gross_pnl, "brokerage": ch_info["brokerage"],
            "taxes_other": ch_info["taxes_other"], "charges": ch_info["total"],
            "pnl": net_pnl, "day_pnl": self.day_pnl, "setup": pos["setup_name"],
        }
        self.closed_trades.append(journal_entry)
        record_trade_journal(journal_entry)
        self.render_live_dashboard(now_ist.strftime("%H:%M"))

    # ==========================================================================
    # DYNAMIC SWING LOW/HIGH SL SHIFTING TO INFINITY + REVERSAL CHECK
    # ==========================================================================
    def update_dynamic_swing_sl_and_check_reversal(
        self, idx: str, df_fut: pd.DataFrame, fut_close: float, atr14: float,
        now_ist: datetime, pattern_sig: str, strong_bull_pcr: bool, strong_bear_pcr: bool
    ) -> bool:
        """
        1. Shifts SL dynamically to every new higher Swing Low (for CE) or lower Swing High (for PE) + buffer up to 15:10 IST.
        2. If opposite PCR + VWAP pattern triggers, closes current trade and returns True to immediately reverse to PE/CE.
        """
        with self.pos_lock:
            pos = self.active_positions.get(idx)
        if not pos or len(df_fut) < 4:
            return False

        is_short = pos["tx_side"] == "SELL"
        is_mcx = pos["type"] == "MCX_FUT"
        entry = pos["entry_price"]
        ltp = pos.get("last_ltp", entry)
        tick_sz = pos.get("tick_size", 0.05)
        eff_delta = pos.get("delta", 0.50)
        r_unit = max(pos.get("r_unit", 1.0), 1e-6)
        vwap_val = float(df_fut.iloc[-1]["vwap"])
        hhmm = now_ist.strftime("%H:%M")

        # 1. Check Reversal to Opposite Side (e.g., CE -> PE when PCR falls & bearish VWAP rejection occurs)
        if pos["side"] == "CE" and pattern_sig == "PE" and strong_bear_pcr and fut_close < vwap_val:
            self.close_position_now(idx, ltp, "REVERSAL_TO_PE", now_ist, is_reversal=True)
            return True
        if pos["side"] == "PE" and pattern_sig == "CE" and strong_bull_pcr and fut_close > vwap_val:
            self.close_position_now(idx, ltp, "REVERSAL_TO_CE", now_ist, is_reversal=True)
            return True

        # 2. Check Futures Swing SL hit
        fut_sl = float(pos.get("fut_sl", 0.0))
        if fut_sl > 0:
            if pos["side"] == "CE" and fut_close <= fut_sl:
                self.close_position_now(idx, ltp, "FUT_SWING_SL_HIT", now_ist)
                return False
            if pos["side"] == "PE" and fut_close >= fut_sl:
                self.close_position_now(idx, ltp, "FUT_SWING_SL_HIT", now_ist)
                return False

        # 3. Always shift SL to Next Latest Swing Low (CE) / Swing High (PE) with Buffer up to 15:10 IST
        allow_shift = (hhmm <= "15:10") if not is_mcx else (hhmm <= MCX_EXIT_TIME)
        if allow_shift:
            swing_low, swing_high = find_latest_swing_levels(df_fut, lookback=10)
            buf = SWING_BUFFER_ATR * atr14
            curr_gain = (entry - ltp) if is_short else (ltp - entry)
            curr_r = curr_gain / r_unit

            if pos["side"] == "CE":
                new_fut_sl = round(swing_low - buf, 2)
                if new_fut_sl > fut_sl and new_fut_sl < fut_close:
                    pos["fut_sl"] = new_fut_sl
                    if is_mcx:
                        cand_opt_sl = round_to_tick(new_fut_sl, tick_sz)
                    else:
                        fut_move_from_entry = new_fut_sl - float(pos.get("fut_entry", fut_close))
                        cand_opt_sl = round_to_tick(entry + (fut_move_from_entry * eff_delta), tick_sz)

                    # Guarantee at least 1:1 -> +0.15R and 1:2 -> +1.0R lock
                    if curr_r >= 2.0:
                        cand_opt_sl = max(cand_opt_sl, round_to_tick(entry + 1.0 * r_unit, tick_sz))
                    elif curr_r >= 1.0:
                        cand_opt_sl = max(cand_opt_sl, round_to_tick(entry + 0.15 * r_unit, tick_sz))

                    if cand_opt_sl > pos["sl"] and cand_opt_sl < ltp - tick_sz:
                        pos["sl"] = cand_opt_sl
                        pos["trail_stage"] = "SWING TRAIL → ∞"
                        self.save_active_positions_state()
            else:
                new_fut_sl = round(swing_high + buf, 2)
                if (fut_sl <= 0 or new_fut_sl < fut_sl) and new_fut_sl > fut_close:
                    pos["fut_sl"] = new_fut_sl
                    if is_mcx:
                        cand_opt_sl = round_to_tick(new_fut_sl, tick_sz)
                        if curr_r >= 2.0:
                            cand_opt_sl = min(cand_opt_sl, round_to_tick(entry - 1.0 * r_unit, tick_sz))
                        elif curr_r >= 1.0:
                            cand_opt_sl = min(cand_opt_sl, round_to_tick(entry - 0.15 * r_unit, tick_sz))
                        if cand_opt_sl < pos["sl"] and cand_opt_sl > ltp + tick_sz:
                            pos["sl"] = cand_opt_sl
                            pos["trail_stage"] = "SWING TRAIL → ∞"
                            self.save_active_positions_state()
                    else:
                        fut_move_from_entry = float(pos.get("fut_entry", fut_close)) - new_fut_sl
                        cand_opt_sl = round_to_tick(entry + (fut_move_from_entry * eff_delta), tick_sz)
                        if curr_r >= 2.0:
                            cand_opt_sl = max(cand_opt_sl, round_to_tick(entry + 1.0 * r_unit, tick_sz))
                        elif curr_r >= 1.0:
                            cand_opt_sl = max(cand_opt_sl, round_to_tick(entry + 0.15 * r_unit, tick_sz))
                        if cand_opt_sl > pos["sl"] and cand_opt_sl < ltp - tick_sz:
                            pos["sl"] = cand_opt_sl
                            pos["trail_stage"] = "SWING TRAIL → ∞"
                            self.save_active_positions_state()

        return False

    # ==========================================================================
    # MAIN SYMBOL EVALUATION (FUTURES VWAP + PCR + MOMENTUM GATE)
    # ==========================================================================
    def evaluate_instrument(self, idx: str, cfg: dict, now_ist: datetime):
        is_mcx = cfg["type"] == "MCX_FUT"
        tf_min = 5 if is_mcx else 1
        fut_key = self.fut_contracts.get(idx, {}).get("instrument_key", "")
        if not fut_key:
            return None

        # Track Futures Data (1m for Indices, 5m for MCX)
        fut_df_raw = fetch_intraday_candles(fut_key, tf_min)
        spot_df_raw = fut_df_raw if is_mcx else fetch_intraday_candles(cfg["spot_key"], tf_min)
        df_fut = build_fut_vwap_df(fut_df_raw, spot_df_raw)
        if df_fut.empty or len(df_fut) < 5:
            return None

        self.latest_fut_dfs[idx] = df_fut
        c_last = df_fut.iloc[-1]
        fut_close = float(c_last["close"])
        vwap_val = float(c_last["vwap"])
        atr14 = max(float(c_last["atr14"]), cfg.get("tick_size", 0.05) * 2)
        vol_ratio = float(c_last["vol_ratio"])

        # Option Chain PCR for Indices; Futures Volume/Price for MCX
        chain, active_expiry, is_today_exp = [], "", False
        if is_mcx:
            pcr = 0.0
            strong_bull_pcr = (fut_close > vwap_val) and (vol_ratio >= MIN_FUT_VOL_RATIO)
            strong_bear_pcr = (fut_close < vwap_val) and (vol_ratio >= MIN_FUT_VOL_RATIO)
        else:
            active_expiry, is_today_exp = self.get_smart_expiry(idx, now_ist)
            chain = fetch_option_chain(cfg["spot_key"], active_expiry)
            pcr, _, strong_bull_pcr, strong_bear_pcr = self.analyze_strong_pcr(idx, chain)

        # Detect Bullish / Bearish Pattern resting on VWAP with Momentum Gate
        pat_res = detect_vwap_pattern_with_momentum(df_fut)
        pattern_sig = pat_res["signal"]
        pattern_name = pat_res["pattern"]

        with self.pos_lock:
            in_trade = idx in self.active_positions

        reversed_now = False
        if in_trade:
            reversed_now = self.update_dynamic_swing_sl_and_check_reversal(
                idx, df_fut, fut_close, atr14, now_ist, pattern_sig, strong_bull_pcr, strong_bear_pcr
            )
            with self.pos_lock:
                pos = self.active_positions.get(idx)
            if pos and not reversed_now:
                ltp_now = pos.get("last_ltp", fut_close if is_mcx else pos["entry_price"])
                is_short = pos["tx_side"] == "SELL"
                est_ch = float(pos.get("est_charges", 180.0 if is_mcx else 65.0))
                eff_u = float(pos.get("pnl_units", pos["qty"]))
                pos_mtm = ((pos["entry_price"] - ltp_now) * eff_u if is_short else (ltp_now - pos["entry_price"]) * eff_u) - est_ch
                return {
                    "idx": idx, "is_mcx": is_mcx,
                    "spot": round(fut_close, 2) if fut_close < 1000 else int(round(fut_close)),
                    "vwap": round(vwap_val, 2) if vwap_val < 1000 else int(round(vwap_val)),
                    "pcr": pcr,
                    "short_status": f"ACTIVE #{pos['trade_no']} (₹{pos_mtm:+.0f})",
                }

        uptime_sec = time.time() - self.engine_boot_ts
        if uptime_sec < STARTUP_WARMUP_SEC:
            rem_w = max(1, int(STARTUP_WARMUP_SEC - uptime_sec))
            return {
                "idx": idx, "is_mcx": is_mcx,
                "spot": round(fut_close, 2) if fut_close < 1000 else int(round(fut_close)),
                "vwap": round(vwap_val, 2) if vwap_val < 1000 else int(round(vwap_val)),
                "pcr": pcr, "short_status": f"WARMUP ({rem_w}s)",
            }

        sec_since_exit = time.time() - self.last_exit_ts.get(idx, 0.0)
        curr_candle_ts = str(c_last["timestamp"])
        if not reversed_now and (sec_since_exit < REENTRY_COOLDOWN_SEC or curr_candle_ts == self.last_exit_candle_ts.get(idx, "")):
            rem_min = max(1, int(math.ceil((REENTRY_COOLDOWN_SEC - sec_since_exit) / 60.0)))
            return {
                "idx": idx, "is_mcx": is_mcx,
                "spot": round(fut_close, 2) if fut_close < 1000 else int(round(fut_close)),
                "vwap": round(vwap_val, 2) if vwap_val < 1000 else int(round(vwap_val)),
                "pcr": pcr, "short_status": f"COOLDOWN ({rem_min}m)",
            }

        with self.pos_lock:
            total_open_trades = len(self.active_positions)

        if total_open_trades < MAX_ACTIVE_TRADES_TOTAL and self.is_within_trade_window(idx, now_ist):
            if pattern_sig == "CE" and strong_bull_pcr:
                if is_mcx:
                    self.execute_mcx_entry(idx, cfg, "CE", pattern_name, df_fut, fut_close, atr14, now_ist)
                else:
                    self.execute_option_entry(idx, cfg, "CE", pattern_name, df_fut, fut_close, chain, atr14, now_ist, is_today_exp)
            elif pattern_sig == "PE" and strong_bear_pcr:
                if is_mcx:
                    self.execute_mcx_entry(idx, cfg, "PE", pattern_name, df_fut, fut_close, atr14, now_ist)
                else:
                    self.execute_option_entry(idx, cfg, "PE", pattern_name, df_fut, fut_close, chain, atr14, now_ist, is_today_exp)

        pcr_tag = "PCR↑" if strong_bull_pcr else ("PCR↓" if strong_bear_pcr else "PCR~")
        vwap_tag = "ABV VWAP" if fut_close >= vwap_val else "BLW VWAP"
        short_status = f"{vwap_tag} | {pcr_tag}" if not is_mcx else f"{vwap_tag} | VOL {vol_ratio:.1f}x"

        return {
            "idx": idx, "is_mcx": is_mcx,
            "spot": round(fut_close, 2) if fut_close < 1000 else int(round(fut_close)),
            "vwap": round(vwap_val, 2) if vwap_val < 1000 else int(round(vwap_val)),
            "pcr": pcr, "short_status": short_status,
        }

    def execute_mcx_entry(self, idx, cfg, side, pattern_name, df_fut, fut_price, atr14, now_ist):
        with self.pos_lock:
            if idx in self.active_positions or len(self.active_positions) >= MAX_ACTIVE_TRADES_TOTAL:
                return

        inst_key = cfg["spot_key"]
        lot_size = int(cfg["lot_size"])
        pnl_mult = int(cfg.get("pnl_multiplier", lot_size))
        tick_sz = cfg.get("tick_size", 0.05)
        tx_side = "BUY" if side == "CE" else "SELL"

        ltp = float(fetch_ltp_quote([inst_key]).get(inst_key, {}).get("last_price", fut_price))
        swing_low, swing_high = find_latest_swing_levels(df_fut, lookback=10)
        buf = SWING_BUFFER_ATR * atr14

        if tx_side == "BUY":
            fut_sl = round_to_tick(swing_low - buf, tick_sz)
            sl_points = max(round_to_tick(ltp - fut_sl, tick_sz), round_to_tick(ltp * 0.0012, tick_sz), tick_sz * 4)
            sl_price = round_to_tick(ltp - sl_points, tick_sz)
        else:
            fut_sl = round_to_tick(swing_high + buf, tick_sz)
            sl_points = max(round_to_tick(fut_sl - ltp, tick_sz), round_to_tick(ltp * 0.0012, tick_sz), tick_sz * 4)
            sl_price = round_to_tick(ltp + sl_points, tick_sz)

        qty = min(lot_size, cfg["freeze_qty"])
        total_est_ch = fetch_exact_trade_charges(inst_key, ltp, ltp, pnl_mult, True).get("total", 180.0)

        order_res = place_marketable_limit_order(inst_key, qty, tx_side, ltp, tick_sz)
        if order_res.get("status") == "success":
            fill = order_res["fill_price"]
            sym = self.fut_contracts.get(idx, {}).get("trading_symbol", f"{idx} FUT")
            self.trade_counter = max(self.trade_counter, len(self.closed_trades)) + 1
            t_no = self.trade_counter
            entry_time_str = now_ist.strftime("%H:%M:%S")
            with self.pos_lock:
                self.active_positions[idx] = {
                    "trade_no": t_no, "type": "MCX_FUT", "instrument_key": inst_key, "strike": sym,
                    "side": side, "tx_side": tx_side, "display_side": tx_side,
                    "lots": 1, "lot_size": lot_size, "qty": qty, "pnl_units": pnl_mult,
                    "tick_size": tick_sz, "delta": 1.0, "entry_price": fill,
                    "fut_entry": fut_price, "fut_sl": sl_price, "r_unit": sl_points,
                    "est_charges": total_est_ch, "last_ltp": fill, "sl": sl_price,
                    "peak_gain": 0.0, "trail_stage": None, "is_expiry_day": False,
                    "setup_name": f"VWAP_{pattern_name}", "entry_time": entry_time_str,
                }
            self.subscribe_tick_instrument(inst_key)
            self.save_active_positions_state()
            self.render_live_dashboard(now_ist.strftime("%H:%M"))

    def execute_option_entry(self, idx, cfg, side, pattern_name, df_fut, fut_price, chain, atr14, now_ist, is_today_exp):
        with self.pos_lock:
            if idx in self.active_positions or len(self.active_positions) >= MAX_ACTIVE_TRADES_TOTAL:
                return

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

        swing_low, swing_high = find_latest_swing_levels(df_fut, lookback=10)
        buf = SWING_BUFFER_ATR * atr14
        if side == "CE":
            fut_sl = round(swing_low - buf, 2)
            fut_risk = max(fut_price - fut_sl, 0.5 * atr14)
        else:
            fut_sl = round(swing_high + buf, 2)
            fut_risk = max(fut_sl - fut_price, 0.5 * atr14)

        raw_delta = abs(float(greeks.get("delta", 0.0)))
        live_delta = min(0.75, max(0.35, raw_delta)) if raw_delta > 0 else 0.50
        raw_opt_sl = min(max(fut_risk * live_delta, ltp * 0.10), ltp * 0.35)
        sl_points = max(round_to_tick(raw_opt_sl, tick_sz), tick_sz * 4)
        sl_price = round_to_tick(max(tick_sz, ltp - sl_points), tick_sz)

        qty = min(int(cfg["lot_size"]), cfg["freeze_qty"])
        total_est_ch = fetch_exact_trade_charges(inst_key, ltp, ltp, qty, False).get("total", 65.0)

        order_res = place_marketable_limit_order(inst_key, qty, "BUY", ltp, tick_sz)
        if order_res.get("status") == "success":
            fill = order_res["fill_price"]
            self.trade_counter = max(self.trade_counter, len(self.closed_trades)) + 1
            t_no = self.trade_counter
            entry_time_str = now_ist.strftime("%H:%M:%S")
            with self.pos_lock:
                self.active_positions[idx] = {
                    "trade_no": t_no, "type": "INDEX_OPT", "instrument_key": inst_key,
                    "strike": target_strike, "side": side, "tx_side": "BUY",
                    "display_side": side, "lots": 1, "lot_size": int(cfg["lot_size"]),
                    "qty": qty, "pnl_units": qty, "tick_size": tick_sz,
                    "delta": live_delta, "entry_price": fill, "fut_entry": fut_price,
                    "fut_sl": fut_sl, "r_unit": sl_points, "est_charges": total_est_ch,
                    "last_ltp": fill, "sl": sl_price, "peak_gain": 0.0,
                    "trail_stage": None, "is_expiry_day": is_today_exp,
                    "setup_name": f"VWAP_PCR_{pattern_name}", "entry_time": entry_time_str,
                }
            self.subscribe_tick_instrument(inst_key)
            self.save_active_positions_state()
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
                exit_reason = "EOD_EXIT_1515"
            elif pos["type"] == "MCX_FUT" and hhmm >= MCX_EXIT_TIME:
                exit_reason = "MCX_EOD_EXIT"
            elif not is_short and ltp <= pos["sl"]:
                exit_reason = "SWING_TRAIL_SL" if pos.get("trail_stage") else "SWING_SL_HIT"
            elif is_short and ltp >= pos["sl"]:
                exit_reason = "SWING_TRAIL_SL" if pos.get("trail_stage") else "SWING_SL_HIT"

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
        if is_overnight_dead_hours(now_ist) or now_ist.weekday() >= 5:
            log.info(f"Outside market hours ({now_ist.strftime('%H:%M')} IST). Standing down cleanly.")
            self.release_single_instance_lock()
            return

        signal.signal(signal.SIGTERM, lambda s, f: setattr(self, "stop_requested", True))
        signal.signal(signal.SIGINT, lambda s, f: setattr(self, "stop_requested", True))

        self.load_today_state_from_journal()
        self.init_contracts()
        self.load_active_positions_state()
        self.bot_status_text = "BOT LIVE"
        session_start_ts = time.time()
        self.engine_boot_ts = session_start_ts

        notify(
            f"🌅 <b>V3 QUANT ENGINE • ONLINE</b>\n"
            f"────────────────────────────\n"
            f"🔑 <b>Token:</b> Active  |  ⚡ <b>Mode:</b> <code>{'PAPER' if PAPER_MODE else 'LIVE'}</code>\n"
            f"🧠 <b>Logic:</b> <code>PCR + FUT VWAP PATTERN (1m IDX / 5m MCX)</code>\n"
            f"🛡️ <b>Trail:</b> <code>SWING LOW/HIGH SL → 1:1 / 1:2 → ∞</code>"
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

            self.manage_open_positions(now_ist)

            if hhmm >= "23:35":
                self.shutdown_cleanly(now_ist, "EOD CLOSED")
                break

            for idx, cfg in INDICES.items():
                is_mcx = cfg["type"] == "MCX_FUT"
                if not is_mcx and hhmm > INDEX_EXIT_TIME:
                    if idx not in self.last_summaries:
                        self.last_summaries[idx] = {"idx": idx, "is_mcx": False, "spot": 0, "vwap": 0, "pcr": 0.0, "short_status": "CLOSED"}
                    continue
                res = self.evaluate_instrument(idx, cfg, now_ist)
                if res:
                    self.last_summaries[idx] = res

            if not self.dashboard_msg_id and self.last_summaries:
                self.render_live_dashboard(hhmm)

            time.sleep(8)


if __name__ == "__main__":
    engine = IntradayV3Engine()
    engine.run()
