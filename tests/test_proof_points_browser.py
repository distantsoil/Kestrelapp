"""Proof points refresh, Cancel, in a real browser under the shipped CSP (issue #208 round 12).

The reviewed diff's Cancel once ran an inline onclick, which the Content-Security-Policy
(script-src 'self', #212) refuses: the click did nothing. It now deletes its own panel through
htmx after the leave animation. This test clicks it in Chromium and asserts the panel is gone and
no policy violation was logged. Invented text only; no request leaves the machine.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1.
"""

from __future__ import annotations

import os
import pathlib
import tempfile

import pytest

from tests.browser import launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("KESTREL_LIVE_BROWSER") != "1",
        reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
    ),
    pytest.mark.usefixtures("_isolate_data_home"),
]

SHOTS = pathlib.Path(os.environ.get("KESTREL_SHOT_DIR") or tempfile.gettempdir())
PANEL = '[data-testid="pf-pp-diff"]'


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.services import profile_edit as pe
    from kestrel.web.serve import running_server
    from tests import profile_seed
    from tests.test_proof_refresh import _points

    profile_seed.seed()
    pe.save_section("employment", "proof_points", {"proof_points": _points()})
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


@pytest.mark.parametrize("theme, width", [("light", 1280), ("dark", 1280), ("light", 390),
                                          ("dark", 390)])
def test_cancel_closes_the_diff_without_a_policy_violation(live, theme, width):
    base, browser = live
    ctx = browser.new_context(viewport={"width": width, "height": 1100}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    console: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: console.append(m.text))
    try:
        page.goto(base + "/profile?tab=documents&track=employment")
        wait_for_layout(page)
        page.click('[data-testid="pf-pp-refresh"]')
        page.wait_for_selector(f'{PANEL} [data-testid="pf-pp-cancel"]')
        wait_for_layout(page)
        page.locator(PANEL).screenshot(path=str(SHOTS / f"pp-diff-{theme}-{width}.png"))
        page.click(f'{PANEL} [data-testid="pf-pp-cancel"]')
        page.wait_for_selector(PANEL, state="detached", timeout=3000)
        assert page.locator('[data-testid="pf-proof-points"]').count() == 1  # the card stays
        assert not [c for c in console if "Content Security Policy" in c
                    or "Content-Security-Policy" in c], console
        assert not errors, errors
    finally:
        ctx.close()
