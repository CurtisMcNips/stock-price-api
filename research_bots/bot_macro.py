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
  FEDFUNDS   — Federal funds rate
  CPIAUCSL   — CPI inflation (monthly)
  GDPC1      — Real GDP (quarterly) — uses YoY comparison, not MoM
  UNRATE     — Unemployment rate
  DGS10      — 10-year treasury yield
  INDPRO     — Industrial production index (monthly proxy for growth)

FIX: GDP was using the "GDP" series which is nominal and quarterly.
  With only 1-2 recent obs and a quarterly cadence, len(gdp) < 2 often.
  Now uses GDPC1 with a 5-observation window and YoY comparison.
  Also uses INDPRO as a higher-frequency growth proxy.
  Single-value series now handled gracefully (uses absolute level, not change).
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
    "Shipping":      "BDRY",
    "Defence":       "ITA",
    "Insurance":     "IAK",
    "Logistics":     "SHIP",
}

SECTOR_MACRO_SENSITIVITY = {
    #                      rate_env  inflation  growth  unemployment  yields
    "Technology":         [-0.6,     -0.2,       0.7,    -0.3,        -0.5],
    "Finance":            [ 0.7,      0.2,       0.5,    -0.2,         0.6],
    "Healthcare":         [-0.1,     -0.1,       0.3,    -0.1,        -0.1],
    "Energy":             [-0.2,      0.7,       0.4,    -0.1,         0.1],
    "Consumer":           [-0.4,     -0.6,       0.6,    -0.5,        -0.3],
    "Industrials":        [-0.3,      0.1,       0.8,    -0.4,        -0.2],
    "Metals":             [-0.3,      0.6,       0.5,    -0.2,         0.0],
    "Minerals":           [-0.3,      0.6,       0.5,    -0.2,         0.0],
    "Real Estate":        [-0.8,     -0.2,       0.3,    -0.3,        -0.7],
    "Utilities":          [-0.5,      0.0,       0.2,    -0.1,        -0.6],
    "Crypto":             [-0.4,      0.3,       0.5,    -0.2,        -0.3],
    "Forex":              [ 0.3,     -0.3,       0.2,     0.0,         0.4],
    "Shipping":           [-0.1,      0.3,       0.7,    -0.2,         0.0],
    "Defence":            [ 0.1,      0.2,       0.3,     0.0,         0.1],
    "Insurance":          [ 0.4,      0.3,       0.3,    -0.1,         0.3],
    "Logistics":          [-0.2,      0.2,       0.7,    -0.3,        -0.1],
}


async def _fetch_fred_series(series_id: str, limit: int = 5) -> Optional[List[float]]:
    """
    Fetch last N observations from FRED.
    Returns list of float values (most recent first) or None on failure.
    Uses limit=5 by default so we always have enough for YoY/MoM comparisons.
    """
    params = {
        "series_id":         series_id,
        "api_key":           FRED_API_KEY,
        "file_type":         "json",
        "sort_order":        "desc",
        "limit":             limit,
        "observation_start": "2022-01-01",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(FRED_BASE_URL, params=params)
            if r.status_code != 200:
                log.debug(f"FRED {series_id} returned {r.status_code}")
                return None
            data = r.json()
            obs = data.get("observations", [])
            values = []
            for o in obs:
                try:
                    val = float(o["value"])
                    values.append(val)
                except (ValueError, KeyError):
                    pass   # skip "." placeholder values
            return values if values else None
    except Exception as e:
        log.warning(f"FRED fetch failed for {series_id}: {e}")
        return None


def _safe_change(values: List[float], periods: int = 1) -> Optional[float]:
    """
    Calculate change between most recent and N periods ago.
    Returns None if not enough data points.
    Never crashes on single-value series.
    """
    if not values or len(values) <= periods:
        return None
    return values[0] - values[periods]


def _safe_pct_change(values: List[float], periods: int = 1) -> Optional[float]:
    """Percentage change. Safe for single-value series."""
    if not values or len(values) <= periods or values[periods] == 0:
        return None
    return (values[0] - values[periods]) / abs(values[periods]) * 100


async def _fetch_etf_momentum(etf: str) -> Optional[float]:
    """Fetch 5-day ETF momentum from Yahoo. Returns -1.0 to 1.0."""
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
    Handles single-value FRED series gracefully using absolute level context.
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
        # GDP: use GDPC1 (real GDP, quarterly) with 5 obs for YoY comparison
        # INDPRO: monthly industrial production — higher frequency growth proxy
        fred_results = await asyncio.gather(
            _fetch_fred_series("FEDFUNDS", 3),    # Fed funds rate (monthly)
            _fetch_fred_series("CPIAUCSL", 3),    # CPI (monthly)
            _fetch_fred_series("GDPC1",    5),    # Real GDP (quarterly) — need 5 for YoY
            _fetch_fred_series("UNRATE",   3),    # Unemployment (monthly)
            _fetch_fred_series("DGS10",    3),    # 10yr yield (daily, but we pull recent)
            _fetch_fred_series("INDPRO",   3),    # Industrial production (monthly)
            return_exceptions=True,
        )

        fed_rate, cpi, gdp, unemployment, yield_10y, indpro = [
            r if not isinstance(r, Exception) else None
            for r in fred_results
        ]

        # ── Fetch sector ETF momentum ─────────────────────────
        etf = SECTOR_ETF_MAP.get(sector)
        etf_momentum = await _fetch_etf_momentum(etf)  if etf  else None
        spy_momentum = await _fetch_etf_momentum("SPY")

        # ── Interpret FRED data ───────────────────────────────
        macro_signals  = {}
        bull_factors   = []
        bear_factors   = []
        fred_available = False

        # ── Interest rate environment ─────────────────────────
        rate_env = 0.0
        if fed_rate:
            fred_available = True
            rate_change = _safe_change(fed_rate, 1)
            if rate_change is not None:
                rate_env = max(-1.0, min(1.0, rate_change / 0.5))
                if rate_env > 0.1:
                    bear_factors.append(
                        f"Fed funds rising ({fed_rate[1]:.2f}% → {fed_rate[0]:.2f}%) — tightening"
                    )
                elif rate_env < -0.1:
                    bull_factors.append(
                        f"Fed funds falling ({fed_rate[1]:.2f}% → {fed_rate[0]:.2f}%) — easing"
                    )
                else:
                    bull_factors.append(f"Fed funds stable at {fed_rate[0]:.2f}% — neutral policy")
            else:
                # Single value — use absolute level as context
                rate_level = fed_rate[0]
                if rate_level > 4.5:
                    bear_factors.append(f"Fed funds rate elevated at {rate_level:.2f}% — restrictive environment")
                    rate_env = -0.4
                elif rate_level < 1.0:
                    bull_factors.append(f"Fed funds rate low at {rate_level:.2f}% — accommodative environment")
                    rate_env = 0.4
                else:
                    bull_factors.append(f"Fed funds rate at {rate_level:.2f}%")
            macro_signals["rate_env"] = rate_env

        # ── CPI inflation ─────────────────────────────────────
        inflation_signal = 0.0
        if cpi:
            fred_available = True
            cpi_change = _safe_pct_change(cpi, 1)
            if cpi_change is not None:
                inflation_signal = max(-1.0, min(1.0, cpi_change / 0.3))
                if cpi_change > 0.2:
                    bear_factors.append(f"CPI inflation rising ({cpi_change:+.2f}% MoM) — price pressure building")
                elif cpi_change < -0.1:
                    bull_factors.append(f"CPI inflation easing ({cpi_change:+.2f}% MoM) — price pressure reducing")
                else:
                    bull_factors.append(f"CPI inflation stable — no near-term price pressure signal")
            else:
                # Single value — use absolute level
                cpi_level = cpi[0]
                bull_factors.append(f"CPI index at {cpi_level:.1f} — single data point, monitoring")
            macro_signals["inflation"] = inflation_signal

        # ── GDP growth — YoY comparison (quarterly data) ──────
        growth_signal = 0.0
        if gdp:
            fred_available = True
            # YoY: compare latest vs 4 quarters ago (index 0 vs index 4)
            # MoM fallback: compare vs 1 quarter ago (index 0 vs index 1)
            gdp_yoy = _safe_pct_change(gdp, 4)  # YoY (need 5 observations)
            gdp_qoq = _safe_pct_change(gdp, 1)  # QoQ fallback

            if gdp_yoy is not None:
                growth_signal = max(-1.0, min(1.0, gdp_yoy / 3.0))
                if gdp_yoy > 2.0:
                    bull_factors.append(f"Real GDP growth +{gdp_yoy:.1f}% YoY — solid expansion")
                elif gdp_yoy > 0:
                    bull_factors.append(f"Real GDP growth +{gdp_yoy:.1f}% YoY — modest expansion")
                elif gdp_yoy < -1.0:
                    bear_factors.append(f"Real GDP contracting {gdp_yoy:.1f}% YoY — recession territory")
                else:
                    bear_factors.append(f"Real GDP growth slowing ({gdp_yoy:.1f}% YoY)")
            elif gdp_qoq is not None:
                growth_signal = max(-1.0, min(1.0, gdp_qoq / 1.0))
                if gdp_qoq > 0.5:
                    bull_factors.append(f"Real GDP +{gdp_qoq:.1f}% QoQ — positive growth quarter")
                elif gdp_qoq < -0.5:
                    bear_factors.append(f"Real GDP {gdp_qoq:.1f}% QoQ — contraction signal")
                else:
                    bull_factors.append(f"Real GDP quarterly change: {gdp_qoq:+.1f}%")
            else:
                # Single observation — use absolute level change from prior
                bull_factors.append(f"GDP data available (single observation — trend monitoring)")
            macro_signals["growth"] = growth_signal

        # ── Industrial production (higher-frequency growth proxy) ─
        if indpro and growth_signal == 0.0:
            indpro_change = _safe_pct_change(indpro, 1)
            if indpro_change is not None:
                growth_signal = max(-1.0, min(1.0, indpro_change / 1.0))
                if indpro_change > 0.3:
                    bull_factors.append(f"Industrial production rising {indpro_change:+.2f}% MoM — activity expanding")
                elif indpro_change < -0.3:
                    bear_factors.append(f"Industrial production falling {indpro_change:+.2f}% MoM — activity contracting")
                macro_signals["growth"] = growth_signal

        # ── Unemployment ──────────────────────────────────────
        unemp_signal = 0.0
        if unemployment:
            fred_available = True
            unemp_change = _safe_change(unemployment, 1)
            if unemp_change is not None:
                unemp_signal = max(-1.0, min(1.0, -unemp_change / 0.3))
                if unemp_change > 0.2:
                    bear_factors.append(
                        f"Unemployment rising ({unemployment[1]:.1f}% → {unemployment[0]:.1f}%) — labour market softening"
                    )
                elif unemp_change < -0.2:
                    bull_factors.append(
                        f"Unemployment falling ({unemployment[1]:.1f}% → {unemployment[0]:.1f}%) — strong labour market"
                    )
                else:
                    bull_factors.append(f"Unemployment stable at {unemployment[0]:.1f}%")
            else:
                # Single value — use absolute level
                u = unemployment[0]
                if u > 5.5:
                    bear_factors.append(f"Unemployment elevated at {u:.1f}%")
                    unemp_signal = -0.3
                elif u < 4.0:
                    bull_factors.append(f"Unemployment low at {u:.1f}% — tight labour market")
                    unemp_signal = 0.3
                else:
                    bull_factors.append(f"Unemployment at {u:.1f}% — neutral labour market")
            macro_signals["unemployment"] = unemp_signal

        # ── 10yr Treasury yield ───────────────────────────────
        yield_signal = 0.0
        if yield_10y:
            fred_available = True
            yield_change = _safe_change(yield_10y, 1)
            if yield_change is not None:
                yield_signal = max(-1.0, min(1.0, yield_change / 0.25))
                if yield_change > 0.1:
                    bear_factors.append(f"10yr yield rising ({yield_10y[0]:.2f}%) — discount rate headwind")
                elif yield_change < -0.1:
                    bull_factors.append(f"10yr yield falling ({yield_10y[0]:.2f}%) — risk appetite improving")
                else:
                    bull_factors.append(f"10yr yield stable at {yield_10y[0]:.2f}%")
            else:
                y = yield_10y[0]
                if y > 4.5:
                    bear_factors.append(f"10yr yield elevated at {y:.2f}% — tight financial conditions")
                    yield_signal = -0.4
                elif y < 2.0:
                    bull_factors.append(f"10yr yield low at {y:.2f}% — loose financial conditions")
                    yield_signal = 0.3
                else:
                    bull_factors.append(f"10yr yield at {y:.2f}%")
            macro_signals["yields"] = yield_signal

        # ── Calculate sector-specific macro impact ────────────
        sector_sensitivity = SECTOR_MACRO_SENSITIVITY.get(sector)
        macro_score = 0.0

        if sector_sensitivity and macro_signals:
            signal_values = [
                macro_signals.get("rate_env",     0.0),
                macro_signals.get("inflation",    0.0),
                macro_signals.get("growth",       0.0),
                macro_signals.get("unemployment", 0.0),
                macro_signals.get("yields",       0.0),
            ]
            weighted = sum(s * w for s, w in zip(signal_values, sector_sensitivity))
            total_w  = sum(abs(w) for w in sector_sensitivity)
            macro_score = weighted / total_w if total_w else 0.0
            macro_score = max(-1.0, min(1.0, macro_score))

        # ── Blend FRED score with ETF momentum ────────────────
        if etf_momentum is not None and spy_momentum is not None:
            relative_etf = max(-1.0, min(1.0, etf_momentum - spy_momentum * 0.5))
        elif etf_momentum is not None:
            relative_etf = etf_momentum
        else:
            relative_etf = 0.0

        if fred_available and macro_score != 0.0:
            sector_flow = round(macro_score * 0.6 + relative_etf * 0.4, 3)
            source_str  = f"FRED + Yahoo ({etf or 'ETF'})"
            confidence  = 0.85
        elif fred_available:
            # FRED loaded but score was flat — still count as available
            sector_flow = round(relative_etf * 0.7, 3)
            source_str  = f"FRED (flat) + Yahoo ({etf or 'ETF'})"
            confidence  = 0.70
        else:
            sector_flow = round(relative_etf, 3)
            source_str  = f"Yahoo Finance ({etf or 'ETF'} vs SPY)"
            confidence  = 0.60

        sector_flow = max(-1.0, min(1.0, sector_flow))

        if etf and etf_momentum is not None:
            etf_pct = etf_momentum * 5
            if etf_momentum > 0.2:
                bull_factors.append(f"{etf} sector ETF +{etf_pct:.1f}% 5-day — capital rotating in")
            elif etf_momentum < -0.2:
                bear_factors.append(f"{etf} sector ETF {etf_pct:.1f}% 5-day — capital rotating out")

        # Ensure we always have at least one factor in each list
        if not bull_factors:
            bull_factors.append(f"Macro environment broadly neutral for {sector or 'this sector'}")
        if not bear_factors:
            bear_factors.append(f"No dominant macro headwind identified for {sector or 'this sector'}")

        # ── Summary ───────────────────────────────────────────
        data_source = "FRED + ETF data" if fred_available else "ETF momentum"
        if sector_flow > 0.2:
            summary = f"Macro tailwind for {sector} (score {sector_flow:+.2f}) — {data_source}"
        elif sector_flow < -0.2:
            summary = f"Macro headwind for {sector} (score {sector_flow:+.2f}) — {data_source}"
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
                "sector":         sector,
                "etf":            etf,
                "macro_score":    round(macro_score, 3),
                "etf_momentum":   etf_momentum,
                "spy_momentum":   spy_momentum,
                "sector_flow":    sector_flow,
                "fred_available": fred_available,
                "macro_signals":  macro_signals,
            },
        )
