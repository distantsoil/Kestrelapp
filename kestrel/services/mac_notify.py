"""macOS desktop notifications that carry Kestrel's own name and icon (D-NOTIFY-630).

`osascript -e 'display notification'` is credited by macOS to Script Editor, so a banner from it
shows Script Editor's icon and cannot be allowed or muted under Kestrel in System Settings >
Notifications. Inside the packaged Kestrel.app the banner is posted through UNUserNotificationCenter
instead (pyobjc-framework-UserNotifications), which credits it to the app bundle.

One entry point, `post(title, body)`, returns a Path value saying what happened:

- NATIVE: posted through UNUserNotificationCenter.
- OSASCRIPT: handed to macOS through the old osascript path (macOS may show nothing there), used
  when running from source, with no bundle, or when the native call failed before showing one.
- DENIED: the person turned Kestrel's notifications off.
- UNDECIDED: macOS asked the person and they have not answered yet (not reported as a refusal).
- Neither of those shows anything or falls back, because osascript would show the banner under
  another app's name and defeat the choice the person made or has yet to make.

The Apple and pyobjc claims this module relies on (local-only scheduling, completion handlers
on a background thread, the delegate's willPresent requirement, blocks and protocols in pyobjc)
are cited with links and a read date in the decision log, D-NOTIFY-636 to 638. What has not
been run without a Mac is listed there as unverified.

Invariants: a notification is local only (UNUserNotificationCenter schedules local notifications
only, and nothing here touches the network), carries exactly the title and body the caller passes
(no user info, no attachments, no sound), and the text is passed as data, never spliced into a
script. Every failure is logged and returns; nothing here raises.
"""

from __future__ import annotations

import enum
import shutil
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from typing import Any

import structlog

log = structlog.get_logger()

# Must equal BUNDLE_ID in packaging/kestrel.spec (a test compares them).
BUNDLE_ID = "com.kestrel.desktop"

# How long to wait for macOS to answer. The permission question waits on a person, so it gets
# longer; a settings read or a post is answered at once.
_REPLY_TIMEOUT_S = 5.0
_PERMISSION_TIMEOUT_S = 30.0

# osascript receives the text as ARGUMENTS to this fixed script, never spliced into it: a title
# holding a quote or a backslash must not be able to change what the script does.
OSASCRIPT = (
    "on run argv\n"
    "display notification (item 2 of argv) with title (item 1 of argv)\n"
    "end run"
)


class Path(enum.Enum):
    NATIVE = "native"
    OSASCRIPT = "osascript"
    DENIED = "denied"
    UNDECIDED = "undecided"  # macOS asked the person and they have not answered yet
    NONE = "none"  # no way to show one on this machine


class _NativeFailed(Exception):
    """The native call could not show the banner; the caller falls back."""


class _Denied(Exception):
    """The person has not allowed Kestrel's notifications."""


class _Undecided(Exception):
    """The permission question is on screen (or was) and has no answer yet: neither a yes nor a
    no, so it is never logged or treated as a refusal."""


# Set while the macOS permission question has been asked and not yet answered, so a later cycle
# never asks again (Apple documents the question as shown once, but that is unverified here).
_question_pending = threading.Event()
_question_lock = threading.Lock()


def _claim_question() -> bool:
    """Atomic test-and-set: True for the one caller that may ask, False for every other."""
    with _question_lock:
        if _question_pending.is_set():
            return False
        _question_pending.set()
        return True

_logged: set[str] = set()  # one line per kind of event per process, never one per cycle


def _log_once(key: str, event: str, **kw: Any) -> None:
    if key not in _logged:
        _logged.add(key)
        log.info(event, **kw)


def in_app_bundle() -> bool:
    """True when this process is the packaged Kestrel.app (a bundle with Kestrel's identifier).
    From source the main bundle is the Python interpreter's, whose identifier differs, and
    UNUserNotificationCenter must not be used there."""
    if sys.platform != "darwin":
        return False
    try:
        import objc  # type: ignore[import-not-found]
        from Foundation import NSBundle  # type: ignore[import-not-found]
    except ImportError as exc:  # pyobjc missing: not a bundle we can use
        log.debug("mac_notify_bundle_check_failed", error=f"{type(exc).__name__}: {exc}")
        return False
    try:
        bundle = NSBundle.mainBundle()
        return (
            str(bundle.bundleIdentifier() or "") == BUNDLE_ID
            and str(bundle.bundlePath() or "").endswith(".app")
        )
    except (objc.error, AttributeError) as exc:  # a broken binding: not a bundle we can use
        log.debug("mac_notify_bundle_check_failed", error=f"{type(exc).__name__}: {exc}")
        return False


_delegate_lock = threading.Lock()
_delegate_obj: Any = None


def _delegate() -> Any:
    """One delegate per process, built under a lock: the close-to-tray notice and a delivery
    cycle can arrive together, and the class must be defined once."""
    global _delegate_obj
    with _delegate_lock:
        if _delegate_obj is None:
            _delegate_obj = _make_delegate()
        return _delegate_obj


def _make_delegate() -> Any:
    """The notification center's delegate. Without one, macOS does not show a banner while the
    app is the active app (Apple's UNUserNotificationCenterDelegate willPresent documentation),
    and Kestrel is active at the moment close-to-tray hides its window. The center may keep only a
    weak reference (not documented either way), so this one object lives for the process
    (_delegate_obj)."""
    import objc  # type: ignore[import-not-found]
    import UserNotifications as UN  # type: ignore[import-not-found]
    from Foundation import NSObject  # type: ignore[import-not-found]

    try:
        protocols = [objc.protocolNamed("UNUserNotificationCenterDelegate")]
    except objc.error:  # protocol not loaded: pyobjc still deduces the signature from metadata
        protocols = []

    class KestrelNotificationDelegate(NSObject):
        __pyobjc_protocols__ = protocols  # pyobjc's documented form that also type-checks

        def userNotificationCenter_willPresentNotification_withCompletionHandler_(
            self, center, notification, handler
        ):
            handler(UN.UNNotificationPresentationOptionBanner)

    return KestrelNotificationDelegate.alloc().init()


def _wait(start: Callable[[Callable[..., None]], None], timeout: float) -> tuple[Any, ...] | None:
    """Run `start(callback)` and wait for the callback's arguments. macOS may run the callback on
    a background thread (Apple, requestAuthorization and add(_:withCompletionHandler:)). None when
    nothing answered in time."""
    done = threading.Event()
    box: list[tuple[Any, ...]] = []

    def callback(*args: Any) -> None:
        box.append(args)
        done.set()

    start(callback)
    return box[0] if done.wait(timeout) else None


def _native_post(title: str, body: str) -> None:
    """Post through UNUserNotificationCenter. Returns when macOS accepted it. Raises _Denied when
    notifications are off for Kestrel and _NativeFailed for anything else."""
    try:
        import UserNotifications as UN  # type: ignore[import-not-found]

        center = UN.UNUserNotificationCenter.currentNotificationCenter()
        center.setDelegate_(_delegate())
    except Exception as exc:
        raise _NativeFailed(f"{type(exc).__name__}: {exc}") from exc

    try:
        got = _wait(center.getNotificationSettingsWithCompletionHandler_, _REPLY_TIMEOUT_S)
        if got is None:
            raise _NativeFailed("no answer to the settings read")
        status = got[0].authorizationStatus()
        if status == UN.UNAuthorizationStatusNotDetermined:
            # Only one thread asks, and only once while it stays unanswered: a second caller (the
            # close notice and a delivery cycle can arrive together) or a later cycle never asks
            # again or holds a worker thread for 30 s; its notification is dropped.
            if not _claim_question():
                raise _Undecided
            # First notification ever: ask now, at the moment one is needed, for banners only
            # (no sound, no badge). macOS shows its own question; a person who has not answered
            # yet leaves this notification unshown.
            _log_once("ask", "mac_notify_permission_requested")
            try:
                asked = _wait(
                    lambda cb: center.requestAuthorizationWithOptions_completionHandler_(
                        UN.UNAuthorizationOptionAlert, cb
                    ),
                    _PERMISSION_TIMEOUT_S,
                )
            except Exception:
                # The question was never put to the person (a binding that could not make the
                # call): not "pending", so a later cycle may try again. Only a timeout, where the
                # question really is on screen, keeps the mark.
                _question_pending.clear()
                raise
            if asked is None:
                raise _Undecided  # the mark stays set until a later cycle sees a decided status
            _question_pending.clear()
            granted, error = asked[0], asked[1] if len(asked) > 1 else None
            if error is not None:
                # The system could not even ask (for example it refuses this build): that is a
                # failure, not a "no", so the osascript fallback still shows the notification.
                raise _NativeFailed(f"macOS could not ask for permission: {error}")
            if not granted:
                raise _Denied
        elif status == UN.UNAuthorizationStatusDenied:
            _question_pending.clear()
            raise _Denied
        else:
            _question_pending.clear()

        content = UN.UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        content.setBody_(body)  # no sound is set: the banner is silent (Kestrel plays its own)
        request = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(
            str(uuid.uuid4()), content, None  # a nil trigger delivers it now
        )
        added = _wait(
            lambda cb: center.addNotificationRequest_withCompletionHandler_(request, cb),
            _REPLY_TIMEOUT_S,
        )
    except (_Denied, _Undecided, _NativeFailed):
        raise
    except Exception as exc:
        raise _NativeFailed(f"{type(exc).__name__}: {exc}") from exc
    if added is None:
        raise _NativeFailed("no answer to the post")
    if added[0] is not None:
        raise _NativeFailed(f"macOS refused the notification: {added[0]}")


def _osascript_post(title: str, body: str) -> bool:
    tool = shutil.which("osascript")
    if tool is None:
        _log_once("no-osascript", "notify_toast_tool_missing", tool="osascript")
        return False
    from kestrel.llm.profiles import no_console_window, system_tool_env

    subprocess.run(  # noqa: S603  fixed argv list, no shell
        [tool, "-e", OSASCRIPT, title, body],
        check=True, capture_output=True, timeout=10,
        **no_console_window(), **system_tool_env(),
    )
    return True


def post(title: str, body: str) -> Path:
    """Show a silent local notification, natively when this is the packaged app. See the module
    docstring for what each result means. Raises only when the osascript fallback is the path
    taken and that tool exists and then fails (the caller's channel handler logs that)."""
    if in_app_bundle():
        try:
            _native_post(title, body)
        except _Denied:
            _log_once(
                "denied", "mac_notify_denied",
                hint="turn on Kestrel under System Settings > Notifications",
            )
            return Path.DENIED
        except _Undecided:
            _log_once("undecided", "mac_notify_question_unanswered")
            return Path.UNDECIDED
        except _NativeFailed as exc:
            log.warning("mac_notify_native_failed", error=str(exc), fallback="osascript")
        else:
            log.info("mac_notify_path", path=Path.NATIVE.value)
            return Path.NATIVE
    if _osascript_post(title, body):
        # osascript exits 0 even where macOS shows nothing (a Mac where Script Editor was never
        # registered shows no banner at all), so this means "handed to macOS", never "shown".
        log.info(
            "mac_notify_path", path=Path.OSASCRIPT.value, bundle=in_app_bundle(),
            shown_verified=False,
        )
        return Path.OSASCRIPT
    return Path.NONE
