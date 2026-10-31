"""Browser checks for the back-end review fixes (D-UI-160). Gated: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import os

import pytest

from tests.browser import launch_sync

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server
    from tests import profile_seed

    profile_seed.seed()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


def test_r16_the_near_floor_hint_follows_the_control(live):
    base, browser = live
    page = browser.new_page(viewport={"width": 1280, "height": 1300})
    page.goto(f"{base}/profile?tab=employment&track=employment")
    page.wait_for_selector(".pf-margin .k-seg")
    hint = page.locator('[data-testid="near-floor-hint"]').first
    assert "5% below" in hint.inner_text()
    page.locator(".pf-margin__sw").first.click()  # off is Exact (D-UI-200)
    assert "set aside" in hint.inner_text() and "5% below" not in hint.inner_text()
    page.locator(".pf-margin__sw").first.click()  # back on, then pick 10%
    page.locator(".pf-margin .k-seg button", has_text="10%").click()
    assert "10% below" in hint.inner_text() and "set aside" not in hint.inner_text()
    assert "always kept" in hint.inner_text()
