#!/usr/bin/env python3
"""Round trip, step 2: read a Million Verifier full report (or a QuickEnrich
results file) back into the run, apply the same rules as the API path, and
rewrite the outputs.

  python3 scripts/import_report.py --run-dir runs/roofing --report mv_upload_round1_FULL_REPORT.csv
  python3 scripts/import_report.py --run-dir runs/roofing --report quickenrich_results.csv

A Million Verifier report is recognized by its quality/result columns. Any other
file is read as enrichment results: rows are matched on fef_row_id when the file
kept it, otherwise on first name + last name + domain, and each address found is
added for verification (run find_founders.py again, or export the next round).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import env  # noqa: E402
from lib.cache import DomainMemo  # noqa: E402
from lib.columns import norm_header, read_table  # noqa: E402
from lib.config import load_config  # noqa: E402
from lib.domains import registrable, split_email, valid_email  # noqa: E402
from lib.ledger import Ledger, summarize  # noqa: E402
from lib.millionverifier import read_report  # noqa: E402
from lib.names import email_token  # noqa: E402
from lib.report import finalize_all, write_evidence, write_outputs  # noqa: E402
from lib.state import RunDir  # noqa: E402
from lib.verify import apply_answer, from_report  # noqa: E402


def import_mv(rows, answers: dict, memo, cfg) -> dict:
    stats = {"verified": 0, "catch_all": 0, "unknown": 0, "next_round": 0, "all_invalid": 0}
    for r in rows:
        if not r.is_open or r.duplicate_of:
            continue
        touched = False
        for f in r.found_emails:
            if not f.get("mv") and f["email"] in answers:
                f["mv"] = answers[f["email"]].to_dict()
                touched = True
                dom = registrable(split_email(f["email"])[1], cfg.lex)
                if f["mv"]["result"] == "catch_all":
                    memo.set_status(dom, "catch_all")
        if r.verified:
            stats["verified"] += 1
            continue
        if r.guesses:
            ans = {e: a.to_dict() for e, a in answers.items()}
            before = sum(1 for g in r.guesses if g.get("mv"))
            out = from_report(r.guesses, ans, domain=r.domain, memo=memo)
            touched = touched or sum(1 for g in r.guesses if g.get("mv")) != before
            r.verify = out.to_dict()
            key = {"verified": "verified", "catch_all": "catch_all", "unknown": "unknown",
                   "waiting": "next_round", "all_invalid": "all_invalid"}[out.status]
            stats[key] += 1
        if touched:
            r.set_lane("verify_import", "done")
    return stats


def import_enrichment(rows, path: Path, cfg) -> int:
    header, data = read_table(path)
    idx = {norm_header(h): i for i, h in enumerate(header)}

    def col(*names):
        return next((idx[n] for n in names if n in idx), None)

    e_i = col("email", "work email", "business email", "email address")
    rid_i = col("fef row id")
    f_i, l_i = col("first name", "firstname"), col("last name", "lastname")
    d_i = col("domain", "company domain", "website", "company website")
    if e_i is None:
        raise SystemExit(f"{path.name}: no email column found")
    by_id = {r.row_id: r for r in rows}
    by_name = {}
    for r in rows:
        t = r.target or {}
        if t.get("first") and t.get("last") and r.domain:
            by_name[(email_token(t["first"]), email_token(t["last"]), r.domain)] = r
    added = 0
    for line in data:
        email = (line[e_i] if e_i < len(line) else "").strip().lower()
        if not valid_email(email):
            continue
        row = by_id.get(line[rid_i]) if rid_i is not None and rid_i < len(line) else None
        if row is None and f_i is not None and l_i is not None:
            dom = None
            if d_i is not None and d_i < len(line):
                from lib.domains import clean_host
                h = clean_host(line[d_i])
                dom = registrable(h, cfg.lex) if h else None
            dom = dom or registrable(split_email(email)[1], cfg.lex)
            row = by_name.get((email_token(line[f_i]), email_token(line[l_i]), dom))
        if row is None or not row.is_open or any(f["email"] == email for f in row.found_emails):
            continue
        row.found_emails.append({"email": email, "source": "quickenrich", "url": None, "quote": path.name, "mv": None})
        row.set_lane("enrichment_import", "done", file=path.name)
        added += 1
    return added


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-dir", required=True)
    p.add_argument("--report", action="append", required=True, help="Million Verifier full report or enrichment CSV")
    args = p.parse_args(argv)
    env.load_dotenv()
    cfg = load_config()
    run = RunDir(args.run_dir)
    if not run.exists():
        print(f"No run in {run.path}", file=sys.stderr)
        return 2
    memo = DomainMemo(Path(env.get("FEF_CACHE_DIR") or (env.REPO_ROOT / ".fef-cache")) / "domains.json")
    header, _mapping = run.load_mapping()
    rows = run.load_rows()
    for rep in args.report:
        path = Path(rep)
        head, _ = read_table(path)
        heads = {norm_header(h) for h in head}
        if {"quality", "result"} <= heads:
            answers = read_report(path)
            stats = import_mv(rows, answers, memo, cfg)
            print(f"{path.name}: {len(answers)} answers -> " + ", ".join(f"{k} {v}" for k, v in stats.items()))
        else:
            n = import_enrichment(rows, path, cfg)
            print(f"{path.name}: {n} new address(es) added; verify them with find_founders.py (API) or the next export round.")
    costs = summarize(Ledger(run.ledger_path).events())["by_row"]
    finalize_all(rows, cfg, memo, costs)
    memo.flush()
    for r in rows:
        run.save(r)
    counts = write_outputs(run.path, rows, header)
    write_evidence(run.path, rows)
    print("Outputs: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    waiting = sum(1 for r in rows if r.result.get("find_status") == "awaiting_verification")
    if waiting:
        print(f"{waiting} person(s) still need a check: run export_guesses.py for the next round.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
