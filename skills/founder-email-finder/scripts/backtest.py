#!/usr/bin/env python3
"""Backtest the finder on rows where you already know the right email.

guesses (offline, free): for every held-out row, build the guesses the finder
  would make and see where the real email lands. Rank 1 means one Million
  Verifier check; not in the list means a miss. Compares the calibrated order
  with the starting order.

  python3 scripts/backtest.py guesses --segment local --from roofing.csv pest.csv

make / score (live): hide the emails, run find_founders.py on the result, then
  score what it found against the answers.

  python3 scripts/backtest.py make --from roofing.csv --sample 50 --out private/bt
  python3 scripts/find_founders.py --input private/bt/input.csv --run-dir runs/bt --yes ...
  python3 scripts/backtest.py score --run-dir runs/bt --key private/bt/key.csv
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.calibration import is_holdout, iter_labeled  # noqa: E402
from lib.config import load_config  # noqa: E402
from lib.guesses import build_guesses  # noqa: E402


def rank_of(email: str, guesses) -> int | None:
    for g in guesses:
        if g.email == email:
            return g.rank
    return None


def score_order(rows, cfg, segment: str, max_guesses: int) -> dict:
    ranks = [rank_of(r.email, build_guesses(r.name, r.domain, segment=segment, cfg=cfg, max_guesses=max_guesses))
             for r in rows]
    n = len(ranks)
    hits = [x for x in ranks if x is not None]
    return {"n": n,
            "hit1": sum(1 for x in hits if x == 1) / n if n else 0.0,
            "hit3": sum(1 for x in hits if x <= 3) / n if n else 0.0,
            "hit_all": len(hits) / n if n else 0.0,
            "avg_checks_when_found": sum(hits) / len(hits) if hits else 0.0,
            "avg_checks_per_person": (sum(hits) + (n - len(hits)) * max_guesses) / n if n else 0.0}


def cmd_guesses(args) -> int:
    cfg = load_config()
    base_cfg = replace(cfg, calibrated=None)
    ok_rows, catch_rows = [], []
    for f in args.files:
        for r in iter_labeled(Path(f), cfg):
            if args.people == "founders" and not r.founder_level:
                continue
            if not is_holdout(r.domain, args.holdout):
                continue
            if r.mv_result in (None, "ok"):
                ok_rows.append(r)
            elif r.mv_result == "catch_all":
                catch_rows.append(r)
    if not ok_rows:
        print("No held-out rows with a verified email.")
        return 1
    cal = score_order(ok_rows, cfg, args.segment, args.max_guesses)
    base = score_order(ok_rows, base_cfg, args.segment, args.max_guesses)
    labels = Counter(r.label for r in ok_rows)
    lines = [f"# Backtest: guess order ({args.segment})", "",
             f"Held-out rows with a Million Verifier \"ok\" email: {cal['n']} "
             f"({', '.join(f'{k} {v}' for k, v in labels.most_common())}). Up to {args.max_guesses} guesses per person.",
             "None of these rows were used to learn the calibrated order.", "",
             "| | Starting order | Calibrated order |", "|---|---|---|",
             f"| Right email is guess #1 | {base['hit1']:.1%} | {cal['hit1']:.1%} |",
             f"| Right email in the first 3 | {base['hit3']:.1%} | {cal['hit3']:.1%} |",
             f"| Right email anywhere in the {args.max_guesses} | {base['hit_all']:.1%} | {cal['hit_all']:.1%} |",
             f"| Checks per person found | {base['avg_checks_when_found']:.2f} | {cal['avg_checks_when_found']:.2f} |",
             f"| Checks per person, misses included | {base['avg_checks_per_person']:.2f} | {cal['avg_checks_per_person']:.2f} |",
             ""]
    if catch_rows:
        c = score_order(catch_rows, cfg, args.segment, args.max_guesses)
        share = len(catch_rows) / (len(catch_rows) + len(ok_rows))
        lines += ["## Catch-all domains", "",
                  f"{len(catch_rows)} held-out rows ({share:.0%}) sit on catch-all domains, where Million Verifier "
                  "can't tell guesses apart, so the finder stops after one check and reports its first guess as risky.",
                  f"Using the export's own email as the reference, that first guess is right {c['hit1']:.1%} of the time.", ""]
    lines += ["Notes: this measures the guess order only. It assumes the person's name and the company domain "
              "are already known and that Million Verifier answers \"ok\" for the real address. Finding names "
              "(website, GetLeads, Serper) is measured by `make` + `score`."]
    text = "\n".join(lines) + "\n"
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Wrote {args.out}")
    return 0


def cmd_make(args) -> int:
    cfg = load_config()
    rows = []
    for f in args.files:
        rows += [r for r in iter_labeled(Path(f), cfg)
                 if r.mv_result in (None, "ok") and is_holdout(r.domain, args.holdout)]
    random.Random(7).shuffle(rows)
    rows = rows[:args.sample]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "input.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["company", "website", "first_name", "last_name", "bt_id"])
        for i, r in enumerate(rows):
            first, last = (r.name.first, r.name.last) if args.hide == "email" else ("", "")
            if args.hide == "last_name":
                first, last = r.name.first, ""
            w.writerow(["", r.domain, first, last, f"bt{i}"])
    with (out / "key.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["bt_id", "email", "first_name", "last_name", "domain"])
        for i, r in enumerate(rows):
            w.writerow([f"bt{i}", r.email, r.name.first, r.name.last, r.domain])
    print(f"Wrote {len(rows)} rows to {out / 'input.csv'} (hidden: {args.hide}) and the answers to {out / 'key.csv'}.")
    print("Keep that folder out of git: it holds real names and emails.")
    return 0


def cmd_score(args) -> int:
    key = {}
    with open(args.key, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key[row["bt_id"]] = row
    status = Counter()
    correct = Counter()
    with open(Path(args.run_dir) / "results.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            truth = key.get(row.get("bt_id"))
            if not truth:
                continue
            st = row.get("find_status") or row.get("find_status_fef")
            email = (row.get("founder_email") or "").lower()
            status[st] += 1
            if email and email == truth["email"].lower():
                correct[st] += 1
    total = sum(status.values())
    print(f"Scored {total} rows against {args.key}")
    print("| Status | Rows | Email matches the answer |\n|---|---|---|")
    for st, n in status.most_common():
        print(f"| {st} | {n} | {correct[st]} ({correct[st] / n:.0%}) |")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("guesses")
    g.add_argument("--from", dest="files", nargs="+", required=True)
    g.add_argument("--segment", choices=("local", "startup"), required=True)
    g.add_argument("--people", choices=("founders", "all"), default="founders")
    g.add_argument("--holdout", type=float, default=0.3)
    g.add_argument("--max-guesses", type=int, default=6)
    g.add_argument("--out", help="also write the report here (aggregates only)")
    m = sub.add_parser("make")
    m.add_argument("--from", dest="files", nargs="+", required=True)
    m.add_argument("--sample", type=int, default=50)
    m.add_argument("--hide", choices=("email", "last_name", "names"), default="email",
                   help="email: keep names; last_name: keep first names; names: company + website only")
    m.add_argument("--holdout", type=float, default=0.3)
    m.add_argument("--out", required=True)
    s = sub.add_parser("score")
    s.add_argument("--run-dir", required=True)
    s.add_argument("--key", required=True)
    args = p.parse_args(argv)
    return {"guesses": cmd_guesses, "make": cmd_make, "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
