"""Offers (D-UI-122, D-UI-136; SPEC-INTERFACES 2.1 `/offers`). Offer records benchmarked against
the operator's OWN accumulated data (comp corpus, stated band, real competing offers, accepted
history), scripts drafted for the operator to edit and deliver themselves, and a comparison when
more than one offer is open. Three tabs: Compare, Negotiate and History.

  GET  /offers                         the page (the body alone for an HTMX request); `?tab=`,
                                        `?pursuit=` picks the offer to negotiate
  GET  /offers/new                     the Record an offer sheet
  GET  /offers/{track}/{id}/edit       the same sheet, prefilled
  POST /offers                         record an offer
  POST /offers/{track}/{id}/edit       save changes to one
  POST /offers/{track}/{id}/rate       one star rating (a judgment row of the comparison)
  POST /offers/{track}/{id}/outcome    I accepted, I declined, It expired (each with Undo)
  POST /offers/{track}/{id}/reopen     Reopen, and the Undo of the three above
  POST /offers/weights                 how much each comparison row counts
  POST /offers/{id}/qual               the older all-at-once ratings form (kept for callers)
  POST /offers/{pursuit_id}/scripts    the draft reply

Three hard rules are enforced here, not merely in the modules underneath:
  - The walk-away number is the operator's. The draft form requires it as plain text; nothing on
    this page computes, defaults, or adjusts it, and an ask below it is refused.
  - Scripts are drafts. There is no send action anywhere on this page, and a script whose
    justification rests on nothing supplied is shown flagged.
  - Sample size is always visible. The benchmark always shows n; under five observations it shows
    the count in words and no range (hard rule 6).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from html import escape
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import (
    EngagementOffer,
    Evaluation,
    Offer,
    Opportunity,
    Org,
    OrgSummary,
    Pursuit,
    Setting,
)
from kestrel.intelligence.comp import MIN_OBSERVATIONS, comp_range
from kestrel.llm import client as llm_client
from kestrel.negotiation.benchmark import benchmark_offer
from kestrel.negotiation.compare import QUAL_AXES
from kestrel.negotiation.scripts import ScriptResult, _pp, draft_scripts
from kestrel.pipeline._shared import tier_is_hosted
from kestrel.pipeline.profile import load_profile
from kestrel.privacy import EmployerMap
from kestrel.services import offer_outcome
from kestrel.services.pursuit_board import move_to_status, parse_day, today
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import offers_view as ov
from kestrel.web.deps import active_track, sessionmaker, templates
from kestrel.web.errors import plain_error
from kestrel.web.redact import confidential_once, redact_json, redact_name, redact_text

log = structlog.get_logger()

router = APIRouter()

TABS = (("compare", "Compare"), ("negotiate", "Negotiate"), ("history", "History"))
_OPEN_STATUSES = offer_outcome.OPEN_STATUSES
_CLOSED_STATUSES = tuple(ov.OUTCOME_WORDS)
_OFFER_STATUSES = ("verbal", "written", "revised", "declined", "accepted", "expired")
# Statuses a pursuit can be in and still meaningfully hold an offer. A closed pursuit still
# accepts one (the operator may be closing the loop late, or keeping the record), so this widens
# the picker rather than excluding history.
_LIVE_PURSUIT_STATUSES = (
    "shortlisted", "preparing", "applied", "screening", "interviewing", "final", "offer",
    "negotiating", "scoping", "proposal_sent", "conversation",
)
# Which live stages an offer may be recorded against, per track's target stage. An offer most
# often arrives when the pursuit is already applied / screening / interviewing / final, so all of
# them move; closed stages are excluded so recording an offer never quietly reopens a pursuit.
_MOVABLE_TO_OFFER = {
    "offer": (
        "shortlisted", "preparing", "applied", "screening", "interviewing", "final",
    ),
    "negotiating": (
        "shortlisted", "researching", "outreach_sent", "conversation", "scoping",
        "proposal_sent",
    ),
}
_WEIGHTS_KEY = "negotiation.matrix_weights"
_QUAL_MAX = 5.0
_SHOWN = 12  # past offers listed on History
# The free-text fields a card shows. Typed by the operator, so they can name the current
# employer, and confidential mode redacts every one of them.
_TERM_FIELDS = (
    "payment_terms", "exclusivity", "equity_description", "level_title", "reports_to",
    "location_policy", "expenses", "renewal_terms", "ip_terms",
)
# The sheet's fields per track: (form name, kind) where kind is f(loat), i(nt) or s(tring).
_FIELDS = {
    "employment": (
        ("base", "f"), ("bonus_target_pct", "f"), ("equity_description", "s"),
        ("equity_value_estimate", "f"), ("signing", "f"), ("benefits_notes", "s"),
        ("pto_days", "i"), ("start_date", "s"), ("deadline", "s"), ("level_title", "s"),
        ("reports_to", "s"), ("location_policy", "s"), ("notes_md", "s"),
    ),
    "consulting": (
        ("day_rate", "f"), ("days_committed", "i"), ("duration_weeks", "i"),
        ("payment_terms", "s"), ("expenses", "s"), ("renewal_terms", "s"), ("exclusivity", "s"),
        ("ip_terms", "s"), ("notice_period", "s"), ("deadline", "s"), ("notes_md", "s"),
    ),
}


def _model_for_track(track: str):
    """The offer table for a track. `Offer` and `EngagementOffer` have independent id sequences, so
    a bare id is ambiguous across them and the track is what disambiguates."""
    if track == "employment":
        return Offer
    if track == "consulting":
        return EngagementOffer
    return None


# ----------------------------------------------------------------------------- reading offers
async def _names(session: AsyncSession, pursuit: Pursuit) -> tuple[Opportunity | None, str | None]:
    opp = (
        await session.get(Opportunity, pursuit.opportunity_id) if pursuit.opportunity_id else None
    )
    org = await session.get(Org, opp.org_id) if opp and opp.org_id else None
    return opp, (org.name if org else (opp.org_name if opp else None))


async def _offer_rows(session: AsyncSession, track: str) -> list[dict]:
    """The latest offer per pursuit on this track, kept when it is open, or closed within the last
    two weeks (a closed card stays, dimmed, so Reopen and Undo have somewhere to live). Mirrors the
    "latest offer per pursuit" rule benchmark_offer itself uses (kestrel/negotiation/benchmark.py
    `_latest_offer`) so the list and the benchmark never disagree about which offer is current."""
    model = _model_for_track(track)
    rows = (
        (
            await session.execute(
                select(model).order_by(model.pursuit_id, model.received_at.desc(), model.id.desc())
            )
        )
        .scalars()
        .all()
    )
    out: list[dict] = []
    seen: set[int] = set()
    cutoff = today() - timedelta(days=ov.RECENT_CLOSED_DAYS)
    for o in rows:
        if o.pursuit_id in seen:
            continue
        seen.add(o.pursuit_id)
        pursuit = await session.get(Pursuit, o.pursuit_id)
        if pursuit is None or (pursuit.track or "employment") != track:
            continue
        is_open = o.status in _OPEN_STATUSES
        if not is_open:
            closed_on = parse_day(pursuit.closed_at)
            if o.status not in _CLOSED_STATUSES or closed_on is None or closed_on < cutoff:
                continue
        opp, org_name = await _names(session, pursuit)
        out.append({"pursuit": pursuit, "offer": o, "track": track, "opp": opp,
                    "org_name": org_name, "open": is_open})
    return out


async def _past_offers(session: AsyncSession, track: str) -> list[dict]:
    """Every closed offer on this track (accepted, declined or expired), newest first."""
    model = _model_for_track(track)
    rows = (
        (
            await session.execute(
                select(model).where(model.status.in_(_CLOSED_STATUSES))
                .order_by(model.received_at.desc(), model.id.desc())
            )
        )
        .scalars()
        .all()
    )
    out = []
    for o in rows:
        pursuit = await session.get(Pursuit, o.pursuit_id)
        if pursuit is None or (pursuit.track or "employment") != track:
            continue
        opp, org_name = await _names(session, pursuit)
        out.append({"offer": o, "opp": opp, "org_name": org_name})
    return out


async def _pursuit_names(session: AsyncSession, pursuit_id: int) -> tuple[str | None, str | None]:
    """(organization, role) for a pursuit, for the competing-offers line."""
    pursuit = await session.get(Pursuit, pursuit_id)
    if pursuit is None:
        return None, None
    opp, org_name = await _names(session, pursuit)
    return org_name, (opp.title if opp else None)


async def _pickable_pursuits(session: AsyncSession, track: str) -> list[dict]:
    """Pursuits an offer can be recorded against: a live one, or anything with no offer yet."""
    rows = (
        (await session.execute(select(Pursuit).order_by(Pursuit.status, Pursuit.id)))
        .scalars()
        .all()
    )
    have_offer: set[int] = set()
    for model in (Offer, EngagementOffer):
        for pid in (
            await session.execute(select(model.pursuit_id).distinct())
        ).scalars().all():
            have_offer.add(pid)
    out: list[dict] = []
    for p in rows:
        if (p.track or "employment") != track:
            continue
        if p.status not in _LIVE_PURSUIT_STATUSES and p.id in have_offer:
            continue
        opp, org_name = await _names(session, p)
        out.append(
            {
                "pursuit": p,
                "track": track,
                "org_name": org_name,
                "title": opp.title if opp else "Untitled",
                "has_offer": p.id in have_offer,
            }
        )
    return out


async def _weights(session: AsyncSession) -> dict[str, float]:
    """The operator's comparison weights, or nothing so every row reads Matters."""
    row = await session.get(Setting, _WEIGHTS_KEY)
    if row is None or not isinstance(row.value, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in row.value.items():
        if k in ov.WEIGHT_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool) \
                and v >= 0:
            out[k] = float(v)
    return out


def _terms_of(offer: Any) -> dict[str, str | None]:
    return {f: getattr(offer, f, None) for f in _TERM_FIELDS}


def _floor_target(track: str) -> tuple[float | None, float | None]:
    """The operator's floor and target, from the profile (Profile sets them). Blank is None."""
    try:
        profile = load_profile(track)
    except (OSError, ValueError):  # a profile that cannot be read is no floor, not a failure
        profile = None
    if track == "consulting":
        terms = getattr(profile, "consulting_terms", None)
        floor, target = (getattr(terms, k, None) for k in ("day_rate_floor", "day_rate_target"))
    else:
        terms = getattr(profile, "employment_terms", None)
        floor, target = (getattr(terms, k, None) for k in ("comp_floor_usd", "comp_target_usd"))
    return ov.num(floor), ov.num(target)


async def _fit_score(session: AsyncSession, opportunity_id: int | None) -> float | None:
    """The evaluation's own score for the role, out of five, from its deepest tier that has one."""
    if not opportunity_id:
        return None
    rows = (
        (
            await session.execute(
                select(Evaluation)
                .where(Evaluation.opportunity_id == opportunity_id)
                .order_by(Evaluation.tier.desc(), Evaluation.id.desc())
            )
        )
        .scalars()
        .all()
    )
    for e in rows:
        v = e.weighted_score if e.weighted_score is not None else e.score
        if v is not None:
            return max(0.0, min(5.0, float(v)))
    return None


async def _risk_fact(session: AsyncSession, opp: Opportunity | None) -> str | None:
    """What research says about hiring at the company, in its own words, when there is a summary."""
    if opp is None or not opp.org_id:
        return None
    row = (
        await session.execute(select(OrgSummary).where(OrgSummary.org_id == opp.org_id))
    ).scalar_one_or_none()
    if row is None or row.status != "ok":
        return None
    for tag in row.tags or []:
        if isinstance(tag, dict) and tag.get("key") == "hiring" and tag.get("text"):
            return str(tag["text"])
    return None


async def _build_cards(session: AsyncSession, track: str, fmt: str) -> list[dict]:
    """Every shown offer on this track as a card the templates draw."""
    rows = await _offer_rows(session, track)
    conf_on, emp_map = await confidential_once(session)
    floor, target = _floor_target(track)
    con = track == "consulting"
    on = today()
    cards: list[dict] = []
    for row in rows:
        offer, opp, pursuit = row["offer"], row["opp"], row["pursuit"]
        bench = await benchmark_offer(session, pursuit.id)
        competing = []
        for c in bench.competing if bench else []:
            org, title = await _pursuit_names(session, c["pursuit_id"])
            competing.append({**c, "org_name": redact_name(org, emp_map, enabled=conf_on),
                              "title": redact_text(title, emp_map, enabled=conf_on)})
        terms = {f: redact_text(v, emp_map, enabled=conf_on) for f, v in _terms_of(offer).items()}
        org_shown = redact_name(row["org_name"], emp_map, enabled=conf_on) or "Unknown org"
        title = redact_text(opp.title, emp_map, enabled=conf_on) if opp else None
        value = ov.num(getattr(offer, "day_rate" if con else "base", None))
        comp = bench.comp if bench else None
        similar = None
        if comp is not None and comp.sufficient and comp.p25 is not None and comp.p75 is not None:
            similar = (comp.p25, comp.p75)
        similar_n = comp.n if comp is not None else 0
        closed = offer.status if offer.status in _CLOSED_STATUSES else None
        risk = await _risk_fact(session, opp)
        cards.append(
            {
                "id": offer.id,
                "track": track,
                "pursuit_id": pursuit.id,
                "offer": offer,
                "org": org_shown,
                "title": title or "Untitled",
                "mono": ov.initials(org_shown),
                "mono_i": ov.mono_index(org_shown),
                "status": ov.OUTCOME_WORDS.get(offer.status) or offer.status.capitalize(),
                "tone": "bad" if closed in ("declined", "expired") else
                        "good" if offer.status in ("written", "accepted") else "info",
                "closed": closed,
                "open": row["open"],
                "total": ov.card_total(offer, track),
                "deadline": ov.deadline_view(getattr(offer, "deadline", None), today=on, fmt=fmt),
                "band": ov.band_view(
                    value=value, floor=floor, target=target, similar=similar,
                    similar_n=similar_n, unit="Day rate" if con else "Base",
                    noun="work" if con else "roles",
                ),
                "bench": ov.bench_lines(
                    track=track, value=value, floor=floor, target=target, similar=similar,
                    similar_n=similar_n, posted=bench.stated_band if bench else None,
                    inside_posted=bench.inside_stated_band if bench else None,
                ),
                "corpus_line": comp.summary() if comp is not None else "",
                "exact": ov.exact_line(offer, track),
                "facts": ov.card_facts(offer, track, terms, fmt),
                "terms": terms,
                "competing": competing,
                "history": bench.history if bench else [],
                "fit": await _fit_score(session, opp.id if opp else None),
                "risk_fact": redact_text(risk, emp_map, enabled=conf_on),
            }
        )
    return cards


# ------------------------------------------------------------------------------- page context
def _tab_of(request: Request) -> str:
    asked = request.query_params.get("tab", "compare")
    return asked if asked in dict(TABS) else "compare"


async def _history_rows(session: AsyncSession, track: str, fmt: str) -> list[dict]:
    conf_on, emp_map = await confidential_once(session)
    con = track == "consulting"
    out = []
    for r in (await _past_offers(session, track))[:_SHOWN]:
        o, opp = r["offer"], r["opp"]
        value = ov.num(getattr(o, "day_rate" if con else "base", None))
        out.append({
            "who": redact_text(opp.title, emp_map, enabled=conf_on) if opp else "Untitled",
            "org": redact_name(r["org_name"], emp_map, enabled=conf_on) or "Unknown org",
            "value": ov.day_rate(value) if con else ov.money_k(value),
            "outcome": ov.OUTCOME_WORDS.get(o.status, o.status),
            "good": o.status == "accepted",
            "when": ov.day_text(o.received_at, fmt),
        })
    return out


async def _market_groups(session: AsyncSession, track: str) -> list[dict]:
    """ "What roles like yours pay", before any offer: the titles the operator is pursuing, each
    with the comp corpus range against their floor and target, or "Too few" under five."""
    con = track == "consulting"
    floor, target = _floor_target(track)
    conf_on, emp_map = await confidential_once(session)
    pursuits = (
        (await session.execute(select(Pursuit).where(Pursuit.track == track))).scalars().all()
    )
    counts: dict[str, int] = {}
    shown: dict[str, str] = {}
    for p in pursuits:
        opp = await session.get(Opportunity, p.opportunity_id) if p.opportunity_id else None
        if opp is None or not (opp.title or "").strip():
            continue
        key = opp.title.strip().lower()
        counts[key] = counts.get(key, 0) + 1
        shown.setdefault(key, opp.title.strip())
    groups = []
    for key in sorted(counts, key=lambda k: (-counts[k], k))[:4]:
        rng = await comp_range(session, normalized_title=key)
        # There is no day-rate corpus in this stack; a consulting range is shown only when every
        # observation really is a day rate.
        ok = (
            rng.sufficient
            and rng.p25 is not None
            and rng.p75 is not None
            and (rng.interval == "day" if con else rng.interval == "year")
        )
        similar = (rng.p25, rng.p75) if ok else None
        pts = [
            p
            for p in (floor, target, *(similar or ()), rng.median if ok else None)
            if p is not None
        ]
        lo, hi = (min(pts), max(pts)) if pts else (0.0, 1.0)
        pad = (hi - lo) * 0.18 or hi * 0.1 or 1.0
        lo, hi = max(0.0, lo - pad), hi + pad

        def pc(v: float, lo: float = lo, hi: float = hi) -> float:
            return round(max(0.0, min(100.0, (v - lo) / (hi - lo) * 100)), 1)

        k = ov.money_k
        groups.append(
            {
                "title": redact_text(shown[key], emp_map, enabled=conf_on),
                "n": rng.n,
                "ok": ok,
                "span": (
                    f"from {rng.n} similar {'engagements' if con else 'roles'}"
                    if ok
                    else f"Too few to tell ({rng.n})"
                ),
                "left": pc(similar[0]) if similar else 0,
                "width": round(pc(similar[1]) - pc(similar[0]), 1) if similar else 0,
                "mid": pc(rng.median) if ok and rng.median is not None else 0,
                "floor": pc(floor) if floor is not None else None,
                "target": pc(target) if target is not None else None,
                "nums": (
                    f"{k(similar[0])} to {k(similar[1])}{' a day' if con else ''}, "
                    f"middle {k(rng.median)}"
                )
                if similar and rng.median is not None
                else f"Under {MIN_OBSERVATIONS}, so Kestrel shows no range"
                f"{'' if con else ' for this title'}.",
            }
        )
    return groups


NEGOTIATE_TIER = "tier3"  # prompt negotiate/v1 runs on this tier (llm/prompts/negotiate.v1.md)
_DRAFT_SENDS = (
    "the offer terms, your walk-away and ask, your proof points and the pay data Kestrel holds "
    "for similar roles"
)


async def _negotiate_route(session: AsyncSession, settings: Any) -> dict[str, Any]:
    """Where the draft is written, as the call will actually be routed: on this computer, or by an
    online model (Hosted mode on, and the negotiate tier pointed at an external backend). An
    online route carries exactly what is sent (the Tier 3 line of the Hosted mode disclosure, the
    text the operator acknowledged, plus what this draft adds) and what happens to the current
    employer. The server checks the same facts again when a draft is asked for."""
    if not tier_is_hosted(settings, NEGOTIATE_TIER):
        return {"online": False}
    tcfg = settings.llm.tiers[NEGOTIATE_TIER]
    from kestrel.services.online_consent import destination_id
    from kestrel.web.pages.needs_try import _employer_state, _hosted_models

    label = (await _hosted_models(settings))[0]

    return {
        "online": True,
        "model": tcfg.model,
        "dest": destination_id(settings, NEGOTIATE_TIER),
        "provider": (label or tcfg.backend or "the online service"),
        "sends": settings.llm.hosted_mode.disclosure_terms()["sends"][NEGOTIATE_TIER],
        "draft_sends": _DRAFT_SENDS,
        "employer": await _employer_state(session, settings),
    }


async def _offers_ctx(request: Request, *, oob: bool = False) -> dict:
    """The body's context: the tab, the offers with their benchmarks, the comparison when more than
    one is open, and what the other tabs need. Built in one place so `page` and every mutation
    render from the same stored state and can never disagree."""
    track = active_track(request)
    tab = _tab_of(request)
    q = request.query_params
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        fmt = settings.region.date_format
        cards = await _build_cards(s, track, fmt)
        weights = await _weights(s)
        open_cards = [c for c in cards if c["open"]]
        ctx: dict[str, Any] = {
            "title": "Offers",
            "query": dict(q),
            "track": track,
            "con": track == "consulting",
            "tab": tab,
            "tabs": TABS,
            "cards": cards,
            "open_n": len(open_cards),
            "empty": not cards,
            "oob": oob,
            "ov": ov,
            "weights": weights,
            "route": await _negotiate_route(s, settings),
        }
        if len(open_cards) >= 2:
            m = ov.build_matrix(open_cards, track, weights)
            ctx["matrix"] = m
            ctx["summary"] = [{"name": x["name"], "text": ov.ahead_text(x["leads"])}
                              for x in m.summary]
        if tab == "history":
            past = await _past_offers(s, track)
            ctx["history"] = await _history_rows(s, track, fmt)
            ctx["past_n"] = len(past)
            ctx["min_obs"] = MIN_OBSERVATIONS
        if not cards:
            ctx["groups"] = await _market_groups(s, track)
            floor, target = _floor_target(track)
            ctx["floor"], ctx["target"] = ov.money_k(floor), ov.money_k(target)
        if tab == "negotiate" and open_cards:
            want = str(q.get("pursuit") or "")
            chosen = next((c for c in open_cards if str(c["pursuit_id"]) == want), open_cards[0])
            has_competing = bool(chosen["competing"])
            sits = [
                {"key": k, "name": n, "hint": h,
                 "disabled": k == "competing_offer" and not has_competing}
                for k, n, h in ov.SITUATIONS[track]
            ]
            picked = str(q.get("situation") or "")
            if picked not in {x["key"] for x in sits if not x["disabled"]}:
                picked = next(x["key"] for x in sits if not x["disabled"])
            ctx.update(
                open_cards=open_cards, chosen=chosen, sits=sits, picked=picked,
                walk=str(q.get("walk_away") or ""), ask=str(q.get("ask") or ""),
            )
        return ctx


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


async def _list_response(
    request: Request, message: str | None = None, events: dict[str, Any] | None = None
) -> Any:
    """Every mutation re-renders the whole body, so what is shown always comes from what is stored
    rather than from a stale client view. The header count and the Record button ride along out
    of band."""
    ctx = await _offers_ctx(request, oob=True)
    ev = dict(events or {})
    if message:
        ev.setdefault("k-toast", _toast(message))
    return templates().TemplateResponse(
        request, "partials/offers_list.html", ctx, headers=_events(ev)
    )


@router.get("/offers", response_class=HTMLResponse)
async def page(request: Request):
    hx = bool(request.headers.get("HX-Request"))
    ctx = await _offers_ctx(request, oob=hx)
    tmpl = "partials/offers_list.html" if hx else "offers.html"
    return templates().TemplateResponse(request, tmpl, ctx)


# ----------------------------------------------------------------------------- the sheet
async def _sheet(
    request: Request, track: str, row: Any | None = None, pursuit_id: int | None = None
):
    async with sessionmaker()() as s:
        conf_on, emp_map = await confidential_once(s)
        pickable = await _pickable_pursuits(s, track)
        for p in pickable:
            p["org_name"] = redact_name(p["org_name"], emp_map, enabled=conf_on)
            p["title"] = redact_text(p["title"], emp_map, enabled=conf_on)
        label = None
        if row is not None:
            pursuit = await s.get(Pursuit, row.pursuit_id)
            opp, org_name = await _names(s, pursuit) if pursuit else (None, None)
            label = (
                f"{redact_name(org_name, emp_map, enabled=conf_on) or 'Unknown org'} · "
                f"{redact_text(opp.title, emp_map, enabled=conf_on) if opp else 'Untitled'}"
            )

    def day(raw: str | None) -> str:
        d = parse_day(raw)
        return d.isoformat() if d else ""

    ctx = {
        "track": track, "con": track == "consulting", "row": row, "pursuits": pickable,
        "pursuit_id": pursuit_id, "label": label, "day": day,
        "statuses": _OPEN_STATUSES if row is None or row.status in _OPEN_STATUSES
        else (*_OPEN_STATUSES, row.status),
    }
    return templates().TemplateResponse(request, "partials/offer_sheet.html", ctx)


@router.get("/offers/new", response_class=HTMLResponse)
async def new_sheet(request: Request):
    pid = request.query_params.get("pursuit")
    return await _sheet(
        request, active_track(request), pursuit_id=int(pid) if pid and pid.isdigit() else None
    )


@router.get("/offers/{track}/{offer_id}/edit", response_class=HTMLResponse)
async def edit_sheet(request: Request, track: str, offer_id: int):
    model = _model_for_track(track)
    if model is None:
        return HTMLResponse("Unknown track.", status_code=404)
    async with sessionmaker()() as s:
        row = await s.get(model, offer_id)
    if row is None:
        return HTMLResponse("That offer no longer exists.", status_code=404)
    return await _sheet(request, track, row=row)


# ------------------------------------------------------------------------------ the values
def _opt_float(form: Any, name: str) -> float | None:
    """A blank or absent numeric field is None (not recorded), never 0. A 0 base or a 0 bonus
    target is a real figure the operator typed and must survive as 0."""
    raw = form.get(name)
    if raw is None:
        return None
    text = str(raw).strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _opt_int(form: Any, name: str) -> int | None:
    v = _opt_float(form, name)
    return int(v) if v is not None else None


def _opt_str(form: Any, name: str) -> str | None:
    raw = form.get(name)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _values(form: Any, track: str, *, only_present: bool) -> dict[str, Any]:
    """The offer's own fields from a form. On edit only the fields the form carries change, so a
    field the sheet does not show (notes, benefits) is never wiped."""
    read = {"f": _opt_float, "i": _opt_int, "s": _opt_str}
    return {
        name: read[kind](form, name)
        for name, kind in _FIELDS[track]
        if not only_present or name in form
    }


def _parse_qual(form: Any) -> dict[str, float] | None:
    """The five qualitative ratings from a form, or None when the operator left them all blank.
    A blank is 'not rated' and stays out of the comparison, so an unrated row is not scored as
    zero."""
    out: dict[str, float] = {}
    for axis in QUAL_AXES:
        v = _opt_float(form, f"qual_{axis}")
        if v is None:
            continue
        out[axis] = max(0.0, min(_QUAL_MAX, v))
    return out or None


def _form_error(message: str) -> HTMLResponse:
    """A problem with the sheet, shown inside it (the sheet stays open and keeps what was typed)."""
    return HTMLResponse(
        f'<span class="k-pill k-pill--warn" role="alert">{escape(message)}</span>',
        headers={"HX-Retarget": "#of-form-error", "HX-Reswap": "innerHTML"},
    )


@router.post("/offers", response_class=HTMLResponse)
async def record_offer(request: Request):
    """Record an offer against a pursuit. This is the only place an Offer/EngagementOffer row is
    ever created; everything else on this page (benchmark, comparison, competing-offer
    detection, negotiation scripts) reads what is recorded here.

    Nothing is sent anywhere and nothing is submitted on the operator's behalf: this records what
    the operator was told, so Kestrel can value it and draft scripts they deliver themselves."""
    form = await request.form()
    pursuit_id = _opt_int(form, "pursuit_id")
    if pursuit_id is None:
        return _form_error("Choose which application this offer is for.")

    async with sessionmaker()() as s:
        pursuit = await s.get(Pursuit, pursuit_id)
        if pursuit is None:
            return _form_error("That application no longer exists.")
        # D-RR1-2: the PURSUIT's track is authoritative, never the form's. A form's track select
        # could default to employment regardless of the pursuit, so a consulting pursuit's offer
        # was filed in the employment table -- which `benchmark_offer` and `_latest_offer`, both of
        # which pick the table from `pursuit.track`, never read. The offer was invisible.
        track = pursuit.track or "employment"
        if track not in ("employment", "consulting"):
            return _form_error("That application has an unknown track.")
        status = str(form.get("status") or "verbal")
        if status not in _OFFER_STATUSES:
            return _form_error("Unknown offer status.")
        received = _opt_str(form, "received_at") or utcnow_iso()
        model = _model_for_track(track)
        row = model(
            pursuit_id=pursuit_id, status=status, qual_json=_parse_qual(form), received_at=received,
            **_values(form, track, only_present=False),
        )
        s.add(row)
        # A recorded offer means the pursuit is being negotiated: move it there so the pursuit
        # board and the next-action sweep agree with what is recorded here. Through
        # `move_to_status`, THE one sanctioned way a card changes (review): setting
        # `pursuit.status` directly wrote no `status_event`, so the transition had no history.
        #
        # Every live stage an offer actually arrives at moves, not just the first four: an offer
        # most often lands when the pursuit is already `applied`, `screening`, `interviewing` or
        # `final`, and leaving the card behind meant the board disagreed with /offers at exactly
        # the moment it mattered (review). Closed stages are left alone, so recording an
        # offer on a rejected pursuit does not quietly reopen it.
        target = "negotiating" if track == "consulting" else "offer"
        if pursuit.status in _MOVABLE_TO_OFFER.get(target, ()):
            await move_to_status(s, pursuit, target, note="Offer recorded")
        await s.commit()

    return await _list_response(
        request, "Offer saved and added to the comparison.", {"of-saved": True}
    )


@router.post("/offers/{track}/{offer_id}/edit", response_class=HTMLResponse)
async def edit_offer(request: Request, track: str, offer_id: int):
    """Save changes to one offer. Only the fields the sheet carries change."""
    model = _model_for_track(track)
    if model is None:
        return _form_error("Unknown track.")
    form = await request.form()
    status = str(form.get("status") or "")
    if status and status not in _OFFER_STATUSES:
        return _form_error("Unknown offer status.")
    async with sessionmaker()() as s:
        row = await s.get(model, offer_id)
        if row is None:
            return _form_error("That offer no longer exists.")
        for name, value in _values(form, track, only_present=True).items():
            setattr(row, name, value)
        if status:
            row.status = status
        received = _opt_str(form, "received_at")
        if received:
            row.received_at = received
        await s.commit()
    return await _list_response(request, "Offer updated.", {"of-saved": True})


# ------------------------------------------------------------------------------- ratings
@router.post("/offers/{track}/{offer_id}/rate", response_class=HTMLResponse)
async def rate(request: Request, track: str, offer_id: int):
    """One star rating on one judgment row, from the comparison. A rating is the operator's own;
    pressing the star already set clears it. Only the rows this track has can be rated."""
    model = _model_for_track(track)
    form = await request.form()
    axis = str(form.get("axis") or "")
    if model is None or axis not in ov.RATING_KEYS.get(track, ()):
        return Response(status_code=204)
    value = _opt_float(form, "value")
    async with sessionmaker()() as s:
        row = await s.get(model, offer_id)
        if row is None:
            return Response(status_code=204)
        qual = dict(row.qual_json or {})
        if value is None or value <= 0 or qual.get(axis) == value:
            qual.pop(axis, None)
        else:
            qual[axis] = max(0.0, min(_QUAL_MAX, value))
        row.qual_json = qual or None
        await s.commit()
    ctx = await _offers_ctx(request)
    return templates().TemplateResponse(request, "partials/offer_matrix.html", ctx)


@router.post("/offers/{offer_id}/qual", response_class=HTMLResponse)
async def save_qual(request: Request, offer_id: int):
    """Save the operator's 0-5 ratings, all at once. These are judgments the tool must not make
    for them (compare.OfferComp.qual).

    The posted `track` selects the table, and the lookup resolves inside that ONE table.
    `Offer.id` and `EngagementOffer.id` are independent sequences, so both tables hand out id 1 to
    their first row: resolving a bare id against `Offer` first meant rating a consulting offer
    silently overwrote an unrelated employment offer's ratings (review). With no `track`
    posted, an id that exists in BOTH tables is refused rather than guessed.
    """
    form = await request.form()
    qual = _parse_qual(form)
    track = str(form.get("track") or "").strip()
    async with sessionmaker()() as s:
        models: list = []
        model = _model_for_track(track)
        if model is not None:
            models = [model]
        elif not track:
            models = [Offer, EngagementOffer]
        else:
            return HTMLResponse(
                f'<div class="warnbox" role="status">Unknown track: {escape(track)}</div>'
            )
        row = None
        for candidate in models:
            found = await s.get(candidate, offer_id)
            if found is not None:
                if row is None:
                    row = found
                else:
                    # Ambiguous without a track: refuse rather than write to a coin toss.
                    return HTMLResponse(
                        '<div class="warnbox" role="status">That id matches both an employment '
                        "and a consulting offer. Re-save from the offer's own card.</div>"
                    )
        if row is None:
            return HTMLResponse(
                '<div class="warnbox" role="status">That offer no longer exists.</div>'
            )
        row.qual_json = qual
        await s.commit()
    return await _list_response(request, "Ratings saved.")


@router.post("/offers/weights", response_class=HTMLResponse)
async def save_weights(request: Request):
    """The operator's own comparison weights, 0 (Ignore) to 5 (Most). Only the rows posted are
    stored, so a row never moved stays at Matters. Answers with the "Ahead on" summary alone: the
    sliders already show the new words, and nothing else on the page changes."""
    form = await request.form()
    raw = {
        k: max(0.0, min(5.0, v)) for k in ov.WEIGHT_KEYS if (v := _opt_float(form, k)) is not None
    }
    async with sessionmaker()() as s:
        row = await s.get(Setting, _WEIGHTS_KEY)
        if row is None:
            s.add(Setting(key=_WEIGHTS_KEY, value=raw))
        else:
            row.value = raw
        await s.commit()
    ctx = await _offers_ctx(request)
    return templates().TemplateResponse(request, "partials/offer_summary.html", ctx)


# ------------------------------------------------------------------------------ outcomes
@router.post("/offers/{track}/{offer_id}/outcome", response_class=HTMLResponse)
async def outcome(request: Request, track: str, offer_id: int):
    """I accepted, I declined, It expired: the offer takes that status and the pursuit closes with
    that outcome. The toast's Undo posts to Reopen with where the offer and the pursuit were."""
    form = await request.form()
    which = str(form.get("outcome") or "")
    async with sessionmaker()() as s:
        done = await offer_outcome.close_offer(s, track, offer_id, which)
        if isinstance(done, str):
            await s.rollback()
            return await _list_response(request, None, {"k-toast": _toast(done, tone="warn")})
        await s.commit()
    undo = f"/offers/{track}/{offer_id}/reopen?token={done.token}"
    note = (
        f"Marked {ov.OUTCOME_WORDS[which].lower()}. "
        "The application moves to Closed with this outcome."
    )
    return await _list_response(request, None, {"k-toast": _toast(note, undo_url=undo)})


@router.post("/offers/{track}/{offer_id}/reopen", response_class=HTMLResponse)
async def reopen(request: Request, track: str, offer_id: int):
    async with sessionmaker()() as s:
        if "token" in request.query_params:
            why = await offer_outcome.undo_close(s, track, offer_id, request.query_params["token"])
        else:
            why = await offer_outcome.reopen_offer(
                s,
                track,
                offer_id,
                offer_status=request.query_params.get("offer_status"),
                pursuit_status=request.query_params.get("pursuit_status"),
            )
        if why:
            await s.rollback()
        else:
            await s.commit()
    if not request.headers.get("HX-Request"):  # Undo from the toast: the page asks for itself
        if why:  # refused: the toast reads the marker and says so (kestrel-ui.js)
            return HTMLResponse(
                f'<span data-undo-msg data-ok="0">{escape(why)}</span>',
                headers=_events({"of-reload": True}),
            )
        return Response(status_code=204, headers=_events({"of-reload": True}))
    return await _list_response(request, why or "Reopened.")


# ------------------------------------------------------------------------------- the draft
def _redact_scripts(result: ScriptResult, mapping: EmployerMap, *, enabled: bool) -> ScriptResult:
    """Every model-written string the draft renders, with the current employer shown as the
    operator's "Show it as" text. The walk-away is the operator's own figure, left unchanged."""
    if not enabled or not mapping:
        return result
    return replace(
        result,
        situation_read=redact_text(result.situation_read, mapping, enabled=True) or "",
        scripts=redact_json(result.scripts, mapping, enabled=True),
        leverage_inventory=redact_json(result.leverage_inventory, mapping, enabled=True),
        risks=redact_json(result.risks, mapping, enabled=True),
        unsupported_scenarios=redact_json(result.unsupported_scenarios, mapping, enabled=True),
    )


def _draft_panel(request: Request, **ctx: Any) -> Any:
    return templates().TemplateResponse(request, "partials/offer_scripts.html", ctx)


@router.post("/offers/{pursuit_id}/scripts", response_class=HTMLResponse)
async def scripts(request: Request, pursuit_id: int):
    """Draft the reply for one pursuit's offer, for one situation. `walk_away` is REQUIRED and
    supplied by the operator as plain text; there is no code path here that computes or defaults
    it, and draft_scripts echoes it back unchanged. An ask below it is refused. A model that is
    not running degrades to a message, never a 500."""
    form = await request.form()
    walk_away = str(form.get("walk_away") or "").strip()
    ask = _opt_str(form, "ask")
    if not walk_away:
        return _draft_panel(
            request, error="A walk-away number is required before drafting scripts.", result=None
        )
    problem = ov.ask_problem(ask, walk_away)
    if problem:
        return _draft_panel(request, error=problem, result=None)
    situation = str(form.get("situation") or "").strip()
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        route = await _negotiate_route(s, settings)
    if route["online"]:
        # An online model writes this draft: it needs the operator's confirmation of THIS send,
        # for the configured model, checked here and not trusted from the page.
        confirmed = str(form.get("confirmed") or "") == "1"
        from kestrel.services.online_consent import confirmation_matches

        if not confirmed or not confirmation_matches(route, form):
            return _draft_panel(
                request,
                error="Nothing was sent. Confirm the online draft first: tick the box that "
                      "says what will be sent.",
                result=None,
            )
        if route["employer"] == "held":
            return _draft_panel(
                request,
                error="Nothing was sent. Confidential mode is on and no current employer is "
                      "chosen, so Kestrel will not send this. Choose your employer in Settings.",
                result=None,
            )
    try:
        async with httpx.AsyncClient() as http:
            async with sessionmaker()() as s:
                pursuit = await s.get(Pursuit, pursuit_id)
                track = (pursuit.track if pursuit else None) or "employment"
                names = {k: n for k, n, _ in ov.SITUATIONS.get(track, ())}
                chosen = situation if situation in names else None
                kwargs: dict[str, Any] = {}
                if chosen:
                    kwargs["scenarios"] = [chosen]
                if ask:
                    kwargs["ask"] = ask
                client = llm_client.LLMClient(settings, http=http)
                result = await draft_scripts(s, client, pursuit_id, walk_away=walk_away, **kwargs)
                await s.commit()  # persists a quarantine row if the model call failed validation
                conf_on, emp_map = await confidential_once(s)
                if result is not None:
                    # Confidential mode: model prose names the current employer like anything
                    # else on this page does, so it is redacted the same way.
                    result = _redact_scripts(result, emp_map, enabled=conf_on)
                points: list[tuple[str, str]] = []
                profile = load_profile(track) if pursuit else None
                for p in getattr(profile, "proof_points", None) or []:
                    claim = redact_text(_pp(p, "claim") or "", emp_map, enabled=conf_on)
                    evid = redact_text(_pp(p, "evidence") or "", emp_map, enabled=conf_on)
                    if claim:
                        points.append((claim, evid or ""))
        if result is None:
            return _draft_panel(
                request,
                error=(
                    "No pursuit found, or the model could not produce a valid response "
                    "(quarantined for review)."
                ),
                result=None,
            )
        # The draft is the script for the situation asked for; a model that answered with another
        # one still shows what it wrote, under its own name.
        picked = next((x for x in result.scripts if x.get("scenario") == chosen), None) \
            or (result.scripts[0] if result.scripts else None)
        if picked is None:
            return _draft_panel(
                request,
                error="Kestrel will not draft that one: it needs a real second offer on record.",
                result=None,
            )
        paras = [ov.highlight(t, points) for t in ov.paragraphs(picked.get("script"))]
        return _draft_panel(
            request, error=None, result=result, script=picked, paras=paras,
            situation_name=names.get(picked.get("scenario"), "")
            or str(picked.get("scenario", "")).replace("_", " ").capitalize(),
        )
    except Exception as exc:  # a model that is not running must not 500 the page
        log.warning("offer_scripts_failed", pursuit_id=pursuit_id, error_type=type(exc).__name__,
                    error=str(exc))
        return _draft_panel(
            request, error=f"Scripts need the local model running: {plain_error(exc)}", result=None
        )
