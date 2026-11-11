"""Requirement corrections on the Role page (issue 170, steps 2 and 3): the routes and what the
page prints. Against the invented roles of tests/role_seed.py; nothing reaches the network or a
model.

Invariants (each pinned by a test named for it):
1. Nothing goes online without the confirmed dialog, including Score again with my changes and
   the rescore offer; the dialog lists the corrections.
2. Text typed here (a note) never reaches a resume or a PDF.
3. A correction never archives or rejects a role and never changes a Tier 0 or Tier 1 result.
4. A row shows a source only when a stored line matched; otherwise Inferred, never a guess.
5. Undo restores the exact prior state, at the route level.
6. The Evaluation JSON is never rewritten.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import select

from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import (
    DocVariant,
    Evaluation,
    Opportunity,
    RequirementCorrection,
    Setting,
    Story,
    Task,
)
from kestrel.match_text import requirement_hash
from tests import role_seed
from tests.test_contracts import _world, recorder  # noqa: F401  (the fixture)

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

PROCESS = "Process improvement across distribution centers"  # Met, model evidence, no stored line
SQL = "SQL and forecasting"  # Met, evidence names the seeded story
SUPPLY = "Supply chain domain"  # Partly met
SIX = "Six Sigma Green Belt preferred"  # Not met
H = requirement_hash


@pytest.fixture
def ids():
    out = asyncio.run(role_seed.seed_async())

    async def current(s):
        # Every seeded read was scored by the current prompt with no corrections, so the strip
        # and the older line open exactly when a correction changes the role's block (D-SCORE-4).
        for ev in (await s.execute(select(Evaluation).where(Evaluation.tier >= 2))).scalars():
            ev.prompt_version = (
                "evaluate/v3" if ev.track == "employment" else "evaluate_consulting/v3"
            )
            ev.correction_ids = []
        await s.commit()

    _db(current)
    return out


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _db(fn):
    async def run():
        async with get_sessionmaker()() as s:
            return await fn(s)

    return asyncio.run(run())


def _rows(model, *where):
    async def fn(s):
        return list((await s.execute(select(model).where(*where))).scalars())

    return _db(fn)


def _corrections() -> list[RequirementCorrection]:
    return _rows(RequirementCorrection)


def _toast(r) -> dict:
    return json.loads(r.headers["hx-trigger"])["k-toast"]


def _post(c, opp, reason, text, **extra):
    data = {"h": H(text), "reason": reason, **extra}
    return c.post(
        f"/opportunities/{opp}/role/correction", data=data, headers={"HX-Request": "true"}
    )


def _row(html: str, text: str):
    soup = BeautifulSoup(html, "html.parser")
    return soup.select_one(f'.rl-mrow[data-h="{H(text)}"]')


def _page(c, opp) -> str:
    return c.get(f"/opportunities/{opp}").text


def _set_blocks(opp_id: int, edit) -> None:
    """Change the stored read of a role (the seed's Evaluation), for a case the seed lacks."""

    async def fn(s):
        ev = (
            (
                await s.execute(
                    select(Evaluation)
                    .where(Evaluation.opportunity_id == opp_id)
                    .order_by(Evaluation.tier.desc(), Evaluation.id.desc())
                )
            )
            .scalars()
            .first()
        )
        blocks = copy.deepcopy(ev.blocks)
        edit(blocks)
        ev.blocks = blocks
        await s.commit()

    _db(fn)


def _snap(row: RequirementCorrection) -> dict:
    return {c.name: getattr(row, c.name) for c in RequirementCorrection.__table__.columns}


# ------------------------------------------------------------------------------- the rows
def test_a_met_row_with_no_stored_line_says_inferred_and_a_pencil_opens_the_panel(ids):
    with _client() as c:
        html = _page(c, ids["strong"])
    row = _row(html, PROCESS)
    assert row is not None and row["data-c"] == "met"
    assert row.select_one('[data-testid="inferred-tag"]').get_text(strip=True) == "Inferred"
    assert "No line in your resume or stories says this. Kestrel's reasoning:" in row.get_text()
    pen = row.select_one('[data-testid="match-pencil"]')
    assert pen["aria-label"] == f"Correct: {PROCESS}" and pen["title"] == "Correct this"
    chips = [b.get_text(strip=True) for b in row.select("[data-rl-pick]")]
    assert chips == ["It's overstated", "Not true of me", "They mean something else"]


def test_a_met_row_that_quotes_a_stored_story_has_no_tag_and_names_nothing_extra(ids):
    with _client() as c:
        html = _page(c, ids["strong"])
    row = _row(html, SQL)
    assert row.select_one('[data-testid="inferred-tag"]') is None
    assert row["data-kind"] == "story_own"
    assert "Open the story" in row.get_text()


def test_gaps_say_i_have_done_this_and_offer_the_four_answers(ids):
    with _client() as c:
        html = _page(c, ids["strong"])
    six = _row(html, SIX)
    assert six.select_one('[data-testid="match-done"]').get_text(strip=True) == "I've done this"
    assert [b.get_text(strip=True) for b in six.select("[data-rl-pick]")] == [
        "I have all of it",
        "Some of it",
        "It's a fair gap",
        "They mean something else",
    ]
    supply = _row(html, SUPPLY)  # Partly met: None of it, not a fair gap
    assert "None of it" in supply.get_text() and "It's a fair gap" not in supply.get_text()


def test_an_unverified_row_answers_in_one_click_and_is_never_inferred(ids):
    _set_blocks(
        ids["strong"],
        lambda b: b["b_fit"]["matches"].append(
            {"requirement": "Vendor contract renewals", "evidence": "-", "strength": "unverified"}
        ),
    )
    with _client() as c:
        html = _page(c, ids["strong"])
    row = _row(html, "Vendor contract renewals")
    assert row.select_one('[data-testid="inferred-tag"]') is None
    assert [b.get_text(strip=True) for b in row.select(".rl-mquick .rl-mchip")] == [
        "I've done this",
        "Some of it",
        "Not yet",
    ]
    with _client() as c:
        r = _post(c, ids["strong"], "some", "Vendor contract renewals", quick="1")
    saved = _corrections()[0]
    assert r.status_code == 200 and saved.reason == "some" and saved.story_id is None
    assert saved.operator_state == "part" and saved.basis == "none"


# ------------------------------------------------------------------------------- saving
def test_not_true_of_me_moves_the_row_to_gaps_tints_it_and_offers_undo(ids):
    with _client() as c:
        r = _post(c, ids["strong"], "not_me", PROCESS, note="I never ran carriers.")
        assert r.status_code == 200
        toast = _toast(r)
        assert toast["message"] == "Saved. Now not met on this role."
        assert re.fullmatch(
            rf"/opportunities/{ids['strong']}/role/correction/\d+/undo\?token=[\w-]+",
            toast["undoUrl"],
        )
        assert toast["refresh"][0]["url"] == f"/opportunities/{ids['strong']}/role/match"
        soup = BeautifulSoup(r.text, "html.parser")
        row = soup.select_one(f'#rl-match .rl-mrow[data-h="{H(PROCESS)}"]')
        assert row["data-c"] == "gap" and "is-yours" in row["class"] and "is-flash" in row["class"]
        assert row.select_one(".rl-card--warn") is None  # it sits inside the Gaps card
        assert row.find_parent(class_="rl-card--warn") is not None
        assert row.select_one('[data-testid="match-pill"]').get_text() == "You: Not met"
        assert "Kestrel: Met" in row.get_text() and "I never ran carriers." in row.get_text()
        assert row.select_one('[data-testid="match-undo"]') is not None
        assert soup.select_one("#rl-cx.on") is not None  # the strip, out of band
        assert "You changed 1 thing Kestrel concluded. Its 4.6 is from before that." in r.text
        page = _page(c, ids["strong"])
    saved = _corrections()[0]
    assert (saved.scope, saved.track, saved.operator_state, saved.kestrel_state) == (
        "person",
        "employment",
        "gap",
        "met",
    )
    assert saved.basis == "inferred" and saved.note == "I never ran carriers."
    assert saved.evaluation_id and saved.opportunity_id == ids["strong"]
    assert 'data-testid="wheel-center">1 of 5<' in page
    assert "1 changed by you" in page
    assert "Score again with my changes" in page and "See all in Profile" in page


def test_the_basis_is_quoted_when_a_story_matched_and_none_for_a_gap(ids):
    with _client() as c:
        _post(c, ids["strong"], "overstated", SQL)
        _post(c, ids["strong"], "agree", SIX)
    by = {x.requirement_text: x for x in _corrections()}
    assert by[SQL].basis == "quoted" and by[SQL].operator_state == "part"
    assert by[SIX].basis == "none" and by[SIX].operator_state == "gap"


def test_all_of_it_needs_a_story_and_then_credits_the_row_with_it(ids):
    story = _rows(Story)[0]
    with _client() as c:
        r = _post(c, ids["strong"], "all", SIX)
        assert _toast(r)["tone"] == "warn" and "Pick the story" in _toast(r)["message"]
        assert _corrections() == []
        r = _post(c, ids["strong"], "all", SIX, story_id=str(story.id), note="Did it in 2021.")
        assert _toast(r)["message"] == "Saved. Now met on this role."
        page = _page(c, ids["strong"])
    saved = _corrections()[0]
    assert saved.story_id == story.id and saved.operator_state == "met"
    row = _row(page, SIX)
    assert row["data-c"] == "met" and row.find_parent(class_="rl-card--good") is not None
    ev = BeautifulSoup(page, "html.parser").select('[data-testid="evidence-row"]')
    mine = [e for e in ev if SIX in e.get_text()][0]
    assert "Story · Demand forecast rebuild" in mine.get_text()  # credited to the story


def test_they_mean_something_else_needs_a_note_and_keeps_the_state(ids):
    with _client() as c:
        r = _post(c, ids["strong"], "misread", SQL)
        assert _toast(r)["tone"] == "warn" and _corrections() == []
        r = _post(c, ids["strong"], "misread", SQL, note="They mean forecasting in Excel.")
        assert _toast(r)["message"] == "Saved. Kestrel will learn from it."
        assert "rl-similar" not in r.headers["hx-trigger"]  # a note about this posting only
        page = _page(c, ids["strong"])
    saved = _corrections()[0]
    assert saved.scope == "posting" and saved.operator_state == "met"
    row = _row(page, SQL)
    assert row["data-c"] == "met" and "They mean forecasting in Excel." in row.get_text()
    assert 'data-testid="wheel-center">2 of 5<' in page  # nothing moved


def test_an_unknown_requirement_or_answer_writes_nothing(ids):
    with _client() as c:
        r = c.post(
            f"/opportunities/{ids['strong']}/role/correction",
            data={"h": H("Not on this posting"), "reason": "not_me"},
        )
        assert _toast(r)["tone"] == "warn" and "no longer on this posting" in _toast(r)["message"]
        r = _post(c, ids["strong"], "teleport", PROCESS)
        assert _toast(r)["tone"] == "warn"
        r = c.post(
            "/opportunities/999999/role/correction", data={"h": H(PROCESS), "reason": "not_me"}
        )
        assert _toast(r)["message"] == "That role is gone."
    assert _corrections() == []


def test_a_correction_made_on_this_role_applies_to_another_role_with_the_same_ask(ids):
    _set_blocks(
        ids["second"],
        lambda b: b["b_fit"]["matches"].append(
            {
                "requirement": PROCESS.upper() + ".",
                "evidence": "Seen at two sites",
                "strength": "strong",
            }
        ),
    )
    with _client() as c:
        _post(c, ids["strong"], "not_me", PROCESS)
        page = _page(c, ids["second"])
        sim = c.get(f"/opportunities/{ids['strong']}/role/similar?hash={H(PROCESS)}")
    row = _row(page, PROCESS)
    assert row["data-c"] == "gap"  # the exact hash matches through case and punctuation
    assert row.select_one('[data-testid="match-from"]').get_text() == "From your corrections"
    assert row.select_one('[data-testid="match-undo"]') is None  # no Undo here
    assert row.select_one('[data-testid="match-pencil-profile"]')["href"] == (
        "/profile?tab=corrections"
    )
    soup = BeautifulSoup(sim.text, "html.parser")
    assert "1 other role ask" in soup.get_text() and f"/opportunities/{ids['second']}" in sim.text
    assert soup.select_one('[data-testid="rescore-others"]')["hx-vals"] == (
        json.dumps({"opp_ids": str(ids["second"])})
    )


def test_the_similar_route_lists_only_other_open_roles_and_refuses_a_bad_hash(ids):
    with _client() as c:
        assert c.get(f"/opportunities/{ids['strong']}/role/similar?hash=zz").status_code == 400
        r = c.get(f"/opportunities/{ids['strong']}/role/similar?hash={H(PROCESS)}")
        assert r.status_code == 200 and r.text == ""  # nobody else asks for it
        assert c.get(f"/opportunities/999999/role/similar?hash={H(PROCESS)}").status_code == 404


def test_a_lead_corrects_the_same_way(ids):
    with _client() as c:
        html = _page(c, ids["lead"])
        rows = BeautifulSoup(html, "html.parser").select(".rl-mrow")
        assert rows and all(r.select_one("[data-rl-fix]") for r in rows)
        text = rows[0].select_one(".rl-mt").get_text()
        r = _post(c, ids["lead"], "misread", text, note="They mean the weekly report.")
        assert r.status_code == 200
    assert _corrections()[0].track == "consulting"


# -------------------------------------------------------------------------------- undo
def test_invariant_5_undo_restores_exactly_at_the_route_level(ids):
    """A save over an earlier answer is undone to that answer, field for field, and an undo
    token is used up."""
    with _client() as c:
        _post(c, ids["strong"], "overstated", PROCESS, note="first")
        first = _snap(_corrections()[0])
        r = _post(c, ids["strong"], "not_me", PROCESS, note="second")
        undo_url = _toast(r)["undoUrl"]
        assert _snap(_corrections()[0])["operator_state"] == "gap"
        before_page = BeautifulSoup(_page(c, ids["strong"]), "html.parser")
        u = c.post(undo_url)
        assert u.status_code == 200 and _toast(u)["message"] == "Undone."
        assert _snap(_corrections()[0]) == first  # the same id, timestamps and every column
        assert _row(_page(c, ids["strong"]), PROCESS).select_one(
            "[data-testid=match-pill]"
        ).get_text() == ("You: Partly met")
        assert before_page.select_one('[data-testid="wheel-center"]').get_text() == "1 of 5"
        # one use: the second press is refused and changes nothing
        again = c.post(undo_url)
        assert "data-undo-msg" in again.text and 'data-ok="0"' in again.text
        assert _snap(_corrections()[0]) == first


def test_undoing_a_fresh_save_deletes_it_and_the_page_is_as_before(ids):
    with _client() as c:
        before = _page(c, ids["strong"])
        r = _post(c, ids["strong"], "not_me", PROCESS)
        assert len(_corrections()) == 1
        c.post(_toast(r)["undoUrl"])
        assert _corrections() == []
        after = _page(c, ids["strong"])

    def strip(h):
        return re.sub(r"\s+", " ", BeautifulSoup(h, "html.parser").select_one("#rl-match").decode())

    assert strip(after) == strip(before)
    assert 'data-testid="wheel-center">2 of 5<' in after


def test_the_rows_own_undo_removes_and_the_toast_brings_it_back(ids):
    with _client() as c:
        _post(c, ids["strong"], "not_me", PROCESS, note="mine")
        saved = _snap(_corrections()[0])
        r = c.post(f"/opportunities/{ids['strong']}/role/correction/{saved['id']}/undo")
        assert _toast(r)["message"] == "Correction removed. Back to Kestrel's read."
        assert _corrections() == []
        back = c.post(_toast(r)["undoUrl"])
        assert back.status_code == 200
    assert _snap(_corrections()[0]) == saved


def test_an_undo_for_another_role_or_a_stale_token_changes_nothing(ids):
    with _client() as c:
        r = _post(c, ids["strong"], "not_me", PROCESS)
        cid = _corrections()[0].id
        token = _toast(r)["undoUrl"].split("token=")[1]
        wrong = c.post(f"/opportunities/{ids['second']}/role/correction/{cid}/undo?token={token}")
        assert "data-undo-msg" in wrong.text
        other_id = c.post(
            f"/opportunities/{ids['strong']}/role/correction/{cid + 50}/undo?token={token}"
        )
        assert "data-undo-msg" in other_id.text
        gone = c.post(f"/opportunities/{ids['strong']}/role/correction/{cid}/undo?token=nope")
        assert "data-undo-msg" in gone.text
        # a correction made on another role is changed in Profile, not removed from here
        no_token = c.post(f"/opportunities/{ids['second']}/role/correction/{cid}/undo")
        assert "data-undo-msg" in no_token.text
    assert len(_corrections()) == 1


def test_a_refused_row_undo_warns_and_never_replaces_the_match_area(ids):
    """D-UI-322: the row's own Undo is an htmx post aimed at #rl-match. On a stale page (the
    correction was removed elsewhere) it answers with a warning toast and HX-Reswap none, so the
    lists, the wheel and Evidence stay; the toast's plain fetch still gets the refusal span."""
    with _client() as c:
        _post(c, ids["strong"], "not_me", PROCESS, note="mine")
        cid = _corrections()[0].id
        c.post(f"/profile/corrections/{cid}/remove", headers={"HX-Request": "true"})
        url = f"/opportunities/{ids['strong']}/role/correction/{cid}/undo"
        r = c.post(url, headers={"HX-Request": "true"})
        assert r.headers.get("hx-reswap") == "none" and r.text == ""
        assert _toast(r)["message"] == "This can no longer be undone."
        assert "data-undo-msg" in c.post(url).text


def _both_roles_ask_process(ids):
    _set_blocks(
        ids["second"],
        lambda b: b["b_fit"]["matches"].append(
            {"requirement": PROCESS, "evidence": "Seen at two sites", "strength": "strong"}
        ),
    )


def test_a_role_undo_never_deletes_a_later_answer_made_from_another_role(ids):
    """Save on role A, answer the same requirement on role B (the person row is replaced in
    place), then Undo on A: refused with a plain message, and B's answer stays."""
    _both_roles_ask_process(ids)
    with _client() as c:
        a = _post(c, ids["strong"], "not_me", PROCESS)
        _post(c, ids["second"], "overstated", PROCESS, note="later answer on B")
        later = [_snap(r) for r in _corrections()]
        u = c.post(_toast(a)["undoUrl"])
        assert "data-undo-msg" in u.text and 'data-ok="0"' in u.text
        assert "changed since" in u.text
    assert [_snap(r) for r in _corrections()] == later


def test_a_role_undo_is_refused_after_a_change_in_profile(ids):
    with _client() as c:
        r = _post(c, ids["strong"], "not_me", PROCESS)
        cid = _corrections()[0].id
        ch = c.post(f"/profile/corrections/{cid}", data={"operator_state": "met", "note": "p"})
        assert ch.status_code == 200
        later = [_snap(x) for x in _corrections()]
        u = c.post(_toast(r)["undoUrl"])
        assert "data-undo-msg" in u.text and 'data-ok="0"' in u.text
    assert [_snap(x) for x in _corrections()] == later


def test_an_older_tab_cannot_undo_over_a_newer_save_on_the_same_role(ids):
    with _client() as c:
        first = _post(c, ids["strong"], "overstated", PROCESS)  # tab one
        second = _post(c, ids["strong"], "not_me", PROCESS)  # tab two
        later = [_snap(x) for x in _corrections()]
        u = c.post(_toast(first)["undoUrl"])
        assert 'data-ok="0"' in u.text
        assert [_snap(x) for x in _corrections()] == later
        ok = c.post(_toast(second)["undoUrl"])  # the newest still works
        assert _toast(ok)["message"] == "Undone."


def test_a_removed_rows_undo_is_refused_when_the_id_was_taken_again(ids):
    with _client() as c:
        _post(c, ids["strong"], "not_me", PROCESS, note="mine")
        cid = _corrections()[0].id
        rm = c.post(f"/opportunities/{ids['strong']}/role/correction/{cid}/undo")
        _post(c, ids["strong"], "overstated", PROCESS)  # a newer answer for the requirement
        later = [_snap(x) for x in _corrections()]
        back = c.post(_toast(rm)["undoUrl"])
        assert 'data-ok="0"' in back.text
    assert [_snap(x) for x in _corrections()] == later


# ------------------------------------------------------------------------------ invariants
def test_invariant_4_a_row_shows_a_source_only_when_a_stored_line_matched(ids):
    """Without a stored line the row is Inferred; once the line exists it names it. A line that
    only resembles the evidence is not a match."""
    with _client() as c:
        page = _page(c, ids["strong"])
        assert _row(page, PROCESS).select_one('[data-testid="inferred-tag"]') is not None
        evid = [
            e.get_text(" ", strip=True)
            for e in BeautifulSoup(page, "html.parser").select('[data-testid="evidence-row"]')
        ]
        assert [e for e in evid if PROCESS in e][0].endswith(
            "Inferred · no line in your resume or stories"
        )

        async def add(s):
            s.add(
                Story(
                    title="Throughput project",
                    situation="s",
                    action="Improved flow in several centers at once, a close paraphrase.",
                    result="r",
                    is_canonical=False,  # not canonical: still a stored story now
                    operator_edited=False,
                    themes=[],
                )
            )
            await s.commit()

        _db(add)
        assert _row(_page(c, ids["strong"]), PROCESS).select_one('[data-testid="inferred-tag"]')

        async def exact(s):
            s.add(
                Story(
                    title="Throughput up 22% in 2 sites",
                    situation="s",
                    action="a",
                    result="Your Meadowbrook bullet: throughput up 22% in 2 sites",
                    is_canonical=False,
                    operator_edited=False,
                    themes=[],
                )
            )
            await s.commit()

        _db(exact)
        row = _row(_page(c, ids["strong"]), PROCESS)
        assert row.select_one('[data-testid="inferred-tag"]') is None
        assert row["data-kind"] == "story_ai"  # a story Kestrel drafted: Fix the story
        assert "Fix the story" in row.get_text()


def test_match_source_needs_a_long_enough_quote_in_whole_words():
    """Short or generic evidence matches nothing, and a word inside another word is no match."""
    from types import SimpleNamespace

    from kestrel.web.pages.role import match_source

    story = SimpleNamespace(id=1, title="Built JavaScript dashboards for finance", result=None,
                            action="sql forecast models", operator_edited=True)
    resume = [("Cut month-end close from nine days to five", 7, "Resume", "Experience")]
    for short in ("Java", "a", "SQL", "dashboards", "finance"):
        assert match_source(short, [story], resume) is None, short
    # a stored line sitting inside another word of the evidence is not a quote
    assert match_source("Built nosql forecast models for ops planning", [story], []) is None
    assert match_source("Writes java dashboards for finance", [story], []) is None
    # a real quote still matches, either way round, and the resume floor is 25
    hit = match_source("Built JavaScript dashboards for finance", [story], [])
    assert hit is not None and hit["story_id"] == 1
    assert match_source("Built javascript dashboards for finance teams daily", [story], [])
    assert match_source("close from nine days", [], resume) is None  # 20 chars: under the floor
    full = match_source("Cut month-end close from nine days to five", [], resume)
    assert full["kind"] == "resume"


def test_a_short_evidence_saves_basis_inferred_never_quoted(ids):
    def edit(b):
        for m in b["b_fit"]["matches"]:
            if m["requirement"] == PROCESS:
                m["evidence"] = "dashboards"

    _set_blocks(ids["strong"], edit)

    async def add(s):
        s.add(Story(title="Built JavaScript dashboards for finance", situation="s", action="a",
                    result="r", is_canonical=True, operator_edited=True, themes=[]))
        await s.commit()

    _db(add)
    with _client() as c:
        page = _page(c, ids["strong"])
        assert _row(page, PROCESS).select_one('[data-testid="inferred-tag"]') is not None
        _post(c, ids["strong"], "not_me", PROCESS)
    assert _corrections()[0].basis == "inferred"


def test_not_true_of_me_routes_by_source(ids):
    async def make_ai(s):
        (await s.execute(select(Story))).scalars().first().operator_edited = False
        await s.commit()

    _db(make_ai)
    with _client() as c:
        page = _page(c, ids["strong"])
    sql = _row(page, SQL)
    assert sql["data-kind"] == "story_ai" and "Fix the story" in sql.get_text()
    assert "Kestrel changes nothing itself" in sql.get_text()
    assert "Fix the story" not in _row(page, PROCESS).get_text()  # inferred: nothing to edit


def test_invariant_3_and_6_a_correction_changes_no_role_and_no_evaluation(ids):
    def state():
        async def fn(s):
            opps = [
                (o.id, o.stage, o.stage_reason, o.archived_at, o.archive_reason)
                for o in (await s.execute(select(Opportunity).order_by(Opportunity.id))).scalars()
            ]
            evs = [
                json.dumps(
                    [e.id, e.tier, e.verdict, e.score, e.blocks, e.dimensions], sort_keys=True
                )
                for e in (await s.execute(select(Evaluation).order_by(Evaluation.id))).scalars()
            ]
            return opps, evs

        return _db(fn)

    before = state()
    with _client() as c:
        for reason, text in (("not_me", PROCESS), ("overstated", SQL), ("agree", SIX)):
            _post(c, ids["strong"], reason, text)
        _post(c, ids["strong"], "misread", SUPPLY, note="x")
    assert state() == before
    assert len(_corrections()) == 4


def test_invariant_2_a_note_never_reaches_a_resume_or_pdf(ids):
    """The note is stored on the correction and nowhere else: every rendered bullet is the stored
    text of its variant, byte for byte, with and without the correction."""
    from kestrel.docs.catalog import default_config, load_catalog, resolve
    from kestrel.docs.render import bullets_in_html, render_html

    secret = "ZEBRA-NOTE-7731 I never ran carriers"

    async def render(s):
        catalog = await load_catalog(s, ids["master"])
        doc = resolve(catalog, default_config(catalog))
        variants: dict[str, set[str]] = {}
        for v in (await s.execute(select(DocVariant))).scalars():
            variants.setdefault(v.key, set()).add(v.text)
        return render_html(doc), variants

    html_before, variants_before = _db(render)
    with _client() as c:
        r = _post(c, ids["strong"], "not_me", PROCESS, note=secret)
        assert r.status_code == 200
    html_after, variants_after = _db(render)
    assert variants_after == variants_before  # no variant was added or changed
    assert secret not in html_after
    assert all(secret not in text for texts in variants_after.values() for text in texts)
    bullets = bullets_in_html(html_after)
    assert bullets and bullets == bullets_in_html(html_before)
    assert all(text in variants_after[key] for key, text in bullets)
    assert _corrections()[0].note == secret  # it lives on the correction only


def test_a_story_chip_list_and_template_are_on_the_page(ids):
    with _client() as c:
        html = _page(c, ids["strong"])
    soup = BeautifulSoup(html, "html.parser")
    tpl = soup.select_one("template#rl-story-tpl")
    assert tpl is not None and "Demand forecast rebuild" in tpl.decode_contents()
    assert "+ Write a new story" in tpl.decode_contents()
    assert "Kestrel can only credit what you've written down." in soup.get_text()
    assert "+ Add a story for something else they ask for" in soup.get_text()


def test_evidence_is_collapsed_with_its_count_and_lists_every_row(ids):
    def many(b):
        for i in range(8):
            b["b_fit"]["matches"].append(
                {
                    "requirement": f"Extra ask {i}",
                    "evidence": f"Kestrel reads {i}",
                    "strength": "strong",
                }
            )

    _set_blocks(ids["strong"], many)
    with _client() as c:
        html = _page(c, ids["strong"])
    soup = BeautifulSoup(html, "html.parser")
    toggle = soup.select_one('[data-testid="evidence-toggle"]')
    assert toggle["aria-expanded"] == "false"
    assert "on" not in soup.select_one("#rl-evbody")["class"]
    assert soup.select_one('[data-testid="evidence-count"]').get_text() == (
        "1 from your resume and stories · 10 inferred"
    )
    assert len(soup.select('[data-testid="evidence-row"]')) == 11  # the 6-row cap is gone


def test_confidential_mode_hides_the_employer_in_the_note_and_the_offer(ids):
    """Confidential mode (the real setting, with the employer named in the resume) redacts the
    employer in a saved note on the page and in the swap, and in the rescore offer, and keeps the
    row's hash."""
    from kestrel.db.models import DocMaster, DocSection

    async def conf(s):
        s.add(Setting(key="confidential_mode", value=True))
        m = DocMaster(track="employment", doc_kind="resume", name="Resume")
        s.add(m)
        await s.flush()
        s.add(DocSection(doc_master_id=m.id, section_kind="role", title="Experience",
                         org_label="Zephyrine Corp", org_label_redacted="Current Employer",
                         end_date="present"))
        opp = await s.get(Opportunity, ids["second"])
        opp.title = "Planner at Zephyrine Corp"
        await s.commit()

    _both_roles_ask_process(ids)
    _db(conf)
    with _client() as c:
        r = _post(c, ids["strong"], "not_me", PROCESS, note="Zephyrine Corp ran that, not me")
        page = _page(c, ids["strong"])
        sim = c.get(f"/opportunities/{ids['strong']}/role/similar?hash={H(PROCESS)}")
        prof = c.get("/profile?tab=corrections").text
    assert r.status_code == 200 and "Current Employer ran that" in r.text
    assert "Zephyrine" not in r.text and "Zephyrine" not in page
    assert "Zephyrine" not in sim.text and "Current Employer" in sim.text
    assert "Zephyrine" not in prof
    assert _row(page, PROCESS)["data-c"] == "gap" and _row(page, PROCESS)["data-h"] == H(PROCESS)
    assert _corrections()[0].note == "Zephyrine Corp ran that, not me"  # stored as typed


# --------------------------------------------------------------- invariant 1: nothing online
def _score_again_world(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")
    return asyncio.run(_world())


def _task_count() -> int:
    return len(_rows(Task))


def test_invariant_1_no_correction_route_sends_queues_or_calls_a_model(recorder, monkeypatch):  # noqa: F811
    """With an online route configured on every tier: saving, undoing, removing, restoring and
    asking for the offer call no model, open no connection and queue no task."""
    ids = _score_again_world(monkeypatch)
    tasks = _task_count()
    with TestClient(_build_app(), raise_server_exceptions=False) as c:
        r = _post(c, ids["strong"], "not_me", PROCESS, note="n")
        assert r.status_code == 200
        c.post(_toast(r)["undoUrl"])
        r = _post(c, ids["strong"], "overstated", SQL)
        cid = _corrections()[0].id
        c.post(f"/opportunities/{ids['strong']}/role/correction/{cid}/undo")
        c.get(f"/opportunities/{ids['strong']}/role/similar?hash={H(PROCESS)}")
        c.get(f"/opportunities/{ids['strong']}/role/match")
    assert recorder.llm == [] and recorder.sent() == []
    assert _task_count() == tasks


def test_invariant_1_score_again_and_the_offer_open_the_dialog_and_never_post(
    recorder,  # noqa: F811
    monkeypatch,
):
    """The strip's button and the offer's button are GETs of /rescore/choose: neither form nor
    button posts to /rescore. The dialog lists the corrections, names the online model and what
    is sent, and an unconfirmed post to /rescore is refused with no task queued."""
    ids = _score_again_world(monkeypatch)
    _set_blocks(
        ids["second"],
        lambda b: b["b_fit"]["matches"].append(
            {"requirement": PROCESS, "evidence": "-", "strength": "strong"}
        ),
    )
    tasks = _task_count()
    with TestClient(_build_app(), raise_server_exceptions=False) as c:
        _post(c, ids["strong"], "not_me", PROCESS, note="I never ran carriers.")
        _post(c, ids["strong"], "misread", SQL, note="They mean Excel forecasting.")
        page = BeautifulSoup(_page(c, ids["strong"]), "html.parser")
        offer = BeautifulSoup(
            c.get(f"/opportunities/{ids['strong']}/role/similar?hash={H(PROCESS)}").text,
            "html.parser",
        )
        for btn in (
            page.select_one('[data-testid="score-again-mine"]'),
            offer.select_one('[data-testid="rescore-others"]'),
        ):
            assert btn.get("hx-get") == "/rescore/choose" and not btn.get("hx-post")
            assert btn.find_parent("form") is None
        for part in (page.select_one("#rl-cx"), offer):
            assert not [e for e in part.select("[hx-post],form") if "/rescore" in str(e)]
        dlg = c.get("/rescore/choose", params={"opp_ids": str(ids["strong"])}).text
        d = BeautifulSoup(dlg, "html.parser")
        lines = [
            li.get_text() for li in d.select('[data-testid="score-again-correction-lines"] li')
        ]
        assert lines == _expected_lines(ids["strong"])  # exactly what scoring would state
        assert any("does not have this" in x and "I never ran carriers." in x for x in lines)
        assert any("(posting)" in x and "They mean Excel forecasting." in x for x in lines)
        assert d.select_one("[data-sf-confirm]") is not None  # the ask-first step
        assert d.select_one('[data-testid="score-again-sends-corrections"]') is not None
        confirmed = d.select_one("[data-sf-confirmed]")
        assert confirmed.has_attr("disabled")  # confirmed=1 is only enabled by Send and score
        r = c.post("/rescore", data={"opp_ids": str(ids["strong"]), "model": "same"})
        assert r.status_code < 500
        assert "score-again-done" not in r.text
    # Opening the dialog looks up the key's balance (a bodiless GET carrying only the saved key,
    # as before this feature); nothing else is requested and no posting or profile text is sent.
    assert recorder.llm == []
    assert [q for q in recorder.http if q.method != "GET" or q.content] == []
    assert _task_count() == tasks


def test_the_dialog_lists_nothing_when_there_are_no_corrections(ids):
    with _client() as c:
        dlg = c.get("/rescore/choose", params={"opp_ids": str(ids["strong"])}).text
    assert "score-again-correction-lines" not in dlg


def _expected_lines(opp_id: int) -> list[str]:
    from kestrel.learning.corrections import scoring_corrections
    from kestrel.pipeline.context import operator_correction_lines

    async def fn(s):
        opp = await s.get(Opportunity, opp_id)
        return operator_correction_lines(await scoring_corrections(s, opp))

    return _db(fn)


def test_the_dialog_lists_what_scoring_states_and_follows_a_changed_correction(ids):
    with _client() as c:
        _post(c, ids["strong"], "not_me", PROCESS, note="first note")

        def listed():
            d = BeautifulSoup(
                c.get("/rescore/choose", params={"opp_ids": str(ids["strong"])}).text,
                "html.parser",
            )
            return [
                li.get_text() for li in d.select('[data-testid="score-again-correction-lines"] li')
            ]

        first = listed()
        assert first == _expected_lines(ids["strong"]) and "first note" in first[0]
        _post(c, ids["strong"], "overstated", PROCESS, note="second note")
        second = listed()
        assert second == _expected_lines(ids["strong"]) and second != first
        assert "has some of this" in second[0] and "second note" in second[0]


def _build_app():
    from kestrel.web.app import build_app

    return build_app(worker=False)


# ------------------------------------------- a story written from a correction panel (#54)
def test_a_story_from_a_correction_panel_never_queues_a_rescore_even_on_a_direct_post(ids):
    """Invariant: a story saved from a correction panel (the sheet's For names the requirement)
    queues no score, held or not, and its toast promises none; the page's strip offers Score
    again once the correction exists. A direct POST with rescore=1 is treated the same."""
    with _client() as c:
        r = c.post(f"/opportunities/{ids['strong']}/role/story", data={
            "situation": "An invented night rota kept slipping.", "action": "I rebuilt it.",
            "result": "It held for two winters.", "rescore": "1", "confirmed": "1",
            "for_text": PROCESS,
        }, headers={"HX-Request": "true"})
        assert r.status_code == 200
        msg = _toast(r)["message"]
        assert "score this role again" not in msg and "waiting to be scored" not in msg, msg
        assert any(s.situation == "An invented night rota kept slipping." for s in _rows(Story))
        assert not _rows(Task, Task.type.in_(("t1", "t2")))


def test_a_story_toast_never_promises_a_rescore_that_is_held(ids, monkeypatch):
    """Save and re-score (no correction panel) whose task ends up held for the person's OK:
    the toast says nothing was sent instead of promising a score."""
    from kestrel.queue import rescore as rq
    from kestrel.services.online_consent import APPROVAL_HOLD

    real = rq.enqueue_rescore

    async def held(s, opp_ids, **kw):
        res = await real(s, opp_ids, **kw)
        for t in (await s.execute(select(Task).where(Task.type == "t2"))).scalars():
            t.last_error = APPROVAL_HOLD
        return res

    monkeypatch.setattr(rq, "enqueue_rescore", held)
    with _client() as c:
        r = c.post(f"/opportunities/{ids['strong']}/role/story",
                   data={"situation": "An invented situation.", "rescore": "1"},
                   headers={"HX-Request": "true"})
        msg = _toast(r)["message"]
        assert "score this role again" not in msg and "Nothing was sent" in msg, msg
