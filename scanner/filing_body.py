"""Filing body-text extraction (the India pdf_extract analog, for EDGAR HTML).

Reads the primary document of catalyst-tagged filings so the agent sees WHAT a
filing is about — e.g. an 8-K "Item 1.01 Entry into a Material Definitive Agreement"
becomes "…a $200M supply agreement with X…". Re-tags on the body so business
catalysts (contract / capacity / FDA / partnership / patent) that aren't in the
item title get surfaced. Cached in the filing_text table (keyed by the filing's
dedupe_hash) so each document is fetched at most once.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from scanner import store
from scanner.http import PoliteSession
from scanner.prefilter import tag_keywords

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)  # EDGAR XBRL .htm are XML
log = logging.getLogger(__name__)

_WORKERS = 8
MAX_CHARS = 5000
# A primary doc shorter than this is usually a two-paragraph wrapper ("see
# Exhibit 99.1") — the substance lives in the press-release exhibit.
_THIN_PRIMARY = 1500


def extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def _ex99_text(session: PoliteSession, f: dict[str, Any]) -> str:
    """Fetch the first EX-99 exhibit of a filing (the press release) — used when
    the primary document is a thin wrapper. Best-effort; '' on any failure."""
    url = f.get("filing_url") or ""
    m = re.match(r"(https://www\.sec\.gov/Archives/edgar/data/\d+/\d+)/", url)
    if not m:
        return ""
    folder = m.group(1)
    try:
        listing = session.edgar_get(f"{folder}/index.json", timeout=30).json()
        items = ((listing.get("directory") or {}).get("item")) or []
    except Exception as exc:  # noqa: BLE001
        log.debug("exhibit listing %s -> %s", folder, exc)
        return ""
    for it in items:
        name = (it.get("name") or "").lower()
        if re.search(r"ex[-_]?99", name) and name.endswith((".htm", ".html", ".txt")):
            try:
                return extract_text(session.edgar_get(f"{folder}/{it.get('name')}", timeout=45).text)
            except Exception as exc:  # noqa: BLE001
                log.debug("exhibit fetch %s -> %s", name, exc)
                return ""
    return ""


def _body_for(session: PoliteSession, f: dict[str, Any], fetch_missing: bool = True) -> str:
    """Return cached (or, if `fetch_missing`, freshly-fetched) body text for one filing."""
    h = f.get("dedupe_hash")
    if h:
        cached = store.get_filing_text(h)
        if cached and cached.get("text"):
            return cached["text"]
    if not fetch_missing:
        return ""
    url = f.get("filing_url") or ""
    if not url.lower().endswith((".htm", ".html", ".txt")):
        return ""
    method = "html"
    try:
        html = session.edgar_get(url, timeout=45).text
        text = extract_text(html)
    except Exception as exc:  # noqa: BLE001
        log.warning("body fetch %s -> %s", url, exc)
        text = ""
    # Thin 8-K wrapper → append the EX-99 press-release exhibit, where the actual
    # deal terms / dollar figures live (materiality hints read boilerplate otherwise).
    if text and len(text) < _THIN_PRIMARY and (f.get("form_type") or "").startswith(("8-K", "6-K")):
        extra = _ex99_text(session, f)
        if extra:
            text = f"{text} [EX-99 exhibit] {extra}"
            method = "html+ex99"
    text = text[:MAX_CHARS]
    if h:
        store.save_filing_text(h, url, text, method)
    return text


def _enrich_rank(f: dict[str, Any]) -> int:
    """Fetch-priority when the cap bites: deal-bearing 8-K items first, then other
    tagged filings, then untagged 6-Ks (no item codes — body is their only shot)."""
    form = f.get("form_type") or ""
    items = set(f.get("item_codes") or [])
    if form.startswith("8-K") and items & {"1.01", "2.01", "8.01", "7.01"}:
        return 0
    if f.get("candidate_tags"):
        return 1
    return 2


def enrich(filings: list[dict[str, Any]], session: PoliteSession | None = None,
           max_fetch: int = 80, fetch_missing: bool = True) -> dict[str, int]:
    """Fetch + cache body text for up to `max_fetch` non-routine filings; set
    f['body_text'] and ADD any catalyst tags found in the body.

    Targets: every catalyst-tagged filing PLUS untagged 6-K/6-K/A current reports —
    6-Ks carry no 8-K item codes and a useless doc-description headline, so reading
    the body is the ONLY way an ADR catalyst (TSM, ASML, NVO...) can surface.
    When the cap bites, deal-bearing 8-Ks (1.01/2.01/8.01/7.01) go first.

    Body-derived tags are PERSISTED back to the filings table, so tag queries and
    later cache-only passes (the dashboard) see them too. `fetch_missing=False`
    uses only already-cached bodies — zero network, dashboard-snappy.

    Returns {"targets": fetched, "skipped": beyond-cap, "retagged": n} so the
    context pack can DISCLOSE incomplete body coverage instead of hiding it.
    """
    session = session or PoliteSession()
    candidates = [f for f in filings if not f.get("is_routine")
                  and (f.get("candidate_tags") or (f.get("form_type") or "").startswith("6-K"))]
    candidates.sort(key=_enrich_rank)          # stable: recency preserved within rank
    targets = candidates[:max_fetch]
    skipped = len(candidates) - len(targets)
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        results = list(pool.map(lambda x: (x, _body_for(session, x, fetch_missing)), targets))
    tag_updates: list[tuple[int, list[str]]] = []
    for f, text in results:
        f["body_text"] = text
        if text:
            merged = list(dict.fromkeys((f.get("candidate_tags") or []) + tag_keywords(text)))
            if merged != f.get("candidate_tags") and f.get("id") is not None:
                tag_updates.append((f["id"], merged))
            f["candidate_tags"] = merged
    store.set_filing_tags_bulk(tag_updates)
    log.info("Filing-body enrich: %d filings read (%s), %d skipped by cap, %d re-tagged from body",
             len(targets), "cached+fetched" if fetch_missing else "cache-only", skipped, len(tag_updates))
    return {"targets": len(targets), "skipped": skipped, "retagged": len(tag_updates)}
