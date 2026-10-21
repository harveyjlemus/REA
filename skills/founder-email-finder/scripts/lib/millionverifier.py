"""Million Verifier (lane 7): one address per call, plus the CSV report reader
for the manual round trip.

GET https://api.millionverifier.com/api/v3/?api=KEY&email=E&timeout=T
  -> {email, quality, result, resultcode, subresult, free, role, credits, error, ...}
GET https://api.millionverifier.com/api/v3/credits?api=KEY -> {credits, ...}

result is one of ok, catch_all, unknown, error, disposable, invalid.
A bad key still answers HTTP 200, with the problem in `error`: that stops the run.
The key travels in the URL, so URLs are never logged."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .http import FetchError, NetworkBlocked

BASE = "https://api.millionverifier.com/api/v3/"
RESULTS = ("ok", "catch_all", "unknown", "error", "disposable", "invalid")
CODE_TO_RESULT = {1: "ok", 2: "catch_all", 3: "unknown", 4: "error", 5: "disposable", 6: "invalid"}


class MvError(Exception):
    pass


class MvAuthError(MvError):
    pass


class MvNoCredits(MvError):
    pass


@dataclass
class MvResult:
    email: str
    result: str
    quality: str | None = None
    subresult: str | None = None
    free: bool | None = None
    role: bool | None = None
    credits_left: int | None = None
    source: str = "api"          # api | report

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_result(value, code=None) -> str:
    v = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if v in ("catchall", "accept_all", "accept-all"):
        v = "catch_all"
    if v in RESULTS:
        return v
    try:
        return CODE_TO_RESULT.get(int(code), "unknown")
    except (TypeError, ValueError):
        return "unknown"


def _bool(v) -> bool | None:
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    return True if s in ("true", "yes", "1") else (False if s in ("false", "no", "0") else None)


class MillionVerifierClient:
    def __init__(self, api_key: str, transport, *, ledger=None, usd_per_check: float | None = None,
                 retry_waits: tuple[float, ...] = (2.0, 5.0)):
        self.key = api_key
        self.t = transport
        self.ledger = ledger
        self.usd_per_check = usd_per_check
        self.retry_waits = retry_waits

    def credits(self) -> int | None:
        try:
            resp = self.t.get(BASE + "credits", params={"api": self.key}, timeout=(6.0, 15.0))
        except NetworkBlocked:
            raise
        except FetchError:
            return None
        data = resp.json() if resp.status == 200 else {}
        if data.get("error"):
            self._raise_for(str(data["error"]))
        try:
            return int(data.get("credits"))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _raise_for(error: str):
        low = error.lower()
        if "apikey" in low or "api key" in low or "not found" in low or "invalid key" in low:
            raise MvAuthError(f"Million Verifier rejected the API key ({error})")
        if "credit" in low:
            raise MvNoCredits(f"Million Verifier is out of credits ({error})")
        raise MvError(error)

    def verify(self, email: str, *, timeout_s: int = 20, row_id: str | None = None) -> MvResult:
        attempts = len(self.retry_waits) + 1
        for attempt in range(attempts):
            try:
                resp = self.t.get(BASE, params={"api": self.key, "email": email, "timeout": timeout_s},
                                  timeout=(6.0, timeout_s + 15.0))
            except NetworkBlocked:
                raise
            except FetchError as exc:
                if attempt == attempts - 1:
                    self._log(row_id, 0, "error", f"unreachable: {exc.kind}")
                    return MvResult(email=email, result="error", subresult=f"unreachable: {exc.kind}")
                time.sleep(self.retry_waits[attempt])
                continue
            data = resp.json() if resp.status == 200 else {}
            if resp.status != 200:
                if attempt == attempts - 1:
                    return MvResult(email=email, result="error", subresult=f"HTTP {resp.status}")
                time.sleep(self.retry_waits[attempt])
                continue
            if data.get("error"):
                self._raise_for(str(data["error"]))
            result = normalize_result(data.get("result"), data.get("resultcode"))
            if result == "error" and attempt < attempts - 1:
                time.sleep(self.retry_waits[attempt])
                continue
            out = MvResult(email=email, result=result, quality=data.get("quality"),
                           subresult=data.get("subresult"), free=_bool(data.get("free")),
                           role=_bool(data.get("role")),
                           credits_left=data.get("credits") if isinstance(data.get("credits"), int) else None)
            self._log(row_id, 1, result, None)
            return out
        return MvResult(email=email, result="error")

    def _log(self, row_id, units: int, outcome: str, detail: str | None) -> None:
        if not self.ledger:
            return
        if self.usd_per_check is None:
            self.ledger.log(row_id=row_id, provider="millionverifier", action="verify", units=units,
                            basis="unknown", outcome=outcome, detail=detail)
        else:
            self.ledger.log(row_id=row_id, provider="millionverifier", action="verify", units=units,
                            usd=round(units * self.usd_per_check, 6), basis="imputed", outcome=outcome,
                            detail=detail)


def read_report(path: Path | str) -> dict[str, MvResult]:
    """email -> result from a Million Verifier report CSV (full report preferred)."""
    from .columns import norm_header, read_table
    header, rows = read_table(path)
    idx = {norm_header(h): i for i, h in enumerate(header)}
    result_i, quality_i = idx.get("result"), idx.get("quality")
    email_i = next((idx[k] for k in ("email", "e mail", "email address", "emails") if k in idx), None)
    if email_i is None:
        # MV keeps the original columns; take the first column full of addresses.
        for i in range(len(header)):
            sample = [r[i] for r in rows[:20] if i < len(r) and r[i]]
            if sample and all("@" in s for s in sample):
                email_i = i
                break
    if email_i is None or result_i is None:
        raise MvError(f"{Path(path).name}: no email or result column; is this a Million Verifier full report?")
    out: dict[str, MvResult] = {}
    free_i, role_i = idx.get("free"), idx.get("role")
    for r in rows:
        if email_i >= len(r) or not r[email_i].strip():
            continue
        email = r[email_i].strip().lower()
        out[email] = MvResult(email=email, result=normalize_result(r[result_i] if result_i < len(r) else ""),
                              quality=(r[quality_i] if quality_i is not None and quality_i < len(r) else None),
                              free=_bool(r[free_i]) if free_i is not None and free_i < len(r) else None,
                              role=_bool(r[role_i]) if role_i is not None and role_i < len(r) else None,
                              source="report")
    return out
