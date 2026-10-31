"""Models and Model check in a real browser (UI overhaul parity check against the locked mockups,
the locked design mockup (kept privately) and bench-mockup-locked.html). Light and dark
at 1280 and 1680: no overlap, no wrapped titles or buttons, no horizontal scroll; then the
interactions the mockups demo (the Change model drawer opens centered on the right, Model check
runs in place with live progress and Cancel).

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
from tests.model_check_seed import seed_check, seed_labels

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

_LINES = LINES_JS

_KIDS = """el => Array.from(el.children).filter(e => e.offsetParent !== null)
  .map(e => { const b = e.getBoundingClientRect();
    return {t: (e.textContent || '').trim().slice(0, 30), l: b.left, r: b.right, tp: b.top,
            bt: b.bottom}; })"""

_BOXES = """sel => Array.from(document.querySelectorAll(sel)).filter(e => e.offsetParent !== null)
  .map(e => { const b = e.getBoundingClientRect();
    return {t: (e.textContent || '').trim().slice(0, 30), l: b.left, r: b.right, tp: b.top,
            bt: b.bottom}; })"""


# A fixed-position overlay has no offsetParent, so it gets its own box reader.
_FIXED = """(() => { const b = document.querySelector('#mo-change').getBoundingClientRect();
  return {l: b.left, r: b.right, tp: b.top, bt: b.bottom}; })()"""


def _overlaps(a, b) -> bool:
    return a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1 and a["tp"] < b["bt"] - 1 \
        and b["tp"] < a["bt"] - 1


@pytest.fixture
def live(monkeypatch):
    """A server on the test's own throwaway data directory, a seeded check and a browser. The
    Ollama probe and hardware are patched so the page does not depend on the machine."""
    from playwright.sync_api import sync_playwright

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.llm.profiles import Hardware
    from kestrel.services import setup_state as ss
    from kestrel.web.serve import running_server

    monkeypatch.setattr(ss, "detected_hardware", lambda: Hardware(
        os="windows", gpu_vendor="nvidia", vram_gb=12.0, ram_gb=32.0))
    monkeypatch.setattr(ss, "probe_ollama", lambda settings, fresh=False: (
        "0.5.0", ["nomic-embed-text:latest"]))
    migrate.run_migrations(paths.db_path())
    seed_labels(9)
    seed_check(changes=1)
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


def _open(live, path: str, theme: str, width: int):
    base, browser = live
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}{path}")
    page.wait_for_selector(".mo-page")
    wait_for_layout(page)
    return ctx, page, errors


@pytest.mark.parametrize("path", ["/models", "/bench"])
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_no_overlap_no_wrapping_no_sideways_scroll(live, path, theme, width):
    ctx, page, errors = _open(live, path, theme, width)
    try:
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        assert page.evaluate("document.querySelector('.main, main').scrollWidth <= "
                             "document.querySelector('.main, main').clientWidth + 1") is True
        # Titles, pills, chips and buttons stay on one line.
        for sel in ("h1", ".k-pill", ".k-chip", ".mo-card .k-btn", ".mo-model b", ".mo-name",
                    ".k-stag"):
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    assert page.evaluate(_LINES, el) <= 1, (
                        path, sel, theme, width, el.inner_text()[:40])
        # No two siblings in a card header, a row of actions or the two-up grids overlap.
        for container in (".mo-head", ".mo-row", ".mo-actions", ".mo-two", ".mo-check-card"):
            for group in page.query_selector_all(container):
                if not group.is_visible():
                    continue
                kids = page.evaluate(_KIDS, group)
                for i, a in enumerate(kids):
                    for b in kids[i + 1:]:
                        assert not _overlaps(a, b), (path, container, a, b, theme, width)
        # The help column is level with the first card (wide) or below the cards (narrow), never
        # over them.
        main_cards = page.evaluate(_BOXES, ".k-layout__main > .mo-card, .k-layout__main > .mo-two")
        for h in page.evaluate(_BOXES, ".k-hcard"):
            for c in main_cards:
                assert not _overlaps(c, h), (path, c, h)
        help_col = page.evaluate(_BOXES, ".k-help-col")[0]
        if width >= 1680:
            assert help_col["l"] >= max(c["r"] for c in main_cards) - 1
        scene = page.evaluate(_BOXES, ".k-scene")[0]
        # The shell's scene band runs to the window edge (the page padding sits inside it).
        assert scene["r"] <= width + 1
        assert not page.query_selector("#announcement-slot")
        assert not errors, errors
    finally:
        ctx.close()


def test_the_steps_table_keeps_every_column_readable(live):
    ctx, page, _ = _open(live, "/models", "light", 1280)
    try:
        rows = page.query_selector_all("[data-testid^='mo-step-']")
        assert len(rows) == 5
        for sel in (".mo-tbl th", ".mo-act .k-btn", ".mo-fit"):
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    assert page.evaluate(_LINES, el) <= 1, (sel, el.inner_text()[:40])
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_speed_controls_save_in_place_and_fit_with_details_open(live, theme, width):
    from playwright.sync_api import expect

    ctx, page, errors = _open(live, "/models", theme, width)
    try:
        page.locator("#mo-downloads > details > summary").click()
        page.get_by_test_id("mo-speed-t1").fill("6")
        page.get_by_test_id("mo-speed-t2").fill("3")
        page.get_by_test_id("mo-speed-save").click()
        expect(page.get_by_test_id("mo-speed-message")).to_contain_text("Saved.")
        expect(page.get_by_test_id("mo-speed-parallel")).to_contain_text("at least 6")
        assert page.url.endswith("/models")
        assert page.locator("#mo-downloads > details").get_attribute("open") is not None
        for field in ("mo-speed-t1", "mo-speed-t2", "mo-speed-save"):
            assert page.get_by_test_id(field).is_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator("main").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        for element in page.locator("#mo-speed b, #mo-speed button, #mo-speed label, h1").all():
            assert element.evaluate(_LINES) <= 1
        boxes = page.locator("#mo-speed .mo-actions").evaluate(_KIDS)
        for index, first in enumerate(boxes):
            for second in boxes[index + 1:]:
                assert not _overlaps(first, second), (first, second, theme, width)
        page.reload()
        page.locator("#mo-downloads > details > summary").click()
        expect(page.get_by_test_id("mo-speed-t1")).to_have_value("6")
        expect(page.get_by_test_id("mo-speed-t2")).to_have_value("3")
        assert not errors, errors
    finally:
        ctx.close()


def test_the_change_model_drawer_opens_centered_on_the_right_and_loads_in_place(live):
    ctx, page, errors = _open(live, "/models", "light", 1680)
    try:
        page.get_by_test_id("mo-change-tier2").click()
        page.wait_for_selector("#mo-change-form")
        box = page.evaluate(_FIXED)
        win = page.evaluate("({w: innerWidth, h: innerHeight})")
        assert box["r"] >= win["w"] - 4, box  # on the right edge
        mid = (box["tp"] + box["bt"]) / 2
        assert abs(mid - win["h"] / 2) <= 40, (mid, win)  # vertically centered, never at the top
        page.get_by_test_id("mo-where-online").click()
        page.wait_for_selector("[data-testid='mo-online-refused'], [data-testid='mo-online-list']")
        after = page.evaluate(_FIXED)
        # Swapping the body keeps it centered; it never jumps to the top.
        assert abs((after["tp"] + after["bt"]) / 2 - win["h"] / 2) <= 40, (after, win)
        assert not errors, errors
    finally:
        ctx.close()


def test_model_check_runs_in_place_with_progress_and_cancel(live):
    ctx, page, errors = _open(live, "/bench", "light", 1680)
    try:
        page.get_by_test_id("ck-run").click()
        page.wait_for_selector("[data-testid='ck-live']")
        assert page.get_by_test_id("ck-cancel").is_visible()
        assert page.evaluate("location.pathname") == "/bench"  # nothing navigated
        page.get_by_test_id("ck-cancel").click()
        page.wait_for_selector("[data-testid='ck-run']")
        assert "canceled" in page.get_by_test_id("ck-notice").inner_text()
        assert not errors, errors
    finally:
        ctx.close()


def test_models_mounts_unmounts_and_mounts_again_with_one_set_of_listeners(live):
    """The page lifecycle (D-UI-210): Models unmounted and mounted again in place starts exactly
    what it started on the document load and leaves nothing behind. Afterwards one click opens a
    row's more menu and it stays open (a doubled listener opens and closes it in one click),
    one click on a column header sorts it once, and an unmounted page closes the menu and answers
    no click."""
    base, browser = live
    ctx = browser.new_context(viewport={"width": 1680, "height": 900}, color_scheme="light")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(TIMER_PROBE_JS)
    try:
        page.goto(f"{base}/models")
        page.wait_for_selector(".mo-page")
        wait_for_layout(page)
        first = assert_page_remounts_clean(
            page, "models", "models.js", settle_ms=400, quiet_ms=600)
        assert first["timers"] == 0 and first["observers"] == 0
        more = page.query_selector("[data-mo-more]")
        menu = more.evaluate_handle("b => b.parentNode.querySelector('.mo-menu')")
        more.click()
        assert menu.as_element().is_visible()
        assert more.get_attribute("aria-expanded") == "true"
        page.keyboard.press("Escape")
        assert not menu.as_element().is_visible()
        more.click()
        assert menu.as_element().is_visible()
        # The comparison sorts once per click: ascending first.
        page.evaluate("document.querySelector('#mo-history').open = true")
        th = page.query_selector('#model-compare th[data-sort="num"]')
        th.click()
        assert th.get_attribute("data-dir") == "asc"
        th.click()
        assert th.get_attribute("data-dir") == "desc"
        # Unmounting closes the open menu; unmounted, nothing opens it or sorts.
        page.evaluate("KUI.page.unmount()")
        assert not menu.as_element().is_visible()
        assert more.get_attribute("aria-expanded") == "false"
        more.click()
        th.click()
        page.wait_for_timeout(200)
        assert not menu.as_element().is_visible()
        assert th.get_attribute("data-dir") == "desc"
        assert not errors, errors
    finally:
        ctx.close()


def test_model_check_mounts_unmounts_and_mounts_again_and_its_pickers_bind_once(live):
    """The page lifecycle (D-UI-210): Model check has no listeners of its own, so mounting it holds
    nothing and unmounting leaves nothing. Its pickers (the shared model_combo.js) are upgraded
    once per element: scanning again, or mounting the page again, never doubles a picker's
    listeners (a doubled toggle opens the list and shuts it in one click), and a picker copied
    from saved markup, with the old ready flag on it, is still upgraded."""
    base, browser = live
    ctx = browser.new_context(viewport={"width": 1680, "height": 900}, color_scheme="light")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(TIMER_PROBE_JS)
    try:
        page.goto(f"{base}/bench")
        page.wait_for_selector(".mo-page")
        wait_for_layout(page)
        page.click("#ck-advanced > summary")
        page.wait_for_selector('[data-testid="bench-run-model"][data-ready]')
        assert page.evaluate("KUI.page.current()") == "bench"
        assert page.evaluate("KUI.page._stats()") == {
            "name": "bench", "listeners": 0, "timers": 0, "observers": 0}
        assert page.evaluate("document.querySelectorAll('[data-combo][data-ready]').length") == 2
        assert page.evaluate("KUI.page.unmount()") is True
        assert page.evaluate("KUI.page.current()") is None
        assert page.evaluate("window.__kTimers.pending('/static/js/bench.js')") == 0
        assert page.evaluate("KUI.page.mount()") is True
        assert page.evaluate("KUI.page.current()") == "bench"
        page.evaluate("keCombo.scan(); keCombo.scan(document.body); KUI.page.mount()")
        toggle = '[data-testid="bench-run-model"] [data-combo-toggle]'
        listbox = '[data-testid="bench-run-model"] [role=listbox]'
        page.click(toggle)
        assert page.is_visible(listbox)  # one click opens it and it stays open
        page.keyboard.press("Escape")
        assert not page.is_visible(listbox)
        # A picker copied with its ready flag (saved markup) is upgraded when scanned.
        page.evaluate("""() => {
          const src = document.querySelector('[data-testid="bench-run-model"]');
          const copy = src.cloneNode(true);
          copy.setAttribute('data-testid', 'copied-combo');
          src.parentNode.appendChild(copy); keCombo.scan(copy.parentNode); }""")
        page.click('[data-testid="copied-combo"] [data-combo-toggle]')
        assert page.is_visible('[data-testid="copied-combo"] [role=listbox]')
        assert not errors, errors
    finally:
        ctx.close()


_CONTRAST = r"""sels => {
  const parse = c => { const m = c.match(/[\d.]+/g).map(Number); return {r: m[0], g: m[1], b: m[2],
    a: m.length > 3 ? m[3] : 1}; };
  const lum = ({r, g, b}) => { const f = v => { v /= 255;
    return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); };
    return .2126 * f(r) + .7152 * f(g) + .0722 * f(b); };
  const bgOf = el => { for (let e = el; e; e = e.parentElement) {
      const c = parse(getComputedStyle(e).backgroundColor); if (c.a > .5) return c; }
    return {r: 255, g: 255, b: 255, a: 1}; };
  const bad = [];
  for (const sel of sels) for (const el of document.querySelectorAll(sel)) {
    if (!el.offsetParent || !(el.textContent || '').trim()) continue;
    const fg = parse(getComputedStyle(el).color), bg = bgOf(el);
    const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
    const ratio = (a + .05) / (b + .05);
    if (ratio < 4.5) bad.push([sel, (el.textContent || '').trim().slice(0, 24), ratio.toFixed(2)]);
  }
  return bad;
}"""


@pytest.mark.parametrize("path", ["/models", "/bench"])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_meets_contrast_in_both_themes(live, path, theme):
    ctx, page, _ = _open(live, path, theme, 1680)
    try:
        bad = page.evaluate(_CONTRAST, [
            ".mo-tbl td b", ".mo-fit", ".mo-cr", ".mo-checks b", ".mo-kv dd", ".mo-chips li",
            ".mo-msg", ".k-hcard li", ".k-btn--primary", ".mo-strip dd", ".mo-head h3",
        ])
        assert not bad, (path, theme, bad)
    finally:
        ctx.close()
