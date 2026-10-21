"""Read the config folder once: patterns, column aliases, word lists, name lists,
search templates and (when present) the calibrated guess order."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from .env import SKILL_DIR

CONFIG_DIR = SKILL_DIR / "config"
CALIBRATED_FILE = "patterns.calibrated.yaml"


def _yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _name_list(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line.lower())
    return out


@dataclass
class Lexicons:
    founder_rank: dict[str, int]
    founder_max_rank: int
    role_inboxes: set[str]
    non_own_domains: set[str]
    freemail_domains: set[str]
    multi_part_suffixes: set[str]
    parked_markers: list[str]
    botwall_markers: list[str]
    js_shell_markers: list[str]
    subpage_keywords: list[list[str]]
    subpage_fallback_paths: list[str]
    name_stopwords: set[str]
    honorifics: set[str]
    name_suffixes: set[str]
    last_name_particles: set[str]
    segment_keywords: dict[str, list[str]]

    # One-word titles that also appear inside unrelated titles ("Partner Success
    # Manager", "Principal Engineer"). They count only at the start of a title.
    _START_ONLY = frozenset({"partner", "principal", "operator", "gm", "vp", "president"})

    def title_rank(self, title: str | None) -> int | None:
        """Best (lowest) founder rank whose phrase appears in the title."""
        if not title:
            return None
        norm = " ".join(title.lower().replace("&", " and ").split())
        text = f" {norm} "
        # Title segments: "Founder & CEO, Acme" -> ["founder", "ceo", "acme"]
        segments = [s.strip() for s in re.split(r"\s*(?:,|/|\||–|—| - | and )\s*", norm) if s.strip()]
        best = None
        for phrase, rank in self.founder_rank.items():
            if phrase in self._START_ONLY:
                ok = any(seg == phrase or seg.startswith((phrase + " of ", phrase + " at ", phrase + " @ "))
                         for seg in segments)
            else:
                ok = f" {phrase} " in text or any(seg == phrase for seg in segments)
            if ok and (best is None or rank < best):
                best = rank
        return best


@dataclass
class Config:
    patterns: dict[str, str]
    starting_order: list[str]
    first_only_order: list[str]
    max_guesses: int
    alt_last_top_n: int
    nickname_guess_rank: int
    aliases: dict
    lex: Lexicons
    formal_to_nicks: dict[str, list[str]]
    nick_to_formal: dict[str, list[str]]
    first_names: set[str]
    first_rank: dict[str, int]
    last_names: set[str]
    serp: dict
    calibrated: dict | None = None
    config_dir: Path = field(default=CONFIG_DIR)

    def nickname_forms(self, first_token: str) -> list[str]:
        """Other common forms of a first name, both directions, no duplicates."""
        out: list[str] = []
        for form in self.formal_to_nicks.get(first_token, []) + self.nick_to_formal.get(first_token, []):
            if form != first_token and form not in out:
                out.append(form)
        return out

    def calibrated_order(self, segment: str) -> tuple[list[str], dict] | None:
        """(order, info) for a segment if calibration has enough rows for it."""
        if not self.calibrated:
            return None
        seg = (self.calibrated.get("segments") or {}).get(segment)
        if not seg or not seg.get("order"):
            return None
        min_rows = int(self.calibrated.get("min_rows_to_trust", 30))
        if int(seg.get("sample_size", 0)) < min_rows:
            return None
        order = [p for p in seg["order"] if p in self.patterns]
        return (order, seg) if order else None


def _lexicons(raw: dict) -> Lexicons:
    founder_rank: dict[str, int] = {}
    for rank, phrases in (raw.get("founder_titles") or {}).items():
        for phrase in phrases:
            founder_rank[" ".join(str(phrase).lower().split())] = int(rank)
    return Lexicons(
        founder_rank=founder_rank,
        founder_max_rank=int(raw.get("founder_max_rank", 3)),
        role_inboxes={str(x).lower() for x in raw.get("role_inboxes", [])},
        non_own_domains={str(x).lower() for x in raw.get("non_own_domains", [])},
        freemail_domains={str(x).lower() for x in raw.get("freemail_domains", [])},
        multi_part_suffixes={str(x).lower() for x in raw.get("multi_part_suffixes", [])},
        parked_markers=[str(x).lower() for x in raw.get("parked_markers", [])],
        botwall_markers=[str(x).lower() for x in raw.get("botwall_markers", [])],
        js_shell_markers=[str(x).lower() for x in raw.get("js_shell_markers", [])],
        subpage_keywords=[[str(w).lower() for w in group] for group in raw.get("subpage_keywords", [])],
        subpage_fallback_paths=list(raw.get("subpage_fallback_paths", [])),
        name_stopwords={str(x).lower() for x in raw.get("name_stopwords", [])},
        honorifics={str(x).lower().rstrip(".") for x in raw.get("honorifics", [])},
        name_suffixes={str(x).lower().rstrip(".") for x in raw.get("name_suffixes", [])},
        last_name_particles={str(x).lower() for x in raw.get("last_name_particles", [])},
        segment_keywords={k: [str(w).lower() for w in v] for k, v in (raw.get("segment_keywords") or {}).items()},
    )


def load_config(config_dir: Path | str | None = None) -> Config:
    return _load_config(str(Path(config_dir or CONFIG_DIR).resolve()))


@lru_cache(maxsize=4)
def _load_config(config_dir: str) -> Config:
    cdir = Path(config_dir)
    pat = _yaml(cdir / "patterns.yaml")
    nick_raw = _yaml(cdir / "nicknames.yaml")
    formal_to_nicks: dict[str, list[str]] = {}
    nick_to_formal: dict[str, list[str]] = {}
    for formal, nicks in nick_raw.items():
        formal = str(formal).lower()
        formal_to_nicks[formal] = [str(n).lower() for n in nicks]
        for n in formal_to_nicks[formal]:
            nick_to_formal.setdefault(n, []).append(formal)
    firsts = _name_list(cdir / "first_names.txt")
    calibrated_path = cdir / CALIBRATED_FILE
    return Config(
        patterns={str(k): str(v) for k, v in (pat.get("patterns") or {}).items()},
        starting_order=list(pat.get("starting_order", [])),
        first_only_order=list(pat.get("first_only_order", ["first"])),
        max_guesses=int(pat.get("max_guesses_per_person", 6)),
        alt_last_top_n=int(pat.get("alt_last_name_top_n", 2)),
        nickname_guess_rank=int(pat.get("nickname_guess_rank", 0)),
        aliases=_yaml(cdir / "column_aliases.yaml"),
        lex=_lexicons(_yaml(cdir / "lexicons.yaml")),
        formal_to_nicks=formal_to_nicks,
        nick_to_formal=nick_to_formal,
        first_names=set(firsts),
        first_rank={n: i for i, n in enumerate(firsts)},
        last_names=set(_name_list(cdir / "last_names.txt")),
        serp=_yaml(cdir / "serp_queries.yaml"),
        calibrated=_yaml(calibrated_path) if calibrated_path.is_file() else None,
        config_dir=cdir,
    )
