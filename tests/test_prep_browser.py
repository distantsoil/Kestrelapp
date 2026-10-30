"""Interview prep in a real browser (UI overhaul parity check against the locked mockup,
the locked design mockup (kept privately)). Light and dark at 1280 and 1680: no overlap, no
wrapped titles or buttons, no horizontal scroll, on every tab and in the empty state; then the
story sheet, the swap menu and the PDF preview.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1. Set KESTREL_PREP_SHOTS to a
directory to also save a screenshot of every state."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests import prep_seed
from tests.browser import (
    LINES_JS,
    TIMER_PROBE_JS,
    assert_page_remounts_clean,
    launch_sync,
    wait_for_layout,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

_LINES = LINES_JS
_BOXES = """sel => Array.from(document.querySelectorAll(sel)).filter(e => e.offsetParent !== null)
  .map(e => { const b = e.getBoundingClientRect();
    return {t: (e.textContent || '').trim().slice(0, 30), l: b.left, r: b.right, tp: b.top,
            bt: b.bottom}; })"""
_BG = ("() => getComputedStyle(document.body).backgroundColor.match(/[\\d.]+/g)"
       ".slice(0, 3).map(Number)")
_SHOTS = os.environ.get("KESTREL_PREP_SHOTS")


def _shot(page, name: str) -> None:
    if _SHOTS:
        Path(_SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(_SHOTS) / f"{name}.png"), full_page=True)


def _overlaps(a, b) -> bool:
    return a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1 and a["tp"] < b["bt"] - 1 \
        and b["tp"] < a["bt"] - 1


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    ids = prep_seed.seed(with_pack=True)
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, ids
        browser.close()


def _check(page, label: str) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), label
    for sel in (".pp-page h1", ".pp-tabacts .k-btn", ".pp-q__btns .k-btn",
                ".pp-th b"):
        for el in page.query_selector_all(sel):
            if el.is_visible() and sel != ".pp-th b":
                assert page.evaluate(_LINES, el) == 1, (label, sel, el.inner_text()[:40])
    for sel in (".pp-tab, .pp-tabacts .k-btn", ".pp-ready .pp-tile", ".pp-q__btns .k-btn"):
        boxes = page.evaluate(_BOXES, sel)
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                assert not _overlaps(a, b), (label, sel, a["t"], b["t"])


def _check_layout_1280(page, label: str) -> None:
    """The locked mockup's @container rules at 1280: the main column and the side cards stay
    side by side (it stacks only at 1000px of content or less), and the four readiness tiles sit
    in one row on the pack tab."""
    grid = page.query_selector(".pp-grid, .pp-pgrid")
    if grid is not None and grid.is_visible():
        kids = [b for b in page.evaluate(
            """el => Array.from(el.children).filter(c => c.offsetParent !== null)
                 .map(c => { const b = c.getBoundingClientRect(); return {l: b.left, r: b.right,
                   tp: b.top}; })""", grid)]
        assert len(kids) >= 2, label
        assert kids[1]["l"] >= kids[0]["r"] - 1 and abs(kids[1]["tp"] - kids[0]["tp"]) < 40, \
            (label, "main column and side cards stack at 1280", kids)
    tiles = page.evaluate(_BOXES, ".pp-ready .pp-tile")
    if tiles:
        assert len(tiles) == 4 and len({round(t["tp"]) for t in tiles}) == 1, \
            (label, "the four tiles are not in one row", tiles)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_every_tab_fits_in_both_themes_and_widths(live, theme, width):
    base, browser, ids = live
    opp = ids["Larkspur Logistics"]["opp"]
    page = browser.new_page(viewport={"width": width, "height": 900})
    page.add_init_script(f"try{{localStorage.setItem('kestrel-theme','{theme}')}}catch(e){{}}")
    for tab in ("pack", "practice", "bank"):
        page.goto(f"{base}/prep/{opp}?tab={tab}")
        page.wait_for_selector("#pp-layout")
        # The theme really applied: the page background is light or dark, so a wrong storage
        # key can never turn the dark runs into light ones again.
        assert page.evaluate("document.documentElement.getAttribute('data-theme')") == theme
        r, g, b = page.evaluate(_BG)
        assert ((r + g + b) / 3 > 128) == (theme == "light"), (theme, (r, g, b))
        if width == 1280:
            _check_layout_1280(page, f"{tab} {theme}")
        _check(page, f"{tab} {theme} {width}")
        _shot(page, f"{tab}-{theme}-{width}")
    page.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_keep_separate_and_undo_restore_the_proposal_in_place(live, theme, width):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel import paths
    from kestrel.db.engine import make_async_engine
    from kestrel.db.models import Story

    async def proposal():
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine)() as session:
            session.add(Story(title="Undo this proposal", is_canonical=True))
            await session.commit()
        await engine.dispose()

    base, browser, _ = live
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, proposal()).result()
    page = browser.new_page(viewport={"width": width, "height": 900})
    page.add_init_script(f"localStorage.setItem('kestrel-theme','{theme}')")
    page.goto(base + "/stories")
    page.wait_for_selector('[data-testid="pp-proposal"]')
    _check(page, f"before {theme} {width}")
    page.click('[data-testid="dismiss-story"]')
    page.wait_for_selector('[data-testid="pp-proposal"]', state="hidden")
    page.click(".k-toast__undo")
    page.wait_for_selector('[data-testid="pp-proposal"]')
    assert "Undo this proposal" in page.inner_text('[data-testid="pp-proposal"]')
    assert "tab=bank" not in page.url
    _check(page, f"restored {theme} {width}")
    page.close()


def test_the_sheet_the_swap_menu_and_the_preview_open_centered(live):
    base, browser, ids = live
    opp = ids["Larkspur Logistics"]["opp"]
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{base}/prep/{opp}")
    page.wait_for_selector("#pp-layout")
    page.click("[data-pp-open]")  # a story opens to its words
    assert page.is_visible(".pp-star")
    page.click('[data-testid="pp-swap"]')
    assert page.is_visible(".pp-menuwrap .pp-menu")
    page.keyboard.press("Escape")
    page.click('[data-testid="pp-have-story"]')
    page.wait_for_selector("#pp-story-sheet.is-open")
    # Measured once its opening motion is over. wait_for_layout flushes style first, so a
    # transition that has not started yet (a loaded machine) is not mistaken for a finished one.
    wait_for_layout(page)
    box = page.eval_on_selector("#pp-story-sheet", "e => e.getBoundingClientRect().toJSON()")
    assert abs((box["top"] + box["bottom"]) / 2 - 450) < 12  # vertically centered
    _shot(page, "sheet")
    page.fill('textarea[name="situation"]', "A short situation.")
    page.click('[data-testid="pp-sheet-save"]')
    page.wait_for_selector("#pp-story-sheet", state="hidden", timeout=5000)
    assert "4/5" in page.inner_text("#pp-ready")
    page.click('[data-testid="pp-pdf"]')
    page.wait_for_selector("#pp-preview.is-open")
    wait_for_layout(page)
    widths = page.eval_on_selector_all(
        ".pp-sheet-wrap", "els => els.map(e => Math.round(e.getBoundingClientRect().width))")
    assert widths and all(w < 816 for w in widths)  # scaled to fit, never reflowed
    _shot(page, "preview")
    page.close()


def test_the_practice_tab_keeps_its_place_when_a_mode_is_picked(live):
    base, browser, ids = live
    opp = ids["Larkspur Logistics"]["opp"]
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{base}/prep/{opp}?tab=practice")
    page.click('[data-testid="pp-mode-technical_leadership"]')
    assert page.input_value("#pp-mode") == "technical_leadership"
    page.close()


def test_a_sheet_opened_during_the_page_arrival_opens_in_the_window(live):
    """2026-10-29: a section still playing its staggered arrival (a transform) became the
    containing block of the fixed sheet inside it, which opened below the window. Opening an
    overlay now finishes those arrivals first."""
    base, browser, ids = live
    opp = ids["Larkspur Logistics"]["opp"]
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{base}/prep/{opp}")
    page.wait_for_selector("#pp-layout")
    page.click('[data-testid="pp-have-story"]')  # straight away, during the arrival
    page.wait_for_selector("#pp-story-sheet.is-open")
    top = page.eval_on_selector("#pp-story-sheet", "e => e.getBoundingClientRect().top")
    assert 0 <= top < 900, top
    page.close()


def test_the_page_mounts_unmounts_and_mounts_again_with_one_set_of_everything(live):
    """The page lifecycle (D-UI-210): Prep unmounted and mounted again in place starts exactly
    what it started on the document load and leaves no timer running in between. After it, one
    click on a story chip opens the story and leaves it open (a doubled listener opens and shuts
    it in the same click), a sheet's scale timers run and stop with the page, and an unmounted
    page no longer answers the chip."""
    base, browser, ids = live
    opp = ids["Larkspur Logistics"]["opp"]
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="light")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(TIMER_PROBE_JS)
    try:
        page.goto(f"{base}/prep/{opp}")
        page.wait_for_selector("#pp-layout")
        wait_for_layout(page)
        first = assert_page_remounts_clean(page, "prep", "prep.js", settle_ms=600)
        assert first["timers"] == 0 and first["observers"] == 0, first
        page.click("[data-pp-open]")
        assert page.is_visible(".pp-star")
        page.click("[data-pp-open]")
        assert page.is_hidden(".pp-star")
        page.click('[data-testid="pp-swap"]')
        assert page.is_visible(".pp-menuwrap .pp-menu")
        page.keyboard.press("Escape")
        assert page.is_hidden(".pp-menuwrap .pp-menu")
        # A sheet opens through the swap handler, whose scale timers belong to the page.
        page.click('[data-testid="pp-have-story"]')
        page.wait_for_selector("#pp-story-sheet.is-open")
        page.keyboard.press("Escape")
        page.wait_for_selector("#pp-story-sheet", state="hidden")
        page.wait_for_timeout(600)
        assert page.evaluate("KUI.page._stats().timers") == 0
        page.evaluate("KUI.page.unmount()")
        page.click("[data-pp-open]")
        assert page.is_hidden(".pp-star")
        assert errors == []
    finally:
        ctx.close()
