"""Needs another try in a real browser (UI overhaul parity check against the locked mockup,
the locked design mockup (kept privately)). Light and dark at 1280 and 1680: no overlap,
no wrapping titles or buttons, no horizontal scroll; then the interactions the mockup demos
(select and bulk, the model menu, the two dialogs, Try again in place, Remove with Undo).

Gated like the other live-browser design-system test: set KESTREL_LIVE_BROWSER=1. The browser is
the Edge already on the machine, else a Playwright Chromium (tests/browser.py)."""

from __future__ import annotations

import os

import pytest

from tests import retry_seed
from tests.browser import (
    LINES_JS,
    TIMER_PROBE_JS,
    TITLE_RIGHT_JS,
    art_visible_left,
    assert_page_remounts_clean,
    launch_sync,
    wait_for_layout,
)
from tests.browser_plugin import browser_artifacts  # noqa: F401

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


def _overlaps(a, b) -> bool:
    return a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1 and a["tp"] < b["bt"] - 1 \
        and b["tp"] < a["bt"] - 1


@pytest.fixture
def live(monkeypatch):
    """A server on the test's own throwaway data directory (the autouse _isolate_data_home gives
    each test a fresh one, so the server cannot outlive a test) and a browser."""
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    async def no_hosted(_settings):
        return ("", None, [], [], {}, None)

    # The menu lists what Ollama has and what a provider offers; neither is asked here.
    monkeypatch.setattr("kestrel.web.pages.needs_try._local_models",
                        lambda settings: (["kestrel-tier3:latest"], None))
    monkeypatch.setattr("kestrel.web.pages.needs_try._hosted_models", no_hosted)
    retry_seed.seed()
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
    page.goto(f"{base}/quarantine")
    page.wait_for_selector('[data-testid="rt-row"]')
    wait_for_layout(page)
    return ctx, page, errors


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_title_row_clears_the_art_of_every_scene_of_the_day(live, monkeypatch, theme):
    """The scene of the day rotates through every scene (ui_settings.scene_for_day), so the check
    in the test below passed or failed with the date it ran on. This one draws each scene on a
    fixed day of its own and asks the same question, at the width where this page's title row
    (the title, the count and the How it works button) is closest to the art: no scene draws under
    it. It reads a module's own day alias, never the real clock."""
    from datetime import date

    import kestrel.web.deps as deps
    from kestrel import ui_settings as u

    first = date(2026, 1, 1).toordinal()
    seen: set[str] = set()
    for k in range(len(u.SCENES)):
        monkeypatch.setattr(deps, "_today", lambda k=k: date.fromordinal(first + k))
        ctx, page, _errors = _page(live, theme, 1280)
        try:
            scene = page.evaluate("document.querySelector('.k-scene').dataset.scene")
            seen.add(scene)
            drawn = art_visible_left(page)
            right = page.evaluate(TITLE_RIGHT_JS)
            # Two exact facts, each able to fail alone (a-or-b hid which one held): this scene
            # draws art, and the first visible art pixel is at or right of the title row's edge.
            assert drawn is not None, (
                "no visible art was measured, so the title row was never checked", scene, theme)
            assert right <= drawn, (scene, theme, right, drawn)
        finally:
            ctx.close()
    assert seen == set(u.SCENES), "a scene was never drawn, so it was never checked"


# Paints a solid block into the scene art, inside the art's own box and right under the title
# row's last child (the How it works button), so the drawing sits under the button and nowhere
# to its right. Returns that child's box.
_ART_UNDER_BUTTON_JS = """() => {
    const art = document.querySelector('.k-scene__art:not(.k-scene__touch)');
    const kids = Array.from(document.querySelector('.k-scene__title').children)
      .filter(c => c.getClientRects().length);
    const btn = kids[kids.length - 1].getBoundingClientRect();
    const box = art.getBoundingClientRect();
    const left = Math.max(btn.left, box.left), right = btn.right - 2;
    const top = Math.max(btn.top, box.top), bottom = Math.min(btn.bottom, box.bottom);
    const inv = art.getScreenCTM().inverse();
    const at = (x, y) => new DOMPoint(x, y).matrixTransform(inv);
    const p = at(left, top), q = at(right, bottom);
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x', p.x); rect.setAttribute('y', p.y);
    rect.setAttribute('width', q.x - p.x); rect.setAttribute('height', q.y - p.y);
    rect.setAttribute('fill', getComputedStyle(document.body).color);
    rect.setAttribute('data-synthetic', '1');
    art.appendChild(rect);
    return {l: btn.left, r: btn.right, left, right}; }"""


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_art_drawn_under_the_title_button_fails_the_title_row_check(live, theme):
    """The invariant behind the measure (D-TEST-796): art drawn under the title row, including
    under the How it works button, is still found, so the title-row check above (right <= drawn)
    fails on it. The block sits only under the button, inside the art's box."""
    ctx, page, _errors = _page(live, theme, 1280)
    try:
        btn = page.evaluate(_ART_UNDER_BUTTON_JS)
        assert btn["right"] - btn["left"] >= 8, ("no room under the button inside the art", btn)
        drawn = art_visible_left(page)
        right = page.evaluate(TITLE_RIGHT_JS)
        assert drawn is not None, "the art under the button was not seen"
        assert drawn < right, (theme, btn, right, drawn)
        assert drawn <= btn["left"] + 2, (theme, btn, drawn)
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_no_overlap_no_wrapping_no_sideways_scroll(live, theme, width):
    ctx, page, errors = _page(live, theme, width)
    try:
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 0") is True
        assert page.evaluate("document.querySelector('.main, main').scrollWidth <= "
                             "document.querySelector('.main, main').clientWidth + 1") is True
        # Titles, chips, buttons, pills and the lede stay on one line.
        for sel in ("h1", ".rt-lede", ".k-pill", ".k-chip", ".rt-card .k-btn", ".rt-role b",
                    ".rt-role span", ".rt-why", ".rt-when", ".rt-tries", ".rt-all"):
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    assert page.evaluate(_LINES, el) <= 1, (
                        sel, theme, width, el.inner_text()[:40])
        # No two siblings in a row's grid or action line overlap, and the bar holds together.
        for container in (".rt-bar", ".rt-top", ".rt-acts"):
            for group in page.query_selector_all(container):
                if not group.is_visible():
                    continue
                kids = page.evaluate(
                    """el => Array.from(el.children).filter(e => e.offsetParent !== null)
                      .map(e => { const b = e.getBoundingClientRect();
                        return {t: (e.textContent || '').trim().slice(0, 30), l: b.left,
                                r: b.right, tp: b.top, bt: b.bottom}; })""", group)
                for i, a in enumerate(kids):
                    for b in kids[i + 1:]:
                        assert not _overlaps(a, b), (container, a, b, theme, width)
        # The help column is level with the card (wide) or below it (narrow), never over it.
        card = page.evaluate(_BOXES, "#rt-card")[0]
        for h in page.evaluate(_BOXES, ".k-hcard"):
            assert not _overlaps(card, h), (card, h)
        # Parity with the mockup: the help column sits at the right on a wide window and under the
        # list at 1300px of content or less (its container query), the band and its art stay inside
        # the content column, and the breadcrumb sits right above the title.
        help_col = page.evaluate(_BOXES, ".k-help-col")[0]
        if width >= 1680:
            assert help_col["l"] >= card["r"] - 1 and help_col["tp"] <= card["tp"] + 2, (
                help_col, card)
        else:
            assert help_col["tp"] >= card["bt"] - 1, (help_col, card)
        scene = page.evaluate(_BOXES, ".k-scene")[0]
        right = max(help_col["r"], card["r"])
        assert scene["r"] <= right + 1 and scene["l"] >= card["l"] - 1, (scene, card)
        # The reason and the role read in full, as in the mockup: nothing is cut with an ellipsis.
        for sel in (".rt-why", ".rt-role b"):
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    assert page.evaluate("e => e.scrollWidth <= e.clientWidth + 1", el), (
                        sel, theme, width, el.inner_text()[:40])
        crumb, h1 = page.evaluate(_BOXES, ".rt-crumb")[0], page.evaluate(_BOXES, "h1")[0]
        assert 0 <= h1["tp"] - crumb["bt"] <= 24, (crumb, h1)
        assert not page.query_selector("#announcement-slot")
        # Nothing sits on the scene art: the band's title block ends before the art begins.
        title = page.evaluate(_BOXES, ".k-scene__title")[0]
        drawn = art_visible_left(page)
        if drawn is not None:
            assert page.evaluate(TITLE_RIGHT_JS) <= drawn, (title, drawn)
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [480, 390, 720, 770])
def test_narrow_row_stacks_and_nothing_runs_past_the_edge(live, theme, width):
    """A browser tab at phone width: title and company, then the reason, then step, time and
    tries, then the buttons; every text reads in full and nothing sits past the right edge."""
    ctx, page, errors = _page(live, theme, width)
    try:
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        # Nothing visible in the page content ends past the viewport's right edge.
        past = page.evaluate(
            """w => Array.from(document.querySelectorAll('.rt-page *'))
              .filter(e => e.offsetParent !== null && !e.closest('.k-scene__art, .rt-mono, '
                + '[hidden], .k-flyout'))
              .filter(e => e.getBoundingClientRect().right > w + 1)
              .map(e => e.className || e.tagName).slice(0, 5)""", width)
        assert not past, (theme, width, past)
        # The header and bar wrap inside the column.
        for sel in (".rt-crumb", ".k-scene__title h1", ".rt-lede", ".rt-bar", ".rt-chips"):
            box = page.evaluate(_BOXES, sel)[0]
            assert box["r"] <= width + 1, (sel, box)
        for row in page.query_selector_all('[data-testid="rt-row"]'):
            boxes = page.evaluate(
                """r => { const q = s => r.querySelector(s).getBoundingClientRect();
                  const f = b => ({l: b.left, r: b.right, tp: b.top, bt: b.bottom});
                  return {role: f(q('.rt-role')), why: f(q('.rt-why')), step: f(q('.rt-step')),
                          when: f(q('.rt-when')), tries: f(q('.rt-tries')),
                          acts: f(q('.rt-acts'))}; }""",
                row)
            role, why, step, acts = boxes["role"], boxes["why"], boxes["step"], boxes["acts"]
            assert role["bt"] <= why["tp"] + 1 and why["bt"] <= step["tp"] + 1, boxes
            assert step["bt"] <= acts["tp"] + 1, boxes
            # Step, time and tries share one line, in that order, without overlap.
            assert abs(step["tp"] - boxes["when"]["tp"]) < 12, boxes
            assert step["r"] <= boxes["when"]["l"] + 1 <= boxes["tries"]["l"] + 1, boxes
            # The title, company and reason read as words: wide enough to hold a few letters
            # per line, never one character per line.
            for key in ("role", "why"):
                assert boxes[key]["r"] - boxes[key]["l"] > 150, (key, boxes)
            for sel in (".rt-step", ".rt-when", ".rt-tries"):
                assert page.evaluate(_LINES, row.query_selector(sel)) <= 1, (sel, theme, width)
            kids = page.evaluate(
                """r => Array.from(r.querySelectorAll('.rt-top > *, .rt-acts > *'))
                  .filter(e => e.offsetParent !== null).map(e => {
                    const b = e.getBoundingClientRect();
                    return {t: (e.textContent || '').trim().slice(0, 30), l: b.left, r: b.right,
                            tp: b.top, bt: b.bottom}; })""", row)
            for i, a in enumerate(kids):
                for b in kids[i + 1:]:
                    assert not _overlaps(a, b), (a, b, theme, width)
        # Buttons keep their words on one line each, and their text is not cut.
        buttons = [el for el in page.query_selector_all(".rt-card .k-btn") if el.is_visible()]
        assert buttons, (theme, width)
        cut = [el.inner_text() for el in buttons if page.evaluate(_LINES, el) > 1]
        assert not cut, cut
        for sel in (".rt-why", ".rt-role b", ".rt-role span"):
            shown = [el for el in page.query_selector_all(sel) if el.is_visible()]
            assert shown, (sel, theme, width)
            clipped = [el.inner_text() for el in shown
                       if not page.evaluate("e => e.scrollWidth <= e.clientWidth + 1", el)]
            assert not clipped, (sel, clipped)
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1440])
def test_sidebar_status_line_shows_in_full_or_wraps(live, theme, width):
    """The status readout ("Problem · 7 items need another try") is never cut: its text wraps
    inside the sidebar (a narrower window collapses it to icons, so those widths have no text)."""
    ctx, page, _ = _page(live, theme, width)
    try:
        page.wait_for_function(
            "document.querySelector('#rail').innerText.includes('need another try')")
        spans = [el for el in page.query_selector_all("#rail .k-sstat__l > span:last-child")
                 if el.is_visible()]
        assert spans, (theme, width)
        text = " ".join(el.inner_text() for el in spans)
        assert "Problem" in text and "need another try" in text, text
        for el in spans:
            assert page.evaluate("e => e.scrollWidth <= e.clientWidth + 1", el), (
                el.inner_text(), theme, width)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
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


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_on_the_card_and_help_cards_meets_contrast_in_both_themes(live, theme):
    ctx, page, _ = _page(live, theme, 1680)
    try:
        bad = page.evaluate(_CONTRAST, [
            ".rt-role b a", ".rt-role span", ".rt-why", ".rt-when", ".rt-tries", ".rt-step",
            ".rt-chips .k-chip", ".rt-remove", ".rt-note", ".rt-note a", ".rt-det summary",
            ".k-hcard h3", ".k-hcard li", ".k-morelink", ".rt-all", ".k-btn--primary",
            ".k-btn:not(.k-btn--primary)", ".k-scene__title .k-pill",
        ])
        assert not bad, (theme, bad)
    finally:
        ctx.close()


def test_the_page_follows_the_theme_tokens(live):
    ctx, page, _ = _page(live, "dark", 1280)
    try:
        bg = page.evaluate("getComputedStyle(document.querySelector('#rt-card')).backgroundColor")
        ink = page.evaluate("getComputedStyle(document.querySelector('.rt-role b')).color")
        assert bg != "rgb(255, 255, 255)" and ink != "rgb(0, 0, 0)"
    finally:
        ctx.close()


def test_select_bulk_and_filter_in_the_browser(live):
    ctx, page, errors = _page(live, "light", 1680)
    try:
        bulk = page.get_by_test_id("rt-bulk")
        assert bulk.inner_text() == "Try all again"
        page.locator(".rt-cb").nth(0).check()
        page.locator(".rt-cb").nth(1).check()
        assert bulk.inner_text() == "Try selected again (2)"
        assert page.locator("#rt-all-text").inner_text() == "2 selected"
        page.locator("#rt-all").check()
        assert page.locator("#rt-all-text").inner_text() == "7 selected"
        page.locator("#rt-all").uncheck()
        assert page.locator("#rt-all-text").inner_text() == "Select all"
        # A filter swaps the card in place; the page does not reload.
        page.evaluate("window.__same = 1")
        page.get_by_test_id("rt-chip-deep").click()
        page.wait_for_function("document.querySelectorAll('[data-testid=rt-row]').length === 1")
        assert page.evaluate("window.__same") == 1
        page.get_by_test_id("rt-chip-all").click()
        page.wait_for_function("document.querySelectorAll('[data-testid=rt-row]').length === 7")
        assert not errors, errors
    finally:
        ctx.close()


def test_the_model_menu_and_both_dialogs_open_where_the_mockup_puts_them(live):
    ctx, page, errors = _page(live, "light", 1280)
    try:
        row = page.locator('[data-testid="rt-row"]').first
        row.get_by_test_id("rt-model").click()
        menu = page.get_by_test_id("rt-menu")
        menu.wait_for()
        box = menu.bounding_box()
        assert box and box["x"] >= 0 and box["y"] + box["height"] <= 900
        assert "on this computer, free" in menu.inner_text().lower()
        page.keyboard.press("Escape")
        assert page.locator('[data-testid="rt-menu"]').count() == 0
        # Score it anyway: the dialog opens, centered in the window, and Cancel closes it.
        row.get_by_test_id("rt-anyway").click()
        modal = page.locator("#rt-anyway-dialog")
        modal.wait_for(state="visible")
        mb = modal.bounding_box()
        assert abs((mb["y"] + mb["height"] / 2) - 450) < 60
        assert "skips your rules check" in modal.inner_text()
        modal.get_by_role("button", name="Cancel").click()
        modal.wait_for(state="hidden")
        assert not errors, errors
    finally:
        ctx.close()


def test_try_again_waits_in_place_and_remove_has_an_undo(live):
    ctx, page, errors = _page(live, "light", 1280)
    try:
        first = page.locator('[data-testid="rt-row"]').first
        rid = first.get_attribute("id")
        first.get_by_test_id("retry").click()
        page.locator(f"#{rid} [data-testid=retry-queued]").wait_for()
        assert "Trying again" in page.locator(f"#{rid}").inner_text()
        # Remove another row: it folds out, a toast offers Undo, Undo brings it back.
        target = page.locator('[data-testid="rt-row"]').nth(1)
        tid = target.get_attribute("id")
        target.get_by_test_id("rt-remove").click()
        page.locator(f"#{tid}").wait_for(state="detached")
        toast = page.locator(".k-toast")
        toast.wait_for()
        assert "stays in Pipeline, not scored" in toast.inner_text()
        toast.get_by_role("button", name="Undo").click()
        page.wait_for_function("document.querySelectorAll('[data-testid=rt-row]').length === 7")
        assert not errors, errors
    finally:
        ctx.close()


def test_the_page_mounts_unmounts_and_mounts_again_with_one_set_of_listeners(live):
    """The page lifecycle (D-UI-210): Needs another try unmounted and mounted again in place
    starts exactly what it started on the document load. After it, one rt-reload asks for the
    card once, not twice (a doubled listener would ask twice), the model menu still opens and
    closes, and selecting still counts. Unmounted, an Undo (kestrel:undone) asks for nothing."""
    base, browser = live
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="light")
    page = ctx.new_page()
    errors: list[str] = []
    asked: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("request", lambda r: asked.append(r.url) if "/quarantine?step=" in r.url else None)
    page.add_init_script(TIMER_PROBE_JS)
    try:
        page.goto(f"{base}/quarantine")
        page.wait_for_selector('[data-testid="rt-row"]')
        wait_for_layout(page)
        first = assert_page_remounts_clean(page, "needs-try", "retry.js", settle_ms=500)
        assert first["timers"] == 0
        asked.clear()
        page.evaluate("document.dispatchEvent(new CustomEvent('rt-reload'))")
        page.wait_for_function("document.querySelectorAll('[data-testid=rt-row]').length === 7")
        page.wait_for_timeout(400)
        assert len(asked) == 1, asked
        row = page.locator('[data-testid="rt-row"]').first
        row.get_by_test_id("rt-model").click()
        page.get_by_test_id("rt-menu").wait_for()
        page.keyboard.press("Escape")
        assert page.locator('[data-testid="rt-menu"]').count() == 0
        page.locator(".rt-cb").nth(0).check()
        assert page.get_by_test_id("rt-bulk").inner_text() == "Try selected again (1)"
        page.evaluate("KUI.page.unmount()")
        asked.clear()
        page.evaluate("document.dispatchEvent(new CustomEvent('kestrel:undone'))")
        page.evaluate("document.dispatchEvent(new CustomEvent('rt-reload'))")
        page.wait_for_timeout(600)
        assert asked == []
        page.locator(".rt-cb").nth(1).check()
        assert page.get_by_test_id("rt-bulk").inner_text() == "Try selected again (1)"
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme,width", [("light", 1680), ("dark", 1680), ("light", 1000)])
def test_a_failure_with_no_role_reads_right_and_can_be_removed(live, theme, width):
    """D-UI-224, the owner's report: a job title ideas failure (no role) could not be cleared.
    Its row names the step, points to Profile, lays out with no overlap, and Remove folds it out
    with an Undo."""
    import sqlite3

    from kestrel import paths

    con = sqlite3.connect(paths.db_path())
    with con:
        cur = con.execute(
            "INSERT INTO quarantine (opportunity_id, tier, model, raw_response, error, attempts,"
            " created_at) VALUES (NULL, 15, 'm', 'It is. It is.', 'the reply hit max_tokens"
            " 16000', 3, '2026-10-30T23:18:19Z')")
        qid = cur.lastrowid
    con.close()
    ctx, page, errors = _page(live, theme, width)
    try:
        row = page.locator(f"#rt-row-{qid}")
        row.scroll_into_view_if_needed()
        text = row.inner_text()
        assert "No role attached" in text and "Job title ideas" in text
        assert "Open Profile and press Suggest again" in text
        assert row.get_by_test_id("retry").count() == 0 and row.locator(".rt-cb").count() == 0
        assert row.get_by_test_id("rt-place").get_attribute("href") == "/profile?tab=employment"
        boxes = page.evaluate(_BOXES, f"#rt-row-{qid} .rt-top > *, #rt-row-{qid} .rt-acts > *")
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                assert not _overlaps(a, b), (a, b)
        if os.environ.get("KESTREL_SHOT_DIR"):
            row.screenshot(path=os.path.join(os.environ["KESTREL_SHOT_DIR"],
                                             f"retry-roleless-{theme}-{width}.png"))
        row.get_by_test_id("rt-remove").click()
        page.locator(f"#rt-row-{qid}").wait_for(state="detached")
        toast = page.locator(".k-toast")
        toast.wait_for()
        assert "Nothing was deleted" in toast.inner_text()
        toast.get_by_role("button", name="Undo").click()
        page.locator(f"#rt-row-{qid}").wait_for()
        assert not errors, errors
    finally:
        ctx.close()
