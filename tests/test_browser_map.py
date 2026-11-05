"""D-TEST-7 A: the map from UI files to browser test files stays complete and true."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("diff_gate", ROOT / "scripts" / "diff_gate.py")
diff_gate = importlib.util.module_from_spec(_spec)
sys.modules["diff_gate"] = diff_gate
_spec.loader.exec_module(diff_gate)

BMAP = diff_gate.load_map()


def _repo_ui_files() -> list[str]:
    out = []
    for top in (
        "kestrel/web/templates",
        "kestrel/web/static",
        "kestrel/web/pages",
        "kestrel/desktop",
        "kestrel/docs/templates",
    ):
        out += [p.relative_to(ROOT).as_posix() for p in (ROOT / top).rglob("*") if p.is_file()]
    web = ROOT / "kestrel/web"
    out += [p.relative_to(ROOT).as_posix() for p in web.glob("*.py")]
    # every module under kestrel/web is a UI file now, none is exempt by name
    out += [p.relative_to(ROOT).as_posix() for p in (ROOT / "kestrel/docs").glob("*.py")]
    return sorted(p for p in out if diff_gate.is_ui_file(p))


def test_the_repo_has_ui_files_to_check():
    assert len(_repo_ui_files()) > 100


@pytest.mark.parametrize("path", _repo_ui_files())
def test_every_ui_file_maps_to_a_browser_test(path):
    assert diff_gate.browser_files_for(path, BMAP), (
        f"{path} matches no rule in tests/browser_map.toml; add it so a change to it runs a "
        "browser test"
    )


def test_every_mapped_browser_file_exists():
    for name in sorted(BMAP.all_browser_files()):
        assert (ROOT / "tests" / name).is_file(), f"tests/{name} is in the map but missing"


def test_every_browser_test_file_is_in_the_map():
    on_disk = {p.name for p in (ROOT / "tests").glob("test_*_browser.py")}
    known = BMAP.all_browser_files() | set(BMAP.soak)
    assert on_disk <= known, sorted(on_disk - known)


def test_soak_only_browser_files_exist_and_are_never_selected_by_the_gate():
    assert BMAP.soak == ("test_layout_crawl_browser.py",)
    for name in BMAP.soak:
        assert (ROOT / "tests" / name).is_file(), f"tests/{name} is listed as soak but is missing"
    assert not set(BMAP.soak) & BMAP.all_browser_files()
    assert not set(BMAP.soak) & set(BMAP.shell)
    selected, _ = diff_gate.select_browser_tests(["kestrel/web/templates/base.html"], BMAP)
    assert not {f"tests/{n}" for n in BMAP.soak} & set(selected)


def test_the_shell_set_is_all_browser_files_and_nonempty():
    assert BMAP.shell
    assert set(BMAP.shell) <= {p.name for p in (ROOT / "tests").glob("test_*_browser.py")}


def test_every_web_module_is_a_ui_file():
    for p in (ROOT / "kestrel/web").glob("*.py"):
        assert diff_gate.is_ui_file(p.relative_to(ROOT).as_posix()), p.name


def test_docs_modules_that_render_a_template_are_ui_files():
    rendering = [
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "kestrel/docs").glob("*.py")
        if "get_template" in p.read_text(encoding="utf-8")
    ]
    assert rendering, "expected the docs engine to render templates"
    for path in rendering:
        assert diff_gate.is_ui_file(path), f"{path} renders a template but is not a UI file"


def test_docs_files_map_to_the_letter_and_tailor_browser_files():
    letters = diff_gate.browser_files_for("kestrel/docs/letters.py", BMAP)
    assert "test_cover_letter_browser.py" in letters and "test_role_browser.py" in letters
    assert diff_gate.browser_files_for("kestrel/docs/templates/letter.html", BMAP) >= letters
    assert "test_tailor_browser.py" in diff_gate.browser_files_for(
        "kestrel/docs/templates/document.html", BMAP
    )
    assert "test_tailor_browser.py" in diff_gate.browser_files_for("kestrel/docs/render.py", BMAP)
