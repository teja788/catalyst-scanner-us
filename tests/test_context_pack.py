"""Regression tests for buy-cluster aggregation (context_pack._build_priority).

Bug (2026-06-12 pack): LFTO showed "[BUY CLUSTER] 2 insiders bought ≈$59,999,962"
but the two Form 4s reported the SAME purchase — 1,304,347 sh @ $23.00 on
2026-06-09 — filed by two related General Atlantic entities (accessions
0000950142-26-001717 / -001716). The cluster aggregation must dedupe identical
trades — same (issuer, trade date, shares, price) — before counting insiders and
summing dollars. The per-filer insider_buys rows stay intact: both filings are real.
"""
from scanner.context_pack import _build_priority


def _buy(filer: str, shares: float, price: float, trade_date: str | None,
         accession: str, cik: str = "0001234567") -> dict:
    return {
        "cik": cik, "ticker": "LFTO", "company": "Liftoff Mobile",
        "filer_name": filer, "relationship": "10% owner", "form_type": "4",
        "side": "BUY", "shares": shares, "price": price, "pct": None,
        "matched_investor": None, "is_insider": 1, "is_buy": 1, "is_activist": 0,
        "detail": "", "filing_url": "https://www.sec.gov/example",
        "accession": accession, "filed_at": "2026-06-10T18:05:00-04:00",
        "trade_date": trade_date,
    }


def test_same_trade_by_related_cofilers_is_not_a_cluster():
    rows = [
        _buy("GENERAL ATLANTIC GENPAR, L.P.", 1304347.0, 23.0, "2026-06-09",
             "0000950142-26-001717"),
        _buy("General Atlantic (LFT), L.P.", 1304347.0, 23.0, "2026-06-09",
             "0000950142-26-001716"),
    ]
    priority = _build_priority([], rows)
    assert priority["buy_clusters"] == [], (
        "one purchase reported by two related co-filers must not aggregate as a "
        "2-insider $60M cluster"
    )
    # both real filings still surface individually
    assert len(priority["insider_buys"]) == 2


def test_same_trade_dedupe_works_for_legacy_rows_without_trade_date():
    # rows ingested before trade_date existed carry None — the pair still dedupes
    rows = [
        _buy("GENERAL ATLANTIC GENPAR, L.P.", 1304347.0, 23.0, None,
             "0000950142-26-001717"),
        _buy("General Atlantic (LFT), L.P.", 1304347.0, 23.0, None,
             "0000950142-26-001716"),
    ]
    assert _build_priority([], rows)["buy_clusters"] == []


def test_distinct_trades_still_cluster():
    rows = [
        _buy("ALPHA CEO", 1000.0, 10.0, "2026-06-08", "0000000001-26-000001"),
        _buy("BETA CFO", 2000.0, 20.0, "2026-06-09", "0000000002-26-000001"),
    ]
    clusters = _build_priority([], rows)["buy_clusters"]
    assert len(clusters) == 1
    assert clusters[0]["n_insiders"] == 2
    assert clusters[0]["usd"] == 1000.0 * 10.0 + 2000.0 * 20.0


def test_same_size_trades_on_different_days_are_not_deduped():
    # identical shares@price but different trade dates = two genuine buys
    rows = [
        _buy("ALPHA CEO", 1000.0, 10.0, "2026-06-08", "0000000001-26-000001"),
        _buy("BETA CFO", 1000.0, 10.0, "2026-06-09", "0000000002-26-000001"),
    ]
    clusters = _build_priority([], rows)["buy_clusters"]
    assert len(clusters) == 1
    assert clusters[0]["n_insiders"] == 2
    assert clusters[0]["usd"] == 20000.0


def test_cluster_survives_dedupe_when_a_third_genuine_buy_exists():
    rows = [
        _buy("GENERAL ATLANTIC GENPAR, L.P.", 1304347.0, 23.0, "2026-06-09",
             "0000950142-26-001717"),
        _buy("General Atlantic (LFT), L.P.", 1304347.0, 23.0, "2026-06-09",
             "0000950142-26-001716"),
        _buy("GAMMA DIRECTOR", 10000.0, 25.0, "2026-06-10", "0000000003-26-000001"),
    ]
    clusters = _build_priority([], rows)["buy_clusters"]
    assert len(clusters) == 1
    assert clusters[0]["n_insiders"] == 2          # GA pair collapses to one buyer
    assert clusters[0]["usd"] == 1304347.0 * 23.0 + 10000.0 * 25.0
