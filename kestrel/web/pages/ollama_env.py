"""Settings > This computer: Ollama's speed settings, set with a button (issue #181, D-SETUP-650).

GET /setup/ollama-env renders the section (read only); `?review=1` renders the confirm, which
states each variable, its value, where it is set and how to undo it. Three POSTs change the
system, each only on its own button: apply (the confirmed plan's digest), undo (the stored
record's digest) and restart (where Kestrel can restart Ollama safely). Every system call runs
off the event loop.

Invariant: nothing on the person's system changes without the exact button press; a post with
a missing or stale digest changes nothing (tests/test_ollama_env.py, tests/test_contracts.py)."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from kestrel.db.models import Setting, Task
from kestrel.services import ollama_env as oe
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.auth import LOCAL_ONLY, request_is_local
from kestrel.web.deps import sessionmaker, templates

log = structlog.get_logger()
router = APIRouter()

RESTART_WORD = "ollama"


async def _stored_undo(session) -> Any:
    row = await session.get(Setting, oe.UNDO_KEY)
    return row.value if row is not None else None


async def _store(record: oe.UndoRecord) -> None:
    async with sessionmaker()() as s:
        row = await s.get(Setting, oe.UNDO_KEY)
        if row is None:
            s.add(Setting(key=oe.UNDO_KEY, value=record.as_json()))
        else:
            row.value = record.as_json()
        await s.commit()


async def _load() -> tuple[Any, Any]:
    async with sessionmaker()() as s:
        return await load_settings_with_db(s), await _stored_undo(s)


async def _render(request: Request, *, review: bool = False, message: str = "",
                  good: bool = True, restart_offer: bool = False) -> HTMLResponse:
    settings, stored = await _load()
    if request_is_local(request):
        url = settings.llm.ollama_native_url
        try:
            a = await asyncio.to_thread(oe.assess, settings, stored, url)
        except oe.EnvChangeFailed as exc:
            a = oe.Assessment(False, exc.sentence, oe.need_for(settings), (), None, None, None)
    else:
        # A key holder (phone, tunnel) never sees this computer's values or paths, and the
        # page runs no command for them.
        a = oe.Assessment(False, "Only this computer can see or change these.",
                          oe.need_for(settings), (), None, None, None)
    be = a.backend
    return templates().TemplateResponse(request, "partials/ollama_env.html", {
        "oe": a, "be": be, "review": review and a.plan is not None and not a.blocked,
        "message": message, "good": good, "restart_offer": restart_offer,
        "restart_word": RESTART_WORD,
        "restart_cmds": be.restart_commands() if be else [],
        "docs": oe.DOCS, "docs_read": oe.DOCS_READ,
    })


@router.get("/setup/ollama-env", response_class=HTMLResponse)
async def ollama_env_section(request: Request):
    return await _render(request, review=request.query_params.get("review") == "1")


@router.post("/setup/ollama-env/apply", response_class=HTMLResponse, dependencies=[LOCAL_ONLY])
async def ollama_env_apply(request: Request):
    form = await request.form()
    digest = str(form.get("plan") or "")
    settings, stored = await _load()
    url = settings.llm.ollama_native_url
    try:
        plan, record = await asyncio.to_thread(oe.apply, settings, stored, digest, url)
    except oe.StalePlan as exc:
        return await _render(request, message=exc.sentence, good=False)
    except oe.EnvChangeFailed as exc:
        if exc.record is not None and not exc.partial:
            await _store(exc.record)
            return await _render(request, message=exc.sentence, good=False)
        if exc.record is None:
            return await _render(request, message=f"{exc.sentence} Nothing was changed.",
                                 good=False)
        await _store(exc.record)
        return await _render(request, good=False, message=f"{exc.sentence} Some values may "
                             "have changed; press Undo to put them back.")
    await _store(record)
    names = " and ".join(c.name for c in plan.changes)
    return await _render(request, message=f"Set {names}. Ollama reads them only when it "
                         "starts, so restart Ollama now.", restart_offer=True)


@router.post("/setup/ollama-env/undo", response_class=HTMLResponse, dependencies=[LOCAL_ONLY])
async def ollama_env_undo(request: Request):
    form = await request.form()
    digest = str(form.get("undo") or "")
    _settings, stored = await _load()
    try:
        _record, skipped = await asyncio.to_thread(oe.undo, stored, digest)
    except oe.StalePlan as exc:
        return await _render(request, message=exc.sentence, good=False)
    except oe.EnvChangeFailed as exc:
        return await _render(request, good=False,
                             message=f"{exc.sentence} Undo did not finish; press it again.")
    async with sessionmaker()() as s:
        row = await s.get(Setting, oe.UNDO_KEY)
        if row is not None:
            await s.delete(row)
        await s.commit()
    message = ("Undone: the values are back to how they were before Kestrel changed them. "
               "Restart Ollama to use them.")
    if skipped:
        message += (f" Left as they are now, because they changed after Kestrel set them: "
                    f"{', '.join(skipped)}.")
    return await _render(request, message=message, restart_offer=True)


@router.post("/setup/ollama-env/restart", response_class=HTMLResponse,
             dependencies=[LOCAL_ONLY])
async def ollama_env_restart(request: Request):
    form = await request.form()
    if str(form.get("restart") or "") != RESTART_WORD:
        return await _render(request, message="Not restarted.", good=False)
    async with sessionmaker()() as s:
        working = (await s.execute(
            select(func.count()).select_from(Task).where(Task.status == "leased")
        )).scalar_one()
    if working:
        return await _render(request, message="Not restarted: Kestrel is using Ollama right "
                             "now. Try again when the current work finishes.", good=False,
                             restart_offer=True)
    try:
        await asyncio.to_thread(oe.restart)
    except oe.EnvChangeFailed as exc:
        return await _render(request, message=exc.sentence, good=False)
    from kestrel.services import setup_state as ss

    ss.forget_ollama_probe()
    return await _render(request, message="Ollama is restarting. Give it a few seconds, then "
                         "press Check again in step 2.")
