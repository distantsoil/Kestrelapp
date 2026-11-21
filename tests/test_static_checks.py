"""D-TEST-8: the type check and changed-line lint that scripts/check.py runs. Each check is fed
the broken code it exists to catch (written into a throwaway module here, never into kestrel/)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "static_checks", ROOT / "scripts" / "static_checks.py"
)
sc = importlib.util.module_from_spec(_spec)
sys.modules["static_checks"] = sc
_spec.loader.exec_module(sc)

# The two bugs from the ompepic review (2026-11-04).
BROKEN = """\
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from starlette.requests import Request


class Base(DeclarativeBase):
    pass


class Row(Base):
    __tablename__ = "row"
    id: Mapped[int] = mapped_column(primary_key=True)


async def find(s: AsyncSession):
    return await s.execute(select(Row)).scalar_one_or_none()


async def read_form(request: Request):
    form = request.form()
    return form.get("name")
"""

FIXED = BROKEN.replace(
    "await s.execute(select(Row)).scalar_one_or_none()",
    "(await s.execute(select(Row))).scalar_one_or_none()",
).replace("form = request.form()", "form = await request.form()")


@pytest.fixture(scope="module")
def broken_module(tmp_path_factory):
    path = tmp_path_factory.mktemp("typed") / "broken_module.py"
    path.write_text(BROKEN, encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def broken_diags(broken_module):
    return sc.run_pyright([str(broken_module)])


def test_pyright_catches_an_await_bound_to_the_wrong_call(broken_diags):
    messages = [d["message"] for d in broken_diags]
    assert any("scalar_one_or_none" in m and "Coroutine" in m for m in messages), messages


def test_pyright_catches_an_unawaited_form_read_used_with_get(broken_diags):
    messages = [d["message"] for d in broken_diags]
    assert any('"get"' in m and "FormData" in m for m in messages), messages


def test_new_errors_in_a_changed_file_fail_with_file_line_and_fix(broken_module, broken_diags):
    changed = {sc._rel(str(broken_module), ROOT)}
    problems = sc.new_type_errors(broken_diags, {"files": {}}, changed)
    assert len(problems) == 2
    assert all(
        "RULE type check" in p and "broken_module.py:" in p and "Fix:" in p for p in problems
    )


def test_errors_already_in_the_baseline_are_allowed_even_when_lines_move(
    broken_module, broken_diags, tmp_path
):
    baseline = sc.make_baseline(broken_diags)
    assert baseline["total"] == 2
    moved = tmp_path / "moved.py"
    moved.write_text("\n\n\n" + BROKEN, encoding="utf-8")
    again = sc.run_pyright([str(moved)])
    # same code four lines lower: the baseline of the first file does not name this file, so
    # re-key it to the new name and expect the line shift alone to cost nothing
    renamed = {"files": {sc._rel(str(moved), ROOT): next(iter(baseline["files"].values()))}}
    assert sc.new_type_errors(again, renamed, {sc._rel(str(moved), ROOT)}) == []


def test_a_third_error_beyond_the_baseline_is_reported(broken_module, broken_diags, tmp_path):
    baseline = sc.make_baseline(broken_diags)
    worse = tmp_path / "worse.py"
    worse.write_text(
        BROKEN + "\n\nasync def again(request: Request):\n    return request.form().get('x')\n",
        encoding="utf-8",
    )
    diags = sc.run_pyright([str(worse)])
    name = sc._rel(str(worse), ROOT)
    allowed = {"files": {name: next(iter(baseline["files"].values()))}}
    problems = sc.new_type_errors(diags, allowed, {name})
    assert len(problems) == 1 and "worse.py:" in problems[0]


def test_errors_in_unchanged_files_are_ignored(broken_diags):
    assert sc.new_type_errors(broken_diags, {"files": {}}, {"kestrel/other.py"}) == []


def test_the_fixed_module_has_no_type_errors(tmp_path):
    path = tmp_path / "fixed_module.py"
    path.write_text(FIXED, encoding="utf-8")
    assert sc.run_pyright([str(path)]) == []


def test_the_committed_baseline_is_well_formed():
    data = json.loads(sc.BASELINE_PATH.read_text(encoding="utf-8"))
    assert data["total"] == sum(sum(c.values()) for c in data["files"].values())
    assert all(f.startswith("kestrel/") for f in data["files"])


def test_a_branch_with_no_python_change_skips_the_type_check():
    notes: list[str] = []
    assert sc.type_check_changed(["README.md"], log=notes.append) == []
    assert "nothing to check" in notes[0]


LINT_SOURCE = """\
import asyncio
import time


async def work():
    return 1


def swallow():
    try:
        work()
    except Exception:
        pass


async def blocks():
    time.sleep(1)
    asyncio.create_task(work())
"""


def test_changed_line_lint_flags_bad_patterns_only_on_changed_lines(tmp_path):
    path = tmp_path / "lint_me.py"
    path.write_text(LINT_SOURCE, encoding="utf-8")
    findings = sc.run_ruff_select([str(path)])
    codes = {f["code"] for f in findings}
    assert {"BLE001", "S110", "RUF006"} <= codes and any(c.startswith("ASYNC") for c in codes)
    name = sc._rel(str(path), ROOT)
    every_line = {name: set(range(1, 30))}
    problems = sc.lint_on_changed_lines(findings, every_line)
    assert len(problems) == len(findings)
    assert all("RULE changed-line lint" in p and f"{name}:" in p and "Fix:" in p for p in problems)
    assert any("BLE001" in p and "specific exception" in p for p in problems)
    # only the `import` lines changed: nothing on a changed line is flagged
    assert sc.lint_on_changed_lines(findings, {name: {1, 2}}) == []
    assert sc.lint_on_changed_lines(findings, {"other.py": set(range(1, 30))}) == []


# --- The type check also runs as the other systems see the code (D-CI-595) ----------------------

WINDOWS_ONLY = """\
import os
import sys


def open_folder(path: str) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def guarded(path: str) -> int:
    if sys.platform == "win32":
        return -1
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def toast() -> None:
    import a_package_only_installed_on_windows  # noqa: F401
"""


@pytest.fixture(scope="module")
def windows_only_module(tmp_path_factory):
    path = tmp_path_factory.mktemp("platforms") / "windows_only.py"
    path.write_text(WINDOWS_ONLY, encoding="utf-8")
    return path


def test_host_platform_names_the_three_systems():
    assert [sc.host_platform(n) for n in ("win32", "darwin", "linux", "freebsd14")] == [
        "Windows", "Darwin", "Linux", "Linux"]
    assert sc.host_platform() in sc.PLATFORMS and set(sc.PLATFORMS) == {"Windows", "Darwin",
                                                                        "Linux"}


def test_a_call_windows_lacks_is_an_error_when_checked_as_windows(windows_only_module):
    """The 2026-11-17 case: os.O_DIRECTORY passed every Linux builder and stopped the gate on the
    owner's PC. Checked as Windows, pyright reports it from any machine."""
    diags = sc.run_pyright([str(windows_only_module)], platform="Windows")
    lines = sorted(d["range"]["start"]["line"] + 1 for d in diags
                   if "O_DIRECTORY" in d["message"])
    assert lines == [6]  # the unguarded call; the one behind `if sys.platform` is proven safe
    as_linux = sc.run_pyright([str(windows_only_module)], platform="Linux")
    assert [d for d in as_linux if "O_DIRECTORY" in d["message"]] == []


def test_the_gate_step_reports_it_once_with_the_system_named_and_skips_missing_packages(
        windows_only_module):
    root = windows_only_module.parent
    name = windows_only_module.name
    host_diags = sc.run_pyright([str(windows_only_module)], platform="Linux")

    def run(files, *, platform=None):
        return sc.run_pyright([str(root / f) for f in files], platform=platform)

    got = sc.other_platform_errors([name], {"files": {}}, host_diags, root=root, host="Linux",
                                   run=run)
    assert len(got) == 1, got
    assert got[0].startswith(f"RULE type check (as Windows sees it): {name}:6: ")
    assert '"O_DIRECTORY" is not a known attribute of module "os"' in got[0]
    assert "this line is wrong on Windows only" in got[0]
    # The package that exists only on another system is not a finding there.
    assert "a_package_only_installed_on_windows" not in " ".join(got)


def test_an_error_the_host_run_already_reported_is_not_reported_again_per_system(
        broken_module, broken_diags):
    root = broken_module.parent

    def run(files, *, platform=None):
        return sc.run_pyright([str(root / f) for f in files], platform=platform)

    assert broken_diags, "the fixture has errors on every system"
    assert sc.other_platform_errors([broken_module.name], {"files": {}}, broken_diags,
                                    root=root, run=run) == []


def test_the_host_system_is_not_run_twice(windows_only_module):
    asked = []

    def run(files, *, platform=None):
        asked.append(platform)
        return []

    sc.other_platform_errors(["x.py"], {"files": {}}, [], root=windows_only_module.parent,
                             host="Windows", run=run)
    assert asked == ["Darwin", "Linux"]

