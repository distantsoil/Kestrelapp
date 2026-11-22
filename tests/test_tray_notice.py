"""The close-to-tray notice is also kept in the notifications list, and the tray's Recent
notifications entry opens that list (issue #200, D-NOTIFY-639). The OS banner can fail to appear
(macOS shows nothing for a notification that is not Kestrel's own), so the list is where the notice
is always found. Invented text only, nothing leaves this computer."""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import select

from kestrel.db.models import Notification
from kestrel.services import tray_notice
from kestrel.services.delivery import deliver_pending
from kestrel.services.setting_overrides import load_settings_with_db
from tests.test_hosted_mode import _fresh_app

ROOT = Path(__file__).resolve().parent.parent


def test_the_wording_has_one_source_and_the_shell_uses_it():
    from kestrel.desktop import shell

    assert shell.CLOSE_TO_TRAY_TITLE == tray_notice.TITLE
    assert shell.CLOSE_TO_TRAY_BODY == tray_notice.BODY
    assert shell.CLOSE_TO_TRAY_BODY_MAC == tray_notice.BODY_MAC
    assert tray_notice.body("darwin") == tray_notice.BODY_MAC
    assert tray_notice.body("linux") == tray_notice.BODY and tray_notice.body("win32")
    assert "—" not in tray_notice.BODY + tray_notice.BODY_MAC


def test_the_route_answers_with_a_row_id_and_takes_nothing_from_the_caller():
    client = _fresh_app()
    r = client.post("/api/tray/closed", json={"title": "ignored", "body": "ignored"})
    assert r.status_code == 200 and isinstance(r.json()["id"], int)
    html = client.get("/notifications/list").text
    assert tray_notice.TITLE in html and 'data-kind="window_closed"' in html
    assert "ignored" not in html  # the caller's text is never stored


async def test_the_recorded_row_is_fixed_text_unread_and_already_handled(sm):
    note_id = await tray_notice.record(sm)
    async with sm() as s:
        n = (await s.execute(select(Notification).where(Notification.id == note_id))).scalar_one()
    assert n.kind == tray_notice.KIND and n.title == tray_notice.TITLE
    assert n.body == tray_notice.body(sys.platform)
    assert n.delivered_channels == tray_notice.HANDLED  # handled: never a second banner
    assert n.read_at is None  # unread, so the bell shows it


async def test_the_delivery_cycle_never_sends_the_recorded_notice_again(sm, monkeypatch):
    """The row is already handled: a delivery cycle must not pop a second banner for it."""
    from kestrel.services import delivery

    await tray_notice.record(sm)
    seen: list = []
    monkeypatch.setattr(delivery, "_toast", lambda t, b: seen.append((t, b)) or True)
    async with sm() as s:
        settings = await load_settings_with_db(s)
        settings.notify.enabled = True
        settings.notify.toast = True
        settings.notify.sound = False
        settings.notify.ntfy_topic = None
        settings.notify.webhook_url = None
        settings.notify.quiet_hours = {"enabled": False, "start": "21:00", "end": "07:00"}
        result = await deliver_pending(s, settings)
    assert result.sent == 0 and seen == []


def test_the_first_close_of_a_launch_also_records_the_notice_once(monkeypatch):
    from kestrel.desktop import shell

    posted: list[str] = []
    monkeypatch.setattr(shell, "_post", lambda path: posted.append(path))
    monkeypatch.setattr(shell, "IS_MACOS", False)
    monkeypatch.setattr(shell, "_toast_shown", False)
    monkeypatch.setattr(shell, "_quitting", False)
    monkeypatch.setattr(shell, "_started", False)
    monkeypatch.setattr(shell, "_frame_api", None)
    monkeypatch.setattr(shell, "_hidden", False)
    monkeypatch.setattr(shell, "server", type("S", (), {"should_exit": False})())
    monkeypatch.setattr(shell, "icon", type("I", (), {"notify": lambda *a: None})())
    monkeypatch.setattr(shell, "window", type("W", (), {"hide": lambda self: None})())
    _fresh_app()
    import time

    assert shell.on_closing() is False
    assert shell.on_closing() is False
    deadline = time.monotonic() + 5
    while not posted and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    assert posted == ["/api/tray/closed"]  # once per launch, like the banner


def test_the_tray_entry_opens_the_notifications_panel_through_the_bell(monkeypatch):
    from kestrel.desktop import shell

    shown, js = [], []
    monkeypatch.setattr(shell, "show_window", lambda: shown.append(1))
    win = type("W", (), {"evaluate_js": lambda self, c: js.append(c)})()
    monkeypatch.setattr(shell, "window", win)
    shell._show_notifications()
    assert shown == [1] and len(js) == 1
    # The script clicks the bell by its test id; the bell with that id must exist in the page.
    assert "[data-testid=\"bell\"]" in js[0]
    base = (ROOT / "kestrel/web/templates/base.html").read_text(encoding="utf-8")
    assert 'data-testid="bell"' in base and "KUI" not in js[0]


def test_the_tray_menu_entry_is_wired_to_the_notifications_panel(monkeypatch):
    """Structure, not text: build the real menu with a recording pystray and call the entry."""
    from kestrel.desktop import shell

    items: dict[str, object] = {}

    class _Item:
        def __init__(self, text, action=None, **kw):
            items[text if isinstance(text, str) else "dynamic"] = action

    class _Menu:
        SEPARATOR = object()

        def __init__(self, *a):
            pass

    class _Icon:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, name):  # run(), run_detached(), ...: nothing under test
            return lambda *a, **k: None

    fake = type("P", (), {"MenuItem": _Item, "Menu": _Menu, "Icon": _Icon})
    monkeypatch.setattr(shell, "IS_MACOS", False)
    # The real tray starts a tooltip thread that polls /api/status every 4 s and never stops in a
    # test; under respx its polls land in later tests' mocks (as in tests/test_platform.py).
    monkeypatch.setattr(shell, "_tooltip_poller", lambda: None)
    shell._build_tray(fake, with_window=False)
    called: list[str] = []
    monkeypatch.setattr(shell, "_show_notifications", lambda: called.append("panel"))
    monkeypatch.setattr(shell, "IS_MACOS", False)
    items["Recent notifications"](None, None)
    assert called == ["panel"]
