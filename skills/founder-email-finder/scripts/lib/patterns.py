"""Email patterns: render a pattern for a person, and work out which pattern(s)
a real address was built with.

A pattern is "proven" at a domain when a real address there matches a real
person's name (jsmith@acme.com next to "John Smith"). Without a name, the shape
of an address ("jsmith") is only a hint, and is kept as such."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .config import Config
from .names import PersonName, email_token

TOKEN_RE = re.compile(r"\{(first|last|f|l|m)\}")
LOCAL_OK = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")


def uses_last(template: str) -> bool:
    return "{last}" in template or "{l}" in template


def render(template: str, first: str, last: str | None = None, middle: str | None = None) -> str | None:
    """Fill a pattern with already-cleaned tokens. None if a needed piece is missing."""
    values = {
        "first": first or None,
        "last": last or None,
        "f": first[:1] if first else None,
        "l": last[:1] if last else None,
        "m": middle[:1] if middle else None,
    }
    missing = False

    def sub(m: re.Match) -> str:
        nonlocal missing
        v = values[m.group(1)]
        if not v:
            missing = True
            return ""
        return v

    out = TOKEN_RE.sub(sub, template)
    if missing or not out or len(out) > 64 or ".." in out or not LOCAL_OK.match(out):
        return None
    return out


def matching_patterns(local: str, name: PersonName, cfg: Config) -> list[str]:
    """Pattern ids that turn this person's name into this local part."""
    local = local.lower()
    firsts = [name.first_token] + cfg.nickname_forms(name.first_token)
    if name.nickname:
        firsts.append(email_token(name.nickname))
    lasts = name.last_variants(cfg.lex.last_name_particles) or [None]
    middle = name.middle_token or None
    hits: list[str] = []
    for pid, tpl in cfg.patterns.items():
        if uses_last(tpl) and lasts == [None]:
            continue
        for fv in firsts:
            if not fv:
                continue
            for lv in lasts:
                if render(tpl, fv, lv, middle) == local and pid not in hits:
                    hits.append(pid)
    return hits


def shape_patterns(local: str, cfg: Config) -> list[tuple[str, str]]:
    """Guess a pattern from an address alone, using name lists. Weak evidence."""
    l = local.lower()
    firsts, lasts = cfg.first_names, cfg.last_names
    if l in firsts and l not in cfg.lex.role_inboxes:
        return [("first", f"'{l}' is a first name")]
    m = re.fullmatch(r"([a-z]+)([._-])([a-z]+)", l)
    if m:
        a, sep, b = m.groups()
        if len(a) == 1 and b in lasts:
            return [("f.last" if sep == "." else ("first_last" if sep == "_" else "first-last"),
                     f"'{a}' looks like an initial and '{b}' a last name")]
        if a in firsts and b in lasts:
            pid = {".": "first.last", "_": "first_last", "-": "first-last"}[sep]
            return [(pid, f"'{a}' is a first name and '{b}' a last name")]
        if a in firsts and len(b) == 1 and sep == ".":
            return [("first.l", f"'{a}' is a first name followed by an initial")]
        return []
    if re.fullmatch(r"[a-z]+", l) and len(l) >= 4:
        out: list[tuple[str, str]] = []
        if l[1:] in lasts and l[1:] not in firsts:
            out.append(("flast", f"'{l[0]}' + last name '{l[1:]}'"))
        for i in range(2, len(l) - 1):
            if l[:i] in firsts and l[i:] in lasts and len(l[i:]) >= 3:
                out.append(("firstlast", f"first name '{l[:i]}' + last name '{l[i:]}'"))
                break
        if not out and l[:-1] in firsts:
            out.append(("firstl", f"first name '{l[:-1]}' + an initial"))
        return out
    return []


@dataclass
class PatternEvidence:
    pattern: str
    strength: str               # "proven" | "shape"
    email: str
    person: str | None          # full name the address matched, when proven
    source: str                 # website | serper | scrapio | input | verified
    url: str | None = None
    note: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        who = f" = {self.person}" if self.person else ""
        where = f" ({self.url})" if self.url else ""
        return f"{self.email}{who}{where}"


def evidence_from_address(email: str, people: list[PersonName], cfg: Config, *,
                          source: str, url: str | None = None) -> list[PatternEvidence]:
    """Turn one real address at the domain into pattern evidence."""
    local, _, _ = email.lower().partition("@")
    if not local or local in cfg.lex.role_inboxes:
        return []
    out: list[PatternEvidence] = []
    for person in people:
        for pid in matching_patterns(local, person, cfg):
            out.append(PatternEvidence(pattern=pid, strength="proven", email=email.lower(),
                                       person=person.full, source=source, url=url))
    if out:
        return out
    return [PatternEvidence(pattern=pid, strength="shape", email=email.lower(), person=None,
                            source=source, url=url, note=why)
            for pid, why in shape_patterns(local, cfg)]


def rank_evidence(evidence: list[dict]) -> tuple[list[str], list[str]]:
    """(proven patterns, shape-only patterns), most-supported first."""
    proven: dict[str, int] = {}
    shape: dict[str, int] = {}
    for e in evidence:
        bucket = proven if e.get("strength") in ("proven", "verified") else shape
        bucket[e["pattern"]] = bucket.get(e["pattern"], 0) + 1
    p = sorted(proven, key=lambda k: -proven[k])
    s = [k for k in sorted(shape, key=lambda k: -shape[k]) if k not in proven]
    return p, s
