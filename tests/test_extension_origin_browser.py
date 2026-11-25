"""The Role page header for a role captured with the browser extension, in a real browser: the
facts row names the origin (extension plus site), nothing overlaps or wraps, and nothing scrolls
sideways, in light, dark and a narrow window. Screenshots go to KESTREL_SHOTS when it is set.
All text is invented; no test touches the network.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests import role_seed
from tests.browser import art_visible_left, launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

SHOTS = os.environ.get("KESTREL_SHOTS", "")
POSTING = "Plan the weekly delivery schedule and keep the team informed. " * 12

_KIDS = """el => Array.from(el.children).filter(e => e.offsetParent !== null).map(e => {
  const b = e.getBoundingClientRect();
  return {t: (e.textContent || '').trim().slice(0, 40), l: b.left, r: b.right, tp: b.top,
          bt: b.bottom, h: b.height}; })"""


async def _capture_one() -> int:
    from kestrel import paths
    from kestrel.db.engine import make_async_engine
    from kestrel.ingest.manual import manual_ingest

    engine = make_async_engine(paths.db_path())
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            opp = await manual_ingest(
                s, url="https://jobs.example.test/post/42", text="Dispatch Planner\n" + POSTING,
                title="Dispatch Planner", captured_via="extension")
            return opp.id
    finally:
        await engine.dispose()


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    asyncio.run(role_seed.seed_async())
    opp_id = asyncio.run(_capture_one())
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, opp_id
        browser.close()


@pytest.mark.parametrize(("theme", "width"), [("light", 1280), ("dark", 1280), ("light", 420)])
def test_the_header_names_the_extension_origin_without_overlap(live, theme, width):
    base, browser, opp_id = live
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    try:
        page.goto(f"{base}/opportunities/{opp_id}")
        page.wait_for_selector("#rl-body")
        wait_for_layout(page)
        origin = page.locator('[data-testid="facts"] [data-kind="source"]')
        assert origin.inner_text().strip() == (
            "From the browser extension (jobs.example.test)")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        kids = page.evaluate(_KIDS, page.query_selector('[data-testid="facts"]'))
        for i, a in enumerate(kids):
            for b in kids[i + 1:]:
                overlap = (a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1
                           and a["tp"] < b["bt"] - 1 and b["tp"] < a["bt"] - 1)
                assert not overlap, (a, b, theme, width)
        if SHOTS:
            folder = Path(SHOTS)
            folder.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(folder / f"role-origin-{theme}-{width}.png"))
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_facts_row_stops_before_the_header_art(live, theme):
    base, browser, opp_id = live
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    try:
        page.goto(f"{base}/opportunities/{opp_id}")
        page.wait_for_selector("#rl-body")
        wait_for_layout(page)
        # art_visible_left measures where the art is really drawn (D-TEST-796).
        art_left = art_visible_left(page)
        facts_right = page.evaluate(
            "(() => { const k = [...document.querySelector('[data-testid=facts]').children];"
            " return Math.max(...k.map(e => { const r = document.createRange();"
            " r.selectNodeContents(e); return r.getBoundingClientRect().right; })); })()")
        assert art_left is None or facts_right <= art_left, (facts_right, art_left)
    finally:
        ctx.close()
