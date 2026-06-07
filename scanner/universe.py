"""Build the top-N US-listed-by-market-cap universe + ticker -> SEC CIK map.

Pipeline (Section 4 of the build spec, scaled to top-N across exchanges):
  1. Download Nasdaq Trader nasdaqlisted.txt + otherlisted.txt — the authoritative
     listed-symbol set, with ETF / Test Issue flags. Keep COMMON stocks on the
     configured exchanges (nasdaq/nyse/amex); drop ETFs, test issues,
     units/warrants/rights/preferred, and (optionally) SPAC shells.
  2. Download SEC company_tickers.json -> ticker -> CIK (10-digit, zero-padded).
     Requiring a CIK keeps only names that actually file with SEC — the point of
     an EDGAR-driven scanner.
  3. Download market caps from the Nasdaq.com screener (one bulk call per
     exchange) for RANKING. yfinance is the per-symbol fallback if it bot-blocks.
  4. Join on a separator-stripped symbol (SEC "BRK-B" / screener "BRK/B" /
     Trader "BRK.B" -> "BRKB"), dedupe by CIK, sort by market cap desc, take N.
  5. Generate a small alias table per company for news tagging (M4).
  6. Persist raw caches + the merged map under data/universe/.

The merged map is the universe everything downstream ingests/filters.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from pathlib import Path
from typing import Any

from scanner.config import load_settings, load_sources, resolve_path
from scanner.http import PoliteSession

log = logging.getLogger(__name__)

UNIVERSE_DIR = resolve_path("data/universe")

# Corporate suffix TOKENS stripped from the END of a name to make a short alias.
# We deliberately keep interior words ("Bank of America" stays whole).
_CORP_SUFFIX_TOKENS = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd",
    "limited", "plc", "holdings", "holding", "group", "sa", "nv", "ag", "lp",
    "llc", "the", "class",
}
# Also strip trailing connectors so "Deere & Company" -> "deere" (not "deere &").
_TRAILING_STRIP = _CORP_SUFFIX_TOKENS | {"&", "and"}
_PUNCT = re.compile(r"[^\w\s&]")

# Trailing security-type descriptor stripped to recover the company name, e.g.
# "Apple Inc. Common Stock" -> "Apple Inc."; "Alphabet Inc. Class A Common Stock"
# -> "Alphabet Inc.".
_SEC_TAIL = re.compile(
    r"\s*[-,]?\s*(?:class\s+[a-z]\s+)?"
    r"(?:common stock|common shares|ordinary shares|"
    r"american depositary shares|depositary shares|class\s+[a-z])\b.*$",
    re.I,
)


def _norm(symbol: str) -> str:
    """Canonical join key: uppercase, strip class separators ('.', '-', '/')."""
    return re.sub(r"[.\-/ ]", "", (symbol or "").strip().upper())


def _clean_name(name: str) -> str:
    out = _SEC_TAIL.sub("", name or "").strip(" -,")
    return out or (name or "")


def _parse_money(val: Any) -> float | None:
    if val in (None, "", "NA", "N/A", "0", "0.00"):
        return None
    try:
        return float(str(val).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Alias generation (for tagging news headlines to companies in Milestone 4)
# --------------------------------------------------------------------------- #
def make_aliases(company: str, symbol: str) -> list[str]:
    """Produce a few distinct lower-cased aliases for news matching."""
    aliases: set[str] = set()
    if company:
        cleaned = _PUNCT.sub(" ", company.lower())
        tokens = re.sub(r"\s+", " ", cleaned).strip().split()
        while tokens and tokens[-1] in _TRAILING_STRIP:
            tokens.pop()
        core = " ".join(tokens)
        if len(core) >= 3:
            aliases.add(core)
    if symbol:
        aliases.add(symbol.lower())
    return sorted(a for a in aliases if len(a) >= 3)


# --------------------------------------------------------------------------- #
# 1. Listed-symbol set (Nasdaq Trader) — authoritative, with ETF/test flags.
# --------------------------------------------------------------------------- #
def _parse_pipe(text: str) -> list[dict[str, str]]:
    """Parse a pipe-delimited Nasdaq Trader file, skipping the footer line."""
    lines = [ln for ln in text.splitlines()
             if ln.strip() and not ln.startswith("File Creation Time")]
    return list(csv.DictReader(io.StringIO("\n".join(lines)), delimiter="|"))


def _keep_name(name: str, cfg: dict[str, Any]) -> bool:
    """Positive-select COMMON stocks (and ADRs if configured); veto SPAC shells."""
    nl = (name or "").lower()
    if cfg.get("exclude_spacs", True) and ("acquisition corp" in nl or "blank check" in nl):
        return False
    for pat in (cfg.get("drop_security_types") or []):
        if pat.lower() in nl:
            return False
    if "common stock" in nl or "common shares" in nl or "ordinary share" in nl:
        return True
    if cfg.get("include_adrs", True) and "depositary" in nl and "preferred" not in nl:
        return True
    return False


def fetch_listed(session: PoliteSession) -> list[dict[str, str]]:
    """Return common-stock {symbol, name, exchange} on the configured exchanges."""
    cfg = load_settings().get("universe", {}) or {}
    include = set(cfg.get("include_exchanges", ["nasdaq", "nyse", "amex"]))
    src = load_sources().get("nasdaq_trader", {})
    out: list[dict[str, str]] = []

    if "nasdaq" in include:
        text = session.get(src["nasdaqlisted"], timeout=60).text
        for r in _parse_pipe(text):
            if r.get("Test Issue") == "Y" or r.get("ETF") == "Y":
                continue
            sym, name = (r.get("Symbol") or "").strip(), (r.get("Security Name") or "").strip()
            if sym and name and _keep_name(name, cfg):
                out.append({"symbol": sym, "name": name, "exchange": "nasdaq"})

    if include & {"nyse", "amex"}:
        text = session.get(src["otherlisted"], timeout=60).text
        exmap = {"A": "amex", "N": "nyse"}   # A=NYSE American, N=NYSE (P/Z/V skipped)
        for r in _parse_pipe(text):
            if r.get("Test Issue") == "Y" or r.get("ETF") == "Y":
                continue
            ex = exmap.get((r.get("Exchange") or "").strip())
            if not ex or ex not in include:
                continue
            sym, name = (r.get("ACT Symbol") or "").strip(), (r.get("Security Name") or "").strip()
            if sym and name and _keep_name(name, cfg):
                out.append({"symbol": sym, "name": name, "exchange": ex})

    log.info("Nasdaq Trader: %d common stocks after filtering (%s)", len(out), ", ".join(sorted(include)))
    return out


# --------------------------------------------------------------------------- #
# 2. ticker -> CIK (SEC company_tickers.json)
# --------------------------------------------------------------------------- #
def fetch_cik_map(session: PoliteSession) -> tuple[dict[str, str], dict[str, str], Any]:
    """Return ({norm_ticker: cik10}, {norm_ticker: title}, raw_json)."""
    url = load_sources().get("edgar", {}).get("company_tickers")
    data = session.edgar_get(url, timeout=60).json()
    rows = data.values() if isinstance(data, dict) else data
    cik: dict[str, str] = {}
    title: dict[str, str] = {}
    for row in rows:
        t = str(row.get("ticker", "")).strip()
        if not t:
            continue
        k = _norm(t)
        cik.setdefault(k, str(row.get("cik_str")).zfill(10))
        title.setdefault(k, (row.get("title") or "").strip())
    log.info("SEC company_tickers: %d ticker->CIK entries", len(cik))
    return cik, title, data


# --------------------------------------------------------------------------- #
# 3. market caps for ranking (Nasdaq.com screener, bulk per exchange)
# --------------------------------------------------------------------------- #
def fetch_mcaps(session: PoliteSession) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Return ({norm_symbol: {market_cap, sector, country, name}}, {exchange: rowcount}).

    Per-exchange isolation: one exchange failing still returns the others.
    """
    cfg = load_settings().get("universe", {}) or {}
    include = [e for e in ("nasdaq", "nyse", "amex") if e in set(cfg.get("include_exchanges", []))]
    base = load_sources().get("nasdaq_screener", {}).get("stocks_api")
    out: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for ex in include:
        try:
            resp = session.nasdaq_get(base, params={"exchange": ex})
            rows = ((resp.json() or {}).get("data") or {}).get("rows") or []
            counts[ex] = len(rows)
            for r in rows:
                key = _norm(r.get("symbol", ""))
                if not key:
                    continue
                out.setdefault(key, {
                    "market_cap": _parse_money(r.get("marketCap")),
                    "sector": (r.get("sector") or "").strip(),
                    "country": (r.get("country") or "").strip(),
                    "name": (r.get("name") or "").strip(),
                })
        except Exception as exc:  # noqa: BLE001 - per-exchange isolation
            counts[ex] = -1
            log.warning("Nasdaq screener failed for exchange=%s: %s", ex, exc)
    log.info("Nasdaq screener market caps: %d symbols (%s)", len(out), counts)
    return out, counts


# --------------------------------------------------------------------------- #
# 4-6. Join, rank, persist
# --------------------------------------------------------------------------- #
def build_map(session: PoliteSession | None = None) -> dict[str, Any]:
    """Fetch all sources, join on symbol, rank by mcap, take top-N, persist."""
    session = session or PoliteSession()
    UNIVERSE_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_settings().get("universe", {}) or {}
    top_n = int(cfg.get("top_n", 500))

    listed = fetch_listed(session)
    cik_map, title_map, cik_raw = fetch_cik_map(session)
    mcaps, mcap_counts = fetch_mcaps(session)

    (UNIVERSE_DIR / "cik_map_raw.json").write_text(
        json.dumps(cik_raw, ensure_ascii=False), encoding="utf-8")

    by_cik: dict[str, dict[str, Any]] = {}
    no_cik = 0
    for it in listed:
        key = _norm(it["symbol"])
        c = cik_map.get(key)
        if not c:
            no_cik += 1
            continue
        mc = mcaps.get(key, {})
        cap = mc.get("market_cap")
        name = _clean_name(it["name"]) or title_map.get(key, it["name"])
        rec = {
            "ticker": it["symbol"],
            "cik": c,
            "name": name,
            "exchange": it["exchange"],
            "sector": mc.get("sector", ""),
            "country": mc.get("country", ""),
            "market_cap": cap,
            "aliases": make_aliases(name, it["symbol"]),
        }
        # Dedupe by CIK (e.g. GOOG/GOOGL share a CIK): keep the higher-mcap class.
        prev = by_cik.get(c)
        if prev is None or (cap or 0) > (prev.get("market_cap") or 0):
            by_cik[c] = rec

    merged = list(by_cik.values())
    merged.sort(key=lambda r: (r.get("market_cap") or 0.0), reverse=True)
    top = merged[:top_n]

    (UNIVERSE_DIR / "us_universe.json").write_text(
        json.dumps(top, indent=2, ensure_ascii=False), encoding="utf-8")
    _write_csv(UNIVERSE_DIR / "us_universe.csv", top)

    stats = {
        "listed_common": len(listed),
        "no_cik_dropped": no_cik,
        "unique_companies": len(by_cik),
        "screener_rows": mcap_counts,
        "universe_size": len(top),
        "missing_mcap_in_universe": sum(1 for r in top if not r.get("market_cap")),
        "mcap_top": top[0].get("market_cap") if top else None,
        "mcap_floor": top[-1].get("market_cap") if top else None,
        "exchange_breakdown": _breakdown(top),
        "out_dir": str(UNIVERSE_DIR),
    }
    log.info("Universe built: %s", stats)
    return stats


def _breakdown(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r.get("exchange", "?")] = out.get(r.get("exchange", "?"), 0) + 1
    return out


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    cols = ["ticker", "cik", "name", "exchange", "sector", "country", "market_cap"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])


def load_map() -> list[dict[str, Any]]:
    """Read the previously-built universe map (used by downstream ingesters)."""
    path = UNIVERSE_DIR / "us_universe.json"
    if not path.exists():
        raise FileNotFoundError("Universe map not found. Run `setup-universe` first.")
    return json.loads(path.read_text(encoding="utf-8"))
