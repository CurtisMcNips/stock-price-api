"""
Market Brain — Market-Aware Research Scheduler
═══════════════════════════════════════════════════════════════════════

Philosophy
──────────
Every sweep has a single, specific reason to exist.
Every bot bundle is chosen for that moment only.
Nothing runs speculatively. Nothing runs redundantly.

When you open the app, it should feel like the system has been
watching all night and is already ahead of you.

─────────────────────────────────────────────────────────────────────
EXCHANGE HOURS IN UTC (winter — BST offset handled by Europe/London tz)
─────────────────────────────────────────────────────────────────────
Tokyo (TSE)           00:00 – 06:00 UTC
Hong Kong (HKEX)      01:30 – 08:00 UTC
Shanghai (SSE)        01:30 – 07:00 UTC
Frankfurt / Euronext  07:00 – 16:30 UTC
London (LSE)          08:00 – 16:30 UTC
NYSE / NASDAQ         14:30 – 21:00 UTC
  pre-market          09:00 – 14:30 UTC  (04:00–09:30 ET)
  post-market         21:00 – 01:00 UTC  (16:00–20:00 ET)
Crypto                24 / 7

KEY US DATA RELEASES (UTC)
  13:30  CPI · Core CPI · NFP · PPI · Retail Sales · Jobless Claims
  15:00  JOLTS · ISM · Factory Orders
  19:00  FOMC rate decision

KEY UK / EU DATA RELEASES (UTC)
  07:00  UK CPI · GDP · Employment · Retail Sales
  09:00  Eurozone PMI · CPI · GDP
  09:30  ECB decisions

─────────────────────────────────────────────────────────────────────
SWEEP SCHEDULE  (all times UK local — BST/GMT shifts automatically)
─────────────────────────────────────────────────────────────────────

  02:00  OVERNIGHT
         ├─ Why:    Asia mid-session running. US post-market winding down.
         │          Any after-hours earnings dropped. Crypto active.
         │          Overnight macro events (geopolitics, Asia data) just happened.
         ├─ Bots:   News · Earnings · Technicals   (FAST — no fund. quota)
         └─ Assets: Tier-1 US · ALL Crypto · Asian ADRs

  07:00  UK PRE-MARKET
         ├─ Why:    UK economic data drops at 07:00 GMT sharp.
         │          LSE opens at 08:00 — 60 minutes to digest.
         │          Overnight analyst upgrades/cuts published.
         │          Futures direction is readable.
         ├─ Bots:   Macro · News · Technicals   (fast; macro data is the signal)
         └─ Assets: All UK (.L) · EU equities · Commodities · Forex

  08:15  UK MARKET OPEN
         ├─ Why:    LSE has been open 15 minutes. Opening prices confirmed.
         │          Gap fills visible. Opening volume readable.
         │          Fundamentals from 07:00 sweep still fresh — don't re-run.
         ├─ Bots:   Technicals · News   (FAST ONLY — zero FMP calls)
         └─ Assets: Tier-1 + Tier-2 UK/EU

  11:30  UK MID-SESSION
         ├─ Why:    UK halfway through session.
         │          US pre-market has been open ~2.5 hrs — direction forming.
         │          Intraday reversals, news drops, cross-market signals.
         ├─ Bots:   News · Technicals · Macro
         └─ Assets: ALL Tier-1 (cross-asset signals matter here)

  12:00  US PRE-MARKET  ★ Most important sweep of the day
         ├─ Why:    NYSE opens in 2.5 hrs. Pre-market active since 09:00 ET.
         │          Overnight earnings out. Analyst upgrades published.
         │          Full intelligence reset — this is the daily baseline.
         ├─ Bots:   ALL bots · News + Earnings prioritised first
         └─ Assets: ALL Tier-1 US · Tier-2 US · ALL Crypto

  14:45  US MARKET OPEN
         ├─ Why:    NYSE/NASDAQ opened 15 minutes ago.
         │          Opening print confirmed. Volatility settling.
         │          Fundamentals from 12:00 sweep still fresh — don't re-run.
         ├─ Bots:   Technicals · News   (FAST ONLY — zero FMP calls)
         └─ Assets: Tier-1 US · Tier-1 Crypto

  16:45  UK MARKET CLOSE  ★ Definitive EU daily snapshot
         ├─ Why:    LSE closed 15 minutes ago. EU day is done.
         │          Closing prices locked. Post-close analyst notes publishing.
         │          Fundamentals are valid for end-of-day state.
         ├─ Bots:   ALL bots · Technicals first (capture closing print)
         └─ Assets: ALL Tier-1 + Tier-2 UK/EU

  17:00  US MID-SESSION
         ├─ Why:    NYSE 2.5 hrs in. London has closed.
         │          US midday momentum established.
         │          Sector rotation and volume trends readable.
         ├─ Bots:   Technicals · News · Analyst
         └─ Assets: Tier-1 US

  21:15  US MARKET CLOSE  ★ Definitive US daily snapshot
         ├─ Why:    NYSE/NASDAQ closed 15 minutes ago. Post-market just opened.
         │          End-of-day prices confirmed. After-close earnings starting.
         │          This is the record overnight and next-day pre-market reads from.
         ├─ Bots:   ALL bots · Technicals first
         └─ Assets: ALL Tier-1 + Tier-2  (skip UK/EU — already done at 16:45)

  23:00  POST-MARKET / EARNINGS
         ├─ Why:    Post-market 2 hours in. Most after-close earnings now out.
         │          Crypto preparing for Asia open.
         │          This is where the real surprises live.
         ├─ Bots:   Earnings · News · Technicals   (surgical — preserve quota)
         └─ Assets: Tier-1 US · ALL Crypto

  Sun 23:30  WEEKEND PREP  ★ Monday morning readiness
         ├─ Why:    Futures reopen Sunday 23:00 UTC.
         │          Weekend news absorbed. Geopolitical developments captured.
         │          Crypto weekend volatility summarised.
         │          Full reset so Monday 07:00 pre-market sweep builds on a clean base.
         ├─ Bots:   ALL bots
         └─ Assets: ALL Tier-1 + Tier-2

  Sun 02:00  TIER-3 DEEP SWEEP
         ├─ Why:    Quietest period of the week. No markets open anywhere.
         │          Illiquid and rarely-viewed assets deserve weekly fresh data.
         ├─ Bots:   ALL bots
         └─ Assets: Tier-3 only
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

log = logging.getLogger("mb.scheduler")

_scheduler   = None
_is_running  = False

# Scheduler timezone — APScheduler handles BST/GMT shift automatically
LONDON_TZ = "Europe/London"
GRACE_S   = 300   # 5-minute misfire grace window

# ─────────────────────────────────────────────────────────────
# BOT BUNDLES  — chosen for each market moment
# ─────────────────────────────────────────────────────────────

# Open / fast moments: only time-sensitive bots
# Rationale: at open, price + news IS the signal.
#            FMP/AV fundamentals haven't changed in 15 minutes.
#            Running them burns daily quota for zero new information.
BOTS_FAST = ["TechnicalLevelsBot", "NewsBot"]

# Pre-market: all bots, news/earnings first
# Rationale: this is the full daily intelligence reset.
#            News + Earnings are most likely stale (overnight gap).
#            Fundamentals may have updated (quarterly releases).
BOTS_PREMARKET = [
    "NewsBot", "EarningsBot", "AnalystBot",
    "FundamentalsBot", "MacroBot", "TechnicalLevelsBot",
]

# Close snapshot: technicals first (capture the close), then everything
# Rationale: closing price is the day's definitive data point.
#            Run technicals immediately, then let fundamentals follow.
BOTS_CLOSE = [
    "TechnicalLevelsBot", "NewsBot", "EarningsBot",
    "FundamentalsBot", "AnalystBot", "MacroBot",
]

# Overnight / post-market: News + Earnings + Technicals only
# Rationale: fundamentals don't change at 02:00 or 23:00.
#            Only news, earnings announcements, and price moves matter.
BOTS_OVERNIGHT = ["NewsBot", "EarningsBot", "TechnicalLevelsBot"]

# Intraday: lightweight signal check
BOTS_INTRADAY = ["TechnicalLevelsBot", "NewsBot", "MacroBot"]

# UK pre-market: macro data just dropped — Macro goes first
BOTS_UK_PREMARKET = ["MacroBot", "NewsBot", "TechnicalLevelsBot"]


# ─────────────────────────────────────────────────────────────
# ASSET FILTERS
# ─────────────────────────────────────────────────────────────

_NON_US_SUFFIXES = {".L", ".PA", ".DE", ".AS", ".MI", ".MC", ".TO", ".AX", "=X"}
_EU_SUFFIXES     = {".L", ".PA", ".DE", ".AS", ".MI", ".MC"}

_ASIAN_ADRS = {
    "BABA", "BIDU", "NIO", "JD", "PDD", "SE", "TSM", "TCEHY", "SONY",
    "HDB", "INFY", "WIT", "TTM", "RDY", "VALE", "PBR", "ITUB", "GRAB",
    "NVO", "ASML", "SAP", "DESP", "XPEV",
}

_COMMODITY_FOREX = {
    "GLD", "SLV", "USO", "DBC", "WEAT", "CORN", "PDBC",
}

def _is_us(t: str) -> bool:
    return not any(t.endswith(s) for s in _NON_US_SUFFIXES) and "-USD" not in t

def _is_uk_eu(t: str) -> bool:
    return any(t.endswith(s) for s in _EU_SUFFIXES)

def _is_crypto(t: str) -> bool:
    return "-USD" in t

def _is_commodity_forex(t: str) -> bool:
    return "=X" in t or t in _COMMODITY_FOREX

def _is_asian_adr(t: str) -> bool:
    return t in _ASIAN_ADRS

def _pick(symbols: List[str], *predicates) -> List[str]:
    """Return symbols matching ANY predicate, preserving order, deduped."""
    seen, out = set(), []
    for s in symbols:
        if s not in seen and any(p(s) for p in predicates):
            out.append(s)
            seen.add(s)
    return out

def _drop(symbols: List[str], *predicates) -> List[str]:
    """Return symbols matching NONE of the predicates."""
    return [s for s in symbols if not any(p(s) for p in predicates)]


# ─────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────

async def _load_universe() -> Dict[str, dict]:
    try:
        from app import rget
        import json
        raw = await rget("universe:assets")
        if raw:
            assets = json.loads(raw)
            return {a["ticker"]: a for a in assets if a.get("ticker")}
    except Exception as e:
        log.warning(f"Universe load failed: {e}")
    # Fallback to static Tier-1 list
    from research_engine.orchestrator.priority_tiers import TIER1_STATIC
    return {s: {"ticker": s, "sector": "Unknown", "quote_type": "EQUITY"}
            for s in TIER1_STATIC}


async def _sync_watchlist():
    """Pull current watchlist from Redis → promote items to Tier-1."""
    try:
        from app import rget
        import json
        raw = await rget("portfolio:watchlist")
        if raw:
            wl      = json.loads(raw)
            tickers = [w["ticker"] for w in wl if w.get("ticker")]
            from research_engine.orchestrator.priority_tiers import priority_manager
            priority_manager.set_watchlist(tickers)
    except Exception:
        pass


def _tier(n: int) -> List[str]:
    from research_engine.orchestrator.priority_tiers import priority_manager
    return (priority_manager.get_tier1() if n == 1 else
            priority_manager.get_tier2() if n == 2 else
            priority_manager.get_tier3())


def _is_weekday() -> bool:
    return datetime.now(timezone.utc).weekday() < 5  # Mon=0 … Fri=4


async def _run(
    cycle:          str,
    symbols:        List[str],
    universe:       Dict[str, dict],
    bots_override:  Optional[List[str]] = None,
    priority_bots:  Optional[List[str]] = None,
    weekday_only:   bool = False,
):
    """
    Run a sweep batch.

    bots_override  — run ONLY these bots, ignore TTL (e.g. open sweeps)
    priority_bots  — run these first, then remaining bots in TTL-normal order
    weekday_only   — silently skip on Sat/Sun
    """
    if weekday_only and not _is_weekday():
        log.info(f"[{cycle}] Weekend — skipped")
        return
    if not symbols:
        log.info(f"[{cycle}] No symbols — skipped")
        return

    from research_engine.orchestrator.sweeper import sweep_asset

    note = ""
    if bots_override:  note = f" | bots={bots_override}"
    elif priority_bots: note = f" | priority={priority_bots[:3]}…"
    log.info(f"[{cycle}] Starting — {len(symbols)} assets{note}")

    t0 = time.monotonic()
    ok = err = 0

    for sym in symbols:
        meta = universe.get(sym, {
            "ticker": sym, "sector": "Unknown", "quote_type": "EQUITY"
        })
        try:
            await sweep_asset(
                sym, meta,
                force         = bool(bots_override),
                cycle         = cycle,
                priority_bots = priority_bots,
                bots_override = bots_override,
            )
            ok += 1
        except Exception as e:
            log.error(f"[{cycle}] {sym}: {e}")
            err += 1
        await asyncio.sleep(0.3)   # gentle inter-asset pause

    elapsed = round(time.monotonic() - t0, 1)
    log.info(f"[{cycle}] Done — {ok} ok  {err} errors  {elapsed}s")


# ═════════════════════════════════════════════════════════════
# SWEEP JOBS — one function per market event
# ═════════════════════════════════════════════════════════════

async def job_overnight():
    """
    02:00 UK — Asia mid-session, US post-market winding down.

    Captures: overnight news, after-hours earnings, crypto moves,
              geopolitical events while western markets slept.
    Fast bots only — fundamentals are irrelevant at 02:00 and
    burning FMP quota overnight wastes the day's allowance.
    """
    await _sync_watchlist()
    u   = await _load_universe()
    t1  = _tier(1)
    targets = _pick(t1, _is_us, _is_crypto, _is_asian_adr)
    await _run("overnight", targets, u,
               bots_override=BOTS_OVERNIGHT)


async def job_uk_premarket():
    """
    07:00 UK — UK macro data drops at exactly 07:00 GMT.
    LSE opens at 08:00 — 60 minutes to absorb.

    Captures: UK CPI/GDP/employment data, overnight analyst notes,
              futures direction, Asian session close signals,
              EU pre-open sentiment.
    MacroBot runs first — macro data is the primary signal here.
    """
    await _sync_watchlist()
    u   = await _load_universe()
    t1  = _tier(1)
    t2  = _tier(2)
    targets = list(dict.fromkeys(
        _pick(t1, _is_uk_eu, _is_commodity_forex) +
        _pick(t2, _is_uk_eu)
    ))
    await _run("uk_premarket", targets, u,
               priority_bots=BOTS_UK_PREMARKET)


async def job_uk_open():
    """
    08:15 UK — LSE has been open 15 minutes. Opening prices confirmed.

    Captures: opening price action, gap confirmation, opening volume.
    FAST BOTS ONLY — the 07:00 sweep refreshed fundamentals.
    Running FMP again 75 minutes later wastes daily quota for nothing new.
    """
    await _sync_watchlist()
    u   = await _load_universe()
    t1  = _tier(1)
    t2  = _tier(2)
    targets = list(dict.fromkeys(
        _pick(t1, _is_uk_eu) + _pick(t2, _is_uk_eu)
    ))
    await _run("uk_open", targets, u,
               bots_override=BOTS_FAST,
               weekday_only=True)


async def job_uk_midsession():
    """
    11:30 UK — UK mid-session. US pre-market 2.5 hrs in, direction forming.

    Captures: intraday reversals, news drops, volume spikes,
              cross-market signals, US pre-market drift into UK session.
    All Tier-1 because sector rotation crosses asset types here.
    """
    await _sync_watchlist()
    u   = await _load_universe()
    t1  = _tier(1)
    await _run("uk_midsession", t1, u,
               priority_bots=BOTS_INTRADAY,
               weekday_only=True)


async def job_us_premarket():
    """
    12:00 UK — NYSE opens in 2.5 hours. Most important sweep of the day.

    Captures: overnight US earnings, analyst upgrades/cuts published
              pre-market, pre-market movers, all news since yesterday's close,
              any economic data released this morning.
    ALL bots run. News + Earnings prioritised because they are most
    likely to have changed since the previous day's close sweep.
    This is the daily baseline every subsequent read builds on.
    """
    await _sync_watchlist()
    u   = await _load_universe()
    t1  = _tier(1)
    t2  = _tier(2)
    targets = list(dict.fromkeys(
        _pick(t1, _is_us) + _pick(t2, _is_us) + _pick(t1, _is_crypto)
    ))
    await _run("us_premarket", targets, u,
               priority_bots=BOTS_PREMARKET)


async def job_us_open():
    """
    14:45 UK — NYSE/NASDAQ opened 15 minutes ago.

    Captures: opening price action, gap confirmation, institutional flow.
    FAST BOTS ONLY — the 12:00 sweep is only 2.75 hrs old.
    Fundamentals, analyst ratings, and macro haven't changed.
    Running FMP here burns quota for identical data.
    """
    await _sync_watchlist()
    u   = await _load_universe()
    t1  = _tier(1)
    targets = list(dict.fromkeys(
        _pick(t1, _is_us) + _pick(t1, _is_crypto)
    ))
    await _run("us_open", targets, u,
               bots_override=BOTS_FAST,
               weekday_only=True)


async def job_uk_close():
    """
    16:45 UK — LSE closed 15 minutes ago. EU day is complete.

    Captures: closing prices locked in, post-close analyst notes,
              end-of-day EU sentiment, closing fundamentals snapshot.
    ALL bots — Technicals first to lock in the closing print,
    then fundamentals/analyst sweep for the day's final state.
    This is the definitive UK/EU daily record.
    """
    await _sync_watchlist()
    u   = await _load_universe()
    t1  = _tier(1)
    t2  = _tier(2)
    targets = list(dict.fromkeys(
        _pick(t1, _is_uk_eu) + _pick(t2, _is_uk_eu)
    ))
    await _run("uk_close", targets, u,
               priority_bots=BOTS_CLOSE,
               weekday_only=True)


async def job_us_midsession():
    """
    17:00 UK — US 2.5 hours into session. London has just closed.

    Captures: US midday momentum, EU-to-US contagion or divergence,
              intraday sector rotation, volume trend confirmation.
    Lightweight — Technicals + News + Analyst (intraday upgrades happen).
    """
    await _sync_watchlist()
    u   = await _load_universe()
    t1  = _tier(1)
    us  = _pick(t1, _is_us)
    await _run("us_midsession", us, u,
               priority_bots=["TechnicalLevelsBot", "NewsBot", "AnalystBot"],
               weekday_only=True)


async def job_us_close():
    """
    21:15 UK — NYSE/NASDAQ closed 15 minutes ago. Post-market just opened.

    Captures: end-of-day prices confirmed, after-close earnings starting,
              closing analyst actions, final daily sentiment.
    ALL bots — Technicals first for the closing print.
    This is the record the overnight and next pre-market sweeps build from.
    UK/EU excluded — already fully swept at 16:45.
    """
    await _sync_watchlist()
    u   = await _load_universe()
    t1  = _tier(1)
    t2  = _tier(2)
    # Skip UK/EU — definitive snapshot already captured at 16:45.
    # Running FMP again 4.5 hours later duplicates calls for identical data.
    targets = list(dict.fromkeys(
        _drop(t1, _is_uk_eu) + _drop(t2, _is_uk_eu)
    ))
    await _run("us_close", targets, u,
               priority_bots=BOTS_CLOSE,
               weekday_only=True)


async def job_post_market():
    """
    23:00 UK — Post-market 2 hours in. Most after-close earnings now published.

    Captures: earnings beats/misses, after-hours price moves,
              crypto overnight positioning for Asia open.
    Surgical — Earnings + News + Technicals only.
    Fundamentals are the same as 21:15. No point re-running them.
    """
    await _sync_watchlist()
    u   = await _load_universe()
    t1  = _tier(1)
    targets = list(dict.fromkeys(
        _pick(t1, _is_us) + _pick(t1, _is_crypto)
    ))
    await _run("post_market", targets, u,
               bots_override=BOTS_OVERNIGHT,
               weekday_only=True)


async def job_weekend_prep():
    """
    Sunday 23:30 UK — Futures reopen at 23:00 UTC Sunday.

    Captures: weekend news, geopolitical developments,
              crypto weekend volatility, any Sunday analyst research notes.
    Full reset — this is what makes Monday morning feel alive.
    ALL bots, all Tier-1 + Tier-2.
    """
    await _sync_watchlist()
    u   = await _load_universe()
    t1  = _tier(1)
    t2  = _tier(2)
    targets = list(dict.fromkeys(t1 + t2))
    await _run("weekend_prep", targets, u,
               priority_bots=BOTS_PREMARKET)


async def job_tier3_weekly():
    """
    Sunday 02:00 UK — Quietest window of the week. No markets open anywhere.

    Sweeps illiquid / rarely-viewed assets that don't warrant daily attention
    but should still have fresh weekly data for when users do open them.
    Full bots, no rush.
    """
    await _sync_watchlist()
    u   = await _load_universe()
    t3  = _tier(3)
    if t3:
        await _run("tier3_weekly", t3, u)
    else:
        log.info("[tier3_weekly] No Tier-3 assets to sweep")


# ═════════════════════════════════════════════════════════════
# JOB REGISTRY
# (func, uk_hour, uk_minute, day_of_week, job_id, display_name)
# ═════════════════════════════════════════════════════════════

_JOBS = [
    # Runs every day (weekday + weekend)
    (job_overnight,      2,  0,  "mon-sun",  "overnight",       "02:00 UK  Overnight — Asia/Crypto/Post-market"),
    (job_uk_premarket,   7,  0,  "mon-sun",  "uk_premarket",    "07:00 UK  UK Pre-Market — macro data + EU prep"),
    (job_us_premarket,  12,  0,  "mon-sun",  "us_premarket",    "12:00 UK  US Pre-Market — full sweep ★"),

    # Weekdays only
    (job_uk_open,        8, 15,  "mon-fri",  "uk_open",         "08:15 UK  London Open — fast (Technicals+News)"),
    (job_uk_midsession, 11, 30,  "mon-fri",  "uk_midsession",   "11:30 UK  UK Mid-Session"),
    (job_us_open,       14, 45,  "mon-fri",  "us_open",         "14:45 UK  NYSE Open — fast (Technicals+News)"),
    (job_uk_close,      16, 45,  "mon-fri",  "uk_close",        "16:45 UK  London Close — full EU snapshot ★"),
    (job_us_midsession, 17,  0,  "mon-fri",  "us_midsession",   "17:00 UK  US Mid-Session"),
    (job_us_close,      21, 15,  "mon-fri",  "us_close",        "21:15 UK  US Close — full snapshot ★"),
    (job_post_market,   23,  0,  "mon-fri",  "post_market",     "23:00 UK  Post-Market — earnings + crypto"),

    # Weekend only
    (job_weekend_prep,  23, 30,  "sun",      "weekend_prep",    "Sun 23:30 UK  Weekend Prep — full reset ★"),
    (job_tier3_weekly,   2,  0,  "sun",      "tier3_weekly",    "Sun 02:00 UK  Tier-3 Weekly Deep Sweep"),
]


# ─────────────────────────────────────────────────────────────
# SCHEDULER CONTROL
# ─────────────────────────────────────────────────────────────

def start_scheduler():
    global _scheduler, _is_running
    if _is_running:
        log.warning("Scheduler already running — ignoring start call")
        return

    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        log.error("APScheduler missing — pip install 'apscheduler>=3.10'")
        return

    _scheduler = AsyncIOScheduler(timezone="UTC")

    log.info("Research scheduler — registering jobs:")
    for (func, hour, minute, dow, job_id, name) in _JOBS:
        _scheduler.add_job(
            func,
            CronTrigger(
                day_of_week = dow,
                hour        = hour,
                minute      = minute,
                timezone    = LONDON_TZ,   # BST/GMT handled automatically
            ),
            id                  = job_id,
            name                = name,
            max_instances       = 1,
            misfire_grace_time  = GRACE_S,
            replace_existing    = True,
        )
        log.info(f"  [{dow:>7}]  {name}")

    _scheduler.start()
    _is_running = True
    log.info(f"Scheduler live — {len(_JOBS)} jobs registered")


def stop_scheduler():
    global _scheduler, _is_running
    if _scheduler and _is_running:
        _scheduler.shutdown(wait=False)
        _is_running = False
        log.info("Scheduler stopped")


def get_scheduler_status() -> dict:
    if not _scheduler or not _is_running:
        return {"running": False, "jobs": []}

    jobs = []
    for job in _scheduler.get_jobs():
        nxt = job.next_run_time
        jobs.append({
            "id":       job.id,
            "name":     job.name,
            "next_run": nxt.isoformat() if nxt else None,
        })
    jobs.sort(key=lambda j: j["next_run"] or "9999")
    return {"running": True, "job_count": len(jobs), "jobs": jobs}


async def trigger_sweep_now(tier: int = 1, cycle: str = "manual") -> dict:
    """Manually trigger an out-of-schedule sweep (admin / testing)."""
    await _sync_watchlist()
    u = await _load_universe()
    from research_engine.orchestrator.priority_tiers import priority_manager
    if tier == 1:
        syms = priority_manager.get_tier1()
    elif tier == 2:
        syms = priority_manager.get_tier1() + priority_manager.get_tier2()
    else:
        syms = priority_manager.get_all_ordered()
    asyncio.create_task(_run(cycle, syms, u, priority_bots=BOTS_PREMARKET))
    return {"triggered": True, "assets": len(syms), "cycle": cycle}
