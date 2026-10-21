# Backtest: guess order (local)

Held-out rows with a Million Verifier "ok" email: 853 (roofing 534, pest 291, agencies 28). Up to 6 guesses per person.
None of these rows were used to learn the calibrated order.

| | Starting order | Calibrated order |
|---|---|---|
| Right email is guess #1 | 64.7% | 64.7% |
| Right email in the first 3 | 84.2% | 84.2% |
| Right email anywhere in the 6 | 92.0% | 92.1% |
| Checks per person found | 1.67 | 1.61 |
| Checks per person, misses included | 2.01 | 1.95 |

## Catch-all domains

222 held-out rows (21%) sit on catch-all domains, where Million Verifier can't tell guesses apart, so the finder stops after one check and reports its first guess as risky.
Using the export's own email as the reference, that first guess is right 45.0% of the time.

Notes: this measures the guess order only. It assumes the person's name and the company domain are already known and that Million Verifier answers "ok" for the real address. Finding names (website, GetLeads, Serper) is measured by `make` + `score`.
