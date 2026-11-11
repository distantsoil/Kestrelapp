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
    swap_result,
    wait_for_layout,
)
from tests.browser_plugin import browser_artifacts  # noqa: F401

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


_TITLES = ("() => Array.from(document.querySelectorAll('.ap-table tbody tr .ap-tt b'))"
           ".map(b => b.textContent.trim())")


def _open_table(live, query: str = "&view=table", **ctx_kw):
    base, browser, track, _ids = live
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, **ctx_kw)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    return ctx, page, errors, f"{base}/pursuits?track={track}{query}"


_FIRST = {"role": 1, "stage": 1, "due": -1, "applied": -1, "fit": -1}
_BOARD_SORT = "(() => { const b = document.querySelector('#apps-board');" \
              " return [b.dataset.sortcol, Number(b.dataset.sortdir)]; })()"


def _wait_sort(page, col: str, direction: int) -> None:
    """Wait for the server's table to be swapped in with this sort (the sort is the server's)."""
    page.wait_for_function(
        """([c, d]) => { const b = document.querySelector('#apps-board');
          return !!b && b.dataset.sortcol === c && Number(b.dataset.sortdir) === d
            && !document.querySelector('.htmx-request'); }""",
        arg=[col, direction], timeout=8000)
    page.wait_for_function("() => document.querySelectorAll('.ap-table tbody tr').length > 0")


def _next_sort(page, col: str) -> tuple[str, int]:
    """What a press on this header asks for: first press, reverse, then back to the default."""
    cur_col, cur_dir = page.evaluate(_BOARD_SORT)
    if cur_col == col:
        return (col, -cur_dir) if cur_dir == _FIRST[col] else ("", 1)
    return col, _FIRST[col]


def _press(page, col: str) -> None:
    want = _next_sort(page, col)
    page.click(f"th[data-sort={col}] button")
    _wait_sort(page, *want)


def test_table_headers_are_buttons_that_sort_by_the_visible_title_and_announce_it(live):
    """#137: a sortable header is a real button, aria-sort follows it, and the role column sorts
    on the title shown (not the search text), both ways."""
    ctx, page, errors, url = _open_table(live)
    try:
        page.goto(url)
        page.wait_for_selector(".ap-table tbody tr")
        wait_for_layout(page)
        assert page.locator("th[data-sort] > button.ap-sortbtn").count() == 5
        assert page.get_attribute("th[data-sort=role]", "aria-sort") == "none"
        page.focus("th[data-sort=role] button")
        page.keyboard.press("Enter")  # keyboard, not a mouse click
        _wait_sort(page, "role", 1)
        assert page.get_attribute("th[data-sort=role]", "aria-sort") == "ascending"
        titles = page.evaluate(_TITLES)
        assert titles == sorted(titles, key=str.lower), titles
        # Focus is still on the header button after the table was swapped, so Space works next.
        assert page.evaluate("document.activeElement.closest('th')?.dataset.sort") == "role"
        page.keyboard.press("Space")
        _wait_sort(page, "role", -1)
        assert page.get_attribute("th[data-sort=role]", "aria-sort") == "descending"
        assert page.evaluate(_TITLES) == sorted(titles, key=str.lower, reverse=True)
        # Another column takes the mark; the role header goes back to none.
        _press(page, "fit")
        assert page.get_attribute("th[data-sort=role]", "aria-sort") == "none"
        assert page.get_attribute("th[data-sort=fit]", "aria-sort") == "descending"
        # The mark is drawn (a direction indicator), not only announced.
        wait_for_layout(page)
        assert page.evaluate(
            "getComputedStyle(document.querySelector('th[data-sort=fit] button'), '::after')"
            ".opacity") == "1"
        assert not errors, errors
    finally:
        ctx.close()


def test_due_sorts_most_overdue_first_and_rows_with_no_due_stay_last_either_way(live):
    ctx, page, errors, url = _open_table(live)
    try:
        page.goto(url)
        page.wait_for_selector(".ap-table tbody tr")
        dues = """() => Array.from(document.querySelectorAll('.ap-table tbody tr')).map(r => ({
            folded: r.dataset.folded, quiet: r.dataset.quiet,
            days: Number(r.dataset.quiet_days)}))"""
        _press(page, "due")
        first = page.evaluate(dues)
        overdue = [r["days"] for r in first if r["folded"] != "1" and r["quiet"] == "1"]
        assert overdue == sorted(overdue, reverse=True) and overdue, first
        assert first[0]["quiet"] == "1"  # the first press shows what needs you most
        assert all(r["folded"] == "1" for r in first[-2:]), first  # closed rows last
        _press(page, "due")  # reversed: still closed rows last
        second = page.evaluate(dues)
        assert second[-1]["folded"] == "1"
        assert all(r["folded"] == "1" for r in second[-2:]), second
        assert not errors, errors
    finally:
        ctx.close()


def test_rows_glide_to_their_new_place_and_reduced_motion_snaps(live):
    ctx, page, errors, url = _open_table(live)
    try:
        page.goto(url)
        page.wait_for_selector(".ap-table tbody tr")
        wait_for_layout(page)
        page.evaluate("document.querySelector('th[data-sort=role] button').click()")
        # The server's table is swapped in, then the moved rows animate from their old places.
        page.wait_for_function("""() => Array.from(document.querySelectorAll('.ap-table tbody tr'))
            .reduce((k, r) => k + r.getAnimations().length, 0) >= 2""", timeout=8000)
    finally:
        ctx.close()
    ctx, page, errors, url = _open_table(live, reduced_motion="reduce")
    try:
        page.goto(url)
        page.wait_for_selector(".ap-table tbody tr")
        seen = page.evaluate("""() => new Promise(res => {
            let most = 0;
            const tick = () => { most = Math.max(most, Array.from(
              document.querySelectorAll('.ap-table tbody tr'))
              .reduce((k, r) => k + r.getAnimations().length, 0)); };
            const t = setInterval(tick, 16);
            document.querySelector('th[data-sort=role] button').click();
            setTimeout(() => { clearInterval(t); res(most); }, 1500); })""")
        _wait_sort(page, "role", 1)
        assert seen == 0, seen
    finally:
        ctx.close()


def test_the_app_reduce_motion_switch_also_stops_the_row_glide(live):
    """Fable C2: html[data-reduce-motion="1"] (Settings > Reduce motion) turns the sort glide off
    like the OS setting does; the CSS rule alone cannot stop Element.animate."""
    ctx, page, errors, url = _open_table(live)
    try:
        page.goto(url)
        page.wait_for_selector(".ap-table tbody tr")
        wait_for_layout(page)
        page.evaluate("document.documentElement.setAttribute('data-reduce-motion', '1')")
        seen = page.evaluate("""() => new Promise(res => {
            let most = 0;
            const tick = () => { most = Math.max(most, Array.from(
              document.querySelectorAll('.ap-table tbody tr'))
              .reduce((k, r) => k + r.getAnimations().length, 0)); };
            const t = setInterval(tick, 16);
            document.querySelector('th[data-sort=role] button').click();
            setTimeout(() => { clearInterval(t); res(most); }, 1500); })""")
        _wait_sort(page, "role", 1)
        assert seen == 0, seen
        assert not errors, errors
    finally:
        ctx.close()


def _paged_text(page) -> str:
    return page.locator('[data-testid="table-page"]').inner_text()


def test_a_new_search_opens_on_the_first_page_and_a_trailing_space_keeps_the_totals(
    live, monkeypatch
):
    """Fable C7: (a) typing a search on table page 3 shows the first matches, not page 3 of them;
    (b) 'e ' in Company counts the same matches as 'e' (the server strips the space)."""
    monkeypatch.setattr("kestrel.web.pages.pursuits.TABLE_PAGE_SIZE", 3)
    base, browser, track, _ids = live
    ctx, page, errors, url = _open_table(live, "&view=table&page=3")
    try:
        page.goto(url)
        page.wait_for_selector('[data-testid="table-page"]')
        assert "Page 3 of" in _paged_text(page)
        page.fill("#ap-q", "a")
        page.wait_for_function(
            "() => { const e = document.querySelector('[data-testid=table-page]');"
            " return !!e && /matching/.test(e.textContent); }", timeout=8000)
        text = _paged_text(page)
        assert "Showing 1 to 3 of" in text and "Page 1 of" in text, text
        page.fill("#ap-q", "")
        page.wait_for_function(
            "() => { const e = document.querySelector('[data-testid=table-page]');"
            " return !!e && !/matching/.test(e.textContent); }", timeout=8000)
        page.click('[data-testid="more-filters"]')
        page.fill('[data-f="company"]', "e")
        page.wait_for_function(
            "() => { const e = document.querySelector('[data-testid=table-page]');"
            " return !!e && /matching/.test(e.textContent); }", timeout=8000)
        exact = _paged_text(page)
        page.fill('[data-f="company"]', "e ")
        page.wait_for_timeout(900)
        assert _paged_text(page) == exact, (exact, _paged_text(page))
        assert not errors, errors
    finally:
        ctx.close()


def test_the_pager_keeps_the_keyboard_on_the_button_that_was_pressed(live, monkeypatch):
    """Fable C4: the pager is replaced with the board; Next stays focused while there is a next."""
    monkeypatch.setattr("kestrel.web.pages.pursuits.TABLE_PAGE_SIZE", 3)
    ctx, page, errors, url = _open_table(live)
    try:
        page.goto(url)
        page.wait_for_selector('[data-testid="table-next"]')
        page.focus('[data-testid="table-next"]')
        page.keyboard.press("Enter")
        page.wait_for_function("() => /Page 2 of/.test(document.querySelector("
                               "'[data-testid=table-page]').textContent)", timeout=8000)
        page.wait_for_timeout(300)
        assert page.evaluate("document.activeElement.getAttribute('data-testid')") == "table-next"
        assert not errors, errors
    finally:
        ctx.close()


def test_the_sort_and_the_view_survive_a_swap_without_a_flash(live):
    """#137: after the board swaps (htmx), the sort is applied before paint (the order is already
    right in the afterSwap handler) and the saved view is rendered by the server, with no click
    on mount."""
    base, browser, track, _ids = live
    ctx, page, errors, url = _open_table(live)
    try:
        page.goto(url)
        page.wait_for_selector(".ap-table tbody tr")
        _press(page, "role")
        _press(page, "role")  # descending
        expect = page.evaluate(_TITLES)
        got = swap_result(
            page, "/pursuits?view=table",
            "htmx.ajax('GET', '/pursuits?view=table&track=' + arg,"
            " {target: '#apps-board', swap: 'outerHTML'});",
            """return {titles: Array.from(document.querySelectorAll('.ap-table tbody tr .ap-tt b'))
                .map(b => b.textContent.trim()),
              sort: document.querySelector('th[data-sort=role]').getAttribute('aria-sort')};""",
            track)
        assert got["titles"] == expect and got["sort"] == "descending", got
        # The choice of Table is kept by cookie: a fresh load with no view in the URL renders the
        # table in the server's own HTML (no board first, then a switch).
        page.click('[data-testid="view-board"]')
        page.wait_for_selector('[data-testid="apps-board-grid"]')
        page.click('[data-testid="view-table"]')
        page.wait_for_selector('[data-testid="apps-table"]')
        html = page.request.get(f"{base}/pursuits?track={track}").text()
        assert 'data-testid="apps-table"' in html and 'data-testid="apps-board-grid"' not in html
        page.click('[data-testid="view-board"]')
        page.wait_for_selector('[data-testid="apps-board-grid"]')
        html = page.request.get(f"{base}/pursuits?track={track}").text()
        assert 'data-testid="apps-board-grid"' in html
        assert not errors, errors
    finally:
        ctx.close()


def test_sorting_and_the_view_work_with_storage_blocked(live):
    block = ("Object.defineProperty(window, 'localStorage', "
             "{get() { throw new Error('blocked'); }});")
    ctx, page, errors, url = _open_table(live)
    page.add_init_script(block)
    try:
        page.goto(url)
        page.wait_for_selector(".ap-table tbody tr")
        _press(page, "role")
        titles = page.evaluate(_TITLES)
        assert titles == sorted(titles, key=str.lower)
        page.click('[data-testid="view-board"]')
        page.wait_for_selector('[data-testid="apps-board-grid"]')
        assert not errors, errors
    finally:
        ctx.close()



_ROWS = """() => Array.from(document.querySelectorAll('.ap-table tbody tr')).map(r => ({
  title: r.querySelector('.ap-tt b').textContent.trim(), col: r.dataset.col,
  fit: r.dataset.fit, applied: r.dataset.applied, ord: Number(r.dataset.ord)}))"""
_PIPELINE = {"to_apply": 0, "to_contact": 0, "applied": 1, "conversation": 1,
             "interviewing": 2, "proposal": 2, "closed": 3}


def _sorted_by(page, col: str) -> list[dict]:
    _press(page, col)
    return page.evaluate(_ROWS)


def _empties_last(values: list[str]) -> bool:
    filled = [v != "" for v in values]
    return filled == sorted(filled, reverse=True)


def test_each_header_sorts_reverses_and_the_third_press_returns_the_default_order(live):
    """#121: Stage, Fit and Applied sort (stage in pipeline order, not by name), a second press
    reverses, a third restores the server's order, empty values are last both ways, and the row
    count never changes."""
    ctx, page, errors, url = _open_table(live)
    try:
        page.goto(url)
        page.wait_for_selector(".ap-table tbody tr")
        wait_for_layout(page)
        default = page.evaluate(_ROWS)
        ords = [r["ord"] for r in default]
        assert len(default) >= 8 and ords == sorted(ords)
        # Stage: pipeline order (to apply, then applied, interviewing, closed), then reversed.
        up = _sorted_by(page, "stage")
        ranks = [_PIPELINE[r["col"]] for r in up]
        assert ranks == sorted(ranks) and len(set(ranks)) > 2, ranks
        assert page.get_attribute("th[data-sort=stage]", "aria-sort") == "ascending"
        down = _sorted_by(page, "stage")
        ranks = [_PIPELINE[r["col"]] for r in down]
        assert ranks == sorted(ranks, reverse=True)
        assert page.get_attribute("th[data-sort=stage]", "aria-sort") == "descending"
        back = _sorted_by(page, "stage")  # third press: default order, no sort
        assert back == default
        assert page.get_attribute("th[data-sort=stage]", "aria-sort") == "none"
        # Fit: highest first, unscored last in both directions.
        best = _sorted_by(page, "fit")
        scores = [r["fit"] for r in best]
        nums = [float(x) for x in scores if x != ""]
        assert nums == sorted(nums, reverse=True) and len(nums) >= 5 and _empties_last(scores)
        worst = _sorted_by(page, "fit")
        nums = [float(x) for x in (r["fit"] for r in worst) if x != ""]
        assert nums == sorted(nums) and _empties_last([r["fit"] for r in worst])
        assert _sorted_by(page, "fit") == default
        # Applied: most recent first, roles not yet applied for last in both directions.
        newest = _sorted_by(page, "applied")
        days = [r["applied"] for r in newest]
        filled = [d for d in days if d]
        assert filled == sorted(filled, reverse=True) and len(filled) >= 3 and _empties_last(days)
        assert any(d == "" for d in days)  # the seed has roles not yet applied for
        oldest = _sorted_by(page, "applied")
        days = [r["applied"] for r in oldest]
        filled = [d for d in days if d]
        assert filled == sorted(filled) and _empties_last(days)
        assert _sorted_by(page, "applied") == default
        for rows in (up, down, best, worst, newest, oldest):
            assert len(rows) == len(default) and {r["title"] for r in rows} == {
                r["title"] for r in default}
        assert not errors, errors
    finally:
        ctx.close()


def test_the_sort_and_the_view_are_remembered_across_a_revisit(live):
    """#121: sort by Stage, leave through the sidebar and come back through it with no view in
    the address: the table opens already sorted, from the server's table plus the saved sort."""
    base, browser, track, _ids = live
    ctx, page, errors, url = _open_table(live)
    try:
        page.goto(url)
        page.wait_for_selector(".ap-table tbody tr")
        page.click('[data-testid="view-board"]')
        page.wait_for_selector('[data-testid="apps-board-grid"]')
        page.click('[data-testid="view-table"]')  # the person's own choice is what is stored
        page.wait_for_selector(".ap-table tbody tr")
        _sorted_by(page, "stage")
        _sorted_by(page, "stage")  # descending
        expect = page.evaluate(_ROWS)
        page.goto(f"{base}/?track={track}")
        page.wait_for_selector('a[href^="/pursuits"]', state="attached")
        link = page.locator('a[href^="/pursuits"]:visible').first
        assert "view=" not in (link.get_attribute("href") or "")  # the sidebar names no view
        link.click()
        page.wait_for_selector(".ap-table tbody tr")
        assert page.get_attribute("th[data-sort=stage]", "aria-sort") == "descending"
        assert page.evaluate(_ROWS) == expect
        assert not errors, errors
    finally:
        ctx.close()


def test_the_applied_date_follows_the_date_format_setting(live):
    base, browser, track, _ids = live
    ctx, page, errors, url = _open_table(live)
    try:
        for fmt, make in (
            ("DD/MM/YYYY", lambda d: f"{d[8:10]}/{d[5:7]}/{d[0:4]}"),
            ("YYYY-MM-DD", lambda d: d),
            ("MM/DD/YYYY", lambda d: f"{d[5:7]}/{d[8:10]}/{d[0:4]}"),
        ):
            r = page.request.post(f"{base}/settings/ui", data={"key": "region.date_format",
                                                                "value": fmt})
            assert r.ok, r.text()
            page.goto(url)
            page.wait_for_selector(".ap-table tbody tr")
            cells = page.evaluate("""() =>
              Array.from(document.querySelectorAll('.ap-table tbody tr'))
              .map(r => [r.dataset.applied, r.querySelector('[data-testid=applied-cell]')
                .textContent.trim()])""")
            applied = [(d, t) for d, t in cells if d]
            assert applied
            for day, shown in applied:
                assert shown == make(day), (fmt, day, shown)
        assert not errors, errors
    finally:
        page.request.post(f"{base}/settings/ui", data={"key": "region.date_format",
                                                       "value": "MM/DD/YYYY"})
        ctx.close()


@pytest.mark.parametrize("width", [390, 700, 860, 1000])
def test_the_date_column_hides_before_the_table_overlaps(live, width):
    """#121: the Applied column is shown only when the table has room for it; where it is shown,
    no cell's text spills into the next one and the table does not scroll sideways."""
    base, browser, track, _ids = live
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    page = ctx.new_page()
    try:
        page.goto(f"{base}/pursuits?track={track}&view=table")
        page.wait_for_selector(".ap-table tbody tr")
        wait_for_layout(page)
        got = page.evaluate("""() => {
          const wrap = document.querySelector('.ap-tablewrap');
          const head = document.querySelector('th.ap-c-date');
          const row = '.ap-table tbody tr:first-child td';
          const cells = Array.from(document.querySelectorAll(row));
          const boxes = cells.filter(c => c.offsetParent !== null)
            .map(c => c.getBoundingClientRect());
          let overlap = false;
          for (let i = 1; i < boxes.length; i++) {
            if (boxes[i].left < boxes[i - 1].right - 1) overlap = true;
          }
          return {shown: getComputedStyle(head).display !== 'none', overlap,
                  scrolls: wrap.scrollWidth > wrap.clientWidth + 1,
                  bodyShown: cells.some(c => c.classList.contains('ap-c-date')
                    && c.offsetParent !== null),
                  room: wrap.clientWidth};
        }""")
        assert got["shown"] == got["bodyShown"], got  # head and cells leave together
        assert not got["overlap"], got
        assert not (got["shown"] and got["scrolls"]), got  # shown only where it fits
        assert got["shown"] == (width >= 1000), got  # hidden on the narrow windows, shown wide
    finally:
        ctx.close()


_APPLIED_DAYS = ("() => Array.from(document.querySelectorAll('.ap-table tbody tr'))"
                 ".map(r => r.dataset.applied)")


def test_sort_by_date_then_reload_and_revisit_keep_the_view_with_no_flash(live):
    """#137: Date applied sorts newest first with undated roles last; a reload and a return with
    no view in the address draw the table already sorted (the server's first paint, so the rows
    never move after load); an explicit ?view=kanban still wins."""
    base, browser, track, _ids = live
    ctx, page, errors, url = _open_table(live)
    try:
        page.goto(url)
        page.wait_for_selector(".ap-table tbody tr")
        page.click('[data-testid="view-board"]')
        page.wait_for_selector('[data-testid="apps-board-grid"]')
        page.click('[data-testid="view-table"]')  # the person's own choice is what is stored
        page.wait_for_selector(".ap-table tbody tr")
        _press(page, "applied")
        days = page.evaluate(_APPLIED_DAYS)
        filled = [d for d in days if d]
        assert filled == sorted(filled, reverse=True) and len(filled) >= 3 and _empties_last(days)
        sorted_titles = page.evaluate(_TITLES)
        # The order at DOMContentLoaded (before any script work) is already the sorted one.
        page.add_init_script("""document.addEventListener('DOMContentLoaded', () => {
            window.__first = Array.from(document.querySelectorAll('.ap-table tbody tr .ap-tt b'))
              .map(b => b.textContent.trim());
            window.__aria = (document.querySelector('th[data-sort=applied]') || {})
              .getAttribute?.('aria-sort'); });""")
        page.goto(f"{base}/pursuits?track={track}")  # no view, no sort in the address
        page.wait_for_selector(".ap-table tbody tr")
        wait_for_layout(page)
        assert page.evaluate("window.__first") == sorted_titles == page.evaluate(_TITLES)
        assert page.evaluate("window.__aria") == "descending"
        assert page.get_attribute("th[data-sort=applied]", "aria-sort") == "descending"
        # The explicit view in the address wins over the stored Table choice.
        page.goto(f"{base}/pursuits?track={track}&view=kanban")
        page.wait_for_selector('[data-testid="apps-board-grid"]')
        assert page.locator(".ap-table").count() == 0
        # A third press returns to the default and that is what comes back too.
        page.goto(f"{base}/pursuits?track={track}&view=table")
        page.wait_for_selector(".ap-table tbody tr")
        _press(page, "applied")  # oldest first
        _press(page, "applied")  # third: default order
        page.goto(f"{base}/pursuits?track={track}")
        page.wait_for_selector(".ap-table tbody tr")
        assert page.get_attribute("th[data-sort=applied]", "aria-sort") == "none"
        assert not errors, errors
    finally:
        ctx.close()


def test_the_sort_is_usable_from_the_keyboard_alone(live):
    """#137: Tab reaches a header button, Enter and Space press it, and focus is still on that
    button after the rows move, so the next press needs no mouse."""
    ctx, page, errors, url = _open_table(live)
    try:
        page.goto(url)
        page.wait_for_selector(".ap-table tbody tr")
        wait_for_layout(page)
        page.focus("th[data-sort=applied] button")
        assert page.evaluate("document.activeElement.textContent.trim()") == "Date applied"
        page.keyboard.press("Enter")
        _wait_sort(page, "applied", -1)
        assert page.evaluate("document.activeElement.closest('th').dataset.sort") == "applied"
        page.keyboard.press("Space")
        _wait_sort(page, "applied", 1)
        days = page.evaluate(_APPLIED_DAYS)
        filled = [d for d in days if d]
        assert filled == sorted(filled) and _empties_last(days)
        page.keyboard.press("Enter")  # third press: the default order
        _wait_sort(page, "", 1)
        assert page.get_attribute("th[data-sort=applied]", "aria-sort") == "none"
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1024, 1440])
def test_the_sorted_table_has_no_overlap_or_sideways_scroll(live, theme, width):
    """#137: sorted by Date applied, light and dark, at the narrow 1024 and a wide window: header
    buttons and their direction mark never overlap, the page never scrolls sideways."""
    base, browser, track, _ids = live
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"{base}/pursuits?track={track}&view=table")
        page.wait_for_selector(".ap-table tbody tr")
        wait_for_layout(page)
        _press(page, "applied")
        wait_for_layout(page)
        _shot(page, f"apps-table-sorted-{track}-{theme}-{width}")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        boxes = page.evaluate(
            """() => Array.from(document.querySelectorAll('.ap-table th button'))
              .filter(b => b.offsetParent !== null).map(b => { const r = b.getBoundingClientRect();
              return {t: b.textContent.trim(), l: r.left, r: r.right, tp: r.top,
                bt: r.bottom}; })""")
        assert len(boxes) >= 4
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                assert not _overlaps(a, b), (a, b)
        assert not errors, errors
    finally:
        ctx.close()
