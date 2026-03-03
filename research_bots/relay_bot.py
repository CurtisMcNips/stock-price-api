"""
Market Brain — Relay Bot
─────────────────────────
The Relay Bot sits between raw bot outputs and the Chief of Staff.

Responsibilities:
  1. Accept signal packets from all research bots (including GeoBot)
  2. Cluster signals by asset / sector / theme within a time window
  3. Detect multi-source alignment (same direction, multiple bots)
  4. Score cluster strength — promotes clusters to CoS with enriched metadata
  5. Trigger Perplexity hypothesis-testing for high-strength clusters
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
PERPLEXITY_MODEL = "sonar"                # current model name as of 2025
CLUSTER_WINDOW   = 600                   # 10 minutes
MIN_CLUSTER_CONF = 0.28
PERPLEXITY_THRESHOLD = 0.62              # trigger above this cluster strength
_relay_last_perp = 0.0
PERPLEXITY_RATE  = 3.0                   # minimum seconds between calls

# ── Signal packet ─────────────────────────────────────────────────

@dataclass
class RelaySignal:
    ticker:        str
    asset_meta:    dict
    source:        str
    direction:     str
    strength:      float
    confidence:    float
    summary:       str
    tags:          List[str]
    signal_inputs: dict
    bull_factors:  List[str]
    bear_factors:  List[str]
    ts:            float = field(default_factory=time.time)

@dataclass
class RelayCluster:
    cluster_key:          str
    ticker:               str
    sector:               str
    direction:            str
    signals:              List[RelaySignal] = field(default_factory=list)
    sources:              List[str]         = field(default_factory=list)
    created_at:           float             = field(default_factory=time.time)
    updated_at:           float             = field(default_factory=time.time)
    verified:             bool              = False
    perp_verdict:         str               = ""   # supported|mixed|weak|contradicted
    perp_score:           float             = 0.0  # -1.0 to +1.0
    perp_justification:   str               = ""
    perp_key_risks:       List[str]         = field(default_factory=list)
    perp_extra_sources:   List[str]         = field(default_factory=list)
    perp_audit_notes:     str               = ""
    cross_asset_aligned:  bool              = False

    @property
    def strength(self) -> float:
        if not self.signals:
            return 0.0
        base = sum(s.strength * s.confidence for s in self.signals) / len(self.signals)
        unique_sources = len(set(s.source for s in self.signals))
        if unique_sources >= 3: base = min(95, base * 1.15)
        if unique_sources >= 5: base = min(95, base * 1.10)
        has_geo = any(s.source == "GeoBot" for s in self.signals)
        if has_geo: base = min(95, base * 1.08)
        return round(base, 1)

    @property
    def confidence(self) -> float:
        if not self.signals: return 0.0
        return round(sum(s.confidence for s in self.signals) / len(self.signals), 3)

    @property
    def summary(self) -> str:
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
        geo_sectors  = {"Shipping", "Defence", "Energy", "Geopolitical", "Commodities"}
        macro_sectors = {"ETF", "Forex", "Macro"}
        if self.sector in geo_sectors:  return "geo"
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

    for k in list(_clusters.keys()):
        if now - _clusters[k].updated_at > CLUSTER_WINDOW * 3:
            del _clusters[k]

    if key in _clusters:
        cluster = _clusters[key]
        if now - cluster.updated_at <= CLUSTER_WINDOW:
            cluster.signals.append(signal)
            cluster.sources = list(set(cluster.sources + [signal.source]))
            cluster.updated_at = now
            return cluster

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


# ── Perplexity hypothesis-testing ────────────────────────────────
# Per spec:
#   - Send a hypothesis, not raw signal text
#   - Require structured verdict: verification_score (-1 to +1),
#     verdict (supported/mixed/weak/contradicted), justification,
#     key_risks, extra_sources, audit_notes
#   - Apply: confidence = clamp(confidence + verification_score * 20, 0, 100)

def _build_hypothesis(cluster: RelayCluster) -> str:
    """
    Convert a cluster into a testable hypothesis for Perplexity.
    Framed as a statement to verify, not a question to answer.
    """
    direction_phrase = {
        "bullish": "structurally bullish for",
        "bearish": "structurally bearish for",
        "neutral": "creating mixed conditions for",
    }.get(cluster.direction, "affecting")

    # Build a timeframe based on wave
    timeframes = {
        "single": "1-4 week",
        "weak":   "1-4 week",
        "medium": "1-6 month",
        "strong": "3-12 month",
    }
    timeframe = timeframes.get(cluster.alignment_level, "1-6 month")

    key_drivers = "; ".join(cluster.all_bull_factors[:2] or cluster.all_bear_factors[:2])
    sources_str = ", ".join(set(cluster.sources))[:80]

    hypothesis = (
        f"Hypothesis: Current conditions are {direction_phrase} "
        f"{cluster.sector} equities (specifically {cluster.ticker}) "
        f"over a {timeframe} timeframe. "
        f"Key drivers cited: {key_drivers or cluster.summary}. "
        f"Signal sources: {sources_str}."
    )
    return hypothesis


async def _perplexity_verify_cluster(cluster: RelayCluster) -> Tuple[float, str, dict]:
    """
    Submit a hypothesis to Perplexity and parse the structured verdict.

    Returns:
        (verification_score, verdict_label, full_result_dict)
        verification_score is -1.0 to +1.0 (maps to confidence delta)
    """
    global _relay_last_perp
    if not PERPLEXITY_KEY:
        return 0.0, "", {}

    elapsed = time.time() - _relay_last_perp
    if elapsed < PERPLEXITY_RATE:
        await asyncio.sleep(PERPLEXITY_RATE - elapsed)

    hypothesis = _build_hypothesis(cluster)

    system_prompt = (
        "You are a financial verification engine. "
        "Your role is to test a market hypothesis against current real-world information. "
        "Search for recent news, data, and events relevant to the hypothesis. "
        "Be rigorous — look for both supporting and contradicting evidence. "
        "Respond ONLY in valid JSON. No markdown, no preamble, no explanation outside the JSON."
    )

    user_prompt = (
        f"Test this hypothesis:\n\n{hypothesis}\n\n"
        f"Search for current evidence and respond ONLY in this exact JSON format:\n"
        f'{{'
        f'"verification_score": <float from -1.0 (fully contradicted) to +1.0 (fully supported)>,'
        f'"verdict": "<supported|mixed|weak|contradicted>",'
        f'"justification": "<2-3 sentences citing specific evidence you found>",'
        f'"key_risks": ["<risk 1>", "<risk 2>", "<risk 3>"],'
        f'"extra_sources": ["<source or headline 1>", "<source or headline 2>"],'
        f'"audit_notes": "<any important caveats or data gaps>"'
        f'}}'
    )

    try:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.post(
                PERPLEXITY_URL,
                headers={
                    "Authorization": f"Bearer {PERPLEXITY_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model":       PERPLEXITY_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                    "max_tokens":  600,
                    "temperature": 0.1,
                },
            )
            _relay_last_perp = time.time()

            if r.status_code != 200:
                log.warning(f"Relay Perplexity HTTP {r.status_code}: {r.text[:200]}")
                return 0.0, "", {}

            raw_text = (
                r.json()
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "{}")
            )
            # Strip any accidental markdown fences
            text = raw_text.strip()
            for fence in ("```json", "```"):
                text = text.lstrip(fence)
            text = text.rstrip("```").strip()

            result = json.loads(text)

            # Clamp verification_score to [-1, +1]
            raw_score = float(result.get("verification_score", 0.0))
            score = max(-1.0, min(1.0, raw_score))

            verdict = result.get("verdict", "weak")
            if verdict not in ("supported", "mixed", "weak", "contradicted"):
                verdict = "weak"

            log.info(
                f"Relay Perplexity [{cluster.ticker}]: "
                f"verdict={verdict} score={score:+.2f} "
                f"hypothesis='{hypothesis[:80]}...'"
            )
            return score, verdict, result

    except json.JSONDecodeError as e:
        log.warning(f"Relay Perplexity JSON parse error [{cluster.ticker}]: {e} — raw: {raw_text[:200]}")
        return 0.0, "", {}
    except Exception as e:
        log.warning(f"Relay Perplexity error [{cluster.ticker}]: {e}")
        return 0.0, "", {}


def _apply_verification_to_cluster(
    cluster:         RelayCluster,
    verification_score: float,
    verdict:         str,
    result:          dict,
    base_confidence: float,
) -> float:
    """
    Apply Perplexity verdict to cluster state and return adjusted confidence.

    Formula: confidence = clamp(confidence + verification_score * 20, 0, 100)
    """
    cluster.perp_score         = verification_score
    cluster.perp_verdict       = verdict
    cluster.perp_justification = result.get("justification", "")[:300]
    cluster.perp_key_risks     = result.get("key_risks", [])[:3]
    cluster.perp_extra_sources = result.get("extra_sources", [])[:3]
    cluster.perp_audit_notes   = result.get("audit_notes", "")[:150]
    cluster.verified           = verdict in ("supported", "mixed")

    adjusted = base_confidence + (verification_score * 20)
    adjusted = max(0.0, min(100.0, adjusted))
    return round(adjusted, 1)


def _detect_cross_asset_alignment() -> Dict[str, List[str]]:
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
    ticker:     str,
    asset_meta: dict,
    research,               # BotResearch from orchestrator
    cos_token:  str,
    direction:  str,
    strength:   float,
    summary:    str,
    tags:       List[str],
) -> Optional[dict]:
    """
    Main relay entry point. Called instead of _post_cos_signal.
    Clusters, verifies via Perplexity hypothesis-testing, then posts to CoS.
    """
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
    sig.source = "+".join(sorted(set(
        b for b in research.bot_summaries.keys()
        if not research.errors.get(b)
    )))[:40] or "sweep_engine"

    cluster = _find_or_create_cluster(sig)

    if cluster.strength < MIN_CLUSTER_CONF * 100:
        log.debug(f"Relay: {ticker} cluster too weak ({cluster.strength:.0f}) — holding")
        return None

    # ── Perplexity hypothesis-testing for strong clusters ─────────
    adjusted_confidence = cluster.confidence * 100  # start from cluster confidence %

    if cluster.strength >= PERPLEXITY_THRESHOLD * 100 and not cluster.verified:
        perp_score, verdict, full_result = await _perplexity_verify_cluster(cluster)
        if verdict:  # got a usable response
            adjusted_confidence = _apply_verification_to_cluster(
                cluster, perp_score, verdict, full_result, adjusted_confidence
            )

            # Boost/dampen signal strengths based on verdict
            multiplier = {
                "supported":    1.0 + perp_score * 0.2,
                "mixed":        1.0,
                "weak":         0.85,
                "contradicted": 0.7,
            }.get(verdict, 1.0)
            for s in cluster.signals:
                s.strength = min(95, s.strength * multiplier)

            log.info(
                f"Relay verified [{ticker}]: verdict={verdict} "
                f"perp_score={perp_score:+.2f} "
                f"conf_adjusted={adjusted_confidence:.1f} "
                f"verified={cluster.verified}"
            )

    # ── Cross-asset alignment ─────────────────────────────────────
    aligned = _detect_cross_asset_alignment()
    sector_dir_key = f"{cluster.sector}:{cluster.direction}"
    if sector_dir_key in aligned:
        cluster.cross_asset_aligned = True
        log.info(
            f"Relay cross-asset [{cluster.sector} {cluster.direction}]: "
            f"{aligned[sector_dir_key]}"
        )

    # ── Build enriched summary for CoS ───────────────────────────
    enriched_summary = cluster.summary
    if cluster.cross_asset_aligned:
        n = len(aligned.get(sector_dir_key, []))
        enriched_summary = f"[CROSS-ASSET ×{n}] {enriched_summary}"
    if cluster.verified and cluster.perp_justification:
        enriched_summary = (
            f"{enriched_summary} | "
            f"Perplexity [{cluster.perp_verdict}]: {cluster.perp_justification[:80]}"
        )

    enriched_tags = list(set(cluster.tags + [
        cluster.alignment_level,
        cluster.catalyst_type,
        *(["verified"]    if cluster.verified else []),
        *(["cross_asset"] if cluster.cross_asset_aligned else []),
        *(["geo"]         if any(s.source == "GeoBot" for s in cluster.signals) else []),
        *(["perp_supported"]    if cluster.perp_verdict == "supported" else []),
        *(["perp_contradicted"] if cluster.perp_verdict == "contradicted" else []),
    ]))[:8]

    # ── Post to CoS ───────────────────────────────────────────────
    # Use adjusted_confidence as the strength signal so CoS can gate waves correctly
    cos_strength = round(min(95.0, max(adjusted_confidence, cluster.strength)), 1)

    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.post(
                f"{MB_API_URL}/api/cos/signal",
                json={
                    "source":        "relay_bot",
                    "asset":         ticker,
                    "sector":        cluster.sector or None,
                    "direction":     cluster.direction,
                    "strength":      cos_strength,
                    "summary":       enriched_summary[:200],
                    "tags":          enriched_tags,
                    "catalyst_type": cluster.catalyst_type,
                    # Verification metadata passed through for CoS to store
                    "verification": {
                        "verdict":       cluster.perp_verdict,
                        "score":         cluster.perp_score,
                        "justification": cluster.perp_justification,
                        "key_risks":     cluster.perp_key_risks,
                        "extra_sources": cluster.perp_extra_sources,
                        "audit_notes":   cluster.perp_audit_notes,
                        "verified":      cluster.verified,
                    } if cluster.perp_verdict else None,
                },
                headers={
                    "Authorization": f"Bearer {cos_token}",
                    "Content-Type":  "application/json",
                },
            )
            if r.status_code == 200:
                resp = r.json()
                log.info(
                    f"Relay → CoS: {ticker} | {cluster.direction} | "
                    f"strength={cos_strength:.0f} | wave={resp.get('wave')} | "
                    f"conf={resp.get('confidence')} | sources={len(cluster.sources)} | "
                    f"verified={cluster.verified} | verdict={cluster.perp_verdict or 'none'} | "
                    f"cross_asset={cluster.cross_asset_aligned}"
                )
                return resp
            else:
                log.warning(f"CoS rejected relay for {ticker}: {r.status_code}")
    except Exception as e:
        log.warning(f"Relay CoS post failed for {ticker}: {e}")

    return None


def get_cluster_status() -> dict:
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
                "perp_verdict":  c.perp_verdict or "none",
                "perp_score":    c.perp_score,
                "cross_asset":   c.cross_asset_aligned,
                "age_mins":      round((time.time() - c.created_at) / 60, 1),
            }
            for c in sorted(_clusters.values(), key=lambda x: x.strength, reverse=True)
        ],
    }
