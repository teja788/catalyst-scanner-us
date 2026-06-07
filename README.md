# catalyst-scanner-us

A **local, free, on-demand research tool** for discovering *asymmetric opportunities*
among the **top US-listed companies by market cap** (Nasdaq + NYSE + NYSE American) —
an idea-generation tool to surface leads to research.

> ⚠️ Personal **research / idea-generation** tool — **not** investment advice, **not** a
> buy/sell signal generator, and **not** redistribution of SEC/news/wire data. It surfaces
> sourced *leads*; you research and decide.

US sibling of the India "catalyst-scanner" — **same architecture**, US data layer
(SEC EDGAR instead of BSE, US wires/RSS instead of Indian RSS, 13D/13G/Form 4 instead
of bulk/block/SAST), keyed on **SEC CIK** instead of ISIN, on **US/Eastern** time.

## How it works

Deterministic Python does the fragile, cacheable plumbing — fetch → dedupe → store →
pre-filter — and assembles a compact **context pack**. The *reasoning* (ranking catalysts
by materiality-vs-size, novelty, trust, plausible impact) is done **live by the agent**
(Claude Code / Codex) against the rubric in `CLAUDE.md`. No LLM API key is required.

```
INGEST (SEC EDGAR filings + news/wire RSS + 13D/13G/Form 4 ownership)
  -> SQLite store (dedupe + catch-up since last run)
  -> PRE-FILTER (drop routine noise, tag candidate catalysts incl. 8-K item codes)
  -> CONTEXT PACK (runtime/context_pack.md)
  -> REASONING (live agent now; optional LLM-API stub later)
```

**Trust order (US):** SEC filing (highest) > press-release wire / company PR > financial
news > single-source/unconfirmed. Every item carries its **source link** to the output.

## Setup (Windows)

```bat
setup.bat            REM create .venv + install deps (one time)
run.bat version      REM sanity check: prints version + active config
run.bat --help       REM see all commands
dashboard.bat        REM optional: launch the Streamlit web dashboard (localhost:8501)
```

## Dashboard (optional)

`dashboard.bat` opens a Streamlit UI over the stored scan data — a thin layer over the
same `scanner` functions (no new data logic):
- **Window** (days/hours back) only *displays* stored data; **Update / Full refresh**
  do the same catch-up the CLI does; **Backfill** fills only a missing older gap.
- Tabs: **Signals** (catalyst-tagged filings + flagged ownership), **Filings**
  (filterable), **Ownership** (13D/13G/Form 4), **Chat** (sourced retrieval).
- **AI rank + chat use your Claude Code login by default — no API key needed** (the
  dashboard shells out to `claude -p` headlessly). Or pick Claude/OpenAI with an API
  key (session memory only, never written to disk). Launch via the desktop icon, not
  from inside a Claude Code terminal (nested sessions are blocked).

## Commands

| Command | What it does | Status |
|---|---|---|
| `setup-universe` | Build top-N US-by-mcap universe + ticker→CIK map | Milestone 2 |
| `refresh` | Run all ingesters (catch-up since last run) | Milestones 3–6 |
| `scan` | `refresh` → pre-filter → write context pack | Milestones 7–9 |
| `ask "<q>"` | Print stored data relevant to a follow-up | Milestone 10 |
| `digest` | Save a dated markdown digest | Milestone 10 |
| `schedule` | Print/install the Windows Task Scheduler jobs (ET-aware) | Milestone 11 |

## Configuration (edit freely)

- `config/settings.yaml` — universe (exchanges, **`top_n`**), lookback, rate limits, EDGAR User-Agent, timezone
- `config/sources.yaml` — EDGAR endpoints, Nasdaq universe sources, news/wire RSS feeds
- `config/catalysts.yaml` — 8-K item-code map + catalyst keyword taxonomy
- `config/superinvestors.yaml` — marquee/activist filer watchlist
- `config/noise_filters.yaml` — routine items to drop / down-rank

**Universe size:** `top_n` in `settings.yaml` starts at **500** for fast iteration; set it to
**5000** for full coverage — no code change required.

## Build milestones

| # | Milestone | Status |
|---|---|---|
| 1 | Scaffold (venv, config, CLI skeleton, `.bat`) | ✅ done |
| 2 | Universe: top-N US-by-mcap + ticker→CIK map | ✅ done (500 names; flip `top_n`→5000 anytime) |
| 3 | EDGAR ingester (daily-index + 8-K item parsing) | ✅ done (drops 424B2/FWP structured-note noise) |
| 4 | News + wires ingester (company tagging) | ✅ done (10 live feeds; precision-guarded tagging) |
| 5 | Ownership ingester (13D/13G + Form 4 + 13F) | ✅ done (insider buys, activist stakes, superinvestor match) |
| 6 | Storage + dedupe | ✅ done (`refresh` live; re-runs insert 0 new) |
| 7 | Pre-filter (noise + candidate tagging) | ✅ done (8-K items + keywords; procedural-only flagged) |
| 8 | Context pack assembler | ✅ done (`scan` live; trust-separated md+json, mcap + coverage) |
| 9 | `CLAUDE.md` rubric + live end-to-end `scan` | ✅ done — **usable end-to-end** |
| 10 | `ask` + `digest` | ✅ done (stored query + targeted `--fetch`; dated digests) |
| 11 | Windows scheduler (ET-aware) | ✅ done (45-min + after-close catch-up; IST↔ET documented) |
| 12 | Phase-2 stubs (FDA, contracts, patents, short interest, FRED, LLM scorer, price adapter, watchlist, notify) | ✅ done (clearly-marked TODOs, off by default) |

After Milestone 9 the tool is usable end-to-end; 10–12 are enhancements.
