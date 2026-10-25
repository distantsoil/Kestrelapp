"""The follow-up dialog in Edge, offline, in both themes and at both design widths."""

from __future__ import annotations

import os

import pytest

from kestrel.web.serve import running_server
from tests import apps_seed
from tests.browser import launch_sync
from tests.test_applications_browser import _KIDS, _LINES, _overlaps, _shot
from tests.test_follow_up import FakeClient, settings_for

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
@pytest.mark.parametrize("online", [False, True])
def test_follow_up_dialog_layout_consent_and_copy(monkeypatch, theme, width, online):
    import asyncio

    from playwright.sync_api import expect, sync_playwright

    from kestrel.services import follow_up as drafts
    from kestrel.web.deps import sessionmaker

    ids = apps_seed.seed()
    pid = ids["Senior Operations Analyst"]
    settings = settings_for(online)
    loops = []

    async def load_settings(_session):
        loops.append(asyncio.get_running_loop())
        return settings.model_copy(deep=True)

    monkeypatch.setattr(drafts, "load_settings_with_db", load_settings)
    monkeypatch.setattr("kestrel.web.pages.pursuits.load_settings_with_db", load_settings)
    with running_server(worker=False) as base, sync_playwright() as playwright:
        browser = launch_sync(playwright)
        context = browser.new_context(viewport={"width": width, "height": 1000},
                                      color_scheme=theme, permissions=["clipboard-read",
                                                                       "clipboard-write"])
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(base + "/pursuits")
            page.locator(f'[data-follow-up="{pid}"]').click()
            dialog = page.locator("#ap-follow-up")
            expect(dialog).to_have_attribute("aria-hidden", "false")
            expect(dialog.locator("form")).to_be_visible()
            if online:
                expect(dialog.locator('[data-testid="follow-up-sends"]')).to_be_visible()
                expect(dialog.locator('input[name="confirmed"]')).not_to_be_checked()
                dialog.locator('input[name="confirmed"]').check()
            for element in dialog.locator("h2, button").all():
                assert element.evaluate(_LINES) <= 1
            for group in dialog.locator(".ap-actions, form").all():
                boxes = group.evaluate(_KIDS)
                for index, first in enumerate(boxes):
                    for second in boxes[index + 1:]:
                        assert not _overlaps(first, second), (first, second)
            assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            _shot(page, f"follow-up-{online}-{theme}-{width}")
            dialog.get_by_role("button", name="Write the draft").click()
            expect(dialog.locator('[data-testid="follow-up-pending"]')).to_be_visible()

            async def run_worker():
                await drafts.drain(sessionmaker(), lambda cfg: FakeClient(cfg))

            asyncio.run_coroutine_threadsafe(run_worker(), loops[0]).result(timeout=20)
            expect(dialog.locator("#ap-draft-text")).to_be_visible(timeout=15000)
            dialog.locator("#ap-draft-text").fill("My edited follow-up.")
            dialog.get_by_role("button", name="Copy draft", exact=True).click()
            expect(dialog.locator("#ap-copy-status")).to_contain_text("Copied.")
            assert "My edited follow-up." in page.evaluate("navigator.clipboard.readText()")
            assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
            dialog.get_by_role("button", name="Close", exact=True).click()
            page.reload()
            page.locator(f'[data-follow-up="{pid}"]').click()
            expect(dialog.locator("#ap-draft-text")).to_have_value(
                "Could you share an update on next steps?",
            )
            assert not errors, errors
        finally:
            context.close()
            browser.close()
