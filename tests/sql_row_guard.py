"""A runtime guard on who touches the install ID's table (D-INST-15, D-INST-16): by call site.

The install ID lives in its own one-row table, `install_identity`, which only
kestrel/services/install_id.py reads or writes. No settings reader can reach it because it is not
in their table, so this guard does not inspect any value or any SQL. It uses SQLite's own
authorizer: every statement that reads a column of `install_identity`, or inserts, updates or
deletes its rows, is allowed only when the innermost frame of this repository's own code (kestrel
or a test, never a library) is in the install ID module, in migration 0041 (the one-time
move), or one of the named functions in ALLOWED. A read through the install ID module counts
only when every frame of the app outside it is a function in CALLERS.

Every connection is guarded:
- `sqlite3.connect` and `sqlite3.dbapi2.connect` (SQLAlchemy's pysqlite dialect looks it up in
  dbapi2; aiosqlite and plain code use sqlite3) open a GuardedConnection that installs the
  authorizer as it is created, on the thread that will use it, with statement caching off so
  every statement is prepared, and authorized, every time it runs;
- a pool "connect" listener on every SQLAlchemy engine (async and sync, the doctor's included)
  checks that the driver connection under it is a GuardedConnection, and records any that is not,
  so an engine that opens connections another way fails the test instead of passing unseen."""

from __future__ import annotations

import sqlite3
import sqlite3.dbapi2
import sys
import threading
import traceback
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCESSOR = "kestrel/services/install_id.py"
MOVE = "kestrel/db/migrations/versions/0041_install_identity.py"
TABLE = "install_identity"
# Every function of the app that may be on the way into the install ID module (round 8: the whole
# chain, not just the nearest caller), by file and name. A read counts only when every kestrel
# frame outside the module is listed, so a new MCP tool, JSON route or export that calls one of
# these functions fails as a stranger in the chain. Work handed to a thread starts a new stack,
# so its chain begins at the thread's entry (build_bundle_to_temp, restore_backup): known limit,
# D-INST-20 (amended, round 8). Test code may call the module.
CALLERS = {
    "kestrel/web/install_id_view.py::for_request": "the ID on a page of this computer",
    "kestrel/web/pages/about.py::page": "About",
    "kestrel/web/pages/settings_data.py::data_pane_ctx": "Settings > Your data",
    "kestrel/web/pages/settings.py::pane_ctx": "Settings, the pane being drawn",
    "kestrel/web/pages/settings.py::settings_page": "the Settings page",
    "kestrel/web/pages/settings.py::settings_section": "one Settings section",
    "kestrel/web/app.py::_ensure_install_id": "startup writes the record once",
    "kestrel/web/app.py::lifespan": "the app's startup",
    "kestrel/services/diagnostics.py::install_id_value": "the diagnostics file's ID",
    "kestrel/services/diagnostics.py::summary": "the diagnostics summary",
    "kestrel/services/diagnostics.py::build_bundle": "the diagnostics file the person saves",
    "kestrel/services/diagnostics.py::build_bundle_to_temp": "the same, written to a temp file",
    "kestrel/services/backup.py::restore_backup": "a restore keeps this computer's record",
}
_WRITES = (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE)

# Lines of the __call__ of every pure ASGI middleware of this repository that an app built under
# the guard registers in its stack (read from the stack itself when the app first runs, never by
# name): a request passes through such a frame on its way to every route, so the frame does not
# choose to reach the install ID. It is skipped in the chain except as the direct caller, so a
# middleware that reads the ID itself is still a stranger (D-INST-30, #204 with #212).
_pass_through: dict[str, set[int]] = {}


def _middleware_lines(stack) -> dict[str, set[int]]:
    """{repo path: lines of __call__} for each middleware of ours in a built ASGI stack."""
    out: dict[str, set[int]] = {}
    obj, seen = stack, 0
    while obj is not None and seen < 64:
        seen += 1
        call = getattr(type(obj), "__call__", None)  # noqa: B004 - the class's own method
        code = getattr(call, "__code__", None)
        if code is not None and _ours(code.co_filename):
            rel = Path(code.co_filename).relative_to(ROOT).as_posix()
            out.setdefault(rel, set()).update(ln for _s, _e, ln in code.co_lines() if ln)
        obj = getattr(obj, "app", None)
    return out


def _passing(frame) -> bool:
    rel = Path(frame.filename).relative_to(ROOT).as_posix()
    return frame.lineno in _pass_through.get(rel, ())


def _ours(filename: str) -> bool:
    """A frame of this repository's code (kestrel or a test), never a library's or the guard's:
    the innermost such frame is the code that asked for the statement."""
    path = Path(filename)
    try:
        rel = path.relative_to(ROOT)  # by path parts, so Windows backslashes and drive case match
    except ValueError:
        return False
    return bool(rel.parts) and not rel.parts[0].startswith(".venv") and path != Path(__file__)


def _accessor(frame) -> bool:
    return Path(frame.filename).relative_to(ROOT).as_posix() == ACCESSOR


def chain_of(ours: list) -> list[str]:
    """`file::function` for each frame of ours, outermost first, without the middleware frames
    a request only passes through. A middleware frame stays when it is the direct caller (the
    frame just outside the install ID module, or the innermost frame)."""
    kept = [f for i, f in enumerate(ours) if not (_passing(f) and i < len(ours) - 1
            and not _accessor(ours[i + 1]))]
    return [f"{Path(f.filename).relative_to(ROOT).as_posix()}::{f.name}" for f in kept]


def _stack() -> list[traceback.FrameSummary]:
    """The caller's stack, oldest frame first, like traceback.extract_stack() but without touching
    linecache: extract_stack (and StackSummary.extract even with lookup_lines=False) stat-checks
    every file on the stack, once per statement here, and that hung the install ID contract test
    past its timeout on Windows. Only filename, name and lineno are used."""
    frames = []
    frame = sys._getframe(1)
    while frame is not None:
        code = frame.f_code
        frames.append(traceback.FrameSummary(
            code.co_filename, frame.f_lineno, code.co_name, lookup_line=False))
        frame = frame.f_back
    frames.reverse()
    return frames


class _Guard:
    def __init__(self, allowed: dict[str, str]) -> None:
        self.allowed = allowed
        self.violations: list[str] = []
        self.allowed_reads: set[str] = set()
        self.unguarded: list[str] = []
        self.engine_connections = 0
        self.chains: set[tuple[str, ...]] = set()

    def on_touch(self, write: bool) -> None:
        # aiosqlite runs statements on its own thread: the stack captured where the statement
        # was submitted (on the event loop, with the awaiting code on it) comes first.
        stack = (getattr(_ctx, "stack", None) or []) + _stack()
        where = chain_of([f for f in stack if _ours(f.filename)])
        inner = where[-1] if where else ""
        if inner.startswith(ACCESSOR + "::"):
            chain = [w for w in where if w.startswith("kestrel/")
                     and not w.startswith(ACCESSOR + "::")]
            self.chains.add(tuple(chain))
            strangers = [w for w in chain if w not in CALLERS]
            if strangers:
                self.violations.append(
                    f"{' <- '.join(reversed(strangers))} reached the install ID module "
                    "(not in CALLERS)")
            else:
                self.allowed_reads.add(ACCESSOR)
        elif inner.startswith(MOVE + "::"):  # the one-time move (its CHECK reads as well)
            self.allowed_reads.add(MOVE)
        elif inner in self.allowed:
            self.allowed_reads.add(inner)
        else:
            self.violations.append(" <- ".join(reversed(where)) or "<outside the repository>")


_active: _Guard | None = None
_ctx = threading.local()


def _authorize(action, arg1, _arg2, _db, _trigger):
    if _active is not None and arg1 == TABLE and (
            action == sqlite3.SQLITE_READ or action in _WRITES):
        _active.on_touch(action in _WRITES)
    return sqlite3.SQLITE_OK


class GuardedConnection(sqlite3.Connection):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.set_authorizer(_authorize)


def _driver_connection(dbapi_connection) -> object:
    """The sqlite3 connection under a SQLAlchemy DBAPI connection (pysqlite hands it over as is;
    the aiosqlite adapter wraps an aiosqlite.Connection that holds it)."""
    obj = dbapi_connection
    for attr in ("_connection", "_conn"):
        if isinstance(obj, sqlite3.Connection):
            break
        obj = getattr(obj, attr, obj)
    return obj


@contextmanager
def guard(monkeypatch, allowed: dict[str, str]):
    """Turn the guard on for the block; yields the _Guard holding what it saw. Engines created
    before the block keep connections opened without the authorizer: callers reset the app's
    engine inside the block."""
    global _active
    from sqlalchemy import event
    from sqlalchemy.pool import Pool

    real = sqlite3.connect

    def connect(*a, **kw):
        kw.setdefault("factory", GuardedConnection)
        kw["cached_statements"] = 0
        return real(*a, **kw)

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(sqlite3.dbapi2, "connect", connect)

    def on_connect(dbapi_connection, _record):
        if _active is None:
            return
        _active.engine_connections += 1
        driver = _driver_connection(dbapi_connection)
        if not isinstance(driver, GuardedConnection):
            _active.unguarded.append(type(driver).__name__)

    event.listen(Pool, "connect", on_connect)

    import aiosqlite.core

    real_execute = aiosqlite.core.Connection._execute

    async def _execute(self, fn, *a, **kw):
        origin = _stack()

        def run():
            _ctx.stack = origin
            try:
                return fn(*a, **kw)
            finally:
                _ctx.stack = None

        return await real_execute(self, run)

    monkeypatch.setattr(aiosqlite.core.Connection, "_execute", _execute)

    from starlette.applications import Starlette

    real_call = Starlette.__call__

    async def app_call(self, scope, receive, send):
        if self.middleware_stack is None:
            self.middleware_stack = self.build_middleware_stack()
        for rel, lines in _middleware_lines(self.middleware_stack).items():
            _pass_through.setdefault(rel, set()).update(lines)
        await real_call(self, scope, receive, send)

    monkeypatch.setattr(Starlette, "__call__", app_call)
    _active = _Guard(allowed)
    try:
        yield _active
    finally:
        _active = None
        _pass_through.clear()
        event.remove(Pool, "connect", on_connect)
