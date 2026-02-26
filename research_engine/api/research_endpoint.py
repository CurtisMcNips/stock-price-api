"""
Market Brain — Research API Endpoint
──────────────────────────────────────
/api/research?symbol=XYZ

THIS ENDPOINT NEVER MAKES EXTERNAL API CALLS.
It reads from Redis only.
If cache miss → triggers a background sweep and returns a pending response.
The scheduler populates Redis proactively; this just serves it.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from research_engine.cache.redis_client import cache_get, key_research
from research_engine.cache.ttl_config import RESULT_TTL, TTL
from research_engine.models.asset_payload import ResearchPayload, _fmt_age
from research_engine.orchestrator.delta_detector import compute_stale_fields

log = logging.getLogger("mb.api.research")

# Track in-flight fallback sweeps to avoid duplicate triggers
_in_flight: set = set()


async def get_research_response(symbol: str, asset_meta: Optional[dict] = None) -> dict:
    """
    Main handler for /api/research.
    1. Check Redis for cached result
    2. If found and fresh → return immediately
    3. If stale or missing → trigger background sweep, return what we have
    4. If nothing at all → trigger sweep, return pending response
    """
    symbol = symbol.upper()

    # ── 1. Try Redis ──────────────────────────────────────────
    cached_raw = await cache_get(key_research(symbol))

    if cached_raw:
        try:
            payload = ResearchPayload.from_dict(cached_raw)
            age_s   = payload.age_seconds()

            # Annotate stale fields
            stale = compute_stale_fields(cached_raw, TTL)
            if payload.meta:
                payload.meta.stale_fields = stale

            result = payload.to_dict()
            result["_served_from"] = "cache"
            result["_age_s"] = age_s

            # If data is getting old, trigger background refresh
            if age_s > RESULT_TTL * 0.75 and symbol not in _in_flight:
                _trigger_background_sweep(symbol, asset_meta or {})
                result["_refreshing"] = True

            log.debug(f"{symbol}: served from cache (age={_fmt_age(age_s)})")
            return result

        except Exception as e:
            log.warning(f"{symbol}: cache parse error: {e}")

    # ── 2. Cache miss — trigger sweep and return pending ──────
    log.info(f"{symbol}: cache miss — triggering fallback sweep")

    if symbol not in _in_flight:
        _trigger_background_sweep(symbol, asset_meta or {})

    return _pending_response(symbol)


def _trigger_background_sweep(symbol: str, asset_meta: dict):
    """Trigger a background sweep without blocking the response."""
    if symbol in _in_flight:
        return

    _in_flight.add(symbol)

    async def _do():
        try:
            from research_engine.orchestrator.sweeper import sweep_asset
            meta = asset_meta or {"ticker": symbol, "sector": "Unknown", "quote_type": "EQUITY"}
            await sweep_asset(symbol, meta, force=False, cycle="on_demand")
        except Exception as e:
            log.error(f"Background sweep failed for {symbol}: {e}")
        finally:
            _in_flight.discard(symbol)

    asyncio.create_task(_do())


def _pending_response(symbol: str) -> dict:
    """Return a well-formed pending response when no cache exists yet."""
    return {
        "symbol":       symbol,
        "data":         {},
        "bull_factors": [],
        "bear_factors": [],
        "signal_inputs": {},
        "meta": {
            "symbol":         symbol,
            "last_updated":   None,
            "sweep_cycle":    "pending",
            "freshness":      {},
            "bots":           {},
            "delta_detected": False,
            "stale_fields":   [],
            "data_points":    0,
            "bots_run":       0,
            "sweep_duration_s": 0,
        },
        "_served_from": "pending",
        "_message":     "Research sweep triggered. Data will be available within 30 seconds.",
    }


async def record_view(symbol: str):
    """
    Record that a user viewed this asset.
    Promotes it in priority tiers for more frequent sweeps.
    """
    try:
        from research_engine.orchestrator.priority_tiers import priority_manager
        priority_manager.record_view(symbol.upper())
    except Exception:
        pass
