"""Start Kestrel when the computer starts (Settings > This computer > Everyday options).

It opens quietly in the tray: the entry runs the app with `--tray`, which the desktop entry point
reads and uses to leave the window hidden. Each platform has its own plain place for this, all in
the current person's own account, so no administrator rights are needed and nothing is installed
system-wide:

* Windows: a value under HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run.
* macOS: a launch agent file in ~/Library/LaunchAgents.
* Linux: an autostart .desktop file in ~/.config/autostart.

`set_enabled` returns None when it worked, or one plain sentence saying why not; it never raises.
The "enabled" answer is read from the entry itself, never from a stored flag, so a person who
removes it in their system's own startup list sees the switch turn off. Nothing is sent
anywhere."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from xml.sax.saxutils import escape

import structlog

log = structlog.get_logger()

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Kestrel"
AGENT_LABEL = "co.kestrelapp.kestrel"
DESKTOP_NAME = "kestrel.desktop"
TRAY_FLAG = "--tray"


def command() -> list[str]:
    """What the system runs at sign-in: the packaged app itself, or this Python in a source
    checkout."""
    if getattr(sys, "frozen", False):
        return [sys.executable, TRAY_FLAG]
    exe = sys.executable
    if sys.platform.startswith("win") and exe.lower().endswith("python.exe"):
        windowless = Path(exe).with_name("pythonw.exe")  # no console window at sign-in
        if windowless.is_file():
            exe = str(windowless)
    return [exe, "-m", "kestrel.desktop", TRAY_FLAG]


def platform_name() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _quote_windows(parts: list[str]) -> str:
    return " ".join(f'"{p}"' if " " in p else p for p in parts)


def _agent_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def _desktop_path(home: Path) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
    return base / "autostart" / DESKTOP_NAME


def _plist(parts: list[str]) -> str:
    args = "".join(f"    <string>{escape(p)}</string>\n" for p in parts)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f"  <key>Label</key>\n  <string>{AGENT_LABEL}</string>\n"
        f"  <key>ProgramArguments</key>\n  <array>\n{args}  </array>\n"
        "  <key>RunAtLoad</key>\n  <true/>\n"
        "</dict>\n</plist>\n"
    )


def _desktop_entry(parts: list[str]) -> str:
    def q(p: str) -> str:
        return '"' + p.replace("\\", "\\\\").replace('"', '\\"') + '"' if " " in p else p

    return (
        "[Desktop Entry]\nType=Application\nName=Kestrel\n"
        f"Exec={' '.join(q(p) for p in parts)}\n"
        "X-GNOME-Autostart-enabled=true\nTerminal=false\n"
    )


def is_enabled(*, home: Path | None = None) -> bool:
    plat = platform_name()
    try:
        if plat == "windows":
            import winreg  # type: ignore[import-not-found]

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, VALUE_NAME)
            return True
        h = home or Path.home()
        return (_agent_path(h) if plat == "macos" else _desktop_path(h)).is_file()
    except OSError:
        return False


def set_enabled(on: bool, *, home: Path | None = None,
                parts: list[str] | None = None) -> str | None:
    """Add or remove the startup entry. None when it took, else one plain sentence."""
    plat = platform_name()
    cmd = parts or command()
    try:
        if plat == "windows":
            import winreg  # type: ignore[import-not-found]

            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                if on:
                    winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _quote_windows(cmd))
                else:
                    try:
                        winreg.DeleteValue(key, VALUE_NAME)
                    except FileNotFoundError:
                        pass
            return None
        h = home or Path.home()
        path = _agent_path(h) if plat == "macos" else _desktop_path(h)
        if on:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".part")
            tmp.write_text(_plist(cmd) if plat == "macos" else _desktop_entry(cmd),
                           encoding="utf-8")
            os.replace(tmp, path)
        else:
            path.unlink(missing_ok=True)
        return None
    except OSError as exc:
        log.warning("autostart_failed", on=on, platform=plat, error_type=type(exc).__name__)
        return "Your computer would not let Kestrel change its startup list."
