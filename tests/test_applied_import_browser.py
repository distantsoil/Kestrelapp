"""Import past applications, walked end to end in a real browser (issue distantsoil/kestrel#50):
choose a file, match columns, review, Add, and the visible result. A 200 is not enough: each step
must show the next step's card, and Add must show the "added" line and create the applications.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import os

import pytest

from tests.browser import launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("KESTREL_LIVE_BROWSER") != "1",
        reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
    ),
    pytest.mark.usefixtures("_isolate_data_home"),
]


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


def _page(base, browser):
    page = browser.new_context(viewport={"width": 1280, "height": 1000}).new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    return page, errors


def test_sample_file_walks_every_step_and_adds_the_applications(live):
    base, browser = live
    page, errors = _page(base, browser)
    page.goto(f"{base}/settings#data/st-imp")
    page.wait_for_selector('[data-testid="st-imp-sample"]')
    wait_for_layout(page)
    page.click('[data-testid="st-imp-sample"]')
    page.click('[data-testid="st-imp-next"]')
    page.click('[data-testid="st-imp-add"]')
    page.wait_for_selector('[data-testid="st-imp-done"]')
    assert "5 applications added" in page.inner_text('[data-testid="st-imp-done"]')
    assert not errors
    page.goto(f"{base}/pursuits")
    assert "Birchwood Freight" in page.content()


def test_add_pressed_while_the_review_card_draws_still_sends_the_ticked_rows(live):
    """The settings entry motion draws ticked boxes unticked for two frames after a swap. A press
    in that window must send what the page shows, not the drawn default (the flaky "0 added")."""
    base, browser = live
    page, errors = _page(base, browser)
    page.goto(f"{base}/settings#data/st-imp")
    page.wait_for_selector('[data-testid="st-imp-sample"]')
    wait_for_layout(page)
    page.click('[data-testid="st-imp-sample"]')
    page.wait_for_selector('[data-testid="st-imp-next"]')
    # Press Add in the same task as the swap, after the entry motion has unticked the boxes.
    page.evaluate(
        """() => document.addEventListener('htmx:load', (e) => {
             const add = e.target.querySelector && e.target.querySelector('#st-imp-add');
             if (!add) return;
             window.__drawnUnticked = !!e.target.querySelector('input.st-check')
               && !e.target.querySelector('input.st-check:checked');
             add.click();
           })"""
    )
    page.click('[data-testid="st-imp-next"]')
    page.wait_for_selector('[data-testid="st-imp-done"]')
    assert page.evaluate("window.__drawnUnticked") is True  # the press really hit the window
    assert "5 applications added" in page.inner_text('[data-testid="st-imp-done"]')
    assert not errors


def _to_review(page, base, on_load_js):
    page.goto(f"{base}/settings#data/st-imp")
    page.wait_for_selector('[data-testid="st-imp-sample"]')
    wait_for_layout(page)
    page.click('[data-testid="st-imp-sample"]')
    page.wait_for_selector('[data-testid="st-imp-next"]')
    page.evaluate(
        """(body) => document.addEventListener('htmx:load', (e) => {
             const add = e.target.querySelector && e.target.querySelector('#st-imp-add');
             if (!add) return;
             window.__unticked = e.target.querySelectorAll('input.st-check:checked').length === 0;
             new Function('add', body)(add);
           })""",
        on_load_js,
    )
    page.click('[data-testid="st-imp-next"]')


def test_enter_pressed_while_the_review_card_draws_still_sends_the_ticked_rows(live):
    base, browser = live
    page, errors = _page(base, browser)
    _to_review(
        page,
        base,
        "add.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));"
        "add.form.requestSubmit(add);",
    )
    page.wait_for_selector('[data-testid="st-imp-done"]')
    assert page.evaluate("window.__unticked") is True
    assert "5 applications added" in page.inner_text('[data-testid="st-imp-done"]')
    assert not errors


def test_with_no_press_the_drawn_boxes_come_back_ticked(live):
    base, browser = live
    page, errors = _page(base, browser)
    _to_review(page, base, "")
    page.wait_for_function("window.__unticked !== undefined")
    assert page.evaluate("window.__unticked") is True  # the motion did draw them unticked
    page.wait_for_function(
        "document.querySelectorAll('#st-imp input.st-check:checked').length === 5"
    )
    assert not errors


def test_reduced_motion_never_unticks_the_boxes(live):
    base, browser = live
    page = browser.new_context(
        viewport={"width": 1280, "height": 1000}, reduced_motion="reduce"
    ).new_page()
    _to_review(page, base, "")
    page.wait_for_function("window.__unticked !== undefined")
    assert page.evaluate("window.__unticked") is False


def test_a_large_tracker_chosen_from_applications_is_added(live, tmp_path):
    """The owner's path: Applications, Import past applications, choose a Markdown tracker, Next,
    Add. 600 rows with long notes go past Starlette's form defaults (1 MB a field, 1,000 fields),
    which answered 400 and left the card unchanged."""
    base, browser = live
    note = "Easy Apply | résumé v12, follow-up 10/08 with the recruiter; " * 30
    rows = ["| Company | Role | Date | Status | Notes |", "| --- | --- | --- | --- | --- |"]
    rows += [f"| Org {i} | Analyst {i} | 2026-09-{i % 28 + 1:02d} | Applied | {note} |"
             for i in range(600)]
    tracker = tmp_path / "Job applications data.md"
    tracker.write_text("\n".join(rows) + "\n", encoding="utf-8")
    page, errors = _page(base, browser)
    page.goto(f"{base}/settings")
    wait_for_layout(page)
    page.click('a[href="/pursuits"]')  # arrive in place, as a person does
    link = '[data-testid="applied-import-link"], [data-testid="applied-import-link-empty"]'
    page.wait_for_selector(link)
    wait_for_layout(page)
    page.click(link)
    page.wait_for_selector('[data-testid="st-imp-file"]', state="attached")
    wait_for_layout(page)
    page.set_input_files('[data-testid="st-imp-file"]', str(tracker))
    page.click('[data-testid="st-imp-next"]')
    page.click('[data-testid="st-imp-add"]', timeout=30000)
    page.wait_for_selector('[data-testid="st-imp-done"]', timeout=60000)
    assert "600 applications added" in page.inner_text('[data-testid="st-imp-done"]')
    assert not errors


def test_a_press_that_fails_on_the_server_says_so(live):
    """htmx swaps nothing on an error answer; the page must still say the press failed."""
    base, browser = live
    page, errors = _page(base, browser)
    page.route("**/settings/data/applied/review", lambda r: r.fulfill(status=500, body="x"))
    page.goto(f"{base}/settings#data/st-imp")
    page.wait_for_selector('[data-testid="st-imp-sample"]')
    wait_for_layout(page)
    page.click('[data-testid="st-imp-sample"]')
    page.click('[data-testid="st-imp-next"]')
    toast = page.wait_for_selector(".k-toast")
    assert "did not go through, so nothing changed" in toast.inner_text()
    assert page.locator(".k-toast").count() == 1
    assert page.get_attribute("#st-imp", "data-step") == "1"  # the review is still there
    assert not errors


def test_a_background_request_that_fails_stays_quiet(live):
    """Only a request a person started gets the message: a background load that fails does not."""
    base, browser = live
    page, errors = _page(base, browser)
    page.route("**/status/counts*", lambda r: r.fulfill(status=500, body="x"))
    page.goto(f"{base}/settings#data/st-imp")
    page.wait_for_selector('[data-testid="st-imp-sample"]')
    wait_for_layout(page)
    page.evaluate("htmx.ajax('GET', '/status/counts', {swap: 'none'})")
    page.wait_for_timeout(600)
    assert page.locator(".k-toast").count() == 0
    assert not errors
