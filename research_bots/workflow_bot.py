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
  - Perplexity hypothesis-testing for unverified high-confidence catalysts
  - Lifecycle events: expired, renewed, promoted
  - Daily brief scheduling
  - Opportunity identification triggers

Runs on a loop every 5 minutes inside the web service.
Also triggered explicitly after each CoS signal ingestion.

Perplexity spec (per design doc):
  - Only trigger when: wave ≥ CONFIRMED, confidence 55-85,
    domains ≥ 2, mission_relevance ≥ 0.5, renewals increased since last check
  - Send a hypothesis to test, not just "research this"
  - Require structured verdict: verification_score (-1 to +1),
    verdict (supported/mixed/weak/contradicted), justification,
    key_risks, extra_sources, audit_notes
  - Apply: confidence = clamp(confidence + verification_score * 20, 0, 100)
"""

import asyncio
import json
import logging
import os
import time
from typing import Optional

import httpx

log = logging.getLogger("mb.workflow")

MB_API_URL     = os.getenv("MB_API_URL", "http://localhost:8000")
PERPLEXITY_KEY = os.environ.get("PERPLEXITY_API_KEY", "")
PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
PERPLEXITY_MODEL = "sonar"              # current model name as of 2025

# Thresholds
FOCUS_PROMOTE_CONFIDENCE  = 65.0
FOCUS_DEMOTE_CONFIDENCE   = 38.0

# Perplexity trigger conditions (per spec)
PERP_CONF_MIN  = 55.0                   # don't waste on weak signals
PERP_CONF_MAX  = 85.0                   # already well-verified above this
PERP_WAVES     = {"confirmed", "escalation", "structural", "regime"}
PERP_MIN_BOTS  = 2                      # domains ≥ 2 (distinct bot sources)

OPPORTUNITY_TRIGGER_WAVE   = {"escalation", "structural", "regime"}
CROSS_ASSET_ALERT_MIN      = 3

_last_perp_trigger: dict = {}
PERP_COOLDOWN = 3600                    # 1 hour per catalyst
_prev_renewal_counts: dict = {}         # catalyst_id → renewal count at last check

_workflow_events: list = []


def _log_event(event_type: str, catalyst_id: str, message: str, data: dict = None):
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


# ── Perplexity trigger gate ───────────────────────────────────────

def _should_trigger_perplexity(catalyst: dict) -> tuple[bool, str]:
    """
    Apply all trigger conditions from the spec.
    Returns (should_trigger, reason_if_not).
    """
    cat_id     = catalyst["id"]
    confidence = catalyst.get("confidence", 0.0)
    wave       = catalyst.get("wave", "spark")
    verified   = catalyst.get("verified", False)
    renewals   = catalyst.get("renewals", 0)
    tags       = catalyst.get("tags", [])

    # Already verified — skip
    if verified:
        return False, "already_verified"

    # Rate limit
    last = _last_perp_trigger.get(cat_id, 0)
    if time.time() - last < PERP_COOLDOWN:
        return False, "cooldown"

    # No API key
    if not PERPLEXITY_KEY:
        return False, "no_api_key"

    # Wave condition
    if wave not in PERP_WAVES:
        return False, f"wave_too_low:{wave}"

    # Confidence window (55-85)
    if confidence < PERP_CONF_MIN:
        return False, f"confidence_too_low:{confidence:.0f}"
    if confidence > PERP_CONF_MAX:
        return False, f"confidence_too_high:{confidence:.0f}"

    # Domains ≥ 2 — approximate from tags or sources field
    source_bots = [t for t in tags if t in (
        "GeoBot", "MacroBot", "EarningsBot", "FundamentalsBot",
        "NewsBot", "TechnicalBot", "InsiderBot", "AnalystBot", "HiringBot",
        "weak", "medium", "strong",  # alignment tags
    )]
    bot_count = len(source_bots)
    if bot_count < PERP_MIN_BOTS and "cross_asset" not in tags:
        return False, f"insufficient_sources:{bot_count}"

    # Renewals increased since last check (signal is freshening, not stale)
    prev_renewals = _prev_renewal_counts.get(cat_id, 0)
    if renewals <= prev_renewals and prev_renewals > 0:
        return False, "no_new_renewals"

    return True, "ok"


# ── Hypothesis builder ────────────────────────────────────────────

def _build_hypothesis(catalyst: dict) -> str:
    """
    Construct a specific, testable hypothesis from catalyst data.
    The hypothesis is sent to Perplexity as a claim to verify — not a question.
    """
    title     = catalyst.get("title", catalyst.get("headline", "market signal"))
    direction = catalyst.get("direction", "neutral")
    wave      = catalyst.get("wave", "confirmed")
    assets    = catalyst.get("assets", [])[:3]
    sectors   = catalyst.get("sectors", [])[:2]

    direction_phrase = {
        "bullish": "structurally bullish for",
        "bearish": "structurally bearish for",
        "neutral": "creating material uncertainty for",
    }.get(direction, "affecting")

    timeframe_map = {
        "confirmed":  "1-4 week",
        "escalation": "1-6 month",
        "structural": "3-12 month",
        "regime":     "6-18 month",
    }
    timeframe = timeframe_map.get(wave, "1-6 month")

    asset_str  = ", ".join(assets)  if assets  else sectors[0] if sectors else "the sector"
    sector_str = ", ".join(sectors) if sectors else "the relevant sector"

    bull_factors = catalyst.get("bull_factors", [])[:2]
    drivers      = "; ".join(bull_factors) if bull_factors else title

    hypothesis = (
        f"Hypothesis: Current market conditions are {direction_phrase} "
        f"{sector_str} equities (including {asset_str}) "
        f"over a {timeframe} timeframe. "
        f"Key drivers cited: {drivers}."
    )
    return hypothesis


# ── Perplexity call ───────────────────────────────────────────────

async def _call_perplexity_hypothesis(hypothesis: str, catalyst_id: str) -> Optional[dict]:
    """
    Submit a hypothesis to Perplexity for structured verification.

    Returns structured verdict dict or None on failure.
    Verdict schema:
        {
            "verification_score": float (-1.0 to +1.0),
            "verdict":            str (supported|mixed|weak|contradicted),
            "justification":      str,
            "key_risks":          list[str],
            "extra_sources":      list[str],
            "audit_notes":        str,
        }
    """
    system_prompt = (
        "You are a financial verification engine. "
        "Test the provided market hypothesis against current real-world information. "
        "Search for evidence both supporting and contradicting the claim. "
        "Be rigorous. Consider recency, source quality, and conflicting signals. "
        "Respond ONLY in valid JSON. No markdown, no preamble."
    )

    user_prompt = (
        f"Test this hypothesis:\n\n{hypothesis}\n\n"
        f"Respond ONLY in this exact JSON format:\n"
        f'{{'
        f'"verification_score": <float -1.0 to +1.0, '
        f'where +1.0 = fully supported, -1.0 = fully contradicted>,'
        f'"verdict": "<supported|mixed|weak|contradicted>",'
        f'"justification": "<2-3 sentences citing specific evidence you found>",'
        f'"key_risks": ["<risk 1>", "<risk 2>", "<risk 3>"],'
        f'"extra_sources": ["<headline or source 1>", "<headline or source 2>"],'
        f'"audit_notes": "<any important caveats, data gaps, or conflicting signals>"'
        f'}}'
    )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                PERPLEXITY_URL,
                headers={
                    "Authorization": f"Bearer {PERPLEXITY_KEY}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":    PERPLEXITY_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                    "max_tokens":  700,
                    "temperature": 0.1,
                },
            )

            if r.status_code != 200:
                log.warning(f"Perplexity HTTP {r.status_code} for {catalyst_id}: {r.text[:200]}")
                return None

            raw_text = (
                r.json()
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "{}")
            )
            text = raw_text.strip()
            for fence in ("```json", "```"):
                text = text.lstrip(fence)
            text = text.rstrip("```").strip()

            result = json.loads(text)

            # Normalise and clamp
            raw_score = float(result.get("verification_score", 0.0))
            result["verification_score"] = max(-1.0, min(1.0, raw_score))

            verdict = result.get("verdict", "weak")
            if verdict not in ("supported", "mixed", "weak", "contradicted"):
                result["verdict"] = "weak"

            log.info(
                f"Perplexity verified [{catalyst_id}]: "
                f"verdict={result['verdict']} "
                f"score={result['verification_score']:+.2f}"
            )
            return result

    except json.JSONDecodeError as e:
        log.warning(f"Perplexity JSON parse error [{catalyst_id}]: {e}")
        return None
    except Exception as e:
        log.warning(f"Perplexity call error [{catalyst_id}]: {e}")
        return None


# ── Confidence adjustment ─────────────────────────────────────────

def _adjust_confidence(base_confidence: float, verification_score: float) -> float:
    """
    confidence = clamp(confidence + verification_score * 20, 0, 100)
    """
    adjusted = base_confidence + (verification_score * 20)
    return round(max(0.0, min(100.0, adjusted)), 1)


# ── Auto-verification ─────────────────────────────────────────────

async def _auto_perplexity_verify(catalyst: dict, cos_token: str) -> Optional[dict]:
    """
    Full verification flow for one catalyst:
      1. Gate check
      2. Build hypothesis
      3. Call Perplexity
      4. Adjust confidence
      5. Post updated signal back to CoS
      6. Log event
    """
    cat_id = catalyst["id"]

    should, reason = _should_trigger_perplexity(catalyst)
    if not should:
        log.debug(f"Workflow Perplexity skipped [{cat_id}]: {reason}")
        return None

    hypothesis = _build_hypothesis(catalyst)
    _last_perp_trigger[cat_id] = time.time()

    # Track renewals at trigger time so we don't re-trigger on same renewal count
    _prev_renewal_counts[cat_id] = catalyst.get("renewals", 0)

    log.info(f"Workflow triggering Perplexity [{cat_id}]: '{hypothesis[:100]}...'")

    result = await _call_perplexity_hypothesis(hypothesis, cat_id)
    if not result:
        return None

    verification_score = result["verification_score"]
    verdict            = result["verdict"]
    adjusted_conf      = _adjust_confidence(catalyst.get("confidence", 50.0), verification_score)

    # Post verification back into the intelligence chain via CoS signal
    # This lets CoS apply the confidence change through its normal lifecycle
    verification_summary = (
        f"Perplexity [{verdict}] score={verification_score:+.2f}: "
        f"{result.get('justification', '')[:150]}"
    )

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"{MB_API_URL}/api/cos/signal",
                json={
                    "source":    "perplexity",
                    "asset":     (catalyst.get("assets") or [None])[0],
                    "sector":    (catalyst.get("sectors") or [None])[0],
                    "direction": catalyst.get("direction", "neutral"),
                    # verification_score maps to signal strength [-100, +100 normalised to 0-100]
                    "strength":  max(0.0, (verification_score + 1.0) * 50),
                    "summary":   verification_summary[:200],
                    "tags":      [
                        "verified", "perplexity",
                        f"perp_{verdict}",
                        *(["geo"] if result.get("audit_notes", "").lower().find("geo") != -1 else []),
                    ],
                    "catalyst_type": catalyst.get("type", "asset"),
                    "verification": {
                        "verdict":            verdict,
                        "score":              verification_score,
                        "justification":      result.get("justification", ""),
                        "key_risks":          result.get("key_risks", []),
                        "extra_sources":      result.get("extra_sources", []),
                        "audit_notes":        result.get("audit_notes", ""),
                        "adjusted_confidence": adjusted_conf,
                        "verified":           verdict in ("supported", "mixed"),
                        "verified_at":        time.time(),
                    },
                },
                headers={
                    "Authorization": f"Bearer {cos_token}",
                    "Content-Type":  "application/json",
                },
            )
    except Exception as e:
        log.warning(f"Workflow: failed to post Perplexity result to CoS: {e}")

    _log_event(
        "perplexity_verified" if verdict in ("supported", "mixed") else "perplexity_checked",
        cat_id,
        f"verdict={verdict} score={verification_score:+.2f} "
        f"conf {catalyst.get('confidence', 0):.0f}→{adjusted_conf:.0f}",
        {
            "hypothesis":        hypothesis[:150],
            "verdict":           verdict,
            "verification_score": verification_score,
            "adjusted_conf":     adjusted_conf,
            "key_risks":         result.get("key_risks", []),
            "extra_sources":     result.get("extra_sources", []),
        },
    )

    return result


# ── Main workflow cycle ───────────────────────────────────────────

async def run_workflow_cycle(catalysts: list, cos_token: str) -> dict:
    """
    Main workflow cycle. Called every 5 minutes and after each CoS signal.
    Processes all active catalysts and triggers appropriate actions.
    Returns summary of actions taken.
    """
    actions = {
        "attention_promoted":    [],
        "attention_demoted":     [],
        "perplexity_triggered":  [],
        "opportunities_flagged": [],
        "cross_asset_alerts":    [],
        "lifecycle_events":      [],
        "total_processed":       len(catalysts),
    }

    now = time.time()

    # ── 1. Attention management ───────────────────────────────────
    for cat in catalysts:
        cat_id     = cat["id"]
        confidence = cat.get("confidence", 0.0)
        wave       = cat.get("wave", "spark")
        attention  = cat.get("attention", "watch")
        verified   = cat.get("verified", False)

        should_focus = (
            confidence >= FOCUS_PROMOTE_CONFIDENCE
            or wave in ("escalation", "structural", "regime")
            or (wave == "confirmed" and verified)
        )
        if should_focus and attention == "watch":
            cat["attention"] = "focus"
            _log_event("attention_promoted", cat_id,
                       f"→ Focus: conf={confidence:.0f} wave={wave}")
            actions["attention_promoted"].append(cat_id)

        elif attention == "focus" and confidence < FOCUS_DEMOTE_CONFIDENCE:
            cat["attention"] = "watch"
            _log_event("attention_demoted", cat_id,
                       f"→ Watch: conf={confidence:.0f} decayed")
            actions["attention_demoted"].append(cat_id)

    # ── 2. Perplexity hypothesis-testing ─────────────────────────
    # Only trigger on catalysts meeting all spec conditions
    candidates = [
        cat for cat in catalysts
        if not cat.get("verified")
        and PERP_CONF_MIN <= cat.get("confidence", 0) <= PERP_CONF_MAX
        and cat.get("wave") in PERP_WAVES
    ]

    # Prioritise by wave strength then confidence
    wave_priority = {"regime": 4, "structural": 3, "escalation": 2, "confirmed": 1}
    candidates.sort(
        key=lambda c: (wave_priority.get(c.get("wave", ""), 0), c.get("confidence", 0)),
        reverse=True,
    )

    # Verify up to 3 per cycle with a small gap between calls
    verified_count = 0
    for cat in candidates:
        if verified_count >= 3:
            break
        result = await _auto_perplexity_verify(cat, cos_token)
        if result:
            verified_count += 1
            actions["perplexity_triggered"].append({
                "catalyst_id":        cat["id"],
                "verdict":            result.get("verdict"),
                "verification_score": result.get("verification_score"),
                "hypothesis":         _build_hypothesis(cat)[:100],
            })
            # Small delay between calls to avoid hammering the API
            await asyncio.sleep(1.5)

    # ── 3. Opportunity flagging ───────────────────────────────────
    for cat in catalysts:
        if (
            cat.get("wave") in OPPORTUNITY_TRIGGER_WAVE
            and cat.get("direction") in ("bullish", "bearish")
            and cat.get("sectors")
        ):
            actions["opportunities_flagged"].append({
                "catalyst_id": cat["id"],
                "sectors":     cat.get("sectors", []),
                "wave":        cat.get("wave"),
                "direction":   cat.get("direction"),
            })
        if len(actions["opportunities_flagged"]) >= 5:
            break

    # ── 4. Cross-asset alert detection ───────────────────────────
    sector_direction: dict = {}
    for cat in catalysts:
        for sector in cat.get("sectors", []):
            key = f"{sector}:{cat.get('direction', 'neutral')}"
            sector_direction.setdefault(key, []).append(cat["id"])

    for key, cat_ids in sector_direction.items():
        if len(cat_ids) >= CROSS_ASSET_ALERT_MIN:
            sector, direction = key.split(":", 1)
            _log_event(
                "cross_asset_alert", "multi",
                f"Cross-asset: {len(cat_ids)} catalysts {direction} in {sector}",
                {"sector": sector, "direction": direction, "count": len(cat_ids)},
            )
            actions["cross_asset_alerts"].append({
                "sector":       sector,
                "direction":    direction,
                "count":        len(cat_ids),
                "catalyst_ids": cat_ids,
            })

    # ── 5. Lifecycle events ───────────────────────────────────────
    for cat in catalysts:
        time_left = cat.get("expires_at", now) - now
        if 0 < time_left < 7200:
            _log_event(
                "expiring_soon", cat["id"],
                f"Expires in {time_left/3600:.1f}h: {cat.get('title', '')[:60]}",
            )
            actions["lifecycle_events"].append({
                "type":        "expiring_soon",
                "catalyst_id": cat["id"],
                "hours_left":  round(time_left / 3600, 1),
            })

    return actions


def get_workflow_events(limit: int = 50) -> list:
    return _workflow_events[:limit]
