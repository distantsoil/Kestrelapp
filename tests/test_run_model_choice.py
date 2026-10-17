"""A model for one run (D-UI-72, amended): Sources > Run now and Today's Scan now offer "My usual
models" or "A different model, this time only". The pick reaches the full review (Tier 2) of the
roles that run is processing, never Tier 1 and never a later pass; the configured models never
change; a restart keeps it on queued tasks. Only local models whose context meets the Tier 2
budget are offered. The model is faked and nothing reaches a live endpoint."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import respx
from httpx import Response
from sqlalchemy import select

from kestrel import paths
from kestrel.analytics.progress import _model_in_use
from kestrel.config import get_settings
from kestrel.db.models import Opportunity, ScrapeRun, Setting, Source, Task
from kestrel.ingest.http import HttpClient
from kestrel.ingest.runner import run_source
from kestrel.pipeline.orphans import requeue_orphans
from kestrel.pipeline.tier0 import run_tier0_batch
from kestrel.pipeline.tier1 import handle_t1
from kestrel.pipeline.tier2 import handle_t2
from kestrel.pipeline.worker import TierStats, Worker
from kestrel.services import run_model
from kestrel.services.run_model import T2_ONLY_KEY
from tests.pipeline_fakes import FakeLLM


def _recent() -> str:
    return (datetime.now(UTC) - timedelta(days=3)).date().isoformat()
_GH = json.loads((Path(__file__).parent / "fixtures" / "greenhouse.json").read_text())
OVERRIDE = {"backend": "local", "model": "kestrel-tier3", "json_mode": "schema"}


class _RecordingLLM(FakeLLM):
    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.models: list[tuple[str, str]] = []

    async def complete(self, *, tier, messages, **kw):
        self.models.append((tier, self.settings.llm.tiers[tier].model))
        return await super().complete(tier=tier, messages=messages, **kw)


async def _source(sm, name="s") -> int:
    async with sm() as s:
        src = Source(track="employment", kind="greenhouse", name=name, enabled=True,
                     config={"token": "meridian"})
        s.add(src)
        await s.commit()
        return src.id


async def _scrape_run(sm, sid: int, override: dict | None) -> int:
    counts: dict = {"new": 1}
    run_model.stamp(counts, override, "kestrel-tier3 (local)")
    async with sm() as s:
        run = ScrapeRun(source_id=sid, status="ok", counts=counts)
        s.add(run)
        await s.commit()
        return run.id


async def _new_opp(sm, sid: int, run_id: int, title: str, override: dict | None = None) -> int:
    """A role a run just stored; `override` is what the runner attaches for its pick."""
    async with sm() as s:
        o = Opportunity(
            track="employment", opp_type="job_posting", title=title, comp_max=250000,
            comp_source="parsed", remote_type="hybrid", state="OH", city="Columbus",
            location_raw="Columbus, OH", date_posted=_recent(), seen_count=1, ghost_score=0.1,
            stage="new", source_id=sid, scrape_run_id=run_id,
            description_md="Own IT leadership, budget and vendor accountability.",
        )
        s.add(o)
        await s.flush()
        await run_model.attach(s, o.id, override)
        await s.commit()
        return o.id


async def _tasks(sm, task_type: str) -> list[Task]:
    async with sm() as s:
        return list((await s.execute(
            select(Task).where(Task.type == task_type).order_by(Task.id))).scalars())


async def test_run_source_records_the_choice_on_its_scrape_run_only(sm):
    with respx.mock:
        respx.get("https://boards-api.greenhouse.io/v1/boards/meridian/jobs").mock(
            return_value=Response(200, json=_GH))
        sid = await _source(sm)
        async with HttpClient(user_agent="test", respect_robots=False) as http:
            chosen = await run_source(sm, sid, http=http, model_override=OVERRIDE,
                                      model_label="kestrel-tier3 (local)")
            usual = await run_source(sm, sid, http=http)
    async with sm() as s:
        a = await s.get(ScrapeRun, chosen.scrape_run_id)
        b = await s.get(ScrapeRun, usual.scrape_run_id)
        assert run_model.override_of(a.counts)["model"] == "kestrel-tier3"
        assert run_model.label_of(a.counts) == "kestrel-tier3 (local) (this run only)"
        assert run_model.override_of(b.counts) is None and run_model.label_of(b.counts) is None
        # The stamp survives the runner's final counts write, beside the real counts.
        assert a.counts["new"] == 2
        # The run attached its pick to the roles it stored, for Tier 0 to consume.
        for o in (await s.execute(select(Opportunity))).scalars():
            assert (await s.get(Setting, f"{run_model.PENDING_PREFIX}{o.id}")).value == OVERRIDE


async def test_override_reaches_only_tier2_of_that_runs_roles(sm):
    settings = get_settings()
    configured = (settings.llm.tiers["tier1"].model, settings.llm.tiers["tier2"].model)
    sid = await _source(sm)
    picked = await _scrape_run(sm, sid, OVERRIDE)
    usual = await _scrape_run(sm, sid, None)
    a = await _new_opp(sm, sid, picked, "VP of IT", OVERRIDE)
    b = await _new_opp(sm, sid, usual, "Director of IT")

    await run_tier0_batch(sm, settings=settings)
    t1 = {t.payload["opportunity_id"]: t.payload for t in await _tasks(sm, "t1")}
    assert t1[a][T2_ONLY_KEY]["model"] == "kestrel-tier3"
    assert "model_override" not in t1[a]  # Tier 1 itself never takes a one-run pick
    assert T2_ONLY_KEY not in t1[b] and "model_override" not in t1[b]
    async with sm() as s:  # the marker was consumed in Tier 0's commit
        assert await s.get(Setting, f"{run_model.PENDING_PREFIX}{a}") is None

    llm = _RecordingLLM(settings)
    worker = Worker(sm, llm, settings)
    for kind, handler in (("t1", handle_t1), ("t2", handle_t2)):
        await worker.drain_tier(kind, 1, handler, TierStats())
    t2 = {t.payload["opportunity_id"]: t.payload for t in await _tasks(sm, "t2")}
    assert t2[a]["model_override"]["model"] == "kestrel-tier3"
    assert "model_override" not in t2[b]
    assert not t2[a].get("rescore")  # an ordinary first score, not a score-again
    assert sorted(llm.models) == sorted([
        ("tier1", configured[0]), ("tier2", "kestrel-tier3"),
        ("tier1", configured[0]), ("tier2", configured[1]),
    ])
    # Nothing configured changed.
    assert worker._settings.llm.tiers["tier1"].model == configured[0]
    assert get_settings().llm.tiers["tier2"].model == configured[1]


async def test_a_restart_keeps_the_pick_on_queued_tasks_and_later_passes_use_usual(sm):
    settings = get_settings()
    sid = await _source(sm)
    picked = await _scrape_run(sm, sid, OVERRIDE)
    a = await _new_opp(sm, sid, picked, "VP of IT", OVERRIDE)
    await run_tier0_batch(sm, settings=settings)
    # A restart: a new Worker over the same database finds the queued task with its payload.
    (task,) = await _tasks(sm, "t1")
    assert task.payload[T2_ONLY_KEY]["model"] == "kestrel-tier3"
    worker = Worker(sm, _RecordingLLM(settings), settings)
    effective, _client = await worker._task_settings("t1", task.payload)
    assert effective.llm.tiers["tier1"].model == settings.llm.tiers["tier1"].model
    # The task row lost (stranded stage): the startup sweep re-queues it on the usual models.
    async with sm() as s:
        await s.delete(await s.get(Task, task.id))
        await s.commit()
    assert (await requeue_orphans(sm))["requeued"] == 1
    (again,) = await _tasks(sm, "t1")
    assert again.payload["opportunity_id"] == a
    assert T2_ONLY_KEY not in again.payload and "model_override" not in again.payload


async def test_a_role_first_stored_by_a_pick_run_and_re_evaluated_gets_usual_models(sm):
    """The pick is attached by the run processing the role, never looked up from
    opp.scrape_run_id: an MCP re-evaluation (reset to new) runs on the configured models."""
    settings = get_settings()
    sid = await _source(sm)
    picked = await _scrape_run(sm, sid, OVERRIDE)
    a = await _new_opp(sm, sid, picked, "VP of IT", OVERRIDE)
    await run_tier0_batch(sm, settings=settings)
    async with sm() as s:  # the first pass's task is done; the MCP tool resets the role
        for t in (await s.execute(select(Task))).scalars():
            t.status = "done"
        (await s.get(Opportunity, a)).stage = "new"
        await s.commit()
    await run_tier0_batch(sm, settings=settings)
    live = [t for t in await _tasks(sm, "t1") if t.status == "pending"]
    assert len(live) == 1 and live[0].payload["opportunity_id"] == a
    assert T2_ONLY_KEY not in live[0].payload and "model_override" not in live[0].payload
    async with sm() as s:  # the role still names the run that first stored it
        assert (await s.get(Opportunity, a)).scrape_run_id == picked


async def test_mcp_re_evaluation_drops_an_unconsumed_pick(sm):
    from kestrel.services import run_model as rm

    sid = await _source(sm)
    picked = await _scrape_run(sm, sid, OVERRIDE)
    a = await _new_opp(sm, sid, picked, "VP of IT", OVERRIDE)
    async with sm() as s:
        await rm.discard(s, a)
        await s.commit()
    await run_tier0_batch(sm, settings=get_settings())
    (task,) = await _tasks(sm, "t1")
    assert T2_ONLY_KEY not in task.payload


async def test_run_panel_names_a_one_run_model(sm):
    settings = get_settings()
    async with sm() as s:
        s.add(Task(type="t1", status="leased", payload={"opportunity_id": 1,
                                                       T2_ONLY_KEY: OVERRIDE}))
        await s.commit()
    async with sm() as s:
        label = await _model_in_use(s, settings, True)
        assert label == "kestrel-tier3 (local) (this run only)"
        assert "(this run only)" not in (await _model_in_use(s, settings, False) or "")
    # A pick that would fall back to the configured model (no Kestrel ctx) is never named.
    async with sm() as s:
        for t in (await s.execute(select(Task))).scalars():
            t.status = "done"
        s.add(Task(type="t2", status="leased", payload={
            "opportunity_id": 2, "model_override": {"backend": "local", "model": "kestrel-x"}}))
        await s.commit()
    async with sm() as s:
        assert "(this run only)" not in (await _model_in_use(s, settings, True) or "")
        # A leased t2 on a real pick wins over a t1 task's: the full review names the model.
        s.add(Task(type="t2", status="leased", payload={
            "opportunity_id": 3, "model_override": {"backend": "local", "model": "kestrel-tier2"}}))
        s.add(Task(type="t1", status="leased", payload={"opportunity_id": 4,
                                                        T2_ONLY_KEY: OVERRIDE}))
        await s.commit()
    async with sm() as s:
        assert await _model_in_use(s, settings, True) == "kestrel-tier2 (local) (this run only)"


# --------------------------------------------------------------------------- the route
def _client(monkeypatch, *, local=("kestrel-tier3:latest",)):
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
                        lambda settings: (list(local), None))
    ran: list[tuple] = []

    async def fake_run(source_id, override=None, label=None):
        ran.append((source_id, override, label))
        return "ok: {}"

    monkeypatch.setattr("kestrel.web.pages.sources._trigger_run", fake_run)

    async def seed() -> int:
        async with get_sessionmaker()() as s:
            src = Source(track="employment", kind="greenhouse", name="Board", enabled=True,
                         config={"token": "x"})
            s.add(src)
            await s.commit()
            return src.id

    sid = asyncio.run(seed())
    forget_engine()
    return TestClient(build_app(worker=False)), sid, ran


def test_dialog_markup(monkeypatch):
    client, sid, _ran = _client(monkeypatch)
    body = client.get(f"/sources/{sid}/run-now/choose").text
    assert 'data-testid="run-now-dialog"' in body and "showModal" in body
    assert "My usual models" in body and "A different model, this time only" in body
    assert 'value="kestrel-tier3:latest"' in body and 'data-testid="run-now-pick"' in body
    assert 'name="model_choice" value="usual" checked' in body
    assert "—" not in body
    panel = client.get("/sources").text
    assert f'hx-get="/sources/{sid}/run-now/choose"' in panel
    assert 'type="hidden" name="page"' in panel
    page = client.get(f"/sources/{sid}/run-now/choose?page=1").text
    assert "<dialog" in page and "showModal" not in page
    assert f'action="/sources/{sid}/run-now"' in page
    assert client.get("/sources/9999/run-now/choose").status_code == 404


def test_usual_models_and_a_valid_pick(monkeypatch):
    client, sid, ran = _client(monkeypatch)
    r = client.post(f"/sources/{sid}/run-now", data={"model_choice": "usual"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200 and ran[-1] == (sid, None, None)
    r = client.post(f"/sources/{sid}/run-now",
                    data={"model_choice": "other", "model": "kestrel-tier3:latest"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    _sid, override, label = ran[-1]
    assert override["model"] == "kestrel-tier3:latest" and override["backend"] == "local"
    assert label == "kestrel-tier3:latest (local)"


def test_refusals_run_nothing_and_say_why(monkeypatch):
    client, sid, ran = _client(monkeypatch)
    hx = {"HX-Request": "true"}
    r = client.post(f"/sources/{sid}/run-now", data={"model_choice": "other", "model": ""},
                    headers=hx)
    assert "Not run: pick a model, or choose My usual models." in r.text
    r = client.post(f"/sources/{sid}/run-now", data={"model_choice": "other", "model": "llama3:8b"},
                    headers=hx)
    assert "Not run:" in r.text and "llama3:8b" in r.text  # a raw Ollama tag
    r = client.post(f"/sources/{sid}/run-now",
                    data={"model_choice": "other", "model": "hosted:x/y:free"}, headers=hx)
    assert "Not run:" in r.text  # hosted while Hosted mode is off
    assert ran == []


def test_no_script_post_redirects_with_a_fixed_outcome(monkeypatch):
    client, sid, ran = _client(monkeypatch)
    r = client.post(f"/sources/{sid}/run-now", data={"model_choice": "other", "model": ""},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == (
        f"/sources?run_outcome=no_pick&run_id={sid}")
    page = client.get(r.headers["location"]).text
    assert "Not run: pick a model, or choose My usual models." in page and ran == []
    r = client.post(f"/sources/{sid}/run-now", data={"model_choice": "usual"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith(f"run_outcome=ran&run_id={sid}")
    assert "Ran just now." in client.get(r.headers["location"]).text and len(ran) == 1


def test_scans_list_and_run_panel_say_this_run_only(monkeypatch):
    from kestrel.db.engine import forget_engine, get_sessionmaker

    client, sid, _ran = _client(monkeypatch)

    async def seed():
        counts: dict = {"new": 2}
        run_model.stamp(counts, OVERRIDE, "kestrel-tier3 (local)")
        async with get_sessionmaker()() as s:
            s.add(ScrapeRun(source_id=sid, status="ok", counts=counts))
            await s.commit()

    asyncio.run(seed())
    forget_engine()
    runs = client.get("/runs").text
    assert "on kestrel-tier3 (local) (this run only)" in runs
    assert "&#39;model_override&#39;" not in runs and "model_override_label" not in runs
    assert "on kestrel-tier3 (local) (this run only)" in client.get("/sources").text


# ------------------------------------------------------------- Today's Scan now (D-UI-72 amended)
async def test_scan_now_carries_the_choice_on_each_scrape_task_except_signal_searches(sm):
    from kestrel.ingest.scheduler import enqueue_all_scrapes
    from kestrel.queue.queue import TaskQueue

    async with sm() as s:
        s.add_all([
            Source(track="employment", kind="greenhouse", name="a", enabled=True),
            Source(track="employment", kind="sec_edgar", name="sec", enabled=True),
        ])
        await s.commit()
    async with sm() as s:
        assert await enqueue_all_scrapes(s, TaskQueue(sm), model_override=OVERRIDE,
                                         model_label="kestrel-tier3 (local)") == 2
    tasks = {t.payload["source_id"]: t.payload for t in await _tasks(sm, "scrape")}
    kinds = {}
    async with sm() as s:
        for src in (await s.execute(select(Source))).scalars():
            kinds[src.kind] = src.id
    assert tasks[kinds["greenhouse"]]["model_override"]["model"] == "kestrel-tier3"
    assert tasks[kinds["greenhouse"]]["model_label"] == "kestrel-tier3 (local)"
    assert "model_override" not in tasks[kinds["sec_edgar"]]
    # A plain Scan now (and the tray's) queues no override.
    async with sm() as s:
        for t in (await s.execute(select(Task))).scalars():
            t.status = "done"
        await s.commit()
    async with sm() as s:
        await enqueue_all_scrapes(s, TaskQueue(sm))
    pending = [t for t in await _tasks(sm, "scrape") if t.status == "pending"]
    assert pending and all("model_override" not in t.payload for t in pending)


@respx.mock
async def test_worker_runs_a_queued_scrape_with_its_override_after_a_restart(sm):
    respx.get("https://boards-api.greenhouse.io/v1/boards/meridian/jobs").mock(
        return_value=Response(200, json=_GH))
    sid = await _source(sm)
    async with sm() as s:
        s.add(Task(type="scrape", payload={"source_id": sid, "model_override": OVERRIDE,
                                           "model_label": "kestrel-tier3 (local)"}))
        await s.commit()
    worker = Worker(sm, FakeLLM(get_settings()), get_settings())  # a fresh worker: a restart
    await worker.drain_scrapes()
    async with sm() as s:
        run = (await s.execute(select(ScrapeRun))).scalars().one()
        assert run_model.label_of(run.counts) == "kestrel-tier3 (local) (this run only)"
        opps = (await s.execute(select(Opportunity))).scalars().all()
        assert opps and all(o.scrape_run_id == run.id for o in opps)


def test_scan_dialog_start_validation_and_no_script(monkeypatch):
    client, sid, _ran = _client(monkeypatch)
    body = client.get("/scan/choose").text
    assert 'data-testid="run-now-dialog"' in body and 'hx-post="/scan/start"' in body
    assert "My usual models" in body and "Company-filing and similar searches" in body
    assert "—" not in body
    assert 'hx-get="/scan/choose"' in client.get("/").text
    page = client.get("/scan/choose?page=1").text
    assert 'action="/scan/start"' in page and 'name="next" value="/"' in page

    hx = {"HX-Request": "true"}
    r = client.post("/scan/start", data={"model_choice": "other", "model": ""}, headers=hx)
    assert "Not started: pick a model, or choose My usual models." in r.text
    r = client.post("/scan/start", data={"model_choice": "other", "model": "llama3:8b"}, headers=hx)
    assert "Not started:" in r.text
    r = client.post("/scan/start", data={"model_choice": "other", "model": "hosted:x/y:free"},
                    headers=hx)
    assert "Not started:" in r.text
    assert asyncio.run(_count_scrapes()) == 0

    r = client.post("/scan/start", data={"model_choice": "other", "model": "", "next": "/"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?scan_outcome=no_pick"
    assert "Not started: pick a model" in client.get("/?scan_outcome=no_pick").text

    r = client.post("/scan/start",
                    data={"model_choice": "other", "model": "kestrel-tier3:latest"}, headers=hx)
    assert 'data-state="scanning"' in r.text
    payloads = asyncio.run(_scrape_payloads())
    assert payloads and payloads[0]["model_override"]["model"] == "kestrel-tier3:latest"


async def _count_scrapes() -> int:
    from kestrel.db.engine import forget_engine, get_sessionmaker

    async with get_sessionmaker()() as s:
        n = len((await s.execute(select(Task).where(Task.type == "scrape"))).scalars().all())
    forget_engine()
    return n


async def _scrape_payloads() -> list[dict]:
    from kestrel.db.engine import forget_engine, get_sessionmaker

    async with get_sessionmaker()() as s:
        rows = [t.payload for t in (await s.execute(
            select(Task).where(Task.type == "scrape"))).scalars()]
    forget_engine()
    return rows


# ------------------------------------------------------------------ model swap thrash (CLAUDE.md)
async def test_tier2_drains_one_model_at_a_time_never_alternating(sm):
    """A mixed Tier 2 queue (usual, pick, usual, an override naming the usual model, pick,
    usual) is leased grouped by the model it runs on: the configured model's group first, then
    the pick's, each drained to empty before the next, so the model never swaps back and forth."""
    settings = get_settings()
    configured = settings.llm.tiers["tier2"].model
    usual_named = {"backend": settings.llm.tiers["tier2"].backend, "model": configured,
                   "json_mode": "schema"}
    sid = await _source(sm)
    run = await _scrape_run(sm, sid, None)
    plan = [None, OVERRIDE, None, usual_named, OVERRIDE, None]
    oids = [await _new_opp(sm, sid, run, f"VP of IT {i}") for i in range(len(plan))]
    async with sm() as s:
        for i, (oid, override) in enumerate(zip(oids, plan, strict=True)):
            (await s.get(Opportunity, oid)).stage = "t1_pass"
            payload = {"opportunity_id": oid}
            if override:
                payload["model_override"] = override
            s.add(Task(type="t2", payload=payload, priority=10 * (i % 2)))
        await s.commit()
    llm = _RecordingLLM(settings)
    worker = Worker(sm, llm, settings)
    await worker.drain_tier("t2", 2, handle_t2, TierStats())
    order = [m for tier, m in llm.models if tier == "tier2"]
    assert order == [configured] * 4 + ["kestrel-tier3"] * 2
    switches = sum(1 for x, y in zip(order, order[1:], strict=False) if x != y)
    assert switches == 1


async def test_model_groups_put_the_configured_model_first_then_the_largest(sm):
    settings = get_settings()
    async with sm() as s:
        for i, model in enumerate(["kestrel-a", "kestrel-b", "kestrel-b", None, "kestrel-a",
                                   "kestrel-b"]):
            payload: dict = {"opportunity_id": 100 + i}
            if model:
                payload["model_override"] = {"backend": "local", "model": model}
            s.add(Task(type="t2", payload=payload))
        await s.commit()
    worker = Worker(sm, _RecordingLLM(settings), settings)
    groups = await worker._model_groups("t2")
    assert len(groups) == 3
    seen = []
    async with sm() as s:
        for where in groups:
            rows = (await s.execute(select(Task).where(where))).scalars().all()
            seen.append(sorted({(t.payload.get("model_override") or {}).get("model")
                                for t in rows}, key=str))
    assert seen == [[None], ["kestrel-b"], ["kestrel-a"]]
