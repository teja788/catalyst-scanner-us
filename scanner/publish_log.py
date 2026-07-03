"""Publish the local research log to GitHub Pages.

The raw log (digests/research_log.md) is PRIVATE by design — gitignored, local
only. This module copies it to docs/index.md (the GitHub Pages source) and
optionally commits+pushes JUST that file, so publishing is always an explicit,
deliberate act — nothing goes public as a side effect of saving an analysis.

GitHub Pages renders docs/index.md through Jekyll (docs/_config.yml sets the
theme), so no HTML generation or extra dependencies are needed here.

One-time repo setup (manual, in the GitHub UI):
  Settings -> Pages -> "Deploy from a branch" -> branch `main`, folder `/docs`.
  NOTE: the published page is PUBLIC. On the Free plan, Pages also requires the
  repository itself to be public.
"""
from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

from scanner.config import ROOT, load_settings, resolve_path
from scanner.research_log import LOG_PATH

log = logging.getLogger(__name__)

DOCS_PAGE = resolve_path("docs/index.md")

_FRONT_MATTER = """---
layout: default
title: catalyst-scanner-us — research log
---

"""


def _tz() -> ZoneInfo:
    return ZoneInfo(load_settings().get("timezone", "America/New_York"))


def pages_url() -> str:
    """Best-effort https://<owner>.github.io/<repo>/ from the origin remote."""
    try:
        remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT,
                                capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", remote)
    return f"https://{m.group(1)}.github.io/{m.group(2)}/" if m else ""


def render_page() -> str:
    """The docs/index.md content: front matter + publish stamp + the log verbatim."""
    if not LOG_PATH.exists():
        raise FileNotFoundError(
            f"No research log at {LOG_PATH} — run a scan analysis first (it saves there).")
    stamp = datetime.now(_tz()).strftime("%Y-%m-%d %H:%M ET")
    return (_FRONT_MATTER
            + f"_Published {stamp} · research leads only — **not investment advice**._\n\n"
            + LOG_PATH.read_text(encoding="utf-8"))


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def publish(push: bool = True) -> dict[str, str]:
    """Write docs/index.md from the local log; optionally commit+push ONLY that file.

    Returns {"status", "page", "url"} — status is "published", "written (not pushed)",
    or "unchanged". Raises on git failures so callers can show the real error.
    """
    page = render_page()
    DOCS_PAGE.parent.mkdir(parents=True, exist_ok=True)
    # Ignore the volatile "Published <stamp>" line when deciding if anything changed,
    # so re-publishing an identical log doesn't create an empty-diff commit.
    old = DOCS_PAGE.read_text(encoding="utf-8") if DOCS_PAGE.exists() else ""
    strip = lambda s: re.sub(r"_Published .*? ET", "", s, count=1)  # noqa: E731
    if strip(old) == strip(page):
        return {"status": "unchanged", "page": str(DOCS_PAGE), "url": pages_url()}
    DOCS_PAGE.write_text(page, encoding="utf-8")
    if not push:
        return {"status": "written (not pushed)", "page": str(DOCS_PAGE), "url": pages_url()}

    rel = DOCS_PAGE.relative_to(ROOT).as_posix()
    _git("add", rel)
    stamp = datetime.now(_tz()).strftime("%Y-%m-%d %H:%M ET")
    commit = _git("commit", "-m", f"Publish research log ({stamp})", "--", rel)
    if commit.returncode != 0:
        if "nothing to commit" in (commit.stdout + commit.stderr):
            return {"status": "unchanged", "page": str(DOCS_PAGE), "url": pages_url()}
        raise RuntimeError(f"git commit failed: {(commit.stderr or commit.stdout).strip()[:400]}")
    pushed = _git("push")
    if pushed.returncode != 0:
        raise RuntimeError(f"git push failed: {(pushed.stderr or pushed.stdout).strip()[:400]}")
    log.info("Research log published to %s", pages_url())
    return {"status": "published", "page": str(DOCS_PAGE), "url": pages_url()}
