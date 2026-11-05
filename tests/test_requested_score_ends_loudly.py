"""A Score again never ends silently (issue #144, hard rule 4). When Tier 2 skips a role the
operator asked to score because its posting is empty, the role stays Not scored (never rejected),
and the end of the request shows on the row, the role page and, once, in the next Pipeline page's
notice strip. The model is faked; nothing reaches a network. Invented text only."""

from __future__ import annotations

import asyncio
import html as htmllib

import pytest
from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Evaluation, Notification, Opportunity, Task
from kestrel.pipeline.worker import Worker
from kestrel.services.score_again_notice import KIND, NOT_SCORED_WHY
from kestrel.web.fit import EMPTY_POSTING_STAGE_REASON
from tests import test_pipeline_view as T
from tests.test_not_scored_reasons import _db, _reasons
from tests.test_score_again import _drain, _opp, _tasks


async def _empty_role(sm) -> int:
    oid = await _opp(sm, stage="t1_pass", title="Head of Platform")
    async with sm() as s:
        o = await s.get(Opportunity, oid)
        o.description_md = ""
        o.stage_reason = EMPTY_POSTING_STAGE_REASON
        await s.commit()
    return oid


async def test_tier2_skip_of_a_requested_empty_role_records_a_note_and_changes_no_stage(sm):
    oid = await _empty_role(sm)
    async with sm() as s:  # asked for, but the task lacks score_stub (an older request)
        s.add(Task(type="t2", payload={"opportunity_id": oid, "requested": True}, priority=1000))
        await s.commit()
    settings = get_settings()
    from tests.test_score_again import _RecordingLLM

    llm = _RecordingLLM(settings)
    await _drain(Worker(sm, llm, settings), "t2")
    assert llm.models == []  # the model was never asked
    async with sm() as s:
        opp = await s.get(Opportunity, oid)
        notes = (await s.execute(select(Notification).where(Notification.kind == KIND))
                 ).scalars().all()
        evals = (await s.execute(select(Evaluation))).scalars().all()
    assert len(notes) == 1 and notes[0].opportunity_id == oid and notes[0].read_at is None
    assert opp.stage == "t1_pass" and opp.archived_at is None  # Not scored, never Not a fit
    assert evals == [] and len(await _tasks(sm)) == 1


async def test_an_automatic_skip_stays_quiet(sm):
    oid = await _empty_role(sm)
    async with sm() as s:
        s.add(Task(type="t2", payload={"opportunity_id": oid, "changed": True}, priority=1))
        await s.commit()
    settings = get_settings()
    from tests.test_score_again import _RecordingLLM

    await _drain(Worker(sm, _RecordingLLM(settings), settings), "t2")
    async with sm() as s:
        assert (await s.execute(select(Notification))).scalars().all() == []


async def test_a_second_skip_while_the_first_note_is_unread_adds_no_second_note(sm):
    oid = await _empty_role(sm)
    from kestrel.services.score_again_notice import record_not_scored

    async with sm() as s:
        assert await record_not_scored(s, oid) is True
        assert await record_not_scored(s, oid) is False
        await s.commit()
        assert len((await s.execute(select(Notification))).scalars().all()) == 1


def test_the_row_the_role_page_and_the_next_notice_all_say_why_and_the_notice_shows_once():
    asyncio.run(T._seed())
    ids = _db(_reasons)
    oid = ids["empty"]

    async def ask(s):
        from kestrel.services.score_again_notice import record_not_scored

        await record_not_scored(s, oid)
        await s.commit()

    _db(ask)
    with T._client() as c:
        first = htmllib.unescape(T._get(c, "/opportunities"))
        again = htmllib.unescape(T._get(c, "/opportunities"))
        role = htmllib.unescape(c.get(f"/opportunities/{oid}").text)
    assert 'data-testid="pl-notice"' in first
    assert "1 role you asked to score was not scored" in first
    assert NOT_SCORED_WHY in T._row(first, oid)  # the row's own reason
    assert 'data-testid="pl-notice"' not in again  # shown once
    assert NOT_SCORED_WHY in T._row(again, oid)  # the row keeps saying it
    assert NOT_SCORED_WHY in role
    assert 'data-fit="not a fit"' not in T._row(first, oid)  # never reads as a rejection


def test_a_role_nobody_asked_about_keeps_the_plain_reason():
    asyncio.run(T._seed())
    ids = _db(_reasons)
    with T._client() as c:
        page = htmllib.unescape(T._get(c, "/opportunities"))
    row = T._row(page, ids["empty"])
    assert NOT_SCORED_WHY not in row and "The posting has no description." in row
    assert 'data-testid="pl-notice"' not in page


@pytest.mark.parametrize("n", [2])
def test_several_ended_requests_make_one_short_notice(n):
    asyncio.run(T._seed())
    ids = _db(_reasons)

    async def ask(s):
        from kestrel.services.score_again_notice import record_not_scored

        for oid in (ids["empty"], ids["waiting"]):
            await record_not_scored(s, oid)
        await s.commit()

    _db(ask)
    with T._client() as c:
        page = htmllib.unescape(c.get("/opportunities").text)
    assert f"{n} roles you asked to score were not scored" in page
    assert page.count('data-testid="pl-notice"') == 1


async def test_delivery_never_pushes_the_in_app_note(sm, monkeypatch):
    """D-UI-244 amended: the note is in app only. A delivery cycle with every channel on sends
    nothing for it (no toast, ntfy or webhook) and no Settings switch is needed."""
    from kestrel.services import delivery
    from kestrel.services.score_again_notice import record_not_scored

    oid = await _empty_role(sm)
    async with sm() as s:
        await record_not_scored(s, oid)
        await s.commit()
    sent: list[tuple[str, str | None]] = []

    async def fake_dispatch(settings, title, body):
        sent.append((title, body))
        return ["toast"]

    monkeypatch.setattr(delivery, "_dispatch", fake_dispatch)
    settings = get_settings()
    settings.notify.enabled = True
    settings.notify.quiet_hours = {"enabled": False, "start": "21:00", "end": "07:00"}
    async with sm() as s:
        res = await delivery.deliver_pending(s, settings)
    assert res.sent == 0 and sent == []
    async with sm() as s:
        note = (await s.execute(select(Notification).where(Notification.kind == KIND))
                ).scalars().one()
    assert note.delivered_channels == ["in_app"]  # untouched by delivery, still listed in the bell


def test_reading_the_note_in_the_bell_does_not_hide_the_pipeline_strip():
    asyncio.run(T._seed())
    ids = _db(_reasons)

    async def ask_then_read(s):
        from kestrel.db.base import utcnow_iso
        from kestrel.services.score_again_notice import record_not_scored

        await record_not_scored(s, ids["empty"])
        await s.commit()
        for n in (await s.execute(select(Notification).where(Notification.kind == KIND))
                  ).scalars():
            n.read_at = utcnow_iso()  # the bell got there first
        await s.commit()

    _db(ask_then_read)
    with T._client() as c:
        first = htmllib.unescape(c.get("/opportunities").text)
        again = htmllib.unescape(c.get("/opportunities").text)
    assert "1 role you asked to score was not scored" in first
    assert 'data-testid="pl-notice"' not in again


async def test_a_second_skip_after_the_strip_showed_adds_no_second_unread_bell_note(sm):
    from kestrel.services.score_again_notice import record_not_scored, take_notice

    oid = await _empty_role(sm)
    async with sm() as s:
        await record_not_scored(s, oid)
        await s.commit()
    async with sm() as s:
        assert await take_notice(s) is not None  # the strip showed it; the bell copy is unread
        await s.commit()
    async with sm() as s:
        assert await record_not_scored(s, oid) is False
        await s.commit()
    async with sm() as s:
        notes = (await s.execute(select(Notification).where(Notification.kind == KIND))
                 ).scalars().all()
    assert len(notes) == 1 and notes[0].read_at is None
    # Once the bell copy is read, a later skip is a new note again.
    async with sm() as s:
        (await s.get(Notification, notes[0].id)).read_at = "2026-11-04T00:00:00+00:00"
        await s.commit()
    async with sm() as s:
        assert await record_not_scored(s, oid) is True
        await s.commit()
        assert len((await s.execute(select(Notification).where(Notification.kind == KIND))
                    ).scalars().all()) == 2
        assert (await take_notice(s)) is not None  # the new one shows on the strip
