#!/usr/bin/env python3
"""Million Verifier round trip, step 1: write the next addresses to check.

By default one address per person (the next unchecked guess, or an address a
lane found), so you keep the savings of stopping at the first "ok". Upload the
file to Million Verifier, download the FULL report, then run import_report.py.

  python3 scripts/export_guesses.py --run-dir runs/roofing
  python3 scripts/export_guesses.py --run-dir runs/roofing --per-person all
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import env  # noqa: E402
from lib.cache import DomainMemo  # noqa: E402
from lib.state import RunDir  # noqa: E402


def next_round(run_dir: Path) -> int:
    nums = [int(m.group(1)) for f in run_dir.glob("mv_upload_round*.csv")
            if (m := re.search(r"round(\d+)", f.name))]
    return max(nums, default=0) + 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-dir", required=True)
    p.add_argument("--per-person", default="1", help="'1' (default) or 'all'")
    args = p.parse_args(argv)
    env.load_dotenv()
    run = RunDir(args.run_dir)
    if not run.exists():
        print(f"No run in {run.path}", file=sys.stderr)
        return 2
    memo = DomainMemo(Path(env.get("FEF_CACHE_DIR") or (env.REPO_ROOT / ".fef-cache")) / "domains.json")
    rows = run.load_rows()
    out_rows = []
    for r in rows:
        if not r.is_open or r.duplicate_of:
            continue
        if r.domain and memo.status(r.domain) == "catch_all":
            continue  # every address there would come back catch_all
        pending = [(0, f["email"], f"found by {f['source']}") for f in r.found_emails if not f.get("mv")]
        pending += [(g["rank"], g["email"], g["pattern"]) for g in r.guesses if not g.get("mv")]
        if not pending:
            continue
        take = pending if args.per_person == "all" else pending[:1]
        t = r.target or {}
        for rank, email, why in take:
            out_rows.append([email, r.row_id, rank, why, " ".join(p for p in (t.get("first"), t.get("last")) if p),
                             r.input.get("company") or "", r.domain or ""])
    if not out_rows:
        print("Nothing to export: every person is verified, closed, or has no guesses left.")
        return 0
    n = next_round(run.path)
    path = run.path / f"mv_upload_round{n}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["email", "fef_row_id", "rank", "why", "person", "company", "domain"])
        w.writerows(out_rows)
    people = len({r[1] for r in out_rows})
    print(f"Wrote {path} with {len(out_rows)} address(es) for {people} person(s).")
    print("Next: upload it to Million Verifier, download the FULL report (not 'OK only'), then run:")
    print(f"  python3 {Path(__file__).with_name('import_report.py')} --run-dir {run.path} --report <full report.csv>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
