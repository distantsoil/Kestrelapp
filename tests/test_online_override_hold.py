"""A task that names its own online model is sent only with a confirmation (D-SEC-22).

INVARIANT: nothing is sent to an online model without the confirmed dialog matching the model,
on every path, including tasks with a `model_override` and tasks created by any producer. An
unstamped task whose `model_override` names an online model is held, never sent; a run's one-run
online pick carries the run's confirmation from the scrape through Tier 0 and Tier 1 to the Tier 2
task. Invented roles; the model is a recording fake and the network is closed."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from kestrel.config import HostedMode
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Opportunity, Setting, Task
from kestrel.pipeline._shared import settings_with_override
from kestrel.services import run_model
from kestrel.services.online_consent import destination_id, held_for_approval
from kestrel.services.setting_overrides import load_settings_with_db
from tests import role_seed
from tests.pipeline_fakes import FakeLLM
from tests.test_contracts import recorder  # noqa: F401  (the fixture)

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

ONLINE = {"backend": "frontier", "model": "vendor/invented-pick", "json_mode": "schema",
          "context_length": 200000}


class _Rec(FakeLLM):
    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.sent: list[tuple[str, bool]] = []

    async def complete(self, **kw):
        tcfg = self.settings.llm.tiers[kw["tier"]]
        self.sent.append((tcfg.model, self.settings.llm.backends[tcfg.backend].external))
        return await super().complete(**kw)


async def _hosted_on(s) -> None:
    for k, v in {
        "llm.hosted_mode.enabled": True,
        "llm.hosted_mode.acknowledged_at": "2026-10-15T00:00:00Z",
        "llm.hosted_mode.acknowledged_terms": HostedMode().disclosure_terms(),
    }.items():
        await s.merge(Setting(key=k, value=v))


def _drain(stamp: bool) -> tuple[list, str]:
    async def go():
        ids = await role_seed.seed_async()
        sm = get_sessionmaker()
        async with sm() as s:
            await _hosted_on(s)
            await s.commit()
            settings = await load_settings_with_db(s)
            payload = {"opportunity_id": ids["strong"], "rescore": True, "requested": True,
                       "requested_at": "2099-01-01T00:00:00Z", "model_override": ONLINE}
            if stamp:
                eff = settings_with_override(settings, "tier2", ONLINE)
                payload["online_approval"] = {"tier2": [destination_id(eff, "tier2")]}
            s.add(Task(type="t2", payload=payload, status="pending", priority=1000,
                       attempts=0, max_attempts=3))
            await s.commit()
        assert not settings.llm.backends[settings.llm.tiers["tier2"].backend].external
        from kestrel.pipeline.tier2 import handle_t2
        from kestrel.pipeline.worker import TierStats, Worker

        client = _Rec(settings)
        await Worker(sm, client, settings).drain_tier("t2", 1, handle_t2, TierStats())
        async with sm() as s:
            t = (await s.execute(select(Task).where(Task.type == "t2"))).scalars().one()
            return client.sent, t.status

    return asyncio.run(go())


def test_an_unstamped_task_with_an_online_model_override_is_held(recorder, monkeypatch):  # noqa: F811
    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")
    sent, status = _drain(stamp=False)
    assert sent == [], f"an unconfirmed online pick was sent: {sent}"
    assert status != "done"


def test_the_same_task_with_a_matching_stamp_is_sent(recorder, monkeypatch):  # noqa: F811
    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")
    sent, status = _drain(stamp=True)
    assert sent == [("vendor/invented-pick", True)] and status == "done"


def test_a_local_override_and_a_plain_task_are_not_held_by_this_rule():
    from kestrel.config import get_settings

    settings = get_settings()
    local = {"backend": "local", "model": "kestrel-tier3", "json_mode": "schema"}
    assert not held_for_approval(settings, ("tier2",), {"model_override": local})
    assert not held_for_approval(settings, ("tier2",), {"opportunity_id": 1})


def test_a_runs_online_pick_carries_the_runs_confirmation_to_tier2():
    """The producer that used to leave the task unstamped: a scan with an online one-run model
    (run_model marker -> Tier 0 -> Tier 1 -> Tier 2)."""
    async def go():
        ids = await role_seed.seed_async()
        sm = get_sessionmaker()
        async with sm() as s:
            await _hosted_on(s)
            await s.commit()
            settings = await load_settings_with_db(s)
            dest = destination_id(settings_with_override(settings, "tier2", ONLINE), "tier2")
            approval = {"tier2": [dest]}
            o = Opportunity(track="employment", opp_type="job_posting", title="Invented lead",
                            stage="new", description_md="Invented duties. " * 30)
            s.add(o)
            await s.flush()
            await run_model.attach(s, o.id, ONLINE, approval)
            await s.commit()
            oid = o.id
        from kestrel.pipeline.tier0 import run_tier0_batch

        await run_tier0_batch(sm, settings=settings)
        async with sm() as s:
            t1 = [t for t in (await s.execute(select(Task).where(Task.type == "t1")))
                  .scalars() if t.payload.get("opportunity_id") == oid]
        assert ids
        return t1, approval

    t1, approval = asyncio.run(go())
    assert len(t1) == 1
    payload = t1[0].payload
    assert payload[run_model.T2_ONLY_KEY] == ONLINE
    assert payload["online_approval"] == approval
