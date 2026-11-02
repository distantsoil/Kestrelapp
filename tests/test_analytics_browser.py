"""Analytics in a real browser (UI overhaul parity check against the locked mockup,
the locked design mockup (kept privately)). Light and dark at 1280 and 1680: no overlap,
no wrapped titles, tiles or buttons, no horizontal scroll; then the interactions the mockup demos
(the range, Table view, the Export menu, the group tabs and the hover tip).

Gated like the other live-browser design-system tests: set KESTREL_LIVE_BROWSER=1. The browser is
the Edge already on the machine, else a Playwright Chromium (tests/browser.py)."""

from __future__ import annotations

import os

import pytest

from tests.browser import TIMER_PROBE_JS, assert_page_remounts_clean, launch_sync, wait_for_layout
from tests.test_analytics_page import _seed_web

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("KESTREL_LIVE_BROWSER") != "1",
        reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
    ),
    pytest.mark.usefixtures("_isolate_data_home"),
]

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
def live(monkeypatch):
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    _seed_web(12)
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


def _page(live, theme: str, width: int, path: str = "/analytics"):
    base, browser = live
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}{path}")
    page.wait_for_selector('[data-testid="an-kpis"]')
    wait_for_layout(page)
    return ctx, page, errors


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_no_overlap_no_wrapping_no_sideways_scroll(live, theme, width):
    ctx, page, errors = _page(live, theme, width)
    try:
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        assert page.evaluate("document.querySelector('.main, main').scrollWidth <= "
                             "document.querySelector('.main, main').clientWidth + 1") is True
        for sel in ("h1", ".an-head h3", ".an-bar .k-btn", ".an-bar .k-chip",
                    ".an-bar .k-seg button", ".an-kpi__l", ".an-kpi__n", ".an-frow__lab",
                    ".an-frow__v", ".an-tabs button",
                    ".an-age span"):
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    assert page.evaluate(_LINES, el) <= 1, (sel, theme, width, el.inner_text()[:40])
        # Rows that mix font sizes (a big number beside its label) stay one line tall.
        for sel in (".an-sl", ".an-sbhead", ".an-frow"):
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    assert page.evaluate("e => e.getBoundingClientRect().height", el) <= 44, (
                        sel, theme, width, el.inner_text()[:40])
        for container in (".an-bar", ".an-kpis", ".an-g2", ".an-rows", ".an-head", ".an-ages"):
            for group in page.query_selector_all(container):
                if not group.is_visible():
                    continue
                kids = page.evaluate(_KIDS, group)
                for i, a in enumerate(kids):
                    for b in kids[i + 1:]:
                        assert not _overlaps(a, b), (container, a, b, theme, width)
        # The cards stack as the mockup's container queries say: five tiles on a wide window,
        # three at 1300px of content or less.
        tiles = page.evaluate("""() => Array.from(document.querySelectorAll('.an-kpi'))
            .map(e => Math.round(e.getBoundingClientRect().top))""")
        per_row = tiles.count(tiles[0])
        assert per_row == (5 if width >= 1680 else 3), (per_row, width)
        assert not page.query_selector("#announcement-slot")
        assert not errors, errors
    finally:
        ctx.close()


def test_the_range_swaps_the_body_in_place_and_table_view_swaps_charts_for_tables(live):
    ctx, page, errors = _page(live, "light", 1680)
    try:
        page.click('[data-testid="an-range-30"]')
        page.wait_for_selector('[data-testid="an-range-30"].is-on')
        assert "range=30" in page.url
        assert "last 30 days" in page.inner_text('[data-testid="an-summary"]')
        assert page.is_visible(".an-sbar")
        page.click('[data-testid="an-table-view"]')
        page.wait_for_selector(".an-stack.is-table")
        assert not page.is_visible(".an-sbar") and page.is_visible('.an-card .an-at')
        assert page.is_visible('[data-testid="an-standing"] .an-table')
        assert not errors, errors
    finally:
        ctx.close()


def test_the_export_menu_the_group_tabs_and_the_hover_tip(live):
    ctx, page, errors = _page(live, "dark", 1680)
    try:
        page.click('[data-testid="an-export"] summary')
        assert page.is_visible(".an-menu a >> text=Summary and charts (PDF)")
        menu = page.evaluate("""() => { const b = document.querySelector('.an-menu')
            .getBoundingClientRect(); return {l: b.left, r: b.right, t: b.top, bt: b.bottom,
            w: innerWidth}; }""")
        assert 0 <= menu["l"] and menu["r"] <= menu["w"]
        page.keyboard.press("Escape")
        assert not page.is_visible(".an-menu")
        page.click('[data-testid="an-tab-remote"]')
        assert page.is_visible('[data-panel="remote"]')
        assert not page.is_visible('[data-panel="src"]')
        page.hover(".an-sbar i")
        box = page.query_selector(".an-sbar i").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2 + 2, box["y"] + box["height"] / 2)
        page.wait_for_selector(".an-tip.is-on")
        assert page.is_visible(".an-tip.is-on")
        assert " of " in page.inner_text(".an-tip")
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_funnel_bars_are_their_true_share_of_the_track(live, theme, width):
    """A 12-of-12 bar spans its whole track and a 6-of-12 bar about half of it."""
    ctx, page, errors = _page(live, theme, width)
    try:
        # The later steps sit in a fold under the four narrowing steps (the mockup draws four).
        page.evaluate(
            "document.querySelectorAll('details.an-fold').forEach(d => { d.open = true; })")
        page.wait_for_timeout(1200)  # the bars grow in (.9s); measure them at rest
        rows = page.evaluate("""() => Array.from(document.querySelectorAll('.an-frow'))
          .filter(r => r.offsetParent !== null).map(r => {
            const t = r.querySelector('.an-frow__bar').getBoundingClientRect().width;
            const b = r.querySelector('.an-frow__bar u').getBoundingClientRect().width;
            return {lab: r.querySelector('.an-frow__lab').textContent.trim(),
                    v: parseInt(r.querySelector('.an-frow__v').textContent.replace(/,/g, '')),
                    track: t, bar: b, w: r.querySelector('.an-frow__bar u').style.width}; })""")
        full = [r for r in rows if r["v"] == 12]
        half = [r for r in rows if r["v"] == 6]
        assert full and half, rows
        for r in rows:
            assert r["track"] >= 60, ("the bar track collapsed", r, theme, width)
        for r in full:
            assert r["bar"] >= r["track"] * 0.97, (r, theme, width)
        for r in half:
            assert 0.45 <= r["bar"] / r["track"] <= 0.55, (r, theme, width)
    finally:
        ctx.close()



@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_cards_contain_their_content_and_tiles_leave_no_empty_slot(live, theme, width):
    ctx, page, errors = _page(live, theme, width)
    try:
        # Nothing runs past its own card, and with the seeded short names the group tabs, rate
        # cells and first-column names stay on one line (a long name may wrap, D-UI-226).
        bad = page.evaluate("""() => { const out = [];
          for (const card of document.querySelectorAll('.an-card')) {
            const c = card.getBoundingClientRect();
            for (const el of card.querySelectorAll('*')) {
              if (el.offsetParent === null || el.closest('[hidden]')) continue;
              const r = el.getBoundingClientRect();
              if (r.width && (r.right > c.right + 1 || r.left < c.left - 1))
                out.push([el.tagName, el.className, (el.textContent || '').trim().slice(0, 24)]);
            } }
          return out; }""")
        assert not bad, (bad[:5], theme, width)
        for sel, tallest in (('.an-tabs', 44), ('.an-rate', 28), ('.an-at td > b', 24)):
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    box = el.bounding_box()
                    assert box["height"] <= tallest, ("wrapped", sel, box, theme, width,
                                                      el.inner_text())
        # The last tile row is filled edge to edge: 5 across, or 3 then 2 that span the row.
        rows = page.evaluate("""() => { const k = document.querySelector('.an-kpis')
            .getBoundingClientRect(); const byTop = {};
          for (const e of document.querySelectorAll('.an-kpi')) {
            const b = e.getBoundingClientRect();
            const t = Math.round(b.top); (byTop[t] = byTop[t] || []).push([b.left, b.right]); }
          return {left: k.left, right: k.right, rows: Object.values(byTop)}; }""")
        for row in rows["rows"]:
            assert abs(min(a for a, _ in row) - rows["left"]) <= 1, (rows, theme, width)
            assert abs(max(b for _, b in row) - rows["right"]) <= 1, (rows, theme, width)
        assert not errors, errors
    finally:
        ctx.close()


def test_how_kestrel_is_running_sits_at_the_top_of_its_card(live):
    ctx, page, _ = _page(live, "light", 1680)
    try:
        top = page.evaluate("""() => { const c = document.querySelector('.an-moved');
          const h = c.querySelector('.an-head'); return h.getBoundingClientRect().top
            - c.getBoundingClientRect().top; }""")
        assert top <= 24, top
    finally:
        ctx.close()




def test_paired_cards_stretch_to_equal_height(live):
    """D-UI-218: the funnel row and the Replies by fit / Where roles come from row stretch to
    equal height, as the mockup's grid does."""
    ctx, page, errors = _page(live, "light", 1680)
    try:
        for left, right in (("an-standing", "an-funnel"), ("an-bands", "an-sources")):
            a = page.get_by_test_id(left).bounding_box()
            b = page.get_by_test_id(right).bounding_box()
            assert abs(a["height"] - b["height"]) <= 1, (left, right, a, b)
            assert abs(a["y"] - b["y"]) <= 1, (left, right)
        assert not errors, errors
    finally:
        ctx.close()


def test_the_page_mounts_unmounts_and_mounts_again_with_one_set_of_listeners(live):
    """The page lifecycle (D-UI-210): Analytics unmounted and mounted again in place starts
    exactly what it started on the document load and leaves nothing behind. Afterwards the ask-first
    dialog that HTMX swaps in opens once, not twice (a doubled listener opens it twice), the group
    tabs and the hover tip still work, and an unmounted page answers none of them."""
    base, browser = live
    ctx = browser.new_context(viewport={"width": 1680, "height": 900}, color_scheme="light")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(TIMER_PROBE_JS)
    try:
        page.goto(f"{base}/analytics")
        page.wait_for_selector('[data-testid="an-kpis"]')
        wait_for_layout(page)
        first = assert_page_remounts_clean(
            page, "analytics", "analytics.js", settle_ms=400, quiet_ms=600)
        assert first["timers"] == 0 and first["observers"] == 0
        page.evaluate("""() => { window.__opens = 0; const o = KUI.openModal;
          KUI.openModal = function () { window.__opens++; return o.apply(this, arguments); }; }""")
        ask = "htmx.ajax('GET', '/analytics/narrative/online?track=employment', " \
              "{target: '#an-dialog', swap: 'innerHTML'})"
        page.evaluate(ask)
        page.wait_for_selector(".k-modal.is-open")
        assert page.evaluate("window.__opens") == 1
        page.keyboard.press("Escape")
        page.wait_for_function("!document.querySelector('.k-modal.is-open')")
        page.click('[data-testid="an-tab-remote"]')
        assert page.is_visible('[data-panel="remote"]')
        assert not page.is_visible('[data-panel="src"]')
        page.hover(".an-sbar i")
        box = page.query_selector(".an-sbar i").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2 + 2, box["y"] + box["height"] / 2)
        page.wait_for_selector(".an-tip.is-on")
        # Unmounted, the tip goes and stays gone, and the swapped-in dialog is not opened.
        page.evaluate("KUI.page.unmount()")
        assert not page.query_selector(".an-tip.is-on")
        page.mouse.move(box["x"] + box["width"] / 2 - 2, box["y"] + box["height"] / 2)
        page.wait_for_timeout(200)
        assert not page.query_selector(".an-tip.is-on")
        page.evaluate("window.__opens = 0")
        page.evaluate(ask)
        page.wait_for_selector("#k-layer .k-modal, #an-dialog .k-modal", state="attached")
        page.wait_for_timeout(200)
        assert page.evaluate("window.__opens") == 0
        assert not errors, errors
    finally:
        ctx.close()


_CONTAINED = """() => { const out = [];
  for (const card of document.querySelectorAll('.an-card')) {
    const c = card.getBoundingClientRect();
    for (const el of card.querySelectorAll('*')) {
      if (el.offsetParent === null || el.closest('[hidden]')) continue;
      const r = el.getBoundingClientRect();
      if (r.width && (r.right > c.right + 1 || r.left < c.left - 1))
        out.push([el.tagName, el.className, (el.textContent || '').trim().slice(0, 24),
                  Math.round(r.right - c.right)]); } }
  return out; }"""


@pytest.fixture
def live_long_source(monkeypatch):
    """The owner's data shape (issue #60): a long source name and three-digit counts."""
    import sqlite3

    from playwright.sync_api import sync_playwright

    from kestrel import paths
    from kestrel.web.serve import running_server

    _seed_web(150)
    con = sqlite3.connect(paths.db_path())
    try:
        con.execute("UPDATE source SET name = 'Applied history import (employment)'")
        con.commit()
    finally:
        con.close()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1520, 1600, 1640, 1680, 1760])
def test_where_roles_come_from_stays_inside_its_card_side_by_side(live_long_source, theme, width):
    """Between the 1500px stacking step and a wide window the card is about 600px wide. A long
    source name and "0% (0 of 149)" used to run up to 74px past it (issue #60); they wrap now."""
    ctx, page, errors = _page(live_long_source, theme, width)
    try:
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        bad = page.evaluate(_CONTAINED)
        assert not bad, (bad[:5], theme, width)
        if width >= 1760:  # room enough: nothing wraps, as the mockup draws it
            for sel in (".an-rate", ".an-at td > b"):
                for el in page.query_selector_all(sel):
                    if el.is_visible():
                        assert el.bounding_box()["height"] <= 28, (sel, theme, width)
        assert not errors, errors
    finally:
        ctx.close()
