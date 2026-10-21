"""Two caches.

PageCache (inside the run folder): every page read is saved, raw HTML and
visible text, so re-runs and the AI read never fetch a page twice.

DomainMemo (shared across runs, default .fef-cache/domains.json): what
Million Verifier said about a domain (catch_all / unknown) and which pattern
it confirmed there, kept for 30 days so a re-run doesn't pay twice.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def url_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class PageCache:
    def __init__(self, root: Path | str):
        # Absolute, so the page reader (a subagent) finds the files from anywhere.
        self.root = Path(root).resolve()

    def _paths(self, url: str) -> tuple[Path, Path, Path]:
        k = url_key(url)
        return self.root / f"{k}.html", self.root / f"{k}.txt", self.root / f"{k}.json"

    def get(self, url: str) -> dict | None:
        html_p, txt_p, meta_p = self._paths(url)
        if not meta_p.is_file():
            return None
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        meta["html"] = html_p.read_text(encoding="utf-8") if html_p.is_file() else ""
        meta["text"] = txt_p.read_text(encoding="utf-8") if txt_p.is_file() else ""
        return meta

    def put(self, url: str, *, status: int | None, final_url: str | None, html: str, text: str,
            error: str | None = None, fetched_at: str | None = None) -> dict:
        html_p, txt_p, meta_p = self._paths(url)
        meta = {"url": url, "status": status, "final_url": final_url, "error": error,
                "fetched_at": fetched_at, "key": url_key(url), "text_path": str(txt_p)}
        _atomic_write(html_p, html or "")
        _atomic_write(txt_p, text or "")
        _atomic_write(meta_p, json.dumps(meta))
        return {**meta, "html": html or "", "text": text or ""}

    def text_path(self, url: str) -> Path:
        return self._paths(url)[1]


class DomainMemo:
    SAVE_EVERY_S = 5.0

    def __init__(self, path: Path | str | None, ttl_days: int = 30):
        self.path = Path(path) if path else None
        self.ttl = timedelta(days=ttl_days)
        self._lock = threading.Lock()
        self._dirty = False
        self._last_save = 0.0
        self.data: dict[str, dict] = {}
        if self.path and self.path.is_file():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.data = {}
        if self.path:
            atexit.register(self.flush)

    def _fresh(self, entry: dict) -> bool:
        try:
            when = datetime.fromisoformat(entry["at"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            return False
        return datetime.now(timezone.utc) - when <= self.ttl

    def status(self, domain: str) -> str | None:
        """'catch_all' | 'unknown' | None, if remembered and fresh."""
        entry = self.data.get(domain, {}).get("status")
        return entry["value"] if entry and self._fresh(entry) else None

    def verified_patterns(self, domain: str) -> list[str]:
        entries = self.data.get(domain, {}).get("verified_patterns", [])
        return [e["pattern"] for e in entries if self._fresh(e)]

    def set_status(self, domain: str, value: str) -> None:
        with self._lock:
            self.data.setdefault(domain, {})["status"] = {"value": value, "at": _now()}
            self._save()

    def clear_status(self, domain: str) -> None:
        with self._lock:
            self.data.get(domain, {}).pop("status", None)
            self._save()

    def add_verified_pattern(self, domain: str, pattern: str) -> None:
        with self._lock:
            entries = self.data.setdefault(domain, {}).setdefault("verified_patterns", [])
            entries[:] = [e for e in entries if e.get("pattern") != pattern]
            entries.insert(0, {"pattern": pattern, "at": _now()})
            self._save()

    def _save(self) -> None:
        """Called with the lock held. Writes at most every few seconds; flush() writes now."""
        self._dirty = True
        if time.monotonic() - self._last_save >= self.SAVE_EVERY_S:
            self._write()

    def _write(self) -> None:
        if self.path and self._dirty:
            _atomic_write(self.path, json.dumps(self.data, sort_keys=True))
            self._dirty = False
            self._last_save = time.monotonic()

    def flush(self) -> None:
        with self._lock:
            self._write()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
