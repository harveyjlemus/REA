"""Load the project's `.env` into os.environ.

Rules:
  * The `.env` used: $FEF_ROOT/.env if FEF_ROOT is set; else the nearest `.env`
    at or above the current directory; else the one at the repo root this
    skill lives in.
  * A variable already set in the real environment always wins (cloud
    environment secrets arrive that way).
  * Values are never printed or logged; callers read os.environ.
  * FEF_NO_DOTENV=1 skips the file entirely (the tests set it).
"""
from __future__ import annotations

import os
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = SKILL_DIR.parents[1]


def find_env_file(start: Path | None = None) -> Path | None:
    root = os.environ.get("FEF_ROOT")
    if root:
        candidate = Path(root).expanduser() / ".env"
        return candidate if candidate.is_file() else None
    here = (start or Path.cwd()).resolve()
    for parent in (here, *here.parents):
        if (parent / ".env").is_file():
            return parent / ".env"
    candidate = REPO_ROOT / ".env"
    return candidate if candidate.is_file() else None


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            out[key] = value
    return out


def load_dotenv(start: Path | None = None) -> Path | None:
    """Fill os.environ from `.env` without overriding anything already set.
    Returns the file used (or None)."""
    if os.environ.get("FEF_NO_DOTENV") == "1":
        return None
    path = find_env_file(start)
    if not path:
        return None
    for key, value in parse_env_file(path).items():
        if value and key not in os.environ:
            os.environ[key] = value
    return path


def get(key: str, default: str | None = None) -> str | None:
    value = os.environ.get(key)
    return value if value not in (None, "") else default


def get_float(key: str) -> float | None:
    value = get(key)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None
