import os
import json
import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [MACRO_RADAR] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("MACRO_RADAR")

UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
MACRO_FILE = "live_journal/macro_context.json"

# Covers all 5 Pillars: Macro/GDP/Rates, Geopolitics/Wars/Elections/Budget, Earnings, FII/DII/Crude/INR, Disasters/Crises
RSS_FEEDS = [
    "https://news.google.com/rss/search?q=Nifty+Sensex+India+stock+market+earnings+when:1h&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=RBI+repo+rate+CPI+inflation+GDP+growth+Fed+when:1d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=war+missile+attack+crude+oil+sanctions+crisis+when:1h&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=India+election+results+union+budget+tax+monsoon+drought+when:1d&hl=en-IN&gl=IN&ceid=IN:en",
]

SEVERE_BEARISH_KEYWORDS = [
    # Geopolitical & Wars
    "war", "missile", "airstrike", "invasion", "nuclear", "emergency", "terror attack", "sanctions",
    "coalition collapse", "election shock", "government falls",
    # Macro, Inflation, GDP & Tax Shocks
    "rate hike", "repo rate hike", "recession", "gdp slump", "stagflation", "capital gains tax hike",
    # Corporate & Financial Crises
    "market crash", "black monday", "circuit breaker", "earnings miss", "guidance cut",
    "banking crisis", "pandemic", "lockdown", "severe drought", "oil shock"
]

EVENT_VOLATILITY_KEYWORDS = [
    "repo rate", "rbi policy", "monetary policy", "union budget", "gdp data",
    "election result", "exit poll", "fed rate", "fomc", "cpi inflation", "quarterly results"
]

BULLISH_KEYWORDS = [
    "rate cut", "repo rate cut", "stimulus", "record high", "fii buying", "gdp surge",
    "landslide victory", "ceasefire", "peace deal", "normal monsoon", "earnings beat", "tax cut"
]


def upstox_headers():
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
    }


def fetch_macro_market_quotes() -> dict:
    """
    Fetches live India VIX + Active MCX Crude Oil & USD/INR % change from Upstox.
    """
    out = {"vix": 15.0, "vix_chg_pct": 0.0, "crude_chg_pct": 0.0, "usdinr_chg_pct": 0.0}
    if not UPSTOX_ACCESS_TOKEN:
        return out

    keys = ["NSE_INDEX|India VIX"]
    # Resolve active MCX Crude Oil & NSE USDINR futures for live spike tracking
    for sym, exch in (("CRUDEOIL", "MCX"), ("USDINR", "NCD")):
        try:
            r = requests.get(
                "https://api.upstox.com/v2/instruments/search",
                headers=upstox_headers(),
                params={"query": sym, "exchanges": exch, "segments": "FUT", "expiry": "current_month", "records": 5},
                timeout=6,
            ).json()
            items = [x for x in r.get("data", []) if str(x.get("trading_symbol", "")).upper().startswith(sym)]
            items.sort(key=lambda x: str(x.get("expiry", "9999-12-31")))
            if items and items[0].get("instrument_key"):
                keys.append(items[0]["instrument_key"])
        except Exception:
            pass

    try:
        res = requests.get(
            "https://api.upstox.com/v2/market-quote/quotes",
            headers=upstox_headers(),
            params={"instrument_key": ",".join(keys)},
            timeout=8,
        ).json()
        for k, v in res.get("data", {}).items():
            ltp = float(v.get("last_price", 0.0))
            prev_c = float(v.get("ohlc", {}).get("close", ltp))
            if ltp <= 0 or prev_c <= 0:
                continue
            chg_pct = round(((ltp - prev_c) / prev_c) * 100.0, 2)
            sym_u = (k + str(v.get("symbol", ""))).upper()
            if "VIX" in sym_u:
                out["vix"] = round(ltp, 2)
                out["vix_chg_pct"] = chg_pct
            elif "CRUDEOIL" in sym_u:
                out["crude_chg_pct"] = chg_pct
            elif "USDINR" in sym_u:
                out["usdinr_chg_pct"] = chg_pct
    except Exception:
        pass
    return out


def fetch_fii_dii_activity() -> dict:
    result = {"fii_net_cr": 0.0, "dii_net_cr": 0.0, "fii_idx_fut_net": 0.0, "flow_regime": "NEUTRAL"}
    if not UPSTOX_ACCESS_TOKEN:
        return result

    try:
        fii_res = requests.get(
            "https://api.upstox.com/v2/market/fii",
            headers=upstox_headers(),
            params=[("data_type", "NSE_EQ|CASH"), ("data_type", "NSE_FO|INDEX_FUTURES"), ("interval", "1D")],
            timeout=8,
        ).json()
        fii_data = fii_res.get("data", {})
        cash_recs = fii_data.get("NSE_EQ|CASH", [])
        fut_recs = fii_data.get("NSE_FO|INDEX_FUTURES", [])
        if cash_recs:
            latest_fii = sorted(cash_recs, key=lambda x: x.get("time_stamp", 0))[-1]
            result["fii_net_cr"] = round(float(latest_fii.get("net_value", latest_fii.get("netValue", 0.0))), 2)
        if fut_recs:
            latest_fut = sorted(fut_recs, key=lambda x: x.get("time_stamp", 0))[-1]
            result["fii_idx_fut_net"] = round(float(latest_fut.get("net_value", latest_fut.get("netValue", 0.0))), 2)

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
    except Exception as e:
        log.warning(f"FII/DII fetch fallback: {e}")

    fii_n, dii_n = result["fii_net_cr"], result["dii_net_cr"]
    if fii_n > 500 and dii_n > 500:
        result["flow_regime"] = "BOTH_BUYING_STRONG_BULL"
    elif fii_n < -1500 and dii_n < 0:
        result["flow_regime"] = "BOTH_SELLING_BEAR_ALERT"
    elif fii_n < -2500:
        result["flow_regime"] = "HEAVY_FII_SELLING"
    elif fii_n > 2500:
        result["flow_regime"] = "HEAVY_FII_BUYING"
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
    q_info = fetch_macro_market_quotes()
    vix, vix_chg = q_info["vix"], q_info["vix_chg_pct"]
    crude_chg, inr_chg = q_info["crude_chg_pct"], q_info["usdinr_chg_pct"]
    fii_dii = fetch_fii_dii_activity()
    headlines = scan_news_headlines()

    bear_hits, event_hits, bull_hits, matched_alerts = [], [], [], []
    for h in headlines:
        hl = h.lower()
        for kw in SEVERE_BEARISH_KEYWORDS:
            if kw in hl:
                bear_hits.append(kw)
                matched_alerts.append(f"⚠️ {h[:95]}")
                break
        for kw in EVENT_VOLATILITY_KEYWORDS:
            if kw in hl:
                event_hits.append(kw)
                break
        for kw in BULLISH_KEYWORDS:
            if kw in hl:
                bull_hits.append(kw)
                break

    unique_bear = list(dict.fromkeys(bear_hits))
    unique_event = list(dict.fromkeys(event_hits))
    unique_bull = list(dict.fromkeys(bull_hits))

    # Currency & Crude Oil Shock (India is net oil importer: Crude +3.5% or Rupee fall > +0.5% hurts Nifty)
    oil_inr_shock = (crude_chg >= 3.5) or (inr_chg >= 0.50)

    veto_ce = False
    veto_pe = False
    emergency_eject_ce = False
    event_caution = len(unique_event) >= 1
    threat_level = "NORMAL"
    summary_reason = (
        f"FII ₹{fii_dii['fii_net_cr']:+.0f}Cr | DII ₹{fii_dii['dii_net_cr']:+.0f}Cr | "
        f"VIX {vix} ({vix_chg:+.1f}%) | Crude {crude_chg:+.1f}%"
    )

    if len(unique_bear) >= 2 or vix >= 21.0 or vix_chg >= 7.0 or (len(unique_bear) >= 1 and (vix_chg >= 4.5 or oil_inr_shock)):
        threat_level = "CRASH_ALERT"
        veto_ce = True
        emergency_eject_ce = True
        summary_reason = f"BREAKING MACRO SHOCK ({', '.join(unique_bear[:2]) or f'VIX +{vix_chg}%'}) -> EJECT & VETO CE"
    elif fii_dii["flow_regime"] == "BOTH_SELLING_BEAR_ALERT" or oil_inr_shock or (len(unique_bear) == 1 and vix >= 16.5):
        threat_level = "BEARISH_CAUTION"
        veto_ce = True
        summary_reason = f"FII/Crude/INR Bearish Pressure (Crude {crude_chg:+.1f}%, INR {inr_chg:+.2f}%) -> CE Blocked"
    elif fii_dii["flow_regime"] in ("BOTH_BUYING_STRONG_BULL", "HEAVY_FII_BUYING") and len(unique_bear) == 0 and not oil_inr_shock:
        threat_level = "BULLISH_TAILWIND"
        summary_reason = f"Strong FII/DII Buying (FII ₹{fii_dii['fii_net_cr']:+.0f}Cr)"
    elif event_caution:
        threat_level = "EVENT_VOLATILITY"
        summary_reason = f"Major Event ({', '.join(unique_event[:2])}) -> Tight Risk Guard"

    prev_level = "NORMAL"
    if os.path.isfile(MACRO_FILE):
        try:
            with open(MACRO_FILE, "r", encoding="utf-8") as f:
                prev_level = json.load(f).get("threat_level", "NORMAL")
        except Exception:
            pass

    context = {
        "updated_at": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
        "india_vix": vix,
        "vix_chg_pct": vix_chg,
        "crude_chg_pct": crude_chg,
        "usdinr_chg_pct": inr_chg,
        "fii_dii": fii_dii,
        "threat_level": threat_level,
        "veto_ce": veto_ce,
        "veto_pe": veto_pe,
        "emergency_eject_ce": emergency_eject_ce,
        "event_caution": event_caution,
        "summary": summary_reason,
        "top_alerts": matched_alerts[:3],
    }

    with open(MACRO_FILE, "w", encoding="utf-8") as f:
        json.dump(context, f, indent=2)

    escalated = (threat_level in ("CRASH_ALERT", "BEARISH_CAUTION") and threat_level != prev_level)
    if (notify_user or escalated) and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        alert_lines = "\n".join([f"  • <i>{a}</i>" for a in matched_alerts[:2]]) or "  • No sudden war/crash headlines."
        title = "🚨 <b>BREAKING LIVE MARKET SHOCK ALERT!</b>" if escalated else "🌍 <b>PRE-MARKET 5-PILLAR MACRO & FII/DII RADAR</b>"
        msg = (
            f"{title}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📡 <b>Status:</b> <code>{threat_level}</code> │ <b>VIX:</b> <code>{vix} ({vix_chg:+.1f}%)</code>\n"
            f"🛢️ <b>Crude:</b> <code>{crude_chg:+.1f}%</code> │ 💱 <b>USD/INR:</b> <code>{inr_chg:+.2f}%</code>\n"
            f"🏦 <b>FII Cash:</b> <code>₹{fii_dii['fii_net_cr']:+.0f} Cr</code> │ <b>DII Cash:</b> <code>₹{fii_dii['dii_net_cr']:+.0f} Cr</code>\n"
            f"🛡️ <b>Action:</b> {summary_reason}\n"
            f"📰 <b>Headlines:</b>\n{alert_lines}"
        )
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                timeout=8,
            )
        except Exception:
            pass

    return context


if __name__ == "__main__":
    evaluate_macro_context(notify_user=True)
