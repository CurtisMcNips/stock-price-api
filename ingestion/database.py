"""
Market Brain — Ingestion Database Layer
────────────────────────────────────────
Drop-in replacement for the SQLite version.

All asset writes are POSTed to the main app via /api/ingest.
Run tracking, notifications, and deactivations are lightweight no-ops
(the main app owns the universe; the ingestion engine is stateless).

Environment variables:
  MB_API_URL     — https://web-production-db367d.up.railway.app
  INGEST_API_KEY — mb-ingest-secret
"""

import asyncio
import logging
import os
import time
import uuid
from typing import List, Dict, Optional

import httpx

log = logging.getLogger("mb-ingestion.database")

MB_API_URL     = os.environ.get("MB_API_URL", "").rstrip("/")
INGEST_API_KEY = os.environ.get("INGEST_API_KEY", "mb-ingest-secret")

INGEST_ENDPOINT = f"{MB_API_URL}/api/ingest"
BATCH_SIZE      = 50
REQUEST_TIMEOUT = 30
RETRY_ATTEMPTS  = 3
RETRY_DELAY     = 3.0

# In-memory state (per-run only — resets on restart)
_active_tickers: Dict[str, dict] = {}
_runs: List[dict] = []
_pending_notifications: List[dict] = []


# ══════════════════════════════════════════════════════════════
# NORMALISATION
# ══════════════════════════════════════════════════════════════
def _infer_asset_type(asset: dict) -> str:
    quote_type = (asset.get("quote_type") or "").upper()
    source     = (asset.get("source") or "").lower()
    ticker     = (asset.get("ticker") or "").upper()
    sector     = (asset.get("sector") or "").lower()
    if source == "coingecko" or quote_type == "CRYPTOCURRENCY" or sector == "crypto":
        return "crypto"
    if quote_type == "ETF":
        return "etf"
    if "=X" in ticker or quote_type == "CURRENCY":
        return "forex"
    if "=F" in ticker or quote_type == "FUTURE":
        return "commodity"
    return "equity"


def _normalise(asset: dict) -> dict:
    ticker = (asset.get("ticker") or "").upper().strip()
    return {
        "ticker":          ticker,
        "name":            asset.get("name") or ticker,
        "asset_type":      asset.get("asset_type") or _infer_asset_type(asset),
        "sector":          asset.get("sector"),
        "industry":        asset.get("industry"),
        "exchange":        asset.get("exchange"),
        "country":         asset.get("country", "US"),
        "currency":        asset.get("currency", "USD"),
        "market_cap":      asset.get("market_cap"),
        "cap_tier":        asset.get("cap_tier") or asset.get("market_cap_tier"),
        "volatility_tier": asset.get("volatility_tier"),
        "price_last":      asset.get("price") or asset.get("price_last"),
        "change_pct":      asset.get("change_pct"),
        "avg_volume_30d":  asset.get("avg_volume_30d"),
        "source":          asset.get("source", "unknown"),
        "tags":            asset.get("tags", []),
    }


# ══════════════════════════════════════════════════════════════
# HTTP POST TO MAIN APP
# ══════════════════════════════════════════════════════════════
async def _post_batch(client: httpx.AsyncClient, assets: List[dict]) -> bool:
    if not MB_API_URL:
        log.error("MB_API_URL not set — cannot push assets")
        return False

    payload = {"api_key": INGEST_API_KEY, "assets": assets}

    for attempt in range(RETRY_ATTEMPTS):
        try:
            r = await client.post(INGEST_ENDPOINT, json=payload, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                log.info(f"  ✓ batch OK — accepted={data.get('accepted','?')} rejected={data.get('rejected','?')}")
                return True
            if r.status_code == 401:
                log.error("  ✗ API key rejected — check INGEST_API_KEY on both Railway services")
                return False
            log.warning(f"  HTTP {r.status_code} attempt {attempt+1}: {r.text[:200]}")
        except httpx.TimeoutException:
            log.warning(f"  Timeout attempt {attempt+1}")
        except Exception as e:
            log.warning(f"  Error attempt {attempt+1}: {e}")

        if attempt < RETRY_ATTEMPTS - 1:
            await asyncio.sleep(RETRY_DELAY * (attempt + 1))

    return False


async def _push_assets_async(assets: List[dict]) -> Dict[str, int]:
    if not assets:
        return {"sent": 0, "batches_ok": 0, "batches_failed": 0}

    normalised = [_normalise(a) for a in assets if a.get("ticker")]
    seen = {}
    for a in normalised:
        seen[a["ticker"]] = a
    deduped = list(seen.values())

    for a in deduped:
        _active_tickers[a["ticker"]] = a

    log.info(f"Pushing {len(deduped)} assets to main app...")

    ok_count = fail_count = 0
    async with httpx.AsyncClient() as client:
        for i in range(0, len(deduped), BATCH_SIZE):
            batch = deduped[i : i + BATCH_SIZE]
            ok = await _post_batch(client, batch)
            if ok:
                ok_count += 1
            else:
                fail_count += 1
            if i + BATCH_SIZE < len(deduped):
                await asyncio.sleep(0.4)

    return {"sent": len(deduped), "batches_ok": ok_count, "batches_failed": fail_count}


# ══════════════════════════════════════════════════════════════
# DATABASE API — matches what ingestion_engine.py expects
# ══════════════════════════════════════════════════════════════

def init_db() -> None:
    """No-op — no local DB needed."""
    log.info("database.init_db: stateless mode (HTTP POST to main app)")


def upsert_asset(asset: dict, run_id: str = "", source: str = "") -> str:
    """Buffer one asset in memory. Flushed to main app at end of run."""
    ticker = (asset.get("ticker") or "").upper().strip()
    if not ticker:
        return "skipped"

    action = "updated" if ticker in _active_tickers else "added"
    _active_tickers[ticker] = {**asset, "ticker": ticker}

    _pending_notifications.append({
        "id":         str(uuid.uuid4()),
        "event_type": "asset_added" if action == "added" else "asset_updated",
        "ticker":     ticker,
        "payload":    _normalise(asset),
        "processed":  False,
    })

    return action


async def flush_pending() -> None:
    """Await this to push all buffered assets to the main app."""
    if not _active_tickers:
        return
    assets = list(_active_tickers.values())
    result = await _push_assets_async(assets)
    log.info(f"flush_pending: {result}")


def get_all_active_tickers() -> List[str]:
    return list(_active_tickers.keys())


def get_asset(ticker: str) -> Optional[dict]:
    return _active_tickers.get(ticker.upper())


def deactivate_asset(ticker: str, run_id: str = "", source: str = "", reason: str = "") -> None:
    ticker = ticker.upper()
    if ticker in _active_tickers:
        del _active_tickers[ticker]
        log.info(f"Deactivated {ticker}: {reason}")


# ── Run tracking ──────────────────────────────────────────────
def start_run(run_id: str, source: str) -> None:
    _runs.append({
        "run_id":      run_id,
        "source":      source,
        "started_at":  time.time(),
        "status":      "running",
        "added":       0,
        "updated":     0,
        "deactivated": 0,
    })


async def complete_run(run_id: str, stats: dict, status: str = "completed") -> None:
    for r in _runs:
        if r["run_id"] == run_id:
            r.update({
                "status":       status,
                "completed_at": time.time(),
                "added":        stats.get("added", 0),
                "updated":      stats.get("updated", 0),
                "deactivated":  stats.get("deactivated", 0),
            })
            break
    await flush_pending()


def get_recent_runs(limit: int = 5) -> List[dict]:
    return _runs[-limit:]


def get_universe_summary() -> dict:
    by_type = {}
    by_sector = {}
    for a in _active_tickers.values():
        t = a.get("asset_type", "unknown")
        s = a.get("sector", "Other") or "Other"
        by_type[t]   = by_type.get(t, 0) + 1
        by_sector[s] = by_sector.get(s, 0) + 1
    return {
        "total_active":   len(_active_tickers),
        "total_inactive": 0,
        "by_type":        by_type,
        "by_sector":      by_sector,
    }


# ── Notifications ─────────────────────────────────────────────
def get_pending_notifications(limit: int = 200) -> List[dict]:
    unprocessed = [n for n in _pending_notifications if not n["processed"]]
    return unprocessed[:limit]


def mark_notifications_processed(ids: List[str]) -> None:
    id_set = set(ids)
    for n in _pending_notifications:
        if n["id"] in id_set:
            n["processed"] = True
