"""The run folder and the per-row state (the row's evidence packet).

<run-dir>/
  input.csv            copy of the file you gave it
  mapping.json         how its columns were read
  rows.jsonl           one line per input row: row_id, original values, mapped values
  state/<row_id>.json  everything found for the row, with sources (resumable)
  pages/               every page read, raw and as text
  ai_read/             queue and answers for the AI read step
  cost_ledger.jsonl    one line per call
"""
from __future__ import annotations

import csv
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .columns import Mapping, apply_overrides, detect_mapping, read_table, row_values

STATE_VERSION = 1


@dataclass
class Row:
    row_id: str
    n: int
    input: dict
    raw: dict = field(default_factory=dict)
    kind: str = ""                 # has_email | full_name | first_name | company_only | none
    segment: str = "local"
    domain: str | None = None
    host: str | None = None
    flags: list[str] = field(default_factory=list)
    lanes: dict = field(default_factory=dict)
    people: list[dict] = field(default_factory=list)       # candidates from every lane
    target: dict | None = None                             # the person chosen
    found_emails: list[dict] = field(default_factory=list) # addresses found for the target
    evidence: list[dict] = field(default_factory=list)     # pattern evidence at the domain
    guesses: list[dict] = field(default_factory=list)
    verify: dict = field(default_factory=dict)             # waterfall outcome
    business_email: str | None = None
    duplicate_of: str | None = None
    closed: str | None = None      # terminal reason before guessing (no_company_domain, ...)
    result: dict = field(default_factory=dict)
    version: int = STATE_VERSION

    # -- lanes
    def lane(self, name: str) -> dict:
        return self.lanes.get(name) or {}

    def set_lane(self, name: str, status: str, **info) -> None:
        from .ledger import utc_now
        entry = {"status": status, "observed_at": utc_now()}
        entry.update({k: v for k, v in info.items() if v is not None})
        self.lanes[name] = entry

    def done(self, name: str) -> bool:
        return self.lane(name).get("status") in ("done", "absent")

    # -- outcome helpers
    @property
    def verified(self) -> dict | None:
        """The found or guessed address Million Verifier said ok to, if any."""
        for f in self.found_emails:
            if (f.get("mv") or {}).get("result") == "ok":
                return f
        if self.verify.get("status") == "verified":
            return {"email": self.verify["email"], "source": "guess", "pattern": self.verify.get("pattern")}
        return None

    @property
    def is_open(self) -> bool:
        return not self.closed and not self.duplicate_of and self.verified is None

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("raw", None)
        return d


class RunDir:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    # -- layout
    @property
    def state_dir(self) -> Path: return self.path / "state"
    @property
    def pages_dir(self) -> Path: return self.path / "pages"
    @property
    def ai_dir(self) -> Path: return self.path / "ai_read"
    @property
    def ledger_path(self) -> Path: return self.path / "cost_ledger.jsonl"
    @property
    def input_path(self) -> Path: return self.path / "input.csv"
    @property
    def mapping_path(self) -> Path: return self.path / "mapping.json"
    @property
    def rows_path(self) -> Path: return self.path / "rows.jsonl"

    def exists(self) -> bool:
        return self.rows_path.is_file() and self.mapping_path.is_file()

    def create(self, input_file: Path | str, cfg, overrides: list[str] | None = None) -> tuple[Mapping, list[Row]]:
        """Copy the input in, detect the mapping, write rows.jsonl."""
        self.path.mkdir(parents=True, exist_ok=True)
        src = Path(input_file)
        if src.suffix.lower() in (".xlsx", ".xlsm"):
            # Keep a CSV copy so every later step reads one format.
            header, data = read_table(src)
            with self.input_path.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(header)
                w.writerows(data)
        elif src.resolve() != self.input_path.resolve():
            shutil.copyfile(src, self.input_path)
        header, data = read_table(self.input_path)
        if not header:
            raise ValueError(f"{src.name} has no header row")
        mapping = apply_overrides(detect_mapping(header, cfg), header, overrides or [])
        self.mapping_path.write_text(json.dumps({"header": header, **mapping.to_dict()}, indent=2), encoding="utf-8")
        width = len(str(len(data)))
        rows: list[Row] = []
        with self.rows_path.open("w", encoding="utf-8") as fh:
            for i, values in enumerate(data):
                canonical, original = row_values(header, values, mapping)
                rid = f"r{str(i + 1).zfill(max(4, width))}"
                fh.write(json.dumps({"row_id": rid, "n": i, "input": canonical, "raw": original}) + "\n")
                rows.append(Row(row_id=rid, n=i, input=canonical, raw=original))
        return mapping, rows

    def remap(self, cfg, overrides: list[str]) -> Mapping:
        """Re-read the copied input with new --map overrides (only before anything ran)."""
        if any(self.state_dir.glob("*.json")):
            raise ValueError("this run already has results; start a new --run-dir to change the mapping")
        mapping, _ = self.create(self.input_path, cfg, overrides)
        return mapping

    def load_mapping(self) -> tuple[list[str], Mapping]:
        d = json.loads(self.mapping_path.read_text(encoding="utf-8"))
        header = d.pop("header")
        return header, Mapping.from_dict(d)

    def load_rows(self) -> list[Row]:
        rows: list[Row] = []
        for line in self.rows_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            base = json.loads(line)
            st = self.state_dir / f"{base['row_id']}.json"
            if st.is_file():
                data = json.loads(st.read_text(encoding="utf-8"))
                data["raw"] = base["raw"]
                data.pop("version", None)
                rows.append(Row(**{k: v for k, v in data.items() if k in Row.__dataclass_fields__}))
            else:
                rows.append(Row(row_id=base["row_id"], n=base["n"], input=base["input"], raw=base["raw"]))
        return rows

    def save(self, row: Row) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        p = self.state_dir / f"{row.row_id}.json"
        tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(row.to_json(), indent=1, default=str), encoding="utf-8")
        os.replace(tmp, p)

    def save_all(self, rows: list[Row]) -> None:
        for r in rows:
            self.save(r)
