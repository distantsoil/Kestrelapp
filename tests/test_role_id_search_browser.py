"""The Roles list under an ID query in a real browser (issue 213): the labels sit on their row, in
light, dark and 390 wide, with no overlap and no sideways scroll. Set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import asyncio
import os

import pytest

from tests.browser import launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401
from tests.test_role_id_search import _add_consulting_role, _fresh, _seed

pytestmark = [
    pytest.mark.usefixtures("_isolate_data_home"),
    pytest.mark.skipif(
        os.environ.get("KESTREL_LIVE_BROWSER") != "1",
        reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
    ),
]

_TAGS = """() => Array.from(document.querySelectorAll('[data-testid="pl-row"] .pl-tag'))
  .filter(e => e.offsetParent !== null).map(e => { const b = e.getBoundingClientRect();
    return {t: e.textContent.trim(), l: b.left, r: b.right, tp: b.top, bt: b.bottom,
            w: document.documentElement.clientWidth}; })"""


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 1280), ("light", 390)])
@pytest.mark.parametrize("which", ["dup", "closed_co", "other_track"])
def test_id_labels_show_without_overlap(theme, width, which):
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    _fresh()
    _ids = asyncio.run(_seed())
    _ids["other_track"] = asyncio.run(_add_consulting_role())[0]
    out = os.environ.get("KESTREL_SHOTS")
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
        page = ctx.new_page()
        page.goto(f"{base}/opportunities?track=employment&q=%23{_ids[which]}")
        page.wait_for_selector('[data-testid="pl-row"]')
        wait_for_layout(page)
        tags = page.evaluate(_TAGS)
        word = {"dup": "main copy", "closed_co": "Hidden company",
                "other_track": "Consulting tab"}[which]
        assert any(word in t["t"] for t in tags), tags
        for t in tags:
            assert t["l"] >= -1 and t["r"] <= t["w"] + 1, t  # not clipped off the window
        for i, a in enumerate(tags):
            for b in tags[i + 1:]:
                overlap = (a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1
                           and a["tp"] < b["bt"] - 1 and b["tp"] < a["bt"] - 1)
                assert not overlap, (a, b)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        if out:
            page.screenshot(path=f"{out}/{which}-{theme}-{width}.png", full_page=False)
        browser.close()
