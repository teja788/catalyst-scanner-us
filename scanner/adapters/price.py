"""FUTURE HOOK — price / quote adapter. NOT WIRED IN.

Section 17: a free price adapter for size context (e.g. % move on a catalyst day)
and reaction-watching. Start free with yfinance (already a dependency) or Finnhub.

Design intent when built:
- Read any keys from env INSIDE the functions (never at import), so importing this
  module never requires a key and the core tool keeps working without one.
- Return data normalised to simple dicts so the context pack needs no changes.
"""
from __future__ import annotations

from typing import Any


def get_quote(ticker: str) -> dict[str, Any]:
    """Would return {price, change_pct, volume, mcap} for a ticker (yfinance/Finnhub)."""
    raise NotImplementedError(
        "Price adapter is a future hook. TODO: `import yfinance; yf.Ticker(ticker).fast_info` "
        "or Finnhub /quote; map to {price, change_pct, volume, mcap}. Read FINNHUB_API_KEY "
        "from env inside this function only."
    )
