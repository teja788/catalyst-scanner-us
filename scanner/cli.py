"""Typer CLI for catalyst-scanner-us.

This is the Milestone-1 skeleton: every command exists and is wired to the
console, but the data-bearing commands print a clear "not yet implemented"
notice pointing at the milestone that will build them. This lets us verify the
plumbing (venv, imports, CLI dispatch, config loading) before any network code.

Commands (Section 13 of the build spec):
  setup-universe  (re)build the top-N US-by-market-cap universe + ticker->CIK map  [M2]
  refresh         run all ingesters (catch-up since last run)                      [M3-M6]
  scan            refresh -> prefilter -> write the context pack                   [M7-M9]
  ask             print stored data relevant to a question                        [M10]
  digest          save a dated markdown digest                                    [M10]
  schedule        print/install the Windows Task Scheduler job (ET-aware)         [M11]
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from scanner import __app_name__, __version__
from scanner.config import load_settings, resolve_path

log = logging.getLogger(__name__)

app = typer.Typer(
    name=__app_name__,
    help="On-demand US-market catalyst scanner (local, free sources, research-only).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _tz() -> ZoneInfo:
    """The configured display timezone (ET by default)."""
    return ZoneInfo(load_settings().get("timezone", "America/New_York"))


@app.callback()
def _main() -> None:
    """Configure UTF-8 console + file logging once, before any command runs."""
    # Windows consoles default to a legacy codepage (cp1252); force UTF-8 so
    # em-dashes, curly quotes and the like render instead of mojibake.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    log_dir = resolve_path("runtime/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        handlers=[logging.FileHandler(log_dir / "scanner.log", encoding="utf-8")],
        force=True,  # reconfigure cleanly even if basicConfig ran before
    )


def _todo(command: str, milestone: str) -> None:
    """Uniform 'planned but not built yet' notice, so the CLI is honest about scope."""
    console.print(
        Panel.fit(
            f"[yellow]'{command}'[/yellow] is scaffolded but not implemented yet.\n"
            f"It will be built in [bold]{milestone}[/bold].",
            title="Not yet implemented",
            border_style="yellow",
        )
    )


def _fmt_usd(v: float | None) -> str:
    """Human-readable market cap: $3.0T, $850.2B, $1.2M, or em-dash if unknown."""
    if not v:
        return "—"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if v >= div:
            return f"${v / div:.1f}{unit}"
    return f"${v:,.0f}"


@app.command()
def version() -> None:
    """Show app name, version, and the active universe/lookback config."""
    s = load_settings()
    uni = s.get("universe", {})
    table = Table(show_header=False, box=None)
    table.add_row("App", f"[bold]{__app_name__}[/bold]")
    table.add_row("Version", __version__)
    table.add_row("Exchanges", ", ".join(uni.get("include_exchanges", [])) or "?")
    table.add_row("Universe size (top_n)", str(uni.get("top_n")))
    table.add_row("Include ADRs", str(uni.get("include_adrs")))
    table.add_row("Lookback (hours)", str(s.get("lookback_hours")))
    table.add_row("Timezone", str(s.get("timezone")))
    table.add_row("Scoring mode", str(s.get("scoring", {}).get("mode")))
    table.add_row("EDGAR User-Agent", str(s.get("edgar", {}).get("user_agent")))
    console.print(Panel(table, title=__app_name__, border_style="cyan"))


@app.command(name="setup-universe")
def setup_universe() -> None:
    """(Re)build the top-N US-by-market-cap universe + ticker->CIK map."""
    from scanner.universe import _norm, build_map, load_map

    with console.status("[cyan]Fetching Nasdaq Trader lists + SEC CIK map + market caps..."):
        stats = build_map()

    table = Table(title="Universe built", border_style="green")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Listed common stocks", f"{stats['listed_common']:,}")
    table.add_row("Dropped (no SEC CIK)", f"{stats['no_cik_dropped']:,}")
    table.add_row("Unique companies (by CIK)", f"{stats['unique_companies']:,}")
    table.add_row("Screener rows / exchange", str(stats["screener_rows"]))
    table.add_row("Universe size (top_n)", f"[bold green]{stats['universe_size']:,}[/bold green]")
    table.add_row("Missing mcap in universe", str(stats["missing_mcap_in_universe"]))
    table.add_row("Market-cap top", _fmt_usd(stats["mcap_top"]))
    table.add_row(f"Market-cap floor (#{stats['universe_size']})", _fmt_usd(stats["mcap_floor"]))
    table.add_row("Exchange breakdown", str(stats["exchange_breakdown"]))
    console.print(table)

    # Spot-check well-known names so CIK mapping is eyeball-verifiable.
    uni = load_map()
    by_norm = {_norm(c["ticker"]): (i, c) for i, c in enumerate(uni, 1)}
    spot = Table(title="Spot-check — rank · ticker · CIK · market cap", border_style="cyan")
    spot.add_column("#", justify="right")
    spot.add_column("Ticker")
    spot.add_column("CIK")
    spot.add_column("Market cap", justify="right")
    spot.add_column("Company")
    for t in ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "BRK-B", "TSLA"):
        hit = by_norm.get(_norm(t))
        if hit:
            i, c = hit
            spot.add_row(str(i), c["ticker"], c["cik"], _fmt_usd(c.get("market_cap")), c.get("name", ""))
        else:
            spot.add_row("—", t, "[yellow]not in top-N[/yellow]", "", "")
    console.print(spot)
    console.print(f"[dim]Map written to {stats['out_dir']} (us_universe.json + .csv)[/dim]")


# Reusable time-window options so every windowed command exposes the same flags.
_HOURS_OPT = typer.Option(None, "--hours", "-H", help="Look back this many hours (overrides catch-up cursor).")
_DAYS_OPT = typer.Option(None, "--days", "-D", help="Look back this many days. Combines with --hours.")


def resolve_window(hours: int | None, days: int | None) -> tuple[datetime | None, str | None]:
    """Turn --hours/--days into an explicit `since` instant (ET) + a label.

    Returns (None, None) when neither is given, so callers keep their default
    behaviour (catch-up cursor / settings.lookback_hours).
    """
    if hours is None and days is None:
        return None, None
    total = (days or 0) * 24 + (hours or 0)
    if total <= 0:
        raise typer.BadParameter("Window must be positive (use --hours and/or --days > 0).")
    since = datetime.now(_tz()) - timedelta(hours=total)
    label = (f"{days}d{hours}h" if days and hours else f"{days}d" if days else f"{hours}h")
    return since, label


def _refresh_all(since_override: datetime | None = None,
                 sources: set[str] | None = None) -> dict[str, dict]:
    """Run every ingester with catch-up, store with dedupe, track runs.

    Each source is isolated so one failure still lets the others proceed.
    """
    run = sources or {"edgar", "news", "ownership", "external"}
    from scanner import ingest_edgar, ingest_external, ingest_news, ingest_ownership, store
    from scanner.http import PoliteSession
    from scanner.universe import load_map

    store.init_db()
    store.sync_companies(load_map())
    session = PoliteSession()
    results: dict[str, dict] = {}

    def _do(source: str, fetch_and_store) -> None:
        try:
            fetched, new = fetch_and_store()
            store.mark_run(source, fetched, "ok")
            results[source] = {"fetched": fetched, "new": new, "status": "ok"}
        except Exception as exc:  # noqa: BLE001 - per-source isolation
            store.mark_run(source, 0, "error", note=str(exc)[:200])
            results[source] = {"fetched": 0, "new": 0, "status": f"error: {exc}"}
            log.warning("refresh source %s failed: %s", source, exc)

    def _edgar():
        since = since_override or store.get_last_success("edgar")
        items = ingest_edgar.ingest(session=session, since=since)
        return len(items), store.upsert_filings(items)

    def _news():
        items = ingest_news.ingest(session=session)
        return len(items), store.upsert_news(items)

    def _ownership():
        since = since_override or store.get_last_success("ownership")
        items = ingest_ownership.ingest(session=session, since=since)
        return len(items), store.upsert_ownership(items)

    def _external():
        # Federal contracts / FDA-clinical / patents — slower-moving; pull last 30d, dedupe.
        c = ingest_external.ingest(days=30, session=session)
        return sum(c.get(k, 0) for k in ("contracts", "fda", "patents")), c.get("new", 0)

    if "edgar" in run:
        _do("edgar", _edgar)
    if "news" in run:
        _do("news", _news)
    if "ownership" in run:
        _do("ownership", _ownership)
    if "external" in run:
        _do("external", _external)
    return results


def _render_refresh(results: dict[str, dict]) -> None:
    table = Table(title="Refresh — per-source results", border_style="cyan")
    table.add_column("Source")
    table.add_column("Fetched", justify="right")
    table.add_column("New (deduped)", justify="right")
    table.add_column("Status")
    for src, r in results.items():
        colour = "green" if r["status"] == "ok" else "red"
        table.add_row(src, str(r["fetched"]), str(r["new"]), f"[{colour}]{r['status']}[/{colour}]")
    console.print(table)


@app.command()
def refresh(hours: int = _HOURS_OPT, days: int = _DAYS_OPT) -> None:
    """Run all ingesters (catch-up since last run) and update SQLite.

    With --hours/--days, fetch that far back instead of from the last-run cursor.
    """
    since, label = resolve_window(hours, days)
    note = f" (window: last {label})" if label else " (catch-up since last run)"
    console.print(f"[dim]Refreshing EDGAR filings + news + ownership{note}...[/dim]")
    _render_refresh(_refresh_all(since_override=since))


@app.command()
def scan(skip_refresh: bool = typer.Option(False, "--skip-refresh",
         help="Use already-stored data; don't fetch first (faster)."),
         hours: int = _HOURS_OPT, days: int = _DAYS_OPT) -> None:
    """Catch-up refresh, then pre-filter, then write the context pack.

    Intended use: run this, then tell the agent "read the context pack and give
    me today's asymmetric signals." Use --hours/--days to widen the window.
    """
    from scanner.context_pack import build_context_pack

    since, label = resolve_window(hours, days)
    note = f" [cyan](window: last {label})[/cyan]" if label else ""
    if not skip_refresh:
        console.print(f"[dim]Catch-up refresh{note}...[/dim]")
        _render_refresh(_refresh_all(since_override=since))
    else:
        console.print(f"[dim]--skip-refresh: using stored data{note} (cached bodies only — no network).[/dim]")

    with console.status("[cyan]Pre-filtering + assembling context pack..."):
        # --skip-refresh promises "no fetching": use only cached bodies/prices.
        stats = build_context_pack(since=since, fetch_bodies=not skip_refresh)

    table = Table(title="Context pack assembled", border_style="green")
    table.add_column("Section")
    table.add_column("Count", justify="right")
    table.add_row("SEC filings", f"{stats['filings']} ({stats['filings_substantive']} substantive)")
    table.add_row("Ownership (flagged)", str(stats["ownership_flagged"]))
    table.add_row("Company news", str(stats["company_news"]))
    table.add_row("Market-wide news", str(stats["market_news"]))
    console.print(table)
    console.print(f"\n[bold]Context pack:[/bold] {stats['md_path']}")
    console.print("[dim]Next: ask the agent to read the context pack and rank today's asymmetric signals.[/dim]")


def _resolve_companies(question: str, universe: list[dict]) -> list[dict]:
    """Lenient company resolver for a user's question (recall-first): whole-word
    match on ticker or any alias. Skips common-word tickers (ON, IT, ALL, ...) so
    'any news on NVDA' doesn't resolve ON Semiconductor — they still match by name."""
    import re
    from scanner.ingest_news import TICKER_STOPWORDS
    q = question.lower()
    hits, seen = [], set()
    for c in universe:
        if c["cik"] in seen:
            continue
        tk = (c.get("ticker") or "").upper()
        # Old universe files carry the ticker inside aliases too — strip it there
        # so the stopword guard below can't be bypassed ("any news FOR NVDA" must
        # not resolve Forestar (FOR) via its 'for' alias).
        cands = [a for a in c.get("aliases", []) if a != tk.lower()]
        if tk and tk not in TICKER_STOPWORDS:
            cands.append(tk.lower())
        for cand in cands:
            if len(cand) < 2:
                continue
            if re.search(rf"(?<![a-z0-9]){re.escape(cand)}(?![a-z0-9])", q):
                hits.append(c)
                seen.add(c["cik"])
                break
    return hits


def _short(text: str, n: int = 96) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def _print_ask(filings: list[dict], news: list[dict], ownership: list[dict]) -> None:
    console.print(f"\n[bold]SEC filings ({len(filings)}):[/bold]")
    for f in filings or []:
        console.print(f"  [green][{f.get('form_type','')}][/green] {(f.get('filed_at') or '')[:16]}  {_short(f.get('headline',''))}")
        if f.get("filing_url"):
            console.print(f"    [dim]{f['filing_url']}[/dim]")
    if not filings:
        console.print("  [dim]none[/dim]")
    console.print(f"\n[bold]Ownership ({len(ownership)}):[/bold]")
    for o in ownership or []:
        flag = "BUY" if o.get("is_buy") else ("13D" if o.get("is_activist") else o.get("side", ""))
        console.print(f"  [yellow][{o.get('form_type','')}][/yellow] {flag} — {o.get('filer_name','?')} {(o.get('filed_at') or '')[:10]}")
    if not ownership:
        console.print("  [dim]none[/dim]")
    console.print(f"\n[bold]News ({len(news)}):[/bold]")
    for n in news or []:
        console.print(f"  [blue][{n.get('source','')}][/blue] {(n.get('published_at') or '')[:16]}  {_short(n.get('headline',''))}")
        if n.get("url"):
            console.print(f"    [dim]{n['url']}[/dim]")
    if not news:
        console.print("  [dim]none[/dim]")


@app.command()
def ask(question: str = typer.Argument(..., help="A question naming a company / ticker."),
        fetch: bool = typer.Option(False, "--fetch", help="Do a fresh targeted EDGAR pull for the resolved company first."),
        hours: int = _HOURS_OPT, days: int = _DAYS_OPT) -> None:
    """Print stored data relevant to a follow-up question (for the agent to reason over)."""
    from scanner import ingest_edgar, store
    from scanner.http import PoliteSession
    from scanner.prefilter import tag_keywords
    from scanner.universe import load_map

    since, label = resolve_window(hours, days)
    since_iso = since.isoformat() if since else None
    store.init_db()
    universe = load_map()
    companies = _resolve_companies(question, universe)
    tags = tag_keywords(question)

    if fetch and companies:
        fetch_since = since or (datetime.now(_tz()) - timedelta(days=14))
        session = PoliteSession()
        new = 0
        with console.status(f"[cyan]Fresh EDGAR pull for {', '.join(c['ticker'] for c in companies)}..."):
            for c in companies:
                new += store.upsert_filings(ingest_edgar.fetch_company(session, c["cik"], since=fetch_since))
        console.print(f"[dim]Fetched fresh filings (+{new} new).[/dim]")

    console.print(Panel.fit(
        f"[bold]Question:[/bold] {question}\n"
        f"Resolved: {', '.join(c['ticker'] for c in companies) or '—'}  |  "
        f"Catalyst tags: {', '.join(tags) or '—'}  |  "
        f"Window: {('last ' + label) if label else 'recent'}",
        title="ask", border_style="cyan"))

    ciks = [c["cik"] for c in companies]
    if ciks:
        _print_ask(store.filings_for_ciks(ciks, since_iso=since_iso),
                   store.news_for_ciks(ciks, since_iso=since_iso),
                   store.ownership_for_ciks(ciks, since_iso=since_iso))
    else:
        console.print("[yellow]No company recognised. Name a ticker (e.g. 'NVDA') or company.[/yellow]")


@app.command()
def watch(action: str = typer.Argument(..., help="add | remove | list"),
          ticker: str = typer.Argument(None, help="Ticker (for add/remove)."),
          note: str = typer.Option("", "--note", help="Why it's on the watchlist.")) -> None:
    """Manage the watchlist. Watched tickers float to the TOP of the context pack
    (a ★ WATCHLIST section above even the superinvestor hits)."""
    from scanner import store
    from scanner.universe import _norm, load_map

    store.init_db()
    action = action.lower()
    if action == "list":
        rows = store.get_watchlist()
        if not rows:
            console.print("[dim]Watchlist is empty. Add with: run.bat watch add NVDA --note \"...\"[/dim]")
            return
        table = Table(title="Watchlist", border_style="cyan")
        table.add_column("Ticker")
        table.add_column("CIK")
        table.add_column("Added")
        table.add_column("Note")
        for w in rows:
            table.add_row(w.get("ticker") or "?", w["cik"], (w.get("added_at") or "")[:10], w.get("note") or "")
        console.print(table)
        return
    if not ticker:
        raise typer.BadParameter("watch add/remove needs a ticker.")
    hit = next((c for c in load_map() if _norm(c.get("ticker", "")) == _norm(ticker)), None)
    if hit is None:
        console.print(f"[yellow]{ticker!r} not found in the universe map.[/yellow]")
        raise typer.Exit(1)
    if action == "add":
        store.add_to_watchlist(hit["cik"], hit.get("ticker", ""), note)
        console.print(f"[green]★ Watching {hit['ticker']} ({hit.get('name','')}).[/green]")
    elif action == "remove":
        gone = store.remove_from_watchlist(hit["cik"])
        console.print(f"[green]Removed {hit['ticker']}.[/green]" if gone
                      else f"[yellow]{hit['ticker']} was not on the watchlist.[/yellow]")
    else:
        raise typer.BadParameter("action must be add, remove, or list.")


@app.command(name="publish-log")
def publish_log_cmd(no_push: bool = typer.Option(False, "--no-push",
                    help="Write docs/index.md but do NOT git commit/push it.")) -> None:
    """Publish the local research log to GitHub Pages (docs/index.md).

    The raw log stays private/local; this copies it to the Pages source and
    pushes ONLY that file. The published page is PUBLIC — publish deliberately.
    """
    from scanner.publish_log import publish

    try:
        r = publish(push=not no_push)
    except (FileNotFoundError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]{r['status']}[/green] → {r['page']}")
    if r["url"]:
        console.print(f"[bold]Page:[/bold] {r['url']} [dim](public; first deploy takes ~1 min. "
                      "One-time setup: repo Settings → Pages → Deploy from branch → main /docs)[/dim]")


@app.command()
def digest(hours: int = _HOURS_OPT, days: int = _DAYS_OPT) -> None:
    """Build the context pack and save a dated markdown digest to digests/.

    In agent mode (default) the digest is the deterministic candidate set with
    sources — the ranked interpretation is produced live by the agent reading it.
    """
    from pathlib import Path
    from scanner.context_pack import build_context_pack
    from scanner.config import resolve_path

    since, _label = resolve_window(hours, days)
    stats = build_context_pack(since=since)
    md = Path(stats["md_path"]).read_text(encoding="utf-8")
    note = ("_Deterministic candidate set (sourced leads). In agent mode the ranking is "
            "produced live by the agent reading this pack per CLAUDE.md._")
    today = datetime.now(_tz()).strftime("%Y-%m-%d")
    digest_dir = resolve_path(load_settings().get("output", {}).get("digest_dir", "digests"))
    digest_dir.mkdir(parents=True, exist_ok=True)
    path = digest_dir / f"{today}.md"
    path.write_text(f"# Daily Digest — {today}\n\n{note}\n\n---\n\n{md}", encoding="utf-8")
    console.print(f"[green]Digest saved:[/green] {path}")


@app.command()
def schedule(install: bool = typer.Option(False, "--install", help="Actually create the scheduled tasks."),
             remove: bool = typer.Option(False, "--remove", help="Delete the scheduled tasks.")) -> None:
    """Print (or install) the Windows Task Scheduler jobs for background refresh.

    Two jobs: a recurring refresh while the laptop is on, plus an after-US-close
    catch-up. Times are LOCAL (IST); the evening job maps to ~4:30pm ET.
    """
    import subprocess

    cfg = load_settings().get("schedule", {})
    every = int(cfg.get("refresh_every_min", 45))
    evening = str(cfg.get("evening_catchup_local", "02:00"))
    bat = resolve_path("scheduled_refresh.bat")
    name_45, name_eve = "catalyst-us-refresh", "catalyst-us-evening-catchup"
    # /TR gets embedded quotes so a project path containing spaces still works.
    create_45 = ["schtasks", "/Create", "/TN", name_45, "/TR", f'"{bat}"', "/SC", "MINUTE", "/MO", str(every), "/F"]
    create_eve = ["schtasks", "/Create", "/TN", name_eve, "/TR", f'"{bat}"', "/SC", "DAILY", "/ST", evening, "/F"]

    if remove:
        for name in (name_45, name_eve):
            r = subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], capture_output=True, text=True)
            console.print(f"[{'green' if r.returncode == 0 else 'yellow'}]{(r.stdout or r.stderr).strip()}[/]")
        return

    if install:
        for cmd in (create_45, create_eve):
            r = subprocess.run(cmd, capture_output=True, text=True)
            ok = r.returncode == 0
            console.print(f"[{'green' if ok else 'red'}]{(r.stdout or r.stderr).strip()}[/]")
        console.print("[dim]Installed. Remove with: run.bat schedule --remove[/dim]")
    else:
        console.print(Panel(
            "Run in an [bold]Administrator[/bold] Command Prompt (or use [bold]--install[/bold]):\n\n"
            f"[cyan]schtasks /Create /TN {name_45} /TR \"{bat}\" /SC MINUTE /MO {every} /F[/cyan]\n\n"
            f"[cyan]schtasks /Create /TN {name_eve} /TR \"{bat}\" /SC DAILY /ST {evening} /F[/cyan]\n\n"
            "Remove later with [cyan]run.bat schedule --remove[/cyan].",
            title="Windows Task Scheduler", border_style="cyan"))

    console.print(
        f"\n[bold]Timezone note (you're on IST):[/bold] tasks run on LOCAL machine time. The daily "
        f"catch-up at [bold]{evening} IST[/bold] ≈ 10:30–11:30pm ET the previous day — after EDGAR's "
        "~10pm ET acceptance cutoff, so the FULL day's filings (including the dense 4–6pm ET "
        "after-market 8-K wave) have landed.\n"
        "[bold yellow]Laptop-only caveat:[/bold yellow] coverage = \"whenever the laptop is on and the task ran.\" "
        "Because [bold]scan[/bold]/[bold]refresh[/bold] always catch up since the last successful run, opening "
        "the tool at any random time still pulls everything since then — you never miss filings to a sleep.")


if __name__ == "__main__":
    app()
