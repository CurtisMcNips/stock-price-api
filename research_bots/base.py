"""
Market Brain — Research Bot Base
──────────────────────────────────
All research bots inherit from ResearchBot.

Each bot produces a BotResult containing:
  - signal_inputs: dict of values that plug directly into generateSignals()
  - factors: list of human-readable strings for the bull/bear panel
  - summary: one-line explanation of what the bot found
  - confidence: 0.0-1.0 how confident the bot is in its data
  - source: where the data came from
  - cached: whether this came from cache

Signal input keys match what generateSignals() expects:
  sentiment      -1.0 to 1.0
  catalystNews   -1.0 to 1.0
  sectorFlow     -1.0 to 1.0
  revGrowth      float (%)
  daysToEarnings float (days)
  insiderBuy     0.0 to 1.0
  shortInt       float (%)
  earningsBeat   float (%)
  debtRatio      float
"""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

log = logging.getLogger("mb.bots")

CACHE_TTL_DEFAULT = 1800  # 30 minutes


@dataclass
class BotResult:
    bot_name:      str
    ticker:        str
    signal_inputs: Dict[str, float]   # feeds directly into signal engine
    bull_factors:  List[str]          # positive reasons
    bear_factors:  List[str]          # negative reasons
    summary:       str                # one-liner for UI
    confidence:    float              # 0.0-1.0
    source:        str                # data source name
    cached:        bool = False
    error:         Optional[str] = None
    raw:           Dict[str, Any] = field(default_factory=dict)  # raw data for debugging
    timestamp:     float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "bot":           self.bot_name,
            "ticker":        self.ticker,
            "signal_inputs": self.signal_inputs,
            "bull_factors":  self.bull_factors,
            "bear_factors":  self.bear_factors,
            "summary":       self.summary,
            "confidence":    round(self.confidence, 2),
            "source":        self.source,
            "cached":        self.cached,
            "error":         self.error,
            "timestamp":     int(self.timestamp),
        }


# ── In-memory result cache ────────────────────────────────────
_bot_cache: Dict[str, dict] = {}   # "BotName:TICKER" -> { ts, result }


def cache_get(bot_name: str, ticker: str, ttl: int = CACHE_TTL_DEFAULT) -> Optional[BotResult]:
    key = f"{bot_name}:{ticker}"
    entry = _bot_cache.get(key)
    if entry and (time.time() - entry["ts"]) < ttl:
        result = entry["result"]
        result.cached = True
        return result
    return None


def cache_set(bot_name: str, ticker: str, result: BotResult):
    _bot_cache[f"{bot_name}:{ticker}"] = {"ts": time.time(), "result": result}


# ── Base class ────────────────────────────────────────────────
class ResearchBot(ABC):
    """
    Base class for all Market Brain research bots.

    Subclasses must implement:
      - name: str property
      - cache_ttl: int property (seconds)
      - _fetch(ticker, asset_meta) -> BotResult

    The run() method handles caching and error recovery automatically.
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    def cache_ttl(self) -> int:
        return CACHE_TTL_DEFAULT

    @abstractmethod
    async def _fetch(self, ticker: str, asset_meta: dict) -> BotResult: ...

    async def run(self, ticker: str, asset_meta: dict = None) -> BotResult:
        """Public entry point. Returns cached result if fresh enough."""
        cached = cache_get(self.name, ticker, self.cache_ttl)
        if cached:
            return cached
        try:
            result = await self._fetch(ticker, asset_meta or {})
            cache_set(self.name, ticker, result)
            return result
        except Exception as e:
            log.error(f"{self.name} failed for {ticker}: {e}")
            return BotResult(
                bot_name=self.name, ticker=ticker,
                signal_inputs={}, bull_factors=[], bear_factors=[],
                summary=f"{self.name} unavailable",
                confidence=0.0, source="error", error=str(e),
            )

    def _empty_result(self, ticker: str, reason: str) -> BotResult:
        return BotResult(
            bot_name=self.name, ticker=ticker,
            signal_inputs={}, bull_factors=[], bear_factors=[],
            summary=reason, confidence=0.0, source=self.name,
        )
