"""Model downloads and derived-model builds as task rows (release round R1, hard rule 5).

The Setup steps (Settings > System and the onboarding model step) used to shell out to `ollama
pull` and `ollama create` on a thread, which a user without a terminal could not follow and a
restart lost. Each model is now one `task` row: `ollama_pull` (payload {"tag"}) or `ollama_create`
(payload {"name", "tier", "modelfile", "profile", "reason"}), every row of one button press sharing
a `batch` id. A loop on uvicorn's event loop (run_loop, started from the FastAPI lifespan) leases
them one at a time and talks to Ollama's own HTTP API:

* POST /api/pull with stream on: one JSON object per line, `status` plus `total` and `completed`
  for each layer while it downloads. The percent shown is the sum over the layers seen so far.
  Ollama resumes a cancelled or interrupted pull from where it stopped, so a task re-leased after
  a restart continues the download rather than starting over.
* POST /api/create with stream on, built from the Modelfile Kestrel already renders
  (profiles.render_modelfile): FROM becomes `from`, each PARAMETER a `parameters` entry. The raw
  text also goes in `modelfile` for an Ollama older than the structured create request (Go's JSON
  decoder ignores fields it does not know, so a current one is unaffected).

Resumable: the lease is renewed while a task runs, and a leased row left by a killed process goes
back to pending when the loop starts (only this process runs these tasks: the desktop app is
single instance), and that clean release gives back the attempt its lease took, so restarts never
use up a download's attempts; only a real failure counts (release round review, finding 6).
Cancellable: cancel_all() marks every live row `cancelled` and sets the running stream's cancel
event, which closes the connection at once, even while Ollama sends nothing (verifying a big
layer, building); the page reads "Stopping…" until the stream has really closed (release round
review, finding 5). Progress lives in memory (it is a display, rebuilt by the next
stream line after a restart); the task rows are the durable state.

Kestrel still never installs Ollama: these calls go to the Ollama the user already runs at the
configured address, which is this computer by default and may be another host on their network
(then the Setup page says "Ollama at <host>" and the buttons "Download on <host>"). Hard rule 1 is
about third parties; nothing here goes anywhere but that Ollama, plus Ollama's own download from
its library, which the user asked for by pressing the button."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Task
from kestrel.llm import profiles

log = structlog.get_logger()

PULL_TYPE = "ollama_pull"
CREATE_TYPE = "ollama_create"
TYPES: tuple[str, ...] = (PULL_TYPE, CREATE_TYPE)
KIND = {PULL_TYPE: "pull", CREATE_TYPE: "build"}
LIVE = ("pending", "leased")
WORKER_ID = "setup-models"
MAX_ATTEMPTS = 5
LEASE_S = 90
RENEW_S = 20
IDLE_POLL_S = 1.0
# A big layer can sit between progress lines for a while on a slow link; a read that waits this
# long with nothing at all is a stalled connection, retried as a failure.
STREAM_TIMEOUT = httpx.Timeout(30.0, read=600.0)
RETRY_BACKOFF_S = 3.0
CANCELLED_NOTE = "cancelled by the user"


class OllamaApiError(RuntimeError):
    """Ollama answered with an `error` object in the stream, or a non-2xx status."""


class _Stopped(Exception):
    """The user cancelled this task while it ran."""


# ================================================================================== progress
@dataclass
class Progress:
    task_id: int
    kind: str  # pull | build
    target: str  # the tag being pulled or the derived model being built
    status: str = "starting"
    layers: dict[str, tuple[int, int]] = field(default_factory=dict)  # digest -> (done, total)
    error: str = ""

    @property
    def percent(self) -> int | None:
        total = sum(t for _, t in self.layers.values())
        if total <= 0:
            return None
        done = sum(min(c, t) for c, t in self.layers.values())
        return max(0, min(100, int(done * 100 / total)))


_progress: dict[int, Progress] = {}
_cancelled: set[int] = set()
# Per running task: set by cancel_all so the stream closes now, not at Ollama's next line.
_cancel_events: dict[int, asyncio.Event] = {}
# Tasks whose stream is open right now (a cancelled one here is still "Stopping…").
_running: set[int] = set()


def reset() -> None:
    """Forget in-memory progress; the test suite calls this between tests."""
    _progress.clear()
    _cancelled.clear()
    _cancel_events.clear()
    _running.clear()


def _cancel_event(task_id: int) -> asyncio.Event:
    ev = _cancel_events.get(task_id)
    if ev is None:
        ev = _cancel_events[task_id] = asyncio.Event()
    if task_id in _cancelled:
        ev.set()
    return ev


def apply_line(prog: Progress, obj: dict[str, Any]) -> None:
    """Fold one stream object into the progress. An `error` object raises OllamaApiError."""
    if not isinstance(obj, dict):
        return
    if obj.get("error"):
        raise OllamaApiError(str(obj["error"])[:500])
    status = str(obj.get("status") or "").strip()
    digest = str(obj.get("digest") or "")
    total = obj.get("total")
    if digest and isinstance(total, int | float) and total > 0:
        completed = obj.get("completed")
        done = int(completed) if isinstance(completed, int | float) else 0
        prog.layers[digest] = (done, int(total))
        status = "downloading"
    if status:
        prog.status = status


def step_text(prog: Progress) -> str:
    """The current step in plain words for the page."""
    s = prog.status
    if s == "downloading":
        return "Downloading"
    if s.startswith("pulling manifest"):
        return "Starting the download"
    if s.startswith("verifying"):
        return "Checking the download"
    if s.startswith("writing manifest") or s.startswith("removing"):
        return "Finishing"
    if s == "success":
        return "Done"
    if s in ("starting", ""):
        return "Starting"
    return s[:1].upper() + s[1:80]


# ============================================================================ modelfile → body
def _value(raw: str) -> int | float | str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1]
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def create_body(name: str, modelfile: str) -> dict[str, Any]:
    """The POST /api/create body for a rendered Modelfile. Only the directives Kestrel renders
    (FROM, PARAMETER, single-line SYSTEM and TEMPLATE) are mapped; a repeated parameter (stop)
    becomes a list. Raises ValueError when there is no FROM line."""
    body: dict[str, Any] = {"model": name, "stream": True}
    params: dict[str, Any] = {}
    for line in modelfile.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        head, _, rest = text.partition(" ")
        word = head.upper()
        if word == "FROM":
            body["from"] = rest.strip()
        elif word == "PARAMETER":
            key, _, raw = rest.strip().partition(" ")
            val = _value(raw)
            if key in params:
                prev = params[key]
                params[key] = [*prev, val] if isinstance(prev, list) else [prev, val]
            else:
                params[key] = val
        elif word in ("SYSTEM", "TEMPLATE"):
            body[word.lower()] = str(_value(rest))
    if not body.get("from"):
        raise ValueError(f"the Modelfile for {name} has no FROM line")
    if params:
        body["parameters"] = params
    body["modelfile"] = modelfile  # an Ollama before the structured create request reads this
    return body


# =================================================================================== enqueue
async def live_tasks(session: AsyncSession) -> list[Task]:
    rows = await session.execute(
        select(Task).where(Task.type.in_(TYPES), Task.status.in_(LIVE)).order_by(Task.id)
    )
    return list(rows.scalars().all())


async def queue_pulls(session: AsyncSession, tags: list[str]) -> int:
    """One pull task per tag that has no live task yet. Commits. Returns how many were queued."""
    live = {str((t.payload or {}).get("tag")) for t in await live_tasks(session)
            if t.type == PULL_TYPE}
    batch = uuid.uuid4().hex[:12]
    n = 0
    for tag in tags:
        if tag in live:
            continue
        session.add(Task(type=PULL_TYPE, payload={"tag": tag, "batch": batch},
                         max_attempts=MAX_ATTEMPTS))
        n += 1
    await session.commit()
    if n:
        log.info("setup_pulls_queued", count=n)
    return n


async def queue_builds(session: AsyncSession, profile: str, reason: str = "") -> int:
    """One create task per derived model, carrying the Modelfile text rendered now. The same
    files are written to the models folder so the commands under Show details stay true. The
    profile is recorded once every task of the batch is done. Commits."""
    if any(t.type == CREATE_TYPE for t in await live_tasks(session)):
        return 0
    from kestrel import setup as setup_cli

    dest = setup_cli.modelfiles_dir()
    await asyncio.to_thread(profiles.write_modelfiles, profile, dest)
    batch = uuid.uuid4().hex[:12]
    for tier in profiles.TIERS:
        session.add(Task(
            type=CREATE_TYPE,
            payload={
                "name": profiles.derived_name(tier), "tier": tier,
                "modelfile": profiles.render_modelfile(tier, profile),
                "profile": profile, "reason": reason, "batch": batch,
            },
            max_attempts=MAX_ATTEMPTS,
        ))
    await session.commit()
    log.info("setup_builds_queued", count=len(profiles.TIERS), profile=profile)
    return len(profiles.TIERS)


async def cancel_all(session: AsyncSession) -> int:
    """Mark every live pull and build cancelled; a running one stops at its next stream line.
    Commits. Returns how many rows were cancelled."""
    ids = [t.id for t in await live_tasks(session)]
    if not ids:
        return 0
    await session.execute(
        update(Task)
        .where(Task.id.in_(ids), Task.status.in_(LIVE))
        .values(status="cancelled", last_error=CANCELLED_NOTE, worker_id=None,
                lease_until=None, updated_at=utcnow_iso())
    )
    await session.commit()
    _cancelled.update(ids)
    for tid in ids:
        ev = _cancel_events.get(tid)
        if ev is not None:
            ev.set()  # the running stream closes its connection now
    log.info("setup_tasks_cancelled", count=len(ids))
    return len(ids)


def stopping() -> bool:
    """True while a cancelled task's stream has not closed yet."""
    return any(tid in _cancelled for tid in _running)


async def release_stale(sm: async_sessionmaker[AsyncSession]) -> int:
    """At loop start: a leased row here was left by a process that is gone (the app is single
    instance), so put it back to pending now instead of waiting for its lease to run out. The
    attempt that lease took is given back: a restart is not a failure (finding 6)."""
    from kestrel.queue.queue import _refunded_attempts

    async with sm() as s:
        res = await s.execute(
            update(Task)
            .where(Task.type.in_(TYPES), Task.status == "leased")
            .values(status="pending", worker_id=None, lease_until=None,
                    attempts=_refunded_attempts(), updated_at=utcnow_iso())
        )
        await s.commit()
        return res.rowcount or 0


# ================================================================================== snapshot
def _row_view(t: Task) -> dict[str, Any]:
    p = t.payload or {}
    target = str(p.get("tag") or p.get("name") or "")
    prog = _progress.get(t.id)
    view: dict[str, Any] = {
        "id": t.id, "kind": KIND.get(t.type, ""), "target": target, "status": t.status,
        "percent": None, "step": "", "error": "",
    }
    if t.status == "pending":
        view["step"] = "Waiting" if not t.attempts else "Trying again shortly"
        if t.attempts and t.last_error:
            view["error"] = t.last_error[:300]
    elif t.status == "leased":
        view["step"] = step_text(prog) if prog else "Starting"
        view["percent"] = prog.percent if prog else None
    elif t.status == "done":
        view["step"], view["percent"] = "Done", 100
    elif t.status == "cancelled":
        view["step"] = "Stopping…" if t.id in _running else "Cancelled"
    else:  # dead or failed
        view["step"] = "Failed"
        view["error"] = (t.last_error or "it stopped before it finished")[:300]
    return view


async def snapshot(session: AsyncSession) -> dict[str, Any]:
    """What the page shows: the newest batch's rows, whether any task is live, and which kind."""
    newest = (
        await session.execute(
            select(Task).where(Task.type.in_(TYPES)).order_by(Task.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if newest is None:
        return {"live": False, "kind": None, "items": [], "failed": False, "stopping": False}
    live = await live_tasks(session)
    if live:
        first = live[0]
        batch = (first.payload or {}).get("batch")
        kind = KIND.get(first.type)
    else:
        batch = (newest.payload or {}).get("batch")
        kind = KIND.get(newest.type)
    rows = (
        await session.execute(
            select(Task).where(Task.type.in_(TYPES)).order_by(Task.id.desc()).limit(40)
        )
    ).scalars().all()
    items = [_row_view(t) for t in sorted(rows, key=lambda t: t.id)
             if (t.payload or {}).get("batch") == batch]
    halting = stopping()
    return {
        # Still live while a cancelled stream is closing, so the page keeps polling.
        "live": bool(live) or halting,
        "kind": kind,
        "items": items,
        "failed": any(i["status"] in ("dead", "failed") for i in items),
        "stopping": halting,
    }


# ===================================================================================== drain
async def _stream(
    http: httpx.AsyncClient, url: str, body: dict[str, Any], prog: Progress, task_id: int
) -> None:
    """Run the stream beside the task's cancel event. Cancel cancels the reading task, which
    leaves `http.stream`'s context and closes the response at once: no waiting for Ollama's
    next line, which can be minutes away (release round review, finding 5)."""
    stop = _cancel_event(task_id)
    if stop.is_set():
        raise _Stopped
    work = asyncio.create_task(_stream_lines(http, url, body, prog, task_id))
    waiter = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({work, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (work, waiter):
            if not t.done():
                t.cancel()
        for t in (work, waiter):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
    if work.cancelled() or stop.is_set():
        raise _Stopped
    work.result()  # re-raises the stream's own error


async def _stream_lines(
    http: httpx.AsyncClient, url: str, body: dict[str, Any], prog: Progress, task_id: int
) -> None:
    async with http.stream("POST", url, json=body, timeout=STREAM_TIMEOUT) as r:
        if r.status_code >= 400:
            text = (await r.aread()).decode("utf-8", "replace")
            try:
                msg = str(json.loads(text).get("error") or text)
            except (ValueError, AttributeError):
                msg = text
            raise OllamaApiError(f"Ollama answered {r.status_code}: {msg[:300]}")
        saw_success = False
        async for line in r.aiter_lines():
            if task_id in _cancelled:
                raise _Stopped
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                log.debug("setup_stream_unparsed", task=task_id, line=line[:120])
                continue
            apply_line(prog, obj)
            if isinstance(obj, dict) and obj.get("status") == "success":
                saw_success = True
        if task_id in _cancelled:
            raise _Stopped
        if not saw_success:
            raise OllamaApiError("Ollama closed the connection before it finished")


async def _renew(sm: async_sessionmaker[AsyncSession], task_id: int) -> None:
    from kestrel.queue.queue import TaskQueue

    queue = TaskQueue(sm)
    while True:
        await asyncio.sleep(RENEW_S)
        try:
            if not await queue.heartbeat(task_id, WORKER_ID, lease_seconds=LEASE_S):
                return
        except Exception as exc:  # noqa: BLE001 - a missed renewal must not kill the download
            log.warning("setup_task_renew_failed", task=task_id, error=str(exc))


async def _record_profile_if_batch_done(
    sm: async_sessionmaker[AsyncSession], payload: dict[str, Any]
) -> bool:
    batch = payload.get("batch")
    async with sm() as s:
        rows = (await s.execute(select(Task).where(Task.type == CREATE_TYPE))).scalars().all()
    mine = [t for t in rows if (t.payload or {}).get("batch") == batch]
    if not mine or any(t.status != "done" for t in mine):
        return False
    from kestrel.services import setup_state as ss

    profile = str(payload.get("profile") or "")
    reason = str(payload.get("reason") or "") or "chosen on the Setup page"
    await asyncio.to_thread(
        profiles.save_active_profile, profile, reason, ss.detected_hardware()
    )
    log.info("setup_profile_recorded", profile=profile)
    return True


async def drain_once(
    sm: async_sessionmaker[AsyncSession], native_url: str, *, http: httpx.AsyncClient | None = None
) -> bool:
    """Lease and run one pull or build. Returns False when there was nothing to do. Never
    raises for a failed download: the row goes back to the queue while attempts remain, then
    dead with the reason, which the page shows beside a Try again button."""
    from kestrel.queue.queue import TaskQueue
    from kestrel.services import setup_state as ss

    queue = TaskQueue(sm)
    task = await queue.lease(WORKER_ID, types=list(TYPES), lease_seconds=LEASE_S)
    if task is None:
        return False
    payload = dict(task.payload or {})
    kind = KIND.get(task.type, "")
    target = str(payload.get("tag") or payload.get("name") or "")
    prog = Progress(task.id, kind, target)
    _progress[task.id] = prog
    _running.add(task.id)
    base = native_url.rstrip("/")
    own = http is None
    client = http or httpx.AsyncClient()
    beat = asyncio.create_task(_renew(sm, task.id))
    try:
        if task.type == PULL_TYPE:
            await _stream(client, f"{base}/api/pull", {"model": target, "stream": True},
                          prog, task.id)
        else:
            body = create_body(target, str(payload.get("modelfile") or ""))
            await _stream(client, f"{base}/api/create", body, prog, task.id)
    except _Stopped:
        prog.status = "cancelled"
        log.info("setup_task_stopped", task=task.id, kind=kind, target=target)
        return True
    except (httpx.HTTPError, OllamaApiError, ValueError, OSError) as exc:
        reason = _reason(exc, base)
        prog.error = reason
        status = await queue.fail(task.id, reason, worker_id=WORKER_ID, retry=True)
        log.warning("setup_task_failed", task=task.id, kind=kind, target=target,
                    error=reason, now=status)
        if status == "pending":
            await asyncio.sleep(RETRY_BACKOFF_S * max(1, task.attempts))
        return True
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
        _cancelled.discard(task.id)
        _cancel_events.pop(task.id, None)
        _running.discard(task.id)
        if own:
            await client.aclose()
        ss.forget_ollama_probe()  # the tags changed; the next page render asks again
    if await queue.complete(task.id, worker_id=WORKER_ID):
        prog.status = "success"
        log.info("setup_task_done", task=task.id, kind=kind, target=target)
        if task.type == CREATE_TYPE:
            await _record_profile_if_batch_done(sm, payload)
    return True


def _reason(exc: Exception, base: str) -> str:
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
        return f"Ollama is not answering at {base}. Open Ollama, then press Try again."
    if isinstance(exc, httpx.TimeoutException):
        return "The download stalled (no data for ten minutes). Press Try again to continue it."
    if isinstance(exc, OllamaApiError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"[:500]


async def run_loop(
    stop: asyncio.Event,
    sm: async_sessionmaker[AsyncSession],
    native_url: Callable[[], Awaitable[str]],
) -> None:
    """Drain pulls and builds on uvicorn's loop until `stop` is set. `native_url` is read fresh
    for each task so a changed Ollama address applies without a restart."""
    try:
        released = await release_stale(sm)
        if released:
            log.info("setup_tasks_resumed", count=released)
    except Exception as exc:  # noqa: BLE001 - a locked database at launch: the leases expire
        log.warning("setup_tasks_release_failed", error=str(exc))
    async with httpx.AsyncClient() as http:
        while not stop.is_set():
            try:
                did = await drain_once(sm, await native_url(), http=http)
            except Exception as exc:  # noqa: BLE001 - the loop must outlive one bad cycle
                log.warning("setup_task_loop_error", error=f"{type(exc).__name__}: {exc}")
                did = False
            if not did:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=IDLE_POLL_S)
