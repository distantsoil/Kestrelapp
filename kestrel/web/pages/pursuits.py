"""Applications / Leads board (SPEC-INTERFACES 2.1, D-UI-46). The route stays /pursuits. Four
columns per track over the fine statuses, a stage select on each card, verb buttons that record
what the operator did ("Applied?"), a way to add a role found elsewhere, and a table view.
Every status change writes a `status_event` (SPEC.md 4.5). Kestrel never applies or contacts anyone:
a verb only records what the operator already did."""

from __future__ import annotations

import html
import json
import re
from datetime import date, timedelta

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.date_display import format_display
from kestrel.db.base import utcnow_iso
from kestrel.db.enums import PURSUIT_STATUS, valid_pursuit_status
from kestrel.db.models import (
    DocMaster,
    DocRender,
    EngagementOffer,
    Evaluation,
    Offer,
    Opportunity,
    Org,
    Outcome,
    Pursuit,
    Setting,
    StatusEvent,
)
from kestrel.services import follow_up as drafts
from kestrel.services import pursuit_board as board_svc
from kestrel.services import pursuit_undo as undo
from kestrel.services.pursuits import set_pursuit_status
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.deps import active_track, sessionmaker, templates, track_is_consulting
from kestrel.web.redact import confidential, redact_name, redact_text

router = APIRouter()
log = structlog.get_logger()


# Limits for a long board (#30). The date range hides cards whose last activity is older than N
# days ("0" is All time); the cap shows the first N cards of each column. Both live in the URL and,
# once chosen, in a setting row so the next visit starts where the user left off.
RANGE_KEY = "ui.board_range"
CAP_KEY = "ui.board_cap"
RANGE_CHOICES: tuple[str, ...] = ("7", "14", "30", "90", "0")
CAP_CHOICES: tuple[str, ...] = ("5", "10", "25", "50", "0")
LATER_DAYS = 7  # the Follow-ups card lists what is due within this many days past tomorrow


def _day(stamp: str | None) -> str:
    """The date part of a stored timestamp, or "" (a card with no date is never range-hidden)."""
    return (stamp or "")[:10]


async def _stored_choice(session: AsyncSession, key: str, allowed: tuple[str, ...]) -> str | None:
    row = await session.get(Setting, key)
    value = str(row.value) if row is not None and row.value is not None else None
    return value if value in allowed else None


def _search_text(*parts: str | None) -> str:
    """What the search box matches for one card: lowercased, punctuation folded to spaces. Every
    part is already redacted, so confidential mode never leaks the employer through search."""
    joined = " ".join(p for p in parts if p)
    return re.sub(r"[\W_]+", " ", joined.lower()).strip()


def _track_of(raw: str | None) -> str:
    if raw in PURSUIT_STATUS:
        return raw
    return "consulting" if track_is_consulting("") else "employment"


async def _resume_options(session: AsyncSession, opp_ids: list[int]) -> dict[int, list[dict]]:
    """Renders made for each opportunity, newest first, labeled by master and date only (a PDF
    file name can carry an organization name, so it is never shown)."""
    if not opp_ids:
        return {}
    rows = (
        await session.execute(
            select(DocRender, DocMaster.name)
            .join(DocMaster, DocMaster.id == DocRender.doc_master_id, isouter=True)
            .where(DocRender.opportunity_id.in_(opp_ids))
            .order_by(DocRender.created_at.desc(), DocRender.id.desc())
        )
    ).all()
    out: dict[int, list[dict]] = {}
    for render, master_name in rows:
        label = f"{master_name or 'Document'} · {format_display(render.created_at)}"
        out.setdefault(render.opportunity_id, []).append({"id": render.id, "label": label})
    return out


async def _tailor_master(session: AsyncSession, track: str) -> DocMaster | None:
    """The document Tailor opens for this track: the drawer's own choice (routes_pages)."""
    from kestrel.web.routes_pages import _pick_master

    masters = list(
        (
            await session.execute(
                select(DocMaster).where(DocMaster.track == track, DocMaster.is_active.is_(True))
            )
        ).scalars()
    )
    return _pick_master(masters, track)


async def _fit_labels(session: AsyncSession, opps: list[Opportunity]) -> dict[int, dict]:
    """The fit each role shows: the number once scored, else nothing (the card says "Being
    scored", or "Not scored" and why for a role hidden as applied that nothing will score,
    D-UI-225; never a dash and never a no, hard rule 4)."""
    from kestrel.web.fit import fit_for
    from kestrel.web.queries import _latest_eval_subq, hidden_unscored

    if not opps:
        return {}
    sub = _latest_eval_subq()
    evals = {
        e.opportunity_id: e
        for e in (
            await session.execute(
                select(Evaluation)
                .join(sub, sub.c.eid == Evaluation.id)
                .where(Evaluation.opportunity_id.in_([o.id for o in opps]))
            )
        ).scalars()
    }
    unscored = await hidden_unscored(session, opps)
    out: dict[int, dict] = {}
    for opp in opps:
        fit = fit_for(evals.get(opp.id), opp.stage)
        out[opp.id] = {
            "score": f"{fit.score:.1f}" if fit.score is not None else "", "value": fit.score,
            # Hidden as applied before any model read it: Not scored, never Being scored (#59).
            "unscored": unscored.get(opp.id) if fit.score is None else None,
        }
    return out


def _pay_line(opp: Opportunity) -> str:
    from kestrel.web.routes_pages import _comp_line

    return _comp_line(opp) or ""


def _place_line(opp: Opportunity) -> str:
    remote = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}.get(opp.remote_type, "")
    place = opp.location_raw or ", ".join(p for p in (opp.city, opp.state) if p)
    return " · ".join(x for x in (place, remote) if x)


def _next_line(p: Pursuit, on: date) -> dict:
    """The card's "Next:" line: the action, and when. An interview or call shows its date and
    time, a late follow-up shows when it was due."""
    if not p.next_action:
        return {"text": "", "icon": "clock", "when": ""}
    due = board_svc.parse_day(p.next_action_at)
    if due is None:
        return {"text": p.next_action, "icon": "clock", "when": ""}
    if board_svc.is_scheduled(p, on):
        clock = board_svc.clock_label(p.next_action_at)
        when = f"{board_svc.short_date(p.next_action_at)}{', ' + clock if clock else ''}"
        return {"text": p.next_action, "icon": "cal", "when": when}
    if due < on:
        when = f"was due {board_svc.short_date(p.next_action_at)}"
        return {"text": p.next_action, "icon": "clock", "when": when}
    return {"text": p.next_action, "icon": "clock", "when": board_svc.due_label(due, on)}


async def _board(
    session: AsyncSession, track: str, *, notice: dict | None = None, new_id: int | None = None
) -> dict:
    rows = (
        await session.execute(
            select(Pursuit, Opportunity, Org)
            .join(Opportunity, Opportunity.id == Pursuit.opportunity_id)
            .join(Org, Org.id == Opportunity.org_id, isouter=True)
            .where(Pursuit.track == track)
        )
    ).all()
    on = date.today()
    conf, mapping = await confidential(session)
    opps = [opp for _p, opp, _o in rows]
    resumes = await _resume_options(session, [o.id for o in opps])
    fits = await _fit_labels(session, opps)
    master = await _tailor_master(session, track)
    ghost_days = (await load_settings_with_db(session)).pursuits.ghosting_days
    pids = [p.id for p, _o, _g in rows]
    added: dict[int, str] = {}
    offers_for: set[int] = set()
    if pids:
        added = {
            pid: (first or "")[:10]
            for pid, first in (
                await session.execute(
                    select(StatusEvent.pursuit_id, func.min(StatusEvent.occurred_at))
                    .where(StatusEvent.pursuit_id.in_(pids))
                    .group_by(StatusEvent.pursuit_id)
                )
            ).all()
        }
        for model in (Offer, EngagementOffer):
            offers_for |= set(
                (await session.execute(select(model.pursuit_id).where(model.pursuit_id.in_(pids))))
                .scalars()
            )
    by_status: dict[str, list] = {}
    for pursuit, opp, org in rows:
        by_status.setdefault(pursuit.status, []).append((pursuit, opp, org))
    folded_statuses = board_svc.FOLDED_STATUSES[track]
    columns = []
    cards_flat: list[dict] = []
    for col in board_svc.columns_for(track):
        members = [t for st in col.statuses for t in by_status.get(st, [])]
        ordered = board_svc.order_cards([t[0] for t in members], on)
        lookup = {t[0].id: t for t in members}
        cards = []
        for pursuit in ordered:
            _p, opp, org = lookup[pursuit.id]
            quiet = board_svc.quiet_days(pursuit, on)
            interview_was = board_svc.interview_passed(pursuit, on)
            docs = resumes.get(opp.id, [])
            shown_org = redact_name(org.name if org else opp.org_name, mapping, enabled=conf)
            shown_title = redact_text(opp.title or "", mapping, enabled=conf) or "Untitled"
            letters, slot = board_svc.monogram(shown_org)
            notes_shown = redact_text(pursuit.notes_md or "", mapping, enabled=conf)
            nxt = _next_line(pursuit, on)
            nxt["text"] = redact_text(nxt["text"], mapping, enabled=conf)
            if interview_was is not None:
                nxt = {
                    "text": "Record the outcome", "icon": "clock",
                    "when": f"interview was {board_svc.short_date(interview_was.isoformat())}",
                }
            card = {
                "pursuit": pursuit,
                "opp": opp,
                "title": shown_title,
                "org_name": shown_org,
                "letters": letters,
                "slot": slot,
                "fit": fits.get(opp.id) or {"score": "", "value": None},
                "priority": bool(pursuit.priority),
                # Confidential mode shows the notes with the employer hidden. Saving that text
                # back would overwrite the real note, so a redacted note is read-only here.
                "kind": redact_text(
                    (pursuit.next_action or "").split("(", 1)[1].rstrip(") ")
                    if "(" in (pursuit.next_action or "") else "", mapping, enabled=conf,
                ),
                "notes": notes_shown,
                "notes_locked": notes_shown != (pursuit.notes_md or ""),
                "place": redact_text(_place_line(opp), mapping, enabled=conf),
                "pay": _pay_line(opp),
                "search": _search_text(
                    shown_org,
                    *(
                        redact_text(t, mapping, enabled=conf)
                        for t in (
                            opp.title, opp.location_raw, pursuit.notes_md, pursuit.next_action,
                        )
                    ),
                ),
                "overdue": quiet is not None or interview_was is not None,
                "quiet_days": quiet,
                "next": nxt,
                "step": board_svc.next_step(track, pursuit.status),
                "stage_label": board_svc.stage_label(pursuit.status),
                "docs": docs,
                # The same entry the drawer's Tailor uses (renders/new), with the way back here.
                "tailor_href": (
                    f"/renders/new?master_id={master.id}&opportunity_id={opp.id}&from=/pursuits"
                    if master is not None
                    else "/documents"
                ),
                "default_doc": pursuit.document_id or (docs[0]["id"] if docs else None),
                "closed": pursuit.status in board_svc.CLOSED_STATUSES[track],
                "folded": pursuit.status in folded_statuses,
                "closed_on": board_svc.short_date(pursuit.closed_at),
                "applied": _day(pursuit.applied_at),
                "updated": _day(pursuit.last_activity_at) or _day(pursuit.applied_at),
                "added": added.get(pursuit.id, ""),
                "work": opp.remote_type or "unknown",
                # A waiting card quiet past pursuits.ghosting_days offers (never forces) the move.
                "ghost_hint": board_svc.ghost_hint(pursuit, on, days=ghost_days),
                "ghost_status": board_svc.GHOST_STATUS.get(track),
                "column": col.key,
                "is_new": pursuit.id == new_id,
                "has_offers": pursuit.id in offers_for,
            }
            cards.append(card)
            cards_flat.append(card)
        columns.append({
            "col": col,
            "cards": cards,
            "open": [c for c in cards if not c["folded"]],
            "folded": [c for c in cards if c["folded"]],
        })
    open_cards = [c for c in cards_flat if not c["folded"]]
    return {
        "columns": columns,
        "cards": cards_flat,
        "statuses": PURSUIT_STATUS[track],
        "stage_groups": [
            {"label": c.label, "stages": [(st, board_svc.stage_label(st)) for st in c.statuses]}
            for c in board_svc.columns_for(track)
        ],
        "menus": {name: board_svc.MENUS[name] for name in board_svc.MENU_NAMES[track]},
        "stage_name": board_svc.stage_name,
        "stage_label": board_svc.stage_label,
        "track": track,
        "today": on.isoformat(),
        "follow_up": (on + timedelta(days=board_svc.FOLLOW_UP_DAYS)).isoformat(),
        "outcome_choices": board_svc.OUTCOME_CHOICES[track],
        "page_label": "Leads" if track == "consulting" else "Applications",
        "counts": {
            "open": len(open_cards),
            "closed": len(cards_flat) - len(open_cards),
            "quiet": sum(1 for c in open_cards if c["quiet_days"]),
        },
        "rail": _rail(cards_flat, on),
        "notice": notice,
        "oob": False,
    }


def _rail(cards: list[dict], on: date) -> dict:
    """The right rail: interviews (calls) coming up, and the follow-ups due now."""
    interviews, due_now, later = [], [], []
    for c in cards:
        p = c["pursuit"]
        if c["folded"] or c["closed"]:
            continue
        when = board_svc.parse_day(p.next_action_at)
        if board_svc.is_scheduled(p, on) and when is not None:
            kind = ""
            if "(" in (p.next_action or ""):
                kind = c["kind"]
            interviews.append({
                "card": c, "month": format_display(when, "month_upper"),
                "day": format_display(when, "day"), "when": when,
                "date_label": format_display(when, "custom_only"),
                "clock": board_svc.clock_label(p.next_action_at), "kind": kind,
            })
        elif p.next_action and when is not None:
            delta = (when - on).days
            if delta >= 0:
                label = board_svc.due_label(when, on)
            elif c["quiet_days"]:
                label = f"{c['quiet_days']} day{'' if c['quiet_days'] == 1 else 's'} quiet"
            else:
                label = f"Was due {board_svc.short_date(p.next_action_at)}"
            item = {
                "card": c, "label": label, "delta": delta,
                "tone": "late" if delta < 0 else "today" if delta == 0 else "",
                "draftable": drafts.eligible(p),
            }
            if delta <= 1:
                due_now.append(item)
            elif delta <= LATER_DAYS:
                later.append(item)
    interviews.sort(key=lambda i: (i["when"], i["clock"]))
    due_now.sort(key=lambda i: i["delta"])
    later.sort(key=lambda i: i["delta"])
    return {
        "interviews": interviews, "due_now": due_now, "later": later,
        "choices": [c for c in cards if not c["folded"] and not c["closed"]],
    }


async def _panel_ctx(
    session: AsyncSession, pursuit_id: int, form: str | None = None
) -> dict | None:
    """The slide-over panel for one card: the card's own data plus its history timeline."""
    p = await session.get(Pursuit, pursuit_id)
    if p is None:
        return None
    board = await _board(session, p.track)
    card = next((c for c in board["cards"] if c["pursuit"].id == pursuit_id), None)
    if card is None:
        return None
    conf, mapping = await confidential(session)
    events = (
        await session.execute(
            select(StatusEvent)
            .where(StatusEvent.pursuit_id == pursuit_id)
            .order_by(StatusEvent.occurred_at.desc(), StatusEvent.id.desc())
        )
    ).scalars().all()
    history = []
    for e in events:
        if e.note:
            text = redact_text(e.note, mapping, enabled=conf)
        elif e.from_status is None:
            text = "Added to your list"
        else:
            text = board_svc.HISTORY_TEXT.get(e.to_status) or (
                f"Stage set to {board_svc.stage_label(e.to_status)}"
            )
        history.append({"text": text, "date": board_svc.short_date(e.occurred_at)})
    outcome = (
        await session.execute(
            select(Outcome).where(Outcome.pursuit_id == pursuit_id).order_by(Outcome.id.desc())
        )
    ).scalars().first()
    track = p.track
    reason_raw = (outcome.rejection_reason if outcome else None) or p.outcome_reason or ""
    reason = redact_text(reason_raw, mapping, enabled=conf)
    results = (
        [("accepted", "Accepted"), ("withdrawn", "Declined"), ("rejected", "Not selected"),
         ("ghosted", "No reply")]
        if track == "employment"
        else [("won", "Won"), ("lost", "Lost"), ("no_response", "No response")]
    )
    furthest = (
        ["applied", "screening", "interviewing", "final", "offer"]
        if track == "employment"
        else ["outreach_sent", "conversation", "proposal_sent", "negotiating"]
    )
    return {
        "c": card, "history": history, "outcome": outcome, "track": track, "form": form,
        "reason": reason, "reason_locked": reason != reason_raw,
        "buttons": board_svc.record_buttons(track, p.status),
        "results": results, "furthest": [(st, board_svc.stage_label(st)) for st in furthest],
        "today": date.today().isoformat(),
        "follow_up": (date.today() + timedelta(days=board_svc.FOLLOW_UP_DAYS)).isoformat(),
        "docs_used": [d for d in card["docs"] if d["id"] in (p.document_id, p.cover_letter_id)],
        "stage_groups": board["stage_groups"],
        "stage_label": board_svc.stage_label,
        "short_date": board_svc.short_date,
    }


def _trigger(message: str, undo_token: str | None = None, *, close_panel: bool = False) -> str:
    """The HX-Trigger header: a toast (with Undo when there is a snapshot), and optionally an
    order to close the panel."""
    trigger: dict = {}
    if message:
        detail: dict = {"message": message}
        if undo_token:
            detail["undoUrl"] = f"/pursuits/undo/{undo_token}"
        trigger["k-toast"] = detail
    if close_panel:
        trigger["apps-close-panel"] = True
    return json.dumps(trigger)


async def _render_board(
    request: Request, track: str, *, notice: dict | None = None, new_id: int | None = None,
    toast: str | None = None, undo: str | None = None, close_panel: bool = False,
    keep_panel: bool = False,
) -> HTMLResponse:
    """The board plus, out of band, the rail, the count line and (when one is open) the panel.
    Always 200: HTMX does not swap a 4xx body, so a failure is a notice. `keep_panel` leaves the
    open panel alone, so a form that failed to save keeps what was typed."""
    raw = request.query_params.get("panel") or ""
    if not raw and request.method == "POST":
        raw = str((await request.form()).get("panel") or "")
    panel_id = int(raw) if raw.isdigit() else None
    async with sessionmaker()() as s:
        board = await _board(s, track, notice=notice, new_id=new_id)
        panel_ctx = None
        if panel_id is not None and not close_panel and not keep_panel:
            panel_ctx = await _panel_ctx(s, panel_id, request.query_params.get("form"))
    ctx = {
        "query": {}, "view": request.query_params.get("view") or "kanban", **board, "oob": True,
        "panel": panel_ctx, "closed_open": False,
    }
    resp = templates().TemplateResponse(request, "partials/pursuit_update.html", ctx)
    if toast or close_panel:
        resp.headers["HX-Trigger"] = _trigger(toast or "", undo, close_panel=close_panel)
    return resp


async def _missing(request: Request) -> HTMLResponse:
    """A card that is gone (removed in another tab). HTMX does not swap a 4xx body, so an HTMX
    caller gets the board with a notice instead; anything else gets a plain 404."""
    if not request.headers.get("HX-Request"):
        return HTMLResponse("not found", status_code=404)
    return await _render_board(
        request, active_track(request),
        notice={"ok": False, "text": "That card is no longer on the board."},
    )


@router.get("/pursuits", response_class=HTMLResponse)
async def pursuits(request: Request):
    track = active_track(request)
    view = request.query_params.get("view") or "kanban"
    from kestrel.analytics.today import switched_off_work

    async with sessionmaker()() as s:
        auto_draft = None
        raw_opp = request.query_params.get("follow_up", "")
        if raw_opp.isdecimal():
            candidate = (await s.execute(select(Pursuit).where(
                Pursuit.opportunity_id == int(raw_opp),
            ))).scalar_one_or_none()
            if candidate is not None and drafts.eligible(candidate):
                auto_draft, track = candidate.id, candidate.track
        board = await _board(s, track)
        # R2: a switched-off track's open pursuits are never hidden; a line names the track
        # and links to its board (an explicit ?track= opens it, D-UI-57).
        off = [o for o in await switched_off_work(s) if o["track"] != track]
        # The URL wins, then the choice saved last time, then nothing hidden (#30).
        board_range = request.query_params.get("range")
        if board_range not in RANGE_CHOICES:
            board_range = await _stored_choice(s, RANGE_KEY, RANGE_CHOICES) or "0"
        board_cap = request.query_params.get("cap")
        if board_cap not in CAP_CHOICES:
            board_cap = await _stored_choice(s, CAP_KEY, CAP_CHOICES) or "0"
    ctx = {
        "title": board["page_label"], "query": dict(request.query_params), "view": view, **board,
        "off_tracks": off, "board_range": board_range, "board_cap": board_cap,
        "closed_open": request.query_params.get("open") == "closed",
        "range_choices": RANGE_CHOICES, "cap_choices": CAP_CHOICES, "panel": None,
        "auto_draft": auto_draft,
    }
    if request.headers.get("HX-Request"):
        ctx["oob"] = True
        return templates().TemplateResponse(request, "partials/pursuit_update.html", ctx)
    return templates().TemplateResponse(request, "pursuits.html", ctx)


async def _follow_up_view(
    request: Request, session: AsyncSession, pursuit: Pursuit, error: str | None = None,
) -> HTMLResponse:
    settings = await load_settings_with_db(session)
    facts = await drafts.facts_for(session, pursuit)
    task = await drafts.latest(session, pursuit.id)
    pending = bool(task and task.status in ("pending", "leased"))
    payload = (task.payload or {}) if task else {}
    if task is not None and not pending and payload.get("facts") != facts:
        # A saved draft or error describes the facts it was written from. Once the stage, next
        # action or dates have moved on it is stale: show a fresh request (with the consent flow
        # when online) instead of the old draft.
        task, payload = None, {}
    conf_on, mapping = await confidential(session)
    result = payload.get("result")
    if result and conf_on:
        result = mapping.redact_json(result)
    shown_facts = mapping.redact_json(facts) if conf_on else facts
    return templates().TemplateResponse(request, "partials/follow_up.html", {
        "pid": pursuit.id, "route": await drafts.disclosure(session, settings),
        "facts": shown_facts, "facts_stamp": drafts.stamp(facts),
        "eligible": drafts.eligible(pursuit), "error": error or payload.get("error"),
        "task": task, "result": result,
        "pending": pending,
    })


@router.get("/pursuits/{pursuit_id}/follow-up", response_class=HTMLResponse)
async def follow_up_dialog(request: Request, pursuit_id: int):
    async with sessionmaker()() as session:
        pursuit = await session.get(Pursuit, pursuit_id)
        if pursuit is None:
            return HTMLResponse("That application is no longer available.")
        return await _follow_up_view(request, session, pursuit)


@router.post("/pursuits/{pursuit_id}/follow-up", response_class=HTMLResponse)
async def draft_follow_up(request: Request, pursuit_id: int):
    form = await request.form()
    body = drafts.DraftRequest(
        route_stamp=str(form.get("route_stamp") or ""),
        facts_stamp=str(form.get("facts_stamp") or ""),
        model=str(form.get("model") or ""), confirmed=str(form.get("confirmed") or ""),
    )
    async with sessionmaker()() as session:
        pursuit = await session.get(Pursuit, pursuit_id)
        if pursuit is None:
            return HTMLResponse("That application is no longer available.")
        settings = await load_settings_with_db(session)
        error = await drafts.request_draft(session, pursuit, settings, body)
        await session.commit()
        return await _follow_up_view(request, session, pursuit, error)


@router.get("/pursuits/{pursuit_id}/panel", response_class=HTMLResponse)
async def panel(request: Request, pursuit_id: int):
    """The slide-over panel for one card (its inline forms open with ?form=)."""
    async with sessionmaker()() as s:
        ctx = await _panel_ctx(s, pursuit_id, request.query_params.get("form"))
    if ctx is None:
        return HTMLResponse("That card is no longer on the board.", status_code=404)
    return templates().TemplateResponse(request, "partials/pursuit_panel.html", ctx)


@router.post("/pursuits/prefs", response_class=HTMLResponse)
async def save_board_prefs(request: Request):
    """Remember the date range and column cap the user chose (#30). A value
    that is not one of the offered choices is ignored. Always 200: the page fires this and
    forgets it."""
    form = await request.form()
    wanted = {
        RANGE_KEY: (form.get("range"), RANGE_CHOICES),
        CAP_KEY: (form.get("cap"), CAP_CHOICES),
    }
    try:
        async with sessionmaker()() as s:
            for key, (value, allowed) in wanted.items():
                if value not in allowed:
                    continue
                # One atomic upsert, not read-then-insert: two saves fired close together (the
                # page posts one per change) both saw no row and the second insert failed on the
                # key, silently losing the choice (#14 Windows CI). Same pattern as
                # setting_overrides.queue_run_mode.
                now = utcnow_iso()
                stmt = sqlite_insert(Setting).values(key=key, value=value, updated_at=now)
                await s.execute(stmt.on_conflict_do_update(
                    index_elements=[Setting.key], set_={"value": value, "updated_at": now}
                ))
            await s.commit()
    except SQLAlchemyError as exc:
        log.warning("board_prefs_not_saved", error_type=type(exc).__name__)
    return HTMLResponse("")


@router.post("/pursuits/add", response_class=HTMLResponse)
async def add_role(request: Request):
    """Add a role found elsewhere: a URL (fetched once, politely) and/or pasted text, ingested
    through the manual path and put on the board in the first column at once."""
    from kestrel.ingest.http import HttpClient
    from kestrel.services import applied_import
    from kestrel.services.setting_overrides import load_settings_with_db

    form = await request.form()
    track = _track_of(str(form.get("track") or ""))
    url = str(form.get("url") or "").strip()
    text = str(form.get("text") or "").strip()
    async with sessionmaker()() as s:
        if url and url.lower().startswith(("http://", "https://")):
            settings = await load_settings_with_db(s)
            async with HttpClient(
                user_agent=settings.ingest.user_agent,
                default_rate_per_sec=settings.ingest.default_rate_per_sec,
                timeout=settings.ingest.request_timeout_s,
                respect_robots=settings.ingest.respect_robots,
                max_attempts=1,
            ) as http:
                res = await applied_import.add_found_role(
                    s, track=track, url=url, text=text,
                    title=str(form.get("title") or ""),
                    org_name=str(form.get("org_name") or ""), http=http,
                )
        else:
            res = await applied_import.add_found_role(
                s, track=track, url=url, text=text,
                title=str(form.get("title") or ""), org_name=str(form.get("org_name") or ""),
            )
    notice = {"ok": res.ok, "text": res.message}
    new_id = res.pursuit_id if res.ok and res.created else None
    first = board_svc.columns_for(track)[0].label
    toast = f"Added to {first}. Kestrel is scoring it now." if new_id else None
    return await _render_board(request, track, notice=notice, new_id=new_id, toast=toast)


def _when_text(day: date | None) -> str:
    return format_display(day, "short")


def _verb_message(track: str, verb: str, p: Pursuit) -> str:
    """The toast after a verb: what moved, and what the follow-up is."""
    stage = board_svc.stage_label(p.status)
    column = board_svc.column_by_key(track, board_svc.column_key(track, p.status) or "")
    due = _when_text(board_svc.parse_day(p.next_action_at))
    if verb == "applied":
        return f"Moved to {column.label if column else stage}. Follow up on {due}."
    if verb == "follow_again":
        return f"Follow-up moved to {due}."
    if verb in ("interview", "call"):
        clock = board_svc.clock_label(p.next_action_at)
        where = "Upcoming calls" if track == "consulting" else "Upcoming interviews"
        return f"{'Interview' if verb == 'interview' else 'Call'} on {due}"\
            f"{' at ' + clock if clock else ''}. It is in {where}."
    if verb == "proposal":
        return f"Moved to {column.label if column else stage}. Follow up on {due}."
    if verb == "offer":
        return "Offer received. Nice work." if track == "employment" else "Won. Congratulations."
    return f"Moved to {stage}."


@router.post("/pursuits/{pursuit_id}/status", response_class=HTMLResponse)
async def move_status(request: Request, pursuit_id: int):
    """Move a pursuit to a new status and WRITE the status_event audit row (the stage menu and
    drag-and-drop both land here). Rejects a status that is not valid for the pursuit's track.
    An HTMX caller gets the refreshed board; a plain post gets 204."""
    form = await request.form()
    to_status = str(form.get("to_status") or "").strip()
    token = None
    async with sessionmaker()() as s:
        p = await s.get(Pursuit, pursuit_id)
        if p is None:
            return await _missing(request)
        track = p.track
        snap = await undo.snapshot(s, p)
        # One path for every move (board_svc.move_to_status): the applied defaults, closed_at
        # set or cleared, the status_event. An invalid stage is a 200 notice, since HTMX does
        # not swap a 4xx body and the operator would see nothing happen.
        err = await board_svc.move_to_status(
            s, p, to_status, note=str(form.get("note") or "") or None
        )
        if err:
            await s.rollback()
        else:
            token = await undo.save(s, snap)
            await s.commit()
    if request.headers.get("HX-Request"):
        if err:
            return await _render_board(request, track, notice={"ok": False, "text": err})
        return await _render_board(
            request, track, toast=f"Moved to {board_svc.stage_label(to_status)}.", undo=token
        )
    if err:
        return HTMLResponse(html.escape(err), status_code=400)
    return HTMLResponse("", status_code=204)


@router.post("/pursuits/{pursuit_id}/remove", response_class=HTMLResponse)
async def remove_card(request: Request, pursuit_id: int):
    """Remove from my shortlist / leads, from the board card (#23). The same full removal as the
    decision panel's Remove (D-UI-75, opportunity_actions.remove_from_list): the pursuit with its
    history, outcome and offers goes in one transaction, and the toast offers Undo (the history
    and outcome come back; an offer, if one was entered, does not). Answers with the board plus the
    sidebar badges out of band. Always 200: HTMX does not swap a 4xx body, so a failure is a
    notice."""
    from kestrel.web.errors import plain_error
    from kestrel.web.pages.opportunity_actions import remove_from_list
    from kestrel.web.routes_api import oob_nav_counts
    from kestrel.web.words import words

    track = active_track(request)
    notice: dict
    token = None
    done = False
    async with sessionmaker()() as s:
        try:
            p = await s.get(Pursuit, pursuit_id)
            opp = await s.get(Opportunity, p.opportunity_id) if p is not None else None
            if p is None or opp is None:
                notice = {"ok": False, "text": "That card is no longer on the board."}
            else:
                track = p.track
                has_offer = False
                for model in (Offer, EngagementOffer):
                    found = await s.execute(
                        select(model.id).where(model.pursuit_id == p.id).limit(1)
                    )
                    has_offer = has_offer or found.first() is not None
                snap = await undo.remove_snapshot(s, p, opp)
                notice = {
                    "ok": True,
                    "text": await remove_from_list(
                        s, opp, commit=False, label_ids=[v["id"] for v in snap["labels"]]
                    ),
                }
                if not has_offer:  # an entered offer cannot be put back, so no Undo is offered
                    snap["opp_after"] = {k: getattr(opp, k) for k in undo.OPP_FIELDS}
                    token = await undo.save(s, snap)
                await s.commit()
                done = True
        except Exception as exc:  # a write path must never 500 (CLAUDE.md hard rules)
            log.warning(
                "board_remove.failed",
                pursuit_id=pursuit_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            await s.rollback()
            done = False
            token = None
            notice = {
                "ok": False,
                "text": f"Could not remove it from your {words(track)['list']}: {plain_error(exc)}",
            }
        oob = await oob_nav_counts(s)
    resp = await _render_board(
        request,
        track,
        notice=notice,
        toast="Removed. It goes back to Pipeline." if done else None,
        undo=token,
        close_panel=done,
    )
    resp.body += oob.encode("utf-8")
    resp.headers["content-length"] = str(len(resp.body))
    return resp


@router.post("/pursuits/{pursuit_id}/act/{verb}", response_class=HTMLResponse)
async def act(request: Request, pursuit_id: int, verb: str):
    """A card button: record what the operator did and move the card. Returns the board."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    token = None
    async with sessionmaker()() as s:
        p = await s.get(Pursuit, pursuit_id)
        if p is None:
            return await _missing(request)
        track = p.track
        snap = await undo.snapshot(s, p)
        err = await board_svc.apply_verb(s, p, verb, form)
        if err:
            await s.rollback()
        else:
            token = await undo.save(s, snap)
            await s.commit()
            message = _verb_message(track, verb, p)
    if err:
        return await _render_board(
            request, track, notice={"ok": False, "text": err}, keep_panel=True
        )
    return await _render_board(request, track, toast=message, undo=token)


@router.post("/pursuits/{pursuit_id}/act/applied/quick", response_class=HTMLResponse)
async def applied_quick(request: Request, pursuit_id: int):
    """The one-click path of "I applied": records applied today with a follow-up in seven days
    (and the resume chosen for the button). Kept for callers that post it; the card itself now
    opens the form in the panel."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    form.pop("applied_on", None)
    form.pop("follow_up", None)
    async with sessionmaker()() as s:
        p = await s.get(Pursuit, pursuit_id)
        if p is None:
            return HTMLResponse("not found", status_code=404)
        track = p.track
        err = await board_svc.apply_verb(s, p, "applied", form)
        if err:
            await s.rollback()
        else:
            await s.commit()
    if err:
        return await _render_board(request, track, notice={"ok": False, "text": err})
    label = board_svc.VERB_LABELS[track]["applied"]
    ctx = {
        "pid": pursuit_id, "track": track, "done": board_svc.done_label(label),
        "moving_to": "Applied" if track == "employment" else "In conversation",
    }
    return templates().TemplateResponse(request, "partials/applied_done.html", ctx)


@router.post("/pursuits/{pursuit_id}/reposts", response_class=HTMLResponse)
async def set_allow_reposts(request: Request, pursuit_id: int):
    """SPEC.md 5.6: whether a new posting that dedups into this applied pursuit's group may
    surface (on) or is hidden on arrival as a re-post (off, the default)."""
    form = await request.form()
    allow = "on" in form.getlist("allow")
    async with sessionmaker()() as s:
        p = await s.get(Pursuit, pursuit_id)
        if p is None:
            return HTMLResponse("not found", status_code=404)
        p.allow_reposts = allow
        await s.commit()
    return HTMLResponse("", status_code=204)


@router.post("/pursuits/{pursuit_id}/next-action", response_class=HTMLResponse)
async def set_next_action(request: Request, pursuit_id: int):
    form = await request.form()
    async with sessionmaker()() as s:
        p = await s.get(Pursuit, pursuit_id)
        if p is None:
            return HTMLResponse("not found", status_code=404)
        p.next_action = str(form.get("next_action") or "") or None
        p.next_action_at = str(form.get("next_action_at") or "") or None
        p.last_activity_at = utcnow_iso()
        await s.commit()
    return HTMLResponse("saved")


@router.post("/pursuits/{pursuit_id}/priority", response_class=HTMLResponse)
async def toggle_priority(request: Request, pursuit_id: int):
    """Mark or clear High priority (`pursuit.priority`). It only labels the card."""
    token = None
    async with sessionmaker()() as s:
        p = await s.get(Pursuit, pursuit_id)
        if p is None:
            return await _missing(request)
        track = p.track
        snap = await undo.snapshot(s, p)
        p.priority = 0 if p.priority else 1
        message = "Marked high priority." if p.priority else "High priority removed."
        token = await undo.save(s, snap)
        await s.commit()
    return await _render_board(request, track, toast=message, undo=token)


@router.post("/pursuits/{pursuit_id}/notes", response_class=HTMLResponse)
async def save_notes(request: Request, pursuit_id: int):
    """The panel's notes box, saved as the operator types (on change). Notes stay on this
    computer; nothing reads them but the operator."""
    form = await request.form()
    async with sessionmaker()() as s:
        p = await s.get(Pursuit, pursuit_id)
        if p is None:
            return HTMLResponse("not found", status_code=404)
        p.notes_md = str(form.get("notes") or "").strip()[:20000] or None
        await s.commit()
    return HTMLResponse("", status_code=204)


@router.post("/pursuits/{pursuit_id}/done", response_class=HTMLResponse)
async def follow_up_done(request: Request, pursuit_id: int):
    """Tick a follow-up off the rail: the card's next action and its date are cleared."""
    token = None
    async with sessionmaker()() as s:
        p = await s.get(Pursuit, pursuit_id)
        if p is None:
            return await _missing(request)
        track = p.track
        snap = await undo.snapshot(s, p)
        p.next_action = p.next_action_at = None
        p.last_activity_at = utcnow_iso()
        token = await undo.save(s, snap)
        await s.commit()
    return await _render_board(request, track, toast="Marked done.", undo=token)


@router.post("/pursuits/reminder", response_class=HTMLResponse)
async def add_reminder(request: Request):
    """ "+ Add a reminder": there is no separate tasks feature; a reminder is the card's own next
    action and its date."""
    form = await request.form()
    raw = str(form.get("pursuit_id") or "")
    days = {"0": 0, "3": 3, "7": 7}.get(str(form.get("days") or "0"), 0)
    what = " ".join(str(form.get("what") or "").split())[:120] or "Follow up"
    token = None
    async with sessionmaker()() as s:
        p = await s.get(Pursuit, int(raw)) if raw.isdigit() else None
        if p is None:
            return await _render_board(
                request,
                _track_of(str(form.get("track") or "")),
                notice={"ok": False, "text": "Choose which application the reminder is for."},
            )
        track = p.track
        snap = await undo.snapshot(s, p)
        due = date.today() + timedelta(days=days)
        p.next_action, p.next_action_at = what, due.isoformat()
        p.last_activity_at = utcnow_iso()
        token = await undo.save(s, snap)
        await s.commit()
    when = "today" if days == 0 else "this week" if days == 3 else "next week"
    return await _render_board(request, track, toast=f"Reminder set for {when}.", undo=token)


@router.post("/pursuits/undo/{token}", response_class=HTMLResponse)
async def undo_change(token: str):
    """Undo from a toast. The snapshot is used up; the page refreshes its own board after."""
    from kestrel.web.routes_api import oob_nav_counts

    async with sessionmaker()() as s:
        track, message = await undo.restore(s, token)
        await s.commit()
        oob = await oob_nav_counts(s)  # a restored Remove changes the sidebar badges
    ok = "1" if track else "0"
    return HTMLResponse(f'<span data-undo-msg data-ok="{ok}">{html.escape(message)}</span>{oob}')


@router.post("/pursuits/{pursuit_id}/outcome", response_class=HTMLResponse)
async def capture_outcome(request: Request, pursuit_id: int):
    """Outcome capture on close: writes (or updates) the Outcome row (learning entry), moves the
    card to the chosen final status with a status_event, and closes the pursuit. Captured from what
    the operator enters; never inferred, never sent anywhere."""
    form = await request.form()
    token = None
    async with sessionmaker()() as s:
        p = await s.get(Pursuit, pursuit_id)
        if p is None:
            return await _missing(request)
        track = p.track
        final = str(form.get("final_status") or "").strip() or "closed"
        if final != "closed" and (
            not valid_pursuit_status(track, final) or final not in board_svc.OUTCOME_CHOICES[track]
        ):
            if request.headers.get("HX-Request"):
                return await _render_board(
                    request, track, notice={"ok": False, "text": "Choose how it ended."},
                    keep_panel=True,
                )
            return HTMLResponse("invalid status", status_code=400)
        days = str(form.get("days_to_first_response") or "").strip()
        reason = str(form.get("rejection_reason") or "").strip() or None
        keep_reason = bool(form.get("reason_locked"))  # shown hidden, so blank means unchanged
        stage = str(form.get("furthest_stage") or "").strip() or None
        if stage is not None and not valid_pursuit_status(track, stage):
            stage = None
        snap = await undo.snapshot(s, p)
        outcome = (
            await s.execute(
                select(Outcome).where(Outcome.pursuit_id == p.id).order_by(Outcome.id.desc())
            )
        ).scalars().first()
        if outcome is None:
            outcome = Outcome(pursuit_id=p.id, opportunity_id=p.opportunity_id)
            s.add(outcome)
        outcome.final_status = final
        outcome.furthest_stage = stage
        outcome.days_to_first_response = int(days) if days.isdigit() else None
        if keep_reason and not reason:
            reason = outcome.rejection_reason or p.outcome_reason
        outcome.rejection_reason = reason
        outcome.operator_retrospective = str(form.get("retrospective") or "") or None
        outcome.recorded_at = utcnow_iso()
        if final != "closed" and final != p.status:
            await set_pursuit_status(s, p.id, final, note=reason)
        p.outcome = final
        p.outcome_stage = stage
        p.outcome_reason = reason
        p.closed_at = p.closed_at or utcnow_iso()
        p.next_action = p.next_action_at = None
        p.last_activity_at = utcnow_iso()
        token = await undo.save(s, snap)
        await s.commit()
    if request.headers.get("HX-Request"):
        return await _render_board(
            request, track, toast="Outcome saved. Kestrel learns from it.", undo=token
        )
    return HTMLResponse("Outcome recorded. Thank you. This feeds the learning loop.")
