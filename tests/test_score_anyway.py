"""Score this role anyway (D-UI-74, amended) end to end, and the rule's why on the role and in
the Score again dialog (D-UI-73). The override row is written only when Tier 1 actually runs.
The model is faked; nothing reaches a live endpoint."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from kestrel import paths
from kestrel.config import get_settings
from kestrel.db.models import Opportunity, Setting, Task
from kestrel.pipeline.tier0 import run_tier0_batch
from kestrel.pipeline.worker import Worker
from kestrel.queue.rescore import enqueue_rescore
from kestrel.services import tier0_override
from tests.test_pipeline_tier0 import _opt_in
from tests.test_score_again import _drain, _recent, _RecordingLLM, _tasks

_REASON = "blocker:seniority_floor: Below seniority floor"


async def _director(sm, **kw) -> int:
    async with sm() as s:
        o = Opportunity(
            track="employment", opp_type="job_posting", title="Director of IT Services",
            comp_max=250000, comp_source="parsed", remote_type="remote", country="US",
            location_raw="Remote (US)", date_posted=_recent(), seen_count=1, ghost_score=0.1,
            stage="t0_reject", stage_reason=_REASON,
            description_md="Own IT services, budget and vendor accountability.",
        )
        for k, v in kw.items():
            setattr(o, k, v)
        s.add(o)
        await s.commit()
        return o.id


async def test_score_anyway_runs_tier1_then_tier2_on_the_pick_and_survives_tier0(sm, monkeypatch):
    _opt_in(monkeypatch, seniority_floor="vp")  # the operator's floor is above Director
    settings = get_settings()
    oid = await _director(sm)
    other = await _director(sm)  # set aside too, but not asked for: still refused
    override = {"backend": "local", "model": "kestrel-tier3", "json_mode": "schema"}

    async with sm() as s:
        refused = await enqueue_rescore(s, [other], model_override=override)
        res = await enqueue_rescore(s, [oid], model_override=override, tier0_anyway=True)
        await s.commit()
    assert refused.rules == [other] and refused.queued == []
    assert res.queued == [oid] and res.anyway == [oid] and res.rules == []
    assert any("scored despite your rules" in m for m in res.messages())
    (task,) = await _tasks(sm)
    assert task.type == "t1" and task.payload["tier0_override"] is True
    # Operator-requested: the track switch never holds it (finding 10).
    assert task.payload["requested"] is True and task.payload["rescore"] is True
    assert task.payload["tier0_reason"] == _REASON  # the intent rides on the task
    async with sm() as s:
        # Not recorded until Tier 1 runs it (D-UI-74 amended).
        assert await tier0_override.get(s, oid) is None

    llm = _RecordingLLM(settings)
    worker = Worker(sm, llm, settings)
    await _drain(worker, "t1")
    async with sm() as s:
        rec = await tier0_override.get(s, oid)
        assert rec["rule"] == "seniority_floor" and rec["reason"] == _REASON
        assert await tier0_override.get(s, other) is None
        # Not a config row: the settings layer never reads it.
        from kestrel.services.setting_overrides import is_config_key

        assert not is_config_key(tier0_override.key_for(oid))
    await _drain(worker, "t2")
    assert llm.models == [("tier1", "kestrel-tier3"), ("tier2", "kestrel-tier3")]
    async with sm() as s:
        assert (await s.get(Opportunity, oid)).stage == "t2_done"
        assert (await s.get(Opportunity, other)).stage == "t0_reject"

    # A later Tier 0 pass (an MCP re-evaluation resets the role to new) does not reject it again.
    async with sm() as s:
        for i in (oid, other):
            (await s.get(Opportunity, i)).stage = "new"
        await s.commit()
    counts = await run_tier0_batch(sm, settings=settings)
    assert counts["seen"] == 2 and counts["pass"] == 1 and counts["reject"] == 1
    async with sm() as s:
        kept = await s.get(Opportunity, oid)
        assert kept.stage == "t0_pass" and "tier0_override" in kept.stage_reason
        assert (await s.get(Opportunity, other)).stage == "t0_reject"
        # The operator's rules never changed.
        rows = (await s.execute(select(Setting.key))).scalars().all()
        assert not any(k.startswith("employment_terms") for k in rows)


async def test_score_anyway_is_idempotent_on_a_re_lease(sm):
    settings = get_settings()
    oid = await _director(sm)
    async with sm() as s:
        await enqueue_rescore(s, [oid], tier0_anyway=True)
        again = await enqueue_rescore(s, [oid], tier0_anyway=True)
        await s.commit()
    assert again.already == [oid]
    llm = _RecordingLLM(settings)
    worker = Worker(sm, llm, settings)
    await _drain(worker, "t1")
    n = len(llm.models)
    async with sm() as s:  # re-leased after a crash, the stage back where it was
        (await s.get(Opportunity, oid)).stage = "t0_reject"
        task = (await s.execute(select(Task).where(Task.type == "t1"))).scalars().first()
        task.status = "pending"
        await s.commit()
    await _drain(worker, "t1")
    assert len(llm.models) == n  # the Tier 1 evaluation since requested_at: no second call


async def test_a_plain_t1_task_never_runs_from_t0_reject(sm):
    """Only an operator's Score this role anyway runs from t0_reject; an ordinary task skips."""
    from kestrel.pipeline.tasks import enqueue_once

    settings = get_settings()
    oid = await _director(sm)
    async with sm() as s:
        await enqueue_once(s, "t1", oid, extra={"tier0_override": True})  # no rescore flag
        await s.commit()
    llm = _RecordingLLM(settings)
    await _drain(Worker(sm, llm, settings), "t1")
    assert llm.models == []
    async with sm() as s:
        assert (await s.get(Opportunity, oid)).stage == "t0_reject"
        assert await tier0_override.get(s, oid) is None


async def test_a_task_that_dies_records_no_override(sm):
    """Every attempt errors (the model is down) until the task is dead: no override row, the
    role stays set aside, and nothing says "You asked to score this"."""
    settings = get_settings()
    oid = await _director(sm)
    async with sm() as s:
        await enqueue_rescore(s, [oid], tier0_anyway=True)
        await s.commit()

    class _Down(_RecordingLLM):
        async def complete(self, *, tier, messages, **kw):
            raise ConnectionError("model is down")

    worker = Worker(sm, _Down(settings), settings)
    for _ in range(5):  # max_attempts is 3: retried until dead
        async with sm() as s:
            for t in (await s.execute(select(Task).where(Task.status == "pending"))).scalars():
                t.lease_until = None
            await s.commit()
        await _drain(worker, "t1")
    (task,) = await _tasks(sm)
    assert task.status == "dead"
    async with sm() as s:
        assert await tier0_override.get(s, oid) is None
        assert (await s.get(Opportunity, oid)).stage == "t0_reject"


async def test_an_existing_ordinary_t1_task_becomes_the_score_anyway(sm):
    """A t0_reject role already holding a pending ordinary t1 task (it would only skip): the
    operator's ask turns that task into the override instead of saying "already waiting"."""
    from kestrel.pipeline.tasks import enqueue_once

    settings = get_settings()
    oid = await _director(sm)
    async with sm() as s:
        await enqueue_once(s, "t1", oid, extra={"t2_model_override": {"backend": "local",
                                                                      "model": "kestrel-tier3"}})
        await s.commit()
    async with sm() as s:
        res = await enqueue_rescore(s, [oid], tier0_anyway=True)
        await s.commit()
    assert res.queued == [oid] and res.anyway == [oid] and res.already == []
    (task,) = await _tasks(sm)
    p = task.payload
    assert p["tier0_override"] and p["rescore"] and p["requested"] and p["requested_at"]
    assert "t2_model_override" not in p and "model_override" not in p  # usual models asked for
    async with sm() as s:
        assert await tier0_override.get(s, oid) is None
        again = await enqueue_rescore(s, [oid], tier0_anyway=True)
    assert again.already == [oid]  # now it really is waiting
    llm = _RecordingLLM(settings)
    await _drain(Worker(sm, llm, settings), "t1")
    assert [t for t, _m in llm.models] == ["tier1"]
    async with sm() as s:
        assert (await tier0_override.get(s, oid))["rule"] == "seniority_floor"
        assert (await s.get(Opportunity, oid)).stage in ("t1_pass", "t1_reject")


async def test_the_startup_sweep_requeues_an_overridden_t0_reject_role_with_the_override(sm):
    from kestrel.pipeline.orphans import NEXT_STEP, requeue_orphans

    assert NEXT_STEP["t0_reject"] == ("t1", 1)
    oid = await _director(sm)
    plain = await _director(sm)  # set aside, never asked for: not an orphan
    async with sm() as s:
        await tier0_override.record(s, oid, _REASON)
        await s.commit()
    assert (await requeue_orphans(sm))["requeued"] == 1
    (task,) = await _tasks(sm)
    assert task.payload["opportunity_id"] == oid and task.payload["tier0_override"] is True
    assert task.payload["rescore"] is True and task.payload["requested"] is True
    assert plain != oid


# ------------------------------------------------------------------------------- web
def _client(monkeypatch):
    from starlette.testclient import TestClient

    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
    from kestrel.web.app import build_app

    _opt_in(monkeypatch, seniority_floor="vp")
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations(paths.db_path())
    forget_engine()
    monkeypatch.setattr("kestrel.web.pages.rescore._local_models",
                        lambda settings: (["kestrel-tier3:latest"], None))

    async def seed() -> int:
        return await _director(get_sessionmaker())

    oid = asyncio.run(seed())
    forget_engine()
    return TestClient(build_app(worker=False)), oid


_SENTENCE = ("Set aside by your seniority rule: the title reads as Director, and your lowest level "
             "is Vice President.")


def test_the_role_says_why_and_score_anyway_from_the_dialog(monkeypatch):
    client, oid = _client(monkeypatch)
    drawer = client.get(f"/opportunities/{oid}/panel").text
    assert 'data-testid="rule-why"' in drawer and _SENTENCE in drawer
    link = 'href="/profile?tab=employment&amp;track=employment">Profile &gt; Employment</a>'
    assert link in drawer
    tab = client.get(f"/opportunities/{oid}/tab/evaluation").text
    assert _SENTENCE in tab and 'data-testid="eval-rule-link"' in tab

    dialog = client.get(f"/rescore/choose?opp_ids={oid}").text
    assert 'data-testid="score-again-anyway"' in dialog and "Score this role anyway" in dialog
    assert _SENTENCE in dialog and "Your rules do not change" in dialog
    assert 'name="tier0_anyway" value="1" data-testid="score-again-anyway">' in dialog  # unchecked
    assert "—" not in dialog

    # Without the box: refused, with the rule, where to change it, and the way through.
    r = client.post("/rescore", data={"opp_ids": str(oid), "model_choice": "same"})
    assert "Nothing queued." in r.text and _SENTENCE in r.text
    assert "Change it in Profile &gt; Employment." in r.text
    assert '<a href="/profile?tab=employment&amp;track=employment">' in r.text
    assert "Sources" not in r.text  # the old, wrong destination

    r = client.post("/rescore", data={"opp_ids": str(oid), "model_choice": "other",
                                      "model": "kestrel-tier3:latest", "tier0_anyway": "1"})
    assert "Queued 1 role for scoring." in r.text and "scored despite your rules" in r.text
    # Queued, not yet run: the note waits for the override to be recorded (D-UI-74 amended).
    note = "You asked to score this despite your rule on seniority."
    assert note not in client.get(f"/opportunities/{oid}/panel").text

    async def run_it():
        from kestrel.db.engine import forget_engine, get_sessionmaker

        sm = get_sessionmaker()
        await _drain(Worker(sm, _RecordingLLM(get_settings()), get_settings()), "t1")
        forget_engine()

    asyncio.run(run_it())
    drawer = client.get(f"/opportunities/{oid}/panel").text
    assert note in drawer
    tab = client.get(f"/opportunities/{oid}/tab/evaluation").text
    assert note in tab


def test_no_script_refusal_has_its_own_outcome(monkeypatch):
    client, oid = _client(monkeypatch)
    r = client.post("/rescore", data={"opp_ids": str(oid), "model_choice": "same",
                                      "next": "/pipeline"}, follow_redirects=False)
    assert r.status_code == 303 and "outcome=rules" in r.headers["location"]
    from kestrel.web.pages.rescore import outcome_for

    out = outcome_for({"outcome": "rules", "n": "0"})
    assert out["ok"] is False and "Score this role anyway" in out["text"]


def test_settings_roles_is_where_the_posting_age_rule_changes(monkeypatch):
    client, _oid = _client(monkeypatch)
    page = client.get("/settings/section/roles").text
    assert 'data-testid="st-num-age"' in page and 'value="45"' in page
    client.post("/settings/roles/number", data={"track": "employment", "key": "age",
                                                "value": "60"})
    page = client.get("/settings/section/roles").text
    assert 'data-testid="st-num-age"' in page and 'value="60"' in page
    bad = client.post("/settings/roles/number", data={"track": "employment", "key": "age",
                                                      "value": "0"})
    assert "Use a whole number from 1 to 365. Kestrel is still using 60." in bad.text
    assert 'value="60"' in client.get("/settings/section/roles").text  # 0 refused
