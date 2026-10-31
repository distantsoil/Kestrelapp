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
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db.engine import forget_engine, make_async_engine
from kestrel.db.models import Evaluation, Opportunity
from tests import today_seed
from tests.browser import (
    LINES_JS,
    TIMER_PROBE_JS,
    TITLE_RIGHT_JS,
    art_visible_left,
    assert_page_remounts_clean,
    launch_sync,
    wait_for_layout,
)

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
    page.wait_for_selector('[data-testid="td-card"], .td-empty')
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
        drawn = art_visible_left(page)
        if drawn is not None:
            assert page.evaluate(TITLE_RIGHT_JS) <= drawn, (title, drawn)
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("width", [1280, 1680])
def test_the_carousel_shows_whole_cards_with_the_next_one_peeking(live, width):
    ctx, page, _ = _page(live, "light", width)
    try:
        vp = page.evaluate(_BOXES, "#td-vp")[0]
        cards = page.evaluate(_BOXES, ".td-card")
        assert len(cards) == 5
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
        assert page.locator(".td-card").count() == 5  # nothing was pressed
    finally:
        ctx.close()


def test_shortlist_and_save_remove_the_card_and_the_list_catches_up(live):
    ctx, page, errors = _page(live, "light", 1680)
    try:
        first = page.query_selector(".td-card").get_attribute("data-id")
        page.locator(f'.td-card[data-id="{first}"] .td-sl').click()
        page.wait_for_function(
            f"!document.querySelector('.td-card[data-id=\"{first}\"]')", timeout=5000)
        assert page.locator(".td-card").count() == 4
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
        page.wait_for_function("document.querySelectorAll('.td-card').length >= 4", timeout=6000)
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
        assert page.locator(".td-card").count() == 5  # Review alone did not remove it
    finally:
        ctx.close()


@pytest.mark.parametrize("theme,width", [("light", 1680), ("dark", 1680), ("light", 1280)])
@pytest.mark.parametrize("only_pending,batch", [
    (False, False), (False, True), (True, False), (True, True),
])
def test_a_finished_review_arrives_at_its_ranked_position_without_page_jumps(
    live, theme, width, only_pending, batch,
):
    base, browser, ids = live
    if only_pending:
        async def earlier():
            engine = make_async_engine(paths.db_path())
            sm = async_sessionmaker(engine, expire_on_commit=False)
            async with sm() as s:
                quick = await s.get(Opportunity, ids["quick"])
                first_seen = quick.first_seen_at
                for key in ("strong", "good", "est", "maybe", "nofit", "rule", "quick"):
                    opp = await s.get(Opportunity, ids[key])
                    opp.first_seen_at = "2020-01-01T00:00:00+00:00"
                await s.commit()
            await engine.dispose()
            forget_engine()
            return first_seen

        with ThreadPoolExecutor(1) as pool:
            first_seen = pool.submit(asyncio.run, earlier()).result()
    ctx, page, errors = _page(live, theme, width)
    try:
        assert page.locator(f'.td-card[data-id="{ids["quick"]}"]').count() == 0
        if only_pending:
            assert page.get_by_test_id("td-pending").inner_text() == ""
            assert page.locator("#td-sub").inner_text() == "Nothing found yet today"

            async def found():
                engine = make_async_engine(paths.db_path())
                sm = async_sessionmaker(engine, expire_on_commit=False)
                async with sm() as s:
                    opp = await s.get(Opportunity, ids["quick"])
                    opp.first_seen_at = first_seen
                    await s.commit()
                await engine.dispose()
                forget_engine()

            with ThreadPoolExecutor(1) as pool:
                pool.submit(asyncio.run, found()).result()
            page.evaluate("window.TodayPage.pollNew()")
            page.wait_for_function(
                "document.querySelector('#td-pending').textContent === '1 more being reviewed'")
            assert page.locator(".td-card").count() == 0
        assert page.get_by_test_id("td-pending").inner_text() == "1 more being reviewed"
        if not only_pending:
            page.get_by_test_id("td-next").click()
        else:
            assert page.locator("#td-sub").inner_text() == "1 new role found today"
        page.wait_for_function("!document.querySelector('.td-launch')")
        wait_for_layout(page)
        page.evaluate("""() => {
          window.reviewFrames = [];
          window.recordReview = true;
          function sample() {
            window.reviewFrames.push(['#td-vp', '.td-lower'].map(selector =>
              document.querySelector(selector).getBoundingClientRect().top).concat(performance.now()));
            if (window.recordReview) requestAnimationFrame(sample);
          }
          sample();
        }""")
        # The card at the viewport's left edge is the one the operator is looking at.
        anchor = None if only_pending else page.evaluate(
            """() => { const vp = document.querySelector('#td-vp').getBoundingClientRect();
              return [...document.querySelectorAll('.td-card')].find(
                c => c.getBoundingClientRect().left >= vp.left - 1).dataset.id; }""")
        left = None if only_pending else page.evaluate(
            f"document.querySelector('.td-card[data-id=\"{anchor}\"]').getBoundingClientRect().left")

        async def add():
            engine = make_async_engine(paths.db_path())
            sm = async_sessionmaker(engine, expire_on_commit=False)
            async with sm() as s:
                opp = await s.get(Opportunity, ids["quick"])
                opp.stage = "t2_done"
                s.add(Evaluation(opportunity_id=opp.id, tier=2, track="employment",
                                 verdict="apply", verdict_model="apply", score=4.5,
                                 weighted_score=4.5, blocks={}))
                if batch:
                    # One poll may bring several reviews, including a taller card with tags.
                    second = await s.get(Opportunity, ids["good" if only_pending else "backlog"])
                    second.first_seen_at = opp.first_seen_at
                    if not only_pending:
                        ev = (await s.execute(select(Evaluation).where(
                            Evaluation.opportunity_id == second.id))).scalar_one()
                        ev.score = ev.weighted_score = 4.45
                await s.commit()
            await engine.dispose()
            forget_engine()

        with ThreadPoolExecutor(1) as pool:  # Playwright's sync API owns this thread's loop
            pool.submit(asyncio.run, add()).result()
        page.evaluate("window.TodayPage.pollNew()")
        page.wait_for_selector('.td-card:has-text("Business Analyst II")', timeout=6000)
        page.wait_for_function("!document.querySelector('.td-card.is-grow')")
        wait_for_layout(page)
        order = page.eval_on_selector_all(".td-t", "els => els.map(e => e.textContent.trim())")
        # 4.5 ranks below 4.6 and above 4.4: second from the left.
        names = [x.removesuffix("NEW") for x in order]
        assert names.index("Business Analyst II") == (0 if only_pending else 1), order
        assert page.locator(".td-new").count() == (2 if batch else 1)
        assert page.get_by_test_id("td-pending").inner_text() == ""
        frames = page.evaluate("window.recordReview = false; window.reviewFrames")
        assert len(frames) > 2
        ranges = [max(frame[column] for frame in frames) - min(frame[column] for frame in frames)
                  for column in (0, 1)]
        assert ranges[0] < 1, ranges
        if only_pending:
            # Replacing the empty state can change height, but it must glide every frame.
            speeds = [abs(b[1] - a[1]) / max(1, b[2] - a[2])
                      for a, b in zip(frames, frames[1:], strict=False)]
            assert max(speeds) < 1.2, (frames[0], frames[-1], max(speeds))
            assert len({round(frame[1], 1) for frame in frames}) > 3
        else:
            assert max(ranges) < 1, (frames[0], min(frames, key=lambda f: f[1]),
                                     max(frames, key=lambda f: f[1]), frames[-1], ranges)
        # The card the operator was looking at did not move.
        if anchor:
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


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("size", [(1680, 1000), (1000, 800)])
def test_tour_footer_buttons_stay_inside_the_popover(live, theme, size):
    """Every step: Skip, Back and Next sit inside the popover's box, with no sideways scroll."""
    ctx, page, errors = _page(live, theme, size[0], "/?tour=1", height=size[1])
    try:
        page.wait_for_selector("#td-tour.is-on", timeout=5000)
        for step in range(6):
            page.evaluate(f"window.KTour.start({step})")
            wait_for_layout(page)
            page.wait_for_timeout(700)   # the popover glides to its place
            tc = page.evaluate(_BOX1, "#td-tc")
            for b in page.evaluate(_BOXES, "#td-tc .k-btn"):
                assert tc["l"] - 1 <= b["l"] and b["r"] <= tc["r"] + 1, (step, b, tc)
                assert tc["tp"] - 1 <= b["tp"] and b["bt"] <= tc["bt"] + 1, (step, b, tc)
            assert tc["l"] >= 0 and tc["r"] <= size[0] and tc["tp"] >= 0 and tc["bt"] <= size[1]
        assert not errors, errors
    finally:
        ctx.close()


def test_the_page_mounts_unmounts_and_mounts_again_with_one_set_of_everything(live):
    """The page lifecycle (D-UI-210): Today unmounted and mounted again in place starts exactly
    what it started on the document load, leaves no timer running in between (the strip's fade,
    the new-match poll, the entrance), and still works after it: one arrow press moves the
    carousel by one page, not two, and the tour opens once."""
    base, browser, _ids = live
    ctx = browser.new_context(viewport={"width": 1280, "height": 1000}, color_scheme="light")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(TIMER_PROBE_JS)
    try:
        page.goto(f"{base}/")
        page.wait_for_selector('[data-testid="td-card"], .td-empty')
        wait_for_layout(page)
        first = assert_page_remounts_clean(page, "today", "today.js")
        assert first["timers"] >= 1  # the new-match poll at least
        assert page.evaluate("typeof window.TodayPage.pollNew") == "function"
        wait_for_layout(page)
        dots = page.query_selector_all("#td-dots i")
        assert len(dots) >= 3, len(dots)  # room for a doubled press to show
        page.get_by_test_id("td-next").click()
        wait_for_layout(page)
        on = page.evaluate("[...document.querySelectorAll('#td-dots i')]"
                           ".findIndex(d => d.classList.contains('is-on'))")
        assert on == 1, on
        page.evaluate("window.KTour.start(0)")
        page.wait_for_selector("#td-tour.is-on")
        page.keyboard.press("ArrowRight")
        assert page.inner_text("#td-tc .td-tc__cnt") == "2 of 6"
        page.keyboard.press("Escape")
        page.wait_for_selector("#td-tour", state="hidden")
        # Unmounting during the tour closes it, and the page's globals go with it.
        page.evaluate("window.KTour.start(0)")
        page.evaluate("KUI.page.unmount()")
        assert page.is_hidden("#td-tour")
        assert page.evaluate("[typeof window.TodayPage, typeof window.KTour]") == [
            "undefined", "undefined"]
        assert not errors, errors
    finally:
        ctx.close()
