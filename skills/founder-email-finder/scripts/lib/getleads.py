"""GetLeads (lane 4): enrich-from-person for full names, decision-makers for
company-only rows. REST API, Bearer key (glb_live_...).

  POST /api/v1/enrich/from-person            {"items": [...]}, up to 100 per call
       -> {"ok": true, "results": [{first_name, last_name, email_domain, success,
                                    email, profileUrl, data}], ...}
  POST /api/v1/contacts/lookup/decision-makers  {"domain": ..., "limit": n}
       -> contact rows (work_email, first_name, last_name, current_title, ...)
  GET  /api/v1/usage/fair-use                0 credits
"""
from __future__ import annotations

import time

from .http import FetchError, NetworkBlocked

DEFAULT_BASE = "https://app.getleads.io"
BATCH = 100


class GetLeadsError(Exception):
    pass


class GetLeadsAuthError(GetLeadsError):
    pass


def _first_list(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "contacts", "decision_makers", "decisionMakers", "records", "rows", "items", "data"):
            val = data.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                inner = _first_list(val)
                if inner:
                    return inner
    return []


def _pick(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def normalize_contact(row: dict) -> dict:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    merged = {**data, **{k: v for k, v in row.items() if k != "data"}}
    return {
        "first": _pick(merged, "first_name", "firstName"),
        "last": _pick(merged, "last_name", "lastName"),
        "full": _pick(merged, "full_name", "fullName", "name"),
        "title": _pick(merged, "current_title", "title", "job_title", "jobTitle", "headline", "linkedin_headline"),
        "email": (str(_pick(merged, "email", "work_email", "workEmail") or "").strip().lower() or None),
        "email_status": _pick(merged, "email_status", "emailStatus"),
        "linkedin": _pick(merged, "profileUrl", "linkedin_url", "linkedinUrl", "linkedin"),
        "company": _pick(merged, "company_name", "current_employer", "companyName"),
        "domain": _pick(merged, "company_domain", "email_domain", "domain"),
    }


class GetLeadsClient:
    def __init__(self, api_key: str, transport, *, base_url: str | None = None, ledger=None,
                 usd_per_credit: float | None = 0.0, min_interval: float = 0.65, retry_wait: float = 5.0):
        self.key = api_key
        self.t = transport
        self.base = (base_url or DEFAULT_BASE).rstrip("/")
        self.ledger = ledger
        self.usd_per_credit = usd_per_credit
        self.min_interval = min_interval  # stays under 100 requests/minute
        self.retry_wait = retry_wait
        self._last = 0.0

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}

    def _pace(self) -> None:
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _call(self, method: str, path: str, payload=None) -> dict:
        url = self.base + path
        for attempt in range(3):
            self._pace()
            try:
                if method == "GET":
                    resp = self.t.get(url, headers=self._headers())
                else:
                    resp = self.t.post_json(url, payload, headers=self._headers())
            except NetworkBlocked:
                raise
            except FetchError as exc:
                if attempt == 2:
                    raise GetLeadsError(f"GetLeads unreachable: {exc.kind}") from None
                time.sleep(self.retry_wait * (attempt + 1))
                continue
            if resp.status in (401, 403):
                raise GetLeadsAuthError(f"GetLeads rejected the API key (HTTP {resp.status})")
            if resp.status == 429 or resp.status >= 500:
                if attempt == 2:
                    raise GetLeadsError(f"GetLeads HTTP {resp.status}")
                time.sleep(self.retry_wait * (attempt + 1))
                continue
            if resp.status >= 400:
                raise GetLeadsError(f"GetLeads HTTP {resp.status}: {resp.text[:200]}")
            try:
                return resp.json() or {}
            except ValueError:
                raise GetLeadsError("GetLeads returned something that is not JSON") from None
        raise GetLeadsError("GetLeads: out of retries")

    def _log(self, row_id, action: str, units: float, outcome: str) -> None:
        if not self.ledger:
            return
        if self.usd_per_credit is None:
            self.ledger.log(row_id=row_id, provider="getleads", action=action, units=units,
                            basis="unknown", outcome=outcome)
        else:
            self.ledger.log(row_id=row_id, provider="getleads", action=action, units=units,
                            usd=round(units * self.usd_per_credit, 6), basis="imputed", outcome=outcome)

    def fair_use(self) -> dict | None:
        try:
            return self._call("GET", "/api/v1/usage/fair-use")
        except (GetLeadsError, NetworkBlocked):
            return None

    def enrich_from_person(self, items: list[dict], row_ids: list[str]) -> list[dict]:
        """items: {first_name, last_name, email_domain?, company_name?}. Results come
        back in the same order (matched by echo when the API reorders)."""
        out: list[dict] = []
        for start in range(0, len(items), BATCH):
            chunk = items[start:start + BATCH]
            ids = row_ids[start:start + BATCH]
            data = self._call("POST", "/api/v1/enrich/from-person", {"items": chunk})
            results = _first_list(data)
            aligned = self._align(chunk, results)
            for rid, res in zip(ids, aligned):
                ok = bool(res and res.get("success"))
                # One credit per row where success is true (GetLeads' own billing note).
                self._log(rid, "enrich_from_person", 1 if ok else 0, "match" if ok else "no_match")
                out.append(normalize_contact(res) | {"success": ok} if res else {"success": False})
        return out

    @staticmethod
    def _align(chunk: list[dict], results: list) -> list[dict | None]:
        if len(results) == len(chunk):
            return results
        by_key = {}
        for r in results:
            if isinstance(r, dict):
                k = (str(r.get("first_name", "")).lower(), str(r.get("last_name", "")).lower(),
                     str(r.get("email_domain") or r.get("company_name") or "").lower())
                by_key[k] = r
        return [by_key.get((i["first_name"].lower(), i["last_name"].lower(),
                            str(i.get("email_domain") or i.get("company_name") or "").lower())) for i in chunk]

    def decision_makers(self, domain: str, limit: int, row_id: str | None = None) -> list[dict]:
        data = self._call("POST", "/api/v1/contacts/lookup/decision-makers", {"domain": domain, "limit": limit})
        rows = [normalize_contact(r) for r in _first_list(data) if isinstance(r, dict)]
        self._log(row_id, "decision_makers", len(rows), f"{len(rows)} records")
        return rows
