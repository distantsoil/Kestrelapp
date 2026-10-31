"""Calibrate in a real browser (UI overhaul parity check against the locked mockup,
the locked design mockup (kept privately)). Light and dark at 1280 and 1680: no
overlap, no wrapping titles or buttons, no horizontal scroll; then the interactions the mockup
demos (answer first, keys 1 to 5, Save advancing in place, Skip with Undo, Change from history).

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1. The browser is the Edge
already on the machine, else a Playwright Chromium (tests/browser.py)."""

from __future__ import annotations

import os

import pytest

from tests.browser import (
    LINES_JS,
    TIMER_PROBE_JS,
    assert_page_remounts_clean,
    launch_sync,
    wait_for_layout,
)
from tests.test_calibrate_page import _seed

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

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


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    _seed(n_roles=14, n_labeled=9)
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
    page.goto(f"{base}/calibrate")
    page.wait_for_selector('[data-testid="cal-card"] .cal-q')
    wait_for_layout(page)
    return ctx, page, errors


def test_train_button_explains_the_rating_floor_in_place(live):
    from playwright.sync_api import expect

    ctx, page, errors = _page(live, "light", 1280)
    try:
        # Training lives in the Your sorting model flyout, opened from the taste strip.
        page.click('[data-testid="cal-taste"]')
        page.wait_for_selector("#cal-model-fly.is-open")
        page.click('[data-testid="cal-train"]')
        expect(page.locator('#cal-training [role="status"]')).to_contain_text("Nothing queued")
        expect(page.locator('#cal-training [role="status"]')).to_contain_text("ratings so far")
        expect(page.locator('[data-testid="cal-card"]')).to_be_visible()
        assert not errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_no_overlap_no_wrapping_no_sideways_scroll(live, theme, width):
    ctx, page, errors = _page(live, theme, width)
    try:
        page.click('[data-testid="pick-4"]')  # the answered state is the widest one
        page.wait_for_selector('[data-testid="cal-reveal"]')
        page.click("#cal-whybtn")
        page.wait_for_selector("#cal-why-panel")
        wait_for_layout(page)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        assert (
            page.evaluate(
                "document.querySelector('.main, main').scrollWidth <= "
                "document.querySelector('.main, main').clientWidth + 1"
            )
            is True
        )
        for sel in (
            "h1",
            ".k-pill",
            ".cal-q .k-btn",
            ".cal-pickbtn",
            ".cal-pq",
            ".cal-big",
            ".cal-strong",
            ".cal-qtitle h2",
            ".cal-cmp .k-pill",
        ):
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    assert page.evaluate(_LINES, el) <= 1, (sel, theme, width, el.inner_text()[:40])
        for container in (".cal-prog", ".cal-qhead", ".cal-act", ".cal-pick", ".cal-ringrow"):
            for group in page.query_selector_all(container):
                if not group.is_visible():
                    continue
                kids = page.evaluate(_KIDS, group)
                for i, a in enumerate(kids):
                    for b in kids[i + 1 :]:
                        assert not _overlaps(a, b), (container, a, b, theme, width)
        card = page.evaluate(_BOXES, ".cal-q")[0]
        for h in page.evaluate(_BOXES, ".k-hcard"):
            assert not _overlaps(card, h), (card, h)
        help_col = page.evaluate(_BOXES, ".k-help-col")[0]
        if width >= 1680:
            assert help_col["l"] >= card["r"] - 1 and help_col["tp"] <= card["tp"] + 2
        else:
            assert help_col["tp"] >= card["bt"] - 1
        assert not page.query_selector("#announcement-slot")
        # The mockup keeps "You approve any change." under Scoring tune-up at 1280 too.
        approve = page.get_by_text("You approve any change.")
        assert approve.count() == 1 and approve.is_visible(), (theme, width)
        assert not errors, errors
    finally:
        ctx.close()


def _card_settled(page):
    """htmx binds a swapped-in card's buttons when it settles, a beat after the swap. A shortcut
    pressed in between clicks a button htmx has not bound yet and nothing is sent. A person cannot
    press that fast; a test driving the keyboard can, so it waits for the card to settle."""
    page.wait_for_selector("#cal-card:not(.htmx-swapping):not(.htmx-settling)")


def test_answer_first_keys_save_skip_undo_and_change(live):
    ctx, page, errors = _page(live, "light", 1680)
    try:
        assert page.query_selector('[data-testid="cal-veil"]')
        assert not page.query_selector('[data-testid="cal-reveal"]')
        assert page.is_disabled("#cal-save")
        first = page.get_attribute('[data-testid^="calitem-"]', "data-testid")
        page.keyboard.press("4")  # key 4 is Good
        page.wait_for_selector('[data-testid="cal-reveal"]')
        assert "You said Good" in page.inner_text('[data-testid="cal-reveal"]')
        assert page.is_enabled("#cal-save")
        page.keyboard.press("o")
        page.wait_for_selector("#cal-why-panel", state="visible")  # grid-rows transition opens it
        page.click(".cal-chip >> nth=0")
        assert page.inner_text("#cal-savel") == "Save with reasons"
        page.keyboard.press("Enter")
        page.wait_for_function(
            "first => !document.querySelector('[data-testid=\"' + first + '\"]')", arg=first
        )
        assert page.query_selector('[data-testid="cal-veil"]')  # the next role starts hidden
        _card_settled(page)
        shown = page.get_attribute('[data-testid^="calitem-"]', "data-testid")
        page.keyboard.press("s")
        page.wait_for_function(
            "s => !document.querySelector('[data-testid=\"' + s + '\"]')", arg=shown
        )
        page.click(".k-toast__undo >> nth=-1")  # the Skip toast, not the earlier Save one
        page.wait_for_selector(f'[data-testid="{shown}"]')
        # Change from the history loads the role into the card, in place.
        page.click('[data-testid^="change-"] >> nth=0')
        page.wait_for_selector('[data-testid="cal-revisit"]')
        assert page.query_selector('[data-testid="cal-reveal"]')
        page.click('[data-testid="cal-revisit-cancel"]')
        page.wait_for_selector('[data-testid="cal-veil"]')
        assert not errors, errors
    finally:
        ctx.close()


def test_the_page_mounts_unmounts_and_mounts_again_with_one_set_of_listeners(live):
    """The page lifecycle (D-UI-210): Calibrate unmounted and mounted again in place starts exactly
    what it started on the document load and leaves nothing behind. Mount syncs the Save button
    itself (the old start waited for DOMContentLoaded, which a page mounted in place never sees).
    After it, one O opens the Tell Kestrel why panel (a doubled listener toggles it twice, so it
    stays shut), one nudge click moves one step, and an unmounted page answers no key."""
    base, browser = live
    ctx = browser.new_context(viewport={"width": 1680, "height": 900}, color_scheme="light")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(TIMER_PROBE_JS)
    try:
        page.goto(f"{base}/calibrate")
        page.wait_for_selector('[data-testid="cal-card"] .cal-q')
        wait_for_layout(page)
        first = assert_page_remounts_clean(
            page, "calibrate", "calibrate.js", settle_ms=400, quiet_ms=600)
        assert first["timers"] == 0 and first["observers"] == 0
        assert page.is_disabled("#cal-save")
        # Mount runs syncSave: an answer already on the form enables Save with no event at all.
        page.evaluate("""() => { document.querySelector('#cal-form').elements.score.value = '3';
          KUI.page.mount(); }""")
        assert page.is_enabled("#cal-save")
        page.evaluate("document.querySelector('#cal-form').elements.score.value = ''")
        page.evaluate("KUI.page.mount()")
        assert page.is_disabled("#cal-save")
        page.keyboard.press("4")
        page.wait_for_selector('[data-testid="cal-reveal"]')
        assert page.is_enabled("#cal-save")
        page.keyboard.press("o")
        page.wait_for_selector("#cal-why-panel.is-open", state="attached")
        assert page.get_attribute("#cal-whybtn", "aria-expanded") == "true"
        page.wait_for_selector(".cal-nud button")
        row = page.query_selector(".cal-nud")
        base_score = int(row.get_attribute("data-base"))
        step = 1 if base_score < 5 else -1
        row.query_selector(f"button[data-nd='{step}']").click()
        assert row.query_selector("input").input_value() == str(step)  # one click, one step
        assert row.query_selector("b").inner_text() == str(base_score + step)
        # Unmounted, the keys do nothing: the panel stays as it was.
        page.evaluate("KUI.page.unmount()")
        page.keyboard.press("o")
        page.wait_for_timeout(300)
        assert page.get_attribute("#cal-whybtn", "aria-expanded") == "true"
        assert not errors, errors
    finally:
        ctx.close()
