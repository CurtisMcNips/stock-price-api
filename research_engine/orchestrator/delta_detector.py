"""
Market Brain — Delta Detector
───────────────────────────────
Compares new bot results against cached results.
Decides whether data has meaningfully changed.
Prevents unnecessary Redis writes (and downstream noise).
"""

import logging
from typing import Any, Optional

log = logging.getLogger("mb.delta")


# ── Significance thresholds ────────────────────────────────────
# If a numeric value changes by less than this fraction, it's not significant
NUMERIC_THRESHOLD = 0.02   # 2% change = significant

# Fields where ANY change is significant
ALWAYS_SIGNIFICANT = {"earnings_date", "consensus", "golden_cross", "death_cross"}

# Fields to ignore entirely (noise)
IGNORE_FIELDS = {"_ts", "source", "data_age_s"}


def _is_significant_change(old: Any, new: Any, key: str = "") -> bool:
    """Return True if old→new is a meaningful change worth recording."""
    if key in IGNORE_FIELDS:
        return False
    if key in ALWAYS_SIGNIFICANT:
        return str(old) != str(new)
    if old is None and new is None:
        return False
    if old is None or new is None:
        return True   # appeared or disappeared

    # Numeric: check relative change
    try:
        old_f, new_f = float(old), float(new)
        if old_f == 0:
            return new_f != 0
        rel = abs(new_f - old_f) / abs(old_f)
        return rel >= NUMERIC_THRESHOLD
    except (TypeError, ValueError):
        pass

    # List: compare as sets of strings (e.g. bull/bear factors)
    if isinstance(old, list) and isinstance(new, list):
        return set(str(x) for x in old) != set(str(x) for x in new)

    # Dict: recurse
    if isinstance(old, dict) and isinstance(new, dict):
        return detect_delta(old, new)

    return str(old) != str(new)


def detect_delta(old_data: Optional[dict], new_data: dict) -> bool:
    """
    Returns True if new_data is meaningfully different from old_data.
    Used to decide whether to update Redis and log a change.
    """
    if old_data is None:
        return True   # first time — always write

    changed_keys = []
    all_keys = set(old_data.keys()) | set(new_data.keys())

    for key in all_keys:
        if key in IGNORE_FIELDS:
            continue
        old_val = old_data.get(key)
        new_val = new_data.get(key)
        if _is_significant_change(old_val, new_val, key):
            changed_keys.append(key)

    if changed_keys:
        log.debug(f"Delta detected in keys: {changed_keys[:5]}")
        return True
    return False


def compute_stale_fields(cached_result: dict, ttl_map: dict) -> list:
    """
    Given a cached full research result and the TTL map,
    return the list of field names that are past their TTL.
    Used to populate meta.stale_fields in responses.
    """
    import time
    from datetime import datetime, timezone

    stale = []
    data = cached_result.get("data", {})
    meta = cached_result.get("meta", {})

    # Check each data section's last_updated vs its TTL
    for field_name, ttl_s in ttl_map.items():
        section = data.get(field_name, {})
        if not section:
            continue
        ts_str = section.get("_fetched_at") or meta.get("last_updated")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > ttl_s:
                stale.append(field_name)
        except Exception:
            continue

    return stale
