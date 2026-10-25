"""Durable research tasks: no live fetches or models, including cancellation races."""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import delete, select, update

from kestrel.config import get_settings
from kestrel.db.models import (
    Contact,
    Opportunity,
    Org,
    OrgResearch,
    OutreachDraft,
    Quarantine,
    Task,
)
from kestrel.queue.queue import TaskQueue
from kestrel.research import jobs
from kestrel.research.excerpts import Excerpt, ExcerptSet
from tests.test_research_company import _FakeP5LLM, _payload
from tests.test_research_contacts import _EXCERPTS, _PAYLOAD, _FakeClient


async def seed(sm, kind="company", *, settings=None, confirmed=False):
    async with sm() as session:
        org = Org(name="Acme Corp", normalized_name="acme corp")
        session.add(org)
        await session.flush()
        opp = Opportunity(
            track="employment", opp_type="job_posting", title="Operations lead", org_id=org.id
        )
        session.add(opp)
        await session.flush()
        await jobs.enqueue(
            session, opp, kind, settings or get_settings(), confirmed_online=confirmed
        )
        return opp.id, org.id


async def rows(sm, model):
    async with sm() as session:
        return list(await session.scalars(select(model).order_by(model.id)))


def fake_sources(monkeypatch, excerpts=None):
    async def acquire(*args, **kwargs):
        return excerpts if excerpts is not None else ExcerptSet()

    monkeypatch.setattr(jobs, "acquire_excerpts", acquire)


async def test_enqueue_is_atomic_even_for_simultaneous_requests(sm):
    opp_id, _ = await seed(sm)
    async with sm() as session:
        await session.execute(delete(Task))
        await session.commit()

    async def add():
        async with sm() as session:
            opp = await session.get(Opportunity, opp_id)
            return await jobs.enqueue(session, opp, "company", get_settings())

    assert sorted(await asyncio.gather(add(), add())) == [False, True]
    assert len(await rows(sm, Task)) == 1


async def test_expired_lease_resumes_and_completion_is_idempotent(sm, monkeypatch):
    await seed(sm)
    fake_sources(monkeypatch)
    queue = TaskQueue(sm)
    crashed = await queue.lease("crashed", types=[jobs.TYPES[0]])
    async with sm() as session:
        await session.execute(update(Task).values(lease_until="2000-01-01T00:00:00+00:00"))
        await session.commit()
    assert await queue.reclaim_expired() == 1
    assert await jobs.drain(sm) == 1
    assert await jobs.drain(sm) == 0
    assert (await rows(sm, Task))[0].status == "done"
    research = await rows(sm, OrgResearch)
    assert len(research) == 1 and research[0].insufficient_sources
    with pytest.raises(asyncio.CancelledError):
        await jobs._run(sm, crashed)
    assert len(await rows(sm, OrgResearch)) == 1


async def test_real_research_prompt_persists_sources_and_org_signals(sm, monkeypatch):
    await seed(sm)
    fake_sources(
        monkeypatch,
        ExcerptSet(
            [
                Excerpt(1, "https://acme.example/about", "company_page", "Acme grew revenue."),
                Excerpt(2, "https://acme.example/team", "company_page", "Acme has a new team."),
            ]
        ),
    )
    monkeypatch.setattr(
        jobs, "LLMClient", lambda settings, **kwargs: _FakeP5LLM(settings, _payload())
    )
    await jobs.drain(sm)
    research = (await rows(sm, OrgResearch))[0]
    assert not research.insufficient_sources and len(research.sources) == 2
    assert len((await rows(sm, Org))[0].signals) == 1


async def test_contacts_and_drafts_commit_once_with_task(sm, monkeypatch):
    await seed(sm, "contacts")
    fake_sources(monkeypatch, _EXCERPTS)
    monkeypatch.setattr(
        jobs, "LLMClient", lambda *args, **kwargs: _FakeClient(json.dumps(_PAYLOAD))
    )
    await jobs.drain(sm)
    first = await rows(sm, Contact)
    assert first and first[-1].email == "jane.roe@acme.com"
    assert all(d.status == "draft" for d in await rows(sm, OutreachDraft))
    await jobs.drain(sm)
    assert len(await rows(sm, Contact)) == len(first)
    assert (await rows(sm, Task))[0].status == "done"


@pytest.mark.parametrize("kind", ["company", "contacts"])
async def test_invalid_model_reply_quarantines_raw_and_finishes(sm, monkeypatch, kind):
    await seed(sm, kind)
    fake_sources(
        monkeypatch,
        ExcerptSet(
            [
                *_EXCERPTS.excerpts,
                Excerpt(2, "https://acme.example/about", "company_page", "Acme builds things."),
            ]
        ),
    )
    client = _FakeClient("bad json {")
    monkeypatch.setattr(jobs, "LLMClient", lambda *args, **kwargs: client)
    await jobs.drain(sm)
    assert (await rows(sm, Task))[0].status == "done"
    assert "another try" in (await rows(sm, Task))[0].payload["progress"]
    quarantined = await rows(sm, Quarantine)
    assert len(quarantined) == 1 and quarantined[0].raw_response == "bad json {"
    assert quarantined[0].attempts == 2


async def test_cancel_is_scoped_and_does_not_overwrite_a_finished_task(sm, monkeypatch):
    opp_id, _ = await seed(sm)
    task = (await rows(sm, Task))[0]
    async with sm() as session:
        assert not await jobs.cancel(session, opp_id + 1, task.id)
        assert await jobs.cancel(session, opp_id, task.id)
    fake_sources(monkeypatch)
    await jobs.drain(sm)
    assert not await rows(sm, OrgResearch)
    async with sm() as session:
        opp = await session.get(Opportunity, opp_id)
        await jobs.enqueue(session, opp, "company", get_settings())
    await jobs.drain(sm)
    completed = (await rows(sm, Task))[-1]
    async with sm() as session:
        assert not await jobs.cancel(session, opp_id, completed.id)
    assert (await rows(sm, Task))[-1].status == "done"


async def test_cancel_during_model_call_discards_all_uncommitted_output(sm, monkeypatch):
    opp_id, org_id = await seed(sm, "contacts")
    fake_sources(monkeypatch, _EXCERPTS)
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def slow(session, *args, **kwargs):
        session.add(Contact(org_id=org_id, name="Uncommitted person"))
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(jobs, "discover_contacts", slow)
    worker = asyncio.create_task(jobs.drain(sm))
    await asyncio.wait_for(started.wait(), 5)
    task = (await rows(sm, Task))[0]
    async with sm() as session:
        await jobs.cancel(session, opp_id, task.id)
    await asyncio.wait_for(worker, 5)
    assert stopped.is_set() and not await rows(sm, Contact)
    assert (await rows(sm, Task))[0].status == "cancelled"


async def test_shutdown_returns_task_for_restart(sm, monkeypatch):
    await seed(sm)
    started = asyncio.Event()
    stop = asyncio.Event()

    async def slow(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(jobs, "acquire_excerpts", slow)
    worker = asyncio.create_task(jobs.drain(sm, stop=stop))
    await asyncio.wait_for(started.wait(), 5)
    stop.set()
    await asyncio.wait_for(worker, 5)
    task = (await rows(sm, Task))[0]
    assert task.status == "pending" and task.attempts == 0
    fake_sources(monkeypatch)
    await jobs.drain(sm)
    assert (await rows(sm, Task))[0].status == "done"


async def test_backend_failure_retries_then_reports_failure_without_partial_writes(sm, monkeypatch):
    await seed(sm)

    async def broken(*args, **kwargs):
        raise RuntimeError("sensitive provider reply")

    monkeypatch.setattr(jobs, "acquire_excerpts", broken)
    await jobs.drain(sm)
    task = (await rows(sm, Task))[0]
    assert task.status == "dead" and task.attempts == 3
    assert "sensitive" not in task.last_error
    assert not await rows(sm, OrgResearch)


@pytest.mark.parametrize("change", ["model", "url", "approval", "hosted", "employer"])
async def test_worker_holds_changed_or_unapproved_online_destination(sm, monkeypatch, change):
    settings = get_settings().model_copy(deep=True)
    tier = settings.llm.tiers["tier3"]
    backend = settings.llm.backends[tier.backend]
    backend.external = True
    settings.llm.hosted_mode.enabled = True
    settings.confidential_mode = False
    await seed(sm, settings=settings, confirmed=change != "approval")
    current = settings.model_copy(deep=True)
    if change == "model":
        current.llm.tiers["tier3"].model = "different-model"
    elif change == "url":
        current.llm.backends[tier.backend].base_url = "https://different.example/v1"
    elif change == "hosted":
        current.llm.hosted_mode.enabled = False
    elif change == "employer":
        current.confidential_mode = True
        current.llm.hosted_mode.acknowledged_at = "2026-10-24T00:00:00+00:00"
        current.llm.hosted_mode.acknowledged_terms = current.llm.hosted_mode.disclosure_terms()

    async def load(*args, **kwargs):
        return current

    async def never(*args, **kwargs):
        pytest.fail("No public fetch or model call is allowed for a held send")

    monkeypatch.setattr(jobs, "load_settings_with_db", load)
    monkeypatch.setattr(jobs, "acquire_excerpts", never)
    await jobs.drain(sm)
    assert (await rows(sm, Task))[0].status == "held"


async def test_stale_worker_cannot_commit_after_cancel(sm, monkeypatch):
    opp_id, _ = await seed(sm)
    queue = TaskQueue(sm)
    leased = await queue.lease("stale", types=[jobs.TYPES[0]])
    async with sm() as session:
        await jobs.cancel(session, opp_id, leased.id)
    fake_sources(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        await jobs._run(sm, leased)
    assert not await rows(sm, OrgResearch)
    assert (await rows(sm, Task))[0].status == "cancelled"


async def test_settings_change_during_source_fetch_holds_before_model_send(sm, monkeypatch):
    await seed(sm)
    changed = get_settings().model_copy(deep=True)

    async def load(*args, **kwargs):
        return changed

    async def acquire(*args, **kwargs):
        changed.llm.tiers["tier3"].model = "changed-during-fetch"
        return _EXCERPTS

    def never(*args, **kwargs):
        pytest.fail("A changed destination cannot reach the model")

    monkeypatch.setattr(jobs, "load_settings_with_db", load)
    monkeypatch.setattr(jobs, "acquire_excerpts", acquire)
    monkeypatch.setattr(jobs, "LLMClient", never)
    await jobs.drain(sm)
    assert (await rows(sm, Task))[0].status == "held"


async def test_cancellation_at_fetch_boundary_prevents_the_model_call(sm, monkeypatch):
    opp_id, _ = await seed(sm)
    task = (await rows(sm, Task))[0]

    async def acquire(*args, **kwargs):
        async with sm() as session:
            await jobs.cancel(session, opp_id, task.id)
        return _EXCERPTS

    def never(*args, **kwargs):
        pytest.fail("Cancellation before the model call must prevent it")

    monkeypatch.setattr(jobs, "acquire_excerpts", acquire)
    monkeypatch.setattr(jobs, "LLMClient", never)
    await jobs.drain(sm)
    assert (await rows(sm, Task))[0].status == "cancelled"


class _DownClient(_FakeClient):
    async def complete(self, **kwargs):
        raise ConnectionError("model offline")


class _FlakyClient(_FakeClient):
    """Offline for the first call, then answers."""

    calls = 0

    async def complete(self, **kwargs):
        type(self).calls += 1
        if type(self).calls == 1:
            raise ConnectionError("model offline")
        return await super().complete(**kwargs)


def _sources(monkeypatch):
    fake_sources(
        monkeypatch,
        ExcerptSet(
            [
                *_EXCERPTS.excerpts,
                Excerpt(2, "https://acme.example/about", "company_page", "Acme builds things."),
            ]
        ),
    )


@pytest.mark.parametrize("kind", ["company", "contacts"])
async def test_model_transport_failure_uses_the_retry_budget_and_is_reported(
    sm, monkeypatch, kind
):
    """A connection error reaches the queue's failure handling (D-UI-167): the task is retried
    until its attempts are spent, then dead with a plain message. It is never marked done, never
    quarantined, and no empty result is saved as if the work had finished."""
    await seed(sm, kind)
    _sources(monkeypatch)
    monkeypatch.setattr(jobs, "LLMClient", lambda *args, **kwargs: _DownClient(""))
    await jobs.drain(sm)
    task = (await rows(sm, Task))[0]
    assert task.status == "dead" and task.attempts == task.max_attempts > 1
    assert "could not be reached" in task.last_error
    assert not await rows(sm, Quarantine)
    assert not await rows(sm, OrgResearch) and not await rows(sm, Contact)


@pytest.mark.parametrize("kind", ["company", "contacts"])
async def test_model_transport_failure_then_recovery_completes_on_the_retry(
    sm, monkeypatch, kind
):
    await seed(sm, kind)
    _sources(monkeypatch)
    _FlakyClient.calls = 0
    payload = json.dumps(_PAYLOAD) if kind == "contacts" else json.dumps(_payload())
    monkeypatch.setattr(jobs, "LLMClient", lambda *args, **kwargs: _FlakyClient(payload))
    await jobs.drain(sm)
    task = (await rows(sm, Task))[0]
    assert task.status == "done" and task.attempts == 2
