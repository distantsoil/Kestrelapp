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
from sqlalchemy.exc import SQLAlchemyError

from kestrel.db.models import Opportunity, Quarantine
from kestrel.llm.providers import is_free_variant
from kestrel.queue import row_retry
from kestrel.queue.rescore import enqueue_rescore
from kestrel.services import hosted_balance, retry_removal
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import retry_groups
from kestrel.web.auth import LOCAL_ONLY, release_credit_hold
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
)

log = structlog.get_logger()
router = APIRouter()

ROW_PREFIX = retry_groups.ROW_PREFIX
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
# The row view, the one-row-per-role merge and the group lookup live in retry_groups, so Today
# reads the same groups without importing a page.
_view = retry_groups.view
_role_views = retry_groups.role_views
_groups_now = retry_groups.groups_now


def _counts(views: list[dict]) -> dict[str, int]:
    return {key: (len(views) if key == "all" else sum(1 for v in views if v["step_key"] == key))
            for key in _STEP_KEYS}


def _step(raw: str | None) -> str:
    return raw if raw in _STEP_KEYS else "all"


async def _list_context(request: Request, step: str, group: str = "") -> dict:
    """The whole list: open rows as one row per role, the resolved ones as history (D-UI-70), the
    counts for the filter chips, and how many older failures the cap left out."""
    async with sessionmaker()() as s:
        rows = await _quarantine_rows(s)
        open_total = await _open_total(s)
        dismissed = await retry_groups.dismissed_counts(s)
    open_rows = [r for r in rows if not r["resolved"]]
    views = _role_views(open_rows)
    groups = retry_groups.build_groups(views)
    # `?group=` shows exactly one group's roles; an unknown or stale key shows the whole list.
    picked = retry_groups.find(groups, group) if group else None
    shown = views if step == "all" else [v for v in views if v["step_key"] == step]
    if picked is not None:
        ids = {m["id"] for m in picked["members"]}
        shown = [v for v in views if v["id"] in ids]
    return {
        "groups": [_with_words(g) for g in retry_groups.visible(groups, dismissed)],
        "group_picked": picked,
        "query": dict(request.query_params),
        "rows": shown,
        "counts": _counts(views),
        "filters": FILTERS,
        "step": "all" if picked is not None else step,
        "waiting": len(views),
        "resolved_rows": [_view(r) for r in rows if r["resolved"]],
        "open_more": max(0, open_total - len(open_rows)),
        "outcome": None,
    }


def _with_words(g: dict) -> dict:
    """A group with its model in the words the menu uses (a derived local model by its role)."""
    tag = _version_tag(g["model"]) if g["model"] else ""
    return {
        **g,
        "model_words": _LOCAL_WORDS.get(tag, (g["model"] or "one model",))[0],
        "ids": retry_groups.shown_ids(g["members"]),
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
async def page(request: Request, step: str = "all", group: str = ""):
    from kestrel.web.pages.rescore import outcome_for

    ctx = await _list_context(request, _step(step), group[:80])
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
    if view["not_sent"]:  # refused before sending: say why, not "try a different model"
        events = {"rt-refresh": True, "k-toast": _toast(
            f"Not sent: {_role_name(view)}. {view['reason']}.", tone="warn")}
        return HTMLResponse(_row_html(request, view, flash=True), headers=_events(events))
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
    release_credit_hold(request)
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
    ctx = {
        "quarantine_id": quarantine_id,
        "dom_id": f"{ROW_PREFIX}{quarantine_id}",
        "has_role": q is not None and q.opportunity_id is not None,
        **await _menu_options(settings),
    }
    return templates().TemplateResponse(request, "partials/retry_menu.html", ctx)


async def _menu_options(settings: Any) -> dict:
    """What the model menu offers, for one role or a group: this computer first, then online."""
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
    return {
        "local_options": options,
        "local_note": local_note,
        "hosted_label": label,
        "hosted_ready": bool(picker),
        "hosted_note": hosted_note,
    }


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
    if not settings.confidential_mode:  # the one condition the send itself reads (D-PRIV-805)
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
        q = await s.get(Quarantine, quarantine_id)
        opp = await s.get(Opportunity, q.opportunity_id) if q and q.opportunity_id else None
        ctx = await _online_ctx(s, [opp] if opp is not None else [])
    ctx.update(quarantine_id=quarantine_id, dom_id=f"{ROW_PREFIX}{quarantine_id}")
    return templates().TemplateResponse(request, "partials/retry_online.html", ctx)


async def _online_ctx(s, opps: list[Opportunity]) -> dict:
    """What the ask-first dialog shows for one role or a group: the provider, what is sent (the
    disclosure text), the size of the postings, the cost per role and the credit left. The one
    place the dialog's words are built, so a group's dialog can never say less than a role's."""
    import json

    from kestrel.learning.correction_lines import dialog_digest, lines_for
    from kestrel.services.online_consent import approval_for, merge_approvals

    settings = await load_settings_with_db(s)
    credit_row = await hosted_balance.rail_row(s, settings)
    employer = await _employer_state(s, settings)
    descs = [o.description_md for o in opps if o.description_md]
    # The cost beside each model is for the longest posting, so a group's total is never above
    # the count times it (a short first posting must not set the price of a long group).
    models, hosted = await _offered_models(
        settings, max(descs, key=len) if descs else None
    )
    pages = max(1, round(sum(len(d) for d in descs) / _PAGE_CHARS)) if descs else None
    sends = settings.llm.hosted_mode.disclosure_terms()["sends"]
    # The correction lines this score would send, and their digest, posted back by Send and try
    # so a change while the dialog is open is refused (issue #178, D-PRIV-670). For a group they
    # are the lines of exactly the roles the dialog shows.
    role_ids = [o.id for o in opps]
    correction_lines = await lines_for(s, role_ids)
    corrections_digest = await dialog_digest(s, role_ids)
    one = opps[0] if len(opps) == 1 else None
    return {
        "correction_lines": correction_lines,
        "corrections_digest": corrections_digest,
        "role": (one.title if one is not None else None) or "this role",
        "org": one.org_name if one is not None else None,
        "group_count": len(opps) if len(opps) > 1 else 0,
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


@router.post("/quarantine/{quarantine_id}/model", response_class=HTMLResponse,
    dependencies=[LOCAL_ONLY])
async def try_model(request: Request, quarantine_id: int):
    """Try with a different model, this run only (settings never change). A local pick queues
    at once. An online pick needs `confirmed=1`, which only the ask-first dialog sends, and is
    checked here too: Hosted mode must be on and the model must be one the dialog lists."""
    form = await request.form()
    pick = str(form.get("model") or "").strip()[:240]
    problem = await _pick_problem(form, pick)
    if problem:
        return await _row_response(request, quarantine_id, error=problem)
    return await _queue(request, quarantine_id, pick=pick, anyway=False, form=form)


async def _pick_problem(form: Any, pick: str) -> str | None:
    """Why a model pick cannot go ahead, or None. The one check a single role and a group both
    pass: a pick is required, and an online pick needs the dialog's `confirmed=1`, Hosted mode on
    and a model the dialog lists. Nothing is queued while this returns text."""
    if not pick:
        return "Pick a model first."
    if pick.startswith(HOSTED_PREFIX):
        if str(form.get("confirmed") or "") != "1":
            return "Nothing was sent. Confirm in the dialog first."
        async with sessionmaker()() as s:
            settings = await load_settings_with_db(s)
        from kestrel.services.setup_state import online_send_held, send_refusal

        why_not = online_send_held(settings)
        if why_not is not None:
            return send_refusal(
                why_not, "Nothing was sent. Online models need Hosted mode, which is off."
            )
        offered, _ = await _offered_models(settings, None)
        if pick.removeprefix(HOSTED_PREFIX) not in {m["id"] for m in offered}:
            return "Nothing was sent. That model is not on the online list."
    return None


CORRECTIONS_CHANGED = (
    "Nothing was sent. Your corrections changed since the dialog opened. Open Try a different "
    "model again to see what would be sent."
)


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
    operator's choice is recorded by Tier 1 when it runs (kestrel.services.tier0_override).

    The dialog posts no online confirmation. When scoring goes online, the role is handed to the
    Score again dialog instead, which lists what would be sent, including the correction lines,
    and binds them to the send (issue #178, D-PRIV-671). Nothing is queued here then."""
    form = await request.form()
    async with sessionmaker()() as s:
        q = await s.get(Quarantine, quarantine_id)
        opp = await s.get(Opportunity, q.opportunity_id) if q and q.opportunity_id else None
        set_aside = opp is not None and opp.stage == "t0_reject"
    if "online_approval" not in form:
        handoff = await _online_handoff(request, quarantine_id)
        if handoff is not None:
            return handoff
    # A role a rule set aside: skip the rule (D-UI-791). Any other role has no rule to skip, so
    # this runs the row's own step through the row's own plan (D-UI-790), with the same consent
    # checks as Try with a different model.
    return await _queue(request, quarantine_id, pick="same", anyway=set_aside, form=form)


ANYWAY_ASK = (
    "Nothing was sent. Scoring this role goes to an online model, so Score again shows what "
    "would be sent first."
)


async def _online_handoff(request: Request, quarantine_id: int) -> Response | None:
    """Score again for this failure's role when its scoring would go online, else None. The URL
    is built here from row ids, never from the form."""
    from urllib.parse import urlencode

    from fastapi.responses import RedirectResponse

    from kestrel.services.online_consent import approval_for

    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        q = await s.get(Quarantine, quarantine_id)
    if q is None or q.opportunity_id is None or not approval_for(settings, ("tier1", "tier2")):
        return None
    url = "/rescore/choose?" + urlencode({
        # No quarantine_id: skipping a rule is not a retry of the row, so it spends none (D-UI-791).
        "opp_ids": str(int(q.opportunity_id)), "page": "1", "next": "/quarantine",
    })
    if request.headers.get("HX-Request"):
        return HTMLResponse(f"<p>{ANYWAY_ASK}</p>", headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


async def _queue(
    request: Request, quarantine_id: int, *, pick: str, anyway: bool, form=None
) -> Response:
    """Queue the role behind `quarantine_id` for scoring through `enqueue_rescore`, counting the
    attempt on its quarantine row. `pick` is "same", a local model or `hosted:<id>`."""
    release_credit_hold(request)
    note: str | None = None
    try:
        async with sessionmaker()() as s:
            q = await s.get(Quarantine, quarantine_id)
            if q is None or q.opportunity_id is None:
                return await _row_response(request, quarantine_id,
                                           error="This failure has no role to score.")
            task_type: str | None = None
            if not anyway:
                # What runs is the row's own step (D-UI-790), never the role's stage. A refusal
                # queues nothing and spends no retry.
                plan = row_retry.plan(q, await s.get(Opportunity, q.opportunity_id))
                if plan.task is None:
                    return await _row_response(
                        request, quarantine_id, error=plan.why,
                        events={"k-toast": _toast(plan.why or "Not queued.", tone="warn")},
                    )
                task_type = plan.task
            settings = await load_settings_with_db(s)
            result, problem, as_toast = await _enqueue(
                s, settings, [q.opportunity_id], pick=pick, anyway=anyway, form=form,
                task_type=task_type,
            )
            if result is None:
                return await _row_response(
                    request, quarantine_id, error=problem,
                    events={"k-toast": _toast(problem or "Not queued.", tone="warn")}
                    if as_toast else None,
                )
            if not anyway:  # skipping a rule is not a retry of the row (D-UI-791)
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


async def _enqueue(
    s, settings: Any, opp_ids: list[int], *, pick: str, anyway: bool, form: Any,
    task_type: str | None = None,
) -> tuple[Any, str | None, bool]:
    """Queue `opp_ids` through `enqueue_rescore` with the pick's one-off model. The one path a
    single role and a group share, so the consent rules cannot differ between them: an online
    pick must pass `approval_problem` against the dialog's own approval before anything is
    queued. Returns (the result, None, False), or (None, why it was not queued, whether to also
    toast it). The caller commits."""
    from kestrel.services.online_consent import approval_problem

    override = None
    if pick != "same":
        override, _label, why = await _override_for(settings, pick)
        if override is None:
            return None, f"Not queued: {why}", True
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
        return None, "Nothing was sent. Confirm the online send first.", False
    # The correction lines the dialog listed, read again in the session that queues, bind the send
    # (D-PRIV-674): an approval with no or a stale digest sends nothing. For a group the lines are
    # those of exactly the roles being queued, the ones its dialog showed.
    from kestrel.learning.correction_lines import confirm_corrections
    from kestrel.services.online_consent import read_approval

    approval = read_approval((form or {}).get("online_approval"))
    confirmed = None
    if approval:
        confirmed = await confirm_corrections(s, opp_ids, (form or {}).get("corrections_digest"))
        if confirmed is None:
            return None, CORRECTIONS_CHANGED, False
    result = await enqueue_rescore(
        s, opp_ids, model_override=override, tier0_anyway=anyway,
        explain=rule_explainer(s, settings),
        online_approval=approval, corrections=confirmed, task_type=task_type,
    )
    return result, None, False


# ------------------------------------------------------------------------ remove and undo
@router.post("/quarantine/{quarantine_id}/remove", response_class=HTMLResponse)
async def remove(request: Request, quarantine_id: int):
    """Take the role off this list only. Nothing is deleted: the quarantine rows keep their raw
    replies and the role stays in Pipeline, not scored. Undo restores it."""
    async with sessionmaker()() as s:
        q = await s.get(Quarantine, quarantine_id)
        if q is None:
            return await _row_response(request, quarantine_id, error="This failure is gone.")
        if q.opportunity_id is None:
            # D-UI-224: a failure with no role is removed by its own row. Nothing is deleted.
            stamp = await retry_removal.mark_row(s, quarantine_id)
            await s.commit()
            return _gone(
                f"{ROW_PREFIX}{quarantine_id}",
                {
                    "k-toast": _toast(
                        "Removed from this list. Nothing was deleted.",
                        undo_url=f"/quarantine/{quarantine_id}/restore?at={quote(stamp)}",
                    ),
                },
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
        if q is not None:
            if q.opportunity_id is None:
                cleared = bool(at) and await retry_removal.clear_row(s, quarantine_id, at=at)
            else:
                cleared = bool(at) and await retry_removal.clear(s, q.opportunity_id, at=at)
            if not cleared:
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
    release_credit_hold(request)
    async with sessionmaker()() as s:
        rows = await _quarantine_rows(s, resolved_cap=0)
    views = _role_views([r for r in rows if not r["resolved"]])
    if ids:
        # A failure with no role has nothing to queue here (D-UI-224).
        targets = [v for v in views if v["id"] in ids and v["opp"] is not None]
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
        msg = ((f"Trying {queued} {'role' if queued == 1 else 'roles'} again. Scores appear here "
                "as they finish." + (f" {errors} could not be queued." if errors else ""))
               if queued else "Nothing was queued. Open a role to see why.")
    return HTMLResponse("".join(out), headers=_events({
        "k-toast": _toast(msg, tone="warn" if errors else None), "rt-refresh": True,
    }))


# ------------------------------------------------------------------- roles that failed alike
# The banner (D-UI-550 to D-UI-556). Every action takes the group's key, never a list of ids: the
# members are worked out again from the rows as they are now, so a stale page cannot widen it,
# and a group that has shrunk below the threshold is refused with nothing queued.
async def _group_card(
    request: Request, *, note: str | None = None, tone: str | None = None,
    undo_url: str | None = None,
) -> Response:
    """The whole card, as it is now, with a toast. A group action changes many rows at once."""
    ctx = await _list_context(request, "all")
    ctx.update(title="Needs another try", retry_error=None)
    headers = _events({"k-toast": _toast(note, undo_url=undo_url, tone=tone)}) if note else {}
    return templates().TemplateResponse(request, "partials/retry_card.html", ctx, headers=headers)


def _shown_members(g: dict | None, group_ids: str) -> list[dict]:
    """The members of `g` the banner named: those whose id is in `group_ids`, or every member when
    the request names none (a direct link). Never a role that is not in the group now."""
    if g is None:
        return []
    shown = retry_groups.parse_shown_ids(group_ids)
    return [m for m in g["members"] if not shown or m["opp"].id in shown]


def _roles_phrase(n: int) -> str:
    return f"{n} {'role' if n == 1 else 'roles'}"


@router.get("/quarantine/group/{key}/models", response_class=HTMLResponse)
async def group_models_menu(request: Request, key: str, group_ids: str = ""):
    """The model menu for a group: the same options as one role's, posting to the group. The roles
    it will act on are the ones the banner named (`group_ids`) that are still in the group."""
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        g = retry_groups.find(await _groups_now(s), key)
    members = _shown_members(g, group_ids)
    ctx = {
        "quarantine_id": 0, "dom_id": "rt-card", "has_role": bool(members),
        "group_ids": retry_groups.shown_ids(members) if members else "",
        "model_url": f"/quarantine/group/{quote(key, safe='')}/model",
        "online_url": f"/quarantine/group/{quote(key, safe='')}/online",
        "target": "#rt-card",
        **await _menu_options(settings),
    }
    return templates().TemplateResponse(request, "partials/retry_menu.html", ctx)


@router.get("/quarantine/group/{key}/online", response_class=HTMLResponse)
async def group_online_dialog(request: Request, key: str, group_ids: str = ""):
    """The ask-first dialog for a group: the same words as for one role, with the count. It shows
    the roles the banner named that are still in the group, and carries exactly those ids. Nothing
    leaves this computer until Send and try."""
    async with sessionmaker()() as s:
        g = retry_groups.find(await _groups_now(s), key)
        members = _shown_members(g, group_ids)
        if not members:
            return HTMLResponse("")
        ctx = await _online_ctx(s, [m["opp"] for m in members])
    ctx.update(
        group_ids=retry_groups.shown_ids(members),
        quarantine_id=0, dom_id="rt-card", target="#rt-card",
        model_url=f"/quarantine/group/{quote(key, safe='')}/model",
    )
    return templates().TemplateResponse(request, "partials/retry_online.html", ctx)


@router.post("/quarantine/group/{key}/model", response_class=HTMLResponse,
    dependencies=[LOCAL_ONLY])
async def group_model(request: Request, key: str):
    """Try the group with a different model, this run only. The model pick is checked as for one
    role (`_pick_problem`) and the queueing goes through the same `_enqueue`, so an online pick
    needs the dialog's confirmation and approval. Settings never change."""
    form = await request.form()
    pick = str(form.get("model") or "").strip()[:240]
    if pick == "same":
        pick = ""  # a group is only ever tried with a model the person chose
    problem = await _pick_problem(form, pick)
    if problem:
        return await _group_card(request, note=problem, tone="warn")
    release_credit_hold(request)
    shown = retry_groups.parse_shown_ids(form.get("group_ids"))
    note: str | None = None
    ok = False
    try:
        async with sessionmaker()() as s:
            g = retry_groups.find(await _groups_now(s), key)
            if g is None:
                return await _group_card(
                    request, tone="warn",
                    note="These roles changed since the page loaded. Nothing was queued.",
                )
            # Every pick, a free local one as much as an online one, acts on exactly the roles
            # the banner named: a role that joined the group since is left out, one that left is
            # skipped, and a request that shares no role with the banner queues nothing.
            members = [m for m in g["members"] if m["opp"].id in shown]
            if not members:
                return await _group_card(
                    request, tone="warn",
                    note="Nothing was queued. The roles in this group changed after the page "
                         "loaded. Look at them again.",
                )
            # Each member's task is its own row's step, read again here (D-UI-790). A group has
            # one step, so the plans agree; a member whose row cannot run now is left out.
            plans = {m["opp"].id: row_retry.plan(m["q"], m["opp"]).task for m in members}
            members = [m for m in members if plans[m["opp"].id] is not None]
            kinds = {plans[m["opp"].id] for m in members}
            if len(kinds) != 1:
                return await _group_card(
                    request, tone="warn",
                    note="Nothing was queued. The roles in this group changed after the page "
                         "loaded. Look at them again.",
                )
            settings = await load_settings_with_db(s)
            result, why, _toast_it = await _enqueue(
                s, settings, [m["opp"].id for m in members],
                pick=pick, anyway=False, form=form, task_type=kinds.pop(),
            )
            if result is None:
                return await _group_card(request, note=why or "Not queued.", tone="warn")
            for m in members:
                await _count_retry(s, str(m["id"]), result.queued)
            await s.commit()
            n = len(result.queued)
            ok = n > 0
            note = (f"Trying {_roles_phrase(n)} again with the model you chose."
                    if n else " ".join(result.messages()) or "Not queued. Open a role to see why.")
    except (SQLAlchemyError, OSError, ValueError) as exc:  # a write path never turns into a 500
        log.warning("quarantine_group_queue_failed", error_type=type(exc).__name__,
                    error=str(exc))
        note = f"Not queued: {plain_error(exc)}"
    return await _group_card(request, note=note, tone=None if ok else "warn")


@router.post("/quarantine/group/{key}/dismiss", response_class=HTMLResponse)
async def group_dismiss(request: Request, key: str):
    """Hide a group's banner until the group grows. Nothing else changes: every role stays in
    the list. The toast's Undo brings the banner back."""
    async with sessionmaker()() as s:
        groups = await _groups_now(s)
        g = retry_groups.find(groups, key)
        if g is None:
            return await _group_card(request)
        stamp = await retry_groups.dismiss(s, key, g["count"])
        # Markers of groups that are gone are cleaned up here, on a write, not on a page load.
        await retry_groups.prune_dismissed(s, {x["key"] for x in groups})
        await s.commit()
    return await _group_card(
        request, note="Hidden until more roles fail this way.",
        undo_url=f"/quarantine/group/{quote(key, safe='')}/undismiss?at={quote(stamp)}",
    )


@router.post("/quarantine/group/{key}/undismiss", response_class=HTMLResponse)
async def group_undismiss(request: Request, key: str, at: str = ""):
    async with sessionmaker()() as s:
        if not (at and await retry_groups.undo_dismiss(s, key, at=at)):
            return HTMLResponse(
                '<span data-undo-msg data-ok="0">This can no longer be undone.</span>'
            )
        await s.commit()
    return Response(status_code=204, headers=_events({"rt-reload": True}))
