"""Issue #113 item 2: named-detail grounding on story consolidation (P8).

Invariant: a named detail present in the person's resume roles or companies is never flagged and
never re-asked; an invented company or person name absent from every source is flagged as a mark
only (a name flag never triggers the re-ask; number grounding still does). The
resume is matched locally and never added to a prompt. Every name here is invented."""

from __future__ import annotations

from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import DocMaster, DocSection, DocSlot, DocVariant, Story
from kestrel.interview.stories import consolidate_stories
from kestrel.llm.plain_repair import repair_request
from kestrel.writing_check import ungrounded_details, ungrounded_names
from tests.test_interview_stories import _add_eval, _seed_opp, _sketch
from tests.test_story_plain_writing import _reply, _Scripted

SKETCH = ["The delta platform was in trouble. I led the delta turnaround with Mira Okonkwo."]
RESUME = ["Brightwater Freight", "Operations Lead", "Partnered with Tobias Vellacourt on routing."]


def _names(flags: list[dict]) -> list[str]:
    return [f["phrase"] for f in flags if f.get("detail") == "name"]


# ------------------------------------------------------------------------------------ the check
def test_resume_company_title_and_person_are_never_flagged():
    text = ("At Brightwater Freight I was Operations Lead and worked with Tobias Vellacourt "
            "and Mira Okonkwo.")
    assert ungrounded_details({"action": text}, SKETCH, name_sources=RESUME) == []


def test_invented_company_and_person_are_flagged():
    text = "I briefed Corvane Systems and hired Dana Whitfield to run it."
    flags = ungrounded_details({"action": text}, SKETCH, name_sources=RESUME)
    assert _names(flags) == ["Corvane Systems", "Dana Whitfield"]
    assert all(f["kind"] == "ungrounded" and f["field"] == "action" for f in flags)


def test_the_same_invented_name_is_flagged_once_the_resume_does_not_ground_it_elsewhere():
    flags = ungrounded_names({"r": "We met Corvane Systems. We left Corvane Systems."}, RESUME)
    assert _names(flags) == ["Corvane Systems"]


def test_ordinary_capitals_are_never_flagged():
    calm = ("The Team met in March. On Monday We shipped it. Last September the Board agreed. "
            "In Q3 Our Senior Product Manager and the Vice President of Sales signed off.")
    assert ungrounded_names({"result": calm}, SKETCH) == []


def test_the_postings_own_company_and_title_are_grounded_by_the_posting_text():
    posting = "Halvorsen Robotics is hiring a Field Integration Specialist."
    text = "As Field Integration Specialist at Halvorsen Robotics I would start with intake."
    assert ungrounded_names({"body": text}, [posting]) == []


def test_a_confidential_show_it_as_text_is_grounded_and_never_replaced():
    # The output carries the redacted form; it is matched as written and the flag (if any)
    # quotes only output text, never the hidden employer.
    shown_as = "A Northern Carrier"
    text = "At A Northern Carrier I cut dwell time."
    resume = ["Brightwater Freight", shown_as]
    assert ungrounded_details({"result": text}, SKETCH, name_sources=resume) == []


def test_possessives_and_case_differences_still_match():
    text = "Brightwater Freight's routing desk and TOBIAS VELLACOURT agreed."
    assert ungrounded_names({"r": text}, ["brightwater freight", "tobias vellacourt"]) == []


def test_without_name_sources_names_are_not_checked():
    assert ungrounded_details({"r": "Corvane Systems hired Dana Whitfield."}, SKETCH) == []


def test_the_reask_line_for_a_name_mentions_the_resume():
    flag = {"kind": "ungrounded", "detail": "name", "field": "action", "phrase": "Dana Whitfield"}
    request = repair_request([], [flag])
    assert "Dana Whitfield" in request and "resume" in request


# ------------------------------------------------------------------------ through consolidation
async def _seed_resume(sm, *, marked: bool = False) -> None:
    async with sm() as s:
        master = DocMaster(track="employment", name="Resume", doc_kind="resume",
                           summary_variants=[], skill_groups=[])
        s.add(master)
        await s.flush()
        sec = DocSection(doc_master_id=master.id, section_kind="role",
                         org_label="Brightwater Freight", title="Operations Lead",
                         org_label_redacted="A Northern Carrier" if marked else None,
                         start_date="2019", end_date="2022", sort_order=0)
        s.add(sec)
        await s.flush()
        slot = DocSlot(doc_section_id=sec.id, key="b1", sort_order=0)
        s.add(slot)
        await s.flush()
        s.add(DocVariant(doc_slot_id=slot.id, key="v1",
                         text="Partnered with Tobias Vellacourt on routing."))
        await s.commit()


async def _build(sm, client):
    opp = await _seed_opp(sm)
    await _add_eval(sm, opp, [_sketch("Tell me about a turnaround.", "delta")])
    async with sm() as s:
        await consolidate_stories(s, client, get_settings())
    async with sm() as s:
        return list((await s.execute(select(Story))).scalars().all())


def _prompt_text(client) -> str:
    return "\n".join(str(m.get("content")) for call in client.calls for m in call)


async def test_a_resume_name_in_a_story_is_not_flagged_or_reasked(sm):
    await _seed_resume(sm)
    action = "I led the delta turnaround at Brightwater Freight with Tobias Vellacourt."
    client = _Scripted(_reply(action=action))
    (story,) = await _build(sm, client)
    assert len(client.calls) == 1  # no re-ask
    assert story.style_flags == [] and story.action == action
    # The resume is matched locally: it never enters a prompt that did not already have it.
    assert "Brightwater" not in _prompt_text(client).replace(action, "")


async def test_an_invented_company_is_a_mark_only_and_never_reasked(sm):
    await _seed_resume(sm)
    action = "I led the delta turnaround for Corvane Systems."
    client = _Scripted(_reply(action=action))
    (story,) = await _build(sm, client)
    assert len(client.calls) == 1  # a name flag never triggers the re-ask
    assert story.action == action
    assert {"kind": "ungrounded", "detail": "name", "field": "action",
            "phrase": "Corvane Systems"} in story.style_flags


async def test_an_ungrounded_number_still_reasks(sm):
    await _seed_resume(sm)
    client = _Scripted(_reply(result="delta recovered 87 percent."), _reply())
    await _build(sm, client)
    assert len(client.calls) == 2
    assert "87 percent" in client.calls[1][-1]["content"]


async def test_the_redacted_employer_text_is_not_flagged(sm):
    await _seed_resume(sm, marked=True)
    action = "I led the delta turnaround at A Northern Carrier."
    client = _Scripted(_reply(action=action))
    (story,) = await _build(sm, client)
    assert len(client.calls) == 1 and story.style_flags == []
    assert story.action == action  # never swapped back to the hidden name


# ------------------------------------------------------------------------- phrase logic (review)
GROUNDED = ["Brightwater Freight", "Partnered with Tobias Vellacourt on routing."]


def test_a_sentence_initial_word_never_pulls_a_grounded_name_into_a_flag():
    for text in (
        "Within Brightwater Freight I ran routing.",
        "Across Brightwater Freight's network we cut dwell time.",
        "Although Brightwater Freight was small, it grew.",
        "Situation Brightwater Freight faced a backlog.",
        "Done. Within Brightwater Freight I ran routing.",
    ):
        assert ungrounded_names({"r": text}, GROUNDED) == [], text


def test_a_sentence_initial_word_is_ignored_and_one_word_left_is_not_a_name():
    assert ungrounded_names({"r": "Despite Finance pushback we shipped."}, GROUNDED) == []
    assert ungrounded_names({"r": "Dana closed it."}, GROUNDED) == []


def test_runs_are_quoted_as_written_without_cutting_interior_words():
    text = "I moved teams between New York and San Francisco, then ran Black Friday."
    assert _names(ungrounded_names({"r": text}, GROUNDED)) == [
        "New York and San Francisco", "Black Friday",
    ]


def test_after_a_sentence_initial_word_the_rest_of_an_invented_run_is_quoted():
    flags = ungrounded_names({"r": "Within Corvane Systems I ran routing."}, GROUNDED)
    assert _names(flags) == ["Corvane Systems"]


def test_a_run_whose_words_are_all_in_the_sources_is_never_flagged():
    src = ["new york", "san francisco", "black friday"]
    text = "I moved teams between New York and San Francisco, then ran Black Friday."
    assert ungrounded_names({"r": text}, src) == []
