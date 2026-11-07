"""Where a cover letter or engagement outline is kept (SPEC-MODULES 1.6, D-LETTER-1, D-LETTER-4).

A letter is a `letter` row attached to a `doc_render` row: the render holds the PDF (file, name,
page count, `pdf_digest`) and its id is the one the role page's links carry; the letter holds the
text, the claims, the marks, the prompt version and the word count. Per role there is one
`current` letter (in Documents and on the application), at most one `proposed` (a Draft again the
person has not kept yet) and at most one `previous` (what Undo brings back). `older` letters came
from 0.7.5, which kept several per role and showed the newest; nothing shows or undoes them. A
partial unique index allows one `current` per role and kind (D-LETTER-5).

Every write that moves a letter between states is a conditional UPDATE on the state it read, with
the row count checked, so two requests at once can never both act (D-LETTER-5).

Every function that deletes a letter returns the stored PDF paths of what it deleted, as stored
(raw); the caller removes those files after its commit, through `paths.stored_pdf`, so a path
outside the documents folder is never touched and a failed commit never loses a file.

INVARIANTS: Draft again never changes the current letter until the person keeps the new one; Undo
restores the previous letter exactly as it was stored (nothing in it is rewritten); a story's
usage counts each role's current letter once. Nothing here sends anything (hard rule 1)."""

from __future__ import annotations

from typing import Any, cast

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import DocRender, Letter, Pursuit, Story, StoryUsage

log = structlog.get_logger()

CURRENT, PROPOSED, PREVIOUS, OLDER = "current", "proposed", "previous", "older"
STATUSES = (CURRENT, PROPOSED, PREVIOUS, OLDER)
# The question a cover letter's story_usage row carries, so it is told apart from interview use.
LETTER_USAGE = "Cover letter"


async def letter_for(s: AsyncSession, render_id: int | None) -> Letter | None:
    if render_id is None:
        return None
    return (await s.execute(
        select(Letter).where(Letter.render_id == render_id)
    )).scalars().first()


async def render_in(s: AsyncSession, opp_id: int, status: str) -> DocRender | None:
    """The role's newest letter render with this status, or None."""
    return (await s.execute(
        select(DocRender).join(Letter, Letter.render_id == DocRender.id)
        .where(Letter.opportunity_id == opp_id, Letter.status == status)
        .order_by(DocRender.id.desc()).limit(1)
    )).scalars().first()


async def _current_id(s: AsyncSession, opp_id: int, kind: str) -> int | None:
    return (await s.execute(select(Letter.render_id).where(
        Letter.opportunity_id == opp_id, Letter.kind == kind, Letter.status == CURRENT
    ))).scalars().first()


async def _move(s: AsyncSession, render_id: int, was: str, now: str) -> bool:
    """Move one letter from `was` to `now` only if it is still `was`. True when it moved."""
    res = await s.execute(
        update(Letter).where(Letter.render_id == render_id, Letter.status == was)
        .values(status=now, updated_at=utcnow_iso())
        .execution_options(synchronize_session=False)
    )
    return cast(CursorResult, res).rowcount == 1


async def _pursuit(s: AsyncSession, opp_id: int) -> Pursuit | None:
    return (await s.execute(
        select(Pursuit).where(Pursuit.opportunity_id == opp_id)
    )).scalars().first()


async def create(
    s: AsyncSession, *, opp_id: int, master_id: int, kind: str, body_md: str,
    claims_used: list[str], flags: list[dict], prompt_version: str, extras: dict[str, Any],
    status: str = CURRENT,
) -> tuple[DocRender, Letter, list[str]]:
    """Save a new draft (flushes, does not commit). A `current` one is attached to the
    application; a `proposed` one waits beside the current one and changes nothing else. A newer
    proposal replaces an older one, so there is never more than one; the third value is the
    replaced proposal's stored PDF path, for the caller to remove after its commit.

    One current letter per role and kind (D-LETTER-5): a draft asked to be `current` while the
    role already has a current one (the draft it was to replace is gone, or a redraft finished
    after an Undo) is saved as `proposed` instead, so the person's current draft is never
    replaced or demoted without them choosing Keep new draft."""
    from kestrel.docs.letters import word_count

    stale: list[str] = []
    if status == CURRENT and await _current_id(s, opp_id, kind) is not None:
        status = PROPOSED
    if status == PROPOSED:
        stale = await _drop_all(s, opp_id, PROPOSED)
    pursuit = await _pursuit(s, opp_id)
    render = DocRender(opportunity_id=opp_id, doc_master_id=master_id, template=kind,
                       config={"kind": kind}, rationale={}, defaults_used=False)
    s.add(render)
    await s.flush()
    letter = Letter(
        render_id=render.id, opportunity_id=opp_id,
        pursuit_id=pursuit.id if pursuit is not None else None, kind=kind, status=status,
        body_md=body_md, generated_md=body_md, claims_used=list(claims_used),
        flags=list(flags), prompt_version=prompt_version, word_count=word_count(body_md),
        edited=False, extras=dict(extras),
    )
    s.add(letter)
    await s.flush()
    if status == CURRENT and pursuit is not None:
        pursuit.cover_letter_id = render.id
    return render, letter, stale


async def drop(s: AsyncSession, render_id: int) -> list[str]:
    """Delete one letter and its render (flushes, does not commit); returns its stored PDF path
    for the caller to remove after the commit. The application lets go of it first (foreign
    key)."""
    render = await s.get(DocRender, render_id)
    if render is None:
        return []
    await s.execute(update(Pursuit).where(Pursuit.cover_letter_id == render_id)
                    .values(cover_letter_id=None))
    await s.execute(delete(Letter).where(Letter.render_id == render_id))
    raw = render.pdf_path
    await s.delete(render)
    await s.flush()
    return [raw] if raw else []


async def _drop_all(s: AsyncSession, opp_id: int, status: str) -> list[str]:
    out: list[str] = []
    for rid in list((await s.execute(select(Letter.render_id).where(
        Letter.opportunity_id == opp_id, Letter.status == status
    ))).scalars()):
        out += await drop(s, rid)
    return out


async def _attach(s: AsyncSession, opp_id: int, render_id: int | None) -> None:
    pursuit = await _pursuit(s, opp_id)
    if pursuit is not None:
        pursuit.cover_letter_id = render_id


async def keep_new(s: AsyncSession, opp_id: int, render_id: int) -> list[str] | None:
    """Keep new draft: the proposal becomes current, the current one becomes the previous one
    (an older previous goes). None, and nothing changed, when it is no longer this role's
    proposal. Commits; returns the PDF paths to remove."""
    moved = await s.execute(
        update(Letter).where(Letter.render_id == render_id, Letter.opportunity_id == opp_id,
                             Letter.status == PROPOSED)
        .values(status="keeping", updated_at=utcnow_iso())
        .execution_options(synchronize_session=False)
    )
    if cast(CursorResult, moved).rowcount != 1:
        # Nothing matched, so nothing changed. No rollback: the caller's loaded rows stay usable.
        return None
    gone = await _drop_all(s, opp_id, PREVIOUS)
    await s.execute(
        update(Letter).where(Letter.opportunity_id == opp_id, Letter.status == CURRENT)
        .values(status=PREVIOUS).execution_options(synchronize_session=False)
    )
    await s.execute(
        update(Letter).where(Letter.render_id == render_id).values(status=CURRENT)
        .execution_options(synchronize_session=False)
    )
    await _attach(s, opp_id, render_id)
    await sync_story_usage(s, opp_id)
    await s.commit()
    return gone


async def keep_previous(s: AsyncSession, opp_id: int, render_id: int) -> list[str] | None:
    """Keep previous draft: the proposal is deleted and the current one stays as it is. None when
    it is no longer this role's proposal (one guarded write decides). Commits; returns the PDF
    paths to remove."""
    letter = await letter_for(s, render_id)
    if letter is None or letter.opportunity_id != opp_id:
        return None
    if not await _move(s, render_id, PROPOSED, "dropping"):
        return None
    gone = await drop(s, render_id)
    await s.commit()
    return gone


async def undo_keep(s: AsyncSession, opp_id: int, render_id: int) -> list[str] | None:
    """Undo after Keep new draft, for the current draft `render_id` the person confirmed: the
    previous letter is current again exactly as it was stored, and `render_id` (with any edits) is
    deleted. None, and nothing changed, when `render_id` is no longer this role's current draft or
    there is no previous one. Commits; returns the PDF paths to remove."""
    letter = await letter_for(s, render_id)
    prev = await render_in(s, opp_id, PREVIOUS)
    if letter is None or letter.opportunity_id != opp_id or prev is None:
        return None
    if not await _move(s, render_id, CURRENT, "undoing"):
        return None
    if not await _move(s, prev.id, PREVIOUS, CURRENT):
        await _move(s, render_id, "undoing", CURRENT)  # put it back as it was
        await s.commit()
        return None
    gone = await drop(s, render_id)
    await _attach(s, opp_id, prev.id)
    await sync_story_usage(s, opp_id)
    await s.commit()
    return gone


async def discard(s: AsyncSession, opp_id: int, render_id: int) -> list[str] | None:
    """Discard (after the sheet asked): this letter and the role's previous one go, so Undo has
    nothing older to bring back. None when it is not this role's letter. Commits; returns the PDF
    paths to remove."""
    letter = await letter_for(s, render_id)
    if letter is None or letter.opportunity_id != opp_id:
        return None
    was_current = letter.status == CURRENT
    gone = await drop(s, render_id)
    if was_current:
        gone += await _drop_all(s, opp_id, PREVIOUS)
    await sync_story_usage(s, opp_id)
    await s.commit()
    return gone


def _story_id(key: str) -> int | None:
    from kestrel.docs.letters import STORY_PREFIX

    raw = key[len(STORY_PREFIX):] if key.startswith(STORY_PREFIX) else ""
    return int(raw) if raw.isdigit() else None


async def sync_story_usage(s: AsyncSession, opp_id: int) -> None:
    """One `story_usage` row per accepted story the role's current letter really uses, and none for
    a story it no longer uses; `use_count` moves with the rows (D-LETTER-2). A story counts when
    the letter's claims name it, it was in the prompt the letter was drafted from (`sent_keys`),
    and, once the person has edited the text, a sentence of the text still traces to it. Flushes."""
    from kestrel.docs.letters import annotate, is_accepted_story, source_map, story_rows

    letters = list((await s.execute(select(Letter).where(
        Letter.opportunity_id == opp_id, Letter.status == CURRENT
    ))).scalars())
    per_letter: list[tuple[Letter, list[str]]] = []
    ids: set[int] = set()
    for letter in letters:
        sent = (letter.extras or {}).get("sent_keys")
        keys = [str(k) for k in letter.claims_used or [] if _story_id(str(k)) is not None
                and (sent is None or str(k) in sent)]
        per_letter.append((letter, keys))
        ids |= {i for i in (_story_id(k) for k in keys) if i is not None}
    stories = {st.id: st for st in (await s.execute(
        select(Story).where(Story.id.in_(ids))
    )).scalars()} if ids else {}
    wanted: set[int] = set()
    for letter, keys in per_letter:
        if letter.edited and keys:
            # The person's own text decides now: a story whose sentence they removed is not used.
            srcs = source_map([], [], story_rows(list(stories.values())))
            traced = {seg["key"] for para in annotate(letter.body_md or "", srcs, keys, [], [])
                      for seg in para if seg["kind"] == "claim"}
            keys = [k for k in keys if k in traced]
        wanted |= {i for i in (_story_id(k) for k in keys)
                   if i is not None and i in stories and is_accepted_story(stories[i])}
    have = {u.story_id: u for u in (await s.execute(select(StoryUsage).where(
        StoryUsage.opportunity_id == opp_id, StoryUsage.question == LETTER_USAGE
    ))).scalars()}
    for sid in wanted - set(have):
        s.add(StoryUsage(story_id=sid, opportunity_id=opp_id, question=LETTER_USAGE))
        await s.execute(update(Story).where(Story.id == sid).values(
            use_count=func.coalesce(Story.use_count, 0) + 1, updated_at=Story.updated_at))
    for sid in set(have) - wanted:
        await s.delete(have[sid])
        await s.execute(update(Story).where(Story.id == sid).values(
            use_count=func.max(func.coalesce(Story.use_count, 0) - 1, 0),
            updated_at=Story.updated_at))
    await s.flush()
