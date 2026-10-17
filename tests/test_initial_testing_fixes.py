"""Fixes from the operator's first hands-on session (2026-10-14, D-HOSTED-3, D-SETUP-5, D-UI-10,
D-UI-11): the hosted model list drops what cannot answer a chat completion, the automatic picks
prefer known families over price, the Connect form remembers "free only" and leads back to Setup,
and the shared event loop is kept clear (and watched) so clicks are not swallowed."""

from __future__ import annotations

import asyncio
import time

import pytest
import respx
import structlog
from httpx import Response

from kestrel.llm import providers as prov
from kestrel.setup import default_picks, pick_reason
from tests.test_byok import _openrouter
from tests.test_hosted_connect import _OR, _mock_openrouter
from tests.test_hosted_mode import _db_settings, _fresh_app


def _m(
    model_id: str,
    *,
    prompt: float | None = None,
    completion: float | None = None,
    ctx: int | None = None,
    created: int | None = None,
) -> prov.ModelInfo:
    return prov.ModelInfo(
        id=model_id,
        prompt_usd_per_m=prompt,
        completion_usd_per_m=completion,
        context_length=ctx,
        created=created,
        structured_outputs=True,
    )


# ----------------------------------------------------------------- the OpenRouter model list
@respx.mock
def test_openrouter_list_drops_batch_variants_and_non_text_models():
    """`openai/gpt-5-pro:batch` was picked for tier2 and every call answered HTTP 404 ("cannot be
    used with the chat/completions endpoint"), which broke the resume import. The batch variant,
    and anything that does not emit text are out before the pick; a json_object-only model is
    kept in JSON mode (D-UI-48); a plain structured_outputs model stays, with its release
    time."""
    respx.get(f"{_OR}/models").mock(
        return_value=Response(
            200,
            json={
                "data": [
                    {
                        "id": "openai/gpt-5-pro:batch",
                        "pricing": {"prompt": "0.0000075", "completion": "0.00006"},
                        "supported_parameters": ["structured_outputs"],
                    },
                    {
                        "id": "google/some-music-model",
                        "pricing": {"prompt": "0", "completion": "0"},
                        "architecture": {"output_modalities": ["audio"]},
                        "supported_parameters": ["structured_outputs"],
                    },
                    {
                        "id": "x/json-object-only",
                        "supported_parameters": ["response_format"],
                    },
                    {
                        "id": "deepseek/deepseek-chat-v3.1",
                        "created": 1_750_000_000,
                        "context_length": 164_000,
                        "architecture": {"output_modalities": ["text"]},
                        "pricing": {"prompt": "0.0000002", "completion": "0.0000008"},
                        "supported_parameters": ["structured_outputs", "response_format"],
                    },
                    {"id": "y/unknown-capability"},
                ]
            },
        )
    )
    backend = _openrouter()
    listed = asyncio.run(prov.list_models(backend))
    ids = [m.id for m in listed]
    assert "openai/gpt-5-pro:batch" not in ids
    assert "google/some-music-model" not in ids
    fit = {m.id: m for m in prov.models_fit_for_tier3(listed, backend)}
    # D-UI-48: JSON mode is admitted, recorded as such (schema in the prompt, no strict grammar)
    assert fit["x/json-object-only"].json_mode == "json_object"
    assert "y/unknown-capability" in fit  # unknown stays listed (hard rule 4)
    deep = fit["deepseek/deepseek-chat-v3.1"]
    assert deep.structured_outputs is True and deep.created == 1_750_000_000


def test_openrouter_is_chat_model_rules():
    assert not prov.openrouter_is_chat_model({"id": "a/b:batch"})
    assert not prov.openrouter_is_chat_model(
        {"id": "a/b", "architecture": {"output_modalities": ["image"]}}
    )
    assert prov.openrouter_is_chat_model(
        {"id": "a/b", "architecture": {"output_modalities": ["text", "image"]}}
    )
    assert prov.openrouter_is_chat_model({"id": "a/b:free"})
    assert prov.openrouter_is_chat_model({"id": "a/b", "architecture": {}})


# -------------------------------------------------------------------------- the default picks
def test_default_picks_prefer_a_known_family_over_the_priciest_affordable_model():
    """The old rule took the priciest model under the cap as "strongest", which on a live list is
    a years-old model or a non-chat variant. Family first, newest within it, reasoning builds
    skipped; the fast member of a known family takes tier1 and docsel."""
    pool = [
        _m("openai/gpt-4-32k", prompt=60.0, completion=120.0, ctx=32_000, created=1),
        _m("mistralai/mistral-large-2411", prompt=2.0, completion=6.0, ctx=128_000, created=2),
        _m("deepseek/deepseek-chat-v3-0324", prompt=0.3, completion=0.9, ctx=164_000, created=3),
        _m("deepseek/deepseek-chat-v3.1", prompt=0.2, completion=0.8, ctx=164_000, created=5),
        _m("deepseek/deepseek-r1-0528", prompt=0.5, completion=2.2, ctx=164_000, created=6),
        _m("google/gemini-2.5-flash", prompt=0.3, completion=2.5, ctx=1_000_000, created=4),
        _m("google/gemini-2.5-pro-preview", prompt=1.25, completion=10.0, ctx=1_000_000, created=7),
    ]
    picks = {t: m.id for t, m in default_picks(pool, "openrouter").items()}
    assert picks == {
        "tier1": "google/gemini-2.5-flash",
        "tier2": "deepseek/deepseek-chat-v3.1",
        "docsel": "google/gemini-2.5-flash",
    }


def test_default_picks_in_a_free_pool_use_family_order_then_recency():
    """Every price is zero in a free-only pool, so the old rule reduced to "largest context, then
    the alphabetically last id". The family order decides instead, and a `:free` id matches."""
    pool = [
        _m("z-ai/glm-4.5-air:free", prompt=0.0, completion=0.0, ctx=131_000, created=5),
        _m(
            "deepseek/deepseek-chat-v3-0324:free",
            prompt=0.0,
            completion=0.0,
            ctx=164_000,
            created=3,
        ),
        _m("deepseek/deepseek-r1:free", prompt=0.0, completion=0.0, ctx=164_000, created=6),
        _m("qwen/qwen3-30b-a3b:free", prompt=0.0, completion=0.0, ctx=41_000, created=4),
        _m("meta-llama/llama-3.3-70b-instruct:free", prompt=0.0, completion=0.0, ctx=131_000),
        _m("some-vendor/huge-context:free", prompt=0.0, completion=0.0, ctx=2_000_000, created=9),
    ]
    picks = {t: m.id for t, m in default_picks(pool, "openrouter").items()}
    assert picks["tier2"] == "deepseek/deepseek-chat-v3-0324:free"
    assert picks["tier1"] == picks["docsel"] == "qwen/qwen3-30b-a3b:free"


def test_default_picks_fall_back_to_price_when_no_family_is_known():
    pool = [
        _m("a/cheap", prompt=0.1, completion=0.2, ctx=32_000),
        _m("b/strong", prompt=3.0, completion=15.0, ctx=128_000),
        _m("c/too-pricey", prompt=30.0, completion=60.0, ctx=128_000),
    ]
    picks = {t: m.id for t, m in default_picks(pool, "openrouter").items()}
    assert picks == {"tier1": "a/cheap", "tier2": "b/strong", "docsel": "a/cheap"}


def test_default_picks_use_an_unpickable_model_only_when_nothing_else_exists():
    only = [_m("deepseek/deepseek-r1:free", prompt=0.0, completion=0.0)]
    assert default_picks(only, "openrouter")["tier2"].id == "deepseek/deepseek-r1:free"


def test_pick_reason_states_price_or_free_and_context():
    free = _m("q/x:free", prompt=0.0, completion=0.0, ctx=131_072)
    paid = _m("d/y", prompt=0.2, completion=0.8, ctx=164_000)
    assert pick_reason("tier1", free, {"q/x:free"}) == "tier1 q/x:free (free, 131k context)"
    assert pick_reason("tier2", paid, set()) == "tier2 d/y ($0.20/0.80 per M in/out, 164k context)"
    assert pick_reason("docsel", _m("n/p"), set()) == "docsel n/p (price not listed)"


# ----------------------------------------------------------------- the Connect form in the UI
_FREE_AND_PAID = {
    "data": [
        {
            "id": "qwen/qwen3-30b-a3b:free",
            "context_length": 41_000,
            "pricing": {"prompt": "0", "completion": "0"},
            "supported_parameters": ["structured_outputs"],
        },
        {
            "id": "deepseek/deepseek-chat-v3.1",
            "context_length": 164_000,
            "pricing": {"prompt": "0.0000002", "completion": "0.0000008"},
            "supported_parameters": ["structured_outputs"],
        },
    ]
}


def _mock_free_and_paid(router) -> None:
    router.get(f"{_OR}/key").mock(
        return_value=Response(200, json={"data": {"label": "k", "is_free_tier": True}})
    )
    router.get(f"{_OR}/models").mock(return_value=Response(200, json=_FREE_AND_PAID))


def test_free_only_is_a_remembered_choice_the_form_shows_and_reverses(monkeypatch):
    """ "Free models only" looked like a one-way door: the box was blank on every visit and the
    message never said the picks were limited to free models. The choice is a setting row now,
    the box shows it, the message says how to change it, and connecting again re-picks."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-ok")
    client = _fresh_app()
    with respx.mock as router:
        _mock_free_and_paid(router)
        body = client.post(
            "/settings/hosted/connect",
            # Free models need the explicit training opt-in (D-UI-32).
            data={"provider": "openrouter", "ack": "on", "switch": "on", "free_only": "on",
                  "allow_training": "on"},
        ).text
    assert "Picked from free models only" in body and "connect again with the box" in body
    assert "tier2 qwen/qwen3-30b-a3b:free (free, 41k context)" in body
    assert 'name="free_only" value="on" checked' in body and "(current)" in body
    st = _db_settings()
    assert st.llm.hosted_mode.free_only is True
    assert st.llm.tiers["tier2"].model == "qwen/qwen3-30b-a3b:free"
    # A fresh render of the panel still shows the standing choice.
    assert 'name="free_only" value="on" checked' in client.get("/settings/hosted").text
    with respx.mock as router:
        _mock_free_and_paid(router)
        body = client.post(
            "/settings/hosted/connect",
            data={"provider": "openrouter", "ack": "on", "switch": "on"},
        ).text
    assert "Picked from every model that honors a JSON schema" in body
    assert "tier2 deepseek/deepseek-chat-v3.1 ($0.20/0.80 per M in/out, 164k context)" in body
    st = _db_settings()
    assert st.llm.hosted_mode.free_only is False
    assert st.llm.tiers["tier2"].model == "deepseek/deepseek-chat-v3.1"
    assert "—" not in body


def test_connect_reached_from_setup_offers_the_way_back(monkeypatch):
    """Setup step 6 sends the operator to Settings > Hosted; the setup flow has to come back to
    Setup once the key is accepted, and must not when Settings was opened directly."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-ok")
    client = _fresh_app()
    page = client.get("/settings?return_to=setup").text
    assert 'hx-get="/settings/hosted?return_to=setup"' in page
    assert 'hx-get="/settings/hosted?return_to=setup"' not in client.get("/settings").text
    panel = client.get("/settings/hosted?return_to=setup").text
    assert 'data-testid="setup-origin"' in panel and 'href="/setup"' in panel
    assert 'name="return_to" value="setup"' in panel
    assert 'data-testid="return-to-setup"' not in panel  # not connected yet
    with respx.mock as router:
        _mock_openrouter(router)
        connected = client.post(
            "/settings/hosted/connect",
            data={"provider": "openrouter", "ack": "on", "switch": "on", "return_to": "setup"},
        ).text
    assert "Connected to OpenRouter" in connected
    assert 'data-testid="return-to-setup"' in connected and 'href="/setup"' in connected
    with respx.mock as router:
        _mock_openrouter(router)
        direct = client.post(
            "/settings/hosted/connect",
            data={"provider": "openrouter", "ack": "on", "switch": "on"},
        ).text
    assert "Connected to OpenRouter" in direct and 'data-testid="return-to-setup"' not in direct
    # A failed connect keeps the origin so the retry still leads back.
    with respx.mock as router:
        _mock_openrouter(router, key_status=401)
        failed = client.post(
            "/settings/hosted/connect",
            data={"provider": "openrouter", "ack": "on", "return_to": "setup"},
        ).text
    assert (
        'name="return_to" value="setup"' in failed and 'data-testid="return-to-setup"' not in failed
    )


def test_unknown_return_to_values_are_dropped_not_echoed(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    panel = _fresh_app().get("/settings/hosted?return_to=javascript:alert(1)").text
    assert "javascript:alert" not in panel and 'name="return_to"' not in panel


def test_every_htmx_click_gets_busy_feedback(monkeypatch):
    """A click that waits on the server looked ignored. The shell marks the body busy for the
    life of every htmx request and the stylesheet turns that into a progress cursor."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    assert "htmx:beforeRequest" in client.get("/settings").text
    css = client.get("/static/css/kestrel.css").text
    assert "body.busy" in css and "cursor: progress" in css


# ------------------------------------------------------------------- keeping the loop clear
async def test_drain_scrapes_builds_no_http_client_while_nothing_is_queued(sm, monkeypatch):
    """The scrape drain ran every idle cycle and built an httpx client (a TLS context, parsed on
    the shared loop) each time, for an empty queue. With nothing claimable it returns at once;
    with a task it still builds the client."""
    from kestrel.config import get_settings
    from kestrel.pipeline.worker import Worker
    from kestrel.queue.queue import TaskQueue
    from tests.pipeline_fakes import FakeLLM

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("HttpClient built with an empty scrape queue")

    monkeypatch.setattr("kestrel.ingest.http.HttpClient", Boom)  # imported inside the drain
    settings = get_settings()
    worker = Worker(sm, FakeLLM(settings), settings)
    stats = await worker.drain_scrapes()
    assert stats.completed == 0 and stats.errors == 0
    await TaskQueue(sm).enqueue("scrape", {"source_id": 999_999})
    with pytest.raises(AssertionError, match="HttpClient built"):
        await worker.drain_scrapes()


def test_drift_check_is_throttled_between_tier1_work():
    from kestrel.web import app as app_mod

    app_mod._drift_last["at"] = 0.0
    assert app_mod._drift_due(now=1000.0)
    assert not app_mod._drift_due(now=1000.0 + app_mod._DRIFT_INTERVAL_S - 1)
    assert app_mod._drift_due(now=1000.0 + app_mod._DRIFT_INTERVAL_S + 1)


async def test_stall_monitor_names_a_blocked_event_loop():
    """A frozen click is sync work on the loop; the monitor logs how long it was blocked so the
    operator's next log answers "what froze"."""
    from kestrel.web.app import _stall_monitor

    stop = asyncio.Event()
    with structlog.testing.capture_logs() as logs:
        monitor = asyncio.create_task(_stall_monitor(stop, tick=0.05))
        await asyncio.sleep(0.1)
        time.sleep(0.4)  # the kind of blocking call the monitor exists to expose
        await asyncio.sleep(0.15)
        stop.set()
        await monitor  # ends on its own at the next tick once stop is set
    stalls = [e for e in logs if e.get("event") == "event_loop_stall"]
    assert stalls and stalls[0]["stalled_ms"] >= 250


# ------------------------------------------------------------------------- copy and wayfinding
def test_hosted_intro_is_three_panels_with_a_tiers_link(monkeypatch):
    """The Hosted tab opened with one 170-word paragraph. Now a one-line lead, three panels, and
    a link to the Help section that explains what a tier is."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    page = client.get("/settings").text
    assert 'data-testid="hosted-intro"' in page and 'href="/help#tiers"' in page
    assert "second, broader path" not in page
    help_page = client.get("/help").text
    assert 'id="tiers"' in help_page and 'id="tier-names"' in help_page
    for name in ("tier1", "tier2", "tier3", "docsel", "trigger", "frontier", "embed"):
        assert f'<span class="mono muted">{name}</span>' in help_page
    assert "—" not in page and "—" not in help_page


def test_pursuits_page_says_kestrel_never_applies(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    body = _fresh_app().get("/pursuits").text
    assert 'data-testid="never-applies"' in body and "never applies for" in body


def test_slow_requests_are_logged_with_their_path(monkeypatch):
    from kestrel.web import app as app_mod

    monkeypatch.setattr(app_mod, "_SLOW_REQUEST_MS", 0)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    with structlog.testing.capture_logs() as logs:
        assert client.get("/help").status_code == 200
    slow = [e for e in logs if e.get("event") == "slow_request"]
    assert slow and slow[0]["path"] == "/help" and slow[0]["method"] == "GET"
    assert "ms" in slow[0]


# ------------------------------------------------------------------ review-driven pick rules
def test_fast_markers_match_whole_tokens_not_substrings():
    """ "mini" inside "gemini" made every Gemini id a fast variant, so gemini-2.5-pro took
    triage ahead of a workhorse model. Tokens, not substrings; sizes up to 32b count as small."""
    assert not prov.is_fast_variant("google/gemini-2.5-pro")
    assert prov.is_fast_variant("google/gemini-2.5-flash")
    assert prov.is_fast_variant("openai/gpt-5-mini")
    assert prov.is_fast_variant("mistralai/mistral-small-3.2-24b-instruct")
    assert prov.is_fast_variant("qwen/qwen3-30b-a3b:free")
    assert not prov.is_fast_variant("meta-llama/llama-3.3-70b-instruct")
    assert not prov.is_fast_variant("some/elite-model")
    pool = [
        _m("deepseek/deepseek-chat-v3.1", prompt=0.2, completion=0.8, ctx=164_000, created=5),
        _m("google/gemini-2.5-pro", prompt=1.25, completion=10.0, ctx=1_000_000, created=7),
    ]
    picks = {t: m.id for t, m in default_picks(pool, "openrouter").items()}
    assert picks["tier1"] == picks["docsel"] == "deepseek/deepseek-chat-v3.1"


def test_unpicked_markers_match_tokens_and_variant_suffixes():
    assert not prov.is_pickable("deepseek/deepseek-r1-0528")
    assert not prov.is_pickable("google/gemini-2.0-flash-exp:free")
    assert not prov.is_pickable("anthropic/claude-sonnet-4.5:extended")
    assert not prov.is_pickable("openai/gpt-5:online")
    assert prov.is_pickable("some/expert-model")  # "exp" is not a token here
    assert not prov.is_pickable("some/preview-tts")  # "preview" is a token here
    assert prov.is_pickable("deepseek/deepseek-chat-v3.1:free")


def test_paid_id_outranks_its_free_twin_when_both_are_listed():
    """With "free only" cleared, the free twin (rate-limited, may train on inputs) must not win on
    a later `created` stamp."""
    pool = [
        _m("deepseek/deepseek-chat-v3.1", prompt=0.2, completion=0.8, ctx=164_000, created=5),
        _m("deepseek/deepseek-chat-v3.1:free", prompt=0.0, completion=0.0, ctx=164_000, created=8),
        _m("qwen/qwen3-30b-a3b", prompt=0.1, completion=0.3, ctx=41_000, created=4),
        _m("qwen/qwen3-30b-a3b:free", prompt=0.0, completion=0.0, ctx=41_000, created=9),
    ]
    picks = {t: m.id for t, m in default_picks(pool, "openrouter").items()}
    assert picks["tier2"] == "deepseek/deepseek-chat-v3.1"
    assert picks["tier1"] == "qwen/qwen3-30b-a3b"


def test_busy_cursor_skips_polled_partials(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    page = _fresh_app().get("/runs").text
    assert "indexOf('every')" in page and 'hx-trigger="every 5s' in page
