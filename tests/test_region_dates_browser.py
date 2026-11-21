"""Saved Region formats across real pages, light/dark at both required widths."""

from __future__ import annotations

import os
import re
from datetime import date

import pytest

from tests import prep_seed
from tests.browser import launch_sync
from tests.browser_plugin import browser_artifacts  # noqa: F401
from tests.test_prep_browser import _check as check_prep

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    ids = prep_seed.seed(with_pack=True)
    with running_server(worker=False) as base, sync_playwright() as playwright:
        browser = launch_sync(playwright)
        yield base, browser, ids
        browser.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
@pytest.mark.parametrize("chosen", ["DD/MM/YYYY", "Mon D, YYYY"])
def test_saved_region_reaches_prep_applications_and_today(live, theme, width, chosen):
    from playwright.sync_api import expect

    base, browser, ids = live
    opportunity = ids["Larkspur Logistics"]["opp"]
    day = ids["Larkspur Logistics"]["day"]  # as seeded (D-TEST-582)
    expected = (f"{day.day:02d}/{day.month:02d}/{day.year}" if chosen == "DD/MM/YYYY"
                else f"{day:%b} {day.day}, {day.year}")
    page = browser.new_page(viewport={"width": width, "height": 900})
    page.add_init_script(f"localStorage.setItem('kestrel-theme','{theme}')")
    page.goto(base + "/settings#preferences")
    page.locator('[data-testid="st-date-format"]').select_option(chosen)
    preview = "23/10/2026" if chosen == "DD/MM/YYYY" else "Oct 23, 2026"
    expect(page.locator('[data-testid="st-fmt-preview"]')).to_contain_text(preview)
    page.locator('[data-testid="st-time-24h"]').click()
    expect(page.locator('[data-testid="st-fmt-preview"]')).to_contain_text("20:02")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.goto(f"{base}/prep/{opportunity}")
    expect(page.locator('[data-testid="pp-when"]')).to_contain_text(expected + " · 10:00")
    assert page.get_attribute("html", "data-theme") == theme
    check_prep(page, f"{chosen} {theme} {width}")
    page.goto(base + "/pursuits")
    expect(page.locator('[data-testid="rail-interviews"]')).to_contain_text(expected)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert page.evaluate("""() => {
      for (const row of document.querySelectorAll('.ap-ivr')) {
        const boxes = Array.from(row.children).map(el => el.getBoundingClientRect());
        for (let index = 1; index < boxes.length; index++) {
          if (boxes[index].left < boxes[index - 1].right - 1) return false;
        }
      }
      for (const button of document.querySelectorAll('.ap-ivr .k-btn')) {
        const range = document.createRange(); range.selectNodeContents(button);
        if (new Set(Array.from(range.getClientRects()).map(box => Math.round(box.top))).size > 1)
          return false;
      }
      return true;
    }""")
    # D-TEST-582: the server reads its own clock while it renders, so the day it shows lies
    # between this test's clock read just before the request and just after it; the two agree
    # except across midnight, and then either day is the right answer.
    before = date.today()
    page.goto(base + "/")
    after = date.today()

    def label(day: date) -> str:
        return (f"{day.day:02d}/{day.month:02d}/{day.year}"
                if chosen == "DD/MM/YYYY" else f"{day:%b} {day.day}, {day.year}")

    expect(page.locator(".td-crumb")).to_contain_text(
        re.compile("|".join(re.escape(label(d)) for d in sorted({before, after}))))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.close()


def test_time_toggle_survives_a_refresh_still_in_flight(live):
    """#229 merge: the date-format save reloads the Preferences pane. When the 24-hour press
    lands before that reload arrives, the reload (rendered before the press was saved) must not
    leave the old 12-hour preview on screen. The reload is held, deterministically, until the
    press has been saved and answered, then delivered as the server rendered it."""
    from playwright.sync_api import expect

    base, browser, _ids = live
    page = browser.new_page(viewport={"width": 1680, "height": 900})
    page.goto(base + "/settings#preferences")
    expect(page.locator('[data-testid="st-time-12h"]')).to_have_attribute("aria-pressed", "true")
    held: list = []

    def hold_first(route):
        if held:
            route.continue_()
        else:
            held.append(route)

    page.route("**/settings/section/preferences*", hold_first)
    with page.expect_request("**/settings/section/preferences*"):
        page.locator('[data-testid="st-date-format"]').select_option("DD/MM/YYYY")
    expect(page.locator('[data-testid="st-fmt-preview"]')).to_contain_text("23/10/2026")
    assert len(held) == 1, "the date-format save did not reload the pane"
    route = held[0]
    response = route.fetch()  # the server renders the reload now, before the press
    assert "8:02 PM" in response.text()
    with page.expect_response("**/settings/ui") as pressed:
        page.locator('[data-testid="st-time-24h"]').click()
    assert pressed.value.ok
    expect(page.locator('[data-testid="st-fmt-preview"]')).to_contain_text("20:02")
    route.fulfill(response=response)  # the older reload arrives after the press was answered
    expect(page.locator('[data-testid="st-fmt-preview"]')).to_contain_text("20:02")
    expect(page.locator('[data-testid="st-time-24h"]')).to_have_attribute("aria-pressed", "true")
    page.close()
