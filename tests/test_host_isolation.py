"""D-TEST-571, D-TEST-572 (#205): the developer's machine never reaches a test.

The shared fixtures in tests/conftest.py close the host's own Ollama port and remove the host's
KESTREL_<setting> and OLLAMA_* variables. These tests set that host state up the way a developer's
computer has it (Ollama listening, variables exported in the shell) and check that a test sees the
cloud's state instead.
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urlsplit

import pytest
from conftest import HOST_OLLAMA_PORT

# A developer's shell, as the variables would arrive: set before any test fixture runs.
_SHELL = {
    "KESTREL_LLM__OLLAMA_NATIVE_URL": "http://127.0.0.1:1",
    "KESTREL_CONFIDENTIAL_MODE": "true",
    "kestrel_log_level": "DEBUG",
    "OLLAMA_HOST": "0.0.0.0:11434",
    "OLLAMA_NUM_PARALLEL": "1",
}
# A harness switch names no setting and must survive.
_HARNESS = "KESTREL_SHOT_DIR"


@pytest.fixture(scope="module")
def developer_shell():
    """Module scoped, so it runs before every function-scoped conftest fixture, the way the real
    shell's variables are there before pytest starts."""
    saved = {k: os.environ.get(k) for k in (*_SHELL, _HARNESS)}
    os.environ.update(_SHELL)
    os.environ[_HARNESS] = "invented-shots"
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_the_hosts_settings_and_ollama_variables_are_removed(developer_shell):
    present = sorted(k for k in _SHELL if k in os.environ)
    assert present == []
    assert os.environ.get(_HARNESS) == "invented-shots"


def test_settings_read_the_shipped_values_not_the_shell(developer_shell):
    from kestrel.config import get_settings

    s = get_settings()
    assert urlsplit(s.llm.ollama_native_url).port == HOST_OLLAMA_PORT
    assert s.confidential_mode is False


def test_the_hosts_ollama_port_is_refused_even_when_something_listens():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            listener.bind(("127.0.0.1", HOST_OLLAMA_PORT))
            listener.listen(1)
        except OSError:
            pass  # the host's own Ollama holds it: something listens either way
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2)
        try:
            with pytest.raises(ConnectionRefusedError):
                client.connect(("127.0.0.1", HOST_OLLAMA_PORT))
            assert client.connect_ex(("127.0.0.1", HOST_OLLAMA_PORT)) != 0
        finally:
            client.close()
    finally:
        listener.close()


def test_other_loopback_ports_stay_open():
    """The closed port is the host's Ollama only: a fake server on a port of its own still works."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.settimeout(2)
        assert client.connect_ex(listener.getsockname()) == 0
    finally:
        client.close()
        listener.close()


def test_the_closed_port_is_the_one_the_shipped_settings_name():
    from kestrel import paths

    text = (paths.packaged_config() / "settings.yml").read_text(encoding="utf-8")
    import yaml

    url = yaml.safe_load(text)["llm"]["ollama_native_url"]
    assert urlsplit(url).port == HOST_OLLAMA_PORT


# The child interpreter below is bounded at 120 s; this test's own limit sits above it (D-TEST-797).
@pytest.mark.timeout(150)
def test_importing_the_test_harness_leaves_the_real_data_home_and_shell(tmp_path):
    """D-TEST-574: test modules and module fixtures run before any per-test fixture, so the
    harness takes the process off the developer's data home and shell as soon as it is imported."""
    import subprocess
    import sys
    from pathlib import Path

    real_home = tmp_path / "the developer's real data home"
    real_home.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    env.update(_SHELL, KESTREL_DATA_HOME=str(real_home), KESTREL_SHOT_DIR="invented-shots")
    probe = (
        "import json, os, tests.conftest\n"
        "from tests import throwaway\n"
        "from pathlib import Path\n"
        "home = os.environ['KESTREL_DATA_HOME']\n"
        "print(json.dumps({'home': home, 'marked': throwaway.is_throwaway(Path(home)),\n"
        f"    'left': sorted(k for k in {sorted(_SHELL)!r} if k in os.environ),\n"
        "    'shots': os.environ.get('KESTREL_SHOT_DIR')}))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=120, check=True,
    )
    import json

    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["home"] != str(real_home) and got["marked"] is True
    assert got["left"] == [] and got["shots"] == "invented-shots"


# The nested pytest below is bounded at 300 s; this test's own limit sits above it, so a slow
# nested run fails with TimeoutExpired instead of the 60 s default stopping the worker on
# Windows (the class e0b192d7 fixed in test_pytest_guards.py; D-TEST-797).
@pytest.mark.timeout(330)
def test_one_tests_fake_machine_never_reaches_the_next_test(tmp_path):
    """D-TEST-576, D-TEST-578, D-TEST-581: the tests in tests/host_leak_probe.py run in order in
    a pytest process of their own; each even-numbered one fails if the one before it left its
    state behind (a cached fake machine, a Setup action thread still running). The test that
    returned with its action still running is itself reported, as an error naming the action,
    and is the only one."""
    import re
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    env["COLUMNS"] = "400"  # the short summary line is cut to the terminal width
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rE", "-p", "no:cacheprovider", "-p", "no:xdist",
         "-p", "no:randomly", "--basetemp", str(tmp_path / "inner"), "tests/host_leak_probe.py"],
        cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    lines = out.stdout.strip().splitlines()
    assert out.returncode == 1 and re.fullmatch(r"4 passed, 1 error in [0-9.]+s", lines[-1]), (
        out.stdout[-2000:])
    errors = [ln for ln in lines if ln.startswith("ERROR ")]
    assert len(errors) == 1, out.stdout[-2000:]
    assert "test_three_starts_a_setup_action" in errors[0]
    assert "setup-probe" in errors[0] and "Action.join" in errors[0]


def test_git_reads_no_config_from_the_developers_home(tmp_path, monkeypatch):
    """D-TEST-577: a developer's ~/.gitconfig (here one that signs every commit) is not read by
    the git a test runs."""
    import subprocess

    (tmp_path / ".gitconfig").write_text("[commit]\n\tgpgsign = true\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    out = subprocess.run(["git", "config", "--get", "commit.gpgsign"], cwd=tmp_path,
                         capture_output=True, text=True, encoding="utf-8", check=False)
    assert out.stdout.strip() == ""


def test_the_windows_event_loop_connect_path_is_guarded_too():
    """D-TEST-579: asyncio's Proactor loop (the Windows default) connects through IOCP and never
    calls socket.connect. Its sock_connect is driven here with a stand-in proactor that would
    connect anything, on any OS: the host's Ollama port must still be refused, and a public
    address must still be stopped."""
    import asyncio
    from asyncio import proactor_events

    from conftest import OutboundNetworkInTest

    reached: list = []

    class _AnyProactor:
        async def connect(self, sock, address):
            reached.append(address)

    class _Loop:
        _proactor = _AnyProactor()
        _debug = False  # Python 3.12.13's sock_connect reads it before connecting

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        connect = proactor_events.BaseProactorEventLoop.sock_connect
        with pytest.raises(ConnectionRefusedError):
            asyncio.run(connect(_Loop(), sock, ("127.0.0.1", HOST_OLLAMA_PORT)))
        with pytest.raises(OutboundNetworkInTest):
            asyncio.run(connect(_Loop(), sock, ("203.0.113.7", 443)))
        asyncio.run(connect(_Loop(), sock, ("127.0.0.1", 9)))
    finally:
        sock.close()
    assert reached == [("127.0.0.1", 9)]


def _a_big_gpu_host(monkeypatch):
    """The developer's computer as the detector would find it: an NVIDIA card and 64 GB."""
    from kestrel.llm import profiles

    monkeypatch.setattr(profiles, "_os_name", lambda: "linux")
    monkeypatch.setattr(profiles, "_nvidia_vram_gb", lambda: 24.0)
    monkeypatch.setattr(profiles, "_ram_gb", lambda os_name: 64.0)


def test_a_test_sees_the_fixed_machine_not_the_hosts_gpu(monkeypatch):
    """D-TEST-580: hardware detection answers with the fixed test machine, whatever the host
    has."""
    from kestrel.llm import profiles
    from kestrel.services import setup_state

    _a_big_gpu_host(monkeypatch)
    setup_state.forget_hardware()
    hw = setup_state.detected_hardware()
    assert (hw.os, hw.gpu_vendor, hw.vram_gb, hw.ram_gb) == ("linux", None, None, 16.0)
    assert profiles.recommend_profile(hw)[0] == "hosted_only"


@pytest.mark.real_hardware_detection
def test_a_detector_test_opts_in_to_the_real_detector(monkeypatch):
    from kestrel.llm import profiles

    _a_big_gpu_host(monkeypatch)
    hw = profiles.detect_hardware()
    assert (hw.gpu_vendor, hw.vram_gb, hw.ram_gb) == ("nvidia", 24.0, 64.0)
