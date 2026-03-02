"""
Market Brain — Relay Bot
─────────────────────────
The Relay Bot sits between raw bot outputs and the Chief of Staff.

Responsibilities:
  1. Accept signal packets from all research bots (including GeoBot)
  2. Cluster signals by asset / sector / theme within a time window
  3. Detect multi-source alignment (same direction, multiple bots)
  4. Score cluster strength — promotes clusters to CoS with enriched metadata
  5. Trigger Perplexity verification for high-strength clusters
  6. Detect cross-asset alignment (same sector, same direction, multiple assets)
  7. Route enriched clusters to CoS via /api/cos/signal
  8. Never post weak or single-source signals directly — must cluster first

This module runs inside sweep_engine and orchestrator.
It replaces the direct _post_cos_signal call with a smarter relay layer.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx

log = logging.getLogger("mb.relay")

MB_API_URL       = os.getenv("MB_API_URL", "http://localhost:8000")
PERPLEXITY_KEY   = os.environ.get("PERPLEXITY_API_KEY", "")
PERPLEXITY_URL   = "https://api.perplexity.ai/chat/completions"
CLUSTER_WINDOW   = 600      # 10 minutes — signals within this window cluster together
MIN_CLUSTER_CONF = 0.28     # minimum confidence to relay at all
PERPLEXITY_THRESHOLD = 0.62 # trigger Perplexity verification above this cluster strength
_relay_last_perp = 0.0      # rate limit Perplexity calls
PERPLEXITY_RATE  = 3.0      # minimum seconds between Perplexity calls

# ── Signal packet (what relay receives from orchestrator) ─────────

@dataclass
class RelaySignal:
    ticker:       str
    asset_meta:   dict
    source:       str        # bot name: NewsBot, GeoBot, MacroBot etc.
    direction:    str        # bullish / bearish / neutral
    strength:     float      # 0–100
    confidence:   float      # 0.0–1.0
    summary:      str
    tags:         List[str]
    signal_inputs: dict      # raw signal_inputs from bot
    bull_factors: List[str]
    bear_factors: List[str]
    ts:           float = field(default_factory=time.time)

@dataclass
class RelayCluster:
    cluster_key:  str        # "{asset_or_sector}:{direction}"
    ticker:       str
    sector:       str
    direction:    str
    signals:      List[RelaySignal] = field(default_factory=list)
    sources:      List[str]  = field(default_factory=list)
    created_at:   float      = field(default_factory=time.time)
    updated_at:   float      = field(default_factory=time.time)
    verified:     bool       = False
    perp_score:   float      = 0.0
    perp_context: str        = ""
    cross_asset_aligned: bool = False

    @property
    def strength(self) -> float:
        """Weighted cluster strength — more sources = higher strength."""
        if not self.signals:
            return 0.0
        base = sum(s.strength * s.confidence for s in self.signals) / len(self.signals)
        # Source diversity bonus
        unique_sources = len(set(s.source for s in self.signals))
        if unique_sources >= 3: base = min(95, base * 1.15)
        if unique_sources >= 5: base = min(95, base * 1.10)
        # GeoBot signals carry extra weight for geo-sensitive sectors
        has_geo = any(s.source == "GeoBot" for s in self.signals)
        if has_geo: base = min(95, base * 1.08)
        return round(base, 1)

    @property
    def confidence(self) -> float:
        if not self.signals: return 0.0
        return round(sum(s.confidence for s in self.signals) / len(self.signals), 3)

    @property
    def summary(self) -> str:
        """Best summary from highest-confidence signal."""
        best = max(self.signals, key=lambda s: s.confidence)
        return best.summary[:120]

    @property
    def alignment_level(self) -> str:
        n = len(set(s.source for s in self.signals))
        if n >= 5: return "strong"
        if n >= 3: return "medium"
        if n >= 2: return "weak"
        return "single"

    @property
    def catalyst_type(self) -> str:
        geo_sectors = {"Shipping", "Defence", "Energy", "Geopolitical", "Commodities"}
        if self.sector in geo_sectors: return "geo"
        macro_sectors = {"ETF", "Forex", "Macro"}
        if self.sector in macro_sectors: return "macro"
        return "asset"

    @property
    def all_bull_factors(self) -> List[str]:
        seen, out = set(), []
        for s in sorted(self.signals, key=lambda x: x.confidence, reverse=True):
            for f in s.bull_factors:
                if f not in seen:
                    seen.add(f); out.append(f)
                if len(out) >= 5: return out
        return out

    @property
    def all_bear_factors(self) -> List[str]:
        seen, out = set(), []
        for s in sorted(self.signals, key=lambda x: x.confidence, reverse=True):
            for f in s.bear_factors:
                if f not in seen:
                    seen.add(f); out.append(f)
                if len(out) >= 5: return out
        return out

    @property
    def tags(self) -> List[str]:
        tags = set()
        for s in self.signals:
            tags.update(s.tags)
        tags.discard("")
        return list(tags)[:6]


# ── In-memory cluster store ───────────────────────────────────────
_clusters: Dict[str, RelayCluster] = {}


def _cluster_key(ticker: str, direction: str) -> str:
    return f"{ticker.upper()}:{direction}"


def _find_or_create_cluster(signal: RelaySignal) -> RelayCluster:
    key = _cluster_key(signal.ticker, signal.direction)
    now = time.time()

    # Expire old clusters
    for k in list(_clusters.keys()):
        if now - _clusters[k].updated_at > CLUSTER_WINDOW * 3:
            del _clusters[k]

    if key in _clusters:
        cluster = _clusters[key]
        # Only extend if within cluster window
        if now - cluster.updated_at <= CLUSTER_WINDOW:
            cluster.signals.append(signal)
            cluster.sources = list(set(cluster.sources + [signal.source]))
            cluster.updated_at = now
            return cluster

    # New cluster
    cluster = RelayCluster(
        cluster_key=key,
        ticker=signal.ticker,
        sector=signal.asset_meta.get("sector", ""),
        direction=signal.direction,
        signals=[signal],
        sources=[signal.source],
    )
    _clusters[key] = cluster
    return cluster


async def _perplexity_verify_cluster(cluster: RelayCluster) -> Tuple[float, str]:
    """Call Perplexity to verify a high-strength cluster. Returns (score, context)."""
    global _relay_last_perp
    if not PERPLEXITY_KEY:
        return 0.0, ""

    elapsed = time.time() - _relay_last_perp
    if elapsed < PERPLEXITY_RATE:
        await asyncio.sleep(PERPLEXITY_RATE - elapsed)

    prompt = (
        f"Verify this market signal cluster and rate its credibility:\n\n"
        f"Asset: {cluster.ticker}\n"
        f"Sector: {cluster.sector}\n"
        f"Direction: {cluster.direction}\n"
        f"Signal sources: {', '.join(set(cluster.sources))}\n"
        f"Summary: {cluster.summary}\n"
        f"Key factors: {'; '.join(cluster.all_bull_factors[:3])}\n\n"
        f"Respond ONLY in JSON:\n"
        f'{{"verified": true/false, "confidence_score": 0-100, '
        f'"sentiment": "bullish"|"bearish"|"neutral", '
        f'"reliability": "high"|"medium"|"low", '
        f'"geopolitical_context": "brief if relevant or empty string", '
        f'"reasoning": "1-2 sentences max"}}'
    )

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                PERPLEXITY_URL,
                headers={"Authorization": f"Bearer {PERPLEXITY_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "llama-3.1-sonar-large-128k-online",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 300,
                    "temperature": 0.1,
                },
            )
            _relay_last_perp = time.time()
            if r.status_code != 200:
                return 0.0, ""
            text = r.json().get("choices", [{}])[0].get("message", {}).get("content", "{}")
            text = text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            result = json.loads(text)
            score   = float(result.get("confidence_score", 50)) / 100
            context = result.get("reasoning", "") or result.get("geopolitical_context", "")
            return round(score, 3), context[:200]
    except Exception as e:
        log.warning(f"Relay Perplexity error: {e}")
        return 0.0, ""


def _detect_cross_asset_alignment() -> Dict[str, List[str]]:
    """
    Scan all active clusters for cross-asset alignment.
    Returns {sector:direction -> [tickers]} for groups with 3+ aligned assets.
    """
    sector_groups: Dict[str, List[str]] = {}
    for cluster in _clusters.values():
        if cluster.strength < 40:
            continue
        key = f"{cluster.sector}:{cluster.direction}"
        sector_groups.setdefault(key, [])
        if cluster.ticker not in sector_groups[key]:
            sector_groups[key].append(cluster.ticker)

    return {k: v for k, v in sector_groups.items() if len(v) >= 3}


async def relay_signal(
    ticker: str,
    asset_meta: dict,
    research,          # BotResearch from orchestrator
    cos_token: str,
    direction: str,
    strength: float,
    summary: str,
    tags: List[str],
) -> Optional[dict]:
    """
    Main relay entry point. Called instead of _post_cos_signal.

    Clusters the signal, checks alignment, optionally verifies with Perplexity,
    then posts an enriched signal to CoS.

    Returns the CoS response dict or None if signal was too weak / not relayed.
    """

    # Build relay signal
    sig = RelaySignal(
        ticker=ticker,
        asset_meta=asset_meta,
        source=research.sources[0] if research.sources else "sweep_engine",
        direction=direction,
        strength=strength,
        confidence=research.overall_confidence,
        summary=summary,
        tags=tags,
        signal_inputs=research.signal_inputs,
        bull_factors=research.bull_factors[:5],
        bear_factors=research.bear_factors[:5],
    )

    # Assign per-bot sources more accurately
    sig.source = "+".join(sorted(set(
        b for b in research.bot_summaries.keys()
        if not research.errors.get(b)
    )))[:40] or "sweep_engine"

    cluster = _find_or_create_cluster(sig)

    # Too weak — don't relay yet
    if cluster.strength < MIN_CLUSTER_CONF * 100:
        log.debug(f"Relay: {ticker} cluster too weak ({cluster.strength:.0f}) — holding")
        return None

    # Perplexity verification for strong clusters
    if cluster.strength >= PERPLEXITY_THRESHOLD * 100 and not cluster.verified:
        perp_score, perp_context = await _perplexity_verify_cluster(cluster)
        if perp_score > 0:
            cluster.verified     = perp_score > 0.55
            cluster.perp_score   = perp_score
            cluster.perp_context = perp_context
            # Boost strength if verified
            if cluster.verified:
                for s in cluster.signals:
                    s.strength = min(95, s.strength * (1 + perp_score * 0.2))
            log.info(f"Relay Perplexity [{ticker}]: score={perp_score:.2f} verified={cluster.verified}")

    # Cross-asset alignment detection
    aligned = _detect_cross_asset_alignment()
    sector_dir_key = f"{cluster.sector}:{cluster.direction}"
    if sector_dir_key in aligned:
        cluster.cross_asset_aligned = True
        aligned_assets = aligned[sector_dir_key]
        log.info(f"Relay: Cross-asset alignment detected — {cluster.sector} {cluster.direction}: {aligned_assets}")

    # Build enriched summary for CoS
    enriched_summary = cluster.summary
    if cluster.cross_asset_aligned:
        n = len(aligned.get(sector_dir_key, []))
        enriched_summary = f"[CROSS-ASSET x{n}] {enriched_summary}"
    if cluster.verified and cluster.perp_context:
        enriched_summary = f"{enriched_summary} | Verified: {cluster.perp_context[:80]}"

    # Build enriched tags
    enriched_tags = list(set(cluster.tags + [
        cluster.alignment_level,
        cluster.catalyst_type,
        *([f"verified"] if cluster.verified else []),
        *([f"cross_asset"] if cluster.cross_asset_aligned else []),
        *([f"geo"] if any(s.source == "GeoBot" for s in cluster.signals) else []),
    ]))[:8]

    # Determine catalyst type for CoS expiry
    cat_type_map = {"geo": "geo", "macro": "macro", "asset": "asset"}
    cos_catalyst_type = cat_type_map.get(cluster.catalyst_type, "asset")
    if cluster.sector in ("Geopolitical",): cos_catalyst_type = "geo"

    # Post to CoS
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.post(
                f"{MB_API_URL}/api/cos/signal",
                json={
                    "source":        "relay_bot",
                    "asset":         ticker,
                    "sector":        cluster.sector or None,
                    "direction":     cluster.direction,
                    "strength":      round(cluster.strength, 1),
                    "summary":       enriched_summary[:200],
                    "tags":          enriched_tags,
                    "catalyst_type": cos_catalyst_type,
                },
                headers={"Authorization": f"Bearer {cos_token}", "Content-Type": "application/json"},
            )
            if r.status_code == 200:
                resp = r.json()
                log.info(
                    f"Relay → CoS: {ticker} | {cluster.direction} | "
                    f"strength={cluster.strength:.0f} | wave={resp.get('wave')} | "
                    f"conf={resp.get('confidence')} | sources={len(cluster.sources)} | "
                    f"verified={cluster.verified} | cross_asset={cluster.cross_asset_aligned}"
                )
                return resp
            else:
                log.warning(f"CoS rejected relay for {ticker}: {r.status_code}")
    except Exception as e:
        log.warning(f"Relay CoS post failed for {ticker}: {e}")

    return None


def get_cluster_status() -> dict:
    """Returns current cluster state for diagnostics."""
    return {
        "active_clusters": len(_clusters),
        "clusters": [
            {
                "key":           c.cluster_key,
                "ticker":        c.ticker,
                "sector":        c.sector,
                "direction":     c.direction,
                "strength":      c.strength,
                "sources":       len(c.sources),
                "alignment":     c.alignment_level,
                "verified":      c.verified,
                "cross_asset":   c.cross_asset_aligned,
                "age_mins":      round((time.time() - c.created_at) / 60, 1),
            }
            for c in sorted(_clusters.values(), key=lambda x: x.strength, reverse=True)
        ],
    }
