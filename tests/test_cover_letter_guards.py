"""Issue #124 (reopened 2026-11-05) and #107: the letters table, stories as a source, the length
guards, and Draft again that keeps the previous draft until the new one is kept, with Undo
(SPEC-MODULES 1.6, D-LETTER-1 to D-LETTER-4). Every test drives the real routes and the real worker
step and fakes only the model call (`LLMClient.complete`) and, where a PDF is made, the browser
(`render_pdf`). Invented text only; nothing reaches the network."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from alembic import command
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import select

from kestrel import paths
from kestrel.config import HostedMode
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
from kestrel.db.models import DocRender, Letter, Pursuit, Setting, Story, StoryUsage
from kestrel.docs import render as docrender
from kestrel.llm.client import ChatResult, LLMClient
from tests import role_seed
from tests.letter_helpers import form_fields, generate

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

MODEL = "vendor/letter-model"
URL = "/opportunities/{}/role/cover-letter"
GEN = "/opportunities/{}/role/cover-letter/generate"
SHORT = (
    "Dear Hiring Team,\n\nYour planning team reports on three calendars, and I have spent two "
    "years making calendars agree.\n\nI would like thirty minutes next week to walk through it."
)


def _long(words: int, paragraphs: int = 3) -> str:
    per = max(words // paragraphs, 1)
    paras = [" ".join(["planning"] * (per - 1)) + " ends." for _ in range(paragraphs)]
    return "Dear Hiring Team,\n\n" + "\n\n".join(paras)


@pytest.fixture
def ids():
    return asyncio.run(role_seed.seed_async())


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False), raise_server_exceptions=False)


def _db(fn):
    async def go():
        async with get_sessionmaker()() as s:
            return await fn(s)

    return asyncio.run(go())


class Model:
    """The model call: each reply comes from `bodies` in turn (the last one repeats). Records the
    messages of every request."""

    def __init__(self, monkeypatch, bodies=(SHORT,), claims=None):
        self.requests: list[list[dict]] = []
        outer = self
        bodies = list(bodies)

        async def complete(self_client, **kwargs):
            outer.requests.append(kwargs.get("messages") or [])
            body = bodies[min(len(outer.requests) - 1, len(bodies) - 1)]
            payload = {"body_md": body, "word_count": len(body.split()),
                       "claims_used": list(claims or []), "opening_alternatives": []}
            return ChatResult(content=json.dumps(payload), model=MODEL)

        monkeypatch.setattr(LLMClient, "complete", complete)

    @property
    def calls(self) -> int:
        return len(self.requests)

    def prompt(self, n: int = 0) -> str:
        return "\n".join(str(m.get("content") or "") for m in self.requests[n])


def _letters(opp_id: int) -> list[Letter]:
    async def go(s):
        return list((await s.execute(
            select(Letter).where(Letter.opportunity_id == opp_id).order_by(Letter.id))).scalars())

    return _db(go)


def _status(opp_id: int) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for row in _letters(opp_id):
        out.setdefault(row.status, []).append(row.render_id)
    return out


def _text(html: str) -> str:
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def _add_story(**kw) -> int:
    async def go(s):
        st = Story(themes=[], **kw)
        s.add(st)
        await s.commit()
        return st.id

    return _db(go)


def _pursuit_letter(opp_id: int) -> int | None:
    async def go(s):
        p = (await s.execute(select(Pursuit).where(Pursuit.opportunity_id == opp_id))).scalars()
        row = p.first()
        return row.cover_letter_id if row else None

    return _db(go)


# =============================================================== the letters table and migration
def _go(db, direction: str, rev: str) -> None:
    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        cfg = migrate._config(str(mig_dir), db)
        (command.upgrade if direction == "up" else command.downgrade)(cfg, rev)


OLD_BODY = "Dear Hiring Team,\n\nAn invented letter saved before the letters table existed."


def _old_letter_config() -> dict:
    return {"kind": "cover_letter", "body_md": OLD_BODY, "generated_md": "Dear Hiring Team,",
            "claims_used": ["pp_old"], "flags": [{"kind": "style", "field": "body_md",
                                                  "phrase": "invented"}],
            "basis": {"master_id": 1, "config": {}}, "edited": True, "words": 12,
            "pdf_digest": "abc"}


def test_migration_0028_moves_a_render_letter_into_the_letter_table_and_back(tmp_path):
    db = tmp_path / "m.db"
    _go(db, "up", "0027")
    # Fill only the columns the rows need, whatever else the schema requires.
    from tests.test_selection_override_detach import _ins

    con = sqlite3.connect(db)
    _ins(con, "opportunity", id=1, track="employment", opp_type="job_posting", title="Planner",
         org_name="Invented Co", url="https://e.test/1", stage="t2_done")
    _ins(con, "doc_master", id=1, track="employment", name="t", doc_kind="resume", header="{}",
         summary_variants="[]", skill_groups="[]")
    _ins(con, "doc_render", id=7, opportunity_id=1, doc_master_id=1, template="cover_letter",
         config=json.dumps(_old_letter_config()), created_at="2026-11-01T00:00:00+00:00")
    _ins(con, "doc_render", id=8, opportunity_id=1, doc_master_id=1, config="{}")  # a resume
    con.commit()
    con.close()
    _go(db, "up", "0028")
    con = sqlite3.connect(db)
    rows = con.execute("SELECT render_id, opportunity_id, kind, status, body_md, generated_md,"
                       " claims_used, flags, word_count, edited, extras, prompt_version"
                       " FROM letter").fetchall()
    assert len(rows) == 1  # the resume render is not a letter
    r = rows[0]
    assert r[:6] == (7, 1, "cover_letter", "current", OLD_BODY, "Dear Hiring Team,")
    assert json.loads(r[6]) == ["pp_old"] and json.loads(r[7])[0]["phrase"] == "invented"
    assert r[8] == 12 and r[9] == 1 and json.loads(r[10])["basis"]["master_id"] == 1
    assert r[11] is None  # earlier versions did not record it
    cfg = json.loads(con.execute("SELECT config FROM doc_render WHERE id = 7").fetchone()[0])
    assert cfg == {"kind": "cover_letter", "pdf_digest": "abc"}  # the text moved; the PDF stays
    fks = {row[2]: row[6] for row in con.execute("PRAGMA foreign_key_list(letter)")}
    assert fks["doc_render"] == "CASCADE"
    con.close()
    _go(db, "down", "0027")
    con = sqlite3.connect(db)
    back = json.loads(con.execute("SELECT config FROM doc_render WHERE id = 7").fetchone()[0])
    assert back["body_md"] == OLD_BODY and back["claims_used"] == ["pp_old"] and back["edited"]
    assert "letter" not in {t[0] for t in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()


def test_a_letter_saved_before_the_table_opens_after_migrating(ids, monkeypatch):
    """The real case: a role page world at 0.7.5's schema with a letter stored as a render
    record, then the app's own upgrade, then the person opens Documents and the draft."""
    from tests.test_selection_override_detach import _ins

    asyncio.run(reset_engine())
    forget_engine()
    db = paths.db_path()
    _go(db, "down", "0027")
    con = sqlite3.connect(db)
    cfg = _old_letter_config()
    cfg["basis"] = {"master_id": ids["master"], "config": {}}
    _ins(con, "doc_render", id=900, opportunity_id=ids["strong"], doc_master_id=ids["master"],
         template="cover_letter", config=json.dumps(cfg), created_at="2026-11-01T00:00:00+00:00")
    con.commit()
    con.close()
    migrate.run_migrations()  # what the app runs on start
    Model(monkeypatch)
    with _client() as c:
        sheet = c.get(URL.format(ids["strong"]))
        docs = c.get(f"/opportunities/{ids['strong']}/tab/documents")
    assert sheet.status_code == 200 and 'data-testid="cover-letter-draft"' in sheet.text
    assert "An invented letter saved before the letters table existed." in _text(sheet.text)
    assert 'data-render="900"' in sheet.text
    assert "12 words" in _text(docs.text) and "edited" in _text(docs.text)


# ================================================================ stories as a source (D-LETTER-2)
ACCEPTED = dict(title="Invented warehouse story", situation="SITUATIONWORDS stayed private.",
                action="I rerouted the invented night shift. Then I trained the leads.",
                result="Invented returns fell by a third. The team kept it.",
                is_canonical=True, operator_edited=True)
PROPOSED = dict(title="Invented proposal", situation="A proposed situation.",
                action="PROPOSEDACTION was drafted by Kestrel.", result="PROPOSEDRESULT happened.",
                is_canonical=True, operator_edited=False)


def test_only_accepted_stories_reach_the_prompt_and_never_whole(ids, monkeypatch):
    good = _add_story(**ACCEPTED)
    _add_story(**PROPOSED)
    fake = Model(monkeypatch)
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})
    sent = fake.prompt(0)
    assert f"[story-{good}]" in sent
    assert "I rerouted the invented night shift." in sent
    assert "Invented returns fell by a third." in sent
    assert "Then I trained the leads." not in sent  # one action line only
    assert "SITUATIONWORDS" not in sent  # never the whole story
    assert "PROPOSEDACTION" not in sent and "PROPOSEDRESULT" not in sent


def test_a_story_claim_resolves_and_shows_its_source_and_a_proposed_one_flags(ids, monkeypatch):
    good = _add_story(**ACCEPTED)
    body = (SHORT.replace("I would like", "I rerouted the invented night shift and invented "
                          "returns fell by a third.\n\nI would like"))
    Model(monkeypatch, bodies=(body,), claims=[f"story-{good}"])
    with _client() as c:
        ok = generate(c, GEN.format(ids["strong"]), {})
    soup = BeautifulSoup(ok.text, "html.parser")
    assert soup.select_one('[data-testid="cover-letter-unresolved"]') is None
    src = soup.select_one(f'.cl-src[data-cl-src="story-{good}"]')
    assert src is not None and "Your story" in src.get_text(" ")
    assert soup.select_one(f'.cl-claim[data-cl-key="story-{good}"]') is not None

    ids2 = asyncio.run(role_seed.seed_async())
    bad = _add_story(**PROPOSED)
    Model(monkeypatch, bodies=(SHORT,), claims=[f"story-{bad}"])
    with _client() as c:
        flagged = generate(c, GEN.format(ids2["strong"]), {})
    assert "unverified claims present" in flagged.text.lower()
    assert _letters(ids2["strong"])[0].claims_used == [f"story-{bad}"]  # kept, never dropped


def test_a_cited_story_records_one_usage_row_and_discard_takes_it_back(ids, monkeypatch):
    good = _add_story(**ACCEPTED)
    Model(monkeypatch, claims=[f"story-{good}"])

    async def usage(s):
        rows = (await s.execute(select(StoryUsage).where(
            StoryUsage.story_id == good, StoryUsage.opportunity_id == ids["strong"]))).scalars()
        return len(list(rows)), (await s.get(Story, good)).use_count

    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})
        assert _db(usage) == (1, 1)
        rid = _status(ids["strong"])["current"][0]
        # A second draft citing the same story does not count it twice.
        c.post(f"{URL.format(ids['strong'])}/{rid}/redraft")
        generate(c, GEN.format(ids["strong"]), {"replace": str(rid)})
        new = _status(ids["strong"])["proposed"][0]
        c.post(f"{URL.format(ids['strong'])}/{new}/keep-new")
        assert _db(usage) == (1, 1)
        c.post(f"{URL.format(ids['strong'])}/{new}/discard")
    assert _db(usage) == (0, 0)


# =================================================================== length guards (D-LETTER-3)
def test_an_over_length_draft_is_asked_to_shorten_once_then_kept_and_marked(ids, monkeypatch):
    long_body = _long(420)
    fake = Model(monkeypatch, bodies=(long_body, long_body))
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    assert fake.calls == 2  # the draft, then exactly one re-ask
    assert "Shorten it to at most 350 words" in fake.prompt(1)
    letter = _letters(ids["strong"])[0]
    assert letter.body_md == long_body  # never cut
    mark = BeautifulSoup(r.text, "html.parser").select_one(
        '[data-testid="cover-letter-over-length"]')
    assert mark is not None
    assert f"Over length ({len(long_body.split())} words, limit 350)" in mark.get_text(" ")
    assert all(f.get("kind") != "length" for f in letter.flags or [])


def test_a_shortened_reply_is_taken_and_has_no_mark(ids, monkeypatch):
    fake = Model(monkeypatch, bodies=(_long(420), SHORT))
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    assert fake.calls == 2 and _letters(ids["strong"])[0].body_md == SHORT
    assert 'data-testid="cover-letter-over-length"' not in r.text


def test_too_many_paragraphs_counts_against_the_same_single_reask(ids, monkeypatch):
    many = _long(200, paragraphs=6) + "\n\nI leveraged the invented calendar."
    fake = Model(monkeypatch, bodies=(many,))
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    assert fake.calls == 2  # one re-ask for the stock word AND the length together
    assert "stock word" in fake.prompt(1) and "paragraphs" in fake.prompt(1)
    assert "Too many paragraphs (7, limit 4)" in _text(r.text)


def _go_online() -> None:
    async def seed(s):
        s.add_all([
            Setting(key="llm.hosted_mode.enabled", value=True),
            Setting(key="llm.hosted_mode.acknowledged_at", value="2026-10-15T00:00:00Z"),
            Setting(key="llm.hosted_mode.acknowledged_terms",
                    value=HostedMode().disclosure_terms()),
            Setting(key="llm.tiers.tier3.backend", value="frontier"),
            Setting(key="llm.tiers.tier3.model", value=MODEL),
        ])
        await s.commit()

    _db(seed)


def test_online_the_shorten_reask_goes_only_after_the_confirmed_dialog(ids, monkeypatch):
    """INVARIANT: nothing is sent to an online model without the confirmed dialog, including the
    shorten re-ask."""
    _go_online()
    fake = Model(monkeypatch, bodies=(_long(420),))
    with _client() as c:
        panel = c.get(URL.format(ids["strong"])).text
        assert "too long" in _text(panel) and "up to two of your accepted stories" in _text(panel)
        refused = generate(c, GEN.format(ids["strong"]), form_fields(panel, ticked=False))
        assert fake.calls == 0 and 'data-testid="cover-letter-ask"' in refused.text
        assert _letters(ids["strong"]) == []
        generate(c, GEN.format(ids["strong"]), form_fields(panel))
    assert fake.calls == 2  # the draft and its one shorten re-ask, both after the tick


def test_inputs_above_the_caps_are_cut_before_the_prompt_is_built(ids, monkeypatch):
    from kestrel.services import profile_edit as pe

    pe.add_proof_points("employment", [
        {"key": f"pp_{i}", "claim": f"Invented claim {i} about gardening", "evidence": "",
         "metric": ""} for i in range(5)
    ] + [{"key": "pp_hit", "claim": "Rebuilt the demand forecast for distribution centers",
          "evidence": "", "metric": ""}])
    for i in range(3):
        _add_story(**{**ACCEPTED, "title": f"Invented story {i}"})
    fake = Model(monkeypatch)
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})
    sent = fake.prompt(0)
    proofs = sent.split("PROOF POINTS", 1)[1].split("MY STORIES", 1)[0]
    lines = [ln for ln in proofs.splitlines() if ln.startswith("- [")]
    assert len(lines) <= 3
    assert "[pp_hit]" in proofs  # the one that shares the posting's words is kept
    stories = sent.split("MY STORIES", 1)[1].split("GUIDANCE", 1)[0]
    assert len([ln for ln in stories.splitlines() if ln.startswith("- [story-")]) == 2


def _draft(c, opp_id: int, monkeypatch, body: str = SHORT) -> int:
    Model(monkeypatch, bodies=(body,))
    generate(c, GEN.format(opp_id), {})
    return _status(opp_id)["current"][0]


def test_the_server_refuses_to_save_above_600_words_and_keeps_the_typed_text(ids, monkeypatch):
    with _client() as c:
        rid = _draft(c, ids["strong"], monkeypatch)
        typed = _long(640)
        r = c.post(f"{URL.format(ids['strong'])}/{rid}/save", data={"body_md": typed,
                                                                    "tab": "edit"})
        ok = c.post(f"{URL.format(ids['strong'])}/{rid}/save", data={"body_md": _long(480),
                                                                     "tab": "edit"})
    toast = json.loads(r.headers["hx-trigger"])["k-toast"]
    assert toast["message"].startswith("Not saved: a cover letter can be at most 600 words")
    soup = BeautifulSoup(r.text, "html.parser")
    assert soup.find("textarea").get_text() == typed  # nothing the person typed is lost
    assert soup.select_one("[data-cl-count]")["data-state"] == "stop"
    assert soup.select_one("[data-cl-text]")["data-max"] == "600"
    assert _letters(ids["strong"])[0].body_md == _long(480)  # the second save, over 350, is kept
    assert BeautifulSoup(ok.text, "html.parser").select_one("[data-cl-count]")[
        "data-state"] == "over"


@pytest.fixture
def pages(monkeypatch):
    """A stand-in browser: the text marked "overflowing" runs to two pages, anything else fits.
    (The real page count from Edge is checked on the owner's PC: BLOCKED, requires local.)"""
    made: list[str] = []

    async def fake(html: str):
        made.append(html)
        return docrender.RenderedPdf(pdf=b"%PDF-1.4\n%%EOF\n",
                                     page_count=2 if "overflowing" in html else 1)

    monkeypatch.setattr(docrender, "render_pdf", fake)
    return made


def test_the_one_page_check_fires_on_a_long_body_before_download(ids, monkeypatch, pages):
    with _client() as c:
        rid = _draft(c, ids["strong"], monkeypatch)
        base = f"{URL.format(ids['strong'])}/{rid}"
        fits = c.get(f"{base}/fit")
        long_body = " ".join(["overflowing"] * 590)
        c.post(f"{base}/save", data={"body_md": "Dear Hiring Team,\n\n" + long_body})
        over = c.get(f"{base}/fit")
        dl = c.get(f"{base}/download")
    assert 'data-testid="cover-letter-fit-ok"' in fits.text
    assert 'data-testid="cover-letter-fit-over"' in over.text
    assert "runs to 2 pages" in _text(over.text) and "will not shrink" in _text(over.text)
    assert dl.status_code == 200  # never refused: the person decides
    assert len(pages) == 2  # the download reused the PDF the check made for the same text


def test_the_preview_asks_for_the_fit_check_only_when_opened(ids, monkeypatch, pages):
    with _client() as c:
        rid = _draft(c, ids["strong"], monkeypatch)
        sheet = c.get(URL.format(ids["strong"]))
    fit = BeautifulSoup(sheet.text, "html.parser").select_one("[data-cl-fit]")
    assert fit["hx-get"].endswith(f"/{rid}/fit") and fit["hx-trigger"] == "cl-fit once"
    assert pages == []  # opening the sheet makes no PDF


# ========================================================= Draft again and Undo (D-LETTER-4)
def _snapshot(render_id: int) -> tuple:
    for row in _letters_all():
        if row.render_id == render_id:
            return (row.body_md, row.generated_md, row.claims_used, row.flags, row.word_count,
                    row.edited, row.extras, row.prompt_version, row.kind)
    raise AssertionError("no such letter")


def _letters_all() -> list[Letter]:
    async def go(s):
        return list((await s.execute(select(Letter))).scalars())

    return _db(go)


def test_draft_again_keeps_the_old_draft_until_kept_and_undo_restores_it_exactly(
    ids, monkeypatch
):
    opp = ids["strong"]
    with _client() as c:
        c.post(f"/opportunities/{opp}/role/shortlist")
        old = _draft(c, opp, monkeypatch)
        base = URL.format(opp)
        c.post(f"{base}/{old}/save", data={"body_md": SHORT + "\n\nMy own invented edit."})
        before = _snapshot(old)
        assert _pursuit_letter(opp) == old
        fake = Model(monkeypatch, bodies=(SHORT.replace("two years", "four years"),))
        ask = c.post(f"{base}/{old}/redraft")
        shown = generate(c, GEN.format(opp), form_fields(ask.text))
        assert "My own invented edit." not in fake.prompt(0)  # never fed the old draft
        new = _status(opp)["proposed"][0]
        # The old draft is untouched, still in Documents and on the application.
        assert _status(opp)["current"] == [old] and _snapshot(old) == before
        assert _pursuit_letter(opp) == old
        assert 'data-testid="cover-letter-proposal"' in shown.text
        docs = c.get(f"/opportunities/{opp}/tab/documents").text
        assert "A new draft is ready" in _text(docs)
        assert f"/cover-letter/{new}/keep-new" in shown.text
        assert 'data-testid="cover-letter-discard"' not in shown.text
        kept = c.post(f"{base}/{new}/keep-new")
        # D-LETTER-7: no one-click Undo in the toast; Undo asks first in the sheet.
        assert "undoUrl" not in json.loads(kept.headers["hx-trigger"])["k-toast"]
        assert _status(opp) == {"current": [new], "previous": [old]}
        assert _pursuit_letter(opp) == new
        assert 'data-testid="cover-letter-undo"' in kept.text
        undone = c.post(f"{base}/{new}/undo", data={"sure": "1"})
    assert _status(opp) == {"current": [old]}
    assert _snapshot(old) == before  # exactly as it was
    assert _pursuit_letter(opp) == old
    assert "My own invented edit." in _text(undone.text)

    async def gone(s):
        return await s.get(DocRender, new)

    assert _db(gone) is None


def test_keep_previous_deletes_the_new_draft_and_changes_nothing_else(ids, monkeypatch):
    opp = ids["strong"]
    with _client() as c:
        old = _draft(c, opp, monkeypatch)
        before = _snapshot(old)
        base = URL.format(opp)
        generate(c, GEN.format(opp), form_fields(c.post(f"{base}/{old}/redraft").text))
        new = _status(opp)["proposed"][0]
        r = c.post(f"{base}/{new}/keep-previous")
        again = c.post(f"{base}/{new}/keep-new")  # a stale button: nothing changes
    assert _status(opp) == {"current": [old]} and _snapshot(old) == before
    assert "Kept your previous draft" in json.loads(r.headers["hx-trigger"])["k-toast"]["message"]
    assert "already gone" in json.loads(again.headers["hx-trigger"])["k-toast"]["message"]


def test_discard_still_asks_first_and_takes_the_previous_draft_too(ids, monkeypatch):
    opp = ids["strong"]
    with _client() as c:
        old = _draft(c, opp, monkeypatch)
        base = URL.format(opp)
        generate(c, GEN.format(opp), form_fields(c.post(f"{base}/{old}/redraft").text))
        new = _status(opp)["proposed"][0]
        sheet = c.post(f"{base}/{new}/keep-new").text
        # Keep new on a draft that is no longer a proposal changes nothing (a second press).
        twice = c.post(f"{base}/{new}/keep-new")
        assert "already gone" in json.loads(twice.headers["hx-trigger"])["k-toast"]["message"]
        assert _status(opp) == {"current": [new], "previous": [old]}
        soup = BeautifulSoup(sheet, "html.parser")
        confirm = soup.select_one('[data-testid="cover-letter-confirm"]')
        assert confirm is not None and "Discard this draft?" in confirm.get_text(" ")
        assert soup.select_one('[data-testid="cover-letter-discard"]').get("hx-post") is None
        c.post(f"{base}/{new}/discard")
        nothing = c.post(f"{base}/{new}/undo", data={"sure": "1"})
    assert _letters(opp) == []
    assert "Nothing changed" in json.loads(nothing.headers["hx-trigger"])["k-toast"]["message"]


def test_a_redraft_resumes_after_a_restart_and_still_waits_to_be_kept(ids, monkeypatch):
    """Hard rule 5: the redraft is a task row; a quit before the worker ran it loses nothing."""
    from tests.letter_helpers import drain_letters

    opp = ids["strong"]
    with _client() as c:
        old = _draft(c, opp, monkeypatch)
        base = URL.format(opp)
        form = form_fields(c.post(f"{base}/{old}/redraft").text)
        card = c.post(GEN.format(opp), data=form)  # queued, not run: the app "quits" here
        assert "data-watching" in card.text
    asyncio.run(reset_engine())
    forget_engine()
    Model(monkeypatch)
    drain_letters()  # the next start's worker step
    assert _status(opp)["current"] == [old] and len(_status(opp)["proposed"]) == 1


# ================================================================ #107: marks on a letter
def test_plain_writing_marks_render_on_a_flagged_letter_and_hide_after_an_edit(ids, monkeypatch):
    flagged = SHORT.replace("I would like", "I leveraged the invented calendars.\n\nI would like")
    Model(monkeypatch, bodies=(flagged,))  # the re-ask answers the same, so the mark stays
    with _client() as c:
        first = generate(c, GEN.format(ids["strong"]), {})
        rid = _status(ids["strong"])["current"][0]
        soup = BeautifulSoup(first.text, "html.parser")
        marks = soup.select('[data-testid="cover-letter-marks"] [data-testid="pp-style-mark"]')
        assert marks and "leveraged" in marks[0].get_text(" ").lower()
        assert soup.select_one('[data-testid="pp-style-note"]') is not None
        edited = c.post(f"{URL.format(ids['strong'])}/{rid}/save",
                        data={"body_md": flagged + "\n\nA line of my own."})
        reopened = c.get(URL.format(ids["strong"]))
    for html in (edited.text, reopened.text):
        assert 'data-testid="pp-style-mark"' not in html
        assert 'data-testid="cover-letter-draft"' in html


def test_story_rows_leave_out_a_proposed_story_whatever_the_query_returns():
    """The second guard behind the database filter: a story the person has not accepted never
    becomes a source, even if a caller hands it in."""
    from types import SimpleNamespace

    from kestrel.docs.letters import story_rows

    proposed = SimpleNamespace(id=3, title="t", action="Did a thing.", result="It worked.",
                               is_canonical=True, operator_edited=False)
    accepted = SimpleNamespace(**{**vars(proposed), "id": 4, "operator_edited": True})
    assert [r["key"] for r in story_rows([proposed, accepted])] == ["story-4"]


# ------------------------------------------- D-LETTER-9: lengths of time and promises
# The owner's local check, 2026-11-06: a letter said "I have three years of analysis experience"
# for a person with eight, and "prepared to start immediately", and neither was marked.
_OWN = ["Cut weekly reporting from two days to two hours, 3 warehouses.",
        "Raised inventory accuracy from 92% to 98%."]
_POSTING = "The role requires 3 years of analysis experience."


def _flagged(body, sources=_OWN, posting=_POSTING):
    from kestrel.docs.letters import unverified_details

    return [f["phrase"] for f in unverified_details(body, sources, [], posting)]


def test_a_bare_number_elsewhere_does_not_support_a_length_of_time():
    assert _flagged("I have three years of analysis experience.") == ["three years"]
    assert _flagged("I bring 3 years in analysis.") == ["3 years"]


def test_a_length_of_time_the_person_wrote_is_supported():
    own = [*_OWN, "Eight years as an operations analyst."]
    assert _flagged("I have eight years as an analyst.", own) == []
    assert _flagged("I have 8 years as an analyst.", own) == []
    assert _flagged("I have eight months as an analyst.", own) == ["eight months"]


def test_the_postings_own_requirement_may_be_restated_about_the_role():
    assert _flagged("The role requires 3 years of analysis experience.") == []


def test_promises_the_person_never_made_are_marked():
    assert _flagged("I am prepared to start immediately.") == ["start immediately"]
    assert _flagged("I can start right away and am willing to relocate.") == [
        "start right away", "willing to relocate"]
    assert _flagged("I would not need sponsorship.") == ["sponsorship"]


def test_a_promise_in_the_persons_own_text_is_not_marked():
    own = [*_OWN, "Willing to relocate to the coast."]
    assert _flagged("I am willing to relocate.", own) == []


def test_a_letter_with_no_promise_or_time_claim_is_clean():
    assert _flagged("I look forward to discussing how I can help the team.") == []


def test_every_length_of_time_is_found_even_after_the_same_number():
    # A repeated compound: the second "twenty-five" is the claim, the first is grounded.
    assert _flagged("I managed twenty-five clients over twenty-five years.",
                    ["25 clients."]) == ["twenty-five years"]
    # Hyphenated lengths of time are found: grounded by the same number elsewhere ("8 sites"),
    # they are still marked.
    assert _flagged("My eight-year career included an 18-month rollout.",
                    ["8 sites and 18 stores."]) == ["eight-year", "18-month"]


def test_a_length_of_time_is_marked_once_not_twice():
    # The bare number is marked (and keeps its re-ask); the length of time is not added again.
    assert _flagged("I bring eight years of analysis experience.") == ["eight"]


def test_ordinary_prose_is_not_read_as_a_promise():
    assert _flagged("I could start contributing immediately to your relocation project.") == []


def test_more_promise_wordings_are_marked():
    assert _flagged("I can start on short notice, am happy to travel, and am authorized to "
                    "work in the US.") == ["start on short notice", "happy to travel",
                                           "authorized to work"]


def test_a_promise_of_the_same_kind_in_the_persons_text_grounds_another_wording():
    assert _flagged("I am open to relocation.", [*_OWN, "Willing to relocate to the coast."]) == []


async def test_time_and_promise_marks_never_trigger_the_re_ask():
    """A false mark must not make the model delete a true fact (D-LETTER-9, like names #113):
    the draft is kept as written, with both marks, after one model call."""
    from kestrel.docs.letters import generate_cover
    from tests.test_writing_check import _cover, _FakeLLM

    body = "I have three years in planning and can start immediately."
    llm = _FakeLLM(_cover(body), _cover("I plan well."))
    draft = await generate_cover(llm, user_prompt="x", valid_keys=set(), grounding=_OWN,
                                 posting=_POSTING)
    assert llm.calls == 1  # no re-ask
    assert draft.value.body_md == body
    marks = {(f.get("detail"), f["phrase"]) for f in draft.style_flags}
    assert {("time", "three years"), ("promise", "start immediately")} <= marks


def test_an_engagement_outline_keeps_the_number_check_only():
    from kestrel.docs.letters import unverified_details

    out = unverified_details("Over three months we will relocate the team.", ["x"], [], _POSTING,
                             cover=False)
    assert [f["phrase"] for f in out] == ["three"]
