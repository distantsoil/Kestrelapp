"""Runs in a real browser (UI overhaul parity check against the locked mockup,
the locked design mockup (kept privately)). Light and dark at 1280 and 1680: no overlap, no
wrapping titles or buttons, no horizontal scroll; then what the mockup demos: a live run that
updates in place without re-mounting, Stop confirming inline, the card glowing when the run
finishes, a past run opening in a centered drawer, and the "This run only" sheet asking first.

Gated like the other live-browser design-system test: set KESTREL_LIVE_BROWSER=1. The browser is
the Edge already on the machine, else a Playwright Chromium (tests/browser.py)."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db.base import iso
from kestrel.db.engine import make_async_engine
from kestrel.db.models import Run, ScrapeRun, Task
from tests import runs_seed
from tests.browser import (
    LINES_JS,
    TIMER_PROBE_JS,
    TITLE_RIGHT_JS,
    art_visible_left,
    assert_page_remounts_clean,
    launch_sync,
    wait_for_layout,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

# One line of text: every client rect of the element's text sits at the same height.
_LINES = LINES_JS

_BOXES = """sel => Array.from(document.querySelectorAll(sel)).filter(e => e.offsetParent !== null)
  .map(e => { const b = e.getBoundingClientRect();
    return {t: (e.textContent || '').trim().slice(0, 30), l: b.left, r: b.right, tp: b.top,
            bt: b.bottom}; })"""

_KIDS = """el => Array.from(el.children).filter(e => e.offsetParent !== null)
  .map(e => { const b = e.getBoundingClientRect();
    return {t: (e.textContent || '').trim().slice(0, 30), l: b.left, r: b.right, tp: b.top,
            bt: b.bottom}; })"""


def _overlaps(a, b) -> bool:
    return (
        a["l"] < b["r"] - 1
        and b["l"] < a["r"] - 1
        and a["tp"] < b["bt"] - 1
        and b["tp"] < a["bt"] - 1
    )


def _db(fn):
    async def go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                out = await fn(s)
                await s.commit()
                return out
        finally:
            await engine.dispose()

    # Playwright's sync API keeps an event loop on this thread, so the write runs on its own.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(go())).result()


def _serve(active: bool):
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    runs_seed.seed(active=active)
    return running_server(worker=False), sync_playwright()


@pytest.fixture(params=[False, True], ids=["idle", "running"])
def live(request):
    server, pw = _serve(request.param)
    with server as base, pw as p:
        browser = launch_sync(p)
        yield base, browser, request.param
        browser.close()


@pytest.fixture
def running():
    server, pw = _serve(True)
    with server as base, pw as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


def _page(base, browser, theme: str, width: int):
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/runs")
    page.wait_for_selector('[data-testid="run-live"]')
    wait_for_layout(page)
    return ctx, page, errors


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_no_overlap_no_wrapping_no_sideways_scroll(live, theme, width):
    base, browser, is_running = live
    ctx, page, errors = _page(base, browser, theme, width)
    try:
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 0") is True
        assert (
            page.evaluate(
                "document.querySelector('.main, main').scrollWidth <= "
                "document.querySelector('.main, main').clientWidth + 1"
            )
            is True
        )
        # Titles, pills, chips, buttons and the live step read on one line.
        for sel in (
            "h1",
            ".rn-chip",
            ".k-pill",
            ".rn-page .k-btn",
            ".rn-top h3",
            ".rn-run b",
            ".rn-tile b",
            ".rn-stg__nm",
            ".rn-split .k-btn",
        ):
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    assert page.evaluate(_LINES, el) <= 1, (sel, theme, width, el.inner_text()[:40])
        # A History sentence is shown whole: nothing cut with an ellipsis, nothing spilling out.
        for el in page.query_selector_all(".rn-run__why"):
            if el.is_visible():
                cut = page.evaluate(
                    "e => e.scrollWidth > e.clientWidth + 1 || e.scrollHeight > e.clientHeight + 1",
                    el,
                )
                assert not cut, ("why is truncated", el.inner_text(), theme, width)
        # Idle: the three small cards stay in one row (no orphan) unless online adds a fourth.
        if not is_running:
            minis = page.evaluate(_BOXES, "#runs-live .rn-mini")
            if minis and len(minis) == 3:
                assert len({round(m["tp"]) for m in minis}) == 1, (minis, theme, width)
        # No two siblings in these rows overlap.
        for container in (
            ".rn-top",
            ".rn-acts__main",
            ".rn-stages",
            ".rn-tiles",
            ".rn-minis",
            ".rn-run",
            ".rn-nums",
            ".k-scene__title",
        ):
            for group in page.query_selector_all(container):
                if not group.is_visible():
                    continue
                kids = page.evaluate(_KIDS, group)
                for i, a in enumerate(kids):
                    for b in kids[i + 1 :]:
                        assert not _overlaps(a, b), (container, a, b, theme, width)
        # The help column sits at the right on a wide window and under the cards at 1300px of
        # content or less (the shared container rule); nothing sits over the live card.
        card = page.evaluate(_BOXES, "#runs-live")[0]
        help_col = page.evaluate(_BOXES, ".k-help-col")[0]
        if width >= 1680:
            assert help_col["l"] >= card["r"] - 1 and help_col["tp"] <= card["tp"] + 2
        else:
            assert help_col["tp"] >= card["bt"] - 1
        for h in page.evaluate(_BOXES, ".k-help-col > *"):
            assert not _overlaps(card, h), (card, h)
        # Stage tiles sit inside the card; the band's art stays clear of the title block.
        if is_running:
            for tile in page.evaluate(_BOXES, ".rn-stg"):
                assert tile["l"] >= card["l"] and tile["r"] <= card["r"] + 1, (tile, card)
        title = page.evaluate(_BOXES, ".k-scene__title")[0]
        drawn = art_visible_left(page)
        if drawn is not None:
            assert page.evaluate(TITLE_RIGHT_JS) <= drawn, (title, drawn)
        assert not page.query_selector("#announcement-slot")
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme,width", [("light", 1680), ("dark", 1680), ("light", 900)])
def test_skipped_summary_fits_the_existing_drawer(running, theme, width, tmp_path):
    base, browser = running

    async def record(session):
        rows = (await session.execute(select(ScrapeRun).order_by(ScrapeRun.id))).scalars().all()
        rows[0].counts = {**rows[0].counts, "dropped_outside_area": 30}
        rows[1].counts = {**rows[1].counts, "dropped_unrelated_title": 12}

    _db(record)
    ctx, page, errors = _page(base, browser, theme, width)
    try:
        page.get_by_test_id("rn-run").first.click()
        summary = page.get_by_test_id("rn-skipped")
        summary.wait_for()
        wait_for_layout(page)
        assert summary.inner_text() == "Skipped 42: 30 outside your area, 12 unrelated titles"
        assert summary.evaluate(
            "el => el.scrollWidth <= el.clientWidth && el.scrollHeight <= el.clientHeight"
        )
        assert page.evaluate(_LINES, summary.element_handle()) == 1
        kids = page.locator('[data-testid="rn-drawer-run"]').evaluate(_KIDS)
        for index, first in enumerate(kids):
            for second in kids[index + 1 :]:
                assert not _overlaps(first, second), (first, second)
        assert not errors
        page.screenshot(path=str(tmp_path / f"runs-skips-{theme}-{width}.png"))
    finally:
        ctx.close()


_LONG_TITLE = "Senior" + "Unbroken" * 40 + "Title"
_LONG_ORG = "A Very Long Fictional Organization Name " * 8


def _seed_title_sample() -> None:
    sample = [{"title": f"Pastry Chef {i}", "org": f"Fictional Bakery {i}"} for i in range(23)]
    sample.append({"title": _LONG_TITLE, "org": _LONG_ORG})
    sample.append({"title": "Night Baker", "org": None})

    async def record(session):
        rows = (await session.execute(select(ScrapeRun).order_by(ScrapeRun.id))).scalars().all()
        rows[0].counts = {
            **rows[0].counts,
            "dropped_unrelated_title": 40,
            "dropped_title_sample": sample,
        }

    _db(record)


def _open_dropped(page):
    page.get_by_test_id("rn-run").first.click()
    page.get_by_test_id("rn-dropped-titles").wait_for()
    wait_for_layout(page)
    return page.get_by_test_id("rn-dropped-titles")


_OPEN_PROBE = """el => new Promise(resolve => {
  const list = el.querySelector('ul');
  const seen = [];
  el.querySelector('summary').click();
  // Sample every frame until the fold has settled, bounded by time (2s), not a frame count,
  // so a fast-refresh display cannot end the probe before the .28s animation does.
  const t0 = performance.now();
  const tick = () => {
    seen.push(parseFloat(getComputedStyle(list).opacity));
    const settled = seen.length >= 4 && seen[seen.length - 1] === 1
      && list.getAnimations().length === 0;
    if (!settled && performance.now() - t0 < 2000) {
      requestAnimationFrame(tick);
    } else { resolve(seen); }
  };
  tick();
})"""


@pytest.mark.parametrize("theme,width", [("light", 1680), ("dark", 1680), ("light", 900)])
def test_dropped_titles_disclosure_fits_opens_and_closes_smoothly(running, theme, width, tmp_path):
    base, browser = running
    _seed_title_sample()
    ctx, page, errors = _page(base, browser, theme, width)
    try:
        details = _open_dropped(page)
        assert details.evaluate("el => el.open") is False
        details.locator("summary").click()
        page.wait_for_function(
            "document.querySelector('[data-testid=rn-dropped-titles]').open === true"
        )
        page.wait_for_timeout(450)  # the fold animation has ended
        assert details.evaluate("el => el.open") is True
        count = page.get_by_test_id("rn-dropped-count")
        assert "Showing 25 of 40." in count.inner_text()
        drawer = page.locator('[data-testid="rn-drawer-run"]')
        box = drawer.bounding_box()
        assert box
        for sel in ('[data-testid="rn-dropped-titles"] li', '[data-testid="rn-dropped-count"]'):
            items = page.locator(sel)
            assert items.count() >= 1
            for index in range(items.count()):
                item = items.nth(index)
                assert item.evaluate("el => el.scrollWidth <= el.clientWidth + 1"), (
                    sel,
                    item.inner_text()[:40],
                )
                rect = item.bounding_box()
                assert rect and rect["x"] >= box["x"] - 1, (sel, rect, box)
                assert rect["x"] + rect["width"] <= box["x"] + box["width"] + 1, (sel, rect, box)
        kids = drawer.evaluate(_KIDS)
        for index, first in enumerate(kids):
            for second in kids[index + 1 :]:
                assert not _overlaps(first, second), (first, second)
        assert not errors, errors
        page.screenshot(path=str(tmp_path / f"runs-dropped-{theme}-{width}.png"))
        details.locator("summary").click()
        page.wait_for_timeout(300)
        assert details.evaluate("el => el.open") is False
        assert not errors, errors
    finally:
        ctx.close()


def test_dropped_titles_disclosure_animates_open(running):
    base, browser = running
    _seed_title_sample()
    ctx, page, errors = _page(base, browser, "light", 1680)
    try:
        details = _open_dropped(page)
        seen = details.evaluate(_OPEN_PROBE)
        assert seen[0] < 1, seen
        assert seen[-1] == 1, seen
        assert details.evaluate("el => el.open") is True
        assert not errors, errors
    finally:
        ctx.close()


def test_dropped_titles_disclosure_opens_at_once_with_reduced_motion(running):
    base, browser = running
    _seed_title_sample()
    ctx = browser.new_context(
        viewport={"width": 1680, "height": 900}, color_scheme="light", reduced_motion="reduce"
    )
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"{base}/runs")
        page.wait_for_selector('[data-testid="run-live"]')
        wait_for_layout(page)
        details = _open_dropped(page)
        seen = details.evaluate(_OPEN_PROBE)
        assert seen[0] == 1 and set(seen) == {1}, seen
        assert details.evaluate("el => el.open") is True
        details.locator("summary").click()
        page.wait_for_function(
            "document.querySelector('[data-testid=rn-dropped-titles]').open === false",
            timeout=1000,
        )
        assert not errors, errors
    finally:
        ctx.close()


def test_a_past_run_opens_in_a_centered_drawer_that_does_not_jump(running):
    base, browser = running
    ctx, page, errors = _page(base, browser, "light", 1680)
    try:
        page.get_by_test_id("rn-run").first.click()
        drawer = page.locator("#rn-drawer")
        page.wait_for_selector('[data-testid="rn-drawer-run"]')
        wait_for_layout(page)
        box = drawer.bounding_box()
        assert box and abs((box["y"] + box["height"] / 2) - 450) < 40, box
        before = drawer.bounding_box()
        # A poll lands while it is open: fire the page's own refresh (the 5 s timer is too slow
        # to wait for) and wait for its response.
        with page.expect_response(lambda r: r.url.endswith("/runs") and r.request.method == "GET"):
            page.evaluate("htmx.trigger(document.body, 'run-refresh')")
        wait_for_layout(page)
        assert drawer.bounding_box() == before
        assert "roles per minute, by step" in drawer.inner_text().lower()
        page.keyboard.press("Escape")
        page.wait_for_function(
            "document.querySelector('#rn-drawer').getAttribute('aria-hidden') === 'true'"
        )
        assert not errors, errors
    finally:
        ctx.close()


def test_the_live_card_updates_in_place_and_never_re_mounts(running):
    base, browser = running
    ctx, page, errors = _page(base, browser, "light", 1680)
    try:
        page.evaluate("""() => {
          window.__tile = document.querySelector('[data-bar="search"]');
          window.__feed = document.querySelector('[data-feed]');
          window.__live = document.querySelector('[data-region="live"] .rn-live-in');
          window.__count = document.querySelectorAll('[data-feed] li').length;
        }""")
        step_before = page.get_by_test_id("run-step").inner_text()

        async def advance(s):
            run = (await s.execute(select(Run).where(Run.status == "running"))).scalars().one()
            task = (await s.execute(select(Task).where(Task.type == "scrape"))).scalars().one()
            task.status = "done"
            s.add(
                ScrapeRun(
                    source_id=task.payload["source_id"],
                    status="ok",
                    started_at=iso(datetime.now(UTC) - timedelta(seconds=30)),
                    finished_at=iso(datetime.now(UTC)),
                    counts={"new": 7},
                )
            )
            return run.id

        _db(advance)
        page.evaluate("htmx.trigger(document.body, 'run-refresh')")
        page.wait_for_function(
            "document.querySelectorAll('[data-feed] li').length > window.__count"
        )
        # The same nodes are still in the page: counts and bars were patched, nothing was swapped.
        assert page.evaluate("""() =>
            document.querySelector('[data-bar="search"]') === window.__tile
            && document.querySelector('[data-feed]') === window.__feed
            && document.querySelector('[data-region="live"] .rn-live-in') === window.__live""")
        assert page.get_by_test_id("run-step").inner_text() != step_before
        assert (
            page.evaluate("document.querySelector('[data-feed] li').textContent").count(
                "Found 7 roles"
            )
            == 1
        )
        assert "Online" not in page.locator(".rn-stages").inner_text()
        assert not errors, errors
    finally:
        ctx.close()


def test_a_poll_inserts_no_node_above_the_latest_run_card(live):
    """#77: the 5 s poll once inserted an empty spinner above the card and pushed the page down."""
    base, browser, _active = live
    ctx, page, errors = _page(base, browser, "light", 1280)
    try:
        page.evaluate("""() => {
          window.__added = [];
          const card = document.querySelector('[data-testid="latest-run"]');
          window.__top = card.getBoundingClientRect().top;
          new MutationObserver(ms => ms.forEach(m => m.addedNodes.forEach(n => {
            if (n.nodeType === 1 && (n.matches('.busy-status') || n.querySelector('.busy-status')))
              window.__added.push(n.className);
          }))).observe(document.body, {childList: true, subtree: true});
          window.__polls = 0;
          document.addEventListener('htmx:afterRequest', e => {
            if (e.detail.elt && e.detail.elt.id === 'runs-poll') window.__polls += 1; });
        }""")
        page.evaluate("htmx.trigger(document.body, 'run-refresh')")
        page.wait_for_function("window.__polls >= 1")
        page.wait_for_timeout(200)
        assert page.evaluate("window.__added") == []
        assert page.evaluate("document.querySelector('.busy-status')") is None
        assert page.evaluate(
            "document.querySelector('[data-testid=\"latest-run\"]').getBoundingClientRect().top"
        ) == page.evaluate("window.__top")
        assert not errors, errors
    finally:
        ctx.close()


def test_stop_confirms_inline_and_keep_running_backs_out(running):
    base, browser = running
    ctx, page, errors = _page(base, browser, "light", 1280)
    try:
        main, ask = page.locator(".rn-acts__main"), page.locator(".rn-acts__ask")
        assert main.is_visible() and not ask.is_visible()
        page.get_by_test_id("run-stop").click()
        assert ask.is_visible() and not main.is_visible()
        assert "Stop this run?" in ask.inner_text()
        assert page.evaluate(_LINES, ask.locator(".rn-cfm").element_handle()) <= 3
        page.get_by_role("button", name="Keep running").click()
        assert main.is_visible() and not ask.is_visible()
        # A poll that lands while the question is open does not close it.
        page.get_by_test_id("run-stop").click()
        with page.expect_response(lambda response: response.url.endswith("/runs")
                                  and response.request.method == "GET"):
            page.evaluate("htmx.trigger(document.body, 'run-refresh')")
        wait_for_layout(page)
        assert ask.is_visible()
        assert not errors, errors
    finally:
        ctx.close()


def test_the_card_glows_and_the_header_swaps_when_the_run_finishes(running):
    base, browser = running
    ctx, page, errors = _page(base, browser, "light", 1680)
    try:
        assert page.get_by_test_id("rn-chip").is_visible()
        assert page.locator('[data-testid="rn-run-now"]').count() == 0

        async def finish(s):
            run = (await s.execute(select(Run).where(Run.status == "running"))).scalars().one()
            run.status, run.finished_at = "done", iso(datetime.now(UTC))
            run.counts = {"scrape": {"completed": 3, "errors": 0, "new": 11}}
            for t in (await s.execute(select(Task))).scalars():
                t.status = "done"

        _db(finish)
        page.evaluate("htmx.trigger(document.body, 'run-refresh')")
        page.wait_for_selector('[data-region="live"] [data-run="idle"]')
        page.wait_for_selector('[data-testid="rn-run-now"]')
        assert "k-arrive" in page.get_attribute('[data-region="live"]', "class")
        assert page.locator('[data-testid="rn-chip"]').count() == 0
        assert "Latest run" in page.locator('[data-region="live"]').inner_text()
        assert not errors, errors
    finally:
        ctx.close()


def test_this_run_only_asks_before_anything_goes_online(live):
    base, browser, is_running = live
    if is_running:
        pytest.skip("the menu is only there while idle")
    ctx, page, errors = _page(base, browser, "light", 1280)
    try:
        page.get_by_test_id("rn-run-menu").click()
        page.get_by_test_id("rn-this-run").click()
        dialog = page.locator("#rn-choose-dialog")
        dialog.wait_for(state="visible")
        wait_for_layout(page)
        page.wait_for_timeout(600)  # the sheet's entrance finishes before it is measured
        mb = dialog.bounding_box()
        assert abs((mb["y"] + mb["height"] / 2) - 450) < 60
        assert page.locator("#rn-ask").is_hidden()
        assert page.get_by_test_id("rn-mode-online").is_disabled()  # Hosted mode is not set up
        assert page.locator("#rn-start").is_enabled()
        dialog.get_by_role("button", name="Cancel").click()
        dialog.wait_for(state="hidden")
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_page_follows_the_theme_tokens(live, theme):
    base, browser, _ = live
    ctx, page, _ = _page(base, browser, theme, 1280)
    try:
        bg = page.evaluate("getComputedStyle(document.querySelector('.rn-hist')).backgroundColor")
        if theme == "dark":
            assert bg != "rgb(255, 255, 255)"
        else:
            assert bg == "rgb(255, 255, 255)"
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_needs_another_try_rail_wraps_to_full_text(running, theme):
    """D-UI-218: the rail's lines wrap as the runs mockup does; nothing is cut with an ellipsis."""
    base, browser = running
    ctx, page, errors = _page(base, browser, theme, 1280)
    try:
        rows = page.evaluate("""() => Array.from(document.querySelectorAll(
            '[data-testid="rn-q"] .rn-q__t > *')).map(e => { const c = getComputedStyle(e);
          return {ws: c.whiteSpace, to: c.textOverflow, clipped: e.scrollWidth > e.clientWidth + 1
                  || e.scrollHeight > e.clientHeight + 1}; })""")
        assert rows
        for row in rows:
            assert row["ws"] != "nowrap" and row["to"] != "ellipsis" and not row["clipped"], row
        assert not errors
    finally:
        ctx.close()


def test_the_page_mounts_unmounts_and_mounts_again_with_one_set_of_listeners():
    """The page lifecycle (D-UI-210): Runs unmounted and mounted again in place starts exactly
    what it started on the document load and leaves no timer behind. After it, one click on the
    split button's caret opens the menu (a doubled listener would open and shut it) and Escape
    closes it. Unmounted, the page stops answering."""
    server, pw = _serve(False)
    with server as base, pw as p:
        browser = launch_sync(p)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="light")
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(TIMER_PROBE_JS)
        try:
            page.goto(f"{base}/runs")
            page.wait_for_selector('[data-testid="run-live"]')
            wait_for_layout(page)
            first = assert_page_remounts_clean(page, "runs", "runs.js", settle_ms=500)
            assert first["timers"] == 0
            caret = page.get_by_test_id("rn-run-menu")
            caret.click()
            assert page.locator("#rn-menu").is_visible()
            assert caret.get_attribute("aria-expanded") == "true"
            page.keyboard.press("Escape")
            assert page.locator("#rn-menu").is_hidden()
            assert caret.get_attribute("aria-expanded") == "false"
            # The poll still lands after a remount and patches the page.
            with page.expect_response(
                lambda r: r.url.endswith("/runs") and r.request.method == "GET"
            ):
                page.evaluate("htmx.trigger(document.body, 'run-refresh')")
            page.evaluate("KUI.page.unmount()")
            caret.click()
            assert page.locator("#rn-menu").is_hidden()
            assert not errors, errors
        finally:
            ctx.close()
            browser.close()


def test_the_run_detail_page_names_itself_and_starts_nothing():
    """The page lifecycle (D-UI-210): one run's page has no script of its own (runs.js belongs to
    the list), so it names itself on the content root, nothing is defined under that name and
    nothing mounts, with no listener or timer of a page script running."""
    ids = runs_seed.seed(active=False)
    from kestrel.web.serve import running_server

    run_id = ids["runs"][0]
    from playwright.sync_api import sync_playwright

    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="light")
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(TIMER_PROBE_JS)
        try:
            page.goto(f"{base}/runs/{run_id}")
            page.wait_for_selector('[data-testid="rn-status"]')
            wait_for_layout(page)
            assert page.get_attribute("main.container", "data-k-page") == "run-detail"
            assert page.evaluate("KUI.page.current()") is None
            assert page.evaluate("KUI.page._stats()") == {
                "name": None, "listeners": 0, "timers": 0, "observers": 0}
            assert page.evaluate("KUI.page.mount()") is False  # nothing is defined under the name
            assert page.query_selector('script[src*="/static/js/runs.js"]') is None
            page.wait_for_timeout(600)
            assert page.evaluate("window.__kTimers.pending('/static/js/runs.js')") == 0
            assert not errors, errors
        finally:
            ctx.close()
            browser.close()


# ------------------------------------------------------------------ PR 2: say what Kestrel is doing
_FRAMES_JS = """() => {
  window.__frames = []; window.__go = true;
  const tick = () => {
    const region = document.querySelector('[data-region="live"]');
    const acts = document.querySelector('.rn-acts');
    const latest = document.querySelector('[data-testid="latest-run"]');
    window.__frames.push({
      h: region ? Math.round(region.getBoundingClientRect().height * 10) / 10 : null,
      top: latest ? Math.round(latest.getBoundingClientRect().top * 10) / 10 : null,
      shown: acts ? Array.from(acts.querySelectorAll('button')).filter(b => b.offsetParent !== null
        && getComputedStyle(b).visibility !== 'hidden').length : 0,
      step: (document.querySelector('[data-testid="run-step"]') || {}).textContent || ''});
    if (window.__go) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}"""


def _eased(frames, key="top", what="the card"):
    """Whenever a value changes by more than 2 px it travels: several in-between values, and no
    single frame carries more than 60% of the whole move (a jump would carry all of it)."""
    vals = [f[key] for f in frames if f[key] is not None]
    moves, run = [], [vals[0]]
    for v in vals[1:]:
        if abs(v - run[-1]) > 0.05:
            run.append(v)
            continue
        if len(run) > 1:
            moves.append(run)
        run = [v]
    if len(run) > 1:
        moves.append(run)
    for mv in moves:
        span = abs(mv[-1] - mv[0])
        if span <= 2:
            continue
        biggest = max(abs(b - a) for a, b in zip(mv, mv[1:], strict=False))
        assert len(mv) >= 4 and biggest <= 0.6 * span, (what, mv)
    return moves


def _stop_flow_with(monkeypatch=None, *, lock_failure: bool = False):
    """Patch the server (it runs in this process) so the stop write counts calls, and optionally
    fails the way a held lock does: a driver 'database is locked' past a short retry budget."""
    import sqlite3

    from sqlalchemy.exc import OperationalError

    from kestrel.pipeline import runlog
    from kestrel.web import db_retry

    calls = {"writes": 0}
    orig = db_retry.write_with_retry
    real_finish = runlog.finish_run_in

    async def short(fn, **kw):
        calls["writes"] += 1
        return await orig(fn, **{**kw, "budget_s": 0.3})

    async def locked(*a, **k):
        raise OperationalError("UPDATE run", {}, sqlite3.OperationalError("database is locked"))

    monkeypatch.setattr(db_retry, "write_with_retry", short)
    if lock_failure:
        monkeypatch.setattr(runlog, "finish_run_in", locked)
    else:
        monkeypatch.setattr(runlog, "finish_run_in", real_finish)
    return calls


def _unpause():
    from kestrel.web.app import set_paused

    set_paused(False)


# Every animation that ends has ended (the live dot and the like loop for ever: not waited on).
_ANIMATIONS_DONE = (
    "document.getAnimations().filter(a => a.playState === 'running'"
    " && a.effect.getComputedTiming().endTime !== Infinity).length === 0"
)


def _hold(page, pattern: str) -> list:
    """Park every request that matches `pattern` until the test lets it go (a slow server, with
    nothing to sleep through): the routes wait in the returned list."""
    held: list = []
    page.route(pattern, lambda route: held.append(route))
    return held


def test_stop_says_stopping_at_once_even_when_the_server_is_slow_and_a_second_press_is_a_no_op(
    running, monkeypatch
):
    """#91: the press changes the card before the server answers; the backend sees one stop."""
    base, browser = running
    calls = _stop_flow_with(monkeypatch)
    ctx, page, errors = _page(base, browser, "light", 1280)
    sent: list[str] = []
    try:
        page.on("request", lambda r: sent.append(r.url) if r.url.endswith("/runs/stop") else None)
        held = _hold(page, "**/runs/stop")
        page.get_by_test_id("run-stop").click()
        page.evaluate(_FRAMES_JS)
        # Two clicks reach the handler before the first response; one request leaves the page.
        page.evaluate("""() => { const b = document.querySelector('[data-testid="run-stop-yes"]');
                                  b.click(); b.click(); }""")
        btn = page.get_by_test_id("run-stopping")
        assert btn.is_visible(timeout=300) and btn.is_disabled()
        assert btn.inner_text().strip() == "Stopping…"
        assert page.get_by_test_id("run-title").inner_text() == "Stopping"
        assert "Stopping" in page.get_by_test_id("run-step").inner_text()
        assert page.locator(".rn-acts__main").is_hidden()
        assert page.locator(".rn-acts__ask").is_hidden()
        btn.click(force=True)
        page.evaluate("document.querySelector('[data-testid=\"run-stop-yes\"]').click()")
        page.wait_for_function(_ANIMATIONS_DONE)
        assert len(held) == 1 and len(sent) == 1, (held, sent)
        with page.expect_response(lambda r: r.url.endswith("/runs/stop")):
            held[0].continue_()
        page.wait_for_function(_ANIMATIONS_DONE)
        assert len(sent) == 1 and calls["writes"] == 1, (sent, calls)
        page.evaluate("window.__go = false")
        frames = page.evaluate("window.__frames")
        assert frames and all(f["shown"] >= 1 for f in frames), frames
        _eased(frames, "h", "the live card")
        _eased(frames, "top", "the card below it")
        assert not errors, errors
    finally:
        _unpause()
        ctx.close()


def test_the_live_card_height_travels_when_the_confirm_question_folds_away(running):
    base, browser = running
    ctx, page, errors = _page(base, browser, "light", 390)
    try:
        page.get_by_test_id("run-stop").click()  # the long question opens: the card grows
        wait_for_layout(page)
        page.evaluate(_FRAMES_JS)
        page.get_by_role("button", name="Keep running").click()
        page.wait_for_function(_ANIMATIONS_DONE)
        page.evaluate("window.__go = false")
        moves = _eased(page.evaluate("window.__frames"), "h", "the live card")
        assert moves, "the card changed height at 390px and the test saw no move"
    finally:
        ctx.close()


async def _lease_scrape(s):
    task = (await s.execute(select(Task).where(Task.type == "scrape"))).scalars().one()
    task.status = "leased"
    task.lease_until = iso(datetime.now(UTC) + timedelta(minutes=5))


def test_a_stop_that_fails_on_a_held_lock_shows_the_true_state_and_can_be_pressed_again(
    running, monkeypatch
):
    """#91: the write is blocked past the retry budget (a real 503 from the server). The worker
    was held first, so the card reads Paused, and Stop works again."""
    base, browser = running
    _stop_flow_with(monkeypatch, lock_failure=True)
    ctx, page, errors = _page(base, browser, "light", 1280)
    try:
        _db(_lease_scrape)  # a step is under a live lease: a flag-driven card would say Stopping
        page.get_by_test_id("run-stop").click()
        with page.expect_response(lambda r: r.url.endswith("/runs/stop")) as resp:
            page.get_by_test_id("run-stop-yes").click()
        assert resp.value.status == 503
        page.wait_for_function(
            "document.querySelector('[data-testid=\"run-live\"]').dataset.run === 'paused'")
        assert page.get_by_test_id("run-title").inner_text() == "Paused"
        stop = page.get_by_test_id("run-stop")
        assert stop.is_visible() and stop.is_enabled()
        stop.click()  # asks again; nothing is stuck
        assert page.locator(".rn-acts__ask").is_visible()
    finally:
        _unpause()
        ctx.close()


def test_a_restarted_app_never_says_stopping_whatever_the_stored_rows_say(running):
    """A real restart: the run is stored as stopping, a stop was asked, a step holds a lease, but
    the new process's worker is not held (nobody paused it). The card says nothing of stopping."""
    base, browser = running

    async def stored(s):
        run = (await s.execute(select(Run).where(Run.status == "running"))).scalars().one()
        run.status = "stopping"
        run.finished_at = iso(datetime.now(UTC))
        await _lease_scrape(s)
        from kestrel.analytics.run_summary import STOP_KEY
        from kestrel.db.models import Setting

        s.add(Setting(key=STOP_KEY, value={"run_id": run.id}))

    _db(stored)
    _unpause()  # a fresh process starts with the worker running
    ctx, page, errors = _page(base, browser, "light", 1280)
    try:
        assert page.get_by_test_id("run-live").get_attribute("data-run") != "stopping"
        assert not page.get_by_test_id("run-stopping").is_visible()
        assert not errors, errors
    finally:
        ctx.close()


def test_the_server_says_stopping_from_rows_while_a_step_holds_a_live_lease(running):
    """Stopping comes from the stored run status plus a live lease; a poll never reverts it."""
    base, browser = running
    ctx, page, errors = _page(base, browser, "light", 1280)
    try:
        async def lease(s):
            task = (await s.execute(select(Task).where(Task.type == "scrape"))).scalars().one()
            task.status = "leased"
            task.lease_until = iso(datetime.now(UTC) + timedelta(minutes=5))

        _db(lease)
        page.get_by_test_id("run-stop").click()
        page.get_by_test_id("run-stop-yes").click()
        page.wait_for_function(
            "document.querySelector('[data-testid=\"run-live\"]').dataset.run === 'stopping'")
        with page.expect_response(lambda r: r.url.endswith("/runs") and r.request.method == "GET"):
            page.evaluate("htmx.trigger(document.body, 'run-refresh')")
        wait_for_layout(page)
        assert page.get_by_test_id("run-title").inner_text() == "Stopping"
        assert page.get_by_test_id("run-stopping").is_disabled()
        # The header chip and the status rail say the same word as the card (one derivation).
        page.wait_for_function(
            "document.querySelector('[data-testid=\"rn-chip\"]').textContent.includes('Stopping')")
        assert "Paused" not in page.get_by_test_id("rn-chip").inner_text()
        assert page.get_by_test_id("rn-run-now").count() == 0  # no Run now while stopping
        page.evaluate("htmx.ajax('GET', '/status/rail', {target: '#rail', swap: 'innerHTML'})")
        page.wait_for_function(
            "document.querySelector('[data-testid=\"status-readout\"]').textContent"
            ".includes('Stopping')")
        assert "Pausing" not in page.get_by_test_id("status-readout").inner_text()
        # The step finishes: nothing under a lease, so the card stops saying Stopping.
        async def done(s):
            task = (await s.execute(select(Task).where(Task.type == "scrape"))).scalars().one()
            task.status = "done"

        _db(done)
        with page.expect_response(lambda r: r.url.endswith("/runs") and r.request.method == "GET"):
            page.evaluate("htmx.trigger(document.body, 'run-refresh')")
        page.wait_for_function(
            "document.querySelector('[data-testid=\"run-live\"]').dataset.run !== 'stopping'")
        assert not errors, errors
    finally:
        _unpause()
        ctx.close()


_BUSY_WATCH_JS = """() => {
  window.__busy = [];
  new MutationObserver(() => {
    if (document.body.classList.contains('busy')) window.__busy.push(1); })
    .observe(document.body, {attributes: true, attributeFilter: ['class']});
}"""


_TWO_FRAMES = "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"


def test_a_poll_and_the_scan_button_refresh_leave_the_busy_bar_idle(running):
    """#99: what the app asks for on its own never lights the bar."""
    base, browser = running
    ctx, page, errors = _page(base, browser, "light", 1280)
    try:
        page.evaluate(_BUSY_WATCH_JS)
        with page.expect_response(lambda r: r.url.endswith("/runs") and r.request.method == "GET"):
            page.evaluate("htmx.trigger(document.body, 'run-refresh')")
        page.evaluate(_TWO_FRAMES)
        assert page.evaluate("window.__busy") == []
        # The toast script's own refresh: the rail says one thing, the button another. The
        # request is held in flight while the bar is read.
        today = ctx.new_page()
        today.goto(f"{base}/")
        today.wait_for_selector("#scan-btn", state="attached")
        today.evaluate(_BUSY_WATCH_JS)
        held = _hold(today, "**/scan/button")
        with today.expect_request("**/scan/button"):
            today.evaluate("""() => {
              const sig = document.getElementById('scan-signal');
              sig.setAttribute('data-ready', '1'); sig.setAttribute('data-scanning', '0');
              sig.setAttribute('data-event', 'none');
              document.getElementById('scan-btn').setAttribute('data-scanning', '1');
              document.dispatchEvent(new Event('htmx:oobAfterSwap'));
            }""")
        today.evaluate(_TWO_FRAMES)
        assert today.evaluate("document.body.classList.contains('busy')") is False
        held[0].continue_()
        today.evaluate(_TWO_FRAMES)
        assert today.evaluate("window.__busy") == []
        assert not errors, errors
    finally:
        ctx.close()


def test_a_press_that_runs_through_htmx_ajax_lights_the_busy_bar():
    """#99: the Sources drawer opens through htmx.ajax; it is a press, so the bar shows."""
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    runs_seed.seed(active=False)
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        try:
            page.goto(f"{base}/sources")
            page.wait_for_selector("[data-src-open]", state="attached")
            held = _hold(page, "**/sources/*/drawer")
            page.evaluate(_BUSY_WATCH_JS)
            with page.expect_request("**/sources/*/drawer"):
                page.evaluate("document.querySelector('[data-src-open]').click()")  # scripted
            assert page.evaluate("document.body.classList.contains('busy')") is True
            held[0].continue_()
            page.wait_for_function("!document.body.classList.contains('busy')")
        finally:
            ctx.close()
            browser.close()


def test_aborting_one_of_two_presses_keeps_the_bar_for_the_other(running):
    """An abort lets go of the request it ended, and only that one: the other press keeps the bar
    until it answers (no 30 s timer is waited on)."""
    base, browser = running
    ctx, page, errors = _page(base, browser, "light", 1280)
    try:
        held = _hold(page, "**/runs/history*")
        page.evaluate("""() => {
          for (const n of ['a', 'b']) {
            const b = document.createElement('button');
            b.id = 'press-' + n; b.textContent = n;
            b.setAttribute('hx-get', '/runs/history?x=' + n); b.setAttribute('hx-swap', 'none');
            document.body.appendChild(b); htmx.process(b);
          }
        }""")
        with page.expect_request("**/runs/history?x=a"):
            page.locator("#press-a").click()
        with page.expect_request("**/runs/history?x=b"):
            page.locator("#press-b").click()
        assert page.evaluate("document.body.classList.contains('busy')") is True
        page.evaluate("htmx.trigger(document.getElementById('press-a'), 'htmx:abort')")
        page.evaluate(_TWO_FRAMES)
        assert page.evaluate("document.body.classList.contains('busy')") is True  # b is waiting
        next(r for r in held if r.request.url.endswith("x=b")).continue_()
        # a was released by its abort: when b answers nothing is left, long before any timer.
        page.wait_for_function("!document.body.classList.contains('busy')", timeout=3000)
        assert not errors, errors
    finally:
        ctx.close()


def test_a_busy_count_that_never_comes_back_clears_itself(running):
    """#99: a counted request with no answer cannot keep the bar running on an idle app."""
    base, browser = running
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    try:
        page.clock.install()
        page.goto(f"{base}/runs")
        page.wait_for_selector('[data-testid="run-live"]')
        page.evaluate("""() => document.dispatchEvent(new CustomEvent('htmx:beforeRequest', {
          detail: {xhr: {}, elt: document.body, requestConfig: {headers: {}}}}))""")
        assert page.evaluate("document.body.classList.contains('busy')") is True
        page.clock.fast_forward(31000)
        assert page.evaluate("document.body.classList.contains('busy')") is False
    finally:
        ctx.close()


@pytest.mark.parametrize("width", [390, 700, 1280])
def test_a_poll_updates_the_work_in_flight_without_moving_the_card(running, width):
    """#92: retries and elapsed time arrive through the 5 s poll; when the line wraps differently
    (a retry count appears, 9 min becomes 10) the card eases to its new height, and no frame
    moves it in one step. The run starts 9 min 30 s back and is moved to 10 min 30 s by hand, so
    no minute boundary is crossed by the test's own running time."""
    base, browser = running

    async def started(s, minutes_ago, *, retry=False):
        run = (await s.execute(select(Run).where(Run.status == "running"))).scalars().one()
        run.started_at = iso(datetime.now(UTC) - timedelta(minutes=minutes_ago))
        if retry:
            task = (await s.execute(select(Task).where(Task.type == "scrape"))).scalars().one()
            task.status = "pending"
            task.attempts = 1
            task.last_error = "timed out"

    async def nine_thirty(s):
        await started(s, 9.5)

    async def retry_nine_thirty(s):
        await started(s, 9.5, retry=True)

    async def ten_thirty(s):
        await started(s, 10.5, retry=True)

    _db(nine_thirty)
    ctx, page, errors = _page(base, browser, "light", width)
    try:
        before = page.get_by_test_id("run-step").inner_text()
        assert "9 min so far" in before and "retrying" not in before, before
        page.evaluate(_FRAMES_JS)
        for change, want in ((retry_nine_thirty, "retrying"), (ten_thirty, "10 min so far")):
            _db(change)
            with page.expect_response(
                lambda r: r.url.endswith("/runs") and r.request.method == "GET"
            ):
                page.evaluate("htmx.trigger(document.body, 'run-refresh')")
            page.wait_for_function(
                "t => document.querySelector('[data-testid=\"run-step\"]').textContent"
                ".includes(t)", arg=want)
            page.wait_for_function(_ANIMATIONS_DONE)
        page.evaluate("window.__go = false")
        frames = page.evaluate("window.__frames")
        assert any("retrying" in f["step"] for f in frames)
        _eased(frames, "top", "the card below the live card")
        _eased(frames, "h", "the live card")
        assert not errors, errors
    finally:
        ctx.close()
