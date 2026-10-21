# Verification

## The waterfall (API mode, the default)

Guesses are checked one at a time, best first:

| Million Verifier answers | The tool | The row ends as |
|---|---|---|
| `ok` | stops; this is the email | `found`, verification `ok`, confidence `high` |
| `invalid`, `disposable` | checks the next guess | (continues) |
| `catch_all` | stops spending on the whole domain: every address there looks the same | `risky`, the first guess (or a found address) with its evidence |
| `unknown` | retries once at the end of the run with a 60 s timeout; if still unknown, stops the domain | `risky`, verification `unknown` |
| `error` | the client already retried twice | `not_found`, `check_failed` |

- The credit balance is read in the preview. The run stops at `--mv-max-checks`.
- Million Verifier answers HTTP 200 even for a bad key, with the problem in the
  `error` field. The tool reads it and switches the lane off, instead of
  recording thousands of failed checks.
- Addresses found by a lane (input, GetLeads, the website, Google snippets) are
  checked before any guess. GetLeads addresses are re-checked too, so every row in
  `send_ready.csv` passed the same check.
- Catch-all answers and confirmed patterns are remembered per domain for 30 days
  in `.fef-cache/domains.json`, so re-runs don't pay twice.

## Confidence

- **high**: Million Verifier `ok` on a domain that isn't catch-all.
- **medium**: catch-all or unknown, but the address was found by a lane (not
  guessed) or another real address at the domain proves its pattern, and the
  name has a source.
- **low**: catch-all or unknown with only a guess behind it.

Only `send_ready.csv` (`ok`) is safe to send. Risky rows are there to decide on.

## The CSV round trip (`--verify manual`)

1. `find_founders.py ... --verify manual` builds guesses and stops before Million Verifier.
2. `export_guesses.py` writes `mv_upload_roundN.csv`: one address per person (the
   next unchecked one), or every remaining guess with `--per-person all`.
3. Upload it to Million Verifier and download the **full report**. The "OK only"
   file can't tell catch-all from invalid.
4. `import_report.py --report <file>` applies the same rules as the API path and
   rewrites the outputs. People still unresolved get the next guess in the next
   export.

One address per person per round keeps the waterfall's savings: most people are
resolved by round 1 or 2 (the local-owner backtest needs 1.6 checks per person found).
