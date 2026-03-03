"""
Market Brain — Persona Bot + Sebastian
───────────────────────────────────────
Two distinct layers in one module:

  PersonaBot — backend communication layer.
    Translates structured catalyst data into human-readable intelligence.
    Called async after every catalyst creation / wave transition / confidence update.
    Does NOT interpret signals, assign confidence, or classify waves.
    Outputs are stored on the catalyst as persona_summary fields.

  Sebastian — user-facing analyst.
    Invoked on demand via /api/sebastian.
    Fetches live prices from OpportunityEngine before every response.
    Pulls top opportunities from active catalysts.
    Supports two modes: beginner (plain English first) and expert (full depth).
    Generates contextual prompts per wave state and bias.
    Never influences the intelligence chain.

Per spec:
  - Sebastian fetches 40+ live prices at invocation time
  - Sebastian reads persona_summary fields stored by PersonaBot
  - Sebastian pulls top opportunities from active escalating catalysts
  - Beginner-friendly explanation always comes first
  - Technical depth available on request or for advanced users
"""

import json
import logging
import os
import time
from typing import Optional

import httpx

log = logging.getLogger("mb.persona")

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"

# ─────────────────────────────────────────────────────────────────
#  PERSONA BOT  — backend only, never user-facing
# ─────────────────────────────────────────────────────────────────

PERSONA_SYSTEM = """You are the Persona Bot — the communication layer of the Market Intelligence Engine.

Your job is to translate structured catalyst data into clear, human-readable explanations.
You do not generate signals, interpret raw data, assign confidence, or classify waves.
You only communicate what the intelligence engine has already decided.

Your tone:
- structured, clear, rational
- grounded in real-world cause-and-effect
- avoids hype, avoids vagueness
- anticipates what matters next
- educational but not patronising
- strategic but not speculative

You always produce:
1. A concise headline
2. A structured explanation
3. What changed (wave, confidence, verification, cross-asset alignment)
4. Why it matters
5. What to watch next

You never:
- invent catalysts or new data
- reinterpret signals
- change confidence or wave state
- contradict the Chief of Staff
- speculate beyond the provided packet

Respond ONLY in valid JSON. No markdown fences, no preamble."""


async def _call_claude_json(system: str, prompt: str, max_tokens: int = 600) -> Optional[dict]:
    """Call Claude and parse JSON response. Returns parsed dict or None."""
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
                    "model":      ANTHROPIC_MODEL,
                    "max_tokens": max_tokens,
                    "system":     system,
                    "messages":   [{"role": "user", "content": prompt}],
                },
            )
            if r.status_code != 200:
                log.warning(f"Claude API error {r.status_code}: {r.text[:200]}")
                return None
            content = r.json().get("content", [{}])[0].get("text", "{}")
            content = content.strip()
            for fence in ("```json", "```"):
                content = content.lstrip(fence)
            content = content.rstrip("```").strip()
            return json.loads(content)
    except Exception as e:
        log.warning(f"Claude call error: {e}")
        return None


async def _call_claude_text(system: str, prompt: str, max_tokens: int = 1000) -> Optional[str]:
    """Call Claude and return raw text response."""
    if not ANTHROPIC_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key":         ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      ANTHROPIC_MODEL,
                    "max_tokens": max_tokens,
                    "system":     system,
                    "messages":   [{"role": "user", "content": prompt}],
                },
            )
            if r.status_code != 200:
                log.warning(f"Claude text error {r.status_code}")
                return None
            return r.json().get("content", [{}])[0].get("text", "").strip()
    except Exception as e:
        log.warning(f"Claude text call error: {e}")
        return None


# ── Fallbacks ─────────────────────────────────────────────────────

def _fallback_catalyst_summary(packet: dict) -> dict:
    title     = packet.get("headline", "Unknown catalyst")
    direction = packet.get("direction", "neutral")
    wave      = packet.get("wave", "spark")
    confidence= packet.get("confidence", 0.0)
    drivers   = packet.get("drivers", [])
    return {
        "title":          title,
        "summary":        f"{title} — {direction} signal at {wave} wave ({confidence:.0%} confidence). "
                          f"{drivers[0] if drivers else 'No drivers specified.'}",
        "why_it_matters": f"Wave: {wave}. Direction: {direction}. Confidence: {confidence:.0%}.",
        "what_to_watch":  ["Monitor for wave escalation", "Watch for additional bot alignment", "Check cross-asset sector flow"],
    }

def _fallback_wave_transition(packet: dict) -> dict:
    return {
        "title":        f"Wave transition: {packet.get('from_wave')} → {packet.get('to_wave')}",
        "explanation":  f"Catalyst moved from {packet.get('from_wave')} to {packet.get('to_wave')}. Reason: {packet.get('reason', 'Multi-bot alignment.')}",
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
            {"title": "Top Catalysts",  "content": f"{len(top)} active catalysts tracked."},
            {"title": "Market Themes",  "content": ", ".join(packet.get("market_themes", ["No themes identified"]))},
        ],
    }


# ─────────────────────────────────────────────────────────────────
#  SEBASTIAN  — user-facing analyst
# ─────────────────────────────────────────────────────────────────

# Wave state → human framing
WAVE_FRAMING = {
    "spark": {
        "label":   "Early Signal",
        "plain":   "Something has just started moving — it's early and unconfirmed, but worth knowing about.",
        "meaning": "A single source has flagged activity. Not enough to act on, but the system is watching.",
    },
    "confirmed": {
        "label":   "Confirmed Signal",
        "plain":   "Multiple sources are now agreeing. This signal has enough backing to take seriously.",
        "meaning": "Two or more intelligence sources have aligned. Confidence is building.",
    },
    "escalation": {
        "label":   "Escalating Signal",
        "plain":   "The picture is getting clearer and stronger. Multiple sources agree and momentum is building.",
        "meaning": "High alignment across bots. This is the stage where opportunities often become actionable.",
    },
    "structural": {
        "label":   "Structural Shift",
        "plain":   "This has moved beyond a short-term signal — there's a structural change happening here.",
        "meaning": "3+ bots aligned, verified externally, cross-asset confirmation. The kind of move that lasts weeks or months.",
    },
    "regime": {
        "label":   "Regime Change",
        "plain":   "This is a major market event. The kind of catalyst that reshapes entire sectors.",
        "meaning": "Maximum evidence threshold reached. 4+ bots, verified, cross-asset, geo/macro type.",
    },
}

# Per-wave entry prompts (one for each wave × bias combination)
SEBASTIAN_ENTRY_PROMPTS = {
    ("bullish", "spark"):        "Something's starting here. Too early to act on, but worth keeping on your radar.",
    ("bullish", "confirmed"):    "This one is building real momentum. Multiple sources are agreeing — want me to walk through what's driving it?",
    ("bullish", "escalation"):   "The setup here is becoming genuinely interesting. Strong alignment, momentum is building. Let me show you what the bots are seeing.",
    ("bullish", "structural"):   "This has moved beyond a trade idea. There's a structural shift happening — the kind that can last weeks or months. Worth understanding properly.",
    ("bullish", "regime"):       "This is a regime-level catalyst. Major event, maximum evidence. I'd want you to understand exactly what's happening before considering any position.",
    ("bearish", "spark"):        "Early warning signs here. Nothing confirmed yet, but worth monitoring.",
    ("bearish", "confirmed"):    "Confirmed negative signals across multiple sources. Worth understanding why before the market fully prices this in.",
    ("bearish", "escalation"):   "Pressure is building on the downside. The bear case is getting crowded for a reason. Want me to break it down?",
    ("bearish", "structural"):   "Structural weakness confirmed. This isn't a dip — this is a trend change. Let me show you what the bots are seeing.",
    ("bearish", "regime"):       "Regime-level deterioration. Multiple systemic signals aligned. Protective positioning worth understanding.",
    ("neutral", "spark"):        "Something stirred but no clear direction yet. Monitoring.",
    ("neutral", "confirmed"):    "Mixed signals — some sources bullish, some cautious. Classic uncertainty setup.",
    ("neutral", "escalation"):   "High activity, no strong bias. The market is figuring this out in real time.",
    ("neutral", "structural"):   "Complex picture. Multiple valid narratives competing. Let's look at the evidence together.",
    ("neutral", "regime"):       "High-intensity regime with no clear direction. Volatility is the signal here.",
}


def _build_sebastian_system(
    live_prices_block:  str,
    catalysts_block:    str,
    opportunities_block: str,
    user_mode:          str = "auto",
) -> str:
    """
    Build Sebastian's full system prompt, injecting live context at call time.

    user_mode:
      "beginner" — always lead with plain English, avoid jargon
      "expert"   — full technical depth, less hand-holding
      "auto"     — judge from the question and provide both layers
    """

    mode_instruction = {
        "beginner": (
            "The user is new to markets. Always lead with a plain-English explanation "
            "(one sentence a 16-year-old could understand) before any technical detail. "
            "Define any jargon the first time you use it. Be warm and clear."
        ),
        "expert": (
            "The user is an experienced trader. Skip the basics. Go straight to "
            "technical depth — wave state, confidence mechanics, bot alignment, "
            "risk/reward. Use proper terminology."
        ),
        "auto": (
            "Assess the question and lead with a plain one-sentence summary, "
            "then provide the full technical picture. "
            "This serves both beginners and experienced traders reading the same answer."
        ),
    }.get(user_mode, "")

    return f"""You are Sebastian — the user-facing analyst for Market Brain.

You help traders at ALL levels understand market intelligence clearly.

{mode_instruction}

CORE RULES:
- Never tell users to buy or sell. Explain the picture, let them decide.
- Always distinguish between what the intelligence engine has decided (factual) and what you're interpreting (your view).
- Keep responses structured: summary first, then depth.
- Be concise. Most answers should be 3-5 short paragraphs maximum.
- If asked about a specific asset, always reference its current price and change from the live data below.
- If asked about opportunities, reference the ranked list below.
- Never invent prices, signals, or catalyst data. If you don't have it, say so.
- This is not financial advice — always be clear about that without being annoying about it.

WAVE STATE GUIDE (for explaining to users):
- spark: single early signal, unconfirmed
- confirmed: 2+ bot sources aligned, confidence ≥42%
- escalation: 2+ bots + verification or cross-asset, confidence ≥58%
- structural: 3+ bots + verified + cross-asset, confidence ≥72%, age ≥2h
- regime: 4+ bots + verified + cross-asset + geo/macro, confidence ≥85%, age ≥6h

LIVE PRICES (fetched at {time.strftime('%H:%M UTC')}):
{live_prices_block}

ACTIVE INTELLIGENCE (top catalysts by wave/confidence):
{catalysts_block}

TOP OPPORTUNITIES (ranked by wave + confidence + momentum):
{opportunities_block}"""


def _format_catalysts_for_sebastian(catalysts: list) -> str:
    """Format active catalysts into a clean block for Sebastian's context."""
    if not catalysts:
        return "  No active catalysts."

    lines = []
    for cat in catalysts[:8]:  # top 8 by wave priority
        wave       = cat.get("wave", "spark")
        direction  = cat.get("direction", "neutral")
        confidence = cat.get("confidence", 0.0)
        title      = cat.get("title", cat.get("headline", ""))[:80]
        assets     = ", ".join(cat.get("assets", [])[:3])
        verified   = "✓ verified" if cat.get("verified") else ""
        persona    = cat.get("persona_summary", {})
        summary    = persona.get("summary", cat.get("summary", ""))[:100] if persona else cat.get("summary", "")[:100]

        lines.append(
            f"  [{wave.upper()} | {direction} | {confidence:.0f}% conf {verified}]\n"
            f"  {title}\n"
            f"  Assets: {assets or 'sector-wide'}\n"
            f"  {summary}"
        )
    return "\n\n".join(lines)


def _format_opportunities_for_sebastian(opportunities: list) -> str:
    """Format opportunity list into a clean block for Sebastian's context."""
    if not opportunities:
        return "  No ranked opportunities available."

    lines = []
    for opp in opportunities[:10]:
        ticker    = opp.get("ticker", "")
        name      = opp.get("name", "")
        price     = opp.get("price", 0)
        currency  = opp.get("currency", "USD")
        change    = opp.get("change_pct", 0.0)
        wave      = opp.get("catalyst_wave", "")
        direction = opp.get("direction", "")
        sub       = opp.get("sub_sector", "")
        change_str = f"{change:+.2f}%" if change is not None else "N/A"
        lines.append(
            f"  {ticker} ({name}) — {currency} {price} ({change_str} today) | "
            f"{sub} | {direction} | wave: {wave}"
        )
    return "\n".join(lines)


def get_entry_prompt(bias: str, wave: str) -> str:
    """Return the contextual entry prompt for a given wave + bias combination."""
    return SEBASTIAN_ENTRY_PROMPTS.get(
        (bias, wave),
        SEBASTIAN_ENTRY_PROMPTS.get(("neutral", wave), "Monitoring this situation.")
    )


def get_wave_framing(wave: str) -> dict:
    """Return the plain-English framing for a wave state."""
    return WAVE_FRAMING.get(wave, WAVE_FRAMING["spark"])


# ─────────────────────────────────────────────────────────────────
#  PersonaBot class
# ─────────────────────────────────────────────────────────────────

class PersonaBot:
    """
    Backend communication layer for Market Brain.
    Translates structured intelligence packets into human-readable summaries.
    Outputs stored on catalysts as persona_summary fields.
    """

    async def rewrite_catalyst(self, catalyst_packet: dict) -> dict:
        prompt = (
            f"Rewrite this catalyst into a clear intelligence summary.\n\n"
            f"Catalyst: {json.dumps(catalyst_packet, indent=2)}\n\n"
            f"Respond ONLY in JSON:\n"
            f'{{"title": "concise headline (max 10 words)", '
            f'"summary": "2-3 sentences explaining what happened and why", '
            f'"why_it_matters": "2-3 sentences on market implications", '
            f'"what_to_watch": ["trigger 1", "trigger 2", "trigger 3"]}}'
        )
        result = await _call_claude_json(PERSONA_SYSTEM, prompt, max_tokens=500)
        return result or _fallback_catalyst_summary(catalyst_packet)

    async def mission_update(self, mission_packet: dict) -> dict:
        prompt = (
            f"Generate a mission progress update.\n\n"
            f"Mission packet: {json.dumps(mission_packet, indent=2)}\n\n"
            f"Respond ONLY in JSON:\n"
            f'{{"mission": "mission name", '
            f'"update": "2 sentences on what changed", '
            f'"impact": "2 sentences on market and position impact", '
            f'"next_steps": ["step 1", "step 2", "step 3"]}}'
        )
        result = await _call_claude_json(PERSONA_SYSTEM, prompt, max_tokens=400)
        if result:
            return result
        return {
            "mission":    mission_packet.get("mission", "Unknown"),
            "update":     f"Mission wave moved to {mission_packet.get('wave', 'unknown')}. {mission_packet.get('reason', '')}",
            "impact":     f"Confidence change: {mission_packet.get('confidence_change', 'unknown')}.",
            "next_steps": ["Monitor catalyst for escalation", "Review affected assets"],
        }

    async def wave_transition(self, transition_packet: dict) -> dict:
        prompt = (
            f"Explain this wave transition clearly.\n\n"
            f"Transition: {json.dumps(transition_packet, indent=2)}\n\n"
            f"Respond ONLY in JSON:\n"
            f'{{"title": "brief title", '
            f'"explanation": "2-3 sentences on why the wave changed and what it means", '
            f'"implications": ["implication 1", "implication 2", "implication 3"], '
            f'"watch_next": ["watch 1", "watch 2"]}}'
        )
        result = await _call_claude_json(PERSONA_SYSTEM, prompt, max_tokens=400)
        return result or _fallback_wave_transition(transition_packet)

    async def confidence_update(self, confidence_packet: dict) -> dict:
        prompt = (
            f"Explain this confidence change clearly.\n\n"
            f"Update: {json.dumps(confidence_packet, indent=2)}\n\n"
            f"Respond ONLY in JSON:\n"
            f'{{"title": "brief title", '
            f'"explanation": "2 sentences on what drove the confidence change", '
            f'"drivers": ["driver 1", "driver 2"], '
            f'"watch_next": ["watch 1", "watch 2"]}}'
        )
        result = await _call_claude_json(PERSONA_SYSTEM, prompt, max_tokens=300)
        return result or _fallback_confidence_update(confidence_packet)

    async def daily_brief(self, brief_packet: dict) -> dict:
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
        result = await _call_claude_json(PERSONA_SYSTEM, prompt, max_tokens=700)
        return result or _fallback_daily_brief(brief_packet)

    async def enrich_catalyst_for_storage(self, catalyst: dict) -> dict:
        """
        Called by CoS after creating/updating a catalyst.
        Generates the persona_summary field stored on the catalyst.
        """
        packet = {
            "headline":   catalyst.get("headline", ""),
            "wave":       catalyst.get("wave", "spark"),
            "confidence": catalyst.get("confidence", 0.0),
            "direction":  catalyst.get("direction", "neutral"),
            "drivers":    catalyst.get("bull_factors", [])[:3],
            "verification": {
                "verified": catalyst.get("verified", False),
                "verdict":  catalyst.get("verification", {}).get("verdict", ""),
            },
            "cross_asset": {
                "assets":    catalyst.get("assets", [])[:5],
                "alignment": "strong" if len(catalyst.get("assets", [])) >= 3 else "weak",
            },
            "affected_assets": catalyst.get("assets", []),
            "notes": catalyst.get("summary", ""),
        }
        return await self.rewrite_catalyst(packet)

    async def generate_wave_transition_explanation(
        self,
        catalyst:   dict,
        from_wave:  str,
        to_wave:    str,
        reason:     str,
    ) -> dict:
        """
        Called on every wave promotion/demotion.
        Returns explanation stored as persona_wave_note on the catalyst.
        """
        framing_from = WAVE_FRAMING.get(from_wave, {})
        framing_to   = WAVE_FRAMING.get(to_wave, {})
        packet = {
            "catalyst_id":  catalyst.get("id", ""),
            "title":        catalyst.get("title", catalyst.get("headline", "")),
            "from_wave":    from_wave,
            "to_wave":      to_wave,
            "reason":       reason,
            "plain_from":   framing_from.get("plain", ""),
            "plain_to":     framing_to.get("plain", ""),
            "direction":    catalyst.get("direction", "neutral"),
            "confidence":   catalyst.get("confidence", 0.0),
        }
        return await self.wave_transition(packet)


# ─────────────────────────────────────────────────────────────────
#  Sebastian class
# ─────────────────────────────────────────────────────────────────

class Sebastian:
    """
    User-facing analyst for Market Brain.
    Invoked on demand. Fetches live context before every response.
    Never influences the intelligence chain.
    """

    def __init__(self):
        self._mb_api_url = os.getenv("MB_API_URL", "http://localhost:8000")

    async def _fetch_live_context(self, cos_token: str) -> tuple[str, list, list]:
        """
        Fetch live prices + catalysts + opportunities.
        Returns (prices_block, catalysts_list, opportunities_list).
        """
        from research_bots.opportunity_engine import (
            get_live_price_context,
            get_all_tracked_tickers,
            get_opportunities_for_catalyst,
        )

        prices_block     = "  Prices temporarily unavailable."
        catalysts_list   = []
        opportunities_list = []

        try:
            # Live prices for all tracked tickers
            all_tickers  = get_all_tracked_tickers()
            prices_block = await get_live_price_context(all_tickers[:40])
        except Exception as e:
            log.warning(f"Sebastian: live prices error: {e}")

        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    f"{self._mb_api_url}/api/catalysts",
                    headers={"Authorization": f"Bearer {cos_token}"},
                )
                if r.status_code == 200:
                    data = r.json()
                    catalysts_list = data.get("catalysts", data if isinstance(data, list) else [])
                    # Sort by wave priority then confidence
                    wave_order = {"regime": 5, "structural": 4, "escalation": 3, "confirmed": 2, "spark": 1}
                    catalysts_list.sort(
                        key=lambda c: (wave_order.get(c.get("wave", "spark"), 0), c.get("confidence", 0)),
                        reverse=True,
                    )
        except Exception as e:
            log.warning(f"Sebastian: catalysts fetch error: {e}")

        try:
            # Get opportunities from top escalating catalyst
            escalating = [c for c in catalysts_list if c.get("wave") in ("escalation", "structural", "regime")]
            if escalating:
                opp_result = await get_opportunities_for_catalyst(escalating[0])
                opportunities_list = opp_result.get("opportunities", [])
        except Exception as e:
            log.warning(f"Sebastian: opportunities error: {e}")

        return prices_block, catalysts_list, opportunities_list

    async def answer(
        self,
        question:  str,
        cos_token: str,
        user_mode: str = "auto",
        history:   list = None,
    ) -> dict:
        """
        Main Sebastian entry point.

        Args:
            question:  the user's question
            cos_token: JWT for internal API calls
            user_mode: "beginner" | "expert" | "auto"
            history:   previous messages [{"role": ..., "content": ...}]

        Returns:
            {
                "response":          str,   # Sebastian's answer
                "entry_prompt":      str,   # contextual wave/bias prompt if relevant
                "wave_framing":      dict,  # plain-English wave explanation if relevant
                "top_opportunities": list,  # top 3 opportunities mentioned
                "prices_fetched":    int,   # number of live prices injected
                "catalysts_used":    int,   # number of catalysts in context
            }
        """
        prices_block, catalysts_list, opportunities_list = await self._fetch_live_context(cos_token)

        catalysts_block    = _format_catalysts_for_sebastian(catalysts_list)
        opportunities_block = _format_opportunities_for_sebastian(opportunities_list)
        system             = _build_sebastian_system(
            prices_block, catalysts_block, opportunities_block, user_mode
        )

        # Build message history
        messages = []
        if history:
            for msg in history[-10:]:  # last 10 turns for context
                if msg.get("role") in ("user", "assistant") and msg.get("content"):
                    messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": question})

        response_text = None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    ANTHROPIC_URL,
                    headers={
                        "x-api-key":         ANTHROPIC_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type":      "application/json",
                    },
                    json={
                        "model":      ANTHROPIC_MODEL,
                        "max_tokens": 1000,
                        "system":     system,
                        "messages":   messages,
                    },
                )
                if r.status_code == 200:
                    response_text = r.json().get("content", [{}])[0].get("text", "").strip()
        except Exception as e:
            log.warning(f"Sebastian Claude call error: {e}")

        if not response_text:
            response_text = (
                "I'm having trouble connecting right now. "
                "The intelligence data above is still valid — "
                "check the signals panel for the current picture."
            )

        # Determine if any specific wave/asset was mentioned for entry prompt
        top_catalyst    = catalysts_list[0] if catalysts_list else {}
        top_wave        = top_catalyst.get("wave", "spark")
        top_bias        = top_catalyst.get("direction", "neutral")
        entry_prompt    = get_entry_prompt(top_bias, top_wave)
        wave_framing    = get_wave_framing(top_wave)
        top_opps        = opportunities_list[:3]

        # Count live prices fetched
        prices_fetched = prices_block.count("USD") + prices_block.count("GBX") + prices_block.count("GBP")

        return {
            "response":          response_text,
            "entry_prompt":      entry_prompt,
            "wave_framing":      wave_framing,
            "top_opportunities": top_opps,
            "prices_fetched":    prices_fetched,
            "catalysts_used":    len(catalysts_list),
        }

    async def explain_wave(self, wave: str, asset: str = "", bias: str = "neutral") -> str:
        """
        Beginner-friendly explanation of a specific wave state.
        Used for onboarding prompts and card tooltips.
        """
        framing = WAVE_FRAMING.get(wave, WAVE_FRAMING["spark"])
        entry   = get_entry_prompt(bias, wave)

        if not ANTHROPIC_KEY:
            return f"{framing['plain']} {entry}"

        system = (
            "You are Sebastian, a market intelligence analyst. "
            "Explain the concept requested in 2-3 short sentences. "
            "Lead with a plain-English explanation a complete beginner can understand. "
            "Then add one sentence of technical context. "
            "Do not mention buying or selling. Be warm and clear."
        )
        prompt = (
            f"Explain the '{wave}' wave state in Market Brain's intelligence system"
            f"{f' in the context of {asset}' if asset else ''}. "
            f"Plain-English meaning: '{framing['plain']}'. "
            f"Technical meaning: '{framing['meaning']}'."
        )
        result = await _call_claude_text(system, prompt, max_tokens=200)
        return result or f"{framing['plain']} {entry}"

    async def quick_summary(self, catalyst: dict) -> str:
        """
        One-paragraph quick summary of a single catalyst for card display.
        Uses persona_summary if available, otherwise generates fresh.
        """
        persona = catalyst.get("persona_summary", {})
        if persona and persona.get("summary"):
            return persona["summary"]

        title     = catalyst.get("title", catalyst.get("headline", ""))
        wave      = catalyst.get("wave", "spark")
        direction = catalyst.get("direction", "neutral")
        framing   = WAVE_FRAMING.get(wave, WAVE_FRAMING["spark"])
        entry     = get_entry_prompt(direction, wave)

        if not ANTHROPIC_KEY:
            return f"{title}. {framing['plain']}"

        system = (
            "You are Sebastian. Write a single short paragraph (2-3 sentences) "
            "summarising this market signal for a dashboard card. "
            "Plain English first. No jargon without explanation. "
            "No buy/sell advice. Be factual and clear."
        )
        prompt = (
            f"Summarise: {title}\n"
            f"Wave: {wave} ({framing['plain']})\n"
            f"Direction: {direction}\n"
            f"Entry prompt: {entry}"
        )
        result = await _call_claude_text(system, prompt, max_tokens=150)
        return result or f"{title}. {framing['plain']}"


# ─────────────────────────────────────────────────────────────────
#  Module-level singletons
# ─────────────────────────────────────────────────────────────────

_persona_bot: Optional[PersonaBot] = None
_sebastian:   Optional[Sebastian]  = None

def get_persona_bot() -> PersonaBot:
    global _persona_bot
    if _persona_bot is None:
        _persona_bot = PersonaBot()
    return _persona_bot

def get_sebastian() -> Sebastian:
    global _sebastian
    if _sebastian is None:
        _sebastian = Sebastian()
    return _sebastian
