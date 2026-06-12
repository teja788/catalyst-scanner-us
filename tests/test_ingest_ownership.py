"""Regression tests for superinvestor matching + Form-4 parsing (ingest_ownership).

Bug (2026-06-12 pack): a Form 4 filed by "LOEB GARY" — an Intuitive Surgical/ISRG
insider, accession 0001561733-26-000012 — was tagged [SUPERINVESTOR: Loeb]. The
watchlist carried the bare surname "Loeb" (intended: Daniel Loeb / Third Point) and
the matcher accepted it. Matching now requires EVERY word of a watchlist entry to
appear in the filer name (any order, because EDGAR lists people surname-first:
"LOEB DANIEL S"), and config/superinvestors.yaml carries full names, not surnames.
"""
from scanner.ingest_ownership import _match_investor, _parse_form4, _superinvestors


# --------------------------------------------------------------------------- #
# _match_investor unit semantics
# --------------------------------------------------------------------------- #
def test_unrelated_insider_sharing_surname_does_not_match():
    assert _match_investor("LOEB GARY", ["Daniel Loeb"]) is None


def test_edgar_surname_first_personal_filing_matches():
    assert _match_investor("LOEB DANIEL S", ["Daniel Loeb"]) == "Daniel Loeb"


def test_fund_entity_matches():
    assert _match_investor("THIRD POINT LLC", ["Third Point"]) == "Third Point"
    assert (_match_investor("PERSHING SQUARE CAPITAL MANAGEMENT, L.P.", ["Pershing Square"])
            == "Pershing Square")


def test_all_entry_words_required():
    assert _match_investor("SQUARE INC", ["Pershing Square"]) is None
    assert _match_investor("POINT BIOPHARMA", ["Third Point"]) is None


def test_single_word_fund_entry_still_matches():
    assert _match_investor("APPALOOSA LP", ["Appaloosa"]) == "Appaloosa"


def test_word_boundary_still_enforced():
    # surname inside a longer word must not match (the pre-existing guarantee)
    assert _match_investor("Jackman Worthing", ["Ackman"]) is None


def test_empty_inputs():
    assert _match_investor("", ["Daniel Loeb"]) is None
    assert _match_investor("LOEB DANIEL S", [""]) is None


# --------------------------------------------------------------------------- #
# Shipped watchlist regression — pins BOTH the matcher and superinvestors.yaml
# (catches a bare surname being re-added to the config).
# --------------------------------------------------------------------------- #
def test_shipped_watchlist_loeb_gary_regression():
    watchlist = _superinvestors()
    assert _match_investor("LOEB GARY", watchlist) is None, (
        "bare-surname false positive: LOEB GARY (ISRG insider) must not match "
        "the Daniel Loeb watchlist entry"
    )
    assert _match_investor("LOEB DANIEL S", watchlist) == "Daniel Loeb"
    assert _match_investor("THIRD POINT LLC", watchlist) == "Third Point"


# --------------------------------------------------------------------------- #
# _parse_form4: trade_date of the buy leg (the buy-cluster dedupe key)
# --------------------------------------------------------------------------- #
FORM4_BUY = """
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>GENERAL ATLANTIC GENPAR, L.P.</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isTenPercentOwner>1</isTenPercentOwner></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-06-09</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1304347</value></transactionShares>
        <transactionPricePerShare><value>23.00</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


def test_parse_form4_buy_carries_trade_date():
    p = _parse_form4(FORM4_BUY)
    assert p["side"] == "BUY"
    assert p["shares"] == 1304347.0
    assert p["price"] == 23.0
    assert p["trade_date"] == "2026-06-09"


def test_parse_form4_non_buy_has_no_trade_date():
    p = _parse_form4("<ownershipDocument><rptOwnerName>X</rptOwnerName></ownershipDocument>")
    assert p["side"] == "OTHER"
    assert p["trade_date"] is None
