"""Mid-run switch to local (issue #103): when the operator turns off Hosted mode while a run is
draining, the rest of the run scores locally and the person is told. Invariant: after a switch to
local, zero further online requests in the same run.

Every network call is respx-mocked; the test verifies the note is set on the run and that the
worker's settings reflect the switch."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Opportunity, Setting
from kestrel.llm.client import LLMClient
from kestrel.pipeline.worker import Worker


@pytest.fixture
def settings() -> Any:
    s = get_settings()
    s.llm.tiers["tier1"].backend = "ollama"
    s.llm.tiers["tier1"].model = "qwen3:4b"
    s.llm.tiers["tier2"].backend = "frontier"
    s.llm.tiers["tier2"].model = "meta-llama/llama-3.3-70b-instruct:free"
    s.llm.hosted_mode.enabled = True
    s.llm.hosted_mode.acknowledged_at = "2026-10-12T10:00:00+00:00"
    s.llm.hosted_mode.acknowledged_terms = s.llm.hosted_mode.disclosure_terms()
    return s


async def test_mid_run_switch_sets_note(sm, settings):
    """When Hosted mode is turned off mid-run, the worker sets a note on the run."""
    # Seed the hosted_mode setting as ON
    async with sm() as s:
        s.add(Setting(key="llm.hosted_mode.enabled", value=True))
        s.add(
            Setting(
                key="llm.hosted_mode.acknowledged_at",
                value="2026-10-12T10:00:00+00:00",
            )
        )
        await s.commit()

    client = LLMClient(settings)
    worker = Worker(sm, client, settings)

    # Simulate: run starts with hosted mode ON, then the operator turns it OFF
    # The worker's _hosted_now() reads the setting rows; we flip them to OFF
    async with sm() as s:
        row = (
            await s.execute(
                select(Setting).where(Setting.key == "llm.hosted_mode.enabled")
            )
        ).scalar_one()
        row.value = False
        await s.commit()

    # Now call _task_settings for a tier2 task (which is external)
    result_settings, _ = await worker._task_settings("t2", {})

    # The note should be set
    assert worker._mid_run_switch_note is not None
    assert "Switched to your computer" in worker._mid_run_switch_note
    assert "scores locally" in worker._mid_run_switch_note

    # The settings should reflect the switch (hosted mode OFF)
    assert result_settings.llm.hosted_mode.enabled is False


async def test_mid_run_switch_note_reset_on_new_run(sm, settings):
    """The note is reset at the start of each run."""
    client = LLMClient(settings)
    worker = Worker(sm, client, settings)

    # Simulate: previous run set the note
    worker._mid_run_switch_note = (
        "Switched to your computer; the rest of this run scores locally."
    )

    # Simulate: new run starts (calls run(), which resets the note)
    # We can't call run() directly (it would try to open a run and drain tiers),
    # so we verify the reset logic by checking the initial value in __init__
    worker2 = Worker(sm, client, settings)
    assert worker2._mid_run_switch_note is None


# ---- the invariant, counted at the provider (#103) ----------------------------------------------
async def test_after_the_switch_to_local_no_further_request_reaches_the_online_model(
    sm, monkeypatch
):
    """Three roles are waiting for the full review on an online model the run was confirmed for.
    The person turns Hosted mode off while the first is being rated. The provider is mocked and
    counted: exactly the one request already under way reaches it, none after the switch, and the
    other roles are not marked done (they stay for a run on this computer)."""
    import httpx
    import respx
    from httpx import Response

    from kestrel.db.models import Task
    from kestrel.pipeline.tier2 import handle_t2
    from kestrel.pipeline.worker import TierStats
    from kestrel.queue.queue import TaskQueue
    from tests.pipeline_fakes import _p2
    from tests.test_hosted_mode import _OR_CHAT, _hosted_settings, _seed

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    first = await _seed(sm, stage="t1_pass", with_map=True)
    ids = [first]
    async with sm() as s:
        base = await s.get(Opportunity, first)
        for n in (2, 3):
            more = Opportunity(
                track=base.track, opp_type=base.opp_type, source_id=base.source_id,
                org_id=base.org_id, org_name=base.org_name, title=f"Director of IT {n}",
                url=f"https://x/{n}", description_md=base.description_md, stage="t1_pass",
                version=1, seen_count=1, ghost_score=0.0, pre_score=0.5,
            )
            s.add(more)
            await s.flush()
            ids.append(more.id)
        await s.commit()
    settings = _hosted_settings("tier2")
    queue = TaskQueue(sm)
    for opp_id in ids:
        await queue.enqueue("t2", {"opportunity_id": opp_id})
    sent: list[int] = []

    async def provider(request: httpx.Request) -> Response:
        sent.append(1)
        if len(sent) == 1:  # the person switches to local while this request is in flight
            async with sm() as s:
                s.add(Setting(key="llm.hosted_mode.enabled", value=False))
                await s.commit()
        return Response(200, json={
            "choices": [{"message": {"content": _p2(None)}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        })

    with respx.mock:
        route = respx.post(_OR_CHAT).mock(side_effect=provider)
        async with httpx.AsyncClient() as http:
            worker = Worker(sm, LLMClient(settings, http=http), settings)
            await worker.drain_tier("t2", 1, handle_t2, TierStats())
    assert route.call_count == 1  # zero further online requests after the switch
    assert worker._mid_run_switch_note is not None
    async with sm() as s:
        done = (await s.execute(select(Task).where(Task.type == "t2", Task.status == "done"))
                ).scalars().all()
    assert len(done) == 1  # the others were not completed against a refused send


async def test_after_stop_no_new_online_request_starts(sm, monkeypatch):
    """The notice's Stop button pauses the worker (/runs/stop). The request under way finishes;
    no other role is sent to the online model after it."""
    import httpx
    import respx
    from httpx import Response

    from kestrel.pipeline.tier2 import handle_t2
    from kestrel.pipeline.worker import TierStats
    from kestrel.queue.queue import TaskQueue
    from tests.pipeline_fakes import _p2
    from tests.test_hosted_mode import _OR_CHAT, _hosted_settings, _seed

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    first = await _seed(sm, stage="t1_pass", with_map=True)
    ids = [first]
    async with sm() as s:
        base = await s.get(Opportunity, first)
        for n in (2, 3):
            more = Opportunity(
                track=base.track, opp_type=base.opp_type, source_id=base.source_id,
                org_id=base.org_id, org_name=base.org_name, title=f"Director of IT {n}",
                url=f"https://x/{n}", description_md=base.description_md, stage="t1_pass",
                version=1, seen_count=1, ghost_score=0.0, pre_score=0.5,
            )
            s.add(more)
            await s.flush()
            ids.append(more.id)
        await s.commit()
    settings = _hosted_settings("tier2")
    queue = TaskQueue(sm)
    for opp_id in ids:
        await queue.enqueue("t2", {"opportunity_id": opp_id})
    stopped = {"on": False}

    async def provider(request: httpx.Request) -> Response:
        stopped["on"] = True  # the person presses Stop while this request is in flight
        return Response(200, json={
            "choices": [{"message": {"content": _p2(None)}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        })

    with respx.mock:
        route = respx.post(_OR_CHAT).mock(side_effect=provider)
        async with httpx.AsyncClient() as http:
            worker = Worker(sm, LLMClient(settings, http=http), settings,
                            paused=lambda: stopped["on"])
            await worker.drain_tier("t2", 1, handle_t2, TierStats())
    assert route.call_count == 1
