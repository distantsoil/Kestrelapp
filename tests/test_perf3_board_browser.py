"""D-PERF-3 item 2 in a real browser: a long Closed group draws 100 cards a column, Show more adds
the rest in place (animated, with the counts still true), the table pages, and none of it overlaps
or clips in light, dark or narrow. Set KESTREL_SHOT_DIR to keep the screenshots.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.browser import launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401
from tests.test_perf3_board import _fresh, _seed_many

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

_KIDS = """el => Array.from(el.children).filter(e => e.offsetParent !== null)
  .map(e => { const b = e.getBoundingClientRect();
    return {t: (e.textContent || '').trim().slice(0, 30), l: b.left, r: b.right, tp: b.top,
            bt: b.bottom}; })"""


def _overlaps(a, b) -> bool:
    return a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1 and a["tp"] < b["bt"] - 1 \
        and b["tp"] < a["bt"] - 1


def _shot(page, name: str) -> None:
    out = os.environ.get("KESTREL_SHOT_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(out) / f"{name}.png"))


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    _fresh()
    _seed_many(open_n=24, closed_n=230)
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


def _open(live, theme: str, width: int, query: str = "", reduced: bool = False):
    base, browser = live
    ctx = browser.new_context(
        viewport={"width": width, "height": 900}, color_scheme=theme,
        reduced_motion="reduce" if reduced else "no-preference",
    )
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/pursuits?track=employment{query}")
    page.wait_for_selector('[data-testid="pcard"], [data-testid="prow"]')
    wait_for_layout(page)
    return ctx, page, errors


def _no_overlap(page, theme, width) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
    for sel in (".ap-bar", ".ap-foot", ".ap-c1", ".ap-pager"):
        for group in page.query_selector_all(sel):
            if not group.is_visible():
                continue
            kids = page.evaluate(_KIDS, group)
            for i, a in enumerate(kids):
                for b in kids[i + 1:]:
                    assert not _overlaps(a, b), (sel, a, b, theme, width)
    for col in page.query_selector_all(".ap-cards"):
        kids = page.evaluate(_KIDS, col)
        for i, a in enumerate(kids):
            for b in kids[i + 1:]:
                assert not _overlaps(a, b), ("cards", a, b, theme, width)
    for el in page.query_selector_all(".ap-closedmore, .ap-pager .k-btn"):
        if el.is_visible():
            lines = page.evaluate(
                "el => Math.round(el.getBoundingClientRect().height / "
                "parseFloat(getComputedStyle(el).lineHeight || 16))", el)
            assert lines <= 2, el.inner_text()


@pytest.mark.parametrize("theme,width", [("light", 1440), ("dark", 1440), ("light", 420)])
def test_show_more_loads_the_rest_in_place_and_nothing_overlaps(live, theme, width):
    ctx, page, errors = _open(live, theme, width, "&open=closed")
    try:
        assert page.locator('[data-testid="pcard"][data-folded="1"]').count() == 100
        more = page.locator('[data-testid="closed-more"]')
        assert more.count() == 1 and more.is_visible()
        # The header and the chips tell the whole truth before anything more is loaded.
        assert page.locator('.ap-cgh span').first.inner_text() == "Closed · 230"
        assert page.locator('[data-testid="filter-count"]').inner_text() == "Showing 254 of 254"
        assert page.locator('[data-chip="all"] .k-chip__n').inner_text() == "254"
        _no_overlap(page, theme, width)
        more.scroll_into_view_if_needed()
        page.evaluate("window.scrollBy(0, 220)")  # the button with the cards above it
        wait_for_layout(page)
        _shot(page, f"closed-held-{theme}-{width}")
        more.click()
        # One page a press (D-PERF-4): the next 100, then a button for the last 30.
        page.wait_for_function(
            "document.querySelectorAll('[data-testid=\"pcard\"][data-folded=\"1\"]').length === 200"
        )
        wait_for_layout(page)
        more = page.locator('[data-testid="closed-more"]')
        assert more.count() == 1 and more.inner_text() == "Show 30 more closed"
        assert page.locator('[data-testid="filter-count"]').inner_text() == "Showing 254 of 254"
        assert page.locator('[data-chip="all"] .k-chip__n').inner_text() == "254"
        _no_overlap(page, theme, width)
        more.scroll_into_view_if_needed()
        page.evaluate("window.scrollBy(0, 220)")
        wait_for_layout(page)
        _shot(page, f"closed-page2-{theme}-{width}")
        more.click()
        page.wait_for_function(
            "document.querySelectorAll('[data-testid=\"pcard\"][data-folded=\"1\"]').length === 230"
        )
        wait_for_layout(page)
        assert page.locator('[data-testid="closed-more"]').count() == 0
        assert page.locator('.ap-cgh span').first.inner_text() == "Closed · 230"
        assert page.locator('[data-testid="filter-count"]').inner_text() == "Showing 254 of 254"
        assert page.locator('[data-chip="all"] .k-chip__n').inner_text() == "254"
        _no_overlap(page, theme, width)
        page.evaluate("window.scrollBy(0, 400)")
        wait_for_layout(page)
        _shot(page, f"closed-all-{theme}-{width}")
        assert not errors, errors
    finally:
        ctx.close()


def test_show_more_is_animated_and_reduced_motion_turns_it_off(live):
    ctx, page, _errors = _open(live, "light", 1440, "&open=closed")
    try:
        css = page.evaluate("""() => {
            const probe = document.createElement('article');
            probe.className = 'ap-card htmx-added';
            document.body.appendChild(probe);
            const v = getComputedStyle(probe).animationName;
            probe.remove();
            return v; }""")
        assert css == "ap-arrive"
    finally:
        ctx.close()
    ctx, page, _errors = _open(live, "light", 1440, "&open=closed", reduced=True)
    try:
        css = page.evaluate("""() => {
            const probe = document.createElement('article');
            probe.className = 'ap-card htmx-added';
            document.body.appendChild(probe);
            const v = getComputedStyle(probe).animationName;
            probe.remove();
            return v; }""")
        assert css == "none"
    finally:
        ctx.close()


def test_a_search_with_unloaded_closed_cards_speaks_only_for_the_cards_that_are_here(live):
    ctx, page, _errors = _open(live, "light", 1440, "&open=closed")
    try:
        page.fill('[data-testid="pursuit-search"]', "Role 0000")
        wait_for_layout(page)
        shown = page.locator('[data-testid="filter-count"]').inner_text()
        # Narrowed: counted over the loaded cards only, so it cannot claim the unloaded ones.
        assert shown.startswith("Showing 1 of ") and int(shown.rsplit(" ", 1)[1]) == 124
    finally:
        ctx.close()


@pytest.mark.parametrize("theme,width", [("light", 1440), ("dark", 1440), ("light", 420)])
def test_table_pages_and_keeps_its_order(live, theme, width):
    ctx, page, errors = _open(live, theme, width, "&view=table")
    try:
        assert page.locator('[data-testid="prow"]').count() == 100
        pager = page.locator('[data-testid="table-page"]')
        assert pager.inner_text().startswith("Page 1 of 3")
        # A page of rows still counts everything: the chips and the line are the server's totals.
        assert page.locator('[data-chip="all"] .k-chip__n').inner_text() == "254"
        assert page.locator('[data-chip="closed"] .k-chip__n').inner_text() == "230"
        assert page.locator('[data-testid="filter-count"]').inner_text() == "Showing 100 of 254"
        _no_overlap(page, theme, width)
        _shot(page, f"table-p1-{theme}-{width}")
        page.locator('[data-testid="table-next"]').click()
        page.wait_for_function(
            "document.querySelector('[data-testid=\"table-page\"]').textContent"
            ".startsWith('Page 2 of 3')"
        )
        wait_for_layout(page)
        assert page.locator('[data-testid="prow"]').count() == 100
        _no_overlap(page, theme, width)
        _shot(page, f"table-p2-{theme}-{width}")
        assert not errors, errors
    finally:
        ctx.close()


# ---------------------------------------------- search and filters cover every application
def _off_loop(fn):
    """The sync browser owns this thread's event loop; the database helpers run in another."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(1) as pool:
        return pool.submit(fn).result()


def _titles() -> dict[int, str]:
    from tests.test_perf3_board import _titles as titles

    return _off_loop(titles)


def _typed(page, text: str) -> None:
    page.fill('[data-testid="pursuit-search"]', text)
    # The page filters what it holds at once; the server's answer then redraws the board for
    # exactly this search (its record of that is data-fkey).
    page.wait_for_function(
        "t => document.querySelector('#apps-board').getAttribute('data-fkey').indexOf(t) > 0",
        arg=text)
    wait_for_layout(page)


@pytest.mark.parametrize("theme,width", [("light", 1440), ("dark", 1440), ("light", 420)])
def test_board_search_finds_a_closed_card_the_page_had_not_loaded(live, theme, width):
    ctx, page, errors = _open(live, theme, width, "&open=closed")
    try:
        drawn = set(page.eval_on_selector_all(
            '[data-testid="pcard"]', "els => els.map(e => e.dataset.id)"))
        titles = _titles()
        hidden = next(pid for pid in titles if str(pid) not in drawn)
        wanted = titles[hidden].lower()
        assert page.locator('[data-testid="pcard"]', has_text=titles[hidden]).count() == 0
        _typed(page, wanted)
        card = page.locator(f'[data-testid="pcard"][data-id="{hidden}"]')
        assert card.count() == 1 and card.is_visible()
        assert page.locator('[data-testid="filter-count"]').inner_text() == "Showing 1 of 1"
        assert page.locator('[data-chip="all"] .k-chip__n').inner_text() == "1"
        assert page.locator('[data-testid="closed-more"]').count() == 0
        _no_overlap(page, theme, width)
        card.scroll_into_view_if_needed()
        wait_for_layout(page)
        _shot(page, f"search-board-{theme}-{width}")
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme,width", [("light", 1440), ("dark", 1440), ("light", 420)])
def test_table_search_pages_the_matches_and_finds_a_row_from_page_two(live, theme, width):
    ctx, page, errors = _open(live, theme, width, "&view=table")
    try:
        page.locator('[data-testid="table-next"]').click()
        page.wait_for_function(
            "document.querySelector('[data-testid=\"table-page\"]').textContent"
            ".startsWith('Page 2 of 3')")
        wait_for_layout(page)
        row_id = page.eval_on_selector('[data-testid="prow"]', "e => e.dataset.id")
        wanted = _titles()[int(row_id)].lower()
        _typed(page, wanted)
        assert page.locator(f'[data-testid="prow"][data-id="{row_id}"]').count() == 1
        assert page.locator('[data-testid="prow"]').count() == 1
        assert page.locator('[data-testid="filter-count"]').inner_text() == "Showing 1 of 1"
        _no_overlap(page, theme, width)
        _shot(page, f"search-table-{theme}-{width}")
        # Many matches: the pager pages the matches and says so.
        _typed(page, "role 01")
        pager = page.locator('[data-testid="table-page"]')
        n = len([t for t in _titles().values() if all(w in t.lower() for w in ["role", "01"])])
        assert n > 100  # the seed makes this a paged result
        assert f"of {n} matching" in pager.inner_text()
        assert not errors, errors
    finally:
        ctx.close()


def test_a_filter_runs_on_the_server_too(live):
    ctx, page, _errors = _open(live, "light", 1440, "&open=closed")
    try:
        titles = _titles()
        drawn = set(page.eval_on_selector_all(
            '[data-testid="pcard"]', "els => els.map(e => e.dataset.id)"))
        hidden = next(pid for pid in titles if str(pid) not in drawn)
        from sqlalchemy import update

        from kestrel.db.models import Pursuit
        from tests.test_perf3_board import _run

        async def mark(s):
            await s.execute(update(Pursuit).where(Pursuit.id == hidden).values(priority=1))
            await s.commit()

        _off_loop(lambda: _run(mark))
        page.reload()
        page.wait_for_selector('[data-testid="pcard"]')
        page.click("#ap-mf-btn")
        page.select_option('[data-f="pri"]', "1")
        page.wait_for_function(
            "document.querySelector('#apps-board').getAttribute('data-fkey')"
            ".indexOf('\"1\"') > 0")
        wait_for_layout(page)
        assert page.locator(f'[data-testid="pcard"][data-id="{hidden}"]').count() == 1
        assert page.locator('[data-testid="filter-count"]').inner_text() == "Showing 1 of 1"
    finally:
        ctx.close()
