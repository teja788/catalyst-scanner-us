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


def extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def _body_for(session: PoliteSession, f: dict[str, Any]) -> str:
    """Return cached or freshly-fetched body text for one filing."""
    h = f.get("dedupe_hash")
    if h:
        cached = store.get_filing_text(h)
        if cached and cached.get("text"):
            return cached["text"]
    url = f.get("filing_url") or ""
    if not url.lower().endswith((".htm", ".html", ".txt")):
        return ""
    try:
        html = session.edgar_get(url, timeout=45).text
        text = extract_text(html)[:MAX_CHARS]
    except Exception as exc:  # noqa: BLE001
        log.warning("body fetch %s -> %s", url, exc)
        text = ""
    if h:
        store.save_filing_text(h, url, text, "html")
    return text


def enrich(filings: list[dict[str, Any]], session: PoliteSession | None = None,
           max_fetch: int = 80) -> list[dict[str, Any]]:
    """Fetch + cache body text for up to `max_fetch` catalyst-tagged, non-routine
    filings; set f['body_text'] and ADD any catalyst tags found in the body.
    """
    session = session or PoliteSession()
    targets = [f for f in filings if f.get("candidate_tags") and not f.get("is_routine")][:max_fetch]
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        results = list(pool.map(lambda x: (x, _body_for(session, x)), targets))
    for f, text in results:
        f["body_text"] = text
        if text:
            merged = list(dict.fromkeys((f.get("candidate_tags") or []) + tag_keywords(text)))
            f["candidate_tags"] = merged
    log.info("Filing-body enrich: %d filings read (cached or fetched)", len(targets))
    return filings
