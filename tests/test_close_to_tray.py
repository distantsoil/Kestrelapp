"""D-UI-8: closing the desktop window hides to the tray by default, quits when the operator turns
that off on Settings > Appearance, and always quits when there is no tray to come back from."""

from __future__ import annotations

import pytest

from tests.test_hosted_mode import _fresh_app


class _Icon:
    def __init__(self) -> None:
        self.stopped = False
        self.notes: list[str] = []

    def stop(self) -> None:
        self.stopped = True

    def notify(self, body: str, title: str) -> None:
        self.notes.append(body)


class _Window:
    def __init__(self) -> None:
        self.hidden = False

    def hide(self) -> None:
        self.hidden = True


class _Server:
    should_exit = False


@pytest.fixture
def shell(monkeypatch):
    from kestrel.desktop import shell

    monkeypatch.setattr(shell, "window", _Window())
    monkeypatch.setattr(shell, "icon", _Icon())
    monkeypatch.setattr(shell, "server", _Server())
    monkeypatch.setattr(shell, "_quitting", False)
    monkeypatch.setattr(shell, "_toast_shown", False)
    # The quit here is the tray's own-loop one (stop()); the macOS shared-loop path hides the item
    # instead and is covered in test_platform.py, so this runs the same on a macOS host.
    monkeypatch.setattr(shell, "IS_MACOS", False)
    return shell


def test_default_close_hides_to_the_tray(shell):
    client = _fresh_app()
    assert shell.on_closing() is False
    assert shell.window.hidden and not shell._quitting and not shell.server.should_exit
    assert "Settings > Appearance" in shell.icon.notes[0]
    body = client.get("/settings").text
    assert "minimizes Kestrel to the system tray" in body
    assert 'name="close_to_tray" value="on" checked' in body


def test_turned_off_close_quits_gracefully(shell):
    client = _fresh_app()
    r = client.post("/settings/desktop", data={"close_to_tray": ["off"]})
    assert "quits Kestrel" in r.text
    assert shell.on_closing() is True  # the OS close goes ahead
    assert shell._quitting and shell.server.should_exit and shell.icon.stopped
    assert not shell.window.hidden
    assert 'value="on" checked' not in client.get("/settings").text.split("desktop-window")[1]


def test_turned_back_on_hides_again(shell):
    client = _fresh_app()
    client.post("/settings/desktop", data={"close_to_tray": ["off"]})
    client.post("/settings/desktop", data={"close_to_tray": ["off", "on"]})
    assert shell.on_closing() is False and shell.window.hidden


def test_no_tray_means_close_quits_even_when_on(shell, monkeypatch):
    _fresh_app()
    monkeypatch.setattr(shell, "icon", None)
    assert shell.on_closing() is True and shell._quitting


def test_quitting_close_is_always_allowed(shell):
    _fresh_app()
    shell._quitting = True
    assert shell.on_closing() is True and not shell.window.hidden


def test_without_a_row_the_settings_default_applies(shell, monkeypatch):
    from kestrel.web.deps import close_to_tray

    _fresh_app()
    assert close_to_tray() is True
    monkeypatch.setenv("KESTREL_UI__CLOSE_TO_TRAY", "false")
    assert close_to_tray() is False
    assert shell.on_closing() is True and shell._quitting


def test_the_tray_notice_shows_once_per_launch_and_says_how_to_change_it(shell):
    _fresh_app()
    assert shell.on_closing() is False
    shell.window.hidden = False
    assert shell.on_closing() is False
    assert len(shell.icon.notes) == 1  # the second close of the session is quiet
    note = shell.icon.notes[0]
    assert "keeps working in the background" in note
    assert "Settings > Appearance > Desktop window" in note and "—" not in note


def test_a_failed_tray_notice_is_logged_at_warning(shell, monkeypatch):
    import structlog

    def _boom(*a, **k):
        raise OSError("balloon refused")

    monkeypatch.setattr(shell.icon, "notify", _boom)
    with structlog.testing.capture_logs() as logs:
        shell._notify("t", "b")
    assert any(e["event"] == "tray_notify_failed" and e["log_level"] == "warning" for e in logs)


def test_quit_is_bounded_when_the_server_will_not_stop(shell, monkeypatch):
    """D-UI-17: Quit during a long hosted call left 0.2.0 running with no window and no tray."""
    import threading

    stop = threading.Event()
    stuck = threading.Thread(target=stop.wait, daemon=True)
    stuck.start()
    exits: list[int] = []
    monkeypatch.setattr(shell, "server_thread", stuck)
    monkeypatch.setattr(shell, "QUIT_JOIN_S", 0.05)
    monkeypatch.setattr(shell.os, "_exit", lambda code: exits.append(code))
    shell._finish_quit()
    assert exits == [0]
    stop.set()


def test_quit_exits_normally_when_the_server_stops(shell, monkeypatch):
    import threading

    done = threading.Thread(target=lambda: None)
    done.start()
    exits: list[int] = []
    monkeypatch.setattr(shell, "server_thread", done)
    monkeypatch.setattr(shell.os, "_exit", lambda code: exits.append(code))
    assert shell._finish_quit() == 0 and exits == []


def test_open_requests_are_capped_at_quit():
    from kestrel.web import serve

    server = serve._make_server(lambda *a: None, log_level="warning")
    assert server.config.timeout_graceful_shutdown == serve.GRACEFUL_REQUEST_S <= 10
