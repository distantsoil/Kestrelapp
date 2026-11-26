"""The install ID in a real browser (D-INST-1): About and Settings > Your data.

Copy puts the ID on the clipboard and plays a short confirmation (read from the animation itself,
never a frame count); the OS reduced-motion setting and the app's Reduce motion switch turn it
off; the details toggle opens; no horizontal scroll and nothing clipped at 390 and 1440, light
and dark. Screenshots go to KESTREL_SHOTS_DIR when it is set (never into the repo).

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.browser import launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

PAGES = (("/about", "ab"), ("/settings#data", "st"))

# Every element of the block lies inside the viewport and none of its text is cut off.
_FITS = """root => {
  const vw = document.documentElement.clientWidth;
  const bad = [];
  for (const e of root.querySelectorAll('*')) {
    if (e.offsetParent === null || e.classList.contains('k-sr-only')) continue;  // hidden by design
    const b = e.getBoundingClientRect();
    if (b.right > vw + 1 || b.left < -1) bad.push('outside: ' + e.className);
    if (e.scrollWidth > e.clientWidth + 1 && getComputedStyle(e).overflowX !== 'visible')
      bad.push('clipped: ' + e.className);
  }
  return bad;
}"""


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


def _open(live, path, prefix, *, theme="light", width=1440, height=900, motion="no-preference"):
    base, browser = live
    ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=theme,
                              reduced_motion=motion)
    ctx.grant_permissions(["clipboard-read", "clipboard-write"])
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}{path}")
    page.wait_for_selector(f'[data-testid="{prefix}-iid-short"]', state="visible")
    wait_for_layout(page)
    return ctx, page, errors


def _shot(page, name: str) -> None:
    out = os.environ.get("KESTREL_SHOTS_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(out) / f"{name}.png"), full_page=False)


@pytest.mark.parametrize("path,prefix", PAGES)
def test_copy_puts_the_short_id_on_the_clipboard_with_a_short_confirmation(live, path, prefix):
    ctx, page, errors = _open(live, path, prefix)
    try:
        short = page.inner_text(f'[data-testid="{prefix}-iid-short"]')
        assert short.startswith("K-") and len(short) == 11
        btn = page.locator(f'[data-testid="{prefix}-iid-copy"]')
        btn.click()
        page.wait_for_function(
            "el => el.classList.contains('is-copied')", arg=btn.element_handle())
        # The animation the class starts, read from its computed style (stable for as long as the
        # class is on, unlike a running-animation list that a slow machine may see end).
        assert btn.evaluate("el => getComputedStyle(el).animationName") == "k-iid-copied"
        assert btn.inner_text() == "Copied"
        assert page.evaluate("navigator.clipboard.readText()") == short
        said = page.locator(f'[data-testid="{prefix}-install-id"] [data-iid-said]')
        assert said.text_content() == "Copied"
        # The label comes back.
        page.wait_for_function("el => el.textContent === 'Copy'", arg=btn.element_handle())
        assert errors == []
    finally:
        ctx.close()


@pytest.mark.parametrize("path,prefix", PAGES)
def test_show_details_reveals_the_full_id_and_its_copy(live, path, prefix):
    ctx, page, errors = _open(live, path, prefix)
    try:
        full = page.locator(f'[data-testid="{prefix}-iid-full"]')
        assert not full.is_visible()
        page.click(f'[data-testid="{prefix}-iid-details"] > summary')
        full.wait_for(state="visible")
        value = full.inner_text()
        assert len(value) == 36 and value.count("-") == 4
        page.click(f'[data-testid="{prefix}-iid-copy-full"]')
        page.wait_for_function("v => navigator.clipboard.readText().then(t => t === v)", arg=value)
        assert errors == []
    finally:
        ctx.close()


@pytest.mark.parametrize("path,prefix", PAGES)
def test_reduced_motion_turns_the_confirmation_off(live, path, prefix):
    ctx, page, _ = _open(live, path, prefix, motion="reduce")
    try:
        btn = page.locator(f'[data-testid="{prefix}-iid-copy"]')
        btn.click()
        page.wait_for_function(
            "el => el.classList.contains('is-copied')", arg=btn.element_handle())
        assert btn.evaluate("el => getComputedStyle(el).animationName") == "none"
        assert btn.evaluate("el => el.getAnimations().length") == 0
    finally:
        ctx.close()


def _seed_reduce_motion(on: bool) -> None:
    """The stored pref the app's own switch writes, so the server renders that state and the
    rail poll's snapshot agrees with it. The browser tests own this thread's event loop, so the
    database write runs on a worker thread (as tests.quick_fixtures does)."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from kestrel.db.engine import get_sessionmaker
    from kestrel.services import ui_state

    async def _go():
        async with get_sessionmaker()() as s:
            await ui_state.set_value(s, "ui.reduce_motion", on)
            await s.commit()

    with ThreadPoolExecutor(1) as ex:
        ex.submit(asyncio.run, _go()).result()


@pytest.mark.parametrize("path,prefix", PAGES)
def test_the_apps_reduce_motion_switch_turns_the_confirmation_off(live, path, prefix):
    """The app's Reduce motion switch is ON (stored, so the page renders reduced): the copy
    confirmation keeps its words and loses its pulse. The stored pref and the page's derived
    state agree, so the status rail's poll cannot flip either (issue: the switch reset itself)."""
    _seed_reduce_motion(True)
    ctx, page, errors = _open(live, path, prefix)
    try:
        assert page.evaluate(
            "document.documentElement.getAttribute('data-reduce-motion')") == "1"
        btn = page.locator(f'[data-testid="{prefix}-iid-copy"]')
        btn.click()
        page.wait_for_function(
            "el => el.classList.contains('is-copied')", arg=btn.element_handle())
        assert btn.evaluate("el => getComputedStyle(el).animationName") == "none"
        assert btn.evaluate("el => el.getAnimations().length") == 0
        assert btn.inner_text() == "Copied"  # the words stay; only the pulse is off
        assert errors == []
    finally:
        ctx.close()


def test_the_real_reduce_motion_switch_survives_the_status_rail_poll(live):
    """The app's Reduce motion switch stays on across the sidebar's own status poll: the poll
    re-ran initPrefs, which rewrote data-reduce-motion from the stale server-rendered snapshot,
    so the switch reset itself and the confirmation's pulse came back until reload. Wait on the
    poll's own response (never a sleep) and assert the attribute and the animation."""
    _seed_reduce_motion(False)  # the stored pref starts off; the test turns the REAL switch on
    ctx, page, errors = _open(live, "/settings#data", "st")
    try:
        assert page.evaluate(
            "document.documentElement.getAttribute('data-reduce-motion')") == "0"
        page.evaluate("location.hash = '#preferences'")
        page.wait_for_selector(
            '#st-sec-preferences:not([hidden]) [data-st-key="ui.reduce_motion"]')
        page.click('[data-st-key="ui.reduce_motion"] + .k-switch__track')
        page.wait_for_selector('.k-toast:has-text("Motion reduced.")')
        assert page.evaluate(
            "document.documentElement.getAttribute('data-reduce-motion')") == "1"
        # Past one whole status poll (it runs on its own cadence): the switch stays on.
        with page.expect_response(lambda r: "/status/rail" in r.url, timeout=20000):
            pass
        assert page.evaluate(
            "document.documentElement.getAttribute('data-reduce-motion')") == "1"
        assert page.evaluate("document.documentElement.classList.contains('st-rm')")
        # The derived state agrees: the copy confirmation stays animation-free.
        page.evaluate("location.hash = '#data'")
        page.wait_for_selector('[data-testid="st-iid-copy"]', state="visible")
        btn = page.locator('[data-testid="st-iid-copy"]')
        btn.click()
        page.wait_for_function(
            "el => el.classList.contains('is-copied')", arg=btn.element_handle())
        assert btn.evaluate("el => getComputedStyle(el).animationName") == "none"
        assert errors == []
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
@pytest.mark.parametrize("path,prefix", PAGES)
def test_nothing_overflows_or_clips(live, path, prefix, theme, width, height):
    ctx, page, _ = _open(live, path, prefix, theme=theme, width=width, height=height)
    try:
        page.click(f'[data-testid="{prefix}-iid-details"] > summary')
        page.locator(f'[data-testid="{prefix}-iid-full"]').wait_for(state="visible")
        wait_for_layout(page)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        block = page.locator(f'[data-testid="{prefix}-install-id"]')
        assert block.evaluate(_FITS) == []
        block.scroll_into_view_if_needed()
        name = "about" if prefix == "ab" else "settings-data"
        _shot(page, f"{name}-{theme}-{width}")
    finally:
        ctx.close()
