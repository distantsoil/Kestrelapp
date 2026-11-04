"""Issue 113a: the consent panels disclose the plain-writing re-ask.

INVARIANT: the consent dialog must disclose every send it authorizes. Nothing is sent to an online
model beyond what the confirmed dialog states. After a schema-valid reply, `plain_repair.enforce`
can send ONE more request to the same model (the original prompt, the first reply and a list of
writing problems). Every panel for an action that can do that says so. These tests only cover the
disclosure copy; they do not touch when or whether anything is sent. Panels for actions that never
re-ask (scoring, import, title and variant suggestions, assessment) must not carry the line."""

from __future__ import annotations

import asyncio

import pytest

from kestrel.config import get_settings
from kestrel.services.online_consent import online_route
from kestrel.web.deps import templates

LINE = (
    "If the reply has writing problems, Kestrel asks the same model once more to fix them. "
    "That second request sends the same material plus the model's first reply."
)


def _online(tier: str):
    """Settings with `tier` on an online backend and Hosted mode acknowledged."""
    st = get_settings().model_copy(deep=True)
    st.confidential_mode = False
    st.llm.tiers[tier] = st.llm.tiers[tier].model_copy(
        update={"backend": "frontier", "model": "vendor/model-a"})
    hm = st.llm.hosted_mode
    hm.enabled = True
    hm.acknowledged_at = "2026-10-01T00:00:00Z"
    hm.acknowledged_terms = hm.disclosure_terms()
    return st


def _route(tier: str = "tier2", **kw):
    return asyncio.run(online_route(None, _online(tier), tier, **kw))


def _ask(route: dict, tid: str = "x") -> str:
    tpl = templates().env.from_string(
        '{% import "partials/online_ask.html" as oa %}{{ oa.ask(route, tid, "do it") }}'
    )
    return tpl.render(route=route, tid=tid)


def test_route_carries_the_flag_only_when_asked_for():
    assert _route(reask=True)["reask"] is True
    assert _route()["reask"] is False
    assert _route("tier3", reask=True)["reask"] is True


def test_shared_panel_shows_the_line_for_a_reasking_action():
    html = _ask(_route(reask=True), "questions")
    assert LINE in html.replace("&#39;", "'")
    assert 'data-testid="questions-reask"' in html


def test_shared_panel_omits_the_line_for_a_non_reasking_action():
    # Title suggestions, score again and the like use the same panel without the flag.
    html = _ask(_route())
    assert "asks the same model once more" not in html
    assert "-reask" not in html


def test_no_line_when_the_action_stays_on_this_computer():
    st = get_settings().model_copy(deep=True)
    route = asyncio.run(online_route(None, st, "tier2", reask=True))
    assert route == {"online": False}
    assert _ask(route).strip() == ""


@pytest.fixture
def migrated(_isolate_data_home):
    from kestrel.db import migrate
    from kestrel.db.engine import reset_engine

    asyncio.run(reset_engine())
    migrate.run_migrations()
    yield
    asyncio.run(reset_engine())


def _patch_settings(monkeypatch, tier: str):
    from kestrel.services import setting_overrides

    async def load(session):
        return _online(tier)

    monkeypatch.setattr(setting_overrides, "load_settings_with_db", load)


def _with_session(fn):
    from kestrel.db.engine import get_sessionmaker

    async def go():
        async with get_sessionmaker()() as session:
            return await fn(session)

    return asyncio.run(go())


def test_the_story_build_and_question_routes_ask_for_the_line(monkeypatch, migrated):
    from kestrel.web import prep_view
    from kestrel.web.pages import prep

    _patch_settings(monkeypatch, "tier2")
    build = _with_session(prep_view.build_ctx)["build_route"]
    questions = _with_session(prep.questions_route)
    for route in (build, questions):
        assert route["online"] is True
        assert route["reask"] is True
    html = _ask(questions, "questions")
    assert LINE in html.replace("&#39;", "'")


def test_follow_up_negotiation_company_and_mock_routes_ask_for_the_line(migrated):
    from types import SimpleNamespace

    from kestrel.services import follow_up
    from kestrel.web import prep_view
    from kestrel.web.pages import offers, orgs

    follow = _with_session(lambda s: follow_up.disclosure(s, _online("tier3")))
    negotiate = _with_session(lambda s: offers._negotiate_route(s, _online("tier3")))
    mock = _with_session(lambda s: prep_view.route_info(s, _online("tier2")))
    company = _with_session(lambda s: orgs._ask_dialog(s, SimpleNamespace(id=1), _online("tier2")))
    assert follow["online"] is True
    assert negotiate["online"] is True
    assert mock["online"] is True
    for route in (follow, negotiate, mock, company):
        assert route["reask"] is True


def test_scoring_import_and_suggestion_routes_never_ask_for_it(monkeypatch):
    # Title suggestions, scoring and import never re-ask: the same online route without the flag.
    _patch_settings(monkeypatch, "tier1")
    route = _route("tier1")
    assert route["online"] is True
    assert route["reask"] is False
    assert "asks the same model once more" not in _ask(route, "title-suggest")


def _render(name: str, **ctx) -> str:
    return templates().env.get_template(f"partials/{name}.html").render(**ctx).replace("&#39;", "'")


def test_negotiation_notice_follow_up_and_mock_dialog_show_the_line_only_when_flagged():
    base = {
        "online": True, "model": "m", "dest": "d", "provider": "Acme", "sends": "the draft",
        "draft_sends": "the offer figures", "employer": "off",
    }
    on = _render("offer_send_notice", route={**base, "reask": True}, sends_line="")
    off = _render("offer_send_notice", route=base, sends_line="")
    assert LINE in on and 'data-testid="of-reask"' in on
    assert "asks the same model once more" not in off

    fu = {"online": True, "model": "m", "provider": "Acme", "sends": "the facts", "stamp": "s",
          "problem": None, "hidden": False}
    ctx = {"pid": 1, "pending": False, "result": None, "task": None, "eligible": True,
           "facts": {}, "facts_stamp": "f", "error": None}
    assert LINE in _render("follow_up", route={**fu, "reask": True}, **ctx)
    assert "asks the same model once more" not in _render("follow_up", route=fu, **ctx)

    pp = {"online": True, "native": True, "model": "m", "dest": "d", "provider": "Acme",
          "sends": "answers", "practice_sends": "your answer", "employer": "off"}
    assert LINE in _render("pp_online", route={**pp, "reask": True}, opp_id=1, credit=None)
    assert "asks the same model once more" not in _render(
        "pp_online", route=pp, opp_id=1, credit=None)


def test_analytics_and_company_dialogs_show_the_line():
    an = _render("analytics_online", track="employment", range="90", provider="Acme", model="m",
                 dest="d", sends="Counts", employer="off", hosted=True, reask=True)
    assert LINE in an
    an_off = _render("analytics_online", track="employment", range="90", provider="Acme",
                     model="m", dest="d", sends="Counts", employer="off", hosted=True)
    assert "asks the same model once more" not in an_off
