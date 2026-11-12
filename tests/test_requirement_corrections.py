"""Requirement corrections, the data layer (issue 170 step 1; SPEC-MODULES 5.1
`requirement_correction`, D-LEARN-3). Hash normalization, scope rules, live role counts, rescore
bookkeeping, story deletion, the Profile list, applying corrections to the role page's rows, and
invariants 3, 5 and 6 of the handoff. Invented text only; no network, no model."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.models import (
    Evaluation,
    Label,
    Opportunity,
    RequirementCorrection,
    Story,
    VerdictOverride,
)
from kestrel.interview.stories import delete_story
from kestrel.learning import corrections as corr
from kestrel.match_text import norm, requirement_hash
from kestrel.web import role_view as rv

REQ_SQL = "Writes careful SQL against a warehouse"
REQ_TEAM = "Leads a small planning team"
REQ_BADGE = "Holds a harbor crane license"


def _blocks(matches=(), gaps=()) -> dict:
    return {
        "b_fit": {
            "matches": [
                {"requirement": r, "strength": st, "evidence": ev} for r, st, ev in matches
            ],
            "gaps": [
                {"requirement": r, "severity": "material", "mitigation": "Ask."} for r in gaps
            ],
        }
    }


DEFAULT = _blocks(
    matches=[
        (REQ_SQL, "strong", "Built nightly reports in SQL."),
        (REQ_TEAM, "partial", "Led two."),
    ],
    gaps=[REQ_BADGE],
)


async def _role(s, *, track="employment", blocks=None, archived=False, title="Planner") -> int:
    o = Opportunity(
        track=track,
        opp_type="job_posting",
        title=title,
        org_name="Quillfeather Co",
        stage="t2_done",
        stage_reason="t2 consider",
        archived_at="2026-01-01T00:00:00.000000+00:00" if archived else None,
    )
    s.add(o)
    await s.flush()
    s.add(
        Evaluation(
            opportunity_id=o.id,
            tier=2,
            track=track,
            score=3.5,
            verdict="consider",
            blocks=DEFAULT if blocks is None else blocks,
        )
    )
    await s.commit()
    return o.id


async def _story(s, title="Ran the tide table rebuild") -> int:
    st = Story(
        title=title,
        action="Rebuilt the tide tables.",
        result="Saved a week.",
        is_canonical=True,
        operator_edited=True,
    )
    s.add(st)
    await s.commit()
    return st.id


async def _rows(s, opp_id: int) -> list[dict]:
    opp = await s.get(Opportunity, opp_id)
    ev = await corr._latest_evaluation(s, opp_id)
    return await corr.corrected_requirements(s, opp, ev, {"blocks": ev.blocks})


async def _table(s) -> list[dict]:
    rows = await s.execute(select(RequirementCorrection).order_by(RequirementCorrection.id))
    return [corr.snapshot(r) for r in rows.scalars()]


def _state(rows, req) -> str:
    return next(r["state"] for r in rows if r["text"] == req)


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    """Each write gets a later, distinct time through the module's own clock alias."""
    tick = iter(range(10, 99))
    monkeypatch.setattr(corr, "_now", lambda: f"2026-11-11T10:{next(tick):02d}:00.000000+00:00")


# ------------------------------------------------------------------------------- hash
def test_the_hash_ignores_case_spacing_and_punctuation_and_role_py_uses_the_same_norm():
    assert requirement_hash("Writes careful SQL, against a warehouse!") == requirement_hash(
        "  writes careful   sql against a WAREHOUSE "
    )
    assert requirement_hash(REQ_SQL) != requirement_hash(REQ_TEAM)
    assert norm("A/B  testing.") == "a b testing"
    from kestrel.web.pages import role

    assert role._norm is norm


CPP, CSHARP, PLAIN_C = "Proficiency in C++", "Proficiency in C#", "Proficiency in C"
CYR_A, CYR_B = "Опыт работы со складом", "Знание английского языка"


def test_the_hash_keeps_cpp_csharp_c_and_every_script_apart():
    """D-LEARN-11: C++, C# and C, and two different requirements written in Cyrillic, are four
    plus two identities, never one."""
    hashes = {requirement_hash(t) for t in (CPP, CSHARP, PLAIN_C)}
    assert len(hashes) == 3
    assert requirement_hash(CYR_A) != requirement_hash(CYR_B)
    assert requirement_hash("  proficiency IN c++ ") == requirement_hash(CPP)
    assert requirement_hash("Process-improvement_across DCs") == requirement_hash(
        "process improvement across dcs")


def test_a_cpp_correction_does_not_answer_the_csharp_requirement():
    rows = [{"text": CPP, "state": "gap"}, {"text": CSHARP, "state": "gap"}]

    class C:
        id, scope, opportunity_id, archived_at = 1, "person", 9, None
        reason, operator_state, kestrel_state = "all", "met", "gap"
        requirement_hash, story_id, story_deleted_at, agrees_now, note = (
            requirement_hash(CPP), None, None, False, None)
        basis, requirement_text = "none", CPP

    out = corr.apply_corrections(rows, [C()], opportunity_id=1)
    assert out[0]["state"] == "met" and out[1]["state"] == "gap"
    assert "correction" not in out[1]


async def test_a_requirement_with_nothing_to_tell_apart_is_refused(sm):
    async with sm() as s:
        a = await _role(s)
        with pytest.raises(corr.CorrectionError):
            await corr.save_correction(s, opportunity_id=a, requirement_text="-- !! --",
                                       kestrel_state="gap", reason="none")


def test_migration_0034_re_derives_every_stored_hash_and_downgrades(tmp_path):
    import hashlib

    db = tmp_path / "m.db"
    _go(db, "up", "0033")
    old = hashlib.sha1(norm(CPP).encode(), usedforsecurity=False).hexdigest()
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO requirement_correction (track, requirement_text, requirement_hash, "
        "kestrel_state, operator_state, reason, scope, basis, agrees_now, created_at, "
        "updated_at) VALUES ('employment', ?, ?, 'gap', 'met', 'all', 'person', 'none', 0, "
        "'2026-01-01', '2026-01-01')", (CPP, old))
    con.commit()
    con.close()
    _go(db, "up", "0034")
    con = sqlite3.connect(db)
    got = con.execute("SELECT requirement_hash FROM requirement_correction").fetchone()[0]
    con.close()
    assert got == requirement_hash(CPP) != old
    _go(db, "down", "0033")
    con = sqlite3.connect(db)
    got = con.execute("SELECT requirement_hash FROM requirement_correction").fetchone()[0]
    con.close()
    assert got == old


# ------------------------------------------------------------------------------- migration
def _go(db: Path, direction: str, rev: str) -> None:
    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        cfg = migrate._config(str(mig_dir), db)
        (command.upgrade if direction == "up" else command.downgrade)(cfg, rev)


def test_migration_0032_creates_the_table_and_indexes_and_downgrades(tmp_path):
    db = tmp_path / "m.db"
    _go(db, "up", "0032")
    con = sqlite3.connect(db)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    con.close()
    assert {
        "requirement_correction",
        "ux_requirement_correction_person",
        "ux_requirement_correction_posting",
        "ix_requirement_correction_track_hash",
    } <= names
    _go(db, "down", "0031")
    con = sqlite3.connect(db)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    con.close()
    assert not any("requirement_correction" in n for n in names)
    _go(db, "up", "head")
    assert migrate.head_revision() == "0034"


def test_migration_0033_adds_the_correction_ids_and_downgrades(tmp_path):
    db = tmp_path / "m.db"
    _go(db, "up", "0033")
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(evaluation)")}
    con.close()
    assert "correction_ids" in cols
    _go(db, "down", "0032")
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(evaluation)")}
    con.close()
    assert "correction_ids" not in cols


# ------------------------------------------------------------------------------- scope rules
async def test_one_person_row_per_track_and_hash_and_a_newer_answer_replaces_it(sm):
    async with sm() as s:
        a = await _role(s)
        b = await _role(s, title="Second planner")
        c = await _role(
            s,
            track="consulting",
            blocks={
                "b_thesis": {
                    "matches": [{"requirement": REQ_SQL, "strength": "strong", "evidence": "x"}]
                }
            },
        )
        first, _ = await corr.save_correction(
            s, opportunity_id=a, requirement_text=REQ_SQL, kestrel_state="met", reason="not_me"
        )
        created = first.created_at
        second, _ = await corr.save_correction(
            s,
            opportunity_id=b,
            requirement_text=" writes careful sql, against a warehouse ",
            kestrel_state="met",
            reason="overstated",
            note="Only read-only queries.",
        )
        assert second.id == first.id and second.created_at == created
        assert (second.reason, second.operator_state, second.opportunity_id) == (
            "overstated",
            "part",
            b,
        )
        assert second.updated_at > created
        other, _ = await corr.save_correction(
            s, opportunity_id=c, requirement_text=REQ_SQL, kestrel_state="met", reason="not_me"
        )
        assert other.id != first.id
        n = (await s.execute(select(func.count()).select_from(RequirementCorrection))).scalar()
        assert n == 2
        s.add(
            RequirementCorrection(
                track="employment",
                opportunity_id=a,
                requirement_text=REQ_SQL,
                requirement_hash=first.requirement_hash,
                kestrel_state="met",
                operator_state="gap",
                reason="not_me",
                scope="person",
                basis="none",
                agrees_now=False,
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_misread_is_a_posting_note_needing_text_that_wins_on_its_role_only(sm):
    async with sm() as s:
        a = await _role(s)
        b = await _role(s, title="Second planner")
        with pytest.raises(corr.CorrectionError):
            await corr.save_correction(
                s,
                opportunity_id=a,
                requirement_text=REQ_SQL,
                kestrel_state="met",
                reason="misread",
                note="  ",
            )
        await corr.save_correction(
            s, opportunity_id=b, requirement_text=REQ_SQL, kestrel_state="met", reason="not_me"
        )
        note, _ = await corr.save_correction(
            s,
            opportunity_id=a,
            requirement_text=REQ_SQL,
            kestrel_state="met",
            reason="misread",
            note="They mean reading dashboards.",
        )
        assert (note.scope, note.operator_state) == ("posting", "met")
        rows_a, rows_b = await _rows(s, a), await _rows(s, b)
    hit_a = next(r for r in rows_a if r["text"] == REQ_SQL)
    assert hit_a["state"] == "met" and hit_a["correction"]["reason"] == "misread"
    assert hit_a["correction"]["applies"] is False
    assert _state(rows_b, REQ_SQL) == "gap"


async def test_a_person_answer_on_a_role_replaces_that_roles_note_and_undo_brings_both_back(sm):
    async with sm() as s:
        a = await _role(s)
        await corr.save_correction(
            s,
            opportunity_id=a,
            requirement_text=REQ_SQL,
            kestrel_state="met",
            reason="misread",
            note="Dashboards only.",
        )
        before = await _table(s)
        _, undo = await corr.save_correction(
            s, opportunity_id=a, requirement_text=REQ_SQL, kestrel_state="met", reason="not_me"
        )
        assert [r["scope"] for r in await _table(s)] == ["person"]
        await corr.undo_correction(s, corr.Undo.from_json(json.loads(json.dumps(undo.to_json()))))
        assert await _table(s) == before


async def test_bad_reasons_states_and_roles_are_refused(sm):
    async with sm() as s:
        a = await _role(s)
        for kw in (
            {"reason": "maybe"},
            {"kestrel_state": "unsure"},
            {"basis": "guess"},
            {"opportunity_id": 999},
            {"story_id": 999},
            {"requirement_text": "  "},
        ):
            args = {
                "opportunity_id": a,
                "requirement_text": REQ_SQL,
                "kestrel_state": "met",
                "reason": "not_me",
                **kw,
            }
            with pytest.raises(corr.CorrectionError):
                await corr.save_correction(s, **args)


# ------------------------------------------------------------------------------- apply to rows
async def test_corrections_change_the_rows_and_wheel_and_mark_ones_made_elsewhere(sm):
    async with sm() as s:
        a = await _role(s)
        b = await _role(s, title="Second planner")
        story = await _story(s)
        plain = await _rows(s, b)
        await corr.save_correction(
            s,
            opportunity_id=a,
            requirement_text=REQ_BADGE,
            kestrel_state="gap",
            reason="all",
            story_id=story,
        )
        await corr.save_correction(
            s, opportunity_id=a, requirement_text=REQ_SQL, kestrel_state="met", reason="overstated"
        )
        rows_a, rows_b = await _rows(s, a), await _rows(s, b)
    assert rv.wheel(plain)["center"] == "1 of 3"
    for rows, elsewhere in ((rows_a, False), (rows_b, True)):
        assert _state(rows, REQ_BADGE) == "met" and _state(rows, REQ_SQL) == "part"
        badge = next(r for r in rows if r["text"] == REQ_BADGE)
        assert badge["mark"] == "+" and badge["word"] == "Met"
        assert badge["correction"]["kestrel_state"] == "gap"
        assert badge["correction"]["from_elsewhere"] is elsewhere
        assert badge["correction"]["story_id"] == story
        assert [r["k"] for r in rows] == [0, 1, 2]
        w = rv.wheel(rows)
        assert (w["met"], w["part"], w["gap"], w["center"]) == (1, 2, 0, "1 of 3")
    assert "correction" not in next(r for r in rows_a if r["text"] == REQ_TEAM)


def test_apply_leaves_the_input_rows_alone_and_hashes_the_unredacted_text():
    class C:
        id, reason, note, scope, story_id = 7, "not_me", None, "person", None
        operator_state, opportunity_id, agrees_now = "gap", 1, False
        archived_at = story_deleted_at = None
        requirement_hash = requirement_hash(REQ_SQL)

    rows = [
        {
            "k": 0,
            "text": "Writes careful SQL against a [current employer]",
            "state": "met",
            "mark": "+",
            "word": "Met",
            "evidence": "e",
            "how": "",
        }
    ]
    original = json.dumps(rows)
    assert "correction" not in corr.apply_corrections(rows, [C()], opportunity_id=1)[0]
    out = corr.apply_corrections(rows, [C()], opportunity_id=1, texts=[REQ_SQL])
    assert out[0]["state"] == "gap" and out[0]["correction"]["id"] == 7
    assert json.dumps(rows) == original


async def test_consulting_thesis_and_delivery_rows_take_corrections(sm):
    blocks = {
        "b_thesis": {
            "matches": [
                {
                    "requirement": "Inventory model audits",
                    "strength": "partial",
                    "evidence": "Ran two audits.",
                }
            ]
        },
        "f_delivery": {
            "solo_feasible": True,
            "effort_shape": "Six weeks.",
            "gaps": ["Needs a licensed surveyor"],
            "partner_needed": "a surveyor",
        },
    }
    async with sm() as s:
        lead = await _role(s, track="consulting", blocks=blocks, title="Audit lead")
        before = await _rows(s, lead)
        assert [r["text"] for r in before] == [
            "Inventory model audits",
            "Can be delivered on your own",
            "Needs a licensed surveyor",
            "A partner is needed: a surveyor",
        ]
        story = await _story(s)
        for text, state, reason, kw in (
            ("Inventory model audits", "part", "all", {"story_id": story}),
            ("Can be delivered on your own", "met", "not_me", {}),
            ("Needs a licensed surveyor", "gap", "some", {}),
            ("A partner is needed: a surveyor", "gap", "agree", {}),
        ):
            await corr.save_correction(
                s,
                opportunity_id=lead,
                requirement_text=text,
                kestrel_state=state,
                reason=reason,
                **kw,
            )
        after = await _rows(s, lead)
    assert [r["state"] for r in after] == ["met", "gap", "part", "gap"]
    assert all("correction" in r for r in after)


def test_the_role_page_wheel_uses_the_corrected_states():
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel.db.engine import make_async_engine
    from kestrel.web.app import build_app
    from tests import role_seed

    ids = asyncio.run(role_seed.seed_async())

    async def save():
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            await corr.save_correction(
                s,
                opportunity_id=ids["strong"],
                requirement_text="Six Sigma Green Belt preferred",
                kestrel_state="gap",
                reason="some",
            )
        await engine.dispose()

    with TestClient(build_app(worker=False)) as c:
        html = c.get(f"/opportunities/{ids['strong']}").text
        assert 'data-testid="wheel-center">2 of 5<' in html and "2 not met" in html
        asyncio.run(save())
        html = c.get(f"/opportunities/{ids['strong']}").text
    assert 'data-testid="wheel-center">2 of 5<' in html and "1 not met" in html


# ------------------------------------------------------------------------------- counts on N roles
async def test_counts_on_n_roles_is_live_open_roles_on_the_track_by_latest_read(sm):
    async with sm() as s:
        a = await _role(s)
        await _role(s, title="Open twin")
        await _role(s, archived=True, title="Archived twin")
        await _role(
            s,
            track="consulting",
            blocks={
                "b_thesis": {
                    "matches": [{"requirement": REQ_SQL, "strength": "strong", "evidence": "x"}]
                }
            },
        )
        moved = await _role(s, title="Read changed")
        s.add(
            Evaluation(
                opportunity_id=moved,
                tier=2,
                track="employment",
                blocks=_blocks(gaps=["Something else entirely"]),
            )
        )
        await s.commit()
        h = requirement_hash(REQ_SQL)
        assert await corr.count_roles(s, "employment", h) == 2
        assert (await corr.roles_using(s, "employment", [h]))[h] == [a, a + 1]
        assert await corr.count_roles(s, "consulting", h) == 1
        assert await corr.roles_using(s, "employment", []) == {}


# ------------------------------------------------------------------------------- rescore
async def test_a_rescore_sets_agrees_now_keeps_the_operators_state_and_archives_a_gone_note(sm):
    async with sm() as s:
        a = await _role(s)
        person, _ = await corr.save_correction(
            s, opportunity_id=a, requirement_text=REQ_SQL, kestrel_state="met", reason="overstated"
        )
        note, _ = await corr.save_correction(
            s,
            opportunity_id=a,
            requirement_text=REQ_TEAM,
            kestrel_state="part",
            reason="misread",
            note="They mean shift leads.",
        )
        # The new read disagrees on SQL (still strong) and drops the team requirement.
        s.add(
            Evaluation(
                opportunity_id=a,
                tier=2,
                track="employment",
                blocks=_blocks(matches=[(REQ_SQL, "strong", "x")], gaps=[REQ_BADGE]),
            )
        )
        await corr.after_evaluation(s, a)
        await s.commit()
        await s.refresh(person)
        await s.refresh(note)
        assert person.agrees_now is False and person.archived_at is None
        assert note.archived_at is not None
        assert _state(await _rows(s, a), REQ_SQL) == "part"
        # A later read agrees; then one without the requirement never archives a person row.
        s.add(
            Evaluation(
                opportunity_id=a,
                tier=2,
                track="employment",
                blocks=_blocks(matches=[(REQ_SQL, "partial", "x")]),
            )
        )
        await corr.after_evaluation(s, a)
        await s.commit()
        await s.refresh(person)
        assert person.agrees_now is True
        s.add(Evaluation(opportunity_id=a, tier=2, track="employment", blocks=_blocks()))
        await corr.after_evaluation(s, a)
        await s.commit()
        await s.refresh(person)
        assert person.archived_at is None and person.agrees_now is True
        n = (await s.execute(select(func.count()).select_from(RequirementCorrection))).scalar()
        assert n == 2  # archived, never deleted


# ------------------------------------------------------------------------------- story deletion
async def test_deleting_the_story_stops_an_all_or_some_answer_and_profile_says_so(sm):
    async with sm() as s:
        a = await _role(s)
        story = await _story(s)
        row, _ = await corr.save_correction(
            s,
            opportunity_id=a,
            requirement_text=REQ_BADGE,
            kestrel_state="gap",
            reason="all",
            story_id=story,
        )
        free, _ = await corr.save_correction(
            s, opportunity_id=a, requirement_text=REQ_TEAM, kestrel_state="part", reason="none"
        )
        assert _state(await _rows(s, a), REQ_BADGE) == "met"
        assert await delete_story(s, story)
        await s.refresh(row)
        await s.refresh(free)
        assert row.story_id is None and row.story_deleted_at is not None
        assert free.story_deleted_at is None
        rows = await _rows(s, a)
        hit = next(r for r in rows if r["text"] == REQ_BADGE)
        assert hit["state"] == "gap" and hit["correction"]["story_deleted"] is True
        assert _state(rows, REQ_TEAM) == "gap"
        items = {i.correction.id: i for i in await corr.list_for_profile(s)}
        assert items[row.id].story_deleted is True and items[free.id].story_deleted is False


async def test_undoing_a_dismissed_draft_gives_the_answer_its_story_back(sm):
    from kestrel.interview.stories import dismiss_draft, undo_dismissal

    async with sm() as s:
        a = await _role(s)
        st = Story(title="Drafted tide table story", action="Rebuilt tables.", result="Saved.",
                   is_canonical=True, operator_edited=False)
        s.add(st)
        await s.commit()
        row, _ = await corr.save_correction(
            s, opportunity_id=a, requirement_text=REQ_BADGE, kestrel_state="gap", reason="all",
            story_id=st.id)
        other, _ = await corr.save_correction(
            s, opportunity_id=a, requirement_text=REQ_TEAM, kestrel_state="part", reason="none")
        assert await dismiss_draft(s, st.id, undo_token="tok") is True
        await s.refresh(row)
        assert row.story_id is None and row.story_deleted_at is not None
        assert _state(await _rows(s, a), REQ_BADGE) == "gap"
        assert await undo_dismissal(s, "tok") is True
        await s.refresh(row)
        await s.refresh(other)
        assert row.story_id == st.id and row.story_deleted_at is None
        assert other.story_id is None and other.story_deleted_at is None
        assert _state(await _rows(s, a), REQ_BADGE) == "met"
        items = {i.correction.id: i for i in await corr.list_for_profile(s)}
        assert items[row.id].story_deleted is False and items[row.id].story_title == st.title


async def test_an_answer_that_moved_on_is_not_relinked_by_a_draft_undo(sm):
    from kestrel.interview.stories import dismiss_draft, undo_dismissal

    async with sm() as s:
        a = await _role(s)
        st = Story(title="Drafted tide table story", action="Rebuilt tables.", result="Saved.",
                   is_canonical=True, operator_edited=False)
        s.add(st)
        await s.commit()
        row, _ = await corr.save_correction(
            s, opportunity_id=a, requirement_text=REQ_BADGE, kestrel_state="gap", reason="all",
            story_id=st.id)
        await dismiss_draft(s, st.id, undo_token="tok2")
        await s.refresh(row)  # the delete unlinked it in the database
        await corr.save_correction(  # the person answered again, with no story
            s, opportunity_id=a, requirement_text=REQ_BADGE, kestrel_state="gap", reason="none")
        assert await undo_dismissal(s, "tok2") is True
        await s.refresh(row)
        assert row.story_id is None and row.reason == "none" and row.story_deleted_at is None


async def test_some_of_it_without_a_story_still_applies(sm):
    async with sm() as s:
        a = await _role(s)
        await corr.save_correction(
            s, opportunity_id=a, requirement_text=REQ_BADGE, kestrel_state="gap", reason="some"
        )
        assert _state(await _rows(s, a), REQ_BADGE) == "part"


# ------------------------------------------------------------------------------- profile list
async def test_profile_lists_person_rows_newest_first_and_search_covers_every_field(sm):
    async with sm() as s:
        a = await _role(s, title="Harbor planner")
        story = await _story(s, title="Crane certification drive")
        r1, _ = await corr.save_correction(
            s,
            opportunity_id=a,
            requirement_text=REQ_SQL,
            kestrel_state="met",
            reason="overstated",
            note="Only read-only queries.",
        )
        r2, _ = await corr.save_correction(
            s,
            opportunity_id=a,
            requirement_text=REQ_BADGE,
            kestrel_state="gap",
            reason="all",
            story_id=story,
        )
        await corr.save_correction(
            s,
            opportunity_id=a,
            requirement_text=REQ_TEAM,
            kestrel_state="part",
            reason="misread",
            note="Shift leads.",
        )
        items = await corr.list_for_profile(s)
        assert [i.correction.id for i in items] == [r2.id, r1.id]
        assert items[0].story_title == "Crane certification drive"
        assert items[0].role_title == "Harbor planner" and items[0].counts_on == 1
        for q, want in (
            ("crane license", [r2.id]),
            ("READ-ONLY", [r1.id]),
            ("overstated", [r1.id]),
            ("all of it", [r2.id]),
            ("harbor planner", [r2.id, r1.id]),
            ("nothing like this", []),
        ):
            assert [i.correction.id for i in await corr.list_for_profile(s, query=q)] == want
        assert await corr.list_for_profile(s, track="consulting") == []


# ------------------------------------------------------------------------------- invariant 5
async def test_invariant_5_undo_and_restore_return_the_exact_prior_state(sm):
    async with sm() as s:
        a = await _role(s)
        story = await _story(s)
        start_table, start_rows = await _table(s), await _rows(s, a)
        _, undo = await corr.save_correction(
            s, opportunity_id=a, requirement_text=REQ_SQL, kestrel_state="met", reason="not_me"
        )
        await corr.undo_correction(s, undo)
        assert await _table(s) == start_table and await _rows(s, a) == start_rows

        await corr.save_correction(
            s,
            opportunity_id=a,
            requirement_text=REQ_BADGE,
            kestrel_state="gap",
            reason="all",
            story_id=story,
            note="From the crane yard.",
        )
        t1, r1 = await _table(s), await _rows(s, a)
        _, undo = await corr.save_correction(
            s, opportunity_id=a, requirement_text=REQ_BADGE, kestrel_state="gap", reason="none"
        )
        assert await _table(s) != t1
        await corr.undo_correction(s, undo)
        assert await _table(s) == t1 and await _rows(s, a) == r1

        cid = t1[0]["id"]
        _, undo = await corr.change_correction(s, cid, operator_state="part", note="Half of it.")
        await corr.undo_correction(s, undo)
        assert await _table(s) == t1 and await _rows(s, a) == r1

        undo = await corr.remove_correction(s, cid)
        assert await _table(s) == [] and await _rows(s, a) == start_rows
        await corr.restore_correction(s, undo)
        assert await _table(s) == t1 and await _rows(s, a) == r1
        assert await corr.remove_correction(s, 9999) is None
        assert await corr.change_correction(s, 9999, operator_state="met", note=None) is None


# ------------------------------------------------------------------------------- invariant 6
async def test_invariant_6_the_evaluation_is_byte_identical_after_every_operation(sm):
    async with sm() as s:
        a = await _role(s)
        story = await _story(s)

        async def stored():
            return (await s.execute(text("SELECT * FROM evaluation ORDER BY id"))).all()

        before = await stored()
        _, u1 = await corr.save_correction(
            s,
            opportunity_id=a,
            requirement_text=REQ_BADGE,
            kestrel_state="gap",
            reason="all",
            story_id=story,
        )
        _, u2 = await corr.save_correction(
            s,
            opportunity_id=a,
            requirement_text=REQ_TEAM,
            kestrel_state="part",
            reason="misread",
            note="Leads.",
        )
        await _rows(s, a)
        await corr.list_for_profile(s, query="crane")
        res = await corr.change_correction(s, u1.written[0], operator_state="part", note=None)
        await corr.undo_correction(s, res[1])
        await corr.after_evaluation(s, a)
        await s.commit()
        await corr.restore_correction(s, await corr.remove_correction(s, u2.written[0]))
        await delete_story(s, story)
        await _rows(s, a)
        assert await stored() == before


# ------------------------------------------------------------------------------- invariant 3
async def test_invariant_3_a_correction_never_archives_rejects_or_changes_a_keep_or_kill(sm):
    async with sm() as s:
        a = await _role(s)
        b = await _role(s, title="Second planner")

        async def roles():
            return (
                await s.execute(
                    text(
                        "SELECT id, stage, stage_reason, archived_at, archive_reason, pre_score "
                        "FROM opportunity ORDER BY id"
                    )
                )
            ).all()

        before = await roles()
        for reason, state, req in (
            ("not_me", "met", REQ_SQL),
            ("none", "part", REQ_TEAM),
            ("agree", "gap", REQ_BADGE),
        ):
            await corr.save_correction(
                s, opportunity_id=a, requirement_text=req, kestrel_state=state, reason=reason
            )
        rows_b = await _rows(s, b)
        assert all(r["state"] == "gap" for r in rows_b)
        await corr.after_evaluation(s, a)
        await corr.after_evaluation(s, b)
        await s.commit()
        assert await roles() == before
        # Not label rows: nothing a correction does feeds the learning thresholds or history.
        for model in (Label, VerdictOverride):
            assert (await s.execute(select(func.count()).select_from(model))).scalar() == 0


def test_invariant_3_tier_0_and_tier_1_never_read_corrections():
    for mod in ("kestrel/pipeline/tier0.py", "kestrel/pipeline/tier1.py", "kestrel/rules"):
        p = Path(mod)
        files = [p] if p.is_file() else list(p.rglob("*.py"))
        assert files
        for f in files:
            src = f.read_text(encoding="utf-8")
            assert "corrections" not in src and "RequirementCorrection" not in src, f
