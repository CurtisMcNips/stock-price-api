"""
Market Brain — Asset Payload Model
────────────────────────────────────
Defines the canonical structure of a complete research result.
This is what /api/research returns and what Redis stores.
"""

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class BotStatus:
    name: str
    status: str          # "success" | "cached" | "fallback" | "skipped" | "failed"
    source: str          # e.g. "FMP", "Yahoo Finance", "GNews"
    data_age_s: int      # seconds since data was fetched
    error: Optional[str] = None


@dataclass
class DataFreshness:
    news:         Optional[str] = None   # "2h", "1d", etc.
    price:        Optional[str] = None
    fundamentals: Optional[str] = None
    analyst:      Optional[str] = None
    earnings:     Optional[str] = None
    technicals:   Optional[str] = None
    macro:        Optional[str] = None


@dataclass
class ResearchMeta:
    symbol:          str
    last_updated:    str           # ISO timestamp
    sweep_cycle:     str           # "morning" | "afternoon" | "evening" | "on_demand" | "fallback"
    freshness:       DataFreshness = field(default_factory=DataFreshness)
    bots:            Dict[str, str] = field(default_factory=dict)  # bot_name → status
    delta_detected:  bool = False
    stale_fields:    List[str] = field(default_factory=list)
    data_points:     int = 0
    bots_run:        int = 0
    sweep_duration_s: float = 0.0


@dataclass
class ResearchPayload:
    """
    The complete research result for one asset.
    Stored in Redis; served by /api/research.
    """
    symbol:      str
    data:        Dict[str, Any]   = field(default_factory=dict)
    meta:        Optional[ResearchMeta] = None
    bull_factors: List[str]       = field(default_factory=list)
    bear_factors: List[str]       = field(default_factory=list)
    signal_inputs: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ResearchPayload":
        meta_d = d.get("meta") or {}
        freshness = DataFreshness(**{
            k: v for k, v in (meta_d.get("freshness") or {}).items()
            if k in DataFreshness.__dataclass_fields__
        })
        meta = ResearchMeta(
            symbol=meta_d.get("symbol", d.get("symbol", "")),
            last_updated=meta_d.get("last_updated", ""),
            sweep_cycle=meta_d.get("sweep_cycle", "unknown"),
            freshness=freshness,
            bots=meta_d.get("bots", {}),
            delta_detected=meta_d.get("delta_detected", False),
            stale_fields=meta_d.get("stale_fields", []),
            data_points=meta_d.get("data_points", 0),
            bots_run=meta_d.get("bots_run", 0),
            sweep_duration_s=meta_d.get("sweep_duration_s", 0.0),
        )
        return cls(
            symbol=d.get("symbol", ""),
            data=d.get("data", {}),
            meta=meta,
            bull_factors=d.get("bull_factors", []),
            bear_factors=d.get("bear_factors", []),
            signal_inputs=d.get("signal_inputs", {}),
        )

    def age_seconds(self) -> int:
        """How old is this payload in seconds."""
        if not self.meta or not self.meta.last_updated:
            return 999999
        try:
            from datetime import datetime, timezone
            ts = datetime.fromisoformat(self.meta.last_updated.replace("Z", "+00:00"))
            return int((datetime.now(timezone.utc) - ts).total_seconds())
        except Exception:
            return 999999

    def is_stale(self, max_age_s: int) -> bool:
        return self.age_seconds() > max_age_s


def _fmt_age(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
