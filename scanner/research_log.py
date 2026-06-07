"""Append signal/analysis results to a persistent, deduplicated research log.

One human-readable Markdown file (digests/research_log.md) accumulates results you
ask for. Each entry embeds a content hash (and an optional caller key) as an HTML
comment, so re-saving the SAME result — or the same `key` — is skipped instead of
duplicated. Used by the dashboard's "Rank with AI" panel.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

from scanner.config import load_settings, resolve_path

LOG_PATH = resolve_path("digests/research_log.md")


def _tz() -> ZoneInfo:
    return ZoneInfo(load_settings().get("timezone", "America/New_York"))


def _hash(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:16]


def save(content: str, title: str = "Signals", key: str | None = None) -> str:
    """Append `content` to the research log unless it's already there.

    Dedup: skip if either the content hash OR the explicit `key` already appears.
    Returns "saved", "duplicate (skipped)", or "empty (skipped)".
    """
    content = (content or "").strip()
    if not content:
        return "empty (skipped)"

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = LOG_PATH.read_text(encoding="utf-8") if LOG_PATH.exists() else ""

    h = _hash(content)
    if f"hash:{h}" in existing or (key and f"key:{key}" in existing):
        return "duplicate (skipped)"

    stamp = datetime.now(_tz()).strftime("%Y-%m-%d %H:%M ET")
    marker = f"<!-- hash:{h}" + (f" key:{key}" if key else "") + " -->"
    header = ("# Research log\n\n_Signal results, appended on request. Deduped by content/key._\n"
              if not existing else "")
    entry = f"{header}\n\n---\n\n## {stamp} — {title}\n{marker}\n\n{content}\n"

    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(entry)
    return "saved"
