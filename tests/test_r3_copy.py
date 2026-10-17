"""Release round R3 (D-R3-1 to D-R3-5): plain labels instead of jargon, no em dashes on screen,
track words, interview prep reachable from every role, story notes / Dismiss / Delete, and the
README / Help / About promises. Offline: a migrated SQLite file and the test client."""

from __future__ import annotations

import html as htmllib
import re
from pathlib import Path

from sqlalchemy import func, select

from kestrel.config import get_settings
from kestrel.db.models import Setting, Story, StoryUsage
from kestrel.interview.notes import save_notes
from kestrel.interview.stories import (
    DISMISSED_KEY,
    consolidate_stories,
    delete_story,
    dismiss_draft,
)
from tests.test_interview_stories import _add_eval, _FakeClient, _seed_opp, _sketch
from tests.test_pursuit_board import _fresh, _run, _seed

_ROOT = Path(__file__).resolve().parent.parent
_MAIN_PAGES = ["/", "/opportunities", "/pursuits", "/documents", "/stories", "/analytics",
               "/help", "/about", "/models", "/sources", "/calibrate", "/runs", "/bench",
               "/quarantine", "/offers", "/backup"]
_DASH = chr(0x2014)


def _visible(page: str) -> str:
    """The text a person reads: no scripts, styles, comments or tags (so no title= tooltips)."""
    body = page.split("<main", 1)[-1]
    body = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", body)
    body = re.sub(r"<!--[\s\S]*?-->", " ", body)
    body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", htmllib.unescape(body))


# --------------------------------------------------------------------------- em dashes
def test_no_em_dash_reaches_the_screen_on_the_main_pages():
    c = _fresh()
    ids = _seed(status="preparing", title="Prep role")
    with c:
        for path in [*_MAIN_PAGES, f"/opportunities/{ids[0]}", f"/prep/{ids[0]}"]:
            r = c.get(path)
            assert r.status_code == 200, path
            assert _DASH not in htmllib.unescape(r.text), path


# ----------------------------------------------------------------------------- jargon
_JARGON = (r"Tier [0-3]", r"Tier[0-3]", "Quarantine", "Worker idle", r"\bn=0\b", "derived model",
           "Ollama not reachable", "context window", r"cpu_8gb", "Honest summary",
           "What the system wants")


def test_default_copy_on_the_main_pages_has_no_jargon():
    c = _fresh()
    ids = _seed(status="preparing", title="Prep role")
    with c:
        for path in ["/", "/opportunities", "/pursuits", "/documents", "/stories", "/analytics",
                     f"/opportunities/{ids[0]}", f"/prep/{ids[0]}"]:
            text = _visible(c.get(path).text)
            for pattern in _JARGON:
                assert not re.search(pattern, text), (path, pattern)


def test_today_says_things_to_finish_and_small_samples_say_too_few_to_judge():
    c = _fresh()
    with c:
        text = _visible(c.get("/").text)
    assert "Things to finish" in text
    assert "What the system wants from you" not in text
    assert "too few to judge (0 of 5 needed)" in text


def test_the_status_rail_uses_plain_words(monkeypatch):
    from kestrel.web import routes_control

    async def _down(native_url: str):
        return {"up": False, "loaded": []}

    monkeypatch.setattr(routes_control, "_probe_ollama", _down)
    c = _fresh()
    with c:
        rail = _visible(c.get("/status/rail").text)
    assert "Not scoring anything right now" in rail and "Worker idle" not in rail
    assert "Ollama not reachable" not in rail


def test_tier_label_keeps_the_setting_name_beside_the_plain_label():
    from kestrel.web.deps import tier_label, too_few

    assert tier_label("tier1") == "Quick screen (Tier 1)"
    assert tier_label(2) == "Full review (Tier 2)"
    assert tier_label("t3") == "Deeper read (Tier 3)"
    assert too_few(0) == "too few to judge (0 of 5 needed)"
    assert too_few(3, 10) == "too few to judge (3 of 10 needed)"


# ---------------------------------------------------------------------------- track words
def test_the_role_panel_and_prep_page_speak_consulting_for_a_lead():
    c = _fresh(consulting=True)
    ids = _seed(track="consulting", status="shortlisted", title="Fractional CTO")
    with c:
        panel = c.get(f"/opportunities/{ids[0]}").text
        prep = c.get(f"/prep/{ids[0]}").text
    assert 'data-testid="interview-prep"' in panel and ">Meeting prep</a>" in panel
    assert 'data-testid="interview-prep">Interview prep' not in panel
    assert "<h1>Meeting prep</h1>" in prep
    assert "My notes for this meeting" in prep and "My notes for this interview" not in prep


def test_guides_are_track_aware():
    from kestrel.web.deps import GUIDES, guide_for

    for g in GUIDES.values():
        text = guide_for(g["path"], "consulting")["text"]
        assert "{" not in text and "resume" not in text and "shortlist" not in text, g["path"]


# ----------------------------------------------------------------- interview prep (D-UI-55)
def test_every_role_panel_and_applications_card_has_an_interview_prep_button():
    c = _fresh()
    ids = _seed(status="preparing", title="Prep role")
    with c:
        panel = c.get(f"/opportunities/{ids[0]}").text
        board = c.get("/pursuits").text
    assert f'href="/prep/{ids[0]}?auto=1"' in panel and ">Interview prep</a>" in panel
    assert f'href="/prep/{ids[0]}?auto=1"' in board
    assert 'data-testid="board-interview-prep"' in board


def test_first_open_builds_the_pack_and_notes_come_first():
    c = _fresh()
    ids = _seed(status="preparing", title="Prep role")
    with c:
        first = c.get(f"/prep/{ids[0]}?auto=1").text
        assert 'data-testid="prep-auto-build"' in first
        assert f'hx-post="/prep/{ids[0]}/build"' in first
        built = c.post(f"/prep/{ids[0]}/build").text
        page = c.get(f"/prep/{ids[0]}").text
    for text in (built, page):
        assert "My notes for this interview" in text
        assert text.index("My notes for this interview") < text.index("Questions to ask them")
    assert 'data-testid="prep-auto-build"' not in page


def test_a_pack_built_from_the_role_actions_is_announced_with_a_primary_button():
    c = _fresh()
    ids = _seed(status="preparing", title="Prep role")
    with c:
        r = c.post(f"/opportunities/{ids[0]}/prep")
    assert "Your interview prep is ready." in r.text
    assert re.search(
        r'<a class="btn primary" href="/prep/\d+" data-testid="open-interview-prep">'
        r"Open interview prep</a>", r.text)
    assert "Open prep pack page" not in r.text


# ------------------------------------------------------------------------------ My stories
def test_my_stories_lists_roles_with_notes_and_offers_add_notes():
    c = _fresh()
    ids = _seed(status="preparing", title="Prep role")
    _seed(status="shortlisted", title="Second role")
    _run(lambda s: save_notes(s, ids[0], "Tell the payments outage story."))
    with c:
        page = c.get("/stories").text
        redirect = c.get(f"/stories/notes/add?opportunity_id={ids[0]}", follow_redirects=False)
    assert 'data-testid="your-notes"' in page
    assert f'href="/prep/{ids[0]}#prep-notes"' in page and "Tell the payments outage" in page
    assert 'data-testid="add-notes"' in page and ">Add notes</button>" in page
    assert "Second role" in page.split('data-testid="add-notes"')[1]
    text = _visible(page)
    assert ("Builds drafts from your notes on 1 role and the interview questions Kestrel "
            "drafted when it scored each role.") in text
    assert redirect.status_code == 303 and redirect.headers["location"] == (
        f"/prep/{ids[0]}#prep-notes")


def test_my_stories_with_no_notes_says_so():
    c = _fresh()
    with c:
        page = c.get("/stories").text
    assert 'data-testid="no-notes"' in page
    assert "on 0 roles and the interview questions" in _visible(page)


async def test_a_dismissed_draft_is_never_proposed_again(sm):
    opp = await _seed_opp(sm)
    await _add_eval(sm, opp, [_sketch("Tell me about a turnaround.", "delta")])
    await _add_eval(sm, opp, [_sketch("A hard technical call?", "omega")])
    async with sm() as s:
        first = await consolidate_stories(s, _FakeClient(), get_settings())
    assert len(first.created_story_ids) == 2
    async with sm() as s:
        drafts = (await s.execute(select(Story).order_by(Story.id))).scalars().all()
        assert await dismiss_draft(s, drafts[0].id) is True
    async with sm() as s:
        assert (await s.execute(select(func.count()).select_from(Story))).scalar_one() == 1
        assert (await s.get(Setting, DISMISSED_KEY)).value
    # Forget the watermark so every sketch is collected again, as a full rebuild would.
    async with sm() as s:
        for key in ("consolidation.stories_watermark", "consolidation.consumed_sketch_ids"):
            row = await s.get(Setting, key)
            if row is not None:
                await s.delete(row)
        await s.commit()
    async with sm() as s:
        again = await consolidate_stories(s, _FakeClient(), get_settings())
    # Only the omega sketch is read again; the dismissed delta draft is not proposed a second time.
    assert again.sketches == 1


async def test_a_saved_story_cannot_be_dismissed_but_can_be_deleted(sm):
    async with sm() as s:
        mine = Story(title="Mine", is_canonical=True, operator_edited=True, themes=[])
        s.add(mine)
        await s.commit()
        assert await dismiss_draft(s, mine.id) is False
        assert await delete_story(s, mine.id) is True
        assert await delete_story(s, mine.id) is False
        assert (await s.execute(select(func.count()).select_from(StoryUsage))).scalar_one() == 0


def test_dismiss_and_delete_routes_and_the_themed_confirm():
    c = _fresh()

    async def _add(s):
        draft = Story(title="A draft", is_canonical=True, operator_edited=False, themes=[])
        mine = Story(title="Kept story", is_canonical=True, operator_edited=True, themes=[])
        s.add_all([draft, mine])
        await s.commit()
        return draft.id, mine.id

    draft_id, mine_id = _run(_add)
    with c:
        page = c.get("/stories").text
        assert f'hx-post="/stories/{draft_id}/dismiss"' in page and ">Dismiss</button>" in page
        assert f'data-story-delete="{mine_id}"' in page and ">Delete</button>" in page
        assert f'data-story-delete="{draft_id}"' not in page  # a draft is dismissed, not deleted
        assert 'id="story-delete"' in page and 'class="quit-confirm"' in page  # the themed dialog
        assert "Delete story</button>" in page and "confirm(" not in page
        gone = c.post(f"/stories/{draft_id}/dismiss", headers={"HX-Request": "true"}).text
        assert "Draft dismissed" in gone and "A draft" not in gone.split("Stories")[-1]
        assert "Kept story" in gone
        assert "already gone" in c.post(f"/stories/{draft_id}/dismiss").text
        # A saved story is refused by Dismiss and removed by Delete.
        assert "Kept story" in c.post(f"/stories/{mine_id}/dismiss").text
        deleted = c.post(f"/stories/{mine_id}/delete", headers={"HX-Request": "true"}).text
        assert "Story deleted." in deleted and "Kept story" not in deleted.split("Stories")[-1]


# ------------------------------------------------------------- README / Help / About
def test_readme_is_app_first_and_has_the_unsigned_beta_section():
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    first_run, dev = readme.split("## For developers", 1)
    assert "kestrel setup" not in first_run
    assert "### Unsigned beta" in first_run
    beta = first_run.split("### Unsigned beta", 1)[1].split("###", 1)[0]
    for phrase in ("More info", "Run anyway", "right-click", "**Open**", "Open** again",
                   "signing comes later"):
        assert phrase in beta, phrase
    assert "(docs/LINUX.md)" in first_run
    for promise in ("you are not the product", "any job seeker", "Windows, macOS or Linux",
                    "OpenRouter, OpenModel, Anthropic, OpenAI or any OpenAI-compatible",
                    "never applies, emails or messages anyone",
                    "PolyForm Noncommercial License 1.0.0",
                    "Claude, Qwen and NVIDIA Nemotron"):
        assert promise in first_run or promise.capitalize() in first_run, promise
    assert _DASH not in readme
    assert "kestrel setup" in dev


def test_help_and_about_state_the_public_promises_and_no_linkedin_source():
    c = _fresh()
    with c:
        helptext = _visible(c.get("/help").text)
        about = _visible(c.get("/about").text)
    assert ("Kestrel does not search LinkedIn; LinkedIn does not allow automated access. "
            "You can paste a LinkedIn posting by hand.") in helptext
    for text in (helptext, about):
        assert "any job seeker" in text and "you are not the product" in text
        assert "OpenRouter, OpenModel, Anthropic, OpenAI or any OpenAI-compatible" in text
        assert "never applies, emails or messages anyone" in text
    assert "Built with Claude, Qwen and NVIDIA Nemotron" in about
    assert "PolyForm Noncommercial" in about and "LICENSE.md" in about
    # The source table is generated from the catalog; no row is a LinkedIn source.
    table = c.get("/help").text.split("Where opportunities come from")[1].split("<h3>LinkedIn")[0]
    assert "linkedin" not in re.sub(r"LinkedIn is not offered[^<]*", "", table).lower()
