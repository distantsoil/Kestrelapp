"""Hosted-only setup (D-SETUP-4): a machine with no GPU and little RAM finishes `kestrel setup`
without Ollama or a model download and runs every tier on a hosted provider under the operator's
own key.

  1. recommend_profile picks hosted_only for a weak machine, with the stated reason.
  2. `kestrel setup --hosted` against a respx-mocked OpenRouter (one free model, one paid): the key
     is written to .env, verified, the table printed, the disclosure shown and `yes` required, and
     the same setting rows the Settings page writes land in the setting table; `embed` stays local.
  3. Refusal without `yes`, the `--free` restriction, the `--model` flags, and a dry run that runs
     nothing.
  4. The disclosure text in setup.py matches the Settings page template word for word.
  5. doctor in hosted-only mode: the Ollama checks are INFO "skipped", embeddings a WARN, nothing
     blocks; with only some tiers hosted the local checks never expect a hosted model in Ollama.
Every network call is respx-mocked or raises; nothing live."""

from __future__ import annotations

import asyncio
import html
import re
import subprocess
from pathlib import Path

import httpx
import pytest
import respx
import typer
from httpx import Response
from typer.testing import CliRunner

from kestrel import paths
from kestrel import setup as setup_mod
from kestrel.config import get_settings
from kestrel.llm import profiles
from kestrel.llm import providers as prov
from kestrel.setup import HOSTED_ROUTES

runner = CliRunner()
root = typer.Typer()
root.add_typer(setup_mod.setup_app, name="setup")

HW_WEAK = profiles.Hardware(os="windows", gpu_vendor=None, vram_gb=None, ram_gb=8.0)
OR = "https://openrouter.ai/api/v1"
MODELS = {
    "data": [
        {
            "id": "a/paid",
            "name": "Paid A",
            "context_length": 128000,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            "supported_parameters": ["structured_outputs"],
        },
        {
            "id": "b/gratis:free",
            "name": "Gratis B",
            "context_length": 32000,
            "pricing": {"prompt": "0", "completion": "0"},
            "supported_parameters": ["structured_outputs", "response_format"],
        },
        {
            "id": "c/prose-only",
            "name": "No schema",
            "context_length": 8000,
            "pricing": {"prompt": "0.000001", "completion": "0.000001"},
            "supported_parameters": ["temperature"],
        },
    ]
}


@pytest.fixture
def quiet(monkeypatch):
    """No subprocess, no Ollama, a weak machine, no provider key in the environment, and a stub
    doctor (its own hosted-only behavior is tested below against the written rows)."""
    monkeypatch.setattr(
        setup_mod.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(setup_mod.subprocess, "Popen", lambda cmd, **kw: None)

    def refuse(url, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(setup_mod.httpx, "get", refuse)
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(profiles, "detect_hardware", lambda: HW_WEAK)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENMODEL_API_KEY", raising=False)
    # The .env loader owns a key once write_secret stored it and pops it again when the (per-test)
    # file is gone, so a key this test sets in the environment must not be "owned" by a previous
    # test's file.
    from kestrel.config import _dotenv_state

    owned: set = _dotenv_state["owned"]  # type: ignore[assignment]
    owned.discard("OPENROUTER_API_KEY")
    owned.discard("OPENMODEL_API_KEY")
    from kestrel import doctor as doctor_mod

    class _Report:
        has_blocking_failure = False
        results: list = []

    monkeypatch.setattr(doctor_mod, "run_doctor", lambda quick=False: _Report())
    monkeypatch.setattr(doctor_mod, "format_report", lambda rep: "(doctor report)")
    yield
    owned.discard("OPENROUTER_API_KEY")
    owned.discard("OPENMODEL_API_KEY")


def _mock_openrouter(models: dict = MODELS, key_ok: bool = True):
    respx.get(f"{OR}/key").mock(
        return_value=Response(
            200 if key_ok else 401,
            json={"data": {"label": "setup-key", "is_free_tier": True, "limit_remaining": 4.5}}
            if key_ok
            else {"error": {"message": "bad key"}},
        )
    )
    respx.get(f"{OR}/models").mock(return_value=Response(200, json=models))


def _db_settings():
    async def _go():
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from kestrel.db.engine import make_async_engine
        from kestrel.services.setting_overrides import load_settings_with_db

        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine)() as s:
                return await load_settings_with_db(s)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


# ---------------------------------------------------------------------------- 1. recommendation
def test_weak_machine_is_recommended_the_hosted_profile(quiet):
    name, reason = profiles.recommend_profile(HW_WEAK)
    assert name == "hosted_only"
    assert reason == (
        "no supported GPU detected (8 GB RAM): on the CPU an evaluation takes minutes per job; "
        "use a hosted provider"
    )
    out = runner.invoke(root, ["setup", "--dry-run"]).output
    assert "Hardware: windows, no supported GPU detected, 8 GB RAM" in out
    assert "Recommended profile: hosted_only (no supported GPU detected (8 GB RAM)" in out
    # the recommendation alone routes the dry run down the hosted path: no Ollama, no pull
    assert "Provider: OpenRouter" in out and "ollama pull" not in out


# ---------------------------------------------------------------------------- 2. the hosted path
@respx.mock
def test_hosted_setup_writes_key_rows_and_acknowledgement(quiet):
    _mock_openrouter()
    # provider (default), key (hidden), tier1 / tier2 / docsel model prompts (defaults), `yes`
    result = runner.invoke(root, ["setup", "--hosted"], input="\nsk-or-v1-abc123\n\n\n\nyes\n")
    out = result.output
    assert result.exit_code == 0, out
    assert "Profile: hosted_only (requested; recommended was hosted_only)" in out
    assert "Ollama" not in out  # no Ollama step, no install command, no pull, no build
    assert "Key written to" in out and "OPENROUTER_API_KEY" in out
    assert "sk-or-v1-abc123" not in out  # never echoed
    env_text = paths.dotenv_path().read_text()
    assert "OPENROUTER_API_KEY=sk-or-v1-abc123" in env_text
    assert "OpenRouter: key accepted; label setup-key; $4.50 of credit left; free tier" in out
    # the table: both schema-capable models with context, prices and the free marker; the
    # prose-only one is never offered (hard rule 3)
    assert "2 schema-capable models on OpenRouter (1 free)" in out
    assert re.search(r"b/gratis:free\s+32,000\s+0\.00\s+0\.00\s+free", out)
    assert re.search(r"a/paid\s+128,000\s+3\.00\s+15\.00", out)
    assert "c/prose-only" not in out
    assert "daily request cap" in out
    # defaults: cheapest for tier1 and docsel, the stronger one for tier2 (and its followers)
    assert "tier1: frontier/b/gratis:free" in out
    assert "docsel: frontier/b/gratis:free" in out
    assert "trigger: frontier/b/gratis:free" in out
    for tier in ("tier2", "tier3", "frontier"):
        assert f"{tier}: frontier/a/paid" in out
    # the disclosure, the addendum, and the acknowledgement
    assert "Read before turning this on." in out and "first 1200 tokens" in out
    assert "Hosted-only setup also routes two tiers" in out
    assert "Tier 3 work sends the opportunity" in out and "Trigger classification sends" in out
    assert "Hosted mode on (acknowledged" in out
    assert "Embeddings: on this machine with the bundled in-process model" in out
    assert "(doctor report)" in out and "Setup complete" in out
    assert "Traceback" not in out and "—" not in out
    # the rows: exactly the keys Settings > Hosted writes, plus tier3 and trigger. They are
    # hosted ASSIGNMENTS (llm.hosted_tiers) plus the switch (llm.mode=hosted); the effective
    # tiers carry them because the mode is hosted, and the local set is untouched.
    assert "Run mode: hosted" in out
    st = _db_settings()
    hm = st.llm.hosted_mode
    assert hm.enabled is True and hm.acknowledged_at and hm.active
    assert st.llm.mode == "hosted"
    for tier in ("tier1", "docsel", "trigger"):
        assert st.llm.hosted_tiers[tier].backend == "frontier"
        assert st.llm.tiers[tier].backend == "frontier"
        assert st.llm.tiers[tier].model == "b/gratis:free"
    for tier in ("tier2", "tier3", "frontier"):
        assert st.llm.tiers[tier].backend == "frontier"
        assert st.llm.tiers[tier].model == "a/paid"
    assert "frontier" not in st.llm.hosted_tiers  # hosted in either mode, so it stays in tiers
    # embed never goes hosted: the hosted_only profile runs it in-process (D-EMBED-3), in the
    # local tier set, so no tier needs Ollama
    assert st.llm.tiers["embed"].backend == "inprocess"
    assert "embed" not in st.llm.hosted_tiers
    rows = _rows()
    assert "llm.backends" not in str(rows)  # no protocol row for OpenRouter
    assert rows["llm.mode"] == "hosted"
    assert not [
        k for k in rows
        if k.startswith("llm.tiers.")
        and "frontier" not in k
        and not k.startswith("llm.tiers.embed")
    ]
    assert rows["llm.tiers.embed.backend"] == "inprocess"
    rec = profiles.load_active_profile()
    assert rec is not None and rec.profile == "hosted_only"
    # every row of the per-tier Settings table reads as hosted and in use
    rows = {r.tier: r for r in prov.tier_status(st)}
    assert all(r.external and r.in_use for r in rows.values())


def _rows() -> dict:
    async def _go():
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from kestrel.db.engine import make_async_engine
        from kestrel.db.models import Setting

        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine)() as s:
                return dict((await s.execute(select(Setting.key, Setting.value))).all())
        finally:
            await engine.dispose()

    return asyncio.run(_go())


@respx.mock
def test_yes_never_accepts_the_disclosure_and_turns_nothing_on(quiet, monkeypatch):
    """`--yes` runs the install, pull and build steps unasked; it is not the operator reading
    the disclosure. With an existing key and the auto-recommended hosted_only profile, a bare
    `kestrel setup --yes` must stop AT the disclosure with a clear message and a non-zero exit,
    not quietly turn on automatic hosted sending."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-already")
    _mock_openrouter()
    for args in (["setup", "--yes"], ["setup", "--hosted", "--yes", "--model", "b/gratis:free"]):
        result = runner.invoke(root, args)
        out = result.output
        assert result.exit_code == 1, out
        assert "Read before turning this on." in out  # the disclosure is still printed
        assert "--yes does not accept the Hosted mode disclosure" in out
        assert "--accept-hosted-disclosure" in out
        assert "nothing was turned on and no row written" in out
        assert "Disclosure acknowledged" not in out and "Hosted mode on" not in out
        assert "Setup did not finish: Hosted" in out
        assert not paths.db_path().exists() or not _rows()
        assert not profiles.active_profile_path().exists()


@respx.mock
def test_accept_flag_keeps_an_existing_key_and_honors_model_flags(quiet, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-already")
    _mock_openrouter()
    result = runner.invoke(
        root,
        [
            "setup", "--hosted", "--yes", "--accept-hosted-disclosure",
            "--model", "b/gratis:free", "--model-tier2", "a/paid",
        ],
    )
    out = result.output
    assert result.exit_code == 0, out
    assert "OPENROUTER_API_KEY is already set" in out and "Key written" not in out
    assert "Read before turning this on." in out
    assert "Disclosure accepted with --accept-hosted-disclosure" in out
    st = _db_settings()
    assert st.llm.tiers["tier1"].model == "b/gratis:free"
    assert st.llm.tiers["docsel"].model == "b/gratis:free"
    assert st.llm.tiers["tier2"].model == "a/paid"
    assert st.llm.tiers["frontier"].model == "a/paid"
    assert st.llm.hosted_mode.active
    # the flag alone, without --yes, also stands in for the typed yes (the provider and
    # keep-the-key prompts take their defaults; --model skips the per-tier prompts)
    result = runner.invoke(
        root, ["setup", "--hosted", "--accept-hosted-disclosure", "--model", "a/paid"],
        input="\n\n",
    )
    assert result.exit_code == 0, result.output
    assert "Disclosure accepted with --accept-hosted-disclosure" in result.output


@respx.mock
def test_hosted_setup_refuses_without_yes_and_writes_nothing(quiet):
    _mock_openrouter()
    result = runner.invoke(root, ["setup", "--hosted"], input="\nsk-or-v1-abc\n\n\n\nno\n")
    out = result.output
    assert result.exit_code == 1, out
    assert "disclosure not accepted; nothing was turned on and no row written" in out
    assert "Setup did not finish: Hosted" in out
    assert "Doctor: skipped, hosted setup did not finish" in out
    assert not paths.db_path().exists() or not _rows()
    assert not profiles.active_profile_path().exists()


@respx.mock
def test_free_restricts_the_pool_and_says_what_free_costs(quiet):
    _mock_openrouter()
    result = runner.invoke(
        root, ["setup", "--hosted", "--free", "--provider", "openrouter"],
        input="sk-or-v1\n\n\n\nyes\n",
    )
    out = result.output
    assert result.exit_code == 0, out
    assert "1 free, schema-capable models on OpenRouter (1 free)" in out
    assert "a/paid" not in out.split("Read before")[0]
    assert "may train on your inputs unless OpenRouter data collection is set to deny" in out
    assert "there may be no free model left" in out
    st = _db_settings()
    assert {st.llm.tiers[t].model for t in ("tier1", "tier2", "tier3", "docsel", "frontier")} == {
        "b/gratis:free"
    }


@respx.mock
def test_free_with_a_paid_model_flag_is_refused_by_name(quiet, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-already")
    _mock_openrouter()
    result = runner.invoke(root, ["setup", "--hosted", "--yes", "--free", "--model", "a/paid"])
    assert result.exit_code == 1
    assert "'a/paid' is not in the provider's free, schema-capable list" in result.output
    assert not paths.db_path().exists() or not _rows()  # refused before any row is written


@respx.mock
def test_no_free_model_is_a_plain_failure(quiet, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-already")
    _mock_openrouter(models={"data": MODELS["data"][:1]})
    result = runner.invoke(root, ["setup", "--hosted", "--yes", "--free"])
    assert result.exit_code == 1
    assert "no free model can honor a JSON schema here" in result.output


@respx.mock
def test_rejected_key_fails_in_one_line(quiet):
    _mock_openrouter(key_ok=False)
    result = runner.invoke(root, ["setup", "--hosted"], input="\nsk-or-bad\n")
    out = result.output
    assert result.exit_code == 1, out
    assert "OpenRouter rejected the key" in out and "Traceback" not in out
    assert "Setup did not finish: Hosted" in out


def test_end_of_input_at_a_prompt_is_one_line_not_a_trace(quiet):
    result = runner.invoke(root, ["setup", "--hosted"], input="")
    out = result.output
    assert result.exit_code == 1, out
    assert "Hosted: aborted at a prompt" in out and "Traceback" not in out


def test_yes_without_a_key_says_where_to_put_it(quiet):
    result = runner.invoke(root, ["setup", "--hosted", "--yes"])
    assert result.exit_code == 1
    assert "OPENROUTER_API_KEY is not set; add it to" in result.output


def test_unknown_provider_and_profile_conflict_are_refused(quiet):
    out = runner.invoke(root, ["setup", "--hosted", "--provider", "acme", "--yes"]).output
    assert "unknown provider 'acme'; known: openrouter, openmodel, anthropic, openai" in out
    out = runner.invoke(root, ["setup", "--hosted", "--profile", "gpu_16gb", "--dry-run"]).output
    assert "--hosted and --profile gpu_16gb conflict" in out


# ---------------------------------------------------------------------------- 3. dry run
def test_dry_run_hosted_prints_every_step_and_runs_nothing(quiet, monkeypatch):
    calls: list[str] = []

    async def boom(*a, **k):
        calls.append("network")
        raise AssertionError("a dry run never calls the provider")

    monkeypatch.setattr(prov, "verify_key", boom)
    monkeypatch.setattr(prov, "list_models", boom)
    result = runner.invoke(root, ["setup", "--dry-run", "--hosted", "--provider", "openmodel"])
    out = result.output
    assert result.exit_code == 0, out
    assert "dry run" in out
    assert "Profile: hosted_only (requested; recommended was hosted_only)" in out
    assert "Provider: OpenModel (openmodel.ai) (openmodel -> https://api.openmodel.ai/v1)" in out
    assert "would run: prompt for the OpenModel (openmodel.ai) API key and write it to" in out
    assert "OPENMODEL_API_KEY" in out
    assert "would run: GET https://api.openmodel.ai/v1/models (verify the key" in out
    assert "would run: GET https://api.openmodel.ai/v1/models (list the schema-capable" in out
    assert "would run: choose a model per tier: tier1, tier2, docsel" in out
    assert "would run: print the Hosted mode disclosure and require `yes`" in out
    assert "would run: kestrel db upgrade" in out
    assert "llm.hosted_mode.acknowledged_at" in out and "llm.mode=hosted" in out
    assert "llm.hosted_tiers.<tier>.backend" in out
    assert "tier1, tier2, tier3, docsel, trigger, frontier" in out
    assert "would run: write" in out and "hardware_profile.json" in out
    assert "would run: kestrel doctor" in out
    assert "would run: seed default config" in out
    assert "Setup complete" in out
    assert "ollama" not in out.lower().replace("ollama.com", "")  # no Ollama step at all
    assert calls == []
    assert not paths.dotenv_path().exists()
    assert not paths.db_path().exists()
    assert not profiles.active_profile_path().exists()


# ---------------------------------------------------------------------------- 4. disclosure
def _template_disclosure() -> str:
    """The warnbox paragraph of the Settings partial, tags stripped and the Jinja placeholders
    turned into the format fields setup.py uses. The macro lives in hosted_disclosure.html,
    shared by Settings > Hosted and the onboarding model step (D-UI-30)."""
    tpl = Path(paths.web_templates_dir()) / "partials" / "hosted_disclosure.html"
    text = tpl.read_text(encoding="utf-8")
    m = re.search(r'<p class="warnbox"[^>]*>(.*?)</p>', text, re.DOTALL)
    assert m, "the disclosure warnbox moved; update setup.HOSTED_DISCLOSURE and this test"
    body = re.sub(r"\{\{\s*tier_sends\.(\w+)\s*\}\}", r"{\1}", m.group(1))
    body = html.unescape(re.sub(r"<[^>]+>", "", body))
    return "\n".join(line.strip() for line in body.strip().splitlines() if line.strip())


def test_disclosure_matches_the_settings_page_word_for_word():
    assert setup_mod.HOSTED_DISCLOSURE == _template_disclosure()
    from kestrel.web.pages import settings as settings_page

    assert setup_mod._TIER_SENDS == settings_page._TIER_SENDS
    # every routed tier has a sends line, so the disclosure and the table name all of them
    assert set(setup_mod._TIER_SENDS) == set(prov.HOSTED_TIERS) == {t for t, _ in HOSTED_ROUTES}
    s = get_settings()
    assert setup_mod.tier_sends(s) == settings_page._tier_sends(s)
    filled = setup_mod.hosted_disclosure(s)
    assert "{" not in filled and "first 1200 tokens" in filled and "up to 16000 tokens" in filled
    assert "—" not in filled + setup_mod.HOSTED_ONLY_ADDENDUM


# ---------------------------------------------------------------------------- 5. doctor
def _hosted_settings(*tiers: str, ack: bool = True):
    s = get_settings()
    for tier in tiers:
        s.llm.tiers[tier].backend = "frontier"
        s.llm.tiers[tier].model = "b/gratis:free"
    s.llm.hosted_mode.enabled = True
    s.llm.hosted_mode.acknowledged_at = "2026-10-12T10:00:00+00:00" if ack else None
    s.llm.hosted_mode.acknowledged_terms = s.llm.hosted_mode.disclosure_terms() if ack else None
    return s


ALL_LOCAL_TIERS = ("tier1", "tier2", "tier3", "docsel", "trigger")


class _Client:
    """A preflight client with no Ollama behind it, unless `tags` is given."""

    native_url = "http://127.0.0.1:11434"

    def __init__(self, tags: list[str] | None = None) -> None:
        self.tags = tags
        self.asked: list[str] = []

    def version(self):
        if self.tags is None:
            raise httpx.ConnectError("refused")
        return "0.9.0"

    def list_models(self):
        if self.tags is None:
            raise httpx.ConnectError("refused")
        return list(self.tags)

    def show(self, model):
        self.asked.append(model)
        raise httpx.ConnectError("refused")


def test_doctor_hosted_only_skips_the_ollama_checks_and_warns_on_embeddings():
    from kestrel import doctor as d

    s = _hosted_settings(*ALL_LOCAL_TIERS)
    assert d.hosted_only(s) and d.local_tiers(s) == []
    rep = d.DoctorReport()
    d._skip_local_checks(rep, _Client(), s)
    rows = {r.name: r for r in rep.results}
    for name in ("Ollama reachable", "Models present", "num_ctx", "needle", "thinking off",
                 "schema honored"):
        assert rows[name].status == d.INFO and rows[name].detail == "skipped: every tier is hosted"
        assert not rows[name].blocking
    emb = rows["Embeddings"]
    assert emb.status == d.WARN and not emb.blocking
    assert "embeddings unavailable without Ollama: few-shot and reranker off" in emb.detail
    assert "nomic-embed-text" in emb.remediation
    assert not rep.has_blocking_failure
    # Ollama present with the embedding model: a PASS instead
    rep = d.DoctorReport()
    d._skip_local_checks(rep, _Client(tags=["nomic-embed-text:latest"]), s)
    assert next(r for r in rep.results if r.name == "Embeddings").status == d.PASS
    # not acknowledged, or one tier still local: not hosted-only
    assert not d.hosted_only(_hosted_settings(*ALL_LOCAL_TIERS, ack=False))
    assert not d.hosted_only(_hosted_settings("tier1", "tier2", "docsel"))
    assert d.local_tiers(_hosted_settings("tier1", "tier2", "docsel")) == ["tier3", "trigger"]


def test_doctor_partial_hosting_never_expects_a_hosted_model_in_ollama():
    from kestrel import doctor as d

    s = _hosted_settings("tier1", "docsel")  # tier2, tier3, trigger stay local
    client = _Client(tags=["kestrel-tier2", "kestrel-tier3", "kestrel-tier1", "nomic-embed-text"])
    rep = d.DoctorReport()
    present = d._check_models_present(rep, client, s)
    row = rep.results[0]
    assert row.status == d.PASS and "b/gratis:free" not in row.detail
    assert present == {"kestrel-tier2", "kestrel-tier3", "kestrel-tier1", "nomic-embed-text"}
    rep = d.DoctorReport()
    d._check_thinking(rep, client, s, present)  # chat() is absent on _Client: WARN rows, never
    d._check_schema(rep, client, s, present)  # a hosted model name, and nothing blocking
    names = " ".join(r.name for r in rep.results)
    assert "b/gratis:free" not in names and "kestrel-tier2" in names
    assert not rep.has_blocking_failure


def test_run_doctor_in_hosted_only_mode_has_no_blocking_failure(monkeypatch):
    """End to end on the rows setup writes: the REAL run_doctor reads the setting table (not only
    the YAML), sees every tier hosted, and reports INFO rows instead of a FAIL on Ollama."""
    from kestrel import doctor as d
    from kestrel.config import _dotenv_state
    from kestrel.db import migrate

    _dotenv_state["owned"].discard("OPENROUTER_API_KEY")  # type: ignore[union-attr]
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-already")
    backend = get_settings().llm.backends["frontier"]
    picks = {
        "tier1": prov.ModelInfo(id="b/gratis:free"),
        "tier2": prov.ModelInfo(id="a/paid"),
        "docsel": prov.ModelInfo(id="b/gratis:free"),
    }
    migrate.run_migrations()
    asyncio.run(
        setup_mod._write_rows(setup_mod.hosted_rows("frontier", backend, picks, "2026-10-12T10:00"))
    )
    monkeypatch.setattr(d, "_check_browser", lambda rep: rep.add("Browser (PDF)", d.PASS, "x"))

    def refused(self, *a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(d.OllamaClient, "version", refused)
    monkeypatch.setattr(d.OllamaClient, "list_models", refused)
    rep = d.run_doctor(quick=True)
    rows = {r.name: r for r in rep.results}
    assert rows["Ollama reachable"].status == d.INFO
    assert rows["Models present"].status == d.INFO
    assert rows["OLLAMA_NUM_PARALLEL"].status == d.INFO
    assert rows["Embeddings"].status == d.WARN
    assert rows["Hosted mode"].status == d.PASS and "tier1 -> frontier/b/gratis:free" in (
        rows["Hosted mode"].detail
    )
    assert rows["Hosted key"].status == d.PASS
    assert rows["Model fit"].status == d.INFO
    assert not rep.has_blocking_failure
    assert not [r for r in rep.results if r.status == d.FAIL]


def test_openmodel_rows_carry_the_protocol_and_refuse_a_mix(quiet):
    """OpenModel has one protocol per backend: the rows carry the one every pick speaks
    (messages preferred), and picks that share none are refused by name."""
    s = get_settings()
    backend = s.llm.backends["openmodel"]
    m1 = prov.ModelInfo(id="x/one", protocols=["responses"])
    m2 = prov.ModelInfo(id="x/two", protocols=["responses", "messages"])
    m3 = prov.ModelInfo(id="x/three", protocols=["messages"])
    mixed = {"tier1": m1, "tier2": m2, "docsel": m1}
    rows = setup_mod.hosted_rows("openmodel", backend, mixed, "t")
    assert rows["llm.backends.openmodel.protocol"] == "responses"  # x/one speaks only that
    # the frontier is hosted in either mode and lives in llm.tiers; every other route is a hosted
    # assignment (llm.hosted_tiers), and the switch is set to hosted (D-HOSTED-2)
    assert rows["llm.tiers.frontier.backend"] == "openmodel"
    assert rows["llm.tiers.frontier.model"] == "x/two"
    assert rows["llm.hosted_tiers.tier3.model"] == "x/two"
    assert rows["llm.hosted_tiers.tier1.backend"] == "openmodel"
    assert rows["llm.mode"] == "hosted"
    assert not [k for k in rows if k.startswith("llm.tiers.") and "frontier" not in k]
    all_two = {"tier1": m2, "tier2": m2, "docsel": m2}
    both = setup_mod.hosted_rows("openmodel", backend, all_two, "t")
    assert both["llm.backends.openmodel.protocol"] == "messages"
    st = setup_mod.SetupState(
        yes=True, hosted_opts=setup_mod.HostedOptions(model_tier1="x/one", model_tier2="x/two")
    )
    assert setup_mod.choose_models(st, [m1, m2, m3], backend) is not None
    st = setup_mod.SetupState(
        yes=True, hosted_opts=setup_mod.HostedOptions(model_tier1="x/one", model_tier2="x/three")
    )
    assert setup_mod.choose_models(st, [m1, m2, m3], backend) is None
    assert st.failed == ["Hosted"]
    with pytest.raises(ValueError, match="share one"):
        setup_mod.shared_protocol({"tier1": m1, "tier2": m3})
    assert setup_mod.shared_protocol({"tier1": prov.ModelInfo(id="x/none")}) is None
