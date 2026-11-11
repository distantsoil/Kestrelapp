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
