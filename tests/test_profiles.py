"""Hardware profiles (kestrel.llm.profiles): the packaged models.yml loads and is complete, each
hardware shape maps to the right profile, render_modelfile is what ships, the active profile round
trips, and doctor's fit check WARNS (never FAILs) with the profile to switch to."""

from __future__ import annotations

import subprocess

import pytest

from kestrel.config import get_settings
from kestrel.llm import profiles as p

LOCAL_PROFILES = {"cpu_8gb", "gpu_12gb", "gpu_16gb", "gpu_24gb", "mac_16gb", "mac_32gb", "mac_64gb"}
PROFILE_NAMES = LOCAL_PROFILES | {"hosted_only"}
SHIPPED_NUM_CTX = {"tier1": 4096, "tier2": 16384, "tier3": 20480, "docsel": 8192}


# ---------------------------------------------------------------------------------- catalog
def test_catalog_loads_with_every_profile_and_shipped_budgets():
    cat = p.load_catalog()
    assert set(cat.names()) == PROFILE_NAMES
    assert cat.default_profile == "gpu_16gb"
    for tier, ctx in SHIPPED_NUM_CTX.items():
        assert cat.num_ctx.for_tier(tier) == ctx
    for name, prof in cat.profiles.items():
        for tier, ctx in SHIPPED_NUM_CTX.items():
            assert prof.num_ctx.for_tier(tier) >= ctx, name
        if prof.hosted:
            continue
        assert prof.models.embed == "nomic-embed-text"
        assert prof.models.tier3 == prof.models.tier2, name
        assert prof.models.docsel == prof.models.tier1, name
        assert prof.concurrency.tier1 >= prof.concurrency.tier2
        for tag in prof.models.base_tags():
            assert tag in cat.model_sizes_gb, f"{name}: {tag} missing from model_sizes_gb"
    g = cat.guardrails
    assert (g.temperature, g.top_p, g.repeat_penalty, g.seed) == (0.1, 0.9, 1.1, 7)
    assert g.num_predict.model_dump() == {
        "tier1": 400,
        "tier2": 4500,
        "tier3": 6500,
        "docsel": 1600,
    }


def test_hosted_only_profile_has_no_local_model_and_nothing_to_build():
    """hosted_only: every tier null (nothing to pull), concurrency 2/2, a note, and rendering a
    Modelfile for it is a clear ValueError, never a `FROM None`."""
    prof = p.load_catalog().profile(p.HOSTED_PROFILE)
    assert prof.hosted and prof.kind == "hosted" and prof.memory_gb == 0
    assert prof.models.base_tags() == []
    assert all(prof.models.for_tier(t) is None for t in (*p.TIERS, p.EMBED_TIER))
    assert (prof.concurrency.tier1, prof.concurrency.tier2) == (2, 2)
    assert "few-shot" in prof.note and "reranker" in prof.note
    assert "GPU" in prof.description
    with pytest.raises(ValueError, match="hosted provider"):
        p.render_modelfile("tier1", p.HOSTED_PROFILE)
    # every local profile still names every tier
    for name in LOCAL_PROFILES:
        local = p.load_catalog().profile(name)
        assert not local.hosted and len(local.models.base_tags()) >= 2, name


def test_reference_profile_matches_the_rtx_4080_super():
    prof = p.load_catalog().profile("gpu_16gb")
    assert prof.models.tier1 == "qwen3:4b" and prof.models.tier2 == "qwen3:14b"
    assert (prof.concurrency.tier1, prof.concurrency.tier2) == (4, 2)


def test_unknown_profile_is_a_keyerror_naming_the_known_ones():
    with pytest.raises(KeyError, match="gpu_16gb"):
        p.load_catalog().profile("gpu_999gb")


# ------------------------------------------------------------------------------- recommend
@pytest.mark.parametrize(
    ("hw", "expected"),
    [
        (p.Hardware(os="windows", gpu_vendor="nvidia", vram_gb=16.0, ram_gb=128.0), "gpu_16gb"),
        (p.Hardware(os="windows", gpu_vendor="nvidia", vram_gb=15.9, ram_gb=32.0), "gpu_16gb"),
        (p.Hardware(os="linux", gpu_vendor="nvidia", vram_gb=24.0, ram_gb=64.0), "gpu_24gb"),
        (p.Hardware(os="linux", gpu_vendor="nvidia", vram_gb=12.0, ram_gb=32.0), "gpu_12gb"),
        # under 11 GB of VRAM: hosted at any RAM (D-SETUP-6)
        (p.Hardware(os="windows", gpu_vendor="nvidia", vram_gb=8.0, ram_gb=32.0), "hosted_only"),
        (p.Hardware(os="windows", gpu_vendor="nvidia", vram_gb=8.0, ram_gb=16.0), "hosted_only"),
        (p.Hardware(os="windows", gpu_vendor="nvidia", vram_gb=4.0, ram_gb=8.0), "hosted_only"),
        (p.Hardware(os="linux", gpu_vendor="nvidia", vram_gb=6.0, ram_gb=None), "hosted_only"),
        # no supported GPU: hosted at any RAM (D-SETUP-6)
        (p.Hardware(os="windows", gpu_vendor=None, vram_gb=None, ram_gb=64.0), "hosted_only"),
        (p.Hardware(os="windows", gpu_vendor=None, vram_gb=None, ram_gb=16.0), "hosted_only"),
        (p.Hardware(os="macos", gpu_vendor="apple", ram_gb=16.0, apple_silicon=True), "mac_16gb"),
        (p.Hardware(os="macos", gpu_vendor="apple", ram_gb=36.0, apple_silicon=True), "mac_32gb"),
        (p.Hardware(os="macos", gpu_vendor="apple", ram_gb=64.0, apple_silicon=True), "mac_64gb"),
        (p.Hardware(os="macos", gpu_vendor="apple", ram_gb=128.0, apple_silicon=True), "mac_64gb"),
        (p.Hardware(os="macos", gpu_vendor="apple", ram_gb=8.0, apple_silicon=True), "hosted_only"),
        (p.Hardware(os="macos", gpu_vendor=None, ram_gb=32.0, apple_silicon=False), "hosted_only"),
        (p.Hardware(os="windows", gpu_vendor=None, vram_gb=None, ram_gb=8.0), "hosted_only"),
        (p.Hardware(os="windows", gpu_vendor=None, vram_gb=None, ram_gb=15.9), "hosted_only"),
        (p.Hardware(os="linux", gpu_vendor=None, vram_gb=None, ram_gb=None), "hosted_only"),
        (p.Hardware(os="unknown"), "hosted_only"),
    ],
)
def test_recommend_profile_for_each_hardware_shape(hw, expected):
    name, reason = p.recommend_profile(hw)
    assert name == expected
    assert name in p.load_catalog().profiles
    assert reason
    if expected == "hosted_only":
        assert reason.endswith("use a hosted provider")
        if hw.gpu_vendor == "nvidia":
            assert reason.startswith(f"NVIDIA GPU with {hw.vram_gb:g} GB VRAM: too little")


# ---------------------------------------------------------------------------------- detect
def _fake_run(table: dict[str, str | None]):
    """A subprocess.run stand-in keyed by the first token; None means the binary is missing."""

    def run(cmd, **kwargs):
        out = table.get(cmd[0])
        if out is None:
            raise FileNotFoundError(cmd[0])
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    return run


def test_detect_nvidia_on_windows_reads_vram_and_ram(monkeypatch):
    monkeypatch.setattr(p.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        p.subprocess,
        "run",
        _fake_run({"nvidia-smi": "16376\n", "powershell": "137438953472\n"}),
    )
    monkeypatch.setattr(p, "_read_text", lambda path: None)
    hw = p.detect_hardware()
    assert hw.gpu_vendor == "nvidia" and hw.vram_gb == 16.0 and hw.ram_gb == 128.0
    assert hw.os == "windows" and not hw.apple_silicon
    assert p.recommend_profile(hw)[0] == "gpu_16gb"


def test_detect_apple_silicon_uses_unified_memory(monkeypatch):
    monkeypatch.setattr(p.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        p.subprocess,
        "run",
        _fake_run({"sysctl": "34359738368\n", "uname": "arm64\n"}),
    )
    hw = p.detect_hardware()
    assert hw.apple_silicon and hw.gpu_vendor == "apple"
    assert hw.ram_gb == 32.0 and hw.vram_gb == 32.0
    assert p.recommend_profile(hw)[0] == "mac_32gb"


def test_detect_linux_without_gpu_never_raises(monkeypatch):
    monkeypatch.setattr(p.platform, "system", lambda: "Linux")
    monkeypatch.setattr(p.subprocess, "run", _fake_run({}))  # nothing on PATH
    monkeypatch.setattr(p, "_read_text", lambda path: "MemTotal:       65536000 kB\n")
    hw = p.detect_hardware()
    assert hw.os == "linux" and hw.gpu_vendor is None and hw.vram_gb is None
    assert hw.ram_gb == 62.5
    name, reason = p.recommend_profile(hw)
    assert name == "hosted_only" and "CPU" in reason and "62.5 GB RAM" in reason


def test_detect_survives_a_subprocess_timeout(monkeypatch):
    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(p.platform, "system", lambda: "Linux")
    monkeypatch.setattr(p.subprocess, "run", boom)
    monkeypatch.setattr(p, "_read_text", lambda path: None)
    monkeypatch.delattr(p.os, "sysconf", raising=False)
    hw = p.detect_hardware()
    assert hw.ram_gb is None and hw.vram_gb is None
    # detection failed entirely: the hosted path, which needs nothing local, is the recommendation
    assert p.recommend_profile(hw)[0] == "hosted_only"


# --------------------------------------------------------------------------------- render
def test_render_modelfile_has_from_num_ctx_and_every_guardrail():
    text = p.render_modelfile("tier2", "gpu_24gb")
    lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
    assert lines == [
        "FROM qwen3:30b-a3b",
        "PARAMETER num_ctx 16384",
        "PARAMETER num_predict 4500",
        "PARAMETER temperature 0.1",
        "PARAMETER top_p 0.9",
        "PARAMETER repeat_penalty 1.1",
        "PARAMETER seed 7",
    ]
    assert "request that sets temperature or" in text  # precedence documented inline


def test_render_modelfile_rejects_tiers_without_a_derived_model():
    with pytest.raises(KeyError):
        p.render_modelfile("embed", "gpu_16gb")
    with pytest.raises(KeyError):
        p.render_modelfile("tier1", "no_such_profile")


@pytest.mark.parametrize("tier", p.TIERS)
def test_shipped_modelfiles_are_the_default_profile_rendered(tier):
    """The packaged Modelfile.* are generated; a hand edit that drifts from models.yml fails."""
    cat = p.load_catalog()
    assert p.shipped_modelfile(tier) == p.render_modelfile(tier, cat.default_profile)


def test_write_modelfiles_renders_every_tier(tmp_path):
    written = p.write_modelfiles("mac_16gb", tmp_path / "models")
    assert [w.name for w in written] == [f"Modelfile.{t}" for t in p.TIERS]
    assert "FROM qwen3:8b" in (tmp_path / "models" / "Modelfile.tier2").read_text()


# ------------------------------------------------------------------------- active profile
def test_active_profile_round_trips_under_config_dir():
    assert p.load_active_profile() is None
    hw = p.Hardware(os="linux", gpu_vendor="nvidia", vram_gb=24.0, ram_gb=64.0)
    path = p.save_active_profile("gpu_24gb", "NVIDIA GPU with 24 GB VRAM", hw)
    assert path.name == "hardware_profile.json" and path.parent.name == "config"
    rec = p.load_active_profile()
    assert rec is not None and rec.profile == "gpu_24gb" and rec.hardware["vram_gb"] == 24.0
    assert rec.chosen_at


# ------------------------------------------------------------------------------- fit check
class _Rep:
    def __init__(self):
        self.rows = []

    def add(self, name, status, detail="", remediation="", blocking=False):
        self.rows.append((name, status, detail, remediation, blocking))


class _Client:
    def __init__(self, details: dict | None = None, fail: bool = False):
        self.details, self.fail = details, fail
        self.asked: list[str] = []

    def show(self, model):
        self.asked.append(model)
        if self.fail:
            raise ConnectionError("ollama down")
        return {"details": self.details or {}}


def test_estimate_size_from_show_details():
    assert p.estimate_size_gb({"parameter_size": "14.8B", "quantization_level": "Q4_K_M"}) == 8.9
    assert p.estimate_size_gb({"parameter_size": "137M", "quantization_level": "F16"}) == 0.3
    assert p.estimate_size_gb({"parameter_size": "", "quantization_level": "Q4"}) is None


def test_fit_check_passes_when_the_model_fits_vram(monkeypatch):
    monkeypatch.setattr(
        p, "detect_hardware", lambda: p.Hardware(os="windows", gpu_vendor="nvidia", vram_gb=16.0)
    )
    rep = _Rep()
    client = _Client({"parameter_size": "8.2B", "quantization_level": "Q4_K_M"})
    p.check_model_fit(rep, get_settings(), client)
    (row,) = rep.rows
    assert row[0] == "Model fit" and row[1] == "PASS"
    assert set(client.asked) == {f"kestrel-{t}" for t in p.TIERS}


def test_fit_check_warns_never_fails_and_names_the_profile_to_switch_to(monkeypatch):
    monkeypatch.setattr(
        p, "detect_hardware", lambda: p.Hardware(os="linux", gpu_vendor="nvidia", vram_gb=12.0)
    )
    p.save_active_profile("gpu_24gb", "forced", None)
    rep = _Rep()
    client = _Client({"parameter_size": "30.5B", "quantization_level": "Q4_K_M"})
    p.check_model_fit(rep, get_settings(), client)
    (row,) = rep.rows
    assert row[1] == "WARN" and row[4] is False
    assert "gpu_24gb" in row[2] and "12 GB VRAM" in row[2]
    assert "kestrel setup --profile gpu_12gb" in row[3]


def test_fit_check_falls_back_to_the_profile_table_when_ollama_is_down(monkeypatch):
    monkeypatch.setattr(
        p,
        "detect_hardware",
        lambda: p.Hardware(os="macos", gpu_vendor="apple", ram_gb=16.0, apple_silicon=True),
    )
    rep = _Rep()
    p.check_model_fit(rep, get_settings(), _Client(fail=True))  # default profile gpu_16gb
    (row,) = rep.rows
    assert row[1] == "WARN"
    assert "9.3 GB" in row[2]  # qwen3:14b from model_sizes_gb, not from /api/show
    assert "mac_16gb" in row[3]


def test_fit_check_is_info_when_nothing_was_detected(monkeypatch):
    monkeypatch.setattr(p, "detect_hardware", lambda: p.Hardware(os="unknown", notes=("x",)))
    rep = _Rep()
    p.check_model_fit(rep, get_settings(), _Client(fail=True))
    (row,) = rep.rows
    assert row[1] == "INFO"


def test_fit_check_is_info_when_every_tier_is_hosted(monkeypatch):
    """hosted_only recorded and every derived tier external: nothing to fit, Ollama never asked."""
    monkeypatch.setattr(
        p, "detect_hardware", lambda: p.Hardware(os="windows", gpu_vendor=None, ram_gb=8.0)
    )
    p.save_active_profile(p.HOSTED_PROFILE, "forced", None)
    s = get_settings()
    for tier in p.TIERS:
        s.llm.tiers[tier].backend = "frontier"
    rep = _Rep()
    client = _Client(fail=True)
    p.check_model_fit(rep, s, client)
    (row,) = rep.rows
    assert row[1] == "INFO" and "every tier is hosted" in row[2] and "hosted_only" in row[2]
    assert client.asked == []
