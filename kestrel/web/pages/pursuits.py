"""Applications / Leads board (SPEC-INTERFACES 2.1, D-UI-46). The route stays /pursuits. Four
columns per track over the fine statuses, a stage select on each card, verb buttons that record
what the operator did ("Applied?"), a way to add a role found elsewhere, and a table view.
Every status change writes a `status_event` (SPEC.md 4.5). Kestrel never applies or contacts anyone:
a verb only records what the operator already did."""

from __future__ import annotations

import html
import re
from datetime import date, timedelta

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.enums import PURSUIT_STATUS, valid_pursuit_status
from kestrel.db.models import DocMaster, DocRender, Opportunity, Org, Outcome, Pursuit
from kestrel.services import pursuit_board as board_svc
from kestrel.services.pursuits import set_pursuit_status
from kestrel.web.deps import active_track, sessionmaker, templates, track_is_consulting
from kestrel.web.redact import confidential, redact_name, redact_text

router = APIRouter()
log = structlog.get_logger()


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
        label = f"{master_name or 'Document'} · {(render.created_at or '')[:10]}"
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


async def _board(session: AsyncSession, track: str, *, notice: dict | None = None) -> dict:
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
    resumes = await _resume_options(session, [opp.id for _p, opp, _o in rows])
    master = await _tailor_master(session, track)
    by_status: dict[str, list] = {}
    for pursuit, opp, org in rows:
        by_status.setdefault(pursuit.status, []).append((pursuit, opp, org))
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
            sub = []
            if pursuit.applied_at:
                sub.append(f"applied {board_svc.short_date(pursuit.applied_at)}")
            if interview_was is not None:
                sub.append(
                    f"interview was {board_svc.short_date(interview_was.isoformat())}; "
                    "record the outcome"
                )
            elif pursuit.next_action and pursuit.next_action_at:
                when = board_svc.short_date(pursuit.next_action_at)
                if len(pursuit.next_action_at) >= 16:  # an interview carries a time of day
                    when += f" {pursuit.next_action_at[11:16]}"
                sub.append(f"{pursuit.next_action.lower()} {when}")
            docs = resumes.get(opp.id, [])
            shown_org = redact_name(org.name if org else opp.org_name, mapping, enabled=conf)
            card = {
                "pursuit": pursuit,
                "opp": opp,
                "org_name": shown_org,
                "search": _search_text(
                    shown_org,
                    *(redact_text(t, mapping, enabled=conf) for t in (
                        opp.title, opp.location_raw, pursuit.notes_md, pursuit.next_action)),
                ),
                "overdue": quiet is not None or interview_was is not None,
                "quiet_days": quiet,
                "sub": " · ".join(sub),
                "verbs": [
                    {
                        "key": k, "label": board_svc.VERB_LABELS[track][k],
                        "done": board_svc.done_label(board_svc.VERB_LABELS[track][k]),
                    }
                    for k in board_svc.verbs_for_card(track, pursuit.status)
                ],
                "docs": docs,
                # The same entry the drawer's Tailor uses (renders/new), with the way back here.
                "tailor_href": (
                    f"/renders/new?master_id={master.id}&opportunity_id={opp.id}&from=/pursuits"
                    if master is not None else "/documents"
                ),
                "default_doc": pursuit.document_id or (docs[0]["id"] if docs else None),
                "closed": pursuit.status in board_svc.CLOSED_STATUSES[track],
                "needs_outcome": col.key == "closed" and pursuit.outcome is None,
                "column": col.key,
            }
            cards.append(card)
            cards_flat.append(card)
        columns.append({"col": col, "cards": cards})
    return {
        "columns": columns,
        "cards": cards_flat,
        "statuses": PURSUIT_STATUS[track],
        "stage_name": board_svc.stage_name,
        "track": track,
        "today": on.isoformat(),
        "follow_up": (on + timedelta(days=board_svc.FOLLOW_UP_DAYS)).isoformat(),
        "outcome_choices": board_svc.OUTCOME_CHOICES[track],
        "stage_choices": [
            st for st in PURSUIT_STATUS[track][2:] if st not in board_svc.CLOSED_STATUSES[track]
        ],
        "page_label": "Leads" if track == "consulting" else "Applications",
        "notice": notice,
    }


async def _render_board(
    request: Request, track: str, *, notice: dict | None = None
) -> HTMLResponse:
    """The board partial. Always 200: HTMX does not swap a 4xx body, so a failure is a notice."""
    async with sessionmaker()() as s:
        board = await _board(s, track, notice=notice)
    ctx = {"query": {}, "view": "kanban", **board}
    return templates().TemplateResponse(request, "partials/pursuit_board.html", ctx)


@router.get("/pursuits", response_class=HTMLResponse)
async def pursuits(request: Request):
    track = active_track(request)
    view = request.query_params.get("view") or "kanban"
    from kestrel.analytics.today import switched_off_work

    async with sessionmaker()() as s:
        board = await _board(s, track)
        # R2: a switched-off track's open pursuits are never hidden; a line names the track
        # and links to its board (an explicit ?track= opens it, D-UI-57).
        off = [o for o in await switched_off_work(s) if o["track"] != track]
    ctx = {
        "title": board["page_label"], "query": dict(request.query_params), "view": view, **board,
        "off_tracks": off,
    }
    if request.headers.get("HX-Request"):
        return templates().TemplateResponse(request, "partials/pursuit_board.html", ctx)
    return templates().TemplateResponse(request, "pursuits.html", ctx)


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
    return await _render_board(request, track, notice=notice)


@router.post("/pursuits/{pursuit_id}/status", response_class=HTMLResponse)
async def move_status(request: Request, pursuit_id: int):
    """Move a pursuit to a new status and WRITE the status_event audit row (the stage select and
    drag-and-drop both land here). Rejects a status that is not valid for the pursuit's track.
    An HTMX caller gets the refreshed board; a plain post gets 204."""
    form = await request.form()
    to_status = str(form.get("to_status") or "").strip()
    async with sessionmaker()() as s:
        p = await s.get(Pursuit, pursuit_id)
        if p is None:
            return HTMLResponse("not found", status_code=404)
        track = p.track
        # One path for every move (board_svc.move_to_status): the applied defaults, closed_at
        # set or cleared, the status_event. An invalid stage is a 200 notice, since HTMX does
        # not swap a 4xx body and the operator would see nothing happen.
        err = await board_svc.move_to_status(
            s, p, to_status, note=str(form.get("note") or "") or None
        )
        if err:
            await s.rollback()
        else:
            await s.commit()
    if request.headers.get("HX-Request"):
        notice = {"ok": False, "text": err} if err else None
        return await _render_board(request, track, notice=notice)
    if err:
        return HTMLResponse(html.escape(err), status_code=400)
    return HTMLResponse("", status_code=204)


@router.post("/pursuits/{pursuit_id}/remove", response_class=HTMLResponse)
async def remove_card(request: Request, pursuit_id: int):
    """Remove from my shortlist / leads, from the board card (#23). The same full undo as the
    decision panel's Remove (D-UI-75, opportunity_actions.remove_from_list): the pursuit with its
    history, outcome and offers goes in one transaction. Answers with the board plus the sidebar
    badges out of band. Always 200: HTMX does not swap a 4xx body, so a failure is a notice."""
    from kestrel.web.errors import plain_error
    from kestrel.web.pages.opportunity_actions import remove_from_list
    from kestrel.web.routes_api import oob_nav_counts
    from kestrel.web.words import words

    track = active_track(request)
    notice: dict
    async with sessionmaker()() as s:
        try:
            p = await s.get(Pursuit, pursuit_id)
            opp = await s.get(Opportunity, p.opportunity_id) if p is not None else None
            if p is None or opp is None:
                notice = {"ok": False, "text": "That card is no longer on the board."}
            else:
                track = p.track
                notice = {"ok": True, "text": await remove_from_list(s, opp)}
        except Exception as exc:  # a write path must never 500 (CLAUDE.md hard rules)
            log.warning(
                "board_remove.failed", pursuit_id=pursuit_id,
                error_type=type(exc).__name__, error=str(exc),
            )
            await s.rollback()
            notice = {
                "ok": False,
                "text": f"Could not remove it from your {words(track)['list']}: {plain_error(exc)}",
            }
        oob = await oob_nav_counts(s)
    resp = await _render_board(request, track, notice=notice)
    resp.body += oob.encode("utf-8")
    resp.headers["content-length"] = str(len(resp.body))
    return resp


@router.post("/pursuits/{pursuit_id}/act/{verb}", response_class=HTMLResponse)
async def act(request: Request, pursuit_id: int, verb: str):
    """A card button: record what the operator did and move the card. Returns the board."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    async with sessionmaker()() as s:
        p = await s.get(Pursuit, pursuit_id)
        if p is None:
            return HTMLResponse("not found", status_code=404)
        track = p.track
        err = await board_svc.apply_verb(s, p, verb, form)
        if err:
            await s.rollback()
        else:
            await s.commit()
    notice = {"ok": False, "text": err} if err else None
    return await _render_board(request, track, notice=notice)


@router.post("/pursuits/{pursuit_id}/act/applied/quick", response_class=HTMLResponse)
async def applied_quick(request: Request, pursuit_id: int):
    """The one-click path of "Applied?": records applied today with a follow-up in seven days
    (and the resume chosen for the button), then answers with the button turned green. The
    partial asks the board to refresh itself a moment later, which moves the card to Applied.
    The "different date" form beneath still posts to the ordinary applied verb."""
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


@router.post("/pursuits/{pursuit_id}/outcome", response_class=HTMLResponse)
async def capture_outcome(request: Request, pursuit_id: int):
    """Outcome capture on close: writes the Outcome row (learning entry), moves the card to the
    chosen final status with a status_event, and closes the pursuit."""
    form = await request.form()
    async with sessionmaker()() as s:
        p = await s.get(Pursuit, pursuit_id)
        if p is None:
            return HTMLResponse("not found", status_code=404)
        track = p.track
        final = str(form.get("final_status") or "").strip() or "closed"
        if final != "closed" and not valid_pursuit_status(track, final):
            return HTMLResponse("invalid status", status_code=400)
        days = str(form.get("days_to_first_response") or "").strip()
        reason = str(form.get("rejection_reason") or "").strip() or None
        stage = str(form.get("furthest_stage") or "").strip() or None
        s.add(
            Outcome(
                pursuit_id=p.id,
                opportunity_id=p.opportunity_id,
                final_status=final,
                furthest_stage=stage,
                days_to_first_response=int(days) if days.isdigit() else None,
                rejection_reason=reason,
                operator_retrospective=str(form.get("retrospective") or "") or None,
            )
        )
        if final != "closed" and final != p.status:
            await set_pursuit_status(s, p.id, final, note=reason)
        p.outcome = final
        p.outcome_stage = stage
        p.outcome_reason = reason
        p.closed_at = utcnow_iso()
        p.next_action = p.next_action_at = None
        p.last_activity_at = utcnow_iso()
        await s.commit()
    if request.headers.get("HX-Request"):
        return await _render_board(
            request, track,
            notice={"ok": True, "text": "Outcome recorded. It feeds the learning loop."},
        )
    return HTMLResponse("Outcome recorded. Thank you. This feeds the learning loop.")
