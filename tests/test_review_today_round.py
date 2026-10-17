"""The review of the Today run panel round (D-UI-59..68, D-LLM-6, D-MCP-1, D-SRC-6): one test
per finding. Offline: no model, no network, no real MCP port held open past a test."""

from __future__ import annotations

import asyncio
import socket
import sys

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.analytics import run_summary as rs
from kestrel.db.base import utcnow_iso
from kestrel.db.engine import make_async_engine
from kestrel.db.models import DocMaster, Evaluation, Run, Setting, Source, Task
from kestrel.pipeline import runlog
from kestrel.services import tracks
from tests.test_run_summary import _ago, _client, _opp, _panel, _seed, _src, _summary

HX = {"HX-Request": "true"}


@pytest.fixture(autouse=True)
def _no_ollama(monkeypatch):
    from kestrel.web import routes_control

    async def _fake(native_url: str):
        return {"up": True, "loaded": []}

    monkeypatch.setattr(routes_control, "_probe_ollama", _fake)
    yield
    tracks.forget_cache()


def _db(fn):
    """Run `fn(session)` on a fresh engine over the per-test database file."""

    async def go():
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sm() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(go())


# ------------------------------------------------------------- finding 1: one live predicate
def test_held_track_work_never_reads_as_running_and_cancel_stops_the_held_scrape():
    """Employment only; one pending consulting scrape and one consulting t2 task. The scan is
    not running, the rail does not say Scanning, and Cancel cancels the held scrape."""
    from kestrel.web.routes_control import run_active, scan_state

    asyncio.run(_seed([
        Setting(key="tracks.enabled", value=["employment"]),
        _src("Cons", track="consulting"),
        _opp(1, track="consulting", stage="t1_pass"),
        Task(type="scrape", status="pending", payload={"source_id": 1}),
        Task(type="t2", status="pending", payload={"opportunity_id": 1}),
    ]))
    tracks._cache["value"] = ("employment",)

    async def state(s):
        return await scan_state(s), await run_active(s)

    st, active = _db(state)
    assert st["scanning"] is False and st["remaining"] == 0 and st["scoring"] == 0
    assert active is False
    assert not _summary(None)["active"]
    with _client() as c:
        rail = c.get("/status/rail").text
        assert "Scanning" not in rail
        assert c.post("/api/scan/cancel").json() == {"cancelled": 1}

    async def statuses(s):
        return dict((await s.execute(select(Task.type, Task.status))).all())

    assert _db(statuses) == {"scrape": "cancelled", "t2": "pending"}  # t2 waits, unchanged


def test_runs_page_keeps_finished_counts_while_held_live_counts_drop():
    from kestrel.web.queries import live_activity

    asyncio.run(_seed([
        Setting(key="tracks.enabled", value=["employment"]),
        _src("Cons", track="consulting"),
        Task(type="scrape", status="pending", payload={"source_id": 1}),
        Task(type="scrape", status="done", payload={"source_id": 1}),
    ]))
    tracks._cache["value"] = ("employment",)
    act = _db(live_activity)
    assert act["tasks"]["scrape"] == {"done": 1}


# ------------------------------------------------------------- finding 2: pause between tasks
async def test_a_pause_finishes_the_leased_task_and_claims_no_more(sm):
    from kestrel.config import get_settings
    from kestrel.pipeline._shared import HandlerOutcome
    from kestrel.pipeline.worker import TierStats, Worker
    from kestrel.queue.queue import TaskQueue

    q = TaskQueue(sm)
    for i in range(3):
        await q.enqueue("embed", {"opportunity_id": i + 1})
    paused = {"on": False}
    seen: list[int] = []

    async def handler(_sm, _client, _settings, *, opportunity_id, profiles):
        seen.append(opportunity_id)
        paused["on"] = True  # the operator pauses while this task runs
        return HandlerOutcome("done", 0)

    worker = Worker(sm, object(), get_settings(), paused=lambda: paused["on"])
    stats = TierStats()
    await worker.drain_tier("embed", 1, handler, stats)
    assert seen == [1] and stats.completed == 1
    async with sm() as s:
        result = await s.execute(select(Task.status, func.count()).group_by(Task.status))
        rows = {st: int(n) for st, n in result.all()}
    assert rows == {"done": 1, "pending": 2}  # finished, nothing released or claimed


def test_paused_with_nothing_leased_reads_paused_with_resume_and_a_slow_poll(monkeypatch):
    from kestrel.web import app as app_mod

    asyncio.run(_seed([
        _src("Ops"),
        _opp(1, stage="t1_pass"),
        Task(type="t2", status="pending", payload={"opportunity_id": 1}),
    ]))
    monkeypatch.setattr(app_mod, "_paused", type("E", (), {"is_set": lambda self: True})())
    s = _summary(None)
    assert s["paused"] and s["finishing"] == 0 and rs.step_words(s) == "Paused"
    with _client() as c:
        panel = _panel(c.get("/").text)
        live = c.get("/runs").text
    assert ">Paused<" in panel and 'hx-trigger="every 30s, run-refresh from:body"' in panel
    assert 'hx-post="/api/worker/resume"' in panel and "tray" not in panel  # finding 14
    assert 'data-testid="run-resume"' in live and "from the tray" not in live


def test_resume_answers_for_the_tray_and_redraws_the_panel():
    from kestrel.web.app import is_paused, set_paused

    asyncio.run(_seed())
    set_paused(True)
    try:
        with _client() as c:
            r = c.post("/api/worker/resume", headers=HX)
        assert r.json() == {"paused": False} and r.headers["HX-Trigger"] == "run-refresh"
        assert not is_paused()
    finally:
        set_paused(False)


def test_a_resume_never_cuts_an_open_runs_window():
    """Counts never drop on resume: the open run's start is the window, not the resume stamp."""
    asyncio.run(_seed([
        _src("Ops"),
        Run(name=runlog.APP_RUN_NAME, status="running", started_at=_ago(minutes=30)),
        _opp(1, stage="t2_done"), _opp(2, stage="t1_pass"),
        Evaluation(opportunity_id=1, tier=2, track="employment", verdict="apply", score=4.2,
                   weighted_score=4.2, created_at=_ago(minutes=20)),
        Task(type="t2", status="pending", payload={"opportunity_id": 2}),
        Setting(key=rs.RESUMED_KEY, value=_ago(minutes=5)),
    ]))
    s = _summary(None)
    assert s["active"] and s["rated"] == 1
    assert s["started_at"] <= _ago(minutes=29)


def test_a_paused_cycle_keeps_its_run_open_for_the_next_one():
    """The serve loop carries a cycle a pause cut short; its Run row stays open."""
    import inspect

    from kestrel.web import app as app_mod

    src = inspect.getsource(app_mod._worker_loop)
    assert "carried = cycle" in src and "carried or runlog.CycleRun" in src


# ------------------------------------------------------------- finding 3: SEC titles opt-in
def test_sec_titles_toggle_is_off_by_default_and_rebuilds_on_change():
    from kestrel.ingest.generated import SEC_NEUTRAL_QUERIES, SEC_TITLES_KEY, SEC_TITLES_LABEL
    from kestrel.services import profile_edit as pe

    asyncio.run(_seed([Setting(key="tracks.enabled", value=["employment", "consulting"])]))
    pe.save_section("employment", "about", {"full_name": "Pat", "email": "pat@example.com"})
    pe.save_section("employment", "employment",
                    {"employment_terms": {"target_titles": ["Nurse Manager"]}})
    pe.save_section("consulting", "consulting",
                    {"consulting_terms": {"service_lines": ["Clinical staffing"]}})

    async def sec(s):
        row = (await s.execute(select(Source).where(Source.kind == "sec_edgar"))).scalar_one()
        opt = await s.get(Setting, SEC_TITLES_KEY)
        return row.config["queries"], (opt.value if opt else None)

    with _client() as c:
        c.post("/sources/searches", data={"enable": "0"})
        assert _db(sec) == (SEC_NEUTRAL_QUERIES, None)
        page = c.get("/sources").text
        assert SEC_TITLES_LABEL in page and 'data-testid="sec-titles"' in page
        assert "checked" not in page.split('data-testid="sec-titles"')[1].split(">")[0]
        r = c.post("/sources/sec-titles", data={"on": "1"}, headers=HX)
        assert r.status_code == 200 and "checked" in r.text.split('data-testid="sec-titles"')[1]
        assert _db(sec) == (['"nurse manager"', *SEC_NEUTRAL_QUERIES], True)
        c.post("/sources/sec-titles", data={}, headers=HX)  # unticked: no "on" field
        assert _db(sec) == (SEC_NEUTRAL_QUERIES, False)


# ------------------------------------------------------------- finding 4: stop never raises
async def test_mcp_stop_never_raises_and_always_clears_state():
    from kestrel.mcp import runtime

    async def boom():
        raise OSError("bind went away")

    task = asyncio.create_task(boom())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    runtime._state.update(task=task, stop=asyncio.Event(), port=9110)
    await runtime.stop()  # must not re-raise the stored OSError
    assert runtime._state["task"] is None and runtime._state["stop"] is None
    assert not runtime.running()


def test_lifespan_runs_the_worker_shutdown_even_when_mcp_stop_fails(monkeypatch):
    from fastapi.testclient import TestClient

    from kestrel.mcp import runtime
    from kestrel.web import app as app_mod

    calls: list[str] = []

    async def idle(stop):
        await stop.wait()

    async def noop(*a, **k):
        calls.append("startup-step")

    async def bad_stop():
        raise RuntimeError("stop exploded")

    async def graceful(task, stop):
        calls.append("graceful")
        task.cancel()

    async def rebuild():
        calls.append("rebuild")

    for name in ("_worker_loop", "_delivery_loop", "_stall_monitor", "_setup_models_loop"):
        monkeypatch.setattr(app_mod, name, idle)
    for name in ("_seed_shipped_sources", "_seed_shipped_layouts", "_apply_keep_awake",
                 "_start_mcp_if_enabled"):
        monkeypatch.setattr(app_mod, name, noop)
    monkeypatch.setattr(app_mod, "_rebuild_generated_sources", rebuild)
    monkeypatch.setattr(app_mod, "_graceful_worker_shutdown", graceful)
    monkeypatch.setattr(runtime, "stop", bad_stop)
    asyncio.run(_seed())
    with TestClient(app_mod.build_app(worker=True)) as c:
        assert c.get("/health").status_code == 200
    assert "rebuild" in calls  # finding 9: the startup rebuild runs in the lifespan
    assert calls[-1] == "graceful"
    assert not runtime.is_live()


def test_mcp_switch_failure_is_a_plain_error_not_a_500(monkeypatch):
    from kestrel.mcp import runtime

    async def broken_start(port, *, bind_all=False):
        raise RuntimeError("no loop for you")

    asyncio.run(_seed())
    monkeypatch.setattr(runtime, "is_live", lambda: True)
    monkeypatch.setattr(runtime, "start", broken_start)
    with _client() as c:
        r = c.post("/settings/mcp", data={"enabled": "1"}, headers=HX)
    assert r.status_code == 200
    assert "The MCP server could not be started: no loop for you" in r.text


# ------------------------------------------------------------- finding 5: open run is active
def test_an_open_run_with_no_live_task_reads_finishing_up_never_finished():
    asyncio.run(_seed([
        _src("Ops"),
        Run(name=runlog.APP_RUN_NAME, status="running", started_at=_ago(minutes=3)),
    ]))
    s = _summary(None)
    assert s["active"] and s["stage"] == "finishing" and rs.step_words(s) == "Finishing up"
    with _client() as c:
        panel = _panel(c.get("/").text)
    assert ">Finishing up<" in panel and ">finished<" not in panel and "Last run" not in panel


# ------------------------------------------------------------- finding 6: TIME_WAIT
@pytest.mark.skipif(sys.platform == "win32", reason="SO_REUSEADDR is left alone on Windows")
def test_port_free_is_not_fooled_by_time_wait():
    from kestrel.mcp import runtime

    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(1)
    port = lsock.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    conn, _ = lsock.accept()
    conn.close()  # the server side closes first: its port enters TIME_WAIT
    lsock.close()
    try:
        assert runtime.port_free(port) is True
    finally:
        client.close()


def test_port_free_sets_reuseaddr_only_off_windows(monkeypatch):
    from kestrel.mcp import runtime

    seen: list[tuple] = []

    class FakeSock:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def setsockopt(self, *a):
            seen.append(a)

        def bind(self, addr):
            return None

    monkeypatch.setattr(runtime.socket, "socket", FakeSock)
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    assert runtime.port_free(1) and seen == []
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    assert runtime.port_free(1) and seen


# ------------------------------------------------------------- finding 7: dead inner server
async def test_serve_mcp_ends_when_the_inner_server_ends_and_running_says_so(monkeypatch):
    import uvicorn

    from kestrel.mcp import runtime

    async def dies(self, sockets=None):
        return None  # uvicorn returns like this when its own startup fails

    monkeypatch.setattr(uvicorn.Server, "serve", dies)
    monkeypatch.setattr(runtime, "port_free", lambda port, bind_all=False: True)
    assert await runtime.start(9119) is None
    task = runtime._state["task"]
    # Wait on the task itself, not a polling budget a slow Windows runner can run out of.
    await asyncio.wait({task}, timeout=10)
    assert task.done()
    assert not runtime.running()
    assert runtime.stopped_reason() == "the MCP server stopped unexpectedly"
    await runtime.stop()
    assert runtime.stopped_reason() is None


async def test_stopped_reason_is_right_before_the_done_callback_has_run(monkeypatch):
    """asyncio runs a task's done-callbacks via call_soon AFTER the task is done, so a caller can
    read running() False one loop iteration before the reason is recorded (the Windows CI
    failure: `None == 'the MCP server stopped unexpectedly'`). Hold the callback back entirely:
    stopped_reason must still answer from the finished task, and the late callback must change
    nothing and log nothing a second time."""
    import uvicorn
    from structlog.testing import capture_logs

    from kestrel.mcp import runtime

    async def dies(self, sockets=None):
        return None

    real_done = runtime._done
    monkeypatch.setattr(uvicorn.Server, "serve", dies)
    monkeypatch.setattr(runtime, "port_free", lambda port, bind_all=False: True)
    monkeypatch.setattr(runtime, "_done", lambda task: None)  # the callback has not run yet
    with capture_logs() as logs:
        assert await runtime.start(9119) is None
        task = runtime._state["task"]
        await asyncio.wait({task}, timeout=10)
        assert task.done() and not runtime.running()
        assert runtime.stopped_reason() == "the MCP server stopped unexpectedly"
        real_done(task)  # the callback, late
        assert runtime.stopped_reason() == "the MCP server stopped unexpectedly"
    assert [e for e in logs if e["event"] == "mcp_server_stopped"] == [
        {"event": "mcp_server_stopped", "log_level": "error",
         "error": "RuntimeError: the MCP server stopped unexpectedly"}]
    await runtime.stop()
    assert runtime.stopped_reason() is None


def test_mcp_panel_says_stopped_with_a_reason_and_start_again(monkeypatch):
    from kestrel.mcp import runtime

    asyncio.run(_seed([Setting(key="mcp.enabled", value=True)]))
    monkeypatch.setattr(runtime, "is_live", lambda: True)
    monkeypatch.setattr(runtime, "stopped_reason", lambda: "it stopped on its own")
    with _client() as c:
        body = c.get("/settings/mcp").text
    assert "On, stopped: it stopped on its own" in body
    assert 'data-testid="mcp-restart"' in body and "Start again" in body


# ------------------------------------------------------------- finding 9: startup rebuild
def test_startup_rebuild_retires_the_old_it_sec_source_and_never_blocks(monkeypatch):
    from kestrel.ingest import generated
    from kestrel.ingest.generated import SEC_NEUTRAL_QUERIES, SEC_SOURCE_NAME
    from kestrel.services import profile_edit as pe
    from kestrel.web import app as app_mod

    old = {"user_agent": "Kestrel/1.0 (pat@example.com)", "forms": ["8-K"],
           "queries": ['"chief information officer"', '"information technology"']}
    asyncio.run(_seed([
        Setting(key="tracks.enabled", value=["employment", "consulting"]),
        Source(track="consulting", kind="sec_edgar", name="SEC filings (IT)", config=old,
               enabled=True, origin="generated"),
    ]))
    pe.save_section("employment", "about", {"full_name": "Pat", "email": "pat@example.com"})
    pe.save_section("consulting", "consulting",
                    {"consulting_terms": {"service_lines": ["Clinical staffing"]}})
    asyncio.run(app_mod._rebuild_generated_sources())

    async def rows(s):
        return {r.name: (r.config.get("queries"), r.enabled, r.origin)
                for r in (await s.execute(select(Source).where(Source.kind == "sec_edgar")))
                .scalars()}

    got = _db(rows)
    assert got[SEC_SOURCE_NAME] == (SEC_NEUTRAL_QUERIES, True, "generated")
    assert "SEC filings (IT)" not in got  # the old IT-only row is gone (never ran: deleted)

    async def boom(*a, **k):
        raise RuntimeError("profile unreadable")

    monkeypatch.setattr(generated, "rebuild_generated_sources", boom)
    asyncio.run(app_mod._rebuild_generated_sources())  # logged, never raised


# ------------------------------------------------------------- finding 10: operator requests
def test_operator_requested_tasks_are_never_held_by_the_track_switch():
    from kestrel.pipeline.tasks import enqueue_once
    from kestrel.queue.queue import claimable

    asyncio.run(_seed([
        Setting(key="tracks.enabled", value=["employment"]),
        _src("Cons", track="consulting"),
        _opp(1, track="consulting", stage="t1_pass"),
        _opp(2, track="consulting", stage="t1_pass"),
        _opp(3, track="consulting", stage="t2_done"),
        Task(type="t2", status="pending", payload={"opportunity_id": 1}),  # background: held
        Task(type="t2", status="pending", payload={"opportunity_id": 2}),
    ]))
    tracks._cache["value"] = ("employment",)

    async def claim_ids(s):
        oid = func.json_extract(Task.payload, "$.opportunity_id")
        return sorted(int(x) for x in (await s.execute(
            select(oid).where(claimable(utcnow_iso())))).scalars())

    assert _db(claim_ids) == []

    async def operator_asks(s):
        # Score again on a role whose background task is held, and on one with none queued.
        assert await enqueue_once(s, "t2", 2, extra={"rescore": True, "requested": True}) is False
        assert await enqueue_once(s, "t2", 3, extra={"requested": True}) is True
        await s.commit()

    _db(operator_asks)
    assert _db(claim_ids) == [2, 3]


def test_score_again_and_quarantine_retry_mark_their_tasks_requested():
    import inspect

    from kestrel.mcp import server
    from kestrel.pipeline import tier1
    from kestrel.queue import rescore
    from kestrel.web.pages import runs_detail

    assert '"requested": True' in inspect.getsource(rescore.enqueue_rescore)
    assert 'extra["requested"] = True' in inspect.getsource(rescore.requeue_rescore_task)
    assert 'extra={"requested": True}' in inspect.getsource(runs_detail._retry_quarantine)
    assert '"requested": True' in inspect.getsource(tier1.handle_t1)
    assert '"requested": True' in inspect.getsource(server)


# ------------------------------------------------------------- finding 11: server-side checks
def test_wizard_resume_accept_needs_the_approval_server_side():
    asyncio.run(_seed())
    with _client() as c:
        r = c.post("/api/documents/import/accept",
                   data={"ob": "1", "text": "x", "proposal_json": "{}"}, headers=HX)
    assert r.status_code == 200
    assert r.headers["HX-Retarget"] == "#ob-approve-error"
    assert "Approve your resume text first" in r.text

    async def n_docs(s):
        return (await s.execute(select(func.count()).select_from(DocMaster))).scalar_one()

    assert _db(n_docs) == 0


def test_onboarding_next_ignores_a_posted_step_not_yet_reached():
    from kestrel.services import onboarding as ob

    asyncio.run(_seed())

    async def at_about(s):
        state = await ob.load(s) or ob.OnboardingState()
        state.step = "about"
        state.visited = ["welcome", "models", "tracks"]
        await ob.save(s, state)
        await s.commit()

    _db(at_about)
    with _client() as c:
        # A crafted post claims the searches step; the server reads About you, whose
        # requirement (your name) is unmet, so the wizard stays there and says why.
        r = c.post("/onboarding/next", data={"step": "searches"}, headers=HX)
    assert 'data-step="about"' in r.text and "Add your full name" in r.text


# ------------------------------------------------------------- finding 13: rule segment only
def test_rule_word_reads_the_rule_name_not_the_detail():
    from kestrel.web.fit import rule_word

    # D-UI-73 named the sponsorship rule; the detail's "title" still never names it.
    assert rule_word("blocker:sponsorship: title says visa sponsorship needed") == "sponsorship"
    assert rule_word("blocker:my_own_rule: title words") == "rule"
    assert rule_word("blocker:geo: stale title in Austin") == "location"
    assert rule_word("blocker:anti_title: help desk") == "title"
    assert rule_word(["comp_floor"]) == "pay"
    assert rule_word("blocker:stale: posted 90 days ago") == "posting age"


# ------------------------------------------------------------- finding 15: state keys
def test_app_stamps_never_reach_the_config_layer():
    from kestrel.ingest.generated import SEC_TITLES_KEY
    from kestrel.services.run_schedule import LAST_AT_KEY
    from kestrel.services.setting_overrides import STATE_KEYS, is_config_key

    for key in (LAST_AT_KEY, rs.RESUMED_KEY, SEC_TITLES_KEY):
        assert key in STATE_KEYS and not is_config_key(key)
