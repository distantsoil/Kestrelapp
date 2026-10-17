"""Low-spec machines and BYOK (D-SETUP-6, D-SETUP-7, D-SETUP-8).

  1. recommend_profile: a local profile only on an NVIDIA GPU with >= 11 GB of VRAM or Apple
     silicon with >= 14 GB; everything else hosted_only, with a plain, specific reason.
  2. ollama_needed: True iff a tier in settings.llm.tiers names the `local` backend; the embed
     tier is not special-cased.
  3. doctor: with no tier on Ollama, every Ollama row is INFO "not needed", nothing is probed and
     nothing blocks; with a tier on Ollama the FAIL is unchanged.
  4. The Setup page warning: shown under Local or Both where Hosted is recommended, never under
     Hosted or on a capable machine.
  5. Today's derived-models ask and the status rail stay quiet about Ollama when it is not needed.
Every network call is faked or poisoned; nothing live."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from kestrel import paths
from kestrel.config import BackendConfig, Settings, get_settings
from kestrel.llm import profiles
from kestrel.services import setup_state as ss

HW_4080 = profiles.Hardware(os="windows", gpu_vendor="nvidia", vram_gb=16.0, ram_gb=128.0)
HW_LAPTOP = profiles.Hardware(os="windows", gpu_vendor=None, vram_gb=None, ram_gb=8.0)
HW_BIG_CPU = profiles.Hardware(os="linux", gpu_vendor=None, vram_gb=None, ram_gb=128.0)
HW_SMALL_MAC = profiles.Hardware(os="macos", gpu_vendor="apple", ram_gb=8.0, apple_silicon=True)


# --------------------------------------------------------------------------- 1. recommendation
@pytest.mark.parametrize(
    ("hw", "expected"),
    [
        # Apple silicon: the 14 GB floor
        (HW_SMALL_MAC, "hosted_only"),
        (profiles.Hardware(os="macos", gpu_vendor="apple", ram_gb=13.9, apple_silicon=True),
         "hosted_only"),
        (profiles.Hardware(os="macos", gpu_vendor="apple", ram_gb=14.0, apple_silicon=True),
         "mac_16gb"),
        (profiles.Hardware(os="macos", gpu_vendor="apple", ram_gb=None, apple_silicon=True),
         "hosted_only"),
        # NVIDIA: the 11 GB floor, whatever the RAM
        (profiles.Hardware(os="windows", gpu_vendor="nvidia", vram_gb=10.9, ram_gb=128.0),
         "hosted_only"),
        (profiles.Hardware(os="windows", gpu_vendor="nvidia", vram_gb=6.0, ram_gb=None),
         "hosted_only"),
        (profiles.Hardware(os="windows", gpu_vendor="nvidia", vram_gb=11.0, ram_gb=16.0),
         "gpu_12gb"),
        (HW_4080, "gpu_16gb"),
        # no supported GPU (integrated, CPU only, AMD or Intel, Intel Mac): hosted at any RAM
        (HW_LAPTOP, "hosted_only"),
        (HW_BIG_CPU, "hosted_only"),
        (profiles.Hardware(os="macos", gpu_vendor=None, ram_gb=64.0), "hosted_only"),
        (profiles.Hardware(os="unknown"), "hosted_only"),
    ],
)
def test_local_is_recommended_only_on_a_supported_gpu(hw, expected):
    name, reason = profiles.recommend_profile(hw)
    assert name == expected
    assert reason and "—" not in reason  # no em dashes in operator-facing copy
    if expected == profiles.HOSTED_PROFILE:
        assert reason.endswith("use a hosted provider")


def test_hosted_reasons_are_plain_and_specific():
    assert profiles.recommend_profile(HW_SMALL_MAC) == (
        "hosted_only",
        "Apple silicon with 8 GB unified memory: too little to run the models alongside the OS; "
        "use a hosted provider",
    )
    small_card = profiles.Hardware(os="windows", gpu_vendor="nvidia", vram_gb=8.0, ram_gb=64.0)
    assert profiles.recommend_profile(small_card)[1] == (
        "NVIDIA GPU with 8 GB VRAM: too little to hold the evaluation model; use a hosted provider"
    )
    assert profiles.recommend_profile(HW_BIG_CPU)[1] == (
        "no supported GPU detected (128 GB RAM): on the CPU an evaluation takes minutes per job; "
        "use a hosted provider"
    )


def test_local_profiles_stay_available_to_choose():
    cat = profiles.load_catalog()
    for name in ("cpu_8gb", "gpu_12gb", "gpu_16gb", "mac_16gb", "hosted_only"):
        assert name in cat.profiles
    assert not cat.profile(profiles.FALLBACK_PROFILE).hosted


# ---------------------------------------------------------------------------- 2. ollama_needed
def _settings() -> Settings:
    return get_settings().model_copy(deep=True)


def _all_off_ollama(s: Settings) -> Settings:
    """Every tier off Ollama: the scoring tiers on the hosted backend, embed on a non-Ollama local
    backend standing in for the in-process embedder another workstream adds."""
    s.llm.backends["inproc"] = BackendConfig(base_url="inproc://")
    for name, tier in s.llm.tiers.items():
        tier.backend = "inproc" if name == "embed" else "frontier"
    return s


def test_ollama_needed_keys_off_the_local_backend_only():
    s = _settings()
    assert profiles.ollama_needed(s)  # the shipped config runs every tier on Ollama
    s = _all_off_ollama(s)
    assert not profiles.ollama_needed(s)
    # Embed is not special-cased: put it back on Ollama and Ollama is needed again.
    s.llm.tiers["embed"].backend = "local"
    assert profiles.ollama_needed(s)
    # Nor is any other tier.
    s = _all_off_ollama(_settings())
    s.llm.tiers["docsel"].backend = "local"
    assert profiles.ollama_needed(s)


# ---------------------------------------------------------------------------------- 3. doctor
def _doctor_with(monkeypatch, s: Settings):
    from kestrel import doctor as d

    monkeypatch.setattr(d, "_settings_for_doctor", lambda rep: s)
    monkeypatch.setattr(d, "_check_browser", lambda rep: rep.add("Browser (PDF)", d.PASS, "x"))
    monkeypatch.setattr(profiles, "detect_hardware", lambda: HW_LAPTOP)
    monkeypatch.delenv("OLLAMA_NUM_PARALLEL", raising=False)
    monkeypatch.delenv("OLLAMA_KEEP_ALIVE", raising=False)
    return d


def test_doctor_reports_ollama_as_not_needed_and_never_probes_it(monkeypatch):
    s = _all_off_ollama(_settings())
    s.llm.hosted_mode.enabled = True
    s.llm.hosted_mode.acknowledged_at = "2026-10-14T10:00:00+00:00"
    s.llm.hosted_mode.acknowledged_terms = s.llm.hosted_mode.disclosure_terms()
    d = _doctor_with(monkeypatch, s)

    def probed(self, *a, **k):
        raise AssertionError("Ollama probed although no tier runs on it")

    monkeypatch.setattr(d.OllamaClient, "version", probed)
    monkeypatch.setattr(d.OllamaClient, "list_models", probed)
    monkeypatch.setattr(d.OllamaClient, "show", probed)
    rep = d.run_doctor(quick=True)
    rows = {r.name: r for r in rep.results}
    for name in d.OLLAMA_ROWS:
        assert rows[name].status == d.INFO, name
        assert rows[name].detail == "not needed: no tier runs on Ollama"
        assert not rows[name].blocking
    ollama_rows = [r for r in rep.results if "ollama" in r.name.lower()]
    assert all(r.status == d.INFO for r in ollama_rows)
    assert not rep.has_blocking_failure
    out = d.format_report(rep)
    assert "[FAIL]  Ollama reachable" not in out and "[WARN]  OLLAMA_" not in out


def test_doctor_unchanged_when_a_tier_runs_on_ollama(monkeypatch):
    d = _doctor_with(monkeypatch, _settings())

    def refused(self, *a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(d.OllamaClient, "version", refused)
    rep = d.run_doctor(quick=True)
    rows = {r.name: r for r in rep.results}
    assert rows["Ollama reachable"].status == d.FAIL and rows["Ollama reachable"].blocking
    assert rows["OLLAMA_NUM_PARALLEL"].status == d.WARN
    assert rows["OLLAMA_KEEP_ALIVE"].status == d.WARN
    assert rep.has_blocking_failure


# ------------------------------------------------------------------------------ 4. Setup page
class _NoOllama:
    def __init__(self, *a, **k) -> None:
        pass

    def version(self, timeout: float = 5.0) -> str:
        raise httpx.ConnectError("refused")

    def list_models(self, timeout: float = 10.0) -> list[str]:
        return []


@pytest.fixture
def setup_env(monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("network call from a test")

    monkeypatch.setattr(httpx, "get", no_network)
    monkeypatch.setattr(ss, "OllamaClient", _NoOllama)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    ss.forget_hardware()
    ss.forget_ollama_probe()

    def use(hw: profiles.Hardware) -> None:
        monkeypatch.setattr(profiles, "detect_hardware", lambda: hw)
        ss.forget_hardware()

    yield use
    ss.forget_hardware()
    ss.forget_ollama_probe()


def test_assess_warns_when_local_is_chosen_where_hosted_is_recommended(setup_env):
    setup_env(HW_SMALL_MAC)
    for choice in ("local", "both"):
        st = ss.assess(get_settings(), {ss.CHOICE_KEY: choice})
        assert st.recommended == "hosted_only"
        assert st.low_spec_warning == ss.LOW_SPEC_WARNING
    assert ss.assess(get_settings(), {ss.CHOICE_KEY: "hosted"}).low_spec_warning is None
    setup_env(HW_4080)
    for choice in ss.CHOICES:
        assert ss.assess(get_settings(), {ss.CHOICE_KEY: choice}).low_spec_warning is None


def test_warning_copy_leads_with_the_decision():
    w = ss.LOW_SPEC_WARNING
    assert w.startswith("This machine can't run Kestrel's models at a usable speed.")
    assert "Use a hosted provider (bring your own key)." in w
    assert "Local mode stays available" in w
    assert "—" not in w and "—" not in ss.LOCAL_FALLBACK_REASON


def _fresh_app():
    from starlette.testclient import TestClient

    from kestrel.db import migrate
    from kestrel.db.engine import reset_engine
    from kestrel.web.app import build_app

    migrate.run_migrations(paths.db_path())
    asyncio.run(reset_engine())
    return TestClient(build_app(worker=False))


_HX = {"HX-Request": "true"}  # the System panel answers htmx with itself (D-UI-31)


def test_setup_page_shows_the_warning_box_only_when_it_applies(setup_env):
    setup_env(HW_LAPTOP)
    client = _fresh_app()
    body = client.post("/setup/mode", data={"mode": "local"}, headers=_HX).text
    assert 'data-testid="low-spec-warning"' in body and 'class="warnbox msg"' in body
    assert "This machine can&#39;t run Kestrel&#39;s models at a usable speed." in body
    assert "no supported GPU detected (8 GB RAM)" in body
    assert "—" not in body
    body = client.post("/setup/mode", data={"mode": "hosted"}, headers=_HX).text
    assert 'data-testid="low-spec-warning"' not in body
    setup_env(HW_4080)
    body = client.post("/setup/mode", data={"mode": "local"}, headers=_HX).text
    assert 'data-testid="low-spec-warning"' not in body


# -------------------------------------------------------------------- 5. Today and the rail
def test_today_skips_the_derived_models_ask_when_ollama_is_not_needed(monkeypatch):
    from kestrel.analytics import today

    def boom(*a, **k):
        raise AssertionError("setup probe ran although no tier runs on Ollama")

    monkeypatch.setattr(ss, "assess", boom)
    assert asyncio.run(today._derived_models_missing(_all_off_ollama(_settings()))) == []


def test_today_still_asks_when_a_tier_runs_on_ollama(monkeypatch):
    from types import SimpleNamespace

    from kestrel.analytics import today

    state = SimpleNamespace(ollama_version="0.9.0", missing_derived=["kestrel-tier2"])
    monkeypatch.setattr(ss, "assess", lambda settings: state)
    assert asyncio.run(today._derived_models_missing(_settings())) == ["kestrel-tier2"]


def test_status_rail_says_ollama_is_not_needed_instead_of_unreachable(monkeypatch):
    from kestrel.web import routes_control

    probed: list[str] = []

    async def _probe(url: str):
        probed.append(url)
        return {"up": False, "loaded": []}

    monkeypatch.setattr(routes_control, "_probe_ollama", _probe)
    row = asyncio.run(routes_control._model_row(_all_off_ollama(_settings()), "local"))
    assert row["label"] == "Ollama not needed" and row["state"] != "bad" and not probed
    row = asyncio.run(routes_control._model_row(_settings(), "local"))
    assert row["label"] == "AI engine not running" and probed


# ------------------------------------------------------------------- review follow-ups
def test_todays_byok_state_still_needs_ollama_for_embeddings():
    """Hosted mode on with the embed tier still on local Ollama: Ollama is still needed, so the
    doctor keeps its checks (the hosted branch), never "not needed"."""
    from kestrel.llm.profiles import ollama_needed

    s = _settings()
    for name, tier in s.llm.tiers.items():
        if name != "embed":
            tier.backend = "frontier"
    assert s.llm.tiers["embed"].backend == "local" and ollama_needed(s)


def test_a_non_external_server_not_named_local_still_counts():
    from kestrel.config import BackendConfig
    from kestrel.llm.profiles import ollama_needed

    s = _all_off_ollama(_settings())
    assert not ollama_needed(s)
    s.llm.backends["vllm"] = BackendConfig(base_url="http://127.0.0.1:8000/v1", external=False)
    s.llm.tiers["tier1"].backend = "vllm"
    assert ollama_needed(s)  # its num_ctx and needle checks must not be skipped


def test_unknown_hardware_gets_its_own_warning_and_a_recorded_local_profile_none():
    from kestrel.llm import profiles
    from kestrel.services import setup_state as ss

    cat = profiles.load_catalog()
    hosted = profiles.HOSTED_PROFILE
    unknown = profiles.Hardware(os="windows")
    small = profiles.Hardware(os="macos", apple_silicon=True, gpu_vendor="apple", ram_gb=8.0)
    assert ss.low_spec_warning(cat, hosted, "local", hw=unknown) == ss.UNKNOWN_HW_WARNING
    cpu = profiles.Hardware(os="windows", ram_gb=32.0)
    assert ss.low_spec_warning(cat, hosted, "local", hw=cpu).endswith(ss.NO_GPU_NOTE)
    assert ss.low_spec_warning(cat, hosted, "local", hw=small) == ss.LOW_SPEC_WARNING
    assert ss.low_spec_warning(cat, hosted, "local", hw=small, recorded_local=True) is None
    assert ss.low_spec_warning(cat, hosted, "hosted", hw=small) is None
    assert "can't run" not in ss.UNKNOWN_HW_WARNING and "—" not in ss.UNKNOWN_HW_WARNING
