"""
Market Brain — TTL Configuration
─────────────────────────────────
Single source of truth for all cache durations.
Organised by data type — how fast the real world changes.
"""

# ── Per data-type TTL (seconds) ───────────────────────────────

TTL = {
    # Fast-changing — user needs this fresh
    "price":        4 * 3600,       # 4 hours
    "news":         2 * 3600,       # 2 hours  (GNews 100/day limit)
    "volume":       4 * 3600,       # 4 hours
    "sentiment":    2 * 3600,       # 2 hours
    "technicals":   4 * 3600,       # 4 hours (Polygon/Yahoo OHLCV)

    # Medium-changing — daily refresh is fine
    "fundamentals": 24 * 3600,      # 1 day    (FMP key metrics)
    "analyst":      24 * 3600,      # 1 day    (FMP ratings)
    "earnings":     24 * 3600,      # 1 day    (earnings calendar)

    # Slow-changing — monthly
    "macro":        30 * 24 * 3600, # 30 days  (FRED series)
}

# ── Full research result TTL (composite) ─────────────────────
# When a full asset sweep is stored, the outer envelope expires
# at the shortest TTL of its components (news/sentiment)
RESULT_TTL = TTL["news"]   # 2 hours — full result refreshes when news does

# ── Sweep schedule names ──────────────────────────────────────
SWEEP_CYCLES = {
    "morning":   "08:00",
    "afternoon": "13:00",
    "evening":   "18:00",
}

# ── Priority tier sweep frequencies ─────────────────────────
TIER_SWEEP_HOURS = {
    1: 4,   # Tier 1: every 4 hours
    2: 12,  # Tier 2: every 12 hours (twice/day)
    3: 168, # Tier 3: once a week
}
