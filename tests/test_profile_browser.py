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
from tests.browser import (
    G4_LISTENER_PROBE_JS,
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


def _open(live, tab: str, theme: str, width: int, probe: bool = False):
    base, browser, master_id = live
    ctx = browser.new_context(viewport={"width": width, "height": 1300}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    if probe:
        page.add_init_script(TIMER_PROBE_JS)
    page.goto(_url(base, tab, master_id))
    page.wait_for_selector(".pf-tabs")
    if tab == "documents":
        page.wait_for_selector("#preview [data-pf-page] iframe")
    wait_for_layout(page)
    return ctx, page, errors


def _save_and_wait(page, selector: str = "[data-pf-savebar] button[type=submit]") -> None:
    """Click Save, then wait for the NEW saved toast and the clean form that replaced the old one
    (the toast rises with the response, ahead of the swap)."""
    page.evaluate(
        "document.querySelectorAll('.k-toast').forEach(t => t.setAttribute('data-old', ''))")
    page.click(selector)
    page.wait_for_selector(
        '.k-toast[data-testid="profile-saved"]:not([data-old]):not(.is-leaving)')
    page.wait_for_selector("form[data-pf-form]:not([data-dirty])", state="attached")


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
                    """el => Array.from(el.children).filter(e => e.offsetParent !== null
                        && !e.classList.contains('k-tabink'))
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


def test_a_save_is_confirmed_once_by_the_toast_and_the_bar_slides_away(live):
    """#88: no "Saved to profile.yml" bar under the Saved / Undo toast."""
    ctx, page, errors = _open(live, "about", "light", 1280)
    try:
        page.fill("#pa-head", "Operations lead")
        page.click("[data-pf-savebar] button[type=submit]")
        toast = page.locator('.k-toast[data-testid="profile-saved"]')
        toast.wait_for(state="visible")
        assert "Saved" in toast.inner_text()
        page.wait_for_function(
            "() => !document.querySelector('[data-pf-savebar]').hasAttribute('data-shown')")
        page.wait_for_function(
            "() => getComputedStyle(document.querySelector('[data-pf-savebar]'))"
            ".visibility === 'hidden'")
        assert "Saved to profile.yml" not in page.inner_text("body")
        assert page.locator(".pf-saved").count() == 0
        assert page.locator(".k-toast").count() == 1
        assert not errors, errors
    finally:
        ctx.close()


_SCAN_DONE_JS = """() => {
  const sig = document.getElementById('scan-signal');
  const fire = (ev) => {
    sig.setAttribute('data-ready', '1'); sig.setAttribute('data-event', ev);
    sig.setAttribute('data-scanning', '0'); sig.setAttribute('data-new', '4');
    document.dispatchEvent(new Event('htmx:afterSettle', {bubbles: true}));
  };
  fire('1'); fire('2');
}"""


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 900])
def test_the_search_done_toast_clears_itself_pauses_on_hover_and_clears_the_save_bar(
        live, theme, width):
    """#86: it used to stay forever and sat on top of the Save bar, wrapped message or not."""
    ctx, page, errors = _open(live, "about", theme, width)
    try:
        page.fill("#pa-head", "Operations lead")  # the Save bar is up
        bar = page.locator("[data-pf-savebar]")
        assert bar.is_visible()
        # The longest message the bar says: a blocked tab switch.
        page.click('.pf-tabs a[data-tab="location"]')
        assert "Save or discard" in bar.inner_text()
        page.evaluate(_SCAN_DONE_JS)
        toast = page.locator("#kestrel-toast")
        toast.wait_for(state="visible")
        page.wait_for_timeout(500)  # the region has settled above the bar
        t = toast.bounding_box()
        b = bar.bounding_box()
        assert t["y"] + t["height"] <= b["y"] + 1, (t, b)  # never over Save
        measured = page.evaluate(
            "getComputedStyle(document.documentElement).getPropertyValue('--pf-savebar-h')")
        assert measured == f"{round(b['height'])}px", (measured, b)
        # Held while the pointer is on it, past the 9 s it would otherwise last.
        page.mouse.move(t["x"] + t["width"] / 2, t["y"] + t["height"] / 2)
        page.wait_for_timeout(9500)
        assert toast.is_visible()
        page.mouse.move(5, 5)
        toast.wait_for(state="hidden", timeout=12000)
        assert not errors, errors
    finally:
        ctx.close()


_FRAMES_JS = """() => new Promise(resolve => {
  const frames = [];
  const t0 = performance.now();
  const bar = () => document.querySelector('[data-pf-savebar]');
  const tick = () => {
    const b = bar();
    const kids = b ? Array.from(b.children) : [];
    const tf = b ? new DOMMatrix(getComputedStyle(b).transform).m42 : null;
    frames.push({t: Math.round(performance.now() - t0),
      kids: kids.map(k => +getComputedStyle(k).opacity),
      barY: tf, barOpacity: b ? +getComputedStyle(b).opacity : null,
      toasts: Array.from(document.querySelectorAll('.k-toast')).map(
        k => ({old: k.hasAttribute('data-old'), leaving: k.classList.contains('is-leaving'),
               top: Math.round(k.getBoundingClientRect().top)}))});
    if (performance.now() - t0 < 2200) requestAnimationFrame(tick); else resolve(frames);
  };
  requestAnimationFrame(tick);
})"""


def test_a_save_slides_the_bar_away_with_its_contents_and_the_old_toast_leaves_animated(live):
    """#88: no frame shows the bar's contents faded out before it moves, and a second save's
    earlier toast leaves with its exit animation instead of vanishing."""
    ctx, page, errors = _open(live, "about", "light", 1280)
    try:
        page.fill("#pa-head", "Operations lead")
        _save_and_wait(page)
        page.wait_for_timeout(700)
        page.fill("#pa-head", "Operations director")
        page.wait_for_timeout(700)  # the bar has finished sliding in
        page.evaluate("document.querySelectorAll('.k-toast').forEach("
                      "t => t.setAttribute('data-old', ''))")
        page.evaluate("() => { window.__frames = (" + _FRAMES_JS + ")(); }")
        page.click("[data-pf-savebar] button[type=submit]", no_wait_after=True)
        frames = page.evaluate("window.__frames")
        shown = [f for f in frames if f["barY"] is not None]
        assert shown
        # Contents are never faded: every child of the bar is fully opaque in every frame.
        assert all(all(o == 1 for o in f["kids"]) for f in shown), shown[:3]
        # The bar did slide (translateY went from 0 to a positive offset).
        assert max(f["barY"] for f in shown) > 20
        # The old toast was seen leaving (is-leaving) at least once before it was gone.
        assert any(t["old"] and t["leaving"] for f in frames for t in f["toasts"])
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
        _save_and_wait(page)
        page.click('.pf-tabs a[data-tab="location"]')
        page.wait_for_selector('[data-testid="profile-location"]')
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("width", [1280, 700])
@pytest.mark.parametrize("tab", ["about", "employment"])
def test_the_save_bar_never_covers_the_last_hint_at_the_foot_of_the_page(live, tab, width):
    """With the form dirty and the page scrolled to the bottom, the lowest hint on the page is
    fully above the Save bar (the bar used to sit over it: the page had no room below)."""
    ctx, page, errors = _open(live, tab, "light", width)
    try:
        page.set_viewport_size({"width": width, "height": 700})
        wait_for_layout(page)
        page.evaluate("""() => { const f = document.querySelector('form[data-pf-form]');
          const i = f.querySelector('input[type=text], input:not([type]), textarea');
          i.value = i.value + ' '; i.dispatchEvent(new Event('input', {bubbles: true})); }""")
        page.wait_for_selector("[data-pf-savebar]", state="visible")
        page.wait_for_timeout(600)  # the bar slides in and the room below eases open
        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        page.wait_for_timeout(300)
        got = page.evaluate("""() => {
          const bar = document.querySelector('[data-pf-savebar]').getBoundingClientRect();
          // The lowest text on the page column: its last hint or note, whatever card holds it.
          const hints = Array.from(document.querySelectorAll('.pf-wrap *')).filter(h =>
            h.offsetParent !== null && !h.closest('[data-pf-savebar]') && !h.children.length &&
            (h.textContent || '').trim() && getComputedStyle(h).visibility === 'visible');
          const low = hints.map(h => h.getBoundingClientRect())
            .sort((a, b) => b.bottom - a.bottom)[0];
          return {barTop: bar.top, barBottom: bar.bottom, hintBottom: low ? low.bottom : null,
                  hintTop: low ? low.top : null, inView: low ? low.bottom <= innerHeight : null};
        }""")
        assert got["hintBottom"] is not None, "no hint on the tab"
        assert got["inView"], got
        # Clear of the bar: either wholly above it, or (phone width, sticky bar in the flow) not
        # overlapping it.
        # and with breathing room, so the last line never sits hard against the bar.
        assert got["hintBottom"] <= got["barTop"] - 8 or got["hintTop"] >= got["barBottom"], got
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
        _save_and_wait(page)
        box.get_by_role("button", name="Remove Lane F middle", exact=True).locator("svg").click()
        expect(box.get_by_text("Lane F middle", exact=True)).to_have_count(0)
        assert "Lane F middle" not in box.locator("textarea").input_value()
        expect(page.locator('[data-pf-savebar]')).to_be_visible()
        field.fill("Lane F new")
        field.press("Enter")
        box.get_by_role("button", name="Remove Lane F new", exact=True).click()
        expect(box.get_by_text("Lane F new", exact=True)).to_have_count(0)
        _save_and_wait(page)
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
        _save_and_wait(page)
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
        _save_and_wait(page)
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
        # The box starts closed (D-UI-199); a suggestion that arrives opens it.
        expect(page.locator(
            '#title-suggestions [data-testid="title-suggest-again"]')).to_be_attached()
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
        page.click('[data-testid="title-suggest-toggle"]')  # the box opens from the card (D-UI-199)
        form = page.locator('[data-testid="title-suggest-online"]')
        form.get_by_role("button", name="Suggest", exact=True).click()
        expect(page.locator('[data-testid="title-suggestions-note"]')).not_to_contain_text(
            "Queued.")
        form.locator('[name="confirmed"]').check()
        with page.expect_request("**/title-suggestions/refresh") as sent:
            form.get_by_role("button", name="Suggest", exact=True).click()
        assert parse_qs(sent.value.post_data or "") == {
            "confirmed": ["1"], "model": ["test-model"], "dest": ["test-destination"],
        }
        expect(page.locator('[data-testid="title-suggestions-note"]')).to_contain_text(
            "Queued.")
        assert not errors
    finally:
        ctx.close()


def test_near_floor_switch_off_saves_exact_and_on_defaults_to_five(live):
    ctx, page, errors = _open(live, "employment", "light", 1680)
    try:
        seg = page.locator(".pf-margin .k-seg").first
        labels = [t.strip() for t in seg.locator("button").all_inner_texts()]
        assert labels == ["2%", "5%", "10%", "15%"], labels
        assert seg.locator("button.is-on").inner_text().strip() == "5%"
        page.locator(".pf-margin__sw").first.click()
        _save_and_wait(page)
        assert not page.locator(".pf-margin [data-pf-margin-on]").first.is_checked()
        assert page.locator(".pf-margin [data-pf-margin-value]").first.input_value() == "0"
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


# --------------------------------------------------------------------- O17: US Letter pages
def _grow_resume(master_id: int, bullets: int = 46) -> None:
    """Many long bullets on the seeded role, so the document runs past one Letter page. Written
    straight to the database file: the running server owns the event loop."""
    import json
    import sqlite3

    from kestrel import paths

    conn = sqlite3.connect(str(paths.db_path()))
    try:
        sec = conn.execute("SELECT id FROM doc_section WHERE doc_master_id=?",
                           (master_id,)).fetchone()[0]
        for i in range(3, bullets + 3):
            cur = conn.execute(
                "INSERT INTO doc_slot (doc_section_id, key, sort_order, optional,"
                " default_variant_key) VALUES (?,?,?,0,'original')", (sec, f"r1_b{i}", i))
            conn.execute(
                "INSERT INTO doc_variant (doc_slot_id, key, text, tags, derived)"
                " VALUES (?,?,?,?,0)",
                (cur.lastrowid, "original",
                 f"Bullet {i}: ran the weekly operations review across four sites and "
                 "tracked every open action to closure with the site managers.",
                 json.dumps(["operations"])))
        conn.commit()
    finally:
        conn.close()


def test_a_long_resume_preview_is_paginated_into_letter_pages(live):
    base, browser, master_id = live
    _grow_resume(master_id)
    ctx, page, errors = _open(live, "documents", "light", 1680)
    try:
        card = page.locator("#preview [data-pf-page]")
        page.wait_for_function(
            "() => +document.querySelector('#preview [data-pf-page]')"
            ".getAttribute('data-pf-pages') > 1")
        pages = int(card.get_attribute("data-pf-pages"))
        assert pages >= 2
        assert card.locator("iframe").evaluate("f => f.offsetHeight") == pages * 1056
        assert card.locator(".pf-page__gap").count() == pages - 1
        # Every line of text sits inside one sheet's margins: nothing is cut by a page break.
        bad = card.locator("iframe").evaluate(
            """f => { const d = f.contentDocument, m = parseFloat(
                getComputedStyle(d.body).paddingTop), bad = [];
              d.querySelectorAll('ul.bullets > li, h2.section-heading, .role .row').forEach(e => {
                const r = e.getBoundingClientRect();
                const pg = Math.floor(r.top / 1056);
                if (r.bottom > (pg + 1) * 1056 - m + 0.5) bad.push(e.textContent.trim()
                    .slice(0, 30));
                if (r.top < pg * 1056 + m - 0.5 && pg > 0) bad.push('top ' + e.textContent);
              });
              return bad; }""")
        assert bad == [], bad
        # The heading is never left alone at the foot of a sheet.
        orphan = card.locator("iframe").evaluate(
            """f => { const d = f.contentDocument;
              return Array.from(d.querySelectorAll('h2.section-heading')).filter(h => {
                const nx = d.elementFromPoint(40, h.getBoundingClientRect().bottom + 4);
                return nx && Math.floor(h.getBoundingClientRect().top / 1056)
                  !== Math.floor((h.getBoundingClientRect().bottom + 4) / 1056); }).length; }""")
        assert orphan == 0
        # The resume text itself is untouched by the paging.
        text = card.locator("iframe").evaluate("f => f.contentDocument.body.innerText")
        assert "Bullet 3: ran the weekly operations review" in text
        _shot(page, "profile-documents-preview-pages")
        # Enlarge shows the same pages.
        page.click("[data-pf-enlarge]")
        page.wait_for_selector("#pf-enlarge.is-open")
        wait_for_layout(page)
        page.wait_for_function(
            "() => document.querySelector('#pf-enlarge-body [data-pf-page]')"
            ".getAttribute('data-pf-pages')")
        big = page.locator("#pf-enlarge-body [data-pf-page]")
        assert int(big.get_attribute("data-pf-pages")) == pages
        assert not errors, errors
    finally:
        ctx.close()


def test_a_short_resume_preview_is_one_page_with_no_break(live):
    ctx, page, errors = _open(live, "documents", "light", 1680)
    try:
        card = page.locator("#preview [data-pf-page]")
        page.wait_for_function(
            "() => document.querySelector('#preview [data-pf-page]')"
            ".getAttribute('data-pf-pages')")
        assert card.get_attribute("data-pf-pages") == "1"
        assert card.locator(".pf-page__gap").count() == 0
    finally:
        ctx.close()


# ----------------------------------------------------------------- O18: the avatar in place
def test_uploading_a_photo_updates_both_avatars_without_leaving_the_page(live, tmp_path):
    from tests.test_profile_review_fixes import _png

    base, browser, master_id = live
    ctx, page, errors = _open(live, "about", "light", 1280)
    try:
        page.evaluate("window.__stay = 1")
        assert page.locator("#k-topavatar img").count() == 0
        shot = tmp_path / "me.png"
        shot.write_bytes(_png())
        page.set_input_files("#pf-photo-file", str(shot))
        page.wait_for_selector("#pf-photo .pf-avatar img")
        page.wait_for_selector("#k-topavatar img")
        assert page.evaluate("window.__stay") == 1  # no navigation, no reload
        assert page.locator("#k-topavatar").get_attribute("data-avatar") == "photo"
        assert page.locator("#k-topavatar img").get_attribute("src").startswith(
            "/profile/photo?v=")
        page.click("[data-pf-photo-remove]")
        page.wait_for_function("() => !document.querySelector('#k-topavatar img')")
        assert page.locator("#k-topavatar").get_attribute("data-avatar") == "initial"
        assert not errors, errors
    finally:
        ctx.close()


def test_choosing_a_picture_in_settings_updates_the_top_bar_at_once(live):
    base, browser, _master = live
    ctx = browser.new_context(viewport={"width": 1280, "height": 1000})
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"{base}/settings#preferences")
        page.wait_for_selector('[data-testid="st-av-star"]')
        page.evaluate("window.__stay = 1")
        page.click('[data-testid="st-av-star"]')
        page.wait_for_function(
            "() => document.querySelector('#k-topavatar').dataset.avatar === 'star'")
        assert page.locator("#k-topavatar svg").count() == 1
        assert page.evaluate("window.__stay") == 1
        assert not errors, errors
    finally:
        ctx.close()


def test_work_mode_and_location_agree_both_ways(live):
    """D-UI-214: Hybrid in Work mode ticks the metro rule on Location (and Remote ticks Remote);
    unticking Remote on Location drops it from Work mode. The two never contradict."""
    ctx, page, errors = _open(live, "employment", "light", 1680)
    try:
        def modes() -> dict:
            return page.evaluate(
                """() => Object.fromEntries(Array.from(document.querySelectorAll(
                  '[data-testid=work-mode] input')).map(i => [i.name, i.checked]))""")

        def geo() -> dict:
            return page.evaluate(
                """() => Object.fromEntries(['remote_ok', 'hybrid_requires_metro'].map(
                  n => [n, document.querySelector('[name=' + n + ']').checked]))""")

        def save() -> None:
            _save_and_wait(page)

        # Start from On-site only, saved.
        for key in ("remote", "hybrid"):
            if modes()[f"mode_{key}"]:
                page.click(f'[data-testid=work-mode] label:has(input[name=mode_{key}])')
        if not modes()["mode_onsite"]:
            page.click('[data-testid=work-mode] label:has(input[name=mode_onsite])')
        save()
        page.click('.pf-tabs a[data-tab="location"]')
        page.wait_for_selector('[data-testid="profile-location"]')
        assert geo() == {"remote_ok": False, "hybrid_requires_metro": False}

        # Choose Remote and Hybrid in Work mode: Location shows both ticked.
        page.click('.pf-tabs a[data-tab="employment"]')
        page.wait_for_selector('[data-testid="work-mode"]')
        for key in ("remote", "hybrid"):
            page.click(f'[data-testid=work-mode] label:has(input[name=mode_{key}])')
        save()
        page.click('.pf-tabs a[data-tab="location"]')
        page.wait_for_selector('[data-testid="profile-location"]')
        assert geo() == {"remote_ok": True, "hybrid_requires_metro": True}

        # Untick Remote on Location: Work mode loses Remote, keeps Hybrid and On-site.
        page.click("label[for=pl-remote]")
        save()
        page.click('.pf-tabs a[data-tab="employment"]')
        page.wait_for_selector('[data-testid="work-mode"]')
        assert modes() == {"mode_remote": False, "mode_hybrid": True, "mode_onsite": True}
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("tab", ["about", "employment", "documents"])
def test_the_page_mounts_unmounts_and_mounts_again_with_one_set_of_everything(live, tab):
    """The page lifecycle (D-UI-210): Profile unmounted and mounted again in place starts exactly
    what it started on the document load (the chips, the Save bar, the preview's observers and
    frames) and leaves nothing running in between."""
    ctx, page, errors = _open(live, tab, "light", 1680, probe=True)
    try:
        first = assert_page_remounts_clean(page, "profile", "profile.js", settle_ms=800)
        assert first["observers"] == 1  # the preview fit
        if tab == "documents":
            # After the second mount a refreshed preview's new frame is heard and paginated.
            page.evaluate("document.querySelector('#preview [data-pf-page]').dataset.old = '1';"
                          "htmx.trigger('#preview', 'pf-refresh')")
            page.wait_for_selector("#preview [data-pf-page][data-pf-pages]:not([data-old])")
        assert not errors, errors
    finally:
        ctx.close()


_RO_PROBE_JS = """(() => {
  const seen = new Set();
  const obs = ResizeObserver.prototype.observe;
  const unobs = ResizeObserver.prototype.unobserve;
  ResizeObserver.prototype.observe = function (t, o) { seen.add(t); return obs.call(this, t, o); };
  ResizeObserver.prototype.unobserve = function (t) { seen.delete(t); return unobs.call(this, t); };
  window.__roBars = () => Array.from(seen).filter(e => e.hasAttribute &&
    e.hasAttribute('data-pf-savebar')).map(e => e.isConnected);
})();"""


def test_the_save_bar_watcher_lets_go_of_swapped_out_bars(live):
    """Each save swaps the form and its bar; the observer must not keep every old bar."""
    base, browser, master_id = live
    ctx = browser.new_context(viewport={"width": 1280, "height": 1300})
    page = ctx.new_page()
    page.add_init_script(_RO_PROBE_JS)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(_url(base, "about", master_id))
        page.wait_for_selector(".pf-tabs")
        wait_for_layout(page)
        for i in range(3):
            page.fill("#pa-head", f"Operations lead {i}")
            _save_and_wait(page)
            page.wait_for_timeout(300)
            assert page.evaluate("window.__roBars()") == [True], i
        assert not errors, errors
    finally:
        ctx.close()


def test_the_leave_guard_holds_after_a_remount_and_is_released_at_unmount(live):
    """window.kLeaveGuard is the page's while it is mounted: after an unmount and a mount again it
    still holds a link with unsaved changes (one nudge, the page stays); after an unmount it is
    gone, and the same link leaves."""
    ctx, page, errors = _open(live, "about", "light", 1680)
    try:
        page.evaluate("KUI.page.unmount()")
        assert page.evaluate("typeof window.kLeaveGuard") == "undefined"
        assert page.evaluate("KUI.page.mount()") is True
        assert page.evaluate("typeof window.kLeaveGuard") == "function"
        page.fill("#pa-head", "Operations lead")
        bar = page.locator("[data-pf-savebar]")
        assert bar.is_visible() and "Unsaved changes" in bar.inner_text()
        assert page.evaluate("window.kLeaveGuard()") is False
        page.click('#sidebar a[href="/opportunities"]')
        page.wait_for_timeout(400)
        assert "/profile" in page.url
        assert "before you leave this page" in bar.inner_text()
        page.evaluate("KUI.page.unmount()")
        assert page.evaluate("typeof window.kLeaveGuard") == "undefined"
        page.click('#sidebar a[href="/opportunities"]')
        page.wait_for_url("**/opportunities**")
        assert not errors, errors
    finally:
        ctx.close()


def test_the_documents_scripts_run_again_without_doubling_their_listeners(live):
    """D-UI-210: the Documents tab brings two inline scripts with its markup (the test render's
    link following the Look, and the import status). When a swap runs them again over the same
    markup, each control and the import section hold one set, nothing sits on the document, and
    one import request starts one seconds counter, which stops when its line leaves the page."""
    base, browser, master_id = live
    ctx = browser.new_context(viewport={"width": 1280, "height": 1300}, color_scheme="light")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(G4_LISTENER_PROBE_JS)
    page.add_init_script("""(() => {  // the one-second timers running now
      const live = new Set(), set = window.setInterval, clear = window.clearInterval;
      window.setInterval = function (fn, ms, ...args) {
        const id = set.call(window, fn, ms, ...args);
        if (ms === 1000) live.add(id);
        return id;
      };
      window.clearInterval = function (id) { live.delete(id); return clear.call(window, id); };
      window.__seconds = () => live.size;
    })();""")
    try:
        page.goto(_url(base, "documents", master_id))
        page.wait_for_selector("#pf-import")
        wait_for_layout(page)
        look = "#layout, #font, #header_align, #accent, #accent_custom"
        live_look = "sel => window.__g4Listeners.live(sel)"
        before = page.evaluate(live_look, look)
        assert before == 10, before  # change and input on each of the five controls
        assert page.evaluate("sel => window.__g4Listeners.live(sel, 'htmx:beforeRequest')",
                             "#pf-import") == 1

        def run_scripts_again() -> None:
            page.evaluate("""() => {
              for (const old of document.querySelectorAll('.pf-tb script, #pf-import + script')) {
                const again = document.createElement('script');
                again.textContent = old.textContent;
                old.after(again);
                old.remove();
              }
            }""")

        run_scripts_again()
        run_scripts_again()
        assert page.evaluate(live_look, look) == before
        assert page.evaluate("sel => window.__g4Listeners.live(sel, 'htmx:beforeRequest')",
                             "#pf-import") == 1
        assert page.evaluate("sel => window.__g4Listeners.live(sel, 'htmx:afterRequest')",
                             "#pf-import") == 1
        # The link still follows the Look once.
        page.evaluate("""() => { const f = document.getElementById('font');
          f.value = f.options[f.options.length - 1].value;
          f.dispatchEvent(new Event('change', { bubbles: true })); }""")
        href = page.get_attribute("#test-render", "href")
        assert href.count("&font=") == 1, href
        # One import request, one seconds counter; the answer ends it.
        fire = """([type, make]) => {
          let el = document.querySelector('.import-processing');
          if (make) {
            const box = document.getElementById('import-proposal');
            box.innerHTML = '<form class="import-processing"><span class="import-elapsed"></span>'
              + '</form>';
            el = box.firstElementChild;
          }
          el.dispatchEvent(new CustomEvent(type, { bubbles: true, detail: { elt: el } }));
        }"""
        assert page.evaluate("window.__seconds()") == 0
        page.evaluate(fire, ["htmx:beforeRequest", True])
        assert page.evaluate("window.__seconds()") == 1
        page.evaluate(fire, ["htmx:afterRequest", False])
        assert page.evaluate("window.__seconds()") == 0
        # A counter whose line left the page stops by itself on its next beat.
        page.evaluate(fire, ["htmx:beforeRequest", True])
        assert page.evaluate("window.__seconds()") == 1
        page.evaluate("document.getElementById('import-proposal').innerHTML = ''")
        page.wait_for_function("window.__seconds() === 0", timeout=3000)
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1200, 1280])
def test_documents_editor_and_preview_stay_side_by_side(live, theme, width):
    """D-UI-230: editor left, live preview right at 1200 and 1280; the delete action is a small
    danger button in the header row, not bare text; nothing scrolls sideways."""
    ctx, page, errors = _open(live, "documents", theme, width)
    try:
        _shot(page, f"docs-{width}-{theme}")
        m = page.evaluate("""() => {
          const r = s => document.querySelector(s).getBoundingClientRect();
          const del = document.querySelector('[data-testid=doc-delete-btn]');
          const d = del.getBoundingClientRect(), cs = getComputedStyle(del);
          return {main: r('.pf-dmain'), prev: r('.pf-dprev'), del: [d.width, d.height],
                  border: cs.borderTopWidth, bg: cs.backgroundColor,
                  sx: document.documentElement.scrollWidth - innerWidth}; }""")
        assert m["prev"]["left"] >= m["main"]["right"]
        assert abs(m["prev"]["top"] - m["main"]["top"]) <= 4
        label = page.query_selector(".pf-tb b")  # "Live preview" is not squeezed under a button
        assert label.evaluate("e => e.getBoundingClientRect().width >= e.scrollWidth - 1")
        boxes = page.evaluate(_BOXES, ".pf-tb > b, .pf-tb > .k-btn")
        assert not any(_overlaps(a, b) for i, a in enumerate(boxes) for b in boxes[i + 1:])
        assert m["sx"] <= 0 and m["del"][1] <= 40 and m["border"] == "1px"
        assert m["bg"] not in ("rgba(0, 0, 0, 0)", "transparent")
        assert errors == []
    finally:
        ctx.close()


_TEXT_UNDER_CUE_JS = """() => {
  const cues = Array.from(document.querySelectorAll('.k-cue--fixed.is-show'));
  const hits = [], rg = document.createRange();
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) {
    const t = n.nodeValue.trim(), el = n.parentElement;
    if (!t || !el || el.closest('.k-cue, script, style, noscript, [hidden]')) continue;
    rg.selectNodeContents(n);
    for (const b of rg.getClientRects()) {
      for (const c of cues) {
        const q = c.getBoundingClientRect();
        if (b.width < 1 || b.right <= q.left || b.left >= q.right || b.bottom <= q.top
            || b.top >= q.bottom) continue;
        const x = Math.max(b.left, q.left) + 1, y = Math.max(b.top, q.top) + 1;
        const seen = document.elementsFromPoint(
          Math.min(x, q.right - 1), Math.min(y, q.bottom - 1));
        if (seen.includes(el)) hits.push(t.slice(0, 30));
      }
    }
  }
  // A control or image under a shown cue is covered content too, not only text.
  const sel = 'a, button, input, select, textarea, label, img, svg, video, iframe, '
    + '[role="button"]';
  for (const el of document.querySelectorAll(sel)) {
    if (el.closest('.k-cue, [hidden]')) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.pointerEvents === 'none') {
      continue;
    }
    for (const b of el.getClientRects()) {
      for (const c of cues) {
        const q = c.getBoundingClientRect();
        if (b.width < 1 || b.height < 1 || b.right <= q.left || b.left >= q.right
            || b.bottom <= q.top || b.top >= q.bottom) continue;
        const x = Math.min(Math.max(b.left, q.left) + 1, q.right - 1);
        const y = Math.min(Math.max(b.top, q.top) + 1, q.bottom - 1);
        if (document.elementsFromPoint(x, y).includes(el)) {
          hits.push('<' + el.tagName.toLowerCase() + '> ' + (el.className.baseVal
            ?? el.className ?? '').toString().slice(0, 30));
        }
      }
    }
  }
  return {shown: cues.length, hits};
}"""


def test_the_scroll_cue_never_sits_over_text_or_a_control_on_documents(live):
    """The shared fixed scroll cue steps aside (or stays hidden) where text, a button, a field,
    a link or an image sits under it: at 1280 it used to cover a role card's "2 bullets" line."""
    base, browser, master_id = live
    _grow_resume(master_id, 12)
    ctx = browser.new_context(viewport={"width": 1280, "height": 760})
    page = ctx.new_page()
    try:
        page.goto(_url(base, "documents", master_id))
        page.wait_for_selector("#preview [data-pf-page] iframe")
        wait_for_layout(page)
        total = page.evaluate("document.documentElement.scrollHeight - innerHeight")
        assert total > 600
        seen_cue = 0
        for y in range(0, int(total) + 1, 120):
            page.evaluate("y => window.scrollTo(0, y)", y)
            page.wait_for_timeout(900)  # the cue shows again once the page has been still
            got = page.evaluate(_TEXT_UNDER_CUE_JS)
            seen_cue += got["shown"]
            assert got["hits"] == [], (y, got)
        assert seen_cue > 0, "the cue never showed, so the check proved nothing"
    finally:
        ctx.close()


_COUNT_SCANS_JS = """() => {
  window.__cueScans = 0;
  const real = Document.prototype.elementsFromPoint;
  document.elementsFromPoint = function (...a) { window.__cueScans++; return real.apply(this, a); };
}"""


def test_the_scroll_cue_does_not_rescan_while_the_page_is_idle(live):
    """The cue looks under itself only after a scroll ends, a resize or a change to the content
    (the spy counts elementsFromPoint, which every look uses): seven idle seconds cost none."""
    base, browser, master_id = live
    _grow_resume(master_id, 12)
    ctx = browser.new_context(viewport={"width": 1280, "height": 760})
    page = ctx.new_page()
    try:
        page.goto(_url(base, "documents", master_id))
        page.wait_for_selector("#preview [data-pf-page] iframe")
        wait_for_layout(page)
        page.evaluate("window.scrollTo(0, 200)")  # both cues may show: the scan has work to do
        page.wait_for_timeout(1500)
        page.evaluate(_COUNT_SCANS_JS)
        page.wait_for_timeout(7000)
        assert page.evaluate("window.__cueScans") == 0, "the cue rescanned on a timer"
        # It still looks when something real happens, so the zero above means something.
        page.evaluate("window.scrollTo(0, 320)")
        page.wait_for_timeout(1200)
        after_scroll = page.evaluate("window.__cueScans")
        assert after_scroll > 0, "no look after a scroll"
        page.evaluate("""() => { const p = document.createElement('p'); p.textContent = 'new';
                        document.querySelector('.k-main').appendChild(p); }""")
        page.wait_for_timeout(800)
        assert page.evaluate("window.__cueScans") > after_scroll, "no look after a content change"
    finally:
        ctx.close()
