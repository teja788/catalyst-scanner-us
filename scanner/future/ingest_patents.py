"""FUTURE HOOK — patent grants. NOT IMPLEMENTED.

Free endpoint (verify live; may now require a free key):
  - USPTO PatentsView:  https://search.patentsview.org/api/v1/patent/

Approach: query grants by assignee organisation, match assignee -> universe, tag as
'patent_grant'. A slow/structural signal (IP moat), not a same-day catalyst.
"""
from __future__ import annotations

from typing import Any


def ingest(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    raise NotImplementedError("Patents ingester is a phase-2 TODO. See module docstring.")
