"""D-STORY-2: a story build runs in short passes and saves each group as it finishes.

INVARIANT: nothing already written is sent to the model again, a failed group is not resent by
the same build, the build always finishes, and the drafts notice is sent exactly once. Offline:
the model is a fake (tests/test_interview_stories.py)."""

from __future__ import annotations

from sqlalchemy import func, select

from kestrel.config import get_settings
from kestrel.db.models import Notification, Story, Task
from kestrel.interview import story_task
from kestrel.interview.stories import DRAFTS_NOTIFY_KIND, consolidate_stories
from tests.test_stories_no_lock import _db_file, _ProbingClient, _three_events


def _passes(monkeypatch) -> None:
    """Every pass stops after one group, as an online Tier 2 build does at its deadline."""
    monkeypatch.setattr(story_task, "PASS_SECONDS", 0.0)
    monkeypatch.setattr(story_task, "_use_passes", lambda _settings: True)


class _Recording(_ProbingClient):
    def __init__(self, db_file: str, **kw) -> None:
        super().__init__(db_file, **kw)
        self.sent: list[str] = []

    async def complete(self, **kw):
        self.sent.append(next(m["content"] for m in kw["messages"] if m["role"] == "user").lower())
        return await super().complete(**kw)


async def _task(sm) -> Task:
    async with sm() as s:
        return (await s.execute(select(Task).where(Task.type == story_task.TASK_TYPE))).scalar_one()


async def _count(sm, model) -> int:
    async with sm() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def _notices(sm) -> list[str]:
    async with sm() as s:
        rows = (await s.execute(
            select(Notification).where(Notification.kind == DRAFTS_NOTIFY_KIND))).scalars().all()
    return [n.title for n in rows]


async def test_a_build_gives_the_worker_back_between_groups_and_finishes(sm, monkeypatch):
    _passes(monkeypatch)  # every pass stops after one group
    await _three_events(sm)
    async with sm() as s:
        assert await story_task.request_build(s)
    client = _Recording(_db_file(sm))
    for n in (1, 2):
        assert await story_task.drain_story_builds(sm, client, get_settings()) == 0
        task = await _task(sm)
        assert task.status == "pending" and task.attempts == 0  # given back, not a failure
        assert await _count(sm, Story) == n  # each pass's group is already saved
        assert await _notices(sm) == []  # one notice, at the end
    async with sm() as s:
        assert (await story_task.build_state(s))["state"] == "building"
    assert await story_task.drain_story_builds(sm, client, get_settings()) == 1
    assert (await _task(sm)).status == "done"
    async with sm() as s:
        last = await story_task.last_build(s)
    # Every group handled once: written as a draft, or attached to a draft an earlier pass saved
    # (the fake writes the same story text each time). Only written groups are sent.
    assert last["ok"] is True and last["sketches"] == 3
    assert last["created"] + last["attached"] == 3
    assert len(client.sent) == last["created"] == await _count(sm, Story)
    assert len(await _notices(sm)) == 1


async def test_a_failed_group_is_not_resent_by_the_same_build_only_the_next(sm, monkeypatch):
    _passes(monkeypatch)
    await _three_events(sm)
    async with sm() as s:
        assert await story_task.request_build(s)
    client = _Recording(_db_file(sm), fail_on="omega")
    for _ in range(6):  # more passes than groups: a resend would show up here
        if await story_task.drain_story_builds(sm, client, get_settings()):
            break
    assert (await _task(sm)).status == "done"
    # The merge and its one repair attempt, in one pass; no later pass of this build resends it.
    assert sum("omega" in m for m in client.sent) == 2
    # The next build sends the failed group again, and only it.
    retry = _Recording(_db_file(sm))
    async with sm() as s:
        result = await consolidate_stories(s, retry, get_settings())
    assert len(retry.sent) == 1 and "omega" in retry.sent[0]
    assert len(result.created_story_ids) == 1


async def test_a_pass_that_fails_a_group_then_raises_does_not_resent_it_on_retry(sm, monkeypatch):
    # D-STORY-2 known limit, closed: a pass that dies (raises) after failing a group still records
    # that group in the task's payload, so the retry of the same build does not send it again. The
    # give_back path (a pass that stops on purpose) already did this; the exception path did not.
    await _three_events(sm, order=("omega", "sigma"))
    async with sm() as s:
        assert await story_task.request_build(s)
    import kestrel.interview.stories as _stories

    real_merge = _stories._merge_cluster

    async def raise_on_sigma(session, client, spec, sketches, proof_points, resume=None):
        text = " ".join(sk.text() for sk in sketches).lower()
        if "sigma" in text:
            raise RuntimeError("the model call for this group failed hard")
        return await real_merge(session, client, spec, sketches, proof_points, resume)

    monkeypatch.setattr(_stories, "_merge_cluster", raise_on_sigma)
    client = _Recording(_db_file(sm), fail_on="omega")
    # First pass: omega's merge fails (a bad reply), then sigma's call dies the pass. The build is
    # failed (retryable), and the group that already failed must be recorded for the retry.
    assert await story_task.drain_story_builds(sm, client, get_settings()) == 0
    task = await _task(sm)
    assert task.status == "pending" and task.attempts == 1
    assert task.payload.get(story_task._SKIP_KEY), "the failed group must be recorded for the retry"
    assert sum("omega" in m for m in client.sent) >= 1  # omega was sent (and failed)
    # Rewind the retry backoff (issue #153) so the retry can be leased immediately.
    async with sm() as s:
        t = await s.get(Task, task.id)
        t.retry_at = None
        await s.commit()
    # Retry of the same build: omega is not resent, and sigma is retried and succeeds.
    monkeypatch.setattr(_stories, "_merge_cluster", real_merge)
    retry = _Recording(_db_file(sm))
    assert await story_task.drain_story_builds(sm, retry, get_settings()) == 1
    assert (await _task(sm)).status == "done"
    assert sum("omega" in m for m in retry.sent) == 0, "the failed group must not be resent"
    assert sum("sigma" in m for m in retry.sent) >= 1, "the group that raised is retried"


async def test_after_a_build_in_passes_a_new_build_sends_nothing(sm, monkeypatch):
    # The last pass moves the watermark over the groups earlier passes wrote, so clearing the
    # consumed list never makes them look new.
    _passes(monkeypatch)
    await _three_events(sm)
    async with sm() as s:
        assert await story_task.request_build(s)
    client = _Recording(_db_file(sm))
    while not await story_task.drain_story_builds(sm, client, get_settings()):
        pass
    again = _Recording(_db_file(sm))
    async with sm() as s:
        result = await consolidate_stories(s, again, get_settings())
    assert again.sent == [] and result.sketches == 0


async def test_a_notice_owed_by_a_build_cut_short_goes_out_once_with_the_next(sm):
    await _three_events(sm)
    from tests.test_stories_no_lock import _Killed

    async with sm() as s:
        try:
            await consolidate_stories(s, _ProbingClient(_db_file(sm), explode_on="sigma"),
                                      get_settings())
        except _Killed:
            pass
    written = await _count(sm, Story)
    assert written >= 1 and await _notices(sm) == []
    async with sm() as s:
        await consolidate_stories(s, _ProbingClient(_db_file(sm)), get_settings())
    notices = await _notices(sm)
    assert len(notices) == 1
    async with sm() as s:
        await consolidate_stories(s, _ProbingClient(_db_file(sm)), get_settings())
    assert len(await _notices(sm)) == 1  # never twice


async def test_a_local_build_runs_in_one_pass_and_still_saves_as_it_goes(sm, monkeypatch):
    # A local Tier 2 shares the GPU with scoring: no give-back between groups (drain-one-tier).
    monkeypatch.setattr(story_task, "PASS_SECONDS", 0.0)
    await _three_events(sm)
    async with sm() as s:
        assert await story_task.request_build(s)
    assert await story_task.drain_story_builds(sm, _Recording(_db_file(sm)), get_settings()) == 1
    assert (await _task(sm)).status == "done"


async def test_a_build_held_between_passes_sends_nothing_more_and_still_announces(sm,
                                                                                 monkeypatch):
    # INVARIANT: nothing goes online without a confirmation matching the model configured at the
    # send, on every pass. The confirmation rides in the payload through give_back.
    from kestrel.services.online_consent import CONSENT_KEY
    from tests.test_privacy_review import MODEL
    from tests.test_privacy_review import _online as online_everywhere

    _passes(monkeypatch)
    await _three_events(sm)
    online_everywhere(monkeypatch)  # the settings the drain re-reads: Tier 2 online on MODEL
    async with sm() as s:
        route = await online_route_for(s)
        assert await story_task.request_build(s, consent=consent_for(route))
    client = _Recording(_db_file(sm))
    assert await story_task.drain_story_builds(sm, client, get_settings()) == 0
    task = await _task(sm)
    assert task.status == "pending" and task.payload[CONSENT_KEY] == MODEL  # kept by give_back
    assert len(client.sent) == 1
    online_everywhere(monkeypatch, model="vendor/model-b")  # changed, never confirmed
    await story_task.drain_story_builds(sm, client, get_settings())
    assert len(client.sent) == 1  # held: nothing more was sent
    assert (await _task(sm)).status == "done"
    assert len(await _notices(sm)) == 1  # the draft pass one saved is still announced


async def online_route_for(session):
    from kestrel.services.online_consent import online_route
    from kestrel.services.setting_overrides import load_settings_with_db

    return await online_route(session, await load_settings_with_db(session), "tier2")


def consent_for(route):
    from kestrel.services.online_consent import consent_payload

    return consent_payload(route)


async def test_a_note_edited_after_a_pass_consumed_it_is_read_again(sm, monkeypatch):
    # The watermark covers what earlier passes consumed by the time recorded when they were
    # consumed, so an edit saved later stays above it and the next build reads the new text.
    from kestrel.interview import notes as notes_mod
    from tests.test_interview_stories import _seed_opp

    _passes(monkeypatch)
    # The fake embeds "delta" and "omega" far apart, so each note is its own group.
    texts = {"delta": "I rebuilt the delta rota after an outage.",
             "omega": "I moved the omega ledger to a new vendor in a quarter."}
    from kestrel.db.models import Opportunity

    first_opp = await _seed_opp(sm)
    async with sm() as s:
        base = await s.get(Opportunity, first_opp)
        second = Opportunity(track="employment", opp_type="job_posting", org_id=base.org_id,
                             source_id=base.source_id, title="Director IT", org_name="Acme",
                             url="https://x/2")
        s.add(second)
        await s.commit()
        second_opp = second.id
    opps = {"delta": first_opp, "omega": second_opp}
    for word, text in texts.items():
        async with sm() as s:
            await notes_mod.save_notes(s, opps[word], text)
    async with sm() as s:
        assert await story_task.request_build(s)
    client = _Recording(_db_file(sm))
    assert await story_task.drain_story_builds(sm, client, get_settings()) == 0  # pass one
    assert len(client.sent) == 1
    first = "delta" if "delta" in client.sent[0] else "omega"
    async with sm() as s:  # the operator rewrites the note pass one already turned into a draft
        await notes_mod.save_notes(s, opps[first], f"I rewrote the {first} plan after a review.")
    while not await story_task.drain_story_builds(sm, client, get_settings()):
        pass
    again = _Recording(_db_file(sm))
    async with sm() as s:
        result = await consolidate_stories(s, again, get_settings())
    # The rewritten note is collected again (written as a draft or attached to one; the fake
    # model writes the same story text every time, so it may attach without a call).
    assert result.sketches == 1
    assert len(result.created_story_ids) + len(result.attached) == 1
    assert all("rewrote the" in m for m in again.sent)


async def test_a_pass_quit_cuts_off_keeps_its_failed_groups_for_the_resumed_build(sm, monkeypatch):
    # D-STORY-4: Quit cancels an online pass after it already failed a group. The cancel must
    # record that group in the payload before it goes on, so the build resumed at the next start
    # does not send it again; the quit hands the lease back with the attempt refunded.
    import asyncio

    import kestrel.interview.stories as _stories
    from kestrel.queue.queue import TaskQueue

    await _three_events(sm, order=("omega", "sigma"))
    async with sm() as s:
        assert await story_task.request_build(s)
    real_merge = _stories._merge_cluster
    hanging = asyncio.Event()

    async def hang_on_sigma(session, client, spec, sketches, proof_points, resume=None):
        text = " ".join(sk.text() for sk in sketches).lower()
        if "sigma" in text:
            hanging.set()
            await asyncio.Event().wait()  # the online call Quit cuts off
        return await real_merge(session, client, spec, sketches, proof_points, resume)

    monkeypatch.setattr(_stories, "_merge_cluster", hang_on_sigma)
    client = _Recording(_db_file(sm), fail_on="omega")
    build = asyncio.create_task(story_task.drain_story_builds(sm, client, get_settings()))
    await asyncio.wait_for(hanging.wait(), timeout=10)
    build.cancel()
    await asyncio.gather(build, return_exceptions=True)
    assert await TaskQueue(sm).release_all_leased() == 1
    task = await _task(sm)
    assert task.status == "pending" and task.attempts == 0
    assert task.payload.get(story_task._SKIP_KEY), "the failed group must survive the cut-off"
    monkeypatch.setattr(_stories, "_merge_cluster", real_merge)
    retry = _Recording(_db_file(sm))
    assert await story_task.drain_story_builds(sm, retry, get_settings()) == 1
    assert sum("omega" in m for m in retry.sent) == 0, "the failed group must not be resent"
    assert sum("sigma" in m for m in retry.sent) >= 1
