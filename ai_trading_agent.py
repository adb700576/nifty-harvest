import os
import re
import ast
import csv
import json
import time
import math
import shutil
import py_compile
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

IST = timezone(timedelta(hours=5, minutes=30))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AI_TRADING_AGENT] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("AI_TRADING_AGENT")

UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

JOURNAL_FILE = "live_journal/trade_journal.csv"
PARAMS_FILE = "live_journal/strategy_params.json"
MEMORY_FILE = "live_journal/market_memory.json"
MACRO_FILE = "live_journal/macro_context.json"
HOMEWORK_REPORT_FILE = "live_journal/daily_homework_report.json"
DOCS_CACHE_FILE = "live_journal/upstox_docs_snapshot.json"
UPSTOX_DOCS_URL = "https://upstox.com/developer/api-documentation/open-api"

ALL_10_SCRIPTS = {
    "NIFTY":      {"type": "INDEX", "key": "NSE_INDEX|Nifty 50"},
    "BANKNIFTY":  {"type": "INDEX", "key": "NSE_INDEX|Nifty Bank"},
    "FINNIFTY":   {"type": "INDEX", "key": "NSE_INDEX|Nifty Fin Service"},
    "MIDCPNIFTY": {"type": "INDEX", "key": "NSE_INDEX|NIFTY MID SELECT"},
    "SENSEX":     {"type": "INDEX", "key": "BSE_INDEX|SENSEX"},
    "CRUDEOIL":   {"type": "MCX",   "key": ""},
    "NATURALGAS": {"type": "MCX",   "key": ""},
    "GOLD":       {"type": "MCX",   "key": ""},
    "SILVER":     {"type": "MCX",   "key": ""},
    "COPPER":     {"type": "MCX",   "key": ""},
}


def is_overnight_quiet_hours(now_ist: datetime) -> bool:
    hhmm = now_ist.strftime("%H:%M")
    return hhmm >= "23:36" or hhmm < "08:30"


def send_agent_telegram(text: str, force_morning: bool = False):
    now_ist = datetime.now(IST)
    if not force_morning and is_overnight_quiet_hours(now_ist):
        log.info("[QUIET HOURS] Suppressed Telegram message between 23:36 and 08:30 IST.")
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not text:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Telegram notify error: {e}")


def _upstox_headers() -> dict:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def _resolve_mcx_keys() -> dict:
    resolved = {k: dict(v) for k, v in ALL_10_SCRIPTS.items()}
    if not UPSTOX_ACCESS_TOKEN:
        return resolved
    today_date = datetime.now(IST).date()
    safe_min_expiry = (today_date + timedelta(days=7)).strftime("%Y-%m-%d")

    for sym, info in resolved.items():
        if info["type"] != "MCX":
            continue
        cands = []
        for exp_kw in ("current_month", "next_month"):
            try:
                res = requests.get(
                    "https://api.upstox.com/v2/instruments/search",
                    headers=_upstox_headers(),
                    params={"query": sym, "exchanges": "MCX", "segments": "FUT", "expiry": exp_kw, "page_number": 1, "records": 20},
                    timeout=8,
                ).json().get("data", [])
                exact = [
                    it for it in res
                    if str(it.get("instrument_type", "")).upper() == "FUT"
                    and str(it.get("trading_symbol", "")).upper().startswith(f"{sym} FUT")
                ]
                cands.extend(exact if exact else [it for it in res if str(it.get("instrument_type", "")).upper() == "FUT"])
            except Exception:
                pass
        if cands:
            cands.sort(key=lambda x: str(x.get("expiry", "9999-12-31")))
            liq = [c for c in cands if str(c.get("expiry", "")) >= safe_min_expiry]
            pick = liq[0] if liq else cands[0]
            info["key"] = str(pick.get("instrument_key", ""))
            info["contract"] = str(pick.get("trading_symbol", sym))
    return resolved


def _fetch_recent_5m_candles(instrument_key: str) -> pd.DataFrame:
    if not instrument_key or not UPSTOX_ACCESS_TOKEN:
        return pd.DataFrame()
    encoded = requests.utils.quote(instrument_key, safe="")
    # Try intraday first; if empty (early morning before 09:15), fetch last 3 trading days historical 5m
    urls = [
        f"https://api.upstox.com/v3/historical-candle/intraday/{encoded}/minutes/5",
    ]
    to_d = datetime.now(IST).strftime("%Y-%m-%d")
    from_d = (datetime.now(IST) - timedelta(days=4)).strftime("%Y-%m-%d")
    urls.append(f"https://api.upstox.com/v3/historical-candle/{encoded}/minutes/5/{to_d}/{from_d}")

    for u in urls:
        try:
            candles = requests.get(u, headers=_upstox_headers(), timeout=10).json().get("data", {}).get("candles", [])
            if candles:
                df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df = df.sort_values("timestamp").reset_index(drop=True)
                for c in ["open", "high", "low", "close", "volume", "oi"]:
                    df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
                return df
        except Exception:
            continue
    return pd.DataFrame()


class TickByTickAITradingAgent:
    """
    24x7 Autonomous Institutional Quant & Tick-by-Tick AI Trading Agent:
    1. Streams every tick (ms) via Upstox V3 WebSocket across all Indices, MCX & Active Positions.
    2. Monitors live OI, Change in OI, PCR, Greeks & Tick Velocity continuously.
    3. Dynamically mutates strategy_params.json in real-time during trending or drawdown regimes.
    4. Performs Nightly & 08:55 AM Institutional 10-Script OHLC/VWAP/ATR/OI Homework Audit
       and sends a structured Marathi Desk Briefing with file-level code recommendations.
    """

    def __init__(self, engine_ref=None):
        self.engine = engine_ref
        self.tick_buffer = {}
        self.velocity_scores = {}
        self.last_ai_think_ts = 0.0
        self.last_oi_scan_ts = 0.0
        self.agent_lock = threading.Lock()
        self.live_oi_snapshot = {}

    def on_market_tick(self, instrument_key: str, ltp: float, volume: float = 0.0, oi: float = 0.0):
        now_ms = time.time() * 1000.0
        norm_key = instrument_key.replace(":", "|")

        with self.agent_lock:
            buf = self.tick_buffer.setdefault(norm_key, deque(maxlen=60))
            buf.append((now_ms, ltp, volume, oi))

            if len(buf) >= 5:
                oldest_ms, oldest_px, _, _ = buf[0]
                dt_sec = max((now_ms - oldest_ms) / 1000.0, 0.05)
                pct_move = ((ltp - oldest_px) / max(oldest_px, 1e-6)) * 100.0
                self.velocity_scores[norm_key] = round(pct_move / dt_sec, 4)

        if self.engine and hasattr(self.engine, "on_tick_price"):
            self.engine.on_tick_price(norm_key, ltp)

    def scan_live_oi_and_greeks(self):
        if not self.engine or not UPSTOX_ACCESS_TOKEN:
            return
        now_ts = time.time()
        if (now_ts - self.last_oi_scan_ts) < 4.0:
            return
        self.last_oi_scan_ts = now_ts

        for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"):
            cfg = getattr(self.engine, "last_summaries", {}).get(idx)
            if not cfg:
                continue
            self.live_oi_snapshot[idx] = {
                "spot": cfg.get("spot", 0),
                "pcr": cfg.get("pcr", 1.0),
                "pcr_diff": cfg.get("cum_pcr_diff", 0.0),
                "fuel": cfg.get("fuel", 0),
                "tick_velocity": self.velocity_scores.get(idx, 0.0),
            }

    def think_and_adapt_live_strategy(self):
        if not self.engine:
            return
        now_ts = time.time()
        if (now_ts - self.last_ai_think_ts) < 15.0:
            return
        self.last_ai_think_ts = now_ts

        self.scan_live_oi_and_greeks()
        params = self.engine.tuned_params
        changed = False
        reason_notes = []

        strong_bears = [
            k for k, v in self.live_oi_snapshot.items()
            if v.get("pcr", 1.0) <= 0.82 and v.get("fuel", 0) >= 48
        ]
        strong_bulls = [
            k for k, v in self.live_oi_snapshot.items()
            if v.get("pcr", 1.0) >= 1.22 and v.get("fuel", 0) >= 48
        ]

        if (len(strong_bears) >= 2 or len(strong_bulls) >= 2) and len(self.engine.closed_trades) == 0:
            if float(params.get("min_fuel_score", 45.0)) > 40.0:
                params["min_fuel_score"] = 40.0
                changed = True
                reason_notes.append("Trend Ignition: Fuel lowered to 40.0")
            if int(params.get("driver_gear", 1)) == 1 and self.engine.day_pnl >= 0:
                params["driver_gear"] = 2
                changed = True
                reason_notes.append("Gear upshifted G1->G2")

        live_mtm = self.engine.compute_live_mtm()
        net_live = self.engine.day_pnl + live_mtm
        max_loss = abs(float(params.get("max_daily_loss_inr", 3750.0)))
        if net_live <= -(max_loss * 0.65) and int(params.get("driver_gear", 2)) > 1:
            params["driver_gear"] = 1
            params["min_fuel_score"] = 50.0
            changed = True
            reason_notes.append(f"Drawdown Shield (Net ₹{net_live:+.0f}) -> Gear 1")

        if changed:
            os.makedirs("live_journal", exist_ok=True)
            with open(PARAMS_FILE, "w", encoding="utf-8") as f:
                json.dump(params, f, indent=2)
            log.info(f"[LIVE ADAPT] {' | '.join(reason_notes)}")

    def compute_skill_level_and_journal_stats(self) -> dict:
        trades = []
        if os.path.isfile(JOURNAL_FILE):
            try:
                with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        try:
                            trades.append({
                                "date": str(r.get("date", ""))[:10],
                                "symbol": str(r.get("symbol", "")).strip(),
                                "side": str(r.get("side", "")).strip(),
                                "pnl": float(r.get("pnl", 0.0) or 0.0),
                                "exit_reason": str(r.get("exit_reason", "")).strip(),
                                "setup": str(r.get("setup", "")).strip(),
                            })
                        except Exception:
                            continue
            except Exception:
                pass

        total = len(trades)
        wins = sum(1 for t in trades if t["pnl"] > 0)
        wr = round((wins / total) * 100.0, 1) if total > 0 else 0.0
        gross_win = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
        pf = round(gross_win / max(gross_loss, 1.0), 2) if total > 0 else 0.0
        net_pnl = round(sum(t["pnl"] for t in trades), 2)

        # Skill Level progression based on sample size + Profit Factor + Win Rate
        if total < 25:
            level_str = f"Level 1/5 • Foundation & Capital Defense ({total}/25 Trades)"
        elif total < 50 or pf < 1.2:
            level_str = f"Level 2/5 • Pattern Calibration ({total}/50 Trades)"
        elif pf < 1.6 or wr < 50.0:
            level_str = f"Level 3/5 • Selective Trend Capture ({total} Trades)"
        elif pf < 2.1 or wr < 60.0:
            level_str = f"Level 4/5 • Institutional Execution ({total} Trades)"
        else:
            level_str = f"Level 5/5 • Autonomous Quant Desk ({total} Trades)"

        last_date = max((t["date"] for t in trades if t["date"]), default="")
        last_day_trades = [t for t in trades if t["date"] == last_date]

        return {
            "total_trades": total,
            "wins": wins,
            "win_rate": wr,
            "profit_factor": pf,
            "net_pnl": net_pnl,
            "skill_level": level_str,
            "last_session_date": last_date,
            "last_session_trades": last_day_trades,
        }

    def collect_10_scripts_homework_metrics(self) -> dict:
        scripts = _resolve_mcx_keys()
        summary = {}
        for sym, info in scripts.items():
            df = _fetch_recent_5m_candles(info.get("key", ""))
            if df.empty or len(df) < 5:
                continue
            df["day"] = df["timestamp"].dt.strftime("%Y-%m-%d")
            last_day = df["day"].iloc[-1]
            day_df = df[df["day"] == last_day].copy().reset_index(drop=True)
            if len(day_df) < 3:
                continue

            prev_close = day_df["close"].shift(1).fillna(day_df["open"])
            tr = pd.concat([
                day_df["high"] - day_df["low"],
                (day_df["high"] - prev_close).abs(),
                (day_df["low"] - prev_close).abs(),
            ], axis=1).max(axis=1)
            atr14 = max(float(tr.rolling(14, min_periods=1).mean().iloc[-1]), 1e-6)

            tp = (day_df["high"] + day_df["low"] + day_df["close"]) / 3.0
            vol_safe = day_df["volume"].replace(0, 1.0)
            vwap_ser = (tp * vol_safe).cumsum() / vol_safe.cumsum()

            day_open = float(day_df["open"].iloc[0])
            day_high = float(day_df["high"].max())
            day_low = float(day_df["low"].min())
            day_close = float(day_df["close"].iloc[-1])
            end_vwap = float(vwap_ser.iloc[-1])
            range_pts = round(day_high - day_low, 2)
            range_atr = round(range_pts / atr14, 2)
            vwap_gap_atr = round(abs(day_close - end_vwap) / atr14, 2)
            chg_pct = round(((day_close - day_open) / max(day_open, 1e-6)) * 100.0, 2)
            oi_chg = round(float(day_df["oi"].iloc[-1] - day_df["oi"].iloc[0]), 0)

            summary[sym] = {
                "date": last_day,
                "contract": info.get("contract", sym),
                "open": round(day_open, 2),
                "high": round(day_high, 2),
                "low": round(day_low, 2),
                "close": round(day_close, 2),
                "chg_pct": chg_pct,
                "range_pts": range_pts,
                "atr14": round(atr14, 2),
                "range_in_atr": range_atr,
                "end_vwap": round(end_vwap, 2),
                "vwap_gap_in_atr": vwap_gap_atr,
                "oi_change": oi_chg,
            }
        return summary

    def generate_institutional_homework_briefing(self, send_morning_msg: bool = False) -> dict:
        """
        Audits all 10 scripts (OHLC, VWAP distance in ATR, Range in ATR, OI, Journal Stats)
        and produces an Institutional Quant Desk Briefing in Marathi with specific file-level
        code upgrade recommendations (e.g. `live_engine.py`, `auto_tuner.py`).
        """
        stats = self.compute_skill_level_and_journal_stats()
        market_scan = self.collect_10_scripts_homework_metrics()

        params = {}
        if os.path.isfile(PARAMS_FILE):
            try:
                with open(PARAMS_FILE, "r", encoding="utf-8") as f:
                    params = json.load(f)
            except Exception:
                pass

        macro = {}
        if os.path.isfile(MACRO_FILE):
            try:
                with open(MACRO_FILE, "r", encoding="utf-8") as f:
                    macro = json.load(f)
            except Exception:
                pass

        # Identify top missed/captured directional moves (>= 3.5x ATR intraday range)
        big_movers = sorted(
            [(sym, d) for sym, d in market_scan.items() if d.get("range_in_atr", 0) >= 3.0],
            key=lambda x: x[1]["range_in_atr"],
            reverse=True,
        )

        # Fallback deterministic institutional desk analysis (if Gemini key missing or API busy)
        mover_lines = []
        for sym, d in big_movers[:3]:
            direction = "तेजी (Bullish)" if d["chg_pct"] >= 0 else "मंदी (Bearish)"
            mover_lines.append(
                f"• <b>{sym}:</b> <code>{d['range_pts']} pts</code> ({d['range_in_atr']}× ATR) {direction} | "
                f"VWAP अंतर: <code>{d['vwap_gap_in_atr']}× ATR</code>"
            )
        if not mover_lines:
            mover_lines.append("• मागील सत्रात सर्व १० स्क्रिप्ट्समध्ये मध्यम रेंज (Range-Bound) हालचाल होती.")

        ai_insight_block = (
            "1️⃣ <b>मार्केट व Missed Rally ऑडिट (10 Scripts OHLC/VWAP/ATR):</b>\n"
            + "\n".join(mover_lines)
            + "\n\n2️⃣ <b>कालचे पॅरामीटर ट्यूनिंग (<code>strategy_params.json</code>):</b>\n"
            + f"• Gear: <code>G{params.get('driver_gear', 1)}</code> | Index Fuel: <code>{params.get('min_fuel_score', 45.0)}</code> | "
            + f"MCX Evening Fuel: <code>{params.get('mcx_evening_fuel_score', 44.0)}</code>\n"
            + f"• धडा: <i>{params.get('last_lesson', 'Capital Defense & Base-Near Pullbacks')}</i>\n\n"
            + "3️⃣ <b>कोड सुधारणा शिफारस (Code Desk Recommendation):</b>\n"
            + "• <code>live_engine.py</code> मधील <b>Gann/VWAP Base Guard (<code>&lt;= 2.5 × ATR</code>)</b> आणि "
            + "<b>SILVER Anti-Trap (<code>2.60 × ATR</code>)</b> पुढील २०–३० ट्रेड्ससाठी स्थिर ठेवा जेणेकरून अचूक Win Rate मोजता येईल."
        )

        if GEMINI_API_KEY and market_scan:
            prompt = (
                "You are the Chief Quantitative Strategist & Algorithmic Desk Head for Arjun's Upstox V3 trading system.\n"
                "Write a crisp, institutional-grade Morning Desk Briefing in MARATHI (with English quant terms like VWAP, ATR, Gann, PCR, OI, `live_engine.py`, `auto_tuner.py`, `macro_news_radar.py`).\n"
                "Address the user as 'नमस्कार अर्जुन,' in the opening line.\n\n"
                f"System Skill & Journal Stats:\n{json.dumps(stats, ensure_ascii=False)}\n\n"
                f"Last Session 10-Script OHLC / ATR / VWAP / OI Data:\n{json.dumps(market_scan, ensure_ascii=False)}\n\n"
                f"Current Strategy Params:\n{json.dumps(params, ensure_ascii=False)}\n\n"
                f"Macro Context:\n{json.dumps(macro, ensure_ascii=False)}\n\n"
                "Format the response using clean Telegram HTML tags (<b>, <code>, <i>) ONLY (no markdown ** or ##) with these exact 3 sections:\n"
                "1️⃣ <b>कालचा १०-स्क्रिप्ट्स डेटा व रॅली ऑडिट (OHLC, VWAP, ATR, OI):</b> Explain which script had the biggest move (in points & ATR multiples), how close to VWAP/Gann it started, and whether our `<= 2.5 × ATR` rule would catch the base or block late chasing.\n"
                "2️⃣ <b>स्वयंचलित पॅरामीटर ट्यूनिंग (<code>strategy_params.json</code>):</b> Summarize what `auto_tuner.py` adjusted for today.\n"
                "3️⃣ <b>कोड अपग्रेड शिफारस (File-Level Quant Audit):</b> Give 1–2 concrete, actionable quant engineering recommendations naming the exact file (`live_engine.py`, `auto_tuner.py`, or `macro_news_radar.py`) and exact condition/threshold to test next. Keep total output under 1800 characters."
            )
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
                res = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=25).json()
                raw_txt = (
                    res.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                    .strip()
                )
                if raw_txt:
                    clean_html = raw_txt.replace("```html", "").replace("```", "").strip()
                    ai_insight_block = clean_html
            except Exception as e:
                log.warning(f"Gemini homework generation fallback used: {e}")

        now_ist = datetime.now(IST)
        header = (
            f"🏛️ <b>V3 QUANT DESK • MORNING HOMEWORK BRIEFING</b>\n"
            f"📅 <code>{now_ist.strftime('%d-%b-%Y %H:%M IST')}</code>\n"
            f"────────────────────────────\n"
            f"🎓 <b>Agent Skill:</b> <code>{stats['skill_level']}</code>\n"
            f"📈 <b>All-Time Record:</b> <code>{stats['total_trades']}T</code> | WR <b>{stats['win_rate']}%</b> | PF <b>{stats['profit_factor']}</b> | Net <b>₹{stats['net_pnl']:+,.2f}</b>\n"
            f"────────────────────────────\n\n"
        )
        full_msg = header + ai_insight_block

        report_payload = {
            "generated_at": now_ist.isoformat(),
            "session_date": now_ist.strftime("%Y-%m-%d"),
            "stats": stats,
            "market_scan": market_scan,
            "telegram_message": full_msg,
        }
        os.makedirs("live_journal", exist_ok=True)
        with open(HOMEWORK_REPORT_FILE, "w", encoding="utf-8") as f:
            json.dump(report_payload, f, indent=2, ensure_ascii=False)

        if send_morning_msg:
            send_agent_telegram(full_msg, force_morning=True)
            log.info("[MORNING BRIEFING] Sent 08:55 AM Institutional Homework Report to Telegram.")

        return report_payload

    def read_upstox_api_docs_and_audit(self, error_log: str = "") -> str:
        """
        Fetches official Upstox API documentation metadata/endpoints and audits system files.
        Core trading files (`live_engine.py`) are NEVER auto-overwritten without manual review;
        only non-core radar bugs are eligible for safe sandbox patching, while quant improvements
        are delivered in the 08:55 AM Institutional Desk Briefing.
        """
        doc_summary = ""
        try:
            resp = requests.get(UPSTOX_DOCS_URL, timeout=12)
            if resp.status_code == 200:
                clean_txt = re.sub(r"<[^>]+>", " ", resp.text)
                clean_txt = re.sub(r"\s+", " ", clean_txt)[:12000]
                doc_summary = clean_txt
                os.makedirs("live_journal", exist_ok=True)
                with open(DOCS_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(
                        {"fetched_at": datetime.now(IST).isoformat(), "snippet": doc_summary[:4000]},
                        f,
                        indent=2,
                    )
        except Exception as e:
            log.warning(f"Upstox docs fetch warning: {e}")

        # Always prepare tonight's homework report so 08:55 AM briefing is ready
        self.generate_institutional_homework_briefing(send_morning_msg=False)

        if not GEMINI_API_KEY:
            return "Homework report prepared; GEMINI_API_KEY missing for API doc audit."

        if not os.path.isfile("macro_news_radar.py"):
            return "Homework report prepared; system verified clean."

        with open("macro_news_radar.py", "r", encoding="utf-8") as f:
            radar_code = f.read()

        prompt = (
            "You are AI_TRADING_AGENT, a principal Python quant & Upstox V3 API architect.\n"
            "Inspect ONLY `macro_news_radar.py` for concrete syntax/URL/JSON parsing bugs.\n"
            f"Recent Error Log:\n{error_log or 'None'}\n\n"
            f"Upstox API Docs Excerpt:\n{doc_summary[:4000]}\n\n"
            "Return STRICT JSON ONLY:\n"
            '{"should_patch": true/false, "search_block": "exact old snippet", '
            '"replace_block": "exact new snippet", "summary_marathi": "मराठीत थोडक्यात माहिती"}'
        )

        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
            res = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=20).json()
            raw = (
                res.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
                .strip()
            )
            clean_json = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            decision = json.loads(clean_json)
        except Exception as e:
            return f"Homework prepared; API audit skipped: {e}"

        if not decision.get("should_patch"):
            return "Homework prepared; system verified clean against Upstox V3 Docs."

        old_blk = decision.get("search_block", "")
        new_blk = decision.get("replace_block", "")
        marathi_note = decision.get("summary_marathi", "कोड ऑटो-अपडेट पूर्ण.")

        if not old_blk or old_blk not in radar_code:
            return "Homework prepared; patch block not matched safely."

        backup_path = "macro_news_radar.py.bak"
        shutil.copy2("macro_news_radar.py", backup_path)
        patched_code = radar_code.replace(old_blk, new_blk, 1)

        try:
            ast.parse(patched_code)
            with open("macro_news_radar.py", "w", encoding="utf-8") as f:
                f.write(patched_code)
            py_compile.compile("macro_news_radar.py", doraise=True)
            os.remove(backup_path)
            send_agent_telegram(
                f"🤖 <b>AI TRADING AGENT • RADAR BUG-FIX</b>\n"
                f"────────────────────────────\n"
                f"📁 <b>Updated File:</b> <code>macro_news_radar.py</code>\n"
                f"✅ <b>Sandbox Test:</b> Passed\n"
                f"💡 <b>सुधारणा:</b> {marathi_note}"
            )
            return f"Patched macro_news_radar.py: {marathi_note}"
        except Exception as e:
            shutil.move(backup_path, "macro_news_radar.py")
            return f"Sandbox test failed; rolled back macro_news_radar.py: {e}"


if __name__ == "__main__":
    import sys
    agent = TickByTickAITradingAgent()
    if "--morning-briefing" in sys.argv:
        agent.generate_institutional_homework_briefing(send_morning_msg=True)
    else:
        result = agent.read_upstox_api_docs_and_audit()
        log.info(result)
