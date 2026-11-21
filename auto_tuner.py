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
GSHEET_WEBHOOK_URL = os.getenv("GSHEET_WEBHOOK_URL", "").strip()

# NSE Trading Holidays (when Tuesday falls on a holiday, expiry shifts to Monday)
NSE_HOLIDAYS_2026 = {
    "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
    "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28",
    "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20",
    "2026-11-10", "2026-11-24", "2026-12-25",
}


def detect_expiry_for_date(target_dt: datetime, nearest_option_expiry_str: str = "") -> bool:
    """
    Detects whether target_dt is an Index Expiry Day:
    1) Primary: Exact match against broker's nearest NFO option contract expiry date.
    2) Fallback: Tuesday check, or Monday if Tuesday is an NSE holiday.
    """
    target_str = target_dt.strftime("%Y-%m-%d")

    if nearest_option_expiry_str:
        return nearest_option_expiry_str.strip()[:10] == target_str

    weekday = target_dt.weekday()  # Monday=0, Tuesday=1
    if weekday == 1 and target_str not in NSE_HOLIDAYS_2026:
        return True
    if weekday == 0:
        next_day_str = (target_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        if next_day_str in NSE_HOLIDAYS_2026:
            return True

    return False


def get_next_trading_day(now_ist: datetime) -> datetime:
    """Returns the next NSE trading day (skips Saturday, Sunday, and NSE holidays)."""
    candidate = now_ist + timedelta(days=1)
    while candidate.weekday() >= 5 or candidate.strftime("%Y-%m-%d") in NSE_HOLIDAYS_2026:
        candidate += timedelta(days=1)
    return candidate


def check_scheduled_event(target_date_str: str) -> tuple[bool, str]:
    """Checks event_calendar.json (e.g. {'2026-11-23': 'ELECTION_RESULT'}) for major scheduled events."""
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
    is_scheduled_event: bool = False,
    event_name: str = "",
    vix_change_pct: float = 0.0,
) -> dict:
    """
    Automatically protects capital on Election Results, Budget, RBI Policy, or sudden VIX spikes
    by delaying morning entry to 09:45 AM and cutting per-trade risk by 40%.
    """
    if is_scheduled_event or vix_change_pct >= 15.0:
        params["event_mode"] = True
        params["event_label"] = event_name or f"VIX_SPIKE_{vix_change_pct:+.1f}%"
        params["morning_wait_until"] = "09:45"
        params["index_max_risk_inr"] = max(600.0, round(float(params["index_max_risk_inr"]) * 0.60, 2))
        params["min_fuel_score"] = max(float(params.get("min_fuel_score", 48.0)), 55.0)
    else:
        params["event_mode"] = False
        params["event_label"] = ""
        params["morning_wait_until"] = "09:30"
    return params


def upgrade_for_iv_crush_and_low_delta(params: dict, today_trades: list[dict]) -> tuple[dict, str]:
    """
    Upgrades Brain parameters when Spot moves in favor, but Option Premium fails to expand
    due to IV Crush, Theta decay, or low-Delta OTM selection.
    """
    iv_crush_count = sum(
        1
        for t in today_trades
        if t.get("exit_reason") in ("NO_MOMENTUM_EXIT", "IV_CRUSH_EXIT")
        and float(t.get("pnl", 0.0)) <= 0
    )

    if iv_crush_count >= 2:
        params["strike_offset"] = -1          # Shift to 1-strike ITM for strong intrinsic Delta
        params["expiry_strike_offset"] = 0    # Strict ATM on expiry instead of OTM
        params["no_momentum_minutes"] = 10    # Tighten stall exit from 15m to 10m to prevent Theta bleed
        params["min_fuel_score"] = min(62.0, float(params.get("min_fuel_score", 48.0)) + 4.0)
        return params, f" | IV-Crush Upgrade ({iv_crush_count} stalls -> ITM/ATM + 10m Timer)"

    params.setdefault("strike_offset", 0)
    params.setdefault("no_momentum_minutes", 15)
    return params, ""


def calibrate_options_chain_brain(params: dict, today_trades: list[dict]) -> tuple[dict, str]:
    """
    Calibrates Option Greeks (Delta/IV/Theta), Change in OI (Short Covering vs Writing),
    and PCR (Put-Call Ratio) rules inside auto_tuner.py for live_engine.py.
    """
    params.setdefault("min_option_delta", 0.45)           # Avoid low-delta dead strikes
    params.setdefault("expiry_slope_min_delta", 0.30)     # Allow 1-strike OTM only on expiry slope
    params.setdefault("max_theta_decay_pct", 8.0)         # Skip if hourly theta > 8% of premium
    params.setdefault("max_iv_spike_pct", 25.0)           # Block buying into over-inflated IV
    params.setdefault("require_oi_short_covering", True)  # Require Call OI unwinding for CE / Put OI unwinding for PE
    params.setdefault("bullish_pcr_min", 1.15)            # Strong CE bias when PCR >= 1.15 & rising
    params.setdefault("bearish_pcr_max", 0.85)            # Strong PE bias when PCR <= 0.85 & falling

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
    capital = float(os.getenv("CAPITAL_PER_TRADE", "25000.0"))
    now_ist = datetime.now(IST)
    next_session_dt = get_next_trading_day(now_ist)
    next_session_str = next_session_dt.strftime("%Y-%m-%d")

    # CENTRAL BRAIN PARAMETERS (Read directly by live_engine.py)
    params = {
        "brain_version": datetime.now(IST).strftime("v%Y.%m.%d.%H%M"),
        "target_session_date": next_session_str,
        "is_expiry_day": detect_expiry_for_date(next_session_dt, nearest_option_expiry_str),
        "driver_gear": 3,
        # Capital & Risk Shields
        "max_daily_loss_inr": round(capital * 0.15, 2),             # Hard 15% daily capital shield
        "index_max_risk_inr": round(capital * 0.05, 2),             # Hard 5% per-trade index risk cap
        "mcx_max_risk_inr": min(1500.0, round(capital * 0.06, 2)),  # Hard ₹1,500 per-trade MCX risk cap
        # Flat Road vs. Expiry Downhill Slope Rules
        "min_fuel_score": 48.0,                                     # Normal flat-road fuel requirement
        "expiry_slope_fuel_score": 32.0,                            # Expiry 13:45-14:45 downhill slope fuel
        "expiry_slope_start": "13:45",
        "expiry_slope_end": "14:45",
        "expiry_strike_offset": 1,                                  # ATM or 1-strike OTM (never far ₹5 lottery OTM)
        "strike_offset": 0,                                         # Default ATM on normal days
        "mcx_evening_fuel_score": 56.0,
        # Trend, Chop, ATR & News Ignition Geometry
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
        # Greeks, Change in OI & PCR Defaults
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

    if os.path.isfile(PARAMS_FILE):
        try:
            with open(PARAMS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                params.update(saved)
        except Exception:
            pass

    # Refresh session metadata & reset base risk caps before applying daily adjustments
    params["brain_version"] = now_ist.strftime("v%Y.%m.%d.%H%M")
    params["target_session_date"] = next_session_str
    params["is_expiry_day"] = detect_expiry_for_date(next_session_dt, nearest_option_expiry_str)
    params["index_max_risk_inr"] = round(capital * 0.05, 2)
    params["mcx_max_risk_inr"] = min(1500.0, round(capital * 0.06, 2))
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

    if recent_10:
        gross_win = sum(t["pnl_val"] for t in recent_10 if t["pnl_val"] > 0)
        gross_loss = abs(sum(t["pnl_val"] for t in recent_10 if t["pnl_val"] < 0))
        profit_factor = round(gross_win / max(gross_loss, 1.0), 2)
        wins_10 = sum(1 for t in recent_10 if t["pnl_val"] > 0)
        wr_10 = round((wins_10 / len(recent_10)) * 100.0, 1)
        lesson = f"Steady Cruise (PF {profit_factor}, WR {wr_10}%)"

        # 1. Consecutive Loss & Major Accident Shield
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
            params["index_max_risk_inr"] = max(800.0, round(params["index_max_risk_inr"] * 0.80, 2))
            params["mcx_max_risk_inr"] = max(900.0, round(params["mcx_max_risk_inr"] * 0.80, 2))
            params["min_fuel_score"] = min(60.0, float(params.get("min_fuel_score", 48.0)) + 4.0)
            params["expiry_slope_fuel_score"] = min(42.0, float(params.get("expiry_slope_fuel_score", 32.0)) + 3.0)
            lesson = f"SHIELD TRIGGERED -> Gear {old_gear}->{new_gear} | Risk & Fuel tightened (Day ₹{day_net:+.0f})"
        elif len(recent_10) >= 5:
            if wr_10 >= 65.0 and profit_factor >= 2.2:
                new_gear = min(5, old_gear + 1)
                params["index_max_risk_inr"] = min(round(capital * 0.05, 2), round(params["index_max_risk_inr"] * 1.10, 2))
                params["mcx_max_risk_inr"] = min(1500.0, round(params["mcx_max_risk_inr"] * 1.10, 2))
                params["min_fuel_score"] = max(44.0, float(params.get("min_fuel_score", 48.0)) - 2.0)
                params["expiry_slope_fuel_score"] = max(30.0, float(params.get("expiry_slope_fuel_score", 32.0)) - 1.0)
                lesson = f"High Precision -> Upshift Gear {new_gear} (PF {profit_factor})"
            elif wr_10 >= 55.0 and profit_factor >= 1.6 and old_gear < 4:
                new_gear = 4
                lesson = f"Strong Trend Capture -> Upshift Gear 4 (PF {profit_factor})"
            elif wr_10 < 35.0 or profit_factor < 0.75:
                new_gear = max(1, old_gear - 1)
                params["index_max_risk_inr"] = max(800.0, round(params["index_max_risk_inr"] * 0.85, 2))
                params["mcx_max_risk_inr"] = max(900.0, round(params["mcx_max_risk_inr"] * 0.85, 2))
                params["min_fuel_score"] = min(58.0, float(params.get("min_fuel_score", 48.0)) + 3.0)
                lesson = f"Drawdown Shield -> Downshift Gear {new_gear} (PF {profit_factor})"

    # 2. Auto-Calibrate ATR Buffer & Trail Cushion by Gear
    params["driver_gear"] = new_gear
    params["trail_cushion_mult"] = 1.15 if new_gear >= 4 else (0.85 if new_gear <= 2 else 1.0)
    params["atr_sl_buffer_mult"] = 0.14 if new_gear >= 4 else (0.10 if new_gear <= 2 else 0.12)

    # 3. Upgrade for IV Crush / Low Delta & Option Chain Greeks/OI/PCR
    params, iv_note = upgrade_for_iv_crush_and_low_delta(params, today_trades)
    params, greeks_note = calibrate_options_chain_brain(params, today_trades)
    lesson += iv_note + greeks_note

    # 4. Symbol-Specific Memory & Cooldown
    sym_ov = params.get("symbol_overrides", {})
    for sym in set(t.get("symbol", "") for t in today_trades if t.get("symbol")):
        sym_today_pnl = sum(t["pnl_val"] for t in today_trades if t.get("symbol") == sym)
        ov = sym_ov.get(sym, {"min_fuel": params["min_fuel_score"], "cooldown": False})
        if sym_today_pnl < -1000:
            ov["cooldown"] = True
            ov["min_fuel"] = min(65.0, float(ov.get("min_fuel", 48.0)) + 5.0)
            lesson += f" | {sym} Cooldown (Fuel {ov['min_fuel']}+)"
        elif sym_today_pnl > 1000:
            ov["cooldown"] = False
            ov["min_fuel"] = max(44.0, float(ov.get("min_fuel", 48.0)) - 2.0)
        sym_ov[sym] = ov
    params["symbol_overrides"] = sym_ov

    # 5. Apply Event / Election / VIX Shield for Next Session
    is_event, event_name = check_scheduled_event(next_session_str)
    params = apply_event_day_shield(params, is_event, event_name, vix_change_pct)
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
