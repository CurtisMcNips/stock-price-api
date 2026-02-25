"""
Market Brain — 📊 Macro Bot
─────────────────────────────
Tracks macro economic events and maps their impact to sector signals.

Produces:
  signal_inputs.sectorFlow   -1.0 to 1.0 (sector rotation signal)

Sources (in priority order):
  1. FRED API — real Federal Reserve economic data (CPI, Fed rate, GDP)
  2. Yahoo Finance sector ETFs — fallback if FRED unavailable

FRED series used:
  FEDFUNDS   — Federal funds rate (interest rate environment)
  CPIAUCSL   — CPI inflation (consumer prices)
  GDP        — GDP growth
  UNRATE     — Unemployment rate
  DGS10      — 10-year treasury yield (risk appetite proxy)
"""

import logging
import os
from typing import Dict, List, Optional, Tuple

import httpx

from base import ResearchBot, BotResult

log = logging.getLogger("mb.bots.macro")

FRED_API_KEY  = os.environ.get("FRED_API_KEY", "c18ec6200f048fa6f236646d5787ee5f")
FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
YAHOO_CHART   = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}
CACHE_TTL = 3600   # 1 hour

# Sector → proxy ETF mapping (Yahoo fallback)
SECTOR_ETF_MAP = {
    "Technology":    "XLK",
    "Finance":       "XLF",
    "Healthcare":    "XLV",
    "Energy":        "XLE",
    "Consumer":      "XLY",
    "Industrials":   "XLI",
    "Metals":        "XLB",
    "Minerals":      "XLB",
    "Agriculture":   "MOO",
    "Real Estate":   "XLRE",
    "Utilities":     "XLU",
    "Crypto":        "COIN",
    "Forex":         "UUP",
    "Space":         "XLI",
}

# How each macro indicator affects each sector
# Values: positive = bullish for sector, negative = bearish
SECTOR_MACRO_SENSITIVITY = {
    #                      rate_env  inflation  growth  unemployment  yields
    "Technology":         [-0.6,     -0.2,       0.7,    -0.3,        -0.5],
    "Finance":            [ 0.7,      0.2,       0.5,    -0.2,         0.6],
    "Healthcare":         [-0.1,     -0.1,       0.3,    -0.1,        -0.1],
    "Energy":             [-0.2,      0.7,       0.4,    -0.1,         0.1],
    "Consumer":           [-0.4,     -0.6,       0.6,    -0.5,        -0.3],
    "Industrials":        [-0.3,      0.1,       0.8,    -0.4,        -0.2],
    "Metals":             [-0.3,      0.6,       0.5,    -0.2,         0.0],
    "Real Estate":        [-0.8,     -0.2,       0.3,    -0.3,        -0.7],
    "Utilities":          [-0.5,      0.0,       0.2,    -0.1,        -0.6],
    "Crypto":             [-0.4,      0.3,       0.5,    -0.2,        -0.3],
    "Forex":              [ 0.3,     -0.3,       0.2,     0.0,         0.4],
}


async def _fetch_fred_series(series_id: str, limit: int = 2) -> Optional[List[float]]:
    """Fetch last N observations from FRED. Returns list of float values."""
    params = {
        "series_id":      series_id,
        "api_key":        FRED_API_KEY,
        "file_type":      "json",
        "sort_order":     "desc",
        "limit":          limit,
        "observation_start": "2020-01-01",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(FRED_BASE_URL, params=params)
            if r.status_code != 200:
                return None
            data = r.json()
            obs = data.get("observations", [])
            values = []
            for o in obs:
                try:
                    val = float(o["value"])
                    values.append(val)
                except (ValueError, KeyError):
                    pass
            return values if values else None
    except Exception as e:
        log.warning(f"FRED fetch failed for {series_id}: {e}")
        return None


async def _fetch_etf_momentum(etf: str) -> Optional[float]:
    """Fallback: fetch 5-day ETF momentum from Yahoo. Returns -1.0 to 1.0."""
    url    = YAHOO_CHART.format(symbol=etf)
    params = {"interval": "1d", "range": "10d"}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(url, params=params, headers=YAHOO_HEADERS)
            if r.status_code != 200:
                return None
            data   = r.json()
            result = data.get("chart", {}).get("result", [])
            if not result:
                return None
            closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
            closes = [c for c in closes if c is not None]
            if len(closes) < 5:
                return None
            momentum_pct = (closes[-1] - closes[-5]) / closes[-5] * 100
            return round(max(-1.0, min(1.0, momentum_pct / 5.0)), 3)
    except Exception as e:
        log.warning(f"ETF fetch failed for {etf}: {e}")
        return None


class MacroBot(ResearchBot):
    """
    Macro environment analysis using FRED data + sector ETF momentum.
    FRED provides real economic indicators; ETFs provide market reaction.
    """

    @property
    def name(self) -> str:
        return "MacroBot"

    @property
    def cache_ttl(self) -> int:
        return CACHE_TTL

    async def _fetch(self, ticker: str, asset_meta: dict) -> BotResult:
        import asyncio
        sector = asset_meta.get("sector", "")

        # ── Fetch FRED macro indicators in parallel ───────────
        fred_results = await asyncio.gather(
            _fetch_fred_series("FEDFUNDS", 2),   # Fed funds rate
            _fetch_fred_series("CPIAUCSL", 2),   # CPI inflation
            _fetch_fred_series("GDP", 2),         # GDP
            _fetch_fred_series("UNRATE", 2),      # Unemployment
            _fetch_fred_series("DGS10", 2),       # 10yr treasury yield
            return_exceptions=True,
        )

        fed_rate, cpi, gdp, unemployment, yield_10y = [
            r if not isinstance(r, Exception) else None
            for r in fred_results
        ]

        # ── Fetch sector ETF momentum from Yahoo ──────────────
        etf = SECTOR_ETF_MAP.get(sector)
        etf_momentum  = await _fetch_etf_momentum(etf)  if etf  else None
        spy_momentum  = await _fetch_etf_momentum("SPY")

        # ── Interpret FRED data ───────────────────────────────
        macro_signals  = {}
        bull_factors   = []
        bear_factors   = []
        fred_available = False

        # Interest rate environment: rising = tight, falling = easy
        rate_env = 0.0
        if fed_rate and len(fed_rate) >= 2:
            fred_available = True
            rate_change = fed_rate[0] - fed_rate[1]   # latest vs prior
            rate_env    = rate_change / 0.5            # normalise: 0.5% move = 1.0
            rate_env    = max(-1.0, min(1.0, rate_env))
            macro_signals["rate_env"] = rate_env
            if rate_env > 0.1:
                bear_factors.append(f"Fed funds rate rising ({fed_rate[1]:.2f}% → {fed_rate[0]:.2f}%) — tightening environment")
            elif rate_env < -0.1:
                bull_factors.append(f"Fed funds rate falling ({fed_rate[1]:.2f}% → {fed_rate[0]:.2f}%) — easing environment")
            else:
                bull_factors.append(f"Fed funds rate stable at {fed_rate[0]:.2f}% — neutral monetary policy")

        # Inflation: rising = bad for consumers/bonds, good for commodities
        inflation_signal = 0.0
        if cpi and len(cpi) >= 2:
            fred_available = True
            cpi_change = (cpi[0] - cpi[1]) / cpi[1] * 100
            inflation_signal = cpi_change / 0.3
            inflation_signal = max(-1.0, min(1.0, inflation_signal))
            macro_signals["inflation"] = inflation_signal
            if cpi_change > 0.2:
                bear_factors.append(f"CPI inflation rising ({cpi_change:+.2f}% MoM) — eroding purchasing power")
            elif cpi_change < -0.1:
                bull_factors.append(f"CPI inflation easing ({cpi_change:+.2f}% MoM) — price pressure reducing")

        # GDP growth: rising = bullish broadly
        growth_signal = 0.0
        if gdp and len(gdp) >= 2:
            fred_available = True
            gdp_change = (gdp[0] - gdp[1]) / gdp[1] * 100
            growth_signal = gdp_change / 1.0
            growth_signal = max(-1.0, min(1.0, growth_signal))
            macro_signals["growth"] = growth_signal
            if gdp_change > 0.5:
                bull_factors.append(f"GDP growth positive ({gdp_change:+.1f}%) — expanding economy")
            elif gdp_change < -0.5:
                bear_factors.append(f"GDP contracting ({gdp_change:+.1f}%) — recession risk")

        # Unemployment: rising = bad for consumer spending
        unemp_signal = 0.0
        if unemployment and len(unemployment) >= 2:
            fred_available = True
            unemp_change = unemployment[0] - unemployment[1]
            unemp_signal = -unemp_change / 0.3   # rising unemployment = negative signal
            unemp_signal = max(-1.0, min(1.0, unemp_signal))
            macro_signals["unemployment"] = unemp_signal
            if unemp_change > 0.2:
                bear_factors.append(f"Unemployment rising ({unemployment[1]:.1f}% → {unemployment[0]:.1f}%) — labour market weakening")
            elif unemp_change < -0.2:
                bull_factors.append(f"Unemployment falling ({unemployment[1]:.1f}% → {unemployment[0]:.1f}%) — strong labour market")

        # 10yr yield: rising = risk-off pressure on growth stocks
        yield_signal = 0.0
        if yield_10y and len(yield_10y) >= 2:
            fred_available = True
            yield_change = yield_10y[0] - yield_10y[1]
            yield_signal = yield_change / 0.25
            yield_signal = max(-1.0, min(1.0, yield_signal))
            macro_signals["yields"] = yield_signal
            if yield_change > 0.1:
                bear_factors.append(f"10yr Treasury yield rising ({yield_10y[0]:.2f}%) — discount rate headwind")
            elif yield_change < -0.1:
                bull_factors.append(f"10yr Treasury yield falling ({yield_10y[0]:.2f}%) — risk appetite improving")

        # ── Calculate sector-specific macro impact ────────────
        sector_sensitivity = SECTOR_MACRO_SENSITIVITY.get(sector)
        macro_score = 0.0

        if sector_sensitivity and macro_signals:
            signal_values = [
                macro_signals.get("rate_env",      0.0),
                macro_signals.get("inflation",     0.0),
                macro_signals.get("growth",        0.0),
                macro_signals.get("unemployment",  0.0),
                macro_signals.get("yields",        0.0),
            ]
            weighted = sum(s * w for s, w in zip(signal_values, sector_sensitivity))
            total_w  = sum(abs(w) for w in sector_sensitivity)
            macro_score = weighted / total_w if total_w else 0.0
            macro_score = max(-1.0, min(1.0, macro_score))

        # ── Blend FRED macro score with ETF momentum ─────────
        if etf_momentum is not None and spy_momentum is not None:
            relative_etf = max(-1.0, min(1.0, etf_momentum - spy_momentum * 0.5))
        elif etf_momentum is not None:
            relative_etf = etf_momentum
        else:
            relative_etf = 0.0

        # Weight: 60% FRED macro score + 40% ETF momentum if FRED available
        # else 100% ETF
        if fred_available and macro_score != 0.0:
            sector_flow = round(macro_score * 0.6 + relative_etf * 0.4, 3)
            source_str  = f"FRED + Yahoo ({etf or 'ETF'})"
            confidence  = 0.85
        else:
            sector_flow = round(relative_etf, 3)
            source_str  = f"Yahoo Finance ({etf or 'ETF'} vs SPY)"
            confidence  = 0.65

        sector_flow = max(-1.0, min(1.0, sector_flow))

        # Add ETF context to factors
        if etf and etf_momentum is not None:
            etf_pct = etf_momentum * 5
            if etf_momentum > 0.2:
                bull_factors.append(f"{etf} sector ETF +{etf_pct:.1f}% 5-day — capital flowing in")
            elif etf_momentum < -0.2:
                bear_factors.append(f"{etf} sector ETF {etf_pct:.1f}% 5-day — capital flowing out")

        if not bull_factors:
            bull_factors.append(f"Macro environment neutral for {sector or 'this sector'}")
        if not bear_factors:
            bear_factors.append(f"No strong macro headwinds detected currently")

        # ── Summary ───────────────────────────────────────────
        data_source = "FRED + ETF data" if fred_available else "ETF momentum data"
        if sector_flow > 0.2:
            summary = f"Macro tailwind for {sector} — {data_source}"
        elif sector_flow < -0.2:
            summary = f"Macro headwind for {sector} — {data_source}"
        else:
            summary = f"Macro environment neutral for {sector} — {data_source}"

        return BotResult(
            bot_name=self.name,
            ticker=ticker,
            signal_inputs={"sectorFlow": sector_flow},
            bull_factors=bull_factors[:3],
            bear_factors=bear_factors[:3],
            summary=summary,
            confidence=confidence,
            source=source_str,
            raw={
                "sector":        sector,
                "etf":           etf,
                "macro_score":   round(macro_score, 3),
                "etf_momentum":  etf_momentum,
                "spy_momentum":  spy_momentum,
                "sector_flow":   sector_flow,
                "fred_available":fred_available,
                "macro_signals": macro_signals,
            },
        )
