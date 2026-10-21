# REA: Founder Email Finder

A Claude Code skill. Give it a list of companies (with or without a person's
name) and it finds the founder or owner, guesses their email most likely first,
and checks each guess with Million Verifier until one passes. Free sources run
first; GetLeads, Serper and Million Verifier touch only the rows that are left,
under budgets you set. Every value carries its source; every blank has a reason.

It follows Jordan Crawford's TAM Contact Harvester method ("read the free pages
with free code, pay only for the residue") and adds the part that method stops
short of: guessing the email and proving it won't bounce.

- [SPEC.md](SPEC.md): what was agreed, including the build prompt in Jordan's style
- [skills/founder-email-finder/SKILL.md](skills/founder-email-finder/SKILL.md): how Claude runs it
- [references/](skills/founder-email-finder/references/): lanes, verification rules, output columns, backtest

## How well the guessing works on your data

Learned from 2,059 owner emails Million Verifier had already passed in your
roofing, pest-control and agency exports, then tested on 853 held-out owners it
never saw ([full backtest](skills/founder-email-finder/references/backtest-local.md)):

| Local business owners | |
|---|---|
| Owners whose email is `first@domain` | 69% |
| Right email is guess #1 | 65% |
| Right email in the first 3 guesses | 84% |
| Right email in the 6 guesses | 92% |
| Million Verifier checks per owner found | 1.6 |
| Owners on catch-all domains (can't be proven by any verifier) | 21% |

That assumes the name and domain are known. How often the name is found
depends on the list; run the live backtest (`backtest.py make` + `score`) on
your own rows to measure it.

## Set up on your computer

```bash
git clone https://github.com/harveyjlemus/REA && cd REA
python3 -m pip install -r requirements.txt
cp .env.example .env          # fill in SERPER_API_KEY, MILLIONVERIFIER_API_KEY, GETLEADS_API_KEY
make check                    # tests + offline demo, no keys, no spend
make install-skills           # links the skill into .claude/skills/
```

Then in Claude Code, from the repo folder: *"Use founder-email-finder on leads.csv"*.

## Set up in a Claude Code cloud session

In the cloud environment's settings (the cloud environment menu in the
session's title bar, then Edit):

- **Network access:** allow `google.serper.dev`, `api.millionverifier.com`,
  `app.getleads.io` and `dns.google`. The website lane reads every company's own
  site, so it needs a broader access level; without one it is skipped and marked
  `network_blocked`, and the other lanes still run.
- **Keys:** add `SERPER_API_KEY`, `MILLIONVERIFIER_API_KEY` and `GETLEADS_API_KEY`
  as secrets or environment variables. A new session picks them up.

`python3 skills/founder-email-finder/scripts/find_founders.py --check` says what
is set and reachable, without spending anything.

## Run it by hand

```bash
S=skills/founder-email-finder/scripts
python3 $S/find_founders.py --input leads.csv --run-dir runs/roofing --preview 3 \
    --serp-max-searches 20 --mv-max-checks 30 --getleads-max-records 50
# exit code 3 = the AI read is waiting: Claude runs agents/page-reader.md, then rerun
python3 $S/find_founders.py --run-dir runs/roofing --yes \
    --serp-max-searches 500 --mv-max-checks 2000 --getleads-max-records 5000
```

Results land in `runs/roofing/`: `send_ready.csv` (Million Verifier "ok" only),
`risky.csv`, `not_found.csv`, `results.csv`, `run_report.md` and the evidence
per row. CSV and `.xlsx` (Scrap.io) inputs both work.

Prefer to verify in the Million Verifier app? Add `--verify manual`, then
`export_guesses.py` → upload → `import_report.py --report <full report>`,
one round per guess.

## Re-learn the guess order from new exports

```bash
python3 $S/calibrate.py --segment local --from export1.csv export2.csv
python3 $S/backtest.py guesses --segment local --from export1.csv export2.csv
```

Only aggregate counts are written. Lead files and run folders are gitignored:
they hold real people's names and emails.

## Credits

Method: Jordan Crawford / Blueprint GTM (TAM Contact Harvester, Crawford, the
gtm-engineer-starter conventions). Name lists: US Census 1990 (public domain).
