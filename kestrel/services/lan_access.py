"""Turning LAN access on from Settings (#27, D-SEC-3).

`server.bind_all` is read from the settings file plus the environment, never from the database,
by the code that picks the bind address (kestrel.web.serve.bind_host) and by the Host and Origin
guards (kestrel.web.app). The bind is chosen before any database session exists, so a database
row could not take effect without a restart anyway, and wiring only some readers to it would leave
LAN mode half applied. So this module writes the one place all of them already read: the user's own
settings.yml. The edit is textual and touches one key, so every comment in the file survives, and it
is checked before it is trusted: the edited text must parse to exactly the old data plus the new
value, or nothing is written. The write is a temp file, fsync, then an atomic replace.

Nothing here makes a LAN bind safe by itself. The Settings route requires a shared secret and the
user's acknowledgement before it enables, and only this machine may call it."""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass
from pathlib import Path

import structlog
import yaml

from kestrel import paths
from kestrel.errors import UserFacingError

log = structlog.get_logger()

ENV_NAME = "KESTREL_SERVER__BIND_ALL"
_EDITING = "settings.yml.editing"
_TOP_KEY = re.compile(r"^(\S[^:#]*?)\s*:(.*)$")
_BIND_LINE = re.compile(
    r"^(?P<indent>[ \t]+)bind_all[ \t]*:[ \t]*(?P<value>[^#\r\n]*?)(?P<tail>[ \t]*(#.*)?)$"
)
_FLOW_BIND = re.compile(r"bind_all[ \t]*:[ \t]*[^,}\s]+")


class LanAccessError(UserFacingError, ValueError):
    """The settings file could not be changed safely. Nothing was written."""


def user_settings_path() -> Path:
    return paths.config_dir() / "settings.yml"


def _word(value: bool) -> str:
    return "true" if value else "false"


def _read_text() -> str:
    """The user's settings.yml, else the packaged default it would be seeded from."""
    target = user_settings_path()
    source = target if target.exists() else paths.packaged_config() / "settings.yml"
    try:
        return Path(str(source)).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LanAccessError(f"settings.yml could not be read: {type(exc).__name__}.") from exc


def _edit(text: str, value: bool) -> str:
    """`text` with server.bind_all set, changing nothing else. Raises LanAccessError for a shape
    this does not handle (the caller then writes nothing)."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, ln in enumerate(lines) if ln.startswith("server") and _is_server_key(ln)), None
    )
    word = _word(value)
    if start is None:
        sep = "" if (not text or text.endswith(("\n", "\r"))) else nl
        return f"{text}{sep}{nl}server:{nl}  bind_all: {word}{nl}"
    key_match = _TOP_KEY.match(lines[start].rstrip("\r\n"))
    rest = (key_match.group(2) if key_match else "").split("#", 1)[0].strip()
    if rest.startswith("{"):  # flow style: server: {host: x, bind_all: false}
        if _FLOW_BIND.search(lines[start]):
            lines[start] = _FLOW_BIND.sub(f"bind_all: {word}", lines[start], count=1)
        elif rest == "{}":
            lines[start] = lines[start].replace("{}", f"{{bind_all: {word}}}", 1)
        else:
            lines[start] = lines[start].replace("{", f"{{bind_all: {word}, ", 1)
        return "".join(lines)
    if rest:
        raise LanAccessError("The server section of settings.yml has a shape Kestrel cannot edit.")
    child_indent = "  "
    end = start + 1
    while end < len(lines):
        ln = lines[end]
        if ln.strip() == "" or ln.lstrip().startswith("#") or ln[0] in " \t":
            m = _BIND_LINE.match(ln.rstrip("\r\n"))
            if m:
                eol = ln[len(ln.rstrip("\r\n")):]
                lines[end] = f"{m['indent']}bind_all: {word}{m['tail']}{eol}"
                return "".join(lines)
            if ln.strip() and not ln.lstrip().startswith("#"):
                child_indent = ln[: len(ln) - len(ln.lstrip())]
            end += 1
            continue
        break
    lines.insert(start + 1, f"{child_indent}bind_all: {word}{nl}")
    return "".join(lines)


def _is_server_key(line: str) -> bool:
    m = _TOP_KEY.match(line.rstrip("\r\n"))
    return bool(m) and m.group(1).strip().strip("\"'") == "server"


def _fsync_dir(directory: Path) -> None:
    if os.name == "nt":  # Windows cannot open a directory for fsync
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        log.warning("settings_dir_fsync_failed", error_type=type(exc).__name__)


def set_bind_all(value: bool) -> Path:
    """Write server.bind_all into the user's settings.yml and return its path. A missing user file
    is first seeded from the packaged default. Raises LanAccessError and writes nothing when the
    file cannot be read, is not a mapping, or the edit does not verify."""
    text = _read_text()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise LanAccessError("settings.yml is not valid YAML, so Kestrel left it alone.") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise LanAccessError("settings.yml is not a mapping, so Kestrel left it alone.")
    new_text = _edit(text, value)
    expected = copy.deepcopy(data)
    if not isinstance(expected.get("server"), dict):
        expected["server"] = {}
    expected["server"]["bind_all"] = value
    try:
        verified = yaml.safe_load(new_text) == expected
    except yaml.YAMLError:
        verified = False
    if not verified:  # a bug or an exotic file: never write what was not checked
        raise LanAccessError("Kestrel could not change settings.yml safely and left it alone.")

    paths.ensure_dirs()
    target = user_settings_path()
    tmp = target.with_name(_EDITING)
    try:
        with open(tmp, "wb") as fh:
            fh.write(new_text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        log.warning("settings_write_failed", error_type=type(exc).__name__)
        raise LanAccessError(
            f"settings.yml could not be written ({type(exc).__name__}). Nothing was changed."
        ) from exc
    _fsync_dir(target.parent)
    log.info("lan_bind_all_written", bind_all=value)
    return target


# ----------------------------------------------------------------------------------- the state
@dataclass(frozen=True)
class LanState:
    file_on: bool  # what settings.yml says
    effective_on: bool  # what get_settings() resolves to (the environment can override the file)
    running_on: bool | None  # what this process started with; None when unknown
    env_name: str | None  # the overriding environment variable, if one is set
    env_value: str | None
    has_secret: bool

    @property
    def restart_needed(self) -> bool:
        return self.running_on is not None and self.running_on != self.effective_on


def env_override() -> tuple[str, str] | None:
    """The environment variable that overrides the file, as (name, value), if it is set."""
    for key, val in os.environ.items():
        if key.upper() == ENV_NAME:
            return key, val
    return None


def file_value() -> bool:
    try:
        data = yaml.safe_load(_read_text())
    except (LanAccessError, yaml.YAMLError):
        return False
    server = data.get("server") if isinstance(data, dict) else None
    return bool(server.get("bind_all")) if isinstance(server, dict) else False


def state(*, running_on: bool | None, has_secret: bool) -> LanState:
    from kestrel.config import get_settings

    effective = bool(get_settings().server.bind_all)  # also loads .env into the environment
    env = env_override()
    return LanState(
        file_on=file_value(), effective_on=effective, running_on=running_on,
        env_name=env[0] if env else None, env_value=env[1] if env else None,
        has_secret=has_secret,
    )
