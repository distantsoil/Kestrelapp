"""Scoring progress (D-UI-49) and Score again (D-UI-50): the progress dict, re-scoring with the
same model or another one for one task only, quarantine re-entry, Tier 0 refusal, the history
list and the dialog markup. The model is faked; nothing reaches a live endpoint."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from kestrel import paths
from kestrel.analytics.progress import ETA_MIN_FINISHED, scoring_progress
from kestrel.config import get_settings
from kestrel.db.base import iso
from kestrel.db.models import Evaluation, Opportunity, Quarantine, Run, Task
from kestrel.pipeline._shared import settings_with_override
from kestrel.pipeline.tier1 import handle_t1
from kestrel.pipeline.tier2 import handle_t2
from kestrel.pipeline.worker import TierStats, Worker
from kestrel.queue.rescore import enqueue_rescore, parse_ids
from kestrel.web.pages.rescore import comparison, history_rows, short_model
from tests import clock_seed
from tests.pipeline_fakes import FakeLLM

_PAD = " " + "Details follow. " * 20  # a real posting body, over the Tier 2 stub floor


def _recent() -> str:
    return (datetime.now(UTC) - timedelta(days=3)).date().isoformat()


class _RecordingLLM(FakeLLM):
    """Records the tier model each call was made with (the copy's settings, for an override)."""

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.models: list[tuple[str, str]] = []

    async def complete(self, *, tier, messages, **kw):
        self.models.append((tier, self.settings.llm.tiers[tier].model))
        return await super().complete(tier=tier, messages=messages, **kw)


def _ago(**kw) -> str:
    return iso(clock_seed.ago(**kw))


async def _opp(sm, *, stage: str, title: str = "VP of IT", first_seen: str | None = None) -> int:
    async with sm() as s:
        o = Opportunity(
            track="employment", opp_type="job_posting", title=title, comp_max=250000,
            comp_source="parsed", remote_type="hybrid", state="TX", city="Austin",
            location_raw="Austin, TX", date_posted=_recent(), seen_count=1, ghost_score=0.1,
            stage=stage, description_md="Own IT leadership, budget and vendor accountability."
                    + _PAD,
        )
        if first_seen:
            o.first_seen_at = first_seen
        s.add(o)
        await s.commit()
        return o.id


async def _eval(sm, opp_id: int, *, model: str, score: float, created: str,
                verdict: str = "apply") -> None:
    async with sm() as s:
        s.add(Evaluation(opportunity_id=opp_id, tier=2, track="employment", model=model,
                         verdict=verdict, score=score, weighted_score=score, created_at=created))
        await s.commit()


async def _tasks(sm) -> list[Task]:
    async with sm() as s:
        return list((await s.execute(select(Task).order_by(Task.id))).scalars())


async def _evals(sm, opp_id: int) -> list[Evaluation]:
    async with sm() as s:
        return list((await s.execute(
            select(Evaluation).where(Evaluation.opportunity_id == opp_id).order_by(Evaluation.id)
        )).scalars())


async def _drain(worker: Worker, task_type: str) -> TierStats:
    stats = TierStats()
    handler = {"t1": handle_t1, "t2": handle_t2}[task_type]
    await worker.drain_tier(task_type, 1, handler, stats)
    return stats


# ------------------------------------------------------------------------------- progress
async def test_progress_empty(sm):
    async with sm() as s:
        p = await scoring_progress(s, get_settings())
    assert p["active"] is False and p["total"] == 0 and p["eta_minutes"] is None
    assert p["last_run"] is None
    assert p["model"].endswith("(local)")
    assert set(p) == {"active", "total", "scored", "failed", "set_aside", "running", "waiting",
                      "model", "eta_minutes", "last_run", "open_failed"}
    assert p["open_failed"] == 0


async def _finished_task(sm, oid: int, *, task_type: str = "t2", status: str = "done",
                         updated: str | None = None) -> None:
    async with sm() as s:
        s.add(Task(type=task_type, payload={"opportunity_id": oid}, status=status,
                   updated_at=updated or _ago(minutes=1)))
        await s.commit()


async def test_progress_mid_run_with_an_estimate(sm):
    async with sm() as s:
        s.add(Run(name="desktop", status="running", started_at=_ago(hours=1), counts={}))
        await s.commit()
    done = [await _opp(sm, stage="t2_done") for _ in range(6)]
    parked = await _opp(sm, stage="quarantined")
    aside = await _opp(sm, stage="t1_reject")
    waiting = [await _opp(sm, stage="t1_pass") for _ in range(3)]
    running = await _opp(sm, stage="t1_pass")
    old = await _opp(sm, stage="t2_done")  # scored before this batch began: not in it
    for i, oid in enumerate(done):  # one Tier 2 completion a minute
        await _eval(sm, oid, model="m", score=4.0, created=_ago(minutes=10 - i))
        await _finished_task(sm, oid)
    await _finished_task(sm, parked, status="dead")
    await _finished_task(sm, aside, task_type="t1")
    await _finished_task(sm, old, updated=_ago(days=3))
    async with sm() as s:
        for oid in waiting:
            s.add(Task(type="t2", payload={"opportunity_id": oid}))
        s.add(Task(type="t2", payload={"opportunity_id": running}, status="leased"))
        s.add(Task(type="t2", payload={}))  # no opportunity id: ignored, never a NULL in NOT IN
        await s.commit()
        p = await scoring_progress(s, get_settings())
    assert p["active"] is True
    assert (p["scored"], p["failed"], p["running"], p["waiting"]) == (6, 1, 1, 3)
    # Tier 1's reject stays in the batch as set aside, so the denominator never shrinks.
    assert p["set_aside"] == 1 and p["total"] == 12
    assert p["eta_minutes"] == 4  # 4 left at a minute each


async def test_progress_counts_a_posting_with_no_body_in_the_total(sm):
    from kestrel.web.fit import EMPTY_POSTING_STAGE_REASON

    async with sm() as s:
        s.add(Run(name="desktop", status="running", started_at=_ago(hours=1), counts={}))
        await s.commit()
    stub = await _opp(sm, stage="t1_pass")
    done = await _opp(sm, stage="t2_done")
    async with sm() as s:
        (await s.get(Opportunity, stub)).stage_reason = EMPTY_POSTING_STAGE_REASON
        await s.commit()
    await _eval(sm, done, model="m", score=4.0, created=_ago(minutes=5))
    await _finished_task(sm, done)
    await _finished_task(sm, stub, status="done")
    async with sm() as s:
        p = await scoring_progress(s, get_settings())
    assert p["scored"] == 1 and p["set_aside"] == 1 and p["total"] == 2


async def test_progress_batch_without_an_open_run_is_only_what_is_being_scored(sm):
    """Regression: with no open Run, re-scoring 3 roles reads "of 3", never "140 of 143" from a
    24-hour window of everything seen."""
    for _ in range(140):
        await _opp(sm, stage="t2_done", first_seen=_ago(hours=2))
    roles = [await _opp(sm, stage="t2_done") for _ in range(3)]
    async with sm() as s:
        await enqueue_rescore(s, roles)
        await s.commit()
        p = await scoring_progress(s, get_settings())
    assert (p["total"], p["waiting"], p["scored"]) == (3, 3, 0)
    # One finishes: still of 3, with that one scored.
    async with sm() as s:
        t = (await s.execute(select(Task).order_by(Task.id).limit(1))).scalar_one()
        t.status = "done"
        await s.commit()
        p = await scoring_progress(s, get_settings())
    assert (p["total"], p["waiting"], p["scored"]) == (3, 2, 1)


async def test_progress_withholds_the_estimate_under_five_finished(sm):
    async with sm() as s:
        s.add(Run(name="desktop", status="running", started_at=_ago(hours=1), counts={}))
        await s.commit()
    for i in range(ETA_MIN_FINISHED - 1):
        oid = await _opp(sm, stage="t2_done")
        await _eval(sm, oid, model="m", score=4.0, created=_ago(minutes=5 - i))
        await _finished_task(sm, oid)
    oid = await _opp(sm, stage="t1_pass")
    async with sm() as s:
        s.add(Task(type="t2", payload={"opportunity_id": oid}))
        await s.commit()
        p = await scoring_progress(s, get_settings())
    assert p["active"] and p["scored"] == ETA_MIN_FINISHED - 1 and p["eta_minutes"] is None


async def test_progress_finished_reports_the_last_run(sm):
    async with sm() as s:
        s.add(Run(name="desktop", status="done", started_at=_ago(hours=3),
                  finished_at=_ago(hours=2), counts={"tier2": {"completed": 41, "errors": 3}}))
        # A later scan-only run did no scoring: the panel still reports the run that scored.
        s.add(Run(name="desktop", status="done", started_at=_ago(hours=1),
                  finished_at=_ago(minutes=50), counts={"tier2": {"completed": 0, "errors": 0}}))
        await s.commit()
    await _opp(sm, stage="t2_done", first_seen=_ago(hours=2))
    async with sm() as s:
        p = await scoring_progress(s, get_settings())
    # Nothing is being scored and no run is open: there is no batch.
    assert p["active"] is False and p["eta_minutes"] is None
    assert p["scored"] == 0 and p["total"] == 0
    assert p["last_run"]["scored"] == 41 and p["last_run"]["failed"] == 3
    assert p["last_run"]["status"] == "done"


async def test_progress_model_labels():
    from kestrel.analytics.progress import model_label

    s = get_settings()
    external = next((n for n, b in s.llm.backends.items() if b.external), None)
    assert model_label(s, "local", "kestrel-tier2") == "kestrel-tier2 (local)"
    if external:
        assert model_label(s, external, "x/y:free") == "x/y:free (free, online)"
        assert model_label(s, external, "x/y") == "x/y (online)"


# ------------------------------------------------------------------------------- rescore
async def test_rescore_same_model_writes_a_new_evaluation(sm):
    settings = get_settings()
    oid = await _opp(sm, stage="t2_done")
    await _eval(sm, oid, model="old-model", score=3.0, created=_ago(days=1), verdict="consider")
    async with sm() as s:
        res = await enqueue_rescore(s, [oid])
        await s.commit()
    assert res.queued == [oid]
    (task,) = await _tasks(sm)
    assert task.type == "t2" and task.payload["rescore"] and "model_override" not in task.payload

    llm = _RecordingLLM(settings)
    worker = Worker(sm, llm, settings)
    await _drain(worker, "t2")
    evs = await _evals(sm, oid)
    assert len(evs) == 2 and evs[0].model == "old-model"  # the old one is kept
    assert evs[1].model == settings.llm.tiers["tier2"].model
    async with sm() as s:
        assert (await s.get(Opportunity, oid)).stage == "t2_done"

    # Resumable: the same task re-leased after a crash does not write a third evaluation.
    async with sm() as s:
        t = await s.get(Task, task.id)
        t.status = "pending"
        await s.commit()
    await _drain(worker, "t2")
    assert len(await _evals(sm, oid)) == 2


async def test_rescore_with_another_model_reaches_that_task_only(sm):
    settings = get_settings()
    configured = settings.llm.tiers["tier2"].model
    a = await _opp(sm, stage="t2_done", title="VP of IT A")
    b = await _opp(sm, stage="t1_pass", title="VP of IT B")  # an ordinary waiting role
    override = {"backend": "local", "model": "kestrel-tier3", "json_mode": "schema"}
    async with sm() as s:
        await enqueue_rescore(s, [a], model_override=override)
        s.add(Task(type="t2", payload={"opportunity_id": b}))
        await s.commit()
    llm = _RecordingLLM(settings)
    worker = Worker(sm, llm, settings)
    await _drain(worker, "t2")
    assert ("tier2", "kestrel-tier3") in llm.models
    assert ("tier2", configured) in llm.models  # the other task ran on the configured model
    assert (await _evals(sm, a))[-1].model == "kestrel-tier3"
    assert (await _evals(sm, b))[-1].model == configured
    # Nothing changed: the worker's settings, the shared client's, and the loaded settings.
    assert worker._settings.llm.tiers["tier2"].model == configured
    assert llm.settings.llm.tiers["tier2"].model == configured
    assert get_settings().llm.tiers["tier2"].model == configured


def test_override_refuses_a_raw_tag_and_a_hosted_model_while_hosted_mode_is_off():
    import pytest

    from kestrel.pipeline._shared import ModelOverrideError

    s = get_settings()
    with pytest.raises(ModelOverrideError):
        settings_with_override(s, "tier2", {"backend": "local", "model": "llama3:8b"})
    external = next((n for n, b in s.llm.backends.items() if b.external), None)
    if external:
        with pytest.raises(ModelOverrideError):
            settings_with_override(s, "tier2", {"backend": external, "model": "x/y:free"})
    assert settings_with_override(s, "tier2", None) is s
    # A local pick's own context window must hold the tier's prompt (SPEC 8.2): kestrel-tier1
    # (4096) and kestrel-docsel (8192) cannot run Tier 2, an unknown kestrel-* name neither.
    for small in ("kestrel-tier1", "kestrel-docsel", "kestrel-mystery"):
        with pytest.raises(ModelOverrideError):
            settings_with_override(s, "tier2", {"backend": "local", "model": small})
    for ok in ("kestrel-tier2", "kestrel-tier3:latest"):
        assert settings_with_override(s, "tier2", {"backend": "local", "model": ok}) is not s


def test_one_off_picker_offers_only_models_that_meet_the_tier2_budget(monkeypatch):
    """Score again, Run now and Scan now share rescore._local_models and _override_for: a local
    model whose num_ctx is below the Tier 2 budget is neither offered nor accepted."""
    from kestrel.web.pages import rescore as rs

    monkeypatch.setattr(
        "kestrel.web.pages.documents._local_assess_models",
        lambda settings: (["kestrel-docsel:latest", "kestrel-tier1:latest",
                           "kestrel-tier2:latest", "kestrel-tier3:latest"], None),
    )
    s = get_settings()
    offered, note = rs._local_models(s)
    assert offered == ["kestrel-tier2:latest", "kestrel-tier3:latest"]
    assert "kestrel-docsel and kestrel-tier1" in note and "16,384" in note
    override, _label, why = asyncio.run(rs._override_for(s, "kestrel-tier1:latest"))
    assert override is None and "cannot run the full review" in why and "4,096" in why
    override, label, why = asyncio.run(rs._override_for(s, "kestrel-tier3:latest"))
    assert why is None and override["model"] == "kestrel-tier3:latest"
    override, _label, why = asyncio.run(rs._override_for(s, "llama3:8b"))
    assert override is None and "llama3:8b" in why


async def test_quarantined_role_leaves_quarantine_and_keeps_its_history(sm):
    settings = get_settings()
    oid = await _opp(sm, stage="quarantined")
    async with sm() as s:
        s.add(Quarantine(opportunity_id=oid, tier=2, model="m", error="bad json", attempts=2))
        await s.commit()
        await enqueue_rescore(s, [oid])
        await s.commit()
        # Enqueue never moves the stage; the handler does when the new score lands.
        assert (await s.get(Opportunity, oid)).stage == "quarantined"
    await _drain(Worker(sm, FakeLLM(settings), settings), "t2")
    async with sm() as s:
        assert (await s.get(Opportunity, oid)).stage == "t2_done"
        q = (await s.execute(select(Quarantine).where(Quarantine.opportunity_id == oid))).all()
    assert len(q) == 1


async def test_a_scored_role_keeps_its_stage_when_the_rescore_quarantines(sm):
    """Regression: a failed score-again of a scored role keeps the stage, its quarantine row is
    marked as a score-again, Failed scoring lists it apart, Try again re-queues the SAME payload
    (model override kept) without touching the stage, and progress counts it as scored."""
    from kestrel.pipeline._shared import parse_rescore_failure
    from kestrel.web.pages.runs_detail import _quarantine_rows, _retry_quarantine

    settings = get_settings()
    oid = await _opp(sm, stage="t3_done", title="VP of IT [QUAR2]")
    await _eval(sm, oid, model="m", score=4.0, created=_ago(days=1))
    override = {"backend": "local", "model": "kestrel-tier3", "json_mode": "schema"}
    async with sm() as s:
        await enqueue_rescore(s, [oid], model_override=override)
        await s.commit()
    (first,) = await _tasks(sm)
    await _drain(Worker(sm, FakeLLM(settings), settings), "t2")
    async with sm() as s:
        assert (await s.get(Opportunity, oid)).stage == "t3_done"
        q = (await s.execute(select(Quarantine))).scalars().one()
        p = await scoring_progress(s, get_settings())
        rows = await _quarantine_rows(s)
    is_rescore, task_id, _err = parse_rescore_failure(q.error)
    assert is_rescore and task_id == first.id
    assert rows[0]["rescore"] is True
    assert p["failed"] == 0  # its earlier score stands: not a failed role

    async with sm() as s:
        assert await _retry_quarantine(s, q.id) is None
    async with sm() as s:
        assert (await s.get(Opportunity, oid)).stage == "t3_done"  # never reset to t1_pass
    again = [t for t in await _tasks(sm) if t.id != first.id]
    assert len(again) == 1 and again[0].type == "t2" and again[0].status == "pending"
    assert again[0].payload["rescore"] is True
    assert again[0].payload["model_override"] == override
    assert again[0].payload["requested_at"] >= first.payload["requested_at"]
    # A second Try again while one waits queues nothing more.
    async with sm() as s:
        msg = await _retry_quarantine(s, q.id)
    assert msg and "already waiting" in msg
    assert len(await _tasks(sm)) == 2


async def test_a_dead_rescore_task_never_strands_the_role(sm):
    """Regression: score again leaves the stage alone at enqueue, so a task that dies leaves a
    quarantined role quarantined and a Tier 1 reject a Tier 1 reject, never "waiting"."""
    parked = await _opp(sm, stage="quarantined")
    aside = await _opp(sm, stage="t1_reject")
    async with sm() as s:
        res = await enqueue_rescore(s, [parked, aside])
        await s.commit()
    assert sorted(res.queued) == sorted([parked, aside])
    async with sm() as s:
        for t in (await s.execute(select(Task))).scalars():
            t.status = "dead"
        await s.commit()
        assert (await s.get(Opportunity, parked)).stage == "quarantined"
        assert (await s.get(Opportunity, aside)).stage == "t1_reject"


async def test_tier1_rescore_of_a_reject_runs_once_and_skips_when_re_leased(sm):
    settings = get_settings()
    oid = await _opp(sm, stage="t1_reject")
    async with sm() as s:
        await enqueue_rescore(s, [oid])
        await s.commit()
    (task,) = await _tasks(sm)
    llm = _RecordingLLM(settings)
    worker = Worker(sm, llm, settings)
    await _drain(worker, "t1")
    assert llm.models == [("tier1", settings.llm.tiers["tier1"].model)]
    async with sm() as s:
        assert (await s.get(Opportunity, oid)).stage == "t1_pass"
        # Re-leased after a crash, and the stage back at t1_reject: no second Tier 1 call.
        (await s.get(Opportunity, oid)).stage = "t1_reject"
        (await s.get(Task, task.id)).status = "pending"
        await s.commit()
    await _drain(worker, "t1")
    assert len(llm.models) == 1


async def test_tier0_reject_is_refused_unless_asked_and_tier1_reject_reruns_on_the_override(sm):
    """D-UI-74 changed the Tier 0 half: without Score this role anyway a rule's reject is still
    refused (and explained); with it, it is queued (see test_score_anyway.py end to end)."""
    settings = get_settings()
    t0 = await _opp(sm, stage="t0_reject")
    t1 = await _opp(sm, stage="t1_reject")
    new = await _opp(sm, stage="new")
    override = {"backend": "local", "model": "kestrel-tier3", "json_mode": "schema"}
    async with sm() as s:
        res = await enqueue_rescore(s, [t0, t1, new, 99999], model_override=override)
        await s.commit()
    assert res.rules == [t0] and res.queued == [t1] and res.unscreened == [new]
    assert res.missing == [99999]
    assert any("set aside by your rules" in m for m in res.messages())
    (task,) = await _tasks(sm)
    assert task.type == "t1" and task.payload["model_override"]["model"] == "kestrel-tier3"

    llm = _RecordingLLM(settings)
    worker = Worker(sm, llm, settings)
    await _drain(worker, "t1")
    t2_task = [t for t in await _tasks(sm) if t.type == "t2"][0]
    assert t2_task.payload["model_override"]["model"] == "kestrel-tier3"
    await _drain(worker, "t2")
    assert llm.models == [("tier1", "kestrel-tier3"), ("tier2", "kestrel-tier3")]
    async with sm() as s:
        assert (await s.get(Opportunity, t1)).stage == "t2_done"


def test_parse_ids_caps_and_dedupes():
    assert parse_ids("3, 1,3,x,-2,") == [3, 1]
    assert parse_ids(["1,2", "2", "5"]) == [1, 2, 5]
    assert len(parse_ids(",".join(str(i) for i in range(1, 400)))) == 200


# ------------------------------------------------------------------------------- history
async def test_history_newest_first_with_a_comparison(sm):
    oid = await _opp(sm, stage="t2_done")
    await _eval(sm, oid, model="nvidia/nemotron-3.5-super-vl:free", score=3.8,
                created=_ago(days=2), verdict="consider")
    await _eval(sm, oid, model="google/gemma-4-31b:free", score=4.4, created=_ago(days=1))
    async with sm() as s:
        s.add(Evaluation(opportunity_id=oid, tier=1, track="employment", model="t1",
                         verdict="keep", created_at=_ago(hours=1)))
        await s.commit()
        rows = await history_rows(s, oid)
    assert [r["model"] for r in rows] == ["google/gemma-4-31b:free",
                                          "nvidia/nemotron-3.5-super-vl:free"]
    assert rows[0]["fit"] == "good fit" and rows[1]["fit"] == "maybe"
    assert comparison(rows) == "Gemma scored 0.6 higher than Nemotron."
    assert comparison(rows[:1]) is None
    assert short_model("kestrel-tier2:latest") == "kestrel-tier2"


# ------------------------------------------------------------------------------- web
def _client(monkeypatch):
    from starlette.testclient import TestClient

    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
    from kestrel.web.app import build_app

    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations(paths.db_path())
    forget_engine()
    monkeypatch.setattr("kestrel.web.pages.rescore._local_models",
                        lambda settings: (["kestrel-tier3:latest"], None))

    async def seed() -> list[int]:
        sm = get_sessionmaker()
        ids = [await _opp(sm, stage="t2_done", title=f"VP {i}") for i in range(2)]
        await _opp(sm, stage="t0_reject")
        async with sm() as s:  # a measured pace: 30 s a role
            s.add(Run(name="desktop", status="done", finished_at=_ago(hours=1),
                      counts={"tier2": {"completed": 20, "errors": 0, "per_minute": 2.0}}))
            await s.commit()
        return ids

    ids = asyncio.run(seed())
    forget_engine()
    return TestClient(build_app(worker=False)), ids


def test_dialog_markup_and_queue(monkeypatch):
    client, ids = _client(monkeypatch)
    body = client.get(f"/rescore/choose?opp_ids={ids[0]},{ids[1]}").text
    assert '<dialog class="score-again-dialog sf-dialog"' in body and "showModal" in body
    assert "Score 2 roles again" in body
    # D-UI-152: a friendly model name and where it runs, never the raw kestrel-tier2 tag.
    assert re.search(r"<b>Same model</b><span>[^<]+, on this computer\.", body)
    assert "kestrel-tier2" not in body and "kestrel-tier3" not in body.split("<script>")[0].replace(
        'value="kestrel-tier3:latest"', "")
    assert "A different model, this time only" in body
    assert 'value="kestrel-tier3:latest"' in body and 'data-testid="score-again-pick"' in body
    assert "Free models are often much slower and less reliable than paid ones." in body
    assert "2 roles may take about 1 minute." in body
    assert "Cancel" in body and "Score again</button>" in body and "—" not in body

    r = client.post("/rescore", data={"opp_ids": f"{ids[0]},{ids[1]}", "model_choice": "same"})
    assert "Queued 2 roles for scoring." in r.text
    assert r.headers.get("HX-Trigger") == "pipelineRefresh"
    r = client.post("/rescore", data={"opp_ids": str(ids[0]), "model_choice": "other",
                                      "model": "llama3:8b"})
    assert "Not queued" in r.text and "HX-Trigger" not in r.headers
    r = client.post("/rescore", data={"opp_ids": str(ids[0] + 2), "model_choice": "same"})
    # D-UI-73: the refusal names the rule (none stored on this seed: the safe fallback) and
    # links to where rules are seen; Score this role anyway is offered (D-UI-74).
    assert "Set aside by one of your rules" in r.text and "Nothing queued." in r.text
    assert '<a href="/settings">' in r.text and "Score this role anyway" in r.text


def test_button_and_history_includes_render():
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(paths.web_templates_dir()))
    btn = env.get_template("partials/score_again.html").render(opp_ids=[3, 4])
    assert 'hx-get="/rescore/choose"' in btn and '"opp_ids": "3,4"' in btn
    assert "Score again…" in btn and "disabled" not in btn
    assert "disabled" in env.get_template("partials/score_again.html").render(opp_ids=[])
    hist = env.get_template("partials/score_history.html").render(opp_id=7)
    assert 'hx-get="/rescore/history/7"' in hist


def test_every_class_has_a_css_rule():
    tdir = Path(paths.web_templates_dir()) / "partials"
    css = (Path(paths.web_static_dir()) / "css" / "kestrel.css").read_text("utf-8")
    block = css.split("/* --- stream 1: score again --- */")[1]
    classes: set[str] = set()
    for name in ("score_again.html", "score_again_dialog.html", "score_history.html",
                 "score_history_list.html"):
        tpl = (tdir / name).read_text("utf-8")
        for attr in re.findall(r'class="([^"]+)"', tpl):
            classes |= {c for c in attr.split() if c.startswith("score-")}
    classes = {c for c in classes if "{" not in c}
    classes |= {f"score-history-{t}" for t in ("good", "warn", "bad", "idle")}
    assert classes and all(f".{c}" in block for c in classes), classes
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", block)  # theme variables only


# ------------------------------------------------------------- hosted mode turned off mid-run
def _hosted_settings():
    s = get_settings().model_copy(deep=True)
    hm = s.llm.hosted_mode
    hm.enabled = True
    hm.acknowledged_at = _ago(days=1)
    hm.acknowledged_terms = hm.disclosure_terms()
    return s


async def test_worker_rechecks_hosted_mode_before_an_overridden_task(sm):
    """Regression: the worker's settings refresh only between cycles. Hosted mode turned off
    mid-run (a setting row) must stop a hosted override AND a configured hosted tier from being
    sent: the override falls back to the configured model, and a configured hosted tier gets
    the live (off) Hosted mode so the client refuses it."""
    from kestrel.db.models import Setting

    settings = _hosted_settings()
    external = next((n for n, b in settings.llm.backends.items() if b.external), None)
    if external is None:
        return
    worker = Worker(sm, FakeLLM(settings), settings)
    payload = {"opportunity_id": 1, "rescore": True,
               "model_override": {"backend": external, "model": "x/y:free",
                                  "json_mode": "schema"}}
    on, _client = await worker._task_settings("t2", payload)
    assert on.llm.tiers["tier2"].model == "x/y:free"  # still on: the override runs

    async with sm() as s:
        s.add(Setting(key="llm.hosted_mode.enabled", value=False))
        await s.commit()
    off, client = await worker._task_settings("t2", payload)
    assert off is worker._settings and client is worker._client
    assert off.llm.tiers["tier2"].model == settings.llm.tiers["tier2"].model

    # A configured hosted tier: the client gets the live Hosted mode, which refuses the send.
    worker._settings.llm.tiers["tier2"] = worker._settings.llm.tiers["tier2"].model_copy(
        update={"backend": external, "model": "x/z"}
    )
    live, live_client = await worker._task_settings("t2", {"opportunity_id": 1})
    assert live is not worker._settings and live.llm.hosted_mode.active is False
    assert live_client.settings is live
    assert worker._settings.llm.hosted_mode.active is True  # the worker's copy is untouched


async def test_worker_local_tasks_pay_no_hosted_read(sm, monkeypatch):
    settings = get_settings()
    worker = Worker(sm, FakeLLM(settings), settings)

    async def boom():
        raise AssertionError("a local task must not read the setting rows")

    monkeypatch.setattr(worker, "_hosted_now", boom)
    got, _ = await worker._task_settings("t2", {"opportunity_id": 1})
    assert got is worker._settings


def test_hosted_only_never_probes_ollama_or_says_it_is_unreachable(monkeypatch):
    """Operator report: in Hosted mode, Score again said "Ollama is not reachable". With every
    tier hosted, Ollama is not part of the setup: it is never probed and no note appears."""
    from types import SimpleNamespace

    import kestrel.doctor as doctor
    import kestrel.llm.client as client
    from kestrel.web.pages.documents import _local_assess_models

    probed = []

    class Boom:
        def __init__(self, *a, **k):
            pass

        def list_models(self, timeout=None):
            probed.append(True)
            raise ConnectionError("refused")

    monkeypatch.setattr(client, "OllamaClient", Boom)
    local = SimpleNamespace(external=False, base_url="http://127.0.0.1:11434/v1", api_key=None)
    settings = SimpleNamespace(
        llm=SimpleNamespace(backends={"local": local}, ollama_native_url="http://127.0.0.1:11434")
    )
    monkeypatch.setattr(doctor, "hosted_only", lambda s: True)
    assert _local_assess_models(settings) == ([], None) and not probed
    # A setup that does use Ollama still says so when it is down.
    monkeypatch.setattr(doctor, "hosted_only", lambda s: False)
    models, note = _local_assess_models(settings)
    assert models == [] and probed and "not reachable" in note


def test_bench_does_not_report_ollama_down_when_every_tier_is_hosted(monkeypatch):
    import kestrel.doctor as doctor
    from kestrel.web.pages import bench

    seen = []
    monkeypatch.setattr(doctor, "hosted_only", lambda s: seen.append(s) or True)
    app_settings = object()  # the app's settings (DB layer), never get_settings() (YAML only)
    assert bench._available_models(app_settings) == ([], None)
    assert seen == [app_settings]
