"""Issue #75: a build that makes story drafts tells the operator, through the ordinary
notification path (bell, pop-up, Quiet hours, per-kind switch), once, in the build's own commit."""

from __future__ import annotations

from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Notification, Story
from kestrel.interview.stories import DRAFTS_NOTIFY_KIND, consolidate_stories
from kestrel.services.delivery import deliver_pending, kind_enabled
from kestrel.web import notifications_view as nv
from kestrel.web import settings_view as sv
from tests.test_alert_sound import NIGHT, _at, _settings
from tests.test_interview_stories import _add_eval, _FakeClient, _seed_opp, _sketch


async def _notes(sm) -> list[Notification]:
    async with sm() as s:
        return list(
            (await s.execute(select(Notification).where(Notification.kind == DRAFTS_NOTIFY_KIND)))
            .scalars().all()
        )


async def _build(sm):
    async with sm() as s:
        return await consolidate_stories(s, _FakeClient(), get_settings())


async def test_a_build_with_drafts_writes_one_notice_with_the_count(sm):
    opp = await _seed_opp(sm)
    await _add_eval(sm, opp, [_sketch("Turnaround?", "delta")])
    await _add_eval(sm, opp, [_sketch("Hard call?", "omega")])
    res = await _build(sm)
    assert len(res.created_story_ids) == 2
    notes = await _notes(sm)
    assert [n.title for n in notes] == ["2 new story drafts to review"]
    assert notes[0].severity == "info" and notes[0].delivered_channels == []
    assert nv.context_action(DRAFTS_NOTIFY_KIND, None) == {
        "label": "Review drafts", "href": "/stories", "external": False,
    }


async def test_one_draft_reads_in_the_singular(sm):
    opp = await _seed_opp(sm)
    await _add_eval(sm, opp, [_sketch("Turnaround?", "delta")])
    await _build(sm)
    assert [n.title for n in await _notes(sm)] == ["1 new story draft to review"]


async def test_a_build_with_nothing_new_writes_none(sm):
    await _build(sm)
    assert await _notes(sm) == []


async def test_an_attached_only_build_writes_none(sm):
    opp = await _seed_opp(sm)
    async with sm() as s:
        s.add(Story(
            title="My delta rescue", situation="The delta platform nearly took us down.",
            task="Keep us alive.", action="I did the hard part.", result="We survived.",
            reflection="My lesson.", themes=["turnaround"], is_canonical=True,
            operator_edited=True, use_count=1,
        ))
        await s.commit()
    await _add_eval(sm, opp, [_sketch("Another rescue?", "delta")])
    res = await _build(sm)
    assert res.created_story_ids == [] and len(res.attached) == 1
    assert await _notes(sm) == []


async def test_rerunning_a_build_does_not_duplicate(sm):
    opp = await _seed_opp(sm)
    await _add_eval(sm, opp, [_sketch("Turnaround?", "delta")])
    await _build(sm)
    await _build(sm)
    await _build(sm)
    assert len(await _notes(sm)) == 1


async def test_the_notice_rides_the_normal_delivery_path(sm):
    opp = await _seed_opp(sm)
    await _add_eval(sm, opp, [_sketch("Turnaround?", "delta")])
    await _build(sm)
    # Quiet hours hold it, like any kind.
    async with sm() as s:
        held = await deliver_pending(s, _settings(NIGHT), clock=_at(23, 30))
    assert held.sent == 0 and held.held == 1
    assert (await _notes(sm))[0].delivered_channels == []
    # Outside the window it is delivered.
    async with sm() as s:
        out = await deliver_pending(s, _settings(NIGHT), clock=_at(12, 0))
    assert out.sent == 1 and out.collapsed == 1


def test_the_settings_page_has_a_switch_for_it_on_by_default():
    settings = _settings({})
    assert DRAFTS_NOTIFY_KIND in sv.NOTIFY_KINDS["stories"][2]
    assert sv.notify_on(settings, "stories") is True
    assert kind_enabled(settings, DRAFTS_NOTIFY_KIND) is True
    settings.notify.events = {DRAFTS_NOTIFY_KIND: False}
    assert sv.notify_on(settings, "stories") is False
    assert kind_enabled(settings, DRAFTS_NOTIFY_KIND) is False
