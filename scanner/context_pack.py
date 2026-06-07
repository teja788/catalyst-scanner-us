"""Context-pack assembler (Milestone 8).

Turns the pre-filtered candidate set into a compact, token-efficient packet the
reasoning agent reads: runtime/context_pack.md (+ a .json mirror).

Hard separation of trust is structural (US order):
  - SEC FILINGS  -> highest trust (EDGAR, with 8-K item codes)
  - OWNERSHIP    -> disclosed 13D/13G/Form-4 (insider buys, activist, superinvestor)
  - NEWS         -> wires (medium) then financial news (lower), company-tagged
  - MARKET-WIDE  -> untagged context, titles only

Each item keeps its source link. Market cap feeds the materiality-relative-to-size
judgement; a per-ticker COVERAGE count feeds the attention-as-inverse-signal idea
(strong catalyst + low coverage = the asymmetric sweet spot).
"""
from __future__ import annotations

import collections
import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from scanner.config import load_settings, resolve_path
from scanner.prefilter import run_prefilter
from scanner.universe import load_map

log = logging.getLogger(__name__)

MAX_MARKET_NEWS = 25
MAX_NOTE = 240
MAX_FILINGS = 150          # cap the verbose filings list so the pack stays readable at 30-day / 5,000 scale

# 8-K item codes that are concrete corporate events — always surfaced as PRIORITY.
PRIORITY_8K_ITEMS = {"1.03": "bankruptcy/distress", "2.01": "M&A completed", "5.01": "control change"}


def _tz() -> ZoneInfo:
    return ZoneInfo(load_settings().get("timezone", "America/New_York"))


def _et_short(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        return datetime.fromisoformat(iso).astimezone(_tz()).strftime("%Y-%m-%d %H:%M ET")
    except ValueError:
        return iso[:16]


def _fmt_usd(v: float | None) -> str:
    if not v:
        return "?"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if v >= div:
            return f"${v / div:.1f}{unit}"
    return f"${v:,.0f}"


def _short(text: str, n: int = MAX_NOTE) -> str:
    text = (text or "").strip().replace("\r", " ").replace("\n", " ")
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _co(cik: str, idx: dict[str, dict]) -> dict[str, Any]:
    return idx.get(cik, {})


def _label(cik: str, ticker: str, company: str, idx: dict[str, dict]) -> str:
    """TICKER (Company, $mcap) — market cap drives materiality-relative-to-size."""
    meta = _co(cik, idx)
    tk = ticker or meta.get("ticker") or "?"
    name = company or meta.get("name") or ""
    mc = _fmt_usd(meta.get("market_cap"))
    return f"{tk} ({name}, {mc})"


def _own_detail(o: dict[str, Any]) -> str:
    """Shares @ price for a buy, else the stake %."""
    if o.get("side") == "BUY" and o.get("shares"):
        return f"{o['shares']:,.0f} sh @ ${o.get('price')}"
    if o.get("pct") is not None:
        return f"{o['pct']}% stake"
    return ""


def _corporate_events(filings: list[dict[str, Any]]) -> list[tuple[dict[str, Any], list[str]]]:
    """8-Ks that are concrete corporate events (M&A completed / distress / control change)."""
    out: list[tuple[dict[str, Any], list[str]]] = []
    for f in filings:
        if not (f.get("form_type") or "").startswith("8-K"):
            continue
        hits = [PRIORITY_8K_ITEMS[c] for c in (f.get("item_codes") or []) if c in PRIORITY_8K_ITEMS]
        if hits:
            out.append((f, hits))
    return out


def _build_priority(filings: list[dict[str, Any]], ownership: list[dict[str, Any]]) -> dict[str, list]:
    """The deterministic high-signal set, surfaced at the TOP of the pack regardless
    of window size so it can never be lost in a large filing list (the 30-day lesson).

    `activist` includes BOTH new SCHEDULE 13D and 13D/A amendments — amendments are
    where activists ESCALATE, so excluding them (as an earlier ad-hoc query did)
    silently drops top signals like GameStop→eBay or Pershing Square→QSR.
    """
    sup_all = [o for o in ownership if o.get("matched_investor")]
    return {
        # issuer-specific superinvestor hits (13D / Form 4 — actionable, have a ticker)
        "superinvestor": [o for o in sup_all if not (o.get("form_type") or "").startswith("13F")],
        # portfolio 13F-HR by a watchlist manager (quarterly/lagged, no single issuer)
        "superinvestor_13f": [o for o in sup_all if (o.get("form_type") or "").startswith("13F")],
        "activist": [o for o in ownership if o.get("is_activist") and not o.get("matched_investor")],
        # Order insider buys by $ value so the LARGEST buy is never lost to the cap.
        "insider_buys": sorted(
            [o for o in ownership if o.get("is_buy") and not o.get("matched_investor")],
            key=lambda o: (o.get("shares") or 0) * (o.get("price") or 0), reverse=True),
        "corporate_events": _corporate_events(filings),
    }


def build_context_pack(summary: dict[str, Any] | None = None,
                       since: datetime | None = None) -> dict[str, Any]:
    """Assemble + write the context pack. Returns paths and headline stats."""
    summary = summary or run_prefilter(since=since)
    cand = summary["candidates"]
    idx = {c["cik"]: c for c in load_map()}
    settings = load_settings()

    filings = list(cand["filings"])
    # Order: substantive catalyst-tagged first, then by recency (stable sorts).
    filings.sort(key=lambda f: f.get("filed_at") or "", reverse=True)
    filings.sort(key=lambda f: 0 if (f.get("candidate_tags") and not f.get("is_routine")) else 1)

    news = cand["news"]
    tagged_news = [n for n in news if n.get("company_ciks")]
    market_news = [n for n in news if not n.get("company_ciks")]

    # Coverage = how many company-tagged news items mention each CIK (attention proxy).
    coverage: collections.Counter = collections.Counter()
    for n in tagged_news:
        for c in n.get("company_ciks", []):
            coverage[c] += 1

    # Ownership: high-signal first (superinvestor / activist / insider buys), then the rest.
    ownership = list(cand["ownership"])
    ownership.sort(key=lambda o: 0 if (o.get("matched_investor") or o.get("is_activist") or o.get("is_buy")) else 1)

    # PRIORITY signals — surfaced at the top of the pack, window-size-independent.
    priority = _build_priority(filings, ownership)

    md = _render_md(summary, priority, filings, ownership, tagged_news, market_news, coverage, idx)
    pack_json = _render_json(summary, priority, filings, ownership, tagged_news, coverage, idx)

    md_path = resolve_path(settings.get("output", {}).get("context_pack", "runtime/context_pack.md"))
    json_path = md_path.with_suffix(".json")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(json.dumps(pack_json, indent=2, ensure_ascii=False), encoding="utf-8")

    stats = {
        "md_path": str(md_path),
        "json_path": str(json_path),
        "filings": len(filings),
        "filings_substantive": sum(1 for f in filings if f.get("candidate_tags") and not f.get("is_routine")),
        "ownership_flagged": summary["ownership_flagged"],
        "company_news": len(tagged_news),
        "market_news": len(market_news),
        "priority": {k: len(v) for k, v in priority.items()},
    }
    log.info("Context pack written: %s", stats)
    return stats


def _render_md(summary, priority, filings, ownership, tagged_news, market_news, coverage, idx) -> str:
    out: list[str] = []
    now = datetime.now(_tz()).strftime("%Y-%m-%d %H:%M ET")
    uni = load_settings().get("universe", {})
    out.append(f"# Context Pack — {now}")
    out.append(f"Window since: {_et_short(summary['window_since'])}  |  "
               f"Universe: top {uni.get('top_n')} US by mcap ({', '.join(uni.get('include_exchanges', []))})")
    out.append(f"Counts: filings {len(filings)} (substantive {summary['filings_tagged']}), "
               f"ownership flagged {summary['ownership_flagged']}, "
               f"company news {len(tagged_news)}, market news {len(market_news)}")
    out.append("")
    out.append("> Trust order: SEC FILING > OWNERSHIP (disclosed) > WIRE/PR > NEWS. "
               "Coverage = # news items on that ticker (LOW coverage + strong catalyst = asymmetric). "
               "Research leads only — not advice.")
    out.append("")

    # --- PRIORITY SIGNALS (read FIRST; deterministic, window-size-independent) ---
    sup, act, buys, evts = (priority["superinvestor"], priority["activist"],
                            priority["insider_buys"], priority["corporate_events"])
    out.append("## ⚡ PRIORITY SIGNALS — read first (highest-signal, never truncated)")
    if not (sup or act or buys or evts):
        out.append("_No priority signals in window._")
    for o in sup:   # superinvestor/watchlist hits (issuer-specific) — always shown in full
        out.append(f"[SUPERINVESTOR: {o.get('matched_investor')}] "
                   f"{_label(o.get('cik',''), o.get('ticker',''), o.get('company',''), idx)} — "
                   f"{o.get('form_type','')} {_own_detail(o)} — {o.get('filer_name','')} ({_et_short(o.get('filed_at'))})")
        if o.get("filing_url"):
            out.append(f"  Source: {o['filing_url']}")
    if priority.get("superinvestor_13f"):
        names = sorted({o.get("matched_investor") for o in priority["superinvestor_13f"] if o.get("matched_investor")})
        out.append(f"[SUPERINVESTOR 13F-HR — quarterly/lagged] filed recently: {', '.join(names)} "
                   f"(portfolio holdings not parsed — dig the filing)")
    for o in act[:40]:   # activist 13D AND 13D/A (amendments included)
        out.append(f"[ACTIVIST {o.get('form_type','')}] "
                   f"{_label(o.get('cik',''), o.get('ticker',''), o.get('company',''), idx)} {_own_detail(o)} — "
                   f"{o.get('filer_name','')} ({_et_short(o.get('filed_at'))})")
        if o.get("filing_url"):
            out.append(f"  Source: {o['filing_url']}")
    for o in buys[:25]:
        out.append(f"[INSIDER BUY] {_label(o.get('cik',''), o.get('ticker',''), o.get('company',''), idx)} — "
                   f"{o.get('filer_name','')} {_own_detail(o)} ({_et_short(o.get('filed_at'))})")
        if o.get("filing_url"):
            out.append(f"  Source: {o['filing_url']}")
    for f, hits in evts[:25]:
        out.append(f"[CORP EVENT: {', '.join(hits)}] "
                   f"{_label(f.get('cik',''), f.get('ticker',''), f.get('company',''), idx)} — "
                   f"{f.get('form_type','')} ({_et_short(f.get('filed_at'))})")
        if f.get("filing_url"):
            out.append(f"  Source: {f['filing_url']}")
    out.append("")
    out.append("> NOTE: a 13D/A shows the CURRENT %, not whether the investor ADDED or TRIMMED — "
               "verify direction in the filing. Treat 13D and 13D/A equally as activist signals.")
    out.append("")

    # --- SEC FILINGS (verbose; capped so the pack stays readable) ---
    out.append("## SEC FILINGS (high trust — EDGAR)")
    if not filings:
        out.append("_None in window._")
    for f in filings[:MAX_FILINGS]:
        cik = f.get("cik", "")
        tags = f.get("candidate_tags") or []
        routine = " [routine]" if f.get("is_routine") else ""
        tagstr = f"  | tags: [{', '.join(tags)}]" if tags else ""
        cov = coverage.get(cik, 0)
        out.append(f"[SEC FILING] {_label(cik, f.get('ticker',''), f.get('company',''), idx)} — "
                   f"{f.get('form_type','')} — {_et_short(f.get('filed_at'))}{routine}")
        out.append(f"  {f.get('headline','')}{tagstr}  | coverage: {cov}")
        if f.get("filing_url"):
            out.append(f"  Source: {f['filing_url']}")
        out.append("")
    if len(filings) > MAX_FILINGS:
        out.append(f"_… {len(filings) - MAX_FILINGS} more filings not shown (priority signals above are complete; "
                   f"query the DB / dashboard for the full list)._")
        out.append("")

    # --- OWNERSHIP ---
    out.append("## OWNERSHIP / INSIDER (disclosed — 13D/13G/Form 4)")
    flagged = [o for o in ownership if o.get("is_buy") or o.get("is_activist") or o.get("matched_investor")]
    if not flagged:
        out.append("_None flagged in window._")
    for o in flagged:
        cik = o.get("cik", "")
        flags = []
        if o.get("matched_investor"):
            flags.append(f"SUPERINVESTOR:{o['matched_investor']}")
        if o.get("is_activist"):
            flags.append("ACTIVIST-13D")
        if o.get("is_buy"):
            flags.append("INSIDER-BUY")
        detail = ""
        if o.get("side") == "BUY" and o.get("shares"):
            detail = f" {o['shares']:,.0f} sh @ ${o.get('price')}"
        elif o.get("pct") is not None:
            detail = f" {o['pct']}% stake"
        out.append(f"[OWNERSHIP] {_label(cik, o.get('ticker',''), o.get('company',''), idx)} — "
                   f"{o.get('form_type','')} [{', '.join(flags)}]")
        out.append(f"  {o.get('filer_name','?')} ({o.get('relationship') or o.get('side','')}){detail}  "
                   f"({_et_short(o.get('filed_at'))})")
        if o.get("filing_url"):
            out.append(f"  Source: {o['filing_url']}")
        out.append("")

    # --- COMPANY NEWS (wires first, then news) ---
    out.append("## NEWS (company-tagged — wires medium trust, news lower)")
    if not tagged_news:
        out.append("_None in window._")
    tagged_news = sorted(tagged_news, key=lambda n: 0 if n.get("trust") == "wire" else 1)
    for n in tagged_news:
        syms = ", ".join(_co(c, idx).get("ticker", "?") for c in n.get("company_ciks", [])) or "?"
        tags = n.get("candidate_tags") or []
        tagstr = f"  | tags: [{', '.join(tags)}]" if tags else ""
        kind = "WIRE" if n.get("trust") == "wire" else "NEWS"
        out.append(f"[{kind}] {syms} — {n.get('source','')} — {_et_short(n.get('published_at'))}{tagstr}")
        out.append(f"  {_short(n.get('headline',''))}")
        if n.get("url"):
            out.append(f"  Source: {n['url']}")
        out.append("")

    # --- MARKET-WIDE (titles only, capped) ---
    if market_news:
        out.append(f"## MARKET-WIDE NEWS (untagged context — {min(len(market_news), MAX_MARKET_NEWS)} of {len(market_news)})")
        for n in market_news[:MAX_MARKET_NEWS]:
            out.append(f"- [{n.get('source','')}] {_short(n.get('headline',''), 110)}")
        out.append("")

    return "\n".join(out)


def _own_json(o: dict[str, Any], idx: dict[str, dict]) -> dict[str, Any]:
    return {
        "ticker": o.get("ticker"), "company": o.get("company"), "cik": o.get("cik"),
        "market_cap": _co(o.get("cik", ""), idx).get("market_cap"),
        "form_type": o.get("form_type"), "filer": o.get("filer_name"),
        "side": o.get("side"), "shares": o.get("shares"), "price": o.get("price"), "pct": o.get("pct"),
        "matched_investor": o.get("matched_investor"), "is_buy": bool(o.get("is_buy")),
        "is_activist": bool(o.get("is_activist")), "filed_at": o.get("filed_at"), "source": o.get("filing_url"),
    }


def _render_json(summary, priority, filings, ownership, tagged_news, coverage, idx) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(_tz()).isoformat(),
        "window_since": summary["window_since"],
        "stats": {
            "filings": len(filings),
            "ownership_flagged": summary["ownership_flagged"],
            "company_news": len(tagged_news),
        },
        "priority": {
            "superinvestor": [_own_json(o, idx) for o in priority["superinvestor"]],
            "superinvestor_13f": sorted({o.get("matched_investor") for o in priority["superinvestor_13f"] if o.get("matched_investor")}),
            "activist": [_own_json(o, idx) for o in priority["activist"][:60]],
            "insider_buys": [_own_json(o, idx) for o in priority["insider_buys"][:40]],
            "corporate_events": [{
                "ticker": f.get("ticker"), "company": f.get("company"), "cik": f.get("cik"),
                "market_cap": _co(f.get("cik", ""), idx).get("market_cap"),
                "form_type": f.get("form_type"), "events": hits,
                "filed_at": f.get("filed_at"), "source": f.get("filing_url"),
            } for f, hits in priority["corporate_events"][:60]],
        },
        "sec_filings": [{
            "ticker": f.get("ticker"), "company": f.get("company"), "cik": f.get("cik"),
            "market_cap": _co(f.get("cik", ""), idx).get("market_cap"),
            "form_type": f.get("form_type"), "item_codes": f.get("item_codes") or [],
            "candidate_tags": f.get("candidate_tags") or [], "is_routine": bool(f.get("is_routine")),
            "headline": f.get("headline"), "filed_at": f.get("filed_at"),
            "coverage": coverage.get(f.get("cik", ""), 0), "source": f.get("filing_url"),
        } for f in filings],
        "ownership": [{
            "ticker": o.get("ticker"), "company": o.get("company"), "cik": o.get("cik"),
            "form_type": o.get("form_type"), "filer": o.get("filer_name"),
            "side": o.get("side"), "shares": o.get("shares"), "price": o.get("price"), "pct": o.get("pct"),
            "matched_investor": o.get("matched_investor"), "is_buy": bool(o.get("is_buy")),
            "is_activist": bool(o.get("is_activist")), "filed_at": o.get("filed_at"), "source": o.get("filing_url"),
        } for o in ownership if o.get("is_buy") or o.get("is_activist") or o.get("matched_investor")],
        "company_news": [{
            "tickers": [_co(c, idx).get("ticker", "?") for c in n.get("company_ciks", [])],
            "source": n.get("source"), "trust": n.get("trust"), "published_at": n.get("published_at"),
            "candidate_tags": n.get("candidate_tags") or [], "headline": n.get("headline"), "url": n.get("url"),
        } for n in tagged_news],
    }
