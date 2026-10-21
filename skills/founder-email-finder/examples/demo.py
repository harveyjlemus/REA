#!/usr/bin/env python3
"""Offline end-to-end demo on a fictional market (examples/fakeworld.py).

Runs every lane with fake websites and fake API answers: no keys, no network,
no spend. Stops for the AI read, answers it the way the page reader would,
resumes, writes every output, then checks each row ended the way it should.

  python3 examples/demo.py          # temporary folder, removed afterwards
  python3 examples/demo.py --keep   # keeps runs/demo so you can open the files
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import fakeworld  # noqa: E402
from lib.cache import DomainMemo  # noqa: E402
from lib.config import load_config  # noqa: E402
from lib.getleads import GetLeadsClient  # noqa: E402
from lib.ledger import Ledger, summarize  # noqa: E402
from lib.millionverifier import MillionVerifierClient  # noqa: E402
from lib.mx import DohDns, MxChecker  # noqa: E402
from lib.pipeline import Pipeline, Settings  # noqa: E402
from lib.report import run_report, write_evidence, write_outputs  # noqa: E402
from lib.serper import SerperClient  # noqa: E402
from lib.state import RunDir  # noqa: E402


def build(run: RunDir, cfg, memo, t, *, verify_mode="api"):
    ledger = Ledger(run.ledger_path)
    settings = Settings(segment="auto", serp_max=10, mv_max=40, getleads_max=50, concurrency=4,
                        verify_mode=verify_mode, workers=2)
    return Pipeline(run, cfg, settings, transport=t, mx=MxChecker(backends=[DohDns(t)], ledger=ledger),
                    getleads=GetLeadsClient("demo-gl", t, ledger=ledger, min_interval=0, retry_wait=0),
                    serper=SerperClient("demo-sk", t, ledger=ledger, usd_per_search=0.001, retry_wait=0),
                    mv=MillionVerifierClient("demo-mv", t, ledger=ledger, usd_per_check=0.0004, retry_waits=(0, 0)),
                    memo=memo, ledger=ledger, log=lambda m: None), ledger


def run_demo(workdir: Path, quiet: bool = False) -> tuple[int, int, list[str]]:
    cfg = load_config()
    run = RunDir(workdir / "run")
    inp = workdir / "companies.csv"
    fakeworld.write_input(inp)
    _, rows = run.create(inp, cfg)
    memo = DomainMemo(workdir / "domains.json")
    t = fakeworld.transport()
    pipe, _ = build(run, cfg, memo, t)
    first = pipe.run_rows(rows)
    lines = [f"first pass: {first}"]
    answered = fakeworld.answer_ai_read(run.ai_dir)
    lines.append(f"page reader answered {answered} row(s)")
    rows = run.load_rows()
    pipe, ledger = build(run, cfg, memo, t)
    second = pipe.run_rows(rows)
    lines.append(f"second pass: {second}")
    header, mapping = run.load_mapping()
    counts = write_outputs(run.path, rows, header)
    write_evidence(run.path, rows)
    summary = summarize(ledger.events())
    (run.path / "run_report.md").write_text(run_report(rows, summary, budgets=pipe.budgets, blocked=pipe.blocked,
                                                       mapping=mapping), encoding="utf-8")
    passed = 0
    checks = [("pauses for the AI read, then finishes", first == "ai_read_pending" and second == "done")]
    for r, (values, want_status, want_email, what) in zip(sorted(rows, key=lambda r: r.n), fakeworld.ROWS):
        got_status, got_email = r.result.get("find_status"), r.result.get("founder_email") or None
        ok = got_status == want_status and (want_email is None or got_email == want_email)
        checks.append((f"{values[0]}: {what} -> {got_status} {got_email or ''}".rstrip(), ok))
    checks.append(("send_ready.csv holds only verified emails", counts["send_ready.csv"] == 8))
    for label, ok in checks:
        passed += ok
        lines.append(f"  {'ok ' if ok else 'FAIL'} {label}")
    return passed, len(checks), lines


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--keep", action="store_true")
    args = p.parse_args(argv)
    if args.keep:
        workdir = HERE.parents[2] / "runs" / "demo"
        shutil.rmtree(workdir, ignore_errors=True)
        workdir.mkdir(parents=True)
        passed, total, lines = run_demo(workdir)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            passed, total, lines = run_demo(Path(tmp))
    print("Offline demo (fictional companies, fake APIs, no spend)")
    print("\n".join(lines))
    print(f"{passed}/{total} demo checks passed.")
    if args.keep:
        print(f"Outputs kept in {workdir / 'run'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
