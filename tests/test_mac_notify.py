"""macOS notifications post as Kestrel.app (issue #200, D-NOTIFY-630 to D-NOTIFY-633).

No test touches the network, a real Mac or a real pyobjc: UserNotifications is a recorder injected
into sys.modules, and subprocess.run is replaced. The invariant: a notification is local only and
carries exactly the title and body the caller passed."""

from __future__ import annotations

import ast
import sys
import threading
import types
from pathlib import Path as FsPath

import pytest
import structlog

from kestrel.services import delivery, mac_notify

ROOT = FsPath(__file__).resolve().parent.parent


class _Settings:
    def __init__(self, status: int) -> None:
        self._status = status

    def authorizationStatus(self) -> int:  # noqa: N802  mirrors the Apple name
        return self._status


class _Content:
    sound = None

    def __init__(self) -> None:
        self.title = self.body = None
        self.fields_set: set[str] = set()

    @classmethod
    def alloc(cls) -> _Content:
        return cls()

    def init(self) -> _Content:
        return self

    def setTitle_(self, v):  # noqa: N802
        self.title = v
        self.fields_set.add("title")

    def setBody_(self, v):  # noqa: N802
        self.body = v
        self.fields_set.add("body")


class _Request:
    @staticmethod
    def requestWithIdentifier_content_trigger_(ident, content, trigger):  # noqa: N802
        return types.SimpleNamespace(ident=ident, content=content, trigger=trigger)


class _Center:
    """What UNUserNotificationCenter does, as far as Kestrel uses it."""

    def __init__(
        self, status: int, grant: bool = True, add_error=None, settings_answers=True,
        ask_error=None, ask_answers=True,
    ):
        self.status, self.grant, self.add_error = status, grant, add_error
        self.ask_error, self.ask_answers = ask_error, ask_answers
        self.settings_answers = settings_answers
        self.delegate = None
        self.added: list = []
        self.asked: list = []

    def setDelegate_(self, d):  # noqa: N802
        self.delegate = d

    def getNotificationSettingsWithCompletionHandler_(self, cb):  # noqa: N802
        if self.settings_answers:
            cb(_Settings(self.status))

    def requestAuthorizationWithOptions_completionHandler_(self, options, cb):  # noqa: N802
        self.asked.append(options)
        if not self.ask_answers:
            return  # the person has not answered yet
        if self.grant:
            self.status = 2
        cb(self.grant, self.ask_error)

    def addNotificationRequest_withCompletionHandler_(self, request, cb):  # noqa: N802
        self.added.append(request)
        cb(self.add_error)


def _install(monkeypatch, center: _Center | Exception, *, bundled: bool = True):
    """Make the module believe it is the packaged app, with `center` as the notification center."""
    un = types.ModuleType("UserNotifications")
    un.UNAuthorizationStatusNotDetermined = 0
    un.UNAuthorizationStatusDenied = 1
    un.UNAuthorizationStatusAuthorized = 2
    un.UNAuthorizationOptionAlert = 4
    un.UNMutableNotificationContent = _Content
    un.UNNotificationRequest = _Request

    class _UNC:
        @staticmethod
        def currentNotificationCenter():  # noqa: N802
            if isinstance(center, Exception):
                raise center
            return center

    un.UNUserNotificationCenter = _UNC
    monkeypatch.setitem(sys.modules, "UserNotifications", un)
    monkeypatch.setattr(mac_notify, "_delegate", lambda: "delegate")
    monkeypatch.setattr(mac_notify, "in_app_bundle", lambda: bundled)
    monkeypatch.setattr(mac_notify, "_logged", set())
    monkeypatch.setattr(mac_notify, "_question_pending", threading.Event())


class _Run:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if self.fail:
            raise OSError("no osascript")
        return None


@pytest.fixture
def osascript(monkeypatch):
    run = _Run()
    monkeypatch.setattr(mac_notify.subprocess, "run", run)
    monkeypatch.setattr(mac_notify.shutil, "which", lambda n: "/usr/bin/osascript")
    return run


def _logs():
    return structlog.testing.capture_logs()


# --- from inside the bundle the native path is used -------------------------------------------
def test_inside_the_bundle_the_native_path_posts_and_osascript_is_not_run(monkeypatch, osascript):
    center = _Center(status=2)
    _install(monkeypatch, center)
    with _logs() as logs:
        assert mac_notify.post("Run finished", "4 new") is mac_notify.Path.NATIVE
    assert osascript.calls == []
    assert len(center.added) == 1 and center.delegate == "delegate"
    assert any(e["event"] == "mac_notify_path" and e["path"] == "native" for e in logs)


def test_the_native_notification_is_local_silent_and_carries_only_the_given_text(
    monkeypatch, osascript
):
    """The invariant: no sound, no extra fields, a nil trigger (deliver now, local only), and the
    exact title and body, hostile characters included, as data."""
    center = _Center(status=2)
    _install(monkeypatch, center)
    hostile = 'Acme" & do shell script "rm -rf ~'
    assert mac_notify.post(hostile, "line one\nline two \\ 'q'") is mac_notify.Path.NATIVE
    req = center.added[0]
    assert req.trigger is None  # UNNotificationRequest with no trigger: delivered at once, local
    assert req.content.title == hostile and req.content.body == "line one\nline two \\ 'q'"
    assert req.content.fields_set == {"title", "body"}  # nothing else was set
    assert req.content.sound is None  # silent: Kestrel plays its own sound
    assert not hasattr(req.content, "userInfo")


# --- from source the fallback is used ----------------------------------------------------------
def test_from_source_the_osascript_fallback_is_used_and_logged(monkeypatch, osascript):
    _install(monkeypatch, _Center(status=2), bundled=False)
    with _logs() as logs:
        assert mac_notify.post("T", "B") is mac_notify.Path.OSASCRIPT
    assert osascript.calls[0][:2] == ["/usr/bin/osascript", "-e"]
    assert any(e["event"] == "mac_notify_path" and e["path"] == "osascript" for e in logs)


def test_the_osascript_text_is_arguments_never_part_of_the_script(monkeypatch, osascript):
    _install(monkeypatch, _Center(status=2), bundled=False)
    hostile = 'x" & do shell script "id'
    mac_notify.post(hostile, "body")
    argv = osascript.calls[0]
    assert hostile not in argv[2] and argv[3:] == [hostile, "body"]
    assert "sound name" not in argv[2]


def test_a_machine_without_a_native_center_falls_back(monkeypatch, osascript):
    _install(monkeypatch, RuntimeError("no bundle proxy"))
    with _logs() as logs:
        assert mac_notify.post("T", "B") is mac_notify.Path.OSASCRIPT
    warn = [e for e in logs if e["event"] == "mac_notify_native_failed"]
    assert warn and warn[0]["fallback"] == "osascript"


def test_in_app_bundle_is_false_off_a_mac_and_without_pyobjc(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert mac_notify.in_app_bundle() is False
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "Foundation", None)  # an import that fails
    assert mac_notify.in_app_bundle() is False


def test_in_app_bundle_needs_the_kestrel_identifier_and_an_app_path(monkeypatch):
    objc = types.ModuleType("objc")
    objc.error = type("error", (Exception,), {})
    monkeypatch.setitem(sys.modules, "objc", objc)

    def fake(ident, path):
        b = types.SimpleNamespace(bundleIdentifier=lambda: ident, bundlePath=lambda: path)
        mod = types.ModuleType("Foundation")
        mod.NSBundle = types.SimpleNamespace(mainBundle=lambda: b)
        monkeypatch.setitem(sys.modules, "Foundation", mod)

    monkeypatch.setattr(sys, "platform", "darwin")
    fake(mac_notify.BUNDLE_ID, "/Applications/Kestrel.app")
    assert mac_notify.in_app_bundle() is True
    fake("org.python.python", "/Applications/Kestrel.app")
    assert mac_notify.in_app_bundle() is False  # the interpreter's bundle, not ours
    fake(mac_notify.BUNDLE_ID, "/usr/local/bin")
    assert mac_notify.in_app_bundle() is False


def test_a_broken_binding_is_not_a_bundle(monkeypatch):
    objc = types.ModuleType("objc")
    objc.error = type("error", (Exception,), {})
    monkeypatch.setitem(sys.modules, "objc", objc)
    mod = types.ModuleType("Foundation")

    def boom():
        raise objc.error("no main bundle")

    mod.NSBundle = types.SimpleNamespace(mainBundle=boom)
    monkeypatch.setitem(sys.modules, "Foundation", mod)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert mac_notify.in_app_bundle() is False


# --- permission ---------------------------------------------------------------------------------
def test_the_first_notification_asks_for_banners_only_then_posts(monkeypatch, osascript):
    center = _Center(status=0, grant=True)
    _install(monkeypatch, center)
    assert mac_notify.post("T", "B") is mac_notify.Path.NATIVE
    assert center.asked == [4]  # UNAuthorizationOptionAlert only: no sound, no badge
    assert len(center.added) == 1


def test_a_refused_question_shows_nothing_and_does_not_fall_back(monkeypatch, osascript):
    center = _Center(status=0, grant=False)
    _install(monkeypatch, center)
    assert mac_notify.post("T", "B") is mac_notify.Path.DENIED
    assert center.added == [] and osascript.calls == []


def test_denied_in_system_settings_is_logged_once_and_never_falls_back(monkeypatch, osascript):
    center = _Center(status=1)
    _install(monkeypatch, center)
    with _logs() as logs:
        for _ in range(3):
            assert mac_notify.post("T", "B") is mac_notify.Path.DENIED
    assert [e["event"] for e in logs].count("mac_notify_denied") == 1
    assert center.added == [] and center.asked == [] and osascript.calls == []


def test_a_permission_error_is_a_failure_not_a_no_and_osascript_still_shows_it(
    monkeypatch, osascript
):
    """The system refusing to ask (an unsigned build, say) returns granted=False WITH an error.
    That must not read as the person saying no: the notification still goes out by osascript."""
    center = _Center(status=0, grant=False, ask_error="UNErrorDomain 1")
    _install(monkeypatch, center)
    with _logs() as logs:
        assert mac_notify.post("T", "B") is mac_notify.Path.OSASCRIPT
    assert center.added == [] and len(osascript.calls) == 1
    assert any(e["event"] == "mac_notify_native_failed" for e in logs)


def test_an_unanswered_question_is_asked_once_not_every_cycle(monkeypatch, osascript):
    monkeypatch.setattr(mac_notify, "_PERMISSION_TIMEOUT_S", 0.05)
    center = _Center(status=0, ask_answers=False)
    _install(monkeypatch, center)
    with _logs() as logs:
        for _ in range(3):
            assert mac_notify.post("T", "B") is mac_notify.Path.UNDECIDED
    assert len(center.asked) == 1 and osascript.calls == []
    # An unanswered question is unknown, never logged as a refusal with a "turn it on" hint.
    names = [e["event"] for e in logs]
    assert "mac_notify_denied" not in names and names.count("mac_notify_question_unanswered") == 1
    # Once the person answers yes, the next cycle posts and the pending mark is gone.
    center.status = 2
    assert mac_notify.post("T", "B") is mac_notify.Path.NATIVE
    assert not mac_notify._question_pending.is_set()


def test_a_raising_ask_is_not_a_pending_question_and_a_later_cycle_asks_again(
    monkeypatch, osascript
):
    """A binding that cannot make the call never put a question to the person: the mark must not
    stay, or every later banner would be dropped as undecided until the app restarts."""
    center = _Center(status=0)
    calls = []

    def broken(options, cb):
        calls.append(options)
        raise RuntimeError("block signature not resolved")

    center.requestAuthorizationWithOptions_completionHandler_ = broken
    _install(monkeypatch, center)
    with _logs() as logs:
        assert mac_notify.post("T", "B") is mac_notify.Path.OSASCRIPT  # fallback shows it
    assert not mac_notify._question_pending.is_set()
    assert any(e["event"] == "mac_notify_native_failed" for e in logs)
    assert not any(e["event"] == "mac_notify_question_unanswered" for e in logs)
    assert mac_notify.post("T", "B") is mac_notify.Path.OSASCRIPT  # asks again, not undecided
    assert len(calls) == 2


def test_two_threads_never_ask_at_once(monkeypatch, osascript):
    """The close notice and a delivery cycle can arrive together: one asks, the other is dropped
    as undecided at once instead of asking a second time or waiting out the first one's 30 s."""
    entered, release = threading.Event(), threading.Event()
    center = _Center(status=0, ask_answers=False)
    _install(monkeypatch, center)
    real_ask = center.requestAuthorizationWithOptions_completionHandler_

    def slow_ask(options, cb):
        real_ask(options, cb)
        entered.set()
        release.wait(5)
        cb(True, None)

    center.requestAuthorizationWithOptions_completionHandler_ = slow_ask
    first: list = []
    t = threading.Thread(target=lambda: first.append(mac_notify.post("A", "a")))
    t.start()
    assert entered.wait(5)
    assert mac_notify.post("B", "b") is mac_notify.Path.UNDECIDED  # returns at once
    release.set()
    t.join(5)
    assert len(center.asked) == 1 and first == [mac_notify.Path.NATIVE]


def test_the_delegate_class_is_built_once_under_concurrent_first_use(monkeypatch):
    built = []
    gate = threading.Event()

    def make():
        built.append(1)
        gate.wait(0.2)  # a slow first build, so a second caller would overlap it
        return object()

    monkeypatch.setattr(mac_notify, "_delegate_obj", None)
    monkeypatch.setattr(mac_notify, "_make_delegate", make)
    out: list = []
    threads = [
        threading.Thread(target=lambda: out.append(mac_notify._delegate())) for _ in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(built) == 1 and len({id(o) for o in out}) == 1 and len(out) == 6


# --- a failed call -------------------------------------------------------------------------------
def test_a_refused_post_falls_back_and_never_raises(monkeypatch, osascript):
    _install(monkeypatch, _Center(status=2, add_error="UNErrorDomain 1"))
    with _logs() as logs:
        assert mac_notify.post("T", "B") is mac_notify.Path.OSASCRIPT
    assert any(e["event"] == "mac_notify_native_failed" for e in logs)


def test_a_silent_center_times_out_and_falls_back(monkeypatch, osascript):
    monkeypatch.setattr(mac_notify, "_REPLY_TIMEOUT_S", 0.05)
    _install(monkeypatch, _Center(status=2, settings_answers=False))
    assert mac_notify.post("T", "B") is mac_notify.Path.OSASCRIPT


def test_no_osascript_and_no_bundle_shows_nothing(monkeypatch):
    _install(monkeypatch, _Center(status=2), bundled=False)
    monkeypatch.setattr(mac_notify.shutil, "which", lambda n: None)
    assert mac_notify.post("T", "B") is mac_notify.Path.NONE


# --- delivery wiring and the other platforms -----------------------------------------------------
@pytest.mark.parametrize(
    ("result", "shown"),
    [
        (mac_notify.Path.NATIVE, True),
        (mac_notify.Path.OSASCRIPT, True),
        (mac_notify.Path.DENIED, False),
        (mac_notify.Path.UNDECIDED, False),
        (mac_notify.Path.NONE, False),
    ],
)
def test_delivery_toast_on_macos_goes_through_mac_notify(monkeypatch, result, shown):
    calls = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(mac_notify, "post", lambda t, b: calls.append((t, b)) or result)
    assert delivery._toast("Title", None) is shown
    assert calls == [("Title", "")]


def test_linux_still_uses_notify_send_and_never_mac_notify(monkeypatch):
    ran = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(delivery.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(delivery.subprocess, "run", lambda argv, **kw: ran.append(argv))
    monkeypatch.setattr(
        mac_notify, "post", lambda *a: pytest.fail("mac_notify is for macOS only")
    )
    assert delivery._toast("T", "B") is True
    assert ran[0][0] == "/usr/bin/notify-send"


def test_the_close_to_tray_notice_on_macos_posts_through_mac_notify(monkeypatch):
    from kestrel.desktop import shell

    posted = []
    done = threading.Event()

    class _Icon:
        def notify(self, *a):
            pytest.fail("pystray's notify shows as Script Editor on macOS")

    monkeypatch.setattr(shell, "IS_MACOS", True)
    monkeypatch.setattr(shell, "icon", _Icon())
    monkeypatch.setattr(
        mac_notify,
        "post",
        lambda t, b: posted.append((t, b)) or done.set() or mac_notify.Path.NATIVE,
    )
    shell._notify(shell.CLOSE_TO_TRAY_TITLE, shell.CLOSE_TO_TRAY_BODY_MAC)
    assert done.wait(5)
    assert posted == [(shell.CLOSE_TO_TRAY_TITLE, shell.CLOSE_TO_TRAY_BODY_MAC)]


def test_the_close_to_tray_notice_never_holds_the_closing_handler(monkeypatch):
    """The post can wait on macOS (the permission question), and it runs inside the window's
    closing handler: _notify must return at once while the post is still waiting."""
    from kestrel.desktop import shell

    release, entered = threading.Event(), threading.Event()
    thread_names: list[str] = []

    def slow_post(t, b):
        thread_names.append(threading.current_thread().name)
        entered.set()
        release.wait(10)
        return mac_notify.Path.NATIVE

    monkeypatch.setattr(shell, "IS_MACOS", True)
    monkeypatch.setattr(shell, "icon", object())
    monkeypatch.setattr(mac_notify, "post", slow_post)
    shell._notify("t", "b")  # returns although slow_post is still blocked
    assert entered.wait(5) and not release.is_set()
    assert thread_names == ["mac-notice"]
    release.set()


# --- packaging ------------------------------------------------------------------------------------
def test_the_bundle_identifier_matches_the_one_the_packaging_sets():
    tree = ast.parse((ROOT / "packaging" / "kestrel.spec").read_text(encoding="utf-8"))
    spec_id = next(
        n.value.value
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", "") == "BUNDLE_ID" for t in n.targets)
    )
    assert spec_id == mac_notify.BUNDLE_ID


def test_the_user_notifications_binding_is_a_macos_only_pinned_dependency():
    import tomllib

    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]
    line = next(d for d in deps if d.startswith("pyobjc-framework-UserNotifications"))
    assert "sys_platform == 'darwin'" in line and "==12.2.1" in line
