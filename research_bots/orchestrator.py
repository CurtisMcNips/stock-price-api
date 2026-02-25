"""
Market Brain — 🤖 Bot Orchestrator
─────────────────────────────────────
Runs all research bots in parallel and merges their results into
a single unified BotResearch object that app.py can serve.

Usage in app.py:
    from research_bots.orchestrator import run_all_bots
    result = await run_all_bots(ticker, asset_meta)
    return result.to_dict()

Output structure:
    {
      "ticker": "AAPL",
      "signal_inputs": {            ← merged, confidence-weighted
        "sentiment":      0.4,
        "catalystNews":   0.3,
        "sectorFlow":     0.2,
        "revGrowth":      18.5,
        "daysToEarnings": 12.0,
        "insiderBuy":     0.75,
        "shortInt":       4.2,
        "earningsBeat":   8.3,
        "debtRatio":      0.45,
      },
      "bull_factors": [...],        ← top 5 from all bots
      "bear_factors": [...],        ← top 5 from all bots
      "bot_summaries": {...},       ← per-bot one-liners
      "sources": [...],
      "overall_confidence": 0.78,
      "timestamp": 1234567890,
    }
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from base import BotResult, cache_get, cache_set

log = logging.getLogger("mb.bots.orchestrator")

# ── Lazy imports so missing optional bots don't break startup ─
def _load_bots():
    bots = []
    bot_classes = [
        ("bot_news",              "NewsBot"),
        ("bot_earnings",          "EarningsBot"),
        ("bot_macro",             "MacroBot"),
        ("bot_insider",           "InsiderBot"),
        ("bot_fundamentals",      "FundamentalsBot"),
        ("bot_technical_levels",  "TechnicalLevelsBot"),
        ("bot_analyst",           "AnalystBot"),
    ]
    for module_name, class_name in bot_classes:
        try:
            import importlib
            module = importlib.import_module(module_name)
            bot_class = getattr(module, class_name)
            bots.append(bot_class())
            log.info(f"Loaded {class_name}")
        except Exception as e:
            log.warning(f"Could not load {class_name}: {e}")
    return bots

_BOTS = None

def get_bots():
    global _BOTS
    if _BOTS is None:
        _BOTS = _load_bots()
    return _BOTS


# ── Signal input precedence ───────────────────────────────────
# When multiple bots produce the same signal input,
# we use confidence-weighted averaging.
SIGNAL_WEIGHTS = {
    "sentiment":      ["NewsBot", "AnalystBot"],
    "catalystNews":   ["NewsBot"],
    "sectorFlow":     ["MacroBot"],
    "revGrowth":      ["FundamentalsBot"],
    "daysToEarnings": ["EarningsBot"],
    "insiderBuy":     ["InsiderBot"],
    "shortInt":       ["FundamentalsBot"],
    "earningsBeat":   ["EarningsBot"],
    "debtRatio":      ["FundamentalsBot"],
}


@dataclass
class BotResearch:
    ticker:             str
    signal_inputs:      Dict[str, float]
    bull_factors:       List[str]
    bear_factors:       List[str]
    bot_summaries:      Dict[str, str]
    bot_confidences:    Dict[str, float]
    sources:            List[str]
    overall_confidence: float
    errors:             Dict[str, str] = field(default_factory=dict)
    timestamp:          float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "ticker":             self.ticker,
            "signal_inputs":      self.signal_inputs,
            "bull_factors":       self.bull_factors,
            "bear_factors":       self.bear_factors,
            "bot_summaries":      self.bot_summaries,
            "bot_confidences":    {k: round(v, 2) for k, v in self.bot_confidences.items()},
            "sources":            self.sources,
            "overall_confidence": round(self.overall_confidence, 2),
            "errors":             self.errors,
            "timestamp":          int(self.timestamp),
        }


def _merge_signal_inputs(results: List[BotResult]) -> Dict[str, float]:
    """
    Merge signal inputs from all bots using confidence-weighted averaging.
    Higher confidence bots have more influence on shared keys.
    """
    merged = {}
    accumulators: Dict[str, list] = {}   # key -> [(value, confidence)]

    for result in results:
        if result.error or not result.signal_inputs:
            continue
        for key, value in result.signal_inputs.items():
            if value is None:
                continue
            accumulators.setdefault(key, []).append((value, result.confidence))

    for key, entries in accumulators.items():
        total_conf = sum(c for _, c in entries)
        if total_conf == 0:
            merged[key] = entries[0][0]
        else:
            weighted = sum(v * c for v, c in entries) / total_conf
            merged[key] = round(weighted, 3)

    return merged


def _merge_factors(results: List[BotResult], max_per_side: int = 5) -> tuple:
    """
    Collect and deduplicate bull/bear factors from all bots.
    Higher confidence bots' factors appear first.
    """
    sorted_results = sorted(results, key=lambda r: r.confidence, reverse=True)

    bull_factors = []
    bear_factors = []
    seen_bull    = set()
    seen_bear    = set()

    for result in sorted_results:
        if result.error:
            continue
        for factor in result.bull_factors:
            key = factor[:50].lower()
            if key not in seen_bull and len(bull_factors) < max_per_side:
                bull_factors.append(factor)
                seen_bull.add(key)
        for factor in result.bear_factors:
            key = factor[:50].lower()
            if key not in seen_bear and len(bear_factors) < max_per_side:
                bear_factors.append(factor)
                seen_bear.add(key)

    return bull_factors, bear_factors


async def run_all_bots(
    ticker: str,
    asset_meta: dict = None,
    bots: list = None,
    timeout: float = 15.0,
) -> BotResearch:
    """
    Run all research bots in parallel with a timeout.
    Returns merged BotResearch object.
    """
    if bots is None:
        bots = get_bots()

    if not bots:
        return BotResearch(
            ticker=ticker, signal_inputs={}, bull_factors=[], bear_factors=[],
            bot_summaries={}, bot_confidences={}, sources=[],
            overall_confidence=0.0,
            errors={"orchestrator": "No bots available"},
        )

    asset_meta = asset_meta or {}

    # Run all bots concurrently with timeout
    tasks = [asyncio.create_task(bot.run(ticker, asset_meta)) for bot in bots]
    done, pending = await asyncio.wait(tasks, timeout=timeout)

    # Cancel any that timed out
    for task in pending:
        task.cancel()
        log.warning(f"Bot timed out for {ticker}")

    results = []
    for task in done:
        try:
            results.append(task.result())
        except Exception as e:
            log.error(f"Bot task error: {e}")

    # ── Merge results ─────────────────────────────────────────
    signal_inputs    = _merge_signal_inputs(results)
    bull_factors, bear_factors = _merge_factors(results)

    bot_summaries    = {r.bot_name: r.summary     for r in results}
    bot_confidences  = {r.bot_name: r.confidence  for r in results}
    sources          = list({r.source for r in results if not r.error})
    errors           = {r.bot_name: r.error for r in results if r.error}

    # Overall confidence = average of non-error bot confidences
    valid_confidences = [r.confidence for r in results if not r.error and r.confidence > 0]
    overall_conf = sum(valid_confidences) / len(valid_confidences) if valid_confidences else 0.0

    # Ensure at least placeholder factors
    if not bull_factors:
        bull_factors = ["Research bots loading — signals stabilising"]
    if not bear_factors:
        bear_factors = ["Monitor for emerging risk factors"]

    return BotResearch(
        ticker=ticker,
        signal_inputs=signal_inputs,
        bull_factors=bull_factors,
        bear_factors=bear_factors,
        bot_summaries=bot_summaries,
        bot_confidences=bot_confidences,
        sources=sources,
        overall_confidence=overall_conf,
        errors=errors,
    )


async def run_single_bot(bot_name: str, ticker: str, asset_meta: dict = None) -> Optional[BotResult]:
    """Run a specific bot by name."""
    bots = get_bots()
    for bot in bots:
        if bot.name == bot_name:
            return await bot.run(ticker, asset_meta or {})
    return None
