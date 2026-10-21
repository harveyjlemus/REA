#!/usr/bin/env python3
"""Learn the guess order from your own verified emails.

Reads exports where the email is already proven (a Million Verifier "ok", or
Apollo "verified" when there is no MV column), keeps rows whose email is at the
company's own domain, and counts which pattern each one uses. A fixed share of
domains (default 30%) is held out for backtest.py and never counted here.

  python3 scripts/calibrate.py --segment local --from roofing.csv pest.csv agencies.csv

Writes config/patterns.calibrated.yaml: aggregate counts only, no names or emails.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

from lib.calibration import is_holdout, iter_labeled, pattern_counts  # noqa: E402
from lib.config import CALIBRATED_FILE, CONFIG_DIR, load_config  # noqa: E402


def summarize(counts, total: int, cfg, top: int | None = None) -> dict:
    ordered = [p for p, _ in counts.most_common() if p != "other"]
    order = ordered + [p for p in cfg.starting_order if p not in ordered]
    shares = {p: round(counts[p] / total, 4) for p in ordered}
    out = {"sample_size": total, "order": order[:top] if top else order, "shares": shares,
           "other_share": round(counts.get("other", 0) / total, 4) if total else 0.0}
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="files", nargs="+", required=True)
    p.add_argument("--segment", choices=("local", "startup"), required=True,
                   help="which segment these files describe")
    p.add_argument("--people", choices=("founders", "all"), default="founders",
                   help="count only owner/founder-level rows (default) or everyone")
    p.add_argument("--holdout", type=float, default=0.3, help="share of domains kept for the backtest")
    p.add_argument("--min-rows", type=int, default=30, help="fewer rows than this and the order isn't trusted")
    p.add_argument("--out", default=str(CONFIG_DIR / CALIBRATED_FILE))
    args = p.parse_args(argv)
    cfg = load_config()

    train, per_label = [], {}
    held = skipped_mv = 0
    for f in args.files:
        path = Path(f)
        rows = []
        for r in iter_labeled(path, cfg):
            if args.people == "founders" and not r.founder_level:
                continue
            if r.mv_result not in (None, "ok"):
                skipped_mv += 1
                continue
            if is_holdout(r.domain, args.holdout):
                held += 1
                continue
            rows.append(r)
        per_label.setdefault(rows[0].label if rows else path.stem, []).extend(rows)
        train.extend(rows)
        print(f"{path.name}: {len(rows)} verified rows counted")
    if not train:
        print("No usable rows: need first name, last name, an email at the company's domain, and a verdict.")
        return 1
    counts, total = pattern_counts(train, cfg)
    out_path = Path(args.out)
    data = yaml.safe_load(out_path.read_text(encoding="utf-8")) if out_path.is_file() else {}
    data = data or {}
    data.update({"calibrated_at": date.today().isoformat(), "min_rows_to_trust": args.min_rows,
                 "holdout_share": args.holdout})
    data.setdefault("segments", {})[args.segment] = {
        **summarize(counts, total, cfg),
        "sources": {label: len(rows) for label, rows in per_label.items()},
        "people": args.people,
    }
    data.setdefault("labels", {})
    for label, rows in per_label.items():
        if rows:
            c, n = pattern_counts(rows, cfg)
            data["labels"][label] = {**summarize(c, n, cfg, top=6), "segment": args.segment}
    header = ("# Written by scripts/calibrate.py. Aggregate pattern counts from verified emails;\n"
              "# no names or addresses are stored here. Re-run calibrate.py to refresh.\n")
    out_path.write_text(header + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    print(f"\n{args.segment}: {total} verified emails counted ({held} held out for the backtest, "
          f"{skipped_mv} skipped because Million Verifier didn't say ok)")
    print("| Pattern | Share |\n|---|---|")
    for pid, n in counts.most_common(10):
        print(f"| {pid} | {n / total:.1%} |")
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
