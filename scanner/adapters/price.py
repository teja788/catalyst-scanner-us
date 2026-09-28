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


def fetch_daily(session: PoliteSession, ticker: str,
                range_: str = "3mo") -> dict[str, dict[str, float]]:
    """{"closes": {ISO date: close}, "volumes": {ISO date: volume}} for the last
    `range_` of trading days, or {} on failure."""
    if not ticker:
        return {}
    try:
        # Yahoo spells class shares with '-' (BRK-A, BF-B); Nasdaq Trader uses '.'
        js = session.get(_CHART_URL.format(ticker=ticker.upper().replace(".", "-")), timeout=20,
                         params={"range": range_, "interval": "1d"},
                         headers={"Accept": "application/json"}).json()
        result = (js.get("chart", {}).get("result") or [{}])[0]
        ts = result.get("timestamp") or []
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    except Exception as exc:  # noqa: BLE001 - quotes are best-effort garnish
        log.debug("price fetch %s -> %s", ticker, exc)
        return {}
    tz = _et()
    closes: dict[str, float] = {}
    volumes: dict[str, float] = {}
    for t, c, v in zip(ts, quote.get("close") or [], quote.get("volume") or []):
        if c is not None:
            d = datetime.fromtimestamp(t, tz=tz).date().isoformat()
            closes[d] = float(c)
            if v is not None:
                volumes[d] = float(v)
    return {"closes": closes, "volumes": volumes} if closes else {}


def last_session(data: dict[str, dict[str, float]]) -> dict[str, float] | None:
    """{"pct": last session's % move, "vol_x": its volume vs the prior-20-session
    average} — the 'is it moving on this NOW?' check. None when data is short."""
    closes, vols = data.get("closes") or {}, data.get("volumes") or {}
    dates = sorted(closes)
    if len(dates) < 2 or not closes[dates[-2]]:
        return None
    prior = [vols[d] for d in dates[-21:-1] if vols.get(d)]
    avg = sum(prior) / len(prior) if prior else 0
    return {"pct": round((closes[dates[-1]] / closes[dates[-2]] - 1) * 100, 1),
            "vol_x": round(vols.get(dates[-1], 0) / avg, 1) if avg else 0.0}


def close_on_or_before(closes: dict[str, float], day: str) -> tuple[str, float] | None:
    """(date, close) of the last close on/before `day` (ISO), or None."""
    prior = [d for d in sorted(closes) if d <= day[:10]]
    return (prior[-1], closes[prior[-1]]) if prior else None


def reaction(closes: dict[str, float], event_date_iso: str) -> dict[str, Any] | None:
    """Compute {last, last_date, baseline, pct_since_event} for an event date.

    Baseline = the last close BEFORE the market could react: for an event at or
    after the 16:00 ET close that is the event day's own close (the reaction is the
    next session); otherwise (pre-market / intraday / date-only) the close before
    the event day. None when the history doesn't reach back or has no data.
    """
    if not closes:
        return None
    event = (event_date_iso or "")[:10]
    if not event:
        return None
    try:
        after_close = (len(event_date_iso) > 10 and
                       datetime.fromisoformat(event_date_iso).astimezone(_et()).hour >= 16)
    except ValueError:
        after_close = False
    dates = sorted(closes)
    base_dates = [d for d in dates if d <= event] if after_close else [d for d in dates if d < event]
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
