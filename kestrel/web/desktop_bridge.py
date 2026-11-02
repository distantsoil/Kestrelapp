"""A tiny bridge from the web layer to the desktop shell (SPEC-INTERFACES 1.1). /api/focus lives in
the web routes but the window it must raise lives in kestrel.desktop.shell; the shell registers its
show_window here at startup, and /api/focus calls request_focus(). In the headless `kestrel serve`
path no handler is registered, so a focus request is simply a no-op."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import structlog

log = structlog.get_logger()

_focus_handler: Callable[[], None] | None = None


def set_focus_handler(fn: Callable[[], None] | None) -> None:
    global _focus_handler
    _focus_handler = fn


_quit_handler: Callable[[], None] | None = None


def set_quit_handler(fn: Callable[[], None] | None) -> None:
    """The shell registers do_quit here (D-UI-27); headless serve registers nothing."""
    global _quit_handler
    _quit_handler = fn


def can_quit() -> bool:
    return _quit_handler is not None


_window_frame = False


def set_window_frame(on: bool) -> None:
    """The shell says the UI is in its own frameless window (D-UI-88). Then the window controls
    in the sidebar close and quit, and the top row carries no Quit button (the locked mockups
    have none: Quit is in the tray). In the browser fallback the Quit button stays, since a tab
    has no window controls."""
    global _window_frame
    _window_frame = bool(on)


def has_window_frame() -> bool:
    return _window_frame


_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def desktop_window(request) -> bool:
    """Whether this page is for the desktop window: the shell's window is up and the request comes
    from this computer. Decided on the server, so the page knows before pywebview is ready. A LAN
    device is never "desktop"; a local browser tab beside the window may be, which only means it
    skips the offline cache."""
    if not _window_frame or request is None:
        return False
    client = getattr(request, "client", None)
    return bool(client and client.host in _LOOPBACK)


def request_quit() -> bool:
    """Run the shell's real quit on a short delay, so the HTTP answer reaches the window before
    the server starts its drain. Returns whether a handler exists."""
    if _quit_handler is None:
        return False
    import threading

    threading.Timer(0.3, _quit_handler).start()
    return True


_reveal_handler: Callable[[Path], bool | None] | None = None


def set_reveal_handler(fn: Callable[[Path], bool | None] | None) -> None:
    """The shell registers a handler that shows a saved file in the OS file manager (D-UI-41):
    Explorer with the file selected on Windows. Headless serve registers nothing, so the web
    layer knows it is in a plain browser and keeps the normal download."""
    global _reveal_handler
    _reveal_handler = fn


def can_reveal() -> bool:
    return _reveal_handler is not None


def request_reveal(path: Path) -> bool:
    """Show `path` in the file manager. Returns whether the shell took the request. A local
    action on the operator's own file (hard rule 1 is about third parties). Never raises."""
    if _reveal_handler is None:
        return False
    try:
        # A handler that reports failure returns False (the shell does, #100); one that returns
        # nothing took the request.
        return _reveal_handler(path) is not False
    except Exception as exc:  # a file-manager quirk must not fail the request
        log.warning("reveal_failed", error_type=type(exc).__name__,
                    errno=getattr(exc, "errno", None), winerror=getattr(exc, "winerror", None))
        return False


_folder_handler: Callable[[Path], bool | None] | None = None


def set_folder_handler(fn: Callable[[Path], bool | None] | None) -> None:
    """The shell registers a handler that opens a folder in the OS file manager (About's Open
    buttons for the data and logs folders). A plain browser registers nothing."""
    global _folder_handler
    _folder_handler = fn


def can_open_folder() -> bool:
    return _folder_handler is not None


def request_open_folder(path: Path) -> bool:
    """Open the folder `path` in the file manager. Returns whether the shell took the request.
    A local action on the operator's own folder. Never raises."""
    if _folder_handler is None:
        return False
    try:
        return _folder_handler(path) is not False
    except Exception as exc:  # a file-manager quirk must not fail the request
        log.warning("open_folder_failed", error_type=type(exc).__name__,
                    errno=getattr(exc, "errno", None), winerror=getattr(exc, "winerror", None))
        return False


def request_focus() -> bool:
    """Raise the desktop window if a handler is registered. Returns whether one ran. Never raises:
    a second launch focusing the first must never crash on a slow or backend-quirky show()."""
    if _focus_handler is None:
        return False
    try:
        _focus_handler()
        return True
    except Exception:  # focusing is best-effort; a backend quirk must not fail the request
        return False
