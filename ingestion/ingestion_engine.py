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

MIN_PRICE        = float(os.environ.get("MIN_PRICE",       "0.50"))
MIN_ADV_USD      = float(os.environ.get("MIN_ADV_USD",     "500000"))
SKIP_OTC         = os.environ.get("SKIP_OTC",    "true").lower() == "true"
SKIP_PENNY       = os.environ.get("SKIP_PENNY",  "true").lower() == "true"
CONCURRENCY      = int(os.environ.get("FETCH_CONCURRENCY", "5"))

ENGINE_API_URL   = os.environ.get("MB_API_URL", "http://localhost:8000")
ENGINE_API_TOKEN = os.environ.get("MB_BOT_TOKEN", "")


# ══════════════════════════════════════════════════════════════
# PIPELINE STAGES
# ══════════════════════════════════════════════════════════════

async def stage_fetch(mode: str) -> List[dict]:
    yahoo  = YahooFetcher()
    gecko  = CoinGeckoFetcher()
    static = StaticSeedFetcher()

    all_raw = []

    if mode in ("full", "update"):
        seeds = await static.fetch()
        all_raw.extend(seeds)
        log.info(f"Seeds: {len(seeds)} assets")

        crypto = await gecko.fetch_top_coins(limit=200)
        all_raw.extend(crypto)
        log.info(f"CoinGecko: {len(crypto)} crypto assets")

        forex = await yahoo.fetch_forex()
        all_raw.extend(forex)
        log.info(f"Forex: {len(forex)} pairs")

        commodities = await yahoo.fetch_commodities()
        all_raw.extend(commodities)
        log.info(f"Commodities: {len(commodities)} assets")

        etfs = await yahoo.fetch_etfs()
        all_raw.extend(etfs)
        log.info(f"ETFs: {len(etfs)} assets")

    if mode == "full":
        for tier in ["us_large_cap", "us_mid_cap", "us_small_cap"]:
            equities = await yahoo.fetch_equities_screener(tier)
            all_raw.extend(equities)
            log.info(f"Screener {tier}: {len(equities)} equities")
            await asyncio.sleep(1)

    if mode == "crypto":
        crypto = await gecko.fetch_top_coins(limit=200)
        all_raw.extend(crypto)

    if mode == "update":
        existing = get_all_active_tickers()
        log.info(f"Refreshing {len(existing)} existing tickers from Yahoo")
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
    seen: Dict[str, dict] = {}
    for asset in raw_assets:
        ticker = normalise_ticker(
            asset.get("ticker", ""),
            asset.get("exchange")
        ).upper().strip()
        if not ticker:
            continue
        if ticker in seen:
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
    classified = [classify_asset(a) for a in raw_assets]
    log.info(f"Classified: {len(classified)} assets")
    return classified


def stage_filter(assets: List[dict]) -> tuple[List[dict], List[dict]]:
    passing  = []
    rejected = []

    for asset in assets:
        asset_type = asset.get("asset_type", "equity")
        price      = asset.get("price_last")
        volume     = asset.get("avg_volume_30d")
        exchange   = asset.get("exchange")

        if SKIP_OTC:
            ok, reason = is_allowed_exchange(exchange)
            if not ok:
                rejected.append({**asset, "_reject_reason": reason})
                continue

        min_price = MIN_PRICE if SKIP_PENNY else 0.0
        ok, reason = is_liquid_enough(
            volume, price, asset_type,
            min_adv_usd=MIN_ADV_USD,
            min_price=min_price,
        )
        if not ok:
            rejected.append({**asset, "_reject_reason": reason})
            continue

        if not asset.get("name"):
            rejected.append({**asset, "_reject_reason": "missing name"})
            continue

        passing.append(asset)

    log.info(f"Filter: {len(passing)} pass, {len(rejected)} rejected")
    return passing, rejected


def stage_store(assets: List[dict], run_id: str, source: str) -> Dict[str, int]:
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


async def stage_detect_delistings(fetched_tickers: List[str], run_id: str, source: str) -> int:
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
    # Assets are pushed directly via /api/ingest in complete_run.
    # This stage is a no-op — /api/engine/notify does not exist.
    pass


# ══════════════════════════════════════════════════════════════
# FULL PIPELINE
# ══════════════════════════════════════════════════════════════

async def run_ingestion(mode: str = "update") -> Dict:
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
        raw = await stage_fetch(mode)
        stats["fetched"] = len(raw)

        deduped = stage_deduplicate(raw)
        classified = stage_classify(deduped)

        passing, rejected = stage_filter(classified)
        stats["skipped"] = len(rejected)

        store_stats = stage_store(passing, run_id, source)
        stats["added"]   = store_stats.get("added", 0)
        stats["updated"] = store_stats.get("updated", 0)
        stats["errors"]  = store_stats.get("errors", 0)

        if mode == "full":
            fetched_tickers = [a["ticker"] for a in passing]
            stats["deactivated"] = await stage_detect_delistings(
                fetched_tickers, run_id, source
            )

        await stage_notify_engine(run_id)

        elapsed = round(time.time() - start, 1)
        stats["elapsed_seconds"] = elapsed
        stats["status"] = "completed"

        await complete_run(run_id, stats, "completed")  # ← await (triggers HTTP flush)

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
        await complete_run(run_id, stats, "failed")  # ← await (triggers HTTP flush)

    return stats


# ══════════════════════════════════════════════════════════════
# CLI + SCHEDULER
# ══════════════════════════════════════════════════════════════

async def run_scheduled():
    log.info("Ingestion bot started in scheduled mode")

    await run_ingestion(mode="update")

    while True:
        now     = datetime.now(timezone.utc)
        weekday = now.weekday()
        hour    = now.hour

        if weekday == 6 and hour == 2:
            await run_ingestion(mode="full")
            await asyncio.sleep(3600)
        elif hour == 6 and now.minute < 5:
            await run_ingestion(mode="update")
            await asyncio.sleep(300)
        else:
            await asyncio.sleep(60)


def print_status():
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
