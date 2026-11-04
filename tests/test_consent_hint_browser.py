"""D-UI-240 in a real browser: an ask-first send button is never faded. Pressed unticked it sends
nothing, shows "Tick the box to send" beside the box and puts focus on the box; ticking clears
the hint and the send then goes. Three different panels: follow-up, title suggestions, story
build. Screenshots of the hint state go to KESTREL_SHOT_DIR when it is set."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tests.browser import wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401
from tests.test_applications_browser import _KIDS, _overlaps
from tests.test_profile_browser import _open, live  # noqa: F401  (live is a fixture)

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

LOOKS = [("light", 1280), ("dark", 1280), ("light", 480)]
ROUTE = {"online": True, "provider": "Test provider", "model": "test-model",
         "dest": "test-destination", "sends": "resume text", "employer": "hidden",
         "hosted_active": True, "this_send": "", "reask": False}


def _shot(page, name: str) -> None:
    out = os.environ.get("KESTREL_SHOT_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(out) / f"{name}.png"))


def _check_unticked_then_ticked(page, scope, send, box, hint, name, posts, after_tick):
    """Shared steps: unticked press sends nothing; hint, focus; tick clears; send proceeds."""
    from playwright.sync_api import expect

    before = len(posts)
    expect(send).to_be_enabled()
    assert float(send.evaluate("el => getComputedStyle(el).opacity")) == 1.0
    send.click()
    wait_for_layout(page)  # settled, not slept: the hint fades in
    assert len(posts) == before, "an unticked press sent a request"
    expect(hint).to_be_visible()
    assert hint.inner_text().strip() == "Tick the box to send"
    assert box.evaluate("el => el === document.activeElement")
    _shot(page, name)
    kids = scope.evaluate(_KIDS)
    for i, a in enumerate(kids):
        for b in kids[i + 1:]:
            assert not _overlaps(a, b), (a, b)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    box.check()
    expect(hint).to_be_hidden()
    after_tick()


@pytest.mark.parametrize(("theme", "width"), LOOKS)
def test_title_suggestions_panel(live, monkeypatch, theme, width):  # noqa: F811
    from playwright.sync_api import expect

    from kestrel.web.pages import profile

    monkeypatch.setattr(profile, "_suggest_route", AsyncMock(return_value=dict(ROUTE)))
    ctx, page, errors = _open(live, "employment", theme, width)
    posts: list[str] = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
    try:
        page.click('[data-testid="title-suggest-toggle"]')
        panel = page.locator('[data-testid="title-suggest-online"]')
        send = panel.get_by_role("button", name="Suggest", exact=True)
        box = panel.locator('[data-consent-box]')
        hint = panel.locator('[data-consent-hint]')

        def proceed():
            with page.expect_request("**/title-suggestions/refresh"):
                send.click()

        _check_unticked_then_ticked(page, panel.locator(".k-card"), send, box, hint,
                                    f"consent-hint-titles-{theme}-{width}", posts, proceed)
        expect(page.locator('[data-testid="title-suggestions-note"]')).to_be_visible()
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize(("theme", "width"), LOOKS)
def test_story_build_panel(live, monkeypatch, theme, width):  # noqa: F811
    from kestrel.services import online_consent

    monkeypatch.setattr(online_consent, "online_route", AsyncMock(return_value=dict(ROUTE)))
    ctx, page, errors = _open(live, "stories", theme, width)
    posts: list[str] = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
    try:
        panel = page.locator("#pf-stories-list form", has=page.locator("[data-consent-box]"))
        send = panel.locator('[data-testid="pf-stories-build"]')
        box = panel.locator('[data-consent-box]')
        hint = panel.locator('[data-consent-hint]')

        def proceed():
            with page.expect_request("**/profile/stories/build"):
                send.click()

        _check_unticked_then_ticked(page, panel.locator(".k-card"), send, box, hint,
                                    f"consent-hint-stories-{theme}-{width}", posts, proceed)
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize(("theme", "width"), LOOKS)
def test_follow_up_panel(monkeypatch, theme, width):

    from playwright.sync_api import expect, sync_playwright

    from kestrel.services import follow_up as drafts
    from kestrel.web.serve import running_server
    from tests import apps_seed
    from tests.browser import launch_sync
    from tests.test_follow_up import settings_for

    pid = apps_seed.seed()["Senior Operations Analyst"]
    settings = settings_for(True)

    async def load_settings(_session):
        return settings.model_copy(deep=True)

    monkeypatch.setattr(drafts, "load_settings_with_db", load_settings)
    monkeypatch.setattr("kestrel.web.pages.pursuits.load_settings_with_db", load_settings)
    with running_server(worker=False) as base, sync_playwright() as playwright:
        browser = launch_sync(playwright)
        context = browser.new_context(viewport={"width": width, "height": 1000},
                                      color_scheme=theme)
        page = context.new_page()
        posts: list[str] = []
        page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
        try:
            page.goto(base + "/pursuits")
            page.locator(f'[data-follow-up="{pid}"]').click()
            dialog = page.locator("#ap-follow-up")
            expect(dialog.locator("form")).to_be_visible()
            send = dialog.get_by_role("button", name="Write the draft")
            box = dialog.locator("[data-consent-box]")
            hint = dialog.locator("[data-consent-hint]")

            def proceed():
                send.click()
                expect(dialog.locator('[data-testid="follow-up-pending"]')).to_be_visible()
                assert any(u.endswith("/follow-up") for u in posts)

            _check_unticked_then_ticked(page, dialog.locator("form"), send, box, hint,
                                        f"consent-hint-followup-{theme}-{width}", posts, proceed)
        finally:
            context.close()
            browser.close()
