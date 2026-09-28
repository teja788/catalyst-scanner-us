"""Non-EDGAR catalyst feeds (the broad asymmetric-opportunity layer).

Free sources, each fetched for a recent window, matched to a universe company
by org name, and stored as `external_catalysts` rows (dedupe-safe):

  - USAspending.gov      -> federal CONTRACT awards (recipient -> universe)
  - openFDA + ClinicalTrials.gov -> drug APPROVALS / Phase-3 readouts (sponsor -> universe)
  - FDA Novel Drug Approvals page (same-day) + pdufa.bio forward PDUFA/AdComm calendar
  - USPTO PatentsView     -> PATENT grants (assignee -> universe)   [needs a free key]

The org-name matcher is conservative: normalise (strip corporate suffixes/punct) then
require an exact or leading-token hit against the universe name/alias index, so we only
keep confident public-company matches and drop subsidiaries/generic names.

Failure discipline: each feed raises on failure; `ingest` isolates them, stores what
the others returned, then raises so the run is marked failed rather than a quiet "ok".
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests

from scanner import store
from scanner.config import load_settings
from scanner.http import PoliteSession
from scanner.universe import load_map

log = logging.getLogger(__name__)

_TZ = "America/New_York"
_FDA_MAX = 1000   # ponytail: 10 pages; a 30-day window had 177 (2026-09). Raise if it ever bites.


def _ua() -> str:
    return (load_settings().get("edgar", {}) or {}).get("user_agent") or "catalyst-scanner-us"


def _today() -> datetime:
    return datetime.now(ZoneInfo(_TZ))


def _hash(*parts: Any) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Universe org-name matching
# --------------------------------------------------------------------------- #
_SUFFIXES = re.compile(
    r"\b(corporation|corp|incorporated|inc|company|co|llc|l\.?l\.?c|lp|l\.?p|ltd|"
    r"limited|plc|holdings|holding|group|industries|technologies|technology|the)\b", re.I)


def _norm_org(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(r"[^a-z0-9 &]", " ", s)
    s = _SUFFIXES.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def build_name_index(universe: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """normalized org name / alias -> {cik, ticker, company}."""
    universe = universe or load_map()
    idx: dict[str, dict[str, Any]] = {}
    for c in universe:
        entry = {"cik": c["cik"], "ticker": c.get("ticker"), "company": c.get("name")}
        for nm in [c.get("name", "")] + (c.get("aliases") or []):
            k = _norm_org(nm)
            if len(k) >= 5 and k not in idx:   # >=5 chars avoids 1-2 letter collisions
                idx[k] = entry
    return idx


def match_org(name: str, idx: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    k = _norm_org(name)
    if len(k) < 5:
        return None
    if k in idx:
        return idx[k]
    toks = k.split()                  # external name may carry extra tokens
    for n in (3, 2):
        if len(toks) > n:
            kk = " ".join(toks[:n])
            if len(kk) >= 6 and kk in idx:
                return idx[kk]
    return None


def _base(m: dict[str, Any]) -> dict[str, Any]:
    return {"cik": m["cik"], "ticker": m.get("ticker"), "company": m.get("company")}


# --------------------------------------------------------------------------- #
# Feed 1 — federal contract awards (USAspending.gov, no key)
# --------------------------------------------------------------------------- #
def fetch_contracts(session: PoliteSession, start: str, end: str,
                    idx: dict[str, dict[str, Any]], pages: int = 5,
                    page_size: int = 100) -> list[dict[str, Any]]:
    # date_type new_awards_only is essential: the default matches awards with ANY
    # action in the window, and "Award Amount" is cumulative-to-date — verified live
    # to surface a 1993 Lockheed DOE contract ($48B) on a routine modification.
    #
    # TWO sort passes, paginated: a single top-100-by-amount page structurally
    # excludes the asymmetric sweet spot — a $40M award that is huge for a $300M-cap
    # company never outranks the Lockheed-class mega-primes. The by-amount pass
    # keeps the mega awards; the by-recency pass reaches the smaller new awards.
    base_filters = {"award_type_codes": ["A", "B", "C", "D"],
                    "time_period": [{"start_date": start, "end_date": end,
                                     "date_type": "new_awards_only"}]}
    fields = ["Award ID", "Recipient Name", "Award Amount", "Awarding Agency", "Description", "Start Date",
              "Base Obligation Date"]
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for sort_field in ("Award Amount", "Start Date"):
        for page in range(1, pages + 1):
            payload = {"filters": base_filters, "fields": fields,
                       "limit": page_size, "page": page, "sort": sort_field, "order": "desc"}
            try:
                r = session.post("https://api.usaspending.gov/api/v2/search/spending_by_award/",
                                 json=payload, timeout=45,
                                 headers={"User-Agent": _ua(), "Accept": "application/json"})
                js = r.json()
            except Exception as exc:  # noqa: BLE001
                if page == 1:
                    raise              # nothing from this pass = the feed failed; say so
                log.warning("USAspending fetch failed (sort=%s page %d): %s — partial", sort_field, page, exc)
                break
            results = js.get("results", [])
            for a in results:
                key = str(a.get("generated_internal_id") or a.get("Award ID") or "")
                if not key or key in seen_ids:
                    continue
                seen_ids.add(key)
                m = match_org(a.get("Recipient Name", ""), idx)
                if not m:
                    continue
                amt = a.get("Award Amount")
                # the award PAGE needs the generated id (snake_case key, returned
                # automatically) — a bare PIID ("Award ID") does not resolve.
                aid = a.get("generated_internal_id") or a.get("Award ID") or ""
                agency = a.get("Awarding Agency") or ""
                pop = a.get("Start Date") or ""
                out.append({**_base(m), "source": "USAspending", "category": "contract",
                            "headline": (f"Federal contract ${amt:,.0f} — {agency}" if amt else f"Federal contract — {agency}"),
                            "detail": ((a.get("Description") or "")[:280] + (f" (PoP start {pop})" if pop else "")), "amount": amt,
                            "url": f"https://www.usaspending.gov/award/{aid}",
                            # the SIGNING date: "Start Date" is the period of performance and
                            # can be a year ahead (2027 PoPs sorted to the top of the pack)
                            "event_date": a.get("Base Obligation Date") or pop or end,
                            "dedupe_hash": _hash("usasp", a.get("Award ID") or aid, m["cik"])})
            if len(results) < page_size or not (js.get("page_metadata") or {}).get("hasNext"):
                break
        else:
            log.info("USAspending: %s pass exhausted %d pages — deeper awards in window not sampled",
                     sort_field, pages)
    return out


# --------------------------------------------------------------------------- #
# Feed 2 — drug approvals (openFDA) + Phase-3 readouts (ClinicalTrials.gov v2)
# --------------------------------------------------------------------------- #
def fetch_fda(session: PoliteSession, start: str, end: str,
              idx: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """openFDA drug approvals in the window. PAGED: a single 100-row page silently
    dropped ~40% of a 30-day window (177 applications, verified live 2026-09)."""
    hdr = {"User-Agent": _ua(), "Accept": "application/json"}
    s_compact, e_compact = start.replace("-", ""), end.replace("-", "")
    apps: list[dict[str, Any]] = []
    while len(apps) < _FDA_MAX:
        try:
            js = session.get("https://api.fda.gov/drug/drugsfda.json", headers=hdr, timeout=30, params={
                "search": f"submissions.submission_status_date:[{s_compact} TO {e_compact}] "
                          f"AND submissions.submission_status:AP",
                "limit": 100, "skip": len(apps)}).json()
        except requests.HTTPError as exc:
            if getattr(exc.response, "status_code", None) == 404:   # openFDA: no (more) results
                break
            raise
        page = js.get("results", [])
        apps += page
        if not page or len(apps) >= ((js.get("meta") or {}).get("results") or {}).get("total", 0):
            break

    out: list[dict[str, Any]] = []
    for app in apps:
        m = match_org(app.get("sponsor_name", ""), idx)
        if not m:
            continue
        # The search matches the APPLICATION if ANY submission matches, so re-check
        # for the approval that actually falls in the window; label ORIG vs supplement.
        subs = [s for s in (app.get("submissions") or [])
                if s.get("submission_status") == "AP"
                and s_compact <= (s.get("submission_status_date") or "") <= e_compact]
        if not subs:
            continue
        sub = max(subs, key=lambda s: s.get("submission_status_date") or "")
        stype = sub.get("submission_type") or ""
        sdate = sub.get("submission_status_date") or ""
        event = f"{sdate[:4]}-{sdate[4:6]}-{sdate[6:8]}" if len(sdate) == 8 else end
        prods = app.get("products") or [{}]
        brand = (prods[0].get("brand_name") or prods[0].get("generic_name") or "drug")
        appno = app.get("application_number", "")
        kind = "FDA approval" if stype == "ORIG" else "FDA supplemental approval"
        # the Drugs@FDA page wants the numeric ApplNo ("NDA220837" -> "220837")
        out.append({**_base(m), "source": "openFDA", "category": "fda",
                    "headline": f"{kind}: {brand}",
                    "detail": f"{app.get('sponsor_name','')} — {appno} · {stype} "
                              f"{(sub.get('submission_class_code_description') or '').strip()}".strip(),
                    "amount": None,
                    "url": f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={re.sub(r'[^0-9]', '', appno)}",
                    "event_date": event, "dedupe_hash": _hash("openfda", appno, sdate, m["cik"])})
    return out


def fetch_trials(session: PoliteSession, start: str, end: str,
                 idx: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """ClinicalTrials.gov v2 — Phase-3 studies whose RESULTS were posted in the
    window. Filter SERVER-SIDE on ResultsFirstPostDate (verified live 2026-07):
    the old client-side filter over the top-100 most-recently-UPDATED studies
    silently missed any readout not among the 100 latest record edits."""
    hdr = {"User-Agent": _ua(), "Accept": "application/json"}
    out: list[dict[str, Any]] = []
    params: dict[str, Any] = {
        "filter.advanced": f"AREA[Phase]PHASE3 AND AREA[ResultsFirstPostDate]RANGE[{start},{end}]",
        "aggFilters": "results:with", "pageSize": 100}
    for _page in range(5):   # up to 500 readouts per window — far above reality
        js = session.get("https://clinicaltrials.gov/api/v2/studies", headers=hdr,
                         timeout=30, params=params).json()
        for st in js.get("studies", []):
            ps = st.get("protocolSection", {})
            spon = (((ps.get("sponsorCollaboratorsModule") or {}).get("leadSponsor") or {}).get("name") or "")
            m = match_org(spon, idx)
            if not m:
                continue
            ident = ps.get("identificationModule") or {}
            status = ps.get("statusModule") or {}
            posted = ((status.get("resultsFirstPostDateStruct") or {}).get("date") or "")
            if not posted or posted < start or posted > end:   # belt-and-braces
                continue
            nct = ident.get("nctId", "")
            out.append({**_base(m), "source": "ClinicalTrials", "category": "fda",
                        "headline": f"Phase 3 results posted: {(ident.get('briefTitle') or '')[:90]}",
                        "detail": f"{spon} — {nct}", "amount": None,
                        "url": f"https://clinicaltrials.gov/study/{nct}",
                        "event_date": posted, "dedupe_hash": _hash("ctgov", nct, m["cik"])})
        token = js.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
    return out


_NOVEL_URL = "https://www.fda.gov/drugs/novel-drug-approvals-fda/novel-drug-approvals-{year}"


def infer_sponsor(session: PoliteSession, ingredient: str, universe: dict[str, dict[str, Any]]
                  ) -> tuple[dict[str, Any], int] | None:
    """(universe company, n) for the universe CIK that mentions `ingredient` most in
    the last year of SEC filings (EDGAR full-text search), needing >= 2 mentions."""
    today = _today().date()
    js = session.edgar_get("https://efts.sec.gov/LATEST/search-index", timeout=30, params={
        "q": f'"{ingredient}"', "dateRange": "custom",
        "startdt": (today - timedelta(days=365)).isoformat(), "enddt": today.isoformat()}).json()
    counts: dict[str, int] = {}
    for h in (js.get("hits") or {}).get("hits", []):
        for c in h["_source"].get("ciks") or []:
            if c in universe:
                counts[c] = counts.get(c, 0) + 1
    if not counts:
        return None
    cik, n = max(counts.items(), key=lambda kv: kv[1])
    return (universe[cik], n) if n >= 2 else None


def fetch_fda_novel(session: PoliteSession, start: str, end: str,
                    idx: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """FDA's Novel Drug Approvals table — current to the day (openFDA lags ~4 days).
    The table has no sponsor column, so the sponsor is INFERRED from SEC filings that
    mention the active ingredient (labelled as inferred). FDA serves 404 to browser
    and default UAs; the contact UA works (verified 2026-09)."""
    from bs4 import BeautifulSoup

    universe = {c["cik"]: c for c in load_map()}
    out: list[dict[str, Any]] = []
    for year in sorted({start[:4], end[:4]}):
        html = session.get(_NOVEL_URL.format(year=year), timeout=30, headers={"User-Agent": _ua()}).text
        table = BeautifulSoup(html, "lxml").find("table")
        for tr in (table.find_all("tr")[1:] if table else []):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) < 5:
                continue
            _no, drug, ingredient, approved, use = cells[:5]
            try:
                day = datetime.strptime(approved, "%m/%d/%Y").date().isoformat()
            except ValueError:
                continue
            if not (start <= day <= end):
                continue
            hit = infer_sponsor(session, ingredient, universe)
            if not hit:
                continue
            meta, n = hit
            out.append({"cik": meta["cik"], "ticker": meta.get("ticker"), "company": meta.get("name"),
                        "source": "FDA novel approvals", "category": "fda",
                        "headline": f"FDA NOVEL drug approval: {drug} ({ingredient})",
                        "detail": f"{use[:160]} — sponsor INFERRED: most SEC-filing mentions of "
                                  f"'{ingredient}' in the last year ({n})",
                        "amount": None, "url": _NOVEL_URL.format(year=year),
                        "event_date": day, "dedupe_hash": _hash("fdanovel", drug, day)})
    return out


def fetch_pdufa(session: PoliteSession, start: str, end: str,
                idx: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Scheduled FDA decision (PDUFA) and advisory-committee dates from pdufa.bio —
    the FORWARD calendar (window start .. +60 days). Third-party data: its licence
    requires attribution + link-back, so every row links to pdufa.bio."""
    by_ticker = {c.get("ticker", "").upper(): c for c in load_map()}
    horizon = (_today() + timedelta(days=60)).date().isoformat()
    js = session.get("https://www.pdufa.bio/api/v1/events", timeout=30,
                     headers={"User-Agent": _ua(), "Accept": "application/json"}).json()
    out: list[dict[str, Any]] = []
    for e in js.get("data") or []:
        day = e.get("date") or ""
        meta = by_ticker.get((e.get("ticker") or "").upper())
        if (e.get("type") not in ("PDUFA", "AdComm") or e.get("date_precision") != "day"
                or not (start <= day <= horizon) or not meta):
            continue
        out.append({"cik": meta["cik"], "ticker": meta.get("ticker"), "company": meta.get("name"),
                    "source": "pdufa.bio", "category": "pdufa",
                    "headline": f"{e['type']} date {day} ({e.get('status') or '?'}): {(e.get('name') or '')[:90]}",
                    "detail": "Data: pdufa.bio (third-party calendar; attribution required) — "
                              "confirm the date in the company's own filings",
                    "amount": None, "url": e.get("url") or f"https://www.pdufa.bio/ticker/{meta.get('ticker')}",
                    "event_date": day, "dedupe_hash": _hash("pdufa", e.get("id") or f"{meta['cik']}|{day}")})
    return out


# --------------------------------------------------------------------------- #
# Feed 3 — patent grants (USPTO PatentsView; free API key via PATENTSVIEW_API_KEY)
# --------------------------------------------------------------------------- #
def fetch_patents(session: PoliteSession, start: str, end: str,
                  idx: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    key = os.environ.get("PATENTSVIEW_API_KEY", "")
    if not key:
        log.info("PatentsView: no PATENTSVIEW_API_KEY set — skipping patents feed")
        return []
    r = session.get("https://search.patentsview.org/api/v1/patent/",
                    headers={"User-Agent": _ua(), "X-Api-Key": key}, timeout=40, params={
                        "q": json.dumps({"_and": [{"_gte": {"patent_date": start}}, {"_lte": {"patent_date": end}}]}),
                        "f": json.dumps(["patent_id", "patent_title", "patent_date", "assignees.assignee_organization"]),
                        "s": json.dumps([{"patent_date": "desc"}]),
                        "o": json.dumps({"size": 1000})})
    patents = r.json().get("patents", []) or []
    if len(patents) >= 1000:
        log.info("PatentsView: window has >=1000 grants — older grants in window not sampled "
                 "(narrow the window or paginate if patent coverage matters)")
    out: list[dict[str, Any]] = []
    for p in patents:
        org = ""
        for asg in (p.get("assignees") or []):
            org = asg.get("assignee_organization") or ""
            if org:
                break
        m = match_org(org, idx)
        if not m:
            continue
        pid = p.get("patent_id", "")
        out.append({**_base(m), "source": "PatentsView", "category": "patent",
                    "headline": f"Patent granted: {(p.get('patent_title') or '')[:90]}",
                    "detail": f"{org} — US{pid}", "amount": None,
                    "url": f"https://patents.google.com/patent/US{pid}",
                    "event_date": p.get("patent_date") or end, "dedupe_hash": _hash("pv", pid, m["cik"])})
    return out


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def ingest(days: int = 30, conn=None, session: PoliteSession | None = None) -> dict[str, int]:
    """Fetch all external feeds for the last `days`, match to universe, store.

    Each feed is isolated, but a failed feed is REPORTED: rows from the others are
    stored, then this raises so the run shows as failed instead of a quiet "ok"."""
    session = session or PoliteSession()
    end = _today().date().isoformat()
    start = (_today() - timedelta(days=days)).date().isoformat()
    idx = build_name_index()
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {"contracts": 0, "fda": 0, "pdufa": 0, "patents": 0}
    failed: list[str] = []
    for name, key, fn in (("contracts", "contracts", fetch_contracts),
                          ("openFDA", "fda", fetch_fda),
                          ("FDA novel approvals", "fda", fetch_fda_novel),
                          ("ClinicalTrials", "fda", fetch_trials),
                          ("pdufa.bio", "pdufa", fetch_pdufa),
                          ("patents", "patents", fetch_patents)):
        try:
            got = fn(session, start, end, idx)
        except Exception as exc:  # noqa: BLE001 - isolate per feed, report below
            log.warning("external feed %s failed: %s", name, exc)
            failed.append(name)
            got = []
        counts[key] += len(got)
        rows.extend(got)
    counts["new"] = store.upsert_external_catalysts(rows, conn=conn) if rows else 0
    log.info("External feeds: %s (failed: %s)", counts, failed or "none")
    if failed:
        raise RuntimeError(f"external feeds failed: {', '.join(failed)} "
                           f"({counts['new']} new rows stored from the rest)")
    return counts
