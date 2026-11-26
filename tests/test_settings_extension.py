"""Settings > Browser extension (issue #186): the section renders, its status line, the store
buttons, the connection key and the three switches. Cloud-safe: TestClient on the real app, a
throwaway data directory, no network. Invented text only.

The invariant: Copy key never changes whether other devices may open Kestrel (server.bind_all in
settings.yml and every other setting row stay as they were) and never replaces a key that
exists; Make a new key replaces it and refreshes auth's cached key."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.web import auth

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

KEY = "invented-connection-key-0001"
LAN_PEER = "192.168.1.50"


@pytest.fixture(autouse=True)
def _fresh_auth_cache():
    auth.reset_cache()
    yield
    auth.reset_cache()


def _seed_settings_yml() -> None:
    paths.ensure_dirs()
    (paths.config_dir() / "settings.yml").write_text(
        (paths.packaged_config() / "settings.yml").read_text(encoding="utf-8"), encoding="utf-8")


def _client(peer: str = "127.0.0.1") -> TestClient:
    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine
    from kestrel.web.app import build_app

    migrate.run_migrations()
    forget_engine()
    return TestClient(build_app(worker=False), base_url="http://127.0.0.1",
                      raise_server_exceptions=False, client=(peer, 12345))


def _rows() -> dict[str, str]:
    con = sqlite3.connect(paths.db_path())
    try:
        return dict(con.execute("select key, value from setting").fetchall())
    finally:
        con.close()


def _put(key: str, raw_json: str) -> None:
    con = sqlite3.connect(paths.db_path())
    try:
        con.execute("insert or replace into setting (key, value, updated_at) values (?, ?, ?)",
                    (key, raw_json, "2026-01-01T00:00:00Z"))
        con.commit()
    finally:
        con.close()


def _settings_yml() -> bytes:
    return (paths.config_dir() / "settings.yml").read_bytes()


@pytest.fixture
def bind_recorder(monkeypatch):
    """Every call that would change Access from other devices, recorded (a runtime recorder, not
    a text scan): Copy key must make none."""
    from kestrel.services import lan_access

    calls: list[bool] = []
    real = lan_access.set_bind_all

    def record(value: bool):
        calls.append(value)
        return real(value)

    monkeypatch.setattr(lan_access, "set_bind_all", record)
    return calls


# ------------------------------------------------------------------------------ the section
def test_the_section_is_listed_after_privacy_and_renders_its_cards():
    from kestrel.web import settings_view as sv

    ids = [s[0] for s in sv.SECTIONS]
    assert ids.index("extension") == ids.index("privacy") + 1
    assert ("extension", "Browser extension") == sv.SECTIONS[ids.index("extension")][:2]
    with _client() as c:
        page = c.get("/settings")
        frag = c.get("/settings/section/extension")
    assert page.status_code == 200 and frag.status_code == 200
    assert 'data-testid="st-nav-extension"' in page.text
    assert 'id="st-sec-extension"' in page.text
    for text in ("Browser extension",
                 "Bring the posting you are looking at into Kestrel, in one click.",
                 "A posting you have open", "You click Capture or Read",
                 "Kestrel scores it, on this computer", "Add it to your browser",
                 "Get it for Chrome", "Get it for Firefox",
                 "Edge, Brave and Arc use the Chrome version. Safari's comes with Kestrel "
                 "for Mac.", "Connect it", "Connection key", "Copy key",
                 "Paste it into the extension once. Kestrel refuses changes from any other "
                 "extension without it.", "What it does", "Show Kestrel scores on job boards",
                 "Offer roles that are waiting for a description",
                 "Show a count on the toolbar icon", "Show details", "chrome://extensions",
                 "Developer mode", "Load unpacked", "Make a new key",
                 "The extension and any devices you joined will need the new key."):
        assert text in frag.text, text
    assert "Turn it on in Safari" not in frag.text  # no Safari build yet
    for title in ("It never acts for you", "Stays on this computer",
                  "Why a description goes missing"):
        assert title in page.text, title
    assert "—" not in frag.text  # no em dashes in the copy


def test_the_help_cards_say_the_exact_words():
    from kestrel.web import settings_view as sv

    cards = {c[0]: c for c in sv.HELP["extension"]}
    assert cards["It never acts for you"][2] == "info"
    assert cards["It never acts for you"][3] == (
        "Kestrel never applies, fills in a form or sends a message. The extension reads a page "
        "only when you click, and only the page you have open.")
    assert cards["Stays on this computer"][2] == "good"
    assert cards["Stays on this computer"][3] == (
        "The extension talks only to Kestrel on this computer. Nothing goes to any other "
        "server, and nothing is collected.")
    assert cards["Why a description goes missing"][2] == "plain"
    assert cards["Why a description goes missing"][3] == (
        'Some job boards show Kestrel a "verify you are human" page instead of the posting. '
        "Open it in your browser, then use Read in the extension to fill it in.")


# The Chrome listing is live; Firefox stays empty until it is approved (D-EXT-960). The exact
# string is pinned: a query, another host or another path is a different link and must fail here.
CHROME_LISTING = ("https://chromewebstore.google.com/detail/kestrel-capture/"
                  "fdnolbohdjgelmfbhkbedlbjckfebaib")


def _pins_the_listings(urls: dict[str, str]) -> None:
    assert urls == {"chrome": CHROME_LISTING, "firefox": ""}


def _store_anchor(frag: str, browser: str) -> str:
    return frag.split(f'data-testid="xt-get-{browser}"')[0].rsplit("<a", 1)[1]


def test_the_store_urls_are_exactly_the_chrome_listing_and_no_firefox():
    from kestrel.services import extension as ext

    assert ext.STORE_URLS == {"chrome": CHROME_LISTING, "firefox": ""}
    assert ext.STORE_URLS["chrome"] == (
        "https://chromewebstore.google.com/detail/kestrel-capture/"
        "fdnolbohdjgelmfbhkbedlbjckfebaib")


@pytest.mark.parametrize("chrome", [
    CHROME_LISTING + "?hl=en",
    CHROME_LISTING + "?utm_source=kestrel",
    CHROME_LISTING + "/",
    CHROME_LISTING.replace("https://", "http://"),
    CHROME_LISTING.replace("chromewebstore.google.com", "chrome.google.com/webstore"),
    CHROME_LISTING.replace("chromewebstore.google.com", "chromewebstore.example.test"),
    "",
])
def test_a_store_url_with_a_query_or_another_host_fails_the_pin(chrome):
    with pytest.raises(AssertionError):
        _pins_the_listings({"chrome": chrome, "firefox": ""})


def test_a_firefox_url_before_its_approval_fails_the_pin():
    with pytest.raises(AssertionError):
        _pins_the_listings({"chrome": CHROME_LISTING,
                            "firefox": "https://addons.mozilla.org/firefox/addon/kestrel/"})


def test_chrome_opens_its_listing_outside_and_firefox_still_opens_help():
    from kestrel.services import extension as ext

    _pins_the_listings(ext.STORE_URLS)
    with _client() as c:
        frag = c.get("/settings/section/extension").text
    chrome = _store_anchor(frag, "chrome")
    firefox = _store_anchor(frag, "firefox")
    assert f'href="{CHROME_LISTING}"' in chrome
    assert 'target="_blank"' in chrome and 'rel="noopener noreferrer"' in chrome
    assert re.search(r"\son[a-z]+\s*=", chrome, re.IGNORECASE) is None  # no inline handler
    assert frag.count("chromewebstore.google.com") == 1
    assert 'href="/help"' in firefox
    assert "target=" not in firefox and "rel=" not in firefox
    assert "k-btn--primary" in chrome and "k-btn--primary" not in firefox
    assert "Edge, Brave and Arc use the Chrome version." in frag


def test_the_store_buttons_open_help_while_there_is_no_listing(monkeypatch):
    from kestrel.services import extension as ext

    monkeypatch.setitem(ext.STORE_URLS, "chrome", "")
    monkeypatch.setitem(ext.STORE_URLS, "firefox", "")
    assert ext.STORE_URLS == {"chrome": "", "firefox": ""}
    with _client() as c:
        frag = c.get("/settings/section/extension").text
    chrome = _store_anchor(frag, "chrome")
    firefox = _store_anchor(frag, "firefox")
    assert 'href="/help"' in chrome and 'href="/help"' in firefox
    assert "target=" not in chrome and "target=" not in firefox
    assert "chromewebstore" not in frag
    assert "k-btn--primary" in chrome and "k-btn--primary" not in firefox


def test_a_listed_store_page_is_used_and_opens_outside(monkeypatch):
    from kestrel.services import extension as ext

    monkeypatch.setitem(ext.STORE_URLS, "chrome", "https://store.example.test/kestrel")
    with _client() as c:
        frag = c.get("/settings/section/extension").text
    chrome = _store_anchor(frag, "chrome")
    assert 'href="https://store.example.test/kestrel"' in chrome and 'rel="noopener' in chrome


def test_the_key_is_never_written_into_the_page():
    with _client() as c:
        c.get("/settings")  # migrate
        _put(auth.SETTING_KEY, f'"{KEY}"')
        page = c.get("/settings").text
        frag = c.get("/settings/section/extension").text
    assert KEY not in page and KEY not in frag


# ---------------------------------------------------------------------------- status line
def test_not_connected_yet_when_the_extension_never_called():
    with _client() as c:
        frag = c.get("/settings/section/extension").text
    status = frag.split('data-testid="xt-status"', 1)[1].split("</span>", 1)[0]
    assert "Not connected yet" in status and "Connected" not in status.replace("Not connected",
                                                                               "")


def test_connected_names_the_browser_and_how_long_ago(monkeypatch):
    from kestrel.web.pages import settings_extension as page

    now = datetime(2026, 3, 4, 10, 0, tzinfo=UTC)
    monkeypatch.setattr(page, "_now", lambda tz=None: now)
    at = (now - timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
    with _client() as c:
        c.get("/settings")
        _put("extension.last_seen", f'{{"at": "{at}", "browser": "Chrome"}}')
        frag = c.get("/settings/section/extension").text
    status = frag.split('data-testid="xt-status"', 1)[1].split("</span>", 1)[0]
    assert "Connected · Chrome · used 2 minutes ago" in status
    assert "k-pill--good" in frag.split('data-testid="xt-status"', 1)[0].rsplit("<span", 1)[1]


def test_the_status_poll_answers_204_until_the_pill_would_change(monkeypatch):
    """Issue #265: the pill's poll leaves an unchanged pill alone (204) and returns the new pill,
    with the value it now shows, once the extension has called."""
    from kestrel.web.pages import settings_extension as page

    now = datetime(2026, 3, 4, 10, 0, tzinfo=UTC)
    monkeypatch.setattr(page, "_now", lambda tz=None: now)
    with _client() as c:
        frag = c.get("/settings/section/extension").text
        shown = frag.split("/settings/extension/status?shown=", 1)[1].split('"', 1)[0]
        assert shown == "n"
        same = c.get(f"/settings/extension/status?shown={shown}")
        assert same.status_code == 204 and same.text == ""
        at = (now - timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
        _put("extension.last_seen", f'{{"at": "{at}", "browser": "Chrome"}}')
        moved = c.get(f"/settings/extension/status?shown={shown}")
        assert moved.status_code == 200
        assert "Connected · Chrome · used 2 minutes ago" in moved.text
        assert 'id="xt-status"' in moved.text and "k-pill--good" in moved.text
        now_shown = moved.text.split("/settings/extension/status?shown=", 1)[1].split('"', 1)[0]
        assert c.get(f"/settings/extension/status?shown={now_shown}").status_code == 204


# ------------------------------------------------------------------------- the switches
def test_a_value_set_through_set_features_shows_in_the_card():
    import asyncio

    from kestrel.db.engine import get_sessionmaker
    from kestrel.services import extension as ext

    with _client() as c:
        c.get("/settings")

        async def set_off():
            async with get_sessionmaker()() as s:
                await ext.set_features(s, {"badges": False, "toolbar_count": False})
                await s.commit()

        asyncio.run(set_off())
        frag = c.get("/settings/section/extension").text

    def checked(name: str) -> bool:
        tag = frag.split(f'data-testid="xt-sw-{name}"')[0].rsplit("<input", 1)[1]
        return " checked" in tag

    assert checked("badges") is False
    assert checked("waiting") is True  # untouched: the default is on
    assert checked("toolbar_count") is False


def test_a_value_saved_from_the_card_is_what_features_returns():
    import asyncio

    from kestrel.db.engine import get_sessionmaker
    from kestrel.services import extension as ext

    with _client() as c:
        off = c.post("/settings/extension/features", json={"key": "waiting", "on": False})
        assert off.json()["ok"] is True
        assert off.json()["message"] == (
            "The extension will no longer offer roles that are waiting for a description.")

        async def read():
            async with get_sessionmaker()() as s:
                return await ext.features(s)

        assert asyncio.run(read()) == {"badges": True, "waiting": False, "toolbar_count": True}
        on = c.post("/settings/extension/features", json={"key": "waiting", "on": True})
        assert on.json()["ok"] is True
        assert asyncio.run(read())["waiting"] is True


@pytest.mark.parametrize("body", [
    {"key": "last_seen", "on": True},
    {"key": "badges", "on": "yes"},
    {"key": "badges"},
    ["badges", True],
])
def test_the_switch_route_refuses_anything_but_a_known_switch_and_a_boolean(body):
    with _client() as c:
        c.get("/settings")
        before = _rows()
        r = c.post("/settings/extension/features", json=body)
        assert r.status_code == 200 and r.json()["ok"] is False
        assert _rows() == before


# ---------------------------------------------------------------------------- Copy key
def test_copy_key_makes_a_key_when_there_is_none_and_never_turns_on_access(bind_recorder):
    _seed_settings_yml()
    with _client() as c:
        c.get("/settings")
        yml, rows = _settings_yml(), _rows()
        assert auth.SETTING_KEY not in rows
        r = c.post("/settings/extension/key")
        assert r.status_code == 200 and r.json()["ok"] is True
        key = r.json()["key"]
        assert r.headers["cache-control"] == "no-store"
        after = _rows()
    assert isinstance(key, str) and len(key) >= 32
    assert after.pop(auth.SETTING_KEY) == f'"{key}"'
    assert after == rows                        # no other row, server.bind_all included
    assert _settings_yml() == yml               # settings.yml untouched
    assert bind_recorder == []                  # nothing asked to change Access
    assert auth.cached_key() == key


def test_copy_key_never_replaces_a_key_that_exists(bind_recorder):
    _seed_settings_yml()
    with _client() as c:
        c.get("/settings")
        _put(auth.SETTING_KEY, f'"{KEY}"')
        yml, rows = _settings_yml(), _rows()
        first = c.post("/settings/extension/key").json()
        second = c.post("/settings/extension/key").json()
    assert first["key"] == KEY and second["key"] == KEY
    assert _rows() == rows and _settings_yml() == yml and bind_recorder == []


def test_copy_key_keeps_access_on_as_it_was(bind_recorder):
    """With Access from other devices already on, Copy key leaves it on and the key unchanged."""
    _seed_settings_yml()
    with _client() as c:
        assert c.post("/settings/privacy/access", data={"on": "1", "ack": "on"}).status_code == 200
        bind_recorder.clear()
        yml, rows = _settings_yml(), _rows()
        key = c.post("/settings/extension/key").json()["key"]
    assert rows[auth.SETTING_KEY] == f'"{key}"'
    assert _rows() == rows and _settings_yml() == yml and bind_recorder == []


# --------------------------------------------------------------------------- Make a new key
def test_make_a_new_key_replaces_it_and_refreshes_the_cached_key(bind_recorder):
    _seed_settings_yml()
    with _client() as c:
        c.get("/settings")
        _put(auth.SETTING_KEY, f'"{KEY}"')
        auth.set_cached_key(KEY)
        yml = _settings_yml()
        r = c.post("/settings/extension/key/new", data={"ack": "on"})
        assert r.status_code == 200
        assert "New key made. Paste it into the extension again." in r.text
        new = _rows()[auth.SETTING_KEY]
        cached = auth.cached_key()  # read before anything else could refresh it
        copied = c.post("/settings/extension/key").json()["key"]
    assert new != f'"{KEY}"' and new == f'"{copied}"'
    assert cached == copied != KEY
    assert _settings_yml() == yml and bind_recorder == []
    assert copied not in r.text  # the card never carries the key


def test_make_a_new_key_without_the_confirm_changes_nothing():
    with _client() as c:
        c.get("/settings")
        _put(auth.SETTING_KEY, f'"{KEY}"')
        r = c.post("/settings/extension/key/new")
        assert "Confirm that you understand first." in r.text
        assert _rows()[auth.SETTING_KEY] == f'"{KEY}"'


@pytest.mark.parametrize("path", ["/settings/extension/key", "/settings/extension/key/new"])
def test_a_key_holder_on_another_device_cannot_copy_or_change_the_key(path):
    with _client() as c:
        c.get("/settings")
        _put(auth.SETTING_KEY, f'"{KEY}"')
    auth.set_cached_key(KEY)
    with _client(LAN_PEER) as remote:
        before = _rows()
        r = remote.post(path, data={"ack": "on"}, headers={"X-Kestrel-Key": KEY})
    assert r.status_code == 403 and KEY not in r.text
    assert _rows() == before
