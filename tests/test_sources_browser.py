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
from tests.browser import (
    LINES_JS,
    TIMER_PROBE_JS,
    assert_page_remounts_clean,
    launch_sync,
    wait_for_layout,
)

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
    if "legacy_backlog" in request.node.name:
        from tests.test_sources_lane import _seed as seed_backlog

        asyncio.run(seed_backlog(3))
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


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 1280), ("light", 900)])
def test_legacy_backlog_copy_and_confirmation_fit(live, theme, width, tmp_path):
    """The changed copy stays inside the existing layout in light, dark and narrow windows."""
    ctx, page, errors = _page(live, theme, width)
    try:
        group = page.locator('[data-g="company"]')
        if not group.count():
            group = page.get_by_test_id("lane").locator("xpath=ancestor::section")
        if group.get_attribute("class") and "is-open" not in group.get_attribute("class"):
            group.locator(".src-gtog").click()
        page.get_by_test_id("lane-score-now").click()
        wait_for_layout(page)
        confirmation = page.get_by_test_id("lane-confirm")
        assert confirmation.is_visible()
        assert "score matches locally" in confirmation.inner_text()
        assert "cost real money" not in confirmation.inner_text()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        for selector in ("[data-testid=lane] p", "[data-testid=lane] button"):
            for element in page.locator(selector).all():
                if element.is_visible():
                    assert element.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
        page.screenshot(path=str(tmp_path / f"sources-{theme}-{width}.png"), full_page=True)
        assert not errors
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
        wait_for_layout(page)  # the sheet slides in: measure where it settles, not mid-motion
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
@pytest.mark.parametrize("width", [800, 1280])
def test_adzuna_chip_opens_its_key_panel_and_the_remote_group_fits(live, theme, width):
    """B2/B3: ticking Adzuna shows the two key fields and the sentence on what is sent; the Remote
    jobs group sits beside the other boards; nothing overlaps or clips, and unticking hides the
    panel again."""
    ctx, page, errors = _page(live, theme, width)
    try:
        page.get_by_test_id("add-source").click()
        sheet = page.locator("#src-sheet")
        sheet.wait_for(state="visible")
        sheet.get_by_text("Job board search").first.click()
        page.wait_for_selector('#src-sheet[data-kind="boards"].is-open[aria-hidden="false"]')
        panel = page.get_by_test_id("adzuna-panel")
        assert panel.is_hidden()
        assert page.get_by_test_id("board-himalayas").is_visible()
        assert page.get_by_test_id("board-weworkremotely").is_visible()
        chip = page.get_by_test_id("board-adzuna")
        chip.click()
        panel.wait_for(state="visible")
        assert "your search words and place" in page.get_by_test_id("adzuna-sent").inner_text()
        assert page.get_by_test_id("adzuna-signup").get_attribute("href").startswith(
            "https://developer.adzuna.com/")
        boxes = page.evaluate(
            """() => [...document.querySelectorAll(
              '#src-adzuna input, #src-adzuna small, #src-adzuna a, .src-bd')].map(e => {
              const r = e.getBoundingClientRect();
              return {l: r.left, r: r.right, tp: r.top, bt: r.bottom, sw: e.scrollWidth,
                      cw: e.clientWidth, tag: e.tagName, cls: e.className};
            })"""
        )
        sheet_box = sheet.bounding_box()
        for b in boxes:
            assert b["l"] >= sheet_box["x"] - 1, b
            assert b["r"] <= sheet_box["x"] + sheet_box["width"] + 1, b
        chips = [b for b in boxes if "src-bd" in b["cls"] and b["tag"] == "LABEL"]
        for i, a in enumerate(chips):
            for b in chips[i + 1:]:
                assert not _overlaps(a, b), (a, b)
        inputs = [b for b in boxes if b["tag"] == "INPUT"]
        for i, a in enumerate(inputs):
            for b in inputs[i + 1:]:
                assert not _overlaps(a, b), (a, b)
        chip.click()
        panel.wait_for(state="hidden")
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [800, 1280])
def test_usajobs_chip_opens_its_key_panel_without_overlap(live, theme, width):
    """B1: ticking USAJobs on the job-board sheet shows the key and email fields and the one
    sentence on what is sent; nothing overlaps or clips, and unticking hides them again."""
    ctx, page, errors = _page(live, theme, width)
    try:
        page.get_by_test_id("add-source").click()
        sheet = page.locator("#src-sheet")
        sheet.wait_for(state="visible")
        sheet.get_by_text("Job board search").first.click()
        page.wait_for_selector('#src-sheet[data-kind="boards"].is-open[aria-hidden="false"]')
        panel = page.get_by_test_id("usajobs-panel")
        assert panel.is_hidden()
        chip = page.get_by_test_id("board-usajobs")
        chip.click()
        panel.wait_for(state="visible")
        assert "your search words and place" in page.get_by_test_id("usajobs-sent").inner_text()
        assert page.get_by_test_id("usajobs-signup").get_attribute("href").startswith(
            "https://developer.usajobs.gov/")
        boxes = page.evaluate(
            """() => [...document.querySelectorAll(
              '#src-usajobs input, #src-usajobs small, #src-usajobs a')].map(e => {
              const r = e.getBoundingClientRect();
              return {l: r.left, r: r.right, tp: r.top, bt: r.bottom, sw: e.scrollWidth,
                      cw: e.clientWidth, tag: e.tagName};
            })"""
        )
        sheet_box = sheet.bounding_box()
        for b in boxes:
            assert b["l"] >= sheet_box["x"] - 1, b
            assert b["r"] <= sheet_box["x"] + sheet_box["width"] + 1, b
        inputs = [b for b in boxes if b["tag"] == "INPUT"]
        for i, a in enumerate(inputs):
            for b in inputs[i + 1:]:
                assert not _overlaps(a, b), (a, b)
        chip.click()
        panel.wait_for(state="hidden")
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


@pytest.mark.parametrize("width", [1680, 1280, 1100])
def test_the_search_box_shows_its_whole_placeholder(live, width):
    """Audit 2026-10-29: "Suggest a board" squeezed the search box to "Search sour"."""
    ctx, page, errors = _page(live, "light", width)
    try:
        fits = page.evaluate("""() => {
            const i = document.getElementById('src-q');
            const cs = getComputedStyle(i);
            const c = document.createElement('canvas').getContext('2d');
            c.font = cs.font;
            const need = c.measureText(i.placeholder).width;
            return [i.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight), need];
        }""")
        assert fits[0] >= fits[1], fits
        assert not errors, errors
    finally:
        ctx.close()


def test_the_page_mounts_unmounts_and_mounts_again_with_one_set_of_everything(live):
    """The page lifecycle (D-UI-210): Sources unmounted and mounted again in place starts exactly
    what it started on the document load and leaves nothing running in between. After it, one
    press of a health tile filters (a doubled listener would press it twice and undo it), the
    checking poll's budget is the page's own (open while mounted, shut once left), and the
    filters the person set come back from the session."""
    base, browser = live
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="light")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(TIMER_PROBE_JS)
    try:
        page.goto(f"{base}/sources")
        page.wait_for_selector('[data-testid="source-row"]')
        wait_for_layout(page)
        assert page.evaluate("window.srcPollOk()") is True
        first = assert_page_remounts_clean(page, "sources", "sources.js", settle_ms=500)
        assert first["timers"] == 0 and first["observers"] == 0, first
        assert page.evaluate("window.srcPollOk()") is True
        tile = page.locator("[data-src-health]").first
        tile.click()
        assert tile.get_attribute("aria-pressed") == "true"
        key = tile.get_attribute("data-src-health")
        page.fill("#src-q", "north")
        page.wait_for_function("document.querySelectorAll('.src-row:not([hidden])').length === 1")
        # Mounted again, the person's filters are read back from the session, once.
        assert page.evaluate("KUI.page.mount()") is True
        assert page.input_value("#src-q") == "north"
        assert page.get_attribute(f'[data-src-health="{key}"]', "aria-pressed") == "true"
        page.fill("#src-q", "")
        page.locator(f'[data-src-health="{key}"]').click()  # off again: one press, one toggle
        assert page.get_attribute(f'[data-src-health="{key}"]', "aria-pressed") == "false"
        # Left, the checking poll stops asking and the page answers no more clicks.
        page.evaluate("KUI.page.unmount()")
        assert page.evaluate("window.srcPollOk()") is False
        page.locator(f'[data-src-health="{key}"]').click()
        assert page.get_attribute(f'[data-src-health="{key}"]', "aria-pressed") == "false"
        assert not errors, errors
    finally:
        ctx.close()
