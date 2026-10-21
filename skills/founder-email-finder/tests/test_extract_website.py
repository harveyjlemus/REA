from lib.cache import PageCache
from lib.extract import decode_cfemail, find_emails, find_people, html_to_text
from lib.http import FakeTransport, Response
from lib.website import Harvester, classify, pick_subpages


def people(text, cfg, company="Acme Roofing"):
    return [(p.full, p.title, p.rank) for p in find_people(text, "", "u", company=company, cfg=cfg)]


def test_team_cards_pair_each_name_with_its_own_title(cfg):
    got = people("Our Team\nSarah O'Connor\nCo-Owner & President\nJohn Smith\nGeneral Manager\nLisa Park\nOffice Manager", cfg)
    assert ("Sarah O'Connor", "Co-Owner & President", 1) in got
    assert all(name != "Lisa Park" or rank is None or rank > 3 for name, _, rank in got)


def test_sentences_name_the_owner(cfg):
    assert people("Founded in 1998 by Dana Whitfield.", cfg)[0][:2] == ("Dana Whitfield", "Founder")
    assert people("Family owned and operated by Mike Rivera since 1998.", cfg)[0][:2] == ("Mike Rivera", "Owner")
    assert people("JOHN DOE, FOUNDER", cfg)[0][:2] == ("John Doe", "Founder")
    assert people("Hi! I'm Tony, the owner.", cfg)[0][:2] == ("Tony", "Owner")


def test_decoys_are_not_people(cfg):
    assert people("Free Estimates | Owner Operated | Licensed & Insured\nCall Today, Owner", cfg) == []
    assert people("Rivera Roofing, Owner: call now", cfg, company="Rivera Roofing") == []


def test_quotes_are_verbatim(cfg):
    text = "About\nNorthwind was founded in 1998 by Carla Mendes."
    p = find_people(text, "", "u", company="Northwind", cfg=cfg)[0]
    assert p.quote in text


def test_jsonld_founder(cfg):
    html = '<script type="application/ld+json">{"@type":"Organization","founder":{"@type":"Person","name":"Sam Patel"}}</script>'
    p = find_people("", html, "u", company="Orbitly", cfg=cfg)[0]
    assert (p.full, p.method) == ("Sam Patel", "jsonld")


def test_find_emails(cfg):
    html = ('<a href="mailto:mike@acme.test?subject=hi">x</a>'
            '<a href="/cdn-cgi/l/email-protection#c3aaada5ac83a2a0aea6b1acaca5aaada4eda0acae">y</a>')
    text = "sales [at] acme [dot] test · logo@2x.png · john.doe@example.com"
    got = {e.email: e.method for e in find_emails(html, text, "u", domain="acme.test")}
    assert got["mike@acme.test"] == "mailto"
    assert got["sales@acme.test"] == "deobfuscated"
    assert "logo@2x.png" not in got and "john.doe@example.com" not in got
    assert any(m == "cfemail" for m in got.values())
    assert decode_cfemail("00") == ""


def test_classify_buckets(cfg):
    page = "<html><body>" + "We fix roofs. " * 40 + "</body></html>"
    assert classify(200, page, html_to_text(page), None, cfg)[0] == "server_rendered"
    with_email = page.replace("We fix", "Mail mike@acme.test. We fix", 1)
    assert classify(200, with_email, html_to_text(with_email), None, cfg)[0] == "email_in_html"
    shell = '<script src="a.js"></script><script src="b.js"></script><script></script><div id="root"></div>'
    assert classify(200, shell, "", None, cfg)[0] == "js_shell"
    assert classify(403, "<title>Just a moment...</title>", "Just a moment...", None, cfg)[0] == "bot_walled"
    assert classify(200, "Buy this domain! hugedomains", "Buy this domain! hugedomains", None, cfg)[0] == "dead"
    assert classify(None, "", "", "dns", cfg)[0] == "dead"


def test_pick_subpages_prefers_about_and_team(cfg):
    html = '<a href="/gallery">Gallery</a><a href="/contact-us">Contact</a><a href="/about-us">About</a><a href="https://other.test/team">x</a>'
    picked = pick_subpages(html, "https://acme.test/", "acme.test", cfg, 4)
    assert [k for _, k in picked] == ["about", "contact"]
    assert all("other.test" not in u for u, _ in picked)


def test_harvest_reads_subpages_and_respects_robots(cfg, tmp_path):
    t = FakeTransport({
        "https://acme.test/robots.txt": Response(200, "https://acme.test/robots.txt", {"content-type": "text/plain"},
                                                 "User-agent: *\nDisallow: /private"),
        "https://acme.test/": '<a href="/about">About</a><p>' + "Roofing done right. " * 10 + "</p>",
        "https://acme.test/about": "<p>Acme was founded by Carla Mendes in 2001.</p>",
    })
    res = Harvester(t, PageCache(tmp_path), cfg).harvest("acme.test", company="Acme")
    assert res.bucket == "server_rendered" and len(res.pages) == 2
    assert res.people[0]["full"] == "Carla Mendes"
    blocked = FakeTransport({"https://nope.test/robots.txt": Response(200, "", {"content-type": "text/plain"},
                                                                       "User-agent: *\nDisallow: /"),
                             "https://nope.test/": "<p>hi</p>"})
    assert Harvester(blocked, PageCache(tmp_path / "b"), cfg).harvest("nope.test").bucket == "bot_walled"


def test_blocked_network_is_not_a_dead_site(cfg, tmp_path):
    t = FakeTransport(unrouted="blocked")
    res = Harvester(t, PageCache(tmp_path), cfg).harvest("acme.test")
    assert res.network_blocked and res.bucket is None
