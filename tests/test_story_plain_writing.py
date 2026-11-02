"""Issue #93, D-UI-232: the plain-writing and grounding checks on story consolidation (P8), the
style_flags migration (0026), and the marks on the story sheet. Offline: the client is a fake."""

from __future__ import annotations

import json
import sqlite3

from alembic import command
from sqlalchemy import select

from kestrel import paths
from kestrel.config import get_settings
from kestrel.db import migrate
from kestrel.db.models import Story
from kestrel.interview.stories import consolidate_stories
from kestrel.llm.client import ChatResult
from tests.test_interview_stories import _P8, _add_eval, _seed_opp, _sketch


def _reply(**over) -> dict:
    # Every number and name here is in the "delta" sketches below, so grounding is clean.
    base = {
        **_P8,
        "situation": "The delta platform was in trouble.",
        "task": "Fix delta.",
        "action": "I led the delta turnaround.",
        "result": "delta recovered.",
        "reflection": "Test earlier.",
    }
    return {**base, **over}


class _Scripted:
    """Fake client: embeddings put everything in one cluster; P8 replies come from a script."""

    def __init__(self, *replies: dict) -> None:
        self.settings = get_settings()
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def embed(self, texts, *, tier: str = "embed", timeout: float = 60.0):
        return [[1.0, 0.0, 0.0] for _ in texts]

    async def complete(self, *, tier, messages, schema=None, schema_name="response", **_):
        self.calls.append(messages)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return ChatResult(
            content=json.dumps(reply), prompt_tokens=1, completion_tokens=1, latency_ms=1,
            model=self.settings.llm.tiers[tier].model,
        )


async def _build(sm, client):
    opp = await _seed_opp(sm)
    await _add_eval(sm, opp, [_sketch("Tell me about a turnaround.", "delta")])
    async with sm() as s:
        await consolidate_stories(s, client, get_settings())
    async with sm() as s:
        return list((await s.execute(select(Story))).scalars().all())


async def test_a_clean_story_is_saved_with_no_marks_and_no_reask(sm):
    client = _Scripted(_reply())
    (story,) = await _build(sm, client)
    assert len(client.calls) == 1 and story.style_flags == []


async def test_an_em_dash_is_reasked_once_and_the_fixed_text_is_kept(sm):
    client = _Scripted(_reply(action="I led it — fast."), _reply())
    (story,) = await _build(sm, client)
    assert len(client.calls) == 2
    assert story.action == "I led the delta turnaround." and story.style_flags == []
    assert "action" in client.calls[1][-1]["content"]


async def test_a_text_that_still_fails_is_kept_as_written_with_a_mark(sm):
    bad = _reply(action="I led a robust delta turnaround.")
    client = _Scripted(bad)
    (story,) = await _build(sm, client)
    assert len(client.calls) == 2  # the first reply and one re-ask, never more
    assert story.action == "I led a robust delta turnaround."  # not rewritten in code
    assert {"kind": "style", "rule": "banned_word", "field": "action", "phrase": "robust"} \
        in story.style_flags


async def test_an_invented_number_is_marked_ungrounded(sm):
    client = _Scripted(_reply(result="Error rate fell 87 percent."))
    (story,) = await _build(sm, client)
    assert {"kind": "ungrounded", "field": "result", "phrase": "87 percent"} in story.style_flags


async def test_model_declared_gaps_and_questions_become_marks(sm):
    client = _Scripted(_reply(
        uncertain_details=["What did the owners push back on?"],
        unsupported_claims=["Everyone agreed."],
    ))
    (story,) = await _build(sm, client)
    kinds = {(f["kind"], f["phrase"]) for f in story.style_flags}
    assert ("question", "What did the owners push back on?") in kinds
    assert ("unsupported", "Everyone agreed.") in kinds


async def test_saving_an_edit_clears_the_marks_and_keeps_the_words(sm):
    from starlette.datastructures import FormData

    from kestrel.web.pages.stories import _apply_edit

    (story,) = await _build(sm, _Scripted(_reply(action="A robust turnaround.")))
    assert story.style_flags
    async with sm() as s:
        row = await s.get(Story, story.id)
        await _apply_edit(s, row, FormData([("action", "A robust turnaround, mine.")]))
    async with sm() as s:
        row = await s.get(Story, story.id)
    assert row.style_flags == [] and row.action == "A robust turnaround, mine."


# ------------------------------------------------------------------------------- the migration
def _go(db, direction: str, rev: str) -> None:
    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        cfg = migrate._config(str(mig_dir), db)
        (command.upgrade if direction == "up" else command.downgrade)(cfg, rev)


def _cols(db) -> set[str]:
    con = sqlite3.connect(db)
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(story)")}
    finally:
        con.close()


def test_migration_adds_style_flags_keeps_rows_and_reverses(tmp_path):
    db = tmp_path / "m.db"
    _go(db, "up", "0025")
    assert "style_flags" not in _cols(db)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO story (title, use_count, is_canonical, operator_edited, themes,"
                " created_at, updated_at) VALUES ('Keep me', 0, 1, 0, '[]', 'x', 'x')")
    con.commit()
    con.close()
    _go(db, "up", "0026")
    assert "style_flags" in _cols(db)
    con = sqlite3.connect(db)
    assert con.execute("SELECT title, style_flags FROM story").fetchall() == [("Keep me", "[]")]
    assert con.execute("SELECT version_num FROM alembic_version").fetchone() == ("0026",)
    con.close()
    _go(db, "down", "0025")
    assert "style_flags" not in _cols(db)
    con = sqlite3.connect(db)
    assert con.execute("SELECT title FROM story").fetchall() == [("Keep me",)]
    con.close()
    _go(db, "up", "0026")  # and up again
    assert "style_flags" in _cols(db)


# ------------------------------------------------------------------------------- the sheet marks
def _render(story: Story) -> str:
    from kestrel.web.deps import templates

    tpl = templates().env.get_template("partials/pp_story_sheet.html")
    return tpl.render(story=story, label="Merge: x", theme="", qh="", opp="", **{"from": "bank"})


def _draft(flags, edited=False) -> Story:
    return Story(
        id=1, title="T", situation="A robust start.", action="We acted.", result="Saved $9M.",
        operator_edited=edited, is_canonical=True, style_flags=flags,
    )


_FLAGS = [
    {"kind": "style", "rule": "banned_word", "field": "situation", "phrase": "robust"},
    {"kind": "ungrounded", "field": "result", "phrase": "$9M"},
    {"kind": "question", "field": "", "phrase": "What pushback did you get?"},
]


def test_sheet_shows_marks_on_a_draft_and_the_no_rewrite_note():
    html = _render(_draft(_FLAGS))
    assert html.count('data-testid="pp-style-mark"') == 3
    assert "robust" in html and "$9M" in html and "What pushback did you get?" in html
    assert "Kestrel will not rewrite your story" in html


def test_sheet_shows_no_marks_on_the_persons_own_story_or_a_clean_draft():
    assert 'data-testid="pp-style-mark"' not in _render(_draft(_FLAGS, edited=True))
    assert 'data-testid="pp-style-mark"' not in _render(_draft([]))


async def test_the_persons_own_note_words_quoted_in_a_draft_are_never_judged(sm):
    from kestrel.interview.stories import Sketch, _merge_cluster
    from kestrel.llm.prompts import load_prompt

    own = "I ran a robust review with the site leads"
    note = Sketch(
        id=10**12 + 1, evaluation_id=0, opportunity_id=1, created_at="x", question="",
        situation=own + ".", task="", action="", result="", reflection="",
        proof_point_key=None, kind="note",
    )
    client = _Scripted(_reply(action=f"{own}. Then I wrote it up."))
    opp = await _seed_opp(sm)
    note.opportunity_id = opp
    async with sm() as s:
        story = await _merge_cluster(s, client, load_prompt("story_consolidate/v2"), [note], [])
        await s.commit()
    assert len(client.calls) == 1  # no re-ask: the robust span is the person's own text
    assert story.style_flags == []


# ------------------------------------------------------- the re-ask is best effort (amendment)
async def _quarantines(sm) -> int:
    from kestrel.db.models import Quarantine

    async with sm() as s:
        return len((await s.execute(select(Quarantine))).scalars().all())


async def test_a_reask_that_hits_quarantine_leaves_no_row_and_keeps_the_original(sm):
    bad = _reply(action="I led a robust delta turnaround.")
    # The re-ask fails its schema, and so does the guard's own repair.
    client = _Scripted(bad, {"not": "a story"})
    (story,) = await _build(sm, client)
    assert await _quarantines(sm) == 0
    assert story.action == "I led a robust delta turnaround."
    assert any(f["phrase"] == "robust" for f in story.style_flags)


async def test_the_first_reply_failing_still_quarantines(sm):
    from kestrel.db.models import Quarantine

    opp = await _seed_opp(sm)
    await _add_eval(sm, opp, [_sketch("Tell me about a turnaround.", "delta")])
    async with sm() as s:
        await consolidate_stories(s, _Scripted({"not": "a story"}), get_settings())
    async with sm() as s:
        assert len((await s.execute(select(Quarantine))).scalars().all()) == 1


async def test_only_the_flagged_prose_fields_are_taken_from_the_reask(sm):
    bad = _reply(action="I led a robust delta turnaround.")
    other = _reply(
        action="I led the delta turnaround.", title="A different title",
        themes=["crisis"], situation="Changed situation.", spoken_seconds_estimate=5,
    )
    (story,) = await _build(sm, _Scripted(bad, other))
    assert story.action == "I led the delta turnaround."  # the flagged field, from the re-ask
    assert story.title == _P8["title"]  # everything else stays as first validated
    assert story.themes == ["turnaround", "technical_judgment"]
    assert story.situation == "The delta platform was in trouble."


async def test_a_refusal_like_reask_is_not_saved(sm):
    bad = _reply(action="I led a robust delta turnaround.")
    refusal = _reply(action="Not available from your profile.")
    (story,) = await _build(sm, _Scripted(bad, refusal))
    assert story.action == "I led a robust delta turnaround."  # the original, not the refusal
    assert "Not available" not in (story.action or "")


async def test_mock_critique_exempts_the_persons_answer_quoted_in_the_revised_opening(sm):
    """A clause of the person's own answer repeated in revised_opening is theirs, not the
    model's: no re-ask, no flag (D-UI-236 amendment)."""
    from kestrel.db.models import MockSession  # noqa: F401
    from kestrel.interview.mock import start_session, submit_answer
    from tests.test_interview_mock import _P9, _PROOF_POINTS

    opp = await _seed_opp(sm)
    async with sm() as s:
        ms_id = (await start_session(s, opportunity_id=opp, mode="behavioral")).id
    answer = "I ran a robust review with the site leads, then wrote it up."
    reply = {**_P9, "revised_opening": "I ran a robust review with the site leads and led it."}
    client = _Scripted(reply)
    async with sm() as s:
        turn = await submit_answer(s, client, ms_id, answer, proof_points=_PROOF_POINTS, stories=[])
    assert len(client.calls) == 1 and "style_flags" not in turn
