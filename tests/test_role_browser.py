"""The Role page in a real browser (UI overhaul parity check against the locked mockup,
the locked design mockup (kept privately)). Light and dark at 1280 and 1680: no overlap, no
wrapped titles or buttons, no sideways scroll, the decision column beside the evaluation when wide
and under it when narrow; then the interactions the mockup demos.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from tests import role_seed
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


def _overlaps(a, b) -> bool:
    return (
        a["l"] < b["r"] - 1
        and b["l"] < a["r"] - 1
        and a["tp"] < b["bt"] - 1
        and b["tp"] < a["bt"] - 1
    )


@pytest.fixture
def live(monkeypatch):
    from playwright.sync_api import sync_playwright

    from kestrel.pipeline import profile as profile_mod
    from kestrel.web.serve import running_server

    real = profile_mod.load_profile

    def fake(track):
        base = real(track)
        if track == "employment":
            return SimpleNamespace(
                employment_terms=SimpleNamespace(comp_floor_usd=100000, comp_target_usd=125000)
            )
        return base

    monkeypatch.setattr(profile_mod, "load_profile", fake)
    ids = asyncio.run(role_seed.seed_async())
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, ids
        browser.close()


def _page(live, theme: str, width: int, path: str = ""):
    base, browser, ids = live
    ctx = browser.new_context(viewport={"width": width, "height": 1000}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/opportunities/{ids['strong']}{path}")
    page.wait_for_selector("#rl-body")
    wait_for_layout(page)
    return ctx, page, errors


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_no_overlap_no_wrapping_no_sideways_scroll(live, theme, width):
    ctx, page, errors = _page(live, theme, width)
    try:
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        assert (
            page.evaluate(
                "document.querySelector('.k-main, main').scrollWidth <= "
                "document.querySelector('.k-main, main').clientWidth + 1"
            )
            is True
        )
        for sel in (
            "h1",
            ".rl-tabs a",
            ".rl-side .k-btn:not(.rl-lock)",
            ".rl-postbtn",
            ".k-pill",
            ".rl-pn .k-btn",
            ".rl-wchip",
            ".rl-fbrow .k-btn",
            ".rl-btns .k-btn",
        ):
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    assert page.evaluate(_LINES, el) <= 1, (sel, theme, width, el.inner_text()[:40])
        for container in (
            ".rl-row2",
            ".rl-fbrow",
            ".rl-trio",
            ".rl-tabs",
            ".rl-crumbrow",
            ".k-scene__title",
        ):
            for group in page.query_selector_all(container):
                if not group.is_visible():
                    continue
                kids = page.evaluate(
                    """el => Array.from(el.children).filter(e => e.offsetParent !== null)
                      .map(e => { const b = e.getBoundingClientRect();
                        return {t: (e.textContent || '').trim().slice(0, 30), l: b.left,
                                r: b.right, tp: b.top, bt: b.bottom}; })""",
                    group,
                )
                for i, a in enumerate(kids):
                    for b in kids[i + 1 :]:
                        if container == ".rl-tabs" and "" in (a["t"], b["t"]):  # the ink
                            continue
                        assert not _overlaps(a, b), (container, a, b, theme, width)
        # Parity: the decision column is beside the evaluation when wide, under it when the main
        # column is 1300px or less (the mockup's container rule); the band stays in the column.
        main = page.evaluate(_BOXES, ".rl-main")[0]
        side = page.evaluate(_BOXES, ".rl-side")[0]
        if width >= 1680:
            assert side["l"] >= main["r"] - 1 and abs(side["tp"] - main["tp"]) <= 4, (side, main)
        else:
            assert side["tp"] >= main["bt"] - 1, (side, main)
        # The three cards above the wheel sit in one row wide, two rows narrow; no card is cut.
        trio = page.evaluate(_BOXES, ".rl-trio > *")
        assert len({round(b["tp"]) for b in trio}) == (1 if width >= 1680 else 2), trio
        crumb, h1 = page.evaluate(_BOXES, ".rl-crumb")[0], page.evaluate(_BOXES, "h1")[0]
        assert 0 <= h1["tp"] - crumb["bt"] <= 24, (crumb, h1)
        assert not page.query_selector("#announcement-slot")
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("path", ["/posting", "/organization", "/documents", "/history"])
def test_the_other_tabs_fit_and_have_their_own_urls(live, path):
    ctx, page, errors = _page(live, "light", 1280, path)
    try:
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        assert page.locator("#rl-tabs a.is-on").get_attribute("data-tab") == path[1:]
        ink = page.evaluate("""() => { const t = document.querySelector('.rl-tabs__ink');
            const on = document.querySelector('.rl-tabs a.is-on');
            return Math.abs(t.getBoundingClientRect().left - on.getBoundingClientRect().left); }""")
        assert ink < 3
        assert not errors, errors
    finally:
        ctx.close()


def test_tabs_swap_in_place_push_the_url_and_the_underline_glides(live):
    ctx, page, errors = _page(live, "light", 1680)
    try:
        page.evaluate("window.__same = 1")
        page.get_by_test_id("tab-history").click()
        page.locator("#rl-histtab").wait_for()
        assert page.evaluate("window.__same") == 1
        assert page.url.endswith("/history")
        ink = page.evaluate("""() => Math.abs(document.querySelector('.rl-tabs__ink')
            .getBoundingClientRect().left - document.querySelector('.rl-tabs a.is-on')
            .getBoundingClientRect().left)""")
        assert ink < 3
        page.get_by_test_id("tab-evaluation").click()
        page.locator("#rl-tab-evaluation").wait_for()
        assert page.url.endswith(f"/opportunities/{live[2]['strong']}")
        assert not errors, errors
    finally:
        ctx.close()


def test_shortlist_unlocks_prepare_in_place_and_the_second_click_undoes_it(live):
    ctx, page, errors = _page(live, "light", 1680)
    try:
        page.evaluate("window.__same = 1")
        assert page.locator(".rl-lock").count() == 3
        page.locator(".rl-lock").first.hover()
        assert "Available once you shortlist" in page.locator(".rl-lock .rl-tip").first.inner_text()
        page.get_by_test_id("add-to-list").click()
        page.get_by_test_id("on-my-list").wait_for()
        assert page.locator(".rl-lock").count() == 0 and page.evaluate("window.__same") == 1
        toast = page.locator(".k-toast")
        toast.wait_for()
        assert "switched on" in toast.inner_text()
        toast.get_by_role("button", name="Undo").click()
        page.get_by_test_id("add-to-list").wait_for()
        assert page.locator(".rl-lock").count() == 3
        page.get_by_test_id("add-to-list").click()
        page.get_by_test_id("on-my-list").wait_for()
        page.get_by_test_id("on-my-list").click()
        page.get_by_test_id("add-to-list").wait_for()
        assert not errors, errors
    finally:
        ctx.close()


def test_not_for_me_opens_inline_and_why_opens_the_help_card(live):
    ctx, page, errors = _page(live, "light", 1280)
    try:
        page.get_by_test_id("not-for-me").click()
        page.locator("#rl-nfr.is-on").wait_for()
        assert page.locator("#rl-nfr button[data-why]").count() == 4
        page.locator("[data-rl-nfm-close]").click()
        page.locator("#rl-nfr:not(.is-on)").wait_for()
        page.get_by_test_id("why-link").click()
        modal = page.locator("#rl-why")
        modal.wait_for(state="visible")
        mb = modal.bounding_box()
        assert abs((mb["y"] + mb["height"] / 2) - 500) < 80
        assert "Why are some actions greyed out?" in modal.inner_text()
        modal.get_by_role("button", name="Got it").click()
        modal.wait_for(state="hidden")
        assert not errors, errors
    finally:
        ctx.close()


def test_your_view_adjust_opens_and_saves_a_rating_with_undo(live):
    ctx, page, errors = _page(live, "light", 1680)
    try:
        page.get_by_test_id("view-adjust").click()
        page.locator("#rl-adj.is-on").wait_for()
        assert page.get_by_test_id("view-save").is_disabled()
        page.locator("#rl-pick button[data-n='4']").click()
        page.locator("#rl-nud .rl-nr").first.locator("[data-n='-1']").click()
        assert page.locator("#rl-nud .rl-nr").first.locator("b").inner_text() == "4.3"
        page.get_by_test_id("view-save").click()
        page.get_by_test_id("view-saved").wait_for()
        assert "Your rating is saved" in page.locator(".k-toast").inner_text()
        page.locator(".k-toast").get_by_role("button", name="Undo").click()
        page.get_by_test_id("view-yes").wait_for()
        assert not errors, errors
    finally:
        ctx.close()


def test_match_map_focuses_a_group_and_a_click_elsewhere_clears_it(live):
    ctx, page, errors = _page(live, "light", 1680)
    try:
        page.locator(".rl-wchip[data-f='gap']").click()
        page.locator(".rl-main.is-focus").wait_for()
        assert page.locator(".rl-fl.is-hit").count() == 2
        assert page.locator(".rl-wseg.is-hit").count() == 2
        assert "not met" in page.locator("#rl-wfocus").inner_text()
        page.locator(".rl-dsum").first.click()
        page.locator(".rl-main:not(.is-focus)").wait_for()
        page.locator(".rl-wseg").first.dispatch_event("click")
        page.locator(".rl-main.is-focus").wait_for()
        assert not errors, errors
    finally:
        ctx.close()


def test_add_a_story_flies_up_from_the_bottom_and_saves(live):
    ctx, page, errors = _page(live, "light", 1280)
    try:
        page.locator("a[data-rl-story]").first.click()
        sheet = page.locator("#rl-story")
        sheet.wait_for(state="visible")
        wait_for_layout(page)
        box = sheet.bounding_box()
        assert box["y"] + box["height"] >= 990  # anchored to the bottom of the window
        assert "Kestrel only knows what you write down" in sheet.inner_text()
        assert page.get_by_test_id("story-for").inner_text() != ""
        for label in ("What was the situation?", "What did you do?", "What changed?"):
            assert label.lower() in sheet.inner_text().lower()
        sheet.locator("textarea").first.fill("Three sites reported differently.")
        sheet.get_by_role("button", name="Save", exact=True).click()
        page.locator(".k-toast").wait_for()
        assert "Story saved" in page.locator(".k-toast").inner_text()
        sheet.wait_for(state="hidden")
        assert not errors, errors
    finally:
        ctx.close()


def test_documents_open_as_a_scaled_page_and_previous_next_slide_the_body(live):
    ctx, page, errors = _page(live, "light", 1280)
    try:
        page.get_by_test_id("doc-open").click()
        pv = page.locator("#rl-preview")
        pv.wait_for(state="visible")
        wait_for_layout(page)
        frame = pv.locator(".rl-pvframe").bounding_box()
        paper = pv.locator("[data-rl-page]").bounding_box()
        assert (
            frame["width"] <= 816 and abs(frame["width"] - paper["width"]) < 2
        )  # scaled, not reflowed
        assert (
            pv.get_by_test_id("doc-download").is_visible()
            and pv.get_by_test_id("doc-edit").is_visible()
        )
        page.keyboard.press("Escape")
        pv.wait_for(state="hidden")
        page.evaluate("window.__same = 1")
        page.get_by_test_id("rl-next").click()
        page.wait_for_function(f"location.pathname.endsWith('/{live[2]['second']}')")
        page.wait_for_selector("h1:has-text('Process Improvement Lead')")
        assert page.evaluate("window.__same") == 1
        assert "2 of 4" in page.get_by_test_id("rl-pos").inner_text()
        assert "Process Improvement Lead" in page.title()
        page.get_by_test_id("rl-prev").click()
        page.wait_for_selector("h1:has-text('Senior Operations Analyst')")
        assert not errors, errors
    finally:
        ctx.close()


def test_more_menu_and_deeper_read_ask_first(live):
    ctx, page, errors = _page(live, "light", 1680)
    try:
        page.get_by_test_id("more").click()
        menu = page.locator(".rl-menu")
        menu.wait_for(state="visible")
        for word in ("Deeper read", "Archive", "Copy link", "Score again"):
            assert word in menu.inner_text()
        page.get_by_test_id("escalate").click()
        dlg = page.locator("#rl-deeper")
        dlg.wait_for(state="visible")
        assert "Nothing is sent until you press Send" in dlg.inner_text()
        assert "Show exactly what is sent" in dlg.inner_text()
        dlg.get_by_role("button", name="Cancel").click()
        dlg.wait_for(state="hidden")
        assert not errors, errors
    finally:
        ctx.close()


def test_the_page_mounts_unmounts_and_mounts_again_with_one_set_of_everything(live):
    """The page lifecycle (D-UI-210): the Role page unmounted and mounted again in place starts
    exactly what it started on the document load (the dialog observer included) and leaves no
    timer or observer behind. After it, one click on a match-map chip focuses its group and stays
    focused (a doubled listener toggles it on and off in the same click), and the tab and
    Previous/Next history htmx writes still work: a tab pushes its URL and Back returns."""
    base, browser, ids = live
    ctx = browser.new_context(viewport={"width": 1680, "height": 1000}, color_scheme="light")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(TIMER_PROBE_JS)
    try:
        page.goto(f"{base}/opportunities/{ids['strong']}")
        page.wait_for_selector("#rl-body")
        wait_for_layout(page)
        first = assert_page_remounts_clean(page, "role", "role.js", settle_ms=600)
        assert first["observers"] == 1 and first["timers"] == 0, first
        page.locator(".rl-wchip[data-f='gap']").click()
        page.locator(".rl-main.is-focus").wait_for()
        assert page.locator(".rl-fl.is-hit").count() == 2
        page.keyboard.press("Escape")
        page.locator(".rl-main:not(.is-focus)").wait_for()
        # The history a tab writes is htmx's, and the page's own listeners leave it alone.
        before = page.evaluate("history.length")
        page.get_by_test_id("tab-history").click()
        page.locator("#rl-histtab").wait_for()
        assert page.url.endswith("/history")
        assert page.evaluate("history.length") == before + 1
        page.go_back()
        page.wait_for_function("() => !location.pathname.endsWith('/history')")
        # Back put htmx's saved body in place, a new dialog host: the page watches that one, and
        # still holds the one observer it started with.
        page.wait_for_function("KUI.page._stats().observers === 1")
        assert page.evaluate("KUI.page._stats().listeners") == first["listeners"]
        # A dialog an action loads is opened by the observer, once.
        page.get_by_test_id("more").click()
        page.get_by_test_id("escalate").click()
        page.locator("#rl-deeper").wait_for(state="visible")
        # Unmounted, the observer is gone: a dialog dropped into the host stays closed.
        page.evaluate("KUI.page.unmount()")
        page.evaluate("document.querySelector('#rl-dialog').innerHTML = "
                      "'<div class=\"k-modal\" id=\"g2-late\" hidden></div>'")
        page.wait_for_timeout(300)
        assert page.evaluate("document.querySelector('#g2-late').classList.contains('is-open')") \
            is False
        assert errors == []
    finally:
        ctx.close()


def test_a_tab_change_fades_the_new_pane_in_without_a_flash(live):
    """Owner, 2026-10-30: a "flash and flicker" on a tab change. The fade started after htmx's
    settle delay, so the new pane was drawn at full strength for a frame or two, dropped to
    nothing, then faded in. Sampled every frame: the new content starts faint and only rises."""
    ctx, page, errors = _page(live, "light", 1400)
    try:
        for tab in ("posting", "organization", "evaluation"):
            page.evaluate("""() => { window.__f = [];
              const pane = document.getElementById('rl-pane');
              const first = pane.firstElementChild; let n = 0;
              const tick = () => { const c = pane.firstElementChild;
                if (c !== first) window.__f.push(+getComputedStyle(c).opacity
                                                 * +getComputedStyle(pane).opacity);
                if (++n < 90) requestAnimationFrame(tick); };
              requestAnimationFrame(tick); }""")
            page.click(f'#rl-tabs a[data-tab="{tab}"]')
            page.wait_for_timeout(1700)
            seen = page.evaluate("window.__f")
            assert seen and seen[0] < 0.5, (tab, seen[:6])
            rising = all(b >= a - 0.01 for a, b in zip(seen, seen[1:], strict=False))
            assert rising, (tab, seen[:12])
        assert not errors, errors
    finally:
        ctx.close()
