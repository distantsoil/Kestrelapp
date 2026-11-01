"""D-PROF-4 (issue #70): with no operator profile.yml the profile is EMPTY, never the shipped
fictional example. Scoring prompts carry no example proof points; Tier 0 rejects nothing on it
(hard rule 4); the example is still loadable on purpose."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kestrel import paths
from kestrel.config import get_settings
from kestrel.pipeline.context import profile_compact, profile_full, profile_is_thin
from kestrel.pipeline.profile import load_example_profile, load_profile, operator_block
from kestrel.pipeline.tier0 import evaluate_tier0
from kestrel.rules import helpers
from kestrel.services.profile_readiness import operator_profile

EXAMPLE_CLAIM = "Cut weekly reporting time from two days to two hours"


@pytest.mark.parametrize("track", ["employment", "consulting"])
def test_missing_profile_yml_is_an_empty_profile(track):
    assert not (paths.config_dir() / "profile.yml").exists()
    p = load_profile(track)
    assert p is not None
    assert list(p.proof_points) == [] and p.narrative == ""
    assert list(p.must_have) == [] and list(p.must_avoid) == []
    assert profile_is_thin(p)
    assert operator_profile(track) is None and operator_block(track) is None


def test_empty_profile_prompts_hold_no_example_text():
    p = load_profile("employment")
    assert "Jordan" not in profile_full(p) and EXAMPLE_CLAIM not in profile_full(p)
    assert "Operations Analyst" not in profile_compact(p)
    assert "unspecified" in profile_compact(p)
    assert "unspecified" in profile_compact(load_profile("consulting"))


def test_unreadable_profile_yml_is_empty_too():
    (paths.config_dir()).mkdir(parents=True, exist_ok=True)
    (paths.config_dir() / "profile.yml").write_text("employment: [unclosed", encoding="utf-8")
    assert list(load_profile("employment").proof_points) == []


def test_operator_profile_yml_still_wins_and_missing_track_is_none():
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    (paths.config_dir() / "profile.yml").write_text(
        "employment:\n  track: employment\n  narrative: mine\n  proof_points: []\n",
        encoding="utf-8",
    )
    assert load_profile("employment").narrative == "mine"
    assert load_profile("consulting") is None


def _opp(**kw):
    base = dict(
        title="Senior Director of Basket Weaving", comp_max=1000, comp_min=900,
        comp_source="parsed", date_posted=None, event_date=None, seen_count=1, ghost_score=0.1,
        url=None, trigger_data=None, employment_type="part_time", is_remote=None,
        description_md="weave baskets", remote_type="onsite", country="DE", state=None,
        city="Berlin", location_raw="Berlin",
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("track", ["employment", "consulting"])
@pytest.mark.parametrize("remote_type", ["onsite", "hybrid", "remote", "unknown"])
def test_tier0_does_not_reject_on_an_empty_profile(track, remote_type):
    filters = helpers.load_filters(str(paths.config_file("filters.yml")))
    r = evaluate_tier0(
        opp=_opp(remote_type=remote_type), org=None, profile=load_profile(track), source=None,
        settings=get_settings(), filters=filters, track=track,
    )
    assert r.passed, (r.fired_rule, r.reason)


def test_example_profile_is_still_loadable_explicitly():
    ex = load_example_profile("employment")
    assert ex is not None and ex.first_name == "Jordan"
    assert EXAMPLE_CLAIM in profile_full(ex)
    assert load_example_profile("consulting").consulting_terms.service_lines


# ---------------------------------------------------------------- an unreadable profile.yml
def _break_profile():
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    (paths.config_dir() / "profile.yml").write_text("employment: [unclosed SECRETWORD",
                                                   encoding="utf-8")


def test_an_unreadable_profile_is_logged_and_flagged_but_scores_as_empty():
    from kestrel.pipeline.profile import profile_unreadable

    assert profile_unreadable() is False
    _break_profile()
    assert profile_unreadable() is True
    assert list(load_profile("employment").proof_points) == []
    (paths.config_dir() / "profile.yml").write_text("", encoding="utf-8")
    assert profile_unreadable() is False  # an empty file is just no profile
    (paths.config_dir() / "profile.yml").write_text("- a\n- list\n", encoding="utf-8")
    assert profile_unreadable() is True


def test_the_doctor_fails_profile_configured_with_a_plain_fix_and_no_parse_text():
    from kestrel import doctor

    _break_profile()
    rep = doctor.DoctorReport()
    doctor._check_profile(rep)
    (res,) = rep.results
    assert res.name == "Profile configured" and res.status == doctor.FAIL
    assert "SECRETWORD" not in res.detail + res.remediation and "line" not in res.detail
    assert "Start fresh" in res.remediation


def test_readiness_names_the_unreadable_profile_as_its_one_next_step():
    import asyncio

    from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
    from kestrel.services.profile_readiness import (
        PROFILE_UNREADABLE,
        UNREADABLE_LINE,
        load_readiness,
    )

    _break_profile()

    async def go():
        await reset_engine()
        async with get_sessionmaker()() as s:
            return await load_readiness(s, "employment")

    ready = asyncio.run(go())
    forget_engine()
    assert ready.next_step == PROFILE_UNREADABLE and ready.line == UNREADABLE_LINE
    assert UNREADABLE_LINE == ("Your profile file could not be read, so Kestrel is scoring "
                               "without it. Open Profile to fix it.")
