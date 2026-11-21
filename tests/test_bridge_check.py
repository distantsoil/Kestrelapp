"""The desktop window channels under the Content-Security-Policy (D-SEC-61): the self-check,
and the app page's /api/window/* routes. Fakes only; the real backends are checked on the
owner's machines (working.log, BLOCKED: requires local)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kestrel.desktop import bridge_check
from kestrel.desktop.channel import APP_EVENTS, Channel


class _Channel:
    def __init__(self, fetched: float | None) -> None:
        self.last_fetch = fetched


@pytest.fixture(autouse=True)
def _fresh():
    bridge_check.reset()
    yield
    bridge_check.reset()


def test_both_channels_working_is_ok_and_eval_is_never_used():
    t = bridge_check.start(_Channel(1.0), wait_s=5)
    bridge_check.window_reached()
    t.join(5)
    got = bridge_check.summary()
    assert got["splash"] == "ok" and got["window"] == "ok" and got["checked"] is True
    assert got["evaluate_js"] == "not used"


def test_a_loading_page_that_never_fetched_is_recorded():
    t = bridge_check.start(_Channel(None), wait_s=5)
    bridge_check.window_reached()
    t.join(5)
    assert bridge_check.summary()["splash"].startswith("failed: the loading page never asked")


def test_no_app_page_call_within_the_wait_records_the_window_channel_as_failed():
    t = bridge_check.start(_Channel(1.0), wait_s=0.2)
    t.join(5)
    assert bridge_check.summary()["window"].startswith(
        "failed: the app page did not reach the window channel")


def test_it_runs_once_per_process():
    first = bridge_check.start(_Channel(1.0), wait_s=0.1)
    assert bridge_check.start(_Channel(1.0), wait_s=0.1) is None
    first.join(5)


def test_the_window_api_reports_the_page_call():
    from kestrel.desktop.frame import WindowApi

    api = WindowApi(lambda: None, close_intent=lambda: "tray", close=lambda: None)
    assert api.bridge_check() is True
    assert bridge_check.summary()["window"] == "ok"


def test_the_diagnostics_summary_carries_the_result():
    from kestrel.config import load_settings
    from kestrel.services import diagnostics

    bridge_check.window_reached()
    got = diagnostics.summary(load_settings(), diagnostics.make_scrubber(None))
    assert got["desktop_bridge"]["window"] == "ok"


# ------------------------------------------------------------------- /api/window/* (app pages)
class _Api:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def window_info(self) -> dict:
        self.calls.append(("window_info",))
        return {"os": "linux", "maximized": True}

    def window_start_drag(self, x=None, y=None) -> None:
        self.calls.append(("window_start_drag", x, y))

    def splash_quit(self) -> None:  # a loading-page button: never callable from an app page
        self.calls.append(("splash_quit",))


@pytest.fixture
def window_app(monkeypatch):
    from kestrel.db import migrate
    from kestrel.web import desktop_bridge
    from kestrel.web.app import build_app

    migrate.run_migrations()
    api, channel = _Api(), Channel(APP_EVENTS)
    desktop_bridge.set_window_frame(True)
    desktop_bridge.set_window_api(api, channel)
    try:
        with TestClient(build_app(worker=False)) as client:
            yield client, api, channel
    finally:
        desktop_bridge.set_window_api(None, None)
        desktop_bridge.set_window_frame(False)


def test_an_app_page_calls_the_window_and_hears_its_events(window_app):
    client, api, channel = window_app
    r = client.post("/api/window/call/window_info", json={"args": []})
    assert r.status_code == 200 and r.json() == {"value": {"os": "linux", "maximized": True}}
    client.post("/api/window/call/window_start_drag", json={"args": [12, 34]})
    assert api.calls == [("window_info",), ("window_start_drag", 12, 34)]
    assert client.get("/api/window/events?after=-1").json() == {"seq": 0, "events": []}
    channel.post("state", True)
    channel.post("closing")
    assert client.get("/api/window/events?after=0").json() == {
        "seq": 2, "events": [["state", [True]], ["closing", []]]}


def test_an_app_page_cannot_reach_anything_else(window_app):
    client, api, _ = window_app
    for name in ("splash_quit", "_set_maximized", "nope"):
        assert client.post(f"/api/window/call/{name}", json={}).status_code == 404, name
    # Another site's page is refused by the origin guard before the route runs.
    r = client.post("/api/window/call/window_info", json={},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert api.calls == []


def test_without_the_desktop_window_the_routes_are_not_there():
    from kestrel.db import migrate
    from kestrel.web.app import build_app

    migrate.run_migrations()
    with TestClient(build_app(worker=False)) as client:
        assert client.post("/api/window/call/window_info", json={}).status_code == 404
        assert client.get("/api/window/events?after=0").status_code == 404


def test_a_splash_update_the_page_never_takes_is_a_warning_with_its_cause(monkeypatch):
    """#212 round 4, item 3: the old path logged a refused update at debug and the window sat on
    "Opening your data". An update the loading page does not fetch is a warning naming why."""
    from structlog.testing import capture_logs

    import kestrel.desktop.shell as shell
    from kestrel.desktop.channel import SPLASH_EVENTS, Channel

    monkeypatch.setattr(shell._Splash, "DELIVERY_WAIT_S", 0.1)
    never = Channel(SPLASH_EVENTS)
    with capture_logs() as logs:
        shell._Splash(never).fail("Stopped.", "Step: x", "migrations_failed", "STA-004")
        import time

        time.sleep(0.4)
    warned = [e for e in logs if e["event"] == "splash_update_failed"]
    assert warned and warned[0]["log_level"] == "warning" and warned[0]["fn"] == "fail"
    assert warned[0]["cause"] == "the loading page never asked for its updates"

    fetched = Channel(SPLASH_EVENTS)
    with capture_logs() as logs:
        shell._Splash(fetched).say("Opening your data", 30)
        fetched.after(0, 0)  # the page asks, as it does every time an update lands
        time.sleep(0.4)
    assert not [e for e in logs if e["event"] == "splash_update_failed"]


def test_a_window_with_no_loading_page_server_warns_instead_of_staying_silent(monkeypatch):
    from structlog.testing import capture_logs

    import kestrel.desktop.shell as shell

    monkeypatch.setattr(shell, "window", object())
    with capture_logs() as logs:
        shell._Splash(None).say("Opening your data", 30)
    assert [e["cause"] for e in logs if e["event"] == "splash_update_failed"] == [
        "the loading page has no server to fetch its updates from"]
