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
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
MACRO_FILE = "live_journal/macro_context.json"

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
        "vix": 15.0,
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

    if bnf_weighted_sum <= -0.65 and nifty_weighted_sum >= 0.15:
        result["sector_verdict"] = "SPLIT_BANK_WEAK_NIFTY_STRONG"
    elif bnf_weighted_sum <= -0.65 and nifty_weighted_sum < 0:
        result["sector_verdict"] = "HEAVYWEIGHT_DRAG_BEARISH"
    elif green_sec >= 7 and nifty_weighted_sum >= 0.35:
        result["sector_verdict"] = "BROAD_SECTOR_RALLY_BULL"
    elif red_sec >= 7 and nifty_weighted_sum <= -0.35:
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
    elif result["fii_net_cr"] > 500 and result["dii_net_cr"] > 500:
        result["flow_regime"] = "BOTH_BUYING_STRONG_BULL"
    return result


def scan_news_headlines() -> list:
    headlines = []
    headers = {"User-Agent": "Mozilla/5.0"}
    for url in RSS_FEEDS:
        try:
            resp = requests.get(url, headers=headers, timeout=8)
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.text)
            for item in root.findall(".//item")[:5]:
                title_el = item.find("title")
                if title_el is not None and title_el.text:
                    headlines.append(title_el.text.strip())
        except Exception:
            continue
    return list(dict.fromkeys(headlines))[:24]


def evaluate_macro_context(notify_user: bool = False) -> dict:
    os.makedirs("live_journal", exist_ok=True)
    now_ist = datetime.now(IST)
    sec_data = fetch_sectors_and_heavyweights()
    vix, vix_chg = sec_data["vix"], sec_data["vix_chg_pct"]
    fii_dii = fetch_fii_dii_activity()
    headlines = scan_news_headlines()

    bear_hits, event_hits, matched_alerts = [], [], []
    for h in headlines:
        hl = h.lower()
        for kw in SEVERE_BEARISH_KEYWORDS:
            if kw in hl:
                bear_hits.append(kw)
                matched_alerts.append(h[:95])
                break
        for kw in EVENT_VOLATILITY_KEYWORDS:
            if kw in hl:
                event_hits.append(kw)
                break

    unique_bear = list(dict.fromkeys(bear_hits))
    unique_event = list(dict.fromkeys(event_hits))

    veto_ce = False
    veto_pe = False
    emergency_eject_ce = False
    veto_bnf_ce = sec_data["bnf_heavy_impact"] <= -0.65
    event_caution = len(unique_event) >= 1 or (sec_data["sector_verdict"] == "SECTOR_TUG_OF_WAR_CHOP")
    threat_level = "NORMAL"

    if len(unique_bear) >= 2 or vix >= 21.0 or vix_chg >= 7.0 or (len(unique_bear) >= 1 and vix_chg >= 4.5):
        threat_level = "CRASH_ALERT"
        veto_ce = True
        emergency_eject_ce = True
        summary_reason = f"BREAKING SHOCK ({', '.join(unique_bear[:2]) or f'VIX +{vix_chg}%'}) -> EJECT & VETO CE"
    elif sec_data["sector_verdict"] == "HEAVYWEIGHT_DRAG_BEARISH" or fii_dii["flow_regime"] == "BOTH_SELLING_BEAR_ALERT":
        threat_level = "BEARISH_CAUTION"
        veto_ce = True
        summary_reason = f"Heavyweights Dragging ({', '.join(sec_data['top_draggers'][:2])}) -> CE Blocked"
    elif sec_data["sector_verdict"] == "SPLIT_BANK_WEAK_NIFTY_STRONG":
        threat_level = "SPLIT_DIVERGENCE"
        summary_reason = f"Bank Heavyweights Weak ({sec_data['bnf_heavy_impact']:+.2f}%) -> BankNifty PE / Nifty CE"
    elif sec_data["sector_verdict"] == "BROAD_SECTOR_RALLY_BULL":
        threat_level = "BULLISH_TAILWIND"
        summary_reason = f"Broad Rally ({sec_data['green_sectors']}/9 Sectors Green) -> Highway CE Mode"
    else:
        summary_reason = (
            f"Sectors {sec_data['green_sectors']}G/{sec_data['red_sectors']}R | "
            f"NiftyImp {sec_data['nifty_heavy_impact']:+.2f}% | BNFImp {sec_data['bnf_heavy_impact']:+.2f}%"
        )

    context = {
        "updated_at": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
        "india_vix": vix,
        "vix_chg_pct": vix_chg,
        "fii_dii": fii_dii,
        "sector_perf": sec_data["sector_perf"],
        "bnf_heavy_impact": sec_data["bnf_heavy_impact"],
        "nifty_heavy_impact": sec_data["nifty_heavy_impact"],
        "sector_verdict": sec_data["sector_verdict"],
        "top_pullers": sec_data["top_pullers"],
        "top_draggers": sec_data["top_draggers"],
        "threat_level": threat_level,
        "veto_ce": veto_ce,
        "veto_pe": veto_pe,
        "veto_bnf_ce": veto_bnf_ce,
        "emergency_eject_ce": emergency_eject_ce,
        "event_caution": event_caution,
        "summary": summary_reason,
        "top_alerts": matched_alerts[:3],
    }

    with open(MACRO_FILE, "w", encoding="utf-8") as f:
        json.dump(context, f, indent=2)

    log.info(f"[RADAR] {threat_level} | {sec_data['sector_verdict']} | {summary_reason}")
    return context


if __name__ == "__main__":
    evaluate_macro_context(notify_user=False)
