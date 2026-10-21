#!/usr/bin/env python3
"""Find the founder or owner of each company in a CSV, and an email for them
that Million Verifier says won't bounce.

Start with a preview (the default): it builds a small sample on the same lanes
and budgets as the full run, prints a card per row and a cost estimate for the
whole file, then stops. Rows it built are reused by the full run.

  python3 scripts/find_founders.py --input leads.csv --run-dir runs/roofing --preview 3 \\
      --serp-max-searches 20 --mv-max-checks 30 --getleads-max-records 50
  python3 scripts/find_founders.py --run-dir runs/roofing --yes \\
      --serp-max-searches 500 --mv-max-checks 2000 --getleads-max-records 5000

Paid lanes stay off until you give them a budget. Exit codes: 0 done (or the
preview printed), 3 the AI read is waiting for Claude, 2 a setup problem, 1 error.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import env  # noqa: E402
from lib.cache import DomainMemo  # noqa: E402
from lib.config import load_config  # noqa: E402
from lib.http import Transport  # noqa: E402
from lib.ledger import Ledger, summarize  # noqa: E402
from lib.mx import MxChecker  # noqa: E402
from lib.pipeline import Pipeline, Settings  # noqa: E402
from lib.report import estimate, pick_preview, render_preview, run_report, write_evidence, write_outputs  # noqa: E402
from lib.state import RunDir  # noqa: E402

PREVIEW_BUILD_MIN = 10
EXIT_AI_READ = 3
EXIT_SETUP = 2


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true",
                   help="say which keys are set and which hosts this machine can reach, spend nothing, stop")
    p.add_argument("--input", help="CSV to read (first run only; it's copied into the run folder)")
    p.add_argument("--run-dir", help="folder for this run's state and outputs")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--preview", type=int, metavar="N", help="build a small sample, show N cards and the estimate, stop (default 3)")
    mode.add_argument("--yes", action="store_true", help="run every row")
    p.add_argument("--serp-max-searches", type=int, default=0, help="Serper cap for this run (0 = lane off)")
    p.add_argument("--mv-max-checks", type=int, default=0, help="Million Verifier cap for this run (0 = lane off)")
    p.add_argument("--getleads-max-records", type=int, default=0, help="GetLeads credit cap for this run (0 = lane off)")
    p.add_argument("--getleads-per-domain", type=int, default=5, help="decision-makers records per company (default 5)")
    p.add_argument("--max-guesses", type=int, default=None, help="Million Verifier checks per person at most (default 6)")
    p.add_argument("--segment", choices=("auto", "local", "startup"), default="auto")
    p.add_argument("--verify", choices=("api", "manual"), default="api",
                   help="api: check guesses with the Million Verifier API; manual: export CSVs instead")
    p.add_argument("--ai-read", choices=("on", "off"), default="on")
    p.add_argument("--offline", action="store_true", help="no network at all: use saved pages only")
    p.add_argument("--map", action="append", default=[], metavar="field=Header", help="override a column mapping")
    p.add_argument("--max-subpages", type=int, default=4)
    p.add_argument("--concurrency", type=int, default=8, help="websites read at the same time")
    p.add_argument("--per-site-delay", type=float, default=1.0, help="seconds between requests to one site")
    p.add_argument("--limit", type=int, default=None, help="only the first N rows (testing)")
    return p.parse_args(argv)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def build_clients(args, transport, ledger):
    """Clients for the lanes that have a key. A lane without a key stays off."""
    from lib.getleads import GetLeadsClient
    from lib.millionverifier import MillionVerifierClient
    from lib.serper import SerperClient
    gl = sk = mv = None
    if env.get("GETLEADS_API_KEY"):
        gl = GetLeadsClient(env.get("GETLEADS_API_KEY"), transport, base_url=env.get("GETLEADS_BASE_URL"),
                            ledger=ledger, usd_per_credit=env.get_float("GETLEADS_USD_PER_CREDIT"))
    if env.get("SERPER_API_KEY"):
        sk = SerperClient(env.get("SERPER_API_KEY"), transport, ledger=ledger,
                          usd_per_search=env.get_float("SERPER_USD_PER_SEARCH"))
    if env.get("MILLIONVERIFIER_API_KEY"):
        mv = MillionVerifierClient(env.get("MILLIONVERIFIER_API_KEY"), transport, ledger=ledger,
                                   usd_per_check=env.get_float("MV_USD_PER_CHECK"))
    return gl, sk, mv


def check_setup(args, gl, sk, mv) -> list[str]:
    problems = []
    if args.getleads_max_records and not gl:
        problems.append("--getleads-max-records is set but GETLEADS_API_KEY is missing")
    if args.serp_max_searches and not sk:
        problems.append("--serp-max-searches is set but SERPER_API_KEY is missing")
    if args.mv_max_checks and not mv and args.verify == "api":
        problems.append("--mv-max-checks is set but MILLIONVERIFIER_API_KEY is missing")
    return problems


def stratified(rows, n: int) -> list:
    """Up to n open rows, spread across how rows start (company only, first name, ...)."""
    pools: dict[str, list] = {}
    for r in rows:
        if r.closed or r.duplicate_of:
            continue
        pools.setdefault(r.kind, []).append(r)
    already = [r for r in rows if r.result]
    out = list(already[:n])
    order = ["company_only", "first_name", "full_name", "has_email"]
    while len(out) < n and any(pools.values()):
        for k in order + [k for k in pools if k not in order]:
            pool = pools.get(k)
            while pool and pool[0] in out:
                pool.pop(0)
            if pool and len(out) < n:
                out.append(pool.pop(0))
    return out


API_HOSTS = "google.serper.dev, api.millionverifier.com, app.getleads.io, dns.google"


def network_hint(blocked: dict) -> str | None:
    """One plain hint when the environment, not the data, stopped a lane."""
    hits = [lane for lane, why in blocked.items() if "network" in why or "blocked" in why or "DNS" in why]
    if not hits:
        return None
    return ("Network: this environment blocked " + ", ".join(sorted(set(hits))) + ". In a Claude Code cloud "
            "session, open the cloud environment menu in the session's title bar, then Edit > Network access: "
            "allow broader access for the website lane, and these hosts for the APIs: " + API_HOSTS +
            ". Or run this on your own computer, where nothing is blocked.")


def ai_read_message(run: RunDir) -> str:
    from lib import ai_read
    batches = ai_read.pending_batches(run.ai_dir)
    reader = Path(__file__).resolve().parents[1] / "agents" / "page-reader.md"
    lines = [f"AI read needed: {len(batches)} batch file(s) of saved pages.",
             f"For each file in {run.ai_dir / 'batches'} without a match in {run.ai_dir / 'answers'}:",
             f"  run a subagent (model: haiku) with the instructions in {reader},",
             "  giving it the batch file and the answers path ai_read/answers/<same file name>.",
             "Then run this same command again. Or pass --ai-read off to skip this lane."]
    return "\n".join(lines)


def check(transport) -> int:
    """Keys present (never their values) and reachability, using free calls only."""
    from lib.http import FetchError, NetworkBlocked
    from lib.mx import MxChecker
    keys = {"SERPER_API_KEY": "Serper", "MILLIONVERIFIER_API_KEY": "Million Verifier", "GETLEADS_API_KEY": "GetLeads"}
    print("Keys (values are never shown):")
    for k, label in keys.items():
        print(f"  {label:17} {'set' if env.get(k) else 'missing'}  ({k})")
    print("Reachability (free requests only):")
    probes = [("websites (example: www.wikipedia.org)", "https://www.wikipedia.org/"),
              ("Serper", "https://google.serper.dev/"), ("Million Verifier", "https://api.millionverifier.com/"),
              ("GetLeads", "https://app.getleads.io/api/health")]
    for label, url in probes:
        try:
            resp = transport.get(url, timeout=(6.0, 10.0), max_bytes=4096)
            print(f"  {label:38} reachable (HTTP {resp.status})")
        except NetworkBlocked:
            print(f"  {label:38} BLOCKED by this environment's network")
        except FetchError as exc:
            print(f"  {label:38} not reachable ({exc.kind})")
    mx = MxChecker(transport).check("gmail.com")
    print(f"  {'mail check (DNS)':38} {'works via ' + str(mx.method) if mx.status == 'has_mx' else 'BLOCKED: ' + str(mx.error)}")
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    env.load_dotenv()
    if args.check:
        return check(Transport(env.get("FEF_USER_AGENT")))
    if not args.run_dir:
        log("--run-dir is required (or use --check).")
        return EXIT_SETUP
    cfg = load_config()
    run = RunDir(args.run_dir)
    if args.input:
        if run.exists() and any(run.state_dir.glob("*.json")):
            log(f"{run.path} already has a run in it; continuing it (the --input file is not re-read).")
        else:
            mapping, _ = run.create(args.input, cfg, args.map)
    elif not run.exists():
        log("No run here yet: pass --input <file.csv> the first time.")
        return EXIT_SETUP
    elif args.map:
        run.remap(cfg, args.map)
    header, mapping = run.load_mapping()
    rows = run.load_rows()
    if args.limit:
        rows = rows[:args.limit]

    ledger = Ledger(run.ledger_path)
    transport = Transport(env.get("FEF_USER_AGENT"), min_interval_per_host=args.per_site_delay)
    gl, sk, mv = build_clients(args, transport, ledger)
    problems = check_setup(args, gl, sk, mv)
    if problems:
        for pr in problems:
            log(f"setup: {pr}")
        log("Add the key to .env (or the cloud environment's secrets), or drop the budget flag.")
        return EXIT_SETUP

    cache_dir = Path(env.get("FEF_CACHE_DIR") or (env.REPO_ROOT / ".fef-cache"))
    memo = DomainMemo(cache_dir / "domains.json")
    settings = Settings(segment=args.segment, serp_max=args.serp_max_searches, mv_max=args.mv_max_checks,
                        getleads_max=args.getleads_max_records, getleads_per_domain=args.getleads_per_domain,
                        max_guesses=args.max_guesses, max_subpages=args.max_subpages,
                        concurrency=args.concurrency, verify_mode=args.verify, ai_read=args.ai_read == "on",
                        offline=args.offline)
    if args.offline:
        mx = MxChecker(backends=[], ledger=ledger)
        mx.blocked_reason = "offline mode"
    else:
        mx = MxChecker(transport, backend="auto", ledger=ledger)
    pipe = Pipeline(run, cfg, settings, transport=transport, mx=mx, getleads=gl if not args.offline else None,
                    serper=sk if not args.offline else None, mv=mv if not args.offline else None,
                    memo=memo, ledger=ledger, log=log)

    preview = args.preview if args.preview is not None else (None if args.yes else 3)
    pipe.wave_clean(rows)
    pipe._save(rows)
    if preview is not None:
        work = stratified(rows, max(preview, PREVIEW_BUILD_MIN))
        log(f"Preview: building {len(work)} sample row(s) of {len(rows)}.")
    else:
        work = rows
        log(f"Running {len(rows)} row(s).")
    status = pipe.run_rows(work)
    if status == "ai_read_pending":
        log(ai_read_message(run))
        return EXIT_AI_READ

    summary = summarize(ledger.events())
    if preview is not None:
        extras = {}
        if gl and not args.offline:
            fu = gl.fair_use() or {}
            daily = fu.get("daily") or {}
            if daily:
                extras["GetLeads allowance left today"] = f"{daily.get('remaining')} of {daily.get('cap')} (resets {daily.get('resets_at')})"
        if mv and not args.offline:
            try:
                bal = mv.credits()
                if bal is not None:
                    extras["Million Verifier credits"] = f"{bal}"
            except Exception as exc:  # a bad key shows up here before the full run
                extras["Million Verifier"] = str(exc)
        if pipe.blocked:
            for lane, why in pipe.blocked.items():
                extras[f"Lane off: {lane}"] = why
        prices = {"mv": env.get_float("MV_USD_PER_CHECK"), "serper": env.get_float("SERPER_USD_PER_SEARCH"),
                  "getleads": env.get_float("GETLEADS_USD_PER_CREDIT")}
        est = estimate(rows, work, summary, pipe.budgets, prices=prices, concurrency=args.concurrency,
                       delay=args.per_site_delay, extras=extras)
        shown = pick_preview([r for r in work if r.result], preview)
        stop = ("Preview only: nothing beyond the sample was built. To run the rest, run the same command "
                "with --yes instead of --preview. The sample rows are reused, not paid for again.")
        hint = network_hint(pipe.blocked)
        if hint:
            stop = hint + "\n\n" + stop
        text = render_preview(rows, work, shown, mapping, est, stop)
        (run.path / "preview.md").write_text(text, encoding="utf-8")
        print(text)
        return 0

    counts = write_outputs(run.path, rows, header)
    write_evidence(run.path, rows)
    report = run_report(rows, summary, budgets=pipe.budgets, blocked=pipe.blocked, mapping=mapping)
    hint = network_hint(pipe.blocked)
    if hint:
        report += "\n" + hint + "\n"
    (run.path / "run_report.md").write_text(report, encoding="utf-8")
    print(report)
    log("Wrote " + ", ".join(f"{k} ({v} rows)" for k, v in counts.items()) + f" in {run.path}")
    if args.verify == "manual":
        log("Manual verification: run export_guesses.py next, upload the file to Million Verifier, "
            "then import_report.py with the full report.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
