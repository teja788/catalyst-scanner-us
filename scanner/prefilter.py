"""Deterministic pre-filter (Milestone 7).

Two jobs, both cheap and generous (recall over precision — the reasoning layer
makes the final call):
  1. TAG candidate catalyst categories on filings + news, driven by config:
     - 8-K Item codes -> categories (catalysts.yaml: eightk_items)
     - keyword heuristics on headline/body/summary (catalysts.yaml: keywords)
     - form-type -> category for non-8-K filings
  2. DROP / down-rank routine noise (noise_filters.yaml) on news; mark
     procedural-only filings (e.g. an 8-K with just a shareholder vote) as routine.

Ownership rows are already meaningful (insider buys, activist/large stakes,
superinvestor matches) — they pass through, flagged for prioritisation.

This layer does NOT judge what is asymmetric; it only buckets/tags so the agent
reads a small, well-organised packet.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from scanner import store
from scanner.config import load_catalysts, load_noise_filters, load_settings

log = logging.getLogger(__name__)

# Non-8-K form types -> coarse category (8-K is tagged via its item codes).
FORM_CATEGORY = {
    "424B5": ["offering"], "424B4": ["ipo"], "424B1": ["ipo"],
    "S-1": ["ipo"], "S-1/A": ["ipo"],
    "DEF 14A": ["proxy"], "DEFA14A": ["proxy"],
    "10-K": ["periodic"], "10-K/A": ["periodic"],
    "10-Q": ["periodic"], "10-Q/A": ["periodic"], "20-F": ["periodic"],
}

# An 8-K whose item codes are ONLY these is procedural (vote results / exhibits) —
# kept, but marked routine so the context pack can de-prioritise it.
_PROCEDURAL_ITEMS = {"5.07", "9.01"}


def _tz() -> ZoneInfo:
    return ZoneInfo(load_settings().get("timezone", "America/New_York"))


# --------------------------------------------------------------------------- #
# Keyword rule compilation (boundary-guarded, like the India project)
# --------------------------------------------------------------------------- #
def _compile_rules() -> dict[str, re.Pattern]:
    rules = (load_catalysts().get("keywords") or {})
    compiled: dict[str, re.Pattern] = {}
    for cat, keywords in rules.items():
        kws = sorted({str(k).lower() for k in keywords}, key=len, reverse=True)
        if not kws:
            continue
        alt = "|".join(re.escape(k) for k in kws)
        # Guards stop short keywords matching inside longer words.
        compiled[cat] = re.compile(rf"(?<![a-z0-9])(?:{alt})(?![a-z0-9])")
    return compiled


_COMPILED = _compile_rules()
_EIGHTK_ITEMS = {str(k): v for k, v in (load_catalysts().get("eightk_items") or {}).items()}


def tag_keywords(*texts: str) -> list[str]:
    """Catalyst categories whose keywords appear as whole tokens."""
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return []
    return [cat for cat, pat in _COMPILED.items() if pat.search(blob)]


def tag_filing(form_type: str, item_codes: list[str], headline: str, body: str = "") -> list[str]:
    """Tag a filing: 8-K item categories + form category + keyword matches."""
    tags: list[str] = []
    if form_type.startswith("8-K"):
        for code in item_codes:
            tags.extend(_EIGHTK_ITEMS.get(code, []))
    tags.extend(FORM_CATEGORY.get(form_type, []))
    tags.extend(tag_keywords(headline, body))
    return sorted(dict.fromkeys(tags))   # unique, order-stable


def is_noise(*texts: str) -> bool:
    """True if text matches a routine/administrative noise pattern."""
    patterns = load_noise_filters().get("drop_or_downrank", [])
    blob = " ".join(t for t in texts if t).lower()
    return any(str(p).lower() in blob for p in patterns)


def _as_list(val: Any) -> list[str]:
    if isinstance(val, list):
        return val
    if isinstance(val, str) and val not in ("", "[]"):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return []
    return []


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run_prefilter(since: datetime | None = None) -> dict[str, Any]:
    """Read the recent window from the store, tag/triage, return the candidate set.

    Side effect: persists candidate_tags back onto each filing row.
    """
    lookback = int(load_settings().get("lookback_hours", 24))
    since = since or (datetime.now(_tz()) - timedelta(hours=lookback))
    since_iso = since.isoformat()

    conn = store.get_conn()
    try:
        filings = store.get_recent_filings(since_iso, conn=conn)
        news = store.get_recent_news(since_iso, conn=conn)
        ownership = store.get_recent_ownership(since_iso, conn=conn)

        # --- Filings: tag everything (already form-filtered); mark procedural-only ---
        routine = 0
        for f in filings:
            items = _as_list(f.get("item_codes"))
            tags = tag_filing(f.get("form_type", ""), items, f.get("headline", ""), f.get("body_text", ""))
            f["candidate_tags"] = tags
            f["item_codes"] = items
            is_routine = (f.get("form_type", "").startswith("8-K")
                          and bool(items) and set(items) <= _PROCEDURAL_ITEMS)
            f["is_routine"] = is_routine
            routine += int(is_routine)
            store.set_filing_tags(f["id"], tags, conn=conn)

        # --- News: tag; keep company-tagged OR catalyst-tagged; drop noisy untagged ---
        news_candidates: list[dict[str, Any]] = []
        news_noise = 0
        for n in news:
            tags = tag_keywords(n.get("headline", ""), n.get("summary", ""))
            n["candidate_tags"] = tags
            n["company_ciks"] = _as_list(n.get("company_ciks"))
            has_company = bool(n["company_ciks"])
            if has_company or tags:
                news_candidates.append(n)
            elif is_noise(n.get("headline", "")):
                news_noise += 1

        # --- Ownership: all are candidates; flag the high-signal ones ---
        flagged_ownership = [o for o in ownership
                             if o.get("is_buy") or o.get("is_activist") or o.get("matched_investor")]

        summary = {
            "window_since": since_iso,
            "filings_total": len(filings),
            "filings_routine": routine,
            "filings_tagged": sum(1 for f in filings if f.get("candidate_tags")),
            "news_total": len(news),
            "news_candidates": len(news_candidates),
            "news_company_tagged": sum(1 for n in news_candidates if n.get("company_ciks")),
            "ownership_total": len(ownership),
            "ownership_flagged": len(flagged_ownership),
            "candidates": {
                "filings": filings,            # all (form-filtered); ordered later
                "news": news_candidates,
                "ownership": ownership,
                "flagged_ownership": flagged_ownership,
            },
        }
        log.info("Prefilter: filings %d (tagged %d, routine %d) | news %d->%d | ownership %d (flagged %d)",
                 len(filings), summary["filings_tagged"], routine,
                 len(news), len(news_candidates), len(ownership), len(flagged_ownership))
        return summary
    finally:
        conn.close()
