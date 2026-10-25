"""Interview prep (D-UI-121): what the page needs, shared by `/prep`, `/mock` and `/stories`.

One screen with three tabs (Prep pack, Practice, Story bank). The three URLs render the same
shell on a different tab, so a link that already exists keeps working. Everything here reads
stored rows; the one model call on the screen (a practice answer) lives in `pages/mock.py`.

Confidential mode (hard rule, Privacy): the current employer is redacted in every list and
generated text on the page. A story is the operator's own text; the cards show it redacted and
only the editing sheet shows it as written, because that is the thing being edited."""

from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Any

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.date_display import format_display
from kestrel.db.models import (
    MockSession,
    Opportunity,
    Org,
    PrepPack,
    Pursuit,
    Story,
)
from kestrel.interview import prep_state
from kestrel.interview.coverage import TARGET_MAX, TARGET_MIN, theme_coverage
from kestrel.interview.mock import MODES
from kestrel.interview.notes import all_notes, get_notes
from kestrel.interview.prep import live_view
from kestrel.interview.story_task import build_state, last_build
from kestrel.pipeline._shared import tier_is_hosted
from kestrel.services import pursuit_board as board
from kestrel.web.deps import templates
from kestrel.web.redact import confidential_once, redact_json, redact_name, redact_text

TABS = ("pack", "practice", "bank")
BOOKED = ("interviewing", "final")
# The least a trend needs (hard rule 6): under this many finished sessions it says so.
TREND_MIN = 5
_STEP_RE = re.compile(r"\(([^()]{1,40})\)")
_MODE_WORDS = {
    "behavioral": ("Behavioral", "users", "Questions about what you have done"),
    "technical_leadership": ("Leadership", "lead", "How you lead teams and decisions"),
    "executive_panel": ("Executive panel", "brief", "Senior leaders, broad questions"),
    "consulting_discovery": ("Discovery call", "target", "The first call with a buyer"),
}
_EMPLOYMENT_MODES = ("behavioral", "technical_leadership", "executive_panel")
_CONSULTING_MODES = ("consulting_discovery",)
THEME_WORDS = {
    "conflict": "Conflict",
    "failure": "Failure",
    "influence_without_authority": "Influence without authority",
    "turnaround": "Turnaround",
    "prioritization": "Prioritization under constraint",
    "difficult_stakeholder": "Difficult stakeholder",
    "technical_judgment": "Technical judgment",
    "team_building": "Team building",
}


def qhash(text: str) -> str:
    """A short stable id for a question, so a form never has to carry its text."""
    return hashlib.sha1(text.strip().casefold().encode("utf-8")).hexdigest()[:10]


async def question_for(session: AsyncSession, opportunity_id: int, h: str) -> str | None:
    """The role's likely question whose id is `h`, as the page lists it now; None when the list
    changed since the page was drawn."""
    lv = await live_view(session, opportunity_id)
    return next((q["question"] for q in (lv.questions if lv else []) if qhash(q["question"]) == h),
                None)


def is_consulting(track: str | None) -> bool:
    return track == "consulting"


def page_title(track: str | None) -> str:
    return "Discovery call prep" if is_consulting(track) else "Interview prep"


def tab_labels(track: str | None) -> list[tuple[str, str]]:
    return [
        ("pack", "Call prep" if is_consulting(track) else "Prep pack"),
        ("practice", "Practice"),
        ("bank", "Story bank"),
    ]


def modes_for(track: str | None) -> list[dict]:
    keys = _CONSULTING_MODES if is_consulting(track) else _EMPLOYMENT_MODES
    return [
        {"key": k, "name": _MODE_WORDS[k][0], "icon": _MODE_WORDS[k][1], "note": _MODE_WORDS[k][2]}
        for k in keys if k in MODES
    ]


def mono(name: str | None) -> dict:
    """Two letters and a color number for a company badge; the color follows the name."""
    words = (name or "?").split()
    letters = (words[0][:1] + (words[1][:1] if len(words) > 1 else "")).upper() or "?"
    return {"letters": letters, "tone": sum(ord(c) for c in (name or "")) % 6}


# ------------------------------------------------------------------------------- the header
def when_parts(stamp: str | None, *, on: date | None = None) -> dict:
    """The interview date as words: 'Tue, Oct 28 · 10:00 AM', days left and a countdown."""
    day = board.parse_day(stamp)
    if day is None:
        return {"date": None, "label": "", "days": None, "countdown": ""}
    on = on or date.today()
    clock = board.clock_label(stamp)
    label = format_display(day, "weekday_comma") + (f" · {clock}" if clock else "")
    days = (day - on).days
    if days > 1:
        words = f"in {days} days"
    elif days == 1:
        words = "tomorrow"
    elif days == 0:
        words = "today"
    elif days == -1:
        words = "yesterday"
    else:
        words = f"{-days} days ago"
    return {"date": day, "label": label, "days": days, "countdown": words}


def step_name(next_action: str | None) -> str:
    """The named step ('Panel interview') when the pursuit's next action carries one, as
    'Interview (Panel)' does; blank otherwise."""
    m = _STEP_RE.search(next_action or "")
    return m.group(1).strip() if m else ""


async def booked_roles(session: AsyncSession) -> list[tuple[Opportunity, Pursuit]]:
    rows = (
        await session.execute(
            select(Opportunity, Pursuit)
            .join(Pursuit, Pursuit.opportunity_id == Opportunity.id)
            .where(Pursuit.status.in_(BOOKED), Pursuit.closed_at.is_(None))
            .order_by(Pursuit.next_action_at.is_(None), Pursuit.next_action_at, Pursuit.id)
        )
    ).all()
    return [(o, p) for o, p in rows]


async def default_role_id(session: AsyncSession) -> int | None:
    rows = await booked_roles(session)
    return rows[0][0].id if rows else None


async def shell_ctx(session: AsyncSession, opportunity_id: int | None) -> dict:
    """The header of the page: the role, the roles to pick from, and the interview date."""
    conf, emp = await confidential_once(session)

    def red(text: str | None) -> str:
        return redact_text(text or "", emp, enabled=conf) or ""

    booked = await booked_roles(session)
    opp = await session.get(Opportunity, opportunity_id) if opportunity_id else None
    pursuit = None
    if opp is not None:
        pursuit = (
            await session.execute(select(Pursuit).where(Pursuit.opportunity_id == opp.id))
        ).scalar_one_or_none()
    org = await session.get(Org, opp.org_id) if opp is not None and opp.org_id else None
    org_name = (org.name if org else (opp.org_name if opp else None)) or ""
    booked_here = pursuit is not None and pursuit.status in BOOKED and pursuit.closed_at is None
    when = when_parts(pursuit.next_action_at if booked_here else None)
    roles: list[dict] = []
    seen: set[int] = set()
    for o, p in [*booked, *([(opp, pursuit)] if opp is not None and not booked_here else [])]:
        if o.id in seen:
            continue
        seen.add(o.id)
        w = when_parts(p.next_action_at if p is not None and p.status in BOOKED else None)
        roles.append({
            "id": o.id, "title": red(o.title) or "Role",
            "org": redact_name(o.org_name or "", emp, enabled=conf) or "",
            "when": w["label"], "step": step_name(p.next_action if p is not None else None),
            "mono": mono(o.org_name),
        })
    track = opp.track if opp is not None else None
    has_pack = opp is not None and await latest_pack(session, opp.id) is not None
    story_count = (
        await session.execute(
            select(func.count()).select_from(Story).where(Story.is_canonical.is_(True))
        )
    ).scalar_one()
    if track is None:
        track = booked[0][0].track if booked else None
    return {
        "opp": opp, "opp_id": opp.id if opp is not None else None, "track": track,
        "con": is_consulting(track), "pursuit": pursuit,
        "role_title": red(opp.title) if opp is not None else "",
        "org_name": redact_name(org_name, emp, enabled=conf) or "",
        "mono": mono(org_name), "when": when,
        "step": step_name(pursuit.next_action) if booked_here else "",
        "roles": roles, "page_title": page_title(track), "conf": conf, "emp": emp,
        "tabs": tab_labels(track), "story_count": story_count, "has_pack": has_pack,
        "base": f"/prep/{opp.id}" if opp is not None else "/prep",
    }


# ----------------------------------------------------------------------------- the prep pack
async def latest_pack(session: AsyncSession, opportunity_id: int) -> PrepPack | None:
    return (
        await session.execute(
            select(PrepPack).where(PrepPack.opportunity_id == opportunity_id)
            .order_by(PrepPack.created_at.desc(), PrepPack.id.desc()).limit(1)
        )
    ).scalar_one_or_none()


def _story_view(s: Story, *, red) -> dict:
    return {
        "id": s.id, "title": red(s.title), "situation": red(s.situation),
        "action": red(s.action), "result": red(s.result), "themes": list(s.themes or []),
        "used": s.use_count or 0, "mine": bool(s.operator_edited),
    }


async def canonical_stories(session: AsyncSession) -> list[Story]:
    """Proposals awaiting review first, then the operator's most-used stories."""
    stmt = (
        select(Story).where(Story.is_canonical.is_(True))
        .order_by(Story.operator_edited.asc(), Story.use_count.desc(), Story.id.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def pack_ctx(session: AsyncSession, sh: dict) -> dict:
    opp = sh["opp"]
    if opp is None:
        return {"pack": None}
    conf, emp = sh["conf"], sh["emp"]

    def red(text: str | None) -> str:
        return redact_text(text or "", emp, enabled=conf) or ""

    prep = await latest_pack(session, opp.id)
    if prep is None:
        return {"pack": None, "has_pack": False, "notes": await get_notes(session, opp.id)}
    lv = await live_view(session, opp.id)
    stories = {s.id: s for s in await canonical_stories(session)}
    choices = await prep_state.story_choices(session, opp.id)
    rows: list[dict] = []
    for i, q in enumerate(lv.questions if lv else []):
        sid = choices.get(q["question"], q["story_id"])
        story = stories.get(sid)
        why = q["why"]
        if q["kind"] == "gap":
            frm = f"Gap they may probe: {why}" if why else "A gap they may probe"
        elif why:
            frm = why
        else:
            frm = "Drafted when Kestrel scored this role"
        rows.append({
            "n": i + 1, "q": red(q["question"]), "qh": qhash(q["question"]), "from": red(frm),
            "kind": q["kind"], "story": _story_view(story, red=red) if story else None,
        })
    have = sum(1 for r in rows if r["story"])
    generated = [str(a) for a in (prep.questions_to_ask or []) if str(a).strip()]
    added, ticked = await prep_state.ask_state(session, opp.id)
    ask = prep_state.ask_rows(generated, added, ticked)
    for a in ask:
        a["shown"] = red(a["text"])
    panel = [
        {"name": red(p.get("name")), "title": red(p.get("title") or p.get("contact_type")),
         "letters": (p.get("name") or "?")[:1].upper(), "mine": False}
        for p in (prep.panel or []) if isinstance(p, dict)
    ]
    panel += [
        {"name": red(p["name"]), "title": red(p["title"]), "letters": p["name"][:1].upper(),
         "mine": True}
        for p in await prep_state.people(session, opp.id)
    ]
    gaps = [
        {"mark": "!" if g.get("severity") == "fatal" else "~",
         "fatal": g.get("severity") == "fatal",
         "requirement": red(g.get("requirement")), "mitigation": red(g.get("mitigation"))}
        for g in (lv.gaps if lv else [])
    ]
    return {
        "pack": prep, "has_pack": True, "questions": rows, "have": have, "total": len(rows),
        "need": len(rows) - have, "ask": ask, "ask_on": sum(1 for a in ask if a["on"]),
        "panel": panel, "gaps": gaps, "summary": red(lv.role_summary if lv else ""),
        "stories": [_story_view(s, red=red) for s in stories.values()],
        "notes": await get_notes(session, opp.id),
        "ring_circ": 163.36,  # 2 * pi * 26
    }


async def pack_model(session: AsyncSession, sh: dict) -> dict | None:
    """The paged PDF/preview model (kestrel.interview.render.pack_pages) from the same state the
    page shows: the operator's own story text, the ticked questions, the company notes."""
    opp = sh["opp"]
    if opp is None:
        return None
    ctx = await pack_ctx(session, sh)
    if not ctx.get("has_pack"):
        return None
    conf, emp = sh["conf"], sh["emp"]
    lv = await live_view(session, opp.id)
    when = sh["when"]["label"] or "Date not set"
    if sh["step"]:
        when += f" · {sh['step']}"
    return {
        "title": "Call prep" if sh["con"] else "Interview prep",
        "ask_heading": "What they will want to know" if sh["con"] else "What they will likely ask",
        "role": sh["role_title"] or "Role", "org": sh["org_name"] or "", "when": when,
        "summary": ctx["summary"],
        "questions": [
            {
                "q": r["q"],
                "story_title": r["story"]["title"] if r["story"] else "",
                "story_text": (r["story"]["action"] or r["story"]["result"] or "") if r["story"]
                else "",
            }
            for r in ctx["questions"]
        ],
        "asks": [a["shown"] for a in ctx["ask"] if a["on"]],
        "notes": [redact_text(n, emp, enabled=conf) or "" for n in (lv.notes if lv else [])][:6],
    }


# ------------------------------------------------------------------------------ the practice
def _mean(scores: dict | None) -> float | None:
    vals = [float(v) for v in (scores or {}).values() if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 1) if vals else None


def pending_question(ms: MockSession | None) -> str | None:
    """The question awaiting an answer (a transcript carries at most one), so a killed session
    resumes on exactly this turn (hard rule 5)."""
    if ms is None:
        return None
    for turn in ms.transcript or []:
        if turn.get("answer") is None:
            return turn.get("question")
    return None


def turns_of(ms: MockSession | None, emp, conf: bool) -> list[dict]:
    """The answered turns of a session, redacted, each with its place in the transcript (`_i`)."""
    if ms is None:
        return []
    return [
        {**redact_json(t, emp, enabled=conf), "_i": i}
        for i, t in enumerate(ms.transcript or []) if t.get("answer") is not None
    ]


async def practice_ctx(session: AsyncSession, sh: dict) -> dict:
    opp = sh["opp"]
    if opp is None:
        return {}
    conf, emp = sh["conf"], sh["emp"]
    rows = (
        await session.execute(
            select(MockSession).where(MockSession.opportunity_id == opp.id)
            .order_by(MockSession.started_at.desc(), MockSession.id.desc())
        )
    ).scalars().all()
    active = next((h for h in rows if h.ended_at is None), None)
    done = [h for h in rows if h.ended_at is not None and (h.question_count or 0) > 0]
    chart = [
        {"mean": _mean(h.mean_scores), "label": board.short_date(h.started_at)}
        for h in reversed(done[:8]) if _mean(h.mean_scores) is not None
    ]
    lv = await live_view(session, opp.id)
    from kestrel.services.setting_overrides import load_settings_with_db

    pending = redact_text(pending_question(active), emp, enabled=conf) if active else None
    return {
        "route": await route_info(session, await load_settings_with_db(session)),
        "hint": await practice_hint(session, opp.id, pending_question(active)),
        "modes": modes_for(sh["track"]), "active": active, "history": done,
        "pending": pending,
        "turns": turns_of(active, emp, conf), "chart": chart, "live_mean": _mean(
            active.mean_scores) if active is not None else None,
        "chart_max": 5, "sessions": len(done),
        "trend_ok": len(done) >= TREND_MIN, "trend_min": TREND_MIN,
        "q_total": len(lv.questions) if lv else 0,
    }


# ------------------------------------------------------------------------------ the story bank
async def build_ctx(session: AsyncSession) -> dict:
    from kestrel.interview.story_task import BUILD_SENDS, TIER
    from kestrel.services.online_consent import online_route
    from kestrel.services.setting_overrides import load_settings_with_db

    state = await build_state(session)
    return {
        "build_route": await online_route(
            session, await load_settings_with_db(session), TIER, this_send=BUILD_SENDS
        ),
        "building": state["state"] == "building",
        "build_failed": state.get("reason") if state["state"] == "failed" else None,
        "last_build": await last_build(session),
    }


async def notes_ctx(session: AsyncSession) -> dict:
    """"Your notes" (D-UI-55): each role with saved interview notes, linking to that role's prep
    page, and the roles the operator can add notes to."""
    conf, emp = await confidential_once(session)
    saved = await all_notes(session)
    titles: dict[int, tuple[str, str, str]] = {}
    ids = {opp_id for opp_id, _t, _at in saved}
    if ids:
        rows = (
            await session.execute(select(Opportunity).where(Opportunity.id.in_(ids)))
        ).scalars().all()
        titles = {o.id: (o.title or "Role", o.org_name or "", o.track) for o in rows}
    notes_list = [
        {
            "opportunity_id": opp_id,
            "title": titles.get(opp_id, ("Role", "", "employment"))[0],
            "org": titles.get(opp_id, ("", "", ""))[1],
            "saved_at": format_display(at),
            "preview": " ".join(
                (redact_text(text, emp, enabled=conf) or "").split()
            )[:140],
        }
        for opp_id, text, at in reversed(saved)
        if opp_id in titles
    ]
    picks = (
        await session.execute(
            select(Opportunity).join(Pursuit, Pursuit.opportunity_id == Opportunity.id)
            .where(Pursuit.closed_at.is_(None)).order_by(Pursuit.id.desc())
        )
    ).scalars().all()
    return redact_json({
        "notes_list": notes_list, "notes_count": len(notes_list),
        "note_choices": [
            {"id": o.id, "label": o.title or "Role", "org": o.org_name or ""} for o in picks
        ],
    }, emp, enabled=conf)


async def bank_ctx(session: AsyncSession, sh: dict, **extra: Any) -> dict:
    conf, emp = sh["conf"], sh["emp"]

    def red(text: str | None) -> str:
        return redact_text(text or "", emp, enabled=conf) or ""

    report = await theme_coverage(session)
    stories = await canonical_stories(session)
    mine = [s for s in stories if s.operator_edited]
    drafts = [s for s in stories if not s.operator_edited]
    tiles = []
    for t in report.themes:
        n = len(t.story_ids)
        tiles.append({
            "key": t.theme, "name": THEME_WORDS.get(t.theme, t.theme.replace("_", " ").title()),
            "state": "ok" if n >= 2 else ("weak" if n == 1 else "none"),
            "label": "Strong" if n >= 2 else ("One story" if n == 1 else "No story yet"),
        })
    gaps = [t for t in tiles if t["state"] == "none"]
    return {
        **await notes_ctx(session), **await build_ctx(session),
        "tiles": tiles, "covered": len(tiles) - len(gaps), "gap_first": gaps[0]["name"] if gaps
        else None, "target": f"{TARGET_MIN} to {TARGET_MAX}", "total_stories": len(stories),
        "mine": [_story_view(s, red=red) for s in mine],
        "drafts": [_story_view(s, red=red) for s in drafts],
        "story_notice": None, "story_error": None, **extra,
    }


# ----------------------------------------------------------------------------- the whole body
async def body_ctx(
    request: Request, session: AsyncSession, tab: str, opportunity_id: int | None, **extra: Any
) -> dict:
    tab = tab if tab in TABS else "pack"
    sh = await shell_ctx(session, opportunity_id)
    ctx: dict[str, Any] = {
        **sh, "tab": tab, "query": dict(request.query_params), "title": sh["page_title"],
        "auto_build": False, "build_error": None, "pdf_error": None, "notes_saved": False,
    }
    if tab == "pack":
        ctx.update(await pack_ctx(session, sh))
    elif tab == "practice":
        ctx.update(await practice_ctx(session, sh))
        qh = request.query_params.get("qh") or ""
        focus = await question_for(session, sh["opp_id"], qh) if qh and sh["opp"] else None
        ctx["focus_qh"] = qh if focus else ""
        ctx["focus_q"] = redact_text(focus, sh["emp"], enabled=sh["conf"]) if focus else ""
    else:
        ctx.update(await bank_ctx(session, sh))
    ctx.update(extra)
    return ctx


def render_body(request: Request, ctx: dict, *, headers: dict | None = None):
    return templates().TemplateResponse(request, "partials/pp_body.html", ctx, headers=headers)


async def ask_text(session: AsyncSession, opportunity_id: int, index: str) -> str | None:
    """The text of the `index`th question to ask them, as the page lists them now."""
    prep = await latest_pack(session, opportunity_id)
    if prep is None or not index.isdigit():
        return None
    added, ticked = await prep_state.ask_state(session, opportunity_id)
    rows = prep_state.ask_rows(
        [str(a) for a in (prep.questions_to_ask or []) if str(a).strip()], added, ticked
    )
    i = int(index)
    return rows[i]["text"] if 0 <= i < len(rows) else None


async def practice_hint(
    session: AsyncSession, opportunity_id: int, question: str | None
) -> dict:
    """The hint for the question on screen: the operator's own story Kestrel matched to it, if
    any. Nothing is written for them (hard rule 2)."""
    if not question:
        return {"story": None}
    lv = await live_view(session, opportunity_id)
    choices = await prep_state.story_choices(session, opportunity_id)
    stories = {s.id: s for s in await canonical_stories(session)}
    conf, emp = await confidential_once(session)
    for q in lv.questions if lv else []:
        if q["question"].strip().casefold() == question.strip().casefold():
            story = stories.get(choices.get(q["question"], q["story_id"]))
            return {"story": _story_view(
                story, red=lambda text: redact_text(text, emp, enabled=conf),
            ) if story else None}
    return {"story": None}


# ------------------------------------------------------------------- where practice runs
PRACTICE_TIER = "tier2"  # P9 (mock_critique) runs on the full-review tier
_PRACTICE_SENDS = (
    "each question and your typed answer, and the stories and resume lines it checks your "
    "claims against"
)


async def route_info(session: AsyncSession, settings, *, with_models: bool = False) -> dict:
    """Where the next answer would be critiqued, as the call is routed. Local, or online (Hosted
    mode on and the tier on an external backend), with what is sent and what happens to the
    current employer. Models are fetched only for the dialog (`with_models`)."""
    from kestrel.llm.providers import is_free_variant
    from kestrel.services.online_consent import destination_id
    from kestrel.web.pages.needs_try import _employer_state, _offered_models, _sentence

    info: dict = {
        "online": False, "native": False, "can_switch": settings.llm.hosted_mode.active,
        "employer": await _employer_state(session, settings),
        "practice_sends": _PRACTICE_SENDS,
    }
    sends = settings.llm.hosted_mode.disclosure_terms()["sends"]
    if tier_is_hosted(settings, PRACTICE_TIER):
        from kestrel.web.pages.rescore import _hosted_models

        tcfg = settings.llm.tiers[PRACTICE_TIER]
        info.update({
            "online": True, "native": True, "model": tcfg.model,
            "dest": destination_id(settings, PRACTICE_TIER),
            "provider": (await _hosted_models(settings))[0] or tcfg.backend or "the online service",
            "sends": _sentence(sends[PRACTICE_TIER]),
            "any_free": is_free_variant(str(tcfg.model)),
        })
    elif with_models:
        models, hosted = await _offered_models(settings, None)
        info.update({
            "models": models, "provider": hosted["label"] or "the online service",
            "note": hosted["note"], "sends": _sentence(sends[PRACTICE_TIER]),
            "any_free": any(is_free_variant(str(m["id"])) for m in models),
        })
        import json

        info["approval_json"] = json.dumps(
            {PRACTICE_TIER: [model["dest"] for model in models if model.get("dest")]},
            separators=(",", ":"),
        )
    return info


