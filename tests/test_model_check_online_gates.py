"""Model check, review fixes (D-UI-147): every way a check can send to an online model passes the
same gates (a connected provider with a saved key, the operator's approval of the current
disclosure, a ticked confirmation), the queue repeats them, and confidential mode redacts or holds
what an online candidate is sent. Offline: TestClient on the real app, scripted runners and fake
clients, no network."""

from __future__ import annotations

import asyncio
import html
import json
import re

import pytest
import respx
from sqlalchemy import select

from kestrel.bench import check
from kestrel.config import load_settings
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Label, Opportunity, Task
from kestrel.llm.profiles import Hardware
from kestrel.privacy import Employer, EmployerMap
from kestrel.services import hosted_models as hm
from kestrel.services import setup_state as ss
from kestrel.setup import tier_sends
from tests.model_check_seed import seed_labels
from tests.test_bench import (
    FakeBenchClient,
    _consulting_rows,
    _employment_rows,
    _write_goldset,
)
from tests.test_hosted_mode import _fresh_app
from tests.test_hosted_overrides import _connect

PROVIDER = "frontier"  # the backend OpenRouter connects as


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.setattr(ss, "detected_hardware",
                        lambda: Hardware(os="linux", gpu_vendor="nvidia", vram_gb=12.0,
                                         ram_gb=32.0))
    monkeypatch.setattr(ss, "probe_ollama", lambda settings, fresh=False: ("0.5.0", []))
    from kestrel.web.pages import bench

    monkeypatch.setattr(bench, "_available_models", lambda settings: ([], None))
    hm.clear_cache()
    yield
    hm.clear_cache()


def _tasks() -> list[Task]:
    async def go():
        async with get_sessionmaker()() as s:
            return (await s.execute(select(Task).where(Task.type == check.TASK_TYPE))
                    ).scalars().all()

    return asyncio.run(go())


def _approved_app(rated: int = 6):
    client = _fresh_app()
    seed_labels(rated)
    with respx.mock as router:
        _connect(client, router)  # a saved key and the accepted disclosure
    return client


def _approval(client) -> str:
    page = client.get("/bench").text
    return html.unescape(
        re.search(r'name="online_approval" value=\'([^\']*)\'', page).group(1)
    )


def _candidate_approval(model: str, tier: str = "tier2") -> str:
    settings = load_settings()
    backend = settings.llm.backends[PROVIDER]
    return json.dumps({tier: [f"{PROVIDER}|{backend.base_url}|{model}"]})


# ------------------------------------------------------------------ /bench/run (Advanced form)
def test_advanced_online_pick_without_a_connected_provider_is_refused(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    seed_labels(6)
    r = client.post("/bench/run", data={"tier": "tier2", "model": "hosted:vendor/x",
                                        "confirm": PROVIDER})
    assert "Connect an online provider first" in r.text and _tasks() == []


def test_advanced_online_pick_without_approval_is_refused():
    client = _fresh_app()
    seed_labels(6)  # a key in the environment, but the disclosure was never accepted
    r = client.post("/bench/run", data={"tier": "tier2", "model": "hosted:vendor/x",
                                        "confirm": PROVIDER})
    assert "Online models need your approval first" in r.text and _tasks() == []


def test_advanced_online_pick_without_the_tick_is_refused_and_a_wrong_tick_too():
    client = _approved_app()
    for data in ({}, {"confirm": "some-other-provider"}):
        r = client.post("/bench/run", data={"tier": "tier2", "model": "hosted:vendor/x", **data})
        assert "Tick the box to confirm what will be sent" in r.text and _tasks() == []


def test_advanced_online_pick_with_every_gate_queues_on_the_connected_provider():
    client = _approved_app()
    approval = json.loads(_approval(client))
    approval.setdefault("tier2", []).extend(json.loads(_candidate_approval("vendor/x"))["tier2"])
    r = client.post("/bench/run", data={"tier": "tier2", "model": "hosted:vendor/x",
                                        "confirm": PROVIDER,
                                        "online_approval": json.dumps(approval)})
    assert 'data-testid="ck-live"' in r.text
    [task] = _tasks()
    assert task.payload["models"]["rating"][1] == {"model": "vendor/x", "backend": PROVIDER}


def test_advanced_external_backend_with_a_local_tag_is_gated_too():
    client = _approved_app()
    r = client.post("/bench/run", data={"tier": "tier2", "model": "gemma3:12b",
                                        "backend": PROVIDER})
    assert "Tick the box to confirm what will be sent" in r.text and _tasks() == []


def test_advanced_local_pick_on_a_local_tier_needs_no_tick():
    client = _fresh_app()
    seed_labels(6)
    r = client.post("/bench/run", data={"tier": "tier2", "model": "gemma3:12b",
                                        "backend": "local"})
    assert 'data-testid="ck-live"' in r.text and len(_tasks()) == 1


def _tier2_online_in_use(monkeypatch):
    """Tier 2 already runs on the connected online provider (the model in use is online)."""
    from kestrel.web.pages import bench

    orig = bench.load_settings_with_db

    async def patched(session):
        st = (await orig(session)).model_copy(deep=True)
        st.llm.tiers["tier2"] = st.llm.tiers["tier2"].model_copy(
            update={"backend": PROVIDER, "model": "vendor/in-use"})
        return st

    monkeypatch.setattr(bench, "load_settings_with_db", patched)


def test_advanced_local_pick_on_an_online_tier_needs_the_tick(monkeypatch):
    client = _approved_app()
    _tier2_online_in_use(monkeypatch)
    for data in ({}, {"confirm": "some-other-provider"}):
        r = client.post("/bench/run", data={"tier": "tier2", "model": "gemma3:12b",
                                            "backend": "local", **data})
        assert "Tick the box to confirm what will be sent" in r.text and _tasks() == []


def test_advanced_local_pick_on_an_online_tier_needs_approval(monkeypatch):
    client = _fresh_app()
    seed_labels(6)  # a key in the environment, but the disclosure was never accepted
    _tier2_online_in_use(monkeypatch)
    r = client.post("/bench/run", data={"tier": "tier2", "model": "gemma3:12b",
                                        "backend": "local", "confirm": PROVIDER})
    assert "Online models need your approval first" in r.text and _tasks() == []


def test_advanced_local_pick_on_an_online_tier_needs_a_connected_provider(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    seed_labels(6)
    _tier2_online_in_use(monkeypatch)
    r = client.post("/bench/run", data={"tier": "tier2", "model": "gemma3:12b",
                                        "backend": "local", "confirm": PROVIDER})
    assert _tasks() == [] and 'data-testid="ck-live"' not in r.text


def test_advanced_local_pick_on_an_online_tier_with_every_gate_queues(monkeypatch):
    client = _approved_app()
    _tier2_online_in_use(monkeypatch)
    r = client.post("/bench/run", data={"tier": "tier2", "model": "gemma3:12b",
                                        "backend": "local", "confirm": PROVIDER,
                                        "online_approval": _candidate_approval("vendor/in-use")})
    assert 'data-testid="ck-live"' in r.text
    [task] = _tasks()
    assert task.payload["models"]["rating"][0] == {"model": "vendor/in-use", "backend": PROVIDER}


# ------------------------------------------------------------------ POST /bench/check (JSON)
def test_json_check_refuses_an_online_candidate_that_is_not_a_connected_provider():
    client = _fresh_app()
    seed_labels(6)
    body = {"online": {"rating": [{"model": "x", "backend": "y"}]}}
    r = client.post("/bench/check", json=body)
    assert r.status_code == 409 and r.json()["queued"] is False
    assert r.json()["reason"] == "online_refused" and _tasks() == []


def test_json_check_applies_every_gate_to_online_and_to_models():
    client = _approved_app()
    online = {"online": {"rating": [{"model": "vendor/x", "backend": PROVIDER}]}}
    no_tick = client.post("/bench/check", json=online)
    assert no_tick.status_code == 409 and "Tick the box" in no_tick.json()["message"]
    wrong_backend = client.post("/bench/check", json={
        "online": {"rating": [{"model": "vendor/x", "backend": "local"}]}, "confirm": True})
    assert wrong_backend.status_code == 409 and _tasks() == []
    via_models = client.post("/bench/check", json={
        "models": {"rating": [{"model": "vendor/x", "backend": PROVIDER}]}})
    assert via_models.status_code == 409 and "Tick the box" in via_models.json()["message"]
    approval = json.loads(_approval(client))
    approval.setdefault("tier2", []).extend(json.loads(_candidate_approval("vendor/x"))["tier2"])
    ok = client.post("/bench/check", json={**online, "confirm": True,
                                            "online_approval": approval})
    assert ok.status_code == 200 and ok.json()["queued"] is True


def test_json_check_without_online_candidates_is_unchanged():
    client = _fresh_app()
    seed_labels(5)
    assert client.post("/bench/check", json={"steps": ["rating"]}).json()["queued"] is True


# ------------------------------------------------------------------ queue_check itself
async def _queue(sm, settings, **kw):
    if "online_approval" not in kw:
        kw["online_approval"] = check.approval_for_candidates(
            settings,
            kw.get("steps", check.DEFAULT_STEPS),
            kw.get("models"),
            kw.get("online"),
        )
    async with sm() as s:
        for i in range(5):
            o = Opportunity(track="employment", opp_type="posting", title=f"t{i}")
            s.add(o)
            await s.flush()
            s.add(Label(opportunity_id=o.id, track="employment", verdict="apply", score=4.0))
        await s.commit()
    async with sm() as s:
        return await check.queue_check(s, settings=settings, **kw)


def _online_settings(*, approved: bool = True, key: bool = True):
    s = load_settings()
    back = s.llm.backends["frontier"]
    s.llm.backends["frontier"] = back.model_copy(
        update={"api_key": "sk-test" if key else None, "api_key_env": "NO_SUCH_KEY_VAR"})
    hmode = s.llm.hosted_mode
    if approved:
        hmode.enabled = True
        hmode.acknowledged_at = "2026-01-01T00:00:00+00:00"
        hmode.acknowledged_terms = hmode.disclosure_terms()
    return s


async def test_queue_check_itself_refuses_an_unknown_backend(sm, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    got = await _queue(sm, _online_settings(), online={"rating": [{"model": "x", "backend": "y"}]})
    assert not got.queued and got.reason == "online_refused"


async def test_queue_check_refuses_with_no_saved_key_or_no_approval(sm, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    online = {"rating": [{"model": "x", "backend": "frontier"}]}
    no_key = await _queue(sm, _online_settings(key=False), online=online)
    assert not no_key.queued and "Connect an online provider" in no_key.message
    no_ack = await _queue(sm, _online_settings(approved=False), online=online)
    assert not no_ack.queued and "approval" in no_ack.message


async def test_queue_check_refuses_a_local_backend_named_as_online_and_a_bare_model(sm):
    s = _online_settings()
    for online in ({"rating": [{"model": "x", "backend": "local"}]}, {"rating": ["bare-model"]}):
        got = await _queue(sm, s, online=online)
        assert not got.queued and got.reason == "online_refused"


async def test_queue_check_refuses_an_external_backend_hidden_in_models(sm):
    s = _online_settings(approved=False)
    got = await _queue(sm, s, models={"rating": ["cur", {"model": "x", "backend": "frontier"}]})
    assert not got.queued and got.reason == "online_refused"


async def test_queue_check_accepts_the_connected_provider_when_approved(sm):
    got = await _queue(sm, _online_settings(),
                       online={"rating": [{"model": "vendor/x", "backend": "frontier"}]})
    assert got.queued


def test_the_connected_provider_rule_matches_models(monkeypatch):
    from kestrel.web.pages import models_ui

    for key in (True, False):
        for flag in (True, False):
            s = _online_settings(approved=flag, key=key)
            assert check.connected_provider(s) == models_ui.connected_backend(s)


# ------------------------------------------------------------------ confidential mode
def _confidential_settings(settings):
    s = settings.model_copy(deep=True)
    s.llm.tiers["tier1"] = s.llm.tiers["tier1"].model_copy(update={"model": "cur-quick"})
    s.llm.tiers["tier2"] = s.llm.tiers["tier2"].model_copy(update={"model": "cur-rate"})
    s.llm.backends["hosted"] = s.llm.backends["local"].model_copy(update={"external": True})
    hmode = s.llm.hosted_mode
    hmode.enabled = True
    hmode.acknowledged_at = "2026-01-01T00:00:00+00:00"
    hmode.acknowledged_terms = hmode.disclosure_terms()
    hmode.redact_confidential = True
    s.confidential_mode = True
    return s


async def _run(sm, settings, runner, monkeypatch, tmp_path=None):
    monkeypatch.setattr(check, "online_refusal", lambda *a, **k: None)  # gates have own tests
    got = await _queue(sm, settings, steps=["rating"],
                       online={"rating": [{"model": "web-model", "backend": "hosted"}]})
    assert got.queued
    async with sm() as s:
        task = (await s.execute(select(Task).where(Task.type == "bench"))).scalar_one()
    out = await check.run_check(sm, task, settings, runner, [])
    assert out.status == "done"
    return out


async def test_a_confidential_online_send_with_no_employer_chosen_is_held_not_sent(
    sm, monkeypatch
):
    settings = _confidential_settings(load_settings())
    called: list[tuple[str, str | None]] = []

    async def runner(step, model, s, backend=None):
        called.append((model, backend))
        return check.StepResult(step, model, backend == "hosted", 20, 0.8, 10.0, 1.0,
                                backend=backend or "local")

    out = await _run(sm, settings, runner, monkeypatch)
    assert called == [("cur-rate", None)]  # the online candidate was never called
    assert out.check["held"] and out.check["held"][0]["model"] == "web-model"
    assert ((out.check["steps"][0]["best"] or {}).get("model")) != "web-model"  # never wins


async def test_a_confidential_online_send_replaces_the_current_employer(sm, tmp_path, monkeypatch):
    from kestrel.docs import current_employer

    async def fake_map(session, **kw):
        return EmployerMap([Employer("Acme Health", "a current employer")])

    monkeypatch.setattr(current_employer, "load_employer_map", fake_map)
    settings = _confidential_settings(load_settings())
    _write_goldset(tmp_path, "employment", _employment_rows())
    _write_goldset(tmp_path, "consulting", _consulting_rows())
    seen: dict[str, list[str]] = {}

    class Client(FakeBenchClient):
        async def complete(self, *, tier, messages, **kw):
            model = self.settings.llm.tiers[tier].model
            seen.setdefault(model, []).append("\n".join(m.get("content", "") for m in messages))
            return await super().complete(tier=tier, messages=messages, **kw)

    run = check.default_runner(lambda s: Client(s), gold_dir=tmp_path)
    out = await _run(sm, settings, run, monkeypatch)
    assert any("Acme Health" in text for text in seen["cur-rate"])  # the local model sees it all
    assert seen["web-model"] and not any("Acme Health" in t for t in seen["web-model"])
    assert not out.check["held"]


async def test_a_local_candidate_is_never_redacted_or_held(sm, monkeypatch):
    settings = _confidential_settings(load_settings())
    plan = await check._send_plan(sm, settings, "rating", "cur-rate", None)
    assert not plan.refuse and not plan.mapping


# ------------------------------------------------------------------ the page: wording, confirm
def test_the_card_states_the_confidential_employer_rule_and_uses_the_disclosure_words():
    client = _approved_app()
    page = html.unescape(client.get("/bench").text)
    sends = tier_sends(load_settings())
    assert f"for Quick screen, {sends['tier1']}" in page
    assert sends["tier3"] in page  # Deeper read, and in the Advanced confirm
    assert 'data-testid="ck-adv-confirm"' in page


def test_the_advanced_confirm_is_tied_to_the_connected_provider():
    client = _approved_app()
    page = client.get("/bench").text
    assert f'name="confirm" value="{PROVIDER}"' in page


def test_the_card_holds_the_advanced_section_and_has_no_inline_styles():
    client = _approved_app()
    page = client.get("/bench").text
    card = page.split('<section class="k-card mo-card mo-check-card" id="ck-card"')[1].split(
        "</section>")[0]
    assert 'id="ck-advanced"' in card
    body = page.split('class="mo-page"')[1]
    assert "style=" not in body
    panel = client.get("/bench/panel").text
    assert "style=" not in panel and 'id="ck-advanced"' in panel


def test_the_instructions_choice_reads_not_recorded_and_a_previous_pick_is_refused():
    client = _approved_app()
    page = client.get("/bench").text
    assert 'name="instructions"' in page and "The version before it (Not recorded)" in page
    r = client.post("/bench/run", data={"tier": "tier2", "model": "gemma3:12b",
                                        "instructions": "previous"})
    assert "no earlier version of its instructions" in r.text and _tasks() == []


def test_scoring_quality_says_not_recorded_for_the_two_counters_without_data():
    client = _fresh_app()
    page = client.get("/models").text
    assert "Fixed after one retry" in page and "Interview questions predicted" in page
    card = page.split('<section class="k-card mo-card" id="mo-quality"')[1]
    quality = card.split("</section>")[0]
    assert quality.count("Not recorded") == 2


def test_the_apply_box_uses_the_disclosure_words():
    from tests.model_check_seed import seed_check

    client = _approved_app(9)
    seed_check(changes=1, online_best=True)
    page = html.unescape(client.get("/bench").text)
    box = page.split('data-testid="ck-confirm-online"')[1].split("</label>")[0]
    assert tier_sends(load_settings())["tier2"] in box
