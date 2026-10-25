# ruff: noqa: E501
"""The Interview prep screen (D-UI-121): every route and action, one test per state of the
locked mockup, and the rules the screen must keep (the operator's own words, nothing sent
without a confirmation, fewer than five sessions shows no trend, the current employer redacted).

Cloud-safe: database and web layer only; the model is a stand-in and nothing touches the network.
The fictional roles and stories are the mockup's (tests/prep_seed.py)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db.engine import make_async_engine
from kestrel.db.models import MockSession, Pursuit, Setting, Story, StoryUsage
from kestrel.web import prep_view as pv
from tests import prep_seed

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

HX = {"HX-Request": "true", "HX-Target": "pp-body"}
LARK = "Larkspur Logistics"


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _sm() -> async_sessionmaker:
    return async_sessionmaker(make_async_engine(paths.db_path()), expire_on_commit=False)


async def _get(model, id_):
    async with _sm()() as s:
        return await s.get(model, id_)


async def _all(model):
    async with _sm()() as s:
        return list((await s.execute(select(model))).scalars().all())


def _toast(r) -> dict:
    return json.loads(r.headers["HX-Trigger"])["k-toast"]


def _qh(text: str) -> str:
    return pv.qhash(text)


Q1 = prep_seed.QUESTIONS[0][0]
Q4 = prep_seed.QUESTIONS[3][0]


# ------------------------------------------------------------------------------ the header
async def test_header_tabs_and_the_pack_tiles():
    ids = await prep_seed.seed_async(with_pack=True)
    opp = ids[LARK]["opp"]
    with _client() as c:
        t = c.get(f"/prep/{opp}").text
    assert "<h1>Interview prep</h1>" in t and "Applications</a>" in t
    for key in ("pack", "practice", "bank"):
        assert f'data-testid="pp-tab-{key}"' in t
    assert "Prep pack" in t and "Story bank" in t and ">How it works<" in t.replace("\n", "")
    assert "Download PDF" in t
    assert "Senior Operations Analyst" in t and "Panel interview" in t and "in 5 days" in t
    assert "3 of 5 questions have a story" in t
    assert 'data-testid="pp-need">2<' in t and 'data-testid="pp-ask-n">3<' in t
    assert "5 days" in t and "Until the interview" in t
    # mapped story chip opens to the operator's own words; unmapped says so
    assert "Throughput turnaround at two sites" in t and "Your words, as you wrote them." in t
    assert t.count('data-testid="pp-nostory"') == 2 and "+ I have a story for this" in t
    assert "Gaps and how to handle them" in t and "Noise item" not in t
    assert "Who you may meet" in t and "Maya Ruiz" in t and "More panelists?" in t
    assert "The role in one line" in t and "Questions to ask them" in t


async def test_role_picker_lists_roles_with_an_interview_booked():
    ids = await prep_seed.seed_async(with_pack=True)
    with _client() as c:
        t = c.get(f"/prep/{ids[LARK]['opp']}").text
    menu = t.split('data-testid="pp-rolemenu"')[1]
    assert "Process Improvement Lead" in menu and "Phone screen" in menu


async def test_consulting_speaks_discovery_call_prep():
    ids = await prep_seed.seed_async(with_pack=True, consulting=True)
    with _client() as c:
        t = c.get(f"/prep/{ids[LARK]['opp']}").text
        p = c.get(f"/prep/{ids[LARK]['opp']}?tab=practice").text
    assert "<h1>Discovery call prep</h1>" in t and "Call prep" in t
    assert "Leads</a>" in t and "What they will want to know" in t
    assert "Discovery call" in p and "Behavioral" not in p.split("pp-modes")[1].split("</section>")[0]


async def test_no_such_role_and_no_roles_states():
    await prep_seed.seed_async()
    with _client() as c:
        gone = c.get("/prep/9999").text
    assert "<h1>Interview prep</h1>" in gone and "Prep pack" not in gone
    assert "No such role" in gone
    await prep_seed.seed_async()
    async with _sm()() as s:
        for p in (await s.execute(select(Pursuit))).scalars().all():
            p.status = "applied"
        await s.commit()
    with _client() as c:
        home = c.get("/prep").text
        bank = c.get("/stories").text
    assert "No interview is booked yet" in home
    assert 'data-testid="pp-bank"' in bank  # the story bank is open without a role


async def test_the_body_alone_comes_for_an_htmx_request_and_tabs_are_urls():
    ids = await prep_seed.seed_async(with_pack=True)
    opp = ids[LARK]["opp"]
    with _client() as c:
        full = c.get(f"/prep/{opp}?tab=practice")
        part = c.get(f"/prep/{opp}?tab=practice", headers=HX)
        mock = c.get(f"/mock/{opp}").text
        stories = c.get("/stories").text
        nonsense = c.get(f"/prep/{opp}?tab=nope").text
    assert "<html" in full.text and "<html" not in part.text
    assert 'data-tab="practice"' in part.text
    assert 'data-testid="pp-start"' in mock and 'data-testid="pp-bank"' in stories
    assert 'data-tab="pack"' in nonsense
    assert c.get("/mock/9999").status_code == 404


async def test_the_page_uses_the_design_system_and_drops_the_legacy_sheet():
    ids = await prep_seed.seed_async(with_pack=True)
    with _client() as c:
        t = c.get(f"/prep/{ids[LARK]['opp']}").text
    assert "/static/css/pages/prep.css" in t and "/static/js/prep.js" in t
    assert "/static/css/shell.css" in t and "/static/css/kestrel.css" not in t
    assert "k-layout" in t and "k-help-col" in t and t.count("k-flyout") >= 5
    assert "—" not in re.sub(r"<script.*?</script>", "", t, flags=re.S)


# ----------------------------------------------------------------------------- empty and build
async def test_no_pack_offers_a_build_and_the_auto_build_link_builds_it():
    ids = await prep_seed.seed_async()
    opp = ids[LARK]["opp"]
    with _client() as c:
        empty = c.get(f"/prep/{opp}").text
        auto = c.get(f"/prep/{opp}?auto=1").text
        assert "No prep pack for this role yet" in empty and 'data-testid="pp-build"' in empty
        assert 'data-testid="prep-auto-build"' in auto and f'hx-post="/prep/{opp}/build"' in auto
        assert 'data-testid="pp-pdf"' in empty and "disabled" in empty.split('data-testid="pp-pdf"')[0][-120:]
        built = c.post(f"/prep/{opp}/build").text
        page = c.get(f"/prep/{opp}").text
    assert "3 of 5 questions have a story" in built and "No prep pack for this role" not in page
    assert 'data-testid="prep-auto-build"' not in page


# ------------------------------------------------------------------------------- notes
async def test_notes_autosave_and_survive_a_rebuild():
    ids = await prep_seed.seed_async(with_pack=True)
    opp = ids[LARK]["opp"]
    with _client() as c:
        r = c.post(f"/prep/{opp}/notes", data={"notes": "  Ask about the board.  "})
        assert 'data-testid="pp-saved"' in r.text and "is-on" in r.text
        c.post(f"/prep/{opp}/build")
        assert "Ask about the board." in c.get(f"/prep/{opp}").text
        assert c.post("/prep/9999/notes", data={"notes": "x"}).status_code == 404
        assert 'hx-trigger="input changed delay:700ms' in c.get(f"/prep/{opp}").text
    assert (await _get(Setting, f"prep_notes.{opp}")).value == "Ask about the board."


# ------------------------------------------------------------ swap story, and undo it
async def test_swap_story_is_kept_apart_from_the_pack_and_undo_restores_only_that():
    ids = await prep_seed.seed_async(with_pack=True)
    opp = ids[LARK]["opp"]
    stories = {s.title: s.id for s in await _all(Story)}
    new = stories["Onboarding two new analysts"]
    with _client() as c:
        r = c.post(f"/prep/{opp}/choose", data={"qh": _qh(Q1), "story_id": str(new)})
        undo = _toast(r)["undoUrl"]
        assert "Story swapped" in _toast(r)["message"]
        assert 'id="pp-qcard"' in r.text and 'hx-swap-oob="true"' in r.text
        assert "Onboarding two new analysts" in c.get(f"/prep/{opp}").text
        c.post(f"/prep/{opp}/build")  # a rebuild keeps the choice
        assert "Onboarding two new analysts" in c.get(f"/prep/{opp}").text
        # a later change is not undone by a stale token
        c.post(f"/prep/{opp}/choose",
               data={"qh": _qh(Q1), "story_id": str(stories["Rebuilding the demand forecast in SQL"])})
        stale = c.post(undo)
        assert 'data-ok="0"' in stale.text and "can no longer be undone" in stale.text
        assert "Rebuilding the demand forecast in SQL" in c.get(f"/prep/{opp}").text
        ok = _toast(c.post(f"/prep/{opp}/choose", data={"qh": _qh(Q1), "story_id": str(new)}))
        assert 'data-ok="1"' in c.post(ok["undoUrl"]).text
        bad = c.post(f"/prep/{opp}/choose", data={"qh": _qh(Q1), "story_id": "99999"})
        assert "no longer in your bank" in _toast(bad)["message"]


# ------------------------------------------------------------------- questions to ask them
async def test_tick_add_and_the_tile_count():
    ids = await prep_seed.seed_async(with_pack=True)
    opp = ids[LARK]["opp"]
    with _client() as c:
        r = c.post(f"/prep/{opp}/ask/tick", data={"i": "0"})  # unticked
        assert 'data-testid="pp-ask-n">2<' in r.text and 'id="pp-ready"' in r.text
        r = c.post(f"/prep/{opp}/ask/tick", data={"i": "3", "on": "1"})
        assert 'data-testid="pp-ask-n">3<' in r.text
        r = c.post(f"/prep/{opp}/ask", data={"question": "Who else decides?"})
        assert "Added to your questions." in _toast(r)["message"] and "Who else decides?" in r.text
        assert "Your question" in c.get(f"/prep/{opp}").text
        assert "Write a question first" in _toast(c.post(f"/prep/{opp}/ask", data={"question": " "}))["message"]


async def test_people_you_add_are_your_own_notes():
    ids = await prep_seed.seed_async(with_pack=True)
    opp = ids[LARK]["opp"]
    with _client() as c:
        r = c.post(f"/prep/{opp}/people", data={"name": "Sam Ortiz", "title": "Site lead"})
        assert "Sam Ortiz" in r.text and "Added by you" in r.text
        assert "Kestrel never contacts anyone" in c.get(f"/prep/{opp}").text
        assert "Write a name first" in _toast(c.post(f"/prep/{opp}/people", data={"name": ""}))["message"]


# ---------------------------------------------------------------------------- the story sheet
async def test_a_story_for_a_question_is_saved_linked_and_undone_only_if_untouched():
    ids = await prep_seed.seed_async(with_pack=True)
    opp = ids[LARK]["opp"]
    with _client() as c:
        sheet = c.get(f"/stories/sheet?from=pack&opp={opp}&qh={_qh(Q4)}").text
        assert "What was the situation?" in sheet and "What did you do?" in sheet
        assert "What changed?" in sheet and "will not rewrite your story" in sheet
        assert Q4 in sheet and 'data-testid="pp-sheet-for"' in sheet
        r = c.post("/stories", data={
            "from": "pack", "opp": str(opp), "qh": _qh(Q4),
            "situation": "I moved from food distribution to a 3PL with no logistics background.",
            "action": "I spent two weeks on the floor.", "result": "I ran my first review in week five."})
        t = _toast(r)
        assert "linked to this question" in t["message"] and "pp-close-sheet" in r.headers["HX-Trigger"]
        assert "4 of 5 questions have a story" in r.text
        new = [s for s in await _all(Story) if s.operator_edited and "3PL" in (s.situation or "")][0]
        assert new.title == "I moved from food distribution to a 3PL with no logistics background"[:52].rstrip()
        assert 'data-ok="1"' in c.post(t["undoUrl"]).text
        assert not [s for s in await _all(Story) if s.id == new.id]
        assert "3 of 5 questions have a story" in c.get(f"/prep/{opp}").text
        # a story the operator edited after saving is theirs: Undo leaves it alone
        r = c.post("/stories", data={"from": "bank", "situation": "A thing.", "action": "Did it."})
        undo = _toast(r)["undoUrl"]
        sid = [s for s in await _all(Story) if s.situation == "A thing."][0].id
        c.post(f"/stories/{sid}", data={"situation": "A thing, edited."})
        assert 'data-ok="0"' in c.post(undo).text
        assert (await _get(Story, sid)).situation == "A thing, edited."
        empty = c.post("/stories", data={"title": " "})
        assert "give the story a title" in _toast(empty)["message"]


async def test_a_theme_tile_tags_the_new_story_and_the_sheet_prefills_an_edit():
    await prep_seed.seed_async(with_pack=True)
    with _client() as c:
        bank = c.get("/stories").text
        assert "6 of 8 themes covered" in bank and "aim for 8 to 12 stories" in bank
        assert bank.count("</svg>No story yet</span>") == 2
        assert bank.count("</svg>One story</span>") == 6
        c.post("/stories", data={"theme": "failure", "situation": "The launch slipped.",
                                 "action": "I told them early."})
        again = c.get("/stories").text
        assert "7 of 8 themes covered" in again
        first = (await _all(Story))[0]
        sheet = c.get(f"/stories/sheet?from=bank&story={first.id}").text
        assert first.situation in sheet and first.action in sheet and "Delete" in sheet
        original = first.result
        c.post(f"/stories/{first.id}", data={"situation": first.situation + " ", "from": "bank"})
        saved = await _get(Story, first.id)
        assert saved.situation == first.situation + " " and saved.result == original
        assert saved.operator_edited is True


async def test_merge_proposals_are_reviewed_in_the_sheet_or_kept_separate():
    await prep_seed.seed_async(with_pack=True)
    async with _sm()() as s:
        s.add(Story(title="Forecast model rebuild", is_canonical=True, operator_edited=False,
                    themes=[], situation="A draft situation."))
        await s.commit()
    draft = [s for s in await _all(Story) if not s.operator_edited][0]
    with _client() as c:
        t = c.get("/stories").text
        assert "Kestrel found similar stories" in t and "1 to review" in t
        assert "Review and merge" in t and "Keep separate" in t
        assert "Merge: Forecast model rebuild" in c.get(f"/stories/sheet?story={draft.id}").text
        r = c.post(f"/stories/{draft.id}/dismiss", data={"from": "bank"})
        assert "Draft dismissed" in _toast(r)["message"]
        assert "already gone" in _toast(c.post(f"/stories/{draft.id}/dismiss"))["message"]
        assert "Kestrel found similar stories" not in c.get("/stories").text


async def test_keep_separate_undo_restores_exact_proposal_and_links_after_restart():
    ids = await prep_seed.seed_async(with_pack=True)
    async with _sm()() as session:
        story = Story(title="Proposal", situation="  My words.\n", task=None, action="Acted.",
                      themes=["turnaround"], use_count=7, is_canonical=True)
        session.add(story)
        await session.flush()
        usage = StoryUsage(story_id=story.id, opportunity_id=ids[LARK]["opp"], question=Q1)
        session.add(usage)
        await session.commit()
        before = {column.name: getattr(story, column.name) for column in Story.__table__.columns}
        link = {column.name: getattr(usage, column.name) for column in StoryUsage.__table__.columns}
    with _client() as client:
        response = client.post(f"/stories/{story.id}/dismiss", data={"from": "bank"})
        undo = _toast(response)["undoUrl"]
        assert 'id="pp-bank"' in response.text
    assert await _get(Story, story.id) is None
    assert await _get(StoryUsage, usage.id) is None
    with _client() as client:
        assert 'data-ok="1"' in client.post(undo).text
        assert "Kestrel found similar stories" in client.get("/stories").text
        assert 'data-ok="0"' in client.post(undo).text
    restored = await _get(Story, story.id)
    restored_link = await _get(StoryUsage, usage.id)
    assert {column.name: getattr(restored, column.name) for column in Story.__table__.columns} == before
    assert {column.name: getattr(restored_link, column.name) for column in StoryUsage.__table__.columns} == link


@pytest.mark.parametrize("conflict", ["expired", "story", "usage", "missing-role"])
async def test_keep_separate_undo_refuses_expiry_or_later_work(monkeypatch, conflict):
    ids = await prep_seed.seed_async(with_pack=True)
    async with _sm()() as session:
        story = Story(title="Proposal", is_canonical=True)
        session.add(story)
        await session.flush()
        usage = StoryUsage(story_id=story.id, opportunity_id=ids[LARK]["opp"], question=Q1)
        session.add(usage)
        await session.commit()
    with _client() as client:
        undo = _toast(client.post(f"/stories/{story.id}/dismiss"))["undoUrl"]
        async with _sm()() as session:
            if conflict == "expired":
                monkeypatch.setattr("kestrel.interview.stories.time.time", lambda: 10**12)
            elif conflict == "story":
                session.add(Story(id=story.id, title="Later work", operator_edited=True))
            elif conflict == "usage":
                session.add(StoryUsage(id=usage.id, story_id=(await _all(Story))[0].id,
                                       opportunity_id=ids[LARK]["opp"], question="Later question"))
            else:
                row = await session.get(Setting, "stories.dismiss_undo." + undo.rsplit("/", 1)[1])
                snapshot = json.loads(json.dumps(row.value))
                snapshot["usages"][0]["opportunity_id"] = 999999
                row.value = snapshot
            await session.commit()
        result = client.post(undo)
        assert 'data-ok="0"' in result.text and "This can no longer be undone." in result.text
    if conflict == "story":
        assert (await _get(Story, story.id)).title == "Later work"
    else:
        assert await _get(Story, story.id) is None


@pytest.mark.parametrize("changed", [False, True])
async def test_keep_separate_undo_only_reverts_its_own_dismissal_fingerprints(monkeypatch, changed):
    from kestrel.interview.stories import DISMISSED_KEY, Sketch, sketch_fingerprint

    ids = await prep_seed.seed_async(with_pack=True)
    sketch = Sketch(id=123, evaluation_id=1, opportunity_id=ids[LARK]["opp"], created_at="now",
                    question=Q1, situation="Original sketch", task="", action="", result="",
                    reflection="", proof_point_key=None)

    async def sketches(session):
        return [sketch]

    monkeypatch.setattr("kestrel.interview.stories.collect_sketches", sketches)
    async with _sm()() as session:
        story = Story(title="Proposal", is_canonical=True)
        session.add(story)
        session.add(Setting(key=DISMISSED_KEY, value={"123": "older fingerprint"}))
        await session.flush()
        session.add(StoryUsage(story_id=story.id, opportunity_id=ids[LARK]["opp"], question=Q1))
        await session.commit()
    with _client() as client:
        undo = _toast(client.post(f"/stories/{story.id}/dismiss"))["undoUrl"]
        assert (await _get(Setting, DISMISSED_KEY)).value["123"] == sketch_fingerprint(sketch)
        async with _sm()() as session:
            row = await session.get(Setting, DISMISSED_KEY)
            row.value = {**row.value, "other": "later dismissal",
                         **({"123": "later change"} if changed else {})}
            await session.commit()
        assert f'data-ok="{0 if changed else 1}"' in client.post(undo).text
    saved = (await _get(Setting, DISMISSED_KEY)).value
    assert saved["other"] == "later dismissal"
    assert saved["123"] == ("later change" if changed else "older fingerprint")


async def test_delete_asks_first_and_removes_the_story():
    await prep_seed.seed_async(with_pack=True)
    sid = (await _all(Story))[0].id
    with _client() as c:
        confirm = c.get(f"/stories/{sid}/delete-confirm?from=bank").text
        assert "This cannot be undone." in confirm and "Delete story" in confirm
        r = c.post(f"/stories/{sid}/delete", data={"from": "bank"})
        assert _toast(r)["message"] == "Story deleted."
        assert "already gone" in _toast(c.post(f"/stories/{sid}/delete"))["message"]
        assert c.get(f"/stories/{sid}/delete-confirm").status_code == 404


async def test_the_story_bank_keeps_notes_build_and_status():
    ids = await prep_seed.seed_async(with_pack=True)
    with _client() as c:
        t = c.get("/stories").text
        assert 'data-testid="your-notes"' in t and 'data-testid="no-notes"' in t
        assert "on 0 roles and the interview questions" in " ".join(t.split())
        assert 'data-testid="add-notes"' in t and "Process Improvement Lead" in t
        r = c.post("/stories/build")
        assert "Building stories in the background" in _toast(r)["message"]
        assert "already running" in _toast(c.post("/stories/build"))["message"]
        assert "Building now" in c.get("/stories").text and 'hx-trigger="every 5s"' in c.get("/stories").text
        red = c.get(f"/stories/notes/add?opportunity_id={ids[LARK]['opp']}", follow_redirects=False)
    assert red.headers["location"] == f"/prep/{ids[LARK]['opp']}#prep-notes"


# ---------------------------------------------------------------------------------- practice
class _Fake:
    calls: list = []


async def _fake_submit(session, client, mock_session_id, answer, *, ran_on=None, **_k):
    _Fake.calls.append((answer, ran_on, client.settings.llm.tiers["tier2"].backend))
    ms = await session.get(MockSession, mock_session_id)
    t = [dict(x) for x in ms.transcript]
    t[-1].update(answer=answer, ran_on=ran_on,
                 critique={"worked_well": "Clear result.", "improvements": ["Cut the setup."]},
                 scores={"structure": 4.5, "specificity": 4.0, "relevance": 4.5, "length": 3.0,
                         "ownership": 4},
                 unsupported_claims=["cut costs by 30%"])
    t.append({"question": "Next one?", "answer": None, "critique": None, "scores": None,
              "unsupported_claims": None})
    ms.transcript, ms.question_count = t, (ms.question_count or 0) + 1
    ms.mean_scores = {"structure": 4.5}
    await session.commit()
    return t[-2]


async def test_practice_start_answer_feedback_claims_skip_and_end(monkeypatch):
    monkeypatch.setattr("kestrel.web.pages.mock.submit_answer", _fake_submit)
    _Fake.calls.clear()
    ids = await prep_seed.seed_async(with_pack=True)
    opp = ids[LARK]["opp"]
    with _client() as c:
        page = c.get(f"/prep/{opp}?tab=practice&qh={_qh(Q1)}").text
        assert "Runs on your computer" in page and "Behavioral" in page and "Leadership" in page
        assert "Executive panel" in page and f"Starting with: <b>{Q1}</b>" in page
        assert "Not enough sessions yet to show a trend (0 of 5)" in page
        bad = c.post(f"/mock/{opp}/start", data={"mode": "nope"})
        assert "Choose a mode first." in bad.text
        r = c.post(f"/mock/{opp}/start", data={"mode": "behavioral", "qh": _qh(Q1)})
        assert Q1 in r.text and "Question 1" in r.text and 'id="pp-chat"' in r.text
        ms = (await _all(MockSession))[0]
        assert "Show a hint" in r.text and "Throughput turnaround" in r.text  # the matched story
        skipped = c.post(f"/mock/session/{ms.id}/skip")
        assert Q1 not in skipped.text.split('data-testid="pp-pending"')[1][:300]
        empty = c.post(f"/mock/session/{ms.id}/answer", data={"answer": " "})
        assert "Write an answer before submitting." in empty.text
        a = c.post(f"/mock/session/{ms.id}/answer", data={"answer": "We cut costs by 30%."})
        assert "Structure" in a.text and "Specific numbers" in a.text and "Answered it" in a.text
        assert "4.5" in a.text and "Clear result." in a.text and "On your computer" in a.text
        assert '"cut costs by 30%"' in a.text and "Add it as a story" in a.text and "Got it" in a.text
        assert "Next question" in a.text and "End session" in a.text
        assert _Fake.calls[0][1] == "computer"
        ok = c.post(f"/mock/session/{ms.id}/claim", data={"turn": "0", "claim": "0"})
        assert ok.status_code == 200
        assert "Add it as a story" not in c.get(f"/prep/{opp}?tab=practice").text
        done = c.post(f"/mock/session/{ms.id}/end")
        assert "Session saved: 1 answer." in _toast(done)["message"]
        assert 'data-testid="pp-start"' in done.text
        review = c.get(f"/mock/session/{ms.id}").text
        assert "This session is finished." in review and "We cut costs by 30%." in review
        assert "Not enough sessions yet" in c.get(f"/prep/{opp}?tab=practice").text


async def test_a_dead_model_degrades_to_a_message(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("kestrel.web.pages.mock.submit_answer", boom)
    ids = await prep_seed.seed_async(with_pack=True)
    opp = ids[LARK]["opp"]
    with _client() as c:
        c.post(f"/mock/{opp}/start", data={"mode": "behavioral"})
        ms = (await _all(MockSession))[0]
        r = c.post(f"/mock/session/{ms.id}/answer", data={"answer": "Hello."})
    assert r.status_code == 200 and "Practice needs the model running" in r.text


async def test_five_finished_sessions_show_a_trend_instead_of_the_notice():
    ids = await prep_seed.seed_async(with_pack=True)
    opp = ids[LARK]["opp"]
    async with _sm()() as s:
        for k in range(5):
            s.add(MockSession(opportunity_id=opp, mode="behavioral", question_count=2,
                              started_at=f"2026-10-0{k + 1}T10:00:00+00:00",
                              ended_at=f"2026-10-0{k + 1}T10:20:00+00:00",
                              mean_scores={"structure": 3 + k / 10}, transcript=[]))
        await s.commit()
    with _client() as c:
        t = c.get(f"/prep/{opp}?tab=practice").text
    assert 'data-testid="pp-trend"' in t and "5 finished sessions" in t
    assert "Not enough sessions yet" not in t


# ------------------------------------------------------------ where practice runs (online)
def _hosted(monkeypatch, *, employer="hidden", native=False):
    from kestrel.web.pages import mock as m

    async def info(session, settings, *, with_models=False):
        base = {"online": native, "native": native, "can_switch": True, "employer": employer,
                "practice_sends": m.pv._PRACTICE_SENDS, "provider": "OpenRouter",
                "sends": "The role text and your profile", "model": "vendor/model",
                "dest": "b|https://x.example/v1|vendor/model",
                "models": [{"id": "vendor/model", "name": "Vendor model", "cost": "about 2 cents"}]}
        return base

    monkeypatch.setattr(m.pv, "route_info", info)
    monkeypatch.setattr(m, "route_info", info)


async def test_the_online_dialog_states_what_is_sent_and_the_answer_route_checks_it(monkeypatch):
    monkeypatch.setattr("kestrel.web.pages.mock.submit_answer", _fake_submit)
    _Fake.calls.clear()
    _hosted(monkeypatch)
    ids = await prep_seed.seed_async(with_pack=True)
    opp = ids[LARK]["opp"]
    with _client() as c:
        d = c.get(f"/mock/{opp}/online").text
        assert "Use an online model for this practice?" in d and "OpenRouter" in d
        assert "The role text and your profile" in d and "each question and your typed answer" in d
        assert "your current employer is hidden" in d and "about 2 cents" in d
        assert "Stay on my computer" in d and "Use online model" in d
        c.post(f"/mock/{opp}/start", data={"mode": "behavioral"})
        ms = (await _all(MockSession))[0]
        # no confirmation: refused, nothing sent
        r = c.post(f"/mock/session/{ms.id}/answer",
                   data={"answer": "x", "route_model": "hosted:vendor/model"})
        assert "Nothing was sent. Confirm the online model first." in r.text
        # Hosted mode is off in this install: even a confirmed pick is refused
        r = c.post(f"/mock/session/{ms.id}/answer", data={
            "answer": "x", "route_model": "hosted:vendor/model", "confirmed": "1"})
        assert "Nothing was sent" in r.text and not _Fake.calls
        route = c.post(f"/mock/{opp}/route", data={"model": "hosted:vendor/model"})
        assert "Confirm in the dialog first" in route.text
        back = c.get(f"/mock/{opp}/route")
        assert "Runs on your computer" in back.text and "Nothing more is sent" in _toast(back)["message"]


async def test_a_held_employer_blocks_the_online_dialog_copy(monkeypatch):
    _hosted(monkeypatch, employer="held")
    ids = await prep_seed.seed_async(with_pack=True)
    with _client() as c:
        d = c.get(f"/mock/{ids[LARK]['opp']}/online").text
    assert "This send will be held." in d and "Choose your employer in Settings" in d


def _hosted_on(monkeypatch, *, offered=("vendor/model",)):
    """Hosted mode on, the offered list fixed, and the model client stood in for: nothing here
    touches the network. Returns the list of (tier, override) the answer route built."""
    from kestrel.config import HostedMode
    from kestrel.web.pages import mock as m
    from kestrel.web.pages import needs_try, rescore

    built: list = []
    monkeypatch.setattr(HostedMode, "active", property(lambda self: True))

    async def offered_models(settings, description):
        return [{"id": i, "name": i, "cost": "about 2 cents"} for i in offered], {}

    async def override_for(settings, pick):
        return {"backend": "local", "model": pick.removeprefix("hosted:")}, "label", None

    def with_override(settings, tier, override):
        built.append((tier, override))
        return settings

    monkeypatch.setattr(needs_try, "_offered_models", offered_models)
    monkeypatch.setattr(rescore, "_override_for", override_for)
    monkeypatch.setattr(m, "settings_with_override", with_override)
    return built


async def _started(c, opp):
    c.post(f"/mock/{opp}/start", data={"mode": "behavioral"})
    return (await _all(MockSession))[0]


async def test_a_confirmed_online_answer_reaches_the_model_and_says_where_it_ran(monkeypatch):
    monkeypatch.setattr("kestrel.web.pages.mock.submit_answer", _fake_submit)
    _Fake.calls.clear()
    _hosted(monkeypatch)
    built = _hosted_on(monkeypatch)
    ids = await prep_seed.seed_async(with_pack=True)
    opp = ids[LARK]["opp"]
    with _client() as c:
        ms = await _started(c, opp)
        r = c.post(f"/mock/session/{ms.id}/answer", data={
            "answer": "We cut costs.", "route_model": "hosted:vendor/model", "confirmed": "1"})
    assert "Nothing was sent" not in r.text and "Answered it" in r.text
    assert [(a, ran) for a, ran, _b in _Fake.calls] == [("We cut costs.", "online")]
    assert built == [(pv.PRACTICE_TIER, {"backend": "local", "model": "vendor/model"})]


async def test_the_answer_route_itself_refuses_a_held_employer(monkeypatch):
    monkeypatch.setattr("kestrel.web.pages.mock.submit_answer", _fake_submit)
    _Fake.calls.clear()
    _hosted(monkeypatch, employer="held")
    _hosted_on(monkeypatch)
    ids = await prep_seed.seed_async(with_pack=True)
    with _client() as c:
        ms = await _started(c, ids[LARK]["opp"])
        r = c.post(f"/mock/session/{ms.id}/answer", data={
            "answer": "x", "route_model": "hosted:vendor/model", "confirmed": "1"})
    assert "Confidential mode is on and no current employer is chosen" in r.text
    assert not _Fake.calls


async def test_a_model_that_is_not_on_the_offered_list_is_refused(monkeypatch):
    monkeypatch.setattr("kestrel.web.pages.mock.submit_answer", _fake_submit)
    _Fake.calls.clear()
    _hosted(monkeypatch)
    built = _hosted_on(monkeypatch)
    ids = await prep_seed.seed_async(with_pack=True)
    with _client() as c:
        ms = await _started(c, ids[LARK]["opp"])
        r = c.post(f"/mock/session/{ms.id}/answer", data={
            "answer": "x", "route_model": "hosted:other/model", "confirmed": "1"})
    assert "That model is not on the online list." in r.text
    assert not _Fake.calls and not built


async def test_a_native_online_model_needs_the_matching_confirmation(monkeypatch):
    monkeypatch.setattr("kestrel.web.pages.mock.submit_answer", _fake_submit)
    _Fake.calls.clear()
    _hosted(monkeypatch, native=True)
    ids = await prep_seed.seed_async(with_pack=True)
    with _client() as c:
        ms = await _started(c, ids[LARK]["opp"])
        wrong = c.post(f"/mock/session/{ms.id}/answer", data={
            "answer": "x", "confirmed": "1", "route_model_native": "some/other-model"})
        assert "Nothing was sent. Confirm the online model first." in wrong.text
        assert not _Fake.calls
        ok = c.post(f"/mock/session/{ms.id}/answer", data={
            "answer": "x", "confirmed": "1", "route_model_native": "vendor/model"})
        assert "Nothing was sent. Confirm the online model first." in ok.text  # no destination
        assert not _Fake.calls
        ok = c.post(f"/mock/session/{ms.id}/answer", data={
            "answer": "x", "confirmed": "1", "route_model_native": "vendor/model",
            "dest": "b|https://x.example/v1|vendor/model"})
    assert "Answered it" in ok.text
    assert [ran for _a, ran, _b in _Fake.calls] == ["online"]


# ------------------------------------------------------------------------ PDF and preview
async def test_preview_shows_the_true_pages_and_the_pdf_uses_the_same_ones(monkeypatch):
    ids = await prep_seed.seed_async(with_pack=True)
    opp = ids[LARK]["opp"]
    pack = (await _all(__import__("kestrel.db.models", fromlist=["PrepPack"]).PrepPack))[0]
    seen = {}

    async def fake_render(html):
        from types import SimpleNamespace

        seen["html"] = html
        return SimpleNamespace(pdf=b"%PDF-1.4 fake", page_count=2)

    monkeypatch.setattr("kestrel.docs.render.render_pdf", fake_render)
    with _client() as c:
        c.post(f"/prep/{opp}/ask/tick", data={"i": "3", "on": "1"})
        p = c.get(f"/prep/{opp}/preview").text
        assert "Your prep pack" in p and "Page <span" in p and "#fff" in p  # paper stays white
        assert "Your story: Throughput turnaround at two sites." in p
        assert "How is the second distribution center changing the team?" in p
        assert "Say your top three stories out loud once" in p and "Page 1 of" in p
        r = c.post(f"/prep/{pack.id}/pdf")
        assert 'data-testid="pp-pdf-ok"' in r.text
        trig = json.loads(r.headers["HX-Trigger"])
        assert trig["pp-download"]["url"] == f"/prep/{pack.id}/download"
        assert re.fullmatch(r"prep-pack-larkspur-logistics-\d{4}-\d\d-\d\d\.pdf",
                            trig["pp-download"]["name"])
        d = c.get(f"/prep/{pack.id}/download")
        assert d.status_code == 200 and d.content.startswith(b"%PDF")
        assert "prep-pack-larkspur-logistics" in d.headers["content-disposition"]
        assert "Larkspur" in seen["html"] and "<title>Interview prep: " in seen["html"]
        assert c.post("/prep/9999/pdf").status_code == 404
        assert c.get("/prep/9999/download").status_code == 404


async def test_a_missing_browser_says_so_instead_of_failing(monkeypatch):
    ids = await prep_seed.seed_async(with_pack=True)

    async def boom(html):
        raise RuntimeError("no browser")

    monkeypatch.setattr("kestrel.docs.render.render_pdf", boom)
    pack = (await _all(__import__("kestrel.db.models", fromlist=["PrepPack"]).PrepPack))[0]
    with _client() as c:
        r = c.post(f"/prep/{pack.id}/pdf")
    assert r.status_code == 200 and 'data-testid="pp-pdf-error"' in r.text and "Edge" in r.text
    assert ids


def test_the_paged_pack_splits_long_lists_and_numbers_the_pages():
    from kestrel.interview.render import pack_pages

    model = {"title": "Interview prep", "ask_heading": "What they will likely ask",
             "role": "Role", "org": "Org", "when": "Date not set", "summary": "One line.",
             "questions": [{"q": f"Question number {i} " + "word " * 30,
                            "story_title": "A story",
                            "story_text": f"My own words {i}. " * 11 + f"End {i}."}
                           for i in range(14)],
             "asks": ["Ask me?"], "notes": []}
    pages = pack_pages(model)
    assert len(pages) >= 3 and "Page 1 of" in pages[0] and f"Page {len(pages)} of {len(pages)}" in pages[-1]
    # Hard rule 2: every story reaches the pages as the operator's own text, byte for byte.
    import html

    joined = html.unescape(" ".join(pages))
    for q in model["questions"]:
        assert q["story_text"] in joined, q["story_text"][:30]
    assert "Checklist" in pages[-1]


# ---------------------------------------------------------------------- privacy and style
async def test_confidential_mode_redacts_the_current_employer_on_the_page(monkeypatch):
    from kestrel.privacy import Employer, EmployerMap

    ids = await prep_seed.seed_async(with_pack=True)

    async def fake(session):
        return True, EmployerMap([Employer(name="Larkspur Logistics", shown_as="your employer")])

    monkeypatch.setattr("kestrel.web.prep_view.confidential_once", fake)
    with _client() as c:
        t = c.get(f"/prep/{ids[LARK]['opp']}").text
    body = t.split('id="pp-body"')[1]
    assert "Larkspur Logistics" not in body


@pytest.mark.parametrize("surface", ["start", "reload", "empty_answer"])
@pytest.mark.parametrize("confidential", [True, False])
async def test_practice_redacts_question_and_matching_story_without_changing_storage(
    monkeypatch, surface, confidential,
):
    from kestrel.privacy import Employer, EmployerMap

    employer = "Secret Employer"
    question = f"Describe your work at {employer}."
    original = prep_seed.QUESTIONS[0]
    monkeypatch.setattr(prep_seed, "QUESTIONS", [(question, *original[1:])])
    ids = await prep_seed.seed_async(with_pack=True)
    opportunity_id = ids[LARK]["opp"]
    async with _sm()() as session:
        story = (await session.execute(
            select(Story).where(Story.proof_point_key == original[2])
        )).scalar_one()
        story.title = f"{employer} project"
        story.result = f"Improved throughput at {employer}."
        story_id = story.id
        await session.commit()

    async def privacy(session):
        return confidential, EmployerMap([Employer(name=employer, shown_as="your employer")])

    monkeypatch.setattr(pv, "confidential_once", privacy)
    with _client() as client:
        response = client.post(
            f"/mock/{opportunity_id}/start", data={"mode": "behavioral", "qh": _qh(question)},
        )
        mock_session = (await _all(MockSession))[0]
        if surface == "reload":
            response = client.get(f"/mock/{opportunity_id}")
        elif surface == "empty_answer":
            response = client.post(f"/mock/session/{mock_session.id}/answer", data={"answer": ""})
    assert response.status_code == 200
    display_name = "your employer" if confidential else employer
    assert f"Describe your work at {display_name}." in response.text
    assert f"{display_name} project" in response.text
    assert f"Improved throughput at {display_name}." in response.text
    if confidential:
        assert employer not in response.text
    saved_story = await _get(Story, story_id)
    assert saved_story.title == f"{employer} project"
    assert saved_story.result == f"Improved throughput at {employer}."
    assert pv.pending_question(await _get(MockSession, mock_session.id)) == question


@pytest.mark.parametrize("prefix", ["", "x" * 132 + " "])
async def test_story_bank_redacts_note_previews_and_role_choices(monkeypatch, prefix):
    from kestrel.privacy import Employer, EmployerMap

    ids = await prep_seed.seed_async(with_pack=True)
    opportunity_id = ids[LARK]["opp"]
    note = f"{prefix}Ask about {LARK}." if not prefix else f"{prefix}{LARK}."

    async def privacy(session):
        return True, EmployerMap([Employer(name=LARK, shown_as="your employer")])

    monkeypatch.setattr(pv, "confidential_once", privacy)
    with _client() as client:
        client.post(f"/prep/{opportunity_id}/notes", data={"notes": note})
        response = client.get("/stories")
    body = response.text.split('id="pp-body"', 1)[1]
    assert LARK not in body
    assert " ".join(note.replace(LARK, "your employer").split())[:140] in body
    assert "Larksp" not in body
    assert (await _get(Setting, f"prep_notes.{opportunity_id}")).value == note


def test_the_page_style_and_markup_use_theme_tokens_only():
    static = Path(paths.web_static_dir())
    for rel in ("css/pages/prep.css", "js/prep.js"):
        text = (static / rel).read_text(encoding="utf-8")
        assert not re.search(r"#[0-9A-Fa-f]{3,8}\b", text), rel
        assert not re.search(r"\b(?:rgb|rgba|hsl|hsla)\(", text), rel
        assert "—" not in text
    templates = Path(paths.web_templates_dir())
    names = ["prep.html", *("partials/" + p.name for p in (templates / "partials").glob("pp_*.html"))]
    for name in names:
        text = (templates / name).read_text(encoding="utf-8")
        assert "—" not in text, name
        for style in re.findall(r'style="([^"]*)"', text):
            assert "color" not in style and "background" not in style, (name, style)


async def test_story_add_undo_preserves_later_question_choice():
    from kestrel.interview import prep_state

    ids = await prep_seed.seed_async(with_pack=True)
    opp = ids[LARK]["opp"]
    with _client() as c:
        r = c.post(
            "/stories",
            data={
                "from": "pack",
                "opp": str(opp),
                "qh": _qh(Q4),
                "title": "New story",
                "situation": "Later work",
            },
        )
        story = [s for s in await _all(Story) if s.title == "New story"][0]
        async with _sm()() as s:
            await prep_state.set_story_choice(s, opp, Q4, None)
            await s.commit()
        assert 'data-ok="0"' in c.post(_toast(r)["undoUrl"]).text
    assert await _get(Story, story.id) is not None
