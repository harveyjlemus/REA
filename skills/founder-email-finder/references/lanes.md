# Lanes

Free lanes run on every row. Paid lanes run only with a budget, and only on the
rows still open (no verified email yet). A row stops the moment Million
Verifier says "ok" to an address for it.

| # | Lane | Module | Finds | Cost | Needs |
|---|---|---|---|---|---|
| 1 | Clean | `pipeline.wave_clean` | the company's own domain; the person in the row; duplicates, closed businesses, social-only "websites" | $0 | nothing |
| 2 | Mail check | `mx.py` | MX records and the mail provider; no mail server means no guessing | $0 | DNS, or `dns.google` over HTTPS |
| 3 | Website | `website.py`, `extract.py` | homepage bucket, up to 4 About / Team / Contact pages, names, titles, every address on the domain | $0 | outside web access |
| 4 | GetLeads | `getleads.py` | enrich-from-person for full names; decision-makers for company-only rows (skips people titled only "Owner") | $0 on the unlimited plan; uses the daily fair-use allowance | `GETLEADS_API_KEY`, `--getleads-max-records` |
| 5 | AI read | `ai_read.py`, `agents/page-reader.md` | owners plain code missed on saved pages; quote must be on the page | no extra cost in Claude Code | Claude Code (subagent, model haiku) |
| 6 | Serper | `serper.py` | owner names from Google results (LinkedIn titles, listings, knowledge panel); `"@domain"` for the pattern | 2,500 free searches, then prepaid | `SERPER_API_KEY`, `--serp-max-searches` |
| 7 | Guess + verify | `guesses.py`, `verify.py`, `millionverifier.py` | ranked guesses, checked one at a time until "ok" | 1 credit per check | `MILLIONVERIFIER_API_KEY`, `--mv-max-checks` |

## Order by how a row starts

| Row starts with | Path |
|---|---|
| An email | verify it; guess only if it fails |
| Full name + website | GetLeads enrich, then website (pattern evidence), then guess + verify |
| First name + website | website and Serper look for the last name, then GetLeads, then guess + verify |
| Company + website only | website, then GetLeads decision-makers, AI read, Serper, then as above |
| No website of its own | stops: `no_company_domain` |

Company-only rows read the website before GetLeads so the daily fair-use
allowance is spent only on what the free pass couldn't solve.

## The five website buckets

`email_in_html`, `server_rendered`, `js_shell`, `bot_walled`, `dead`. Only the
first two are read further. A robots.txt that disallows the site counts as
`bot_walled`. A site that redirects to another domain is read, and the reason
field says where it went.

## Pattern evidence

An address at the domain that matches a real person's name on the site (or in
Scrap.io's "All emails", or a Google snippet) **proves** the company's pattern
(`jprice@` next to "Jonah Price" proves `flast`). Proven patterns go first in
the guesses. An address without a name only hints at a pattern by its shape and
moves it up less. Once Million Verifier confirms a pattern for one person at a
domain, everyone else there gets it first, and it is remembered for 30 days.

## When the environment blocks a lane

A proxy "403" or a DNS that answers "no such domain" for everything is reported
as `network_blocked`, never as a dead company. The lane switches off for the run,
the other lanes continue, and the command prints where to change the setting.
