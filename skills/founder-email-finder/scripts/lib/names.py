"""Person names: parse them, clean them, and produce the plain-ASCII pieces an
email address is built from ("O'Brien" -> "obrien", "José" -> "jose")."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .config import Config

_SPECIAL = {"ß": "ss", "ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe",
            "đ": "d", "Đ": "d", "ł": "l", "Ł": "l", "þ": "th", "ð": "d", "ı": "i"}
_QUOTED_NICK = re.compile(r"[\"“”‘’'(]\s*([A-Za-z][A-Za-z\-]+)\s*[\"“”‘’')]")


def ascii_fold(s: str) -> str:
    s = "".join(_SPECIAL.get(ch, ch) for ch in s)
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def email_token(s: str | None) -> str:
    """Lowercase ASCII letters and digits only: what goes into an email."""
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]", "", ascii_fold(s).lower())


def _is_initial(token: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z]\.?", token))


@dataclass(frozen=True)
class PersonName:
    first: str
    last: str | None = None
    middle: str | None = None
    nickname: str | None = None

    @property
    def full(self) -> str:
        return " ".join(p for p in (self.first, self.middle, self.last) if p)

    @property
    def key(self) -> str:
        return f"{email_token(self.first)}|{email_token(self.last)}"

    @property
    def first_token(self) -> str:
        return email_token(self.first)

    @property
    def middle_token(self) -> str:
        return email_token(self.middle)

    def last_variants(self, particles: set[str] | None = None) -> list[str]:
        """Email forms of the last name, most likely first.
        "Van Horn" -> [vanhorn, horn]; "Smith-Jones" -> [smithjones, smith, jones]."""
        if not self.last:
            return []
        out: list[str] = []

        def add(v: str) -> None:
            v = email_token(v)
            if v and v not in out:
                out.append(v)

        add(self.last)
        words = [w for w in re.split(r"[\s\-]+", ascii_fold(self.last)) if w]
        if len(words) > 1:
            particles = particles or set()
            core = [w for w in words if w.lower() not in particles]
            if "-" in self.last:
                for w in words:
                    add(w)
            elif core and len(core) < len(words):
                add("".join(core))
            else:
                add(words[0])
        return out


def clean_name_part(value: str | None) -> str | None:
    """Trim a first or last name field the way it arrives in exports."""
    if value is None:
        return None
    v = " ".join(str(value).replace(" ", " ").split()).strip(" ,.;:")
    if not v or v.lower() in {"n/a", "na", "none", "null", "unknown", "-", "?"}:
        return None
    return v


def parse_full_name(raw: str | None, cfg: Config) -> PersonName | None:
    """'Dr. Robert "Bob" Van Horn Jr., CPA' -> PersonName(Robert, Van Horn, nickname Bob)."""
    if not raw:
        return None
    s = " ".join(str(raw).replace(" ", " ").split())
    nickname = None
    m = _QUOTED_NICK.search(s)
    if m and m.start() > 0:
        nickname = m.group(1)
        s = (s[:m.start()] + " " + s[m.end():]).strip()
    # "Smith, John" -> "John Smith" (but "John Smith, CPA" keeps its order)
    if s.count(",") == 1:
        left, right = (p.strip() for p in s.split(","))
        right_tok = email_token(right)
        if right and " " not in right and right_tok not in cfg.lex.name_suffixes and " " not in left:
            s = f"{right} {left}"
        else:
            s = left
    elif s.count(",") > 1:
        s = s.split(",")[0]
    tokens = [t for t in s.split(" ") if t]
    while tokens and email_token(tokens[0]) in cfg.lex.honorifics:
        tokens = tokens[1:]
    while len(tokens) > 1 and email_token(tokens[-1]) in cfg.lex.name_suffixes:
        tokens = tokens[:-1]
    if not tokens or not email_token(tokens[0]):
        return None
    if len(tokens) == 1:
        return PersonName(first=tokens[0], nickname=nickname)
    first, rest = tokens[0], tokens[1:]
    middle = None
    if len(rest) >= 2 and _is_initial(rest[0]):
        middle, rest = rest[0].rstrip("."), rest[1:]
    if len(rest) >= 2:
        # A particle starts the last name: "Van Horn", "de la Cruz".
        idx = next((i for i, t in enumerate(rest) if email_token(t) in cfg.lex.last_name_particles), None)
        if idx is not None:
            if idx > 0 and middle is None:
                middle = " ".join(rest[:idx])
            rest = rest[idx:]
        elif middle is None:
            # "Mary Ann Smith" -> middle Ann; "Juan Carlos Garcia Lopez" -> last "Garcia Lopez".
            middle, rest = rest[0], rest[1:]
    last = " ".join(rest)
    return PersonName(first=first, last=last or None, middle=middle, nickname=nickname)


def person_from_fields(first: str | None, last: str | None, full: str | None,
                       cfg: Config) -> PersonName | None:
    """Prefer explicit first/last columns; fall back to splitting a full name."""
    first, last = clean_name_part(first), clean_name_part(last)
    if first and " " in first and not last:
        parsed = parse_full_name(first, cfg)
        if parsed:
            return parsed
    if first:
        # "J." or a lone initial is not a usable first name for an email guess.
        if len(email_token(first)) < 2:
            parsed = parse_full_name(full, cfg) if full else None
            return parsed or None
        if last and len(email_token(last)) < 2:
            last = None  # privacy-masked "Camille S."
        return PersonName(first=first.split(" ")[0] if not last else first, last=last)
    return parse_full_name(full, cfg) if full else None


def same_person(a: PersonName, b: PersonName, cfg: Config) -> bool:
    """Same first name (or nickname form) and, when both have one, same last name."""
    fa, fb = a.first_token, b.first_token
    if not fa or not fb:
        return False
    if fa != fb and fb not in cfg.nickname_forms(fa):
        return False
    if a.last and b.last:
        la, lb = set(a.last_variants(cfg.lex.last_name_particles)), set(b.last_variants(cfg.lex.last_name_particles))
        return bool(la & lb)
    return True


def display(s: str | None) -> str:
    """Title-case a name that arrived ALL CAPS or all lowercase; leave mixed case alone."""
    if not s:
        return ""
    if s.isupper() or s.islower():
        return " ".join(w[:1].upper() + w[1:].lower() if w else w for w in s.split(" "))
    return s
