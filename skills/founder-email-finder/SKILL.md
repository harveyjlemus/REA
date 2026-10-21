---
name: founder-email-finder
description: Find the founder or owner of each company in a CSV and an email for them that won't bounce. Use whenever the user wants owner or founder emails for a list of companies or websites, wants to guess and verify email permutations, wants to fill in missing emails or last names in an Apollo, Scrap.io or QuickEnrich export, or mentions Million Verifier, GetLeads or Serper for finding emails. Free lanes first (the company's own website, DNS); paid lanes (GetLeads, Serper, Million Verifier) only with a budget the user sets.
version: "1.0"
author: "Built for harveyjlemus/REA, after Jordan Crawford's TAM Contact Harvester method (Blueprint GTM)"
requires-keys: []
optional-keys: [GETLEADS_API_KEY, SERPER_API_KEY, MILLIONVERIFIER_API_KEY]
---

# Founder Email Finder

A CSV of companies goes in. For each one it finds the founder or owner, builds
email guesses most likely first, and checks them with Million Verifier until one
is "ok". Free sources run first; paid lookups touch only the rows that are left,
under a budget. Every value carries its source, and every blank has a reason.

All paths below are relative to this skill's folder. Run commands from the repo
root so `.env` is found.

## 0. Before running anything

1. Confirm the input file with the user, and what's in it (company only, first
   names, full names, existing emails). Every format works; the preview shows the mapping.
2. Run `python3 scripts/find_founders.py --check`. It says which keys are set
   (never their values) and which hosts this machine can reach, and spends nothing.
   If a lane is blocked in a cloud session, pass on the hint it prints; don't
   work around it with made-up data.
3. Ask the user for budgets: a small one for the preview (for example 20 Serper
   searches, 30 checks, 50 GetLeads credits) and, after they've seen the preview,
   the full run's. Never pick paid budgets yourself. A lane with no budget stays off.

## 1. Preview (always first)

```bash
python3 scripts/find_founders.py --input <file.csv> --run-dir runs/<name> --preview 3 \
    --serp-max-searches 20 --mv-max-checks 30 --getleads-max-records 50
```

It builds about 10 sample rows on the same lanes and budgets as the full run,
prints a card for 3 of them, the column mapping, and an estimate for the whole
file, then stops (exit 0). The same text is in `runs/<name>/preview.md`.

Show the user the mapping, the cards and the estimate. Ask:
- Is the mapping right? Fix it with `--map field=Header` (only before the full run).
- Segment: rows are `local` or `startup` from their industry; `--segment` forces one.
- What budgets for the full run?

Wait for a clear yes before step 3.

## 2. AI read (only when a command exits with code 3)

Exit code 3 means plain code found no owner on some saved pages and a small
model should read them. For each file in `runs/<name>/ai_read/batches/` that
has no file of the same name in `runs/<name>/ai_read/answers/`, launch a
subagent with model `haiku`:

> Follow the instructions in `<this skill>/agents/page-reader.md`.
> Batch file: `runs/<name>/ai_read/batches/batch-001.jsonl`.
> Write the answers to `runs/<name>/ai_read/answers/batch-001.jsonl`.

Run up to 4 at a time. If only one or two rows are waiting, you may read them
yourself following the same instructions. Then run the same command again: it
checks every answer (the quote has to be word-for-word on the page) and
continues. `--ai-read off` skips this lane.

## 3. Full run

```bash
python3 scripts/find_founders.py --run-dir runs/<name> --yes \
    --serp-max-searches <N> --mv-max-checks <N> --getleads-max-records <N>
```

Rows built in the preview are reused. Caps count everything this run folder
has spent, preview included, so a rerun never pays twice or past the cap. If
it stops (a cap, exit code 3, a network problem), the same command resumes.

Then tell the user, from `runs/<name>/run_report.md`: how many verified emails
are in `send_ready.csv`, how many risky ones (catch-all, unknown, unchecked)
are in `risky.csv`, the top reasons in `not_found.csv`, and the spend per
verified email.

## Manual Million Verifier round trip (instead of the API)

```bash
python3 scripts/find_founders.py --run-dir runs/<name> --yes --verify manual ...
python3 scripts/export_guesses.py --run-dir runs/<name>        # one address per person
# the user uploads mv_upload_round1.csv to Million Verifier and downloads the FULL report
python3 scripts/import_report.py --run-dir runs/<name> --report <full report.csv>
python3 scripts/export_guesses.py --run-dir runs/<name>        # next round for the unresolved
```

`import_report.py` also reads a QuickEnrich (or any enrichment) results file:
the addresses it finds are added and verified like any other.

## Calibrate and backtest (run when the user has new verified exports)

```bash
python3 scripts/calibrate.py --segment local --from <verified exports...>
python3 scripts/backtest.py guesses --segment local --from <same files> --out references/backtest-local.md
```

Calibration learns the guess order from emails Million Verifier already passed,
holding 30% of domains out; the backtest scores only the held-out rows. Only
aggregate counts are written. Exports themselves stay out of git.

## Rules

- Preview first, and wait for the user's yes before the full run.
- Paid lanes are off until the user sets a budget. Every call, free or paid, is in
  `cost_ledger.jsonl`; an unknown price is recorded as unknown, never $0.
- Never invent or edit a name, email or quote. Every name has a source URL and a
  word-for-word quote; every blank has an `absent_reason`.
- Only `send_ready.csv` (Million Verifier "ok") is safe to send. Say so when you
  report results; catch-all guesses are not.
- Don't print or log API keys. Don't fetch LinkedIn pages (Google results via
  Serper only). Don't probe mail servers directly.
- Run folders hold real people's data: keep them out of git (`runs/` is ignored).

## Files

- `scripts/find_founders.py`: preview, full run, resume, `--check`.
- `scripts/export_guesses.py`, `scripts/import_report.py`: the CSV round trip.
- `scripts/calibrate.py`, `scripts/backtest.py`: learn and test the guess order.
- `scripts/lib/`: one module per lane (`website.py`, `mx.py`, `getleads.py`,
  `serper.py`, `millionverifier.py`), plus `pipeline.py`, `guesses.py`, `report.py`.
- `config/`: guess orders (`patterns.yaml`, `patterns.calibrated.yaml`), column
  names, search queries, word lists, US Census name lists.
- `agents/page-reader.md`: the AI read instructions.
- `references/`: `lanes.md`, `verification.md`, `output-contract.md`, `backtest-local.md`.
- `examples/demo.py`: the whole pipeline on a fictional market, offline.
- `tests/`: `python3 -m pytest -q tests` (offline, no keys).
