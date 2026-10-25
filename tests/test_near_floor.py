"""The near-floor margin on the pay and day-rate floors (D-RULES-2, hard rule 4).

A role whose posted maximum is under the floor by no more than the margin is kept and tagged
"Below your minimum"; one under it by more is rejected; a role with no posted pay is always kept.
The same stops serve the consulting rate floor."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from kestrel import paths
from kestrel.config import get_settings
from kestrel.pipeline import profile as profile_mod
from kestrel.pipeline.profile import load_profile
from kestrel.pipeline.tier0 import evaluate_tier0
from kestrel.rules import helpers, margin
from kestrel.rules.sample import sample_context
from kestrel.services import profile_edit as pe

FLOOR = 100_000


def _filters() -> dict:
    return helpers.load_filters(str(paths.config_file("filters.yml")))


def _opp(comp_max, **kw) -> SimpleNamespace:
    base = dict(
        title="Operations Analyst", description_md="Reports.", comp_max=comp_max,
        comp_min=None, comp_source="parsed" if comp_max is not None else "none",
        date_posted=None, event_date=None, remote_type="remote", country="US", state=None,
        city=None, location_raw="Remote", seen_count=1, ghost_score=0.1, url="https://x",
        trigger_data={"engagement_days": 40},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _profile(monkeypatch, *, pct="unset", rate_pct="unset", floor=FLOOR, rate_floor=1000):
    def loaded() -> dict:
        emp: dict = {"comp_floor_usd": floor}
        con: dict = {"day_rate_floor": rate_floor}
        if pct != "unset":
            emp["comp_floor_margin_pct"] = pct
        if rate_pct != "unset":
            con["day_rate_floor_margin_pct"] = rate_pct
        return {
            "employment": {"track": "employment", "first_name": "Pat", "employment_terms": emp,
                           "geo_rules": {"remote_ok": True}},
            "consulting": {"track": "consulting", "first_name": "Pat", "consulting_terms": con,
                           "geo_rules": {"remote_ok": True}},
        }

    monkeypatch.setattr(profile_mod, "_load_yaml", loaded)


def _eval(track, opp, profile):
    return evaluate_tier0(
        opp=opp, org=SimpleNamespace(sector=None, ownership=None, size_band=None, signals=[]),
        profile=profile, source=SimpleNamespace(kind="greenhouse"), settings=get_settings(),
        filters=_filters(), track=track,
    )


# -------------------------------------------------------------------------------- the helpers
def test_margin_floor_is_the_floor_less_the_margin():
    assert margin.margin_floor(100_000, 5) == 95_000
    assert margin.margin_floor(100_000, 0) == 100_000
    assert margin.margin_floor(100_000, 15) == 85_000
    assert margin.margin_floor(None, 5) is None


def test_margin_stops_and_default():
    assert margin.MARGIN_STOPS == (0, 2, 5, 10, 15)
    assert margin.DEFAULT_MARGIN_PCT == 5
    for bad in (None, "abc", 7, 20, -1, True, float("nan")):
        assert margin.normalize_margin(bad) == 5
    assert [margin.normalize_margin(x) for x in (0, "2", 5.0, 10, 15)] == [0, 2, 5, 10, 15]


@pytest.mark.parametrize(
    ("cap", "pct", "near"),
    [(95_000, 5, True), (94_999, 5, False), (100_000, 5, False), (99_999, 0, False),
     (98_000, 2, True), (97_999, 2, False), (85_000, 15, True), (None, 5, False)],
)
def test_near_floor_window(cap, pct, near):
    assert margin.comp_max_near_floor(_opp(cap), FLOOR, pct) is near


def test_tags_are_read_back_from_the_stage_reason():
    assert margin.tags_from_stage_reason("soft: title_exact, near_floor; geo_uncertain") == [
        "Below your minimum"
    ]
    assert margin.tags_from_stage_reason("soft: title_exact") == []
    assert margin.tags_from_stage_reason("blocker:comp_floor: x") == []
    assert margin.tags_from_stage_reason(None) == []


# --------------------------------------------------------------------- employment, in Tier 0
def test_default_margin_is_five_percent_when_the_profile_has_none(monkeypatch):
    _profile(monkeypatch)  # no comp_floor_margin_pct in the file
    profile = load_profile("employment")
    assert profile.employment_terms.comp_floor_margin_pct == 5
    near = _eval("employment", _opp(96_000), profile)
    assert near.passed and near.tags == ["Below your minimum"] and "near_floor" in near.soft_hits
    far = _eval("employment", _opp(94_000), profile)
    assert not far.passed and far.fired_rule == "comp_floor"


@pytest.mark.parametrize(
    ("pct", "cap", "passes", "tagged"),
    [(0, 99_999, False, False), (0, 100_000, True, False), (2, 98_000, True, True),
     (2, 97_000, False, False), (5, 95_000, True, True), (10, 90_000, True, True),
     (10, 89_999, False, False), (15, 85_000, True, True), (15, 84_000, False, False),
     (5, 105_000, True, False)],
)
def test_each_stop_keeps_or_rejects(monkeypatch, pct, cap, passes, tagged):
    _profile(monkeypatch, pct=pct)
    result = _eval("employment", _opp(cap), load_profile("employment"))
    assert result.passed is passes
    assert (result.tags == ["Below your minimum"]) is tagged


def test_a_role_without_posted_pay_is_always_kept_and_untagged(monkeypatch):
    for pct in (0, 2, 5, 10, 15):
        _profile(monkeypatch, pct=pct)
        result = _eval("employment", _opp(None), load_profile("employment"))
        assert result.passed and result.tags == []


def test_no_floor_means_nothing_to_enforce(monkeypatch):
    _profile(monkeypatch, pct=0, floor=None)
    result = _eval("employment", _opp(1), load_profile("employment"))
    assert result.passed and result.tags == []


def test_an_off_stop_value_in_a_hand_edited_file_reads_as_the_default(monkeypatch):
    _profile(monkeypatch, pct=7)
    assert load_profile("employment").employment_terms.comp_floor_margin_pct == 5


# ------------------------------------------------------------------------------- consulting
def test_the_consulting_rate_floor_uses_the_same_stops(monkeypatch):
    _profile(monkeypatch, rate_pct=5, rate_floor=1000)
    profile = load_profile("consulting")
    assert profile.consulting_terms.day_rate_floor_margin_pct == 5
    near = _eval("consulting", _opp(960), profile)
    assert near.passed and near.tags == ["Below your minimum"]
    far = _eval("consulting", _opp(900), profile)
    assert not far.passed and far.fired_rule == "rate_floor"
    unposted = _eval("consulting", _opp(None), profile)
    assert unposted.passed and unposted.tags == []


def test_the_consulting_default_is_five_percent(monkeypatch):
    _profile(monkeypatch, rate_floor=1000)
    assert load_profile("consulting").consulting_terms.day_rate_floor_margin_pct == 5
    _profile(monkeypatch, rate_pct=0, rate_floor=1000)
    assert not _eval("consulting", _opp(999), load_profile("consulting")).passed


# ----------------------------------------------------------------- the rules file and upgrades
def test_the_shipped_rules_load_and_evaluate_with_the_guard():
    filters = _filters()
    helpers.check_filters(filters, settings=get_settings(), **sample_context())
    for track, rule_id in (("employment", "comp_floor"), ("consulting", "rate_floor")):
        blockers = {r["id"]: r for r in filters[track]["hard_blockers"]}
        assert "margin_floor(" in blockers[rule_id]["when"]
        softs = {r["id"]: r for r in filters[track]["soft_signals"]}
        assert softs["near_floor"]["weight"] == 0.0


def test_an_old_seeded_filters_file_is_upgraded_in_memory(tmp_path):
    old = {
        "employment": {
            "hard_blockers": [{
                "id": "comp_floor",
                "when": "comp_max_below(opp, profile.employment_terms.comp_floor_usd)",
                "reason": "x"}],
            "soft_signals": [],
        },
        "consulting": {
            "hard_blockers": [{
                "id": "rate_floor",
                "when": "comp_max_below(opp, profile.consulting_terms.day_rate_floor)",
                "reason": "x"}],
        },
    }
    path = tmp_path / "filters.yml"
    path.write_text(yaml.safe_dump(old), encoding="utf-8")
    data = helpers.load_filters(str(path))
    assert "margin_floor(" in data["employment"]["hard_blockers"][0]["when"]
    assert [r["id"] for r in data["employment"]["soft_signals"]] == ["near_floor"]
    assert [r["id"] for r in data["consulting"]["soft_signals"]] == ["near_floor"]
    helpers.check_filters(data, settings=get_settings(), **sample_context())


def test_an_operator_edited_blocker_is_read_as_written(tmp_path):
    edited = {"employment": {"hard_blockers": [{
        "id": "comp_floor",
        "when": "comp_max_below(opp, 50000)",
        "reason": "mine"}]}}
    path = tmp_path / "filters.yml"
    path.write_text(yaml.safe_dump(edited), encoding="utf-8")
    data = helpers.load_filters(str(path))
    assert data["employment"]["hard_blockers"][0]["when"] == "comp_max_below(opp, 50000)"
    assert "soft_signals" not in data["employment"]


def test_the_ast_guard_is_untouched():
    """The margin helpers are whitelisted functions; comprehensions stay banned."""
    with pytest.raises(helpers.FilterConfigError):
        helpers.check_filters(
            {"employment": {"hard_blockers": [
                {"id": "g", "when": "any(x for x in [1])", "reason": "x"}]}},
            settings=get_settings(), **sample_context(),
        )


# ------------------------------------------------------------------------- the profile form
def test_the_profile_form_stores_a_stop_and_refuses_anything_else():
    terms = pe.section_from_form("employment", {"seniority_floor": "", "comp_floor_usd": "100000",
                                                "comp_floor_margin_pct": "10"})
    assert terms["employment_terms"]["comp_floor_margin_pct"] == 10
    exact = pe.section_from_form("employment", {"seniority_floor": "",
                                                "comp_floor_margin_pct": "Exact"})
    assert exact["employment_terms"]["comp_floor_margin_pct"] == 0
    left_alone = pe.section_from_form("employment", {"seniority_floor": ""})
    assert "comp_floor_margin_pct" not in left_alone["employment_terms"]
    for bad in ("20", "3", "lots"):
        with pytest.raises(pe.ProfileEditError):
            pe.section_from_form("employment", {"seniority_floor": "",
                                                "comp_floor_margin_pct": bad})
    con = pe.section_from_form("consulting", {"day_rate_floor": "900",
                                              "day_rate_floor_margin_pct": "2"})
    assert con["consulting_terms"]["day_rate_floor_margin_pct"] == 2


def test_a_saved_margin_round_trips_through_the_loader():
    pe.save_section("employment", "about", pe.section_from_form("about", {"first_name": "Pat"}))
    pe.save_section("employment", "employment", pe.section_from_form(
        "employment", {"seniority_floor": "", "comp_floor_usd": "120000",
                       "comp_floor_margin_pct": "15"}))
    assert load_profile("employment").employment_terms.comp_floor_margin_pct == 15
