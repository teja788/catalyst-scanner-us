"""Round-trip tests for the ownership `trade_date` column (schema + migration).

trade_date feeds the buy-cluster dedupe in context_pack._build_priority. If the
column were dropped from the upsert list (or the migration), the store would
silently write NULL and the dedupe key would lose its date component — no other
test would notice.
"""
import sqlite3

from scanner import store


def _row(trade_date: str | None) -> dict:
    return {
        "cik": "0001234567", "ticker": "LFTO", "company": "Liftoff Mobile",
        "filer_name": "GENERAL ATLANTIC GENPAR, L.P.", "relationship": "10% owner",
        "form_type": "4", "side": "BUY", "shares": 1304347.0, "price": 23.0,
        "trade_date": trade_date, "pct": None, "matched_investor": None,
        "is_insider": 1, "is_buy": 1, "is_activist": 0, "detail": "",
        "filing_url": "https://www.sec.gov/example",
        "accession": "0000950142-26-001717", "filed_at": "2026-06-10T18:05:00-04:00",
        "dedupe_hash": "deadbeef",
    }


def _roundtrip(trade_date: str | None) -> dict:
    store.init_db()
    assert store.upsert_ownership([_row(trade_date)]) == 1
    rows = store.get_recent_ownership("2000-01-01")
    assert len(rows) == 1
    return rows[0]


def test_fresh_db_stores_trade_date(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "fresh.db")
    assert _roundtrip("2026-06-09")["trade_date"] == "2026-06-09"


def test_pre_trade_date_db_is_migrated(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    # ownership table as it existed BEFORE trade_date (detail already migrated in)
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE ownership (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cik TEXT, ticker TEXT, company TEXT, filer_name TEXT, relationship TEXT,
        form_type TEXT, side TEXT, shares REAL, price REAL, pct REAL,
        matched_investor TEXT, is_insider INTEGER, is_buy INTEGER,
        is_activist INTEGER, detail TEXT, filing_url TEXT, accession TEXT,
        filed_at TEXT, ingested_at TEXT, dedupe_hash TEXT UNIQUE)""")
    conn.commit()
    conn.close()
    assert _roundtrip("2026-06-09")["trade_date"] == "2026-06-09"
