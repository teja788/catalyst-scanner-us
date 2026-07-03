"""catalyst-scanner-us — Streamlit dashboard.

A thin presentation layer over the existing `scanner` functions:
  - "Days back" / "Hours back" only DISPLAY stored data (zero downloading).
  - "Update" does an INCREMENTAL catch-up (only new data since the last fetch).
  - "Backfill" fetches ONLY the missing older gap, never re-downloading.
  - The AI rank/chat panels are OFF until you provide a Claude or OpenAI key
    (key held in session memory only, never written to disk).

US sibling of the India dashboard: SEC filings (EDGAR) + 13D/13G/Form-4 ownership
+ wires/news, keyed on CIK, market caps in USD, US/Eastern time.

Run:  dashboard.bat   (or  .venv\\Scripts\\streamlit run dashboard.py)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

from scanner import store
from scanner.cli import _refresh_all, _resolve_companies
from scanner.config import load_settings
from scanner.context_pack import build_context_pack
from scanner.prefilter import tag_keywords
from scanner.universe import load_map

ET = ZoneInfo(load_settings().get("timezone", "America/New_York"))
st.set_page_config(page_title="catalyst-scanner-us", page_icon="📡", layout="wide")


def _now() -> datetime:
    return datetime.now(ET)


def _fmt_usd(v) -> str:
    if not v:
        return "?"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if v >= div:
            return f"${v / div:.1f}{unit}"
    return f"${v:,.0f}"


@st.cache_data(ttl=600, show_spinner=False)
def _universe():
    return load_map()


@st.cache_data(show_spinner=False)
def _build_pack(total_h: int, version: int):
    """Assemble the pack for a window. Cached by (window SIZE, data-version) — a
    fresh `since` timestamp would change every rerun and defeat the cache, so the
    instant is computed in here. `version` bumps when data changes. Bodies come
    from the filing_text cache only (zero network), so body-derived catalyst tags
    still reach the dashboard pack."""
    since = _now() - timedelta(hours=total_h)
    stats = build_context_pack(since=since, enrich_bodies=True, fetch_bodies=False)
    with open(stats["json_path"], encoding="utf-8") as fh:
        pack = json.load(fh)
    with open(stats["md_path"], encoding="utf-8") as fh:
        md = fh.read()
    return stats, pack, md


def _bump():
    st.session_state["data_version"] = st.session_state.get("data_version", 0) + 1
    _build_pack.clear()


store.init_db()
universe = _universe()
idx = {c["cik"]: c for c in universe}
st.session_state.setdefault("data_version", 0)
st.session_state.setdefault("chat", [])

# --------------------------------------------------------------------------- #
# Sidebar — parameters
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("🤖 AI engine")
    from scanner.scoring import claude_code
    _cc_ok = claude_code.is_available()
    _opts = (["Claude Code (your login — no key)"] if _cc_ok else []) + ["Claude (API key)", "OpenAI (API key)"]
    engine = st.radio("Engine", _opts, label_visibility="collapsed")
    if engine.startswith("Claude Code"):
        provider, model, api_key, ai_on = "claude_code", None, "", True
        st.caption("✅ Uses your Claude Code login — no API key needed. "
                   "Launch via the desktop icon (not from a Claude Code terminal).")
    else:
        provider = "claude" if engine.startswith("Claude") else "openai"
        default_model = "claude-opus-4-8" if provider == "claude" else "gpt-5.5"
        model = st.text_input("Model", value=default_model, key=f"model_{provider}")
        api_key = st.text_input("API key", type="password",
                                help="Held in session memory only; never written to disk.")
        ai_on = bool(api_key.strip())
        st.caption("AI rank + chat enabled." if ai_on else "Add a key to enable AI rank + chat.")

    st.divider()
    st.header("⏱ Window")
    cwin = st.columns(2)
    days = cwin[0].number_input("Days back", 0, 90, 3)
    hours = cwin[1].number_input("Hours back", 0, 23, 0)
    total_h = days * 24 + hours
    if total_h == 0:
        total_h = int(load_settings().get("lookback_hours", 24))
        st.caption(f"Using settings default: {total_h}h")
    since = _now() - timedelta(hours=total_h)
    st.caption("Window only **displays** stored data — no downloading.")

    st.divider()
    st.header("🔄 Data")
    cov = store.coverage()
    f, o, n = cov["filings"], cov["ownership"], cov["news"]
    e = cov.get("external_catalysts", {"count": 0, "latest": None})
    st.caption(f"Filings: {f['count']} stored · latest {(f['latest'] or '—')[:10]}")
    st.caption(f"Ownership: {o['count']} · News: {n['count']} · "
               f"External: {e['count']} (latest {(e['latest'] or '—')[:10]})")

    c1, c2 = st.columns(2)
    if c1.button("Update news", help="Incremental — only new news since last fetch (~10s)"):
        with st.spinner("Fetching news (catch-up)..."):
            res = _refresh_all(sources={"news"})
        _bump()
        st.success(f"news {res.get('news', {}).get('new', 0)} new")
    if c2.button("Full refresh", help="EDGAR filings + news + ownership catch-up (minutes at 5,000 names)"):
        with st.spinner("Refreshing EDGAR + news + ownership (catch-up)..."):
            res = _refresh_all()
        _bump()
        st.success(f"filings {res.get('edgar', {}).get('new', 0)} · "
                   f"ownership {res.get('ownership', {}).get('new', 0)} new")

    # Gap-only backfill when the window reaches before stored filings.
    if f["earliest"] and since.isoformat() < f["earliest"]:
        if st.button("⤓ Backfill filings to window",
                     help="Fetches ONLY the missing older gap (filings + 13D/13G stakes; "
                          "skips the heavy Form-4 sweep)"):
            from scanner import ingest_edgar, ingest_ownership
            from scanner.http import PoliteSession
            gap_until = datetime.fromisoformat(f["earliest"])
            session = PoliteSession()
            with st.spinner(f"Backfilling filings {since.date()} → {gap_until.date()}..."):
                items = ingest_edgar.ingest(session=session, since=since, until=gap_until)
                added = store.upsert_filings(items)
                # 13D/13G for the same gap — otherwise old windows show filings
                # with no activist context. Form 4s stay excluded (25k+ fetches).
                own_items = ingest_ownership.ingest(
                    session=session, since=since, until=gap_until,
                    forms={"SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A"})
                own_added = store.upsert_ownership(own_items)
            _bump()
            st.success(f"Backfilled {added} older filings + {own_added} 13D/13G rows.")

# --------------------------------------------------------------------------- #
# Header + pack
# --------------------------------------------------------------------------- #
st.title("📡 catalyst-scanner-us")
st.caption("Asymmetric-opportunity scanner for the top US-listed companies "
           "(SEC EDGAR filings + 13D/13G/Form-4 ownership + wires/news). "
           "Research leads only — **not investment advice**.")

stats, pack, md = _build_pack(total_h, st.session_state["data_version"])

m = st.columns(5)
m[0].metric("Window", f"{total_h}h")
m[1].metric("SEC filings", f"{stats['filings']}", f"{stats['filings_substantive']} substantive")
m[2].metric("Ownership (flagged)", stats["ownership_flagged"])
m[3].metric("Company news", stats["company_news"])
m[4].metric("Market news", stats["market_news"])

tab_sig, tab_fil, tab_own, tab_log, tab_chat = st.tabs(
    ["⚡ Signals", "📄 Filings", "🏛 Ownership", "📝 Research log", "💬 Chat"])

# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #
with tab_sig:
    if ai_on:
        spin = ("Ranking via Claude Code (your login — may take 1–3 min)..."
                if provider == "claude_code" else f"Ranking with {model}...")
        if st.button(f"🤖 Rank with AI ({provider})", type="primary"):
            with st.spinner(spin):
                try:
                    if provider == "claude_code":
                        from scanner.scoring import claude_code
                        ranked = claude_code.score(md)
                    else:
                        from scanner.scoring import llm_scorer
                        ranked = llm_scorer.score(md, provider=provider, model=model, api_key=api_key)
                    st.session_state["ranked"] = ranked
                    # remember what the ranking was FOR, so a stale rank isn't
                    # shown against a different window / refreshed data
                    st.session_state["ranked_key"] = (total_h, st.session_state["data_version"])
                    from scanner import research_log
                    status = research_log.save(
                        ranked, title=f"Dashboard AI rank ({provider}, {total_h}h)",
                        key=f"{since.date()}|{total_h}h|{provider}|ai-rank")
                    st.caption(f"📝 Research log: {status} → digests/research_log.md")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"AI ranking failed: {exc}")
        if (st.session_state.get("ranked")
                and st.session_state.get("ranked_key") == (total_h, st.session_state["data_version"])):
            st.markdown(st.session_state["ranked"])
            st.divider()
    else:
        st.info("Pick an AI engine in the sidebar to get an AI-ranked signal list. "
                "Below is the deterministic candidate set (sourced).")

    st.subheader("Catalyst-tagged SEC filings")
    tagged = [f for f in pack["sec_filings"] if f.get("candidate_tags") and not f.get("is_routine")]
    if tagged:
        st.dataframe([{
            "Ticker": f["ticker"], "Mcap": _fmt_usd(f.get("market_cap")),
            "Form": f.get("form_type"), "Catalyst": ", ".join(f["candidate_tags"]),
            "Coverage": f.get("coverage", 0), "Headline": f["headline"], "Source": f["source"],
        } for f in tagged], width="stretch", hide_index=True,
            column_config={"Source": st.column_config.LinkColumn("Source", display_text="filing")})
    else:
        st.caption("No catalyst-tagged filings in this window.")

    st.subheader("Flagged ownership (activist 13D · insider buys · superinvestor)")
    if pack["ownership"]:
        st.dataframe([{
            "Ticker": d["ticker"], "Form": d["form_type"],
            "Flag": ("SUPERINVESTOR" if d.get("matched_investor") else
                     "ACTIVIST-13D" if d.get("is_activist") else
                     "INSIDER-BUY" if d.get("is_buy") else ""),
            "Filer": d["filer"], "Side": d.get("side"),
            "Shares": d.get("shares"), "Price": d.get("price"), "Stake %": d.get("pct"),
            "Source": d.get("source"),
        } for d in pack["ownership"]], width="stretch", hide_index=True,
            column_config={"Source": st.column_config.LinkColumn("Source", display_text="filing")})
    else:
        st.caption("No flagged ownership in this window.")

# --------------------------------------------------------------------------- #
# Filings (all, filterable)
# --------------------------------------------------------------------------- #
with tab_fil:
    all_tags = sorted({t for f in pack["sec_filings"] for t in (f.get("candidate_tags") or [])})
    pick = st.multiselect("Filter by catalyst tag", all_tags)
    rows = pack["sec_filings"]
    if pick:
        rows = [f for f in rows if set(f.get("candidate_tags") or []) & set(pick)]
    st.caption(f"{len(rows)} filings")
    st.dataframe([{
        "When": (f.get("filed_at") or "")[:16], "Ticker": f["ticker"],
        "Mcap": _fmt_usd(f.get("market_cap")), "Form": f.get("form_type"),
        "Tags": ", ".join(f.get("candidate_tags") or []), "Headline": f["headline"],
        "Source": f["source"],
    } for f in rows], width="stretch", hide_index=True,
        column_config={"Source": st.column_config.LinkColumn("Source", display_text="filing")})

# --------------------------------------------------------------------------- #
# Ownership (all stored in window)
# --------------------------------------------------------------------------- #
with tab_own:
    own = store.get_recent_ownership(since.isoformat())
    st.caption(f"{len(own)} ownership filings in window (13D/13G/Form 4)")
    st.dataframe([{
        "When": (d.get("filed_at") or "")[:16], "Ticker": d.get("ticker") or d.get("company"),
        "Form": d.get("form_type"), "Side": d.get("side"), "Filer": d.get("filer_name"),
        "Shares": d.get("shares"), "Price": d.get("price"), "Stake %": d.get("pct"),
        "Buy": bool(d.get("is_buy")), "Activist": bool(d.get("is_activist")),
        "Superinvestor": d.get("matched_investor"),
    } for d in own], width="stretch", hide_index=True)

# --------------------------------------------------------------------------- #
# Research log (local view + explicit publish to GitHub Pages)
# --------------------------------------------------------------------------- #
with tab_log:
    from scanner.publish_log import pages_url, publish
    from scanner.research_log import LOG_PATH

    cols = st.columns([3, 1])
    cols[0].caption(f"Local log: `{LOG_PATH}` — saved analyses, deduped, private until published.")
    if cols[1].button("🌐 Publish to GitHub Pages",
                      help="Copies the log to docs/index.md and pushes ONLY that file. "
                           "The published page is PUBLIC."):
        try:
            r = publish(push=True)
            st.success(f"{r['status']} → {r['url'] or r['page']}")
        except Exception as exc:  # noqa: BLE001 - show the real git/IO error
            st.error(f"Publish failed: {exc}")
    if pages_url():
        st.caption(f"Published page (once Pages is enabled): {pages_url()}")
    if LOG_PATH.exists():
        st.markdown(LOG_PATH.read_text(encoding="utf-8"))
    else:
        st.info("No research log yet — run a scan analysis (it saves here automatically), "
                "or use the ⚡ Signals tab's AI rank.")


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
def _link(text: str, url: str | None) -> str:
    return f"{text} [↗]({url})" if url else text


def _retrieve(q: str) -> str:
    """Deterministic retrieval (the `ask` behaviour) — relevant stored data with
    source links, as Markdown. Works with no API key."""
    companies = _resolve_companies(q, universe)
    ciks = [c["cik"] for c in companies]
    tags = tag_keywords(q)
    syms = ", ".join(c["ticker"] for c in companies) or "—"
    out = [f"**Matched companies:** {syms}  ·  **Catalyst tags:** {', '.join(tags) or '—'}"]
    if ciks:
        fil = store.filings_for_ciks(ciks, limit=15)
        own = store.ownership_for_ciks(ciks, limit=15)
        news = store.news_for_ciks(ciks, limit=15)
        if fil:
            out.append("\n**SEC filings (high trust):**")
            out += [f"- {a_['ticker']} · {a_.get('form_type')} — "
                    f"{_link(a_['headline'], a_.get('filing_url'))}" for a_ in fil]
        if own:
            out.append("\n**Ownership:**")
            out += [f"- {d_.get('ticker')} {d_.get('form_type')} — "
                    f"{d_.get('filer_name')} {d_.get('side')}"
                    f"{' [INSIDER-BUY]' if d_.get('is_buy') else ''}"
                    f"{' [ACTIVIST-13D]' if d_.get('is_activist') else ''}" for d_ in own]
        if news:
            out.append("\n**News (lower trust):**")
            out += [f"- [{n_.get('source')}] {_link(n_['headline'], n_.get('url'))}" for n_ in news]
        if not (fil or own or news):
            out.append("\n_No stored data for this company in the DB. Use the sidebar to Update/Backfill._")
    elif tags:
        for t in tags:
            fil = store.filings_by_tag(t, limit=20)
            out.append(f"\n**Filings tagged `{t}`:**")
            out += [f"- {a_['ticker']} — {_link(a_['headline'], a_.get('filing_url'))}" for a_ in fil] or ["- _none_"]
    else:
        out.append("\n_No company or catalyst recognised. Try a ticker (e.g. `NVDA`) "
                   "or a theme (e.g. `acquisition`, `insider buy`)._")
    return "\n".join(out)


with tab_chat:
    st.caption("Ask about a company or catalyst. **No key needed** — you'll get the matching "
               "stored data with sources. Add a key for an AI-written answer on top.")
    for role, text in st.session_state["chat"]:
        with st.chat_message(role):
            st.markdown(text)
    q = st.chat_input("Ask about a company or catalyst…")
    if q:
        st.session_state["chat"].append(("user", q))
        with st.chat_message("user"):
            st.markdown(q)
        retrieved = _retrieve(q)
        with st.chat_message("assistant"):
            if ai_on:
                spin = "Thinking via Claude Code…" if provider == "claude_code" else f"Thinking with {model}…"
                with st.spinner(spin):
                    try:
                        if provider == "claude_code":
                            from scanner.scoring import claude_code
                            ans = claude_code.chat(q, retrieved)
                        else:
                            from scanner.scoring import llm_scorer
                            ans = llm_scorer.chat(q, retrieved, provider=provider, model=model, api_key=api_key)
                    except Exception as exc:  # noqa: BLE001
                        ans = f"_AI answer failed ({exc}). Showing retrieved data below._"
                final = f"{ans}\n\n---\n**Sources / retrieved data**\n\n{retrieved}"
            else:
                final = (f"{retrieved}\n\n---\n_Pick an AI engine in the sidebar for an AI-written "
                         f"answer; or paste the above into Claude Code / Codex._")
            st.markdown(final)
        st.session_state["chat"].append(("assistant", final))
