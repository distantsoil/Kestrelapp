"""Older scores after the v3 upgrade (issue 179, D-SCORE-4): a stored score reads as older only
when a correction bears on its role, and every place that shows or counts it agrees (the Pipeline
row, the Pipeline panel, the role page, Profile's rescore offer). Against the invented roles of
tests/role_seed.py; the model is never called and nothing reaches the network."""

from __future__ import annotations

import asyncio
import re

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import event, select, update

from kestrel.db.base import utcnow_iso
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Evaluation, Opportunity, RequirementCorrection
from kestrel.learning import corrections as corr
from kestrel.learning import score_age
from tests import role_seed

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

PROCESS = "Process improvement across distribution centers"  # on the strong role only
SQL = "SQL and forecasting"  # on the strong role (and the second one where a test adds it)
OLDER = "older scoring instructions"
SCORED_AT = "2026-11-01T09:00:00.000000+00:00"
_V2 = {"employment": "evaluate/v2", "consulting": "evaluate_consulting/v2"}
_V3 = {"employment": "evaluate/v3", "consulting": "evaluate_consulting/v3"}


@pytest.fixture
def ids():
    return asyncio.run(role_seed.seed_async())


def _db(fn):
    async def run():
        async with get_sessionmaker()() as s:
            return await fn(s)

    return asyncio.run(run())


def _set_versions(versions: dict[str, str], at: str = SCORED_AT) -> None:
    """Every stored read under these versions, scored at `at`: an upgraded database."""
    async def fn(s):
        for track, v in versions.items():
            await s.execute(update(Evaluation).where(Evaluation.track == track)
                            .values(prompt_version=v, created_at=at))
        await s.commit()

    _db(fn)


def _add_sql_to_second(ids) -> None:
    async def fn(s):
        ev = (await s.execute(select(Evaluation).where(
            Evaluation.opportunity_id == ids["second"]))).scalar_one()
        blocks = dict(ev.blocks)
        fit = dict(blocks.get("b_fit") or {})
        fit["matches"] = [*fit.get("matches", []),
                          {"requirement": SQL, "strength": "strong", "evidence": "Weekly."}]
        blocks["b_fit"] = fit
        ev.blocks = blocks
        await s.commit()

    _db(fn)


def _correct(opp_id: int, text: str, *, reason: str = "not_me", note: str | None = None) -> int:
    async def fn(s):
        row, _undo = await corr.save_correction(
            s, opportunity_id=opp_id, requirement_text=text, kestrel_state="met", reason=reason,
            note=note)
        return row.id

    return _db(fn)


def _remove(cid: int) -> None:
    async def fn(s):
        await corr.remove_correction(s, cid)

    _db(fn)


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _list_marked(c, ids) -> set[int]:
    """The roles whose Pipeline row warning says the score is older (both tracks' lists)."""
    marked = set()
    for url in ("/opportunities", "/opportunities?track=consulting"):
        soup = BeautifulSoup(c.get(url).text, "html.parser")
        for row in soup.select('[data-testid="pl-row"]'):
            warn = row.select_one('[data-testid="row-warning"]')
            if warn is not None and OLDER in (warn.get("aria-label") or ""):
                marked.add(int(row["data-id"]))
    return marked & {ids[k] for k in ("strong", "second", "maybe", "lead")}


def _role_marked(c, oid: int) -> bool:
    return 'data-testid="score-older"' in c.get(f"/opportunities/{oid}").text


def _panel_marked(c, oid: int) -> bool:
    return OLDER in c.get(f"/opportunities/{oid}/panel").text


def _everywhere(c, ids) -> dict[str, set[int]]:
    roles = [ids[k] for k in ("strong", "second", "maybe", "lead")]
    return {
        "list": _list_marked(c, ids),
        "role": {o for o in roles if _role_marked(c, o)},
        "panel": {o for o in roles if _panel_marked(c, o)},
        "data": _db(lambda s: score_age.older_among(s, roles)),
    }


def _assert_all(c, ids, expected: set[int]) -> None:
    seen = _everywhere(c, ids)
    assert seen == {k: expected for k in seen}, seen


# --------------------------------------------------------------------------- the invariants
def test_an_upgraded_database_with_no_corrections_shows_no_older_marks_anywhere(ids):
    _set_versions(_V2)
    with _client() as c:
        _assert_all(c, ids, set())


def test_one_correction_on_role_a_marks_a_and_not_b(ids):
    _set_versions(_V2)
    _correct(ids["strong"], PROCESS)
    with _client() as c:
        _assert_all(c, ids, {ids["strong"]})


def test_adding_a_correction_marks_exactly_the_roles_it_applies_to(ids):
    _set_versions(_V3)
    _add_sql_to_second(ids)
    with _client() as c:
        _assert_all(c, ids, set())
        _correct(ids["strong"], SQL)  # both roles ask for SQL; the lead and maybe do not
        _assert_all(c, ids, {ids["strong"], ids["second"]})


def test_a_posting_note_marks_only_its_own_role(ids):
    _set_versions(_V2)
    _add_sql_to_second(ids)
    _correct(ids["strong"], SQL, reason="misread", note="They mean the weekly reports.")
    with _client() as c:
        _assert_all(c, ids, {ids["strong"]})


def test_removing_it_clears_the_mark_when_nothing_else_applies(ids):
    _set_versions(_V2)
    first = _correct(ids["strong"], PROCESS)
    second = _correct(ids["strong"], SQL)
    with _client() as c:
        _assert_all(c, ids, {ids["strong"]})
        _remove(first)
        _assert_all(c, ids, {ids["strong"]})  # SQL still applies
        _remove(second)
        _assert_all(c, ids, set())


def test_a_score_from_after_the_correction_is_current_and_an_old_version_is_always_older(ids):
    _set_versions(_V3)
    _correct(ids["strong"], PROCESS)

    async def rescored(s):
        await s.execute(update(Evaluation).where(Evaluation.opportunity_id == ids["strong"])
                        .values(created_at="2999-01-01T00:00:00.000000+00:00"))
        await s.execute(update(Evaluation).where(Evaluation.opportunity_id == ids["lead"])
                        .values(prompt_version="evaluate_consulting/v1"))
        await s.commit()

    _db(rescored)
    with _client() as c:
        _assert_all(c, ids, {ids["lead"]})


def test_score_again_counts_match_the_marks(ids):
    _set_versions(_V3)
    _add_sql_to_second(ids)
    cid = _correct(ids["strong"], SQL)
    _set_versions(_V3, utcnow_iso())  # rescored after it: none older
    with _client() as c:
        _assert_all(c, ids, set())
        r = c.post(f"/profile/corrections/{cid}", data={"operator_state": "part", "note": ""},
                   headers={"HX-Request": "true"})
        assert r.status_code == 200, r.text
        n = int(re.search(r"Rescore (\d+) role", r.text).group(1))
        marked = _everywhere(c, ids)
        assert n == len(marked["list"]) == len(marked["role"]) == 2, (n, marked)
        offer = c.get(f"/opportunities/{ids['strong']}/role/similar",
                      params={"hash": corr.requirement_hash(SQL)}).text
        others = re.search(r"(\d+) other", offer)
        assert others and int(others.group(1)) == len(marked["list"] - {ids["strong"]})


def test_a_whole_list_is_one_correction_query(ids):
    """The Pipeline list reads the corrections for every row at once, never one query a role."""
    _set_versions(_V2)
    _correct(ids["strong"], PROCESS)
    statements: list[str] = []

    async def fn(s):
        pairs = []
        for oid in (ids["strong"], ids["second"], ids["maybe"], ids["lead"]):
            opp = await s.get(Opportunity, oid)
            ev = (await s.execute(select(Evaluation).where(
                Evaluation.opportunity_id == oid))).scalars().first()
            pairs.append((opp, ev))
        engine = s.bind.sync_engine

        def count(conn, cursor, statement, *a):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", count)
        try:
            return await score_age.older_ids(s, pairs)
        finally:
            event.remove(engine, "before_cursor_execute", count)

    assert _db(fn) == {ids["strong"]}
    hits = [st for st in statements if RequirementCorrection.__tablename__ in st]
    assert len(hits) == 1, statements


# ------------------------------------------- only this posting's corrections (owner, 2026-11-11)
def _strip_open(c, oid: int) -> bool:
    soup = BeautifulSoup(c.get(f"/opportunities/{oid}").text, "html.parser")
    return soup.select_one("#rl-cx.on") is not None


def _sent(oid: int) -> list[str]:
    from kestrel.pipeline.context import operator_correction_lines

    async def fn(s):
        return operator_correction_lines(
            await corr.scoring_corrections(s, await s.get(Opportunity, oid)))

    return _db(fn)


def _given(oid: int, cids: list[int], at: str) -> None:
    """The role's read was scored at `at` with these corrections stated (what Tier 2 stores)."""
    async def fn(s):
        await s.execute(update(Evaluation).where(Evaluation.opportunity_id == oid)
                        .values(correction_ids=cids, created_at=at))
        await s.commit()

    _db(fn)


def test_an_unrelated_correction_is_not_sent_and_does_not_mark_the_role(ids):
    """A person correction about a requirement only the strong role asks for is never stated
    for the second role, and the second role's score is not older for it (D-SCORE-1)."""
    _set_versions(_V3)
    _correct(ids["strong"], PROCESS, note="I ran the night rota.")
    assert _sent(ids["second"]) == []
    assert any(PROCESS in line for line in _sent(ids["strong"]))
    with _client() as c:
        _assert_all(c, ids, {ids["strong"]})
        assert not _strip_open(c, ids["second"])
        assert _strip_open(c, ids["strong"])


def test_a_matching_correction_is_sent_and_marks_the_role_and_the_strip_agrees(ids):
    _set_versions(_V3)
    _add_sql_to_second(ids)
    _correct(ids["strong"], SQL)
    assert any(SQL in line for line in _sent(ids["second"]))
    with _client() as c:
        _assert_all(c, ids, {ids["strong"], ids["second"]})
        assert {o for o in (ids["strong"], ids["second"], ids["maybe"])
                if _strip_open(c, o)} == {ids["strong"], ids["second"]}


def test_removing_a_correction_the_read_was_given_marks_it_older(ids):
    """Older exactly when the role's block changed: a read scored with a correction stays current
    until that correction is removed, then reads as older, and Undo of the removal clears it."""
    _set_versions(_V3)
    cid = _correct(ids["strong"], PROCESS)
    later = "2999-01-01T00:00:00.000000+00:00"
    _given(ids["strong"], [cid], later)
    with _client() as c:
        _assert_all(c, ids, set())
        assert not _strip_open(c, ids["strong"])

        async def remove(s):
            return await corr.remove_correction(s, cid)

        undo = _db(remove)
        _assert_all(c, ids, {ids["strong"]})
        assert _strip_open(c, ids["strong"])

        async def put_back(s):
            await corr.undo_correction(s, undo)

        _db(put_back)
        _assert_all(c, ids, set())


def test_a_correction_the_first_score_missed_marks_the_role_until_it_is_rescored(ids):
    """D-SCORE-7: the first score stated nothing (the description worded the requirement
    differently), and its read then lists the requirement, so Score again would send the
    correction: the role reads as older everywhere, strip included, until a read is given it.
    The second role, which does not ask for it, is marked nowhere."""
    _set_versions(_V3)
    cid = _correct(ids["strong"], PROCESS)
    later = "2999-01-01T00:00:00.000000+00:00"
    _given(ids["strong"], [], later)
    _given(ids["second"], [], later)
    with _client() as c:
        _assert_all(c, ids, {ids["strong"]})
        assert _strip_open(c, ids["strong"]) and not _strip_open(c, ids["second"])
        assert any(PROCESS in line for line in _sent(ids["strong"]))
        _given(ids["strong"], [cid], later)  # Score again: the read was given it
        _assert_all(c, ids, set())
        assert not _strip_open(c, ids["strong"])


# ------------------------------------------- a deeper read and Score again (D-SCORE-8)
def _deeper_read(oid: int) -> None:
    """The role's read becomes a Tier 3 deeper read that stated no correction."""
    async def fn(s):
        await s.execute(update(Evaluation).where(Evaluation.opportunity_id == oid)
                        .values(tier=3, correction_ids=[]))
        await s.commit()

    _db(fn)


def _score_again(oid: int) -> None:
    """What Score again stores: a new Tier 2 read, given exactly what the prompt states now."""
    async def fn(s):
        ev = (await s.execute(select(Evaluation).where(Evaluation.opportunity_id == oid)
                              .order_by(Evaluation.id.desc()).limit(1))).scalar_one()
        given = [c.id for c in await corr.scoring_corrections(s, await s.get(Opportunity, oid))]
        cols = {k.name: getattr(ev, k.name) for k in Evaluation.__table__.columns
                if k.name != "id"}
        cols.update(tier=2, created_at="2999-01-01T00:00:00.000000+00:00", correction_ids=given)
        s.add(Evaluation(**cols))
        await s.commit()

    _db(fn)


def test_score_again_clears_the_mark_on_a_role_whose_latest_read_is_a_deeper_read(ids):
    """The deeper read still speaks for the role (highest tier), but the mark measures what
    Score again would send against the newest read: a Tier 2 Score again given the block clears
    it everywhere, strip included, and a correction after it marks the role again."""
    _set_versions(_V3)
    _deeper_read(ids["strong"])
    _correct(ids["strong"], PROCESS)
    with _client() as c:
        _assert_all(c, ids, {ids["strong"]})
        assert _strip_open(c, ids["strong"])
        _score_again(ids["strong"])
        _assert_all(c, ids, set())
        assert not _strip_open(c, ids["strong"])
        _correct(ids["strong"], SQL)  # changed after the Score again read
        _assert_all(c, ids, {ids["strong"]})
        assert _strip_open(c, ids["strong"])


def test_a_version_older_read_opens_the_strip_when_its_block_has_a_line(ids):
    """A v1 read is older by its version alone; the strip agrees with the older line whenever
    the role's block states something (it says how many lines), and stays closed at 0."""
    _set_versions({"employment": "evaluate/v1", "consulting": "evaluate_consulting/v1"})
    with _client() as c:
        assert _role_marked(c, ids["strong"]) and not _strip_open(c, ids["strong"])
        _correct(ids["strong"], PROCESS)
        assert _sent(ids["strong"])
        assert _role_marked(c, ids["strong"]) and _strip_open(c, ids["strong"])
