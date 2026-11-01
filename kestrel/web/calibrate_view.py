"""What the /calibrate screen shows (D-UI-99): plain words, the session queue, the three strips,
the weight tune-up and the learning history. Read-only: every write is in routes_learning.py.

Hard rule 6 lives here. A count of ratings is always shown with its n; a comparison drawn from
fewer than FLOOR ratings is labeled "not enough yet" instead of being rendered as if it were solid.
Nothing here calls a model."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Settings
from kestrel.db.models import Calibration, Evaluation, Label, MlModel, Opportunity, Org
from kestrel.learning import calibration as calib
from kestrel.learning import reranker as reranker_mod
from kestrel.learning.active import UncertainItem, rank_uncertain
from kestrel.learning.capture import labeled_examples

log = structlog.get_logger()

FLOOR = 5  # hard rule 6: fewer than five observations is "not enough yet"
SESSION_SIZE = 10
HISTORY_PER_PAGE = 8
CLOSE_WITHIN = 0.5  # "Close" when Kestrel's score is within half a point of the operator's

SCORE_WORDS = {1: "Poor", 2: "Weak", 3: "Maybe", 4: "Good", 5: "Great"}

# The five scores, in plain words, keyed like Evaluation.dimensions (learning.calibration._DIMS).
DIM_WORDS: dict[str, dict[str, str]] = {
    "employment": {
        "role_fit": "Role fit",
        "domain_match": "Domain match",
        "company_context": "Company context",
        "comp_logistics": "Pay and logistics",
        "trajectory": "Career direction",
    },
    "consulting": {
        "engagement_fit": "Engagement fit",
        "buyer_access": "Buyer access",
        "economics": "Economics",
        "delivery_feasibility": "Can you deliver it",
        "strategic_value": "Strategic value",
    },
}
REASONS = {
    "employment": ["Pay", "Location", "Level", "Kind of work", "Company", "Other"],
    "consulting": ["Rate", "Scope", "Buyer", "Timing", "Delivery risk", "Other"],
}


def word_for(score: float | None) -> str:
    """The word for a stored score (older labels may hold 4.1; it reads as the nearest word)."""
    if score is None:
        return "Not rated"
    return SCORE_WORDS[max(1, min(5, round(score)))]


def is_close(kestrel: float | None, said: float | None) -> bool:
    return kestrel is not None and said is not None and abs(kestrel - said) < CLOSE_WITHIN


def dim_scores(ev: Evaluation | None, track: str) -> list[dict[str, Any]]:
    """[{key, name, score, why}] in the plain-word order. A dimension the model left out is
    skipped, never invented."""
    out: list[dict[str, Any]] = []
    raw = ev.dimensions if ev is not None and isinstance(ev.dimensions, dict) else {}
    for key, name in DIM_WORDS.get(track, DIM_WORDS["employment"]).items():
        d = raw.get(key)
        score = d.get("score") if isinstance(d, dict) else d
        if isinstance(score, (int, float)):
            why = d.get("why") if isinstance(d, dict) else None
            out.append(
                {
                    "key": key,
                    "name": name,
                    "score": max(1, min(5, round(score))),
                    "why": str(why) if why else "",
                }
            )
    return out


async def org_name(s: AsyncSession, opp: Opportunity) -> str:
    """The employer's name: the stored text, else the org row (a lazy load is not allowed here)."""
    if opp.org_name:
        return opp.org_name
    org = await s.get(Org, opp.org_id) if opp.org_id else None
    return org.name if org is not None else ""


def pay_line(opp: Opportunity) -> str:
    if opp.comp_min is None and opp.comp_max is None:
        return "Pay not listed"
    sym = "$" if (opp.comp_currency or "USD").upper() == "USD" else f"{opp.comp_currency} "
    lo, hi = opp.comp_min, opp.comp_max
    per = {"hourly": " an hour", "daily": " a day", "weekly": " a week", "monthly": " a month"}.get(
        (opp.comp_interval or "").lower(), ""
    )
    est = " est." if opp.comp_source == "inferred" else ""  # a job-board estimate says so
    if lo and hi and round(lo) != round(hi):
        return f"{sym}{lo:,.0f} to {sym}{hi:,.0f}{per}{est}"
    return f"{sym}{(hi or lo):,.0f}{per}{est}"


def place_line(opp: Opportunity) -> str:
    where = opp.location_raw or ", ".join(p for p in (opp.city, opp.state) if p) or ""
    kind = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}.get(opp.remote_type, "")
    return " · ".join(p for p in (where, kind) if p) or "Place not listed"


def why_asked(u: UncertainItem) -> str:
    """Why this role is in the queue, in one plain sentence (no numbers; the page is for a person,
    not an audit). The largest weighted part of the uncertainty wins; novelty counts only when it
    is real (0.5 is the neutral value used when there is nothing rated to compare with)."""
    c = u.components
    parts = {
        "near": 0.40 * c.get("proximity", 0.0),
        "split": 0.25 * c.get("divergence", 0.0),
        "new": 0.20 * c.get("novelty", 0.0) if c.get("novelty", 0.5) > 0.55 else 0.0,
        "recent": 0.15 * c.get("recency", 0.0),
    }
    top = max(parts, key=lambda k: parts[k])
    return {
        "near": "it sits right at the line between keep and skip.",
        "split": "the overall score and the five scores disagree.",
        "new": "it is unlike anything you have rated so far.",
        "recent": "it was posted recently.",
    }[top]


def kestrel_read(ev: Evaluation | None, dims: list[dict[str, Any]]) -> str:
    """Kestrel's own one-line reading: the model's summary when it wrote one, else the reason
    given for its lowest score. Empty when neither exists (the card then shows only the scores)."""
    from kestrel.analytics.today import summary_line

    line = summary_line(ev.blocks if ev is not None else None)
    if line:
        return line
    weakest = min((d for d in dims if d["why"]), key=lambda d: d["score"], default=None)
    return weakest["why"] if weakest else ""


@dataclass
class QueueView:
    current: UncertainItem | None
    done: int  # rated in this session
    total: int  # this session's size (shrinks when fewer roles are left)
    skipped: list[int]
    more_unsure: bool  # roles Kestrel is still unsure about after this session's ten


def _ids(raw: str | None) -> list[int]:
    out: list[int] = []
    for part in (raw or "").split(","):
        if re.fullmatch(r"[0-9]+", part.strip()) and int(part) not in out:
            out.append(int(part))
    return out


async def build_queue(
    s: AsyncSession, track: str, settings: Settings, *, since: str, skip: str | None
) -> QueueView:
    """The session: ten roles, one at a time. Skipped roles move to the end of the queue."""
    done = (
        await s.execute(
            select(func.count(func.distinct(Label.opportunity_id))).where(
                Label.track == track, Label.source == "active_learning", Label.created_at >= since
            )
        )
    ).scalar_one()
    pool = await rank_uncertain(s, track, settings, limit=SESSION_SIZE + 1)
    left = pool[: max(0, SESSION_SIZE - done)]
    skipped = [i for i in _ids(skip) if any(u.opportunity.id == i for u in left)]
    ordered = [u for u in left if u.opportunity.id not in skipped] + [
        next(u for u in left if u.opportunity.id == i) for i in skipped
    ]
    return QueueView(
        current=ordered[0] if ordered else None,
        done=done,
        total=done + len(ordered),
        skipped=skipped,
        more_unsure=len(pool) > len(left) or (not left and bool(pool)),
    )


async def session_line(s: AsyncSession, track: str, since: str) -> str:
    """The end-of-queue summary: how close Kestrel was on this session's ratings, or that there is
    not enough yet to say (hard rule 6)."""
    rows = (
        await s.execute(
            select(Label.opportunity_id, Label.score)
            .where(
                Label.track == track, Label.source == "active_learning", Label.created_at >= since
            )
            .order_by(Label.id)
        )
    ).all()
    said = dict(rows)  # the latest rating of each role wins
    if len(said) < FLOOR:
        return f"Not enough ratings in this session to compare yet (n={len(said)})."
    evs = {
        ev.opportunity_id: ev.score
        for ev in (
            await s.execute(
                select(Evaluation)
                .where(Evaluation.opportunity_id.in_(list(said)), Evaluation.tier == 2)
                .order_by(Evaluation.id)
            )
        ).scalars()
    }
    close = sum(1 for oid, sc in said.items() if is_close(evs.get(oid), sc))
    return f"You and Kestrel were close on {close} of {len(said)}."


# ------------------------------------------------------------------------------- the strips
@dataclass
class Strips:
    n: int  # ratings counted toward the thresholds
    few_at: int
    tune_at: int
    rank_at: int
    tune_state: str  # "more" | "review" | "done" | "checking"
    tune_left: int
    taste_state: str  # "learning" | "in_use" | "not_in_use" | "untrained"
    taste_note: str
    promo: reranker_mod.PromotionStatus | None = None
    promo_error: str | None = None


async def _in_use_sentence(s: AsyncSession, track: str) -> str | None:
    """ "Gets 7 of 10 of your ratings right. Usual scoring gets 6." from the active model's own
    held-out check, or None when that check is too small to say (hard rule 6)."""
    row = (
        await s.execute(
            select(MlModel)
            .where(MlModel.track == track, MlModel.kind == "reranker", MlModel.is_active.is_(True))
            .order_by(MlModel.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    m = row.metrics if row is not None and isinstance(row.metrics, dict) else {}
    mine, usual, n = m.get("reranker_agreement_on_llm_set"), m.get("llm_agreement"), m.get("n_llm")
    if not isinstance(mine, (int, float)) or not isinstance(usual, (int, float)):
        return None
    if not isinstance(n, int) or n < FLOOR:
        return None
    return f"gets {round(mine * 10)} of 10 right vs usual {round(usual * 10)}"


async def build_strips(
    s: AsyncSession, track: str, settings: Settings, *, promo, promo_error: str | None
) -> Strips:
    lr = settings.learning
    n = len(await labeled_examples(s, track))
    tune_state, tune_left = "more", max(0, lr.min_labels_for_calibration - n)
    if n >= lr.min_labels_for_calibration:
        pending = await pending_proposal(s, track)
        decided = await _last_decided(s, track)
        if pending is not None:
            tune_state = "review"
        elif decided is not None and n - decided.n_samples < lr.min_labels_for_calibration:
            tune_state = "done"
        else:
            tune_state = "checking"
    taste_state, note = "learning", ""
    if n >= lr.min_labels_for_reranker:
        if promo is not None and promo.reranker_active:
            taste_state = "in_use"
            note = await _in_use_sentence(s, track) or ""
        elif promo is not None and promo.last_trained_at:
            taste_state = "not_in_use"
        else:
            taste_state = "untrained"
    return Strips(
        n=n,
        few_at=lr.min_labels_for_few_shot,
        tune_at=lr.min_labels_for_calibration,
        rank_at=lr.min_labels_for_reranker,
        tune_state=tune_state,
        tune_left=tune_left,
        taste_state=taste_state,
        taste_note=note,
        promo=promo,
        promo_error=promo_error,
    )


# --------------------------------------------------------------------------- the tune-up
async def pending_proposal(s: AsyncSession, track: str) -> Calibration | None:
    return next((c for c in await calib.latest_proposals(s) if c.track == track), None)


async def _last_decided(s: AsyncSession, track: str) -> Calibration | None:
    return (
        await s.execute(
            select(Calibration)
            .where(Calibration.track == track, Calibration.status.in_(("accepted", "rejected")))
            .order_by(Calibration.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def tune_proposal(s: AsyncSession, track: str, settings: Settings) -> Calibration | None:
    """The pending proposal, creating one only when there is none and a full interval of ratings
    has passed since the last decision (so a page view never piles up proposals)."""
    n = len(await labeled_examples(s, track))
    need = settings.learning.min_labels_for_calibration
    if n < need:
        return None
    pending = await pending_proposal(s, track)
    if pending is not None:
        return pending
    decided = await _last_decided(s, track)
    if decided is not None and n - decided.n_samples < need:
        return None
    return await calib.propose_calibration(s, track, settings)


@dataclass
class TuneRow:
    name: str
    now: int  # percent
    new: int


@dataclass
class TuneView:
    id: int
    n: int
    rows: list[TuneRow] = field(default_factory=list)
    miss_now: float = 0.0
    miss_new: float = 0.0
    better: bool = False
    reason: str = ""


def tune_view(cal: Calibration) -> TuneView:
    words = DIM_WORDS.get(cal.track, DIM_WORDS["employment"])
    prev, prop = cal.previous_weights or {}, cal.proposed_weights or {}
    rows = [
        TuneRow(words[k], round(prev.get(k, 0) * 100), round(prop.get(k, 0) * 100))
        for k in words
        if k in prop
    ]
    miss_now, miss_new = cal.cv_mae_previous or 0.0, cal.cv_mae_proposed or 0.0
    better = miss_new < miss_now
    up = max(rows, key=lambda r: r.new - r.now, default=None)
    reason = (
        f"{up.name} matters more to you than Kestrel assumed."
        if better and up is not None and up.new > up.now
        else ""
    )
    return TuneView(
        id=cal.id,
        n=cal.n_samples,
        rows=rows,
        miss_now=miss_now,
        miss_new=miss_new,
        better=better,
        reason=reason,
    )


# -------------------------------------------------------------------------------- history
@dataclass
class HistoryRow:
    opp_id: int
    when: str
    title: str
    org: str
    said_word: str
    kestrel: float | None
    result: str  # "Close" | "Different" | "Changed"
    changed: bool


@dataclass
class HistoryView:
    rows: list[HistoryRow]
    total: int
    close: int
    page: int
    pages: int


def _when(iso: str) -> str:
    from datetime import UTC, datetime

    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    days = (datetime.now(UTC) - t).days
    if days < 1:
        return "Today" if (datetime.now(UTC) - t).total_seconds() > 300 else "Just now"
    if days == 1:
        return "Yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    return f"{days // 30} months ago"


async def build_history(s: AsyncSession, track: str, *, page: int = 0) -> HistoryView:
    """One line per rated role, newest first: what the operator said against what Kestrel said.
    "Changed" is any role the operator has rated more than once."""
    examples = list(reversed(await labeled_examples(s, track)))
    ids = [e.opportunity_id for e in examples]
    if not ids:
        return HistoryView([], 0, 0, 0, 1)
    opps = {
        o.id: o
        for o in (await s.execute(select(Opportunity).where(Opportunity.id.in_(ids)))).scalars()
    }
    evs: dict[int, Evaluation] = {}
    for ev in (
        await s.execute(
            select(Evaluation)
            .where(Evaluation.opportunity_id.in_(ids), Evaluation.tier == 2)
            .order_by(Evaluation.id)
        )
    ).scalars():
        evs[ev.opportunity_id] = ev
    times = dict(
        (
            await s.execute(
                select(Label.opportunity_id, func.count())
                .where(Label.opportunity_id.in_(ids))
                .group_by(Label.opportunity_id)
            )
        ).all()
    )
    rows: list[HistoryRow] = []
    for e in examples:
        opp = opps.get(e.opportunity_id)
        if opp is None:
            continue
        ks = evs[opp.id].score if opp.id in evs else None
        changed = times.get(opp.id, 0) > 1
        rows.append(
            HistoryRow(
                opp_id=opp.id,
                when=_when(e.created_at),
                title=opp.title or "(untitled)",
                org=await org_name(s, opp),
                said_word=word_for(e.operator_score),
                kestrel=ks,
                changed=changed,
                result="Changed"
                if changed
                else ("Close" if is_close(ks, e.operator_score) else "Different"),
            )
        )
    close = sum(
        1
        for e in examples
        if e.opportunity_id in opps
        and is_close(
            evs[e.opportunity_id].score if e.opportunity_id in evs else None, e.operator_score
        )
    )
    pages = max(1, -(-len(rows) // HISTORY_PER_PAGE))
    page = max(0, min(page, pages - 1))
    return HistoryView(
        rows[page * HISTORY_PER_PAGE : (page + 1) * HISTORY_PER_PAGE], len(rows), close, page, pages
    )
