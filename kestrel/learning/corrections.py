"""Requirement corrections: the operator corrects one thing a posting asks for (issue 170).

SPEC-MODULES 5.1 `requirement_correction`, D-LEARN-3, D-UI-290, D-UI-291. This module is the one
contract the role page, Profile › Corrections and the scoring context use:

- `save_correction`, `change_correction`, `remove_correction` write rows and return an `Undo`;
  `undo_correction` puts back exactly what was there, field for field, same id and timestamps.
- `apply_corrections` lays the corrections over the rows `kestrel.web.role_view.requirements`
  builds, so the wheel and the counts use the operator's states. It never touches the stored
  Evaluation: the corrected state lives only in the returned rows (invariant 6).
- `list_for_profile`, `roles_using`, `count_roles` answer Profile and the rescore offer.
- `after_evaluation` runs when a new read is stored: it sets `agrees_now` and archives a
  `posting` row whose requirement left that posting.

A correction is not a `label` row: nothing here writes `label` or `verdict_override`, so it never
counts toward the 15, 30 or 200 thresholds, Calibrate history or /settings/learning. Nothing here
reads or writes an opportunity's stage, archive state or any Tier 0 or Tier 1 result (hard rule 4).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Evaluation, Opportunity, RequirementCorrection, Story
from kestrel.match_text import requirement_hash, requirement_norm
from kestrel.web import role_view as rv

# The module's own clock alias: tests patch this, never the global clock (D-TEST-2).
_now = utcnow_iso

MET, PART, GAP = rv.MET, rv.PART, rv.GAP
STATES = (MET, PART, GAP)
REASONS = ("overstated", "not_me", "misread", "all", "some", "none", "agree")
BASES = ("quoted", "inferred", "none")
PERSON, POSTING = "person", "posting"
# Where each answer moves the row. `misread` (They mean something else) and `agree` (It's a fair
# gap) keep Kestrel's state: the first adds a note about this posting, the second confirms.
_TARGET = {"overstated": PART, "not_me": GAP, "all": MET, "some": PART, "none": GAP}
# Answers that credit the operator; a deleted story stops them (SPEC-MODULES 5.1).
STORY_REASONS = ("all", "some")
# What the reason chips say, for the Profile search (handoff 3.7).
REASON_WORDS = {
    "overstated": "It's overstated",
    "not_me": "Not true of me",
    "misread": "They mean something else",
    "all": "I have all of it",
    "some": "Some of it",
    "none": "None of it",
    "agree": "It's a fair gap",
}


class CorrectionError(ValueError):
    """A correction that cannot be saved as asked (bad reason or state, missing note or role)."""


@dataclass(frozen=True)
class Undo:
    """What one write changed: `written` ids are removed on undo, then every `before` snapshot is
    put back exactly. JSON-safe through `to_json` and `from_json` so a route can hold it."""

    written: tuple[int, ...] = ()
    before: tuple[dict[str, Any], ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"written": list(self.written), "before": [dict(b) for b in self.before]}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Undo:
        return cls(
            written=tuple(int(i) for i in data.get("written") or ()),
            before=tuple(dict(b) for b in data.get("before") or ()),
        )


@dataclass
class ProfileItem:
    """One `person` correction as Profile › Corrections lists it."""

    correction: RequirementCorrection
    role_title: str | None
    role_org: str | None
    role_archived: bool
    story_title: str | None
    story_operator_edited: bool | None
    story_deleted: bool
    counts_on: int
    roles: list[int] = field(default_factory=list)


def snapshot(row: RequirementCorrection) -> dict[str, Any]:
    """Every column of a row, as stored."""
    return {c.name: getattr(row, c.name) for c in RequirementCorrection.__table__.columns}


def operator_state_for(reason: str, kestrel_state: str) -> str:
    """The state an answer gives the row; `misread` and `agree` keep Kestrel's."""
    if reason not in REASONS:
        raise CorrectionError(f"unknown reason {reason!r}")
    if kestrel_state not in STATES:
        raise CorrectionError(f"unknown state {kestrel_state!r}")
    return _TARGET.get(reason, kestrel_state)


def scope_for(reason: str) -> str:
    return POSTING if reason == "misread" else PERSON


def applies(c: Any) -> bool:
    """Whether a correction changes the row's state: never an archived one or a `misread` note,
    and not an `all` or `some` answer whose story was deleted (it falls back to Kestrel's state)."""
    if c.archived_at or c.reason == "misread":
        return False
    return not (c.reason in STORY_REASONS and c.story_deleted_at)


def requirement_hashes(track: str | None, blocks: dict | None) -> list[str]:
    """The hash of every row the role page builds from one evaluation's blocks, in row order."""
    opp = _TrackOnly(track)
    return [requirement_hash(r["text"]) for r in rv.requirements(opp, {"blocks": blocks or {}})]


@dataclass(frozen=True)
class _TrackOnly:
    track: str | None


# ------------------------------------------------------------------------------- apply to rows
def apply_corrections(
    rows: Sequence[dict[str, Any]],
    corrections: Iterable[Any],
    *,
    opportunity_id: int,
    texts: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """The rows with each matching correction laid over them, as new dicts (the input is left as
    is). `texts` are the unredacted requirement texts in row order, used for the hash when the
    rows' own text was redacted for confidential mode. A `posting` row for this role wins over a
    `person` row for the same requirement (D-LEARN-6). Every row gets its hash as `h`.
    A matched row gets `state`, `mark` and `word` from the operator's state when the correction
    applies, and a `correction` dict: id, reason, note, scope, story_id, operator_state,
    kestrel_state (this read's state for the row), from_elsewhere (a `person` row made on
    another role), agrees_now, applies, story_deleted."""
    posting: dict[str, Any] = {}
    person: dict[str, Any] = {}
    for c in corrections:
        if c.scope == POSTING:
            if c.opportunity_id == opportunity_id and not c.archived_at:
                posting[c.requirement_hash] = c
        elif c.scope == PERSON:
            person[c.requirement_hash] = c
    use_texts = texts if texts is not None and len(texts) == len(rows) else None
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        new = dict(row)
        h = requirement_hash(use_texts[i] if use_texts is not None else row.get("text"))
        new["h"] = h  # the row's requirement hash: what the role page's forms post (issue 170)
        c = posting.get(h) or person.get(h)
        if c is not None:
            kestrel_state = row["state"]
            on = applies(c)
            state = c.operator_state if on and c.operator_state in STATES else kestrel_state
            new.update(
                {
                    "state": state,
                    "mark": rv.MARKS[state],
                    "word": rv.STATE_WORDS[state],
                    "correction": {
                        "id": c.id,
                        "reason": c.reason,
                        "note": c.note,
                        "scope": c.scope,
                        "story_id": c.story_id,
                        "operator_state": c.operator_state,
                        "kestrel_state": kestrel_state,
                        "from_elsewhere": c.scope == PERSON and c.opportunity_id != opportunity_id,
                        "agrees_now": bool(c.agrees_now),
                        "applies": on,
                        "story_deleted": bool(c.story_deleted_at),
                    },
                }
            )
        out.append(new)
    return out


async def corrections_for(s: AsyncSession, opp: Opportunity) -> list[RequirementCorrection]:
    """Every correction that may apply to a role: its live `posting` rows and the `person` rows
    on its track."""
    stmt = select(RequirementCorrection).where(
        or_(
            and_(
                RequirementCorrection.scope == POSTING,
                RequirementCorrection.opportunity_id == opp.id,
                RequirementCorrection.archived_at.is_(None),
            ),
            and_(
                RequirementCorrection.scope == PERSON,
                RequirementCorrection.track == opp.track,
            ),
        )
    )
    return list((await s.execute(stmt)).scalars())


# The most lines the scoring prompt's OPERATOR_CORRECTIONS block carries (PROMPTS.md P2 User).
SCORING_MAX_LINES = 20


# The shortest normalized requirement text that may match a posting's description by text
# (D-SCORE-7): "C", "R", "Go", "C#" or "UX" occur as words in ordinary prose by chance, so they
# match only by hash, once the role has a read.
TEXT_MATCH_MIN_CHARS = 3


def _padded(text: str | None) -> str:
    return f" {requirement_norm(text)} "


def requirement_in_text(requirement: str | None, text: str | None) -> bool:
    """Whether the whole requirement occurs in `text` on word boundaries, both in the form the
    requirement hash uses (`requirement_norm`: lowercase, `+` and `#` kept, every script kept), so
    "C" never matches "C++" or "CEO" and "Java" never matches "JavaScript" (D-SCORE-7). Code only:
    no model is asked. A requirement shorter than TEXT_MATCH_MIN_CHARS never matches."""
    req = requirement_norm(requirement)
    if len(req) < TEXT_MATCH_MIN_CHARS:
        return False
    return f" {req} " in _padded(text)


def for_scoring(
    corrections: Iterable[Any], *, opportunity_id: int,
    requirement_hashes: Iterable[str] | None = None, description: str | None = None,
) -> list[Any]:
    """The corrections the scoring prompt states as the operator's facts for one role, at most
    SCORING_MAX_LINES (PROMPTS.md P2 User, D-SCORE-1): this role's live `posting` rows that carry
    a note, then the `person` rows on the track that apply AND whose requirement this posting
    asks for. With a read (`requirement_hashes`, from its latest Tier 2 or Tier 3 read) that is
    an exact hash match (D-SCORE-5); with no read yet (`requirement_hashes` None) it is the
    requirement's text occurring in the posting's `description` on whole words
    (`requirement_in_text`, owner 2026-11-11, D-SCORE-7). A `person` row about anything else is
    never stated: an unrelated "does not have X" line on every posting could lower verdicts
    (owner, 2026-11-11, reversing D-SCORE-1's "no filtering"). On its own role a `posting` row
    replaces a `person` row for the same requirement (D-LEARN-6), the same precedence
    `apply_corrections` uses. Newest answer first (D-SCORE-3). Pure; `scoring_corrections` reads
    the rows."""
    here = set(requirement_hashes) if requirement_hashes is not None else None
    desc = _padded(description) if here is None and description else None

    def asked(c: Any) -> bool:
        if here is not None:
            return c.requirement_hash in here
        if desc is None:
            return False
        req = requirement_norm(getattr(c, "requirement_text", None))
        return len(req) >= TEXT_MATCH_MIN_CHARS and f" {req} " in desc

    posting: dict[str, Any] = {}
    person: dict[str, Any] = {}
    for c in corrections:
        if c.scope == POSTING:
            if c.opportunity_id == opportunity_id and not c.archived_at and (c.note or "").strip():
                posting[c.requirement_hash] = c
        elif (
            c.scope == PERSON and applies(c) and c.operator_state in STATES and asked(c)
        ):
            person[c.requirement_hash] = c
    chosen = list(posting.values()) + [c for h, c in person.items() if h not in posting]
    chosen.sort(key=lambda c: (c.updated_at or c.created_at or "", c.id or 0), reverse=True)
    return chosen[:SCORING_MAX_LINES]


def is_read(ev: Any) -> bool:
    """Whether an evaluation is a read the hash rule matches against: Tier 2 or Tier 3. A Tier 1
    screen lists no requirements, so a role with only one has no read yet (D-SCORE-7)."""
    return ev is not None and int(getattr(ev, "tier", 0) or 0) >= 2


def for_role(corrections: Iterable[Any], opp: Any, ev: Any) -> list[Any]:
    """`for_scoring` for one role and its latest evaluation: the hash rule against a read, the
    description text rule before the first one. The one rule the prompt, the consent digest and
    dialog, and `score_age` use (D-SCORE-7)."""
    oid = int(getattr(opp, "id", 0) or 0)
    if is_read(ev):
        return for_scoring(corrections, opportunity_id=oid, requirement_hashes=requirement_hashes(
            getattr(opp, "track", None), getattr(ev, "blocks", None)))
    return for_scoring(corrections, opportunity_id=oid,
                       description=getattr(opp, "description_md", None))


async def scoring_corrections(s: AsyncSession, opp: Opportunity) -> list[RequirementCorrection]:
    """`for_role` over the rows `corrections_for` reads and the role's latest evaluation: what a
    Tier 2 or Tier 3 prompt for this role states, and what an online send's confirmation lists."""
    ev = await _latest_evaluation(s, opp.id)
    return for_role(await corrections_for(s, opp), opp, ev)


async def corrected_requirements(
    s: AsyncSession,
    opp: Opportunity,
    ev: Evaluation | None,
    ev_json: dict | None,
    *,
    thin: bool = False,
) -> list[dict[str, Any]]:
    """`role_view.requirements` with the corrections applied. Hashes come from the stored
    (unredacted) evaluation blocks, so confidential mode does not change which rows match."""
    rows = rv.requirements(opp, ev_json, thin=thin)
    if not rows:
        return rows
    texts = None
    if ev is not None:
        raw = rv.requirements(_TrackOnly(opp.track), {"blocks": ev.blocks or {}})
        texts = [r["text"] for r in raw]
    return apply_corrections(
        rows, await corrections_for(s, opp), opportunity_id=opp.id, texts=texts
    )


# --------------------------------------------------------------------------------- writes
async def _latest_evaluation(s: AsyncSession, opp_id: int) -> Evaluation | None:
    """Highest tier, then newest: the same rule as `kestrel.web.queries.latest_evaluation`."""
    return (
        await s.execute(
            select(Evaluation)
            .where(Evaluation.opportunity_id == opp_id)
            .order_by(Evaluation.tier.desc(), Evaluation.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _one(s: AsyncSession, *where) -> RequirementCorrection | None:
    return (
        await s.execute(select(RequirementCorrection).where(*where).limit(1))
    ).scalar_one_or_none()


async def save_correction(
    s: AsyncSession,
    *,
    opportunity_id: int,
    requirement_text: str,
    kestrel_state: str,
    reason: str,
    note: str | None = None,
    story_id: int | None = None,
    basis: str = "none",
    evaluation_id: int | None = None,
) -> tuple[RequirementCorrection, Undo]:
    """Create or replace a correction made on one role, and commit. A `person` answer replaces
    the track's earlier row for the same requirement (in place: same id and `created_at`) and the
    role's own `posting` note on it; a `posting` answer (`misread`, note required) replaces that
    role's earlier note. Returns the row and the `Undo` that puts back what was there."""
    op_state = operator_state_for(reason, kestrel_state)
    if basis not in BASES:
        raise CorrectionError(f"unknown basis {basis!r}")
    note = (note or "").strip() or None
    if reason == "misread" and not note:
        raise CorrectionError("They mean something else needs a note")
    text = " ".join((requirement_text or "").split())
    if not text or not requirement_norm(text):  # nothing a hash could tell apart (D-LEARN-11)
        raise CorrectionError("the requirement is empty")
    opp = await s.get(Opportunity, opportunity_id)
    if opp is None:
        raise CorrectionError(f"no role {opportunity_id}")
    if story_id is not None and await s.get(Story, story_id) is None:
        raise CorrectionError(f"no story {story_id}")
    if evaluation_id is None:
        ev = await _latest_evaluation(s, opportunity_id)
        evaluation_id = ev.id if ev is not None else None
    h = requirement_hash(text)
    scope = scope_for(reason)
    if scope == PERSON:
        existing = await _one(
            s,
            RequirementCorrection.scope == PERSON,
            RequirementCorrection.track == opp.track,
            RequirementCorrection.requirement_hash == h,
        )
    else:
        existing = await _one(
            s,
            RequirementCorrection.scope == POSTING,
            RequirementCorrection.opportunity_id == opportunity_id,
            RequirementCorrection.requirement_hash == h,
        )
    before: list[dict[str, Any]] = []
    if scope == PERSON:
        # The newer answer on this role replaces the role's own note about the requirement.
        stale = await _one(
            s,
            RequirementCorrection.scope == POSTING,
            RequirementCorrection.opportunity_id == opportunity_id,
            RequirementCorrection.requirement_hash == h,
        )
        if stale is not None:
            before.append(snapshot(stale))
            await s.delete(stale)
    now = _now()
    values = {
        "track": opp.track,
        "opportunity_id": opportunity_id,
        "evaluation_id": evaluation_id,
        "requirement_text": text,
        "requirement_hash": h,
        "kestrel_state": kestrel_state,
        "operator_state": op_state,
        "reason": reason,
        "scope": scope,
        "note": note,
        "story_id": story_id,
        "basis": basis,
        "agrees_now": False,
        "archived_at": None,
        "story_deleted_at": None,
        "updated_at": now,
    }
    if existing is not None:
        before.append(snapshot(existing))
        for k, v in values.items():
            setattr(existing, k, v)
        row = existing
    else:
        row = RequirementCorrection(**values, created_at=now)
        s.add(row)
    await s.flush()
    undo = Undo(written=(row.id,), before=tuple(before))
    await s.commit()
    return row, undo


async def change_correction(
    s: AsyncSession, correction_id: int, *, operator_state: str, note: str | None
) -> tuple[RequirementCorrection, Undo] | None:
    """Profile's What's true now?: set the state and the note on an existing row, and commit.
    The reason and story stay; `agrees_now` clears until the next read. None when there is no
    such row."""
    if operator_state not in STATES:
        raise CorrectionError(f"unknown state {operator_state!r}")
    row = await s.get(RequirementCorrection, correction_id)
    if row is None:
        return None
    note = (note or "").strip() or None
    if row.reason == "misread" and not note:
        raise CorrectionError("They mean something else needs a note")
    undo = Undo(written=(row.id,), before=(snapshot(row),))
    row.operator_state = operator_state
    row.note = note
    row.agrees_now = False
    row.updated_at = _now()
    await s.commit()
    return row, undo


async def remove_correction(s: AsyncSession, correction_id: int) -> Undo | None:
    """Delete a row, and commit. The `Undo` restores it with the same id and timestamps. None
    when there is no such row."""
    row = await s.get(RequirementCorrection, correction_id)
    if row is None:
        return None
    undo = Undo(written=(), before=(snapshot(row),))
    await s.delete(row)
    await s.commit()
    return undo


async def undo_correction(s: AsyncSession, undo: Undo) -> None:
    """Put back exactly what a save, change or remove replaced, and commit."""
    for rid in dict.fromkeys((*undo.written, *(b["id"] for b in undo.before))):
        row = await s.get(RequirementCorrection, rid)
        if row is not None:
            await s.delete(row)
    await s.flush()
    for snap in undo.before:
        s.add(RequirementCorrection(**snap))
    await s.commit()


# Remove's Undo in Profile and on the role page is the same operation.
restore_correction = undo_correction


# ------------------------------------------------------------------------------- reads
async def roles_using(s: AsyncSession, track: str, hashes: Iterable[str]) -> dict[str, list[int]]:
    """For each hash, the open (not archived) roles on the track whose latest evaluation has that
    requirement, by id. Computed live (SPEC-MODULES 5.1 "Counts on N roles")."""
    wanted = set(hashes)
    out: dict[str, list[int]] = {h: [] for h in wanted}
    if not wanted:
        return out
    from kestrel.web.queries import _latest_eval_subq

    latest = _latest_eval_subq()
    stmt = (
        select(Opportunity.id, Evaluation.blocks)
        .join(latest, latest.c.opportunity_id == Opportunity.id)
        .join(Evaluation, Evaluation.id == latest.c.eid)
        .where(Opportunity.track == track, Opportunity.archived_at.is_(None))
        .order_by(Opportunity.id)
    )
    for opp_id, blocks in (await s.execute(stmt)).all():
        for h in set(requirement_hashes(track, blocks)) & wanted:
            out[h].append(opp_id)
    return out


async def count_roles(s: AsyncSession, track: str, requirement_hash_: str) -> int:
    return len((await roles_using(s, track, [requirement_hash_]))[requirement_hash_])


def _matches(item: ProfileItem, q: str) -> bool:
    c = item.correction
    hay = " ".join(
        x
        for x in (
            c.requirement_text,
            c.reason,
            REASON_WORDS.get(c.reason, ""),
            c.note or "",
            item.role_title or "",
        )
        if x
    ).lower()
    return all(word in hay for word in q.lower().split())


async def list_for_profile(
    s: AsyncSession, *, track: str | None = None, query: str | None = None
) -> list[ProfileItem]:
    """Every `person` correction, newest first (`updated_at`, then id), with its role, story and
    live role count. `query` keeps the items whose requirement, reason, note or role title holds
    every word of it (case-insensitive)."""
    stmt = (
        select(RequirementCorrection, Opportunity, Story)
        .outerjoin(Opportunity, Opportunity.id == RequirementCorrection.opportunity_id)
        .outerjoin(Story, Story.id == RequirementCorrection.story_id)
        .where(RequirementCorrection.scope == PERSON)
        .order_by(RequirementCorrection.updated_at.desc(), RequirementCorrection.id.desc())
    )
    if track is not None:
        stmt = stmt.where(RequirementCorrection.track == track)
    rows = (await s.execute(stmt)).all()
    by_track: dict[str, set[str]] = {}
    for c, _o, _st in rows:
        by_track.setdefault(c.track, set()).add(c.requirement_hash)
    using: dict[tuple[str, str], list[int]] = {}
    for t, hs in by_track.items():
        for h, ids in (await roles_using(s, t, hs)).items():
            using[(t, h)] = ids
    items = [
        ProfileItem(
            correction=c,
            role_title=o.title if o is not None else None,
            role_org=o.org_name if o is not None else None,
            role_archived=bool(o is not None and o.archived_at),
            story_title=st.title if st is not None else None,
            story_operator_edited=bool(st.operator_edited) if st is not None else None,
            story_deleted=bool(c.story_deleted_at),
            counts_on=len(using.get((c.track, c.requirement_hash), [])),
            roles=list(using.get((c.track, c.requirement_hash), [])),
        )
        for c, o, st in rows
    ]
    q = (query or "").strip()
    return [i for i in items if _matches(i, q)] if q else items


# ------------------------------------------------------------------------------- rescore
async def after_evaluation(s: AsyncSession, opportunity_id: int) -> None:
    """Called when a new read of a role is stored (before the caller commits). For each
    correction made on this role: when the requirement is still there, `agrees_now` says whether
    Kestrel's new state equals the operator's (the operator's state wins either way); a `posting`
    row whose requirement left the posting is archived, never deleted. A `person` row is never
    archived by one posting. Never touches the evaluation or the role."""
    ev = await _latest_evaluation(s, opportunity_id)
    opp = await s.get(Opportunity, opportunity_id)
    if ev is None or opp is None:
        return
    states: dict[str, str] = {}
    for r in rv.requirements(_TrackOnly(opp.track), {"blocks": ev.blocks or {}}):
        states.setdefault(requirement_hash(r["text"]), r["state"])
    rows = (
        await s.execute(
            select(RequirementCorrection).where(
                RequirementCorrection.opportunity_id == opportunity_id,
                RequirementCorrection.archived_at.is_(None),
            )
        )
    ).scalars()
    now = _now()
    for c in rows:
        state = states.get(c.requirement_hash)
        if state is None:
            if c.scope == POSTING:
                c.archived_at = now
            continue
        c.agrees_now = c.reason != "misread" and state == c.operator_state
    await s.flush()
