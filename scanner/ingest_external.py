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

import requests

from scanner import store
from scanner.config import load_settings
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
def fetch_contracts(start: str, end: str, idx: dict[str, dict[str, Any]], limit: int = 200) -> list[dict[str, Any]]:
    payload = {
        "filters": {"award_type_codes": ["A", "B", "C", "D"],
                    "time_period": [{"start_date": start, "end_date": end}]},
        "fields": ["Award ID", "Recipient Name", "Award Amount", "Awarding Agency", "Description", "Start Date"],
        "limit": min(limit, 100), "page": 1, "sort": "Award Amount", "order": "desc",
    }
    out: list[dict[str, Any]] = []
    try:
        r = requests.post("https://api.usaspending.gov/api/v2/search/spending_by_award/",
                          json=payload, headers={"User-Agent": _ua()}, timeout=45)
        r.raise_for_status()
        results = r.json().get("results", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("USAspending fetch failed: %s", exc)
        return []
    for a in results:
        m = match_org(a.get("Recipient Name", ""), idx)
        if not m:
            continue
        amt = a.get("Award Amount")
        aid = a.get("Generated Internal ID") or a.get("Award ID") or ""
        agency = a.get("Awarding Agency") or ""
        pop = a.get("Start Date") or ""
        out.append({**_base(m), "source": "USAspending", "category": "contract",
                    "headline": (f"Federal contract ${amt:,.0f} — {agency}" if amt else f"Federal contract — {agency}"),
                    "detail": ((a.get("Description") or "")[:280] + (f" (PoP start {pop})" if pop else "")), "amount": amt,
                    "url": f"https://www.usaspending.gov/award/{a.get('Award ID') or aid}",
                    "event_date": end,   # time_period filter already guarantees a recent award action
                    "dedupe_hash": _hash("usasp", a.get("Award ID") or aid, m["cik"])})
    return out


# --------------------------------------------------------------------------- #
# Feed 2 — drug approvals (openFDA) + Phase-3 readouts (ClinicalTrials.gov v2)
# --------------------------------------------------------------------------- #
def fetch_fda(start: str, end: str, idx: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    hdr = {"User-Agent": _ua()}
    s_compact, e_compact = start.replace("-", ""), end.replace("-", "")
    # 2a. openFDA — recent drug approvals
    try:
        r = requests.get("https://api.fda.gov/drug/drugsfda.json", headers=hdr, timeout=30, params={
            "search": f"submissions.submission_status_date:[{s_compact} TO {e_compact}] AND submissions.submission_status:AP",
            "limit": 100})
        if r.status_code == 200:
            for app in r.json().get("results", []):
                m = match_org(app.get("sponsor_name", ""), idx)
                if not m:
                    continue
                prods = app.get("products") or [{}]
                brand = (prods[0].get("brand_name") or prods[0].get("generic_name") or "drug")
                appno = app.get("application_number", "")
                out.append({**_base(m), "source": "openFDA", "category": "fda",
                            "headline": f"FDA approval: {brand}", "detail": f"{app.get('sponsor_name','')} — application {appno}",
                            "amount": None, "url": f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={appno.split('-')[-1] if appno else ''}",
                            "event_date": end, "dedupe_hash": _hash("openfda", appno, m["cik"])})
    except Exception as exc:  # noqa: BLE001
        log.warning("openFDA fetch failed: %s", exc)
    # 2b. ClinicalTrials.gov v2 — recently-updated Phase-3 studies with results
    try:
        r = requests.get("https://clinicaltrials.gov/api/v2/studies", headers=hdr, timeout=30, params={
            "filter.advanced": "AREA[Phase]PHASE3", "filter.overallStatus": "COMPLETED",
            "sort": "LastUpdatePostDate:desc", "pageSize": 80})
        if r.status_code == 200:
            for st in r.json().get("studies", []):
                ps = st.get("protocolSection", {})
                spon = (((ps.get("sponsorCollaboratorsModule") or {}).get("leadSponsor") or {}).get("name") or "")
                m = match_org(spon, idx)
                if not m:
                    continue
                ident = ps.get("identificationModule") or {}
                status = ps.get("statusModule") or {}
                upd = ((status.get("lastUpdatePostDateStruct") or {}).get("date") or "")
                if upd and upd < start:        # only the window
                    continue
                nct = ident.get("nctId", "")
                out.append({**_base(m), "source": "ClinicalTrials", "category": "fda",
                            "headline": f"Phase 3 completed: {(ident.get('briefTitle') or '')[:90]}",
                            "detail": f"{spon} — {nct}", "amount": None,
                            "url": f"https://clinicaltrials.gov/study/{nct}",
                            "event_date": upd or end, "dedupe_hash": _hash("ctgov", nct, m["cik"])})
    except Exception as exc:  # noqa: BLE001
        log.warning("ClinicalTrials fetch failed: %s", exc)
    return out


# --------------------------------------------------------------------------- #
# Feed 3 — patent grants (USPTO PatentsView; free API key via PATENTSVIEW_API_KEY)
# --------------------------------------------------------------------------- #
def fetch_patents(start: str, end: str, idx: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    key = os.environ.get("PATENTSVIEW_API_KEY", "")
    if not key:
        log.info("PatentsView: no PATENTSVIEW_API_KEY set — skipping patents feed")
        return []
    out: list[dict[str, Any]] = []
    import json as _json
    try:
        r = requests.get("https://search.patentsview.org/api/v1/patent/",
                         headers={"User-Agent": _ua(), "X-Api-Key": key}, timeout=40, params={
                             "q": _json.dumps({"_and": [{"_gte": {"patent_date": start}}, {"_lte": {"patent_date": end}}]}),
                             "f": _json.dumps(["patent_id", "patent_title", "patent_date", "assignees.assignee_organization"]),
                             "o": _json.dumps({"size": 100})})
        r.raise_for_status()
        for p in r.json().get("patents", []):
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
def ingest(days: int = 30, conn=None) -> dict[str, int]:
    """Fetch all external feeds for the last `days`, match to universe, store."""
    end = _today().date().isoformat()
    start = (_today() - timedelta(days=days)).date().isoformat()
    idx = build_name_index()
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for name, fn in (("contracts", lambda: fetch_contracts(start, end, idx)),
                     ("fda", lambda: fetch_fda(start, end, idx)),
                     ("patents", lambda: fetch_patents(start, end, idx))):
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
