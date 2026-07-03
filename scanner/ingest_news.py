"""News + press-wire RSS ingester + company tagging (Milestone 4).

Mirrors the India project's ingest_news.py: a 3-tier precision Tagger maps free
text (headlines/summaries) to universe companies; feeds are fetched with per-feed
isolation; items are deduped by URL. US adaptations:
  - keyed on SEC CIK (not ISIN);
  - only BROAD feeds run in the refresh — per-ticker feeds (Yahoo) are reserved
    for `ask`, since looping them over 500-5000 names per run won't scale;
  - timestamps in ET;
  - a TICKER stopword list so common English words that are also tickers
    (ALL, NOW, IT, ON, DOW, ...) don't false-tag; the company's NAME alias still
    tags it.

Feeds were curated live in M4 (see sources.yaml); dead/stale ones are commented.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import feedparser

from scanner.config import load_settings, load_sources
from scanner.http import PoliteSession
from scanner.universe import load_map

log = logging.getLogger(__name__)


def _et() -> ZoneInfo:
    return ZoneInfo(load_settings().get("timezone", "America/New_York"))


# Common English words / finance abbreviations that are ALSO tickers — skip the
# uppercase-ticker tier for these (they would false-tag constantly, e.g. "IR" for
# Investor Relations, "DE" for Germany/Delaware). The company's distinctive NAME
# alias ("ingersoll rand", "deere", "allstate") still tags it.
TICKER_STOPWORDS = {
    "ALL", "NOW", "IT", "ON", "DOW", "KEY", "NEW", "CEO", "ARE", "FOR", "BIG",
    "FUN", "LOVE", "CAR", "WELL", "GOOD", "OPEN", "GAIN", "HAS", "AI", "DD",
    "SO", "OR", "BY", "AN", "AT", "BE", "GO", "UP", "US", "TV", "PM", "EV", "HE", "WE",
    "IR", "DE", "PR", "FY", "CFO", "COO", "ESG", "IPO", "ETF", "USA", "USD",
    "SEC", "FDA", "ROI", "EPS", "GDP", "ADR", "ADS", "EST", "CET", "TSX", "NYSE", "AM",
    "GPS",   # Gap's ticker, but headlines say GPS the technology
    "SNAP",  # Snap's ticker, but policy headlines say "SNAP benefits" (food stamps)
}

# Company NAME aliases too generic to tag on — they collide with ubiquitous
# exchange mentions ("...lists on Nasdaq") or with common English words that
# Title-Case headlines capitalise ("Price Target Raised" is not Target Corp).
# Precision over recall: these companies tag via an explicit ticker/name instead.
ALIAS_STOPWORDS = {"nasdaq", "nyse", "target", "gap", "block", "shell", "snap"}


# --------------------------------------------------------------------------- #
# Company tagging
# --------------------------------------------------------------------------- #
class Tagger:
    """Maps free text to universe companies (by CIK) via three precision tiers.

      1. MULTI-WORD names ("public storage", "digital realty") -> case-insensitive.
      2. SINGLE-WORD names ("salesforce", "nvidia", len>=4) -> only when Capitalised.
      3. TICKERS ("AAPL", "NVDA") -> uppercase-only, minus the stopword list.
    """

    _WORD_RE = re.compile(r"[A-Za-z][A-Za-z&]+")

    def __init__(self, universe: list[dict[str, Any]]):
        self._meta = {c["cik"]: c for c in universe}
        multi_map: dict[str, str] = {}    # "public storage" -> cik
        single_map: dict[str, str] = {}   # "salesforce" -> cik (match only if Capitalised)
        ticker_map: dict[str, str] = {}   # "CRM" -> cik (uppercase only)

        for c in universe:
            cik = c["cik"]
            ticker = (c.get("ticker") or "").strip().upper()
            if len(ticker) >= 2 and ticker not in TICKER_STOPWORDS:
                ticker_map.setdefault(ticker, cik)
            for alias in c.get("aliases", []):
                # Ticker-as-alias (present in universe files built before 2026-07):
                # tickers belong ONLY to the uppercase tier below — as a lowercase
                # alias they'd match capitalised sentence-start English words
                # ("Well", "Open", "Next") and false-tag ~9% of news.
                if alias == ticker.lower():
                    continue
                if alias in ALIAS_STOPWORDS:
                    # the short alias is too generic ("target"), but the FULL formal
                    # name ("target corporation") is still distinctive — keep that path
                    full = re.sub(r"\s+", " ", re.sub(r"[^\w\s&]", " ", (c.get("name") or "").lower())).strip()
                    if " " in full:
                        multi_map.setdefault(full, cik)
                    continue
                if " " in alias:
                    multi_map.setdefault(alias, cik)
                elif len(alias) >= 4:
                    single_map.setdefault(alias, cik)

        self._multi_map = multi_map
        self._single_map = single_map
        self._ticker_map = ticker_map
        self._multi_re = self._compile(multi_map.keys(), boundary_amp=False)
        self._ticker_re = self._compile(ticker_map.keys(), boundary_amp=True)

    @staticmethod
    def _compile(aliases, *, boundary_amp: bool) -> re.Pattern | None:
        items = sorted((a for a in aliases if a), key=len, reverse=True)
        if not items:
            return None
        alt = "|".join(re.escape(a) for a in items)
        lhs = r"(?<![A-Za-z0-9&])" if boundary_amp else r"(?<![A-Za-z0-9])"
        rhs = r"(?![A-Za-z0-9&])" if boundary_amp else r"(?![A-Za-z0-9])"
        return re.compile(f"{lhs}({alt}){rhs}")

    def tag(self, text: str) -> list[str]:
        """Return matched company CIKs (deduped, order-stable)."""
        if not text:
            return []
        found: dict[str, None] = {}
        # 1. Multi-word names, case-insensitive.
        if self._multi_re:
            low = text.lower()
            for m in self._multi_re.finditer(low):
                cik = self._multi_map.get(m.group(1))
                if cik:
                    found.setdefault(cik, None)
        # 2. Single-word names, only when capitalised in the original text.
        for m in self._WORD_RE.finditer(text):
            tok = m.group(0)
            if tok[:1].isupper():
                cik = self._single_map.get(tok.lower())
                if cik:
                    found.setdefault(cik, None)
        # 3. Uppercase-only tickers (minus stopwords).
        if self._ticker_re:
            for m in self._ticker_re.finditer(text):
                cik = self._ticker_map.get(m.group(1))
                if cik:
                    found.setdefault(cik, None)
        return list(found.keys())


# --------------------------------------------------------------------------- #
# Feed parsing
# --------------------------------------------------------------------------- #
def _entry_datetime(entry: Any) -> datetime:
    """Best-effort publish time in ET; falls back to 'now' if absent."""
    for attr in ("published_parsed", "updated_parsed"):
        tm = getattr(entry, attr, None)
        if tm:
            return datetime(*tm[:6], tzinfo=timezone.utc).astimezone(_et())
    return datetime.now(_et())


def _dedupe_hash(source: str, link: str, title: str) -> str:
    """Dedupe by the article's stable identity (its URL), else source|title."""
    key = (link or "").strip().lower() or f"{source}|{title}".lower()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def fetch_feed(session: PoliteSession, feed: dict[str, Any], tagger: Tagger) -> list[dict[str, Any]]:
    """Fetch + parse one feed into normalised, company-tagged news items."""
    name = feed.get("name", "?")
    trust = feed.get("trust", "news")
    resp = session.get(feed["url"], timeout=30)
    parsed = feedparser.parse(resp.content)
    items: list[dict[str, Any]] = []
    for e in parsed.entries:
        title = (getattr(e, "title", "") or "").strip()
        link = (getattr(e, "link", "") or "").strip()
        summary = (getattr(e, "summary", "") or "").strip()
        published = _entry_datetime(e)
        ciks = tagger.tag(f"{title}. {summary}")
        items.append({
            "source": name,
            "trust": trust,
            "headline": title,
            "url": link,
            "summary": summary,
            "published_at": published.isoformat(),
            "company_ciks": ciks,
            "dedupe_hash": _dedupe_hash(name, link, title),
        })
    return items


def ingest(session: PoliteSession | None = None) -> list[dict[str, Any]]:
    """Pull all BROAD feeds (skipping per_ticker feeds). Per-feed failures isolated."""
    session = session or PoliteSession()
    tagger = Tagger(load_map())
    feeds = [f for f in load_sources().get("news_feeds", []) if not f.get("per_ticker")]

    results: list[dict[str, Any]] = []
    for feed in feeds:
        try:
            items = fetch_feed(session, feed, tagger)
            tagged = sum(1 for i in items if i["company_ciks"])
            log.info("News feed %-32s -> %3d items (%d tagged)", feed.get("name"), len(items), tagged)
            results.extend(items)
        except Exception as exc:  # noqa: BLE001 - isolate per-feed failures
            log.warning("News feed failed (%s): %s", feed.get("name"), exc)
    return results
