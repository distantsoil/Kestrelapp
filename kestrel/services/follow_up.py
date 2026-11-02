"""Durable follow-up drafts (P17). The operator reviews, copies and delivers the text."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from datetime import date, timedelta
from typing import Any

import structlog
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.config import Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Opportunity, Org, Pursuit, Task
from kestrel.docs.current_employer import load_employer_map
from kestrel.llm.plain_repair import enforce
from kestrel.llm.prompts import load_prompt
from kestrel.llm.service_name import service_name
from kestrel.pipeline._shared import run_prompt_hosted
from kestrel.queue.queue import TaskQueue
from kestrel.services import pursuit_board
from kestrel.services.setting_overrides import load_settings_with_db

TASK_TYPE = "follow_up"
PROMPT = "follow_up/v2"
TIER = "tier3"
log = structlog.get_logger()


class DraftRequest(BaseModel):
    route_stamp: str
    facts_stamp: str
    model: str = ""
    confirmed: str = ""


def stamp(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def eligible(pursuit: Pursuit) -> bool:
    due = pursuit_board.parse_day(pursuit.next_action_at)
    action = (pursuit.next_action or "").lower().replace("-", " ")
    return bool(
        not pursuit.closed_at
        and pursuit.status not in pursuit_board.CLOSED_STATUSES[pursuit.track]
        and action.startswith("follow up")
        and due and due <= date.today() + timedelta(days=1)
    )


async def facts_for(session: AsyncSession, pursuit: Pursuit) -> dict[str, Any]:
    opp = await session.get(Opportunity, pursuit.opportunity_id)
    org = await session.get(Org, opp.org_id) if opp and opp.org_id else None
    return {
        "track": pursuit.track,
        "role": (opp.title or "")[:240] if opp else "",
        "organization": ((org.name if org else opp.org_name) or "")[:240] if opp else "",
        "stage": pursuit_board.stage_label(pursuit.status),
        "applied_date": (pursuit.applied_at or "")[:10],
        "last_activity_date": (pursuit.last_activity_at or "")[:10],
        "next_action": (pursuit.next_action or "")[:120],
        "due_date": (pursuit.next_action_at or "")[:10],
    }


def routing(settings: Settings) -> dict[str, Any]:
    tier = settings.llm.tiers[TIER]
    backend = settings.llm.backends[tier.backend]
    return {
        "backend": tier.backend, "model": tier.model, "url": backend.base_url,
        "protocol": backend.protocol, "online": backend.external,
    }


async def send_problem(session: AsyncSession, settings: Settings) -> str | None:
    if not routing(settings)["online"]:
        return None
    if not settings.llm.hosted_mode.active:
        return "Nothing was sent. Turn on Hosted mode and accept its disclosure first."
    if settings.confidential_mode and not await load_employer_map(session):
        return (
            "Nothing was sent. Confidential mode is on. Choose your current employer "
            "in Settings before using an online model."
        )
    return None


async def disclosure(session: AsyncSession, settings: Settings) -> dict[str, Any]:
    route = routing(settings)
    return {
        **route, "stamp": stamp(route), "provider": service_name(route["url"]),
        "sends": settings.llm.hosted_mode.disclosure_terms()["sends"][TIER],
        "problem": await send_problem(session, settings),
        "hidden": settings.confidential_mode and route["online"],
    }


async def latest(session: AsyncSession, pursuit_id: int) -> Task | None:
    return (await session.execute(
        select(Task).where(
            Task.type == TASK_TYPE,
            func.json_extract(Task.payload, "$.pursuit_id") == pursuit_id,
        ).order_by(Task.id.desc()).limit(1)
    )).scalar_one_or_none()


async def request_draft(
    session: AsyncSession, pursuit: Pursuit, settings: Settings, form: DraftRequest,
) -> str | None:
    if not eligible(pursuit):
        return "This application no longer has a due follow-up."
    route = routing(settings)
    facts = await facts_for(session, pursuit)
    if form.route_stamp != stamp(route) or form.facts_stamp != stamp(facts):
        return "Nothing was sent. The facts or model changed. Reopen the draft and confirm again."
    if route["online"]:
        if form.confirmed != "1" or form.model != route["model"]:
            return "Nothing was sent. Confirm the online draft first by ticking the box."
        if problem := await send_problem(session, settings):
            return problem
    previous = await latest(session, pursuit.id)
    if previous is not None:
        payload = previous.payload or {}
        if previous.status in ("pending", "leased") or (
            payload.get("facts") == facts and payload.get("route") == route
            and payload.get("result")
        ):
            return None
    session.add(Task(type=TASK_TYPE, payload={
        "pursuit_id": pursuit.id, "opportunity_id": pursuit.opportunity_id,
        "requested": True, "facts": facts, "route": route,
        "confirmed": form.confirmed == "1", "model": form.model,
        "prompt_version": PROMPT,
    }))
    await session.flush()
    return None


async def _write(session: AsyncSession, client: Any, task: Task) -> dict[str, Any]:
    from kestrel.pipeline._shared import make_quarantine_hook

    payload = task.payload or {}
    if payload.get("result") or payload.get("error"):
        return {key: payload[key] for key in ("result", "error") if key in payload}
    pursuit = await session.get(Pursuit, payload["pursuit_id"])
    if pursuit is None or not eligible(pursuit):
        return {"error": "This application no longer has a due follow-up."}
    settings = client.settings
    route = routing(settings)
    if route != payload["route"] or await facts_for(session, pursuit) != payload["facts"]:
        return {"error": "The facts or model changed. Reopen the draft and confirm again."}
    if route["online"]:
        if not payload.get("confirmed") or payload.get("model") != route["model"]:
            return {"error": "Confirm the online draft again. Nothing was sent."}
        if problem := await send_problem(session, settings):
            return {"error": problem}
        settings.llm.hosted_mode.redact_confidential = True
    # A task queued before the plain-writing rules (follow_up/v1) is written with the current
    # prompt; the stored version only names what the task was created with.
    spec = load_prompt(PROMPT)
    messages = [
        {"role": "system", "content": spec.system},
        {"role": "user", "content": spec.user.replace(
            "{PURSUIT_FACTS}", json.dumps(payload["facts"], ensure_ascii=False))},
    ]
    hook = make_quarantine_hook(session, tier=17, opportunity_id=pursuit.opportunity_id)

    async def run(msgs: list[dict], reask: bool = False) -> Any:
        return await run_prompt_hosted(
            session, client, spec, msgs, quarantine_tier=17,
            opportunity_id=pursuit.opportunity_id,
            on_quarantine=None if reask else hook,
        )

    result = await run(messages)
    if result.quarantined:
        return {"error": "The reply could not be validated. It was set aside in Needs another try."}
    if not result.ok or result.value is None:
        raise RuntimeError("The model could not write this draft. Check Models, then try again.")
    plain = await enforce(spec, result.value, messages=messages, run=run)
    return {"result": plain.value.model_dump(), "style_flags": plain.flags}


async def drain(
    sm: async_sessionmaker[AsyncSession], make_client: Any, *, stop: Any = None,
) -> int:
    from kestrel.pipeline.worker import _renew_lease

    queue = TaskQueue(sm)
    worker_id = "follow-up-drafts"
    count = 0
    while stop is None or not stop.is_set():
        task = await queue.lease(worker_id, types=[TASK_TYPE], lease_seconds=900)
        if task is None:
            break
        beat = asyncio.create_task(_renew_lease(queue, task.id, worker_id))
        try:
            async with sm() as session:
                settings = await load_settings_with_db(session)
                result = await _write(session, make_client(settings.model_copy(deep=True)), task)
                await session.execute(update(Task).where(
                    Task.id == task.id, Task.status == "leased", Task.worker_id == worker_id,
                ).values(
                    payload={**task.payload, **result}, status="done", updated_at=utcnow_iso(),
                ))
                await session.commit()
            count += 1
        except Exception as exc:
            log.warning("follow_up_failed", task_id=task.id, error_type=type(exc).__name__)
            await queue.fail(task.id, "The draft could not be written. Check Models and try again.",
                             worker_id=worker_id, retry=True)
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
    return count
