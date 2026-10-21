from dataclasses import replace

from lib.guesses import build_guesses
from lib.names import person_from_fields
from lib.patterns import matching_patterns, render, shape_patterns


def test_render_needs_every_piece():
    assert render("{f}{last}", "mike", "rivera") == "mrivera"
    assert render("{first}.{l}", "mike", "rivera") == "mike.r"
    assert render("{f}{last}", "mike", None) is None


def test_matching_patterns_uses_nicknames_and_compound_last_names(cfg):
    p = person_from_fields("Greg", "Van Horn", None, cfg)
    assert matching_patterns("gvanhorn", p, cfg) == ["flast"]
    assert matching_patterns("ghorn", p, cfg) == ["flast"]
    assert "first" in matching_patterns("mike", person_from_fields("Michael", "Rivera", None, cfg), cfg)


def test_shape_patterns_are_hints(cfg):
    assert shape_patterns("jsmith", cfg)[0][0] == "flast"
    assert shape_patterns("info", cfg) == []
    assert shape_patterns("mary.jones", cfg)[0][0] == "first.last"


def test_proven_pattern_goes_first(cfg):
    p = person_from_fields("Mike", "Rivera", None, cfg)
    ev = [{"pattern": "first.last", "strength": "proven", "email": "jo.doe@acme.test", "person": "Jo Doe"}]
    gs = build_guesses(p, "acme.test", segment="local", cfg=cfg, evidence=ev)
    assert gs[0].email == "mike.rivera@acme.test"
    assert "proven by jo.doe@acme.test" in gs[0].why
    assert len(gs) <= cfg.max_guesses and len({g.email for g in gs}) == len(gs)


def test_verified_pattern_beats_evidence(cfg):
    p = person_from_fields("Mike", "Rivera", None, cfg)
    gs = build_guesses(p, "acme.test", segment="local", cfg=cfg, verified_patterns=["firstl"])
    assert gs[0].email == "miker@acme.test"


def test_first_name_only_rows_get_first_name_guesses(cfg):
    gs = build_guesses(person_from_fields("Michael", None, None, cfg), "acme.test", segment="local", cfg=cfg)
    assert [g.email for g in gs][:2] == ["michael@acme.test", "mike@acme.test"]
    assert all("." not in g.email.split("@")[0] for g in gs)


def test_nickname_guess_lands_at_its_rank(cfg):
    gs = build_guesses(person_from_fields("Michael", "Rivera", None, cfg), "acme.test", segment="local", cfg=cfg)
    assert gs[cfg.nickname_guess_rank - 1].email == "mike@acme.test"


def test_uncalibrated_segment_uses_starting_order(cfg):
    plain = replace(cfg, calibrated=None)
    gs = build_guesses(person_from_fields("Sam", "Patel", None, plain), "orbitly.test", segment="startup", cfg=plain)
    assert [g.pattern for g in gs][:3] == plain.starting_order[:3]
    assert gs[0].why == "starting order"


def test_calibrated_order_is_used_when_present(cfg):
    if not cfg.calibrated_order("local"):
        return
    order = cfg.calibrated_order("local")[0]
    gs = build_guesses(person_from_fields("Ann", "Lee", None, cfg), "lee.test", segment="local", cfg=cfg)
    assert gs[0].pattern == order[0]
