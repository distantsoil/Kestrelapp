"""The role page's "I applied" panel in a real browser (#145): a busy state while it saves, a short
saved confirmation, and a failure that is shown in place without swapping the board anywhere.
The partial is loaded into a shell page, as the role page loads it.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests import apps_seed
from tests.browser import launch_sync, swap_result, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

# A slot the partial loads into, and the load; swap_result waits for this request's swap only.
LOAD_JS = """const slot = document.createElement('div');
  slot.id = 'slot'; slot.style.padding = '24px';
  document.querySelector('main, #main, body').appendChild(slot);
  htmx.ajax('GET', '/opportunities/' + arg + '/actions', {target: '#slot', swap: 'innerHTML'});"""


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    apps_seed.seed("employment")
    opp_ids = {t: _opp_id(t) for t in ("Product Operations Manager", "Senior Data Analyst")}
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, opp_ids
        browser.close()


def _opp_id(title: str) -> int:
    import asyncio

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel import paths
    from kestrel.db.engine import forget_engine, make_async_engine
    from kestrel.db.models import Opportunity

    async def go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine)() as s:
                return (await s.execute(
                    select(Opportunity.id).where(Opportunity.title == title))).scalar_one()
        finally:
            await engine.dispose()
            forget_engine()

    return asyncio.run(go())


def _shot(page, name: str) -> None:
    out = os.environ.get("KESTREL_SHOT_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(out) / f"{name}.png"))


def _open(live, title: str, theme: str = "light", width: int = 1280):
    base, browser, opp_ids = live
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/help")
    wait_for_layout(page)
    swap_result(page, f"/opportunities/{opp_ids[title]}/actions", LOAD_JS, "return true",
                opp_ids[title])
    page.wait_for_selector('[data-testid="mark-applied"]')
    wait_for_layout(page)
    return ctx, page, errors


def test_the_panel_has_no_inline_style_and_a_failure_stays_in_place(live):
    ctx, page, errors = _open(live, "Product Operations Manager")
    try:
        assert page.evaluate("document.querySelectorAll('#opp-actions [style]').length") == 0
        _shot(page, "applied-panel-idle")
        # Force a refusal from the server: a date it cannot read.
        page.evaluate("""document.addEventListener('htmx:configRequest', e => {
            if (e.detail.path.endsWith('/act/applied/quick')) e.detail.parameters.applied_on = 'x';
        })""")
        page.click('[data-testid="mark-applied"]')
        page.wait_for_function(
            "document.querySelector('#applied-error').textContent.trim() !== ''")
        wait_for_layout(page)
        text = page.inner_text("#applied-error")
        assert text.startswith("That was not recorded.") and "not a valid date" in text
        # The form is still there, enabled again, and no board was swapped in.
        assert page.locator('[data-testid="mark-applied"]').is_enabled()
        assert page.locator("#applied-status #apps-board, #applied-status .ap-board").count() == 0
        assert page.evaluate("document.querySelectorAll('#apps-board').length") == 0
        _shot(page, "applied-panel-error")
        assert errors == []
    finally:
        ctx.close()


def test_pressing_shows_busy_then_a_short_saved_confirmation(live):
    ctx, page, errors = _open(live, "Senior Data Analyst", theme="dark")
    try:
        # Hold the answer for a moment so the busy state can be seen.
        page.route("**/act/applied/quick", lambda route: (
            page.wait_for_timeout(500), route.continue_()))
        page.click('[data-testid="mark-applied"]')
        page.wait_for_function(
            "document.querySelector('[data-testid=mark-applied]')?.disabled === true")
        assert page.evaluate(
            "document.querySelector('[data-testid=applied-form]').classList.contains('htmx-request')")
        page.wait_for_selector('[data-testid="applied-done"]')
        done = page.locator('[data-testid="applied-done"]')
        assert "oa-saved" in (done.get_attribute("class") or "")
        assert "Applied" in done.inner_text()
        _shot(page, "applied-panel-saved")
        assert errors == []
    finally:
        ctx.close()
