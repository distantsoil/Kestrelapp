"""Help and About in a real browser (UI overhaul parity check against the locked mockups,
the locked design mockup (kept privately) and about-mockup-locked.html). Light and dark at
1280 and 1680: no overlap, no wrapping titles or buttons, no horizontal scroll; then the
interactions the mockups demo (search, topic chips, the reader, flyouts, copy, the toggles).

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1. The browser is the Edge
already on the machine, else a Playwright Chromium (tests/browser.py)."""

from __future__ import annotations

import os

import pytest
from playwright.sync_api import expect

from tests.browser import (
    LINES_JS,
    TIMER_PROBE_JS,
    assert_page_remounts_clean,
    launch_sync,
    wait_for_layout,
)

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

_KIDS = """el => Array.from(el.children).filter(e => e.offsetParent !== null)
  .map(e => { const b = e.getBoundingClientRect();
    return {t: (e.textContent || '').trim().slice(0, 30), l: b.left, r: b.right, tp: b.top,
            bt: b.bottom}; })"""


def _overlaps(a, b) -> bool:
    return a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1 and a["tp"] < b["bt"] - 1 \
        and b["tp"] < a["bt"] - 1


@pytest.fixture
def live():
    """A server on the test's own throwaway data directory and a browser."""
    from playwright.sync_api import sync_playwright

    from kestrel.db import migrate
    from kestrel.web.serve import running_server

    migrate.run_migrations()
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
    page.wait_for_selector("h1")
    wait_for_layout(page)
    return ctx, page, errors


def _check_layout(page, theme, width, *, single_line, groups, main_sel):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 0") is True
    assert page.evaluate("document.querySelector('.main, main').scrollWidth <= "
                         "document.querySelector('.main, main').clientWidth + 1") is True
    for sel in single_line:
        for el in page.query_selector_all(sel):
            if el.is_visible():
                assert page.evaluate(_LINES, el) <= 1, (sel, theme, width, el.inner_text()[:40])
    for container in groups:
        for group in page.query_selector_all(container):
            if not group.is_visible():
                continue
            kids = page.evaluate(_KIDS, group)
            for i, a in enumerate(kids):
                for b in kids[i + 1:]:
                    assert not _overlaps(a, b), (container, a, b, theme, width)
    # The help column is level with the first card (wide) or below the main column (narrow),
    # never over it; the band's art stays inside the content column.
    main = page.evaluate(_BOXES, main_sel)[0]
    help_col = page.evaluate(_BOXES, ".k-help-col")[0]
    assert not _overlaps(main, help_col), (main, help_col)
    if width >= 1680:
        assert help_col["l"] >= main["r"] - 1, (help_col, main)
    else:
        assert help_col["tp"] >= main["bt"] - 1, (help_col, main)
    scene = page.evaluate(_BOXES, ".k-scene")[0]
    # The band runs to the window's right edge (the shell, as the mockups draw it).
    assert scene["r"] <= page.evaluate("innerWidth") + 1 and scene["l"] >= main["l"] - 1
    title = page.evaluate(_BOXES, ".k-scene__title")[0]
    art = page.evaluate(_BOXES, ".k-scene__art")
    if art:
        # The art is decorative and sits behind the band (the shell, as drawn); About's longer
        # subtitle may run a little under it at 1280.
        assert title["r"] <= art[0]["l"] + 130, (title, art[0])
    assert not page.query_selector("#announcement-slot")
    assert page.evaluate("[...document.querySelectorAll('link[rel=stylesheet]')]"
                         ".every(l => !l.href.endsWith('/css/kestrel.css'))") is True


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_help_layout_has_no_overlap_wrapping_or_sideways_scroll(live, theme, width):
    ctx, page, errors = _open(live, "/help", theme, width)
    try:
        _check_layout(
            page, theme, width,
            single_line=("h1", ".k-chip", ".hp-step b", ".hp-step__cost", ".hp-art__t",
                         ".hp-acts .k-btn", ".k-hcard h3", ".k-scene .k-pill"),
            groups=(".hp-steps", ".hp-topics", ".hp-acts", "#hp-chips"),
            main_sel=".k-layout__main",
        )
        # Four steps in a row on a wide window, two by two when narrow (the container query).
        steps = page.evaluate(_BOXES, ".hp-step")
        assert len(steps) == 4
        if width >= 1680:
            assert len({round(s["tp"]) for s in steps}) == 1, steps
        # The search sits above the chips, the chips above the steps.
        search = page.evaluate(_BOXES, ".hp-search")[0]
        chips = page.evaluate(_BOXES, "#hp-chips")[0]
        assert search["bt"] <= chips["tp"] + 1 and chips["bt"] <= steps[0]["tp"] + 1
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_about_layout_has_no_overlap_wrapping_or_sideways_scroll(live, theme, width):
    ctx, page, errors = _open(live, "/about", theme, width)
    try:
        _check_layout(
            page, theme, width,
            single_line=("h1", ".ab-vtag", ".ab-pr b", ".ab-mk span", ".ab-upd .k-btn",
                         ".k-hcard h3", ".ab-lnk .k-btn", ".ab-frow__l .k-btn"),
            groups=(".ab-id", ".ab-prom", ".ab-mk", ".ab-frow__l"),
            main_sel=".k-layout__main",
        )
        rows = page.evaluate(_BOXES, ".ab-pr")
        assert len(rows) == 5
        mark = page.evaluate(_BOXES, ".ab-mark")[0]
        assert 80 <= mark["r"] - mark["l"] <= 90
        assert not errors, errors
    finally:
        ctx.close()


def test_help_search_chips_reader_and_flyouts_work(live):
    ctx, page, errors = _open(live, "/help", "light", 1680)
    try:
        topics = lambda: [e for e in page.query_selector_all(".hp-topic") if e.is_visible()]  # noqa: E731
        assert len(topics()) == 5
        # "/" focuses the search; typing filters the list live and marks the match.
        page.keyboard.press("/")
        assert page.evaluate("document.activeElement.id") == "hp-q"
        page.keyboard.type("slow")
        expect(page.locator(".hp-art mark").first).to_be_visible()
        # The search reads whole articles, so what stays visible has "slow" in its article body
        # (title or text) and what is hidden does not.
        verdicts = page.evaluate("""() => [...document.querySelectorAll('.hp-art')].map(b => {
          const a = document.getElementById('hp-a-' + b.getAttribute('data-help-article')
            .replace(':', '-'));
          const text = ((a && (a.getAttribute('data-title') + ' ' + a.textContent)) || '')
            .toLowerCase();
          return {shown: b.offsetParent !== null, hit: text.includes('slow')}; })""")
        assert any(v["shown"] for v in verdicts)
        assert all(v["shown"] == v["hit"] for v in verdicts), verdicts
        assert len(topics()) < 5
        assert page.query_selector("#hp-steps-card").is_hidden()
        assert page.query_selector(".hp-art mark")
        # Nothing matches: the empty card appears.
        page.fill("#hp-q", "zzzz-no-such-thing")
        expect(page.locator("#hp-none")).to_be_visible()
        assert page.query_selector("#hp-none").is_visible() and not topics()
        # Escape clears the search and the list comes back.
        page.keyboard.press("Escape")
        expect(page.locator(".hp-topic:visible")).to_have_count(5)
        assert page.input_value("#hp-q") == "" and len(topics()) == 5
        # A topic chip narrows the list; All topics restores it.
        page.click('#hp-chips [data-topic="privacy"]')
        assert len(topics()) == 1 and page.query_selector("#hp-steps-card").is_hidden()
        page.click('#hp-chips [data-topic="score"]')
        assert page.query_selector("#hp-steps-card").is_visible()
        page.click('#hp-chips [data-topic="all"]')
        assert len(topics()) == 5
        # An article opens in the reader, in the app (no navigation), and Escape closes it.
        url = page.url
        page.click('[data-help-article="models:grey"]')
        page.wait_for_selector("#hp-reader.is-open")
        assert page.inner_text("#hp-reader-t") == "Why is this greyed out?"
        assert len(page.inner_text("#hp-reader-body")) > 300 and page.url == url
        page.keyboard.press("Escape")
        page.wait_for_function("!document.querySelector('#hp-reader').classList.contains('is-open')")
        # A step opens the four-steps article.
        page.click(".hp-step >> nth=3")
        page.wait_for_selector("#hp-reader.is-open")
        assert page.inner_text("#hp-reader-t") == "The four steps"
        page.keyboard.press("Escape")
        # A helper flyout opens, and its button opens the full article over it.
        page.click('[data-testid="hp-helper-wrong"] .k-morelink')
        page.wait_for_selector("#hp-fly-wrong.is-open")
        page.click('#hp-fly-wrong [data-help-article="fix:problem"]')
        page.wait_for_selector("#hp-reader.is-open")
        assert page.inner_text("#hp-reader-t") == "Something isn't working"
        assert not page.query_selector("#hp-fly-wrong.is-open")
        assert not errors, errors
    finally:
        ctx.close()


def test_a_link_to_help_lan_opens_the_block_it_names(live):
    base, browser = live
    ctx = browser.new_context(viewport={"width": 1680, "height": 900})
    page = ctx.new_page()
    try:
        # The reference blocks are reader articles now; their anchors open them in the flyout.
        page.goto(f"{base}/help#remoterocketship")
        page.wait_for_selector("#hp-reader.is-open")
        assert page.inner_text("#hp-reader-t") == "Where opportunities come from"
        spot = '#hp-reader-body [data-hp-anchor="remoterocketship"]'
        assert page.query_selector(spot).is_visible()
        # The reader's copy repeats no id from the article it shows.
        dupes = page.evaluate(
            "() => { const seen = {}; const out = [];"
            " document.querySelectorAll('[id]').forEach(n => {"
            " if (seen[n.id]) out.push(n.id); seen[n.id] = 1; }); return out; }"
        )
        assert dupes == []
        # Nor does it repeat a data-testid (D-UI-218): the hooks move to data-hp-testid.
        tdupes = page.evaluate(
            "() => { const seen = {}; const out = [];"
            " document.querySelectorAll('[data-testid]').forEach(n => {"
            " const t = n.getAttribute('data-testid');"
            " if (seen[t]) out.push(t); seen[t] = 1; }); return out; }"
        )
        assert tdupes == []
        assert page.query_selector('#hp-reader-body [data-hp-testid="hp-sources"]')
        assert page.query_selector("#hp-reader-body [data-testid]") is None
        page.goto(f"{base}/help#lan")
        page.wait_for_selector("#hp-reader.is-open")
        assert page.inner_text("#hp-reader-t") == "Kestrel from another device"
        page.goto(f"{base}/help#fix:problem")
        page.wait_for_selector("#hp-reader.is-open")
    finally:
        ctx.close()


def test_about_promises_flyouts_copy_and_toggles_work(live):
    ctx, page, errors = _open(live, "/about", "light", 1680)
    try:
        ctx.grant_permissions(["clipboard-read", "clipboard-write"])
        page.click(".ab-pr >> nth=2")
        page.wait_for_selector("#ab-fly-p2.is-open")
        assert "Never applies for you" in page.inner_text("#ab-fly-p2")
        page.keyboard.press("Escape")
        # Component versions and the notices open and close; the notices load as text.
        page.click("#ab-versions > summary")
        assert page.query_selector("#ab-comp").is_visible()
        page.click("#ab-notices > summary")
        page.wait_for_function("document.querySelector('#ab-notes-text').textContent.length > 200")
        assert "Loading" not in page.inner_text("#ab-notes-text")
        # Copy versions puts the page's own version on the clipboard.
        page.click('[data-testid="copy-versions"]')
        expect(page.get_by_test_id("copy-versions")).to_have_text("Copied")
        clip = page.evaluate("navigator.clipboard.readText()")
        shown = page.inner_text("#about-version").replace("Version ", "")
        assert clip.splitlines()[0] == f"Kestrel {shown}" and "Python" in clip
        # The data folder's Copy button copies the path shown.
        page.click('[data-copy-target="ab-path-data"]')
        expect(page.locator('[data-copy-target="ab-path-data"]')).to_have_text("Copied")
        assert page.evaluate("navigator.clipboard.readText()") == page.inner_text(
            '[data-testid="path-data"]').strip()
        # Check for updates swaps its answer into the card (the route is faked: no network).
        page.route("**/updates/check", lambda route: route.fulfill(
            status=200, content_type="text/html",
            body='<span class="ab-res is-good" data-testid="update-result">'
                 "You have the latest version</span>"
                 '<span id="update-last-checked" class="ab-last" hx-swap-oob="true">'
                 "Last checked: just now</span>"))
        page.click('[data-testid="check-updates"]')
        page.wait_for_selector('[data-testid="update-result"]')
        assert "latest version" in page.inner_text("#update-result")
        assert "just now" in page.inner_text("#update-last-checked")
        # Five taps on the mark: the kestrel glides and the toast speaks.
        for _ in range(5):
            page.click("#ab-mark")
        page.wait_for_selector("#ab-glider.is-go", state="attached")
        assert not errors, errors
    finally:
        ctx.close()


def test_a_link_to_about_diagnostics_opens_the_toggle_it_names(live):
    base, browser = live
    ctx = browser.new_context(viewport={"width": 1680, "height": 900})
    page = ctx.new_page()
    try:
        # Diagnostics live in the Something wrong? flyout; the anchor opens it.
        page.goto(f"{base}/about#diagnostics")
        page.wait_for_selector("#ab-fly-wrong.is-open")
        assert page.query_selector("#diagnostics a, #diagnostics button").is_visible()
    finally:
        ctx.close()


# Counts the calls a page makes into the shared overlay and toast helpers, so a doubled listener
# (two sets of handlers answering one click) shows as 2 where one set shows 1.
_COUNT_CALLS_JS = """names => { window.__calls = {};
  for (const n of names) { window.__calls[n] = 0; const o = KUI[n];
    KUI[n] = function () { window.__calls[n]++; return o.apply(this, arguments); }; } }"""


def test_help_mounts_unmounts_and_mounts_again_with_one_set_of_listeners(live):
    """The page lifecycle (D-UI-210): Help unmounted and mounted again in place starts exactly
    what it started on the document load and leaves nothing behind; afterwards one click opens
    one article (a doubled listener opens it twice), search and the hash still work, and
    unmounting with an article open closes the reader."""
    base, browser = live
    ctx = browser.new_context(viewport={"width": 1680, "height": 900}, color_scheme="light")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(TIMER_PROBE_JS)
    try:
        page.goto(f"{base}/help")
        page.wait_for_selector("h1")
        wait_for_layout(page)
        first = assert_page_remounts_clean(page, "help", "help.js", settle_ms=400, quiet_ms=600)
        assert first["timers"] == 0 and first["observers"] == 0
        page.evaluate(_COUNT_CALLS_JS, ["openFlyout"])
        page.click('[data-help-article="models:grey"]')
        page.wait_for_selector("#hp-reader.is-open")
        assert page.evaluate("window.__calls.openFlyout") == 1
        page.keyboard.press("Escape")
        page.wait_for_function("!document.querySelector('#hp-reader').classList.contains('is-open')")
        # The search and the topic chips run off the second mount's state, not the first's.
        page.fill("#hp-q", "zzzz-no-such-thing")
        page.wait_for_selector("#hp-none", state="visible")
        page.fill("#hp-q", "")
        page.click('#hp-chips [data-topic="privacy"]')
        assert len([e for e in page.query_selector_all(".hp-topic") if e.is_visible()]) == 1
        page.click('#hp-chips [data-topic="all"]')
        # A hash change opens its article once, and only while the page is mounted.
        page.evaluate("window.__calls.openFlyout = 0")
        page.evaluate("location.hash = '#lan'")
        page.wait_for_selector("#hp-reader.is-open")
        assert page.inner_text("#hp-reader-t") == "Kestrel from another device"
        assert page.evaluate("window.__calls.openFlyout") == 1
        # Leaving with the reader open closes it, and nothing the page started answers afterwards.
        page.evaluate("KUI.page.unmount()")
        assert not page.query_selector("#hp-reader.is-open")
        page.evaluate("window.__calls.openFlyout = 0")
        page.evaluate("location.hash = '#sources'")
        page.click('[data-help-article="models:grey"]')
        page.wait_for_timeout(300)
        assert page.evaluate("window.__calls.openFlyout") == 0
        assert not errors, errors
    finally:
        ctx.close()


def test_about_mounts_unmounts_and_mounts_again_with_one_set_of_listeners(live):
    """The page lifecycle (D-UI-210): About unmounted and mounted again in place leaves no
    listener, timer or fetch behind; afterwards three taps on the mark do not release the kestrel
    (a doubled listener counts each tap twice and does), five do, with one toast. A tap timer
    pending at unmount is cleared, and a notices fetch cut off by it is finished on the next
    mount."""
    base, browser = live
    ctx = browser.new_context(viewport={"width": 1680, "height": 900}, color_scheme="light")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(TIMER_PROBE_JS)
    try:
        page.goto(f"{base}/about")
        page.wait_for_selector("h1")
        wait_for_layout(page)
        first = assert_page_remounts_clean(page, "about", "about.js", settle_ms=400, quiet_ms=600)
        assert first["timers"] == 0 and first["observers"] == 0
        page.evaluate(_COUNT_CALLS_JS, ["toast"])
        for _ in range(3):
            page.click("#ab-mark")
        assert not page.query_selector("#ab-glider.is-go")
        assert page.evaluate("window.__calls.toast") == 0
        # The pending tap timer (the count resets after 1.5 s) goes with the page.
        assert page.evaluate("KUI.page._stats().timers") == 1
        page.evaluate("KUI.page.unmount()")
        assert page.evaluate("window.__kTimers.pending('/static/js/about.js')") == 0
        page.click("#ab-mark")  # unmounted: nothing answers
        assert page.evaluate("KUI.page._stats()")["listeners"] == 0
        assert page.evaluate("KUI.page.mount()") is True
        for _ in range(5):
            page.click("#ab-mark")
        page.wait_for_selector("#ab-glider.is-go", state="attached")
        assert page.evaluate("window.__calls.toast") == 1
        # Leaving while the notices load drops the answer; the next mount loads them.
        page.evaluate("""async () => {
          const d = document.getElementById('ab-notices');
          const toggled = new Promise(r => d.addEventListener('toggle', r, { once: true }));
          d.open = true;
          await toggled;
          KUI.page.unmount();
        }""")
        page.wait_for_timeout(400)
        assert page.inner_text("#ab-notes-text") == "Loading…"
        assert page.get_attribute("#ab-notices", "data-loaded") is None
        assert page.evaluate("KUI.page.mount()") is True
        page.wait_for_function("document.querySelector('#ab-notes-text').textContent.length > 200")
        assert page.get_attribute("#ab-notices", "data-loaded") == "1"
        assert not errors, errors
    finally:
        ctx.close()
