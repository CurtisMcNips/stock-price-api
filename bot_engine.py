"""
Market Brain AI Bot — Engine
Runs as a separate service alongside the FastAPI backend.

- Logs in to Market Brain API
- Fetches live prices every 5 minutes
- Applies rule-based filters to select trades/watches
- Calls Claude AI to reason about each pick and write notes
- Tracks positions, scores accuracy, raises alerts
- Exposes a /bot/* REST API for the dashboard

Requirements:
    pip install fastapi uvicorn httpx anthropic apscheduler aiofiles python-dotenv

Environment variables (.env or Railway):
    MB_API_URL      = https://your-market-brain-app.railway.app
    MB_BOT_EMAIL    = bot@marketbrain.ai
    MB_BOT_PASSWORD = <bot account password>
    ANTHROPIC_API_KEY = sk-ant-...
    BOT_BUDGET      = 10000          # virtual starting balance
    BOT_RISK_PCT    = 0.02           # 2% per trade
    BOT_MIN_RR      = 2.0            # minimum risk/reward
    BOT_MIN_SCORE   = 55             # minimum signal score
    BOT_MIN_TF_SCORE= 65             # minimum timeframe alignment %
    ALERT_THRESHOLD = -10            # alert when position drops this %
    BOT_PORT        = 8001
"""

import asyncio
import json
import logging
import os
import time
import math
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path
from dotenv import load_dotenv

import httpx
from anthropic import Anthropic
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mb-bot")

# ── Config ─────────────────────────────────────────────────────
MB_API_URL       = os.getenv("MB_API_URL", "http://localhost:8000")
MB_BOT_EMAIL     = os.getenv("MB_BOT_EMAIL", "bot@marketbrain.ai")
MB_BOT_PASSWORD  = os.getenv("MB_BOT_PASSWORD", "botpassword123")
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
BOT_BUDGET       = float(os.getenv("BOT_BUDGET", "10000"))
BOT_RISK_PCT     = float(os.getenv("BOT_RISK_PCT", "0.02"))
BOT_MIN_RR       = float(os.getenv("BOT_MIN_RR", "2.0"))
BOT_MIN_SCORE    = float(os.getenv("BOT_MIN_SCORE", "55"))
BOT_MIN_TF_SCORE = float(os.getenv("BOT_MIN_TF_SCORE", "65"))
ALERT_THRESHOLD  = float(os.getenv("ALERT_THRESHOLD", "-10"))
BOT_PORT         = int(os.getenv("BOT_PORT", "8001"))
MAX_OPEN_TRADES  = 8
SCAN_INTERVAL    = 300  # seconds between scans
DATA_FILE        = Path("bot_state.json")

# ── All assets mirrored from index.html ────────────────────────
ASSETS = [
    {"ticker":"NVDA","name":"NVIDIA","sector":"Technology","sub":"Semiconductors","cap":"Large","vol":"Low","priceRange":[100,200]},
    {"ticker":"AMD","name":"AMD","sector":"Technology","sub":"Semiconductors","cap":"Large","vol":"Med","priceRange":[80,180]},
    {"ticker":"MSFT","name":"Microsoft","sector":"Technology","sub":"Software","cap":"Large","vol":"Low","priceRange":[300,500]},
    {"ticker":"GOOGL","name":"Alphabet","sector":"Technology","sub":"Software","cap":"Large","vol":"Low","priceRange":[140,200]},
    {"ticker":"META","name":"Meta Platforms","sector":"Technology","sub":"Social Media","cap":"Large","vol":"Med","priceRange":[300,600]},
    {"ticker":"AAPL","name":"Apple","sector":"Technology","sub":"Hardware","cap":"Large","vol":"Low","priceRange":[150,230]},
    {"ticker":"SOUN","name":"SoundHound AI","sector":"Technology","sub":"AI / ML","cap":"Micro","vol":"VHigh","priceRange":[3,18]},
    {"ticker":"CRWD","name":"CrowdStrike","sector":"Technology","sub":"Cybersecurity","cap":"Large","vol":"Med","priceRange":[200,380]},
    {"ticker":"IONQ","name":"IonQ","sector":"Technology","sub":"Quantum","cap":"Small","vol":"High","priceRange":[6,40]},
    {"ticker":"MRNA","name":"Moderna","sector":"Healthcare","sub":"Pharma","cap":"Mid","vol":"High","priceRange":[35,130]},
    {"ticker":"PFE","name":"Pfizer","sector":"Healthcare","sub":"Pharma","cap":"Large","vol":"Low","priceRange":[20,50]},
    {"ticker":"JNJ","name":"J&J","sector":"Healthcare","sub":"Pharma","cap":"Large","vol":"Low","priceRange":[140,175]},
    {"ticker":"ISRG","name":"Intuitive Surgical","sector":"Healthcare","sub":"MedTech","cap":"Large","vol":"Low","priceRange":[300,450]},
    {"ticker":"XOM","name":"ExxonMobil","sector":"Energy","sub":"Oil & Gas","cap":"Large","vol":"Low","priceRange":[95,130]},
    {"ticker":"CVX","name":"Chevron","sector":"Energy","sub":"Oil & Gas","cap":"Large","vol":"Low","priceRange":[130,175]},
    {"ticker":"FSLR","name":"First Solar","sector":"Energy","sub":"Solar","cap":"Mid","vol":"Med","priceRange":[100,250]},
    {"ticker":"NEE","name":"NextEra Energy","sector":"Energy","sub":"Renewables","cap":"Large","vol":"Low","priceRange":[50,85]},
    {"ticker":"GLD","name":"SPDR Gold ETF","sector":"Metals","sub":"Gold","cap":"Large","vol":"Low","priceRange":[170,230]},
    {"ticker":"FCX","name":"Freeport-McMoRan","sector":"Metals","sub":"Copper","cap":"Large","vol":"Med","priceRange":[35,60]},
    {"ticker":"LAC","name":"Lithium Americas","sector":"Minerals","sub":"Lithium","cap":"Small","vol":"VHigh","priceRange":[2,20]},
    {"ticker":"ALB","name":"Albemarle","sector":"Minerals","sub":"Lithium","cap":"Mid","vol":"High","priceRange":[40,150]},
    {"ticker":"UEC","name":"Uranium Energy","sector":"Minerals","sub":"Uranium","cap":"Small","vol":"High","priceRange":[4,12]},
    {"ticker":"BHP","name":"BHP Group","sector":"Minerals","sub":"Mining","cap":"Large","vol":"Low","priceRange":[40,70]},
    {"ticker":"BTC-USD","name":"Bitcoin","sector":"Crypto","sub":"Layer 1","cap":"Large","vol":"Extreme","priceRange":[25000,100000]},
    {"ticker":"ETH-USD","name":"Ethereum","sector":"Crypto","sub":"Layer 1","cap":"Large","vol":"Extreme","priceRange":[1500,5000]},
    {"ticker":"SOL-USD","name":"Solana","sector":"Crypto","sub":"Layer 1","cap":"Mid","vol":"Extreme","priceRange":[20,250]},
    {"ticker":"COIN","name":"Coinbase","sector":"Crypto","sub":"Exchange","cap":"Mid","vol":"VHigh","priceRange":[100,330]},
    {"ticker":"RKLB","name":"Rocket Lab","sector":"Space","sub":"Launch","cap":"Small","vol":"High","priceRange":[4,26]},
    {"ticker":"LMT","name":"Lockheed Martin","sector":"Space","sub":"Defence","cap":"Large","vol":"Low","priceRange":[400,550]},
    {"ticker":"AMZN","name":"Amazon","sector":"Consumer","sub":"E-Commerce","cap":"Large","vol":"Med","priceRange":[120,220]},
    {"ticker":"TSLA","name":"Tesla","sector":"Consumer","sub":"EV","cap":"Large","vol":"High","priceRange":[150,400]},
    {"ticker":"NFLX","name":"Netflix","sector":"Consumer","sub":"Streaming","cap":"Large","vol":"Med","priceRange":[400,800]},
    {"ticker":"JPM","name":"JPMorgan","sector":"Finance","sub":"Banking","cap":"Large","vol":"Low","priceRange":[150,230]},
    {"ticker":"GS","name":"Goldman Sachs","sector":"Finance","sub":"Banking","cap":"Large","vol":"Med","priceRange":[350,550]},
    {"ticker":"V","name":"Visa","sector":"Finance","sub":"Payments","cap":"Large","vol":"Low","priceRange":[220,310]},
]

TF_PROFILES = {
    "⚡ Intraday":     {"short":"0-24h",  "simDays":0.5},
    "📈 Short Swing":  {"short":"2-5d",   "simDays":3.5},
    "🌊 Medium Swing": {"short":"1-4wk",  "simDays":17.5},
    "🏔️ Position":    {"short":"1-6mo",  "simDays":105},
    "🌳 Long Term":    {"short":"6mo+",   "simDays":548},
}

CAP_TIERS = {"Nano":0,"Micro":1,"Small":2,"Mid":3,"Large":4}

# ── State ──────────────────────────────────────────────────────
state = {
    "token": None,
    "balance": BOT_BUDGET,
    "start_balance": BOT_BUDGET,
    "open_trades": [],        # active virtual positions
    "closed_trades": [],      # completed positions
    "watch_items": [],        # timed watches
    "notes": [],              # AI-generated reasoning notes
    "alerts": [],             # triggered alerts
    "accuracy": {},           # running accuracy stats
    "scan_count": 0,
    "last_scan": None,
    "started_at": datetime.now(timezone.utc).isoformat(),
    "status": "starting",
    "live_prices": {},
}

def save_state():
    try:
        DATA_FILE.write_text(json.dumps(state, indent=2, default=str))
    except Exception as e:
        log.error(f"State save failed: {e}")

def load_state():
    global state
    if DATA_FILE.exists():
        try:
            saved = json.loads(DATA_FILE.read_text())
            state.update(saved)
            log.info("State restored from disk")
        except Exception as e:
            log.warning(f"Could not load state: {e}")

# ── Signal Engine (mirrors index.html logic) ───────────────────
def sr(seed, mn, mx):
    import math
    x = math.sin(seed + 1) * 10000
    return mn + (x - math.floor(x)) * (mx - mn)

def generate_signals(asset, key):
    s = sum(ord(c) for c in asset["ticker"]) + key
    def r(mn, mx, o=0): return sr(s + o, mn, mx)

    cap_tier = CAP_TIERS.get(asset["cap"], 2)
    rsi       = r(18, 82, 1)
    macd      = r(-1, 1, 2)
    volume    = r(0.4, 4.0, 3)
    sentiment = r(-1, 1, 4)
    short_int = r(0, 35, 5)
    earn_beat = r(-25, 40, 6)
    p_vs_50   = r(-30, 40, 7)
    p_vs_200  = r(-40, 50, 8)
    insider   = r(0, 1, 9)
    catalyst  = r(-1, 1, 10)
    sector_f  = r(-1, 1, 11)
    days_earn = r(0, 90, 13)
    debt_r    = r(0, 3, 14)
    rev_g     = r(-20, 120, 15)

    score = 0
    score += 22 if rsi < 30 else (-18 if rsi > 72 else (50 - rsi) * 0.3)
    score += macd * 18
    score += (volume - 1) * 10
    score += sentiment * 22
    score += -18 if short_int > 25 else (12 if short_int < 5 else 0)
    score += earn_beat * 0.5
    score += 14 if p_vs_50 < -20 else (-10 if p_vs_50 > 30 else 0)
    score += 18 if insider > 0.7 else 0
    score += catalyst * 18
    score += sector_f * 12
    score += 18 if rev_g > 50 else (-12 if rev_g < -10 else 0)
    score += -14 if debt_r > 2 else 0
    if asset["sector"] == "Technology" and sector_f > 0.3: score += 15
    if asset["sector"] == "Crypto": score += 18 if r(0,1,34) > 0.5 else -14
    score = max(-100, min(100, score))

    risk = ("CRITICAL" if score < -55 else "HIGH" if score < -15 else
            "MODERATE" if score < 18 else "POSITIVE" if score < 55 else "STRONG")

    price = sr(s + 42, asset["priceRange"][0], asset["priceRange"][1])
    stop_pct = (0.15 if cap_tier <= 1 else 0.10 if cap_tier == 2 else 0.07)
    max_up = (400 if cap_tier == 0 else 200 if cap_tier == 1 else
              100 if cap_tier == 2 else 50 if cap_tier == 3 else 30)
    upside = (score / 100) * max_up if score > 0 else 0
    rr = round((upside / 100) / stop_pct, 1) if stop_pct > 0 else 0

    vol_mod = {"Low":0.6,"Med":0.8,"High":1.0,"VHigh":1.2,"Extreme":1.5}.get(asset["vol"], 1)

    tf_scores = {}
    tf_scores["⚡ Intraday"]    = max(5, min(95, (max(0,(volume-1.5)*35)+(25 if short_int>20 and volume>2 else 0)+(12 if 28<rsi<52 else 0)+(20 if catalyst>0.5 else 0)+(8 if macd>0.3 else 0)-(15 if rsi>70 else 0))*vol_mod))
    tf_scores["📈 Short Swing"] = max(5, min(95, (22 if macd>0 else 0)+(20 if 25<rsi<45 else 0)+(15 if volume>1.3 else 0)+(18 if earn_beat>10 else 0)+(12 if p_vs_50<-10 else 0)+(10 if catalyst>0 else 0)-(10 if debt_r>2 else 0)))
    tf_scores["🌊 Medium Swing"]= max(5, min(95, (22 if sector_f>0.2 else 0)+(20 if rev_g>20 else 0)+(14 if macd>0 else 0)+(10 if volume>1.1 else 0)+(15 if p_vs_200<-15 else 0)+(12 if insider>0.5 else 0)+(14 if days_earn<30 else 0)-(12 if debt_r>2 else 0)))
    tf_scores["🏔️ Position"]   = max(5, min(95, (28 if rev_g>30 else 16 if rev_g>10 else 0)+(22 if insider>0.65 else 0)+(18 if debt_r<1 else 8 if debt_r<2 else 0)+(15 if sector_f>0.3 else 0)+(10 if cap_tier>=2 else 0)))
    tf_scores["🌳 Long Term"]   = max(5, min(95, (25 if rev_g>20 else 0)+(20 if debt_r<1.5 else 0)+(22 if cap_tier>=3 else 12 if cap_tier==2 else 0)+(15 if insider>0.5 else 0)+(10 if sector_f>0 else 0)))

    best_tf = max(tf_scores, key=tf_scores.get)

    return {
        "score": round(score),
        "risk": risk,
        "price": round(price, 4),
        "upside_pct": round(upside, 1),
        "stop_pct": round(stop_pct * 100, 1),
        "rr_ratio": rr,
        "tf_scores": tf_scores,
        "best_tf": best_tf,
        "metrics": {"rsi": rsi, "macd": macd, "volume": volume,
                    "sentiment": sentiment, "rev_growth": rev_g, "debt_ratio": debt_r},
    }

# ── Rules Filter ───────────────────────────────────────────────
def passes_rules(asset, sig):
    """Hard rules — must all pass before AI is consulted."""
    if sig["score"] < BOT_MIN_SCORE:                   return False, "Score below threshold"
    if sig["rr_ratio"] < BOT_MIN_RR:                   return False, "R/R below minimum"
    best_score = sig["tf_scores"][sig["best_tf"]]
    if best_score < BOT_MIN_TF_SCORE:                  return False, "TF alignment too low"
    if sig["risk"] in ("CRITICAL", "HIGH"):             return False, "Risk rating too high"
    already_open = [t for t in state["open_trades"] if t["ticker"] == asset["ticker"]]
    if already_open:                                   return False, "Already in position"
    if len(state["open_trades"]) >= MAX_OPEN_TRADES:   return False, "Max open trades reached"
    if sig["price"] <= 0:                              return False, "No valid price"
    shares_possible = math.floor((state["balance"] * BOT_RISK_PCT) /
                                  (sig["price"] * sig["stop_pct"] / 100))
    if shares_possible < 1:                            return False, "Insufficient balance for 1 share"
    return True, "Passed"

# ── Claude AI Commentary ───────────────────────────────────────
anthropic_client = Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

async def get_ai_commentary(asset, sig, action, live_price=None):
    """Ask Claude to reason about this pick and write a note."""
    if not anthropic_client:
        return {
            "reasoning": "AI commentary disabled — set ANTHROPIC_API_KEY.",
            "confidence": "N/A",
            "concerns": [],
            "verdict": action,
        }

    price_note = f"Live price: ${live_price:.4f}." if live_price else f"Simulated price: ${sig['price']:.4f}."
    prompt = f"""You are an independent analyst reviewing a trade signal generated by the Market Brain signal engine.

Asset: {asset['ticker']} — {asset['name']} ({asset['sector']} / {asset['sub']}, {asset['cap']} Cap)
Action proposed: {action}
{price_note}
Signal score: {sig['score']}/100  |  Risk rating: {sig['risk']}
Best timeframe: {sig['best_tf']} (score: {sig['tf_scores'][sig['best_tf']]:.0f}%)
R/R ratio: {sig['rr_ratio']}:1  |  Upside: +{sig['upside_pct']}%  |  Stop loss: -{sig['stop_pct']}%
RSI: {sig['metrics']['rsi']:.1f}  |  MACD: {sig['metrics']['macd']:.2f}  |  Volume: {sig['metrics']['volume']:.1f}x
Revenue growth: {sig['metrics']['rev_growth']:.1f}%  |  Debt ratio: {sig['metrics']['debt_ratio']:.2f}
Volatility profile: {asset['vol']}

In 3-4 sentences, provide:
1. Your genuine reasoning on whether this signal makes sense
2. Any concerns or factors the engine may be missing
3. Your confidence level (HIGH / MEDIUM / LOW) and why
4. A one-line verdict (AGREE / AGREE WITH CAUTION / DISAGREE)

Be critical and specific. Do not just parrot the signal engine's metrics. Think like a skeptical analyst."""

    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        ))
        text = response.content[0].text.strip()

        confidence = "MEDIUM"
        for line in text.split("\n"):
            if "HIGH" in line and "confidence" in line.lower(): confidence = "HIGH"; break
            if "LOW" in line and "confidence" in line.lower(): confidence = "LOW"; break

        verdict = "AGREE"
        if "DISAGREE" in text: verdict = "DISAGREE"
        elif "CAUTION" in text: verdict = "AGREE WITH CAUTION"

        concerns = []
        for line in text.split("."):
            if any(w in line.lower() for w in ["risk","concern","however","but","caution","miss","gap","limit"]):
                c = line.strip()
                if c: concerns.append(c[:120])

        return {"reasoning": text, "confidence": confidence,
                "concerns": concerns[:3], "verdict": verdict}
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return {"reasoning": f"AI error: {e}", "confidence": "N/A",
                "concerns": [], "verdict": action}

# ── Market Brain API Client ─────────────────────────────────────
async def mb_login():
    """Register/login the bot account."""
    async with httpx.AsyncClient(timeout=15) as client:
        # Try register first
        try:
            r = await client.post(f"{MB_API_URL}/api/auth/register", json={
                "name": "Market Brain Bot",
                "email": MB_BOT_EMAIL,
                "password": MB_BOT_PASSWORD,
            })
            if r.status_code in (200, 201):
                state["token"] = r.json()["token"]
                log.info("Bot account registered")
                return True
        except Exception: pass

        # Fall back to login
        try:
            r = await client.post(f"{MB_API_URL}/api/auth/login", json={
                "email": MB_BOT_EMAIL,
                "password": MB_BOT_PASSWORD,
            })
            if r.status_code == 200:
                state["token"] = r.json()["token"]
                log.info("Bot logged in")
                return True
        except Exception as e:
            log.error(f"Login failed: {e}")
    return False

async def fetch_live_prices():
    """Pull live prices from Market Brain's price API."""
    tickers = [a["ticker"] for a in ASSETS]
    prices = {}
    async with httpx.AsyncClient(timeout=20) as client:
        for i in range(0, len(tickers), 40):
            chunk = tickers[i:i+40]
            try:
                r = await client.get(f"{MB_API_URL}/api/prices",
                                     params={"symbols": ",".join(chunk)})
                if r.status_code == 200:
                    prices.update(r.json().get("data", {}))
            except Exception as e:
                log.warning(f"Price fetch error: {e}")
    return prices

# ── Trade / Watch Management ───────────────────────────────────
def place_trade(asset, sig, shares, commentary):
    price = sig["price"]
    cost = round(shares * price, 2)
    trade = {
        "id": f"{asset['ticker']}-{int(time.time())}",
        "ticker": asset["ticker"],
        "name": asset["name"],
        "sector": asset["sector"],
        "cap": asset["cap"],
        "timeframe": sig["best_tf"],
        "tf_score": sig["tf_scores"][sig["best_tf"]],
        "entry_price": price,
        "shares": shares,
        "total_cost": cost,
        "stop_pct": sig["stop_pct"],
        "target_pct": sig["upside_pct"],
        "signal_score": sig["score"],
        "signal_risk": sig["risk"],
        "rr_ratio": sig["rr_ratio"],
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": "open",
        "commentary": commentary,
        "pnl_pct": 0,
        "alerts_fired": [],
    }
    state["open_trades"].append(trade)
    state["balance"] = round(state["balance"] - cost, 2)
    add_note(asset, sig, "TRADE OPENED", commentary, price, shares)
    log.info(f"Trade opened: {asset['ticker']} x{shares} @ ${price:.2f}")

def add_watch(asset, sig, commentary):
    duration_ms = {
        "⚡ Intraday": 86400000,
        "📈 Short Swing": 432000000,
        "🌊 Medium Swing": 1209600000,
        "🏔️ Position": 7776000000,
        "🌳 Long Term": 15552000000,
    }.get(sig["best_tf"], 604800000)

    watch = {
        "id": f"watch-{asset['ticker']}-{int(time.time())}",
        "ticker": asset["ticker"],
        "name": asset["name"],
        "sector": asset["sector"],
        "cap": asset["cap"],
        "timeframe": sig["best_tf"],
        "tf_score": sig["tf_scores"][sig["best_tf"]],
        "added_at": time.time() * 1000,
        "expires_at": (time.time() + duration_ms / 1000) * 1000,
        "entry_price": sig["price"],
        "predicted_chg": sig["upside_pct"],
        "signal_score": sig["score"],
        "signal_risk": sig["risk"],
        "status": "watching",
        "commentary": commentary,
    }
    state["watch_items"].append(watch)
    add_note(asset, sig, "WATCH ADDED", commentary, sig["price"])
    log.info(f"Watch added: {asset['ticker']} for {sig['best_tf']}")

def add_note(asset, sig, action, commentary, price, shares=None):
    note = {
        "id": f"note-{int(time.time()*1000)}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ticker": asset["ticker"],
        "name": asset["name"],
        "sector": asset["sector"],
        "action": action,
        "signal_score": sig["score"],
        "risk_rating": sig["risk"],
        "best_tf": sig["best_tf"],
        "tf_score": sig["tf_scores"][sig["best_tf"]],
        "rr_ratio": sig["rr_ratio"],
        "price": price,
        "shares": shares,
        "reasoning": commentary.get("reasoning", ""),
        "confidence": commentary.get("confidence", "N/A"),
        "verdict": commentary.get("verdict", ""),
        "concerns": commentary.get("concerns", []),
    }
    state["notes"].insert(0, note)
    state["notes"] = state["notes"][:100]  # keep last 100

def add_alert(ticker, message, severity="WARNING"):
    alert = {
        "id": f"alert-{int(time.time()*1000)}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ticker": ticker,
        "message": message,
        "severity": severity,
        "seen": False,
    }
    state["alerts"].insert(0, alert)
    state["alerts"] = state["alerts"][:50]
    log.warning(f"ALERT [{severity}] {ticker}: {message}")

# ── Update Open Positions ──────────────────────────────────────
def update_positions(live_prices):
    """Check open positions against live prices, fire alerts, close on stop/target."""
    for trade in state["open_trades"][:]:
        ticker = trade["ticker"]
        live = live_prices.get(ticker)
        current = live["price"] if live and live.get("price") else trade["entry_price"]
        if live and live.get("currency") == "GBX":
            current = current / 100

        pnl_pct = ((current - trade["entry_price"]) / trade["entry_price"]) * 100
        trade["current_price"] = round(current, 4)
        trade["pnl_pct"] = round(pnl_pct, 2)
        trade["pnl"] = round((current - trade["entry_price"]) * trade["shares"], 2)

        # Stop loss hit
        if pnl_pct <= -trade["stop_pct"]:
            close_trade(trade, current, "STOP LOSS HIT")
            add_alert(ticker, f"Stop loss triggered at {pnl_pct:.1f}% loss. Position closed.", "DANGER")
            continue

        # Target hit
        if pnl_pct >= trade["target_pct"]:
            close_trade(trade, current, "TARGET REACHED")
            add_alert(ticker, f"Target reached at +{pnl_pct:.1f}%. Position closed. ✓", "SUCCESS")
            continue

        # Threshold alert
        if pnl_pct <= ALERT_THRESHOLD and f"thresh-{round(pnl_pct)}" not in trade["alerts_fired"]:
            add_alert(ticker, f"Position down {pnl_pct:.1f}%. Monitor closely.", "WARNING")
            trade["alerts_fired"].append(f"thresh-{round(pnl_pct)}")

def close_trade(trade, final_price, reason):
    trade["status"] = "closed"
    trade["final_price"] = round(final_price, 4)
    trade["final_pnl"] = round((final_price - trade["entry_price"]) * trade["shares"], 2)
    trade["final_pnl_pct"] = round(((final_price - trade["entry_price"]) / trade["entry_price"]) * 100, 2)
    trade["closed_at"] = datetime.now(timezone.utc).isoformat()
    trade["close_reason"] = reason
    proceeds = round(final_price * trade["shares"], 2)
    state["balance"] = round(state["balance"] + proceeds, 2)
    state["open_trades"] = [t for t in state["open_trades"] if t["id"] != trade["id"]]
    state["closed_trades"].insert(0, trade)
    update_accuracy()

def expire_watches(live_prices):
    """Expire watches, score prediction accuracy."""
    now_ms = time.time() * 1000
    for watch in state["watch_items"]:
        if watch["status"] == "watching" and now_ms >= watch["expires_at"]:
            ticker = watch["ticker"]
            live = live_prices.get(ticker)
            actual_price = live["price"] if live and live.get("price") else watch["entry_price"]
            actual_chg = ((actual_price - watch["entry_price"]) / watch["entry_price"]) * 100
            dir_correct = (watch["predicted_chg"] > 0 and actual_chg > 0) or \
                          (watch["predicted_chg"] < 0 and actual_chg < 0)
            watch["status"] = "expired"
            watch["actual_price"] = round(actual_price, 4)
            watch["actual_chg"] = round(actual_chg, 2)
            watch["dir_correct"] = dir_correct
            watch["accuracy"] = max(0, 100 - abs(actual_chg - watch["predicted_chg"]) * 3)
            update_accuracy()
            add_alert(ticker,
                f"Watch expired: predicted {watch['predicted_chg']:+.1f}%, actual {actual_chg:+.1f}%. "
                f"Direction {'✓ correct' if dir_correct else '✗ wrong'}.",
                "SUCCESS" if dir_correct else "WARNING")

def update_accuracy():
    closed = [t for t in state["closed_trades"]]
    expired = [w for w in state["watch_items"] if w["status"] == "expired"]
    all_results = closed + expired
    if not all_results:
        state["accuracy"] = {}
        return

    def score(item):
        return item.get("final_pnl_pct") or item.get("actual_chg") or 0

    wins = [x for x in all_results if score(x) > 0]
    by_sector = {}
    by_cap = {}
    by_tf = {}

    for item in all_results:
        sec = item.get("sector", "Unknown")
        cap = item.get("cap", "Unknown")
        tf = item.get("timeframe", "Unknown")
        w = score(item) > 0

        for d, k in [(by_sector, sec), (by_cap, cap), (by_tf, tf)]:
            if k not in d: d[k] = {"wins": 0, "total": 0}
            d[k]["total"] += 1
            if w: d[k]["wins"] += 1

    state["accuracy"] = {
        "overall": round(len(wins) / len(all_results) * 100, 1),
        "wins": len(wins),
        "losses": len(all_results) - len(wins),
        "total": len(all_results),
        "avg_return": round(sum(score(x) for x in all_results) / len(all_results), 2),
        "by_sector": {k: {"rate": round(v["wins"]/v["total"]*100,1), **v} for k,v in by_sector.items()},
        "by_cap": {k: {"rate": round(v["wins"]/v["total"]*100,1), **v} for k,v in by_cap.items()},
        "by_tf": {k: {"rate": round(v["wins"]/v["total"]*100,1), **v} for k,v in by_tf.items()},
    }

# ── Main Scan Loop ─────────────────────────────────────────────
async def run_scan():
    log.info(f"--- Scan #{state['scan_count'] + 1} starting ---")
    state["status"] = "scanning"

    # Fetch prices
    live_prices = await fetch_live_prices()
    state["live_prices"] = {k: {"price": v.get("price"), "change_pct": v.get("change_pct")}
                            for k, v in live_prices.items() if v.get("price")}

    # Update existing positions
    update_positions(live_prices)
    expire_watches(live_prices)

    # Generate signals with a rotating key (new key each scan for variety)
    scan_key = int(time.time() / SCAN_INTERVAL)
    candidates = []
    for asset in ASSETS:
        sig = generate_signals(asset, scan_key)
        # Merge live price if available
        live = live_prices.get(asset["ticker"])
        if live and live.get("price"):
            price = live["price"]
            if live.get("currency") == "GBX": price /= 100
            sig["price"] = round(price, 4)
            sig["live"] = True
        else:
            sig["live"] = False

        ok, reason = passes_rules(asset, sig)
        if ok:
            candidates.append((asset, sig))

    # Sort by composite score (signal score * best TF score)
    candidates.sort(key=lambda x: x[1]["score"] * x[1]["tf_scores"][x[1]["best_tf"]], reverse=True)

    # Process top candidates (cap at 3 per scan to avoid API hammering)
    processed = 0
    for asset, sig in candidates[:3]:
        if processed >= 3: break

        commentary = await get_ai_commentary(asset, sig, "BUY/WATCH")

        # If AI disagrees — only watch, don't trade
        if commentary["verdict"] == "DISAGREE":
            add_watch(asset, sig, commentary)
            add_alert(asset["ticker"],
                f"Signal passed rules but AI DISAGREES. Added to watch only. Confidence: {commentary['confidence']}",
                "WARNING")
        elif commentary["verdict"] == "AGREE WITH CAUTION":
            # Watch + half-size trade
            add_watch(asset, sig, commentary)
            risk_budget = state["balance"] * BOT_RISK_PCT * 0.5  # half size
            shares = max(1, math.floor(risk_budget / (sig["price"] * sig["stop_pct"] / 100)))
            place_trade(asset, sig, shares, commentary)
        else:
            # Full trade + watch
            risk_budget = state["balance"] * BOT_RISK_PCT
            shares = max(1, math.floor(risk_budget / (sig["price"] * sig["stop_pct"] / 100)))
            place_trade(asset, sig, shares, commentary)
            add_watch(asset, sig, commentary)

        processed += 1
        await asyncio.sleep(1)  # avoid rate limiting

    state["scan_count"] += 1
    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    state["status"] = "idle"
    save_state()
    log.info(f"Scan complete. Open: {len(state['open_trades'])}, Balance: ${state['balance']:.2f}")

# ── FastAPI Dashboard API ──────────────────────────────────────
app = FastAPI(title="Market Brain Bot API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/bot/status")
def bot_status():
    open_trades = state["open_trades"]
    total_invested = sum(t["total_cost"] for t in open_trades)
    unrealised = sum(t.get("pnl", 0) for t in open_trades)
    pnl_total = state["balance"] - state["start_balance"] + unrealised
    return {
        "status": state["status"],
        "started_at": state["started_at"],
        "last_scan": state["last_scan"],
        "scan_count": state["scan_count"],
        "balance": state["balance"],
        "start_balance": state["start_balance"],
        "total_invested": round(total_invested, 2),
        "unrealised_pnl": round(unrealised, 2),
        "total_pnl": round(pnl_total, 2),
        "total_pnl_pct": round(pnl_total / state["start_balance"] * 100, 2),
        "open_trades": len(open_trades),
        "closed_trades": len(state["closed_trades"]),
        "watch_items": len([w for w in state["watch_items"] if w["status"] == "watching"]),
        "alerts_unseen": len([a for a in state["alerts"] if not a["seen"]]),
        "accuracy": state.get("accuracy", {}),
        "live_prices": state["live_prices"],
    }

@app.get("/bot/trades")
def bot_trades():
    return {"open": state["open_trades"], "closed": state["closed_trades"][:50]}

@app.get("/bot/watches")
def bot_watches():
    return {"items": state["watch_items"][-50:]}

@app.get("/bot/notes")
def bot_notes():
    return {"notes": state["notes"]}

@app.get("/bot/alerts")
def bot_alerts():
    return {"alerts": state["alerts"]}

@app.post("/bot/alerts/clear")
def clear_alerts():
    for a in state["alerts"]: a["seen"] = True
    return {"ok": True}

@app.post("/bot/scan")
async def trigger_scan():
    """Manually trigger a scan."""
    asyncio.create_task(run_scan())
    return {"ok": True, "message": "Scan triggered"}

@app.get("/bot/export")
def export_report():
    """Export full state as JSON for analysis."""
    return JSONResponse(content=state)

# ── Startup ────────────────────────────────────────────────────
scheduler = AsyncIOScheduler()

@app.on_event("startup")
async def startup():
    load_state()
    ok = await mb_login()
    if not ok:
        log.warning("Could not authenticate with Market Brain API — running in offline mode")
    state["status"] = "idle"

    # Schedule recurring scans
    scheduler.add_job(run_scan, "interval", seconds=SCAN_INTERVAL, id="scan")
    scheduler.start()

    # Run first scan immediately
    asyncio.create_task(run_scan())
    log.info(f"Bot started. Scanning every {SCAN_INTERVAL//60} minutes. Dashboard API on :{BOT_PORT}")

@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()
    save_state()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot_engine:app", host="0.0.0.0", port=BOT_PORT, reload=False)
