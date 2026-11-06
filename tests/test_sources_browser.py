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
from tests.browser_plugin import browser_artifacts  # noqa: F401

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
    if any(k in request.node.name for k in ("while_looking_up", "suggested_row", "settling_row")):
        _seed_pursuits()
    if "action_layout" in request.node.name:
        _seed_state_rows()
    if "while_looking_up" in request.node.name:
        _seed_found_boards()  # the rows already say Found on a board, so Add is on offer
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
    worker = any(k in request.node.name for k in ('suggested_rows_resolve', 'settling_row'))
    with running_server(worker=worker) as base, sync_playwright() as p:
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
        page.wait_for_selector("text=Last 10 checks")
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


def _seed_pursuits() -> None:
    from kestrel.db.models import Opportunity, Org, Pursuit
    from kestrel.ingest.orgs import normalized_name

    async def go() -> None:
        await reset_engine()
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            for name, status in (("Tidewater Freight Group", "applied"),
                                 ("Ironbridge Supply", "shortlisted")):
                o = Org(name=name, normalized_name=normalized_name(name))
                s.add(o)
                await s.flush()
                opp = Opportunity(track="employment", opp_type="job", external_id=name,
                                  title="Role", org_name=name, org_id=o.id)
                s.add(opp)
                await s.flush()
                s.add(Pursuit(opportunity_id=opp.id, track="employment", status=status))
            await s.commit()
        await engine.dispose()
        forget_engine()

    asyncio.run(go())


def _seed_state_rows() -> None:
    """Four suggested companies, one in each state: found, none, error, checking."""
    from kestrel.db.models import Opportunity, Org, Pursuit
    from kestrel.ingest.orgs import normalized_name
    from kestrel.services import source_suggestions as ss

    names = ["Tidewater Freight Group", "Ironbridge Supply",
             "Monarca Food Solutions (Evans Food Group / Palmex)",
             "Mindful Health Solutions (Radial)"]

    async def go() -> None:
        await reset_engine()
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            for n in names:
                o = Org(name=n, normalized_name=normalized_name(n))
                s.add(o)
                await s.flush()
                opp = Opportunity(track="employment", opp_type="job", external_id=n,
                                  title="Role", org_name=n, org_id=o.id)
                s.add(opp)
                await s.flush()
                s.add(Pursuit(opportunity_id=opp.id, track="employment", status="applied"))
            await ss.record_board_lookup(s, names[0], found=True, platform="Greenhouse",
                                         jobs=12, kind="greenhouse", config={"token": "t"})
            await ss.record_board_lookup(s, names[1], found=False)
            await ss.record_check_error(s, names[2])
            await s.commit()
        await engine.dispose()
        forget_engine()

    asyncio.run(go())


def _seed_found_boards() -> None:
    from kestrel.services import source_suggestions as ss

    async def go() -> None:
        await reset_engine()
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            for name in ("Tidewater Freight Group", "Ironbridge Supply"):
                await ss.record_board_lookup(s, name, found=True, platform="Greenhouse", jobs=3,
                                             kind="greenhouse", config={"token": "x"})
            await s.commit()
        await engine.dispose()
        forget_engine()

    asyncio.run(go())


_SUG_BOXES = """row => { const r = row.getBoundingClientRect();
  const q = s => { const e = row.querySelector(s); if (!e || e.offsetParent === null) return null;
    const b = e.getBoundingClientRect();
    return {l: b.left, r: b.right, tp: b.top, bt: b.bottom}; };
  return {row: {l: r.left, r: r.right, tp: r.top, bt: r.bottom},
    kids: Array.from(row.children).filter(e => e.offsetParent !== null).map(e => {
      const b = e.getBoundingClientRect();
      return {t: (e.textContent || '').trim().slice(0, 24), l: b.left, r: b.right, tp: b.top,
              bt: b.bottom}; }),
    title: q('.src-nm'), sub: q('.src-ty'), dismiss: q('[data-testid="suggestion-dismiss"]')}; }"""


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 700])
def test_a_suggested_row_keeps_its_layout_while_looking_up(live, theme, width):
    """Pressing Add holds the lookup open: the row must not reflow (the title stays put, nothing
    wraps to a second line) and no control overlaps the badge or the text."""
    ctx, page, errors = _page(live, theme, width)
    release: list = []
    try:
        page.route("**/sources/suggestions/add", lambda route: release.append(route))
        row = page.locator('[data-testid="suggestion"]').first
        row.wait_for()
        before = page.evaluate(_SUG_BOXES, row.element_handle())
        row.locator('[data-testid="suggestion-add"]').click()
        page.wait_for_selector(".busy-status")
        page.wait_for_timeout(450)
        shot = os.environ.get("KESTREL_SHOTS")
        if shot:
            os.makedirs(shot, exist_ok=True)
            page.locator(".src-sugs").screenshot(path=f"{shot}/{theme}-{width}.png")
        during = page.evaluate(_SUG_BOXES, row.element_handle())
        assert abs(during["title"]["tp"] - before["title"]["tp"]) <= 1, (before, during)
        assert abs(during["row"]["bt"] - before["row"]["bt"]) <= 1, (before, during)
        kids = during["kids"]
        status = next(k for k in kids if k["t"].startswith("Looking"))
        # The status takes the faded subline's place: clear of the title, badge and buttons.
        assert status["tp"] >= during["title"]["bt"] - 1, (status, during["title"])
        kids = [k for k in kids if not (k is not status and k["t"].startswith(("Ironbridge",
                                                                              "Tidewater")))]
        for i, a in enumerate(kids):
            assert a["l"] >= during["row"]["l"] - 1 and a["r"] <= during["row"]["r"] + 1, kids
            assert a["tp"] >= during["row"]["tp"] - 1 and a["bt"] <= during["row"]["bt"] + 1, kids
            for b in kids[i + 1:]:
                assert not _overlaps(a, b), (a, b, theme, width)
        # Dismiss stays where it was, to the right of the text.
        assert during["dismiss"] is not None
        assert abs(during["dismiss"]["l"] - before["dismiss"]["l"]) <= 1, (before, during)
        assert during["dismiss"]["l"] >= during["sub"]["r"] - 1, during
        assert not errors, errors
    finally:
        for route in release:
            route.abort()
        ctx.close()


def _stub_board_lookup(monkeypatch, outcomes: dict[str, str]) -> list[str]:
    """Answer every board lookup in the running app without the network. `outcomes` maps a
    company to "none", "down" (no definite answer) or "found". Returns the names looked up."""
    from kestrel.ingest import board_lookup

    seen: list[str] = []

    async def fake(name, http):
        seen.append(name)
        how = outcomes.get(name, "none")
        match = (board_lookup.BoardMatch("greenhouse", {"token": "x"}, 12, "Greenhouse")
                 if how == "found" else None)
        return board_lookup.LookupResult(
            company=name, match=match, tried=["greenhouse:x"], inconclusive=how == "down")

    monkeypatch.setattr(board_lookup, "find_company_board", fake)
    return seen


def _db(action):
    """Run `action(session)` against the test's database. A thread of its own: the browser
    driver keeps an event loop running in the test thread."""
    import threading

    async def go() -> None:
        await reset_engine()
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            await action(s)
            await s.commit()
        await engine.dispose()
        forget_engine()

    worker = threading.Thread(target=lambda: asyncio.run(go()))
    worker.start()
    worker.join()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 390])
def test_suggested_rows_resolve_by_themselves_in_the_background_check(
    live, monkeypatch, theme, width
):
    """#123 (a): nobody presses anything; each row goes from Checking to its answer."""
    seen = _stub_board_lookup(monkeypatch, {"Tidewater Freight Group": "none",
                                            "Ironbridge Supply": "found"})
    ctx, page, errors = _page(live, theme, width)
    try:
        none_row = page.locator('[data-testid="suggestion"][data-name="Tidewater Freight Group"]')
        found_row = page.locator('[data-testid="suggestion"][data-name="Ironbridge Supply"]')
        none_row.wait_for()
        none_row.locator('[data-testid="suggestion-add-by-hand"]').wait_for(timeout=60000)
        found_row.locator('[data-testid="suggestion-add"]').wait_for(timeout=60000)
        assert none_row.get_attribute("data-state") == "none"
        assert "No board Kestrel can read" in none_row.inner_text()
        assert none_row.locator('[data-testid="suggestion-check-again"]').count() == 1
        assert "Found on Greenhouse, 12 open" in found_row.inner_text()
        assert sorted(seen) == ["Ironbridge Supply", "Tidewater Freight Group"]
        shot = os.environ.get("KESTREL_SHOTS")
        if shot:
            os.makedirs(shot, exist_ok=True)
            page.locator(".src-sugs").screenshot(path=f"{shot}/resolved-{theme}-{width}.png")
        for row in (none_row, found_row):
            boxes = page.evaluate(_SUG_BOXES, row.element_handle())
            kids = boxes["kids"]
            for i, a in enumerate(kids):
                assert a["l"] >= boxes["row"]["l"] - 1 and a["r"] <= boxes["row"]["r"] + 1, kids
                for b in kids[i + 1:]:
                    assert not _overlaps(a, b), (a, b, theme, width)
        assert not errors, errors
    finally:
        ctx.close()


def _settle_animation(live, monkeypatch, reduce: str) -> str:
    """The animation name on a suggestion row that settled through its own poll. The lookup is
    held until the page shows Checking, so the row really settles by itself."""
    import threading

    from kestrel.ingest import board_lookup

    gate = threading.Event()

    async def held(name, http):
        import asyncio as aio

        await aio.to_thread(gate.wait, 60)
        return board_lookup.LookupResult(company=name, match=None, tried=["g:x"])

    monkeypatch.setattr(board_lookup, "find_company_board", held)
    base, browser = live
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, reduced_motion=reduce)
    page = ctx.new_page()
    try:
        page.goto(f"{base}/sources")
        page.wait_for_selector('[data-testid="suggestion"][data-state="checking"]')
        gate.set()
        settled = '[data-testid="suggestion"][data-state="none"].is-settling'
        page.wait_for_selector(settled, timeout=60000)
        return page.locator(settled).first.locator(".src-rn").evaluate(
            "e => getComputedStyle(e).animationName")
    finally:
        gate.set()
        ctx.close()


def test_a_settling_row_fades_in_to_its_answer(live, monkeypatch):
    assert _settle_animation(live, monkeypatch, "no-preference") != "none"


def test_a_settling_row_does_not_animate_with_reduced_motion(live, monkeypatch):
    assert _settle_animation(live, monkeypatch, "reduce") == "none"


def test_a_suggested_row_updates_on_the_first_press_when_the_lookup_cannot_answer(
    live, monkeypatch
):
    """#123 (b): the page was loaded while the row offered Add; the remembered answer has since
    gone. The first press must change the row (and nothing is remembered as not found)."""
    from kestrel.services import source_suggestions as ss

    _stub_board_lookup(monkeypatch, {"Tidewater Freight Group": "down"})

    async def remember(s):
        await ss.record_board_lookup(s, "Tidewater Freight Group", found=True,
                                     platform="Greenhouse", jobs=3, kind="greenhouse",
                                     config={"token": "tidewater"})

    _db(remember)
    ctx, page, errors = _page(live, "light", 1280)
    try:
        row = page.locator('[data-testid="suggestion"][data-name="Tidewater Freight Group"]')
        row.locator('[data-testid="suggestion-add"]').wait_for()

        async def forget(s):
            from kestrel.db.models import Setting

            await s.delete(await s.get(Setting, ss.BOARD_CACHE_KEY))

        _db(forget)
        row.locator('[data-testid="suggestion-add"]').click()
        page.wait_for_selector('[data-name="Tidewater Freight Group"][data-state="error"]',
                               timeout=60000)
        assert "Could not check. Try again" in row.inner_text()
        assert row.locator('[data-testid="suggestion-add"]').count() == 0
        assert row.locator('[data-testid="suggestion-try-again"]').count() == 1
        assert not errors, errors
    finally:
        ctx.close()


_ACTION_BOXES = """row => { const box = e => { const b = e.getBoundingClientRect();
    return {l: b.left, r: b.right, tp: b.top, bt: b.bottom}; };
  const vis = e => e.offsetParent !== null;
  return {row: box(row), badge: box(row.querySelector('.src-mono')),
    name: box(row.querySelector('.src-rn')),
    buttons: Array.from(row.querySelectorAll('.k-btn')).filter(vis).map(e =>
      ({t: e.textContent.trim(), ...box(e)})),
    dismiss: box(row.querySelector('[data-testid="suggestion-dismiss"]')),
    state: row.dataset.state}; }"""


def _assert_actions_sit_right(b, where):
    others = [x for x in b["buttons"] if x["t"] != "Dismiss"]
    d = b["dismiss"]
    for x in b["buttons"]:
        assert x["l"] >= b["name"]["l"] - 1, ("a button left of the name", x, where)
        assert x["r"] <= b["row"]["r"] + 1 and x["l"] >= b["row"]["l"] - 1, (x, where)
        assert not _overlaps(x, b["badge"]) and not _overlaps(x, b["name"]), (x, where)
    assert d["r"] >= b["row"]["r"] - 14, ("Dismiss is not on the right", d, b["row"], where)
    for x in others:  # Dismiss beside the others (to their right) or directly under them
        beside = d["tp"] < x["bt"] and d["l"] >= x["r"] - 1
        below = d["tp"] >= x["bt"] - 1
        assert beside or below, (d, x, where)
        assert d["r"] >= x["r"] - 1, ("Dismiss is left of the actions", d, x, where)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 390])
def test_suggestion_action_layout_in_every_state(live, theme, width):
    """Found, No board, Could not check and Checking: the actions stay grouped on the right, clear
    of the badge and the text, at a phone width as at a desktop one."""
    ctx, page, errors = _page(live, theme, width)
    try:
        rows = page.locator('[data-testid="suggestion"]')
        rows.first.wait_for()
        seen = set()
        for i in range(rows.count()):
            row = rows.nth(i)
            b = page.evaluate(_ACTION_BOXES, row.element_handle())
            seen.add(b["state"])
            _assert_actions_sit_right(b, (b["state"], theme, width))
        assert seen == {"found", "none", "error", "checking"}, seen
        shot = os.environ.get("KESTREL_SHOTS")
        if shot:
            os.makedirs(shot, exist_ok=True)
            page.locator(".src-sugs").screenshot(path=f"{shot}/actions-{theme}-{width}.png")
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 390])
def test_suggestion_action_layout_while_the_add_is_in_flight(live, theme, width):
    ctx, page, errors = _page(live, theme, width)
    release: list = []
    try:
        page.route("**/sources/suggestions/add", lambda route: release.append(route))
        row = page.locator('[data-testid="suggestion"][data-state="found"]')
        row.wait_for()
        before = page.evaluate(_ACTION_BOXES, row.element_handle())
        row.locator('[data-testid="suggestion-add"]').click()
        page.wait_for_selector(".busy-status")
        page.wait_for_timeout(450)
        during = page.evaluate(_ACTION_BOXES, row.element_handle())
        # Heights, not positions: the page may scroll on the click.
        h_before = before["row"]["bt"] - before["row"]["tp"]
        h_during = during["row"]["bt"] - during["row"]["tp"]
        assert abs(h_during - h_before) <= 1, (before["row"], during["row"])
        _assert_actions_sit_right(during, ("in flight", theme, width))
        status = page.evaluate(
            """row => { const e = row.querySelector('.busy-status'); const b =
              e.getBoundingClientRect();
              return {l: b.left, r: b.right, tp: b.top, bt: b.bottom}; }""",
            row.element_handle())
        assert not _overlaps(status, during["badge"]), status
        assert not errors, errors
    finally:
        for route in release:
            route.abort()
        ctx.close()
