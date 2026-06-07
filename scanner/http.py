"""Shared HTTP layer: one polite session for every fetcher.

Design goals (Section 16 of the spec):
- Be a polite citizen: throttle, exponential backoff, capped retries.
- Two header modes on the SAME session:
    * general (browser-like UA) for RSS feeds + the Nasdaq.com screener, which
      reject non-browser clients;
    * EDGAR (a UA carrying a real NAME + EMAIL) — SEC's fair-access rule REQUIRES
      this on every request, capped under 10 req/sec (we use ~8).
- Per-call failures raise, so each ingester can catch and continue (one dead
  source must not kill the whole run).

Mirrors the India project's http.py; BSE/NSE priming is replaced by EDGAR +
Nasdaq header helpers.
"""
from __future__ import annotations

import logging
import threading
import time

import requests

from scanner.config import load_settings

log = logging.getLogger(__name__)

# A realistic desktop-Chrome UA for public endpoints that naive-bot-block.
# We do not bypass auth or paywalls; everything we fetch is public.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class PoliteSession:
    """A requests.Session wrapper that rate-limits and retries with backoff.

    Use one instance per logical run. Reusing the session keeps TCP connections
    warm. The single throttle applies across ALL calls on the session.
    """

    def __init__(self) -> None:
        s = load_settings()
        self.delay = float(s.get("request_delay_sec", 0.13))
        self.max_retries = int(s.get("max_retries", 3))
        # SEC requires a UA with a real name + contact email (settings.edgar.user_agent).
        self.edgar_ua = (s.get("edgar", {}) or {}).get("user_agent") or "catalyst-scanner-us"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": _BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        })
        # Thread-safe global rate gate: requests are spaced `delay` apart no matter
        # how many threads call concurrently, so the aggregate stays under the SEC
        # cap while concurrency hides per-request network latency.
        self._lock = threading.Lock()
        self._next_at = 0.0

    # -- internal: enforce the configured spacing across ALL calls/threads ------
    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            target = max(now, self._next_at)   # this call's assigned slot
            self._next_at = target + self.delay
            wait = target - now
        if wait > 0:                            # sleep OUTSIDE the lock so threads overlap
            time.sleep(wait)

    def get(self, url: str, *, timeout: int = 30, headers: dict | None = None, **kwargs) -> requests.Response:
        """Throttled GET. Retries transient errors (429 / 5xx / network) with
        backoff; raises immediately on other 4xx (e.g. an expected 404 for a
        non-trading-day daily index). Raises on final failure.

        Per-request `headers` override the session defaults for that call only
        (used by edgar_get / nasdaq_get to swap the User-Agent / Accept).
        """
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=timeout, headers=headers, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc  # network error — retry
            else:
                if resp.status_code not in (429, 500, 502, 503, 504):
                    resp.raise_for_status()   # raises on other 4xx without retrying
                    return resp
                last_exc = requests.HTTPError(f"{resp.status_code} from {url}")
            backoff = self.delay * (2 ** (attempt - 1))
            log.warning("GET %s failed (attempt %d/%d): %s — backing off %.2fs",
                        url, attempt, self.max_retries, last_exc, backoff)
            if attempt < self.max_retries:
                time.sleep(backoff)
        assert last_exc is not None
        raise last_exc

    # -- SEC EDGAR: the required name+email User-Agent --------------------------
    def edgar_get(self, url: str, *, timeout: int = 30, **kwargs) -> requests.Response:
        """GET an SEC endpoint with the mandatory fair-access User-Agent header."""
        headers = {
            "User-Agent": self.edgar_ua,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json, text/plain, */*",
        }
        return self.get(url, timeout=timeout, headers=headers, **kwargs)

    # -- Nasdaq.com screener: browser-like JSON request ------------------------
    def nasdaq_get(self, url: str, *, params: dict | None = None, timeout: int = 40) -> requests.Response:
        """GET the api.nasdaq.com screener with the headers it expects.

        The screener returns 403 to plain clients; a browser UA (already the
        session default) plus a JSON Accept + nasdaq.com referer/origin is enough.
        """
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/",
        }
        return self.get(url, params=params, headers=headers, timeout=timeout)
