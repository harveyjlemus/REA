"""AI read (lane 5): a small model re-reads pages already saved, for the rows
where plain code found no owner (or no last name).

Inside Claude Code:
  1. find_founders.py writes ai_read/batches/batch-NNN.jsonl and stops with exit code 3.
  2. Claude runs one subagent (model: haiku) per batch with agents/page-reader.md.
     Each writes ai_read/answers/batch-NNN.jsonl.
  3. The same find_founders.py command, run again, reads the answers.

An answer is accepted only if its quote is word-for-word on the saved page it
names, and the person's name is inside that quote. Anything else is rejected
with the reason, so an invented name can't get through."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from .config import Config
from .names import email_token, parse_full_name

BATCH_SIZE = 10
MAX_CHARS_PER_ROW = 12000
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTES = {ord("‘"): "'", ord("’"): "'", ord("‚"): "'", ord("‛"): "'", ord("“"): '"', ord("”"): '"',
           ord("„"): '"', ord(" "): " "}


def normalize_for_match(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").translate(_DASHES).translate(_QUOTES)
    return " ".join(s.split()).casefold()


def entry_for(row, site: dict, cfg: Config) -> dict | None:
    """The queue line for one row, or None when there's nothing readable."""
    pages = []
    budget = MAX_CHARS_PER_ROW
    order = {"about": 0, "team": 1, "home": 2, "contact": 3}
    for p in sorted(site.get("pages", []), key=lambda p: order.get(p.get("kind"), 9)):
        if not p.get("text_path") or not p.get("chars"):
            continue
        pages.append({"url": p.get("final_url") or p["url"], "text_path": p["text_path"], "kind": p.get("kind")})
        budget -= p["chars"]
        if budget <= 0:
            break
    if not pages:
        return None
    target = row.target or {}
    task = "find_last_name" if target.get("first") and not target.get("last") else "find_owner"
    return {"row_id": row.row_id, "company": row.input.get("company") or row.domain, "domain": row.domain,
            "task": task, "first_name": target.get("first") if task == "find_last_name" else None,
            "pages": pages}


def write_batches(ai_dir: Path, entries: list[dict]) -> list[Path]:
    """Write entries not already waiting in a batch. Returns the new batch files."""
    batch_dir = ai_dir / "batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    (ai_dir / "answers").mkdir(parents=True, exist_ok=True)
    waiting: set[str] = set()
    numbers = [0]
    for f in batch_dir.glob("batch-*.jsonl"):
        numbers.append(int(re.findall(r"\d+", f.stem)[0]))
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                waiting.add(json.loads(line)["row_id"])
    fresh = [e for e in entries if e["row_id"] not in waiting]
    out = []
    n = max(numbers)
    for start in range(0, len(fresh), BATCH_SIZE):
        n += 1
        path = batch_dir / f"batch-{n:03d}.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in fresh[start:start + BATCH_SIZE]), encoding="utf-8")
        out.append(path)
    return out


def pending_batches(ai_dir: Path) -> list[Path]:
    """Batch files that have no answers file yet."""
    batch_dir, ans_dir = ai_dir / "batches", ai_dir / "answers"
    return sorted(f for f in batch_dir.glob("batch-*.jsonl") if not (ans_dir / f.name).is_file())


def rows_in_answered_batches(ai_dir: Path) -> set[str]:
    """Row ids whose batch already has an answers file (answered or skipped)."""
    out: set[str] = set()
    ans_dir = ai_dir / "answers"
    for f in (ai_dir / "batches").glob("batch-*.jsonl"):
        if (ans_dir / f.name).is_file():
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    out.add(json.loads(line)["row_id"])
    return out


def read_answers(ai_dir: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in sorted((ai_dir / "answers").glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip().strip(",")
            if not line or not line.startswith("{"):
                continue
            try:
                ans = json.loads(line)
            except ValueError:
                continue
            if isinstance(ans, dict) and ans.get("row_id"):
                out[str(ans["row_id"])] = ans
    return out


def check_answer(answer: dict, entry: dict, cfg: Config) -> tuple[dict | None, str]:
    """(accepted person dict, reason). The person dict has the shape of a
    website candidate, with source 'ai_read'."""
    if not answer.get("found"):
        return None, f"not found: {str(answer.get('reason') or 'no owner named on the pages')[:120]}"
    name_raw = str(answer.get("name") or "").strip()
    quote = str(answer.get("quote") or "").strip()
    url = str(answer.get("url") or "").strip()
    page = next((p for p in entry["pages"] if p["url"] == url), None)
    if page is None:
        return None, "rejected: the url is not one of the pages given"
    try:
        text = Path(page["text_path"]).read_text(encoding="utf-8")
    except OSError:
        return None, "rejected: saved page text is missing"
    if not quote or normalize_for_match(quote) not in normalize_for_match(text):
        return None, "rejected: the quote is not word-for-word on the page"
    name = parse_full_name(name_raw, cfg)
    if not name or not name.first_token:
        return None, "rejected: not a usable name"
    q = email_token(quote)
    if name.first_token not in q or (name.last and email_token(name.last) not in q):
        return None, "rejected: the name is not inside the quote"
    if entry.get("task") == "find_last_name":
        want = email_token(entry.get("first_name"))
        if not name.last or (name.first_token != want and name.first_token not in cfg.nickname_forms(want)):
            return None, "rejected: not the person with the given first name"
    title = str(answer.get("title") or "").strip() or None
    rank = cfg.lex.title_rank(title) or cfg.lex.title_rank(quote)
    if entry.get("task") == "find_owner" and (rank is None or rank > cfg.lex.founder_max_rank):
        return None, "rejected: the quote doesn't show a founder, owner or president"
    return {"first": name.first, "last": name.last, "middle": name.middle, "full": name.full, "title": title,
            "rank": rank, "method": "ai_read", "strength": "strong", "url": url, "quote": quote[:300],
            "source": "ai_read"}, "accepted"
