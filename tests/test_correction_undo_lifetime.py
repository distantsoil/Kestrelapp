"""A removed correction's Undo lasts a day, then it and its snapshot are gone (fable review).

INVARIANT: an Undo token older than a day restores nothing, and the daily housekeeping deletes
every Undo snapshot (a removed correction's note among them) older than a day, even when no new
correction is ever saved. Clocks are each module's own alias (D-TEST-2). Invented text only."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.base import iso
from kestrel.db.engine import make_async_engine
from kestrel.db.models import Setting
from kestrel.services import prune
from kestrel.web import correction_undo

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
SNAPSHOT = {"opp": None, "profile": True, "stamps": {},
            "undo": {"written": [], "before": [{"id": 4, "note": "an invented private note"}]}}


async def _with_rows(rows: dict[str, datetime]):
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        for key, at in rows.items():
            s.add(Setting(key=key, value=SNAPSHOT, updated_at=iso(at)))
        await s.commit()
    return engine, sm


def test_a_token_older_than_a_day_finds_nothing(monkeypatch):
    monkeypatch.setattr(correction_undo, "_now", lambda: NOW)

    async def go():
        engine, sm = await _with_rows({
            "undo.correction.fresh": NOW - timedelta(hours=2),
            "undo.correction.stale": NOW - timedelta(days=3),
        })
        try:
            async with sm() as s:
                assert await correction_undo.find(s, "fresh") is not None
                assert await correction_undo.find(s, "stale") is None
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_housekeeping_deletes_old_undo_snapshots_and_keeps_new_ones(monkeypatch):
    monkeypatch.setattr(prune, "_now", lambda: NOW)

    async def go():
        engine, sm = await _with_rows({
            "undo.correction.stale": NOW - timedelta(days=3),
            "undo.pursuit.stale": NOW - timedelta(days=2),
            "undo.applied.stale": NOW - timedelta(days=2),
            "undo.correction.fresh": NOW - timedelta(hours=1),
            "ui.scheme": NOW - timedelta(days=30),
        })
        try:
            out = await prune.prune(sm)
            async with sm() as s:
                keys = set((await s.execute(select(Setting.key))).scalars())
        finally:
            await engine.dispose()
        assert out.undo_snapshots == 3
        assert "undo.correction.fresh" in keys and "ui.scheme" in keys
        assert not {k for k in keys if k.endswith(".stale")}

    asyncio.run(go())
