"""
Market Brain Research Bots
─────────────────────────────
Drop the research_bots/ folder into your repo root, then:

    from research_bots.orchestrator import run_all_bots
    result = await run_all_bots("AAPL", asset_meta)
"""

from .orchestrator import run_all_bots, run_single_bot, BotResearch
from .base import BotResult, ResearchBot

__all__ = ["run_all_bots", "run_single_bot", "BotResearch", "BotResult", "ResearchBot"]
