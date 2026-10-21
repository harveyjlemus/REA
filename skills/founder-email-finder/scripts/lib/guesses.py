"""Build ranked email guesses for one person at one domain.

Order, strongest first:
  1. patterns proven at this domain (a real address matched a real name),
  2. patterns hinted by the shape of addresses seen at the domain,
  3. the calibrated order for the segment, else the starting order.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .config import Config
from .names import PersonName
from .patterns import rank_evidence, render, uses_last


@dataclass
class Guess:
    email: str
    pattern: str
    rank: int
    why: str
    mv: dict | None = None      # Million Verifier answer once checked

    def to_dict(self) -> dict:
        return asdict(self)


def pattern_order(segment: str, cfg: Config) -> tuple[list[str], str]:
    cal = cfg.calibrated_order(segment)
    if cal:
        order, info = cal
        return order, f"calibrated on {info.get('sample_size')} verified {segment} emails"
    return list(cfg.starting_order), "starting order"


def _share_note(segment: str, pid: str, cfg: Config) -> str | None:
    cal = cfg.calibrated_order(segment)
    if not cal:
        return None
    share = (cal[1].get("shares") or {}).get(pid)
    if share is None:
        return None
    return f"{share:.0%} of verified {segment} emails use {pid}"


def build_guesses(name: PersonName, domain: str, *, segment: str, cfg: Config,
                  evidence: list[dict] | None = None, max_guesses: int | None = None,
                  verified_patterns: list[str] | None = None) -> list[Guess]:
    """Ranked, de-duplicated guesses. `verified_patterns` are patterns Million
    Verifier already confirmed for someone else at this domain."""
    max_guesses = max_guesses or cfg.max_guesses
    proven, shape = rank_evidence(evidence or [])
    verified_patterns = verified_patterns or []
    base, base_label = pattern_order(segment, cfg)
    first = name.first_token
    if not first or not domain:
        return []
    middle = name.middle_token or None
    lasts = name.last_variants(cfg.lex.last_name_particles)
    first_forms = [first]

    why: dict[str, str] = {}
    for pid in verified_patterns:
        why.setdefault(pid, "verified for someone else at this domain")
    for e in evidence or []:
        if e.get("strength") in ("proven", "verified"):
            why.setdefault(e["pattern"], f"proven by {e['email']} ({e.get('person') or 'a real address'})")
        else:
            why.setdefault(e["pattern"], f"address shape at this domain: {e['email']}")

    ordered: list[str] = []
    for pid in [*verified_patterns, *proven, *shape, *base]:
        if pid in cfg.patterns and pid not in ordered:
            ordered.append(pid)

    if not lasts:
        # First name only: patterns without a last name, plus the nickname form.
        allowed = [p for p in ordered if not uses_last(cfg.patterns[p])]
        for p in cfg.first_only_order:
            if p in cfg.patterns and p not in allowed:
                allowed.append(p)
        ordered = allowed
        first_forms = [first] + cfg.nickname_forms(first)

    primaries: list[tuple[str, str, str]] = []   # (local part, pattern, why)
    alts: list[tuple[str, str, str]] = []
    for pid in ordered:
        tpl = cfg.patterns[pid]
        note = why.get(pid) or _share_note(segment, pid, cfg) or base_label
        for fi, fform in enumerate(first_forms):
            nick_note = note if fi == 0 else f"{note} (nickname form '{fform}')"
            if uses_last(tpl):
                primaries.append((render(tpl, fform, lasts[0], middle), pid, nick_note))
                for alt in lasts[1:]:
                    alts.append((render(tpl, fform, alt, middle), pid, f"{nick_note}; last name as '{alt}'"))
            else:
                primaries.append((render(tpl, fform, None, middle), pid, nick_note))
    primaries = [p for p in primaries if p[0]]
    # Other spellings of a two-part last name ("horn" for "Van Horn") are less
    # likely than the next pattern, so they go after the strongest primaries.
    alts = [a for a in alts if a[0] and a[0] not in {p[0] for p in primaries}][:cfg.alt_last_top_n]
    keep = max(1, max_guesses - len(alts))
    sequence = primaries[:keep] + alts + primaries[keep:]
    if lasts and cfg.nickname_guess_rank and "first" in cfg.patterns:
        # "mike@" for Michael Rivera: one short-form guess, at a fixed position.
        nick = next((n for n in cfg.nickname_forms(first) if n != first), None)
        local = render(cfg.patterns["first"], nick, None, None) if nick else None
        if local and local not in {s[0] for s in sequence}:
            at = min(len(sequence), cfg.nickname_guess_rank - 1)
            sequence.insert(at, (local, "first", f"short form of {name.first} ('{nick}')"))

    out: list[Guess] = []
    seen: set[str] = set()
    for local, pid, note in sequence:
        email = f"{local}@{domain}"
        if email in seen:
            continue
        seen.add(email)
        out.append(Guess(email=email, pattern=pid, rank=len(out) + 1, why=note))
        if len(out) >= max_guesses:
            break
    return out
