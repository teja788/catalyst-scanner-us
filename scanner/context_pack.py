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
import re
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
# Includes the high-signal NEGATIVE events (delisting / restatement): distress is
# also an asymmetric setup, and these are rare enough not to crowd the section.
PRIORITY_8K_ITEMS = {"1.03": "bankruptcy/distress", "2.01": "M&A completed", "5.01": "control change",
                     "3.01": "delisting notice", "4.02": "restatement/non-reliance"}

# LUCRATIVE business-catalyst tags — the genuinely-asymmetric forward catalysts.
# Deliberately EXCLUDES the broad 8-K-1.01 item tags (contract/material_agreement) and
# capital_action, which are dominated by dilutive FINANCING (ATM/notes/securities
# purchase). A 1.01 only enters this bucket if its BODY re-tags to one of these.
CATALYST_TAGS = {"contract_win", "partnership", "capacity_capex",
                 "fda_clinical", "guidance", "index_inclusion", "patent"}
_CATALYST_KWS = ("agreement", "contract", "supply", "license", "partnership", "approval",
                 "phase 3", "phase 2", "capacity", "patent", "guidance", "milestone",
                 "expansion", "facility", "purchase order")

# Forms / 8-K items that are executive-comp or governance — NOT business catalysts.
# Excluded from the lucrative bucket even if their text mentions FDA/award/phase-3
# (e.g. comp tied to an FDA-approval "performance condition", or an "equity award").
_NONCATALYST_FORMS = {"DEF 14A", "DEFA14A", "DEFR14A", "PRE 14A", "PREC14A", "DEFM14A"}
_COMP_GOV_ITEMS = {"5.02", "5.03", "5.07"}


def _is_comp_or_proxy(f: dict[str, Any]) -> bool:
    if (f.get("form_type") or "") in _NONCATALYST_FORMS:
        return True
    items = f.get("item_codes") or []
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except (ValueError, TypeError):
            items = []
    # 9.01 (exhibits) rides along on almost every 8-K — ignore it, or a plain
    # {5.02, 9.01} comp filing bypasses the filter (and its PR body can carry
    # FDA/award keywords that would fake a business catalyst).
    real = set(items) - {"9.01"}
    return bool(real) and real <= _COMP_GOV_ITEMS


# Dollar figures in filing bodies — the most objective materiality hint a filing
# offers, and one phrase-keyword tagging completely ignores. Both spellings:
# "$400 million" / "$1.2 billion" and fully-written "$36,000,000".
_MONEY_WORD_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(million|billion)\b", re.I)
_MONEY_DIGITS_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3}){2,})(?:\.\d+)?\b")


def _max_body_amount(body: str) -> float | None:
    """Largest dollar figure mentioned in the body (USD), or None."""
    best = 0.0
    for num, unit in _MONEY_WORD_RE.findall(body or ""):
        best = max(best, float(num.replace(",", "")) * (1e9 if unit.lower() == "billion" else 1e6))
    for num in _MONEY_DIGITS_RE.findall(body or ""):
        best = max(best, float(num.replace(",", "")))
    return best or None


def _amount_note(body: str, mcap: float | None) -> str:
    """'≈$36.0M mentioned in body (~5% of mcap)' — or '' when no figure found."""
    amt = _max_body_amount(body)
    if not amt:
        return ""
    note = f"≈{_fmt_usd(amt)} mentioned in body"
    if mcap:
        ratio = amt / mcap * 100
        note += f" (~{ratio:.0f}% of mcap)" if ratio >= 1 else " (<1% of mcap)"
    return note


def _catalyst_snippet(body: str) -> str:
    """Show body text around the first catalyst keyword, skipping the 8-K boilerplate header."""
    if not body:
        return ""
    low = body.lower()
    for k in _CATALYST_KWS:
        i = low.find(k)
        if i > 140:   # past the cover-page boilerplate
            return "…" + body[max(0, i - 110):i + 210].strip() + "…"
    return _short(body, 240)


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
    """Shares @ price for a buy (price omitted when it didn't parse), else the stake %."""
    if o.get("side") == "BUY" and o.get("shares"):
        return (f"{o['shares']:,.0f} sh @ ${o['price']}" if o.get("price")
                else f"{o['shares']:,.0f} sh")
    if o.get("pct") is not None:
        s = f"{o['pct']}% stake"
        # Dropping below 5% on a 13D/A is often the FINAL amendment (an exit) —
        # the rubric warns about this; flag it deterministically.
        if o.get("form_type") == "SCHEDULE 13D/A" and o["pct"] < 5:
            s += " (below 5% — possible exit)"
        return s
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
    # Multi-insider BUY clusters — several insiders buying the same name in one
    # window is the classically strong form of the signal, and easy to miss when
    # each Form 4 renders as its own line.
    _by_cik: dict[str, list[dict[str, Any]]] = {}
    for o in ownership:
        if o.get("is_buy") and o.get("cik"):
            _by_cik.setdefault(o["cik"], []).append(o)
    clusters = []
    for cik, rows in _by_cik.items():
        buyers = {r.get("filer_name") for r in rows if r.get("filer_name")}
        if len(buyers) >= 2:
            clusters.append({
                "cik": cik, "ticker": rows[0].get("ticker"), "company": rows[0].get("company"),
                "n_insiders": len(buyers),
                "usd": sum((r.get("shares") or 0) * (r.get("price") or 1) for r in rows),
            })
    clusters.sort(key=lambda c: c["usd"], reverse=True)
    return {
        "buy_clusters": clusters,
        # issuer-specific superinvestor hits (13D / Form 4 — actionable, have a ticker)
        "superinvestor": [o for o in sup_all if not (o.get("form_type") or "").startswith("13F")],
        # portfolio 13F-HR by a watchlist manager (quarterly/lagged, no single issuer)
        "superinvestor_13f": [o for o in sup_all if (o.get("form_type") or "").startswith("13F")],
        "activist": [o for o in ownership if o.get("is_activist") and not o.get("matched_investor")],
        # Order insider buys by $ value so the LARGEST buy is never lost to the cap.
        # When the price didn't parse, fall back to share count (price 1) instead of
        # zeroing the buy to the bottom of the list.
        "insider_buys": sorted(
            [o for o in ownership if o.get("is_buy") and not o.get("matched_investor")],
            key=lambda o: (o.get("shares") or 0) * (o.get("price") or 1), reverse=True),
        "corporate_events": _corporate_events(filings),
        # Business catalysts (contracts / capacity / FDA / partnerships / patents),
        # revealed by reading the filing body — the broad asymmetric-opportunity feed.
        "catalysts": [f for f in filings
                      if (set(f.get("candidate_tags") or []) & CATALYST_TAGS)
                      and not f.get("is_routine") and not _is_comp_or_proxy(f)],
    }


def build_context_pack(summary: dict[str, Any] | None = None,
                       since: datetime | None = None,
                       enrich_bodies: bool = True,
                       fetch_bodies: bool = True) -> dict[str, Any]:
    """Assemble + write the context pack. Returns paths and headline stats.

    `enrich_bodies` reads the primary document of catalyst-tagged filings (cached)
    so an 8-K "Material Agreement" reveals WHAT the contract is. `fetch_bodies=False`
    keeps the enrichment but uses only already-cached bodies — zero network (the
    dashboard does this to stay snappy while still seeing body-derived catalysts).
    """
    summary = summary or run_prefilter(since=since)
    cand = summary["candidates"]
    idx = {c["cik"]: c for c in load_map()}
    settings = load_settings()

    filings = list(cand["filings"])
    # Order: substantive catalyst-tagged first, then by recency (stable sorts).
    filings.sort(key=lambda f: f.get("filed_at") or "", reverse=True)
    filings.sort(key=lambda f: 0 if (f.get("candidate_tags") and not f.get("is_routine")) else 1)

    if enrich_bodies:
        from scanner import filing_body
        # reads bodies + re-tags on them (cache-only when fetch_bodies=False)
        filing_body.enrich(filings, max_fetch=200, fetch_missing=fetch_bodies)

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
    # External catalyst feeds (federal contracts / FDA-clinical / patents), matched to universe.
    from scanner import store as _store
    _since_date = (summary.get("window_since") or "")[:10]
    priority["external"] = _store.get_recent_external_catalysts(_since_date) if _since_date else []
    runs = _store.get_runs()   # per-source freshness for the pack header

    md = _render_md(summary, priority, filings, ownership, tagged_news, market_news, coverage, idx, runs)
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


def _render_md(summary, priority, filings, ownership, tagged_news, market_news, coverage, idx,
               runs: dict[str, dict] | None = None) -> str:
    out: list[str] = []
    _now_dt = datetime.now(_tz())
    now = _now_dt.strftime("%Y-%m-%d %H:%M ET")
    uni = load_settings().get("universe", {})
    out.append(f"# Context Pack — {now}")
    out.append(f"Window since: {_et_short(summary['window_since'])}  |  "
               f"Universe: top {uni.get('top_n')} US by mcap ({', '.join(uni.get('include_exchanges', []))})")
    out.append(f"Counts: filings {len(filings)} (substantive {summary['filings_tagged']}), "
               f"ownership flagged {summary['ownership_flagged']}, "
               f"company news {len(tagged_news)}, market news {len(market_news)}")
    # Per-source freshness, so an empty section reads as "not fetched" when that's
    # the truth — not as a quiet day.
    if runs:
        parts, stale = [], []
        for src in ("edgar", "news", "ownership", "external"):
            last = (runs.get(src) or {}).get("last_success_at")
            if not last:
                parts.append(f"{src}: never")
                stale.append(src)
                continue
            parts.append(f"{src}: {_et_short(last)}")
            try:
                age_h = (_now_dt - datetime.fromisoformat(last)).total_seconds() / 3600
                if age_h > 48:
                    stale.append(src)
            except ValueError:
                pass
        out.append("Data freshness: " + "  ·  ".join(parts))
        if stale:
            out.append(f"> ⚠ STALE: {', '.join(stale)} last refreshed >48h ago (or never) — "
                       "empty sections may mean 'not fetched', not 'quiet day'. Run `refresh` first.")
    out.append("")
    out.append("> Trust order: SEC FILING > OWNERSHIP (disclosed) > WIRE/PR > NEWS. "
               "Coverage = # news items on that ticker (LOW coverage + strong catalyst = asymmetric). "
               "Research leads only — not advice.")
    out.append("")

    # --- PRIORITY SIGNALS (read FIRST; deterministic, window-size-independent) ---
    sup, act, buys, evts = (priority["superinvestor"], priority["activist"],
                            priority["insider_buys"], priority["corporate_events"])
    out.append("## ⚡ PRIORITY SIGNALS — read first (highest-signal; any overflow is "
               "counted below, never silently lost)")
    if not (sup or act or buys or evts):
        out.append("_No priority signals in window._")

    def _overflow(total: int, cap: int, kind: str) -> None:
        if total > cap:
            out.append(f"  … {total - cap} more {kind} in window not shown — "
                       f"query the DB / dashboard for the rest.")
    for o in sup:   # superinvestor/watchlist hits (issuer-specific) — always shown in full
        out.append(f"[SUPERINVESTOR: {o.get('matched_investor')}] "
                   f"{_label(o.get('cik',''), o.get('ticker',''), o.get('company',''), idx)} — "
                   f"{o.get('form_type','')} {_own_detail(o)} — {o.get('filer_name','')} ({_et_short(o.get('filed_at'))})")
        if o.get("detail"):
            out.append(f"  → {o['detail']}")
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
        if o.get("detail"):
            out.append(f"  → {o['detail']}")
        if o.get("filing_url"):
            out.append(f"  Source: {o['filing_url']}")
    _overflow(len(act), 40, "activist 13D/13D-A rows")
    for cl in priority.get("buy_clusters", [])[:10]:   # multi-insider buying, rolled up
        out.append(f"[BUY CLUSTER] {_label(cl['cik'], cl.get('ticker', ''), cl.get('company', ''), idx)} — "
                   f"{cl['n_insiders']} insiders bought ≈${cl['usd']:,.0f} combined in window")
    _overflow(len(priority.get("buy_clusters", [])), 10, "buy clusters")
    for o in buys[:25]:
        out.append(f"[INSIDER BUY] {_label(o.get('cik',''), o.get('ticker',''), o.get('company',''), idx)} — "
                   f"{o.get('filer_name','')} {_own_detail(o)} ({_et_short(o.get('filed_at'))})")
        if o.get("filing_url"):
            out.append(f"  Source: {o['filing_url']}")
    _overflow(len(buys), 25, "insider buys (smallest by $ value)")
    for f, hits in evts[:25]:
        out.append(f"[CORP EVENT: {', '.join(hits)}] "
                   f"{_label(f.get('cik',''), f.get('ticker',''), f.get('company',''), idx)} — "
                   f"{f.get('form_type','')} ({_et_short(f.get('filed_at'))})")
        if f.get("filing_url"):
            out.append(f"  Source: {f['filing_url']}")
    _overflow(len(evts), 25, "corporate-event 8-Ks")
    for f in priority.get("catalysts", [])[:30]:   # contracts / capacity / FDA / partnerships / patents
        tags = [t for t in (f.get("candidate_tags") or []) if t in CATALYST_TAGS]
        out.append(f"[CATALYST: {', '.join(tags)}] "
                   f"{_label(f.get('cik',''), f.get('ticker',''), f.get('company',''), idx)} — "
                   f"{f.get('form_type','')} ({_et_short(f.get('filed_at'))})")
        snip = _catalyst_snippet(f.get("body_text") or "")
        if snip:
            out.append(f"  → {snip}")
        note = _amount_note(f.get("body_text") or "", _co(f.get("cik", ""), idx).get("market_cap"))
        if note:
            out.append(f"  $ {note}")
        if f.get("filing_url"):
            out.append(f"  Source: {f['filing_url']}")
    _overflow(len(priority.get("catalysts", [])), 30, "business-catalyst filings")
    # External feeds (non-EDGAR): federal contracts, FDA/clinical, patents.
    _ext = priority.get("external", [])
    if _ext:
        _bycat: dict[str, list] = {}
        for e in _ext:
            _bycat.setdefault(e.get("category", "other"), []).append(e)
        _labels = {"contract": "FEDERAL CONTRACT", "fda": "FDA / CLINICAL", "patent": "PATENT"}
        for cat in ("fda", "contract", "patent"):
            for e in _bycat.get(cat, [])[:20]:
                amt = f" ${e['amount']:,.0f}" if e.get("amount") else ""
                out.append(f"[{_labels.get(cat, cat.upper())}: {e.get('source')}] "
                           f"{_label(e.get('cik',''), e.get('ticker',''), e.get('company',''), idx)}{amt} — "
                           f"{e.get('headline','')} ({(e.get('event_date') or '')[:10]})")
                if e.get("detail"):
                    out.append(f"  → {_short(e['detail'], 200)}")
                if e.get("url"):
                    out.append(f"  Source: {e['url']}")
            _overflow(len(_bycat.get(cat, [])), 20, f"{_labels.get(cat, cat)} items")
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
        note = _amount_note(f.get("body_text") or "", _co(cik, idx).get("market_cap"))
        notestr = f"  | {note}" if note else ""
        out.append(f"[SEC FILING] {_label(cik, f.get('ticker',''), f.get('company',''), idx)} — "
                   f"{f.get('form_type','')} — {_et_short(f.get('filed_at'))}{routine}")
        out.append(f"  {f.get('headline','')}{tagstr}{notestr}  | coverage: {cov}")
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
        d = _own_detail(o)
        detail = f" {d}" if d else ""
        out.append(f"[OWNERSHIP] {_label(cik, o.get('ticker',''), o.get('company',''), idx)} — "
                   f"{o.get('form_type','')} [{', '.join(flags)}]")
        out.append(f"  {o.get('filer_name','?')} ({o.get('relationship') or o.get('side','')}){detail}  "
                   f"({_et_short(o.get('filed_at'))})")
        if o.get("filing_url"):
            out.append(f"  Source: {o['filing_url']}")
        out.append("")

    # --- COMPANY NEWS (wires first, then news; routine/noise items sort last) ---
    out.append("## NEWS (company-tagged — wires medium trust, news lower)")
    if not tagged_news:
        out.append("_None in window._")
    tagged_news = sorted(tagged_news, key=lambda n: (0 if n.get("trust") == "wire" else 1,
                                                     1 if n.get("is_noise") else 0))
    for n in tagged_news:
        syms = ", ".join(_co(c, idx).get("ticker", "?") for c in n.get("company_ciks", [])) or "?"
        tags = n.get("candidate_tags") or []
        tagstr = f"  | tags: [{', '.join(tags)}]" if tags else ""
        kind = "WIRE" if n.get("trust") == "wire" else "NEWS"
        noise = " [routine/noise]" if n.get("is_noise") else ""
        out.append(f"[{kind}] {syms} — {n.get('source','')} — {_et_short(n.get('published_at'))}{tagstr}{noise}")
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
        "is_activist": bool(o.get("is_activist")), "detail": o.get("detail"),
        "filed_at": o.get("filed_at"), "source": o.get("filing_url"),
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
            "buy_clusters": priority.get("buy_clusters", [])[:20],
            "insider_buys": [_own_json(o, idx) for o in priority["insider_buys"][:40]],
            "corporate_events": [{
                "ticker": f.get("ticker"), "company": f.get("company"), "cik": f.get("cik"),
                "market_cap": _co(f.get("cik", ""), idx).get("market_cap"),
                "form_type": f.get("form_type"), "events": hits,
                "filed_at": f.get("filed_at"), "source": f.get("filing_url"),
            } for f, hits in priority["corporate_events"][:60]],
            "catalysts": [{
                "ticker": f.get("ticker"), "company": f.get("company"), "cik": f.get("cik"),
                "market_cap": _co(f.get("cik", ""), idx).get("market_cap"),
                "form_type": f.get("form_type"),
                "tags": [t for t in (f.get("candidate_tags") or []) if t in CATALYST_TAGS],
                "snippet": _catalyst_snippet(f.get("body_text") or ""),
                "body_amount": _max_body_amount(f.get("body_text") or ""),
                "filed_at": f.get("filed_at"), "source": f.get("filing_url"),
            } for f in priority["catalysts"][:60]],
            "external": [{
                "ticker": e.get("ticker"), "company": e.get("company"), "cik": e.get("cik"),
                "source": e.get("source"), "category": e.get("category"),
                "headline": e.get("headline"), "detail": e.get("detail"), "amount": e.get("amount"),
                "event_date": e.get("event_date"), "source_url": e.get("url"),
            } for e in priority.get("external", [])[:80]],
        },
        "sec_filings": [{
            "ticker": f.get("ticker"), "company": f.get("company"), "cik": f.get("cik"),
            "market_cap": _co(f.get("cik", ""), idx).get("market_cap"),
            "form_type": f.get("form_type"), "item_codes": f.get("item_codes") or [],
            "candidate_tags": f.get("candidate_tags") or [], "is_routine": bool(f.get("is_routine")),
            "body_amount": _max_body_amount(f.get("body_text") or ""),
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
            "candidate_tags": n.get("candidate_tags") or [], "is_noise": bool(n.get("is_noise")),
            "headline": n.get("headline"), "url": n.get("url"),
        } for n in tagged_news],
    }
