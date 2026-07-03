# CLAUDE.md — catalyst-scanner-us (project memory + reasoning rubric)

This file makes every session behave consistently. It has two parts: the
**commands** to operate the tool, and the **reasoning rubric** the agent applies
when the user asks for a scan or a follow-up.

> This is a personal **research idea-generation** tool. Output surfaces sourced
> *leads to investigate* — never buy/sell advice, never certainty.

---

## Commands

Run via `run.bat <command>` (Windows) or `.venv\Scripts\python.exe -m scanner.cli <command>`.

| Command | What it does |
|---|---|
| `setup-universe` | (Re)build the top-N US-by-market-cap universe + ticker→CIK map. Run occasionally. |
| `refresh` | Run all ingesters (EDGAR + news + ownership), catch-up since last run, store with dedupe. |
| `scan` | `refresh` → pre-filter → write `runtime/context_pack.md`. Add `--skip-refresh` to use stored data. |
| `ask "<question>"` | Print stored data relevant to a company / tag for a follow-up (`--fetch` for a fresh pull). |
| `digest` | Save a dated ranked digest to `digests/`. |
| `watch add/remove/list <TICKER>` | Manage the watchlist — watched tickers float to a ★ section at the very top of the pack. |
| `schedule` | Print/install the Windows Task Scheduler jobs (ET-aware). |

All windowed commands accept `--hours N` / `--days N` (they combine). On a weekend
the last filings are Friday's, so use `--days 3` to reach them.

**The core loop:** the user runs `scan`, then says *"read the context pack and give
me today's asymmetric signals."* The agent reads `runtime/context_pack.md` and applies
the rubric below.

---

## Reasoning rubric (apply when the user asks for a scan)

Read `runtime/context_pack.md` and produce a **ranked list of asymmetric
opportunities**. The pack separates SEC FILINGS (highest trust) from OWNERSHIP
(disclosed 13D/13G/Form 4), WIRE/PR, and NEWS (lower trust) — preserve that
separation and never blur it.

**Check the `Data freshness:` header line first** — if a source is marked STALE
(>48h since its last successful refresh), say so explicitly instead of calling it a
quiet day; an empty section then means "not fetched", not "nothing happened".
Also check the **`⚠ BODY COVERAGE INCOMPLETE` header line** (appears on wide
windows): it means N candidate filings were not body-read this pass, so the
CATALYST bucket may be missing items — mention it, and suggest re-running `scan`
(bodies are cached, each pass reads further) or narrowing the window.

**Always start with the `⚡ PRIORITY SIGNALS` section at the top of the pack.** It
deterministically surfaces the highest-signal items — superinvestor/watchlist hits,
activist stakes, insider buys, completed M&A / distress / control-change 8-Ks,
**business CATALYSTS read from filing bodies** (lucrative contracts, capacity
expansions, FDA/clinical, partnerships, patents — financing/comp filtered out), and
**EXTERNAL feeds** (federal contract awards via USAspending, FDA drug approvals +
ClinicalTrials Phase-3 readouts, USPTO patents) — and is **never silently truncated**:
generous per-bucket caps keep it readable and any overflow appears as an explicit
"… N more in window" line (the verbose SEC FILINGS list below it is capped for
readability). This guarantees the top signals are never lost in a large window.

> The tool's purpose is BROAD asymmetric opportunities — not just ownership. Weight the
> CATALYSTS / FDA / CONTRACTS signals as highly as activist/insider ones: a Phase-3
> readout, a needle-moving federal contract, or a new lucrative supply deal is exactly
> the forward catalyst the rubric wants. The `→` line gives the specifics; judge
> materiality-to-size and the mechanism as usual.
- Treat **SCHEDULE 13D and 13D/A equally** — amendments are where activists *escalate*
  (e.g. GameStop→eBay, Pershing Square→QSR). Do NOT filter to "new 13D only".
- A **13D/A shows the CURRENT %**, not whether the investor ADDED or TRIMMED. Each
  ownership row now carries a **`detail`** field (the `→` line in the pack) with a
  direction hint (NEW / ADDED / TRIMMED / technical) + a verbatim Item 5(c) snippet —
  read it. Better still: rows with a **`Δ pct` line** ("8.1% → 9.9% (ADDED vs prior
  stored filing)") carry the direction computed from OUR OWN stored history — trust
  that over the prose hint. A **`⚠ SWITCHED 13G → 13D`** line means a passive holder
  turned active — one of the strongest activist-intent signals; weight it highly.
  For a borderline call, open the filing. A stake below 5% on a 13D/A often
  means an exit; a long-term holder's routine amendment (e.g. Ackman/QSR, held since
  2014) is NOT a new catalyst even though it surfaces as an activist signal.
- **Insider buys carry quality markers**: "10b5-1 PLANNED transaction" = pre-scheduled,
  mechanically executed — much weaker than a "discretionary open-market buy". "First
  buy by this insider in stored history" = novelty cue (history depth = the DB's).
  C-suite (CEO/CFO) buys outweigh director buys. Lone buys under ~$25k are floored out
  of the priority list (they remain in the OWNERSHIP section).
- **`px … % since event` lines are the priced-in check**: a big move since the event
  means the market has already re-rated it — down-rank; a flat price on a strong
  catalyst is the asymmetric setup. Absence of the line just means no quote data.
- The CATALYST bucket is **pre-sorted by materiality-to-size** (body $ / mcap), not
  recency — the top entries are the candidate needle-movers. The `earnings_strength`
  tag (record quarter / beats) is deliberately NOT a lucrative catalyst — backward-
  looking results are not a forward mechanism unless guidance actually changed.
- A **★ WATCHLIST section** (if present) lists everything touching user-watched
  tickers — always address these first, even before superinvestor hits.
- Never hand-roll a narrower query than the pack itself; if you must query the DB
  directly, include amendments and the superinvestor slice.

For each candidate, judge:

1. **Catalyst type & strength** — what kind of event (8-K item, activist stake,
   insider buy, contract, M&A, offering), and how strong/durable.
2. **Materiality relative to size** — is this big *for this company*? Use the
   market cap in the pack. A $200M order means more to a $2B company than a $2T one.
   **Prioritise high materiality-to-size.** Enriched filing lines carry a
   deterministic `≈$N mentioned in body (~X% of mcap)` hint — a strong materiality
   cue, but it is the LARGEST figure anywhere in the body, which is often a
   HISTORICAL amount recited as background (8-Ks restate old deal terms), or a
   financing amount. Before citing a magnitude, read the item's OPENING sentences —
   that's where the NEW event lives — and confirm the figure's DATE belongs to the
   new event, not to a transaction that already closed. (Learned the hard way:
   a "$220M" body mention was an Aug-2025 closing; the actual June-2026 event was
   a $4M final holdback receipt.)
3. **Novelty / under-the-radar** — likely not yet widely noticed or priced in?
   **Use the COVERAGE count: prefer strong-catalyst + LOW-coverage names** — that's
   the asymmetric sweet spot. Down-rank the obvious mega-cap headline everyone sees.
4. **Source credibility** — SEC filing > wire/PR > financial news > single-source.
   Label trust explicitly on every item.
5. **Plausible forward impact** — could this meaningfully change future
   revenue / earnings / cash flow or trigger a re-rating? Reason about the *mechanism*.

### The bar — be a tough filter, flag FEW high-quality leads

**"Asymmetric" = a signal that creates a GREAT FUTURE OPPORTUNITY for the stock:**
a genuine, under-appreciated catalyst that could *materially* change the company's
future revenue / earnings / cash flow or trigger a re-rating, with limited or known
downside. You are a skeptical gatekeeper, not a list-maker. Flag a lead ONLY if it
clears **every** gate:

1. **Real forward catalyst with a stated mechanism** — you can say *how* this changes
   future revenue/earnings/cash flow or drives a re-rating. Not a
   disclosure/compliance/process event (a routine 8-K 5.07 vote, boilerplate 7.01
   Reg FD, automatic Form-4 grant, periodic 10-Q with nothing new).
2. **Material to size** — needle-moving relative to the company's market cap.
3. **Under-appreciated** — likely not yet widely noticed or priced in (LOW coverage).
   Skip obvious, well-covered mega-events.
4. **Substantiated** — a hard SEC filing with real substance, or strongly corroborated
   (filing + wire). Never flag a thin headline or single-source rumour.
5. **Asymmetric payoff** — meaningful upside *if it plays out*.

If a candidate fails ANY gate, do **not** flag it — move it to a terse "Watch" line or
omit it. **Prefer few leads. Most days, "Nothing notable today." is the correct
answer** — say it plainly rather than manufacturing conviction.

**Almost never a "great future opportunity" — drop or down-rank to Watch:** routine
exec changes (8-K 5.02) absent strategic context; shareholder-vote results (5.07);
boilerplate Reg FD (7.01); periodic reports (10-K/10-Q) with no surprise; passive
13G stakes by index funds (BlackRock/Vanguard/State Street/FMR); structured-note
offerings; self-filings; anything whose materiality can't be established from the data.

### Output format (only leads that clear the bar, highest conviction first)

```
#. **TICKER — Company Name**
   - **What happened:** <concise, one or two lines>
   - **Why asymmetric:** <materiality relative to size + the mechanism, 1–2 lines>
   - **Trust:** <SEC filing | Wire/PR | News | Unconfirmed> · **Conviction:** <High | Medium | Low>
   - **Source:** [SEC filing / outlet](link)
```

For an **ownership** lead, lead "What happened" with the **direction/intent** from the
pack's `→ detail` line — NEW / ADDED / TRIMMED / BUYOUT / MERGER / BOARD-nominees —
and NEVER imply a fresh stake for a long-held holder's routine amendment (e.g.
Ackman/QSR). Always use this per-lead bullet structure — not tables.

Then:
- A short **"Watch, not act"** section for weaker / ambiguous items (terser).
- If the day is quiet, a single line: **"Nothing notable."**
- End with one line: _Research only, not investment advice._

Keep SEC FILINGS visually separate from NEWS, and always include the source link.

### Follow-up questions about a company

Query the SQLite store (`run.bat ask "<company>"`, add `--fetch` for a fresh targeted
EDGAR pull) and answer with **sourced specifics**. If you don't have the data, say so
and offer to fetch it — never fabricate.

---

## Architecture (one-liner per layer)

Deterministic Python does plumbing only: `ingest_edgar` (daily-index + 8-K items) /
`ingest_news` (wires + RSS, company-tagged) / `ingest_ownership` (13D/13G/Form 4,
superinvestor-matched) / `ingest_external` (USAspending contracts + openFDA/ClinicalTrials
readouts + PatentsView, universe name-matched) → `store` (SQLite, dedupe, catch-up) →
`prefilter` (drop noise, tag catalysts) → `filing_body` (reads catalyst 8-K bodies + re-tags,
so a "material agreement" reveals the actual deal) → `context_pack` (the small packet you
read). The agent does all the judgement. `scoring/llm_scorer.py` is OFF by default (no API
key needed). Patents need a free `PATENTSVIEW_API_KEY` env var; without it that feed is skipped.

## Hard constraints

- Free sources only; local-only; Windows; SEC fair-access (User-Agent + ~8 req/s).
- Distinguish SEC filings from news/unconfirmed, always.
- Cite the source link for every item, all the way to the output.
- Never fabricate data or present samples as real. If a source is down, say so.
- Times are US/Eastern. "Last 24h" = `lookback_hours` in `config/settings.yaml`.
