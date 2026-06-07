"""SEC EDGAR filings ingester (Milestone 3).

Strategy (Section 5 of the build spec): rather than poll each company, pull the
EDGAR DAILY INDEX (one file per filing day listing ALL filings), filter it to our
universe's CIKs + catalyst FORM types, then enrich each match via the per-company
submissions API — which carries the 8-K item codes, the precise acceptance
timestamp, and the primary-document name (for a direct, clickable source URL).

This scales to the 5,000-name universe: the daily index costs the same regardless
of universe size, and the only per-company calls are for CIKs that ACTUALLY filed
a catalyst form in the window (bounded by activity, not by universe size).

Ownership forms (3/4/5, SCHEDULE 13D/13G, 13F-HR) are captured by the ownership
ingester in M5; this module handles the "company filings" catalyst feed.

Verified live (2026-06): daily-index path + row format, unpadded CIKs, literal
form strings, and that the submissions API exposes 8-K `items`.
"""
from __future__ import annotations

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser

from scanner.config import load_settings, load_sources
from scanner.http import PoliteSession
from scanner.universe import load_map

log = logging.getLogger(__name__)

# Concurrent fetch workers (latency-hiding; the http.py rate gate caps the rate).
_WORKERS = 8

# Catalyst "company filing" forms (exact daily-index strings). Ownership forms
# (3/4/5, SCHEDULE 13D/13G, 13F-HR) are handled by ingest_ownership.py (M5).
FILING_FORMS = {
    "8-K", "8-K/A",
    "6-K", "6-K/A",            # foreign private issuers' current report (ADRs)
    "10-K", "10-K/A",
    "10-Q", "10-Q/A",
    "20-F", "20-F/A",          # foreign annual report (ADRs)
    "S-1", "S-1/A",
    # 424B: keep IPO / secondary-offering prospectuses, but DROP 424B2 / 424B3 +
    # FWP — they are ~99% bank structured-note shelf takedowns (one bank filed
    # 2,300+ in a single week), pure noise for a catalyst scanner. Re-add here if
    # you ever want full offering breadth.
    "424B1", "424B4", "424B5",
    "DEF 14A", "DEFA14A",      # proxy / additional proxy soliciting material
}

# Human-readable 8-K item descriptions (the signal in a current report).
EIGHTK_ITEM_DESC = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.04": "Triggering Events Accelerating a Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting / Failure to Satisfy a Listing Rule",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure / Election of Directors or Officers",
    "5.03": "Amendments to Articles of Incorporation or Bylaws",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}


def _et() -> ZoneInfo:
    return ZoneInfo(load_settings().get("timezone", "America/New_York"))


def _now() -> datetime:
    return datetime.now(_et())


def _dedupe_hash(accession: str) -> str:
    """An accession number is globally unique per filing — the natural dedupe key."""
    return hashlib.sha1(accession.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 1. Daily index (discovery)
# --------------------------------------------------------------------------- #
def _iter_dates(since: datetime, until: datetime) -> Iterable[date]:
    d, last = since.date(), until.date()
    while d <= last:
        yield d
        d += timedelta(days=1)


def fetch_daily_index(session: PoliteSession, d: date) -> list[dict[str, Any]]:
    """Return parsed rows for one filing day, or [] if no index (weekend/holiday)."""
    import requests

    base = load_sources().get("edgar", {}).get(
        "daily_index_base", "https://www.sec.gov/Archives/edgar/daily-index")
    q = (d.month - 1) // 3 + 1
    url = f"{base}/{d.year}/QTR{q}/master.{d.strftime('%Y%m%d')}.idx"
    try:
        text = session.edgar_get(url, timeout=45).text
    except requests.HTTPError as exc:
        code = getattr(exc.response, "status_code", None)
        if code not in (403, 404):
            log.warning("daily index %s -> %s", url, exc)
        return []   # 403/404 = non-trading day (SEC's CDN 403s missing files): no filings
    return _parse_idx(text)


def _parse_idx(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    data = False
    for ln in text.splitlines():
        if ln.startswith("CIK|"):
            data = True
            continue
        if not data or ln.count("|") < 4:
            continue
        cik_s, company, form, filed, filename = ln.split("|", 4)
        if not cik_s.isdigit():
            continue
        accession = filename.rsplit("/", 1)[-1].replace(".txt", "")
        rows.append({
            "cik": int(cik_s), "company": company.strip(), "form": form.strip(),
            "date": filed.strip(), "accession": accession,
        })
    return rows


# --------------------------------------------------------------------------- #
# 2. Submissions API (enrichment): items + acceptance time + primary doc
# --------------------------------------------------------------------------- #
def _fetch_submissions(session: PoliteSession, cik10: str) -> dict[str, dict[str, Any]]:
    """Return {accession_with_dashes: detail} from a company's recent filings."""
    import requests

    url = (load_sources().get("edgar", {})
           .get("submissions_api", "https://data.sec.gov/submissions/CIK{cik}.json")
           ).format(cik=cik10)
    try:
        recent = session.edgar_get(url, timeout=45).json().get("filings", {}).get("recent", {})
    except (requests.HTTPError, ValueError) as exc:
        log.warning("submissions %s -> %s", cik10, exc)
        return {}
    out: dict[str, dict[str, Any]] = {}
    accs = recent.get("accessionNumber", [])
    for i, acc in enumerate(accs):
        out[acc] = {
            "items": recent.get("items", [""] * len(accs))[i],
            "acceptanceDateTime": recent.get("acceptanceDateTime", [""] * len(accs))[i],
            "primaryDocument": recent.get("primaryDocument", [""] * len(accs))[i],
            "primaryDocDescription": recent.get("primaryDocDescription", [""] * len(accs))[i],
            "reportDate": recent.get("reportDate", [""] * len(accs))[i],
        }
    return out


# --------------------------------------------------------------------------- #
# 3. Normalise
# --------------------------------------------------------------------------- #
def _to_et_iso(acceptance: str, fallback_date: str) -> str:
    """SEC acceptanceDateTime is UTC; convert to ET. Fall back to the filed date."""
    if acceptance:
        try:
            return dtparser.parse(acceptance).astimezone(_et()).isoformat()
        except (ValueError, TypeError):
            pass
    try:
        return datetime.strptime(fallback_date, "%Y%m%d").replace(tzinfo=_et()).isoformat()
    except ValueError:
        return fallback_date


def _item_codes(items_str: str) -> list[str]:
    return [c.strip() for c in (items_str or "").split(",") if c.strip()]


def _headline(form: str, codes: list[str], doc_desc: str, company: str) -> str:
    # ASCII-clean separators (no em-dash) so stored headlines stay portable on Windows.
    if form.startswith("8-K") and codes:
        labels = [f"Item {c} {EIGHTK_ITEM_DESC.get(c, '')}".strip() for c in codes]
        return "8-K Items: " + "; ".join(labels)
    if doc_desc:
        return f"{form}: {doc_desc}"
    return f"{form}: {company}"


def _normalize(match: dict[str, Any], detail: dict[str, Any], meta: dict[str, Any] | None,
               archives_base: str) -> dict[str, Any]:
    cik_int = match["cik"]
    acc = match["accession"]
    accn = acc.replace("-", "")
    codes = _item_codes(detail.get("items", "")) if match["form"].startswith("8-K") else []
    primary = detail.get("primaryDocument") or ""
    if primary:
        filing_url = f"{archives_base}/{cik_int}/{accn}/{primary}"
    else:
        filing_url = f"{archives_base}/{cik_int}/{accn}/{acc}-index.htm"
    company = (meta or {}).get("name") or match["company"]
    return {
        "cik": str(cik_int).zfill(10),
        "ticker": (meta or {}).get("ticker", ""),
        "company": company,
        "form_type": match["form"],
        "item_codes": codes,
        "headline": _headline(match["form"], codes, detail.get("primaryDocDescription", ""), company),
        "body_text": "",
        "filing_url": filing_url,
        "accession": acc,
        "filed_at": _to_et_iso(detail.get("acceptanceDateTime", ""), match["date"]),
        "report_date": detail.get("reportDate", ""),
        "dedupe_hash": _dedupe_hash(acc),
        "source": "SEC EDGAR",
    }


# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #
def ingest(session: PoliteSession | None = None,
           since: datetime | None = None,
           until: datetime | None = None,
           forms: set[str] | None = None) -> list[dict[str, Any]]:
    """Fetch catalyst filings for the universe within [since, until].

    Discovery via the daily index (one call per calendar day in the window);
    enrichment via the submissions API (one call per CIK that actually filed a
    catalyst form). Per-day and per-CIK failures are isolated and logged.
    """
    session = session or PoliteSession()
    forms = forms or FILING_FORMS
    lookback = int(load_settings().get("lookback_hours", 24))
    since = since or (_now() - timedelta(hours=lookback))
    until = until or _now()

    universe = load_map()
    by_cik = {int(c["cik"]): c for c in universe}
    archives_base = load_sources().get("edgar", {}).get(
        "archives_base", "https://www.sec.gov/Archives/edgar/data")

    # 1. Discover matched filings from the daily index.
    matches: list[dict[str, Any]] = []
    days = 0
    for d in _iter_dates(since, until):
        rows = fetch_daily_index(session, d)
        if rows:
            days += 1
        for r in rows:
            if r["form"] in forms and r["cik"] in by_cik:
                matches.append(r)

    # 2. Enrich per active CIK (one submissions call each, fetched concurrently
    #    under the shared rate gate), then normalise.
    unique_ciks = list({m["cik"] for m in matches})
    subs_cache: dict[int, dict[str, dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        for cik, subs in pool.map(lambda c: (c, _fetch_submissions(session, str(c).zfill(10))), unique_ciks):
            subs_cache[cik] = subs
    out = [_normalize(m, subs_cache.get(m["cik"], {}).get(m["accession"], {}),
                      by_cik.get(m["cik"]), archives_base) for m in matches]

    log.info("EDGAR ingest: %d filings from %d active CIKs over %d index-days [%s..%s]",
             len(out), len(subs_cache), days, since.date(), until.date())
    return out


def fetch_company(session: PoliteSession, cik10: str, since: datetime | None = None,
                  forms: set[str] | None = None) -> list[dict[str, Any]]:
    """Targeted fresh pull for ONE company via the submissions API (for `ask --fetch`).

    Per-company is fine here — it's a single CIK on demand, not the whole universe.
    """
    forms = forms or FILING_FORMS
    meta = {int(c["cik"]): c for c in load_map()}.get(int(cik10))
    archives = load_sources().get("edgar", {}).get("archives_base", "https://www.sec.gov/Archives/edgar/data")
    url = load_sources().get("edgar", {}).get(
        "submissions_api", "https://data.sec.gov/submissions/CIK{cik}.json").format(cik=cik10)
    try:
        recent = session.edgar_get(url, timeout=45).json().get("filings", {}).get("recent", {})
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_company %s -> %s", cik10, exc)
        return []
    accs = recent.get("accessionNumber", [])
    since_d = since.date().isoformat() if since else None
    out: list[dict[str, Any]] = []
    for i, acc in enumerate(accs):
        form = (recent.get("form") or [None] * len(accs))[i]
        if form not in forms:
            continue
        fdate = (recent.get("filingDate") or [""] * len(accs))[i]
        if since_d and fdate and fdate < since_d:
            continue
        detail = {k: (recent.get(k) or [""] * len(accs))[i]
                  for k in ("items", "acceptanceDateTime", "primaryDocument", "primaryDocDescription", "reportDate")}
        match = {"cik": int(cik10), "accession": acc, "form": form,
                 "date": (fdate or "").replace("-", ""), "company": (meta or {}).get("name", "")}
        out.append(_normalize(match, detail, meta, archives))
    return out
