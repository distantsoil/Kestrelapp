"""Suggested target titles (P15, D-TTL-3, #31).

After a resume import, about once a week, and after every 10 roles newly marked applied, a queued
`title_suggestions` task asks the Tier 2 model which job titles the operator's own document
supports that are not already in their target titles. The answers are stored as `suggested` rows
for the operator to accept or dismiss. **Nothing is added to target_titles without an explicit
Accept**, and a title that was dismissed (or accepted, or already shown) is never stored again:
the table's case-insensitive unique index remembers every title it has shown.

The model suggests; it never writes resume text. The evidence stored beside a title is the
operator's own bullet, copied here from the slot the model named. The slot keys are an enum of the
document's real keys, generated per call (the P3 and P13 pattern), so an invented key is
structurally impossible on a constrained backend and a validation failure elsewhere: one repair,
then a quarantine row with the raw reply kept, never a silent default.

Resumable like every task (hard rule 5): the lease is renewed while a call runs, and a killed
worker's task is reclaimed once the lease expires."""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import structlog
from pydantic import BaseModel, create_model
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.config import Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import (
    DocMaster,
    Opportunity,
    Pursuit,
    Setting,
    Task,
    TitleSuggestion,
)
from kestrel.llm.schemas import P15TitleCore, P15TitleSuggestions, _cap, _dedupe_cap, _max_items

log = structlog.get_logger()

TASK_TYPE = "title_suggestions"
PROMPT = "title_suggest/v1"
QUARANTINE_TIER = 15  # the quarantine row's `tier` names the prompt, as P8, P9, P13 and P14 do
MAX_ATTEMPTS = 3
LAST_RUN_KEY = "titles.last_run"
WEEKLY_DAYS = 7
APPLIED_STEP = 10  # new applied roles since the last run that make another run due
MAX_TITLES = 10
MAX_PURSUED = 30
EVIDENCE_CHARS = 160
_LIVE = ("pending", "leased")
_CHECK_EVERY_S = 600.0
_next_check = 0.0


def norm(title: str) -> str:
    """A title as compared: spacing collapsed, case folded."""
    return " ".join((title or "").split()).casefold()


# ---------------------------------------------------------------------------------- queueing
async def pending(session: AsyncSession) -> bool:
    n = (
        await session.execute(
            select(func.count()).select_from(Task).where(
                Task.type == TASK_TYPE, Task.status.in_(_LIVE)
            )
        )
    ).scalar_one()
    return bool(n)


async def request_title_suggestions(session: AsyncSession, reason: str) -> bool:
    """Queue one run unless one is already waiting or running. Flushes, never commits: the
    caller commits with its own change (a resume import). True when a task was added."""
    if await pending(session):
        return False
    session.add(
        Task(
            type=TASK_TYPE, payload={"reason": reason, "requested_at": utcnow_iso()},
            max_attempts=MAX_ATTEMPTS,
        )
    )
    await session.flush()
    return True


async def employment_resume(session: AsyncSession) -> DocMaster | None:
    """The newest active employment resume: the document the suggestions are read from."""
    return (
        await session.execute(
            select(DocMaster)
            .where(
                DocMaster.track == "employment", DocMaster.doc_kind == "resume",
                DocMaster.is_active.is_(True),
            )
            .order_by(DocMaster.updated_at.desc(), DocMaster.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _applied_count(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(Pursuit).where(
                Pursuit.track == "employment", Pursuit.applied_at.is_not(None)
            )
        )
    ).scalar_one()


def _parse(stamp: object) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def due(session: AsyncSession) -> str | None:
    """Why a run is due now, else None: "first" (never run, and there is a resume), "weekly"
    (the last run is over a week old) or "applied" (10 roles newly marked applied since it).
    Nothing is due without an employment resume to read."""
    if await employment_resume(session) is None:
        return None
    row = await session.get(Setting, LAST_RUN_KEY)
    last = row.value if row is not None and isinstance(row.value, dict) else None
    if last is None:
        return "first"
    at = _parse(last.get("at"))
    if at is None or datetime.now(UTC) - at >= timedelta(days=WEEKLY_DAYS):
        return "weekly"
    since = await _applied_count(session) - int(last.get("applied_count") or 0)
    return "applied" if since >= APPLIED_STEP else None


async def maybe_request(sm: async_sessionmaker[AsyncSession], *, force: bool = False) -> bool:
    """The worker's cadence check, at most every ten minutes. Never raises into the cycle."""
    global _next_check
    now = time.monotonic()
    if not force and now < _next_check:
        return False
    _next_check = now + _CHECK_EVERY_S
    try:
        async with sm() as s:
            reason = await due(s)
            if reason is None:
                return False
            if await _runs_online(s):
                # An automatic pass would send the whole resume to an online model without the
                # user doing anything ("asks before anything goes to an online model"). On an
                # online Tier 2 only Suggest again runs it; record the skip so this stays quiet.
                await _record_run(s, added=0, status="skipped_online")
                await s.commit()
                return False
            if not await request_title_suggestions(s, reason):
                return False
            await s.commit()
            return True
    except Exception as exc:  # the cadence is a convenience; a failure waits for the next check
        log.warning("title_suggestions_check_failed", error_type=type(exc).__name__, error=str(exc))
        return False


async def _runs_online(session: AsyncSession) -> bool:
    """Whether P15's tier would go to an external backend with the settings in effect now."""
    from kestrel.llm.prompts import load_prompt
    from kestrel.services.setting_overrides import load_settings_with_db

    settings = await load_settings_with_db(session)
    tier = load_prompt(PROMPT).tier
    llm = settings.llm
    if llm.mode == "hosted" and tier in llm.hosted_tiers:
        return True
    tcfg = llm.tiers.get(tier)
    backend = llm.backends.get(tcfg.backend) if tcfg is not None else None
    return bool(backend is not None and backend.external)


# --------------------------------------------------------------------------- the per-call schema
def title_model_for(slot_keys: list[str]) -> type[BaseModel]:
    """The P15 model with every evidence key an enum of the document's real slot keys."""
    key_type: Any = Literal[tuple(slot_keys)]  # type: ignore[valid-type]
    title = create_model(
        "P15TitleForMaster", __base__=P15TitleCore,
        evidence_slot_keys=(
            Annotated[list[key_type], _dedupe_cap(5), _max_items(5)], ...
        ),
    )
    return create_model(
        "P15TitleSuggestionsForMaster", __base__=P15TitleSuggestions,
        titles=(Annotated[list[title], _cap(MAX_TITLES), _max_items(MAX_TITLES)], ...),
    )


def title_schema(model_cls: type[BaseModel]) -> dict:
    from kestrel.docs.assess import assess_schema

    return assess_schema(model_cls)


# ------------------------------------------------------------------------------- generating
@dataclass
class Outcome:
    status: str  # stored | skipped | quarantined | error
    added: int = 0
    error: str | None = None


def _target_titles() -> list[str]:
    from kestrel.services.profile_edit import load_state

    terms = load_state().tracks["employment"].get("employment_terms") or {}
    return [str(t).strip() for t in (terms.get("target_titles") or []) if str(t).strip()]


async def _record_failure(sm: async_sessionmaker[AsyncSession]) -> None:
    """Record a failed pass, so due() waits for the weekly cadence instead of re-queuing a new
    task (and, on an online key out of credit, a new resume send) every ten minutes."""
    from sqlalchemy.exc import SQLAlchemyError

    try:
        async with sm() as s:
            await _record_run(s, added=0, status="error")
            await s.commit()
    except SQLAlchemyError as exc:
        log.warning("title_suggestions_record_failed", error_type=type(exc).__name__)


async def _record_run(session: AsyncSession, *, added: int, status: str) -> None:
    value = {
        "at": utcnow_iso(), "applied_count": await _applied_count(session), "added": added,
        "status": status,
    }
    row = await session.get(Setting, LAST_RUN_KEY)
    if row is None:
        session.add(Setting(key=LAST_RUN_KEY, value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()


async def generate(
    session: AsyncSession, client: Any, settings: Settings, *, requested_at: str | None = None
) -> Outcome:
    """Ask for titles and store the new ones. Commits. A run with no resume to read is recorded
    and skipped. A reply that fails validation is repaired once, then quarantined."""
    from kestrel.docs.assess import document_text, slot_keys
    from kestrel.docs.catalog import load_catalog
    from kestrel.llm.prompts import load_prompt
    from kestrel.pipeline._shared import make_quarantine_hook, run_prompt_hosted

    master = await employment_resume(session)
    catalog = await load_catalog(session, master.id) if master is not None else None
    keys = slot_keys(catalog) if catalog is not None else []
    if catalog is None or not keys:
        await _record_run(session, added=0, status="no_resume")
        await session.commit()
        return Outcome("skipped", error="there is no employment resume with bullets to read")

    texts = {}
    for sl in catalog.all_slots():
        v = next((x for x in sl.variants if x.key == sl.default_variant_key), None) or (
            sl.variants[0] if sl.variants else None)
        if v is not None:
            texts[sl.key] = v.text[:EVIDENCE_CHARS]
    seen_rows = (await session.execute(select(TitleSuggestion))).scalars().all()
    seen = {norm(r.title) for r in seen_rows}
    dismissed = [r.title for r in seen_rows if r.status == "dismissed"]
    targets = _target_titles()
    pursued = (
        await session.execute(
            select(Opportunity.title, Pursuit.status)
            .join(Pursuit, Pursuit.opportunity_id == Opportunity.id)
            .where(Pursuit.track == "employment", Opportunity.title.is_not(None))
            .order_by(Pursuit.last_activity_at.desc().nulls_last(), Pursuit.id.desc())
            .limit(MAX_PURSUED)
        )
    ).all()

    spec = load_prompt(PROMPT)
    values = {
        "{TARGET_TITLES}": "\n".join(f"- {t}" for t in targets) or "(none yet)",
        "{DISMISSED}": "\n".join(f"- {t}" for t in dismissed) or "(none)",
        "{PURSUED}": "\n".join(f"- {t} ({st})" for t, st in pursued) or "(none yet)",
        "{document}": document_text(catalog),
    }
    user = spec.user
    for marker, text in values.items():
        user = user.replace(marker, text)
    model_cls = title_model_for(keys)
    messages = [{"role": "system", "content": spec.system}, {"role": "user", "content": user}]
    result = await run_prompt_hosted(
        session, client, replace(spec, model_cls=model_cls), messages,
        quarantine_tier=QUARANTINE_TIER, master_id=master.id,
        on_quarantine=make_quarantine_hook(session, tier=QUARANTINE_TIER, opportunity_id=None),
        schema=title_schema(model_cls),
    )
    if result.quarantined:
        await _record_run(session, added=0, status="quarantined")
        await session.commit()  # the quarantine row (raw reply kept) staged by the hook
        log.warning("title_suggestions_quarantined", error=result.error)
        return Outcome("quarantined", error=result.error)
    if not result.ok or result.value is None:
        await session.rollback()
        return Outcome("error", error=result.error or "no reply")

    blocked = {norm(t) for t in targets} | seen
    added = 0
    for item in result.value.titles:
        title = " ".join(item.title.split())
        if not title or norm(title) in blocked:
            continue
        blocked.add(norm(title))
        evidence = [
            {"slot_key": k, "text": texts[k]} for k in item.evidence_slot_keys if k in texts
        ]
        session.add(
            TitleSuggestion(
                title=title, why=item.why.strip(), evidence=evidence, status="suggested",
                prompt_version=spec.version,
            )
        )
        added += 1
    await _record_run(session, added=added, status="ok")
    await session.commit()
    return Outcome("stored", added=added)


# --------------------------------------------------------------------------------- draining
async def drain_title_suggestions(
    sm: async_sessionmaker[AsyncSession], client: Any, settings: Settings, *, stop: Any = None
) -> int:
    """Run every queued suggestion task, one at a time, on the Tier 2 model (right after the
    interview questions, so the tier stays drained). A transport error or unexpected failure goes
    back to the queue while attempts remain; a quarantined reply completes the task, since the
    raw reply is kept for review and retrying would loop. Never raises into the worker cycle."""
    from kestrel.interview.story_task import LEASE_S, _renew
    from kestrel.queue.queue import TaskQueue

    queue = TaskQueue(sm)
    wid = "pipeline-titles"
    done = 0
    while stop is None or not stop.is_set():
        task = await queue.lease(wid, types=[TASK_TYPE], lease_seconds=LEASE_S)
        if task is None:
            break
        beat = asyncio.create_task(_renew(queue, task.id, wid))
        try:
            async with sm() as s:
                outcome = await generate(
                    s, client, settings, requested_at=(task.payload or {}).get("requested_at")
                )
        except Exception as exc:  # a failed task is retried, never fatal to the cycle
            beat.cancel()
            reason = f"{type(exc).__name__}: {exc}"
            log.warning("title_suggestions_failed", task=task.id, error=reason)
            await queue.fail(task.id, reason, worker_id=wid, retry=True)
            await _record_failure(sm)
            continue
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
        if outcome.status == "error":
            await queue.fail(task.id, outcome.error or "error", worker_id=wid, retry=True)
            await _record_failure(sm)
            continue
        await queue.complete(task.id, worker_id=wid)
        done += 1
    return done


# ------------------------------------------------------------------------ accept and dismiss
async def accept(session: AsyncSession, suggestion_id: int) -> tuple[bool, str]:
    """The operator pressed Accept: add the title to the target titles through the profile
    edit, then mark it accepted. (ok, message); a title that is no longer waiting changes
    nothing. The profile is written first, so a failed write leaves the suggestion waiting."""
    from kestrel.services import profile_edit as pe

    row = await session.get(TitleSuggestion, suggestion_id)
    if row is None or row.status != "suggested":
        return False, "That suggestion is no longer waiting."
    try:
        pe.add_target_title(row.title)
    except pe.ProfileEditError as exc:
        return False, str(exc)
    except OSError as exc:
        log.warning("title_accept_write_failed", error_type=type(exc).__name__, error=str(exc))
        return False, "Could not write profile.yml. Nothing was changed."
    row.status = "accepted"
    row.decided_at = utcnow_iso()
    await session.commit()
    return True, f"Added {row.title} to your target titles."


async def dismiss(session: AsyncSession, suggestion_id: int) -> bool:
    """Dismiss a waiting suggestion: it is kept so the title is never suggested again."""
    row = await session.get(TitleSuggestion, suggestion_id)
    if row is None or row.status != "suggested":
        return False
    row.status = "dismissed"
    row.decided_at = utcnow_iso()
    await session.commit()
    return True


async def view(session: AsyncSession) -> dict:
    """What the Suggested titles list shows: the waiting suggestions (newest first), whether a
    run is queued, whether there is a resume to read, and how the last run went."""
    rows = (
        await session.execute(
            select(TitleSuggestion)
            .where(TitleSuggestion.status == "suggested")
            .order_by(TitleSuggestion.id.desc())
        )
    ).scalars().all()
    last = await session.get(Setting, LAST_RUN_KEY)
    return {
        "suggestions": [
            {"id": r.id, "title": r.title, "why": r.why, "evidence": list(r.evidence or [])}
            for r in rows
        ],
        "pending": await pending(session),
        "has_resume": await employment_resume(session) is not None,
        "last": last.value if last is not None and isinstance(last.value, dict) else None,
    }
