"""Performance guards (D-PERF): the live-task predicates stay off a full task-table scan, the
prefilter changes no answer, and startup stays light. All data is invented; no network."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import warnings
from datetime import timedelta

from alembic import command
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.exc import SAWarning

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.base import iso, utcnow
from kestrel.db.engine import MMAP_SIZE_BYTES, make_sync_engine
from kestrel.db.models import Opportunity, Task
from kestrel.queue import queue as q
from kestrel.services.online_consent import APPROVAL_HOLD
from kestrel.web import queries


def _literal_sql(stmt) -> str:
    return str(
        stmt.compile(dialect=sqlite_dialect.dialect(), compile_kwargs={"literal_binds": True})
    )


def _plan(db, sql: str) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return [r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql)]
    finally:
        con.close()


def _seed_opps_and_tasks(db) -> None:
    con = sqlite3.connect(db)
    # Fill every NOT NULL column that has no default from its declared type.
    cols = [
        (r[1], r[2])
        for r in con.execute("PRAGMA table_info(opportunity)")
        if r[3] and r[4] is None and r[1] != "id"
    ]
    names = ", ".join(["id", *(c for c, _ in cols)])
    marks = ", ".join("?" * (len(cols) + 1))
    for i in range(1, 40):
        vals = [0 if "INT" in t.upper() or "BOOL" in t.upper() or "FLOAT" in t.upper() else "x"
                for _, t in cols]
        con.execute(
            f"INSERT INTO opportunity ({names}) VALUES ({marks})", (i, *vals)
        )
    con.execute("UPDATE opportunity SET stage = 'quarantined'")
    for i in range(1, 400):
        con.execute(
            "INSERT INTO task (type, payload, status, priority, attempts, max_attempts,"
            " created_at, updated_at) VALUES ('t1', ?, 'done', 0, 1, 3, 'x', 'x')",
            (f'{{"opportunity_id": {i % 40}}}',),
        )
    con.commit()
    con.close()


def test_group_count_probes_the_task_table_by_index_not_a_scan(tmp_path):
    db = tmp_path / "plan.db"
    migrate.run_migrations(db)
    _seed_opps_and_tasks(db)
    stmt = select(func.count()).select_from(Opportunity).where(queries.failed_scoring_clause())
    plan = _plan(db, _literal_sql(stmt))
    # Lines about the task table itself (its index is named for the opportunity column, 0030).
    task_lines = [p for p in plan if p.startswith(("SEARCH task", "SCAN task"))]
    assert task_lines, plan
    assert not any(p.startswith("SCAN task") or p.startswith("SCAN t ") for p in plan), plan
    assert any("SEARCH" in p and "task" in p for p in plan), plan


def test_deferred_count_filter_uses_the_status_index(tmp_path):
    db = tmp_path / "def.db"
    migrate.run_migrations(db)
    _seed_opps_and_tasks(db)
    stmt = select(func.count()).select_from(Task).where(
        Task.status.in_(q.LIVE_STATUSES), q.is_deferred()
    )
    plan = _plan(db, _literal_sql(stmt))
    assert not any(p.startswith("SCAN") for p in plan), plan


# The predicates as they read before the redundant status prefilter (D-PERF), kept here so the
# parity test proves the prefilter changed no answer.
def _old_claim_state(now: str):
    ready = or_(Task.retry_at.is_(None), Task.retry_at <= now)
    return or_(
        and_(Task.status == "pending", ready),
        and_(Task.status == "leased", Task.lease_until < now, Task.attempts < Task.max_attempts),
    )


def _old_claimable_base(now: str):
    base = and_(_old_claim_state(now), ~q.in_lane())
    free = q.not_held()
    return base if free is None else and_(base, free)


def _old_in_flight(now: str):
    awaiting = func.coalesce(Task.last_error, "") == APPROVAL_HOLD
    backoff = and_(Task.status == "pending", Task.retry_at.is_not(None), ~q.in_lane())
    held = q.not_held()
    backoff = backoff if held is None else and_(backoff, held)
    return and_(
        or_(
            _old_claimable_base(now),
            backoff,
            and_(Task.status == "leased", or_(Task.lease_until.is_(None), Task.lease_until >= now)),
        ),
        ~awaiting,
    )


async def test_prefilter_changes_no_claimable_or_in_flight_answer(sm):
    now_dt = utcnow()
    now = iso(now_dt)
    past = iso(now_dt - timedelta(minutes=5))
    future = iso(now_dt + timedelta(minutes=5))
    rows: list[dict] = []
    for status in ("pending", "leased", "done", "dead", "cancelled", "failed", "weird"):
        for extra in (
            {},
            {"retry_at": past},
            {"retry_at": future},
            {"lease_until": past},
            {"lease_until": future},
            {"lease_until": past, "attempts": 3},
            {"payload": {"lane": 1}},
            {"payload": {"deferred": True}},
            {"payload": {"requested": True, "lane": 1}},
            {"last_error": APPROVAL_HOLD},
        ):
            rows.append({"type": "t1", "status": status, "max_attempts": 3, **extra})
    async with sm() as s:
        for i, r in enumerate(rows):  # a distinct opportunity per row: one live task each
            r["payload"] = {**r.get("payload", {}), "opportunity_id": 1000 + i}
        s.add_all(Task(**r) for r in rows)
        await s.commit()
        for name in ("claimable", "in_flight"):
            new = getattr(q, name)(now)
            old = (
                and_(_old_claimable_base(now), ~q.is_deferred())
                if name == "claimable"
                else _old_in_flight(now)
            )
            got = set((await s.execute(select(Task.id).where(new))).scalars())
            want = set((await s.execute(select(Task.id).where(old))).scalars())
            assert got == want and want, name
        statuses = {
            st
            for st in (
                await s.execute(select(Task.status).where(q.in_flight(now)).distinct())
            ).scalars()
        }
        assert statuses <= {"pending", "leased"}


async def test_deferred_count_counts_only_live_deferred_rows(sm):
    async with sm() as s:
        s.add_all(
            [
                Task(type="t1", status="pending", payload={"deferred": True}),
                Task(type="t1", status="pending", payload={"deferred": True}),
                Task(type="t1", status="pending", payload={}),
                *[Task(type="t1", status="done", payload={"x": i}) for i in range(30)],
                *[Task(type="t1", status="dead", payload={"x": i}) for i in range(30)],
            ]
        )
        await s.commit()
    assert await q.deferred_count(sm) == 2


def test_new_connections_map_memory_and_startup_optimizes(tmp_path, monkeypatch):
    db = tmp_path / "p.db"
    migrate.run_migrations(db)
    engine = make_sync_engine(db)
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA mmap_size").scalar() == MMAP_SIZE_BYTES
    finally:
        engine.dispose()
    seen: list[object] = []
    monkeypatch.setattr(migrate, "optimize_db", lambda target: seen.append(target))
    migrate.run_migrations(db)
    assert seen == [db]


def test_importing_the_app_does_not_import_sklearn():
    code = "import sys, kestrel.web.app; sys.exit(1 if 'sklearn' in sys.modules else 0)"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-500:]


def test_full_migration_chain_raises_no_reflection_warning(tmp_path):
    db = tmp_path / "chain.db"
    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        cfg = migrate._config(str(mig_dir), db)
        with warnings.catch_warnings():
            warnings.simplefilter("error", SAWarning)
            command.upgrade(cfg, "head")
    con = sqlite3.connect(db)
    try:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        con.close()
    assert "ux_task_live_scoring" in names and "ix_task_retry_at" in names


def test_retry_at_downgrade_and_upgrade_round_trip(tmp_path):
    db = tmp_path / "rt.db"
    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        cfg = migrate._config(str(mig_dir), db)
        with warnings.catch_warnings():
            warnings.simplefilter("error", SAWarning)
            command.upgrade(cfg, "0029")
            command.downgrade(cfg, "0028")
        con = sqlite3.connect(db)
        cols = {r[1] for r in con.execute("PRAGMA table_info(task)")}
        idx = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        con.close()
        assert "retry_at" not in cols and "ix_task_retry_at" not in idx
        assert "ux_task_live_scoring" in idx
        with warnings.catch_warnings():
            warnings.simplefilter("error", SAWarning)
            command.upgrade(cfg, "head")
        con = sqlite3.connect(db)
        idx = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        con.close()
        assert "ux_task_live_scoring" in idx and "ix_task_retry_at" in idx
