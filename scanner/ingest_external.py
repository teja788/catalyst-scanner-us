"""Non-EDGAR catalyst feeds (the broad asymmetric-opportunity layer).

Three free sources, each fetched for a recent window, matched to a universe company
by org name, and stored as `external_catalysts` rows (dedupe-safe):

  - USAspending.gov      -> federal CONTRACT awards (recipient -> universe)
  - openFDA + ClinicalTrials.gov -> drug APPROVALS / Phase-3 readouts (sponsor -> universe)
  - USPTO PatentsView     -> PATENT grants (assignee -> universe)   [needs a free key]

The org-name matcher is conservative: normalise (strip corporate suffixes/punct) then
require an exact or leading-token hit against the universe name/alias index, so we only
keep confident public-company matches and drop subsidiaries/generic names.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from scanner import store
from scanner.config import load_settings
from scanner.http import PoliteSession
from scanner.universe import load_map

log = logging.getLogger(__name__)

_UA = "Catalyst Scanner US iamteja1988@gmail.com"
_TZ = "America/New_York"


def _ua() -> str:
    return (load_settings().get("edgar", {}) or {}).get("user_agent", _UA)


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
    fields = ["Award ID", "Recipient Name", "Award Amount", "Awarding Agency", "Description", "Start Date"]
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
                log.warning("USAspending fetch failed (sort=%s page %d): %s", sort_field, page, exc)
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
                            "event_date": pop or end,   # new_awards_only: Start Date ~ award date
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
    out: list[dict[str, Any]] = []
    hdr = {"User-Agent": _ua(), "Accept": "application/json"}
    s_compact, e_compact = start.replace("-", ""), end.replace("-", "")
    # 2a. openFDA — recent drug approvals. The search matches the APPLICATION if ANY
    # submission matches, so re-check the submissions list for the approval that
    # actually falls in the window, and label ORIG (new approval) vs supplement.
    try:
        r = session.get("https://api.fda.gov/drug/drugsfda.json", headers=hdr, timeout=30, params={
            "search": f"submissions.submission_status_date:[{s_compact} TO {e_compact}] AND submissions.submission_status:AP",
            "limit": 100})
        for app in r.json().get("results", []):
            m = match_org(app.get("sponsor_name", ""), idx)
            if not m:
                continue
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
    except Exception as exc:  # noqa: BLE001 - openFDA 404s an empty result set
        log.warning("openFDA fetch failed (404 = no approvals in window): %s", exc)
    # 2b. ClinicalTrials.gov v2 — Phase-3 studies whose RESULTS were posted in the
    # window. Filter SERVER-SIDE on ResultsFirstPostDate (verified live 2026-07):
    # the old client-side filter over the top-100 most-recently-UPDATED studies
    # silently missed any readout not among the 100 latest record edits.
    try:
        params: dict[str, Any] = {
            "filter.advanced": f"AREA[Phase]PHASE3 AND AREA[ResultsFirstPostDate]RANGE[{start},{end}]",
            "aggFilters": "results:with", "pageSize": 100}
        for _page in range(5):   # up to 500 readouts per window — far above reality
            r = session.get("https://clinicaltrials.gov/api/v2/studies", headers=hdr,
                            timeout=30, params=params)
            js = r.json()
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
    except Exception as exc:  # noqa: BLE001
        log.warning("ClinicalTrials fetch failed: %s", exc)
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
    out: list[dict[str, Any]] = []
    import json as _json
    try:
        r = session.get("https://search.patentsview.org/api/v1/patent/",
                        headers={"User-Agent": _ua(), "X-Api-Key": key}, timeout=40, params={
                            "q": _json.dumps({"_and": [{"_gte": {"patent_date": start}}, {"_lte": {"patent_date": end}}]}),
                            "f": _json.dumps(["patent_id", "patent_title", "patent_date", "assignees.assignee_organization"]),
                            "s": _json.dumps([{"patent_date": "desc"}]),
                            "o": _json.dumps({"size": 1000})})
        patents = r.json().get("patents", []) or []
        if len(patents) >= 1000:
            log.info("PatentsView: window has >=1000 grants — older grants in window not sampled "
                     "(narrow the window or paginate if patent coverage matters)")
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
    except Exception as exc:  # noqa: BLE001
        log.warning("PatentsView fetch failed: %s", exc)
    return out


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def ingest(days: int = 30, conn=None, session: PoliteSession | None = None) -> dict[str, int]:
    """Fetch all external feeds for the last `days`, match to universe, store."""
    session = session or PoliteSession()
    end = _today().date().isoformat()
    start = (_today() - timedelta(days=days)).date().isoformat()
    idx = build_name_index()
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for name, fn in (("contracts", lambda: fetch_contracts(session, start, end, idx)),
                     ("fda", lambda: fetch_fda(session, start, end, idx)),
                     ("patents", lambda: fetch_patents(session, start, end, idx))):
        try:
            got = fn()
        except Exception as exc:  # noqa: BLE001
            log.warning("external feed %s failed: %s", name, exc)
            got = []
        counts[name] = len(got)
        rows.extend(got)
    new = store.upsert_external_catalysts(rows, conn=conn) if rows else 0
    counts["new"] = new
    log.info("External feeds: %s", counts)
    return counts
