"""
Market Brain — 📐 Technical Levels Bot
─────────────────────────────────────────
Data sources (priority order):
  1. Polygon.io — real-time US data, proper OHLCV, faster refresh
  2. Yahoo Finance — fallback, UK/global stocks

Analyses:
  - 52-week high/low proximity
  - MA20, MA50, MA200 position
  - Golden cross / death cross detection
  - Bollinger Band position
  - Support/resistance pivot levels
  - ATR-based volatility regime
"""

import logging
import os
from typing import List, Optional, Tuple

import httpx

from base import ResearchBot, BotResult

log = logging.getLogger("mb.bots.technicallevels")

POLYGON_API_KEY = os.environ.get("POLYGON_KEY", "phpD3Q34FRcLIb1kphSqBAQjDBWKrb_y")
POLYGON_BASE    = "https://api.polygon.io/v2"
YAHOO_CHART     = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_HEADERS   = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}
CACHE_TTL = 3600


def _is_us_ticker(ticker: str) -> bool:
    non_us = [".L", ".PA", ".DE", ".AS", ".TO", ".AX", "=X", "-USD"]
    return not any(ticker.endswith(s) for s in non_us)


def _calc_ma(prices: List[float], period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def _calc_bollinger(prices: List[float], period: int = 20) -> Optional[Tuple]:
    if len(prices) < period:
        return None
    mid      = sum(prices[-period:]) / period
    variance = sum((p - mid) ** 2 for p in prices[-period:]) / period
    std      = variance ** 0.5
    return (mid + 2 * std, mid, mid - 2 * std)


def _find_pivots(highs: List[float], lows: List[float], window: int = 5) -> Tuple:
    resistance, support = [], []
    for i in range(window, len(highs) - window):
        if all(highs[i] >= highs[i-j] and highs[i] >= highs[i+j] for j in range(1, window+1)):
            resistance.append(highs[i])
        if all(lows[i] <= lows[i-j] and lows[i] <= lows[i+j] for j in range(1, window+1)):
            support.append(lows[i])
    return resistance[-3:], support[-3:]


async def _fetch_polygon_ohlcv(ticker: str) -> Optional[dict]:
    """Polygon.io OHLCV — US stocks, real-time data."""
    try:
        from datetime import datetime, timedelta
        end   = datetime.now()
        start = end - timedelta(days=400)
        url   = f"{POLYGON_BASE}/aggs/ticker/{ticker}/range/1/day/{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(url, params={
                "adjusted": "true", "sort": "asc", "limit": 365,
                "apiKey": POLYGON_API_KEY,
            })
            if r.status_code != 200:
                return None
            data    = r.json()
            results = data.get("results", [])
            if len(results) < 50:
                return None
            closes = [bar["c"] for bar in results]
            highs  = [bar["h"] for bar in results]
            lows   = [bar["l"] for bar in results]
            volumes= [bar["v"] for bar in results]
            return {"closes": closes, "highs": highs, "lows": lows,
                    "volumes": volumes, "source": "Polygon"}
    except Exception as e:
        log.warning(f"Polygon fetch failed for {ticker}: {e}")
        return None


async def _fetch_yahoo_ohlcv(ticker: str) -> Optional[dict]:
    """Yahoo Finance OHLCV fallback."""
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(YAHOO_CHART.format(symbol=ticker),
                params={"interval": "1d", "range": "1y"}, headers=YAHOO_HEADERS)
            if r.status_code != 200:
                return None
            data   = r.json()
            result = data.get("chart", {}).get("result", [{}])[0]
            quotes = result.get("indicators", {}).get("quote", [{}])[0]
            closes  = [c for c in (quotes.get("close")  or []) if c is not None]
            highs   = [h for h in (quotes.get("high")   or []) if h is not None]
            lows    = [l for l in (quotes.get("low")    or []) if l is not None]
            volumes = [v for v in (quotes.get("volume") or []) if v is not None]
            if len(closes) < 50:
                return None
            return {"closes": closes, "highs": highs, "lows": lows,
                    "volumes": volumes, "meta": result.get("meta", {}),
                    "source": "Yahoo Finance"}
    except Exception as e:
        log.warning(f"Yahoo OHLCV failed for {ticker}: {e}")
        return None


class TechnicalLevelsBot(ResearchBot):

    @property
    def name(self) -> str:
        return "TechnicalLevelsBot"

    @property
    def cache_ttl(self) -> int:
        return CACHE_TTL

    async def _fetch(self, ticker: str, asset_meta: dict) -> BotResult:
        # Polygon for US, Yahoo for UK/global
        ohlcv = None
        if _is_us_ticker(ticker):
            ohlcv = await _fetch_polygon_ohlcv(ticker)
        if not ohlcv:
            ohlcv = await _fetch_yahoo_ohlcv(ticker)

        if not ohlcv:
            return self._empty_result(ticker, "Price history unavailable")

        closes  = ohlcv["closes"]
        highs   = ohlcv["highs"]
        lows    = ohlcv["lows"]
        source  = ohlcv["source"]
        current = closes[-1]

        ma20  = _calc_ma(closes, 20)
        ma50  = _calc_ma(closes, 50)
        ma200 = _calc_ma(closes, 200) if len(closes) >= 200 else None
        bb    = _calc_bollinger(closes, 20)

        # 52-week range
        meta      = ohlcv.get("meta", {})
        year_high = meta.get("fiftyTwoWeekHigh") or max(highs[-252:] if len(highs) >= 252 else highs)
        year_low  = meta.get("fiftyTwoWeekLow")  or min(lows[-252:]  if len(lows)  >= 252 else lows)
        year_range = year_high - year_low
        year_pos   = ((current - year_low) / year_range * 100) if year_range > 0 else 50

        # ATR
        atr = None
        if len(highs) >= 14 and len(lows) >= 14:
            tr_vals = [highs[-i] - lows[-i] for i in range(1, 15)]
            atr     = sum(tr_vals) / 14
            atr_pct = (atr / current) * 100

        # Pivot support/resistance
        resistance_levels, support_levels = _find_pivots(highs, lows)
        supports_below    = sorted([s for s in support_levels    if s < current], reverse=True)
        resistances_above = sorted([r for r in resistance_levels if r > current])
        nearest_support    = supports_below[0]    if supports_below    else year_low
        nearest_resistance = resistances_above[0] if resistances_above else year_high
        support_pct    = (current - nearest_support)    / current * 100
        resistance_pct = (nearest_resistance - current) / current * 100

        # Cross detection
        golden_cross = death_cross = None
        if ma50 and ma200 and len(closes) > 1:
            prev_ma50  = _calc_ma(closes[:-1], 50)
            prev_ma200 = _calc_ma(closes[:-1], 200) if len(closes[:-1]) >= 200 else None
            if prev_ma50 and prev_ma200:
                if prev_ma50 < prev_ma200 and ma50 > ma200:
                    golden_cross = True
                elif prev_ma50 > prev_ma200 and ma50 < ma200:
                    death_cross = True

        bull_factors, bear_factors = [], []

        # 52-week position
        if year_pos >= 90:
            bull_factors.append(f"Near 52-week high ({year_pos:.0f}th percentile) — strong momentum")
        elif year_pos >= 70:
            bull_factors.append(f"Upper range of 52-week channel ({year_pos:.0f}th percentile)")
        elif year_pos <= 15:
            bear_factors.append(f"Near 52-week low ({year_pos:.0f}th percentile) — potential value or falling knife")
        elif year_pos <= 35:
            bear_factors.append(f"Lower 52-week range ({year_pos:.0f}th percentile)")

        if ma50:
            pct = (current - ma50) / ma50 * 100
            if pct > 0:
                bull_factors.append(f"Trading {pct:.1f}% above MA50 — uptrend confirmed")
            else:
                bear_factors.append(f"Trading {abs(pct):.1f}% below MA50 — downtrend")

        if ma200:
            if current > ma200:
                bull_factors.append("Above 200-day MA — long-term uptrend intact")
            else:
                bear_factors.append("Below 200-day MA — long-term downtrend")

        if golden_cross:
            bull_factors.append("Golden cross (MA50 > MA200) — strong technical buy signal")
        if death_cross:
            bear_factors.append("Death cross (MA50 < MA200) — strong technical sell signal")

        if support_pct < 3:
            bull_factors.append(f"Near support at {nearest_support:.2f} — potential bounce zone")
        if resistance_pct < 3:
            bear_factors.append(f"Near resistance at {nearest_resistance:.2f} — potential ceiling")
        elif resistance_pct > 15:
            bull_factors.append(f"Clear runway to resistance at {nearest_resistance:.2f} (+{resistance_pct:.1f}%)")

        if bb:
            upper, middle, lower = bb
            if current > upper:
                bear_factors.append("Above upper Bollinger Band — overbought, mean reversion risk")
            elif current < lower:
                bull_factors.append("Below lower Bollinger Band — oversold, mean reversion potential")

        if not bull_factors:
            bull_factors.append("No major technical resistance nearby")
        if not bear_factors:
            bear_factors.append(f"Support at {nearest_support:.2f} ({support_pct:.1f}% downside)")

        trend   = "uptrend" if (ma50 and current > ma50) else "downtrend"
        summary = f"{year_pos:.0f}th percentile 52wk · {trend} · {source}"
        if golden_cross:
            summary += " · golden cross"
        if death_cross:
            summary += " · death cross"

        return BotResult(
            bot_name=self.name, ticker=ticker, signal_inputs={},
            bull_factors=bull_factors[:4], bear_factors=bear_factors[:4],
            summary=summary, confidence=0.8, source=source,
            raw={"current": current, "ma20": round(ma20, 2) if ma20 else None,
                 "ma50": round(ma50, 2) if ma50 else None,
                 "ma200": round(ma200, 2) if ma200 else None,
                 "year_high": round(year_high, 2), "year_low": round(year_low, 2),
                 "year_position_pct": round(year_pos, 1),
                 "nearest_support": round(nearest_support, 2),
                 "nearest_resistance": round(nearest_resistance, 2),
                 "golden_cross": golden_cross, "death_cross": death_cross},
        )
