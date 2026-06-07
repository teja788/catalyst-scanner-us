"""FUTURE HOOK — federal contract awards. NOT IMPLEMENTED.

Free endpoints (verify live when building):
  - USAspending.gov:  https://api.usaspending.gov/api/v2/search/spending_by_award/  (no key)
  - DoD daily contract announcements; SAM.gov for opportunities

Approach: pull recent awards, match recipient names to the universe (recipient ->
ticker/CIK), tag as 'contract_win' with the award amount + agency. Material for
defense/gov-exposed names.
"""
from __future__ import annotations

from typing import Any


def ingest(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    raise NotImplementedError("Federal-contracts ingester is a phase-2 TODO. See module docstring.")
