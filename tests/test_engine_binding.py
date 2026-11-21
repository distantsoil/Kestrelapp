"""D-TEST-570 (#180): the cached app engine answers only for the database it was built for.

A test that ran after another one could inherit the cached engine of the previous test's data
home whenever anything called get_engine() between that test's teardown and the next test's own
data home: a server thread that outlived its test, or a fixture. The next test then read a
database that was never migrated ("no such table: doc_render" in the route crawl). These tests
build that exact sequence by hand.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from sqlalchemy import text

from kestrel import paths
from kestrel.db import engine as engine_mod
from kestrel.db import migrate


def _doc_render_rows() -> int:
    async def go() -> int:
        async with engine_mod.get_sessionmaker()() as s:
            return int((await s.execute(text("SELECT count(*) FROM doc_render"))).scalar_one())

    return asyncio.run(go())


def test_a_cached_engine_never_answers_for_another_data_home(tmp_path, monkeypatch):
    stale_home = tmp_path / "previous_test"
    stale_home.mkdir()
    monkeypatch.setenv("KESTREL_DATA_HOME", str(stale_home))
    engine_mod.forget_engine()
    engine_mod.get_engine()  # built while the previous test's home was current, never migrated

    home = tmp_path / "this_test"
    home.mkdir()
    monkeypatch.setenv("KESTREL_DATA_HOME", str(home))
    migrate.run_migrations()
    assert _doc_render_rows() == 0
    assert Path(engine_mod.get_engine().url.database or "").as_posix() == paths.db_path().as_posix()


def test_an_engine_built_by_a_leftover_thread_is_not_reused(tmp_path, monkeypatch):
    """The same sequence with the stale engine made on another thread, the way a server thread
    that outlived its test makes it."""
    stale_home = tmp_path / "previous_test"
    stale_home.mkdir()
    monkeypatch.setenv("KESTREL_DATA_HOME", str(stale_home))
    engine_mod.forget_engine()
    worker = threading.Thread(target=engine_mod.get_engine)
    worker.start()
    worker.join()

    home = tmp_path / "this_test"
    home.mkdir()
    monkeypatch.setenv("KESTREL_DATA_HOME", str(home))
    migrate.run_migrations()
    assert _doc_render_rows() == 0


def test_the_same_data_home_keeps_one_engine(tmp_path, monkeypatch):
    """A running app never moves its data home: it builds the engine once and keeps it."""
    monkeypatch.setenv("KESTREL_DATA_HOME", str(tmp_path))
    engine_mod.forget_engine()
    first = engine_mod.get_engine()
    assert engine_mod.get_engine() is first
    assert engine_mod.get_sessionmaker() is engine_mod.get_sessionmaker()
