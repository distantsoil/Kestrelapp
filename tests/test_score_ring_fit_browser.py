"""The score ring on a role's evaluation keeps its number and "out of 5" inside the inner circle
(issue #271): no corner of either text box crosses the circle's edge, at a wide and a narrow
window, in light and dark. A text box is the glyphs' own box (a Range), not the line box.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import asyncio
import os

import pytest

from tests import role_seed
from tests.browser import launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

# Each text run inside the ring's inner circle, measured as the glyphs' own box, and the
# smallest gap from any corner of that box to the circle's edge (negative: it crosses the edge).
_FIT_JS = """() => {
  const ring = document.querySelector('[data-testid=score-card] .rl-ring');
  const inner = ring && ring.firstElementChild;
  if (!inner) return null;
  const c = inner.getBoundingClientRect();
  const cx = c.left + c.width / 2, cy = c.top + c.height / 2, rad = Math.min(c.width, c.height) / 2;
  const out = [];
  for (const el of inner.querySelectorAll('b, small')) {
    const rg = document.createRange(); rg.selectNodeContents(el);
    const r = rg.getBoundingClientRect();
    let gap = Infinity;
    for (const [x, y] of [[r.left, r.top], [r.right, r.top], [r.left, r.bottom],
                          [r.right, r.bottom]]) {
      gap = Math.min(gap, rad - Math.hypot(x - cx, y - cy));
    }
    out.push({t: el.textContent.trim(), gap, w: r.width, h: r.height, rad});
  }
  return out;
}"""


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    ids = asyncio.run(role_seed.seed_async())
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, ids
        browser.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [420, 1280])
def test_score_text_sits_inside_the_ring(live, theme, width):
    base, browser, ids = live
    ctx = browser.new_context(viewport={"width": width, "height": 1000}, color_scheme=theme)
    try:
        page = ctx.new_page()
        page.goto(f"{base}/opportunities/{ids['strong']}")
        page.wait_for_selector("[data-testid=score-card] .rl-ring")
        wait_for_layout(page)
        runs = page.evaluate(_FIT_JS)
        assert runs and {r["t"] for r in runs} >= {"out of 5"}, runs
        for r in runs:
            # At least 4 px of the inner circle between every corner of the text and the ring.
            assert r["gap"] >= 4, (theme, width, r)
    finally:
        ctx.close()
