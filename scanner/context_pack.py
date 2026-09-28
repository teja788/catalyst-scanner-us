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
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from scanner.config import load_settings, resolve_path
from scanner.prefilter import run_prefilter
from scanner.universe import load_map, load_revenues

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
_NONCATALYST_FORMS = {"DEF 14A", "DEFA14A", "DEFR14A", "PRE 14A", "PREC14A", "DEFM14A",
                      # periodic reports recite the whole year's agreements — not a
                      # new forward event (a 20-F's 2023 financing topped the bucket)
                      "10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A"}
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
# offers, and one phrase-keyword tagging completely ignores. Three spellings:
# "$400 million" / "$1.2 billion", abbreviated "$400M" / "$1.2B" / "$5bn" (the
# press-release norm), and fully-written "$36,000,000".
_MONEY_WORD_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(million|billion)\b", re.I)
_MONEY_ABBR_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s?(mm|m|bn|b|k)(?![a-z0-9])", re.I)
_MONEY_DIGITS_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3}){2,})(?:\.\d+)?\b")
_ABBR_MULT = {"k": 1e3, "m": 1e6, "mm": 1e6, "b": 1e9, "bn": 1e9}


def _max_body_amount(body: str) -> float | None:
    """Largest dollar figure mentioned in the body (USD), or None."""
    best = 0.0
    for num, unit in _MONEY_WORD_RE.findall(body or ""):
        best = max(best, float(num.replace(",", "")) * (1e9 if unit.lower() == "billion" else 1e6))
    for num, unit in _MONEY_ABBR_RE.findall(body or ""):
        best = max(best, float(num.replace(",", "")) * _ABBR_MULT[unit.lower()])
    for num in _MONEY_DIGITS_RE.findall(body or ""):
        best = max(best, float(num.replace(",", "")))
    return best or None


def _event_text(body: str, filed_at: str = "") -> str:
    """The part of a filing body that describes the NEW event: from the first
    "Item X.YY" heading (past the 8-K cover page), minus sentences that recite
    history — any sentence naming an earlier year or saying "previously disclosed".
    (Seen live: CRML's 20-F "$125M" was a 2023 GEM agreement; KYNB's "$220M" was an
    old closing.) Still a hint: the rubric's date check stands."""
    if not body:
        return ""
    m = re.search(r"item\s+\d\.\d{2}", body, re.I)
    start = m.end() if m else 0
    year = int(filed_at[:4]) if filed_at[:4].isdigit() else None
    keep = []
    for sent in re.split(r"(?<=[.;])\s+", body[start:start + 4000]):
        if re.search(r"previously (?:disclosed|reported|announced)", sent, re.I):
            continue
        if year and any(int(y) < year for y in re.findall(r"\b(?:19|20)\d{2}\b", sent)):
            continue
        keep.append(sent)
    return " ".join(keep)


def _pct(x: float) -> str:
    return f"~{x * 100:,.0f}%" if x >= 0.01 else "<1%"


def _materiality(f: dict[str, Any], idx: dict[str, dict], rev: dict[str, float]) -> tuple[float, str]:
    """(ratio, note) for the largest $ figure in the filing's EVENT text. The ratio
    is vs annual REVENUE when known (>= $10M) — a contract is judged against sales —
    else vs market cap. Note: '≈$36.0M in event text (~12% of annual revenue, ~5% of mcap)'."""
    amt = _max_body_amount(_event_text(f.get("body_text") or "", f.get("filed_at") or ""))
    if not amt:
        return 0.0, ""
    cik = f.get("cik", "")
    mcap, sales = _co(cik, idx).get("market_cap"), rev.get(cik)
    sales = sales if sales and sales >= 10e6 else None
    parts = ([f"{_pct(amt / sales)} of annual revenue"] if sales else []) + \
            ([f"{_pct(amt / mcap)} of mcap"] if mcap else [])
    ratio = amt / sales if sales else (amt / mcap if mcap else 0.0)
    note = f"≈{_fmt_usd(amt)} in event text" + (f" ({', '.join(parts)})" if parts else "")
    if mcap and amt > 5 * mcap:   # NCRA: "$520.5M" on a $4M company = an "up to" cap, not revenue
        note += f" {_IMPLAUSIBLE} (>5x mcap — likely an 'up to' cap or aspirational total; verify)"
    return ratio, note


_IMPLAUSIBLE = "⚠ implausible"


def _catalyst_snippet(body: str) -> str:
    """Show body text around the EARLIEST catalyst keyword past the 8-K cover page.

    The cover page ends where the first "Item X.YY" heading appears — search from
    there. Bodies without that heading (6-Ks, EX-99 press releases, wire text)
    are searched from position 0: the old fixed `> 140` offset made a keyword in
    the first 140 chars fall through to showing boilerplate instead.
    """
    if not body:
        return ""
    low = body.lower()
    m = re.search(r"item\s+\d\.\d{2}", low)
    start = m.end() if m else 0
    best = -1
    for k in _CATALYST_KWS:
        i = low.find(k, start)
        if i >= 0 and (best < 0 or i < best):
            best = i
    if best >= 0:
        return "…" + body[max(0, best - 110):best + 210].strip() + "…"
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
    """Direction + shares @ price for a Form 4 (price omitted when it didn't parse),
    else the stake %. The side is ALWAYS shown: a superinvestor Form-4 SELL used to
    render with no direction and read like a marquee buy."""
    side = o.get("side") or ""
    if side in ("BUY", "SELL", "BUY-PRIVATE") and o.get("shares"):
        qty = f"{side} {o['shares']:,.0f} sh"
        return f"{qty} @ ${o['price']}" if o.get("price") else qty
    if side.startswith("13F-"):
        return f"13F {side[4:]}"
    if (o.get("form_type") or "").startswith("4"):
        return "no open-market buy/sell (grant/exercise/other)"
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
        # Related co-filers each file their OWN Form 4 for the SAME purchase (e.g.
        # two General Atlantic entities reporting one 1.3M-share LFTO buy, which
        # doubled the cluster to "2 insiders ≈$60M"). Dedupe identical trades —
        # same (trade date, shares, price) — before counting buyers / summing
        # dollars; the per-filer insider_buys rows stay untouched (both real).
        # Legacy rows without trade_date carry None on every co-filed copy, so
        # the pair still collapses.
        uniq: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
        for r in rows:
            uniq.setdefault((r.get("trade_date"), r.get("shares"), r.get("price")), r)
        deduped = list(uniq.values())
        buyers = {r.get("filer_name") for r in deduped if r.get("filer_name")}
        if len(buyers) >= 2:
            # Dollar total from PRICED legs only — the old (price or 1) fallback
            # fabricated "$500k" out of 500k unpriced shares. Unpriced share count
            # is reported separately so nothing is hidden.
            clusters.append({
                "cik": cik, "ticker": rows[0].get("ticker"), "company": rows[0].get("company"),
                "n_insiders": len(buyers),
                "usd": sum((r.get("shares") or 0) * r["price"] for r in deduped if r.get("price")),
                "unpriced_shares": sum((r.get("shares") or 0) for r in deduped if not r.get("price")),
            })
    clusters.sort(key=lambda c: (c["usd"], c["unpriced_shares"]), reverse=True)
    # PRIORITY-list floor for lone insider buys: sub-$25k token buys consume the
    # capped list without moving the needle. Unpriced buys are kept (can't value
    # them); every buy, tiny or not, still appears in the OWNERSHIP section below.
    min_usd = float((load_settings().get("signals") or {}).get("insider_buy_min_usd", 25000))
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
            [o for o in ownership if o.get("is_buy") and not o.get("matched_investor")
             and (not o.get("price") or (o.get("shares") or 0) * o["price"] >= min_usd)],
            key=lambda o: (o.get("shares") or 0) * (o.get("price") or 1), reverse=True),
        "corporate_events": _corporate_events(filings),
        # Business catalysts (contracts / capacity / FDA / partnerships / patents),
        # revealed by reading the filing body — the broad asymmetric-opportunity feed.
        "catalysts": [f for f in filings
                      if (set(f.get("candidate_tags") or []) & CATALYST_TAGS)
                      and not f.get("is_routine") and not _is_comp_or_proxy(f)],
    }


def _annotate_ownership_history(priority: dict[str, list], _store) -> None:
    """Deterministic direction / novelty from our OWN stored history:

    - 13D/13G rows: pct delta vs the same filer's PRIOR disclosure on the issuer
      (ADDED / TRIMMED computed from data, not regex-guessed from Item 5(c) prose).
    - 13D rows: flag a prior 13G by the same filer — a passive→active SWITCH is
      one of the strongest activist-intent signals there is.
    - Insider buys: flag the filer's FIRST stored open-market buy on the issuer.
    """
    conn = _store.get_conn()
    try:
        for o in priority.get("superinvestor", []) + priority.get("activist", []):
            ft = o.get("form_type") or ""
            if not ft.startswith("SCHEDULE 13"):
                continue
            prev = _store.prior_stake_pct(o.get("cik", ""), o.get("filer_name", ""),
                                          o.get("filed_at") or "", conn=conn)
            cur = o.get("pct")
            if prev is not None and cur is not None and prev != cur:
                dirn = "ADDED" if cur > prev else "TRIMMED"
                o["pct_delta"] = f"{prev}% → {cur}% ({dirn} vs prior stored filing)"
            if ft.startswith("SCHEDULE 13D") and _store.prior_13g_exists(
                    o.get("cik", ""), o.get("filer_name", ""), o.get("filed_at") or "", conn=conn):
                o["switched_from_13g"] = True
        for o in priority.get("insider_buys", [])[:40]:
            if not _store.prior_buy_exists(o.get("cik", ""), o.get("filer_name", ""),
                                           o.get("filed_at") or "", conn=conn):
                o["first_stored_buy"] = True
    finally:
        conn.close()


def _rank_catalysts_by_materiality(priority: dict[str, list], idx: dict[str, dict],
                                   rev: dict[str, float]) -> None:
    """Sort the CATALYST bucket by event-$ / annual revenue (else / mcap) so the
    40%-of-sales contract is line 1, not lost at position 28 of a 30-item cap. The
    amount is still only a hint — the rubric's date check stands."""
    for f in priority.get("catalysts", []):
        f["_mat_ratio"], f["_mat_note"] = _materiality(f, idx, rev)
    # implausible magnitudes (> 5x mcap) rank after every plausible one
    priority.get("catalysts", []).sort(
        key=lambda f: (_IMPLAUSIBLE not in f.get("_mat_note", ""), f.get("_mat_ratio", 0.0)), reverse=True)


def _filter_contracts(priority: dict[str, list], idx: dict[str, dict], settings: dict[str, Any]) -> None:
    """Keep federal awards that are material to the recipient (award / mcap >=
    signals.contract_min_pct_mcap %), largest ratio first. The raw feed ranked by
    date and was full of $4k-$50k awards to $50B+ companies."""
    floor = float((settings.get("signals") or {}).get("contract_min_pct_mcap", 0.25)) / 100
    ext = priority.get("external", [])
    contracts = [e for e in ext if e.get("category") == "contract"]
    for e in contracts:
        mcap = _co(e.get("cik", ""), idx).get("market_cap")
        e["_mat_ratio"] = (e["amount"] / mcap) if (e.get("amount") and mcap) else 0.0
    keep = sorted((e for e in contracts if e["_mat_ratio"] >= floor), key=lambda e: e["_mat_ratio"], reverse=True)
    priority["external"] = [e for e in ext if e.get("category") != "contract"] + keep
    priority["contracts_hidden"] = len(contracts) - len(keep)


def _attach_watchlist(priority: dict[str, list], filings: list[dict[str, Any]],
                      ownership: list[dict[str, Any]], _store) -> None:
    """Float anything touching a WATCHLISTED ticker to a dedicated bucket at the
    very top of the pack (Section-17 UX, now wired)."""
    watch = {w["cik"]: w for w in _store.get_watchlist()}
    priority["watchlist_filings"] = [f for f in filings if f.get("cik") in watch] if watch else []
    priority["watchlist_ownership"] = [o for o in ownership if o.get("cik") in watch] if watch else []


def _fetch_price_reactions(priority: dict[str, list], settings: dict[str, Any]) -> dict[str, dict]:
    """{ticker: {"closes", "volumes"}} for the priority tickers — the 'has it
    already re-rated?' check on the under-appreciated gate. Business catalysts and
    corporate events come right after superinvestors: with a 50-ticker cap they were
    crowded out (KOD's +171% Phase-3 day showed no px line). Best-effort."""
    sig = settings.get("signals") or {}
    if not sig.get("price_reaction", True):
        return {}
    max_tickers = int(sig.get("price_reaction_max_tickers", 150))
    items = (priority.get("watchlist_ownership", [])[:10] + priority.get("superinvestor", [])
             + priority.get("catalysts", [])[:30] + [f for f, _h in priority.get("corporate_events", [])[:25]]
             + priority.get("buy_clusters", [])[:10] + priority.get("insider_buys", [])[:25]
             + [o for o in priority.get("superinvestor_13f", []) if o.get("cik")][:30]
             + priority.get("activist", [])[:40] + priority.get("external", [])[:60])
    tickers: list[str] = []
    for item in items:
        t = (item.get("ticker") or "").strip()
        if t and t not in tickers:
            tickers.append(t)
    tickers = tickers[:max_tickers]
    if not tickers:
        return {}
    from concurrent.futures import ThreadPoolExecutor
    from scanner.adapters import price as _price
    from scanner.http import PoliteSession
    session = PoliteSession()
    with ThreadPoolExecutor(max_workers=8) as pool:
        pairs = pool.map(lambda t: (t, _price.fetch_daily(session, t)), tickers)
    out = {t: data for t, data in pairs if data}
    log.info("Price reactions fetched for %d/%d priority tickers", len(out), len(tickers))
    return out


def _price_note(ticker: str, event_iso: str | None, prices: dict[str, dict]) -> str:
    """'px $12.34 (+18.2% since event) · last session +3.1% on 4.2x avg volume' —
    or '' when no data. The last-session part is the 'moving on it NOW?' check."""
    data = prices.get((ticker or "").strip())
    if not data:
        return ""
    from scanner.adapters import price as _price
    r = _price.reaction(data["closes"], event_iso or "")
    if not r:
        return ""
    note = f"px ${r['last']:,.2f} ({r['pct_since_event']:+.1f}% since event)"
    ls = _price.last_session(data)
    if ls:
        note += f" · last session {ls['pct']:+.1f}% on {ls['vol_x']:.1f}x avg volume"
    return note


def _flag_price_mismatch(ownership: list[dict[str, Any]], clusters: list[dict[str, Any]],
                         prices: dict[str, dict]) -> None:
    """Warn when a Form-4 buy price is far (>1.5x / <0.67x) from the market close on
    the trade date: the price is in a foreign currency (BBD: BRL 17.98 vs ADR $3.37)
    or the trade was not at market. Clusters inherit the warning — their $ is wrong."""
    from scanner.adapters import price as _price
    flagged: set[str] = set()
    for o in ownership:
        data = prices.get((o.get("ticker") or "").strip())
        if not (o.get("is_buy") and o.get("price") and data):
            continue
        ref = _price.close_on_or_before(data["closes"], o.get("trade_date") or o.get("filed_at") or "")
        if ref and ref[1] and not 0.67 <= o["price"] / ref[1] <= 1.5:
            o["px_warn"] = (f"Form-4 price ${o['price']:,.2f} vs market close ${ref[1]:,.2f} on {ref[0]} — "
                            "foreign currency or not an open-market price; $ totals unreliable")
            flagged.add(o.get("cik", ""))
    for cl in clusters:
        if cl.get("cik") in flagged:
            cl["px_warn"] = "Form-4 prices do not match the market price — $ total unreliable (currency/plan?)"


def build_context_pack(summary: dict[str, Any] | None = None,
                       since: datetime | None = None,
                       enrich_bodies: bool = True,
                       fetch_bodies: bool = True,
                       out_path: str | None = None) -> dict[str, Any]:
    """Assemble + write the context pack. Returns paths and headline stats.

    `out_path` (project-relative .md) defaults to settings output.context_pack —
    the file the agent reads. The dashboard passes its own path so opening it
    never overwrites the pack a CLI `scan` just wrote.

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

    enrich_info: dict[str, int] = {}
    if enrich_bodies:
        from scanner import filing_body
        # Scale the fetch cap with the window: a fixed 200 read only ~10% of a
        # 30-day window's ~2,000 candidates, silently disabling body-derived
        # CATALYST tags on wide scans. Any remainder is DISCLOSED in the pack header.
        try:
            _w_start = datetime.fromisoformat(summary["window_since"])
            window_days = max(1, int((datetime.now(_tz()) - _w_start).total_seconds() // 86400) + 1)
        except (ValueError, TypeError):
            window_days = 1
        max_fetch = min(2500, max(200, 150 * window_days))
        # reads bodies + re-tags on them (cache-only when fetch_bodies=False)
        enrich_info = filing_body.enrich(filings, max_fetch=max_fetch, fetch_missing=fetch_bodies)
    summary["enrich"] = enrich_info

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

    rev = load_revenues()      # annual revenue by CIK — the materiality denominator
    _annotate_ownership_history(priority, _store)
    _rank_catalysts_by_materiality(priority, idx, rev)
    _filter_contracts(priority, idx, settings)
    _attach_watchlist(priority, filings, ownership, _store)
    prices = _fetch_price_reactions(priority, settings) if fetch_bodies else {}
    _flag_price_mismatch(ownership, priority.get("buy_clusters", []), prices)

    md = _render_md(summary, priority, filings, ownership, tagged_news, market_news,
                    coverage, idx, runs, prices, rev)
    pack_json = _render_json(summary, priority, filings, ownership, tagged_news, coverage, idx, prices, rev)

    md_path = resolve_path(out_path or settings.get("output", {}).get("context_pack", "runtime/context_pack.md"))
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
        "priority": {k: len(v) if isinstance(v, list) else v for k, v in priority.items()},
    }
    log.info("Context pack written: %s", stats)
    return stats


def _render_md(summary, priority, filings, ownership, tagged_news, market_news, coverage, idx,
               runs: dict[str, dict] | None = None, prices: dict[str, dict] | None = None,
               rev: dict[str, float] | None = None) -> str:
    out: list[str] = []
    prices = prices or {}
    rev = rev or {}
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
            r = runs.get(src) or {}
            if not r.get("last_success_at"):
                parts.append(f"{src}: never")
                stale.append(src)
                continue
            ok = r.get("status") == "ok"
            part = f"{src}: {'ok' if ok else 'FAILED'} {_et_short(r.get('updated_at'))}"
            if src in ("edgar", "ownership"):
                # The cursor = start of the day after the last FINAL daily index, so
                # "through" is the last fully-indexed filing day. Later days in the
                # pack come from EDGAR's live feed (best-effort) until that index lands.
                try:
                    thru = datetime.fromisoformat(r["last_success_at"]) - timedelta(days=1)
                    part += f" (index through {thru:%a %Y-%m-%d}; later days via live feed)"
                except ValueError:
                    pass
            parts.append(part)
            try:
                age_h = (_now_dt - datetime.fromisoformat(r.get("updated_at") or "")).total_seconds() / 3600
            except ValueError:
                age_h = 1e9
            if not ok or age_h > 48:
                stale.append(src)
        out.append("Data freshness: " + "  ·  ".join(parts))
        if stale:
            out.append(f"> ⚠ STALE/FAILED: {', '.join(stale)} — last run failed, or no run in >48h "
                       "(or never). Empty sections may mean 'not fetched', not 'quiet day'. "
                       "Run `refresh` and check runtime/logs/scanner.log.")
    # Body-coverage disclosure: catalyst tags come from filing BODIES; if the
    # fetch cap bit, say so instead of letting the CATALYST bucket imply completeness.
    _enr = summary.get("enrich") or {}
    if _enr.get("skipped"):
        out.append(f"> ⚠ BODY COVERAGE INCOMPLETE: {_enr['skipped']} candidate filings were NOT "
                   f"body-read this pass ({_enr.get('targets', 0)} read) — body-derived CATALYST "
                   "tags may be missing for them. Narrow the window or re-run `scan` (bodies are "
                   "cached, so each pass reads further).")
    out.append("")
    out.append("> Trust order: SEC FILING > OWNERSHIP (disclosed) > WIRE/PR > NEWS. "
               "Coverage = # news items on that ticker (LOW coverage + strong catalyst = asymmetric). "
               "`px …% since event` = price move since the event's prior close — a big move means "
               "the market has already re-rated it (down-rank); flat = possibly still unnoticed. "
               "Research leads only — not advice.")
    out.append("")

    # --- PRIORITY SIGNALS (read FIRST; deterministic, window-size-independent) ---
    sup, act, buys, evts = (priority["superinvestor"], priority["activist"],
                            priority["insider_buys"], priority["corporate_events"])
    out.append("## ⚡ PRIORITY SIGNALS — read first (highest-signal; any overflow is "
               "counted below, never silently lost)")
    if not any(priority.get(k) for k in ("superinvestor", "superinvestor_13f", "activist",
                                         "buy_clusters", "insider_buys", "corporate_events",
                                         "catalysts", "external", "watchlist_filings",
                                         "watchlist_ownership")):
        out.append("_No priority signals in window._")

    def _overflow(total: int, cap: int, kind: str) -> None:
        if total > cap:
            out.append(f"  … {total - cap} more {kind} in window not shown — "
                       f"query the DB / dashboard for the rest.")

    def _px(o: dict[str, Any]) -> None:
        note = _price_note(o.get("ticker", ""), o.get("filed_at") or o.get("event_date"), prices)
        if note:
            out.append(f"  {note}")

    def _hist(o: dict[str, Any]) -> None:
        if o.get("pct_delta"):
            out.append(f"  Δ {o['pct_delta']}")
        if o.get("switched_from_13g"):
            out.append("  ⚠ SWITCHED 13G → 13D — passive holder turned ACTIVE (strong intent signal)")

    # --- watchlist first: anything touching a user-watchlisted ticker ---
    wl_f, wl_o = priority.get("watchlist_filings", []), priority.get("watchlist_ownership", [])
    if wl_f or wl_o:
        out.append("### ★ WATCHLIST hits")
        for f in wl_f[:20]:
            out.append(f"[★ FILING] {_label(f.get('cik',''), f.get('ticker',''), f.get('company',''), idx)} — "
                       f"{f.get('form_type','')} — {f.get('headline','')} ({_et_short(f.get('filed_at'))})")
            if f.get("filing_url"):
                out.append(f"  Source: {f['filing_url']}")
        _overflow(len(wl_f), 20, "watchlist filings")
        for o in wl_o[:20]:
            out.append(f"[★ OWNERSHIP] {_label(o.get('cik',''), o.get('ticker',''), o.get('company',''), idx)} — "
                       f"{o.get('form_type','')} {_own_detail(o)} — {o.get('filer_name','')} ({_et_short(o.get('filed_at'))})")
            if o.get("filing_url"):
                out.append(f"  Source: {o['filing_url']}")
        _overflow(len(wl_o), 20, "watchlist ownership rows")

    for o in sup:   # superinvestor/watchlist hits (issuer-specific) — always shown in full
        out.append(f"[SUPERINVESTOR: {o.get('matched_investor')}] "
                   f"{_label(o.get('cik',''), o.get('ticker',''), o.get('company',''), idx)} — "
                   f"{o.get('form_type','')} {_own_detail(o)} — {o.get('filer_name','')} ({_et_short(o.get('filed_at'))})")
        if o.get("detail"):
            out.append(f"  → {o['detail']}")
        _hist(o)
        _px(o)
        if o.get("filing_url"):
            out.append(f"  Source: {o['filing_url']}")
    if priority.get("superinvestor_13f"):
        names = sorted({o.get("matched_investor") for o in priority["superinvestor_13f"]
                        if o.get("matched_investor") and not o.get("cik")})
        if names:
            out.append(f"[SUPERINVESTOR 13F-HR — quarterly/lagged] filed recently: {', '.join(names)}")
        changes = [o for o in priority["superinvestor_13f"] if o.get("cik")]
        for o in changes[:30]:   # per-issuer NEW / ADD / CUT / EXIT vs the prior 13F
            out.append(f"[SUPERINVESTOR 13F: {o.get('matched_investor')}] "
                       f"{_label(o.get('cik',''), o.get('ticker',''), o.get('company',''), idx)} — "
                       f"{o.get('detail','')}")
            _px(o)
            if o.get("filing_url"):
                out.append(f"  Source: {o['filing_url']}")
        _overflow(len(changes), 30, "13F position changes")
    for o in act[:40]:   # activist 13D AND 13D/A (amendments included)
        out.append(f"[ACTIVIST {o.get('form_type','')}] "
                   f"{_label(o.get('cik',''), o.get('ticker',''), o.get('company',''), idx)} {_own_detail(o)} — "
                   f"{o.get('filer_name','')} ({_et_short(o.get('filed_at'))})")
        if o.get("detail"):
            out.append(f"  → {o['detail']}")
        _hist(o)
        _px(o)
        if o.get("filing_url"):
            out.append(f"  Source: {o['filing_url']}")
    _overflow(len(act), 40, "activist 13D/13D-A rows")
    for cl in priority.get("buy_clusters", [])[:10]:   # multi-insider buying, rolled up
        unpriced = (f" + {cl['unpriced_shares']:,.0f} sh unpriced"
                    if cl.get("unpriced_shares") else "")
        out.append(f"[BUY CLUSTER] {_label(cl['cik'], cl.get('ticker', ''), cl.get('company', ''), idx)} — "
                   f"{cl['n_insiders']} insiders bought ≈${cl['usd']:,.0f} combined in window{unpriced}")
        if cl.get("px_warn"):
            out.append(f"  ⚠ {cl['px_warn']}")
    _overflow(len(priority.get("buy_clusters", [])), 10, "buy clusters")
    for o in buys[:25]:
        rel = f" ({o['relationship']})" if o.get("relationship") else ""
        out.append(f"[INSIDER BUY] {_label(o.get('cik',''), o.get('ticker',''), o.get('company',''), idx)} — "
                   f"{o.get('filer_name','')}{rel} {_own_detail(o)} ({_et_short(o.get('filed_at'))})")
        marks = []
        if o.get("detail"):        # 10b5-1 planned vs discretionary (Form-4 checkbox)
            marks.append(o["detail"])
        if o.get("first_stored_buy"):
            marks.append("first buy by this insider in stored history")
        if marks:
            out.append(f"  → {'; '.join(marks)}")
        if o.get("px_warn"):
            out.append(f"  ⚠ {o['px_warn']}")
        _px(o)
        if o.get("filing_url"):
            out.append(f"  Source: {o['filing_url']}")
    _overflow(len(buys), 25, "insider buys (smallest by $ value)")
    for f, hits in evts[:25]:
        out.append(f"[CORP EVENT: {', '.join(hits)}] "
                   f"{_label(f.get('cik',''), f.get('ticker',''), f.get('company',''), idx)} — "
                   f"{f.get('form_type','')} ({_et_short(f.get('filed_at'))})")
        _px(f)
        if f.get("filing_url"):
            out.append(f"  Source: {f['filing_url']}")
    _overflow(len(evts), 25, "corporate-event 8-Ks")
    # Catalysts are ordered by body-amount / mcap (materiality-to-size) so the
    # needle-mover leads; recency breaks ties.
    for f in priority.get("catalysts", [])[:30]:   # contracts / capacity / FDA / partnerships / patents
        tags = [t for t in (f.get("candidate_tags") or []) if t in CATALYST_TAGS]
        out.append(f"[CATALYST: {', '.join(tags)}] "
                   f"{_label(f.get('cik',''), f.get('ticker',''), f.get('company',''), idx)} — "
                   f"{f.get('form_type','')} ({_et_short(f.get('filed_at'))})")
        snip = _catalyst_snippet(f.get("body_text") or "")
        if snip:
            out.append(f"  → {snip}")
        note = f.get("_mat_note")
        if note:
            out.append(f"  $ {note}")
        _px(f)
        if f.get("filing_url"):
            out.append(f"  Source: {f['filing_url']}")
    _overflow(len(priority.get("catalysts", [])), 30, "business-catalyst filings (ranked by $ vs revenue/mcap)")
    # External feeds (non-EDGAR): federal contracts, FDA/clinical, patents.
    _ext = priority.get("external", [])
    if _ext:
        _bycat: dict[str, list] = {}
        for e in _ext:
            _bycat.setdefault(e.get("category", "other"), []).append(e)
        _labels = {"contract": "FEDERAL CONTRACT", "fda": "FDA / CLINICAL",
                   "pdufa": "PDUFA / ADCOMM (scheduled)", "patent": "PATENT"}
        _bycat.get("pdufa", []).sort(key=lambda e: e.get("event_date") or "")   # soonest first
        for cat in ("fda", "pdufa", "contract", "patent"):
            for e in _bycat.get(cat, [])[:20]:
                amt = f" ${e['amount']:,.0f}" if e.get("amount") else ""
                out.append(f"[{_labels.get(cat, cat.upper())}: {e.get('source')}] "
                           f"{_label(e.get('cik',''), e.get('ticker',''), e.get('company',''), idx)}{amt} — "
                           f"{e.get('headline','')} ({(e.get('event_date') or '')[:10]})")
                if e.get("detail"):
                    out.append(f"  → {_short(e['detail'], 200)}")
                _px(e)
                if e.get("url"):
                    out.append(f"  Source: {e['url']}")
            _overflow(len(_bycat.get(cat, [])), 20, f"{_labels.get(cat, cat)} items")
    if priority.get("contracts_hidden"):
        out.append(f"  _{priority['contracts_hidden']} federal awards below the materiality floor "
                   f"(award / mcap < signals.contract_min_pct_mcap) or unpriced — hidden._")
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
        note = _materiality(f, idx, rev)[1]
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
        "pct_delta": o.get("pct_delta"), "switched_from_13g": bool(o.get("switched_from_13g")),
        "first_stored_buy": bool(o.get("first_stored_buy")), "px_warn": o.get("px_warn"),
        "filed_at": o.get("filed_at"), "source": o.get("filing_url"),
    }


def _px_json(item: dict[str, Any], prices: dict[str, dict]) -> dict[str, Any] | None:
    data = prices.get((item.get("ticker") or "").strip())
    if not data:
        return None
    from scanner.adapters import price as _price
    r = _price.reaction(data["closes"], item.get("filed_at") or item.get("event_date") or "")
    return {**r, "last_session": _price.last_session(data)} if r else None


def _render_json(summary, priority, filings, ownership, tagged_news, coverage, idx,
                 prices: dict[str, dict] | None = None, rev: dict[str, float] | None = None) -> dict[str, Any]:
    prices = prices or {}
    rev = rev or {}
    return {
        "generated_at": datetime.now(_tz()).isoformat(),
        "window_since": summary["window_since"],
        "stats": {
            "filings": len(filings),
            "ownership_flagged": summary["ownership_flagged"],
            "company_news": len(tagged_news),
            "body_enrich": summary.get("enrich") or {},
        },
        "priority": {
            "watchlist": {
                "filings": [{"ticker": f.get("ticker"), "form_type": f.get("form_type"),
                             "headline": f.get("headline"), "filed_at": f.get("filed_at"),
                             "source": f.get("filing_url")}
                            for f in priority.get("watchlist_filings", [])[:40]],
                "ownership": [_own_json(o, idx) for o in priority.get("watchlist_ownership", [])[:40]],
            },
            "superinvestor": [{**_own_json(o, idx), "price_reaction": _px_json(o, prices)}
                              for o in priority["superinvestor"]],
            "superinvestor_13f": sorted({o.get("matched_investor") for o in priority["superinvestor_13f"]
                                         if o.get("matched_investor") and not o.get("cik")}),
            "superinvestor_13f_changes": [_own_json(o, idx) for o in priority["superinvestor_13f"] if o.get("cik")],
            "activist": [{**_own_json(o, idx), "price_reaction": _px_json(o, prices)}
                         for o in priority["activist"][:60]],
            "buy_clusters": priority.get("buy_clusters", [])[:20],
            "insider_buys": [{**_own_json(o, idx), "price_reaction": _px_json(o, prices)}
                             for o in priority["insider_buys"][:40]],
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
                "materiality": f.get("_mat_note") or None,
                "materiality_ratio": f.get("_mat_ratio") or None,
                "price_reaction": _px_json(f, prices),
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
            "materiality": _materiality(f, idx, rev)[1] or None,
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
