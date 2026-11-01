"""D-SRC-13 title screening at the runner boundary. All source data is fake."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import yaml
from sqlalchemy import func, select

from kestrel import paths
from kestrel.db.models import Opportunity, Org, Pursuit, ScrapeRun, Source, Task
from kestrel.ingest import runner, smart_filter
from kestrel.ingest.base import RawPosting
from kestrel.ingest.http import HttpClient
from kestrel.ingest.manual import manual_ingest


def _profile(track: str, targets: list[str]) -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("profile.yml").write_text(yaml.safe_dump({track: {
        "employment_terms": {"target_titles": targets},
        "geo_rules": {"remote_country_allowlist": ["US"]},
    }}), encoding="utf-8")


@pytest.mark.parametrize("kind", ["greenhouse", "jobspy", "rss", "sec_edgar"])
@pytest.mark.parametrize("track", ["employment", "consulting"])
async def test_titles_are_screened_for_every_source_and_track(sm, monkeypatch, kind, track):
    _profile(track, ["Director of IT"])
    titles = ["Customer Service Associate", "Director of Technology", "IT Wizard", "Director",
              "", None]
    postings = [RawPosting(external_id=str(i), title=title, org_name=f"Fictional Org {i}",
                           description_md="A fictional posting.", location_raw="Remote")
                for i, title in enumerate(titles)]

    class Fake:
        def validate_config(self, config: dict) -> None:
            pass

        async def fetch(self, config: dict, http: HttpClient) -> list[RawPosting]:
            return postings

    monkeypatch.setattr(runner, "get_adapter", lambda kind: Fake())
    monkeypatch.setattr(runner.board_discovery, "discover_boards", AsyncMock(return_value=0))
    build = AsyncMock(wraps=smart_filter.build_context)
    monkeypatch.setattr(smart_filter, "build_context", build)
    async with sm() as session:
        source = Source(track=track, kind=kind, name="Fake titles", config={}, enabled=True)
        session.add(source)
        await session.commit()
        sid = source.id
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        first = await runner.run_source(sm, sid, http=http)
        again = await runner.run_source(sm, sid, http=http)
    assert build.await_count == 2  # once per run, not once per posting
    assert first.status == again.status == "ok"
    assert first.counts["new"] == again.counts["reseen"] == 5
    assert first.counts["dropped_unrelated_title"] == again.counts["dropped_unrelated_title"] == 1
    assert first.counts["errors"] == again.counts["errors"] == 0
    async with sm() as session:
        assert set(await session.scalars(select(Opportunity.external_id))) == set("12345")
        assert await session.scalar(select(func.count()).select_from(Org)) == 5
        assert await session.scalar(select(func.count()).select_from(Task)) == 5
        stored = dict((await session.get(ScrapeRun, first.scrape_run_id)).counts)
        # D-SRC-17: the dropped-title sample is kept on the stored row only, never the outcome.
        sample = stored.pop(runner.DROPPED_SAMPLE_KEY)
        assert stored == first.counts and runner.DROPPED_SAMPLE_KEY not in first.counts
        assert len(sample) == 1 and set(sample[0]) == {"title", "org"}


async def test_manual_unrelated_title_bypasses_ingest_filter(sm):
    _profile("employment", ["Director of IT"])
    async with sm() as session:
        role = await manual_ingest(session, title="Customer Service Associate",
                                   text="Help customers with their orders.")
        oid = role.id
    async with sm() as session:
        assert (await session.get(Opportunity, oid)).title == "Customer Service Associate"


async def test_learned_pursuit_words_and_missing_comparison_keep_titles(sm):
    _profile("employment", ["Director of IT"])
    async with sm() as session:
        for _i in range(2):
            opp = Opportunity(track="employment", opp_type="job_posting", title="Pastry Chef")
            session.add(opp)
            await session.flush()
            session.add(Pursuit(track="employment", opportunity_id=opp.id, status="applied"))
        await session.commit()
        ctx = await smart_filter.build_context(
            session, "employment", runner.load_profile("employment")
        )
        assert smart_filter.keep("Pastry Chef", ctx)
        empty = await smart_filter.build_context(session, "consulting", None)
        assert smart_filter.keep("Customer Service Associate", empty)
