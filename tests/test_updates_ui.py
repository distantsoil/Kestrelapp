"""The update checker UI (banner, dialog, manual check, one-time cards, tray status, Settings
toggle). Cloud-safe: a migrated SQLite file, no network (check_now is monkeypatched)."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import kestrel
from kestrel import paths
from kestrel.db import migrate
from kestrel.db.base import utcnow_iso
from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
from kestrel.services import updater

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

PAGE = "https://kestrelapp.co/download"
NEWER = "99.0.0"
ROOT = Path(__file__).resolve().parents[1]


def _feed(version: str = NEWER) -> dict:
    return {
        "schema": 1,
        "version": version,
        "published_at": "2026-10-01",
        "download_page": PAGE,
        "downloads": {},
        "releases": [
            {
                "version": version,
                "date": "2026-10-01",
                "notes_md": "### Better\n- Faster <script>alert(1)</script> scans",
            },
            {"version": "0.0.1", "date": "2020-01-01", "notes_md": "- ancient"},
        ],
    }


async def _prepare(**fields) -> None:
    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    async with get_sessionmaker()() as s:
        state = updater.UpdateState(disclosure_shown=True, **fields)
        await updater.save_state(s, state)
    forget_engine()


def _newer_state(**extra) -> dict:
    return {"latest_version": NEWER, "download_page": PAGE, "cached_feed": _feed(), **extra}


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


async def _state() -> updater.UpdateState:
    async with get_sessionmaker()() as s:
        return await updater.load_state(s)


def test_banner_empty_without_update():
    asyncio.run(_prepare())
    with _client() as c:
        assert c.get("/updates/banner").text.strip() == ""


def test_banner_shows_version_and_base_wires_it():
    asyncio.run(_prepare(**_newer_state()))
    with _client() as c:
        body = c.get("/updates/banner").text
        page = c.get("/help").text
    assert f"Kestrel {NEWER} is available." in body
    assert "Show details" in body and "Later" in body and "See what is new" in body
    assert f"You are on {kestrel.__version__}." in body
    # D-UI-152: no self-update; the banner sends the user to the download page.
    link = f'href="{PAGE}" target="_blank" rel="noopener noreferrer">Get it from kestrelapp.co'
    assert link in body
    assert "Restart" not in body and "restart" not in body
    assert "Nothing about you is sent." in body
    assert 'hx-get="/updates/banner"' in page and 'id="update-modal"' in page


def test_banner_hidden_when_updates_disabled():
    from kestrel.db.models import Setting

    asyncio.run(_prepare(**_newer_state()))

    async def off():
        async with get_sessionmaker()() as s:
            s.add(Setting(key="updates.enabled", value=False))
            await s.commit()

    asyncio.run(off())
    forget_engine()
    with _client() as c:
        assert c.get("/updates/banner").text.strip() == ""
        assert c.get("/api/updates/status").json() == {"available": False, "version": None}


def test_remind_and_skip_hide_banner():
    asyncio.run(_prepare(**_newer_state()))
    with _client() as c:
        assert c.post("/updates/remind").text.strip() == ""
        assert c.get("/updates/banner").text.strip() == ""
    asyncio.run(_prepare(**_newer_state()))
    with _client() as c:
        assert c.post("/updates/skip", data={"version": NEWER}).text.strip() == ""
        assert c.get("/updates/banner").text.strip() == ""
    assert asyncio.run(_state()).skipped_version == NEWER


def test_details_escapes_and_links_plainly():
    asyncio.run(_prepare(**_newer_state()))
    with _client() as c:
        body = c.get("/updates/details").text
    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body
    assert "ancient" not in body  # not newer than the running version
    assert f'href="{PAGE}" target="_blank" rel="noopener noreferrer"' in body
    page_link = [h for h in re.findall(r'href="([^"]+)"', body) if not h.startswith("/static/")]
    assert "?" not in page_link[0]
    assert "Remind me tomorrow" in body and "Skip this version" in body
    assert "Your data stays where it is." in body


def _stub_check(monkeypatch, *, latest, ok=True):
    async def fake(session, *, feed_url, current=None, transport=None):
        assert "?" not in feed_url
        state = updater.UpdateState(
            latest_version=latest, last_checked_at=utcnow_iso(), last_check_ok=ok
        )
        return state

    monkeypatch.setattr(updater, "check_now", fake)


def test_check_outcomes_never_show_exception_text(monkeypatch):
    asyncio.run(_prepare())
    with _client() as c:
        _stub_check(monkeypatch, latest=NEWER)
        assert f"Kestrel {NEWER} is available." in c.post("/updates/check").text
        _stub_check(monkeypatch, latest=kestrel.__version__)
        assert f"Up to date (running {kestrel.__version__})." in c.post("/updates/check").text
        _stub_check(monkeypatch, latest=None, ok=False)
        assert "Couldn't reach kestrelapp.co. Kestrel will try again later." in (
            c.post("/updates/check").text
        )

        async def boom(*a, **k):
            raise RuntimeError("secret-boom-detail")

        monkeypatch.setattr(updater, "check_now", boom)
        r = c.post("/updates/check")
    assert r.status_code == 200
    assert "secret-boom-detail" not in r.text and "Couldn't reach" in r.text


def test_disclosure_card_once_and_turn_off():
    asyncio.run(_prepare())
    asyncio.run(_set_disclosure(False))
    with _client() as c:
        assert "checks kestrelapp.co once a day" in c.get("/updates/disclosure-card").text
        assert c.get("/").text.count("/updates/disclosure-card") == 1
        assert c.post("/updates/disclosure/off").text == ""
        assert c.get("/updates/disclosure-card").text == ""
        assert c.get("/api/updates/status").json()["available"] is False
        assert 'name="updates_enabled"' in c.get("/settings").text
    assert asyncio.run(_state()).disclosure_shown is True
    assert "checked" not in _checkbox_line(_client_settings())


def _checkbox_line(body: str) -> str:
    return next(ln for ln in body.splitlines()
                if 'type="checkbox"' in ln and 'name="updates_enabled"' in ln)


def _client_settings() -> str:
    with _client() as c:
        return c.get("/settings").text


async def _set_disclosure(value: bool) -> None:
    async with get_sessionmaker()() as s:
        state = await updater.load_state(s)
        state.disclosure_shown = value
        await updater.save_state(s, state)
    forget_engine()


def test_disclosure_ok_keeps_updates_on():
    asyncio.run(_prepare())
    asyncio.run(_set_disclosure(False))
    with _client() as c:
        assert c.post("/updates/disclosure/ok").text == ""
        assert c.get("/updates/disclosure-card").text == ""
        assert "checked" in _checkbox_line(c.get("/settings").text)


def test_whats_new_card_shows_once():
    asyncio.run(_prepare(**_newer_state(upgraded_from="0.0.0")))
    with _client() as c:
        body = c.get("/updates/whats-new-card").text
        assert f"Kestrel is now on version {kestrel.__version__}" in body
        assert "up from 0.0.0. Here is what changed." in body
        assert "Nothing about you was sent to check for updates." in body
        assert "Got it" in body and "See every change" in body
        assert "ancient" in body and "Faster" not in body  # only releases <= running version
        assert c.post("/updates/whats-new/dismiss").text == ""
        assert c.get("/updates/whats-new-card").text == ""
    assert asyncio.run(_state()).upgraded_from is None


def test_mark_update_seen_records_upgrade():
    asyncio.run(_prepare(last_seen_version="0.0.1"))

    async def run():
        from kestrel.web.app import _mark_update_seen

        await _mark_update_seen()
        return await _state()

    state = asyncio.run(run())
    assert state.upgraded_from == "0.0.1" and state.last_seen_version == kestrel.__version__


def test_status_endpoint_json():
    asyncio.run(_prepare())
    with _client() as c:
        assert c.get("/api/updates/status").json() == {"available": False, "version": None}
    asyncio.run(_prepare(**_newer_state()))
    with _client() as c:
        assert c.get("/api/updates/status").json() == {"available": True, "version": NEWER}


def test_settings_toggle_persists():
    asyncio.run(_prepare())
    with _client() as c:
        assert "checked" in _checkbox_line(c.get("/settings").text)
        assert "Last checked: never" in c.get("/settings").text
        c.post("/settings/updates", data={"updates_enabled": ["off"]})
        assert "checked" not in _checkbox_line(c.get("/settings").text)
        c.post("/settings/updates", data={"updates_enabled": ["off", "on"]})
        assert "checked" in _checkbox_line(c.get("/settings").text)


def test_about_has_check_button_and_old_help_route_is_gone():
    """The check lives on About (SPEC-INTERFACES 2.1 /about); Help no longer carries it."""
    asyncio.run(_prepare())
    with _client() as c:
        body = c.get("/about").text
        assert 'hx-post="/updates/check"' in body and ">Check for updates</button>" in body
        assert "update_check_url" not in body
        assert 'hx-post="/updates/check"' not in c.get("/help").text
        assert c.post("/help/update-check").status_code in (404, 405)


def test_about_check_says_where_to_get_it_and_never_offers_to_install(monkeypatch):
    """Kestrel never installs on its own: the answer names the version and links the download
    page, in About's wording ("Kestrel X.Y.Z is available. Get it from kestrelapp.co")."""
    asyncio.run(_prepare())
    with _client() as c:
        _stub_check(monkeypatch, latest=NEWER)
        r = c.post("/updates/check", data={"surface": "about"})
        assert f"Kestrel {NEWER} is available." in r.text
        assert f'href="{PAGE}"' in r.text and "Get it from kestrelapp.co</a>" in r.text
        assert 'id="update-last-checked"' in r.text and "hx-swap-oob" in r.text
        lowered = r.text.lower()
        assert "install" not in lowered and "restart" not in lowered and "<button" not in lowered
        _stub_check(monkeypatch, latest=kestrel.__version__)
        r = c.post("/updates/check", data={"surface": "about"})
        assert "You have the latest version" in r.text and "Up to date" not in r.text
        _stub_check(monkeypatch, latest=None, ok=False)
        r = c.post("/updates/check", data={"surface": "about"})
        assert "Couldn’t reach kestrelapp.co. Kestrel will try again later." in r.text
        # Any other caller keeps the shared wording.
        _stub_check(monkeypatch, latest=kestrel.__version__)
        assert "Up to date (running" in c.post("/updates/check").text


def test_no_em_dash_in_new_templates_or_copy():
    files = [ROOT / "kestrel/web/pages/updates.py"]
    files += list((ROOT / "kestrel/web/templates/partials").glob("update_*.html"))
    for f in files:
        assert "—" not in f.read_text(encoding="utf-8"), f.name
    asyncio.run(_prepare(**_newer_state(upgraded_from="0.0.0")))
    with _client() as c:
        for url in ("/updates/banner", "/updates/details", "/updates/whats-new-card"):
            assert "—" not in c.get(url).text, url


_EVIL = "javascript:alert(1)"


def test_unsafe_stored_download_page_falls_back_to_the_constant():
    asyncio.run(_prepare(**_newer_state(download_page=_EVIL, upgraded_from="0.0.0")))
    with _client() as c:
        for url in ("/updates/banner", "/updates/details", "/updates/whats-new-card"):
            body = c.get(url).text
            assert "javascript:" not in body, url
        assert f'href="{updater.DOWNLOAD_PAGE}"' in c.get("/updates/banner").text
        assert f'href="{updater.DOWNLOAD_PAGE}"' in c.get("/updates/details").text


def test_poll_update_status_keeps_last_value_on_bad_answers(monkeypatch):
    import httpx

    from kestrel.desktop import shell

    monkeypatch.setattr(shell, "icon", None)
    monkeypatch.setattr(shell, "_update_version", "1.2.3")

    def answer(status, body):
        monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(status, json=body))
        shell._poll_update_status()
        return shell._update_version

    assert answer(500, {"available": False}) == "1.2.3"
    assert answer(200, {"detail": "nope"}) == "1.2.3"
    assert answer(200, {"available": True, "version": "2.0.0"}) == "2.0.0"
    assert answer(200, {"available": False}) is None
