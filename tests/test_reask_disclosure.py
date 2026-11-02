"""Issue 113a: the consent panels disclose the plain-writing re-ask.

INVARIANT: the consent dialog must disclose every send it authorizes. Nothing is sent to an online
model beyond what the confirmed dialog states. After a schema-valid reply, `plain_repair.enforce`
can send ONE more request to the same model (the original prompt, the first reply and a list of
writing problems). Every panel for an action that can do that says so. These tests only cover the
disclosure copy; they do not touch when or whether anything is sent. Panels for actions that never
re-ask (scoring, import, title and variant suggestions, assessment) must not carry the line."""

from __future__ import annotations

import asyncio

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


def test_the_real_callers_of_enforce_ask_for_the_line():
    import inspect

    from kestrel.services import follow_up
    from kestrel.web import prep_view
    from kestrel.web.pages import offers, orgs, prep

    assert "reask=True" in inspect.getsource(prep_view.build_ctx)  # story build
    assert "reask=True" in inspect.getsource(prep.questions_route)  # likely questions
    assert '"reask": True' in inspect.getsource(offers._negotiate_route)  # negotiation scripts
    assert '"reask": True' in inspect.getsource(follow_up.disclosure)
    assert '"reask": True' in inspect.getsource(orgs._ask_dialog)  # company summary
    assert '"reask": True' in inspect.getsource(prep_view.route_info)  # mock interview


def test_scoring_import_and_suggestion_routes_never_ask_for_it():
    import inspect

    from kestrel.web.pages import documents, role

    assert "reask=True" not in inspect.getsource(role)
    assert "reask=True" not in inspect.getsource(documents)


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
