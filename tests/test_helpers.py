"""Null-safety and the documented sentinels for the Tier 0 helpers (SPEC.md 6.1)."""

from __future__ import annotations

from types import SimpleNamespace

from kestrel.rules import helpers as h


def test_age_days_none_is_large_sentinel():
    assert h.age_days(None) == 999999
    assert h.age_days("not-a-date") == 999999


def test_seniority_rank_ladder_and_unknown():
    assert h.seniority_rank("VP of Information Technology") == 6
    assert h.seniority_rank("Chief Information Officer") == 8
    assert h.seniority_rank("Head of IT") == 4
    assert h.seniority_rank("Help Desk Technician") == 0
    assert h.seniority_rank("COO") == 8  # broadened c[a-z]o also catches COO/CMO/CRO/...
    assert h.seniority_rank("Chief Marketing Officer") == 8
    assert h.seniority_rank("Chief Vibes Curator") == -1  # unknown -> passes floors via the guard
    assert h.seniority_rank(None) == -1


def test_comp_helpers_null_safe():
    opp = SimpleNamespace(comp_max=None)
    assert h.comp_max_below(opp, 100000) is False
    assert h.comp_max_at_least(opp, 100000) is False
    opp2 = SimpleNamespace(comp_max=150000)
    assert h.comp_max_below(opp2, 200000) is True
    assert h.comp_max_at_least(opp2, 100000) is True


def test_value_in_case_insensitive_and_null_safe():
    assert h.value_in("Healthcare", ["healthcare", "finance"]) is True
    assert h.value_in(None, ["x"]) is False
    assert h.value_in("x", None) is False


def test_title_helpers_null_safe():
    opp = SimpleNamespace(title=None)
    assert h.title_contains_any(opp, ["vp"]) is False
    opp2 = SimpleNamespace(title="VP of IT")
    assert h.title_contains_any(opp2, ["vp of"]) is True
    assert h.title_equals_any(opp2, ["vp of it"]) is True


def test_trigger_int_below_null_safe():
    assert h.trigger_int_below(SimpleNamespace(trigger_data=None), "engagement_days", 20) is False
    opp = SimpleNamespace(trigger_data={"engagement_days": 10})
    assert h.trigger_int_below(opp, "engagement_days", 20) is True
    assert h.trigger_int_below(opp, "missing_key", 20) is False


def test_geo_allows_unknown_location_passes():
    rules = {"remote_country_allowlist": ["US"], "onsite_states": ["TX"], "onsite_metros": []}
    opp = SimpleNamespace(
        remote_type="unknown", country=None, state=None, city=None, location_raw=None
    )
    assert h._geo_allows(opp, rules) is True  # bias to false positives


def test_geo_remote_unknown_country_escalates_not_rejects():
    # hard rule 4: a remote role with an unparsed country must PASS, not be blocked.
    rules = {"remote_country_allowlist": ["US"], "onsite_states": ["TX"], "onsite_metros": []}
    unknown = SimpleNamespace(
        remote_type="remote", country=None, state=None, city=None, location_raw="Remote"
    )
    assert h._geo_allows(unknown, rules) is True
    # a remote role in a KNOWN, non-allowed country is a real negative and still blocks
    foreign = SimpleNamespace(
        remote_type="remote", country="IN", state=None, city=None, location_raw="Remote - India"
    )
    assert h._geo_allows(foreign, rules) is False


def test_geo_allows_matches_state_and_metro_alias():
    rules = {
        "remote_country_allowlist": ["US"],
        "onsite_states": ["TX"],
        "onsite_metros": [{"name": "Austin", "aliases": ["Round Rock"]}],
    }
    tx = SimpleNamespace(
        remote_type="onsite", country="US", state="TX", city="Waco", location_raw="Waco, TX"
    )
    assert h._geo_allows(tx, rules) is True
    metro = SimpleNamespace(
        remote_type="onsite", country="US", state="CA", city="Round Rock", location_raw="Round Rock"
    )
    assert h._geo_allows(metro, rules) is True
    elsewhere = SimpleNamespace(
        remote_type="onsite", country="US", state="NY", city="Albany", location_raw="Albany, NY"
    )
    assert h._geo_allows(elsewhere, rules) is False


def test_a_named_place_with_no_remote_wording_is_on_site_there():
    """Owner ruling D-RULES-9: "North Carolina" with no remote or hybrid wording is on-site in
    North Carolina, so a profile that lists only Texas sets it aside; vague or remote ones stay."""
    from types import SimpleNamespace

    from kestrel.rules.helpers import geo_evaluate

    rules = {"remote_country_allowlist": ["US"], "onsite_states": ["TX"], "onsite_metros": []}

    def opp(**kw):
        base = {"remote_type": "unknown", "country": "US", "state": None, "city": None,
                "location_raw": None, "title": "Senior Manager", "description_md": "Lead a team."}
        base.update(kw)
        return SimpleNamespace(**base)

    assert geo_evaluate(opp(state="NC", location_raw="North Carolina"), rules) == (False, False)
    by_city = opp(city="North Carolina", location_raw="North Carolina")  # stored before D-RULES-9
    assert geo_evaluate(by_city, rules)[0] is False
    remote = opp(state="NC", location_raw="North Carolina", description_md="Fully remote role.")
    assert geo_evaluate(remote, rules) == (True, True)
    assert geo_evaluate(opp(city="Multiple Locations", location_raw="Multiple Locations"),
                        rules) == (True, True)
    assert geo_evaluate(opp(location_raw=None), rules) == (True, True)
    assert geo_evaluate(opp(state="TX", location_raw="Texas"), rules) == (True, False)
