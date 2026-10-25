"""Tailor resume in a real browser (parity with the locked design mockup (kept privately)).
Light and dark at 1280 and 1680: no overlap, no wrapped titles or buttons, no horizontal scroll;
then the interactions: a swap changes the card and the preview in place (the frame is never
reloaded), hover toggles attributes without re-rendering, and a line that needs a pick opens the
confirm before Download.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import os

import pytest

from tests import tailor_seed
from tests.browser import LINES_JS, launch_sync, wait_for_layout

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

_LINES = LINES_JS


def _serve(config):
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    ids = tailor_seed.seed(config=config)
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, ids
        browser.close()


@pytest.fixture
def live():
    yield from _serve(None)


@pytest.fixture
def live_needs():
    yield from _serve({"needs_pick": {"m_review": "alt_3"}})


def _open(live, theme: str, width: int):
    base, browser, ids = live
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/renders/{ids['render']}")
    page.wait_for_selector("#tl-frame")
    page.wait_for_function("document.querySelector('#tl-frame').contentDocument.body")
    wait_for_layout(page)
    return ctx, page, errors


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_no_wrapping_no_sideways_scroll_and_the_page_scales_to_its_column(live, theme, width):
    ctx, page, errors = _open(live, theme, width)
    try:
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        for sel in ("h1", ".tl-head__acts .k-btn", ".tl-ptabs .k-btn", ".tl-segx button",
                    ".tl-badge"):
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    assert page.evaluate(_LINES, el) <= 1, (sel, theme, width, el.inner_text())
        wrap = page.evaluate("""() => {
            const w = document.querySelector('#tl-pwrap').getBoundingClientRect();
            const s = document.querySelector('[data-tl-scale]').getBoundingClientRect();
            return {w: w.width, s: s.width}; }""")
        assert wrap["s"] <= wrap["w"] + 1  # the scaled true page fits its frame
        assert not errors, errors
    finally:
        ctx.close()


def test_a_swap_updates_in_place_and_never_reloads_the_preview_frame(live):
    ctx, page, errors = _open(live, "light", 1680)
    try:
        page.evaluate("document.querySelector('#tl-frame').contentWindow.__kept = 1")
        page.evaluate("window.__same = 1")
        page.click('#tl-card-m_throughput [data-tl-more]')
        page.click('#tl-card-m_throughput label:has-text("Raised throughput")')
        page.wait_for_selector('#tl-card-m_throughput[data-state="you"]')
        assert page.evaluate("window.__same") == 1
        assert page.evaluate("document.querySelector('#tl-frame').contentWindow.__kept") == 1
        assert "Raised throughput 22%" in page.evaluate(
            "document.querySelector('#tl-frame').contentDocument.body.innerText")
        # The card stays open after the swap.
        assert "is-open" in page.get_attribute("#tl-card-m_throughput", "class")
        page.wait_for_selector(".k-toast")
        page.click(".k-toast__undo")
        page.wait_for_selector('#tl-card-m_throughput[data-state="picked"], '
                               '#tl-card-m_throughput[data-state="first"]')
        assert not errors, errors
    finally:
        ctx.close()


def test_hover_links_a_card_its_printed_line_and_what_the_role_asks_for(live):
    ctx, page, errors = _open(live, "light", 1680)
    try:
        page.hover('.tl-req[data-req="m_throughput"]')
        page.wait_for_function(
            "document.querySelector('#tl-frame').contentDocument"
            ".querySelector('[data-slot=\"m_throughput\"][data-hl]')")
        assert page.get_attribute('#tl-card-m_throughput', "data-hot") is not None
        page.hover("h1")
        page.wait_for_function(
            "!document.querySelector('#tl-frame').contentDocument.querySelector('[data-hl]')")
        assert not errors, errors
    finally:
        ctx.close()


def test_download_asks_first_while_a_line_needs_a_pick(live_needs):
    ctx, page, errors = _open(live_needs, "light", 1280)
    try:
        assert "is-err" in page.get_attribute("#tl-card-m_review", "class")
        page.click('[data-testid="tl-download"]')
        page.wait_for_selector("#tl-gate.is-open, #tl-gate[aria-hidden=\"false\"]")
        text = page.inner_text("#tl-gate")
        assert "1 line still needs your pick." in text
        assert "Kestrel used your first wording there. Download anyway?" in text
        page.click('[data-testid="tl-choose"]')
        page.wait_for_selector('#tl-gate[aria-hidden="true"]', state="attached")
        assert not errors, errors
    finally:
        ctx.close()


def test_the_posting_slide_over_opens_centered_and_closes_on_escape(live):
    ctx, page, errors = _open(live, "light", 1680)
    try:
        page.click('[data-testid="tl-view-posting"]')
        page.wait_for_selector("#tl-post[aria-hidden=\"false\"]")
        wait_for_layout(page)
        box = page.evaluate("""() => { const r = document.querySelector('#tl-post')
            .getBoundingClientRect(); return {tp: r.top, bt: r.bottom, h: innerHeight}; }""")
        assert abs((box["tp"] + box["bt"]) / 2 - box["h"] / 2) < 8
        page.keyboard.press("Escape")
        page.wait_for_selector("#tl-post[aria-hidden=\"true\"]", state="attached")
        assert not errors, errors
    finally:
        ctx.close()


def test_download_is_fetched_so_an_error_never_replaces_the_page(live):
    ctx, page, errors = _open(live, "light", 1680)
    try:
        url = page.url
        page.evaluate("window.__same = 1")
        # The server cannot make the PDF: the person sees a message and stays on this page.
        page.route("**/renders/*/download*", lambda r: r.fulfill(
            status=500, content_type="text/html", body="<p>No browser to make the PDF.</p>"))
        page.click('[data-testid="tl-download"]')
        page.wait_for_selector(".k-toast")
        assert "No browser to make the PDF." in page.inner_text(".k-toast")
        assert page.url == url and page.evaluate("window.__same") == 1
        # The server makes it: the browser saves a file under the suggested name.
        page.unroute("**/renders/*/download*")
        page.route("**/renders/*/download*", lambda r: r.fulfill(
            status=200, content_type="application/pdf", body=b"%PDF-1.4 test"))
        name = page.get_attribute('[data-testid="tl-download"]', "data-name")
        with page.expect_download() as info:
            page.click('[data-testid="tl-download"]')
        assert info.value.suggested_filename == (name or "resume.pdf")
        assert page.url == url and page.evaluate("window.__same") == 1
        assert not errors, errors
    finally:
        ctx.close()
