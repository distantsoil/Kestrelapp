"""Pipeline's role panel and its actions (D-UI-96, SPEC-INTERFACES 2.1 /opportunities).

The list itself is `kestrel.web.routes_pages.opportunities`; this module is everything that
happens beside it:

  GET  /opportunities/{id}/panel          the panel that slides over the list (also /drawer)
  POST /opportunities/{id}/decide/...     shortlist, Not for me because..., put back, keep it
                                          anyway, hide a company, and the Undo of each
  POST /opportunities/{id}/lead           Consulting: a signal becomes a lead (existing fields)
  POST /pipeline/bulk/{action}            the dark bar: Shortlist, Not for me, Mark reviewed ...
  GET/POST /pipeline/views...             saved views

Actions answer with HX-Trigger events instead of markup, the way Needs another try does: `pl-gone`
folds rows out, `pl-close` closes the panel, `pipelineRefresh` asks the list again and `k-toast`
raises the toast with its Undo. Nothing here sends anything to anyone (hard rule 1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, or_, select

from kestrel.date_display import format_display
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Opportunity, Org, Pursuit, Setting, Task, VerdictOverride
from kestrel.services import hidden_orgs, saved_views
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.urls import safe_web_url
from kestrel.web import pipeline_view as pv
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import plain_error
from kestrel.web.fit import (
    EMPTY_POSTING_STAGE_REASON,
    GROUP_FAILED,
    GROUP_NEW,
    GROUP_UNSCORED,
    NOT_FIT,
    verdict_for_fit,
)
from kestrel.web.queries import (
    OppRow,
    _retry_removed,
    attach_rescoring,
    live_scoring_exists,
    saved_clause,
    scoring_states,
    scoring_task_exists,
)
from kestrel.web.words import words

log = structlog.get_logger()
router = APIRouter()

# The reason text of the agree label Mark reviewed writes; its Undo deletes only rows carrying it.
REVIEWED_REASON = "Reviewed in Pipeline."
# The most roles one bulk action touches (the list shows 25 a group; this is generous).
BULK_MAX = 200


# --------------------------------------------------------------------------- response helpers
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


def _answer(events: dict[str, Any], extra: str = "") -> Response:
    """An action's answer: nothing to swap, only events (and any out-of-band nav counts)."""
    return HTMLResponse(extra, headers={"HX-Reswap": "none", **_events(events)})


def _plural(n: int, one: str = "role", many: str = "roles") -> str:
    return f"{n} {one if n == 1 else many}"


def parse_ids(raw: str | list[str] | None) -> list[int]:
    """Role ids from "1,2,3" (or repeated values): positive integers, no repeats, capped."""
    parts: list[str] = []
    for chunk in raw if isinstance(raw, list) else [raw or ""]:
        parts.extend(str(chunk).split(","))
    out: list[int] = []
    for p in parts:
        p = p.strip()
        if p.isdigit() and int(p) > 0 and int(p) not in out:
            out.append(int(p))
    return out[:BULK_MAX]


# ---------------------------------------------------------------------------------- the panel
async def _flags(s, opp_id: int) -> tuple[bool, bool, bool, bool]:
    """(a scoring task can run now, the role was removed from Needs another try, saved, any
    scoring task is queued, held or not)."""
    row = (
        await s.execute(
            select(
                live_scoring_exists(),
                _retry_removed(Opportunity.id),
                saved_clause(),
                scoring_task_exists(),
            ).where(Opportunity.id == opp_id)
        )
    ).one_or_none()
    if not row:
        return False, False, False, False
    return bool(row[0]), bool(row[1]), bool(row[2]), bool(row[3])


def _panel_kind(opp, fit) -> str:
    if opp.opp_type == "trigger_event":
        return "signal"
    if fit.group in (GROUP_NEW, GROUP_UNSCORED, GROUP_FAILED):
        return "pending"
    if opp.stage in ("t0_reject", "t1_reject") and fit.group == "not_fit":
        return "nf"
    return "role"


async def _profile_thin(track: str) -> bool:
    import asyncio

    from kestrel.services.profile_readiness import operator_profile_is_thin

    # The operator's own profile.yml only: the fictional example is not "a profile".
    return await asyncio.to_thread(operator_profile_is_thin, track)


async def panel_context(s, opp_id: int) -> dict[str, Any] | None:
    """Everything partials/pl_panel.html prints for one role. Prose comes from the redacted
    copies `_detail_context` builds (confidential mode), never the raw ORM fields."""
    from kestrel.web.fit import fit_for
    from kestrel.web.queries import latest_evaluation, row_fit
    from kestrel.web.routes_pages import _detail_context, watch_lines

    opp0 = await s.get(Opportunity, opp_id)
    if opp0 is None:
        return None
    live, removed, saved, queued = await _flags(s, opp_id)
    ev0 = await latest_evaluation(s, opp_id)
    # Where an unscored role is in the queue (running, queued, failed, removed ...), before the
    # detail context edits any ORM field for display.
    probe = OppRow(
        opp=opp0, ev=ev0, org_name=None, source_name=None, removed=removed,
        fit=fit_for(ev0, opp0.stage, live=live, stage_reason=opp0.stage_reason,
                    removed=removed, hidden_applied=opp0.archive_reason == "applied",
                    queued=queued),
    )
    await scoring_states(s, [probe])
    await attach_rescoring(s, [probe])
    detail = await _detail_context(s, opp_id)
    if detail is None:
        return None
    opp, ev, ev_json = detail["opp"], detail["ev"], detail["ev_json"]
    pursuit = detail.get("pursuit")
    open_pursuit = pursuit is not None and pursuit.closed_at is None
    override = detail.get("override")
    fit = row_fit(
        opp, ev, pursued=open_pursuit,
        pursuit_status=pursuit.status if pursuit is not None else None,
        operator_verdict=override.operator_verdict if override is not None else None,
        live=live, removed=removed, queued=queued,
    )
    kind = _panel_kind(opp, fit)
    track_words = words(opp.track)
    ctx: dict[str, Any] = {
        "kind": kind,
        "opp": opp,
        "ev": ev,
        "fit": fit,
        "title": detail["title_display"],
        "org_name": detail["org_name"],
        "place": pv.place_text(opp),
        "w": track_words,
        "consulting": opp.track == "consulting",
        "on_list": open_pursuit,
        "hidden": bool(opp.archived_at),
        "saved": saved,
        "removed": removed,
        "scoring": probe.scoring,
        "rescoring": probe.rescoring,
        "failed_reason": probe.failed_reason,
        "hidden_why": probe.hidden_why,
        "posted": pv.posted_words(opp.date_posted),
        "liveness": pv.liveness_fact(opp),
        "open_href": f"/opportunities/{opp.id}",
        "posting_url": safe_web_url(opp.url),
        "reasons_list": pv.reasons_for(opp.track),
        "ai_disclaimer": detail.get("ai_disclaimer"),
        "tone": pv.ring_tone(fit.word),
        # "You asked to score this despite your rule on ..." once Score anyway has run (D-UI-74).
        "override_note": detail.get("override_note"),
    }
    if kind == "role":
        watch = list(
            detail.get("watch") or watch_lines(ev_json.get("legitimacy"), opp.ghost_score, opp)
        )
        warn = pv.warning_text(opp, ev)
        if warn and not watch:
            watch = [warn]
        ctx.update(
            score=fit.score,
            ring_pct=round((fit.score or 0) / 5 * 100),
            dims=pv.dimension_rows(ev_json),
            pay=pv.pay_box(opp, ev_json),
            level=pv.level_box(opp, ev_json),
            reasons=pv.fit_reasons(ev_json),
            gaps=pv.gap_rows(ev_json, thin=await _profile_thin(opp.track)),
            lead=pv.lead_with(opp, ev_json),
            summary=pv.summary_text(opp, ev_json),
            watch=watch,
            model_word=detail.get("model_word"),
            rules_word=detail.get("rules_word"),
        )
    elif kind == "nf":
        ctx.update(
            rule_why=detail.get("rule_why"),
            excerpt=_excerpt(detail.get("description_display")),
        )
    elif kind == "signal":
        ctx.update(_signal_context(opp, ev, ev_json, detail))
    return ctx


def _excerpt(text: str | None, limit: int = 600) -> str:
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _signal_context(opp, ev, ev_json, detail) -> dict[str, Any]:
    """What a Consulting signal's panel shows: what happened, why it may need outside help, who to
    approach, the opening move. The model's note is labeled by the template."""
    from kestrel.web.queries import signal_type
    from kestrel.web.redact import redact_text

    td = opp.trigger_data or {}
    mapping, conf = detail.get("mapping"), bool(detail.get("confidential"))

    def clean(value: Any) -> str:
        return redact_text(" ".join(str(value or "").split()), mapping, enabled=conf)

    blocks = (ev_json or {}).get("blocks") or {}
    approach = blocks.get("e_approach") if isinstance(blocks.get("e_approach"), dict) else {}
    situation = blocks.get("a_situation") if isinstance(blocks.get("a_situation"), dict) else {}
    buyers = blocks.get("c_buyers") if isinstance(blocks.get("c_buyers"), dict) else {}
    key, label = signal_type(opp)
    score = None
    if ev is not None:
        score = ev.score if ev.score is not None else ev.weighted_score
    source_url = td.get("source_url")
    return {
        "sig": {
            "type_key": key,
            "type_label": label,
            "happened": clean(opp.title) or clean(td.get("evidence_quote")),
            "evidence": clean(td.get("evidence_quote")),
            "source_url": source_url if isinstance(source_url, str) and source_url.startswith(
                ("http://", "https://")) and not conf else None,
            "when": format_display(opp.event_date),
            "why": clean(td.get("implied_need")) or clean(situation.get("inferred")),
            "who": clean(approach.get("to_whom")) or clean(buyers.get("economic_buyer")),
            "move": clean(approach.get("opening_move")),
            "score": score,
            "ring_pct": round((score or 0) / 5 * 100),
            "tone": "good" if (score or 0) >= 3.5 else "warn",
            "word": "Worth researching" if (score or 0) >= 3.5 else "Weak signal",
        },
        "reasons_list": pv.reasons_for(opp.track, signal=True),
        "org_href": f"/orgs/{opp.org_id}" if opp.org_id else f"/opportunities/{opp.id}",
    }


async def render_panel(request: Request, opp_id: int) -> str:
    """The panel's inner HTML (an empty-state message when the role is gone)."""
    async with sessionmaker()() as s:
        ctx = await panel_context(s, opp_id)
    if ctx is None:
        return templates().get_template("partials/pl_panel.html").render(
            request=request, kind="missing"
        )
    return templates().get_template("partials/pl_panel.html").render(request=request, **ctx)


@router.get("/opportunities/{opp_id}/panel", response_class=HTMLResponse)
@router.get("/opportunities/{opp_id}/drawer", response_class=HTMLResponse)
async def role_panel(request: Request, opp_id: int):
    """The panel for one role. Read only: nothing here writes."""
    html = await render_panel(request, opp_id)
    return HTMLResponse(html, status_code=200 if 'data-kind="missing"' not in html else 404)


# ---------------------------------------------------------------------------- single-role actions
async def _panel_after(request: Request, opp_id: int, events: dict[str, Any]) -> Response:
    """Re-render the panel after an action that leaves the role on the list (Shortlist)."""
    html = await render_panel(request, opp_id)
    return HTMLResponse(html, headers=_events({"pipelineRefresh": True, **events}))


async def _oob_counts(s) -> str:
    from kestrel.web.routes_api import oob_nav_counts

    return await oob_nav_counts(s)


@router.post("/opportunities/{opp_id}/decide/shortlist", response_class=HTMLResponse)
async def decide_shortlist(request: Request, opp_id: int):
    """Shortlist (Add to leads in Consulting). Undo takes the pursuit back out, but only when
    this click made it."""
    from kestrel.web.pages.opportunity_actions import add_to_list

    toast: dict[str, Any] = {}
    async with sessionmaker()() as s:
        try:
            opp = await s.get(Opportunity, opp_id)
            if opp is None:
                return _answer({"k-toast": _toast("That role is gone.", tone="warn")})
            existed = (
                await s.execute(select(Pursuit.id).where(Pursuit.opportunity_id == opp_id))
            ).first() is not None
            pursuit, text = await add_to_list(s, opp)
            if pursuit is None:
                toast = _toast(text, tone="warn")
            else:
                where = words(opp.track)["list"]
                toast = _toast(
                    text if existed else f"Added to your {where}.",
                    None
                    if existed
                    else f"/opportunities/{opp_id}/decide/unshortlist"
                    f"?token={s.info['added_undo'][opp_id]}",
                )
        except Exception as exc:  # a write path must never 500 (CLAUDE.md hard rules)
            log.warning("pipeline_shortlist_failed", opp_id=opp_id, error=str(exc))
            await s.rollback()
            toast = _toast(f"Could not add it: {plain_error(exc)}", tone="warn")
        oob = await _oob_counts(s)
    html = await render_panel(request, opp_id)
    return HTMLResponse(html + oob, headers=_events({"pipelineRefresh": True, "k-toast": toast}))


NO_UNDO_HTML = '<span data-undo-msg data-ok="0">This can no longer be undone.</span>'


async def _undo_new_pursuit(s, opp: Opportunity, token: str = "") -> bool:
    from kestrel.services.pursuit_undo import undo_added

    return await undo_added(s, token, opp.id)


@router.post("/opportunities/{opp_id}/decide/unshortlist", response_class=HTMLResponse)
async def decide_unshortlist(request: Request, opp_id: int):

    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        done = opp is not None and await _undo_new_pursuit(
            s, opp, request.query_params.get("token", "")
        )
        await s.commit()
        oob = await _oob_counts(s)
    # A refused Undo says so: the toast reads the marker (kestrel-ui.js).
    return _answer(
        {"pipelineRefresh": True, "pl-panel-refresh": opp_id}, oob if done else NO_UNDO_HTML + oob
    )


async def hide_with_reason(
    s, opp: Opportunity, key: str | None, *, signal: bool = False, bulk: bool = False,
    at: str | None = None,
) -> int | None:
    """Hide a role (Not for me) and record why as the operator's label; returns the label's id,
    for Undo. A fit reason is a pass the ranker learns from, with the dimension it taught
    (`dimension_corrections`; `label.dimensions` holds scores the calibration reads, so reasons
    never go there). "Just not now" teaches nothing: its label carries no verdict or score. A
    bulk Not for me has no reason to give, so it hides and writes no label at all."""
    from kestrel.web.queries import record_override

    stamp = at or utcnow_iso()
    if bulk:
        opp.archived_at = stamp
        opp.archive_reason = "not_interested"
        return None
    reason = pv.reason_by_key(opp.track, key, signal=signal) or pv.reason_by_key(
        opp.track, "later", signal=signal
    )
    rkey, text, dimension = reason
    opp.archived_at = stamp
    opp.archive_reason = "later" if rkey == "later" else "not_interested"
    if rkey == "later":
        return None  # says nothing about the role: no label, so no earlier word is hidden
    verdict, score = verdict_for_fit(NOT_FIT, opp.track)
    label = await record_override(
        s, opp_id=opp.id, mode="override", operator_verdict=verdict, operator_score=score,
        reason=f"{'Not relevant' if signal else 'Not for me'}: {text}.",
        dimension_corrections={"reason": rkey, "dimension": dimension}, commit=False,
    )
    return label.id if label is not None else None


@router.post("/opportunities/{opp_id}/decide/not-for-me", response_class=HTMLResponse)
async def decide_not_for_me(request: Request, opp_id: int, reason: str = ""):
    """One click on one of the four reasons hides the role and records why (D-UI-96)."""
    async with sessionmaker()() as s:
        try:
            opp = await s.get(Opportunity, opp_id)
            if opp is None:
                return _answer({"k-toast": _toast("That role is gone.", tone="warn")})
            signal = opp.opp_type == "trigger_event"
            if opp.archived_at:
                return _answer({"pl-gone": {"ids": [opp_id]}, "pl-close": opp_id})
            label_id = await hide_with_reason(s, opp, reason, signal=signal)
            stamp = opp.archived_at or ""
            await s.commit()
            oob = await _oob_counts(s)
        except Exception as exc:  # a write path must never 500 (CLAUDE.md hard rules)
            log.warning("pipeline_not_for_me_failed", opp_id=opp_id, error=str(exc))
            await s.rollback()
            note = f"Could not hide it: {plain_error(exc)}"
            return _answer({"k-toast": _toast(note, tone="warn")})
    undo = f"/opportunities/{opp_id}/decide/undo-hide?at={quote(stamp)}" + (
        f"&label={label_id}" if label_id else ""
    )
    msg = (
        "Marked not relevant. Thanks, that helps Kestrel learn."
        if signal else "Hidden. Thanks, that helps Kestrel learn. Find it under Hidden."
    )
    return _answer(
        {"pl-gone": {"ids": [opp_id]}, "pl-close": opp_id, "k-toast": _toast(msg, undo)}, oob
    )


async def unhide(s, opp_ids: list[int], label_ids: list[int], at: str = "") -> int:
    """Undo a hide: the role comes back and the label that click wrote goes with it, so a
    mistaken click teaches nothing. Only labels this action wrote are removed, and only roles
    this action archived: `at` is the archive stamp the action wrote, so a role the operator
    hid again separately afterward (a different stamp) is left alone. Returns how many roles
    came back."""
    if not at:
        return 0
    restored: list[int] = []
    candidates = list(
        (
            await s.execute(
                select(Opportunity).where(
                    Opportunity.id.in_(opp_ids),
                    Opportunity.archive_reason.in_(("not_interested", "later")),
                    Opportunity.archived_at == at,
                )
            )
        ).scalars()
    )
    if len(candidates) != len(set(opp_ids)):
        return 0
    for opp in candidates:
        opp.archived_at = None
        opp.archive_reason = None
        restored.append(opp.id)
    if label_ids and restored:
        await s.execute(
            delete(VerdictOverride).where(
                VerdictOverride.id.in_(label_ids),
                VerdictOverride.opportunity_id.in_(restored),
                or_(
                    VerdictOverride.reason.like("Not for me:%"),
                    VerdictOverride.reason.like("Not relevant:%"),
                ),
            )
        )
    await s.commit()
    return len(restored)


@router.post("/opportunities/{opp_id}/decide/undo-hide", response_class=HTMLResponse)
async def decide_undo_hide(request: Request, opp_id: int, label: str = "", at: str = ""):
    async with sessionmaker()() as s:
        n = await unhide(s, [opp_id], parse_ids(label), at)
        oob = await _oob_counts(s)
    return _answer({"pipelineRefresh": True}, oob if n else NO_UNDO_HTML + oob)


@router.post("/opportunities/{opp_id}/decide/put-back", response_class=HTMLResponse)
async def decide_put_back(request: Request, opp_id: int):
    """The Hidden chip's way back: the role returns to Pipeline. The label stays: it was the
    operator's word at the time."""
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        if opp is not None and opp.archived_at:
            opp.archived_at = None
            opp.archive_reason = None
            await s.commit()
        oob = await _oob_counts(s)
    return _answer(
        {"pl-gone": {"ids": [opp_id]}, "pl-close": opp_id,
         "k-toast": _toast("Back in Pipeline.")}, oob,
    )


# Shown when pasted text is too short to score; the same bar the pipeline holds a scraped
# posting to (pipeline.tier2.is_stub_posting).
PASTE_TOO_SHORT = "That is too short to score. Paste the whole posting."
PASTE_MAX_CHARS = 50_000
PASTE_TOO_LONG = "That is too long to paste. Keep it under 50,000 characters."
PASTE_HIDDEN = "Unhide it first, then paste the description."
_PASTE_TEXT = {
    "description_hidden": PASTE_HIDDEN,
    "description_short": PASTE_TOO_SHORT,
    "description_long": PASTE_TOO_LONG,
    "description_has": "This role already has its description.",
    "description_gone": "That role is gone.",
}


@router.post("/opportunities/{opp_id}/description", response_class=HTMLResponse)
async def paste_description(request: Request, opp_id: int):
    """Paste the description for a posting that came with none (issue #90, D-UI-236). The text is
    stored on the role the way a re-scrape that brings a body stores it
    (ingest.normalize.attach_description), and the role goes back to `new`, so Tier 0 runs again
    on it with your rules and the normal pipeline scores it (the same reset the MCP re-evaluation
    and Show it anyway use). Only an active role with no description takes it, and the text is
    capped at PASTE_MAX_CHARS. The Pipeline panel posts with htmx and gets a toast and the panel;
    the role page posts a plain form (`back=role`) and gets a redirect with a notice. Nothing is
    fetched or sent anywhere (hard rule 1); the text stays on this computer until you score it
    online."""
    from types import SimpleNamespace

    from kestrel.ingest.normalize import attach_description
    from kestrel.pipeline.tier2 import is_stub_posting
    from kestrel.services import run_model
    from kestrel.web.flash import flash_redirect

    form = await request.form()
    text = str(form.get("text") or "").strip()
    to_role = str(form.get("back") or "") == "role"

    def refuse(code: str) -> Response:
        if to_role:
            return flash_redirect(f"/opportunities/{opp_id}", code)
        return _answer({"k-toast": _toast(_PASTE_TEXT[code], tone="warn")})

    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        if opp is None:
            return refuse("description_gone")
        if opp.archived_at:
            return refuse("description_hidden")
        if not (opp.stage == "t1_pass" and opp.stage_reason == EMPTY_POSTING_STAGE_REASON):
            return refuse("description_has")
        if len(text) > PASTE_MAX_CHARS:
            return refuse("description_long")
        stand_in = SimpleNamespace(trigger_data=None, event_date=None, opp_type=opp.opp_type,
                                   description_md=text)
        if is_stub_posting(stand_in):
            return refuse("description_short")
        await attach_description(s, opp, text)
        opp.stage = "new"
        opp.stage_reason = None
        # The configured models score it: no one-run pick rides along.
        await run_model.discard(s, opp_id)
        s.add(Task(type="embed", payload={"opportunity_id": opp_id}))
        await s.commit()
    if to_role:
        return flash_redirect(f"/opportunities/{opp_id}", "description_saved")
    return await _panel_after(
        request, opp_id,
        {"k-toast": _toast("Description saved. Kestrel will check it against your rules and "
                           "score it.")},
    )


@router.post("/opportunities/{opp_id}/decide/save", response_class=HTMLResponse)
async def decide_save(request: Request, opp_id: int):
    """Save for later: a bookmark under the Saved chip, with no decision, label or pursuit
    (kestrel.services.saved_roles). Undo takes the bookmark off."""
    from kestrel.services import saved_roles

    async with sessionmaker()() as s:
        if await s.get(Opportunity, opp_id) is None:
            return _answer({"k-toast": _toast("That role is gone.", tone="warn")})
        added = await saved_roles.save(s, opp_id)
        await s.commit()
        row = await s.get(Setting, saved_roles.key_for(opp_id))
        stamp = row.value["at"]
    undo = f"/opportunities/{opp_id}/decide/unsave?at={quote(stamp)}" if added else None
    msg = "Saved for later. Find it under Saved." if added else "Already saved."
    return _answer({"pipelineRefresh": True, "k-toast": _toast(msg, undo)})


@router.post("/opportunities/{opp_id}/decide/unsave", response_class=HTMLResponse)
async def decide_unsave(request: Request, opp_id: int):
    from kestrel.services import saved_roles

    async with sessionmaker()() as s:
        stamp = request.query_params.get("at")
        if stamp is not None:
            row = await s.get(Setting, saved_roles.key_for(opp_id))
            if row is None or row.value.get("at") != stamp:
                return _answer({}, NO_UNDO_HTML)
        await saved_roles.unsave(s, opp_id)
        await s.commit()
    return _answer({"pipelineRefresh": True})


@router.post("/opportunities/{opp_id}/decide/keep-anyway", response_class=HTMLResponse)
async def decide_keep_anyway(request: Request, opp_id: int):
    """Keep it anyway, on a role the rules set aside (D-UI-62, D-UI-74): it is scored with the
    rules check skipped, so it moves to Being scored. The operator's rules are not changed."""
    from kestrel.queue.rescore import enqueue_rescore
    from kestrel.web.pages.rescore import rule_explainer

    note: str | None = None
    form = await request.form()
    async with sessionmaker()() as s:
        try:
            settings = await load_settings_with_db(s)
            from kestrel.services.online_consent import approval_problem
            problem = await approval_problem(s, settings, ("tier1", "tier2"), form)
            if problem:
                return _answer({
                    "k-toast": _toast(
                        "Nothing was sent. Confirm the online scoring first.", tone="warn"
                    )
                })
            result = await enqueue_rescore(
                s, [opp_id], tier0_anyway=True, score_stubs=True,
                explain=rule_explainer(s, settings),
                online_approval=form.get("online_approval"),
            )
            await s.commit()
            if not (result.queued or result.already):
                note = " ".join(result.messages()) or "Not queued. Open the role to see why."
        except Exception as exc:  # a write path never turns into a 500 (CLAUDE.md)
            log.warning("pipeline_keep_anyway_failed", opp_id=opp_id, error=str(exc))
            await s.rollback()
            note = f"Not queued: {plain_error(exc)}"
    if note:
        return _answer({"k-toast": _toast(note, tone="warn")})
    return _answer({
        "pl-gone": {"ids": [opp_id]}, "pl-close": opp_id,
        "k-toast": _toast("Kept. Kestrel will score it with the AI."),
    })


@router.post("/opportunities/{opp_id}/decide/hide-company", response_class=HTMLResponse)
async def decide_hide_company(request: Request, opp_id: int):
    """Hide this company from a row (D-UI-123): every role of it leaves Pipeline and Today, with
    Undo. Nothing is deleted and scoring still runs."""
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        if opp is None or opp.org_id is None:
            return _answer({"k-toast": _toast("This role has no company to hide.", tone="warn")})
        org = await s.get(Org, opp.org_id)
        name = org.name if org is not None else "the company"
        if (await load_settings_with_db(s)).confidential_mode:
            from kestrel.web.redact import redact_name, redaction_map

            name = redact_name(name, await redaction_map(s), enabled=True)
        already = org is not None and org.hidden_at is not None
        await hidden_orgs.hide_org(s, opp.org_id)
        await s.commit()
        org_id = opp.org_id
        stamp = org.hidden_at if org is not None else None
        oob = await _oob_counts(s)
    if already or not stamp:
        # This click changed nothing, so it offers nothing to undo: an earlier, deliberate hide
        # must not be reversed by it.
        return _answer({"pipelineRefresh": True, "pl-close": opp_id,
                        "k-toast": _toast(f"{name} was already hidden.")}, oob)
    return _answer({
        "pipelineRefresh": True, "pl-close": opp_id,
        "k-toast": _toast(f"{name} hidden. Its roles leave Pipeline.",
                          f"/opportunities/{opp_id}/decide/unhide-company?org={org_id}"
                          f"&at={quote(stamp)}"),
    }, oob)


@router.post("/opportunities/{opp_id}/decide/unhide-company", response_class=HTMLResponse)
async def decide_unhide_company(request: Request, opp_id: int, org: int = 0, at: str = ""):
    """Undo of Hide this company: only while the company still carries the stamp this click
    wrote (`at`), so a later or earlier hide of the same company is left alone."""
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        target = org or (opp.org_id if opp is not None else None)
        done = False
        if target:
            row = await s.get(Org, int(target))
            if row is not None and (not at or row.hidden_at == at):
                await hidden_orgs.unhide_org(s, int(target))
                await s.commit()
                done = True
        oob = await _oob_counts(s)
    return _answer({"pipelineRefresh": True}, oob if done else NO_UNDO_HTML + oob)


# --------------------------------------------------------------------------------- consulting
_DUE_DAYS = {"today": 0, "week": 3, "next": 7}


def due_date(choice: str, today: datetime | None = None) -> str:
    """The day a lead's first step is due, on the operator's clock: today, three days on for
    "this week" and a week on for "next week"."""
    base = (today or datetime.now(UTC)).astimezone().date()
    return (base + timedelta(days=_DUE_DAYS.get(choice, _DUE_DAYS["week"]))).isoformat()


@router.post("/opportunities/{opp_id}/lead", response_class=HTMLResponse)
async def add_lead(request: Request, opp_id: int):
    """Add to leads, from a signal's inline form: a pursuit from existing fields only (status,
    next_action, next_action_at). No contacts, stages or amounts: Kestrel is not a CRM, and
    nothing is sent to anyone."""
    from kestrel.web.pages.opportunity_actions import add_to_list

    form = await request.form()
    step = " ".join(str(form.get("step") or "").split())[:200] or "Research the company"
    due = str(form.get("due") or "week")
    toast: dict[str, Any]
    async with sessionmaker()() as s:
        try:
            opp = await s.get(Opportunity, opp_id)
            if opp is None:
                return _answer({"k-toast": _toast("That signal is gone.", tone="warn")})
            existed = (
                await s.execute(select(Pursuit.id).where(Pursuit.opportunity_id == opp_id))
            ).first() is not None
            pursuit, text = await add_to_list(s, opp)
            if pursuit is None:
                toast = _toast(text, tone="warn")
            elif existed:
                toast = _toast(text)  # already a lead: nothing was made, so nothing to undo
            else:
                if not pursuit.next_action:
                    pursuit.next_action = step
                    pursuit.next_action_at = due_date(due)
                from kestrel.services import pursuit_undo

                token = s.info["added_undo"][opp_id]
                row = await s.get(Setting, pursuit_undo.PREFIX + token)
                row.value = {**row.value, "pursuit": pursuit_undo.row_dict(pursuit)}
                await s.commit()
                label = {"today": "today", "next": "next week"}.get(due, "this week")
                toast = _toast(
                    f"Added to Leads. Next step: {step}, {label}.",
                    f"/opportunities/{opp_id}/decide/unshortlist?token={token}",
                )
        except Exception as exc:  # a write path must never 500 (CLAUDE.md hard rules)
            log.warning("pipeline_add_lead_failed", opp_id=opp_id, error=str(exc))
            await s.rollback()
            toast = _toast(f"Could not add it: {plain_error(exc)}", tone="warn")
        oob = await _oob_counts(s)
    html = await render_panel(request, opp_id)
    return HTMLResponse(html + oob, headers=_events({"pipelineRefresh": True, "k-toast": toast}))


# ------------------------------------------------------------------------------------ bulk bar
@router.post("/pipeline/bulk/{action}", response_class=HTMLResponse)
async def bulk(request: Request, action: str):
    """The dark bar under the list (D-UI-96). `ids` is a comma list. Shortlist and Not for me
    carry an Undo; Score again goes through the same model choice every Score again does;
    Deeper read needs a per-role OK, so it only says so."""
    form = await request.form()
    ids = parse_ids(str(form.get("ids") or ""))
    if not ids:
        return _answer({"k-toast": _toast("Pick one or more roles first.", tone="warn")})
    handler = {
        "shortlist": _bulk_shortlist,
        "not-for-me": _bulk_not_for_me,
        "mark-reviewed": _bulk_reviewed,
    }.get(action)
    if action == "deeper-read":
        return _answer({"k-toast": _toast(
            "Deeper read needs your OK for each role: Kestrel shows exactly what it sends first. "
            "Open a role and choose Deeper read with a stronger model there.")})
    if handler is None:
        return _answer({"k-toast": _toast("That action is not available.", tone="warn")}, "")
    try:
        async with sessionmaker()() as s:
            events = await handler(s, ids)
            oob = await _oob_counts(s)
    except Exception as exc:  # a write path never turns into a 500 (CLAUDE.md)
        log.warning("pipeline_bulk_failed", action=action, error=str(exc))
        return _answer({"k-toast": _toast(f"Could not finish: {plain_error(exc)}", tone="warn")})
    return _answer(events, oob)


async def _bulk_shortlist(s, ids: list[int]) -> dict[str, Any]:
    from kestrel.web.pages.opportunity_actions import add_to_list

    have = set(
        (
            await s.execute(select(Pursuit.opportunity_id).where(Pursuit.opportunity_id.in_(ids)))
        ).scalars()
    )
    added: list[int] = []
    for opp in (
        (await s.execute(select(Opportunity).where(Opportunity.id.in_(ids)))).scalars().all()
    ):
        if opp.id in have:
            continue
        pursuit, _text = await add_to_list(s, opp)
        if pursuit is not None:
            added.append(opp.id)
    where = "leads" if await _all_consulting(s, ids) else "shortlist"
    msg = f"{_plural(len(added))} added to your {where}." if added else "Already on your list."
    undo = (
        (
            f"/pipeline/bulk-undo/shortlist?ids={','.join(map(str, added))}&tokens="
            + ",".join(s.info["added_undo"][oid] for oid in added)
        )
        if added
        else None
    )
    return {"pipelineRefresh": True, "pl-clear": True, "k-toast": _toast(msg, undo)}


async def _all_consulting(s, ids: list[int]) -> bool:
    tracks = set(
        (await s.execute(select(Opportunity.track).where(Opportunity.id.in_(ids)))).scalars()
    )
    return tracks == {"consulting"}


async def _bulk_not_for_me(s, ids: list[int]) -> dict[str, Any]:
    hidden: list[int] = []
    stamp = utcnow_iso()  # one stamp for the whole action: Undo restores only what carries it
    for opp in (
        await s.execute(select(Opportunity).where(Opportunity.id.in_(ids)))
    ).scalars().all():
        if opp.archived_at:
            continue
        await hide_with_reason(s, opp, None, bulk=True, at=stamp)
        hidden.append(opp.id)
    await s.commit()
    undo = (
        f"/pipeline/bulk-undo/not-for-me?ids={','.join(map(str, hidden))}&at={quote(stamp)}"
        if hidden else None
    )
    return {
        "pl-gone": {"ids": hidden}, "pl-clear": True,
        "k-toast": _toast(f"{_plural(len(hidden))} moved to Hidden.", undo),
    }


async def _bulk_reviewed(s, ids: list[int]) -> dict[str, Any]:
    """Mark reviewed: the operator looked and agrees with the read, recorded as an agree label
    (the same one Calibrate writes). A role with no score has nothing to agree with."""
    from kestrel.web.queries import latest_evaluation, record_override

    done: list[int] = []
    skipped = 0
    for opp_id in ids:
        ev = await latest_evaluation(s, opp_id)
        if ev is None or ev.tier < 2 or ev.verdict is None:
            skipped += 1
            continue
        again = (
            await s.execute(
                select(VerdictOverride.id).where(
                    VerdictOverride.opportunity_id == opp_id,
                    VerdictOverride.evaluation_id == ev.id,
                ).limit(1)
            )
        ).first()
        if again is not None:  # the operator already gave a word on this read
            continue
        row = await record_override(
            s, opp_id=opp_id, mode="agree", reason=REVIEWED_REASON, commit=False
        )
        if row is not None:
            done.append(row.id)
    await s.commit()
    msg = f"{_plural(len(done))} marked reviewed."
    if skipped:
        msg += f" {_plural(skipped)} not scored yet."
    undo = f"/pipeline/bulk-undo/mark-reviewed?labels={','.join(map(str, done))}" if done else None
    return {"pipelineRefresh": True, "pl-clear": True, "k-toast": _toast(msg, undo)}


@router.post("/pipeline/bulk-undo/{action}", response_class=HTMLResponse)
async def bulk_undo(request: Request, action: str, ids: str = "", labels: str = "", at: str = ""):
    """Undo of a bulk action. Only the roles and labels that action itself touched are named in
    the URL, so an Undo never reaches anything it did not make."""
    role_ids, label_ids = parse_ids(ids), parse_ids(labels)
    done = 0
    async with sessionmaker()() as s:
        if action == "shortlist":
            tokens = request.query_params.get("tokens", "").split(",")
            if len(tokens) != len(role_ids):
                return _answer({}, NO_UNDO_HTML)
            for oid, token in zip(role_ids, tokens, strict=True):
                opp = await s.get(Opportunity, oid)
                if opp is None or not await _undo_new_pursuit(s, opp, token):
                    await s.rollback()
                    return _answer({}, NO_UNDO_HTML)
                done += 1
            await s.commit()
        elif action == "not-for-me":
            done = await unhide(s, role_ids, label_ids, at)
        elif action == "mark-reviewed" and label_ids:
            res = await s.execute(
                delete(VerdictOverride).where(
                    VerdictOverride.id.in_(label_ids), VerdictOverride.reason == REVIEWED_REASON
                )
            )
            done = res.rowcount or 0
            await s.commit()
        oob = await _oob_counts(s)
    return _answer({"pipelineRefresh": True}, oob if done else NO_UNDO_HTML + oob)


# ------------------------------------------------------------------------------- saved views
def _view_url(params: dict[str, str], track: str | None) -> str:
    from urllib.parse import urlencode

    return "/opportunities?" + urlencode({"track": track or "", **params})


@router.get("/pipeline/views", response_class=HTMLResponse)
async def views_menu(request: Request):
    """The View dropdown: the track's saved views, default first, with Make default and Remove."""
    from kestrel.web.deps import active_track

    track = active_track(request)
    async with sessionmaker()() as s:
        views = await saved_views.list_views(s, track)
    items = [
        {
            "id": v.id, "name": v.name, "default": bool(v.is_default),
            "href": _view_url({"view": str(v.id)}, track),
        }
        for v in views
    ]
    return templates().TemplateResponse(
        request, "partials/pl_views.html",
        {"items": items, "track": track, "mine_href": _view_url({"view": "default"}, track)},
    )


@router.post("/pipeline/views", response_class=HTMLResponse)
async def views_save(request: Request):
    """Save as a view: the filters currently set (the form posts them), under a name."""
    from kestrel.web.deps import active_track

    form = await request.form()
    track = active_track(request)
    name = str(form.get("view_name") or "")
    params = {k: form.get(k) for k in saved_views.FILTER_KEYS} | {"sort": form.get("sort")}
    async with sessionmaker()() as s:
        view = await saved_views.save_view(s, track, name, params)
        if view is None:
            return _answer({"k-toast": _toast("Give the view a name first.", tone="warn")})
        await s.commit()
        vid, vname = view.id, view.name
    return _answer({
        "k-toast": _toast(f"Saved the view {vname}."), "pl-view": {"id": vid, "name": vname},
    })


@router.post("/pipeline/views/{view_id}/default", response_class=HTMLResponse)
async def views_default(request: Request, view_id: int):
    async with sessionmaker()() as s:
        ok = await saved_views.set_default(s, view_id)
        await s.commit()
    msg = "That view opens first from now on." if ok else "That view is gone."
    return _answer({"k-toast": _toast(msg, tone=None if ok else "warn")})


@router.post("/pipeline/views/{view_id}/remove", response_class=HTMLResponse)
async def views_remove(request: Request, view_id: int):
    async with sessionmaker()() as s:
        ok = await saved_views.delete_view(s, view_id)
        await s.commit()
    msg = "View removed." if ok else "That view is gone."
    return _answer({"k-toast": _toast(msg, tone=None if ok else "warn")})
