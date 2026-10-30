"""The shared shell below the desktop width (D-UI-91), in a real browser: under 1100 px the
sidebar is an icon rail that never disappears; on a phone it gives way to the bottom bar and its
More sheet. Converted pages load shell.css, so this guards that sheet, not kestrel.css.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from tests import today_seed
from tests.browser import LINES_JS, launch_sync, wait_for_layout

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

_PAGES = ["/", "/opportunities", "/settings", "/models", "/help"]


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    asyncio.run(today_seed.seed())
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


def _open(live, path: str, width: int, theme: str = "light"):
    base, browser = live
    ctx = browser.new_context(viewport={"width": width, "height": 800}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}{path}")
    page.wait_for_selector("h1, .k-scene__title")
    page.wait_for_load_state("networkidle")
    wait_for_layout(page)
    return ctx, page, errors


def _settled(page) -> None:
    page.evaluate("document.fonts.ready")
    page.wait_for_function("""() => document.getAnimations().every(a =>
      a.effect.getTiming().iterations === Infinity ||
      a.playState === 'finished' || a.playState === 'idle')""")


def _shot(page, name: str) -> None:
    if out := os.environ.get("KESTREL_SHOT_DIR"):
        Path(out).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(out) / f"narrow-{name}.png"), full_page=True)


def _controls_fit(page, selector: str, *, single_line: bool = True) -> None:
    for control in page.locator(selector).all():
        if not control.is_visible():
            continue
        box = control.bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= page.viewport_size["width"] + 1
        assert control.evaluate("el => el.scrollWidth <= el.clientWidth + 1"), control.inner_text()
        if single_line:
            assert control.evaluate(LINES_JS) <= 1, control.inner_text()


def _no_sideways_scroll(page) -> None:
    wide = page.evaluate("""() => document.documentElement.scrollWidth <= innerWidth ? [] :
      [...document.querySelectorAll('.k-win *')].filter(e => !e.closest('svg,[hidden],.k-flyout,'
        + '.k-slideover,.k-modal') && e.getBoundingClientRect().right > innerWidth + 1)
        .map(e => e.tagName + '.' + e.className + ' ' + Math.round(e.getBoundingClientRect().right))
        .slice(0, 6)""")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True, wide


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("path", _PAGES)
def test_below_1100_the_sidebar_is_an_icon_rail_and_every_page_is_reachable(live, path, theme):
    ctx, page, errors = _open(live, path, 1000, theme)
    try:
        _no_sideways_scroll(page)
        side = page.locator("#sidebar").bounding_box()
        assert side and 40 <= side["width"] <= 80, side  # a rail, not the 232 px sidebar
        assert page.locator(".side .k-brand__word").is_hidden()
        assert page.locator(".side .k-sec").first.is_hidden()
        assert page.locator(".side .track-switch").count() == 0 or \
            page.locator(".side .track-switch").is_hidden()
        links = page.locator("nav.mainnav a")
        shown = [a for a in links.all() if a.is_visible()]
        assert len(shown) >= 10, len(shown)  # every page still has its icon
        for a in shown:
            box = a.bounding_box()
            assert box and box["width"] > 20 and box["x"] >= 0 and box["x"] + box["width"] <= 80
        # Below 1100 only the sidebar collapses: the search keeps its box and its text
        # (today-mockup-locked.html :37-42).
        assert page.locator(".k-search .k-search__text").is_visible()
        box = page.locator(".k-search").bounding_box()
        assert box and box["width"] >= 300, box
        assert page.locator(".bottombar").is_hidden()
        # An icon is a real link: it goes where the page list says.
        target = page.locator('nav.mainnav a[href="/settings"]').first
        target.click()
        page.wait_for_url("**/settings*")
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [400, 1000, 1280, 1680])
def test_today_content_and_carousel_controls_fit(live, width, theme):
    from playwright.sync_api import expect

    ctx, page, errors = _open(live, "/", width, theme)
    try:
        expect(page.locator('[data-testid="td-card"]').first).to_be_visible()
        _no_sideways_scroll(page)
        _controls_fit(page, ".td-head h3", single_line=width > 700)
        _controls_fit(page, ".td-arrows a, .td-arrows button, .td-panel .k-btn")
        title = page.locator("#td-mh").bounding_box()
        arrows = page.locator(".td-arrows").bounding_box()
        assert title and arrows
        assert title["y"] + title["height"] <= arrows["y"] + 1 or \
            title["x"] + title["width"] <= arrows["x"] + 1
        _shot(page, f"today-{width}-{theme}")
        if width <= 1000:
            track = page.locator("#td-track")
            before = track.evaluate("el => getComputedStyle(el).transform")
            page.get_by_role("button", name="Next matches", exact=True).click()
            expect(track).not_to_have_css("transform", before)
            _settled(page)
            _no_sideways_scroll(page)
            page.get_by_role("button", name="Previous matches", exact=True).click()
            _settled(page)
            _no_sideways_scroll(page)
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [400, 1000, 1280, 1680])
def test_every_settings_section_fits(live, width, theme):
    from playwright.sync_api import expect

    ctx, page, errors = _open(live, "/settings", width, theme)
    try:
        for section in ("computer", "roles", "scoring", "privacy", "data", "preferences"):
            page.get_by_test_id(f"st-nav-{section}").click()
            pane = page.locator(f"#st-sec-{section}")
            expect(pane).to_be_visible()
            expect(pane.locator(".st-card").first).to_be_visible()
            _settled(page)
            _no_sideways_scroll(page)
            _controls_fit(page, f"#st-sec-{section} :is(.k-btn, .k-link, select), .st-si")
            _shot(page, f"settings-{section}-{width}-{theme}")
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [400, 1000, 1280, 1680])
def test_help_steps_and_expanded_tables_fit(live, width, theme):
    from playwright.sync_api import expect

    ctx, page, errors = _open(live, "/help", width, theme)
    try:
        _no_sideways_scroll(page)
        _controls_fit(page, ".hp-step b, .hp-step__cost")
        # The long references are articles now (help_ref_*.html), opened by their own link, not
        # folds: each still fits with no sideways scroll.
        for article in ("tier-names", "sources", "lan"):
            page.goto(page.url.split("#")[0] + "#" + article)
            expect(page.locator(f"#{article}").first).to_be_attached()
            _no_sideways_scroll(page)
        # The sources table, as the reader shows it: at phone width it scrolls inside itself.
        page.goto(page.url.split("#")[0] + "#sources")
        reader = page.get_by_test_id("hp-reader-body")
        scroller = reader.locator(".hp-scroll").first
        if width == 400:
            assert scroller.evaluate("el => el.scrollWidth > el.clientWidth")
            scroller.evaluate("el => { el.scrollLeft = el.scrollWidth; }")
            assert scroller.evaluate("el => el.scrollLeft > 0")
            last_cell = reader.locator(".hp-src tbody tr").last.locator("td").last
            bounds = last_cell.bounding_box()
            assert bounds and bounds["x"] + bounds["width"] <= width + 1
            assert page.evaluate("scrollX") == 0
        _shot(page, f"help-{width}-{theme}")
        assert not errors, errors
    finally:
        ctx.close()


def test_at_1200_the_full_sidebar_is_still_there(live):
    ctx, page, _ = _open(live, "/", 1200)
    try:
        assert page.locator(".side .k-brand__word").is_visible()
        assert page.locator("#sidebar").bounding_box()["width"] > 200
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("path", _PAGES)
def test_on_a_phone_the_bottom_bar_replaces_the_rail_and_more_lists_every_page(live, path, theme):
    ctx, page, errors = _open(live, path, 400, theme)
    try:
        _no_sideways_scroll(page)
        assert page.locator("#sidebar").is_hidden()
        bar = page.locator(".bottombar")
        assert bar.is_visible()
        box = bar.bounding_box()
        assert box and abs(box["y"] + box["height"] - 800) < 2, box  # pinned to the bottom
        assert box["x"] >= 0 and box["x"] + box["width"] <= 400 + 1
        # Content is not hidden under the bar: the container leaves room for it.
        pad = page.evaluate(
            "parseFloat(getComputedStyle(document.querySelector('main.container')).paddingBottom)")
        assert pad >= box["height"] - 2, (pad, box)
        # More opens the sheet with every page; a link there is reachable and works.
        page.locator(".bottombar button", has_text="More").click()
        page.wait_for_selector(".sheet nav.mainnav a")
        links = [a for a in page.locator(".sheet nav.mainnav a").all() if a.is_visible()]
        assert len(links) >= 10, len(links)
        page.locator('.sheet nav.mainnav a[href="/settings"]').click()
        page.wait_for_url("**/settings*")
        assert not errors, errors
    finally:
        ctx.close()
