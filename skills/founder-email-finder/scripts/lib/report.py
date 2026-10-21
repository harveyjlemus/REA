"""Outputs: the result columns for each row, the four CSVs, the run report,
and the preview cards with a cost estimate for the whole file."""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from .config import Config
from .domains import registrable, split_email
from .names import PersonName
from .patterns import matching_patterns
from .state import Row

OUT_COLUMNS = ["find_status", "founder_name", "founder_first", "founder_last", "founder_title", "founder_email",
               "verification", "confidence", "email_source", "email_pattern", "pattern_evidence", "name_source",
               "name_evidence", "business_email", "top_guess", "other_people", "mx_provider", "site_bucket",
               "guesses_checked", "row_cost_usd", "row_credits", "absent_reason", "warnings", "fef_row_id"]
TRUST = {"input": 0, "getleads": 1, "website": 2, "serper": 3, "ai_read": 4, "guess": 9}
PAID = ("millionverifier", "serper", "getleads")


def _pattern_for(email: str, row: Row, cfg: Config) -> str:
    t = row.target or {}
    if not t.get("first"):
        return ""
    name = PersonName(first=t["first"], last=t.get("last"), middle=t.get("middle"))
    hits = matching_patterns(split_email(email)[0], name, cfg)
    return hits[0] if hits else ""


def _proof_for(pattern: str, row: Row) -> str:
    if not pattern:
        return ""
    proven = [e for e in row.evidence if e["pattern"] == pattern and e.get("strength") == "proven"]
    if proven:
        e = proven[0]
        where = f" ({e['url']})" if e.get("url") else ""
        return f"{e['email']} = {e.get('person')}{where}"
    shape = [e for e in row.evidence if e["pattern"] == pattern]
    return f"address shape: {shape[0]['email']}" if shape else ""


def _code(reason: str | None, default: str = "not_checked") -> str:
    """Lane reasons ('lane_off: no API key') -> the short codes the outputs use."""
    r = str(reason or "")
    if r.startswith("lane_off"):
        return "lane_off"
    if "budget" in r:
        return "budget_reached"
    if "network" in r or "blocked" in r:
        return "network_blocked"
    if "manual" in r:
        return "awaiting_verification"
    return default


def _why_no_name(row: Row) -> str:
    lanes = row.lanes
    if any(l.get("reason") == "budget_reached" for l in lanes.values()):
        return "budget_reached"
    web = lanes.get("website", {})
    if "network_blocked" in str(web.get("reason") or ""):
        return "network_blocked"
    bucket = web.get("bucket")
    if bucket == "dead":
        return "site_dead"
    if bucket in ("bot_walled", "js_shell"):
        return "site_blocked"
    if any(str(lanes.get(l, {}).get("reason") or "").startswith("lane_off") for l in ("getleads_dm", "serper_person")):
        return "lane_off"
    return "no_name_found"


def finalize_row(row: Row, cfg: Config, memo, cost: dict | None) -> dict:
    t = row.target or {}
    res = {c: "" for c in OUT_COLUMNS}
    res["fef_row_id"] = row.row_id
    if t.get("first"):
        res["founder_first"] = t.get("first") or ""
        res["founder_last"] = t.get("last") or ""
        res["founder_name"] = " ".join(p for p in (t.get("first"), t.get("last")) if p)
        res["founder_title"] = t.get("title") or ""
        res["name_source"] = t.get("source", "") + (f"+{t['last_source']}" if t.get("last_source") else "")
        quote = " ".join(str(t.get("quote") or "").split())[:220]
        res["name_evidence"] = " ".join(p for p in (t.get("url") or "", f'"{quote}"' if quote else "") if p)
    res["business_email"] = row.business_email or ""
    others = [p for p in row.people if p.get("full") and p.get("full") != res["founder_name"]]
    seen, labels = set(), []
    for p in others:
        if p["full"] not in seen:
            seen.add(p["full"])
            labels.append(f"{p['full']} ({p['title']})" if p.get("title") else p["full"])
    res["other_people"] = "; ".join(labels[:5])
    mx = row.lane("mx")
    res["mx_provider"] = mx.get("provider") or mx.get("result") or ""
    res["site_bucket"] = row.lane("website").get("bucket") or ""
    res["guesses_checked"] = str(sum(1 for g in row.guesses if (g.get("mv") or {}).get("source", "api") == "api" and g.get("mv")))
    if cost:
        res["row_cost_usd"] = f"{cost['usd']:.4f}" + (" + unknown" if cost.get("unknown_usd_calls") else "")
        units = cost.get("units", {})
        res["row_credits"] = " ".join(f"{p}:{int(units[p])}" for p in PAID if units.get(p))
    else:
        res["row_cost_usd"] = "0.0000"
    warnings = list(dict.fromkeys(row.flags))
    if t.get("source") == "ai_read" or t.get("last_source") == "ai_read":
        warnings.append("name_from_ai_read")
    if t.get("first") and not t.get("last"):
        warnings.append("first_name_only")
    if t.get("rank") and t["rank"] > cfg.lex.founder_max_rank:
        warnings.append("not_founder_level")

    def use(email: str, source: str, verification: str) -> None:
        res["founder_email"] = email
        res["email_source"] = source
        res["verification"] = verification
        res["email_pattern"] = _pattern_for(email, row, cfg) if source != "guess" else (row.verify.get("pattern") or "")
        res["pattern_evidence"] = _proof_for(res["email_pattern"], row)

    v = row.verified
    if v:
        use(v["email"], v.get("source", "guess"), "ok")
        res["find_status"], res["confidence"] = "found", "high"
    elif row.closed:
        res["find_status"] = "skipped" if row.closed in ("closed_business", "no_company_domain") else "not_found"
        res["absent_reason"] = row.closed
    else:
        risky = [(f, (f.get("mv") or {}).get("result")) for f in sorted(row.found_emails, key=lambda f: TRUST.get(f["source"], 5))
                 if (f.get("mv") or {}).get("result") in ("catch_all", "unknown")]
        if row.verify.get("status") in ("catch_all", "unknown") and row.verify.get("email"):
            risky.append(({"email": row.verify["email"], "source": "guess"}, row.verify["status"]))
        unchecked = [f for f in sorted(row.found_emails, key=lambda f: TRUST.get(f["source"], 5)) if not f.get("mv")]
        vstatus = row.verify.get("status")
        if risky:
            f, r = risky[0]
            use(f["email"], f["source"], r)
            found_not_guessed = f["source"] != "guess"
            proven = bool(res["pattern_evidence"]) and not res["pattern_evidence"].startswith("address shape")
            res["confidence"] = "medium" if (found_not_guessed or proven) and t.get("source") else "low"
            res["find_status"] = "risky"
            res["absent_reason"] = "catch_all_unresolved" if r == "catch_all" else "unknown_unresolved"
        elif unchecked:
            f = unchecked[0]
            use(f["email"], f["source"], "not_checked")
            res["find_status"] = "unverified"
            res["confidence"] = "medium" if f["source"] == "getleads" else "low"
            res["absent_reason"] = _code(row.lane("verify_found").get("reason"))
        elif vstatus == "waiting":
            nxt = next((g for g in row.guesses if not g.get("mv")), None)
            res["find_status"] = "awaiting_verification"
            res["top_guess"] = nxt["email"] if nxt else ""
            res["absent_reason"] = "awaiting_verification"
        elif vstatus == "not_checked":
            # Guesses exist but nothing checked them (no Million Verifier key or
            # budget this run): they wait, exactly like the manual round trip.
            nxt = next((g for g in row.guesses if not g.get("mv")), None)
            res["find_status"] = "awaiting_verification"
            res["verification"] = "not_checked"
            res["top_guess"] = nxt["email"] if nxt else ""
            res["absent_reason"] = _code(row.verify.get("detail"))
        elif vstatus == "all_invalid":
            res["find_status"], res["absent_reason"] = "not_found", "all_guesses_invalid"
        elif vstatus in ("budget_reached", "check_failed"):
            res["find_status"], res["absent_reason"] = "not_found", vstatus
            res["top_guess"] = next((g["email"] for g in row.guesses if not g.get("mv")), "")
        elif not t.get("first"):
            res["find_status"], res["absent_reason"] = "not_found", _why_no_name(row)
        else:
            res["find_status"] = "not_found"
            res["absent_reason"] = "no_guesses" if row.domain else "no_company_domain"
    if row.guesses and not res["top_guess"] and res["find_status"] != "found":
        res["top_guess"] = row.guesses[0]["email"]
    res["warnings"] = ", ".join(dict.fromkeys(warnings))
    return res


def finalize_all(rows: list[Row], cfg: Config, memo, costs: dict) -> None:
    """Result columns for every row; a duplicate copies its first row's result."""
    by_id = {r.row_id: r for r in rows}
    for r in rows:
        r.result = finalize_row(r, cfg, memo, costs.get(r.row_id))
    for r in rows:
        if r.duplicate_of and r.duplicate_of in by_id:
            src = by_id[r.duplicate_of].result
            warn = ", ".join(w for w in (src.get("warnings"), f"duplicate_of {r.duplicate_of}") if w)
            r.result = {**src, "find_status": "duplicate", "absent_reason": "duplicate", "warnings": warn,
                        "row_cost_usd": "0.0000", "row_credits": "", "fef_row_id": r.row_id}


# ------------------------------------------------------------------ CSV files
def _columns(header: list[str]) -> list[tuple[str, str]]:
    """(our key, column name as written) avoiding clashes with input headers."""
    taken = {h.strip().lower() for h in header}
    return [(c, f"{c}_fef" if c.lower() in taken else c) for c in OUT_COLUMNS]


def write_outputs(run_dir: Path, rows: list[Row], header: list[str]) -> dict[str, int]:
    cols = _columns(header)
    files = {"results.csv": lambda r: True,
             "send_ready.csv": lambda r: r.result.get("find_status") == "found" and r.result.get("verification") == "ok",
             "risky.csv": lambda r: r.result.get("find_status") in ("risky", "unverified"),
             "not_found.csv": lambda r: r.result.get("find_status") in ("not_found", "skipped")}
    counts = {}
    for name, keep in files.items():
        path = Path(run_dir) / name
        n = 0
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([c for _, c in cols] + header)
            for r in sorted(rows, key=lambda r: r.n):
                if not r.result:
                    continue
                if name != "results.csv" and r.duplicate_of:
                    continue
                if keep(r):
                    w.writerow([r.result.get(k, "") for k, _ in cols] + [r.raw.get(h, "") for h in header])
                    n += 1
        counts[name] = n
    return counts


def write_evidence(run_dir: Path, rows: list[Row]) -> None:
    import json
    d = Path(run_dir) / "evidence"
    d.mkdir(parents=True, exist_ok=True)
    for r in rows:
        if r.result:
            (d / f"{r.row_id}.json").write_text(json.dumps(r.to_json(), indent=1, default=str), encoding="utf-8")


# ------------------------------------------------------------------ run report
def run_report(rows: list[Row], ledger_summary: dict, *, budgets: dict, blocked: dict, mapping) -> str:
    total = len(rows)
    status = Counter(r.result.get("find_status") for r in rows if r.result)
    reasons = Counter(r.result.get("absent_reason") for r in rows if r.result and r.result.get("absent_reason"))
    sources = Counter(r.result.get("email_source") for r in rows if r.result.get("find_status") == "found")
    name_src = Counter((r.target or {}).get("source") for r in rows if (r.target or {}).get("first"))
    buckets = Counter(r.lane("website").get("bucket") for r in rows if r.lane("website").get("bucket"))
    kinds = Counter(r.kind for r in rows)
    prov = ledger_summary.get("by_provider", {})
    lines = ["# Run report", "",
             f"Input: {total} rows, read as **{mapping.format_label}**.", "",
             "## Funnel", "",
             "| Stage | Rows |", "|---|---|",
             f"| Input rows | {total} |",
             f"| Skipped (closed, no own domain) | {status.get('skipped', 0)} |",
             f"| Duplicates | {status.get('duplicate', 0)} |",
             f"| Person named | {sum(name_src.values())} |",
             f"| **Verified email (send_ready.csv)** | **{status.get('found', 0)}** |",
             f"| Risky: catch-all / unknown / unchecked (risky.csv) | {status.get('risky', 0) + status.get('unverified', 0)} |",
             f"| Waiting for manual verification | {status.get('awaiting_verification', 0)} |",
             f"| Not found (not_found.csv) | {status.get('not_found', 0)} |", "",
             "## Where things came from", "",
             "- Rows started as: " + ", ".join(f"{k} {v}" for k, v in kinds.most_common()),
             "- Names from: " + (", ".join(f"{k} {v}" for k, v in name_src.most_common()) or "none"),
             "- Verified emails from: " + (", ".join(f"{k} {v}" for k, v in sources.most_common()) or "none"),
             "- Website buckets: " + (", ".join(f"{k} {v}" for k, v in buckets.most_common()) or "website lane did not run"),
             "", "## Why rows have no verified email", ""]
    lines += [f"- {k}: {v}" for k, v in reasons.most_common()] or ["- (none)"]
    lines += ["", "## Spend", "", "| Provider | Calls | Units | Known $ | Calls with unknown $ |", "|---|---|---|---|---|"]
    for p, d in sorted(prov.items()):
        lines.append(f"| {p} | {d['calls']} | {int(d['units'])} | {d['usd']:.4f} | {d['unknown_usd_calls']} |")
    known = sum(d["usd"] for d in prov.values())
    found = status.get("found", 0)
    lines += ["", f"Known spend: ${known:.4f}" + (f" · ${known / found:.4f} per verified email" if found else ""),
              "Unknown dollar amounts are counted, not guessed: set MV_USD_PER_CHECK / SERPER_USD_PER_SEARCH in .env.",
              "", "## Budgets (they count everything this run folder spent, previews included)", ""]
    for name, b in budgets.items():
        lines.append(f"- {name}: {b.used} of {b.limit} used" if b.enabled else f"- {name}: off (no budget set)")
    if blocked:
        lines += ["", "## Lanes that could not run", ""] + [f"- {k}: {v}" for k, v in blocked.items()]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ preview
def _lane_line(row: Row, name: str) -> str:
    l = row.lane(name)
    if not l:
        return "not needed"
    bits = [l.get("status", "")]
    for k in ("result", "provider", "bucket", "outcome", "searches", "records", "n", "checked", "addresses"):
        if l.get(k) not in (None, "", []):
            bits.append(f"{k}={l[k]}")
    if l.get("reason"):
        bits.append(f"({l['reason']})")
    return " · ".join(str(b) for b in bits if b != "")


def card(row: Row) -> str:
    r = row.result or {}
    company = row.input.get("company") or ""
    head = f"### {row.row_id} · {company} · {row.domain or 'no domain'}"
    lines = [head, f"Starts with: {row.kind.replace('_', ' ')} · segment: {row.segment}", "",
             "| Lane | What happened |", "|---|---|",
             f"| 1 Clean | {_lane_line(row, 'clean')} |",
             f"| 2 Mail check | {_lane_line(row, 'mx')} |",
             f"| 3 Website | {_lane_line(row, 'website')} |",
             f"| 4 GetLeads | enrich: {_lane_line(row, 'getleads_enrich')}; decision-makers: {_lane_line(row, 'getleads_dm')} |",
             f"| 5 AI read | {_lane_line(row, 'ai_read')} |",
             f"| 6 Serper | person: {_lane_line(row, 'serper_person')}; pattern: {_lane_line(row, 'serper_pattern')} |"]
    checks = [f"{g['email']} {g['mv']['result']}" for g in row.guesses if g.get("mv")]
    found = [f"{f['email']} ({f['source']}) {(f.get('mv') or {}).get('result', 'not checked')}" for f in row.found_emails]
    lines.append(f"| 7 Verify | {'; '.join(found + checks) or row.verify.get('detail') or 'nothing to check'} |")
    email_txt = r.get("founder_email") or (f"no email yet (next guess to check: {r['top_guess']})"
                                            if r.get("top_guess") else "no email")
    summary = (f"**Result:** {r.get('find_status', '?').upper()} · {email_txt}"
               f" · verification {r.get('verification') or '-'} · confidence {r.get('confidence') or '-'}")
    if r.get("founder_name"):
        summary += f" · {r['founder_name']} ({r.get('name_source')})"
    if r.get("absent_reason"):
        summary += f" · reason: {r['absent_reason']}"
    summary += f" · cost ${r.get('row_cost_usd', '0')}" + (f" ({r['row_credits']})" if r.get("row_credits") else "")
    lines += ["", summary]
    if r.get("name_evidence"):
        lines.append(f"Name evidence: {r['name_evidence']}")
    return "\n".join(lines)


def estimate(rows: list[Row], built: list[Row], ledger_summary: dict, budgets: dict, *, prices: dict,
             concurrency: int, delay: float, extras: dict) -> str:
    """Project the sample's per-row usage onto the rows not built yet."""
    by_row = ledger_summary.get("by_row", {})
    built_ids = {r.row_id for r in built if r.result}
    sample = [by_row.get(rid, {"units": {}}) for rid in built_ids]
    n_sample = max(1, len(sample))
    remaining = [r for r in rows if r.row_id not in built_ids and not r.closed and not r.duplicate_of]
    closed = Counter(r.closed for r in rows if r.closed)
    kinds = Counter(r.kind for r in rows)
    lines = ["## Estimate for the whole file", "",
             f"- Rows: {len(rows)} · " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in kinds.most_common()),
             f"- Stopped for free before any lookup: " + (", ".join(f"{k} {v}" for k, v in closed.items()) or "none"),
             f"- Built in this preview: {len(built_ids)} · still to run: {len(remaining)}", ""]
    lines += ["| Lane | Per row in the sample | Projected for the rest | Cap this run | Cost |", "|---|---|---|---|---|"]
    for prov, label, bname, price_key in (("website", "Website pages", None, None),
                                          ("getleads", "GetLeads credits", "getleads", "getleads"),
                                          ("serper", "Serper searches", "serper", "serper"),
                                          ("millionverifier", "Million Verifier checks", "mv", "mv")):
        per = sum(s["units"].get(prov, 0) for s in sample) / n_sample
        proj = per * len(remaining)
        cap = budgets[bname] if bname else None
        cap_txt = "free" if not cap else (f"{cap.limit} (off)" if not cap.enabled else f"{cap.limit}")
        if cap and cap.enabled and proj > cap.remaining:
            cap_txt += " ← the cap will stop this lane early"
        price = prices.get(price_key) if price_key else 0.0
        if price_key is None or proj == 0:
            cost = "$0"
        elif cap and not cap.enabled:
            cost = "lane off"
        else:
            cost = f"~${proj * price:.2f}" if price is not None else "unknown (set the price in .env)"
        lines.append(f"| {label} | {per:.2f} | ~{proj:.0f} | {cap_txt} | {cost} |")
    pages = sum(s["units"].get("website", 0) for s in sample) / n_sample * len(remaining)
    minutes = pages * max(delay, 0.8) / max(1, concurrency) / 60
    lines += ["", f"- Website time: about {minutes:.0f} min at {concurrency} sites at a time"]
    for k, v in extras.items():
        lines.append(f"- {k}: {v}")
    return "\n".join(lines)


def render_preview(rows: list[Row], built: list[Row], shown: list[Row], mapping, est: str,
                   stop_line: str) -> str:
    fields = ", ".join(f"{k} ← '{v}'" for k, v in mapping.fields.items())
    lines = ["# Preview", "", f"**File read as:** {mapping.format_label}", "", f"**Columns used:** {fields}", ""]
    if mapping.notes:
        lines += ["**Notes:** " + "; ".join(mapping.notes), ""]
    lines += ["## Sample rows", ""]
    for r in shown:
        lines += [card(r), ""]
    lines += [est, "", stop_line, ""]
    return "\n".join(lines)


def pick_preview(built: list[Row], n: int) -> list[Row]:
    """A spread: one row of each outcome first, then by input order."""
    out, seen = [], set()
    for r in sorted(built, key=lambda r: r.n):
        key = (r.result or {}).get("find_status")
        if key not in seen:
            out.append(r)
            seen.add(key)
        if len(out) >= n:
            return out
    for r in sorted(built, key=lambda r: r.n):
        if r not in out:
            out.append(r)
        if len(out) >= n:
            break
    return out


def domain_of(email: str, cfg: Config) -> str:
    return registrable(split_email(email)[1], cfg.lex)
