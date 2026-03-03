"""
Market Brain — HiringBot
─────────────────────────
Detects employment signals: hiring spikes, freezes, category surges,
sector-wide patterns. Signals flow to Relay Bot then CoS.

Data sources (in priority order):
  1. RapidAPI LinkedIn Jobs (RAPIDAPI_KEY)
  2. SerpAPI Google Jobs (SERPAPI_KEY)
  3. GNews hiring headlines fallback (GNEWS_API_KEY — always available)

Output: raw signal packets per spec — no interpretation, no wave/confidence.
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Optional

import httpx

from base import ResearchBot, BotResult

log = logging.getLogger("mb.bots.hiring")

RAPIDAPI_KEY  = os.environ.get("RAPIDAPI_KEY", "")
SERPAPI_KEY   = os.environ.get("SERPAPI_KEY", "")
GNEWS_API_KEY = os.environ.get("GNEWS_API_KEY", "c7d8195679eab38431bbd674bb74fd96")
CACHE_TTL     = 7200  # 2 hours

# ── Job category taxonomy ─────────────────────────────────────────
# Maps detected job terms to strategic signal categories
JOB_CATEGORIES = {
    "engineering":   ["engineer", "developer", "architect", "devops", "sre", "platform"],
    "ai_ml":         ["machine learning", "ai engineer", "data scientist", "llm", "ml ops", "nlp"],
    "defence":       ["defence", "defense", "security clearance", "classified", "weapons", "military"],
    "logistics":     ["logistics", "supply chain", "procurement", "warehouse", "freight", "shipping"],
    "compliance":    ["compliance", "regulatory", "legal counsel", "risk officer", "aml", "kyc"],
    "sales_growth":  ["sales", "business development", "account executive", "revenue", "partnerships"],
    "operations":    ["operations", "plant manager", "manufacturing", "production", "quality"],
    "finance":       ["cfo", "finance director", "treasury", "investor relations", "ipo"],
    "crisis_prep":   ["crisis", "emergency", "continuity", "resilience", "incident response"],
    "expansion":     ["head of", "vp of", "director of", "chief", "gm", "general manager"],
}

# Sector → strategic hiring implication
HIRING_SECTOR_MAP = {
    "Technology":   {"ai_ml": "bullish", "engineering": "bullish", "compliance": "neutral"},
    "Defence":      {"defence": "bullish", "engineering": "bullish", "crisis_prep": "bullish"},
    "Shipping":     {"logistics": "bullish", "operations": "bullish", "crisis_prep": "bearish"},
    "Energy":       {"engineering": "bullish", "compliance": "neutral", "operations": "bullish"},
    "Finance":      {"compliance": "bearish", "finance": "neutral", "crisis_prep": "bearish"},
    "Healthcare":   {"compliance": "neutral", "engineering": "bullish"},
    "Consumer":     {"logistics": "bullish", "sales_growth": "bullish"},
}

# ── Intensity scoring ─────────────────────────────────────────────
def _score_intensity(job_count: int, baseline: int = 10) -> float:
    """Map job count vs baseline to 0.0-1.0 intensity."""
    if job_count <= 0: return 0.0
    ratio = job_count / max(baseline, 1)
    if ratio >= 5.0: return 1.0
    if ratio >= 3.0: return 0.85
    if ratio >= 2.0: return 0.70
    if ratio >= 1.5: return 0.55
    if ratio >= 1.0: return 0.40
    return 0.25


def _detect_categories(text: str) -> list:
    """Detect job categories from job title/description text."""
    text_lower = text.lower()
    found = []
    for category, keywords in JOB_CATEGORIES.items():
        if any(kw in text_lower for kw in keywords):
            found.append(category)
    return found


def _derive_direction(categories: list, sector: str = "") -> str:
    """Derive hiring signal direction from categories and sector."""
    sector_map = HIRING_SECTOR_MAP.get(sector, {})
    # Freeze/reduction signals → bearish
    if not categories:
        return "neutral"
    bullish = sum(1 for c in categories if sector_map.get(c) == "bullish")
    bearish = sum(1 for c in categories if sector_map.get(c) == "bearish")
    if bullish > bearish: return "bullish"
    if bearish > bullish: return "bearish"
    # Strategic expansion always bullish
    if "expansion" in categories or "sales_growth" in categories:
        return "bullish"
    # Crisis prep or compliance surge → bearish
    if "crisis_prep" in categories or "compliance" in categories:
        return "bearish"
    return "neutral"


# ── Data sources ──────────────────────────────────────────────────

async def _fetch_rapidapi_jobs(query: str, company: str = "") -> list:
    """Fetch jobs from RapidAPI LinkedIn Jobs Search."""
    if not RAPIDAPI_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://linkedin-jobs-search.p.rapidapi.com/",
                params={
                    "query":    f"{company} {query}".strip(),
                    "location": "Worldwide",
                    "page":     "1",
                    "sort_by":  "recent",
                },
                headers={
                    "X-RapidAPI-Key":  RAPIDAPI_KEY,
                    "X-RapidAPI-Host": "linkedin-jobs-search.p.rapidapi.com",
                },
            )
            if r.status_code == 200:
                return r.json() if isinstance(r.json(), list) else r.json().get("jobs", [])
    except Exception as e:
        log.debug(f"RapidAPI jobs error: {e}")
    return []


async def _fetch_serpapi_jobs(query: str, company: str = "") -> list:
    """Fetch jobs from SerpAPI Google Jobs."""
    if not SERPAPI_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://serpapi.com/search",
                params={
                    "engine":   "google_jobs",
                    "q":        f"{company} {query} jobs".strip(),
                    "api_key":  SERPAPI_KEY,
                    "num":      20,
                },
            )
            if r.status_code == 200:
                return r.json().get("jobs_results", [])
    except Exception as e:
        log.debug(f"SerpAPI jobs error: {e}")
    return []


async def _fetch_gnews_hiring(company_name: str, ticker: str) -> list:
    """Fallback: search GNews for hiring news about this company."""
    if not GNEWS_API_KEY:
        return []
    clean = ticker.replace(".L", "").replace("=X", "").replace("-USD", "")
    query = f"{company_name or clean} hiring OR layoffs OR workforce OR jobs"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://gnews.io/api/v4/search",
                params={
                    "q":      query,
                    "token":  GNEWS_API_KEY,
                    "lang":   "en",
                    "max":    5,
                    "sortby": "publishedAt",
                },
            )
            if r.status_code == 200:
                return r.json().get("articles", [])
    except Exception as e:
        log.debug(f"GNews hiring error: {e}")
    return []


def _parse_gnews_hiring(articles: list, company_name: str) -> dict:
    """
    Parse GNews articles for hiring signals.
    Returns {pattern, magnitude, categories, direction, intensity, notes}
    """
    if not articles:
        return {}

    hiring_keywords  = ["hiring", "recruiting", "headcount", "workforce expansion", "adding jobs", "new roles"]
    layoff_keywords  = ["layoffs", "lay off", "job cuts", "redundancies", "downsizing", "restructuring", "firing"]
    freeze_keywords  = ["hiring freeze", "pause hiring", "halt recruitment", "no new hires"]

    hiring_count  = 0
    layoff_count  = 0
    freeze_count  = 0
    all_categories = []
    notes_list = []

    for article in articles:
        text = f"{article.get('title', '')} {article.get('description', '')}".lower()
        if any(k in text for k in hiring_keywords):  hiring_count += 1
        if any(k in text for k in layoff_keywords):  layoff_count += 1
        if any(k in text for k in freeze_keywords):  freeze_count += 1
        cats = _detect_categories(text)
        all_categories.extend(cats)
        if article.get("title"):
            notes_list.append(article["title"][:80])

    if not any([hiring_count, layoff_count, freeze_count]):
        return {}

    # Determine pattern
    if freeze_count > 0:
        pattern   = "freeze"
        direction = "bearish"
        magnitude = "high" if freeze_count >= 2 else "medium"
        intensity = 0.7
    elif layoff_count > hiring_count:
        pattern   = "reduction"
        direction = "bearish"
        magnitude = "high" if layoff_count >= 3 else "medium"
        intensity = _score_intensity(layoff_count, 2)
    elif hiring_count > 0:
        cats = list(set(all_categories))
        pattern   = "sustained_increase" if hiring_count >= 3 else "spike"
        direction = _derive_direction(cats)
        magnitude = "high" if hiring_count >= 3 else "medium" if hiring_count >= 2 else "low"
        intensity = _score_intensity(hiring_count, 2)
    else:
        return {}

    return {
        "pattern":        pattern,
        "magnitude":      magnitude,
        "job_categories": list(set(all_categories))[:5],
        "direction":      direction,
        "intensity":      round(intensity, 2),
        "notes":          notes_list[0] if notes_list else f"Hiring activity detected for {company_name}",
    }


class HiringBot(ResearchBot):
    """
    Detects employment signals for a company/ticker.
    Outputs structured signal packets compatible with Relay Bot.
    Does not assign wave or confidence — detection only.
    """

    @property
    def name(self) -> str:
        return "HiringBot"

    @property
    def cache_ttl(self) -> int:
        return CACHE_TTL

    async def _fetch(self, ticker: str, asset_meta: dict) -> BotResult:
        company_name = asset_meta.get("name", "")
        sector       = asset_meta.get("sector", "")

        # Skip non-stock assets — hiring signals only apply to companies
        asset_type = asset_meta.get("asset_type", "stock")
        if asset_type not in ("stock",) or ticker.startswith("GEO:"):
            return self._empty_result(ticker, "HiringBot: not applicable for this asset type")

        # Try APIs in priority order
        jobs      = []
        source    = "GNews"
        articles  = []

        if RAPIDAPI_KEY:
            jobs   = await _fetch_rapidapi_jobs("", company_name or ticker)
            source = "RapidAPI/LinkedIn"
        elif SERPAPI_KEY:
            jobs   = await _fetch_serpapi_jobs("", company_name or ticker)
            source = "SerpAPI/Google"

        # Parse structured job listings
        if jobs:
            categories = []
            for job in jobs[:20]:
                title = job.get("title", "") or job.get("job_title", "") or ""
                desc  = job.get("description", "") or job.get("job_description", "") or ""
                cats  = _detect_categories(f"{title} {desc}")
                categories.extend(cats)

            categories = list(set(categories))
            job_count  = len(jobs)
            direction  = _derive_direction(categories, sector)
            intensity  = _score_intensity(job_count, baseline=8)
            magnitude  = "high" if job_count >= 15 else "medium" if job_count >= 5 else "low"
            pattern    = "spike" if job_count >= 15 else "sustained_increase" if job_count >= 5 else "category_surge"
            notes      = f"{job_count} open roles detected"
            if categories:
                notes += f" — categories: {', '.join(categories[:3])}"

        else:
            # GNews fallback
            articles = await _fetch_gnews_hiring(company_name, ticker)
            parsed   = _parse_gnews_hiring(articles, company_name or ticker)
            if not parsed:
                return BotResult(
                    bot_name=self.name, ticker=ticker,
                    signal_inputs={"sentiment": 0.0, "catalystNews": 0.0},
                    bull_factors=[],
                    bear_factors=["No hiring signals detected"],
                    summary=f"No hiring activity found for {company_name or ticker}",
                    confidence=0.2, source="HiringBot/GNews",
                )
            categories = parsed["job_categories"]
            direction  = parsed["direction"]
            intensity  = parsed["intensity"]
            magnitude  = parsed["magnitude"]
            pattern    = parsed["pattern"]
            notes      = parsed["notes"]
            source     = "GNews"

        # Build BotResult — signal_inputs feed into CoS via Relay
        sentiment_score = intensity if direction == "bullish" else -intensity if direction == "bearish" else 0.0

        bull_factors = []
        bear_factors = []

        if direction == "bullish":
            bull_factors.append(f"Hiring signal [{pattern.replace('_',' ')}]: {notes}")
            if "ai_ml" in categories:
                bull_factors.append("AI/ML hiring surge — strategic capability build")
            if "expansion" in categories:
                bull_factors.append("Senior/exec hiring — indicates growth phase")
            if "sales_growth" in categories:
                bull_factors.append("Sales hiring — revenue expansion expected")
        elif direction == "bearish":
            bear_factors.append(f"Negative hiring signal [{pattern}]: {notes}")
            if "compliance" in categories:
                bear_factors.append("Compliance/legal hiring spike — regulatory pressure")
            if "crisis_prep" in categories:
                bear_factors.append("Crisis/resilience hiring — potential operational stress")
        else:
            bull_factors.append(f"Hiring activity detected: {notes}")

        # Confidence from magnitude
        conf_map = {"high": 0.72, "medium": 0.52, "low": 0.35}
        confidence = conf_map.get(magnitude, 0.4)

        summary = f"{company_name or ticker}: {pattern.replace('_',' ')} detected [{magnitude} magnitude]"
        if categories:
            summary += f" — {', '.join(categories[:2])}"

        return BotResult(
            bot_name=self.name,
            ticker=ticker,
            signal_inputs={
                "sentiment":    round(sentiment_score, 3),
                "catalystNews": round(abs(sentiment_score) * 0.6, 3),
                "sectorFlow":   round(sentiment_score * 0.4, 3),
            },
            bull_factors=bull_factors[:4],
            bear_factors=bear_factors[:4],
            summary=summary,
            confidence=confidence,
            source=f"HiringBot/{source}",
            raw={
                "pattern":        pattern,
                "magnitude":      magnitude,
                "job_categories": categories,
                "direction":      direction,
                "intensity":      intensity,
                "notes":          notes,
                "source":         source,
            },
        )
