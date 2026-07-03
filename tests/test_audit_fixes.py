"""Regression tests for the 2026-07 audit fixes.

Each test pins a specific bug found in the live data / code audit:
  - money-regex: "$400M"/"$1.2B" press-release abbreviations were invisible
  - catalyst snippet: a keyword in the first 140 chars fell through to boilerplate
  - news Tagger: ticker-as-alias matched capitalised English words ("Well-Being"
    → Welltower) — ~9% of tagged news was false
  - ask resolver: stopword tickers leaked back in via their alias ("for" → FOR)
  - buy clusters: unpriced legs were valued at $1/share, fabricating dollar totals
  - Form 4: multi-leg buys took the MAX leg price instead of the weighted average;
    10b5-1 planned buys were indistinguishable from discretionary ones
  - ownership history: 13D/A direction, 13G→13D switch, first-stored-buy are now
    computed deterministically from the store
"""
import sqlite3

import pytest

from scanner import store
from scanner.context_pack import _build_priority, _catalyst_snippet, _max_body_amount
from scanner.ingest_news import Tagger
from scanner.ingest_ownership import _parse_form4


# --------------------------------------------------------------------------- #
# Money extraction
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,expected", [
    ("a $400M supply agreement", 400e6),
    ("valued at $1.2B over five years", 1.2e9),
    ("approximately $5bn of notes", 5e9),
    ("an award of $36,000,000", 36e6),
    ("worth $3.5 million initially and $2.1 billion at scale", 2.1e9),
    ("a $750k grant", 750e3),
    ("fees of $1,250M in aggregate", 1.25e9),
])
def test_money_regex_variants(text, expected):
    assert _max_body_amount(text) == pytest.approx(expected)


def test_money_regex_no_figure():
    assert _max_body_amount("no dollars here, just 400 million users") is None


# --------------------------------------------------------------------------- #
# Catalyst snippet
# --------------------------------------------------------------------------- #
def test_snippet_finds_keyword_in_first_140_chars_when_no_boilerplate():
    body = "Acme signed a supply agreement with BigCo for 10 years covering all products."
    snip = _catalyst_snippet(body)
    assert "supply" in snip.lower()


def test_snippet_skips_cover_page_via_item_heading():
    boiler = "UNITED STATES SECURITIES AND EXCHANGE COMMISSION Washington, D.C. FORM 8-K " * 3
    body = boiler + " Item 1.01 Entry into a Material Definitive Agreement. The Company " \
                    "entered into a supply agreement worth $200 million with BigCo."
    snip = _catalyst_snippet(body)
    assert "supply agreement" in snip.lower()


# --------------------------------------------------------------------------- #
# News Tagger — ticker-as-alias must not tag capitalised English words
# --------------------------------------------------------------------------- #
_UNI = [
    # universe rows in the OLD format (ticker still inside aliases) — the Tagger
    # must be robust to those files too
    {"cik": "0000001", "ticker": "WELL", "name": "Welltower Inc.",
     "aliases": ["well", "welltower"]},
    {"cik": "0000002", "ticker": "OPEN", "name": "Opendoor Technologies Inc.",
     "aliases": ["open", "opendoor technologies"]},
    {"cik": "0000003", "ticker": "PLAY", "name": "Dave & Buster's Entertainment",
     "aliases": ["play", "dave & buster s entertainment"]},
]


def test_ticker_alias_does_not_tag_english_words():
    tagger = Tagger(_UNI)
    assert tagger.tag("Employee Well-Being Slips as Financial Stress Surges") == []
    assert tagger.tag("Markets Open Higher on Fed Hopes") == []
    assert tagger.tag("Play of the Day: Tech Rally Continues") == []


def test_company_name_still_tags():
    tagger = Tagger(_UNI)
    assert tagger.tag("Welltower announces $2B acquisition") == ["0000001"]
    assert tagger.tag("Opendoor Technologies posts record quarter") == ["0000002"]


def test_uppercase_ticker_tier_still_tags_non_stopword():
    tagger = Tagger(_UNI)
    # PLAY is not in TICKER_STOPWORDS — the explicit uppercase mention tags it
    assert tagger.tag("PLAY shares jump 12% after earnings") == ["0000003"]


def test_ask_resolver_stopword_alias_bypass():
    from scanner.cli import _resolve_companies
    uni = [{"cik": "0000004", "ticker": "FOR", "name": "Forestar Group Inc.",
            "aliases": ["for", "forestar"]},
           {"cik": "0000005", "ticker": "NVDA", "name": "NVIDIA Corp",
            "aliases": ["nvidia"]}]
    hits = _resolve_companies("any news for NVDA today?", uni)
    assert [c["ticker"] for c in hits] == ["NVDA"]
    # the distinctive name alias still resolves it
    assert [c["ticker"] for c in _resolve_companies("what about forestar?", uni)] == ["FOR"]


# --------------------------------------------------------------------------- #
# Buy clusters — no fabricated dollars from unpriced legs
# --------------------------------------------------------------------------- #
def _buy(filer, shares, price, trade_date, cik="0001234567"):
    return {"cik": cik, "ticker": "T", "company": "TestCo", "filer_name": filer,
            "relationship": "Director", "form_type": "4", "side": "BUY",
            "shares": shares, "price": price, "pct": None, "matched_investor": None,
            "is_insider": 1, "is_buy": 1, "is_activist": 0, "detail": "",
            "filing_url": "", "accession": "", "filed_at": "2026-06-10T18:05:00-04:00",
            "trade_date": trade_date}


def test_cluster_usd_excludes_unpriced_legs():
    rows = [_buy("ALPHA", 500_000.0, None, "2026-06-08"),
            _buy("BETA", 10_000.0, 30.0, "2026-06-09")]
    cl = _build_priority([], rows)["buy_clusters"][0]
    assert cl["usd"] == 300_000.0          # only the priced leg
    assert cl["unpriced_shares"] == 500_000.0


def test_insider_buy_floor_drops_token_buys_but_keeps_unpriced():
    rows = [_buy("TINY", 100.0, 5.0, "2026-06-08"),          # $500 — floored out
            _buy("BIG", 10_000.0, 30.0, "2026-06-09"),        # $300k — kept
            _buy("NOPRICE", 50_000.0, None, "2026-06-09")]    # unpriced — kept
    buys = _build_priority([], rows)["insider_buys"]
    assert {b["filer_name"] for b in buys} == {"BIG", "NOPRICE"}


# --------------------------------------------------------------------------- #
# Form 4 parsing — weighted price + 10b5-1 marker
# --------------------------------------------------------------------------- #
_FORM4 = """<ownershipDocument>
<aff10b5One>{flag}</aff10b5One>
<reportingOwner><reportingOwnerId><rptOwnerName>DOE JANE</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship></reportingOwner>
<nonDerivativeTable>
<nonDerivativeTransaction>
  <transactionDate><value>2026-06-09</value></transactionDate>
  <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
  <transactionCode>P</transactionCode>
  <transactionAmounts><transactionShares><value>1000</value></transactionShares>
  <transactionPricePerShare><value>10</value></transactionPricePerShare></transactionAmounts>
</nonDerivativeTransaction>
<nonDerivativeTransaction>
  <transactionDate><value>2026-06-09</value></transactionDate>
  <transactionCode>P</transactionCode>
  <transactionAmounts><transactionShares><value>3000</value></transactionShares>
  <transactionPricePerShare><value>20</value></transactionPricePerShare></transactionAmounts>
</nonDerivativeTransaction>
</nonDerivativeTable>
</ownershipDocument>"""


def test_form4_weighted_average_price_not_max():
    p = _parse_form4(_FORM4.format(flag="0"))
    assert p["is_buy"] and p["shares"] == 4000
    # (1000*10 + 3000*20) / 4000 = 17.5 — the old max() said 20
    assert p["price"] == pytest.approx(17.5)
    assert "discretionary" in p["detail"]


def test_form4_10b5_1_flag_marks_planned_buys():
    p = _parse_form4(_FORM4.format(flag="1"))
    assert "10b5-1" in p["detail"]


# --------------------------------------------------------------------------- #
# Ownership-history helpers (deterministic direction / novelty)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store.init_db(conn)
    yield conn
    conn.close()


def _own_row(form, pct=None, is_buy=0, filed="2026-06-01T10:00:00-04:00", acc="A1"):
    return {"cik": "0000009", "ticker": "T", "company": "TestCo",
            "filer_name": "FUND LP", "relationship": "", "form_type": form,
            "side": "STAKE", "shares": None, "price": None, "trade_date": None,
            "pct": pct, "matched_investor": None, "is_insider": 0, "is_buy": is_buy,
            "is_activist": 0, "detail": "", "filing_url": "", "accession": acc,
            "filed_at": filed, "dedupe_hash": acc}


def test_prior_stake_pct_returns_latest_before(mem_conn):
    store.upsert_ownership([
        _own_row("SCHEDULE 13D", pct=6.0, filed="2026-05-01T10:00:00-04:00", acc="A1"),
        _own_row("SCHEDULE 13D/A", pct=8.1, filed="2026-06-01T10:00:00-04:00", acc="A2"),
    ], conn=mem_conn)
    prev = store.prior_stake_pct("0000009", "FUND LP", "2026-06-15T10:00:00-04:00", conn=mem_conn)
    assert prev == 8.1


def test_prior_13g_exists_detects_switch(mem_conn):
    store.upsert_ownership([
        _own_row("SCHEDULE 13G", pct=5.2, filed="2026-04-01T10:00:00-04:00", acc="G1"),
    ], conn=mem_conn)
    assert store.prior_13g_exists("0000009", "FUND LP", "2026-06-15T10:00:00-04:00", conn=mem_conn)
    assert not store.prior_13g_exists("0000009", "OTHER FUND", "2026-06-15T10:00:00-04:00", conn=mem_conn)


def test_prior_buy_exists_first_buy_detection(mem_conn):
    assert not store.prior_buy_exists("0000009", "FUND LP", "2026-06-15T10:00:00-04:00", conn=mem_conn)
    store.upsert_ownership([_own_row("4", is_buy=1, filed="2026-05-20T10:00:00-04:00", acc="B1")],
                           conn=mem_conn)
    assert store.prior_buy_exists("0000009", "FUND LP", "2026-06-15T10:00:00-04:00", conn=mem_conn)


# --------------------------------------------------------------------------- #
# Price reaction math (pure function — no network)
# --------------------------------------------------------------------------- #
def test_price_reaction_baseline_is_close_before_event():
    from scanner.adapters.price import reaction
    closes = {"2026-06-08": 10.0, "2026-06-09": 10.5, "2026-06-10": 13.0, "2026-06-12": 12.0}
    r = reaction(closes, "2026-06-10T08:00:00-04:00")
    assert r["baseline"] == 10.5           # last close BEFORE the event day
    assert r["last"] == 12.0
    assert r["pct_since_event"] == pytest.approx(14.3, abs=0.05)


def test_price_reaction_none_when_history_too_short():
    from scanner.adapters.price import reaction
    assert reaction({"2026-06-12": 12.0}, "2026-06-10") is None
    assert reaction({}, "2026-06-10") is None
