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
    "driver_gear": 3,                 # Progressive Gear 1..5 (3=Balanced, 4=Confident, 5=Pro Highway/Drift)
    "min_fuel_score": 45.0,
    "mcx_evening_fuel_score": 55.0,
    "mcx_max_risk_inr": 2000.0,
    "min_adx_trend": 18.0,
    "max_chop_index": 61.8,
    "trail_cushion_mult": 1.0,        # Auto-widens if early exits miss 120 -> 250 rallies
    "sl_buffer_mult": 0.12,           # Auto-widens if SL hits are proved too tight
    "scalp_target_r": 1.35,           # Quick target in Sideways City Traffic Mode
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


def classify_day_behavior(df: pd.DataFrame) -> dict:
    """
    Explains WHY today moved in one direction (Highway) vs stayed Sideways (City Traffic).
    """
    if df.empty or len(df) < 15:
        return {"regime": "UNKNOWN", "adx": 20.0, "chop": 50.0, "day_range_atr": 2.5, "cause": "Insufficient intraday bars"}

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
    day_range = float(high.max() - low.min())
    day_range_atr = day_range / avg_atr
    net_move = float(close.iloc[-1] - df["open"].iloc[0])
    directional_efficiency = abs(net_move) / max(day_range, 1e-6)

    if directional_efficiency >= 0.55 and day_range_atr >= 3.2 and adx >= 22.0:
        regime = "ONE_DIRECTIONAL_TREND"
        dir_word = "BULLISH" if net_move > 0 else "BEARISH"
        cause = f"HIGHWAY {dir_word}: Efficiency {directional_efficiency:.0%}, Range {day_range_atr:.1f}x ATR, ADX {adx:.0f}"
    elif adx < 18.0 or chop > 61.8 or day_range_atr < 2.2:
        regime = "SIDEWAYS"
        cause = f"CITY TRAFFIC: Low Efficiency {directional_efficiency:.0%}, Chop {chop:.0f}, ADX {adx:.0f}"
    else:
        regime = "MODERATE_TREND"
        cause = f"MIXED ROADS: Efficiency {directional_efficiency:.0%}, ADX {adx:.0f}"

    return {
        "regime": regime,
        "adx": round(adx, 1),
        "chop": round(chop, 1),
        "day_range_atr": round(day_range_atr, 2),
        "efficiency": round(directional_efficiency, 2),
        "cause": cause,
    }


def deep_post_mortem_trades(today_trades: list, market_dfs: dict, market_regimes: dict) -> dict:
    """
    Performs Causal Post-Mortem on every trade:
    1. EARLY_EXIT_MISSED_RALLY: e.g., Bought 100, exited 120, later rallied to 250 -> Trail too tight!
    2. SL_TOO_TIGHT: SL hit, then market reversed in our favor for 2.5R+ -> Entry right, SL too close!
    3. WRONG_ENTRY_AGAINST_TREND: SL hit and market kept moving against us -> Bad entry!
    4. SIDEWAYS_TRAP: Lost trying to chase trend on a chop day -> Should have used Scalp Mode!
    """
    memories = []
    early_exit_count = 0
    sl_too_tight_count = 0
    wrong_entry_count = 0
    sideways_trap_count = 0
    clean_win_count = 0

    for t in today_trades:
        sym = t["symbol"]
        pnl = float(t["pnl"])
        side = t["side"]
        setup = t.get("setup", "")
        entry_t = t.get("entry_time", "09:20:00")
        exit_t = t.get("exit_time", "15:15:00")
        reg = market_regimes.get(sym, {"regime": "MODERATE_TREND", "adx": 20.0, "chop": 50.0})
        df = market_dfs.get(sym, pd.DataFrame())

        post_exit_r = 0.0
        post_sl_favorable_r = 0.0

        if not df.empty and len(df) >= 5:
            df_t = df.copy()
            df_t["hhmmss"] = df_t["timestamp"].dt.strftime("%H:%M:%S")
            atr_est = max(float((df["high"] - df["low"]).rolling(14, min_periods=1).mean().iloc[-1]), 1e-6)

            after_exit = df_t[df_t["hhmmss"] >= exit_t].head(24)
            if not after_exit.empty:
                spot_at_exit = float(after_exit["open"].iloc[0])
                if side in ("BUY", "CE"):
                    further_fav = float(after_exit["high"].max()) - spot_at_exit
                else:
                    further_fav = spot_at_exit - float(after_exit["low"].min())
                post_exit_r = round(max(0.0, further_fav / atr_est), 2)

            after_entry = df_t[df_t["hhmmss"] >= entry_t].head(20)
            if not after_entry.empty:
                spot_at_entry = float(after_entry["open"].iloc[0])
                if side in ("BUY", "CE"):
                    total_fav = float(after_entry["high"].max()) - spot_at_entry
                else:
                    total_fav = spot_at_entry - float(after_entry["low"].min())
                post_sl_favorable_r = round(max(0.0, total_fav / atr_est), 2)

        if pnl > 0 and post_exit_r >= 2.5:
            root_cause = "EARLY_EXIT_MISSED_RALLY"
            lesson = f"Exited with profit, but market continued +{post_exit_r:.1f}R after exit -> Widen Trail Cushion"
            early_exit_count += 1
        elif pnl <= 0 and post_sl_favorable_r >= 2.5:
            root_cause = "SL_TOO_TIGHT"
            lesson = f"SL hit, but price later moved +{post_sl_favorable_r:.1f}R in trade direction -> Widen SL Buffer"
            sl_too_tight_count += 1
        elif pnl <= 0 and reg["regime"] == "SIDEWAYS":
            root_cause = "SIDEWAYS_TRAP"
            lesson = "Entered trend setup in choppy range -> Switch to City Scalp Mode (1.35R)"
            sideways_trap_count += 1
        elif pnl <= 0:
            root_cause = "WRONG_ENTRY_AGAINST_TREND"
            lesson = "Price never moved in favor after entry -> Tighten Fuel/Memory filter on this setup"
            wrong_entry_count += 1
        else:
            root_cause = "CLEAN_EXECUTION"
            lesson = "Captured move cleanly without premature exit"
            clean_win_count += 1

        session_tag = "MCX_EVENING" if (sym not in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX") and entry_t >= "15:30:00") else "DAY_SESSION"
        memories.append({
            "date": t["date"],
            "symbol": sym,
            "session": session_tag,
            "side": side,
            "setup": setup,
            "regime": reg["regime"],
            "adx": reg["adx"],
            "chop": reg["chop"],
            "pnl": pnl,
            "post_exit_r": post_exit_r,
            "outcome": root_cause,
            "lesson": lesson,
        })

    return {
        "memories": memories,
        "early_exits": early_exit_count,
        "sl_too_tight": sl_too_tight_count,
        "wrong_entries": wrong_entry_count,
        "sideways_traps": sideways_trap_count,
        "clean_wins": clean_win_count,
    }


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
        market_regimes[sym] = classify_day_behavior(df)

    forensics = deep_post_mortem_trades(today_trades, market_dfs, market_regimes)

    existing_memories = []
    if os.path.isfile(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                existing_memories = json.load(f)
        except Exception:
            pass

    updated_memories = (existing_memories + forensics["memories"])[-300:]
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(updated_memories, f, indent=2)

    tuning_notes = []

    # 1. Early Exit Calibration (120 -> 250 pullback survival)
    if forensics["early_exits"] >= 1:
        params["trail_cushion_mult"] = min(1.65, round(params.get("trail_cushion_mult", 1.0) + 0.15, 2))
        tuning_notes.append(
            f"Early Exits ({forensics['early_exits']}) missed post-exit rallies -> Trail Cushion widened to {params['trail_cushion_mult']}x"
        )

    # 2. SL Tightness vs Wrong Entry Calibration
    if forensics["sl_too_tight"] >= 1:
        params["sl_buffer_mult"] = min(0.22, round(params.get("sl_buffer_mult", 0.12) + 0.02, 2))
        tuning_notes.append(
            f"SL Too Tight ({forensics['sl_too_tight']}) -> Swing SL Buffer widened to {params['sl_buffer_mult']} ATR"
        )
    if forensics["wrong_entries"] >= 2:
        params["min_fuel_score"] = min(58.0, round(params.get("min_fuel_score", 45.0) + 2.5, 1))
        tuning_notes.append(
            f"Wrong Entries ({forensics['wrong_entries']}) -> Raised Base Fuel Gate to {params['min_fuel_score']}"
        )

    # 3. Sideways Regime Calibration
    if forensics["sideways_traps"] >= 1:
        params["min_adx_trend"] = min(23.0, round(params.get("min_adx_trend", 18.0) + 1.0, 1))
        tuning_notes.append(
            f"Sideways Traps ({forensics['sideways_traps']}) -> Trend ADX Gate ≥ {params['min_adx_trend']} (below uses City Scalp Mode)"
        )

    # 4. Progressive Driver Skill Gear (1 -> 5) based on Rolling 15-Trade Performance
    recent_15 = all_trades[-15:]
    curr_gear = int(params.get("driver_gear", 3))
    if len(recent_15) >= 5:
        r_wins = sum(1 for x in recent_15 if x["pnl"] > 0)
        r_wr = (r_wins / len(recent_15)) * 100.0
        r_net = sum(x["pnl"] for x in recent_15)
        if r_wr >= 55.0 and r_net > 3000 and curr_gear < 5:
            curr_gear += 1
            tuning_notes.append(f"Driver Skill Promoted -> Gear {curr_gear}/5 (WR {r_wr:.0f}%, Net ₹{r_net:+.0f})")
        elif (r_wr < 30.0 or r_net < -3500) and curr_gear > 1:
            curr_gear -= 1
            tuning_notes.append(f"Driver Safety Downshift -> Gear {curr_gear}/5 (Protecting capital)")
    params["driver_gear"] = curr_gear

    # 5. Per-Symbol Overrides
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
            else:
                overrides[sym] = {"min_fuel": 48.0 if reg_today == "SIDEWAYS" else 45.0, "cooldown": False}

    params["symbol_overrides"] = overrides
    params["last_tuned"] = now_ist.strftime("%Y-%m-%d %H:%M IST")
    with open(PARAMS_FILE, "w", encoding="utf-8") as f:
        json.dump(params, f, indent=2)

    notes_str = " | ".join(tuning_notes) if tuning_notes else f"Gear {curr_gear}/5 steady; parameters calibrated"
    push_tuner_to_google_sheet({
        "date": today_str,
        "trades_audited": len(today_trades),
        "sideways_traps": forensics["sideways_traps"],
        "big_moves": forensics["early_exits"] + forensics["clean_wins"],
        "memory_count": len(updated_memories),
        "adx_gate": params["min_adx_trend"],
        "mcx_risk_cap": params["mcx_max_risk_inr"],
        "notes": f"[Gear {curr_gear}/5] {notes_str}",
    })

    nifty_cause = market_regimes.get("NIFTY", {}).get("cause", "N/A")
    notes_block = "\n".join([f"  • {n}" for n in tuning_notes]) if tuning_notes else f"  • Driver Gear {curr_gear}/5 steady; no drift detected."

    report = (
        f"🧠 <b>V3 SELF-MATURING COACH • NIGHTLY FORENSICS ({today_str})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏎️ <b>Driver Skill Level:</b> <b>Gear {curr_gear}/5</b> │ Trail Cushion: <b>{params['trail_cushion_mult']}x</b> │ SL Buffer: <b>{params['sl_buffer_mult']}</b>\n"
        f"🔍 <b>Audit:</b> {len(today_trades)} Trades │ Early Exits: {forensics['early_exits']} │ Tight SL: {forensics['sl_too_tight']} │ Bad Entry: {forensics['wrong_entries']}\n"
        f"🧭 <b>NIFTY Day Behavior:</b> <code>{nifty_cause}</code>\n"
        f"⚙️ <b>Self-Learned Upgrades for Tomorrow:</b>\n{notes_block}"
    )
    notify_telegram(report)


if __name__ == "__main__":
    run_nightly_auto_tuner()
