"""Sources in a real browser (UI overhaul parity check against the locked mockup,
the locked design mockup (kept privately)). Light and dark at 1280 and 1680: no overlap,
no wrapping titles or buttons, no horizontal scroll, the toolbar on one line.

Gated like the other live-browser design-system tests: set KESTREL_LIVE_BROWSER=1. The browser is
the Edge already on the machine, else a Playwright Chromium (tests/browser.py)."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import ScrapeRun, Source
from tests.browser import LINES_JS, launch_sync, wait_for_layout

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

OK = ("ok", None, {"fetched": 14, "new": 1})
RATE = ("partial", "AdapterError: HTTP 429 Too Many Requests", {"fetched": 3})
MOVED = ("failed", "AdapterError: HTTP 404 Not Found", {})
BOARDS = ("ok", None, {"fetched": 14, "new": 1, "by_site": {"indeed": 9, "glassdoor": 5}})

# (name, kind, config, origin, enabled, runs): one of each state in the mockup.
ROWS = [
    ("Northwind Health", "greenhouse", {"token": "northwind"}, "custom", True, [OK, OK]),
    ("Larkspur Logistics", "lever", {"company": "larkspur"}, "discovered", True, [OK]),
    ("Operations Analyst", "jobspy",
     {"search_term": "Operations Analyst", "site_name": ["indeed", "glassdoor"]}, "generated",
     True, [BOARDS]),
    ("Remote roles", "jobspy", {"search_term": "Remote Analyst", "site_name": ["indeed"]},
     "custom", True, [OK, RATE]),
    ("Pinecrest Labs", "ashby", {"token": "pinecrest"}, "custom", True, [MOVED]),
    ("Meadowbrook Foods", "smartrecruiters", {"company": "meadowbrook"}, "shipped", False, []),
]


def _seed() -> None:
    async def go() -> None:
        await reset_engine()
        migrate.run_migrations()
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(UTC)
        async with sm() as s:
            for name, kind, config, origin, enabled, runs in ROWS:
                src = Source(track="employment", kind=kind, name=name, config=config,
                             origin=origin, enabled=enabled, cadence_hours=24,
                             consecutive_failures=4 if runs and runs[-1] is MOVED else 0,
                             last_run_at=(now - timedelta(hours=1)).isoformat() if runs else None)
                s.add(src)
                await s.flush()
                for i, (status, error, counts) in enumerate(runs):
                    s.add(ScrapeRun(source_id=src.id, status=status, error=error, counts=counts,
                                    started_at=(now - timedelta(hours=len(runs) - i)).isoformat()))
            await s.commit()
        await engine.dispose()
        forget_engine()

    asyncio.run(go())


# One line of text: every client rect of the element's text sits at the same height.
_LINES = LINES_JS

_KIDS = """el => Array.from(el.children).filter(e => e.offsetParent !== null)
  .map(e => { const b = e.getBoundingClientRect();
    return {t: (e.textContent || '').trim().slice(0, 30), l: b.left, r: b.right, tp: b.top,
            bt: b.bottom}; })"""


def _overlaps(a, b) -> bool:
    return a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1 and a["tp"] < b["bt"] - 1 \
        and b["tp"] < a["bt"] - 1


@pytest.fixture
def live(monkeypatch, request):
    """A server on the test's own throwaway data directory (the autouse _isolate_data_home gives
    each test a fresh one) and a browser."""
    from playwright.sync_api import sync_playwright

    from kestrel.web import routes_control
    from kestrel.web.serve import running_server

    async def _down(*a, **k):
        return {"up": False, "loaded": []}

    monkeypatch.setattr(routes_control, "_probe_ollama", _down, raising=False)
    _seed()
    if getattr(request, "param", False):
        from kestrel.db.models import Setting
        from tests.test_run_mode import _app_db, _rows

        async def seed(s):
            for key, value in _rows(mode="hosted").items():
                await s.merge(Setting(key=key, value=value))
        _app_db(seed)
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
    page.goto(f"{base}/sources")
    page.wait_for_selector('[data-testid="source-row"]')
    wait_for_layout(page)
    return ctx, page, errors


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_no_overlap_no_wrapping_no_sideways_scroll(live, theme, width):
    ctx, page, errors = _page(live, theme, width)
    try:
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 0") is True
        assert page.evaluate("document.querySelector('.main, main').scrollWidth <= "
                             "document.querySelector('.main, main').clientWidth + 1") is True
        # Titles, pills, buttons, row names and the tile text stay on one line.
        for sel in ("h1", ".src-counts", ".k-pill", ".src-tools .k-btn", ".src-gtx h3",
                    ".src-rl", ".src-rc", ".src-det", ".src-t"):
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    assert page.evaluate(_LINES, el) <= 1, (sel, theme, width, el.inner_text()[:40])
        # A row name and its tag share one line (the tag sits a pixel off the name's baseline,
        # so rows are measured by height, not by text rects).
        for el in page.query_selector_all(".src-nm"):
            if el.is_visible():
                fits = "e => e.scrollHeight <= 24 && e.scrollWidth <= e.clientWidth + 1"
                assert page.evaluate(fits, el), (theme, width, el.inner_text()[:40])
        # The toolbar holds together on one line: "Add a source" follows "Check all now".
        kids = page.evaluate(_KIDS, page.locator(".src-tools").element_handle())
        assert len({round((k["tp"] + k["bt"]) / 2 / 6) for k in kids}) == 1, (kids, theme, width)
        for i, a in enumerate(kids):
            for b in kids[i + 1:]:
                assert not _overlaps(a, b), (a, b, theme, width)
        check, add = (page.locator(f'[data-testid="{t}"]').bounding_box()
                      for t in ("check-all", "add-source"))
        assert add["x"] >= check["x"] + check["width"] - 1 and abs(add["y"] - check["y"]) < 4
        # No two parts of a row overlap.
        for row in page.query_selector_all('[data-testid="source-row"]'):
            cells = page.evaluate(_KIDS, row)
            for i, a in enumerate(cells):
                for b in cells[i + 1:]:
                    assert not _overlaps(a, b), (a, b, theme, width)
        # The help column is level with the list (wide) or under it (narrow), never over it.
        main = page.locator(".k-layout__main").bounding_box()
        help_col = page.locator(".k-help-col").bounding_box()
        if width >= 1680:
            assert help_col["x"] >= main["x"] + main["width"] - 1
        else:
            assert help_col["y"] >= main["y"] + main["height"] - 1
        assert not errors, errors
    finally:
        ctx.close()


def test_the_page_follows_the_theme_tokens(live):
    ctx, page, _ = _page(live, "dark", 1280)
    try:
        bg = page.evaluate("getComputedStyle(document.querySelector('.src-grp')).backgroundColor")
        ink = page.evaluate("getComputedStyle(document.querySelector('.src-nm')).color")
        assert bg != "rgb(255, 255, 255)" and ink != "rgb(0, 0, 0)"
    finally:
        ctx.close()


def test_a_row_opens_its_drawer_and_the_add_sheet_opens_centered(live):
    ctx, page, errors = _page(live, "light", 1280)
    try:
        page.locator('[data-testid="source-details"]').first.click()
        page.wait_for_selector("#src-drawer-slot >> text=Last 10 checks")
        page.keyboard.press("Escape")
        page.get_by_test_id("add-source").click()
        sheet = page.locator("#src-sheet")
        sheet.wait_for(state="visible")
        box = sheet.bounding_box()
        assert abs((box["y"] + box["height"] / 2) - 450) < 80
        assert not errors, errors
    finally:
        ctx.close()


def test_a_sheet_replaced_while_open_stays_open_and_every_group_add_works(live):
    """Review finding: step 2 of Add a source replaced the open sheet and left only the scrim."""
    ctx, page, errors = _page(live, "light", 1280)
    try:
        page.get_by_test_id("add-source").click()
        sheet = page.locator("#src-sheet")
        sheet.wait_for(state="visible")
        sheet.get_by_text("Job board search").first.click()
        # The page opens the replaced sheet after htmx has settled it (it re-applies the response's
        # attributes then), so wait for the finished state, not the swap.
        page.wait_for_selector('#src-sheet[data-kind="boards"].is-open[aria-hidden="false"]')
        sheet.wait_for(state="visible")
        assert "is-open" in (sheet.get_attribute("class") or "")
        assert sheet.get_attribute("aria-hidden") == "false"
        page.keyboard.press("Escape")
        sheet.wait_for(state="hidden")
        # The Feeds group's own "+ Add a feed" opens the feed form (it was a dead click).
        page.locator('[data-src-add="feeds"]').click()
        page.wait_for_selector('#src-sheet[data-kind="feeds"]')
        page.locator('#src-sheet[data-kind="feeds"]').wait_for(state="visible")
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
@pytest.mark.parametrize("live", [True], indirect=True)
def test_check_all_online_confirmation_opens_and_queues(live, theme, width):
    from playwright.sync_api import expect

    ctx, page, errors = _page(live, theme, width)
    try:
        page.get_by_test_id("check-all").click()
        dialog = page.locator("#sources-check-dialog")
        expect(dialog).to_be_visible(timeout=3000)
        expect(dialog).to_contain_text("What will be sent")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        dialog.get_by_role("checkbox").check()
        with page.expect_response(lambda r: r.url.endswith("/sources/check-all")) as response:
            dialog.get_by_role("button", name="Check all now").click()
        assert response.value.status == 200
        expect(page.locator("#src-list")).to_contain_text("Checking")
        expect(dialog).not_to_be_visible()
        assert not errors
    finally:
        ctx.close()
