"""Tier 0 batch sweep (SPEC.md 6.1): rule attribution on rejection, pre_score on survivors, stage
transitions, hard-rule-4 bias (unknown title / missing date pass), and one t1 task per survivor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from kestrel import paths
from kestrel.config import get_settings
from kestrel.db.models import Opportunity, Source, Task
from kestrel.pipeline.profile import load_profile
from kestrel.pipeline.tier0 import evaluate_tier0, run_tier0_batch
from kestrel.rules import helpers


# A posting date that is always inside max_posting_age_days, so the stale blocker never
# time-bombs this gate (it did: a hardcoded July date went red on 2026-09-13).
def _recent() -> str:
    return (datetime.now(UTC) - timedelta(days=3)).date().isoformat()


async def _seed_source(sm) -> int:
    async with sm() as s:
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        s.add(src)
        await s.flush()
        sid = src.id
        await s.commit()
    return sid


def _opp(**kw) -> Opportunity:
    base = dict(
        track="employment",
        opp_type="job_posting",
        remote_type="hybrid",
        comp_source="parsed",
        seen_count=1,
        ghost_score=0.1,
        stage="new",
    )
    base.update(kw)
    return Opportunity(**base)


# A profile that opts in to filters. The shipped example filters nothing (no floors, no excluded
# titles, no on-site places: release concern 10 and release round review finding 1), so the
# rules are exercised with an operator who set them.
_OPTED_IN_GEO = {
    "remote_ok": True, "remote_country_allowlist": ["US"], "onsite_states": ["OH"],
    "onsite_metros": [{"name": "Columbus", "aliases": ["Columbus", "Dublin"]}],
    "hybrid_requires_metro": True,
}


def _opt_in(monkeypatch, **terms_update) -> None:
    from kestrel.pipeline import profile as profile_mod

    real = profile_mod._load_yaml

    def opted_in() -> dict:
        data = real()
        terms = dict(data["employment"]["employment_terms"])
        terms.update(anti_titles=["Account Executive"], **terms_update)
        data["employment"] = {**data["employment"], "employment_terms": terms,
                              "geo_rules": dict(_OPTED_IN_GEO)}
        return data

    monkeypatch.setattr(profile_mod, "_load_yaml", opted_in)


@pytest.fixture
def exec_floors(monkeypatch):
    """Opted in to a director floor, a pay floor, an excluded title and on-site places."""
    _opt_in(monkeypatch, seniority_floor="director", comp_floor_usd=190000)


@pytest.fixture
def opted_in_geo(monkeypatch):
    _opt_in(monkeypatch)


async def test_tier0_attributes_rules_and_scores(sm, exec_floors):
    sid = await _seed_source(sm)
    async with sm() as s:
        s.add_all(
            [
                _opp(  # survivor: VP title, in-metro, comp above target
                    source_id=sid, title="VP of IT", comp_max=250000, state="OH", city="Columbus",
                    location_raw="Columbus, OH", date_posted=_recent(),
                    description_md="Own IT leadership, budget and vendor accountability, security.",
                ),
                _opp(  # anti-title
                    source_id=sid, title="Account Executive", comp_max=250000, state="OH",
                    city="Columbus", location_raw="Columbus, OH",
                ),
                _opp(  # below comp floor (190000)
                    source_id=sid, title="Head of IT", comp_max=120000, state="OH", city="Columbus",
                    location_raw="Columbus, OH",
                ),
                _opp(  # below seniority floor (director); comp fine, not anti
                    source_id=sid, title="Support Engineer", comp_max=250000, state="OH",
                    city="Columbus", location_raw="Columbus, OH",
                ),
                _opp(  # geo outside acceptable set, onsite
                    source_id=sid, title="IT Director", comp_max=250000, remote_type="onsite",
                    country="GB", location_raw="London, UK",
                ),
                _opp(  # hard rule 4: unknown title + no dates -> passes
                    source_id=sid, title="Chief Vibes Curator", comp_max=250000, state="OH",
                    city="Columbus", location_raw="Columbus, OH", date_posted=None,
                ),
            ]
        )
        await s.commit()

    settings = get_settings()
    counts = await run_tier0_batch(sm, settings=settings)
    assert counts["seen"] == 6

    async with sm() as s:
        by_title = {
            o.title: o for o in (await s.execute(select(Opportunity))).scalars().all()
        }
        assert by_title["VP of IT"].stage == "t0_pass"
        assert by_title["VP of IT"].pre_score > 0
        assert by_title["Chief Vibes Curator"].stage == "t0_pass"  # unknown title passes

        assert by_title["Account Executive"].stage == "t0_reject"
        assert "anti_title" in by_title["Account Executive"].stage_reason
        assert "comp_floor" in by_title["Head of IT"].stage_reason
        assert "seniority_floor" in by_title["Support Engineer"].stage_reason
        assert "geo" in by_title["IT Director"].stage_reason

        # one t1 task per survivor (VP of IT, Chief Vibes Curator)
        n_t1 = (
            await s.execute(select(func.count()).select_from(Task).where(Task.type == "t1"))
        ).scalar_one()
        assert n_t1 == 2


def test_geo_uncertain_matches_spec_step5_both_directions(opted_in_geo):
    """geo_uncertain is recorded only in the SPEC 6.1 step-5 case (unknown remote, nothing matched
    but allowed), NOT for a confident metro-substring match, and IS recorded when a parsed-but-
    unlisted state falls through."""
    profile = load_profile("employment")
    filters = helpers.load_filters(str(paths.config_file("filters.yml")))
    settings = get_settings()

    def geo_uncertain_for(**opp_kw) -> bool:
        opp = SimpleNamespace(
            title="VP of IT", description_md="lead IT", comp_max=250000, comp_source="parsed",
            date_posted=None, event_date=None, seen_count=1, ghost_score=0.1, url=None,
            trigger_data=None, employment_type=None, is_remote=None, **opp_kw,
        )
        r = evaluate_tier0(opp=opp, org=None, profile=profile, source=None, settings=settings,
                           filters=filters, track="employment")
        return r.passed and r.geo_uncertain

    # confident metro substring match at step 4 -> NOT uncertain (was over-reported by the proxy)
    assert geo_uncertain_for(
        remote_type="unknown", state=None, city=None, location_raw="Columbus, OH"
    ) is False
    # parsed-but-unlisted state, nothing matches -> genuinely uncertain (was under-reported)
    assert geo_uncertain_for(
        remote_type="unknown", state="CA", city=None, location_raw="Somewhere, CA"
    ) is True


async def test_tier0_is_idempotent_on_rerun(sm):
    sid = await _seed_source(sm)
    async with sm() as s:
        s.add(_opp(source_id=sid, title="VP of IT", comp_max=250000, state="OH", city="Columbus",
                   location_raw="Columbus, OH"))
        await s.commit()
    settings = get_settings()
    await run_tier0_batch(sm, settings=settings)
    await run_tier0_batch(sm, settings=settings)  # second sweep: nothing is 'new' anymore
    async with sm() as s:
        n_t1 = (
            await s.execute(select(func.count()).select_from(Task).where(Task.type == "t1"))
        ).scalar_one()
        assert n_t1 == 1  # not duplicated


async def test_default_filters_keep_a_mid_level_analyst(sm):
    """Release concern 10: with the shipped example profile (no seniority or pay floor) a
    mid-career analyst posting is not rejected for its level or its pay."""
    sid = await _seed_source(sm)
    async with sm() as s:
        s.add_all([
            _opp(source_id=sid, title="Operations Analyst", comp_max=62000, state="OH",
                 city="Columbus", location_raw="Columbus, OH", date_posted=_recent(),
                 description_md="Weekly operations reporting and process improvement."),
            _opp(source_id=sid, title="Supply Chain Coordinator", comp_max=55000, state="OH",
                 city="Columbus", location_raw="Columbus, OH", date_posted=_recent()),
            _opp(source_id=sid, title="Business Analyst", comp_max=None, remote_type="remote",
                 country="US", location_raw="Remote (US)", date_posted=_recent()),
        ])
        await s.commit()
    await run_tier0_batch(sm, settings=get_settings())
    async with sm() as s:
        rows = (await s.execute(select(Opportunity))).scalars().all()
        assert {o.title: o.stage for o in rows} == {
            "Operations Analyst": "t0_pass",
            "Supply Chain Coordinator": "t0_pass",
            "Business Analyst": "t0_pass",
        }
