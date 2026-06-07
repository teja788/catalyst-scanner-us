"""Phase-2 ingester stubs (Section 17) — clearly-marked TODOs, NOT implemented.

Each module documents the free endpoint + the approach, and raises
NotImplementedError from its ingest() so nothing runs by accident. Wire them into
cli._refresh_all the same way as ingest_edgar/news/ownership when built, and verify
each endpoint live first (per the build discipline used for M2-M5).

  ingest_fda            — openFDA + ClinicalTrials.gov v2 (approvals, PDUFA, readouts)
  ingest_contracts      — USAspending.gov awards (recipient -> universe match)
  ingest_patents        — USPTO PatentsView grants by assignee
  ingest_short_interest — FINRA/exchange bi-monthly short interest (squeeze setups)
  ingest_macro          — FRED rates/macro context (key required)
"""
