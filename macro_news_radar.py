import os
import json
import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [MACRO_SECTOR_RADAR] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("MACRO_SECTOR_RADAR")

UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
MACRO_FILE = "live_journal/macro_context.json"
VIX_HISTORY_FILE = "live_journal/vix_baseline.json"

SECTOR_INDICES = {
    "BANK":   "NSE_INDEX|Nifty Bank",
    "FIN":    "NSE_INDEX|Nifty Fin Service",
    "OILGAS": "NSE_INDEX|Nifty Oil & Gas",
    "IT":     "NSE_INDEX|Nifty IT",
    "AUTO":   "NSE_INDEX|Nifty Auto",
    "METAL":  "NSE_INDEX|Nifty Metal",
    "MEDIA":  "NSE_INDEX|Nifty Media",
    "FMCG":   "NSE_INDEX|Nifty FMCG",
    "PHARMA": "NSE_INDEX|Nifty Pharma",
}

HEAVYWEIGHTS = {
    "HDFCBANK":   {"sector": "BANK",   "nifty_w": 10.54, "bnf_w": 28.0},
    "ICICIBANK":  {"sector": "BANK",   "nifty_w": 8.81,  "bnf_w": 23.0},
    "SBIN":       {"sector": "BANK",   "nifty_w": 3.79,  "bnf_w": 10.5},
    "AXISBANK":   {"sector": "BANK",   "nifty_w": 3.32,  "bnf_w": 9.5},
    "KOTAKBANK":  {"sector": "BANK",   "nifty_w": 2.84,  "bnf_w": 9.0},
    "BAJFINANCE": {"sector": "FIN",    "nifty_w": 2.53,  "bnf_w": 0.0},
    "RELIANCE":   {"sector": "OILGAS", "nifty_w": 7.52,  "bnf_w": 0.0},
    "ONGC":       {"sector": "OILGAS", "nifty_w": 0.85,  "bnf_w": 0.0},
    "NTPC":       {"sector": "OILGAS", "nifty_w": 1.46,  "bnf_w": 0.0},
    "INFY":       {"sector": "IT",     "nifty_w": 3.41,  "bnf_w": 0.0},
    "TCS":        {"sector": "IT",     "nifty_w": 1.97,  "bnf_w": 0.0},
    "HCLTECH":    {"sector": "IT",     "nifty_w": 1.22,  "bnf_w": 0.0},
    "TATASTEEL":  {"sector": "METAL",  "nifty_w": 1.48,  "bnf_w": 0.0},
    "HINDALCO":   {"sector": "METAL",  "nifty_w": 1.32,  "bnf_w": 0.0},
    "JSWSTEEL":   {"sector": "METAL",  "nifty_w": 1.17,  "bnf_w": 0.0},
    "COALINDIA":  {"sector": "METAL",  "nifty_w": 0.97,  "bnf_w": 0.0},
    "BHARTIARTL": {"sector": "MEDIA",  "nifty_w": 5.12,  "bnf_w": 0.0},
    "ZEEL":       {"sector": "MEDIA",  "nifty_w": 0.15,  "bnf_w": 0.0},
    "LT":         {"sector": "INFRA",  "nifty_w": 4.17,  "bnf_w": 0.0},
    "M&M":        {"sector": "AUTO",   "nifty_w": 2.51,  "bnf_w": 0.0},
    "MARUTI":     {"sector": "AUTO",   "nifty_w": 1.47,  "bnf_w": 0.0},
    "ITC":        {"sector": "FMCG",   "nifty_w": 2.43,  "bnf_w": 0.0},
    "HINDUNILVR": {"sector": "FMCG",   "nifty_w": 1.57,  "bnf_w": 0.0},
    "SUNPHARMA":  {"sector": "PHARMA", "nifty_w": 1.88,  "bnf_w": 0.0},
}

RSS_FEEDS = [
    "https://news.google.com/rss/search?q=Nifty+Sensex+India+stock+market+earnings+when:1h&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=RBI+repo+rate+CPI+inflation+GDP+growth+Fed+when:1d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=war+missile+attack+crude+oil+sanctions+crisis+when:1h&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=India+election+results+union+budget+tax+monsoon+drought+when:1d&hl=en-IN&gl=IN&ceid=IN:en",
]

SEVERE_BEARISH_KEYWORDS = [
    "war", "missile", "airstrike", "invasion", "nuclear", "emergency", "terror attack", "sanctions",
    "coalition collapse", "election shock", "government falls", "rate hike", "repo rate hike",
    "recession", "gdp slump", "stagflation", "market crash", "black monday", "circuit breaker",
    "banking crisis", "pandemic", "lockdown", "severe drought", "oil shock"
]

EVENT_VOLATILITY_KEYWORDS = [
    "repo rate", "rbi policy", "monetary policy", "union budget", "gdp data",
    "election result", "exit poll", "fed rate", "fomc", "cpi inflation", "quarterly results"
]


def upstox_headers():
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
    }


def update_and_get_vix_baseline(today_str: str, current_vix: float) -> float:
    """Self-learning 20-day rolling VIX average instead of a hardcoded VIX level."""
    history = {}
    if os.path.isfile(VIX_HISTORY_FILE):
        try:
            with open(VIX_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            pass

    if current_vix > 0:
        history[today_str] = round(current_vix, 2)
        recent_keys = sorted(history.keys())[-20:]
        history = {k: history[k] for k in recent_keys}
        os.makedirs("live_journal", exist_ok=True)
        with open(VIX_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)

    vals = [float(v) for v in history.values() if float(v) > 0]
    return round(sum(vals) / len(vals), 2) if vals else max(current_vix, 14.0)


def resolve_equity_keys() -> dict:
    resolved = {}
    if not UPSTOX_ACCESS_TOKEN:
        return resolved
    for sym in HEAVYWEIGHTS:
        try:
            r = requests.get(
                "https://api.upstox.com/v2/instruments/search",
                headers=upstox_headers(),
                params={"query": sym, "exchanges": "NSE", "segments": "EQ", "records": 5},
                timeout=6,
            ).json()
            exact = [x for x in r.get("data", []) if str(x.get("trading_symbol", "")).upper() == sym]
            if exact and exact[0].get("instrument_key"):
                resolved[sym] = exact[0]["instrument_key"]
        except Exception:
            continue
    return resolved


def fetch_sectors_and_heavyweights() -> dict:
    result = {
        "vix": 0.0,
        "vix_chg_pct": 0.0,
        "sector_perf": {},
        "heavyweight_perf": {},
        "green_sectors": 0,
        "red_sectors": 0,
        "bnf_heavy_impact": 0.0,
        "nifty_heavy_impact": 0.0,
        "top_pullers": [],
        "top_draggers": [],
        "sector_verdict": "NEUTRAL",
    }
    if not UPSTOX_ACCESS_TOKEN:
        return result

    eq_map = resolve_equity_keys()
    rev_eq = {v: k for k, v in eq_map.items()}
    rev_sec = {v: k for k, v in SECTOR_INDICES.items()}

    all_keys = ["NSE_INDEX|India VIX"] + list(SECTOR_INDICES.values()) + list(eq_map.values())
    try:
        res = requests.get(
            "https://api.upstox.com/v2/market-quote/quotes",
            headers=upstox_headers(),
            params={"instrument_key": ",".join(all_keys)},
            timeout=10,
        ).json()
        raw_data = res.get("data", {})
    except Exception as e:
        log.warning(f"Sector/Heavyweight quote error: {e}")
        return result

    bnf_weighted_sum = 0.0
    nifty_weighted_sum = 0.0
    stock_impacts = []

    for k, v in raw_data.items():
        norm_k = k.replace(":", "|")
        tok = v.get("instrument_token", norm_k)
        ltp = float(v.get("last_price", 0.0))
        prev_c = float(v.get("ohlc", {}).get("close", ltp))
        if ltp <= 0 or prev_c <= 0:
            continue
        chg_pct = round(((ltp - prev_c) / prev_c) * 100.0, 2)

        if "India VIX" in norm_k or "India VIX" in str(v.get("symbol", "")):
            result["vix"] = round(ltp, 2)
            result["vix_chg_pct"] = chg_pct
        elif norm_k in rev_sec or tok in rev_sec:
            sec_name = rev_sec.get(norm_k) or rev_sec.get(tok)
            result["sector_perf"][sec_name] = chg_pct
        elif norm_k in rev_eq or tok in rev_eq:
            sym = rev_eq.get(norm_k) or rev_eq.get(tok)
            result["heavyweight_perf"][sym] = chg_pct
            w_info = HEAVYWEIGHTS[sym]
            n_imp = round((chg_pct * w_info["nifty_w"]) / 100.0, 3)
            b_imp = round((chg_pct * w_info["bnf_w"]) / 100.0, 3)
            nifty_weighted_sum += n_imp
            bnf_weighted_sum += b_imp
            stock_impacts.append((sym, chg_pct, n_imp))

    green_sec = sum(1 for v in result["sector_perf"].values() if v > 0.15)
    red_sec = sum(1 for v in result["sector_perf"].values() if v < -0.15)
    result["green_sectors"] = green_sec
    result["red_sectors"] = red_sec
    result["bnf_heavy_impact"] = round(bnf_weighted_sum, 2)
    result["nifty_heavy_impact"] = round(nifty_weighted_sum, 2)

    stock_impacts.sort(key=lambda x: x[2], reverse=True)
    result["top_pullers"] = [f"{s}({c:+.1f}%)" for s, c, imp in stock_impacts[:3] if imp > 0]
    result["top_draggers"] = [f"{s}({c:+.1f}%)" for s, c, imp in reversed(stock_impacts[-3:]) if imp < 0]

    total_secs = max(len(result["sector_perf"]), 1)
    majority_secs = max(2, int(round(total_secs * 0.75)))

    if bnf_weighted_sum <= -0.65 and nifty_weighted_sum >= 0.15:
        result["sector_verdict"] = "SPLIT_BANK_WEAK_NIFTY_STRONG"
    elif bnf_weighted_sum <= -0.65 and nifty_weighted_sum < 0:
        result["sector_verdict"] = "HEAVYWEIGHT_DRAG_BEARISH"
    elif green_sec >= majority_secs and nifty_weighted_sum >= 0.35:
        result["sector_verdict"] = "BROAD_SECTOR_RALLY_BULL"
    elif red_sec >= majority_secs and nifty_weighted_sum <= -0.35:
        result["sector_verdict"] = "BROAD_SECTOR_SELLOFF_BEAR"
    elif abs(green_sec - red_sec) <= 2:
        result["sector_verdict"] = "SECTOR_TUG_OF_WAR_CHOP"
    else:
        result["sector_verdict"] = "BALANCED"

    return result


def fetch_fii_dii_activity() -> dict:
    result = {"fii_net_cr": 0.0, "dii_net_cr": 0.0, "flow_regime": "NEUTRAL"}
    if not UPSTOX_ACCESS_TOKEN:
        return result
    try:
        fii_res = requests.get(
            "https://api.upstox.com/v2/market/fii",
            headers=upstox_headers(),
            params=[("data_type", "NSE_EQ|CASH"), ("interval", "1D")],
            timeout=8,
        ).json()
        cash_recs = fii_res.get("data", {}).get("NSE_EQ|CASH", [])
        if cash_recs:
            latest_fii = sorted(cash_recs, key=lambda x: x.get("time_stamp", 0))[-1]
            result["fii_net_cr"] = round(float(latest_fii.get("net_value", latest_fii.get("netValue", 0.0))), 2)

        dii_res = requests.get(
            "https://api.upstox.com/v2/market/dii",
            headers=upstox_headers(),
            params={"interval": "1D"},
            timeout=8,
        ).json()
        dii_recs = dii_res.get("data", [])
        if isinstance(dii_recs, dict):
            dii_recs = dii_recs.get("NSE_EQ|CASH", [])
        if dii_recs:
            latest_dii = sorted(dii_recs, key=lambda x: x.get("time_stamp", 0))[-1]
            result["dii_net_cr"] = round(float(latest_dii.get("net_value", latest_dii.get("netValue", 0.0))), 2)
    except Exception:
        pass

    if result["fii_net_cr"] < -1500 and result["dii_net_cr"] < 0:
        result["flow_regime"] = "BOTH_SELLING_BEAR_ALERT"
    elif result["
