"""P17 drafts are durable, consent checked, confidential and never delivered."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Opportunity, Pursuit, Quarantine
from kestrel.llm.client import ChatResult
from kestrel.privacy import Employer, EmployerMap
from kestrel.services import follow_up as drafts
from kestrel.web.app import build_app
from tests import apps_seed

DRAFT = {"subject": "Checking in", "body": "Could you share an update on next steps?",
         "checks": ["Review the role title before using this draft."]}


class FakeClient:
    def __init__(self, settings, replies=None):
        self.settings = settings
        self.replies = iter(replies or [json.dumps(DRAFT)])
        self.calls = []

    async def complete(self, *, tier, messages, schema=None, **kwargs):
        self.calls.append({"messages": messages, "schema": schema, **kwargs})
        return ChatResult(content=next(self.replies), model=self.settings.llm.tiers[tier].model,
                          prompt_tokens=1, completion_tokens=1, latency_ms=1)


def settings_for(online=False):
    settings = get_settings().model_copy(deep=True)
    settings.confidential_mode = False
    if online:
        backend = settings.llm.backends[settings.llm.tiers["tier3"].backend].model_copy(deep=True)
        backend.external = True
        backend.base_url = "https://models.example.test/v1"
        settings.llm.backends["test-online"] = backend
        settings.llm.tiers["tier3"].backend = "test-online"
        settings.llm.tiers["tier3"].model = "test-model"
        hm = settings.llm.hosted_mode
        hm.enabled, hm.acknowledged_at = True, "2026-10-24T00:00:00Z"
        hm.acknowledged_terms = hm.disclosure_terms()
    return settings


async def seed(sm, track="employment"):
    async with sm() as session:
        opp = Opportunity(track=track, opp_type="job_posting", title="Analyst",
                          org_name="Example Employer")
        session.add(opp)
        await session.flush()
        pursuit = Pursuit(opportunity_id=opp.id, track=track,
                          status="applied" if track == "employment" else "outreach_sent",
                          next_action="Follow up", next_action_at=date.today().isoformat())
        session.add(pursuit)
        await session.commit()
        return pursuit.id


async def queue(sm, pid, settings, **overrides):
    async with sm() as session:
        pursuit = await session.get(Pursuit, pid)
        fields = {
            "route_stamp": drafts.stamp(drafts.routing(settings)),
            "facts_stamp": drafts.stamp(await drafts.facts_for(session, pursuit)),
            "model": settings.llm.tiers["tier3"].model, "confirmed": "1",
            **overrides,
        }
        error = await drafts.request_draft(
            session, pursuit, settings, drafts.DraftRequest(**fields),
        )
        await session.commit()
        return error


async def stored(sm, pid):
    async with sm() as session:
        return await drafts.latest(session, pid)


def loader(monkeypatch, settings):
    async def load(_session):
        return settings.model_copy(deep=True)

    monkeypatch.setattr(drafts, "load_settings_with_db", load)
    monkeypatch.setattr("kestrel.web.pages.pursuits.load_settings_with_db", load)
    monkeypatch.setattr("kestrel.services.setting_overrides.load_settings_with_db", load)


@pytest.mark.parametrize("track", ["employment", "consulting"])
async def test_queue_survives_restart_stores_once_and_leaves_pursuit_unchanged(
    sm, monkeypatch, track,
):
    settings = settings_for()
    loader(monkeypatch, settings)
    pid = await seed(sm, track)
    assert await queue(sm, pid, settings) is None
    assert await queue(sm, pid, settings) is None
    first = await stored(sm, pid)
    client = FakeClient(settings)
    assert await drafts.drain(sm, lambda _cfg: client) == 1
    assert await drafts.drain(sm, lambda _cfg: client) == 0
    done = await stored(sm, pid)
    assert done.id == first.id and done.status == "done" and done.payload["result"] == DRAFT
    assert len(client.calls) == 1
    assert client.calls[0]["schema"]["required"] == ["subject", "body", "checks"]
    assert "Example Employer" in client.calls[0]["messages"][1]["content"]
    assert await queue(sm, pid, settings) is None
    assert (await stored(sm, pid)).id == first.id
    async with sm() as session:
        pursuit = await session.get(Pursuit, pid)
        assert pursuit.next_action == "Follow up" and pursuit.closed_at is None
        assert pursuit.status == ("applied" if track == "employment" else "outreach_sent")


@pytest.mark.parametrize("fields", [{"confirmed": ""}, {"model": "stale"},
                                   {"route_stamp": "stale"}, {"facts_stamp": "stale"}])
async def test_online_enqueue_refuses_missing_or_stale_consent(sm, fields):
    settings = settings_for(True)
    pid = await seed(sm)
    assert await queue(sm, pid, settings, **fields)
    assert await stored(sm, pid) is None


@pytest.mark.parametrize("change", ["model", "backend", "hosted_off", "facts", "closed", "done"])
async def test_worker_rechecks_consent_and_due_facts_after_queue(sm, monkeypatch, change):
    settings = settings_for(True)
    loader(monkeypatch, settings)
    pid = await seed(sm)
    assert await queue(sm, pid, settings) is None
    if change == "model":
        settings.llm.tiers["tier3"].model = "changed-model"
    elif change == "backend":
        settings.llm.backends["test-online"].base_url = "https://changed.example.test/v1"
    elif change == "hosted_off":
        settings.llm.hosted_mode.enabled = False
    else:
        async with sm() as session:
            pursuit = await session.get(Pursuit, pid)
            if change == "facts":
                pursuit.next_action = "Follow up again"
            elif change == "closed":
                pursuit.closed_at = date.today().isoformat()
            else:
                pursuit.next_action = None
            await session.commit()
    client = FakeClient(settings)
    await drafts.drain(sm, lambda _cfg: client)
    assert not client.calls
    assert (await stored(sm, pid)).payload["error"]


async def test_confidential_send_is_held_or_encoded_even_with_legacy_redaction_off(sm, monkeypatch):
    settings = settings_for(True)
    settings.confidential_mode = True
    settings.llm.hosted_mode.redact_confidential = False
    loader(monkeypatch, settings)
    pid = await seed(sm)
    async def empty(_session, **_kwargs):
        return EmployerMap()
    monkeypatch.setattr(drafts, "load_employer_map", empty)
    assert "current employer" in await queue(sm, pid, settings)
    assert await stored(sm, pid) is None
    mapping = EmployerMap([Employer("Example Employer", "Current employer")])
    async def mapped(_session, **_kwargs):
        return mapping
    monkeypatch.setattr(drafts, "load_employer_map", mapped)
    monkeypatch.setattr("kestrel.docs.current_employer.load_employer_map", mapped)
    assert await queue(sm, pid, settings) is None
    client = FakeClient(settings)
    await drafts.drain(sm, lambda _cfg: client)
    assert len(client.calls) == 1
    assert "Example Employer" not in json.dumps(client.calls)


async def test_invalid_reply_repairs_once_then_keeps_raw_quarantine(sm, monkeypatch):
    settings = settings_for()
    loader(monkeypatch, settings)
    pid = await seed(sm)
    await queue(sm, pid, settings)
    client = FakeClient(settings, ['{"body": "missing subject"}', '{"subject": "missing body"}'])
    await drafts.drain(sm, lambda _cfg: client)
    assert len(client.calls) == 2
    assert (await stored(sm, pid)).payload["error"]
    async with sm() as session:
        bad = (await session.execute(select(Quarantine).where(Quarantine.tier == 17))).scalar_one()
        assert "missing body" in bad.raw_response and bad.attempts == 2


async def test_expired_lease_resumes(sm, monkeypatch):
    settings = settings_for()
    loader(monkeypatch, settings)
    pid = await seed(sm)
    await queue(sm, pid, settings)
    async with sm() as session:
        task = await drafts.latest(session, pid)
        task.status, task.worker_id, task.lease_until = "leased", "crashed", "2000-01-01"
        task.attempts = 1
        await session.commit()
    client = FakeClient(settings)
    assert await drafts.drain(sm, lambda _cfg: client) == 1
    assert (await stored(sm, pid)).payload["result"] == DRAFT


@pytest.mark.parametrize("change", ["future", "interview", "closed"])
async def test_only_due_follow_ups_can_queue(sm, change):
    pid = await seed(sm)
    async with sm() as session:
        pursuit = await session.get(Pursuit, pid)
        if change == "future":
            pursuit.next_action_at = (date.today() + timedelta(days=7)).isoformat()
        elif change == "interview":
            pursuit.next_action = "Interview"
        else:
            pursuit.status = "rejected"
        await session.commit()
    assert await queue(sm, pid, settings_for())
    assert await stored(sm, pid) is None


def form_fields(html):
    soup = BeautifulSoup(html, "html.parser")
    return {node["name"]: node.get("value", "")
            for node in soup.select('input[type="hidden"]')}


@pytest.mark.parametrize("online", [False, True])
def test_routes_show_disclosure_require_confirmation_and_poll_persisted_task(monkeypatch, online):
    ids = apps_seed.seed()
    pid = ids["Senior Operations Analyst"]
    settings = settings_for(online)
    loader(monkeypatch, settings)
    with TestClient(build_app(worker=False)) as client:
        board = client.get("/pursuits").text
        assert f'data-follow-up="{pid}"' in board
        path = f"/pursuits/{pid}/follow-up"
        dialog = client.get(path).text
        fields = form_fields(dialog)
        if online:
            sends = settings.llm.hosted_mode.disclosure_terms()["sends"]["tier3"]
            assert sends[0].upper() + sends[1:] in dialog
            assert "models.example.test" in dialog and "test-model" in dialog
            assert "Confirm the online draft" in client.post(path, data=fields).text
            fields["confirmed"] = "1"
        else:
            assert "written on your computer" in dialog
            assert 'name="confirmed"' not in dialog
        queued = client.post(path, data=fields)
        assert queued.status_code == 200 and "Draft queued" in queued.text
        assert "every 2s" in queued.text
        assert "Draft queued" in client.get(path).text
        assert "no longer available" in client.get("/pursuits/99999/follow-up").text


def test_schema_rejects_blank_draft_and_em_dashes():
    from pydantic import ValidationError

    from kestrel.llm.schemas import P17FollowUp

    for bad in ({**DRAFT, "body": " "}, {**DRAFT, "subject": "Hello\u2014again"}):
        with pytest.raises(ValidationError):
            P17FollowUp.model_validate(bad)
    assert len(P17FollowUp.model_validate({**DRAFT, "body": "x" * 1500}).body) == 1200


async def test_transport_failure_stops_after_three_attempts(sm, monkeypatch):
    settings = settings_for()
    loader(monkeypatch, settings)
    pid = await seed(sm)
    await queue(sm, pid, settings)

    class Unavailable(FakeClient):
        async def complete(self, **kwargs):
            raise ConnectionError("offline")

    await drafts.drain(sm, lambda cfg: Unavailable(cfg))
    task = await stored(sm, pid)
    assert task.status == "dead" and task.attempts == 3
    assert "offline" not in task.last_error


async def test_hosted_off_and_missing_employer_refused_at_execution(sm, monkeypatch):
    settings = settings_for(True)
    loader(monkeypatch, settings)
    pid = await seed(sm)
    await queue(sm, pid, settings)
    settings.confidential_mode = True
    client = FakeClient(settings)
    await drafts.drain(sm, lambda _cfg: client)
    assert not client.calls
    assert "current employer" in (await stored(sm, pid)).payload["error"]


def test_saved_draft_is_escaped_and_confidential_when_reopened(monkeypatch):
    from tests.test_applications_screen import _run

    ids = apps_seed.seed()
    pid = ids["Senior Operations Analyst"]
    settings = settings_for()
    settings.confidential_mode = True
    loader(monkeypatch, settings)
    mapping = EmployerMap([Employer("Secret Employer", "Current employer")])

    async def mapped(_session, **_kwargs):
        return mapping

    monkeypatch.setattr("kestrel.docs.current_employer.load_employer_map", mapped)

    async def save(session):
        from kestrel.db.models import Task

        facts = await drafts.facts_for(session, await session.get(Pursuit, pid))
        session.add(Task(type="follow_up", status="done", payload={
            "pursuit_id": pid, "facts": facts,
            "result": {"subject": "Secret Employer", "body": "</textarea><script>bad()</script>",
                       "checks": ["Secret Employer"]},
        }))
        await session.commit()

    _run(save)
    with TestClient(build_app(worker=False)) as client:
        html = client.get(f"/pursuits/{pid}/follow-up").text
    assert "Secret Employer" not in html and "Current employer" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "Copy draft" in html and "Write the draft" not in html


def test_notification_opens_matching_due_draft_and_generic_reminder_stays_generic():
    from kestrel.db.models import Notification
    from tests.test_applications_screen import _run

    ids = apps_seed.seed()
    pid = ids["Senior Operations Analyst"]

    async def save(session):
        pursuit = await session.get(Pursuit, pid)
        session.add(Notification(kind="next_action_due", severity="info", title="Follow up",
                                 pursuit_id=pid))
        await session.commit()
        return pursuit.opportunity_id

    oid = _run(save)
    with TestClient(build_app(worker=False)) as client:
        panel = client.get("/notifications/list").text
        assert "Draft follow-up" in panel and f"/pursuits?follow_up={oid}" in panel
        assert f'data-auto-draft="{pid}"' in client.get(f"/pursuits?follow_up={oid}").text

        async def change(session):
            pursuit = await session.get(Pursuit, pid)
            pursuit.next_action = "Finish cover letter"
            await session.commit()

        _run(change)
        assert "Draft follow-up" not in client.get("/notifications/list").text
        assert "data-auto-draft" not in client.get(f"/pursuits?follow_up={oid}").text


def test_saved_draft_is_reused_only_while_its_facts_still_match(monkeypatch):
    """Generate a draft, move the pursuit on (stage and next action change), and reopen: the old
    draft is not shown; a fresh request is, with the new facts. Unchanged facts still reuse it."""
    from tests.test_applications_screen import _run

    ids = apps_seed.seed()
    pid = ids["Senior Operations Analyst"]
    settings = settings_for()
    loader(monkeypatch, settings)

    async def save(session):
        from kestrel.db.models import Task

        facts = await drafts.facts_for(session, await session.get(Pursuit, pid))
        session.add(Task(type="follow_up", status="done", payload={
            "pursuit_id": pid, "facts": facts, "route": drafts.routing(settings),
            "result": {"subject": "Old subject", "body": "Old application draft.", "checks": []},
        }))
        await session.commit()

    async def advance(session):
        pursuit = await session.get(Pursuit, pid)
        pursuit.status = "interviewing"
        pursuit.next_action = "Follow up after interview"
        pursuit.next_action_at = date.today().isoformat()
        await session.commit()

    _run(save)
    path = f"/pursuits/{pid}/follow-up"
    with TestClient(build_app(worker=False)) as client:
        same = client.get(path).text
        assert "Old application draft." in same and "Write the draft" not in same
        _run(advance)
        later = client.get(path).text
    assert "Old application draft." not in later
    assert "Write the draft" in later and "Interview" in later
