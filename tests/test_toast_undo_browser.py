"""The shared toast Undo in a real browser (D-UI-160, R5): a failed request says nothing changed
and does not announce an undo; a refused Undo (the marker in the answer) shows its message.
Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import os

import pytest

from tests.browser import launch_sync, wait_for_app_ready, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)


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


def _undo(live, handler):
    base, browser = live
    page = browser.new_page()
    page.goto(f"{base}/about")
    page.wait_for_selector("h1")
    page.route("**/x-undo", handler)
    page.evaluate("""() => { window.__undone = 0;
      document.addEventListener('kestrel:undone', () => { window.__undone += 1; });
      KUI.toast('Moved.', {undoUrl: '/x-undo'}); }""")
    page.click(".k-toast__undo")
    page.wait_for_function("""() => window.__undone > 0 ||
        [...document.querySelectorAll('.k-toast span')].some(
            node => node.textContent !== 'Moved.')""")
    page.locator(".k-toast__undo").wait_for(state="detached")
    texts = page.evaluate(
        "Array.from(document.querySelectorAll('.k-toast span')).map(e => e.textContent)")
    return texts, page.evaluate("window.__undone")


def test_a_server_error_says_nothing_changed_and_announces_no_undo(live):
    texts, undone = _undo(live, lambda route: route.fulfill(status=500, body="boom"))
    assert "Could not undo. Nothing changed." in texts and undone == 0


def test_a_dropped_connection_says_nothing_changed(live):
    texts, undone = _undo(live, lambda route: route.abort())
    assert "Could not undo. Nothing changed." in texts and undone == 0


def test_a_refused_undo_shows_its_message(live):
    body = '<span data-undo-msg data-ok="0">This can no longer be undone.</span>'
    texts, _undone = _undo(live, lambda route: route.fulfill(status=200, body=body,
                                                              content_type="text/html"))
    assert "This can no longer be undone." in texts


def test_an_undo_that_worked_announces_itself_quietly(live):
    texts, undone = _undo(live, lambda route: route.fulfill(status=204))
    assert undone == 1 and "Could not undo. Nothing changed." not in texts


def test_a_toast_and_its_undo_outlive_the_page_that_raised_them(live):
    """D-UI-210: a toast belongs to the shell, not to the page. Raised while Settings is mounted,
    its Undo still works after Settings is unmounted, and its request is not cancelled with the
    page's signal."""
    base, browser = live
    page = browser.new_page()
    page.goto(f"{base}/settings")
    page.wait_for_selector('[data-testid="st-nav-computer"]')
    assert page.evaluate("KUI.page.current()") == "settings"
    page.route("**/x-undo", lambda route: route.fulfill(status=204))
    page.evaluate("""() => { window.__undone = 0;
      document.addEventListener('kestrel:undone', () => { window.__undone += 1; });
      KUI.toast('Moved.', {undoUrl: '/x-undo'}); }""")
    page.evaluate("KUI.page.unmount()")
    page.click(".k-toast__undo")
    page.wait_for_function("window.__undone === 1")


def _press_failing_button(live, handler) -> list[str]:
    """Press an htmx button whose request fails; return the toast texts that stay on screen."""
    base, browser = live
    page = browser.new_page()
    page.goto(f"{base}/about")
    page.wait_for_selector("h1")
    page.route("**/x-save", handler)
    page.evaluate("""() => {
      const b = document.createElement('button');
      b.id = 'x-save'; b.textContent = 'Save'; b.setAttribute('hx-post', '/x-save');
      b.setAttribute('hx-swap', 'none');
      document.body.appendChild(b); htmx.process(b); }""")
    page.click("#x-save")
    page.wait_for_selector(".k-toast")
    page.wait_for_timeout(400)  # the generic failure toast is scheduled on a 0ms timer
    return page.evaluate(
        "Array.from(document.querySelectorAll('.k-toast span')).map(e => e.textContent)")


def test_a_503_with_its_own_toast_shows_exactly_one_toast(live):
    """#89: the busy answer carries its own HX-Trigger toast; the generic one must not double it."""
    import json

    own = "Kestrel is busy saving something else. Nothing changed; try again in a moment."
    texts = _press_failing_button(
        live,
        lambda route: route.fulfill(
            status=503,
            body=own,
            headers={"HX-Trigger": json.dumps({"k-toast": {"message": own, "tone": "warn"}})},
        ),
    )
    assert texts == [own]


def test_a_failure_with_no_toast_of_its_own_still_gets_the_generic_one(live):
    texts = _press_failing_button(live, lambda route: route.fulfill(status=500, body="boom"))
    assert len(texts) == 1 and texts[0].startswith("That did not go through")


def test_a_toast_auto_dismisses_when_test_mode_is_off_and_stays_in_test_mode(live):
    """D-TEST-8: the server marks <html data-test-mode> under KESTREL_TEST_MODE=1 and toasts then
    stay until dismissed. With the mark removed (a real run) a short timeout still dismisses."""
    base, browser = live
    page = browser.new_page()
    page.goto(f"{base}/about")
    wait_for_app_ready(page)
    assert page.evaluate("document.documentElement.dataset.testMode") == "1"
    page.evaluate("KUI.toast('Stays.', {timeout: 150})")
    page.wait_for_selector(".k-toast")
    page.wait_for_timeout(600)  # past the 150 ms timer: a negative check has no signal to wait on
    assert page.locator(".k-toast").count() == 1
    page.evaluate("delete document.documentElement.dataset.testMode")
    page.evaluate("KUI.toast('Goes.', {timeout: 150})")
    page.locator(".k-toast", has_text="Goes.").wait_for(state="detached")
    assert page.locator(".k-toast", has_text="Stays.").count() == 1


def test_a_change_during_a_run_raises_a_notice_whose_button_stops_the_run(live):
    """#103: a save answered with the run-notice event shows the notice with a Stop the run
    button, and the button posts to /runs/stop, once."""
    import json

    base, browser = live
    page = browser.new_page()
    page.goto(f"{base}/about")
    page.wait_for_selector("h1")
    detail = {
        "message": "This run is still using qwen3:14b on this computer, as confirmed. Your change "
                   "applies to the next run. Stop the run if you do not want it to continue.",
        "tone": "warn", "undoUrl": "/runs/stop", "undoLabel": "Stop the run", "timeout": 20000,
    }
    page.route("**/x-save", lambda route: route.fulfill(
        status=200, body="saved", content_type="text/html",
        headers={"HX-Trigger": json.dumps({"k-run-notice": detail})}))
    stops: list[str] = []
    page.route("**/runs/stop", lambda route: (stops.append(route.request.method),
                                              route.fulfill(status=204)))
    page.evaluate("htmx.ajax('POST', '/x-save', {target: 'body', swap: 'none'})")
    page.wait_for_selector(".k-toast__undo")
    assert page.locator(".k-toast__undo").inner_text() == "Stop the run"
    assert "Your change applies to the next run" in page.locator(".k-toast").inner_text()
    assert page.locator(".k-toast").get_attribute("data-tone") == "warn"
    page.click(".k-toast__undo")
    page.locator(".k-toast__undo").wait_for(state="detached")
    assert stops == ["POST"]


def test_a_real_settings_save_during_a_run_shows_the_notice_and_stop_ends_the_hold(live):
    """#103 end to end: the run is held the way the worker loop holds it, a real save goes to the
    real route from the page, the real middleware answers with the notice, and pressing its Stop
    button posts to the real /runs/stop, which ends the run and releases the held settings."""
    import asyncio
    import time
    from concurrent.futures import ThreadPoolExecutor

    from kestrel.services.setting_overrides import load_settings_with_db
    from kestrel.web import app as app_mod
    from kestrel.web.deps import sessionmaker
    from tests import runs_seed

    async def _held():
        async with sessionmaker()() as s:
            return await load_settings_with_db(s)

    def _setup():  # off the browser's event loop: seeding and loading run their own
        runs_seed.seed(active=True)
        return asyncio.run(_held())

    base, browser = live
    with ThreadPoolExecutor(max_workers=1) as pool:
        held = pool.submit(_setup).result()
    app_mod._hold_run(held)
    try:
        page = browser.new_page()
        page.goto(f"{base}/about")
        page.wait_for_selector("h1")
        page.evaluate("htmx.ajax('POST', '/models/speed', "
                      "{values: {tier1: '9', tier2: '2'}, target: 'body', swap: 'none'})")
        page.wait_for_selector(".k-toast__undo")
        assert page.locator(".k-toast__undo").inner_text() == "Stop the run"
        assert "Your change applies to the next run" in page.locator(".k-toast").inner_text()
        assert app_mod.confirmed_run_settings() is not None
        page.click(".k-toast__undo")
        page.locator(".k-toast__undo").wait_for(state="detached")
        deadline = time.monotonic() + 10
        while app_mod.confirmed_run_settings() is not None:
            assert time.monotonic() < deadline, "Stop did not release the run hold"
            time.sleep(0.05)
    finally:
        app_mod._release_run()
        app_mod.set_paused(False)


# ------------------------------------------------ the run card says only what it stored
# Settings > This computer, "How should Kestrel run?". Invariant: the card shows what is
# stored, and a message that a choice was saved appears once, only when it was.
_ONLINE_OFF_ROWS = {"enabled": False, "choice": "local", "mode": "local"}
_ONLINE_ON_ROWS = {"enabled": True, "choice": "local", "mode": "local"}


def _off_loop(fn):
    """Run `fn` on its own thread: the browser owns this thread's event loop."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn).result()


def _seed_online(*, enabled: bool, choice: str, mode: str) -> None:
    """Online models set up once (disclosure accepted as it reads now, both scoring steps
    assigned), switched on or off, with the run card rows as given."""
    import asyncio

    from kestrel.config import HostedMode
    from kestrel.db.models import Setting
    from kestrel.web.deps import sessionmaker

    rows = {
        "llm.hosted_mode.enabled": enabled,
        "llm.hosted_mode.acknowledged_at": "2026-10-01T00:00:00+00:00",
        "llm.hosted_mode.acknowledged_terms": HostedMode().disclosure_terms(),
        "setup.run_choice": choice, "llm.mode": mode,
    }
    for tier in ("tier1", "tier2"):
        rows[f"llm.hosted_tiers.{tier}.backend"] = "frontier"
        rows[f"llm.hosted_tiers.{tier}.model"] = "invented/model-for-tests"

    async def write():
        async with sessionmaker()() as s:
            for key, value in rows.items():
                s.add(Setting(key=key, value=value))
            await s.commit()

    _off_loop(lambda: asyncio.run(write()))


def _stored_choice():
    import asyncio

    from kestrel.db.models import Setting
    from kestrel.web.deps import sessionmaker

    async def read():
        async with sessionmaker()() as s:
            row = await s.get(Setting, "setup.run_choice")
            return None if row is None else row.value

    return _off_loop(lambda: asyncio.run(read()))


_CARD = """() => ({
  toasts: Array.from(document.querySelectorAll('.k-toast span')).map(e => e.textContent),
  on: Array.from(document.querySelectorAll('.st-mc[aria-checked="true"]'))
    .map(e => e.dataset.testid),
  said: Array.from(document.querySelectorAll('[data-testid="st-mode-message"]'))
    .filter(e => e.offsetParent !== null).map(e => e.textContent.trim()),
})"""


def _open_run_card(live, **viewport):
    base, browser = live
    page = browser.new_page(**viewport)
    page.goto(f"{base}/settings#computer")
    wait_for_app_ready(page)
    page.wait_for_selector('[data-testid="st-mode-online"]')
    return page


def _press_and_wait_for_the_card(page, key: str, release=None) -> dict:
    """Press one run card and wait until the card it answers with is on the page."""
    page.evaluate("document.getElementById('st-mode').dataset.before = '1'")
    page.click(f'[data-testid="st-mode-{key}"]')
    if release is not None:
        release()
    page.wait_for_function("""() => { const card = document.getElementById('st-mode');
        return !!card && card.dataset.before !== '1'; }""")
    return page.evaluate(_CARD)


def test_choosing_online_while_online_models_are_off_says_why_and_raises_no_toast(live):
    """Online models set up, then switched off. Choosing Online model used to raise "Kestrel
    will use an online model" with Undo and show On this computer again, and a second press
    stacked a second one. Now nothing is stored, no toast is raised, and the card says why."""
    from kestrel.services import setup_state as ss

    _seed_online(**_ONLINE_OFF_ROWS)
    page = _open_run_card(live)
    for press in ("first", "second"):
        seen = _press_and_wait_for_the_card(page, "online")
        assert seen["toasts"] == [], press
        assert seen["on"] == ["st-mode-local"], press
        assert seen["said"] == [ss.ONLINE_OFF], press
        assert _stored_choice() == "local", press
    assert page.locator('[data-testid="st-online-models"]').is_visible()  # the place it names
    page.reload()
    wait_for_app_ready(page)
    page.wait_for_selector('[data-testid="st-mode-online"]')
    assert page.evaluate(_CARD) == {"toasts": [], "on": ["st-mode-local"], "said": []}


def test_a_second_run_choice_pressed_while_the_first_is_in_flight_is_not_sent(live):
    """Online models on. Online model is pressed, then Both before the answer is back: one
    request, one message, and the card shows what is stored."""
    _seed_online(**_ONLINE_ON_ROWS)
    page = _open_run_card(live)
    held: list = []

    def hold(route):
        if route.request.method == "POST":
            held.append(route)  # answered only when the test lets it go
        else:
            route.continue_()

    page.route("**/settings/computer/mode", hold)

    def press_both_then_let_the_first_go():
        page.wait_for_function("() => document.querySelectorAll('.htmx-request').length > 0")
        page.click('[data-testid="st-mode-both"]')
        held[0].continue_()

    seen = _press_and_wait_for_the_card(page, "online", press_both_then_let_the_first_go)
    assert len(held) == 1  # the second press sent nothing
    assert seen["toasts"] == ["Kestrel will use an online model and ask before sending."]
    assert seen["on"] == ["st-mode-online"] and _stored_choice() == "hosted"


_FIT = """() => {
  const card = document.getElementById('st-mode').getBoundingClientRect();
  const said = document.querySelector('[data-testid="st-mode-message"]');
  const box = said.getBoundingClientRect();
  const hit = (a, b) => a.left < b.right - 1 && b.left < a.right - 1
    && a.top < b.bottom - 1 && b.top < a.bottom - 1;
  const others = [document.querySelector('#st-mode .st-modes'),
                  document.querySelector('[data-testid="st-online-models-note"]')];
  return {
    sideways: document.documentElement.scrollWidth > innerWidth,
    inside: box.left >= card.left && box.right <= card.right + 1 && box.height > 0,
    clipped: said.scrollWidth > said.clientWidth + 1 || said.scrollHeight > said.clientHeight + 1,
    overlaps: others.filter(e => hit(box, e.getBoundingClientRect())).length,
    color: getComputedStyle(said).color,
    plain: getComputedStyle(document.querySelector('[data-testid="st-online-models-note"]')).color,
  };
}"""


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 390])
def test_the_refusal_fits_the_card_in_both_schemes_wide_and_narrow(live, theme, width):
    """The reason is whole, inside the card, over nothing, and reads as a refusal. Screenshots
    go to the folder KESTREL_SHOTS names when it is set (never into the repository)."""
    import pathlib

    _seed_online(**_ONLINE_OFF_ROWS)
    page = _open_run_card(live, viewport={"width": width, "height": 1000}, color_scheme=theme)
    wait_for_layout(page)
    _press_and_wait_for_the_card(page, "online")
    wait_for_layout(page)
    fit = page.evaluate(_FIT)
    assert not fit["sideways"] and fit["inside"] and not fit["clipped"], (theme, width, fit)
    assert fit["overlaps"] == 0 and fit["color"] != fit["plain"], (theme, width, fit)
    root = os.environ.get("KESTREL_SHOTS")
    if root:
        folder = pathlib.Path(root) / "run-card-refusal"
        folder.mkdir(parents=True, exist_ok=True)
        page.locator("#st-mode").scroll_into_view_if_needed()
        page.screenshot(path=str(folder / f"after-{theme}-{width}.png"))
