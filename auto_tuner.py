import os
import csv
import json
import math
import logging
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [AUTO_TUNER] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("AUTO_TUNER")

UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GSHEET_WEBHOOK_URL = os.getenv("GSHEET_WEBHOOK_URL", "").strip()

JOURNAL_FILE = "live_journal/trade_journal.csv"
PARAMS_FILE = "live_journal/strategy_params.json"
MEMORY_FILE = "live_journal/market_memory.json"

SYMBOLS = {
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

DEFAULT_PARAMS = {
    "min_fuel_score": 45.0,
    "mcx_evening_fuel_score": 55.0,
    "mcx_max_risk_inr": 2000.0,
    "min_adx_trend": 18.0,
    "max_chop_index": 61.8,
    "symbol_overrides": {},
    "last_tuned": "",
}


def upstox_headers():
    return {"Accept": "application/json", "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}"}


def notify_telegram(msg: str):
    log.info(msg.replace("\n", " | "))
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


def push_tuner_to_google_sheet(metrics: dict):
    if not GSHEET_WEBHOOK_URL:
        return
    try:
        requests.post(GSHEET_WEBHOOK_URL, json={"action": "LOG_TUNER", "metrics": metrics}, timeout=8)
        log.info("[GSHEET] Synced nightly AI learning metrics to Google Sheets.")
    except Exception as e:
        log.warning(f"[GSHEET] Tuner sync warning: {e}")


def resolve_mcx_key(symbol: str) -> str:
    if not UPSTOX_ACCESS_TOKEN:
        return ""
    try:
        resp = requests.get(
            "https://api.upstox.com/v2/instruments/search",
            headers=upstox_headers(),
            params={"query": symbol, "exchanges": "MCX", "segments": "FUT", "expiry": "current_month", "records": 15},
            timeout=10,
        ).json()
        items = [x for x in resp.get("data", []) if str(x.get("trading_symbol", "")).upper().startswith(f"{symbol} FUT")]
        items.sort(key=lambda x: str(x.get("expiry", "9999-12-31")))
        return str(items[0].get("instrument_key", "")) if items else ""
    except Exception:
        return ""


def fetch_today_5m_candles(instrument_key: str) -> pd.DataFrame:
    if not instrument_key or not UPSTOX_ACCESS_TOKEN:
        return pd.DataFrame()
    enc = requests.utils.quote(instrument_key, safe="")
    url = f"https://api.upstox.com/v3/historical-candle/intraday/{enc}/minutes/5"
    try:
        candles = requests.get(url, headers=upstox_headers(), timeout=10).json().get("data", {}).get("candles", [])
        if not candles:
            return pd.DataFrame()
        df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        for c in ["open", "high", "low", "close", "volume", "oi"]:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        return df
    except Exception:
        return pd.DataFrame()


def compute_regime_metrics(df: pd.DataFrame) -> dict:
    if df.empty or len(df) < 15:
        return {"regime": "UNKNOWN", "adx": 20.0, "chop": 50.0, "day_range_atr": 2.5}

    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1).fillna(df["open"])
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr14 = tr.rolling(14, min_periods=1).mean()

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

    avg_atr = max(float(atr14.mean()), 1e-6)
    day_range_atr = float((high.max() - low.min()) / avg_atr)

    if adx < 18.0 or chop > 61.8 or day_range_atr < 2.2:
        regime = "SIDEWAYS"
    elif adx >= 25.0 and chop < 50.0:
        regime = "STRONG_TREND"
    else:
        regime = "MODERATE_TREND"

    return {"regime": regime, "adx": round(adx, 1), "chop": round(chop, 1), "day_range_atr": round(day_range_atr, 2)}


def correlate_trades_with_market(today_trades: list, market_dfs: dict, market_regimes: dict) -> tuple:
    memories = []
    moonshot_moves_count = 0
    sideways_losses_count = 0

    for t in today_trades:
        sym = t["symbol"]
        pnl = float(t["pnl"])
        side = t["side"]
        setup = t.get("setup", "")
        entry_t = t.get("entry_time", "09:20:00")
        reg = market_regimes.get(sym, {"regime": "MODERATE_TREND", "adx": 20.0, "chop": 50.0})

        df = market_dfs.get(sym, pd.DataFrame())
        max_potential_r = 2.0 if pnl > 0 else 0.0

        if not df.empty and len(df) >= 5:
            df_t = df.copy()
            df_t["hhmmss"] = df_t["timestamp"].dt.strftime("%H:%M:%S")
            after_entry = df_t[df_t["hhmmss"] >= entry_t].head(20)
            if not after_entry.empty:
                spot_in = float(after_entry["open"].iloc[0])
                atr_est = max(float((df["high"] - df["low"]).rolling(14, min_periods=1).mean().iloc[-1]), 1e-6)
                max_fav = (float(after_entry["high"].max()) - spot_in) if side in ("BUY", "CE") else (spot_in - float(after_entry["low"].min()))
                max_potential_r = round(max(0.0, max_fav / atr_est), 2)

        if pnl <= 0 and reg["regime"] == "SIDEWAYS":
            outcome = "SIDEWAYS_TRAP"
            sideways_losses_count += 1
        elif max_potential_r >= 4.0:
            outcome = "EXTENDED_TREND_MISSED"
            moonshot_moves_count += 1
        elif pnl > 0:
            outcome = "CLEAN_WIN"
        else:
            outcome = "NORMAL_SL"

        session_tag = "MCX_EVENING" if (sym not in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX") and entry_t >= "15:30:00") else "DAY_SESSION"
        memories.append({
            "date": t["date"], "symbol": sym, "session": session_tag, "side": side,
            "setup": setup, "regime": reg["regime"], "adx": reg["adx"], "chop": reg["chop"],
            "pnl": pnl, "max_potential_r": max_potential_r, "outcome": outcome,
        })

    return memories, moonshot_moves_count, sideways_losses_count


def run_nightly_auto_tuner():
    os.makedirs("live_journal", exist_ok=True)
    now_ist = datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")

    params = dict(DEFAULT_PARAMS)
    if os.path.isfile(PARAMS_FILE):
        try:
            with open(PARAMS_FILE, "r", encoding="utf-8") as f:
                params.update(json.load(f))
        except Exception:
            pass

    all_trades = []
    if os.path.isfile(JOURNAL_FILE):
        with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    r["pnl"] = float(r.get("pnl", 0.0))
                    all_trades.append(r)
                except Exception:
                    continue

    today_trades = [t for t in all_trades if t.get("date") == today_str]

    market_dfs, market_regimes = {}, {}
    for sym, cfg in SYMBOLS.items():
        key = cfg.get("spot_key") or resolve_mcx_key(sym)
        df = fetch_today_5m_candles(key)
        market_dfs[sym] = df
        market_regimes[sym] = compute_regime_metrics(df)

    new_memories, big_moves, sideways_traps = correlate_trades_with_market(today_trades, market_dfs, market_regimes)

    existing_memories = []
    if os.path.isfile(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                existing_memories = json.load(f)
        except Exception:
            pass

    updated_memories = (existing_memories + new_memories)[-300:]
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(updated_memories, f, indent=2)

    tuning_notes = []
    if sideways_traps >= 2:
        params["min_adx_trend"] = min(24.0, round(params.get("min_adx_trend", 18.0) + 1.5, 1))
        params["max_chop_index"] = max(55.0, round(params.get("max_chop_index", 61.8) - 1.5, 1))
        tuning_notes.append(f"Sideways Traps ({sideways_traps}) → Tightened ADX ≥ {params['min_adx_trend']} & Chop ≤ {params['max_chop_index']}")
    elif sideways_traps == 0 and len(today_trades) >= 3:
        params["min_adx_trend"] = max(17.0, round(params.get("min_adx_trend", 18.0) - 0.5, 1))

    overrides = params.get("symbol_overrides", {})
    for sym in SYMBOLS:
        sym_hist = [t for t in all_trades if t.get("symbol") == sym][-12:]
        reg_today = market_regimes.get(sym, {}).get("regime", "UNKNOWN")
        if len(sym_hist) >= 3:
            wins = sum(1 for x in sym_hist if x["pnl"] > 0)
            wr = (wins / len(sym_hist)) * 100.0
            net_sym = sum(x["pnl"] for x in sym_hist)
            if wr < 25.0 and net_sym < -2500:
                overrides[sym] = {"min_fuel": 62.0, "cooldown": True}
                tuning_notes.append(f"{sym}: Low WR ({wr:.0f}%, ₹{net_sym:+.0f}) → Raised Fuel filter to 62")
            else:
                overrides[sym] = {"min_fuel": 50.0 if reg_today == "SIDEWAYS" else 45.0, "cooldown": False}

    mcx_recent = [t for t in all_trades if t.get("symbol") in ("CRUDEOIL", "NATURALGAS", "GOLD", "SILVER", "COPPER")][-10:]
    if len(mcx_recent) >= 3:
        mcx_net = sum(x["pnl"] for x in mcx_recent)
        if mcx_net < -3000:
            params["mcx_max_risk_inr"] = max(1200.0, round(params.get("mcx_max_risk_inr", 2000.0) - 250.0, 0))
            params["mcx_evening_fuel_score"] = min(65.0, round(params.get("mcx_evening_fuel_score", 55.0) + 2.5, 1))
            tuning_notes.append(f"MCX Drawdown Guard → Risk Cap ₹{params['mcx_max_risk_inr']:.0f} & Eve Fuel ≥ {params['mcx_evening_fuel_score']}")
        elif mcx_net > 3000:
            params["mcx_max_risk_inr"] = min(2800.0, round(params.get("mcx_max_risk_inr", 2000.0) + 200.0, 0))
            tuning_notes.append(f"MCX Profit Scaling → Risk Cap ₹{params['mcx_max_risk_inr']:.0f}")

    params["symbol_overrides"] = overrides
    params["last_tuned"] = now_ist.strftime("%Y-%m-%d %H:%M IST")
    with open(PARAMS_FILE, "w", encoding="utf-8") as f:
        json.dump(params, f, indent=2)

    notes_str = " | ".join(tuning_notes) if tuning_notes else "All parameters optimal"
    push_tuner_to_google_sheet({
        "date": today_str,
        "trades_audited": len(today_trades),
        "sideways_traps": sideways_traps,
        "big_moves": big_moves,
        "memory_count": len(updated_memories),
        "adx_gate": params["min_adx_trend"],
        "mcx_risk_cap": params["mcx_max_risk_inr"],
        "notes": notes_str,
    })

    regime_summary = " │ ".join([f"{k}:{v['regime'][:4]}(ADX {v['adx']:.0f})" for k, v in list(market_regimes.items())[:5]])
    notes_block = "\n".join([f"  • {n}" for n in tuning_notes]) if tuning_notes else "  • All parameters optimal; no major drift detected today."

    report = (
        f"🧠 <b>V3 AI AUTO-TUNER • NIGHTLY EVOLUTION ({today_str})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Today Audited:</b> {len(today_trades)} Trades │ Sideways Traps: {sideways_traps} │ 4R+ Moves: {big_moves}\n"
        f"🧬 <b>Market Memory Vault:</b> {len(updated_memories)} experiences │ Synced to Google Sheets ✅\n"
        f"📡 <b>Index Regimes:</b> <code>{regime_summary}</code>\n"
        f"⚙️ <b>Self-Calibrated Rules for Tomorrow:</b>\n{notes_block}"
    )
    notify_telegram(report)


if __name__ == "__main__":
    run_nightly_auto_tuner()
