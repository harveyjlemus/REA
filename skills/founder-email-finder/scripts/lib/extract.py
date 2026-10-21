"""Pull emails, people and their roles out of a web page with plain code.

Every person comes back with the verbatim sentence it was read from (`quote`),
the page it was on, and how it was found. Names are checked against the US
Census first/last name lists and a stopword list, so "Call Today" or
"Bumble Roofing" never become a person."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from .config import Config
from .names import PersonName, email_token, parse_full_name

# ---------------------------------------------------------------- emails
EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9][A-Za-z0-9._%+-]{0,63})@"
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24})(?![A-Za-z0-9-])")
OBFUSCATED_RE = re.compile(
    r"\b([A-Za-z0-9][A-Za-z0-9._-]{0,63})\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|\s+at\s+)\s*"
    r"([A-Za-z0-9-]+(?:\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|\s+dot\s+|\.)\s*[A-Za-z0-9-]+)+)", re.I)
CFEMAIL_ATTR_RE = re.compile(r'data-cfemail="([0-9a-fA-F]{6,})"')
CFEMAIL_HREF_RE = re.compile(r"/cdn-cgi/l/email-protection#([0-9a-fA-F]{6,})")
MAILTO_RE = re.compile(r'mailto:([^"\'?>\s]+)', re.I)

BAD_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "ico", "bmp", "tif", "tiff", "mp4",
            "woff", "woff2", "ttf", "eot", "map", "json", "php", "html", "htm", "asp", "aspx"}
PLACEHOLDER_LOCALS = {"example", "email", "name", "your", "yourname", "youremail", "user", "username",
                      "test", "someone", "john.doe", "jane.doe", "johndoe", "janedoe", "first.last",
                      "firstname.lastname", "firstname", "lastname", "me", "you", "address", "mail",
                      "your.name", "your-email", "email.address"}
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "domain.com", "email.com",
                       "yourdomain.com", "company.com", "mysite.com", "yoursite.com", "website.com",
                       "sentry.io", "wixpress.com", "sentry.wixpress.com", "sentry-next.wixpress.com",
                       "test.com", "address.com", "yourcompany.com", "domain.tld"}


@dataclass
class FoundEmail:
    email: str
    method: str           # mailto | text | cfemail | jsonld | deobfuscated
    url: str | None
    context: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def decode_cfemail(hexstr: str) -> str | None:
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return None


def _clean_email(raw: str) -> str | None:
    e = raw.strip().strip(".,;:!?)(<>[]{}\"'").lower()
    if e.startswith("mailto:"):
        e = e[7:]
    e = e.split("?")[0]
    m = EMAIL_RE.fullmatch(e)
    if not m:
        return None
    local, domain = m.group(1), m.group(2)
    if domain.rsplit(".", 1)[-1] in BAD_TLDS or domain in PLACEHOLDER_DOMAINS:
        return None
    if local in PLACEHOLDER_LOCALS or re.fullmatch(r"[0-9a-f]{20,}", local):
        return None
    if ".." in local or local.endswith("."):
        return None
    return f"{local}@{domain}"


def find_emails(html: str, text: str, url: str | None, *, domain: str | None = None) -> list[FoundEmail]:
    found: dict[str, FoundEmail] = {}

    def add(raw: str | None, method: str, context: str = "") -> None:
        if not raw:
            return
        e = _clean_email(raw)
        if e and e not in found:
            found[e] = FoundEmail(email=e, method=method, url=url, context=context.strip()[:160])

    for m in MAILTO_RE.finditer(html or ""):
        add(m.group(1).replace("%40", "@"), "mailto")
    for rx in (CFEMAIL_ATTR_RE, CFEMAIL_HREF_RE):
        for m in rx.finditer(html or ""):
            add(decode_cfemail(m.group(1)), "cfemail")
    for m in EMAIL_RE.finditer(text or ""):
        add(m.group(0), "text", (text or "")[max(0, m.start() - 70):m.end() + 70])
    # Raw HTML too: addresses in attributes or scripts never reach the visible text.
    for m in EMAIL_RE.finditer(html or ""):
        add(m.group(0), "text")
    if domain:
        for m in OBFUSCATED_RE.finditer(text or ""):
            dom = re.sub(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|\s+dot\s+)\s*", ".", m.group(2), flags=re.I)
            dom = re.sub(r"\s+", "", dom).lower()
            if dom == domain or dom.endswith("." + domain):
                add(f"{m.group(1)}@{dom}", "deobfuscated", m.group(0))
    return list(found.values())


# ---------------------------------------------------------------- page text
def html_to_text(html: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "template", "iframe", "head"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def jsonld_blocks(html: str) -> list[tuple[dict, str]]:
    """(object, raw JSON text) for every JSON-LD object on the page, @graph flattened."""
    from bs4 import BeautifulSoup
    out: list[tuple[dict, str]] = []
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw.strip().rstrip(";"))
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            obj = stack.pop(0)
            if isinstance(obj, dict):
                out.append((obj, raw))
                graph = obj.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
            elif isinstance(obj, list):
                stack.extend(obj)
    return out


# ---------------------------------------------------------------- people
_TOK = (r"(?:[A-Z][a-z]{1,20}(?:[-'’][A-Z]?[a-z]{1,20})?|Mc[A-Z][a-z]{1,20}|Mac[A-Z][a-z]{2,20}"
        r"|O['’][A-Z][a-z]{1,20}|D['’][A-Z][a-z]{1,20})")
_PART = r"(?:van|von|de|del|della|der|den|da|di|du|la|le|Van|Von|De|Del|Della|Der|Den|Da|Di|Du|La|Le|St\.)"
NAME = rf"(?P<name>{_TOK}(?:[ \t]+[A-Z]\.)?(?:[ \t]+{_PART})*[ \t]+{_TOK}(?:[ \t]+{_TOK})?)"
NAME1 = rf"(?P<name>{_TOK})"
_TITLE_WORDS = (r"(?:co-?\s?founder|founding\s+partner|founder|co-?\s?owner|owner(?:\s*/\s*operator)?|president"
                r"|ceo|chief\s+executive(?:\s+officer)?|principal|proprietor"
                r"|managing\s+(?:partner|member|director)|partner|general\s+manager)")
TITLE = rf"(?P<title>(?i:{_TITLE_WORDS}(?:[ \t]*(?:&|and|/|,)[ \t]*{_TITLE_WORDS}){{0,2}}))"
_SEP = r"[ \t]*(?:,|–|—|-|\||:|\()[ \t]*"
# Any job title at all, to stop a founder title above a name from pairing with
# someone who has a title of their own on the next line.
_JOB_WORDS = (r"(?i:\b(?:manager|director|coordinator|specialist|assistant|technician|officer|estimator"
              r"|supervisor|administrator|associate|lead|foreman|accountant|bookkeeper|engineer|designer"
              r"|consultant|representative|advisor|analyst|installer|apprentice|receptionist|vp|head)\b)")

# (regex, method, strength, implied title when the pattern carries none)
PATTERNS: list[tuple[re.Pattern, str, str, str | None]] = [
    (re.compile(rf"{NAME}{_SEP}(?i:the[ \t]+|our[ \t]+)?{TITLE}\b"), "name_title", "strong", None),
    # Team cards: the name alone on a line, the title alone on the next.
    (re.compile(rf"^[ \t]*{NAME}[ \t]*\n[ \t]*{TITLE}[ \t]*$", re.M), "name_newline_title", "strong", None),
    (re.compile(rf"\b{TITLE}[ \t]*(?::|,|–|—|-)[ \t]*{NAME}"), "title_name", "strong", None),
    # Title line above a name line, unless that name has its own title below it
    # (then this title belongs to the person above).
    (re.compile(rf"^[ \t]*{TITLE}[ \t]*\n[ \t]*{NAME}[ \t]*$(?!\n[ \t]*(?i:{_TITLE_WORDS}|[^\n]{{0,40}}{_JOB_WORDS}))", re.M),
     "title_newline_name", "strong", None),
    (re.compile(rf"\b(?i:our|the)[ \t]+{TITLE}[ \t]+{NAME}"), "our_title_name", "strong", None),
    (re.compile(rf"(?i:founded|started|established|opened|launched)(?:[ \t]+(?i:in)[ \t]+\d{{4}})?[ \t]+(?i:by)[ \t]+{NAME}"),
     "founded_by", "strong", "Founder"),
    (re.compile(rf"(?i:owned[ \t]+(?:and|&)[ \t]+operated|family[ \t]+owned|owned)[ \t]+(?i:by)[ \t]+{NAME}"),
     "owned_by", "strong", "Owner"),
    (re.compile(rf"{NAME}[ \t]+(?i:founded|started|established|launched|owns|bought|purchased|took[ \t]+over)\b"),
     "name_founded", "medium", "Founder"),
    (re.compile(rf"{NAME}[ \t]+{TITLE}\b"), "name_space_title", "weak", None),
    # Staff with any job title. Never picked as the founder, but their addresses
    # prove the company's pattern ("jprice@" next to "Jonah Price, Estimator").
    (re.compile(rf"^[ \t]*{NAME}[ \t]*\n[ \t]*(?P<title>[^\n]{{0,40}}{_JOB_WORDS}[^\n]{{0,30}})[ \t]*$", re.M),
     "name_newline_job", "strong", None),
    (re.compile(rf"{NAME}[ \t]*(?:,|–|—|-|\|)[ \t]*(?P<title>[A-Za-z &/]{{0,30}}{_JOB_WORDS}[A-Za-z &/]{{0,20}})"),
     "name_job", "strong", None),
    # One-word names: only in tight, explicit forms, and only Census first names.
    (re.compile(rf"(?i:i'm|i am|my name is)[ \t]+{NAME1}[ \t]*,?[ \t]*(?i:the[ \t]+|and[ \t]+i'm[ \t]+the[ \t]+)?{TITLE}\b"),
     "im_name_title", "medium", None),
    (re.compile(rf"\b{NAME1}{_SEP}(?i:the[ \t]+|our[ \t]+)?{TITLE}\b(?![ \t]+[A-Z])"), "first_title", "weak1", None),
    (re.compile(rf"\b{TITLE}[ \t]*:[ \t]*{NAME1}\b(?![ \t]+[A-Z])"), "title_first", "weak1", None),
]
MEET_RE = re.compile(rf"(?i:meet)[ \t]+{NAME}")


@dataclass
class FoundPerson:
    first: str
    last: str | None
    middle: str | None
    full: str
    title: str | None
    rank: int | None
    method: str
    strength: str
    url: str | None
    quote: str
    source: str = "website"

    def to_dict(self) -> dict:
        return asdict(self)

    def name(self) -> PersonName:
        return PersonName(first=self.first, last=self.last, middle=self.middle)


def _quote(text: str, start: int, end: int, limit: int = 220) -> str:
    """The line(s) holding the match, verbatim, trimmed to `limit` chars."""
    ls = text.rfind("\n", 0, start)
    ls = 0 if ls < 0 else ls + 1
    le = text.find("\n", end)
    le = len(text) if le < 0 else le
    if end - start < limit and le - ls > limit:
        pad = (limit - (end - start)) // 2
        ls, le = max(ls, start - pad), min(le, end + pad)
    return text[ls:le].strip()


def _company_tokens(company: str | None) -> set[str]:
    return {email_token(t) for t in re.split(r"[\s&,.\-/]+", company or "") if email_token(t)}


def check_name(raw: str, cfg: Config, *, company: str | None, strength: str) -> PersonName | None:
    """A PersonName if `raw` reads as a real person's name, else None."""
    stop = cfg.lex.name_stopwords
    particles = cfg.lex.last_name_particles
    tokens = raw.split()
    while tokens and email_token(tokens[-1]) in stop:
        tokens.pop()
    while tokens and email_token(tokens[0]) in stop:
        tokens.pop(0)
    if not tokens:
        return None
    for t in tokens:
        tt = email_token(t)
        if not tt or (tt in stop and tt not in particles):
            return None
    comp = _company_tokens(company)
    toks = [email_token(t) for t in tokens if email_token(t) not in particles]
    if comp and all(t in comp for t in toks):
        return None  # "Rivera Roofing" is the company, not a person
    first = toks[0]
    if len(tokens) == 1:
        if strength not in ("weak1", "medium") or first not in cfg.first_names or len(first) < 3:
            return None
        return PersonName(first=tokens[0])
    first_ok = first in cfg.first_names
    last_ok = toks[-1] in cfg.last_names
    if strength == "weak" and not (first_ok and last_ok):
        return None
    if strength in ("strong", "medium") and not (first_ok or last_ok):
        return None
    if not first_ok and comp and first in comp:
        return None
    return parse_full_name(" ".join(tokens), cfg)


def _norm_title(title: str | None) -> str | None:
    if not title:
        return None
    t = " ".join(title.split()).strip(" ,&/")
    return t[:1].upper() + t[1:] if t else None


def _titlecase_caps(text: str) -> str:
    """'JOHN SMITH, OWNER' -> 'John Smith, Owner' (same length, so spans line up)."""
    return re.sub(r"\b[A-Z][A-Z'’\-]{1,}\b", lambda m: m.group(0)[0] + m.group(0)[1:].lower(), text)


def find_people(text: str, html: str, url: str | None, *, company: str | None, cfg: Config) -> list[FoundPerson]:
    people: dict[str, FoundPerson] = {}
    strength_order = {"strong": 0, "medium": 1, "weak": 2, "weak1": 3}

    def add(name: PersonName, title: str | None, method: str, strength: str, quote: str, source="website") -> None:
        rank = cfg.lex.title_rank(title)
        key = name.key
        cand = FoundPerson(first=name.first, last=name.last, middle=name.middle, full=name.full,
                           title=_norm_title(title), rank=rank, method=method, strength=strength,
                           url=url, quote=quote, source=source)
        old = people.get(key)
        if old is None:
            people[key] = cand
            return
        better_rank = (cand.rank or 99) < (old.rank or 99)
        better_strength = strength_order.get(cand.strength, 9) < strength_order.get(old.strength, 9)
        if better_rank or (cand.rank == old.rank and better_strength):
            people[key] = cand

    work = _titlecase_caps(text or "")
    claimed: list[tuple[int, int]] = []   # title spans already paired with the name above them
    for rx, method, strength, implied in PATTERNS:
        for m in rx.finditer(work):
            span = m.span("title") if "title" in m.groupdict() and m.group("title") else None
            if span and method.startswith("title") and any(s < span[1] and span[0] < e for s, e in claimed):
                continue
            name = check_name(m.group("name"), cfg, company=company, strength=strength)
            if not name:
                continue
            if span and method.startswith("name"):
                claimed.append(span)
            title = m.groupdict().get("title") or implied
            add(name, title, method, "medium" if strength == "weak1" else strength,
                _quote(text, m.start(), m.end()))
    for m in MEET_RE.finditer(work):
        name = check_name(m.group("name"), cfg, company=company, strength="weak")
        if not name:
            continue
        window = work[m.end():m.end() + 160]
        t = re.search(TITLE, window)
        if t:
            add(name, t.group("title"), "meet", "medium", _quote(text, m.start(), m.end() + t.end()))

    for obj, raw in jsonld_blocks(html):
        _people_from_jsonld(obj, raw, add, cfg, company)
    return sorted(people.values(), key=lambda p: ((p.rank or 99), strength_order.get(p.strength, 9)))


def _people_from_jsonld(obj: dict, raw: str, add, cfg: Config, company: str | None) -> None:
    def person(entry, implied_title: str | None):
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            return
        name_raw = entry.get("name") or " ".join(filter(None, [entry.get("givenName"), entry.get("familyName")]))
        if not isinstance(name_raw, str) or not name_raw.strip():
            return
        name = check_name(name_raw.strip(), cfg, company=company, strength="strong")
        if not name:
            return
        title = entry.get("jobTitle") or implied_title
        if isinstance(title, list):
            title = ", ".join(str(t) for t in title)
        i = raw.find(name_raw)
        quote = raw[max(0, i - 60): i + len(name_raw) + 60].strip() if i >= 0 else name_raw
        add(name, title if isinstance(title, str) else implied_title, "jsonld", "strong", " ".join(quote.split()))

    types = obj.get("@type")
    types = types if isinstance(types, list) else [types]
    if "Person" in types:
        person(obj, None)
    for key, implied in (("founder", "Founder"), ("founders", "Founder"), ("employee", None),
                         ("employees", None), ("member", None), ("owner", "Owner")):
        val = obj.get(key)
        for entry in (val if isinstance(val, list) else [val]):
            if entry:
                person(entry, implied)
