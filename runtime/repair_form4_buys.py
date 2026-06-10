"""One-off repair: re-derive stored Form-4 BUY rows with the fixed parser.

Why: the old parser scanned BOTH transaction tables for code P, so warrants
"acquired" under employment agreements (derivative rows — pure compensation)
were flagged as open-market insider buys, with the price taken from any leg
(e.g. a same-form tax-withholding sale). Verified live with ABAT's CEO Form 4
(comp warrants surfaced as an "$8.3M buy"). A real buy is a PRICED
non-derivative P; this script re-fetches each stored is_buy row once and
re-derives side / shares / price / is_buy.

Run from the project root:
    .venv\\Scripts\\python.exe runtime\\repair_form4_buys.py
Idempotent: a second run reports everything unchanged.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner import store
from scanner.http import PoliteSession
from scanner.ingest_ownership import _parse_form4, _txt_url


def main() -> None:
    session = PoliteSession()
    conn = store.get_conn()
    rows = conn.execute(
        "SELECT id, cik, accession, filer_name, shares, price FROM ownership "
        "WHERE form_type LIKE '4%' AND is_buy=1").fetchall()
    print(f"Re-checking {len(rows)} stored insider-BUY rows...", flush=True)
    downgraded = repriced = unchanged = errors = 0
    for row in rows:
        try:
            text = session.edgar_get(_txt_url(int(row["cik"]), row["accession"]), timeout=45).text
        except Exception as exc:  # noqa: BLE001
            print(f"  ! fetch failed {row['accession']}: {exc}", flush=True)
            errors += 1
            continue
        p = _parse_form4(text)
        if not p["is_buy"]:
            conn.execute(
                "UPDATE ownership SET side=?, shares=?, price=?, is_buy=0 WHERE id=?",
                (p["side"], p["shares"], p["price"], row["id"]))
            downgraded += 1
            print(f"  - NOT A BUY {row['accession']}: {row['filer_name'][:40]} "
                  f"(was {row['shares'] or 0:,.0f} sh @ ${row['price']}) -> side={p['side']}", flush=True)
        elif (p["shares"], p["price"]) != (row["shares"], row["price"]):
            conn.execute("UPDATE ownership SET shares=?, price=? WHERE id=?",
                         (p["shares"], p["price"], row["id"]))
            repriced += 1
        else:
            unchanged += 1
    conn.commit()
    conn.close()
    print(f"Done: {downgraded} downgraded (comp/derivative, not buys), "
          f"{repriced} share/price corrected, {unchanged} confirmed real buys, {errors} errors.")


if __name__ == "__main__":
    main()
