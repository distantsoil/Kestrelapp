"""A task held for the person's online OK shows "Waiting for your OK" with a link to confirm on
the Pipeline row, the panel and the Today-style state, never a blank (issue #143). The hold does
not count in line and does not keep the page polling. Nothing is sent: a held task is released
only by the confirmed dialog. Cloud-safe; invented text only."""

from __future__ import annotations

import asyncio
import html as htmllib

from sqlalchemy import select

from kestrel.db.models import Task
from kestrel.services.online_consent import APPROVAL_HOLD
from kestrel.web.rescoring import APPROVAL_TITLE, rescoring_states
from tests import test_pipeline_view as T
from tests.test_not_scored_reasons import _db, _reasons


def _hold(task_type: str, opp_id: int, **payload):
    async def go(s):
        s.add(Task(type=task_type, priority=1000, last_error=APPROVAL_HOLD,
                   payload={"opportunity_id": opp_id, "rescore": True, **payload}))
        await s.commit()

    _db(go)


def test_a_held_task_is_its_own_state_not_in_line_and_not_blank():
    asyncio.run(T._seed())
    ids = _db(_reasons)

    async def states(s):
        return await rescoring_states(s, [ids["waiting"], ids["queued"]])

    base_ahead = _db(states)[ids["queued"]].ahead
    _hold("t2", ids["waiting"])
    found = _db(states)
    held = found[ids["waiting"]]
    assert held.state == "approval" and held.title == APPROVAL_TITLE == "Waiting for your OK"
    assert "Nothing was sent" in held.hint and held.ahead == 0
    # The held task is not counted ahead of the role that is genuinely waiting.
    assert found[ids["queued"]].ahead == base_ahead


def test_the_pipeline_row_and_panel_show_the_state_and_a_confirm_link_that_sends_nothing():
    asyncio.run(T._seed())
    ids = _db(_reasons)
    oid = ids["waiting"]
    _hold("t2", oid)
    with T._client() as c:
        page = htmllib.unescape(T._get(c, "/opportunities"))
        panel = htmllib.unescape(c.get(f"/opportunities/{oid}/panel").text)
    row = T._row(page, oid)
    assert 'data-scoring="approval"' in row and "Waiting for your OK" in row
    assert f'href="/rescore/choose?opp_ids={oid}&page=1&next=/opportunities"' in row
    assert 'data-testid="panel-confirm"' in panel and "Waiting for your OK" in panel

    async def after(s):
        found = await s.execute(select(Task).where(Task.last_error == APPROVAL_HOLD))
        return found.scalars().all()

    assert len(_db(after)) == 1  # nothing released by looking at the page


def test_a_scored_role_with_a_held_rescore_names_the_state_on_its_row():
    asyncio.run(T._seed())
    ids = asyncio.run(_scored_id())
    _hold("t2", ids)
    with T._client() as c:
        page = htmllib.unescape(T._get(c, "/opportunities"))
    row = T._row(page, ids)
    assert 'data-state="approval"' in row and "Waiting for your OK" in row
    assert 'data-testid="row-confirm"' in row


async def _scored_id() -> int:
    async def pick(s):
        from kestrel.db.models import Opportunity

        return (await s.execute(
            select(Opportunity.id).where(Opportunity.stage == "t2_done").order_by(Opportunity.id)
        )).scalars().first()

    return await T._with_session(pick)


def test_a_held_task_does_not_start_the_poll():
    asyncio.run(T._seed())
    ids = _db(_reasons)

    async def clear(s):
        from sqlalchemy import delete

        await s.execute(delete(Task))
        await s.commit()

    _db(clear)
    _hold("t2", ids["waiting"])
    with T._client() as c:
        page = c.get("/opportunities").text
    # The list polls only while live work exists; a hold waits for the person, not the worker.
    assert "pipelineRefresh from:body, every 10s" not in page
    assert "pipelineRefresh from:body" in page
