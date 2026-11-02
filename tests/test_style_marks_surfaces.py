"""Issue #113: the small plain-writing mark on every surface that shows generated text, not only
stories. Each surface shows the mark when flags are stored and none when there are none (and none
on text the person edited). Offline: templates are rendered directly with invented text."""

from __future__ import annotations

from kestrel.web.deps import templates

MARK = 'data-testid="pp-style-mark"'


def _render(name: str, **ctx) -> str:
    return templates().env.get_template(name).render(**ctx)


STYLE = {"kind": "style", "rule": "banned_word", "field": "", "phrase": "robust"}


def _flag(field: str, phrase: str = "robust", kind: str = "style") -> dict:
    return {**STYLE, "kind": kind, "field": field, "phrase": phrase}


# ------------------------------------------------------------------ mock interview feedback
def _turn(flags):
    return {
        "question": "Tell me about a launch.", "answer": "We shipped it.", "_i": 0,
        "scores": {"structure": 4.0},
        "critique": {"worked_well": "A robust opening.", "improvements": ["Add one number."]},
        "style_flags": flags,
    }


def _chat(flags) -> str:
    return _render(
        "partials/pp_chat.html", turns=[_turn(flags)], active=None, opp=None, error=None, oob=False,
        base="/x", just_answered=False,
        review=type("Rv", (), {"id": 1, "mode": "behavioral", "question_count": 1,
                               "started_at": "2026-10-01T00:00:00Z"})(),
    )


def test_mock_feedback_shows_marks_when_flagged_and_not_otherwise():
    html = _chat([_flag("worked_well"), _flag("improvements[0]", "leverage")])
    assert html.count(MARK) == 2 and "robust" in html and "leverage" in html
    assert "Kestrel will not rewrite this" in html
    assert MARK not in _chat([])


# ------------------------------------------------------------------------- likely questions
def _questions(flags) -> str:
    qview = {
        "questions": [{"question": "Why us?", "category": "motivation", "why_asked": "Fit check.",
                       "style_flags": flags, "story_title": None}],
        "pending": False, "failed": False, "applied": True,
        "route": {"online": False},
    }
    return _render("partials/opp_questions.html", qview=qview, opp_id=1, qnote=None)


def test_interview_questions_show_marks_when_flagged_and_not_otherwise():
    html = _questions([_flag("questions[0].why_asked", "delve")])
    assert html.count(MARK) == 1 and "delve" in html
    assert "Kestrel will not rewrite this" in html
    assert MARK not in _questions([])


# ----------------------------------------------------------------------- company summary
def _company(flags) -> str:
    from kestrel.web import company_view as cv

    record = {
        "status": "ok", "generated_at": "2026-10-01T00:00:00+00:00", "sources": [],
        "sentences": [{"text": "It makes tools.", "source_indexes": [], "style_flags": flags}],
        "tags": [],
    }
    view = cv.summary_view(record, live=False, online_model=None)
    return _render(
        "partials/company_summary.html", sm=view, org=type("O", (), {"id": 1})(), notice=None,
        dialog=None, can_write=True,
    )


def test_company_summary_shows_marks_when_flagged_and_not_otherwise():
    html = _company([_flag("sentences[0].text", "seamless")])
    assert html.count(MARK) == 1 and "seamless" in html
    assert MARK not in _company([])


def test_company_summary_marks_are_redacted_like_the_text():
    from kestrel.web import company_view as cv

    record = {
        "status": "ok", "sources": [], "tags": [],
        "sentences": [{"text": "x", "source_indexes": [],
                       "style_flags": [_flag("sentences[0].text", "Acme")]}],
    }
    view = cv.summary_view(record, live=False, online_model=None,
                           redact=lambda t: t.replace("Acme", "Employer"))
    assert view["sentences"][0]["style_flags"][0]["phrase"] == "Employer"


# ------------------------------------------------------------------------ analytics summary
def _analytics(flags) -> str:
    narrative = {
        "summary_md": "A robust quarter.", "one_thing": "Apply earlier.", "evidence": "n=12",
        "warnings": [], "style_flags": flags,
    }
    return _render(
        "partials/analytics_summary.html", report=type("R", (), {"range_label": "30 days"})(),
        track="employment", range="30d", updated="", model_note=None, summary=None,
        plain=type("P", (), {"working": "a", "not_working": "b", "worth": "c",
                             "quiet_link": False})(),
        narrative=narrative, print_mode=True, can_write=False, hosted_send=False, svo=None,
    )


def test_analytics_summary_shows_marks_when_flagged_and_not_otherwise():
    html = _analytics([_flag("summary_md"), _flag("one_thing_to_change", "utilize")])
    assert html.count(MARK) == 2 and "utilize" in html
    assert MARK not in _analytics([])


async def test_analytics_saves_only_the_flags_for_the_text_it_shows(sm):
    from kestrel.analytics.summary import AnalyticsSummary
    from kestrel.web.pages.analytics import _save_narrative

    summary = AnalyticsSummary(
        track="employment", metrics={}, discriminating=True, insufficient_data=False,
        summary_md="A robust quarter.", headline_finding="h", one_thing_to_change="o",
        evidence_for_it="e", sample_size_warnings=[], from_model=True,
        style_flags=[_flag("summary_md"), _flag("headline_finding", "other")],
    )
    async with sm() as s:
        saved = await _save_narrative(s, "employment", "30d", summary)
    assert [f["field"] for f in saved["style_flags"]] == ["summary_md"]


# --------------------------------------------------------------------- negotiation scripts
def _scripts(flags, script_flags) -> str:
    result = type("Res", (), {
        "situation_read": "A read.", "walk_away": 100, "leverage_inventory": [],
        "risks": ["Risk one."], "style_flags": flags,
    })()
    script = {"supported": True, "script": "Hello there.", "justification_basis": "b",
              "likely_pushback": "No.", "response_to_pushback": "Yes."}
    return _render(
        "partials/offer_scripts.html", error=None, result=result, script=script,
        paras=[[("Hello there.", "")]], situation_name="Raise", script_flags=script_flags,
    )


def test_negotiation_draft_shows_marks_when_flagged_and_not_otherwise():
    flags = [_flag("situation_read", "leverage"), _flag("risks[0]", "robust")]
    html = _scripts(flags, [_flag("script", "Hello")])
    assert html.count(MARK) == 3 and "Kestrel will not rewrite this" in html
    assert MARK not in _scripts([], [])


def test_negotiation_flags_follow_the_shown_script_not_its_position():
    from kestrel.web.pages.offers import _script_flags

    flags = [_flag("scripts[2].script", "Hello"), _flag("scripts[2].script", "absent")]
    kept = _script_flags(flags, {"script": "Hello there."})
    assert [(f["field"], f["phrase"]) for f in kept] == [("script", "Hello")]


# ------------------------------------------------------------------------- follow-up draft
def _follow_up(flags) -> str:
    return _render(
        "partials/follow_up.html", pid=1, pending=False, error=None, task=None, eligible=True,
        result={"subject": "Checking in", "body": "A robust note.", "checks": ["Confirm date."]},
        style_flags=flags, facts={}, route={"online": False}, facts_stamp="x",
    )


def test_follow_up_draft_shows_marks_when_flagged_and_not_otherwise():
    html = _follow_up([_flag("body"), _flag("checks[0]", "leverage")])
    assert html.count(MARK) == 2 and "Kestrel will not rewrite this" in html
    assert "data-follow-up-marks" in html  # hidden by the page script once the person types
    assert MARK not in _follow_up([])


# ------------------------------------------------------------------------ story (unchanged)
def test_story_sheet_still_hides_marks_on_edited_text():
    from kestrel.db.models import Story

    s = Story(id=1, title="T", situation="A robust start.", action="a", result="r",
              operator_edited=True, is_canonical=True, style_flags=[_flag("situation")])
    html = _render("partials/pp_story_sheet.html", story=s, label="x", theme="", qh="", opp="",
                   **{"from": "bank"})
    assert MARK not in html


# ------------------------------------------------ the note shows only when a mark shows (#113)
NOTE = 'data-testid="pp-style-note"'


def test_note_is_absent_when_the_only_flag_is_style_dropped():
    dropped = {"kind": "style_dropped", "field": "", "phrase": "3"}
    assert NOTE not in _chat([dropped]) and MARK not in _chat([dropped])
    assert NOTE not in _analytics([dropped])


def test_note_is_absent_when_the_flag_is_on_the_subject_line():
    html = _follow_up([_flag("subject")])
    assert MARK not in html and NOTE not in html


def test_note_is_absent_when_the_flagged_question_has_no_why_asked():
    qview = {
        "questions": [{"question": "Why us?", "category": "motivation", "why_asked": "",
                       "style_flags": [_flag("questions[0].why_asked")], "story_title": None}],
        "pending": False, "failed": False, "applied": True, "route": {"online": False},
    }
    html = _render("partials/opp_questions.html", qview=qview, opp_id=1, qnote=None)
    assert MARK not in html and NOTE not in html


# --------------------------------------------------------- analytics keeps only shown flags
async def test_analytics_drops_a_flag_on_the_headline_that_was_stripped(sm):
    from kestrel.analytics.summary import AnalyticsSummary
    from kestrel.web.pages.analytics import _save_narrative

    lead = "The score is not separating outcomes yet."
    summary = AnalyticsSummary(
        track="employment", metrics={"score_vs_outcome": {}}, discriminating=False,
        insufficient_data=False, summary_md=f"{lead}\n\nA robust quarter.",
        headline_finding=lead, one_thing_to_change="o", evidence_for_it="e",
        sample_size_warnings=[], from_model=True,
        style_flags=[_flag("summary_md", "separating"), _flag("summary_md", "robust")],
    )
    async with sm() as s:
        saved = await _save_narrative(s, "employment", "90", summary)
    assert [f["phrase"] for f in saved["style_flags"]] == ["robust"]


def test_marks_leave_through_the_shared_fade_not_an_instant_hide():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "kestrel/web/static/js"
    for name in ("offers.js", "applications.js"):
        assert "KUI.fadeOut(marks)" in (root / name).read_text(encoding="utf-8"), name
    assert "function fadeOut" in (root / "kestrel-ui.js").read_text(encoding="utf-8")
