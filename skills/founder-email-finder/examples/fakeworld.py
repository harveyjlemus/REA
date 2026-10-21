"""A fictional market for the offline demo and the tests.

Every company, person and domain here is made up (.test is a reserved
top-level domain). The FakeTransport answers for their websites, DNS,
GetLeads, Serper and Million Verifier, so the whole pipeline runs with no
network and no spend. Each company is built to exercise one path."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from lib.http import FakeTransport, Response  # noqa: E402

HEADER = ["company", "website", "first_name", "last_name", "title", "email", "city", "state", "industry", "closed"]

# (row, expected find_status, expected founder_email or None, what it exercises)
ROWS = [
    (["Northwind Roofing", "https://www.northwindroofing.test/", "", "", "", "", "Salem", "MA", "Roofing", ""],
     "found", "cmendes@northwindroofing.test", "company only: owner on /about, a staff address proves the pattern"),
    (["Blue Peak HVAC", "bluepeakhvac.test", "", "", "", "", "Lowell", "MA", "HVAC", ""],
     "risky", "victor@bluepeakhvac.test", "company only: catch-all domain, best guess reported as risky"),
    (["Lumen Pest Control", "lumenpest.test", "Priya", "Natarajan", "Owner", "", "Worcester", "MA", "Pest control", ""],
     "found", "priya@lumenpest.test", "full name: GetLeads finds it, Million Verifier confirms"),
    (["Keystone Landscape", "keystonelandscape.test", "Dana", "", "", "", "Nashua", "NH", "Landscaping", ""],
     "found", "dwhitfield@keystonelandscape.test", "first name only: last name from /team, second guess verifies"),
    (["Copperline Plumbing", "copperlineplumbing.test", "", "", "", "", "Quincy", "MA", "Plumbing", ""],
     "found", "marcus@copperlineplumbing.test", "JavaScript-only site: GetLeads decision-makers names the founder"),
    (["Old Mill Bakery", "oldmillbakery.test", "", "", "", "", "Salem", "MA", "Bakery", ""],
     "found", "rosa@oldmillbakery.test", "dead site: Serper finds the owner's LinkedIn result"),
    (["Harbor Kayak Tours", "https://facebook.com/harborkayaktours", "", "", "", "", "Gloucester", "MA", "Tours", ""],
     "skipped", None, "website is a Facebook page: no domain to guess at"),
    (["Granite Auto Body", "graniteautobody.test", "", "", "", "", "Concord", "NH", "Auto body", "yes"],
     "skipped", None, "closed business: skipped for free"),
    (["Tidewater Electric", "tidewaterelectric.test", "Tom", "Ellison", "Owner", "tom@tidewaterelectric.test",
      "Newport", "RI", "Electrical", ""],
     "found", "tom@tidewaterelectric.test", "has an email already: verified, nothing guessed"),
    (["Sagebrush Design", "sagebrushdesign.test", "", "", "", "", "Portland", "ME", "Marketing agency", ""],
     "found", "elena@sagebrushdesign.test", "plain code finds nobody: the AI read names her, quote checked"),
    (["Summit Roof Pros", "summitroof.test", "", "", "", "", "Manchester", "NH", "Roofing", ""],
     "not_found", None, "bot wall and no other source: not found, with the reason"),
    (["Northwind Roofing", "northwindroofing.test", "", "", "", "", "Salem", "MA", "Roofing", ""],
     "duplicate", None, "same company twice: kept once"),
    (["Orbitly", "orbitly.test", "", "", "", "", "Boston", "MA", "Software", ""],
     "found", "sam.patel@orbitly.test", "startup: founder from JSON-LD, startup guess order"),
]

SITES = {
    "https://northwindroofing.test/": """<html><body><h1>Northwind Roofing</h1>
<nav><a href="/about-us">About Us</a> <a href="/contact">Contact</a> <a href="/gallery">Gallery</a></nav>
<p>Roof replacement, repair and gutters across the North Shore since 1998. Free estimates.</p></body></html>""",
    "https://northwindroofing.test/about-us": """<html><body><h2>Our Story</h2>
<p>Northwind Roofing was founded in 1998 by Carla Mendes.</p>
<h3>Team</h3><p>Jonah Price</p><p>Estimator</p>
<p>Questions about a quote? Email <a href="mailto:jprice@northwindroofing.test">jprice@northwindroofing.test</a></p>
</body></html>""",
    "https://northwindroofing.test/contact": """<html><body><p>Call us or write to
<a href="mailto:info@northwindroofing.test">info@northwindroofing.test</a></p></body></html>""",
    "https://bluepeakhvac.test/": """<html><body><h1>Blue Peak HVAC</h1><p>Heating and cooling for Lowell homes.
Licensed and insured. Owner: Victor Hale</p><p>Service area: Lowell, Chelmsford, Dracut, Tewksbury.</p></body></html>""",
    "https://keystonelandscape.test/": """<html><body><h1>Keystone Landscape</h1><a href="/our-team">Our Team</a>
<p>Design, planting, hardscapes and seasonal cleanups in southern New Hampshire.</p></body></html>""",
    "https://keystonelandscape.test/our-team": """<html><body><h2>Our Team</h2>
<p>Dana Whitfield</p><p>Owner</p><p>Luis Ortega</p><p>Crew Lead</p></body></html>""",
    "https://copperlineplumbing.test/": """<html><head><script src="/a.js"></script><script src="/b.js"></script>
<script src="/c.js"></script></head><body><div id="root"></div></body></html>""",
    "https://sagebrushdesign.test/": """<html><body><h1>Sagebrush Design</h1><a href="/about">About</a>
<p>Brand identity and websites for small businesses in Maine and beyond.</p></body></html>""",
    "https://sagebrushdesign.test/about": """<html><body><h2>About the studio</h2>
<p>Sagebrush Design is the studio of Elena Ruiz. Elena started the studio after ten years at a big agency in Boston.</p>
<p>We work with a handful of clients at a time so every project gets real attention.</p></body></html>""",
    "https://orbitly.test/": """<html><head><script type="application/ld+json">{"@context":"https://schema.org",
"@type":"Organization","name":"Orbitly","founder":{"@type":"Person","name":"Sam Patel"}}</script></head>
<body><h1>Orbitly</h1><p>Scheduling software for field teams. Questions: <a href="mailto:hello@orbitly.test">hello@orbitly.test</a></p>
<p>Plan routes, dispatch crews and get paid faster, all in one place.</p></body></html>""",
}
BOT_WALL = Response(status=403, url="https://summitroof.test/",
                    text="<html><head><title>Just a moment...</title></head><body>cf-chl challenge-platform</body></html>")

MX = {"northwindroofing.test": "aspmx.l.google.com", "bluepeakhvac.test": "bluepeakhvac-test.mail.protection.outlook.com",
      "lumenpest.test": "aspmx.l.google.com", "keystonelandscape.test": "mx1.secureserver.net",
      "copperlineplumbing.test": "aspmx.l.google.com", "oldmillbakery.test": "mx.zoho.com",
      "tidewaterelectric.test": "aspmx.l.google.com", "sagebrushdesign.test": "aspmx.l.google.com",
      "summitroof.test": "aspmx.l.google.com", "orbitly.test": "aspmx.l.google.com",
      "graniteautobody.test": "aspmx.l.google.com", "gmail.com": "gmail-smtp-in.l.google.com"}

MV_OK = {"cmendes@northwindroofing.test", "priya@lumenpest.test", "dwhitfield@keystonelandscape.test",
         "marcus@copperlineplumbing.test", "rosa@oldmillbakery.test", "tom@tidewaterelectric.test",
         "elena@sagebrushdesign.test", "sam.patel@orbitly.test", "jprice@northwindroofing.test"}
MV_CATCH_ALL_DOMAINS = {"bluepeakhvac.test"}

# What a good page reader writes for the one row plain code can't solve.
AI_ANSWERS = {"sagebrushdesign.test": {"found": True, "name": "Elena Ruiz", "title": "Founder",
                                       "url": "https://sagebrushdesign.test/about",
                                       "quote": "Sagebrush Design is the studio of Elena Ruiz."}}


def write_input(path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        for row, *_ in ROWS:
            w.writerow(row)


def _dns(method, url, payload):
    q = parse_qs(urlsplit(url).query)
    name = q.get("name", [""])[0]
    if name in MX:
        return Response(status=200, url=url, text=json.dumps({"Status": 0, "Answer": [{"type": 15, "data": f"10 {MX[name]}."}]}))
    return Response(status=200, url=url, text=json.dumps({"Status": 3}))


def _mv(method, url, payload):
    q = parse_qs(urlsplit(url).query)
    email = q.get("email", [""])[0].lower()
    domain = email.split("@")[-1]
    if q.get("api", [""])[0] != "demo-mv":
        return Response(status=200, url=url, text=json.dumps({"error": "Apikey not found"}))
    result = "catch_all" if domain in MV_CATCH_ALL_DOMAINS else ("ok" if email in MV_OK else "invalid")
    quality = {"ok": "good", "catch_all": "risky"}.get(result, "bad")
    return Response(status=200, url=url, text=json.dumps({"email": email, "result": result, "quality": quality,
                                                          "resultcode": {"ok": 1, "catch_all": 2}.get(result, 6),
                                                          "subresult": result, "free": False, "role": False,
                                                          "credits": 4999, "error": ""}))


def _getleads_enrich(method, url, payload):
    results = []
    for it in (payload or {}).get("items", []):
        hit = (it["first_name"], it["last_name"]) == ("Priya", "Natarajan")
        results.append({**it, "success": hit, "email": "priya@lumenpest.test" if hit else None,
                        "profileUrl": "https://www.linkedin.com/in/priya-natarajan-example" if hit else None,
                        "data": {"current_title": "Owner"} if hit else None})
    return Response(status=200, url=url, text=json.dumps({"ok": True, "results": results, "creditsRemaining": None}))


def _getleads_dm(method, url, payload):
    contacts = []
    if (payload or {}).get("domain") == "copperlineplumbing.test":
        contacts = [{"first_name": "Marcus", "last_name": "Bell", "current_title": "Founder & CEO",
                     "work_email": "marcus@copperlineplumbing.test", "email_status": "VALID",
                     "linkedin_url": "https://www.linkedin.com/in/marcus-bell-example",
                     "company_domain": "copperlineplumbing.test"}]
    return Response(status=200, url=url, text=json.dumps({"ok": True, "contacts": contacts}))


def _serper(method, url, payload):
    q = (payload or {}).get("q", "")
    if "Old Mill Bakery" in q:
        organic = [{"title": "Rosa Delgado - Owner - Old Mill Bakery | LinkedIn",
                    "link": "https://www.linkedin.com/in/rosa-delgado-example",
                    "snippet": "Owner at Old Mill Bakery. Salem, Massachusetts."}]
    else:
        organic = []
    return Response(status=200, url=url, text=json.dumps({"organic": organic}))


def transport() -> FakeTransport:
    t = FakeTransport()
    for url, html in SITES.items():
        t.add(url, Response(status=200, url=url, headers={"content-type": "text/html"}, text=html))
    t.add("https://www.northwindroofing.test/", Response(status=200, url="https://northwindroofing.test/",
                                                        headers={"content-type": "text/html"},
                                                        text=SITES["https://northwindroofing.test/"]))
    t.add("https://summitroof.test/", BOT_WALL)
    t.add("https://dns.google/resolve", _dns)
    t.add("https://api.millionverifier.com/api/v3/credits", {"credits": 5000})
    t.add("https://api.millionverifier.com/api/v3/", _mv)
    t.add("https://app.getleads.io/api/v1/enrich/from-person", _getleads_enrich)
    t.add("https://app.getleads.io/api/v1/contacts/lookup/decision-makers", _getleads_dm)
    t.add("https://app.getleads.io/api/v1/usage/fair-use", {"daily": {"remaining": 9000, "cap": 500000}})
    t.add("https://google.serper.dev/search", _serper)
    return t


def answer_ai_read(ai_dir: Path) -> int:
    """Play the page reader: answer every waiting batch (only Sagebrush is solvable)."""
    from lib import ai_read
    n = 0
    for batch in ai_read.pending_batches(ai_dir):
        lines = []
        for line in batch.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            ans = AI_ANSWERS.get(entry["domain"]) or {"found": False, "reason": "no owner named on these pages"}
            lines.append(json.dumps({"row_id": entry["row_id"], **ans}))
            n += 1
        (ai_dir / "answers" / batch.name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return n
