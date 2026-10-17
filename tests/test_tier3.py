"""Tier 3 escalation (SPEC.md 6.4, hard rule 1): operator-triggered, preview-then-confirm, audited,
confidential-redacted, and NEVER an automatic external path.

The security properties, in order of importance:
  1. Building the preview transmits nothing (read-only).
  2. A wrong/missing confirmation token transmits nothing and never calls the model.
  3. On confirmation the payload sent is byte-for-byte the payload previewed, and every send is
     recorded in frontier_transmission.
  4. Confidential mode redacts the employer BEFORE the payload leaves the machine.
  5. No automatic path can reach the external backend: the client refuses it without explicit
     opt-in, and the worker never opts in.
"""

from __future__ import annotations

import inspect

import pytest
from sqlalchemy import func, select

from kestrel.config import get_settings
from kestrel.db.models import (
    DocMaster,
    DocSection,
    Evaluation,
    FrontierTransmission,
    Opportunity,
    Org,
    Setting,
)
from kestrel.llm.client import ExternalBackendError, LLMClient
from kestrel.pipeline import tier3
from tests.pipeline_fakes import FakeLLM


async def _seed_opp(
    s, *, track="employment", org_name="Acme Corp", desc="Own IT strategy here."
) -> int:
    org = Org(name=org_name, normalized_name=org_name.lower())
    s.add(org)
    await s.flush()
    opp = Opportunity(
        track=track, opp_type="job_posting", org_id=org.id, org_name=org_name,
        title="VP of Information Technology", url="https://x/1", description_md=desc,
        stage="t2_done", version=1, seen_count=1, ghost_score=0.0,
    )
    s.add(opp)
    await s.commit()
    return opp.id


class _RecordingClient(FakeLLM):
    """Records every complete() call; can assert it is never called (the refuse path)."""

    def __init__(self, settings, *, forbid_calls: bool = False) -> None:
        super().__init__(settings)
        self.calls: list[dict] = []
        self.forbid_calls = forbid_calls

    async def complete(self, **kw):
        if self.forbid_calls:
            raise AssertionError("client.complete must NOT be called on a refused escalation")
        self.calls.append(kw)
        return await super().complete(**kw)


async def test_preview_is_read_only_and_transmits_nothing(sm):
    async with sm() as s:
        oid = await _seed_opp(s)
    settings = get_settings()
    async with sm() as s:
        preview = await tier3.build_escalation_preview(s, settings, oid)
        assert preview.messages and preview.confirmation_token
        assert preview.provider == "frontier"
        n = (await s.execute(select(func.count()).select_from(FrontierTransmission))).scalar_one()
        assert n == 0  # nothing recorded
        assert (await s.get(Opportunity, oid)).stage == "t2_done"  # stage unchanged


async def test_wrong_confirmation_token_refuses_and_never_calls_the_model(sm, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")  # available, so ONLY the token gates it
    async with sm() as s:
        oid = await _seed_opp(s)
    settings = get_settings()
    client = _RecordingClient(settings, forbid_calls=True)
    async with sm() as s:
        res = await tier3.transmit_escalation(
            s, client, settings, oid, confirmation_token="not-the-real-token"
        )
    assert res.sent is False
    assert "confirmation" in (res.error or "")
    assert client.calls == []  # the model was never contacted
    async with sm() as s:
        n = (await s.execute(select(func.count()).select_from(FrontierTransmission))).scalar_one()
        assert n == 0
        assert (await s.get(Opportunity, oid)).stage == "t2_done"


async def test_missing_api_key_makes_preview_unavailable_and_blocks_send(sm, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    async with sm() as s:
        oid = await _seed_opp(s)
    settings = get_settings()
    async with sm() as s:
        preview = await tier3.build_escalation_preview(s, settings, oid)
        assert preview.unavailable and "OpenRouter key" in preview.unavailable
        assert "Settings > Hosted" in preview.unavailable and ".env" not in preview.unavailable
        client = _RecordingClient(settings, forbid_calls=True)
        res = await tier3.transmit_escalation(
            s, client, settings, oid, confirmation_token=preview.confirmation_token
        )
    assert res.sent is False and client.calls == []  # no key -> nothing sent


async def test_confirm_transmits_payload_byte_for_byte_and_audits(sm, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    async with sm() as s:
        oid = await _seed_opp(s)
    settings = get_settings()
    async with sm() as s:
        preview = await tier3.build_escalation_preview(s, settings, oid)
    client = _RecordingClient(settings)
    async with sm() as s:
        res = await tier3.transmit_escalation(
            s, client, settings, oid, confirmation_token=preview.confirmation_token
        )
        assert res.sent is True and res.evaluation_id is not None
        # what was transmitted is byte-for-byte what was previewed, and marked external-allowed
        assert client.calls[0]["messages"] == preview.messages
        assert client.calls[0]["allow_external"] is True
        assert client.calls[0]["tier"] == "frontier"
        # the audit row
        trans = (await s.execute(select(FrontierTransmission))).scalars().all()
        assert len(trans) == 1
        assert trans[0].status == "sent"
        assert trans[0].payload == preview.messages
        assert trans[0].payload_sha256 == preview.confirmation_token
        assert trans[0].base_url == preview.base_url and trans[0].model == preview.model
        assert trans[0].evaluation_id == res.evaluation_id
        # a Tier 3 evaluation and the terminal stage
        ev = (await s.execute(select(Evaluation).where(Evaluation.tier == 3))).scalars().all()
        assert len(ev) == 1
        assert (await s.get(Opportunity, oid)).stage == "t3_done"


async def test_confidential_mode_redacts_employer_before_send(sm, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    async with sm() as s:
        # the operator's current employer, and the placeholder they chose
        dm = DocMaster(track="employment", name="resume", doc_kind="resume")
        s.add(dm)
        await s.flush()
        s.add(DocSection(
            doc_master_id=dm.id, section_kind="role",
            org_label="SecretCo", org_label_redacted="[Confidential]",
        ))
        s.add(Setting(key="confidential_mode", value=True))
        # the current employer's name appears in the opportunity text that would be transmitted
        oid = await _seed_opp(s, desc="Previously at SecretCo I led the IT function.")
        await s.commit()
    settings = get_settings()
    async with sm() as s:
        preview = await tier3.build_escalation_preview(s, settings, oid)
    blob = "\n".join(m["content"] for m in preview.messages)
    assert preview.confidential is True
    assert "SecretCo" not in blob  # the employer never appears in the payload
    from kestrel.privacy import _LIVE

    assert any(c in blob for c in _LIVE)  # replaced by a random stand-in code (D-PRIV-1)
    # The token map is built once at preview and bound into the confirmation token, so the
    # transmit restores the reply with exactly this map.
    assert preview.mapping and preview.mapping.emap.names() == ["SecretCo"]
    assert preview.confirmation_token != tier3._token_of(
        preview.model, preview.base_url, preview.messages)
    async with sm() as s:  # a second preview reuses the stored codes: the same token
        again = await tier3.build_escalation_preview(s, settings, oid)
    assert again.confirmation_token == preview.confirmation_token

    # and the transmitted payload is exactly this redacted payload
    client = _RecordingClient(settings)
    async with sm() as s:
        await tier3.transmit_escalation(
            s, client, settings, oid, confirmation_token=preview.confirmation_token
        )
    sent_blob = "\n".join(m["content"] for m in client.calls[0]["messages"])
    assert "SecretCo" not in sent_blob and any(c in sent_blob for c in _LIVE)


async def test_second_identical_transmit_is_refused_as_replay(sm, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    async with sm() as s:
        oid = await _seed_opp(s)
    settings = get_settings()
    async with sm() as s:
        preview = await tier3.build_escalation_preview(s, settings, oid)
    client = _RecordingClient(settings)
    async with sm() as s:
        r1 = await tier3.transmit_escalation(
            s, client, settings, oid, confirmation_token=preview.confirmation_token
        )
        assert r1.sent is True
    async with sm() as s:
        r2 = await tier3.transmit_escalation(
            s, client, settings, oid, confirmation_token=preview.confirmation_token
        )
    assert r2.sent is False and "already transmitted" in (r2.error or "")
    assert len(client.calls) == 1  # the model was contacted exactly once
    async with sm() as s:
        trans = (await s.execute(select(FrontierTransmission))).scalars().all()
        ev = (await s.execute(select(Evaluation).where(Evaluation.tier == 3))).scalars().all()
        assert len(trans) == 1 and len(ev) == 1  # no duplicate egress or evaluation


async def test_confidential_mode_without_redaction_map_refuses_send(sm, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    async with sm() as s:
        s.add(Setting(key="confidential_mode", value=True))  # ON, but no DocSection map configured
        oid = await _seed_opp(s)
        await s.commit()
    settings = get_settings()
    client = _RecordingClient(settings, forbid_calls=True)
    async with sm() as s:
        preview = await tier3.build_escalation_preview(s, settings, oid)
        assert preview.unavailable and "redaction map" in preview.unavailable
        assert preview.confidential is False  # nothing was actually redacted
        res = await tier3.transmit_escalation(
            s, client, settings, oid, confirmation_token=preview.confirmation_token
        )
    assert res.sent is False and client.calls == []  # never transmit the employer unredacted


async def test_external_backend_refused_without_explicit_optin(sm):
    """The client refuses an external backend unless allow_external=True or Hosted mode is on AND
    acknowledged (SPEC 6.4) — so the worker, MCP, or any other path can never transmit off-machine
    by accident (hard rule 1). Hosted mode is off by default, and `enabled` alone is not enough."""
    settings = get_settings()
    assert settings.llm.hosted_mode.active is False  # the shipped default
    client = LLMClient(settings)
    with pytest.raises(ExternalBackendError):
        await client.complete(tier="frontier", messages=[{"role": "user", "content": "x"}])
    settings.llm.hosted_mode.enabled = True  # no acknowledged_at: still refused
    with pytest.raises(ExternalBackendError):
        await client.complete(tier="frontier", messages=[{"role": "user", "content": "x"}])


def test_worker_has_no_external_path():
    """Static guarantee: the worker never opts into the external backend and never runs Tier 3."""
    from kestrel.pipeline import worker

    src = inspect.getsource(worker)
    assert "allow_external" not in src  # the worker cannot opt in
    assert "frontier" not in src  # never touches the external tier
    assert "handle_t3" not in src  # and never runs a Tier 3 handler
