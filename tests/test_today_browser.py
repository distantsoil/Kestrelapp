"""Today in a real browser (UI overhaul parity check against the locked mockup,
the locked design mockup (kept privately)). Light and dark at 1280 and 1680: no overlap,
no wrapping titles or buttons, no sideways scroll, the carousel's peek and fixed card height, the
strip stacked below 1300px of content; then the interactions the mockup demos (Shortlist and Save
remove a card, Review slides the summary in centered, a new match opens a gap at its ranked
position, the walkthrough over the real sidebar, Undo brings a card back).

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1. The browser is the Edge
already on the machine, else a Playwright Chromium (tests/browser.py). Set KESTREL_SHOT_DIR to a
folder to also save a screenshot per case."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db.engine import forget_engine, make_async_engine
from kestrel.db.models import Evaluation, Opportunity, Org
from tests import today_seed
from tests.browser import LINES_JS, launch_sync, wait_for_layout

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

_LINES = LINES_JS

_BOX1 = """sel => { const b = document.querySelector(sel).getBoundingClientRect();
  return {l: b.left, r: b.right, tp: b.top, bt: b.bottom, w: b.width, h: b.height}; }"""

_BOXES = """sel => Array.from(document.querySelectorAll(sel)).filter(e => e.offsetParent !== null)
  .map(e => { const b = e.getBoundingClientRect();
    return {t: (e.textContent || '').trim().slice(0, 30), l: b.left, r: b.right, tp: b.top,
            bt: b.bottom, w: b.width, h: b.height}; })"""


def _overlaps(a, b) -> bool:
    return a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1 and a["tp"] < b["bt"] - 1 \
        and b["tp"] < a["bt"] - 1


@pytest.fixture
def live(monkeypatch):
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    ids = asyncio.run(today_seed.seed())
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, ids
        browser.close()


def _page(live, theme: str, width: int, path: str = "/", height: int = 1000):
    base, browser, _ids = live
    ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}{path}")
    page.wait_for_selector('[data-testid="td-card"]')
    wait_for_layout(page)
    return ctx, page, errors


def _shot(page, name: str) -> None:
    out = os.environ.get("KESTREL_SHOT_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(out) / f"{name}.png"), full_page=False)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_no_overlap_no_wrapping_no_sideways_scroll(live, theme, width):
    ctx, page, errors = _page(live, theme, width)
    try:
        _shot(page, f"today-{theme}-{width}")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        assert page.evaluate("document.querySelector('.main, main').scrollWidth <= "
                             "document.querySelector('.main, main').clientWidth + 1") is True
        # One line each: the heading, the strip's words, pills, buttons, the card's lines.
        for sel in ("h1", ".td-lede", ".td-k", ".k-pill", ".td-btns .k-btn",
                    ".td-item .k-btn", ".td-seg .k-link", ".td-arr"):
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    assert page.evaluate(_LINES, el) <= 1, (sel, theme, width, el.inner_text()[:40])
        # A score and a pay line are one line tall (their number and unit differ in size).
        for sel in (".td-score", ".td-pay"):
            for box in page.evaluate(_BOXES, sel):
                assert box["h"] <= 28, (sel, box, theme, width)
        # The section heading and its count line share one line (a taller box means it wrapped).
        assert page.evaluate(_BOXES, ".td-head h3")[0]["h"] <= 34
        # No two siblings overlap in a row of controls, a card's header or the strip.
        for container in (".td-strip", ".td-arrows", ".td-btns", ".td-row", ".td-item"):
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
                        assert not _overlaps(a, b), (container, a, b, theme, width)
        # The strip: three columns on a wide window, stacked at 1300px of content or less.
        segs = page.evaluate(_BOXES, ".td-seg")
        assert len(segs) == 3
        if width >= 1680:
            assert abs(segs[0]["tp"] - segs[1]["tp"]) < 2 and segs[1]["l"] >= segs[0]["r"] - 1
        else:
            assert segs[1]["tp"] >= segs[0]["bt"] - 1 and abs(segs[0]["l"] - segs[1]["l"]) < 2
        # The lower panels sit side by side at both widths (the mockup's 1100px step).
        lower = page.evaluate(_BOXES, ".td-panel")
        assert len(lower) == 2 and abs(lower[0]["tp"] - lower[1]["tp"]) < 2
        # The scene art stays inside the band and the greeting does not sit on it.
        title = page.evaluate(_BOXES, ".k-scene__title")[0]
        art = page.evaluate(_BOXES, ".k-scene__art")
        if art:
            assert title["r"] <= art[0]["l"] + 80, (title, art[0])
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("width", [1280, 1680])
def test_the_carousel_shows_whole_cards_with_the_next_one_peeking(live, width):
    ctx, page, _ = _page(live, "light", width)
    try:
        vp = page.evaluate(_BOXES, "#td-vp")[0]
        cards = page.evaluate(_BOXES, ".td-card")
        assert len(cards) == 6
        # Every card has one height and a width of about 270 to 300 px.
        assert len({round(c["h"]) for c in cards}) == 1, [c["h"] for c in cards]
        assert all(255 <= c["w"] <= 320 for c in cards), [c["w"] for c in cards]
        inside = [c for c in cards if c["l"] >= vp["l"] - 1 and c["r"] <= vp["r"] + 1]
        peeking = [c for c in cards if c["r"] > vp["r"] + 1 and c["l"] < vp["r"]]
        assert inside and peeking, (width, len(inside), len(peeking))
        # A wider window shows more cards.
        assert len(inside) == (4 if width == 1680 else 3), (width, len(inside))
        # Arrows, keys and the page dots move it; the previous arrow starts disabled.
        assert page.get_by_test_id("td-prev").is_disabled()
        first = cards[0]["l"]
        page.get_by_test_id("td-next").click()
        wait_for_layout(page)
        assert page.evaluate(_BOXES, ".td-card")[0]["l"] < first - 100
        page.keyboard.press("ArrowLeft")
        wait_for_layout(page)
        assert abs(page.evaluate(_BOXES, ".td-card")[0]["l"] - first) < 2
        dots = page.query_selector_all("#td-dots i")
        assert len(dots) >= 2
        dots[1].click()
        wait_for_layout(page)
        assert page.evaluate(_BOXES, ".td-card")[0]["l"] < first - 100
        # The last dot is the end of the strip: Next is disabled there and Previous is not.
        dots[-1].click()
        wait_for_layout(page)
        assert page.get_by_test_id("td-next").is_disabled()
        assert not page.get_by_test_id("td-prev").is_disabled()
    finally:
        ctx.close()


def test_dragging_moves_the_carousel_and_never_presses_the_button_under_it(live):
    ctx, page, _ = _page(live, "light", 1680)
    try:
        before = page.evaluate(_BOXES, ".td-card")[0]["l"]
        btn = page.query_selector(".td-card .td-sl")
        box = btn.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        # Start the drag on the card's body (not on a button), end it over a button.
        card = page.query_selector(".td-card").bounding_box()
        sx, sy = card["x"] + 40, card["y"] + 60
        page.mouse.move(sx, sy)
        page.mouse.down()
        page.mouse.move(sx - 120, sy, steps=6)
        page.mouse.move(x - 500, y, steps=6)
        page.mouse.up()
        wait_for_layout(page)
        assert page.evaluate(_BOXES, ".td-card")[0]["l"] < before - 100
        assert page.locator(".td-card").count() == 6  # nothing was pressed
    finally:
        ctx.close()


def test_shortlist_and_save_remove_the_card_and_the_list_catches_up(live):
    ctx, page, errors = _page(live, "light", 1680)
    try:
        first = page.query_selector(".td-card").get_attribute("data-id")
        page.locator(f'.td-card[data-id="{first}"] .td-sl').click()
        page.wait_for_function(
            f"!document.querySelector('.td-card[data-id=\"{first}\"]')", timeout=5000)
        assert page.locator(".td-card").count() == 5
        # The shortlisted role shows in Decisions waiting without a page reload.
        page.wait_for_function(
            "document.querySelector('#td-decisions').textContent.includes("
            "'Senior Operations Analyst')", timeout=5000)
        assert page.get_by_test_id("dw-count").inner_text() == "3"
        # Save for later removes the next card too (it is not a decision).
        second = page.query_selector(".td-card").get_attribute("data-id")
        page.locator(f'.td-card[data-id="{second}"] .td-save').click()
        page.wait_for_function(
            f"!document.querySelector('.td-card[data-id=\"{second}\"]')", timeout=5000)
        assert page.get_by_test_id("dw-count").inner_text() == "3"
        # The toast offers Undo; it brings the card back.
        page.get_by_text("Undo").first.click()
        page.wait_for_function("document.querySelectorAll('.td-card').length >= 5", timeout=6000)
        assert not errors, errors
    finally:
        ctx.close()


def test_review_slides_the_summary_in_centered_and_never_decides(live):
    ctx, page, _ = _page(live, "light", 1680)
    try:
        first = page.query_selector(".td-card").get_attribute("data-id")
        page.locator(f'.td-card[data-id="{first}"] [data-td-review]').click()
        page.wait_for_selector("#td-drawer.is-open", timeout=5000)
        wait_for_layout(page)
        _shot(page, "today-drawer")
        d = page.evaluate(_BOX1, "#td-drawer")
        mid = d["tp"] + d["h"] / 2
        assert abs(mid - 500) < 30, d  # vertically centered in the window, never pinned to the top
        assert d["r"] >= 1679 and 480 <= d["w"] <= 540
        assert "role summary" in page.locator("#td-drawer").inner_text().lower()
        # The footer's three actions sit on one line each.
        for b in page.query_selector_all("#td-drawer .k-slideover__foot .k-btn"):
            assert page.evaluate(_LINES, b) <= 1
        page.keyboard.press("Escape")
        page.wait_for_function("!document.querySelector('#td-drawer.is-open')", timeout=3000)
        assert page.locator(".td-card").count() == 6  # Review alone did not remove it
    finally:
        ctx.close()


def test_a_new_match_opens_a_gap_at_its_ranked_position_and_the_view_stays_put(live):
    base, browser, ids = live
    ctx, page, errors = _page(live, "light", 1680)
    try:
        page.get_by_test_id("td-next").click()
        wait_for_layout(page)
        # The card at the viewport's left edge is the one the operator is looking at.
        anchor = page.evaluate(
            """() => { const vp = document.querySelector('#td-vp').getBoundingClientRect();
              return [...document.querySelectorAll('.td-card')].find(
                c => c.getBoundingClientRect().left >= vp.left - 1).dataset.id; }""")
        left = page.evaluate(
            f"document.querySelector('.td-card[data-id=\"{anchor}\"]').getBoundingClientRect().left")

        async def add():
            engine = make_async_engine(paths.db_path())
            sm = async_sessionmaker(engine, expire_on_commit=False)
            async with sm() as s:
                org = (await s.get(Org, 1))
                opp = Opportunity(
                    track="employment", opp_type="job_posting", org_id=org.id, org_name=org.name,
                    title="Continuous Improvement Manager", url="https://example.test/new",
                    stage="t2_done", first_seen_at=today_seed._local_midnight_safe(timedelta()),
                    location_raw="Remote (US)", remote_type="remote", comp_min=132000,
                    comp_max=150000, comp_currency="USD", comp_interval="year",
                    comp_source="posted", description_md="Lean and reporting.",
                )
                s.add(opp)
                await s.flush()
                s.add(Evaluation(opportunity_id=opp.id, tier=2, track="employment",
                                 verdict="apply", verdict_model="apply", score=4.5,
                                 weighted_score=4.5, blocks={}))
                await s.commit()
            await engine.dispose()
            forget_engine()

        with ThreadPoolExecutor(1) as pool:  # Playwright's sync API owns this thread's loop
            pool.submit(asyncio.run, add()).result()
        page.evaluate("window.TodayPage.pollNew()")
        page.wait_for_selector('.td-card:has-text("Continuous Improvement Manager")', timeout=6000)
        page.wait_for_function("!document.querySelector('.td-card.is-grow')")
        wait_for_layout(page)
        order = page.eval_on_selector_all(".td-t", "els => els.map(e => e.textContent.trim())")
        # 4.5 ranks below 4.6 and above 4.4: second from the left.
        names = [x.removesuffix("NEW") for x in order]
        assert names.index("Continuous Improvement Manager") == 1, order
        assert page.locator(".td-new").count() == 1
        # The card the operator was looking at did not move.
        assert abs(page.evaluate(
            f"document.querySelector('.td-card[data-id=\"{anchor}\"]').getBoundingClientRect().left"
        ) - left) < 2
        assert "found today" in page.locator("#td-sub").inner_text()
        assert not errors, errors
    finally:
        ctx.close()


def test_the_walkthrough_spotlights_the_real_sidebar_and_cards(live):
    ctx, page, errors = _page(live, "light", 1680, "/?tour=1")
    try:
        page.wait_for_selector("#td-tour.is-on", timeout=5000)
        seen = []
        for _ in range(6):
            seen.append(page.locator("#td-tc h4").inner_text())
            spot = page.evaluate(_BOXES, "#td-spot")
            page.locator('#td-tc [data-td-tour="next"]').click()
            wait_for_layout(page)
            box = spot[0] if spot else None
            assert box and box["r"] - box["l"] > 20 and box["bt"] - box["tp"] > 20, \
                ("the spotlight has no box", seen[-1], spot)
        assert seen == ["Today is your daily start", "Every role Kestrel finds",
                        "Your top matches", "See why a role fits", "Teach Kestrel your taste",
                        "Track what you applied to"]
        assert page.locator("#td-tc h4").inner_text() == "That's it"
        _shot(page, "today-tour-last")
        page.locator('#td-tc [data-td-tour="next"]').click()
        page.wait_for_function("document.querySelector('#td-tour').hidden", timeout=3000)
        assert not errors, errors
        # Step two highlights the REAL Pipeline link in the sidebar.
        page.evaluate("window.KTour.start(1)")
        wait_for_layout(page)
        spot = page.evaluate(_BOXES, "#td-spot")[0]
        link = page.evaluate(_BOXES, 'nav.mainnav a[href="/opportunities"]')[0]
        assert abs(spot["l"] - (link["l"] - 6)) < 3 and abs(spot["tp"] - (link["tp"] - 6)) < 3
    finally:
        ctx.close()


def test_the_strip_hover_follows_the_strip_corner_and_keeps_inner_edges_square(live):
    ctx, page, _ = _page(live, "light", 1680)
    try:
        strip_r = page.evaluate(
            "parseFloat(getComputedStyle(document.querySelector('.td-strip')).borderTopLeftRadius)")
        assert strip_r >= 12
        for seg in page.query_selector_all(".td-seg"):
            radii = page.evaluate(
                "e => { const s = getComputedStyle(e); return [s.borderTopLeftRadius, "
                "s.borderTopRightRadius, s.borderBottomLeftRadius, s.borderBottomRightRadius]; }",
                seg)
            assert all(float(r.replace("px", "")) == 0 for r in radii), radii
        assert page.evaluate(
            "getComputedStyle(document.querySelector('.td-strip')).overflow") == "hidden"
        page.hover('[data-seg="found"]')
        wait_for_layout(page)
        assert page.evaluate(
            "getComputedStyle(document.querySelector('[data-seg=\"found\"]')).backgroundColor"
        ) != "rgba(0, 0, 0, 0)"
    finally:
        ctx.close()


def test_the_estimate_label_shows_its_disclaimer_on_hover(live):
    ctx, page, _ = _page(live, "light", 1680)
    try:
        est = page.locator('[data-testid="td-est"]').first
        est.scroll_into_view_if_needed()
        est.hover()
        page.wait_for_function("""() => [...document.querySelectorAll('.td-tip')].some(
            element => Number(getComputedStyle(element).opacity) > 0.9)""")
        tip = est.locator(".td-tip")
        assert float(tip.evaluate("e => getComputedStyle(e).opacity")) > 0.9
        assert "may be wrong" in tip.inner_text()
    finally:
        ctx.close()


def test_undo_of_a_shortlist_takes_the_role_out_of_decisions_again(live):
    ctx, page, errors = _page(live, "light", 1680)
    try:
        first = page.query_selector(".td-card").get_attribute("data-id")
        page.locator(f'.td-card[data-id="{first}"] .td-sl').click()
        page.wait_for_function(
            "document.querySelector('#td-decisions').textContent.includes("
            "'Senior Operations Analyst')", timeout=5000)
        page.get_by_text("Undo").first.click()
        page.wait_for_function(
            "!document.querySelector('#td-decisions').textContent.includes("
            "'Senior Operations Analyst')", timeout=6000)
        assert not errors, errors
    finally:
        ctx.close()


def test_a_one_line_title_leaves_three_lines_for_the_why_text(live):
    ctx, page, _ = _page(live, "light", 1680)
    try:
        wl = page.evaluate(
            "[...document.querySelectorAll('.td-card')].map(c => ["
            "c.querySelector('.td-t').scrollHeight, c.style.getPropertyValue('--wl')])")
        assert wl and all(v == "3" for _h, v in wl), wl
        # Cards that are off screen cannot take focus (the viewport scrolls natively otherwise).
        assert page.locator(".td-card[inert]").count() >= 1
    finally:
        ctx.close()
