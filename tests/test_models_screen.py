"""Models (D-UI-105, D-UI-147): the page and every action that swaps a card in place. Cloud-safe:
TestClient on the real app, a throwaway data directory, no model and no network (the Ollama probe
and the hardware are patched, the provider is mocked with respx). The live-browser parity check is
tests/test_models_browser.py."""

from __future__ import annotations

import asyncio
import json
import re

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from kestrel import doctor
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Setting, Task
from kestrel.llm.profiles import Hardware
from kestrel.services import hosted_models as hm
from kestrel.services import setup_state as ss
from kestrel.web import models_view as mv
from tests.test_hosted_mode import _db_settings, _fresh_app
from tests.test_hosted_overrides import _GEMMA, _NEMOTRON, _PAID, _connect, _mock, _state

GPU = Hardware(os="windows", gpu_vendor="nvidia", vram_gb=12.0, ram_gb=32.0)
CPU = Hardware(os="linux", gpu_vendor=None, vram_gb=None, ram_gb=16.0)


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    """A GPU machine with Ollama up and one model present; the provider key is a fake."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.setattr(ss, "detected_hardware", lambda: GPU)
    monkeypatch.setattr(ss, "probe_ollama",
                        lambda settings, fresh=False: ("0.5.0", ["nomic-embed-text:latest"]))
    hm.clear_cache()
    yield
    hm.clear_cache()


def _all_base_tags() -> list[str]:
    """Every base model the recommended profile for the test's machine needs, as Ollama lists."""
    from kestrel.llm import profiles

    cat = profiles.load_catalog()
    return cat.profile(profiles.recommend_profile(GPU)[0]).models.base_tags()


def _settings_row(key: str):
    async def go():
        async with get_sessionmaker()() as s:
            row = await s.get(Setting, key)
            return None if row is None else row.value

    return asyncio.run(go())


# ------------------------------------------------------------------------------ the words
def test_friendly_names_never_show_a_raw_id():
    assert mv.friendly_model("kestrel-tier2", base="qwen3:14b") == "qwen3:14b"
    assert mv.friendly_model("kestrel-tier2") == "Kestrel's model for this step"
    assert mv.friendly_model("openai/gpt-4o-mini") == "gpt-4o-mini"
    assert mv.friendly_model("llama3:latest") == "llama3"
    assert mv.friendly_model("") == "No model"
    assert mv.model_billions("qwen3:14b") == "14 billion"
    assert mv.model_billions("nomic-embed-text") == ""


def test_fit_with_you_states_its_count_and_says_too_few_under_five():
    from kestrel.analytics.models_history import ModelRow

    def row(n_labeled, rate):
        return ModelRow(tier=2, model="m", prompt_version="v1", n=n_labeled, mean_score=None,
                        verdicts={}, mean_latency_ms=None, mean_prompt_tokens=None,
                        mean_completion_tokens=None, n_labeled=n_labeled, agreement_rate=rate,
                        mean_abs_score_error=None, insufficient=n_labeled < 5, hosted=False)

    few = mv.fit_with_you([row(4, 1.0)], 2, "m", better=False)
    assert few.label == "Too few ratings yet (4)" and few.tone == "muted"
    good = mv.fit_with_you([row(8, 0.75)], 2, "m", better=False)
    assert good.label == "Good match, 75% of 8" and good.tone == "good"
    assert mv.fit_with_you([row(8, 0.75)], 2, "m", better=True).tone == "info"
    assert mv.fit_with_you([], None, "m", better=False).label == "Not needed"


def test_hardware_fit_tags():
    assert mv.fit_tag(2.6, GPU)[0] == "Fits well"
    assert mv.fit_tag(11.5, GPU)[0] == "A tight fit"
    assert mv.fit_tag(18.6, GPU)[0] == "Too big for this computer"
    assert mv.fit_tag(9.3, CPU)[0] == "Fits, but slow"
    assert mv.fit_tag(None, GPU)[0] == "Size unknown"


def test_under_both_only_deeper_read_is_online_and_embedding_stays_local():
    assert mv.run_where("both", "tier3", False) is True
    assert mv.run_where("both", "tier2", False) is False
    assert mv.run_where("hosted", "tier1", False) is True
    assert mv.run_where("hosted", "embed", True) is False
    assert mv.run_where("hosted", "rules", True) is False
    assert mv.run_where("local", "tier2", False) is False


# --------------------------------------------------------------------------------- the page
def test_page_shows_the_five_steps_in_plain_words_with_no_raw_ids():
    client = _fresh_app()
    r = client.get("/models")
    assert r.status_code == 200
    body = r.text
    for name in ("Rules", "Quick screen", "Rating", "Resume picking", "Deeper read"):
        assert name in body
    for code in ("T0", "T1", "T2", "T3", "Docs"):
        assert f">{code}</span>" in body
    assert "No AI model" in body and "Asks you first" in body
    assert "kestrel-tier" not in body.split('data-testid="mo-steps"')[1].split("</section>")[0]
    assert "Too few ratings yet (0)" in body
    assert "Whole posting" in body and "Start of posting" in body
    assert 'data-testid="mo-uses"' in body and "On this computer" in body
    assert 'data-testid="mo-check-btn"' in body and 'href="/bench"' in body
    assert "—" not in body.split('class="mo-page"')[1]


def test_a_cpu_only_machine_turns_amber_and_offers_the_online_setup(monkeypatch):
    monkeypatch.setattr(ss, "detected_hardware", lambda: CPU)
    body = _fresh_app().get("/models").text
    assert 'data-testid="mo-cpu-note"' in body and "mo-edge-warn" in body
    assert "Set up an online model" in body


def test_models_are_not_loaded_into_the_page_until_asked():
    body = _fresh_app().get("/models").text
    assert 'data-testid="hosted-overrides"' not in body
    assert 'hx-get="/models/hosted/overrides"' in body


# ----------------------------------------------------------------------------- health check
def _save_report(*rows):
    rep = doctor.DoctorReport()
    for name, status, detail in rows:
        rep.add(name, status, detail)
    ss.save_report(rep)


def test_health_check_before_any_run_reads_not_checked_yet():
    body = _fresh_app().get("/models/health").text
    assert body.count("Not checked yet") >= 5
    for title in ("AI engine running", "Models downloaded", "Every step can read enough",
                  "Thinking turned off", "Answers in the right format"):
        assert title in body
    assert 'data-testid="mo-health-run"' in body and "Show details" in body


def test_a_failed_room_to_read_test_shows_too_small_and_offers_fix_it():
    client = _fresh_app()
    _save_report(
        ("Ollama reachable", "PASS", "version 0.5"), ("Models present", "PASS", "all found"),
        ("num_ctx tier2", "FAIL", "kestrel-tier2: num_ctx 8192 < required 16384"),
        ("thinking off kestrel-tier2", "PASS", "no think"),
        ("schema honored kestrel-tier2", "PASS", "ok"),
    )
    health = client.get("/models/health").text
    assert 'data-testid="mo-hc-room"' in health and "mo-hc-fix-room" in health
    assert 'class="is-fail"' in health
    page = client.get("/models").text
    assert "Too small: cuts long postings short" in page
    assert 'data-testid="mo-fix-tier2"' in page
    assert 'data-testid="mo-canread-tier1"' in page
    row_open = page.split('data-testid="mo-step-tier2"')[0].rsplit("<tr", 1)[1]
    assert 'class="is-bad"' in row_open  # the row itself turns red


def test_run_check_starts_the_preflight_in_the_background(monkeypatch):
    called = []
    monkeypatch.setattr(ss, "verify", lambda: called.append(1))
    r = _fresh_app().post("/models/health/run")
    assert r.status_code == 200 and called == [1]
    assert 'id="mo-health"' in r.text


def test_run_check_while_one_runs_says_so(monkeypatch):
    def busy():
        raise ss.ActionBusy("verify is still running")

    monkeypatch.setattr(ss, "verify", busy)
    r = _fresh_app().post("/models/health/run")
    assert r.status_code == 200 and "already running" in r.text


def test_fix_it_queues_the_rebuild_and_retests_afterward(monkeypatch):
    from kestrel.web.pages import models_ui

    client = _fresh_app()
    monkeypatch.setattr(ss, "probe_ollama", lambda settings, fresh=False: (
        "0.5.0", _all_base_tags()))
    r = client.post("/models/health/fix")
    assert r.status_code == 200
    assert models_ui._after["retest"] is True, re.sub("<[^>]+>", " ", r.text)[-900:]
    async def tasks():
        async with get_sessionmaker()() as s:
            return (await s.execute(select(Task.type))).scalars().all()
    assert any("create" in t or "build" in t for t in asyncio.run(tasks())), r.text
    models_ui._after["retest"] = False


# ---------------------------------------------------------------------------- downloads
def test_download_offers_what_is_missing_and_queues_pulls():
    client = _fresh_app()
    card = client.get("/models/downloads").text
    assert 'data-testid="mo-dl-start"' in card and "missing" in card
    assert "resumes if interrupted" in card.lower()
    assert "ollama pull" in card and "Show details" in card  # commands only behind details
    r = client.post("/models/download")
    assert r.status_code == 200

    async def pulls():
        async with get_sessionmaker()() as s:
            return (await s.execute(select(Task.payload).where(Task.type.like("%pull%")))
                    ).scalars().all()

    queued = asyncio.run(pulls())
    assert queued and all(p.get("tag") for p in queued)
    after = client.get("/models/downloads").text
    assert 'data-testid="mo-dl-pause"' in after and "every 2s" in after  # polls while live


def test_download_pause_stops_the_queue_and_resume_is_offered():
    client = _fresh_app()
    client.post("/models/download")
    r = client.post("/models/download/pause")
    assert r.status_code == 200
    assert "data-testid=\"mo-dl-start\"" in client.get("/models/downloads").text


def test_with_the_engine_down_the_card_says_so(monkeypatch):
    monkeypatch.setattr(ss, "probe_ollama", lambda settings, fresh=False: (None, []))
    card = _fresh_app().get("/models/downloads").text
    assert 'data-testid="mo-engine-down"' in card and "Open Ollama" in card


# --------------------------------------------------------------------------- online keys
def test_a_key_is_saved_without_ever_being_echoed():
    client = _fresh_app()
    secret = "sk-or-this-is-a-secret-0123456789"
    r = client.post("/models/online/key", data={"provider": "openrouter", "op": "save",
                                                "api_key": secret})
    assert r.status_code == 200
    assert secret not in r.text and "key saved" in r.text.lower()
    assert 'id="mo-online-card"' in r.text and 'hx-swap-oob="outerHTML"' in r.text
    assert "Stored only on this computer. Never in backups." in r.text


def test_an_unknown_provider_is_refused_in_plain_words():
    r = _fresh_app().post("/models/online/key", data={"provider": "nope", "op": "save",
                                                      "api_key": "x" * 20})
    assert r.status_code == 200 and "Choose a provider first." in r.text


def test_the_key_form_lists_every_provider_with_a_monogram_and_a_key_link():
    body = _fresh_app().get("/models/online?open=1").text
    for label in ("OpenRouter", "OpenModel", "Anthropic", "OpenAI"):
        assert label in body
    assert "Get a key" in body and "mo-badge-letter" in body
    assert 'type="password"' in body


# ------------------------------------------------------------------------------ change model
def test_the_drawer_lists_local_models_with_a_fit_tag():
    body = _fresh_app().get("/models/change/tier2").text
    assert 'data-testid="mo-local-list"' in body
    assert "qwen3:14b" in body and "Fits" in body and "Not downloaded" in body
    assert "Needs room for about" in body
    assert 'data-testid="mo-where-online"' in body


def _with_tags(monkeypatch, *extra: str) -> None:
    monkeypatch.setattr(ss, "probe_ollama", lambda settings, fresh=False: (
        "0.5.0", [*_all_base_tags(), *extra]))


def _create_route(router, status: int = 200):
    return router.post("http://127.0.0.1:11434/api/create").mock(
        return_value=Response(status, json={"status": "success"}))


def test_a_local_pick_is_tuned_for_the_step_then_saved_and_undo_puts_it_back(monkeypatch):
    _with_tags(monkeypatch, "qwen3:14b")
    client = _fresh_app()
    with respx.mock as router:
        route = _create_route(router)
        r = client.post("/models/change/tier2", data={"where": "local", "model": "qwen3:14b"})
    assert r.status_code == 200 and "Rating now uses qwen3:14b" in r.text
    # Never a raw tag: a derived model with this step's own room to read is what the step runs.
    assert _settings_row("llm.tiers.tier2.model") == "kestrel-tier2-qwen3-14b"
    assert _settings_row("models.base.kestrel-tier2-qwen3-14b") == "qwen3:14b"
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "kestrel-tier2-qwen3-14b" and sent["from"] == "qwen3:14b"
    assert sent["parameters"]["num_ctx"] >= 16384 and sent["stream"] is False
    assert 'data-testid="mo-change-undo"' in r.text and "mo-change-reset" in r.text
    # The page names the model it was built from, never the derived id.
    page = client.get("/models").text
    row = page.split('data-testid="mo-step-tier2"')[1].split("</tr>")[0]
    assert "qwen3:14b" in row and "kestrel-tier2-qwen3" not in row
    u = client.post(_undo_pick(r))
    assert "Put back as it was." in u.text
    assert _settings_row("llm.tiers.tier2.model") is None


def _undo_pick(response):
    import re

    return re.search(r'hx-post="(/models/change/tier2/undo[^"]*)"', response.text).group(1)


def test_old_model_undo_cannot_undo_the_next_pick(monkeypatch):
    _with_tags(monkeypatch, "qwen3:14b")
    client = _fresh_app()
    with respx.mock as router:
        _create_route(router)
        first = client.post("/models/change/tier2", data={"model": "qwen3:14b"})
    second = client.post("/models/change/tier2", data={"model": "qwen3:8b"})
    out = client.post(_undo_pick(first))
    assert "This can no longer be undone." in out.text
    assert _settings_row("llm.tiers.tier2.model") is None
    assert "Put back as it was." in client.post(_undo_pick(second)).text
    assert _settings_row("llm.tiers.tier2.model") == "kestrel-tier2-qwen3-14b"


def test_a_model_not_downloaded_starts_the_download_and_leaves_the_step_alone(monkeypatch):
    client = _fresh_app()
    r = client.post("/models/change/tier2", data={"where": "local", "model": "qwen3:14b"})
    assert "qwen3:14b is not downloaded yet. The download has started." in r.text
    assert "will not switch to a model that cannot run yet" in r.text
    assert _settings_row("llm.tiers.tier2.model") is None

    async def pulls():
        async with get_sessionmaker()() as s:
            return (await s.execute(select(Task.payload).where(Task.type.like("%pull%")))
                    ).scalars().all()

    assert [p["tag"] for p in asyncio.run(pulls())] == ["qwen3:14b"]


def test_a_tuning_failure_changes_nothing_and_says_why(monkeypatch):
    _with_tags(monkeypatch, "qwen3:14b")
    client = _fresh_app()
    with respx.mock as router:
        _create_route(router, status=500)
        r = client.post("/models/change/tier2", data={"where": "local", "model": "qwen3:14b"})
    assert "could not tune qwen3:14b for this step" in r.text
    assert _settings_row("llm.tiers.tier2.model") is None
    assert _settings_row("models.undo.tier2") is None  # nothing changed, nothing to undo


def test_picking_the_profiles_own_model_is_the_default_again(monkeypatch):
    _with_tags(monkeypatch, "qwen3:14b")
    client = _fresh_app()
    with respx.mock as router:
        _create_route(router)
        client.post("/models/change/tier2", data={"where": "local", "model": "qwen3:14b"})
    assert _settings_row("llm.tiers.tier2.model")
    r = client.post("/models/change/tier2", data={"where": "local", "model": "qwen3:8b"})
    assert "Rating now uses qwen3:8b" in r.text
    assert _settings_row("llm.tiers.tier2.model") is None


def test_reset_to_default_removes_the_override(monkeypatch):
    _with_tags(monkeypatch, "qwen3:14b")
    client = _fresh_app()
    with respx.mock as router:
        _create_route(router)
        client.post("/models/change/tier2", data={"where": "local", "model": "qwen3:14b"})
    r = client.post("/models/change/tier2/reset")
    assert "Back to the default." in r.text
    assert _settings_row("llm.tiers.tier2.model") is None


def test_a_model_kestrel_does_not_know_is_refused():
    r = _fresh_app().post("/models/change/tier2", data={"where": "local", "model": "made-up:1b"})
    assert "does not know that model" in r.text
    assert _settings_row("llm.tiers.tier2.model") is None


def test_an_unknown_step_is_a_404():
    client = _fresh_app()
    assert client.get("/models/change/tier9").status_code == 404
    assert client.post("/models/change/tier9", data={"model": "x"}).status_code == 404


def test_an_online_pick_with_no_connected_provider_is_refused_in_plain_words(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    view = client.get("/models/change/tier2?where=online").text
    assert 'data-testid="mo-online-refused"' in view and "No online provider is connected" in view
    r = client.post("/models/change/tier2", data={"where": "online", "model": _NEMOTRON,
                                                  "confirm": "on"})
    assert "No online provider is connected" in r.text
    assert _settings_row("llm.tiers.tier2.backend") is None


def test_an_online_pick_states_what_is_sent_and_needs_the_tick():
    client = _fresh_app()
    with respx.mock as router:
        _connect(client, router)
    with respx.mock as router:
        _mock(router)
        view = client.get("/models/change/tier2?where=online").text
        assert "What is sent" in view and "mo-confirm-online" in view
        assert "Free only" in view and "data-testid=\"mo-online-list\"" in view
        refused = client.post("/models/change/tier2", data={"where": "online",
                                                            "model": _NEMOTRON})
        assert "Tick the box to confirm what will be sent" in refused.text
        saved = client.post("/models/change/tier2", data={"where": "online", "model": _NEMOTRON,
                                                          "confirm": "on"})
    assert "Saved as override" in saved.text
    assert _db_settings().llm.hosted_tiers["tier2"].model == _NEMOTRON


def test_an_online_model_below_the_steps_budget_is_disabled_with_the_reason():
    client = _fresh_app()
    with respx.mock as router:
        _connect(client, router)
    with respx.mock as router:
        _mock(router)
        view = client.get("/models/change/tier2?where=online").text
    assert "is below this tier" in view and "disabled" in view


def test_the_free_only_filter_hides_paid_models():
    client = _fresh_app()
    with respx.mock as router:
        _connect(client, router)
    with respx.mock as router:
        _mock(router)
        all_models = client.get("/models/change/tier3?where=online").text
        free = client.get("/models/change/tier3?where=online&free=1").text
    assert _PAID in all_models and _PAID not in free


# ----------------------------------------------------------------------------------- speed
def test_speed_saves_whole_numbers_and_refuses_the_rest():
    client = _fresh_app()
    r = client.post("/models/speed", data={"tier1": "6", "tier2": "3"})
    assert "Saved." in r.text
    assert _settings_row("concurrency.tier1") == 6 and _settings_row("concurrency.tier2") == 3
    bad = client.post("/models/speed", data={"tier1": "0", "tier2": "abc"})
    assert "Enter a whole number from 1 to 16." in bad.text
    assert _settings_row("concurrency.tier1") == 6


# ---------------------------------------------------------------------- quality and history
def test_quality_says_too_few_answers_under_five_and_history_stays_behind_details():
    body = _fresh_app().get("/models").text
    assert 'data-testid="mo-quality-few"' in body and "at least 5 needed" in body
    assert "<details" in body.split('id="mo-history"')[0][-80:]
    assert "Speed and cost" in body and "Not in use yet" in body


def test_a_finished_check_with_changes_shows_a_suggestion_banner():
    from tests.model_check_seed import seed_check

    client = _fresh_app()
    seed_check(changes=1)
    body = client.get("/models").text
    assert 'data-testid="mo-suggestion"' in body and "Kestrel recommends one change" in body
    assert "See Model check" in body


# ---------------------------------------------------------------- review fixes: online and undo
def test_an_online_pick_needs_the_operators_approval_in_settings_first():
    """A saved key alone is not consent: the client refuses a send until the disclosure is
    accepted, so the drawer says so up front instead of saving a pick that cannot run."""
    client = _fresh_app()  # the key is in the environment, no approval was ever given
    r = client.post("/models/change/tier2", data={"where": "online", "model": _NEMOTRON,
                                                  "confirm": "on"})
    assert "Online models need your approval first" in r.text
    assert _settings_row("models.undo.tier2") is None


def test_a_refused_online_save_leaves_no_undo_behind():
    client = _fresh_app()
    with respx.mock as router:
        _connect(client, router)
    with respx.mock as router:
        _mock(router)
        r = client.post("/models/change/tier2", data={"where": "online", "model": _GEMMA,
                                                      "confirm": "on"})
    assert "Not saved" in r.text and "is below this tier" in r.text
    assert _settings_row("models.undo.tier2") is None
    assert 'data-testid="mo-change-undo"' not in r.text


def test_undo_never_touches_an_override_that_was_there_before_the_change():
    from kestrel.web.pages import models_ui

    client = _fresh_app()
    with respx.mock as router:
        _connect(client, router)
    with respx.mock as router:
        _mock(router)
        saved = client.post(
            "/models/change/tier2", data={"where": "online", "model": _NEMOTRON, "confirm": "on"}
        )
    assert "Saved as override" in saved.text
    before = _db_settings().llm.hosted_tiers["tier2"].model

    async def local_undo_snapshot():
        async with get_sessionmaker()() as s:
            before = await models_ui.snapshot(s, "tier2", None)
            s.add(Setting(key="llm.tiers.tier2.model", value="local-change"))
            await models_ui.store_snapshot(s, "tier2", before)
            await s.commit()

    asyncio.run(local_undo_snapshot())
    assert "Put back as it was." in client.post(_undo_pick(client.get("/models/change/tier2"))).text
    assert _db_settings().llm.hosted_tiers["tier2"].model == before == _NEMOTRON
    # An undo of the online change itself clears the override it wrote.
    with respx.mock as router:
        _mock(router)
        saved = client.post(
            "/models/change/tier2", data={"where": "online", "model": _NEMOTRON, "confirm": "on"}
        )
    client.post(_undo_pick(saved))
    # The previous online override survives a subsequent pick of the same model.
    assert _state()[1]["tier2"]["model"] == _NEMOTRON
