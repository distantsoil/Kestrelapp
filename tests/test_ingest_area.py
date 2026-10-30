"""D-SRC-13 country screening before storage, with fake sources and no network calls."""

from __future__ import annotations

import pytest
import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel import paths
from kestrel.db.models import CompObservation, Opportunity, Org, ScrapeRun, Source, Task
from kestrel.ingest import runner
from kestrel.ingest.base import RawPosting
from kestrel.ingest.http import HttpClient
from kestrel.ingest.manual import manual_ingest

SessionMaker = async_sessionmaker[AsyncSession]
LOCATIONS = (
    "Austin, TX", "Toronto, Ontario, CA", "Bangalore, Karnataka, India",
    "Remote", "2 Locations", "New York - Remote", "", None, "Uncharted place",
)


class _FakeSource:
    def __init__(self, locations: tuple[str | None, ...]) -> None:
        self.postings = [
            RawPosting(
                external_id=str(i), title=f"Operations Analyst {i}",
                org_name=f"Fictional Employer {i}", location_raw=location,
                description_md="Lead operations and plan team work.",
                comp_min=70000, comp_max=90000, comp_currency="USD", comp_interval="year",
            )
            for i, location in enumerate(locations)
        ]

    def validate_config(self, config: dict) -> None:
        pass

    async def fetch(self, config: dict, http: HttpClient) -> list[RawPosting]:
        return self.postings


def _profile(track: str, countries: list[str] | None) -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    geo = {} if countries is None else {"remote_country_allowlist": countries}
    paths.config_dir().joinpath("profile.yml").write_text(
        yaml.safe_dump({track: {"geo_rules": geo}}), encoding="utf-8"
    )


async def _source(
    sm: SessionMaker, monkeypatch: pytest.MonkeyPatch, *, track: str = "employment",
    kind: str = "greenhouse", locations: tuple[str | None, ...] = LOCATIONS,
) -> int:
    fake = _FakeSource(locations)
    monkeypatch.setattr(runner, "get_adapter", lambda kind: fake)

    async def no_discovery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr(runner.board_discovery, "discover_boards", no_discovery)
    async with sm() as session:
        source = Source(
            track=track, kind=kind, name="Fake source", config={}, enabled=True,
            consecutive_failures=2,
        )
        session.add(source)
        await session.commit()
        return source.id


@pytest.mark.parametrize("track", ["employment", "consulting"])
@pytest.mark.parametrize("kind", ["greenhouse", "jobspy"])
async def test_ingest_keeps_allowed_and_uncertain_locations_and_counts_drops(
    sm: SessionMaker, monkeypatch: pytest.MonkeyPatch, track: str, kind: str,
) -> None:
    _profile(track, ["US"])
    sid = await _source(sm, monkeypatch, track=track, kind=kind)
    kept = {"0", "3", "4", "5", "6", "7", "8"}
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        first = await runner.run_source(sm, sid, http=http)
        again = await runner.run_source(sm, sid, http=http)
    assert first.status == again.status == "ok"
    assert first.error is again.error is None
    assert first.counts["fetched"] == len(LOCATIONS)
    assert first.counts["new"] == again.counts["reseen"] == len(kept)
    assert first.counts["dropped_outside_area"] == again.counts["dropped_outside_area"] == 2
    assert first.counts["errors"] == again.counts["errors"] == 0
    async with sm() as session:
        rows = (await session.scalars(select(Opportunity))).all()
        assert {row.external_id for row in rows} == kept
        assert all(row.seen_count == 2 for row in rows)
        for model in (Org, CompObservation, Task):
            assert await session.scalar(select(func.count()).select_from(model)) == len(kept)
        for outcome in (first, again):
            run = await session.get(ScrapeRun, outcome.scrape_run_id)
            assert run.counts == outcome.counts
            assert run.finished_at is not None
        source = await session.get(Source, sid)
        assert source.consecutive_failures == 0 and source.enabled


@pytest.mark.parametrize(
    ("countries", "kept"),
    [(["ca"], {"1", "3", "4", "5", "6", "7", "8"}),
     (["US", "CA"], {"0", "1", "3", "4", "5", "6", "7", "8"}),
     ([], {str(i) for i in range(len(LOCATIONS))}),
     (None, {str(i) for i in range(len(LOCATIONS))})],
)
async def test_ingest_uses_the_profiles_countries_without_inventing_restrictions(
    sm: SessionMaker, monkeypatch: pytest.MonkeyPatch, countries: list[str] | None,
    kept: set[str],
) -> None:
    _profile("employment", countries)
    sid = await _source(sm, monkeypatch)
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        outcome = await runner.run_source(sm, sid, http=http)
    assert outcome.status == "ok"
    assert outcome.counts["dropped_outside_area"] == len(LOCATIONS) - len(kept)
    async with sm() as session:
        assert set(await session.scalars(select(Opportunity.external_id))) == kept


async def test_missing_track_profile_keeps_roles(
    sm: SessionMaker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _profile("employment", ["US"])
    sid = await _source(sm, monkeypatch, track="consulting")
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        outcome = await runner.run_source(sm, sid, http=http)
    assert outcome.status == "ok"
    assert outcome.counts["new"] == len(LOCATIONS)
    assert outcome.counts["dropped_outside_area"] == 0


async def test_all_outside_area_is_success_and_stores_no_related_rows(
    sm: SessionMaker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _profile("employment", ["US"])
    sid = await _source(sm, monkeypatch, locations=LOCATIONS[1:3])
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        outcome = await runner.run_source(sm, sid, http=http)
    assert outcome.status == "ok" and outcome.error is None
    assert outcome.counts["new"] == outcome.counts["errors"] == 0
    assert outcome.counts["dropped_outside_area"] == 2
    async with sm() as session:
        for model in (Opportunity, Org, CompObservation, Task):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        assert (await session.get(Source, sid)).consecutive_failures == 0
        assert (await session.get(ScrapeRun, outcome.scrape_run_id)).counts == outcome.counts


@pytest.mark.parametrize("location", ["Toronto, Ontario, CA", "Bangalore, Karnataka, India"])
async def test_manual_paste_of_foreign_role_is_stored(sm: SessionMaker, location: str) -> None:
    _profile("employment", ["US"])
    text = f"Operations Analyst\nLocation: {location}\nLead operations and plan team work."
    async with sm() as session:
        role = await manual_ingest(session, text=text, title="Operations Analyst")
        assert role is not None
        role_id = role.id
    async with sm() as session:
        stored = await session.get(Opportunity, role_id)
        assert stored.description_md == text and stored.stage == "new"
        assert (await session.get(Source, stored.source_id)).kind == "manual"
        assert await session.scalar(select(func.count()).select_from(Task)) == 1
