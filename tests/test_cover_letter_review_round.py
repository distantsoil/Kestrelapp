"""Review round on #124 (D-LETTER-5 to D-LETTER-7): the letter move survives an interrupted or
messy upgrade, a role never has two current letters, Undo asks first and names the draft it
deletes, the keep and Undo writes are guarded against a second request, a claim resolves only to
what the prompt carried, the ask-first panel names a story's title, a downgrade shows the current
letter, and the links around a letter stay true. Invented text only; nothing reaches the network."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Letter, Pursuit, Setting, StoryUsage
from kestrel.docs import letter_store
from tests import role_seed
from tests.letter_helpers import drain_letters, form_fields, generate
from tests.test_cover_letter_guards import (
    ACCEPTED,
    GEN,
    SHORT,
    URL,
    Model,
    _add_story,
    _client,
    _db,
    _draft,
    _go,
    _go_online,
    _letters,
    _status,
    _text,
)
from tests.test_selection_override_detach import _ins

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


@pytest.fixture
def ids():
    return asyncio.run(role_seed.seed_async())


# ======================================================== 1. the move is safe to run again
def _seed_0027(db) -> None:
    _go(db, "up", "0027")
    con = sqlite3.connect(db)
    _ins(con, "opportunity", id=1, track="employment", opp_type="job_posting", title="Planner",
         org_name="Invented Co", url="https://e.test/1", stage="t2_done")
    _ins(con, "opportunity", id=2, track="employment", opp_type="job_posting", title="Analyst",
         org_name="Invented Co", url="https://e.test/2", stage="t2_done")
    _ins(con, "doc_master", id=1, track="employment", name="t", doc_kind="resume", header="{}",
         summary_variants="[]", skill_groups="[]")
    con.commit()
    con.close()


def _letter_render(db, rid: int, opp: int, cfg: dict, template: str = "cover_letter") -> None:
    con = sqlite3.connect(db)
    _ins(con, "doc_render", id=rid, opportunity_id=opp, doc_master_id=1, template=template,
         config=json.dumps(cfg), created_at=f"2026-11-0{rid % 9 + 1}T00:00:00+00:00")
    con.commit()
    con.close()


def _body(n: int) -> dict:
    return {"kind": "cover_letter", "body_md": f"Dear Hiring Team,\n\nInvented letter {n}.",
            "claims_used": [], "words": 6}


def test_an_upgrade_interrupted_after_the_table_was_made_moves_every_letter_on_rerun(tmp_path):
    """D-LETTER-5: SQLite can commit the CREATE TABLE while the move rolls back. The rerun must
    not take the table as proof that the letters were moved."""
    done = tmp_path / "done.db"
    _go(done, "up", "0028")
    ddl = sqlite3.connect(done).execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='letter'").fetchone()[0]
    db = tmp_path / "m.db"
    _seed_0027(db)
    for rid in (7, 8):
        _letter_render(db, rid, rid - 6, _body(rid))
    con = sqlite3.connect(db)
    con.execute(ddl)  # what an interrupted run left behind: the table, still at 0027
    # ...and a letter already in it from an earlier pass: it is never moved a second time.
    con.execute("INSERT INTO letter (render_id, opportunity_id, kind, status, body_md,"
                " word_count, edited, created_at, updated_at) VALUES (7, 1, 'cover_letter',"
                " 'current', 'Dear Hiring Team,\n\nInvented letter 7.', 6, 0, 'x', 'x')")
    con.commit()
    con.close()
    _go(db, "up", "0028")
    con = sqlite3.connect(db)
    moved = dict(con.execute("SELECT render_id, body_md FROM letter").fetchall())
    assert con.execute("SELECT COUNT(*) FROM letter").fetchone()[0] == 2
    assert moved == {7: "Dear Hiring Team,\n\nInvented letter 7.",
                     8: "Dear Hiring Team,\n\nInvented letter 8."}


def test_a_letter_with_unreadable_values_does_not_stop_the_others(tmp_path):
    db = tmp_path / "m.db"
    _seed_0027(db)
    _letter_render(db, 7, 1, {**_body(7), "words": "many", "claims_used": "not a list",
                              "flags": {"odd": True}})
    _letter_render(db, 8, 2, _body(8))
    _go(db, "up", "0028")
    con = sqlite3.connect(db)
    rows = {r[0]: r[1:] for r in con.execute(
        "SELECT render_id, word_count, claims_used, flags FROM letter")}
    assert set(rows) == {7, 8}
    assert rows[7] == (6, "[]", "[]")  # counted again from the text; the rest stored empty
    assert rows[8][0] == 6


# ================================================ 2. at most one current letter per role
def test_the_move_gives_a_role_with_several_letters_one_current(tmp_path):
    db = tmp_path / "m.db"
    _seed_0027(db)
    for rid in (7, 8, 9):
        _letter_render(db, rid, 1, _body(rid))
    _letter_render(db, 10, 2, _body(10))
    _letter_render(db, 11, 2, _body(11))
    con = sqlite3.connect(db)
    _ins(con, "pursuit", id=1, opportunity_id=1, track="employment", status="applied",
         cover_letter_id=8)
    con.commit()
    con.close()
    _go(db, "up", "0028")
    con = sqlite3.connect(db)
    status = dict(con.execute("SELECT render_id, status FROM letter").fetchall())
    # The application's letter for role 1; the newest for role 2; the rest kept, never shown.
    assert status == {7: "older", 8: "current", 9: "older", 10: "older", 11: "current"}
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE letter SET status = 'current' WHERE render_id = 7")


def test_a_draft_asked_to_be_current_beside_a_current_one_waits_as_a_proposal(ids, monkeypatch):
    """The Undo-then-redraft sequence lands a finished draft whose target is gone; it must never
    become a second current letter or replace the person's draft (D-LETTER-5)."""
    opp = ids["strong"]
    with _client() as c:
        first = _draft(c, opp, monkeypatch)

    async def make(s):
        render, letter, _stale = await letter_store.create(
            s, opp_id=opp, master_id=ids["master"], kind="cover_letter", body_md=SHORT,
            claims_used=[], flags=[], prompt_version="cover/v4", extras={},
            status=letter_store.CURRENT)
        await s.commit()
        return render.id, letter.status

    new, status = _db(make)
    assert status == letter_store.PROPOSED
    assert _status(opp) == {"current": [first], "proposed": [new]}

    async def force(s):
        s.add(Letter(render_id=new, opportunity_id=opp, kind="cover_letter", body_md="x",
                     status="current"))
        await s.flush()

    with pytest.raises(IntegrityError):  # the index refuses a second current letter outright
        _db(force)


def test_undo_and_keep_previous_wait_while_a_redraft_is_running(ids, monkeypatch):
    opp = ids["strong"]
    base = URL.format(opp)
    with _client() as c:
        old = _draft(c, opp, monkeypatch)
        generate(c, GEN.format(opp), form_fields(c.post(f"{base}/{old}/redraft").text))
        new = _status(opp)["proposed"][0]
        c.post(f"{base}/{new}/keep-new")
        # Draft again on the kept draft is queued; the worker has not run it yet.
        card = c.post(GEN.format(opp), data=form_fields(c.post(f"{base}/{new}/redraft").text))
        assert "data-watching" in card.text
        r = c.post(f"{base}/{new}/undo", data={"sure": "1"})
        assert "being written" in json.loads(r.headers["hx-trigger"])["k-toast"]["message"]
        assert _status(opp) == {"current": [new], "previous": [old]}
        drain_letters()
        st = _status(opp)
        assert st["current"] == [new] and st["previous"] == [old] and len(st["proposed"]) == 1
        latest = st["proposed"][0]
        stuck = c.post(GEN.format(opp), data=form_fields(c.post(f"{base}/{new}/redraft").text))
        assert "data-watching" in stuck.text
        kp = c.post(f"{base}/{latest}/keep-previous")
        assert "being written" in json.loads(kp.headers["hx-trigger"])["k-toast"]["message"]
    assert latest in _status(opp)["proposed"]


# ============================================================ 3. Undo asks first
def test_undo_asks_first_and_names_the_draft_it_deletes(ids, monkeypatch):
    opp = ids["strong"]
    base = URL.format(opp)
    with _client() as c:
        old = _draft(c, opp, monkeypatch)
        generate(c, GEN.format(opp), form_fields(c.post(f"{base}/{old}/redraft").text))
        new = _status(opp)["proposed"][0]
        sheet = c.post(f"{base}/{new}/keep-new").text
        c.post(f"{base}/{new}/save", data={"body_md": SHORT + "\n\nMy own invented edit."})
        bare = c.post(f"{base}/{new}/undo")  # no answer to the sheet's question
        wrong = c.post(f"{base}/{old}/undo", data={"sure": "1"})  # not the draft on screen
    soup = BeautifulSoup(sheet, "html.parser")
    button = soup.select_one('[data-testid="cover-letter-undo"]')
    assert button.get("hx-post") is None and button["data-cl-ask"] == "undo"
    ask = soup.select_one('[data-cl-confirm="undo"]')
    assert "with any edits you made to it, is deleted" in ask.get_text(" ")
    assert ask.select_one("form")["hx-post"].endswith(f"/{new}/undo")
    assert "Nothing changed" in json.loads(bare.headers["hx-trigger"])["k-toast"]["message"]
    assert "Nothing changed" in json.loads(wrong.headers["hx-trigger"])["k-toast"]["message"]
    assert _status(opp) == {"current": [new], "previous": [old]}  # the edited draft is kept


# ======================================================= 4. guarded against a second request
def _keep_cycle(c, opp, monkeypatch) -> tuple[int, int]:
    base = URL.format(opp)
    old = _draft(c, opp, monkeypatch)
    generate(c, GEN.format(opp), form_fields(c.post(f"{base}/{old}/redraft").text))
    return old, _status(opp)["proposed"][0]


def test_keep_previous_never_deletes_a_draft_a_concurrent_keep_new_made_current(
    ids, monkeypatch
):
    opp = ids["strong"]
    with _client() as c:
        old, new = _keep_cycle(c, opp, monkeypatch)
    real = letter_store.letter_for

    async def racing(s, rid):
        row = await real(s, rid)  # this request read "proposed"
        async with get_sessionmaker()() as other:  # a second request keeps it meanwhile
            await letter_store.keep_new(other, opp, rid)
        return row

    monkeypatch.setattr(letter_store, "letter_for", racing)

    async def go(s):
        return await letter_store.keep_previous(s, opp, new)

    assert _db(go) is None
    monkeypatch.setattr(letter_store, "letter_for", real)
    assert _status(opp) == {"current": [new], "previous": [old]}


def test_two_undos_at_once_bring_back_one_draft_and_delete_only_one(ids, monkeypatch):
    opp = ids["strong"]
    with _client() as c:
        old, new = _keep_cycle(c, opp, monkeypatch)
        c.post(f"{URL.format(opp)}/{new}/keep-new")
    real = letter_store.render_in
    raced = []

    async def racing(s, opp_id, status):
        row = await real(s, opp_id, status)
        if status == letter_store.PREVIOUS and not raced:
            raced.append(1)
            async with get_sessionmaker()() as other:  # the other request finishes first
                await letter_store.undo_keep(other, opp_id, new)
        return row

    monkeypatch.setattr(letter_store, "render_in", racing)

    async def go(s):
        return await letter_store.undo_keep(s, opp, new)

    assert _db(go) is None
    monkeypatch.setattr(letter_store, "render_in", real)
    assert _status(opp) == {"current": [old]}


def test_undo_changes_nothing_when_the_previous_draft_went_meanwhile(ids, monkeypatch):
    opp = ids["strong"]
    with _client() as c:
        old, new = _keep_cycle(c, opp, monkeypatch)
        c.post(f"{URL.format(opp)}/{new}/keep-new")
    real = letter_store.render_in
    raced = []

    async def racing(s, opp_id, status):
        row = await real(s, opp_id, status)
        if status == letter_store.PREVIOUS and not raced:
            raced.append(1)
            async with get_sessionmaker()() as other:  # the previous draft is discarded meanwhile
                await letter_store.discard(other, opp_id, old)
        return row

    monkeypatch.setattr(letter_store, "render_in", racing)

    async def go(s):
        return await letter_store.undo_keep(s, opp, new)

    assert _db(go) is None
    monkeypatch.setattr(letter_store, "render_in", real)
    assert _status(opp) == {"current": [new]}  # the kept draft stays; never no draft at all


# =========================================== 5. a claim resolves only to what was sent
def test_a_real_story_key_that_was_not_in_the_prompt_is_unverified(ids, monkeypatch):
    for i in range(2):  # these two share the posting's words, so they are the two sent
        _add_story(**{**ACCEPTED, "title": f"Invented planning story {i}",
                      "action": "I rebuilt the demand forecast for distribution centers.",
                      "result": "Late shipments fell."})
    unsent = _add_story(**{**ACCEPTED, "title": "Invented garden story",
                           "action": "I planted invented tulips.", "result": "They grew."})
    fake = Model(monkeypatch, claims=[f"story-{unsent}"])
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    assert f"[story-{unsent}]" not in fake.prompt(0)
    assert "unverified claims present" in r.text.lower()
    # Flagged as a claim that matches nothing sent, not only for some other detail.
    soup = BeautifulSoup(r.text, "html.parser")
    assert soup.select_one('[data-testid="cover-letter-unresolved"]') is not None
    assert soup.select_one(f'.cl-src[data-cl-src="story-{unsent}"]') is None

    async def usage(s):
        return list((await s.execute(select(StoryUsage).where(
            StoryUsage.story_id == unsent))).scalars())

    assert _db(usage) == []


# ======================================================= 6. the panel names the title
def test_the_ask_first_panel_names_each_part_of_a_story_that_is_sent(ids):
    _go_online()
    with _client() as c:
        panel = _text(c.get(URL.format(ids["strong"])).text)
    assert ("for each story: a reference key, its title, its first action sentence and its "
            "result") in panel


# ================================================ 7. a downgrade shows the current letter
def test_a_downgrade_keeps_the_current_letter_shown_and_drops_the_drafts_it_cannot_show(tmp_path):
    db = tmp_path / "m.db"
    _seed_0027(db)
    _letter_render(db, 7, 1, _body(7))
    _letter_render(db, 20, 2, {"kind": "engagement_outline", "body_md": "An invented outline.",
                               "exclusions": ["a", "b", "c"]}, template="engagement_outline")
    con = sqlite3.connect(db)
    _ins(con, "pursuit", id=1, opportunity_id=1, track="employment", status="applied",
         cover_letter_id=7)
    con.commit()
    con.close()
    _go(db, "up", "0028")
    con = sqlite3.connect(db)
    for rid, status in ((8, "previous"), (9, "proposed")):
        _ins(con, "doc_render", id=rid, opportunity_id=1, doc_master_id=1,
             template="cover_letter", config='{"kind": "cover_letter"}')
        _ins(con, "letter", render_id=rid, opportunity_id=1, kind="cover_letter", status=status,
             body_md=f"Invented {status} draft.", word_count=3, edited=0,
             created_at="x", updated_at="x")
    con.commit()
    con.close()
    _go(db, "down", "0027")
    con = sqlite3.connect(db)
    renders = {r[0]: json.loads(r[1]) for r in con.execute(
        "SELECT id, config FROM doc_render WHERE template IS NOT NULL")}
    assert set(renders) == {7, 20}  # the proposed and previous drafts are gone
    newest = con.execute("SELECT MAX(id) FROM doc_render WHERE opportunity_id = 1"
                         " AND template = 'cover_letter'").fetchone()[0]
    assert newest == 7 and renders[7]["body_md"].endswith("Invented letter 7.")
    assert con.execute("SELECT cover_letter_id FROM pursuit WHERE id = 1").fetchone()[0] == 7
    assert renders[20]["body_md"] == "An invented outline." and renders[20]["exclusions"]


# ============================================================ 8. the links stay true
def test_a_pursuit_undo_never_points_the_application_at_a_stale_or_deleted_letter(
    ids, monkeypatch
):
    from kestrel.services import pursuit_undo

    opp = ids["strong"]
    base = URL.format(opp)
    with _client() as c:
        old, new = _keep_cycle(c, opp, monkeypatch)
        c.post(f"{base}/{new}/keep-new")  # `old` is now only the previous draft

    async def restore(s, saved_link: int):
        await s.execute(Pursuit.__table__.delete().where(Pursuit.opportunity_id == opp))
        await s.commit()
        data = {"opportunity_id": opp, "opp_after": {},
                "pursuit": {"id": 999, "opportunity_id": opp, "track": "employment",
                            "status": "shortlisted", "cover_letter_id": saved_link}}
        track, _msg = await pursuit_undo._restore_removed(s, data)
        await s.commit()
        p = (await s.execute(select(Pursuit).where(Pursuit.opportunity_id == opp))).scalars()
        return track, p.one().cover_letter_id

    # A saved link to what is now the previous draft, and one to a render that no longer exists:
    # both become the role's current letter, never a stale or broken link.
    assert _db(lambda s: restore(s, old)) == ("employment", new)
    assert _db(lambda s: restore(s, 987654)) == ("employment", new)


def test_a_letter_drafted_before_shortlisting_belongs_to_the_application(ids, monkeypatch):
    opp = ids["strong"]
    with _client() as c:
        _draft(c, opp, monkeypatch)
        assert _letters(opp)[0].pursuit_id is None
        c.post(f"/opportunities/{opp}/role/shortlist")

    async def pid(s):
        return (await s.execute(select(Pursuit.id).where(Pursuit.opportunity_id == opp))).scalar()

    assert _letters(opp)[0].pursuit_id == _db(pid) is not None


def _usage_count(story_id: int) -> int:
    async def go(s):
        return len(list((await s.execute(select(StoryUsage).where(
            StoryUsage.story_id == story_id))).scalars()))

    return _db(go)


STORY_LINE = "I rerouted the invented night shift and invented returns fell by a third."


def test_deleting_the_resume_a_letter_came_from_takes_its_story_usage_too(ids, monkeypatch):
    good = _add_story(**ACCEPTED)
    with _client() as c:
        Model(monkeypatch, bodies=(SHORT + "\n\n" + STORY_LINE,), claims=[f"story-{good}"])
        generate(c, GEN.format(ids["strong"]), {})
        assert _usage_count(good) == 1
        r = c.post(f"/documents/{ids['master']}/delete")
        assert r.status_code < 400
    assert _letters(ids["strong"]) == [] and _usage_count(good) == 0


def test_an_edit_that_removes_the_story_sentence_stops_counting_the_story(ids, monkeypatch):
    good = _add_story(**ACCEPTED)
    with _client() as c:
        Model(monkeypatch, bodies=(SHORT + "\n\n" + STORY_LINE,), claims=[f"story-{good}"])
        generate(c, GEN.format(ids["strong"]), {})
        rid = _status(ids["strong"])["current"][0]
        assert _usage_count(good) == 1
        c.post(f"{URL.format(ids['strong'])}/{rid}/save",
               data={"body_md": SHORT + "\n\nA line of my own about planning."})
        assert _usage_count(good) == 0
        c.post(f"{URL.format(ids['strong'])}/{rid}/save", data={"body_md": SHORT + "\n\n"
                                                                + STORY_LINE})
    assert _usage_count(good) == 1  # put back, counted again


# ================================== privacy: confidential mode covers story text when sent
def test_confidential_mode_hides_the_employer_in_a_story_sent_online(ids, monkeypatch):
    from kestrel.db.models import DocMaster, DocSection

    async def employer(s):
        s.add(Setting(key="confidential_mode", value=True))
        master = DocMaster(track="employment", name="Old resume", doc_kind="resume")
        s.add(master)
        await s.flush()
        s.add(DocSection(doc_master_id=master.id, section_kind="experience", sort_order=0,
                         org_label="Harbor Ledger", org_label_redacted="a freight company"))
        await s.commit()

    _db(employer)
    _add_story(**{**ACCEPTED, "title": "Invented Harbor Ledger story",
                  "action": "At Harbor Ledger I rebuilt the distribution forecast.",
                  "result": "Harbor Ledger cut late shipments."})
    _go_online()
    fake = Model(monkeypatch)
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), form_fields(c.get(URL.format(ids["strong"])).text))
    assert fake.calls >= 1
    sent = "\n".join(fake.prompt(n) for n in range(fake.calls))
    assert "story-" in sent and "rebuilt the distribution forecast" in sent  # the story went
    assert "Harbor Ledger" not in sent  # with the employer hidden


def test_the_unique_current_index_is_in_the_migrated_schema(ids):
    from kestrel import paths

    con = sqlite3.connect(paths.db_path())
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name = 'ux_letter_one_current'"
                      ).fetchone()[0]
    assert "UNIQUE" in sql.upper() and "status = 'current'" in sql
