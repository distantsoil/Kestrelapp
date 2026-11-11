"""D-PERF-3 item 1 and 4: the Pipeline list loads only what it shows and computes each role's group
once, Analytics selects columns, and every task-by-role lookup uses the indexed column, with the
same answers as the code they replaced (kept in tests/perf3_oracle.py). Cloud-safe: a migrated
SQLite file, no model, no network."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import func as sa_func
from sqlalchemy import insert, inspect, select, update

from kestrel import paths
from kestrel.analytics import funnel as funnel_mod
from kestrel.analytics import report as rpt
from kestrel.db import migrate
from kestrel.db.models import Task, payload_opportunity_id
from kestrel.web import queries as Q
from kestrel.web.queries import GROUP_ORDER, OppFilters, list_opportunities
from tests import perf3_oracle as O
from tests.test_perf2_counts import _seed, _tracks_back, sm  # noqa: F401  (fixtures)

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _run(sm_, fn):
    async def _go():
        async with sm_() as s:
            return await fn(s)

    return asyncio.run(_go())


def _shape(page) -> dict:
    """Everything a caller can read off a page, row by row."""
    return {
        "total": page.total, "page": page.page, "counts": page.group_counts,
        "rule_rejects": page.rule_rejects, "not_fit_rules": sorted(page.not_fit_rules),
        "failed_ids": page.failed_ids,
        "rows": [
            (r.opp.id, r.ev.id if r.ev else None, r.org_name, r.source_name, r.summary,
             r.pursuit_id, getattr(r, "pursuit_status", None), r.fit, getattr(r, "mine", None),
             getattr(r, "removed", None), getattr(r, "saved", None),
             getattr(r, "scoring", None), getattr(r, "failed_reason", None),
             getattr(r, "hidden_why", None))
            for r in page.rows
        ],
    }


FILTERS = [
    dict(grouped=True, page_size=200),
    dict(grouped=True, page_size=200, track="employment"),
    dict(grouped=True, page_size=200, track="consulting"),
    # A group cut short: the rank must keep the same first rows, in the list's own order.
    dict(grouped=True, page_size=200, limits={"worth": 2, "maybe": 1, "applied": 1, "new": 1,
                                                "unscored": 1, "failed": 1}),
    dict(grouped=True, page_size=200, sort="newest", limits={"worth": 3}),
    dict(grouped=True, page_size=200, sort="score", limits={"worth": 1, "new": 2}),
    dict(grouped=True, page_size=200, section="not_fit", page_size_=0),
    dict(grouped=True, page_size=3, section="not_fit"),
    dict(grouped=True, page_size=200, include_archived=True, include_duplicates=True),
    dict(grouped=True, page_size=200, hidden_only=True),
    dict(grouped=True, page_size=200, q="Role"),
    dict(grouped=False, page_size=7),
]


def _clean(kw: dict) -> dict:
    return {k: v for k, v in kw.items() if not k.endswith("_")}


@pytest.mark.parametrize("kw", FILTERS, ids=[str(i) for i in range(len(FILTERS))])
def test_grouped_list_matches_the_old_one_row_for_row(sm, kw):  # noqa: F811
    asyncio.run(_seed(sm))
    f = OppFilters(**_clean(kw))
    new = _shape(_run(sm, lambda s: list_opportunities(s, replace(f))))
    old = _shape(_run(sm, lambda s: O.old_list_opportunities(s, replace(f))))
    assert new == old


def test_the_fixture_puts_roles_in_every_visible_group(sm):  # noqa: F811
    asyncio.run(_seed(sm))
    page = _run(sm, lambda s: list_opportunities(s, OppFilters(grouped=True, page_size=200)))
    assert {k for k, n in page.group_counts.items() if n} >= {"worth", "maybe", "new", "failed"}
    assert len(page.rows) > 10


def test_list_rows_leave_the_large_columns_unloaded(sm):  # noqa: F811
    asyncio.run(_seed(sm))

    async def go(s):
        page = await list_opportunities(s, OppFilters(grouped=True, page_size=200))
        flat = await list_opportunities(s, OppFilters(page_size=50))
        return page.rows + flat.rows

    rows = _run(sm, go)
    assert rows
    for r in rows:
        assert {"description_md", "previous_description_md", "raw"} <= inspect(r.opp).unloaded
    evs = [r.ev for r in rows if r.ev is not None]
    assert evs
    for ev in evs:
        assert {"raw_response", "dimensions"} <= inspect(ev).unloaded
        assert "blocks" not in inspect(ev).unloaded  # the summary line reads it


def test_the_group_is_computed_once_not_once_per_group(sm):  # noqa: F811
    """Seven statements before (six groups and the counts); the ranked pass is two reads plus
    the counts. The point is the shape: no statement per group."""
    from sqlalchemy import event

    asyncio.run(_seed(sm))
    eng = sm.kw["bind"].sync_engine

    def statements(call) -> list[str]:
        seen: list[str] = []

        def grab(conn, cur, stmt, params, ctx, many):
            seen.append(stmt)

        event.listen(eng, "before_cursor_execute", grab)
        try:
            _run(sm, call)
        finally:
            event.remove(eng, "before_cursor_execute", grab)
        return seen

    f = OppFilters(grouped=True, page_size=200)
    new = statements(lambda s: list_opportunities(s, replace(f)))
    old = statements(lambda s: O.old_list_opportunities(s, replace(f)))
    assert len([s_ for s_ in new if "anon_1.rn" in s_]) == 1
    # the old list ran one statement per visible group (five) where this runs two
    assert len(new) <= len(old) - 3, (len(new), len(old))


# ------------------------------------------------------------------ the task-by-role predicates
def _old_patch(monkeypatch):
    for name, fn in O.OLD_PATCH.items():
        monkeypatch.setattr(Q, name, fn)
        monkeypatch.setattr(O, name, fn)


def test_task_lookups_by_column_answer_what_the_payload_lookups_did(sm, monkeypatch):  # noqa: F811
    asyncio.run(_seed(sm))

    async def more(s):  # tasks written the Core way: no mapper event, only the trigger
        ids = await s.execute(select(Q.Opportunity.id).order_by(Q.Opportunity.id))
        opps = ids.scalars().all()
        rows = []
        for i, oid in enumerate(opps):
            rows.append({
                "type": ("t1", "t2", "rescore", "embed", "t3")[i % 5],
                "payload": {"opportunity_id": oid}, "status": ("pending", "done", "dead")[i % 3],
                "priority": 0, "attempts": 0, "max_attempts": 3,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            })
        await s.execute(insert(Task), rows)
        await s.commit()

    _run(sm, more)

    def probe(s):
        async def go():
            out = {}
            for track in (None, "employment", "consulting"):
                f = OppFilters(grouped=True, page_size=200, track=track,
                               limits={k: 500 for k in GROUP_ORDER})
                out[track] = _shape(await list_opportunities(s, f))
                out[f"{track}:failed"] = await Q.scoring_failed_count(s, track)
                out[f"{track}:counts"] = await Q.group_counts_by_track(s)
                out[f"{track}:chips"] = await Q.chip_counts(s, f)
            return out

        return go()

    new = _run(sm, probe)
    _old_patch(monkeypatch)
    old = _run(sm, probe)
    assert new == old


def test_explain_uses_the_task_opportunity_index(sm):  # noqa: F811
    asyncio.run(_seed(sm))
    stmt = select(sa_func.count()).select_from(Q.Opportunity).where(Q.failed_scoring_clause())
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    con = sqlite3.connect(paths.db_path())
    plan = [r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql)]
    con.close()
    assert any("ix_task_opportunity_id" in p for p in plan), plan


def test_worker_prior_lane_statement_uses_the_index(sm):  # noqa: F811
    asyncio.run(_seed(sm))
    from sqlalchemy import exists, or_

    from kestrel.pipeline import lane

    stmt = select(exists().where(
        Task.type.in_(("t1", "t2")),
        Task.opportunity_id == 7,
        or_(
            sa_func.json_type(Task.payload, "$.lane").is_not(None),
            sa_func.json_extract(Task.payload, f"$.{lane.LOCAL_ONLY}") == 1,
        ),
    ))
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    con = sqlite3.connect(paths.db_path())
    plan = [r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql)]
    con.close()
    assert any("ix_task_opportunity_id" in p for p in plan), plan


# --------------------------------------------------------------------------------- analytics
def test_analytics_report_and_funnel_match_the_old_loaders(sm, monkeypatch):  # noqa: F811
    asyncio.run(_seed(sm))
    now = datetime.now(UTC)  # after the seed, so every role's first_seen_at is inside the window

    async def report(s, track, rng):
        return json.loads(json.dumps(await rpt.build_report(s, track, rng, now=now), default=str))

    async def both(s):
        out = {}
        for track in ("employment", "consulting"):
            for rng in ("30", "90", "all"):
                out[f"r:{track}:{rng}"] = await report(s, track, rng)
            fn = await funnel_mod.funnel(s, track)
            out[f"f:{track}"] = fn.as_dict()
        return out

    new = _run(sm, both)

    async def old_both(s):
        out = {}
        for track in ("employment", "consulting"):
            fn = await O.old_funnel(s, track)
            out[f"f:{track}"] = fn.as_dict()
        return out

    old_funnels = _run(sm, old_both)
    for k, v in old_funnels.items():
        assert new[k] == v, k
    monkeypatch.setattr(rpt, "_load", O.old_load)
    old = _run(sm, both)
    assert new == old
    assert any(v["tiles"][0]["n"] for k, v in new.items() if k.startswith("r:"))


# -------------------------------------------------------------- the task.opportunity_id column
def test_orm_and_core_writers_set_the_column_and_every_payload_change_follows(sm):  # noqa: F811
    async def go(s):
        s.add(Task(type="t1", payload={"opportunity_id": 41}))
        s.add(Task(type="embed", payload={"opportunity_id": "42"}))
        s.add(Task(type="scrape", payload={"source_id": 3}))
        await s.commit()
        await s.execute(insert(Task), [
            {"type": "t2", "payload": {"opportunity_id": 43}, "status": "pending", "priority": 0,
             "attempts": 0, "max_attempts": 3, "created_at": "x", "updated_at": "x"},
        ])
        await s.commit()
        got = lambda: select(Task.type, Task.opportunity_id).order_by(Task.id)  # noqa: E731
        rows = (await s.execute(got())).all()
        assert rows == [("t1", 41), ("embed", 42), ("scrape", None), ("t2", 43)]
        # A Core UPDATE of the payload (the queue strips a key with json_remove): follows.
        await s.execute(update(Task).where(Task.type == "t2").values(
            payload=sa_func.json_set(Task.payload, "$.opportunity_id", 99)))
        await s.execute(update(Task).where(Task.type == "t1").values(
            payload=sa_func.json_remove(Task.payload, "$.opportunity_id")))
        await s.commit()
        rows = (await s.execute(got())).all()
        assert rows == [("t1", None), ("embed", 42), ("scrape", None), ("t2", 99)]
        # An ORM payload rewrite: follows.
        t = (await s.execute(select(Task).where(Task.type == "scrape"))).scalar_one()
        t.payload = {"opportunity_id": 7}
        await s.commit()
        found = await s.execute(select(Task.opportunity_id).where(Task.id == t.id))
        assert found.scalar_one() == 7

    _run(sm, go)


def test_payload_opportunity_id_reads_only_integers():
    assert payload_opportunity_id({"opportunity_id": 5}) == 5
    assert payload_opportunity_id({"opportunity_id": "6"}) == 6
    assert payload_opportunity_id({"opportunity_id": True}) is None
    assert payload_opportunity_id({"opportunity_id": "x"}) is None
    assert payload_opportunity_id({}) is None
    assert payload_opportunity_id(None) is None


def test_every_task_writer_in_the_code_goes_through_a_path_that_sets_it():
    """The ORM writers set it in a mapper event and every other write fires the SQL triggers, so
    no writer has a list to keep complete: this pins both mechanisms in the shipped schema."""
    migrate.run_migrations()
    con = sqlite3.connect(paths.db_path())
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {"trg_task_opportunity_ins", "trg_task_opportunity_upd"} <= names
    con.execute(
        "INSERT INTO task (type, payload, status, priority, attempts, max_attempts, created_at,"
        " updated_at) VALUES ('t1', '{\"opportunity_id\": 12}', 'pending', 0, 0, 3, 'x', 'x')"
    )
    assert con.execute("SELECT opportunity_id FROM task").fetchall() == [(12,)]
    con.execute("UPDATE task SET payload = json_set(payload, '$.opportunity_id', 13)")
    assert con.execute("SELECT opportunity_id FROM task").fetchall() == [(13,)]
    con.close()


def test_migration_0030_backfills_and_downgrades_cleanly(tmp_path):
    import warnings

    from alembic import command
    from sqlalchemy.exc import SAWarning

    db = tmp_path / "m.db"
    ins = (
        "INSERT INTO task (type, payload, status, priority, attempts, max_attempts, created_at,"
        " updated_at) VALUES ('t1', '{\"opportunity_id\": 77}', 'done', 0, 1, 3, 'x', 'x'),"
        " ('scrape', '{\"source_id\": 1}', 'done', 0, 1, 3, 'x', 'x'),"
        " ('embed', '{\"opportunity_id\": \"78\"}', 'done', 0, 1, 3, 'x', 'x')"
    )
    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        cfg = migrate._config(str(mig_dir), db)
        with warnings.catch_warnings():
            warnings.simplefilter("error", SAWarning)
            command.upgrade(cfg, "0029")
            con = sqlite3.connect(db)
            con.execute(ins)
            con.commit()
            con.close()
            command.upgrade(cfg, "head")
        con = sqlite3.connect(db)
        assert con.execute(
            "SELECT type, opportunity_id FROM task ORDER BY id"
        ).fetchall() == [("t1", 77), ("scrape", None), ("embed", 78)]
        con.close()
        with warnings.catch_warnings():
            warnings.simplefilter("error", SAWarning)
            command.downgrade(cfg, "0029")
        con = sqlite3.connect(db)
        assert "opportunity_id" not in [r[1] for r in con.execute("PRAGMA table_info(task)")]
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'").fetchall()
        assert con.execute("SELECT count(*) FROM task").fetchone()[0] == 3
        idx = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        con.close()
        assert "ux_task_live_scoring" in idx and "ix_task_opportunity_id" not in idx
        assert "ix_opportunity_scrape_run_id" not in idx
        with warnings.catch_warnings():
            warnings.simplefilter("error", SAWarning)
            command.upgrade(cfg, "head")
    con = sqlite3.connect(db)
    idx = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"ix_task_opportunity_id", "ix_opportunity_scrape_run_id",
            "ix_quarantine_opportunity_id", "ux_task_live_scoring"} <= idx
    assert con.execute("SELECT opportunity_id FROM task ORDER BY id").fetchall() == [
        (77,), (None,), (78,)]
    con.close()
