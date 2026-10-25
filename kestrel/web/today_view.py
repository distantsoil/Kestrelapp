"""What the Today screen prints (D-UI-92, D-UI-156): the greeting, the status strip, the Top
matches cards, Decisions waiting, Applications needing action and New signals.

Everything here reads rows that already exist; no model is called and nothing is sent anywhere
(hard rules 1 and 2). The cards print the operator's own words and the model's stored read, never
text this module writes about a role. Confidential mode is applied here, not in the template, so
the current employer is redacted on every list (CLAUDE.md privacy). A figure that rests on fewer
than five observations is labeled, never shown as if it were solid (hard rule 6)."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import structlog
from sqlalchemy import select

from kestrel.analytics.today import actions_due
from kestrel.db.models import DocMaster, Evaluation, Opportunity, Org, Pursuit, VerdictOverride
from kestrel.web import pipeline_view as pv
from kestrel.web.fit import GROUP_FAILED, GROUP_NEW, GROUP_WORTH
from kestrel.web.queries import (
    GROUP_ORDER,
    OppFilters,
    _base_stmt,
    _group_expr,
    _with_operator,
    list_signals,
    local_day_start_iso,
    row_fit,
    saved_clause,
)
from kestrel.web.redact import confidential_once, redact_name, redact_text

log = structlog.get_logger()

# How many cards the carousel holds. A wider window shows more of them at once; the rest are a
# swipe away. Today's roles only.
CARD_LIMIT = 24
# The most rows each lower panel prints; the header count is the whole number.
PANEL_LIMIT = 6
# Skill tags per card (the mockup shows up to three).
TAG_LIMIT = 3
_KIND_LABELS = {"contract_posting": "Contract posting", "rfp": "RFP", "referral": "Referral"}
ESTIMATE_NOTE = (
    "Estimated. This posting lists no pay. The range comes from similar roles Kestrel has seen "
    "and may be wrong. How many roles it rests on is not recorded."
)


# ------------------------------------------------------------------------------- the greeting
def greeting_word(now: datetime | None = None) -> str:
    """Good morning before noon, Good afternoon before 6 PM, Good evening after (local time)."""
    hour = (now or datetime.now()).hour
    return "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"


def greeting(first_name: str, now: datetime | None = None) -> str:
    """"Good morning, Jordan." or "Good morning." when the profile has no first name yet."""
    word = greeting_word(now)
    return f"{word}, {first_name}." if first_name else f"{word}."


def first_name_for(track: str) -> str:
    """The operator's first name from profile.yml: the active track's block, else the other's.
    Empty when the file has none (the greeting then drops the comma and name)."""
    from kestrel.services import names
    from kestrel.services.profile_edit import load_state

    try:
        state = load_state()
    except Exception as exc:  # a broken profile file must never break the landing page
        log.warning("today_profile_unreadable", error=str(exc))
        return ""
    for key in (track, "employment", "consulting"):
        name = names.greeting_name(state.tracks.get(key))
        if name:
            return name
    return ""


# -------------------------------------------------------------------------------- skill tags
async def operator_skills(s, track: str) -> list[str]:
    """The operator's own skills: their resume skill groups for the track plus the profile's
    must-haves. Never invented; an empty list means no tags."""
    seen: dict[str, str] = {}

    def add(raw: Any) -> None:
        text = " ".join(str(raw or "").split())
        if 2 <= len(text) <= 40 and text.lower() not in seen:
            seen[text.lower()] = text

    for groups in (
        await s.execute(
            select(DocMaster.skill_groups).where(
                DocMaster.track == track, DocMaster.is_active.is_(True)
            )
        )
    ).scalars():
        for g in groups or []:
            for item in (g.get("items") if isinstance(g, dict) else None) or []:
                add(item)
    from kestrel.services.profile_edit import load_state

    try:
        block = load_state().tracks.get(track) or {}
    except Exception as exc:
        log.warning("today_profile_unreadable", error=str(exc))
        block = {}
    for item in block.get("must_have") or []:
        add(item)
    return list(seen.values())


def skill_tags(skills: list[str], text: str, limit: int = TAG_LIMIT) -> list[str]:
    """The operator's skills that the posting mentions, matched by text with no model call. A
    skill matches as a whole word or phrase, case-insensitively ("SQL" never matches "mysql")."""
    hay = text or ""
    out: list[str] = []
    for skill in skills:
        pattern = rf"(?<![\w+#]){re.escape(skill)}(?![\w+#])"
        if re.search(pattern, hay, re.IGNORECASE):
            out.append(skill)
            if len(out) >= limit:
                break
    return out


# --------------------------------------------------------------------------------- why it fits
def _first_sentence(text: str, limit: int = 200) -> str:
    text = " ".join((text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    out = m.group(1) if m else text
    if len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    if out and out[-1] not in ".!?…":
        out += "."
    return out


def why_it_fits(opp, ev_json: dict | None) -> str:
    """Two to three sentences, labeled as model-written by the card: the model's read of the
    role, then the evidence line of its strongest match. `headline_reason` is not stored on the
    evaluation (a migration would be needed), so the read is block A's first sentence; neither
    part adds a model call or a new field (D-UI-92, D-UI-156)."""
    b = (ev_json or {}).get("blocks") or {}
    parts: list[str] = []
    lead = pv.summary_text(opp, ev_json)
    if lead:
        parts.append(_first_sentence(lead))
    if opp.track == "consulting":
        thesis = b.get("b_thesis") if isinstance(b.get("b_thesis"), dict) else {}
        extra = thesis.get("engagement") if thesis else ""
        if extra:
            parts.append(_first_sentence(str(extra)))
    else:
        fit = b.get("b_fit") if isinstance(b.get("b_fit"), dict) else {}
        matches = [m for m in (fit.get("matches") or []) if isinstance(m, dict)]
        rank = {"strong": 0, "partial": 1, "adjacent": 2}
        matches.sort(key=lambda m: rank.get(m.get("strength"), 3))
        for m in matches:
            if m.get("evidence"):
                parts.append(_first_sentence(str(m["evidence"])))
                break
    return " ".join(p for p in parts if p)


# ------------------------------------------------------------------- the strong match cutoff
def strong_cutoff(settings) -> float:
    """The Strong match cutoff the operator set in Settings (`thresholds.apply`, 4.0 by default).
    A "new strong match" alert follows it, not a separate `notify.high_score` (SPEC.md 7.3,
    D-UI-156)."""
    return float(settings.thresholds.apply)


def is_strong_match(score: float | None, settings) -> bool:
    return score is not None and score >= strong_cutoff(settings)


# ---------------------------------------------------------------------------- the card parts
def _card(opp, ev, fit, org_name, *, mapping, conf, skills: list[str],
          settings=None) -> dict[str, Any]:
    ev_json = {"blocks": ev.blocks or {}} if ev is not None else None
    pending = fit.group == GROUP_NEW
    pay = pv.pay_text(opp)
    estimate = pv.is_estimate(opp)
    if estimate:
        pay = pay.removesuffix(" est.")
    text = " ".join(filter(None, [opp.title, opp.description_md]))
    consulting = opp.track == "consulting"
    return {
        "id": opp.id,
        "mono": pv.monogram(org_name),
        "title": redact_text(opp.title or "Untitled", mapping, enabled=conf),
        "org": redact_name(org_name, mapping, enabled=conf),
        "place": redact_text(pv.place_text(opp), mapping, enabled=conf),
        "word": fit.word,
        "tone": fit.tone,
        "score": fit.score,
        "pending": pending,
        "pay": pay,
        "estimate": estimate,
        "estimate_note": ESTIMATE_NOTE,
        "value": pv.engagement_value(opp, ev) if consulting and not pending else "",
        "kind": _KIND_LABELS.get(opp.opp_type, "") if consulting else "",
        "why": "" if pending else redact_text(why_it_fits(opp, ev_json), mapping, enabled=conf),
        "tags": [] if pending else skill_tags(skills, text),
        "consulting": consulting,
        "strong": bool(settings is not None and not pending
                       and is_strong_match(fit.score, settings)),
    }


def _rank(c: dict[str, Any]) -> tuple:
    return (c["pending"], -(c["score"] if c["score"] is not None else -1.0), -c["id"])


async def top_matches(s, track: str, *, limit: int = CARD_LIMIT, settings=None) -> dict[str, Any]:
    """Today's newly found roles, best fit first, whatever the score (a day that only produced 1s
    and 2s still shows them). Today's roles only (the local day), not the backlog. A role the
    operator already decided (shortlisted, saved, labeled not for me), one the rules set aside, a
    scoring failure (the retry note covers it) and a signal are not cards. A role with only a
    quick screen is a card that says its full review is in progress. `found` counts every role
    found today, whether or not it earns a card."""
    stmt = _with_operator(
        _base_stmt(
            OppFilters(track=track, new_today=True),
            Opportunity, Evaluation, Org.name, Pursuit.id, VerdictOverride.operator_verdict,
            saved_clause(),
        )
    ).where(Opportunity.opp_type != "trigger_event")
    rows = (await s.execute(stmt)).all()
    if settings is None:
        from kestrel.services.setting_overrides import load_settings_with_db

        settings = await load_settings_with_db(s)
    conf, mapping = await confidential_once(s)
    skills = await operator_skills(s, track)
    cards: list[dict[str, Any]] = []
    for opp, ev, org_name, pursuit_id, label, saved in rows:
        if pursuit_id is not None or saved or label == "pass":
            continue
        if opp.stage in ("t0_reject", "t1_reject"):
            continue
        fit = row_fit(opp, ev)
        if fit.group == GROUP_FAILED or (fit.group == "not_fit" and ev is None):
            continue
        cards.append(
            _card(opp, ev, fit, org_name or opp.org_name, mapping=mapping, conf=conf,
                  skills=skills, settings=settings)
        )
    cards.sort(key=_rank)
    return {"cards": cards[:limit], "found": len(rows), "n": len(cards)}


async def cards_after(s, track: str, have: set[int], settings=None) -> dict[str, Any]:
    """The cards the page does not have yet, each with the id of the card it sits before (None:
    at the end), so a new match opens a gap at its ranked position (live updates, D-UI-156)."""
    data = await top_matches(s, track, settings=settings)
    cards = data["cards"]
    out = []
    for i, c in enumerate(cards):
        if c["id"] in have:
            continue
        before = next((x["id"] for x in cards[i + 1:] if x["id"] in have), None)
        out.append({"card": c, "before": before})
    return {"new": out, "found": data["found"], "ids": [c["id"] for c in cards]}


# ----------------------------------------------------------------------- decisions waiting
def _decision_status(track: str, pursuit_status: str | None) -> tuple[str, str]:
    """(key, words) for a decision row."""
    if pursuit_status is None:
        return "undecided", "Not decided"
    if track == "consulting":
        return "listed", "In leads, not contacted"
    return "listed", "Shortlisted, not applied"


async def decisions_waiting(s, track: str, *, limit: int = PANEL_LIMIT,
                            settings=None) -> dict[str, Any]:
    """Roles the operator has not decided (not shortlisted, applied or set aside) plus shortlisted
    roles not yet applied to; in Consulting, undecided work plus leads not yet contacted. The
    undecided ones are the backlog: today's new roles wait in Top matches instead, so a role never
    sits in both."""
    since = local_day_start_iso()
    if settings is None:
        from kestrel.services.setting_overrides import load_settings_with_db

        settings = await load_settings_with_db(s)
    stmt = _with_operator(
        _base_stmt(
            OppFilters(track=track), Opportunity, Evaluation, Org.name, Pursuit.id,
            Pursuit.status, Pursuit.applied_at, Pursuit.closed_at,
            VerdictOverride.operator_verdict, saved_clause(),
        )
    ).where(
        _group_expr() == GROUP_ORDER.index(GROUP_WORTH), Opportunity.opp_type != "trigger_event"
    )
    conf, mapping = await confidential_once(s)
    undecided: list[dict[str, Any]] = []
    listed: list[dict[str, Any]] = []
    for opp, ev, org_name, pid, pstatus, applied_at, closed_at, label, saved in (
        await s.execute(stmt)
    ).all():
        fit = row_fit(opp, ev)
        if pid is None:
            if saved or label is not None or (opp.first_seen_at or "") >= since:
                continue
            bucket = undecided
        else:
            first = "researching" if track == "consulting" else ("shortlisted", "preparing")
            wanted = (first,) if isinstance(first, str) else first
            if closed_at or applied_at or pstatus not in wanted:
                continue
            bucket = listed
        key, words_ = _decision_status(track, pstatus if pid is not None else None)
        bucket.append(
            {
                "id": opp.id,
                "pursuit_id": pid,
                "title": redact_text(opp.title or "Untitled", mapping, enabled=conf),
                "org": redact_name(org_name or opp.org_name, mapping, enabled=conf),
                "score": fit.score,
                "tone": "good" if is_strong_match(fit.score, settings) else "warn",
                "status_key": key,
                "status": words_,
                "consulting": track == "consulting",
            }
        )
    for bucket in (undecided, listed):
        bucket.sort(key=lambda r: (-(r["score"] if r["score"] is not None else -1), -r["id"]))
    rows = undecided + listed
    return {"rows": rows[:limit], "n": len(rows)}


# --------------------------------------------------------------------- applications needing
async def applications_needing_action(s, track: str, *, limit: int = PANEL_LIMIT) -> dict:
    """Follow-ups and interviews coming up (Consulting: proposal due, follow-up, call), from the
    same query the Applications rail reads. Each row offers the Applications verb that fits:
    Prepare for a call, Follow up again for a quiet reply; anything else opens Applications.
    There is no Draft follow-up (D-UI-148)."""
    data = await actions_due(s, track, limit=limit)
    rows = []
    for it in data["items"]:
        action = it["action"] or ""
        low = action.lower()
        if it["bucket"] == "outcome":
            kind, verb = "Interview", "open"
        elif "interview" in low or low.startswith("call"):
            kind, verb = ("Call" if track == "consulting" else "Interview"), "prepare"
        elif "proposal" in low:
            kind, verb = "Proposal due", "open"
        else:
            kind, verb = "Follow-up", "follow"
        label = {"overdue": "Overdue", "today": "Today", "tomorrow": "Tomorrow"}.get(
            it["bucket"], it["label"] if it["bucket"] != "outcome" else "Record the outcome"
        )
        due_day = date.fromisoformat(it["due"])
        if it["bucket"] == "overdue":
            days = (date.today() - due_day).days
            label = f"Due {days} {'day' if days == 1 else 'days'} ago"
        rows.append({**it, "kind": kind, "verb": verb, "when": label,
                     "urgent": it["bucket"] in ("overdue", "today", "outcome")})
    return {"rows": rows, "n": data["n"]}


# ------------------------------------------------------------------------------ new signals
async def new_signals(s, *, limit: int = 3) -> dict[str, Any]:
    """Consulting only: public trigger events not yet acted on, newest first, with their fit.
    Trigger events appear only here (Top matches leaves them out)."""
    conf, mapping = await confidential_once(s)
    sigs = [
        r for r in await list_signals(s, OppFilters(track="consulting")) if r.pursuit_id is None
    ]
    rows = []
    for r in sigs[:limit]:
        td = r.opp.trigger_data or {}
        score = None
        if r.ev is not None:
            score = r.ev.score if r.ev.score is not None else r.ev.weighted_score
        source = " ".join(str(td.get("source_label") or td.get("source") or "").split())
        from kestrel.date_display import format_display

        when = format_display(r.opp.event_date)
        rows.append(
            {
                "id": r.opp.id,
                "org": redact_name(r.org_name, mapping, enabled=conf),
                "what": redact_text(" ".join((r.opp.title or "").split()), mapping, enabled=conf),
                "source": " · ".join(x for x in (source, when) if x),
                "score": score,
                "tone": "good" if (score or 0) >= 3.5 else "warn",
                "org_href": (f"/orgs/{r.opp.org_id}" if r.opp.org_id
                             else f"/opportunities/{r.opp.id}"),
            }
        )
    return {"rows": rows, "n": len(sigs)}


# -------------------------------------------------------------------------------- the strip
def model_segment(card: dict[str, Any], mode: str) -> dict[str, str]:
    """Segment 3: where the AI runs, in the three phrasings the spec gives. `card` is
    runs_page._model_card; `mode` is local, hosted or both."""
    model = card.get("model") or ""
    provider = card.get("provider") or ""
    credit = card.get("left") or ""
    credit_short = credit.replace(" on your key's limit", "")
    if mode == "both":
        bits = [model, provider, credit_short]
        return {"title": "Local, plus your online key",
                "detail": " · ".join(b for b in bits if b), "tone": ""}
    if mode == "hosted":
        bits = [provider, model, "BYOK", credit_short.replace("left", "credit left")]
        return {"title": "Online model, your key",
                "detail": " · ".join(b for b in bits if b), "tone": ""}
    return {"title": "Model on this computer",
            "detail": " · ".join(b for b in (model, "local and private") if b), "tone": ""}


# ------------------------------------------------------------------------------- finish setup
async def finish_setup(s) -> dict[str, Any] | None:
    """The banner for an install whose onboarding is not complete (D-UI-30, D-UI-116): how many
    steps are left and what they are, read from the real things (a resume on file, a stored
    choice of how scoring runs), never from a stored flag. None once onboarding is complete."""
    from sqlalchemy import func

    from kestrel.db.models import Setting
    from kestrel.services import onboarding as ob
    from kestrel.services import setup_state as ss

    if await ob.is_complete(s):
        return None
    left: list[str] = []
    if not (await s.execute(select(func.count(DocMaster.id)))).scalar_one():
        left.append("add your resume text")
    if await s.get(Setting, ss.MODE_KEY) is None:
        left.append("choose how scoring runs")
    n = len(left) or 1
    todo = " and ".join(left) if left else "open the setup guide to finish"
    return {"n": n, "line": todo[:1].upper() + todo[1:] + "."}
