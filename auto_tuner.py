import os
import csv
import json
import logging
import requests
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [AUTO_TUNER] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("AUTO_TUNER")

JOURNAL_FILE = "live_journal/trade_journal.csv"
PARAMS_FILE = "live_journal/strategy_params.json"
GSHEET_WEBHOOK_URL = os.getenv("GSHEET_WEBHOOK_URL", "").strip()


def run_nightly_calibration():
    os.makedirs("live_journal", exist_ok=True)
    capital = float(os.getenv("CAPITAL_PER_TRADE", "25000.0"))

    # Autonomous Risk & Swing Rules (Self-calibrated for 20k-30k retail capital vs institutional flow)
    params = {
        "driver_gear": 3,
        "max_daily_loss_inr": round(capital * 0.15, 2),       # Hard 15% daily capital shield (e.g. ₹3,750 on ₹25k)
        "index_max_risk_inr": round(capital * 0.05, 2),       # Hard 5% per-trade index cap (e.g. ₹1,250)
        "mcx_max_risk_inr": min(1500.0, round(capital * 0.06, 2)),  # Hard ₹1,500 per-trade MCX cap
        "min_fuel_score": 48.0,
        "mcx_evening_fuel_score": 56.0,
        "min_adx_trend": 19.0,
        "max_chop_index": 60.0,
        "swing_lookback_candles": 75,                         # Analyze last 75 candles before calculating swing SL
        "trail_cushion_mult": 1.0,
        "sl_buffer_mult": 0.12,
        "scalp_target_r": 1.35,
        "symbol_overrides": {},
        "last_lesson": "Autonomous Capital Shield Active",
    }
    if os.path.isfile(PARAMS_FILE):
        try:
            with open(PARAMS_FILE, "r", encoding="utf-8") as f:
                params.update(json.load(f))
        except Exception:
            pass

    if not os.path.isfile(JOURNAL_FILE):
        with open(PARAMS_FILE, "w", encoding="utf-8") as f:
            json.dump(params, f, indent=2)
        log.info("Initialized autonomous risk & swing parameters.")
        return

    all_trades = []
    with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                row["pnl_val"] = float(row.get("pnl", 0.0))
                all_trades.append(row)
            except Exception:
                continue

    if not all_trades:
        return

    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    today_trades = [t for t in all_trades if t.get("date") == today_str]
    recent_10 = all_trades[-10:]

    # 1. Rolling 10-Trade Profit Factor & Win Rate
    gross_win = sum(t["pnl_val"] for t in recent_10 if t["pnl_val"] > 0)
    gross_loss = abs(sum(t["pnl_val"] for t in recent_10 if t["pnl_val"] < 0))
    profit_factor = round(gross_win / max(gross_loss, 1.0), 2)
    wins_10 = sum(1 for t in recent_10 if t["pnl_val"] > 0)
    wr_10 = round((wins_10 / len(recent_10)) * 100.0, 1)

    old_gear = int(params.get("driver_gear", 3))
    new_gear = old_gear
    lesson = f"Steady Cruise (PF {profit_factor}, WR {wr_10}%)"

    day_net = round(sum(t["pnl_val"] for t in today_trades), 2)
    daily_limit = float(params.get("max_daily_loss_inr", round(capital * 0.15, 2)))

    # 2. Consecutive Loss & Major Accident Check
    consec_losses_today = 0
    max_consec_today = 0
    for t in today_trades:
        if t["pnl_val"] < 0:
            consec_losses_today += 1
            max_consec_today = max(max_consec_today, consec_losses_today)
        else:
            consec_losses_today = 0

    major_accident = (day_net <= -daily_limit) or (max_consec_today >= 3)

    if major_accident:
        new_gear = min(2, max(1, old_gear - 2))
        params["index_max_risk_inr"] = max(800.0, round(float(params["index_max_risk_inr"]) * 0.80, 2))
        params["mcx_max_risk_inr"] = max(900.0, round(float(params["mcx_max_risk_inr"]) * 0.80, 2))
        lesson = f"SHIELD TRIGGERED -> Gear {old_gear}->{new_gear} | Risk tightened (Day ₹{day_net:+.0f})"
    elif len(recent_10) >= 5:
        if wr_10 >= 65.0 and profit_factor >= 2.2:
            new_gear = min(5, old_gear + 1)
            params["index_max_risk_inr"] = min(round(capital * 0.05, 2), round(float(params["index_max_risk_inr"]) * 1.10, 2))
            params["mcx_max_risk_inr"] = min(1500.0, round(float(params["mcx_max_risk_inr"]) * 1.10, 2))
            lesson = f"High Precision -> Upshift Gear {new_gear} (PF {profit_factor})"
        elif wr_10 >= 55.0 and profit_factor >= 1.6 and old_gear < 4:
            new_gear = 4
            lesson = f"Strong Trend Capture -> Upshift Gear 4 (PF {profit_factor})"
        elif wr_10 < 35.0 or profit_factor < 0.75:
            new_gear = max(1, old_gear - 1)
            params["index_max_risk_inr"] = max(800.0, round(float(params["index_max_risk_inr"]) * 0.85, 2))
            params["mcx_max_risk_inr"] = max(900.0, round(float(params["mcx_max_risk_inr"]) * 0.85, 2))
            lesson = f"Drawdown Shield -> Downshift Gear {new_gear} (PF {profit_factor})"

    params["driver_gear"] = new_gear
    params["trail_cushion_mult"] = 1.10 if new_gear >= 4 else (0.90 if new_gear <= 2 else 1.0)

    # 3. Symbol-Specific Memory & Cooldown
    sym_ov = params.get("symbol_overrides", {})
    for sym in set(t.get("symbol", "") for t in today_trades if t.get("symbol")):
        sym_today_pnl = sum(t["pnl_val"] for t in today_trades if t.get("symbol") == sym)
        ov = sym_ov.get(sym, {"min_fuel": 48.0, "cooldown": False})
        if sym_today_pnl < -1000:
            ov["cooldown"] = True
            ov["min_fuel"] = min(65.0, float(ov.get("min_fuel", 48.0)) + 5.0)
            lesson += f" | {sym} Cooldown (Fuel {ov['min_fuel']}+)"
        elif sym_today_pnl > 1000:
            ov["cooldown"] = False
            ov["min_fuel"] = max(48.0, float(ov.get("min_fuel", 48.0)) - 2.0)
        sym_ov[sym] = ov

    params["symbol_overrides"] = sym_ov
    params["last_lesson"] = lesson

    with open(PARAMS_FILE, "w", encoding="utf-8") as f:
        json.dump(params, f, indent=2)

    log.info(f"[AUTONOMOUS] Gear {old_gear}->{new_gear} | {lesson}")

    if GSHEET_WEBHOOK_URL:
        try:
            requests.post(
                GSHEET_WEBHOOK_URL,
                json={
                    "action": "LOG_LEARNING",
                    "date": today_str,
                    "gear": new_gear,
                    "day_pnl": day_net,
                    "win_rate": wr_10,
                    "profit_factor": profit_factor,
                    "lesson": lesson,
                },
                timeout=8,
            )
        except Exception:
            pass


if __name__ == "__main__":
    run_nightly_calibration()
