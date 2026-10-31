"""The announcement mechanism (WP-22): a local, fixed-text, dismissible banner. The shipped list
is empty (the refreshed-look notice was removed, D-UI-157); a test-only notice proves the rest.
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

NOTICE = "test-notice"
TEXT = "A test notice for the mechanism."


@pytest.fixture
def notice(monkeypatch: pytest.MonkeyPatch) -> None:
    from kestrel.web import announcements

    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", ({"id": NOTICE, "text": TEXT},))


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


def test_the_shipped_list_is_empty_and_the_refreshed_look_notice_is_gone() -> None:
    from kestrel.web.announcements import ANNOUNCEMENTS

    assert ANNOUNCEMENTS == ()
    c = _client()
    for path in ("/", "/announcements", "/sources", "/quarantine", "/settings", "/help"):
        text = c.get(path).text.lower()
        assert "refreshed look" not in text, path
        assert 'data-testid="announcement"' not in text, path
    assert c.get("/announcements").text.strip() == ""


def test_today_carries_the_slot_under_the_whats_new_card_and_the_fragment_shows_it(notice) -> None:
    c = _client()
    page = c.get("/").text
    assert page.count('id="announcement-slot"') == 1
    assert 'hx-get="/announcements"' in page
    # D-UI-157: it sits where the small-surfaces mockup puts the what's-new card.
    assert page.index('id="whats-new-slot"') < page.index('id="announcement-slot"')
    assert page.index('id="announcement-slot"') < page.index('id="today"') + 2000
    frag = c.get("/announcements")
    assert frag.status_code == 200
    assert 'data-testid="announcement"' in frag.text
    assert TEXT in frag.text


@pytest.mark.parametrize("path", [
    "/stories", "/sources", "/quarantine", "/pipeline", "/applications", "/profile",
    "/prep", "/offers", "/calibrate", "/analytics", "/models", "/runs", "/settings",
    "/help", "/about", "/backup",
])
def test_no_other_page_carries_the_slot(path: str, notice) -> None:
    c = _client()
    r = c.get(path)
    assert r.status_code in (200, 307, 308) or r.status_code == 404, r.status_code
    if r.status_code == 200:
        assert "announcement-slot" not in r.text and "/announcements" not in r.text
        assert TEXT not in r.text


def test_once_dismissed_today_never_shows_it_again(notice) -> None:
    c = _client()
    assert 'data-testid="announcement"' in c.get("/announcements").text
    c.post(f"/announcements/{NOTICE}/dismiss")
    # Today still renders the (empty) slot; the fragment it loads is now empty, on every visit.
    assert 'id="announcement-slot"' in c.get("/").text
    for _ in range(2):
        assert 'data-testid="announcement"' not in c.get("/announcements").text


def test_dismiss_stores_the_id_and_the_notice_stays_gone(notice) -> None:
    c = _client()
    r = c.post(f"/announcements/{NOTICE}/dismiss")
    assert r.status_code == 200
    assert "announcement" not in r.text
    assert asyncio.run(_stored()) == [NOTICE]
    assert 'data-testid="announcement"' not in c.get("/announcements").text
    # Dismissing twice does not duplicate the id.
    c.post(f"/announcements/{NOTICE}/dismiss")
    assert asyncio.run(_stored()) == [NOTICE]


def test_unknown_id_is_ignored_with_200(notice) -> None:
    c = _client()
    r = c.post("/announcements/not-a-real-id/dismiss")
    assert r.status_code == 200
    assert asyncio.run(_stored()) is None


def test_copy_has_no_em_dash_and_no_network(monkeypatch: pytest.MonkeyPatch, notice) -> None:
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


def test_the_announcement_uses_the_notice_style_not_the_warning_style():
    # Good news must not look like an alert (hands-on testing, 2026-10-22).
    from pathlib import Path

    html = Path("kestrel/web/templates/partials/announcement.html").read_text(encoding="utf-8")
    assert 'class="sf-notice"' in html and "warnbox" not in html


def test_with_no_announcements_the_dismiss_route_is_a_harmless_200() -> None:
    c = _client()
    assert c.post("/announcements/anything/dismiss").status_code == 200
    assert asyncio.run(_stored()) is None
