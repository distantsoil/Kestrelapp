"""Near titles (D-TTL-1): the normalizer table across several fields, the family matches, the
helpers that use it for soft signals, excluded titles staying strict, seniority unchanged, and the
seeded filters.yml upgrade. Pure functions; no database, no network."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from kestrel import paths
from kestrel.rules import helpers, titles


def _opp(title: str) -> SimpleNamespace:
    return SimpleNamespace(title=title)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # Information technology leadership (the operator report)
        ("IT Director", "Director of IT"),
        ("Director, Information Technology", "Director of IT"),
        ("director of i.t.", "IT Director"),
        ("VP of IT", "Vice President, Technology"),
        ("VP of IT", "VP Information Technology"),
        ("VP of IT", "VP, Technology"),
        ("SVP of IT", "Senior Vice President, Information Technology"),
        ("Sr. Manager, IT Operations", "Senior Manager of Information Technology Ops"),
        ("CIO", "Chief Information Officer"),
        ("CTO", "Chief Technology Officer"),
        # Health care
        ("RN", "Registered Nurse"),
        ("RN - ICU", "ICU Registered Nurse"),
        ("LPN", "Licensed Practical Nurse"),
        ("Nurse Practitioner, Family", "Family NP"),
        # Design and analysis
        ("UX Designer", "User Experience Designer"),
        ("Sr UX Designer", "Senior User Experience Designer"),
        ("Vice President, Technology", "VP of IT"),
        ("Director of Technology", "Director of IT"),
        ("Head, Technology", "Head of IT"),
        ("VP Technology", "VP of IT"),
        ("Data Analyst, Sr.", "Senior Data Analyst"),
        ("QA Engineer", "Quality Assurance Engineer"),
        # People and operations
        ("HR Manager", "Human Resources Manager"),
        ("Director of People Operations", "HR Director"),
        ("Operations Mgr", "Ops Manager"),
        ("Software Developer", "Software Engineer"),
    ],
)
def test_near_equal_table(a, b):
    assert titles.near_equal(a, b), (titles.normalize(a), titles.normalize(b))


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Head of IT", "VP of IT"),  # levels stay distinct, never folded
        ("Director of IT", "VP of IT"),
        ("IT Manager", "IT Director"),
        ("SVP of IT", "VP of IT"),  # equal sets only; SVP contains VP (below), it is not VP
        ("Registered Nurse", "Nurse Practitioner"),
        ("UX Designer", "UI Designer"),
        ("CIO", "CTO"),
        # "Technology" is IT only in a bare level title (D-TTL-1 amended)
        ("Director of Information Security and Technology",
         "Director of Information Security and IT"),
        ("Technology Sales Manager", "IT Sales Manager"),
        ("Director of Educational Technology", "Director of Educational IT"),
        ("Technology Director", "IT Director"),  # not one of the three bare forms
        # PR and UI are ambiguous: not expanded
        ("PR Manager", "Public Relations Manager"),
        ("UI Designer", "User Interface Designer"),
        ("", ""),
    ],
)
def test_not_near_equal(a, b):
    assert not titles.near_equal(a, b)


def test_containment_ignores_order_and_keeps_concepts_whole():
    assert titles.near_contains("Director of IT Services", "IT Director")
    assert titles.near_contains("Senior Director, Information Technology", "Director of IT")
    assert titles.near_contains("SVP of IT", "VP of IT")
    assert titles.near_contains("Registered Nurse - Night Shift", "RN")
    assert not titles.near_contains("Head of IT", "VP of IT")
    # "information" alone is not information technology
    assert not titles.near_contains("Director of Information Security", "IT Director")
    assert not titles.near_contains("Director", "")


def test_filler_and_punctuation():
    assert titles.normalize("The Director of the IT & Ops") == (
        "director", "information_technology", "operation")
    assert titles.normalize("C++ Developer") == ("c++", "developer")
    assert titles.normalize(None) == ()


def test_soft_helpers_use_near_titles():
    targets = ["Director of IT", "VP of IT"]
    assert helpers.title_near_equals_any(_opp("IT Director"), targets)
    assert helpers.title_near_equals_any(_opp("Vice President, Technology"), targets)
    assert helpers.title_near_equals_any(_opp("director of it"), targets)  # exact, lowercased
    assert not helpers.title_near_equals_any(_opp("Head of IT"), targets)
    assert not helpers.title_near_equals_any(_opp("IT Director"), None)
    assert helpers.title_near_any(_opp("Director of IT Services"), targets)
    assert helpers.title_near_any(_opp("Director of ITSM"), ["Director of IT"])  # old substring
    assert not helpers.title_near_any(_opp("Head of IT"), ["VP of IT"])
    assert not helpers.title_near_any(_opp(None), targets)


def test_title_equals_any_is_strict_exact_again():
    """Hard rule 4: a rule written with title_equals_any, a blocker above all, reads as
    written. "Sr Analyst" never equals "Senior Analyst" there."""
    assert helpers.title_equals_any(_opp("Sr Analyst"), ["sr analyst"])
    assert not helpers.title_equals_any(_opp("Senior Analyst"), ["Sr Analyst"])
    assert not helpers.title_equals_any(_opp("IT Director"), ["Director of IT"])
    assert helpers.title_near_equals_any(_opp("Senior Analyst"), ["Sr Analyst"])  # the soft one


def test_a_blocker_using_title_equals_any_does_not_reject_a_near_title():
    from kestrel.config import get_settings
    from kestrel.pipeline.tier0 import evaluate_tier0

    filters = {"employment": {
        "hard_blockers": [{"id": "no_sr_analyst", "reason": "not this one",
                           "when": "title_equals_any(opp, ['Sr Analyst'])"}],
        "soft_signals": [],
    }}

    def run(title):
        opp = SimpleNamespace(title=title, description_md="", comp_max=None, comp_source="none",
                              date_posted=None, seen_count=1, ghost_score=0.0, remote_type=None,
                              country=None, state=None, city=None, location_raw=None)
        return evaluate_tier0(opp=opp, org=None, profile=SimpleNamespace(), source=None,
                              settings=get_settings(), filters=filters, track="employment")

    assert not run("Sr Analyst").passed
    assert run("Senior Analyst").passed


def test_anti_titles_stay_strict_substring():
    """Hard rule 4: a broader excluded-title match would reject more roles."""
    anti = ["Help Desk", "IT Technician"]
    assert helpers.title_contains_any(_opp("Help Desk Lead"), anti)
    assert not helpers.title_contains_any(_opp("Helpdesk Lead"), anti)
    assert not helpers.title_contains_any(_opp("Technician, Information Technology"), anti)
    assert not helpers.title_contains_any(_opp("Information Technology Technician"), anti)


def test_packaged_filters_keep_anti_title_strict_and_soft_signals_near():
    filters = helpers.load_filters(str(paths.packaged_config() / "filters.yml"))
    blockers = {r["id"]: r["when"] for r in filters["employment"]["hard_blockers"]}
    soft = {r["id"]: r["when"] for r in filters["employment"]["soft_signals"]}
    assert blockers["anti_title"].startswith("title_contains_any(")
    assert soft["title_contains"].startswith("title_near_any(")
    assert soft["title_exact"].startswith("title_near_equals_any(")


def test_a_seeded_untouched_soft_line_reads_as_near_and_an_edited_one_does_not(tmp_path):
    old = {"employment": {
        "hard_blockers": [{
            "id": "anti_title", "reason": "x",
            "when": "title_contains_any(opp, profile.employment_terms.anti_titles)"}],
        "soft_signals": [
            {"id": "title_contains", "weight": 1.0,
             "when": "title_contains_any(opp, profile.employment_terms.target_titles)"},
            {"id": "title_exact", "weight": 2.0,
             "when": "title_equals_any(opp, profile.employment_terms.target_titles)"},
            {"id": "title_exact", "weight": 2.0,  # edited: read as written
             "when": "title_equals_any(opp, profile.employment_terms.target_titles) and 1"},
            {"id": "mine", "weight": 1.0,
             "when": "title_contains_any(opp, profile.employment_terms.target_titles)"},
        ],
    }}
    path = tmp_path / "filters.yml"
    path.write_text(yaml.safe_dump(old), encoding="utf-8")
    got = helpers.load_filters(str(path))["employment"]
    assert got["soft_signals"][0]["when"].startswith("title_near_any(")
    assert got["soft_signals"][1]["when"].startswith("title_near_equals_any(")
    assert got["soft_signals"][2]["when"].endswith(" and 1")  # edited: untouched by the upgrade
    assert got["soft_signals"][3]["when"].startswith("title_contains_any(")  # not the shipped id
    assert got["hard_blockers"][0]["when"].startswith("title_contains_any(")


def test_seniority_is_unchanged():
    cases = {
        "Chief Information Officer": 8, "SVP of IT": 7, "VP of IT": 6, "Senior Director, IT": 5,
        "Director of IT Services": 4, "Head of IT": 4, "Senior Manager, IT": 3, "IT Manager": 2,
        "Team Lead": 1, "Data Analyst": 0, "Chief Vibes Curator": -1, "": -1,
    }
    for title, rank in cases.items():
        assert helpers.seniority_rank(title) == rank, title
    assert helpers.seniority_match("Director of IT Services") == (4, "Director")


def test_phrasings_swap_abbreviations_and_families_only():
    assert titles.phrasings("Director of IT") == [
        "Director of Information Technology", "Director of Technology"]
    assert titles.phrasings("IT Director") == ["Information Technology Director"]
    assert titles.phrasings("Vice President, Technology") == ["VP, Technology",
                                                              "Vice President, IT"]
    assert titles.phrasings("Registered Nurse") == ["RN"]
    assert titles.phrasings("UX Designer") == ["User Experience Designer"]
    assert titles.phrasings("HR Manager") == ["Human Resources Manager",
                                              "People Operations Manager"]
    assert titles.phrasings("Senior Data Analyst") == []  # nothing common to swap
    assert titles.phrasings("PR Manager") == [] and titles.phrasings("UI Designer") == []
    # A family swap that would change the meaning is dropped by the token-set check.
    assert titles.phrasings("Technology Sales Manager") == []


# The reviewer's examples: word-order inversions ("Director Executive", "Staff of Chief",
# "Software Engineer" from "Engineer Software") must never appear.
_JUNK_CASES = {
    "Vice President": ["VP"],
    "General Manager": ["GM"],
    "Executive Director": [],
    "Medical Director": [],
    "Project Manager": [],
    "Chief of Staff": [],
    "Engineer Software": [],
    "Nurse Registered": [],
    "RN Case Manager": ["Registered Nurse Case Manager"],
}


@pytest.mark.parametrize(("title", "expected"), list(_JUNK_CASES.items()))
def test_phrasings_never_reorder_words(title, expected):
    out = titles.phrasings(title)
    assert out == expected
    for v in out:
        assert titles.token_set(v) == titles.token_set(title)


def test_every_variant_normalizes_to_its_title():
    sample = ["Director of IT", "VP of IT", "Head of HR", "Senior IT Manager", "CHRO",
              "SVP, Technology", "Registered Nurse", "LPN", "QA Engineer", "Software Developer",
              "Director of People Operations", "UX Designer", "BI Analyst", "SRE"]
    for t in sample:
        out = titles.phrasings(t)
        assert len(out) <= 3 and t not in out and len({o.lower() for o in out}) == len(out)
        for v in out:
            assert titles.token_set(v) == titles.token_set(t), (t, v)


def test_tier0_scores_a_near_title_and_its_anti_title_stays_strict():
    from kestrel.config import get_settings
    from kestrel.pipeline.tier0 import evaluate_tier0

    filters = helpers.load_filters(str(paths.packaged_config() / "filters.yml"))
    profile = SimpleNamespace(
        requires_sponsorship=False, must_have=[], must_avoid=[], geo_rules=None,
        employment_terms=SimpleNamespace(
            target_titles=["Director of IT"], anti_titles=["Help Desk"], seniority_floor="",
            comp_floor_usd=None, comp_target_usd=None),
    )

    def run(title):
        opp = SimpleNamespace(title=title, description_md="", comp_max=None, comp_source="none",
                              date_posted=None, seen_count=1, ghost_score=0.0, remote_type=None,
                              country=None, state=None, city=None, location_raw=None)
        return evaluate_tier0(opp=opp, org=None, profile=profile, source=None,
                              settings=get_settings(), filters=filters, track="employment")

    hit = run("IT Director")
    assert hit.passed and {"title_exact", "title_contains"} <= set(hit.soft_hits)
    assert "title_contains" in run("Director, Information Technology Services").soft_hits
    assert not run("Help Desk Supervisor").passed  # strict anti_title still rejects
    assert run("Helpdesk Supervisor").passed  # and does not widen
