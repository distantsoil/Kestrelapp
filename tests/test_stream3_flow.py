"""Applications, Tailor, Documents and interview prep (D-UI-51, D-UI-52): the drag fix, "My
shortlist", the one-click "Applied?", every Tailor part (summary, bullets, skills) with Swap, Edit
wording and Hide, the Documents editors that need no import, interview notes, and the story
tools. Offline: a migrated SQLite file and the test client."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db.engine import make_async_engine
from kestrel.db.models import (
    DocMaster,
    DocRender,
    DocSlot,
    DocVariant,
    SelectionOverride,
    Setting,
    Story,
    Task,
)
from tests.test_pursuit_board import _add_render, _column, _fresh, _pursuit, _seed
from tests.test_stream_c_pages import _client, _seed_render
from tests.test_web_phase13 import _dispose_engines  # noqa: F401  (autouse fixture)


def _read(fn):
    async def _go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _master_of(render_id: int) -> int:
    async def _go(s):
        return (await s.get(DocRender, render_id)).doc_master_id

    return _read(_go)


def _give_master(master_id: int, **fields) -> None:
    async def _go(s):
        m = await s.get(DocMaster, master_id)
        for k, v in fields.items():
            setattr(m, k, v)
        await s.commit()

    _read(_go)


# ------------------------------------------------------------------------------- drag fix
def test_board_drag_script_binds_once_and_clears_on_dragend():
    from pathlib import Path

    from kestrel import paths

    c = _fresh()
    _seed()
    with c:
        html = c.get("/pursuits").text
    js = (Path(paths.web_static_dir()) / "js" / "applications.js").read_text(encoding="utf-8")
    # The script is one external file, bound once on the document by the page's mount through its
    # lifecycle context (D-UI-210), so a board swap adds nothing and leaving removes it.
    assert html.count("/static/js/applications.js") == 1
    assert js.count("ctx.on(doc, 'dragstart'") == 1
    assert "ctx.on(doc, 'dragend'" in js
    assert "drag.id = drag.col = drag.el = null" in js  # dragend clears the card
    assert "drag.id === null" in js  # a drop with no card in hand is ignored
    card = html.split('data-testid="pcard"')[1].split("</article>")[0]
    assert 'draggable="true"' in html.split('data-testid="pcard"')[0].rsplit("<article", 1)[1]
    assert "Head of IT" in card


# --------------------------------------------------------------------- My shortlist, Applied?
def test_the_shortlisted_stage_reads_shortlisted_but_is_stored_unchanged():
    c = _fresh()
    _, pid = _seed(status="shortlisted")
    with c:
        html = c.get("/pursuits").text
        table = c.get("/pursuits?view=table").text
    assert ">Shortlisted</button>" in html.split('id="ap-menu-stage"')[1]
    assert ">shortlisted<" not in html
    assert 'data-stage-pick="shortlisted"' in html
    assert "Shortlisted" in table
    assert _pursuit(pid)[0].status == "shortlisted"


def test_applied_one_click_records_today_with_a_follow_up():
    c = _fresh()
    opp_id, pid = _seed()
    rid = _add_render(opp_id)
    with c:
        html = c.get("/pursuits").text
        assert ">I applied</button>" in html  # the card opens the form; nothing is recorded yet
        assert _pursuit(pid)[0].status == "shortlisted"
        r = c.post(f"/pursuits/{pid}/act/applied/quick", data={"document_id": str(rid)})
        assert r.status_code == 200
        assert "✓ Applied" in r.text
        board = c.get("/pursuits?view=kanban&track=employment").text
        assert "Head of IT" in _column(board, "Applied")
    p, events = _pursuit(pid)
    today = date.today()
    assert p.status == "applied" and p.document_id == rid
    assert p.applied_at == f"{today.isoformat()}T00:00:00+00:00"
    assert p.next_action_at == (today + timedelta(days=7)).isoformat()
    assert [(e.from_status, e.to_status) for e in events] == [("shortlisted", "applied")]


def test_quick_applied_ignores_posted_dates_and_refuses_another_roles_resume():
    c = _fresh()
    other, _ = _seed(title="Other")
    foreign = _add_render(other)
    _, pid = _seed(title="Mine")
    with c:
        r = c.post(f"/pursuits/{pid}/act/applied/quick", data={"document_id": str(foreign)})
        assert "does not belong" in r.text
        assert _pursuit(pid)[0].status == "shortlisted"
        c.post(f"/pursuits/{pid}/act/applied/quick", data={"applied_on": "2020-01-01"})
        assert c.post("/pursuits/9999/act/applied/quick", data={}).status_code == 404
    assert _pursuit(pid)[0].applied_at.startswith(date.today().isoformat())


# ------------------------------------------------------------------------------------ Tailor
def _seed_tailor():
    render_id, _slot = _seed_render()
    master_id = _master_of(render_id)
    _give_master(
        master_id,
        summary_variants=[{"key": "summary_1", "text": "Technology leader, fifteen years."},
                          {"key": "summary_2", "text": "Operator who ships."}],
        skill_groups=[{"key": "identity", "label": "Identity", "items": ["Entra ID", "Okta"],
                       "default_visible": True}],
    )

    async def _cfg(s):
        r = await s.get(DocRender, render_id)
        r.config = {**(r.config or {}), "summary": "summary_1",
                    "skill_groups": {"identity": True}}
        await s.commit()

    _read(_cfg)
    return render_id, master_id


def test_tailor_lists_summary_first_then_bullets_then_skills_each_with_its_wordings():
    render_id, _ = _seed_tailor()
    html = _client().get(f"/renders/{render_id}").text
    assert html.index(' id="tl-card-summary"') < html.index(' id="tl-card-s1"')
    assert html.index(' id="tl-card-s1"') < html.index(' id="tl-card-skills"')
    summary = html.split(' id="tl-card-summary"')[1].split(' id="tl-card-s1"')[0]
    assert "Technology leader, fifteen years." in summary
    assert "Other wordings (1)" in summary and "Operator who ships." in summary
    skills = html.split(' id="tl-card-skills"')[1]
    assert "Identity" in skills and "Entra ID, Okta" in skills


def test_edit_wording_saves_a_new_operator_variant_and_selects_it():
    render_id, _ = _seed_tailor()
    c = _client()
    typed = "Cut run costs 37% and doubled uptime, 120 people, 4 sites."  # numbers not in original
    r = c.post(f"/renders/{render_id}/wording", data={"slot_key": "s1", "text": typed})
    assert r.status_code == 200 and "Saved as a new wording of your own" in r.text
    assert typed in r.text and "Led the IT function." in r.text  # the original stays a choice

    async def _state(s):
        v = (await s.execute(select(DocVariant).where(DocVariant.text == typed))).scalar_one()
        slot = await s.get(DocSlot, v.doc_slot_id)
        cfg = (await s.get(DocRender, render_id)).config
        ov = (await s.execute(select(SelectionOverride))).scalars().all()
        n = len((await s.execute(
            select(DocVariant).where(DocVariant.doc_slot_id == slot.id))).scalars().all())
        return v, slot, cfg, ov, n

    v, slot, cfg, ov, n = _read(_state)
    assert v.derived is False and v.key == "mine1" and slot.key == "s1" and n == 3
    assert cfg["slots"]["s1"] == "mine1"
    assert [(o.slot_key, o.model_choice, o.operator_choice) for o in ov] == [
        ("s1", "a", "mine1")]
    again = c.post(f"/renders/{render_id}/wording", data={"slot_key": "s1", "text": "Second."})
    assert again.status_code == 200 and _read(_second_key) == "mine2"


async def _second_key(s):
    row = (await s.execute(select(DocVariant).where(DocVariant.text == "Second."))).scalar_one()
    return row.key


def test_edit_wording_on_the_summary_adds_a_summary_wording_and_refuses_empty_text():
    render_id, master_id = _seed_tailor()
    c = _client()
    empty = c.post(f"/renders/{render_id}/wording", data={"slot_key": "summary", "text": "  "})
    assert "Not saved" in empty.text
    r = c.post(f"/renders/{render_id}/wording",
               data={"slot_key": "summary", "text": "I build teams."})
    assert "new summary wording" in r.text and "I build teams." in r.text

    async def _state(s):
        m = await s.get(DocMaster, master_id)
        return m.summary_variants, (await s.get(DocRender, render_id)).config

    variants, cfg = _read(_state)
    assert variants[-1] == {"key": "mine1", "text": "I build teams."} and len(variants) == 3
    assert cfg["summary"] == "mine1"
    assert c.post(f"/renders/{render_id}/wording",
                  data={"slot_key": "ghost", "text": "x"}).status_code == 400


def _preview_of(text: str):
    """The preview document a swap answers with (the #tl-src source the page patches the frame
    from), parsed."""
    from bs4 import BeautifulSoup

    src = BeautifulSoup(text, "html.parser").select_one("textarea#tl-src")
    return BeautifulSoup(src.get_text(), "html.parser")


def test_hide_works_on_a_required_part_the_summary_and_skills_for_this_render_only():
    render_id, master_id = _seed_tailor()
    c = _client()
    for slot in ("s1", "summary", "skills"):
        r = c.post(f"/renders/{render_id}/swap", data={"slot_key": slot, "variant_key": "hide"})
        assert r.status_code == 200
    preview = _preview_of(r.text).get_text()
    assert "Led the IT function." not in preview and "Technology leader" not in preview
    assert "Identity" not in preview

    async def _state(s):
        cfg = (await s.get(DocRender, render_id)).config
        m = await s.get(DocMaster, master_id)
        return cfg, m.summary_variants, m.skill_groups

    cfg, summaries, groups = _read(_state)
    assert cfg["slots"]["s1"] == "hide" and cfg["hide_summary"] and cfg["hide_skills"]
    assert len(summaries) == 2 and len(groups) == 1  # nothing was deleted
    back = c.post(f"/renders/{render_id}/swap", data={"slot_key": "summary",
                                                      "variant_key": "summary_2"})
    assert "Operator who ships." in back.text and "hide_summary" not in _read(_state)[0]
    assert "Operator who ships." in _preview_of(back.text).get_text()
    assert c.post(f"/renders/{render_id}/swap",
                  data={"slot_key": "skills", "variant_key": "bogus"}).status_code == 400


# ---------------------------------------------------------------------------------- Documents
def test_documents_page_shows_the_editors_and_the_optional_checkbox():
    render_id, master_id = _seed_tailor()
    html = _client().get(f"/documents?master_id={master_id}").text
    for marker in ('data-testid="doc-details"', 'data-testid="doc-summary"',
                   'data-testid="doc-skills"', 'data-testid="slot-optional"',
                   "Can be left out when it does not fit the role",
                   'data-testid="new-document"'):
        assert marker in html, marker


def test_header_summary_and_skills_editors_save_and_render_omits_empty_sections():
    from kestrel.docs.catalog import load_catalog, resolve
    from kestrel.docs.render import render_html

    render_id, master_id = _seed_tailor()
    c = _client()
    base = f"/documents/{master_id}"
    r = c.post(f"{base}/header", data={"name": "Pat Example",
                                       "contact": "pat@example.com · Austin, TX"})
    assert "Saved" in r.text and 'value="Pat Example"' in r.text
    r = c.post(f"{base}/summary", data={"text": "A third wording."})
    assert "Summary · 3 wordings" in r.text
    r = c.post(f"{base}/summary/summary_1", data={"text": "Edited wording."})
    assert "Edited wording." in r.text
    r = c.post(f"{base}/skills", data={"label": "Cloud", "items": "Azure, M365 ,, AWS"})
    assert "Cloud" in r.text
    r = c.post(f"{base}/skills/identity", data={"label": "Identity", "items": "Entra ID"})
    assert "Skills group saved." in r.text
    assert "needs a label" in c.post(f"{base}/skills", data={"label": "x"}).text

    async def _doc(s):
        m = await s.get(DocMaster, master_id)
        cat = await load_catalog(s, master_id)
        return m.header, m.skill_groups, render_html(resolve(cat, {"summary": "summary_1"}))

    header, groups, html = _read(_doc)
    assert header["name"] == "Pat Example" and header["contact"].startswith("pat@example.com")
    assert [g["items"] for g in groups] == [["Entra ID"], ["Azure", "M365", "AWS"]]
    assert "Pat Example" in html and "pat@example.com · Austin, TX" in html
    assert "Edited wording." in html

    # delete: the last summary wording is kept, others can go
    assert "Summary wording removed." in c.post(f"{base}/summary/summary_2/delete").text
    assert "Skills group removed." in c.post(f"{base}/skills/cloud/delete").text

    # a master with nothing in it prints no empty Summary, Skills or Experience section
    async def _empty(s):
        m = await s.get(DocMaster, master_id)
        m.summary_variants, m.skill_groups, m.header = [], [], {}
        await s.commit()
        cat = await load_catalog(s, master_id)
        return render_html(resolve(cat, {}))

    empty = _read(_empty)
    assert 'data-block="summary"' not in empty and 'data-block="skills"' not in empty
    assert 'class="name"' not in empty and 'class="contact"' not in empty


def test_last_summary_wording_cannot_be_deleted():
    _render_id, master_id = _seed_tailor()
    c = _client()
    base = f"/documents/{master_id}"
    c.post(f"{base}/summary/summary_2/delete")
    r = c.post(f"{base}/summary/summary_1/delete")
    assert "Kept: at least one summary wording stays" in r.text

    async def _n(s):
        return len((await s.get(DocMaster, master_id)).summary_variants)

    assert _read(_n) == 1


def test_per_bullet_optional_checkbox_saves_and_a_blank_resume_needs_no_import():
    _render_id, master_id = _seed_tailor()
    c = _client()

    async def _slot(s):
        return (await s.execute(select(DocSlot).where(DocSlot.key == "s1"))).scalar_one()

    slot = _read(_slot)
    assert slot.optional is False
    c.post(f"/documents/slot/{slot.id}/optional", data={"optional": "1"})
    assert _read(_slot).optional is True
    c.post(f"/documents/slot/{slot.id}/optional", data={})
    assert _read(_slot).optional is False

    r = c.post("/documents/new", data={"track": "employment", "doc_kind": "resume",
                                       "name": "Fresh start"})
    new_id = int(r.headers["HX-Redirect"].rsplit("=", 1)[1])
    page = c.get(f"/documents?master_id={new_id}").text
    assert "Fresh start" in page and 'data-testid="doc-details"' in page
    c.post(f"/documents/{new_id}/header", data={"name": "Sam"})
    c.post(f"/documents/{new_id}/summary", data={"text": "Hello."})
    c.post(f"/documents/{new_id}/roles", data={"org_label": "Co", "title": "Lead",
                                                "text": "Did the thing."})
    assert "Did the thing." in c.get(f"/documents?master_id={new_id}").text


# ----------------------------------------------------------------------------- prep and stories
def test_prep_notes_save_per_role_and_survive_a_rebuild():
    c = _fresh()
    opp_id, _ = _seed()
    with c:
        page = c.get(f"/prep/{opp_id}").text
        assert "Your notes for this interview" in page and 'data-testid="prep-notes"' in page
        r = c.post(f"/prep/{opp_id}/notes", data={"notes": "  Ask about the board.  "})
        assert "Saved" in r.text
        assert "Ask about the board." in c.get(f"/prep/{opp_id}").text
        assert c.post("/prep/9999/notes", data={"notes": "x"}).status_code == 404

    async def _row(s):
        return (await s.get(Setting, f"prep_notes.{opp_id}")).value

    assert _read(_row) == "Ask about the board."
    with c:
        c.post(f"/prep/{opp_id}/notes", data={"notes": ""})

    async def _gone(s):
        return await s.get(Setting, f"prep_notes.{opp_id}")

    assert _read(_gone) is None


def _toast_msg(resp) -> str:
    import json as _json

    return _json.loads(resp.headers["HX-Trigger"])["k-toast"]["message"]


def test_add_a_story_creates_an_operator_owned_story_and_the_lede_is_rewritten():
    c = _fresh()
    with c:
        page = " ".join(c.get("/stories").text.split())
        assert "Your interview stories, in your own words." in page
        assert "A draft is yours once you save it; nothing rewrites it after that." in page
        assert "+ Add a story" in page and "Build stories" in page
        bad = c.post("/stories", data={"title": " "})
        assert "give the story a title" in _toast_msg(bad)
        r = c.post("/stories", data={
            "title": "The migration", "situation": "Two data centers", "task": "Merge them",
            "action": "I led it", "result": "Saved 20%", "reflection": "Start earlier"})
        assert "Story saved" in _toast_msg(r) and "The migration" in r.text

    async def _stories(s):
        return (await s.execute(select(Story))).scalars().all()

    (story,) = _read(_stories)
    assert story.is_canonical and story.operator_edited and story.action == "I led it"
    assert story.reflection == "Start earlier" and story.themes == []


def test_build_stories_enqueues_one_task_and_the_worker_runs_it_with_the_notes(monkeypatch):
    from kestrel.interview import stories as stories_mod
    from kestrel.interview.notes import save_notes
    from kestrel.interview.stories import ConsolidationResult, collect_note_sketches
    from kestrel.interview.story_task import TASK_TYPE, drain_story_builds

    c = _fresh()
    opp_id, _ = _seed()
    with c:
        r = c.post("/stories/build")
        assert "Building stories in the background" in _toast_msg(r)
        assert 'hx-trigger="every 5s"' in r.text
        again = c.post("/stories/build")
        assert "already running" in _toast_msg(again)

    async def _tasks(s):
        return (await s.execute(select(Task).where(Task.type == TASK_TYPE))).scalars().all()

    (task,) = _read(_tasks)
    assert task.status == "pending"

    async def _note_sketches(s):
        await save_notes(s, opp_id, "I rebuilt the on-call rota.")
        return await collect_note_sketches(s)

    (sketch,) = _read(_note_sketches)
    assert sketch.kind == "note" and sketch.situation == "I rebuilt the on-call rota."
    assert sketch.opportunity_id == opp_id

    seen = {}

    async def fake(session, client, settings):
        seen["sketches"] = len(await stories_mod.collect_note_sketches(session))
        return ConsolidationResult(sketches=1, clusters=1, created_story_ids=[7])

    monkeypatch.setattr(stories_mod, "consolidate_stories", fake)

    async def _drain():
        engine = make_async_engine(paths.db_path())
        try:
            sm = async_sessionmaker(engine, expire_on_commit=False)
            return await drain_story_builds(sm, object(), object())
        finally:
            await engine.dispose()

    assert asyncio.run(_drain()) == 1 and seen["sketches"] == 1
    assert _read(_tasks)[0].status == "done"
    with c:
        page = c.get("/stories").text
    assert "Last build: 1 new draft" in page


def test_a_build_killed_mid_run_is_retried_and_a_dead_one_says_why(monkeypatch):
    """Regression: a build is retried (max_attempts 3, lease renewed), and the page reads the
    task row: "building" while it is queued again, "Last build failed: <reason> · Try again" once
    it is dead, even when the process died before it wrote a word."""
    from kestrel.interview import stories as stories_mod
    from kestrel.interview import story_task
    from kestrel.interview.story_task import MAX_ATTEMPTS, TASK_TYPE, drain_story_builds

    c = _fresh()
    _seed()
    with c:
        c.post("/stories/build")

    async def _tasks(s):
        return (await s.execute(select(Task).where(Task.type == TASK_TYPE))).scalars().all()

    (task,) = _read(_tasks)
    assert task.max_attempts == MAX_ATTEMPTS == 3

    # Killed mid-build: the lease ran out with attempts left, so it is queued again, not lost.
    async def _kill(s):
        t = await s.get(Task, task.id)
        t.status, t.attempts, t.lease_until = "leased", 1, "2000-01-01T00:00:00+00:00"
        await s.commit()

    _read(_kill)

    async def _reclaim():
        from kestrel.queue.queue import TaskQueue

        engine = make_async_engine(paths.db_path())
        try:
            return await TaskQueue(async_sessionmaker(engine, expire_on_commit=False)) \
                .reclaim_expired()
        finally:
            await engine.dispose()

    asyncio.run(_reclaim())
    assert _read(_tasks)[0].status == "pending"
    with c:
        page = c.get("/stories").text
    assert "Building now" in page

    calls = {"n": 0}

    async def boom(session, client, settings):
        calls["n"] += 1
        raise RuntimeError("model unreachable")

    monkeypatch.setattr(stories_mod, "consolidate_stories", boom)
    monkeypatch.setattr(story_task, "_RENEW_S", 0.01)

    async def _drain():
        engine = make_async_engine(paths.db_path())
        try:
            sm = async_sessionmaker(engine, expire_on_commit=False)
            return await drain_story_builds(sm, object(), object())
        finally:
            await engine.dispose()

    assert asyncio.run(_drain()) == 0
    assert calls["n"] == MAX_ATTEMPTS - 1  # one attempt was used by the killed run
    (dead,) = _read(_tasks)
    assert dead.status == "dead" and "model unreachable" in (dead.last_error or "")
    with c:
        page = c.get("/stories").text
    assert 'data-testid="build-failed"' in page and "model unreachable" in page
    assert "Try again" in page

    # Dead with no error recorded (the process died on its last attempt): it still says so.
    async def _silent(s):
        t = await s.get(Task, dead.id)
        t.last_error = None
        await s.commit()

    _read(_silent)
    with c:
        page = c.get("/stories").text
    assert "it stopped before it finished" in page
