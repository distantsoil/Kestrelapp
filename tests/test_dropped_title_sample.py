"""D-SRC-17: the first 25 titles the smart filter drops are kept in the stored ScrapeRun counts
(title and org name only, each cut at 200 characters), never in the returned outcome. All source
data is fake and nothing touches the network."""

from __future__ import annotations

from unittest.mock import AsyncMock

import yaml

from kestrel import paths
from kestrel.db.models import ScrapeRun, Source
from kestrel.ingest import runner
from kestrel.ingest.base import RawPosting
from kestrel.ingest.http import HttpClient
from kestrel.services import diagnostics

KEY = "dropped_title_sample"


def _profile(targets: list[str], geo: dict | None = None) -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("profile.yml").write_text(
        yaml.safe_dump(
            {
                "employment": {
                    "employment_terms": {"target_titles": targets},
                    "geo_rules": geo or {"remote_country_allowlist": ["US"]},
                }
            }
        ),
        encoding="utf-8",
    )


async def _run(sm, monkeypatch, postings: list[RawPosting]):
    class Fake:
        def validate_config(self, config: dict) -> None:
            pass

        async def fetch(self, config: dict, http: HttpClient) -> list[RawPosting]:
            return postings

    monkeypatch.setattr(runner, "get_adapter", lambda kind: Fake())
    monkeypatch.setattr(runner.board_discovery, "discover_boards", AsyncMock(return_value=0))
    async with sm() as session:
        source = Source(track="employment", kind="rss", name="Fake sample", config={}, enabled=True)
        session.add(source)
        await session.commit()
        sid = source.id
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        outcome = await runner.run_source(sm, sid, http=http)
    async with sm() as session:
        stored = (await session.get(ScrapeRun, outcome.scrape_run_id)).counts
    return outcome, stored


def _posting(i: int, title: str, org: str | None = None) -> RawPosting:
    return RawPosting(
        external_id=str(i),
        title=title,
        org_name=org if org is not None else f"Fictional Org {i}",
        description_md="A fictional posting with a private description.",
        location_raw="Remote",
        url=f"https://example.test/jobs/{i}",
    )


def test_constants_and_diagnostics_key_agree():
    assert runner.DROPPED_SAMPLE_KEY == KEY
    assert runner.DROPPED_SAMPLE_CAP == 25
    assert diagnostics._TITLE_SAMPLE_KEY == runner.DROPPED_SAMPLE_KEY


async def test_first_25_dropped_titles_are_stored_in_order(sm, monkeypatch):
    _profile(["Director of IT"])
    postings = [_posting(i, f"Customer Service Associate {i}") for i in range(30)]
    outcome, stored = await _run(sm, monkeypatch, postings)
    assert stored["dropped_unrelated_title"] == 30
    sample = stored[KEY]
    assert len(sample) == 25
    assert [row["title"] for row in sample] == [
        f"Customer Service Associate {i}" for i in range(25)
    ]
    assert sample[3] == {"title": "Customer Service Associate 3", "org": "Fictional Org 3"}
    assert all(set(row) == {"title", "org"} for row in sample)
    assert KEY not in outcome.counts
    assert outcome.counts["dropped_unrelated_title"] == 30


async def test_long_title_and_org_are_cut_at_200(sm, monkeypatch):
    _profile(["Director of IT"])
    postings = [_posting(1, "Customer Service " + "x" * 300, org="O" * 300)]
    _, stored = await _run(sm, monkeypatch, postings)
    row = stored[KEY][0]
    assert len(row["title"]) == 200 and row["title"].startswith("Customer Service ")
    assert len(row["org"]) == 200


async def test_missing_org_is_stored_as_none(sm, monkeypatch):
    _profile(["Director of IT"])
    posting = RawPosting(
        external_id="1",
        title="Customer Service Associate",
        org_name=None,
        description_md="A fictional posting.",
        location_raw="Remote",
    )
    _, stored = await _run(sm, monkeypatch, [posting])
    assert stored[KEY] == [{"title": "Customer Service Associate", "org": None}]


async def test_no_title_drops_means_no_key(sm, monkeypatch):
    _profile(["Director of IT"])
    postings = [_posting(i, "Director of Technology") for i in range(3)]
    outcome, stored = await _run(sm, monkeypatch, postings)
    assert not stored.get("dropped_unrelated_title")
    assert KEY not in stored and KEY not in outcome.counts


async def test_outside_area_drops_are_not_sampled(sm, monkeypatch):
    _profile(
        ["Director of IT"],
        {"local_municipalities": ["Springfield"], "remote_country_allowlist": ["US"]},
    )
    postings = [
        RawPosting(
            external_id=str(i),
            title="Director of Technology",
            org_name=f"Fictional Org {i}",
            description_md="A fictional posting.",
            location_raw="Berlin, Germany",
        )
        for i in range(4)
    ]
    outcome, stored = await _run(sm, monkeypatch, postings)
    assert stored.get("dropped_outside_area") == 4  # the drops really happened
    assert KEY not in stored and KEY not in outcome.counts
    assert not stored.get("dropped_unrelated_title")


async def test_outcome_counts_never_carry_the_sample(sm, monkeypatch):
    _profile(["Director of IT"])
    postings = [_posting(i, "Pastry Chef") for i in range(3)]
    outcome, stored = await _run(sm, monkeypatch, postings)
    assert len(stored[KEY]) == 3
    assert KEY not in outcome.counts
    assert "Pastry Chef" not in repr(outcome.counts)
