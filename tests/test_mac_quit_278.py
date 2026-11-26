"""macOS app-level Quit and Dock reopen (#278).

Cmd-Q, the app menu's Quit and the Dock's Quit reach NSApplication's delegate, not the
window's closing event: pywebview's own delegate answers them by closing every window, and
with close-to-tray on Kestrel's closing handler hides the window and cancels the quit, so the
app never left. Kestrel's delegate runs the real graceful quit instead (the tray Quit's path)
and answers a Dock click by showing the window again.

The delegate's class is built through _mac_app_delegate_class, which takes its superclass and
the AppKit constants as arguments: a test on Windows or Linux builds it from fakes and never
imports AppKit.
"""

from __future__ import annotations

import importlib
import sys
import threading
import time
import types
from typing import Any

import pytest

_CANCEL = object()  # the AppKit.NSTerminateCancel stand-in: compared by identity
_LATER = object()  # the AppKit.NSTerminateLater stand-in
_YES = object()


class _FakeNSApp:
    """The NSApplication surface the delegate and the bounce use, with AppKit's documented
    bounce rule implemented: a request on an active app is ignored (Apple: "Sending
    requestUserAttention(_:) to an app that is already active has no effect")."""

    def __init__(self, active: bool = False, app_hidden: bool = False):
        self.active = active
        self.app_hidden = app_hidden
        self.hide_calls: list = []
        self.unhide_calls: list = []
        self.requests: list = []
        self.replies: list = []

    def isActive(self) -> bool:
        return self.active

    def isHidden(self) -> bool:
        return self.app_hidden

    def hide_(self, sender) -> None:
        self.active = False
        self.app_hidden = True
        self.hide_calls.append(sender)

    def unhide_(self, sender) -> None:
        self.app_hidden = False
        self.unhide_calls.append(sender)

    def requestUserAttention_(self, kind) -> None:
        if self.active:
            return  # the documented rule above: an active app ignores the request
        self.requests.append(kind)

    def replyToApplicationShouldTerminate_(self, reply) -> None:
        self.replies.append(reply)


class _Window:
    def __init__(self) -> None:
        self.calls: list = []
        self.hidden = False

    def hide(self) -> None:
        self.hidden = True
        self.calls.append("hide")

    def show(self) -> None:
        self.calls.append("show")

    def restore(self) -> None:
        self.calls.append("restore")

    def destroy(self) -> None:
        self.calls.append(("destroy", threading.current_thread().name))


class _Server:
    def __init__(self) -> None:
        self.should_exit = False


class _ServerThread:
    """A server thread whose join explodes: the overlay must survive it."""

    def join(self, timeout: float) -> None:
        raise RuntimeError("server join refused")


class _PreemptedQuitting:
    """_quitting as a fake: False for the first two reads (the delegate's guard and the
    quit's own check), True from the third on -- a tray Quit began its drain between the
    terminate thread's re-check and its call."""

    def __init__(self) -> None:
        self.reads = 0

    def __bool__(self) -> bool:
        self.reads += 1
        return self.reads > 2


@pytest.fixture
def shell(monkeypatch):
    from kestrel.desktop import shell

    # Every global the delegate paths read or write (D-TEST-31): no test relies on another's.
    monkeypatch.setattr(shell, "window", _Window())
    monkeypatch.setattr(shell, "icon", None)
    monkeypatch.setattr(shell, "server", None)
    monkeypatch.setattr(shell, "server_thread", None)
    monkeypatch.setattr(shell, "_quitting", False)
    monkeypatch.setattr(shell, "_started", False)
    monkeypatch.setattr(shell, "_hidden", False)
    monkeypatch.setattr(shell, "_toast_shown", True)
    monkeypatch.setattr(shell, "_frame_api", None)
    monkeypatch.setattr(shell, "_app_channel", None)
    monkeypatch.setattr(shell, "_mac_delegate", None)
    monkeypatch.setattr(shell, "_terminate_started", False, raising=False)  # absent pre-fix
    monkeypatch.setattr(shell, "_exit_clean_logged", False, raising=False)  # absent pre-fix
    return shell


def _delegate(shell):
    cls = shell._mac_app_delegate_class(object, _CANCEL, _LATER, _YES)
    return cls()


def _refuse_appkit_imports(monkeypatch) -> None:
    """Any explicit AppKit-family import during a call fails loudly, so an off-macOS guard
    gap cannot slip through as a quiet skip."""
    def _refuse(name, *args: Any, **kwargs: Any):
        raise AssertionError(f"AppKit ({name}) must not be imported off macOS")

    monkeypatch.setattr(importlib, "import_module", _refuse)


def structlog_testing_logs():
    """structlog.testing.capture_logs, imported here so every test shares one spelling."""
    import structlog

    return structlog.testing.capture_logs()


def _joined_mac_quit(timeout: float = 30.0) -> None:
    """Wait for the hook's own thread to finish. Bounded by the overlay's own constants
    (CLOSING_MIN_S plus room), never a fixed small elapsed ceiling."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        workers = [t for t in threading.enumerate() if t.name == "mac-quit"]
        if not workers:
            return  # it already ran to completion
        workers[0].join(timeout=0.2)


def _wait_until(what, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if what():
            return
        time.sleep(0.01)
    raise AssertionError("the quit never reached the expected state")


# --------------------------------------------------------------------- 1. the terminate hook


def test_terminate_hook_quits_gracefully_and_never_hides(shell):
    d = _delegate(shell)
    server = _Server()
    shell.server = server
    shell._started = True
    app = _FakeNSApp()

    result = d.applicationShouldTerminate_(app)

    _joined_mac_quit()
    assert any(c[0] == "destroy" for c in shell.window.calls)
    assert result is _LATER  # a logout waits for the drain instead of being cancelled
    assert shell._quitting and server.should_exit
    assert not shell.window.hidden and "hide" not in shell.window.calls
    destroy = shell.window.calls[-1]
    assert destroy[0] == "destroy"
    assert destroy[1] == "mac-quit"  # the join ran off the main thread, where AppKit waits


def test_the_terminate_reply_is_queued_once_after_the_drain(shell, monkeypatch):
    from kestrel.desktop import shell as shell_module

    server = _Server()
    shell.server = server
    shell._started = True
    app = _FakeNSApp()
    queued: list = []
    _queued_apphelper(monkeypatch, queued)
    monkeypatch.setattr(shell_module, "IS_MACOS", True)
    d = _delegate(shell)

    result = d.applicationShouldTerminate_(app)

    _joined_mac_quit()
    assert result is _LATER
    assert len(queued) == 1 and queued[0][2] is True  # queued once, at/after the destroy
    queued[0][0](*queued[0][1])
    assert app.replies == [True]  # a logout may proceed
    assert shell.window.calls[-1][0] == "destroy"  # our own exit path is untouched


def test_a_second_terminate_request_replies_once(shell, monkeypatch):
    from kestrel.desktop import shell as shell_module

    server = _Server()
    shell.server = server
    shell._started = True
    app = _FakeNSApp()
    queued: list = []
    _queued_apphelper(monkeypatch, queued)
    monkeypatch.setattr(shell_module, "IS_MACOS", True)
    d = _delegate(shell)

    first = d.applicationShouldTerminate_(app)
    second = d.applicationShouldTerminate_(app)  # a double Cmd-Q

    _joined_mac_quit()
    assert first is _LATER and second is _LATER
    assert len(queued) == 1  # the termination is owned once; a second request replies nothing
    assert queued[0][2] is True  # the one reply is queued at/after the window destroy
    queued[0][0](*queued[0][1])
    assert app.replies == [True]  # exactly one reply


def test_terminate_during_a_tray_quit_drain_replies_nothing(shell, monkeypatch):
    from kestrel.desktop import shell as shell_module

    server = _Server()
    shell.server = server
    shell._quitting = True  # a tray Quit's drain is running and owns the exit
    app = _FakeNSApp()
    queued: list = []
    _queued_apphelper(monkeypatch, queued)
    monkeypatch.setattr(shell_module, "IS_MACOS", True)
    d = _delegate(shell)

    result = d.applicationShouldTerminate_(app)

    _joined_mac_quit()
    assert result is _LATER
    assert queued == []  # a reply here could make AppKit exit before the drain freed leases
    assert server.should_exit is False  # the running drain's begin is not duplicated


def test_a_reply_is_never_queued_before_the_drain_it_belongs_to(shell, monkeypatch):
    # The r3 residual window: a tray Quit begins between the terminate thread's re-check
    # and its call (the fake reads False twice, then True). The reply must not be queued
    # while no drain has run; it belongs to the drain its own check-and-set began.
    from kestrel.desktop import shell as shell_module

    server = _Server()
    shell.server = server
    shell._started = True
    app = _FakeNSApp()
    queued: list = []
    _queued_apphelper(monkeypatch, queued)
    monkeypatch.setattr(shell_module, "IS_MACOS", True)
    monkeypatch.setattr(shell_module, "_quitting", _PreemptedQuitting())
    d = _delegate(shell)

    result = d.applicationShouldTerminate_(app)

    _joined_mac_quit()
    assert result is _LATER
    assert len(queued) == 1 and queued[0][2] is True  # queued at/after a destroy, never before
    queued[0][0](*queued[0][1])
    assert app.replies == [True]


def test_a_quit_begun_before_the_owned_check_replies_nothing(shell, monkeypatch):
    # A tray Quit begins between the delegate's guard and the quit's owned check-and-set
    # (the fake reads False once, then True): the terminate thread owns nothing, replies
    # nothing, and the other drain's owner ends the process through _finish_quit.
    from kestrel.desktop import shell as shell_module

    server = _Server()
    shell.server = server
    shell._started = True
    app = _FakeNSApp()
    queued: list = []
    _queued_apphelper(monkeypatch, queued)
    monkeypatch.setattr(shell_module, "IS_MACOS", True)

    class _PreemptedEarly:
        def __init__(self) -> None:
            self.reads = 0

        def __bool__(self) -> bool:
            self.reads += 1
            return self.reads > 1

    monkeypatch.setattr(shell_module, "_quitting", _PreemptedEarly())
    d = _delegate(shell)

    result = d.applicationShouldTerminate_(app)

    _joined_mac_quit()
    assert result is _LATER
    assert queued == []  # nothing owned, nothing replied
    assert server.should_exit is False  # the other drain's begin is not duplicated


def test_terminate_hook_while_a_quit_is_already_running_does_not_quit_again(shell):
    d = _delegate(shell)
    server = _Server()
    shell.server = server
    shell._quitting = True
    app = _FakeNSApp()

    result = d.applicationShouldTerminate_(app)

    _joined_mac_quit()
    assert result is _LATER
    assert server.should_exit is False  # the running quit is left alone
    assert app.replies == []  # off macOS (no queue fake), the reply call never runs


# ----------------------------------------------------------------------- 2. the reopen hook


def test_reopen_hook_shows_and_restores_a_window_closed_to_the_tray(shell):
    d = _delegate(shell)
    shell._hidden = True

    result = d.applicationShouldHandleReopen_hasVisibleWindows_(None, True)

    assert result is _YES
    assert shell.window.calls == ["show", "restore"]
    assert shell._hidden is False


def test_reopen_hook_on_a_visible_window_still_answers_yes(shell):
    d = _delegate(shell)
    shell._hidden = False

    result = d.applicationShouldHandleReopen_hasVisibleWindows_(None, True)

    assert result is _YES
    assert shell.window.calls == ["show", "restore"]


# ------------------------------------------------------------------------- 3. the installer


def _fake_appkit(monkeypatch):
    """sys.modules fakes for the three names the installer imports on macOS."""
    appkit = types.ModuleType("AppKit")
    appkit.NSTerminateCancel = _CANCEL
    appkit.NSTerminateLater = _LATER
    foundation = types.ModuleType("Foundation")
    foundation.YES = _YES

    class _FakeAppDelegate:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

    class _FakeBrowserView:
        AppDelegate = _FakeAppDelegate
        _shared_app_delegate = None
        app = None

    cocoa = types.ModuleType("webview.platforms.cocoa")
    cocoa.BrowserView = _FakeBrowserView
    for name, mod in [
        ("webview", types.ModuleType("webview")),
        ("webview.platforms", types.ModuleType("webview.platforms")),
        ("webview.platforms.cocoa", cocoa),
        ("AppKit", appkit),
        ("Foundation", foundation),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)
    return cocoa


class _FakeApp:
    def __init__(self) -> None:
        self.delegates: list = []

    def setDelegate_(self, delegate) -> None:
        self.delegates.append(delegate)


def test_the_delegate_is_installed_where_pywebview_keeps_its_shared_delegate(shell, monkeypatch):
    cocoa = _fake_appkit(monkeypatch)
    fake_app = _FakeApp()
    cocoa.BrowserView.app = fake_app

    shell._install_mac_app_delegate()

    installed = shell._mac_delegate
    assert installed is not None
    assert isinstance(installed, cocoa.BrowserView.AppDelegate)
    assert cocoa.BrowserView._shared_app_delegate is installed
    assert fake_app.delegates == [installed]


def test_the_installer_replaces_pywebviews_own_delegate(shell, monkeypatch):
    cocoa = _fake_appkit(monkeypatch)
    fake_app = _FakeApp()
    cocoa.BrowserView.app = fake_app
    # pywebview built its own delegate before ours ran (the window was created first).
    pywebviews_own = cocoa.BrowserView.AppDelegate.alloc().init()
    cocoa.BrowserView._shared_app_delegate = pywebviews_own

    shell._install_mac_app_delegate()

    installed = shell._mac_delegate
    assert cocoa.BrowserView._shared_app_delegate is installed
    assert fake_app.delegates == [installed]  # ours, not pywebview's, is the app's delegate


def test_the_installer_reruns_after_pywebview_clears_the_slot(shell, monkeypatch):
    cocoa = _fake_appkit(monkeypatch)
    fake_app = _FakeApp()
    cocoa.BrowserView.app = fake_app

    shell._install_mac_app_delegate()
    first = shell._mac_delegate
    # The last window closed (pywebview clears the slot and the app's delegate, cocoa.py
    # 115-118): a later window must not bring pywebview's own delegate back.
    cocoa.BrowserView._shared_app_delegate = None
    fake_app.delegates.clear()

    shell._install_mac_app_delegate()

    assert shell._mac_delegate is first
    assert cocoa.BrowserView._shared_app_delegate is first
    assert fake_app.delegates == [first]


# -------------------------------------------------------------------- 4. the overlay hardens


def test_a_failed_overlay_wait_still_closes_the_window(shell):
    shell.server_thread = _ServerThread()
    shell._started = True

    with structlog_testing_logs() as logs:
        shell._close_with_overlay()  # must not raise

    assert shell.window.calls[-1][0] == "destroy"
    assert any(
        e["event"] == "closing_overlay_failed" and e["log_level"] == "warning" for e in logs
    )


def test_a_later_quit_after_a_failed_wait_is_not_a_no_op(shell):
    server = _Server()
    shell.server = server
    shell.server_thread = _ServerThread()
    shell._started = True
    shell._close_with_overlay()
    assert shell.window.calls[-1][0] == "destroy"

    shell.window.calls.clear()
    shell.do_quit()
    _wait_until(lambda: any(c[0] == "destroy" for c in shell.window.calls))

    assert shell.server.should_exit is True


# ------------------------------------------------------------------ 5. the denied-notice bounce


def _fake_bounce_appkit(monkeypatch, app=None):
    """Fake AppKit plus the one macOS fact the bounce needs (IS_MACOS). The fake app
    implements AppKit's documented bounce rule (an active app ignores the request), so the
    tests pin exactly the informational kind, requested once."""
    from kestrel.desktop import shell as shell_module

    app = app or _FakeNSApp()
    appkit = types.SimpleNamespace(
        NSApplication=types.SimpleNamespace(sharedApplication=lambda: app),
        NSInformationalRequest=("informational",),
    )
    monkeypatch.setattr(shell_module, "IS_MACOS", True)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    return appkit.NSInformationalRequest, app


def _queued_apphelper(monkeypatch, queued: list) -> None:
    """Fake PyObjCTools.AppHelper: a ModuleType whose callAfter records instead of queueing.
    Each record notes whether the window was already destroyed, so a test can prove the
    drain ran before the queued call was made."""
    from kestrel.desktop import shell as shell_module

    class _Helper(types.ModuleType):
        def callAfter(self, fn, *args: Any) -> None:
            destroyed = any(c[0] == "destroy" for c in shell_module.window.calls)
            queued.append((fn, args, destroyed))

    monkeypatch.setitem(sys.modules, "PyObjCTools", types.ModuleType("PyObjCTools"))
    monkeypatch.setitem(sys.modules, "PyObjCTools.AppHelper", _Helper("PyObjCTools.AppHelper"))


def test_a_denied_notice_bounces_the_dock_once_with_the_informational_kind(shell, monkeypatch):
    from kestrel.services import mac_notify

    kind, app = _fake_bounce_appkit(monkeypatch)
    monkeypatch.setattr(mac_notify, "post", lambda title, body: mac_notify.Path.DENIED)

    shell._post_mac_notice("Kestrel is still running", "body")

    assert app.requests == [kind]  # exactly one bounce, the informational kind


def test_undecided_and_no_path_bounce_too(shell, monkeypatch):
    from kestrel.services import mac_notify

    kind, app = _fake_bounce_appkit(monkeypatch)
    for result in (mac_notify.Path.UNDECIDED, mac_notify.Path.NONE):
        monkeypatch.setattr(mac_notify, "post", lambda title, body, result=result: result)
        shell._post_mac_notice("Kestrel is still running", "body")

    assert app.requests == [kind, kind]  # one bounce per silent notice


def test_the_osascript_path_bounces_too(shell, monkeypatch):
    # mac_notify's docstring: OSASCRIPT is "handed to macOS through the old osascript path
    # (macOS may show nothing there)". A notice that may not show is silent, so it bounces.
    from kestrel.services import mac_notify

    kind, app = _fake_bounce_appkit(monkeypatch)
    monkeypatch.setattr(mac_notify, "post", lambda title, body: mac_notify.Path.OSASCRIPT)

    shell._post_mac_notice("Kestrel is still running", "body")

    assert app.requests == [kind]


def test_a_native_banner_never_bounces(shell, monkeypatch):
    from kestrel.services import mac_notify

    _, app = _fake_bounce_appkit(monkeypatch)
    monkeypatch.setattr(mac_notify, "post", lambda title, body: mac_notify.Path.NATIVE)

    shell._post_mac_notice("Kestrel is still running", "body")

    assert app.requests == []  # a banner was shown; nothing more


def test_a_denied_notice_logs_the_bounce_for_the_mac_checker(shell, monkeypatch):
    from kestrel.services import mac_notify

    _, app = _fake_bounce_appkit(monkeypatch)
    monkeypatch.setattr(mac_notify, "post", lambda title, body: mac_notify.Path.DENIED)

    with structlog_testing_logs() as logs:
        shell._post_mac_notice("Kestrel is still running", "body")

    assert any(
        e["event"] == "dock_bounce_requested" and e["path"] == "denied" for e in logs
    )


def test_a_frontmost_bounce_hides_the_app_then_requests(shell, monkeypatch):
    # Apple: "Sending requestUserAttention(_:) to an app that is already active has no
    # effect". The close-to-tray notice fires while Kestrel IS the active app, so without a
    # hide the bounce is never seen (this test was red on 53817c16da with zero bounces).
    kind, app = _fake_bounce_appkit(monkeypatch, _FakeNSApp(active=True))

    shell._request_dock_bounce()

    assert app.hide_calls == [None]  # the app hid, so the next app came forward
    assert app.requests == [kind]  # and the bounce was then seen


def test_an_inactive_app_bounces_without_hiding(shell, monkeypatch):
    kind, app = _fake_bounce_appkit(monkeypatch, _FakeNSApp(active=False))

    shell._request_dock_bounce()

    assert app.hide_calls == []  # nothing to hide away from
    assert app.requests == [kind]


def test_off_macos_a_denied_notice_never_touches_appkit(shell, monkeypatch):
    # No AppKit fake here on purpose: any AppKit-family import the code attempts off macOS
    # must fail this test loudly, not slip through as a quiet skip.
    from kestrel.services import mac_notify

    _refuse_appkit_imports(monkeypatch)
    monkeypatch.setattr(mac_notify, "post", lambda title, body: mac_notify.Path.DENIED)

    with structlog_testing_logs() as logs:
        shell._post_mac_notice("Kestrel is still running", "body")

    # The notice completed (the denied path logged), with no bounce and no import.
    assert any(e["event"] == "tray_notified" and e["path"] == "denied" for e in logs)


def test_the_bounce_leaves_the_calling_thread_on_macos(shell, monkeypatch):
    # The notice runs on the mac-notice thread; AppKit needs the main thread. From a
    # non-main thread the bounce must be queued (callAfter), never run in place.
    from kestrel.services import mac_notify

    kind, app = _fake_bounce_appkit(monkeypatch)
    queued: list = []
    _queued_apphelper(monkeypatch, queued)
    monkeypatch.setattr(mac_notify, "post", lambda title, body: mac_notify.Path.DENIED)

    worker = threading.Thread(
        target=shell._post_mac_notice, args=("t", "b"), name="mac-notice", daemon=True
    )
    worker.start()
    worker.join(timeout=30.0)

    assert app.requests == []  # nothing ran on the calling thread
    assert len(queued) == 1
    queued[0][0](*queued[0][1])
    assert app.requests == [kind]


# --------------------------------------------------------------- 6. the app hide is reversible


def test_show_window_unhides_an_app_hidden_by_the_bounce(shell, monkeypatch):
    # The bounce's NSApp.hide_ hides every window of the app; window.show() alone cannot
    # bring back a window of a hidden app, so show_window unhides first.
    app = _FakeNSApp(app_hidden=True)
    _fake_bounce_appkit(monkeypatch, app)
    shell._hidden = True

    shell.show_window()

    assert app.unhide_calls == [None]
    assert not app.app_hidden
    assert shell.window.calls == ["show", "restore"]
    assert shell._hidden is False


def test_show_window_on_a_visible_app_makes_no_appkit_app_unhide(shell, monkeypatch):
    app = _FakeNSApp(app_hidden=False)
    _fake_bounce_appkit(monkeypatch, app)
    shell._hidden = False

    shell.show_window()

    assert app.unhide_calls == []  # nothing to undo
    assert shell.window.calls == ["show", "restore"]


def test_off_macos_show_window_never_touches_appkit(shell, monkeypatch):
    # No AppKit fake on purpose: any AppKit-family import the code attempts off macOS must
    # fail this test loudly.
    _refuse_appkit_imports(monkeypatch)
    shell._hidden = True

    shell.show_window()

    assert shell.window.calls == ["show", "restore"]
    assert shell._hidden is False


# ------------------------------------------------ 7. the clean exit precedes the reply


def test_the_reply_is_queued_after_the_clean_exit(shell, monkeypatch):
    # AppKit's rule, confirmed by the Mac checker (0 of 5 kestrel_exit_clean): after a True
    # reply to NSTerminateLater it exits the process and run() never returns, so the whole
    # clean exit (drain, closing marker, log) must be finished before the reply is queued.
    from kestrel.desktop import shell as shell_module

    server = _Server()
    shell.server = server
    shell._started = True
    app = _FakeNSApp()
    queued: list = []
    _queued_apphelper(monkeypatch, queued)
    monkeypatch.setattr(shell_module, "IS_MACOS", True)
    d = _delegate(shell)

    with structlog_testing_logs() as logs:
        result = d.applicationShouldTerminate_(app)
        _joined_mac_quit()

    assert result is _LATER
    assert any(e["event"] == "kestrel_exit_clean" for e in logs)
    assert len(queued) == 1 and queued[0][2] is True  # queued at/after the destroy
    queued[0][0](*queued[0][1])
    assert app.replies == [True]


def test_the_clean_exit_is_logged_once_per_process(shell):
    # The terminate thread and the main-thread tail can both reach _finish_quit (the run
    # loop stops before the queued reply): the exit is logged once, not once per caller.
    with structlog_testing_logs() as logs:
        shell._finish_quit()
        shell._finish_quit()

    assert [e["event"] for e in logs].count("kestrel_exit_clean") == 1


# ------------------------------------------------------- 8. the long poll ends at shutdown


def test_the_events_long_poll_ends_at_shutdown():
    from kestrel.desktop.channel import APP_EVENTS, Channel

    channel = Channel(APP_EVENTS)
    results: list = []

    def poll() -> None:
        results.append(channel.after(0, 20.0))

    worker = threading.Thread(target=poll, name="events-poll", daemon=True)
    worker.start()
    _wait_until(lambda: channel.last_fetch is not None)  # the poll is inside its wait

    channel.end_waits()

    worker.join(timeout=8.0)  # bounded well under the poll's own 20 s wait
    assert not worker.is_alive()  # the shutdown is not held for the full poll timeout
    assert results == [(0, [])]


def test_a_page_waiting_when_the_quit_starts_still_sees_closing(shell, monkeypatch):
    # The real-Mac regression: end_waits ran before the closing post, so the page's open
    # poll returned ending with no events and the closing screen never showed (0 of 5).
    # The post comes first; a wait already open must wake with the closing event. The
    # 50 ms delay before the post makes the interleaving deterministic: on the unfixed
    # order the waiter's wake from end_waits cannot be outrun by the closing post.
    from kestrel.desktop.channel import APP_EVENTS, Channel

    channel = Channel(APP_EVENTS)
    shell._app_channel = channel
    shell.server = _Server()
    shell._started = True
    real_post = shell._post_to_app

    def slow_post(name: str, *args: Any) -> None:
        time.sleep(0.05)
        real_post(name, *args)

    monkeypatch.setattr(shell, "_post_to_app", slow_post)
    results: list = []

    def poll() -> None:
        results.append(channel.after(0, 20.0))

    worker = threading.Thread(target=poll, name="events-poll", daemon=True)
    worker.start()
    # Provably inside the wait, not merely past last_fetch: CPython's Condition keeps its
    # waiter locks in _waiters, so len == 1 means the worker is parked in wait_for.
    _wait_until(lambda: len(channel._cond._waiters) == 1)

    shell.do_quit()

    worker.join(timeout=8.0)
    assert not worker.is_alive()
    assert results == [(1, [["closing", []]])]
    assert shell.server.should_exit is True


def test_closing_is_posted_before_the_waits_end(shell, monkeypatch):
    # The order itself: when the waits end, 'closing' (seq 1) is already in the channel.
    from kestrel.desktop.channel import APP_EVENTS, Channel

    channel = Channel(APP_EVENTS)
    shell._app_channel = channel
    shell.server = _Server()
    shell._started = True
    seen: list = []
    original = Channel.end_waits

    def spy_end(self):
        seen.append(self.seq)
        original(self)

    monkeypatch.setattr(Channel, "end_waits", spy_end)

    shell._close_with_overlay()

    assert seen == [1]
    assert channel.after(0, 0)[1] == [["closing", []]]


def test_a_new_wait_after_shutdown_returns_at_once():
    # r5 item 2, pinned: end_waits ends not only the wait in flight but every later one,
    # so the page's next long poll answers at once instead of sitting out its timeout.
    from kestrel.desktop.channel import APP_EVENTS, Channel

    channel = Channel(APP_EVENTS)
    channel.end_waits()
    results: list = []

    worker = threading.Thread(target=lambda: results.append(channel.after(0, 20.0)), daemon=True)
    worker.start()

    worker.join(timeout=8.0)  # bounded well under the poll's own 20 s wait
    assert not worker.is_alive() and results == [(0, [])]
