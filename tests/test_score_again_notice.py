"""Issue #71: the notice's Score again must actually score. On an online model it asks for the
send confirmation in place (the shared ask-first panel) and queues nothing until confirmed; a
local model queues directly. The role list is the readiness service's, read on the server."""

from __future__ import annotations

import asyncio
import html
import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from kestrel.config import get_settings
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Task
from kestrel.services import online_consent as oc
from tests.test_proof_points_import import (  # noqa: F401
    COST,
    _accept,
    _seed_scored_roles,
    _value,
    fresh,
)

# These tests need a populated profile; with no profile.yml it is empty (D-PROF-4).
pytestmark = pytest.mark.usefixtures("example_operator_profile")

HX = {"HX-Request": "true"}
FORM = {"track": "employment", "back": "today"}


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _tasks() -> int:
    async def go():
        async with get_sessionmaker()() as s:
            return (await s.execute(select(func.count()).select_from(Task))).scalar_one()
    return asyncio.run(go())


@pytest.fixture
def online(monkeypatch):
    s = get_settings()
    for tier in ("tier1", "tier2"):
        s.llm.tiers[tier].backend = "frontier"
        s.llm.tiers[tier].model = "meta-llama/llama-3.3-70b-instruct:free"
    s.llm.hosted_mode.enabled = True
    s.llm.hosted_mode.acknowledged_at = "2026-01-01T00:00:00Z"
    s.llm.hosted_mode.acknowledged_terms = s.llm.hosted_mode.disclosure_terms()
    s.confidential_mode = False

    async def load(session, *a, **k):
        return s
    # Pages bind the loader by name when they are first imported, so patch every binding (each
    # restored when the test ends) and the result never depends on which test imported the app.
    import sys

    from kestrel.services import setting_overrides

    real = setting_overrides.load_settings_with_db
    for mod in list(sys.modules.values()):
        if getattr(mod, "load_settings_with_db", None) is real:
            monkeypatch.setattr(mod, "load_settings_with_db", load)
    return s


@pytest.fixture
def stale(fresh):  # noqa: F811
    ids = _seed_scored_roles(2, scored_at="2020-01-01T00:00:00Z")
    _accept(_value(COST))
    return ids


def _approval(s) -> str:
    return json.dumps(oc.approval_for(s, ("tier1", "tier2")), separators=(",", ":"))


def test_online_press_without_consent_asks_in_place_and_queues_nothing(stale, online):
    with _client() as c:
        before = _tasks()
        r = c.post("/api/profile/score-again", data=FORM, headers=HX)
        assert r.status_code == 200
        assert 'name="confirmed"' in r.text and 'data-testid="score-again-ask"' in r.text
        assert online.llm.hosted_mode.disclosure_terms()["sends"]["tier2"][:20].lower() in \
            r.text.lower()
        assert "Choose Score again on the Pipeline page" not in r.text
        assert _tasks() == before
        # Without script it goes back to the page, which shows the same ask.
        r = c.post("/api/profile/score-again", data=FORM, follow_redirects=False)
        assert r.headers["location"] == "/?track=employment&rescore=confirm" and _tasks() == before
        page = c.get("/?track=employment&rescore=confirm").text
        assert 'data-testid="score-again-ask"' in page and "Pipeline page" not in page


def test_the_notice_ask_lists_every_online_tier_it_approves(stale, online):
    with _client() as c:
        r = c.post("/api/profile/score-again", data=FORM, headers=HX)
    terms = online.llm.hosted_mode.disclosure_terms()["sends"]
    assert set(json.loads(html.unescape(
        re.search(r"online_approval\" value='([^']+)'", r.text).group(1)))) \
        == {"tier1", "tier2"}
    for tier in ("tier1", "tier2"):
        m = re.search(rf'data-testid="score-again-sends-{tier}">(.*?)</li>', r.text, re.S)
        assert m, tier
        assert terms[tier][:25].lower() in m.group(1).lower()
        assert "meta-llama/llama-3.3-70b-instruct:free" in m.group(1)


def test_the_pipeline_rescore_dialog_lists_the_quick_screen_too(stale, online):
    with _client() as c:
        page = c.get("/rescore/choose?opp_ids=1").text
    assert 'data-testid="score-again-sends-tier1"' in page
    assert online.llm.hosted_mode.disclosure_terms()["sends"]["tier1"][:25].lower() in page.lower()


def test_quick_screen_online_with_a_local_full_review_never_says_nothing_is_sent(
        stale, online, monkeypatch):
    from kestrel.web.pages import rescore as rescore_page

    local = next(n for n, b in online.llm.backends.items() if not b.external)
    online.llm.tiers["tier2"].backend = local
    online.llm.tiers["tier2"].model = "kestrel-tier2"
    assert [s["tier"] for s in oc.step_disclosures(online, ("tier1", "tier2"))] == ["tier1"]

    async def load(session, *a, **k):
        return online
    monkeypatch.setattr(rescore_page, "load_settings_with_db", load)
    with _client() as c:
        page = c.get("/rescore/choose?opp_ids=1").text
    assert "Nothing is sent anywhere" not in page
    assert 'data-testid="score-again-online-steps"' in page
    assert 'data-testid="score-again-sends-tier1"' in page
    assert 'data-testid="score-again-sends-tier2"' not in page
    provider = oc.step_disclosures(online, ("tier1",))[0]["provider"]
    sent = page.split('data-testid="score-again-sends"')[1][:700]
    assert provider in sent and "The quick screen goes to" in sent
    tier2_line = online.llm.hosted_mode.disclosure_terms()["sends"]["tier2"][:25].lower()
    assert tier2_line not in sent.lower()


def test_unticked_or_stale_confirmation_is_refused(stale, online):
    with _client() as c:
        before = _tasks()
        r = c.post("/api/profile/score-again", headers=HX,
                   data={**FORM, "online_approval": _approval(online)})
        assert 'data-testid="score-again-ask"' in r.text and _tasks() == before
        # Pressing Score again inside the ask without the tick says what is missing.
        assert 'data-testid="score-again-problem"' in r.text and oc.NOT_CONFIRMED in r.text
        r = c.post("/api/profile/score-again", headers=HX,
                   data={**FORM, "confirmed": "1", "online_approval": '{"tier2":["other|x|y"]}'})
        assert oc.CHANGED in r.text and _tasks() == before


def test_confirmed_online_press_queues_the_readiness_selection_once(stale, online):
    data = {**FORM, "confirmed": "1", "online_approval": _approval(online)}
    with _client() as c:
        before = _tasks()
        r = c.post("/api/profile/score-again", data=data, headers=HX)
        assert "Queued 2 roles to score again." in r.text
        assert _tasks() - before == 2
        c.post("/api/profile/score-again", data=data, headers=HX)
        assert _tasks() - before == 2  # a second press queues nothing new
    async def payloads():
        async with get_sessionmaker()() as s:
            return [t.payload for t in (await s.execute(
                select(Task).where(Task.type == "t2"))).scalars()]
    pay = asyncio.run(payloads())
    assert sorted(p["opportunity_id"] for p in pay) == sorted(stale)
    assert all(p.get("online_approval") == oc.approval_for(online, ("tier1", "tier2"))
               for p in pay)


def test_confirmed_form_cannot_widen_the_selection(stale, online):
    fresh_role = _seed_scored_roles(1, scored_at="2999-01-01T00:00:00Z")[0]  # not stale
    with _client() as c:
        before = _tasks()
        c.post("/api/profile/score-again", headers=HX,
               data={**FORM, "confirmed": "1", "online_approval": _approval(online),
                     "opp_ids": str(fresh_role)})
        assert _tasks() - before == 2


def test_local_model_queues_directly_and_says_so(stale):
    with _client() as c:
        before = _tasks()
        r = c.post("/api/profile/score-again", data=FORM, headers=HX)
        assert "Queued 2 roles to score again." in r.text and 'name="confirmed"' not in r.text
        assert _tasks() - before == 2
        page = c.get("/?rescoring=2").text
        assert "Queued 2 roles to score again." in page


def test_notice_forms_swap_in_place_without_inline_script(stale):
    with _client() as c:
        html = c.get("/").text
        form = re.search(r'<form[^>]*score-again-form[^>]*>', html).group(0)
        assert 'hx-post="/api/profile/score-again"' in form
        assert "onclick" not in form


@pytest.mark.parametrize("forged", ["https://evil.example", "//evil.example", "/\\evil.example",
                                    "javascript:alert(1)"])
def test_a_forged_back_never_reaches_a_redirect_or_the_response(stale, forged):
    with _client() as c:
        for headers in (None, HX):
            r = c.post("/api/profile/score-again", headers=headers, follow_redirects=False,
                       data={**FORM, "back": forged, "tab": forged})
            assert "evil" not in r.headers.get("location", "") and "evil" not in r.text
            assert "javascript" not in r.text
            loc = r.headers.get("location")
            assert loc is None or loc.startswith("/?track=employment")


def test_import_accept_with_an_unknown_track_shows_a_fixed_sentence(fresh):  # noqa: F811
    from kestrel.docs.imports import ImportTrackRefused
    from kestrel.llm.schemas import P7ResumeImport
    from kestrel.web.pages.documents import IMPORT_TRACK_REFUSED

    value = P7ResumeImport(roles=[], summary_variants=[], skill_groups=[], proof_points=[COST])
    with _client() as c:
        r = c.post("/api/documents/import/accept", data={
            "proposal_json": value.model_dump_json(), "text": "x", "track": "nope",
            "doc_kind": "resume"})
    assert IMPORT_TRACK_REFUSED in r.text.replace("&#39;", "'")
    assert "That track is not one Kestrel knows" not in r.text
    assert ImportTrackRefused  # the exception type is what is caught


def test_today_and_profile_say_the_profile_file_could_not_be_read(fresh):  # noqa: F811
    from kestrel import paths
    from kestrel.services.profile_readiness import UNREADABLE_LINE

    (paths.config_dir() / "profile.yml").write_text("employment: [unclosed SECRETWORD",
                                                   encoding="utf-8")
    with _client() as c:
        for url in ("/", "/profile?tab=about&track=employment"):
            page = c.get(url).text
            assert 'data-next-step="profile_unreadable"' in page, url
            assert UNREADABLE_LINE in page and "SECRETWORD" not in page, url
            assert "Open Profile" in page
