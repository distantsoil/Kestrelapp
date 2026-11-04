"""The mutator and test selection of scripts/soak_mutation.py, on a tiny invented module.
Nothing here edits the real tree or touches git or the network."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "soak_mutation", ROOT / "scripts" / "soak_mutation.py"
)
sm = importlib.util.module_from_spec(_spec)
sys.modules["soak_mutation"] = sm
_spec.loader.exec_module(sm)

TINY = '''\
"""Docstring is never mutated."""


def grade(score):
    if score >= 50:
        label = "pass"
        return label
    if score == 0:
        raise ValueError("score is zero")
    return False


def flag(n):
    return 7
'''


def _by_kind(kind: str):
    return [s for s in sm.sites(ast.parse(TINY)) if s.kind == kind]


def test_every_mutation_kind_is_found():
    kinds = {s.kind for s in sm.sites(ast.parse(TINY))}
    assert kinds == {"compare", "negate", "return", "remove", "message"}


def test_compare_flips_and_reports_before_and_after():
    first = _by_kind("compare")[0]
    assert (first.before, first.after) == ("score >= 50", "score < 50")


def test_return_values_become_none_false_or_zero():
    afters = {(s.before, s.after) for s in _by_kind("return")}
    assert ("label", "None") in afters
    assert ("False", "True") in afters
    assert ("7", "0") in afters


def test_error_message_is_blanked_and_docstring_untouched():
    msg = _by_kind("message")[0]
    assert msg.after == "''" and "score is zero" in msg.before
    assert all(s.lineno > 1 for s in sm.sites(ast.parse(TINY)))


def test_mutated_source_changes_exactly_one_thing_and_still_parses():
    original = ast.unparse(ast.parse(TINY))
    all_sites = sm.sites(ast.parse(TINY))
    for i in range(len(all_sites)):
        mutant = sm.mutated_source(TINY, i)
        ast.parse(mutant)
        assert mutant.strip() != original.strip(), all_sites[i]


def test_negate_wraps_the_condition():
    idx = next(i for i, s in enumerate(sm.sites(ast.parse(TINY))) if s.kind == "negate")
    assert "not score >= 50" in sm.mutated_source(TINY, idx)


def test_remove_replaces_a_statement_in_an_if_body_with_pass():
    idx = next(i for i, s in enumerate(sm.sites(ast.parse(TINY))) if s.kind == "remove")
    assert "pass" in sm.mutated_source(TINY, idx)


def test_sites_on_lines_selects_by_line_number():
    lines = {s.lineno for s in sm.sites(ast.parse(TINY)) if s.kind == "message"}
    picked = sm.sites_on_lines(TINY, lines)
    assert picked and all(s.lineno in lines for _, s in picked)
    assert sm.sites_on_lines(TINY, {1}) == []


def test_site_keys_are_stable_between_runs():
    a = [s.key for s in sm.sites(ast.parse(TINY))]
    b = [s.key for s in sm.sites(ast.parse(TINY))]
    assert a == b and len(set(a)) == len(a)


def test_dotted_names():
    assert sm.dotted("kestrel/docs/letters.py") == "kestrel.docs.letters"
    assert sm.dotted("kestrel/docs/__init__.py") == "kestrel.docs"


def test_test_selection_by_import_by_name_and_skips_browser_files(tmp_path):
    (tmp_path / "test_a.py").write_text("from kestrel.docs.letters import x\n", encoding="utf-8")
    (tmp_path / "test_b.py").write_text(
        "from kestrel.docs import (\n    a,\n    letters,\n)\n", encoding="utf-8"
    )
    (tmp_path / "test_letters.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "test_other.py").write_text(
        "from kestrel.docs.lettersx import y\n", encoding="utf-8"
    )
    (tmp_path / "test_c_browser.py").write_text("import kestrel.docs.letters\n", encoding="utf-8")
    names = [p.name for p in sm.tests_for("kestrel/docs/letters.py", tmp_path)]
    assert names == ["test_a.py", "test_b.py", "test_letters.py"]


def _project(tmp_path: Path, test_body: str) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tiny.py").write_text(TINY, encoding="utf-8")
    (tmp_path / "tests" / "test_tiny.py").write_text(test_body, encoding="utf-8")
    return tmp_path


STRONG = """
import sys, os
sys.path.insert(0, os.getcwd())
import pytest
from tiny import grade

def test_grade():
    assert grade(60) == "pass"
    assert grade(10) is False
    with pytest.raises(ValueError, match="zero"):
        grade(0)
"""
WEAK = """
import sys, os
sys.path.insert(0, os.getcwd())
from tiny import grade

def test_runs():
    grade(10)
"""


def _kill_rate(project: Path) -> tuple[int, int]:
    killed = total = 0
    test = project / "tests" / "test_tiny.py"
    for i in range(len(sm.sites(ast.parse(TINY)))):
        (project / "tiny.py").write_text(sm.mutated_source(TINY, i), encoding="utf-8")
        result, _ = sm.run_tests(project, [test], 120)
        (project / "tiny.py").write_text(TINY, encoding="utf-8")
        total += 1
        killed += result != "pass"
    return killed, total


def test_a_strong_test_kills_more_mutants_than_a_weak_one(tmp_path):
    (tmp_path / "s").mkdir()
    (tmp_path / "w").mkdir()
    strong = _project(tmp_path / "s", STRONG)
    weak = _project(tmp_path / "w", WEAK)
    assert sm.run_tests(strong, [strong / "tests" / "test_tiny.py"], 120)[0] == "pass"
    s_killed, total = _kill_rate(strong)
    w_killed, _ = _kill_rate(weak)
    assert w_killed < s_killed <= total
    assert s_killed >= total - 2


def test_a_hung_mutant_counts_as_a_timeout(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_slow.py").write_text(
        "import time\n\ndef test_x():\n    time.sleep(30)\n", encoding="utf-8"
    )
    result, last = sm.run_tests(tmp_path, [tmp_path / "tests" / "test_slow.py"], 2)
    assert result == "timeout" and "2 seconds" in last


def test_finding_text_names_line_before_after_and_the_next_step():
    site = sm.Site(12, "compare", "x >= 1", "x < 1", lambda: None)
    f = sm.finding_for("kestrel/a.py", site)
    assert f.where == "kestrel/a.py:12"
    assert "`x >= 1` to `x < 1`" in f.rule and "Add a test" in f.action


def test_plan_reports_a_changed_module_no_test_imports(tmp_path):
    (tmp_path / "kestrel").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "kestrel" / "lonely.py").write_text(TINY, encoding="utf-8")
    todo, findings = sm.plan({"kestrel/lonely.py": {5}}, tmp_path)
    assert todo == [] and "no test file imports" in findings[0].rule
    (tmp_path / "tests" / "test_lonely.py").write_text("pass\n", encoding="utf-8")
    todo, findings = sm.plan({"kestrel/lonely.py": {5}}, tmp_path)
    assert todo and findings == []
