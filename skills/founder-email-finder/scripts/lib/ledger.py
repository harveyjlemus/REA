"""The cost ledger: one JSON line per call, free or paid.

Dollar cost has an explicit basis:
  measured  - the provider told us (or it is truly $0, like reading a website)
  imputed   - units x the price you set in .env (MV_USD_PER_CHECK, ...)
  unknown   - no price known; usd is null, never silently $0
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

BASES = ("measured", "imputed", "unknown")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ledger:
    def __init__(self, path: Path | str | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, *, row_id: str | None, provider: str, action: str, units: float = 1,
            usd: float | None = None, basis: str = "measured", outcome: str | None = None,
            detail: str | None = None) -> dict:
        if basis not in BASES:
            raise ValueError(f"basis must be one of {BASES}")
        if basis == "unknown":
            usd = None
        elif usd is None:
            raise ValueError("measured/imputed costs need a dollar value; use basis='unknown' instead")
        event = {"ts": utc_now(), "event_id": uuid.uuid4().hex, "row_id": row_id, "provider": provider,
                 "action": action, "units": units, "usd": usd, "basis": basis}
        if outcome:
            event["outcome"] = outcome
        if detail:
            event["detail"] = detail[:200]
        if self.path:
            line = json.dumps(event, sort_keys=True) + "\n"
            with self._lock:
                with self.path.open("a", encoding="utf-8") as fh:
                    if fcntl:
                        fcntl.flock(fh, fcntl.LOCK_EX)
                    fh.write(line)
                    fh.flush()
                    os.fsync(fh.fileno())
                    if fcntl:
                        fcntl.flock(fh, fcntl.LOCK_UN)
        return event

    def events(self) -> list[dict]:
        if not self.path or not self.path.is_file():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out


def summarize(events: list[dict]) -> dict:
    """Totals by provider and by row. Unknown dollars are counted, not added."""
    by_provider: dict[str, dict] = defaultdict(lambda: {"calls": 0, "units": 0.0, "usd": 0.0, "unknown_usd_calls": 0})
    by_row: dict[str, dict] = defaultdict(lambda: {"usd": 0.0, "unknown_usd_calls": 0, "units": defaultdict(float)})
    for e in events:
        p = by_provider[e["provider"]]
        p["calls"] += 1
        p["units"] += float(e.get("units") or 0)
        r = by_row[e.get("row_id") or "_run"]
        r["units"][e["provider"]] += float(e.get("units") or 0)
        if e.get("usd") is None:
            p["unknown_usd_calls"] += 1
            r["unknown_usd_calls"] += 1
        else:
            p["usd"] += float(e["usd"])
            r["usd"] += float(e["usd"])
    return {"by_provider": dict(by_provider), "by_row": {k: {**v, "units": dict(v["units"])} for k, v in by_row.items()}}
