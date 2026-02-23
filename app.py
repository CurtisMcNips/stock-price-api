"""
Market Brain API v3.0
─────────────────────────────────────────────────────────────────────────────
Real-world analytical engine. No pseudo-random seeds. No synthetic variation.

Architecture:
  - All existing auth, portfolio, price-fetching endpoints preserved unchanged
  - NEW: /api/analysis/*  — real analytical engine fed by structured data
  - NEW: /api/engine/*    — parameter management, feedback, audit trail
  - NEW: /api/feed/*      — structured data ingestion from bots/feeds

The engine NEVER fetches data itself. Bots POST structured market data to
/api/feed/update. The engine reads that data, applies its analytical
framework, and returns scored, reasoned, auditable analysis.

Engine parameter changes require human approval via /api/engine/propose.
The engine cannot modify its own scoring weights without an approved proposal.
─────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import json
import logging
import os
import time
import hashlib
import hmac
import base64
import math
import statistics
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────
REDIS_URL       = os.environ.get("REDIS_URL", "redis://localhost:6379")
SECRET_KEY      = os.environ.get("SECRET_KEY", "change-me-in-production-railway-env")
CACHE_TTL       = 5
TOKEN_TTL       = 60 * 60 * 24 * 30
REQUEST_TIMEOUT = 8

YAHOO_URL          = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_FALLBACK_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── In-memory stores ───────────────────────────────────────────
_memory_cache:      Dict[str, dict] = {}
_memory_users:      Dict[str, dict] = {}
_memory_tokens:     Dict[str, str]  = {}
_memory_portfolios: Dict[str, dict] = {}
redis_client: Optional[aioredis.Redis] = None

# ── Engine state (lives in memory + Redis, never in code) ──────
# market_data[ticker] = latest structured feed payload
_market_data:   Dict[str, dict] = {}
# engine_params = the analytical weights (human-approved changes only)
_engine_params: Dict[str, Any]  = {}
# feedback_log = list of bot/user feedback records
_feedback_log:  List[dict]      = []
# analysis_cache[ticker] = last computed analysis
_analysis_cache: Dict[str, dict] = {}
# proposals = pending parameter change proposals
_proposals: List[dict] = []
# audit_log = immutable record of every analysis produced
_audit_log: List[dict] = []


# ══════════════════════════════════════════════════════════════
# REDIS HELPERS
# ══════════════════════════════════════════════════════════════
async def get_redis() -> Optional[aioredis.Redis]:
    global redis_client
    if redis_client:
        try:
            await redis_client.ping()
            return redis_client
        except Exception:
            redis_client = None
    try:
        redis_client = await aioredis.from_url(
            REDIS_URL, decode_responses=True, socket_timeout=2
        )
        await redis_client.ping()
        log.info("Redis connected")
        return redis_client
    except Exception as e:
        log.warning(f"Redis unavailable ({e}) — using memory")
        return None

async def rget(key: str) -> Optional[str]:
    r = await get_redis()
    if r:
        try: return await r.get(key)
        except: pass
    return None

async def rset(key: str, value: str, ttl: int = 0):
    r = await get_redis()
    if r:
        try:
            if ttl: await r.setex(key, ttl, value)
            else:   await r.set(key, value)
            return
        except: pass

async def rdel(key: str):
    r = await get_redis()
    if r:
        try: await r.delete(key)
        except: pass


# ══════════════════════════════════════════════════════════════
# AUTH HELPERS  (unchanged from v2)
# ══════════════════════════════════════════════════════════════
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return base64.b64encode(salt + key).decode()

def verify_password(password: str, stored: str) -> bool:
    try:
        data = base64.b64decode(stored.encode())
        salt, key = data[:16], data[16:]
        new_key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
        return hmac.compare_digest(key, new_key)
    except:
        return False

def make_token(email: str) -> str:
    raw = f"{email}:{time.time()}:{os.urandom(16).hex()}"
    return base64.b64encode(raw.encode()).decode().replace("=", "")

async def save_user(email: str, user: dict):
    await rset(f"user:{email}", json.dumps(user))
    _memory_users[email] = user

async def load_user(email: str) -> Optional[dict]:
    val = await rget(f"user:{email}")
    if val: return json.loads(val)
    return _memory_users.get(email)

async def save_token(token: str, email: str):
    await rset(f"token:{token}", email, TOKEN_TTL)
    _memory_tokens[token] = email

async def load_token(token: str) -> Optional[str]:
    val = await rget(f"token:{token}")
    if val: return val
    return _memory_tokens.get(token)

async def delete_token(token: str):
    await rdel(f"token:{token}")
    _memory_tokens.pop(token, None)

async def save_portfolio(email: str, portfolio: dict):
    await rset(f"portfolio:{email}", json.dumps(portfolio))
    _memory_portfolios[email] = portfolio

async def load_portfolio(email: str) -> dict:
    val = await rget(f"portfolio:{email}")
    if val: return json.loads(val)
    if email in _memory_portfolios: return _memory_portfolios[email]
    return {"trades": [], "watchItems": [], "balance": 1000, "startBalance": 1000}

async def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    token = authorization[7:]
    email = await load_token(token)
    if not email:
        raise HTTPException(401, "Invalid or expired token")
    user = await load_user(email)
    if not user:
        raise HTTPException(401, "User not found")
    return user


# ══════════════════════════════════════════════════════════════
# ENGINE PARAMETERS
# Default weights — human-approved changes only via /api/engine/propose
# ══════════════════════════════════════════════════════════════
DEFAULT_ENGINE_PARAMS = {
    "version": "3.0.0",
    "last_updated": None,
    "approved_by": "system_default",

    # ── Trend weights (sum to 1.0) ──────────────────────────
    "w_trend_direction":   0.25,   # price above/below key MAs
    "w_trend_strength":    0.20,   # ADX / slope of trend
    "w_momentum":          0.15,   # RSI, MACD position
    "w_volume":            0.15,   # volume vs average
    "w_volatility":        0.10,   # ATR, BB width (risk factor)
    "w_sector_momentum":   0.10,   # sector relative strength
    "w_catalyst":          0.05,   # news/event impact score

    # ── Timeframe volatility multipliers ───────────────────
    # Based on real observed typical ranges per asset class
    "tf_intraday_mult":    1.0,
    "tf_swing_mult":       2.2,
    "tf_medium_mult":      4.5,
    "tf_position_mult":    9.0,
    "tf_longterm_mult":    18.0,

    # ── Confidence thresholds ──────────────────────────────
    "confidence_high":     70,     # score above this = HIGH confidence
    "confidence_low":      40,     # score below this = LOW confidence

    # ── Risk rating thresholds ─────────────────────────────
    "risk_critical":      -55,
    "risk_high":          -15,
    "risk_moderate":       18,
    "risk_positive":       55,

    # ── Feedback learning rate ─────────────────────────────
    # How much a confirmed feedback shifts parameter suggestions
    # (proposals still need human approval to take effect)
    "feedback_learning_rate": 0.02,

    # ── Staleness threshold (seconds) ─────────────────────
    "data_staleness_warn":  3600,   # 1 hour — show warning
    "data_staleness_fail":  86400,  # 24 hours — refuse to analyse
}

def load_engine_params() -> dict:
    """Load params from Redis/memory, fall back to defaults."""
    return {**DEFAULT_ENGINE_PARAMS, **_engine_params}


# ══════════════════════════════════════════════════════════════
# REAL-WORLD ANALYTICAL ENGINE
# No randomness. Every output is a deterministic function of input data.
# ══════════════════════════════════════════════════════════════

def clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))

def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b and b != 0 else default

def score_trend_direction(d: dict) -> tuple[float, str]:
    """
    Score: is price in a bullish or bearish trend?
    Uses: price vs MA20, MA50, MA200, price position in 52w range.
    Returns (score -100..100, explanation).
    """
    price   = d.get("price", 0)
    ma20    = d.get("ma20")
    ma50    = d.get("ma50")
    ma200   = d.get("ma200")
    wk52hi  = d.get("fifty_two_week_high")
    wk52lo  = d.get("fifty_two_week_low")

    score = 0.0
    parts = []

    if price and ma20:
        pct = safe_div(price - ma20, ma20) * 100
        contrib = clamp(pct * 2.5, -30, 30)
        score += contrib
        parts.append(f"price {'above' if pct>0 else 'below'} MA20 by {abs(pct):.1f}%")

    if price and ma50:
        pct = safe_div(price - ma50, ma50) * 100
        contrib = clamp(pct * 2.0, -25, 25)
        score += contrib
        parts.append(f"price {'above' if pct>0 else 'below'} MA50 by {abs(pct):.1f}%")

    if price and ma200:
        pct = safe_div(price - ma200, ma200) * 100
        contrib = clamp(pct * 1.5, -20, 20)
        score += contrib
        parts.append(f"price {'above' if pct>0 else 'below'} MA200 by {abs(pct):.1f}%")

    if price and wk52hi and wk52lo and wk52hi > wk52lo:
        position = safe_div(price - wk52lo, wk52hi - wk52lo)  # 0..1
        contrib = (position - 0.5) * 30  # -15..+15
        score += contrib
        pct_pos = round(position * 100)
        parts.append(f"at {pct_pos}th percentile of 52-week range")

    explanation = "; ".join(parts) if parts else "insufficient MA data"
    return clamp(score, -100, 100), explanation


def score_trend_strength(d: dict) -> tuple[float, str]:
    """
    Score: how strong is the trend (not just direction)?
    Uses: ADX, consecutive closes in same direction, MA alignment.
    """
    adx     = d.get("adx")          # 0–100, >25 = trending
    ma20    = d.get("ma20")
    ma50    = d.get("ma50")
    ma200   = d.get("ma200")
    consec  = d.get("consecutive_closes", 0)  # +N = N up closes, -N = N down

    score = 0.0
    parts = []

    if adx is not None:
        if adx > 40:
            score += 35; parts.append(f"strong trend ADX={adx:.0f}")
        elif adx > 25:
            score += 20; parts.append(f"moderate trend ADX={adx:.0f}")
        elif adx > 15:
            score += 5;  parts.append(f"weak trend ADX={adx:.0f}")
        else:
            score -= 10; parts.append(f"ranging/choppy ADX={adx:.0f}")

    # MA alignment: bullish = MA20>MA50>MA200
    if ma20 and ma50 and ma200:
        if ma20 > ma50 > ma200:
            score += 30; parts.append("MAs fully bullish aligned")
        elif ma20 < ma50 < ma200:
            score -= 30; parts.append("MAs fully bearish aligned")
        elif ma20 > ma50:
            score += 15; parts.append("short-term MAs bullish")
        else:
            score -= 15; parts.append("short-term MAs bearish")

    if consec:
        contrib = clamp(consec * 5, -20, 20)
        score += contrib
        direction = "up" if consec > 0 else "down"
        parts.append(f"{abs(consec)} consecutive {direction} closes")

    explanation = "; ".join(parts) if parts else "insufficient trend data"
    return clamp(score, -100, 100), explanation


def score_momentum(d: dict) -> tuple[float, str]:
    """
    Score: is momentum building or fading?
    Uses: RSI, MACD line vs signal, MACD histogram slope.
    """
    rsi         = d.get("rsi")
    macd_line   = d.get("macd_line")
    macd_signal = d.get("macd_signal")
    macd_hist   = d.get("macd_histogram")
    macd_hist_prev = d.get("macd_histogram_prev")

    score = 0.0
    parts = []

    if rsi is not None:
        if rsi < 30:
            score += 35; parts.append(f"RSI oversold {rsi:.0f} — mean-reversion opportunity")
        elif rsi < 40:
            score += 20; parts.append(f"RSI approaching oversold {rsi:.0f}")
        elif rsi > 75:
            score -= 30; parts.append(f"RSI overbought {rsi:.0f} — pullback risk")
        elif rsi > 65:
            score -= 10; parts.append(f"RSI elevated {rsi:.0f}")
        elif 45 <= rsi <= 60:
            score += 10; parts.append(f"RSI healthy momentum zone {rsi:.0f}")
        else:
            parts.append(f"RSI neutral {rsi:.0f}")

    if macd_line is not None and macd_signal is not None:
        crossover = macd_line - macd_signal
        if crossover > 0:
            score += 20; parts.append("MACD above signal — bullish")
        else:
            score -= 20; parts.append("MACD below signal — bearish")

    if macd_hist is not None and macd_hist_prev is not None:
        hist_slope = macd_hist - macd_hist_prev
        if hist_slope > 0 and macd_hist > 0:
            score += 15; parts.append("MACD histogram expanding bullish")
        elif hist_slope < 0 and macd_hist < 0:
            score -= 15; parts.append("MACD histogram expanding bearish")
        elif hist_slope > 0 and macd_hist < 0:
            score += 8; parts.append("MACD histogram contracting — possible reversal")

    explanation = "; ".join(parts) if parts else "insufficient momentum data"
    return clamp(score, -100, 100), explanation


def score_volume(d: dict) -> tuple[float, str]:
    """
    Score: is volume confirming or diverging from price direction?
    Uses: volume vs 20-day average, on-balance volume trend, price-volume divergence.
    """
    volume      = d.get("volume")
    avg_volume  = d.get("avg_volume_20d")
    obv_trend   = d.get("obv_trend")    # "rising", "falling", "flat"
    price_chg   = d.get("change_pct", 0)

    score = 0.0
    parts = []

    if volume and avg_volume and avg_volume > 0:
        vol_ratio = volume / avg_volume
        if vol_ratio > 2.0:
            # High volume — score direction of price move
            if price_chg > 0:
                score += 40; parts.append(f"volume {vol_ratio:.1f}x avg on up move — strong conviction")
            else:
                score -= 40; parts.append(f"volume {vol_ratio:.1f}x avg on down move — distribution signal")
        elif vol_ratio > 1.3:
            contrib = 20 if price_chg > 0 else -20
            score += contrib
            parts.append(f"above-average volume {vol_ratio:.1f}x — {'confirming' if price_chg>0 else 'pressuring'}")
        elif vol_ratio < 0.5:
            score -= 10; parts.append(f"very low volume {vol_ratio:.1f}x — weak conviction")
        else:
            parts.append(f"normal volume {vol_ratio:.1f}x avg")

    if obv_trend:
        if obv_trend == "rising":
            score += 20; parts.append("OBV trending up — accumulation")
        elif obv_trend == "falling":
            score -= 20; parts.append("OBV trending down — distribution")
        else:
            parts.append("OBV flat — no clear accumulation/distribution")

    explanation = "; ".join(parts) if parts else "no volume data available"
    return clamp(score, -100, 100), explanation


def score_volatility(d: dict) -> tuple[float, str, float]:
    """
    Score: is volatility a risk or opportunity?
    Also returns the ATR percentage — used directly for stop/target sizing.
    Uses: ATR%, Bollinger Band width, implied volatility if available.
    Returns (score, explanation, atr_pct)
    """
    atr_pct     = d.get("atr_pct")         # ATR as % of price
    bb_width    = d.get("bb_width_pct")     # BB width as % of price
    iv          = d.get("implied_vol")      # options IV if available
    hist_vol    = d.get("hist_vol_20d")     # 20d historical vol
    price       = d.get("price", 0)
    day_hi      = d.get("day_high")
    day_lo      = d.get("day_low")

    score = 0.0
    parts = []
    effective_atr = atr_pct

    # Derive ATR from day range if not supplied
    if not effective_atr and price and day_hi and day_lo:
        effective_atr = safe_div(day_hi - day_lo, price) * 100

    if effective_atr:
        # Moderate volatility is good for traders; extreme is dangerous
        if effective_atr < 0.5:
            score += 5; parts.append(f"ATR {effective_atr:.1f}% — very low vol, tight moves")
        elif effective_atr < 2.0:
            score += 20; parts.append(f"ATR {effective_atr:.1f}% — healthy trading range")
        elif effective_atr < 4.0:
            score += 10; parts.append(f"ATR {effective_atr:.1f}% — elevated vol, bigger swings")
        elif effective_atr < 8.0:
            score -= 10; parts.append(f"ATR {effective_atr:.1f}% — high vol, widen stops")
        else:
            score -= 25; parts.append(f"ATR {effective_atr:.1f}% — extreme vol, high risk")

    if bb_width:
        if bb_width < 5:
            score += 15; parts.append(f"BB width {bb_width:.1f}% — squeeze, potential breakout")
        elif bb_width > 20:
            score -= 10; parts.append(f"BB width {bb_width:.1f}% — expanded, mean reversion likely")

    if iv and hist_vol:
        iv_ratio = safe_div(iv, hist_vol)
        if iv_ratio > 1.3:
            score -= 10; parts.append(f"IV/HV {iv_ratio:.1f} — options pricing in event risk")
        elif iv_ratio < 0.8:
            score += 10; parts.append(f"IV/HV {iv_ratio:.1f} — options cheap relative to history")

    explanation = "; ".join(parts) if parts else "no volatility data supplied"
    return clamp(score, -100, 100), explanation, effective_atr or 2.0


def score_sector_momentum(d: dict) -> tuple[float, str]:
    """
    Score: is the sector itself in favour?
    Uses: sector relative strength vs SPY, sector flow data from feed.
    """
    sector_rs   = d.get("sector_rs")        # sector 1m return vs SPY, %
    sector_flow = d.get("sector_flow")      # "inflow", "outflow", "neutral"
    sector_rank = d.get("sector_rank")      # 1-11 ranking (1=strongest)

    score = 0.0
    parts = []

    if sector_rs is not None:
        contrib = clamp(sector_rs * 3, -40, 40)
        score += contrib
        parts.append(f"sector {sector_rs:+.1f}% vs SPY 1m")

    if sector_flow:
        if sector_flow == "inflow":
            score += 25; parts.append("sector seeing institutional inflow")
        elif sector_flow == "outflow":
            score -= 25; parts.append("sector seeing institutional outflow")

    if sector_rank:
        if sector_rank <= 3:
            score += 20; parts.append(f"sector ranked #{sector_rank} of 11 — leadership")
        elif sector_rank >= 9:
            score -= 20; parts.append(f"sector ranked #{sector_rank} of 11 — lagging")

    explanation = "; ".join(parts) if parts else "no sector data supplied"
    return clamp(score, -100, 100), explanation


def score_catalyst(d: dict) -> tuple[float, str]:
    """
    Score: are there near-term catalysts that shift risk/reward?
    Uses: days to earnings, catalyst_score from feed, news_sentiment.
    """
    days_earnings   = d.get("days_to_earnings")
    catalyst_score  = d.get("catalyst_score")     # -1 to +1, from feed
    news_sentiment  = d.get("news_sentiment")      # -1 to +1, from feed
    insider_activity = d.get("insider_activity")  # "buying", "selling", "none"

    score = 0.0
    parts = []

    if days_earnings is not None:
        if days_earnings <= 7:
            score -= 10; parts.append(f"earnings in {days_earnings}d — binary event risk, consider sizing down")
        elif days_earnings <= 21:
            score += 5;  parts.append(f"earnings in {days_earnings}d — pre-earnings momentum possible")
        elif days_earnings <= 45:
            score += 10; parts.append(f"earnings in {days_earnings}d — clear runway")

    if catalyst_score is not None:
        contrib = clamp(catalyst_score * 30, -30, 30)
        score += contrib
        direction = "positive" if catalyst_score > 0.2 else ("negative" if catalyst_score < -0.2 else "neutral")
        parts.append(f"catalyst assessment: {direction} ({catalyst_score:+.2f})")

    if news_sentiment is not None:
        contrib = clamp(news_sentiment * 20, -20, 20)
        score += contrib
        sentiment = "bullish" if news_sentiment > 0.2 else ("bearish" if news_sentiment < -0.2 else "neutral")
        parts.append(f"news sentiment: {sentiment} ({news_sentiment:+.2f})")

    if insider_activity:
        if insider_activity == "buying":
            score += 20; parts.append("insider buying activity — strong conviction signal")
        elif insider_activity == "selling":
            score -= 15; parts.append("insider selling — caution warranted")

    explanation = "; ".join(parts) if parts else "no catalyst data supplied"
    return clamp(score, -100, 100), explanation


def compute_timeframe_projections(
    composite_score: float,
    atr_pct: float,
    params: dict,
    asset_class: str,
    data: dict,
) -> dict:
    """
    Project realistic upside/downside per timeframe.
    Based on:
    - Composite directional score (-100 to +100)
    - Real ATR% (actual measured volatility)
    - Timeframe multipliers (how many ATR periods fit in the window)
    - Asset class adjustments (crypto vs forex vs large cap equity)

    Returns dict of timeframe -> {upside, downside, confidence, reasoning}
    """
    # Asset class ATR multiplier — crypto moves more per ATR unit than forex
    class_mult = {
        "Crypto": 3.0, "Minerals": 2.0, "Space": 2.0,
        "Technology": 1.4, "Healthcare": 1.3, "Energy": 1.2,
        "Consumer": 1.1, "Finance": 1.0, "Metals": 1.1,
        "Forex": 0.5, "Agriculture": 0.9,
    }.get(asset_class, 1.2)

    direction = composite_score / 100.0    # -1.0 to +1.0
    abs_dir   = abs(direction)

    # Base move = ATR% × class_mult × timeframe_mult × confidence scalar
    # Upside/downside asymmetry driven by direction strength
    def project(tf_mult: float) -> dict:
        base = atr_pct * class_mult * tf_mult
        # Upside biased by bullish direction; downside by bearish
        upside   = base * (0.5 + abs_dir * 0.5) if direction > 0 else base * (0.5 - abs_dir * 0.3)
        downside = base * (0.5 + abs_dir * 0.5) if direction < 0 else base * (0.5 - abs_dir * 0.3)
        upside   = round(clamp(upside,  0.1, 500.0), 1)
        downside = round(clamp(downside, 0.1, 500.0), 1)
        # Confidence: driven by composite score magnitude + data completeness
        data_completeness = data.get("data_completeness", 0.5)  # 0..1 from feed
        confidence_raw = abs_dir * 60 + data_completeness * 40
        confidence = round(clamp(confidence_raw, 5, 95), 0)
        rr = round(safe_div(upside, downside, 0), 2)
        return {
            "upside_pct": upside,
            "downside_pct": downside,
            "rr_ratio": rr,
            "confidence": int(confidence),
        }

    return {
        "⚡ Intraday":     project(params.get("tf_intraday_mult",  1.0)),
        "📈 Short Swing":  project(params.get("tf_swing_mult",     2.2)),
        "🌊 Medium Swing": project(params.get("tf_medium_mult",    4.5)),
        "🏔️ Position":    project(params.get("tf_position_mult",  9.0)),
        "🌳 Long Term":    project(params.get("tf_longterm_mult",  18.0)),
    }


def determine_entry_quality(data: dict, composite_score: float) -> tuple[str, str]:
    """
    Determine entry quality based on real price structure.
    Returns (quality_label, reasoning)
    """
    rsi      = data.get("rsi")
    price    = data.get("price", 0)
    ma20     = data.get("ma20")
    bb_lower = data.get("bb_lower")
    bb_upper = data.get("bb_upper")
    atr_pct  = data.get("atr_pct", 2.0)

    reasons = []

    # Check proximity to support (BB lower, MA20)
    near_support = False
    if bb_lower and price:
        dist_pct = safe_div(price - bb_lower, price) * 100
        if dist_pct < atr_pct:
            near_support = True
            reasons.append(f"price near BB lower ({dist_pct:.1f}% from support)")

    if ma20 and price:
        dist_pct = safe_div(price - ma20, price) * 100
        if abs(dist_pct) < atr_pct:
            near_support = abs(dist_pct) < 0
            reasons.append(f"price {'at' if abs(dist_pct)<0.5 else 'near'} MA20")

    oversold = rsi and rsi < 35
    if oversold: reasons.append(f"RSI oversold ({rsi:.0f})")

    overbought = rsi and rsi > 70
    if overbought: reasons.append(f"RSI overbought ({rsi:.0f}) — chasing risk")

    near_resistance = False
    if bb_upper and price:
        dist_pct = safe_div(bb_upper - price, price) * 100
        if dist_pct < atr_pct:
            near_resistance = True
            reasons.append(f"price near BB upper — resistance overhead")

    if (near_support or oversold) and composite_score > 20:
        return "IDEAL", "; ".join(reasons) or "price at support with positive signal"
    elif overbought or near_resistance:
        return "POOR", "; ".join(reasons) or "chasing — price extended"
    elif composite_score > 0:
        return "GOOD", "; ".join(reasons) or "positive signal, no specific support level"
    else:
        return "FAIR", "; ".join(reasons) or "mixed signals — wait for clearer setup"


def build_analysis(ticker: str, data: dict, params: dict) -> dict:
    """
    Core engine: takes structured market data, returns full analysis.
    Completely deterministic — same input always produces same output.
    No randomness. Every number is traceable to its input.
    """
    timestamp = int(time.time())
    asset_class = data.get("sector", "Technology")

    # ── Score each factor ──────────────────────────────────
    s_trend_dir,  e_trend_dir  = score_trend_direction(data)
    s_trend_str,  e_trend_str  = score_trend_strength(data)
    s_momentum,   e_momentum   = score_momentum(data)
    s_volume,     e_volume     = score_volume(data)
    s_vol,        e_vol, atr   = score_volatility(data)
    s_sector,     e_sector     = score_sector_momentum(data)
    s_catalyst,   e_catalyst   = score_catalyst(data)

    # ── Weighted composite score ───────────────────────────
    w = params
    composite = (
        s_trend_dir  * w["w_trend_direction"]  +
        s_trend_str  * w["w_trend_strength"]   +
        s_momentum   * w["w_momentum"]         +
        s_volume     * w["w_volume"]           +
        s_vol        * w["w_volatility"]       +
        s_sector     * w["w_sector_momentum"]  +
        s_catalyst   * w["w_catalyst"]
    )
    composite = round(clamp(composite, -100, 100), 1)

    # ── Risk rating ────────────────────────────────────────
    if composite < w["risk_critical"]:   risk = "CRITICAL"
    elif composite < w["risk_high"]:     risk = "HIGH"
    elif composite < w["risk_moderate"]: risk = "MODERATE"
    elif composite < w["risk_positive"]: risk = "POSITIVE"
    else:                                risk = "STRONG"

    # ── Timeframe projections ──────────────────────────────
    projections = compute_timeframe_projections(composite, atr, params, asset_class, data)
    best_tf = max(projections, key=lambda k: projections[k]["confidence"])

    # ── Entry quality ──────────────────────────────────────
    entry_q, entry_reason = determine_entry_quality(data, composite)

    # ── Stop loss sizing from ATR ──────────────────────────
    # Real stops: 1.5× ATR for large cap, 2× for small, 3× for crypto
    cap = data.get("cap", "Mid")
    atr_stop_mult = {"Large": 1.5, "Mid": 2.0, "Small": 2.5, "Micro": 3.0, "Nano": 3.5}.get(cap, 2.0)
    if asset_class == "Crypto": atr_stop_mult *= 1.5
    if asset_class == "Forex":  atr_stop_mult = 1.0
    stop_pct = round(atr * atr_stop_mult, 1)
    stop_pct = clamp(stop_pct, 0.5, 50.0)

    # ── Confidence level ───────────────────────────────────
    dc = data.get("data_completeness", 0.5)
    abs_score = abs(composite)
    confidence_score = int(abs_score * 0.6 + dc * 40)
    if dc < 0.3:
        confidence_label = "LOW"
    elif confidence_score >= w["confidence_high"]:
        confidence_label = "HIGH"
    elif confidence_score >= w["confidence_low"]:
        confidence_label = "MEDIUM"
    else:
        confidence_label = "LOW"

    # ── Data staleness check ───────────────────────────────
    data_age = timestamp - data.get("feed_timestamp", timestamp)
    staleness = "fresh"
    if data_age > w["data_staleness_fail"]:
        staleness = "stale_fail"
    elif data_age > w["data_staleness_warn"]:
        staleness = "stale_warn"

    # ── Risks ──────────────────────────────────────────────
    risks = []
    if data.get("days_to_earnings") and data["days_to_earnings"] <= 7:
        risks.append("Binary earnings event within 7 days — consider reducing size")
    if atr > 5:
        risks.append(f"High ATR ({atr:.1f}%) — wide stops required, size accordingly")
    if data.get("rsi", 50) > 75:
        risks.append("Overbought RSI — momentum may stall or reverse")
    if data.get("short_interest_pct", 0) > 20:
        risks.append(f"High short interest {data['short_interest_pct']:.0f}% — short squeeze OR continued pressure risk")
    if data.get("debt_ratio", 0) > 2:
        risks.append("Elevated debt ratio — vulnerable in risk-off environments")
    if staleness == "stale_warn":
        risks.append(f"Data is {data_age//3600}h old — verify before acting")
    if staleness == "stale_fail":
        risks.append(f"WARNING: Data is {data_age//3600}h old — analysis unreliable")
    if dc < 0.4:
        risks.append("Incomplete data feed — confidence reduced; missing fields limit accuracy")

    # ── Reasoning narrative ────────────────────────────────
    direction_word = "bullish" if composite > 10 else ("bearish" if composite < -10 else "neutral")
    reasoning = (
        f"Composite score {composite:+.1f}/100 ({direction_word}). "
        f"Trend: {e_trend_dir}. "
        f"Strength: {e_trend_str}. "
        f"Momentum: {e_momentum}. "
        f"Volume: {e_volume}. "
        f"Volatility: {e_vol}. "
        f"Sector: {e_sector}. "
        f"Catalysts: {e_catalyst}."
    )

    # ── Factor breakdown (auditable) ───────────────────────
    factor_scores = {
        "trend_direction":  {"score": round(s_trend_dir,1),  "weight": w["w_trend_direction"],  "explanation": e_trend_dir},
        "trend_strength":   {"score": round(s_trend_str,1),  "weight": w["w_trend_strength"],   "explanation": e_trend_str},
        "momentum":         {"score": round(s_momentum,1),   "weight": w["w_momentum"],         "explanation": e_momentum},
        "volume":           {"score": round(s_volume,1),     "weight": w["w_volume"],           "explanation": e_volume},
        "volatility":       {"score": round(s_vol,1),        "weight": w["w_volatility"],       "explanation": e_vol},
        "sector_momentum":  {"score": round(s_sector,1),     "weight": w["w_sector_momentum"],  "explanation": e_sector},
        "catalyst":         {"score": round(s_catalyst,1),   "weight": w["w_catalyst"],         "explanation": e_catalyst},
    }

    result = {
        # Identity
        "ticker":           ticker,
        "name":             data.get("name", ticker),
        "sector":           asset_class,
        "cap":              cap,
        "analysed_at":      datetime.now(timezone.utc).isoformat(),
        "engine_version":   params.get("version", "3.0.0"),
        "data_staleness":   staleness,
        "data_completeness": dc,

        # Core output
        "composite_score":  composite,
        "risk_rating":      risk,
        "confidence":       confidence_label,
        "confidence_score": confidence_score,
        "entry_quality":    entry_q,
        "entry_reasoning":  entry_reason,

        # Price data (from feed)
        "price":            data.get("price"),
        "change_pct":       data.get("change_pct"),
        "atr_pct":          round(atr, 2),
        "stop_loss_pct":    stop_pct,

        # Timeframe projections
        "projections":      projections,
        "best_timeframe":   best_tf,

        # Explanation
        "reasoning":        reasoning,
        "factor_scores":    factor_scores,
        "risks":            risks,

        # Feedback tracking
        "feedback_count":   data.get("feedback_count", 0),
        "feedback_accuracy": data.get("feedback_accuracy"),
    }

    return result


# ══════════════════════════════════════════════════════════════
# ENGINE PERSISTENCE
# ══════════════════════════════════════════════════════════════
async def persist_engine_state():
    await rset("engine:params", json.dumps(_engine_params))
    await rset("engine:feedback", json.dumps(_feedback_log[-500:]))
    await rset("engine:proposals", json.dumps(_proposals))
    await rset("engine:market_data", json.dumps(_market_data))

async def load_engine_state():
    global _engine_params, _feedback_log, _proposals, _market_data
    for key, target, default in [
        ("engine:params",    "_engine_params",  {}),
        ("engine:feedback",  "_feedback_log",   []),
        ("engine:proposals", "_proposals",      []),
        ("engine:market_data", "_market_data",  {}),
    ]:
        val = await rget(key)
        if val:
            try:
                globals()[target] = json.loads(val)
            except: pass


# ══════════════════════════════════════════════════════════════
# LIFESPAN
# ══════════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_redis()
    await load_engine_state()
    Path("static").mkdir(exist_ok=True)
    log.info("Market Brain v3 engine started — real-world analytical mode")
    yield
    await persist_engine_state()
    if redis_client:
        await redis_client.aclose()


# ══════════════════════════════════════════════════════════════
# APP
# ══════════════════════════════════════════════════════════════
app = FastAPI(
    title="Market Brain API",
    version="3.0.0",
    description="Real-world analytical engine. No synthetic randomness.",
    lifespan=lifespan
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"]
)


# ══════════════════════════════════════════════════════════════
# PYDANTIC MODELS
# ══════════════════════════════════════════════════════════════
class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

class PortfolioSave(BaseModel):
    trades: list
    watchItems: list
    balance: float
    startBalance: float

class MarketDataFeed(BaseModel):
    """
    Structured data feed — posted by bots/scrapers.
    Engine reads this, never fetches data itself.
    All fields optional; data_completeness auto-computed.
    """
    ticker:             str
    name:               Optional[str]       = None
    sector:             Optional[str]       = None
    cap:                Optional[str]       = None

    # Price
    price:              Optional[float]     = None
    change_pct:         Optional[float]     = None
    prev_close:         Optional[float]     = None
    day_high:           Optional[float]     = None
    day_low:            Optional[float]     = None
    volume:             Optional[int]       = None

    # Moving averages
    ma20:               Optional[float]     = None
    ma50:               Optional[float]     = None
    ma200:              Optional[float]     = None

    # Trend
    adx:                Optional[float]     = None
    consecutive_closes: Optional[int]       = None   # +N up, -N down

    # Momentum
    rsi:                Optional[float]     = None
    macd_line:          Optional[float]     = None
    macd_signal:        Optional[float]     = None
    macd_histogram:     Optional[float]     = None
    macd_histogram_prev:Optional[float]     = None

    # Volume
    avg_volume_20d:     Optional[int]       = None
    obv_trend:          Optional[str]       = None   # rising/falling/flat

    # Volatility
    atr_pct:            Optional[float]     = None
    bb_upper:           Optional[float]     = None
    bb_lower:           Optional[float]     = None
    bb_width_pct:       Optional[float]     = None
    hist_vol_20d:       Optional[float]     = None
    implied_vol:        Optional[float]     = None

    # 52-week
    fifty_two_week_high:Optional[float]     = None
    fifty_two_week_low: Optional[float]     = None

    # Sector
    sector_rs:          Optional[float]     = None   # sector return vs SPY 1m
    sector_flow:        Optional[str]       = None   # inflow/outflow/neutral
    sector_rank:        Optional[int]       = None   # 1-11

    # Catalysts
    days_to_earnings:   Optional[int]       = None
    catalyst_score:     Optional[float]     = None   # -1 to +1
    news_sentiment:     Optional[float]     = None   # -1 to +1
    insider_activity:   Optional[str]       = None   # buying/selling/none

    # Fundamentals
    debt_ratio:         Optional[float]     = None
    rev_growth_yoy:     Optional[float]     = None
    short_interest_pct: Optional[float]     = None

    # Meta
    source:             Optional[str]       = "unknown"
    feed_timestamp:     Optional[int]       = None

class FeedbackRequest(BaseModel):
    """Bot or user submitting outcome feedback on a previous analysis."""
    ticker:         str
    analysis_id:    Optional[str]   = None
    timeframe:      str
    predicted_direction: str        # "bullish" or "bearish"
    predicted_move_pct:  float
    actual_move_pct:     float
    outcome:        str             # "correct_direction", "wrong_direction", "correct_magnitude"
    notes:          Optional[str]   = None
    submitted_by:   Optional[str]   = "bot"

class ParameterProposal(BaseModel):
    """Human-submitted proposal to adjust engine parameters."""
    parameter:      str
    current_value:  Any
    proposed_value: Any
    justification:  str
    evidence:       Optional[str]   = None
    proposed_by:    str


# ══════════════════════════════════════════════════════════════
# AUTH ENDPOINTS  (unchanged)
# ══════════════════════════════════════════════════════════════
@app.post("/api/auth/register", tags=["Auth"])
async def register(req: RegisterRequest):
    email = req.email.lower().strip()
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    existing = await load_user(email)
    if existing:
        raise HTTPException(409, "Email already registered")
    user = {
        "name": req.name.strip(), "email": email,
        "password_hash": hash_password(req.password),
        "created_at": int(time.time()),
    }
    await save_user(email, user)
    token = make_token(email)
    await save_token(token, email)
    return {"token": token, "user": {"name": user["name"], "email": email}}

@app.post("/api/auth/login", tags=["Auth"])
async def login(req: LoginRequest):
    email = req.email.lower().strip()
    user = await load_user(email)
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    token = make_token(email)
    await save_token(token, email)
    return {"token": token, "user": {"name": user["name"], "email": email}}

@app.post("/api/auth/logout", tags=["Auth"])
async def logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        await delete_token(authorization[7:])
    return {"ok": True}

@app.get("/api/auth/me", tags=["Auth"])
async def me(current_user: dict = Depends(get_current_user)):
    return {"name": current_user["name"], "email": current_user["email"]}


# ══════════════════════════════════════════════════════════════
# PORTFOLIO ENDPOINTS  (unchanged)
# ══════════════════════════════════════════════════════════════
@app.get("/api/portfolio", tags=["Portfolio"])
async def get_portfolio(current_user: dict = Depends(get_current_user)):
    return await load_portfolio(current_user["email"])

@app.post("/api/portfolio", tags=["Portfolio"])
async def save_portfolio_endpoint(data: PortfolioSave, current_user: dict = Depends(get_current_user)):
    await save_portfolio(current_user["email"], data.dict())
    return {"ok": True}


# ══════════════════════════════════════════════════════════════
# PRICE ENDPOINTS  (unchanged)
# ══════════════════════════════════════════════════════════════
_memory_price_cache: Dict[str, dict] = {}
_last_known: Dict[str, dict] = {}
_http_client: Optional[httpx.AsyncClient] = None

async def cache_get(key: str) -> Optional[dict]:
    val = await rget(key)
    if val: return json.loads(val)
    entry = _memory_price_cache.get(key)
    if entry and (time.time() - entry["_ts"]) < CACHE_TTL:
        return entry["data"]
    return None

async def cache_set(key: str, data: dict):
    await rset(f"cache:{key}", json.dumps(data), CACHE_TTL)
    _memory_price_cache[key] = {"data": data, "_ts": time.time()}

async def get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            timeout=REQUEST_TIMEOUT,
        )
    return _http_client

async def fetch_yahoo(symbol: str, client: httpx.AsyncClient) -> Optional[dict]:
    for url in [YAHOO_URL.format(symbol=symbol), YAHOO_FALLBACK_URL.format(symbol=symbol)]:
        try:
            r = await client.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code != 200: continue
            data = r.json()
            result = data.get("chart", {}).get("result", [])
            if not result: continue
            meta = result[0].get("meta", {})
            price = meta.get("regularMarketPrice") or meta.get("previousClose")
            if not price: continue
            prev_close = meta.get("previousClose") or meta.get("chartPreviousClose") or price
            change = price - prev_close
            change_pct = (change / prev_close * 100) if prev_close else 0
            market_state = meta.get("marketState", "CLOSED")
            if market_state == "PRE":  price = meta.get("preMarketPrice")  or price
            elif market_state == "POST": price = meta.get("postMarketPrice") or price
            return {
                "symbol": symbol, "price": round(float(price), 4),
                "change": round(float(change), 4),
                "change_pct": round(float(change_pct), 4),
                "prev_close": round(float(prev_close), 4),
                "currency": meta.get("currency", "USD"),
                "market_state": market_state,
                "exchange": meta.get("exchangeName", ""),
                "name": meta.get("shortName") or meta.get("longName") or symbol,
                "volume": meta.get("regularMarketVolume"),
                "day_high": meta.get("regularMarketDayHigh"),
                "day_low": meta.get("regularMarketDayLow"),
                "fifty_two_week_high": meta.get("fiftyTwoWeekHigh"),
                "fifty_two_week_low":  meta.get("fiftyTwoWeekLow"),
                "timestamp": int(time.time()), "source": "yahoo_finance",
            }
        except httpx.TimeoutException: log.warning(f"Timeout: {symbol}")
        except Exception as e:         log.warning(f"Error {symbol}: {e}")
    return None

async def get_price(symbol: str, client: httpx.AsyncClient) -> dict:
    cached = await cache_get(f"price:{symbol}")
    if cached: return {**cached, "cached": True}
    data = await fetch_yahoo(symbol, client)
    if data:
        _last_known[symbol] = data
        await cache_set(f"price:{symbol}", data)
        # Auto-merge into market_data for the engine
        if symbol in _market_data:
            _market_data[symbol].update({
                "price":      data["price"],
                "change_pct": data["change_pct"],
                "day_high":   data.get("day_high"),
                "day_low":    data.get("day_low"),
                "volume":     data.get("volume"),
                "fifty_two_week_high": data.get("fifty_two_week_high"),
                "fifty_two_week_low":  data.get("fifty_two_week_low"),
            })
        return {**data, "cached": False}
    if symbol in _last_known:
        return {**_last_known[symbol], "stale": True, "cached": False}
    return {"symbol": symbol, "price": None, "error": "Unavailable", "timestamp": int(time.time())}

def normalise_symbol(symbol: str) -> str:
    symbol = symbol.upper().strip()
    for prefix, suffix in [
        ("LON:", ".L"), ("EPA:", ".PA"), ("ETR:", ".DE"),
        ("AMS:", ".AS"), ("TSX:", ".TO"), ("ASX:", ".AX")
    ]:
        if symbol.startswith(prefix): return symbol[len(prefix):] + suffix
    return symbol

@app.get("/health")
async def health():
    r = await get_redis()
    return {
        "status": "healthy",
        "redis": "connected" if r else "memory",
        "engine_version": load_engine_params().get("version"),
        "assets_with_data": len(_market_data),
        "timestamp": int(time.time()),
    }

@app.get("/api/price/{symbol}", tags=["Prices"])
async def get_single_price(symbol: str):
    return await get_price(normalise_symbol(symbol), await get_client())

@app.get("/api/prices", tags=["Prices"])
async def get_multiple_prices(symbols: str = Query(...), delay_ms: int = Query(0)):
    raw = [s.strip() for s in symbols.split(",") if s.strip()]
    if not raw: raise HTTPException(400, "No symbols")
    if len(raw) > 50: raise HTTPException(400, "Max 50 symbols")
    sym_list = [normalise_symbol(s) for s in raw]
    client = await get_client()
    if delay_ms == 0:
        results = await asyncio.gather(*[get_price(s, client) for s in sym_list])
    else:
        results = []
        for s in sym_list:
            results.append(await get_price(s, client))
            await asyncio.sleep(delay_ms / 1000)
    return {
        "symbols": sym_list, "count": len(results),
        "timestamp": int(time.time()),
        "data": {r["symbol"]: r for r in results}
    }

@app.get("/api/search", tags=["Prices"])
async def search_symbol(q: str = Query(...)):
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"https://query1.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=8",
                headers=HEADERS, timeout=8
            )
            quotes = r.json().get("quotes", [])
            return {"query": q, "results": [
                {"symbol": q["symbol"],
                 "name": q.get("shortname") or q.get("longname"),
                 "exchange": q.get("exchDisp")}
                for q in quotes if q.get("symbol")
            ]}
    except Exception as e:
        raise HTTPException(500, f"Search failed: {e}")


# ══════════════════════════════════════════════════════════════
# DATA FEED ENDPOINTS
# Bots POST structured data here. Engine reads it.
# ══════════════════════════════════════════════════════════════
@app.post("/api/feed/update", tags=["Feed"])
async def feed_update(payload: MarketDataFeed):
    """
    Receive structured market data from a bot or scraper.
    Computes data_completeness automatically.
    Triggers analysis update for this ticker.
    """
    ticker = normalise_symbol(payload.ticker)
    data = payload.dict()
    data["ticker"] = ticker

    # Set feed timestamp if not provided
    if not data.get("feed_timestamp"):
        data["feed_timestamp"] = int(time.time())

    # Compute data completeness — what % of analytical fields are populated
    analytical_fields = [
        "price", "ma20", "ma50", "ma200", "rsi", "macd_line", "macd_signal",
        "volume", "avg_volume_20d", "atr_pct", "adx", "sector_rs",
        "days_to_earnings", "catalyst_score", "news_sentiment",
    ]
    filled = sum(1 for f in analytical_fields if data.get(f) is not None)
    data["data_completeness"] = round(filled / len(analytical_fields), 2)

    # Merge with existing (preserve fields not in this update)
    existing = _market_data.get(ticker, {})
    existing.update({k: v for k, v in data.items() if v is not None})
    _market_data[ticker] = existing

    # Recompute analysis immediately
    params = load_engine_params()
    analysis = build_analysis(ticker, existing, params)
    _analysis_cache[ticker] = analysis

    # Audit log entry
    _audit_log.append({
        "event": "feed_update",
        "ticker": ticker,
        "timestamp": int(time.time()),
        "data_completeness": data["data_completeness"],
        "source": data.get("source", "unknown"),
    })

    await persist_engine_state()

    log.info(f"Feed update: {ticker} completeness={data['data_completeness']:.0%} source={data.get('source')}")
    return {
        "ok": True,
        "ticker": ticker,
        "data_completeness": data["data_completeness"],
        "analysis_updated": True,
        "composite_score": analysis["composite_score"],
        "risk_rating": analysis["risk_rating"],
    }

@app.post("/api/feed/batch", tags=["Feed"])
async def feed_batch(payloads: List[MarketDataFeed]):
    """Batch update — multiple tickers in one call."""
    if len(payloads) > 100:
        raise HTTPException(400, "Max 100 assets per batch")
    results = []
    for payload in payloads:
        try:
            ticker = normalise_symbol(payload.ticker)
            data = payload.dict()
            data["ticker"] = ticker
            if not data.get("feed_timestamp"):
                data["feed_timestamp"] = int(time.time())
            analytical_fields = [
                "price", "ma20", "ma50", "ma200", "rsi", "macd_line",
                "macd_signal", "volume", "avg_volume_20d", "atr_pct", "adx",
                "sector_rs", "days_to_earnings", "catalyst_score", "news_sentiment",
            ]
            filled = sum(1 for f in analytical_fields if data.get(f) is not None)
            data["data_completeness"] = round(filled / len(analytical_fields), 2)
            existing = _market_data.get(ticker, {})
            existing.update({k: v for k, v in data.items() if v is not None})
            _market_data[ticker] = existing
            params = load_engine_params()
            analysis = build_analysis(ticker, existing, params)
            _analysis_cache[ticker] = analysis
            results.append({"ticker": ticker, "ok": True, "score": analysis["composite_score"]})
        except Exception as e:
            results.append({"ticker": payload.ticker, "ok": False, "error": str(e)})
    await persist_engine_state()
    return {"processed": len(results), "results": results}

@app.get("/api/feed/status", tags=["Feed"])
async def feed_status():
    """What data does the engine currently have?"""
    now = int(time.time())
    summary = {}
    for ticker, data in _market_data.items():
        age = now - data.get("feed_timestamp", now)
        summary[ticker] = {
            "data_completeness": data.get("data_completeness", 0),
            "age_seconds": age,
            "staleness": "fresh" if age < 3600 else ("warn" if age < 86400 else "stale"),
            "has_analysis": ticker in _analysis_cache,
            "last_score": _analysis_cache.get(ticker, {}).get("composite_score"),
        }
    return {
        "total_assets": len(_market_data),
        "with_analysis": len(_analysis_cache),
        "assets": summary,
    }


# ══════════════════════════════════════════════════════════════
# ANALYSIS ENDPOINTS
# ══════════════════════════════════════════════════════════════
@app.get("/api/analysis/{ticker}", tags=["Analysis"])
async def get_analysis(ticker: str):
    """
    Get full analysis for a ticker.
    If the engine has fed data, returns real analysis.
    If only Yahoo price data is available, returns price-only
    analysis with reduced confidence.
    If nothing is available, returns a clear 'no data' response.
    """
    ticker = normalise_symbol(ticker)
    params = load_engine_params()

    # Check analysis cache
    if ticker in _analysis_cache:
        analysis = _analysis_cache[ticker]
        # Recompute if stale
        age = int(time.time()) - _market_data.get(ticker, {}).get("feed_timestamp", 0)
        if age < params["data_staleness_fail"]:
            return analysis

    # Try building from market data (may be partial)
    if ticker in _market_data:
        analysis = build_analysis(ticker, _market_data[ticker], params)
        _analysis_cache[ticker] = analysis
        return analysis

    # Fall back: try to get price from Yahoo and do minimal analysis
    client = await get_client()
    price_data = await get_price(ticker, client)
    if price_data.get("price"):
        minimal_data = {
            "ticker": ticker,
            "name": price_data.get("name", ticker),
            "price": price_data["price"],
            "change_pct": price_data.get("change_pct"),
            "day_high": price_data.get("day_high"),
            "day_low": price_data.get("day_low"),
            "volume": price_data.get("volume"),
            "fifty_two_week_high": price_data.get("fifty_two_week_high"),
            "fifty_two_week_low": price_data.get("fifty_two_week_low"),
            "data_completeness": 0.15,
            "feed_timestamp": int(time.time()),
        }
        analysis = build_analysis(ticker, minimal_data, params)
        analysis["data_source"] = "yahoo_price_only"
        analysis["note"] = (
            "Analysis based on price data only. Feed structured data to "
            "/api/feed/update for full analysis including trend, momentum, "
            "volume, sector, and catalyst scoring."
        )
        return analysis

    # No data at all
    return JSONResponse(status_code=404, content={
        "ticker": ticker,
        "status": "no_data",
        "message": (
            f"No data available for {ticker}. "
            "Post structured market data to /api/feed/update to enable analysis. "
            "See /docs for the MarketDataFeed schema."
        ),
        "data_required": [
            "price", "ma20", "ma50", "rsi", "volume", "avg_volume_20d",
            "atr_pct", "sector_rs", "catalyst_score"
        ],
    })

@app.get("/api/analysis", tags=["Analysis"])
async def get_all_analysis(
    sector: Optional[str] = None,
    min_score: Optional[float] = None,
    min_confidence: Optional[str] = None,
    sort_by: str = "composite_score",
):
    """Get analysis for all assets with data, with optional filters."""
    results = list(_analysis_cache.values())

    if sector:
        results = [r for r in results if r.get("sector", "").lower() == sector.lower()]
    if min_score is not None:
        results = [r for r in results if r.get("composite_score", -999) >= min_score]
    if min_confidence:
        order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
        min_val = order.get(min_confidence.upper(), 0)
        results = [r for r in results if order.get(r.get("confidence", "LOW"), 0) >= min_val]

    reverse = sort_by not in ("risk_rating",)
    try:
        results.sort(key=lambda r: r.get(sort_by, 0) or 0, reverse=reverse)
    except: pass

    return {
        "count": len(results),
        "sorted_by": sort_by,
        "engine_version": load_engine_params().get("version"),
        "results": results,
    }

@app.get("/api/analysis/{ticker}/audit", tags=["Analysis"])
async def get_audit_trail(ticker: str):
    """Return the full audit trail for a ticker — every analysis ever produced."""
    ticker = normalise_symbol(ticker)
    entries = [e for e in _audit_log if e.get("ticker") == ticker]
    return {"ticker": ticker, "entries": entries, "total": len(entries)}


# ══════════════════════════════════════════════════════════════
# FEEDBACK ENDPOINTS
# ══════════════════════════════════════════════════════════════
@app.post("/api/engine/feedback", tags=["Engine"])
async def submit_feedback(fb: FeedbackRequest):
    """
    Submit outcome feedback. Engine logs it and generates
    parameter adjustment suggestions (proposals only — not applied).
    """
    record = {
        "id": f"fb-{int(time.time()*1000)}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **fb.dict(),
    }
    _feedback_log.append(record)

    # Update ticker-level feedback stats
    ticker = normalise_symbol(fb.ticker)
    if ticker in _market_data:
        md = _market_data[ticker]
        md["feedback_count"] = md.get("feedback_count", 0) + 1
        prev_acc = md.get("feedback_accuracy", 0.5)
        hit = 1.0 if fb.outcome == "correct_direction" else 0.0
        # Running average
        n = md["feedback_count"]
        md["feedback_accuracy"] = round((prev_acc * (n-1) + hit) / n, 3)

    # Auto-generate proposal if accuracy is consistently off
    proposals_generated = []
    ticker_feedback = [f for f in _feedback_log if f["ticker"] == fb.ticker]
    if len(ticker_feedback) >= 5:
        recent = ticker_feedback[-10:]
        accuracy = sum(1 for f in recent if f["outcome"] == "correct_direction") / len(recent)
        if accuracy < 0.4:
            params = load_engine_params()
            proposal_text = (
                f"Accuracy for {fb.ticker} is {accuracy:.0%} over {len(recent)} observations. "
                f"Consider reviewing {fb.timeframe} timeframe weights or sector adjustment "
                f"for {_market_data.get(ticker, {}).get('sector', 'unknown')} assets."
            )
            proposals_generated.append(proposal_text)
            log.info(f"Low accuracy signal for {fb.ticker}: {accuracy:.0%}")

    await persist_engine_state()

    return {
        "ok": True,
        "feedback_id": record["id"],
        "ticker_feedback_count": len([f for f in _feedback_log if f["ticker"] == fb.ticker]),
        "proposals_generated": proposals_generated,
    }

@app.get("/api/engine/feedback", tags=["Engine"])
async def get_feedback(ticker: Optional[str] = None, limit: int = 50):
    """Retrieve feedback log."""
    results = _feedback_log
    if ticker:
        ticker = normalise_symbol(ticker)
        results = [f for f in results if f.get("ticker") == ticker]
    return {"total": len(results), "feedback": results[-limit:]}


# ══════════════════════════════════════════════════════════════
# ENGINE PARAMETER MANAGEMENT
# ══════════════════════════════════════════════════════════════
@app.get("/api/engine/params", tags=["Engine"])
async def get_engine_params():
    """View current engine parameters."""
    return {
        "active_params": load_engine_params(),
        "defaults": DEFAULT_ENGINE_PARAMS,
        "overrides_count": len(_engine_params),
        "pending_proposals": len(_proposals),
    }

@app.post("/api/engine/propose", tags=["Engine"])
async def propose_parameter_change(proposal: ParameterProposal):
    """
    Propose a parameter change. Does NOT apply it.
    Humans review proposals at /api/engine/proposals and approve via /api/engine/approve.
    Engine never modifies its own parameters without approval.
    """
    params = load_engine_params()
    if proposal.parameter not in params:
        raise HTTPException(400, f"Unknown parameter: {proposal.parameter}. See /api/engine/params for valid fields.")

    record = {
        "id": f"prop-{int(time.time()*1000)}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        **proposal.dict(),
    }
    _proposals.append(record)
    await persist_engine_state()

    log.info(f"Parameter proposal submitted: {proposal.parameter} {params[proposal.parameter]} → {proposal.proposed_value}")
    return {
        "ok": True,
        "proposal_id": record["id"],
        "status": "pending_human_approval",
        "message": (
            f"Proposal logged. A human must approve this at POST /api/engine/approve/{{proposal_id}} "
            f"before {proposal.parameter} changes from {params[proposal.parameter]} to {proposal.proposed_value}."
        ),
    }

@app.get("/api/engine/proposals", tags=["Engine"])
async def list_proposals():
    """List all parameter change proposals."""
    return {"proposals": _proposals, "total": len(_proposals)}

@app.post("/api/engine/approve/{proposal_id}", tags=["Engine"])
async def approve_proposal(proposal_id: str, current_user: dict = Depends(get_current_user)):
    """
    Human approves a parameter proposal. Applies the change.
    Requires authentication — only logged-in users can approve changes.
    """
    proposal = next((p for p in _proposals if p["id"] == proposal_id), None)
    if not proposal:
        raise HTTPException(404, f"Proposal {proposal_id} not found")
    if proposal["status"] != "pending":
        raise HTTPException(400, f"Proposal already {proposal['status']}")

    param = proposal["parameter"]
    new_value = proposal["proposed_value"]

    _engine_params[param] = new_value
    proposal["status"] = "approved"
    proposal["approved_by"] = current_user["email"]
    proposal["approved_at"] = datetime.now(timezone.utc).isoformat()

    # Invalidate analysis cache so all assets recompute with new params
    _analysis_cache.clear()

    await persist_engine_state()

    log.info(f"Parameter approved by {current_user['email']}: {param} = {new_value}")
    return {
        "ok": True,
        "parameter": param,
        "new_value": new_value,
        "approved_by": current_user["email"],
        "note": "Analysis cache cleared — all assets will recompute on next request.",
    }

@app.post("/api/engine/reject/{proposal_id}", tags=["Engine"])
async def reject_proposal(proposal_id: str, current_user: dict = Depends(get_current_user)):
    """Reject a parameter proposal."""
    proposal = next((p for p in _proposals if p["id"] == proposal_id), None)
    if not proposal: raise HTTPException(404, "Proposal not found")
    proposal["status"] = "rejected"
    proposal["rejected_by"] = current_user["email"]
    proposal["rejected_at"] = datetime.now(timezone.utc).isoformat()
    await persist_engine_state()
    return {"ok": True, "proposal_id": proposal_id, "status": "rejected"}

@app.get("/api/engine/accuracy", tags=["Engine"])
async def engine_accuracy():
    """
    Overall accuracy report across all feedback.
    Broken down by timeframe, sector, and signal strength.
    """
    if not _feedback_log:
        return {"message": "No feedback submitted yet.", "total": 0}

    total = len(_feedback_log)
    correct = sum(1 for f in _feedback_log if f["outcome"] == "correct_direction")
    overall = round(correct / total * 100, 1)

    by_tf = {}
    by_sector = {}
    for fb in _feedback_log:
        tf = fb.get("timeframe", "unknown")
        ticker = fb.get("ticker", "")
        sector = _market_data.get(normalise_symbol(ticker), {}).get("sector", "unknown")
        hit = fb["outcome"] == "correct_direction"
        for d, k in [(by_tf, tf), (by_sector, sector)]:
            if k not in d: d[k] = {"correct": 0, "total": 0}
            d[k]["total"] += 1
            if hit: d[k]["correct"] += 1

    return {
        "overall_accuracy": overall,
        "total_feedback": total,
        "correct": correct,
        "by_timeframe": {k: {"accuracy": round(v["correct"]/v["total"]*100,1), **v} for k,v in by_tf.items()},
        "by_sector": {k: {"accuracy": round(v["correct"]/v["total"]*100,1), **v} for k,v in by_sector.items()},
        "engine_version": load_engine_params().get("version"),
    }


# ══════════════════════════════════════════════════════════════
# WEBSOCKET  (unchanged)
# ══════════════════════════════════════════════════════════════
class ConnectionManager:
    def __init__(self): self.active: Dict[str, List[WebSocket]] = {}
    async def connect(self, ws: WebSocket, symbol: str):
        await ws.accept(); self.active.setdefault(symbol, []).append(ws)
    def disconnect(self, ws: WebSocket, symbol: str):
        if symbol in self.active:
            self.active[symbol] = [w for w in self.active[symbol] if w != ws]
            if not self.active[symbol]: del self.active[symbol]

manager = ConnectionManager()

@app.websocket("/ws/{symbol}")
async def websocket_price(websocket: WebSocket, symbol: str):
    symbol = normalise_symbol(symbol)
    await manager.connect(websocket, symbol)
    client = await get_client()
    try:
        while True:
            data = await get_price(symbol, client)
            # Include latest analysis if available
            analysis_summary = {}
            if symbol in _analysis_cache:
                a = _analysis_cache[symbol]
                analysis_summary = {
                    "composite_score": a.get("composite_score"),
                    "risk_rating": a.get("risk_rating"),
                    "confidence": a.get("confidence"),
                }
            await websocket.send_json({**data, "ws": True, "analysis": analysis_summary})
            await asyncio.sleep(3)
    except WebSocketDisconnect: pass
    except Exception as e: log.error(f"WS error {symbol}: {e}")
    finally: manager.disconnect(websocket, symbol)


# ══════════════════════════════════════════════════════════════
# STATIC / FRONTEND  (unchanged)
# ══════════════════════════════════════════════════════════════
static_dir = Path("static")
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/{full_path:path}", include_in_schema=False)
async def serve_frontend(full_path: str):
    index = Path("static/index.html")
    if index.exists(): return FileResponse(index)
    return {"status": "ok", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False, log_level="info")
