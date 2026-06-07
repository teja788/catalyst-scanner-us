"""FUTURE HOOK — short interest (squeeze setups). NOT IMPLEMENTED.

Free source (verify live; bi-monthly cadence):
  - FINRA / exchange short-interest data (settlement-date files)

Approach: load short interest + days-to-cover per ticker, match to the universe, and
flag HIGH short interest + a fresh strong catalyst as a potential squeeze setup
(combine with the EDGAR/ownership signals already collected).
"""
from __future__ import annotations

from typing import Any


def ingest(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    raise NotImplementedError("Short-interest ingester is a phase-2 TODO. See module docstring.")
