"""
Market Brain — Persona Bot
───────────────────────────
Communication layer. Translates structured catalyst data into
clear, human-readable intelligence for the user.

This bot does NOT interpret signals, assign confidence, or classify waves.
It only communicates what the intelligence engine has already decided.

Per specification: tone is structured, clear, rational, grounded in
real-world context, cause-and-effect, anticipates what matters next.
"""

import json
import logging
import os
from typing import Optional

import httpx

log = logging.getLogger("mb.persona")

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

PERSONA_PROMPT = """You are the Persona Bot — the communication layer of the Market Intelligence Engine.

Your job is to translate structured catalyst data into clear, human-readable explanations.
You do not generate signals, interpret raw data, assign confidence, or classify waves.
You only communicate what the intelligence engine has already decided.

Your tone:
- structured, clear, rational
- grounded in real-world context
- avoids hype, avoids vagueness
- explains cause-and-effect cleanly
- anticipates what matters next
- connects catalysts to missions and positions
- focuses on clarity, not drama
- educational but not patronising
- strategic but not speculative

You always produce:
1. A concise headline
2. A structured explanation
3. What changed (wave, confidence, verification, cross-asset alignment)
4. Why it matters
5. What to watch next

You never:
- invent catalysts
- reinterpret signals
- change confidence or wave
- contradict the Chief of Staff
- contradict Sebastian
- add new data
- speculate beyond the provided packet

Your outputs must be short, structured, and immediately useful.

Always respond in valid JSON only. No markdown fences, no preamble."""


async def _call_claude(prompt: str, max_tokens: int = 600) -> Optional[dict]:
    """Call Claude claude-sonnet-4-20250514 with the Persona system prompt. Returns parsed JSON or None."""
    if not ANTHROPIC_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key":         ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      "claude-sonnet-4-20250514",
                    "max_tokens": max_tokens,
                    "system":     PERSONA_PROMPT,
                    "messages":   [{"role": "user", "content": prompt}],
                },
            )
            if r.status_code != 200:
                log.warning(f"Persona Claude error {r.status_code}")
                return None
            content = r.json().get("content", [{}])[0].get("text", "{}")
            content = content.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            return json.loads(content)
    except Exception as e:
        log.warning(f"Persona bot error: {e}")
        return None


def _fallback_catalyst_summary(packet: dict) -> dict:
    """Fallback summary when Claude is unavailable."""
    ticker     = packet.get("headline", "Unknown catalyst")
    direction  = packet.get("direction", "neutral")
    wave       = packet.get("wave", "spark")
    confidence = packet.get("confidence", 0.0)
    drivers    = packet.get("drivers", [])
    return {
        "title":          ticker,
        "summary":        f"{ticker} — {direction} signal at {wave} wave ({confidence:.0%} confidence). "
                          f"{drivers[0] if drivers else 'No drivers specified.'}",
        "why_it_matters": f"Wave classification: {wave}. Direction: {direction}. Confidence: {confidence:.0%}.",
        "what_to_watch":  ["Monitor for wave escalation", "Watch for additional bot alignment", "Check cross-asset sector flow"],
    }


def _fallback_wave_transition(packet: dict) -> dict:
    return {
        "title":        f"Wave transition: {packet.get('from_wave')} → {packet.get('to_wave')}",
        "explanation":  f"Catalyst moved from {packet.get('from_wave')} to {packet.get('to_wave')} wave. Reason: {packet.get('reason', 'Multi-bot alignment.')}",
        "implications": ["Higher confidence threshold now applies", "Signal persists longer before decay"],
        "watch_next":   ["Perplexity verification", "Cross-asset alignment confirmation"],
    }


def _fallback_confidence_update(packet: dict) -> dict:
    old = packet.get("old_confidence", 0.0)
    new = packet.get("new_confidence", 0.0)
    delta = new - old
    direction_str = "increased" if delta > 0 else "decreased"
    return {
        "title":       f"Confidence {direction_str}: {old:.0%} → {new:.0%}",
        "explanation": f"Confidence {direction_str} by {abs(delta):.0%}. Reason: {packet.get('reason', 'Signal update.')}",
        "drivers":     [packet.get("reason", "Signal update")],
        "watch_next":  ["Watch for further verification", "Monitor wave transition conditions"],
    }


def _fallback_daily_brief(packet: dict) -> dict:
    top = packet.get("top_catalysts", [])
    return {
        "headline": f"Daily Brief — {packet.get('date', 'Today')}",
        "sections": [
            {"title": "Top Catalysts", "content": f"{len(top)} active catalysts tracked."},
            {"title": "Market Themes", "content": ", ".join(packet.get("market_themes", ["No themes identified"]))},
        ],
    }


class PersonaBot:
    """
    Communication layer for Market Brain.
    Translates structured intelligence packets into human-readable summaries.
    """

    def __init__(self):
        self.persona_prompt = PERSONA_PROMPT

    async def rewrite_catalyst(self, catalyst_packet: dict) -> dict:
        """
        Rewrite a catalyst packet into a structured human summary.
        Input: CatalystPacket (id, headline, wave, confidence, direction, drivers, etc.)
        Output: {title, summary, why_it_matters, what_to_watch}
        """
        prompt = (
            f"Rewrite this catalyst into a clear intelligence summary.\n\n"
            f"Catalyst: {json.dumps(catalyst_packet, indent=2)}\n\n"
            f"Respond ONLY in JSON:\n"
            f'{{"title": "concise headline", '
            f'"summary": "2-3 sentences explaining what happened and why", '
            f'"why_it_matters": "2-3 sentences on market/mission implications", '
            f'"what_to_watch": ["trigger 1", "trigger 2", "trigger 3"]}}'
        )
        result = await _call_claude(prompt, max_tokens=500)
        return result or _fallback_catalyst_summary(catalyst_packet)

    async def mission_update(self, mission_packet: dict) -> dict:
        """
        Generate a mission progress update.
        Input: {mission, wave, confidence_change, catalyst_id, reason}
        Output: {mission, update, impact, next_steps}
        """
        prompt = (
            f"Generate a mission update for this catalyst development.\n\n"
            f"Mission packet: {json.dumps(mission_packet, indent=2)}\n\n"
            f"Respond ONLY in JSON:\n"
            f'{{"mission": "mission name", '
            f'"update": "2 sentences on what changed", '
            f'"impact": "2 sentences on market and position impact", '
            f'"next_steps": ["step 1", "step 2", "step 3"]}}'
        )
        result = await _call_claude(prompt, max_tokens=400)
        if result:
            return result
        return {
            "mission":    mission_packet.get("mission", "Unknown"),
            "update":     f"Mission wave moved to {mission_packet.get('wave', 'unknown')}. {mission_packet.get('reason', '')}",
            "impact":     f"Confidence change: {mission_packet.get('confidence_change', 'unknown')}.",
            "next_steps": ["Monitor catalyst for escalation", "Review affected assets"],
        }

    async def wave_transition(self, transition_packet: dict) -> dict:
        """
        Explain a wave transition clearly.
        Input: {catalyst_id, from_wave, to_wave, reason}
        Output: {title, explanation, implications, watch_next}
        """
        prompt = (
            f"Explain this wave transition clearly.\n\n"
            f"Transition: {json.dumps(transition_packet, indent=2)}\n\n"
            f"Respond ONLY in JSON:\n"
            f'{{"title": "brief title", '
            f'"explanation": "2-3 sentences on why the wave changed and what it means", '
            f'"implications": ["implication 1", "implication 2", "implication 3"], '
            f'"watch_next": ["watch 1", "watch 2"]}}'
        )
        result = await _call_claude(prompt, max_tokens=400)
        return result or _fallback_wave_transition(transition_packet)

    async def confidence_update(self, confidence_packet: dict) -> dict:
        """
        Explain a confidence change.
        Input: {catalyst_id, old_confidence, new_confidence, reason}
        Output: {title, explanation, drivers, watch_next}
        """
        prompt = (
            f"Explain this confidence change clearly.\n\n"
            f"Update: {json.dumps(confidence_packet, indent=2)}\n\n"
            f"Respond ONLY in JSON:\n"
            f'{{"title": "brief title", '
            f'"explanation": "2 sentences on what drove the confidence change", '
            f'"drivers": ["driver 1", "driver 2"], '
            f'"watch_next": ["watch 1", "watch 2"]}}'
        )
        result = await _call_claude(prompt, max_tokens=300)
        return result or _fallback_confidence_update(confidence_packet)

    async def daily_brief(self, brief_packet: dict) -> dict:
        """
        Generate the daily intelligence brief.
        Input: {date, top_catalysts, missions, positions, market_themes}
        Output: {headline, sections[]}
        """
        prompt = (
            f"Generate the daily intelligence brief.\n\n"
            f"Brief data: {json.dumps(brief_packet, indent=2)}\n\n"
            f"Respond ONLY in JSON:\n"
            f'{{"headline": "one-line brief headline for today", '
            f'"sections": ['
            f'{{"title": "Top Catalysts", "content": "2-3 sentences"}},'
            f'{{"title": "Missions", "content": "2-3 sentences"}},'
            f'{{"title": "Market Themes", "content": "2-3 sentences"}},'
            f'{{"title": "What to Watch Today", "content": "bullet list as string"}}'
            f']}}'
        )
        result = await _call_claude(prompt, max_tokens=700)
        return result or _fallback_daily_brief(brief_packet)

    async def enrich_catalyst_for_storage(self, catalyst: dict) -> dict:
        """
        Called by CoS after creating/updating a catalyst.
        Generates the persona_summary field stored on the catalyst.
        This is what the UI displays instead of raw signal text.
        """
        packet = {
            "headline":    catalyst.get("headline", ""),
            "wave":        catalyst.get("wave", "spark"),
            "confidence":  catalyst.get("confidence", 0.0),
            "direction":   catalyst.get("direction", "neutral"),
            "drivers":     catalyst.get("bull_factors", [])[:3],
            "verification": {
                "verified": catalyst.get("verified", False),
                "strength": "high" if catalyst.get("confidence", 0) > 0.7 else "medium",
            },
            "cross_asset": {
                "assets":    catalyst.get("assets", [])[:5],
                "alignment": "strong" if len(catalyst.get("assets", [])) >= 3 else "weak",
            },
            "affected_assets": catalyst.get("assets", []),
            "notes": catalyst.get("summary", ""),
        }
        result = await self.rewrite_catalyst(packet)
        return result


# Module-level singleton
_persona_bot: Optional[PersonaBot] = None

def get_persona_bot() -> PersonaBot:
    global _persona_bot
    if _persona_bot is None:
        _persona_bot = PersonaBot()
    return _persona_bot
