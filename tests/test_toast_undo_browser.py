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
