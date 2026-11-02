"""`kestrel setup` (kestrel.setup): a dry run prints every step and the command each would run
without executing anything; a missing Ollama prints the platform install command and does not run
it without --yes; --yes runs the install, pull and create commands; the exit code is non-zero when a
blocking step failed. subprocess and httpx are mocked throughout: no live Ollama, no network."""

from __future__ import annotations

import subprocess

import httpx
import pytest
import typer
from typer.testing import CliRunner

from kestrel import paths
from kestrel import setup as setup_mod
from kestrel.llm import profiles

runner = CliRunner()
root = typer.Typer()
root.add_typer(setup_mod.setup_app, name="setup")  # how cli.py mounts it

HW_4080 = profiles.Hardware(os="windows", gpu_vendor="nvidia", vram_gb=16.0, ram_gb=128.0)


class _Calls:
    def __init__(self):
        self.run: list[list[str]] = []
        self.popen: list[list[str]] = []
        self.http: list[str] = []
        self.confirms: list[str] = []


@pytest.fixture
def calls(monkeypatch):
    """Mock every side effect: subprocess.run/Popen, httpx.get, the Ollama wait, and doctor."""
    c = _Calls()

    def fake_run(cmd, **kwargs):
        c.run.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def fake_popen(cmd, **kwargs):
        c.popen.append(list(cmd))
        return None

    def fake_get(url, **kwargs):
        c.http.append(url)
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(setup_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(setup_mod.httpx, "get", fake_get)
    monkeypatch.setattr(setup_mod, "_sleep", lambda s: None)
    monkeypatch.setattr(setup_mod, "OLLAMA_START_TIMEOUT_S", 0.0)
    monkeypatch.setattr(profiles, "detect_hardware", lambda: HW_4080)
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        setup_mod.typer, "confirm", lambda q, default=False: c.confirms.append(q) or default
    )
    return c


def _ollama_answers(monkeypatch, calls: _Calls, tags: list[str] | None = None):
    """Make httpx.get report a running Ollama with the given tags pulled."""

    def fake_get(url, **kwargs):
        calls.http.append(url)
        if url.endswith("/api/version"):
            return httpx.Response(200, json={"version": "0.9.0"}, request=httpx.Request("GET", url))
        if url.endswith("/api/tags"):
            body = {"models": [{"name": t} for t in (tags or [])]}
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))
        raise AssertionError(url)

    monkeypatch.setattr(setup_mod.httpx, "get", fake_get)


# -------------------------------------------------------------------------------- dry run
def test_dry_run_prints_every_step_and_runs_nothing(calls):
    result = runner.invoke(root, ["setup", "--dry-run"])
    out = result.output
    assert result.exit_code == 0, out
    assert "dry run" in out
    assert "Hardware: windows, NVIDIA GPU 16 GB VRAM, 128 GB RAM" in out
    assert "Recommended profile: gpu_16gb" in out
    assert "winget install --id Ollama.Ollama -e" in out  # install command printed, not run
    assert "would run: ollama pull qwen3:4b-instruct-2507-q4_K_M" in out
    assert "would run: ollama pull qwen3:14b" in out
    assert "would run: ollama pull nomic-embed-text" in out
    for tier in profiles.DERIVED_TIERS:
        assert f"would run: ollama create kestrel-{tier} -f " in out
    assert "would run: write" in out and "hardware_profile.json" in out
    assert "would run: kestrel doctor" in out
    assert "would run: seed default config" in out
    assert "profile.yml" in out
    assert "Setup complete" in out
    # Nothing executed, nobody asked, nothing written.
    assert calls.run == [] and calls.popen == [] and calls.confirms == []
    assert not profiles.active_profile_path().exists()
    assert not (paths.config_dir() / "models").exists()
    assert "Traceback" not in out


def test_dry_run_with_explicit_profile_uses_it(calls):
    result = runner.invoke(root, ["setup", "--dry-run", "--profile", "mac_16gb"])
    assert result.exit_code == 0, result.output
    assert "Profile: mac_16gb (requested; recommended was gpu_16gb)" in result.output
    assert "would run: ollama pull qwen3:8b" in result.output
    assert "qwen3:14b" not in result.output


def test_unknown_profile_fails_without_a_trace(calls):
    result = runner.invoke(root, ["setup", "--dry-run", "--profile", "gpu_99gb"])
    assert result.exit_code == 1
    assert "unknown profile 'gpu_99gb'" in result.output and "gpu_16gb" in result.output
    assert "Traceback" not in result.output


# -------------------------------------------------------------------------- ollama missing
def test_missing_ollama_prints_install_command_and_does_not_run_it(calls):
    """Installing software defaults to No; without --yes the step fails, nothing runs. The
    operator first sees the three-way choice (install, hosted, quit) and takes the default."""
    # accept the recommended profile, then choice 1 (install)
    result = runner.invoke(root, ["setup"], input="\n\n")
    out = result.output
    assert result.exit_code == 1, out
    assert "Ollama not found on PATH" in out
    assert "winget install --id Ollama.Ollama -e" in out
    assert "1) Install Ollama" in out
    assert "2) Use a hosted provider instead (OpenRouter, OpenModel, Anthropic or OpenAI)" in out
    assert "3) Quit" in out
    assert any("Install Ollama now" in q for q in calls.confirms)
    assert calls.run == [] and calls.popen == []
    assert "Pull models: skipped" in out and "Build models: skipped" in out
    assert "Doctor: skipped" in out
    assert "Setup did not finish: Ollama" in out
    assert "Traceback" not in out


def test_missing_ollama_quit_choice_names_the_hosted_alternative(calls):
    result = runner.invoke(root, ["setup"], input="\n3\n")
    out = result.output
    assert result.exit_code == 1, out
    assert calls.confirms == [] and calls.run == []
    assert "kestrel setup --hosted" in out and "Setup did not finish: Ollama" in out


def test_missing_ollama_hosted_choice_switches_to_the_hosted_path(calls, monkeypatch):
    """Choice 2 drops the Ollama, pull and build steps and runs the hosted provider step; here
    the provider step stops at the key prompt (nothing typed), so nothing is written."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # profile (default), choice 2, provider (default openrouter), empty key
    result = runner.invoke(root, ["setup"], input="\n2\n\n\n")
    assert "Choice [1]: 2" in result.output
    out = result.output
    assert result.exit_code == 1, out
    assert "Profile: hosted_only (chosen because Ollama is not installed)" in out
    assert "Provider: OpenRouter" in out
    assert "no key entered" in out
    assert "Pull models" not in out and "Build models" not in out
    assert "Doctor: skipped, hosted setup did not finish" in out
    assert "Record profile: skipped" in out
    assert calls.run == [] and calls.popen == []
    assert not profiles.active_profile_path().exists()
    assert "Traceback" not in out


def test_hosted_path_is_offered_on_a_strong_machine_too(calls):
    """The hosted path is a choice on every machine, not only a fallback for weak ones."""
    result = runner.invoke(root, ["setup", "--dry-run"])
    out = result.output
    assert "Recommended profile: gpu_16gb" in out
    assert "kestrel setup --hosted" in out
    assert "hosted_only" in profiles.load_catalog().names()


@pytest.mark.parametrize(
    ("hw", "command"),
    [
        (
            profiles.Hardware(os="macos", gpu_vendor="apple", ram_gb=32.0, apple_silicon=True),
            "brew install --cask ollama",
        ),
        (
            profiles.Hardware(os="linux", gpu_vendor="nvidia", vram_gb=24.0, ram_gb=64.0),
            "curl -fsSL https://ollama.com/install.sh | sh",
        ),
    ],
)
def test_install_command_matches_the_platform(calls, monkeypatch, hw, command):
    monkeypatch.setattr(profiles, "detect_hardware", lambda: hw)
    result = runner.invoke(root, ["setup", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert command in result.output


def test_yes_runs_install_then_fails_cleanly_when_ollama_never_answers(calls):
    result = runner.invoke(root, ["setup", "--yes"])
    out = result.output
    assert result.exit_code == 1, out
    assert calls.run == [["cmd", "/c", "winget install --id Ollama.Ollama -e"]]
    assert calls.confirms == []  # --yes never prompts
    assert "installed but not answering yet" in out


# --------------------------------------------------------------------------- ollama present
def test_yes_pulls_missing_bases_builds_models_and_records_profile(calls, monkeypatch):
    _ollama_answers(
        monkeypatch, calls, tags=["qwen3:4b-instruct-2507-q4_K_M", "nomic-embed-text:latest"])
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: "/usr/bin/ollama")

    class _Report:
        has_blocking_failure = False
        results: list = []

    from kestrel import doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "run_doctor", lambda quick=False: _Report())
    monkeypatch.setattr(doctor_mod, "format_report", lambda rep: "(doctor report)")

    result = runner.invoke(root, ["setup", "--yes"])
    out = result.output
    assert result.exit_code == 0, out
    assert "Ollama 0.9.0 running" in out
    assert "qwen3:4b-instruct-2507-q4_K_M already pulled" in out
    assert "nomic-embed-text already pulled" in out
    pulls = [c for c in calls.run if c[:2] == ["ollama", "pull"]]
    assert pulls == [["ollama", "pull", "qwen3:14b"]]
    creates = [c for c in calls.run if c[:2] == ["ollama", "create"]]
    assert [c[2] for c in creates] == [f"kestrel-{t}" for t in profiles.DERIVED_TIERS]
    models_dir = paths.config_dir() / "models"
    for c in creates:
        assert c[3] == "-f" and c[4].startswith(str(models_dir))
    assert "FROM qwen3:14b" in (models_dir / "Modelfile.tier2").read_text().splitlines()
    rec = profiles.load_active_profile()
    assert rec is not None and rec.profile == "gpu_16gb" and rec.hardware["vram_gb"] == 16.0
    assert "(doctor report)" in out and "Doctor: no blocking failures" in out
    assert "Config seeded" in out and "kestrel serve" in out
    assert calls.confirms == []


def test_failed_pull_blocks_build_and_exits_non_zero(calls, monkeypatch):
    _ollama_answers(monkeypatch, calls, tags=[])
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: "/usr/bin/ollama")

    def failing_run(cmd, **kwargs):
        calls.run.append(list(cmd))
        rc = 1 if cmd[:2] == ["ollama", "pull"] else 0
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="pull failed: no network")

    monkeypatch.setattr(setup_mod.subprocess, "run", failing_run)
    result = runner.invoke(root, ["setup", "--yes"])
    out = result.output
    assert result.exit_code == 1, out
    assert "exited 1" in out
    assert not any(c[:2] == ["ollama", "create"] for c in calls.run)
    assert "Doctor: skipped" in out
    assert "Setup did not finish: Pull" in out
    assert "Traceback" not in out


def test_ollama_installed_but_stopped_is_started_after_confirmation(calls, monkeypatch):
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: "/usr/local/bin/ollama")
    answered = {"n": 0}

    def fake_get(url, **kwargs):
        calls.http.append(url)
        if url.endswith("/api/version"):
            answered["n"] += 1
            if answered["n"] >= 2:  # down on the first probe, up once `ollama serve` ran
                return httpx.Response(
                    200, json={"version": "0.9.0"}, request=httpx.Request("GET", url)
                )
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(setup_mod.httpx, "get", fake_get)
    monkeypatch.setattr(setup_mod.typer, "confirm", lambda q, default=False: default)
    st = setup_mod.SetupState(os_name="macos")
    assert setup_mod.step_ollama(st) is True
    assert calls.popen == [["ollama", "serve"]]
    assert st.failed == []
