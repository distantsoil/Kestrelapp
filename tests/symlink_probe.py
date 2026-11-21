"""Making links in tests.

A FILE symlink on Windows needs administrator rights or Developer Mode; without them Windows
refuses with error 1314, and a test that plants one has nothing to test, so it skips. Any other
error, on any OS, fails the test. Folder links need no privilege: they are junctions on Windows."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kestrel import paths

_NO_SYMLINK_PRIVILEGE = 1314  # ERROR_PRIVILEGE_NOT_HELD


def symlink_or_skip(link: Path, target: Path) -> None:
    """A file symlink at `link` to `target`, made in the test's own folder; skips only when
    Windows refuses it for want of the privilege."""
    try:
        link.symlink_to(target)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == _NO_SYMLINK_PRIVILEGE:
            pytest.skip("this Windows account cannot create file symlinks (no administrator "
                        "rights or Developer Mode)")
        raise


def link_folder(link: Path, target: Path) -> None:
    """A link to the folder `target`: a junction on Windows (no privilege needed), else a
    symlink."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


needs_dir_fd = pytest.mark.skipif(
    not paths._DIR_FD,
    reason="this OS cannot work relative to an open folder; the path checks cover it",
)
