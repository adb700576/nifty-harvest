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
MEMORY_FILE = "live_journal/market_memory.json"
GSHEET_WEBHOOK_URL = os.getenv("GSHEET_WEBHOOK_URL", "").strip()


def run_nightly_calibration():
    os.makedirs("live_journal", exist_ok=True)
    params = {
        "driver_gear": 3,
        "min_fuel_score": 45.0,
        "mcx_evening_fuel_score": 55.0,
        "mcx_max_risk_inr": 2000.0,
        "min_adx_trend": 18.0,
        "max_chop_index": 61.8,
        "trail_cushion_mult": 1.0,
        "sl_buffer_mult": 0.12,
        "scalp_target_r": 1.35,
        "symbol_overrides": {},
    }
    if os.path.isfile(PARAMS_FILE):
        try:
            with open(PARAMS_FILE, "r", encoding="utf-8") as f:
                params.update(json.load(f))
        except Exception:
            pass

    if not os.path.isfile(JOURNAL_FILE):
        log.info("No trade journal yet. Keeping baseline parameters.")
        return

    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    today_trades = []
    with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("date") == today_str:
                today_trades.append(row)

    if not today_trades:
        log.info("Zero trades today. No parameter adjustments needed.")
        return

    net_pnl = sum(float(t.get("pnl", 0.0)) for t in today_trades)
    wins = sum(1 for t in today_trades if float(t.get("pnl", 0.0)) > 0)
    win_rate = (wins / len(today_trades)) * 100.0

    gear = int(params.get("driver_gear", 3))
    if net_pnl > 0 and win_rate >= 60.0:
        gear = min(5, gear + 1)
    elif net_pnl < 0 or win_rate < 40.0:
        gear = max(1, gear - 1)
    params["driver_gear"] = gear

    with open(PARAMS_FILE, "w", encoding="utf-8") as f:
        json.dump(params, f, indent=2)

    log.info(f"Nightly calibration saved silently: Gear {gear}/5 | Day Net ₹{net_pnl:+.2f} | Win Rate {win_rate:.1f}%")

    if GSHEET_WEBHOOK_URL:
        try:
            requests.post(
                GSHEET_WEBHOOK_URL,
                json={
                    "action": "LOG_LEARNING",
                    "date": today_str,
                    "gear": gear,
                    "day_pnl": round(net_pnl, 2),
                    "win_rate": round(win_rate, 1),
                },
                timeout=8,
            )
        except Exception:
            pass


if __name__ == "__main__":
    run_nightly_calibration()
