"""Profile in a real browser (UI overhaul parity check against the locked mockup,
the locked design mockup (kept privately)). Light and dark at 1280 and 1680 on every
tab: no overlap, no wrapping titles or buttons, no horizontal scroll; then the interactions the
mockup demos (the Save bar and its tab-switch nudge, the near-floor margin stops, and the
document preview that matches Enlarge line for line).

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1. The browser is the Edge
already on the machine, else a Playwright Chromium (tests/browser.py). Set KESTREL_SHOT_DIR to
also write a screenshot of each state."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests import profile_seed
from tests.browser import LINES_JS, launch_sync, wait_for_layout

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

TABS = ("about", "documents", "employment", "consulting", "location", "stories")

# One line of text: every client rect of the element's text sits at the same height.
_LINES = LINES_JS

_BOXES = """sel => Array.from(document.querySelectorAll(sel)).filter(e => e.offsetParent !== null)
  .map(e => { const b = e.getBoundingClientRect();
    return {t: (e.textContent || '').trim().slice(0, 30), l: b.left, r: b.right, tp: b.top,
            bt: b.bottom}; })"""


def _overlaps(a, b) -> bool:
    return a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1 and a["tp"] < b["bt"] - 1 \
        and b["tp"] < a["bt"] - 1


@pytest.fixture
def live(monkeypatch):
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    master_id = profile_seed.seed()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, master_id
        browser.close()


def _url(base: str, tab: str, master_id: int) -> str:
    if tab == "documents":
        return f"{base}/documents?master_id={master_id}"
    track = tab if tab in ("employment", "consulting") else "employment"
    return f"{base}/profile?tab={tab}&track={track}"


def _open(live, tab: str, theme: str, width: int):
    base, browser, master_id = live
    ctx = browser.new_context(viewport={"width": width, "height": 1300}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(_url(base, tab, master_id))
    page.wait_for_selector(".pf-tabs")
    if tab == "documents":
        page.wait_for_selector("#preview [data-pf-page] iframe")
    wait_for_layout(page)
    return ctx, page, errors


def _shot(page, name: str) -> None:
    out = os.environ.get("KESTREL_SHOT_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(out) / f"{name}.png"))


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
@pytest.mark.parametrize("tab", TABS)
def test_no_overlap_no_wrapping_no_sideways_scroll(live, tab, theme, width):
    ctx, page, errors = _open(live, tab, theme, width)
    try:
        _shot(page, f"profile-{tab}-{theme}-{width}")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 0") is True
        assert page.evaluate("document.querySelector('.main, main').scrollWidth <= "
                             "document.querySelector('.main, main').clientWidth + 1") is True
        for sel in ("h1", ".pf-tabs a", ".k-pill", ".k-chip", ".pf-wrap .k-btn", ".pf-doccard b",
                    ".pf-doccard small", ".pf-margin .k-seg button", ".pf-track a"):
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    assert page.evaluate(_LINES, el) <= 1, (
                        sel, tab, theme, width, el.inner_text()[:40])
        # Cards in a row never overlap, and the help column never sits over the main column.
        for container in (".pf-cols", ".pf-dgrid", ".pf-drow", ".pf-tb", ".pf-tabs"):
            for group in page.query_selector_all(container):
                if not group.is_visible():
                    continue
                kids = page.evaluate(
                    """el => Array.from(el.children).filter(e => e.offsetParent !== null)
                      .map(e => { const b = e.getBoundingClientRect();
                        return {t: (e.textContent || '').trim().slice(0, 30), l: b.left,
                                r: b.right, tp: b.top, bt: b.bottom}; })""", group)
                for i, a in enumerate(kids):
                    for b in kids[i + 1:]:
                        assert not _overlaps(a, b), (container, a, b, tab, theme, width)
        main = page.evaluate(_BOXES, ".k-layout__main")[0]
        help_col = page.evaluate(_BOXES, ".k-help-col")[0]
        if width >= 1680:
            assert help_col["l"] >= main["r"] - 1 and help_col["tp"] <= main["tp"] + 2, (
                help_col, main)
        else:
            assert help_col["tp"] >= main["bt"] - 1, (help_col, main)
        assert not errors, errors
    finally:
        ctx.close()


def test_save_bar_appears_on_an_edit_and_a_blocked_tab_switch_nudges(live):
    ctx, page, errors = _open(live, "about", "light", 1680)
    try:
        bar = page.locator("[data-pf-savebar]")
        assert not bar.is_visible()
        page.fill("#pa-head", "Operations lead")
        assert bar.is_visible() and "Unsaved changes" in bar.inner_text()
        _shot(page, "profile-savebar")
        page.click('.pf-tabs a[data-tab="location"]')
        # Blocked with a nudge, not a modal: still on About you, the message says why.
        assert "tab=about" in page.url or page.url.endswith("/profile")
        assert "Save or discard" in bar.inner_text()
        assert not page.query_selector(".k-modal.is-open")
        page.click("[data-pf-savebar] button[type=submit]")
        page.wait_for_selector('[data-testid="profile-saved"]')
        page.click('.pf-tabs a[data-tab="location"]')
        page.wait_for_selector('[data-testid="profile-location"]')
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("name", ["target_titles", "anti_titles"])
def test_chip_remove_updates_saved_titles_before_and_after_swap(live, name):
    from playwright.sync_api import expect

    ctx, page, errors = _open(live, "employment", "light", 1680)
    try:
        box = page.locator(f'[data-pf-chips="{name}"]')
        field = box.locator('[data-pf-chip-input]')
        for title in ("Lane F first", "Lane F middle", "Lane F last"):
            field.fill(title)
            field.press("Enter")
        page.click('[data-pf-savebar] button[type=submit]')
        page.wait_for_selector('[data-testid="profile-saved"]')
        box.get_by_role("button", name="Remove Lane F middle", exact=True).locator("svg").click()
        expect(box.get_by_text("Lane F middle", exact=True)).to_have_count(0)
        assert "Lane F middle" not in box.locator("textarea").input_value()
        expect(page.locator('[data-pf-savebar]')).to_be_visible()
        field.fill("Lane F new")
        field.press("Enter")
        box.get_by_role("button", name="Remove Lane F new", exact=True).click()
        expect(box.get_by_text("Lane F new", exact=True)).to_have_count(0)
        page.click('[data-pf-savebar] button[type=submit]')
        page.wait_for_selector('[data-testid="profile-saved"]')
        page.reload()
        values = box.locator("textarea").input_value().splitlines()
        assert "Lane F first" in values and "Lane F last" in values
        assert "Lane F middle" not in values and "Lane F new" not in values
        assert not errors
    finally:
        ctx.close()


def test_refused_save_keeps_edits_and_can_be_corrected(live):
    from playwright.sync_api import expect

    ctx, page, errors = _open(live, "employment", "light", 1680)
    try:
        page.fill("#pe-ctarget", "175000")
        page.fill("#pe-cfloor", "abc")
        page.fill("#pe-titles", "Unsaved title")
        page.press("#pe-titles", "Enter")
        page.click('[data-pf-savebar] button[type=submit]')
        expect(page.locator('[data-testid="profile-problem"]')).to_contain_text("whole number")
        expect(page.locator("#pe-cfloor")).to_have_value("abc")
        expect(page.locator("#pe-ctarget")).to_have_value("175000")
        expect(page.get_by_text("Unsaved title", exact=True)).to_be_visible()
        page.click('.pf-tabs a[data-tab="location"]')
        expect(page.locator('[data-pf-msg]')).to_contain_text("Save or discard")
        page.fill("#pe-cfloor", "120000")
        page.click('[data-pf-savebar] button[type=submit]')
        page.wait_for_selector('[data-testid="profile-saved"]')
        page.reload()
        expect(page.locator("#pe-ctarget")).to_have_value("175000")
        expect(page.get_by_text("Unsaved title", exact=True)).to_be_visible()
        assert not errors
    finally:
        ctx.close()


def _seed_title_suggestion(title):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel import paths
    from kestrel.db.engine import make_async_engine
    from kestrel.db.models import TitleSuggestion

    async def seed():
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine)() as s:
            s.add(TitleSuggestion(title=title, why="Matches your resume", evidence=[],
                                  status="suggested", prompt_version="v"))
            await s.commit()
        await engine.dispose()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(lambda: asyncio.run(seed())).result()


def test_accept_title_preserves_unsaved_edits(live):
    from playwright.sync_api import expect

    _seed_title_suggestion("Suggested new title")
    ctx, page, errors = _open(live, "employment", "light", 1680)
    try:
        page.fill("#pe-ctarget", "175000")
        page.fill("#pe-titles", "Unsaved title")
        page.press("#pe-titles", "Enter")
        page.click('[data-testid="title-accept"]')
        expect(page.locator('[data-pf-chips="target_titles"]')
               .get_by_text("Suggested new title", exact=True)).to_be_visible()
        expect(page.locator("#pe-ctarget")).to_have_value("175000")
        expect(page.get_by_text("Unsaved title", exact=True)).to_be_visible()
        page.click('[data-pf-savebar] button[type=submit]')
        page.wait_for_selector('[data-testid="profile-saved"]')
        page.reload()
        expect(page.locator("#pe-ctarget")).to_have_value("175000")
        expect(page.get_by_text("Unsaved title", exact=True)).to_be_visible()
        expect(page.get_by_text("Suggested new title", exact=True)).to_be_visible()
        assert not errors
    finally:
        ctx.close()


def test_background_title_suggestions_appear_without_reloading_edits(live):
    from playwright.sync_api import expect

    ctx, page, errors = _open(live, "employment", "light", 1680)
    try:
        expect(page.locator(
            '#title-suggestions [data-testid="title-suggest-again"]')).to_be_visible()
        page.fill("#pe-ctarget", "175000")
        _seed_title_suggestion("Background title")
        expect(page.locator('[data-testid="title-suggestion"]')
               .get_by_text("Background title", exact=True)).to_be_visible(timeout=12000)
        expect(page.locator("#pe-ctarget")).to_have_value("175000")
        assert not errors
    finally:
        ctx.close()


def test_hosted_title_refresh_preserves_explicit_consent_fields(live, monkeypatch):
    from unittest.mock import AsyncMock
    from urllib.parse import parse_qs

    from playwright.sync_api import expect

    from kestrel.web.pages import profile

    monkeypatch.setattr(profile, "_suggest_route", AsyncMock(return_value={
        "online": True, "provider": "Test provider", "model": "test-model",
        "dest": "test-destination", "sends": "resume text", "employer": "hidden",
        "hosted_active": True,  # online_route reports it; consent_problem requires it (D-UI-182)
    }))
    ctx, page, errors = _open(live, "employment", "light", 1680)
    try:
        form = page.locator('[data-testid="title-suggest-online"]')
        form.get_by_role("button", name="Suggest again").click()
        expect(page.locator('[data-testid="title-suggestions-note"]')).not_to_contain_text(
            "Queued.")
        form.locator('[name="confirmed"]').check()
        with page.expect_request("**/title-suggestions/refresh") as sent:
            form.get_by_role("button", name="Suggest again").click()
        assert parse_qs(sent.value.post_data or "") == {
            "confirmed": ["1"], "model": ["test-model"], "dest": ["test-destination"],
        }
        expect(page.locator('[data-testid="title-suggestions-note"]')).to_contain_text(
            "Queued.")
        assert not errors
    finally:
        ctx.close()


def test_near_floor_margin_has_the_five_stops_and_defaults_to_five(live):
    ctx, page, errors = _open(live, "employment", "light", 1680)
    try:
        seg = page.locator(".pf-margin .k-seg").first
        labels = [t.strip() for t in seg.locator("button").all_inner_texts()]
        assert labels == ["Exact", "2%", "5%", "10%", "15%"], labels
        assert seg.locator("button.is-on").inner_text().strip() == "5%"
        seg.locator("button", has_text="Exact").click()
        page.click("[data-pf-savebar] button[type=submit]")
        page.wait_for_selector('[data-testid="profile-saved"]')
        assert page.locator(".pf-margin .k-seg button.is-on").first.inner_text().strip() \
            == "Exact"
        assert "set aside" in page.locator('[data-testid="near-floor-hint"]').first.inner_text()
        assert not errors, errors
    finally:
        ctx.close()


def test_the_preview_is_a_scaled_true_page_and_enlarge_matches_it(live):
    """One page laid out at 816 px, scaled with a transform: the lines in the card and in
    Enlarge are the same lines, because neither is re-flowed."""
    ctx, page, errors = _open(live, "documents", "light", 1680)
    try:
        card = page.locator("#preview [data-pf-page]")
        frame = card.locator("iframe")
        # Laid out at 816 px whatever the scale: the transform shrinks it, nothing re-flows.
        assert frame.evaluate("f => f.offsetWidth") == 816
        k_card = float(card.get_attribute("data-pf-scale-k"))
        assert 0.3 < k_card < 1.0
        _shot(page, "profile-documents-preview")
        page.click("[data-pf-enlarge]")
        page.wait_for_selector("#pf-enlarge.is-open")
        wait_for_layout(page)
        big = page.locator("#pf-enlarge-body [data-pf-page]")
        k_big = float(big.get_attribute("data-pf-scale-k"))
        assert k_big > k_card

        def lines(scope: str) -> list[str]:
            return page.evaluate(
                """sel => { const d = document.querySelector(sel + ' iframe').contentDocument;
                  const out = [];
                  const w = d.createTreeWalker(d.body, NodeFilter.SHOW_TEXT);
                  let last = null, cur = '';
                  for (let n = w.nextNode(); n; n = w.nextNode()) {
                    if (!n.textContent.trim()) continue;
                    const r = d.createRange(); r.selectNodeContents(n);
                    for (const x of r.getClientRects()) {
                      const top = Math.round(x.top);
                      if (last !== null && top !== last) { out.push(cur); cur = ''; }
                      last = top; cur += '|';
                    }
                    cur += n.textContent.trim();
                  }
                  out.push(cur); return out; }""", scope)

        assert lines("#preview [data-pf-page]") == lines("#pf-enlarge-body [data-pf-page]")
        _shot(page, "profile-documents-enlarged")
        assert not errors, errors
    finally:
        ctx.close()
