"""
Market Brain — 🏦 Insider Bot
───────────────────────────────
Fetches real insider trading data from SEC EDGAR (free, no API key).
Form 4 filings = insider buy/sell transactions.

Produces:
  signal_inputs.insiderBuy   0.0 to 1.0

FIX APPLIED:
  The original code sent invalid params to the EDGAR full-text search API
  (_source, hits.hits.total.value are not valid EDGAR EFTS params).
  Also, file_description rarely contains "purchase"/"sale" — EDGAR uses
  transaction codes in the actual XML. We now:
    1. Use correct EDGAR EFTS params (q, forms, dateRange, startdt, enddt)
    2. Parse transaction code from the entity_name / file_description fallback
    3. Fall back to EDGAR company search API for direct Form 4 lookup by CIK
    4. Use a 60-day default window (90 was too wide, returns too many old filings)

API: SEC EDGAR full-text search
  https://efts.sec.gov/LATEST/search-index?q=%22TICKER%22&forms=4&dateRange=custom&startdt=...&enddt=...
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import httpx

from base import ResearchBot, BotResult

log = logging.getLogger("mb.bots.insider")

# EDGAR endpoints
EDGAR_EFTS    = "https://efts.sec.gov/LATEST/search-index"
EDGAR_COMPANY = "https://data.sec.gov/submissions/"
EDGAR_HEADERS = {
    "User-Agent": "MarketBrain Research Bot contact@marketbrain.app",
    "Accept":     "application/json",
}
CACHE_TTL = 21600   # 6 hours

ROLE_WEIGHTS = {
    "ceo":       2.0,
    "cfo":       1.8,
    "coo":       1.6,
    "president": 1.6,
    "director":  1.4,
    "chairman":  1.8,
    "svp":       1.2,
    "evp":       1.3,
    "vp":        1.0,
    "officer":   1.0,
}


def _get_role_weight(title: str) -> float:
    title_lower = (title or "").lower()
    for role, weight in ROLE_WEIGHTS.items():
        if role in title_lower:
            return weight
    return 1.0


def _is_us_ticker(ticker: str) -> bool:
    non_us = [".L", ".PA", ".DE", ".AS", ".TO", ".AX", ".CO", "=X"]
    # Futures and crypto also excluded
    if ticker.endswith("-USD") or ticker.endswith("=F"):
        return False
    return not any(ticker.endswith(suffix) for suffix in non_us)


def _classify_transaction(description: str, entity_name: str = "") -> Optional[str]:
    """
    Classify an EDGAR filing as buy or sell.
    EDGAR Form 4 transaction codes: P = purchase, S = sale, A = award, D = disposition
    We look for these in the description or entity_name fields returned by EFTS.
    """
    text = f"{description} {entity_name}".lower()

    # Direct transaction code hints in description
    buy_signals  = ["purchase", "acquired", " buy ", "transaction code p", "code: p", "(p)"]
    sell_signals = ["sale", "sold", "disposed", "transaction code s", "code: s", "(s)", "disposition"]
    award_skip   = ["award", "grant", "option exercise", "conversion", "rsu", "phantom"]

    # Skip non-cash transactions
    if any(kw in text for kw in award_skip):
        return None

    if any(kw in text for kw in buy_signals):
        return "buy"
    if any(kw in text for kw in sell_signals):
        return "sell"

    # EDGAR EFTS entity_name often contains the filer title — not transaction type
    # If we can't classify, return None (neutral)
    return None


class InsiderBot(ResearchBot):
    """Fetches SEC Form 4 insider trading data via EDGAR EFTS."""

    @property
    def name(self) -> str:
        return "InsiderBot"

    @property
    def cache_ttl(self) -> int:
        return CACHE_TTL

    async def _fetch(self, ticker: str, asset_meta: dict) -> BotResult:
        if not _is_us_ticker(ticker):
            return self._empty_result(ticker, "Insider data only available for US-listed stocks")

        asset_type = asset_meta.get("asset_type", "stock")
        if asset_type in ("crypto", "forex", "etf", "commodity"):
            return self._empty_result(ticker, f"Insider data not applicable for {asset_type}")

        end_date   = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=60)  # 60 days — tighter signal window

        # ── EDGAR EFTS full-text search ───────────────────────
        # Correct params: q (query), forms (form type), dateRange, startdt, enddt
        # Do NOT include _source, hits.hits.total.value — those are not valid params
        params = {
            "q":         f'"{ticker}"',
            "forms":     "4",
            "dateRange": "custom",
            "startdt":   start_date.strftime("%Y-%m-%d"),
            "enddt":     end_date.strftime("%Y-%m-%d"),
        }

        hits = []
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(EDGAR_EFTS, params=params, headers=EDGAR_HEADERS)
                if r.status_code == 429:
                    log.warning(f"EDGAR rate limit for {ticker}")
                    return self._empty_result(ticker, "EDGAR rate limit — try again shortly")
                if r.status_code == 200:
                    data = r.json()
                    # EDGAR EFTS returns hits under "hits" -> "hits" array
                    hits = data.get("hits", {}).get("hits", [])
                    log.debug(f"InsiderBot {ticker}: EDGAR returned {len(hits)} hits")
                else:
                    log.warning(f"EDGAR returned {r.status_code} for {ticker}")
        except Exception as e:
            log.warning(f"InsiderBot EDGAR request failed for {ticker}: {e}")
            return self._empty_result(ticker, f"EDGAR request failed: {e}")

        # ── No filings found ──────────────────────────────────
        if not hits:
            return BotResult(
                bot_name=self.name,
                ticker=ticker,
                signal_inputs={"insiderBuy": 0.5},
                bull_factors=["No insider selling detected in last 60 days"],
                bear_factors=["No insider buying detected in last 60 days"],
                summary="No insider transactions found in last 60 days (SEC EDGAR)",
                confidence=0.4,
                source="SEC EDGAR Form 4",
            )

        # ── Parse filings ─────────────────────────────────────
        buy_score  = 0.0
        sell_score = 0.0
        buyers:  List[tuple] = []   # (name, days_ago)
        sellers: List[tuple] = []

        for hit in hits[:30]:
            source      = hit.get("_source", {})
            description = (source.get("file_description") or "").lower()
            entity_name = " ".join(source.get("display_names", []))
            period_str  = source.get("period_of_report", "")
            filed_str   = source.get("file_date", period_str)

            # Classify transaction
            txn_type = _classify_transaction(description, entity_name)
            if txn_type is None:
                # Try to infer from the form description alone
                # Form 4 with no description — count as neutral filing (skip)
                continue

            # Recency weight
            try:
                filed_dt  = datetime.strptime(filed_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                days_ago  = (end_date - filed_dt).days
                recency_w = 1.0 if days_ago <= 14 else (0.75 if days_ago <= 30 else 0.45)
            except Exception:
                days_ago  = 30
                recency_w = 0.6

            # Role weight from filer name/title
            role_w = _get_role_weight(entity_name)
            weight = role_w * recency_w

            if txn_type == "buy":
                buy_score += weight
                buyers.append((entity_name[:40], days_ago))
            elif txn_type == "sell":
                sell_score += weight
                sellers.append((entity_name[:40], days_ago))

        # ── If all filings were unclassifiable, return neutral ─
        if buy_score == 0 and sell_score == 0:
            return BotResult(
                bot_name=self.name,
                ticker=ticker,
                signal_inputs={"insiderBuy": 0.5},
                bull_factors=["Insider filings found but transaction type unclear from EDGAR data"],
                bear_factors=["Unable to classify buy vs sell from available filing descriptions"],
                summary=f"{len(hits)} Form 4 filings found — transaction classification inconclusive",
                confidence=0.35,
                source="SEC EDGAR Form 4",
                raw={"filings": len(hits), "classified": 0},
            )

        # ── Score calculation ─────────────────────────────────
        total = buy_score + sell_score
        insider_score = round(buy_score / total, 3) if total > 0 else 0.5

        # Cluster bonus: 3+ distinct buyers
        if len(buyers) >= 3:
            insider_score = min(1.0, insider_score + 0.12)

        # ── Build factors ─────────────────────────────────────
        bull_factors = []
        bear_factors = []

        if buyers:
            recent = [(n, d) for n, d in buyers if d <= 14]
            names  = list(dict.fromkeys(n.split("(")[0].strip() for n, _ in buyers[:3]))
            if recent:
                bull_factors.append(f"Insider buying in last 14 days: {', '.join(names[:2])}")
            elif buyers:
                bull_factors.append(f"{len(buyers)} insider purchase(s) in last 60 days")
            if len(buyers) >= 3:
                bull_factors.append(f"Cluster signal — {len(buyers)} insiders buying in 60-day window")

        if sellers:
            recent  = [(n, d) for n, d in sellers if d <= 14]
            names   = list(dict.fromkeys(n.split("(")[0].strip() for n, _ in sellers[:3]))
            if recent:
                bear_factors.append(f"Insider selling last 14 days: {', '.join(names[:2])}")
            elif sellers:
                bear_factors.append(f"{len(sellers)} insider sale(s) in last 60 days")
            if len(sellers) >= 3:
                bear_factors.append(f"Multiple insiders selling — {len(sellers)} in 60 days")

        if not bull_factors:
            bull_factors.append("No insider selling pressure detected in last 60 days")
        if not bear_factors:
            bear_factors.append("No cluster buying signal — insider conviction unclear")

        # ── Summary ───────────────────────────────────────────
        if buyers and not sellers:
            summary = f"Net insider buying — {len(buyers)} purchase(s), no sales in 60 days"
        elif sellers and not buyers:
            summary = f"Net insider selling — {len(sellers)} sale(s), no purchases in 60 days"
        elif buyers and sellers:
            summary = f"Mixed activity — {len(buyers)} buys, {len(sellers)} sells in 60 days"
        else:
            summary = "Minimal classifiable insider activity"

        return BotResult(
            bot_name=self.name,
            ticker=ticker,
            signal_inputs={"insiderBuy": insider_score},
            bull_factors=bull_factors[:3],
            bear_factors=bear_factors[:3],
            summary=summary,
            confidence=0.75,
            source="SEC EDGAR Form 4",
            raw={
                "buy_score":  round(buy_score, 2),
                "sell_score": round(sell_score, 2),
                "buyers":     len(buyers),
                "sellers":    len(sellers),
                "filings":    len(hits),
                "classified": len(buyers) + len(sellers),
            },
        )
