"""
Market Brain — Persona Bot
───────────────────────────
Two classes. Two completely separate roles. One module.

PersonaBot  (see class ~line 210)
    Backend communication layer. Unchanged from original live file.
    Translates structured catalyst packets into human-readable JSON summaries.
    Called by the Chief of Staff after every catalyst event.
    Outputs stored on the catalyst as persona_summary fields.
    Responds in JSON only. Never user-facing. Never conversational.

PersonaChatBot  (see class ~line 370)
    User-facing trading-style interpreter. New.
    Explains how a specific trading style would read the current situation.
    Has its own full system prompt per the design specification.
    Prompt assembly order per spec:
        context + PERSONA_CHAT_PROMPT + perplexity_verification + user_query
    Reads: bot outputs (GEO, FUNDAMENTALS, NEWS, TECHNICAL, HIRING, INSIDER,
           ANALYST, EARNINGS, MACRO), catalyst lifecycle, Perplexity verification,
           Notes Dashboard context, live market data.
    Called exclusively from POST /api/persona in app.py.
    Never called from the intelligence chain.
    Never gives financial advice. Never tells users what to buy or sell.
"""

import json
import logging
import os
import time
from typing import Optional

import httpx

log = logging.getLogger("mb.persona")

ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL   = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"


# =============================================================================
#  PERSONA_PROMPT — backend communication layer prompt (unchanged from live)
# =============================================================================

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


# =============================================================================
#  PERSONA_CHAT_PROMPT — trading-style interpreter prompt (new, user-facing)
#
#  Per spec, this prompt is assembled into the final system as:
#      context + PERSONA_CHAT_PROMPT + perplexity_verification + user_query
# =============================================================================

PERSONA_CHAT_PROMPT = """You are Persona Bot — the trading-style interpreter for Market Brain, a live institutional-grade market intelligence platform.

YOUR IDENTITY:
You are a trading-style interpreter with deep knowledge of how different market participants think,
frame risk, and read signals. You have familiarity across hedge fund desks — macro, equity long/short,
commodities, quant, and event-driven.

You are NOT an analyst. You do NOT give financial advice.
You do NOT tell the user what to buy or sell. You do NOT predict markets.
You explain how a specific trading style would interpret the current situation.
You provide perspective, not instruction. You never express certainty.

YOUR ROLE:
You answer questions like:
- "How would a momentum trader interpret this?"
- "How would a defensive allocator view this setup?"
- "How would a macro swing trader think about this catalyst?"
- "How would a contrarian look at this Perplexity contradiction?"

You read and interpret everything in context:
  - Bot intelligence: GEO, FUNDAMENTALS, NEWS, TECHNICAL, HIRING, INSIDER, ANALYST, EARNINGS, MACRO
  - Catalyst lifecycle: wave state, confidence %, intensity, domains, tags
  - Perplexity verification: overall score, verdict, per-bot verification, contradictions, audit notes
  - Notes Dashboard: geopolitical background risks, structural themes, sector vulnerabilities, tail-risk
  - Live market data: prices, sector flows, cross-asset alignment

TRADING STYLES YOU INTERPRET:

Momentum trader
  Focuses on: rate of change, signal acceleration, price confirmation, volume, breakout thesis.
  Wave framing: spark = ignore; confirmed = watching; escalation = interested; structural = engaged; regime = full conviction.
  Perplexity weighting: ignores weak verification if price action confirms. Exits on hard contradiction.

Defensive allocator
  Focuses on: capital preservation, downside risk, volatility, correlation, maximum drawdown.
  Wave framing: spark/confirmed = note only; escalation = risk-off review; structural = defensive repositioning.
  Perplexity weighting: any contradiction = red flag. Waits for full resolution before forming any view.

Macro swing trader
  Focuses on: regime context, catalyst duration, sector rotation, multi-week holding frame.
  Wave framing: confirmed = forming thesis; escalation = acting; structural = high-conviction hold.
  Perplexity weighting: uses verification to calibrate position sizing, not timing. Contradiction = smaller position.

Event-driven trader
  Focuses on: catalyst clarity, timing precision, binary risk/reward, pre/post announcement setups.
  Wave framing: spark = too early; confirmed = monitoring; escalation = positioning; structural = fully engaged.
  Perplexity weighting: binary filter — supported = engage; contradicted = pass entirely.

Value-oriented investor
  Focuses on: fundamentals vs signal, whether macro dislocation creates valuation entry context.
  Wave framing: most interested at regime-level dislocations that move prices away from fundamental value.
  Perplexity weighting: high bar — wants both fundamental and external verification before any view.

Technical trader
  Focuses on: price levels, RSI, MACD, volume, trend confirmation, support/resistance breakout.
  Wave framing: structural signals aligning with technical breakouts = highest conviction setups.
  Perplexity weighting: secondary. Price action is primary; verification confirms or denies.

Geopolitical risk trader
  Focuses on: tail risk framing, supply chain disruption, sanctions premium, conflict risk, safe havens.
  Wave framing: GEO bot weight is most important; regime = full risk premium engagement.
  Perplexity weighting: high weight — geopolitical claims require external corroboration.

Commodities specialist
  Focuses on: supply/demand imbalance, shipping route disruption, futures curve, energy/metals/agriculture interplay.
  Wave framing: structural and regime waves in commodity-adjacent sectors = core conviction territory.
  Perplexity weighting: looks for corroboration across TECHNICAL + GEO + NEWS bots specifically.

Quant / mean-reversion trader
  Focuses on: statistical edge, signal decay rate, overshoot thesis, normalisation timing, factor exposure.
  Wave framing: escalation and structural = overshoot risk to fade; regime = possible new equilibrium.
  Perplexity weighting: wants high verification score. Contradiction = signal contamination risk.

Risk-on / risk-off allocator
  Focuses on: VIX context, cross-asset correlation, safe haven flows, beta of current positioning.
  Wave framing: regime = forced repositioning; structural = cautious risk-on or risk-off directional shift.
  Perplexity weighting: looks for macro-level verification (FRED, ETF momentum, cross-asset).

Contrarian
  Focuses on: crowded positioning, exhaustion signals, Perplexity contradictions, fading consensus.
  Wave framing: most interested at escalation (crowded thesis) and decay (reversal opportunity).
  Perplexity weighting: specifically seeks contradictions as evidence of overcrowded consensus.

CATALYST LIFECYCLE — how each wave maps to trading-style interpretation:
  SPARK:       Single early signal, unconfirmed. Most styles ignore. Contrarian watches.
  CONFIRMED:   2+ bots aligned, confidence 42%+. Momentum starts watching. Defensive notes it.
  ESCALATION:  2+ bots + verification or cross-asset, confidence 58%+. Most styles forming a view.
  STRUCTURAL:  3+ bots + verified + cross-asset, confidence 72%+, age 2h+. Macro and event-driven engage.
  REGIME:      4+ bots + verified + cross-asset + geo/macro, confidence 85%+, age 6h+. Full evidence threshold.
  DECAY:       Confidence falling, signal weakening. Momentum exits. Contrarian starts watching reversal.
  EXHAUSTION:  Signal spent. Value and quant assess whether dislocation created opportunity.

PERPLEXITY VERIFICATION — always surface how the chosen style weights the current verdict:
  The verification block includes: overall score (-1.0 to +1.0), verdict (supported/mixed/weak/contradicted),
  justification, key risks, extra sources, audit notes, and per-bot verification status.
  Always explain how the chosen trading style specifically weighs the current verification result.

NOTES DASHBOARD CONTEXT:
Reference geopolitical background risks, structural themes, sector vulnerabilities, and tail-risk scenarios
when they are relevant to the question. Always distinguish:
  - Active catalyst: a live signal in the intelligence chain
  - Watch item: being monitored, not yet a signal
  - Background context: informs interpretation but is not itself a signal

DATA ACCESS:
You can reference any Yahoo Finance ticker the user asks about. You are not limited to a predefined list.
If uncertain about a ticker symbol, ask the user to confirm it.
Use sector, industry, macro context, catalyst context, fundamentals, sentiment, and volatility
to contextualise any asset within the chosen trading style.

OUTPUT FORMAT — always use this structure for every response:
  1. Style framing:       "A [trading style] would interpret this as..."
  2. Signal reading:      How this style reads the current wave state, confidence, and bot alignment
  3. Key focus areas:     The 2-3 things this style specifically prioritises in this exact setup
  4. Verification weight: How this style treats the current Perplexity verdict and any contradictions
  5. What they watch:     The specific trigger or signal this style needs to see next
  6. Risk framing:        How this style thinks about the downside in this setup

Be specific. Reference the actual wave state, confidence level, verification verdict, and active
bot signals from the context provided. Do not give generic style descriptions in isolation.

SAFETY RULES (non-negotiable):
  - Never tell the user what to buy or sell
  - Never give investment advice or personalised recommendations
  - Never predict price movements or market outcomes
  - Always frame as "a [style] trader would..." — never as "you should..."
  - Always close every response with: "This is interpretation of a trading style — not financial advice."

AGENT ROUTING:
If the user asks for analyst-style catalyst breakdown, wave state explanation, or bot signal
interpretation rather than trading-style framing, respond:
"That sounds like a question for Sebastian\'s analysis — would you like to switch to Sebastian?"
Do not attempt to answer analyst questions yourself."""


# =============================================================================
#  Internal HTTP helpers
# =============================================================================

async def _call_claude_json(system: str, prompt: str, max_tokens: int = 600) -> Optional[dict]:
    """
    Call Claude and return parsed JSON dict.
    Used by PersonaBot (backend communication layer).
    """
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
                log.warning(f"PersonaBot JSON error {r.status_code}")
                return None
            content = r.json().get("content", [{}])[0].get("text", "{}")
            content = content.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            return json.loads(content)
    except Exception as e:
        log.warning(f"PersonaBot JSON call failed: {e}")
        return None


async def _call_claude_text(
    system:     str,
    messages:   list,
    max_tokens: int = 1100,
) -> Optional[str]:
    """
    Call Claude with conversation history and return raw text.
    Used by PersonaChatBot (user-facing chat).
    """
    if not ANTHROPIC_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=35) as client:
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
                    "messages":   messages,
                },
            )
            if r.status_code != 200:
                log.warning(f"PersonaChatBot error {r.status_code}: {r.text[:200]}")
                return None
            return r.json().get("content", [{}])[0].get("text", "").strip()
    except Exception as e:
        log.warning(f"PersonaChatBot call failed: {e}")
        return None


# =============================================================================
#  Fallbacks for PersonaBot (backend layer)
# =============================================================================

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
        "what_to_watch":  [
            "Monitor for wave escalation",
            "Watch for additional bot alignment",
            "Check cross-asset sector flow",
        ],
    }


def _fallback_wave_transition(packet: dict) -> dict:
    return {
        "title":        f"Wave transition: {packet.get('from_wave')} -> {packet.get('to_wave')}",
        "explanation":  (
            f"Catalyst moved from {packet.get('from_wave')} to {packet.get('to_wave')} wave. "
            f"Reason: {packet.get('reason', 'Multi-bot alignment.')}"
        ),
        "implications": [
            "Higher confidence threshold now applies",
            "Signal persists longer before decay",
        ],
        "watch_next": ["Perplexity verification", "Cross-asset alignment confirmation"],
    }


def _fallback_confidence_update(packet: dict) -> dict:
    old   = packet.get("old_confidence", 0.0)
    new   = packet.get("new_confidence", 0.0)
    delta = new - old
    d_str = "increased" if delta > 0 else "decreased"
    return {
        "title":       f"Confidence {d_str}: {old:.0%} -> {new:.0%}",
        "explanation": f"Confidence {d_str} by {abs(delta):.0%}. Reason: {packet.get('reason', 'Signal update.')}",
        "drivers":     [packet.get("reason", "Signal update")],
        "watch_next":  ["Watch for further verification", "Monitor wave transition conditions"],
    }


def _fallback_daily_brief(packet: dict) -> dict:
    top = packet.get("top_catalysts", [])
    return {
        "headline": f"Daily Brief — {packet.get('date', 'Today')}",
        "sections": [
            {"title": "Top Catalysts", "content": f"{len(top)} active catalysts tracked."},
            {"title": "Market Themes",  "content": ", ".join(packet.get("market_themes", ["No themes identified"]))},
        ],
    }


# =============================================================================
#  PersonaBot — backend communication layer (contract unchanged from live)
# =============================================================================

class PersonaBot:
    """
    Communication layer for Market Brain.
    Translates structured intelligence packets into human-readable summaries.
    Called by the Chief of Staff — never user-facing directly.
    Public method contract is unchanged from the original live file.
    """

    def __init__(self):
        self.persona_prompt = PERSONA_PROMPT

    async def rewrite_catalyst(self, catalyst_packet: dict) -> dict:
        """
        Rewrite a catalyst packet into a structured human summary.
        Input:  CatalystPacket (id, headline, wave, confidence, direction, drivers, etc.)
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
        result = await _call_claude_json(PERSONA_PROMPT, prompt, max_tokens=500)
        return result or _fallback_catalyst_summary(catalyst_packet)

    async def mission_update(self, mission_packet: dict) -> dict:
        """
        Generate a mission progress update.
        Input:  {mission, wave, confidence_change, catalyst_id, reason}
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
        result = await _call_claude_json(PERSONA_PROMPT, prompt, max_tokens=400)
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
        Input:  {catalyst_id, from_wave, to_wave, reason}
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
        result = await _call_claude_json(PERSONA_PROMPT, prompt, max_tokens=400)
        return result or _fallback_wave_transition(transition_packet)

    async def confidence_update(self, confidence_packet: dict) -> dict:
        """
        Explain a confidence change.
        Input:  {catalyst_id, old_confidence, new_confidence, reason}
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
        result = await _call_claude_json(PERSONA_PROMPT, prompt, max_tokens=300)
        return result or _fallback_confidence_update(confidence_packet)

    async def daily_brief(self, brief_packet: dict) -> dict:
        """
        Generate the daily intelligence brief.
        Input:  {date, top_catalysts, missions, positions, market_themes}
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
        result = await _call_claude_json(PERSONA_PROMPT, prompt, max_tokens=700)
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

    async def generate_wave_transition_explanation(
        self,
        catalyst:  dict,
        from_wave: str,
        to_wave:   str,
        reason:    str,
    ) -> dict:
        """Called on every wave promotion/demotion by the CoS."""
        packet = {
            "catalyst_id": catalyst.get("id", ""),
            "title":       catalyst.get("title", catalyst.get("headline", "")),
            "from_wave":   from_wave,
            "to_wave":     to_wave,
            "reason":      reason,
            "direction":   catalyst.get("direction", "neutral"),
            "confidence":  catalyst.get("confidence", 0.0),
        }
        return await self.wave_transition(packet)


# =============================================================================
#  PersonaChatBot — trading-style interpreter (new, user-facing)
# =============================================================================

class PersonaChatBot:
    """
    User-facing trading-style interpreter for Market Brain.

    Completely separate from PersonaBot:
      - Different role: style interpreter, not communication layer
      - Different prompt: PERSONA_CHAT_PROMPT, not PERSONA_PROMPT
      - Different output: natural conversational text, not structured JSON
      - Different calling convention: multi-turn chat(), not one-shot packets

    Called exclusively from POST /api/persona in app.py.
    Never called from the intelligence chain (CoS, sweep engine, relay bot, workflow bot).

    Prompt assembly order per spec:
        context + PERSONA_CHAT_PROMPT + perplexity_verification + user_query
    All context is assembled by the /api/persona endpoint before calling chat().
    """

    def _build_system(
        self,
        live_prices:   str,
        catalysts:     str,
        verification:  str = "",
        extra_context: str = "",
        mode:          str = "auto",
    ) -> str:
        """
        Assemble the full system prompt per spec.
        Order: context -> PERSONA_CHAT_PROMPT -> verification -> extra_context
        """
        mode_note = {
            "beginner": (
                "\nLANGUAGE: The user is new to markets. Use plain English. "
                "Explain trading style terminology the first time you use it."
            ),
            "expert": (
                "\nLANGUAGE: The user is experienced. Use technical terminology freely. "
                "Skip basic explanations of style concepts."
            ),
            "auto": (
                "\nLANGUAGE: Calibrate to the question. Plain English first, "
                "with technical depth for users who clearly have market experience."
            ),
        }.get(mode, "")

        context_block = (
            f"LIVE_MARKET_PRICES (fetched {time.strftime('%H:%M UTC')}):\n"
            f"{live_prices}\n\n"
            f"ACTIVE_INTELLIGENCE:\n"
            f"{catalysts}\n\n"
        )

        verification_block = (
            f"\nPERPLEXITY_VERIFICATION:\n{verification}\n"
            if verification and verification.strip() else ""
        )

        extra_block = (
            f"\nADDITIONAL_CONTEXT:\n{extra_context}\n"
            if extra_context and extra_context.strip() else ""
        )

        return (
            context_block
            + PERSONA_CHAT_PROMPT
            + mode_note
            + verification_block
            + extra_block
        )

    async def chat(
        self,
        question:      str,
        history:       list,
        live_prices:   str,
        catalysts:     str,
        verification:  str = "",
        extra_context: str = "",
        mode:          str = "auto",
    ) -> str:
        """
        Main entry point for Persona Bot conversational chat.
        Called from POST /api/persona in app.py.

        Args:
            question:      The user question (stripped by caller).
            history:       Previous turns [{role, content}]. Last 12 used.
            live_prices:   Live price block fetched by /api/persona endpoint.
            catalysts:     Catalyst context block fetched by /api/persona endpoint.
            verification:  Perplexity verification block (string, optional).
            extra_context: Extra context from frontend (focused catalyst, asset focus, etc.).
            mode:          beginner | expert | auto

        Returns:
            str: Persona Bot response text.
        """
        system = self._build_system(
            live_prices=live_prices,
            catalysts=catalysts,
            verification=verification,
            extra_context=extra_context,
            mode=mode,
        )

        messages = []
        for msg in history[-12:]:
            role    = msg.get("role", "")
            content = msg.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": str(content)[:2000]})
        messages.append({"role": "user", "content": question})

        response = await _call_claude_text(system, messages, max_tokens=1100)

        if not response:
            return (
                "I'm having trouble connecting right now. "
                "The intelligence data is still available — "
                "try rephrasing your question, or switch to Sebastian for analysis."
            )

        return response


# =============================================================================
#  Module-level singletons
# =============================================================================

_persona_bot:      Optional[PersonaBot]     = None
_persona_chat_bot: Optional[PersonaChatBot] = None


def get_persona_bot() -> PersonaBot:
    """Singleton accessor for PersonaBot — the CoS communication layer."""
    global _persona_bot
    if _persona_bot is None:
        _persona_bot = PersonaBot()
    return _persona_bot


def get_persona_chat_bot() -> PersonaChatBot:
    """Singleton accessor for PersonaChatBot — the user-facing trading-style interpreter."""
    global _persona_chat_bot
    if _persona_chat_bot is None:
        _persona_chat_bot = PersonaChatBot()
    return _persona_chat_bot
