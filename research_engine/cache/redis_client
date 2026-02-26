"""
Market Brain — Redis Client
────────────────────────────
Thin wrapper providing typed get/set/delete for the research engine.
Sits on top of the app.py Redis connection — no separate connection pool.
Falls back to an in-process dict if Redis is unavailable.
"""

import json
import logging
import time
from typing import Any, Optional

log = logging.getLogger("mb.cache")

# In-process fallback when Redis is down
_memory: dict[str, dict] = {}


async def _rget(key: str) -> Optional[str]:
    """Import app-level rget at call time to avoid circular imports."""
    try:
        from app import rget
        return await rget(key)
    except Exception:
        return None


async def _rset(key: str, value: str, ttl: int):
    try:
        from app import rset
        await rset(key, value, ttl)
    except Exception:
        pass


async def cache_get(key: str) -> Optional[Any]:
    """Get a JSON value from Redis. Returns None on miss or error."""
    try:
        raw = await _rget(key)
        if raw:
            return json.loads(raw)
    except Exception as e:
        log.debug(f"cache_get miss {key}: {e}")

    # Memory fallback
    entry = _memory.get(key)
    if entry and time.time() < entry["expires"]:
        return entry["data"]
    return None


async def cache_set(key: str, data: Any, ttl: int) -> bool:
    """Store a JSON value in Redis with TTL. Also writes memory fallback."""
    try:
        raw = json.dumps(data, default=str)
        await _rset(key, raw, ttl)
        # Always keep memory mirror
        _memory[key] = {"data": data, "expires": time.time() + ttl}
        return True
    except Exception as e:
        log.warning(f"cache_set failed {key}: {e}")
        # At least write memory
        try:
            _memory[key] = {"data": data, "expires": time.time() + ttl}
            return True
        except Exception:
            return False


async def cache_delete(key: str):
    """Delete a key from Redis and memory."""
    try:
        from app import get_redis
        r = await get_redis()
        if r:
            await r.delete(key)
    except Exception:
        pass
    _memory.pop(key, None)


async def cache_exists(key: str) -> bool:
    """Check if a key exists and is not expired."""
    try:
        from app import get_redis
        r = await get_redis()
        if r:
            return bool(await r.exists(key))
    except Exception:
        pass
    entry = _memory.get(key)
    return entry is not None and time.time() < entry["expires"]


async def cache_ttl_remaining(key: str) -> Optional[int]:
    """Return seconds until key expires, or None if not found."""
    try:
        from app import get_redis
        r = await get_redis()
        if r:
            ttl = await r.ttl(key)
            return ttl if ttl > 0 else None
    except Exception:
        pass
    entry = _memory.get(key)
    if entry:
        remaining = int(entry["expires"] - time.time())
        return remaining if remaining > 0 else None
    return None


# ── Key builders ──────────────────────────────────────────────

def key_research(symbol: str) -> str:
    return f"research:v2:{symbol.upper()}"

def key_bot(symbol: str, bot_name: str) -> str:
    return f"bot:v2:{symbol.upper()}:{bot_name}"

def key_meta(symbol: str) -> str:
    return f"meta:v2:{symbol.upper()}"

def key_sweep_lock(symbol: str) -> str:
    return f"sweep_lock:{symbol.upper()}"
