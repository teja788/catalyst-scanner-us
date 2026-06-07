"""SEC EDGAR ownership ingester (Milestone 5).

Three disclosure signals, all discovered via the shared daily index (M3):
  - SCHEDULE 13D / 13D/A : activist / strategic >5% stake (intent to influence)
  - SCHEDULE 13G / 13G/A : passive >5% stake
  - Form 4 / 4/A         : insider transactions; we flag open-market BUYS (code P)
  - 13F-HR               : quarterly institutional holdings (SLOW / lagged signal)

Indexing facts verified live (2026-06):
  - For Form 4 and SCHEDULE 13*, the daily-index CIK is the ISSUER (subject
    company) -> filter cheaply to our universe by that CIK; the FILER (the
    investor/insider) comes from the filing's full-submission .txt.
  - For 13F-HR, the daily-index CIK is the FILER (the manager) -> match the
    daily-index company NAME against the superinvestor watchlist instead.

Each matched filing is fetched once as its full-submission .txt, which contains
both the SGML header (FILED BY, acceptance time) and the embedded ownership XML
(transaction codes). Superinvestor matching is a case-insensitive substring of
the filer/owner name against config/superinvestors.yaml.

NOTE: Form 3/5 (initial/annual) are skipped for now (lower signal); 13F holdings
are flagged at the filing level (the full holdings->universe diff via CUSIP is a
documented phase-2 refinement).
"""
from __future__ import annotations

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser

from scanner.config import load_settings, load_superinvestors
from scanner.http import PoliteSession
from scanner.ingest_edgar import _iter_dates, fetch_daily_index
from scanner.universe import load_map

log = logging.getLogger(__name__)

# Concurrent fetch workers. The shared rate gate in http.py keeps the aggregate
# under the SEC cap regardless; workers only hide per-request network latency.
_WORKERS = 8

# Issuer-keyed ownership forms (daily-index CIK == issuer, so filterable by universe).
ISSUER_FORMS = {
    "4", "4/A",
    "SCHEDULE 13D", "SCHEDULE 13D/A",
    "SCHEDULE 13G", "SCHEDULE 13G/A",
}


def _et() -> ZoneInfo:
    return ZoneInfo(load_settings().get("timezone", "America/New_York"))


def _now() -> datetime:
    return datetime.now(_et())


def _dedupe_hash(accession: str) -> str:
    return hashlib.sha1(accession.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Superinvestor matching
# --------------------------------------------------------------------------- #
def _superinvestors() -> list[str]:
    return [str(x).strip() for x in (load_superinvestors().get("investors") or []) if str(x).strip()]


def _match_investor(name: str, watchlist: list[str]) -> str | None:
    """Word-boundary match of a filer/owner name to the watchlist, so a surname
    like 'Ackman' does NOT match inside 'Jackman Worthing'."""
    if not name:
        return None
    low = name.lower()
    for inv in watchlist:
        if re.search(rf"(?<![a-z0-9]){re.escape(inv.lower())}(?![a-z0-9])", low):
            return inv
    return None


# --------------------------------------------------------------------------- #
# Tiny XML/SGML helpers (regex — ownership docs are namespace-free)
# --------------------------------------------------------------------------- #
def _tag(text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
    return m.group(1).strip() if m else None


def _val(block: str, tag: str) -> str | None:
    """Extract <tag><value>X</value></tag> (the Form 4 amount pattern)."""
    m = re.search(rf"<{tag}>\s*<value>(.*?)</value>", block, re.S)
    return m.group(1).strip() if m else None


def _num(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _flag(text: str, tag: str) -> bool:
    v = _tag(text, tag)
    return v is not None and v.strip().lower() in ("1", "true")


def _acceptance_iso(text: str, fallback_date: str) -> str:
    """Parse <ACCEPTANCE-DATETIME>YYYYMMDDHHMMSS (naive ET) from the SGML header."""
    m = re.search(r"<ACCEPTANCE-DATETIME>(\d{14})", text)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=_et()).isoformat()
        except ValueError:
            pass
    try:
        return datetime.strptime(fallback_date, "%Y%m%d").replace(tzinfo=_et()).isoformat()
    except ValueError:
        return fallback_date


# --------------------------------------------------------------------------- #
# Per-form parsers
# --------------------------------------------------------------------------- #
def _parse_form4(text: str) -> dict[str, Any]:
    """Extract owner, relationship, side (BUY/SELL/OTHER), shares, price from a Form 4."""
    m = re.search(r"<ownershipDocument>(.*?)</ownershipDocument>", text, re.S)
    doc = m.group(1) if m else text
    owner = _tag(doc, "rptOwnerName") or ""
    rel = []
    if _flag(doc, "isDirector"):
        rel.append("Director")
    if _flag(doc, "isOfficer"):
        rel.append((_tag(doc, "officerTitle") or "Officer").strip())
    if _flag(doc, "isTenPercentOwner"):
        rel.append("10% owner")

    blocks = re.findall(r"<(?:nonDerivative|derivative)Transaction>(.*?)</(?:nonDerivative|derivative)Transaction>",
                        doc, re.S)
    codes: list[str] = []
    buy_sh = sell_sh = 0.0
    price: float | None = None
    for b in blocks:
        code = _tag(b, "transactionCode") or ""
        sh = _num(_val(b, "transactionShares"))
        pr = _num(_val(b, "transactionPricePerShare"))
        if code:
            codes.append(code)
        if code == "P" and sh:
            buy_sh += sh
        elif code == "S" and sh:
            sell_sh += sh
        if pr and (price is None or pr > price):
            price = pr

    side = "BUY" if "P" in codes else ("SELL" if "S" in codes else "OTHER")
    shares = buy_sh if side == "BUY" else (sell_sh if side == "SELL" else None)
    return {
        "filer_name": owner,
        "relationship": ", ".join(rel),
        "side": side,
        "shares": shares or None,
        "price": price,
        "is_buy": "P" in codes,
    }


def _parse_sc13(text: str) -> dict[str, Any]:
    """Extract the FILED BY filer + (best-effort) percent-of-class from a 13D/13G."""
    filer = filer_cik = None
    fb = text.find("FILED BY")
    if fb >= 0:
        seg = text[fb:fb + 800]
        mn = re.search(r"COMPANY CONFORMED NAME:\s*(.+)", seg)
        mc = re.search(r"CENTRAL INDEX KEY:\s*(\d+)", seg)
        filer = mn.group(1).strip() if mn else None
        filer_cik = mc.group(1).strip() if mc else None
    pct = None
    # Modern 13D/G (2024+) is structured XML: <percentOfClass>5.2</percentOfClass>
    # (sometimes wrapped in <value>). Fall back to the legacy cover-page text.
    mx = re.search(r"<percentOfClass>\s*(?:<value>)?\s*(\d{1,3}(?:\.\d+)?)", text, re.I)
    mp = mx or re.search(r"PERCENT OF CLASS[^%\d]{0,80}?(\d{1,3}(?:\.\d+)?)\s*%", text, re.I | re.S)
    if mp:
        try:
            pct = float(mp.group(1))
        except ValueError:
            pass
    return {"filer_name": filer or "", "filer_cik": filer_cik, "pct": pct}


# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #
def _txt_url(cik_int: int, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}.txt"


def _index_url(cik_int: int, accession: str) -> str:
    accn = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn}/{accession}-index.htm"


def ingest(session: PoliteSession | None = None,
           since: datetime | None = None,
           until: datetime | None = None,
           forms: set[str] | None = None) -> list[dict[str, Any]]:
    """Fetch ownership disclosures for the universe within [since, until].

    Discovery via the daily index; each matched filing fetched once as .txt.
    `forms` restricts the issuer-keyed forms processed (default = all of
    ISSUER_FORMS); pass e.g. just the SCHEDULE 13D/13G set to skip the heavy
    Form-4 sweep on a wide backfill. Per-filing failures are isolated and logged.
    """
    session = session or PoliteSession()
    forms = forms or ISSUER_FORMS
    lookback = int(load_settings().get("lookback_hours", 24))
    since = since or (_now() - timedelta(hours=lookback))
    until = until or _now()

    by_cik = {int(c["cik"]): c for c in load_map()}
    watchlist = _superinvestors()

    # 1. Discover from the daily index.
    issuer_hits: list[dict[str, Any]] = []     # Form 4 / 13D / 13G about our companies
    f13_hits: list[dict[str, Any]] = []        # 13F-HR by a watchlist manager
    for d in _iter_dates(since, until):
        for r in fetch_daily_index(session, d):
            if r["form"] in forms and r["cik"] in by_cik:
                issuer_hits.append(r)
            elif r["form"].startswith("13F"):
                if _match_investor(r["company"], watchlist):
                    f13_hits.append(r)

    # 2. Issuer-keyed forms (Form 4 + 13D/13G): fetch each .txt once, parse.
    #    Fetched concurrently (latency-hiding) under the shared SEC rate gate.
    def _process_issuer(r: dict[str, Any]) -> dict[str, Any] | None:
        meta = by_cik[r["cik"]]
        try:
            text = session.edgar_get(_txt_url(r["cik"], r["accession"]), timeout=45).text
        except Exception as exc:  # noqa: BLE001 - isolate per-filing failures
            log.warning("ownership .txt %s -> %s", r["accession"], exc)
            return None
        is_13d = r["form"].startswith("SCHEDULE 13D")
        if r["form"].startswith("4"):
            p = _parse_form4(text)
            rec = {**p, "form_type": r["form"], "is_insider": 1, "is_activist": 0}
        else:  # SCHEDULE 13D / 13G
            p = _parse_sc13(text)
            # Self-filing (filer CIK == issuer CIK) is a treasury/subsidiary 13D, not
            # an external activist stake — don't flag it (e.g. "JPM 100% stake").
            is_self = bool(p["filer_cik"]) and p["filer_cik"].lstrip("0") == str(r["cik"])
            rec = {"filer_name": p["filer_name"], "relationship": "", "side": "STAKE",
                   "shares": None, "price": None, "pct": p["pct"], "is_buy": 0,
                   "form_type": r["form"], "is_insider": 0,
                   "is_activist": 1 if (is_13d and not is_self) else 0}
        rec.update({
            "ticker": meta.get("ticker", ""),
            "cik": meta["cik"],
            "company": meta.get("name") or r["company"],
            "matched_investor": _match_investor(rec.get("filer_name", ""), watchlist),
            "filing_url": _index_url(r["cik"], r["accession"]),
            "filed_at": _acceptance_iso(text, r["date"]),
            "accession": r["accession"],
            "dedupe_hash": _dedupe_hash(r["accession"]),
            "source": "SEC EDGAR",
        })
        rec.setdefault("pct", None)
        return rec

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        out: list[dict[str, Any]] = [rec for rec in pool.map(_process_issuer, issuer_hits) if rec]

    # 3. 13F-HR by a watchlist manager (quarterly/lagged): record the filing event.
    for r in f13_hits:
        out.append({
            "ticker": "", "cik": "", "company": "",
            "filer_name": r["company"], "relationship": "", "form_type": "13F-HR",
            "side": "13F", "shares": None, "price": None, "pct": None, "is_buy": 0,
            "is_insider": 0, "is_activist": 0,
            "matched_investor": _match_investor(r["company"], watchlist),
            "filing_url": _index_url(r["cik"], r["accession"]),
            "filed_at": _acceptance_iso("", r["date"]),
            "accession": r["accession"],
            "dedupe_hash": _dedupe_hash(r["accession"]),
            "source": "SEC EDGAR",
            "note": "quarterly 13F holdings (lagged); holdings->universe diff is phase-2",
        })

    buys = sum(1 for o in out if o.get("is_buy"))
    marquee = sum(1 for o in out if o.get("matched_investor"))
    log.info("Ownership ingest: %d records (%d insider buys, %d activist 13D, %d superinvestor, %d 13F) [%s..%s]",
             len(out), buys, sum(1 for o in out if o.get("is_activist")), marquee, len(f13_hits),
             since.date(), until.date())
    return out
