"""
Market Brain — Ingestion Database Layer
────────────────────────────────────────
Previously wrote assets to SQLite.
Now POSTs directly to the main app via /api/ingest.

The main app stores assets in Redis, and /api/universe reads from there.

Environment variables expected:
  MB_API_URL     — e.g. https://web-production-db367d.up.railway.app
  INGEST_API_KEY — e.g. mb-ingest-secret
"""

import asyncio
import logging
import os
from typing import List, Dict

import httpx

log = logging.getLogger("mb-ingestion.database")

MB_API_URL     = os.environ.get("MB_API_URL", "").rstrip("/")
INGEST_API_KEY = os.environ.get("INGEST_API_KEY", "mb-ingest-secret")

INGEST_ENDPOINT = f"{MB_API_URL}/api/ingest"
BATCH_SIZE      = 50
REQUEST_TIMEOUT = 30
RETRY_ATTEMPTS  = 3
RETRY_DELAY     = 3.0


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
    if quote_type == "ETF" or source == "yahoo_etf":
        return "etf"
    if "=X" in ticker or quote_type == "CURRENCY":
        return "forex"
    if "=F" in ticker or quote_type == "FUTURE":
        return "commodity"
    return "equity"


def _normalise(asset: dict) -> dict:
    ticker = asset.get("ticker", "").upper().strip()
    return {
        "ticker":          ticker,
        "name":            asset.get("name") or ticker,
        "asset_type":      _infer_asset_type(asset),
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
# HTTP POST
# ══════════════════════════════════════════════════════════════
async def _post_batch(client: httpx.AsyncClient, assets: List[dict]) -> bool:
    if not MB_API_URL:
        log.error("MB_API_URL not set — cannot push assets to main app")
        return False

    payload = {"api_key": INGEST_API_KEY, "assets": assets}

    for attempt in range(RETRY_ATTEMPTS):
        try:
            r = await client.post(INGEST_ENDPOINT, json=payload, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                log.info(f"Ingest batch OK — accepted {data.get('accepted','?')}, "
                         f"rejected {data.get('rejected','?')}")
                return True
            if r.status_code == 401:
                log.error("Ingest API key rejected — check INGEST_API_KEY on both services")
                return False
            log.warning(f"Ingest POST HTTP {r.status_code} (attempt {attempt+1}): {r.text[:200]}")
        except httpx.TimeoutException:
            log.warning(f"Ingest POST timeout (attempt {attempt+1})")
        except Exception as e:
            log.warning(f"Ingest POST error (attempt {attempt+1}): {e}")

        if attempt < RETRY_ATTEMPTS - 1:
            await asyncio.sleep(RETRY_DELAY * (attempt + 1))

    return False


# ══════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════
async def push_assets(assets: List[dict]) -> Dict[str, int]:
    """Normalise and push assets to the main app via /api/ingest."""
    if not assets:
        return {"sent": 0, "batches_ok": 0, "batches_failed": 0}

    normalised = [_normalise(a) for a in assets if a.get("ticker")]
    seen = {}
    for a in normalised:
        seen[a["ticker"]] = a
    deduped = list(seen.values())

    log.info(f"Pushing {len(deduped)} assets to {INGEST_ENDPOINT}")

    batches_ok = batches_failed = 0

    async with httpx.AsyncClient() as client:
        for i in range(0, len(deduped), BATCH_SIZE):
            batch = deduped[i : i + BATCH_SIZE]
            ok = await _post_batch(client, batch)
            if ok:
                batches_ok += 1
            else:
                batches_failed += 1
            if i + BATCH_SIZE < len(deduped):
                await asyncio.sleep(0.5)

    return {"sent": len(deduped), "batches_ok": batches_ok, "batches_failed": batches_failed}


# ══════════════════════════════════════════════════════════════
# SYNC WRAPPERS (for callers that aren't async)
# ══════════════════════════════════════════════════════════════
def save_assets(assets: List[dict]) -> None:
    """Synchronous wrapper around push_assets."""
    result = asyncio.run(push_assets(assets))
    if result["batches_failed"]:
        log.warning(f"save_assets: {result['batches_failed']} batch(es) failed")
    else:
        log.info(f"save_assets: pushed {result['sent']} assets OK")


def save_asset(asset: dict) -> None:
    save_assets([asset])
