"""Remotive adapter tests. No network."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.config import get_settings
from kestrel.db.engine import make_async_engine
from kestrel.db.models import Opportunity, ScrapeRun, Setting, Source
from kestrel.ingest import fetch_cap
from kestrel.ingest.adapters.remotive import RemotiveAdapter
from kestrel.ingest.base import SourceConfigError
from kestrel.ingest.http import HttpClient
from kestrel.ingest.runner import run_source

_BASE = "https://remotive.com/api/remote-jobs"


async def _fetch(config: dict | None = None):
    s = get_settings().ingest
    cfg = config or {}
    async with HttpClient(
        user_agent=s.user_agent, respect_robots=False, max_attempts=1
    ) as client:
        return await RemotiveAdapter().fetch(cfg, client)


def _payload() -> dict:
    """A real API payload captured 2026-11-05 (tests/fixtures/remotive_jobs.json), with every
    name, title and text replaced by invented ones."""
    path = Path(__file__).parent / "fixtures" / "remotive_jobs.json"
    return json.loads(path.read_text(encoding="utf-8"))


@respx.mock
async def test_remotive_maps_jobs_correctly():
    respx.get(f"{_BASE}?limit=50").mock(return_value=httpx.Response(200, json=_payload()))

    batch = await _fetch()

    assert len(batch) == 3
    p0 = batch[0]
    assert p0.title == "Platform Engineer"
    assert p0.org_name == "Sample Harbor Co"
    assert p0.location_raw == "USA Only"
    assert p0.is_remote is True
    assert p0.description_html == "<p>Run the sample platform.</p><ul><li>Invented duty</li></ul>"
    assert p0.date_posted == "2026-11-01T09:30:00"
    assert p0.url.startswith("https://remotive.com/remote-jobs/software-development/")
    assert p0.external_id == "900001"
    assert p0.raw == {"via": "Remotive", "via_url": "https://remotive.com"}

    p2 = batch[2]
    assert p2.title == "Support Lead"
    assert p2.description_html is None
    assert p2.external_id == "900003"


@respx.mock
async def test_remotive_handles_429():
    respx.get(f"{_BASE}?limit=50").mock(return_value=httpx.Response(429))

    batch = await _fetch()

    assert len(batch) == 0
    assert batch.partial_error is not None
    assert "rate limit" in batch.partial_error.lower()


@respx.mock
async def test_remotive_handles_empty_jobs():
    respx.get(f"{_BASE}?limit=50").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )

    batch = await _fetch()

    assert len(batch) == 0
    assert batch.partial_error is None


@respx.mock
async def test_remotive_fallback_external_id():
    """Jobs without an id use the url as the dedup key."""
    job = {k: v for k, v in _payload()["jobs"][0].items() if k != "id"}
    respx.get(f"{_BASE}?limit=50").mock(
        return_value=httpx.Response(200, json={"jobs": [job]})
    )

    batch = await _fetch()

    assert len(batch) == 1
    assert batch[0].external_id == job["url"]


@respx.mock
async def test_remotive_deduplicates_by_id():
    first = _payload()["jobs"][0]
    again = {**first, "title": "Duplicate"}
    respx.get(f"{_BASE}?limit=50").mock(
        return_value=httpx.Response(200, json={"jobs": [first, again]})
    )

    batch = await _fetch()

    assert len(batch) == 1
    assert batch[0].title == "Platform Engineer"


def test_validate_config_rejects_bad_limit():
    adapter = RemotiveAdapter()
    with pytest.raises(SourceConfigError, match="limit"):
        adapter.validate_config({"limit": 0})
    with pytest.raises(SourceConfigError, match="limit"):
        adapter.validate_config({"limit": 101})
    with pytest.raises(SourceConfigError, match="limit"):
        adapter.validate_config({"limit": True})
    # Valid values pass
    adapter.validate_config({"limit": 1})
    adapter.validate_config({"limit": 100})
    adapter.validate_config({})


# ----------------------------------------------------------------------------- D-SRC-15
# The named robots.txt exception, attribution, the 4-a-day cap (stored, so a restart keeps it),
# and unreadable replies. No network: respx answers every request.

_JOB = _payload()["jobs"][0]


async def _make_source(sm) -> int:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("profile.yml").write_text(
        "employment:\n  employment_terms:\n    target_titles: [Platform Engineer]\n",
        encoding="utf-8",
    )
    async with sm() as s:
        src = Source(track="employment", kind="remotive", name="Remotive", config={}, enabled=True)
        s.add(src)
        await s.commit()
        return src.id


async def _run_once(sm, sid):
    async with HttpClient(user_agent="test", respect_robots=False, max_attempts=1) as http:
        return await run_source(sm, sid, http=http)


@respx.mock
async def test_robots_txt_is_not_consulted_for_the_documented_api():
    robots = respx.get("https://remotive.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /api/\n")
    )
    api = respx.get(f"{_BASE}?limit=50").mock(
        return_value=httpx.Response(200, json={"jobs": [_JOB]})
    )
    async with HttpClient(
        user_agent=get_settings().ingest.user_agent, respect_robots=True, max_attempts=1
    ) as client:
        batch = await RemotiveAdapter().fetch({}, client)
    assert api.call_count == 1 and not robots.called
    assert len(batch) == 1 and batch.partial_error is None


@respx.mock
async def test_each_job_names_remotive_as_its_source_and_links_back():
    respx.get(f"{_BASE}?limit=50").mock(return_value=httpx.Response(200, json={"jobs": [_JOB]}))
    batch = await _fetch()
    p = batch[0]
    assert p.raw["via"] == "Remotive" and p.raw["via_url"] == "https://remotive.com"
    assert p.url == _JOB["url"] and p.url.startswith("https://remotive.com/")


@respx.mock
async def test_a_stored_role_carries_the_remotive_credit_and_link(sm):
    respx.get(f"{_BASE}?limit=50").mock(return_value=httpx.Response(200, json={"jobs": [_JOB]}))
    sid = await _make_source(sm)
    outcome = await _run_once(sm, sid)
    assert outcome.status == "ok" and outcome.counts["new"] == 1
    async with sm() as s:
        opp = (await s.execute(select(Opportunity))).scalars().one()
        src = await s.get(Source, sid)
    assert opp.url == _JOB["url"] and src.kind == "remotive"


@pytest.mark.parametrize(
    "response",
    [httpx.Response(403), httpx.Response(429), httpx.Response(503)],
)
@respx.mock
async def test_a_refused_reply_is_a_partial_run_not_a_crash(response):
    respx.get(f"{_BASE}?limit=50").mock(return_value=response)
    batch = await _fetch()
    assert len(batch) == 0
    assert batch.partial_error and "Remotive" in batch.partial_error


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"jobs": "oops"}),
        httpx.Response(200, json=["a", "list"]),
        httpx.Response(200, json={"job-count": 0}),
    ],
)
@respx.mock
async def test_an_unreadable_reply_is_an_adapter_error_like_every_other_board(response):
    """D-SRC-19: shape drift is never an empty success (the other adapters raise the same)."""
    from kestrel.ingest.base import AdapterError

    respx.get(f"{_BASE}?limit=50").mock(return_value=response)
    with pytest.raises(AdapterError, match="could not read"):
        await _fetch()


@respx.mock
async def test_an_unreachable_remotive_is_an_adapter_error_sentence():
    from kestrel.ingest.base import AdapterError

    respx.get(f"{_BASE}?limit=50").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(AdapterError, match="Could not reach Remotive"):
        await _fetch()


@respx.mock
async def test_the_fifth_fetch_in_a_local_day_is_refused_even_after_a_restart(sm):
    api = respx.get(f"{_BASE}?limit=50").mock(
        return_value=httpx.Response(200, json={"jobs": [_JOB]})
    )
    sid = await _make_source(sm)
    for _ in range(4):
        assert (await _run_once(sm, sid)).status == "ok"
    assert api.call_count == 4

    # A restart: a brand-new engine on the same database file, nothing carried in memory.
    db_file = sm.kw["bind"].url.database
    engine = make_async_engine(db_file)
    try:
        fresh = async_sessionmaker(engine, expire_on_commit=False)
        outcome = await _run_once(fresh, sid)
        async with fresh() as s:
            last = (await s.execute(
                select(ScrapeRun).order_by(ScrapeRun.id.desc()).limit(1))).scalars().one()
    finally:
        await engine.dispose()
    assert outcome.status == "partial"
    assert api.call_count == 4  # the fifth never reached the network
    assert last.error and "4 times a day" in last.error and "tomorrow" in last.error


@respx.mock
async def test_the_count_starts_again_on_the_next_local_day(sm, monkeypatch):
    api = respx.get(f"{_BASE}?limit=50").mock(
        return_value=httpx.Response(200, json={"jobs": [_JOB]})
    )
    sid = await _make_source(sm)
    for _ in range(4):
        await _run_once(sm, sid)
    assert (await _run_once(sm, sid)).status == "partial" and api.call_count == 4
    tomorrow = datetime.now().astimezone() + timedelta(days=1)
    monkeypatch.setattr(fetch_cap, "_now", lambda: tomorrow)
    assert (await _run_once(sm, sid)).status == "ok" and api.call_count == 5
    async with sm() as s:
        rec = (await s.get(Setting, "fetch_cap:remotive")).value
    assert rec == {"day": tomorrow.date().isoformat(), "count": 1}
    json.dumps(rec)  # plain JSON, stored in the database row


async def test_two_overlapping_reservations_at_three_used_let_exactly_one_through(sm):
    import asyncio

    async def take() -> bool:
        async with sm() as s:
            return await fetch_cap.reserve(s, "remotive", 4)

    for _ in range(3):
        assert await take()
    results = await asyncio.gather(take(), take())
    assert sorted(results) == [False, True]
    async with sm() as s:
        assert await fetch_cap.used_today(s, "remotive") == 4


@respx.mock
async def test_two_remotive_source_rows_share_one_daily_cap_of_four(sm):
    api = respx.get(f"{_BASE}?limit=50").mock(
        return_value=httpx.Response(200, json={"jobs": [_JOB]})
    )
    first = await _make_source(sm)
    async with sm() as s:
        second_src = Source(track="employment", kind="remotive", name="Remotive 2", config={},
                            enabled=True)
        s.add(second_src)
        await s.commit()
        second = second_src.id
    for sid in (first, second, first, second):
        assert (await _run_once(sm, sid)).status == "ok"
    assert (await _run_once(sm, first)).status == "partial"
    assert (await _run_once(sm, second)).status == "partial"
    assert api.call_count == 4
