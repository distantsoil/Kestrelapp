"""Settings > Online models (#224) in a real browser: every fold on the page plays its opening
motion each time it opens, not only the first time; the OS reduced-motion setting turns it off;
and a narrow window never scrolls sideways. No provider is set up and nothing is sent."""

from __future__ import annotations

import os

import pytest

from tests.browser import launch_sync
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

FOLDS = ('[data-testid="hosted-disclosure-fold"]', '[data-testid="hosted-advanced"]')
# The opening animations running on a fold's content right now.
_OPENING = """(sel) => document.getAnimations().filter(a => a.animationName === 'k-fold-in'
  && a.effect && a.effect.target && a.effect.target.parentElement
  && a.effect.target.parentElement.matches(sel)).length"""


@pytest.fixture
def live(monkeypatch):
    from playwright.sync_api import sync_playwright

    from kestrel.db import migrate
    from kestrel.web.serve import running_server

    migrate.run_migrations()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p, monkeypatch)
        yield base, browser
        browser.close()


def _toggle(page, sel: str) -> None:
    page.locator(f"{sel} > summary").click()


def _wait_closed(page, sel: str) -> None:
    page.wait_for_function("(s) => !document.querySelector(s).open", arg=sel)


def test_every_fold_plays_its_opening_each_time_it_opens(live):
    base, browser = live
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.goto(f"{base}/settings/hosted/review", wait_until="networkidle")
    for sel in FOLDS:
        page.locator(sel).scroll_into_view_if_needed()
        if page.evaluate("(s) => document.querySelector(s).open", sel):
            _toggle(page, sel)
            _wait_closed(page, sel)
        for round_ in (1, 2):  # the second open is the one that used to appear at once
            _toggle(page, sel)
            assert page.evaluate(_OPENING, sel) == 1, (sel, round_)
            page.wait_for_timeout(400)
            _toggle(page, sel)
            _wait_closed(page, sel)
    ctx.close()


def test_reduced_motion_opens_every_fold_without_motion(live):
    base, browser = live
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
    page = ctx.new_page()
    page.goto(f"{base}/settings/hosted/review", wait_until="networkidle")
    for sel in FOLDS:
        page.locator(sel).scroll_into_view_if_needed()
        if page.evaluate("(s) => document.querySelector(s).open", sel):
            _toggle(page, sel)
            _wait_closed(page, sel)
        _toggle(page, sel)
        assert page.evaluate("(s) => document.querySelector(s).open", sel), sel
        assert page.evaluate(_OPENING, sel) == 0, sel
    assert page.evaluate("document.getAnimations().filter(a => a.playState === 'running'"
                         " && a.effect && a.effect.target && a.effect.target.closest"
                         " && a.effect.target.closest('#hosted-panel')).length") == 0
    ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_a_narrow_window_never_scrolls_sideways(live, theme):
    base, browser = live
    ctx = browser.new_context(viewport={"width": 390, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    page.goto(f"{base}/settings/hosted/review", wait_until="networkidle")
    page.evaluate("document.querySelectorAll('#hosted-panel details').forEach(d => d.open = true)")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), theme
    # The stacked diagram and the short lanes list replace the wide ones.
    assert page.locator(".ho-flow__narrow").is_visible()
    assert not page.locator(".ho-flow__wide").is_visible()
    assert page.locator('[data-testid="hosted-lanes-compact"]').is_visible()
    ctx.close()
