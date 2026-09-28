"""SEC EDGAR ownership ingester (Milestone 5).

Three disclosure signals, all discovered via the shared daily index (M3):
  - SCHEDULE 13D / 13D/A : activist / strategic >5% stake (intent to influence)
  - SCHEDULE 13G / 13G/A : passive >5% stake
  - Form 4 / 4/A         : insider transactions; we flag open-market BUYS (code P)
  - 13F-HR               : quarterly institutional holdings (SLOW / lagged signal)

Indexing facts verified live (2026-06):
  - The daily index lists a filing under EVERY associated CIK — for Form 4 and
    SCHEDULE 13* that means BOTH the subject company AND the filer get a row
    (same accession). We filter cheaply by universe CIK, dedupe by accession,
    then re-key each record to the TRUE subject parsed from the document itself
    (SGML SUBJECT COMPANY / <issuerCik>) so a filer-side match can't mis-key it.
  - For 13F-HR, the daily-index CIK is the FILER (the manager) -> match the
    daily-index company NAME against the superinvestor watchlist instead.

Each matched filing is fetched once as its full-submission .txt, which contains
both the SGML header (FILED BY, acceptance time) and the embedded ownership XML
(transaction codes). Superinvestor matching is case-insensitive against the
filer/owner name: every word of a config/superinvestors.yaml entry must appear
(any order — EDGAR lists people surname-first).

NOTE: Form 3/5 (initial/annual) are skipped for now (lower signal); 13F holdings
are flagged at the filing level (the full holdings->universe diff via CUSIP is a
documented phase-2 refinement).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser

from scanner import store
from scanner.config import load_settings, load_superinvestors, resolve_path
from scanner.http import PoliteSession
from scanner.ingest_edgar import _iter_dates, acceptance_iso, rows_for_day
from scanner.universe import _norm, load_map

log = logging.getLogger(__name__)

# Concurrent fetch workers. The shared rate gate in http.py keeps the aggregate
# under the SEC cap regardless; workers only hide per-request network latency.
_WORKERS = 8

# Issuer-keyed ownership forms (daily-index CIK == issuer, so filterable by universe).
ISSUER_FORMS = {
    "4", "4/A",
    "SCHEDULE 13D", "SCHEDULE 13D/A",
    "SCHEDULE 13G", "SCHEDULE 13G/A",
}


def _et() -> ZoneInfo:
    return ZoneInfo(load_settings().get("timezone", "America/New_York"))


def _now() -> datetime:
    return datetime.now(_et())


def _dedupe_hash(accession: str) -> str:
    return hashlib.sha1(accession.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Superinvestor matching
# --------------------------------------------------------------------------- #
def _superinvestors() -> list[str]:
    return [str(x).strip() for x in (load_superinvestors().get("investors") or []) if str(x).strip()]


def _match_investor(name: str, watchlist: list[str]) -> str | None:
    """Match a filer/owner name to the watchlist: EVERY word of an entry must
    appear word-bounded in the name, in ANY order — EDGAR lists people
    surname-first ('LOEB DANIEL S'), so a full-name entry 'Daniel Loeb' still
    matches. A bare surname shared by an unrelated insider must NOT match
    (seen live: 'LOEB GARY', an ISRG insider, tagged as Dan Loeb), and a word
    never matches inside a longer one ('Ackman' vs 'Jackman Worthing')."""
    if not name:
        return None
    low = name.lower()
    for inv in watchlist:
        words = inv.lower().split()
        if words and all(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", low)
                         for w in words):
            return inv
    return None


# --------------------------------------------------------------------------- #
# Tiny XML/SGML helpers (regex — ownership docs are namespace-free)
# --------------------------------------------------------------------------- #
def _tag(text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
    return m.group(1).strip() if m else None


def _val(block: str, tag: str) -> str | None:
    """Extract <tag><value>X</value></tag> (the Form 4 amount pattern)."""
    m = re.search(rf"<{tag}>\s*<value>(.*?)</value>", block, re.S)
    return m.group(1).strip() if m else None


def _num(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _flag(text: str, tag: str) -> bool:
    v = _tag(text, tag)
    return v is not None and v.strip().lower() in ("1", "true")


def _subject_cik_int(text: str, form: str) -> int | None:
    """The TRUE subject/issuer CIK, read from the document itself.

    The daily index lists a filing under EVERY associated CIK — subject AND filer —
    (verified live: the GameStop→eBay 13D/A appears under both 1326380 and 1065088),
    so the index-row CIK alone cannot tell which side we matched. Form 4 carries
    <issuerCik> in its XML; 13D/13G carry a SUBJECT COMPANY block in the SGML header
    (modern ones also an <issuerCIK> XML tag)."""
    if form.startswith("4"):
        m = re.search(r"<issuerCik>(\d+)</issuerCik>", text, re.I)
        if not m:   # legacy fallback: the SGML header's ISSUER block
            i = text.find("ISSUER:")
            m = re.search(r"CENTRAL INDEX KEY:\s*(\d+)", text[i:i + 800]) if i >= 0 else None
        return int(m.group(1)) if m else None
    i = text.find("SUBJECT COMPANY")
    if i >= 0:
        m = re.search(r"CENTRAL INDEX KEY:\s*(\d+)", text[i:i + 800])
        if m:
            return int(m.group(1))
    m = re.search(r"<issuerCIK>(\d+)</issuerCIK>", text, re.I)   # modern 13D/G XML
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# Per-form parsers
# --------------------------------------------------------------------------- #
def _parse_form4(text: str) -> dict[str, Any]:
    """Extract owner, relationship, side (BUY/SELL/OTHER), shares, price from a Form 4."""
    m = re.search(r"<ownershipDocument>(.*?)</ownershipDocument>", text, re.S)
    doc = m.group(1) if m else text
    owner = _tag(doc, "rptOwnerName") or ""
    rel = []
    if _flag(doc, "isDirector"):
        rel.append("Director")
    if _flag(doc, "isOfficer"):
        rel.append((_tag(doc, "officerTitle") or "Officer").strip())
    if _flag(doc, "isTenPercentOwner"):
        rel.append("10% owner")

    # ONLY the non-derivative table can evidence an open-market BUY. Code P also
    # appears on DERIVATIVE rows (e.g. warrants "acquired" under an employment
    # agreement — pure compensation), and the old any-table scan flagged those as
    # insider buys (ABAT CEO's comp warrants showed as an "$8.3M buy"). A real buy
    # is a PRICED non-derivative P; the price must come from the buy legs, not
    # from a same-form tax-withholding sale.
    blocks = re.findall(r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>", doc, re.S)
    footnotes = {fid: re.sub(r"\s+", " ", txt).strip() for fid, txt in
                 re.findall(r'<footnote id="(F\d+)">(.*?)</footnote>', doc, re.S)}
    codes: list[str] = []
    buy_sh = sell_sh = buy_val = sell_val = 0.0
    buy_date = None
    owned_after = None
    buy_notes: list[str] = []
    for b in blocks:
        code = _tag(b, "transactionCode") or ""
        sh = _num(_val(b, "transactionShares"))
        pr = _num(_val(b, "transactionPricePerShare"))
        if code:
            codes.append(code)
        if code == "P" and sh and pr:
            buy_sh += sh
            buy_val += sh * pr
            # Trade date of the buy leg: related co-filers (e.g. two General
            # Atlantic funds) each file a Form 4 for the SAME purchase; the pack
            # dedupes cluster math on (trade_date, shares, price).
            buy_date = buy_date or _val(b, "transactionDate")
            owned_after = _num(_val(b, "sharesOwnedFollowingTransaction")) or owned_after
            buy_notes += [footnotes.get(f, "") for f in re.findall(r'<footnoteId id="(F\d+)"', b)]
        elif code == "S" and sh:
            sell_sh += sh
            if pr:
                sell_val += sh * pr

    # Share-weighted average price across legs (a multi-leg buy at $10/$20 is
    # NOT a "$20 buy" — the old max() overstated the spend).
    buy_px = round(buy_val / buy_sh, 4) if buy_sh else None
    sell_px = round(sell_val / sell_sh, 4) if sell_val else None
    side = "BUY" if buy_sh else ("SELL" if sell_sh else "OTHER")
    shares = buy_sh if side == "BUY" else (sell_sh if side == "SELL" else None)
    # 10b5-1 pre-planned trades are mechanically scheduled — far weaker signal
    # than a discretionary open-market buy. The document-level checkbox marks it.
    planned = _flag(doc, "aff10b5One")
    detail = ("10b5-1 PLANNED transaction (pre-scheduled — weaker signal)"
              if planned else ("discretionary open-market buy" if buy_sh else ""))
    # Code P also covers PRIVATE purchases (seen live: GSAT "$30M buy" = shares bought
    # from another director "in a private transaction for estate planning") and IPO /
    # placement allocations. The buy leg's own footnotes say so — such a row is not an
    # open-market signal, so it leaves the buy buckets (side BUY-PRIVATE, is_buy 0).
    private = next((n for n in buy_notes if _PRIVATE_RE.search(n)), None)
    if buy_sh and private:
        side, detail = "BUY-PRIVATE", f"PRIVATE/OFFERING purchase per footnote — not open-market: “{private[:160]}”"
    elif buy_sh and owned_after:
        before = owned_after - buy_sh
        # How much the insider grew the (same-line) holding — a 25x increase is a
        # different signal from topping up 1%.
        detail += (f"; holding +{buy_sh / before * 100:,.0f}% ({before:,.0f} → {owned_after:,.0f} sh)"
                   if before > 0 else f"; NEW position ({owned_after:,.0f} sh)")
    return {
        "filer_name": owner,
        "relationship": ", ".join(rel),
        "side": side,
        "shares": shares or None,
        "price": buy_px if buy_sh else sell_px,
        "trade_date": buy_date if buy_sh else None,
        "is_buy": side == "BUY",
        "detail": detail,
    }


_PRIVATE_RE = re.compile(
    r"private(?:ly)?[\s-]+(?:transaction|negotiated|sale|purchase|placement)|estate planning|"
    r"subscription agreement|initial public offering|\bIPO\b|underwritten (?:public )?offering|"
    r"directed share|registered direct|concurrent private|securities purchase agreement|"
    r"in connection with the (?:offering|closing)", re.I)


def _parse_sc13(text: str) -> dict[str, Any]:
    """Extract the FILED BY filer + (best-effort) percent-of-class from a 13D/13G."""
    filer = filer_cik = None
    fb = text.find("FILED BY")
    if fb >= 0:
        seg = text[fb:fb + 800]
        mn = re.search(r"COMPANY CONFORMED NAME:\s*(.+)", seg)
        mc = re.search(r"CENTRAL INDEX KEY:\s*(\d+)", seg)
        filer = mn.group(1).strip() if mn else None
        filer_cik = mc.group(1).strip() if mc else None
    pct = None
    # Modern 13D/G (2024+) is structured XML: <percentOfClass>5.2</percentOfClass>
    # (sometimes wrapped in <value>). Fall back to the legacy cover-page text.
    mx = re.search(r"<percentOfClass>\s*(?:<value>)?\s*(\d{1,3}(?:\.\d+)?)", text, re.I)
    mp = mx or re.search(r"PERCENT OF CLASS[^%\d]{0,80}?(\d{1,3}(?:\.\d+)?)\s*%", text, re.I | re.S)
    if mp:
        try:
            pct = float(mp.group(1))
        except ValueError:
            pass
    return {"filer_name": filer or "", "filer_cik": filer_cik, "pct": pct}


def _amendment_detail(full_txt: str, form: str) -> str:
    """Best-effort 'what did this amendment do' — the Item 5(c) last-60-days
    transaction clause + a coarse direction hint (ADDED / TRIMMED / NEW / technical).

    Heuristic: the *snippet* is the reliable part (a verbatim quote from the filing);
    the direction tag is a hint to confirm. This is the 'digging' that distinguishes a
    fresh activist buy from a long-term holder's routine amendment (e.g. Ackman/QSR).
    """
    if form == "SCHEDULE 13G":
        return "NEW 13G — passive >5% position"
    # isolate the primary SC 13 document (skip exhibits), strip tags
    body = full_txt
    for d in full_txt.split("<DOCUMENT>"):
        # legacy primary docs are <TYPE>SC 13D/A; post-2024 structured ones are
        # <TYPE>SCHEDULE 13D/A (verified live) — match both.
        if re.search(r"<TYPE>\s*(?:SC|SCHEDULE)\s*13", d):
            body = d
            break
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))

    # 1) AFFIRMATIVE activist intent in Item 4 — high signal. Phrased to avoid the
    #    boilerplate "may acquire / may propose a merger" laundry list every 13D has.
    intents = [
        (r"(?:delivered|submitted|made|sent)[^.]{0,80}(?:non-?binding\s+)?(?:proposal|offer|letter)[^.]{0,90}acquir", "BUYOUT PROPOSAL"),
        (r"agreement and plan of merger|definitive merger agreement|enter(?:ed)?\s+into[^.]{0,40}merger\s+agreement", "MERGER AGREEMENT"),
        (r"commenc\w*[^.]{0,30}tender offer|tender offer to purchase", "TENDER OFFER"),
        (r"intend[s]?\s+to\s+nominate|has\s+nominated|deliver\w*[^.]{0,40}nominat", "BOARD NOMINEES"),
    ]
    for pat, tag in intents:
        m = re.search(pat, text, re.I)
        if m:
            snip = re.sub(r"\s+", " ", text[max(0, m.start() - 60):m.start() + 260]).strip()
            return f"[{tag}] …{snip}…"[:340]

    # 2) else: new vs amendment + last-60-days transaction DIRECTION
    m = re.search(r".{0,110}(?:60|sixty)\s+days.{0,200}", text, re.I)
    clause = m.group(0).strip() if m else ""
    cl = clause.lower()
    priced = re.search(r"\$\s?\d[\d,]*(?:\.\d+)?\s*(?:per share|/\s?share|a share)", text, re.I)
    if re.search(r"\b(purchased|acquired|bought)\b", cl) and not re.search(r"\b(sold|disposed of)\b", cl):
        dirn = "ADDED"
    elif re.search(r"\b(sold|disposed of)\b", cl):
        dirn = "TRIMMED"
    elif cl and re.search(r"\bno\b.{0,40}transaction", cl) and "except" not in cl:
        dirn = "no recent txns (technical)"
    elif "except" in cl:
        dirn = "recent txns — see exhibits"
    else:
        dirn = "dig Item 5(c)"
    if form == "SCHEDULE 13D":
        return f"NEW 13D — initial >5% position ({dirn})"
    price = f" · ${priced.group(0).lstrip('$')}" if priced else ""
    return f"[{form} · {dirn}{price}] {clause[:160]}".strip()


# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 13F-HR holdings diff (superinvestor NEW / ADD / CUT / EXIT per issuer)
# --------------------------------------------------------------------------- #
_CUSIP_CACHE = resolve_path("data/universe/cusip_map.json")


def _ns_tag(block: str, tag: str) -> str | None:
    """Like _tag, tolerating the namespace prefixes some 13F tables use (ns1:)."""
    m = re.search(rf"<(?:\w+:)?{tag}>(.*?)</(?:\w+:)?{tag}>", block, re.S)
    return m.group(1).strip() if m else None


def parse_13f_table(xml: str) -> dict[str, dict[str, Any]]:
    """{cusip: {"name", "shares", "value"}} from a 13F information table — SHARE rows only
    (no bond principal, no put/call option rows), summed across manager lines."""
    out: dict[str, dict[str, Any]] = {}
    for row in re.findall(r"<(?:\w+:)?infoTable>(.*?)</(?:\w+:)?infoTable>", xml, re.S):
        if _ns_tag(row, "putCall") or (_ns_tag(row, "sshPrnamtType") or "SH") != "SH":
            continue
        cusip, sh = (_ns_tag(row, "cusip") or "").upper(), _num(_ns_tag(row, "sshPrnamt")) or 0.0
        if cusip:
            h = out.setdefault(cusip, {"name": _ns_tag(row, "nameOfIssuer") or "", "shares": 0.0, "value": 0.0})
            h["shares"] += sh
            h["value"] += _num(_ns_tag(row, "value")) or 0.0   # USD (whole dollars since 2023)
    return out


def diff_13f(prev: dict[str, dict[str, Any]], cur: dict[str, dict[str, Any]]) -> list[tuple[str, str, float, float]]:
    """[(cusip, NEW|ADD|CUT|EXIT, prev_shares, cur_shares)] — ADD/CUT = at least a
    50% change in share count; smaller rebalancing is noise for this purpose."""
    out = []
    for c in sorted(prev.keys() | cur.keys()):
        p, n = prev.get(c, {}).get("shares", 0.0), cur.get(c, {}).get("shares", 0.0)
        kind = ("NEW" if n and not p else "EXIT" if p and not n else
                "ADD" if p and n >= p * 1.5 else "CUT" if p and n <= p * 0.5 else None)
        if kind:
            out.append((c, kind, p, n))
    return out


def _13f_holdings(session: PoliteSession, cik_int: int, acc: str) -> dict[str, dict[str, Any]]:
    folder = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc.replace('-', '')}"
    items = session.edgar_get(f"{folder}/index.json", timeout=30).json()["directory"]["item"]
    table = next(i["name"] for i in items
                 if i["name"].lower().endswith(".xml") and i["name"].lower() != "primary_doc.xml")
    return parse_13f_table(session.edgar_get(f"{folder}/{table}", timeout=60).text)


def _cusip_tickers(session: PoliteSession, cusips: list[str]) -> dict[str, str | None]:
    """CUSIP -> ticker via OpenFIGI (free, no key: 25 requests/min, 10 per request),
    cached on disk — 13F tables abbreviate names ("ALLY FINL INC"), so names can't be
    matched reliably."""
    cache: dict[str, str | None] = {}
    if _CUSIP_CACHE.exists():
        cache = json.loads(_CUSIP_CACHE.read_text(encoding="utf-8"))
    todo = [c for c in cusips if c not in cache]
    for i in range(0, len(todo), 10):
        batch = todo[i:i + 10]
        if i:
            time.sleep(2.5)                     # stay under the keyless 25/min limit
        res = session.post("https://api.openfigi.com/v3/mapping", timeout=30,
                           json=[{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in batch]).json()
        for c, r in zip(batch, res):
            cache[c] = ((r.get("data") or [{}])[0].get("ticker")) if isinstance(r, dict) else None
    if todo:
        _CUSIP_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CUSIP_CACHE.write_text(json.dumps(cache, indent=0), encoding="utf-8")
    return {c: cache.get(c) for c in cusips}


def _13f_changes(session: PoliteSession, r: dict[str, Any], by_cik: dict[int, dict[str, Any]],
                 watchlist: list[str]) -> list[dict[str, Any]]:
    """Ownership rows for a watchlist manager's 13F-HR position changes on universe
    issuers, vs that manager's previous 13F-HR. [] for amendments / first filings."""
    if r["form"] != "13F-HR":
        return []
    cik10 = str(r["cik"]).zfill(10)
    recent = session.edgar_get(f"https://data.sec.gov/submissions/CIK{cik10}.json",
                               timeout=45).json()["filings"]["recent"]
    filings = sorted(((p, a) for a, f, p in zip(recent["accessionNumber"], recent["form"], recent["reportDate"])
                      if f == "13F-HR"), reverse=True)
    period = next((p for p, a in filings if a == r["accession"]), None)
    prev_acc = next((a for p, a in filings if period and p < period), None)
    if not (period and prev_acc):
        return []
    cur = _13f_holdings(session, r["cik"], r["accession"])
    prev = _13f_holdings(session, r["cik"], prev_acc)
    # Skip sub-$5M positions on both sides: a 3,564-share "NEW" line from a
    # sub-manager (seen live, Berkshire/DHI) is not a superinvestor signal.
    changes = [ch for ch in diff_13f(prev, cur)
               if max(prev.get(ch[0], {}).get("value", 0), cur.get(ch[0], {}).get("value", 0)) >= 5e6]
    tickers = _cusip_tickers(session, [c for c, *_ in changes])
    by_ticker = {_norm(m.get("ticker", "")): m for m in by_cik.values()}
    out = []
    for cusip, kind, p, n in changes:
        meta = by_ticker.get(_norm(tickers.get(cusip) or ""))
        if not meta:
            continue
        out.append({
            "ticker": meta.get("ticker", ""), "cik": meta["cik"], "company": meta.get("name", ""),
            "filer_name": r["company"], "relationship": "", "form_type": "13F-HR",
            "side": f"13F-{kind}", "shares": n or None, "price": None, "pct": None, "is_buy": 0,
            "is_insider": 0, "is_activist": 0,
            "detail": (f"13F {kind}: {p:,.0f} → {n:,.0f} sh (${cur.get(cusip, prev.get(cusip, {})).get('value', 0) / 1e6:,.0f}M; "
                       f"quarter ending {period}; filed ~45 days later)"),
            "matched_investor": _match_investor(r["company"], watchlist),
            "filing_url": _index_url(r["cik"], r["accession"]),
            "filed_at": acceptance_iso("", r["date"]),
            "accession": r["accession"],
            "dedupe_hash": _dedupe_hash(f"{r['accession']}|{cusip}"),
            "source": "SEC EDGAR",
        })
    return out


def _txt_url(cik_int: int, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}.txt"


def _index_url(cik_int: int, accession: str) -> str:
    accn = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn}/{accession}-index.htm"


def ingest(session: PoliteSession | None = None,
           since: datetime | None = None,
           until: datetime | None = None,
           forms: set[str] | None = None,
           stats: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Fetch NEW ownership disclosures for the universe within [since, until].

    Discovery via the daily index (live feed for today/yesterday); each NEW matched
    filing fetched once as .txt. `stats` gets {"last_index_day", "failed"} like
    ingest_edgar.ingest, so the caller never moves the cursor past a lost filing.
    `forms` restricts the issuer-keyed forms processed (default = all of
    ISSUER_FORMS); pass e.g. just the SCHEDULE 13D/13G set to skip the heavy
    Form-4 sweep on a wide backfill. Per-filing failures are isolated and logged.
    """
    session = session or PoliteSession()
    forms = forms or ISSUER_FORMS
    lookback = int(load_settings().get("lookback_hours", 24))
    since = since or (_now() - timedelta(hours=lookback))
    until = until or _now()

    by_cik = {int(c["cik"]): c for c in load_map()}
    watchlist = _superinvestors()

    # 1. Discover from the daily index. A filing appears under EVERY associated CIK
    #    (subject AND filer), so dedupe by accession here — the true subject company
    #    is re-derived from the document in _process_issuer either way.
    issuer_hits: list[dict[str, Any]] = []     # Form 4 / 13D / 13G about our companies
    f13_hits: list[dict[str, Any]] = []        # 13F-HR by a watchlist manager
    seen_acc: set[str] = set()
    last_final = None
    for d in _iter_dates(since, until):
        rows, final = rows_for_day(session, d)
        if final and rows:
            last_final = d
        for r in rows:
            if r["form"] in forms and r["cik"] in by_cik:
                if r["accession"] not in seen_acc:
                    seen_acc.add(r["accession"])
                    issuer_hits.append(r)
            elif r["form"] in ("13F-HR", "13F-HR/A"):   # skip 13F-NT notices (no holdings)
                if _match_investor(r["company"], watchlist):
                    f13_hits.append(r)
    # Skip filings already stored — INSERT OR IGNORE would discard them anyway, and
    # the intraday live-feed sweep would otherwise re-fetch every Form 4 each run.
    known = store.existing_hashes("ownership", [_dedupe_hash(r["accession"]) for r in issuer_hits + f13_hits])
    issuer_hits = [r for r in issuer_hits if _dedupe_hash(r["accession"]) not in known]
    f13_hits = [r for r in f13_hits if _dedupe_hash(r["accession"]) not in known]

    # 2. Issuer-keyed forms (Form 4 + 13D/13G): fetch each .txt once, parse.
    #    Fetched concurrently (latency-hiding) under the shared SEC rate gate.
    def _process_issuer(r: dict[str, Any]) -> dict[str, Any] | None | bool:
        """Record, None (not about a universe company), or False (fetch FAILED)."""
        try:
            text = session.edgar_get(_txt_url(r["cik"], r["accession"]), timeout=45).text
        except Exception as exc:  # noqa: BLE001 - isolate per-filing failures
            log.warning("ownership .txt %s -> %s", r["accession"], exc)
            return False
        # Re-key to the TRUE subject company from the document — the index row we
        # matched may be the FILER's side (e.g. GameStop filing a 13D/A on eBay).
        subj = _subject_cik_int(text, r["form"]) or r["cik"]
        meta = by_cik.get(subj)
        if meta is None:
            return None   # filer-side row of a filing about a non-universe company
        is_13d = r["form"].startswith("SCHEDULE 13D")
        if r["form"].startswith("4"):
            p = _parse_form4(text)   # carries its own `detail` (10b5-1 / discretionary)
            rec = {**p, "form_type": r["form"], "is_insider": 1, "is_activist": 0}
        else:  # SCHEDULE 13D / 13G
            p = _parse_sc13(text)
            # Self-filing (filer CIK == subject CIK) is a treasury/subsidiary 13D, not
            # an external activist stake — don't flag it.
            is_self = bool(p["filer_cik"]) and int(p["filer_cik"]) == subj
            rec = {"filer_name": p["filer_name"], "relationship": "", "side": "STAKE",
                   "shares": None, "price": None, "pct": p["pct"], "is_buy": 0,
                   "form_type": r["form"], "is_insider": 0,
                   "is_activist": 1 if (is_13d and not is_self) else 0,
                   "detail": _amendment_detail(text, r["form"])}  # <-- the 'digging'
        rec.update({
            "ticker": meta.get("ticker", ""),
            "cik": meta["cik"],
            "company": meta.get("name") or r["company"],
            "matched_investor": _match_investor(rec.get("filer_name", ""), watchlist),
            "filing_url": _index_url(subj, r["accession"]),
            "filed_at": acceptance_iso(text, r["date"]),
            "accession": r["accession"],
            "dedupe_hash": _dedupe_hash(r["accession"]),
            "source": "SEC EDGAR",
        })
        rec.setdefault("pct", None)
        return rec

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        results = list(pool.map(_process_issuer, issuer_hits))
    out: list[dict[str, Any]] = [rec for rec in results if rec]
    failed = sum(1 for rec in results if rec is False)

    # 3. 13F-HR by a watchlist manager (quarterly/lagged): diff the holdings against
    #    the manager's previous 13F-HR (NEW / ADD / CUT / EXIT per universe issuer),
    #    then record the filing event itself. A failed diff skips BOTH, so the
    #    filing stays unknown and the next refresh retries it.
    for r in f13_hits:
        try:
            changes = _13f_changes(session, r, by_cik, watchlist)
        except Exception as exc:  # noqa: BLE001 - counted; retried next refresh
            log.warning("13F diff %s -> %s", r["accession"], exc)
            failed += 1
            continue
        out += changes
        out.append({
            "ticker": "", "cik": "", "company": "",
            "filer_name": r["company"], "relationship": "", "form_type": "13F-HR",
            "side": "13F", "shares": None, "price": None, "pct": None, "is_buy": 0,
            "is_insider": 0, "is_activist": 0,
            "detail": f"quarterly 13F-HR (lagged) — {len(changes)} universe position changes vs prior 13F",
            "matched_investor": _match_investor(r["company"], watchlist),
            "filing_url": _index_url(r["cik"], r["accession"]),
            "filed_at": acceptance_iso("", r["date"]),
            "accession": r["accession"],
            "dedupe_hash": _dedupe_hash(r["accession"]),
            "source": "SEC EDGAR",
        })

    if stats is not None:
        stats.update(last_index_day=last_final, failed=failed)
    buys = sum(1 for o in out if o.get("is_buy"))
    marquee = sum(1 for o in out if o.get("matched_investor"))
    log.info("Ownership ingest: %d new records (%d insider buys, %d activist 13D, %d superinvestor, "
             "%d 13F, %d fetch failures) [%s..%s]",
             len(out), buys, sum(1 for o in out if o.get("is_activist")), marquee, len(f13_hits),
             failed, since.date(), until.date())
    return out
