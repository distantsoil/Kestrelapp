"""#148: the startup error card says why the server stopped, read from stderr.log, in plain
words, with no path and no key. Fixture logs only; no real server, no network."""

from __future__ import annotations

import pytest

from kestrel import paths, startup_health

TRACE = """INFO:     Started server process [4242]
INFO:     Waiting for application startup.
Traceback (most recent call last):
  File "C:\\Users\\sam\\AppData\\Local\\Kestrel\\app\\kestrel\\web\\app.py", line 856, in lifespan
    await _start_mcp_if_enabled()
  File "/opt/kestrel/mcp/server.py", line 10, in <module>
    import fastmcp
importlib.metadata.PackageNotFoundError: No package metadata was found for fastmcp
ERROR:    Application startup failed. Exiting.
"""


@pytest.fixture
def stderr_log():
    paths.ensure_dirs()
    f = paths.logs_dir() / "stderr.log"
    f.write_text("", encoding="utf-8")
    yield f
    f.unlink(missing_ok=True)


def test_the_cause_names_the_exception_in_plain_words(stderr_log):
    stderr_log.write_text(TRACE, encoding="utf-8")
    lines = startup_health.startup_cause()
    assert lines[0] == "The server stopped while it was starting."
    assert lines[1].startswith("importlib.metadata.PackageNotFoundError: No package metadata")
    assert len(lines) <= 3


def test_no_path_and_no_key_reach_the_card(stderr_log):
    stderr_log.write_text(
        TRACE.replace(
            "No package metadata was found for fastmcp",
            "bad value sk-abcdefgh12345678 in C:\\Users\\sam\\notes\\a.txt or /home/sam/x/y.py",
        ),
        encoding="utf-8",
    )
    text = "\n".join(startup_health.startup_cause())
    assert "sk-abcdefgh" not in text and "sam" not in text
    assert "C:\\" not in text and "/home" not in text and "/opt" not in text


def test_only_this_attempts_part_of_the_log_is_read(stderr_log):
    stderr_log.write_text(TRACE, encoding="utf-8")  # an earlier launch crashed
    mark = startup_health.stderr_log_size()
    assert startup_health.startup_cause(mark) == []  # this launch has written nothing yet
    with stderr_log.open("a", encoding="utf-8") as fh:
        fh.write("ValueError: nope\nERROR:    Application startup failed. Exiting.\n")
    assert startup_health.startup_cause(mark)[1] == "ValueError: nope"


def test_a_log_with_no_failed_startup_or_no_log_gives_nothing(stderr_log):
    stderr_log.write_text("INFO:     all good\n", encoding="utf-8")
    assert startup_health.startup_cause() == []  # a slow start is not called a crash
    stderr_log.unlink()
    assert startup_health.startup_cause() == []


def test_the_problem_card_shows_the_cause(monkeypatch, stderr_log):
    """End to end through the launcher: a server that crashes in startup gives the card the
    cause instead of 'did not answer in time'."""
    import threading

    from kestrel.db import migrate
    from kestrel.desktop import shell as sh
    from tests.test_startup_retry import _Rec

    def crashed_server() -> int:
        stderr_log.write_text(TRACE, encoding="utf-8")  # what the dying server wrote
        t = threading.Thread(target=lambda: None)
        t.start()
        t.join()  # a server thread that is already gone
        monkeypatch.setattr(sh, "server_thread", t)
        return 9  # nothing listens here

    monkeypatch.setattr(sh, "start_server", crashed_server)
    monkeypatch.setattr(sh, "HEALTH_WAIT_S", 0.3)
    monkeypatch.setattr(sh, "HEALTH_EXTENDED_S", 0.3)
    for attr in ("_quitting", "_started", "_boot_failed", "_cancelling"):
        monkeypatch.setattr(sh, attr, False)
    monkeypatch.setattr(startup_health, "_offered", False)
    monkeypatch.delenv(startup_health.SAFE_MODE_ENV, raising=False)
    paths.startup_health_json().unlink(missing_ok=True)
    migrate.run_migrations()
    rec = _Rec()
    monkeypatch.setattr(sh, "_Splash", lambda win: rec)
    try:
        sh._boot_steps(busy=False)
    finally:
        paths.startup_health_json().unlink(missing_ok=True)
    assert rec.failed, "the problem card must be shown"
    reason, details, code = rec.failed[0]
    assert code == "health_never_up"
    assert "stopped while starting" in reason and "PackageNotFoundError" not in reason
    assert "PackageNotFoundError: No package metadata" in details  # under Show details
    assert "did not answer in time" not in reason
    assert "C:\\" not in reason + details and "stderr.log" in details


# D-SEC-16: a path with spaces is masked whole, quoted or not, on Windows and macOS. Invented
# folder names; no part of any of them may reach the card.
SPACED = [
    "FileNotFoundError: [Errno 2] No such file or directory: "
    "'C:\\\\Users\\\\Robin Quill\\\\AppData\\\\Local\\\\Kestrel\\\\kestrel.db'",
    "PermissionError: C:\\Users\\Robin Quill\\AppData\\Local\\Kestrel\\Old Notes.pdf is locked",
    "sqlite3.OperationalError: unable to open /Users/Robin Quill/Library/Application Support/"
    "Kestrel/kestrel.db, giving up",
    'OSError: "/Users/Robin Quill/Library/Application Support/Kestrel" is missing',
    "OSError: \\\\fileserver\\Robin Quill\\Kestrel\\kestrel.db is gone",
    # D-SEC-18: a last part with no extension is masked to its end, spaces included.
    "OSError: /home/Robin Quill",
    "OSError: /Users/Robin Quill/Library/Quill Kestrel Files",
    "OSError: C:\\Users\\Robin Quill\\Quill Kestrel",
    "OSError: \\\\fileserver\\share Robin\\Quill Kestrel Docs",
    # D-SEC-19: a name may hold `,` `;` `)` `]` `'`; the line is masked from the path to its end.
    "OSError: /home/Quill, Robin/kestrel.db is locked",
    "OSError: C:\\Users\\Robin\\Quill, Inc\\kestrel.db",
    "OSError: /Users/Robin/Quill (Holdings) Ltd/kestrel.db",
    "OSError: C:\\Data\\O'Quill Files\\kestrel.db",
    "OSError: \\\\fileserver\\Quill; Robin]\\kestrel.db",
]


@pytest.mark.parametrize("line", SPACED)
def test_a_path_with_spaces_is_masked_whole(stderr_log, line):
    stderr_log.write_text(
        f"Traceback (most recent call last):\n{line}\nERROR:    Application startup failed.\n",
        encoding="utf-8",
    )
    out = startup_health.startup_cause(0)
    text = " ".join(out)
    assert "a file on this computer" in text
    for part in ("Robin", "Quill", "Users", "AppData", "Library", "Application Support",
                 "Kestrel", "kestrel.db", "Old Notes", "fileserver", "Files", "Docs", "share"):
        assert part not in text, (part, text)


def test_prose_around_a_path_survives_and_ordinary_slashes_are_left(stderr_log):
    stderr_log.write_text(
        "Traceback (most recent call last):\nValueError: bad read/write value 3\n"
        "ERROR:    Application startup failed.\n", encoding="utf-8",
    )
    assert startup_health.startup_cause(0)[1] == "ValueError: bad read/write value 3"


def test_the_employer_map_is_applied_when_one_is_given(stderr_log):
    stderr_log.write_text(
        "Traceback (most recent call last):\nRuntimeError: Tallowmere Foundry refused\n"
        "ERROR:    Application startup failed.\n", encoding="utf-8",
    )
    out = startup_health.startup_cause(0, {"Tallowmere Foundry": "a current employer"})
    assert "Tallowmere" not in " ".join(out)
    assert "Tallowmere" in " ".join(startup_health.startup_cause(0))  # no map, no redaction


@pytest.mark.parametrize("line", [
    "OSError: bad /Acme Corp/kestrel.db",
    "OSError: cannot open /AcmeCorp",
    "FileNotFoundError: Acme Corp/data/kestrel.db",
    "FileNotFoundError: ./Acme/x.db",
    "PermissionError: C:Acme Corp\\x.db",
])
def test_no_part_of_an_unrecognized_path_reaches_the_card(line):
    # A root folder with a space, a single part, a relative or a drive-relative path: none of
    # the folder names may reach the startup card (the full line stays in stderr.log).
    shown = startup_health._plain(line, lambda s: s)
    assert "Acme" not in shown and "Corp" not in shown
    assert shown.startswith(line.split(": ", 1)[0])  # the exception's name is kept


@pytest.mark.parametrize("line", [
    "FileNotFoundError: [Errno 2] No such file or directory: 'Acme Corp/kestrel.db'",
    'FileNotFoundError: [Errno 2] No such file or directory: "Acme Corp/kestrel.db"',
    "sqlite3.OperationalError: unable to open database file (Users/Acme Corp/AppData)",
    "OSError: cannot read [Acme Corp\\settings.yml]",
])
def test_a_quoted_or_bracketed_relative_path_with_a_space_is_masked(line):
    # A library can report a relative path inside quotes or brackets; a folder named after a
    # person (here an invented company) must not reach the startup card (D-SEC-19 (D)).
    shown = startup_health._plain(line, lambda s: s)
    assert "Acme" not in shown and "Corp" not in shown and "kestrel.db" not in shown
    assert shown.startswith(line.split(": ", 1)[0])


# ------------------------------------------------------------------ the startup card's code
def _module_constants(tree) -> dict[str, str]:
    """Module-level `NAME = "text"` assignments of one parsed module."""
    import ast

    out: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            out[node.targets[0].id] = node.value.value
    return out


def _startup_codes_in_source() -> set[str]:
    """Every code a startup card can be shown with, found structurally: an ast walk over
    kestrel/ for StartupError(...) calls and for _show_problem(...) calls, resolving a string
    literal, a module-level constant, or `module.CONSTANT`. Anything it cannot resolve fails the
    test, so a new unmapped or unreadable code can never slip through."""
    import ast
    from pathlib import Path

    import kestrel

    root = Path(kestrel.__file__).parent
    trees = {
        f: ast.parse(f.read_bytes().decode("utf-8")) for f in sorted(root.rglob("*.py"))
    }
    consts = {f: _module_constants(t) for f, t in trees.items()}
    by_stem: dict[str, dict[str, str]] = {}
    for f, c in consts.items():
        by_stem.setdefault(f.stem, {}).update(c)

    def resolve(arg, file) -> str:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        if isinstance(arg, ast.Name) and arg.id in consts[file]:
            return consts[file][arg.id]
        if (
            isinstance(arg, ast.Attribute)
            and isinstance(arg.value, ast.Name)
            and arg.attr in by_stem.get(arg.value.id, {})
        ):
            return by_stem[arg.value.id][arg.attr]
        raise AssertionError(f"{file.name}:{arg.lineno}: cannot resolve a startup code")

    found: set[str] = set()
    for f, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "StartupError":
                assert node.args, f"{f.name}:{node.lineno}: StartupError with no code"
                found.add(resolve(node.args[0], f))
            elif name == "_show_problem":
                keyword = {k.arg: k.value for k in node.keywords}
                arg = node.args[3] if len(node.args) > 3 else keyword.get("code")
                assert arg is not None, f"{f.name}:{node.lineno}: a card with no code"
                if (  # `exc.code` passes on a code the StartupError(...) calls already supply
                    isinstance(arg, ast.Attribute)
                    and arg.attr == "code"
                    and isinstance(arg.value, ast.Name)
                    and arg.value.id == "exc"
                ):
                    continue
                found.add(resolve(arg, f))
    return found


def test_every_startup_code_maps_to_a_registered_sta_code():
    from kestrel import error_codes
    from kestrel.desktop import shell

    codes = _startup_codes_in_source()
    assert codes, "the walk found no startup codes at all"
    assert codes == set(shell.STARTUP_ERROR_CODES)  # none unmapped, none mapped but unused
    for code in codes:
        shown = shell.startup_error_code(code)
        assert shown.startswith("STA-")
        assert error_codes.get(shown).fix  # the registry has plain text for it
    assert len(set(shell.STARTUP_ERROR_CODES.values())) == len(codes)  # one code per card


def test_the_walk_fails_on_a_code_it_cannot_resolve(tmp_path, monkeypatch):
    """The structural check must refuse what it cannot read, never skip it."""
    import kestrel

    pkg = tmp_path / "kestrel"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "m.py").write_text(
        "def f(code):\n    raise StartupError(code, 'r', 'd')\n", encoding="utf-8"
    )
    monkeypatch.setattr(kestrel, "__file__", str(pkg / "__init__.py"))
    with pytest.raises(AssertionError, match="cannot resolve"):
        _startup_codes_in_source()


def test_an_unknown_startup_code_fails_loudly_never_a_blank():
    from kestrel.desktop import shell

    with pytest.raises(KeyError):
        shell.startup_error_code("a_code_nobody_registered")


def test_the_card_and_the_log_both_carry_the_code(monkeypatch):
    from structlog.testing import capture_logs

    from kestrel.desktop import shell

    class Rec:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def fail(self, *args):
            self.calls.append(args)

    monkeypatch.setattr(shell, "_boot_failed", False)  # restored after the test
    monkeypatch.setattr(shell, "_quiet", False)  # a quiet start would try to show a window
    rec = Rec()
    with capture_logs() as logged:
        shell._show_problem(rec, "A reason.", "Startup step: x", "health_never_up")
    assert rec.calls == [("A reason.", "Startup step: x", "health_never_up", "STA-006")]
    assert any(
        e.get("event") == "startup_problem_shown" and e.get("error_code") == "STA-006"
        for e in logged
    )
    assert shell._boot_failed is True


def test_an_unregistered_code_logs_once_and_the_card_still_shows(monkeypatch):
    from structlog.testing import capture_logs

    from kestrel.desktop import shell

    class Rec:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def fail(self, *args):
            self.calls.append(args)

    monkeypatch.setattr(shell, "_boot_failed", False)
    monkeypatch.setattr(shell, "_quiet", False)
    monkeypatch.setattr(shell, "_stop_server", lambda: None)
    monkeypatch.setattr(shell, "_write_marker", lambda **k: None)
    monkeypatch.setattr(shell, "_quitting", False)
    rec = Rec()
    monkeypatch.setattr(shell, "_Splash", lambda win: rec)

    def _boom(splash, stop=lambda: False):
        raise shell.StartupError("a_code_nobody_registered", "A reason.", "d")

    monkeypatch.setattr(shell, "_start_backend", _boom)
    with capture_logs() as logged:
        shell._boot_steps(busy=False)
    assert rec.calls == [("A reason.", "d", "a_code_nobody_registered", "")]  # a card, no code
    gaps = [e for e in logged if e.get("event") == "startup_code_unregistered"]
    assert len(gaps) == 1  # computed once per error, so it logs once
    aborted = [e for e in logged if e.get("event") == "desktop_start_aborted"]
    assert aborted and aborted[0]["error_code"] == ""


def test_the_log_carries_the_code_on_every_aborted_start(monkeypatch):
    """Both paths log desktop_start_aborted with error_code: the window path and the
    no-window fallback (abort_start)."""
    from structlog.testing import capture_logs

    from kestrel.desktop import shell

    monkeypatch.setattr(shell, "_stop_previous_server", lambda: True)
    monkeypatch.setattr(shell, "release_single_instance", lambda: None)
    with capture_logs() as logged:
        assert shell.abort_start("no_port", shell._card_code("no_port")) == 1
    aborted = [e for e in logged if e.get("event") == "desktop_start_aborted"]
    assert len(aborted) == 1
    assert aborted[0]["reason"] == "no_port" and aborted[0]["error_code"] == "STA-005"


def test_the_busy_and_canceled_card_codes_reach_the_log(monkeypatch):
    from structlog.testing import capture_logs

    from kestrel.desktop import shell

    class Rec:
        def fail(self, *args):
            pass

    monkeypatch.setattr(shell, "_boot_failed", False)
    monkeypatch.setattr(shell, "_quiet", False)
    with capture_logs() as logged:
        shell._show_problem(Rec(), "r", "d", shell.BUSY_CODE)
        shell._show_problem(Rec(), "r", "d", shell.CANCELED_CODE)
    shown = {
        e["startup_code"]: e["error_code"]
        for e in logged
        if e["event"] == "startup_problem_shown"
    }
    assert shown == {"busy_closing": "STA-007", "start_canceled": "STA-008"}
