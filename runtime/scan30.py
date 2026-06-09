"""One-off 30-day backfill + pack build (run in background).

Full 30 days of EDGAR catalyst filings + activist/large 13D-13G stakes (skips the
~25k-fetch Form-4 insider sweep on purpose). News is point-in-time. Writes the
30-day context pack for the agent to analyze.
"""
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Make `scanner` importable regardless of how this script is launched
# (running runtime/scan30.py puts runtime/ on sys.path, not the project root).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

logging.basicConfig(
    level=logging.INFO,
    handlers=[logging.FileHandler(os.path.join(_ROOT, "runtime", "scan30.log"), encoding="utf-8")],
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    force=True,
)

from scanner import ingest_edgar, ingest_news, ingest_ownership, store
from scanner.context_pack import build_context_pack
from scanner.http import PoliteSession
from scanner.universe import load_map

ET = ZoneInfo("America/New_York")
since = datetime.now(ET) - timedelta(days=30)

store.init_db()
store.sync_companies(load_map())
s = PoliteSession()

print("[1/4] EDGAR catalyst filings (30d)...", flush=True)
f = ingest_edgar.ingest(session=s, since=since)
nf = store.upsert_filings(f)
print(f"      filings: {len(f)} fetched, {nf} new", flush=True)

print("[2/4] Ownership: 13D/13G activist + large stakes (30d)...", flush=True)
sc13 = {"SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A"}
o = ingest_ownership.ingest(session=s, since=since, forms=sc13)
no = store.upsert_ownership(o)
print(f"      ownership: {len(o)} fetched, {no} new", flush=True)

print("[3/4] News (point-in-time)...", flush=True)
nw = ingest_news.ingest(session=s)
nn = store.upsert_news(nw)
print(f"      news: {len(nw)} fetched, {nn} new", flush=True)

print("[4/4] Building 30-day context pack...", flush=True)
stats = build_context_pack(since=since)
print("DONE " + json.dumps({"filings_new": nf, "ownership_new": no, "news_new": nn, **{k: stats[k] for k in ('filings', 'filings_substantive', 'ownership_flagged', 'company_news')}}), flush=True)
