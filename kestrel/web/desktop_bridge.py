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


def request_quit() -> bool:
    """Run the shell's real quit on a short delay, so the HTTP answer reaches the window before
    the server starts its drain. Returns whether a handler exists."""
    if _quit_handler is None:
        return False
    import threading

    threading.Timer(0.3, _quit_handler).start()
    return True


_reveal_handler: Callable[[Path], None] | None = None


def set_reveal_handler(fn: Callable[[Path], None] | None) -> None:
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
        _reveal_handler(path)
        return True
    except Exception as exc:  # a file-manager quirk must not fail the request
        log.warning("reveal_failed", path=str(path), error=f"{type(exc).__name__}: {exc}")
        return False


_folder_handler: Callable[[Path], None] | None = None


def set_folder_handler(fn: Callable[[Path], None] | None) -> None:
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
        _folder_handler(path)
        return True
    except Exception as exc:  # a file-manager quirk must not fail the request
        log.warning("open_folder_failed", path=str(path), error=f"{type(exc).__name__}: {exc}")
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
