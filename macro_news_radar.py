import os
import json
import time
import hashlib
import logging
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [MACRO_SECTOR_RADAR] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("MACRO_SECTOR_RADAR")

UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

MACRO_FILE = "live_journal/macro_context.json"
VIX_HISTORY_FILE = "live_journal/vix_baseline.json"
UNIVERSE_CACHE_FILE = "live_journal/dynamic_universe.json"
SEEN_NEWS_FILE = "live_journal/seen_news.json"
INSTRUMENTS_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"

FRESH_NEWS_MAX_AGE_SEC = 600


def upstox_headers():
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
    }


def send_telegram_alert(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not text:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception as e:
        log.warning(f"Telegram alert error: {e}")


def update_and_get_vix_baseline(today_str: str, current_vix: float) -> float:
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


def discover_live_market_universe(today_str: str) -> dict:
    if os.path.isfile(UNIVERSE_CACHE_FILE):
        try:
            with open(UNIVERSE_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("date") == today_str and cached.get("sectors") and cached.get("fo_equities"):
                return cached
        except Exception:
            pass

    universe = {"date": today_str, "vix_key": "", "sectors": {}, "fo_equities": {}}
    try:
        df = pd.read_csv(INSTRUMENTS_MASTER_URL, compression="gzip", low_memory=False)
        df.columns = [str(c).strip().lower() for c in df.columns]
        sym_col = "tradingsymbol" if "tradingsymbol" in df.columns else "trading_symbol"

        idx_df = df[df["exchange"] == "NSE_INDEX"].copy()
        for _, r in idx_df.iterrows():
            name = str(r.get("name", r.get(sym_col, ""))).strip()
            ikey = str(r.get("instrument_key", "")).strip()
            if not name or not ikey:
                continue
            low_name = name.lower()
            if "vix" in low_name:
                universe["vix_key"] = ikey
            elif low_name.startswith("nifty ") and not any(
                skip in low_name for skip in ("50", "100", "200", "500", "next", "midcap", "smallcap", "alpha", "beta", "value", "dividend", "vix", "bees")
            ):
                clean_sec = name.replace("Nifty ", "").replace("NIFTY ", "").upper().strip()
                if len(universe["sectors"]) < 14:
                    universe["sectors"][clean_sec] = ikey

        fo_df = df[(df["exchange"] == "NSE_FO") & (df["instrument_type"] == "FUTSTK")].copy()
        fo_syms = set(fo_df["name"].dropna().astype(str).str.upper().str.strip())

        eq_df = df[(df["exchange"] == "NSE_EQ") & (df["instrument_type"] == "EQUITY")].copy()
        for _, r in eq_df.iterrows():
            sym = str(r.get(sym_col, "")).upper().strip()
            ikey = str(r.get("instrument_key", "")).strip()
            if sym in fo_syms and ikey:
                universe["fo_equities"][sym] = ikey

        os.makedirs("live_journal", exist_ok=True)
        with open(UNIVERSE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(universe, f, indent=2)
    except Exception as e:
        log.warning(f"Universe discovery error: {e}")

    return universe


def fetch_quotes_batched(keys: list) -> dict:
    merged = {}
    clean_keys = [k for k in dict.fromkeys(keys) if k]
    for i in range(0, len(clean_keys), 100):
        batch = clean_keys[i : i + 100]
        try:
            res = requests.get(
                "https://api.upstox.com/v2/market-quote/quotes",
                headers=upstox_headers(),
                params={"instrument_key": ",".join(batch)},
                timeout=10,
            ).json()
            for k, v in res.get("data", {}).items():
                merged[k] = v
                merged[k.replace(":", "|")] = v
                if v.get("instrument_token"):
                    merged[v["instrument_token"]] = v
        except Exception:
            continue
    return merged


def fetch_sectors_and_dynamic_leaders(today_str: str) -> dict:
    result = {
        "vix": 0.0,
        "vix_chg_pct": 0.0,
        "sector_perf": {},
        "green_sectors": 0,
        "red_sectors": 0,
        "nifty_heavy_impact": 0.0,
        "top_pullers": [],
        "top_draggers": [],
        "top_leader_keys": [],
        "sector_verdict": "NEUTRAL",
    }
    if not UPSTOX_ACCESS_TOKEN:
        return result

    universe = discover_live_market_universe(today_str)
    vix_key = universe.get("vix_key", "")
    sec_map = universe.get("sectors", {})
    eq_map = universe.get("fo_equities", {})

    rev_sec = {v: k for k, v in sec_map.items()}
    rev_eq = {v: k for k, v in eq_map.items()}

    all_keys = ([vix_key] if vix_key else []) + list(sec_map.values()) + list(eq_map.values())
    raw_data = fetch_quotes_batched(all_keys)
    if not raw_data:
        return result

    equity_metrics = []

    for k, v in raw_data.items():
        norm_k = k.replace(":", "|")
        tok = v.get("instrument_token", norm_k)
        ltp = float(v.get("last_price", 0.0))
        prev_c = float(v.get("ohlc", {}).get("close", ltp))
        vol = float(v.get("volume", 0.0))
        if ltp <= 0 or prev_c <= 0:
            continue
        chg_pct = round(((ltp - prev_c) / prev_c) * 100.0, 2)

        if vix_key and (norm_k == vix_key or tok == vix_key):
            result["vix"] = round(ltp, 2)
            result["vix_chg_pct"] = chg_pct
        elif norm_k in rev_sec or tok in rev_sec:
            sec_name = rev_sec.get(norm_k) or rev_sec.get(tok)
            result["sector_perf"][sec_name] = chg_pct
        elif norm_k in rev_eq or tok in rev_eq:
            sym = rev_eq.get(norm_k) or rev_eq.get(tok)
            ikey = eq_map.get(sym, norm_k)
            turnover = ltp * max(vol, 1.0)
            equity_metrics.append({"sym": sym, "ikey": ikey, "chg_pct": chg_pct, "turnover": turnover})

    equity_metrics.sort(key=lambda x: x["turnover"], reverse=True)
    top_leaders = equity_metrics[:25]
    result["top_leader_keys"] = [x["ikey"] for x in top_leaders if x.get("ikey")]

    top_15 = top_leaders[:15]
    total_turnover = sum(x["turnover"] for x in top_15) or 1.0

    weighted_market_impact = 0.0
    stock_impacts = []
    for item in top_15:
        dyn_weight = (item["turnover"] / total_turnover) * 100.0
        imp = round((item["chg_pct"] * dyn_weight) / 100.0, 3)
        weighted_market_impact += imp
        stock_impacts.append((item["sym"], item["chg_pct"], imp))

    green_sec = sum(1 for v in result["sector_perf"].values() if v > 0.15)
    red_sec = sum(1 for v in result["sector_perf"].values() if v < -0.15)
    result["green_sectors"] = green_sec
    result["red_sectors"] = red_sec
    result["nifty_heavy_impact"] = round(weighted_market_impact, 2)

    stock_impacts.sort(key=lambda x: x[2], reverse=True)
    result["top_pullers"] = [f"{s}({c:+.1f}%)" for s, c, imp in stock_impacts[:3] if imp > 0]
    result["top_draggers"] = [f"{s}({c:+.1f}%)" for s, c, imp in reversed(stock_impacts[-3:]) if imp < 0]

    total_secs = max(len(result["sector_perf"]), 1)
    majority_secs = max(2, int(round(total_secs * 0.70)))

    if green_sec >= majority_secs and weighted_market_impact >= 0.35:
        result["sector_verdict"] = "BROAD_SECTOR_RALLY_BULL"
    elif red_sec >= majority_secs and weighted_market_impact <= -0.35:
        result["sector_verdict"] = "BROAD_SECTOR_SELLOFF_BEAR"
    elif abs(green_sec - red_sec) <= 2:
        result["sector_verdict"] = "SECTOR_TUG_OF_WAR_CHOP"
    else:
        result["sector_verdict"] = "DIVERGENT_FLOW"

    return result


def load_seen_news() -> dict:
    if os.path.isfile(SEEN_NEWS_FILE):
        try:
            with open(SEEN_NEWS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_seen_news(seen: dict):
    os.makedirs("live_journal", exist_ok=True)
    cutoff = time.time() - 86400
    pruned = {h: ts for h, ts in seen.items() if float(ts) >= cutoff}
    with open(SEEN_NEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(pruned, f, indent=2)


def fetch_fresh_upstox_news(leader_keys: list) -> list:
    if not UPSTOX_ACCESS_TOKEN:
        return []

    seen = load_seen_news()
    now_ms = int(time.time() * 1000)
    max_age_ms = FRESH_NEWS_MAX_AGE_SEC * 1000
    fresh_articles = []

    requests_to_make = [{"category": "positions", "page_number": 1, "page_size": 20}]
    if leader_keys:
        requests_to_make.append({
            "category": "instrument_keys",
            "instrument_keys": ",".join(leader_keys[:30]),
            "page_number": 1,
            "page_size": 30,
        })

    for params in requests_to_make:
        try:
            resp = requests.get(
                "https://api.upstox.com/v2/news",
                headers=upstox_headers(),
                params=params,
                timeout=8,
            ).json()
            if resp.get("status") != "success":
                continue
            data_map = resp.get("data", {})
            if not isinstance(data_map, dict):
                continue

            for ikey, articles in data_map.items():
                if not isinstance(articles, list):
                    continue
                for art in articles:
                    heading = str(art.get("heading", "")).strip()
                    summary = str(art.get("summary", "")).strip()
                    pub_ms = int(art.get("published_time", 0) or 0)
                    if not heading or pub_ms <= 0:
                        continue
                    if (now_ms - pub_ms) > max_age_ms or (now_ms - pub_ms) < -60000:
                        continue

                    h_id = hashlib.sha256(heading.encode("utf-8")).hexdigest()
                    if h_id in seen:
                        continue

                    seen[h_id] = time.time()
                    fresh_articles.append({
                        "instrument_key": ikey,
                        "heading": heading,
                        "summary": summary,
                        "age_sec": max(0, int((now_ms - pub_ms) / 1000)),
                    })
        except Exception as e:
            log.warning(f"Upstox /v2/news error ({params.get('category')}): {e}")

    if fresh_articles:
        save_seen_news(seen)
    return fresh_articles


def generate_autonomous_desk_brief(sec_data: dict, fii_dii: dict, articles: list, live_pcrs: dict) -> dict:
    """
    100% Zero-Hardcode Autonomous Desk Brain:
    Evaluates live sector breadth, VIX velocity, all live Option Chain PCRs, FII/DII flows,
    and breaking news to dynamically output symbol-level & side-level execution permissions.
    """
    if not GEMINI_API_KEY:
        return {}

    headlines_blob = (
        "\n".join(f"- [{a['age_sec']}s ago] {a['heading']}" for a in articles[:6])
        if articles
        else "None in last 10 mins"
    )

    prompt = (
        "You are an unemotional, cold-blooded institutional Indian derivatives risk manager.\n"
        "Analyze the live market telemetry below and output dynamic execution permissions across all active symbols:\n"
        f"- India VIX: {sec_data['vix']} ({sec_data['vix_chg_pct']:+.2f}%)\n"
        f"- All Live Sectors (% Chg): {json.dumps(sec_data['sector_perf'])}\n"
        f"- Heavyweight Flow Impact: {sec_data['nifty_heavy_impact']:+.2f}%\n"
        f"- Top Pullers: {', '.join(sec_data['top_pullers']) or 'None'} | Top Draggers: {', '.join(sec_data['top_draggers']) or 'None'}\n"
        f"- Live Option Chain PCRs: {json.dumps(live_pcrs)}\n"
        f"- FII/DII Cash Flow: FII ₹{fii_dii['fii_net_cr']} Cr | DII ₹{fii_dii['dii_net_cr']} Cr\n"
        f"- Fresh Breaking News (<10m): {headlines_blob}\n\n"
        "Instructions:\n"
        "1. Decide dynamically if any side ('CE' or 'PE') should be globally blocked ('blocked_sides') due to extreme PCR, sector sweep, or shock.\n"
        "2. Decide dynamically if specific symbol:side pairs (e.g. 'NIFTY:CE', 'BANKNIFTY:CE', 'FINNIFTY:PE') should be blocked ('blocked_pairs') or prioritized ('priority_pairs') based on sector leadership/drag.\n"
        "3. Populate 'eject_sides' ONLY if an immediate breaking macro shock requires emergency exit of open 'CE' or 'PE' positions.\n"
        "4. Set 'force_scalp' to true if sectors are in a tug-of-war / sideways chop.\n"
        "5. Write 'desk_note' as a sharp, natural 2-sentence institutional note explaining the exact numbers and focus—zero hype, zero emotion.\n"
        "Respond ONLY with valid JSON:\n"
        '{"is_shock": true/false, "eject_sides": [], "blocked_sides": [], "blocked_pairs": [], '
        '"priority_pairs": [], "force_scalp": true/false, "stance_label": "DYNAMIC_LABEL", "desk_note": "..."}'
    )

    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
        res = requests.post(
            url,
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=10,
        ).json()
        raw_text = (
            res.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
            .strip()
        )
        clean_json = raw_text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(clean_json)
        return parsed if isinstance(parsed, dict) else {}
    except Exception as e:
        log.warning(f"Gemini autonomous desk brief warning: {e}")
        return {}


def fetch_fii_dii_activity() -> dict:
    result = {"fii_net_cr": 0.0, "dii_net_cr": 0.0, "flow_regime": "NEUTRAL"}
    if not UPSTOX_ACCESS_TOKEN:
        return result
    try:
        fii_res = requests.get(
            "[https://api.upstox.com/v2/market/fii](https://api.upstox.com/v2/market/fii)",
            headers=upstox_headers(),
            params=[("data_type", "NSE_EQ|CASH"), ("interval", "1D")],
            timeout=8,
        ).json()
        cash_recs = fii_res.get("data", {}).get("NSE_EQ|CASH", [])
        if cash_recs:
            latest_fii = sorted(cash_recs, key=lambda x: x.get("time_stamp", 0))[-1]
            result["fii_net_cr"] = round(float(latest_fii.get("net_value", latest_fii.get("netValue", 0.0))), 2)

        dii_res = requests.get(
            "[https://api.upstox.com/v2/market/dii](https://api.upstox.com/v2/market/dii)",
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


def evaluate_macro_context(notify_user: bool = False, live_pcrs: dict = None) -> dict:
    os.makedirs("live_journal", exist_ok=True)
    now_ist = datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")
    live_pcrs = live_pcrs or {}

    prev_context = {}
    if os.path.isfile(MACRO_FILE):
        try:
            with open(MACRO_FILE, "r", encoding="utf-8") as f:
                prev_context = json.load(f)
        except Exception:
            pass

    sec_data = fetch_sectors_and_dynamic_leaders(today_str)
    vix, vix_chg = sec_data["vix"], sec_data["vix_chg_pct"]
    vix_baseline = update_and_get_vix_baseline(today_str, vix)
    vix_above_baseline_pct = round(((vix - vix_baseline) / max(vix_baseline, 1.0)) * 100.0, 1) if vix > 0 else 0.0

    fii_dii = fetch_fii_dii_activity()
    fresh_news = fetch_fresh_upstox_news(sec_data.get("top_leader_keys", []))

    blocked_sides = set()
    eject_sides = set()
    blocked_pairs = set()
    priority_pairs = set()
    force_scalp = (sec_data["sector_verdict"] == "SECTOR_TUG_OF_WAR_CHOP")
    vix_cooling_decay = (vix_chg <= -2.5)
    event_caution = vix_cooling_decay or force_scalp
    threat_level = "NORMAL"

    if vix_above_baseline_pct >= 30.0 or vix_chg >= 7.0:
        threat_level = "VIX_SHOCK_ALERT"
        blocked_sides.add("CE")
        eject_sides.add("CE")

    prev_verdict = prev_context.get("sector_verdict", "")
    regime_shifted = (sec_data["sector_verdict"] != prev_verdict) or (threat_level != prev_context.get("threat_level", ""))

    ai_brief = prev_context.get("ai_brief", "")
    stance_label = prev_context.get("stance_label", "ADAPTIVE_TWO_WAY")

    if fresh_news or regime_shifted or notify_user or not prev_context:
        ai_eval = generate_autonomous_desk_brief(sec_data, fii_dii, fresh_news, live_pcrs)
        if ai_eval:
            ai_brief = str(ai_eval.get("desk_note", "")).strip() or ai_brief
            stance_label = str(ai_eval.get("stance_label", stance_label)).upper().strip()
            force_scalp = bool(ai_eval.get("force_scalp", force_scalp))

            for s in ai_eval.get("blocked_sides", []):
                blocked_sides.add(str(s).upper().strip())
            for s in ai_eval.get("eject_sides", []):
                eject_sides.add(str(s).upper().strip())
            for p in ai_eval.get("blocked_pairs", []):
                blocked_pairs.add(str(p).upper().strip())
            for p in ai_eval.get("priority_pairs", []):
                priority_pairs.add(str(p).upper().strip())

            if ai_eval.get("is_shock"):
                threat_level = "AI_BREAKING_SHOCK"

            if ai_brief and (fresh_news or (regime_shifted and prev_verdict)):
                send_telegram_alert(
                    f"🧠 <b>AI DESK DECISION • {now_ist.strftime('%H:%M')} IST</b>\n"
                    f"────────────────────────────\n"
                    f"🎯 <b>Stance:</b> <code>{stance_label}</code>  |  <b>VIX:</b> <code>{vix} ({vix_chg:+.1f}%)</code>\n"
                    f"<i>{ai_brief}</i>"
                )
    else:
        blocked_sides.update(prev_context.get("blocked_sides", []))
        blocked_pairs.update(prev_context.get("blocked_pairs", []))
        priority_pairs.update(prev_context.get("priority_pairs", []))
        force_scalp = bool(prev_context.get("force_scalp", force_scalp))

    summary_reason = (
        f"Stance {stance_label} | Sectors {sec_data['green_sectors']}G/{sec_data['red_sectors']}R | "
        f"Flow {sec_data['nifty_heavy_impact']:+.2f}%"
    )

    context = {
        "updated_at": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
        "india_vix": vix,
        "vix_baseline_20d": vix_baseline,
        "vix_chg_pct": vix_chg,
        "fii_dii": fii_dii,
        "sector_perf": sec_data["sector_perf"],
        "nifty_heavy_impact": sec_data["nifty_heavy_impact"],
        "sector_verdict": sec_data["sector_verdict"],
        "top_pullers": sec_data["top_pullers"],
        "top_draggers": sec_data["top_draggers"],
        "threat_level": threat_level,
        "stance_label": stance_label,
        "blocked_sides": sorted(blocked_sides),
        "eject_sides": sorted(eject_sides),
        "blocked_pairs": sorted(blocked_pairs),
        "priority_pairs": sorted(priority_pairs),
        "force_scalp": force_scalp,
        "event_caution": event_caution,
        "summary": summary_reason,
        "ai_brief": ai_brief,
    }

    with open(MACRO_FILE, "w", encoding="utf-8") as f:
        json.dump(context, f, indent=2)

    log.info(f"[RADAR] {threat_level} | Stance={stance_label} | Blocked={sorted(blocked_pairs)} | {summary_reason}")
    return context


if __name__ == "__main__":
    evaluate_macro_context(notify_user=False)
