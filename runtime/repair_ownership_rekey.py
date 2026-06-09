"""One-off repair: re-key stored 13D/13G ownership rows to the TRUE subject company.

Why: the EDGAR daily index lists a 13D/13G under BOTH the subject company's CIK and
the filer's CIK (same accession). The ingester used to trust the index-row CIK, so
when both parties were in the universe the stored row could be keyed to the FILER
(wrong ticker/company) and mis-flagged as a non-activist "self-filing" — silently
suppressing the activist signal. The ingester now re-keys from the document; this
script applies the same re-derivation to rows ingested before the fix.

Run from the project root:
    .venv\\Scripts\\python.exe runtime\\repair_ownership_rekey.py

Re-fetches each stored SCHEDULE 13D/13G filing once (throttled, fair-access UA),
re-derives subject / is_activist / detail / pct, and updates rows in place.
Idempotent: a second run reports everything unchanged.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner import store
from scanner.http import PoliteSession
from scanner.ingest_ownership import (_amendment_detail, _index_url, _match_investor,
                                      _parse_sc13, _subject_cik_int, _superinvestors,
                                      _txt_url)
from scanner.universe import load_map


def main() -> None:
    by_cik = {int(c["cik"]): c for c in load_map()}
    watchlist = _superinvestors()
    session = PoliteSession()
    conn = store.get_conn()
    rows = conn.execute(
        "SELECT id, cik, accession, form_type, is_activist, detail FROM ownership "
        "WHERE form_type LIKE 'SCHEDULE 13%'").fetchall()
    print(f"Checking {len(rows)} stored 13D/13G rows...", flush=True)
    rekeyed = updated = deleted = unchanged = errors = 0
    for row in rows:
        stored_cik = int(row["cik"]) if row["cik"] else 0
        try:
            text = session.edgar_get(_txt_url(stored_cik, row["accession"]), timeout=45).text
        except Exception as exc:  # noqa: BLE001 - keep going; report at the end
            print(f"  ! fetch failed {row['accession']}: {exc}", flush=True)
            errors += 1
            continue
        subj = _subject_cik_int(text, row["form_type"]) or stored_cik
        meta = by_cik.get(subj)
        if meta is None:
            # Filer-side row of a filing about a non-universe company — never a signal.
            conn.execute("DELETE FROM ownership WHERE id=?", (row["id"],))
            deleted += 1
            print(f"  - deleted {row['accession']}: subject CIK {subj} not in universe", flush=True)
            continue
        p = _parse_sc13(text)
        is_self = bool(p["filer_cik"]) and int(p["filer_cik"]) == subj
        is_13d = row["form_type"].startswith("SCHEDULE 13D")
        is_activist = 1 if (is_13d and not is_self) else 0
        detail = _amendment_detail(text, row["form_type"])
        if subj == stored_cik and row["is_activist"] == is_activist and (row["detail"] or "") == detail:
            unchanged += 1
            continue
        conn.execute(
            "UPDATE ownership SET cik=?, ticker=?, company=?, filer_name=?, pct=?, "
            "matched_investor=?, is_activist=?, detail=?, filing_url=? WHERE id=?",
            (meta["cik"], meta.get("ticker", ""), meta.get("name", ""), p["filer_name"] or "",
             p["pct"], _match_investor(p["filer_name"] or "", watchlist), is_activist,
             detail, _index_url(subj, row["accession"]), row["id"]))
        if subj != stored_cik:
            rekeyed += 1
            print(f"  * RE-KEYED {row['accession']} -> {meta.get('ticker')} "
                  f"({meta.get('name')}) activist={is_activist}", flush=True)
        else:
            updated += 1
    conn.commit()
    conn.close()
    print(f"Done: {rekeyed} re-keyed, {updated} detail/flag-refreshed, {deleted} deleted "
          f"(non-universe subject), {unchanged} unchanged, {errors} fetch errors.")


if __name__ == "__main__":
    main()
