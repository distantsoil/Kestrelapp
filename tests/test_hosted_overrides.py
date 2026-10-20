"""Hosted model overrides on the Models page (D-UI-42) and the shared model picker on Bench
(D-UI-44). Every provider call is mocked with respx against tests/fixtures/openrouter_models.json,
shaped like OpenRouter's GET /api/v1/models (data[].id, name, context_length, pricing as strings,
supported_parameters). No live endpoint is called."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import respx
from httpx import Response

from kestrel.config import load_settings
from kestrel.llm import providers as prov
from kestrel.services import hosted_models as hm
from tests.test_hosted_mode import _db_settings, _fresh_app

_OR = "https://openrouter.ai/api/v1"
_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "openrouter_models.json").read_text(encoding="utf-8")
)
_NEMOTRON = "nvidia/nemotron-nano-9b-v2:free"
_GEMMA = "google/gemma-3-27b-it:free"  # 8192 context: fine for tier1, too small for tier2
_ZERO = "meta-llama/llama-3.3-8b-instruct"  # priced "0"/"0" without the :free suffix
_PAID = "anthropic/claude-sonnet-4.5"
_JSON_MODE = "mistralai/mistral-small-3.2-24b-instruct"  # lists response_format only
_PROMPT_ONLY = "acme/plain-chat-70b"  # neither response_format nor structured_outputs


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    hm.clear_cache()
    yield
    hm.clear_cache()


def _mock(router, *, models_status: int = 200):
    router.get(f"{_OR}/key").mock(
        return_value=Response(200, json={"data": {"label": "k", "is_free_tier": True}})
    )
    return router.get(f"{_OR}/models").mock(
        return_value=Response(models_status, json=_FIXTURE if models_status == 200 else
                              {"error": {"message": "upstream down"}})
    )


def _connect(client, router):
    _mock(router)
    r = client.post(
        "/settings/hosted/connect",
        data={"provider": "openrouter", "ack": "on", "switch": "on", "free_only": "on",
              "allow_training": "on"},
    )
    assert 'data-connect-ok="true"' in r.text, r.text
    return r


def _state():
    async def _go():
        from kestrel.db.engine import get_sessionmaker

        async with get_sessionmaker()() as s:
            return await hm.read_state(s)

    return asyncio.run(_go())


# ------------------------------------------------------------------------- parsing and filter
def test_fixture_list_parses_to_schema_capable_chat_models_and_free_is_suffix_or_zero_price():
    backend = load_settings().llm.backends["frontier"]

    async def _go():
        with respx.mock as router:
            _mock(router)
            return await hm.fetch_models("frontier", backend)

    models = asyncio.run(_go())
    ids = {m.id for m in models}
    # D-UI-48: a model that lists only response_format (JSON mode) and one that lists neither
    # (prompt only) are admitted beside the schema-capable ones; batch and image models are not.
    assert ids == {_NEMOTRON, "nvidia/llama-3.3-nemotron-super-49b-v1.5", _GEMMA, _ZERO, _PAID,
                   _JSON_MODE, _PROMPT_ONLY}
    picker = hm.picker_models(models)
    free = {p.id for p in picker if p.free}
    assert free == {_NEMOTRON, _GEMMA, _ZERO}
    assert [p.free for p in picker] == sorted((p.free for p in picker), reverse=True)
    by_id = {p.id: p for p in picker}
    assert by_id[_NEMOTRON].price == "free" and by_id[_NEMOTRON].context_length == 128000
    assert by_id[_PAID].price == "$3.00/$15.00 per M in/out"
    assert by_id[_NEMOTRON].name == "NVIDIA: Nemotron Nano 9B V2 (free)"


def test_unknown_pricing_is_never_free():
    m = prov.ModelInfo(id="x/unpriced")
    assert not hm.is_free(m) and hm.price_label(m) == "price not listed"


def test_context_gating_uses_each_tiers_budget():
    gemma = prov.ModelInfo(id=_GEMMA, context_length=8192, prompt_usd_per_m=0,
                           completion_usd_per_m=0)
    assert hm.block_reason(gemma, "tier1", None) is None
    assert hm.block_reason(gemma, "trigger", None) is None  # a Tier 1 sized prompt
    for tier in ("tier2", "tier3", "frontier"):
        why = hm.block_reason(gemma, tier, None)
        assert why and "8,192" in why, tier
    assert hm.context_budget("tier3") == 20480 and hm.context_budget("docsel") == 8192


def test_free_model_is_blocked_while_the_backend_denies_data_collection():
    backend = load_settings().llm.backends["frontier"]
    assert backend.hosted.data_collection == "deny"
    nem = prov.ModelInfo(id=_NEMOTRON, context_length=128000)
    assert "training allowed" in hm.block_reason(nem, "tier1", backend)


# --------------------------------------------------------------------------- the Models page
def test_models_page_without_a_key_says_where_to_connect(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    body = client.get("/models").text
    panel = body.split('data-testid="hosted-overrides"')[1].split('id="model-compare"')[0]
    assert 'data-testid="override-open"' in panel and "—" not in panel
    # The shipped frontier tier is the only assignment; opening without a key says why.
    with respx.mock as router:
        route = _mock(router)
        body = client.get("/models/hosted/overrides?open=1").text
        assert route.call_count == 0
    assert 'data-testid="override-error"' in body and "No key is saved" in body
    assert 'data-testid="label-frontier">Default<' in body


def test_override_flow_save_all_label_connect_rerun_and_reset():
    client = _fresh_app()
    with respx.mock as router:
        _connect(client, router)
    defaults, overrides = _state()
    default_t2 = defaults["tier2"]["model"]
    assert overrides == {} and set(defaults) >= {"tier1", "tier2", "tier3", "frontier"}

    body = client.get("/models").text
    assert 'data-testid="override-open"' in body and "(Override)" not in body
    assert 'data-testid="label-tier2">Default<' in body

    # Opening uses the list Connect just fetched: no second request.
    with respx.mock as router:
        route = _mock(router)
        body = client.get("/models/hosted/overrides?open=1").text
        assert route.call_count == 0
    assert 'data-testid="pick-tier2"' in body and 'data-testid="save-all"' in body
    assert "freeOnly: true" in body  # free choice: Free only starts on
    assert 'x-show="picked"' in body and 'name="save.tier2" value="on" checked' in body
    # Gemma's 8192 window is disabled with the reason on tier2, offered on tier1.
    tier2 = body.split('data-testid="override-row-tier2"')[1].split("</tr>")[0]
    tier1 = body.split('data-testid="override-row-tier1"')[1].split("</tr>")[0]
    gemma2 = tier2.split(f'value="{_GEMMA}"')[1].split("</option>")[0]
    gemma1 = tier1.split(f'value="{_GEMMA}"')[1].split("</option>")[0]
    assert "disabled" in gemma2 and "below this tier" in gemma2
    assert "disabled" not in gemma1
    # Paid models are in the list, hidden while Free only is on.
    paid = tier2.split(f'value="{_PAID}"')[1].split(">")[0]
    assert 'x-bind:hidden="' in paid and "freeOnly" in paid

    # Save all: tier2 on Nemotron, frontier on a paid model; tier1 picked but not ticked.
    with respx.mock as router:
        _mock(router)
        body = client.post(
            "/models/hosted/overrides",
            data={"model.tier2": _NEMOTRON, "save.tier2": "on",
                  "model.frontier": _PAID, "save.frontier": "on",
                  "model.tier1": _GEMMA, "model.docsel": ""},
        ).text
    assert 'data-testid="label-tier2">(Override)<' in body
    # A paid override is allowed with Free models only on, and labeled and announced as billed.
    assert 'data-testid="label-frontier">(Override · paid)<' in body
    assert f"deeper reads: {_PAID} (paid)" in body
    assert "Billed: a paid model is now used for deeper reads" in body
    assert "frontier" not in body.split("Saved as override")[1].split("</div>")[0]
    assert "although Free models only is on" in body
    assert 'data-testid="label-tier1">Default<' in body
    assert 'data-testid="reset-tier2"' in body and 'data-testid="reset-tier1"' not in body
    s = _db_settings()
    assert s.llm.hosted_tiers["tier2"].model == _NEMOTRON
    assert s.llm.tiers["frontier"].model == _PAID
    assert s.llm.hosted_tiers["tier1"].model == defaults["tier1"]["model"]

    # Connect again: the overrides survive, and the result says so.
    with respx.mock as router:
        r = _connect(client, router)
    # Each kept override is named, the paid one marked, and the scope never claims all free.
    assert f"Your override for full review (Tier 2) stays: {_NEMOTRON}." in r.text
    assert f"Your override for deeper reads stays: {_PAID} (paid)." in r.text
    assert "Picked from free models only" not in r.text
    assert "your paid override for deeper reads is billed by openrouter.ai." in r.text
    assert "override on frontier" not in r.text and "frontier keeps" not in r.text
    s = _db_settings()
    assert s.llm.hosted_tiers["tier2"].model == _NEMOTRON
    assert s.llm.tiers["frontier"].model == _PAID

    # Reset to default restores Connect's choice and drops the label.
    body = client.post("/models/hosted/reset", data={"tier": "tier2"}).text
    assert "back on its default" in body and 'data-testid="label-tier2">Default<' in body
    assert _db_settings().llm.hosted_tiers["tier2"].model == default_t2
    assert "tier2" not in _state()[1] and "frontier" in _state()[1]


def test_unknown_id_and_too_small_context_are_refused_by_name():
    client = _fresh_app()
    with respx.mock as router:
        _connect(client, router)
    before = _db_settings().llm.hosted_tiers["tier2"].model
    with respx.mock as router:
        _mock(router)
        body = client.post(
            "/models/hosted/overrides",
            data={"model.tier2": "made/up-model", "save.tier2": "on",
                  "model.tier3": _GEMMA, "save.tier3": "on"},
        ).text
    assert "Not saved" in body and "made/up-model" in body
    assert "is not in the model list from" in body
    assert "deeper read (Tier 3): google/gemma-3-27b-it:free refused, context 8,192" in body
    assert _db_settings().llm.hosted_tiers["tier2"].model == before
    assert _state()[1] == {}


def test_list_failure_is_an_inline_error_with_retry():
    client = _fresh_app()
    with respx.mock as router:
        _connect(client, router)
    hm.clear_cache()
    with respx.mock as router:
        _mock(router, models_status=503)
        body = client.get("/models/hosted/overrides?open=1").text
    assert 'data-testid="override-error"' in body and "could not be fetched" in body
    assert "Try again" in body and "sk-or-test-key" not in body


def test_list_is_cached_then_refreshed_on_request():
    client = _fresh_app()
    with respx.mock as router:
        _connect(client, router)
    hm.clear_cache()
    with respx.mock as router:
        route = _mock(router)
        client.get("/models/hosted/overrides?open=1")
        client.get("/models/hosted/overrides?open=1")
        assert route.call_count == 1
        client.get("/models/hosted/overrides?open=1&refresh=1")
        assert route.call_count == 2


def test_advanced_table_pick_counts_as_an_override_connect_keeps():
    client = _fresh_app()
    with respx.mock as router:
        _connect(client, router)
    form = {f"backend.{t}": "frontier" for t in prov.HOSTED_TIERS}
    s = _db_settings()
    for t in prov.HOSTED_TIERS:
        cfg = s.llm.tiers["frontier"] if t == "frontier" else s.llm.hosted_tiers[t]
        form[f"model.{t}"] = cfg.model
    form["model.docsel"] = _NEMOTRON
    client.post("/settings/hosted/tiers", data=form)
    assert set(_state()[1]) == {"docsel"}
    with respx.mock as router:
        _connect(client, router)
    assert _db_settings().llm.hosted_tiers["docsel"].model == _NEMOTRON


# ------------------------------------------------------------------------------------ wording
def test_settings_and_connect_result_say_the_picks_are_defaults():
    client = _fresh_app()
    body = client.get("/settings/hosted").text
    assert 'data-testid="hosted-defaults-note"' in body
    assert 'href="/models#hosted-overrides"' in body and "are defaults" in body
    with respx.mock as router:
        r = _connect(client, router)
    assert 'data-testid="hosted-connect-result-defaults"' in r.text
    assert "These are defaults" in r.text and "—" not in r.text


# -------------------------------------------------------------------------------------- bench
def test_bench_has_no_free_text_model_entry_and_offers_hosted_models_after_load(monkeypatch):
    from kestrel.web.pages import bench

    monkeypatch.setattr(bench, "_available_models", lambda settings: (["kestrel-tier1"], None))
    client = _fresh_app()
    body = client.get("/bench").text
    assert '<input type="text" name="model"' not in body
    assert 'data-testid="bench-run-model"' in body and 'data-testid="bench-promote-model"' in body
    assert '<optgroup label="Local (Ollama)">' in body and 'value="kestrel-tier1"' in body

    with respx.mock as router:
        _connect(client, router)
    hm.clear_cache()
    body = client.get("/bench").text
    assert 'data-testid="bench-load-hosted"' in body and "hosted:" not in body
    with respx.mock as router:
        _mock(router)
        body = client.get("/bench?hosted=1").text
    assert '<optgroup label="Hosted (OpenRouter)">' in body
    assert f'value="hosted:{_NEMOTRON}"' in body and "128,000 context" in body

    # Promoting a hosted pick saves it as that tier's override.
    body = client.post("/bench/promote", data={"tier": "tier2", "model": f"hosted:{_NEMOTRON}"})
    assert "Saved as override: full review (Tier 2)" in body.text
    assert _db_settings().llm.hosted_tiers["tier2"].model == _NEMOTRON
    assert set(_state()[1]) == {"tier2"}


# ------------------------------------------------------------------- review fixes (D-UI-42..)
def _put_settings(**rows):
    async def _go():
        from kestrel.db.engine import get_sessionmaker

        async with get_sessionmaker()() as s:
            for k, v in rows.items():
                await hm._put(s, k, v)
            await s.commit()

    asyncio.run(_go())


def test_connect_drops_a_kept_override_the_new_privacy_setting_blocks():
    """Finding 3: a paid Connect switches data_collection back to deny, so a kept `:free`
    override would fail every call. It is re-checked, dropped to the default, and named."""
    client = _fresh_app()
    with respx.mock as router:
        _connect(client, router)  # free only, training allowed
    with respx.mock as router:
        _mock(router)
        client.post("/models/hosted/overrides",
                    data={"model.tier3": _NEMOTRON, "save.tier3": "on"})
    assert set(_state()[1]) == {"tier3"}
    with respx.mock as router:
        _mock(router)
        r = client.post("/settings/hosted/connect",
                        data={"provider": "openrouter", "ack": "on", "switch": "on"})
    assert 'data-connect-ok="true"' in r.text
    s = _db_settings()
    assert s.llm.backends["frontier"].hosted.data_collection == "deny"
    assert "tier3" not in _state()[1]
    assert s.llm.hosted_tiers["tier3"].model == _state()[0]["tier3"]["model"] != _NEMOTRON
    assert "cannot run and were cleared" in r.text and "training allowed" in r.text


def test_free_only_connect_keeps_a_paid_tier1_override_and_says_so_in_the_headline():
    """Findings 1 and 2: a paid override stays under Free models only, and neither the
    headline nor the scope line presents the result as all free."""
    client = _fresh_app()
    with respx.mock as router:
        _connect(client, router)
    with respx.mock as router:
        _mock(router)
        body = client.post("/models/hosted/overrides",
                           data={"model.tier1": _PAID, "save.tier1": "on"}).text
    assert 'data-testid="label-tier1">(Override · paid)<' in body
    assert "Billed: a paid model is now used for quick screen (Tier 1)" in body
    assert _state()[1]["tier1"]["free"] is False
    with respx.mock as router:
        r = _connect(client, router)
    head = r.text.split('data-connect-ok="true"')[1]
    assert f"Tier 1 will use {_PAID} (paid override)" in head
    assert f"Your override for quick screen (Tier 1) stays: {_PAID} (paid)." in head
    assert "Picked from free models only" not in head
    assert _db_settings().llm.hosted_tiers["tier1"].model == _PAID


def test_override_paid_flag_falls_back_to_the_free_suffix():
    assert hm._override_paid({"model": _PAID}) is True
    assert hm._override_paid({"model": _NEMOTRON}) is False
    assert hm._override_paid({"model": _ZERO, "free": True}) is False
    assert hm._override_paid(None) is False


def test_save_reset_and_connect_apply_share_one_lock(monkeypatch):
    """Finding 4: the Connect apply step runs holding STATE_LOCK, and save and reset wait for
    it before they read the state."""
    monkeypatch.setattr(hm, "STATE_LOCK", asyncio.Lock())
    seen: list[bool] = []
    real_apply = hm.apply_connect

    async def apply_spy(*a, **k):
        seen.append(hm.STATE_LOCK.locked())
        return await real_apply(*a, **k)

    monkeypatch.setattr(hm, "apply_connect", apply_spy)
    client = _fresh_app()
    with respx.mock as router:
        _connect(client, router)
    assert seen == [True]

    async def waits(make) -> bool:
        from kestrel.db.engine import get_sessionmaker
        from kestrel.services.setting_overrides import load_settings_with_db

        monkeypatch.setattr(hm, "STATE_LOCK", asyncio.Lock())  # bound to this loop
        async with get_sessionmaker()() as s:
            settings = await load_settings_with_db(s)
            await hm.STATE_LOCK.acquire()
            task = asyncio.create_task(make(s, settings))
            await asyncio.sleep(0.05)
            waiting = not task.done()
            hm.STATE_LOCK.release()
            await task
            return waiting

    nem = prov.ModelInfo(id=_NEMOTRON, context_length=128000, prompt_usd_per_m=0,
                         completion_usd_per_m=0)
    assert asyncio.run(waits(lambda s, st: hm.save_overrides(
        s, st, "frontier", st.llm.backends["frontier"], {"tier2": _NEMOTRON}, [nem])))
    assert asyncio.run(waits(lambda s, st: hm.reset_override(s, "tier2")))
    assert "tier2" not in _state()[1]


def test_model_list_cache_is_keyed_on_the_key_and_checks_it_first(monkeypatch):
    """Finding 5: a changed or removed key never answers from the cache, the raw key is never
    in the cache key, and reset forgets the list."""
    backend = load_settings().llm.backends["frontier"]
    models = [prov.ModelInfo(id=_PAID, context_length=200000)]
    hm.prime("frontier", backend, models)
    assert hm.cached("frontier", backend) is not None
    assert not any("sk-or-test-key" in part for k in hm._cache for part in k)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-other-key")
    assert hm.cached("frontier", backend) is None
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    assert hm.cached("frontier", backend) is not None
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(hm.ModelListError, match="No key is saved"):
        asyncio.run(hm.fetch_models("frontier", backend))


def test_reset_forgets_the_model_list_cache(monkeypatch):
    from kestrel.services import reset

    backend = load_settings().llm.backends["frontier"]
    hm.prime("frontier", backend, [prov.ModelInfo(id=_PAID)])
    reset._forget_caches()
    assert hm._cache == {}


def test_advanced_save_leaves_an_untouched_set_elsewhere_tier_alone():
    """Finding 8: the Advanced form posts every tier; only a changed one may become an
    override, so a tier set elsewhere (kestrel setup --hosted) keeps its label."""
    client = _fresh_app()
    with respx.mock as router:
        _connect(client, router)
    _put_settings(**{"llm.hosted_tiers.docsel.backend": "frontier",
                     "llm.hosted_tiers.docsel.model": _NEMOTRON})  # set outside the pages
    s = _db_settings()
    form = {f"backend.{t}": "frontier" for t in prov.HOSTED_TIERS}
    for t in prov.HOSTED_TIERS:
        cfg = s.llm.tiers["frontier"] if t == "frontier" else s.llm.hosted_tiers[t]
        form[f"model.{t}"] = cfg.model
    form["model.tier3"] = _PAID
    r = client.post("/settings/hosted/tiers", data=form)
    assert f"tier3: frontier/{_PAID}" in r.text and "docsel:" not in r.text.split("Saved:")[1]
    assert set(_state()[1]) == {"tier3"}
    body = client.get("/models").text
    assert 'data-testid="label-docsel">Set elsewhere<' in body
    assert 'data-testid="label-tier3">(Override · paid)<' in body


# ------------------------------------------------------------------ JSON mode (D-UI-48)
def test_picker_labels_each_models_json_mode_and_a_json_mode_override_is_sent_json_object():
    from kestrel.llm.client import build_request_body, json_schema_for
    from kestrel.llm.schemas import P1Triage

    client = _fresh_app()
    with respx.mock as router:
        _connect(client, router)
    with respx.mock as router:
        _mock(router)
        body = client.get("/models/hosted/overrides?open=1").text
    tier1 = body.split('data-testid="override-row-tier1"')[1].split("</tr>")[0]
    opt = {mid: tier1.split(f'value="{mid}"')[1].split("</option>")[0]
           for mid in (_NEMOTRON, _JSON_MODE, _PROMPT_ONLY)}
    assert "· structured" in opt[_NEMOTRON]
    assert "· JSON mode" in opt[_JSON_MODE]
    assert "· no JSON guarantee" in opt[_PROMPT_ONLY]
    # Connect's automatic defaults stay on models that honor a strict schema.
    defaults, _ = _state()
    assert {d["model"] for d in defaults.values() if d} & {_JSON_MODE, _PROMPT_ONLY} == set()

    with respx.mock as router:
        _mock(router)
        client.post("/models/hosted/overrides",
                    data={"model.tier1": _JSON_MODE, "save.tier1": "on",
                          "model.docsel": _PROMPT_ONLY, "save.docsel": "on"})
    s = _db_settings()
    assert s.llm.hosted_tiers["tier1"].model == _JSON_MODE
    caps = s.llm.hosted_model_caps
    assert caps[_JSON_MODE].json_mode == "json_object"
    assert caps[_JSON_MODE].context_length == 131072
    assert caps[_PROMPT_ONLY].json_mode == "prompt"
    assert "repetition_penalty" in (caps[_PROMPT_ONLY].params or [])
    # Connect's own picks are recorded too, so the client never guesses a mode.
    assert caps[defaults["tier2"]["model"]].json_mode == "schema"

    backend = s.llm.backends["frontier"]
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    sent = build_request_body(model=_JSON_MODE, messages=msgs, backend=backend, temperature=0,
                              max_tokens=300, schema=json_schema_for(P1Triage),
                              caps=caps[_JSON_MODE])
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["provider"]["require_parameters"] is False
    assert "JSON Schema" in sent["messages"][0]["content"]
    plain = build_request_body(model=_PROMPT_ONLY, messages=msgs, backend=backend, temperature=0,
                               max_tokens=300, schema=json_schema_for(P1Triage),
                               caps=caps[_PROMPT_ONLY])
    assert "response_format" not in plain and plain["repetition_penalty"] == 1.05


# ------------------------------------------------------- Settings > Models (#36, WP-12)
def test_settings_models_tab_lazy_loads_the_panel_not_a_static_table(monkeypatch):
    client = _fresh_app()
    body = client.get("/settings").text
    tab = body.split("x-show=\"tab==='models'\" x-cloak")[1].split("</section>")[0]
    assert 'hx-get="/settings/models"' in tab and 'hx-trigger="intersect once"' in tab
    assert "disabled title=\"Test needs the local model running\"" not in body  # dead button gone
    assert "<table" not in tab  # the panel arrives lazily


def test_settings_models_panel_shows_effective_tiers_profile_and_the_override_loader(
    monkeypatch,
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    panel = client.get("/settings/models").text
    assert 'data-testid="settings-models"' in panel
    for tier in ("tier1", "tier2"):
        assert f'data-testid="effective-{tier}"' in panel
    # The hardware profile select posts to the existing handler; no new write route.
    assert 'hx-post="/setup/profile"' in panel and 'name="profile"' in panel
    # The override panel is the Models page partial, loaded lazily from its existing route.
    assert 'hx-get="/models/hosted/overrides"' in panel
    # One #hosted-overrides at most on the Settings document (the loader, not a copy).
    assert client.get("/settings").text.count('id="hosted-overrides"') == 0


def test_override_partial_says_connect_one_when_no_backend_and_pickers_when_connected(
    monkeypatch,
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    none = client.get("/models/hosted/overrides").text
    assert 'data-testid="override-open"' in none  # frontier ships assigned, so a picker exists
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    with respx.mock as router:
        _connect(client, router)
    connected = client.get("/models/hosted/overrides").text
    assert 'data-testid="override-open"' in connected
    assert 'data-testid="label-tier2">Default<' in connected


def test_saving_from_the_settings_tab_hits_the_same_handler_as_the_models_page():
    client = _fresh_app()
    with respx.mock as router:
        _connect(client, router)
    panel = client.get("/settings/models").text
    assert "/models/hosted/overrides" in panel  # the loader the tab embeds
    with respx.mock as router:
        _mock(router)
        opened = client.get("/models/hosted/overrides?open=1").text
    assert 'hx-post="/models/hosted/overrides"' in opened  # the very form the Models page posts
    with respx.mock as router:
        _mock(router)
        saved = client.post(
            "/models/hosted/overrides", data={"model.tier2": _NEMOTRON, "save.tier2": "on"}
        ).text
    assert 'data-testid="label-tier2">(Override)<' in saved
    assert _db_settings().llm.hosted_tiers["tier2"].model == _NEMOTRON
    assert _NEMOTRON in client.get("/settings/models").text  # the tab shows the new assignment


def test_settings_models_profile_change_uses_the_setup_handler():
    client = _fresh_app()
    panel = client.get("/settings/models").text
    names = [n for n in panel.split('name="profile"')[1].split("</select>")[0].split('value="')[1:]]
    assert names
    pick = names[0].split('"')[0]
    r = client.post("/setup/profile", data={"profile": pick}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "recorded" in r.text
