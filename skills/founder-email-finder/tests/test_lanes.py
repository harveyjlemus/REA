import json

import pytest

from lib.budget import Budget
from lib.cache import DomainMemo, PageCache
from lib.getleads import GetLeadsAuthError, GetLeadsClient
from lib.http import FakeTransport, NetworkBlocked, Response, redact
from lib.ledger import Ledger, summarize
from lib.millionverifier import MillionVerifierClient, MvAuthError, read_report
from lib.mx import DnsUnavailable, DohDns, MxChecker, provider_for
from lib.serper import fill_query, mentions_company, parse_linkedin_title, people_from_results
from lib.verify import from_report, waterfall


class LyingDns:
    method = "dns"

    def mx(self, domain):
        raise DnsUnavailable("system DNS failed the canary lookup (NXDOMAIN)")


def doh_transport(answers):
    def handler(method, url, payload):
        name = url.split("name=")[1].split("&")[0]
        if name in answers:
            return Response(200, url, text=json.dumps({"Status": 0, "Answer": [{"type": 15, "data": f"10 {answers[name]}."}]}))
        return Response(200, url, text=json.dumps({"Status": 3}))
    return FakeTransport({"https://dns.google/resolve": handler})


def test_mx_falls_back_from_lying_dns_to_doh():
    t = doh_transport({"gmail.com": "gmail-smtp-in.l.google.com", "acme.test": "aspmx.l.google.com"})
    mx = MxChecker(backends=[LyingDns(), DohDns(t)])
    r = mx.check("acme.test")
    assert (r.status, r.provider, r.method) == ("has_mx", "google", "doh")
    assert mx.check("missing.test").status == "no_domain"


def test_mx_blocked_everywhere_is_network_blocked():
    mx = MxChecker(backends=[LyingDns(), DohDns(FakeTransport(unrouted="blocked"))])
    assert mx.check("acme.test").status == "network_blocked"


def test_provider_for():
    assert provider_for(["acme-test.mail.protection.outlook.com"]) == "microsoft"
    assert provider_for(["mx1.secureserver.net"]) == "godaddy"
    assert provider_for([]) == "none"


def test_getleads_enrich_and_decision_makers(tmp_path):
    def enrich(method, url, payload):
        return Response(200, url, text=json.dumps({"ok": True, "results": [
            {**payload["items"][0], "success": True, "email": "ann@lee.test", "profileUrl": "li", "data": None},
            {**payload["items"][1], "success": False, "email": None, "data": None}]}))
    t = FakeTransport({"https://app.getleads.io/api/v1/enrich/from-person": enrich,
                       "https://app.getleads.io/api/v1/contacts/lookup/decision-makers":
                           {"contacts": [{"first_name": "Ann", "last_name": "Lee", "current_title": "Founder",
                                          "work_email": "ann@lee.test"}]}})
    ledger = Ledger(tmp_path / "l.jsonl")
    gl = GetLeadsClient("k", t, ledger=ledger, min_interval=0, retry_wait=0)
    out = gl.enrich_from_person([{"first_name": "Ann", "last_name": "Lee", "email_domain": "lee.test"},
                                 {"first_name": "Bo", "last_name": "Ray", "email_domain": "ray.test"}], ["r1", "r2"])
    assert out[0]["email"] == "ann@lee.test" and out[0]["success"] and not out[1]["success"]
    assert summarize(ledger.events())["by_provider"]["getleads"]["units"] == 1
    dm = gl.decision_makers("lee.test", 5)
    assert dm[0]["title"] == "Founder" and dm[0]["email"] == "ann@lee.test"
    bad = GetLeadsClient("k", FakeTransport({"https://app.getleads.io": Response(401, "u")}), min_interval=0, retry_wait=0)
    with pytest.raises(GetLeadsAuthError):
        bad.decision_makers("x.test", 1)


def test_serper_parsing(cfg):
    assert parse_linkedin_title("Rosa Delgado - Owner - Old Mill Bakery | LinkedIn") == ("Rosa Delgado", "Owner", "Old Mill Bakery")
    assert parse_linkedin_title("Rosa Delgado - Owner at Old Mill Bakery | LinkedIn")[1:] == ("Owner", "Old Mill Bakery")
    assert mentions_company("Owner at Old Mill Bakery LLC", "Old Mill Bakery", "oldmill.test")
    assert not mentions_company("Owner at New Day Cafe", "Old Mill Bakery", "oldmill.test")
    assert fill_query('"{company}" owner {city} {state}', {"company": "Acme"}) == '"Acme" owner'
    assert fill_query('site:linkedin.com/in "{first}" "{company}"', {"company": "Acme"}) is None
    data = {"organic": [
        {"title": "Rosa Delgado - Owner - Old Mill Bakery | LinkedIn", "link": "https://www.linkedin.com/in/rd", "snippet": ""},
        {"title": "Jim Hall - Owner - Other Bakery | LinkedIn", "link": "https://www.linkedin.com/in/jh", "snippet": ""}]}
    got = people_from_results(data, company="Old Mill Bakery", domain="oldmill.test", cfg=cfg)
    assert [c["full"] for c in got] == ["Rosa Delgado"]


def test_mv_client_reads_results_and_catches_bad_keys(tmp_path):
    def mv(method, url, payload):
        if "api=good" not in url:
            return Response(200, url, text=json.dumps({"error": "Apikey not found"}))
        return Response(200, url, text=json.dumps({"email": "a@b.test", "result": "ok", "quality": "good", "credits": 9}))
    t = FakeTransport({"https://api.millionverifier.com/api/v3/": mv})
    assert MillionVerifierClient("good", t, retry_waits=(0,)).verify("a@b.test").result == "ok"
    with pytest.raises(MvAuthError):
        MillionVerifierClient("bad", t, retry_waits=(0,)).verify("a@b.test")


def test_read_report(tmp_path):
    p = tmp_path / "r.csv"
    p.write_text("email,fef_row_id,quality,result,free,role\nann@lee.test,r1,good,ok,no,no\nx@y.test,r2,risky,catch_all,no,no\n")
    rep = read_report(p)
    assert rep["ann@lee.test"].result == "ok" and rep["x@y.test"].result == "catch_all"


class FakeMv:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def verify(self, email, timeout_s=20, row_id=None):
        from lib.millionverifier import MvResult
        self.calls.append(email)
        return MvResult(email=email, result=self.answers.get(email, "invalid"))


def guesses(*emails):
    return [{"email": e, "pattern": f"p{i}", "rank": i + 1, "mv": None} for i, e in enumerate(emails)]


def test_waterfall_stops_at_first_ok(tmp_path):
    mv = FakeMv({"b@x.test": "ok"})
    memo = DomainMemo(tmp_path / "m.json")
    out = waterfall(guesses("a@x.test", "b@x.test", "c@x.test"), domain="x.test", mv=mv, budget=Budget("mv", 10),
                    memo=memo, row_id="r1")
    assert (out.status, out.email, out.checks) == ("verified", "b@x.test", 2)
    assert mv.calls == ["a@x.test", "b@x.test"]
    assert memo.verified_patterns("x.test") == ["p1"]


def test_catch_all_stops_the_domain_and_is_remembered(tmp_path):
    memo = DomainMemo(tmp_path / "m.json")
    out = waterfall(guesses("a@x.test", "b@x.test"), domain="x.test", mv=FakeMv({"a@x.test": "catch_all"}),
                    budget=Budget("mv", 10), memo=memo, row_id="r1")
    assert (out.status, out.email) == ("catch_all", "a@x.test")
    again = FakeMv({})
    out2 = waterfall(guesses("z@x.test"), domain="x.test", mv=again, budget=Budget("mv", 10),
                     memo=DomainMemo(tmp_path / "m.json"), row_id="r2")
    assert out2.status == "catch_all" and again.calls == []


def test_budget_and_resume():
    b = Budget("mv", 1)
    gs = guesses("a@x.test", "b@x.test")
    out = waterfall(gs, domain="x.test", mv=FakeMv({}), budget=b, memo=None, row_id="r1")
    assert out.status == "budget_reached" and gs[0]["mv"]["result"] == "invalid"
    mv = FakeMv({"b@x.test": "ok"})
    out = waterfall(gs, domain="x.test", mv=mv, budget=Budget("mv", 5), memo=None, row_id="r1")
    assert out.status == "verified" and mv.calls == ["b@x.test"], "answered guesses are not re-checked"


def test_from_report_waits_for_the_next_round():
    gs = guesses("a@x.test", "b@x.test")
    out = from_report(gs, {"a@x.test": {"email": "a@x.test", "result": "invalid"}}, domain="x.test", memo=None)
    assert (out.status, out.email) == ("waiting", "b@x.test")


def test_ledger_never_turns_unknown_into_zero(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    led.log(row_id="r1", provider="millionverifier", action="verify", basis="unknown")
    led.log(row_id="r1", provider="serper", action="search", usd=0.001, basis="imputed")
    with pytest.raises(ValueError):
        led.log(row_id="r1", provider="serper", action="search", basis="imputed")
    s = summarize(led.events())
    assert s["by_provider"]["millionverifier"]["unknown_usd_calls"] == 1
    assert s["by_row"]["r1"]["usd"] == pytest.approx(0.001)


def test_page_cache_and_memo(tmp_path):
    c = PageCache(tmp_path / "p")
    c.put("https://a.test/", status=200, final_url="https://a.test/", html="<p>x</p>", text="x")
    assert c.get("https://a.test/")["text"] == "x"
    m = DomainMemo(tmp_path / "m.json", ttl_days=0)
    m.set_status("a.test", "catch_all")
    assert DomainMemo(tmp_path / "m.json", ttl_days=30).status("a.test") == "catch_all"


def test_redact_hides_keys():
    assert "secret" not in redact("https://api.millionverifier.com/api/v3/?api=secret&email=a@b.test")


def test_fake_transport_blocked_mode():
    with pytest.raises(NetworkBlocked):
        FakeTransport(unrouted="blocked").get("https://x.test/")
