"""Issue #65: Build stories must not save junk drafts. Placeholder sketches ("Not available from
your profile") are never sent to the model, a refusal from the model is never saved, notes are the
operator's own text and are kept, a build with nothing usable says so, and "Clear all drafts"
dismisses proposals only. Offline: fake model client, migrated SQLite."""

from __future__ import annotations

import json

from sqlalchemy import func, select

from kestrel.config import get_settings
from kestrel.db.models import Setting, Story
from kestrel.interview.notes import save_notes
from kestrel.interview.stories import (
    DISMISSED_KEY,
    REFUSED_KEY,
    Sketch,
    consolidate_stories,
    dismiss_all_drafts,
    is_placeholder_sketch,
    undo_dismissal_group,
)
from kestrel.llm.client import ChatResult
from tests.test_interview_stories import _P8, _add_eval, _FakeClient, _seed_opp, _sketch
from tests.test_pursuit_board import _fresh, _run
from tests.test_r3_copy import _tmsg

_PLACEHOLDERS = ("Not available from your profile", "No content available.", "Your own example.")


def _blank(**kw) -> dict:
    base = {
        "likely_question": "Tell me about a turnaround.", "why_asked": "x", "proof_point_key": None,
        "star_r": {k: "Not available from your profile" for k in
                   ("situation", "task", "action", "result", "reflection")},
    }
    base.update(kw)
    return base


class _SpyClient(_FakeClient):
    """Records what is embedded and how many merge calls are made; answers with `reply`."""

    def __init__(self, reply: dict | None = None) -> None:
        super().__init__()
        self.embedded: list[str] = []
        self.completions = 0
        self.reply = _P8 if reply is None else reply

    async def embed(self, texts, *, tier: str = "embed", timeout: float = 60.0):
        self.embedded += list(texts)
        return await super().embed(texts, tier=tier, timeout=timeout)

    async def complete(self, *, tier, messages, schema=None, schema_name="response", **_):
        self.completions += 1
        return ChatResult(
            content=json.dumps(self.reply), prompt_tokens=1, completion_tokens=1, latency_ms=1,
            model=self.settings.llm.tiers[tier].model,
        )


def _sk(kind="sketch", **f) -> Sketch:
    vals = dict(situation="", task="", action="", result="", reflection="")
    vals.update(f)
    return Sketch(
        id=1, evaluation_id=1, opportunity_id=1, created_at="t", question="q",
        proof_point_key=None, kind=kind, **vals,
    )


def test_placeholder_check_is_conservative():
    assert is_placeholder_sketch(_sk())
    assert is_placeholder_sketch(_sk(situation="N/A", task="None", action="Unknown."))
    assert is_placeholder_sketch(_sk(situation="Not available from your profile.", result="n/a"))
    assert is_placeholder_sketch(_sk(situation="No content available.", task="Your own example."))
    assert not is_placeholder_sketch(_sk(situation="Not available", action="I rebuilt the API."))
    assert not is_placeholder_sketch(_sk(situation="No one told us the none of it was known."))
    # An operator note is their text: kept unless completely empty.
    assert not is_placeholder_sketch(_sk(kind="note", situation="n/a"))
    assert is_placeholder_sketch(_sk(kind="note"))


async def test_placeholder_sketches_are_not_embedded_or_merged(sm):
    opp = await _seed_opp(sm)
    await _add_eval(sm, opp, [_blank(), _blank(), _sketch("A hard call?", "omega")])
    client = _SpyClient()
    async with sm() as s:
        res = await consolidate_stories(s, client, get_settings())
    assert res.sketches == 1 and res.skipped == 2 and len(res.created_story_ids) == 1
    assert client.completions == 1
    assert not any("Not available" in t for t in client.embedded)
    async with sm() as s:
        # The blanks never held the watermark back: the next build has nothing new at all.
        again = await consolidate_stories(s, client, get_settings())
    assert again.sketches == 0 and client.completions == 1


async def test_only_placeholders_means_no_model_call_and_no_drafts(sm):
    opp = await _seed_opp(sm)
    await _add_eval(sm, opp, [_blank(), _blank(star_r={})])
    client = _SpyClient()
    async with sm() as s:
        res = await consolidate_stories(s, client, get_settings())
    assert client.embedded == [] and client.completions == 0
    assert res.created_story_ids == [] and res.skipped == 2
    async with sm() as s:
        assert (await s.execute(select(func.count()).select_from(Story))).scalar_one() == 0


async def _forget(sm) -> None:
    async with sm() as s:
        for k in ("consolidation.stories_watermark", DISMISSED_KEY, REFUSED_KEY):
            row = await s.get(Setting, k)
            if row is not None:
                await s.delete(row)
        await s.commit()


async def test_a_refusal_result_is_discarded_not_saved(sm):
    opp = await _seed_opp(sm)
    await _add_eval(sm, opp, [_sketch("Tell me about a turnaround.", "delta")])
    gone = {k: "Unresolved: sketches contain no substantive content"
            for k in ("situation", "task", "action", "result", "reflection")}
    refusals = (
        {**_P8, **gone, "title": "Unmergeable: no source content"},
        {**_P8, "title": ""},
        {**_P8, "situation": "No content available.", "task": "", "action": "n/a",
         "result": "Not available from your profile", "reflection": "None"},
        {**_P8, **gone, "title": "Canonical story could not be produced"},
        {**_P8, **gone, "title": "Unresolved: sketches contain no substantive content"},
    )
    for reply in refusals:
        await _forget(sm)
        client = _SpyClient(reply)
        async with sm() as s:
            res = await consolidate_stories(s, client, get_settings())
        assert res.created_story_ids == [] and res.skipped == 1, reply["title"]
        async with sm() as s:
            assert (await s.execute(select(func.count()).select_from(Story))).scalar_one() == 0
    # Nothing is hidden or dismissed: a wrong refusal must not bury real material. The refused
    # cluster is remembered by its sketches' fingerprints in its own key, so it is not asked again.
    await _forget(sm)
    client = _SpyClient(refusals[0])
    async with sm() as s:
        await consolidate_stories(s, client, get_settings())
        again = await consolidate_stories(s, client, get_settings())
        assert await s.get(Setting, DISMISSED_KEY) is None
        assert len((await s.get(Setting, REFUSED_KEY)).value) == 1
    assert client.completions == 1 and again.sketches == 0 and again.skipped == 1


async def test_a_refused_sketch_is_read_again_once_its_text_changes(sm):
    opp = await _seed_opp(sm)
    await _add_eval(sm, opp, [_sketch("Tell me about a turnaround.", "delta")])
    gone = {k: "n/a" for k in ("situation", "task", "action", "result", "reflection")}
    client = _SpyClient({**_P8, **gone, "title": "Unmergeable: nothing"})
    async with sm() as s:
        await consolidate_stories(s, client, get_settings())
        stored = dict((await s.get(Setting, REFUSED_KEY)).value)
        (key,) = stored
        (await s.get(Setting, REFUSED_KEY)).value = {key: "0" * 16, "77": "keep"}  # text changed
        await s.commit()
    async with sm() as s:
        await consolidate_stories(s, client, get_settings())
        merged = (await s.get(Setting, REFUSED_KEY)).value
    assert client.completions == 2  # asked again, because the fingerprint no longer matches
    assert merged[key] == stored[key] and merged["77"] == "keep"  # union on write


async def test_a_refused_note_is_never_remembered(sm):
    opp = await _seed_opp(sm)
    async with sm() as s:
        await save_notes(s, opp, "Cleared the backlog of unresolved incidents in Q3.")
    gone = {k: "Insufficient source material" for k in
            ("situation", "task", "action", "result", "reflection")}
    client = _SpyClient({**_P8, **gone, "title": "Insufficient source material"})
    for _ in range(2):
        async with sm() as s:
            await consolidate_stories(s, client, get_settings())
            assert await s.get(Setting, REFUSED_KEY) is None
    assert client.completions == 2


def test_refusal_check_is_strict():
    from kestrel.interview.stories import is_unusable_story as bad

    star = dict(situation="Cleared the backlog of unresolved incidents.", task="no story-point "
                "estimates existed", action="I wrote unrecoverable-state handlers.",
                result="Zero incidents left.", reflection="")
    assert not bad("Cleared the backlog of unresolved incidents", **star)
    assert not bad("Unresolved incidents turnaround", **star)  # leading word, real body
    assert not bad("No story-point estimates", **star)
    gone = {k: "no substantive content" for k in star}
    assert bad("Unmergeable: sketches differ", **gone)
    assert not bad("Fine title", **star)


async def test_a_discarded_merge_stores_no_dismissal_and_keeps_notes(sm):
    opp = await _seed_opp(sm)
    async with sm() as s:
        await save_notes(s, opp, "Cleared the backlog of unresolved incidents in Q3.")
    gone = {k: "Insufficient source material" for k in
            ("situation", "task", "action", "result", "reflection")}
    client = _SpyClient({**_P8, **gone, "title": "Insufficient source material"})
    async with sm() as s:
        await consolidate_stories(s, client, get_settings())
        assert await s.get(Setting, DISMISSED_KEY) is None
    client.reply = _P8
    async with sm() as s:
        res = await consolidate_stories(s, client, get_settings())
    assert res.sketches == 1 and len(res.created_story_ids) == 1


async def test_a_dismissal_made_during_a_build_survives_it(sm):
    opp = await _seed_opp(sm)
    await _add_eval(sm, opp, [_sketch("Tell me about a turnaround.", "delta")])
    gone = {k: "n/a" for k in ("situation", "task", "action", "result", "reflection")}

    class _Racy(_SpyClient):
        async def complete(self, **kw):
            async with sm() as other:  # an operator dismissal lands mid-build
                other.add(Setting(key=DISMISSED_KEY, value={"999": "abc"}))
                await other.commit()
            return await super().complete(**kw)

    client = _Racy({**_P8, **gone, "title": "Unmergeable: nothing"})
    async with sm() as s:
        await consolidate_stories(s, client, get_settings())
    async with sm() as s:
        row = await s.get(Setting, DISMISSED_KEY)
        assert row is not None and row.value.get("999") == "abc"


async def test_operator_notes_are_kept(sm):
    opp = await _seed_opp(sm)
    async with sm() as s:
        await save_notes(s, opp, "n/a")  # their own text, short as it is
    client = _SpyClient()
    async with sm() as s:
        res = await consolidate_stories(s, client, get_settings())
    assert res.sketches == 1 and res.skipped == 0 and client.completions == 1


async def test_a_build_with_nothing_usable_says_so(sm):
    from kestrel.interview import story_task

    opp = await _seed_opp(sm)
    await _add_eval(sm, opp, [_blank()])
    async with sm() as s:
        assert await story_task.request_build(s)
    await story_task.drain_story_builds(sm, _SpyClient(), get_settings())
    async with sm() as s:
        last = await story_task.last_build(s)
    assert last["ok"] and last["created"] == 0 and last["attached"] == 0 and last["skipped"] == 1
    assert not hasattr(story_task, "NOTHING_TO_BUILD")


def test_the_page_shows_the_nothing_to_build_outcome():
    c = _fresh()

    async def _set(s):
        s.add(Setting(key="stories.last_build", value={
            "ok": True, "sketches": 0, "created": 0, "attached": 0, "skipped": 3}))
        await s.commit()

    _run(_set)
    with c:
        page = " ".join(c.get("/stories").text.split())
    assert "Nothing to build from yet. Add proof points on Profile &gt; Documents, then score " \
           "your roles again." in page
    assert "—" not in page.split("<main", 1)[-1]


def test_clear_all_dismisses_proposals_only_and_can_be_undone():
    c = _fresh()

    async def _add(s):
        s.add_all([
            Story(title="Draft A", is_canonical=True, operator_edited=False, themes=[]),
            Story(title="Draft B", is_canonical=True, operator_edited=False, themes=[]),
            Story(title="Mine", is_canonical=True, operator_edited=True, themes=[],
                  situation="my words"),
        ])
        await s.commit()

    _run(_add)
    with c:
        page = c.get("/stories").text
        assert 'hx-post="/stories/dismiss-all"' in page and "hx-confirm=" in page
        assert 'data-testid="clear-drafts"' in page and "Clear all drafts" in page
        r = c.post("/stories/dismiss-all", headers={"HX-Request": "true"})
        assert _tmsg(r) == "Cleared 2 drafts. Build stories will not propose them again."
        undo = json.loads(r.headers["HX-Trigger"])["k-toast"]["undoUrl"]
        listing = c.get("/stories").text
        assert "Draft A" not in listing and "Draft B" not in listing and "Mine" in listing
        assert "Clear all drafts" not in listing
        assert "no drafts" in _tmsg(c.post("/stories/dismiss-all")).lower()
        assert 'data-ok="1"' in c.post(undo).text
        back = c.get("/stories").text
        assert "Draft A" in back and "Draft B" in back and "Mine" in back
        assert 'data-ok="0"' in c.post(undo).text  # once only


async def test_clear_all_never_touches_operator_edited_stories(sm):
    async with sm() as s:
        s.add_all([
            Story(title="Draft", is_canonical=True, operator_edited=False, themes=[]),
            Story(title="Mine", is_canonical=True, operator_edited=True, themes=[]),
        ])
        await s.commit()
    async with sm() as s:
        count, group = await dismiss_all_drafts(s)
    assert count == 1 and group
    async with sm() as s:
        assert [t for (t,) in (await s.execute(select(Story.title))).all()] == ["Mine"]
        assert await dismiss_all_drafts(s) == (0, None)
        assert await undo_dismissal_group(s, group) is True
        assert await undo_dismissal_group(s, group) is False


def test_profile_stories_list_has_clear_all_and_keeps_saved_stories():
    """The owner met the junk drafts on Profile > Stories, so Clear all lives there too."""
    c = _fresh()

    async def _add(s):
        s.add_all([
            Story(title="Draft A", is_canonical=True, operator_edited=False, themes=[]),
            Story(title="Mine", is_canonical=True, operator_edited=True, themes=[],
                  situation="my words"),
        ])
        await s.commit()

    _run(_add)
    with c:
        r = c.post("/profile/stories/dismiss-all", headers={"HX-Request": "true"})
        assert r.status_code == 200
        assert "Cleared 1 draft. Build stories will not propose them again." in r.text
        assert "Draft A" not in r.text and "Mine" in r.text
        assert 'data-testid="pf-clear-drafts"' not in r.text  # no drafts left, no button
        again = c.post("/profile/stories/dismiss-all", headers={"HX-Request": "true"})
        assert "There are no drafts to clear." in again.text


def _bank_status(last_build: dict) -> str:
    c = _fresh()

    async def _set(s):
        s.add(Setting(key="stories.last_build", value=last_build))
        await s.commit()

    _run(_set)
    with c:
        return " ".join(c.get("/stories").text.split())


def test_nothing_new_wording_for_a_full_profile_build():
    page = _bank_status({"ok": True, "sketches": 4, "created": 0, "attached": 0, "skipped": 1})
    assert "Nothing new to build from: save notes on a prep page, or score more roles." in page
    assert "Add proof points on Profile" not in page
    page = _bank_status({"ok": True, "sketches": 0, "created": 0, "attached": 0, "skipped": 0})
    assert "Nothing new to build from" in page


async def test_group_undo_survives_an_item_that_cannot_come_back(sm):
    async with sm() as s:
        s.add_all([Story(title=f"Draft {i}", is_canonical=True, operator_edited=False, themes=[])
                   for i in range(3)])
        await s.commit()
    async with sm() as s:
        count, group = await dismiss_all_drafts(s)
        assert count == 3
    async with sm() as s:  # the first item to be restored (last dismissed) is blocked
        s.add(Story(id=3, title="Squatter", is_canonical=True, operator_edited=True, themes=[]))
        await s.commit()
    async with sm() as s:
        assert await undo_dismissal_group(s, group) is True
        titles = {t for (t,) in (await s.execute(select(Story.title))).all()}
    assert {"Draft 0", "Draft 1", "Squatter"} <= titles


def test_profile_clear_all_offers_undo():
    c = _fresh()

    async def _add(s):
        s.add(Story(title="Draft A", is_canonical=True, operator_edited=False, themes=[]))
        await s.commit()

    _run(_add)
    with c:
        r = c.post("/profile/stories/dismiss-all", headers={"HX-Request": "true"})
        assert 'data-testid="pf-undo-clear"' in r.text
        assert "/profile/stories/undo-dismiss-all/" in r.text
        url = r.text.split('hx-post="/profile/stories/undo-dismiss-all/', 1)[1].split('"', 1)[0]
        back = c.post("/profile/stories/undo-dismiss-all/" + url, headers={"HX-Request": "true"})
        assert "Draft A" in back.text and "Drafts restored." in back.text
