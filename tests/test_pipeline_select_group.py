"""Issue #116: Select all for a Pipeline group, then the whole group (D-PIPE-116).

The bar's second step ("Select all 40 in Maybe") names a group, not role ids: the server resolves
the group's roles when the action runs, from the group key and the filters the page was drawn
with. Invariants proved here, through the real route: the group is resolved at action time (a
role that left it is not touched, a stale count changes nothing), the 200-role cap is stated and
never silent, one Undo reverses the whole action and says the number, Deeper read is never
applied in bulk, and bulk decisions are excluded from learning while a single decision teaches.
Cloud-safe: a migrated SQLite file and the test client; no model, no browser, no network."""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from sqlalchemy import select

from kestrel.db.models import Opportunity, Org, Pursuit, Source, Task, VerdictOverride
from kestrel.learning.capture import is_bulk, labeled_examples
from tests import test_pipeline_view as T

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_HX = {"HX-Request": "true"}


def _events(response) -> dict:
    return json.loads(response.headers.get("HX-Trigger", "{}"))


def _db(fn):
    return asyncio.run(T._with_session(fn))


async def _seed_maybe(n: int, *, prefix: str = "Maybe role") -> list[int]:
    """n scored roles whose model read is Consider, so each sits in the Maybe group."""

    async def go(s):
        org = (await s.execute(select(Org))).scalars().first()
        src = (await s.execute(select(Source))).scalars().first()
        opps = [T._opp(org, src, f"{prefix} {i}", title=f"{prefix} {i}") for i in range(n)]
        s.add_all(opps)
        await s.flush()
        s.add_all([
            T._ev(o, verdict="consider", verdict_model="consider", score=3.2 + (i % 5) / 100,
                  weighted_score=3.1) for i, o in enumerate(opps)
        ])
        await s.commit()
        return [o.id for o in opps]

    return await T._with_session(go)


def _group(html: str, key: str) -> str:
    return html.split(f'<section class="pl-group" data-group="{key}"', 1)[1].split("</section>")[0]


def _section_attrs(html: str, key: str) -> dict[str, str]:
    tag = html.split(f'<section class="pl-group" data-group="{key}"', 1)[1].split(">", 1)[0]
    import html as htmllib

    return {k: htmllib.unescape(v) for k, v in re.findall(r'data-(\w+)="([^"]*)"', tag)}


def _maybe_ids(c) -> tuple[list[int], dict[str, str]]:
    """The Maybe group as the page draws it: the rows on screen and the group's attributes."""
    html = T._get(c, "/opportunities")
    rows = [int(i) for i in re.findall(r'data-id="(\d+)"', _group(html, "maybe"))]
    return rows, _section_attrs(html, "maybe")


async def _archived(s, ids: list[int]) -> list[int]:
    got = (await s.execute(select(Opportunity.id).where(
        Opportunity.id.in_(ids), Opportunity.archived_at.is_not(None)))).scalars()
    return sorted(got)


def _bulk(c, action: str, attrs: dict[str, str], *, expect: int | None = None, **extra):
    data = {"group": "maybe", "scope": attrs["scope"]}
    data["expect"] = str(attrs["count"] if expect is None else expect)
    data.update(extra)
    return c.post(f"/pipeline/bulk/{action}", data=data, headers=_HX)


# ============================================================================== the second step
def test_a_group_of_40_draws_25_and_the_page_carries_what_the_second_step_needs():
    asyncio.run(T._seed())
    made = asyncio.run(_seed_maybe(40))
    with T._client() as c:
        rows, attrs = _maybe_ids(c)
        html = T._get(c, "/opportunities")
    assert len(rows) == 25 and attrs["count"] == "40" and attrs["label"] == "Maybe"
    assert "track=employment" in attrs["scope"] and "lim_" not in attrs["scope"]
    assert set(rows) <= set(made)
    # The bar has the slots the script fills: the step, the note, and the cap it states.
    assert 'id="pl-ball"' in html and 'id="pl-bnote"' in html and 'data-cap="200"' in html
    for action in ("shortlist", "not-for-me", "score-again", "mark-reviewed"):
        assert f'data-bulk="{action}" data-bulk-label=' in html, action
    assert 'data-bulk="deeper-read">Deeper read</button>' in html  # per role, so no count


def test_not_for_me_on_the_whole_group_changes_all_40_not_only_the_25_drawn_and_one_undo_restores():
    asyncio.run(T._seed())
    made = asyncio.run(_seed_maybe(40))
    with T._client() as c:
        rows, attrs = _maybe_ids(c)
        assert len(rows) == 25 and attrs["count"] == "40"
        r = _bulk(c, "not-for-me", attrs)  # no role ids at all: the server names the roles
        ev = _events(r)
        toast = ev["k-toast"]
        assert toast["message"] == "40 roles moved to Hidden."
        assert toast["undoLabel"] == "Undo Not for me, 40 roles"
        assert sorted(ev["pl-gone"]["ids"]) == sorted(made)
        assert _db(lambda s: _archived(s, made)) == sorted(made)
        assert c.post(toast["undoUrl"], headers=_HX).status_code == 200
    assert _db(lambda s: _archived(s, made)) == []  # one Undo, the whole action


def test_shortlist_and_mark_reviewed_work_on_the_whole_group_with_one_undo_each():
    asyncio.run(T._seed())
    made = asyncio.run(_seed_maybe(30))
    with T._client() as c:
        _rows, attrs = _maybe_ids(c)
        ev = _events(_bulk(c, "shortlist", attrs))
        assert ev["k-toast"]["message"] == "30 roles added to your shortlist."
        assert ev["k-toast"]["undoLabel"] == "Undo Shortlist, 30 roles"
        assert c.post(ev["k-toast"]["undoUrl"], headers=_HX).status_code == 200

        async def pursuits(s):
            return len(list((await s.execute(select(Pursuit).where(
                Pursuit.opportunity_id.in_(made)))).scalars()))

        assert _db(pursuits) == 0
        _rows, attrs = _maybe_ids(c)
        assert attrs["count"] == "30"
        ev2 = _events(_bulk(c, "mark-reviewed", attrs))
        assert ev2["k-toast"]["message"] == "30 roles marked reviewed."
        assert ev2["k-toast"]["undoLabel"] == "Undo Mark reviewed, 30 roles"
        assert c.post(ev2["k-toast"]["undoUrl"], headers=_HX).status_code == 200
    labels = _db(_labels)
    assert [lb for lb in labels if lb.opportunity_id in made] == []


async def _labels(s):
    return list((await s.execute(select(VerdictOverride).order_by(VerdictOverride.id))).scalars())


# ====================================================================== resolved at action time
def test_a_role_that_left_the_group_is_not_touched_and_a_stale_count_changes_nothing():
    ids = asyncio.run(T._seed())
    made = asyncio.run(_seed_maybe(40))
    with T._client() as c:
        _rows, attrs = _maybe_ids(c)
        # After the page loaded, the person shortlisted one of the 40 elsewhere: it is Worth a
        # look now, a different group, and no longer the Maybe group's.
        gone = made[0]
        assert c.post(f"/opportunities/{gone}/shortlist").status_code == 200

        stale = _bulk(c, "not-for-me", attrs)  # the bar still says 40; the group holds 39
        toast = _events(stale)["k-toast"]
        assert "changed since you selected it" in toast["message"]
        assert "40 roles before, 39 roles now" in toast["message"] and toast["tone"] == "warn"
        assert _db(lambda s: _archived(s, made)) == []  # nothing was changed

        _rows, fresh = _maybe_ids(c)
        assert fresh["count"] == "39"
        ev = _events(_bulk(c, "not-for-me", fresh))
        assert ev["k-toast"]["message"] == "39 roles moved to Hidden."
        hidden = _db(lambda s: _archived(s, made))
        assert gone not in hidden and len(hidden) == 39
        # A role in another group, named by id alongside the group, is never touched either.
        other = _events(c.post("/pipeline/bulk/not-for-me", headers=_HX, data={
            "group": "maybe", "scope": fresh["scope"], "ids": str(ids["a"])}))
        assert "Pick one or more roles first" in other["k-toast"]["message"]  # group is empty now
        assert _db(lambda s: _archived(s, [ids["a"]])) == []


def test_the_scope_filters_apply_a_search_narrows_the_group_the_server_resolves():
    asyncio.run(T._seed())
    asyncio.run(_seed_maybe(12, prefix="Alpha"))
    other = asyncio.run(_seed_maybe(9, prefix="Bravo"))
    with T._client() as c:
        html = T._get(c, "/opportunities?q=Bravo")
        attrs = _section_attrs(html, "maybe")
        assert attrs["count"] == "9" and "q=Bravo" in attrs["scope"]
        ev = _events(_bulk(c, "not-for-me", attrs))
        assert ev["k-toast"]["message"] == "9 roles moved to Hidden."
        assert sorted(ev["pl-gone"]["ids"]) == sorted(other)  # the 12 Alpha roles stayed


# ================================================================================= the cap
def test_a_group_over_the_cap_changes_the_first_200_says_so_and_never_cuts_silently():
    asyncio.run(T._seed())
    made = asyncio.run(_seed_maybe(205))
    with T._client() as c:
        _rows, attrs = _maybe_ids(c)
        assert attrs["count"] == "205"
        ev = _events(_bulk(c, "not-for-me", attrs))
        msg = ev["k-toast"]["message"]
        assert msg.startswith("200 roles moved to Hidden.")
        assert "first 200 of 205" in msg and "the rest are unchanged" in msg
        assert len(_db(lambda s: _archived(s, made))) == 200
        _rows, left = _maybe_ids(c)
        assert left["count"] == "5"  # the five the toast said were left
        assert c.post(ev["k-toast"]["undoUrl"], headers=_HX).status_code == 200
    assert _db(lambda s: _archived(s, made)) == []


def test_more_than_the_cap_named_by_id_is_refused_whole_never_cut():
    asyncio.run(T._seed())
    made = asyncio.run(_seed_maybe(201))
    with T._client() as c:
        r = c.post("/pipeline/bulk/not-for-me", data={"ids": ",".join(map(str, made))},
                   headers=_HX)
        toast = _events(r)["k-toast"]
        assert "at most 200 roles" in toast["message"] and "201 were named" in toast["message"]
        assert "Nothing was changed" in toast["message"]
        ok = c.post("/pipeline/bulk/not-for-me", data={"ids": ",".join(map(str, made[:200]))},
                    headers=_HX)
        assert _events(ok)["k-toast"]["message"] == "200 roles moved to Hidden."
    assert len(_db(lambda s: _archived(s, made))) == 200


# ================================================================== Deeper read stays per role
def test_deeper_read_is_never_applied_in_bulk_with_or_without_a_group():
    asyncio.run(T._seed())
    made = asyncio.run(_seed_maybe(30))

    async def state(s):
        tasks = list((await s.execute(select(Task))).scalars())
        return len(tasks), len(await _archived(s, made)), len(await _labels(s))

    before = _db(state)
    with T._client() as c:
        _rows, attrs = _maybe_ids(c)
        grouped = _bulk(c, "deeper-read", attrs)
        by_id = c.post("/pipeline/bulk/deeper-read", data={"ids": str(made[0])}, headers=_HX)
    for r in (grouped, by_id):
        assert "needs your OK for each role" in _events(r)["k-toast"]["message"]
    assert _db(state) == before  # no task, no label, no hidden role: nothing was sent or changed


# ===================================================================== learning (owner decision)
def test_a_bulk_not_for_me_on_n_roles_adds_zero_learning_signals_a_single_one_adds_one():
    asyncio.run(T._seed())
    made = asyncio.run(_seed_maybe(12))

    async def signals(s):
        return len(await labeled_examples(s, "employment")), len(await _labels(s))

    base = _db(signals)
    with T._client() as c:
        # One role at a time, with a reason: this teaches once.
        single = made[0]
        r = c.post(f"/opportunities/{single}/decide/not-for-me?reason=level", headers=_HX)
        assert r.status_code == 200
        one = _db(signals)
        assert one == (base[0] + 1, base[1] + 1)

        # The same decision on the other 11 at once: 11 roles hidden, zero new signals.
        _rows, attrs = _maybe_ids(c)
        assert attrs["count"] == "11"
        ev = _events(_bulk(c, "not-for-me", attrs))
        assert ev["k-toast"]["message"] == "11 roles moved to Hidden."
        assert _db(lambda s: _archived(s, made)) == sorted(made)
    assert _db(signals) == one


def test_a_bulk_shortlist_and_a_bulk_mark_reviewed_apply_but_teach_nothing_a_single_one_teaches():
    asyncio.run(T._seed())
    made = asyncio.run(_seed_maybe(10))
    shortlist_ids, review_ids = made[:5], made[5:]

    async def taught(s):
        return sorted(e.opportunity_id for e in await labeled_examples(s, "employment"))

    base = _db(taught)
    with T._client() as c:
        a = c.post("/pipeline/bulk/shortlist", data={"ids": ",".join(map(str, shortlist_ids))},
                   headers=_HX)
        b = c.post("/pipeline/bulk/mark-reviewed", data={"ids": ",".join(map(str, review_ids))},
                   headers=_HX)
        assert _events(a)["k-toast"]["message"] == "5 roles added to your shortlist."
        assert _events(b)["k-toast"]["message"] == "5 roles marked reviewed."

        async def applied(s):
            pursuits = (await s.execute(select(Pursuit.opportunity_id).where(
                Pursuit.opportunity_id.in_(shortlist_ids)))).scalars()
            rows = [lb for lb in await _labels(s) if lb.opportunity_id in made]
            return sorted(pursuits), rows

        pursued, rows = _db(applied)
        assert pursued == sorted(shortlist_ids)  # the decision applied to every selected role
        # The state is recorded (the role is decided) and every such row carries the bulk mark.
        assert sorted(lb.opportunity_id for lb in rows) == sorted(made)
        assert all(is_bulk(lb) for lb in rows)
        assert _db(taught) == base  # but none of it teaches

        # One role at a time: shortlisting after the bulk shortlist is a new, taught judgment,
        # even though a bulk row came first on the same role.
        one = shortlist_ids[0]
        assert c.post(f"/opportunities/{one}/shortlist").status_code == 200
    assert _db(taught) == sorted(base + [one])


def test_the_ledger_the_reranker_and_a_reset_treat_a_bulk_row_as_not_learning():
    from kestrel.config import get_settings
    from kestrel.learning import ledger

    asyncio.run(T._seed())
    made = asyncio.run(_seed_maybe(4))
    with T._client() as c:
        c.post("/pipeline/bulk/mark-reviewed", data={"ids": ",".join(map(str, made[:2]))},
               headers=_HX)
        c.post(f"/opportunities/{made[2]}/shortlist")

    async def look(s):
        rows, total = await ledger._rows(s, track="employment", limit=50, offset=0)
        return sorted(r.opportunity_id for r in rows), total

    shown, total = _db(look)
    assert shown == [made[2]] and total == 1  # only the single decision is in the ledger

    async def reset(s):
        await ledger.reset_learning(s, get_settings(), labels=True, reranker=False,
                                    calibrations=False)
        return sorted(lb.opportunity_id for lb in await _labels(s))

    assert _db(reset) == sorted(made[:2])  # the bulk rows are not learning, so Reset leaves them
