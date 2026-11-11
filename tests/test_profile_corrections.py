"""Profile > Corrections (issue 170 step 4; D-UI-291, D-LEARN-3 to D-LEARN-7). The tab, its cards,
the search, and the routes that change, remove and restore a correction. Invariants proved here:

- Undo and Restore put back the exact prior row, field for field (same id and timestamps), and a
  stale or forged Undo is refused changing nothing.
- Nothing typed here reaches a resume or a PDF: the rendered bullets are the doc_variant texts only.
- Confidential mode redacts the company (and the employer anywhere in what the page prints).
- No route here sends anything online, calls a model or queues a task; a rescore is only ever
  offered through /rescore/choose.
- A change never rewrites the Evaluation, never writes a label, never archives a role.

Invented text only. No network, no model."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from kestrel import paths
from tests import corrections_seed as seed_mod
from tests.test_contracts import (
    _assert_nothing_happened,
    _count_tasks,
    recorder,  # noqa: F401  (a fixture, requested by name in the test below)
)

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

TEMPLATES = Path(__file__).resolve().parents[1] / "kestrel" / "web" / "templates" / "partials"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    from kestrel.web import routes_control

    async def _down(*a, **k):
        return None

    monkeypatch.setattr(routes_control, "_probe_ollama", _down, raising=False)


@pytest.fixture
def world():
    return seed_mod.seed()


@pytest.fixture
def client(world):
    from kestrel.web.app import build_app

    with TestClient(build_app(worker=False), raise_server_exceptions=False) as c:
        yield c


def _rows(table: str = "requirement_correction") -> list[dict]:
    con = sqlite3.connect(paths.db_path())
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(f"select * from {table} order by id")]
    finally:
        con.close()


def _row(cid: int) -> dict | None:
    return next((r for r in _rows() if r["id"] == cid), None)


def _cid(world, requirement: str) -> int:
    return world[f"corr_{requirement}"]


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _tab(client, q: str = ""):
    return _soup(client.get(f"/profile?tab=corrections{('&q=' + q) if q else ''}").text)


def _event(resp) -> dict:
    return json.loads(resp.headers["HX-Trigger-After-Swap"])["pf-corr-saved"]


def _change(client, cid: int, state: str, note: str = ""):
    return client.post(f"/profile/corrections/{cid}", data={"operator_state": state, "note": note},
                       headers={"HX-Request": "true"})


# ------------------------------------------------------------------------------- the tab
def test_the_tab_is_registered_last_with_its_url_and_a_count_badge(client):
    from kestrel.web.pages.profile import TABS

    assert TABS[-1] == ("corrections", "Corrections")
    soup = _tab(client)
    link = soup.select_one('.pf-tabs a[data-tab="corrections"]')
    assert link["href"] == "/profile?tab=corrections" and link["aria-selected"] == "true"
    assert link.select_one(".pf-tabs__n").text == "4"
    # The badge shows on every tab, Documents included.
    other = _soup(client.get("/profile?tab=about").text)
    assert other.select_one('.pf-tabs a[data-tab="corrections"] .pf-tabs__n').text == "4"
    docs = _soup(client.get("/documents").text)
    assert docs.select_one('.pf-tabs a[data-tab="corrections"] .pf-tabs__n').text == "4"


def test_intro_footer_and_help_cards_use_the_mockup_words(client):
    soup = _tab(client)
    text = soup.get_text(" ", strip=True)
    assert ("Where you changed what Kestrel concluded about you. Each one counts for every role."
            in text)
    assert "Notes about a single posting stay on that role." in text
    assert soup.select_one('input[aria-label="Search corrections"]')["placeholder"] == (
        "Search corrections")
    titles = [h.get_text(strip=True) for h in soup.select(".k-hcard h3")]
    assert titles == ["What a correction does", "Changing or removing one",
                      "Stories Kestrel drafted"]
    assert "—" not in text  # no em dashes in the copy


def test_cards_are_newest_first_with_each_part_of_the_mockup(client, world):
    soup = _tab(client)
    cards = soup.select(".pf-cr")
    assert [c.select_one(".pf-cr__req").text for c in cards] == [
        seed_mod.REQ_CARRIER, seed_mod.REQ_LEAN, seed_mod.REQ_TEAMS, seed_mod.REQ_PLAN]
    carrier, lean, teams, plan = cards
    # Kestrel's state struck through, then the operator's pill.
    assert carrier.select_one("s.pf-cr__k").text == "Kestrel: Met"
    assert carrier.select_one(".pf-cr__y").text == "You: Not met"
    assert carrier.select_one(".pf-cr__ag") is None
    assert "No line in your resume or stories says it." in carrier.select_one(".pf-cr__why").text
    assert carrier.select_one(".pf-cr__note").text == (
        "“I chose carriers once; someone else ran them.”")
    # "Kestrel agrees now" only where a later read agreed.
    assert lean.select_one(".pf-cr__ag").text == "Kestrel agrees now"
    assert lean.select_one(".pf-cr__y").text == "You: Met"
    assert lean.select_one(".pf-cr__why").text.strip() == (
        "Shown by your story “Lean program across two sites”.")
    # A story Kestrel drafted and the person never edited: the warning and Fix the story.
    why = teams.select_one(".pf-cr__why")
    assert "Kestrel drafted that story and you have not edited it." in why.text
    assert why.select_one("a").text == "Fix the story"
    assert why.select_one("a")["href"] == f"/stories#story-{world['drafted']}"
    # Some of it with no story: the answer and Add a story.
    why = plan.select_one(".pf-cr__why")
    assert "You answered “Some of it”. Nothing you have written covers it yet." in why.text
    assert why.select_one("a").text == "Add a story"
    assert plan.select_one(".pf-cr__note") is None


def test_the_meta_line_names_the_role_the_date_and_the_live_count(client, world):
    carrier, _lean, teams, plan = _tab(client).select(".pf-cr")
    meta = carrier.select_one(".pf-cr__meta")
    assert meta.get_text(" ", strip=True).startswith(
        "Added from Senior Operations Analyst, Larkspur Logistics ·")
    assert meta.select_one("a")["href"] == f"/opportunities/{world['role_a']}"
    assert re.search(r"· (?:[A-Z][a-z]{2} \d{1,2}(?:, \d{4})?) · Counts on 2 roles$",
                     meta.get_text(" ", strip=True)), meta.text
    # Made on an archived role: the link is still there and still opens.
    teams_meta = teams.select_one(".pf-cr__meta a")
    assert teams_meta["href"] == f"/opportunities/{world['role_b']}"
    assert client.get(teams_meta["href"]).status_code == 200
    assert plan.select_one(".pf-cr__meta a").text.startswith("Operations Program Lead")


def test_counts_on_is_one_role_singular(client, world):
    # Archive one of the two open roles: the count follows, and the noun agrees.
    con = sqlite3.connect(paths.db_path())
    con.execute("update opportunity set archived_at='2026-02-02T00:00:00' where id=?",
                (world["role_c"],))
    con.commit()
    con.close()
    meta = _tab(client).select_one(".pf-cr__meta").get_text(" ", strip=True)
    assert meta.endswith("Counts on 1 role")


def test_a_deleted_story_says_so_and_a_removed_role_does_not_break_the_card(client, world):
    from kestrel.db.engine import forget_engine, get_sessionmaker
    from kestrel.interview.stories import delete_story

    async def go():
        async with get_sessionmaker()() as s:  # the ORM, so the story-deleted mark is written
            assert await delete_story(s, world["mine"])

    asyncio.run(go())
    forget_engine()
    con = sqlite3.connect(paths.db_path())
    con.execute("pragma foreign_keys=on")
    con.execute("delete from evaluation where opportunity_id=?", (world["role_c"],))
    con.execute("delete from opportunity where id=?", (world["role_c"],))
    con.commit()
    con.close()
    soup = _tab(client)
    lean = next(c for c in soup.select(".pf-cr")
                if c.select_one(".pf-cr__req").text == seed_mod.REQ_LEAN)
    why = lean.select_one(".pf-cr__why").text
    assert "The story behind this was deleted." in why
    assert "Kestrel's own read applies again" in why
    plan = next(c for c in soup.select(".pf-cr")
                if c.select_one(".pf-cr__req").text == seed_mod.REQ_PLAN)
    assert "Added from a role you removed" in plan.select_one(".pf-cr__meta").text


def test_the_empty_state(world):
    from kestrel.web.app import build_app

    con = sqlite3.connect(paths.db_path())
    con.execute("delete from requirement_correction")
    con.commit()
    con.close()
    with TestClient(build_app(worker=False)) as c:
        soup = _soup(c.get("/profile?tab=corrections").text)
    assert not soup.select(".pf-cr")
    none = soup.select_one("#pf-cr-none")
    assert not none.has_attr("hidden")
    assert none.text == ("No corrections yet. When you correct what Kestrel concluded about "
                         "you on a role, it appears here.")
    assert soup.select_one("#pf-cr-nomatch").has_attr("hidden")
    assert soup.select_one('.pf-tabs a[data-tab="corrections"] .pf-tabs__n') is None


# ------------------------------------------------------------------------------- search
@pytest.mark.parametrize("q, expected", [
    ("carrier", {seed_mod.REQ_CARRIER}),                      # requirement
    ("some+of+it", {seed_mod.REQ_PLAN}),                      # the reason, as its chip words it
    ("someone+else", {seed_mod.REQ_CARRIER}),                 # the note
    ("pinegate", set()),                                      # the company is not searched
    ("warehouse", {seed_mod.REQ_TEAMS}),                      # the role title
    ("CARRIER+vendor", {seed_mod.REQ_CARRIER}),               # every word, any case
])
def test_search_keeps_the_cards_that_hold_every_word(client, q, expected):
    soup = _tab(client, q)
    shown = {c.select_one(".pf-cr__req").text for c in soup.select(".pf-cr:not(.is-filtered)")}
    assert shown == expected
    # The rest are still in the page, folded away, so clearing the box brings them back.
    assert len(soup.select(".pf-cr")) == 4
    nomatch = soup.select_one("#pf-cr-nomatch")
    assert nomatch.has_attr("hidden") == bool(expected)


def test_no_match_says_what_was_typed(client):
    soup = _tab(client, "zzzz")
    nomatch = soup.select_one("#pf-cr-nomatch")
    assert not nomatch.has_attr("hidden")
    assert nomatch.text == "No corrections match “zzzz”."
    assert soup.select_one("#pf-cr-none").has_attr("hidden")
    assert soup.select_one("input[type=search]")["value"] == "zzzz"


# ------------------------------------------------------------------------------- change
def test_save_changes_state_and_note_and_returns_the_card_with_the_rescore_offer(client, world):
    cid = _cid(world, seed_mod.REQ_CARRIER)
    before = _row(cid)
    r = _change(client, cid, "part", "  Ran two carrier reviews.  ")
    assert r.status_code == 200
    soup = _soup(r.text)
    card = soup.select_one(f"#pf-cr-{cid}")
    assert card.select_one(".pf-cr__y").text == "You: Partly met"
    assert card.select_one("s.pf-cr__k").text == "Kestrel: Met"  # Kestrel's read is untouched
    assert card.select_one(".pf-cr__note").text == "“Ran two carrier reviews.”"
    after = _row(cid)
    assert (after["operator_state"], after["note"]) == ("part", "Ran two carrier reviews.")
    assert after["id"] == cid and after["created_at"] == before["created_at"]
    assert after["reason"] == before["reason"] and after["kestrel_state"] == "met"
    assert after["updated_at"] > before["updated_at"]
    # The offer rides along out of band: a link to the rescore dialog for the two open roles.
    offer = soup.select_one("#pf-cr-offer[hx-swap-oob]")
    link = offer.select_one("a")
    assert link.text == "Rescore 2 roles"
    assert link["href"].startswith("/rescore/choose?")
    assert f"opp_ids={world['role_a']}%2C{world['role_c']}" in link["href"]
    assert "page=1" in link["href"] and "next=%2Fprofile%3Ftab%3Dcorrections" in link["href"]
    ev = _event(r)
    assert ev["message"] == "Saved. Kestrel can rescore the 2 roles that use it."
    assert ev["kind"] == "change" and ev["id"] == cid and ev["undoUrl"].endswith("/restore")


def test_save_clears_kestrel_agrees_now_until_the_next_read(client, world):
    cid = _cid(world, seed_mod.REQ_LEAN)
    assert _row(cid)["agrees_now"] == 1
    r = _change(client, cid, "part", "")
    assert _soup(r.text).select_one(".pf-cr__ag") is None
    assert _row(cid)["agrees_now"] == 0


def test_save_rejects_a_state_that_is_not_one_of_the_three_and_a_missing_row(client, world):
    cid = _cid(world, seed_mod.REQ_CARRIER)
    before = _row(cid)
    assert _change(client, cid, "maybe").status_code == 400
    assert _change(client, cid, "").status_code == 400
    assert _row(cid) == before
    assert _change(client, 99999, "met").status_code == 404


def test_a_very_long_note_is_cut_not_refused(client, world):
    cid = _cid(world, seed_mod.REQ_CARRIER)
    assert _change(client, cid, "met", "x" * 5000).status_code == 200
    assert len(_row(cid)["note"]) == 1000


def test_save_with_no_open_role_offers_nothing_and_says_just_saved(client, world):
    con = sqlite3.connect(paths.db_path())
    con.execute("update opportunity set archived_at='2026-02-02T00:00:00'")
    con.commit()
    con.close()
    r = _change(client, _cid(world, seed_mod.REQ_CARRIER), "met")
    assert _event(r)["message"] == "Saved."
    assert not _soup(r.text).select_one("#pf-cr-offer a")


# ------------------------------------------------------------------------------- undo and restore
def _restore(client, ev: dict):
    return client.post(ev["undoUrl"].replace("/restore", "/restore"),
                       data={"undo": ev["undo"], "stamp": ev["stamp"]},
                       headers={"HX-Request": "true"})


def test_undo_of_a_save_restores_the_exact_row(client, world):
    cid = _cid(world, seed_mod.REQ_CARRIER)
    before = _row(cid)
    r = _change(client, cid, "gap", "A new note.")
    assert _row(cid) != before
    back = _restore(client, _event(r))
    assert back.status_code == 200
    assert _row(cid) == before  # every column, timestamps included
    card = _soup(back.text).select_one(f"#pf-cr-{cid}")
    assert card.select_one(".pf-cr__y").text == "You: Not met"
    assert card.select_one(".pf-cr__note").text == "“I chose carriers once; someone else ran them.”"


def test_remove_deletes_the_row_and_undo_brings_back_the_exact_row(client, world):
    cid = _cid(world, seed_mod.REQ_LEAN)
    before = _row(cid)
    assert before["agrees_now"] == 1
    r = client.post(f"/profile/corrections/{cid}/remove", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert _row(cid) is None
    ev = _event(r)
    assert ev["message"] == "Correction removed. Kestrel goes back to its own read."
    assert ev["kind"] == "remove"
    # The card swaps out for nothing; the rescore offer rides along out of band.
    soup = _soup(r.text)
    assert not soup.select(".pf-cr")
    assert soup.select_one("#pf-cr-offer[hx-swap-oob] a").text == "Rescore 2 roles"
    back = _restore(client, ev)
    assert back.status_code == 200
    assert _row(cid) == before
    assert _soup(back.text).select_one(f"#pf-cr-{cid} .pf-cr__ag") is not None


def test_removing_a_row_twice_and_a_missing_row_are_404(client, world):
    cid = _cid(world, seed_mod.REQ_LEAN)
    assert client.post(f"/profile/corrections/{cid}/remove").status_code == 200
    assert client.post(f"/profile/corrections/{cid}/remove").status_code == 404


def test_a_stale_undo_is_refused_and_changes_nothing(client, world):
    cid = _cid(world, seed_mod.REQ_CARRIER)
    first = _event(_change(client, cid, "gap", "one"))
    _change(client, cid, "part", "two")  # a later change, e.g. from the role page
    now = _row(cid)
    r = _restore(client, first)
    assert r.status_code == 409 and r.text.startswith("Undo is no longer available")
    assert _row(cid) == now


def test_undo_of_a_remove_is_refused_when_a_newer_correction_took_the_requirement(client, world):
    from kestrel.db.engine import get_sessionmaker
    from kestrel.learning import corrections as corr

    cid = _cid(world, seed_mod.REQ_CARRIER)
    ev = _event(client.post(f"/profile/corrections/{cid}/remove"))

    async def again():
        async with get_sessionmaker()() as s:
            await corr.save_correction(
                s, opportunity_id=world["role_a"], requirement_text=seed_mod.REQ_CARRIER,
                kestrel_state="met", reason="overstated")

    asyncio.run(again())
    rows = _rows()
    r = _restore(client, ev)
    assert r.status_code == 409
    assert _rows() == rows


def test_the_browser_gets_a_token_never_the_rows(client, world):
    cid = _cid(world, seed_mod.REQ_CARRIER)
    for ev in (_event(_change(client, cid, "gap", "n")),
               _event(client.post(f"/profile/corrections/{cid}/remove"))):
        assert not ev["undo"].lstrip().startswith(("{", "["))
        assert "requirement_hash" not in ev["undo"]


def test_a_forged_restore_writes_nothing_and_invents_no_row(client, world):
    """The restore trusts nothing the browser posts: a snapshot with another reason, basis, hash,
    created_at, an overlong note, or an id that was never issued (9999) is refused, as is a token
    that was never handed out, one for another row, and one used twice."""
    cid = _cid(world, seed_mod.REQ_CARRIER)
    other = _cid(world, seed_mod.REQ_LEAN)
    snap = _row(cid)
    ev = _event(client.post(f"/profile/corrections/{cid}/remove"))
    rows = _rows()
    forged = [
        {"written": [], "before": [{**snap, "reason": "zzz", "basis": "x",
                                    "requirement_hash": "0" * 40, "created_at": "1999-01-01",
                                    "note": "FORGED " * 300}]},
        {"written": [], "before": [{**snap, "id": 9999, "requirement_hash": "1" * 40,
                                    "requirement_text": "Invented"}]},
        {"written": [], "before": [{**snap, "id": other}]},
        {"written": [], "before": []},
        "not json at all",
        "9999",
        "",
    ]
    for payload in forged:
        body = payload if isinstance(payload, str) else json.dumps(payload)
        for target in (cid, 9999, other):
            r = client.post(f"/profile/corrections/{target}/restore",
                            data={"undo": body, "stamp": ""})
            assert r.status_code == 409, (payload, target)
    # the real token is refused for another row, and still works once for its own
    assert client.post(f"/profile/corrections/{other}/restore",
                       data={"undo": ev["undo"], "stamp": ""}).status_code == 409
    assert _rows() == rows
    assert _restore(client, ev).status_code == 200
    again = _rows()
    assert _restore(client, ev).status_code == 409  # one use
    assert _rows() == again
    assert all(r["id"] != 9999 for r in again)


def test_a_profile_token_is_refused_by_the_role_page_and_the_other_way_round(client, world):
    cid = _cid(world, seed_mod.REQ_CARRIER)
    ev = _event(client.post(f"/profile/corrections/{cid}/remove"))
    r = client.post(f"/opportunities/{world['role_a']}/role/correction/{cid}/undo",
                    data={"token": ev["undo"]})
    assert "data-undo-msg" in r.text and _row(cid) is None
    assert _restore(client, ev).status_code == 200


def test_change_and_remove_refuse_a_posting_row_before_writing(client, world):
    """A posting (They mean something else) note is changed on its role page: Profile neither
    edits nor removes it, and refuses before it writes."""
    from kestrel.db.engine import get_sessionmaker
    from kestrel.learning import corrections as corr

    async def mk():
        async with get_sessionmaker()() as s:
            row, _ = await corr.save_correction(
                s, opportunity_id=world["role_a"], requirement_text=seed_mod.REQ_PLAN,
                kestrel_state="gap", reason="misread", note="they mean budgets")
            return row.id

    pid = asyncio.run(mk())
    before = _rows()
    assert client.post(f"/profile/corrections/{pid}",
                       data={"operator_state": "met", "note": "x"}).status_code == 404
    assert client.post(f"/profile/corrections/{pid}/remove").status_code == 404
    assert _rows() == before


def test_a_save_and_a_remove_never_change_the_evaluation_labels_or_the_roles(client, world):
    tables = ("evaluation", "label", "verdict_override", "opportunity", "story")
    snapshot = {t: _rows(t) for t in tables}
    cid = _cid(world, seed_mod.REQ_CARRIER)
    ev = _event(_change(client, cid, "gap", "note"))
    _restore(client, ev)
    ev2 = _event(client.post(f"/profile/corrections/{cid}/remove"))
    _restore(client, ev2)
    assert {t: _rows(t) for t in tables} == snapshot


# ------------------------------------------------------------------------------- invariants
def test_nothing_typed_here_reaches_a_resume(client, world):
    """A note typed in a correction is not a doc_variant: the rendered resume's bullets are the
    variant texts only, byte for byte, and the page's HTML is identical before and after."""
    from kestrel.db.engine import get_sessionmaker
    from kestrel.docs.catalog import load_catalog, resolve
    from kestrel.docs.render import bullets_in_html, render_html
    from tests import profile_seed

    async def html() -> str:
        async with get_sessionmaker()() as s:
            catalog = await load_catalog(s, world["master"])
        return render_html(resolve(catalog, {}))

    before = asyncio.run(html())
    typed = "Led a unique corrections-only sentence about harbor logistics."
    cid = _cid(world, seed_mod.REQ_CARRIER)
    _change(client, cid, "met", typed)
    after = asyncio.run(html())
    assert after == before
    assert typed not in after
    assert [t for _k, t in bullets_in_html(after)] == list(profile_seed.BULLETS)
    variants = {r["text"] for r in _rows("doc_variant")}
    assert {t for _k, t in bullets_in_html(after)} <= variants
    assert typed not in {r["text"] for r in _rows("doc_variant")}


def test_confidential_mode_redacts_the_company_and_names_in_the_text(world):
    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import DocMaster, DocSection, Setting
    from kestrel.web.app import build_app

    async def turn_on():
        async with get_sessionmaker()() as s:
            s.add(Setting(key="confidential_mode", value=True))
            m = DocMaster(track="employment", doc_kind="resume", name="Resume")
            s.add(m)
            await s.flush()
            s.add(DocSection(doc_master_id=m.id, section_kind="role", title="Experience",
                             org_label="Larkspur Logistics",
                             org_label_redacted="Current Employer", end_date="present"))
            await s.commit()

    asyncio.run(turn_on())
    con = sqlite3.connect(paths.db_path())
    con.execute("update requirement_correction set note=? where requirement_text=?",
                ("Larkspur Logistics ran the carriers.", seed_mod.REQ_CARRIER))
    con.commit()
    con.close()
    from kestrel.db.engine import forget_engine

    forget_engine()
    with TestClient(build_app(worker=False)) as c:
        html = c.get("/profile?tab=corrections").text
        searched = c.get("/profile?tab=corrections&q=larkspur").text
    assert "Larkspur" not in html and "larkspur" not in html.lower()
    assert "Current Employer" in html
    # The employer name finds nothing either: search reads what the page shows.
    assert not _soup(searched).select(".pf-cr:not(.is-filtered)")
    # The other companies are not the operator's current employer: shown as written.
    assert "Pinegate Supply" in html


def test_a_saved_card_is_redacted_too(world):
    from kestrel.db.engine import forget_engine, get_sessionmaker
    from kestrel.db.models import DocMaster, DocSection, Setting
    from kestrel.web.app import build_app

    async def turn_on():
        async with get_sessionmaker()() as s:
            s.add(Setting(key="confidential_mode", value=True))
            m = DocMaster(track="employment", doc_kind="resume", name="Resume")
            s.add(m)
            await s.flush()
            s.add(DocSection(doc_master_id=m.id, section_kind="role", title="Experience",
                             org_label="Larkspur Logistics",
                             org_label_redacted="Current Employer", end_date="present"))
            await s.commit()

    asyncio.run(turn_on())
    forget_engine()
    with TestClient(build_app(worker=False)) as c:
        r = _change(c, _cid(world, seed_mod.REQ_CARRIER), "met", "Larkspur Logistics did it.")
    assert "Larkspur" not in r.text
    assert "Current Employer" in r.text


def test_confidential_mode_never_saves_the_redacted_note_over_the_real_one(world):
    """A note naming the current employer is shown redacted and read only; Save with the state
    changed and the textarea as shown keeps the stored note (D-UI-320)."""
    from kestrel.db.engine import forget_engine, get_sessionmaker
    from kestrel.db.models import DocMaster, DocSection, Setting
    from kestrel.web.app import build_app

    async def turn_on():
        async with get_sessionmaker()() as s:
            s.add(Setting(key="confidential_mode", value=True))
            m = DocMaster(track="employment", doc_kind="resume", name="Resume")
            s.add(m)
            await s.flush()
            s.add(DocSection(doc_master_id=m.id, section_kind="role", title="Experience",
                             org_label="Larkspur Logistics",
                             org_label_redacted="Current Employer", end_date="present"))
            await s.commit()

    asyncio.run(turn_on())
    real = "Larkspur Logistics ran the carriers."
    con = sqlite3.connect(paths.db_path())
    con.execute("update requirement_correction set note=? where requirement_text=?",
                (real, seed_mod.REQ_CARRIER))
    con.commit()
    con.close()
    forget_engine()
    cid = _cid(world, seed_mod.REQ_CARRIER)
    with TestClient(build_app(worker=False)) as c:
        card = _soup(c.get("/profile?tab=corrections").text).select_one(f"#pf-cr-{cid}")
        ta = card.select_one("textarea[name=note]")
        assert "readonly" in ta.attrs and "Larkspur" not in ta.text
        r = _change(c, cid, "part", ta.text)
        assert r.status_code == 200
    con = sqlite3.connect(paths.db_path())
    row = con.execute("select note, operator_state from requirement_correction where id=?",
                      (cid,)).fetchone()
    con.close()
    assert row == (real, "part")


# ------------------------------------------------------------------------------- no online send
ROUTES = (
    ("POST", "/profile/corrections/{cid}", {"operator_state": "part", "note": "n"}),
    ("POST", "/profile/corrections/{cid}/remove", {}),
    ("POST", "/profile/corrections/{cid}/restore", {"undo": "{}", "stamp": ""}),
    ("GET", "/profile?tab=corrections&q=lean", None),
)


@pytest.mark.parametrize("method, path, data", ROUTES)
def test_no_correction_route_calls_a_model_touches_the_network_or_queues_a_task(
        method, path, data, client, world, request, monkeypatch):
    """Invariant: nothing here is sent online, with an online provider connected and without a
    confirmed dialog anywhere. A rescore is only offered, as a link to /rescore/choose."""
    rec = request.getfixturevalue("recorder")
    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")
    url = path.format(cid=_cid(world, seed_mod.REQ_CARRIER))
    before = asyncio.run(_count_tasks())
    r = (client.get(url) if method == "GET"
         else client.post(url, data=data, headers={"HX-Request": "true"}))
    assert r.status_code < 500
    _assert_nothing_happened(rec, before, asyncio.run(_count_tasks()), f"{method} {url}")


def test_the_corrections_templates_hold_no_send_and_offer_the_rescore_dialog_only():
    from tests.test_contracts import SEND_MARKERS

    names = ("profile_corrections.html", "profile_corrections_card.html",
             "profile_corrections_offer.html")
    forms = set()
    for name in names:
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert not SEND_MARKERS.search(text), name
        assert "confirmed" not in text, name
        assert 'hx-post="/rescore' not in text and "action=\"/rescore" not in text, name
        forms |= set(re.findall(r'hx-post="([^"]+)"', text))
    assert {re.sub(r"\{\{[^}]*\}\}", "{id}", f) for f in forms} == {
        "/profile/corrections/{id}", "/profile/corrections/{id}/remove"}
    offer = (TEMPLATES / "profile_corrections_offer.html").read_text(encoding="utf-8")
    assert 'href="{{ offer.href }}"' in offer


def test_each_correction_post_is_a_route_before_the_profile_section_catch_all():
    """/profile/{track}/{section} would take /profile/corrections/{id} if it came first."""
    from kestrel.web.app import build_app
    from tests.test_contracts import _walk

    order = [p for p, _r in _walk(build_app(worker=False).routes)]
    posts = [i for i, p in enumerate(order) if p == "/profile/corrections/{cid}"]
    catch = [i for i, p in enumerate(order) if p == "/profile/{track}/{section}"]
    assert posts and catch
    assert posts[0] < catch[0]
