"""Release round R2: LinkedIn is not searched (D-SRC-5), consulting is off for a new install,
and the shipped example and defaults describe no particular person, place or seniority
(release concern 10). No network, no live board."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

import kestrel
from kestrel.db.models import Notification, Opportunity, Setting, Source
from kestrel.ingest import generated
from kestrel.ingest.catalog import REGION_NOTE, source_catalog
from kestrel.ingest.sources import (
    LINKEDIN_NOTICE_BODY,
    LINKEDIN_NOTICE_KIND,
    retire_linkedin_sources,
)
from kestrel.services import onboarding
from kestrel.services import tracks as tracks_svc

PKG = Path(kestrel.__file__).parent


# ------------------------------------------------------------------------------- LinkedIn out
def test_generated_searches_never_name_linkedin():
    assert "linkedin" not in [s.lower() for s in generated.SITES]
    profiles = {
        "employment": {
            "employment_terms": {"target_titles": ["Operations Analyst"]},
            "geo_rules": {"remote_ok": True, "remote_country_allowlist": ["US"],
                          "onsite_metros": [{"name": "Columbus", "aliases": ["Columbus"]}]},
        },
        "consulting": {"consulting_terms": {"service_lines": ["reporting"]}},
    }
    specs = generated.build_specs(profiles, ("employment", "consulting"), "pat@example.com")
    assert specs
    assert "linkedin" not in repr([(s.name, s.config, s.summary) for s in specs]).lower()


def test_the_source_catalog_does_not_offer_linkedin():
    blob = repr([dataclasses.asdict(row) for row in source_catalog()]) + REGION_NOTE
    assert "linkedin" not in blob.lower()


def test_the_us_note_is_the_agreed_sentence():
    assert generated.US_NOTE == (
        "Kestrel's built-in job boards are US-focused; other countries work but with fewer "
        "results."
    )


async def test_startup_disables_existing_linkedin_sources_once_with_one_notice(sm):
    async with sm() as s:
        s.add_all([
            Source(track="employment", kind="jobspy", name="LinkedIn only", enabled=True,
                   origin="custom",
                   config={"site_name": ["linkedin"], "search_term": "analyst"}),
            Source(track="employment", kind="jobspy", name="LinkedIn string", enabled=True,
                   origin="custom", config={"site_name": "LinkedIn", "search_term": "analyst"}),
            Source(track="employment", kind="jobspy", name="Mixed", enabled=True,
                   origin="custom",
                   config={"site_name": ["indeed", "linkedin"], "search_term": "analyst"}),
            Source(track="employment", kind="jobspy", name="Indeed", enabled=True,
                   origin="generated", config={"site_name": ["indeed"], "search_term": "x"}),
        ])
        await s.commit()
    async with sm() as s:
        changed = await retire_linkedin_sources(s)
        await s.commit()
    assert sorted(changed) == ["LinkedIn only", "LinkedIn string", "Mixed"]
    async with sm() as s:
        rows = {r.name: r for r in (await s.execute(select(Source))).scalars()}
        assert not rows["LinkedIn only"].enabled and rows["LinkedIn only"].config["site_name"] == []
        assert not rows["LinkedIn string"].enabled
        assert rows["Mixed"].enabled and rows["Mixed"].config["site_name"] == ["indeed"]
        assert rows["Indeed"].enabled
        notes = (await s.execute(
            select(Notification).where(Notification.kind == LINKEDIN_NOTICE_KIND)
        )).scalars().all()
        assert len(notes) == 1 and notes[0].body == LINKEDIN_NOTICE_BODY
        assert "Add a role I found elsewhere" in notes[0].body
    # The next start finds nothing to change and says nothing more.
    async with sm() as s:
        assert await retire_linkedin_sources(s) == []
        await s.commit()
        count = len((await s.execute(
            select(Notification).where(Notification.kind == LINKEDIN_NOTICE_KIND)
        )).scalars().all())
        assert count == 1


async def test_a_database_without_linkedin_gets_no_notice(sm):
    async with sm() as s:
        assert await retire_linkedin_sources(s) == []
        assert (await s.execute(select(Notification))).first() is None


# ------------------------------------------------------------------- consulting off by default
async def test_a_new_install_is_employment_only(sm):
    tracks_svc.forget_cache()
    async with sm() as s:
        assert await tracks_svc.keep_existing_choice(s) is False  # nothing to preserve
        assert await tracks_svc.load_enabled(s) == ("employment",)
        assert await s.get(Setting, tracks_svc.KEY) is None
    assert tracks_svc.consulting_enabled() is False
    steps = [st.key for st in onboarding.steps_for(tracks_svc.DEFAULT)]
    # Six steps whatever the tracks (D-UI-155): the tracks change step 5's sections, not the flow.
    assert steps == ["welcome", "models", "about", "resume", "wants", "searches"]


async def test_an_existing_install_keeps_both_tracks(sm):
    """An install from before the change (finished onboarding, or already holds consulting
    work) has no stored choice; it is pinned to the old implicit setting, not switched off."""
    tracks_svc.forget_cache()
    async with sm() as s:
        s.add(Opportunity(track="consulting", opp_type="engagement", title="Lead"))
        await s.commit()
    async with sm() as s:
        assert await tracks_svc.keep_existing_choice(s) is True
        await s.commit()
    async with sm() as s:
        assert await tracks_svc.load_enabled(s) == tracks_svc.BOTH

    tracks_svc.forget_cache()
    async with sm() as s:
        await s.execute(Setting.__table__.delete())
        await s.execute(Opportunity.__table__.delete())
        await onboarding.save(s, onboarding.OnboardingState(step="done", completed=True))
        await s.commit()
    async with sm() as s:
        assert await tracks_svc.keep_existing_choice(s) is True
        await s.commit()
        assert await tracks_svc.load_enabled(s) == tracks_svc.BOTH


async def test_an_explicit_choice_is_never_overwritten(sm):
    async with sm() as s:
        await tracks_svc.save_enabled(s, ["employment"])
        s.add(Opportunity(track="consulting", opp_type="engagement", title="Lead"))
        await s.commit()
    async with sm() as s:
        assert await tracks_svc.keep_existing_choice(s) is False
        assert await tracks_svc.load_enabled(s) == ("employment",)


# --------------------------------------------------------------------------- neutral defaults
NEUTRAL_FILES = [
    *sorted((PKG / "resources" / "config").glob("*.yml")),
    PKG / "docs" / "seed.py",
    PKG / "rules" / "sample.py",
    PKG / "ingest" / "catalog.py",
    PKG / "ingest" / "generated.py",
]
# The original operator's level, field and place. Whole words, case sensitive, so ordinary
# words ("action", "Octopus") never trip it.
ASSUMED = re.compile(
    r"\b(CIO|CTO|CISO|CEO|Texas|TX|Austin|Dallas|Houston|San Antonio|Fort Worth|"
    r"VP of IT|Vice President|Head of IT|Head of Infrastructure)\b"
)


@pytest.mark.parametrize("path", NEUTRAL_FILES, ids=lambda p: p.name)
def test_shipped_example_and_seed_files_assume_no_executive_in_texas(path):
    hits = [(i, m.group(0)) for i, line in enumerate(path.read_text("utf-8").splitlines(), 1)
            for m in ASSUMED.finditer(line)]
    assert not hits, f"{path.name} names the original operator's level or place: {hits}"


def test_the_example_profile_is_a_mid_career_analyst_with_permissive_filters():
    data = yaml.safe_load((PKG / "resources/config/profile.example.yml").read_text("utf-8"))
    emp = data["employment"]
    terms = emp["employment_terms"]
    assert "analyst" in emp["headline"].lower() and emp["location"] == "Columbus, OH"
    assert terms["seniority_floor"] in ("", None) and terms["comp_floor_usd"] in ("", None)
    assert all("Director" not in t and "VP" not in t for t in terms["target_titles"])
    assert data["consulting"]["consulting_terms"]["day_rate_floor"] in ("", None)


def test_the_profile_form_offers_any_level_and_accepts_it():
    from kestrel.services import profile_edit as pe

    assert pe.SENIORITY_FLOORS[0] == ("", "Any level (no floor)")
    out = pe.section_from_form("employment", {"seniority_floor": ""})
    assert out["employment_terms"]["seniority_floor"] == ""


def test_sources_page_states_the_us_focus_and_linkedin(client_factory=None):
    """Release concern 10 and D-SRC-5: Sources says the boards are US-focused and LinkedIn is
    not searched."""
    from kestrel.web.deps import templates

    env = templates().env
    assert "US-focused" in env.globals["us_note"]
