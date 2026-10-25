"""Settings > Which roles you see (D-UI-106, D-UI-107, SPEC-INTERFACES 2.6 item 2).

  GET  /settings/roles/rules?track=     Rules that hide roles: a switch, a count and See them
  POST /settings/roles/rule             switch one rule on or off (off queues a t0_recheck)
  POST /settings/roles/number           days old (1 to 365) or weeks too short (1 to 52)
  GET  /settings/roles/see              the roles a rule would hide, each with Show it anyway
  POST /settings/roles/show/{id}        put one such role into Pipeline (Undo available)
  GET  /settings/roles/cutoffs?track=   Strong match and Worth a look
  POST /settings/roles/cutoffs          change one cutoff, or restore the defaults
  GET  /settings/roles/hidden           Hidden companies
  POST /settings/roles/unhide/{org_id}  show a company's roles again

Nothing hidden is deleted, and a role with no posted pay, place or clearance statement is kept
because the rule has nothing to check (hard rule 4). The pay, titles, level and places are edited
in Profile, never here. Every change answers with the card it changed, so only that card swaps,
and a message with Undo."""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import SQLAlchemyError

from kestrel.db.enums import STAGES
from kestrel.db.models import Opportunity, Org, Setting
from kestrel.pipeline.profile import load_profile
from kestrel.services import hidden_orgs, pursuit_undo, tier0_override
from kestrel.services import tracks as tracks_svc
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import settings_io as io
from kestrel.web import settings_view as sv
from kestrel.web.deps import active_track, sessionmaker, templates
from kestrel.web.errors import plain_error
from kestrel.web.redact import confidential, redact_name, redact_text

log = structlog.get_logger()
router = APIRouter()

CUTOFF_DEFAULTS = (4.0, 3.0)
SHOWN_REASON = "You chose to show this role despite your rule"


# ----------------------------------------------------------------------------- the response
def trigger(message: str = "", *, undo: str | None = None, refresh: dict | None = None,
            tone: str | None = None) -> dict[str, str]:
    """An HX-Trigger header carrying a message with Undo. `refresh` names the fragment to fetch
    again after the Undo, so a list refreshed after Undo never loses the refresh."""
    if not message:
        return {}
    detail: dict[str, Any] = {"message": message}
    if undo:
        detail["undoUrl"] = undo
    if refresh:
        detail.update(refresh)
    if tone:
        detail["tone"] = tone
    return {"HX-Trigger": json.dumps({"k-toast": detail}, ensure_ascii=True)}


def _track(request: Request) -> str:
    asked = request.query_params.get("track")
    if asked in sv.TRACKS:
        return asked
    return active_track(request)


async def _form(request: Request) -> dict[str, str]:
    """A posted form or JSON body as plain strings."""
    ctype = request.headers.get("content-type", "")
    if "json" in ctype:
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return {}
        return {k: str(v) for k, v in body.items()} if isinstance(body, dict) else {}
    form = await request.form()
    return {k: str(v) for k, v in form.items()}


# ------------------------------------------------------------------------------- the rules
async def _rules_off(session, track: str) -> list[str]:
    value = await io.read(session, f"tier0.rules_off.{track}")
    return [str(x) for x in value] if isinstance(value, list) else []


async def rules_ctx(request: Request, track: str, *, error: dict | None = None) -> dict:
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        counts = await sv.rule_counts(
            s, settings, track, shown_ids=await tier0_override.overridden_ids(s)
        )
        off = await _rules_off(s, track)
        enabled = tuple(await tracks_svc.load_enabled(s))
    profile = load_profile(track)
    rows = []
    for d in sv.RULES[track]:
        c = counts.get(d.key) or sv.RuleCount(d.key)
        shown = sum(1 for r in c.roles if r.shown)
        n = max(0, c.n - shown)
        value = sv.number_value(track, d.number, settings, profile) if d.number else None
        rows.append({
            "def": d, "on": d.rule not in off, "n": n, "total": c.n, "of_n": c.of_n,
            "line": sv.rule_line(track, d.key, settings, profile),
            "value": value,
            "short": sv.SHORT_NAMES.get(d.number or "", d.name),
            "label": sv.NUMBER_LABEL.get(d.number or "", ""),
            "limits": sv.NUMBER_LIMITS.get(d.number or "", (0, 0)),
            "error": (error or {}).get(d.key),
            "has_pay_note": d.key in ("pay", "rate"),
        })
    return {
        "track": track, "tracks": enabled, "rule_rows": rows,
        "rule_ids": ", ".join(d.rule for d in sv.RULES[track]),
    }


def _rules_card(request: Request, ctx: dict, headers: dict | None = None) -> HTMLResponse:
    return templates().TemplateResponse(
        request, "partials/st_rules.html", ctx, headers=headers or {}
    )


@router.get("/settings/roles/rules", response_class=HTMLResponse)
async def rules_card(request: Request):
    return _rules_card(request, await rules_ctx(request, _track(request)))


async def queue_rule_recheck(track: str) -> int:
    """Switching a rule off (or loosening it) lets the roles it had set aside come back: queue
    the Tier 0 re-check for the track. The completed-inputs stamp is cleared first so the
    check runs even though the profile did not change. Never fails the save."""
    from kestrel.db.engine import get_sessionmaker
    from kestrel.pipeline import recheck

    try:
        async with get_sessionmaker()() as s:
            row = await s.get(Setting, f"{recheck.FINGERPRINT_PREFIX}{track}")
            if row is not None:
                await s.delete(row)
                await s.commit()
        return await recheck.queue_after_save(get_sessionmaker(), track)
    except (SQLAlchemyError, OSError, ValueError) as exc:
        log.warning("tier0_recheck_queue_failed", track=track, error=str(exc))
        return 0


@router.post("/settings/roles/rule", response_class=HTMLResponse)
async def rule_switch(request: Request):
    f = await _form(request)
    track = f.get("track", "")
    d = sv.rule_def(track, f.get("key", ""))
    if d is None:
        return HTMLResponse("That rule is not one Kestrel has.", status_code=404)
    on = f.get("on") in ("1", "true", "on")
    key = f"tier0.rules_off.{track}"
    async with sessionmaker()() as s:
        prev = await io.read(s, key)
        off = await _rules_off(s, track)
        was_on = d.rule not in off
        if on == was_on:
            ctx = await rules_ctx(request, track)
            return _rules_card(request, ctx)
        new = [r for r in off if r != d.rule] if on else [*off, d.rule]
        now = io.ABSENT if not new else new
        await io.write(s, key, now)
        await s.commit()
    if not on:
        await queue_rule_recheck(track)
    name = d.name if not d.number else sv.SHORT_NAMES[d.number]
    msg = f"{name} is on." if on else f"{name} is off. Those roles will show again."
    refresh = {"refresh": f"/settings/roles/rules?track={track}", "target": "#st-rules"}
    return _rules_card(
        request, await rules_ctx(request, track),
        trigger(msg, undo=io.undo_url(key, prev, now), refresh=refresh),
    )


@router.post("/settings/roles/number", response_class=HTMLResponse)
async def rule_number(request: Request):
    f = await _form(request)
    track = f.get("track", "")
    d = sv.rule_def(track, f.get("key", ""))
    if d is None or not d.number:
        return HTMLResponse("That rule has no number.", status_code=404)
    lo, hi = sv.NUMBER_LIMITS[d.number]
    raw = (f.get("value") or "").strip()
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
    current = sv.number_value(track, d.number, settings, load_profile(track))
    if not raw.isdigit() or not lo <= int(raw) <= hi:
        err = {d.key: f"Use a whole number from {lo} to {hi}. Kestrel is still using {current}."}
        return _rules_card(request, await rules_ctx(request, track, error=err))
    value = int(raw)
    refresh = {"refresh": f"/settings/roles/rules?track={track}", "target": "#st-rules"}
    if value == current:
        return _rules_card(request, await rules_ctx(request, track))
    if d.number == "age":
        key = "max_posting_age_days" if track == "employment" else "consulting.max_trigger_age_days"
        async with sessionmaker()() as s:
            prev = await io.read(s, key)
            await io.write(s, key, value)
            await s.commit()
        await queue_rule_recheck(track)
        word = "roles" if track == "employment" else "postings"
        msg = f"{word.capitalize()} older than {value} days are hidden."
        undo = io.undo_url(key, prev, value)
    else:
        from kestrel.services import profile_edit as pe

        prev_days = getattr(getattr(load_profile("consulting"), "consulting_terms", None),
                            "min_engagement_days", None)
        days = value * 7
        try:
            pe.save_section("consulting", "consulting", {"consulting_terms": {
                "min_engagement_days": days}})
        except (pe.ProfileEditError, OSError) as exc:
            err = {d.key: f"Could not save that: {plain_error(exc)}"}
            return _rules_card(request, await rules_ctx(request, track, error=err))
        from kestrel.db.engine import get_sessionmaker
        from kestrel.pipeline import recheck

        try:
            await recheck.queue_after_save(get_sessionmaker(), "consulting")
        except (SQLAlchemyError, OSError, ValueError) as exc:
            log.warning("tier0_recheck_queue_failed", track="consulting", error=str(exc))
        msg = f"Projects under {value} {'week' if value == 1 else 'weeks'} are hidden."
        undo = (f"/settings/roles/weeks/undo?prev={prev_days}&now={days}"
                if prev_days is not None else None)
    return _rules_card(request, await rules_ctx(request, track),
                       trigger(msg, undo=undo, refresh=refresh))


@router.post("/settings/roles/weeks/undo")
async def weeks_undo(request: Request):
    """Put the shortest project back, only while the profile still holds what the save wrote."""
    from kestrel.services import profile_edit as pe

    q = request.query_params
    cur = getattr(getattr(load_profile("consulting"), "consulting_terms", None),
                  "min_engagement_days", None)
    if q.get("now") != str(cur) or not (q.get("prev") or "").isdigit():
        return io.refuse(io.NO_UNDO)
    try:
        pe.save_section("consulting", "consulting", {"consulting_terms": {
            "min_engagement_days": int(q["prev"])}})
    except (pe.ProfileEditError, OSError) as exc:
        return io.refuse(plain_error(exc))
    return io.reply("Put back.")


# ------------------------------------------------------------------------------ See them
async def _see_ctx(request: Request, track: str, key: str) -> dict | None:
    d = sv.rule_def(track, key)
    if d is None:
        return None
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        counts = await sv.rule_counts(
            s, settings, track, shown_ids=await tier0_override.overridden_ids(s)
        )
        enabled, mapping = await confidential(s)
    c = counts.get(key) or sv.RuleCount(key)
    roles = []
    for r in c.roles:
        roles.append({
            "id": r.opp_id,
            "title": redact_text(r.title, mapping, enabled=enabled),
            "org": redact_name(r.org, mapping, enabled=enabled),
            "why": redact_text(r.why, mapping, enabled=enabled),
            "shown": r.shown, "inpipe": r.stage != "t0_reject" and not r.shown,
        })
    return {
        "track": track, "key": key, "name": sv.SHORT_NAMES.get(d.number or "", d.name),
        "shown_n": len(roles), "total": c.n, "of_n": c.of_n, "roles": roles,
    }


@router.get("/settings/roles/see", response_class=HTMLResponse)
async def see_them(request: Request):
    ctx = await _see_ctx(request, _track(request), request.query_params.get("key", ""))
    if ctx is None:
        return HTMLResponse("That rule is not one Kestrel has.", status_code=404)
    return templates().TemplateResponse(request, "partials/st_see.html", ctx)


@router.post("/settings/roles/show/{opp_id}", response_class=HTMLResponse)
async def show_anyway(request: Request, opp_id: int):
    """Show it anyway: the role goes back to `new` with the operator's choice recorded, so Tier
    0 never sets it aside again for the rule that did. The Undo token carries exactly the stage
    and reason this changed; it reverts only a role that is still as this left it."""
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        if opp is None:
            return HTMLResponse("That role is gone.", status_code=404)
        if opp.stage != "t0_reject":
            ctx = {"r": await _row(s, opp, shown=False, inpipe=True)}
            return templates().TemplateResponse(request, "partials/st_see_row.html", ctx)
        prev_stage, prev_reason = opp.stage, opp.stage_reason or ""
        await tier0_override.record(s, opp.id, opp.stage_reason)
        opp.stage = "new"
        opp.stage_reason = SHOWN_REASON
        await s.commit()
        row = await _row(s, opp, shown=True, inpipe=False)
        title = row["title"]
    undo = ("/settings/roles/show/" + str(opp_id) + "/undo?" + _q(stage=prev_stage,
                                                                  reason=prev_reason))
    return templates().TemplateResponse(
        request, "partials/st_see_row.html", {"r": row},
        headers=trigger(f"{title} will show in your Pipeline.", undo=undo),
    )


def _q(**kw: str) -> str:
    from urllib.parse import urlencode

    return urlencode(kw)


async def _row(s, opp: Opportunity, *, shown: bool, inpipe: bool) -> dict:
    enabled, mapping = await confidential(s)
    return {
        "id": opp.id, "title": redact_text(opp.title or "Untitled role", mapping, enabled=enabled),
        "org": redact_name(opp.org_name, mapping, enabled=enabled) or "",
        "why": "", "shown": shown, "inpipe": inpipe,
    }


@router.post("/settings/roles/show/{opp_id}/undo")
async def show_undo(opp_id: int, request: Request):
    q = request.query_params
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        if opp is None or opp.stage != "new" or opp.stage_reason != SHOWN_REASON:
            return io.refuse(io.NO_UNDO)
        stage = q.get("stage") or "t0_reject"
        reason = q.get("reason") or None
        if stage not in STAGES or (reason is not None and len(reason) > 500):
            return io.refuse(io.NO_UNDO)
        opp.stage = stage
        opp.stage_reason = reason
        row = await s.get(Setting, tier0_override.key_for(opp_id))
        if row is not None:
            await s.delete(row)
        await s.commit()
    return io.reply("Put back.")


# -------------------------------------------------------------------------------- cutoffs
async def cutoffs_ctx(request: Request, track: str, *, error: str | None = None,
                      field: str | None = None, typed: dict | None = None) -> dict:
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        strong, look = settings.thresholds.apply, settings.thresholds.consider
        counts = await sv.cutoff_counts(s, track, strong, look)
        enabled = tuple(await tracks_svc.load_enabled(s))
    return {
        "track": track, "tracks": enabled, "strong": strong, "look": look,
        "counts": counts, "cut_error": error, "cut_field": field, "typed": typed or {},
        "defaults": CUTOFF_DEFAULTS, "f1": sv.f1,
    }


def _cut_card(request: Request, ctx: dict, headers: dict | None = None) -> HTMLResponse:
    return templates().TemplateResponse(request, "partials/st_cutoffs.html", ctx,
                                        headers=headers or {})


@router.get("/settings/roles/cutoffs", response_class=HTMLResponse)
async def cutoffs_card(request: Request):
    return _cut_card(request, await cutoffs_ctx(request, _track(request)))


@router.post("/settings/roles/cutoffs", response_class=HTMLResponse)
async def cutoffs_save(request: Request):
    f = await _form(request)
    track = f.get("track") if f.get("track") in sv.TRACKS else _track(request)
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
    cur_strong, cur_look = settings.thresholds.apply, settings.thresholds.consider
    refresh = {"refresh": f"/settings/roles/cutoffs?track={track}", "target": "#st-cut"}
    prev_s = prev_l = None

    async def stored(s):
        return await io.read(s, "thresholds.apply"), await io.read(s, "thresholds.consider")

    if f.get("restore"):
        if (cur_strong, cur_look) == CUTOFF_DEFAULTS:
            return _cut_card(request, await cutoffs_ctx(request, track),
                             trigger("Cutoffs are already at the defaults."))
        async with sessionmaker()() as s:
            prev_s, prev_l = await stored(s)
            await io.write(s, "thresholds.apply", io.ABSENT)
            await io.write(s, "thresholds.consider", io.ABSENT)
            await s.commit()
        msg = "Cutoffs restored to 4.0 and 3.0."
        new_s = new_l = io.ABSENT
    else:
        which = f.get("which", "")
        if which not in ("strong", "look"):
            return HTMLResponse("Unknown cutoff.", status_code=400)
        raw = f.get("value", "")
        if f.get("delta"):
            try:
                raw = str(round((cur_strong if which == "strong" else cur_look)
                                + float(f["delta"]) * 0.1, 1))
            except ValueError:
                raw = ""
        try:
            v = sv.parse_cutoff(raw)
            sv.check_cutoffs(v if which == "strong" else cur_strong,
                             v if which == "look" else cur_look)
        except ValueError as exc:
            return _cut_card(request, await cutoffs_ctx(
                request, track, error=str(exc), field=which, typed={which: raw}))
        if v == (cur_strong if which == "strong" else cur_look):
            return _cut_card(request, await cutoffs_ctx(request, track))
        key = "thresholds.apply" if which == "strong" else "thresholds.consider"
        async with sessionmaker()() as s:
            prev_s, prev_l = await stored(s)
            await io.write(s, key, v)
            await s.commit()
        new_s = v if which == "strong" else prev_s
        new_l = v if which == "look" else prev_l
        msg = f"{'Strong match' if which == 'strong' else 'Worth a look'} now starts at {v:.1f}."
    undo = ("/settings/roles/cutoffs/undo?" + _q(
        prev_s=io.dumps(prev_s if prev_s is not None else io.ABSENT),
        prev_l=io.dumps(prev_l if prev_l is not None else io.ABSENT),
        now_s=io.dumps(new_s), now_l=io.dumps(new_l)))
    return _cut_card(request, await cutoffs_ctx(request, track),
                     trigger(msg, undo=undo, refresh=refresh))


@router.post("/settings/roles/cutoffs/undo")
async def cutoffs_undo(request: Request):
    """Both cutoffs back as they were, only while both still read as the save left them."""
    q = request.query_params
    try:
        prev_s, prev_l = io.loads(q["prev_s"]), io.loads(q["prev_l"])
        now_s, now_l = io.loads(q["now_s"]), io.loads(q["now_l"])
    except (KeyError, ValueError):
        return io.refuse(io.NO_UNDO)
    async with sessionmaker()() as s:
        if (await io.read(s, "thresholds.apply"), await io.read(s, "thresholds.consider")) != (
            now_s, now_l
        ):
            return io.refuse(io.NO_UNDO)
        await io.write(s, "thresholds.apply", prev_s)
        await io.write(s, "thresholds.consider", prev_l)
        await s.commit()
    return io.reply("Put back.")


# ------------------------------------------------------------------------ hidden companies
async def hidden_ctx(request: Request) -> dict:
    async with sessionmaker()() as s:
        rows = await hidden_orgs.list_hidden(s)
        enabled, mapping = await confidential(s)
    return {"hidden": [
        {"org_id": r.org_id, "name": redact_name(r.name, mapping, enabled=enabled) or r.name,
         "role_count": r.role_count, "hidden_at": r.hidden_at}
        for r in rows
    ]}


@router.get("/settings/roles/hidden", response_class=HTMLResponse)
async def hidden_card(request: Request):
    return templates().TemplateResponse(request, "partials/st_hidden.html",
                                        await hidden_ctx(request))


@router.post("/settings/roles/unhide/{org_id}", response_class=HTMLResponse)
async def unhide(request: Request, org_id: int):
    async with sessionmaker()() as s:
        name = next((r.name for r in await hidden_orgs.list_hidden(s) if r.org_id == org_id), None)
        org = await s.get(Org, org_id)
        if org is None or org.hidden_at is None:
            return HTMLResponse("That company is not in the list.", status_code=404)
        before = org.hidden_at
        if not await hidden_orgs.unhide_org(s, org_id):
            return HTMLResponse("That company is not in the list.", status_code=404)
        token = await pursuit_undo.save(
            s, {"kind": "unhide_org", "org_id": org_id, "before": before})
        await s.commit()
        enabled, mapping = await confidential(s)
    shown = redact_name(name, mapping, enabled=enabled) if name else "The company"
    msg = f"{html.unescape(shown or 'The company')} is shown again. Its roles are back in Pipeline."
    refresh = {"refresh": "/settings/roles/hidden", "target": "#st-hidden"}
    return templates().TemplateResponse(
        request, "partials/st_hidden.html", await hidden_ctx(request),
        headers=trigger(
            msg, undo=f"/settings/roles/unhide/{org_id}/undo?token={token}", refresh=refresh),
    )


@router.post("/settings/roles/unhide/{org_id}/undo")
async def unhide_undo(request: Request, org_id: int):
    """Hide the company again, but only while it is still shown. JSON, as settings.js reads it."""
    async with sessionmaker()() as s:
        org = await s.get(Org, org_id)
        row = await s.get(Setting, pursuit_undo.PREFIX + request.query_params.get("token", ""))
        if (org is None or org.hidden_at is not None or row is None
                or row.value.get("kind") != "unhide_org" or row.value.get("org_id") != org_id
                or row.updated_at < (datetime.now(UTC) - pursuit_undo.KEEP).isoformat()):
            return io.refuse(io.NO_UNDO)
        org.hidden_at = row.value["before"]
        await s.delete(row)
        await s.commit()
    return io.reply("Hidden again.")


# --------------------------------------------------------------------------------- the pane
async def roles_pane_ctx(request: Request) -> dict:
    track = _track(request)
    ctx = await rules_ctx(request, track)
    ctx.update(await cutoffs_ctx(request, track))
    ctx.update(await hidden_ctx(request))
    return ctx

