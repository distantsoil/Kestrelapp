"""Restart-safe P5/P6 work for the Role page (D-UI-167)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx
import structlog
from sqlalchemy import JSON, exists, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.config import Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Opportunity, Org, Quarantine, Task
from kestrel.ingest.http import HttpClient
from kestrel.llm.client import LLMClient
from kestrel.pipeline._shared import plan_hosted_send
from kestrel.queue.queue import TaskQueue
from kestrel.research.company import research_org
from kestrel.research.contacts import discover_contacts
from kestrel.research.errors import ModelTransportError
from kestrel.research.excerpts import acquire_excerpts
from kestrel.services.setting_overrides import load_settings_with_db

log = structlog.get_logger()
TYPES = ("org_research", "org_contacts")
LIVE = ("pending", "leased")
LABELS = {"org_research": "Company research", "org_contacts": "Public contacts"}


def destination(settings: Settings) -> dict[str, Any]:
    tier = settings.llm.tiers["tier3"]
    backend = settings.llm.backends[tier.backend]
    return {
        "backend": tier.backend,
        "model": tier.model,
        "url": backend.base_url,
        "external": backend.external,
    }


async def enqueue(
    session: AsyncSession,
    opp: Opportunity,
    kind: str,
    settings: Settings,
    *,
    confirmed_online: bool = False,
) -> bool:
    task_type = TYPES[1] if kind == "contacts" else TYPES[0]
    if opp.org_id is None or await session.get(Org, opp.org_id) is None:
        raise ValueError("No company is linked to this role yet.")
    payload = {
        "opportunity_id": opp.id,
        "org_id": opp.org_id,
        "requested": True,
        "destination": destination(settings),
        "confirmed_online": confirmed_online,
        "progress": "Waiting for the worker",
    }
    now = utcnow_iso()
    live = exists().where(
        Task.type == task_type,
        Task.status.in_(LIVE),
        Task.payload["opportunity_id"].as_integer() == opp.id,
    )
    result = await session.execute(
        insert(Task)
        .from_select(
            [
                "type",
                "payload",
                "status",
                "priority",
                "attempts",
                "max_attempts",
                "created_at",
                "updated_at",
            ],
            select(
                literal(task_type),
                literal(payload, JSON),
                literal("pending"),
                literal(0),
                literal(0),
                literal(3),
                literal(now),
                literal(now),
            ).where(~live),
        )
        .returning(Task.id)
    )
    added = result.scalar_one_or_none() is not None
    await session.commit()
    return added


async def states(session: AsyncSession, opp_id: int) -> list[dict[str, Any]]:
    rows = (
        await session.scalars(
            select(Task)
            .where(
                Task.type.in_(TYPES),
                Task.payload["opportunity_id"].as_integer() == opp_id,
            )
            .order_by(Task.id.desc())
        )
    ).all()
    seen: set[str] = set()
    out = []
    for task in rows:
        if task.type in seen:
            continue
        seen.add(task.type)
        out.append(
            {
                "id": task.id,
                "label": LABELS[task.type],
                "status": task.status,
                "live": task.status in LIVE,
                "progress": (task.payload or {}).get("progress", "Waiting for the worker"),
            }
        )
    return out


async def cancel(session: AsyncSession, opp_id: int, task_id: int) -> bool:
    result = await session.execute(
        update(Task)
        .where(
            Task.id == task_id,
            Task.type.in_(TYPES),
            Task.status.in_(LIVE),
            Task.payload["opportunity_id"].as_integer() == opp_id,
        )
        .values(status="cancelled", worker_id=None, lease_until=None, updated_at=utcnow_iso())
    )
    await session.commit()
    return result.rowcount == 1


async def _progress(sm: async_sessionmaker[AsyncSession], task: Task, text: str) -> None:
    async with sm() as session:
        result = await session.execute(
            update(Task)
            .where(
                Task.id == task.id,
                Task.status == "leased",
                Task.worker_id == task.worker_id,
            )
            .values(payload={**task.payload, "progress": text}, updated_at=utcnow_iso())
        )
        await session.commit()
        if result.rowcount != 1:
            raise asyncio.CancelledError


async def _approval_error(
    session: AsyncSession,
    settings: Settings,
    payload: dict[str, Any],
) -> str | None:
    target = destination(settings)
    if payload.get("destination") != target:
        return "Model or service changed. Start again to confirm where this runs."
    if target["external"] and (
        not settings.llm.hosted_mode.active or not payload.get("confirmed_online")
    ):
        return "Online approval is needed. Start again to confirm the send."
    return (await plan_hosted_send(session, settings, "tier3")).refuse


async def _run(sm: async_sessionmaker[AsyncSession], task: Task) -> None:
    async with sm() as session:
        settings = await load_settings_with_db(session)
        payload = task.payload
        reason = await _approval_error(session, settings, payload)
        opp = await session.get(Opportunity, payload["opportunity_id"])
        org = await session.get(Org, payload["org_id"])
        if opp is None or org is None or opp.org_id != org.id:
            reason = "The role or its linked company changed. Start again."
        if reason:
            status, message = "held", reason
        else:
            await _progress(sm, task, "Reading public pages")
            async with (
                HttpClient(
                    user_agent=settings.ingest.user_agent,
                    default_rate_per_sec=settings.ingest.default_rate_per_sec,
                    timeout=settings.ingest.request_timeout_s,
                    respect_robots=settings.ingest.respect_robots,
                ) as http,
                httpx.AsyncClient() as llm_http,
            ):
                excerpts = await acquire_excerpts(session, http, org, settings=settings)
                await _progress(
                    sm,
                    task,
                    "Writing company research"
                    if task.type == TYPES[0]
                    else "Finding public contacts",
                )
                async with sm() as current_session:
                    settings = await load_settings_with_db(current_session)
                    reason = await _approval_error(current_session, settings, payload)
                if reason:
                    await session.rollback()
                    await _hold(sm, task, reason)
                    return
                client = LLMClient(settings, http=llm_http)
                if task.type == TYPES[0]:
                    record = await research_org(
                        session, client, org, settings=settings, excerpts=excerpts,
                        raise_on_transport=True,
                    )
                    message = (
                        "Too few public sources to rely on."
                        if record.insufficient_sources
                        else "Research complete."
                    )
                    if (record.summary_md or "").startswith("Research failed:"):
                        message = "The model reply needs another try. See Needs another try."
                else:
                    contacts = await discover_contacts(
                        session, client, org, excerpts, opportunity=opp, commit=False,
                        raise_on_transport=True,
                    )
                    message = f"Found {len(contacts)} verified public contact(s)."
            status = "done"
            if any(isinstance(row, Quarantine) for row in session.new):
                message = "The model reply needs another try. See Needs another try."
            await session.flush()
        result = await session.execute(
            update(Task)
            .where(
                Task.id == task.id,
                Task.status == "leased",
                Task.worker_id == task.worker_id,
            )
            .values(
                status=status,
                payload={**payload, "progress": message},
                worker_id=None,
                lease_until=None,
                updated_at=utcnow_iso(),
            )
        )
        if result.rowcount == 1:
            await session.commit()
        else:
            await session.rollback()


async def _hold(sm: async_sessionmaker[AsyncSession], task: Task, reason: str) -> None:
    async with sm() as session:
        await session.execute(
            update(Task)
            .where(
                Task.id == task.id,
                Task.status == "leased",
                Task.worker_id == task.worker_id,
            )
            .values(
                status="held",
                payload={**task.payload, "progress": reason},
                worker_id=None,
                lease_until=None,
                updated_at=utcnow_iso(),
            )
        )
        await session.commit()


async def drain(sm: async_sessionmaker[AsyncSession], *, stop: asyncio.Event | None = None) -> int:
    from kestrel.pipeline.worker import _TASK_LEASE_S, _renew_lease

    queue = TaskQueue(sm)
    worker_id = "pipeline-research"
    done = 0
    while stop is None or not stop.is_set():
        task = await queue.lease(worker_id, types=list(TYPES), lease_seconds=_TASK_LEASE_S)
        if task is None:
            break
        beat = asyncio.create_task(_renew_lease(queue, task.id, worker_id))
        run = asyncio.create_task(_run(sm, task))
        try:
            while not run.done():
                await asyncio.wait({run}, timeout=0.25)
                current = await queue.get(task.id)
                if (
                    (stop is not None and stop.is_set())
                    or current is None
                    or (current.status != "leased" or current.worker_id != worker_id)
                ):
                    run.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await run
                    await queue.defer(
                        task.id, "Stopped; resumes at the next start", worker_id=worker_id
                    )
                    break
            if not run.cancelled():
                await run
                done += 1
        except asyncio.CancelledError:
            run.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run
            await queue.defer(task.id, "Stopped; resumes at the next start", worker_id=worker_id)
            raise
        except ModelTransportError as exc:
            # A transport failure is neither a validated empty result nor a schema quarantine:
            # it goes back to the queue so the retry budget applies (D-UI-167).
            log.warning("research_task_transport_failed", task=task.id, retryable=exc.retryable)
            await queue.fail(
                task.id,
                "The model could not be reached. Kestrel will try again; check Models if it "
                "keeps failing.",
                worker_id=worker_id,
                retry=exc.retryable,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("research_task_failed", task=task.id, error_type=type(exc).__name__)
            await queue.fail(
                task.id,
                "Research could not finish. Start again if it keeps failing.",
                worker_id=worker_id,
            )
        finally:
            if not run.done():
                run.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await run
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
    return done
