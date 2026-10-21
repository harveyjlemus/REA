"""Company domains: clean a website or email into the domain emails live at,
and tell when there is no domain of the company's own to guess at."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .config import Lexicons

EMAIL_RE = re.compile(r"^[a-z0-9][a-z0-9._%+'-]{0,63}@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


def clean_host(value: str | None) -> str | None:
    """'https://www.Acme.com/about?utm=x' -> 'acme.com'. None if it isn't a host."""
    if not value:
        return None
    v = str(value).strip().strip("<>\"' ").lower()
    if not v or " " in v:
        return None
    if "@" in v and "/" not in v:
        v = v.split("@", 1)[1]
    if "://" not in v:
        v = "http://" + v
    try:
        host = urlsplit(v).hostname
    except ValueError:
        return None
    if not host or "." not in host:
        return None
    host = host.rstrip(".")
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    if host.startswith("www."):
        host = host[4:]
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z0-9-]{2,}", host):
        return None
    return host


def registrable(host: str, lex: Lexicons) -> str:
    """The part a company registers: 'quench.culligan.com' -> 'culligan.com'."""
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in lex.multi_part_suffixes:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _matches(host: str, domains: set[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_non_own(host: str, lex: Lexicons) -> bool:
    return _matches(host, lex.non_own_domains)


def is_freemail(domain: str | None, lex: Lexicons) -> bool:
    return bool(domain) and _matches(domain, lex.freemail_domains)


def valid_email(email: str | None) -> bool:
    return bool(email) and bool(EMAIL_RE.match(email.strip().lower()))


def split_email(email: str) -> tuple[str, str]:
    local, _, domain = email.strip().lower().rpartition("@")
    return local, domain


@dataclass
class DomainInfo:
    domain: str | None              # registrable domain emails are guessed at
    host: str | None                # host the website lives on
    source: str | None              # "website" | "email"
    reason: str | None = None       # why there is no domain, when domain is None
    flags: list[str] = field(default_factory=list)


def domain_for_row(website: str | None, email: str | None, lex: Lexicons) -> DomainInfo:
    """Pick the company's own domain from the website, else from a work email."""
    raw_site = (website or "").strip()
    host = clean_host(raw_site) if raw_site else None
    flags: list[str] = []
    if host and is_non_own(host, lex):
        host_reason = "website_is_social_or_directory"
        host = None
    else:
        host_reason = None
    if host:
        reg = registrable(host, lex)
        path = urlsplit(raw_site if "://" in raw_site else "http://" + raw_site).path or "/"
        if host != reg or path.strip("/") not in ("", "index.html", "index.php", "home"):
            # A location page on a bigger company's site (franchise, chain):
            # emails at that domain usually belong to head office.
            flags.append("shared_domain_hint")
        return DomainInfo(domain=reg, host=host, source="website", flags=flags)
    if email and valid_email(email):
        _, edomain = split_email(email)
        if not is_freemail(edomain, lex) and not is_non_own(edomain, lex):
            return DomainInfo(domain=registrable(edomain, lex), host=edomain, source="email", flags=flags)
    reason = host_reason or ("no_website" if not raw_site else "website_unreadable")
    return DomainInfo(domain=None, host=None, source=None, reason=reason, flags=flags)
