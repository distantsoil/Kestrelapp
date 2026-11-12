"""D-PERF-3 item 3: retention. Finished background tasks go after 30 days, a dead task stays while
its role still needs another try, run history and notifications go after a year, nothing open or
still needed goes, and a kill in the middle leaves a consistent database the next pass finishes
(hard rule 5). The clock is this module's own alias (`prune._now`), never the global one.
Cloud-safe: a migrated SQLite file, no model, no network."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.base import iso
from kestrel.db.engine import make_async_engine, reset_engine
from kestrel.db.models import (
    Evaluation,
    Notification,
    Opportunity,
    Quarantine,
    Run,
    ScrapeRun,
    Setting,
    Source,
    Task,
)
from kestrel.services import prune

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def _ago(days: float) -> str:
    return iso(NOW - timedelta(days=days))


@pytest.fixture
def sm(monkeypatch):
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    monkeypatch.setattr(prune, "_now", lambda: NOW)
    yield async_sessionmaker(engine, expire_on_commit=False)
    asyncio.run(engine.dispose())


def _opp(name: str, stage: str = "t2_done", **kw) -> Opportunity:
    return Opportunity(track="employment", opp_type="job_posting", title=name, org_name="Acme",
                       url=f"https://example.test/{name}", stage=stage, description_md="x", **kw)


async def _world(sm) -> dict[str, int]:
    """One of everything, around the 30 day and 365 day lines."""
    ids: dict[str, int] = {}
    async with sm() as s:
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        s.add(src)
        await s.flush()
        runs = {
            "sr_old": ScrapeRun(source_id=src.id, started_at=_ago(400), finished_at=_ago(399)),
            "sr_old_open": ScrapeRun(source_id=src.id, started_at=_ago(400), finished_at=None,
                                     status="running"),
            "sr_new": ScrapeRun(source_id=src.id, started_at=_ago(5), finished_at=_ago(5)),
        }
        s.add_all(runs.values())
        s.add_all([
            Run(name="old", started_at=_ago(400), finished_at=_ago(400), status="done"),
            Run(name="old_open", started_at=_ago(400), finished_at=None, status="running"),
            Run(name="new", started_at=_ago(2), finished_at=_ago(2), status="done"),
            Notification(kind="info", title="old", created_at=_ago(366)),
            Notification(kind="info", title="new", created_at=_ago(364)),
        ])
        await s.flush()
        ids.update({k: v.id for k, v in runs.items()})
        opps = {
            "from_old_run": _opp("a", scrape_run_id=runs["sr_old"].id),
            "from_new_run": _opp("b", scrape_run_id=runs["sr_new"].id),
            "parked": _opp("c", stage="quarantined"),
            "errored": _opp("d", stage="error"),
            "open_failure": _opp("e", stage="t1_pass"),
            "resolved": _opp("f", stage="t2_done"),
            "plain": _opp("g"),
        }
        s.add_all(opps.values())
        await s.flush()
        ids.update({k: v.id for k, v in opps.items()})
        s.add(Quarantine(opportunity_id=opps["open_failure"].id, tier=2, error="bad",
                         created_at=_ago(40)))
        # A failure a later Tier 2 read resolved: superseded, so it no longer needs the operator.
        s.add(Quarantine(opportunity_id=opps["resolved"].id, tier=2, error="bad",
                         created_at=_ago(40)))
        s.add(Evaluation(opportunity_id=opps["resolved"].id, tier=2, track="employment",
                         verdict="apply", score=4.0, weighted_score=4.0, model="m",
                         created_at=_ago(39)))
        await s.commit()

        def task(name, status, age, role=None, type_="t2"):
            payload = {"opportunity_id": ids[role]} if role else {}
            t = Task(type=type_, status=status, payload=payload,
                     created_at=_ago(age), updated_at=_ago(age))
            s.add(t)
            return name, t

        made = [
            task("done_old", "done", 31, "plain"), task("done_new", "done", 29, "plain"),
            task("cancel_old", "cancelled", 45), task("cancel_new", "cancelled", 3),
            task("dead_old_plain", "dead", 31, "plain"),
            task("dead_old_noopp", "dead", 31, None, "scrape"),
            task("dead_old_resolved", "dead", 31, "resolved"),
            task("dead_old_parked", "dead", 90, "parked"),
            task("dead_old_errored", "dead", 90, "errored"),
            task("dead_old_open_failure", "dead", 90, "open_failure"),
            task("dead_new_plain", "dead", 10, "plain"),
            task("failed_old", "failed", 90, "plain"),
            task("pending_old", "pending", 90, "plain"),
            task("leased_old", "leased", 90, "plain", "t1"),
        ]
        await s.flush()
        ids.update({n: t.id for n, t in made})
        for _n, t in made:  # `updated_at` has an onupdate; the flush above already took ours
            t.updated_at = t.created_at
        await s.commit()
    return ids


def _left(sm, model) -> set[int]:
    async def go():
        async with sm() as s:
            return set((await s.execute(select(model.id))).scalars())

    return asyncio.run(go())


def test_prune_removes_exactly_what_the_rules_name_and_keeps_the_rest(sm):
    ids = asyncio.run(_world(sm))
    out = asyncio.run(prune.prune(sm))
    left = _left(sm, Task)
    gone = {"done_old", "cancel_old", "dead_old_plain", "dead_old_noopp", "dead_old_resolved"}
    kept = {
        "done_new", "cancel_new", "dead_old_parked", "dead_old_errored", "dead_old_open_failure",
        "dead_new_plain", "failed_old", "pending_old", "leased_old",
    }
    assert {n for n in gone if ids[n] in left} == set()
    assert {n for n in kept if ids[n] not in left} == set()
    assert out.tasks_done == 2 and out.tasks_dead == 3

    assert _left(sm, Run) and len(_left(sm, Run)) == 2  # the year-old finished run only
    assert _left(sm, Notification) and len(_left(sm, Notification)) == 1
    assert ids["sr_old"] not in _left(sm, ScrapeRun)
    assert {ids["sr_old_open"], ids["sr_new"]} <= _left(sm, ScrapeRun)
    assert out.runs == 1 and out.scrape_runs == 1 and out.notifications == 1

    async def roles():
        async with sm() as s:
            found = (await s.execute(select(Opportunity))).scalars()
            return {o.title: o.scrape_run_id for o in found}

    seen = asyncio.run(roles())
    assert seen["a"] is None  # the role stays, its pointer to the gone run is cleared
    assert seen["b"] == ids["sr_new"]
    assert len(seen) == 7  # no role was removed


def test_a_second_pass_changes_nothing(sm):
    asyncio.run(_world(sm))
    asyncio.run(prune.prune(sm))
    again = asyncio.run(prune.prune(sm))
    assert again.total == 0


def test_a_kill_between_batches_leaves_a_consistent_database_and_the_next_pass_finishes(
    sm, monkeypatch
):
    ids = asyncio.run(_world(sm))
    monkeypatch.setattr(prune, "BATCH", 1)
    calls = {"n": 0}
    real_sleep = asyncio.sleep

    async def dying_sleep(delay):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt("killed between batches")  # a kill, not an Exception
        await real_sleep(delay)

    monkeypatch.setattr(prune.asyncio, "sleep", dying_sleep)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(prune.prune_if_due(sm))
    monkeypatch.setattr(prune.asyncio, "sleep", real_sleep)

    con = sqlite3.connect(paths.db_path())
    assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()
    # Half done, and no day stamp: nothing claims the pass finished.
    async def stamp():
        async with sm() as s:
            return await s.get(Setting, prune.STAMP_KEY)

    assert asyncio.run(stamp()) is None
    assert len(_left(sm, Task)) > 9  # one batch went before the kill, the rest is still there
    out = asyncio.run(prune.prune_if_due(sm))
    assert out is not None and not out.stopped
    final = _left(sm, Task)
    for name in ("done_old", "cancel_old", "dead_old_plain", "dead_old_noopp",
                 "dead_old_resolved"):
        assert ids[name] not in final
    for name in ("done_new", "dead_old_parked", "failed_old", "pending_old"):
        assert ids[name] in final
    assert asyncio.run(stamp()) is not None


def test_it_runs_once_a_day_and_not_when_stopped_early(sm, monkeypatch):
    asyncio.run(_world(sm))
    assert asyncio.run(prune.prune_if_due(sm)) is not None
    assert asyncio.run(prune.prune_if_due(sm)) is None  # the same day

    monkeypatch.setattr(prune, "_now", lambda: NOW + timedelta(hours=23))
    assert asyncio.run(prune.prune_if_due(sm)) is None
    monkeypatch.setattr(prune, "_now", lambda: NOW + timedelta(hours=25))
    assert asyncio.run(prune.prune_if_due(sm)) is not None

    # A quit mid-pass: it stops at the next batch and does not stamp the day.
    async def stopped():
        async with sm() as s:
            row = await s.get(Setting, prune.STAMP_KEY)
            await s.delete(row)
            await s.commit()
        stop = asyncio.Event()
        stop.set()
        return await prune.prune_if_due(sm, stop=stop)

    out = asyncio.run(stopped())
    assert out is not None and out.stopped

    async def stamp():
        async with sm() as s:
            return await s.get(Setting, prune.STAMP_KEY)

    assert asyncio.run(stamp()) is None


def test_the_worker_asks_at_most_once_an_hour_and_never_dies_of_it(monkeypatch):
    from kestrel.web import app as app_module

    calls = []

    async def fake(sm, stop=None):
        calls.append(1)
        raise RuntimeError("disk full")

    monkeypatch.setattr(prune, "prune_if_due", fake)
    monkeypatch.setitem(app_module._housekeeping_asked, "at", float("-inf"))
    asyncio.run(app_module._housekeeping(object(), asyncio.Event()))
    asyncio.run(app_module._housekeeping(object(), asyncio.Event()))
    assert len(calls) == 1


def test_housekeeping_runs_only_from_an_idle_cycle_with_no_run_open():
    """The hook sits behind `if idle:` and `carried is None` in the worker loop."""
    import inspect

    from kestrel.web import app as app_module

    src = inspect.getsource(app_module._worker_loop)
    at = src.index("await _housekeeping(sm, stop)")
    window = src[max(0, at - 160):at]
    assert "if idle:" in window and "carried is None" in window


def test_every_foreign_key_into_the_pruned_tables_is_handled():
    """A new table pointing at scrape_run, run, notification or task must be dealt with here, or
    the delete fails on a live database where foreign keys are enforced."""
    from kestrel.db.base import Base

    pruned = {"scrape_run", "run", "notification", "task"}
    pointers = {
        (t.name, fk.column.table.name, fk.parent.name)
        for t in Base.metadata.tables.values()
        for fk in t.foreign_keys
        if fk.column.table.name in pruned
    }
    assert pointers == {("opportunity", "scrape_run", "scrape_run_id")}


def test_counts_are_a_function_of_rows_not_of_session_state(sm):
    asyncio.run(_world(sm))

    async def n():
        async with sm() as s:
            return (await s.execute(select(func.count()).select_from(Task))).scalar_one()

    before = asyncio.run(n())
    out = asyncio.run(prune.prune(sm))
    assert asyncio.run(n()) == before - out.tasks_done - out.tasks_dead


# ------------------------------------------------------- what retention must not make us forget
def test_a_lane_role_stays_local_only_after_its_tasks_are_pruned(sm):
    """D-SRC-13 and D-PERF-3: the role carries the background-lane mark itself, so a Score again
    on day 40 still reads local-only after the 30 day prune removed every task that said so."""
    from sqlalchemy import insert

    from kestrel.pipeline import lane

    async def go():
        async with sm() as s:
            plain, via_orm, via_core, released = (_opp(n) for n in ("p", "o", "c", "r"))
            s.add_all([plain, via_orm, via_core, released])
            await s.flush()
            s.add(Task(type="t1", status="done", payload={"opportunity_id": via_orm.id, "lane": 1},
                       created_at=_ago(40), updated_at=_ago(40)))
            await s.execute(insert(Task), [{
                "type": "t2", "status": "done", "payload": {"opportunity_id": via_core.id,
                                                            "lane": 0},
                "priority": 0, "attempts": 0, "max_attempts": 3,
                "created_at": _ago(40), "updated_at": _ago(40)}])
            s.add(Task(type="t1", status="done", payload={"opportunity_id": plain.id},
                       created_at=_ago(40), updated_at=_ago(40)))
            late = Task(type="t1", status="done", payload={"opportunity_id": released.id},
                        created_at=_ago(40), updated_at=_ago(40))
            s.add(late)
            await s.commit()
            late.payload = {"opportunity_id": released.id, "lane_local_only": True}  # lane._free
            await s.commit()
            await s.execute(Task.__table__.update().where(Task.id == late.id)
                            .values(updated_at=_ago(40)))  # the ORM stamped it now
            await s.commit()
            ids = (plain.id, via_orm.id, via_core.id, released.id)
        out = await prune.prune(sm)
        assert out.tasks_done == 4
        async with sm() as s:
            return [await lane.prior_lane(s, i) for i in ids]

    assert asyncio.run(go()) == [False, True, True, True]


def test_migration_backfills_the_lane_mark_from_existing_tasks(tmp_path):
    from alembic import command

    db = tmp_path / "lane.db"
    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        cfg = migrate._config(str(mig_dir), db)
        command.upgrade(cfg, "0029")
        from tests.test_perf_queries import _seed_opps_and_tasks

        _seed_opps_and_tasks(db)  # opportunities 1 to 39, each with plain t1 tasks
        con = sqlite3.connect(db)
        con.execute("INSERT INTO task (type, payload, status, priority, attempts, max_attempts,"
                    " created_at, updated_at) VALUES ('t1','{\"opportunity_id\": 5, \"lane\": 1}',"
                    "'done',0,1,3,'x','x')")
        con.commit()
        con.close()
        command.upgrade(cfg, "head")
    con = sqlite3.connect(db)
    marked = con.execute("SELECT id FROM opportunity WHERE lane_local_only = 1").fetchall()
    assert marked == [(5,)]
    con.close()


def test_a_pruned_scrape_run_takes_its_raw_file_only_from_inside_the_data_folder(sm, tmp_path):
    inside = paths.data_home() / "raw" / "run.json"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("{}")
    outside = tmp_path / "elsewhere.json"
    outside.write_text("precious")

    async def go():
        async with sm() as s:
            src = Source(track="employment", kind="greenhouse", name="b", enabled=True)
            s.add(src)
            await s.flush()
            s.add_all([
                ScrapeRun(source_id=src.id, started_at=_ago(400), finished_at=_ago(399),
                          raw_path=str(inside)),
                ScrapeRun(source_id=src.id, started_at=_ago(400), finished_at=_ago(399),
                          raw_path=str(outside)),
                ScrapeRun(source_id=src.id, started_at=_ago(400), finished_at=_ago(399),
                          raw_path=str(inside.parent / ".." / ".." / "nothing.json")),
            ])
            await s.commit()
        return await prune.prune(sm)

    out = asyncio.run(go())
    assert out.scrape_runs == 3
    assert not inside.exists()
    assert outside.read_text() == "precious"  # the row went; a path outside is never followed


def test_the_follow_up_draft_the_dialog_shows_is_kept_whatever_its_age(sm):
    """D-PERF-5: the drafted follow-up text lives only in its task's payload, and the Follow up
    dialog reads the newest follow_up task of the application. That one stays; an older draft of
    the same application, which nothing shows, goes with the other old finished work."""
    draft = {"subject": "Checking in", "body": "An invented note about the role."}

    async def seed():
        async with sm() as s:
            rows = [
                Task(type="follow_up", status="done", updated_at=_ago(90),
                     payload={"pursuit_id": 1, "result": draft}),  # replaced by the next one
                Task(type="follow_up", status="done", updated_at=_ago(60),
                     payload={"pursuit_id": 1, "result": draft}),  # the one shown
                Task(type="follow_up", status="dead", updated_at=_ago(60),
                     payload={"pursuit_id": 2}),  # the one shown: its failure
                Task(type="embed", status="done", updated_at=_ago(60), payload={}),
            ]
            s.add_all(rows)
            await s.commit()
            return [r.id for r in rows]

    old, shown, shown_dead, other = asyncio.run(seed())
    asyncio.run(prune.prune(sm))

    async def left():
        async with sm() as s:
            return set((await s.execute(select(Task.id))).scalars())

    remaining = asyncio.run(left())
    assert shown in remaining and shown_dead in remaining
    assert old not in remaining and other not in remaining


def test_the_training_markers_request_training_reads_are_kept_whatever_their_age(sm):
    """D-LEARN-10: "once per refused model ever" and the "50 more ratings" baseline live only in
    training task rows. The newest training task of each track and every one that records a
    refused-model retrain stay; an older ordinary one goes."""
    async def seed():
        async with sm() as s:
            rows = [
                Task(type="train_reranker", status="done", updated_at=_ago(120),
                     payload={"track": "employment", "ratings": 200}),  # ordinary, superseded
                Task(type="train_reranker", status="done", updated_at=_ago(90),
                     payload={"track": "employment", "ratings": 210,
                              "replaces_refused": 7}),  # the once-ever marker
                Task(type="train_reranker", status="done", updated_at=_ago(60),
                     payload={"track": "employment", "ratings": 230}),  # newest of the track
                Task(type="train_reranker", status="dead", updated_at=_ago(60),
                     payload={"track": "consulting", "ratings": 210}),  # newest of the track
            ]
            s.add_all(rows)
            await s.commit()
            return [r.id for r in rows]

    old, marker, newest, newest_other = asyncio.run(seed())
    asyncio.run(prune.prune(sm))

    async def left():
        async with sm() as s:
            return set((await s.execute(select(Task.id))).scalars())

    remaining = asyncio.run(left())
    assert {marker, newest, newest_other} <= remaining
    assert old not in remaining
