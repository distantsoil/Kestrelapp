"""The shell in a real browser (D-UI-88, D-UI-91, the locked mockups): the window controls appear
only with the desktop window's JS API and call it; the top row scrolls with the page; the
search shortcut follows the OS; reduced motion stops all motion; the launch entrance runs once.
The window's native drag, resize and traffic lights need real hardware (see the report).

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests import today_seed
from tests.browser import launch_sync

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

FAKE_API = """
window.__calls = [];
const rec = (name, ret) => (...a) => {
  window.__calls.push([name, ...a]); return Promise.resolve(ret);
};
window.pywebview = {api: {
  window_info: rec('info', {os: window.__os || 'win', maximized: false}),
  window_minimize: rec('min'), window_toggle_maximize: rec('max', true),
  window_close_intent: rec('intent', 'tray'), window_close: rec('close'),
  window_start_drag: rec('drag'), window_start_resize: rec('resize'),
  window_set_frame_color: rec('strip'),
}};
"""


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web import desktop_bridge
    from kestrel.web.serve import running_server

    asyncio.run(today_seed.seed())
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, desktop_bridge
        browser.close()


def _page(browser, base, path="/help", init="", **ctx_kw):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, **ctx_kw)
    page = ctx.new_page()
    if init:
        page.add_init_script(init)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base + path)
    page.wait_for_load_state("networkidle")
    return ctx, page, errors


def test_controls_stay_hidden_in_a_plain_browser_even_from_the_desktop_server(live):
    base, browser, bridge = live
    bridge.set_quit_handler(lambda: None)
    bridge.set_window_frame(True)
    ctx, page, errors = _page(browser, base)
    try:
        assert page.locator("#k-wctl").count() == 1 and page.locator("#k-wctl").is_hidden()
        assert all(h.is_hidden() for h in page.locator("[data-k-rsz]").all())
        assert not errors, errors
    finally:
        ctx.close()


def test_controls_call_the_window_api_and_close_asks_first(live):
    base, browser, bridge = live
    bridge.set_quit_handler(lambda: None)
    bridge.set_window_frame(True)
    ctx, page, errors = _page(browser, base, init=FAKE_API)
    try:
        page.wait_for_selector("#k-wctl:not([hidden])")
        wctl = page.locator("#k-wctl").bounding_box()
        brand = page.locator(".k-brand").bounding_box()
        assert wctl and brand and wctl["y"] < brand["y"]  # above the mark, top of the sidebar
        # Windows order: minimize, maximize, close.
        xs = [page.locator(f'[data-wc="{w}"]').bounding_box()["x"] for w in ("min", "max", "close")]
        assert xs == sorted(xs)
        # Windows resizes from the window's own border strip (frame.py), never from the page.
        assert all(h.is_hidden() for h in page.locator("[data-k-rsz]").all())
        page.click('[data-wc="min"]')
        page.click('[data-wc="max"]')
        page.wait_for_function("document.getElementById('k-wctl').classList.contains('is-max')")
        page.click('[data-wc="close"]')
        page.wait_for_function("window.__calls.some(c => c[0] === 'close')")
        # The empty top row drags and a double click toggles maximize; the search does not.
        gap = page.locator(".k-tools .k-tools__gap").first.bounding_box()
        page.mouse.move(gap["x"] + 5, gap["y"] + 5)
        page.mouse.down()
        page.mouse.move(gap["x"] + 40, gap["y"] + 5)
        page.mouse.up()
        page.locator(".k-search").click()
        page.keyboard.press("Escape")
        calls = [c[0] for c in page.evaluate("window.__calls")]
        assert calls[:1] == ["info"] and "min" in calls and "max" in calls
        assert calls.index("intent") < calls.index("close")  # close asks where it goes first
        assert calls.count("drag") == 1 and "resize" not in calls
        assert not errors, errors
    finally:
        ctx.close()


def test_mac_shows_no_drawn_buttons_and_the_shortcut_reads_cmd(live):
    base, browser, bridge = live
    bridge.set_quit_handler(lambda: None)
    bridge.set_window_frame(True)
    init = ("Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});"
            "window.__os = 'mac';" + FAKE_API)
    ctx, page, errors = _page(browser, base, init=init)
    try:
        page.wait_for_selector("#k-wctl.is-mac")
        assert all(b.is_hidden() for b in page.locator("#k-wctl button").all())
        assert all(h.is_hidden() for h in page.locator("[data-k-rsz]").all())  # native resize
        assert page.locator("[data-k-kbd]").inner_text() == "⌘K"
    finally:
        ctx.close()


def test_windows_shortcut_label_and_a_top_row_that_scrolls_away(live):
    base, browser, _ = live
    init = "Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});"
    ctx, page, errors = _page(browser, base, path="/settings", init=init)
    try:
        assert page.locator("[data-k-kbd]").inner_text() == "Ctrl K"
        assert page.evaluate("getComputedStyle(document.querySelector('.k-tools')).position") \
            == "relative"
        before = page.locator(".k-tools").bounding_box()["y"]
        page.evaluate("window.scrollTo(0, 300)")
        page.wait_for_timeout(100)
        after = page.locator(".k-tools").bounding_box()["y"]
        assert after < before - 250  # it scrolled with the page: no fixed band
        bg = page.evaluate("getComputedStyle(document.querySelector('.k-main')).backgroundImage")
        assert "linear-gradient" in bg  # the sky gradient behind the page
        assert not errors, errors
    finally:
        ctx.close()


def test_launch_entrance_runs_once_and_reduced_motion_stops_everything(live):
    base, browser, _ = live
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    try:
        # Read at first paint: the entrance classes come off once the motion is over.
        page.goto(base + "/", wait_until="domcontentloaded")
        assert page.evaluate("document.documentElement.classList.contains('k-intro')")
        assert page.locator(".side .mainnav > a").first.evaluate(
            "el => getComputedStyle(el).animationName") == "k-inL"
        page.wait_for_load_state("networkidle")
        page.goto(base + "/help")
        assert not page.evaluate("document.documentElement.classList.contains('k-intro')")
    finally:
        ctx.close()
    ctx, page, errors = _page(browser, base, path="/", reduced_motion="reduce")
    try:
        page.wait_for_timeout(300)
        running = page.evaluate(
            "document.getAnimations().filter(a => a.playState === 'running').length")
        assert running == 0
        dur = page.locator(".side .mainnav > a").first.evaluate(
            "el => getComputedStyle(el).transitionDuration")
        assert dur in ("0s", "0s, 0s")
    finally:
        ctx.close()


def test_a_double_click_on_the_drag_region_maximizes_and_a_move_drags_on_windows(live):
    """Hardware check, round 6: a move started on the press swallowed the mouse-up, so no double
    click ever reached the page. The move now starts only after the pointer moves, so two
    clicks in place are the browser's own double click."""
    base, browser, bridge = live
    bridge.set_quit_handler(lambda: None)
    bridge.set_window_frame(True)
    ctx, page, errors = _page(browser, base, init=FAKE_API)
    try:
        page.wait_for_selector("#k-wctl:not([hidden])")
        gap = page.locator(".k-tools .k-tools__gap").first.bounding_box()
        x, y = gap["x"] + 5, gap["y"] + 5
        page.mouse.dblclick(x, y)
        names = [c[0] for c in page.evaluate("window.__calls")]
        assert names.count("drag") == 0 and names.count("max") == 1
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + 2, y)  # a tremor is still a click
        assert [c[0] for c in page.evaluate("window.__calls")].count("drag") == 0
        page.mouse.move(x + 20, y + 4)
        page.mouse.up()
        assert [c[0] for c in page.evaluate("window.__calls")].count("drag") == 1
        assert not errors, errors
    finally:
        ctx.close()


def test_the_palette_opens_with_ctrl_k_in_any_case_finds_a_page_and_opens_it(live):
    base, browser, _ = live
    ctx, page, errors = _page(browser, base, path="/settings")
    try:
        page.evaluate("""document.dispatchEvent(new KeyboardEvent('keydown',
          {key: 'K', ctrlKey: true, shiftKey: true, bubbles: true}))""")
        page.wait_for_selector('[data-testid="palette"] input:focus')
        page.keyboard.type("about")
        option = page.locator('#palette-list [role="option"]', has_text="Open About")
        option.wait_for()
        assert page.locator(".k-pal__count").inner_text().endswith("result") or \
            page.locator(".k-pal__count").inner_text().endswith("results")
        option.hover()
        page.keyboard.press("Enter")
        page.wait_for_url("**/about")
        page.evaluate("""document.dispatchEvent(new KeyboardEvent('keydown',
          {key: 'k', metaKey: true, bubbles: true}))""")
        page.wait_for_selector('[data-testid="palette"] input:focus')
        page.keyboard.type("zzqqxx")
        # The search runs as you type; the empty state shows once its answer arrives.
        page.wait_for_selector('[data-testid="palette-empty"]', state="visible")
        page.keyboard.press("Escape")
        page.wait_for_selector('[data-testid="palette"]', state="hidden")
        assert not errors, errors
    finally:
        ctx.close()


def test_loading_page_states_and_one_try_again_at_a_time(live):
    from kestrel.desktop.shell import splash_html

    _, browser, _ = live
    ctx = browser.new_context(viewport={"width": 1200, "height": 760})
    page = ctx.new_page()
    try:
        page.set_content(splash_html())
        page.evaluate(FAKE_API.replace("window_info:", "splash_retry: rec('retry'), window_info:"))
        page.evaluate("KSplash.say('Updating your data to the new version', null, 'Later', 0)")
        page.wait_for_function("document.getElementById('status').textContent.startsWith('Updating')")
        assert page.locator("#bar").evaluate("b => b.classList.contains('is-indet')")
        assert page.locator("#note").inner_text() == "Later"
        page.evaluate("KSplash.fail('Something stopped it.', 'Startup step: x')")
        assert page.locator("#prob").is_visible() and page.locator("#sp").is_hidden()
        assert page.locator("#reason").inner_text() == "Something stopped it."
        assert page.evaluate("document.activeElement.id") == "retry"
        assert page.locator("#det").is_hidden()
        page.click("#detBtn")
        assert page.locator("#det").inner_text() == "Startup step: x"
        page.click("#retry")
        assert page.locator("#retry").is_disabled()
        page.locator("#retry").dispatch_event("click")  # a second click while it runs
        assert [c[0] for c in page.evaluate("window.__calls")].count("retry") == 1
        page.evaluate("KSplash.fail('Again.', '')")
        assert page.locator("#retry").is_enabled()
    finally:
        ctx.close()


LATE_API = FAKE_API.replace("window.pywebview = {api: {", "window.__late = {api: {") + """
window.pywebview = {};  // the object exists early, its API arrives later
setTimeout(() => { window.pywebview.api = window.__late.api;
  window.dispatchEvent(new Event('pywebviewready')); }, 700);
"""
EARLY_EVENT = FAKE_API + """
// pywebviewready fires before the page's own script has run (the Windows order).
window.dispatchEvent(new Event('pywebviewready'));
"""


@pytest.mark.parametrize("init", [LATE_API, EARLY_EVENT], ids=["api-late", "event-early"])
def test_controls_appear_whichever_comes_first_the_api_or_the_page(live, init):
    """Hardware check: on Windows pywebviewready fired before the page listened, so the controls
    stayed hidden. They must appear when the API is already there, or when it arrives later."""
    base, browser, bridge = live
    bridge.set_quit_handler(lambda: None)
    bridge.set_window_frame(True)
    ctx, page, errors = _page(browser, base, init=init)
    try:
        page.wait_for_selector("#k-wctl:not([hidden])", timeout=5000)
        names = [c[0] for c in page.evaluate("window.__calls")]
        assert names.count("info") == 1  # wired once, not once per path
        assert not errors, errors
    finally:
        ctx.close()


def test_top_row_tooltips_stay_inside_the_window(live):
    base, browser, _ = live
    ctx, page, errors = _page(browser, base, path="/help")
    try:
        for sel in ('[data-testid="theme-toggle"]', '#k-topavatar', '[data-testid="bell"]'):
            el = page.locator(sel)
            assert el.get_attribute("title") is None and el.get_attribute("data-k-tip")
            el.hover()
            page.wait_for_timeout(600)
            right = el.evaluate("""e => { const r = e.getBoundingClientRect();
              const w = parseFloat(getComputedStyle(e, '::after').width);
              return r.right - 0 >= w ? r.right : r.left + w; }""")
            assert right <= page.viewport_size["width"], (sel, right)
    finally:
        ctx.close()


def test_scroll_cue_and_an_edge_to_edge_scene_without_sideways_scroll(live):
    """The page column's scroll cue (pipeline mockup): an arrow while there is more below, gone
    at the end, the up arrow then. The scene art reaches the window edge and the page still
    has no sideways overflow (the art is laid out against the page column, not the page)."""
    base, browser, _ = live
    ctx = browser.new_context(viewport={"width": 1280, "height": 700})
    page = ctx.new_page()
    try:
        page.goto(base + "/opportunities")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2500)
        art = page.locator(".k-scene__art:not(.k-scene__touch)").bounding_box()
        assert art and abs(art["x"] + art["width"] - 1280) <= 1, art
        assert page.evaluate("""(() => { const m = document.querySelector('.pl-wrap');
          return m.scrollWidth <= m.clientWidth + 1; })()""")
        page.wait_for_selector(".k-cue--fixed.k-cue--down.is-show", timeout=3000)
        assert page.locator(".k-cue--fixed.k-cue--up.is-show").count() == 0
        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        page.wait_for_selector(".k-cue--fixed.k-cue--up.is-show", timeout=3000)
        assert page.locator(".k-cue--fixed.k-cue--down.is-show").count() == 0
    finally:
        ctx.close()


def test_the_desktop_window_never_keeps_a_service_worker_and_links_navigate(live):
    """Windows hardware check, round 3: a service worker left in the window's profile controlled
    the page, which then had no WebView2 bridge (no controls) and links became downloads. In the
    desktop window the page removes any registration, reloads once uncontrolled, and never
    registers again. A plain browser tab keeps it."""
    base, browser, bridge = live
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    downloads: list = []
    page.on("download", lambda d: downloads.append(d))
    try:
        page.goto(base + "/help")  # a browser tab: the offline cache registers
        page.wait_for_function("navigator.serviceWorker.getRegistrations().then(r => r.length)"
                               " .then(n => n > 0)", timeout=10000)
        # Reload only once the worker is active: a reload while it is still installing is not
        # controlled, and the next wait then times out (failed intermittently on the base commit).
        page.evaluate("navigator.serviceWorker.ready.then(() => true)")
        page.reload()
        page.wait_for_function("!!navigator.serviceWorker.controller", timeout=10000)
        bridge.set_quit_handler(lambda: None)
        bridge.set_window_frame(True)  # now the same server serves the desktop window
        page.goto(base + "/help")
        # The page reloads itself once to shed the worker (base.html), so nothing is evaluated
        # until the uncontrolled document is up: the old document is still controlled, the
        # reloaded one is not.
        page.wait_for_function("!navigator.serviceWorker.controller", timeout=10000)
        page.wait_for_load_state("load")
        assert page.evaluate("document.documentElement.dataset.kDesktop") == "1"
        assert page.evaluate("navigator.serviceWorker.getRegistrations().then(r => r.length)") == 0
        page.click('nav.mainnav a[href="/opportunities"]')
        page.wait_for_url("**/opportunities")
        page.wait_for_load_state("networkidle")
        assert page.evaluate("navigator.serviceWorker.getRegistrations().then(r => r.length)") == 0
        assert downloads == []
    finally:
        ctx.close()


def test_the_page_never_shadows_the_webview2_bridge(live):
    """Hardware check, round 4: a global `function chrome()` replaced window.chrome in WebView2,
    so window.chrome.webview (pywebview's bridge) was gone and no window control worked."""
    base, browser, _ = live
    init = "window.chrome = Object.assign(window.chrome || {}, {webview: {postMessage() {}}});"
    ctx, page, errors = _page(browser, base, path="/settings", init=init)
    try:
        assert page.evaluate("typeof window.chrome") == "object"
        assert page.evaluate("!!(window.chrome && window.chrome.webview)")
        for name in ("external", "webkit", "pywebview", "open", "close", "print", "stop"):
            assert page.evaluate(f"typeof window.{name} !== 'function' || "
                                 f"String(window.{name}).includes('[native code]')"), name
        assert not errors, errors
    finally:
        ctx.close()


def test_linux_page_edges_reach_every_side_and_windows_has_none(live):
    """Linux resizes from the page's edge band (GTK begin_resize_drag); the handles are pinned
    to the window, not the page, and a press in the band resizes even over the scrollbar.
    Windows has no page handles at all: its window keeps a native border strip (round 6)."""
    base, browser, bridge = live
    bridge.set_quit_handler(lambda: None)
    bridge.set_window_frame(True)
    ctx, page, errors = _page(browser, base, init="window.__os = 'linux';" + FAKE_API)
    try:
        page.wait_for_selector("#k-wctl:not([hidden])")
        vw, vh = page.evaluate("[innerWidth, innerHeight]")
        page.evaluate("document.body.style.minHeight = '3000px'")  # a long page
        for edge in ("s", "se", "e"):
            box = page.locator(f'[data-k-rsz="{edge}"]').bounding_box()
            assert box["x"] + box["width"] <= vw and box["y"] + box["height"] <= vh, edge
        got = page.evaluate("""([w, h]) => [[w-1, h/2], [w/2, h-1], [w-1, h-1], [0, h/2],
            [w/2, 0], [0, 0], [w-1, 0], [0, h-1], [w/2, h/2]].map(p =>
            KUI.frame.edgeAt(p[0], p[1], w, h))""", [vw, vh])
        assert got == ["e", "s", "se", "w", "n", "nw", "ne", "sw", None]
        assert not errors, errors
    finally:
        ctx.close()
    ctx, page, errors = _page(browser, base, init=FAKE_API)
    try:
        page.wait_for_selector("#k-wctl:not([hidden])")
        assert all(h.is_hidden() for h in page.locator("[data-k-rsz]").all())
        assert page.evaluate("KUI.frame.edgeAt(0, 0, innerWidth, innerHeight)") is None
    finally:
        ctx.close()


def test_windows_strip_takes_the_sidebar_color_and_rounds_the_page_corners(live):
    """D-UI-196: the page sends --frame-strip on wiring and on a theme change; the inner corners
    are drawn in it while not maximized."""
    base, browser, bridge = live
    bridge.set_quit_handler(lambda: None)
    bridge.set_window_frame(True)
    ctx, page, errors = _page(browser, base, init=FAKE_API)
    try:
        page.wait_for_selector("#k-wctl:not([hidden])")
        page.wait_for_function("window.__calls.some(c => c[0] === 'strip')")
        first = [c for c in page.evaluate("window.__calls") if c[0] == "strip"][0][1]
        assert first.startswith("rgb")
        assert page.locator(".k-corners").is_visible()
        page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
        page.wait_for_function("window.__calls.filter(c => c[0] === 'strip').length === 2")
        second = [c for c in page.evaluate("window.__calls") if c[0] == "strip"][1][1]
        assert second != first
        page.evaluate("KUI.frame.state(true)")
        assert page.locator(".k-corners").is_hidden()
        assert not errors, errors
    finally:
        ctx.close()


def test_power_button_opens_the_same_quit_dialog_as_the_palette(live):
    """D-UI-197: the top row's power button and the palette's Quit entry open one dialog; the
    tooltip stays inside the window."""
    base, browser, bridge = live
    bridge.set_quit_handler(lambda: None)
    bridge.set_window_frame(True)
    ctx, page, errors = _page(browser, base, init=FAKE_API)
    try:
        btn = page.locator('[data-testid="quit-app"]')
        assert btn.is_visible()
        box = btn.bounding_box()
        avatar = page.locator(".k-tools .k-avatar, .k-tools [data-testid='avatar']").first
        if avatar.count():
            assert box["x"] >= avatar.bounding_box()["x"]
        btn.hover()
        tip = page.evaluate(
            "(() => { const b = document.querySelector('[data-testid=quit-app]');"
            " const s = getComputedStyle(b, '::after'); return [s.content]; })()"
        )
        assert "Quit Kestrel" in tip[0]
        assert box["x"] + box["width"] <= page.evaluate("innerWidth")
        btn.click()
        assert page.locator("#quit-confirm").evaluate("d => d.open")
        page.keyboard.press("Escape")
        assert not page.locator("#quit-confirm").evaluate("d => d.open")
        page.keyboard.press("Control+k")
        page.keyboard.type("quit kestrel")
        page.keyboard.press("Enter")
        page.wait_for_function("document.getElementById('quit-confirm').open")
        assert not errors, errors
    finally:
        ctx.close()


def test_a_seasonal_touch_never_duplicates_a_transition_name(live):
    """A band with a seasonal touch draws two art SVGs. A shared view-transition-name is a
    duplicate, and Chromium then drops the page transition (a grey window on every page change
    while the touch is in season, 2026-10-29). Not tied to today's date: a touch is forced on."""
    base, browser, bridge = live
    ctx, page, errors = _page(browser, base, path="/")
    try:
        names = page.evaluate("""() => {
            const band = document.querySelector('.k-scene');
            const touch = window.KSceneBand.seasons()[0].id;
            band.setAttribute('data-touch', touch);
            band.removeAttribute('data-rendered');
            window.KSceneBand.render(band);
            return [...document.querySelectorAll('*')]
              .filter(n => n.getClientRects().length)
              .map(n => getComputedStyle(n).viewTransitionName)
              .filter(v => v && v !== 'none'); }""")
        assert "k-art" in names and "k-touch" in names, names
        assert len(names) == len(set(names)), names
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("size", [(1280, 640), (1280, 700), (1440, 760)])
def test_a_short_window_never_puts_the_menu_under_the_status_readout(live, size):
    """Owner, 2026-10-29: at 1280x640 (the minimum height) Settings sat under the two-line status
    readout. The menu scrolls in its own space above the readout; its last item is reachable."""
    base, browser, bridge = live
    ctx, page, errors = _page(browser, base, path="/")
    try:
        page.set_viewport_size({"width": size[0], "height": size[1]})
        page.wait_for_timeout(400)
        out = page.evaluate("""() => {
            const stat = document.querySelector('.side .k-sstat').getBoundingClientRect();
            const nav = document.querySelector('.side .mainnav');
            const n = nav.getBoundingClientRect();
            const hits = [...nav.querySelectorAll('a')].filter(a => {
              const r = a.getBoundingClientRect();
              const shown = Math.min(r.bottom, n.bottom) - Math.max(r.top, n.top);
              return r.height && shown > 1 && Math.min(r.bottom, n.bottom) > stat.top + 1; })
              .map(a => a.textContent.trim().slice(0, 20));
            nav.scrollTop = nav.scrollHeight;
            const last = [...nav.querySelectorAll('a')].pop().getBoundingClientRect();
            return {hits, lastVisible: last.bottom <= n.bottom + 1 && last.bottom <= stat.top + 1,
                    statInView: stat.bottom <= innerHeight + 1}; }""")
        assert out["hits"] == [], out
        assert out["lastVisible"] and out["statInView"], out
        assert not errors, errors
    finally:
        ctx.close()


def test_text_renders_in_the_shipped_manrope(live):
    """D-UI-127: the interface face is Manrope from the app's own files, not a fallback."""
    base, browser, bridge = live
    ctx, page, errors = _page(browser, base, path="/")
    try:
        page.evaluate("document.fonts.ready")
        loaded = page.evaluate("""async () => { await document.fonts.ready;
            return [...document.fonts].filter(f => f.family.replace(/"/g, '') === 'Manrope'
                                                   && f.status === 'loaded').length; }""")
        assert loaded >= 1
        assert page.evaluate("document.fonts.check('16px Manrope')")
        assert not errors, errors
    finally:
        ctx.close()


def test_track_switch_pill_keeps_air_around_label_and_badge(live, monkeypatch):
    """D-UI-218: with two tracks on, the active pill's label does not touch the pill's left edge
    and the count badge sits clear of both the label and the right edge."""
    from kestrel.services import tracks

    # The server runs in this process and reads the cached choice (set at startup).
    monkeypatch.setitem(tracks._cache, "value", ("employment", "consulting"))
    base, browser, _bridge = live
    ctx, page, errors = _page(browser, base)
    try:
        page.wait_for_selector(".side .track-switch a.active")
        got = page.evaluate("""() => {
          const a = document.querySelector('.side .track-switch a.active');
          const n = a.querySelector('.n'); n.textContent = '12';
          const r = document.createRange(); r.selectNodeContents(a.firstChild);
          const t = r.getBoundingClientRect(), p = a.getBoundingClientRect(),
                b = n.getBoundingClientRect();
          return {left: t.left - p.left, gap: b.left - t.right, right: p.right - b.right,
                  fits: a.scrollWidth <= a.clientWidth + 1};
        }""")
        assert got["left"] >= 6 and got["gap"] >= 4 and got["right"] >= 6 and got["fits"], got
        assert not errors, errors
    finally:
        ctx.close()


# ---------------------------------------------------------------- the bell badge (#95, D-UI-236)
def _set_unread(n: int) -> None:
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel import paths
    from kestrel.db.engine import make_async_engine
    from kestrel.db.models import Notification

    async def go() -> None:
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            await s.execute(delete(Notification))
            s.add_all(Notification(kind="run_finished", severity="info", title=f"Run {i}")
                      for i in range(n))
            await s.commit()
        await engine.dispose()

    # Playwright's sync API keeps a loop running on this thread, so asyncio.run needs its own.
    with ThreadPoolExecutor(1) as pool:
        pool.submit(asyncio.run, go()).result()


_BADGE_JS = """() => {
  const q = s => document.querySelector(s);
  const b = q('#bellcount'), btn = q('[data-testid="bell"]');
  const br = b.getBoundingClientRect(), cr = btn.getBoundingClientRect();
  const cs = getComputedStyle(b);
  // Share of the badge's own area (a capsule) that lies inside the bell circle.
  const r = br.height / 2, cx = cr.left + cr.width / 2, cy = cr.top + cr.height / 2;
  let inBadge = 0, both = 0;
  for (let x = br.left; x < br.right; x += 0.5) for (let y = br.top; y < br.bottom; y += 0.5) {
    const nx = Math.min(Math.max(x, br.left + r), br.right - r);
    if (Math.hypot(x - nx, y - (br.top + r)) > r) continue;
    inBadge++;
    if (Math.hypot(x - cx, y - cy) <= cr.width / 2) both++;
  }
  const others = Array.from(document.querySelectorAll('.k-tools > *, .k-tools button, .k-avatar'))
    .filter(e => e !== btn && !btn.contains(e) && !e.contains(btn) && e.offsetParent !== null)
    .map(e => e.getBoundingClientRect()).filter(o => o.width > 0 && o.height > 0);
  const hit = (x, y) => {
    const e = document.elementFromPoint(x, y); return e === b || b.contains(e); };
  return {
    text: b.textContent, aria: btn.getAttribute('aria-label'),
    hidden: b.getAttribute('aria-hidden'),
    color: cs.color, bg: cs.backgroundColor, shadow: cs.boxShadow, fontSize: cs.fontSize,
    box: [br.left, br.top, br.right, br.bottom], w: br.width, h: br.height,
    overlap: both / inBadge, vw: innerWidth, vh: innerHeight,
    paper: getComputedStyle(btn).backgroundColor,
    clash: others.some(o => br.left < o.right && o.left < br.right
                          && br.top < o.bottom && o.top < br.bottom),
    visibleCorners: [hit(br.left + r, br.top + 3), hit(br.right - r, br.bottom - 3),
                     hit(br.left + 3, br.top + r), hit(br.right - 3, br.top + r)],
    textFits: b.scrollWidth <= b.clientWidth + 1,
    glyph: getComputedStyle(btn.querySelector('svg')).color,
  };
}"""


def _rgb(css: str) -> tuple[float, float, float]:
    import re

    nums = [float(x) for x in re.findall(r"[\d.]+", css)[:3]]
    return nums[0], nums[1], nums[2]


def _ratio(fg: str, bg: str) -> float:
    def lum(c: tuple[float, float, float]) -> float:
        lin = [(v / 255) / 12.92 if v / 255 <= 0.03928 else (((v / 255) + 0.055) / 1.055) ** 2.4
               for v in c]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    hi, lo = sorted((lum(_rgb(fg)), lum(_rgb(bg))), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("theme,width", [("light", 1440), ("dark", 1440), ("light", 760),
                                         ("dark", 760)])
@pytest.mark.parametrize("unread,label", [(3, "3"), (12, "9+")])
def test_bell_badge_contrast_and_geometry(live, theme, width, unread, label):
    base, browser, _bridge = live
    _set_unread(unread)
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    try:
        page.goto(base + "/help")
        page.wait_for_function(
            "document.getElementById('bellcount').textContent.trim() !== ''")
        page.wait_for_function("document.getElementById('bellcount').getAnimations().length === 0")
        d = page.evaluate(_BADGE_JS)
        assert d["text"] == label and d["hidden"] == "true"
        assert d["aria"] == f"Notifications, {unread} unread"  # the true count, not "9+"
        assert _ratio(d["color"], d["bg"]) >= 4.5, d  # the number on the badge
        assert _ratio(d["bg"], d["paper"]) >= 3, d  # the badge against its ring and circle
        assert _ratio(d["glyph"], d["paper"]) >= 3, d  # the bell glyph on its circle
        assert d["paper"] in d["shadow"] and "2px" in d["shadow"], d  # the 2 px ring
        assert d["fontSize"] == "11px"
        assert d["w"] >= 18 and d["h"] >= 18 and abs(d["h"] - 18) < 0.5, d
        assert 0.30 <= d["overlap"] <= 0.50, d  # on the circle's edge, not floating off it
        left, top, right, bottom = d["box"]
        assert left >= 0 and top >= 0 and right <= d["vw"] and bottom <= d["vh"], d
        assert not d["clash"], d  # no neighbouring control under the badge or its ring
        assert all(d["visibleCorners"]) and d["textFits"], d  # not clipped by an ancestor
    finally:
        ctx.close()


_POLL = "htmx.ajax('GET', '/notifications/bell', {target: '#bellcount', swap: 'innerHTML'})"
_STYLE = "getComputedStyle(document.getElementById('bellcount')).{}"
_ANIMS = """() => { const b = document.getElementById('bellcount');
  return b.classList.contains('CLS') && b.getAnimations().map(a => a.animationName) }"""
_NO_ANIM = """() => { const b = document.getElementById('bellcount');
  return !b.classList.contains('is-in') && !b.classList.contains('is-tick')
    && b.getAnimations().length === 0 }"""
_FRAMES = """
window.__frames = [];
document.addEventListener('DOMContentLoaded', () => {
  const sample = () => {
    const b = document.getElementById('bellcount');
    const btn = document.querySelector('[data-testid=bell]');
    const cs = getComputedStyle(b);
    window.__frames.push({text: b.textContent, display: cs.display, opacity: cs.opacity,
      anims: b.getAnimations().length, aria: btn.getAttribute('aria-label')});
    if (window.__frames.length < 40) requestAnimationFrame(sample);
  };
  requestAnimationFrame(sample);
});
"""


def test_bell_badge_is_hidden_at_zero_and_ticks_when_the_count_changes(live):
    base, browser, _bridge = live
    _set_unread(0)
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    try:
        page.goto(base + "/help")
        page.wait_for_load_state("networkidle")
        assert page.evaluate(_STYLE.format("display")) == "none"
        assert page.get_attribute('[data-testid="bell"]', "aria-label") == "Notifications"
        # Hidden to shown replays the entry animation, not the tick.
        _set_unread(2)
        page.evaluate(_POLL)
        names = page.wait_for_function(_ANIMS.replace("CLS", "is-in")).json_value()
        assert names == ["k-badge-in"], names
        page.wait_for_function(
            "document.querySelector('[data-testid=\"bell\"]').getAttribute('aria-label')"
            " === 'Notifications, 2 unread'")
        # Both classes clear on animationend, so the next change is a real, observable tick.
        page.wait_for_function(_NO_ANIM)
        _set_unread(3)
        page.evaluate(_POLL)
        names = page.wait_for_function(_ANIMS.replace("CLS", "is-tick")).json_value()
        assert names == ["k-tick"], names
        page.wait_for_function(_NO_ANIM)
        # Gone again at zero, and back with the entry animation.
        _set_unread(0)
        page.evaluate(_POLL)
        page.wait_for_function("document.getElementById('bellcount').textContent === ''")
        assert page.evaluate(_STYLE.format("display")) == "none"
        _set_unread(1)
        page.evaluate(_POLL)
        names = page.wait_for_function(_ANIMS.replace("CLS", "is-in")).json_value()
        assert names == ["k-badge-in"], names
    finally:
        ctx.close()


def test_bell_badge_is_final_in_the_first_painted_frame(live):
    base, browser, _bridge = live
    _set_unread(3)
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.add_init_script(_FRAMES)
    try:
        page.goto(base + "/help")
        page.wait_for_function("(window.__frames || []).length >= 40")
        frames = page.evaluate("window.__frames")
        first = frames[0]
        assert first["text"] == "3" and first["display"] != "none", first
        assert first["opacity"] == "1", first
        assert first["aria"] == "Notifications, 3 unread", first
        # No frame shows an entry or tick animation: nothing changed after load.
        assert all(f["text"] == "3" and f["anims"] == 0 and f["opacity"] == "1" for f in frames), \
            frames
    finally:
        ctx.close()


def test_bell_badge_motion_is_off_under_reduced_motion(live):
    base, browser, _bridge = live
    _set_unread(3)
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
    page = ctx.new_page()
    try:
        page.goto(base + "/help")
        page.wait_for_function("document.getElementById('bellcount').textContent === '3'")
        assert page.evaluate(_STYLE.format("animationName")) == "none"
    finally:
        ctx.close()
