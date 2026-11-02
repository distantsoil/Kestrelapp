"""Durable local sorting-model training, from Calibrate or after 50 more rated roles."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict

import structlog
from sqlalchemy import JSON, func, insert, literal, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from kestrel.db.models import MlModel, Task
from kestrel.errors import failure_record, shown_failure
from kestrel.learning import reranker
from kestrel.learning.capture import labeled_examples
from kestrel.queue.queue import TaskQueue
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.services.tracks import load_enabled

log = structlog.get_logger()
TASK_TYPE = "train_reranker"
RETRY_RATINGS = 50


def _for_track(track: str) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    return (Task.type == TASK_TYPE, func.json_extract(Task.payload, "$.track") == track)


async def state(session: AsyncSession, track: str) -> dict:
    task = (await session.execute(select(Task).where(*_for_track(track))
                                  .order_by(Task.id.desc()).limit(1))).scalar_one_or_none()
    if task is None:
        return {"pending": False, "message": ""}
    if task.status in ("pending", "leased"):
        return {"pending": True, "message": "Training queued or running on this computer."}
    if task.status == "dead":
        return {"pending": False, "message": "Training could not finish. Try again.",
                "detail": shown_failure(task.last_error)}
    result = (task.payload or {}).get("result", {})
    return {"pending": False, "message": (
        "This training attempt passed the checks and was selected for scoring."
        if result.get("promoted") else
        "Training finished. This attempt did not replace your current scoring."
    ), "detail": result.get("reason")}


async def request_training(session: AsyncSession, track: str, *, automatic: bool = False) -> str:
    settings = await load_settings_with_db(session)
    if not settings.learning.build_model:
        return "Nothing queued. Turn on your sorting model in Settings > Scoring and learning."
    n = len(await labeled_examples(session, track))
    if n < settings.learning.min_labels_for_reranker:
        return (f"Nothing queued. Rate {settings.learning.min_labels_for_reranker - n} more roles "
                f"first ({n} ratings so far).")
    latest = (await session.execute(select(Task).where(*_for_track(track))
                                    .order_by(Task.id.desc()).limit(1))).scalar_one_or_none()
    if latest is not None and latest.status in ("pending", "leased"):
        return "Training is already queued or running."
    if automatic:
        active = (await session.execute(select(MlModel.id).where(
            MlModel.track == track, MlModel.kind == "reranker", MlModel.is_active.is_(True),
        ).limit(1))).scalar_one_or_none()
        if active is not None:
            return "Your sorting model is already in use."
        previous = (await session.execute(select(MlModel.n_samples).where(
            MlModel.track == track, MlModel.kind == "reranker",
        ).order_by(MlModel.id.desc()).limit(1))).scalar_one_or_none()
        last_n = max(previous or 0, (latest.payload or {}).get("ratings", 0) if latest else 0)
        if (latest is not None or previous is not None) and n < last_n + RETRY_RATINGS:
            return "Waiting for 50 more rated roles."
    # One INSERT statement guards concurrent clicks/scheduler requests without a new table.
    exists = select(Task.id).where(*_for_track(track), Task.status.in_(("pending", "leased")))
    payload = {"track": track, "ratings": n, "automatic": automatic}
    result = await session.execute(insert(Task).from_select(
        ["type", "payload"],
        select(literal(TASK_TYPE), literal(payload, type_=JSON)).where(~exists.exists()),
    ))
    await session.commit()
    return ("Training queued on this computer." if result.rowcount else
            "Training is already queued or running.")


async def _heartbeat(queue: TaskQueue, task_id: int, worker_id: str) -> None:
    while True:
        await asyncio.sleep(60)
        try:
            if not await queue.heartbeat(task_id, worker_id, lease_seconds=600):
                return
        except Exception as exc:
            log.warning("sorting_model_heartbeat_failed", task=task_id, error=str(exc))


async def run_cycle(
    sm: async_sessionmaker[AsyncSession], *, stop: asyncio.Event | None = None,
) -> int:
    """Called by the desktop worker. Re-check the switch at execution; never use a hosted model."""
    async with sm() as s:
        for track in await load_enabled(s):
            await request_training(s, track, automatic=True)
    queue = TaskQueue(sm)
    done = 0
    while stop is None or not stop.is_set():
        task = await queue.lease("sorting-model", types=[TASK_TYPE], lease_seconds=600)
        if task is None:
            break
        beat = asyncio.create_task(_heartbeat(queue, task.id, "sorting-model"))
        try:
            async with sm() as s:
                row = await s.get(Task, task.id)
                # A crash after saving the result but before completing the lease needs no fit.
                if "result" not in row.payload:
                    settings = await load_settings_with_db(s)
                    n = len(await labeled_examples(s, row.payload["track"]))
                    result = await reranker.train_reranker(s, row.payload["track"], settings)
                    row.payload = {**row.payload, "ratings": n, "result": asdict(result)}
                    await s.commit()
            await queue.complete(task.id, worker_id="sorting-model")
            done += 1
        except Exception as exc:
            log.warning("sorting_model_training_failed", task=task.id, error=str(exc))
            await queue.fail(task.id, failure_record(exc), worker_id="sorting-model", retry=True)
            break  # retry next cycle, never spin through all attempts on one transient failure
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
    return done
