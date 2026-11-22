"""Update checker (SPEC-INTERFACES.md 1.5): version math, feed fetch, persisted state, privacy."""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

import kestrel
from kestrel.config import load_settings
from kestrel.db.models import Notification
from kestrel.services import setting_overrides, updater

FIX = Path(__file__).parent / "fixtures" / "updates"
NOW = "2026-01-01T00:00:00+00:00"


def _feed_text() -> str:
    return (FIX / "latest.json").read_text(encoding="utf-8")


def _feed() -> updater.LatestFeed:
    return updater.LatestFeed.model_validate(json.loads(_feed_text()))


def _transport(handler):
    return httpx.MockTransport(handler)


def _ok(seen: list[httpx.Request] | None = None, etag: str | None = '"abc"'):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        headers = {"ETag": etag} if etag else {}
        return httpx.Response(200, text=_feed_text(), headers=headers)

    return _transport(handler)


def _status(code: int):
    return _transport(lambda request: httpx.Response(code))


def _future(hours: int = 5) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


# --- versions ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cand", "cur", "expected"),
    [
        ("v0.4.0", "0.3.0", True),
        ("0.10.0", "0.9.0", True),
        ("0.9.0", "0.10.0", False),
        ("0.3.0", "0.3.0", False),
        ("1.0.0-rc.1", "1.0.0", False),
        ("1.0.0", "1.0.0-rc.1", True),
        ("1.0.0-rc.2", "1.0.0-rc.1", True),
        ("1.0.0-rc.1", "1.0.0-rc.2", False),
    ],
)
def test_is_newer(cand, cur, expected):
    assert updater.is_newer(cand, cur) is expected


def test_garbage_version():
    assert updater.parse_version("garbage") is None
    assert updater.parse_version("v1.2.3") is not None
    assert updater.is_newer("garbage", "0.3.0") is False
    assert updater.is_newer("0.4.0", "garbage") is False


def test_pending_releases_newest_first():
    feed = _feed()
    got = [r.version for r in updater.pending_releases(feed, "0.3.0")]
    assert got
    assert got[0] == "0.4.0"
    assert "0.3.0" not in got
    keys = [updater.parse_version(v) for v in got]
    assert keys == sorted(keys, reverse=True)
    assert [r.version for r in updater.pending_releases(feed, "0.3.5")] == ["0.4.0"]
    assert updater.pending_releases(feed, "0.4.0") == []


# --- notes rendering --------------------------------------------------------------------------


def test_render_notes_escapes_html():
    out = updater.render_notes_html("hello <script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_render_notes_fixture_script_is_escaped():
    joined = "\n".join(r.notes_md for r in _feed().releases)
    assert "<script>" in joined
    assert "<script>" not in updater.render_notes_html(joined)


def test_render_notes_heading_and_list():
    assert "<h4>" in updater.render_notes_html("### New")
    out = updater.render_notes_html("- a\n- b")
    assert out.count("<ul>") == 1
    assert out.count("<li>") == 2


# --- banner -----------------------------------------------------------------------------------


def test_banner_version_rules():
    st = updater.UpdateState(latest_version="0.4.0")
    assert updater.banner_version(st, "0.3.0", NOW) == "0.4.0"
    assert updater.banner_version(st, "0.4.0", NOW) is None
    assert updater.banner_version(st, "0.5.0", NOW) is None
    assert updater.banner_version(updater.UpdateState(), "0.3.0", NOW) is None
    skipped = updater.UpdateState(latest_version="0.4.0", skipped_version="0.4.0")
    assert updater.banner_version(skipped, "0.3.0", NOW) is None
    older = updater.UpdateState(latest_version="0.4.0", skipped_version="0.3.5")
    assert updater.banner_version(older, "0.3.0", NOW) == "0.4.0"
    future = updater.UpdateState(latest_version="0.4.0", remind_after="2026-01-02T00:00:00+00:00")
    assert updater.banner_version(future, "0.3.0", NOW) is None
    past = updater.UpdateState(latest_version="0.4.0", remind_after="2025-12-31T00:00:00+00:00")
    assert updater.banner_version(past, "0.3.0", NOW) == "0.4.0"


# --- fetch_feed -------------------------------------------------------------------------------


async def test_fetch_200():
    feed, etag, nm = await updater.fetch_feed(updater.FEED_URL, None, transport=_ok())
    assert feed is not None
    assert feed.version == "0.4.0"
    assert etag == '"abc"'
    assert nm is False


async def test_fetch_304():
    feed, etag, nm = await updater.fetch_feed(updater.FEED_URL, '"abc"', transport=_status(304))
    assert (feed, etag, nm) == (None, '"abc"', True)


@pytest.mark.parametrize("code", [404, 500])
async def test_fetch_http_errors_do_not_raise(code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(code)

    feed, _etag, nm = await updater.fetch_feed(
        updater.FEED_URL, None, transport=_transport(handler)
    )
    assert feed is None
    assert nm is False
    if code == 500:
        assert len(calls) <= 2  # at most one retry


async def test_fetch_timeout_does_not_raise():
    def handler(request):
        raise httpx.ConnectTimeout("slow", request=request)

    feed, _etag, nm = await updater.fetch_feed(
        updater.FEED_URL, None, transport=_transport(handler)
    )
    assert feed is None
    assert nm is False


@pytest.mark.parametrize("name", ["latest_malformed.json", "latest_schema99.json"])
async def test_fetch_bad_body(name):
    body = (FIX / name).read_text(encoding="utf-8")
    t = _transport(lambda request: httpx.Response(200, text=body))
    feed, _etag, nm = await updater.fetch_feed(updater.FEED_URL, None, transport=t)
    assert feed is None
    assert nm is False


async def test_request_privacy():
    seen: list[httpx.Request] = []
    await updater.fetch_feed(updater.FEED_URL, None, transport=_ok(seen))
    assert len(seen) == 1
    req = seen[0]
    assert req.method == "GET"
    assert not req.url.query
    names = {k.lower() for k in req.headers}
    allowed = {"user-agent", "accept", "if-none-match", "accept-encoding", "connection", "host"}
    assert names <= allowed
    assert "cookie" not in names
    assert "if-none-match" not in names
    assert req.headers["user-agent"] == "Kestrel-Updater"
    assert all(kestrel.__version__ not in v for v in req.headers.values())


async def test_request_sends_etag_only_when_cached():
    seen: list[httpx.Request] = []
    await updater.fetch_feed(updater.FEED_URL, '"abc"', transport=_ok(seen))
    assert seen[0].headers["if-none-match"] == '"abc"'


# --- check_now / state ------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _shared_sessionmaker_is_the_test_db(sm, monkeypatch):
    """check_now notifies through the app's shared sessionmaker; point that at the test DB."""
    monkeypatch.setattr(updater, "get_sessionmaker", lambda: sm)


async def _notifications(sm) -> list[Notification]:
    async with sm() as s:
        return list((await s.execute(select(Notification))).scalars())


async def test_check_now_persists(sm):
    async with sm() as s:
        st = await updater.check_now(s, current="0.3.0", transport=_ok())
        assert st.latest_version == "0.4.0"
    async with sm() as s:
        loaded = await updater.load_state(s)
    assert loaded.latest_version == "0.4.0"
    assert loaded.download_page
    assert loaded.last_checked_at
    assert loaded.etag == '"abc"'


async def test_check_now_failure_keeps_prior(sm):
    async with sm() as s:
        await updater.check_now(s, current="0.3.0", transport=_ok())
    async with sm() as s:
        before = await updater.load_state(s)
    async with sm() as s:
        await updater.check_now(s, current="0.3.0", transport=_status(500))
    async with sm() as s:
        after = await updater.load_state(s)
    assert after.latest_version == "0.4.0"
    assert after.download_page == before.download_page
    assert after.last_checked_at
    assert after.last_checked_at >= before.last_checked_at


async def test_check_now_304_keeps_cached(sm):
    async with sm() as s:
        await updater.check_now(s, current="0.3.0", transport=_ok())
    async with sm() as s:
        await updater.check_now(s, current="0.3.0", transport=_status(304))
    async with sm() as s:
        st = await updater.load_state(s)
    assert st.latest_version == "0.4.0"
    assert st.download_page
    assert st.etag == '"abc"'


async def test_check_now_notifies_once(sm):
    for _ in range(2):
        async with sm() as s:
            await updater.check_now(s, current="0.3.0", transport=_ok())
    rows = [n for n in await _notifications(sm) if n.kind == "update"]
    assert len(rows) == 1


async def test_check_now_no_notification_when_current(sm):
    async with sm() as s:
        await updater.check_now(s, current="0.4.0", transport=_ok())
    assert [n for n in await _notifications(sm) if n.kind == "update"] == []


async def test_second_check_sends_if_none_match(sm):
    seen: list[httpx.Request] = []
    async with sm() as s:
        await updater.check_now(s, current="0.3.0", transport=_ok(seen))
    async with sm() as s:
        await updater.check_now(s, current="0.3.0", transport=_ok(seen))
    assert "if-none-match" not in seen[0].headers
    assert seen[1].headers["if-none-match"] == '"abc"'


# --- mark_seen / skip / remind ----------------------------------------------------------------


async def test_mark_seen(sm):
    async with sm() as s:
        first = await updater.mark_seen(s, current="0.3.0")
        assert first is None or first == "0.3.0"
    async with sm() as s:
        assert await updater.mark_seen(s, current="0.3.0") is None
    async with sm() as s:
        assert await updater.mark_seen(s, current="0.4.0") == "0.3.0"
    async with sm() as s:
        assert await updater.mark_seen(s, current="0.4.0") is None


async def test_skip_version_hides_banner(sm):
    async with sm() as s:
        await updater.check_now(s, current="0.3.0", transport=_ok())
    async with sm() as s:
        assert updater.banner_version(await updater.load_state(s), "0.3.0", NOW) == "0.4.0"
        await updater.skip_version(s, "0.4.0")
    async with sm() as s:
        st = await updater.load_state(s)
    assert st.skipped_version == "0.4.0"
    assert updater.banner_version(st, "0.3.0", NOW) is None


async def test_remind_later_hides_banner(sm):
    async with sm() as s:
        await updater.check_now(s, current="0.3.0", transport=_ok())
    async with sm() as s:
        await updater.remind_later(s, hours=24)
    async with sm() as s:
        st = await updater.load_state(s)
    assert st.remind_after
    now = datetime.now(UTC).isoformat()
    assert updater.banner_version(st, "0.3.0", now) is None
    later = (datetime.now(UTC) + timedelta(hours=25)).isoformat()
    assert updater.banner_version(st, "0.3.0", later) == "0.4.0"


# --- config and guards ------------------------------------------------------------------------


def test_config_defaults():
    u = load_settings().updates
    assert u.enabled is True
    assert u.feed_url == "https://kestrelapp.co/latest.json"
    assert u.check_hours == 24


def test_feed_url_constant():
    assert updater.FEED_URL == "https://kestrelapp.co/latest.json"


def test_module_never_downloads_or_executes():
    src = inspect.getsource(updater)
    for banned in ("def download", "subprocess", "os.startfile", "Popen"):
        assert banned not in src


def test_state_key_is_transient():
    assert updater.STATE_KEY in setting_overrides.TRANSIENT_KEYS


# --- safe_download_page, scheduling, notify retry ---------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "http://kestrelapp.co/download",
        "https://evil.example/download",
        "https://kestrelapp.co.evil.example/download",
        "https://kestrelapp.co/download?x=1",
        "https://kestrelapp.co/download?",
        "https://kestrelapp.co/download#frag",
        "https://user:pw@kestrelapp.co/download",
        "https://kestrelapp.co@evil.example/download",
        "https://kestrelapp.co:8443/download",
        "https://kestrelapp.co/down load",
        "",
        None,
    ],
)
def test_safe_download_page_rejects(url):
    assert updater.safe_download_page(url) is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://kestrelapp.co/download", "https://kestrelapp.co/download"),
        ("https://www.kestrelapp.co/download", "https://www.kestrelapp.co/download"),
        ("https://KestrelApp.CO/download", "https://kestrelapp.co/download"),
    ],
)
def test_safe_download_page_accepts(url, expected):
    assert updater.safe_download_page(url) == expected


async def test_check_now_stores_only_a_safe_download_page(sm):
    def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(_feed_text())
        data["download_page"] = "javascript:alert(1)"
        return httpx.Response(200, json=data)

    async with sm() as s:
        st = await updater.check_now(
            s, current="0.3.0", transport=_transport(handler), notify_sessionmaker=sm
        )
    assert st.download_page is None


def test_seconds_until_next_check():
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    floor = float(updater.FIRST_CHECK_DELAY_SECONDS)
    never = updater.UpdateState()
    assert updater.seconds_until_next_check(never, 24, now) == floor
    recent = updater.UpdateState(last_checked_at=(now - timedelta(hours=2)).isoformat())
    assert updater.seconds_until_next_check(recent, 24, now) == 22 * 3600.0
    old = updater.UpdateState(last_checked_at=(now - timedelta(hours=30)).isoformat())
    assert updater.seconds_until_next_check(old, 24, now) == floor
    just = updater.UpdateState(last_checked_at=(now - timedelta(hours=24)).isoformat())
    assert updater.seconds_until_next_check(just, 24, now) == floor
    junk = updater.UpdateState(last_checked_at="not a date")
    assert updater.seconds_until_next_check(junk, 24, now) == floor


async def test_failed_notify_leaves_notified_version_and_retries(sm, monkeypatch):
    from kestrel.services.notifications import NotificationService

    real = NotificationService.notify
    calls = {"n": 0}

    async def flaky(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(NotificationService, "notify", flaky)
    async with sm() as s:
        await updater.check_now(s, current="0.3.0", transport=_ok(), notify_sessionmaker=sm)
    async with sm() as s:
        assert (await updater.load_state(s)).notified_version is None
    assert [n for n in await _notifications(sm) if n.kind == "update"] == []
    async with sm() as s:
        await updater.check_now(s, current="0.3.0", transport=_status(304), notify_sessionmaker=sm)
    async with sm() as s:
        assert (await updater.load_state(s)).notified_version == "0.4.0"
    assert len([n for n in await _notifications(sm) if n.kind == "update"]) == 1


# --- the release file allowlist (D-UPD-8, D-UPD-9) -------------------------------------------

RELEASE = "https://github.com/distantsoil/Kestrelapp/releases/download"
FILES = {"windows": "Kestrel-Setup-9.9.9.exe", "macos": "Kestrel-9.9.9.dmg",
         "linux": "Kestrel-9.9.9-x86_64.AppImage"}
SHA = "ab" * 32


def _direct_feed(version: str = "9.9.9", **tamper) -> dict:
    """A feed whose downloads all point at the real release; `tamper` edits one platform entry."""
    downloads = {
        p: {"file": f, "url": f"{RELEASE}/v{version}/{f}", "sha256": SHA, "signed": False}
        for p, f in FILES.items()
    }
    for key, value in tamper.items():
        downloads["macos"][key] = value
    return {"schema": 1, "version": version, "published_at": "2026-03-01T12:00:00Z",
            "download_page": "https://kestrelapp.co/download", "downloads": downloads,
            "releases": []}


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("win32", "windows"), ("darwin", "macos"), ("linux", "linux"), ("linux2", "linux"),
     ("cygwin", None), ("freebsd14", None), ("", None)],
)
def test_feed_platform(platform, expected):
    assert updater.feed_platform(platform) == expected


def test_feed_platform_defaults_to_this_machine():
    assert updater.feed_platform() == updater.feed_platform(__import__("sys").platform)


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_release_download_accepts_the_real_release_and_rebuilds_the_url(platform):
    found = updater.release_download(_direct_feed(), platform=platform)
    key = updater.feed_platform(platform)
    assert found is not None and found.platform == key
    assert found.file == FILES[key] and found.version == "9.9.9" and found.sha256 == SHA
    assert found.url == f"{RELEASE}/v9.9.9/{FILES[key]}"
    assert "?" not in found.url and "#" not in found.url


def test_release_download_rebuilds_rather_than_passing_the_feed_string_through():
    # The feed's url must equal the rebuilt address byte for byte (D-UPD-9, round 2): a host in
    # capitals is refused, not normalized. An uppercase checksum is accepted and shown in
    # lowercase, and the returned url is the rebuilt constant form.
    assert updater.release_download(_direct_feed(
        url=f"https://GitHub.COM/distantsoil/Kestrelapp/releases/download/v9.9.9/"
            f"{FILES['macos']}"), platform="darwin") is None
    found = updater.release_download(_direct_feed(sha256=SHA.upper()), platform="darwin")
    assert found is not None
    assert found.url == f"{RELEASE}/v9.9.9/{FILES['macos']}" and found.sha256 == SHA
    assert found.url == updater.urlunsplit(("https", updater.RELEASE_HOST,
                                            f"{updater.RELEASE_PATH}v9.9.9/{found.file}", "", ""))


@pytest.mark.parametrize(
    ("reason", "tamper"),
    [
        ("http", {"url": f"http://github.com/distantsoil/Kestrelapp/releases/download/v9.9.9/"
                         f"{FILES['macos']}"}),
        ("other host", {"url": f"https://example.com/distantsoil/Kestrelapp/releases/download/"
                               f"v9.9.9/{FILES['macos']}"}),
        ("lookalike host", {"url": f"https://github.com.evil.example/distantsoil/Kestrelapp/"
                                   f"releases/download/v9.9.9/{FILES['macos']}"}),
        ("other repo", {"url": f"https://github.com/distantsoil/kestrel/releases/download/"
                               f"v9.9.9/{FILES['macos']}"}),
        ("other owner", {"url": f"https://github.com/someone/Kestrelapp/releases/download/"
                                f"v9.9.9/{FILES['macos']}"}),
        ("not an asset", {"url": "https://github.com/distantsoil/Kestrelapp/releases/tag/v9.9.9"}),
        ("version mismatch", {"url": f"{RELEASE}/v9.9.8/{FILES['macos']}"}),
        ("file mismatch", {"url": f"{RELEASE}/v9.9.9/Kestrel-9.9.8.dmg"}),
        ("query", {"url": f"{RELEASE}/v9.9.9/{FILES['macos']}?from=kestrel"}),
        ("fragment", {"url": f"{RELEASE}/v9.9.9/{FILES['macos']}#x"}),
        ("userinfo", {"url": f"https://github.com@evil.example/distantsoil/Kestrelapp/releases/"
                             f"download/v9.9.9/{FILES['macos']}"}),
        ("port", {"url": f"https://github.com:8443/distantsoil/Kestrelapp/releases/download/"
                         f"v9.9.9/{FILES['macos']}"}),
        ("javascript", {"url": "javascript:alert(1)"}),
        ("empty url", {"url": ""}),
        ("file with a path", {"file": "../Kestrel-9.9.9.dmg",
                              "url": f"{RELEASE}/v9.9.9/../Kestrel-9.9.9.dmg"}),
        ("file with a slash", {"file": "x/Kestrel-9.9.9.dmg",
                               "url": f"{RELEASE}/v9.9.9/x/Kestrel-9.9.9.dmg"}),
        ("hidden file", {"file": ".dmg", "url": f"{RELEASE}/v9.9.9/.dmg"}),
        ("file with a space", {"file": "Kestrel 9.9.9.dmg",
                               "url": f"{RELEASE}/v9.9.9/Kestrel 9.9.9.dmg"}),
        ("encoded file", {"file": "Kestrel%2F9.dmg", "url": f"{RELEASE}/v9.9.9/Kestrel%2F9.dmg"}),
        ("short sha", {"sha256": "ab" * 31}),
        ("non-hex sha", {"sha256": "zz" * 32}),
    ],
)
def test_release_download_rejects(reason, tamper):
    assert updater.release_download(_direct_feed(**tamper), platform="darwin") is None, reason
    # The other platforms' entries are untouched and still accepted: the check is per entry.
    assert updater.release_download(_direct_feed(**tamper), platform="win32") is not None


@pytest.mark.parametrize("version", ["v9.9.9", "9.9", "9.9.9/x", " 9.9.9", "9.9.9-rc.1/../"])
def test_release_download_rejects_a_feed_version_that_is_not_plain(version):
    feed = _direct_feed()
    feed["version"] = version
    assert updater.release_download(feed, platform="darwin") is None


def test_release_download_missing_entry_or_feed_is_none():
    feed = _direct_feed()
    del feed["downloads"]["macos"]
    assert updater.release_download(feed, platform="darwin") is None
    assert updater.release_download(_direct_feed(), platform="freebsd14") is None
    assert updater.release_download({"schema": 1, "version": "9.9.9"}, platform="darwin") is None
    assert updater.release_download(None, platform="darwin") is None
    # The shipped fixture points its one entry at kestrelapp.co, not the release: no direct link.
    assert updater.release_download(_feed(), platform="win32") is None


def test_release_download_never_fetches(monkeypatch):
    # Choosing the link is a pure read of the cached feed: no client is built, nothing awaits.
    def boom(*_a, **_k):
        raise AssertionError("release_download opened an HTTP client")

    monkeypatch.setattr(updater.httpx, "AsyncClient", boom)
    assert not inspect.iscoroutinefunction(updater.release_download)
    assert updater.release_download(_direct_feed(), platform="darwin") is not None


# --- bold in release notes (D-UPD-10) ---------------------------------------------------------


def test_render_notes_bold_pairs_only():
    out = updater.render_notes_html("- **The role page** shows more\n\n**All** of **this** is bold")
    assert "<li><strong>The role page</strong> shows more</li>" in out
    assert "<p><strong>All</strong> of <strong>this</strong> is bold</p>" in out
    assert "**" not in out


def test_render_notes_bold_is_applied_after_escaping():
    out = updater.render_notes_html("**<script>alert(1)</script>** and **<b>x</b>**")
    assert "<script>" not in out and "<b>" not in out
    assert "<strong>&lt;script&gt;alert(1)&lt;/script&gt;</strong>" in out
    assert "<strong>&lt;b&gt;x&lt;/b&gt;</strong>" in out


@pytest.mark.parametrize(
    "text",
    ["a lone ** marker", "**unpaired at the start", "unpaired at the end**", "** spaced **",
     "**", "****", "* single * stars *", "a**b", "**\n**"],
)
def test_render_notes_stray_and_unpaired_asterisks_stay_literal(text):
    out = updater.render_notes_html(text)
    assert "<strong>" not in out
    assert out.count("*") == text.count("*")


def test_render_notes_renders_nothing_but_bold_headings_and_bullets():
    out = updater.render_notes_html("[a link](https://evil.example) `code` *em* _u_ **b**")
    assert "<a" not in out and "<code>" not in out and "<em>" not in out
    assert "[a link](https://evil.example)" in out and "<strong>b</strong>" in out


async def test_update_notice_body_does_not_send_people_to_the_site_for_the_file(sm):
    # D-UPD-9: the bell action carries the link; the notice body no longer names kestrelapp.co.
    async with sm() as s:
        await updater.check_now(s, current="0.3.0", transport=_ok())
    rows = [n for n in await _notifications(sm) if n.kind == "update"]
    assert len(rows) == 1
    assert "kestrelapp.co" not in rows[0].body
    assert "download it when you're ready" in rows[0].body


# --- round 2: ASCII digits, whole-string matches, byte-for-byte URL ---------------------------


@pytest.mark.parametrize("text", ["١.8.0", "1.٨.0", "1.8.٠", "v١.0.0"])
def test_parse_version_rejects_non_ascii_digits(text):
    assert updater.parse_version(text) is None
    assert updater.is_newer(text, "0.1.0") is False


def test_release_download_rejects_a_non_ascii_version_digit():
    feed = _direct_feed(version="١.8.0")
    assert updater.release_download(feed, platform="darwin") is None


@pytest.mark.parametrize(
    ("reason", "tamper"),
    [
        ("sha with a trailing newline", {"sha256": SHA + "\n"}),
        ("sha with a trailing space", {"sha256": SHA + " "}),
        ("file with a trailing newline", {"file": FILES["macos"] + "\n",
                                          "url": f"{RELEASE}/v9.9.9/{FILES['macos']}\n"}),
        ("file with a trailing newline, clean url", {"file": FILES["macos"] + "\n"}),
    ],
)
def test_release_download_matches_whole_strings(reason, tamper):
    assert updater.release_download(_direct_feed(**tamper), platform="darwin") is None, reason


@pytest.mark.parametrize(
    ("reason", "given"),
    [
        ("leading space", f" {RELEASE}/v9.9.9/{FILES['macos']}"),
        ("trailing space", f"{RELEASE}/v9.9.9/{FILES['macos']} "),
        ("leading tab", f"\t{RELEASE}/v9.9.9/{FILES['macos']}"),
        ("embedded newline", f"https://github.com/\ndistantsoil/Kestrelapp/releases/download/"
                             f"v9.9.9/{FILES['macos']}"),
        ("leading control character", f"\x01{RELEASE}/v9.9.9/{FILES['macos']}"),
        ("uppercase scheme", f"HTTPS://github.com/distantsoil/Kestrelapp/releases/download/"
                             f"v9.9.9/{FILES['macos']}"),
        ("uppercase host", f"https://GitHub.com/distantsoil/Kestrelapp/releases/download/"
                           f"v9.9.9/{FILES['macos']}"),
    ],
)
def test_release_download_requires_the_url_byte_for_byte(reason, given):
    assert updater.release_download(_direct_feed(url=given), platform="darwin") is None, reason
    assert updater.release_download(_direct_feed(), platform="darwin") is not None


def test_a_version_string_longer_than_any_tag_is_not_read():
    """CodeQL py/polynomial-redos: the version pattern only ever sees a short string."""
    from kestrel.services.updater import _VERSION_MAX_LEN, parse_version

    assert parse_version("v1.2.3") == (1, 2, 3, (1,))
    assert parse_version("1.2.3-" + "a" * (_VERSION_MAX_LEN - 6)) is not None
    assert parse_version("1.2.3-" + "a" * _VERSION_MAX_LEN) is None
    assert parse_version("1." * 5000) is None
