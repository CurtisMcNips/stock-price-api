"""
Market Brain — Technicals Calculator
─────────────────────────────────────
Fetches 30-day OHLCV from Yahoo Finance and computes:
  - RSI-14
  - MACD (12/26/9)
  - Volume ratio (today vs 20-day avg)
  - Price vs MA50 (%)
  - Price vs MA200 (%) — where data allows
  - ATR% (14-day)

Results are cached in-memory for CACHE_TTL seconds to avoid
hammering Yahoo on every page load.

Usage (from app.py):
    from technicals import get_technicals
    data = await get_technicals("AAPL")   # returns dict or None
"""

import asyncio
import logging
import time
from typing import Optional, Dict, List
import httpx

log = logging.getLogger("mb.technicals")

CACHE_TTL    = 900   # 15 minutes
REQUEST_TIMEOUT = 10
YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

# ── IN-MEMORY CACHE ──────────────────────────────────────────
_cache: Dict[str, dict] = {}   # { ticker: { "ts": float, "data": dict } }


def _cache_get(ticker: str) -> Optional[dict]:
    entry = _cache.get(ticker)
    if entry and (time.time() - entry["ts"]) < CACHE_TTL:
        return entry["data"]
    return None


def _cache_set(ticker: str, data: dict):
    _cache[ticker] = {"ts": time.time(), "data": data}


# ── CALCULATIONS ─────────────────────────────────────────────
def _rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    # Initial averages
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _ema(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def _macd(closes: List[float]) -> Optional[dict]:
    if len(closes) < 35:
        return None
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    if not ema12 or not ema26:
        return None
    # Align lengths — ema26 is shorter
    offset = len(ema12) - len(ema26)
    macd_line = [ema12[i + offset] - ema26[i] for i in range(len(ema26))]
    signal = _ema(macd_line, 9)
    if not signal:
        return None
    macd_val  = macd_line[-1]
    signal_val = signal[-1]
    hist       = macd_val - signal_val
    # Normalise to -1..1 relative to recent price for signal engine
    price_scale = closes[-1] if closes[-1] > 0 else 1
    macd_norm = max(-1, min(1, hist / (price_scale * 0.02)))
    return {
        "macd":        round(macd_val, 4),
        "signal":      round(signal_val, 4),
        "histogram":   round(hist, 4),
        "macd_norm":   round(macd_norm, 3),   # -1..1 for signal engine
        "bullish":     hist > 0,
    }


def _ma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _atr_pct(highs, lows, closes, period=14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    atr = sum(trs[-period:]) / period
    return round((atr / closes[-1]) * 100, 2) if closes[-1] > 0 else None


# ── YAHOO FETCH ──────────────────────────────────────────────
async def _fetch_yahoo_history(ticker: str) -> Optional[dict]:
    """Fetch 60 days of daily OHLCV from Yahoo Finance v8 chart endpoint."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {
        "interval": "1d",
        "range":    "60d",
        "events":   "div,splits",
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            r = await client.get(url, params=params, headers=YAHOO_HEADERS)
            if r.status_code != 200:
                log.warning(f"Yahoo history {ticker}: HTTP {r.status_code}")
                return None
            return r.json()
    except Exception as e:
        log.warning(f"Yahoo history fetch error {ticker}: {e}")
        return None


def _parse_history(data: dict) -> Optional[dict]:
    """Parse Yahoo chart response into OHLCV lists."""
    try:
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None
        r = result[0]
        timestamps = r.get("timestamp", [])
        indicators = r.get("indicators", {})
        quote = indicators.get("quote", [{}])[0]
        adjclose = indicators.get("adjclose", [{}])
        closes_adj = adjclose[0].get("adjclose", []) if adjclose else []

        opens  = quote.get("open", [])
        highs  = quote.get("high", [])
        lows   = quote.get("low", [])
        closes = quote.get("close", [])
        volumes = quote.get("volume", [])

        # Filter out None values (market closed days)
        valid = [(o, h, l, c, v, ca) for o, h, l, c, v, ca
                 in zip(opens, highs, lows, closes, volumes,
                        closes_adj if closes_adj else closes)
                 if all(x is not None for x in [o, h, l, c])]
        if len(valid) < 15:
            return None

        return {
            "opens":   [v[0] for v in valid],
            "highs":   [v[1] for v in valid],
            "lows":    [v[2] for v in valid],
            "closes":  [v[3] for v in valid],
            "volumes": [v[4] for v in valid],
            "closes_adj": [v[5] for v in valid],
            "current_price": valid[-1][3],
            "current_volume": valid[-1][4],
        }
    except Exception as e:
        log.warning(f"History parse error: {e}")
        return None


# ── MAIN ENTRY POINT ─────────────────────────────────────────
async def get_technicals(ticker: str) -> Optional[dict]:
    """
    Returns technical indicators for a ticker.
    Cached for 15 minutes. Returns None if Yahoo unavailable.

    Return shape:
    {
        "ticker":          str,
        "rsi":             float,       # 0-100
        "macd_norm":       float,       # -1..1 (for signal engine)
        "macd_bullish":    bool,
        "volume_ratio":    float,       # today vs 20d avg (1.0 = average)
        "price_vs_ma50":   float,       # % above/below MA50
        "price_vs_ma200":  float|None,
        "atr_pct":         float,       # ATR as % of price
        "current_price":   float,
        "cached":          bool,
    }
    """
    cached = _cache_get(ticker)
    if cached:
        return {**cached, "cached": True}

    raw = await _fetch_yahoo_history(ticker)
    if not raw:
        return None

    ohlcv = _parse_history(raw)
    if not ohlcv:
        return None

    closes  = ohlcv["closes"]
    highs   = ohlcv["highs"]
    lows    = ohlcv["lows"]
    volumes = ohlcv["volumes"]

    rsi_val  = _rsi(closes)
    macd_res = _macd(closes)
    ma50     = _ma(closes, 50)
    ma200    = _ma(closes, 200)
    atr      = _atr_pct(highs, lows, closes)
    price    = ohlcv["current_price"]

    # Volume ratio: today vs 20-day average
    vol_avg  = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else None
    vol_ratio = round(ohlcv["current_volume"] / vol_avg, 2) if vol_avg and vol_avg > 0 else 1.0

    price_vs_ma50  = round(((price - ma50)  / ma50)  * 100, 2) if ma50  else None
    price_vs_ma200 = round(((price - ma200) / ma200) * 100, 2) if ma200 else None

    result = {
        "ticker":          ticker,
        "rsi":             rsi_val,
        "macd_norm":       macd_res["macd_norm"]  if macd_res else None,
        "macd_bullish":    macd_res["bullish"]     if macd_res else None,
        "volume_ratio":    vol_ratio,
        "price_vs_ma50":   price_vs_ma50,
        "price_vs_ma200":  price_vs_ma200,
        "atr_pct":         atr,
        "current_price":   round(price, 4),
        "cached":          False,
    }

    _cache_set(ticker, result)
    return result


# ── BATCH ────────────────────────────────────────────────────
async def get_technicals_batch(tickers: List[str], concurrency: int = 6) -> Dict[str, dict]:
    """Fetch technicals for multiple tickers with rate limiting."""
    sem = asyncio.Semaphore(concurrency)
    results = {}

    async def fetch_one(t):
        async with sem:
            data = await get_technicals(t)
            if data:
                results[t] = data
            await asyncio.sleep(0.15)  # gentle on Yahoo

    await asyncio.gather(*[fetch_one(t) for t in tickers])
    return results
