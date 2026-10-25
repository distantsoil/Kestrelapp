"""Settings in a real browser (UI overhaul parity check against the locked mockup,
the locked design mockup (kept privately)). Light and dark at 1280 and 1680, every
section: no overlap, no wrapped titles or buttons, no sideways scroll; then the interactions the
mockup demos (section routing and old links, a control saving with Undo, the rule list, the scene
manager and the confirm before access opens).

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1. The browser is the Edge
already on the machine, else a Playwright Chromium (tests/browser.py)."""

from __future__ import annotations

import os

import pytest

from tests.browser import launch_sync, wait_for_layout

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("KESTREL_LIVE_BROWSER") != "1",
        reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
    ),
    pytest.mark.usefixtures("_isolate_data_home"),
]

SECTIONS = ("computer", "roles", "scoring", "privacy", "data", "preferences")

_LINES = """el => {
  const tops = new Set();
  const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    if (!n.textContent.trim()) continue;
    const r = document.createRange(); r.selectNodeContents(n);
    for (const x of r.getClientRects()) if (x.width > 0) tops.add(Math.round(x.bottom / 8));
  }
  return tops.size;
}"""

_KIDS = """el => Array.from(el.children).filter(e => e.offsetParent !== null)
  .map(e => { const b = e.getBoundingClientRect();
    return {t: (e.textContent || '').trim().slice(0, 30), l: b.left, r: b.right, tp: b.top,
            bt: b.bottom}; })"""


def _overlaps(a, b) -> bool:
    return a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1 and a["tp"] < b["bt"] - 1 \
        and b["tp"] < a["bt"] - 1


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.db import migrate
    from kestrel.web.serve import running_server

    migrate.run_migrations()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


def _page(live, theme: str, width: int, hash_: str = ""):
    base, browser = live
    ctx = browser.new_context(viewport={"width": width, "height": 1000}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/settings{hash_}")
    page.wait_for_selector('[data-testid="st-nav-computer"]')
    wait_for_layout(page)
    return ctx, page, errors


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_no_overlap_no_wrapping_no_sideways_scroll_in_every_section(live, theme, width):
    ctx, page, errors = _page(live, theme, width)
    try:
        for sec in SECTIONS:
            page.evaluate(f"location.hash = '#{sec}'")
            page.wait_for_selector(f'#st-sec-{sec}:not([hidden]) .st-panebody')
            wait_for_layout(page)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True, sec
            assert page.evaluate("document.querySelector('.k-main, main').scrollWidth <= "
                                 "document.querySelector('.k-main, main').clientWidth + 1"), sec
            for sel in ("h1", ".st-head h3", ".st-card .k-btn", ".st-si", ".st-mc__t b",
                        ".st-card .k-pill", ".st-card .k-seg button"):
                for el in page.query_selector_all(f"#st-sec-{sec} {sel}" if sel != "h1"
                                                  and sel != ".st-si" else sel):
                    if el.is_visible():
                        assert page.evaluate(_LINES, el) <= 1, (sec, sel, theme, width,
                                                                el.inner_text()[:40])
            for container in (f"#st-sec-{sec} .st-row", f"#st-sec-{sec} .st-modes",
                              f"#st-sec-{sec} .st-hact", f"#st-sec-{sec} .st-cs",
                              ".st-lay"):
                for group in page.query_selector_all(container):
                    if not group.is_visible():
                        continue
                    kids = page.evaluate(_KIDS, group)
                    for i, a in enumerate(kids):
                        for b in kids[i + 1:]:
                            assert not _overlaps(a, b), (sec, container, a, b, theme, width)
        assert not page.query_selector("#announcement-slot")
        assert not errors, errors
    finally:
        ctx.close()


def test_old_links_open_the_section_that_holds_them_now(live):
    for old, want in (("#system", "computer"), ("#applied", "data"), ("#your-data", "data"),
                      ("#appearance", "preferences"), ("#learning", "scoring"),
                      ("#lan", "privacy")):
        ctx, page, errors = _page(live, "light", 1680, old)
        try:
            page.wait_for_selector(f'#st-sec-{want}:not([hidden])')
            assert page.get_attribute(f'[data-testid="st-nav-{want}"]', "aria-current") == "page"
            assert not errors, errors
        finally:
            ctx.close()


def test_a_switch_saves_with_undo_and_the_rule_list_opens(live):
    ctx, page, errors = _page(live, "light", 1680, "#preferences")
    try:
        page.wait_for_selector('#st-sec-preferences:not([hidden])')
        page.click('[data-st-key="ui.reduce_motion"] + .k-switch__track')
        page.wait_for_selector('.k-toast:has-text("Motion reduced.")')
        page.click('.k-toast__undo')
        page.wait_for_selector('.k-toast:has-text("Put back.")')
        page.evaluate("location.hash = '#roles'")
        page.wait_for_selector('[data-testid="st-rule-switch-visa"]')
        page.click('[data-testid="st-rule-switch-visa"] + .k-switch__track')
        page.wait_for_selector('.k-toast:has-text("is off. Those roles will show again.")')
        assert "Off" in page.inner_text('[data-testid="st-rule-chip-visa"]')
        assert not errors, errors
    finally:
        ctx.close()


def test_the_scene_manager_previews_and_access_asks_first(live):
    ctx, page, errors = _page(live, "dark", 1680, "#preferences")
    try:
        page.wait_for_selector('[data-testid="st-manage-scenes"]')
        page.click('[data-testid="st-manage-scenes"]')
        page.wait_for_selector('#st-mg-scenes.is-open .st-mgrow')
        assert len(page.query_selector_all('#st-mg-scenes .st-mgrow')) == 14
        page.click('#st-mg-scenes .st-mgrow:nth-child(3) .st-mgname')
        assert page.query_selector('#st-mg-scenes #st-mg-band svg')
        box = page.evaluate("""() => { const b = document.querySelector('#st-mg-scenes')
            .getBoundingClientRect(); return {t: b.top, bt: b.bottom, h: innerHeight}; }""")
        assert abs((box["t"] + box["bt"]) / 2 - box["h"] / 2) < 6  # vertically centered
        page.keyboard.press("Escape")
        page.evaluate("location.hash = '#privacy'")
        page.wait_for_selector('[data-testid="st-access-switch"]')
        page.click('[data-testid="st-access-switch"] + .k-switch__track')
        page.wait_for_selector('#st-access-dialog.is-open')
        assert page.is_disabled('#st-access-go')
        page.check('#st-access-ack')
        assert not page.is_disabled('#st-access-go')
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("width", [1280, 1680])
def test_the_three_run_cards_sit_in_one_row_like_the_mockup(live, width):
    ctx, page, errors = _page(live, "light", width, "#computer")
    try:
        page.wait_for_selector('#st-sec-computer:not([hidden]) .st-modes')
        tops = page.evaluate("Array.from(document.querySelectorAll('#st-sec-computer .st-mc'))"
                             ".map(e => Math.round(e.getBoundingClientRect().top))")
        assert len(tops) == 3 and len(set(tops)) == 1, tops
        assert not errors, errors
    finally:
        ctx.close()
