"""The shared toast Undo in a real browser (D-UI-160, R5): a failed request says nothing changed
and does not announce an undo; a refused Undo (the marker in the answer) shows its message.
Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import os

import pytest

from tests.browser import launch_sync

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
