"""The verification waterfall: check guesses one at a time, most likely first,
and stop at the first "ok".

  ok                   stop: this is the email                       -> verified
  invalid, disposable  try the next guess
  catch_all            stop spending on the whole domain             -> catch_all
  unknown              retry once at the end of the run              -> unknown
  error                (the client already retried twice)            -> check_failed

The same rules apply to answers read back from a Million Verifier report
(the CSV round trip), so both paths end the same way."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .budget import Budget
from .cache import DomainMemo

CONTINUE = {"invalid", "disposable"}


@dataclass
class Outcome:
    status: str            # verified | catch_all | unknown | all_invalid | budget_reached | check_failed | waiting
    email: str | None = None
    pattern: str | None = None
    checks: int = 0
    detail: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def apply_answer(guess: dict, result: str, *, domain: str, memo: DomainMemo | None,
                 run_unknown: set[str] | None = None) -> str | None:
    """Record one answer. Returns a terminal status, or None to keep going."""
    if result == "ok":
        if memo:
            memo.add_verified_pattern(domain, guess["pattern"])
            if memo.status(domain) == "catch_all":
                memo.clear_status(domain)
        return "verified"
    if result == "catch_all":
        if memo:
            memo.set_status(domain, "catch_all")
        return "catch_all"
    if result == "unknown":
        if run_unknown is not None:
            run_unknown.add(domain)
        return "unknown"
    if result == "error":
        return "check_failed"
    return None


def waterfall(guesses: list[dict], *, domain: str, mv, budget: Budget, memo: DomainMemo | None,
              row_id: str | None, run_unknown: set[str] | None = None, timeout_s: int = 20) -> Outcome:
    """Check `guesses` (dicts with email, pattern, mv) in order. Guesses already
    answered (a resumed run) are read back, not re-checked."""
    if not guesses:
        return Outcome("all_invalid", detail="no guesses")
    if memo and memo.status(domain) == "catch_all":
        best = guesses[0]
        return Outcome("catch_all", email=best["email"], pattern=best["pattern"],
                       detail="domain remembered as catch-all; no credits spent")
    if run_unknown is not None and domain in run_unknown and not any(g.get("mv") for g in guesses):
        return Outcome("unknown", email=guesses[0]["email"], pattern=guesses[0]["pattern"],
                       detail="another address at this domain came back unknown in this run")
    checks = 0
    for g in guesses:
        if g.get("mv"):
            result = g["mv"]["result"]
        else:
            if not budget.take(1):
                return Outcome("budget_reached", email=None, checks=checks,
                               detail=f"Million Verifier cap reached ({budget.limit} checks)")
            answer = mv.verify(g["email"], timeout_s=timeout_s, row_id=row_id)
            g["mv"] = answer.to_dict()
            result = answer.result
            checks += 1
        status = apply_answer(g, result, domain=domain, memo=memo, run_unknown=run_unknown)
        if status == "verified":
            return Outcome("verified", email=g["email"], pattern=g["pattern"], checks=checks)
        if status == "catch_all":
            best = guesses[0]
            return Outcome("catch_all", email=best["email"], pattern=best["pattern"], checks=checks,
                           detail=f"{g['email']} answered catch_all")
        if status == "unknown":
            return Outcome("unknown", email=g["email"], pattern=g["pattern"], checks=checks,
                           detail=f"{g['email']} answered unknown")
        if status == "check_failed":
            return Outcome("check_failed", email=None, checks=checks, detail=f"{g['email']} errored after retries")
    return Outcome("all_invalid", checks=checks, detail=f"all {len(guesses)} guesses invalid")


def from_report(guesses: list[dict], answers: dict[str, dict], *, domain: str,
                memo: DomainMemo | None) -> Outcome:
    """Same rules, answers from an imported report. Unanswered guess -> waiting."""
    for g in guesses:
        if not g.get("mv"):
            ans = answers.get(g["email"].lower())
            if not ans:
                return Outcome("waiting", email=g["email"], pattern=g["pattern"],
                               detail="next guess not verified yet: export the next round")
            g["mv"] = ans
        status = apply_answer(g, g["mv"]["result"], domain=domain, memo=memo)
        if status == "verified":
            return Outcome("verified", email=g["email"], pattern=g["pattern"])
        if status == "catch_all":
            return Outcome("catch_all", email=guesses[0]["email"], pattern=guesses[0]["pattern"],
                           detail=f"{g['email']} answered catch_all")
        if status in ("unknown", "check_failed"):
            return Outcome("unknown", email=g["email"], pattern=g["pattern"], detail=f"{g['email']} answered {g['mv']['result']}")
    return Outcome("all_invalid", detail=f"all {len(guesses)} guesses invalid")
