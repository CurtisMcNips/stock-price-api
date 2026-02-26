"""
Market Brain — Rate Limiter
─────────────────────────────
Token-bucket rate limiter per API provider.
Prevents any sweep from blowing through daily quotas.

Limits enforced:
  GNews:         100 req/day  → 1 req per 14.4 minutes
  FMP:           300 req/day  → 1 req per 4.8 minutes  (free tier)
  Alpha Vantage:  25 req/day  → 1 req per 57.6 minutes
  Polygon:       unlimited on paid, 5 req/min on free
  FRED:          120 req/min  → effectively unlimited for our use
  Yahoo:         soft limits  → 5 req/min to be safe
"""

import asyncio
import logging
import time
from collections import defaultdict
from typing import Dict

log = logging.getLogger("mb.rate_limiter")


class TokenBucket:
    """Token bucket: refills at `rate` tokens/second up to `capacity`."""

    def __init__(self, capacity: float, rate: float):
        self.capacity  = capacity
        self.rate      = rate       # tokens per second
        self._tokens   = capacity
        self._last     = time.monotonic()
        self._lock     = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> float:
        """
        Acquire tokens. Returns wait time in seconds (0 if immediate).
        Blocks until tokens are available.
        """
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last   = now

            if self._tokens >= tokens:
                self._tokens -= tokens
                return 0.0

            # Need to wait
            wait = (tokens - self._tokens) / self.rate
            self._tokens = 0
            return wait

    async def wait(self, tokens: float = 1.0):
        """Acquire and sleep if needed."""
        wait = await self.acquire(tokens)
        if wait > 0:
            log.debug(f"Rate limit: sleeping {wait:.1f}s")
            await asyncio.sleep(wait)


# ── Provider configurations ───────────────────────────────────
# (capacity, rate_per_second)
# capacity = burst allowance; rate = sustained throughput

_PROVIDER_CONFIG: Dict[str, tuple] = {
    # GNews: 100/day = 1 per 864s. Allow burst of 3 at start.
    "gnews":         (3,   1 / 864),

    # FMP: 300/day = 1 per 288s. Allow burst of 5.
    "fmp":           (5,   1 / 288),

    # Alpha Vantage: 25/day = 1 per 3456s. Allow burst of 2.
    "alpha_vantage": (2,   1 / 3456),

    # Polygon: free tier 5/min = 1 per 12s. Allow burst of 5.
    "polygon":       (5,   1 / 12),

    # FRED: generous limits. 1 per 2s, burst 10.
    "fred":          (10,  0.5),

    # Yahoo: unofficial, be gentle. 1 per 3s, burst 5.
    "yahoo":         (5,   1 / 3),
}

# Singleton buckets
_buckets: Dict[str, TokenBucket] = {}


def get_bucket(provider: str) -> TokenBucket:
    if provider not in _buckets:
        cap, rate = _PROVIDER_CONFIG.get(provider, (5, 1 / 60))
        _buckets[provider] = TokenBucket(cap, rate)
    return _buckets[provider]


async def acquire(provider: str, tokens: float = 1.0):
    """Acquire rate limit slot for a provider. Sleeps if needed."""
    await get_bucket(provider).wait(tokens)


# ── Sweep-level concurrency limiter ──────────────────────────
# Prevents too many assets being swept simultaneously

class SweepLimiter:
    """Semaphore limiting parallel asset sweeps to avoid pile-on."""

    def __init__(self, max_concurrent: int = 3):
        self._sem = asyncio.Semaphore(max_concurrent)

    async def __aenter__(self):
        await self._sem.acquire()
        return self

    async def __aexit__(self, *args):
        self._sem.release()


# Global sweep limiter — only 3 assets swept at once
sweep_limiter = SweepLimiter(max_concurrent=3)
