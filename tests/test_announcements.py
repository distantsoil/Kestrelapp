"""The "refreshed look is coming" notice (WP-22): a local, fixed-text, dismissible banner.
Cloud-safe: a migrated SQLite file, no network."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
from kestrel.db.models import Setting

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

NOTICE = "ui-refresh-2026"


async def _prepare() -> None:
    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()


def _client() -> TestClient:
    from kestrel.web.app import build_app

    asyncio.run(_prepare())
    return TestClient(build_app(worker=False))


async def _stored() -> object:
    async with get_sessionmaker()() as s:
        row = (
            await s.execute(select(Setting).where(Setting.key == "ui.announcements_dismissed"))
        ).scalar_one_or_none()
        return None if row is None else row.value


def test_pages_carry_the_slot_and_the_fragment_shows_the_notice() -> None:
    c = _client()
    page = c.get("/settings")
    assert 'hx-get="/announcements"' in page.text
    frag = c.get("/announcements")
    assert frag.status_code == 200
    assert 'data-testid="announcement"' in frag.text
    assert "A refreshed look is coming" in frag.text


def test_dismiss_stores_the_id_and_the_notice_stays_gone() -> None:
    c = _client()
    r = c.post(f"/announcements/{NOTICE}/dismiss")
    assert r.status_code == 200
    assert "announcement" not in r.text
    assert asyncio.run(_stored()) == [NOTICE]
    assert 'data-testid="announcement"' not in c.get("/announcements").text
    # Dismissing twice does not duplicate the id.
    c.post(f"/announcements/{NOTICE}/dismiss")
    assert asyncio.run(_stored()) == [NOTICE]


def test_unknown_id_is_ignored_with_200() -> None:
    c = _client()
    r = c.post("/announcements/not-a-real-id/dismiss")
    assert r.status_code == 200
    assert asyncio.run(_stored()) is None


def test_copy_has_no_em_dash_and_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    from kestrel.web.announcements import ANNOUNCEMENTS

    for a in ANNOUNCEMENTS:
        assert "—" not in a["text"] and "–" not in a["text"]

    def boom(*args, **kwargs):
        raise AssertionError("the notice must not touch the network")

    monkeypatch.setattr(httpx.AsyncClient, "send", boom)
    monkeypatch.setattr(httpx.Client, "send", boom)
    c = _client()
    assert c.get("/announcements").status_code == 200
    assert c.post(f"/announcements/{NOTICE}/dismiss").status_code == 200


def test_the_announcement_uses_the_guide_style_not_the_warning_style():
    # Good news must not look like an alert (hands-on testing, 2026-10-22).
    from pathlib import Path

    html = Path("kestrel/web/templates/partials/announcement.html").read_text(encoding="utf-8")
    assert 'class="guide-strip"' in html and "warnbox" not in html
