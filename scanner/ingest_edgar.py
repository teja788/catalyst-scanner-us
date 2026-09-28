"""SEC EDGAR filings ingester (Milestone 3).

Strategy (Section 5 of the build spec): rather than poll each company, pull the
EDGAR DAILY INDEX (one file per filing day listing ALL filings), filter it to our
universe's CIKs + catalyst FORM types, then enrich each NEW match from its SGML
header file ({accession}-index-headers.html) — which carries the 8-K item codes,
the acceptance timestamp (in ET) and the primary-document name.

Why the header and not the submissions API: for fresh filings the submissions
API's `acceptanceDateTime` is ET mislabelled "Z" (verified live 2026-09: API
"11:30:40Z" == SGML "113040" ET) and is corrected to true UTC only later, so
converting it put 8-Ks 4-5h early. The header is ET, always.

The current day's daily index does not exist until that night (403). For today /
yesterday EDGAR full-text search (EFTS) stands in (best-effort); those
days are NOT final, so the catch-up cursor never moves past them.

Cost is bounded by activity (one small header per NEW filing), not universe size.
Ownership forms (3/4/5, SCHEDULE 13D/13G, 13F-HR) are handled by ingest_ownership.
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from scanner import store
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


# Per-process daily-index cache: the EDGAR and ownership ingesters sweep the SAME
# index days in one refresh — fetch each day once. PAST days only: the current
# day's index grows intraday, so caching it in a long-lived process (dashboard)
# would serve stale snapshots. Bounded so it can't grow unboundedly.
_IDX_CACHE: dict[date, list[dict[str, Any]]] = {}
_IDX_CACHE_MAX = 40


def fetch_daily_index(session: PoliteSession, d: date) -> list[dict[str, Any]]:
    """Return parsed rows for one filing day, or [] if no index (weekend/holiday/
    not yet published — the caller decides which via rows_for_day).

    Failure discipline (the silent-data-loss guard): a MISSING index is a 403/404
    whose body is S3's "AccessDenied" XML (verified live 2026-07 — SEC serves 403,
    not 404, for absent .idx files). ANY other failure — a fair-access block
    ("Undeclared Automated Tool" HTML, also a 403), an exhausted 429/5xx retry, a
    network error — RAISES, so the refresh marks the source failed and the
    catch-up cursor does NOT advance past a day that was never actually fetched.
    """
    import requests

    if d in _IDX_CACHE:
        return _IDX_CACHE[d]
    base = load_sources().get("edgar", {}).get(
        "daily_index_base", "https://www.sec.gov/Archives/edgar/daily-index")
    q = (d.month - 1) // 3 + 1
    url = f"{base}/{d.year}/QTR{q}/master.{d.strftime('%Y%m%d')}.idx"
    try:
        text = session.edgar_get(url, timeout=45).text
    except requests.HTTPError as exc:
        resp = getattr(exc, "response", None)
        code = getattr(resp, "status_code", None)
        body = ""
        if resp is not None:
            try:
                body = resp.text[:1000]
            except Exception:  # noqa: BLE001 - body only informs classification
                body = ""
        if code == 404 or (code == 403 and "AccessDenied" in body):
            rows: list[dict[str, Any]] = []   # no index (yet)
        else:
            # 403 block page / exhausted 429/5xx: NOT a quiet day — fail the run.
            log.warning("daily index %s -> %s (treating as fetch failure, not a holiday)", url, exc)
            raise
    else:
        rows = _parse_idx(text)
    # Never cache today's or yesterday's miss: that index may still be published.
    if d < _now().date() and (rows or d < _now().date() - timedelta(days=1)):
        if len(_IDX_CACHE) >= _IDX_CACHE_MAX:
            _IDX_CACHE.pop(next(iter(_IDX_CACHE)))   # evict oldest, not everything
        _IDX_CACHE[d] = rows
    return rows


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


_EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
# One ROOT form per query (a comma list silently under-returns; each root also
# returns its /A amendments). Covers FILING_FORMS + the ownership forms + 13F-HR.
_EFTS_FORMS = ("8-K", "6-K", "10-K", "10-Q", "20-F", "S-1", "424B1", "424B4", "424B5",
               "DEF 14A", "DEFA14A", "4", "SCHEDULE 13D", "SCHEDULE 13G", "13F-HR")
_EFTS_TTL_SEC = 600              # EDGAR + ownership ingest reuse one sweep per refresh
_CUR_CACHE: dict[date, tuple[float, list[dict[str, Any]]]] = {}


def efts_search(session: PoliteSession, **params: Any) -> list[dict[str, Any]]:
    """All `_source` hits of an EDGAR full-text-search query (100 per page; the
    service caps from+size at 10,000). Raises when a page has no `hits` object."""
    out: list[dict[str, Any]] = []
    for start in range(0, 10_000, 100):
        js = session.edgar_get(_EFTS_URL, timeout=30, params={**params, "from": start}).json()
        if "hits" not in js:
            raise RuntimeError(f"EFTS error for {params}: {str(js)[:200]}")
        page = [h["_source"] for h in js["hits"].get("hits", [])]
        out += page
        if len(page) < 100:
            break
    return out


def efts_rows(src: dict[str, Any]) -> list[dict[str, Any]]:
    """One EFTS hit -> daily-index-shaped rows, one per associated CIK (subject AND
    filer, exactly like the daily index lists them)."""
    rows = []
    for name in src.get("display_names") or []:
        m = re.search(r"^(.*?)\s*(?:\([^)]*\)\s*)*\(CIK (\d{10})\)\s*$", name)
        if m:
            rows.append({"cik": int(m.group(2)), "company": m.group(1).strip(), "form": src.get("form", ""),
                         "date": (src.get("file_date") or "").replace("-", ""), "accession": src.get("adsh", "")})
    return rows


def fetch_current_rows(session: PoliteSession, d: date) -> list[dict[str, Any]]:
    """Rows filed on `d` from EDGAR full-text search (EFTS) — indexed within minutes,
    ~0.5-4s per call (the getcurrent Atom feed it replaced took ~8 min per sweep).

    Best-effort: a failing form query is logged and skipped. The nightly index is
    authoritative and the cursor never passes a day read here.
    """
    hit = _CUR_CACHE.get(d)
    if hit and time.monotonic() - hit[0] < _EFTS_TTL_SEC:
        return hit[1]
    day = d.isoformat()
    rows: dict[tuple[str, int], dict[str, Any]] = {}
    for form in _EFTS_FORMS:
        try:
            hits = efts_search(session, forms=form, dateRange="custom", startdt=day, enddt=day)
        except Exception as exc:  # noqa: BLE001 - best-effort; the nightly index backfills
            log.warning("EFTS %s %s -> %s (same-day coverage partial)", form, day, exc)
            continue
        for src in hits:                      # hits are per DOCUMENT — dedupe below
            for r in efts_rows(src):
                rows[(r["accession"], r["cik"])] = r
    out = list(rows.values())
    _CUR_CACHE[d] = (time.monotonic(), out)
    return out


def rows_for_day(session: PoliteSession, d: date) -> tuple[list[dict[str, Any]], bool]:
    """(rows, final). final=True for a published index or a past non-trading day;
    final=False for today/yesterday read from EFTS (index not out yet)."""
    rows = fetch_daily_index(session, d)
    if rows or d.weekday() >= 5 or d < _now().date() - timedelta(days=1):
        return rows, True
    return fetch_current_rows(session, d), False


# --------------------------------------------------------------------------- #
# 2. SGML header (enrichment): items + acceptance time (ET) + primary doc
# --------------------------------------------------------------------------- #
def acceptance_iso(text: str, fallback_date: str) -> str:
    """<ACCEPTANCE-DATETIME>YYYYMMDDHHMMSS (naive ET) from an SGML header, as ISO ET."""
    m = re.search(r"<ACCEPTANCE-DATETIME>(\d{14})", text)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=_et()).isoformat()
        except ValueError:
            pass
    try:
        return datetime.strptime(fallback_date, "%Y%m%d").replace(tzinfo=_et()).isoformat()
    except ValueError:
        # Never store a raw non-ISO string: "20260615" sorts AFTER every ISO
        # timestamp lexicographically, so such a row would appear in EVERY window.
        log.warning("unparseable filing date %r — storing empty filed_at", fallback_date)
        return ""


def parse_header(text: str) -> dict[str, Any] | None:
    """Parse an (unescaped) SGML header -> {accepted, items, primary, desc, period}."""
    if "<ACCEPTANCE-DATETIME>" not in text:
        return None
    first_doc = text.split("<DOCUMENT>", 1)[1].split("</DOCUMENT>", 1)[0] if "<DOCUMENT>" in text else ""
    fn = re.search(r"<FILENAME>([^\s<]+)", first_doc)
    desc = re.search(r"<DESCRIPTION>([^\n<]*)", first_doc)
    period = re.search(r"<PERIOD>(\d{8})", text)
    p = period.group(1) if period else ""
    return {
        "accepted": text,
        "items": re.findall(r"<ITEMS>\s*([\d.]+)", text),
        "primary": fn.group(1) if fn else "",
        "desc": desc.group(1).strip() if desc else "",
        "period": f"{p[:4]}-{p[4:6]}-{p[6:]}" if p else "",
    }


def _fetch_header(session: PoliteSession, cik_int: int, acc: str,
                  archives_base: str) -> dict[str, Any] | None:
    """Fetch + parse {acc}-index-headers.html; None on any failure (caller counts it)."""
    url = f"{archives_base}/{cik_int}/{acc.replace('-', '')}/{acc}-index-headers.html"
    try:
        hdr = parse_header(html.unescape(session.edgar_get(url, timeout=30).text))
    except Exception as exc:  # noqa: BLE001 - counted as a failure by the caller
        log.warning("filing header %s -> %s", acc, exc)
        return None
    if hdr is None:
        log.warning("filing header %s has no acceptance time", acc)
    return hdr


# --------------------------------------------------------------------------- #
# 3. Normalise
# --------------------------------------------------------------------------- #
def _headline(form: str, codes: list[str], doc_desc: str, company: str) -> str:
    # ASCII-clean separators (no em-dash) so stored headlines stay portable on Windows.
    if form.startswith("8-K") and codes:
        labels = [f"Item {c} {EIGHTK_ITEM_DESC.get(c, '')}".strip() for c in codes]
        return "8-K Items: " + "; ".join(labels)
    if doc_desc:
        return f"{form}: {doc_desc}"
    return f"{form}: {company}"


def _normalize(match: dict[str, Any], hdr: dict[str, Any], meta: dict[str, Any] | None,
               archives_base: str) -> dict[str, Any]:
    cik_int = match["cik"]
    acc = match["accession"]
    accn = acc.replace("-", "")
    codes = hdr["items"] if match["form"].startswith("8-K") else []
    if hdr["primary"]:
        filing_url = f"{archives_base}/{cik_int}/{accn}/{hdr['primary']}"
    else:
        filing_url = f"{archives_base}/{cik_int}/{accn}/{acc}-index.htm"
    company = (meta or {}).get("name") or match["company"]
    return {
        "cik": str(cik_int).zfill(10),
        "ticker": (meta or {}).get("ticker", ""),
        "company": company,
        "form_type": match["form"],
        "item_codes": codes,
        "headline": _headline(match["form"], codes, hdr["desc"], company),
        "body_text": "",
        "filing_url": filing_url,
        "accession": acc,
        "filed_at": acceptance_iso(hdr["accepted"], match["date"]),
        "report_date": hdr["period"],
        "dedupe_hash": _dedupe_hash(acc),
        "source": "SEC EDGAR",
    }


def _enrich(session: PoliteSession, matches: list[dict[str, Any]], by_cik: dict[int, dict[str, Any]],
            archives_base: str) -> tuple[list[dict[str, Any]], int]:
    """Header-enrich the matches NOT already stored (one fetch each, concurrent under
    the shared rate gate). Returns (records, failed). A failed header is SKIPPED, never
    stored bare: INSERT OR IGNORE would freeze a bare row forever."""
    uniq = {m["accession"]: m for m in matches}           # index lists filer + subject rows
    known = store.existing_hashes("filings", [_dedupe_hash(a) for a in uniq])
    todo = [m for a, m in uniq.items() if _dedupe_hash(a) not in known]
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        hdrs = list(pool.map(lambda m: _fetch_header(session, m["cik"], m["accession"], archives_base), todo))
    out = [_normalize(m, h, by_cik.get(m["cik"]), archives_base) for m, h in zip(todo, hdrs) if h]
    return out, len(todo) - len(out)


# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #
def ingest(session: PoliteSession | None = None,
           since: datetime | None = None,
           until: datetime | None = None,
           forms: set[str] | None = None,
           stats: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Fetch NEW catalyst filings for the universe within [since, until].

    `stats` (optional) is filled with {"last_index_day": latest day read from a
    FINAL index (or None), "failed": filings whose header fetch failed}. The
    caller uses both to decide how far the catch-up cursor may move.
    """
    session = session or PoliteSession()
    forms = forms or FILING_FORMS
    lookback = int(load_settings().get("lookback_hours", 24))
    since = since or (_now() - timedelta(hours=lookback))
    until = until or _now()

    by_cik = {int(c["cik"]): c for c in load_map()}
    archives_base = load_sources().get("edgar", {}).get(
        "archives_base", "https://www.sec.gov/Archives/edgar/data")

    matches: list[dict[str, Any]] = []
    last_final = None
    for d in _iter_dates(since, until):
        rows, final = rows_for_day(session, d)
        if final and rows:
            last_final = d
        matches += [r for r in rows if r["form"] in forms and r["cik"] in by_cik]

    out, failed = _enrich(session, matches, by_cik, archives_base)
    if stats is not None:
        stats.update(last_index_day=last_final, failed=failed)
    log.info("EDGAR ingest: %d new filings (%d header failures), final index through %s [%s..%s]",
             len(out), failed, last_final, since.date(), until.date())
    return out


def fetch_company(session: PoliteSession, cik10: str, since: datetime | None = None,
                  forms: set[str] | None = None) -> list[dict[str, Any]]:
    """Targeted fresh pull for ONE company (for `ask --fetch`).

    The submissions API only DISCOVERS the accessions here; times/items come from
    the SGML header like the main ingest (see the module docstring for why).
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
    matches: list[dict[str, Any]] = []
    for i, acc in enumerate(accs):
        form = (recent.get("form") or [None] * len(accs))[i]
        fdate = (recent.get("filingDate") or [""] * len(accs))[i]
        if form in forms and not (since_d and fdate and fdate < since_d):
            matches.append({"cik": int(cik10), "accession": acc, "form": form,
                            "date": (fdate or "").replace("-", ""),
                            "company": (meta or {}).get("name", "")})
    return _enrich(session, matches, {int(cik10): meta} if meta else {}, archives)[0]
