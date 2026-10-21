"""The pipeline: every lane, free ones first, over all rows, in waves.

A row stops the moment it has an address Million Verifier calls "ok". Each
wave only touches rows that still need it and records what it did on the row,
so a stopped run picks up where it left off and nothing is paid for twice.

Order (company-only rows read the website before GetLeads, to save the daily
fair-use allowance; full-name rows try GetLeads first, it's one batch call):

  clean -> mail check -> verify input emails
  -> GetLeads enrich (input names) -> website -> people from the site
  -> GetLeads enrich (names from the site) -> GetLeads decision-makers
  -> AI read (may pause for Claude) -> GetLeads enrich (names from the AI read)
  -> Serper person -> GetLeads enrich (names from Serper) -> Serper pattern
  -> guess -> verify guesses -> retry unknowns -> finalize
Every discovery step is followed by a Million Verifier check of what it found.
"""
from __future__ import annotations

import json
import re
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import ai_read
from .budget import Budget
from .cache import DomainMemo, PageCache
from .columns import is_truthy
from .config import Config
from .domains import domain_for_row, is_freemail, registrable, split_email, valid_email
from .getleads import GetLeadsAuthError, GetLeadsError
from .guesses import build_guesses
from .http import NetworkBlocked
from .millionverifier import MvAuthError, MvError, MvNoCredits, normalize_result
from .names import PersonName, display, email_token, person_from_fields, same_person
from .patterns import evidence_from_address, matching_patterns
from .serper import SerperAuthError, SerperError, emails_from_results, fill_query, people_from_results
from .state import Row, RunDir
from .verify import apply_answer, waterfall
from .website import Harvester, SiteResult

SOURCE_TRUST = {"input": 0, "getleads": 1, "website": 2, "serper": 3, "ai_read": 4, "guess": 9}


@dataclass
class Settings:
    segment: str = "auto"
    serp_max: int = 0
    mv_max: int = 0
    getleads_max: int = 0
    getleads_per_domain: int = 5
    max_guesses: int | None = None
    max_subpages: int = 4
    concurrency: int = 8
    verify_mode: str = "api"          # api | manual
    ai_read: bool = True
    offline: bool = False
    mv_timeout: int = 20
    verify_existing: bool = True
    workers: int = 4


def _kw_hits(text: str, words: list[str]) -> int:
    """Whole words for short keywords ('api' must not match 'landscaping'),
    word starts for longer ones ('roof' matches 'roofing')."""
    n = 0
    for w in words:
        pat = rf"\b{re.escape(w)}\b" if len(w) <= 4 else rf"\b{re.escape(w)}"
        if re.search(pat, text):
            n += 1
    return n


def detect_segment(inp: dict, cfg: Config) -> str:
    seg = (inp.get("segment") or "").strip().lower()
    if seg in ("local", "startup"):
        return seg
    text = " ".join(str(inp.get(k) or "") for k in ("industry", "title", "company")).lower()
    startup = _kw_hits(text, cfg.lex.segment_keywords.get("startup", []))
    local = _kw_hits(text, cfg.lex.segment_keywords.get("local", []))
    return "startup" if startup > local else "local"


def _target_name(t: dict | None) -> PersonName | None:
    if not t or not t.get("first"):
        return None
    return PersonName(first=t["first"], last=t.get("last"), middle=t.get("middle"))


def _person_dict(name: PersonName, *, source: str, title=None, rank=None, url=None, quote=None, method=None) -> dict:
    return {"first": display(name.first), "last": display(name.last) if name.last else None,
            "middle": name.middle, "full": display(name.full), "title": title, "rank": rank,
            "source": source, "url": url, "quote": quote, "method": method}


class Pipeline:
    def __init__(self, run: RunDir, cfg: Config, settings: Settings, *, transport, mx, getleads=None,
                 serper=None, mv=None, memo: DomainMemo | None = None, ledger=None, log=print):
        self.run = run
        self.cfg = cfg
        self.s = settings
        self.mx = mx
        self.getleads = getleads
        self.serper = serper
        self.mv = mv
        self.memo = memo or DomainMemo(None)
        self.ledger = ledger
        self.log = log
        self.budgets = {"serper": Budget("serper", settings.serp_max),
                        "mv": Budget("mv", settings.mv_max),
                        "getleads": Budget("getleads", settings.getleads_max)}
        # Caps cover the whole run folder: what a preview or an earlier pass of
        # this run already spent counts against them.
        if ledger is not None:
            from .ledger import summarize
            spent = summarize(ledger.events())["by_provider"]
            for name, provider in (("serper", "serper"), ("mv", "millionverifier"), ("getleads", "getleads")):
                self.budgets[name].used = int(spent.get(provider, {}).get("units", 0))
        self.harvester = Harvester(transport, PageCache(run.pages_dir), cfg, max_subpages=settings.max_subpages,
                                   offline=settings.offline, ledger=ledger)
        self.sites: dict[str, dict] = {}
        self.blocked: dict[str, str] = {}        # lane -> why it is off for the whole run
        self.run_unknown: set[str] = set()
        self._lock = threading.Lock()
        self.dirty: set[str] = set()

    # ------------------------------------------------------------------ helpers
    def _touch(self, row: Row) -> None:
        self.dirty.add(row.row_id)

    def _save(self, rows: list[Row]) -> None:
        for r in rows:
            if r.row_id in self.dirty:
                self.run.save(r)
        self.dirty.clear()
        self.memo.flush()

    def _block(self, lane: str, why: str) -> None:
        with self._lock:
            if lane not in self.blocked:
                self.blocked[lane] = why
                self.log(f"  ! {lane} is off for this run: {why}")

    def _lane_off(self, lane: str, key_present: bool) -> str | None:
        """Why a paid lane can't run, or None if it can."""
        if lane in self.blocked:
            return self.blocked[lane]
        budget = {"getleads_enrich": "getleads", "getleads_dm": "getleads", "serper_person": "serper",
                  "serper_pattern": "serper", "verify": "mv"}.get(lane)
        if not key_present:
            return "lane_off: no API key"
        if budget and not self.budgets[budget].enabled:
            return "lane_off: no budget set"
        return None

    def _by_domain(self, rows: list[Row], fn) -> None:
        """Run fn(rows_of_one_domain) per domain, domains in parallel."""
        groups: dict[str, list[Row]] = defaultdict(list)
        for r in rows:
            groups[r.domain or r.row_id].append(r)
        if self.s.workers <= 1 or len(groups) <= 1:
            for g in groups.values():
                fn(g)
            return
        with ThreadPoolExecutor(max_workers=self.s.workers) as pool:
            list(pool.map(fn, groups.values()))

    def _open(self, rows: list[Row]) -> list[Row]:
        return [r for r in rows if r.is_open]

    # ------------------------------------------------------------------ run
    def run_rows(self, rows: list[Row]) -> str:
        """'done', or 'ai_read_pending' when Claude needs to read pages first."""
        self._restore_sites(rows)
        steps = [
            self.wave_clean, self.wave_mx, self.wave_verify_found,
            lambda w: self.wave_getleads_enrich(w, "input"), self.wave_verify_found,
            self.wave_website, self.wave_people_from_site, self.wave_verify_found,
            lambda w: self.wave_getleads_enrich(w, "website"), self.wave_verify_found,
            self.wave_getleads_dm, self.wave_verify_found,
        ]
        for step in steps:
            step(rows)
            self._save(rows)
        if self.wave_ai_read(rows) == "pending":
            self._save(rows)
            return "ai_read_pending"
        self._save(rows)
        for step in (lambda w: self.wave_getleads_enrich(w, "ai_read"), self.wave_verify_found,
                     self.wave_serper_person,
                     lambda w: self.wave_getleads_enrich(w, "serper"), self.wave_verify_found,
                     self.wave_serper_pattern, self.wave_verify_found,
                     self.wave_guess, self.wave_verify_guesses, self.wave_retry_unknown, self.wave_finalize):
            step(rows)
            self._save(rows)
        return "done"

    def _restore_sites(self, rows: list[Row]) -> None:
        for r in rows:
            site = r.lane("website").get("site")
            if r.domain and site and r.domain not in self.sites:
                self.sites[r.domain] = site

    # ------------------------------------------------------------------ 1 clean
    def wave_clean(self, rows: list[Row]) -> None:
        cfg = self.cfg
        for row in rows:
            if row.done("clean"):
                continue
            inp = row.input
            info = domain_for_row(inp.get("website"), inp.get("email"), cfg.lex)
            row.domain, row.host, row.flags = info.domain, info.host, list(info.flags)
            row.segment = self.s.segment if self.s.segment in ("local", "startup") else detect_segment(inp, cfg)
            person = person_from_fields(inp.get("first_name"), inp.get("last_name"), inp.get("full_name"), cfg)
            if person:
                title = inp.get("title") or None
                row.target = _person_dict(person, source="input", title=title, rank=cfg.lex.title_rank(title))
            email = (inp.get("email") or "").strip().lower()
            if valid_email(email) and split_email(email)[0] in cfg.lex.role_inboxes:
                # info@ / office@ belongs to the company, not the founder.
                row.business_email = email
                email = ""
            if valid_email(email):
                entry = {"email": email, "source": "input", "url": None, "quote": None, "mv": None}
                mv_result = inp.get("mv_result")
                if mv_result:
                    entry["mv"] = {"email": email, "result": normalize_result(mv_result),
                                   "quality": inp.get("mv_quality") or None, "source": "input_report"}
                row.found_emails.append(entry)
            biz = (inp.get("business_email") or "").strip().lower()
            if valid_email(biz) and split_email(biz)[0] in cfg.lex.role_inboxes:
                row.business_email = biz
            self._scrapio_emails(row)
            row.kind = ("has_email" if row.found_emails else "full_name" if person and person.last
                        else "first_name" if person else "company_only")
            if is_truthy(inp.get("closed")):
                row.closed = "closed_business"
            elif not row.domain and not row.found_emails:
                row.closed = "no_company_domain"
            row.set_lane("clean", "done", domain=row.domain, kind=row.kind, segment=row.segment,
                         reason=info.reason if not row.domain else None)
            self._touch(row)
        # Duplicates: same domain and same person (or both company-only).
        seen: dict[tuple, str] = {}
        for row in rows:
            if row.closed or row.duplicate_of or not row.domain:
                continue
            t = row.target or {}
            key = (row.domain, email_token(t.get("first")), email_token(t.get("last")))
            if key in seen and seen[key] != row.row_id:
                row.duplicate_of = seen[key]
                self._touch(row)
            else:
                seen.setdefault(key, row.row_id)

    def _scrapio_emails(self, row: Row) -> None:
        """Scrap.io's 'All emails' column: addresses with first/last names attached."""
        raw = row.input.get("all_emails_json")
        if not raw or not row.domain:
            return
        try:
            items = json.loads(raw)
        except ValueError:
            return
        target = _target_name(row.target)
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            email = str(it.get("email") or "").strip().lower()
            if not valid_email(email) or registrable(split_email(email)[1], self.cfg.lex) != row.domain:
                continue
            fn, ln = it.get("firstname"), it.get("lastname")
            if fn and ln:
                person = PersonName(first=str(fn), last=str(ln))
                if target and same_person(person, target, self.cfg):
                    if not any(f["email"] == email for f in row.found_emails):
                        row.found_emails.append({"email": email, "source": "input", "url": None,
                                                 "quote": "Scrap.io All emails", "mv": None})
                    continue
                for ev in evidence_from_address(email, [person], self.cfg, source="scrapio"):
                    self._add_evidence(row, ev.to_dict())
            elif split_email(email)[0] in self.cfg.lex.role_inboxes:
                row.business_email = row.business_email or email

    # ------------------------------------------------------------------ 2 mail check
    def wave_mx(self, rows: list[Row]) -> None:
        for row in self._open(rows):
            if not row.domain or row.done("mx"):
                continue
            if "mx" in self.blocked:
                row.set_lane("mx", "absent", result="network_blocked", reason=self.blocked["mx"])
                self._touch(row)
                continue
            res = self.mx.check(row.domain, row.row_id)
            status = "done" if res.status in ("has_mx", "no_mx", "no_domain") else "absent"
            if res.status == "network_blocked":
                self._block("mx", f"this environment can't look up mail servers ({res.error or 'no DNS'})")
            row.set_lane("mx", status, result=res.status, provider=res.provider, hosts=res.hosts[:3],
                         method=res.method, reason=self.blocked.get("mx") if res.status == "network_blocked" else res.error)
            if res.status in ("no_mx", "no_domain"):
                other = [f for f in row.found_emails if registrable(split_email(f["email"])[1], self.cfg.lex) != row.domain]
                if not other:
                    row.closed = "no_mail_server" if res.status == "no_mx" else "domain_does_not_exist"
            self._touch(row)

    # ------------------------------------------------------------------ verify found addresses
    def wave_verify_found(self, rows: list[Row]) -> None:
        todo = [r for r in self._open(rows) if any(f.get("mv") is None for f in r.found_emails)]
        if not todo:
            return
        off = None if self.s.verify_mode == "api" else "manual verification mode"
        off = off or self._lane_off("verify", self.mv is not None)
        if off:
            for r in todo:
                r.set_lane("verify_found", "absent", reason=off)
                self._touch(r)
            return
        self._by_domain(todo, self._verify_found_group)

    def _verify_found_group(self, rows: list[Row]) -> None:
        for row in rows:
            for f in sorted(row.found_emails, key=lambda f: SOURCE_TRUST.get(f["source"], 5)):
                if f.get("mv") is not None or row.verified:
                    continue
                if "verify" in self.blocked:
                    return
                dom = registrable(split_email(f["email"])[1], self.cfg.lex)
                if self.memo.status(dom) == "catch_all":
                    f["mv"] = {"email": f["email"], "result": "catch_all", "source": "memo"}
                    self._touch(row)
                    continue
                if not self.budgets["mv"].take(1):
                    row.set_lane("verify_found", "absent", reason="budget_reached")
                    self._touch(row)
                    return
                try:
                    ans = self.mv.verify(f["email"], timeout_s=self.s.mv_timeout, row_id=row.row_id)
                except (MvAuthError, MvNoCredits) as exc:
                    self.budgets["mv"].refund(1)
                    self._block("verify", str(exc))
                    return
                except NetworkBlocked as exc:
                    self.budgets["mv"].refund(1)
                    self._block("verify", f"network_blocked: {exc}")
                    return
                except MvError as exc:
                    f["mv"] = {"email": f["email"], "result": "error", "subresult": str(exc)[:120]}
                    continue
                f["mv"] = ans.to_dict()
                target = _target_name(row.target)
                pid = matching_patterns(split_email(f["email"])[0], target, self.cfg)[:1] if target else []
                if pid and dom == row.domain:
                    apply_answer({"pattern": pid[0]}, ans.result, domain=dom, memo=self.memo,
                                 run_unknown=self.run_unknown)
                elif ans.result == "catch_all":
                    self.memo.set_status(dom, "catch_all")
                row.set_lane("verify_found", "done", checked=f["email"], result=ans.result)
                self._touch(row)

    # ------------------------------------------------------------------ 4 GetLeads
    def wave_getleads_enrich(self, rows: list[Row], stage: str) -> None:
        todo = []
        for r in self._open(rows):
            t = r.target or {}
            if not (r.domain and t.get("first") and t.get("last")):
                continue
            key = f"{email_token(t['first'])}|{email_token(t['last'])}"
            if key in r.lane("getleads_enrich").get("tried", []):
                continue
            todo.append((r, key))
        if not todo:
            return
        off = self._lane_off("getleads_enrich", self.getleads is not None)
        if off:
            for r, _ in todo:
                if not r.done("getleads_enrich"):
                    r.set_lane("getleads_enrich", "absent", reason=off)
                    self._touch(r)
            return
        budget = self.budgets["getleads"]
        allowed = []
        for r, key in todo:
            if budget.take(1):
                allowed.append((r, key))
            else:
                r.set_lane("getleads_enrich", "absent", reason="budget_reached",
                           tried=r.lane("getleads_enrich").get("tried", []))
                self._touch(r)
        if not allowed:
            return
        items = [{"first_name": r.target["first"], "last_name": r.target["last"], "email_domain": r.domain,
                  **({"company_name": r.input["company"]} if r.input.get("company") else {})} for r, _ in allowed]
        try:
            results = self.getleads.enrich_from_person(items, [r.row_id for r, _ in allowed])
        except (GetLeadsAuthError, NetworkBlocked) as exc:
            budget.refund(len(allowed))
            self._block("getleads_enrich", str(exc))
            self._block("getleads_dm", str(exc))
            return
        except GetLeadsError as exc:
            budget.refund(len(allowed))
            for r, _ in allowed:
                r.set_lane("getleads_enrich", "error", reason=str(exc)[:200])
                self._touch(r)
            return
        for (r, key), res in zip(allowed, results):
            tried = r.lane("getleads_enrich").get("tried", []) + [key]
            email = (res or {}).get("email")
            note = "no match"
            if res.get("success") and email and valid_email(email):
                edom = registrable(split_email(email)[1], self.cfg.lex)
                if edom == r.domain:
                    if not any(f["email"] == email for f in r.found_emails):
                        r.found_emails.append({"email": email, "source": "getleads", "url": res.get("linkedin"),
                                               "quote": None, "mv": None, "status": res.get("email_status"),
                                               "stage": stage})
                    note = "email found"
                else:
                    r.flags.append("getleads_email_at_other_domain")
                    note = f"email at another domain ({edom})"
            r.set_lane("getleads_enrich", "done", tried=tried, outcome=note, stage=stage)
            self._touch(r)

    def wave_getleads_dm(self, rows: list[Row]) -> None:
        todo = [r for r in self._open(rows) if r.domain and not r.target and not r.done("getleads_dm")]
        if not todo:
            return
        off = self._lane_off("getleads_dm", self.getleads is not None)
        if off:
            for r in todo:
                r.set_lane("getleads_dm", "absent", reason=off)
                self._touch(r)
            return
        groups: dict[str, list[Row]] = defaultdict(list)
        for r in todo:
            groups[r.domain].append(r)
        limit = max(1, self.s.getleads_per_domain)
        for domain, grp in groups.items():
            if "getleads_dm" in self.blocked:
                break
            if not self.budgets["getleads"].take(limit):
                for r in grp:
                    r.set_lane("getleads_dm", "absent", reason="budget_reached")
                    self._touch(r)
                continue
            try:
                contacts = self.getleads.decision_makers(domain, limit, row_id=grp[0].row_id)
            except (GetLeadsAuthError, NetworkBlocked) as exc:
                self.budgets["getleads"].refund(limit)
                self._block("getleads_dm", str(exc))
                break
            except GetLeadsError as exc:
                self.budgets["getleads"].refund(limit)
                for r in grp:
                    r.set_lane("getleads_dm", "error", reason=str(exc)[:200])
                    self._touch(r)
                continue
            self.budgets["getleads"].refund(max(0, limit - len(contacts)))
            best = None
            people = []
            for c in contacts:
                name = person_from_fields(c.get("first"), c.get("last"), c.get("full"), self.cfg)
                if not name:
                    continue
                rank = self.cfg.lex.title_rank(c.get("title"))
                p = _person_dict(name, source="getleads", title=c.get("title"), rank=rank, url=c.get("linkedin"),
                                 quote=f"{c.get('title') or ''} (GetLeads decision-makers)".strip(), method="decision_makers")
                p["email"] = c.get("email")
                people.append(p)
                if rank is not None and rank <= self.cfg.lex.founder_max_rank and c.get("email"):
                    edom = registrable(split_email(c["email"])[1], self.cfg.lex) if valid_email(c["email"]) else None
                    if edom == domain and (best is None or rank < best["rank"]):
                        best = p
            for r in grp:
                r.people.extend(people)
                if best:
                    r.target = {k: v for k, v in best.items() if k != "email"}
                    r.found_emails.append({"email": best["email"], "source": "getleads", "url": best.get("url"),
                                           "quote": best.get("quote"), "mv": None, "stage": "decision_makers"})
                r.set_lane("getleads_dm", "done", records=len(contacts),
                           outcome="founder-level contact found" if best else "no founder-level contact")
                self._touch(r)

    # ------------------------------------------------------------------ 3 website
    def wave_website(self, rows: list[Row]) -> None:
        todo = [r for r in self._open(rows) if r.domain and not r.done("website")]
        domains = sorted({r.domain for r in todo if r.domain not in self.sites})
        if "website" in self.blocked:
            domains = []
        companies = {r.domain: r.input.get("company") for r in todo}
        first_row = {}
        for r in todo:
            first_row.setdefault(r.domain, r.row_id)

        def work(domain: str):
            if "website" in self.blocked:
                return domain, None
            try:
                res = self.harvester.harvest(domain, company=companies.get(domain), row_id=first_row.get(domain))
            except Exception as exc:  # one broken site must never stop the run
                res = SiteResult(domain=domain, bucket="dead", reason=f"error: {type(exc).__name__}")
            if res.network_blocked:
                self._block("website", f"this environment's network blocks outside websites (first seen on {domain})")
            return domain, res

        if domains:
            self.log(f"  website: reading {len(domains)} site(s), {self.s.concurrency} at a time")
            with ThreadPoolExecutor(max_workers=max(1, self.s.concurrency)) as pool:
                for i, (domain, res) in enumerate(pool.map(work, domains), 1):
                    if res is not None and not res.network_blocked:
                        self.sites[domain] = res.to_dict()
                    if i % 50 == 0:
                        self.log(f"    {i}/{len(domains)} sites")
        for r in todo:
            site = self.sites.get(r.domain)
            if site is None:
                if "website" in self.blocked:
                    r.set_lane("website", "absent", reason="network_blocked")
                else:
                    r.set_lane("website", "absent", reason="offline: site not in cache" if self.s.offline else "not read")
            else:
                status = "done" if site.get("bucket") else "absent"
                r.set_lane("website", status, bucket=site.get("bucket"), reason=site.get("reason"),
                           pages=len(site.get("pages", [])), site=site)
            self._touch(r)

    def wave_people_from_site(self, rows: list[Row]) -> None:
        cfg = self.cfg
        for row in self._open(rows):
            site = self.sites.get(row.domain or "")
            if not site or row.done("site_people"):
                continue
            site_people = [dict(p, source="website") for p in site.get("people", [])]
            founders = [p for p in site_people if p.get("rank") is not None and p["rank"] <= cfg.lex.founder_max_rank]
            kind_order = {"about": 0, "team": 1, "home": 2, "contact": 3}
            founders.sort(key=lambda p: (p["rank"], 0 if p.get("last") else 1, kind_order.get(p.get("page_kind"), 5)))
            for p in site_people:
                if not any(q.get("full") == p.get("full") and q.get("source") == "website" for q in row.people):
                    row.people.append(p)
            t = row.target
            if not t and founders:
                row.target = {k: founders[0].get(k) for k in ("first", "last", "middle", "full", "title", "rank",
                                                              "source", "url", "quote", "method")}
            elif t and not t.get("last"):
                tname = _target_name(t)
                match = next((p for p in site_people if p.get("last") and same_person(
                    PersonName(first=p["first"], last=p["last"]), tname, cfg)), None)
                if match:
                    t.update({"last": match["last"], "full": f"{t['first']} {match['last']}",
                              "last_source": "website", "url": match.get("url"), "quote": match.get("quote")})
                    if not t.get("title") and match.get("title"):
                        t.update({"title": match["title"], "rank": match.get("rank")})
            # Addresses at the domain: the target's own, pattern evidence, or an inbox.
            target = _target_name(row.target)
            persons = [PersonName(first=p["first"], last=p.get("last"), middle=p.get("middle"))
                       for p in site_people if p.get("last")]
            for e in site.get("emails", []):
                email = e["email"]
                local, edom = split_email(email)
                if registrable(edom, cfg.lex) != row.domain:
                    if is_freemail(edom, cfg.lex) and target and target.first_token in local:
                        if not any(f["email"] == email for f in row.found_emails):
                            row.found_emails.append({"email": email, "source": "website", "url": e.get("url"),
                                                     "quote": e.get("context"), "mv": None, "note": "personal mailbox"})
                    continue
                if local in cfg.lex.role_inboxes:
                    row.business_email = row.business_email or email
                    continue
                if target and matching_patterns(local, target, cfg):
                    if not any(f["email"] == email for f in row.found_emails):
                        row.found_emails.append({"email": email, "source": "website", "url": e.get("url"),
                                                 "quote": e.get("context"), "mv": None})
                for ev in evidence_from_address(email, persons, cfg, source="website", url=e.get("url")):
                    self._add_evidence(row, ev.to_dict())
            row.set_lane("site_people", "done", people=len(site_people), founders=len(founders))
            self._touch(row)

    def _add_evidence(self, row: Row, ev: dict) -> None:
        if not any(x["pattern"] == ev["pattern"] and x["email"] == ev["email"] for x in row.evidence):
            row.evidence.append(ev)

    # ------------------------------------------------------------------ 5 AI read
    def wave_ai_read(self, rows: list[Row]) -> str:
        todo = []
        for r in self._open(rows):
            t = r.target or {}
            needs = not t or not t.get("last")
            site = self.sites.get(r.domain or "")
            if not needs or r.done("ai_read") or not site or site.get("bucket") not in ("email_in_html", "server_rendered"):
                continue
            todo.append((r, site))
        if not todo:
            return "done"
        if not self.s.ai_read:
            for r, _ in todo:
                r.set_lane("ai_read", "absent", reason="lane_off: --ai-read off")
                self._touch(r)
            return "done"
        answers = ai_read.read_answers(self.run.ai_dir)
        answered_batches = ai_read.rows_in_answered_batches(self.run.ai_dir)
        waiting = []
        for r, site in todo:
            entry = ai_read.entry_for(r, site, self.cfg)
            if entry is None:
                r.set_lane("ai_read", "absent", reason="no readable page text")
                self._touch(r)
                continue
            ans = answers.get(r.row_id)
            if ans is None and r.row_id in answered_batches:
                # The reader answered its batch but skipped this row: don't wait forever.
                r.set_lane("ai_read", "done", outcome="not found: the page reader gave no answer for this row")
                self._touch(r)
                continue
            if ans is None:
                waiting.append(entry)
                continue
            person, why = ai_read.check_answer(ans, entry, self.cfg)
            if person:
                if not r.target:
                    r.target = person
                else:
                    r.target.update({"last": person["last"], "full": person["full"], "last_source": "ai_read",
                                     "url": person["url"], "quote": person["quote"]})
                r.people.append(person)
            r.set_lane("ai_read", "done", outcome=why)
            self._touch(r)
        if waiting:
            ai_read.write_batches(self.run.ai_dir, waiting)
            for r, _ in todo:
                if r.row_id in {e["row_id"] for e in waiting}:
                    r.set_lane("ai_read", "queued", reason="waiting for the page reader")
                    self._touch(r)
            return "pending"
        return "done"

    # ------------------------------------------------------------------ 6 Serper
    def wave_serper_person(self, rows: list[Row]) -> None:
        todo = [r for r in self._open(rows)
                if r.domain and (not r.target or not r.target.get("last")) and not r.done("serper_person")]
        if not todo:
            return
        off = self._lane_off("serper_person", self.serper is not None)
        if off:
            for r in todo:
                r.set_lane("serper_person", "absent", reason=off)
                self._touch(r)
            return
        self._by_domain(todo, self._serper_person_group)

    def _serper_person_group(self, rows: list[Row]) -> None:
        qcfg = self.cfg.serp
        num = int(qcfg.get("results_per_search", 10))
        for row in rows:
            t = row.target or {}
            want_first = t.get("first") if t and not t.get("last") else None
            templates = qcfg.get("last_name", []) if want_first else (qcfg.get("person", {}) or {}).get(row.segment, [])
            # No company name in the file: the domain's first label still finds the business.
            values = {"company": row.input.get("company") or (row.domain or "").split(".")[0], "domain": row.domain or "",
                      "city": row.input.get("city") or "", "state": row.input.get("state") or "",
                      "first": want_first or ""}
            searches, found, reason = 0, None, None
            for tpl in templates:
                q = fill_query(tpl, values)
                if not q:
                    continue
                if "serper_person" in self.blocked:
                    reason = self.blocked["serper_person"]
                    break
                if not self.budgets["serper"].take(1):
                    reason = "budget_reached"
                    break
                try:
                    data = self.serper.search(q, num=num, row_id=row.row_id)
                except (SerperAuthError, NetworkBlocked) as exc:
                    self.budgets["serper"].refund(1)
                    self._block("serper_person", str(exc))
                    self._block("serper_pattern", str(exc))
                    reason = str(exc)
                    break
                except SerperError as exc:
                    reason = str(exc)[:200]
                    continue
                searches += 1
                cands = people_from_results(data, company=row.input.get("company"), domain=row.domain,
                                            cfg=self.cfg, want_first=want_first)
                if not want_first:
                    cands = [c for c in cands if c.get("rank") is not None and c["rank"] <= self.cfg.lex.founder_max_rank]
                if cands:
                    found = cands[0]
                    break
            if found:
                row.people.append(found)
                if want_first:
                    t.update({"last": found["last"], "full": f"{t['first']} {found['last']}", "last_source": "serper",
                              "url": found.get("url"), "quote": found.get("quote")})
                else:
                    row.target = {k: found.get(k) for k in ("first", "last", "middle", "full", "title", "rank",
                                                            "source", "url", "quote", "method")}
            status = "done" if searches or found else "absent"
            row.set_lane("serper_person", status, searches=searches, outcome="found" if found else "not found",
                         reason=reason if not found else None)
            self._touch(row)

    def wave_serper_pattern(self, rows: list[Row]) -> None:
        todo = [r for r in self._open(rows) if r.domain and r.target and r.target.get("first")
                and not any(e.get("strength") == "proven" for e in r.evidence)
                and not self.memo.verified_patterns(r.domain) and not r.done("serper_pattern")
                and self.memo.status(r.domain) != "catch_all"]
        if not todo:
            return
        off = self._lane_off("serper_pattern", self.serper is not None)
        if off:
            for r in todo:
                r.set_lane("serper_pattern", "absent", reason=off)
                self._touch(r)
            return
        groups: dict[str, list[Row]] = defaultdict(list)
        for r in todo:
            groups[r.domain].append(r)
        tpls = self.cfg.serp.get("pattern", [])
        for domain, grp in groups.items():
            q = fill_query(tpls[0], {"domain": domain}) if tpls else None
            if not q or "serper_pattern" in self.blocked:
                continue
            if not self.budgets["serper"].take(1):
                for r in grp:
                    r.set_lane("serper_pattern", "absent", reason="budget_reached")
                    self._touch(r)
                continue
            try:
                data = self.serper.search(q, row_id=grp[0].row_id)
            except (SerperAuthError, NetworkBlocked) as exc:
                self.budgets["serper"].refund(1)
                self._block("serper_pattern", str(exc))
                continue
            except SerperError as exc:
                for r in grp:
                    r.set_lane("serper_pattern", "error", reason=str(exc)[:200])
                    self._touch(r)
                continue
            hits = emails_from_results(data, domain)
            for r in grp:
                target = _target_name(r.target)
                site_people = [PersonName(first=p["first"], last=p.get("last")) for p in r.people if p.get("last")]
                for email, text, url in hits:
                    local = split_email(email)[0]
                    if local in self.cfg.lex.role_inboxes:
                        continue
                    if target and matching_patterns(local, target, self.cfg):
                        if not any(f["email"] == email for f in r.found_emails):
                            r.found_emails.append({"email": email, "source": "serper", "url": url, "quote": text, "mv": None})
                        continue
                    from .extract import find_people
                    nearby = [p.name() for p in find_people(text, "", url, company=r.input.get("company"), cfg=self.cfg)]
                    for ev in evidence_from_address(email, nearby + site_people, self.cfg, source="serper", url=url):
                        self._add_evidence(r, ev.to_dict())
                r.set_lane("serper_pattern", "done", addresses=len(hits))
                self._touch(r)

    # ------------------------------------------------------------------ 7 guess + verify
    def wave_guess(self, rows: list[Row]) -> None:
        for row in self._open(rows):
            if row.guesses or not row.domain or not (row.target or {}).get("first"):
                continue
            if row.lane("mx").get("result") in ("no_mx", "no_domain"):
                continue
            name = _target_name(row.target)
            gs = build_guesses(name, row.domain, segment=row.segment, cfg=self.cfg, evidence=row.evidence,
                               max_guesses=self.s.max_guesses, verified_patterns=self.memo.verified_patterns(row.domain))
            checked = {f["email"]: f.get("mv") for f in row.found_emails if f.get("mv")}
            row.guesses = []
            for g in gs:
                d = g.to_dict()
                if d["email"] in checked:
                    d["mv"] = checked[d["email"]]
                row.guesses.append(d)
            row.set_lane("guess", "done", n=len(row.guesses))
            self._touch(row)

    def wave_verify_guesses(self, rows: list[Row]) -> None:
        todo = [r for r in self._open(rows) if r.guesses and r.verify.get("status") in (None, "budget_reached", "waiting")]
        if not todo:
            return
        if self.s.verify_mode == "manual":
            for r in todo:
                r.verify = {"status": "waiting", "detail": "manual mode: export the guesses with export_guesses.py"}
                self._touch(r)
            return
        off = self._lane_off("verify", self.mv is not None)
        if off:
            for r in todo:
                r.verify = {"status": "not_checked", "detail": off}
                self._touch(r)
            return
        self._by_domain(todo, self._verify_guess_group)

    def _verify_guess_group(self, rows: list[Row]) -> None:
        for row in rows:
            if "verify" in self.blocked:
                row.verify = {"status": "not_checked", "detail": self.blocked["verify"]}
                self._touch(row)
                continue
            vp = self.memo.verified_patterns(row.domain)
            if vp:  # someone else at this domain verified: their pattern goes first
                row.guesses.sort(key=lambda g: (0 if g.get("mv") else 1, 0 if g["pattern"] in vp else 1, g["rank"]))
            try:
                out = waterfall(row.guesses, domain=row.domain, mv=self.mv, budget=self.budgets["mv"],
                                memo=self.memo, row_id=row.row_id, run_unknown=self.run_unknown,
                                timeout_s=self.s.mv_timeout)
            except (MvAuthError, MvNoCredits) as exc:
                self._block("verify", str(exc))
                row.verify = {"status": "not_checked", "detail": str(exc)}
                self._touch(row)
                continue
            except NetworkBlocked as exc:
                self._block("verify", f"network_blocked: {exc}")
                row.verify = {"status": "not_checked", "detail": str(exc)}
                self._touch(row)
                continue
            row.verify = out.to_dict()
            self._touch(row)

    def wave_retry_unknown(self, rows: list[Row]) -> None:
        if self.s.verify_mode != "api" or self.mv is None or "verify" in self.blocked:
            return
        groups: dict[str, list[Row]] = defaultdict(list)
        for r in self._open(rows):
            if r.verify.get("status") == "unknown" and not r.verify.get("retried"):
                groups[r.domain].append(r)
        for domain, grp in groups.items():
            first = grp[0]
            g = next((g for g in first.guesses if g["email"] == first.verify.get("email")), None)
            if g is None or not self.budgets["mv"].take(1):
                continue
            try:
                ans = self.mv.verify(g["email"], timeout_s=60, row_id=first.row_id)
            except (MvError, NetworkBlocked) as exc:
                self._block("verify", str(exc))
                return
            g["mv"] = ans.to_dict()
            if ans.result == "unknown":
                for r in grp:
                    r.verify = {**r.verify, "retried": True, "detail": "still unknown after a retry with a longer timeout"}
                    self._touch(r)
                continue
            self.run_unknown.discard(domain)
            for r in grp:
                r.verify = {}
                for gg in r.guesses:
                    if (gg.get("mv") or {}).get("result") == "unknown":
                        gg["mv"] = ans.to_dict() if gg["email"] == g["email"] else None
                out = waterfall(r.guesses, domain=domain, mv=self.mv, budget=self.budgets["mv"], memo=self.memo,
                                row_id=r.row_id, run_unknown=self.run_unknown, timeout_s=self.s.mv_timeout)
                r.verify = {**out.to_dict(), "retried": True}
                self._touch(r)

    # ------------------------------------------------------------------ finalize
    def wave_finalize(self, rows: list[Row]) -> None:
        from .ledger import summarize
        from .report import finalize_all
        costs = summarize(self.ledger.events() if self.ledger else [])["by_row"]
        finalize_all(rows, self.cfg, self.memo, costs)
        for r in rows:
            self._touch(r)
