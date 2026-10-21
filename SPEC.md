# Founder Email Finder: build spec

*Draft for your approval · 2026-10-21 · repo: harveyjlemus/REA*

**What it does:** you give it a list of companies, with or without a person's name. For each
company it finds the founder or owner, guesses their email, and checks each guess with Million
Verifier until one passes. Free sources run first. Paid lookups only touch the rows that are left,
under a budget you set. Every row shows where each fact came from and what it cost.

It follows Jordan Crawford's TAM Contact Harvester method ("read the free pages with free code,
pay only for the residue") and the rules in his free starter kit. Then it adds the part his
Harvester doesn't do: guessing the email and proving it won't bounce.

---

## 1. The prompt

This is the build prompt, written the way Jordan writes them. It's what I'll build from, and you
can paste it into Claude Code on its own.

```text
Build a Claude Code skill called founder-email-finder.

The job: a CSV of companies goes in. For each company I want the founder or
owner's name and an email that won't bounce, with proof for both. Targets are
local business owners (roofing, HVAC, agencies) and startup founders.

Rows arrive four ways. Handle all four without asking me to clean the file:
1. Company and website only. Find the person.
2. First name and website. Find the last name if you can, then the email.
3. Full name and website. Find the email.
4. A Scrap.io, Apollo, QuickEnrich or Million Verifier export. Map the columns
   yourself and show me the mapping before you spend anything.

Free first. Pay only for the residue. Run the lanes in this order, and stop a
row the moment it has an email Million Verifier calls "ok":
1. Clean. Normalize domains. Drop duplicates, closed businesses and rows whose
   "website" is a Facebook or Yelp page. $0.
2. Mail check. Look up MX records. No mail server, no guessing. $0.
3. Website. Fetch the homepage and sort it into one of five buckets: email in
   the HTML, plain server-rendered page, JavaScript shell, bot wall, dead. Read
   About, Team and Contact pages with plain code. Pull names, roles and every
   email on the domain. One real email at the domain gives you the company's
   pattern. $0.
4. GetLeads. Full names go to enrich-from-person. Company-only rows go to
   decision-makers. $0 on my plan, but it uses my daily fair-use allowance, so
   send only what's left.
5. AI read. A small model re-reads the pages already on disk for names the code
   missed. It must quote the sentence it used, and the quote must be on the page.
6. Serper. Google the owner's name (LinkedIn result titles, local listings) and
   "@domain" for the pattern. Budget-capped.
7. Guess and verify. Rank the guesses, most likely first; a proven pattern goes
   first. Check them one at a time with Million Verifier and stop at the first
   "ok". A catch_all answer means every guess will look valid: stop spending on
   that domain and mark the row risky.

Also give me a CSV round trip for Million Verifier: export one guess per person,
I upload it, you import the full report, pick winners and export the next round.

Rules:
- Preview 3 rows first. Show me a card per row and the cost estimate for the
  whole file. Wait for my yes.
- Paid lanes stay off until I set a budget. Log every call, free or paid, to a
  cost ledger. An unknown cost is unknown, never $0.
- Every field is present, or absent with a reason. "We didn't look" and "we
  looked and found nothing" must never look the same.
- Every name carries a source URL and a verbatim quote. Never invent a name.
- Don't scrape LinkedIn. Don't probe mail servers from my machine. Be polite to
  websites.
- Calibrate the guess order from my own verified exports before trusting a
  default.

Done means: send_ready.csv holds only "ok" emails, every row has a source, a
status and a cost, the tests pass offline with no API keys, and a backtest on
my own verified rows reports the hit rate and the checks per email found.
```

---

## 2. How a row moves through it

```
input CSV
  -> 1 Clean ........... $0
  -> 2 Mail check ...... $0
  -> 3 Website ......... $0
  -> 4 GetLeads ........ $0 on your plan (uses daily fair-use allowance)
  -> 5 AI read ......... no extra cost inside Claude Code
  -> 6 Serper .......... 2,500 free searches, then paid
  -> 7 Guess + verify .. 1 Million Verifier credit per check
  -> results, send_ready, risky, not_found
```

A row stops as soon as it has an email Million Verifier calls "ok". Which lanes it needs depends
on what it starts with:

| Row starts with | Path |
|---|---|
| An email already (Apollo, Scrap.io) | Verify it. Guess only if it fails. |
| Full name + website | GetLeads enrich, then website (for the pattern), then guess + verify |
| First name + website | Website and Serper look for the last name, GetLeads if found, then guess + verify |
| Company + website only | Website, GetLeads decision-makers, AI read, Serper, then as above |
| No website of its own (Facebook page, Yelp, none) | Stops as `no_company_domain`: there's no domain to guess at |

It's built for runs from 10 to 50,000 rows. Progress is saved as it goes, so a big run can stop
and pick up where it left off.

---

## 3. Inputs

Any CSV. The tool recognizes these formats by their headers and shows you its column mapping in
the preview, before it spends anything:

| Format | Columns it uses |
|---|---|
| Scrap.io (Google Maps) | Name, Website / Website (root url), City, State, Main type, Is closed permanently, Individual email (+ its first/last name), All emails (its `pattern` field is free pattern evidence), Linkedin link |
| Apollo export (both layouts in your Drive) | First Name, Last Name, Title, Email, Email Status, Company Name, Company Website / Website Short / Company Domain, Lead City / State, Email Domain Catchall |
| QuickEnrich export | First / last name, company, domain, email |
| Million Verifier report | Your original columns plus quality, result, free, role |
| Anything else | Matched by common header names; you confirm the mapping in the preview |

Rows that Scrap.io marks "closed permanently" are skipped for free.

---

## 4. The lanes, in order

| # | Lane | What it does | Cost | Key |
|---|---|---|---|---|
| 1 | Clean | Cleans domains (strips www, paths, tracking tags). Drops duplicates, closed businesses and social-only "websites". | $0 | none |
| 2 | Mail check | Does the domain accept email at all (MX records)? Which provider (Google, Microsoft, other)? No mail server means no guessing. | $0 | none |
| 3 | Website | Reads the homepage and up to 4 About / Team / Contact pages with plain code. Sorts the site into one of the five buckets below. Pulls names, roles and every email on the domain. | $0 | none |
| 4 | GetLeads | Full names go to enrich-from-person. Company-only rows go to decision-makers (Founder, CEO, President; it skips people titled only "Owner"). | $0 on your unlimited plan, but it draws on your daily fair-use allowance | `GETLEADS_API_KEY` |
| 5 | AI read | A small model re-reads saved pages for names the code missed. It must quote its sentence, and the quote is checked against the page. | No extra cost inside Claude Code (uses your Claude plan) | none |
| 6 | Serper | Google searches for the owner's name, and for "@domain" to find the email pattern. | 2,500 free searches, then prepaid packs (third-party figures: about $0.30 to $1.00 per 1,000) | `SERPER_API_KEY` |
| 7 | Guess + verify | Ranked guesses, checked one at a time with Million Verifier until one is "ok". | 1 credit per check | `MILLIONVERIFIER_API_KEY` |

### The five website buckets (from Jordan's Harvester)

| Bucket | Meaning | What happens |
|---|---|---|
| email_in_html | An email at the domain is already in the page source | Extract it, including Cloudflare-hidden emails, which free code can decode |
| server_rendered | A normal, readable page | Read the About / Team / Contact pages |
| js_shell | Empty until JavaScript runs (some Wix and Squarespace builds) | Skip to GetLeads and Serper; counted so you can see how many |
| bot_walled | Cloudflare or a captcha blocks it | Same as js_shell |
| dead | Doesn't load, parked, or for sale | Stop the row unless GetLeads finds someone |

### Why one email changes everything

If the site shows any real person's email (say `jsmith@acme.com` next to "John Smith"), the
company's pattern is `{f}{last}`. The founder's email in that pattern goes first, and it's often
the only check needed. Scrap.io's "All emails" field and the Serper "@domain" search feed this too.

---

## 5. Guessing

Names get cleaned first: accents removed (José becomes jose), apostrophes dropped (O'Brien becomes
obrien), suffixes dropped (Jr, III, CPA), and two-part last names tried both ways (Van Horn becomes
vanhorn, and horn).

**Starting order**, until calibration replaces it with your own numbers:

| Rank | Pattern | Example for Mike Rivera |
|---|---|---|
| 1 | first | mike@ |
| 2 | first.last | mike.rivera@ |
| 3 | flast | mrivera@ |
| 4 | firstlast | mikerivera@ |
| 5 | firstl | miker@ |
| 6 | f.last | m.rivera@ |

- A pattern proven at the domain jumps to rank 1.
- First-name-only rows get first-name guesses only: `first@`, plus a common nickname (michael and mike).
- At most 6 guesses per person. You can change that.
- Once a pattern verifies at a domain, everyone else at that domain gets it first.

**Calibration from your own data.** Your Drive has Apollo and Million Verifier exports with
verified owner emails (roofing, HVAC, agencies). A free script counts which patterns those owners
actually use, by industry and company size, and writes that as the guess order. It writes separate
orders for local owners and startup founders once there's data for each. This is Jordan's habit
of benchmarking against your own data before trusting a default.

---

## 6. Verifying with Million Verifier

### Inside the tool (the default)

| Million Verifier says | What the tool does | Row ends as |
|---|---|---|
| ok | Stop. This is the email. | verified, high confidence |
| invalid or disposable | Try the next guess | |
| catch_all | Stop spending on this domain. It accepts every address, so every guess would look the same. | risky: best guess plus its evidence |
| unknown | Retry once at the end of the run, then stop that domain | inconclusive |
| error | Wait and retry twice | check_failed |

- It checks your credit balance before the run and stops at your cap.
- Million Verifier answers "200 OK" even for a bad API key, so the tool reads the error field and
  stops the run, instead of logging thousands of failed checks.
- Catch-all and unknown results are remembered per domain for 30 days, so re-runs don't pay twice.

### CSV round trip (when you'd rather verify by hand)

1. `export` writes `mv_upload_round1.csv`, with one guess per person (the top one).
2. You upload it to Million Verifier and download the **full report**. The "OK only" file hides
   the difference between catch-all and invalid.
3. `import` reads the report, picks the winners, and writes round 2 with the next guess for
   everyone still unresolved.

Doing it in rounds keeps the credit savings of stopping at the first "ok". If you'd rather do one
upload, you can export every guess at once.

The same import works for QuickEnrich: `not_found.csv` keeps first name, last name, company and
domain, so you can run it through QuickEnrich by hand and import its results file.

---

## 7. Outputs

Everything goes in one run folder:

| File | What's in it |
|---|---|
| `results.csv` | Every input row, plus the columns below |
| `send_ready.csv` | Only Million Verifier "ok" emails |
| `risky.csv` | Catch-all and unknown rows: best guess, pattern evidence, confidence |
| `not_found.csv` | Rows with no email, and the reason |
| `evidence/<row>.json` | Every fact for the row, with its source URL, date and quote |
| `cost_ledger.jsonl` | One line per call, free or paid |
| `run_report.md` | The funnel (rows, names found, emails found, verified) and cost per verified email |

**New columns:** founder_name, founder_first, founder_last, founder_title, name_source,
name_evidence, email, email_source, email_pattern, pattern_evidence, verification, confidence,
mx_provider, site_bucket, guesses_checked, row_cost_usd, status, absent_reason.

**Confidence**
- **high:** Million Verifier "ok" on a domain that isn't catch-all.
- **medium:** a catch-all domain, but another real email at that domain proves the pattern, and
  the name has a source.
- **low:** catch-all or unknown, with nothing proving the pattern. It's a guess.

**Absent reasons**, so a blank never means "didn't look": `no_company_domain`, `closed_business`,
`no_mail_server`, `site_dead`, `site_blocked`, `no_name_found`, `catch_all_unresolved`,
`unknown_unresolved`, `budget_reached`, `network_blocked`, `lane_off`.

---

## 8. Rules it follows (Jordan's, plus a few for email)

1. **Preview first.** `--preview 3` runs three rows, shows a card for each and the cost estimate
   for the whole file, then waits for your yes.
2. **Paid lanes are off until you set a budget:** `--serp-max-searches`, `--mv-max-checks`,
   `--getleads-max-records`.
3. **A cost ledger for every call.** An unknown cost is recorded as unknown, never as $0.
4. **Absent, with a reason.** "We didn't look" and "we looked and found nothing" never look the same.
5. **No invented names.** Every name has a source URL and a word-for-word quote.
6. **Polite to websites.** It identifies itself, follows robots.txt, makes one request per second
   per site, and reads at most 5 pages per site.
7. **No LinkedIn scraping.** It only reads Google's result titles and snippets, through Serper.
8. **No mail-server probing from your machine.** That can get your IP or domain blacklisted.
   Million Verifier does the checking.
9. **Keys stay out of logs.** They're read from `.env` or the environment, and never printed.
10. **Re-runs resume.** Finished rows and saved pages are reused, not paid for twice.

---

## 9. Where it runs

**Your computer:** clone REA, run `python3 -m pip install -r requirements.txt`, copy `.env.example`
to `.env` and fill in the three keys, run `make install-skills`, then type `/founder-email-finder`
in Claude Code.

**This cloud environment** needs three changes in its settings (the cloud environment menu in the
session's title bar, then Edit):
- **Network:** allow `google.serper.dev`, `api.millionverifier.com`, `app.getleads.io` and
  `dns.google`. This container has no DNS of its own, so the mail check goes over HTTPS.
- **The website lane** fetches every company's own site, which a list of allowed domains can't
  cover. It needs a broader network access level. Without that, the website lane is skipped and
  marked `network_blocked`, and the other lanes still run.
- **Keys:** add `SERPER_API_KEY`, `MILLIONVERIFIER_API_KEY` and `GETLEADS_API_KEY` under Network
  secrets, or as environment variables. A new session picks them up. Don't paste keys into chat.

Steps: https://code.claude.com/docs/en/cloud-environments#network-access

---

## 10. What I'll build

```
REA/
├── SPEC.md                     this file
├── README.md                   setup in 5 steps
├── Makefile                    make check, make install-skills
├── requirements.txt
├── .env.example                the three keys and your user-agent
├── .gitignore                  .env, run folders, your lead files
└── skills/founder-email-finder/
    ├── SKILL.md                what Claude Code reads
    ├── scripts/
    │   ├── find_founders.py    preview, then the full run
    │   ├── export_guesses.py   Million Verifier round trip, out
    │   ├── import_report.py    Million Verifier / QuickEnrich round trip, in
    │   ├── calibrate.py        guess order from your verified exports
    │   └── lib/                columns, domains, mx, website, names, guesses,
    │                           serper, getleads, millionverifier, ledger, cache
    ├── config/                 guess orders, column names, search queries, role words
    ├── agents/page-reader.md   instructions for the AI read
    ├── references/             lanes, verification rules, output contract
    └── tests/                  offline: fake sites, recorded API replies, sample exports
```

**Tests** run offline, with no keys and no spend. They check that:
- each export format maps correctly, and domains get cleaned
- sites land in the right bucket
- emails get extracted (mailto links, Cloudflare-hidden, JSON-LD), and so do names and roles
- patterns get detected, and guesses get ranked
- the Million Verifier rules hold: stop on "ok", stop on a catch-all domain, catch the bad key that still answers "200 OK"
- budgets stop spending, every call lands in the ledger, and a stopped run resumes
- the CSV round trip works
- one full run on fictional companies works end to end

**Backtest on your data:** take rows from your exports where the email is already verified, hide
the email, run the finder, and measure how often it gets the exact email back and how many checks
that took. That gives you a real hit rate and cost per email before you run a new list.

**Done means:**
- `make check` passes with no keys.
- `--preview 3` on real rows shows cards and a cost estimate, then stops.
- `send_ready.csv` holds only "ok" emails.
- Every row has a status, a reason and a cost.
- The backtest report shows the hit rate and the checks per email found.

**Not in v1** (can be added later): reading JavaScript-only sites through a paid scraper such as
Firecrawl, finding a company's website from its name alone, phone numbers, and pushing results
into a campaign tool.

---

## 11. Decisions I need from you before building

1. **Company-only rows:** website first, which saves your GetLeads daily allowance (you'd used
   about 490K of 500K today when I checked), or GetLeads first, which is faster?
2. **GetLeads emails:** should they also get a Million Verifier check (1 credit each) before they
   go in `send_ready.csv`?
3. **Your Drive exports:** may I read 2 or 3 of them (read-only) for calibration and the backtest?
4. **Name:** is `founder-email-finder` OK?

---

## 12. Decisions made, and where the build differs from this draft

Your answers: build it as written, name `founder-email-finder`; website first
for company-only rows; GetLeads emails get a Million Verifier check too; read
three Drive exports for calibration (roofing owners, pest control, agencies; the
9 MB HVAC file was too large for the Drive connector).

Changes made while building, each for a reason found in testing:
- **Column names:** the email column is `founder_email` and the status column is
  `find_status`, because Apollo and Scrap.io files already have an `Email` column.
  Statuses `awaiting_verification` and `duplicate` were added.
- **Calibrated order (local owners):** `first`, `flast`, `first.last`,
  `firstlast`, `firstl`. Learned from your verified rows, it replaces the starting order.
- **Nickname guess:** "Michael Rivera" also gets `mike@` at guess #4. The
  backtest showed about 2% of owners use the short form.
- **Budgets count the whole run folder**, preview and resumes included, so a
  rerun can never spend past the cap.
- **Blocked networks:** a proxy 403, or a DNS that says "no such domain" for
  everything, is reported as `network_blocked`, never as a dead company.
- **Excel input:** `.xlsx` files (your Scrap.io exports) are read directly.
- **AI read:** it runs only as a Claude Code subagent; there's no unattended API mode.

---

Sources:
[TAM Contact Harvester post](https://edge.blueprintgtm.com/p/a-tam-of-named-contacts-for-0) ·
[On the Edge tiers](https://edge.blueprintgtm.com/p/what-you-get-at-each-tier-free-50) ·
[Jordan's starter kit](https://github.com/SantaJordan/gtm-engineer-starter) ·
[GetLeads API docs](https://www.getleads.io/docs/) ·
[Million Verifier API](https://developer.millionverifier.com/) ·
[Million Verifier result types](https://help.millionverifier.com/email-verification/email-verification-results) ·
[Serper pricing breakdown](https://apiserpent.com/blog/serper-pricing-credits-explained) ·
[QuickEnrich](https://www.producthunt.com/products/quickenrich?embed=true)
