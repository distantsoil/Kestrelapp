"""Applications in a real browser (UI overhaul parity check against the locked mockup,
the locked design mockup (kept privately)). Light and dark at 1280 and 1680: no
overlap, no wrapped titles or buttons, no horizontal scroll; then the interactions the mockup
demos (a stage menu, the panel and its forms, the next-step menus, Undo, the Closed group, the
filters and the sheet that adds a role).

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1. The browser is the Edge
already on the machine, else a Playwright Chromium (tests/browser.py). Set KESTREL_SHOT_DIR to
keep a screenshot of each state for a look."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests import apps_seed
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

_LINES = LINES_JS
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


@pytest.fixture(params=["employment", "consulting"])
def live(request):
    """A server on the test's own throwaway data directory and a browser."""
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    ids = apps_seed.seed(request.param)
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, request.param, ids
        browser.close()


def _page(live, theme: str, width: int, query: str = ""):
    base, browser, track, _ids = live
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/pursuits?track={track}{query}")
    page.wait_for_selector('[data-testid="pcard"]')
    wait_for_layout(page)
    return ctx, page, errors


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_no_overlap_no_wrapping_no_sideways_scroll(live, theme, width):
    ctx, page, errors = _page(live, theme, width)
    try:
        _shot(page, f"apps-{live[2]}-{theme}-{width}")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 0") is True
        assert page.evaluate("document.querySelector('.main, main').scrollWidth <= "
                             "document.querySelector('.main, main').clientWidth + 1") is True
        for sel in ("h1", ".ap-bar .k-btn", ".k-chip", ".ap-colh b", ".ap-stg", ".ap-nx"):
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    assert page.evaluate(_LINES, el) <= 1, (sel, theme, width, el.inner_text()[:40])
        for container in (".ap-bar", ".ap-foot", ".ap-c1", ".ap-ivr"):
            for group in page.query_selector_all(container):
                if not group.is_visible():
                    continue
                kids = page.evaluate(_KIDS, group)
                for i, a in enumerate(kids):
                    for b in kids[i + 1:]:
                        assert not _overlaps(a, b), (container, a, b, theme, width)
        # Cards in one column never overlap each other.
        for col in page.query_selector_all(".ap-cards"):
            kids = page.evaluate(_KIDS, col)
            for i, a in enumerate(kids):
                for b in kids[i + 1:]:
                    assert not _overlaps(a, b), ("cards", a, b)
        # The board and the rail sit side by side on a wide window and stack on a narrow one.
        grid = page.evaluate("""() => {
            const b = document.querySelector('#apps-board').getBoundingClientRect();
            const r = document.querySelector('#apps-rail').getBoundingClientRect();
            return {b: {l: b.left, r: b.right, tp: b.top, bt: b.bottom},
                    r: {l: r.left, r: r.right, tp: r.top, bt: r.bottom}}; }""")
        if grid["r"]["l"] >= grid["b"]["r"] - 1:
            assert grid["r"]["tp"] <= grid["b"]["tp"] + 2
        else:
            assert grid["r"]["tp"] >= grid["b"]["bt"] - 1
        assert not page.query_selector("#announcement-slot")
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("width", [1440, 1000, 800])
def test_every_stage_is_reachable_and_the_board_scrolls_inside_itself(live, width):
    ctx, page, errors = _page(live, "light", width)
    try:
        info = page.evaluate("""() => {
            const g = document.querySelector('[data-testid="apps-board-grid"]');
            const v = g.getBoundingClientRect();
            const cols = Array.from(g.querySelectorAll('[data-testid="apps-col"]'))
              .map(c => c.getBoundingClientRect());
            const last = cols[cols.length - 1];
            const hint = document.querySelector('[data-testid="board-scrollhint"]');
            return {vr: v.right, vl: v.left, lastR: last.right, n: cols.length,
                    scrolls: g.scrollWidth > g.clientWidth + 1,
                    hint: !!hint && getComputedStyle(hint).display !== 'none',
                    page: document.documentElement.scrollWidth <= innerWidth}; }""")
        assert info["n"] == 4 and info["page"], info
        if not info["scrolls"]:
            assert width >= 1000, info
            # Nothing is clipped: the last column ends inside the board's visible area.
            assert info["lastR"] <= info["vr"] + 1, info
        else:
            # Space ran out: the board scrolls inside its own box and says so.
            assert width <= 1000 and info["hint"], info
            page.evaluate("document.querySelector('[data-testid=\"apps-board-grid\"]')"
                          ".scrollLeft = 99999")
            last_r = page.evaluate("""() => {
                const c = document.querySelectorAll('[data-testid="apps-col"]');
                const g = document.querySelector('[data-testid="apps-board-grid"]');
                return [c[c.length - 1].getBoundingClientRect().right,
                        g.getBoundingClientRect().right]; }""")
            assert last_r[0] <= last_r[1] + 1, last_r
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("width", [1000, 1280, 1440, 1680])
def test_the_scene_art_scales_with_the_header_and_clears_the_title(live, width):
    ctx, page, errors = _page(live, "light", width)
    try:
        art = page.evaluate("""() => {
            const a = document.querySelector('.k-scene__art');
            const s = document.querySelector('.k-scene');
            const t = document.querySelector('.k-scene__title');
            if (!a) return null;
            const r = a.getBoundingClientRect(), sb = s.getBoundingClientRect();
            return {w: r.width, h: r.height, sw: sb.width, l: r.left,
                    tr: t.getBoundingClientRect().right,
                    op: parseFloat(getComputedStyle(a).opacity),
                    tz: parseInt(getComputedStyle(t).zIndex) || 0}; }""")
        if art:
            assert 379 <= art["w"] <= 621, art
            assert abs(art["h"] / art["w"] - 118 / 620) < 0.01, art
            if width > 1100:
                assert page.evaluate(TITLE_RIGHT_JS) <= (art_visible_left(page) or 1e9), art
            else:
                # At 1100 px and below the locked mockup dims the art to .35 and lets the title
                # row sit in front of it (applications-mockup-locked.html, .scene{opacity:.35}).
                assert abs(art["op"] - 0.35) < 0.01 and art["tz"] >= 1, art
        assert not errors, errors
    finally:
        ctx.close()


def test_the_panel_slides_over_the_board_centered_and_never_pushes_it(live):
    ctx, page, errors = _page(live, "light", 1280)
    try:
        width_of = "document.querySelector('#apps-board').getBoundingClientRect().width"
        before = page.evaluate(width_of)
        page.click('[data-testid="pcard"]')
        page.wait_for_selector("#ap-panel.is-open")
        wait_for_layout(page)
        _shot(page, f"apps-{live[2]}-panel")
        after = page.evaluate(width_of)
        assert abs(before - after) < 1
        box = page.evaluate("""() => {
            const r = document.querySelector('#ap-panel').getBoundingClientRect();
            return {tp: r.top, bt: r.bottom, h: innerHeight}; }""")
        assert abs((box["tp"] + box["bt"]) / 2 - box["h"] / 2) < 6  # vertically centered
        page.keyboard.press("Escape")
        page.wait_for_function("!document.querySelector('#ap-panel').classList.contains('is-open')")
        assert not errors, errors
    finally:
        ctx.close()


def test_a_stage_change_moves_the_card_in_place_and_undo_puts_it_back(live):
    base, browser, track, ids = live
    ctx, page, errors = _page(live, "light", 1680)
    try:
        title = next(iter(ids))
        card = page.locator(f'.ap-card[data-id="{ids[title]}"]')
        col_before = card.get_attribute("data-col")
        card.locator("[data-stage-menu]").click()
        page.wait_for_selector("#ap-menu-stage:not([hidden])")
        _shot(page, f"apps-{track}-stage-menu")
        page.locator('#ap-menu-stage [data-stage-pick]').nth(2).click()
        page.wait_for_selector(".k-toast")
        moved = page.locator(f'.ap-card[data-id="{ids[title]}"]').get_attribute("data-col")
        assert moved != col_before
        page.click(".k-toast__undo")
        page.wait_for_function(
            "([id, col]) => document.querySelector(`.ap-card[data-id=\"${id}\"]`)"
            ".getAttribute('data-col') === col", arg=[str(ids[title]), col_before])
        assert not errors, errors
    finally:
        ctx.close()


def test_the_closed_group_folds_and_search_finds_a_closed_card(live):
    ctx, page, errors = _page(live, "light", 1680)
    try:
        group = page.locator(".ap-closedg")
        assert "is-open" not in (group.get_attribute("class") or "")
        group.locator(".ap-cgh").click()
        page.wait_for_function("document.querySelector('.ap-closedg').classList.contains('is-open')")
        group.locator(".ap-cgh").click()
        page.fill("#ap-q", "pinecrest" if live[2] == "employment" else "ironbridge")
        page.wait_for_function("document.querySelector('.ap-closedg').classList.contains('is-open')")
        assert not errors, errors
    finally:
        ctx.close()


def test_add_a_role_sheet_opens_and_a_blank_one_is_refused(live):
    ctx, page, errors = _page(live, "light", 1280)
    try:
        page.click('[data-testid="add-role"]')
        page.wait_for_selector("#ap-add.is-open")
        _shot(page, f"apps-{live[2]}-add-sheet")
        page.click('#ap-add-form button[type="submit"]')
        page.wait_for_function("document.querySelector('#ap-add-err').textContent.length > 0")
        assert "#ap-add.is-open" and page.locator("#ap-add.is-open").count() == 1
        assert not errors, errors
    finally:
        ctx.close()


def test_the_page_mounts_unmounts_and_mounts_again_and_keeps_other_history_state(live):
    """The page lifecycle (D-UI-210): Applications unmounted and mounted again in place starts
    exactly what it started on the document load and leaves nothing running in between. After it,
    one click on More filters opens the filters, not open-then-shut (a doubled listener would
    toggle twice). Its URL rewrite keeps its own state in the `apps` field and never wipes what
    else is on the history entry; unmounted, the page no longer answers or rewrites anything."""
    base, browser, track, _ids = live
    ctx = browser.new_context(viewport={"width": 1680, "height": 900}, color_scheme="light")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(TIMER_PROBE_JS)
    try:
        page.goto(f"{base}/pursuits?track={track}")
        page.wait_for_selector('[data-testid="pcard"]')
        wait_for_layout(page)
        # The load itself writes its entry: the page's field, in a state that is no longer null,
        # beside the navigation controller's own stamp (D-UI-220).
        assert page.evaluate("history.state && Object.keys(history.state)"
                             ".filter(k => k !== 'kNav')") == ["apps"]
        first = assert_page_remounts_clean(page, "applications", "applications.js", settle_ms=800)
        assert first["timers"] == 0
        assert page.get_attribute("#ap-mf-btn", "aria-expanded") == "false"
        page.click("#ap-mf-btn")
        assert page.get_attribute("#ap-mf-btn", "aria-expanded") == "true"
        assert page.locator("#ap-mf.is-open").count() == 1
        # Another writer's field on the entry survives the page's own rewrite on each keystroke.
        page.evaluate("history.replaceState(Object.assign({}, history.state, {other: 7}), '')")
        page.fill("#ap-q", "a")
        state = page.evaluate("history.state")
        # ...and so does the navigation controller's stamp (D-UI-220).
        assert state["other"] == 7 and state["kNav"]["kind"] == "doc", state
        assert state["apps"]["q"] == "a" and "q=a" in page.url, state
        page.fill("#ap-q", "")
        assert page.evaluate("history.state.other") == 7
        assert "q=" not in page.url
        # Unmounted, the page stops: a click does not toggle and the URL is left alone.
        page.evaluate("KUI.page.unmount()")
        page.click("#ap-mf-btn")
        assert page.get_attribute("#ap-mf-btn", "aria-expanded") == "true"
        assert page.evaluate("history.state.apps.q") == ""
        page.fill("#ap-q", "zzz")
        assert "q=zzz" not in page.url
        assert not errors, errors
    finally:
        ctx.close()
