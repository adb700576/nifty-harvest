import os
import csv
import json
import logging
import requests
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AUTO_TUNER_BRAIN] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("AUTO_TUNER")

JOURNAL_FILE = "live_journal/trade_journal.csv"
PARAMS_FILE = "live_journal/strategy_params.json"
EVENT_CALENDAR_FILE = "live_journal/event_calendar.json"
LEARNED_HOLIDAYS_FILE = "live_journal/learned_holidays.json"
GSHEET_WEBHOOK_URL = os.getenv("GSHEET_WEBHOOK_URL", "").strip()


def load_learned_holidays() -> set:
    if os.path.isfile(LEARNED_HOLIDAYS_FILE):
        try:
            with open(LEARNED_HOLIDAYS_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def record_learned_holiday(date_str: str):
    holidays = load_learned_holidays()
    if date_str not in holidays:
        holidays.add(date_str)
        os.makedirs("live_journal", exist_ok=True)
        with open(LEARNED_HOLIDAYS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(holidays), f, indent=2)


def get_market_closed_reason(dt: datetime, upstox_market_status: str = "") -> str:
    """
    100% Zero-Hardcode Market Closed Detector:
    - Catches Saturdays & Sundays forever via weekday math.
    - Self-learns any weekday Exchange Holiday dynamically from broker status.
    """
    if dt.weekday() == 5:
        return "SATURDAY (WEEKEND)"
    if dt.weekday() == 6:
        return "SUNDAY (WEEKEND)"

    d_str = dt.strftime("%Y-%m-%d")
    if d_str in load_learned_holidays():
        return "NSE HOLIDAY (SELF-LEARNED)"

    if upstox_market_status.strip().upper() == "CLOSED":
        record_learned_holiday(d_str)
        return "NSE HOLIDAY (AUTO-DETECTED)"

    return ""


def detect_expiry_for_date(target_dt: datetime, nearest_option_expiry_str: str = "") -> bool:
    """Matches target date directly against broker's live NFO nearest option expiry contract."""
    if nearest_option_expiry_str:
        return nearest_option_expiry_str.strip()[:10] == target_dt.strftime("%Y-%m-%d")
    return False


def get_next_trading_day(now_ist: datetime) -> datetime:
    """Returns next weekday trading session, skipping weekends and self-learned holidays."""
    learned_holidays = load_learned_holidays()
    candidate = now_ist + timedelta(days=1)
    while candidate.weekday() >= 5 or candidate.strftime("%Y-%m-%d") in learned_holidays:
        candidate += timedelta(days=1)
    return candidate


def check_scheduled_event(target_date_str: str) -> tuple[bool, str]:
    if os.path.isfile(EVENT_CALENDAR_FILE):
        try:
            with open(EVENT_CALENDAR_FILE, "r", encoding="utf-8") as f:
                events = json.load(f)
            if target_date_str in events:
                return True, str(events[target_date_str])
        except Exception:
            pass
    return False, ""


def apply_event_day_shield(
    params: dict,
    capital: float,
    is_scheduled_event: bool = False,
    event_name: str = "",
    vix_change_pct: float = 0.0,
) -> dict:
    if is_scheduled_event or vix_change_pct >= 15.0:
        params["event_mode"] = True
        params["event_label"] = event_name or f"VIX_SPIKE_{vix_change_pct:+.1f}%"
        params["morning_wait_until"] = "09:45"
        min_floor = round(capital * 0.025, 2)
        params["index_max_risk_inr"] = max(min_floor, round(float(params["index_max_risk_inr"]) * 0.60, 2))
        params["min_fuel_score"] = max(float(params.get("min_fuel_score", 48.0)), 55.0)
    else:
        params["event_mode"] = False
        params["event_label"] = ""
        params["morning_wait_until"] = "09:30"
    return params


def upgrade_for_iv_crush_and_low_delta(params: dict, today_trades: list[dict]) -> tuple[dict, str]:
    iv_crush_count = sum(
        1
        for t in today_trades
        if t.get("exit_reason") in ("NO_MOMENTUM_EXIT", "IV_CRUSH_EXIT")
        and float(t.get("pnl", 0.0)) <= 0
    )

    if iv_crush_count >= 2:
        params["strike_offset"] = -1
        params["expiry_strike_offset"] = 0
        params["no_momentum_minutes"] = 10
        params["min_fuel_score"] = min(62.0, float(params.get("min_fuel_score", 48.0)) + 4.0)
        return params, f" | IV-Crush Upgrade ({iv_crush_count} stalls -> ITM/ATM + 10m Timer)"

    params.setdefault("strike_offset", 0)
    params.setdefault("no_momentum_minutes", 15)
    return params, ""


def calibrate_options_chain_brain(params: dict, today_trades: list[dict]) -> tuple[dict, str]:
    params.setdefault("min_option_delta", 0.45)
    params.setdefault("expiry_slope_min_delta", 0.30)
    params.setdefault("max_theta_decay_pct", 8.0)
    params.setdefault("max_iv_spike_pct", 25.0)
    params.setdefault("require_oi_short_covering", True)
    params.setdefault("bullish_pcr_min", 1.15)
    params.setdefault("bearish_pcr_max", 0.85)

    greek_bleed_losses = sum(
        1
        for t in today_trades
        if t.get("exit_reason") in ("NO_MOMENTUM_EXIT", "IV_CRUSH_EXIT", "OI_WALL_REJECT")
        and float(t.get("pnl", 0.0)) < 0
    )

    if greek_bleed_losses >= 2:
        params["min_option_delta"] = min(0.60, round(float(params["min_option_delta"]) + 0.05, 2))
        params["max_theta_decay_pct"] = max(5.0, round(float(params["max_theta_decay_pct"]) - 1.0, 1))
        params["require_oi_short_covering"] = True
        return (
            params,
            f" | Greeks/OI Upgrade (MinDelta={params['min_option_delta']}, MaxTheta={params['max_theta_decay_pct']}%)",
        )

    return params, ""


def run_nightly_calibration(nearest_option_expiry_str: str = "", vix_change_pct: float = 0.0):
    os.makedirs("live_journal", exist_ok=True)
    existing = {}
    if os.path.isfile(PARAMS_FILE):
        try:
            with open(PARAMS_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass

    capital = float(os.getenv("CAPITAL_PER_TRADE") or existing.get("capital_per_trade", 25000.0))
    mcx_capital = float(os.getenv("MCX_CAPITAL_PER_TRADE") or existing.get("mcx_capital_per_trade", 150000.0))

    now_ist = datetime.now(IST)
    next_session_dt = get_next_trading_day(now_ist)
    next_session_str = next_session_dt.strftime("%Y-%m-%d")

    params = {
        "brain_version": now_ist.strftime("v%Y.%m.%d.%H%M"),
        "target_session_date": next_session_str,
        "is_expiry_day": detect_expiry_for_date(next_session_dt, nearest_option_expiry_str),
        "driver_gear": 3,
        "capital_per_trade": capital,
        "mcx_capital_per_trade": mcx_capital,
        "max_daily_loss_inr": round(capital * 0.15, 2),
        "index_max_risk_inr": round(capital * 0.05, 2),
        "mcx_max_risk_inr": round(mcx_capital * 0.01, 2),
        "min_fuel_score": 48.0,
        "expiry_slope_fuel_score": 32.0,
        "expiry_slope_start": "13:45",
        "expiry_slope_end": "14:45",
        "expiry_strike_offset": 1,
        "strike_offset": 0,
        "mcx_evening_fuel_score": 56.0,
        "min_adx_trend": 19.0,
        "max_chop_index": 60.0,
        "swing_lookback_candles": 75,
        "atr_sl_buffer_mult": 0.12,
        "trail_cushion_mult": 1.0,
        "scalp_target_r": 1.35,
        "no_momentum_minutes": 15,
        "morning_wait_until": "09:30",
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
        "last_lesson": "Central Brain Initialized",
    }
    params.update(existing)

    params["brain_version"] = now_ist.strftime("v%Y.%m.%d.%H%M")
    params["target_session_date"] = next_session_str
    params["is_expiry_day"] = detect_expiry_for_date(next_session_dt, nearest_option_expiry_str)
    params["capital_per_trade"] = capital
    params["mcx_capital_per_trade"] = mcx_capital
    params["index_max_risk_inr"] = round(capital * 0.05, 2)
    params["mcx_max_risk_inr"] = round(mcx_capital * 0.01, 2)
    params["max_daily_loss_inr"] = round(capital * 0.15, 2)

    all_trades = []
    if os.path.isfile(JOURNAL_FILE):
        with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    row["pnl_val"] = float(row.get("pnl", 0.0))
                    all_trades.append(row)
                except Exception:
                    continue

    today_str = now_ist.strftime("%Y-%m-%d")
    today_trades = [t for t in all_trades if t.get("date") == today_str]
    recent_10 = all_trades[-10:]

    old_gear = int(params.get("driver_gear", 3))
    new_gear = old_gear
    day_net = round(sum(t["pnl_val"] for t in today_trades), 2)
    daily_limit = float(params["max_daily_loss_inr"])
    profit_factor = 0.0
    wr_10 = 0.0
    lesson = f"Ready for {next_session_str} ({'EXPIRY SLOPE ARMED' if params['is_expiry_day'] else 'NORMAL SESSION'})"

    idx_risk_floor = round(capital * 0.032, 2)
    mcx_risk_floor = round(mcx_capital * 0.006, 2)

    if recent_10:
        gross_win = sum(t["pnl_val"] for t in recent_10 if t["pnl_val"] > 0)
        gross_loss = abs(sum(t["pnl_val"] for t in recent_10 if t["pnl_val"] < 0))
        profit_factor = round(gross_win / max(gross_loss, 1.0), 2)
        wins_10 = sum(1 for t in recent_10 if t["pnl_val"] > 0)
        wr_10 = round((wins_10 / len(recent_10)) * 100.0, 1)
        lesson = f"Steady Cruise (PF {profit_factor}, WR {wr_10}%)"

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
            params["index_max_risk_inr"] = max(idx_risk_floor, round(params["index_max_risk_inr"] * 0.80, 2))
            params["mcx_max_risk_inr"] = max(mcx_risk_floor, round(params["mcx_max_risk_inr"] * 0.80, 2))
            params["min_fuel_score"] = min(60.0, float(params.get("min_fuel_score", 48.0)) + 4.0)
            params["expiry_slope_fuel_score"] = min(42.0, float(params.get("expiry_slope_fuel_score", 32.0)) + 3.0)
            lesson = f"SHIELD TRIGGERED -> Gear {old_gear}->{new_gear} | Risk & Fuel tightened (Day ₹{day_net:+.0f})"
        elif len(recent_10) >= 5:
            if wr_10 >= 65.0 and profit_factor >= 2.2:
                new_gear = min(5, old_gear + 1)
                params["index_max_risk_inr"] = min(round(capital * 0.05, 2), round(params["index_max_risk_inr"] * 1.10, 2))
                params["mcx_max_risk_inr"] = min(round(mcx_capital * 0.01, 2), round(params["mcx_max_risk_inr"] * 1.10, 2))
                params["min_fuel_score"] = max(44.0, float(params.get("min_fuel_score", 48.0)) - 2.0)
                params["expiry_slope_fuel_score"] = max(30.0, float(params.get("expiry_slope_fuel_score", 32.0)) - 1.0)
                lesson = f"High Precision -> Upshift Gear {new_gear} (PF {profit_factor})"
            elif wr_10 >= 55.0 and profit_factor >= 1.6 and old_gear < 4:
                new_gear = 4
                lesson = f"Strong Trend Capture -> Upshift Gear 4 (PF {profit_factor})"
            elif wr_10 < 35.0 or profit_factor < 0.75:
                new_gear = max(1, old_gear - 1)
                params["index_max_risk_inr"] = max(idx_risk_floor, round(params["index_max_risk_inr"] * 0.85, 2))
                params["mcx_max_risk_inr"] = max(mcx_risk_floor, round(params["mcx_max_risk_inr"] * 0.85, 2))
                params["min_fuel_score"] = min(58.0, float(params.get("min_fuel_score", 48.0)) + 3.0)
                lesson = f"Drawdown Shield -> Downshift Gear {new_gear} (PF {profit_factor})"

    params["driver_gear"] = new_gear
    params["trail_cushion_mult"] = 1.15 if new_gear >= 4 else (0.85 if new_gear <= 2 else 1.0)
    params["atr_sl_buffer_mult"] = 0.14 if new_gear >= 4 else (0.10 if new_gear <= 2 else 0.12)

    params, iv_note = upgrade_for_iv_crush_and_low_delta(params, today_trades)
    params, greeks_note = calibrate_options_chain_brain(params, today_trades)
    lesson += iv_note + greeks_note

    # Dynamic 4% of capital symbol cooldown threshold (zero hardcoded rupee threshold)
    cooldown_threshold = round(capital * 0.04, 2)
    sym_ov = params.get("symbol_overrides", {})
    for sym in set(t.get("symbol", "") for t in today_trades if t.get("symbol")):
        sym_today_pnl = sum(t["pnl_val"] for t in today_trades if t.get("symbol") == sym)
        ov = sym_ov.get(sym, {"min_fuel": params["min_fuel_score"], "cooldown": False})
        if sym_today_pnl < -cooldown_threshold:
            ov["cooldown"] = True
            ov["min_fuel"] = min(65.0, float(ov.get("min_fuel", 48.0)) + 5.0)
            lesson += f" | {sym} Cooldown (Fuel {ov['min_fuel']}+)"
        elif sym_today_pnl > cooldown_threshold:
            ov["cooldown"] = False
            ov["min_fuel"] = max(44.0, float(ov.get("min_fuel", 48.0)) - 2.0)
        sym_ov[sym] = ov
    params["symbol_overrides"] = sym_ov

    is_event, event_name = check_scheduled_event(next_session_str)
    params = apply_event_day_shield(params, capital, is_event, event_name, vix_change_pct)
    if params["event_mode"]:
        lesson += f" | EVENT SHIELD ({params['event_label']})"

    params["last_lesson"] = lesson

    with open(PARAMS_FILE, "w", encoding="utf-8") as f:
        json.dump(params, f, indent=2)

    log.info(
        f"[BRAIN {params['brain_version']}] Session={next_session_str} | Expiry={params['is_expiry_day']} | "
        f"Gear {old_gear}->{new_gear} | FlatFuel={params['min_fuel_score']} | "
        f"SlopeFuel={params['expiry_slope_fuel_score']} | MinDelta={params['min_option_delta']} | {lesson}"
    )

    if GSHEET_WEBHOOK_URL:
        try:
            requests.post(
                GSHEET_WEBHOOK_URL,
                json={
                    "action": "LOG_LEARNING",
                    "date": today_str,
                    "next_session": next_session_str,
                    "brain_version": params["brain_version"],
                    "is_expiry_day": params["is_expiry_day"],
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
