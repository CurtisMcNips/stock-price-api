"""
Market Brain — Opportunity Engine
───────────────────────────────────
Identifies the best specific tickers and opportunities within
sectors flagged by active catalysts.

This module:
  1. Maps active catalyst sectors to investable tickers
  2. Fetches live prices from Yahoo Finance for all candidates
  3. Scores each ticker by: catalyst alignment, wave strength,
     relative momentum, sector exposure
  4. Returns ranked, actionable opportunity lists for Sebastian

Sebastian uses this to give real price-anchored recommendations
instead of generic sector commentary.
"""

import asyncio
import logging
import time
from typing import Optional

import httpx

log = logging.getLogger("mb.opportunity")

# ── Master ticker universe ────────────────────────────────────────
# Organised by sector → sub-sector → tickers with metadata
# This is what the system can quote prices and make recommendations on

TICKER_UNIVERSE = {
    "Shipping": {
        "Container": [
            {"ticker": "ZIM",        "name": "ZIM Integrated Shipping",    "exchange": "NYSE"},
            {"ticker": "MAERSK-B.CO","name": "Maersk",                     "exchange": "CPH"},
            {"ticker": "DAC",        "name": "Danaos Corp",                "exchange": "NYSE"},
            {"ticker": "CMRE",       "name": "Costamare",                  "exchange": "NYSE"},
        ],
        "Oil Tanker": [
            {"ticker": "FRO",        "name": "Frontline",                  "exchange": "NYSE"},
            {"ticker": "STNG",       "name": "Scorpio Tankers",            "exchange": "NYSE"},
            {"ticker": "DHT",        "name": "DHT Holdings",               "exchange": "NYSE"},
            {"ticker": "INSW",       "name": "International Seaways",      "exchange": "NYSE"},
            {"ticker": "TK",         "name": "Teekay Corp",                "exchange": "NYSE"},
            {"ticker": "TNK",        "name": "Teekay Tankers",             "exchange": "NYSE"},
        ],
        "LNG Shipping": [
            {"ticker": "FLNG",       "name": "Flex LNG",                   "exchange": "NYSE"},
            {"ticker": "GLNG",       "name": "Golar LNG",                  "exchange": "NASDAQ"},
            {"ticker": "CLCO",       "name": "Cool Company",               "exchange": "NYSE"},
        ],
        "Dry Bulk": [
            {"ticker": "SBLK",       "name": "Star Bulk Carriers",         "exchange": "NASDAQ"},
            {"ticker": "GOGL",       "name": "Golden Ocean",               "exchange": "NASDAQ"},
            {"ticker": "NMM",        "name": "Navios Maritime Partners",   "exchange": "NYSE"},
        ],
    },
    "Defence": {
        "US Defence": [
            {"ticker": "LMT",        "name": "Lockheed Martin",            "exchange": "NYSE"},
            {"ticker": "RTX",        "name": "RTX (Raytheon)",             "exchange": "NYSE"},
            {"ticker": "NOC",        "name": "Northrop Grumman",           "exchange": "NYSE"},
            {"ticker": "GD",         "name": "General Dynamics",           "exchange": "NYSE"},
            {"ticker": "HII",        "name": "Huntington Ingalls (Naval)", "exchange": "NYSE"},
            {"ticker": "LHX",        "name": "L3Harris Technologies",      "exchange": "NYSE"},
            {"ticker": "KTOS",       "name": "Kratos Defense",             "exchange": "NASDAQ"},
            {"ticker": "CACI",       "name": "CACI International",         "exchange": "NYSE"},
        ],
        "UK Defence": [
            {"ticker": "BAE.L",      "name": "BAE Systems",                "exchange": "LSE"},
            {"ticker": "QQ.L",       "name": "QinetiQ",                    "exchange": "LSE"},
            {"ticker": "AVON.L",     "name": "Avon Technologies",          "exchange": "LSE"},
        ],
        "European Defence": [
            {"ticker": "BAESY",      "name": "BAE Systems ADR",            "exchange": "OTC"},
            {"ticker": "RHM.DE",     "name": "Rheinmetall",                "exchange": "XETRA"},
            {"ticker": "AIR.PA",     "name": "Airbus",                     "exchange": "EPA"},
            {"ticker": "SAABY",      "name": "SAAB",                       "exchange": "OTC"},
        ],
        "Drone/Tech Defence": [
            {"ticker": "PLTR",       "name": "Palantir (AI+Defence)",      "exchange": "NYSE"},
            {"ticker": "RKLB",       "name": "Rocket Lab",                 "exchange": "NASDAQ"},
            {"ticker": "ACHR",       "name": "Archer Aviation",            "exchange": "NYSE"},
        ],
    },
    "Energy": {
        "Oil & Gas Major": [
            {"ticker": "XOM",        "name": "ExxonMobil",                 "exchange": "NYSE"},
            {"ticker": "CVX",        "name": "Chevron",                    "exchange": "NYSE"},
            {"ticker": "SHEL.L",     "name": "Shell",                      "exchange": "LSE"},
            {"ticker": "BP.L",       "name": "BP",                         "exchange": "LSE"},
            {"ticker": "COP",        "name": "ConocoPhillips",             "exchange": "NYSE"},
            {"ticker": "TTE.PA",     "name": "TotalEnergies",              "exchange": "EPA"},
        ],
        "LNG": [
            {"ticker": "LNG",        "name": "Cheniere Energy",            "exchange": "NYSE"},
            {"ticker": "CQP",        "name": "Cheniere Energy Partners",   "exchange": "NYSE"},
            {"ticker": "NEXT",       "name": "NextDecade Corp",            "exchange": "NASDAQ"},
        ],
        "Oil Services": [
            {"ticker": "SLB",        "name": "SLB (Schlumberger)",         "exchange": "NYSE"},
            {"ticker": "HAL",        "name": "Halliburton",                "exchange": "NYSE"},
            {"ticker": "BKR",        "name": "Baker Hughes",               "exchange": "NYSE"},
        ],
        "E&P": [
            {"ticker": "PXD",        "name": "Pioneer Natural Resources",  "exchange": "NYSE"},
            {"ticker": "DVN",        "name": "Devon Energy",               "exchange": "NYSE"},
            {"ticker": "FANG",       "name": "Diamondback Energy",         "exchange": "NASDAQ"},
        ],
        "Commodities ETF": [
            {"ticker": "USO",        "name": "US Oil Fund ETF",            "exchange": "NYSE"},
            {"ticker": "BNO",        "name": "Brent Oil ETF",              "exchange": "NYSE"},
            {"ticker": "XLE",        "name": "Energy Select SPDR ETF",     "exchange": "NYSE"},
        ],
    },
    "Insurance": {
        "Marine/Specialty": [
            {"ticker": "BEZ.L",      "name": "Beazley",                    "exchange": "LSE"},
            {"ticker": "MKL",        "name": "Markel Group",               "exchange": "NYSE"},
            {"ticker": "RE",         "name": "Everest Re",                 "exchange": "NYSE"},
            {"ticker": "RNR",        "name": "RenaissanceRe",              "exchange": "NYSE"},
            {"ticker": "AIG",        "name": "AIG",                        "exchange": "NYSE"},
        ],
    },
    "Logistics": {
        "Air Freight": [
            {"ticker": "FDX",        "name": "FedEx",                      "exchange": "NYSE"},
            {"ticker": "UPS",        "name": "UPS",                        "exchange": "NYSE"},
            {"ticker": "EXPD",       "name": "Expeditors International",   "exchange": "NASDAQ"},
        ],
        "Rail/Intermodal": [
            {"ticker": "CSX",        "name": "CSX Corporation",            "exchange": "NASDAQ"},
            {"ticker": "UNP",        "name": "Union Pacific",              "exchange": "NYSE"},
            {"ticker": "NSC",        "name": "Norfolk Southern",           "exchange": "NYSE"},
        ],
        "Freight Tech": [
            {"ticker": "UBER",       "name": "Uber Freight",               "exchange": "NYSE"},
            {"ticker": "CHRW",       "name": "CH Robinson",                "exchange": "NASDAQ"},
        ],
    },
    "Technology": {
        "Semiconductors": [
            {"ticker": "NVDA",       "name": "NVIDIA",                     "exchange": "NASDAQ"},
            {"ticker": "AMD",        "name": "AMD",                        "exchange": "NASDAQ"},
            {"ticker": "AVGO",       "name": "Broadcom",                   "exchange": "NASDAQ"},
            {"ticker": "ASML",       "name": "ASML",                       "exchange": "NASDAQ"},
            {"ticker": "MU",         "name": "Micron",                     "exchange": "NASDAQ"},
            {"ticker": "AMAT",       "name": "Applied Materials",          "exchange": "NASDAQ"},
            {"ticker": "LRCX",       "name": "Lam Research",               "exchange": "NASDAQ"},
            {"ticker": "TSM",        "name": "TSMC",                       "exchange": "NYSE"},
        ],
        "AI/Cloud": [
            {"ticker": "MSFT",       "name": "Microsoft",                  "exchange": "NASDAQ"},
            {"ticker": "GOOGL",      "name": "Alphabet",                   "exchange": "NASDAQ"},
            {"ticker": "AMZN",       "name": "Amazon",                     "exchange": "NASDAQ"},
            {"ticker": "META",       "name": "Meta Platforms",             "exchange": "NASDAQ"},
            {"ticker": "PLTR",       "name": "Palantir",                   "exchange": "NYSE"},
        ],
        "Cybersecurity": [
            {"ticker": "CRWD",       "name": "CrowdStrike",                "exchange": "NASDAQ"},
            {"ticker": "PANW",       "name": "Palo Alto Networks",         "exchange": "NASDAQ"},
            {"ticker": "S",          "name": "SentinelOne",                "exchange": "NYSE"},
            {"ticker": "FTNT",       "name": "Fortinet",                   "exchange": "NASDAQ"},
        ],
    },
    "Commodities": {
        "Precious Metals": [
            {"ticker": "GLD",        "name": "Gold ETF (SPDR)",            "exchange": "NYSE"},
            {"ticker": "GC=F",       "name": "Gold Futures",               "exchange": "CME"},
            {"ticker": "SLV",        "name": "Silver ETF (iShares)",       "exchange": "NYSE"},
            {"ticker": "SI=F",       "name": "Silver Futures",             "exchange": "CME"},
            {"ticker": "NEM",        "name": "Newmont Mining",             "exchange": "NYSE"},
            {"ticker": "GOLD",       "name": "Barrick Gold",               "exchange": "NYSE"},
        ],
        "Base Metals": [
            {"ticker": "FCX",        "name": "Freeport-McMoRan (Copper)",  "exchange": "NYSE"},
            {"ticker": "HG=F",       "name": "Copper Futures",             "exchange": "CME"},
            {"ticker": "VALE",       "name": "Vale SA (Iron/Nickel)",      "exchange": "NYSE"},
        ],
        "Energy": [
            {"ticker": "CL=F",       "name": "WTI Crude Futures",          "exchange": "CME"},
            {"ticker": "BZ=F",       "name": "Brent Crude Futures",        "exchange": "ICE"},
            {"ticker": "NG=F",       "name": "Natural Gas Futures",        "exchange": "CME"},
        ],
    },
    "Finance": {
        "US Banks": [
            {"ticker": "JPM",        "name": "JPMorgan Chase",             "exchange": "NYSE"},
            {"ticker": "GS",         "name": "Goldman Sachs",              "exchange": "NYSE"},
            {"ticker": "MS",         "name": "Morgan Stanley",             "exchange": "NYSE"},
            {"ticker": "BAC",        "name": "Bank of America",            "exchange": "NYSE"},
        ],
        "UK Banks": [
            {"ticker": "HSBA.L",     "name": "HSBC",                       "exchange": "LSE"},
            {"ticker": "BARC.L",     "name": "Barclays",                   "exchange": "LSE"},
            {"ticker": "LLOY.L",     "name": "Lloyds Banking Group",       "exchange": "LSE"},
            {"ticker": "NWG.L",      "name": "NatWest Group",              "exchange": "LSE"},
        ],
    },
}

# ── Live price fetcher ────────────────────────────────────────────
_price_cache: dict = {}
PRICE_TTL = 300  # 5 minutes

async def fetch_price(ticker: str) -> Optional[dict]:
    """Fetch live price from Yahoo Finance. Returns price dict or None."""
    now = time.time()
    if ticker in _price_cache:
        cached_at, data = _price_cache[ticker]
        if now - cached_at < PRICE_TTL:
            return data
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        async with httpx.AsyncClient(
            timeout=6,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MarketBrain/1.0)"},
        ) as client:
            r = await client.get(url, params={"interval": "1m", "range": "1d"})
            if r.status_code != 200:
                return None
            meta = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
            price      = meta.get("regularMarketPrice")
            prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
            currency   = meta.get("currency", "USD")
            market_cap = meta.get("marketCap")
            if not price:
                return None
            change_pct = round(((price - prev_close) / prev_close) * 100, 2) if prev_close else 0.0
            result = {
                "ticker":     ticker,
                "price":      round(float(price), 4),
                "prev_close": round(float(prev_close), 4) if prev_close else None,
                "change_pct": change_pct,
                "currency":   currency,
                "market_cap": market_cap,
            }
            _price_cache[ticker] = (now, result)
            return result
    except Exception:
        return None


async def fetch_prices_batch(tickers: list) -> dict:
    """Fetch prices for multiple tickers concurrently. Returns {ticker: price_dict}."""
    tasks   = {t: asyncio.create_task(fetch_price(t)) for t in tickers}
    results = {}
    for ticker, task in tasks.items():
        try:
            results[ticker] = await task
        except Exception:
            results[ticker] = None
    return results


async def get_opportunities_for_catalyst(catalyst: dict) -> dict:
    """
    Given a catalyst object, identify the best specific tickers to watch.
    Returns ranked opportunity list with live prices.

    Called by Sebastian when answering opportunity questions.
    """
    sectors    = catalyst.get("sectors", [])
    direction  = catalyst.get("direction", "neutral")
    wave       = catalyst.get("wave", "spark")
    confidence = catalyst.get("confidence", 0.0)

    wave_multiplier = {
        "spark": 0.6, "confirmed": 0.75, "escalation": 0.88,
        "structural": 0.95, "regime": 1.0,
    }.get(wave, 0.6)

    candidates = []

    for sector in sectors:
        # Skip geo scan tickers
        if sector in ("Geopolitical",):
            continue
        sector_data = TICKER_UNIVERSE.get(sector, {})
        for sub_sector, tickers in sector_data.items():
            for t in tickers:
                candidates.append({
                    **t,
                    "sector":     sector,
                    "sub_sector": sub_sector,
                    "direction":  direction,
                })

    if not candidates:
        return {"opportunities": [], "message": f"No mapped tickers for sectors: {sectors}"}

    # Deduplicate
    seen = set()
    unique = []
    for c in candidates:
        if c["ticker"] not in seen:
            seen.add(c["ticker"])
            unique.append(c)

    # Fetch live prices (limit to 15 to stay within rate limits)
    tickers_to_price = [c["ticker"] for c in unique[:15]]
    prices = await fetch_prices_batch(tickers_to_price)

    # Score and rank
    scored = []
    for c in unique[:15]:
        price_data = prices.get(c["ticker"])
        if not price_data:
            continue

        # Score: confidence * wave * momentum alignment
        momentum = price_data.get("change_pct", 0.0)
        momentum_score = 0.0
        if direction == "bullish" and momentum > 0:
            momentum_score = min(1.0, momentum / 3.0)
        elif direction == "bearish" and momentum < 0:
            momentum_score = min(1.0, abs(momentum) / 3.0)

        score = (confidence / 100.0) * wave_multiplier * (0.7 + 0.3 * momentum_score)

        scored.append({
            "ticker":      c["ticker"],
            "name":        c["name"],
            "sector":      c["sector"],
            "sub_sector":  c["sub_sector"],
            "exchange":    c.get("exchange", ""),
            "price":       price_data["price"],
            "currency":    price_data["currency"],
            "change_pct":  price_data["change_pct"],
            "direction":   direction,
            "score":       round(score, 3),
            "catalyst_wave": wave,
        })

    # Sort by score descending
    scored.sort(key=lambda x: x["score"], reverse=True)

    return {
        "opportunities": scored[:10],
        "sectors":       sectors,
        "catalyst_wave": wave,
        "direction":     direction,
        "confidence":    confidence,
        "total_scanned": len(unique),
    }


async def get_sector_opportunities(sector: str, direction: str = "bullish") -> dict:
    """Get all opportunities for a specific sector with live prices."""
    sector_data = TICKER_UNIVERSE.get(sector, {})
    if not sector_data:
        # Try fuzzy match
        for key in TICKER_UNIVERSE:
            if sector.lower() in key.lower() or key.lower() in sector.lower():
                sector_data = TICKER_UNIVERSE[key]
                sector = key
                break

    if not sector_data:
        return {"opportunities": [], "message": f"No tickers mapped for sector: {sector}"}

    all_tickers = []
    for sub, tickers in sector_data.items():
        for t in tickers:
            all_tickers.append({**t, "sector": sector, "sub_sector": sub})

    tickers_to_price = [t["ticker"] for t in all_tickers[:20]]
    prices = await fetch_prices_batch(tickers_to_price)

    result = []
    for t in all_tickers[:20]:
        p = prices.get(t["ticker"])
        if p:
            result.append({
                "ticker":     t["ticker"],
                "name":       t["name"],
                "sub_sector": t["sub_sector"],
                "exchange":   t.get("exchange", ""),
                "price":      p["price"],
                "currency":   p["currency"],
                "change_pct": p["change_pct"],
            })

    result.sort(key=lambda x: abs(x.get("change_pct", 0)), reverse=True)
    return {"opportunities": result, "sector": sector, "direction": direction}


async def get_live_price_context(tickers: list) -> str:
    """
    Build a formatted live price string for injection into Sebastian's context.
    Fetches prices for all requested tickers + all active catalyst assets.
    """
    prices = await fetch_prices_batch(tickers[:25])
    lines  = []
    for ticker, data in prices.items():
        if data:
            change = f"{data['change_pct']:+.2f}%" if data.get("change_pct") is not None else "N/A"
            lines.append(f"  {ticker}: {data['currency']} {data['price']} ({change} today)")
    return "\n".join(lines) if lines else "  Live prices temporarily unavailable."


def get_all_tracked_tickers() -> list:
    """Return flat list of all tickers in the universe for sweep coverage."""
    tickers = []
    for sector_data in TICKER_UNIVERSE.values():
        for sub_tickers in sector_data.values():
            for t in sub_tickers:
                tickers.append(t["ticker"])
    return list(set(tickers))
