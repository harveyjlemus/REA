"""Shared by calibrate.py and backtest.py: read verified rows from your own
exports, and split them into a calibration set and a holdout set the same way
every time (by domain), so the backtest never scores on rows it learned from."""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .columns import detect_mapping, read_table, row_values
from .config import Config
from .domains import clean_host, registrable, split_email, valid_email
from .millionverifier import normalize_result
from .names import PersonName, person_from_fields
from .patterns import matching_patterns

FOUNDER_SENIORITY = {"owner", "founder", "c_suite", "c-suite", "partner", "cxo", "c-level", "president"}


@dataclass
class Labeled:
    name: PersonName
    email: str
    domain: str
    mv_result: str | None
    label: str
    founder_level: bool


def is_holdout(domain: str, share: float) -> bool:
    h = int(hashlib.sha256(domain.encode("utf-8")).hexdigest()[:8], 16) % 1000
    return h < int(share * 1000)


def label_for(path: Path) -> str:
    stem = path.stem.lower()
    for sep in ("_", "-", " ", "("):
        stem = stem.split(sep)[0] if stem.split(sep)[0] else stem
    return stem.strip("[] ") or "file"


def iter_labeled(path: Path, cfg: Config, label: str | None = None):
    """Rows with a person, an email at the company's own domain, and a verdict."""
    header, data = read_table(path)
    m = detect_mapping(header, cfg)
    label = label or label_for(path)
    for values in data:
        inp, _ = row_values(header, values, m)
        email = (inp.get("email") or "").strip().lower()
        if not valid_email(email):
            continue
        host = clean_host(inp.get("website"))
        if not host:
            continue
        domain = registrable(host, cfg.lex)
        if registrable(split_email(email)[1], cfg.lex) != domain:
            continue
        name = person_from_fields(inp.get("first_name"), inp.get("last_name"), inp.get("full_name"), cfg)
        if not name or not name.last:
            continue
        mv = normalize_result(inp["mv_result"]) if inp.get("mv_result") else None
        if mv is None and (inp.get("email_status") or "").lower() not in ("verified", "valid", "ok"):
            continue
        seniority = (inp.get("seniority") or "").strip().lower()
        rank = cfg.lex.title_rank(inp.get("title"))
        founder = seniority in FOUNDER_SENIORITY or (rank is not None and rank <= cfg.lex.founder_max_rank)
        yield Labeled(name=name, email=email, domain=domain, mv_result=mv, label=label, founder_level=founder)


def pattern_counts(rows: list[Labeled], cfg: Config) -> tuple[Counter, int]:
    """Weighted pattern counts (a row matching two patterns counts half each)."""
    counts: Counter = Counter()
    for r in rows:
        hits = matching_patterns(split_email(r.email)[0], r.name, cfg)
        if not hits:
            counts["other"] += 1
            continue
        for h in hits:
            counts[h] += 1 / len(hits)
    return counts, len(rows)
