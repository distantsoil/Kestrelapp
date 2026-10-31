"""Background research in place, in both themes at the two supported widths."""

from __future__ import annotations

import os

import pytest

from tests import test_role_browser as role_browser

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)


@pytest.fixture
def live(monkeypatch):
    yield from role_browser.live.__wrapped__(monkeypatch)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_queue_progress_cancel_and_reload_fit(live, theme, width):
    ctx, page, errors = role_browser._page(live, theme, width, "/organization")
    try:
        page.get_by_test_id("org-research-btn").click()
        page.get_by_test_id("research-jobs").wait_for()
        page.get_by_test_id("org-contacts-btn").click()
        page.wait_for_function(
            "document.querySelectorAll('[data-testid=research-job]').length === 2"
        )
        page.reload()
        page.get_by_test_id("research-jobs").wait_for()
        assert page.get_by_test_id("research-job").count() == 2
        assert page.get_by_test_id("research-cancel").count() == 2
        assert "resumes after a restart" in page.get_by_test_id("research-jobs").inner_text()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        for selector in (".rl-research-job h4", ".rl-research-job button"):
            for element in page.locator(selector).all():
                assert element.evaluate(role_browser._LINES) == 1
        for element in page.locator(".rl-research-job").all():
            boxes = element.evaluate("""el => Array.from(el.children).map(child => {
                const box = child.getBoundingClientRect();
                return {l: box.left, r: box.right, tp: box.top, bt: box.bottom};
            })""")
            assert not role_browser._overlaps(*boxes)
        page.get_by_test_id("research-cancel").first.click()
        page.wait_for_function(
            "document.querySelectorAll('[data-testid=research-cancel]').length === 1"
        )
        assert "Canceled." in page.get_by_test_id("research-jobs").inner_text()
        page.get_by_test_id("research-cancel").click()
        page.wait_for_function(
            "document.querySelectorAll('[data-testid=research-cancel]').length === 0"
        )
        assert page.locator("#rl-orgtab").get_attribute("hx-get") is None
        assert "Pat Lee" in page.get_by_test_id("contacts").inner_text()
        assert not errors, errors
    finally:
        ctx.close()
