"""
Market Brain — Asset Sweeper
──────────────────────────────
Core sweep engine. Given one asset, runs all relevant bots,
assembles the result, detects deltas, and stores in Redis.

This is the only place external API calls are made.
The /api/research endpoint reads; this writes.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from research_engine.cache.redis_client import (
    cache_get, cache_set, cache_exists, key_research, key_bot, key_meta
)
from research_engine.cache.ttl_config import TTL, RESULT_TTL
from research_engine.orchestrator.delta_detector import detect_delta
from research_engine.orchestrator.rate_limiter import acquire, sweep_limiter
from research_engine.models.asset_payload import (
    ResearchPayload, ResearchMeta, DataFreshness, BotStatus, _fmt_age
)

log = logging.getLogger("mb.sweeper")

# ── Bot → provider mapping (for rate limiting) ────────────────
BOT_PROVIDERS = {
    "MacroBot":           ["fred", "yahoo"],
    "FundamentalsBot":    ["fmp", "yahoo"],
    "AnalystBot":         ["fmp", "yahoo"],
    "EarningsBot":        ["fmp", "yahoo", "alpha_vantage"],
    "NewsBot":            ["gnews"],
    "TechnicalLevelsBot": ["polygon", "yahoo"],
}

# ── Which bots apply to which asset types ────────────────────
ASSET_TYPE_BOTS = {
    "stock":       ["MacroBot", "FundamentalsBot", "AnalystBot", "EarningsBot", "NewsBot", "TechnicalLevelsBot"],
    "etf":         ["MacroBot", "NewsBot", "TechnicalLevelsBot"],
    "crypto":      ["MacroBot", "NewsBot", "TechnicalLevelsBot"],
    "forex":       ["MacroBot", "TechnicalLevelsBot"],
    "commodity":   ["MacroBot", "TechnicalLevelsBot"],
}


def _get_sweep_cycle() -> str:
    hour = datetime.now(timezone.utc).hour
    if 6 <= hour < 11:
        return "morning"
    elif 11 <= hour < 16:
        return "afternoon"
    elif 16 <= hour < 22:
        return "evening"
    return "overnight"


def _get_asset_type(asset_meta: dict) -> str:
    quote_type = (asset_meta.get("quote_type") or "").upper()
    sector     = (asset_meta.get("sector") or "").lower()
    ticker     = asset_meta.get("ticker", "")

    if quote_type == "CRYPTOCURRENCY" or "-USD" in ticker:
        return "crypto"
    if quote_type == "FOREX" or "=X" in ticker:
        return "forex"
    if quote_type == "ETF":
        return "etf"
    if sector in ("crypto",):
        return "crypto"
    if sector in ("forex",):
        return "forex"
    return "stock"


async def _run_single_bot(bot_name: str, ticker: str, asset_meta: dict) -> Tuple[str, Optional[Any], str, str]:
    """
    Run one bot. Acquires rate limit slots first.
    Returns (bot_name, result_or_none, status, source).
    """
    try:
        # Acquire rate limit for each provider this bot uses
        providers = BOT_PROVIDERS.get(bot_name, [])
        for provider in providers:
            await acquire(provider)

        # Import and instantiate bot
        from research_bots.orchestrator import get_bots
        bots_by_name = {b.name: b for b in get_bots()}
        bot = bots_by_name.get(bot_name)
        if not bot:
            return (bot_name, None, "skipped", "not_found")

        result = await bot._fetch(ticker, asset_meta)
        return (bot_name, result, "success", result.source if result else "unknown")

    except Exception as e:
        log.warning(f"Bot {bot_name} failed for {ticker}: {e}")
        return (bot_name, None, "failed", str(e)[:80])


async def sweep_asset(
    symbol: str,
    asset_meta: dict,
    force: bool = False,
    cycle: str = "on_demand",
    priority_bots: Optional[List[str]] = None,
    bots_override: Optional[List[str]] = None,
) -> Optional[ResearchPayload]:
    """
    Perform a research sweep for one asset.

    Args:
        symbol:        Ticker symbol
        asset_meta:    Dict with sector, quote_type, name, etc.
        force:         Ignore cache TTLs and re-run all bots
        cycle:         Sweep cycle name for metadata
        priority_bots: Run these bots first (e.g. ["NewsBot","MacroBot"] at open)
                       Remaining relevant bots still run after, respecting TTL.
        bots_override: Run ONLY these bots — skip all others entirely.
                       Use for fast open/close sweeps that don't need fundamentals.
    """
    symbol = symbol.upper()
    t_start = time.monotonic()

    async with sweep_limiter:
        return await _do_sweep(
            symbol, asset_meta, force, cycle, t_start,
            priority_bots=priority_bots,
            bots_override=bots_override,
        )


async def _do_sweep(symbol, asset_meta, force, cycle, t_start,
                    priority_bots=None, bots_override=None):
    log.info(f"Sweeping {symbol} (cycle={cycle}, force={force})")

    asset_type    = _get_asset_type(asset_meta)
    relevant_bots = ASSET_TYPE_BOTS.get(asset_type, ASSET_TYPE_BOTS["stock"])

    # Inject derived asset_type into meta so bots can use it directly.
    # Universe stores "quote_type" (e.g. "CRYPTOCURRENCY", "ETF") but bots
    # check asset_meta.get("asset_type") which maps to our internal type names.
    # Without this, crypto/forex/etf assets get treated as stocks by bots.
    asset_meta = {**asset_meta, "asset_type": asset_type}

    # bots_override: run ONLY these (ignore relevance and TTL)
    if bots_override:
        relevant_bots = [b for b in bots_override if b in relevant_bots]
        force = True   # always fresh when overriding

    # priority_bots: run these first, then the rest in normal order
    if priority_bots:
        prioritised = [b for b in priority_bots if b in relevant_bots]
        rest        = [b for b in relevant_bots if b not in priority_bots]
        relevant_bots = prioritised + rest

    # Load previous result for delta detection
    prev_key  = key_research(symbol)
    prev_raw  = await cache_get(prev_key)
    prev_data = prev_raw.get("data", {}) if prev_raw else None

    # Determine which bots actually need running
    bots_to_run = []
    bots_cached = {}

    for bot_name in relevant_bots:
        bot_key = key_bot(symbol, bot_name)
        if not force and await cache_exists(bot_key):
            cached = await cache_get(bot_key)
            bots_cached[bot_name] = cached
            log.debug(f"{symbol}/{bot_name}: cached, skipping")
        else:
            bots_to_run.append(bot_name)

    log.info(f"{symbol}: running {len(bots_to_run)} bots, {len(bots_cached)} cached")

    # Run needed bots (with concurrency = all at once per asset, but rate-limited per provider)
    bot_tasks = [
        _run_single_bot(bot_name, symbol, asset_meta)
        for bot_name in bots_to_run
    ]
    fresh_results = await asyncio.gather(*bot_tasks, return_exceptions=True)

    # Assemble data sections
    now_iso      = datetime.now(timezone.utc).isoformat()
    data         = {}
    bull_factors = []
    bear_factors = []
    signal_inputs = {}
    bot_statuses: Dict[str, str] = {}
    bots_run_count = 0
    data_points    = 0

    # Process fresh bot results
    for item in fresh_results:
        if isinstance(item, Exception):
            log.error(f"Bot task exception for {symbol}: {item}")
            continue

        bot_name, result, status, source = item

        if result is None:
            bot_statuses[bot_name] = "failed"
            continue

        bots_run_count += 1
        bot_statuses[bot_name] = status

        # Merge into data sections
        section_name = _bot_to_section(bot_name)
        section_data = {
            **(result.raw or {}),
            "_fetched_at": now_iso,
            "_source":     source,
        }
        data[section_name] = section_data

        # Accumulate factors and signals
        bull_factors.extend(result.bull_factors or [])
        bear_factors.extend(result.bear_factors or [])
        if result.signal_inputs:
            signal_inputs.update(result.signal_inputs)
            data_points += len(result.signal_inputs)

        # Cache individual bot result
        bot_ttl = _bot_ttl(bot_name)
        await cache_set(key_bot(symbol, bot_name), section_data, bot_ttl)

    # Merge cached bot data for bots we skipped
    for bot_name, cached_data in bots_cached.items():
        bot_statuses[bot_name] = "cached"
        section_name = _bot_to_section(bot_name)
        if cached_data:
            data[section_name] = cached_data
            data_points += len([k for k in cached_data if not k.startswith("_")])

    # Delta detection
    new_signals_snapshot = {k: signal_inputs.get(k) for k in signal_inputs}
    delta = detect_delta(prev_data, data) if prev_data else True

    # Deduplicate and trim factors
    bull_factors = _dedup(bull_factors)[:6]
    bear_factors = _dedup(bear_factors)[:6]

    # Build freshness map
    freshness = DataFreshness(
        news         = _fmt_age(TTL["news"])         if "news"         in data else None,
        price        = _fmt_age(TTL["price"])         if "price"        in data else None,
        fundamentals = _fmt_age(TTL["fundamentals"])  if "fundamentals" in data else None,
        analyst      = _fmt_age(TTL["analyst"])       if "analyst"      in data else None,
        earnings     = _fmt_age(TTL["earnings"])      if "earnings"     in data else None,
        technicals   = _fmt_age(TTL["technicals"])    if "technicals"   in data else None,
        macro        = _fmt_age(TTL["macro"])         if "macro"        in data else None,
    )

    duration = round(time.monotonic() - t_start, 2)

    meta = ResearchMeta(
        symbol           = symbol,
        last_updated     = now_iso,
        sweep_cycle      = cycle if cycle != "on_demand" else _get_sweep_cycle(),
        freshness        = freshness,
        bots             = bot_statuses,
        delta_detected   = delta,
        stale_fields     = [],
        data_points      = data_points,
        bots_run         = bots_run_count,
        sweep_duration_s = duration,
    )

    payload = ResearchPayload(
        symbol        = symbol,
        data          = data,
        meta          = meta,
        bull_factors  = bull_factors,
        bear_factors  = bear_factors,
        signal_inputs = signal_inputs,
    )

    # Write to Redis
    payload_dict = payload.to_dict()
    await cache_set(prev_key, payload_dict, RESULT_TTL)

    if delta:
        log.info(f"{symbol}: sweep complete in {duration}s — delta detected, Redis updated")
    else:
        log.info(f"{symbol}: sweep complete in {duration}s — no significant delta")

    return payload


def _bot_to_section(bot_name: str) -> str:
    mapping = {
        "MacroBot":           "macro",
        "FundamentalsBot":    "fundamentals",
        "AnalystBot":         "analyst",
        "EarningsBot":        "earnings",
        "NewsBot":            "news",
        "TechnicalLevelsBot": "technicals",
    }
    return mapping.get(bot_name, bot_name.lower())


def _bot_ttl(bot_name: str) -> int:
    mapping = {
        "MacroBot":           TTL["macro"],
        "FundamentalsBot":    TTL["fundamentals"],
        "AnalystBot":         TTL["analyst"],
        "EarningsBot":        TTL["earnings"],
        "NewsBot":            TTL["news"],
        "TechnicalLevelsBot": TTL["technicals"],
    }
    return mapping.get(bot_name, TTL["fundamentals"])


def _dedup(factors: list) -> list:
    seen = set()
    out  = []
    for f in factors:
        key = f[:40].lower()
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out
