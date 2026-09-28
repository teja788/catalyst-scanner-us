"""Tests for the 2026-09 signal-quality improvements (no network).

Each pins a false positive / blind spot seen in the live 10-day run:
  - GSAT "$30M insider buy" was a private estate-planning purchase (footnote)
  - BBD cluster priced in BRL; ETRA/CV placement-price "buys"
  - CRML "$125M" catalyst was a 2023 agreement recited in a 20-F
  - federal-contract list full of $4k awards to $50B+ companies
  - superinvestor 13F-HR rows said only "filed recently"
"""
import pytest

from scanner.context_pack import (_build_priority, _event_text, _filter_contracts,
                                  _flag_price_mismatch, _materiality)
from scanner.ingest_ownership import _parse_form4, diff_13f, parse_13f_table

_FORM4 = """<ownershipDocument>
<reportingOwner><reportingOwnerId><rptOwnerName>MONROE JAMES III</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship></reportingOwner>
<nonDerivativeTable><nonDerivativeTransaction>
  <transactionDate><value>2026-09-18</value></transactionDate>
  <transactionCoding><transactionCode>P</transactionCode>{fid}</transactionCoding>
  <transactionAmounts><transactionShares><value>1000</value></transactionShares>
  <transactionPricePerShare><value>82.52</value></transactionPricePerShare></transactionAmounts>
  <postTransactionAmounts><sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
</nonDerivativeTransaction></nonDerivativeTable>
<footnotes><footnote id="F1">{note}</footnote></footnotes>
</ownershipDocument>"""


def test_private_purchase_footnote_is_not_an_open_market_buy():
    p = _parse_form4(_FORM4.format(fid='<footnoteId id="F1"/>',
                                   note="Shares purchased from James Lynch in a private transaction "
                                        "for estate planning purposes."))
    assert p["side"] == "BUY-PRIVATE" and not p["is_buy"]
    assert "not open-market" in p["detail"]


def test_unrelated_footnote_does_not_flag_and_holding_growth_is_shown():
    p = _parse_form4(_FORM4.format(fid="", note="Held via a private placement vehicle."))  # not on the buy leg
    assert p["side"] == "BUY" and p["is_buy"]
    assert "holding +25% (4,000 → 5,000 sh)" in p["detail"]


def test_event_text_drops_historical_recitals():
    body = ("Item 1.01 Entry into a Material Definitive Agreement. On July 4, 2023, the Company entered "
            "into an agreement to draw up to $125 million. On September 21, 2026, the Company signed a "
            "$40 million supply agreement. As previously disclosed, the Company raised $900 million.")
    txt = _event_text(body, "2026-09-25T17:20:00-04:00")
    assert "$40 million" in txt and "$125 million" not in txt and "$900 million" not in txt


def test_materiality_prefers_revenue_denominator():
    f = {"cik": "1", "filed_at": "2026-09-21T08:00:00-04:00",
         "body_text": "Item 1.01 The Company won a $40 million contract."}
    idx = {"1": {"market_cap": 2e9}}
    ratio, note = _materiality(f, idx, {"1": 100e6})
    assert ratio == pytest.approx(0.4)
    assert "~40% of annual revenue" in note and "~2% of mcap" in note
    assert _materiality(f, idx, {})[0] == pytest.approx(0.02)          # no revenue -> mcap


def test_implausible_magnitude_is_flagged_and_ranked_last():
    from scanner.context_pack import _rank_catalysts_by_materiality
    hype = {"cik": "n", "filed_at": "2026-09-23", "body_text": "Item 1.01 A deal worth up to $520.5 million."}
    real = {"cik": "r", "filed_at": "2026-09-23", "body_text": "Item 1.01 A $40 million supply contract."}
    pr = {"catalysts": [hype, real]}
    _rank_catalysts_by_materiality(pr, {"n": {"market_cap": 4.3e6}, "r": {"market_cap": 200e6}}, {})
    assert pr["catalysts"] == [real, hype] and "implausible" in hype["_mat_note"]


def test_periodic_reports_never_enter_catalyst_bucket():
    f20 = {"form_type": "20-F", "candidate_tags": ["partnership"], "item_codes": []}
    f8k = {"form_type": "8-K", "candidate_tags": ["partnership"], "item_codes": ["1.01"]}
    assert _build_priority([f20, f8k], [])["catalysts"] == [f8k]


def test_contracts_below_materiality_floor_are_hidden():
    ext = [{"category": "contract", "cik": "big", "amount": 4_260},
           {"category": "contract", "cik": "small", "amount": 16.5e6},
           {"category": "contract", "cik": "small", "amount": None},
           {"category": "fda", "cik": "big"}]
    pr = {"external": ext}
    _filter_contracts(pr, {"big": {"market_cap": 78e9}, "small": {"market_cap": 490e6}},
                      {"signals": {"contract_min_pct_mcap": 0.25}})
    assert [e.get("amount") for e in pr["external"] if e["category"] == "contract"] == [16.5e6]
    assert pr["contracts_hidden"] == 2 and any(e["category"] == "fda" for e in pr["external"])


def test_form4_price_far_from_market_is_flagged_and_cluster_inherits():
    buy = {"cik": "1", "ticker": "BBD", "is_buy": 1, "price": 17.98, "trade_date": "2026-09-21"}
    cluster = {"cik": "1"}
    _flag_price_mismatch([buy], [cluster], {"BBD": {"closes": {"2026-09-19": 3.4, "2026-09-21": 3.37}}})
    assert "foreign currency" in buy["px_warn"] and cluster["px_warn"]
    ok = {"cik": "2", "ticker": "GRAB", "is_buy": 1, "price": 2.89, "trade_date": "2026-09-21"}
    _flag_price_mismatch([ok], [], {"GRAB": {"closes": {"2026-09-21": 2.95}}})
    assert "px_warn" not in ok


_TABLE = """<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
<infoTable><nameOfIssuer>ALLY FINL INC</nameOfIssuer><cusip>02005N100</cusip><value>500</value>
<shrsOrPrnAmt><sshPrnamt>100</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
<infoTable><nameOfIssuer>ALLY FINL INC</nameOfIssuer><cusip>02005N100</cusip><value>250</value>
<shrsOrPrnAmt><sshPrnamt>50</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
<infoTable><nameOfIssuer>ALLY FINL INC</nameOfIssuer><cusip>02005N100</cusip><value>9</value>
<shrsOrPrnAmt><sshPrnamt>9</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt><putCall>Put</putCall></infoTable>
<infoTable><nameOfIssuer>SOME BOND</nameOfIssuer><cusip>BOND00001</cusip><value>9</value>
<shrsOrPrnAmt><sshPrnamt>9</sshPrnamt><sshPrnamtType>PRN</sshPrnamtType></shrsOrPrnAmt></infoTable>
</informationTable>"""


def test_13f_table_sums_share_lines_and_skips_options_and_bonds():
    assert parse_13f_table(_TABLE) == {"02005N100": {"name": "ALLY FINL INC", "shares": 150.0, "value": 750.0}}


def test_13f_diff_kinds():
    prev = {"A": {"shares": 100}, "B": {"shares": 100}, "C": {"shares": 100}, "D": {"shares": 100}}
    cur = {"B": {"shares": 160}, "C": {"shares": 40}, "D": {"shares": 110}, "E": {"shares": 5}}
    assert diff_13f(prev, cur) == [("A", "EXIT", 100, 0.0), ("B", "ADD", 100, 160),
                                   ("C", "CUT", 100, 40), ("E", "NEW", 0.0, 5)]


def test_universe_rebuild_refuses_to_write_a_gutted_map(tmp_path, monkeypatch):
    from scanner import universe
    monkeypatch.setattr(universe, "UNIVERSE_DIR", tmp_path)
    monkeypatch.setattr(universe, "fetch_listed", lambda s: [])          # bot-check page -> 0 rows
    monkeypatch.setattr(universe, "fetch_cik_map", lambda s: ({}, {}, {}))
    monkeypatch.setattr(universe, "fetch_mcaps", lambda s: ({}, {"nasdaq": 4005}))
    with pytest.raises(RuntimeError, match="NOT rebuilt"):
        universe.build_map(session=object())
    assert not (tmp_path / "us_universe.json").exists()


def test_research_log_leads_parse_only_ranked_leads():
    from scanner.research_log import past_leads
    log = ("# Research log\n\n---\n\n## 2026-09-28 12:48 ET — 10-day scan\n<!-- hash:x -->\n\n"
           "### Leads\n\n1. **GRAB — Grab Holdings Limited**\n   - what\n\n### Watch\n- **KOD (Kodiak)**: x\n")
    assert past_leads(log) == [("2026-09-28", "GRAB", "10-day scan")]


def test_last_session_move_and_volume_ratio():
    from scanner.adapters.price import last_session
    closes = {f"2026-09-{d:02d}": 10.0 for d in range(1, 21)} | {"2026-09-21": 12.0}
    vols = {f"2026-09-{d:02d}": 100.0 for d in range(1, 21)} | {"2026-09-21": 3100.0}
    assert last_session({"closes": closes, "volumes": vols}) == {"pct": 20.0, "vol_x": 31.0}
