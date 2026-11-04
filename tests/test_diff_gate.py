"""The diff parser, map matcher and coverage computation behind scripts/check.py (D-TEST-7).
Synthetic inputs only: no git, no real suite run."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("diff_gate", ROOT / "scripts" / "diff_gate.py")
gate = importlib.util.module_from_spec(_spec)
sys.modules["diff_gate"] = gate
_spec.loader.exec_module(gate)

DIFF = """\
diff --git a/kestrel/a.py b/kestrel/a.py
--- a/kestrel/a.py
+++ b/kestrel/a.py
@@ -3,0 +4,2 @@ def f():
+x = 1
+y = 2
@@ -10 +12 @@
-old
+new
@@ -20,2 +21,0 @@
-gone
-gone too
diff --git a/kestrel/old.py b/kestrel/old.py
--- a/kestrel/old.py
+++ /dev/null
@@ -1,3 +0,0 @@
-a
diff --git a/kestrel/b.py b/kestrel/b.py
--- /dev/null
+++ b/kestrel/b.py
@@ -0,0 +1,3 @@
+one
+two
+three
"""


def test_diff_parser_reads_added_modified_and_new_files_only():
    got = gate.parse_diff_lines(DIFF)
    assert got == {"kestrel/a.py": {4, 5, 12}, "kestrel/b.py": {1, 2, 3}}


def _bmap():
    return gate.BrowserMap(
        shell=("test_frame_browser.py", "test_nav_browser.py"),
        rules=(
            ((" kestrel/web/templates/base.html".strip(),), ("@shell",)),
            (
                ("kestrel/web/pages/zebra.py", "kestrel/web/templates/partials/zb_*.html"),
                ("test_zebra_browser.py",),
            ),
        ),
    )


def test_matcher_expands_the_shell_and_unions_rules():
    bmap = _bmap()
    assert gate.browser_files_for("kestrel/web/templates/base.html", bmap) == set(bmap.shell)
    assert gate.browser_files_for("kestrel/web/templates/partials/zb_row.html", bmap) == {
        "test_zebra_browser.py"
    }
    assert gate.browser_files_for("kestrel/web/pages/other.py", bmap) == set()


def test_select_names_unmapped_ui_files_and_ignores_non_ui():
    changed = [
        "kestrel/web/templates/base.html",
        "kestrel/web/pages/zebra.py",
        "kestrel/web/pages/mystery.py",
        "kestrel/queue/queue.py",
        "README.md",
    ]
    run, unmapped = gate.select_browser_tests(changed, _bmap())
    assert run == [
        "tests/test_frame_browser.py",
        "tests/test_nav_browser.py",
        "tests/test_zebra_browser.py",
    ]
    assert unmapped == ["kestrel/web/pages/mystery.py"]


def test_is_ui_file_excludes_pycache_and_non_ui():
    assert gate.is_ui_file("kestrel/web/static/js/x.js")
    assert gate.is_ui_file("kestrel/web/routes_api.py")
    assert not gate.is_ui_file("kestrel/web/pages/__pycache__/x.cpython-312.pyc")
    assert gate.is_ui_file("kestrel/web/serve.py")  # all of kestrel/web, none exempt by name
    assert not gate.is_ui_file("kestrel/queue/queue.py")


def test_load_map_reads_toml(tmp_path):
    path = tmp_path / "m.toml"
    path.write_text(
        '[shell]\nfiles = ["s.py"]\n[[rule]]\npatterns = ["a/*"]\nfiles = ["@shell", "t.py"]\n',
        encoding="utf-8",
    )
    bmap = gate.load_map(path)
    assert gate.browser_files_for("a/b/c.html", bmap) == {"s.py", "t.py"}


COV = {
    "files": {
        "kestrel/a.py": {
            "executed_lines": [4, 5],
            "missing_lines": [12, 30],
            "excluded_lines": [13],
        },
    }
}


def test_diff_coverage_counts_only_executable_changed_lines(tmp_path):
    changed = {"kestrel/a.py": {4, 5, 12, 13, 99}, "kestrel/web/templates/x.html": {1}}
    pct, total, covered, uncovered = gate.diff_coverage(changed, COV, root=tmp_path)
    assert (total, covered) == (3, 2)  # 13 is excluded, 99 is not code, templates skipped
    assert round(pct, 1) == 66.7
    assert uncovered == {"kestrel/a.py": [12]}


def test_diff_coverage_none_when_nothing_to_measure(tmp_path):
    assert gate.diff_coverage({"kestrel/a.py": {99}}, COV, root=tmp_path)[0] is None
    assert gate.diff_coverage({"tests/t.py": {1, 2}}, COV, root=tmp_path)[0] is None


def test_unimported_file_counts_code_lines_as_uncovered(tmp_path):
    (tmp_path / "kestrel").mkdir()
    (tmp_path / "kestrel" / "new.py").write_text("# note\nx = 1\n\ny = 2\n", encoding="utf-8")
    pct, total, covered, uncovered = gate.diff_coverage(
        {"kestrel/new.py": {1, 2, 3, 4}}, COV, root=tmp_path
    )
    assert (pct, total, covered) == (0.0, 2, 0)
    assert uncovered == {"kestrel/new.py": [2, 4]}


def test_windows_style_paths_in_the_report_still_match(tmp_path):
    cov = {"files": {"kestrel\\a.py": {"executed_lines": [1], "missing_lines": []}}}
    assert gate.diff_coverage({"kestrel/a.py": {1}}, cov, root=tmp_path)[0] == 100.0


def test_absolute_and_windows_report_keys_are_made_repo_relative(tmp_path):
    root = tmp_path / "repo"
    posix_key = (root / "kestrel" / "a.py").as_posix()
    cov = {"files": {posix_key: {"executed_lines": [1], "missing_lines": [2]}}}
    pct = gate.diff_coverage({"kestrel/a.py": {1, 2}}, cov, root=root)[0]
    assert pct == 50.0
    win_root = Path("C:/work/repo")
    assert gate._norm_key("c:\\work\\repo\\kestrel\\a.py", win_root) == "kestrel/a.py"
    assert gate._norm_key("kestrel\\a.py", win_root) == "kestrel/a.py"


def test_deleted_changed_file_is_ignored_with_a_note(tmp_path):
    notes = []
    result = gate.diff_coverage({"kestrel/gone.py": {1, 2}}, COV, root=tmp_path, notes=notes)
    assert result[0] is None
    assert notes == ["kestrel/gone.py no longer exists; ignored"]


RENAME = """\
diff --git a/kestrel/old_name.py b/kestrel/new_name.py
similarity index 80%
rename from kestrel/old_name.py
rename to kestrel/new_name.py
--- a/kestrel/old_name.py
+++ b/kestrel/new_name.py
@@ -2 +2 @@
-x = 1
+x = 2
"""


def test_a_renamed_file_is_measured_under_its_new_path():
    assert gate.parse_diff_lines(RENAME) == {"kestrel/new_name.py": {2}}


def test_backslash_paths_match_the_same_rules_as_posix():
    bmap = _bmap()
    assert gate.browser_files_for("kestrel\\web\\pages\\zebra.py", bmap) == {
        "test_zebra_browser.py"
    }
    assert gate.is_ui_file("kestrel\\web\\static\\js\\x.js")
    run, unmapped = gate.select_browser_tests(
        ["kestrel\\web\\templates\\base.html", "kestrel\\web\\pages\\mystery.py"], bmap
    )
    assert "tests/test_nav_browser.py" in run
    assert unmapped == ["kestrel/web/pages/mystery.py"]


def test_any_module_under_web_and_the_docs_renderers_are_ui_files():
    for path in ("kestrel/web/brand_new_module.py", "kestrel/docs/templates/letter.html",
                 "kestrel/docs/letters.py", "kestrel/docs/render.py"):
        assert gate.is_ui_file(path), path
    assert not gate.is_ui_file("kestrel/docs/select.py")


def _junit(body):
    return f"<testsuites><testsuite>{body}</testsuite></testsuites>"


def test_browser_evidence_needs_a_passed_test_per_selected_file():
    xml = _junit(
        '<testcase classname="tests.test_a_browser" name="t1"/>'
        '<testcase classname="tests.test_b_browser.TestX" name="t1">'
        '<skipped message="Browser unavailable: no Edge"/></testcase>'
        '<testcase classname="tests.test_b_browser.TestX" name="t2">'
        '<skipped message="not on CI"/></testcase>'
    )
    files = ["tests/test_a_browser.py", "tests/test_b_browser.py", "tests/test_c_browser.py"]
    got = gate.browser_evidence(xml, files)
    assert set(got) == {"tests/test_b_browser.py", "tests/test_c_browser.py"}
    assert "all 2 test(s) were skipped" in got["tests/test_b_browser.py"]
    for reason in ("no Edge", "not on CI"):
        assert reason in got["tests/test_b_browser.py"]
    assert got["tests/test_c_browser.py"] == "collected no tests"


def test_describe_base_names_the_ref_date_and_warnings():
    note = gate.describe_base("origin/main", "a" * 40, "2026-10-01", stale_against="main",
                              head="a" * 40)
    assert "origin/main" in note and "2026-10-01" in note
    assert "older than local main" in note and "HEAD equals the base" in note
    plain = gate.describe_base("main", "a" * 40, "2026-11-01", stale_against=None, head="b" * 40)
    assert "WARNING" not in plain


def test_empty_diff_with_a_dirty_tree_is_a_problem():
    assert gate.empty_diff_problem([], {}, " M kestrel/a.py\n")
    assert gate.empty_diff_problem([], {}, "") is None
    assert gate.empty_diff_problem(["kestrel/a.py"], {}, " M kestrel/a.py\n") is None


def test_diff_base_reports_missing_origin_and_the_date(monkeypatch):
    def fake_git(*args):
        out = {
            ("rev-parse", "HEAD"): "h" * 40,
            ("merge-base", "HEAD", "main"): "m" * 40,
            ("show", "-s", "--format=%cs", "main"): "2026-11-02",
        }
        code = 0 if args in out else 1
        return type("P", (), {"returncode": code, "stdout": out.get(args, "") + "\n"})()

    monkeypatch.setattr(gate, "_git", fake_git)
    base, note = gate.diff_base()
    assert base == "m" * 40
    assert "origin/main is missing" in note and "main (mmmmmmmmmm" in note and "2026-11-02" in note
