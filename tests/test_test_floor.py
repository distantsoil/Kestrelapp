"""D-TEST-8 anti-gaming checks (scripts/test_floor.py), each fed the broken input it exists to
catch: a test file that lost asserts, a gate file changed without a decision, tests written to
pass, and a fix commit whose tests also pass on the old code."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("test_floor", ROOT / "scripts" / "test_floor.py")
tf = importlib.util.module_from_spec(_spec)
sys.modules["test_floor"] = tf
_spec.loader.exec_module(tf)

GOOD = """\
import pytest

def test_one():
    assert 1 + 1 == 2
    assert "a" in "abc"

def test_two():
    with pytest.raises(ValueError):
        int("x")
"""


# ---- assertion floor ---------------------------------------------------------------------------


def test_file_stats_counts_asserts_raises_tests_and_skips():
    src = (
        GOOD
        + """
@pytest.mark.skip(reason="x")
def test_three():
    pytest.skip("later")
    assert True

@pytest.mark.skipif(True, reason="y")
@pytest.mark.xfail
async def test_four():
    assert 1
"""
    )
    stats = tf.file_stats(src)
    assert (stats.asserts, stats.tests, stats.skips) == (5, 4, 4)


def test_losing_an_assert_is_reported_with_file_counts_and_how_to_justify():
    weaker = GOOD.replace('    assert "a" in "abc"\n', "")
    problems = tf.floor_problems({"tests/test_a.py": GOOD}, {"tests/test_a.py": weaker}, set())
    assert len(problems) == 1
    msg = problems[0]
    assert "assertion floor" in msg and "tests/test_a.py" in msg
    assert "3 asserts" in msg and "2 asserts" in msg
    assert "decision ID" in msg and "tests/.removals.toml" in msg


def test_losing_a_test_or_gaining_a_skip_is_reported():
    fewer = GOOD.split("def test_two")[0]
    assert tf.floor_problems({"tests/test_a.py": GOOD}, {"tests/test_a.py": fewer}, set())
    skipped = GOOD.replace("def test_one", "@pytest.mark.skip\ndef test_one")
    assert tf.floor_problems({"tests/test_a.py": GOOD}, {"tests/test_a.py": skipped}, set())


def test_a_deleted_test_file_is_reported():
    problems = tf.floor_problems({"tests/test_a.py": GOOD}, {"tests/test_a.py": None}, set())
    assert problems and "deleted" in problems[0]


def test_more_proof_or_a_new_file_passes():
    stronger = GOOD + "\ndef test_three():\n    assert 3\n"
    assert tf.floor_problems({"tests/test_a.py": GOOD}, {"tests/test_a.py": stronger}, set()) == []
    assert tf.floor_problems({"tests/test_a.py": None}, {"tests/test_a.py": GOOD}, set()) == []


def test_a_decision_id_on_a_line_naming_the_file_justifies_the_drop():
    weaker = GOOD.replace('    assert "a" in "abc"\n', "")
    messages = "test: trim\n\nD-TEST-9: tests/test_a.py duplicated test_b.py\n"
    just = tf.justified_paths(messages, ["tests/test_a.py", "tests/test_c.py"])
    assert just == {"tests/test_a.py"}
    assert tf.floor_problems({"tests/test_a.py": GOOD}, {"tests/test_a.py": weaker}, just) == []


def test_an_id_and_a_file_on_different_lines_do_not_justify():
    messages = "D-TEST-9: something else\nremoved tests/test_a.py\n"
    assert tf.justified_paths(messages, ["tests/test_a.py"]) == set()


def test_removals_file_needs_a_decision_id_and_a_reason():
    good = '[[removal]]\nfile = "tests/test_a.py"\ndecision = "D-TEST-9"\nreason = "moved"\n'
    assert tf.read_removals(good) == {"tests/test_a.py": "D-TEST-9"}
    no_reason = '[[removal]]\nfile = "tests/test_a.py"\ndecision = "D-TEST-9"\nreason = " "\n'
    no_id = '[[removal]]\nfile = "tests/test_a.py"\ndecision = "later"\nreason = "moved"\n'
    assert tf.read_removals(no_reason) == {} and tf.read_removals(no_id) == {}
    assert tf.read_removals("not [ toml") == {}


# ---- protected paths ---------------------------------------------------------------------------

PYPROJECT = "[tool.pytest.ini_options]\ntimeout = 60\n[tool.ruff]\nline-length = 100\n"


def test_changing_a_gate_file_without_a_decision_fails_naming_the_path():
    problems = tf.protected_problems(
        ["scripts/check.py", ".github/workflows/ci.yml", "README.md"], {}, {}, "feat: a thing\n"
    )
    assert len(problems) == 2
    text = "\n".join(problems)
    assert "scripts/check.py" in text and ".github/workflows/ci.yml" in text
    assert all("decision ID" in p for p in problems) and "README.md" not in text


def test_a_decision_line_naming_the_path_allows_the_change():
    messages = "chore: gates\n\nD-TEST-8: scripts/check.py gains tiers\n"
    assert tf.protected_problems(["scripts/check.py"], {}, {}, messages) == []


def test_a_decision_id_without_the_path_does_not_allow_the_change():
    assert tf.protected_problems(["CLAUDE.md"], {}, {}, "D-TEST-8: unrelated\n")


@pytest.mark.parametrize(
    "path",
    [
        "tests/conftest.py",
        "tests/browser_plugin.py",
        "tests/browser_map.toml",
        "scripts/diff_gate.py",
        "scripts/test_floor.py",
        ".gitignore",
        "AGENTS.md",
        "tests/.removals.toml",
    ],
)
def test_every_listed_gate_file_is_protected(path):
    assert len(tf.protected_problems([path], {}, {}, "")) == 1


def test_pyproject_is_protected_only_for_pytest_and_ruff_sections():
    base = {"pyproject.toml": PYPROJECT}
    deps = PYPROJECT + '[project]\nname = "x"\n'
    assert tf.protected_problems(["pyproject.toml"], base, {"pyproject.toml": deps}, "") == []
    looser = PYPROJECT.replace("timeout = 60", "timeout = 6000")
    problems = tf.protected_problems(["pyproject.toml"], base, {"pyproject.toml": looser}, "")
    assert len(problems) == 1 and "pyproject.toml" in problems[0]
    ruff = PYPROJECT.replace("100", "300")
    assert tf.protected_problems(["pyproject.toml"], base, {"pyproject.toml": ruff}, "")


def test_contract_allowlists_are_protected_but_other_edits_to_the_file_are_not():
    base = (
        'HIDDEN_CONFIRMED_ALLOWED = {"a.html": "why"}\nOTHER = 1\n\ndef test_x():\n    assert 1\n'
    )
    grown = base.replace('"a.html": "why"', '"a.html": "why", "b.html": "because"')
    key = "tests/test_contracts.py"
    assert tf.protected_problems([key], {key: base}, {key: grown}, "")
    harmless = base.replace("OTHER = 1", "OTHER = 2")
    assert tf.protected_problems([key], {key: base}, {key: harmless}, "") == []


# ---- test-quality lint -------------------------------------------------------------------------

BAD_TESTS = """\
import inspect

def test_conditional(x=1):
    if x:
        assert x == 1

def test_source():
    from kestrel import paths
    src = inspect.getsource(paths)
    assert "def" in src

def test_inline_source():
    import kestrel
    assert "x" in inspect.getsource(kestrel)

def test_always():
    assert True

def test_nothing():
    value = 1 + 1
    print(value)

def test_either(x=1):
    assert x == 1 or x == 2

def test_fine():
    assert 1 == 1
"""


def _rules(findings):
    return sorted((f.rule, f.line) for f in findings)


def test_each_test_quality_rule_fires_on_its_broken_input():
    findings = tf.quality_findings("tests/test_bad.py", BAD_TESTS, None)
    rules = {f.rule for f in findings}
    assert rules == {
        "conditional assert",
        "source-text assert",
        "assert True",
        "test without assertion",
        "assert with or",
    }
    by_rule = {f.rule: f for f in findings}
    assert by_rule["conditional assert"].line == 5
    assert by_rule["assert with or"].warning is True
    assert not any(f.warning for f in findings if f.rule != "assert with or")
    source = [f for f in findings if f.rule == "source-text assert"]
    assert [f.line for f in source] == [10, 14]
    assert all(f.line != 1 and "test_fine" not in f.message for f in findings)


def test_findings_name_rule_file_line_and_fix():
    text = str(tf.quality_findings("tests/test_bad.py", BAD_TESTS, None)[0])
    assert text.startswith("RULE ") and "tests/test_bad.py:" in text and "Fix:" in text


def test_only_findings_on_changed_lines_count_for_an_existing_file():
    assert tf.quality_findings("tests/test_bad.py", BAD_TESTS, {30}) == []
    on_always = tf.quality_findings("tests/test_bad.py", BAD_TESTS, {17})
    assert _rules(on_always) == [("assert True", 17)]


def test_helpers_playwright_expect_and_raises_count_as_assertions():
    src = """\
import pytest

def _assert_page(p):
    assert p

def test_helper():
    _assert_page(1)

def test_expect(page):
    expect(page).to_have_title("x")

def test_raises():
    with pytest.raises(ValueError):
        int("x")

def test_mock(m):
    m.assert_called_once()
"""
    assert tf.quality_findings("tests/test_ok.py", src, None) == []


# ---- red-first proof ---------------------------------------------------------------------------


def test_verdicts():
    assert tf.red_first_verdict(1, 0) is None
    assert tf.red_first_verdict(2, None) is None
    assert "PASSES on the parent" in tf.red_first_verdict(0, None)
    assert "no tests" in tf.red_first_verdict(5, None)
    assert "also fail on the fix" in tf.red_first_verdict(1, 1)
    assert tf.red_first_verdict(3, None)


def test_fix_subjects_and_failed_ids():
    assert tf.is_fix_subject("fix: x") and tf.is_fix_subject("fix(web): x")
    assert not tf.is_fix_subject("feat: fix x") and not tf.is_fix_subject("test: fix")
    out = "FAILED tests/test_a.py::test_x - assert 1 == 2\nERROR tests/test_b.py::test_y\n"
    assert tf.failed_test_ids(out) == ["tests/test_a.py::test_x", "tests/test_b.py::test_y"]


def _git(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "PATH": _path(),
    }
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


def _path() -> str:
    import os

    return os.environ["PATH"]


@pytest.fixture
def toy(tmp_path):
    repo = tmp_path / "toy"
    (repo / "tests").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "INSTRUCTIONS.md").write_text("private\n", encoding="utf-8")  # a private tree
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "tests" / "test_old.py").write_text("def test_old():\n    assert 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: start")
    return repo


def test_a_fix_commit_whose_test_fails_on_the_parent_is_proved(toy):
    base = _git(toy, "rev-parse", "HEAD")
    (toy / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (toy / "tests" / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8"
    )
    _git(toy, "add", "-A")
    _git(toy, "commit", "-q", "-m", "fix: add subtracted")
    logs: list[str] = []
    problems, warnings = tf.red_first_problems(base, log=logs.append, repo=toy)
    assert problems == [] and warnings == []
    assert any("ok" in line for line in logs)


def test_a_fix_commit_that_also_changes_a_data_file_under_tests_is_still_proved(toy):
    """A fix commit often touches a non-test file in tests/ too (a .toml map, a fixture). Those
    files are checked out for the run but never handed to pytest as test targets, which would end
    the run with a usage error (status 4) instead of the real verdict."""
    base = _git(toy, "rev-parse", "HEAD")
    (toy / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (toy / "tests" / "map.toml").write_text("[pages]\n", encoding="utf-8")
    (toy / "tests" / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8"
    )
    _git(toy, "add", "-A")
    _git(toy, "commit", "-q", "-m", "fix: add subtracted")
    logs: list[str] = []
    problems, _warnings = tf.red_first_problems(base, log=logs.append, repo=toy)
    assert problems == [], problems
    assert any("ok (a new test fails on the parent)" in line for line in logs)


def test_a_fix_commit_proved_only_by_a_browser_test_runs_that_test_live(toy):
    """Browser tests skip unless KESTREL_LIVE_BROWSER=1. Run without it, the new test skips on
    the parent and the skip reads as a pass, so a real browser fix fails the proof."""
    base = _git(toy, "rev-parse", "HEAD")
    (toy / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (toy / "tests" / "test_calc_browser.py").write_text(
        "import os\n\nimport pytest\n\nfrom calc import add\n\n"
        "pytestmark = pytest.mark.skipif(\n"
        "    os.environ.get('KESTREL_LIVE_BROWSER') != '1', reason='needs a live browser'\n)\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    _git(toy, "add", "-A")
    _git(toy, "commit", "-q", "-m", "fix: add subtracted")
    logs: list[str] = []
    problems, _warnings = tf.red_first_problems(base, log=logs.append, repo=toy)
    assert problems == [], problems
    assert any("ok (a new test fails on the parent)" in line for line in logs)


def test_a_fix_commit_whose_test_passes_on_the_parent_fails_the_proof(toy):
    base = _git(toy, "rev-parse", "HEAD")
    (toy / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (toy / "tests" / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add_exists():\n    assert add\n", encoding="utf-8"
    )
    _git(toy, "add", "-A")
    _git(toy, "commit", "-q", "-m", "fix: add subtracted")
    problems, _warnings = tf.red_first_problems(base, log=lambda _l: None, repo=toy)
    assert len(problems) == 1
    assert "red-first" in problems[0] and "PASSES on the parent" in problems[0]
    assert "Fix:" in problems[0]


def test_non_fix_commits_and_fixes_without_tests_are_not_checked(toy):
    base = _git(toy, "rev-parse", "HEAD")
    (toy / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(toy, "commit", "-q", "-am", "fix: no test")
    (toy / "tests" / "test_new.py").write_text("def test_n():\n    assert 1\n", encoding="utf-8")
    _git(toy, "add", "-A")
    _git(toy, "commit", "-q", "-m", "test: more")
    problems, warnings = tf.red_first_problems(base, log=lambda _l: None, repo=toy)
    assert problems == [] and warnings == []


def test_going_over_the_time_budget_warns_naming_the_unchecked_commits(toy):
    base = _git(toy, "rev-parse", "HEAD")
    (toy / "tests" / "test_calc.py").write_text("def test_a():\n    assert 0\n", encoding="utf-8")
    _git(toy, "add", "-A")
    _git(toy, "commit", "-q", "-m", "fix: skipped for time")
    problems, warnings = tf.red_first_problems(base, budget=-1, log=lambda _l: None, repo=toy)
    assert problems == []
    assert len(warnings) == 1 and "fix: skipped for time" in warnings[0]
    assert "NOT checked" in warnings[0]


# ---- the whole static run against a real repository --------------------------------------------


def test_run_static_finds_floor_protected_and_quality_problems(toy, monkeypatch):
    (toy / "scripts").mkdir()
    (toy / "scripts" / "check.py").write_text("x = 1\n", encoding="utf-8")
    (toy / "tests" / "test_old.py").write_text(
        "def test_old():\n    assert 1\n    assert 2\n", encoding="utf-8"
    )
    _git(toy, "add", "-A")
    _git(toy, "commit", "-q", "-m", "chore: more")
    base = _git(toy, "rev-parse", "HEAD")
    monkeypatch.setattr(tf, "ROOT", toy)
    # branch work: weaken the test file, change a gate, add a bad new test
    (toy / "tests" / "test_old.py").write_text("def test_old():\n    pass\n", encoding="utf-8")
    (toy / "scripts" / "check.py").write_text("x = 2\n", encoding="utf-8")
    (toy / "tests" / "test_new.py").write_text(
        "def test_n(v=0):\n    if v:\n        assert v\n", encoding="utf-8"
    )
    changed = {"tests/test_old.py": {1, 2}, "scripts/check.py": {1}, "tests/test_new.py": {1, 2, 3}}
    problems, _warnings = tf.run_static(base, changed, sorted(changed))
    text = "\n".join(problems)
    assert "assertion floor: tests/test_old.py" in text
    assert "protected path: scripts/check.py" in text
    assert "conditional assert: tests/test_new.py:3" in text
    assert "test without assertion: tests/test_old.py:1" in text


def test_run_static_accepts_a_branch_that_only_adds_proof(toy, monkeypatch):
    base = _git(toy, "rev-parse", "HEAD")
    monkeypatch.setattr(tf, "ROOT", toy)
    (toy / "tests" / "test_old.py").write_text(
        "def test_old():\n    assert 1 == 1\n    assert 2 == 2\n", encoding="utf-8"
    )
    _git(toy, "add", "-A")
    _git(toy, "commit", "-q", "-m", "test: stronger")
    assert tf.run_static(base, {"tests/test_old.py": {1, 2, 3}}, ["tests/test_old.py"]) == ([], [])


# ---- the public export skips only the history checks -----------------------------------------


def test_the_public_export_is_detected_by_the_missing_instructions_file(tmp_path):
    assert tf.public_export_note(tmp_path) is not None
    (tmp_path / "INSTRUCTIONS.md").write_text("x", encoding="utf-8")
    assert tf.public_export_note(tmp_path) is None


def test_this_tree_is_classed_by_its_own_marker_file():
    """The private tree has INSTRUCTIONS.md and runs the history checks; the public export does
    not and skips them. The same test passes in both repositories."""
    private = (tf.ROOT / "INSTRUCTIONS.md").is_file()
    assert (tf.public_export_note() is None) == private


def test_red_first_classes_the_repository_it_is_given_not_the_one_it_runs_from(tmp_path):
    """A repository without the marker is the public export even when the gate itself runs from
    the private tree, and the other way round, so the toy repositories behave the same in both."""
    repo = tmp_path / "public"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: start")
    problems, warnings = tf.red_first_problems("HEAD", repo=repo)
    assert problems == []
    assert len(warnings) == 1 and "public export" in warnings[0]


def test_on_the_public_export_history_checks_are_skipped_and_the_rest_still_runs(monkeypatch):
    bad = "def test_conditional(x=1):\n    if x:\n        assert x\n"
    monkeypatch.setattr(tf, "public_export_note", lambda root=None: "NOTE public export")

    def _history(*a, **k):
        raise AssertionError("a history check ran on the public export")

    for name in ("floor_problems", "protected_problems", "branch_messages", "_git"):
        monkeypatch.setattr(tf, name, _history)
    monkeypatch.setattr(tf, "_read", lambda path: bad)
    problems, warnings = tf.run_static("origin/main", {"tests/test_x.py": {1, 2, 3}}, [])
    assert warnings[0] == "NOTE public export"
    assert any("conditional assert" in p and "tests/test_x.py" in p for p in problems)


def test_red_first_is_skipped_on_the_public_export(monkeypatch, tmp_path):
    monkeypatch.setattr(tf, "ROOT", tmp_path)  # a tree without INSTRUCTIONS.md
    problems, warnings = tf.red_first_problems("origin/main", repo=tmp_path)
    assert problems == []
    assert len(warnings) == 1 and "public export" in warnings[0]


# ---- changelog guard (D-CI-17) -----------------------------------------------------------------

VER = "kestrel/__init__.py"
APP = "kestrel/app.py"
LOG = "CHANGELOG.md"


@pytest.fixture
def release_repo(tmp_path):
    repo = tmp_path / "release"
    (repo / "kestrel").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "INSTRUCTIONS.md").write_text("private\n", encoding="utf-8")
    (repo / VER).write_text('__version__ = "1.0.0"\n', encoding="utf-8")
    (repo / APP).write_text("x = 1\n", encoding="utf-8")
    (repo / LOG).write_text("## [1.0.0] - 2026-01-01\n\n- Old note\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: start")
    return repo


def _commit(repo: Path, subject: str, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", subject)


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


BUMP = {VER: '__version__ = "1.1.0"\n'}
NOTES_110 = {LOG: "## [1.1.0] - 2026-02-01\n\n- **Score again** says what happened.\n"
                  "\n## [1.0.0] - 2026-01-01\n\n- Old note\n"}
NOTES_UNRELEASED = {LOG: "## [Unreleased]\n\n- **Score again** says what happened.\n"
                         "\n## [1.0.0] - 2026-01-01\n\n- Old note\n"}


def test_work_then_bump_without_notes_is_flagged(release_repo):
    base = _head(release_repo)
    _commit(release_repo, "fix(web): score again", {APP: "x = 2\n"})
    _commit(release_repo, "chore: bump", BUMP)
    problems = tf.changelog_problems(base, repo=release_repo)
    assert problems and all("RULE changelog" in p for p in problems)
    assert "fix(web): score again" in problems[0]
    assert "[1.1.0] section of CHANGELOG.md" in problems[0]


def test_work_then_bump_with_notes_passes(release_repo):
    base = _head(release_repo)
    _commit(release_repo, "fix(web): score again", {APP: "x = 2\n"})
    _commit(release_repo, "chore: bump", {**BUMP, **NOTES_110})
    assert tf.changelog_problems(base, repo=release_repo) == []


def test_work_then_notes_then_bump_passes(release_repo):
    base = _head(release_repo)
    _commit(release_repo, "fix: score again", {APP: "x = 2\n"})
    _commit(release_repo, "docs: notes", NOTES_UNRELEASED)
    _commit(release_repo, "chore: bump", {**BUMP, **NOTES_110})
    assert tf.changelog_problems(base, repo=release_repo) == []


def test_bump_notes_then_a_later_fix_is_flagged(release_repo):
    base = _head(release_repo)
    _commit(release_repo, "chore: bump", {**BUMP, **NOTES_110})
    _commit(release_repo, "fix: one more thing", {APP: "x = 3\n"})
    problems = tf.changelog_problems(base, repo=release_repo)
    assert len(problems) == 1 and "after the last CHANGELOG.md edit" in problems[0]
    assert "one more thing" in problems[0]


def test_a_changelog_edit_that_adds_no_bullet_does_not_count(release_repo):
    base = _head(release_repo)
    _commit(release_repo, "fix: score again", {APP: "x = 2\n"})
    empty = "## [1.1.0]\n\n## [1.0.0]\n\n- Old note\n"
    _commit(release_repo, "chore: bump", {**BUMP, LOG: empty})
    assert tf.changelog_problems(base, repo=release_repo)


def test_no_bump_and_a_fix_without_an_unreleased_note_is_flagged(release_repo):
    base = _head(release_repo)
    _commit(release_repo, "fix: quiet", {APP: "x = 2\n"})
    problems = tf.changelog_problems(base, repo=release_repo)
    assert len(problems) == 1 and "[Unreleased] section" in problems[0]


def test_no_bump_with_an_unreleased_note_passes(release_repo):
    base = _head(release_repo)
    _commit(release_repo, "feat: new page", {APP: "x = 2\n", **NOTES_UNRELEASED})
    assert tf.changelog_problems(base, repo=release_repo) == []


def test_no_bump_a_note_in_an_old_release_section_is_not_an_unreleased_note(release_repo):
    base = _head(release_repo)
    _commit(release_repo, "fix: quiet", {APP: "x = 2\n"})
    older = "## [1.0.0] - 2026-01-01\n\n- Old note\n- More\n"
    _commit(release_repo, "docs: edit old", {LOG: older})
    assert tf.changelog_problems(base, repo=release_repo)


def test_commits_that_are_not_user_facing_are_ignored(release_repo):
    base = _head(release_repo)
    _commit(release_repo, "test: more", {"tests/test_x.py": "def test_x():\n    assert 1\n"})
    _commit(release_repo, "fix: tooling only", {"scripts/tool.py": "y = 1\n"})
    _commit(release_repo, "chore: tidy", {APP: "x = 4\n"})
    _commit(release_repo, "chore: bump", BUMP)
    assert tf.changelog_problems(base, repo=release_repo) == []


def test_the_bump_commit_itself_is_not_held_to_the_changelog_rule(release_repo):
    base = _head(release_repo)
    _commit(release_repo, "feat: release 1.1.0", BUMP)
    assert tf.changelog_problems(base, repo=release_repo) == []


def test_the_changelog_guard_is_skipped_on_the_public_export(release_repo):
    base = _head(release_repo)
    _commit(release_repo, "fix: x", {APP: "x = 2\n"})
    (release_repo / "INSTRUCTIONS.md").unlink()
    assert tf.changelog_problems(base, repo=release_repo) == []


def test_run_static_reports_the_changelog_problem(release_repo, monkeypatch):
    base = _head(release_repo)
    _commit(release_repo, "fix: x", {APP: "x = 2\n"})
    monkeypatch.setattr(tf, "ROOT", release_repo)
    problems, _warnings = tf.run_static(base, {}, [APP])
    assert any("RULE changelog" in p for p in problems)


def test_a_note_that_was_already_on_the_base_does_not_cover_a_new_fix(release_repo):
    _commit(release_repo, "docs: earlier note", {LOG: "## [Unreleased]\n\n- Earlier note\n"})
    base = _head(release_repo)
    _commit(release_repo, "fix: new thing", {APP: "x = 5\n"})
    problems = tf.changelog_problems(base, repo=release_repo)
    assert len(problems) == 1 and "new thing" in problems[0]
