"""Scan and run visibility in the desktop app (D-UI-34..38): the worker loop records Run rows for
cycles that have work and closes an orphaned one as interrupted at the next start; the Scan now
button shows Scanning and cancels pending scrapes; the status rail carries the scan-finished
signal the toast reads; Today states what is happening now and keeps quarantine apart from the
queue; the Runs page shows a running run and recent scans. Offline: no model, no network."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.base import iso, utcnow_iso
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import Quarantine, Run, ScrapeRun, Source, Task
from kestrel.pipeline import runlog
from kestrel.web import deps


def _fresh_engine():
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    return make_async_engine(paths.db_path())


async def _seed(rows: list | None = None) -> None:
    await reset_engine()
    engine = _fresh_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        for row in rows or []:
            s.add(row)
            await s.flush()
        await s.commit()
    await engine.dispose()
    forget_engine()
    deps.set_ui_scheme_cache(None)


def _with_session(fn):
    async def _run():
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        try:
            return await fn(sm)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _rows(model, *where):
    async def _q(sm):
        async with sm() as s:
            return list((await s.execute(select(model).where(*where))).scalars().all())

    return _with_session(_q)


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


@pytest.fixture(autouse=True)
def _no_ollama(monkeypatch):
    from kestrel.web import routes_control

    async def _fake(native_url: str):
        return {"up": True, "loaded": []}

    monkeypatch.setattr(routes_control, "_probe_ollama", _fake)
    deps.set_ui_scheme_cache(None)
    yield
    deps.set_ui_scheme_cache(None)


def _ago_iso(**kw) -> str:
    return iso(datetime.now(UTC) - timedelta(**kw))


def _src(name: str = "Board", **kw) -> Source:
    return Source(track="employment", kind="jobspy", name=name, enabled=True, **kw)


# ------------------------------------------------------------------------- Run row lifecycle
def test_cycle_with_no_work_writes_no_run_row():
    asyncio.run(_seed())

    async def _cycle(sm):
        assert not await runlog.has_pipeline_work(sm)
        cycle = runlog.CycleRun(sm, mode="local")
        await cycle.record("tier0", {"seen": 0, "pass": 0}, did_work=False)
        await cycle.finish("done")
        return cycle.run_id

    assert _with_session(_cycle) is None
    assert _rows(Run) == []


def test_cycle_run_opens_updates_live_and_finishes():
    asyncio.run(_seed([_src(), Task(type="scrape", status="pending", payload={"source_id": 1})]))

    async def _cycle(sm):
        from kestrel.pipeline.worker import TierStats

        assert await runlog.has_pipeline_work(sm)
        cycle = runlog.CycleRun(sm, mode="local")
        await cycle.ensure_open()
        async with sm() as s:
            s.add(ScrapeRun(source_id=1, status="ok", counts={"new": 4}, started_at=utcnow_iso()))
            await s.commit()
        await cycle.record_scrapes(TierStats(completed=1))
        async with sm() as s:  # mid-cycle: still running, counts already visible
            live = await s.get(Run, cycle.run_id)
            assert live.status == "running" and live.finished_at is None
            assert live.counts["scrape"]["new"] == 4 and live.counts["scrape"]["completed"] == 1
        await cycle.record("tier2", runlog.tier_counts(TierStats(completed=2)), did_work=True)
        await cycle.finish("done")
        return cycle.run_id

    run_id = _with_session(_cycle)
    (run,) = _rows(Run)
    assert run.id == run_id and run.name == runlog.APP_RUN_NAME and run.mode == "local"
    assert run.status == "done" and run.finished_at
    assert run.counts["tier2"]["completed"] == 2 and "finished_at" in run.counts["scrape"]


def test_a_stage_that_finds_work_opens_the_run_lazily():
    asyncio.run(_seed())

    async def _cycle(sm):
        from kestrel.pipeline.worker import TierStats

        cycle = runlog.CycleRun(sm, mode="hosted")
        await cycle.record("embed", runlog.tier_counts(TierStats(completed=3)), did_work=True)
        await cycle.finish("done")

    _with_session(_cycle)
    (run,) = _rows(Run)
    assert run.status == "done" and run.mode == "hosted" and run.counts["embed"]["completed"] == 3


def test_leases_this_loop_cannot_claim_are_not_work_and_leave_no_empty_row():
    """Review finding 2: a concurrent CLI's live lease, or an expired lease out of attempts,
    made has_pipeline_work true every 5 s and left an empty `done` Run row each time."""
    future = iso(datetime.now(UTC) + timedelta(minutes=10))
    past = iso(datetime.now(UTC) - timedelta(minutes=10))
    asyncio.run(_seed([
        Task(type="t1", status="leased", lease_until=future, attempts=1, payload={}),
        Task(type="t2", status="leased", lease_until=past, attempts=3, max_attempts=3,
             payload={}),
    ]))

    async def _cycle(sm):
        assert not await runlog.has_pipeline_work(sm)
        # Even a row opened on the queue's say-so is discarded when no stage did work.
        cycle = runlog.CycleRun(sm, mode="local")
        await cycle.ensure_open()
        await cycle.record("tier0", {"seen": 0, "pass": 0}, did_work=False)
        await cycle.finish("done")
        async with sm() as s:
            s.add(Task(type="t1", status="leased", lease_until=past, attempts=1, payload={}))
            await s.commit()
        return await runlog.has_pipeline_work(sm)  # an expired lease with attempts left IS work

    assert _with_session(_cycle) is True
    assert _rows(Run) == []


def test_a_scan_where_every_search_crashed_is_recorded_as_failed_with_errors():
    """Review finding 3: completed 0 returned early, so a scan of crashes recorded nothing and
    the toast, rail and Today stayed silent."""
    asyncio.run(_seed([_src()]))

    async def _cycle(sm):
        from kestrel.pipeline.worker import TierStats

        cycle = runlog.CycleRun(sm, mode="local")
        await cycle.record_scrapes(TierStats(completed=0, errors=2))
        await cycle.finish("done")

    _with_session(_cycle)
    (run,) = _rows(Run)
    assert run.status == "failed" and "Search done; 2 searches had a problem" in (run.notes or "")
    assert run.counts["scrape"]["errors"] == 2 and run.counts["scrape"]["completed"] == 0
    with _client() as c:
        rail = c.get("/status/rail").text
        status = c.get("/api/status").json()
    assert f'data-event="{run.id}"' in rail and 'data-errors="2"' in rail
    assert status["scan"]["state"] == "warn" and "had errors" in status["scan"]["label"]
    now = _now()
    # D-UI-63: the search step never reads "finished"; the sources that had a problem are named.
    assert "finished" not in now["line"] and "2 sources had a problem" in now["line"]


def test_latest_scan_is_found_past_any_number_of_later_scoring_runs():
    """Review finding 2: latest_scan only looked at the newest 20 runs."""
    later = [Run(name=runlog.APP_RUN_NAME, status="done", finished_at=utcnow_iso(),
                 counts={"tier1": {"completed": 1}}) for _ in range(30)]
    asyncio.run(_seed([
        Run(name=runlog.APP_RUN_NAME, status="done", finished_at=utcnow_iso(),
            counts={"scrape": {"completed": 1, "errors": 0, "new": 5,
                               "finished_at": utcnow_iso()}}),
        *later,
    ]))

    async def _q(sm):
        from kestrel.analytics.today import latest_scan

        async with sm() as s:
            return await latest_scan(s)

    scan = _with_session(_q)
    assert scan["id"] == 1 and scan["new"] == 5 and scan["errors"] == 0


def test_orphaned_app_runs_are_interrupted_and_cli_runs_left_alone():
    asyncio.run(_seed([
        Run(name=runlog.APP_RUN_NAME, status="running"),
        Run(name=runlog.APP_RUN_NAME, status="done", finished_at=utcnow_iso()),
        Run(name="pipeline", status="running"),  # a CLI run in another process may be live
    ]))
    n = _with_session(runlog.close_orphaned_runs)
    assert n == 1
    by_status = {(r.name, r.status) for r in _rows(Run)}
    assert (runlog.APP_RUN_NAME, "interrupted") in by_status
    assert ("pipeline", "running") in by_status
    interrupted = _rows(Run, Run.status == "interrupted")[0]
    assert interrupted.finished_at and "stopped before this run finished" in interrupted.notes


def _patch_loop(monkeypatch, stop: asyncio.Event, *, scrape_work: bool):
    from kestrel.pipeline import tier0
    from kestrel.pipeline.worker import TierStats, Worker
    from kestrel.web import app as app_mod

    done = {"t2": False}

    async def _reclaim(self) -> int:
        return 0

    async def _drain_scrapes(self, stats=None):
        stats = stats or TierStats()
        if not scrape_work:
            return stats
        async with self._sm() as s:
            q = select(Task).where(Task.type == "scrape", Task.status == "pending")
            todo = list((await s.execute(q)).scalars())
            if not todo:
                return stats
            for t in todo:
                t.status = "done"
            s.add(ScrapeRun(source_id=1, status="ok", counts={"new": 3}, started_at=utcnow_iso()))
            await s.commit()
        stats.completed = 1
        return stats

    async def _tier0(sm, *, settings, track=None, limit=None):
        return {"seen": 0, "pass": 0, "reject": 0, "geo_uncertain": 0}

    async def _drain_embed(self, stats):
        return None

    async def _drain_tier(self, task_type, concurrency, handler, stats):
        if task_type == "t2" and scrape_work and not done["t2"]:
            done["t2"] = True
            stats.completed = 1

    async def _noop(*a, **k):
        return None

    async def _wait(_stop, _seconds):  # the first idle cycle ends the test
        stop.set()

    monkeypatch.setattr(Worker, "reclaim", _reclaim)
    monkeypatch.setattr(Worker, "drain_scrapes", _drain_scrapes)
    monkeypatch.setattr(Worker, "drain_embed", _drain_embed)
    monkeypatch.setattr(Worker, "drain_tier", _drain_tier)
    monkeypatch.setattr(tier0, "run_tier0_batch", _tier0)
    monkeypatch.setattr(app_mod, "_maybe_run_liveness", _noop)
    monkeypatch.setattr(app_mod, "_drift_due", lambda: False)
    monkeypatch.setattr(app_mod, "_wait", _wait)


def test_desktop_worker_loop_records_one_run_per_cycle_with_work(monkeypatch):
    from kestrel.web import app as app_mod

    asyncio.run(_seed([_src(), Task(type="scrape", status="pending", payload={"source_id": 1})]))
    stop = asyncio.Event()
    _patch_loop(monkeypatch, stop, scrape_work=True)
    asyncio.run(asyncio.wait_for(app_mod._worker_loop(stop), timeout=20.0))
    forget_engine()

    runs = _rows(Run)
    assert len(runs) == 1, "the idle cycle after the work must not write a row"
    run = runs[0]
    assert run.name == runlog.APP_RUN_NAME and run.status == "done" and run.finished_at
    assert run.counts["scrape"]["completed"] == 1 and run.counts["scrape"]["new"] == 3
    assert run.counts["tier2"]["completed"] == 1 and "tier0" in run.counts


def test_desktop_worker_loop_closes_an_orphan_at_start(monkeypatch):
    from kestrel.web import app as app_mod

    asyncio.run(_seed([Run(name=runlog.APP_RUN_NAME, status="running",
                           started_at=iso(datetime.now(UTC) - timedelta(hours=1)))]))
    stop = asyncio.Event()
    _patch_loop(monkeypatch, stop, scrape_work=False)
    asyncio.run(asyncio.wait_for(app_mod._worker_loop(stop), timeout=20.0))
    forget_engine()
    runs = _rows(Run)
    assert [r.status for r in runs] == ["interrupted"]


# ------------------------------------------------------------------------------ scan button
def test_scan_button_idle_and_scanning_states():
    asyncio.run(_seed([_src()]))
    with _client() as c:
        idle = c.get("/scan/button").text
    assert 'data-testid="scan-now"' in idle and "Run now" in idle
    assert "every 5s" not in idle  # no polling while idle

    asyncio.run(_seed([_src(), Task(type="scrape", status="pending", payload={"source_id": 1}),
                       Task(type="scrape", status="leased", payload={"source_id": 1})]))
    with _client() as c:
        busy = c.get("/scan/button").text
        today = c.get("/").text
    assert 'data-state="scanning"' in busy and "Scanning" in busy and "Cancel scan" in busy
    assert 'hx-post="/scan/cancel"' in busy and 'hx-trigger="every 5s"' in busy
    assert "2 searches to go" in busy
    assert 'id="scan-btn"' in today and "Cancel scan" in today  # Today carries the same partial


def test_scan_start_queues_every_enabled_search_and_says_when_there_is_none():
    recent = utcnow_iso()  # not due by cadence: Scan now runs it anyway
    asyncio.run(_seed([_src("A", last_run_at=recent), _src("B"),
                       Source(track="employment", kind="jobspy", name="off", enabled=False)]))
    with _client() as c:
        html = c.post("/scan/start").text
    assert 'data-state="scanning"' in html
    assert len(_rows(Task, Task.type == "scrape", Task.status == "pending")) == 2

    asyncio.run(_seed())
    with _client() as c:
        empty = c.post("/scan/start").text
    assert "Run now" in empty and "No searches are on" in empty


def test_cancel_scan_cancels_pending_and_lets_the_leased_one_finish():
    past = iso(datetime.now(UTC) - timedelta(minutes=10))
    future = iso(datetime.now(UTC) + timedelta(minutes=10))
    asyncio.run(_seed([
        _src("A"), _src("B"), _src("C"), _src("D"),
        Task(type="scrape", status="pending", payload={"source_id": 2}),
        Task(type="scrape", status="pending", payload={"source_id": 3}),
        Task(type="scrape", status="leased", lease_until=future, attempts=1,
             payload={"source_id": 1}),
        # An expired lease with attempts left is claimable next: it is cancelled too.
        Task(type="scrape", status="leased", lease_until=past, attempts=1,
             payload={"source_id": 4}),
        Task(type="t1", status="pending", payload={}),
    ]))
    with _client() as c:
        html = c.post("/scan/cancel").text
    assert "Cancelled 3 searches" in html and "The one in progress finishes first" in html
    assert 'data-state="scanning"' in html  # the live-leased one is still running
    statuses = sorted(t.status for t in _rows(Task, Task.type == "scrape"))
    assert statuses == ["cancelled", "cancelled", "cancelled", "leased"]
    assert _rows(Task, Task.type == "t1")[0].status == "pending"  # scoring is untouched
    # Review finding 1: a cancelled search did not run, so Sources must not say it did.
    assert all(not s_.last_run_at for s_ in _rows(Source))

    async def _requeue(sm):  # ...but the scheduler does not queue them straight back
        from kestrel.ingest.scheduler import enqueue_due_scrapes, scan_hold_until
        from kestrel.queue.queue import TaskQueue

        async with sm() as s:
            held = await scan_hold_until(s)
            n = await enqueue_due_scrapes(s, TaskQueue(sm))
            later = await enqueue_due_scrapes(
                s, TaskQueue(sm), now=datetime.now(UTC) + timedelta(hours=2)
            )
            return held, n, later

    held, n, later = _with_session(_requeue)
    assert held is not None and n == 0
    assert later == 3  # once the hold lapses the due searches run (A is still leased)


def test_scan_now_and_the_tray_clear_the_cancel_hold():
    asyncio.run(_seed([_src("A"), Task(type="scrape", status="pending", payload={"source_id": 1})]))
    with _client() as c:
        c.post("/api/scan/cancel")
        r = c.post("/api/scan/now").json()
    assert r == {"enqueued": 1}  # an explicit scan is never held

    async def _held(sm):
        from kestrel.ingest.scheduler import scan_hold_until

        async with sm() as s:
            return await scan_hold_until(s)

    assert _with_session(_held) is None


def test_the_run_schedule_waits_out_a_cancel_hold_without_losing_its_period():
    asyncio.run(_seed([_src("A")]))

    async def _go(sm):
        from kestrel.ingest import scheduler
        from kestrel.queue.queue import TaskQueue
        from kestrel.services import run_schedule as rs

        async with sm() as s:
            await rs.save(s, rs.RunSchedule(kind="daily", time="00:00"))
            s.add(Task(type="scrape", status="pending", payload={"source_id": 1}))
            await s.commit()
            await scheduler.cancel_pending_scrapes(s)
            held = await rs.fire_if_due(s, TaskQueue(sm))
            await scheduler.clear_scan_hold(s)
            await s.commit()
            fired = await rs.fire_if_due(s, TaskQueue(sm))
            return held, fired

    assert _with_session(_go) == (None, 1)


def test_cancel_with_nothing_running_returns_the_idle_button():
    asyncio.run(_seed([_src()]))
    with _client() as c:
        r = c.post("/api/scan/cancel").json()
        html = c.post("/scan/cancel").text
    assert r == {"cancelled": 0}
    assert 'data-testid="scan-now"' in html


# ------------------------------------------------------------------------------------ toast
def test_rail_carries_the_scan_finished_signal_and_base_has_one_toast():
    asyncio.run(_seed([
        _src(),
        Run(name=runlog.APP_RUN_NAME, status="done", finished_at=utcnow_iso(),
            counts={"scrape": {"completed": 2, "errors": 0, "new": 7,
                               "finished_at": utcnow_iso()}}),
        Run(name=runlog.APP_RUN_NAME, status="done", finished_at=utcnow_iso(),
            counts={"tier1": {"completed": 1}}),  # a later scoring-only cycle is not a scan
    ]))
    with _client() as c:
        rail = c.get("/status/rail").text
        page = c.get("/runs").text
        status = c.get("/api/status").json()
    assert 'id="scan-signal" hx-swap-oob="true"' in rail
    assert 'data-event="1"' in rail and 'data-new="7"' in rail and 'data-scanning="0"' in rail
    assert status["scan_state"]["event"] == 1 and status["scan_state"]["new"] == 7
    assert page.count('id="kestrel-toast"') == 1
    assert 'aria-live="polite"' in page and "'Search done: '" in page
    assert "Scan finished" not in page
    assert "localStorage" in page and "try {" in page


def test_rail_signal_while_scanning_and_before_any_scan():
    asyncio.run(_seed([_src(), Task(type="scrape", status="pending", payload={"source_id": 1})]))
    with _client() as c:
        rail = c.get("/status/rail").text
    assert 'data-scanning="1"' in rail and 'data-event="none"' in rail


_TOAST_HARNESS = r"""
const attrs = {};
const sig = {
  hasAttribute: (k) => k in attrs, getAttribute: (k) => (k in attrs ? attrs[k] : null),
};
const shown = [];
const toast = {
  hidden: true, querySelector: () => ({ set textContent(v) { shown.push(v); } }),
  setAttribute() {}, removeAttribute() {}, hasAttribute: () => false,
};
const listeners = {};
global.document = {
  getElementById: (id) => (id === 'scan-signal' ? sig : id === 'kestrel-toast' ? toast : null),
  addEventListener: (ev, fn) => { listeners[ev] = fn; },
};
global.window = {};
global.sessionStorage = undefined;  // the memo fallback path
global.requestAnimationFrame = (f) => f();
global.setTimeout = (f) => { f(); return 0; };
global.clearTimeout = () => {};
eval(SCRIPT);
function poll(state) { Object.assign(attrs, state); listeners['htmx:afterSettle'](); }
poll({'data-ready': '1', 'data-event': '1', 'data-scanning': '0', 'data-new': '0'});
// A new scan finishes while another is still queued: the event changes mid-scan...
poll({'data-event': '2', 'data-scanning': '1', 'data-new': '4', 'data-errors': '1',
      'data-scoring': '3'});
// ...and the toast shows once scanning clears, with the latest event.
poll({'data-event': '2', 'data-scanning': '0'});
poll({'data-event': '2', 'data-scanning': '0'});  // and only once
// Scoring done by the next search: the toast says so without "Still scoring".
poll({'data-event': '3', 'data-scanning': '0', 'data-new': '1', 'data-errors': '0',
      'data-scoring': '0'});
console.log(JSON.stringify(shown));
"""


def test_toast_shows_an_event_that_landed_while_still_scanning(tmp_path):
    """Review finding 4: an event that changed in a poll still showing data-scanning="1" was
    marked seen without a toast, so it never showed."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    html = (Path(paths.web_templates_dir()) / "partials" / "toast.html").read_text("utf-8")
    script = html.split("<script>")[1].split("</script>")[0]
    js = tmp_path / "toast_harness.js"
    js.write_text(f"const SCRIPT = {json.dumps(script)};\n{_TOAST_HARNESS}", encoding="utf-8")
    out = subprocess.run([node, str(js)], capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(out.stdout.strip().splitlines()[-1]) == [
        "Search done: 4 new, 1 source had a problem. Still scoring 3.",
        "Search done: 1 new.",
    ]


def test_toast_styles_use_theme_tokens_and_respect_reduced_motion():
    css = (Path(paths.web_templates_dir()) / "partials" / "toast.html").read_text(encoding="utf-8")
    style = css.split("<style>")[1].split("</style>")[0]
    assert "@keyframes scan-glow" in style and "prefers-reduced-motion: reduce" in style
    assert "#D4622A" not in style.upper() and "var(--accent" in style
    assert "—" not in css


# ------------------------------------------------------------------------------ Today: now
def _now(**kw):
    from kestrel.analytics.today import happening_now

    async def _q(sm):
        async with sm() as s:
            return await happening_now(s, mode=kw.pop("mode", "local"), **kw)

    return _with_session(_q)


def test_now_line_scanning_scoring_idle_and_paused():
    asyncio.run(_seed([_src(), Task(type="scrape", status="pending", payload={}),
                       Task(type="scrape", status="leased", payload={}),
                       Task(type="t1", status="pending", payload={})]))
    now = _now()
    assert now["state"] == "scanning"
    assert now["line"].startswith("Scanning: 2 searches to go. Nothing to do yet.")
    assert _now(paused=True)["state"] == "paused"

    asyncio.run(_seed([Task(type="t1", status="pending", payload={}),
                       Task(type="t2", status="leased", payload={})]))
    now = _now(mode="hosted")
    assert now["line"] == ("Scoring: 2 postings waiting for the quick screen and the full review"
                           " on hosted models. "
                           "Nothing to do; results land in Pipeline.")

    asyncio.run(_seed())
    assert _now()["line"] == "Idle. No scan yet; press Run now to run the first one."

    at = iso(datetime.now(UTC).replace(hour=14, minute=5))
    asyncio.run(_seed([Run(name=runlog.APP_RUN_NAME, status="done",
                           counts={"scrape": {"completed": 3, "new": 12, "finished_at": at}})]))
    now = _now()
    hhmm = datetime.fromisoformat(at).astimezone().strftime("%H:%M")
    assert now["state"] == "idle"
    assert now["line"].startswith(f"Idle. Last scan {hhmm} found 12 new.")
    for line in (now["line"],):
        assert "—" not in line


def test_quarantine_is_stated_apart_from_pending_work():
    asyncio.run(_seed([Task(type="t1", status="pending", payload={}),
                       Quarantine(tier=1, model="m", prompt="p", raw_response="r",
                                  error="the reply hit max_tokens 900 before the structured "
                                        "object was complete"),
                       Quarantine(tier=2, model="m", prompt="p", raw_response="r",
                                  error="finish_reason: length")]))
    now = _now()
    assert now["state"] == "scoring" and now["quarantined"] == 2
    assert now["quarantine"] == ("2 postings need another try. The model ran out of room. "
                                 "Retry them, or pick another model on Models.")
    with _client() as c:
        page = c.get("/").text
        partial = c.get("/today/now").text
    # Today shows the run panel in place of the Now line (D-UI-59); the line's partial stays.
    assert 'data-testid="td-strip"' in page and 'data-testid="today-now"' not in page
    assert 'hx-get="/today/now"' in partial
    assert "Scoring: 1 posting waiting for the quick screen" in partial


# ---------------------------------------------------------------------------------- Runs page
def test_runs_page_shows_a_running_run_and_its_searches():
    asyncio.run(_seed([
        _src("Indeed VP IT"),
        Run(name=runlog.APP_RUN_NAME, status="running", mode="local",
            started_at=_ago_iso(minutes=10),
            counts={"scrape": {"completed": 1, "errors": 0, "new": 5}}),
        ScrapeRun(source_id=1, status="ok", counts={"new": 5}, started_at=_ago_iso(minutes=9),
                  finished_at=_ago_iso(minutes=8)),
        ScrapeRun(source_id=1, status="failed", error="HTTP 429", counts={},
                  started_at=_ago_iso(minutes=7), finished_at=_ago_iso(minutes=6)),
        Task(type="scrape", status="pending", payload={"source_id": 1}),
    ]))
    with _client() as c:
        page = c.get("/runs").text
        live = c.get("/runs", headers={"HX-Request": "true"}).text
    for html in (page, live):
        card = html.split('data-testid="run-live"')[1].split('data-region="history"')[0]
        assert "Running now" in card and 'data-run="active"' in card
        assert "5 found so far" in card
        events = card.split('data-testid="run-events"')[1].split("</ul>")[0]
        assert "was rate-limited; it is retried next run" in events  # plain words, never "HTTP 429"
        assert "HTTP 429" not in html


def test_runs_page_empty_says_when_a_run_is_recorded():
    asyncio.run(_seed())
    with _client() as c:
        html = c.get("/runs").text
    assert "A run is recorded when the worker has something to scan or score." in html
    assert "No runs yet." in html


_STICKY_HARNESS = r"""
const attrs = {};
const sig = {
  hasAttribute: (k) => k in attrs, getAttribute: (k) => (k in attrs ? attrs[k] : null),
};
const log = [];
const linkAttrs = {};
const link = {
  set textContent(v) { log.push(['text', v]); },
  setAttribute: (k, v) => { linkAttrs[k] = v; }, removeAttribute: (k) => { delete linkAttrs[k]; },
  getAttribute: (k) => linkAttrs[k],
};
const toastAttrs = {};
const toast = {
  hidden: true, querySelector: (q) => (q.indexOf('[href]') >= 0 && !linkAttrs.href ? null : link),
  setAttribute: (k, v) => { toastAttrs[k] = v; },
  removeAttribute: (k) => { if (k === 'data-show') log.push(['hide']); delete toastAttrs[k]; },
  hasAttribute: (k) => k in toastAttrs,
};
const listeners = {};
let timers = 0; const holds = [];
global.document = {
  getElementById: (id) => (id === 'scan-signal' ? sig : id === 'kestrel-toast' ? toast : null),
  addEventListener: (ev, fn) => { listeners[ev] = fn; },
};
let went = null;
global.window = {location: {assign: (u) => { went = u; }}};
global.sessionStorage = undefined;
global.requestAnimationFrame = (f) => f();
global.setTimeout = (f, ms) => {
  if (ms && ms >= 1000) { timers += 1; holds.push(ms); return 1; }
  f(); return 0;
};
global.clearTimeout = () => {};
eval(SCRIPT);
function poll(state) { Object.assign(attrs, state); listeners['htmx:afterSettle'](); }
poll({'data-ready': '1', 'data-event': '1', 'data-scanning': '0', 'data-new': '0'});
poll({'data-event': '2', 'data-new': '3', 'data-errors': '0'});
const first = {href: linkAttrs.href, tone: toastAttrs['data-tone'] || null, timers: timers,
               hidden: log.filter((x) => x[0] === 'hide').length};
poll({'data-event': '3', 'data-new': '1', 'data-errors': '2'});
const second = {tone: toastAttrs['data-tone'] || null, timers: timers};
// A click on the box (not the link, not the x) opens the new postings and closes the toast.
const box = {closest: (q) => (q === '#kestrel-toast' ? toast : null)};
listeners['click']({target: box});
window.kestrelToast('Saved');  // any other toast still fades on its own
console.log(JSON.stringify({first, second, went, plainTimers: timers, holds,
                            plainHref: linkAttrs.href || null}));
"""


def test_scan_toast_clears_itself_and_opens_the_new_postings(tmp_path):
    """The scan toast clears itself after 9 s (#86), links to the Pipeline's New today chip,
    keeps the warning tone when the scan had errors, and a click on it navigates there."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    html = (Path(paths.web_templates_dir()) / "partials" / "toast.html").read_text("utf-8")
    script = html.split("<script>")[1].split("</script>")[0]
    js = tmp_path / "toast_sticky.js"
    js.write_text(f"const SCRIPT = {json.dumps(script)};\n{_STICKY_HARNESS}", encoding="utf-8")
    out = subprocess.run([node, str(js)], capture_output=True, text=True, timeout=30, check=True)
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["first"] == {"href": "/opportunities?new_today=1", "tone": None, "timers": 1,
                            "hidden": 0}
    assert got["second"] == {"tone": "warn", "timers": 2}
    assert got["went"] == "/opportunities?new_today=1"
    # The scan toast holds 9 s (the last is a fallback note sharing its box); plain and Undo
    # toasts go through KUI.toast and keep their own 2600 / 8000 ms.
    assert got["holds"] == [9000, 9000, 9000]
    assert got["plainTimers"] == 3 and got["plainHref"] is None


def test_toast_markup_is_a_link_with_a_warning_tone_and_stays_announced():
    html = (Path(paths.web_templates_dir()) / "partials" / "toast.html").read_text("utf-8")
    assert 'role="status" aria-live="polite"' in html and "data-toast-close" in html
    assert '<a data-toast-text data-testid="toast-link"></a>' in html
    assert "'/opportunities?new_today=1'" in html and "main: true" in html and "sticky" not in html
    style = html.split("<style>")[1].split("</style>")[0]
    # The mockup's toast look (a filled chip at the bottom); the warning tone fills amber.
    warn = ('#kestrel-toast[data-tone="warn"] '
            '{ background: var(--warn-bg); color: var(--warn-ink); }')
    assert warn in style
    # Marked seen only when shown, as before.
    assert html.index("setSeen(ev);\n        var n") > html.index("Marked seen only when shown")
