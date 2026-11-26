"""The capture notice in a real browser (issue #267): Kestrel is open on a page, the extension
captures a role (the same keyed request it sends), and the open page shows "Role received and
being scored" as a toast within one status poll, linking to that role. Light and dark, 1280 and
420 wide. Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import os
import pathlib
import tempfile

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

_SHOTS = pathlib.Path(os.environ.get("KESTREL_SHOTS_DIR") or tempfile.mkdtemp())
KEY = "test-capture-notice-key-0123456789"
NOTICE = "Role received and being scored: Depot Scheduler"
TEXT = "Depot Scheduler\n" + " ".join(
    ["Plan the weekly depot schedule and keep the drivers informed."] * 14)


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.web.serve import running_server

    paths.ensure_dirs()
    (paths.config_dir() / "settings.yml").write_text(
        (paths.packaged_config() / "settings.yml").read_text(encoding="utf-8"), encoding="utf-8")
    migrate.run_migrations()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 420])
def test_a_capture_shows_a_toast_that_opens_the_role(live, theme, width):
    base, browser = live
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"{base}/about")
        page.wait_for_selector("h1")
        wait_for_layout(page)
        # The extension's key, made the way Settings > Browser extension makes it.
        key = page.request.post(f"{base}/settings/extension/key").json()["key"]
        url = f"https://jobs.example.test/depot-{theme}-{width}"
        r = page.request.post(f"{base}/api/opportunities/ingest",
                              headers={"X-Kestrel-Key": key},
                              data={"url": url, "text": TEXT, "title": "Depot Scheduler",
                                    "score_now": True})
        assert r.ok, r.status
        opp_id = r.json()["id"]
        link = page.locator(".k-toast a", has_text=NOTICE)
        link.wait_for()  # the next status poll (every 10 s) carries it
        assert link.get_attribute("href") == f"/opportunities/{opp_id}"
        box, view = link.bounding_box(), page.viewport_size
        assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= view["width"]
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(_SHOTS / f"capture-notice-{theme}-{width}.png"))
        link.click()
        page.wait_for_url(f"{base}/opportunities/{opp_id}*")
        assert not errors, errors
    finally:
        ctx.close()
