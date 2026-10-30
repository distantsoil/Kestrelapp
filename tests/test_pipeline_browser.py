"""Pipeline in a real browser (UI overhaul parity check against the locked mockup,
the locked design mockup (kept privately)). Light and dark at 1280 and 1680: no
overlap, no wrapped titles or buttons, no horizontal scroll; then the interactions the mockup
demos (the panel slides in with its own URL, j / k / Enter / x / Esc, Not for me because... with
Undo, the checkboxes and the bulk bar, Show 25 more, Keep it anyway, Hide company with Undo, and
Consulting's tabs).

Gated like the other live-browser design-system tests: set KESTREL_LIVE_BROWSER=1. The browser is
the Edge already on the machine, else a Playwright Chromium (tests/browser.py)."""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests import test_pipeline_view as T
from tests.browser import (
    LINES_JS,
    TIMER_PROBE_JS,
    assert_page_remounts_clean,
    launch_sync,
    wait_for_layout,
)
from tests.test_pipeline_screen import _signals

pytestmark = [
    pytest.mark.usefixtures("_isolate_data_home"),
    pytest.mark.skipif(
        os.environ.get("KESTREL_LIVE_BROWSER") != "1",
        reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
    ),
]

_LINES = LINES_JS

_BOXES = """sel => Array.from(document.querySelectorAll(sel)).filter(e => e.offsetParent !== null)
  .map(e => { const b = e.getBoundingClientRect();
    return {t: (e.textContent || '').trim().slice(0, 30), l: b.left, r: b.right, tp: b.top,
            bt: b.bottom}; })"""


# A button or chip that wrapped is twice as tall as one line.
_SHORT = "el => el.getBoundingClientRect().height < 44"


def _overlaps(a, b) -> bool:
    return a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1 and a["tp"] < b["bt"] - 1 \
        and b["tp"] < a["bt"] - 1


async def _extras(s):
    """Mockup-like variety: a Maybe, an estimated pay, a ghost warning, a long title, 30 more roles
    in Worth a look, and twelve-plus rule rejects."""
    org = (await s.execute(select(T.Org))).scalars().first()
    src = (await s.execute(select(T.Source))).scalars().first()
    rows = []
    for i in range(30):
        opp = T._opp(org, src, f"w{i}", title=f"Operations Program Manager {i}",
                     comp_source="inferred" if i == 2 else "parsed",
                     ghost_score=0.8 if i == 1 else 0.0,
                     first_seen_at=T.iso(T._now() - timedelta(hours=i + 1)))
        s.add(opp)
        rows.append(opp)
    long = T._opp(org, src, "long", title="Senior Director of Enterprise Operations Excellence "
                  "and Continuous Improvement for the Americas Region")
    maybe = T._opp(org, src, "maybe", title="Inventory Planning Manager")
    s.add_all([long, maybe] + [
        T._opp(org, src, f"nf{i}", stage="t0_reject", stage_reason="blocker:anti_title: help desk")
        for i in range(14)
    ])
    await s.flush()
    # Scored below the two seeded roles, so those lead Worth a look on the first page of 25.
    s.add_all([T._ev(r, score=3.0 + (i % 10) / 20) for i, r in enumerate(rows)])
    s.add(T._ev(long, score=3.5))
    s.add(T._ev(maybe, score=3.3, verdict="consider", verdict_model="consider"))
    await s.commit()


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    ids = asyncio.run(T._seed())
    asyncio.run(T._with_session(_extras))
    sig = asyncio.run(T._with_session(_signals))
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, ids, sig
        browser.close()


def _page(live, theme: str, width: int, path: str = "/opportunities"):
    base, browser, _ids, _sig = live
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}{path}")
    page.wait_for_selector('[data-testid="pl-row"]')
    wait_for_layout(page)
    return ctx, page, errors


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_parity_no_overlap_no_wrapping_no_sideways_scroll(live, theme, width):
    ctx, page, errors = _page(live, theme, width)
    try:
        # No horizontal scroll, anywhere on the page.
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert page.evaluate(
            "(() => { const m = document.querySelector('.pl-wrap'); "
            "return m.scrollWidth <= m.clientWidth + 1; })()")
        # A title is one line in the list at these widths (the long one wraps by design: it is
        # longer than the row), and a button or chip never wraps.
        for el in page.query_selector_all(".pl-row .pl-title__t"):
            text = (el.inner_text() or "").strip()
            if len(text) < 60:
                assert el.evaluate(_LINES) == 1, text
        for sel in (".pl-chips .k-chip", ".pl-sortg .k-seg button", ".pl-foot .k-btn"):
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    assert el.evaluate(_SHORT), (sel, el.inner_text())
        # Row parts do not overlap: the monogram, the text and the score sit side by side.
        for row in page.query_selector_all(".pl-row")[:6]:
            parts = row.evaluate(
                "r => ['.pl-mono', '.pl-main', '.pl-end'].map(s => { const e = r.querySelector(s);"
                " const b = e.getBoundingClientRect();"
                " return {l: b.left, r: b.right, tp: b.top, bt: b.bottom, t: s}; })")
            for i, a in enumerate(parts):
                for b in parts[i + 1:]:
                    assert not _overlaps(a, b), (a["t"], b["t"])
        # Open a panel: its three footer buttons are visible and do not overlap.
        page.click(f"#row-{live[2]['b']} .pl-title__t")
        page.wait_for_selector("#pl-panel.is-open [data-kind=role]")
        wait_for_layout(page)
        boxes = page.evaluate(_BOXES, ".pl-foot__a .k-btn")
        assert len(boxes) == 3
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                assert not _overlaps(a, b), (a["t"], b["t"])
        # The panel fits in the window: its footer is on screen and the body scrolls inside it.
        foot = page.evaluate(
            "(() => { const b = document.querySelector('#pl-foot').getBoundingClientRect();"
            " return [b.top, b.bottom, innerHeight]; })()")
        assert 0 <= foot[0] and foot[1] <= foot[2] + 1, foot
        # Close is the rightmost control of the header.
        head = page.evaluate(_BOXES, ".pl-dh .pl-xbtn")
        assert head[-1]["r"] >= max(h["r"] for h in head) - 1
        assert errors == []
    finally:
        ctx.close()


def test_the_panel_has_its_own_url_and_keys_move_it(live):
    base, _browser, ids, _sig = live
    ctx, page, errors = _page(live, "light", 1680)
    try:
        page.click(f"#row-{ids['a']} .pl-title__t")
        page.wait_for_selector(f"#pl-panel [data-id='{ids['a']}']")
        assert f"open={ids['a']}" in page.url
        # j moves to the next row and the panel follows; k goes back; Escape closes it.
        page.keyboard.press("j")
        page.wait_for_function(
            "id => { const p = document.querySelector('#pl-panel .pl-panel__in');"
            " return p && p.dataset.id !== id; }", arg=str(ids["a"]))
        page.keyboard.press("k")
        page.wait_for_selector(f"#pl-panel [data-id='{ids['a']}']")
        page.keyboard.press("Escape")
        page.wait_for_function("() => !document.querySelector('#pl-panel.is-open')")
        assert "open=" not in page.url
        # Back reopens it, and a pasted link opens the page with it open.
        page.go_back()
        page.wait_for_selector("#pl-panel.is-open")
        deep = page.context.new_page()
        deep.goto(f"{base}/opportunities?open={ids['b']}")
        deep.wait_for_selector(f"#pl-panel.is-open [data-id='{ids['b']}']")
        assert deep.is_visible("#pl-panel .pl-ring")
        # x picks the selected row; Escape clears it first.
        page.keyboard.press("x")
        page.wait_for_selector("#pl-bulk.is-on")
        assert page.inner_text("#pl-bcount") == "1 selected"
        page.keyboard.press("Escape")
        page.wait_for_function("() => !document.querySelector('#pl-bulk.is-on')")
        assert errors == []
    finally:
        ctx.close()


def test_not_for_me_asks_why_in_place_and_undo_brings_the_role_back(live):
    _base, _browser, ids, _sig = live
    ctx, page, errors = _page(live, "light", 1680)
    try:
        page.click(f"#row-{ids['b']} .pl-title__t")
        page.wait_for_selector("[data-testid=panel-not-for-me]")
        page.click("[data-testid=panel-not-for-me]")
        page.wait_for_selector("#pl-foot.is-ask")
        assert page.is_visible("#pl-foot .pl-rgrid button >> nth=3")
        page.click("#pl-foot [data-why=pay_place]")
        page.wait_for_selector(".k-toast .k-toast__undo")
        page.wait_for_function(
            "id => !document.querySelector('#row-' + id)", arg=str(ids["b"]))
        assert "open=" not in page.url or str(ids["b"]) not in page.url
        page.click(".k-toast__undo")
        page.wait_for_selector(f"#row-{ids['b']}")
        assert errors == []
    finally:
        ctx.close()


def test_checkboxes_the_bar_and_shift_select(live):
    _base, _browser, ids, _sig = live
    ctx, page, errors = _page(live, "dark", 1280)
    try:
        page.hover(".pl-row >> nth=0")
        page.click(".pl-row >> nth=0 >> .pl-ck")
        page.click(".pl-row >> nth=3 >> .pl-title__t", modifiers=["Shift"])
        page.wait_for_selector("#pl-bulk.is-on")
        assert page.inner_text("#pl-bcount") == "4 selected"
        bar = page.evaluate(_BOXES, "#pl-bulk button")
        assert len(bar) >= 5
        before_tags = page.locator("[data-testid=on-shortlist]").count()
        # One line, not wrapped: a row of buttons is about 50px tall, two rows twice that.
        assert page.query_selector("#pl-bulk").evaluate(
            "el => el.getBoundingClientRect().height < 64")
        # Shortlist from the bar: rows gain the tag, the bar closes, Undo takes them off.
        page.click("#pl-bulk [data-bulk=shortlist]")
        page.wait_for_selector(".k-toast .k-toast__undo")
        page.wait_for_function("() => !document.querySelector('#pl-bulk.is-on')")
        page.wait_for_function(
            "() => document.querySelectorAll('[data-testid=on-shortlist]').length >= 3")
        # Undo takes the tag off exactly the rows the bar shortlisted.
        tagged = page.locator("[data-testid=on-shortlist]").count()
        page.click(".k-toast .k-toast__undo")
        page.wait_for_function(
            "n => document.querySelectorAll('[data-testid=on-shortlist]').length < n", arg=tagged)
        assert page.locator("[data-testid=on-shortlist]").count() == before_tags
        assert errors == []
    finally:
        ctx.close()


def test_a_group_pages_25_at_a_time_and_not_a_fit_opens_in_its_own_box(live):
    _base, _browser, ids, _sig = live
    ctx, page, errors = _page(live, "light", 1680)
    try:
        before = page.locator("section[data-group=worth] [data-testid=pl-row]").count()
        assert before == 25
        page.click("[data-testid=show-more-worth]")
        page.wait_for_function(
            "() => document.querySelectorAll('section[data-group=worth] [data-testid=pl-row]')"
            ".length > 25")
        assert "lim_worth=50" in page.url
        page.click("[data-testid=not-fit-show]")
        page.wait_for_selector("#not-fit-box .pl-row")
        assert page.locator("#not-fit-box .pl-row").count() == 12
        # Its own box: capped at 300px and scrolling inside, so 12 rows overflow it.
        box = page.evaluate("""() => { const b = document.querySelector('#not-fit-box');
          return {oy: getComputedStyle(b).overflowY, ch: b.clientHeight, sh: b.scrollHeight}; }""")
        assert box["oy"] == "auto" and box["ch"] <= 300 and box["sh"] > box["ch"], box
        page.click("[data-testid=not-fit-more]")
        page.wait_for_function(
            "() => document.querySelectorAll('#not-fit-box .pl-row').length > 12")
        assert errors == []
    finally:
        ctx.close()


def test_keep_it_anyway_and_hide_company_with_undo(live):
    _base, _browser, ids, _sig = live
    ctx, page, errors = _page(live, "light", 1680)
    try:
        page.click("[data-testid=not-fit-show]")
        page.wait_for_selector("#not-fit-box [data-testid=keep-anyway]")
        first = page.locator("#not-fit-box .pl-row:has([data-testid=keep-anyway])").first
        rid = first.get_attribute("data-id")
        first.locator("[data-testid=keep-anyway]").click()
        page.wait_for_selector(".k-toast")
        page.wait_for_function("id => !document.querySelector('#row-' + id)", arg=rid)
        # Hide this company from a row's menu: the roles leave, Undo returns them.
        row = page.locator(f"#row-{ids['a']}")
        row.hover()
        row.locator("summary").click()
        row.locator("[data-testid=hide-company]").click()
        undo = page.locator(".k-toast", has_text="hidden. Its roles leave Pipeline.").locator(
            ".k-toast__undo")
        undo.hover()
        # The Keep-it-anyway toast (with its own Undo) may still be on screen: wait for this
        # action's effect, then press the newest Undo, never the first one that matches.
        page.wait_for_function("id => !document.querySelector('#row-' + id)", arg=str(ids["b"]))
        undo.click()
        page.wait_for_selector(f"#row-{ids['b']}")
        assert errors == []
    finally:
        ctx.close()


def test_consulting_tabs_glide_and_the_signal_panel_adds_a_lead(live):
    _base, _browser, ids, sig = live
    ctx, page, errors = _page(live, "light", 1680, "/opportunities?track=consulting")
    try:
        assert page.is_visible("[data-testid=tab-signals]")
        page.click("[data-testid=tab-signals]")
        page.wait_for_selector("[data-testid=pl-signal]")
        assert page.evaluate("parseFloat(document.querySelector('.pl-tabs__ink').style.width) > 0")
        assert page.locator("[data-testid=pl-signal]").count() == 3
        page.click(f"#row-{sig['sig1']} .pl-title__t")
        page.wait_for_selector("#pl-panel [data-kind=signal]")
        page.click("[data-pl-lead]")
        page.wait_for_selector("#pl-foot.is-lead")
        page.click("#pl-foot .pl-due [data-due=today]")
        page.click("#pl-foot form button[type=submit]")
        page.wait_for_selector(f"#row-{sig['sig1']} [data-testid=in-leads]")
        assert errors == []
    finally:
        ctx.close()


def test_the_page_mounts_unmounts_and_mounts_again_and_keeps_its_history(live):
    """The page lifecycle (D-UI-210): Pipeline unmounted and mounted again in place starts exactly
    what it started on the document load and leaves nothing running in between (the scroll-cue
    timer above all). After it, one j moves one row, not two, and the panel's own history still
    works: a row pushes ?open=<id> with its {pl: id} state, Back closes it, Forward reopens it."""
    base, browser, ids, _sig = live
    ctx = browser.new_context(viewport={"width": 1680, "height": 900}, color_scheme="light")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(TIMER_PROBE_JS)
    try:
        page.goto(f"{base}/opportunities")
        page.wait_for_selector('[data-testid="pl-row"]')
        wait_for_layout(page)
        first = assert_page_remounts_clean(page, "pipeline", "pipeline.js", settle_ms=800)
        assert first["timers"] == 1  # the scroll-cue refresh
        before = page.evaluate("history.length")
        page.click(f"#row-{ids['a']} .pl-title__t")
        page.wait_for_selector(f"#pl-panel [data-id='{ids['a']}']")
        assert f"open={ids['a']}" in page.url
        state = page.evaluate("history.state")
        # The panel's own entry, which the navigation controller files as the page's (D-UI-220).
        assert state["pl"] == ids["a"] and state["kNav"]["kind"] == "page", state
        assert page.evaluate("history.length") == before + 1
        order = page.evaluate("[...document.querySelectorAll('#pipeline-list .pl-row[data-id]')]"
                              ".filter(r => r.offsetParent).map(r => Number(r.dataset.id))")
        page.keyboard.press("j")
        want = order[order.index(ids["a"]) + 1]
        page.wait_for_selector(f"#pl-panel .pl-panel__in[data-id='{want}']")
        assert f"open={want}" in page.url
        page.go_back()
        page.wait_for_selector(f"#pl-panel .pl-panel__in[data-id='{ids['a']}']")
        page.go_back()
        page.wait_for_function("() => !document.querySelector('#pl-panel.is-open')")
        assert "open=" not in page.url
        page.go_forward()
        page.wait_for_selector("#pl-panel.is-open")
        # Unmounted, the page no longer answers Back: the URL changes and the panel stays.
        page.evaluate("KUI.page.unmount()")
        page.go_back()
        page.wait_for_function("() => !location.search.includes('open=')")
        page.wait_for_timeout(300)
        assert page.is_visible("#pl-panel.is-open")
        assert page.evaluate(
            "document.documentElement.style.getPropertyValue('--pl-bulk-left')") == ""
        assert errors == []
    finally:
        ctx.close()
