"""Whether a stored score reads as older than what scoring would send now (issue 179, D-SCORE-4).

The one rule every place that shows or counts "Scored with older scoring instructions" uses (the
Pipeline row's warning, the Pipeline panel, the role page's score line and its corrections strip,
the rescore offers): a score is older exactly when this role's OPERATOR_CORRECTIONS block changed
since it was scored.

- Only a Tier 2 or Tier 3 read with a prompt version can be older; a Tier 1 screen never is.
- A read under a version that is neither the current one nor `SAME_WHEN_EMPTY` (evaluate/v2 and
  evaluate_consulting/v2, whose prompts equal v3's when the corrections block is empty) is older.
- A v2 read was given no block, so it is older when the role's block now has a line.
- A current-version read is older when a correction in the role's block now was added or changed
  after it was scored, or is not among the corrections the read was given
  (`Evaluation.correction_ids`; D-SCORE-7, so a correction a first score missed by text marks
  it), or when a correction the read was given was removed, changed or lost its story since. A
  read stored before that column (NULL) has only the first test. A correction that merely
  stopped matching because a newer read words the requirement differently, or a posting note
  archived because its requirement left the posting, does not mark the score: Score again would
  not send it either.

The block is `corrections.for_role`: this role's `posting` notes and the `person` rows whose
requirement this role's latest read asks for, the exact set, cap and order the prompt states and
the online confirmation lists (D-SCORE-1). So one correction marks only the roles that ask for it.
A role's first score, made before it had a read, states the `person` rows whose requirement text is
in its description (D-SCORE-7); the read records them in `correction_ids`, so a correction it was
given is never "new" to it, and one it was not given marks it only when added or changed later.

Which read (D-SCORE-8): the block is stated against the role's latest read, highest tier first
(SPEC.md 4.4, the read the page shows and the prompt matches against), but the version, the time
and the given ids come from the role's NEWEST read by time (Tier 2 or Tier 3, highest id). A role
whose latest read is a Tier 3 deeper read keeps showing it after Score again, which stores a Tier
2 read given exactly the block; measuring against that newer read is what lets "Score again to
refresh it" clear the mark it shows. A deeper read is never re-run by Score again (it is opt-in
and asks first, SPEC.md 6.4).

Nothing here reads the prompt templates or changes them, and calibration, few-shot, the reranker
and drift keep pooling every version (D-SCORE-2): this is a display and offer rule only.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from functools import lru_cache
from typing import Any

import structlog
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import RequirementCorrection
from kestrel.learning.corrections import (
    PERSON,
    POSTING,
    for_role,
)
from kestrel.pipeline.context import operator_correction_lines

log = structlog.get_logger(__name__)

# Versions whose prompt is the current one's byte for byte when no correction is stated
# (D-SCORE-1: v3 is v2 plus the OPERATOR_CORRECTIONS block and nothing else).
SAME_WHEN_EMPTY: dict[str, frozenset[str]] = {
    "employment": frozenset({"evaluate/v2"}),
    "consulting": frozenset({"evaluate_consulting/v2"}),
}


@lru_cache(maxsize=1)
def current_versions() -> dict[str, str]:
    """The packaged Tier 2 prompt version per track. Empty when the prompts cannot be read: a
    warning is never worth a failed page."""
    try:
        from kestrel.pipeline._shared import prompt_spec
        from kestrel.pipeline.tier2 import _VERSION

        return {track: prompt_spec(v).version for track, v in _VERSION.items()}
    except (OSError, KeyError, ValueError, ImportError) as exc:  # a display hint only
        log.warning("score_age_versions_unreadable", error=str(exc))
        return {}


def _tier(ev: Any) -> int:
    return int(getattr(ev, "tier", 0) or 0)


def _since(ev: Any, newest: Any) -> Any:
    """The read the age is measured against (D-SCORE-8): `newest` when it is a Tier 2 or Tier 3
    read stored after `ev` (Score again after a deeper read), else `ev`."""
    if (newest is not None and _tier(newest) >= 2 and getattr(newest, "prompt_version", None)
            and (getattr(newest, "id", 0) or 0) > (getattr(ev, "id", 0) or 0)):
        return newest
    return ev


def _is_versioned_read(opp: Any, ev: Any) -> bool:
    """A Tier 2 or Tier 3 read with a prompt version, on a track whose version is known."""
    if ev is None or _tier(ev) < 2 or not getattr(ev, "prompt_version", None):
        return False
    return bool(current_versions().get(getattr(opp, "track", "") or ""))


def needs_corrections(opp: Any, ev: Any, newest: Any = None) -> bool:
    """Whether the answer depends on the corrections (a read that can be older and is not older
    by its version alone). Lets a caller skip the query when nothing on the page needs it."""
    ev = _since(ev, newest)
    if ev is None or _tier(ev) < 2 or not getattr(ev, "prompt_version", None):
        return False
    track = getattr(opp, "track", "") or ""
    current = current_versions().get(track)
    if not current:
        return False
    return ev.prompt_version == current or ev.prompt_version in SAME_WHEN_EMPTY.get(track, ())


def _block(opp: Any, ev: Any, corrections: Iterable[Any]) -> list[Any]:
    """The role's block as `scoring_corrections` would state it now (`for_role`, D-SCORE-7). Every
    read this module tests is Tier 2 or Tier 3, so this is the hash rule; a first score's text
    matches are judged through `Evaluation.correction_ids`, which records what was sent."""
    return for_role(corrections, opp, ev)


def _ts(c: Any) -> str:
    return c.updated_at or c.created_at or ""


def changed_things(
    opp: Any, ev: Any, corrections: Iterable[Any] = (), newest: Any = None
) -> int:
    """How many things in this role's block changed since its latest read: 0 when the score is
    current. The role page's corrections strip ("You changed N things ...") opens exactly when
    this is not 0, so it agrees with the older-score line. A version-only older read (v1) counts
    the lines its block states, which may be 0. `corrections` must include this role's archived
    `posting` rows for the removal test (`corrections_with_archived`). `newest` is the role's
    newest read by time when it is not `ev` (D-SCORE-8)."""
    if not _is_versioned_read(opp, ev):
        return 0
    rows = list(corrections)
    block = _block(opp, ev, rows)
    since = _since(ev, newest)
    if since.prompt_version != current_versions()[getattr(opp, "track", "") or ""]:
        return len(operator_correction_lines(block))
    scored_at = getattr(since, "created_at", None) or ""
    changed = {c.id for c in block if _ts(c) > scored_at}
    given = getattr(since, "correction_ids", None)
    if given is not None:
        # Set rule (D-SCORE-7): a correction the block states now that the read was not given
        # (missed by a first score's text match, or matching this read's requirements only) is
        # what Score again would add.
        changed |= {c.id for c in block if c.id not in set(given)}
    by_id = {c.id: c for c in rows}
    for cid in given or ():
        c = by_id.get(cid)
        if c is None or _ts(c) > scored_at or (c.story_deleted_at or "") > scored_at:
            changed.add(cid)
    return len(changed)


def is_older(opp: Any, ev: Any, corrections: Iterable[Any] = (), newest: Any = None) -> bool:
    """The rule in the module docstring, for one role's latest read. `corrections` are the rows
    `corrections_for` reads for the role (more is harmless: `for_scoring` filters them)."""
    if not _is_versioned_read(opp, ev):
        return False
    track = getattr(opp, "track", "") or ""
    version = _since(ev, newest).prompt_version
    if version != current_versions()[track] and version not in SAME_WHEN_EMPTY.get(track, ()):
        return True
    return changed_things(opp, ev, corrections, newest) > 0


async def newest_reads(s: AsyncSession, pairs: Sequence[tuple[Any, Any]]) -> dict[int, Any]:
    """For each role whose latest read is a Tier 3 deeper read, its newest Tier 2 or Tier 3
    read by time when that is a different, later one (D-SCORE-8). No query when no latest read
    is Tier 3."""
    deep = {opp.id: ev for opp, ev in pairs if ev is not None and _tier(ev) >= 3}
    if not deep:
        return {}
    from kestrel.db.models import Evaluation

    out: dict[int, Any] = {}
    rows = (await s.execute(
        select(Evaluation).where(Evaluation.opportunity_id.in_(list(deep)), Evaluation.tier >= 2)
        .order_by(Evaluation.opportunity_id, Evaluation.id.desc())
    )).scalars()
    for ev in rows:
        if ev.opportunity_id not in out and ev.id > (deep[ev.opportunity_id].id or 0):
            out[ev.opportunity_id] = ev
        out.setdefault(ev.opportunity_id, None)
    return {k: v for k, v in out.items() if v is not None}


async def older_ids(s: AsyncSession, pairs: Sequence[tuple[Any, Any]]) -> set[int]:
    """The ids among (opportunity, latest evaluation) pairs whose score reads as older. One
    query for every role at once (the live `posting` rows and the `person` rows on the tracks
    present), none when no read needs the corrections."""
    out: set[int] = set()
    need = []
    newer = await newest_reads(s, pairs)
    for opp, ev in pairs:
        if needs_corrections(opp, ev, newer.get(opp.id)):
            need.append((opp, ev))
        elif is_older(opp, ev, newest=newer.get(opp.id)):
            out.add(opp.id)
    if not need:
        return out
    ids = {opp.id for opp, _ev in need}
    tracks = {opp.track for opp, _ev in need}
    rows = list((await s.execute(select(RequirementCorrection).where(or_(
        and_(RequirementCorrection.scope == POSTING,
             RequirementCorrection.opportunity_id.in_(ids)),
        and_(RequirementCorrection.scope == PERSON,
             RequirementCorrection.track.in_(tracks)),
    )))).scalars())
    by_track: dict[str, list[Any]] = {}
    by_role: dict[int, list[Any]] = {}
    for c in rows:
        if c.scope == PERSON:
            by_track.setdefault(c.track, []).append(c)
        elif c.opportunity_id is not None:
            by_role.setdefault(c.opportunity_id, []).append(c)
    for opp, ev in need:
        mine = by_role.get(opp.id, []) + by_track.get(opp.track, [])
        if is_older(opp, ev, mine, newer.get(opp.id)):
            out.add(opp.id)
    return out


async def older_for(s: AsyncSession, opp: Any, ev: Any) -> bool:
    """`is_older` for one role, reading its corrections only when the answer depends on them."""
    if opp is None:
        return False
    newest = (await newest_reads(s, [(opp, ev)])).get(opp.id)
    if not needs_corrections(opp, ev, newest):
        return is_older(opp, ev, newest=newest)
    return is_older(opp, ev, await corrections_with_archived(s, opp), newest)


async def changed_for(s: AsyncSession, opp: Any, ev: Any) -> int:
    """`changed_things` for one role. Any versioned read reads the corrections, a version-older
    one included: its count is the lines its block states, so the strip opens whenever the older
    line shows and the block has a line (Fable final review)."""
    if opp is None or not _is_versioned_read(opp, ev):
        return 0
    newest = (await newest_reads(s, [(opp, ev)])).get(opp.id)
    return changed_things(opp, ev, await corrections_with_archived(s, opp), newest)


async def corrections_with_archived(s: AsyncSession, opp: Any) -> list[Any]:
    """`corrections_for` plus this role's archived `posting` rows, so an archived note a read was
    given reads as archived (not as removed). `for_scoring` leaves archived rows out."""
    return list((await s.execute(select(RequirementCorrection).where(or_(
        RequirementCorrection.opportunity_id == opp.id,
        and_(RequirementCorrection.scope == PERSON, RequirementCorrection.track == opp.track),
    )))).scalars())


async def older_among(s: AsyncSession, opp_ids: Iterable[int]) -> set[int]:
    """`older_ids` for roles by id, each with its latest evaluation (highest tier, then newest)."""
    ids = list(dict.fromkeys(int(i) for i in opp_ids))
    if not ids:
        return set()
    from kestrel.db.models import Evaluation, Opportunity
    from kestrel.web.queries import _latest_eval_subq

    latest = _latest_eval_subq()
    pairs = (await s.execute(
        select(Opportunity, Evaluation)
        .join(latest, latest.c.opportunity_id == Opportunity.id)
        .join(Evaluation, Evaluation.id == latest.c.eid)
        .where(Opportunity.id.in_(ids))
    )).all()
    return await older_ids(s, [(o, e) for o, e in pairs])
