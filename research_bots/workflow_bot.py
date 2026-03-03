"""
Market Brain — Workflow Bot
────────────────────────────
Automation brain. Sits after Persona Bot in the chain.
Orchestrates actions around catalysts — never interprets them.

Responsibilities:
  - Attention escalation: Watch → Focus (high confidence / wave)
  - Attention de-escalation: Focus → Watch (decayed confidence)
  - Mission progress updates
  - Cross-asset alert generation
  - Follow-up sweep triggers (posts back to sweep_engine)
  - Perplexity verification triggers for unverified high-confidence catalysts
  - Lifecycle events: expired, renewed, promoted
  - Daily brief scheduling
  - Opportunity identification triggers

Runs on a loop every 5 minutes inside the web service.
Also triggered explicitly after each CoS signal ingestion.
"""

import asyncio
import logging
import os
import time
from typing import Optional

import httpx

log = logging.getLogger("mb.workflow")

MB_API_URL   = os.getenv("MB_API_URL", "http://localhost:8000")
PERPLEXITY_KEY = os.environ.get("PERPLEXITY_API_KEY", "")
PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"

# Thresholds
FOCUS_PROMOTE_CONFIDENCE  = 65.0   # promote to Focus above this
FOCUS_DEMOTE_CONFIDENCE   = 38.0   # demote to Watch below this
PERP_TRIGGER_CONFIDENCE   = 58.0   # trigger Perplexity above this
PERP_TRIGGER_WAVE         = {"confirmed", "escalation", "structural", "regime"}
OPPORTUNITY_TRIGGER_WAVE  = {"escalation", "structural", "regime"}
CROSS_ASSET_ALERT_MIN     = 3      # min assets in sector for cross-asset alert

_last_perp_trigger: dict = {}  # catalyst_id → timestamp
PERP_COOLDOWN = 3600  # don't re-verify same catalyst within 1 hour

_workflow_events: list = []   # in-memory event log (last 200)


def _log_event(event_type: str, catalyst_id: str, message: str, data: dict = None):
    """Log a workflow event."""
    event = {
        "type":        event_type,
        "catalyst_id": catalyst_id,
        "message":     message,
        "data":        data or {},
        "ts":          time.time(),
    }
    _workflow_events.insert(0, event)
    if len(_workflow_events) > 200:
        _workflow_events.pop()
    log.info(f"Workflow [{event_type}] {catalyst_id}: {message}")


async def _auto_perplexity_verify(catalyst: dict, cos_token: str) -> Optional[dict]:
    """
    Auto-trigger Perplexity verification for unverified high-confidence catalysts.
    Writes verified=True + sources back onto the catalyst.
    """
    cat_id = catalyst["id"]

    # Rate limit — don't re-verify too often
    last = _last_perp_trigger.get(cat_id, 0)
    if time.time() - last < PERP_COOLDOWN:
        return None

    if not PERPLEXITY_KEY:
        return None

    title   = catalyst.get("title", "market signal")
    wave    = catalyst.get("wave", "spark")
    assets  = ", ".join(catalyst.get("assets", [])[:5])
    sectors = ", ".join(catalyst.get("sectors", [])[:3])
    direction = catalyst.get("direction", "neutral")

    prompt = (
        f"Verify this market signal against current real-world information:\n\n"
        f"Signal: {title}\n"
        f"Wave: {wave} | Direction: {direction}\n"
        f"Assets: {assets}\n"
        f"Sectors: {sectors}\n\n"
        f"Search for current news, data, or events that confirm or contradict this signal.\n"
        f"Respond ONLY in valid JSON:\n"
        f'{{"verified": true/false, '
        f'"confidence_score": 0-100, '
        f'"sentiment": "bullish"|"bearish"|"neutral", '
        f'"reliability": "high"|"medium"|"low", '
        f'"reasoning": "2-3 sentences citing what you found", '
        f'"sources": ["source1", "source2", "source3"], '
        f'"geopolitical_context": "brief context if relevant or empty string"}}'
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
                    "model":    "llama-3.1-sonar-large-128k-online",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 500,
                    "temperature": 0.1,
                },
            )
            _last_perp_trigger[cat_id] = time.time()

            if r.status_code != 200:
                log.warning(f"Perplexity verify failed: {r.status_code}")
                return None

            text = r.json().get("choices", [{}])[0].get("message", {}).get("content", "{}")
            text = text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            import json
            result = json.loads(text)

            verified = result.get("verified", False)
            score    = float(result.get("confidence_score", 50))
            sources  = result.get("sources", [])
            reasoning = result.get("reasoning", "")
            context  = result.get("geopolitical_context", "")

            # Write back to catalyst via API
            patch = {
                "verified":              verified,
                "verification_score":    score,
                "verification_sources":  sources,
                "verification_notes":    reasoning,
                "verification_context":  context,
                "verified_at":           time.time(),
            }

            # Post verification signal to CoS — this updates the catalyst
            await client.post(
                f"{MB_API_URL}/api/cos/signal",
                json={
                    "source":        "perplexity",
                    "asset":         (catalyst.get("assets") or [None])[0],
                    "sector":        (catalyst.get("sectors") or [None])[0],
                    "direction":     result.get("sentiment", direction),
                    "strength":      score,
                    "summary":       reasoning[:200] if reasoning else f"Perplexity verified: {title[:80]}",
                    "tags":          ["verified", "perplexity"] + (["geo"] if context else []),
                    "catalyst_type": catalyst.get("type", "asset"),
                },
                headers={"Authorization": f"Bearer {cos_token}", "Content-Type": "application/json"},
            )

            _log_event(
                "perplexity_verified" if verified else "perplexity_checked",
                cat_id,
                f"Perplexity: verified={verified} score={score:.0f} sources={len(sources)}",
                {"score": score, "sources": sources, "reasoning": reasoning[:150]},
            )

            return result

    except Exception as e:
        log.warning(f"Workflow Perplexity error: {e}")
        return None


async def run_workflow_cycle(catalysts: list, cos_token: str) -> dict:
    """
    Main workflow cycle. Called every 5 minutes and after each CoS signal.
    Processes all active catalysts and triggers appropriate actions.

    Returns summary of actions taken.
    """
    actions = {
        "attention_promoted":   [],
        "attention_demoted":    [],
        "perplexity_triggered": [],
        "opportunities_flagged":[],
        "cross_asset_alerts":   [],
        "lifecycle_events":     [],
        "total_processed":      len(catalysts),
    }

    now = time.time()

    # ── 1. Attention management ───────────────────────────────────
    for cat in catalysts:
        cat_id     = cat["id"]
        confidence = cat.get("confidence", 0.0)
        wave       = cat.get("wave", "spark")
        attention  = cat.get("attention", "watch")
        verified   = cat.get("verified", False)

        # Promote to Focus
        should_focus = (
            confidence >= FOCUS_PROMOTE_CONFIDENCE
            or wave in ("escalation", "structural", "regime")
            or (wave == "confirmed" and verified)
        )
        if should_focus and attention == "watch":
            cat["attention"] = "focus"
            _log_event("attention_promoted", cat_id,
                       f"Promoted to Focus: conf={confidence:.0f} wave={wave}")
            actions["attention_promoted"].append(cat_id)

        # Demote from Focus if confidence has decayed
        elif attention == "focus" and confidence < FOCUS_DEMOTE_CONFIDENCE:
            cat["attention"] = "watch"
            _log_event("attention_demoted", cat_id,
                       f"Demoted to Watch: conf={confidence:.0f} decayed")
            actions["attention_demoted"].append(cat_id)

    # ── 2. Perplexity auto-verification ──────────────────────────
    unverified_high = [
        cat for cat in catalysts
        if not cat.get("verified")
        and cat.get("confidence", 0) >= PERP_TRIGGER_CONFIDENCE
        and cat.get("wave") in PERP_TRIGGER_WAVE
    ]

    # Verify up to 3 per cycle to avoid hammering Perplexity
    for cat in unverified_high[:3]:
        result = await _auto_perplexity_verify(cat, cos_token)
        if result:
            actions["perplexity_triggered"].append({
                "catalyst_id": cat["id"],
                "verified":    result.get("verified"),
                "score":       result.get("confidence_score"),
            })

    # ── 3. Opportunity flagging ───────────────────────────────────
    opportunity_catalysts = [
        cat for cat in catalysts
        if cat.get("wave") in OPPORTUNITY_TRIGGER_WAVE
        and cat.get("direction") in ("bullish", "bearish")
        and cat.get("sectors")
    ]
    for cat in opportunity_catalysts[:5]:
        actions["opportunities_flagged"].append({
            "catalyst_id": cat["id"],
            "sectors":     cat.get("sectors", []),
            "wave":        cat.get("wave"),
            "direction":   cat.get("direction"),
        })

    # ── 4. Cross-asset alert detection ───────────────────────────
    sector_direction: dict = {}
    for cat in catalysts:
        for sector in cat.get("sectors", []):
            key = f"{sector}:{cat.get('direction','neutral')}"
            sector_direction.setdefault(key, []).append(cat["id"])

    for key, cat_ids in sector_direction.items():
        if len(cat_ids) >= CROSS_ASSET_ALERT_MIN:
            sector, direction = key.split(":", 1)
            _log_event("cross_asset_alert", "multi",
                       f"Cross-asset: {len(cat_ids)} catalysts {direction} in {sector}",
                       {"sector": sector, "direction": direction, "count": len(cat_ids)})
            actions["cross_asset_alerts"].append({
                "sector":     sector,
                "direction":  direction,
                "count":      len(cat_ids),
                "catalyst_ids": cat_ids,
            })

    # ── 5. Lifecycle events ───────────────────────────────────────
    for cat in catalysts:
        # About to expire (< 2 hours left)
        time_left = cat.get("expires_at", now) - now
        if 0 < time_left < 7200:
            _log_event("expiring_soon", cat["id"],
                       f"Expires in {time_left/3600:.1f}h: {cat['title'][:60]}")
            actions["lifecycle_events"].append({
                "type": "expiring_soon", "catalyst_id": cat["id"],
                "hours_left": round(time_left / 3600, 1),
            })

    return actions


def get_workflow_events(limit: int = 50) -> list:
    """Return recent workflow events for the dashboard."""
    return _workflow_events[:limit]
