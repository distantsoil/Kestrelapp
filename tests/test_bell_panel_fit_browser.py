"""The notification panel fits the window (found with issue #255): opened from the bell, the panel
stays inside the window with at least 8 px on each side, the page does not scroll sideways, and
its title and tabs are not cut off, at phone widths (320, 380, 420) and at 1280, light and dark.
At phone width the bell is not the rightmost control (the theme button and the avatar follow it),
so a panel anchored to the bell ran past the left edge.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import os

import pytest

from tests.browser import launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

# The panel's box, and every title or tab inside it that is cut off by the panel's edges.
_FIT_JS = """() => {
  const p = document.querySelector('[data-testid=npanel]');
  const b = p.getBoundingClientRect();
  const cut = [...p.querySelectorAll('h3, .sf-tab, .k-npanel__head button')].filter(e => {
    const r = e.getBoundingClientRect();
    return r.left < b.left - 0.5 || r.right > b.right + 0.5 || e.scrollWidth > e.clientWidth + 1;
  }).map(e => e.textContent.trim().slice(0, 30));
  return {l: b.left, r: b.right, vw: innerWidth,
          wide: document.documentElement.scrollWidth > innerWidth + 1, cut};
}"""


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.db import migrate
    from kestrel.web.serve import running_server

    migrate.run_migrations()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [320, 380, 420, 1280])
def test_the_notification_panel_stays_inside_the_window(live, theme, width):
    base, browser = live
    ctx = browser.new_context(viewport={"width": width, "height": 800}, color_scheme=theme)
    try:
        page = ctx.new_page()
        page.goto(f"{base}/help")
        wait_for_layout(page)
        page.locator('[data-testid="bell"]').click()
        page.wait_for_selector('[data-testid="notifications-panel"]')
        # The panel's entrance transition has finished: it no longer carries is-enter.
        page.wait_for_function(
            "!document.querySelector('[data-testid=npanel]').classList.contains('is-enter')")
        fit = page.evaluate(_FIT_JS)
        assert fit["l"] >= 8 and fit["r"] <= fit["vw"] - 8, (theme, width, fit)
        assert not fit["wide"] and not fit["cut"], (theme, width, fit)
    finally:
        ctx.close()
