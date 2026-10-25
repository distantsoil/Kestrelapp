"""Saved Region formats across real pages, light/dark at both required widths."""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

from tests import prep_seed
from tests.browser import launch_sync
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
    day = date.today() + timedelta(days=5)
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
    page.goto(base + "/")
    today = date.today()
    today_label = (f"{today.day:02d}/{today.month:02d}/{today.year}"
                   if chosen == "DD/MM/YYYY" else f"{today:%b} {today.day}, {today.year}")
    expect(page.locator(".td-crumb")).to_contain_text(today_label)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.close()
