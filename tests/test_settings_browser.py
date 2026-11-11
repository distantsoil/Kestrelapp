"""Settings in a real browser (UI overhaul parity check against the locked mockup,
the locked design mockup (kept privately)). Light and dark at 1280 and 1680, every
section: no overlap, no wrapped titles or buttons, no sideways scroll; then the interactions the
mockup demos (section routing and old links, a control saving with Undo, the rule list, the scene
manager and the confirm before access opens).

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1. The browser is the Edge
already on the machine, else a Playwright Chromium (tests/browser.py)."""

from __future__ import annotations

import contextlib
import os
import pathlib
import tempfile

import pytest

from tests.browser import TIMER_PROBE_JS, assert_page_remounts_clean, launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

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


def test_classic_page_loads_turns_the_in_place_page_change_off_at_once(live):
    """D-UI-220: the switch saves, says so, and the very next page change is a new document."""
    ctx, page, errors = _page(live, "light", 1680, "#preferences")
    try:
        page.wait_for_selector('#st-sec-preferences:not([hidden])')
        assert page.get_attribute("html", "data-k-nav") == "1"
        page.click('[data-st-key="ui.classic_nav"] + .k-switch__track')
        page.wait_for_selector('.k-toast:has-text("Pages now load the classic way.")')
        assert page.get_attribute("html", "data-k-nav") == "0"
        page.evaluate("window.__doc = 'settings'")
        page.click('#sidebar .mainnav > a[href="/about"]')
        page.wait_for_function("() => !window.__doc && location.pathname === '/about'")
        assert page.get_attribute("html", "data-k-nav") == "0"
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


def test_the_page_mounts_unmounts_and_mounts_again_with_one_set_of_everything(live):
    """The page lifecycle (D-UI-210): Settings unmounted and mounted again in place starts exactly
    what it started on the document load (its listeners, and the quiet prefetch of the two heavy
    sections now started from mount) and leaves nothing running in between. After it, one press of
    a switch saves once, not twice; and a message with an Undo that another page raises is no
    longer taken over by Settings."""
    base, browser = live
    ctx = browser.new_context(viewport={"width": 1280, "height": 1000}, color_scheme="light")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(TIMER_PROBE_JS)
    try:
        page.goto(f"{base}/settings#preferences")
        page.wait_for_selector('#st-sec-preferences:not([hidden]) .st-panebody')
        wait_for_layout(page)
        # The prefetch asked for both heavy sections once the page was up.
        page.wait_for_function(
            "document.querySelector('#st-sec-roles').getAttribute('data-st-loaded') === '1' && "
            "document.querySelector('#st-sec-scoring').getAttribute('data-st-loaded') === '1'")
        first = assert_page_remounts_clean(page, "settings", "settings.js", settle_ms=1600)
        assert first["timers"] == 0 and first["observers"] == 0, first
        # The palette's Settings entries are added once for the root, however often it mounts.
        assert page.evaluate("Alpine.$data(document.documentElement).pages.filter("
                             "p => (p.label || '').startsWith('Settings: ')).length") == 29
        # Mounted again, the first section shown is the one the address names, and one press of a
        # switch posts once.
        page.wait_for_selector('#st-sec-preferences:not([hidden])')
        posts: list[str] = []
        page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
        page.click('[data-st-key="ui.reduce_motion"] + .k-switch__track')
        page.wait_for_selector('.k-toast:has-text("Motion reduced.")')
        page.wait_for_timeout(300)
        assert [u for u in posts if u.endswith("/settings/ui")] == [f"{base}/settings/ui"]
        # Left, the page lets go of other pages' messages: an Undo toast keeps its own Undo.
        page.evaluate("KUI.page.unmount()")
        page.evaluate("""() => { window.__undone = 0;
          document.addEventListener('kestrel:undone', () => { window.__undone += 1; });
          document.dispatchEvent(new CustomEvent('k-toast',
            { detail: { message: 'Moved.', undoUrl: '/x-undo' } })); }""")
        page.route("**/x-undo", lambda route: route.fulfill(status=204))
        page.click('.k-toast:has-text("Moved.") .k-toast__undo')
        page.wait_for_function("window.__undone === 1")
        assert not errors, errors
    finally:
        ctx.close()


def test_switching_to_online_updates_the_setup_checklist_without_leaving_settings(live):
    """D-UI-241: the checklist follows the Models choice on the same page, no reload."""
    ctx, page, errors = _page(live, "light", 1280, "#computer")
    try:
        page.wait_for_selector('#st-sec-computer:not([hidden]) .st-modes')
        page.wait_for_selector('#st-chk [data-testid="st-chk-tally"]')
        assert page.locator("#st-chk.is-offline").count() == 0
        page.locator('[data-testid="st-mode-online"]').click()
        page.wait_for_selector("#st-chk.is-offline", timeout=5000)
        said = page.locator("#st-chk").inner_text()
        assert "Not needed while you use only an online model" in said
        page.locator('[data-testid="st-mode-local"]').click()
        page.wait_for_function("!document.querySelector('#st-chk.is-offline')", timeout=5000)
        assert not errors
    finally:
        ctx.close()


# ------------------------------------------------ the MCP card: addresses, key note, narrow title
_TEXT_BOXES = """root => {
  const out = [];
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  // A closed k-modal is visibility:hidden, which offsetParent does not report;
  // invisible text must not be judged as overlapping the live card behind it.
  const invisible = el => { let a = el; while (a && a !== document.documentElement) {
    if (['hidden', 'collapse'].includes(getComputedStyle(a).visibility)) return true;
    a = a.parentElement; } return false; };
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    if (!n.textContent.trim()) continue;
    const el = n.parentElement;
    const det = el && el.closest('details');
    const hiddenDet = det && !det.open && !el.closest('summary');
    if (!el || el.offsetParent === null || invisible(el) || hiddenDet) continue;
    const r = document.createRange(); r.selectNodeContents(n);
    // Clipped text (an ellipsis) is judged by its own box, not by the full text behind the clip.
    const clipped = getComputedStyle(el).textOverflow === 'ellipsis';
    const rects = clipped ? [el.getBoundingClientRect()] : r.getClientRects();
    for (const b of rects) if (b.width > 0 && b.height > 0)
      out.push({t: n.textContent.trim().slice(0, 24), l: b.left, r: b.right, tp: b.top,
                bt: b.bottom});
  }
  return out;
}"""


_SHOTS = pathlib.Path(os.environ.get("KESTREL_SHOTS_DIR") or tempfile.mkdtemp())


@contextlib.contextmanager
def _mcp_card_page(monkeypatch, lan_on, theme, width):
    """The MCP card with the switch on, LAN access on or off, in a real browser."""
    import httpx
    from playwright.sync_api import sync_playwright

    from kestrel import config as config_mod
    from kestrel.db import migrate
    from kestrel.web import auth
    from kestrel.web.serve import running_server

    if lan_on:
        real = config_mod.get_settings()
        lan = real.model_copy(update={"server": real.server.model_copy(update={"bind_all": True})})
        monkeypatch.setattr(config_mod, "get_settings", lambda: lan)
        monkeypatch.setattr(auth, "lan_host_ip", lambda: "192.168.1.20")
    migrate.run_migrations()
    with running_server(worker=False) as base, sync_playwright() as p:
        httpx.post(f"{base}/settings/mcp", data={"enabled": "1"})
        browser = launch_sync(p)
        ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
        page = ctx.new_page()
        try:
            page.goto(f"{base}/settings#privacy")
            page.wait_for_selector('[data-testid="st-mcp"] [data-testid="mcp-address"]')
            wait_for_layout(page)
            card = page.query_selector('[data-testid="st-mcp"]')
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
            yield page, card
        finally:
            ctx.close()
            browser.close()


def _assert_no_text_overlap(page, card, label):
    boxes = page.evaluate(_TEXT_BOXES, card)
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert not _overlaps(a, b), (label, a["t"], b["t"])


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1366, 390])
def test_the_mcp_card_with_lan_access_on_shows_both_addresses_and_a_one_line_key_name(
        monkeypatch, theme, width):
    with _mcp_card_page(monkeypatch, True, theme, width) as (page, card):
        _assert_no_text_overlap(page, card, ("lan_on", theme, width))
        token = page.query_selector('[data-testid="mcp-secret-note"] .st-code')
        assert token.inner_text() == "X-Kestrel-Key"
        assert page.evaluate(_LINES, token) <= 1
        assert token.bounding_box()["width"] > 80  # not the 26px monogram badge
        assert page.query_selector('[data-testid="mcp-address-lan"]').is_visible()
        assert page.query_selector('[data-testid="mcp-copy-lan"]').is_visible()
        card.screenshot(path=str(_SHOTS / f"mcp-lan_on-{theme}-{width}.png"))


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1366, 390])
def test_the_mcp_card_with_lan_access_off_points_to_lan_access_without_overlap(
        monkeypatch, theme, width):
    with _mcp_card_page(monkeypatch, False, theme, width) as (page, card):
        _assert_no_text_overlap(page, card, ("lan_off", theme, width))
        assert page.query_selector('[data-testid="mcp-lan-link"]').is_visible()
        assert page.query_selector('[data-testid="mcp-address-lan"]') is None
        card.screenshot(path=str(_SHOTS / f"mcp-lan_off-{theme}-{width}.png"))


# ---------------------------------------------------------- the pairing QR code scales whole (#133)
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1366, 390])
def test_the_pairing_qr_scales_to_its_box_with_all_three_finders_and_a_white_margin(
        monkeypatch, theme, width):
    import io

    import httpx
    from PIL import Image
    from playwright.sync_api import sync_playwright

    from kestrel.db import migrate
    from kestrel.web import auth
    from kestrel.web.serve import running_server

    monkeypatch.setattr(auth, "lan_host_ip", lambda: "192.168.1.20")
    migrate.run_migrations()
    with running_server(worker=False) as base, sync_playwright() as p:
        httpx.post(f"{base}/settings/privacy/access", data={"on": "1", "ack": "on"})
        browser = launch_sync(p)
        ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
        page = ctx.new_page()
        try:
            page.goto(f"{base}/settings#privacy")
            page.wait_for_selector('[data-testid="lan-qr"] svg')
            wait_for_layout(page)
            box = page.query_selector('[data-testid="lan-qr"]')
            svg = page.query_selector('[data-testid="lan-qr"] svg')
            b, sb = box.bounding_box(), svg.bounding_box()
            assert abs(sb["width"] - (b["width"] - 2)) <= 1.5  # fills the box inside its border
            assert abs(sb["width"] - sb["height"]) <= 1
            vb = page.evaluate("document.querySelector('[data-testid=\"lan-qr\"] svg')"
                               ".getAttribute('viewBox')")
            n = int(float(vb.split()[2]))
            border = 4
            img = Image.open(io.BytesIO(svg.screenshot())).convert("L")
            size = img.size[0]
            mod = size / n

            def dark(mx, my):
                return img.getpixel((int((mx + 0.5) * mod), int((my + 0.5) * mod))) < 80

            c = border + 3  # the centre module of each 7x7 finder pattern
            assert dark(c, c) and dark(n - border - 4, c) and dark(c, n - border - 4)
            # A white quiet zone on all four sides (a pixel or two of the box border is skipped).
            for x in range(2, size - 2):
                for y in list(range(2, int(mod * 3))) + list(range(size - int(mod * 3), size - 2)):
                    assert img.getpixel((x, y)) > 200 and img.getpixel((y, x)) > 200
            page.query_selector('[data-testid="st-access"]').screenshot(
                path=str(_SHOTS / f"qr-{theme}-{width}.png"))
        finally:
            ctx.close()
            browser.close()


# ------------------------------------------- the Access card after turning it on (#133)
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1366, 390])
def test_the_access_card_status_line_and_title_never_overlap_or_leave_the_card(
        monkeypatch, theme, width):
    """The state the reporter saw: access just turned on, restart sentence in the status line.
    Text boxes must not overlap each other, the title must end before its switch starts, and no
    text may run past the card's right edge."""
    import httpx
    from playwright.sync_api import sync_playwright

    from kestrel.db import migrate
    from kestrel.web import auth
    from kestrel.web.serve import running_server

    monkeypatch.setattr(auth, "lan_host_ip", lambda: "192.168.1.20")
    migrate.run_migrations()
    with running_server(worker=False) as base, sync_playwright() as p:
        httpx.post(f"{base}/settings/privacy/access", data={"on": "1", "ack": "on"})
        browser = launch_sync(p)
        ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
        page = ctx.new_page()
        try:
            page.goto(f"{base}/settings#privacy")
            page.wait_for_selector('[data-testid="lan-bind-state"]')
            wait_for_layout(page)
            card = page.query_selector('[data-testid="st-access"]')
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
            _assert_no_text_overlap(page, card, ("access", theme, width))
            cb = card.bounding_box()
            for t in page.evaluate(_TEXT_BOXES, card):
                assert t["r"] <= cb["x"] + cb["width"] + 0.5, ("past the card", t["t"], width)
            title = page.query_selector('[data-testid="st-access"] h3').bounding_box()
            switch = page.query_selector('[data-testid="st-access-switch"] + .k-switch__track')
            sw = switch.bounding_box()
            assert title["x"] + title["width"] <= sw["x"] + 1, ("title under switch", width)
            # The title text itself must fit its box (a nowrap title overflows without moving
            # its box, so the box check above alone passes while the last letter hides).
            assert page.evaluate(
                "(() => { const h = document.querySelector('[data-testid=\"st-access\"] h3');"
                " return h.scrollWidth <= h.clientWidth + 1; })()") is True, (
                "title clipped", width)
            status = page.inner_text('[data-testid="lan-bind-state"]')
            assert status.count("Takes effect after Kestrel restarts.") == 1
            card.screenshot(path=str(_SHOTS / f"access-{theme}-{width}.png"))
        finally:
            ctx.close()
            browser.close()


# ---------------------------------- the MCP card after a failed start turned it off (#130)
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1366, 390])
def test_the_mcp_card_turned_off_sentence_sits_clear_of_everything(monkeypatch, theme, width):
    import asyncio

    from playwright.sync_api import sync_playwright
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.engine import make_async_engine
    from kestrel.db.models import Setting
    from kestrel.services.setting_overrides import MCP_TURNED_OFF_KEY
    from kestrel.web.app import mcp_turned_off_sentence
    from kestrel.web.serve import running_server

    migrate.run_migrations()
    sentence = mcp_turned_off_sentence(
        "a part the assistant connection needs is missing from this install; "
        "the details are in the log")

    async def seed():
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            s.add(Setting(key="mcp.enabled", value=False))
            s.add(Setting(key=MCP_TURNED_OFF_KEY, value=sentence))
            await s.commit()
        await engine.dispose()

    asyncio.run(seed())
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
        page = ctx.new_page()
        try:
            page.goto(f"{base}/settings#privacy")
            page.wait_for_selector('[data-testid="mcp-turned-off"]')
            wait_for_layout(page)
            card = page.query_selector('[data-testid="st-mcp"]')
            assert page.inner_text('[data-testid="mcp-turned-off"]') == sentence
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
            _assert_no_text_overlap(page, card, ("mcp_off", theme, width))
            cb = card.bounding_box()
            for t in page.evaluate(_TEXT_BOXES, card):
                assert t["r"] <= cb["x"] + cb["width"] + 0.5, ("past the card", t["t"], width)
            card.screenshot(path=str(_SHOTS / f"mcp-turned-off-{theme}-{width}.png"))
        finally:
            ctx.close()
            browser.close()


# ------------------------------------ "Make a new key" sits where it is found (D-UI-267)
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1680, 390])
def test_make_a_new_key_is_visible_without_show_details_and_asks_first(monkeypatch, theme, width):
    """D-UI-267: the button is visible whenever access is on, next to the key it replaces,
    without opening Show details; making a new one still asks first and still signs out every
    device that used the old key; access off hides it again."""
    import httpx
    from playwright.sync_api import sync_playwright

    from kestrel.db import migrate
    from kestrel.web import auth
    from kestrel.web.serve import running_server

    monkeypatch.setattr(auth, "lan_host_ip", lambda: "192.168.1.20")
    migrate.run_migrations()
    with running_server(worker=False) as base, sync_playwright() as p:
        httpx.post(f"{base}/settings/privacy/access", data={"on": "1", "ack": "on"})
        browser = launch_sync(p)
        ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
        page = ctx.new_page()
        try:
            page.goto(f"{base}/settings#privacy")
            page.wait_for_selector('[data-testid="st-access"]')
            wait_for_layout(page)
            btn = page.query_selector('[data-testid="rotate-open"]')
            assert btn is not None and btn.is_visible(), (theme, width)
            det = page.query_selector('[data-testid="st-access"] details')
            assert det.get_attribute("open") is None
            assert det.query_selector("button") is None
            old_join = page.get_attribute('[data-testid="copy-join-url"]', "data-st-copy")
            assert "key=" in old_join
            btn.click()
            page.wait_for_selector('#st-rotate-dialog.is-open')
            assert page.get_attribute('[data-testid="copy-join-url"]', "data-st-copy") == old_join
            assert page.is_disabled('#st-rotate-go')
            page.check('[data-testid="rotate-ack"]')
            assert not page.is_disabled('#st-rotate-go')
            page.screenshot(path=str(_SHOTS / f"rotate-confirm-{theme}-{width}.png"))
            page.click('[data-testid="rotate-secret"]')
            page.wait_for_selector('[data-testid="lan-bind-note"]')
            assert "New key made" in page.inner_text('[data-testid="lan-bind-note"]')
            new_join = page.get_attribute('[data-testid="copy-join-url"]', "data-st-copy")
            assert new_join and new_join != old_join and "key=" in new_join
            card = page.query_selector('[data-testid="st-access"]')
            card.screenshot(path=str(_SHOTS / f"rotate-{theme}-{width}.png"))
            page.click('[data-testid="lan-bind-off"]')
            page.wait_for_function(
                "() => document.querySelector('[data-testid=\"lan-bind-state\"]')"
                ".textContent.startsWith('Off.')")
            assert page.inner_text('[data-testid="lan-bind-state"]').startswith("Off.")
            assert page.query_selector('[data-testid="rotate-open"]') is None
        finally:
            ctx.close()
            browser.close()


def test_make_a_new_key_press_gives_a_brief_state_and_reduced_motion_kills_it(monkeypatch):
    """AGENTS.md motion rule: a button press gives a brief state change (a scale on :active
    with a transition), and the OS reduced-motion setting turns the transition off."""
    import httpx
    from playwright.sync_api import sync_playwright

    from kestrel.db import migrate
    from kestrel.web import auth
    from kestrel.web.serve import running_server

    monkeypatch.setattr(auth, "lan_host_ip", lambda: "192.168.1.20")
    migrate.run_migrations()
    with running_server(worker=False) as base, sync_playwright() as p:
        httpx.post(f"{base}/settings/privacy/access", data={"on": "1", "ack": "on"})
        browser = launch_sync(p)
        try:
            for reduced in (False, True):
                ctx = browser.new_context(
                    viewport={"width": 1680, "height": 900}, color_scheme="light",
                    reduced_motion="reduce" if reduced else "no-preference")
                page = ctx.new_page()
                try:
                    page.goto(f"{base}/settings#privacy")
                    page.wait_for_selector('[data-testid="rotate-open"]')
                    wait_for_layout(page)
                    btn = page.query_selector('[data-testid="rotate-open"]')
                    dur = page.evaluate("b => getComputedStyle(b).transitionDuration", btn)
                    # The press state exists in both modes; reduced motion only kills the
                    # transition (the scale is applied instantly instead of over 0.1s).
                    assert (dur == "0s") == reduced, (reduced, dur)
                    box = btn.bounding_box()
                    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    page.mouse.down()
                    tr = page.evaluate("b => getComputedStyle(b).transform", btn)
                    page.mouse.up()
                    assert tr not in (None, ""), (reduced, tr)
                finally:
                    ctx.close()
        finally:
            browser.close()


# ------------------------------------------------- restore from a backup (issue #172)
# The Restore confirm used to close and restore nothing: the button was type="submit" and
# carried data-k-close, and kestrel-ui.js's close handler won the click and cancelled the
# submit, so the form never went out. These tests click the real button with a real backup:
# the request goes out, the reply renders in the Backups row, and the dialog closes only
# after it has landed (animated, or instant under reduced motion). Cancel never sends.

def _seed_org(name: str) -> None:
    """Insert an org row with plain sqlite. The test thread cannot use asyncio.run while the
    browser is open: Playwright's sync API keeps its event loop running in a greenlet on this
    same thread, so the thread-local running-loop flag is set for the whole test."""
    import sqlite3
    from datetime import UTC, datetime

    from kestrel import paths

    now = datetime.now(UTC).isoformat()
    con = sqlite3.connect(paths.db_path())
    try:
        con.execute(
            "insert into org (name, normalized_name, first_seen_at, last_seen_at)"
            " values (?, ?, ?, ?)",
            (name, name.lower(), now, now),
        )
        con.commit()
    finally:
        con.close()


def _org_names() -> set[str]:
    import sqlite3

    from kestrel import paths

    con = sqlite3.connect(paths.db_path())
    try:
        return {row[0] for row in con.execute("select normalized_name from org")}
    finally:
        con.close()


def _restore_page(live, tmp_path, *, reduced: bool = False):
    """A valid backup of one seeded org; the database then gains a second org, so only a
    real restore puts the original data back. The confirm is opened on the backup file.
    Returns (ctx, page, errors, posts)."""
    from kestrel.services.backup import create_backup

    _seed_org("Acme Robotics")
    archive = create_backup(tmp_path)
    _seed_org("Someone Else")  # after the backup, so a no-op restore is caught

    base, browser = live
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 1000}, color_scheme="light",
        reduced_motion="reduce" if reduced else "no-preference")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    posts: list[str] = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
    page.goto(f"{base}/settings#data")
    page.wait_for_selector('[data-testid="st-nav-data"]')
    wait_for_layout(page)  # section routed and settled
    page.set_input_files('#st-restore-file', str(archive))
    page.wait_for_selector('#st-restore-dialog[aria-hidden="false"]')
    wait_for_layout(page)  # the open animation is done, so the close is the only motion left
    return ctx, page, errors, posts


def _restore_posts(posts: list[str]) -> list[str]:
    return [u for u in posts if u.endswith("/settings/restore")]


def test_restore_click_sends_the_request_and_renders_the_reply(live, tmp_path):
    """Clicking Restore sends the upload, the reply renders in the Backups row, the dialog
    closes, and the database holds the backup's data (the later org is gone)."""
    ctx, page, errors, posts = _restore_page(live, tmp_path)
    try:
        assert not _restore_posts(posts)
        with page.expect_response(
                lambda r: r.request.method == "POST"
                and r.url.endswith("/settings/restore")) as info:
            page.click('[data-testid="st-restore-go"]')
        response = info.value
        assert response.status == 200
        assert "Restored from" in response.text()
        page.wait_for_selector('#st-restore-res [data-testid="st-restored"]')
        assert "Restored from" in page.inner_text('#st-restore-res [data-testid="st-restored"]')
        page.wait_for_selector('#st-restore-dialog[aria-hidden="true"]')
        assert len(_restore_posts(posts)) == 1, posts  # the one click, one send
        assert _org_names() == {"acme robotics"}, _org_names()
        assert not errors, errors
    finally:
        ctx.close()


def test_restore_cancel_closes_the_dialog_without_sending(live, tmp_path):
    ctx, page, errors, posts = _restore_page(live, tmp_path)
    try:
        page.click('[data-st-restore-cancel]')
        page.wait_for_selector('#st-restore-dialog[aria-hidden="true"]')
        wait_for_layout(page)
        assert not _restore_posts(posts), posts
        assert _org_names() == {"acme robotics", "someone else"}  # nothing was replaced
        assert not errors, errors
    finally:
        ctx.close()


# Samples the dialog's opacity on every animation frame, so a close that fades is caught
# frame by frame and a reduced-motion close (no in-between frames) fails on the same read.
_TRACE_CLOSE = """() => {
  const el = document.querySelector('#st-restore-dialog');
  window.__trace = [];
  const t0 = performance.now();
  const tick = () => {
    window.__trace.push({ o: parseFloat(getComputedStyle(el).opacity) });
    if (performance.now() - t0 < 4000) requestAnimationFrame(tick);
    else window.__traceDone = true;
  };
  window.__traceDone = false;
  requestAnimationFrame(tick);
}"""


def _click_restore_and_read_trace(page, posts):
    page.evaluate(_TRACE_CLOSE)
    page.click('[data-testid="st-restore-go"]')
    page.wait_for_function("() => window.__traceDone === true")
    trace = page.evaluate("() => window.__trace")
    assert page.get_attribute('#st-restore-dialog', 'aria-hidden') == "true"
    assert len(_restore_posts(posts)) == 1, posts
    return trace


def test_restore_dialog_closes_animated_after_the_reply(live, tmp_path):
    ctx, page, errors, posts = _restore_page(live, tmp_path)
    try:
        trace = _click_restore_and_read_trace(page, posts)
        between = [s for s in trace if 0 < s["o"] < 1]
        assert between, (
            f"the close after the restore did not animate (no in-between opacity): "
            f"{trace[:8]} ... {trace[-4:]}")
        assert not errors, errors
    finally:
        ctx.close()


def test_restore_dialog_close_is_instant_under_reduced_motion(live, tmp_path):
    ctx, page, errors, posts = _restore_page(live, tmp_path, reduced=True)
    try:
        trace = _click_restore_and_read_trace(page, posts)
        between = [s for s in trace if 0 < s["o"] < 1]
        assert not between, (
            f"reduced motion still animated the close: {trace[:8]} ... {trace[-4:]}")
        assert not errors, errors
    finally:
        ctx.close()


# ------------------------------------------------------------------ the key holder's view (#155)
_DISABLED_BOXES = """() => Array.from(
  document.querySelectorAll('button:disabled, .k-localonly__note'))
  .filter(e => e.offsetParent !== null).map(e => { const r = e.getBoundingClientRect();
    const g = e.closest('section, .panel, .k-card');
    return {t: (e.textContent || '').trim().slice(0, 24), l: r.left, r: r.right,
            tp: r.top, bt: r.bottom, g: g ? (g.id || g.className) : ''}; })"""


@pytest.fixture
def key_holder(monkeypatch):
    """A server with a published tunnel name, and a browser that reaches it under that name with
    the shared key in its cookie jar: to the app that is a key holder, not this computer."""
    from playwright.sync_api import sync_playwright

    from kestrel.db import migrate
    from kestrel.web import auth
    from kestrel.web.serve import running_server
    from tests.browser import _launch_kwargs

    monkeypatch.setenv("KESTREL_SERVER__PUBLIC_HOSTNAMES", '["keyholder.example.com"]')
    migrate.run_migrations()
    auth.set_cached_key("browser-test-key")
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = None
        for kwargs in _launch_kwargs():
            try:
                browser = p.chromium.launch(
                    headless=True, args=["--host-resolver-rules=MAP keyholder.example.com "
                                         "127.0.0.1"], **kwargs)
                break
            except Exception:  # noqa: BLE001 -- Playwright Error: this candidate is not installed
                continue
        assert browser is not None, "no browser could start"
        port = base.rsplit(":", 1)[1]
        yield f"http://keyholder.example.com:{port}", browser
        browser.close()
    auth.reset_cache()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 390])
def test_a_key_holder_sees_disabled_controls_and_the_sentence_without_overlap(
        key_holder, theme, width):
    base, browser = key_holder
    ctx = browser.new_context(viewport={"width": width, "height": 1000}, color_scheme=theme)
    ctx.add_cookies([{"name": "kestrel_key", "value": "browser-test-key", "url": base + "/"}])
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        for path, selector in (("/settings/hosted", '[data-testid="local-only-fence"]'),
                               ("/backup", '[data-testid="local-only-download"]'),
                               ("/settings#data", '[data-testid="st-backup"][disabled]'),
                               ("/settings#scoring", '[data-testid="local-only-download"]')):
            page.goto(base + path)
            page.wait_for_selector(selector, state="attached")
            wait_for_layout(page)
            # /settings/hosted is the legacy panel: its inline 24rem key box already overflows a
            # 390 px window by 2 px for this computer too, so only the other pages are held to it.
            fits = path == "/settings/hosted" or page.evaluate(
                "document.documentElement.scrollWidth <= innerWidth")
            assert fits is True, path
            notes = page.evaluate("Array.from(document.querySelectorAll("
                                  "'[data-testid=local-only-note]')).map(e => e.innerText.trim())")
            assert set(notes) <= {"Only this computer can change this.",
                                  "Only this computer can download this."}, (path, notes)
            lines = [page.evaluate(_LINES, el)
                     for el in page.query_selector_all(".k-btn:disabled, .btn:disabled")
                     if el.is_visible()]
            assert max(lines, default=0) <= 2, (path, lines)
            boxes = [b for b in page.evaluate(_DISABLED_BOXES) if b["r"] > b["l"]]
            clashes = [(a["t"], b["t"]) for i, a in enumerate(boxes) for b in boxes[i + 1:]
                       if a["g"] == b["g"] and _overlaps(a, b)]  # controls of one card
            assert clashes == [], (path, clashes, theme, width)
        assert not errors, errors
    finally:
        ctx.close()
