"""Settings > Play in a real browser (D-UI-95, D-REL-7): the button asks the app to play Kestrel's
own sound, with the sound and volume as the controls stand, and the window makes no sound of its
own (no Web Audio). The sound itself is the app's (kestrel/services/alert_sound.py, tested in
tests/test_alert_sound.py); this is only the page's half.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1. The browser is the Edge
already on the machine, else a Playwright Chromium (tests/browser.py)."""

from __future__ import annotations

import json
import os

import pytest

from tests.browser import launch_sync

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("KESTREL_LIVE_BROWSER") != "1",
        reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
    ),
    pytest.mark.usefixtures("_isolate_data_home"),
]

# Any use of Web Audio in the window is counted: the window must make no sound.
WATCH = """
(() => {
  window.__webAudio = 0;
  for (const name of ['AudioContext', 'webkitAudioContext', 'Audio']) {
    const Real = window[name];
    if (!Real) continue;
    window[name] = function (...args) { window.__webAudio += 1; return new Real(...args); };
  }
})();
"""


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


def _open(live, answer: dict):
    """Settings > Preferences with the preview endpoint answered by the test (nothing plays)."""
    base, browser = live
    ctx = browser.new_context(viewport={"width": 1280, "height": 1000})
    ctx.add_init_script(WATCH)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    asked: list[dict] = []

    def preview(route):
        asked.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(status=200, content_type="application/json", body=json.dumps(answer))

    page.route("**/settings/sound/preview", preview)
    page.goto(f"{base}/settings#preferences")
    page.wait_for_selector("#st-play", state="visible")
    return ctx, page, asked, errors


def test_play_asks_the_app_for_the_sound_and_volume_on_the_page(live):
    ctx, page, asked, errors = _open(live, {"ok": True, "message": "", "played": True})
    try:
        page.select_option('[data-testid="st-sound-name"]', "bell")
        page.fill("#st-vol", "40")
        page.click("#st-play")
        page.wait_for_function("document.querySelector('#st-play').disabled === true")
        assert asked == [{"name": "bell", "volume": 0.4}]
        assert page.evaluate("window.__webAudio") == 0  # the window makes no sound itself
        page.wait_for_function("document.querySelector('#st-play').disabled === false")
        assert not errors, errors
    finally:
        ctx.close()


def test_a_computer_that_cannot_play_says_so_in_a_message(live):
    message = "Kestrel could not play a sound on this computer."
    ctx, page, asked, errors = _open(live, {"ok": False, "message": message})
    try:
        page.click("#st-play")
        page.wait_for_selector(f".k-toast:has-text('{message}')")
        assert len(asked) == 1 and asked[0]["name"] == "chime"
        assert page.evaluate("window.__webAudio") == 0
        assert not errors, errors
    finally:
        ctx.close()
