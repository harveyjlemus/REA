"""All network access goes through a Transport, so tests and the demo can swap
in a fake one and nothing ever leaves the machine.

The real Transport tells a *blocked network* apart from a *dead website*. In a
locked-down cloud container every outside host can look dead: the proxy
answers "403 Forbidden" to a blocked host, and the container's own DNS may say
"no such domain" for everything. Those are reported as `network_blocked`, so a
blocked environment never marks a real company as out of business."""
from __future__ import annotations

import json as _json
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

DEFAULT_UA = "founder-email-finder/1.0 (+contact: set FEF_USER_AGENT)"


@dataclass
class Response:
    status: int
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""

    def json(self):
        return _json.loads(self.text or "null")


class FetchError(Exception):
    """kind: dns | connect | timeout | ssl | too_large | blocked | other"""

    def __init__(self, kind: str, message: str, url: str = ""):
        super().__init__(message)
        self.kind = kind
        self.url = url


class NetworkBlocked(FetchError):
    def __init__(self, host: str, message: str = ""):
        super().__init__("blocked", message or f"{host} is blocked by this environment's network settings", host)
        self.host = host


_SECRET_PARAM = re.compile(r"((?:api|key|apikey|api_key|token|access_token)=)[^&\s'\")]+", re.I)


def redact(text: str) -> str:
    """Mask API keys that ride in query strings (Million Verifier's does)."""
    return _SECRET_PARAM.sub(r"\1***", text or "")


def _probe_dns() -> bool:
    """Can this machine resolve names itself? (A cloud container behind an
    agent proxy usually can't, and some answer NXDOMAIN for everything.)"""
    for host in ("pypi.org", "google.com"):
        try:
            socket.getaddrinfo(host, 443)
            return True
        except OSError:
            continue
    return False


class Transport:
    def __init__(self, user_agent: str | None = None, *, timeout: tuple[float, float] = (8.0, 20.0),
                 max_bytes: int = 2_000_000, min_interval_per_host: float = 0.0):
        import requests  # imported here so the offline tests never need it at import time
        self._requests = requests
        self._local = threading.local()   # one Session per thread: requests.Session isn't thread-safe
        self.user_agent = user_agent or DEFAULT_UA
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.min_interval = min_interval_per_host
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._dns: bool | None = None

    @property
    def session(self):
        s = getattr(self._local, "session", None)
        if s is None:
            s = self._local.session = self._requests.Session()
        return s

    @property
    def local_dns(self) -> bool:
        if self._dns is None:
            self._dns = _probe_dns()
        return self._dns

    def _pace(self, host: str) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            wait = self._last.get(host, 0.0) + self.min_interval - time.monotonic()
            self._last[host] = time.monotonic() + max(wait, 0.0)
        if wait > 0:
            time.sleep(wait)

    def _translate(self, exc: Exception, url: str) -> FetchError:
        req = self._requests.exceptions
        host = urlsplit(url).hostname or url
        msg = redact(str(exc))
        url = redact(url)
        if isinstance(exc, req.ProxyError) and ("403" in msg or "Tunnel connection failed" in msg):
            return NetworkBlocked(host, f"{host}: blocked by the network proxy (403)")
        if isinstance(exc, req.SSLError):
            return FetchError("ssl", msg[:300], url)
        if isinstance(exc, (req.ConnectTimeout,)):
            return FetchError("timeout", "connect timeout", url)
        if isinstance(exc, req.ReadTimeout):
            return FetchError("timeout", "read timeout", url)
        if isinstance(exc, req.ConnectionError):
            if "NameResolutionError" in msg or "Name or service not known" in msg or "getaddrinfo" in msg \
                    or "nodename nor servname" in msg:
                if not self.local_dns:
                    return NetworkBlocked(host, f"{host}: this machine cannot resolve names (no DNS)")
                return FetchError("dns", "domain does not resolve", url)
            return FetchError("connect", msg[:300], url)
        return FetchError("other", msg[:300], url)

    def get(self, url: str, *, headers: dict | None = None, params: dict | None = None,
            timeout: tuple[float, float] | None = None, max_bytes: int | None = None) -> Response:
        host = urlsplit(url).hostname or ""
        self._pace(host)
        hdrs = {"User-Agent": self.user_agent, "Accept-Language": "en-US,en;q=0.8"}
        hdrs.update(headers or {})
        limit = max_bytes or self.max_bytes
        try:
            with self.session.get(url, headers=hdrs, params=params, timeout=timeout or self.timeout,
                                  stream=True, allow_redirects=True) as r:
                chunks, size = [], 0
                for chunk in r.iter_content(65536):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > limit:
                        break
                body = b"".join(chunks)[:limit]
                enc = r.encoding or r.apparent_encoding or "utf-8"
                try:
                    text = body.decode(enc, errors="replace")
                except LookupError:
                    text = body.decode("utf-8", errors="replace")
                return Response(status=r.status_code, url=r.url,
                                headers={k.lower(): v for k, v in r.headers.items()}, text=text)
        except self._requests.exceptions.RequestException as exc:
            raise self._translate(exc, url) from None

    def post_json(self, url: str, payload, *, headers: dict | None = None,
                  timeout: tuple[float, float] | None = None) -> Response:
        hdrs = {"User-Agent": self.user_agent, "Content-Type": "application/json", "Accept": "application/json"}
        hdrs.update(headers or {})
        try:
            r = self.session.post(url, data=_json.dumps(payload), headers=hdrs, timeout=timeout or self.timeout)
            return Response(status=r.status_code, url=r.url,
                            headers={k.lower(): v for k, v in r.headers.items()}, text=r.text)
        except self._requests.exceptions.RequestException as exc:
            raise self._translate(exc, url) from None


Handler = Callable[[str, str, dict], Response]


class FakeTransport:
    """Routes by URL prefix to canned responses or functions. Anything not
    routed fails like a domain that does not exist."""

    def __init__(self, routes: dict[str, object] | None = None, *, unrouted: str = "dns"):
        self.routes: dict[str, object] = dict(routes or {})
        self.calls: list[tuple[str, str, object]] = []
        self.unrouted = unrouted
        self.local_dns = True

    def add(self, prefix: str, response) -> None:
        self.routes[prefix] = response

    def _route(self, method: str, url: str, payload) -> Response:
        self.calls.append((method, url, payload))
        best = None
        for prefix in self.routes:
            if url.startswith(prefix) and (best is None or len(prefix) > len(best)):
                best = prefix
        if best is None:
            host = urlsplit(url).hostname or url
            if self.unrouted == "blocked":
                raise NetworkBlocked(host)
            raise FetchError("dns", "domain does not resolve", url)
        target = self.routes[best]
        if isinstance(target, Exception):
            raise target
        if callable(target):
            return target(method, url, payload)
        if isinstance(target, Response):
            return Response(status=target.status, url=target.url or url, headers=dict(target.headers), text=target.text)
        if isinstance(target, (dict, list)):
            return Response(status=200, url=url, headers={"content-type": "application/json"}, text=_json.dumps(target))
        return Response(status=200, url=url, headers={"content-type": "text/html"}, text=str(target))

    def get(self, url: str, *, headers: dict | None = None, params: dict | None = None, timeout=None,
            max_bytes: int | None = None) -> Response:
        if params:
            from urllib.parse import urlencode
            url = url + ("&" if "?" in url else "?") + urlencode(params)
        return self._route("GET", url, None)

    def post_json(self, url: str, payload, *, headers: dict | None = None, timeout=None) -> Response:
        return self._route("POST", url, payload)
