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

RSS_FEEDS = [
    "https://news.google.com/rss/search?q=Nifty+Sensex+India+stock+market+today&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=RBI+repo+rate+inflation+India+economy&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=war+missile+attack+crude+oil+geopolitical+crisis&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=India+election+results+government+coalition+market&hl=en-IN&gl=IN&ceid=IN:en",
]

SEVERE_BEARISH_KEYWORDS = [
    "war", "missile", "airstrike", "invasion", "nuclear", "emergency",
    "market crash", "black monday", "circuit breaker", "coalition collapse",
    "election shock", "government falls", "terror attack", "oil shock", "sanctions"
]

EVENT_VOLATILITY_KEYWORDS = [
    "repo rate", "rbi policy", "monetary policy", "union budget",
    "election result", "exit poll", "fed rate", "fomc", "cpi inflation"
]

BULLISH_KEYWORDS = [
    "rate cut", "stimulus", "record high", "fii buying", "gdp surge",
    "landslide victory", "ceasefire", "peace deal", "rbi cuts"
]


def fetch_india_vix() -> float:
    if not UPSTOX_ACCESS_TOKEN:
        return 15.0
    try:
        url = "https://api.upstox.com/v2/market-quote/quotes"
        headers = {"Accept": "application/json", "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}"}
        res = requests.get(url, headers=headers, params={"instrument_key": "NSE_INDEX|India VIX"}, timeout=8).json()
        data = res.get("data", {})
        for v in data.values():
            ltp = float(v.get("last_price", 0.0))
            if ltp > 0:
                return round(ltp, 2)
    except Exception:
        pass
    return 15.0


def scan_news_headlines() -> list:
    headlines = []
    headers = {"User-Agent": "Mozilla/5.0"}
    for url in RSS_FEEDS:
        try:
            resp = requests.get(url, headers=headers, timeout=8)
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.text)
            for item in root.findall(".//item")[:6]:
                title_el = item.find("title")
                if title_el is not None and title_el.text:
                    headlines.append(title_el.text.strip())
        except Exception:
            continue
    return list(dict.fromkeys(headlines))[:20]


def evaluate_macro_context(notify_user: bool = True) -> dict:
    os.makedirs("live_journal", exist_ok=True)
    now_ist = datetime.now(IST)
    vix = fetch_india_vix()
    headlines = scan_news_headlines()

    bear_hits, event_hits, bull_hits = [], [], []
    matched_alerts = []

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

    veto_ce = False
    veto_pe = False
    event_caution = len(unique_event) >= 1
    threat_level = "NORMAL"
    summary_reason = "Normal Global & Domestic Cues"

    if len(unique_bear) >= 2 or vix >= 21.0 or ("war" in unique_bear and vix >= 17.5):
        threat_level = "CRASH_ALERT"
        veto_ce = True
        summary_reason = f"High Geopolitical/Crash Risk ({', '.join(unique_bear[:3]) or 'High VIX'}) -> CE VETOED"
    elif len(unique_bear) == 1 and vix >= 16.5:
        threat_level = "BEARISH_CAUTION"
        veto_ce = True
        summary_reason = f"Macro Headwind ({unique_bear[0]}) + VIX {vix} -> CE Blocked"
    elif len(unique_bull) >= 2 and len(unique_bear) == 0 and vix < 16.0:
        threat_level = "BULLISH_TAILWIND"
        summary_reason = f"Strong Positive Macro ({', '.join(unique_bull[:2])})"
    elif event_caution:
        threat_level = "EVENT_VOLATILITY"
        summary_reason = f"Major Event Day ({', '.join(unique_event[:2])}) -> Tight Risk Guard"

    context = {
        "updated_at": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
        "india_vix": vix,
        "threat_level": threat_level,
        "veto_ce": veto_ce,
        "veto_pe": veto_pe,
        "event_caution": event_caution,
        "bear_keywords": unique_bear,
        "event_keywords": unique_event,
        "bull_keywords": unique_bull,
        "summary": summary_reason,
        "top_alerts": matched_alerts[:3],
    }

    with open(MACRO_FILE, "w", encoding="utf-8") as f:
        json.dump(context, f, indent=2)

    log.info(f"[MACRO] Level={threat_level} | VIX={vix} | VetoCE={veto_ce} | {summary_reason}")

    if notify_user and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        alert_lines = "\n".join([f"  • <i>{a}</i>" for a in matched_alerts[:2]]) or "  • No severe war/crash headlines detected."
        msg = (
            f"🌍 <b>PRE-MARKET MACRO & NEWS RADAR</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📡 <b>Status:</b> <code>{threat_level}</code> │ <b>India VIX:</b> <code>{vix}</code>\n"
            f"🛡️ <b>Action:</b> {summary_reason}\n"
            f"📰 <b>Key Headlines:</b>\n{alert_lines}"
        )
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_notification": True},
                timeout=8,
            )
        except Exception:
            pass

    return context


if __name__ == "__main__":
    evaluate_macro_context(notify_user=True)
