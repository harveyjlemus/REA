"""Serper (lane 6): Google results for the owner's name and the email pattern.

POST https://google.serper.dev/search, header X-API-KEY, body {"q", "num", "gl", "hl"}.
One credit per search of up to 10 results. Only Google's result titles and
snippets are read; LinkedIn itself is never fetched."""
from __future__ import annotations

import re
import time
from difflib import SequenceMatcher

from .config import Config
from .extract import EMAIL_RE, check_name, find_people
from .http import FetchError, NetworkBlocked
from .names import email_token

URL = "https://google.serper.dev/search"
COMPANY_SUFFIXES = {"llc", "inc", "co", "company", "corp", "corporation", "ltd", "the", "and", "of", "group",
                    "pllc", "lp", "llp", "pc", "pa", "incorporated", "enterprises", "services", "service"}


class SerperError(Exception):
    pass


class SerperAuthError(SerperError):
    pass


class SerperClient:
    def __init__(self, api_key: str, transport, *, ledger=None, usd_per_search: float | None = None,
                 retry_wait: float = 3.0):
        self.key = api_key
        self.t = transport
        self.ledger = ledger
        self.usd_per_search = usd_per_search
        self.retry_wait = retry_wait

    def search(self, q: str, *, num: int = 10, row_id: str | None = None) -> dict:
        body = {"q": q, "num": num, "gl": "us", "hl": "en"}
        credits = 1 if num <= 10 else 2
        for attempt in range(3):
            try:
                resp = self.t.post_json(URL, body, headers={"X-API-KEY": self.key})
            except NetworkBlocked:
                raise
            except FetchError as exc:
                if attempt == 2:
                    raise SerperError(f"Serper unreachable: {exc.kind}") from None
                time.sleep(self.retry_wait)
                continue
            if resp.status in (401, 403):
                raise SerperAuthError(f"Serper rejected the API key (HTTP {resp.status})")
            if resp.status == 429 or resp.status >= 500:
                if attempt == 2:
                    raise SerperError(f"Serper HTTP {resp.status}")
                time.sleep(self.retry_wait * (attempt + 1))
                continue
            if resp.status >= 400:
                raise SerperError(f"Serper HTTP {resp.status}: {resp.text[:200]}")
            self._log(row_id, credits, q)
            return resp.json() or {}
        raise SerperError("Serper: out of retries")

    def _log(self, row_id, credits: int, q: str) -> None:
        if not self.ledger:
            return
        if self.usd_per_search is None:
            self.ledger.log(row_id=row_id, provider="serper", action="search", units=credits, basis="unknown",
                            detail=q)
        else:
            self.ledger.log(row_id=row_id, provider="serper", action="search", units=credits,
                            usd=round(credits * self.usd_per_search, 6), basis="imputed", detail=q)


# ------------------------------------------------------------ queries
def fill_query(template: str, values: dict) -> str | None:
    """{city}/{state} are optional and dropped when empty; anything else missing
    means the query is skipped rather than sent half-filled."""
    missing = False

    def sub(m: re.Match) -> str:
        nonlocal missing
        key = m.group(1)
        v = (values.get(key) or "").strip()
        if not v and key not in ("city", "state"):
            missing = True
        return v.replace('"', "")

    q = re.sub(r"\{(\w+)\}", sub, template)
    return None if missing else " ".join(q.split())


# ------------------------------------------------------------ parsing
def company_core(name: str | None) -> list[str]:
    return [t for t in (email_token(w) for w in re.split(r"[\s&,.\-/'’]+", name or "")) if t and t not in COMPANY_SUFFIXES]


def mentions_company(text: str, company: str | None, domain: str | None) -> bool:
    """Does a result plausibly talk about this company? Domain stem or most of the name."""
    low = (text or "").lower()
    if domain:
        stem = domain.split(".")[0]
        if len(stem) >= 4 and stem in re.sub(r"[^a-z0-9]", "", low):
            return True
    core = company_core(company)
    if not core:
        return False
    flat = email_token(low)
    hits = sum(1 for t in core if t in flat)
    if hits == len(core) or (len(core) >= 3 and hits >= len(core) - 1):
        return True
    joined = "".join(core)
    return SequenceMatcher(None, joined, flat).find_longest_match(0, len(joined), 0, len(flat)).size >= max(6, int(len(joined) * 0.8))


def parse_linkedin_title(title: str) -> tuple[str, str | None, str | None] | None:
    """'Mike Rivera - Owner - Acme Roofing | LinkedIn' -> (name, title, company)."""
    if "linkedin" not in (title or "").lower():
        return None
    t = re.split(r"\s*[|–-]\s*LinkedIn\b.*$", title, flags=re.I)[0]
    parts = [p.strip() for p in re.split(r"\s+[-–—|]\s+", t) if p.strip()]
    if not parts:
        return None
    name = parts[0]
    job = parts[1] if len(parts) >= 3 else None
    company = parts[-1] if len(parts) >= 2 else None
    if len(parts) == 2 and company:
        # "Name - Owner at Acme" or "Name - Acme Roofing"
        m = re.match(r"(.+?)\s+(?:at|@)\s+(.+)", company)
        if m:
            job, company = m.group(1), m.group(2)
    return name, job, company


def people_from_results(data: dict, *, company: str | None, domain: str | None, cfg: Config,
                        want_first: str | None = None) -> list[dict]:
    """Founder-level candidates from Google results, each with the URL and the
    verbatim title/snippet it came from. Results that don't mention the company
    are ignored."""
    out: list[dict] = []
    kg = data.get("knowledgeGraph") or {}
    if kg and mentions_company(f"{kg.get('title', '')} {kg.get('website', '')}", company, domain):
        for key, val in (kg.get("attributes") or {}).items():
            rank = cfg.lex.title_rank(key)
            if rank and isinstance(val, str):
                for raw in re.split(r",\s*|\s+and\s+", val):
                    name = check_name(raw.strip(), cfg, company=company, strength="strong")
                    if name:
                        out.append(_cand(name, key, rank, "knowledge_graph", kg.get("website") or kg.get("title"),
                                         f"{key}: {val}"))
    for res in data.get("organic") or []:
        title, snippet, link = res.get("title") or "", res.get("snippet") or "", res.get("link") or ""
        blob = f"{title} {snippet} {link}"
        li = parse_linkedin_title(title) if "linkedin.com/in/" in link else None
        if li:
            name_raw, job, comp = li
            if not mentions_company(f"{comp or ''} {snippet}", company, domain):
                continue
            name = check_name(name_raw, cfg, company=company, strength="strong")
            if not name:
                continue
            job = job or _title_in(snippet, cfg)
            rank = cfg.lex.title_rank(job)
            if rank is None and want_first is None:
                continue
            out.append(_cand(name, job, rank, "linkedin_result", link, title))
            continue
        if not mentions_company(blob, company, domain):
            continue
        for p in find_people(f"{title}\n{snippet}", "", link, company=company, cfg=cfg):
            out.append(_cand(p.name(), p.title, p.rank, f"serp_{p.method}", link, p.quote))
    if want_first:
        wf = email_token(want_first)
        forms = {wf, *cfg.nickname_forms(wf)}
        out = [c for c in out if email_token(c["first"]) in forms and c.get("last")]
    return sorted(out, key=lambda c: (c.get("rank") or 99))


def _title_in(text: str, cfg: Config) -> str | None:
    for phrase in sorted(cfg.lex.founder_rank, key=len, reverse=True):
        if re.search(rf"\b{re.escape(phrase)}\b", text or "", re.I):
            return phrase.title()
    return None


def _cand(name, title, rank, method, url, quote) -> dict:
    return {"first": name.first, "last": name.last, "middle": name.middle, "full": name.full,
            "title": title, "rank": rank, "method": method, "strength": "strong", "url": url,
            "quote": " ".join((quote or "").split())[:300], "source": "serper"}


def emails_from_results(data: dict, domain: str) -> list[tuple[str, str, str]]:
    """(email, the text it appeared in, url) for addresses at the domain."""
    out = []
    for res in data.get("organic") or []:
        blob = f"{res.get('title') or ''} {res.get('snippet') or ''}"
        for m in EMAIL_RE.finditer(blob):
            e = m.group(0).lower().strip(".")
            if e.endswith("@" + domain) or e.endswith("." + domain):
                out.append((e, " ".join(blob.split())[:300], res.get("link") or ""))
    return out
