"""Website lane (lane 3), Jordan Crawford's TAM Contact Harvester method:
classify the homepage into one of five buckets, read the About / Team / Contact
pages with plain code, and pull names, roles and every email on the domain.

  email_in_html    an email is already in the page source
  server_rendered  a normal readable page
  js_shell         empty until JavaScript runs
  bot_walled       Cloudflare, a captcha, or robots.txt says no
  dead             doesn't load, error page, parked or for sale
"""
from __future__ import annotations

import re
import urllib.robotparser
from dataclasses import asdict, dataclass, field
from urllib.parse import urldefrag, urljoin, urlsplit

from .cache import PageCache
from .config import Config
from .extract import find_emails, find_people, html_to_text
from .http import FetchError, NetworkBlocked
from .ledger import utc_now

BUCKETS = ("email_in_html", "server_rendered", "js_shell", "bot_walled", "dead")
ROBOTS_AGENT = "founder-email-finder"
SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|zip|docx?|xlsx?|pptx?|mp4|mp3|mov|ico|css|js|xml|txt)$", re.I)
PAGE_KINDS = ("about", "team", "contact")


@dataclass
class SiteResult:
    domain: str
    bucket: str | None
    reason: str | None = None
    pages: list[dict] = field(default_factory=list)      # url, final_url, status, kind, key, chars
    emails: list[dict] = field(default_factory=list)     # FoundEmail dicts
    people: list[dict] = field(default_factory=list)     # FoundPerson dicts
    network_blocked: bool = False
    observed_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def readable(self) -> bool:
        return self.bucket in ("email_in_html", "server_rendered")


def classify(status: int | None, html: str, text: str, error_kind: str | None, cfg: Config) -> tuple[str, str | None]:
    lex = cfg.lex
    if error_kind:
        return "dead", error_kind
    h = (html or "").lower()
    t = (text or "").strip()
    short = len(t) < 1500
    if status in (401, 403, 429, 503) or (short and any(mk in h[:30000] for mk in lex.botwall_markers)):
        marker = next((mk for mk in lex.botwall_markers if mk in h[:30000]), None)
        if marker or status in (403, 429, 503):
            return "bot_walled", marker or f"HTTP {status}"
    if status is not None and status >= 400:
        return "dead", f"HTTP {status}"
    if len(t) < 3000 and any(mk in h or mk in t.lower() for mk in lex.parked_markers):
        return "dead", "parked or for sale"
    if find_emails(html, text, None):
        return "email_in_html", None
    if len(t) < 250 and (h.count("<script") >= 3 or any(mk in h for mk in lex.js_shell_markers)):
        return "js_shell", "page is empty until JavaScript runs"
    if len(t) < 60:
        return "dead", "empty page"
    return "server_rendered", None


def _same_site(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == domain or host.endswith("." + domain)


def pick_subpages(html: str, base_url: str, domain: str, cfg: Config, max_n: int) -> list[tuple[str, str]]:
    """[(url, kind)] for the pages most likely to name the owner, best first."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    scored: dict[str, tuple[int, int, str]] = {}
    groups = cfg.lex.subpage_keywords
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urldefrag(urljoin(base_url, href))[0]
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not _same_site(url, domain) or SKIP_EXT.search(parts.path):
            continue
        path = parts.path.rstrip("/").lower()
        if not path or path in ("/index.html", "/index.php", "/home"):
            continue
        label = f"{path} {a.get_text(' ', strip=True).lower()}"
        for gi, words in enumerate(groups):
            if any(w in label for w in words):
                key = parts._replace(query="", fragment="").geturl().rstrip("/")
                prev = scored.get(key)
                cand = (gi, len(path), PAGE_KINDS[min(gi, len(PAGE_KINDS) - 1)])
                if prev is None or cand < prev:
                    scored[key] = cand
                break
    ranked = sorted(scored.items(), key=lambda kv: (kv[1][0], kv[1][1]))
    out = [(u, info[2]) for u, info in ranked[:max_n]]
    if not out:
        for path in cfg.lex.subpage_fallback_paths[:max_n]:
            kind = "contact" if "contact" in path else ("team" if "team" in path else "about")
            out.append((urljoin(base_url, path), kind))
    return out


class Harvester:
    def __init__(self, transport, cache: PageCache, cfg: Config, *, max_subpages: int = 4,
                 offline: bool = False, ledger=None):
        self.transport = transport
        self.cache = cache
        self.cfg = cfg
        self.max_subpages = max_subpages
        self.offline = offline
        self.ledger = ledger

    # ------------------------------------------------------------ fetching
    def _fetch(self, url: str, row_id: str | None) -> dict:
        """Cached page dict: url, status, final_url, html, text, error, error_kind."""
        hit = self.cache.get(url)
        if hit is not None:
            return hit
        if self.offline:
            return {"url": url, "status": None, "final_url": None, "html": "", "text": "",
                    "error": "offline: not in cache", "error_kind": "offline"}
        try:
            resp = self.transport.get(url)
        except NetworkBlocked:
            raise
        except FetchError as exc:
            self._log(row_id, exc.kind)
            page = self.cache.put(url, status=None, final_url=None, html="", text="", error=f"{exc.kind}: {exc}",
                                  fetched_at=utc_now())
            page["error_kind"] = exc.kind
            return page
        self._log(row_id, str(resp.status))
        ctype = (resp.headers.get("content-type") or "text/html").lower()
        html = resp.text if ("html" in ctype or "xml" in ctype or "text" in ctype) else ""
        text = html_to_text(html) if html else ""
        page = self.cache.put(url, status=resp.status, final_url=resp.url, html=html, text=text, fetched_at=utc_now())
        page["error_kind"] = None
        return page

    def _log(self, row_id: str | None, outcome: str) -> None:
        if self.ledger:
            self.ledger.log(row_id=row_id, provider="website", action="fetch", units=1, usd=0.0,
                            basis="measured", outcome=outcome)

    def _robots(self, base: str, row_id: str | None) -> urllib.robotparser.RobotFileParser | None:
        page = self._fetch(urljoin(base, "/robots.txt"), row_id)
        if page.get("status") != 200 or not page.get("html"):
            return None
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(page["html"].splitlines())
        return rp

    # ------------------------------------------------------------ the lane
    def harvest(self, domain: str, *, company: str | None = None, row_id: str | None = None) -> SiteResult:
        result = SiteResult(domain=domain, bucket=None, observed_at=utc_now())
        candidates = [f"https://{domain}/", f"https://www.{domain}/"]
        if getattr(self.transport, "local_dns", True):
            candidates.append(f"http://{domain}/")
        home = None
        last_error = None
        try:
            for url in candidates:
                page = self._fetch(url, row_id)
                if page.get("status") is not None:
                    home = page
                    break
                last_error = page.get("error_kind") or "error"
                if last_error == "offline":
                    result.bucket, result.reason = None, "offline: site not in cache"
                    return result
            if home is None:
                result.bucket, result.reason = "dead", last_error or "no response"
                return result
            base = home.get("final_url") or home["url"]
            final_host = (urlsplit(base).hostname or "").lower()
            if final_host and not _same_site(base, domain):
                # Redirected to another company's site: read it, but say so.
                result.reason = f"redirects to {final_host}"
            robots = self._robots(base, row_id)
            if robots and not robots.can_fetch(ROBOTS_AGENT, base):
                result.bucket, result.reason = "bot_walled", "robots.txt disallows reading this site"
                return result
            bucket, why = classify(home.get("status"), home.get("html", ""), home.get("text", ""), None, self.cfg)
            result.bucket = bucket
            result.reason = result.reason or why
            self._add_page(result, home, "home", domain, company)
            if bucket not in ("email_in_html", "server_rendered"):
                return result
            for url, kind in pick_subpages(home.get("html", ""), base, domain, self.cfg, self.max_subpages):
                if robots and not robots.can_fetch(ROBOTS_AGENT, url):
                    continue
                page = self._fetch(url, row_id)
                if page.get("status") == 200 and page.get("text"):
                    self._add_page(result, page, kind, domain, company)
        except NetworkBlocked as exc:
            result.network_blocked = True
            result.bucket, result.reason = None, str(exc)
        return result

    def _add_page(self, result: SiteResult, page: dict, kind: str, domain: str, company: str | None) -> None:
        url = page.get("final_url") or page["url"]
        result.pages.append({"url": page["url"], "final_url": page.get("final_url"), "status": page.get("status"),
                             "kind": kind, "key": page.get("key"), "chars": len(page.get("text", "")),
                             "text_path": page.get("text_path")})
        seen = {e["email"] for e in result.emails}
        for e in find_emails(page.get("html", ""), page.get("text", ""), url, domain=domain):
            if e.email not in seen:
                result.emails.append(e.to_dict())
                seen.add(e.email)
        known = {p["first"].lower() + "|" + (p.get("last") or "").lower(): i for i, p in enumerate(result.people)}
        for p in find_people(page.get("text", ""), page.get("html", ""), url, company=company, cfg=self.cfg):
            d = p.to_dict()
            d["page_kind"] = kind
            k = p.first.lower() + "|" + (p.last or "").lower()
            if k in known:
                old = result.people[known[k]]
                if (d.get("rank") or 99) < (old.get("rank") or 99):
                    result.people[known[k]] = d
            else:
                known[k] = len(result.people)
                result.people.append(d)
