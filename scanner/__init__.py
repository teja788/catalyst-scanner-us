"""catalyst-scanner-us — a local, free, on-demand US-market catalyst scanner.

The Python layer is deterministic plumbing only: it fetches, dedupes, stores,
and pre-filters SEC filings / news / ownership data into a compact "context
pack". It does NOT judge what is asymmetric — that reasoning is done live by the
agent (see CLAUDE.md).

US sibling of the India "catalyst-scanner"; same architecture, US data layer.
"""

__version__ = "0.1.0"
__app_name__ = "catalyst-scanner-us"
