"""
Market Brain — 🤖 Bot Orchestrator
─────────────────────────────────────
Runs all research bots in parallel and merges their results into
a single unified BotResearch object that app.py can serve.

v2 change: after merging, posts a CatalystSignal to /api/cos/signal
so the Chief of Staff can build and track catalyst objects from real
research bot output. This only fires during scheduled sweeps — not
on user-triggered /api/research calls — to avoid signal spam.

Usage in app.py:
    from research_bots.orchestrator import run_all_bots
    result = await run_all_bots(ticker, asset_meta, post_to_cos=False)
    return result.to_dict()

Usage in sweep_engine.py:
    result = await run_all_bots(ticker, asset_meta, post_to_cos=True)
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx

from base import BotResult, cache_get, cache_set

log = logging.getLogger("mb.bots.orchestrator")

# ── CoS integration ────────────────────────────────────────────
MB_API_URL      = os.environ.get("MB_API_URL", "http://localhost:8000")
MB_BOT_EMAIL    = os.environ.get("MB_BOT_EMAIL", "sweeper@marketbrain.ai")
MB_BOT_PASSWORD = os.environ.get("MB_BOT_PASSWORD", "SweeperPass123!")

_cos_token: Optional[str] = None

async def _get_cos_token() -> Optional[str]:
    global _cos_token
    if _cos_token:
        return _cos_token
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Register is idempotent — 409 if already exists, that's fine
            await client.post(f"{MB_API_URL}/api/auth/register", json={
                "name": "Sweep Engine",
                "email": MB_BOT_EMAIL,
                "password": MB_BOT_PASSWORD,
            })
            r = await client.post(f"{MB_API_URL}/api/auth/login", json={
                "email": MB_BOT_EMAIL,
                "password": MB_BOT_PASSWORD,
            })
            if r.status_code == 200:
                _cos_token = r.json()["token"]
                log.info("Sweep engine authenticated with CoS")
                return _cos_token
    except Exception as e:
        log.warning(f"CoS auth failed: {e}")
    return None


def _derive_direction(signal_inputs: dict, bull_factors: list, bear_factors: list) -> str:
    """Derive bullish / bearish / neutral from merged signal inputs."""
    score = 0.0
    if "sentiment"    in signal_inputs: score += signal_inputs["sentiment"]    * 20
    if "catalystNews" in signal_inputs: score += signal_inputs["catalystNews"] * 15
    if "sectorFlow"   in signal_inputs: score += signal_inputs["sectorFlow"]   * 12
    if "insiderBuy"   in signal_inputs: score += (signal_inputs["insiderBuy"] - 0.5) * 20
    if "revGrowth"    in signal_inputs: score += min(15, signal_inputs["revGrowth"] / 8)
    if "earningsBeat" in signal_inputs: score += signal_inputs["earningsBeat"] * 0.3
    if "debtRatio"    in signal_inputs and signal_inputs["debtRatio"] > 2.0: score -= 10
    score += (len(bull_factors) - len(bear_factors)) * 2
    if score > 8:  return "bullish"
    if score < -8: return "bearish"
    return "neutral"


def _derive_strength(overall_confidence: float, signal_inputs: dict) -> float:
    """Map confidence + signal intensity to 0–100 strength for CoS."""
    base = overall_confidence * 65
    if abs(signal_inputs.get("sentiment",    0)) > 0.5: base += 8
    if abs(signal_inputs.get("catalystNews", 0)) > 0.5: base += 8
    if abs(signal_inputs.get("sectorFlow",   0)) > 0.4: base += 6
    if signal_inputs.get("insiderBuy", 0.5)     > 0.7:  base += 8
    if signal_inputs.get("revGrowth",  0)        > 30:   base += 5
    return round(min(95.0, max(10.0, base)), 1)


async def _post_cos_signal(ticker: str, asset_meta: dict, research: "BotResearch") -> None:
    """Post a CatalystSignal to /api/cos/signal. Fire-and-forget via create_task."""
    token = await _get_cos_token()
    if not token:
        log.warning(f"Skipping CoS signal for {ticker} — no auth token")
        return

    direction = _derive_direction(research.signal_inputs, research.bull_factors, research.bear_factors)
    strength  = _derive_strength(research.overall_confidence, research.signal_inputs)

    # Build summary from top factors
    top_bull = research.bull_factors[0] if research.bull_factors else ""
    top_bear = research.bear_factors[0] if research.bear_factors else ""
    if direction == "bullish" and top_bull:
        summary = f"{ticker}: {top_bull}"
    elif direction == "bearish" and top_bear:
        summary = f"{ticker}: {top_bear}"
    else:
        summary = f"{ticker}: {top_bull or top_bear or 'Sweep completed'}"
    summary = summary[:120]

    sector = asset_meta.get("sector", "")
    catalyst_type = "macro" if sector in ("ETF", "Forex", "Commodities") else "asset"
    tags = [s for s in [sector, asset_meta.get("sub")] if s]
    if direction != "neutral":
        tags.append(direction)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{MB_API_URL}/api/cos/signal",
                json={
                    "source":        "sweep_engine",
                    "asset":         ticker,
                    "sector":        sector or None,
                    "direction":     direction,
                    "strength":      strength,
                    "summary":       summary,
                    "tags":          tags[:5],
                    "catalyst_type": catalyst_type,
                },
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            if r.status_code == 200:
                resp = r.json()
                log.info(f"CoS: {ticker} → {resp.get('action')} | wave={resp.get('wave')} conf={resp.get('confidence')}")
            else:
                log.warning(f"CoS rejected {ticker}: {r.status_code}")
    except Exception as e:
        log.warning(f"CoS post failed for {ticker}: {e}")


# ── Lazy bot loader ────────────────────────────────────────────
def _load_bots():
    bots = []
    bot_classes = [
        ("bot_news",             "NewsBot"),
        ("bot_earnings",         "EarningsBot"),
        ("bot_macro",            "MacroBot"),
        ("bot_insider",          "InsiderBot"),
        ("bot_fundamentals",     "FundamentalsBot"),
        ("bot_technical_levels", "TechnicalLevelsBot"),
        ("bot_analyst",          "AnalystBot"),
        ("bot_geo",              "GeoBot"),
    ]
    for module_name, class_name in bot_classes:
        try:
            import importlib
            module    = importlib.import_module(module_name)
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


# ── Merge helpers ──────────────────────────────────────────────
def _merge_signal_inputs(results: List[BotResult]) -> Dict[str, float]:
    accumulators: Dict[str, list] = {}
    for result in results:
        if result.error or not result.signal_inputs:
            continue
        for key, value in result.signal_inputs.items():
            if value is None:
                continue
            accumulators.setdefault(key, []).append((value, result.confidence))
    merged = {}
    for key, entries in accumulators.items():
        total_conf = sum(c for _, c in entries)
        if total_conf == 0:
            merged[key] = entries[0][0]
        else:
            merged[key] = round(sum(v * c for v, c in entries) / total_conf, 3)
    return merged


def _merge_factors(results: List[BotResult], max_per_side: int = 5) -> tuple:
    sorted_results = sorted(results, key=lambda r: r.confidence, reverse=True)
    bull_factors, bear_factors = [], []
    seen_bull, seen_bear = set(), set()
    for result in sorted_results:
        if result.error:
            continue
        for f in result.bull_factors:
            k = f[:50].lower()
            if k not in seen_bull and len(bull_factors) < max_per_side:
                bull_factors.append(f); seen_bull.add(k)
        for f in result.bear_factors:
            k = f[:50].lower()
            if k not in seen_bear and len(bear_factors) < max_per_side:
                bear_factors.append(f); seen_bear.add(k)
    return bull_factors, bear_factors


# ── BotResearch dataclass ──────────────────────────────────────
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


# ── Public entry points ────────────────────────────────────────
async def run_all_bots(
    ticker:      str,
    asset_meta:  dict = None,
    bots:        list = None,
    timeout:     float = 15.0,
    post_to_cos: bool = False,   # True only from sweep_engine, never from /api/research
) -> BotResearch:
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

    tasks = [asyncio.create_task(bot.run(ticker, asset_meta)) for bot in bots]
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
        log.warning(f"Bot timed out for {ticker}")

    results = []
    for task in done:
        try:
            results.append(task.result())
        except Exception as e:
            log.error(f"Bot task error: {e}")

    signal_inputs              = _merge_signal_inputs(results)
    bull_factors, bear_factors = _merge_factors(results)
    bot_summaries              = {r.bot_name: r.summary    for r in results}
    bot_confidences            = {r.bot_name: r.confidence for r in results}
    sources                    = list({r.source for r in results if not r.error})
    errors                     = {r.bot_name: r.error for r in results if r.error}
    valid_conf                 = [r.confidence for r in results if not r.error and r.confidence > 0]
    overall_conf               = sum(valid_conf) / len(valid_conf) if valid_conf else 0.0

    if not bull_factors:
        bull_factors = ["Research bots loading — signals stabilising"]
    if not bear_factors:
        bear_factors = ["Monitor for emerging risk factors"]

    research = BotResearch(
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

    # Post to CoS only from scheduled sweeps, not user-triggered calls
    if post_to_cos and overall_conf > 0.25:
        asyncio.create_task(_post_cos_signal(ticker, asset_meta, research))

    return research


async def run_single_bot(bot_name: str, ticker: str, asset_meta: dict = None) -> Optional[BotResult]:
    bots = get_bots()
    for bot in bots:
        if bot.name == bot_name:
            return await bot.run(ticker, asset_meta or {})
    return None
