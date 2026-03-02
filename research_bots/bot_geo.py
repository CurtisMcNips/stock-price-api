"""
Market Brain — 🌍 Geo Intelligence Bot
────────────────────────────────────────
Monitors geopolitical events, conflicts, sanctions, and macro shocks.
Maps each event type to affected sectors and assets.
Produces high-weight signals that feed directly into the CoS chain.

This bot does NOT track a ticker — it scans the world for events
and generates sector/asset-level impact signals.

Event categories tracked:
  - Armed conflict / war escalation
  - Sanctions and trade restrictions
  - Shipping route disruption (Suez, Hormuz, Red Sea, Panama)
  - Energy supply shock (OPEC, pipeline, LNG terminal)
  - Geopolitical elections / regime change
  - Port closures / blockades
  - Cyber attacks on infrastructure
  - Commodity supply chain disruption

Impact mapping:
  War / conflict       → Defence ↑, Energy ↑, Shipping ↑, Markets ↓
  Suez / Red Sea       → Shipping ↑, Insurance ↑, Oil ↑, Logistics ↑
  Sanctions            → Energy ↑, Commodities ↑, affected stocks ↓
  Energy shock         → Oil ↑, Gas ↑, Renewables ↑, Consumer ↓
  Regime change        → Forex volatile, regional stocks ↓

Cache: 1 hour — geopolitical situations evolve fast
"""

import asyncio
import logging
import os
import time
from typing import Optional, List, Dict, Tuple

import httpx

from base import ResearchBot, BotResult

log = logging.getLogger("mb.bots.geo")

GNEWS_API_KEY = os.environ.get("GNEWS_API_KEY", "c7d8195679eab38431bbd674bb74fd96")
GNEWS_API_URL = "https://gnews.io/api/v4/search"
CACHE_TTL     = 3600   # 1 hour — faster than other bots, geo moves quickly

PERPLEXITY_KEY = os.environ.get("PERPLEXITY_API_KEY", "")
PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"

# ── Geo event taxonomy ────────────────────────────────────────────

# Each entry: (search_query, event_type, affected_sectors, base_strength, direction_hint)
# direction_hint: "bullish_defence" / "bearish_global" / "bullish_energy" etc.
GEO_QUERIES = [
    # Middle East / Iran
    ("Iran military strike OR Iran war OR Iran sanctions",
     "middle_east_conflict", ["Defence", "Energy", "Commodities", "Shipping"], 75, "escalation"),
    ("Red Sea attack OR Houthi shipping OR Suez disruption",
     "shipping_route_disruption", ["Shipping", "Insurance", "Logistics", "Energy"], 80, "escalation"),
    ("Strait of Hormuz OR Iran oil OR OPEC supply cut",
     "energy_supply_shock", ["Energy", "Commodities", "Shipping"], 70, "bullish_energy"),
    # Russia / Ukraine / Europe
    ("Russia Ukraine escalation OR NATO conflict OR Russia energy",
     "russia_europe_conflict", ["Defence", "Energy", "Commodities", "Forex"], 70, "escalation"),
    ("Russia sanctions OR Russia oil OR Nord Stream",
     "russia_energy_sanctions", ["Energy", "Commodities"], 65, "bullish_energy"),
    # China / Taiwan / South China Sea
    ("Taiwan strait OR China military OR South China Sea",
     "china_taiwan_tension", ["Technology", "Defence", "Shipping"], 70, "escalation"),
    ("China trade sanctions OR chip export ban OR China tariffs",
     "china_trade_war", ["Technology", "Defence", "Global"], 65, "bearish_tech"),
    # Global shipping / supply chain
    ("Panama Canal disruption OR shipping route closure OR port blockade",
     "shipping_disruption", ["Shipping", "Logistics", "Insurance"], 65, "escalation"),
    ("container shortage OR supply chain crisis OR freight rates spike",
     "supply_chain_shock", ["Shipping", "Logistics", "Consumer"], 60, "mixed"),
    # Commodity / energy macro
    ("OPEC production cut OR oil supply shock OR energy crisis",
     "energy_macro", ["Energy", "Commodities"], 65, "bullish_energy"),
    ("LNG disruption OR natural gas supply OR pipeline attack",
     "lng_disruption", ["Energy", "Shipping"], 65, "bullish_energy"),
    # Defence / arms
    ("defence spending increase OR military budget OR arms deal",
     "defence_catalyst", ["Defence", "Space"], 60, "bullish_defence"),
    # Sanctions / trade
    ("new sanctions OR trade ban OR export controls",
     "sanctions", ["Defence", "Technology", "Energy", "Global"], 60, "mixed"),
    # Macro shock
    ("central bank emergency OR financial crisis OR bank collapse",
     "macro_shock", ["Finance", "Forex", "Commodities"], 70, "bearish_global"),
]

# Sector → affected tickers (for targeted signal generation)
SECTOR_TICKERS = {
    "Shipping":    ["ZIM", "FRO", "STNG", "DHT", "INSW"],
    "Defence":     ["LMT", "RTX", "NOC", "BAE.L", "GD", "HII"],
    "Energy":      ["XOM", "SHEL.L", "BP.L", "CL=F", "BZ=F", "NG=F", "LNG"],
    "Insurance":   ["BEZ.L"],
    "Logistics":   ["FDX", "UPS", "EXPD"],
    "Technology":  ["NVDA", "AMD", "AVGO"],
    "Commodities": ["GC=F", "CL=F", "BZ=F", "NG=F", "SI=F"],
    "Finance":     ["JPM", "GS"],
    "Forex":       ["EURUSD=X", "GBPUSD=X"],
    "Global":      ["SPY", "QQQ"],
    "Consumer":    ["AMZN", "SHOP"],
    "Space":       ["LMT", "RKLB", "NOC"],
}

# Direction of impact per event_type per sector
# +1 = bullish for sector, -1 = bearish, 0 = neutral/mixed
IMPACT_MAP = {
    "middle_east_conflict":     {"Defence": +1, "Energy": +1, "Shipping": +1, "Commodities": +1, "Technology": -1, "Consumer": -1},
    "shipping_route_disruption":{"Shipping": +1, "Insurance": +1, "Logistics": +1, "Energy": +1, "Consumer": -1, "Global": -1},
    "energy_supply_shock":      {"Energy": +1, "Commodities": +1, "Shipping": +1, "Consumer": -1, "Finance": -1},
    "russia_europe_conflict":   {"Defence": +1, "Energy": +1, "Commodities": +1, "Forex": -1, "Global": -1},
    "russia_energy_sanctions":  {"Energy": +1, "Commodities": +1, "Shipping": +1},
    "china_taiwan_tension":     {"Defence": +1, "Technology": -1, "Shipping": -1, "Global": -1},
    "china_trade_war":          {"Defence": +1, "Technology": -1, "Global": -1},
    "shipping_disruption":      {"Shipping": +1, "Insurance": +1, "Logistics": +1, "Consumer": -1},
    "supply_chain_shock":       {"Logistics": +1, "Shipping": +1, "Consumer": -1},
    "energy_macro":             {"Energy": +1, "Commodities": +1, "Consumer": -1},
    "lng_disruption":           {"Energy": +1, "Shipping": +1},
    "defence_catalyst":         {"Defence": +1, "Space": +1},
    "sanctions":                {"Energy": +1, "Defence": +1, "Technology": -1},
    "macro_shock":              {"Finance": -1, "Forex": -1, "Commodities": +1},
}


def _score_geo_text(text: str) -> Tuple[float, List[str]]:
    """
    Score geopolitical text for intensity and extract key signals.
    Returns (intensity_score 0-1, list of detected themes).
    """
    text_lower = text.lower()
    themes = []
    intensity = 0.0

    escalation_terms = {
        "strike": 0.9, "attack": 0.9, "war": 0.95, "invasion": 0.95,
        "missile": 0.85, "bomb": 0.85, "explosion": 0.8, "killed": 0.8,
        "escalat": 0.8, "conflict": 0.7, "tension": 0.65, "threat": 0.6,
        "sanction": 0.7, "blockade": 0.8, "closure": 0.75, "disruption": 0.7,
        "crisis": 0.75, "emergency": 0.8, "shutdown": 0.7, "seized": 0.85,
        "detained": 0.7, "troops": 0.75, "naval": 0.7, "warship": 0.8,
    }

    for term, weight in escalation_terms.items():
        if term in text_lower:
            themes.append(term)
            intensity = max(intensity, weight)

    # Geo locations boost intensity
    locations = [
        "iran", "red sea", "suez", "hormuz", "strait", "persian gulf",
        "ukraine", "russia", "taiwan", "south china sea", "gaza", "israel",
        "houthi", "yemen", "saudi", "opec", "nato",
    ]
    location_hits = [loc for loc in locations if loc in text_lower]
    if location_hits:
        themes.extend(location_hits)
        intensity = min(1.0, intensity + 0.1 * len(location_hits))

    return round(intensity, 2), themes


async def _fetch_geo_news(query: str) -> List[dict]:
    """Fetch news for a geo query. Returns list of articles."""
    if not GNEWS_API_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(GNEWS_API_URL, params={
                "q":      query,
                "token":  GNEWS_API_KEY,
                "lang":   "en",
                "sortby": "publishedAt",
                "max":    5,
            })
            if r.status_code != 200:
                return []
            return r.json().get("articles", [])
    except Exception as e:
        log.warning(f"GeoBot news fetch failed: {e}")
        return []


async def _perplexity_geo_analysis(event_summary: str, event_type: str) -> Optional[dict]:
    """
    Use Perplexity to get deep geopolitical analysis and market impact assessment.
    Only called for high-intensity events (score > 0.7).
    """
    if not PERPLEXITY_KEY:
        return None

    prompt = (
        f"Analyse this geopolitical event and its market implications:\n\n"
        f"Event: {event_summary}\n"
        f"Category: {event_type.replace('_', ' ')}\n\n"
        f"Respond ONLY in JSON:\n"
        f'{{"severity": "low|medium|high|critical", '
        f'"immediate_impact": "brief description", '
        f'"sectors_bullish": ["list"], '
        f'"sectors_bearish": ["list"], '
        f'"key_assets_to_watch": ["tickers"], '
        f'"duration_estimate": "hours|days|weeks|months", '
        f'"confidence": 0-100, '
        f'"geopolitical_context": "2 sentence background"}}'
    )

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                PERPLEXITY_URL,
                headers={"Authorization": f"Bearer {PERPLEXITY_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "llama-3.1-sonar-large-128k-online",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 400,
                    "temperature": 0.1,
                },
            )
            if r.status_code != 200:
                return None
            text = r.json().get("choices", [{}])[0].get("message", {}).get("content", "{}")
            text = text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            return __import__("json").loads(text)
    except Exception as e:
        log.warning(f"Perplexity geo analysis failed: {e}")
        return None


class GeoBot(ResearchBot):
    """
    Geopolitical intelligence bot.

    Unlike other bots, GeoBot is called with a special ticker "GEO:WORLD"
    or sector-specific like "GEO:SHIPPING", "GEO:MIDDLEEAST".
    It scans for events matching that geo theme and returns impact signals.
    """

    @property
    def name(self) -> str:
        return "GeoBot"

    @property
    def cache_ttl(self) -> int:
        return CACHE_TTL

    async def _fetch(self, ticker: str, asset_meta: dict) -> BotResult:
        """
        ticker can be:
          "GEO:WORLD"       — scan all geo queries
          "GEO:MIDDLEEAST"  — only Middle East queries
          "GEO:SHIPPING"    — only shipping disruption queries
          "GEO:ENERGY"      — only energy queries
          or a normal ticker — GeoBot assesses geopolitical exposure for that stock
        """

        # Determine which queries to run
        geo_filter = ticker.upper().replace("GEO:", "") if ticker.upper().startswith("GEO:") else None
        target_sector = asset_meta.get("sector", "") if not geo_filter else None

        queries_to_run = []
        for q_text, event_type, sectors, base_strength, direction in GEO_QUERIES:
            if geo_filter:
                # Filter by theme
                if geo_filter == "WORLD":
                    queries_to_run.append((q_text, event_type, sectors, base_strength, direction))
                elif geo_filter == "MIDDLEEAST" and "middle_east" in event_type or "iran" in event_type or "hormuz" in q_text.lower():
                    queries_to_run.append((q_text, event_type, sectors, base_strength, direction))
                elif geo_filter == "SHIPPING" and "shipping" in event_type:
                    queries_to_run.append((q_text, event_type, sectors, base_strength, direction))
                elif geo_filter == "ENERGY" and "energy" in event_type or "lng" in event_type:
                    queries_to_run.append((q_text, event_type, sectors, base_strength, direction))
                else:
                    queries_to_run.append((q_text, event_type, sectors, base_strength, direction))
            elif target_sector and target_sector in sectors:
                # For a normal ticker, only run queries affecting its sector
                queries_to_run.append((q_text, event_type, sectors, base_strength, direction))
            elif not target_sector:
                queries_to_run.append((q_text, event_type, sectors, base_strength, direction))

        if not queries_to_run:
            return self._empty_result(ticker, "No relevant geo queries for this asset")

        # Run queries (limit to 4 to conserve API calls)
        queries_to_run = queries_to_run[:4]

        all_findings = []
        bull_factors = []
        bear_factors = []
        top_intensity = 0.0
        top_event = None
        top_articles = []
        affected_sectors = set()

        for q_text, event_type, sectors, base_strength, direction in queries_to_run:
            articles = await _fetch_geo_news(q_text)
            if not articles:
                continue

            event_intensity = 0.0
            event_themes = []
            event_headlines = []

            for article in articles[:5]:
                title = article.get("title", "") or ""
                desc  = article.get("description", "") or ""
                combined = f"{title} {desc}"
                intensity, themes = _score_geo_text(combined)
                if intensity > 0.3:
                    event_intensity = max(event_intensity, intensity)
                    event_themes.extend(themes)
                    event_headlines.append(title[:100])

            if event_intensity < 0.35:
                continue  # Not significant enough

            affected_sectors.update(sectors)
            impact = IMPACT_MAP.get(event_type, {})

            # Generate factor strings
            event_label = event_type.replace("_", " ").title()
            for sector in sectors:
                sector_impact = impact.get(sector, 0)
                if sector_impact > 0:
                    bull_factors.append(
                        f"Geo catalyst [{event_label}] → {sector} sector bullish "
                        f"(intensity {event_intensity:.0%})"
                    )
                elif sector_impact < 0:
                    bear_factors.append(
                        f"Geo risk [{event_label}] → {sector} sector bearish "
                        f"(intensity {event_intensity:.0%})"
                    )

            finding = {
                "event_type":  event_type,
                "intensity":   event_intensity,
                "themes":      list(set(event_themes))[:5],
                "headlines":   event_headlines[:2],
                "sectors":     sectors,
                "direction":   direction,
                "base_strength": base_strength,
            }
            all_findings.append(finding)

            if event_intensity > top_intensity:
                top_intensity = event_intensity
                top_event = finding
                top_articles = event_headlines

        if not all_findings:
            return BotResult(
                bot_name=self.name, ticker=ticker,
                signal_inputs={"sentiment": 0.0, "catalystNews": 0.0, "sectorFlow": 0.0},
                bull_factors=["No significant geopolitical events detected"],
                bear_factors=["Geopolitical risk monitoring active"],
                summary="Geopolitical environment: no elevated signals",
                confidence=0.4, source="GeoBot/GNews",
            )

        # Score overall geo sentiment signal
        # High intensity geo events are usually risk-off for broad markets
        # but bullish for specific sectors (defence, energy, shipping)
        avg_intensity = sum(f["intensity"] for f in all_findings) / len(all_findings)

        # For a specific sector ticker — score direction based on impact map
        geo_sentiment = 0.0
        if target_sector:
            sector_impacts = []
            for finding in all_findings:
                impact = IMPACT_MAP.get(finding["event_type"], {})
                if target_sector in impact:
                    sector_impacts.append(impact[target_sector] * finding["intensity"])
            if sector_impacts:
                geo_sentiment = sum(sector_impacts) / len(sector_impacts)
        else:
            # General scan — positive means elevated geo risk (actionable)
            geo_sentiment = avg_intensity * 0.5  # moderate positive = "elevated risk environment"

        # Build summary
        if top_event:
            top_label = top_event["event_type"].replace("_", " ").title()
            top_sectors = ", ".join(top_event["sectors"][:3])
            summary = (
                f"Geo intelligence: {top_label} detected "
                f"(intensity {top_intensity:.0%}) — "
                f"affects {top_sectors}"
            )
            if top_articles:
                summary += f'. Latest: "{top_articles[0][:80]}"'
        else:
            summary = f"Geopolitical scan: {len(all_findings)} events detected"

        # Deep analysis for high-intensity events
        deep_analysis = None
        if top_intensity >= 0.75 and top_event and PERPLEXITY_KEY:
            event_summary = " | ".join(top_articles[:2])
            deep_analysis = await _perplexity_geo_analysis(event_summary, top_event["event_type"])
            if deep_analysis:
                severity = deep_analysis.get("severity", "medium")
                context  = deep_analysis.get("geopolitical_context", "")
                if context:
                    bull_factors.insert(0, f"Perplexity analysis [{severity.upper()}]: {context[:120]}")
                for sector in deep_analysis.get("sectors_bullish", []):
                    bull_factors.append(f"Perplexity: {sector} bullish given current geo context")
                for sector in deep_analysis.get("sectors_bearish", []):
                    bear_factors.append(f"Perplexity: {sector} bearish given current geo context")

        confidence = min(0.92, 0.4 + avg_intensity * 0.6)

        if not bull_factors:
            bull_factors = ["Geopolitical monitoring active — no immediate bullish signals"]
        if not bear_factors:
            bear_factors = ["Low geopolitical risk environment currently"]

        return BotResult(
            bot_name=self.name,
            ticker=ticker,
            signal_inputs={
                "sentiment":    round(geo_sentiment, 3),
                "catalystNews": round(min(1.0, avg_intensity), 3),
                "sectorFlow":   round(geo_sentiment * 0.7, 3),
            },
            bull_factors=bull_factors[:5],
            bear_factors=bear_factors[:5],
            summary=summary,
            confidence=round(confidence, 2),
            source="GeoBot/GNews" + ("/Perplexity" if deep_analysis else ""),
            raw={
                "findings":          all_findings,
                "top_intensity":     top_intensity,
                "affected_sectors":  list(affected_sectors),
                "deep_analysis":     deep_analysis,
            },
        )
