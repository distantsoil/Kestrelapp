"""Issue #218 (D-PRIV-679): under confidential mode nothing leaves with the current employer
unredacted, whatever the mix of a task's held settings and the live client.

The exact mix: a task stamped by a run's approval (no corrections digest), whose held settings
keep the scoring tier on this computer while the client that actually sends goes online. Before
the fix, `plan_hosted_send` read only the held settings, found the tier local, and planned no
redaction, so the client sent the employer's name. The plan is now built from the sender too.

All text is invented. Nothing reaches the network: the recording model answers locally.
"""

from __future__ import annotations

import asyncio

import pytest

from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Opportunity, Setting
from tests.test_contracts import (  # noqa: F401  (recorder is a fixture)
    EMPLOYER,
    _confidential,
    _drain_scoring,
    _lines_world,
    _strong_tasks,
    recorder,
)

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

POSTING = f"Lead the night shift. You would report to the team at {EMPLOYER} on day one."


def _db(fn):
    async def run():
        async with get_sessionmaker()() as s:
            out = await fn(s)
            await s.commit()
            return out

    return asyncio.run(run())


def _run_task(task_type: str, opp_id: int, stage: str) -> object:
    """Settings as they are now (online), the posting naming the employer, and one task stamped
    by a run's approval, as Tier 0's run pick and the scheduler stamp it: no digest."""
    from kestrel.pipeline.tasks import enqueue_once
    from kestrel.services.online_consent import approval_for
    from kestrel.services.setting_overrides import load_settings_with_db

    async def go(s):
        settings = await load_settings_with_db(s)
        opp = await s.get(Opportunity, opp_id)
        opp.description_md = f"{opp.description_md or ''}\n\n{POSTING}"
        opp.stage = stage
        approval = approval_for(settings, ("tier1", "tier2"))
        assert approval, "the world must have an online scoring route"
        await enqueue_once(s, task_type, opp_id, extra={"online_approval": approval,
                                                         "rescore": True, "requested": True})
        return settings

    return _db(go)


def _hold_local(tiers: tuple[str, ...]) -> None:
    async def go(s):
        for tier in tiers:
            await s.merge(Setting(key=f"llm.tiers.{tier}.backend", value="ollama"))
            await s.merge(Setting(key=f"llm.tiers.{tier}.model", value="local-model:latest"))

    _db(go)


@pytest.mark.parametrize(("task_type", "stage", "local"), [
    ("t2", "t1_pass", ("tier2",)),
    ("t1", "t0_pass", ("tier1", "tier2")),
])
def test_held_local_settings_with_an_online_client_never_send_the_employer(
    task_type, stage, local, recorder, monkeypatch  # noqa: F811
):
    ids = _lines_world(monkeypatch)
    _confidential(monkeypatch)
    online = _run_task(task_type, ids["strong"], stage)
    _hold_local(local)  # the run's held settings keep the tier on this computer
    sent = _drain_scoring(client_settings=online)  # the live client still sends online
    tasks = [(t.type, t.status, t.last_error) for t in _strong_tasks(ids["strong"])]
    assert sent, f"nothing was sent, so this test proves nothing: {tasks}"
    assert not any(EMPLOYER in s for s in sent), "the employer left by name"


def test_the_plan_counts_the_sender_whatever_the_held_settings_say(monkeypatch):
    """The unit under the end-to-end case: held settings local, sender online, confidential on
    with an employer chosen: the plan is external and hides; with the sender local too, it plans
    nothing."""
    from kestrel.pipeline._shared import plan_hosted_send
    from kestrel.services.setting_overrides import load_settings_with_db

    _lines_world(monkeypatch)
    _confidential(monkeypatch)

    async def go(s):
        online = await load_settings_with_db(s)
        local = online.model_copy(deep=True)
        local.llm.tiers["tier2"].backend = "ollama"
        mixed = await plan_hosted_send(s, local, "tier2", sender=online)
        quiet = await plan_hosted_send(s, local, "tier2", sender=local)
        return mixed, quiet

    mixed, quiet = _db(go)
    assert mixed.external and mixed.mapping and EMPLOYER in mixed.mapping.names()
    assert not quiet.external and not quiet.mapping


@pytest.mark.parametrize(("task_type", "stage", "local", "module"), [
    ("t2", "t1_pass", ("tier2",), "kestrel.pipeline.tier2"),
    ("t1", "t0_pass", ("tier1", "tier2"), "kestrel.pipeline.tier1"),
])
def test_an_online_client_gets_the_slice_the_disclosure_states(
    task_type, stage, local, module, recorder, monkeypatch  # noqa: F811
):
    """The description slice is the disclosure's other half: in the same mix, the handler sizes
    it as an online send (the plan's reading), never as the held local settings would."""
    import importlib

    mod = importlib.import_module(module)
    real = mod.jd_budget
    seen: list[object] = []

    def recording(settings, tier, default, *, external=None):
        seen.append(external)
        return real(settings, tier, default, external=external)

    monkeypatch.setattr(mod, "jd_budget", recording)
    ids = _lines_world(monkeypatch)
    _confidential(monkeypatch)
    online = _run_task(task_type, ids["strong"], stage)
    _hold_local(local)
    sent = _drain_scoring(client_settings=online)
    assert sent and seen, (sent, seen)
    assert seen[0] is True, f"sized as {seen}"


def test_the_employer_is_hidden_when_either_side_asks(monkeypatch):
    """The held settings have redact_confidential off, the sender has it on: the plan hides."""
    from kestrel.pipeline._shared import plan_hosted_send
    from kestrel.services.setting_overrides import load_settings_with_db

    _lines_world(monkeypatch)
    _confidential(monkeypatch)

    async def go(s):
        sender = await load_settings_with_db(s)
        held = sender.model_copy(deep=True)
        held.llm.hosted_mode.redact_confidential = False
        return await plan_hosted_send(s, held, "tier2", sender=sender)

    plan = _db(go)
    assert plan.external and plan.mapping and EMPLOYER in plan.mapping.names()
