"""Linux: why the Kestrel window could not open, in words a user can act on (release round R1).

pywebview draws its window on Linux with GTK and WebKit2GTK, through the PyGObject binding (`gi`),
or with Qt. On a machine without them `webview.initialize()` fails and the shell falls back to the
default browser (kestrel.desktop.shell._run_in_browser). The app still works there, but the user
should know what is missing and how to add it, so the browser fallback shows a plain panel (the
dashboard and the onboarding both render it): what is missing, and the one-line install for the
common distributions behind Show details.

`detect()` is pure: it only tries the imports, never installs anything (Kestrel never installs
software on the user's behalf). The shell calls `record_fallback()` when the window could not
open; the web layer reads `fallback()` in the same process."""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger()

# The WebKit2GTK API versions pywebview's GTK backend accepts, newest first.
WEBKIT_VERSIONS: tuple[str, ...] = ("4.1", "4.0")

# One line per distribution family: the WebKit2GTK runtime and its GObject introspection data,
# plus the Python binding for a from-source install. Package names checked against each distro's
# package index; UNVERIFIED on a live machine from the build container (see the decision log).
INSTALL: tuple[tuple[str, str], ...] = (
    ("Debian, Ubuntu, Linux Mint, Pop!_OS",
     "sudo apt install gir1.2-webkit2-4.1 libwebkit2gtk-4.1-0 python3-gi"),
    ("Fedora", "sudo dnf install webkit2gtk4.1 python3-gobject"),
    ("Arch, Manjaro, EndeavourOS", "sudo pacman -S webkit2gtk-4.1 python-gobject"),
)


@dataclass
class WebKitGap:
    """What is missing for the native window, for the browser-fallback panel."""

    missing: str  # one plain sentence
    detail: str = ""  # the import error, for Show details
    install: list[tuple[str, str]] = field(default_factory=lambda: list(INSTALL))
    # The packaged AppImage does not bundle PyGObject, so system packages cannot give it a window:
    # the panel then says the browser is the window rather than promising an install fixes it.
    frozen: bool = field(default_factory=lambda: bool(getattr(sys, "frozen", False)))


_state: dict[str, WebKitGap | None] = {"gap": None}


def _import(name: str):
    return importlib.import_module(name)


def _qt_available() -> bool:
    """pywebview can use Qt instead of GTK; with a Qt WebEngine there is nothing to report."""
    for mod in ("PyQt6.QtWebEngineWidgets", "PyQt5.QtWebEngineWidgets",
                "PySide6.QtWebEngineWidgets"):
        try:
            _import(mod)
        except (ImportError, RuntimeError, ValueError, OSError):
            continue
        return True
    return False


def detect(platform: str | None = None) -> WebKitGap | None:
    """On Linux, None when GTK WebKit (or Qt WebEngine) is usable, otherwise what is missing.
    Always None on other platforms."""
    if (platform or sys.platform) != "linux":
        return None
    try:
        gi = _import("gi")
    except (ImportError, OSError) as exc:
        if _qt_available():
            return None
        return WebKitGap(
            "The GTK bindings for Python (PyGObject) are missing, so Kestrel cannot draw its "
            "own window.",
            f"{type(exc).__name__}: {exc}",
        )
    errors: list[str] = []
    try:
        gi.require_version("Gtk", "3.0")
        _import("gi.repository.Gtk")
    except (ImportError, ValueError, OSError, AttributeError) as exc:
        errors.append(f"Gtk 3.0: {type(exc).__name__}: {exc}")
        if _qt_available():
            return None
        return WebKitGap(
            "GTK 3 is missing, so Kestrel cannot draw its own window.", "; ".join(errors)
        )
    for version in WEBKIT_VERSIONS:
        try:
            gi.require_version("WebKit2", version)
            _import("gi.repository.WebKit2")
        except (ImportError, ValueError, OSError, AttributeError) as exc:
            errors.append(f"WebKit2 {version}: {type(exc).__name__}: {exc}")
            continue
        return None
    if _qt_available():
        return None
    return WebKitGap(
        "WebKit2GTK, the web view GTK uses, is missing, so Kestrel cannot draw its own window.",
        "; ".join(errors),
    )


def record_fallback(error: str, platform: str | None = None) -> WebKitGap | None:
    """The shell could not open the native window: remember why, for the panel. On Linux with
    nothing detectably missing, the panel still says the window could not open and why."""
    if (platform or sys.platform) != "linux":
        _state["gap"] = None
        return None
    gap = detect(platform) or WebKitGap(
        "Kestrel could not open its own window, so it opened in your browser instead.", error
    )
    if error and error not in gap.detail:
        gap.detail = f"{gap.detail}; window error: {error}" if gap.detail else error
    _state["gap"] = gap
    log.warning("linux_webkit_missing", missing=gap.missing, detail=gap.detail[:300])
    return gap


def fallback() -> WebKitGap | None:
    """Set only in the desktop process after the native window failed on Linux."""
    return _state["gap"]


def clear() -> None:
    _state["gap"] = None
