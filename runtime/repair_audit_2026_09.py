"""One-off repair for rows stored before the 2026-09 audit fixes.

1. filings.filed_at — rows enriched from the submissions API while it still gave
   ET mislabelled "Z" were stored 4-5h early. Re-read the true ET acceptance time
   from each filing's SGML header — every row (one small request each; ~25 min for
   11k rows). Idempotent: already-correct rows are left alone.
2. ownership.matched_investor — re-match every row against the current watchlist
   with the current (full-name, word-bounded) matcher; old rows still carried
   surname false-positives such as "LOEB GARY" and "Tepper Oren". Local, no network.

Usage (from the project root):  .venv\\Scripts\\python.exe runtime\\repair_audit_2026_09.py [--dry-run] [--db=PATH]
Back up data/catalyst.db first. Do not run it while a refresh is running (both hit SEC).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from concurrent.futures import ThreadPoolExecutor  # noqa: E402

from scanner import store  # noqa: E402
from scanner.http import PoliteSession  # noqa: E402
from scanner.ingest_edgar import _fetch_header, acceptance_iso  # noqa: E402
from scanner.ingest_ownership import _match_investor, _superinvestors  # noqa: E402

ARCHIVES = "https://www.sec.gov/Archives/edgar/data"


def main(dry: bool) -> None:
    conn = store.get_conn()
    watch = _superinvestors()
    own = conn.execute("SELECT id, filer_name, matched_investor FROM ownership").fetchall()
    rematch = [(m, r["id"]) for r in own
               if (m := _match_investor(r["filer_name"] or "", watch)) != r["matched_investor"]]
    print(f"ownership: {len(rematch)} of {len(own)} superinvestor matches change")

    # ALL rows: the mislabel is not limited to fresh filings (seen: a 2026-05-18 07:00
    # ET filing stored as 03:00 after being ingested three weeks later).
    rows = conn.execute("SELECT id, cik, accession, filed_at FROM filings WHERE filed_at != ''").fetchall()
    print(f"filings: re-reading {len(rows)} headers...")
    session = PoliteSession()
    with ThreadPoolExecutor(max_workers=8) as pool:
        hdrs = list(pool.map(lambda r: _fetch_header(session, int(r["cik"]), r["accession"], ARCHIVES), rows))
    fixes = [(acceptance_iso(h["accepted"], ""), r["id"]) for r, h in zip(rows, hdrs)
             if h and acceptance_iso(h["accepted"], "") not in ("", r["filed_at"])]
    failed = sum(1 for h in hdrs if h is None)
    print(f"filings: {len(fixes)} timestamps corrected, {failed} headers failed (re-run to retry)")

    if not dry:
        conn.executemany("UPDATE ownership SET matched_investor=? WHERE id=?", rematch)
        conn.executemany("UPDATE filings SET filed_at=? WHERE id=?", fixes)
        conn.commit()
        print("written.")
    conn.close()


if __name__ == "__main__":
    db = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--db=")), None)
    if db:                                   # repair a DB other than this checkout's
        from pathlib import Path
        store.DB_PATH = Path(db)
    main(dry="--dry-run" in sys.argv)
