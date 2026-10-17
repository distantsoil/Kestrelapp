"""Why a Tier 0 rule set a role aside (D-UI-73): one sentence per rule, in the operator's words,
with the concrete detail computed from the role and the profile, and a link to where that rule is
changed. Every shipped hard blocker of both tracks is covered, plus the fallback."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from kestrel import paths
from kestrel.config import get_settings
from kestrel.rules import helpers
from kestrel.web import fit

_EMP = NS(
    requires_sponsorship=True,
    employment_terms=NS(seniority_floor="vp", comp_floor_usd=150000,
                        anti_titles=["Help Desk", "Intern"]),
    geo_rules=NS(onsite_states=["OH"], onsite_metros=[]),
)
_CON = NS(consulting_terms=NS(day_rate_floor=1800, min_engagement_days=60,
                              excluded_sectors=["Gambling"]))


def _opp(**kw):
    base = dict(title="Director of IT Services", track="employment", comp_max=120000,
                remote_type="hybrid", location_raw="Denver, CO", city="Denver", state="CO",
                country="US", date_posted="2026-01-01", event_date="2026-01-01",
                trigger_data={"engagement_days": 30})
    base.update(kw)
    return NS(**base)


CASES = [
    ("seniority_floor", {}, _EMP,
     "Set aside by your seniority rule: the title reads as Director, and your lowest level is "
     "Vice President.", "Change it in", "Profile > Employment",
     "/profile?tab=employment&track=employment"),
    ("comp_floor", {}, _EMP,
     "Set aside by your pay rule: the top of the posted pay is $120,000, below your floor of "
     "$150,000.", "Change it in", "Profile > Employment",
     "/profile?tab=employment&track=employment"),
    ("anti_title", {"title": "Help Desk Manager"}, _EMP,
     'Set aside by your excluded titles rule: the title contains "Help Desk", one of your '
     "excluded titles.", "Change it in", "Profile > Employment",
     "/profile?tab=employment&track=employment"),
    ("sponsorship", {}, _EMP,
     "Set aside by your sponsorship rule: the posting says it will not sponsor a visa, and your "
     "profile says you need sponsorship.", "Change it in", "Profile > About you",
     "/profile?tab=about&track=employment"),
    ("clearance", {}, _EMP,
     "Set aside by the security clearance rule: the posting asks for an active security "
     "clearance.", "See it in", "Settings > Filters", "/settings"),
    ("geo", {}, _EMP,
     "Set aside by your location rule: it is hybrid in Denver, CO, outside the places you "
     "listed.", "Change it in", "Profile > Location", "/profile?tab=location&track=employment"),
    ("geo", {"remote_type": "remote", "country": "CA"}, _EMP,
     "Set aside by your location rule: it is remote in CA, outside the countries you listed.",
     "Change it in", "Profile > Location", "/profile?tab=location&track=employment"),
    ("rate_floor", {"track": "consulting", "comp_max": 1200}, _CON,
     "Set aside by your rate rule: the top of the posted rate is $1,200, below your day rate "
     "floor of $1,800.", "Change it in", "Profile > Consulting",
     "/profile?tab=consulting&track=consulting"),
    ("too_short", {"track": "consulting"}, _CON,
     "Set aside by your engagement length rule: the engagement is 30 days, shorter than your "
     "minimum of 60.", "Change it in", "Profile > Consulting",
     "/profile?tab=consulting&track=consulting"),
    ("geo", {"track": "consulting", "location_raw": None, "city": None, "state": None,
             "remote_type": "unknown"}, _CON,
     "Set aside by your location rule: its location is outside the places you listed.",
     "Change it in", "Profile > Location", "/profile?tab=location&track=consulting"),
]


@pytest.mark.parametrize(("rule", "opp_kw", "profile", "text", "verb", "place", "href"), CASES)
def test_why_sentence_per_rule(rule, opp_kw, profile, text, verb, place, href):
    why = fit.explain_rule(f"blocker:{rule}: shipped reason", opp=_opp(**opp_kw),
                           profile=profile, settings=get_settings())
    assert (why.text, why.verb, why.place, why.href) == (text, verb, place, href)
    assert why.sentence == f"{text} {verb} {place}."
    assert "—" not in why.sentence


def test_stale_rules_name_the_days_and_settings_core():
    settings = get_settings()
    why = fit.explain_rule("blocker:stale: Posting too old", opp=_opp(), settings=settings)
    days = helpers.age_days("2026-01-01")
    assert why.text == (f"Set aside by your posting age rule: it was posted {days} days ago, and "
                        f"your limit is {settings.max_posting_age_days} days.")
    assert (why.place, why.href) == ("Settings > Core", "/settings#core")
    why = fit.explain_rule("blocker:stale_trigger: closed", opp=_opp(track="consulting"),
                           settings=settings)
    assert why.word == "event age" and why.place == "Settings > Core"
    assert f"past your {settings.consulting.max_trigger_age_days}-day window" in why.text


def test_excluded_sector_reads_the_org():
    why = fit.explain_rule("blocker:excluded_sector: x", opp=_opp(track="consulting"),
                           profile=_CON, org=NS(sector="Gambling"))
    assert why.text == ("Set aside by your sector rule: the company's sector, Gambling, is one "
                        "you excluded.")


def test_missing_facts_fall_back_to_a_plain_clause_and_unknown_rules_to_filters():
    why = fit.explain_rule("blocker:seniority_floor: Below seniority floor",
                           opp=_opp(title="Chief Vibes Curator"), profile=None)
    assert why.text == ("Set aside by your seniority rule: the title reads as below the lowest "
                        "level you set.")
    why = fit.explain_rule("blocker:my_rule: whatever", opp=_opp())
    assert why.text == "Set aside by one of your rules (my rule)."
    assert (why.verb, why.place) == ("See your rules in", "Settings > Filters")
    assert why.override_note == "You asked to score this despite your rules."
    assert fit.explain_rule(None).text == "Set aside by one of your rules."


def test_override_note_names_the_rule():
    why = fit.explain_rule("blocker:seniority_floor: x", opp=_opp(), profile=_EMP)
    assert why.override_note == "You asked to score this despite your rule on seniority."
    why = fit.explain_rule("blocker:anti_title: help desk", opp=_opp(), profile=_EMP)
    assert why.override_note == "You asked to score this despite your rule on excluded titles."


def test_rule_word_matches_exact_rule_ids_never_substrings():
    assert fit.rule_word("blocker:comp_floor: x") == "pay"
    assert fit.rule_word("blocker:seniority_floor: x") == "seniority"
    # An operator's own rule ids that merely contain a shipped word stay generic.
    for own in ("compliance_check", "my_comp_rule", "seniority_custom", "geo_extra",
                "stale_listing", "title_words"):
        assert fit.rule_word(f"blocker:{own}: x") == "rule", own


def test_every_shipped_blocker_has_a_home():
    filters = helpers.load_filters(str(paths.packaged_config() / "filters.yml"))
    for track in ("employment", "consulting"):
        for rule in filters[track]["hard_blockers"]:
            why = fit.explain_rule(f"blocker:{rule['id']}: {rule['reason']}",
                                   opp=_opp(track=track))
            assert why.rule == rule["id"] and why.word != "rule", rule["id"]
            assert why.verb in ("Change it in", "See it in")
            assert why.href.startswith("/")


async def test_the_rule_profile_is_read_off_the_event_loop(monkeypatch):
    """routes_pages reads profile.yml for the rule's sentence in a thread, never on the loop."""
    import threading

    from kestrel.web import routes_pages

    loop_thread = threading.get_ident()
    seen: list[int] = []

    def fake_load(track):
        seen.append(threading.get_ident())
        return _EMP

    monkeypatch.setattr("kestrel.pipeline.profile.load_profile", fake_load)
    opp = _opp(stage="t0_reject", stage_reason="blocker:seniority_floor: x")
    detail = {"opp": opp, "tier0_override": None}
    await routes_pages.load_rule_profile(detail)
    assert seen and seen[0] != loop_thread and detail["rule_profile"] is _EMP
    why, note = routes_pages.rule_context(detail)
    assert why is not None and why.rule == "seniority_floor" and note is None
    # A role with no rule sentence to show reads nothing.
    seen.clear()
    await routes_pages.load_rule_profile({"opp": _opp(stage="t2_done"), "tier0_override": None})
    assert seen == []
