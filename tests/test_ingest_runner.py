"""run_source: end-to-end greenhouse (respx), broken config -> partial (no abort), and
auto-disable after three consecutive failures."""

from __future__ import annotations

import json
from pathlib import Path

import respx
from httpx import Response
from sqlalchemy import func, select

from kestrel.db.models import CompObservation, Notification, Opportunity, ScrapeRun, Source, Task
from kestrel.ingest.http import HttpClient
from kestrel.ingest.runner import run_source

_GH = json.loads((Path(__file__).parent / "fixtures" / "greenhouse.json").read_text())


async def _mk_source(sm, **kw) -> int:
    fields = {"track": "employment", "kind": "greenhouse", "name": "s"}
    fields["config"] = {}
    fields["enabled"] = True
    fields.update(kw)
    async with sm() as s:
        src = Source(**fields)
        s.add(src)
        await s.flush()
        sid = src.id
        await s.commit()
    return sid


@respx.mock
async def test_run_source_greenhouse_end_to_end(sm):
    respx.get("https://boards-api.greenhouse.io/v1/boards/meridian/jobs").mock(
        return_value=Response(200, json=_GH)
    )
    sid = await _mk_source(sm, config={"token": "meridian"}, name="gh")
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        outcome = await run_source(sm, sid, http=http)

    assert outcome.status == "ok"
    assert outcome.counts["new"] == 2
    async with sm() as s:
        opps = (await s.execute(select(Opportunity))).scalars().all()
        assert len(opps) == 2 and all(o.org_id is not None for o in opps)
        embeds = (await s.execute(select(Task).where(Task.type == "embed"))).scalars().all()
        assert len(embeds) == 2  # one embed enqueued per opportunity
        comps = (await s.execute(select(func.count()).select_from(CompObservation))).scalar()
        assert comps >= 1  # the VP posting has a parsed comp range


async def test_broken_config_produces_partial_run_without_aborting(sm):
    sid = await _mk_source(sm, config={}, name="bad")  # greenhouse with no token
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        outcome = await run_source(sm, sid, http=http)
    assert outcome.status == "partial"
    assert outcome.error and "token" in outcome.error.lower()
    async with sm() as s:
        src = await s.get(Source, sid)
        assert src.consecutive_failures == 1 and src.enabled is True  # not disabled after one


@respx.mock
async def test_one_bad_posting_is_counted_and_the_rest_are_stored(sm, monkeypatch):
    """A posting that fails AFTER a statement ran inside the per-posting transaction forces a
    rollback, which expires every loaded instance. The runner must still finish the run (status
    partial, finished_at set), count the failure, and store the surviving postings; it must not
    trip over the expired Source (MissingGreenlet) and abort the whole source."""
    import kestrel.ingest.runner as runner_mod

    respx.get("https://boards-api.greenhouse.io/v1/boards/meridian/jobs").mock(
        return_value=Response(200, json=_GH)
    )
    sid = await _mk_source(sm, config={"token": "meridian"}, name="gh")
    real = runner_mod.normalize_and_store
    calls = {"n": 0}

    async def _flaky(session, raw, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            await session.execute(select(Source))  # a statement has executed in this transaction
            raise RuntimeError("boom on the first posting")
        return await real(session, raw, **kw)

    monkeypatch.setattr(runner_mod, "normalize_and_store", _flaky)
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        outcome = await run_source(sm, sid, http=http)

    assert outcome.status == "partial"
    assert outcome.counts["errors"] == 1
    assert outcome.counts["new"] == 1
    assert "boom" in (outcome.error or "")
    async with sm() as s:
        run = await s.get(ScrapeRun, outcome.scrape_run_id)
        assert run.status == "partial" and run.finished_at is not None
        n = (await s.execute(select(func.count()).select_from(Opportunity))).scalar()
        assert n == 1  # the surviving posting was stored
        src = await s.get(Source, sid)
        assert src.consecutive_failures == 0 and src.enabled is True


async def test_auto_disable_after_three_failures(sm):
    sid = await _mk_source(sm, config={}, name="bad")
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        for _ in range(3):
            await run_source(sm, sid, http=http)
    async with sm() as s:
        src = await s.get(Source, sid)
        assert src.enabled is False and src.consecutive_failures >= 3
        n = (
            await s.execute(
                select(func.count())
                .select_from(Notification)
                .where(Notification.kind == "source_disabled")
            )
        ).scalar()
        assert n == 1


@respx.mock
async def test_an_empty_successful_search_clears_the_failing_state(sm):
    """D-UI-33: a search that worked but found nothing is a success. Without the reset the rail
    and Today kept naming it as failing after the fault was fixed."""
    respx.get("https://boards-api.greenhouse.io/v1/boards/meridian/jobs").mock(
        return_value=Response(200, json={"jobs": []})
    )
    sid = await _mk_source(sm, config={"token": "meridian"}, name="gh", consecutive_failures=2)
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        outcome = await run_source(sm, sid, http=http)
    assert outcome.status == "ok"
    async with sm() as s:
        assert (await s.get(Source, sid)).consecutive_failures == 0
