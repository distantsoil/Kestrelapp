"""Story consolidation (P8, SPEC-MODULES 3.1). A `consolidate_stories` run embeds every Block F
sketch produced since the last consolidation, clusters them by cosine similarity above
`settings.stories.merge_threshold`, and proposes ONE merged canonical story per cluster, showing the
sketches it came from.

The absolute rule (constraint: the operator's own words about their own career outrank any model
synthesis): an `operator_edited` story is NEVER overwritten by a later merge. A new cluster that
matches an existing canonical story attaches its sketches to that story as usage instead of
re-synthesizing over it. This is enforced HERE, in code — a matched cluster never reaches P8.

Resumability (constraint 5): the run advances a watermark (`consolidation.stories_watermark`) only
after committing, so a kill-and-restart re-processes exactly the sketches not yet consolidated and
never double-counts. Story embeddings are recomputed from story text each run rather than persisted;
a consolidation is an on-demand batch over a dozen stories, so re-embedding them is negligible and
keeps the phase migration-free (see the private decision log)."""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import structlog
from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Evaluation, Opportunity, Setting, Story, StoryUsage
from kestrel.llm.embeddings import cosine
from kestrel.llm.guard import QuarantineHook
from kestrel.llm.prompts import load_prompt
from kestrel.pipeline.profile import load_profile

log = structlog.get_logger()

_WATERMARK_KEY = "consolidation.stories_watermark"
_PROOF_MARKER = '{for each: "- {claim} | evidence: {evidence} | metric: {metric}"}'
_SKETCH_MARKER = (
    '{for each sketch: "[{id}] S: {situation} | T: {task} | A: {action} '
    '| R: {result} | Reflection: {reflection}"}'
)
# A note sketch's id is this base plus its opportunity id, far above any evaluation-based id.
_NOTE_ID_BASE = 10**12
# sketch id = evaluation_id * this + index. Block F is capped at 4 sketches per evaluation
# (schema _cap(4)), so an index never approaches this multiplier and ids never collide across
# evaluations; the wide multiplier is defensive headroom, not a tight bound.
_SKETCHES_PER_EVAL = 1000
_CONSUMED_KEY = "consolidation.consumed_sketch_ids"
# Drafts the operator dismissed (D-UI-55): {sketch id (str): fingerprint of its text}. A build
# skips a sketch whose text is unchanged since it was dismissed, so a dismissed draft is never
# proposed again, while a note the operator later rewrites is a new thing and is read again.
DISMISSED_KEY = "stories.dismissed_sketches"
_GROUP_PREFIX = "stories.dismiss_undo.group."
# Sketches whose cluster the model refused to merge: {sketch id (str): fingerprint of its text}.
# A separate map from the dismissed one, so nothing is hidden from the operator and undo is
# unaffected. A later build skips a sketch only while its text is unchanged (a refused cluster is
# not asked again at full model cost); an operator note is never put here.
REFUSED_KEY = "stories.refused_sketches"


@dataclass
class Sketch:
    """One Block F STAR+Reflection sketch, lifted out of Evaluation.blocks with a stable id."""

    id: int
    evaluation_id: int
    opportunity_id: int
    created_at: str
    question: str
    situation: str
    task: str
    action: str
    result: str
    reflection: str
    proof_point_key: str | None
    # "sketch" (a Block F sketch from a scored role) or "note" (the operator's own notes for an
    # interview, kestrel.interview.notes). A note carries its whole text in `situation`.
    kind: str = "sketch"

    def text(self) -> str:
        return " ".join(
            p for p in (self.situation, self.task, self.action, self.result, self.reflection) if p
        )


# ------------------------------------------------------------------ placeholder / refusal checks
# Text a model writes when the profile had nothing to draw on. Matched against the whole field
# after normalizing (lowercase, punctuation to spaces), so a real sentence that merely contains
# one of these words is never treated as a placeholder.
_PLACEHOLDER_PHRASES = frozenset({
    "n a", "na", "none", "nil", "not applicable", "not available", "no content available",
    "no content", "your own example", "not provided", "not specified", "unknown",
    "no information available", "not available from your profile",
})
# Phrases a merge result uses, at the START of its title, to say it could not build a story.
_REFUSAL_TITLE_RE = re.compile(
    r"^\W*(unmergeable|unrecoverable|unresolved|no story\b|no source content|insufficient source"
    r"|canonical story could not|story could not be (produced|reconstructed|built))",
    re.IGNORECASE,
)
# Words a refusal body uses about itself; judged only on a short field and only together with a
# refusal title.
_REFUSAL_BODY_PHRASES = (
    "could not be", "unmergeable", "no story", "insufficient", "no source content",
    "unrecoverable", "unresolved", "no substantive", "not enough",
)
_REFUSAL_FIELD_MAX = 160
_STAR_FIELDS = ("situation", "task", "action", "result", "reflection")


def _normalize(text: object) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split())


def is_placeholder_text(text: object) -> bool:
    """True for an empty field or one that is only placeholder text ("Not available from your
    profile", "No content available.", "Your own example.", "n/a" ...). Conservative: only a
    short field that is exactly a known phrase, or says a profile item is not available."""
    n = _normalize(text)
    if not n or n in _PLACEHOLDER_PHRASES:
        return True
    return len(n.split()) <= 12 and "from your profile" in n and "not available" in n


def is_placeholder_sketch(sketch: Sketch) -> bool:
    """A sketch with nothing to build a story from: every STAR field empty or placeholder text.
    An operator note is their own text, so it only counts when it is completely empty."""
    fields = [getattr(sketch, f) for f in _STAR_FIELDS]
    if sketch.kind == "note":
        return not any(str(f or "").strip() for f in fields)
    return all(is_placeholder_text(f) for f in fields)


def _empty_or_self_referential(text: object) -> bool:
    if is_placeholder_text(text):
        return True
    t = str(text or "")
    return len(t) <= _REFUSAL_FIELD_MAX and any(p in t.lower() for p in _REFUSAL_BODY_PHRASES)


def is_unusable_story(title: object, **fields: object) -> bool:
    """A merge result that is not a story: no title, every STAR field empty or placeholder, or a
    title that LEADS with a refusal ("Unmergeable: ...", "No story available") while every STAR
    field is empty, placeholder or a short statement about the refusal itself. A real sentence
    that merely contains such a word ("Cleared the backlog of unresolved incidents") is never
    judged unusable. Works on already-validated strings only."""
    if not str(title or "").strip():
        return True
    bodies = [fields.get(f) for f in _STAR_FIELDS]
    if all(is_placeholder_text(b) for b in bodies):
        return True
    return bool(_REFUSAL_TITLE_RE.match(str(title))) and all(
        _empty_or_self_referential(b) for b in bodies
    )


@dataclass
class ConsolidationResult:
    sketches: int = 0
    skipped: int = 0  # placeholder sketches left out, and sketches whose merge was discarded
    clusters: int = 0
    created_story_ids: list[int] = field(default_factory=list)
    attached: list[dict] = field(default_factory=list)  # [{story_id, sketches, operator_edited}]


# ---------------------------------------------------------------- sketch collection + watermark
async def _get_watermark(session: AsyncSession) -> str | None:
    row = await session.get(Setting, _WATERMARK_KEY)
    return row.value if row and isinstance(row.value, str) else None


async def _set_setting(session: AsyncSession, key: str, value: object) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()


async def _get_consumed(session: AsyncSession) -> set[int]:
    row = await session.get(Setting, _CONSUMED_KEY)
    return set(row.value) if row and isinstance(row.value, list) else set()


def sketch_fingerprint(sketch: Sketch) -> str:
    return hashlib.sha1(sketch.text().encode("utf-8")).hexdigest()[:16]  # noqa: S324 - not security


async def _get_dismissed(session: AsyncSession) -> dict[str, str]:
    row = await session.get(Setting, DISMISSED_KEY)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


async def _get_refused(session: AsyncSession, *, fresh: bool = False) -> dict[str, str]:
    row = await session.get(Setting, REFUSED_KEY, populate_existing=fresh)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


def question_text(question: str | None) -> str:
    """A likely question as stored on a usage row: stripped, inner whitespace collapsed."""
    return " ".join((question or "").split())


def question_key(question: str | None) -> str:
    """The comparison form of a likely question (release round review, finding 10): stripped,
    whitespace collapsed, casefolded. Dismiss and consolidate both go through it, so a usage
    row and the sketch it came from always match."""
    return question_text(question).casefold()


async def dismiss_draft(
    session: AsyncSession, story_id: int, *, undo_token: str | None = None
) -> bool:
    """Remove a proposed (not operator-edited) story and remember the sketches it came from, so
    Build stories never proposes it again. Commits. False when there is no such draft; a story
    the operator has saved is theirs and goes through `delete_story`, never this."""
    if undo_token:
        await session.execute(sql_text("BEGIN IMMEDIATE"))
        old_tokens = (await session.scalars(
            select(Setting).where(Setting.key.startswith("stories.dismiss_undo."))
        )).all()
        for old in old_tokens:
            if old.value["expires"] < time.time():
                await session.delete(old)
    story = await session.get(Story, story_id)
    if story is None or story.operator_edited:
        await session.rollback()  # end the write transaction opened above
        return False
    usages = (
        await session.execute(select(StoryUsage).where(StoryUsage.story_id == story_id))
    ).scalars().all()
    wanted = {(u.opportunity_id, question_key(u.question)) for u in usages}
    everything = await collect_sketches(session) + await collect_note_sketches(session)
    dismissed = await _get_dismissed(session)
    previous = dict(dismissed)
    for sk in everything:
        if (sk.opportunity_id, question_key(sk.question)) in wanted:
            dismissed[str(sk.id)] = sketch_fingerprint(sk)
    if undo_token:
        snapshot = {
            "expires": time.time() + 600,
            "story": {
                column.name: getattr(story, column.name) for column in Story.__table__.columns
            },
            "usages": [
                {
                    column.name: getattr(usage, column.name)
                    for column in StoryUsage.__table__.columns
                }
                for usage in usages
            ],
            "changes": {
                key: {"before": previous.get(key), "after": value}
                for key, value in dismissed.items() if previous.get(key) != value
            },
        }
        await _set_setting(session, "stories.dismiss_undo." + undo_token, snapshot)
    await _set_setting(session, DISMISSED_KEY, dismissed)
    for u in usages:
        await session.delete(u)
    await session.flush()  # usage rows first: they hold the foreign key
    await session.delete(story)
    await session.commit()
    return True


async def dismiss_all_drafts(session: AsyncSession) -> tuple[int, str | None]:
    """Dismiss every proposed story (never an operator-edited one) with `dismiss_draft`, so none
    comes back on the next build. Returns (count, group token). The token undoes the whole clear
    through `undo_dismissal_group` for ten minutes; None when there was nothing to clear."""
    ids = (
        await session.scalars(
            select(Story.id).where(Story.is_canonical.is_(True), Story.operator_edited.is_(False))
        )
    ).all()
    await session.rollback()  # end the read; dismiss_draft opens its own write transaction
    tokens: list[str] = []
    for story_id in ids:
        token = dismissal_token()
        try:
            if await dismiss_draft(session, story_id, undo_token=token):
                tokens.append(token)
        except Exception:  # noqa: BLE001 - one draft that cannot be cleared must not stop the rest
            await session.rollback()
            log.warning("story_dismiss_all_item_failed", story=story_id)
    if not tokens:
        return 0, None
    group = dismissal_token()
    await _set_setting(
        session, _GROUP_PREFIX + group, {"expires": time.time() + 600, "tokens": tokens}
    )
    await session.commit()
    return len(tokens), group


async def undo_dismissal_group(session: AsyncSession, group: str) -> bool:
    """Restore the drafts a clear-all removed. True when at least one came back."""
    row = await session.get(Setting, _GROUP_PREFIX + group)
    if row is None or row.value["expires"] < time.time():
        return False
    tokens = list(row.value["tokens"])
    await session.rollback()
    restored = False
    for token in reversed(tokens):  # last dismissed, first restored
        try:
            restored = await undo_dismissal(session, token) or restored
        except Exception:  # noqa: BLE001 - skip an item that cannot come back, finish the rest
            await session.rollback()
            log.warning("story_undo_group_item_failed")
    row = await session.get(Setting, _GROUP_PREFIX + group)
    if row is not None:
        await session.delete(row)
        await session.commit()
    return restored


def dismissal_token() -> str:
    return uuid.uuid4().hex


async def undo_dismissal(session: AsyncSession, token: str) -> bool:
    """Restore the exact proposal once, without replacing later work."""
    await session.execute(sql_text("BEGIN IMMEDIATE"))
    row = await session.get(Setting, "stories.dismiss_undo." + token)
    if row is None:
        await session.rollback()
        return False
    snapshot = row.value
    if time.time() > snapshot["expires"]:
        await session.delete(row)
        await session.commit()
        return False
    if await session.get(Story, snapshot["story"]["id"]) is not None:
        await session.rollback()
        return False
    for usage in snapshot["usages"]:
        if await session.get(StoryUsage, usage["id"]) is not None:
            await session.rollback()
            return False
        if await session.get(Opportunity, usage["opportunity_id"]) is None:
            await session.rollback()
            return False
    dismissed = await _get_dismissed(session)
    if any(dismissed.get(key) != change["after"] for key, change in snapshot["changes"].items()):
        await session.rollback()
        return False
    for key, change in snapshot["changes"].items():
        if change["before"] is None:
            dismissed.pop(key, None)
        else:
            dismissed[key] = change["before"]
    await _set_setting(session, DISMISSED_KEY, dismissed)
    session.add(Story(**snapshot["story"]))
    await session.flush()
    session.add_all(StoryUsage(**usage) for usage in snapshot["usages"])
    await session.delete(row)
    await session.commit()
    return True


async def delete_story(session: AsyncSession, story_id: int) -> bool:
    """Delete a story and its usage rows. Commits. False when there is no such story."""
    story = await session.get(Story, story_id)
    if story is None:
        return False
    for u in (
        await session.execute(select(StoryUsage).where(StoryUsage.story_id == story_id))
    ).scalars().all():
        await session.delete(u)
    await session.flush()
    await session.delete(story)
    await session.commit()
    return True


async def collect_sketches(
    session: AsyncSession, *, since: str | None = None, consumed: set[int] | None = None
) -> list[Sketch]:
    """Every Block F sketch from Tier 2 employment evaluations created after `since`, minus any
    whose id is already `consumed` (a sketch turned into a story in an earlier run that could not
    advance the time watermark because a sibling cluster failed). Sketches live inside
    Evaluation.blocks['f_interview'] (there is no per-sketch row), so each gets a stable synthetic
    id = evaluation_id * 1000 + index, which decodes back to its source evaluation."""
    consumed = consumed or set()
    stmt = select(Evaluation).where(Evaluation.tier == 2, Evaluation.track == "employment")
    if since:
        stmt = stmt.where(Evaluation.created_at > since)
    stmt = stmt.order_by(Evaluation.created_at, Evaluation.id)
    evals = (await session.execute(stmt)).scalars().all()

    out: list[Sketch] = []
    for ev in evals:
        blocks = ev.blocks if isinstance(ev.blocks, dict) else {}
        for idx, item in enumerate(blocks.get("f_interview") or []):
            if not isinstance(item, dict):
                continue
            star = item.get("star_r") or {}
            if not isinstance(star, dict):
                continue
            sid = ev.id * _SKETCHES_PER_EVAL + idx
            if sid in consumed:
                continue
            out.append(
                Sketch(
                    id=sid,
                    evaluation_id=ev.id,
                    opportunity_id=ev.opportunity_id,
                    created_at=ev.created_at,
                    question=(item.get("likely_question") or "").strip(),
                    situation=(star.get("situation") or "").strip(),
                    task=(star.get("task") or "").strip(),
                    action=(star.get("action") or "").strip(),
                    result=(star.get("result") or "").strip(),
                    reflection=(star.get("reflection") or "").strip(),
                    proof_point_key=item.get("proof_point_key"),
                )
            )
    return out


async def collect_note_sketches(
    session: AsyncSession, *, since: str | None = None, consumed: set[int] | None = None
) -> list[Sketch]:
    """The operator's interview notes saved after `since`, as sketches. The text is theirs and
    goes to the merge step as written; the merged result is still only a proposal."""
    from kestrel.interview.notes import all_notes

    consumed = consumed or set()
    out: list[Sketch] = []
    for opportunity_id, text, saved_at in await all_notes(session):
        sid = _NOTE_ID_BASE + opportunity_id
        if (since and saved_at <= since) or sid in consumed:
            continue
        out.append(
            Sketch(
                id=sid, evaluation_id=0, opportunity_id=opportunity_id, created_at=saved_at,
                question="", situation=text.strip(), task="", action="", result="",
                reflection="", proof_point_key=None, kind="note",
            )
        )
    return out


# ------------------------------------------------------------------------------------ clustering
def cluster_sketches(vecs: list[np.ndarray], threshold: float) -> list[list[int]]:
    """Greedy single-linkage clustering by cosine. A sketch joins the first cluster holding a member
    within `threshold`; otherwise it starts its own. Single-linkage is deliberately forgiving: two
    tellings of the same event should merge even if their phrasings only partly overlap."""
    clusters: list[list[int]] = []
    for i in range(len(vecs)):
        for cl in clusters:
            if any(cosine(vecs[i], vecs[j]) >= threshold for j in cl):
                cl.append(i)
                break
        else:
            clusters.append([i])
    return clusters


def _centroid(vecs: list[np.ndarray], idxs: list[int]) -> np.ndarray:
    return np.mean([vecs[i] for i in idxs], axis=0)


def _story_text(s: Story) -> str:
    return " ".join(
        p for p in (s.title, s.situation, s.task, s.action, s.result, s.reflection) if p
    )


# ----------------------------------------------------------------------------------- P8 merging
def _render_user(template: str, *, proof_points: list, sketches: list[Sketch]) -> str:
    proof_lines = "\n".join(
        f"- {_pp(p, 'claim')} | evidence: {_pp(p, 'evidence')} "
        f"| metric: {_pp(p, 'metric') or 'n/a'}"
        for p in proof_points
    ) or "(none provided)"
    sketch_lines = "\n".join(
        f"[{s.id}] NOTE in the operator's own words: {s.situation}"
        if s.kind == "note"
        else f"[{s.id}] S: {s.situation} | T: {s.task} | A: {s.action} "
        f"| R: {s.result} | Reflection: {s.reflection}"
        for s in sketches
    )
    return template.replace(_PROOF_MARKER, proof_lines).replace(_SKETCH_MARKER, sketch_lines)


def _pp(p: Any, attr: str) -> Any:
    return p.get(attr) if isinstance(p, dict) else getattr(p, attr, None)


def _quarantine_hook(session: AsyncSession) -> QuarantineHook:
    async def hook(messages: list[dict], raw_response: str, error: str, model: str) -> None:
        prompt_text = "\n\n".join(f"[{m.get('role')}]\n{m.get('content')}" for m in messages)
        from kestrel.db.models import Quarantine

        session.add(
            Quarantine(
                opportunity_id=None,
                tier=8,
                model=model,
                prompt=prompt_text[:20000],
                raw_response=(raw_response or "")[:20000],
                error=(error or "")[:4000],
                attempts=2,
            )
        )

    return hook


UNUSABLE: Any = object()  # _merge_cluster: the model answered, but not with a story


async def _merge_cluster(
    session: AsyncSession, client: Any, spec: Any, sketches: list[Sketch], proof_points: list
) -> Story | None | Any:
    """Run P8 over a cluster and persist a PROPOSED canonical story (operator_edited stays False
    until the operator rewrites it). Returns None on a quarantined/failed call rather than
    fabricating a story from nothing (hard rule 3)."""
    user = _render_user(spec.user, proof_points=proof_points, sketches=sketches)
    messages = [{"role": "system", "content": spec.system}, {"role": "user", "content": user}]
    # Through the Hosted mode plan (D-PRIV-1): on an online tier the confidential employer is
    # redacted before the send, or the send is refused when nothing is marked.
    from kestrel.pipeline._shared import run_prompt_hosted

    result = await run_prompt_hosted(
        session, client, spec, messages, quarantine_tier=2,
        on_quarantine=_quarantine_hook(session),
    )
    if not result.ok or result.value is None:
        log.warning("story_merge_failed", error=result.error, sketches=[s.id for s in sketches])
        return None

    v = result.value
    if is_unusable_story(
        v.title, situation=v.situation, task=v.task, action=v.action, result=v.result,
        reflection=v.reflection,
    ):
        # The model said it had nothing to build from. Never save that as a draft.
        log.info(
            "story_merge_discarded", title=(v.title or "")[:80], sketches=[s.id for s in sketches]
        )
        return UNUSABLE
    keys = {s.proof_point_key for s in sketches if s.proof_point_key}
    story = Story(
        title=v.title,
        situation=v.situation,
        task=v.task,
        action=v.action,
        result=v.result,
        reflection=v.reflection,
        themes=[t.value if hasattr(t, "value") else t for t in v.themes],
        proof_point_key=next(iter(keys)) if len(keys) == 1 else None,
        is_canonical=True,
        operator_edited=False,
        use_count=len(sketches),
    )
    session.add(story)
    await session.flush()
    for s in sketches:
        session.add(
            StoryUsage(story_id=story.id, opportunity_id=s.opportunity_id,
                       question=question_text(s.question))
        )
    return story


# ------------------------------------------------------------------------------ orchestration
async def consolidate_stories(
    session: AsyncSession, client: Any, settings: Settings
) -> ConsolidationResult:
    """Embed new sketches, cluster them, and for each cluster either attach it to an existing
    canonical story (NEVER overwriting, especially not an operator-edited one) or merge it into a
    new proposed story via P8. Advances the watermark and commits atomically."""
    watermark = await _get_watermark(session)
    consumed = await _get_consumed(session)
    sketches = await collect_sketches(session, since=watermark, consumed=consumed)
    sketches += await collect_note_sketches(session, since=watermark, consumed=consumed)
    dismissed = await _get_dismissed(session)
    if dismissed:
        sketches = [s for s in sketches if dismissed.get(str(s.id)) != sketch_fingerprint(s)]
    # A cluster the model refused before is not asked again while its sketches' text is unchanged.
    # Never a note: the operator's own text is always read.
    refused = await _get_refused(session)
    refused_skipped = 0
    if refused:
        kept = [s for s in sketches
                if s.kind == "note" or refused.get(str(s.id)) != sketch_fingerprint(s)]
        refused_skipped, sketches = len(sketches) - len(kept), kept
    # Sketches with nothing in them (a profile without proof points makes the model write "Not
    # available") are never embedded or sent. This is a pure check of the text, repeated each
    # build, so a sketch whose text later gains real content is read like any other. They count
    # as handled for the watermark, so they never hold it back.
    blank_ids = {s.id for s in sketches if is_placeholder_sketch(s)}
    sketches = [s for s in sketches if s.id not in blank_ids]
    result = ConsolidationResult(
        sketches=len(sketches), skipped=len(blank_ids) + refused_skipped
    )
    if not sketches:
        # Nothing new: leave the watermark exactly where it is. Advancing it to a wall-clock "now"
        # here would risk stepping over an evaluation that committed after this query but with an
        # earlier timestamp, and there is nothing to gain from moving it.
        await session.commit()
        return result

    raw = await client.embed([s.text() for s in sketches])
    vecs = [np.asarray(v, dtype=np.float32) for v in raw]
    clusters = cluster_sketches(vecs, settings.stories.merge_threshold)
    result.clusters = len(clusters)

    existing = (
        (await session.execute(select(Story).where(Story.is_canonical.is_(True)))).scalars().all()
    )
    existing_vecs: dict[int, np.ndarray] = {}
    if existing:
        emb = await client.embed([_story_text(s) for s in existing])
        existing_vecs = {
            s.id: np.asarray(v, dtype=np.float32) for s, v in zip(existing, emb, strict=False)
        }

    profile = load_profile("employment")
    proof_points = list(getattr(profile, "proof_points", None) or []) if profile else []
    spec = load_prompt("story_consolidate/v1")

    done_ids: set[int] = set()  # sketches actually consolidated (created or attached) this run
    newly_refused: dict[str, str] = {}
    for cl in clusters:
        members = [sketches[i] for i in cl]
        centroid = _centroid(vecs, cl)
        match = _best_match(centroid, existing, existing_vecs, settings.stories.merge_threshold)
        if match is not None:
            # A cluster that matches an existing canonical story attaches as usage. This is the
            # never-overwrite guarantee: an operator-edited story is untouched (its text, themes,
            # and edits all preserved), and even a prior proposal is not re-synthesized over.
            for s in members:
                session.add(
                    StoryUsage(
                        story_id=match.id, opportunity_id=s.opportunity_id,
                        question=question_text(s.question),
                    )
                )
            match.use_count = (match.use_count or 0) + len(members)
            result.attached.append(
                {"story_id": match.id, "sketches": len(members),
                 "operator_edited": bool(match.operator_edited)}
            )
            done_ids.update(s.id for s in members)
            continue
        story = await _merge_cluster(session, client, spec, members, proof_points)
        if story is UNUSABLE:
            # Nothing is saved and NOTHING IS HIDDEN or dismissed: a refusal can be wrong, so the
            # sketches stay out of done_ids (like a failed merge). Each non-note one is remembered
            # in REFUSED_KEY by its text fingerprint, so a later build skips it while the text is
            # unchanged and reads it again once it changes; a note is always read again.
            # Placeholder-only sketches are already skipped before this point. The dismissed map
            # is never written here, so a dismissal made while the build ran cannot be lost.
            newly_refused.update(
                {str(m.id): sketch_fingerprint(m) for m in members if m.kind != "note"}
            )
            result.skipped += len(members)
        elif story is not None:
            result.created_story_ids.append(story.id)
            done_ids.update(s.id for s in members)
        # A failed merge (quarantine/bad response) leaves its sketches OUT of done_ids, so they are
        # neither consumed nor stepped over by the watermark; they re-consolidate on the next run.

    await _advance_progress(session, sketches, done_ids, watermark)
    if newly_refused:  # union with what is stored now, inside this transaction
        merged = await _get_refused(session, fresh=True)
        merged.update(newly_refused)
        await _set_setting(session, REFUSED_KEY, merged)
    await session.commit()
    return result


async def _advance_progress(
    session: AsyncSession, sketches: list[Sketch], done_ids: set[int], watermark: str | None
) -> None:
    """Move the durable progress forward without ever stepping over an unconsolidated sketch. If
    every collected sketch was consolidated, advance the time watermark to the newest one and clear
    the consumed set. If any failed, hold the watermark and record the succeeded sketch ids as
    consumed, so the next run retries exactly the failures and re-does nothing (constraint 5)."""
    undone = [s for s in sketches if s.id not in done_ids]
    if not undone:
        newest = max(s.created_at for s in sketches)
        await _set_setting(session, _WATERMARK_KEY, newest)
        await _set_setting(session, _CONSUMED_KEY, [])
        return
    # Hold the watermark below the earliest failure; remember which sketches above it are already
    # done so they are skipped next time. Prune consumed ids at or below the (unchanged) watermark.
    prior = await _get_consumed(session)
    keep = {i for i in (prior | done_ids)}
    await _set_setting(session, _CONSUMED_KEY, sorted(keep))
    if watermark is not None:
        await _set_setting(session, _WATERMARK_KEY, watermark)


def _best_match(
    centroid: np.ndarray, existing: list[Story], vecs: dict[int, np.ndarray], threshold: float
) -> Story | None:
    best: Story | None = None
    best_sim = threshold
    for s in existing:
        v = vecs.get(s.id)
        if v is None:
            continue
        sim = cosine(centroid, v)
        if sim >= best_sim:
            best_sim = sim
            best = s
    return best
