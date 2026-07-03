"""Free daily-close price adapter (Yahoo chart API — no key required).

Feeds the context pack's PRICE REACTION note: "has this name already re-rated
since the event?" is the most direct check on the rubric's under-appreciated /
not-yet-priced-in gate — RSS coverage counts alone are a weak proxy.

Design constraints honoured:
- No key at import or call time; a browser-like UA (the PoliteSession default)
  is enough for query1.finance.yahoo.com (verified live 2026-07; Stooq is
  JS-challenge-walled and unusable headless).
- Every function is failure-tolerant: any per-ticker error returns None/{} so a
  dead quote source can never break a scan.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from scanner.config import load_settings
from scanner.http import PoliteSession

log = logging.getLogger(__name__)

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"


def _et() -> ZoneInfo:
    return ZoneInfo(load_settings().get("timezone", "America/New_York"))


def fetch_daily_closes(session: PoliteSession, ticker: str,
                       range_: str = "3mo") -> dict[str, float]:
    """{ISO date: close} for the last `range_` of trading days, or {} on failure."""
    if not ticker:
        return {}
    try:
        js = session.get(_CHART_URL.format(ticker=ticker.upper()), timeout=20,
                         params={"range": range_, "interval": "1d"},
                         headers={"Accept": "application/json"}).json()
        result = (js.get("chart", {}).get("result") or [{}])[0]
        ts = result.get("timestamp") or []
        closes = ((result.get("indicators", {}).get("quote") or [{}])[0].get("close")) or []
    except Exception as exc:  # noqa: BLE001 - quotes are best-effort garnish
        log.debug("price fetch %s -> %s", ticker, exc)
        return {}
    tz = _et()
    out: dict[str, float] = {}
    for t, c in zip(ts, closes):
        if c is not None:
            out[datetime.fromtimestamp(t, tz=tz).date().isoformat()] = float(c)
    return out


def reaction(closes: dict[str, float], event_date_iso: str) -> dict[str, Any] | None:
    """Compute {last, last_date, baseline, pct_since_event} for an event date.

    Baseline = last close strictly BEFORE the event date (most catalysts land
    after-hours, so the event day's own close already reacts). None when the
    history doesn't reach back to the event or has no data.
    """
    if not closes:
        return None
    event = (event_date_iso or "")[:10]
    if not event:
        return None
    dates = sorted(closes)
    base_dates = [d for d in dates if d < event]
    if not base_dates:
        return None
    baseline = closes[base_dates[-1]]
    last_date = dates[-1]
    last = closes[last_date]
    if not baseline:
        return None
    return {
        "last": round(last, 2),
        "last_date": last_date,
        "baseline": round(baseline, 2),
        "pct_since_event": round((last / baseline - 1.0) * 100, 1),
    }
