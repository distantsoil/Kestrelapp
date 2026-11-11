"""Issue #154: a held or deferred task is never leased while its hold lasts, and is leased exactly
once the moment the hold ends.

`claimable()` now excludes the queue's own deferred mark (D-QUEUE-3), so a held task costs the
queue nothing for as long as the hold lasts: no lease, no defer, no attempt spent. `release_holds`
is the one owner of the mark: it clears it when the hold's own condition ends (the confirmation
now covers the destination, the tiers no longer score on hosted, the credit hold is gone, the stop
is over) or after the safety sweep interval, so a missed event cannot strand work (hard rule 5).
Nothing is sent from here: a released task still passes its hold's checks at run time.

All data is invented; nothing touches the network. The sweep is an absolute interval from the
defer, so no period-boundary seed is needed (D-TEST-2).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from kestrel.config import BackendConfig, Settings, get_settings
from kestrel.db.base import iso
from kestrel.llm import credit
from kestrel.pipeline import lane
from kestrel.queue.queue import (
    REASON_APPROVAL,
    REASON_CREDIT,
    REASON_LOCAL_ONLY,
    REASON_STOPPED,
    TaskQueue,
    claimable,
    in_flight,
    release_holds,
)

_KEY = "key-for-test"


def _settings(monkeypatch) -> Settings:
    """Tier 1 on the external test backend, Hosted mode active: the approval and local-only
    holds are live, and the credit hold can be marked and cleared in process memory."""
    monkeypatch.setenv("OPENMODEL_API_KEY", _KEY)
    s = get_settings().model_copy(deep=True)
    s.llm.backends["frontier"] = BackendConfig(
        thinking_control="none", external=True, provider="openmodel",
        base_url="https://api.openmodel.ai/v1", api_key_env="OPENMODEL_API_KEY",
        protocol="responses",
    )
    s.llm.tiers["tier1"].backend = "frontier"
    s.llm.tiers["tier1"].model = "some-model"
    s.llm.hosted_mode.enabled = True
    s.llm.hosted_mode.acknowledged_at = "2026-11-01T10:00:00+00:00"
    s.llm.hosted_mode.acknowledged_terms = s.llm.hosted_mode.disclosure_terms()
    return s


async def _hold(q: TaskQueue, reason: str, payload: dict, *, note: str = "held") -> int:
    """The worker's exact path: lease the task, then give it back unstarted for the hold."""
    tid = await q.enqueue("t1", payload)
    task = await q.lease("worker-test", types=["t1"])
    assert task is not None and task.id == tid
    assert await q.defer(tid, note, worker_id="worker-test", reason=reason) == "pending"
    return tid


async def _age(sm, tid: int, seconds: float) -> None:
    """Age the row's updated_at so the sweep interval has passed (no event arrived)."""
    from sqlalchemy import update

    from kestrel.db.models import Task

    async with sm() as s:
        await s.execute(
            update(Task).where(Task.id == tid).values(
                updated_at=iso(datetime.now(UTC) - timedelta(seconds=seconds))
            )
        )
        await s.commit()


async def _restamp(sm, tid: int, approval: dict) -> None:
    from kestrel.db.base import utcnow_iso
    from kestrel.db.models import Task

    async with sm() as s:
        task = await s.get(Task, tid)
        task.payload = {**(task.payload or {}), "online_approval": approval}
        task.updated_at = utcnow_iso()
        await s.commit()


async def _rows(sm) -> list:
    from sqlalchemy import select

    from kestrel.db.models import Task

    async with sm() as s:
        rows = (await s.execute(select(Task).order_by(Task.id))).scalars().all()
    return [
        (t.id, t.status, t.attempts, t.last_error, t.payload, t.updated_at) for t in rows
    ]


def test_ten_drain_cycles_lease_no_held_task_and_write_nothing(monkeypatch):
    """The acceptance test: with three held tasks (one per hold), ten drain cycles lease none
    of them and write nothing to them."""
    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker

    s = _settings(monkeypatch)
    credit.mark_out_of_credit("frontier", "openmodel", _KEY)

    async def go():
        migrate.run_migrations()
        sm = get_sessionmaker()
        q = TaskQueue(sm)
        await _hold(q, REASON_APPROVAL,
                    {"opportunity_id": 1,
                     "online_approval": {"tier1": ["stale-dest"]}})
        await _hold(q, REASON_CREDIT, {"opportunity_id": 2})
        await _hold(q, REASON_LOCAL_ONLY, {"opportunity_id": 3})
        before = await _rows(sm)
        assert all(r[3] is not None and r[4] for r in before)
        for i in range(10):
            assert await release_holds(sm, s) == 0
            assert await q.lease(f"drain-{i}", types=["t1"]) is None
        after = await _rows(sm)
        assert after == before  # nothing leased, nothing written, ten cycles running
        return after

    asyncio.run(go())
    credit.clear()


def test_a_approval_held_task_runs_the_moment_the_confirmation_covers(monkeypatch):
    """Releasing the hold (the confirmation now covers the actual destination) makes the task
    claimable immediately, and it is leased exactly once."""
    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker
    from kestrel.services.online_consent import approval_for

    s = _settings(monkeypatch)

    async def go():
        migrate.run_migrations()
        sm = get_sessionmaker()
        q = TaskQueue(sm)
        tid = await _hold(q, REASON_APPROVAL,
                          {"opportunity_id": 1, "online_approval": {"tier1": ["stale-dest"]}})
        assert await release_holds(sm, s) == 0
        assert await q.lease("w", types=["t1"]) is None
        await _restamp(sm, tid, approval_for(s, ("tier1",)))
        assert await release_holds(sm, s) == 1
        got = await q.lease("w", types=["t1"])
        assert got is not None and got.id == tid
        assert got.last_error is None
        assert "deferred" not in (got.payload or {})
        assert "defer_reason" not in (got.payload or {})
        assert await q.lease("w2", types=["t1"]) is None  # already leased: not claimed again

    asyncio.run(go())


def test_a_local_only_held_task_runs_when_the_tiers_go_local(monkeypatch):
    """Releasing the hold (the tiers no longer score on hosted) makes the task claimable
    immediately."""
    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker

    s = _settings(monkeypatch)
    assert lane.scores_on_hosted(s) is True

    async def go():
        migrate.run_migrations()
        sm = get_sessionmaker()
        q = TaskQueue(sm)
        tid = await _hold(q, REASON_LOCAL_ONLY, {"opportunity_id": 1})
        assert await release_holds(sm, s) == 0
        s.llm.tiers["tier1"].backend = "local"  # the operator switched it back to this computer
        assert lane.scores_on_hosted(s) is False
        assert await release_holds(sm, s) == 1
        got = await q.lease("w", types=["t1"])
        assert got is not None and got.id == tid and got.last_error is None

    asyncio.run(go())


def test_a_credit_held_task_runs_when_the_credit_hold_clears(monkeypatch):
    """Releasing the hold (the credit hold is gone) makes the task claimable immediately."""
    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker

    s = _settings(monkeypatch)
    credit.mark_out_of_credit("frontier", "openmodel", _KEY)

    async def go():
        migrate.run_migrations()
        sm = get_sessionmaker()
        q = TaskQueue(sm)
        tid = await _hold(q, REASON_CREDIT, {"opportunity_id": 1})
        assert await release_holds(sm, s) == 0
        credit.clear()
        assert await release_holds(sm, s) == 1
        got = await q.lease("w", types=["t1"])
        assert got is not None and got.id == tid and got.last_error is None

    asyncio.run(go())


def test_a_local_only_hold_keeps_a_task_whose_override_still_scores_hosted(monkeypatch):
    """The worker's hold test covers a task's `model_override` (D-UI-50), so the release must
    too: a local-only held task whose Score again still names a hosted model stays marked, and
    is released when that override can no longer run, when the worker's own fallback puts it
    back on the configured (local) model."""
    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker

    s = _settings(monkeypatch)
    s.llm.tiers["tier1"].backend = "local"  # the configured tier is on this computer
    s.llm.tiers["tier1"].model = "kestrel-tier1"
    assert lane.scores_on_hosted(s) is False  # the hold's condition is met for the tier

    async def go():
        migrate.run_migrations()
        sm = get_sessionmaker()
        q = TaskQueue(sm)
        tid = await _hold(q, REASON_LOCAL_ONLY,
                          {"opportunity_id": 1, "rescore": True,
                           "model_override": {"backend": "frontier", "model": "some-model"}})
        assert await release_holds(sm, s) == 0  # the override still scores hosted
        assert await q.lease("w", types=["t1"]) is None
        s.llm.hosted_mode.enabled = False  # the hosted override can no longer run
        assert await release_holds(sm, s) == 1  # the fallback is local: the hold has ended
        got = await q.lease("w", types=["t1"])
        assert got is not None and got.id == tid and got.last_error is None

    asyncio.run(go())


def test_an_approval_hold_keeps_a_task_whose_override_destination_is_unconfirmed(monkeypatch):
    """The confirmation covers the configured destination, but the task's `model_override`
    (D-UI-50) names a different one: the worker holds it, so the release must not clear the
    mark. Stamping the override's destination (as the worker stamps its own) ends the hold."""
    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker
    from kestrel.services.online_consent import approval_for, merge_approvals

    s = _settings(monkeypatch)
    s.llm.backends["frontier2"] = BackendConfig(
        thinking_control="none", external=True, provider="openmodel",
        base_url="https://api2.openmodel.ai/v1", api_key_env="OPENMODEL_API_KEY",
        protocol="responses",
    )
    override = {"backend": "frontier2", "model": "some-model"}
    s2 = s.model_copy(deep=True)  # the task's effective settings, as the worker resolves them
    s2.llm.tiers["tier1"].backend = "frontier2"
    s2.llm.tiers["tier1"].model = "some-model"

    async def go():
        migrate.run_migrations()
        sm = get_sessionmaker()
        q = TaskQueue(sm)
        tid = await _hold(q, REASON_APPROVAL,
                          {"opportunity_id": 1, "rescore": True, "model_override": override,
                           "online_approval": approval_for(s, ("tier1",))})
        assert await release_holds(sm, s) == 0  # the override's destination is unconfirmed
        assert await q.lease("w", types=["t1"]) is None
        await _restamp(sm, tid, merge_approvals(
            approval_for(s, ("tier1",)), approval_for(s2, ("tier1",))))
        assert await release_holds(sm, s) == 1
        got = await q.lease("w", types=["t1"])
        assert got is not None and got.id == tid and got.last_error is None

    asyncio.run(go())


def test_the_sweep_releases_a_still_held_task_after_its_interval(monkeypatch):
    """The safety sweep (hard rule 5): a deferred task older than the interval is released for a
    trial lease even when its hold is still live and no event arrived."""
    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker

    s = _settings(monkeypatch)
    credit.mark_out_of_credit("frontier", "openmodel", _KEY)

    async def go():
        migrate.run_migrations()
        sm = get_sessionmaker()
        q = TaskQueue(sm)
        tid = await _hold(q, REASON_CREDIT, {"opportunity_id": 1})
        assert await release_holds(sm, s) == 0
        await _age(sm, tid, seconds=90)  # past the 60 s sweep, hold still live
        assert await release_holds(sm, s) == 1
        got = await q.lease("w", types=["t1"])
        assert got is not None and got.id == tid

    asyncio.run(go())
    credit.clear()


def test_a_deferred_task_is_live_work_but_not_claimable(monkeypatch):
    """The display invariant (#143): a deferred task stays out of `claimable` while it keeps a
    place in `in_flight`, so the Pipeline shows it stalled instead of dropping the row."""
    from sqlalchemy import func, select

    from kestrel.db import migrate
    from kestrel.db.base import utcnow_iso
    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import Task

    _settings(monkeypatch)

    async def go():
        migrate.run_migrations()
        sm = get_sessionmaker()
        q = TaskQueue(sm)
        await _hold(q, REASON_CREDIT, {"opportunity_id": 1})
        now = utcnow_iso()

        async def count(pred) -> int:
            async with sm() as sess:
                return await sess.scalar(
                    select(func.count()).select_from(Task).where(pred)
                )

        return await count(claimable(now)), await count(in_flight(now))

    claimable_n, live_n = asyncio.run(go())
    assert claimable_n == 0 and live_n == 1


def test_letter_drafts_keep_their_own_release(monkeypatch):
    """Letter drafts are out of the queue-level release pass (their hold is the person's consent,
    owned by the letter drain): even past the sweep the queue release leaves them marked, and
    only the letter drain's own release makes them leaseable again."""
    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker
    from kestrel.docs import letter_task
    from kestrel.services.online_consent import APPROVAL_HOLD

    s = _settings(monkeypatch)

    async def go():
        migrate.run_migrations()
        sm = get_sessionmaker()
        q = TaskQueue(sm)
        tid = await q.enqueue("letter_draft", {"opportunity_id": 1})
        task = await q.lease("worker-test", types=["letter_draft"])
        assert task is not None and task.id == tid
        await q.defer(tid, APPROVAL_HOLD, worker_id="worker-test")  # the letter's own defer
        await _age(sm, tid, seconds=90)
        assert await release_holds(sm, s) == 0  # the queue release never touches letters
        assert await q.lease("w", types=["letter_draft"]) is None  # the mark still holds it

        async def check(task):
            return None  # the consent now matches

        assert await letter_task.release_holds(sm, check) == 1
        got = await q.lease("w", types=["letter_draft"])
        assert got is not None and got.id == tid and got.last_error is None

    asyncio.run(go())


def test_a_held_task_in_backoff_waits_for_both_and_a_release_keeps_the_wait(monkeypatch):
    """The #154 / #153 interaction: a task that is both held (the deferred mark) AND waiting
    out its retry_at is never leased until BOTH allow it, and releasing the hold does not
    reset or skip its retry wait. Each guard is isolated in turn: with the wait already
    passed the deferred mark alone still holds it (#154); after the hold is released the
    future wait alone still holds it (#153, the release must not reset it). Only when both
    have cleared is it leased, exactly once."""
    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import Task

    s = _settings(monkeypatch)

    async def go():
        migrate.run_migrations()
        sm = get_sessionmaker()
        q = TaskQueue(sm)
        tid = await q.enqueue("t1", {"opportunity_id": 1})
        task = await q.lease("worker-test", types=["t1"])
        assert task is not None and task.id == tid
        assert await q.defer(tid, "stopped", worker_id="worker-test",
                             reason=REASON_STOPPED) == "pending"

        # #154 guard in isolation: the retry wait has already passed, yet the deferred mark
        # alone keeps it out of the queue.
        async with sm() as sess:
            t = await sess.get(Task, tid)
            t.retry_at = iso(datetime.now(UTC).replace(year=2000))  # the wait is past
            t.updated_at = iso(datetime.now(UTC))  # fresh, so the sweep does not fire
            await sess.commit()
        assert await q.lease("drain-0", types=["t1"]) is None, "the deferred mark holds it"

        # Now the backoff is live too (a retryable failure left a future wait): both guards
        # hold it.
        async with sm() as sess:
            t = await sess.get(Task, tid)
            t.retry_at = iso(datetime.now(UTC) + timedelta(minutes=10))
            t.updated_at = iso(datetime.now(UTC))
            await sess.commit()
        assert await q.lease("drain-1", types=["t1"]) is None

        # Releasing the hold clears the mark but must NOT reset or skip the retry wait.
        assert await release_holds(sm, s) == 1
        async with sm() as sess:
            t = await sess.get(Task, tid)
            assert "deferred" not in (t.payload or {})
            assert t.retry_at is not None and t.retry_at > iso(datetime.now(UTC)), \
                "the release must not reset the retry wait"
        # #153 guard in isolation: the hold is gone, but the future wait still holds it.
        assert await q.lease("drain-2", types=["t1"]) is None, "the backoff still holds it"

        # Both cleared: the released task is leased exactly once.
        async with sm() as sess:
            t = await sess.get(Task, tid)
            t.retry_at = iso(datetime.now(UTC).replace(year=2000))
            await sess.commit()
        once = await q.lease("drain-3", types=["t1"])
        assert once is not None and once.id == tid
        assert await q.lease("drain-4", types=["t1"]) is None, "not re-leased"

    asyncio.run(go())
    credit.clear()
