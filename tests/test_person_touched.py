"""`kestrel.services.person_touched`: the one place that answers "has the person done something
with this role?" (issue #250, D-SCORE-919).

Two guards against the accident that already happened twice (a mark of the person's that the
recheck did not know): every table that points at a role, and every dotted settings key prefix in
the code, must be classified in that module as the person's or not. And each of the person's
marks, one at a time, must stop the legacy lane recheck from restaging the role. All text is
invented; no model and no network."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import insert

import kestrel
from kestrel.db.models import Base, Opportunity, Setting
from kestrel.pipeline import lane
from kestrel.services import person_touched as pt

pytestmark = pytest.mark.usefixtures("example_operator_profile")


def _points_at_a_role(table) -> bool:
    return table.name != "opportunity" and any(
        c.name == "opportunity_id" or any(fk.column.table.name == "opportunity"
                                          for fk in c.foreign_keys)
        for c in table.columns)


# ------------------------------------------------------------------ nothing is left unclassified
def test_every_table_that_points_at_a_role_is_classified():
    found = {t.name for t in Base.metadata.sorted_tables if _points_at_a_role(t)}
    person = {m.__tablename__ for m in pt.PERSON_TABLES}
    machine = {m.__tablename__ for m in pt.MACHINE_TABLES}
    assert not person & machine
    assert found == person | machine, (
        "a table points at a role and kestrel.services.person_touched does not say whether a "
        f"row in it is the person's: {sorted(found ^ (person | machine))}")


def test_every_dotted_settings_prefix_is_classified():
    """Every `...PREFIX = "some.dotted."` constant in the code is a family of settings keys. A
    new one keyed by a role id must be listed as the person's mark, or as not one."""
    pattern = re.compile(r'^_?[A-Z_]*PREFIX\s*=\s*"([a-z0-9_]+(?:\.[a-z0-9_]+)*\.)"', re.M)
    found: set[str] = set()
    for path in Path(kestrel.__file__).parent.rglob("*.py"):
        found |= set(pattern.findall(path.read_text(encoding="utf-8")))
    person, other = set(pt.PERSON_SETTING_PREFIXES), set(pt.OTHER_SETTING_PREFIXES)
    assert not person & other
    assert len(found) >= 15  # the scan found the constants: the pattern still matches the code
    assert found == person | other, (
        "a settings key prefix is not classified in kestrel.services.person_touched: "
        f"{sorted(found ^ (person | other))}")


def test_the_prefixes_are_the_features_own_constants():
    from kestrel.interview import notes, prep_state
    from kestrel.services import retry_removal, saved_roles, tier0_override

    assert set(pt.PERSON_SETTING_PREFIXES) == {
        saved_roles.PREFIX, tier0_override.PREFIX, notes.NOTES_PREFIX, prep_state.STORY_PREFIX,
        prep_state.ASK_PREFIX, prep_state.PEOPLE_PREFIX, retry_removal.PREFIX}
    assert pt.setting_keys(7)[0] == saved_roles.key_for(7)


# ------------------------------------------------------------------ each mark, one at a time
async def _role(sm, **extra) -> int:
    async with sm() as s:
        o = Opportunity(track="employment", opp_type="job_posting", title="Pastry Chef",
                        org_name="Fictional Org", stage="t0_pass", stage_reason="seeded",
                        seen_count=1, **extra)
        s.add(o)
        await s.commit()
        return o.id


_DUMMY = {"INTEGER": 1, "FLOAT": 1.0, "BLOB": b"x", "BOOLEAN": False}


async def _insert(s, sm, table, role_id: int | None) -> int:
    """One row of `table` with every required column filled, pointing at `role_id`. A required
    parent row is made the same way; a required parent ROLE is a different role, so the only
    thing that marks `role_id` is the table under test."""
    values: dict = {}
    for col in table.columns:
        if col.primary_key and col.autoincrement and not col.foreign_keys:
            continue
        parents = [fk.column.table for fk in col.foreign_keys]
        if col.name == "opportunity_id":
            values[col.name] = role_id
        elif col.nullable or col.default is not None or col.server_default is not None:
            continue
        elif parents and parents[0].name == "opportunity":
            values[col.name] = await _role(sm)
        elif parents:
            values[col.name] = await _insert(s, sm, parents[0], await _role(sm))
        else:
            values[col.name] = _DUMMY.get(str(col.type).split("(")[0], "x")
    result = await s.execute(insert(table).values(**values))
    return result.inserted_primary_key[0]


async def _recheck(sm, oid: int) -> tuple[bool, str, str | None]:
    async with sm() as s:
        rejected = await lane.reject_mismatch(s, await s.get(Opportunity, oid))
        await s.commit()
    async with sm() as s:
        opp = await s.get(Opportunity, oid)
        return rejected, opp.stage, opp.stage_reason


async def test_an_untouched_waiting_role_is_not_the_persons_and_is_rechecked(sm):
    oid = await _role(sm)
    async with sm() as s:
        assert not await pt.person_touched(s, await s.get(Opportunity, oid))
    rejected, stage, reason = await _recheck(sm, oid)
    assert rejected and stage == "t0_reject" and reason.startswith("unrelated_title")


@pytest.mark.parametrize("model", pt.PERSON_TABLES, ids=lambda m: m.__tablename__)
async def test_a_row_of_the_persons_protects_the_role(sm, model):
    oid = await _role(sm)
    async with sm() as s:
        await _insert(s, sm, model.__table__, oid)
        await s.commit()
    async with sm() as s:
        assert await pt.person_touched(s, await s.get(Opportunity, oid))
    assert await _recheck(sm, oid) == (False, "t0_pass", "seeded")


@pytest.mark.parametrize("model", pt.MACHINE_TABLES, ids=lambda m: m.__tablename__)
async def test_a_row_kestrel_made_does_not_protect_the_role(sm, model):
    oid = await _role(sm)
    async with sm() as s:
        await _insert(s, sm, model.__table__, oid)
        await s.commit()
    async with sm() as s:
        assert not await pt.person_touched(s, await s.get(Opportunity, oid))


@pytest.mark.parametrize("prefix", pt.PERSON_SETTING_PREFIXES)
async def test_a_settings_row_of_the_persons_protects_the_role(sm, prefix):
    oid, other = await _role(sm), await _role(sm)
    async with sm() as s:
        s.add(Setting(key=f"{prefix}{oid}", value={"note": "an invented note"}))
        await s.commit()
    async with sm() as s:
        assert await pt.person_touched(s, await s.get(Opportunity, oid))
        assert not await pt.person_touched(s, await s.get(Opportunity, other))  # this role only
    assert await _recheck(sm, oid) == (False, "t0_pass", "seeded")
    assert (await _recheck(sm, other))[0] is True


@pytest.mark.parametrize("prefix", ["run_model.pending."])
async def test_a_runs_own_settings_row_does_not_protect_the_role(sm, prefix):
    oid = await _role(sm)
    async with sm() as s:
        s.add(Setting(key=f"{prefix}{oid}", value={"model": "invented"}))
        await s.commit()
        assert not await pt.person_touched(s, await s.get(Opportunity, oid))


async def test_interview_notes_saved_through_the_feature_protect_the_role(sm):
    """The case the second review reproduced: notes on a waiting role, then the recheck."""
    from kestrel.interview import notes

    oid = await _role(sm)
    async with sm() as s:
        await notes.save_notes(s, oid, "Invented notes for a first call.")
        await s.commit()
    assert await _recheck(sm, oid) == (False, "t0_pass", "seeded")


async def test_a_hidden_role_is_the_persons(sm):
    from kestrel.db.base import utcnow_iso

    oid = await _role(sm, archived_at=utcnow_iso())
    async with sm() as s:
        assert await pt.person_touched(s, await s.get(Opportunity, oid))
    assert (await _recheck(sm, oid))[0] is False
