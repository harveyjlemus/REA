from lib.domains import clean_host, domain_for_row, is_freemail, registrable
from lib.names import email_token, parse_full_name, person_from_fields, same_person


def test_parse_full_name_handles_titles_nicknames_and_suffixes(cfg):
    p = parse_full_name('Dr. Robert "Bob" Van Horn Jr., CPA', cfg)
    assert (p.first, p.last, p.nickname) == ("Robert", "Van Horn", "Bob")
    assert p.last_variants(cfg.lex.last_name_particles) == ["vanhorn", "horn"]


def test_parse_full_name_orders(cfg):
    assert parse_full_name("Smith, John", cfg).first == "John"
    p = parse_full_name("Juan Carlos Garcia Lopez", cfg)
    assert (p.first, p.middle, p.last) == ("Juan", "Carlos", "Garcia Lopez")


def test_email_token_folds_accents_and_punctuation():
    assert email_token("José") == "jose"
    assert email_token("O'Brien") == "obrien"
    assert email_token("Smith-Jones") == "smithjones"


def test_person_from_fields_drops_masked_last_names(cfg):
    p = person_from_fields("Camille", "S.", None, cfg)
    assert p.first == "Camille" and p.last is None
    assert person_from_fields("", "", "", cfg) is None
    assert person_from_fields("Mike Rivera", "", None, cfg).last == "Rivera"


def test_same_person_knows_nicknames(cfg):
    a = person_from_fields("Michael", "Rivera", None, cfg)
    b = person_from_fields("Mike", None, None, cfg)
    assert same_person(a, b, cfg)
    assert not same_person(a, person_from_fields("Mark", None, None, cfg), cfg)


def test_clean_host_and_registrable(cfg):
    assert clean_host("https://www.Acme-Roofing.com/about?utm=x") == "acme-roofing.com"
    assert clean_host("not a website") is None
    assert clean_host("192.168.1.10") is None
    assert registrable("quench.culligan.com", cfg.lex) == "culligan.com"
    assert registrable("shop.acme.co.uk", cfg.lex) == "acme.co.uk"


def test_domain_for_row(cfg):
    assert domain_for_row("facebook.com/acme", None, cfg.lex).reason == "website_is_social_or_directory"
    assert domain_for_row("acme.wixsite.com/acme", None, cfg.lex).domain is None
    franchise = domain_for_row("https://quench.culligan.com/local/boston/", None, cfg.lex)
    assert franchise.domain == "culligan.com" and "shared_domain_hint" in franchise.flags
    assert domain_for_row("", "mike@acmeroof.com", cfg.lex).domain == "acmeroof.com"
    assert domain_for_row("", "mike@gmail.com", cfg.lex).domain is None
    assert is_freemail("comcast.net", cfg.lex)
