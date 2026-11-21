"""Issue #181 in a real browser: Settings > This computer, the Ollama speed settings card.

Light and dark at 1280 and 390 wide: no overlapping or clipped text, no sideways scroll, in the
plain state, the confirm and after Set them; the confirm and the message fade in, and the OS
reduced-motion setting turns that off. A recorder stands in for the computer, so nothing on this
machine changes. Set KESTREL_SHOT_DIR to save the screenshots.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kestrel.services import ollama_env as oe
from tests.browser import launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("KESTREL_LIVE_BROWSER") != "1",
        reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
    ),
    pytest.mark.usefixtures("_isolate_data_home"),
]


class _Recorder(oe.Backend):
    def __init__(self) -> None:
        super().__init__(mechanism="browser-test",
                         where="your Windows user environment variables "
                         "(HKEY_CURRENT_USER\\Environment)",
                         restart_text="Quit Ollama from its icon in the taskbar notification "
                         "area, then start it again from the Start menu.",
                         persistence="This stays set after you restart Windows.",
                         undo_text="press Undo here, or open Edit environment variables for "
                         "your account and delete them")
        self.values: dict = {oe.PARALLEL: "1", oe.KEEP_ALIVE: "5m"}
        self.writes: list = []

    def owned(self):
        return dict(self.values)

    def write(self, values):
        self.writes.append(dict(values))
        self.values.update(values)

    def commands(self, values):
        return [f"setx {n} {v}" for n, v in values.items() if v is not None]


@pytest.fixture
def live(monkeypatch):
    from playwright.sync_api import sync_playwright

    from kestrel.db import migrate
    from kestrel.web.serve import running_server

    rec = _Recorder()
    monkeypatch.setattr(oe, "_override", [rec])
    monkeypatch.setattr("kestrel.llm.profiles.ollama_needed", lambda s: True)
    migrate.run_migrations()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, rec
        browser.close()


_TEXT_BOXES = """el => {
  const out = [];
  const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    if (!n.textContent.trim() || !n.parentElement.offsetParent) continue;
    if (n.parentElement.closest('details:not([open]) > :not(summary)')) continue;
    const r = document.createRange(); r.selectNodeContents(n);
    for (const x of r.getClientRects()) if (x.width > 0)
      out.push({t: n.textContent.trim().slice(0, 30), l: x.left, r: x.right,
                tp: x.top, bt: x.bottom});
  }
  return out;
}"""

_CLIPPED = """el => Array.from(el.querySelectorAll('*')).filter(e => e.offsetParent &&
  (e.scrollWidth > e.clientWidth + 1) && getComputedStyle(e).overflowX !== 'visible'
  && e.tagName !== 'PRE').map(e => e.className)"""


def _check(page, label: str) -> None:
    card = page.query_selector('[data-testid="ollama-env"]')
    assert card is not None, label
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), label
    box = card.bounding_box()
    boxes = page.evaluate(_TEXT_BOXES, card)
    for b in boxes:
        assert b["l"] >= box["x"] - 1 and b["r"] <= box["x"] + box["width"] + 1, (label, b)
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            overlap = (a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1 and a["tp"] < b["bt"] - 1
                       and b["tp"] < a["bt"] - 1)
            assert not overlap, (label, a, b)
    assert page.evaluate(_CLIPPED, card) == [], label


def _shot(page, name: str) -> None:
    folder = os.environ.get("KESTREL_SHOT_DIR")
    if not folder:
        return
    Path(folder).mkdir(parents=True, exist_ok=True)
    page.query_selector('[data-testid="ollama-env"]').screenshot(path=str(Path(folder) / name))


def _open(live, theme: str, width: int, reduced: bool = False):
    base, browser, _rec = live
    ctx = browser.new_context(viewport={"width": width, "height": 1000}, color_scheme=theme,
                              reduced_motion="reduce" if reduced else "no-preference")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/settings#computer")
    page.wait_for_selector('[data-testid="oe-review"]')
    page.query_selector('[data-testid="ollama-env"]').scroll_into_view_if_needed()
    wait_for_layout(page)
    return ctx, page, errors


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 390])
def test_the_card_its_confirm_and_the_result_fit_in_light_and_dark(live, theme, width):
    ctx, page, errors = _open(live, theme, width)
    rec = live[2]
    try:
        _check(page, "plain")
        _shot(page, f"ollama-env-{theme}-{width}-1-plain.png")
        page.click('[data-testid="oe-review"]')
        page.wait_for_selector('[data-testid="oe-confirm"]')
        wait_for_layout(page)
        assert rec.writes == [], "opening the confirm changed the system"
        _check(page, "confirm")
        _shot(page, f"ollama-env-{theme}-{width}-2-confirm.png")
        page.click('[data-testid="oe-apply"]')
        page.wait_for_selector('[data-testid="oe-undo"]')
        wait_for_layout(page)
        assert rec.writes == [{oe.PARALLEL: "4", oe.KEEP_ALIVE: "2h"}]
        _check(page, "after")
        _shot(page, f"ollama-env-{theme}-{width}-3-after.png")
        assert not errors, errors
    finally:
        ctx.close()


def test_cancel_closes_the_confirm_without_a_change(live):
    ctx, page, _errors = _open(live, "light", 1280)
    try:
        page.click('[data-testid="oe-review"]')
        page.wait_for_selector('[data-testid="oe-confirm"]')
        page.click('[data-testid="oe-cancel"]')
        page.wait_for_selector('[data-testid="oe-review"]')
        assert not page.query_selector('[data-testid="oe-confirm"]')
        assert live[2].writes == []
    finally:
        ctx.close()


_ANIM = "sel => getComputedStyle(document.querySelector(sel)).animationName"


@pytest.mark.parametrize("reduced", [False, True])
def test_the_confirm_fades_in_unless_reduced_motion(live, reduced):
    ctx, page, _errors = _open(live, "light", 1280, reduced=reduced)
    try:
        page.click('[data-testid="oe-review"]')
        page.wait_for_selector('[data-testid="oe-confirm"]')
        name = page.evaluate(_ANIM, '[data-testid="oe-confirm"]')
        assert name == ("none" if reduced else "oe-in")
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 390])
def test_the_models_speed_note_links_to_the_card_without_overlap(live, theme, width):
    base, browser, _rec = live
    ctx = browser.new_context(viewport={"width": width, "height": 1000}, color_scheme=theme)
    page = ctx.new_page()
    try:
        page.goto(f"{base}/models")
        page.locator("#mo-downloads > details > summary").click()
        link = page.get_by_test_id("mo-speed-env-link")
        link.scroll_into_view_if_needed()
        wait_for_layout(page)
        assert link.is_visible()
        frame = form_box = page.query_selector("#mo-speed").bounding_box()
        for b in page.evaluate(_TEXT_BOXES, page.query_selector("#mo-speed")):
            assert form_box["x"] - 1 <= b["l"] and b["r"] <= frame["x"] + frame["width"] + 1, b
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        form = page.query_selector("#mo-speed")
        boxes = page.evaluate(_TEXT_BOXES, form)
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                overlap = (a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1
                           and a["tp"] < b["bt"] - 1 and b["tp"] < a["bt"] - 1)
                assert not overlap, (a, b)
        folder = os.environ.get("KESTREL_SHOT_DIR")
        if folder:
            form.screenshot(path=str(Path(folder) / f"models-speed-note-{theme}-{width}.png"))
        link.locator("a").click()
        page.wait_for_selector('[data-testid="ollama-env"]')
        assert page.url.endswith("/settings#computer")
    finally:
        ctx.close()
