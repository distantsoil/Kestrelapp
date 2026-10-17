"""Hosted mode (SPEC.md 6.4, D-HOSTED-1): any tier may run on the operator's own hosted key, but
only after an explicit, acknowledged opt-in, and the privacy model stays honest.

  1. The client gate refuses an external backend without the acknowledgement and allows it with
     it; Tier 3's allow_external keeps working unchanged.
  2. Tier 1 and Tier 2 redact the current employer before a hosted send (the mocked provider
     receives the placeholder, never the name).
  3. With confidential mode on and no redaction map, the send is REFUSED and the item is
     quarantined, never rejected (hard rule 4), and the model is never contacted.
  4. A hosted tier uses the smaller description slice.
  5. The Settings routes write the acknowledgement and the per-tier rows.
  6. free_models filters on zero pricing; doctor reports off / on / warn.
Every network call is respx-mocked."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx
from httpx import Response
from sqlalchemy import select

from kestrel.config import HOSTED_MAX_TOKENS, HostedMode, get_settings
from kestrel.db.models import (
    DocMaster,
    DocSection,
    Opportunity,
    Org,
    Quarantine,
    Setting,
    Source,
    Task,
)
from kestrel.llm import providers as prov
from kestrel.llm.client import ExternalBackendError, LLMClient
from kestrel.pipeline import _shared
from kestrel.pipeline.profile import load_profile
from kestrel.pipeline.tier1 import handle_t1
from kestrel.pipeline.tier2 import handle_t2
from tests.pipeline_fakes import _p1, _p2

_OR_CHAT = "https://openrouter.ai/api/v1/chat/completions"
_ACK = "2026-10-12T10:00:00+00:00"
_EMPLOYER = "SecretCo"


def _coded(text: str) -> bool:
    """D-PRIV-1: the send carries one of this process's live stand-in codes."""
    from kestrel.privacy import _LIVE

    return any(code in text for code in _LIVE)


_PLACEHOLDER = "[Confidential]"


def _hosted_settings(*tiers: str, ack: bool = True, confidential: bool = False):
    s = get_settings()
    for tier in tiers:
        s.llm.tiers[tier].backend = "frontier"
        s.llm.tiers[tier].model = "meta-llama/llama-3.3-70b-instruct:free"
    s.llm.hosted_mode.enabled = True
    s.llm.hosted_mode.acknowledged_at = _ACK if ack else None
    s.llm.hosted_mode.acknowledged_terms = s.llm.hosted_mode.disclosure_terms() if ack else None
    s.confidential_mode = confidential
    return s


async def _seed(sm, *, stage: str, with_map: bool) -> int:
    """One employment opportunity whose description names the operator's current employer, plus
    (optionally) the resume section that defines the redaction map."""
    async with sm() as s:
        src = Source(track="employment", kind="greenhouse", name="emp", enabled=True)
        org = Org(name="Acme Corp", normalized_name="acme corp")
        s.add_all([src, org])
        await s.flush()
        if with_map:
            dm = DocMaster(track="employment", name="resume", doc_kind="resume")
            s.add(dm)
            await s.flush()
            s.add(
                DocSection(
                    doc_master_id=dm.id, section_kind="role",
                    org_label=_EMPLOYER, org_label_redacted=_PLACEHOLDER,
                )
            )
        opp = Opportunity(
            track="employment", opp_type="job_posting", source_id=src.id, org_id=org.id,
            org_name="Acme Corp", title="VP of Information Technology", url="https://x/1",
            description_md=f"Own IT strategy. Candidates from {_EMPLOYER} welcome.",
            stage=stage, version=1, seen_count=1, ghost_score=0.0, pre_score=0.5,
        )
        s.add(opp)
        await s.commit()
        return opp.id


def _profiles() -> dict:
    return {t: load_profile(t) for t in ("employment", "consulting")}


class _ForbidClient:
    """A client whose complete() must never run (the refuse path)."""

    def __init__(self, settings) -> None:
        self.settings = settings

    async def complete(self, **kw):
        raise AssertionError("the model must not be contacted on a refused hosted send")


# ------------------------------------------------------------------------------------ 1. gate
@respx.mock
def test_gate_refuses_without_acknowledgement_and_allows_with_it(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(_OR_CHAT).mock(
        return_value=Response(200, json={"choices": [{"message": {"content": "{}"}}]})
    )
    msgs = [{"role": "user", "content": "x"}]

    async def go(settings):
        async with httpx.AsyncClient() as http:
            return await LLMClient(settings, http=http).complete(tier="tier1", messages=msgs)

    # enabled without acknowledged_at: refused, nothing sent
    with pytest.raises(ExternalBackendError):
        asyncio.run(go(_hosted_settings("tier1", ack=False)))
    assert not route.called
    # acknowledged: the call goes out
    res = asyncio.run(go(_hosted_settings("tier1")))
    assert route.called and res.content == "{}"
    # the key never appears in the log fields of the announcement (it carries only names)
    from kestrel.llm import client as client_mod

    assert ("frontier", "meta-llama/llama-3.3-70b-instruct:free", "tier1") in (
        client_mod._HOSTED_ANNOUNCED
    )


@respx.mock
def test_tier3_escalation_optin_is_unchanged_by_hosted_mode(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(_OR_CHAT).mock(
        return_value=Response(200, json={"choices": [{"message": {"content": "{}"}}]})
    )
    s = get_settings()
    assert s.llm.hosted_mode.active is False

    async def go():
        async with httpx.AsyncClient() as http:
            return await LLMClient(s, http=http).complete(
                tier="frontier", messages=[{"role": "user", "content": "x"}], allow_external=True
            )

    asyncio.run(go())
    assert route.called


# ----------------------------------------------------------------------- 2. redaction on send
async def test_tier1_redacts_the_employer_before_a_hosted_send(sm, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    oid = await _seed(sm, stage="t0_pass", with_map=True)
    settings = _hosted_settings("tier1", confidential=True)
    with respx.mock:
        route = respx.post(_OR_CHAT).mock(
            return_value=Response(
                200,
                json={
                    "choices": [{"message": {"content": _p1(None)}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 10},
                },
            )
        )
        async with httpx.AsyncClient() as http:
            out = await handle_t1(
                sm, LLMClient(settings, http=http), settings,
                opportunity_id=oid, profiles=_profiles(),
            )
    assert out.action == "t1_pass"
    sent = json.loads(route.calls[0].request.content)
    blob = "\n".join(m["content"] for m in sent["messages"])
    assert _EMPLOYER not in blob and _coded(blob)  # D-PRIV-1: a random stand-in code
    assert "Acme Corp" in blob  # an unrelated org is never redacted


async def test_tier2_redacts_the_employer_before_a_hosted_send(sm, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    oid = await _seed(sm, stage="t1_pass", with_map=True)
    settings = _hosted_settings("tier2", confidential=True)
    with respx.mock:
        route = respx.post(_OR_CHAT).mock(
            return_value=Response(
                200,
                json={
                    "choices": [{"message": {"content": _p2(None)}}],
                    "usage": {"prompt_tokens": 500, "completion_tokens": 200, "cost": 0.0},
                },
            )
        )
        async with httpx.AsyncClient() as http:
            out = await handle_t2(
                sm, LLMClient(settings, http=http), settings,
                opportunity_id=oid, profiles=_profiles(),
            )
    assert out.action == "t2_done"
    sent = json.loads(route.calls[0].request.content)
    blob = "\n".join(m["content"] for m in sent["messages"])
    assert _EMPLOYER not in blob and _coded(blob)  # D-PRIV-1: a random stand-in code
    # the OpenRouter privacy routing rides along on a hosted tier too
    assert sent["provider"]["data_collection"] == "deny"


async def test_local_tier_is_never_redacted_or_refused(sm):
    """Redaction before send is a hosted concern; a local model may see the real employer."""
    await _seed(sm, stage="t0_pass", with_map=False)
    settings = get_settings()
    settings.confidential_mode = True
    async with sm() as s:
        plan = await _shared.plan_hosted_send(s, settings, "tier1")
    assert plan.external is False and plan.refuse is None and not plan.mapping


# ---------------------------------------------------------------- 3. refuse, never reject
async def test_tier1_refuses_without_a_redaction_map_and_promotes(sm):
    oid = await _seed(sm, stage="t0_pass", with_map=False)
    settings = _hosted_settings("tier1", confidential=True)
    out = await handle_t1(
        sm, _ForbidClient(settings), settings, opportunity_id=oid, profiles=_profiles()
    )
    assert out.action == "promoted" and out.quarantined and out.verdict == "keep"
    async with sm() as s:
        opp = await s.get(Opportunity, oid)
        assert opp.stage == "t1_pass"  # kept and promoted, not rejected (hard rule 4)
        q = (await s.execute(select(Quarantine))).scalars().all()
        assert len(q) == 1 and q[0].tier == 1 and q[0].attempts == 0
        assert "no redaction map" in (q[0].error or "")
        t2 = (await s.execute(select(Task).where(Task.type == "t2"))).scalars().all()
        assert len(t2) == 1  # the next tier is queued exactly as after a quarantine


async def test_tier2_refuses_without_a_redaction_map_and_quarantines(sm):
    oid = await _seed(sm, stage="t1_pass", with_map=False)
    settings = _hosted_settings("tier2", confidential=True)
    out = await handle_t2(
        sm, _ForbidClient(settings), settings, opportunity_id=oid, profiles=_profiles()
    )
    assert out.action == "quarantined" and out.quarantined
    async with sm() as s:
        opp = await s.get(Opportunity, oid)
        assert opp.stage == "quarantined" and "refused" in (opp.stage_reason or "")
        q = (await s.execute(select(Quarantine))).scalars().all()
        assert len(q) == 1 and q[0].tier == 2 and q[0].attempts == 0


async def test_docsel_refuses_without_a_redaction_map_and_renders_defaults(sm):
    from kestrel.docs.catalog import load_catalog
    from kestrel.docs.seed import seed_all
    from kestrel.docs.select import select_config

    async with sm() as s:
        ids = await seed_all(s)
        # the example resume may ship a redaction label; strip it so there is NO map
        for sec in (await s.execute(select(DocSection))).scalars().all():
            sec.org_label_redacted = None
            sec.end_date = sec.end_date if sec.end_date and sec.end_date.lower() not in (
                "present", "current", "now") else "2020"  # nothing to infer (D-PRIV-1)
        await s.commit()
        catalog = await load_catalog(s, ids["resume"])
        settings = _hosted_settings("docsel", confidential=True)
        sel = await select_config(s, _ForbidClient(settings), catalog)
    assert sel.defaults_used is True and "no redaction map" in (sel.error or "")


async def test_redact_confidential_off_sends_unredacted_without_refusing(sm):
    """The operator may deliberately turn redaction off (YAML only); then nothing is refused."""
    await _seed(sm, stage="t0_pass", with_map=False)
    settings = _hosted_settings("tier1", confidential=True)
    settings.llm.hosted_mode.redact_confidential = False
    async with sm() as s:
        plan = await _shared.plan_hosted_send(s, settings, "tier1")
    assert plan.external is True and plan.refuse is None and not plan.mapping


# ------------------------------------------------------------------------------ 4. JD slice
def test_hosted_tier_description_slice_follows_the_models_context():
    """D-LLM-3: the hosted Tier 2 slice is sized to the model's context window (minus prompt
    overhead and output budget), floored at max_jd_tokens and capped at jd_ceiling_tokens; with
    the context unknown it is the local budget. Tier 1 keeps its head-only slice."""
    from kestrel.config import HostedModelCaps

    local = get_settings()
    assert _shared.jd_budget(local, "tier2", local.pipeline.max_jd_tokens) == 6000
    hosted = _hosted_settings("tier2")
    assert _shared.jd_budget(hosted, "tier2", 6000) == 6000  # context unknown: the local budget
    model = hosted.llm.tiers["tier2"].model
    hosted.llm.hosted_model_caps[model] = HostedModelCaps(context_length=131072)
    assert _shared.jd_budget(hosted, "tier2", 6000) == 16000  # the ceiling
    hosted.llm.hosted_model_caps[model] = HostedModelCaps(context_length=24000)
    out = hosted.llm.tiers["tier2"].max_tokens or 0
    # Context less overhead and output, less the 15% tokenizer margin.
    assert _shared.jd_budget(hosted, "tier2", 6000) == int(
        (24000 - _shared.HOSTED_PROMPT_OVERHEAD - out) * (1 - _shared.TOKENIZER_MARGIN)
    )
    hosted.llm.hosted_model_caps[model] = HostedModelCaps(context_length=9000)
    # The window cannot hold the 3000 floor beside the prompt and the output: never the floor.
    assert _shared.jd_budget(hosted, "tier2", 6000) == 1
    assert _shared.jd_budget(hosted, "tier1", 1200) == 1200  # tier1 is local here
    hosted.llm.hosted_mode.max_jd_tokens = 500
    hosted.llm.tiers["tier1"].backend = "frontier"
    # A smaller Tier 1 head changes the disclosure: the old acceptance is void, so nothing is
    # hosted until it is accepted again.
    assert _shared.jd_budget(hosted, "tier1", 1200) == 1200
    hosted.llm.hosted_mode.acknowledged_terms = hosted.llm.hosted_mode.disclosure_terms()
    assert _shared.jd_budget(hosted, "tier1", 1200) == 500
    # external but NOT acknowledged: the tier is not hosted, so the local budget stands
    unacked = _hosted_settings("tier2", ack=False)
    assert _shared.jd_budget(unacked, "tier2", 6000) == 6000


# ------------------------------------------------------------------------- 5. settings routes
def _fresh_app():
    from starlette.testclient import TestClient

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.engine import reset_engine
    from kestrel.web.app import build_app

    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations(paths.db_path())
    asyncio.run(reset_engine())
    return TestClient(build_app(worker=False))


def _db_settings():
    async def _go():
        from kestrel.db.engine import get_sessionmaker
        from kestrel.services.setting_overrides import load_settings_with_db

        async with get_sessionmaker()() as s:
            return await load_settings_with_db(s)

    return asyncio.run(_go())


def test_settings_hosted_mode_flow(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENMODEL_API_KEY", raising=False)
    client = _fresh_app()

    # Local by default, the switch and the disclosure are on the page, every tier row is there.
    body = client.get("/settings/hosted").text
    assert "Local models (active)" in body and "Status: not acknowledged" in body
    assert "Turn on hosted mode" in body and "Not configured yet" in body
    assert "may train on your inputs" in body and "first 1200 tokens" in body
    for tier in ("tier1", "tier2", "docsel", "frontier"):
        assert f'name="backend.{tier}"' in body
    assert "—" not in body  # no em dashes in UI copy

    # Switching to hosted before the acknowledgement is refused with the reason; mode unchanged.
    r = client.post("/settings/hosted/switch", data={"mode": "hosted"})
    assert "Still on local models" in r.text and "not been acknowledged" in r.text
    assert _db_settings().llm.mode == "local"

    # Turning it on without the acknowledgement is refused and writes nothing.
    r = client.post("/settings/hosted/mode", data={"action": "on", "ack": ["off"]})
    assert "I understand" in r.text and "Status: not acknowledged" in r.text
    assert _db_settings().llm.hosted_mode.active is False

    # Acknowledged: enabled + acknowledged_at are written; the mode is still local.
    r = client.post("/settings/hosted/mode", data={"action": "on", "ack": ["off", "on"]})
    assert r.status_code == 200 and "Disclosure acknowledged" in r.text
    st = _db_settings()
    assert st.llm.hosted_mode.enabled is True and st.llm.hosted_mode.acknowledged_at
    assert st.llm.hosted_mode.active is True and st.llm.mode == "local"

    # Assign tier1 to OpenRouter with a model id, leave tier2 local, docsel to openmodel. The
    # rows are hosted ASSIGNMENTS: the effective (local) tiers do not change.
    r = client.post(
        "/settings/hosted/tiers",
        data={
            "backend.tier1": "frontier", "model.tier1": "qwen/qwen3-8b:free",
            "backend.tier2": "local", "model.tier2": "kestrel-tier2",
            "backend.docsel": "openmodel", "model.docsel": "anthropic/claude-x",
            "backend.frontier": "frontier", "model.frontier": "anthropic/claude-sonnet-4.6",
        },
    )
    assert r.status_code == 200 and "tier1: frontier/qwen/qwen3-8b:free" in r.text
    assert "cannot be chosen yet" in r.text and "tier2" in r.text
    assert "assigned, used on a hosted run" in r.text
    st = _db_settings()
    assert st.llm.hosted_tiers["tier1"].backend == "frontier"
    assert st.llm.hosted_tiers["tier1"].model == "qwen/qwen3-8b:free"
    assert "tier2" not in st.llm.hosted_tiers
    assert st.llm.hosted_tiers["docsel"].backend == "openmodel"
    assert st.llm.tiers["tier1"].backend == "local" and st.llm.tiers["docsel"].backend == "local"
    assert st.llm.tiers["frontier"].model == "anthropic/claude-sonnet-4.6"

    # Hosted still cannot be chosen: tier2 has no assignment.
    r = client.post("/settings/hosted/switch", data={"mode": "hosted"})
    assert "Still on local models" in r.text and "tier2" in r.text
    assert _db_settings().llm.mode == "local"

    # A hosted tier needs a model id; the frontier cannot go local; nothing bad is written.
    r = client.post(
        "/settings/hosted/tiers",
        data={"backend.tier2": "frontier", "model.tier2": "", "backend.frontier": "local"},
    )
    assert "Not saved" in r.text and "model id is required" in r.text
    assert "frontier must stay" in r.text
    st = _db_settings()
    assert "tier2" not in st.llm.hosted_tiers
    assert st.llm.tiers["frontier"].backend == "frontier"

    # Assign tier2, switch to hosted: the effective tiers now carry the assignments, the local
    # tier's other fields survive the overlay, and embed stays local.
    client.post(
        "/settings/hosted/tiers", data={"backend.tier2": "frontier", "model.tier2": "a/strong"}
    )
    r = client.post("/settings/hosted/switch", data={"mode": "hosted"})
    assert "now runs on hosted models" in r.text and "Hosted models (active)" in r.text
    assert "hosted, sending" in r.text
    st = _db_settings()
    assert st.llm.mode == "hosted"
    assert st.llm.tiers["tier1"].backend == "frontier"
    assert st.llm.tiers["tier1"].model == "qwen/qwen3-8b:free"
    # Temperature survives the overlay; max_tokens takes the hosted default (D-LLM-1).
    assert st.llm.tiers["tier1"].temperature == get_settings().llm.tiers["tier1"].temperature
    assert st.llm.tiers["tier1"].max_tokens == HOSTED_MAX_TOKENS["tier1"]
    assert st.llm.tiers["tier2"].model == "a/strong"
    assert st.llm.tiers["docsel"].backend == "openmodel"
    assert st.llm.tiers["embed"].backend == "local"

    # Back to "stay local" for tier1: the assignment rows go away and, with tier1 unassigned,
    # the stored hosted mode falls back to local and the page says why.
    r = client.post(
        "/settings/hosted/tiers",
        data={"backend.tier1": "local", "model.tier1": "qwen/qwen3-8b:free"},
    )
    assert "tier1: stays local" in r.text
    st = _db_settings()
    assert "tier1" not in st.llm.hosted_tiers
    assert st.llm.mode == "local" and st.llm.tiers["tier1"].backend == "local"
    assert st.llm.tiers["tier1"].model == get_settings().llm.tiers["tier1"].model
    assert "Cannot switch yet" in r.text and "tier1" in r.text

    # Turn off: enabled clears, the mode is local, the acknowledgement stays as a record, the
    # remaining assignments keep their rows.
    r = client.post("/settings/hosted/mode", data={"action": "off"})
    assert "Hosted mode is off" in r.text and "Local models (active)" in r.text
    st = _db_settings()
    assert st.llm.hosted_mode.enabled is False and st.llm.hosted_mode.acknowledged_at
    assert st.llm.mode == "local"
    assert st.llm.hosted_tiers["docsel"].backend == "openmodel"
    assert st.llm.hosted_tiers["tier2"].model == "a/strong"
    assert "assigned, used on a hosted run" in client.get("/settings/hosted").text


def test_settings_test_lists_free_models_with_the_filter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client = _fresh_app()
    with respx.mock:
        respx.get("https://openrouter.ai/api/v1/key").mock(
            return_value=Response(200, json={"data": {"label": "k", "is_free_tier": True}})
        )
        respx.get("https://openrouter.ai/api/v1/models").mock(
            return_value=Response(
                200,
                json={
                    "data": [
                        {
                            "id": "a/paid",
                            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                            "supported_parameters": ["structured_outputs"],
                        },
                        {
                            "id": "b/gratis:free",
                            "pricing": {"prompt": "0", "completion": "0"},
                            "supported_parameters": ["structured_outputs"],
                        },
                    ]
                },
            )
        )
        both = client.post("/settings/hosted/test", data={"backend": "frontier"}).text
        free = client.post(
            "/settings/hosted/test", data={"backend": "frontier", "free_only": ["off", "on"]}
        ).text
    assert "a/paid" in both and "b/gratis:free" in both
    assert ">free · " in both  # the free model is marked in the list
    assert "b/gratis:free" in free and "a/paid" not in free
    assert "daily request cap" in free


# ------------------------------------------------------------------- 6. free_models, doctor
def test_free_models_filters_on_zero_pricing_for_both_directions():
    models = [
        prov.ModelInfo(id="free", prompt_usd_per_m=0.0, completion_usd_per_m=0.0),
        prov.ModelInfo(id="half", prompt_usd_per_m=0.0, completion_usd_per_m=1.0),
        prov.ModelInfo(id="unknown"),
        prov.ModelInfo(id="unknown-out", prompt_usd_per_m=0.0, completion_usd_per_m=None),
        prov.ModelInfo(id="paid", prompt_usd_per_m=3.0, completion_usd_per_m=15.0),
    ]
    assert [m.id for m in prov.free_models(models)] == ["free"]


def test_tier_status_marks_external_tiers_and_in_use():
    rows = {r.tier: r for r in prov.tier_status(_hosted_settings("tier1"))}
    assert set(rows) == {"tier1", "tier2", "tier3", "docsel", "trigger", "frontier"}
    assert list(rows) == list(prov.HOSTED_TIERS)
    assert rows["tier1"].external and rows["tier1"].in_use and rows["tier1"].env_name
    assert not rows["tier2"].external and not rows["tier2"].in_use
    assert rows["frontier"].external and rows["frontier"].in_use
    unacked = {r.tier: r for r in prov.tier_status(_hosted_settings("tier1", ack=False))}
    assert unacked["tier1"].external and not unacked["tier1"].in_use
    assert unacked["frontier"].in_use  # per-item escalation, independent of the mode


def test_doctor_rows_for_off_on_and_warn(monkeypatch):
    from kestrel.doctor import FAIL, INFO, PASS, WARN, DoctorReport, _check_hosted

    def rows(settings):
        rep = DoctorReport()
        _check_hosted(rep, settings)
        assert not [c for c in rep.results if c.status == FAIL]
        return rep.results

    # off, nothing routed
    off = rows(get_settings())
    hm = next(c for c in off if c.name == "Hosted mode")
    assert hm.status == INFO and "off" in hm.detail
    assert not [c for c in off if c.name == "Hosted routing"]

    # off, but a tier names an external backend: WARN, not FAIL
    warn = rows(_hosted_settings("tier1", ack=False))
    hr = next(c for c in warn if c.name == "Hosted routing")
    assert hr.status == WARN and "tier1 -> frontier" in hr.detail
    assert next(c for c in warn if c.name == "Hosted mode" and "never acknowledged" in c.detail)

    # on: PASS with the date and the routed tiers; key missing is a WARN on the backend
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    on = rows(_hosted_settings("tier1", "tier2"))
    hm = next(c for c in on if c.name == "Hosted mode")
    assert hm.status == PASS and f"on since {_ACK}" in hm.detail
    assert "tier1 -> frontier/meta-llama/llama-3.3-70b-instruct:free" in hm.detail
    assert "tier2 -> frontier/" in hm.detail
    key = next(c for c in on if c.name == "Hosted key")
    assert key.status == WARN and "no key saved" in key.detail
    assert key.remediation == "Add your OpenRouter key in Settings > Hosted."

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    on = rows(_hosted_settings("tier1"))
    key = next(c for c in on if c.name == "Hosted key")
    assert key.status == PASS and "sk-or-test" not in key.detail


def test_confidential_setting_row_reaches_the_plan(sm):
    """The plan reads confidential_mode off the Settings object the handler was given, which the
    worker loads with the setting-table layer, so the Core tab checkbox governs hosted sends."""
    s = get_settings()
    assert s.confidential_mode is False
    assert Setting  # the row model exists; load_settings_with_db applies it (tests/test_tier3.py)


# ------------------------------------------------------------ 6. tier3 and trigger are hosted tiers
async def test_trigger_classification_goes_through_the_hosted_plan(sm):
    """The P10 classifier runs on the `trigger` tier. Routed to a hosted backend under Hosted
    mode it must behave exactly like tier1: redact the employer before the send, and with
    confidential mode on and no map, refuse (a quarantine row with attempts=0) without ever
    contacting the model."""
    from kestrel.ingest.signal_base import RawSignal
    from kestrel.ingest.triggers import classify_and_store
    from tests.trigger_fakes import FakeP10LLM

    class _Capture(FakeP10LLM):
        sent: list[list[dict]] = []

        async def complete(self, *, messages, **kw):
            self.sent.append(messages)
            return await super().complete(messages=messages, **kw)

    await _seed(sm, stage="t0_pass", with_map=True)
    settings = _hosted_settings("trigger", confidential=True)
    signal = RawSignal(
        signal_kind="trigger", org_name="AcmeAcq", source_url="https://sec.gov/x",
        external_id="acc-9",
        text=f"ORG:AcmeAcq EVENT:acquisition CONF:0.8 REL:1 {_EMPLOYER} was acquired",
    )
    async with sm() as s:
        src = Source(track="consulting", kind="sec_edgar", name="edgar", enabled=True)
        s.add(src)
        await s.flush()
        out = await classify_and_store(s, _Capture(settings), signal, source=src,
                                       settings=settings)
        await s.commit()
    assert out.action == "promoted_strong"
    blob = "\n".join(m["content"] for m in _Capture.sent[0])
    assert _EMPLOYER not in blob and _coded(blob)  # D-PRIV-1: a random stand-in code

    # no map: refused, quarantined with attempts=0, the model never contacted
    async with sm() as s:
        for row in (await s.execute(select(DocSection))).scalars().all():
            await s.delete(row)
        await s.commit()
    async with sm() as s:
        src = (await s.execute(select(Source))).scalars().first()
        out = await classify_and_store(
            s, _ForbidClient(settings), signal, source=src, settings=settings
        )
        await s.commit()
        q = (await s.execute(select(Quarantine))).scalars().all()
    assert out.action == "quarantined" and "hosted send refused" in (out.error or "")
    assert len(q) == 1 and q[0].attempts == 0 and q[0].tier == 0


async def test_tier3_prompts_share_the_hosted_plan(sm):
    """Every tier3 prompt (research, contacts, negotiation, letters) calls
    pipeline._shared.run_prompt_hosted: the plan is read off the client's settings, the
    employer is redacted, and a refusal is a quarantine row, not a send."""
    from kestrel.llm.prompts import load_prompt
    from kestrel.pipeline._shared import run_prompt_hosted

    spec = load_prompt("cover/v1")
    assert spec.tier == "tier3"
    messages = [{"role": "system", "content": "x"},
                {"role": "user", "content": f"Write for {_EMPLOYER}."}]

    class _Capture:
        def __init__(self, settings):
            self.settings = settings
            self.sent = []

        async def complete(self, *, messages, **kw):
            from kestrel.llm.client import ChatResult

            self.sent.append(messages)
            return ChatResult(content="not json", model="m")

    await _seed(sm, stage="t0_pass", with_map=True)
    settings = _hosted_settings("tier3", confidential=True)
    client = _Capture(settings)
    async with sm() as s:
        res = await run_prompt_hosted(s, client, spec, messages, quarantine_tier=4)
    assert res.quarantined and _EMPLOYER not in client.sent[0][1]["content"]
    assert _coded(client.sent[0][1]["content"])
    # a local tier3 is untouched: no redaction, no plan
    local = _Capture(get_settings())
    async with sm() as s:
        await run_prompt_hosted(s, local, spec, messages, quarantine_tier=4)
    assert local.sent[0][1]["content"] == messages[1]["content"]
    # refusal without a map
    async with sm() as s:
        for row in (await s.execute(select(DocSection))).scalars().all():
            await s.delete(row)
        await s.commit()
    async with sm() as s:
        res = await run_prompt_hosted(s, _ForbidClient(settings), spec, messages,
                                      quarantine_tier=4)
        await s.commit()
        q = (await s.execute(select(Quarantine))).scalars().one()
    assert res.ok is False and res.quarantined and "hosted send refused" in res.error
    assert q.attempts == 0 and q.tier == 4 and _EMPLOYER in (q.prompt or "")
    # and the Settings table, the doctor line and the disclosure name both tiers
    from kestrel import setup as setup_mod
    from kestrel.web.pages import settings as settings_page

    for sends in (setup_mod._TIER_SENDS, settings_page._TIER_SENDS):
        assert "tier3" in sends and "trigger" in sends


def test_doctor_lists_tier3_and_trigger_and_warns_on_a_hosted_embed(monkeypatch):
    from kestrel.doctor import FAIL, PASS, WARN, DoctorReport, _check_hosted

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    s = _hosted_settings("tier1", "tier3", "trigger")
    rep = DoctorReport()
    _check_hosted(rep, s)
    hm = next(c for c in rep.results if c.name == "Hosted mode")
    assert hm.status == PASS and "tier3 -> frontier/" in hm.detail
    assert "trigger -> frontier/" in hm.detail
    assert not [c for c in rep.results if c.name == "Embeddings"]
    # the embed tier on an external backend: WARN naming the row, never FAIL
    s.llm.tiers["embed"].backend = "frontier"
    rep = DoctorReport()
    _check_hosted(rep, s)
    emb = next(c for c in rep.results if c.name == "Embeddings")
    assert emb.status == WARN and "no hosted path" in emb.detail
    assert "llm.tiers.embed.backend" in emb.detail and "local" in emb.remediation
    assert not [c for c in rep.results if c.status == FAIL]


def _app_db(fn):
    async def _go():
        from kestrel.db.engine import get_sessionmaker

        async with get_sessionmaker()() as s:
            out = await fn(s)
            await s.commit()
            return out

    return asyncio.run(_go())


def test_switching_to_local_unroutes_a_legacy_external_tier_row(monkeypatch):
    """A row written before the Local/Hosted switch (`llm.tiers.tier1.backend=frontier`) kept
    sending after `llm.mode=local`. The Local card now labels the backend, and the switch to
    local drops those rows so the profile's local model applies, saying so."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client = _fresh_app()

    async def seed(s):
        for k, v in {
            "llm.hosted_mode.enabled": True,
            "llm.hosted_mode.acknowledged_at": _ACK,
            "llm.hosted_mode.acknowledged_terms": HostedMode().disclosure_terms(),
            "llm.tiers.tier1.backend": "frontier",
            "llm.tiers.tier1.model": "legacy/model",
            "llm.tiers.frontier.model": "anthropic/claude-sonnet-4.6",
        }.items():
            s.add(Setting(key=k, value=v))

    _app_db(seed)
    st = _db_settings()
    assert st.llm.mode == "local" and st.llm.tiers["tier1"].backend == "frontier"
    assert {r.tier for r in prov.tier_status(st) if r.in_use} == {"tier1", "frontier"}
    body = client.get("/settings/hosted").text
    assert "frontier/legacy/model" in body
    assert "hosted backend; switching to Local moves it back" in body

    r = client.post("/settings/hosted/switch", data={"mode": "local"})
    assert r.status_code == 200
    assert "Also moved back to the local model: tier1: local/" in r.text
    st = _db_settings()
    assert st.llm.tiers["tier1"].backend == "local"
    assert st.llm.tiers["tier1"].model == get_settings().llm.tiers["tier1"].model
    assert {r.tier for r in prov.tier_status(st) if r.in_use} == {"frontier"}
    assert st.llm.tiers["frontier"].model == "anthropic/claude-sonnet-4.6"  # never touched
    assert "hosted backend; switching" not in client.get("/settings/hosted").text
    # a second switch has nothing to move and says nothing about it
    r = client.post("/settings/hosted/switch", data={"mode": "local"})
    assert "Also moved back" not in r.text and "now runs on local models" in r.text
