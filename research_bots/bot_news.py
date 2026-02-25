"""
Market Brain — 📰 News Bot
───────────────────────────
Fetches recent headlines for a ticker from GNews API.
Scores sentiment and detects catalyst events.

Produces:
  signal_inputs.sentiment      -1.0 to 1.0
  signal_inputs.catalystNews   -1.0 to 1.0

Free tier: 100 requests/day — we cache for 2 hours to stay well within limits.
GNews has better UK/international coverage than NewsAPI.

Setup:
  Set GNEWS_KEY environment variable.
  Get a free key at https://gnews.io (personal email accepted).

Catalyst detection:
  Positive: earnings beat, upgrade, partnership, FDA approval, contract win,
            buyback, dividend increase, new product
  Negative: earnings miss, downgrade, lawsuit, recall, investigation,
            guidance cut, layoffs, CEO departure
"""

import asyncio
import logging
import os
import re
from typing import Optional

import httpx

from base import ResearchBot, BotResult

log = logging.getLogger("mb.bots.news")

GNEWS_API_KEY = os.environ.get("GNEWS_KEY", "c7d8195679eab38431bbd674bb74fd96")
GNEWS_API_URL = "https://gnews.io/api/v4/search"
CACHE_TTL     = 7200   # 2 hours — conserves 100/day free tier quota

# Sentiment word lists
POSITIVE_WORDS = [
    "beat", "beats", "surges", "jumps", "rises", "gains", "rallies", "soars",
    "upgrade", "upgraded", "buy", "outperform", "overweight", "bullish",
    "partnership", "contract", "approval", "approved", "wins", "awarded",
    "buyback", "dividend", "record", "breakthrough", "launch", "strong",
    "exceeds", "top", "profit", "revenue growth", "raised guidance",
    "acquisition", "merger", "deal", "positive", "recovery", "rebound",
]
NEGATIVE_WORDS = [
    "miss", "misses", "falls", "drops", "tumbles", "plunges", "declines",
    "downgrade", "downgraded", "sell", "underperform", "underweight", "bearish",
    "lawsuit", "sued", "investigation", "probe", "recall", "warning",
    "cut guidance", "lowers guidance", "layoffs", "restructuring", "loss",
    "deficit", "missed", "below", "concern", "risk", "volatile", "weak",
    "disappoints", "disappointing", "breach", "hack", "fine", "penalty",
]

# High-impact catalyst terms
CATALYST_POSITIVE = [
    "earnings beat", "raised guidance", "fda approval", "fda approved",
    "contract awarded", "major contract", "partnership", "acquisition",
    "buyback", "share repurchase", "dividend increase", "analyst upgrade",
    "price target raised", "record revenue", "record earnings",
]
CATALYST_NEGATIVE = [
    "earnings miss", "missed estimates", "cut guidance", "lowered guidance",
    "fda rejection", "class action", "sec investigation", "doj probe",
    "ceo resign", "ceo departure", "recall", "data breach", "analyst downgrade",
    "price target cut", "going concern", "bankruptcy",
]


def _score_text(text: str) -> float:
    """Score a piece of text -1.0 to 1.0 based on word matching."""
    text_lower = text.lower()
    pos = sum(1 for w in POSITIVE_WORDS if w in text_lower)
    neg = sum(1 for w in NEGATIVE_WORDS if w in text_lower)
    total = pos + neg
    if total == 0:
        return 0.0
    return round((pos - neg) / total, 3)


def _detect_catalyst(text: str) -> Optional[tuple]:
    """Returns (catalyst_text, direction) or None."""
    text_lower = text.lower()
    for cat in CATALYST_POSITIVE:
        if cat in text_lower:
            return (cat, 1.0)
    for cat in CATALYST_NEGATIVE:
        if cat in text_lower:
            return (cat, -1.0)
    return None


class NewsBot(ResearchBot):
    """Fetches and scores news headlines for a ticker."""

    @property
    def name(self) -> str:
        return "NewsBot"

    @property
    def cache_ttl(self) -> int:
        return CACHE_TTL

    async def _fetch(self, ticker: str, asset_meta: dict) -> BotResult:
        if not GNEWS_API_KEY:
            return self._empty_result(ticker, "GNEWS_KEY not set")

        # Build search query — use company name if available for better results
        company_name = asset_meta.get("name", "")
        # Strip exchange suffixes for cleaner search
        clean_ticker = ticker.replace(".L", "").replace(".PA", "").replace("=X", "")
        # GNews works best with company name — fall back to ticker
        query = company_name if company_name and len(company_name) > 3 else clean_ticker

        # GNews API params
        params = {
            "q":        query,
            "token":    GNEWS_API_KEY,   # GNews uses 'token' not 'apiKey'
            "lang":     "en",
            "sortby":   "publishedAt",
            "max":      10,              # GNews free tier max per request
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(GNEWS_API_URL, params=params)
                if r.status_code == 429:
                    return self._empty_result(ticker, "GNews rate limit reached")
                if r.status_code == 403:
                    return self._empty_result(ticker, "GNews API key invalid or expired")
                if r.status_code != 200:
                    return self._empty_result(ticker, f"GNews error {r.status_code}")
                data = r.json()
        except Exception as e:
            return self._empty_result(ticker, f"GNews request failed: {e}")

        # GNews returns 'articles' same as NewsAPI — compatible structure
        articles = data.get("articles", [])
        if not articles:
            return BotResult(
                bot_name=self.name, ticker=ticker,
                signal_inputs={"sentiment": 0.0, "catalystNews": 0.0},
                bull_factors=[], bear_factors=[],
                summary="No recent news found",
                confidence=0.3, source="GNews",
            )

        # Score each article
        scores = []
        catalysts = []
        bull_factors = []
        bear_factors = []

        for article in articles[:10]:   # top 10 most recent
            title       = article.get("title", "") or ""
            description = article.get("description", "") or ""
            combined    = f"{title} {description}"

            score = _score_text(combined)
            scores.append(score)

            catalyst = _detect_catalyst(combined)
            if catalyst:
                cat_text, direction = catalyst
                catalysts.append(direction)
                if direction > 0:
                    bull_factors.append(f"Catalyst: {cat_text.title()} detected in recent news")
                else:
                    bear_factors.append(f"Risk: {cat_text.title()} detected in recent news")

            # Add top scored headlines to factors
            elif score > 0.3 and len(bull_factors) < 3:
                bull_factors.append(f"Positive coverage: {title[:80]}")
            elif score < -0.3 and len(bear_factors) < 3:
                bear_factors.append(f"Negative coverage: {title[:80]}")

        # Aggregate scores
        avg_sentiment   = sum(scores) / len(scores) if scores else 0.0
        avg_catalyst    = sum(catalysts) / len(catalysts) if catalysts else 0.0
        catalyst_signal = avg_catalyst if catalysts else avg_sentiment * 0.5

        # Confidence based on article count and recency
        confidence = min(0.9, 0.3 + (len(articles) / 20) * 0.6)

        # Summary
        if avg_sentiment > 0.2:
            summary = f"Predominantly positive news sentiment ({len(articles)} articles)"
        elif avg_sentiment < -0.2:
            summary = f"Predominantly negative news sentiment ({len(articles)} articles)"
        else:
            summary = f"Mixed news sentiment ({len(articles)} articles)"

        if catalysts:
            direction = "positive" if avg_catalyst > 0 else "negative"
            summary += f" — {len(catalysts)} {direction} catalyst(s) detected"

        # Ensure at least one factor each side
        if not bull_factors:
            bull_factors.append(f"No strongly negative headlines in recent {len(articles)} articles")
        if not bear_factors:
            bear_factors.append(f"No strong positive catalysts confirmed yet")

        return BotResult(
            bot_name=self.name,
            ticker=ticker,
            signal_inputs={
                "sentiment":    round(max(-1.0, min(1.0, avg_sentiment)), 3),
                "catalystNews": round(max(-1.0, min(1.0, catalyst_signal)), 3),
            },
            bull_factors=bull_factors[:3],
            bear_factors=bear_factors[:3],
            summary=summary,
            confidence=confidence,
            source="GNews",
            raw={"article_count": len(articles), "avg_sentiment": avg_sentiment},
        )
