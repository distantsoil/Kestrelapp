"""Issue #182 scope B in a real browser: on a run's page the per-board share of new roles with no
description and the jump warning sit inside their table cell, wrap instead of running out of it,
and the page itself never scrolls sideways (the table may scroll inside its own wrapper), at
320, 380, 420 and 1280, light and dark.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import os

import pytest

from tests.browser import launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401
from tests.test_run_detail_bodies import _seed

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

# Each body line and warning: whether it is cut by its cell, or overlaps a sibling.
_FIT_JS = """() => {
  const out = {wide: document.documentElement.scrollWidth > innerWidth + 1, cut: [], overlap: [],
               narrow: []};
  for (const box of document.querySelectorAll('[data-testid=run-bodies]')) {
    // Never squeezed to a few letters a line: the table scrolls in its wrapper instead.
    if (box.getBoundingClientRect().width < 200) out.narrow.push(box.getBoundingClientRect().width);
    const cell = box.closest('td').getBoundingClientRect();
    const kids = [...box.children].map(e => e.getBoundingClientRect());
    for (const r of kids) {
      if (r.left < cell.left - 0.5 || r.right > cell.right + 0.5) {
        out.cut.push([r.left, r.right, cell.left, cell.right]);
      }
    }
    for (let i = 0; i < kids.length; i++) for (let j = i + 1; j < kids.length; j++) {
      const a = kids[i], b = kids[j];
      if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1
          && b.top < a.bottom - 1) out.overlap.push([i, j]);
    }
    for (const e of box.querySelectorAll('small, p')) {
      if (e.scrollWidth > e.clientWidth + 1) out.cut.push(e.textContent.slice(0, 30));
    }
  }
  return out;
}"""


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    rid = _seed({"Board search": {
        "glassdoor": {"new": 20, "empty": 14, "state": "jump"},
        "indeed": {"new": 12, "empty": 1, "state": "ok"},
        "google": {"new": 2, "empty": 2, "state": "jump"},
    }})
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, rid
        browser.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [320, 380, 420, 1280])
def test_the_body_shares_fit_their_cell(live, theme, width):
    base, browser, rid = live
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    try:
        page = ctx.new_page()
        page.goto(f"{base}/runs/{rid}")
        page.wait_for_selector("[data-testid=run-bodies]")
        wait_for_layout(page)
        fit = page.evaluate(_FIT_JS)
        assert not fit["wide"] and not fit["cut"] and not fit["overlap"], (theme, width, fit)
        assert not fit["narrow"], (theme, width, fit)
        assert page.locator("[data-testid=run-body-warning]").count() == 1
    finally:
        ctx.close()
