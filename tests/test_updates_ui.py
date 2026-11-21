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
    # D-UI-152: no self-update; with no release entry in the feed the banner sends the user to
    # the download page (D-UPD-8: with one, to the release file; tested below).
    assert f'href="{PAGE}" target="_blank" rel="noopener noreferrer"' in body
    assert ">Get it from kestrelapp.co" in body and "Download Kestrel" not in body
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


# --- the direct release download (D-UPD-8, D-UPD-9) -------------------------------------------

RELEASE = "https://github.com/distantsoil/Kestrelapp/releases/download"
FILES = {"windows": f"Kestrel-Setup-{NEWER}.exe", "macos": f"Kestrel-{NEWER}.dmg",
         "linux": f"Kestrel-{NEWER}-x86_64.AppImage"}
SHA = "cd" * 32


def _direct_feed(**tamper) -> dict:
    feed = _feed()
    feed["downloads"] = {
        p: {"file": f, "url": f"{RELEASE}/v{NEWER}/{f}", "sha256": SHA, "signed": False}
        for p, f in FILES.items()
    }
    for key, value in tamper.items():
        for entry in feed["downloads"].values():
            entry[key] = value
    return feed


def _this_file() -> str:
    return FILES[updater.feed_platform()]


def _hrefs(body: str) -> list[str]:
    return [h for h in re.findall(r'href="([^"]+)"', body) if not h.startswith("/static/")]


def test_details_offer_this_platforms_installer_from_the_release():
    asyncio.run(_prepare(**_newer_state(cached_feed=_direct_feed())))
    with _client() as c:
        body = c.get("/updates/details").text
    link = f"{RELEASE}/v{NEWER}/{_this_file()}"
    assert f'href="{link}" target="_blank" rel="noopener noreferrer"' in body
    assert f">Download Kestrel {NEWER}</a>" in body
    assert "Get it from kestrelapp.co" not in body
    assert "The file comes from GitHub, the Kestrel release page." in body
    # Show details: file, release, checksum; nothing about the user is sent.
    assert f"File: {_this_file()}" in body
    assert f"Release: v{NEWER} on github.com/distantsoil/Kestrelapp" in body
    assert f"SHA-256: {SHA}" in body and "Nothing about you is sent." in body
    # No query string on any link, and nothing that reads as a self-update.
    assert all("?" not in h for h in _hrefs(body))
    lowered = body.lower()
    assert "install now" not in lowered and "restart" not in lowered
    assert "Your data stays where it is." in body


@pytest.mark.parametrize(
    ("reason", "tamper"),
    [
        ("http", {"url": f"http://github.com/distantsoil/Kestrelapp/releases/download/v{NEWER}/x"}),
        ("other host", {"url": f"https://evil.example/distantsoil/Kestrelapp/releases/download/"
                               f"v{NEWER}/x"}),
        ("other repo", {"url": f"https://github.com/distantsoil/kestrel/releases/download/"
                               f"v{NEWER}/x"}),
        ("version mismatch", {"url": f"{RELEASE}/v{NEWER}1/x"}),
        ("bad sha", {"sha256": "nope"}),
        ("javascript", {"url": "javascript:alert(1)"}),
    ],
)
def test_a_tampered_release_entry_falls_back_to_the_site_on_every_surface(reason, tamper):
    feed = _direct_feed(**tamper)
    if "url" in tamper:
        for entry in feed["downloads"].values():
            entry["url"] = tamper["url"].replace("/x", "/" + entry["file"])
    asyncio.run(_prepare(**_newer_state(cached_feed=feed)))
    with _client() as c:
        details = c.get("/updates/details").text
        banner = c.get("/updates/banner").text
    for body in (details, banner):
        assert f'href="{PAGE}"' in body, reason
        assert ">Get it from kestrelapp.co" in body and "Download Kestrel" not in body, reason
        assert "github.com" not in "".join(_hrefs(body)), reason
        assert "javascript:" not in body, reason
    assert "The file comes from GitHub" not in details
    assert "On the download page, get the installer" in details


def test_a_feed_without_an_entry_for_this_platform_falls_back_to_the_site():
    feed = _direct_feed()
    del feed["downloads"][updater.feed_platform()]
    asyncio.run(_prepare(**_newer_state(cached_feed=feed)))
    with _client() as c:
        body = c.get("/updates/details").text
    assert f'href="{PAGE}"' in body and ">Get it from kestrelapp.co" in body
    assert "SHA-256" not in body


def test_every_update_surface_links_the_same_release_file(monkeypatch):
    from kestrel.db.models import Notification

    async def seed() -> None:
        async with get_sessionmaker()() as s:
            s.add(Notification(kind="update", severity="info",
                               title=f"Kestrel {NEWER} is available", body="See what's new."))
            await s.commit()

    asyncio.run(_prepare(**_newer_state(cached_feed=_direct_feed())))
    asyncio.run(seed())
    link = f"{RELEASE}/v{NEWER}/{_this_file()}"
    label = f"Download Kestrel {NEWER}"
    with _client() as c:
        banner = c.get("/updates/banner").text
        details = c.get("/updates/details").text
        bell = c.get("/notifications/list").text

        async def fake(session, *, feed_url, current=None, transport=None):
            return await updater.load_state(session)

        monkeypatch.setattr(updater, "check_now", fake)
        settings_result = c.post("/updates/check").text
        about_result = c.post("/updates/check", data={"surface": "about"}).text
    for name, body in (("banner", banner), ("details", details), ("bell", bell),
                       ("settings", settings_result), ("about", about_result)):
        assert f'href="{link}"' in body, name
        assert f">{label}" in body, name
        assert "Get it from kestrelapp.co" not in body, name
    assert 'data-testid="notif-action"' in bell and 'target="_blank"' in bell
    assert "The installer comes from GitHub, the Kestrel release page." in banner


def test_the_bell_falls_back_to_the_site_without_a_release_entry():
    from kestrel.db.models import Notification

    async def seed() -> None:
        async with get_sessionmaker()() as s:
            s.add(Notification(kind="update", severity="info", title="Kestrel is available"))
            await s.commit()

    asyncio.run(_prepare(**_newer_state()))
    asyncio.run(seed())
    with _client() as c:
        bell = c.get("/notifications/list").text
    assert f'href="{updater.DOWNLOAD_PAGE}"' in bell and ">Get it from kestrelapp.co" in bell


def test_whats_new_every_change_opens_the_changelog_and_fallback_the_release(monkeypatch):
    """#209 (D-UPD-11): the footer link is the full CHANGELOG; with no notes the fallback names
    the running version's GitHub release, not the download page."""
    monkeypatch.setattr(kestrel, "__version__", "0.7.6")
    asyncio.run(_prepare(**_newer_state(upgraded_from="0.0.0")))
    with _client() as c:
        body = c.get("/updates/whats-new-card").text
    every = body.split('data-testid="whats-new-every-change"')[0].rsplit("<a", 1)[1]
    assert 'href="https://github.com/distantsoil/Kestrelapp/blob/main/CHANGELOG.md"' in every
    asyncio.run(_prepare(upgraded_from="0.0.0", cached_feed={**_feed("0.7.6"), "releases": []}))
    with _client() as c:
        body = c.get("/updates/whats-new-card").text
    page = body.split('data-testid="whats-new-release-page"')[0].rsplit("<a", 1)[1]
    assert 'href="https://github.com/distantsoil/Kestrelapp/releases/tag/v0.7.6"' in page
