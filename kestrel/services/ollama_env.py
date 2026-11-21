"""Ollama's two speed settings, set from Settings > This computer with a button (issue #181).

`OLLAMA_NUM_PARALLEL` below the configured concurrency makes requests wait in line, and a short
`OLLAMA_KEEP_ALIVE` reloads model weights between steps (CLAUDE.md platform traps; SPEC.md 6.6).
Both are read by the separate Ollama process, so Kestrel can only set them where Ollama is
documented to read them, per operating system:

- Windows: the user's environment variables (HKEY_CURRENT_USER\\Environment), the place Ollama's
  FAQ points to ("Ollama inherits your user and system environment variables").
- macOS: `launchctl setenv`, the FAQ's mechanism for the Ollama app.
- Linux: a systemd drop-in for a user `ollama.service`, or a marked block in `~/.profile` when no
  service runs Ollama. A system-wide `ollama.service` needs an administrator, so Kestrel only
  shows the commands for it (D-SETUP-653).

Invariant: nothing on the person's system changes without the exact button press. Every write
goes through one accessor, `backend()`, and only `apply`, `undo` and `restart` below call its
write methods. `apply` and `undo` take the digest of the plan or the undo record the person was
shown; a missing or stale digest changes nothing (D-SETUP-651).

Sources, read 2026-11-15 (D-SETUP-650):
- Ollama FAQ, "How do I configure Ollama server?" and "How do I keep a model loaded in memory":
  https://docs.ollama.com/faq
- setx (writes the user environment in the registry; future windows only):
  https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/setx
- launchctl setenv, getenv, unsetenv: https://ss64.com/mac/launchctl.html
- systemd drop-ins and the user unit path:
  https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html
- systemctl --user, daemon-reload, restart:
  https://www.freedesktop.org/software/systemd/man/latest/systemctl.html
How the running Ollama reads these values is unverified until the live test in working.log.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

PARALLEL = "OLLAMA_NUM_PARALLEL"
KEEP_ALIVE = "OLLAMA_KEEP_ALIVE"
NAMES: tuple[str, ...] = (PARALLEL, KEEP_ALIVE)
KEEP_ALIVE_VALUE = "2h"
# Ollama's default is 5 minutes (FAQ). Under an hour still reloads between long steps.
SHORT_SECONDS = 3600.0
UNDO_KEY = "ollama_env.undo"
HINT_KEYS: tuple[str, ...] = ("created", "no_final_newline")
# How many folders above a file Kestrel created (the drop-in folder and its parents).
FOLDER_HINT = "created_folders"
MAX_CREATED_FOLDERS = 8

DOCS: tuple[tuple[str, str], ...] = (
    ("Ollama FAQ: configuring the Ollama server", "https://docs.ollama.com/faq"),
    ("Microsoft: setx", "https://learn.microsoft.com/en-us/windows-server/administration/"
     "windows-commands/setx"),
    ("launchctl", "https://ss64.com/mac/launchctl.html"),
    ("systemd: drop-in files", "https://www.freedesktop.org/software/systemd/man/latest/"
     "systemd.unit.html"),
    ("systemctl", "https://www.freedesktop.org/software/systemd/man/latest/systemctl.html"),
)
DOCS_READ = "2026-11-15"

_TIMEOUT = 20
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")
_UNIT_SECONDS = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0,
                 "h": 3600.0}


class EnvChangeFailed(Exception):
    """A system change could not be made. `sentence` is the fixed sentence for the page, chosen
    where the failure is raised; it never carries a command's or the OS's own text."""

    def __init__(self, sentence: str) -> None:
        super().__init__(sentence)
        self.sentence = sentence
        self.record: UndoRecord | None = None
        # True when the write stopped part way, so the page adds "press Undo"; False when the
        # sentence already says everything (an outranked change).
        self.partial = True


class StalePlan(EnvChangeFailed):
    """The digest posted is not the plan or undo record on screen now. Nothing was changed."""


# --------------------------------------------------------------------------- values
def keep_alive_seconds(value: str) -> float | None:
    """Seconds for an OLLAMA_KEEP_ALIVE value, inf for a negative one (kept loaded), None when it
    cannot be read. Formats per the Ollama FAQ: a duration string ("10m", "24h"), a number of
    seconds ("3600"), or any negative number ("-1", "-1m")."""
    text = value.strip()
    if not text:
        return None
    negative = text.startswith("-")
    body = text[1:] if negative else text
    try:
        seconds = float(body)
    except ValueError:
        seconds = 0.0
        rest = body
        while rest:
            m = _DURATION.match(rest)
            if m is None:
                return None
            seconds += float(m.group(1)) * _UNIT_SECONDS[m.group(2)]
            rest = rest[m.end():]
    if negative:
        return float("inf")
    return seconds


@dataclass(frozen=True)
class Row:
    name: str
    value: str | None
    verdict: str  # ok, unset, low, short, unreadable
    sentence: str
    target: str | None  # the value Kestrel would set, None when this row is fine


def judge(name: str, value: str | None, need: int) -> Row:
    """One variable's verdict and the value to set. An unreadable value is offered the
    recommended one with a general sentence, never a specific claim about what it means."""
    if name == PARALLEL:
        target = str(need)
        if value is None:
            return Row(name, None, "unset", f"Not set. Ollama then runs 1 request at a time "
                       f"per model; Kestrel is set to send up to {need}.", target)
        if not value.strip().isdigit():
            return Row(name, value, "unreadable", "Kestrel could not read this value. Setting "
                       f"it to {need} replaces it.", target)
        if int(value) < need:
            return Row(name, value, "low", f"Below the {need} requests Kestrel is set to send "
                       "at once, so some wait in line.", target)
        return Row(name, value, "ok", f"At least {need}, so requests do not wait in line.", None)
    if value is None:
        return Row(name, None, "unset", "Not set. Ollama then unloads a model 5 minutes after "
                   "its last request and reloads it for the next step.", KEEP_ALIVE_VALUE)
    seconds = keep_alive_seconds(value)
    if seconds is None:
        return Row(name, value, "unreadable", "Kestrel could not read this value. Setting it "
                   f"to {KEEP_ALIVE_VALUE} replaces it.", KEEP_ALIVE_VALUE)
    if seconds < SHORT_SECONDS:
        return Row(name, value, "short", "Shorter than an hour, so models can unload between "
                   "steps and load again.", KEEP_ALIVE_VALUE)
    return Row(name, value, "ok", "Long enough that models stay loaded between steps.", None)


_SAFE = re.compile(r"[0-9A-Za-z._-]{1,64}")


def safe_value(value: str) -> bool:
    """An allowlist: 1 to 64 letters, digits, dots, underscores and hyphens. Such a value is one
    plain word in a systemd line, a shell profile, cmd and a POSIX shell alike (no newline to
    start a directive, no quote, `%`, `$`, `&`, `|`, `;`, `<`, `>` or space). Every value Kestrel
    sets passes ("4", "2h"); a stored undo record holding any other value is refused whole."""
    return _SAFE.fullmatch(value) is not None


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class Change:
    name: str
    old: str | None
    new: str


@dataclass(frozen=True)
class Plan:
    mechanism: str
    where: str
    changes: tuple[Change, ...]

    @property
    def digest(self) -> str:
        return _digest({"mechanism": self.mechanism, "where": self.where,
                        "changes": [[c.name, c.old, c.new] for c in self.changes]})


@dataclass(frozen=True)
class UndoRecord:
    """What Kestrel's location held before its first change, so Undo puts it back."""
    mechanism: str
    where: str
    previous: dict[str, str | None]
    set_to: dict[str, str]
    # What the place looked like before Kestrel's first change, beyond the two values (a
    # profile Kestrel created; one that had no final newline), so Undo restores it exactly.
    hints: dict[str, bool | int] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return _digest(self.as_json())

    def as_json(self) -> dict[str, Any]:
        return {"mechanism": self.mechanism, "where": self.where,
                "previous": dict(self.previous), "set_to": dict(self.set_to),
                "hints": dict(self.hints)}

    @classmethod
    def from_json(cls, raw: Any) -> UndoRecord | None:
        """Rebuilt through an allowlist: only the two known names, string values (D-SEC-20)."""
        if not isinstance(raw, dict):
            return None
        mech, where = raw.get("mechanism"), raw.get("where")
        prev, set_to = raw.get("previous"), raw.get("set_to")
        if not (isinstance(mech, str) and isinstance(where, str) and isinstance(prev, dict)
                and isinstance(set_to, dict)):
            return None
        # Each earlier value is None (it was not set) or an allowlisted string. Anything else
        # (a number, a list) is refused with the whole record: turning it into None would make
        # Undo delete a variable that was set (#181 round 5).
        if any(v is not None and not (isinstance(v, str) and safe_value(v))
               for n, v in prev.items() if n in NAMES):
            return None
        previous: dict[str, str | None] = {n: v for n, v in prev.items() if n in NAMES}
        values = {n: v for n, v in set_to.items()
                  if n in NAMES and isinstance(v, str) and safe_value(v)}
        if not values:
            return None
        raw_hints = raw.get("hints")
        hints: dict[str, bool | int] = {}
        if isinstance(raw_hints, dict):
            hints.update({k: v for k, v in raw_hints.items()
                          if k in HINT_KEYS and isinstance(v, bool)})
            folders = raw_hints.get(FOLDER_HINT)
            if (isinstance(folders, int) and not isinstance(folders, bool)
                    and 0 <= folders <= MAX_CREATED_FOLDERS):
                hints[FOLDER_HINT] = folders
        return cls(mech, where, previous, values, hints)


# --------------------------------------------------------------------------- runner
def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Every command this module runs: an argument list, never a shell, UTF-8 text."""
    return subprocess.run(  # noqa: S603 - fixed argument lists, no shell
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=_TIMEOUT, check=False, shell=False,
    )


def _must(argv: list[str], what: str) -> str:
    try:
        done = run(argv)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("ollama_env_command_failed", what=what, error_type=type(exc).__name__)
        raise EnvChangeFailed(f"Could not {what}.") from exc
    if done.returncode != 0:
        log.warning("ollama_env_command_failed", what=what, returncode=done.returncode,
                    stderr=(done.stderr or "")[:300])
        raise EnvChangeFailed(f"Could not {what}.")
    return done.stdout or ""


def _ok(argv: list[str]) -> bool:
    try:
        return run(argv).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# --------------------------------------------------------------------------- backends
@dataclass
class Backend:
    """One operating system's place for the two values. Subclasses implement read and write;
    `owned` is what Kestrel's own location holds (the undo point), `read` what Ollama would
    see there."""
    mechanism: str = ""
    where: str = ""
    can_set: bool = True
    why_not: str = ""
    can_restart: bool = False
    restart_text: str = ""
    persistence: str = ""
    undo_text: str = ""
    cleared: str = ""  # where an Undo to "not set" removes the value from, for the note
    extra: dict[str, Any] = field(default_factory=dict)

    def read(self, name: str) -> str | None:
        return self.owned().get(name)

    def unrestorable(self) -> set[str]:
        """Names whose current value Undo could not put back exactly (none by default)."""
        return set()

    def blocked(self) -> str:
        """Why Kestrel will not change this place right now ("" when it may)."""
        return "" if self.can_set else self.why_not

    def hints(self) -> dict[str, bool | int]:
        """The state Undo needs beyond the two values (none by default)."""
        return {}

    def restore(self, values: dict[str, str | None], hints: dict[str, bool | int]) -> None:
        """Undo's write: the values, plus whatever the hints say to put back."""
        self.write(values)

    def owned(self) -> dict[str, str | None]:
        raise NotImplementedError

    def write(self, values: dict[str, str | None]) -> None:
        """Make each name hold its value at this location (None removes it)."""
        raise NotImplementedError

    def restart(self) -> None:
        raise EnvChangeFailed("Kestrel cannot restart Ollama here.")

    def commands(self, values: dict[str, str | None]) -> list[str]:
        raise NotImplementedError

    def restart_commands(self) -> list[str]:
        return []


class WindowsBackend(Backend):
    """HKEY_CURRENT_USER\\Environment, through winreg, then the WM_SETTINGCHANGE broadcast that
    setx also sends, so programs started from the Start menu afterwards see the change."""

    def __init__(self) -> None:
        super().__init__(
            mechanism="windows_user_env",
            where="your Windows user environment variables (HKEY_CURRENT_USER\\Environment)",
            restart_text="Quit Ollama from its icon in the taskbar notification area, then "
            "start it again from the Start menu. Kestrel does not close Ollama on Windows.",
            persistence="This stays set after you restart Windows.",
            undo_text="press Undo here, or open Edit environment variables for your account "
            "and put back each value shown as now (delete one that was not set)",
            cleared="your user environment variables",
        )
        self._kinds: dict[str, int] = {}

    def owned(self) -> dict[str, str | None]:
        return {n: self._reg_get(n) for n in NAMES}

    def read(self, name: str) -> str | None:
        """What Ollama inherits: the user value wins over the system one (Ollama FAQ: "Ollama
        inherits your user and system environment variables")."""
        user = self._reg_get(name)
        return user if user is not None else self._reg_get_machine(name)

    def unrestorable(self) -> set[str]:
        # Undo writes REG_SZ; any other registry type would come back changed.
        return {n for n, kind in self._kinds.items() if kind != 1}

    def write(self, values: dict[str, str | None]) -> None:
        for name, value in values.items():
            if value is None:
                self._reg_delete(name)
            else:
                self._reg_set(name, value)
        self._broadcast()

    def commands(self, values: dict[str, str | None]) -> list[str]:
        return [f"setx {n} {v}" if v is not None
                else f'reg delete "HKCU\\Environment" /v {n} /f' for n, v in values.items()]

    # The registry calls, apart so a test on any OS can replace them.
    def _reg_get(self, name: str) -> str | None:  # pragma: no cover - Windows only
        if sys.platform != "win32":
            return None
        import winreg

        self._kinds.pop(name, None)
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, kind = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None
        except OSError as exc:
            log.warning("ollama_env_registry_read_failed", name=name, error=str(exc))
            return None
        self._kinds[name] = int(kind)
        return str(value)

    def _reg_get_machine(self, name: str) -> str | None:  # pragma: no cover - Windows only
        if sys.platform != "win32":
            return None
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, "SYSTEM\\CurrentControlSet\\Control"
                                "\\Session Manager\\Environment") as key:
                value, _kind = winreg.QueryValueEx(key, name)
        except OSError:
            return None
        return str(value)

    def _reg_set(self, name: str, value: str) -> None:  # pragma: no cover - Windows only
        if sys.platform != "win32":
            raise EnvChangeFailed("Could not save your Windows environment variables.")
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        except OSError as exc:
            log.warning("ollama_env_registry_write_failed", name=name, error=str(exc))
            raise EnvChangeFailed("Could not save your Windows environment variables.") from exc

    def _reg_delete(self, name: str) -> None:  # pragma: no cover - Windows only
        if sys.platform != "win32":
            raise EnvChangeFailed("Could not save your Windows environment variables.")
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("ollama_env_registry_delete_failed", name=name, error=str(exc))
            raise EnvChangeFailed("Could not save your Windows environment variables.") from exc

    def _broadcast(self) -> None:  # pragma: no cover - Windows only
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        result = wintypes.DWORD()
        ok = ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
            0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))
        if not ok:
            log.info("ollama_env_broadcast_unanswered")


class MacBackend(Backend):
    """`launchctl setenv` for apps launchd starts, the Ollama FAQ's mechanism for the app."""

    def __init__(self, home: Path | None = None) -> None:
        home = home or Path.home()
        app = next((p for p in (Path("/Applications/Ollama.app"),
                                home / "Applications" / "Ollama.app") if p.exists()), None)
        super().__init__(
            mechanism="macos_launchctl",
            where="the launchd environment for apps you open (launchctl setenv)",
            can_restart=app is not None,
            restart_text=("Kestrel can quit the Ollama app and open it again." if app else
                          "Quit Ollama from its menu bar icon, then open it again. If you start "
                          "Ollama from a terminal instead, set the values in that shell."),
            persistence="This lasts until you restart your Mac. After a restart, this section "
            "shows the values as not set again, and the button sets them again.",
            undo_text="press Undo here, or run the undo commands under Show details",
            cleared="the launchd environment",
        )

    def owned(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for name in NAMES:
            try:
                done = run(["launchctl", "getenv", name])
            except (OSError, subprocess.TimeoutExpired):
                out[name] = None
                continue
            value = (done.stdout or "").strip() if done.returncode == 0 else ""
            out[name] = value or None
        return out

    def write(self, values: dict[str, str | None]) -> None:
        for name, value in values.items():
            argv = (["launchctl", "unsetenv", name] if value is None
                    else ["launchctl", "setenv", name, value])
            _must(argv, "change the launchd environment")

    def commands(self, values: dict[str, str | None]) -> list[str]:
        return [shlex.join(["launchctl", "setenv", n, v]) if v is not None
                else shlex.join(["launchctl", "unsetenv", n]) for n, v in values.items()]

    def restart(self) -> None:
        if not self.can_restart:
            raise EnvChangeFailed("Kestrel cannot restart Ollama here.")
        if not self._app_running():
            # Ollama may be running another way (brew services, ollama serve): opening the app
            # then would start a second server, so Kestrel leaves it alone.
            raise EnvChangeFailed("The Ollama app is not open, so Kestrel did not restart it. "
                                  "If you run Ollama another way, restart it there.")
        _must(["osascript", "-e", 'tell application "Ollama" to quit'], "quit Ollama")
        deadline = time.monotonic() + 15
        while self._app_running():
            if time.monotonic() > deadline:
                raise EnvChangeFailed("Ollama did not quit. Quit it from the menu bar, then "
                                      "open it again.")
            time.sleep(0.5)
        _must(["open", "-a", "Ollama"], "open Ollama again")

    def _app_running(self) -> bool:
        return _ok(["pgrep", "-x", "Ollama"])

    def restart_commands(self) -> list[str]:
        return ["osascript -e 'tell application \"Ollama\" to quit'", "open -a Ollama"]


# Drop-ins apply in name order and a later Environment= wins; "zz-" sorts after override.conf
# and numbered files. apply() still re-reads the effective value and says so if it is outranked.
_DROPIN_NAME = "zz-kestrel-ollama-env.conf"
_BLOCK_START = "# >>> Kestrel: Ollama settings (Settings > This computer; Undo there) >>>"
_BLOCK_END = "# <<< Kestrel: Ollama settings <<<"
_BLOCK_LINE = re.compile(r"export (OLLAMA_NUM_PARALLEL|OLLAMA_KEEP_ALIVE)=([0-9A-Za-z._-]{1,64})")


def _config_home(home: Path) -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg and os.path.isabs(xdg) else home / ".config"


def _write_text(path: Path, text: str) -> None:
    """Replace a file in one step, keeping its permission bits. A symlinked file (a dotfiles
    manager's ~/.profile) is written at its target, so the link itself is kept."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".kestrel")
    try:
        # Absent on Windows before Python 3.13. Only the macOS and Linux backends write files
        # here, and a missing call must not turn a write into a crash.
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# Every error a read of the person's own file can raise: a missing permission, a symlink loop
# (RuntimeError from resolve), bytes that are not UTF-8 (a ValueError). Each becomes a blocked
# state with a general sentence, never a crash (#181 rounds 3 and 4).
_READ_ERRORS: tuple[type[BaseException], ...] = (OSError, RuntimeError, ValueError)


def _cannot_read(path: Path) -> str:
    return (f"Kestrel could not read {path} as plain text, so it does not change it. The "
            "commands are under Show details.")


def _read_place(path: Path) -> tuple[str | None, str]:
    """(the file's text exactly as stored, or None when it does not exist; a problem). The one
    reader for every file Kestrel may change: ~/.profile and the systemd drop-in."""
    try:
        if not path.exists():
            return None, ""
        return path.read_bytes().decode("utf-8"), ""
    except _READ_ERRORS as exc:
        log.info("ollama_env_file_unreadable", error_type=type(exc).__name__)
        return None, _cannot_read(path)


def _not_writable(path: Path) -> str:
    """A sentence when Kestrel may not replace this file in place, else ""."""
    try:
        target = path.resolve()
        folder = target.parent
        while not folder.exists() and folder != folder.parent:
            folder = folder.parent  # the drop-in folder may not exist yet; mkdir needs its parent
        writable = os.access(folder, os.W_OK) and (
            not target.exists() or os.access(target, os.W_OK))
    except _READ_ERRORS as exc:
        log.info("ollama_env_file_unreadable", error_type=type(exc).__name__)
        return _cannot_read(path)
    if not writable:
        return (f"{path} is read-only, so Kestrel does not change it. The commands are under "
                "Show details.")
    return ""


def _missing_folders(path: Path) -> int:
    """How many folders above `path` do not exist yet (Kestrel would create them)."""
    n = 0
    folder = path.parent
    try:
        while not folder.exists() and folder != folder.parent and n < MAX_CREATED_FOLDERS:
            n += 1
            folder = folder.parent
    except _READ_ERRORS:
        return 0
    return n


def _remove_created_folders(path: Path, count: int) -> None:
    """Remove up to `count` folders above `path`, bottom up, each only while empty. A folder
    that holds anything (another drop-in, the person's own file) stops it."""
    folder = path.parent
    for _ in range(min(count, MAX_CREATED_FOLDERS)):
        try:
            folder.rmdir()
        except OSError:
            return  # not empty, or already gone: leave it and everything above it
        folder = folder.parent


_DROPIN_HEADER: tuple[str, ...] = (
    "# Written by Kestrel (Settings > This computer). Press Undo there, or",
    "# delete this file and run: systemctl --user daemon-reload",
    "[Service]",
)
_DROPIN_LINE = re.compile(
    r'Environment="(OLLAMA_NUM_PARALLEL|OLLAMA_KEEP_ALIVE)=([0-9A-Za-z._-]{1,64})"')


class LinuxUserServiceBackend(Backend):
    """A drop-in for the user's own `ollama.service` (systemctl --user). Kestrel changes the
    file only when it is exactly Kestrel's own: its header, then only allowlisted Environment
    lines for the two names. Any other content (the person's own settings in a file of the same
    name) blocks Set and Undo; nothing in it is rewritten or deleted (#181 round 4)."""

    def __init__(self, home: Path | None = None) -> None:
        home = home or Path.home()
        self.path = _config_home(home) / "systemd" / "user" / "ollama.service.d" / _DROPIN_NAME
        super().__init__(
            mechanism="linux_systemd_user",
            where=f"a settings file for your ollama.service ({self.path})",
            can_restart=True,
            restart_text="Kestrel can restart your ollama.service.",
            persistence="This stays set after you restart the computer.",
            undo_text=f"press Undo here, or delete {self.path} and run "
            "systemctl --user daemon-reload",
            cleared="Kestrel's settings file (your own ollama.service settings apply again)",
        )

    def read(self, name: str) -> str | None:
        try:
            done = run(["systemctl", "--user", "show", "ollama.service",
                        "--property=Environment", "--value"])
        except (OSError, subprocess.TimeoutExpired):
            return self.owned().get(name)
        if done.returncode != 0:
            return self.owned().get(name)
        try:
            parts = shlex.split(done.stdout or "")
        except ValueError:
            return self.owned().get(name)
        found = dict(p.split("=", 1) for p in parts if "=" in p)
        return found.get(name)

    def _parse(self) -> tuple[bool, dict[str, str], str]:
        """(the file exists, its values, a problem)."""
        text, problem = _read_place(self.path)
        if problem:
            return True, {}, problem
        if text is None:
            return False, {}, ""
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        foreign = (f"{self.path} holds settings Kestrel did not write, so Kestrel does not "
                   "change it. The commands are under Show details.")
        if tuple(lines[:len(_DROPIN_HEADER)]) != _DROPIN_HEADER:
            return True, {}, foreign
        values: dict[str, str] = {}
        for line in lines[len(_DROPIN_HEADER):]:
            m = _DROPIN_LINE.fullmatch(line)
            if m is None or m.group(1) in values:
                return True, {}, foreign
            values[m.group(1)] = m.group(2)
        return True, values, ""

    def blocked(self) -> str:
        return self._parse()[2] or _not_writable(self.path)

    def hints(self) -> dict[str, bool | int]:
        return {"created": not self._parse()[0], FOLDER_HINT: _missing_folders(self.path)}

    def owned(self) -> dict[str, str | None]:
        out: dict[str, str | None] = dict.fromkeys(NAMES)
        out.update(self._parse()[1])
        return out

    def _text(self, values: dict[str, str]) -> str:
        lines = list(_DROPIN_HEADER) + [f'Environment="{n}={v}"' for n, v in values.items()]
        return "\n".join(lines) + "\n"

    def write(self, values: dict[str, str | None]) -> None:
        self.restore(values, {})

    def restore(self, values: dict[str, str | None], hints: dict[str, bool | int]) -> None:
        exists, current, problem = self._parse()
        problem = problem or _not_writable(self.path)
        if problem:
            raise EnvChangeFailed(problem)
        keep = {n: v for n, v in {**current, **values}.items() if v is not None}
        if not all(safe_value(v) for v in keep.values()):
            raise EnvChangeFailed("Kestrel did not write a value it could not check.")
        try:
            if keep or (exists and not hints.get("created")):
                # A Kestrel file that was there before Kestrel's first change stays, emptied.
                _write_text(self.path, self._text(keep))
            elif exists:
                self.path.unlink()  # Undo of a file Kestrel created
                _remove_created_folders(self.path, int(hints.get(FOLDER_HINT, 0)))
        except _READ_ERRORS as exc:
            log.warning("ollama_env_dropin_write_failed", error_type=type(exc).__name__)
            raise EnvChangeFailed("Could not save the ollama.service settings file.") from exc
        _must(["systemctl", "--user", "daemon-reload"], "reload your systemd settings")

    def commands(self, values: dict[str, str | None]) -> list[str]:
        keep = {n: v for n, v in values.items() if v is not None}
        if not keep:
            return [shlex.join(["rm", str(self.path)]), "systemctl --user daemon-reload"]
        return [f"mkdir -p {shlex.quote(str(self.path.parent))}",
                f"printf '%s\\n' {' '.join(shlex.quote(x) for x in self._text(keep).splitlines())}"
                f" > {shlex.quote(str(self.path))}",
                "systemctl --user daemon-reload"]

    def restart(self) -> None:
        _must(["systemctl", "--user", "restart", "ollama.service"], "restart ollama.service")

    def restart_commands(self) -> list[str]:
        return ["systemctl --user restart ollama.service"]


class LinuxSystemServiceBackend(Backend):
    """A system-wide `ollama.service`: changing it needs an administrator, so Kestrel only shows
    the FAQ's commands and never runs sudo (D-SETUP-653)."""

    def __init__(self) -> None:
        super().__init__(
            mechanism="linux_systemd_system",
            where="the system ollama.service",
            can_set=False,
            why_not="Ollama runs as a system service on this computer. Changing it needs an "
            "administrator password, so Kestrel does not change it. The commands are under "
            "Show details.",
            restart_text="After the change, run: sudo systemctl restart ollama",
        )

    def owned(self) -> dict[str, str | None]:
        found: dict[str, str | None] = dict.fromkeys(NAMES)
        try:
            done = run(["systemctl", "show", "ollama.service", "--property=Environment",
                        "--value"])
            parts = shlex.split(done.stdout or "") if done.returncode == 0 else []
        except (OSError, subprocess.TimeoutExpired, ValueError):
            parts = []
        for p in parts:
            key, _, val = p.partition("=")
            if key in NAMES:
                found[key] = val
        return found

    def write(self, values: dict[str, str | None]) -> None:
        raise EnvChangeFailed("Kestrel does not change a system service.")

    def commands(self, values: dict[str, str | None]) -> list[str]:
        lines = [f'Environment="{n}={v}"' for n, v in values.items() if v is not None]
        return ["sudo systemctl edit ollama.service",
                "# add under [Service]: " + "  ".join(lines),
                "sudo systemctl daemon-reload", "sudo systemctl restart ollama"]


class LinuxProfileBackend(Backend):
    """A marked block in ~/.profile, for an `ollama serve` started from a login shell."""

    def __init__(self, home: Path | None = None) -> None:
        self.path = (home or Path.home()) / ".profile"
        super().__init__(
            mechanism="linux_profile",
            where=f"your shell profile ({self.path})",
            restart_text="Stop ollama serve, then start it again from a new terminal, or sign "
            "out and back in. If your shell does not read ~/.profile (for example zsh, or bash "
            "with a ~/.bash_profile), add the lines under Show details to the file it reads.",
            persistence="This stays set after you restart the computer.",
            undo_text=f"press Undo here, or delete the Kestrel lines from {self.path}",
            cleared="Kestrel's lines in your shell profile",
        )

    def _parse(self) -> tuple[str, dict[str, str], str]:
        """(the profile without Kestrel's block, the block's values, a problem). Kestrel's block
        is recognized only exactly: one start line, one end line after it, and between them
        nothing but `export NAME=value` lines for the two names with allowlisted values. Any
        other shape (a marker removed or edited, a line added inside) is a problem, and then
        Kestrel neither reads nor changes the file (#181 review, D-SETUP-660)."""
        text, problem = self._text()
        if problem:
            return "", {}, problem
        lines = text.splitlines(keepends=True)
        bare = [line.rstrip("\r\n") for line in lines]
        starts = [i for i, s in enumerate(bare) if s == _BLOCK_START]
        ends = [i for i, s in enumerate(bare) if s == _BLOCK_END]
        marked = [i for i, s in enumerate(bare) if "Kestrel: Ollama settings" in s]
        if not marked:
            return text, {}, ""
        damaged = (f"Kestrel's lines in {self.path} were changed by hand, so Kestrel does not "
                   "edit that file. Fix or remove them yourself, or use the commands under Show "
                   "details.")
        if (len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]
                or sorted(marked) != [starts[0], ends[0]]):
            return text, {}, damaged
        values: dict[str, str] = {}
        for s in bare[starts[0] + 1:ends[0]]:
            m = _BLOCK_LINE.fullmatch(s)
            if m is None or m.group(1) in values:
                return text, {}, damaged
            values[m.group(1)] = m.group(2)
        rest = "".join(lines[:starts[0]] + lines[ends[0] + 1:])
        return rest, values, ""

    def _text(self) -> tuple[str, str]:
        """The profile exactly as stored (line endings kept), or a problem (#181 round 3)."""
        text, problem = _read_place(self.path)
        return text or "", problem

    def blocked(self) -> str:
        return self._parse()[2] or _not_writable(self.path)

    def hints(self) -> dict[str, bool | int]:
        text, _problem = _read_place(self.path)
        if text is None:
            return {"created": True, "no_final_newline": False}
        rest = self._parse()[0]
        return {"created": False,
                "no_final_newline": bool(rest) and not rest.endswith(("\n", "\r"))}

    def read(self, name: str) -> str | None:
        value = self.owned().get(name)
        return value if value is not None else os.environ.get(name)

    def owned(self) -> dict[str, str | None]:
        out: dict[str, str | None] = dict.fromkeys(NAMES)
        out.update(self._parse()[1])
        return out

    def _block(self, values: dict[str, str], nl: str = "\n") -> str:
        lines = [_BLOCK_START] + [f"export {n}={v}" for n, v in values.items()]
        return nl.join(lines + [_BLOCK_END]) + nl

    def write(self, values: dict[str, str | None]) -> None:
        self.restore(values, {})

    def restore(self, values: dict[str, str | None], hints: dict[str, bool | int]) -> None:
        """Every other byte of the profile is kept: its line endings, and (with Undo's hints)
        a missing final newline and whether the file existed before Kestrel's first change."""
        rest, current, problem = self._parse()
        problem = problem or _not_writable(self.path)
        if problem:
            raise EnvChangeFailed(problem)
        keep = {n: v for n, v in {**current, **values}.items() if v is not None}
        if not all(safe_value(v) for v in keep.values()):
            raise EnvChangeFailed("Kestrel did not write a value it could not check.")
        nl = "\r\n" if "\r\n" in rest else "\n"
        if keep:
            if rest and not rest.endswith(("\n", "\r")):
                rest += nl
            rest += self._block(keep, nl)
        elif hints.get("no_final_newline") and rest.endswith(nl):
            rest = rest[: -len(nl)]  # the newline Kestrel added before its block
        try:
            if rest or not self.path.exists():
                if rest:
                    _write_text(self.path, rest)
            elif hints.get("created"):
                # Kestrel created this profile: Undo removes it. Only then (#181 round 3).
                self.path.resolve().unlink()
            else:
                _write_text(self.path, "")
        except _READ_ERRORS as exc:
            log.warning("ollama_env_profile_write_failed", error_type=type(exc).__name__)
            raise EnvChangeFailed("Could not save your shell profile.") from exc

    def commands(self, values: dict[str, str | None]) -> list[str]:
        keep = {n: v for n, v in values.items() if v is not None}
        if not keep:
            return [f"# delete the Kestrel lines from {self.path}"]
        return [f"# add to {self.path}:"] + self._block(keep).splitlines()


def detect(platform: str | None = None, home: Path | None = None) -> Backend:
    """The backend for this computer. On Linux, a user ollama.service wins, then a system one,
    then the shell profile."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        return WindowsBackend()
    if platform == "darwin":
        return MacBackend(home)
    if _ok(["systemctl", "--user", "cat", "ollama.service"]):
        return LinuxUserServiceBackend(home)
    if _ok(["systemctl", "cat", "ollama.service"]):
        return LinuxSystemServiceBackend()
    return LinuxProfileBackend(home)


_override: list[Backend] = []


def backend() -> Backend:
    """The one accessor every read and write goes through (tests replace it)."""
    return _override[-1] if _override else detect()


# --------------------------------------------------------------------------- assessment
@dataclass(frozen=True)
class Assessment:
    shown: bool
    hidden_reason: str
    need: int
    rows: tuple[Row, ...]
    backend: Backend | None
    plan: Plan | None
    undo: UndoRecord | None
    apply_commands: tuple[str, ...] = ()
    undo_commands: tuple[str, ...] = ()
    blocked: str = ""  # why Set them and Undo are not offered here now ("" when they are)


def need_for(settings: Any) -> int:
    c = settings.concurrency
    return max(int(c.tier1), int(c.tier2), int(c.embed))


def _guarded(fn):
    """Every entry point (assess, apply, undo, restart) turns a read or write error that a
    backend did not already name into one general EnvChangeFailed, so no file or OS surprise
    ever reaches a page or the doctor as a crash (#181 round 4)."""

    @functools.wraps(fn)
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except EnvChangeFailed:
            raise
        except (*_READ_ERRORS, subprocess.SubprocessError) as exc:
            log.warning("ollama_env_unexpected_error", step=fn.__name__,
                        error_type=type(exc).__name__)
            raise EnvChangeFailed("Kestrel could not read or change Ollama's settings on this "
                                  "computer. The commands are under Show details.") from exc

    return inner


@_guarded
def assess(settings: Any, stored_undo: Any, ollama_url: str | None = None) -> Assessment:
    """What the section shows. Reads only: nothing here writes."""
    from kestrel.llm.profiles import ollama_needed
    from kestrel.services.setup_state import ollama_remote_host

    need = need_for(settings)
    if not ollama_needed(settings):
        return Assessment(False, "No step runs on Ollama.", need, (), None, None, None)
    host = ollama_remote_host(ollama_url)
    if host:
        return Assessment(False, f"Ollama runs on {host}; set these on that computer.", need,
                          (), None, None, None)
    be = backend()
    rows = tuple(judge(n, be.read(n), need) for n in NAMES)
    changes = tuple(Change(r.name, r.value, r.target) for r in rows if r.target is not None)
    plan = Plan(be.mechanism, be.where, changes) if changes else None
    undo = UndoRecord.from_json(stored_undo)
    if undo is not None and (undo.mechanism != be.mechanism or undo.where != be.where):
        undo = None  # a record from another computer or account (a restored backup)
    apply_cmds = tuple(be.commands({c.name: c.new for c in changes})) if changes else ()
    undo_cmds = tuple(be.commands(dict(undo.previous))) if undo else ()
    return Assessment(True, "", need, rows, be, plan, undo, apply_cmds, undo_cmds,
                      be.blocked())


@_guarded
def apply(settings: Any, stored_undo: Any, digest: str,
          ollama_url: str | None = None) -> tuple[Plan, UndoRecord]:
    """Set exactly the plan whose digest the person confirmed. Returns the plan and the undo
    record to store, which keeps the values from before Kestrel's first change. Raises StalePlan
    (nothing changed) when the digest is not the plan on screen now."""
    now = assess(settings, stored_undo, ollama_url)
    if now.plan is None or now.backend is None or not digest or digest != now.plan.digest:
        raise StalePlan("The settings changed since you looked. Review them again.")
    be = now.backend
    if now.blocked:
        raise EnvChangeFailed(now.blocked)
    owned = be.owned()
    if (any(v is not None and not safe_value(v) for v in owned.values())
            or be.unrestorable() & {c.name for c in now.plan.changes}):
        # Undo could not store this value safely, so nothing is changed (uncertain: general).
        raise EnvChangeFailed("Kestrel could not store the current values safely for Undo, so "
                              "it did not change them. The commands are under Show details.")
    earlier = now.undo
    previous = dict(earlier.previous) if earlier else {}
    set_to = dict(earlier.set_to) if earlier else {}
    for c in now.plan.changes:
        previous.setdefault(c.name, owned.get(c.name))
        set_to[c.name] = c.new
    hints = dict(earlier.hints) if earlier else be.hints()
    record = UndoRecord(be.mechanism, be.where, previous, set_to, hints)
    try:
        be.write({c.name: c.new for c in now.plan.changes})
    except EnvChangeFailed as exc:
        # Some values may already be written (one registry value of two). The record covers
        # what this place holds now: names written here plus any earlier change, never a name
        # that was not written, so Undo never calls an unwritten one "changed since".
        exc.record = _partial_record(be, earlier, owned, now.plan.changes, hints)
        raise
    outranked = [c.name for c in now.plan.changes if be.read(c.name) != c.new]
    if outranked:
        exc = EnvChangeFailed("Kestrel saved them, but another setting on this computer still "
                              "gives Ollama a different value. Undo puts back what was there; "
                              "the commands are under Show details.")
        exc.record = record
        exc.partial = False
        raise exc
    log.info("ollama_env_set", mechanism=be.mechanism, names=[c.name for c in now.plan.changes])
    return now.plan, record


def _partial_record(be: Backend, earlier: UndoRecord | None, owned: dict[str, str | None],
                    changes: tuple[Change, ...], hints: dict[str, bool | int]) -> UndoRecord | None:
    try:
        after = be.owned()
    except (OSError, EnvChangeFailed):
        after = None  # unknown: count every name as possibly written, so Undo is offered
    previous = dict(earlier.previous) if earlier else {}
    set_to = dict(earlier.set_to) if earlier else {}
    for c in changes:
        if after is None or after.get(c.name) == c.new:
            previous.setdefault(c.name, owned.get(c.name))
            set_to[c.name] = c.new
    if not set_to:
        return None
    return UndoRecord(be.mechanism, be.where, previous, set_to, hints)


@_guarded
def undo(stored_undo: Any, digest: str) -> tuple[UndoRecord, list[str]]:
    """Put back what the record says was there before, for each name that still holds the value
    Kestrel set; a name changed since (by the person, elsewhere) is left alone and returned in
    the skipped list. Raises StalePlan when the digest is not the stored record's."""
    record = UndoRecord.from_json(stored_undo)
    be = backend()
    if (record is None or record.mechanism != be.mechanism or record.where != be.where
            or digest != record.digest):
        raise StalePlan("There is nothing to undo, or it changed since you looked.")
    problem = be.blocked()
    if problem:
        raise EnvChangeFailed(problem)
    owned = be.owned()
    back = {n: v for n, v in record.previous.items() if owned.get(n) == record.set_to.get(n)}
    # A name already back at its earlier value (a retried Undo) is done, not "changed since".
    done = {n for n, v in record.previous.items() if n not in back and owned.get(n) == v}
    skipped = sorted(n for n in record.previous if n not in back and n not in done)
    if back:
        be.restore(back, record.hints)
    log.info("ollama_env_undone", mechanism=be.mechanism, names=sorted(back), skipped=skipped)
    return record, skipped


@_guarded
def restart() -> None:
    be = backend()
    if not be.can_restart:
        raise EnvChangeFailed("Kestrel cannot restart Ollama on this computer.")
    be.restart()
    log.info("ollama_env_restarted", mechanism=be.mechanism)
