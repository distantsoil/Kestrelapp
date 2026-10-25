"""Needs another try (D-UI-104, SPEC-INTERFACES 2.1 `/quarantine`): the roles that could not be
scored, one row per role, with the actions that bring each one back.

  GET  /quarantine                       the page (the card alone for an HTMX request); `?step=`
                                          filters to Quick sort, Fit score or Deep read
  GET  /quarantine/summary               the count pill and the filter chips, refreshed in place
  GET  /quarantine/{id}/row              one row (polled while it waits); a scored or removed
                                          row answers with an event that folds it out
  POST /quarantine/{id}/retry            Try again, same model (the D-UI-70 re-queue)
  GET  /quarantine/{id}/models           the model menu: this computer first, then online
  POST /quarantine/{id}/model            Try with a different model (a local pick, or an online
                                          pick the operator confirmed in the dialog)
  GET  /quarantine/{id}/online           the ask-first dialog: what is sent, what is not, the cost
  GET  /quarantine/{id}/anyway           the confirm dialog for Score it anyway
  POST /quarantine/{id}/anyway           skip the rules check for this role (D-UI-74) and score it
  POST /quarantine/{id}/remove           take the role off this list only; Undo restores it
  POST /quarantine/{id}/restore          Undo of Remove
  POST /quarantine/bulk                  Try all again, or the selected rows

Nothing here calls a model: every action writes task rows through the same durable queue the
pipeline uses, and the worker does the scoring (hard rules 4 and 5). Nothing deletes a quarantine
row or a role (hard rule 3 keeps the raw reply); Remove only records a marker
(kestrel.services.retry_removal). Rows update in place: a row swaps itself, never the page."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from kestrel.db.models import Opportunity, Quarantine
from kestrel.llm import credit
from kestrel.llm.providers import is_free_variant
from kestrel.queue.rescore import enqueue_rescore
from kestrel.services import hosted_balance, retry_removal
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import plain_error
from kestrel.web.pages.rescore import (
    HOSTED_PREFIX,
    _count_retry,
    _hosted_models,
    _local_models,
    _override_for,
    history_rows,
    rule_explainer,
)
from kestrel.web.pages.runs_detail import _open_total, _quarantine_rows, _retry_quarantine
from kestrel.web.retry_words import (
    FILTERS,
    is_held,
    plain_reason,
    refused_key,
    step_for,
    tries_words,
    when_words,
)

log = structlog.get_logger()
router = APIRouter()

ROW_PREFIX = "rt-row-"
_STEP_KEYS = tuple(k for k, _ in FILTERS)
# What an online try sends, as a rough size for the cost line: the posting text, a short profile
# summary and the instructions in; the structured answer out. Four characters a token.
_PAGE_CHARS = 3000
_FIXED_IN_TOKENS = 2500
_OUT_TOKENS = 1500

# How the operator's own derived local models read (Kestrel builds these from the tier settings).
_LOCAL_WORDS = {
    "kestrel-tier3": ("Deep read model", "The careful one. A bit slower."),
    "kestrel-tier2": ("Full review model", "The usual model for scoring a role."),
}


# ------------------------------------------------------------------------------------ views
def _view(r: dict) -> dict:
    """One quarantine row as the page shows it: the step, the plain reason, when and the tries."""
    q = r["q"]
    key, label = step_for(q.tier)
    error = r["error"] or q.error
    return {
        **r,
        "id": q.id,
        "dom_id": f"{ROW_PREFIX}{q.id}",
        "step_key": key,
        "step_label": label,
        "reason": plain_reason(error),
        "refused_key": refused_key(error),
        "on_hold": is_held(error),
        "when": when_words(q.created_at),
        "attempts": max(int(q.attempts or 0), 1),
        "tries": tries_words(q.attempts),
        "can_act": r["opp"] is not None,
    }


def _role_views(open_rows: list[dict]) -> list[dict]:
    """One row per role, newest failure first. An older failure of the same role is not a second
    row; its tries add to the first, so a role that failed again reads "5 tries", not twice."""
    seen: dict[int, dict] = {}
    out: list[dict] = []
    for r in open_rows:
        v = _view(r)
        oid = v["opp"].id if v["opp"] is not None else None
        if oid is not None and oid in seen:
            seen[oid]["attempts"] += v["attempts"]
            continue
        out.append(v)
        if oid is not None:
            seen[oid] = v
    for v in out:
        v["tries"] = tries_words(v["attempts"])
    return out


def _counts(views: list[dict]) -> dict[str, int]:
    return {key: (len(views) if key == "all" else sum(1 for v in views if v["step_key"] == key))
            for key in _STEP_KEYS}


def _step(raw: str | None) -> str:
    return raw if raw in _STEP_KEYS else "all"


async def _list_context(request: Request, step: str) -> dict:
    """The whole list: open rows as one row per role, the resolved ones as history (D-UI-70), the
    counts for the filter chips, and how many older failures the cap left out."""
    async with sessionmaker()() as s:
        rows = await _quarantine_rows(s)
        open_total = await _open_total(s)
    open_rows = [r for r in rows if not r["resolved"]]
    views = _role_views(open_rows)
    shown = views if step == "all" else [v for v in views if v["step_key"] == step]
    return {
        "query": dict(request.query_params),
        "rows": shown,
        "counts": _counts(views),
        "filters": FILTERS,
        "step": step,
        "waiting": len(views),
        "resolved_rows": [_view(r) for r in rows if r["resolved"]],
        "open_more": max(0, open_total - len(open_rows)),
        "outcome": None,
    }


def _events(events: dict[str, Any] | None) -> dict[str, str]:
    """An HX-Trigger header. ensure_ascii keeps it latin-1 safe for the header."""
    return {"HX-Trigger": json.dumps(events, ensure_ascii=True)} if events else {}


def _toast(message: str, undo_url: str | None = None, tone: str | None = None) -> dict:
    body: dict[str, Any] = {"message": message}
    if undo_url:
        body["undoUrl"] = undo_url
    if tone:
        body["tone"] = tone
    return body


def _row_html(request: Request, view: dict, **ctx: Any) -> str:
    return templates().get_template("partials/retry_row.html").render(
        request=request, row=view, **ctx
    )


def _gone(row_id: str, events: dict[str, Any] | None = None) -> Response:
    """A row that is no longer on the list: nothing to swap; the page folds it out."""
    body = {"rt-gone": {"id": row_id}, "rt-refresh": True, **(events or {})}
    return Response(status_code=200, headers={"HX-Reswap": "none", **_events(body)})


async def _view_for(session, quarantine_id: int) -> dict | None:
    """The current row for the role behind `quarantine_id`, or None when the role is no longer on
    the list (scored since, removed, or the row never existed)."""
    q = await session.get(Quarantine, quarantine_id)
    if q is None:
        return None
    if q.opportunity_id is not None:
        rows = await _quarantine_rows(session, opportunity_id=q.opportunity_id, resolved_cap=0)
    else:
        rows = await _quarantine_rows(session, quarantine_id=quarantine_id, resolved_cap=0)
    views = _role_views([r for r in rows if not r["resolved"]])
    return views[0] if views else None


async def _row_response(
    request: Request, quarantine_id: int, *, note: str | None = None, flash: bool = False,
    events: dict[str, Any] | None = None, error: str | None = None,
) -> Response:
    """Answer a row action with the row in its new state (or fold it out when it left the list)."""
    async with sessionmaker()() as s:
        view = await _view_for(s, quarantine_id)
    row_id = f"{ROW_PREFIX}{quarantine_id}"
    if view is None:
        gone = dict(events or {})
        if error and "k-toast" not in gone:
            gone["k-toast"] = _toast(error, tone="warn")  # the row left; the message must not
        return _gone(row_id, gone)
    out = {"rt-refresh": True, **(events or {})}
    html = _row_html(request, view, note=note or error, flash=flash)
    return HTMLResponse(html, headers=_events(out))


def _role_name(view: dict | None) -> str:
    if view is None or view["opp"] is None:
        return "this role"
    return view["opp"].title or "this role"


# ---------------------------------------------------------------------------------- the page
@router.get("/quarantine", response_class=HTMLResponse)
async def page(request: Request, step: str = "all"):
    from kestrel.web.pages.rescore import outcome_for

    ctx = await _list_context(request, _step(step))
    ctx["title"] = "Needs another try"
    ctx["outcome"] = outcome_for(request.query_params)
    ctx["retry_error"] = None
    name = "partials/retry_card.html" if request.headers.get("HX-Request") else "quarantine.html"
    return templates().TemplateResponse(request, name, ctx)


@router.get("/quarantine/summary", response_class=HTMLResponse)
async def summary(request: Request, step: str = "all"):
    ctx = await _list_context(request, _step(step))
    return templates().TemplateResponse(request, "partials/retry_summary.html", ctx)


@router.get("/quarantine/{quarantine_id}/row", response_class=HTMLResponse)
async def row(request: Request, quarantine_id: int):
    """One row, polled while it waits for the worker. Still waiting: the row. Scored since: the
    row folds out with the score in a toast. Failed again: the row, flashed, with its tries."""
    async with sessionmaker()() as s:
        view = await _view_for(s, quarantine_id)
        scored = None
        if view is None:
            q = await s.get(Quarantine, quarantine_id)
            if q is not None and q.opportunity_id is not None:
                opp = await s.get(Opportunity, q.opportunity_id)
                hist = await history_rows(s, q.opportunity_id)
                if opp is not None and hist and hist[0]["score"] is not None:
                    scored = (opp, hist[0]["score"])
    row_id = f"{ROW_PREFIX}{quarantine_id}"
    if view is None:
        events: dict[str, Any] = {}
        if scored is not None:
            opp, score = scored
            who = f"{opp.title or 'The role'}" + (f" at {opp.org_name}" if opp.org_name else "")
            events["k-toast"] = _toast(f"Scored: {who}, {score:.1f} of 5.")
        return _gone(row_id, events)
    if view["queued"]:
        return HTMLResponse(_row_html(request, view))
    if view["on_hold"]:  # nothing was sent: it waits for the operator, it did not fail
        events = {"rt-refresh": True, "k-toast": _toast(
            f"Held, not sent: {_role_name(view)} waits for you to choose your current employer."
        )}
        return HTMLResponse(_row_html(request, view), headers=_events(events))
    events = {"rt-refresh": True, "k-toast": _toast(
        f"Still couldn’t score {_role_name(view)}. Try a different model, or score it anyway.",
        tone="warn",
    )}
    return HTMLResponse(_row_html(request, view, flash=True), headers=_events(events))


# -------------------------------------------------------------------------------- try again
@router.post("/quarantine/{quarantine_id}/retry", response_class=HTMLResponse)
async def retry(request: Request, quarantine_id: int):
    """Try again, same model: the D-UI-70 re-queue (`_retry_quarantine`), which only writes a
    stage change and a task row. A script request swaps the row; a plain post gets the card."""
    credit.clear()
    error: str | None = None
    async with sessionmaker()() as s:
        try:
            error = await _retry_quarantine(s, quarantine_id)
        except Exception as exc:  # a write path must never turn into a 500
            await s.rollback()
            log.warning("quarantine_retry_failed", quarantine_id=quarantine_id,
                        error_type=type(exc).__name__, error=str(exc))
            error = f"Could not retry quarantine #{quarantine_id}: {plain_error(exc)}"
    if request.headers.get("HX-Target", "").startswith(ROW_PREFIX):
        events = {"k-toast": _toast(error, tone="warn")} if error else None
        return await _row_response(request, quarantine_id, error=error, events=events)
    ctx = await _list_context(request, "all")
    ctx.update(title="Needs another try", retry_error=error)
    return templates().TemplateResponse(request, "partials/retry_card.html", ctx)


# ------------------------------------------------------------------- try a different model
def _version_tag(tag: str) -> str:
    return tag.split(":", 1)[0]


def _estimate_cost(model: Any, description: str | None) -> str:
    """Words for what one online try may cost on `model`: free, under a cent, or about N cents
    or dollars. From the posting size and the listed price; "price not listed" when unknown."""
    from kestrel.services import hosted_models as hm

    if hm.is_free(model):
        return "free"
    if model.prompt_usd_per_m is None or model.completion_usd_per_m is None:
        return "price not listed"
    tokens_in = _FIXED_IN_TOKENS + len(description or "") / 4
    usd = (tokens_in * model.prompt_usd_per_m + _OUT_TOKENS * model.completion_usd_per_m) / 1e6
    if usd < 0.01:
        return "under 1 cent"
    if usd < 1:
        cents = max(1, round(usd * 100))
        return f"about {cents} cent{'s' if cents != 1 else ''}"
    return f"about ${usd:.2f}"


@router.get("/quarantine/{quarantine_id}/models", response_class=HTMLResponse)
async def models_menu(request: Request, quarantine_id: int):
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        q = await s.get(Quarantine, quarantine_id)
    local, local_note = await asyncio.to_thread(_local_models, settings)
    label, _name, _raw, picker, _reasons, hosted_note = await _hosted_models(settings)
    tcfg = settings.llm.tiers.get("tier2")
    current = _version_tag(tcfg.model) if tcfg is not None else ""
    options = []
    for tag in local:
        if _version_tag(tag) == current:
            continue  # Try again already uses it
        name, note = _LOCAL_WORDS.get(_version_tag(tag), (tag, "Runs on this computer. Free."))
        options.append({"tag": tag, "name": name, "note": note})
    ctx = {
        "quarantine_id": quarantine_id,
        "dom_id": f"{ROW_PREFIX}{quarantine_id}",
        "has_role": q is not None and q.opportunity_id is not None,
        "local_options": options,
        "local_note": local_note,
        "hosted_label": label,
        "hosted_ready": bool(picker),
        "hosted_note": hosted_note,
    }
    return templates().TemplateResponse(request, "partials/retry_menu.html", ctx)


async def _offered_models(settings: Any, description: str | None) -> tuple[list[dict], dict]:
    """The online models the ask-first dialog lists (id, name, cost), and the lookup context
    (provider label, note). The same list gates the pick in `try_model`, so a model that is not
    offered here can never be queued. Empty unless Hosted mode is on."""
    label, backend_name, raw, picker, reasons, note = await _hosted_models(settings)
    by_id = {m.id: m for m in raw}
    backend = settings.llm.backends.get(backend_name)
    models = [
        {"id": p.id, "name": p.name, "cost": _estimate_cost(by_id[p.id], description),
         "dest": f"{backend_name}|{backend.base_url if backend else ''}|{p.id}"}
        for p in picker if p.id in by_id and not reasons.get(p.id)
    ]
    return models, {"label": label, "note": note}


async def _employer_state(session, settings: Any) -> str:
    """What happens to the current employer in an online send, as the pipeline will decide it
    (`plan_hosted_send`): "hidden" (confidential mode, an employer is chosen), "held" (confidential
    mode and none chosen: the send is refused and the role waits) or "off"."""
    if not settings.confidential_mode or not settings.llm.hosted_mode.redact_confidential:
        return "off"
    from kestrel.docs.current_employer import load_employer_map

    return "hidden" if await load_employer_map(session) else "held"


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


@router.get("/quarantine/{quarantine_id}/online", response_class=HTMLResponse)
async def online_dialog(request: Request, quarantine_id: int):
    """The ask-first dialog: provider, what is sent, what is not, the cost and the credit left.
    What is sent is the Tier 2 line of the Hosted mode disclosure (`disclosure_terms`), the text
    the operator acknowledged, not words written here. Nothing leaves this computer until the
    operator presses Send and try."""
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        q = await s.get(Quarantine, quarantine_id)
        opp = await s.get(Opportunity, q.opportunity_id) if q and q.opportunity_id else None
        credit_row = await hosted_balance.rail_row(s, settings)
        employer = await _employer_state(s, settings)
    desc = opp.description_md if opp is not None else None
    models, hosted = await _offered_models(settings, desc)
    pages = max(1, round(len(desc) / _PAGE_CHARS)) if desc else None
    sends = settings.llm.hosted_mode.disclosure_terms()["sends"]
    import json

    from kestrel.services.online_consent import approval_for, merge_approvals
    ctx = {
        "quarantine_id": quarantine_id,
        "dom_id": f"{ROW_PREFIX}{quarantine_id}",
        "role": (opp.title if opp is not None else None) or "this role",
        "org": opp.org_name if opp is not None else None,
        "provider": hosted["label"] or "the online service",
        "models": models,
        "note": hosted["note"],
        "pages": pages,
        "sends": _sentence(sends["tier2"]),
        "employer": employer,
        "credit": credit_row["label"] if credit_row else None,
        # Only OpenRouter's ":free" variants may train on what they receive (as the Role page).
        "any_free": any(is_free_variant(str(m["id"])) for m in models),
        "approval_json": json.dumps(
            merge_approvals(
                approval_for(settings, ("tier1", "tier2")),
                {"tier2": [model["dest"] for model in models]},
            ), separators=(",", ":")
        ),
    }
    return templates().TemplateResponse(request, "partials/retry_online.html", ctx)


@router.post("/quarantine/{quarantine_id}/model", response_class=HTMLResponse)
async def try_model(request: Request, quarantine_id: int):
    """Try with a different model, this run only (settings never change). A local pick queues
    at once. An online pick needs `confirmed=1`, which only the ask-first dialog sends, and is
    checked here too: Hosted mode must be on and the model must be one the dialog lists."""
    form = await request.form()
    pick = str(form.get("model") or "").strip()[:240]
    if not pick:
        return await _row_response(request, quarantine_id, error="Pick a model first.")
    if pick.startswith(HOSTED_PREFIX):
        if str(form.get("confirmed") or "") != "1":
            return await _row_response(
                request, quarantine_id, error="Nothing was sent. Confirm in the dialog first."
            )
        async with sessionmaker()() as s:
            settings = await load_settings_with_db(s)
        if not settings.llm.hosted_mode.active:
            return await _row_response(
                request, quarantine_id,
                error="Nothing was sent. Online models need Hosted mode, which is off.",
            )
        offered, _ = await _offered_models(settings, None)
        if pick.removeprefix(HOSTED_PREFIX) not in {m["id"] for m in offered}:
            return await _row_response(
                request, quarantine_id,
                error="Nothing was sent. That model is not on the online list.",
            )
    return await _queue(request, quarantine_id, pick=pick, anyway=False, form=form)


# ------------------------------------------------------------------------ score it anyway
@router.get("/quarantine/{quarantine_id}/anyway", response_class=HTMLResponse)
async def anyway_dialog(request: Request, quarantine_id: int):
    async with sessionmaker()() as s:
        view = await _view_for(s, quarantine_id)
    ctx = {
        "quarantine_id": quarantine_id,
        "dom_id": f"{ROW_PREFIX}{quarantine_id}",
        "role": _role_name(view),
        "org": view["opp"].org_name if view and view["opp"] is not None else None,
    }
    return templates().TemplateResponse(request, "partials/retry_anyway.html", ctx)


@router.post("/quarantine/{quarantine_id}/anyway", response_class=HTMLResponse)
async def score_anyway(request: Request, quarantine_id: int):
    """Score it anyway (D-UI-74): the role is scored with the rules check skipped, and the
    operator's choice is recorded by Tier 1 when it runs (kestrel.services.tier0_override)."""
    return await _queue(request, quarantine_id, pick="same", anyway=True, form=await request.form())


async def _queue(
    request: Request, quarantine_id: int, *, pick: str, anyway: bool, form=None
) -> Response:
    """Queue the role behind `quarantine_id` for scoring through `enqueue_rescore`, counting the
    attempt on its quarantine row. `pick` is "same", a local model or `hosted:<id>`."""
    credit.clear()
    note: str | None = None
    try:
        async with sessionmaker()() as s:
            q = await s.get(Quarantine, quarantine_id)
            if q is None or q.opportunity_id is None:
                return await _row_response(request, quarantine_id,
                                           error="This failure has no role to score.")
            settings = await load_settings_with_db(s)
            override = None
            if pick != "same":
                override, _label, why = await _override_for(settings, pick)
                if override is None:
                    return await _row_response(request, quarantine_id, error=f"Not queued: {why}",
                                               events={"k-toast": _toast(f"Not queued: {why}",
                                                                          tone="warn")})
            from kestrel.services.online_consent import approval_problem
            effective = settings
            expected = None
            if override is not None:
                from kestrel.pipeline._shared import settings_with_override
                try:
                    effective = settings_with_override(settings, "tier2", override)
                except ValueError:
                    effective = settings
                from kestrel.web.pages.rescore import _approval_for_override

                expected = _approval_for_override(settings, override)
            problem = await approval_problem(
                s, effective, ("tier1", "tier2"), form or {}, expected=expected
            )
            if problem:
                return await _row_response(
                    request, quarantine_id,
                    error="Nothing was sent. Confirm the online send first.",
                )
            result = await enqueue_rescore(
                s, [q.opportunity_id], model_override=override, tier0_anyway=anyway,
                explain=rule_explainer(s, settings),
                online_approval=(form or {}).get("online_approval"),
            )
            await _count_retry(s, str(quarantine_id), result.queued)
            await s.commit()
            if not result.queued:
                note = " ".join(result.messages()) or "Not queued. Open the role to see why."
    except Exception as exc:  # a write path never turns into a 500 (CLAUDE.md)
        log.warning("quarantine_queue_failed", quarantine_id=quarantine_id,
                    error_type=type(exc).__name__, error=str(exc))
        note = f"Not queued: {plain_error(exc)}"
    events = {"k-toast": _toast(note, tone="warn")} if note else None
    return await _row_response(request, quarantine_id, error=note, events=events)


# ------------------------------------------------------------------------ remove and undo
@router.post("/quarantine/{quarantine_id}/remove", response_class=HTMLResponse)
async def remove(request: Request, quarantine_id: int):
    """Take the role off this list only. Nothing is deleted: the quarantine rows keep their raw
    replies and the role stays in Pipeline, not scored. Undo restores it."""
    async with sessionmaker()() as s:
        q = await s.get(Quarantine, quarantine_id)
        if q is None or q.opportunity_id is None:
            return await _row_response(
                request, quarantine_id, error="Only a failure with a role can be removed."
            )
        view = await _view_for(s, quarantine_id)
        opp = await s.get(Opportunity, q.opportunity_id)
        stamp = await retry_removal.mark(s, q.opportunity_id)
        await s.commit()
    who = (opp.title if opp is not None else None) or _role_name(view)
    msg = f"Removed {who} from this list. It stays in Pipeline, not scored."
    return _gone(
        f"{ROW_PREFIX}{quarantine_id}",
        {
            "k-toast": _toast(
                msg, undo_url=f"/quarantine/{quarantine_id}/restore?at={quote(stamp)}"
            ),
        },
    )


@router.post("/quarantine/{quarantine_id}/restore", response_class=HTMLResponse)
async def restore(request: Request, quarantine_id: int, at: str = ""):
    """Undo of Remove: the list asks for itself again (the toast's Undo posts here)."""
    async with sessionmaker()() as s:
        q = await s.get(Quarantine, quarantine_id)
        if q is not None and q.opportunity_id is not None:
            if not at or not await retry_removal.clear(s, q.opportunity_id, at=at):
                return HTMLResponse(
                    '<span data-undo-msg data-ok="0">This can no longer be undone.</span>'
                )
            await s.commit()
    return Response(status_code=204, headers=_events({"rt-reload": True}))


# ------------------------------------------------------------------------------------ bulk
@router.post("/quarantine/bulk", response_class=HTMLResponse)
async def bulk(request: Request):
    """Try all again, or the selected rows. With no selection it is every row in the current
    filter that is not already waiting. Each row is re-queued the same way Try again does, and
    only the changed rows swap (out of band)."""
    form = await request.form()
    ids = {int(v) for v in form.getlist("ids") if str(v).isdigit()}
    step = _step(str(form.get("step") or "all"))
    credit.clear()
    async with sessionmaker()() as s:
        rows = await _quarantine_rows(s, resolved_cap=0)
    views = _role_views([r for r in rows if not r["resolved"]])
    if ids:
        targets = [v for v in views if v["id"] in ids]
    else:
        targets = [v for v in views if (step == "all" or v["step_key"] == step)
                   and not v["queued"] and not v["hidden"] and v["opp"] is not None]
    errors = 0
    for v in targets:
        async with sessionmaker()() as s:
            try:
                if await _retry_quarantine(s, v["id"]):
                    errors += 1
            except Exception as exc:  # one bad row never stops the others
                await s.rollback()
                errors += 1
                log.warning("quarantine_bulk_failed", quarantine_id=v["id"],
                            error_type=type(exc).__name__, error=str(exc))
    out: list[str] = []
    async with sessionmaker()() as s:
        for v in targets:
            fresh = await _view_for(s, v["id"])
            if fresh is not None:
                out.append(_row_html(request, fresh, oob=True, old_id=v["dom_id"]))
    n = len(targets)
    if not n:
        msg = "Nothing to try again."
    else:
        queued = n - errors
        msg = (f"Trying {queued} {'role' if queued == 1 else 'roles'} again. Scores appear here "
               "as they finish." + (f" {errors} could not be queued." if errors else ""))
    return HTMLResponse("".join(out), headers=_events({
        "k-toast": _toast(msg, tone="warn" if errors else None), "rt-refresh": True,
    }))
