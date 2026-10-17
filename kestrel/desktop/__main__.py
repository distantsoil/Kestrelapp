"""Desktop entry point (SPEC-INTERFACES 1.1). Keep the module top level to imports only (F9): in a
spawn-based multiprocessing child, top-level code runs before freeze_support() short-circuits. All
setup happens inside main(), after freeze_support() and the stdout/stderr repair.

This is also the frozen executable's ONLY entry point on every platform, so it dispatches: no
arguments opens the desktop app; any argument runs the `kestrel` CLI (`Kestrel.app/Contents/MacOS/
Kestrel setup`, `./Kestrel.AppImage doctor`, `kestrel-cli.exe setup`). The one entry point is what
keeps the three packages to one PyInstaller Analysis and one set of hidden imports."""

from __future__ import annotations

import multiprocessing
import sys


def _repair_streams() -> None:
    """In a --windowed PyInstaller build sys.stdout/stderr are None (F3). Replace them with file
    handles BEFORE anything logs or prints, or the first log call raises AttributeError."""
    from kestrel import paths

    paths.ensure_dirs()
    if sys.stdout is None:
        sys.stdout = open(paths.logs_dir() / "stdout.log", "a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = open(paths.logs_dir() / "stderr.log", "a", encoding="utf-8", buffering=1)


def cli_args(argv: list[str]) -> list[str]:
    """The arguments that mean "run the CLI, not the window". Finder on older macOS launched apps
    with a `-psn_<n>` process-serial-number argument; that is a launch artifact, never a command."""
    return [a for a in argv[1:] if not a.startswith("-psn_")]


def main() -> None:
    _repair_streams()
    # tiktoken caching is handled by kestrel.ingest.tokens: it sets TIKTOKEN_CACHE_DIR to the
    # vendored kestrel/resources/tiktoken_cache (F2's exact SHA-1 file) which ships in the bundle.
    args = cli_args(sys.argv)
    if args:
        from kestrel.cli import app

        # typer reads sys.argv[1:] itself; hand it the filtered list and the package's own name so
        # `--help` reads `kestrel` whichever executable the operator typed.
        sys.argv = [sys.argv[0], *args]
        app(prog_name="kestrel")
        return

    from kestrel.desktop.shell import run_desktop

    # run_desktop returns non-zero when the server never became healthy (the mutex is released and
    # the process must end); a dropped return code would leave a process the operator cannot see.
    sys.exit(run_desktop())


if __name__ == "__main__":
    multiprocessing.freeze_support()  # trap 1: the first statement in the entry point
    main()
