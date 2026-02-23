"""
Market Brain — Asset Database
─────────────────────────────
SQLite-backed asset universe store.
SQLite chosen for Railway compatibility with zero config.
Swap DATABASE_URL to PostgreSQL for production scale.

Tables:
  assets           — canonical asset universe
  asset_metadata   — flexible key/value extension
  ingestion_logs   — immutable audit trail
  sectors          — sector reference data
  ingestion_runs   — per-run summary records
"""

import sqlite3
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

log = logging.getLogger("mb-ingestion.db")

DATABASE_PATH = os.environ.get("ASSET_DB_PATH", "market_brain_assets.db")


# ══════════════════════════════════════════════════════════════
# CONNECTION
# ══════════════════════════════════════════════════════════════
def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════
# SCHEMA
# ══════════════════════════════════════════════════════════════
SCHEMA = """
-- ── Sector reference ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sectors (
    sector          TEXT PRIMARY KEY,
    description     TEXT,
    etf_proxy       TEXT,        -- e.g. XLK for Technology
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Master asset universe ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS assets (
    ticker          TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    asset_type      TEXT NOT NULL,   -- equity | etf | crypto | forex | index | commodity
    sector          TEXT,
    industry        TEXT,
    exchange        TEXT,
    country         TEXT DEFAULT 'US',
    currency        TEXT DEFAULT 'USD',

    -- Size / classification
    market_cap      REAL,            -- in USD
    market_cap_tier TEXT,            -- Nano | Micro | Small | Mid | Large | Mega
    volatility_tier TEXT,            -- Low | Med | High | VHigh | Extreme

    -- State
    active          INTEGER NOT NULL DEFAULT 1,   -- 1=active, 0=delisted
    tradeable       INTEGER NOT NULL DEFAULT 1,   -- 1=tradeable, 0=suspended

    -- Price range context (updated on each ingestion)
    price_last      REAL,
    price_52w_high  REAL,
    price_52w_low   REAL,
    avg_volume_30d  REAL,

    -- Timestamps
    first_seen      TEXT NOT NULL DEFAULT (datetime('now')),
    last_updated    TEXT NOT NULL DEFAULT (datetime('now')),
    last_active     TEXT,

    -- Source tracking
    source          TEXT,            -- yahoo | coingecko | manual | etc.
    source_id       TEXT,            -- external ID if applicable

    FOREIGN KEY (sector) REFERENCES sectors(sector)
);

CREATE INDEX IF NOT EXISTS idx_assets_sector    ON assets(sector);
CREATE INDEX IF NOT EXISTS idx_assets_type      ON assets(asset_type);
CREATE INDEX IF NOT EXISTS idx_assets_active    ON assets(active);
CREATE INDEX IF NOT EXISTS idx_assets_cap_tier  ON assets(market_cap_tier);

-- ── Flexible metadata store ───────────────────────────────────
CREATE TABLE IF NOT EXISTS asset_metadata (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    key             TEXT NOT NULL,
    value           TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (ticker) REFERENCES assets(ticker),
    UNIQUE(ticker, key)
);

CREATE INDEX IF NOT EXISTS idx_meta_ticker ON asset_metadata(ticker);
CREATE INDEX IF NOT EXISTS idx_meta_key    ON asset_metadata(key);

-- ── Immutable audit log ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS ingestion_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL DEFAULT (datetime('now')),
    run_id          TEXT,
    action          TEXT NOT NULL,  -- added | updated | deactivated | reactivated | skipped | error
    ticker          TEXT,
    asset_type      TEXT,
    details         TEXT,           -- JSON blob
    source          TEXT
);

CREATE INDEX IF NOT EXISTS idx_logs_ticker    ON ingestion_logs(ticker);
CREATE INDEX IF NOT EXISTS idx_logs_action    ON ingestion_logs(action);
CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON ingestion_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_logs_run_id    ON ingestion_logs(run_id);

-- ── Per-run summary ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    status          TEXT NOT NULL DEFAULT 'running',  -- running | completed | failed
    source          TEXT,
    total_fetched   INTEGER DEFAULT 0,
    added           INTEGER DEFAULT 0,
    updated         INTEGER DEFAULT 0,
    deactivated     INTEGER DEFAULT 0,
    skipped         INTEGER DEFAULT 0,
    errors          INTEGER DEFAULT 0,
    notes           TEXT
);

-- ── Engine notification queue ─────────────────────────────────
-- Engine polls this table to learn about universe changes
CREATE TABLE IF NOT EXISTS engine_notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    event_type      TEXT NOT NULL,  -- asset_added | asset_removed | metadata_changed | sector_changed
    ticker          TEXT NOT NULL,
    payload         TEXT,           -- JSON
    processed       INTEGER DEFAULT 0,
    processed_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_notif_processed ON engine_notifications(processed);
"""

SEED_SECTORS = [
    ("Technology",   "Software, semiconductors, hardware, cloud, AI",      "XLK"),
    ("Healthcare",   "Pharma, biotech, medtech, health services",           "XLV"),
    ("Finance",      "Banking, insurance, fintech, asset management",       "XLF"),
    ("Energy",       "Oil & gas, renewables, utilities",                    "XLE"),
    ("Consumer",     "Retail, e-commerce, media, autos, apparel",          "XLY"),
    ("Industrials",  "Aerospace, defence, manufacturing, logistics",        "XLI"),
    ("Materials",    "Metals, mining, chemicals, forestry",                 "XLB"),
    ("Crypto",       "Digital assets, blockchain, DeFi",                    None),
    ("Forex",        "Currency pairs",                                       None),
    ("Commodities",  "Oil, gas, gold, silver, agricultural",                "DJP"),
    ("Space",        "Satellite, launch, defence tech",                     None),
    ("Real Estate",  "REITs, property development",                         "XLRE"),
    ("Utilities",    "Electric, water, gas utilities",                      "XLU"),
    ("Agriculture",  "Farming, food production, fertilisers",               "MOO"),
]


def init_db():
    """Initialise database — safe to call on every startup."""
    with db() as conn:
        conn.executescript(SCHEMA)
        for sector, desc, etf in SEED_SECTORS:
            conn.execute(
                "INSERT OR IGNORE INTO sectors(sector, description, etf_proxy) VALUES (?,?,?)",
                (sector, desc, etf)
            )
    log.info(f"Database initialised: {DATABASE_PATH}")


# ══════════════════════════════════════════════════════════════
# ASSET CRUD
# ══════════════════════════════════════════════════════════════
def upsert_asset(asset: dict, run_id: str, source: str) -> str:
    """
    Insert or update an asset. Returns action taken.
    Never deletes — only deactivates.
    """
    ticker = asset["ticker"].upper().strip()
    now = datetime.now(timezone.utc).isoformat()

    with db() as conn:
        existing = conn.execute(
            "SELECT * FROM assets WHERE ticker = ?", (ticker,)
        ).fetchone()

        if existing is None:
            # New asset
            conn.execute("""
                INSERT INTO assets (
                    ticker, name, asset_type, sector, industry, exchange,
                    country, currency, market_cap, market_cap_tier,
                    volatility_tier, active, tradeable, price_last,
                    price_52w_high, price_52w_low, avg_volume_30d,
                    first_seen, last_updated, last_active, source, source_id
                ) VALUES (
                    :ticker,:name,:asset_type,:sector,:industry,:exchange,
                    :country,:currency,:market_cap,:market_cap_tier,
                    :volatility_tier,:active,:tradeable,:price_last,
                    :price_52w_high,:price_52w_low,:avg_volume_30d,
                    :now,:now,:now,:source,:source_id
                )
            """, {**asset, "ticker": ticker, "now": now,
                  "active": 1, "tradeable": 1,
                  "source": source, "source_id": asset.get("source_id")})

            _log(conn, run_id, "added", ticker, asset.get("asset_type"), source,
                 {"name": asset.get("name"), "sector": asset.get("sector")})
            _notify(conn, "asset_added", ticker, {"name": asset.get("name"), "sector": asset.get("sector")})
            return "added"

        else:
            # Update existing
            changed_fields = {}
            for field in ["name", "sector", "industry", "market_cap", "market_cap_tier",
                          "volatility_tier", "price_last", "price_52w_high", "price_52w_low",
                          "avg_volume_30d", "exchange", "currency"]:
                new_val = asset.get(field)
                old_val = existing[field] if field in existing.keys() else None
                if new_val is not None and new_val != old_val:
                    changed_fields[field] = {"old": old_val, "new": new_val}

            conn.execute("""
                UPDATE assets SET
                    name=COALESCE(:name, name),
                    sector=COALESCE(:sector, sector),
                    industry=COALESCE(:industry, industry),
                    market_cap=COALESCE(:market_cap, market_cap),
                    market_cap_tier=COALESCE(:market_cap_tier, market_cap_tier),
                    volatility_tier=COALESCE(:volatility_tier, volatility_tier),
                    price_last=COALESCE(:price_last, price_last),
                    price_52w_high=COALESCE(:price_52w_high, price_52w_high),
                    price_52w_low=COALESCE(:price_52w_low, price_52w_low),
                    avg_volume_30d=COALESCE(:avg_volume_30d, avg_volume_30d),
                    active=1, last_updated=:now, last_active=:now
                WHERE ticker=:ticker
            """, {**asset, "ticker": ticker, "now": now})

            if changed_fields:
                _log(conn, run_id, "updated", ticker, asset.get("asset_type"), source,
                     {"changes": changed_fields})
                # Notify engine of sector/volatility changes specifically
                if "sector" in changed_fields:
                    _notify(conn, "sector_changed", ticker, changed_fields["sector"])
                if "volatility_tier" in changed_fields:
                    _notify(conn, "metadata_changed", ticker, {"volatility_tier": changed_fields["volatility_tier"]})

            return "updated"


def deactivate_asset(ticker: str, run_id: str, source: str, reason: str = "delisted"):
    """Mark asset as inactive. Never deletes from DB."""
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            "UPDATE assets SET active=0, last_updated=? WHERE ticker=?",
            (now, ticker)
        )
        _log(conn, run_id, "deactivated", ticker, None, source, {"reason": reason})
        _notify(conn, "asset_removed", ticker, {"reason": reason})
    log.info(f"Deactivated: {ticker} ({reason})")


def reactivate_asset(ticker: str, run_id: str, source: str):
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            "UPDATE assets SET active=1, last_updated=?, last_active=? WHERE ticker=?",
            (now, now, ticker)
        )
        _log(conn, run_id, "reactivated", ticker, None, source, {})
        _notify(conn, "asset_added", ticker, {"reactivated": True})


def set_metadata(ticker: str, key: str, value: Any):
    now = datetime.now(timezone.utc).isoformat()
    val_str = json.dumps(value) if not isinstance(value, str) else value
    with db() as conn:
        conn.execute("""
            INSERT INTO asset_metadata(ticker, key, value, updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(ticker,key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """, (ticker, key, val_str, now))


def get_all_active_tickers() -> List[str]:
    with db() as conn:
        rows = conn.execute("SELECT ticker FROM assets WHERE active=1").fetchall()
    return [r["ticker"] for r in rows]


def get_asset(ticker: str) -> Optional[Dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM assets WHERE ticker=?", (ticker.upper(),)).fetchone()
    return dict(row) if row else None


def get_assets(
    asset_type: Optional[str] = None,
    sector: Optional[str] = None,
    active_only: bool = True,
    cap_tier: Optional[str] = None,
) -> List[Dict]:
    query = "SELECT * FROM assets WHERE 1=1"
    params = []
    if active_only:     query += " AND active=1"
    if asset_type:      query += " AND asset_type=?";    params.append(asset_type)
    if sector:          query += " AND sector=?";        params.append(sector)
    if cap_tier:        query += " AND market_cap_tier=?"; params.append(cap_tier)
    with db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_universe_summary() -> Dict:
    with db() as conn:
        total   = conn.execute("SELECT COUNT(*) FROM assets WHERE active=1").fetchone()[0]
        by_type = conn.execute("SELECT asset_type, COUNT(*) as n FROM assets WHERE active=1 GROUP BY asset_type").fetchall()
        by_sec  = conn.execute("SELECT sector, COUNT(*) as n FROM assets WHERE active=1 GROUP BY sector").fetchall()
        inactive = conn.execute("SELECT COUNT(*) FROM assets WHERE active=0").fetchone()[0]
    return {
        "total_active": total,
        "total_inactive": inactive,
        "by_type":   {r["asset_type"]: r["n"] for r in by_type},
        "by_sector": {r["sector"]: r["n"] for r in by_sec if r["sector"]},
    }


# ══════════════════════════════════════════════════════════════
# RUN MANAGEMENT
# ══════════════════════════════════════════════════════════════
def start_run(run_id: str, source: str) -> str:
    with db() as conn:
        conn.execute("""
            INSERT INTO ingestion_runs(run_id, started_at, status, source)
            VALUES(?,?,?,?)
        """, (run_id, datetime.now(timezone.utc).isoformat(), "running", source))
    return run_id


def complete_run(run_id: str, stats: dict, status: str = "completed"):
    with db() as conn:
        conn.execute("""
            UPDATE ingestion_runs SET
                completed_at=?, status=?, total_fetched=?, added=?,
                updated=?, deactivated=?, skipped=?, errors=?, notes=?
            WHERE run_id=?
        """, (
            datetime.now(timezone.utc).isoformat(), status,
            stats.get("fetched", 0), stats.get("added", 0),
            stats.get("updated", 0), stats.get("deactivated", 0),
            stats.get("skipped", 0), stats.get("errors", 0),
            stats.get("notes", ""), run_id
        ))


def get_recent_runs(limit: int = 10) -> List[Dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM ingestion_runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════
# ENGINE NOTIFICATIONS
# ══════════════════════════════════════════════════════════════
def _notify(conn: sqlite3.Connection, event_type: str, ticker: str, payload: dict):
    conn.execute("""
        INSERT INTO engine_notifications(event_type, ticker, payload)
        VALUES(?,?,?)
    """, (event_type, ticker, json.dumps(payload)))


def get_pending_notifications(limit: int = 100) -> List[Dict]:
    """Engine calls this to learn about universe changes."""
    with db() as conn:
        rows = conn.execute("""
            SELECT * FROM engine_notifications
            WHERE processed=0
            ORDER BY created_at ASC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def mark_notifications_processed(ids: List[int]):
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        for nid in ids:
            conn.execute(
                "UPDATE engine_notifications SET processed=1, processed_at=? WHERE id=?",
                (now, nid)
            )


# ══════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ══════════════════════════════════════════════════════════════
def _log(conn: sqlite3.Connection, run_id: str, action: str,
         ticker: Optional[str], asset_type: Optional[str],
         source: str, details: dict):
    conn.execute("""
        INSERT INTO ingestion_logs(run_id, action, ticker, asset_type, details, source)
        VALUES(?,?,?,?,?,?)
    """, (run_id, action, ticker, asset_type, json.dumps(details), source))
