"""Issue #255 in a real browser: in the bell panel, Dismiss from an item's menu slides that item
out (it paints a leaving state with a real transition while htmx waits), the panel answers
without it, the bell count drops, and the Dismissed tab lists it with Restore, which slides it out
of Dismissed and back into All. At 420 and 1280, light and dark, the three tabs fit on one row
without overlapping.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest

from tests.browser import launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

PANEL = '[data-testid="notifications-panel"]'

# Watches the item with this id: whether it painted the leaving state (opacity under 1 while the
# panel is swapping), and when it left the page.
_WATCH_JS = """(id) => {
  const item = document.querySelector('[data-note-id="' + id + '"]');
  window.__leave = {faded: false, removed: false, duration: 0};
  const look = () => {
    if (!item.isConnected) { window.__leave.removed = true; return; }
    const cs = getComputedStyle(item);
    window.__leave.duration = Math.max(window.__leave.duration,
                                       parseFloat(cs.transitionDuration) || 0);
    if (item.closest('.htmx-swapping') && parseFloat(cs.opacity) < 1) window.__leave.faded = true;
    requestAnimationFrame(look);
  };
  requestAnimationFrame(look);
}"""


def _seed() -> dict[str, int]:
    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
    from kestrel.db.models import Notification

    async def go():
        await reset_engine()
        migrate.run_migrations()
        now = datetime.now(UTC)
        async with get_sessionmaker()() as s:
            src = Notification(kind="source_disabled", severity="warning",
                               title="Harbor board stopped after three failures")
            done = Notification(kind="run_finished", severity="success",
                                title="Run finished: 4 new",
                                created_at=(now - timedelta(days=2)).isoformat())
            s.add_all([src, done])
            await s.commit()
            out = {"src": src.id, "done": done.id}
        forget_engine()
        return out

    return asyncio.run(go())


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    ids = _seed()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, ids
        browser.close()


def _leave(page, note_id: int, testid: str) -> dict:
    item = page.locator(f'[data-note-id="{note_id}"]')
    item.locator("summary").click()
    page.evaluate(_WATCH_JS, note_id)
    item.get_by_test_id(testid).click()
    page.wait_for_function("window.__leave.removed === true")
    return page.evaluate("window.__leave")


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [420, 1280])
def test_dismiss_slides_the_item_to_dismissed_and_restore_brings_it_back(live, theme, width):
    base, browser, ids = live
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    try:
        page = ctx.new_page()
        page.goto(f"{base}/help")
        wait_for_layout(page)
        page.locator('[data-testid="bell"]').click()
        page.wait_for_selector(f'{PANEL} [data-note-id="{ids["src"]}"]')
        tabs = page.evaluate("""() => Array.from(document.querySelectorAll('.sf-tab')).map(e => {
          const b = e.getBoundingClientRect(); return {l: b.left, r: b.right, h: b.height}; })""")
        assert len(tabs) == 3, tabs
        for a, b in zip(tabs, tabs[1:], strict=False):
            assert a["r"] <= b["l"] + 1, tabs
        assert all(t["h"] < 44 for t in tabs), tabs
        assert page.inner_text("#bellcount") == "2"

        leave = _leave(page, ids["src"], "notif-dismiss")
        assert leave["faded"] and leave["duration"] > 0, leave
        page.wait_for_function("document.querySelector('#bellcount').textContent.trim() === '1'")
        assert page.locator(f'[data-note-id="{ids["src"]}"]').count() == 0

        page.get_by_test_id("notif-tab-dismissed").click()
        page.wait_for_selector(f'{PANEL}[data-tab="dismissed"] [data-note-id="{ids["src"]}"]')
        leave = _leave(page, ids["src"], "notif-restore")
        assert leave["faded"], leave
        page.wait_for_selector(f'{PANEL}[data-tab="dismissed"] [data-testid="notif-empty"]')
        assert "No dismissed notifications." in page.inner_text('[data-testid="notif-empty"]')

        page.get_by_test_id("notif-tab-all").click()
        page.wait_for_selector(f'{PANEL}[data-tab="all"] [data-note-id="{ids["src"]}"]')
    finally:
        ctx.close()
