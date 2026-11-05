"""The diagnostics bundle lists live and recent tasks by type, status and last error (issues #143
and #144), so a Score again that went nowhere can be read from the file. Role ids only: no posting
text, title, organization or payload value. Invented text only; nothing reaches a network."""

from __future__ import annotations

import asyncio
import json
import zipfile

import pytest

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import make_async_engine
from kestrel.db.models import Opportunity, Task
from kestrel.services import diagnostics
from kestrel.services.online_consent import APPROVAL_HOLD

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_TITLE = "Chief Zebra Wrangler"
_SECRET_PAYLOAD = "payload-value-that-must-not-leak"


async def _seed() -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel.db.engine import forget_engine

    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        opp = Opportunity(track="employment", opp_type="job_posting", title=_TITLE,
                          org_name="Orchard Works", description_md="Posting body text here.",
                          stage="t1_pass")
        s.add(opp)
        await s.flush()
        oid = opp.id
        s.add_all([
            Task(type="t2", status="pending", payload={
                "opportunity_id": oid, "rescore": True, "score_stub": True,
                "note": _SECRET_PAYLOAD, "online_approval": {"tier2": ["svc|model"]}}),
            Task(type="t2", status="pending", last_error=APPROVAL_HOLD,
                 payload={"opportunity_id": oid + 1000}),
            Task(type="t1", status="leased", payload={"opportunity_id": oid}),
            Task(type="t2", status="done", payload={"opportunity_id": oid}),
            Task(type="embed", status="failed", attempts=3,
                 last_error="boom " + "x" * 500 + " sk-abcdefghijklmnop", payload={}),
        ])
        await s.commit()
    await engine.dispose()
    forget_engine()
    return oid


def test_the_bundle_lists_live_and_recent_tasks_without_posting_text(tmp_path, monkeypatch):
    from kestrel import doctor as doctor_mod
    from kestrel.config import get_settings

    def _no_doctor(*, quick: bool = False):
        raise RuntimeError("doctor skipped in this test")

    monkeypatch.setattr(doctor_mod, "run_doctor", _no_doctor)
    oid = asyncio.run(_seed())
    paths.ensure_dirs()
    archive = diagnostics.build_bundle(get_settings(), out_dir=tmp_path / "bundle")
    with zipfile.ZipFile(archive) as zf:
        members = {n: zf.read(n) for n in zf.namelist()}
    tasks = json.loads(members["summary.json"])["tasks"]
    assert tasks["by_type_and_status"] == {
        "embed/failed": 1, "t1/leased": 1, "t2/done": 1, "t2/pending": 2}
    assert tasks["live_by_type"] == {"t1": 1, "t2": 2}
    recent = tasks["recent"]
    assert len(recent) == 5 and len(recent) <= diagnostics.TASK_ROWS
    again = next(r for r in recent if r["flags"] == ["rescore", "score_stub"])
    assert again["opportunity_id"] == oid and again["status"] == "pending"
    assert again["online_approval"] is True  # that it carries one, never where it points
    held = next(r for r in recent if r["last_error"] == APPROVAL_HOLD[:200])
    assert held["type"] == "t2"
    failed = next(r for r in recent if r["type"] == "embed")
    assert failed["attempts"] == 3 and len(failed["last_error"]) <= 200
    assert "sk-abcdefghijklmnop" not in failed["last_error"]  # tokens are masked
    # roles.json names titles by design (its own contract); the task list adds none anywhere else.
    for name, data in members.items():
        if name == "roles.json":
            continue
        for needle in (_TITLE, "Orchard Works", "Posting body text", _SECRET_PAYLOAD, "svc|model"):
            assert needle.encode() not in data, (name, needle)
    assert _SECRET_PAYLOAD.encode() not in members["roles.json"]


def test_the_task_list_is_bounded(tmp_path):
    import sqlite3

    async def many():
        from sqlalchemy.ext.asyncio import async_sessionmaker

        migrate.run_migrations()
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            s.add_all([Task(type="embed", status="done", payload={}) for _ in range(90)])
            await s.commit()
        await engine.dispose()

    asyncio.run(many())
    conn = sqlite3.connect(str(paths.db_path()))
    conn.row_factory = sqlite3.Row
    try:
        out = diagnostics.task_rows(conn, lambda t: t)
    finally:
        conn.close()
    assert len(out["recent"]) == diagnostics.TASK_ROWS
    assert out["by_type_and_status"] == {"embed/done": 90}


def _bundle_tasks(tmp_path, monkeypatch, **kw):
    from kestrel import doctor as doctor_mod
    from kestrel.config import get_settings

    def _no_doctor(*, quick: bool = False):
        raise RuntimeError("doctor skipped in this test")

    monkeypatch.setattr(doctor_mod, "run_doctor", _no_doctor)
    paths.ensure_dirs()
    archive = diagnostics.build_bundle(get_settings(), out_dir=tmp_path / "bundle", **kw)
    with zipfile.ZipFile(archive) as zf:
        return json.loads(zf.read("summary.json"))["tasks"]


async def _seed_split_secret() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        # A key that starts just before the 200-character cut: cutting first leaves "sk-abcd".
        s.add(Task(type="t2", status="failed", payload={},
                   last_error="y" * (diagnostics.TASK_ERROR_CHARS - 8) + " sk-abcdefghijklmnop zz"))
        await s.commit()
    await engine.dispose()


def test_a_secret_split_by_the_cut_is_scrubbed_before_the_cut(tmp_path, monkeypatch):
    asyncio.run(_seed_split_secret())
    tasks = _bundle_tasks(tmp_path, monkeypatch)
    err = tasks["recent"][0]["last_error"]
    assert "sk-" not in err and len(err) <= diagnostics.TASK_ERROR_CHARS


def test_conservative_mode_withholds_every_task_error(tmp_path, monkeypatch):
    asyncio.run(_seed())
    tasks = _bundle_tasks(tmp_path, monkeypatch, conservative=True)
    errors = [r["last_error"] for r in tasks["recent"] if r["last_error"]]
    assert errors and set(errors) == {diagnostics.WITHHELD}
    assert tasks["by_type_and_status"]["embed/failed"] == 1  # counts stay
