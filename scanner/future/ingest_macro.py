"""FUTURE HOOK — macro context (FRED). NOT IMPLEMENTED.

Free endpoint (free API key required, read from env inside the function):
  - FRED:  https://api.stlouisfed.org/fred/series/observations  (rates, CPI, etc.)
  - (optional) Google Trends for consumer-demand signals

Approach: pull a small set of macro series for backdrop the agent can weigh when
ranking (rate regime, etc.). Context only — not per-company catalysts.
"""
from __future__ import annotations

from typing import Any


def ingest(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    raise NotImplementedError("Macro (FRED) ingester is a phase-2 TODO. See module docstring. "
                              "Read FRED_API_KEY from env inside this function only.")
