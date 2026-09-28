"""Regression tests for the 2026-09 audit fixes (no network).

  - 8-K times: read from the SGML header (ET), not the submissions API, whose
    same-day values are ET mislabelled "Z" (stored 4-5h early)
  - catch-up cursor: a narrow --hours never skips the gap; the cursor never passes
    a day that has no published daily index; lost filings keep the cursor
  - same-day filings: the live current-filings feed stands in for today's index
  - superinvestor Form-4 SELLs render with their direction
  - openFDA is paged (one 100-row page dropped ~40% of a 30-day window)
  - body cap counts only network fetches, so re-runs read further
  - price baseline for after-close events is the event day's close
  - news: every feed failing is a failed run, not "ok"
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from scanner import ingest_edgar, store

ET = ZoneInfo("America/New_York")

_HEADER = """<SEC-HEADER>0001437749-26-020293.hdr.sgml : 20260611
<ACCEPTANCE-DATETIME>20260611160919
<ACCESSION-NUMBER>0001437749-26-020293
<TYPE>8-K
<PERIOD>20260610
<ITEMS>5.07
<ITEMS>9.01
<FILING-DATE>20260611
</SEC-HEADER>
<DOCUMENT>
<TYPE>8-K
<SEQUENCE>1
<FILENAME>nwpx20260415c_8k.htm
<DESCRIPTION>FORM 8-K
</DOCUMENT>
<DOCUMENT>
<TYPE>EX-101.SCH
<FILENAME>nwpx-20260610.xsd
<DESCRIPTION>XBRL TAXONOMY EXTENSION SCHEMA
</DOCUMENT>"""


def test_header_gives_true_et_time_items_and_primary_doc():
    h = ingest_edgar.parse_header(_HEADER)
    # live case: the API said 16:09:19 "Z" on the day -> stored as 12:09 ET before the fix
    assert ingest_edgar.acceptance_iso(h["accepted"], "") == "2026-06-11T16:09:19-04:00"
    assert h["items"] == ["5.07", "9.01"]
    assert h["primary"] == "nwpx20260415c_8k.htm"
    assert h["desc"] == "FORM 8-K"
    assert h["period"] == "2026-06-10"


def test_header_without_acceptance_is_a_failure():
    assert ingest_edgar.parse_header("<html>blocked</html>") is None


def test_efts_hit_becomes_one_index_row_per_cik():
    # live shape (2026-09-25): a Form 4 lists the reporting person AND the issuer
    src = {"adsh": "0002153015-26-000004", "form": "4", "file_date": "2026-09-25",
           "display_names": ["Beiboer Paul Gijsbert  (CIK 0002153015)",
                             "Lineage, Inc.  (LINE)  (CIK 0001868159)"]}
    assert ingest_edgar.efts_rows(src) == [
        {"cik": 2153015, "company": "Beiboer Paul Gijsbert", "form": "4", "date": "20260925",
         "accession": "0002153015-26-000004"},
        {"cik": 1868159, "company": "Lineage, Inc.", "form": "4", "date": "20260925",
         "accession": "0002153015-26-000004"}]


@pytest.fixture()
def monday(monkeypatch):
    monkeypatch.setattr(ingest_edgar, "_now", lambda: datetime(2026, 9, 28, 11, 0, tzinfo=ET))
    monkeypatch.setattr(ingest_edgar, "fetch_current_rows", lambda s, d: [{"live": d}])


def test_rows_for_day_final_vs_live(monday, monkeypatch):
    idx = {date(2026, 9, 25): [{"idx": 1}]}
    monkeypatch.setattr(ingest_edgar, "fetch_daily_index", lambda s, d: idx.get(d, []))
    assert ingest_edgar.rows_for_day(None, date(2026, 9, 25)) == ([{"idx": 1}], True)   # published
    assert ingest_edgar.rows_for_day(None, date(2026, 9, 27)) == ([], True)             # Sunday
    assert ingest_edgar.rows_for_day(None, date(2026, 9, 28)) == ([{"live": date(2026, 9, 28)}], False)
    assert ingest_edgar.rows_for_day(None, date(2026, 9, 22)) == ([], True)             # old miss = holiday


# --------------------------------------------------------------------------- #
# Catch-up cursor (cli._refresh_all) against a temp DB with a fake ingester
# --------------------------------------------------------------------------- #
@pytest.fixture()
def refresh(tmp_path, monkeypatch):
    from scanner import cli, ingest_edgar as ie, universe
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(universe, "load_map", lambda: [])
    calls = {}

    def fake_ingest(session=None, since=None, stats=None, **_):
        calls["since"] = since
        stats.update(calls.get("stats", {}))
        return []
    monkeypatch.setattr(ie, "ingest", fake_ingest)
    return cli, calls


def test_narrow_window_still_covers_gap_since_cursor(refresh):
    cli, calls = refresh
    old = datetime(2026, 9, 24, 0, 0, tzinfo=ET)
    store.init_db()
    store.mark_run("edgar", 0, "ok", success_at=old)
    calls["stats"] = {"last_index_day": date(2026, 9, 25), "failed": 0}
    cli._refresh_all(since_override=datetime(2026, 9, 28, 9, 0, tzinfo=ET), sources={"edgar"})
    assert calls["since"] == old                                  # not the narrow --hours start
    assert store.get_last_success("edgar") == datetime(2026, 9, 26, 0, 0, tzinfo=ET)  # day after Fri index


def test_months_old_cursor_catchup_is_capped(refresh):
    cli, calls = refresh
    store.init_db()
    store.mark_run("edgar", 0, "ok", success_at=datetime(2026, 6, 12, tzinfo=ET))
    calls["stats"] = {"last_index_day": None, "failed": 0}
    cli._refresh_all(sources={"edgar"})
    assert (datetime.now(ET) - calls["since"]).days == 14     # not a 3-month sweep


def test_no_final_index_keeps_cursor(refresh):
    cli, calls = refresh
    cur = datetime(2026, 9, 26, 0, 0, tzinfo=ET)
    store.init_db()
    store.mark_run("edgar", 0, "ok", success_at=cur)
    calls["stats"] = {"last_index_day": None, "failed": 0}       # only live-feed / weekend days
    cli._refresh_all(sources={"edgar"})
    assert store.get_last_success("edgar") == cur


def test_failed_filings_keep_cursor_and_mark_error(refresh):
    cli, calls = refresh
    cur = datetime(2026, 9, 24, 0, 0, tzinfo=ET)
    store.init_db()
    store.mark_run("edgar", 0, "ok", success_at=cur)
    calls["stats"] = {"last_index_day": date(2026, 9, 25), "failed": 3}
    res = cli._refresh_all(sources={"edgar"})
    assert res["edgar"]["status"].startswith("error: 3 filings failed")
    assert store.get_last_success("edgar") == cur


# --------------------------------------------------------------------------- #
# Pack rendering / enrichment / prices / feeds
# --------------------------------------------------------------------------- #
def test_superinvestor_sell_shows_direction():
    from scanner.context_pack import _own_detail
    sell = {"form_type": "4", "side": "SELL", "shares": 260.0, "price": 68.06}
    assert _own_detail(sell) == "SELL 260 sh @ $68.06"
    assert "no open-market" in _own_detail({"form_type": "4", "side": "OTHER"})


def test_body_cap_counts_only_network_fetches(monkeypatch):
    from scanner import filing_body
    filings = [{"id": i, "dedupe_hash": f"h{i}", "form_type": "8-K", "item_codes": ["1.01"],
                "candidate_tags": ["contract"]} for i in range(5)]
    monkeypatch.setattr(store, "get_filing_texts", lambda hs: {"h0": "cached", "h1": "cached"})
    monkeypatch.setattr(store, "set_filing_tags_bulk", lambda pairs: None)
    fetched = []
    monkeypatch.setattr(filing_body, "_body_for", lambda s, f: fetched.append(f["id"]) or "new")
    info = filing_body.enrich(filings, session=object(), max_fetch=2)
    assert sorted(fetched) == [2, 3]                  # cap spent on UNCACHED filings only
    assert info == {"targets": 4, "skipped": 1, "retagged": 0}


def test_after_close_event_uses_event_day_close():
    from scanner.adapters.price import reaction
    closes = {"2026-06-09": 10.0, "2026-06-10": 11.0, "2026-06-11": 13.2}
    assert reaction(closes, "2026-06-10T16:09:19-04:00")["baseline"] == 11.0   # after the close
    assert reaction(closes, "2026-06-10T08:00:00-04:00")["baseline"] == 10.0   # pre-market


def test_openfda_is_paged():
    from scanner.ingest_external import fetch_fda

    class Resp:
        def __init__(self, js):
            self._js = js

        def json(self):
            return self._js

    class Session:
        def __init__(self):
            self.skips = []

        def get(self, url, params=None, **_):
            self.skips.append(params["skip"])
            n = 100 if params["skip"] < 100 else 77
            apps = [{"sponsor_name": "ACME PHARMA", "application_number": f"NDA{params['skip'] + i}",
                     "submissions": [{"submission_status": "AP", "submission_status_date": "20260915",
                                      "submission_type": "ORIG"}]} for i in range(n)]
            return Resp({"meta": {"results": {"total": 177}}, "results": apps})

    s = Session()
    out = fetch_fda(s, "2026-09-01", "2026-09-28", {"acme pharma": {"cik": "1", "ticker": "ACM", "company": "Acme"}})
    assert s.skips == [0, 100]
    assert len(out) == 177


def test_all_news_feeds_failing_raises(monkeypatch):
    from scanner import ingest_news
    monkeypatch.setattr(ingest_news, "load_map", lambda: [])
    monkeypatch.setattr(ingest_news, "load_sources", lambda: {"news_feeds": [{"name": "A"}, {"name": "B"}]})
    monkeypatch.setattr(ingest_news, "fetch_feed", lambda *a: (_ for _ in ()).throw(OSError("down")))
    with pytest.raises(RuntimeError, match="all 2 news feeds failed"):
        ingest_news.ingest(session=object())
