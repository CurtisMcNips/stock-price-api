import sys, os
sys.path.append(os.path.dirname(__file__))
sys.path.append(os.path.join(os.path.dirname(__file__), "ingestion"))
"""
Market Brain — Ingestion Engine
────────────────────────────────
Orchestrates the full ingestion pipeline:
  1. Fetch raw asset data from all sources
  2. Classify and tag each asset
  3. Filter out illiquid / OTC / invalid assets
  4. Upsert into database
  5. Deactivate delisted assets
  6. Notify the analytical engine of changes
  7. Log everything

This file is the only one that should be run directly.
  python ingestion_engine.py --mode full
  python ingestion_engine.py --mode update
  python ingestion_engine.py --mode validate
"""

import asyncio
import argparse
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Optional

from database import (
    init_db, upsert_asset, deactivate_asset, get_all_active_tickers,
    get_asset, start_run, complete_run, get_recent_runs, get_universe_summary,
    get_pending_notifications, mark_notifications_processed,
)
from classifiers import (
    classify_asset, is_liquid_enough, is_allowed_exchange, normalise_ticker
)
from fetchers import YahooFetcher, CoinGeckoFetcher, StaticSeedFetcher
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("mb-ingestion.engine")

# ── Config (override via environment variables) ────────────────
MIN_PRICE        = float(os.environ.get("MIN_PRICE",       "0.50"))
MIN_ADV_USD      = float(os.environ.get("MIN_ADV_USD",     "500000"))   # $500k daily value
SKIP_OTC         = os.environ.get("SKIP_OTC",    "true").lower() == "true"
SKIP_PENNY       = os.environ.get("SKIP_PENNY",  "true").lower() == "true"
CONCURRENCY      = int(os.environ.get("FETCH_CONCURRENCY", "5"))

# Engine API base URL — for posting notifications
ENGINE_API_URL   = os.environ.get("MB_API_URL", "http://localhost:8000")
ENGINE_API_TOKEN = os.environ.get("MB_BOT_TOKEN", "")


# ══════════════════════════════════════════════════════════════
# PIPELINE STAGES
# ══════════════════════════════════════════════════════════════

async def stage_fetch(mode: str) -> List[dict]:
    """
    Stage 1 — Fetch raw asset data from all sources.
    mode='full'   → all sources, all screener tiers
    mode='update' → skip screener, only refresh known tickers + static seeds
    mode='crypto' → crypto only
    mode='validate' → just validate existing DB tickers
    """
    yahoo  = YahooFetcher()
    gecko  = CoinGeckoFetcher()
    static = StaticSeedFetcher()

    all_raw = []

    if mode in ("full", "update"):
        # Static seeds always run (fast, no rate limits)
        seeds = await static.fetch()
        all_raw.extend(seeds)
        log.info(f"Seeds: {len(seeds)} assets")

        # Crypto from CoinGecko (more reliable than Yahoo for crypto)
        crypto = await gecko.fetch_top_coins(limit=100)
        all_raw.extend(crypto)
        log.info(f"CoinGecko: {len(crypto)} crypto assets")

        # Forex pairs
        forex = await yahoo.fetch_forex()
        all_raw.extend(forex)
        log.info(f"Forex: {len(forex)} pairs")

        # Commodities
        commodities = await yahoo.fetch_commodities()
        all_raw.extend(commodities)
        log.info(f"Commodities: {len(commodities)} assets")

        # ETFs
        etfs = await yahoo.fetch_etfs()
        all_raw.extend(etfs)
        log.info(f"ETFs: {len(etfs)} assets")

    if mode == "full":
        # Full screener runs — large, mid, small cap equities
        for tier in ["us_large_cap", "us_mid_cap", "us_small_cap"]:
            equities = await yahoo.fetch_equities_screener(tier)
            all_raw.extend(equities)
            log.info(f"Screener {tier}: {len(equities)} equities")
            await asyncio.sleep(1)

    if mode == "crypto":
        crypto = await gecko.fetch_top_coins(limit=200)
        all_raw.extend(crypto)

    if mode == "update":
        # Refresh existing DB tickers
        existing = get_all_active_tickers()
        log.info(f"Refreshing {len(existing)} existing tickers from Yahoo")
        # Batch in groups of 50 to avoid hammering Yahoo
        batches = [existing[i:i+50] for i in range(0, len(existing), 50)]
        for batch in batches:
            refreshed = await yahoo.fetch_tickers_batch(
                batch, include_summary=False, concurrency=CONCURRENCY
            )
            all_raw.extend(refreshed)
            await asyncio.sleep(0.5)

    log.info(f"Total raw assets fetched: {len(all_raw)}")
    return all_raw


def stage_deduplicate(raw_assets: List[dict]) -> List[dict]:
    """
    Stage 2 — Deduplicate by ticker.
    Later sources override earlier ones for the same ticker
    (CoinGecko data is richer for crypto, Yahoo richer for equities).
    Merge rather than replace where possible.
    """
    seen: Dict[str, dict] = {}
    for asset in raw_assets:
        ticker = normalise_ticker(
            asset.get("ticker", ""),
            asset.get("exchange")
        ).upper().strip()
        if not ticker:
            continue
        if ticker in seen:
            # Merge: new values override only if non-null
            existing = seen[ticker]
            for k, v in asset.items():
                if v is not None and k != "source":
                    existing[k] = v
        else:
            seen[ticker] = {**asset, "ticker": ticker}

    result = list(seen.values())
    log.info(f"After dedup: {len(result)} unique assets")
    return result


def stage_classify(raw_assets: List[dict]) -> List[dict]:
    """Stage 3 — Classify each asset (type, cap tier, volatility tier, sector)."""
    classified = [classify_asset(a) for a in raw_assets]
    log.info(f"Classified: {len(classified)} assets")
    return classified


def stage_filter(assets: List[dict]) -> tuple[List[dict], List[dict]]:
    """
    Stage 4 — Filter out assets that don't meet quality criteria.
    Returns (passing, rejected) for audit logging.
    """
    passing  = []
    rejected = []

    for asset in assets:
        ticker     = asset.get("ticker", "")
        asset_type = asset.get("asset_type", "equity")
        price      = asset.get("price_last")
        volume     = asset.get("avg_volume_30d")
        exchange   = asset.get("exchange")

        # Exchange filter
        if SKIP_OTC:
            ok, reason = is_allowed_exchange(exchange)
            if not ok:
                rejected.append({**asset, "_reject_reason": reason})
                continue

        # Liquidity / penny filter
        min_price = MIN_PRICE if SKIP_PENNY else 0.0
        ok, reason = is_liquid_enough(
            volume, price, asset_type,
            min_adv_usd=MIN_ADV_USD,
            min_price=min_price,
        )
        if not ok:
            rejected.append({**asset, "_reject_reason": reason})
            continue

        # Must have a name
        if not asset.get("name"):
            rejected.append({**asset, "_reject_reason": "missing name"})
            continue

        passing.append(asset)

    log.info(f"Filter: {len(passing)} pass, {len(rejected)} rejected")
    return passing, rejected


def stage_store(assets: List[dict], run_id: str, source: str) -> Dict[str, int]:
    """
    Stage 5 — Upsert all passing assets into the database.
    Returns counts of actions taken.
    """
    stats = {"added": 0, "updated": 0, "errors": 0}
    for asset in assets:
        try:
            action = upsert_asset(asset, run_id, source)
            stats[action] = stats.get(action, 0) + 1
        except Exception as e:
            log.error(f"Store error for {asset.get('ticker')}: {e}")
            stats["errors"] += 1
    log.info(f"Stored: {stats}")
    return stats


async def stage_detect_delistings(
    fetched_tickers: List[str],
    run_id: str,
    source: str,
) -> int:
    """
    Stage 6 — Detect delistings.
    Any ticker that was active in DB but not seen in this run gets validated.
    If Yahoo confirms it's gone, deactivate it.
    """
    active_tickers = set(get_all_active_tickers())
    fetched_set    = set(fetched_tickers)
    missing        = active_tickers - fetched_set

    if not missing:
        log.info("No delistings detected")
        return 0

    log.info(f"Validating {len(missing)} potentially delisted tickers")
    yahoo = YahooFetcher()
    deactivated = 0

    for ticker in missing:
        is_valid = await yahoo.validate_ticker(ticker)
        if not is_valid:
            deactivate_asset(ticker, run_id, source, reason="not found in data source")
            deactivated += 1
        await asyncio.sleep(0.3)

    log.info(f"Deactivated {deactivated} delisted assets")
    return deactivated


async def stage_notify_engine(run_id: str):
    """
    Stage 7 — Push pending notifications to the analytical engine.
    Engine reads these to update its asset universe.
    """
    try:
        import httpx
        notifications = get_pending_notifications(limit=200)
        if not notifications:
            return

        headers = {}
        if ENGINE_API_TOKEN:
            headers["Authorization"] = f"Bearer {ENGINE_API_TOKEN}"

        async with httpx.AsyncClient(timeout=10) as client:
            for notif in notifications:
                try:
                    await client.post(
                        f"{ENGINE_API_URL}/api/engine/notify",
                        json={
                            "event_type": notif["event_type"],
                            "ticker":     notif["ticker"],
                            "payload":    notif["payload"],
                            "run_id":     run_id,
                        },
                        headers=headers,
                    )
                except Exception:
                    pass  # Engine notification is best-effort

        ids = [n["id"] for n in notifications]
        mark_notifications_processed(ids)
        log.info(f"Notified engine of {len(notifications)} events")

    except Exception as e:
        log.warning(f"Engine notification failed: {e} — will retry next run")


# ══════════════════════════════════════════════════════════════
# FULL PIPELINE
# ══════════════════════════════════════════════════════════════

async def run_ingestion(mode: str = "update") -> Dict:
    """
    Run the full ingestion pipeline.
    Returns a summary dict with counts and status.
    """
    run_id = f"run-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    source = f"ingestion_{mode}"
    start  = time.time()

    log.info(f"═══ Starting ingestion run {run_id} mode={mode} ═══")
    start_run(run_id, source)

    stats = {
        "run_id":      run_id,
        "mode":        mode,
        "fetched":     0,
        "added":       0,
        "updated":     0,
        "deactivated": 0,
        "skipped":     0,
        "errors":      0,
    }

    try:
        # 1. Fetch
        raw = await stage_fetch(mode)
        stats["fetched"] = len(raw)

        # 2. Deduplicate
        deduped = stage_deduplicate(raw)

        # 3. Classify
        classified = stage_classify(deduped)

        # 4. Filter
        passing, rejected = stage_filter(classified)
        stats["skipped"] = len(rejected)

        # 5. Store
        store_stats = stage_store(passing, run_id, source)
        stats["added"]   = store_stats.get("added", 0)
        stats["updated"] = store_stats.get("updated", 0)
        stats["errors"]  = store_stats.get("errors", 0)

        # 6. Detect delistings (only on full runs — expensive)
        if mode == "full":
            fetched_tickers = [a["ticker"] for a in passing]
            stats["deactivated"] = await stage_detect_delistings(
                fetched_tickers, run_id, source
            )

        # 7. Notify engine
        await stage_notify_engine(run_id)

        elapsed = round(time.time() - start, 1)
        stats["elapsed_seconds"] = elapsed
        stats["status"] = "completed"

        complete_run(run_id, stats, "completed")

        summary = get_universe_summary()
        log.info(
            f"═══ Run complete in {elapsed}s — "
            f"added={stats['added']} updated={stats['updated']} "
            f"deactivated={stats['deactivated']} "
            f"universe={summary['total_active']} active assets ═══"
        )

    except Exception as e:
        log.error(f"Ingestion run failed: {e}", exc_info=True)
        stats["status"] = "failed"
        stats["notes"]  = str(e)
        complete_run(run_id, stats, "failed")

    return stats


# ══════════════════════════════════════════════════════════════
# CLI + SCHEDULER
# ══════════════════════════════════════════════════════════════

async def run_scheduled():
    """
    Continuous scheduler.
    Runs a 'full' ingestion weekly, 'update' daily.
    Used when deployed as a long-running Railway service.
    """
    import calendar

    log.info("Ingestion bot started in scheduled mode")

    # Run an update immediately on startup
    await run_ingestion(mode="update")

    while True:
        now     = datetime.now(timezone.utc)
        weekday = now.weekday()   # 0=Monday
        hour    = now.hour

        # Full run: every Sunday at 02:00 UTC
        if weekday == 6 and hour == 2:
            await run_ingestion(mode="full")
            await asyncio.sleep(3600)  # sleep 1h to avoid double-run

        # Daily update: every day at 06:00 UTC (market prep)
        elif hour == 6 and now.minute < 5:
            await run_ingestion(mode="update")
            await asyncio.sleep(300)   # sleep 5min to avoid double-run

        else:
            await asyncio.sleep(60)    # check every minute


def print_status():
    """Print current universe status to stdout."""
    init_db()
    summary = get_universe_summary()
    runs    = get_recent_runs(5)
    print("\n══════════════════════════════════════════")
    print("  Market Brain — Asset Universe Status")
    print("══════════════════════════════════════════")
    print(f"  Total active:   {summary['total_active']}")
    print(f"  Total inactive: {summary['total_inactive']}")
    print("\n  By type:")
    for t, n in summary["by_type"].items():
        print(f"    {t:<12} {n}")
    print("\n  By sector:")
    for s, n in sorted(summary["by_sector"].items(), key=lambda x: -x[1]):
        print(f"    {s:<20} {n}")
    print("\n  Recent runs:")
    for r in runs:
        print(f"    {r['run_id']}  {r['status']}  +{r['added']} ={r['updated']} -{r['deactivated']}")
    print("══════════════════════════════════════════\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Market Brain Asset Ingestion Bot")
    parser.add_argument(
        "--mode",
        choices=["full", "update", "crypto", "validate", "schedule", "status"],
        default="update",
        help=(
            "full=all sources + delistings  "
            "update=refresh existing+seeds  "
            "crypto=crypto only  "
            "validate=check tickers  "
            "schedule=run forever  "
            "status=print DB status"
        )
    )
    args = parser.parse_args()

    init_db()

    if args.mode == "status":
        print_status()
    elif args.mode == "schedule":
        asyncio.run(run_scheduled())
    else:
        result = asyncio.run(run_ingestion(mode=args.mode))
        print(f"\nResult: {result}")
