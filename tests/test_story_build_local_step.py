"""The story-build consent panels say which step stays on this computer.

INVARIANT: the consent panel must not change what is sent. Matching notes runs on the bundled
embedding model and never goes online (D-EMBED-1); only the writing step uses the online model.
These tests cover the disclosure copy only; the guard that keeps embeddings off an online
backend is tested in tests/test_llm_client.py and tests/test_inprocess_embed.py."""

from __future__ import annotations

import asyncio

import pytest

from kestrel.config import get_settings
from kestrel.services.online_consent import online_route
from kestrel.web.deps import templates

LINE = (
    "Kestrel matches your notes and saved interview sketches on this computer first, so the same "
    "experience becomes one story. Only the writing step uses the online model."
)


def _route():
    st = get_settings().model_copy(deep=True)
    st.confidential_mode = False
    st.llm.tiers["tier2"] = st.llm.tiers["tier2"].model_copy(
        update={"backend": "frontier", "model": "vendor/model-a"})
    hm = st.llm.hosted_mode
    hm.enabled = True
    hm.acknowledged_at = "2026-10-01T00:00:00Z"
    hm.acknowledged_terms = hm.disclosure_terms()
    return asyncio.run(online_route(None, st, "tier2", reask=True))


@pytest.mark.parametrize("name", ["partials/profile_stories_list.html", "partials/pp_bank.html"])
def test_both_story_build_panels_name_the_local_step(name):
    html = templates().env.get_template(name).render(
        build_route=_route(), stories=[], tiles=[], total_stories=0, target=8)
    assert 'data-testid="build-local"' in html
    assert LINE in html


@pytest.fixture
def migrated(_isolate_data_home):
    from kestrel.db import migrate
    from kestrel.db.engine import reset_engine

    asyncio.run(reset_engine())
    migrate.run_migrations()
    yield
    asyncio.run(reset_engine())


def test_the_panel_says_exactly_what_a_story_build_sends(monkeypatch, migrated):
    # D-STORY-3: the scoring line ("the description slice ... your full profile") is not what a
    # story build sends. The panel names the notes, the sketches and the profile's proof points,
    # which is everything the story prompt holds (kestrel/interview/stories.py _render_user).
    from kestrel.web import prep_view
    from tests.test_reask_disclosure import _patch_settings, _with_session

    _patch_settings(monkeypatch, "tier2")
    route = _with_session(prep_view.build_ctx)["build_route"]
    html = templates().env.get_template("partials/profile_stories_list.html").render(
        build_route=route, stories=[])
    assert ("Your interview notes, the interview sketches saved with each scored role, and the "
            "proof points in your profile (each claim with its evidence and metric).") in html
    assert "description slice" not in html and "full profile" not in html
    assert "For this request that means" not in html


def test_other_online_panels_do_not_carry_it():
    tpl = templates().env.from_string(
        '{% import "partials/online_ask.html" as oa %}{{ oa.ask(route, "questions", "do it") }}')
    html = tpl.render(route=_route())
    assert "-local" not in html
    assert "on this computer first" not in html


@pytest.mark.parametrize("name,button", [
    ("partials/profile_stories_list.html", "pf-stories-build"),
    ("partials/pp_bank.html", "build-stories"),
])
def test_a_local_model_shows_no_panel_and_no_line(name, button):
    html = templates().env.get_template(name).render(
        build_route={"online": False}, stories=[], tiles=[], total_stories=0, target=8)
    assert f'data-testid="{button}"' in html
    assert "build-ask" not in html
    assert "build-local" not in html
    assert "on this computer first" not in html
