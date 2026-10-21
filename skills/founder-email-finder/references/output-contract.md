# Output contract

## Files in the run folder

| File | Holds |
|---|---|
| `results.csv` | every input row: the columns below first, then your original columns unchanged |
| `send_ready.csv` | `find_status = found` and verification `ok` (duplicates left out) |
| `risky.csv` | `risky` (catch-all / unknown) and `unverified` (found but not checked) |
| `not_found.csv` | `not_found` and `skipped`, with the reason; names and domains kept for another tool |
| `evidence/<row>.json` | the row's full record: every candidate, address, guess and Million Verifier answer, with sources and dates |
| `cost_ledger.jsonl` | one line per call: provider, units, dollars and basis (measured / imputed / unknown) |
| `run_report.md` | the funnel, sources, reasons, spend and budgets |
| `preview.md` | the last preview |

If one of your input columns already has one of these names, ours gets `_fef` added.

## Columns

| Column | Meaning |
|---|---|
| `find_status` | `found`, `risky`, `unverified`, `awaiting_verification`, `not_found`, `skipped`, `duplicate` |
| `founder_name`, `founder_first`, `founder_last`, `founder_title` | the person chosen |
| `founder_email` | the address (empty unless found, risky or unverified) |
| `verification` | `ok`, `catch_all`, `unknown`, `not_checked` |
| `confidence` | `high`, `medium`, `low` (see verification.md) |
| `email_source` | `input`, `getleads`, `website`, `serper`, `quickenrich`, `guess` |
| `email_pattern` | e.g. `flast` |
| `pattern_evidence` | the real address that proves the pattern, and whose it is |
| `name_source` | `input`, `website`, `getleads`, `serper`, `ai_read` (`input+website` when the site completed a last name) |
| `name_evidence` | page URL and the word-for-word quote |
| `business_email` | a role inbox found for the company (info@, office@), never used as the founder's |
| `top_guess` | the next address to check when nothing is verified yet |
| `other_people` | other people found, with titles |
| `mx_provider`, `site_bucket` | mail provider; website bucket |
| `guesses_checked` | Million Verifier checks spent on guesses for this row |
| `row_cost_usd`, `row_credits` | known dollars (`+ unknown` when a price isn't set) and credits by provider |
| `absent_reason` | why there's no verified email (codes below) |
| `warnings` | `shared_domain_hint`, `name_from_ai_read`, `first_name_only`, `not_founder_level`, `getleads_email_at_other_domain`, `duplicate_of rNNNN` |
| `fef_row_id` | the row's id in the run |

## absent_reason codes

`closed_business`, `no_company_domain`, `no_mail_server`, `domain_does_not_exist`,
`network_blocked`, `site_dead`, `site_blocked`, `no_name_found`, `lane_off`,
`budget_reached`, `all_guesses_invalid`, `catch_all_unresolved`,
`unknown_unresolved`, `check_failed`, `awaiting_verification`, `not_checked`,
`no_guesses`, `duplicate`.

A blank field never means "didn't look": the lane's status and reason are in
`evidence/<row>.json` under `lanes`.
