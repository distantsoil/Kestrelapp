"""D-IMPORT-7: a document import always shows it is working and always ends with a message.

Both ways of sending the proposal (the processing form that starts on load, and the online
consent dialog's "Send and import") show one progress panel at once, and every failure of the
request (an error answer, an empty one, no answer, a hang) ends the spinner and writes a plain
message with Try again. The proposal request is intercepted in the browser; nothing leaves the
machine. Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import os

import pytest

from tests import profile_seed
from tests.browser import launch_sync, wait_for_layout

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

_PROPOSE = "**/api/documents/import"
_READ = "**/api/documents/import/read"
_TEXT = "Pat Example\n## Experience\nAcme | Town, ST  Jan 2020 - Dec 2022\n- Ran the desk.\n"


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    master_id = profile_seed.seed()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, master_id
        browser.close()


def _online_read_page() -> str:
    """The read step's answer on an online model: the real consent partial, as the server renders
    it when the route is online."""
    from kestrel.web.deps import templates

    html = templates().get_template("partials/doc_online_consent.html").render(
        dialog_id="doc-import-online", post_url="/api/documents/import", target="#import-proposal",
        action="Send and import",
        route={"provider": "Example", "model": "m", "dest": "d", "sends": "Your resume",
               "this_send": "every bullet", "employer": "hidden"},
        extra={"text": _TEXT, "track": "employment", "doc_kind": "resume",
               "extracted_shown": "1"},
    )
    return '<div class="import-result">' + html + "</div>"


def _page(live, path: str, *, timeout_ms: int | None = None, clock: bool = False):
    base, browser, master_id = live
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="light")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    if timeout_ms:
        page.add_init_script(f"window.__kImportTimeoutMs = {timeout_ms};")
    if clock:
        page.clock.install()
    page.goto(f"{base}/documents?master_id={master_id}")
    page.wait_for_selector("#import-form")
    wait_for_layout(page)
    return ctx, page, errors


def _start(page, path: str) -> None:
    """Press Propose import; on the online path the read step answers with the consent dialog,
    which is then confirmed."""
    if path == "online":
        page.route(_READ, lambda r: r.fulfill(status=200, content_type="text/html",
                                              body=_online_read_page()))
    page.fill("#import-form textarea[name=text]", _TEXT)
    page.click("#import-form button[type=submit]")
    if path == "online":
        page.wait_for_selector('[data-testid="doc-online-send"]', state="visible")
        page.click('[data-testid="doc-online-send"]')


def _answer(kind: str):
    def handler(route):
        if kind == "500":
            route.fulfill(status=500, body="boom")
        elif kind == "403":
            route.fulfill(status=403, content_type="application/json",
                          body='{"detail": "cross-origin request refused"}')
        elif kind == "400":
            route.fulfill(status=400, body="Field exceeded maximum size")
        elif kind == "204":
            route.fulfill(status=204, body="")
        elif kind == "abort":
            route.abort("connectionreset")
    return handler


@pytest.mark.parametrize("path", ["local", "online"])
@pytest.mark.parametrize("kind", ["500", "403", "400", "204", "abort"])
def test_a_failed_proposal_ends_the_spinner_and_says_so_with_try_again(live, path, kind):
    ctx, page, errors = _page(live, path)
    try:
        page.route(_PROPOSE, _answer(kind))
        _start(page, path)
        page.wait_for_selector('[data-testid="import-error"]', timeout=5000)
        assert page.is_visible('[data-testid="import-error"]')
        assert "Nothing was saved" in page.inner_text('[data-testid="import-error"]')
        assert page.is_visible('[data-testid="import-retry"]')
        assert page.query_selector('[data-testid="import-working"]') is None  # the spinner ended
        # The generic toast does not say it a second time.
        assert page.query_selector(".k-toast") is None
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("path", ["local", "online"])
def test_try_again_sends_the_same_import_once_more(live, path):
    ctx, page, errors = _page(live, path)
    try:
        calls: list[int] = []

        def handler(route):
            calls.append(1)
            if len(calls) == 1:
                route.fulfill(status=500, body="boom")
            else:
                route.fulfill(status=200, content_type="text/html",
                              body='<div data-testid="import-second">second answer</div>')

        page.route(_PROPOSE, handler)
        _start(page, path)
        page.wait_for_selector('[data-testid="import-retry"]', timeout=5000)
        page.click('[data-testid="import-retry"]')
        if path == "online":  # D-SEC-7: starting over shows the consent dialog; Send confirms
            page.wait_for_selector("#doc-import-online.is-open", state="visible", timeout=5000)
            assert len(calls) == 1
            page.click('[data-testid="doc-online-send"]')
        page.wait_for_selector('[data-testid="import-second"]', timeout=5000)
        assert len(calls) == 2 and page.query_selector('[data-testid="import-error"]') is None
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("path", ["local", "online"])
def test_a_hang_shows_the_long_wait_line_then_ends_with_a_message(live, path):
    ctx, page, errors = _page(live, path, timeout_ms=7000, clock=True)
    try:
        page.route(_PROPOSE, lambda route: None)  # never answered
        _start(page, path)
        page.wait_for_selector('[data-testid="import-working"]', timeout=5000)
        assert not page.is_visible('[data-testid="import-long"]')
        page.clock.run_for(61_000)  # a minute of the page's own time
        assert page.is_visible('[data-testid="import-long"]')
        assert "taking longer" in page.inner_text('[data-testid="import-long"]')
        assert page.inner_text('[data-testid="import-elapsed"]').strip() in {"61 s", "60 s"}
        page.wait_for_selector('[data-testid="import-error"]', timeout=15000)  # the client gave up
        assert "too long" in page.inner_text('[data-testid="import-error"]')
        assert page.query_selector('[data-testid="import-working"]') is None
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("path", ["local", "online"])
def test_a_second_send_while_one_runs_is_ignored(live, path):
    ctx, page, errors = _page(live, path)
    try:
        held: list = []
        page.route(_PROPOSE, lambda route: held.append(route))
        _start(page, path)
        page.wait_for_selector('[data-testid="import-working"]', timeout=5000)
        # A second send, however it is made, never reaches the server.
        if path == "online":
            page.evaluate("document.querySelector('[data-testid=\"doc-online-send\"]').click()")
        else:
            page.evaluate("""() => { const f = document.querySelector('form.import-processing');
              htmx.ajax('POST', '/api/documents/import',
                        {source: f, target: '.import-result'}); }""")
        page.wait_for_timeout(600)
        assert len(held) == 1
        assert len(page.query_selector_all('[data-testid="import-working"]')) == 1
        held[0].fulfill(status=500, body="boom")
        page.wait_for_selector('[data-testid="import-error"]', timeout=5000)
        assert not errors, errors
    finally:
        ctx.close()


def test_the_online_path_shows_the_processing_panel_within_300_ms_of_send(live):
    ctx, page, errors = _page(live, "online")
    try:
        page.route(_PROPOSE, lambda route: None)
        _start_online_without_click(page)
        # Timed inside the page, from the press to the panel showing, so Playwright's own round
        # trips on a busy machine do not count against the 300 ms the person would see.
        page.evaluate("""() => {
          window.__kt = {};
          document.addEventListener('click', (e) => {
            const send = e.target.closest('[data-testid="doc-online-send"]');
            if (send) window.__kt.press = performance.now();
          }, true);
          const seen = () => {
            const el = document.querySelector('[data-testid="import-working"]');
            if (el && !window.__kt.shown && el.getClientRects().length) {
              window.__kt.shown = performance.now();
            }
          };
          const opts = {subtree: true, childList: true, attributes: true};
          new MutationObserver(seen).observe(document.body, opts);
        }""")
        page.click('[data-testid="doc-online-send"]')
        page.wait_for_selector('[data-testid="import-working"]', state="visible", timeout=5000)
        took = page.evaluate("() => window.__kt.shown - window.__kt.press")
        assert took < 300, f"the processing panel took {took:.0f} ms after the press"
        assert not errors, errors
    finally:
        ctx.close()


def test_try_again_on_the_online_path_asks_for_consent_again_and_never_posts_alone(live):
    """D-SEC-7: a fast failure closes the dialog (its own request ended), and Try again starts
    over from Propose import, so the consent dialog shows again. Nothing posts a confirmed send
    until the person presses Send, and a dialog opened after the failure is not closed by anything.
    """
    ctx, page, errors = _page(live, "online")
    try:
        posts: list[str] = []

        def handler(route):
            posts.append(route.request.post_data or "")
            route.fulfill(status=403, content_type="application/json",
                          body='{"detail": "cross-origin request refused"}')

        page.route(_PROPOSE, handler)
        _start(page, "online")
        page.wait_for_selector('[data-testid="import-error"]', timeout=5000)
        page.wait_for_selector("#doc-import-online.is-open", state="detached", timeout=3000)
        assert len(posts) == 1 and "confirmed=1" in posts[0]
        page.click('[data-testid="import-retry"]')
        page.wait_for_selector("#doc-import-online.is-open", state="visible", timeout=5000)
        page.wait_for_timeout(1000)  # no timer, request or script may close the second dialog
        assert page.is_visible("#doc-import-online.is-open")
        assert len(posts) == 1, "Try again posted without the consent dialog being confirmed"
        page.click('[data-testid="doc-online-send"]')
        page.wait_for_selector('[data-testid="import-error"]', timeout=5000)
        assert len(posts) == 2
        assert not errors, errors
    finally:
        ctx.close()


def _start_online_without_click(page) -> None:
    page.route(_READ, lambda r: r.fulfill(status=200, content_type="text/html",
                                          body=_online_read_page()))
    page.fill("#import-form textarea[name=text]", _TEXT)
    page.click("#import-form button[type=submit]")
    page.wait_for_selector('[data-testid="doc-online-send"]', state="visible")
