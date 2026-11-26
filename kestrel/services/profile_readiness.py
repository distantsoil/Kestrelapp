"""What the profile still needs before scoring can be trusted, as one next step (issues #67, #68,
D-PROF-1, D-PROF-2). It only warns: scoring is never held. The Today and Profile notices read it,
so both name the same single step and never a list.

`assess` is pure. `load_readiness` gathers its inputs from profile.yml and the database.

"Scored before your proof points" has two meanings, chosen by what is known:
- `proof_points_changed_at` is set (an import added proof points): the roles whose latest Tier 2
  score is older than that moment.
- It is not set (the points were typed by hand, or there are none): the roles scored while the
  profile had no proof points at all. A profile that has proof points and no stamp counts zero,
  because nothing says when they arrived.

A role whose Score again already ran since that moment (its task finished or gave up) is not
counted again, even when the run wrote no new score: a posting with no text is skipped and a failed
attempt keeps the old score, and both are shown where they always are (issue #253). Otherwise the
same roles were queued on every press and the notice never went away. Dismiss hides the Score
again notice for the track until the proof points change again (`DISMISSED_KEY` holds the stamp
that was dismissed)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import DocMaster, Evaluation, Opportunity, Org, Setting, Task
from kestrel.services.hidden_orgs import not_hidden_org_clause
from kestrel.web.fit import EMPTY_POSTING_STAGE_REASON

log = structlog.get_logger()

KEY = "proof_points_changed_at"
DISMISSED_KEY = "proof_points_notice_dismissed"
# {track: {"stamp": the proof-points stamp, "ids": roles the notice's Score again queued for it}}.
# Kept in a setting, not read from task rows: prune deletes finished tasks (review of #253).
TRIED_KEY = "proof_points_rescored"
DOCS_HREF = "/profile?tab=documents"
UNREADABLE_LINE = ("Your profile file could not be read, so Kestrel is scoring without it. "
                   "Open Profile to fix it.")

PROFILE_UNREADABLE = "profile_unreadable"
IMPORT_RESUME = "import_resume"
REVIEW_IMPORT = "review_import"
ADD_TITLES = "add_titles"
SCORE_AGAIN = "score_again"
READY = "ready"
# The stages a Tier 2 score again is accepted from and that hold a finished score.
RESCORABLE_STAGES = ("t2_done", "t3_done")


@dataclass
class Readiness:
    has_resume: bool
    has_proof_points: bool
    has_narrative: bool
    has_target_titles: bool
    roles_scored_before_proof_points: int
    next_step: str
    line: str
    link_label: str = ""
    link_href: str = ""
    role_ids: list[int] = field(default_factory=list)
    # The proof-points stamp this readiness was read for; Dismiss posts it back (review of #253).
    stamp: str = ""

    @property
    def show_notice(self) -> bool:
        return self.next_step != READY


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _roles(n: int) -> str:
    return "1 role was" if n == 1 else f"{n} roles were"


def assess(
    profile: Any, *, track: str = "employment", has_resume: bool,
    scored: list[tuple[int, str]], changed_at: str | None, unreadable: bool = False,
    dismissed: bool = False,
) -> Readiness:
    """`scored` is (role id, when its latest Tier 2 score was made) for the track's scored roles.
    `dismissed`: the person dismissed the Score again notice for this `changed_at`."""
    proofs = list(_get(profile, "proof_points") or [])
    has_proof = bool(proofs)
    has_narrative = bool(str(_get(profile, "narrative") or "").strip())
    terms = _get(profile, "consulting_terms" if track == "consulting" else "employment_terms")
    titles = _get(terms, "service_lines" if track == "consulting" else "target_titles") or []
    has_titles = any(str(t).strip() for t in titles)

    if changed_at:
        stale = [oid for oid, at in scored if at < changed_at]
    elif not has_proof:
        stale = [oid for oid, _at in scored]
    else:
        stale = []

    thin = not has_proof and not has_narrative
    link_label = link_href = ""
    if unreadable:
        step = PROFILE_UNREADABLE
        line = UNREADABLE_LINE
        link_label, link_href = "Open Profile", "/profile"
    elif thin and not has_resume:
        step = IMPORT_RESUME
        line = "Add your resume so Kestrel can score roles against your real results."
        link_label, link_href = "Add your resume", DOCS_HREF
    elif thin:
        step = REVIEW_IMPORT
        line = "Your resume is in, but no proof points came from it. Add them before you score."
        link_label, link_href = "Review your resume", DOCS_HREF
    elif not has_titles:
        step = ADD_TITLES
        what = "services you offer" if track == "consulting" else "job titles you want"
        line = f"Add the {what} so Kestrel knows what to look for."
        link_label = "Add them"
        link_href = f"/profile?tab={track}&track={track}"
    elif stale and has_proof and not dismissed:
        step = SCORE_AGAIN
        line = f"{_roles(len(stale)).capitalize()} scored before your proof points."
        link_label = "Score again"
    else:
        step, line = READY, ""
    return Readiness(
        has_resume=has_resume, has_proof_points=has_proof, has_narrative=has_narrative,
        has_target_titles=has_titles, roles_scored_before_proof_points=len(stale),
        next_step=step, line=line, link_label=link_label, link_href=link_href,
        role_ids=stale if step == SCORE_AGAIN else [], stamp=changed_at or "",
    )


async def changed_at(session: AsyncSession, track: str) -> str | None:
    row = await session.get(Setting, KEY)
    value = row.value if row is not None else None
    stamp = value.get(track) if isinstance(value, dict) else None
    return stamp if isinstance(stamp, str) and stamp else None


async def dismissed_at(session: AsyncSession, track: str) -> str | None:
    """The proof-points stamp whose Score again notice the person dismissed on `track`."""
    row = await session.get(Setting, DISMISSED_KEY)
    value = row.value if row is not None else None
    stamp = value.get(track) if isinstance(value, dict) else None
    return stamp if isinstance(stamp, str) and stamp else None


async def dismiss_score_again(session: AsyncSession, track: str, shown: str | None) -> bool:
    """Hide the Score again notice on `track` until the proof points change again (the caller
    commits). `shown` is the stamp the pressed notice was shown for: a press on a notice older
    than the current stamp, or with none, stores nothing (review of #253). False when nothing
    was stored."""
    stamp = await changed_at(session, track)
    if stamp is None or not shown or shown != stamp:
        return False
    row = await session.get(Setting, DISMISSED_KEY)
    value = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    value[track] = stamp
    if row is None:
        session.add(Setting(key=DISMISSED_KEY, value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()
    return True


async def tried_ids(session: AsyncSession, track: str, stamp: str) -> set[int]:
    """The roles the notice's Score again already queued for `stamp` on `track`. The stored value
    is read through our own shape (D-SEC-20): anything else (a restored backup, a hand edit)
    reads as nothing tried, is logged, and never raises (review round 3)."""
    row = await session.get(Setting, TRIED_KEY)
    value = row.value if row is not None else None
    entry = value.get(track) if isinstance(value, dict) else None
    if row is not None and not (isinstance(value, dict) and (entry is None or isinstance(
            entry, dict) and isinstance(entry.get("ids"), list))):
        log.warning("proof_points_rescored_malformed", track=track)
    if not isinstance(entry, dict) or entry.get("stamp") != stamp:
        return set()
    ids = entry.get("ids")
    if not isinstance(ids, list):
        return set()
    return {i for i in ids if isinstance(i, int) and not isinstance(i, bool) and i > 0}


async def record_tried(session: AsyncSession, track: str, stamp: str, ids: list[int]) -> None:
    """Remember that the notice's Score again queued `ids` for `stamp` (the caller commits), so
    they are not offered again for it even after prune removes their finished tasks. A newer
    stamp replaces the record."""
    row = await session.get(Setting, TRIED_KEY)
    value = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    old = await tried_ids(session, track, stamp)
    value[track] = {"stamp": stamp, "ids": sorted(old | {int(i) for i in ids})}
    if row is None:
        session.add(Setting(key=TRIED_KEY, value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()


async def mark_proof_points_changed(session: AsyncSession, track: str) -> None:
    """Record that proof points were just added to `track` (the caller commits)."""
    row = await session.get(Setting, KEY)
    value = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    value[track] = utcnow_iso()
    if row is None:
        session.add(Setting(key=KEY, value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()


def operator_profile(track: str) -> dict | None:
    """The track's raw block from the operator's own profile.yml, or None when there is no such
    file, it cannot be read, or it has no block for the track. Reads through
    `kestrel.pipeline.profile` (the one loader); never the fictional profile.example.yml, so a
    fresh install reads as empty, not as ready."""
    from kestrel.pipeline.profile import operator_block

    return operator_block(track)


def operator_profile_unreadable() -> bool:
    from kestrel.pipeline.profile import profile_unreadable

    return profile_unreadable()


def operator_profile_is_thin(track: str) -> bool:
    """Thin per the operator's own profile.yml; true when there is none (issue #64)."""
    from kestrel.pipeline.context import profile_is_thin

    return profile_is_thin(operator_profile(track))


async def load_readiness(session: AsyncSession, track: str) -> Readiness:
    profile = await asyncio.to_thread(operator_profile, track)
    unreadable = await asyncio.to_thread(operator_profile_unreadable)
    try:
        return await _gather(session, track, profile, unreadable)
    except SQLAlchemyError as exc:  # a page never fails because the database could not be read
        log.warning("profile_readiness_unreadable", error_type=type(exc).__name__)
        return assess(profile, track=track, has_resume=False, scored=[], changed_at=None,
                      unreadable=unreadable)


async def _gather(
    session: AsyncSession, track: str, profile: Any, unreadable: bool = False,
) -> Readiness:
    has_resume = bool((await session.execute(
        select(func.count()).select_from(DocMaster)
        .where(DocMaster.track == track, DocMaster.doc_kind == "resume")
    )).scalar_one())
    # Only roles a new Tier 2 score can reach: active, shown, scored, and not the empty-posting
    # stub. Rejected, hidden, applied and quarantined roles never get one, so counting them
    # would leave the notice up for good and re-queue the same failing roles on every press.
    rows = (await session.execute(
        select(Evaluation.opportunity_id, func.max(Evaluation.created_at))
        .join(Opportunity, Opportunity.id == Evaluation.opportunity_id)
        .outerjoin(Org, Org.id == Opportunity.org_id)
        .where(
            Evaluation.tier == 2, Opportunity.track == track,
            Opportunity.archived_at.is_(None), not_hidden_org_clause(),
            Opportunity.stage.in_(RESCORABLE_STAGES),
            or_(Opportunity.stage_reason.is_(None),
                Opportunity.stage_reason != EMPTY_POSTING_STAGE_REASON),
        )
        .group_by(Evaluation.opportunity_id)
        .order_by(Evaluation.opportunity_id)
    )).all()
    # A role already waiting for a scoring task is on its way, so it is not offered again.
    waiting = {
        int(v) for v in (await session.execute(
            select(func.json_extract(Task.payload, "$.opportunity_id"))
            .where(Task.type.in_(("t1", "t2")), Task.status.in_(("pending", "leased")))
        )).scalars() if v is not None
    }
    stamp = await changed_at(session, track)
    if stamp:
        # A role whose Score again already ran since the proof points changed had its chance; a
        # run that wrote no new score (skipped or failed) is not queued again (issue #253). Only
        # a Score again counts: an automatic task can skip without reading the new points.
        waiting |= {
            int(v) for v in (await session.execute(
                select(Task.opportunity_id)
                .where(Task.type.in_(("t1", "t2")), Task.status.in_(("done", "dead")),
                       Task.created_at >= stamp,
                       func.json_extract(Task.payload, "$.rescore").is_(True))
            )).scalars() if v is not None
        }
        # The same fact once prune has removed those tasks: the notice's own presses.
        waiting |= await tried_ids(session, track, stamp)
    return assess(
        profile, track=track, has_resume=has_resume,
        scored=[(int(oid), str(at)) for oid, at in rows if int(oid) not in waiting],
        changed_at=stamp, unreadable=unreadable,
        dismissed=stamp is not None and await dismissed_at(session, track) == stamp,
    )
