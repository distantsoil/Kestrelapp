"""The stress run's selection, port holder, random order and fast clock (#188, D-CI-26)."""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look the module up while the class is built
    spec.loader.exec_module(module)
    return module


stress = _load("stress_under_test", "scripts/stress.py")

TEXTS = {
    "tests/test_alpha.py": "from kestrel.web import retry_words\n",
    "tests/test_beta.py": "import kestrel.web.retry_words_extra\n",
    "tests/test_gamma.py": "from kestrel.web.pages import (\n    alpha,\n    retry_words,\n)\n",
    "tests/test_delta.py": "path = ROOT / 'scripts' / 'check.py'\nfrom clock_seed import ago\n",
    "tests/test_epsilon.py": "def test_check():\n    assert check_everything()\n",
    # Names the leaf only outside the import: not a user of kestrel/web/retry_words.py.
    "tests/test_eta.py": "from kestrel.web import app\nWORDS = 'retry_words'\n",
    "tests/test_zeta_browser.py": "from kestrel.web import retry_words\n",
    "tests/test_clock_boundaries.py": "from kestrel.web import retry_words\n",
    "tests/clock_seed.py": "def ago(): ...\n",
    "tests/conftest.py": "",
    # The forms the real suite uses most for helpers and test modules.
    "tests/test_theta.py": "from tests import clock_seed\n",
    "tests/test_iota.py": "from tests import (\n    role_seed,\n    test_alpha,\n)\n",
    # A module named only in a monkeypatch target string.
    "tests/test_kappa.py": 'monkeypatch.setattr("kestrel.web.serve.wait_for_health", f)\n',
    # Package imports: a name the package defines, a submodule, a version attribute.
    "tests/test_lambda.py": "from kestrel.web import DEFINED_HERE\n",
    "tests/test_mu.py": "from kestrel.web import serve\n",
    "tests/test_nu.py": "import kestrel\nassert kestrel.__version__\n",
    "tests/test_xi.py": "from kestrel import paths\n",
}
APP = {
    "kestrel/web/__init__.py": "from kestrel.web.base import DEFINED_HERE\n",
    "kestrel/__init__.py": '__version__ = "0.0.0"\n',
    "kestrel/web/pages/retry_words.py": 'TEMPLATE = "partials/retry_row.html"\n',
}


def test_changed_module_selects_the_test_files_that_import_it():
    got = stress.affected_test_files(["kestrel/web/retry_words.py"], TEXTS)
    assert got == ["tests/test_alpha.py"]


def test_changed_package_module_matches_a_parenthesized_import():
    got = stress.affected_test_files(["kestrel/web/pages/retry_words.py"], TEXTS)
    assert got == ["tests/test_gamma.py"]


def test_changed_script_matches_its_file_name_never_the_bare_word():
    assert stress.affected_test_files(["scripts/check.py"], TEXTS) == ["tests/test_delta.py"]


def test_changed_helper_selects_its_users_and_a_changed_test_selects_itself():
    got = stress.affected_test_files(["tests/clock_seed.py", "tests/test_epsilon.py"], TEXTS)
    assert got == ["tests/test_delta.py", "tests/test_epsilon.py", "tests/test_theta.py"]


def test_a_changed_test_module_selects_the_tests_that_import_it():
    got = stress.affected_test_files(["tests/test_alpha.py"], TEXTS)
    assert got == ["tests/test_alpha.py", "tests/test_iota.py"]


def test_a_module_named_only_in_a_patch_target_string_is_a_user():
    assert stress.affected_test_files(["kestrel/web/serve.py"], TEXTS) == [
        "tests/test_kappa.py", "tests/test_mu.py"]


def test_a_package_init_selects_importers_of_its_own_names_not_of_its_submodules():
    assert stress.affected_test_files(["kestrel/web/__init__.py"], TEXTS, APP) == [
        "tests/test_lambda.py"]
    assert stress.affected_test_files(["kestrel/__init__.py"], TEXTS, APP) == [
        "tests/test_nu.py"]


def test_a_template_change_selects_the_tests_of_the_module_that_names_it():
    got = stress.affected_test_files(["kestrel/web/templates/partials/retry_row.html"],
                                     TEXTS, APP)
    assert got == ["tests/test_gamma.py"]


@pytest.mark.parametrize("source,names", [
    ("from kestrel.web.base import A, B as C\nX = 1\ndef f(): ...\nclass K: ...\n",
     {"A", "C", "X", "f", "K"}),
    ("import os.path\n__all__: list[str] = []\n", {"os", "__all__"}),
    ("def broken(:\n", {"*"}),
])
def test_init_names_are_what_the_package_itself_defines(source, names):
    assert stress.init_names(source) == names


def test_shared_plumbing_selects_every_stressable_test_file():
    got = stress.affected_test_files(["tests/conftest.py"], TEXTS)
    assert got == sorted(n for n in TEXTS
                         if n.startswith("tests/test_") and not n.endswith("_browser.py")
                         and n != "tests/test_clock_boundaries.py")
    assert "tests/test_alpha.py" in got and len(got) == 13


def test_browser_files_and_the_child_pytest_boundary_file_are_left_out():
    got = stress.affected_test_files(
        ["tests/test_zeta_browser.py", "tests/test_clock_boundaries.py"], TEXTS)
    assert got == []


def test_unrelated_change_selects_nothing():
    assert stress.affected_test_files(["README.md", "kestrel/other.py"], TEXTS) == []


def test_clock_seed_users_are_the_frozen_clock_set():
    files = ["tests/test_alpha.py", "tests/test_delta.py", "tests/test_theta.py"]
    assert stress.clock_seeding(files, TEXTS) == ["tests/test_delta.py", "tests/test_theta.py"]


@pytest.mark.parametrize("now,first,second", [
    (datetime(2026, 11, 12, 15, 0, tzinfo=UTC), "2026-11-12T00:00:39", "2026-11-11T23:59:30"),
    (datetime(2026, 11, 12, 0, 0, 10, tzinfo=UTC), "2026-11-11T00:00:39", "2026-11-11T23:59:30"),
    (datetime(2026, 11, 12, 23, 59, 40, tzinfo=UTC), "2026-11-12T00:00:39",
     "2026-11-12T23:59:30"),
])
def test_instants_are_the_most_recent_past_ones(now, first, second):
    got = stress.recent_instants(now)
    assert got["00:00:39 UTC"].isoformat().startswith(first)
    assert got["23:59:30 UTC"].isoformat().startswith(second)
    assert all(at <= now for at in got.values())


def test_plan_without_files_is_empty():
    assert stress.plan([], [], now=datetime(2026, 11, 12, tzinfo=UTC)) == []


def test_plan_runs_each_condition_once_before_the_repeats():
    runs = stress.plan(["tests/test_a.py"], ["tests/test_a.py"],
                       now=datetime(2026, 11, 12, 15, tzinfo=UTC), seeds=[1, 2, 3, 4, 5])
    assert [r.label for r in runs] == [
        "random order 1 of 5, seed 1", "port 8765 held, clock at 10x real speed",
        "clock from 00:00:39 UTC", "clock from 23:59:30 UTC",
        "random order 2 of 5, seed 2", "random order 3 of 5, seed 3",
        "random order 4 of 5, seed 4", "random order 5 of 5, seed 5"]
    assert [r.repeat for r in runs] == [False] * 4 + [True] * 4
    assert [r.hold_port for r in runs] == [False, True] + [False] * 6
    assert runs[1].env == {"KESTREL_STRESS_SEED": "1", "KESTREL_CLOCK_RATE": "10"}


def test_plan_without_clock_seeders_skips_the_frozen_runs():
    runs = stress.plan(["tests/test_a.py"], [], now=datetime(2026, 11, 12, tzinfo=UTC),
                       seeds=[1, 2, 3, 4, 5])
    assert not any("clock from" in r.label for r in runs) and len(runs) == 6


@pytest.mark.parametrize("elapsed,first,ok", [
    (0, 60, True), (1140, 60, True), (1141, 60, False), (600, 900, False)])
def test_a_repeat_runs_only_while_the_budget_allows(elapsed, first, ok):
    assert stress.budget_allows(elapsed, first, budget_s=1200) is ok


def test_pytest_args_run_four_workers_with_the_order_plugin_and_the_clock_plugin_when_needed():
    plain, clocked = stress.plan(["tests/test_a.py"], ["tests/test_a.py"],
                                 now=datetime(2026, 11, 12, tzinfo=UTC), seeds=[7] * 5)[:2]
    args = stress.pytest_args("py", plain)
    assert args[args.index("-n") + 1] == "4" and "tests.stress_order" in args
    assert "tests.boundary_clock" not in args and args[-1] == "tests/test_a.py"
    assert "tests.boundary_clock" in stress.pytest_args("py", clocked)


def test_port_holder_holds_the_port_and_releases_it():
    with stress.PortHolder(0) as held:
        port = held.sock.getsockname()[1]
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(OSError):
            probe.bind(("127.0.0.1", port))
        probe.close()
    assert held.sock is None


def test_port_holder_accepts_a_port_another_process_already_holds():
    other = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    other.bind(("127.0.0.1", 0))
    other.listen(1)
    try:
        with stress.PortHolder(other.getsockname()[1]) as held:
            assert held.already_taken and held.sock is None
    finally:
        other.close()


def _child_pytest(tmp_path, body: str, env: dict[str, str], *plugins: str):
    test = tmp_path / "test_probe.py"
    test.write_text(textwrap.dedent(body), encoding="utf-8")
    args = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "0"]
    for plugin in plugins:
        args += ["-p", plugin]
    return subprocess.run(
        [*args, "--rootdir", str(tmp_path), str(test)], cwd=ROOT, capture_output=True,
        text=True, encoding="utf-8", timeout=120,
        env={**{k: v for k, v in os.environ.items()
                if not k.startswith(("KESTREL_STRESS", "KESTREL_CLOCK", "KESTREL_FROZEN"))},
             "PYTHONPATH": str(ROOT), **env},
    )


ORDER_PROBE = """
    import os

    def _mark(name):
        with open(os.environ["ORDER_FILE"], "a", encoding="utf-8") as fh:
            fh.write(name)

    def test_a(): _mark("a")
    def test_b(): _mark("b")
    def test_c(): _mark("c")
    def test_d(): _mark("d")
    def test_e(): _mark("e")
"""


def _order(tmp_path, seed: str | None) -> str:
    out = tmp_path / f"order-{seed}-{len(list(tmp_path.glob('order-*')))}.txt"
    env = {"ORDER_FILE": str(out), **({"KESTREL_STRESS_SEED": seed} if seed else {})}
    proc = _child_pytest(tmp_path, ORDER_PROBE, env, "tests.stress_order")
    assert "5 passed" in proc.stdout, proc.stdout + proc.stderr
    return out.read_text(encoding="utf-8")


def test_order_plugin_without_a_seed_keeps_file_order(tmp_path):
    assert _order(tmp_path, None) == "abcde"


def test_order_plugin_shuffles_by_seed_and_the_same_seed_replays_the_order(tmp_path):
    orders = {seed: _order(tmp_path, seed) for seed in ("1", "2", "3", "4", "5")}
    assert all(sorted(o) == list("abcde") for o in orders.values())
    assert len(set(orders.values())) > 1  # different seeds, different orders
    assert _order(tmp_path, "3") == orders["3"]


CLOCK_PROBE = """
    import time
    from datetime import datetime, UTC
    import clock_seed

    def test_clock_runs_fast():
        first = clock_seed.datetime.now(UTC)
        time.sleep(0.2)
        gained = (clock_seed.datetime.now(UTC) - first).total_seconds()
        print(f"GAINED={gained:.2f}")
        assert gained >= 1.5
"""


def test_fast_clock_runs_module_clocks_at_the_given_rate(tmp_path):
    proc = _child_pytest(tmp_path, CLOCK_PROBE, {"KESTREL_CLOCK_RATE": "10",
                                                 "PYTHONPATH": f"{ROOT}{os.pathsep}"
                                                               f"{ROOT / 'tests'}"},
                         "tests.boundary_clock")
    assert "1 passed" in proc.stdout, proc.stdout + proc.stderr


def test_clock_at_real_speed_does_not_gain(tmp_path):
    proc = _child_pytest(tmp_path, CLOCK_PROBE, {"PYTHONPATH": f"{ROOT}{os.pathsep}"
                                                               f"{ROOT / 'tests'}"},
                         "tests.boundary_clock")
    assert "1 failed" in proc.stdout, proc.stdout + proc.stderr


@pytest.mark.parametrize("raw", ["fast", "0", "-2"])
def test_a_bad_clock_rate_stops_the_run_with_its_value(raw):
    clock = _load("boundary_clock_under_test", "tests/boundary_clock.py")
    with pytest.raises(pytest.UsageError, match="KESTREL_CLOCK_RATE"):
        clock._rate(raw)


def test_no_clock_rate_is_real_speed():
    clock = _load("boundary_clock_under_test", "tests/boundary_clock.py")
    assert clock._rate(None) == 1 and clock._rate("10") == 10
