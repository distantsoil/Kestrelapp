"""A restore brings back finished follow-up drafts as inert history, and nothing else of the
queue (D-SEC-20 amendment).

INVARIANT: the only task rows a backup can bring back are finished follow-up drafts, the newest per
application, holding only the fields the Follow up dialog reads (no consent, model or route field),
and no restored task is ever leased by the worker. Invented text only; no network."""

from __future__ import annotations

import asyncio
import json
import sqlite3

from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.db.engine import get_sessionmaker, reset_engine
from kestrel.db.models import Pursuit, Task
from kestrel.queue import TaskQueue
from kestrel.services import follow_up as drafts
from kestrel.services.backup import create_backup, restore_backup
from kestrel.web.app import build_app
from tests import apps_seed
from tests.test_applications_screen import _run
from tests.test_follow_up import loader, settings_for

BODY = "An invented follow-up body about the role."


def _tasks() -> list[tuple[str, str, dict]]:
    con = sqlite3.connect(str(paths.db_path()))
    try:
        return [(t, s, json.loads(p)) for t, s, p in
                con.execute("SELECT type, status, payload FROM task ORDER BY id")]
    finally:
        con.close()


def test_a_restored_follow_up_draft_is_shown_and_nothing_restored_is_leased(monkeypatch, tmp_path):
    ids = apps_seed.seed()
    pid = ids["Senior Operations Analyst"]
    loader(monkeypatch, settings_for())

    async def save(session):
        facts = await drafts.facts_for(session, await session.get(Pursuit, pid))
        base = {"pursuit_id": pid, "facts": facts, "route": {"model": "vendor/invented"},
                "consented_model": "vendor/invented", "online_approval": {"tier3": ["x"]}}
        session.add(Task(type="follow_up", status="done", payload={
            **base, "result": {"subject": "Older", "body": "older draft", "checks": []}}))
        session.add(Task(type="follow_up", status="done", payload={
            **base, "result": {"subject": "Checking in", "body": BODY, "checks": []}}))
        for status in ("pending", "leased", "failed", "dead"):
            session.add(Task(type="follow_up", status=status, payload={
                **base, "requested": True,
                "result": {"subject": "Not finished", "body": "never shown", "checks": []}}))
        session.add(Task(type="t2", status="done", payload={"opportunity_id": 1}))
        await session.commit()

    _run(save)
    archive = create_backup(tmp_path)
    _run(lambda s: s.execute(Task.__table__.delete()))
    asyncio.run(reset_engine())

    result = restore_backup(archive)

    rows = _tasks()
    assert [(t, s) for t, s, _p in rows] == [("follow_up", "done")]
    payload = rows[0][2]
    assert set(payload) <= set(drafts_fields())
    assert payload["result"]["body"] == BODY
    assert result.tasks_not_restored == 6
    with TestClient(build_app(worker=False)) as client:
        html = client.get(f"/pursuits/{pid}/follow-up").text
    assert BODY in html and "Copy draft" in html

    async def lease_everything():
        return await TaskQueue(get_sessionmaker()).lease("w", lease_seconds=60)

    assert asyncio.run(lease_everything()) is None
    asyncio.run(reset_engine())


def drafts_fields() -> tuple[str, ...]:
    from kestrel.services.backup import FOLLOW_UP_FIELDS

    return FOLLOW_UP_FIELDS
