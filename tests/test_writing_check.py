"""Plain-writing check (issue #93, D-UI-232): the pure linter, the grounding check, the shared
rule block, and the one-re-ask flow. Fully offline; the client is a fake."""

from __future__ import annotations

import json

import pytest

from kestrel.llm.plain_repair import enforce
from kestrel.llm.prompts import global_rules, load_prompt, plain_writing
from kestrel.llm.schemas import SCHEMA_BY_VERSION, P8Story
from kestrel.writing_check import (
    PROSE_FIELDS,
    check,
    check_object,
    ungrounded_details,
)

# The story from the issue screenshot: every habit it names should be found.
GOLDEN = (
    "The technical work was the easy part. The harder part was the people. "
    "We drew a hard line around specialist managed security capability — that stayed. "
    "It gave a more secure outcome and lower spend at the same time, not one at the other's "
    "expense. Getting them to see their interests sitting in the same place is what let it land."
)


def _rules(text: str, **kw) -> set[str]:
    return {v.rule for v in check(text, **kw)}


def test_golden_story_hits_dash_contrast_and_closer():
    assert {"dash", "contrast", "closer"} <= _rules(GOLDEN)


@pytest.mark.parametrize("text,rule", [
    ("We moved fast — and finished.", "dash"),
    ("We moved fast – and finished.", "dash"),
    ("It was a robust plan.", "banned_word"),
    ("It was seamlessly done.", "banned_word"),
    ("I will leverage the team.", "banned_word"),
    ("They leveraged the contract.", "banned_word"),
    ("It is a pivotal move and a journey.", "banned_word"),
    ("We did this in order to save money.", "filler"),
    ("Moreover, costs fell.", "filler"),
    ("It could potentially fail.", "filler"),
    ("The unit serves as a hub.", "plain_verb"),
    ("It was not a rewrite, but a rescue.", "contrast"),
    ("It wasn't the budget, it was the people.", "contrast"),
    ("Costs fell. That is what made it land.", "closer"),
    ("It was fast, cheap, and safe.", "triplet"),
])
def test_banned_patterns_are_found(text, rule):
    assert rule in _rules(text)


@pytest.mark.parametrize("text", [
    "Costs fell 15% in a quarter.",
    "Negotiate from your leverage in the talk.",  # noun use of leverage is plain
    "The range 2019–2021 is a range.",  # en dash between digits is a range
    "I ran the review with the site leads.",
])
def test_clean_text_passes(text):
    assert check(text) == []


def test_tier2_words_flag_only_when_they_cluster():
    assert check("We drive results.") == []
    hits = check("We foster a holistic view.")
    assert {v.rule for v in hits} == {"tier2_cluster"}
    assert not any(v.blocking for v in hits)


def test_contrast_and_triplets_never_block():
    for v in check("It was fast, cheap, and safe. It was not a rewrite, but a rescue."):
        assert not v.blocking


def test_quoted_and_operator_spans_are_skipped():
    quote = 'The ad says "a robust, seamless — platform" and I agree it is large.'
    assert check(quote) == []
    own = "I led a robust rollout across the sites"
    assert check(f"In short: {own}.", exempt=[own]) == []
    assert "banned_word" in _rules(f"In short: {own}.")


def test_check_never_changes_its_input():
    text = "A robust plan — done."
    before = text
    check(text)
    assert text == before


def test_check_object_reads_only_named_fields():
    data = {"situation": "A robust plan.", "title": "A robust title", "themes": ["robust"]}
    found = check_object("story_consolidate/v2", data)
    assert {v.field for v in found} == {"situation"}
    # an unlisted prompt (labels, selection, import) is never checked
    assert check_object("triage/v2", data) == []


def test_every_registered_prose_prompt_has_a_schema():
    assert set(PROSE_FIELDS) <= set(SCHEMA_BY_VERSION)


# --------------------------------------------------------------------------- the shared block
_PROSE_PROMPTS = [
    "cover/v2", "engagement_outline/v2", "story_consolidate/v2", "mock_critique/v2",
    "negotiate/v2", "analytics_summary/v2", "interview_questions/v2", "org_summary/v2",
    "follow_up/v2",
]


@pytest.mark.parametrize("version", _PROSE_PROMPTS)
def test_rule_block_is_in_each_prose_prompt(version):
    spec = load_prompt(version)
    assert plain_writing() in spec.system
    assert "{PLAIN_WRITING}" not in spec.system
    assert global_rules() in spec.system
    assert "No em dashes" not in spec.system  # the block replaces the per-prompt line


def test_block_header_comment_is_not_sent_but_credits_the_source():
    from importlib.resources import files

    raw = files("kestrel.llm").joinpath("prompts/_plain_writing.md").read_text(encoding="utf-8")
    assert "avoid-ai-writing" in raw and "Conor Bronsdon" in raw
    assert "<!--" not in plain_writing() and "Bronsdon" not in plain_writing()


@pytest.mark.parametrize("version", ["triage/v2", "doc_select/v1", "research/v1", "contacts/v1"])
def test_json_only_prompts_omit_the_block(version):
    assert plain_writing() not in load_prompt(version).system


# --------------------------------------------------------------------------------- grounding
SOURCES = [
    "I cut vendor spend. Result: more than $1M in annualized savings and $700K in further "
    "annual savings. Led the Merge program with Dana.",
]


def test_an_invented_number_is_flagged():
    flags = ungrounded_details({"result": "Savings reached $2M a year."}, SOURCES)
    assert [(f["kind"], f["phrase"]) for f in flags] == [("ungrounded", "$2M")]


def test_a_supported_number_and_name_pass():
    text = "We saved $1M, then $700K more. I worked with Dana on the Merge program."
    assert ungrounded_details({"result": text}, SOURCES) == []


def test_number_forms_match_by_value():
    assert ungrounded_details({"result": "That is 1 million dollars."}, SOURCES) == []
    assert ungrounded_details({"result": "That is 1,000,000 saved."}, SOURCES) == []


def test_names_and_employers_are_not_checked():
    text = "At Acme, the CFO Jane Smith asked me to run the Merge program."
    assert ungrounded_details({"action": text}, ["I ran a program."]) == []


def test_number_words_and_numerals_match_by_value():
    three, digit = "We shipped three releases.", "We shipped 3 releases."
    assert ungrounded_details({"result": three}, ["Shipped 3 releases."]) == []
    assert ungrounded_details({"result": digit}, ["Shipped three releases."]) == []
    assert ungrounded_details({"result": "I own one team."}, ["A team."]) == []  # a pronoun


def test_money_and_percent_forms_are_normalized():
    src = ["Saved $500K, a 4% drop, about 1,250 hours."]
    ok = "Saved 500,000 dollars, down 4 percent, 1250 hours."
    assert ungrounded_details({"result": ok}, src) == []
    assert ungrounded_details({"result": "Saved $500,000."}, src) == []


def test_a_decimal_comma_is_not_read_as_a_thousands_group():
    flags = ungrounded_details({"result": "That took 25 days."}, ["It took 2,5 days."])
    assert [f["phrase"] for f in flags] == ["25"]


@pytest.mark.parametrize("text", [
    "The Q1\u2013Q3 plan ran Mon\u2013Fri.",
    "Pay was $120K\u2013$140K.",
    "From 2019\u20132021 I led it.",
    "Costs fell 15\u201320% in a year.",
])
def test_a_dash_in_a_range_is_not_a_violation(text):
    assert "dash" not in _rules(text)


def test_a_dash_as_punctuation_still_is():
    assert "dash" in _rules("We moved fast \u2014 and finished.")
    assert "dash" in _rules("Q1 went well \u2014 Q2 did not.")


# ----------------------------------------------------------------------------------- the flow
def _story(**over) -> dict:
    base = {
        "title": "Merge", "situation": "Vendors overlapped.", "task": "Cut spend.",
        "action": "I ran the program.", "result": "Savings were $1M.", "reflection": "Plan sooner.",
        "themes": ["cost_reduction"], "uncertain_details": [], "spoken_seconds_estimate": 90,
        "source_sketch_ids": [1], "unsupported_claims": [],
    }
    return {**base, **over}


class _Res:
    def __init__(self, value):
        self.ok, self.value = value is not None, value


class _Runner:
    """A fake guarded call: returns the next scripted reply, records the messages it was sent."""

    def __init__(self, *replies: dict | None):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, reask=False):
        self.calls.append(messages)
        reply = self.replies.pop(0)
        return _Res(P8Story.model_validate(reply) if reply is not None else None)


_SPEC = load_prompt("story_consolidate/v2")
_MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


async def test_clean_reply_is_not_reasked():
    run = _Runner()
    out = await enforce(_SPEC, P8Story.model_validate(_story()), messages=_MSGS, run=run)
    assert run.calls == [] and out.flags == [] and not out.reasked


async def test_blocking_hit_is_reasked_once_listing_all_violations():
    bad = P8Story.model_validate(
        _story(situation="A robust plan.", action="We moved — fast.")
    )
    run = _Runner(_story())  # the re-ask comes back clean
    out = await enforce(_SPEC, bad, messages=_MSGS, run=run)
    assert len(run.calls) == 1 and out.reasked and out.flags == []
    ask = run.calls[0][-1]["content"]
    assert "robust" in ask and "situation" in ask and "action" in ask  # both, in one request
    assert run.calls[0][-2]["role"] == "assistant"
    assert out.value.situation == "Vendors overlapped."


async def test_still_failing_after_the_reask_is_accepted_with_flags_not_rewritten():
    bad = _story(situation="A robust plan.")
    run = _Runner(bad)
    out = await enforce(_SPEC, P8Story.model_validate(bad), messages=_MSGS, run=run)
    assert len(run.calls) == 1  # one re-ask, never a second
    assert out.value.situation == "A robust plan."  # kept byte for byte
    assert [(f["rule"], f["phrase"]) for f in out.flags] == [("banned_word", "robust")]


async def test_a_failed_reask_keeps_the_first_reply_with_flags():
    bad = _story(situation="A robust plan.")
    out = await enforce(_SPEC, P8Story.model_validate(bad), messages=_MSGS, run=_Runner(None))
    assert out.value.situation == "A robust plan." and out.flags


async def test_flag_only_rules_are_marked_without_a_reask():
    run = _Runner()
    v = P8Story.model_validate(_story(reflection="It was not a rewrite, but a rescue."))
    out = await enforce(_SPEC, v, messages=_MSGS, run=run)
    assert run.calls == [] and [f["rule"] for f in out.flags] == ["contrast"]


async def test_ungrounded_detail_triggers_the_reask_and_is_flagged_if_it_stays():
    v = P8Story.model_validate(_story(result="Savings were $9M."))
    run = _Runner(_story(result="Savings were $9M."))
    out = await enforce(
        _SPEC, v, messages=_MSGS, run=run,
        extra_check=lambda val: ungrounded_details({"result": val.result}, SOURCES),
    )
    assert len(run.calls) == 1
    assert [(f["kind"], f["phrase"]) for f in out.flags] == [("ungrounded", "$9M")]
    assert "$9M" in run.calls[0][-1]["content"]


def test_replies_stay_json_serializable():
    assert json.dumps(P8Story.model_validate(_story()).model_dump(mode="json"))


# ------------------------------------------------- a prose call site through the real guard
class _FakeLLM:
    def __init__(self, *bodies: dict) -> None:
        from kestrel.config import get_settings

        self.settings = get_settings()
        self.bodies = list(bodies)
        self.calls = 0

    async def complete(self, *, tier, messages, schema=None, schema_name="response", **_):
        from kestrel.llm.client import ChatResult

        body = self.bodies[min(self.calls, len(self.bodies) - 1)]
        self.calls += 1
        return ChatResult(
            content=json.dumps(body), prompt_tokens=1, completion_tokens=1, latency_ms=1,
            model=self.settings.llm.tiers[tier].model,
        )


def _cover(body: str) -> dict:
    return {"body_md": body, "word_count": 20, "claims_used": [], "opening_alternatives": []}


async def test_cover_reasks_once_then_accepts_with_marks():
    from kestrel.docs.letters import generate_cover

    llm = _FakeLLM(_cover("A robust record — and results."), _cover("A robust record."))
    draft = await generate_cover(llm, user_prompt="x", valid_keys=set())
    assert llm.calls == 2  # the first reply and exactly one re-ask
    assert draft.value.body_md == "A robust record."  # kept as the model wrote it
    assert [f["phrase"] for f in draft.style_flags] == ["robust"]


async def test_cover_clean_reply_makes_one_call():
    from kestrel.docs.letters import generate_cover

    llm = _FakeLLM(_cover("I cut spend by $1M."))
    draft = await generate_cover(llm, user_prompt="x", valid_keys=set())
    assert llm.calls == 1 and draft.style_flags == []


async def test_cover_exempts_the_persons_own_bullet_text():
    from kestrel.docs.letters import generate_cover

    own = "Built a robust vendor program"
    llm = _FakeLLM(_cover(f"{own}. It cut spend."))
    draft = await generate_cover(llm, user_prompt="x", valid_keys=set(), exempt=(own,))
    assert llm.calls == 1 and draft.style_flags == []


# --------------------------------------------------------------- caps and the context guard
def test_style_flags_are_capped_with_the_count_but_grounding_flags_never():
    from kestrel.llm.plain_repair import MAX_STYLE_FLAGS, _flags
    from kestrel.writing_check import Violation

    many = [Violation("banned_word", f"w{i}", i, i + 1, "situation") for i in range(20)]
    grounding = [{"kind": "ungrounded", "field": "result", "phrase": f"{i}"} for i in range(15)]
    out = _flags(many, grounding)
    assert len([f for f in out if f["kind"] == "style"]) == MAX_STYLE_FLAGS
    dropped = [f for f in out if f["kind"] == "style_dropped"]
    assert len(dropped) == 1 and dropped[0]["count"] == str(20 - MAX_STYLE_FLAGS)
    assert len([f for f in out if f["kind"] == "ungrounded"]) == 15  # none truncated


async def test_a_reask_that_would_not_fit_the_context_is_skipped():
    big = _story(situation="A robust plan.", reflection="word " * 40000)
    run = _Runner()
    out = await enforce(_SPEC, P8Story.model_validate(big), messages=_MSGS, run=run)
    assert run.calls == [] and any(f["phrase"] == "robust" for f in out.flags)


@pytest.mark.parametrize("version", [v for v in _PROSE_PROMPTS])
def test_the_reask_prompt_fits_each_tiers_num_ctx(version):
    """The re-ask carries the prompt's fixed text, a reply as large as the prompt allows, and the
    request listing the most violations it will name; with the output budget it must fit the
    tier's num_ctx (the variable inputs are budgeted in test_prompt_budgets)."""
    from kestrel.ingest.tokens import count_tokens
    from kestrel.llm.plain_repair import MAX_STYLE_FLAGS, TIER_NUM_CTX, repair_request
    from kestrel.writing_check import Violation

    spec = load_prompt(version)
    request = repair_request(
        [Violation("banned_word", "robust", 0, 6, "scripts[3].response_to_pushback")]
        * (MAX_STYLE_FLAGS * 2), [],
    )
    used = (
        count_tokens(spec.system) + count_tokens(spec.user)
        + int(spec.max_tokens * 0.8) + count_tokens(request)
    )
    assert used + spec.max_tokens <= TIER_NUM_CTX[spec.tier], (version, used, spec.max_tokens)


# ------------------------------------------------- scoring is untouched (D-UI-236 amendment)
def test_the_scoring_prompts_keep_their_version_and_get_no_plain_writing_block():
    from types import SimpleNamespace

    from kestrel.bench import harness
    from kestrel.pipeline import tier2, tier3
    from kestrel.web import pipeline_view

    assert tier2._VERSION == tier3._VERSION == {
        "employment": "evaluate/v2", "consulting": "evaluate_consulting/v2",
    }
    assert harness.DEFAULT_PROMPT_VERSION["employment"] == "evaluate/v2"
    assert "evaluate/v3" not in SCHEMA_BY_VERSION
    for version in ("evaluate/v2", "evaluate_consulting/v2"):
        assert plain_writing() not in load_prompt(version).system
    # An evaluation scored under the current version does not show as stale.
    ev = SimpleNamespace(tier=2, prompt_version="evaluate/v2", legitimacy=None, needs_review=False)
    opp = SimpleNamespace(track="employment", ghost_score=0.0, liveness=None, seen_count=1)
    assert "older scoring instructions" not in (pipeline_view.warning_text(opp, ev) or "")


# ------------------------------------------- lists merge only when they line up (review fix)
def _script(scenario: str, text: str, basis: str = "comp") -> dict:
    return {
        "scenario": scenario, "script": text, "justification_basis": basis,
        "likely_pushback": "Budget.", "response_to_pushback": "Understood.",
    }


def _nego(*scripts: dict) -> dict:
    return {
        "situation_read": "Offer is fair.", "scripts": list(scripts),
        "leverage_inventory": [], "risks": [],
    }


class _AnyRunner:
    def __init__(self, model, reply: dict) -> None:
        self.model, self.reply, self.calls = model, reply, 0

    async def __call__(self, messages, reask=False):
        self.calls += 1
        return _Res(self.model.model_validate(self.reply))


async def _nego_enforce(first: dict, reply: dict):
    from kestrel.llm.schemas import P11Negotiate

    run = _AnyRunner(P11Negotiate, reply)
    spec = load_prompt("negotiate/v2")
    out = await enforce(spec, P11Negotiate.model_validate(first), messages=_MSGS, run=run)
    assert run.calls == 1
    return out


async def test_a_reordered_list_is_not_merged():
    first = _nego(_script("base", "A robust ask."), _script("equity", "Plain ask.", "market"))
    fixed = _nego(_script("equity", "Plain ask.", "market"), _script("base", "A clear ask."))
    out = await _nego_enforce(first, fixed)
    assert [s.scenario for s in out.value.scripts] == ["base", "equity"]
    assert out.value.scripts[0].script == "A robust ask."  # original whole, never re-paired
    assert any(f["phrase"] == "robust" for f in out.flags)


async def test_a_list_with_a_dropped_item_is_not_merged():
    first = _nego(_script("base", "A robust ask."), _script("equity", "Plain ask.", "market"))
    out = await _nego_enforce(first, _nego(_script("base", "A clear ask.")))
    assert len(out.value.scripts) == 2 and out.value.scripts[0].script == "A robust ask."


async def test_a_list_whose_keys_changed_is_not_merged():
    first = _nego(_script("base", "A robust ask."))
    out = await _nego_enforce(first, _nego(_script("base", "A clear ask.", "invented basis")))
    assert out.value.scripts[0].script == "A robust ask."


async def test_a_list_with_identical_keys_is_merged():
    first = _nego(_script("base", "A robust ask."), _script("equity", "Plain ask.", "market"))
    fixed = _nego(_script("base", "A clear ask."), _script("equity", "Other words.", "market"))
    out = await _nego_enforce(first, fixed)
    assert out.value.scripts[0].script == "A clear ask."  # the flagged field, from the re-ask
    assert out.value.scripts[1].script == "Plain ask."  # not flagged: stays as first validated
    assert out.flags == []


async def test_a_string_list_must_keep_its_length():
    from kestrel.llm.schemas import P11Negotiate

    first = _nego(_script("base", "Fine."))
    first["risks"] = ["A robust risk.", "Another."]
    reply = _nego(_script("base", "Fine."))
    reply["risks"] = ["A clear risk."]
    run = _AnyRunner(P11Negotiate, reply)
    out = await enforce(
        load_prompt("negotiate/v2"), P11Negotiate.model_validate(first), messages=_MSGS, run=run
    )
    assert list(out.value.risks) == ["A robust risk.", "Another."]


async def test_the_interview_question_keys_stay_with_their_text():
    from kestrel.llm.schemas import P14InterviewQuestions

    def q(text: str, why: str, story: int) -> dict:
        return {"question": text, "category": "behavioral", "why_asked": why, "story_id": story}

    first = {"questions": [q("Q one?", "A robust probe.", 1), q("Q two?", "Fine.", 2)]}
    swapped = {"questions": [q("Q two?", "Fine.", 2), q("Q one?", "A clear probe.", 1)]}
    run = _AnyRunner(P14InterviewQuestions, swapped)
    out = await enforce(
        load_prompt("interview_questions/v2"), P14InterviewQuestions.model_validate(first),
        messages=_MSGS, run=run,
    )
    assert [x.question for x in out.value.questions] == ["Q one?", "Q two?"]
    assert out.value.questions[0].why_asked == "A robust probe."


async def test_a_cover_body_is_taken_only_when_its_claims_match():
    from kestrel.docs.letters import generate_cover

    def cover(body: str, claims: list[str]) -> dict:
        return {**_cover(body), "claims_used": claims}

    keys = {"a", "b"}
    llm = _FakeLLM(cover("A robust record.", ["a"]), cover("A clear record.", ["a"]))
    draft = await generate_cover(llm, user_prompt="x", valid_keys=keys)
    assert draft.value.body_md == "A clear record." and draft.style_flags == []
    llm = _FakeLLM(cover("A robust record.", ["a"]), cover("A clear record.", ["a", "b"]))
    draft = await generate_cover(llm, user_prompt="x", valid_keys=keys)
    assert draft.value.body_md == "A robust record." and draft.value.claims_used == ["a"]
    assert [f["phrase"] for f in draft.style_flags] == ["robust"]


# ------------------------------------------------------------ the range allowance is narrow
@pytest.mark.parametrize("text", [
    "$5M — 20% above plan.",
    "In 2021 — 2022 was flat.",
    "We ran Marketing — Monday.",
    "Pay was $120K – $140K.",
    "Pay was $120K—$140K.",
    "Ran Marketing–Monday for a year.",
    "Open Monday–Friday.",
])
def test_a_spaced_em_or_wordlike_dash_is_a_violation(text):
    assert "dash" in _rules(text)


@pytest.mark.parametrize("text", [
    "Pay was $120K–$140K.", "The Q1–Q3 plan.", "Open Mon–Fri.", "Jan–Mar was slow.",
])
def test_an_unspaced_range_between_range_tokens_passes(text):
    assert "dash" not in _rules(text)


def test_compound_number_words_read_as_numbers():
    assert ungrounded_details({"r": "Twenty-five people."}, ["A team of 25."]) == []
    assert ungrounded_details({"r": "A team of 25."}, ["Twenty five people."]) == []
    flags = ungrounded_details({"r": "Thirty-five people."}, ["A team of 25."])
    assert [f["phrase"] for f in flags] == ["Thirty-five"]
