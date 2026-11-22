"""The Needs another try group banner in a real browser (issue 206): light, dark and 390 wide,
nothing overlapping or clipped, one line per button where it fits, no sideways scroll; then the
interactions: the menu, Show these, Hide with Undo, and a group try that reaches the queue.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1. Set KESTREL_RT_SHOTS to a
folder to also save screenshots of every state."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest

from kestrel.llm import failure_kind as fk
from tests import retry_seed
from tests.browser import launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

_ROWS = [
    (f"Operations Analyst {i}", "Larkspur Logistics", 2,
     fk.tag("schema validation failed: verdict required", fk.UNREADABLE),
     '{"fit": "strong", "summary": "The role centers on weekly reporting', 2,
     timedelta(minutes=20 + i))
    for i in range(5)
] + [
    ("Supply Chain Planner", "Meadowbrook Foods", 1, "request error: ReadTimeout: timed out",
     "(no answer after 120 seconds)", 1, timedelta(minutes=23)),
    ("Process Improvement Lead", "Larkspur Logistics", 2, "generation loop detected",
     "the the the the", 1, timedelta(hours=2)),
]

_BOXES = """sel => Array.from(document.querySelectorAll(sel)).filter(e => e.offsetParent !== null)
  .map(e => { const b = e.getBoundingClientRect();
    return {t: (e.textContent || '').trim().slice(0, 30), l: b.left, r: b.right, tp: b.top,
            bt: b.bottom}; })"""


def _overlaps(a, b) -> bool:
    return a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1 and a["tp"] < b["bt"] - 1 \
        and b["tp"] < a["bt"] - 1


@pytest.fixture
def live(monkeypatch):
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    async def no_hosted(_settings):
        return ("", None, [], [], {}, None)

    monkeypatch.setattr("kestrel.web.pages.needs_try._local_models",
                        lambda settings: (["kestrel-tier3:latest"], None))
    monkeypatch.setattr("kestrel.web.pages.rescore._local_models",
                        lambda settings: (["kestrel-tier3:latest"], None))
    monkeypatch.setattr("kestrel.web.pages.needs_try._hosted_models", no_hosted)
    monkeypatch.setattr("kestrel.web.pages.rescore._hosted_models", no_hosted)
    retry_seed.seed(_ROWS)
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


def _page(live, theme: str, width: int):
    base, browser = live
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/quarantine")
    page.wait_for_selector('[data-testid="rt-group"]')
    wait_for_layout(page)
    return ctx, page, errors


def _shoot(page, name: str) -> None:
    out = os.environ.get("KESTREL_RT_SHOTS")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(out) / f"{name}.png"))


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [390, 480, 1280])
def test_the_banner_fits_in_light_dark_and_narrow(live, theme, width):
    ctx, page, errors = _page(live, theme, width)
    try:
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        banner = page.locator('[data-testid="rt-group"]')
        assert banner.count() == 1  # one banner for the five roles, not one per role
        assert "5 roles failed the same way in Fit score" in banner.inner_text()
        assert page.locator('[data-testid="rt-row"]').count() == 7  # no role is hidden
        box = banner.bounding_box()
        card = page.locator("#rt-card").bounding_box()
        assert box["x"] >= card["x"] - 1
        assert box["x"] + box["width"] <= card["x"] + card["width"] + 1
        kids = page.evaluate(_BOXES, ".rt-group .rt-g-text, .rt-group .rt-g-acts .k-btn")
        for i, a in enumerate(kids):
            for b in kids[i + 1:]:
                assert not _overlaps(a, b), (a, b, theme, width)
            # Nothing is cut at the banner's right edge.
            assert a["r"] <= box["x"] + box["width"] + 1, (a, theme, width)
        for btn in page.query_selector_all(".rt-group .k-btn"):
            clipped = btn.evaluate("b => b.scrollWidth > b.clientWidth + 1")
            assert not clipped, btn.inner_text()
        _shoot(page, f"group-{theme}-{width}")
        assert not errors
    finally:
        ctx.close()


def test_the_menu_show_these_and_hide_with_undo(live):
    ctx, page, errors = _page(live, "light", 1280)
    try:
        page.click('[data-testid="rt-group-model"]')
        page.wait_for_selector('.rt-group .rt-menu [data-testid="rt-local"]')
        opener = page.locator('[data-testid="rt-group-model"]')
        assert opener.get_attribute("aria-expanded") == "true"
        _shoot(page, "group-menu-light-1280")
        page.keyboard.press("Escape")
        assert page.locator(".rt-group .rt-menu").count() == 0
        page.click('[data-testid="rt-group-show"]')
        page.wait_for_selector('[data-testid="rt-group-all"]')
        assert page.locator('[data-testid="rt-row"]').count() == 5
        assert "Showing only these roles." in page.locator('[data-testid="rt-group"]').inner_text()
        _shoot(page, "group-picked-light-1280")
        page.click('[data-testid="rt-group-all"]')
        page.wait_for_function(
            "document.querySelectorAll('[data-testid=\"rt-row\"]').length === 7")
        assert page.locator('[data-testid="rt-group-show"]').count() == 1
        page.click('[data-testid="rt-group-hide"]')
        page.wait_for_function("!document.querySelector('[data-testid=\"rt-group\"]')")
        assert page.locator('[data-testid="rt-row"]').count() == 7
        page.get_by_role("button", name="Undo").first.click()
        page.wait_for_selector('[data-testid="rt-group"]')
        assert not errors
    finally:
        ctx.close()


def test_a_group_try_with_a_local_model_reaches_the_queue_for_the_five_roles_only(live):
    import sqlite3

    from kestrel import paths

    def tasks() -> int:
        con = sqlite3.connect(str(paths.db_path()))
        try:
            return con.execute("select count(*) from task").fetchone()[0]
        finally:
            con.close()

    ctx, page, errors = _page(live, "light", 1280)
    try:
        assert tasks() == 0  # looking at the banner and the menu queues nothing
        page.click('[data-testid="rt-group-model"]')
        page.wait_for_selector('.rt-group .rt-menu [data-testid="rt-local"]')
        wait_for_layout(page)  # D-TEST-821: the menu is loaded by htmx
        page.click('.rt-group .rt-menu [data-testid="rt-local"]')
        page.wait_for_selector('[data-testid="retry-queued"]')
        assert tasks() == 5
        _shoot(page, "group-tried-light-1280")
        assert not errors
    finally:
        ctx.close()
