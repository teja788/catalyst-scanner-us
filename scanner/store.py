"""SQLite storage: schema, dedupe-safe upserts, catch-up tracking, read queries.

Mirrors the India project's store.py design:
- Dedupe is enforced by a UNIQUE index on each table's `dedupe_hash` plus
  `INSERT OR IGNORE`, so re-running an ingester never creates duplicates -- the
  database is the single source of truth, not the caller.
- Catch-up is driven by the `runs` table: each source records its
  `last_success_at`; the next refresh fetches only since then.
- Timestamps are stored as ISO-8601 strings in ET.

US adaptations: keyed on SEC CIK; `filings` (form_type + item_codes), `news`
(company_ciks), `ownership` (13D/13G/Form 4/13F).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from scanner.config import load_settings, resolve_path

log = logging.getLogger(__name__)
DB_PATH = resolve_path("data/catalyst.db")


def _tz() -> ZoneInfo:
    return ZoneInfo(load_settings().get("timezone", "America/New_York"))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    cik         TEXT PRIMARY KEY,
    ticker      TEXT,
    name        TEXT,
    aliases     TEXT,           -- json array
    exchange    TEXT,
    market_cap  REAL,
    sector      TEXT,
    country     TEXT
);

CREATE TABLE IF NOT EXISTS filings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    cik            TEXT,
    ticker         TEXT,
    company        TEXT,
    form_type      TEXT,
    item_codes     TEXT,          -- json array (8-K items)
    headline       TEXT,
    body_text      TEXT,
    filing_url     TEXT,
    accession      TEXT,
    filed_at       TEXT,
    report_date    TEXT,
    ingested_at    TEXT,
    dedupe_hash    TEXT UNIQUE,
    candidate_tags TEXT           -- json array, filled by the prefilter (M7)
);

CREATE TABLE IF NOT EXISTS news (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_ciks  TEXT,           -- json array
    source        TEXT,
    trust         TEXT,
    headline      TEXT,
    url           TEXT,
    summary       TEXT,
    published_at  TEXT,
    ingested_at   TEXT,
    dedupe_hash   TEXT UNIQUE,
    candidate_tags TEXT
);

CREATE TABLE IF NOT EXISTS ownership (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    cik              TEXT,
    ticker           TEXT,
    company          TEXT,
    filer_name       TEXT,
    relationship     TEXT,
    form_type        TEXT,          -- 4 | SCHEDULE 13D | SCHEDULE 13G | 13F-HR
    side             TEXT,          -- BUY | SELL | STAKE | 13F | OTHER
    shares           REAL,
    price            REAL,
    pct              REAL,
    matched_investor TEXT,
    is_insider       INTEGER,
    is_buy           INTEGER,
    is_activist      INTEGER,
    detail           TEXT,          -- "what the amendment did": direction + Item 4/5(c) snippet
    filing_url       TEXT,
    accession        TEXT,
    filed_at         TEXT,
    ingested_at      TEXT,
    dedupe_hash      TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS runs (
    source          TEXT PRIMARY KEY,
    last_success_at TEXT,
    items_fetched   INTEGER,
    status          TEXT,
    note            TEXT,
    updated_at      TEXT
);

-- Stub for Section 17 (UX wired later): prioritise/segregate tagged tickers.
CREATE TABLE IF NOT EXISTS watchlist (
    cik      TEXT PRIMARY KEY,
    ticker   TEXT,
    added_at TEXT,
    note     TEXT
);

-- Cached filing body text (extracted on demand for catalyst-tagged filings, M8),
-- keyed by the filing's dedupe_hash so extraction is done once and reused.
CREATE TABLE IF NOT EXISTS filing_text (
    ref_hash    TEXT PRIMARY KEY,
    url         TEXT,
    text        TEXT,
    n_chars     INTEGER,
    method      TEXT,
    created_at  TEXT
);

-- Non-EDGAR catalyst feeds (M-feeds): federal contracts, FDA/clinical, patents —
-- matched to a universe company by org name. Same dedupe discipline as the rest.
CREATE TABLE IF NOT EXISTS external_catalysts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cik          TEXT,
    ticker       TEXT,
    company      TEXT,
    source       TEXT,          -- USAspending | openFDA | ClinicalTrials | PatentsView
    category     TEXT,          -- contract | fda | patent
    headline     TEXT,
    detail       TEXT,
    amount       REAL,          -- contract $ (nullable)
    url          TEXT,
    event_date   TEXT,
    ingested_at  TEXT,
    dedupe_hash  TEXT UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_filings_cik  ON filings(cik);
CREATE INDEX IF NOT EXISTS idx_filings_at   ON filings(filed_at);
CREATE INDEX IF NOT EXISTS idx_news_pub     ON news(published_at);
CREATE INDEX IF NOT EXISTS idx_own_cik      ON ownership(cik);
CREATE INDEX IF NOT EXISTS idx_own_at       ON ownership(filed_at);
CREATE INDEX IF NOT EXISTS idx_ext_date     ON external_catalysts(event_date);
"""


def _now_iso() -> str:
    return datetime.now(_tz()).isoformat()


def get_conn() -> sqlite3.Connection:
    """Open the DB (creating the file/dir on first use) with Row access.

    timeout=30: the scheduled refresh, CLI, and dashboard can write concurrently
    (WAL allows one writer) — wait out a busy writer instead of raising."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or get_conn()
    try:
        conn.executescript(_SCHEMA)
        # Lightweight migration for DBs created before `detail` existed.
        try:
            conn.execute("ALTER TABLE ownership ADD COLUMN detail TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.commit()
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# Companies (from the universe map)
# --------------------------------------------------------------------------- #
def sync_companies(universe: list[dict[str, Any]], conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or get_conn()
    try:
        rows = [(
            c["cik"], c.get("ticker"), c.get("name"), json.dumps(c.get("aliases", [])),
            c.get("exchange"), c.get("market_cap"), c.get("sector"), c.get("country"),
        ) for c in universe if c.get("cik")]
        conn.executemany(
            """INSERT INTO companies (cik, ticker, name, aliases, exchange, market_cap, sector, country)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET
                 ticker=excluded.ticker, name=excluded.name, aliases=excluded.aliases,
                 exchange=excluded.exchange, market_cap=excluded.market_cap,
                 sector=excluded.sector, country=excluded.country""",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# Dedupe-safe upserts. Each returns the number of NEW rows inserted.
# --------------------------------------------------------------------------- #
def _insert_ignore(conn: sqlite3.Connection, table: str, cols: list[str],
                   records: Iterable[dict[str, Any]]) -> int:
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    before = conn.total_changes
    now = _now_iso()
    payload = []
    for r in records:
        row = dict(r)
        row["ingested_at"] = now
        payload.append([row.get(c) for c in cols])
    conn.executemany(sql, payload)
    conn.commit()
    return conn.total_changes - before


def upsert_filings(items: list[dict[str, Any]], conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or get_conn()
    try:
        cols = ["cik", "ticker", "company", "form_type", "item_codes", "headline",
                "body_text", "filing_url", "accession", "filed_at", "report_date",
                "ingested_at", "dedupe_hash", "candidate_tags"]
        prepared = []
        for it in items:
            r = dict(it)
            r["item_codes"] = json.dumps(r.get("item_codes", []))
            r.setdefault("candidate_tags", "[]")
            prepared.append(r)
        return _insert_ignore(conn, "filings", cols, prepared)
    finally:
        if own:
            conn.close()


def upsert_news(items: list[dict[str, Any]], conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or get_conn()
    try:
        cols = ["company_ciks", "source", "trust", "headline", "url", "summary",
                "published_at", "ingested_at", "dedupe_hash", "candidate_tags"]
        prepared = []
        for it in items:
            r = dict(it)
            r["company_ciks"] = json.dumps(r.get("company_ciks", []))
            r.setdefault("candidate_tags", "[]")
            prepared.append(r)
        return _insert_ignore(conn, "news", cols, prepared)
    finally:
        if own:
            conn.close()


def upsert_ownership(items: list[dict[str, Any]], conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or get_conn()
    try:
        cols = ["cik", "ticker", "company", "filer_name", "relationship", "form_type",
                "side", "shares", "price", "pct", "matched_investor", "is_insider",
                "is_buy", "is_activist", "detail", "filing_url", "accession", "filed_at",
                "ingested_at", "dedupe_hash"]
        prepared = []
        for it in items:
            r = dict(it)
            for k in ("is_insider", "is_buy", "is_activist"):
                r[k] = int(bool(r.get(k)))
            prepared.append(r)
        return _insert_ignore(conn, "ownership", cols, prepared)
    finally:
        if own:
            conn.close()


def upsert_external_catalysts(items: list[dict[str, Any]], conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or get_conn()
    try:
        cols = ["cik", "ticker", "company", "source", "category", "headline", "detail",
                "amount", "url", "event_date", "ingested_at", "dedupe_hash"]
        return _insert_ignore(conn, "external_catalysts", cols, list(items))
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# Run / catch-up tracking
# --------------------------------------------------------------------------- #
def get_last_success(source: str, conn: sqlite3.Connection | None = None) -> datetime | None:
    own = conn is None
    conn = conn or get_conn()
    try:
        row = conn.execute("SELECT last_success_at FROM runs WHERE source=?", (source,)).fetchone()
        if row and row["last_success_at"]:
            try:
                return datetime.fromisoformat(row["last_success_at"])
            except ValueError:
                return None
        return None
    finally:
        if own:
            conn.close()


def mark_run(source: str, items_fetched: int, status: str, note: str = "",
             success_at: datetime | None = None, conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or get_conn()
    try:
        now = _now_iso()
        last = (success_at or datetime.now(_tz())).isoformat() if status == "ok" else None
        if last is None:  # preserve the prior cursor on failure
            prior = conn.execute("SELECT last_success_at FROM runs WHERE source=?", (source,)).fetchone()
            last = prior["last_success_at"] if prior else None
        conn.execute(
            """INSERT INTO runs (source, last_success_at, items_fetched, status, note, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(source) DO UPDATE SET
                 last_success_at=excluded.last_success_at, items_fetched=excluded.items_fetched,
                 status=excluded.status, note=excluded.note, updated_at=excluded.updated_at""",
            (source, last, items_fetched, status, note, now),
        )
        conn.commit()
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# Read queries
# --------------------------------------------------------------------------- #
def _rows(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _placeholders(n: int) -> str:
    return ",".join("?" for _ in range(n))


def counts(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    own = conn is None
    conn = conn or get_conn()
    try:
        return {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                for t in ("companies", "filings", "news", "ownership", "external_catalysts")}
    finally:
        if own:
            conn.close()


def get_runs(conn: sqlite3.Connection | None = None) -> dict[str, dict[str, Any]]:
    """source -> last run row. Feeds the pack's data-freshness header, so the
    agent can tell 'quiet day' apart from 'nothing fetched lately'."""
    own = conn is None
    conn = conn or get_conn()
    try:
        return {r["source"]: dict(r) for r in conn.execute("SELECT * FROM runs").fetchall()}
    finally:
        if own:
            conn.close()


def coverage(conn: sqlite3.Connection | None = None) -> dict[str, dict[str, Any]]:
    """Stored count + date range per source, so the UI knows what it already has."""
    own = conn is None
    conn = conn or get_conn()
    try:
        out: dict[str, dict[str, Any]] = {}
        for table, col in (("filings", "filed_at"), ("news", "published_at"),
                           ("ownership", "filed_at"), ("external_catalysts", "event_date")):
            row = conn.execute(f"SELECT COUNT(*) n, MIN({col}) lo, MAX({col}) hi FROM {table}").fetchone()
            out[table] = {"count": row["n"], "earliest": row["lo"], "latest": row["hi"]}
        return out
    finally:
        if own:
            conn.close()


def get_recent_filings(since_iso: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or get_conn()
    try:
        return _rows(conn, "SELECT * FROM filings WHERE filed_at >= ? ORDER BY filed_at DESC", (since_iso,))
    finally:
        if own:
            conn.close()


def get_recent_news(since_iso: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or get_conn()
    try:
        return _rows(conn, "SELECT * FROM news WHERE published_at >= ? ORDER BY published_at DESC", (since_iso,))
    finally:
        if own:
            conn.close()


def get_recent_ownership(since_iso: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or get_conn()
    try:
        return _rows(conn, "SELECT * FROM ownership WHERE filed_at >= ? ORDER BY filed_at DESC", (since_iso,))
    finally:
        if own:
            conn.close()


def get_recent_external_catalysts(since_iso: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or get_conn()
    try:
        return _rows(conn, "SELECT * FROM external_catalysts WHERE event_date >= ? ORDER BY event_date DESC", (since_iso,))
    finally:
        if own:
            conn.close()


def filings_by_tag(tag: str, limit: int = 50, since_iso: str | None = None,
                   conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or get_conn()
    try:
        where = "candidate_tags LIKE ?"
        params: list[Any] = [f'%"{tag}"%']
        if since_iso:
            where += " AND filed_at >= ?"
            params.append(since_iso)
        params.append(limit)
        return _rows(conn, f"SELECT * FROM filings WHERE {where} ORDER BY filed_at DESC LIMIT ?", tuple(params))
    finally:
        if own:
            conn.close()


def set_filing_tags(filing_id: int, tags: list[str], conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or get_conn()
    try:
        conn.execute("UPDATE filings SET candidate_tags=? WHERE id=?", (json.dumps(tags), filing_id))
        conn.commit()
    finally:
        if own:
            conn.close()


def set_filing_tags_bulk(pairs: list[tuple[int, list[str]]],
                         conn: sqlite3.Connection | None = None) -> None:
    """Persist candidate_tags for many filings in ONE transaction (the prefilter
    re-tags every filing in the window — per-row commits made that O(rows) fsyncs)."""
    if not pairs:
        return
    own = conn is None
    conn = conn or get_conn()
    try:
        conn.executemany("UPDATE filings SET candidate_tags=? WHERE id=?",
                         [(json.dumps(tags), fid) for fid, tags in pairs])
        conn.commit()
    finally:
        if own:
            conn.close()


# --- targeted reads for `ask` (M10) ---------------------------------------- #
def filings_for_ciks(ciks: list[str], limit: int = 50, since_iso: str | None = None,
                     conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    if not ciks:
        return []
    own = conn is None
    conn = conn or get_conn()
    try:
        where = f"cik IN ({_placeholders(len(ciks))})"
        params: list[Any] = list(ciks)
        if since_iso:
            where += " AND filed_at >= ?"
            params.append(since_iso)
        params.append(limit)
        return _rows(conn, f"SELECT * FROM filings WHERE {where} ORDER BY filed_at DESC LIMIT ?", tuple(params))
    finally:
        if own:
            conn.close()


def ownership_for_ciks(ciks: list[str], limit: int = 50, since_iso: str | None = None,
                       conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    if not ciks:
        return []
    own = conn is None
    conn = conn or get_conn()
    try:
        where = f"cik IN ({_placeholders(len(ciks))})"
        params: list[Any] = list(ciks)
        if since_iso:
            where += " AND filed_at >= ?"
            params.append(since_iso)
        params.append(limit)
        return _rows(conn, f"SELECT * FROM ownership WHERE {where} ORDER BY filed_at DESC LIMIT ?", tuple(params))
    finally:
        if own:
            conn.close()


def news_for_ciks(ciks: list[str], limit: int = 50, since_iso: str | None = None,
                  conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    if not ciks:
        return []
    own = conn is None
    conn = conn or get_conn()
    try:
        clause = " OR ".join("company_ciks LIKE ?" for _ in ciks)
        where = f"({clause})"
        params: list[Any] = [f'%"{c}"%' for c in ciks]
        if since_iso:
            where += " AND published_at >= ?"
            params.append(since_iso)
        params.append(limit)
        return _rows(conn, f"SELECT * FROM news WHERE {where} ORDER BY published_at DESC LIMIT ?", tuple(params))
    finally:
        if own:
            conn.close()


# --- filing body-text cache (M8) ------------------------------------------- #
def get_filing_text(ref_hash: str, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    own = conn is None
    conn = conn or get_conn()
    try:
        row = conn.execute("SELECT * FROM filing_text WHERE ref_hash=?", (ref_hash,)).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def save_filing_text(ref_hash: str, url: str, text: str, method: str,
                     conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or get_conn()
    try:
        conn.execute(
            "INSERT INTO filing_text (ref_hash, url, text, n_chars, method, created_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(ref_hash) DO UPDATE SET "
            "url=excluded.url, text=excluded.text, n_chars=excluded.n_chars, "
            "method=excluded.method, created_at=excluded.created_at",
            (ref_hash, url, text, len(text or ""), method, _now_iso()))
        conn.commit()
    finally:
        if own:
            conn.close()


# --- Watchlist (Section 17 hook). The TABLE + these helpers exist now; the UX
#     (floating watchlisted tickers to the top of the context pack) is wired later. ---
def add_to_watchlist(cik: str, ticker: str = "", note: str = "",
                     conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or get_conn()
    try:
        conn.execute(
            "INSERT INTO watchlist (cik, ticker, added_at, note) VALUES (?,?,?,?) "
            "ON CONFLICT(cik) DO UPDATE SET ticker=excluded.ticker, note=excluded.note",
            (cik, ticker, _now_iso(), note))
        conn.commit()
    finally:
        if own:
            conn.close()


def get_watchlist(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or get_conn()
    try:
        return _rows(conn, "SELECT * FROM watchlist ORDER BY added_at DESC", ())
    finally:
        if own:
            conn.close()
