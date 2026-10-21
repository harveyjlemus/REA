"""Mail check (lane 2): does the domain accept email at all, and who runs its mail?

System DNS is tried first. Before trusting it, a canary lookup (gmail.com) must
succeed: some locked-down containers answer "no such domain" for everything,
which would otherwise mark every company as dead. If system DNS fails the
canary, DNS-over-HTTPS (dns.google) is used through the normal Transport. If
that host is blocked too, the lane reports `network_blocked` and the row goes
on without it."""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field

from .http import FetchError, NetworkBlocked

PROVIDERS: list[tuple[str, tuple[str, ...]]] = [
    ("google", ("google.com", "googlemail.com")),
    ("microsoft", ("outlook.com", "office365.us", "hotmail.com")),
    ("godaddy", ("secureserver.net",)),
    ("zoho", ("zoho.com", "zoho.eu", "zohomail.com")),
    ("proofpoint", ("pphosted.com", "ppe-hosted.com", "proofpoint.com")),
    ("mimecast", ("mimecast.com", "mimecast.co.za")),
    ("barracuda", ("barracudanetworks.com", "barracuda.com")),
    ("yahoo", ("yahoodns.net",)),
    ("apple", ("icloud.com",)),
    ("rackspace", ("emailsrvr.com",)),
    ("ionos", ("ionos.com", "1and1.com", "perfora.net", "kundenserver.de")),
    ("namecheap", ("privateemail.com", "registrar-servers.com")),
    ("fastmail", ("messagingengine.com",)),
]

# Secure gateways often answer "unknown" or "catch_all" to verifiers.
GATEWAYS = {"proofpoint", "mimecast", "barracuda"}


def provider_for(hosts: list[str]) -> str:
    if not hosts:
        return "none"
    for name, suffixes in PROVIDERS:
        for h in hosts:
            h = h.lower().rstrip(".")
            if any(h == s or h.endswith("." + s) for s in suffixes):
                return name
    return "other"


@dataclass
class MxResult:
    domain: str
    status: str                       # has_mx | no_mx | no_domain | network_blocked | error
    hosts: list[str] = field(default_factory=list)
    provider: str | None = None
    method: str | None = None         # dns | doh
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class DnsUnavailable(Exception):
    pass


class SystemDns:
    method = "dns"

    def __init__(self, lifetime: float = 5.0):
        import dns.resolver  # noqa: F401
        self._resolver_mod = dns.resolver
        self.resolver = dns.resolver.Resolver()
        self.resolver.lifetime = lifetime
        self._checked = False

    def _canary(self) -> None:
        if self._checked:
            return
        try:
            self.resolver.resolve("gmail.com", "MX")
        except Exception as exc:  # NXDOMAIN for gmail.com means the resolver can't be trusted
            raise DnsUnavailable(f"system DNS failed the canary lookup ({type(exc).__name__})") from None
        self._checked = True

    def mx(self, domain: str) -> tuple[str, list[str]]:
        self._canary()
        r = self._resolver_mod
        try:
            answers = self.resolver.resolve(domain, "MX")
            hosts = sorted(((a.preference, str(a.exchange).rstrip(".")) for a in answers))
            names = [h for _, h in hosts if h and h != "."]
            return ("has_mx", names) if names else ("no_mx", [])
        except r.NXDOMAIN:
            return "no_domain", []
        except r.NoAnswer:
            return "no_mx", []
        except (r.NoNameservers, r.LifetimeTimeout) as exc:
            return "error", [type(exc).__name__]


class DohDns:
    method = "doh"
    URL = "https://dns.google/resolve"

    def __init__(self, transport):
        self.transport = transport
        self._checked = False

    def _query(self, domain: str) -> dict:
        resp = self.transport.get(self.URL, params={"name": domain, "type": "MX"}, timeout=(6.0, 10.0))
        if resp.status != 200:
            raise FetchError("other", f"dns.google HTTP {resp.status}")
        return resp.json() or {}

    def mx(self, domain: str) -> tuple[str, list[str]]:
        if not self._checked:
            canary = self._query("gmail.com")
            if canary.get("Status") != 0 or not canary.get("Answer"):
                raise DnsUnavailable("dns.google failed the canary lookup")
            self._checked = True
        data = self._query(domain)
        status = data.get("Status")
        if status == 3:
            return "no_domain", []
        if status != 0:
            return "error", [f"DNS status {status}"]
        hosts = []
        for ans in data.get("Answer") or []:
            if ans.get("type") != 15:
                continue
            parts = str(ans.get("data", "")).split()
            if len(parts) == 2:
                hosts.append((int(parts[0]) if parts[0].isdigit() else 99, parts[1].rstrip(".")))
        names = [h for _, h in sorted(hosts) if h and h != "."]
        return ("has_mx", names) if names else ("no_mx", [])


class MxChecker:
    """Memoized per domain. backend='auto' tries system DNS, then dns.google."""

    def __init__(self, transport=None, *, backend: str = "auto", ledger=None, backends: list | None = None):
        self.transport = transport
        self.ledger = ledger
        self._memo: dict[str, MxResult] = {}
        self._lock = threading.Lock()
        self.blocked_reason: str | None = None
        if backends is not None:
            self._backends = backends
        else:
            self._backends = []
            if backend in ("auto", "dns"):
                try:
                    self._backends.append(SystemDns())
                except ImportError:
                    pass
            if backend in ("auto", "doh") and transport is not None:
                self._backends.append(DohDns(transport))

    def check(self, domain: str, row_id: str | None = None) -> MxResult:
        with self._lock:
            if domain in self._memo:
                return self._memo[domain]
        result = self._lookup(domain)
        if self.ledger:
            self.ledger.log(row_id=row_id, provider="dns", action="mx", units=1, usd=0.0,
                            basis="measured", outcome=result.status)
        with self._lock:
            self._memo[domain] = result
        return result

    def _lookup(self, domain: str) -> MxResult:
        if not self._backends:
            return MxResult(domain, "network_blocked", error=self.blocked_reason or "no DNS method available")
        while self._backends:
            backend = self._backends[0]
            try:
                status, hosts = backend.mx(domain)
            except DnsUnavailable as exc:
                self.blocked_reason = str(exc)
                self._backends.pop(0)
                continue
            except NetworkBlocked as exc:
                self.blocked_reason = str(exc)
                self._backends.pop(0)
                continue
            except FetchError as exc:
                return MxResult(domain, "error", method=backend.method, error=str(exc))
            if status == "error":
                return MxResult(domain, "error", method=backend.method, error=", ".join(hosts))
            return MxResult(domain, status, hosts=hosts, provider=provider_for(hosts) if hosts else "none",
                            method=backend.method)
        return MxResult(domain, "network_blocked", error=self.blocked_reason)
