"""D-SRC-17: the dropped-title sample stays on the machine. A diagnostics bundle (which testers
forward) never carries it: not in summary.json, and no sampled title or org in any member."""

from __future__ import annotations

import asyncio
import json
import zipfile

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import ScrapeRun, Source
from kestrel.services import diagnostics

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_TITLES = ["Zebra Wrangler QX7", "Lantern Polisher ZK9"]
_ORGS = ["Fictional Menagerie QX7", "Imaginary Lamps ZK9"]


async def _seed() -> None:
    await reset_engine()
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        src = Source(track="employment", kind="rss", name="boards", enabled=True)
        s.add(src)
        await s.flush()
        s.add(
            ScrapeRun(
                source_id=src.id,
                status="ok",
                counts={
                    "found": 9,
                    "dropped_unrelated_title": 2,
                    "dropped_outside_area": 1,
                    "dropped_title_sample": [
                        {"title": t, "org": o} for t, o in zip(_TITLES, _ORGS, strict=True)
                    ],
                },
            )
        )
        await s.commit()
    await engine.dispose()
    forget_engine()


def test_bundle_never_contains_the_dropped_title_sample(tmp_path, monkeypatch):
    from kestrel import doctor as doctor_mod
    from kestrel.config import get_settings

    def _no_doctor(*, quick: bool = False):
        raise RuntimeError("doctor skipped in this test")

    monkeypatch.setattr(doctor_mod, "run_doctor", _no_doctor)
    asyncio.run(_seed())
    paths.ensure_dirs()
    (paths.logs_dir() / "kestrel.log").write_text(
        '{"event": "run finished", "found": 9}\n', encoding="utf-8"
    )
    archive = diagnostics.build_bundle(get_settings(), out_dir=tmp_path / "bundle")
    with zipfile.ZipFile(archive) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    assert "summary.json" in members and "logs/kestrel.log" in members
    for name, data in members.items():
        for needle in (*_TITLES, *_ORGS, "dropped_title_sample"):
            assert needle.encode() not in data, (name, needle)
    summary = json.loads(members["summary.json"])
    runs = summary["scrape_runs"]
    assert len(runs) == 1
    counts = runs[0]["counts"]
    assert "dropped_title_sample" not in counts
    assert counts["dropped_unrelated_title"] == 2
    assert counts["dropped_outside_area"] == 1 and counts["found"] == 9
