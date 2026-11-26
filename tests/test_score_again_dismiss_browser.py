"""Issue #253 in a real browser: Dismiss on the Today notice "N roles were scored before your
proof points" fades the notice out (it carries the leaving state while htmx waits, with a real
transition), removes it, and it stays gone after a reload. At a wide and a narrow window, light
and dark, the notice's buttons sit on the notice without overlapping each other.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import asyncio
import os

import pytest
import yaml

from tests.browser import launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = [
    pytest.mark.usefixtures("example_operator_profile"),
    pytest.mark.skipif(
        os.environ.get("KESTREL_LIVE_BROWSER") != "1",
        reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
    ),
]

NOTICE = "[data-testid=thin-profile-notice]"

# Records, from the moment it is installed, whether the notice took the leaving state and how long
# its transition is, and when it left the page.
_WATCH_JS = """() => {
  const el = document.querySelector('[data-testid=thin-profile-notice]');
  window.__leave = {swapping: false, duration: 0, removed: false};
  new MutationObserver(() => {
    if (el.classList.contains('htmx-swapping') && !window.__leave.swapping) {
      window.__leave.swapping = true;
      window.__leave.duration = parseFloat(getComputedStyle(el).transitionDuration) || 0;
    }
    if (!el.isConnected) window.__leave.removed = true;
  }).observe(document.body, {subtree: true, childList: true, attributes: true,
                             attributeFilter: ['class']});
}"""


def _seed() -> None:
    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker, reset_engine
    from kestrel.db.models import Evaluation, Opportunity, Setting
    from kestrel.services import profile_edit as pe
    from kestrel.services import profile_readiness as pr

    pe.profile_path().write_text(yaml.safe_dump({"employment": {
        "first_name": "Sam",
        "proof_points": [{"key": "p1", "claim": "Cut churn 20%", "evidence": "Q3",
                          "metric": None}],
        "employment_terms": {"target_titles": ["Operations lead"]},
    }}), "utf-8")

    async def go():
        await reset_engine()
        migrate.run_migrations()
        async with get_sessionmaker()() as s:
            for i in range(2):
                o = Opportunity(track="employment", opp_type="job_posting", title=f"Lead {i}",
                                stage="t2_done", seen_count=1, description_md="Run the floor.")
                s.add(o)
                await s.flush()
                s.add(Evaluation(opportunity_id=o.id, tier=2, track="employment",
                                 created_at="2020-01-01T00:00:00Z"))
            s.add(Setting(key=pr.KEY, value={"employment": "2021-06-01T00:00:00Z"}))
            await s.commit()
        await reset_engine()

    asyncio.run(go())


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    _seed()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [420, 1280])
def test_dismiss_fades_the_notice_out_and_it_stays_gone(live, theme, width):
    base, browser = live
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    try:
        page = ctx.new_page()
        page.goto(f"{base}/")
        page.wait_for_selector(NOTICE)
        wait_for_layout(page)
        boxes = page.evaluate("""() => ['score-again-go', 'score-again-dismiss'].map(id => {
          const b = document.querySelector('[data-testid=' + id + ']').getBoundingClientRect();
          return {l: b.left, r: b.right, t: b.top, b: b.bottom}; })""")
        go, dismiss = boxes
        apart = (go["r"] <= dismiss["l"] + 1 or dismiss["r"] <= go["l"] + 1
                 or go["b"] <= dismiss["t"] + 1 or dismiss["b"] <= go["t"] + 1)
        assert apart, (theme, width, boxes)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        page.evaluate(_WATCH_JS)
        page.get_by_test_id("score-again-dismiss").click()
        page.wait_for_function("window.__leave.removed === true")
        leave = page.evaluate("window.__leave")
        assert leave["swapping"] and leave["duration"] > 0, leave
        assert page.locator(NOTICE).count() == 0
        page.reload()
        page.wait_for_selector("[data-testid=td-hero]")
        assert page.locator(NOTICE).count() == 0
    finally:
        ctx.close()
