"""The Assess popup on Profile > Documents: a themed dialog offering the current model or another
one for this run only (no setting changes). The model is faked and Ollama's tag list is
monkeypatched; nothing reaches a live endpoint."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from kestrel import paths
from kestrel.config import HostedMode as _HostedMode
from kestrel.llm import providers as prov
from kestrel.llm.client import ChatResult
from tests.test_documents_editor import GOOD, _app

_TERMS = _HostedMode().disclosure_terms()

_LOCAL = ["kestrel-tier2:latest", "kestrel-tier3:latest"]


class _Fake:
    sent: list = []

    def __init__(self, settings, http=None, **_):
        self.settings = settings

    async def complete(self, **kw):
        tier = self.settings.llm.tiers[kw["tier"]]
        type(self).sent.append({"tier": kw["tier"], "backend": tier.backend,
                                "model": tier.model, "settings": self.settings})
        return ChatResult(content=json.dumps(GOOD), model=tier.model)


@pytest.fixture
def app(monkeypatch):
    _Fake.sent = []
    monkeypatch.setattr("kestrel.web.pages.documents._local_assess_models",
                        lambda settings: (list(_LOCAL), None))
    return _app(monkeypatch, _Fake, two_slots=True)


def _db_tier2_model() -> str:
    from kestrel.services.setting_overrides import load_settings_with_db

    async def go():
        from kestrel.db.engine import get_sessionmaker

        async with get_sessionmaker()() as s:
            return (await load_settings_with_db(s)).llm.tiers["tier2"].model

    return asyncio.run(go())


def test_the_assess_button_opens_the_popup_instead_of_running(app):
    client, master_id = app
    page = client.get(f"/profile?tab=documents&master_id={master_id}").text
    btn = page.split('data-testid="assess-btn"')[0].rsplit("<button", 1)[1]
    assert f'hx-get="/documents/{master_id}/assess/choose"' in btn
    assert 'hx-target="#assess-dialog-slot"' in btn and 'aria-haspopup="dialog"' in btn
    assert 'id="assess-dialog-slot"' in page
    assert not _Fake.sent  # opening the page runs nothing


def test_the_popup_offers_the_current_model_and_the_local_models(app):
    client, master_id = app
    body = client.get(f"/documents/{master_id}/assess/choose").text
    assert '<dialog id="assess-model" class="assess-model"' in body
    assert 'aria-labelledby="assess-model-title"' in body and "showModal" in body
    assert re.search(r"Assess with the current model \(kestrel-tier2 \(local\)\)", body)
    assert 'value="kestrel-tier3:latest"' in body and 'data-testid="assess-pick"' in body
    assert "this run only" in body and "—" not in body
    # Hosted mode is off: no hosted model is offered, and the note says where to turn it on.
    assert "hosted:" not in body and "once Hosted mode is on" in body
    assert not _Fake.sent


def test_a_local_pick_runs_this_assessment_only(app):
    client, master_id = app
    r = client.post(f"/documents/{master_id}/assess", data={"model": "kestrel-tier3:latest"})
    assert 'data-testid="assessment-result"' in r.text and "this run only" in r.text
    (sent,) = _Fake.sent
    assert sent["model"] == "kestrel-tier3:latest" and sent["backend"] == "local"
    assert _db_tier2_model() == "kestrel-tier2"  # no setting changed


def test_the_current_model_is_the_default_and_a_bad_pick_is_refused_by_name(app):
    client, master_id = app
    client.post(f"/documents/{master_id}/assess")
    assert _Fake.sent[-1]["model"] == "kestrel-tier2"
    n = len(_Fake.sent)
    r = client.post(f"/documents/{master_id}/assess", data={"model": "llama3:8b"})
    assert "Not assessed" in r.text and "&#39;llama3:8b&#39;" in r.text
    r = client.post(f"/documents/{master_id}/assess", data={"model": "hosted:vendor/x"})
    assert "Not assessed" in r.text and "once Hosted mode is on" in r.text
    assert len(_Fake.sent) == n  # nothing was sent for either


def test_a_hosted_pick_uses_its_json_mode_while_hosted_mode_is_on(app, monkeypatch):
    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import Setting
    from kestrel.services import hosted_models as hm

    client, master_id = app
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")

    async def seed():
        async with get_sessionmaker()() as s:
            s.add_all([Setting(key="llm.hosted_mode.enabled", value=True),
                       Setting(key="llm.hosted_mode.acknowledged_at",
                               value="2026-10-15T00:00:00Z"),
                       Setting(key="llm.hosted_mode.acknowledged_terms",
                               value=_TERMS)])
            await s.commit()

    asyncio.run(seed())
    jm = prov.ModelInfo(id="vendor/json-mode", name="JSON Mode", context_length=131072,
                        prompt_usd_per_m=0.1, completion_usd_per_m=0.2,
                        json_mode="json_object", supported_parameters=["response_format"])

    async def fake_fetch(name, backend, *, refresh=False):
        return [jm]

    monkeypatch.setattr(hm, "fetch_models", fake_fetch)
    tiers_checked = []
    real_block = hm.block_reason

    def recording_block(model, tier, backend):
        tiers_checked.append(tier)
        return real_block(model, tier, backend)

    monkeypatch.setattr(hm, "block_reason", recording_block)
    body = client.get(f"/documents/{master_id}/assess/choose").text
    assert 'value="hosted:vendor/json-mode"' in body and "JSON mode" in body
    # The context check uses the assessment prompt's own tier, not a hardcoded one.
    from kestrel.docs.assess import VERSION
    from kestrel.llm.prompts import load_prompt

    assert tiers_checked and set(tiers_checked) == {load_prompt(VERSION).tier}
    # Kestrel is on local models, so the dialog says the hosted pick is an external send.
    assert 'data-testid="assess-external-note"' in body
    assert "this one assessment runs on" in body and "your resume text will be sent" in body
    assert 'data-testid="assess-hosted-note"' in body and "to that provider" in body
    r = client.post(f"/documents/{master_id}/assess", data={"model": "hosted:vendor/json-mode"})
    assert "vendor/json-mode" in r.text and "this run only" in r.text
    (sent,) = _Fake.sent
    assert sent["model"] == "vendor/json-mode" and sent["backend"] == "frontier"
    assert sent["settings"].llm.hosted_model_caps["vendor/json-mode"].json_mode == "json_object"
    assert _db_tier2_model() == "kestrel-tier2"


def test_every_popup_class_has_a_css_rule():
    tpl = (Path(paths.web_templates_dir()) / "partials" / "assess_model.html").read_text("utf-8")
    css = (Path(paths.web_static_dir()) / "css" / "kestrel.css").read_text("utf-8")
    classes = {c for attr in re.findall(r'class="([^"{]+)"', tpl) for c in attr.split()}
    own = {c for c in classes if c.startswith("assess-model")}
    assert own and all(f".{c}" in css for c in own), own
    rules = css.split("/* the Assess popup")[1]
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", rules.split("\n\n")[0])  # theme variables only


def test_assess_shows_a_running_state_outside_the_closed_popup_and_the_picker_filters():
    """Operator asks: the popup closes when the run starts, so the running panel (model name,
    elapsed timer) lands in #assessment and the Assess button reads "Assessing…"; the model
    picker is a combobox (D-UI-69)."""
    from kestrel.web.deps import templates

    class _M:
        def __init__(self, i):
            self.id, self.name, self.free = f"vendor/model-{i}", f"Model {i}", True
            self.context_length, self.price_label, self.json_mode = 32000, "free", "schema"

    html = templates().get_template("partials/assess_model.html").render(
        master_id=1, doc_word="resume", current_label="deepseek (online)",
        hosted_models=[_M(i) for i in range(12)], hosted_reasons={}, local_models=[],
        hosted_label="OpenRouter", hosted_prefix="")
    assert 'data-testid="assess-running"' in html and "assess-elapsed" in html
    assert "Assessing…" in html and 'data-current-label="deepseek (online)"' in html
    assert 'role="combobox"' in html and "model-search" not in html
    assert "Type to filter models" not in html and 'role="listbox"' in html


def test_empty_other_model_pick_is_refused_not_run_on_the_current_model(app):
    """The "other model" form posts `model=` when nothing was picked (script off or bypassed).
    That is a request for a pick, never a quiet run on the current model."""
    client, master_id = app
    r = client.post(f"/documents/{master_id}/assess", data={"model": ""})
    assert "Pick a model first" in r.text
    assert _Fake.sent == []
    # The current-model form sends no model field and still runs.
    ok = client.post(f"/documents/{master_id}/assess")
    assert "Pick a model first" not in ok.text and len(_Fake.sent) == 1
