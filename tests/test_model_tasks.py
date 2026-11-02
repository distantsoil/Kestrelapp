"""Model downloads and builds as task rows (release round R1, kestrel.services.model_tasks): the
pull-stream progress parser, the Modelfile to /api/create body mapping, resume after a restart
(a leased row from a dead process, and a stream cut off mid-download), cancel (queued and
mid-stream), Ollama's error objects, and the progress the page shows. Ollama's HTTP API is mocked
with respx; nothing touches the network."""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import make_async_engine
from kestrel.db.models import Task
from kestrel.llm import profiles
from kestrel.services import model_tasks as mt
from kestrel.services import setup_state as ss

URL = "http://127.0.0.1:11434"
HW = profiles.Hardware(os="linux", gpu_vendor="nvidia", vram_gb=16.0, ram_gb=64.0)


def _lines(*objs: dict) -> str:
    return "\n".join(json.dumps(o) for o in objs) + "\n"


PULL_OK = _lines(
    {"status": "pulling manifest"},
    {"status": "pulling aaa", "digest": "sha256:aaa", "total": 800},
    {"status": "pulling aaa", "digest": "sha256:aaa", "total": 800, "completed": 400},
    {"status": "pulling bbb", "digest": "sha256:bbb", "total": 200, "completed": 200},
    {"status": "pulling aaa", "digest": "sha256:aaa", "total": 800, "completed": 800},
    {"status": "verifying sha256 digest"},
    {"status": "writing manifest"},
    {"status": "success"},
)


@pytest.fixture
async def sm(monkeypatch):
    monkeypatch.setattr(mt, "RETRY_BACKOFF_S", 0.0)
    monkeypatch.setattr(profiles, "detect_hardware", lambda: HW)
    ss.forget_hardware()
    mt.reset()
    migrate.run_migrations(paths.db_path())
    engine = make_async_engine(paths.db_path())
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        mt.reset()
        ss.forget_hardware()


async def _tasks(sm) -> list[Task]:
    async with sm() as s:
        return list((await s.execute(select(Task).order_by(Task.id))).scalars().all())


# ------------------------------------------------------------------------------------ parsing
def test_progress_sums_every_layer_seen_and_names_the_step():
    prog = mt.Progress(1, "pull", "qwen3:14b")
    assert prog.percent is None and mt.step_text(prog) == "Starting"
    mt.apply_line(prog, {"status": "pulling manifest"})
    assert mt.step_text(prog) == "Starting the download"
    mt.apply_line(prog, {"status": "pulling aaa", "digest": "sha256:aaa", "total": 800})
    assert prog.percent == 0 and mt.step_text(prog) == "Downloading"
    mt.apply_line(prog, {"status": "pulling aaa", "digest": "sha256:aaa", "total": 800,
                         "completed": 400})
    assert prog.percent == 50
    mt.apply_line(prog, {"status": "pulling bbb", "digest": "sha256:bbb", "total": 200,
                         "completed": 200})
    assert prog.percent == 60  # (400 + 200) of (800 + 200)
    mt.apply_line(prog, {"status": "verifying sha256 digest"})
    assert mt.step_text(prog) == "Checking the download" and prog.percent == 60
    mt.apply_line(prog, {"status": "success"})
    assert mt.step_text(prog) == "Done"
    mt.apply_line(prog, "not an object")  # ignored, never raises
    with pytest.raises(mt.OllamaApiError, match="pull model manifest: file does not exist"):
        mt.apply_line(prog, {"error": "pull model manifest: file does not exist"})


def test_create_body_maps_the_rendered_modelfile():
    text = profiles.render_modelfile("tier1", "gpu_16gb")
    body = mt.create_body("kestrel-tier1", text)
    assert body["model"] == "kestrel-tier1" and body["stream"] is True
    assert body["from"] == profiles.load_catalog().profile("gpu_16gb").models.for_tier("tier1")
    params = body["parameters"]
    assert isinstance(params["num_ctx"], int) and isinstance(params["seed"], int)
    assert isinstance(params["temperature"], float | int)
    assert body["modelfile"] == text  # for an Ollama older than the structured request
    stop = mt.create_body("m", 'FROM x\nPARAMETER stop "<a>"\nPARAMETER stop "<b>"\n')
    assert stop["parameters"]["stop"] == ["<a>", "<b>"]
    with pytest.raises(ValueError, match="no FROM line"):
        mt.create_body("m", "# nothing\nPARAMETER num_ctx 4\n")


# ---------------------------------------------------------------------------------- pull runs
async def test_pull_runs_to_done_and_reports_progress(sm):
    async with sm() as s:
        assert await mt.queue_pulls(s, ["qwen3:14b", "nomic-embed-text"]) == 2
        assert await mt.queue_pulls(s, ["qwen3:14b"]) == 0  # already queued: no duplicate
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{URL}/api/pull").mock(return_value=httpx.Response(200, text=PULL_OK))
        assert await mt.drain_once(sm, URL)
        assert await mt.drain_once(sm, URL)
        assert not await mt.drain_once(sm, URL)
    assert [json.loads(c.request.content)["model"] for c in route.calls] == [
        "qwen3:14b", "nomic-embed-text"
    ]
    assert [t.status for t in await _tasks(sm)] == ["done", "done"]
    async with sm() as s:
        snap = await mt.snapshot(s)
    assert snap["kind"] == "pull" and not snap["live"] and not snap["failed"]
    assert [(i["target"], i["step"], i["percent"]) for i in snap["items"]] == [
        ("qwen3:14b", "Done", 100), ("nomic-embed-text", "Done", 100)
    ]


async def test_snapshot_shows_the_running_percent(sm):
    async with sm() as s:
        await mt.queue_pulls(s, ["qwen3:14b"])
    from kestrel.queue.queue import TaskQueue

    task = await TaskQueue(sm).lease(mt.WORKER_ID, types=list(mt.TYPES))
    prog = mt.Progress(task.id, "pull", "qwen3:14b")
    mt.apply_line(prog, {"digest": "sha256:a", "total": 1000, "completed": 420})
    mt._progress[task.id] = prog
    async with sm() as s:
        snap = await mt.snapshot(s)
    assert snap["live"] and snap["items"][0]["percent"] == 42
    assert snap["items"][0]["step"] == "Downloading"


async def test_a_leased_row_left_by_a_dead_process_resumes_at_start(sm):
    async with sm() as s:
        await mt.queue_pulls(s, ["qwen3:14b"])
    from kestrel.queue.queue import TaskQueue

    await TaskQueue(sm).lease("a-process-that-died", types=list(mt.TYPES), lease_seconds=3600)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{URL}/api/pull").mock(return_value=httpx.Response(200, text=PULL_OK))
        # Still leased far into the future: nothing to take until the loop's start releases it.
        assert not await mt.drain_once(sm, URL)
        assert await mt.release_stale(sm) == 1
        assert await mt.drain_once(sm, URL)
    (t,) = await _tasks(sm)
    # The dead process's lease was given back at release: a restart is not a failure (finding 6).
    assert t.status == "done" and t.attempts == 1


async def test_a_stream_cut_off_mid_download_is_retried_and_continues(sm):
    async with sm() as s:
        await mt.queue_pulls(s, ["qwen3:14b"])
    cut = _lines(
        {"status": "pulling manifest"},
        {"status": "pulling aaa", "digest": "sha256:aaa", "total": 800, "completed": 300},
    )
    with respx.mock as mock:
        mock.post(f"{URL}/api/pull").mock(side_effect=[
            httpx.Response(200, text=cut), httpx.Response(200, text=PULL_OK)
        ])
        assert await mt.drain_once(sm, URL)
        (t,) = await _tasks(sm)
        assert t.status == "pending" and "before it finished" in (t.last_error or "")
        async with sm() as s:
            snap = await mt.snapshot(s)
        assert snap["live"] and snap["items"][0]["step"] == "Trying again shortly"
        assert await mt.drain_once(sm, URL)
    (t,) = await _tasks(sm)
    assert t.status == "done" and t.attempts == 2


async def test_ollama_down_fails_after_the_attempts_with_a_plain_reason(sm, monkeypatch):
    monkeypatch.setattr(mt, "MAX_ATTEMPTS", 2)
    async with sm() as s:
        await mt.queue_pulls(s, ["qwen3:14b"])
    with respx.mock as mock:
        mock.post(f"{URL}/api/pull").mock(side_effect=httpx.ConnectError("refused"))
        while await mt.drain_once(sm, URL):
            pass
    (t,) = await _tasks(sm)
    assert t.status == "dead" and "Ollama is not answering" in t.last_error
    async with sm() as s:
        snap = await mt.snapshot(s)
    assert snap["failed"] and "Open Ollama" in snap["items"][0]["error"]


async def test_an_error_object_in_the_stream_fails_the_task(sm, monkeypatch):
    monkeypatch.setattr(mt, "MAX_ATTEMPTS", 1)
    async with sm() as s:
        await mt.queue_pulls(s, ["no-such:model"])
    with respx.mock as mock:
        mock.post(f"{URL}/api/pull").mock(return_value=httpx.Response(
            200, text=_lines({"status": "pulling manifest"},
                             {"error": "pull model manifest: file does not exist"})
        ))
        assert await mt.drain_once(sm, URL)
    (t,) = await _tasks(sm)
    assert t.status == "dead" and "file does not exist" in t.last_error


# --------------------------------------------------------------------------------- cancelling
async def test_cancel_stops_queued_and_running_tasks(sm):
    async with sm() as s:
        await mt.queue_pulls(s, ["qwen3:14b", "nomic-embed-text"])
    db = str(paths.db_path())

    def cancel_mid_stream(request: httpx.Request) -> httpx.Response:
        # What cancel_all does from the request thread, while this stream is running.
        con = sqlite3.connect(db)
        try:
            ids = [r[0] for r in con.execute(
                "select id from task where status in ('pending','leased')")]
            con.execute("update task set status='cancelled' where status in ('pending','leased')")
            con.commit()
        finally:
            con.close()
        mt._cancelled.update(ids)
        return httpx.Response(200, text=PULL_OK)

    with respx.mock as mock:
        mock.post(f"{URL}/api/pull").mock(side_effect=cancel_mid_stream)
        assert await mt.drain_once(sm, URL)
        assert not await mt.drain_once(sm, URL)  # the second one was cancelled before it began
    assert [t.status for t in await _tasks(sm)] == ["cancelled", "cancelled"]


async def test_cancel_all_marks_rows_and_the_snapshot(sm):
    async with sm() as s:
        await mt.queue_pulls(s, ["qwen3:14b"])
        assert await mt.cancel_all(s) == 1
        assert await mt.cancel_all(s) == 0
        snap = await mt.snapshot(s)
    assert not snap["live"] and snap["items"][0]["step"] == "Cancelled"
    assert not await mt.drain_once(sm, URL)


# ------------------------------------------------------------------------------------- builds
async def test_builds_record_the_profile_only_when_every_one_is_done(sm, monkeypatch):
    async with sm() as s:
        assert await mt.queue_builds(s, "gpu_16gb", "test reason") == len(profiles.DERIVED_TIERS)
        assert await mt.queue_builds(s, "gpu_16gb") == 0  # one batch at a time
    ok = _lines({"status": "using already created layer sha256:1"}, {"status": "success"})
    with respx.mock as mock:
        route = mock.post(f"{URL}/api/create").mock(return_value=httpx.Response(200, text=ok))
        assert await mt.drain_once(sm, URL)
        assert profiles.load_active_profile() is None  # three still to build
        while await mt.drain_once(sm, URL):
            pass
    names = [json.loads(c.request.content)["model"] for c in route.calls]
    assert names == [profiles.derived_name(t) for t in profiles.DERIVED_TIERS]
    active = profiles.load_active_profile()
    assert active is not None and active.profile == "gpu_16gb" and active.reason == "test reason"


async def test_a_400_from_create_is_shown_with_ollamas_message(sm, monkeypatch):
    monkeypatch.setattr(mt, "MAX_ATTEMPTS", 1)
    async with sm() as s:
        await mt.queue_builds(s, "gpu_16gb")
    with respx.mock as mock:
        mock.post(f"{URL}/api/create").mock(return_value=httpx.Response(
            400, json={"error": "model 'qwen3:4b' not found"}
        ))
        assert await mt.drain_once(sm, URL)
    first = (await _tasks(sm))[0]
    assert first.status == "dead" and "model 'qwen3:4b' not found" in first.last_error
