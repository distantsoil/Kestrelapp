"""The cover letter's voice and closing (D-UI-241, prompt cover/v3). A letter is the person writing
to the employer: first person, "Dear Hiring Team,", and Kestrel adds "Sincerely," and the name at
render time. A reply in the third person gets one repair and then a clear failure. A number or
name found only in the posting is flagged only when its sentence claims it for the person. Every
test fakes only the model call (`LLMClient.complete`); nothing reaches the network."""

from __future__ import annotations

import asyncio
import json

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from kestrel.config import HostedMode
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Setting
from kestrel.docs.letters import (
    COVER,
    OUTLINE,
    build_letter_prompt,
    letter_html,
    reply_third_person_hits,
    strip_signoff,
    third_person_hits,
    unverified_details,
)
from kestrel.llm.client import ChatResult, LLMClient
from kestrel.llm.prompts import load_prompt
from tests import role_seed
from tests.letter_helpers import form_fields, generate

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

MODEL = "vendor/letter-model"
GEN = "/opportunities/{}/role/cover-letter/generate"
FIRST = (
    "Dear Hiring Team,\n\nYour distribution centers report on different calendars, and I "
    "have spent years making them agree. Quartz lantern ledger is the name my team gave the "
    "weekly review that made it stick.\n\nI would like a short call next week."
)
THIRD = (
    "Dear Hiring Team,\n\nThe candidate has spent years making distribution centers "
    "agree on one calendar.\n\nThe candidate would like a short call next week."
)


@pytest.fixture
def ids():
    return asyncio.run(role_seed.seed_async())


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False), raise_server_exceptions=False)


class Replies:
    """Replaces the model call with a queue of letter bodies (the last one repeats)."""

    def __init__(self, monkeypatch, *bodies: str):
        self.requests: list[list[dict]] = []
        queue = list(bodies)
        outer = self

        async def complete(self_client, **kwargs):
            outer.requests.append(kwargs.get("messages") or [])
            body = queue.pop(0) if len(queue) > 1 else queue[0]
            payload = {"body_md": body, "word_count": len(body.split()), "claims_used": [],
                       "opening_alternatives": []}
            return ChatResult(content=json.dumps(payload), model=MODEL)

        monkeypatch.setattr(LLMClient, "complete", complete)

    @property
    def calls(self) -> int:
        return len(self.requests)


# ------------------------------------------------------------------------------ the prompt
def test_the_prompt_is_cover_v3_and_says_first_person_to_the_employer():
    spec = load_prompt("cover/v3")
    assert spec.version == "cover/v3"
    system = spec.system
    assert "first person" in system and 'Dear Hiring Team,' in system
    assert "Never refer to the person in the third person" in system
    assert "Kestrel adds" in system  # the closing and the name are not the model's job
    from kestrel.llm.schemas import SCHEMA_BY_VERSION

    assert "cover/v3" in SCHEMA_BY_VERSION and "engagement_outline/v3" in SCHEMA_BY_VERSION
    assert "first person" in load_prompt("engagement_outline/v3").system


def test_the_user_message_reads_as_guidance_not_as_evaluation_labels():
    text = build_letter_prompt(
        COVER, proofs=[], variants=[("v1", "Rebuilt the forecast.")], headline="Operations lead",
        evaluation={
            "What to lead with": "Planning.",
            "Gap to acknowledge in one sentence, in your own voice": "Freight bidding",
        },
        title="Analyst", company="Acme", description="Lead process work.",
    )
    assert "CANDIDATE" not in text and "EVALUATION" not in text
    assert "Most significant gap" not in text
    assert "do not repeat these labels" in text
    assert "Gap to acknowledge in one sentence, in your own voice: Freight bidding" in text


def test_the_evaluation_lines_use_the_guidance_labels(ids):
    from kestrel.web.pages.role import _evaluation_lines

    ev = type("E", (), {"blocks": {
        "e_angle": {"lead_with": "Planning.", "jd_citations": ["forecasting"]},
        "b_fit": {"gaps": [{"requirement": "Freight bidding", "severity": "high"}]},
        "c_level": {"scope_read": "Senior"},
    }})()
    lines = _evaluation_lines(ev, COVER)
    assert list(lines) == [
        "What to lead with", "Phrases from the posting worth echoing",
        "Gap to acknowledge in one sentence, in your own voice", "How senior the role reads",
    ]


# ------------------------------------------------------------------------- the voice check
def test_third_person_is_found_by_whole_words_only():
    assert third_person_hits("The candidate led it.") == ["the candidate"]
    assert third_person_hits("The  Applicant led it. The candidate’s team.") == [
        "the applicant", "the candidate"]
    assert third_person_hits("The record shows candidate's drive.") == ["candidate's"]
    assert third_person_hits("I am a candidate for this role.") == []
    assert third_person_hits("My candidacy and the applicants pool.") == []
    assert third_person_hits("The consultant would start in week one.", OUTLINE) == [
        "the consultant"]


def test_a_third_person_reply_is_repaired_once_and_a_first_person_one_is_kept(ids, monkeypatch):
    model = Replies(monkeypatch, THIRD, FIRST)
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    assert model.calls == 2, "the first reply and exactly one repair"
    repair = model.requests[1][-1]["content"]
    assert "third person" in repair and "the candidate" in repair
    assert "Quartz lantern ledger" in r.text and "The candidate" not in r.text


def test_a_second_third_person_reply_fails_clearly_and_saves_nothing(ids, monkeypatch):
    model = Replies(monkeypatch, THIRD, THIRD)
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    assert model.calls == 2, "no second repair"
    soup = BeautifulSoup(r.text, "html.parser")
    reason = soup.select_one('[data-testid="cover-letter-reason"]').get_text()
    assert "written about you instead of as you" in reason
    assert "The candidate" not in r.text
    from sqlalchemy import select

    from kestrel.db.models import DocRender

    async def rows():
        async with get_sessionmaker()() as s:
            return (await s.execute(select(DocRender).where(
                DocRender.template == "cover_letter"))).scalars().all()

    assert asyncio.run(rows()) == []


def test_a_first_person_reply_passes_with_no_repair(ids, monkeypatch):
    model = Replies(monkeypatch, FIRST)
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    assert model.calls == 1 and "Quartz lantern ledger" in r.text


# ---------------------------------------------------------------- the closing and the name
HEADER = {"name": "Pat Lee", "email": "pat@example.com"}


def _text_end(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [ln.strip() for ln in soup.body.get_text("\n").splitlines() if ln.strip()]


def test_preview_and_pdf_html_end_with_sincerely_and_the_profile_name():
    for preview in (True, False):
        html = letter_html(HEADER, FIRST, COVER, preview=preview)
        assert _text_end(html)[-2:] == ["Sincerely,", "Pat Lee"]


def test_a_closing_the_model_wrote_anyway_is_not_printed_twice():
    html = letter_html(HEADER, FIRST + "\n\nSincerely,\nPat Lee", COVER)
    assert html.count("Sincerely,") == 1
    assert _text_end(html)[-2:] == ["Sincerely,", "Pat Lee"]


def test_an_outline_has_no_sincerely():
    assert "Sincerely" not in letter_html(HEADER, "## Situation\n\nThree centers.", OUTLINE)


def test_the_preview_keeps_the_page_margin_inside_the_page():
    preview = letter_html(HEADER, FIRST, COVER, preview=True)
    assert "padding: 1in" in preview
    assert "padding: 1in" not in letter_html(HEADER, FIRST, COVER)  # the PDF uses @page


def test_the_saved_view_preview_ends_with_the_closing(ids, monkeypatch):
    Replies(monkeypatch, FIRST)
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    frame = BeautifulSoup(r.text, "html.parser").select_one("iframe.rl-pvframe")
    assert _text_end(frame["srcdoc"])[-1] in ("Sincerely,", "Jordan A. Rivera", "Pat Lee")
    assert "Sincerely," in frame["srcdoc"]


def test_the_hosted_payload_carries_no_name_and_no_closing(ids, monkeypatch):
    async def go_online():
        async with get_sessionmaker()() as s:
            s.add_all([
                Setting(key="llm.hosted_mode.enabled", value=True),
                Setting(key="llm.hosted_mode.acknowledged_at", value="2026-10-15T00:00:00Z"),
                Setting(key="llm.hosted_mode.acknowledged_terms",
                        value=HostedMode().disclosure_terms()),
                Setting(key="llm.tiers.tier3.backend", value="frontier"),
                Setting(key="llm.tiers.tier3.model", value=MODEL),
            ])
            await s.commit()

    asyncio.run(go_online())
    model = Replies(monkeypatch, FIRST)
    with _client() as c:
        form = form_fields(c.get(f"/opportunities/{ids['strong']}/role/cover-letter").text)
        generate(c, GEN.format(ids["strong"]), form)
    assert model.calls >= 1
    sent = "\n".join(m["content"] for m in model.requests[0])
    from kestrel.pipeline.profile import load_profile

    name = str(getattr(load_profile("employment"), "name", "") or "")
    privates = {name, "Pat Lee", "Rivera", "Sincerely"} - {""}
    assert {"Pat Lee", "Rivera", "Sincerely"} <= privates
    for private in privates:
        assert private not in sent, private
    assert "ABOUT ME" in sent and "Dear Hiring Team" in sent  # the instruction, not a name


# ------------------------------------------------------------------- the unverified flags
MINE = ["Rebuilt the demand forecast and cut late shipments by 22 percent."]
POSTING = (
    "The role needs the minimum of 8-10 years of experience in medical technology and strong "
    "ties to Value Partnership programs."
)


def _phrases(body: str, posting: str = POSTING) -> set[str]:
    return {f["phrase"] for f in unverified_details(body, MINE, [], posting)}


def test_the_roles_own_requirement_quoted_back_is_not_flagged():
    body = "The posting asks for the minimum of 8-10 years of experience in Value Partnership work."
    assert _phrases(body) == set()
    assert {"8", "10"} <= {f["phrase"] for f in unverified_details(body, MINE, [], "")}


def test_a_posting_number_claimed_for_the_person_is_flagged():
    assert "8" in _phrases("I bring 8 years in medical technology.")
    assert "Value Partnership" in _phrases("My team ran the Value Partnership program.")
    assert "8" in _phrases("We grew it for 8 years.")


def test_a_detail_in_neither_the_person_nor_the_posting_is_always_flagged():
    assert "47" in _phrases("The role is wide. 47 sites matter to it.")
    assert "47" in _phrases("I managed 47 sites.")


def test_the_postings_words_are_never_a_source_for_a_claim_without_i():
    # claimed with "our": flagged; the posting does not ground it
    assert "10" in _phrases("Our 10 sites are ready.")
    # no posting given: nothing is excused
    assert "8" in {f["phrase"] for f in unverified_details(
        "The minimum is 8 years.", MINE, [], "")}


def test_whole_words_only_for_first_person():
    # "mean", "some" and "wed" hold me/we letters but are not first person
    assert "8" not in _phrases("Some mean wed rule requires 8 years.")


def test_the_hiring_team_greeting_is_not_an_unverified_name():
    body = "Dear Hiring Team,\n\nI rebuilt the demand forecast."
    assert unverified_details(body, MINE, [], POSTING) == []
    assert unverified_details(body, MINE, [], "") == []
    # a made-up person in the greeting is still caught
    assert "Dana Whitfield" in {f["phrase"] for f in unverified_details(
        "Dear Dana Whitfield,\n\nI rebuilt the demand forecast.", MINE, [], POSTING)}


# ------------------------------------------------- the flipped posting rule (hard rule 4)
def test_a_sentence_about_the_role_is_not_flagged_but_a_resume_style_claim_is():
    posting = "We need 8-10 years in medical technology. A team of 12 engineers, 5 sites."
    mine = ["Rebuilt the forecast."]

    def got(body):
        return {f["phrase"] for f in unverified_details(body, mine, [], posting)}

    assert got("Your need for 8 years in medical technology is clear.") == set()
    assert got("The role asks for the minimum of 8-10 years of experience.") == set()
    assert "8" in got("8 years in medical technology.")
    assert "12" in got("Led a team of 12 engineers.")


def test_eight_written_out_is_flagged_when_only_the_posting_has_8():
    posting = "Candidates bring 8 years in medical technology."
    got = {f["phrase"] for f in unverified_details(
        "Eight years in medical technology.", MINE, [], posting)}
    assert "Eight" in got


def test_a_number_matches_as_a_whole_token_not_inside_another():
    # 5 is only in the posting and its sentence is about the role; 15 is the person's own number
    # in a first-person sentence. "5" must not be found inside "15" and flagged there.
    body = "Your role needs 5 sites covered. I managed 15 people."
    got = {f["phrase"] for f in unverified_details(
        body, ["Managed 15 people."], [], "The team runs 5 sites.")}
    assert got == set()


# ------------------------------------------------------------------ the wider voice check
def test_this_person_the_writer_and_he_she_are_third_person_outside_quotes():
    assert third_person_hits("This person led it.") == ["this person"]
    assert third_person_hits("The writer led it. She said so.") == ["she", "the writer"]
    assert set(third_person_hits("Ask him. Her team. His desk. The desks are hers.")) == {
        "him", "her", "his", "hers"}
    assert third_person_hits("They led it; there, here, other, then.") == []
    assert third_person_hits('The posting says "the candidate will lead".') == []
    assert third_person_hits("The posting says \u201cshe leads\u201d.") == []


def test_the_consultant_inside_a_quotation_of_the_posting_is_allowed_in_an_outline():
    assert third_person_hits('You wrote "the consultant must travel".', OUTLINE) == []
    assert third_person_hits("The consultant would travel.", OUTLINE) == ["the consultant"]


def test_every_rendered_text_field_of_a_reply_is_checked():
    from types import SimpleNamespace as NS

    cover = NS(body_md="I led it.", opening_alternatives=["The candidate led it."])
    assert reply_third_person_hits(cover, COVER) == ["the candidate"]
    outline = NS(
        body_md="I would lead it.", assumptions=["She has the data."],
        exclusions=["The consultant would not travel.", "No pricing."],
        commercial_shape=NS(caveat="This person bills by the day."),
    )
    assert reply_third_person_hits(outline, OUTLINE) == [
        "she", "the consultant", "this person"]
    clean = NS(body_md="I led it.", opening_alternatives=["I like it."])
    assert reply_third_person_hits(clean, COVER) == []


# ------------------------------------------------------------------------------ the sign-off
def test_a_signoff_is_stripped_when_blank_lines_or_a_contact_line_follow_the_name():
    body = "Dear Hiring Team,\n\nI would like a call.\n\nSincerely,\n\nPat Lee\n\n"
    assert strip_signoff(body) == "Dear Hiring Team,\n\nI would like a call."
    body = "I would like a call.\n\nBest regards,\nPat Lee\npat@example.com\n555-010-0199"
    assert strip_signoff(body) == "I would like a call."
    assert strip_signoff("Hello.\n\nSincerely,") == "Hello."


def test_a_body_sentence_thank_you_followed_by_more_body_is_kept():
    body = "Thank you.\n\nI rebuilt the forecast across eleven distribution centers this year."
    assert strip_signoff(body) == body
    body = ("I would like a call.\n\nThank you.\n\nMy forecast rebuild cut late shipments "
            "by a fifth, and the weekly review made it last.")
    assert strip_signoff(body) == body
