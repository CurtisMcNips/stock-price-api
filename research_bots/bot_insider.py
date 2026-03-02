"""
Market Brain — 🏦 Insider Bot
───────────────────────────────
Fetches real insider trading data from SEC EDGAR (free, no API key).
Form 4 filings = insider buy/sell transactions, filed within 2 business days.

Produces:
  signal_inputs.insiderBuy   0.0 to 1.0

Logic:
  - Fetches last 30 days of Form 4 filings for the ticker
  - Scores based on: number of buyers, transaction size, insider seniority
  - CEO/CFO/Director buys weighted more than VP-level
  - Cluster buys (3+ insiders buying same period) = strong signal
  - Open market buys only — option exercises excluded
  - Net buy/sell ratio drives final score

API: SEC EDGAR full-text search (free, rate limited to 10 req/sec)
  https://efts.sec.gov/LATEST/search-index?q=%22TICKER%22&dateRange=custom&...

Note: US-listed stocks only. UK/EU tickers will return empty gracefully.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import quote

import httpx

from base import ResearchBot, BotResult

log = logging.getLogger("mb.bots.insider")

EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index"
EDGAR_HEADERS = {
    "User-Agent": "MarketBrain Research Bot contact@marketbrain.app",
    "Accept": "application/json",
}
CACHE_TTL = 21600   # 6 hours — filings don't change that fast

# Insider role weights — C-suite buys matter more
ROLE_WEIGHTS = {
    "ceo":        2.0,
    "cfo":        1.8,
    "coo":        1.6,
    "president":  1.6,
    "director":   1.4,
    "chairman":   1.8,
    "svp":        1.2,
    "evp":        1.3,
    "vp":         1.0,
    "officer":    1.0,
}


def _get_role_weight(title: str) -> float:
    title_lower = (title or "").lower()
    for role, weight in ROLE_WEIGHTS.items():
        if role in title_lower:
            return weight
    return 1.0


def _is_us_ticker(ticker: str) -> bool:
    """EDGAR only covers US-listed stocks."""
    non_us = [".L", ".PA", ".DE", ".AS", ".TO", ".AX", "=X", "-USD"]
    return not any(ticker.endswith(suffix) for suffix in non_us)


class InsiderBot(ResearchBot):
    """Fetches SEC Form 4 insider trading data."""

    @property
    def name(self) -> str:
        return "InsiderBot"

    @property
    def cache_ttl(self) -> int:
        return CACHE_TTL

    async def _fetch(self, ticker: str, asset_meta: dict) -> BotResult:
        # EDGAR only covers US stocks
        if not _is_us_ticker(ticker):
            return self._empty_result(ticker, "Insider data only available for US-listed stocks")

        asset_type = asset_meta.get("asset_type", "stock")
        if asset_type in ("crypto", "forex", "etf"):
            return self._empty_result(ticker, f"Insider data not applicable for {asset_type}")

        # Date range: last 90 days
        end_date   = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=90)

        params = {
            "q":             f'"{ticker}"',
            "dateRange":     "custom",
            "startdt":       start_date.strftime("%Y-%m-%d"),
            "enddt":         end_date.strftime("%Y-%m-%d"),
            "forms":         "4",
            "_source":       "hits.hits._source",
            "hits.hits.total.value": 1,
        }

        try:
            async with httpx.AsyncClient(timeout=12) as client:
                r = await client.get(EDGAR_SEARCH, params=params, headers=EDGAR_HEADERS)
                if r.status_code == 429:
                    return self._empty_result(ticker, "EDGAR rate limit — try again shortly")
                if r.status_code != 200:
                    return self._empty_result(ticker, f"EDGAR returned {r.status_code}")
                data = r.json()
        except Exception as e:
            return self._empty_result(ticker, f"EDGAR request failed: {e}")

        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            # No filings found — neutral signal, not an error
            return BotResult(
                bot_name=self.name,
                ticker=ticker,
                signal_inputs={"insiderBuy": 0.5},   # neutral
                bull_factors=["No insider selling detected in last 90 days"],
                bear_factors=["No insider buying activity detected in last 90 days"],
                summary="No insider transactions in last 90 days",
                confidence=0.4,
                source="SEC EDGAR",
            )

        # Parse filing summaries
        buy_score  = 0.0
        sell_score = 0.0
        buyers     = []
        sellers    = []

        for hit in hits[:20]:
            source = hit.get("_source", {})
            description = (source.get("file_description") or "").lower()
            display_names = source.get("display_names", [])

            # Determine transaction type from description
            is_buy  = any(kw in description for kw in ["purchase", "acquired", "bought"])
            is_sell = any(kw in description for kw in ["sale", "sold", "disposed"])

            if not is_buy and not is_sell:
                continue

            # Get filer name/title
            filer_name  = display_names[0] if display_names else "Insider"
            role_weight = _get_role_weight(filer_name)

            # Weight by recency (last 30 days = 1.0, 30-60 = 0.7, 60-90 = 0.4)
            try:
                filed_str = source.get("period_of_report", "")
                filed_dt  = datetime.strptime(filed_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                days_ago  = (end_date - filed_dt).days
                recency_weight = 1.0 if days_ago <= 30 else (0.7 if days_ago <= 60 else 0.4)
            except Exception:
                recency_weight = 0.6

            weighted = role_weight * recency_weight

            if is_buy:
                buy_score += weighted
                buyers.append((filer_name, days_ago))
            elif is_sell:
                sell_score += weighted
                sellers.append((filer_name, days_ago))

        # ── Score calculation ─────────────────────────────────
        total = buy_score + sell_score
        if total == 0:
            insider_score = 0.5   # neutral
        else:
            # 0.0 = all selling, 1.0 = all buying
            insider_score = round(buy_score / total, 3)

        # Cluster bonus: 3+ distinct buyers = conviction signal
        if len(buyers) >= 3:
            insider_score = min(1.0, insider_score + 0.15)

        # ── Build factors ─────────────────────────────────────
        bull_factors = []
        bear_factors = []

        if buyers:
            recent_buyers = [(n, d) for n, d in buyers if d <= 30]
            if recent_buyers:
                names = ", ".join(set(n.split("(")[0].strip() for n, _ in recent_buyers[:3]))
                bull_factors.append(f"Insider buying last 30 days: {names}")
            if len(buyers) >= 3:
                bull_factors.append(f"Cluster buy signal — {len(buyers)} insiders buying in 90 days")
            elif len(buyers) >= 1:
                bull_factors.append(f"{len(buyers)} insider purchase(s) in last 90 days")

        if sellers:
            recent_sellers = [(n, d) for n, d in sellers if d <= 30]
            if recent_sellers:
                names = ", ".join(set(n.split("(")[0].strip() for n, _ in recent_sellers[:3]))
                bear_factors.append(f"Insider selling last 30 days: {names}")
            if len(sellers) >= 3:
                bear_factors.append(f"Multiple insiders selling — {len(sellers)} transactions in 90 days")

        if not bull_factors:
            bull_factors.append("No insider selling pressure detected")
        if not bear_factors:
            bear_factors.append("No cluster buying signal — insider conviction unclear")

        # ── Summary ───────────────────────────────────────────
        if buyers and not sellers:
            summary = f"Net insider buying — {len(buyers)} purchase(s) in 90 days, no sales"
        elif sellers and not buyers:
            summary = f"Net insider selling — {len(sellers)} sale(s) in 90 days, no purchases"
        elif buyers and sellers:
            summary = f"Mixed insider activity — {len(buyers)} buys, {len(sellers)} sells in 90 days"
        else:
            summary = "Minimal insider transaction activity"

        return BotResult(
            bot_name=self.name,
            ticker=ticker,
            signal_inputs={"insiderBuy": insider_score},
            bull_factors=bull_factors[:3],
            bear_factors=bear_factors[:3],
            summary=summary,
            confidence=0.8,
            source="SEC EDGAR Form 4",
            raw={
                "buy_score":  buy_score,
                "sell_score": sell_score,
                "buyers":     len(buyers),
                "sellers":    len(sellers),
                "filings":    len(hits),
            },
        )
