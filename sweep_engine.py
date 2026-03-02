"""
Market Brain — Sweep Engine v2
────────────────────────────────
Runs as a separate Railway service alongside app.py.

What changed from bot_engine.py:
  ✗ Removed: generate_signals() pseudo-random simulation engine
  ✗ Removed: place_trade() / add_watch() paper trading logic
  ✗ Removed: Math.sin() seed-based scoring
  ✓ Added:   Real sweep schedule (from internal docs) using APScheduler
  ✓ Added:   Research bots run for real data per sweep
  ✓ Added:   Bot output posted to /api/cos/signal (Chief of Staff)
  ✓ Added:   Asset tier logic — Tier 1 on every sweep, Tier 2/3 on full sweeps only
  ✓ Added:   Fast sweep mode (open sweeps) — Technicals + News only
  ✓ Kept:    Auth + price fetching + status API for dashboard

Environment variables (.env or Railway):
  MB_API_URL         = https://your-app.railway.app
  MB_BOT_EMAIL       = sweeper@marketbrain.ai
  MB_BOT_PASSWORD    = SweeperPass123!
  ANTHROPIC_API_KEY  = sk-ant-...   (used by MARI in app.py, not sweep engine)
  GNEWS_API_KEY      = ...
  FMP_API_KEY        = ...
  AV_API_KEY         = ...
  FRED_API_KEY       = ...
  POLYGON_API_KEY    = ...
  SWEEP_PORT         = 8001

Requirements:
  pip install fastapi uvicorn httpx apscheduler python-dotenv
  (research bots: same requirements as app.py — httpx already installed)
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mb.sweep")

# ── Config ──────────────────────────────────────────────────────
MB_API_URL      = os.getenv("MB_API_URL",      "http://localhost:8000")
MB_BOT_EMAIL    = os.getenv("MB_BOT_EMAIL",    "sweeper@marketbrain.ai")
MB_BOT_PASSWORD = os.getenv("MB_BOT_PASSWORD", "SweeperPass123!")
SWEEP_PORT      = int(os.getenv("SWEEP_PORT",  "8001"))

# ── Load research bots ──────────────────────────────────────────
# Assumes research_bots/ folder is in the same repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "research_bots"))
try:
    from orchestrator import run_all_bots, get_bots, BotResearch
    BOTS_AVAILABLE = True
    log.info("Research bots loaded")
except ImportError as e:
    BOTS_AVAILABLE = False
    log.error(f"Could not load research bots: {e}")


# ── Asset universe ──────────────────────────────────────────────
# Tier 1: swept on every relevant sweep cycle
# Tier 2: full sweeps only (pre-market, close, weekend)
# Tier 3: weekly only (Sunday 02:00)
# Dynamic promotion: assets on user watchlist → Tier 1 for that day

TIER_1 = [
    # US Mega-cap + most active
    {"ticker":"NVDA",    "name":"NVIDIA",          "sector":"Technology",  "sub":"Semiconductors","cap":"Large","vol":"Low",   "asset_type":"stock"},
    {"ticker":"AAPL",    "name":"Apple",            "sector":"Technology",  "sub":"Hardware",      "cap":"Large","vol":"Low",   "asset_type":"stock"},
    {"ticker":"MSFT",    "name":"Microsoft",        "sector":"Technology",  "sub":"Software",      "cap":"Large","vol":"Low",   "asset_type":"stock"},
    {"ticker":"GOOGL",   "name":"Alphabet",         "sector":"Technology",  "sub":"Software",      "cap":"Large","vol":"Low",   "asset_type":"stock"},
    {"ticker":"META",    "name":"Meta Platforms",   "sector":"Technology",  "sub":"Social Media",  "cap":"Large","vol":"Med",   "asset_type":"stock"},
    {"ticker":"AMZN",    "name":"Amazon",           "sector":"Technology",  "sub":"Cloud",         "cap":"Large","vol":"Med",   "asset_type":"stock"},
    {"ticker":"TSLA",    "name":"Tesla",            "sector":"Consumer",    "sub":"EV",            "cap":"Large","vol":"High",  "asset_type":"stock"},
    {"ticker":"PLTR",    "name":"Palantir",         "sector":"Technology",  "sub":"AI / ML",       "cap":"Large","vol":"High",  "asset_type":"stock"},
    {"ticker":"AMD",     "name":"AMD",              "sector":"Technology",  "sub":"Semiconductors","cap":"Large","vol":"Med",   "asset_type":"stock"},
    {"ticker":"CRWD",    "name":"CrowdStrike",      "sector":"Technology",  "sub":"Cybersecurity", "cap":"Large","vol":"Med",   "asset_type":"stock"},
    {"ticker":"JPM",     "name":"JPMorgan Chase",   "sector":"Finance",     "sub":"Banking",       "cap":"Large","vol":"Low",   "asset_type":"stock"},
    {"ticker":"GS",      "name":"Goldman Sachs",    "sector":"Finance",     "sub":"Banking",       "cap":"Large","vol":"Med",   "asset_type":"stock"},
    {"ticker":"XOM",     "name":"ExxonMobil",       "sector":"Energy",      "sub":"Oil & Gas",     "cap":"Large","vol":"Low",   "asset_type":"stock"},
    {"ticker":"LLY",     "name":"Eli Lilly",        "sector":"Healthcare",  "sub":"Pharma",        "cap":"Large","vol":"Med",   "asset_type":"stock"},
    {"ticker":"NVO",     "name":"Novo Nordisk",     "sector":"Healthcare",  "sub":"Pharma",        "cap":"Large","vol":"Med",   "asset_type":"stock"},
    {"ticker":"RKLB",    "name":"Rocket Lab",       "sector":"Space",       "sub":"Launch",        "cap":"Small","vol":"High",  "asset_type":"stock"},
    {"ticker":"LMT",     "name":"Lockheed Martin",  "sector":"Space",       "sub":"Defence",       "cap":"Large","vol":"Low",   "asset_type":"stock"},
    # Crypto — always Tier 1
    {"ticker":"BTC-USD", "name":"Bitcoin",          "sector":"Crypto",      "sub":"Layer 1",       "cap":"Large","vol":"Extreme","asset_type":"crypto"},
    {"ticker":"ETH-USD", "name":"Ethereum",         "sector":"Crypto",      "sub":"Layer 1",       "cap":"Large","vol":"Extreme","asset_type":"crypto"},
    {"ticker":"SOL-USD", "name":"Solana",           "sector":"Crypto",      "sub":"Layer 1",       "cap":"Mid",  "vol":"Extreme","asset_type":"crypto"},
    {"ticker":"XRP-USD", "name":"XRP",              "sector":"Crypto",      "sub":"Payments",      "cap":"Large","vol":"Extreme","asset_type":"crypto"},
    # Indices / ETFs
    {"ticker":"SPY",     "name":"S&P 500 ETF",      "sector":"ETF",         "sub":"Index",         "cap":"Large","vol":"Low",   "asset_type":"etf"},
    {"ticker":"QQQ",     "name":"Nasdaq 100 ETF",   "sector":"ETF",         "sub":"Index",         "cap":"Large","vol":"Low",   "asset_type":"etf"},
    # UK blue chips
    {"ticker":"AZN.L",   "name":"AstraZeneca",      "sector":"UK",          "sub":"Pharma",        "cap":"Large","vol":"Low",   "asset_type":"stock"},
    {"ticker":"SHEL.L",  "name":"Shell",            "sector":"UK",          "sub":"Oil & Gas",     "cap":"Large","vol":"Low",   "asset_type":"stock"},
    {"ticker":"BAE.L",   "name":"BAE Systems",      "sector":"UK",          "sub":"Defence",       "cap":"Large","vol":"Low",   "asset_type":"stock"},
    {"ticker":"RR.L",    "name":"Rolls-Royce",      "sector":"UK",          "sub":"Aerospace",     "cap":"Large","vol":"Med",   "asset_type":"stock"},
    # Commodities
    {"ticker":"GC=F",    "name":"Gold Futures",     "sector":"Commodities", "sub":"Precious Metals","cap":"Large","vol":"Med",  "asset_type":"commodity"},
    {"ticker":"CL=F",    "name":"Crude Oil WTI",    "sector":"Commodities", "sub":"Energy",        "cap":"Large","vol":"Med",   "asset_type":"commodity"},
    {"ticker":"BZ=F",    "name":"Brent Crude",      "sector":"Commodities", "sub":"Energy",        "cap":"Large","vol":"Med",   "asset_type":"commodity"},
    {"ticker":"NG=F",    "name":"Natural Gas",       "sector":"Commodities", "sub":"Energy",        "cap":"Large","vol":"High",  "asset_type":"commodity"},
    # Shipping — Red Sea / Suez / oil tanker route plays
    {"ticker":"ZIM",     "name":"ZIM Integrated",   "sector":"Shipping",    "sub":"Container",     "cap":"Mid",  "vol":"High",  "asset_type":"stock"},
    {"ticker":"FRO",     "name":"Frontline",         "sector":"Shipping",    "sub":"Oil Tanker",    "cap":"Mid",  "vol":"High",  "asset_type":"stock"},
    {"ticker":"STNG",    "name":"Scorpio Tankers",   "sector":"Shipping",    "sub":"Oil Tanker",    "cap":"Mid",  "vol":"High",  "asset_type":"stock"},
    # Defence — Middle East escalation
    {"ticker":"RTX",     "name":"RTX (Raytheon)",    "sector":"Defence",     "sub":"Missiles",      "cap":"Large","vol":"Low",   "asset_type":"stock"},
    {"ticker":"NOC",     "name":"Northrop Grumman",  "sector":"Defence",     "sub":"Defence",       "cap":"Large","vol":"Low",   "asset_type":"stock"},
    # Forex
    {"ticker":"EURUSD=X","name":"EUR/USD",           "sector":"Forex",       "sub":"Major Pair",    "cap":"Large","vol":"Low",   "asset_type":"forex"},
    {"ticker":"GBPUSD=X","name":"GBP/USD",           "sector":"Forex",       "sub":"Major Pair",    "cap":"Large","vol":"Low",   "asset_type":"forex"},
]

TIER_2 = [
    {"ticker":"COIN",    "name":"Coinbase",         "sector":"Crypto",      "sub":"Exchange",      "cap":"Mid",  "vol":"VHigh", "asset_type":"stock"},
    {"ticker":"MSTR",    "name":"MicroStrategy",    "sector":"Crypto",      "sub":"BTC Treasury",  "cap":"Mid",  "vol":"Extreme","asset_type":"stock"},
    {"ticker":"IONQ",    "name":"IonQ",             "sector":"Technology",  "sub":"Quantum",       "cap":"Small","vol":"High",  "asset_type":"stock"},
    {"ticker":"SOUN",    "name":"SoundHound AI",    "sector":"Technology",  "sub":"AI / ML",       "cap":"Micro","vol":"VHigh", "asset_type":"stock"},
    {"ticker":"ASTS",    "name":"AST SpaceMobile",  "sector":"Space",       "sub":"Satellite",     "cap":"Small","vol":"VHigh", "asset_type":"stock"},
    {"ticker":"AVGO",    "name":"Broadcom",         "sector":"Technology",  "sub":"Semiconductors","cap":"Large","vol":"Low",   "asset_type":"stock"},
    {"ticker":"MU",      "name":"Micron",           "sector":"Technology",  "sub":"Semiconductors","cap":"Large","vol":"Med",   "asset_type":"stock"},
    {"ticker":"MRNA",    "name":"Moderna",          "sector":"Healthcare",  "sub":"Pharma",        "cap":"Mid",  "vol":"High",  "asset_type":"stock"},
    {"ticker":"ABBV",    "name":"AbbVie",           "sector":"Healthcare",  "sub":"Pharma",        "cap":"Large","vol":"Low",   "asset_type":"stock"},
    {"ticker":"NEE",     "name":"NextEra Energy",   "sector":"Energy",      "sub":"Renewables",    "cap":"Large","vol":"Low",   "asset_type":"stock"},
    {"ticker":"CCJ",     "name":"Cameco",           "sector":"Energy",      "sub":"Uranium",       "cap":"Mid",  "vol":"Med",   "asset_type":"stock"},
    {"ticker":"FCX",     "name":"Freeport-McMoRan", "sector":"Metals",      "sub":"Copper",        "cap":"Large","vol":"Med",   "asset_type":"stock"},
    {"ticker":"GLD",     "name":"SPDR Gold ETF",    "sector":"Metals",      "sub":"Gold",          "cap":"Large","vol":"Low",   "asset_type":"etf"},
    {"ticker":"ALB",     "name":"Albemarle",        "sector":"Minerals",    "sub":"Lithium",       "cap":"Mid",  "vol":"High",  "asset_type":"stock"},
    {"ticker":"NFLX",    "name":"Netflix",          "sector":"Consumer",    "sub":"Streaming",     "cap":"Large","vol":"Med",   "asset_type":"stock"},
    {"ticker":"SHOP",    "name":"Shopify",          "sector":"Consumer",    "sub":"E-Commerce",    "cap":"Large","vol":"Med",   "asset_type":"stock"},
    {"ticker":"HSBA.L",  "name":"HSBC",             "sector":"UK",          "sub":"Banking",       "cap":"Large","vol":"Low",   "asset_type":"stock"},
    {"ticker":"BP.L",    "name":"BP",               "sector":"UK",          "sub":"Oil & Gas",     "cap":"Large","vol":"Low",   "asset_type":"stock"},
    {"ticker":"VOD.L",   "name":"Vodafone",         "sector":"UK",          "sub":"Telecom",       "cap":"Large","vol":"Low",   "asset_type":"stock"},
    {"ticker":"USDJPY=X","name":"USD/JPY",          "sector":"Forex",       "sub":"Major Pair",    "cap":"Large","vol":"Low",   "asset_type":"forex"},
    {"ticker":"BABA",    "name":"Alibaba",          "sector":"Global",      "sub":"E-Commerce",    "cap":"Large","vol":"High",  "asset_type":"stock"},
    {"ticker":"NIO",     "name":"NIO",              "sector":"Consumer",    "sub":"EV",            "cap":"Small","vol":"High",  "asset_type":"stock"},
    {"ticker":"RIVN",    "name":"Rivian",           "sector":"Consumer",    "sub":"EV",            "cap":"Mid",  "vol":"VHigh", "asset_type":"stock"},
    {"ticker":"DOGE-USD","name":"Dogecoin",         "sector":"Crypto",      "sub":"Meme",          "cap":"Mid",  "vol":"Extreme","asset_type":"crypto"},
    {"ticker":"AVAX-USD","name":"Avalanche",        "sector":"Crypto",      "sub":"Layer 1",       "cap":"Small","vol":"Extreme","asset_type":"crypto"},
    # Shipping — extended coverage
    {"ticker":"MAERSK-B.CO","name":"Maersk",        "sector":"Shipping",    "sub":"Container",     "cap":"Large","vol":"Med",   "asset_type":"stock"},
    {"ticker":"DHT",     "name":"DHT Holdings",     "sector":"Shipping",    "sub":"Oil Tanker",    "cap":"Small","vol":"High",  "asset_type":"stock"},
    {"ticker":"INSW",    "name":"Intl Seaways",     "sector":"Shipping",    "sub":"Oil Tanker",    "cap":"Small","vol":"High",  "asset_type":"stock"},
    {"ticker":"DAC",     "name":"Danaos Corp",      "sector":"Shipping",    "sub":"Container",     "cap":"Small","vol":"High",  "asset_type":"stock"},
    # Marine insurance / Lloyd's proxies
    {"ticker":"BEZ.L",   "name":"Beazley",          "sector":"Insurance",   "sub":"Marine/Spec",   "cap":"Mid",  "vol":"Med",   "asset_type":"stock"},
    {"ticker":"LMRK.L",  "name":"Lloyds of London proxy","sector":"Insurance","sub":"Marine",      "cap":"Large","vol":"Low",   "asset_type":"stock"},
    # Alternative supply chain / air freight beneficiaries
    {"ticker":"FDX",     "name":"FedEx",            "sector":"Logistics",   "sub":"Air Freight",   "cap":"Large","vol":"Med",   "asset_type":"stock"},
    {"ticker":"UPS",     "name":"UPS",              "sector":"Logistics",   "sub":"Air Freight",   "cap":"Large","vol":"Low",   "asset_type":"stock"},
    {"ticker":"EXPD",    "name":"Expeditors Intl",  "sector":"Logistics",   "sub":"Freight",       "cap":"Large","vol":"Low",   "asset_type":"stock"},
    # LNG — energy supply disruption
    {"ticker":"LNG",     "name":"Cheniere Energy",  "sector":"Energy",      "sub":"LNG",           "cap":"Large","vol":"Med",   "asset_type":"stock"},
    {"ticker":"TELL",    "name":"Tellurian",        "sector":"Energy",      "sub":"LNG",           "cap":"Small","vol":"VHigh", "asset_type":"stock"},
    # Middle East defence extended
    {"ticker":"GD",      "name":"General Dynamics", "sector":"Defence",     "sub":"Defence",       "cap":"Large","vol":"Low",   "asset_type":"stock"},
    {"ticker":"HII",     "name":"Huntington Ingalls","sector":"Defence",    "sub":"Naval",         "cap":"Large","vol":"Low",   "asset_type":"stock"},
    {"ticker":"BAESY",   "name":"BAE Systems US ADR","sector":"Defence",    "sub":"Defence",       "cap":"Large","vol":"Low",   "asset_type":"stock"},
    # Commodity safe havens
    {"ticker":"SI=F",    "name":"Silver Futures",   "sector":"Commodities", "sub":"Precious Metals","cap":"Large","vol":"High", "asset_type":"commodity"},
    {"ticker":"HG=F",    "name":"Copper Futures",   "sector":"Commodities", "sub":"Base Metals",   "cap":"Large","vol":"Med",   "asset_type":"commodity"},
]

# Bot selection by asset type — matches internal doc section 2.1
BOTS_BY_ASSET_TYPE = {
    "stock":     ["NewsBot", "EarningsBot", "MacroBot", "InsiderBot", "FundamentalsBot", "TechnicalLevelsBot", "AnalystBot", "GeoBot"],
    "etf":       ["MacroBot", "NewsBot", "TechnicalLevelsBot", "GeoBot"],
    "crypto":    ["MacroBot", "NewsBot", "TechnicalLevelsBot"],
    "forex":     ["MacroBot", "TechnicalLevelsBot", "GeoBot"],
    "commodity": ["MacroBot", "NewsBot", "TechnicalLevelsBot", "GeoBot"],
}

# Geopolitical sectors — GeoBot always runs on these
GEO_SENSITIVE_SECTORS = {
    "Shipping", "Defence", "Energy", "Commodities", "Insurance", "Logistics"
}

# Dedicated geo scan assets — run independently of normal asset sweeps
GEO_SCAN_ASSETS = [
    {"ticker": "GEO:WORLD",      "name": "Global Geo Scan",         "sector": "Geopolitical", "sub": "Global",       "asset_type": "geo"},
    {"ticker": "GEO:MIDDLEEAST", "name": "Middle East / Iran",      "sector": "Geopolitical", "sub": "MiddleEast",   "asset_type": "geo"},
    {"ticker": "GEO:SHIPPING",   "name": "Shipping Route Monitor",  "sector": "Geopolitical", "sub": "Shipping",     "asset_type": "geo"},
    {"ticker": "GEO:ENERGY",     "name": "Energy Supply Monitor",   "sector": "Geopolitical", "sub": "Energy",       "asset_type": "geo"},
    {"ticker": "GEO:DEFENCE",    "name": "Defence / Conflict",      "sector": "Geopolitical", "sub": "Defence",      "asset_type": "geo"},
    {"ticker": "GEO:SUPPLYCHAIN","name": "Supply Chain Monitor",    "sector": "Geopolitical", "sub": "Logistics",    "asset_type": "geo"},
]

# Fast sweep bots — market open sweeps only (08:15, 14:45)
FAST_SWEEP_BOTS = ["TechnicalLevelsBot", "NewsBot", "GeoBot"]


# ── State ────────────────────────────────────────────────────────
state: Dict = {
    "token":            None,
    "sweep_count":      0,
    "last_sweep":       None,
    "last_sweep_name":  None,
    "last_sweep_assets":0,
    "last_sweep_ms":    0,
    "errors_today":     0,
    "status":           "starting",
    "started_at":       datetime.now(timezone.utc).isoformat(),
    "sweep_log":        [],   # last 50 sweep records
}

SWEEP_LOG_MAX = 50


def _log_sweep(name: str, assets_swept: int, duration_ms: float, errors: int):
    state["sweep_log"].insert(0, {
        "name":       name,
        "assets":     assets_swept,
        "duration_ms":round(duration_ms),
        "errors":     errors,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
    })
    state["sweep_log"] = state["sweep_log"][:SWEEP_LOG_MAX]
    state["sweep_count"]       += 1
    state["last_sweep"]         = datetime.now(timezone.utc).isoformat()
    state["last_sweep_name"]    = name
    state["last_sweep_assets"]  = assets_swept
    state["last_sweep_ms"]      = round(duration_ms)
    state["errors_today"]      += errors


# ── Auth ─────────────────────────────────────────────────────────
async def mb_login() -> bool:
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.post(f"{MB_API_URL}/api/auth/register", json={
                "name": "Sweep Engine", "email": MB_BOT_EMAIL, "password": MB_BOT_PASSWORD,
            })
            if r.status_code in (200, 201):
                state["token"] = r.json()["token"]
                log.info("Sweep engine registered")
                return True
        except Exception:
            pass
        try:
            r = await client.post(f"{MB_API_URL}/api/auth/login", json={
                "email": MB_BOT_EMAIL, "password": MB_BOT_PASSWORD,
            })
            if r.status_code == 200:
                state["token"] = r.json()["token"]
                log.info("Sweep engine logged in")
                return True
        except Exception as e:
            log.error(f"Auth failed: {e}")
    return False


async def _get_watchlist_tickers() -> List[str]:
    """Pull user watchlists from app.py to promote assets to Tier 1."""
    # In future: call /api/universe or a watchlist endpoint
    # For now: return empty — Tier 1/2 static lists cover primary assets
    return []


# ── Bot selection ─────────────────────────────────────────────────
def _select_bots_for_asset(asset_meta: dict, fast_sweep: bool, available_bots: list) -> list:
    """Return the subset of loaded bots appropriate for this asset + sweep type."""
    asset_type  = asset_meta.get("asset_type", "stock")
    # Geo assets — only GeoBot
    if asset_type == "geo":
        return [b for b in available_bots if b.name == "GeoBot"]
    # Geo-sensitive sectors always include GeoBot even in fast sweeps
    sector = asset_meta.get("sector", "")
    if fast_sweep:
        names = FAST_SWEEP_BOTS if sector not in GEO_SENSITIVE_SECTORS else FAST_SWEEP_BOTS
    else:
        names = BOTS_BY_ASSET_TYPE.get(asset_type, [])
        if sector in GEO_SENSITIVE_SECTORS and "GeoBot" not in names:
            names = names + ["GeoBot"]
    return [b for b in available_bots if b.name in names]


# ── Core sweep runner ─────────────────────────────────────────────
async def _run_sweep(
    name:       str,
    assets:     List[dict],
    fast_sweep: bool = False,
    batch_delay: float = 0.5,   # seconds between assets to avoid rate limits
) -> None:
    if not BOTS_AVAILABLE:
        log.warning(f"Sweep {name} skipped — research bots not available")
        return

    t_start    = time.time()
    errors     = 0
    swept      = 0
    all_bots   = get_bots()

    log.info(f"▶ Sweep: {name} | {len(assets)} assets | fast={fast_sweep}")
    state["status"] = f"sweeping: {name}"

    for asset in assets:
        ticker = asset["ticker"]
        bots   = _select_bots_for_asset(asset, fast_sweep, all_bots)
        if not bots:
            continue
        try:
            result = await run_all_bots(
                ticker,
                asset_meta=asset,
                bots=bots,
                timeout=20.0,
                post_to_cos=True,   # ← this is the sweep engine; always post to CoS
            )
            if result.errors:
                errors += len(result.errors)
            swept += 1
            log.debug(f"  {ticker}: conf={result.overall_confidence:.2f} bull={len(result.bull_factors)} bear={len(result.bear_factors)}")
        except Exception as e:
            log.error(f"  Sweep error {ticker}: {e}")
            errors += 1

        await asyncio.sleep(batch_delay)

    duration_ms = (time.time() - t_start) * 1000
    _log_sweep(name, swept, duration_ms, errors)
    state["status"] = "idle"
    log.info(f"✓ Sweep {name} complete | {swept} assets | {duration_ms/1000:.1f}s | {errors} errors")


# ── Sweep schedule ────────────────────────────────────────────────
# All times UK local (Europe/London). APScheduler handles BST/GMT automatically.
# Schedule mirrors internal doc section 3.

async def sweep_geo():
    """
    Geo Intelligence Sweep — runs every 2 hours, 24/7.
    Independently scans geopolitical events and posts signals to CoS.
    Does not depend on market hours — geopolitical events don't wait.
    GeoBot scans: Middle East, Shipping routes, Energy supply,
    Defence escalation, Supply chain disruption.
    """
    await _run_sweep(
        "Geo Intelligence Sweep",
        assets=GEO_SCAN_ASSETS,
        fast_sweep=False,
    )

async def sweep_overnight():
    """02:00 UK — Asia mid-session, US post-market winding down."""
    await _run_sweep(
        "Overnight Sweep",
        assets=[a for a in TIER_1 if a["asset_type"] in ("stock", "crypto") or a["sector"] in ("ETF",)],
        fast_sweep=False,
    )

async def sweep_uk_premarket():
    """07:00 UK — UK macro releases, 1hr before London open."""
    uk_eu = [a for a in TIER_1 + TIER_2 if a.get("sector") in ("UK", "European") or
              a["ticker"].endswith((".L", ".PA", ".DE", ".AS")) or
              a["asset_type"] in ("forex", "commodity")]
    await _run_sweep("UK Pre-Market Sweep", assets=uk_eu, fast_sweep=False)

async def sweep_uk_open():
    """08:15 UK — LSE open 15 minutes. Fast bots only."""
    uk_tier1 = [a for a in TIER_1 if a.get("sector") in ("UK", "European") or
                a["ticker"].endswith((".L",))]
    await _run_sweep("UK Market Open Sweep", assets=uk_tier1, fast_sweep=True)

async def sweep_uk_midsession():
    """11:30 UK — UK midday, US pre-market forming direction."""
    await _run_sweep("UK Mid-Session Sweep", assets=TIER_1, fast_sweep=False)

async def sweep_us_premarket():
    """12:00 UK — Most important sweep. Full reset for all US assets."""
    us_and_crypto = [a for a in TIER_1 + TIER_2 if
                     a["asset_type"] in ("crypto",) or
                     not a["ticker"].endswith((".L", ".PA", ".DE", ".AS", "=X"))]
    await _run_sweep("US Pre-Market Sweep", assets=us_and_crypto, fast_sweep=False)

async def sweep_us_open():
    """14:45 UK — NYSE open 15 minutes. Fast bots only."""
    us_tier1 = [a for a in TIER_1 if
                a["asset_type"] in ("crypto",) or
                not a["ticker"].endswith((".L", ".PA", ".DE", ".AS", "=X"))]
    await _run_sweep("US Market Open Sweep", assets=us_tier1, fast_sweep=True)

async def sweep_uk_close():
    """16:45 UK — LSE closed 15 minutes ago. Definitive UK/EU snapshot."""
    uk_eu_t2 = [a for a in TIER_1 + TIER_2 if
                a.get("sector") in ("UK", "European") or
                a["ticker"].endswith((".L", ".PA", ".DE", ".AS"))]
    await _run_sweep("UK Market Close Sweep", assets=uk_eu_t2, fast_sweep=False)

async def sweep_us_midsession():
    """17:00 UK — US 2.5hrs in. Sector rotation and volume trends."""
    us_tier1 = [a for a in TIER_1 if
                not a["ticker"].endswith((".L", ".PA", ".DE", ".AS"))]
    await _run_sweep("US Mid-Session Sweep", assets=us_tier1, fast_sweep=False)

async def sweep_us_close():
    """21:15 UK — NYSE closed 15 minutes ago. Definitive US snapshot."""
    non_eu = [a for a in TIER_1 + TIER_2 if
              not a["ticker"].endswith((".L", ".PA", ".DE", ".AS"))]
    await _run_sweep("US Market Close Sweep", assets=non_eu, fast_sweep=False)

async def sweep_postmarket():
    """23:00 UK — Post-market 2hrs in. After-hours earnings + crypto Asia prep."""
    post = [a for a in TIER_1 if
            a["asset_type"] == "crypto" or
            (not a["ticker"].endswith((".L", ".PA", ".DE", ".AS")) and
             a["asset_type"] == "stock")]
    await _run_sweep("Post-Market Sweep", assets=post, fast_sweep=False)

async def sweep_weekend_prep():
    """Sunday 23:30 — Futures reopen. Weekend news absorbed. Full reset."""
    await _run_sweep("Weekend Prep Sweep", assets=TIER_1 + TIER_2, fast_sweep=False)

async def sweep_tier3_weekly():
    """Sunday 02:00 — Quietest window. Tier 3 would go here (not yet in universe)."""
    # Tier 3 (~1000+ assets) requires a larger universe source.
    # For now: run Tier 2 with full bots as the 'quiet window' sweep.
    await _run_sweep("Tier-3 Weekly Sweep", assets=TIER_2, fast_sweep=False)


# ── Manual trigger support ────────────────────────────────────────
async def sweep_manual(tickers: Optional[List[str]] = None):
    """Manually sweep specific tickers or all Tier 1."""
    if tickers:
        all_assets = TIER_1 + TIER_2
        assets     = [a for a in all_assets if a["ticker"] in tickers]
        if not assets:
            assets = [{"ticker": t, "name": t, "sector": "", "sub": "",
                       "cap": "Large", "vol": "Med", "asset_type": "stock"}
                      for t in tickers]
    else:
        assets = TIER_1
    await _run_sweep("Manual Sweep", assets=assets, fast_sweep=False, batch_delay=0.3)


# ── FastAPI status dashboard ──────────────────────────────────────
app = FastAPI(title="Market Brain Sweep Engine", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/sweep/status")
def sweep_status():
    return {
        "status":            state["status"],
        "started_at":        state["started_at"],
        "sweep_count":       state["sweep_count"],
        "last_sweep":        state["last_sweep"],
        "last_sweep_name":   state["last_sweep_name"],
        "last_sweep_assets": state["last_sweep_assets"],
        "last_sweep_ms":     state["last_sweep_ms"],
        "errors_today":      state["errors_today"],
        "bots_available":    BOTS_AVAILABLE,
        "tier1_count":       len(TIER_1),
        "tier2_count":       len(TIER_2),
        "mb_api_url":        MB_API_URL,
    }


@app.get("/sweep/log")
def sweep_log():
    return {"sweeps": state["sweep_log"]}


@app.post("/sweep/trigger")
async def trigger_sweep(sweep_name: str = "manual", tickers: str = ""):
    """
    Manually trigger a sweep.
    ?sweep_name=us_premarket | uk_premarket | overnight | manual
    ?tickers=NVDA,AAPL,BTC-USD (comma-separated, for manual only)
    """
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()] if tickers else []

    sweep_map = {
        "overnight":    sweep_overnight,
        "uk_premarket": sweep_uk_premarket,
        "uk_open":      sweep_uk_open,
        "uk_midsession":sweep_uk_midsession,
        "us_premarket": sweep_us_premarket,
        "us_open":      sweep_us_open,
        "uk_close":     sweep_uk_close,
        "us_midsession":sweep_us_midsession,
        "us_close":     sweep_us_close,
        "postmarket":   sweep_postmarket,
        "weekend":      sweep_weekend_prep,
        "manual":       lambda: sweep_manual(ticker_list or None),
    }

    fn = sweep_map.get(sweep_name)
    if not fn:
        return {"ok": False, "error": f"Unknown sweep: {sweep_name}"}

    asyncio.create_task(fn())
    return {"ok": True, "sweep": sweep_name, "tickers": ticker_list}


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "sweep_engine": "running",
        "bots_available": BOTS_AVAILABLE,
        "timestamp": int(time.time()),
    }


# ── Scheduler setup ───────────────────────────────────────────────
scheduler = AsyncIOScheduler(timezone="Europe/London")

def _add_sweeps():
    # All times UK local (Europe/London) — APScheduler handles BST/GMT
    scheduler.add_job(sweep_overnight,     CronTrigger(hour=2,  minute=0,  timezone="Europe/London"), id="overnight")
    scheduler.add_job(sweep_uk_premarket,  CronTrigger(hour=7,  minute=0,  timezone="Europe/London"), id="uk_premarket")
    scheduler.add_job(sweep_uk_open,       CronTrigger(hour=8,  minute=15, timezone="Europe/London"), id="uk_open")
    scheduler.add_job(sweep_uk_midsession, CronTrigger(hour=11, minute=30, timezone="Europe/London"), id="uk_midsession")
    scheduler.add_job(sweep_us_premarket,  CronTrigger(hour=12, minute=0,  timezone="Europe/London"), id="us_premarket")
    scheduler.add_job(sweep_us_open,       CronTrigger(hour=14, minute=45, timezone="Europe/London"), id="us_open")
    scheduler.add_job(sweep_uk_close,      CronTrigger(hour=16, minute=45, timezone="Europe/London"), id="uk_close")
    scheduler.add_job(sweep_us_midsession, CronTrigger(hour=17, minute=0,  timezone="Europe/London"), id="us_midsession")
    scheduler.add_job(sweep_us_close,      CronTrigger(hour=21, minute=15, timezone="Europe/London"), id="us_close")
    scheduler.add_job(sweep_postmarket,    CronTrigger(hour=23, minute=0,  timezone="Europe/London"), id="postmarket")
    # Weekend sweeps
    scheduler.add_job(sweep_geo,           CronTrigger(hour="*/2", minute=0, timezone="Europe/London"), id="geo_intel")
    scheduler.add_job(sweep_weekend_prep,  CronTrigger(day_of_week="sun", hour=23, minute=30, timezone="Europe/London"), id="weekend_prep")
    scheduler.add_job(sweep_tier3_weekly,  CronTrigger(day_of_week="sun", hour=2,  minute=0,  timezone="Europe/London"), id="tier3_weekly")
    log.info(f"Sweep schedule configured: {len(scheduler.get_jobs())} jobs")


@app.on_event("startup")
async def startup():
    state["status"] = "starting"
    ok = await mb_login()
    if not ok:
        log.warning("Could not auth with Market Brain API — sweeps will still run, CoS posts will fail until auth resolves")

    _add_sweeps()
    scheduler.start()
    state["status"] = "idle"

    # Run an immediate warm-up sweep on the most important Tier 1 assets
    log.info("Running startup warm-up sweep (Tier 1 fast)...")
    asyncio.create_task(sweep_manual(tickers=[a["ticker"] for a in TIER_1[:10]]))
    log.info(f"Sweep engine started. {len(TIER_1)} Tier-1, {len(TIER_2)} Tier-2 assets. API on :{SWEEP_PORT}")


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown(wait=False)
    log.info("Sweep engine stopped")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("sweep_engine:app", host="0.0.0.0", port=SWEEP_PORT, reload=False, log_level="info")
