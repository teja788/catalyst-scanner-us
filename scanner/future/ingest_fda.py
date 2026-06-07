"""FUTURE HOOK — FDA / biotech catalysts. NOT IMPLEMENTED (huge biotech movers).

Free endpoints (verify live when building):
  - openFDA:            https://api.fda.gov/drug/...   (approvals, labels, enforcement)
  - Drugs@FDA + FDA press-announcement RSS
  - ClinicalTrials.gov v2: https://clinicaltrials.gov/api/v2/studies  (trial status/readouts)

Approach: match sponsor/company names to the universe; tag approval / PDUFA date /
phase-3 readout / CRL as catalyst categories (see catalysts.yaml: fda_clinical).
"""
from __future__ import annotations

from typing import Any


def ingest(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    raise NotImplementedError("FDA/biotech ingester is a phase-2 TODO. See module docstring.")
