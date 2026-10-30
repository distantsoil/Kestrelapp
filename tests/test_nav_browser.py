"""In-place navigation in a real browser (D-UI-210, D-UI-220): a page change swaps the content
under the window, the sidebar and the scene art instead of loading a new document, so WebView2
never shows its bare background between two pages. The transaction, Back and Forward, a page's
own history entries, the leave guard, the art kept, and the classic fallbacks.
Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import asyncio
import os

import pytest

from tests import today_seed
from tests.browser import launch_sync, wait_for_layout

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

# What a page change leaves behind: the same document (the marker), the page that is mounted,
# where the window says it is, and no navigation still on its way.
_HERE = """() => ({doc: window.__doc || null, page: window.KUI.page.current(),
  at: location.pathname + location.search,
  going: document.documentElement.classList.contains('k-going'),
  title: document.title,
  current: (document.querySelector('#sidebar .mainnav > a[aria-current]')
    || {getAttribute: () => null}).getAttribute('href'),
  sheets: Array.from(document.head.querySelectorAll('link[rel=stylesheet]'))
    .filter(l => !l.media || l.media === 'all').map(l => new URL(l.href).pathname),
  pending: document.querySelectorAll('link[data-k-pending]').length,
  stats: window.KUI.page._stats()})"""


# Live listeners on document and window, from any script (a page's raw addEventListener, an
# inline partial script run again), not only those a page holds through its ctx.
_SHELL_LISTENERS_JS = """(() => {
  const reg = [];
  const add = EventTarget.prototype.addEventListener;
  const rm = EventTarget.prototype.removeEventListener;
  const cap = o => (o && typeof o === 'object') ? !!o.capture : !!o;
  EventTarget.prototype.addEventListener = function (type, fn, opts) {
    if (this === window || this === document) {
      reg.push({ t: this, type, fn, cap: cap(opts),
                 signal: (opts && typeof opts === 'object') ? opts.signal : null, gone: false });
    }
    return add.call(this, type, fn, opts);
  };
  EventTarget.prototype.removeEventListener = function (type, fn, opts) {
    for (const r of reg) {
      if (r.t === this && r.type === type && r.fn === fn && r.cap === cap(opts)) r.gone = true;
    }
    return rm.call(this, type, fn, opts);
  };
  window.__shellListeners = () => reg.filter(r => !r.gone && !(r.signal && r.signal.aborted))
    .length;
})();"""


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    asyncio.run(today_seed.seed())
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


def _open(live, path: str):
    base, browser = live
    ctx = browser.new_context(viewport={"width": 1400, "height": 950})
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}{path}")
    page.wait_for_function("() => window.KUI && window.KUI.page.current()")
    wait_for_layout(page)
    page.evaluate("window.__doc = 'first'")
    return ctx, page, errors


def _go(page, href: str, name: str) -> dict:
    page.click(f'#sidebar .mainnav > a[href="{href}"]')
    page.wait_for_function(
        "n => window.KUI.page.current() === n"
        " && !document.documentElement.classList.contains('k-going')", arg=name)
    wait_for_layout(page)
    return page.evaluate(_HERE)


def _fresh_sheets(live, path: str) -> list[str]:
    """The stylesheets a full load of the page puts in its head, in order. (A partial that htmx
    loads later can bring one of its own into the body; that is the partial's, not the page's.)"""
    base, browser = live
    ctx = browser.new_context()
    try:
        p = ctx.new_page()
        p.goto(f"{base}{path}")
        return p.evaluate("() => Array.from(document.head.querySelectorAll('link[rel=stylesheet]'))"
                          ".map(l => new URL(l.href).pathname)")
    finally:
        ctx.close()


def test_a_sidebar_change_swaps_the_page_in_place_and_back_and_forward_follow(live):
    ctx, page, errors = _open(live, "/")
    try:
        first = None
        for href, name, title in [("/opportunities", "pipeline", "Pipeline"),
                                  ("/profile", "profile", "Profile"),
                                  ("/", "today", "Today"),
                                  ("/opportunities", "pipeline", "Pipeline")]:
            here = _go(page, href, name)
            assert here["doc"] == "first", f"{href} loaded a new document"
            assert here["at"] == href and here["current"] == href and title in here["title"]
            # The page's own stylesheets, in a fresh load's order, and nothing left pending.
            assert here["sheets"] == _fresh_sheets(live, href), href
            assert here["pending"] == 0
            if name == "pipeline":
                # Coming back mounts exactly one set of listeners and timers, not two.
                first = first or here["stats"]
                assert here["stats"] == first
        page.go_back()
        page.wait_for_function(
            "() => window.KUI.page.current() === 'today' && location.pathname === '/'")
        page.go_back()
        page.wait_for_function("() => window.KUI.page.current() === 'profile'")
        page.go_forward()
        page.wait_for_function("() => window.KUI.page.current() === 'today'")
        assert page.evaluate("() => window.__doc") == "first"
        assert not errors, errors
    finally:
        ctx.close()


def test_the_scene_art_stays_when_the_next_page_draws_the_same_scene(live):
    ctx, page, errors = _open(live, "/opportunities")
    try:
        page.evaluate("document.querySelector('.k-scene__art').__kept = 1")
        _go(page, "/profile", "profile")
        # The same drawing moved into the next band: not redrawn, its animation not restarted.
        assert page.evaluate("() => document.querySelector('.k-scene__art').__kept") == 1
        scenes = "() => document.querySelectorAll('.k-scene .k-scene__scene').length"
        assert page.evaluate(scenes) == 1
        assert not errors, errors
    finally:
        ctx.close()


def test_a_pages_own_history_entries_still_work_after_an_in_place_arrival(live):
    ctx, page, errors = _open(live, "/profile")
    try:
        _go(page, "/opportunities", "pipeline")
        page.wait_for_selector('[data-testid="pl-row"]')
        page.click('[data-testid="pl-row"] >> nth=0 >> .pl-title__t')
        page.wait_for_selector("#pl-panel.is-open")
        assert "open=" in page.url
        page.keyboard.press("Escape")
        page.wait_for_function("() => !document.querySelector('#pl-panel.is-open')")
        # Back is Pipeline's own: the panel opens again, the page is not fetched again.
        page.evaluate("document.querySelector('main.container').__same = 1")
        page.go_back()
        page.wait_for_selector("#pl-panel.is-open")
        assert page.evaluate("() => document.querySelector('main.container').__same") == 1
        page.go_back()
        page.wait_for_function("() => !document.querySelector('#pl-panel.is-open')")
        # One more is a page this window showed before: Profile, in place.
        page.go_back()
        page.wait_for_function("() => window.KUI.page.current() === 'profile'")
        assert page.evaluate("() => window.__doc") == "first"
        assert not errors, errors
    finally:
        ctx.close()


def test_unsaved_profile_changes_hold_a_sidebar_click_the_palette_and_back(live):
    ctx, page, errors = _open(live, "/")
    try:
        _go(page, "/profile", "profile")
        page.locator("[data-pf-form] input[type=text]").first.fill("Changed in a test")
        page.click('#sidebar .mainnav > a[href="/"]')
        page.wait_for_timeout(600)
        assert page.evaluate(_HERE)["page"] == "profile"
        assert page.locator("[data-pf-savebar]").is_visible()
        # Back is refused too: the entry is put back and the page stays as it is.
        page.evaluate("history.back()")
        page.wait_for_timeout(1000)
        here = page.evaluate(_HERE)
        assert here["page"] == "profile" and here["at"] == "/profile" and here["doc"] == "first"
        field = page.locator("[data-pf-form] input[type=text]").first
        assert field.input_value() == "Changed in a test"
        # The palette asks the same guard.
        page.evaluate("window.KUI.go('/opportunities')")
        page.wait_for_timeout(600)
        assert page.evaluate(_HERE)["page"] == "profile"
        assert not errors, errors
    finally:
        ctx.close()


def test_classic_page_loads_and_data_k_reload_load_a_new_document(live):
    ctx, page, errors = _open(live, "/")
    try:
        # A link marked data-k-reload is a full page load.
        page.evaluate("document.querySelector('#sidebar .mainnav > a[href=\"/profile\"]')"
                      ".setAttribute('data-k-reload', '')")
        page.click('#sidebar .mainnav > a[href="/profile"]')
        page.wait_for_function(
            "() => !window.__doc && window.KUI && window.KUI.page.current() === 'profile'")
        # Classic page loads (Settings > Preferences) turns the swap off altogether.
        page.evaluate("window.__doc = 'second';"
                      " document.documentElement.setAttribute('data-k-nav', '0')")
        page.click('#sidebar .mainnav > a[href="/"]')
        page.wait_for_function(
            "() => !window.__doc && window.KUI && window.KUI.page.current() === 'today'")
        assert not errors, errors
    finally:
        ctx.close()


def test_a_reply_that_is_not_a_kestrel_page_is_loaded_the_classic_way(live):
    """A missing role answers with a bare 404 page, not a page of the app: the window loads it
    as a new document rather than swapping half a page in."""
    ctx, page, errors = _open(live, "/")
    try:
        page.evaluate("window.KUI.go('/opportunities/987654321')")
        page.wait_for_url("**/opportunities/987654321")
        page.wait_for_load_state("load")
        assert page.evaluate("() => window.__doc || null") is None
        assert not errors, errors
    finally:
        ctx.close()


def test_every_sidebar_page_changes_in_place_and_comes_back_with_one_set_of_listeners(live):
    """Phase 3 (D-UI-210 plan): every page the sidebar reaches, twice round. Each arrives in the
    same document, mounts its own page, and holds the same listeners, timers and observers the
    second time as the first."""
    base, browser = live
    ctx = browser.new_context(viewport={"width": 1400, "height": 950})
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(_SHELL_LISTENERS_JS)
    page.goto(f"{base}/")
    page.wait_for_function("() => window.KUI && window.KUI.page.current()")
    wait_for_layout(page)
    page.evaluate("window.__doc = 'first'")
    try:
        hrefs = page.evaluate("() => Array.from(document.querySelectorAll('#sidebar .mainnav > a'))"
                              ".map(a => a.getAttribute('href'))")
        assert len(hrefs) >= 10, hrefs
        seen: dict[str, dict] = {}
        # Round one loads each page's script once (a shared script adds its one listener then);
        # rounds two and three must match exactly, page by page.
        for rnd in range(3):
            for href in hrefs:
                page.click(f'#sidebar .mainnav > a[href="{href}"]')
                page.wait_for_function(
                    "h => location.pathname === h"
                    " && !document.documentElement.classList.contains('k-going')", arg=href)
                wait_for_layout(page)
                page.wait_for_timeout(250)
                here = page.evaluate(_HERE)
                assert here["doc"] == "first", f"{href} loaded a new document"
                assert here["current"] == href and here["pending"] == 0, (href, here)
                here["shell"] = page.evaluate("() => window.__shellListeners()")
                if rnd == 2:
                    assert here["stats"] == seen[href]["stats"], (href, seen[href], here)
                    assert here["shell"] == seen[href]["shell"], (href, seen[href], here)
                elif rnd == 1:
                    seen[href] = here
        assert not errors, errors
    finally:
        ctx.close()


def test_back_while_a_page_is_loading_wins_and_the_highlight_follows(live):
    """A Back pressed while a click's page is still on its way cancels that page: the address,
    the content and the sidebar all stay with the entry Back chose."""
    ctx, page, errors = _open(live, "/")
    try:
        _go(page, "/profile", "profile")
        # Slow the next page down, click it, and go Back before it arrives.
        page.route("**/runs", lambda route: (page.wait_for_timeout(1500), route.continue_()))
        page.click('#sidebar .mainnav > a[href="/runs"]')
        page.go_back()
        page.wait_for_function("() => window.KUI.page.current() === 'today'"
                               " && !document.documentElement.classList.contains('k-going')")
        page.wait_for_timeout(2500)  # the slowed page would have landed by now
        here = page.evaluate(_HERE)
        assert here["page"] == "today" and here["at"] == "/" and here["current"] == "/", here
        assert here["doc"] == "first"
        assert not errors, errors
    finally:
        ctx.close()


def test_the_new_pages_heading_has_the_focus_after_a_change(live):
    ctx, page, errors = _open(live, "/")
    try:
        _go(page, "/about", "about")
        focused = page.evaluate("() => document.activeElement && document.activeElement.tagName")
        assert focused == "H1"
        assert not errors, errors
    finally:
        ctx.close()


def test_a_jump_within_the_page_keeps_its_place_in_history(live):
    """A #section link pressed twice makes one stamped entry (not one with no state, which would
    throw off a later refused Back), and Back returns to where the reader was."""
    ctx, page, errors = _open(live, "/about")
    try:
        page.evaluate("""() => {
          const main = document.querySelector('main.container');
          const a = document.createElement('a');
          a.id = 'k-test-link'; a.href = '#k-test-sec'; a.textContent = 'jump';
          const sec = document.createElement('div');
          sec.id = 'k-test-sec'; sec.style.marginTop = '3000px'; sec.textContent = 'here';
          main.prepend(a); main.append(sec);
          window.scrollTo(0, 0);
        }""")
        page.click("#k-test-link")
        page.wait_for_function("() => location.hash === '#k-test-sec' && window.scrollY > 500")
        page.evaluate("window.scrollTo(0, 0)")
        page.click("#k-test-link")
        page.wait_for_function("() => window.scrollY > 500")
        assert page.evaluate("() => !!(history.state && history.state.kNav)")
        page.go_back()
        page.wait_for_function("() => location.hash === '' && window.scrollY < 50")
        assert page.evaluate("() => window.__doc") == "first"
        assert not errors, errors
    finally:
        ctx.close()
