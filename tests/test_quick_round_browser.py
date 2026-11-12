"""The quick round in a real browser (issue 171): the keyboard-only round, the drag, the centered
Top pick button, reduced motion, Today's snooze with Undo, the Calibrate tabs, and the no-overlap
and no-sideways-scroll checks for every new state in light and dark at 1024, 1280 and 1680.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1. Set KESTREL_QR_SHOTS to a
folder to also write the screenshots and the recording (the build deliverables)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from tests import quick_fixtures as qf
from tests import today_seed
from tests.browser import LINES_JS, launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = [
    pytest.mark.usefixtures("_isolate_data_home"),
    pytest.mark.skipif(
        os.environ.get("KESTREL_LIVE_BROWSER") != "1",
        reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
    ),
]

_KIDS = """el => Array.from(el.children).filter(e => e.offsetParent !== null)
  .map(e => { const b = e.getBoundingClientRect();
    return {t: (e.textContent || '').trim().slice(0, 30), l: b.left, r: b.right, tp: b.top,
            bt: b.bottom}; })"""

# A box whose text is wider or taller than the box itself is clipped text.
_CLIPPED = """root => Array.from(root.querySelectorAll('h1,h2,b,span,a,button,li,p,small,kbd'))
  .filter(e => e.offsetParent !== null && e.children.length === 0 && (e.textContent || '').trim())
  .filter(e => { const cs = getComputedStyle(e);
    if (cs.textOverflow === 'ellipsis') return false;
    return e.scrollWidth > e.clientWidth + 1 && cs.display !== 'inline'; })
  .map(e => (e.textContent || '').trim().slice(0, 40))"""


def _t(fn, *args):
    """Database reads run on their own thread: the sync browser API owns this one's event loop."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(1) as ex:
        return ex.submit(fn, *args).result()


def _overlaps(a, b) -> bool:
    return (a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1
            and a["tp"] < b["bt"] - 1 and b["tp"] < a["bt"] - 1)


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    qf.seed(14, finished_runs=1, n_labeled=3)
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


def _open(live, path, *, theme="light", width=1280, height=900, reduced=False):
    base, browser = live
    ctx = browser.new_context(
        viewport={"width": width, "height": height}, color_scheme=theme,
        reduced_motion="reduce" if reduced else "no-preference")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}{path}")
    return ctx, page, errors


def _round(live, **kw):
    ctx, page, errors = _open(live, "/calibrate/quick", **kw)
    page.wait_for_selector(".qr-card.is-front")
    wait_for_layout(page)
    return ctx, page, errors


def _front_opp(page) -> str:
    return page.get_attribute(".qr-card.is-front", "data-opp")


def _answers() -> list[tuple[str, float]]:
    return [(lb.source, lb.score) for lb in _t(qf.labels) if lb.source == "quick"]


def _assert_clean(page, containers, *, width, theme, where):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True, (
        where, "sideways scroll", theme, width)
    for sel in containers:
        for group in page.query_selector_all(sel):
            if not group.is_visible():
                continue
            kids = page.evaluate(_KIDS, group)
            for i, a in enumerate(kids):
                for b in kids[i + 1:]:
                    assert not _overlaps(a, b), (where, sel, a, b, theme, width)
    assert page.evaluate(_CLIPPED, page.query_selector("body")) == [], (where, theme, width)


# ------------------------------------------------------------------------------ the round
def _wait_answers(n: int) -> None:
    for _ in range(80):
        if len(_answers()) == n:
            return
        import time

        time.sleep(0.1)
    assert len(_answers()) == n


def _count_is(page, k: int) -> None:
    page.wait_for_function(
        "k => document.querySelector('#qr-count').textContent.startsWith(k + ' of')", arg=k)


def test_a_keyboard_only_round_saves_each_answer_and_undo_removes_the_last(live):
    ctx, page, errors = _round(live)
    seeded = len(_answers())  # three answers from earlier rounds
    try:
        page.keyboard.press("ArrowRight")
        _count_is(page, 2)
        page.keyboard.press("ArrowLeft")
        _count_is(page, 3)
        page.keyboard.press("ArrowUp")
        _count_is(page, 4)
        page.keyboard.press("ArrowDown")  # not sure: nothing saved
        _count_is(page, 5)
        _wait_answers(seeded + 3)
        assert [s for _, s in _answers()][seeded:] == [4.0, 2.0, 5.0]
        page.keyboard.press("Control+z")  # undoes Not sure: nothing to delete
        _count_is(page, 4)
        assert len(_answers()) == seeded + 3
        page.keyboard.press("Control+z")  # undoes Top pick
        _count_is(page, 3)
        _wait_answers(seeded + 2)
        assert not errors
    finally:
        ctx.close()


def _wait_setting(key, want) -> None:
    import time

    for _ in range(80):
        if _t(qf.setting, key) == want:
            return
        time.sleep(0.1)
    assert _t(qf.setting, key) == want


def test_undoing_every_answer_of_a_round_restores_the_offer_as_it_was(live):
    ctx, page, errors = _round(live)
    key = "ui.quick.next_runs.employment"
    try:
        assert _t(qf.setting, key) is None  # due: one finished run, never taken
        page.keyboard.press("ArrowRight")
        _count_is(page, 2)
        page.keyboard.press("ArrowLeft")
        _count_is(page, 3)
        _wait_setting(key, 4)  # answering took the offer
        page.keyboard.press("Control+z")
        _count_is(page, 2)
        page.wait_for_timeout(300)
        assert _t(qf.setting, key) == 4  # one answer is still saved: still taken
        page.keyboard.press("Control+z")
        _count_is(page, 1)
        _wait_setting(key, 1)  # the round's only answer is gone: the offer is back as it was
        assert not errors
    finally:
        ctx.close()


def test_a_save_failing_during_the_fly_out_brings_the_same_card_back(live):
    ctx, page, errors = _round(live)
    seeded = len(_answers())
    try:
        first = _front_opp(page)
        page.route("**/calibrate/quick/answer**", lambda route: route.fulfill(
            status=404, body='{"error": "opportunity not found"}',
            content_type="application/json"))
        page.keyboard.press("ArrowRight")
        page.wait_for_selector(".k-toast")
        page.wait_for_timeout(600)  # well past the 260 ms flight
        assert _front_opp(page) == first
        assert page.text_content("#qr-count").startswith("1 of")
        page.unroute("**/calibrate/quick/answer**")
        page.keyboard.press("ArrowRight")
        _count_is(page, 2)
        _wait_answers(seeded + 1)
        rows = [lb for lb in _t(qf.labels) if lb.source == "quick"]
        assert str(rows[-1].opportunity_id) == first and rows[-1].score == 4.0
        assert not errors
    finally:
        ctx.close()


def test_finishing_a_round_shows_the_done_screen_and_keep_going_loads_more(live):
    ctx, page, errors = _round(live)
    try:
        for k in range(10):
            page.keyboard.press("ArrowRight")
            if k < 9:
                _count_is(page, k + 2)
        page.wait_for_selector('[data-testid="qr-done"]')
        assert "Done. Kestrel now has" in page.text_content('[data-testid="qr-done-h"]')
        assert page.is_hidden("#qr-round")
        page.click('[data-testid="qr-more"]')
        page.wait_for_selector(".qr-card.is-front")
        assert page.is_visible("#qr-round") and page.is_hidden("#qr-done")
        assert not errors
    finally:
        ctx.close()


def test_dragging_the_card_answers_like_the_keys(live):
    ctx, page, errors = _round(live)
    try:
        box = page.locator(".qr-card.is-front").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx + 80, cy, steps=5)  # mid swipe: the YES stamp shows
        assert float(page.evaluate(
            "getComputedStyle(document.querySelector('.qr-card.is-front .qr-stamp--y')).opacity"
        )) > 0.3
        page.mouse.move(cx + 220, cy, steps=5)
        page.mouse.up()
        for _ in range(60):
            if len(_answers()) == 4:
                break
            page.wait_for_timeout(100)
        assert sorted(s for _, s in _answers()) == [4.0, 4.0, 4.0, 4.0]
        # A short drag springs back and saves nothing.
        box = page.locator(".qr-card.is-front").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx + 40, cy, steps=3)
        page.mouse.up()
        page.wait_for_timeout(700)
        assert len(_answers()) == 4
        # Up past the threshold is a Top pick.
        box = page.locator(".qr-card.is-front").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx, cy - 200, steps=6)
        page.mouse.up()
        for _ in range(60):
            if len(_answers()) == 5:
                break
            page.wait_for_timeout(100)
        assert 5.0 in [s for _, s in _answers()]
        assert not errors
    finally:
        ctx.close()


def test_mid_swipe_the_card_behind_shows_the_next_roles_details(live):
    ctx, page, errors = _round(live)
    try:
        box = page.locator(".qr-card.is-front").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx + 96, cy, steps=4)
        page.wait_for_timeout(400)
        behind = page.evaluate("""() => { const c = document.querySelector('.qr-card.is-n1');
          const t = c.querySelector('.qr-title'); let o = 1;
          for (let e = t; e && e !== c.parentElement; e = e.parentElement)
            o *= parseFloat(getComputedStyle(e).opacity);
          return {text: t.textContent.trim(), opacity: o}; }""")
        assert behind["text"].startswith("Operations Analyst") and behind["opacity"] > 0.9, behind
        page.mouse.move(cx, cy, steps=2)
        page.mouse.up()
        assert not errors
    finally:
        ctx.close()


def test_the_top_pick_button_is_a_circle_with_its_icon_centered(live):
    ctx, page, _ = _round(live)
    try:
        for sel in ("#qr-no", "#qr-unsure", "#qr-top", "#qr-yes"):
            m = page.evaluate("""s => { const b = document.querySelector(s);
              const r = b.getBoundingClientRect();
              const i = b.querySelector('svg').getBoundingClientRect();
              return {w: r.width, h: r.height, dx: (i.left + i.width / 2) - (r.left + r.width / 2),
                      dy: (i.top + i.height / 2) - (r.top + r.height / 2),
                      radius: getComputedStyle(b).borderRadius}; }""", sel)
            assert abs(m["w"] - m["h"]) < 1, (sel, m)
            assert abs(m["dx"]) < 1 and abs(m["dy"]) < 1, (sel, m)
            assert m["radius"] in ("50%", f"{m['w'] / 2}px"), (sel, m)
        top = page.evaluate("document.querySelector('#qr-top').getBoundingClientRect().width")
        yes = page.evaluate("document.querySelector('#qr-yes').getBoundingClientRect().width")
        assert top < yes  # Top pick stays the smaller button
    finally:
        ctx.close()


def test_reduced_motion_turns_the_flight_off_and_the_buttons_and_keys_still_work(live):
    ctx, page, errors = _round(live, reduced=True)
    try:
        page.click("#qr-yes")
        page.keyboard.press("ArrowLeft")
        page.wait_for_function("document.querySelector('#qr-count').textContent.startsWith('3 of')")
        for _ in range(60):
            if len(_answers()) == 5:
                break
            page.wait_for_timeout(100)
        assert [s for _, s in _answers()][3:] == [4.0, 2.0]
        # No pointer-follow in reduced motion: a drag leaves the card alone.
        box = page.locator(".qr-card.is-front").bounding_box()
        page.mouse.move(box["x"] + 50, box["y"] + 100)
        page.mouse.down()
        page.mouse.move(box["x"] + 250, box["y"] + 100, steps=4)
        assert page.evaluate("document.querySelector('.qr-card.is-front').style.transform") == ""
        page.mouse.up()
        assert not errors
    finally:
        ctx.close()


def test_the_apps_reduce_motion_switch_stops_the_drag_and_the_flight(live):
    """D-UI-321: Settings > Reduce motion (data-reduce-motion="1") with the OS setting off."""
    ctx, page, errors = _round(live)
    try:
        page.evaluate("document.documentElement.setAttribute('data-reduce-motion', '1')")
        box = page.locator(".qr-card.is-front").bounding_box()
        page.mouse.move(box["x"] + 50, box["y"] + 100)
        page.mouse.down()
        page.mouse.move(box["x"] + 250, box["y"] + 100, steps=4)
        assert page.evaluate("document.querySelector('.qr-card.is-front').style.transform") == ""
        page.mouse.up()
        first = _front_opp(page)
        page.keyboard.press("ArrowRight")
        _count_is(page, 2)
        gone = page.locator(f'.qr-card[data-opp="{first}"]')
        assert gone.evaluate("c => c.style.transform") == ""  # no fly-off
        assert not errors
    finally:
        ctx.close()


def test_a_save_failing_after_later_answers_keeps_undo_on_the_right_cards(live):
    """D-UI-321: card 2's save fails only after cards 3 and 4 were answered. The pips keep each
    card's own answer, and Undo removes card 4's and card 3's answers, then steps back to card 2
    (never saved) and leaves card 1's saved answer alone."""
    ctx, page, errors = _round(live)
    seeded = len(_answers())
    held: list = []
    calls: list[int] = []

    def handler(route):
        calls.append(1)
        if len(calls) == 2:
            held.append(route)  # card 2: answered later, as a busy database would
        else:
            route.continue_()

    try:
        page.route("**/calibrate/quick/answer**", handler)
        page.keyboard.press("ArrowRight")  # card 1: Yes, saved
        _count_is(page, 2)
        _wait_answers(seeded + 1)
        page.keyboard.press("ArrowLeft")  # card 2: Not for me, its save held
        _count_is(page, 3)
        page.wait_for_timeout(400)
        page.keyboard.press("ArrowUp")  # card 3: Top pick
        _count_is(page, 4)
        page.wait_for_timeout(400)
        page.keyboard.press("ArrowRight")  # card 4: Yes
        _count_is(page, 5)
        _wait_answers(seeded + 3)
        held[0].fulfill(status=503, body='{"error": "busy"}', content_type="application/json")
        page.wait_for_selector(".k-toast")
        pips = page.eval_on_selector_all("#qr-pips i", "els => els.map(e => e.className)")
        assert pips[:4] == ["is-yes", "", "is-top", "is-yes"], pips
        page.keyboard.press("Control+z")  # card 4
        _count_is(page, 4)
        _wait_answers(seeded + 2)
        page.keyboard.press("Control+z")  # card 3
        _count_is(page, 3)
        _wait_answers(seeded + 1)
        page.keyboard.press("Control+z")  # card 2: never saved, nothing removed
        _count_is(page, 2)
        page.wait_for_timeout(500)
        assert len(_answers()) == seeded + 1  # card 1's answer is still there
        assert not errors
    finally:
        ctx.close()


# ------------------------------------------------------------------------------------ Today
@pytest.fixture
def today_live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    asyncio.run(today_seed.seed())
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


def test_snooze_with_undo_on_today(today_live):
    ctx, page, errors = _open(today_live, "/")
    try:
        page.wait_for_selector('[data-testid="td-qr"]')
        page.click('[data-testid="td-qr-later"]')
        page.wait_for_selector("#td-qr-menu:not([hidden])")
        page.click('[data-testid="td-qr-snooze"]')
        page.wait_for_selector('[data-testid="td-qr"]', state="detached")
        toast = page.locator(".k-toast")
        assert "ask again after 3 more runs" in toast.inner_text()
        toast.locator(".k-toast__undo").click()
        page.wait_for_selector('[data-testid="td-qr"]')
        # Stop asking, then Undo, the same way.
        page.click('[data-testid="td-qr-later"]')
        page.click('[data-testid="td-qr-stop"]')
        page.wait_for_selector('[data-testid="td-qr"]', state="detached")
        page.locator(".k-toast__undo").last.click()
        page.wait_for_selector('[data-testid="td-qr"]')
        assert not errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("size", [(1024, 700), (1280, 900), (960, 640), (480, 800)])
def test_a_very_long_title_and_place_stay_inside_the_card(live, theme, size):
    """Fable C3: the card has a fixed height; long text ends in an ellipsis and never runs onto
    the facts, the Open posting row or the answer buttons."""
    from sqlalchemy import update

    from kestrel.db.models import Opportunity

    long_title = ("Senior Manager, Global Supply Chain Planning and Logistics Operations, "
                  "North America (Hybrid) with Regional Leadership Responsibilities")
    long_place = "Greater Columbus Metropolitan Statistical Area, Ohio, United States, Remote OK"

    async def longer(s):
        await s.execute(update(Opportunity).values(title=long_title, location_raw=long_place))
        await s.commit()

    _t(qf.run, longer)
    ctx, page, errors = _round(live, theme=theme, width=size[0], height=size[1])
    try:
        shots = os.environ.get("KESTREL_QR_SHOTS")
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(shots) / f"quick-long-{theme}-{size[0]}x{size[1]}.png"))
        box = page.evaluate("""() => {
            const r = e => e.getBoundingClientRect();
            const card = document.querySelector('.qr-card.is-front'), c = r(card);
            const kids = Array.from(card.children).filter(e => !e.classList.contains('qr-stamp'))
              .map(e => ({t: e.className, bt: r(e).bottom}));
            return {bottom: c.bottom, kids, acts: r(document.querySelector('.qr-acts')).top};
        }""")
        for k in box["kids"]:
            assert k["bt"] <= box["bottom"] + 1, (k, box["bottom"])
        assert box["bottom"] <= box["acts"] + 1, box
        assert not errors
    finally:
        ctx.close()


def test_the_not_now_menu_stays_open_when_the_strip_refreshes(today_live):
    """Fable C6: the strip's poll or a run event morphs it in place; an open menu is not closed
    under the person's keyboard focus."""
    ctx, page, errors = _open(today_live, "/")
    try:
        page.wait_for_selector('[data-testid="td-qr-later"]')
        page.focus('[data-testid="td-qr-later"]')
        page.keyboard.press("Enter")
        page.wait_for_selector("#td-qr-menu:not([hidden])")
        page.keyboard.press("ArrowDown")
        assert page.evaluate("document.activeElement.dataset.testid") == "td-qr-stop"
        page.evaluate("""() => { window.__morphs = 0;
            document.addEventListener('k-morphed', () => { window.__morphs++; }, true);
            htmx.trigger(document.body, 'td-refresh'); }""")
        page.wait_for_function("() => !document.querySelector('.htmx-request')", timeout=5000)
        page.wait_for_timeout(400)
        state = page.evaluate("""() => ({hidden: document.querySelector('#td-qr-menu').hidden,
            expanded: document.querySelector('#td-qr-later').getAttribute('aria-expanded'),
            focus: document.activeElement.dataset.testid || ''})""")
        assert state == {"hidden": False, "expanded": "true", "focus": "td-qr-stop"}, state
        assert not errors
    finally:
        ctx.close()


def test_the_not_now_menu_opens_by_keyboard_and_escape_closes_it(today_live):
    ctx, page, _ = _open(today_live, "/")
    try:
        page.wait_for_selector('[data-testid="td-qr-later"]')
        page.focus('[data-testid="td-qr-later"]')
        page.keyboard.press("Enter")
        page.wait_for_selector("#td-qr-menu:not([hidden])")
        assert page.evaluate("document.activeElement.dataset.testid") == "td-qr-snooze"
        page.keyboard.press("Escape")
        page.wait_for_selector("#td-qr-menu", state="hidden")
        assert page.evaluate("document.activeElement.id") == "td-qr-later"
    finally:
        ctx.close()


@pytest.mark.parametrize("width", [1024, 1280, 1680])
def test_the_not_now_menu_opens_under_its_own_button_inside_the_strip(today_live, width):
    ctx, page, _ = _open(today_live, "/", width=width)
    try:
        page.wait_for_selector('[data-testid="td-qr-later"]')
        page.click('[data-testid="td-qr-later"]')
        page.wait_for_selector("#td-qr-menu:not([hidden])")
        page.wait_for_timeout(300)  # the menu's entrance
        m = page.evaluate("""() => { const r = s => document.querySelector(s)
            .getBoundingClientRect(); const b = r('#td-qr-later'), m = r('#td-qr-menu'),
            q = r('.td-qr'); return {bl: b.left, br: b.right, bb: b.bottom, ml: m.left, mr: m.right,
            mt: m.top, ql: q.left, qr: q.right}; }""")
        assert m["mt"] >= m["bb"]  # below the button
        # Narrow: the buttons sit at the left, so the menu's left edge is the button's. Wide:
        # they sit at the right, so its right edge is. Either way it stays within the strip.
        menu_edge, button_edge = ("ml", "bl") if width == 1024 else ("mr", "br")
        assert abs(m[menu_edge] - m[button_edge]) < 1, m
        assert m["ml"] >= m["ql"] - 1 and m["mr"] <= m["qr"] + 1, m
    finally:
        ctx.close()


# ------------------------------------------------------------------------------- the tabs
def test_the_tabs_switch_and_the_rating_keys_stay_with_detailed(live):
    ctx, page, errors = _open(live, "/calibrate")
    try:
        page.wait_for_selector('[data-testid="cal-quick"]')
        assert page.is_hidden('[data-testid="cal-card"]')
        page.keyboard.press("4")  # the Detailed shortcut does nothing on Quick
        assert page.is_hidden('[data-testid="cal-reveal"]')
        page.click('[data-testid="cal-tab-detailed"]')
        page.wait_for_selector('[data-testid="cal-card"] .cal-q')
        assert page.is_hidden('[data-testid="cal-quick"]')
        assert page.get_attribute('[data-testid="cal-tab-detailed"]', "aria-selected") == "true"
        page.keyboard.press("4")
        page.wait_for_selector('[data-testid="cal-reveal"]')
        page.focus('[data-testid="cal-tab-detailed"]')
        page.keyboard.press("ArrowLeft")
        page.wait_for_selector('[data-testid="cal-quick"]')
        assert page.get_attribute('[data-testid="cal-tab-quick"]', "aria-selected") == "true"
        assert not errors
    finally:
        ctx.close()


def test_the_chosen_tab_is_in_the_address_so_a_reload_keeps_it(live):
    ctx, page, errors = _open(live, "/calibrate")
    try:
        page.wait_for_selector('[data-testid="cal-quick"]')
        page.click('[data-testid="cal-tab-detailed"]')
        page.wait_for_selector('[data-testid="cal-card"] .cal-q')
        assert "tab=detailed" in page.url
        page.reload()
        page.wait_for_selector('[data-testid="cal-card"] .cal-q')
        assert page.get_attribute('[data-testid="cal-tab-detailed"]', "aria-selected") == "true"
        assert page.is_hidden('[data-testid="cal-quick"]')
        page.click('[data-testid="cal-tab-quick"]')
        page.wait_for_selector('[data-testid="cal-quick"]')
        assert "tab=" not in page.url
        # An old link (Settings, a notice) opens Detailed directly.
        page.goto(page.url.split("/calibrate")[0] + "/calibrate?tab=detailed")
        page.wait_for_selector('[data-testid="cal-card"] .cal-q')
        assert page.is_hidden('[data-testid="cal-quick"]')
        assert not errors
    finally:
        ctx.close()


def test_the_offer_settings_save_from_the_quick_tab(live):
    ctx, page, _ = _open(live, "/calibrate")
    try:
        page.wait_for_selector('[data-testid="cal-quick-settings"]')
        page.select_option('[data-testid="cal-quick-cadence"]', "weekly")
        for _ in range(50):
            if _t(qf.setting, "ui.quick.cadence") == "weekly":
                break
            page.wait_for_timeout(100)
        assert _t(qf.setting, "ui.quick.cadence") == "weekly"
        page.click('label[for="cal-quick-on"]')
        for _ in range(50):
            if _t(qf.setting, "ui.quick.enabled") is False:
                break
            page.wait_for_timeout(100)
        assert _t(qf.setting, "ui.quick.enabled") is False
    finally:
        ctx.close()


# ------------------------------------------------------- layout in every new state
_STATES = ("today", "round", "mid", "done", "quick-tab", "detailed-tab")


def _shoot(page, name, theme, width):
    out = os.environ.get("KESTREL_QR_SHOTS")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        label = "narrow" if width == 1024 else str(width)
        page.screenshot(path=str(Path(out) / f"{name}-{theme}-{label}.png"))


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1024, 1280, 1680])
def test_no_overlap_no_clipping_no_sideways_scroll_in_every_state(today_live, theme, width):
    ctx, page, errors = _open(today_live, "/", theme=theme, width=width)
    try:
        page.wait_for_selector('[data-testid="td-qr"]')
        wait_for_layout(page)
        _assert_clean(page, (".td-qr", ".td-qr__acts", ".td-qr__body", ".td-strip"),
                      width=width, theme=theme, where="today")
        page.click('[data-testid="td-qr-later"]')
        page.wait_for_selector("#td-qr-menu:not([hidden])")
        menu = page.evaluate("""() => { const r = document.querySelector('#td-qr-menu')
          .getBoundingClientRect(); return {l: r.left, r: r.right}; }""")
        assert menu["l"] >= 0 and menu["r"] <= width
        _assert_clean(page, ("#td-qr-menu",), width=width, theme=theme, where="menu")
        page.wait_for_timeout(450)  # the menu's own entrance
        _shoot(page, "today", theme, width)
    finally:
        ctx.close()
    ctx, page, errors = _round(today_live, theme=theme, width=width)
    try:
        _assert_clean(page, (".qr-top", ".qr-rhead", ".qr-acts", ".qr-foot"),
                      width=width, theme=theme, where="round")
        _shoot(page, "round", theme, width)
        box = page.locator(".qr-card.is-front").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] / 2 + 96, box["y"] + box["height"] / 2, steps=4)
        _assert_clean(page, (".qr-rhead", ".qr-acts"), width=width, theme=theme, where="mid")
        _shoot(page, "mid", theme, width)
        page.mouse.up()
        page.click("#qr-finish")
        page.wait_for_selector('[data-testid="qr-done"]')
        wait_for_layout(page)
        _assert_clean(page, (".qr-donebox", ".qr-split", ".qr-steps li", ".qr-btns"),
                      width=width, theme=theme, where="done")
        _shoot(page, "done", theme, width)
        assert not errors
    finally:
        ctx.close()
    ctx, page, errors = _open(today_live, "/calibrate", theme=theme, width=width)
    try:
        page.wait_for_selector('[data-testid="cal-quick"]')
        wait_for_layout(page)
        _assert_clean(page, (".cal-quick", ".cal-set", ".qr-meter"),
                      width=width, theme=theme, where="quick-tab")
        _shoot(page, "cal-quick", theme, width)
        page.click('[data-testid="cal-tab-detailed"]')
        page.wait_for_selector('[data-testid="cal-card"] .cal-q')
        page.wait_for_timeout(500)
        _assert_clean(page, (".cal-prog",), width=width, theme=theme,
                      where="detailed-tab")
        _shoot(page, "cal-detailed", theme, width)
        assert not errors
    finally:
        ctx.close()


def test_the_lines_probe_is_available():
    assert LINES_JS  # shared helper imported for the layout checks above


@pytest.mark.skipif(not os.environ.get("KESTREL_QR_SHOTS"), reason="set KESTREL_QR_SHOTS to record")
def test_record_a_swipe_a_snooze_with_undo_and_a_tab_change(today_live):
    """The deliverable recording (D-UI-238): a swipe, a snooze with Undo and a Calibrate tab
    change, one video each, in the shots folder. Frames are checked by the builder with ffmpeg."""
    base, browser = today_live
    out = Path(os.environ["KESTREL_QR_SHOTS"])
    out.mkdir(parents=True, exist_ok=True)

    def video(name, steps):
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900}, record_video_dir=str(out / "_v"),
            record_video_size={"width": 1280, "height": 900})
        page = ctx.new_page()
        steps(page)
        path = page.video.path()
        ctx.close()
        Path(path).replace(out / f"{name}.webm")

    def swipe(page):
        page.goto(f"{base}/calibrate/quick")
        page.wait_for_selector(".qr-card.is-front")
        page.wait_for_timeout(800)
        for dx in (220, -220):
            box = page.locator(".qr-card.is-front").bounding_box()
            cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            page.mouse.move(cx, cy)
            page.mouse.down()
            page.mouse.move(cx + dx, cy, steps=14)
            page.mouse.up()
            page.wait_for_timeout(900)
        page.keyboard.press("ArrowDown")  # not sure: saves nothing, keeps 5 roles for Today
        page.wait_for_timeout(900)

    def snooze(page):
        # The swipe took the offer; put the strip back as it was before the take.
        page.request.post(f"{base}/today/quick/restore?enabled=1&runs=1&track=employment")
        page.goto(f"{base}/")
        page.wait_for_selector('[data-testid="td-qr"]')
        page.wait_for_timeout(1500)
        page.click('[data-testid="td-qr-later"]')
        page.wait_for_timeout(500)
        page.click('[data-testid="td-qr-snooze"]')
        page.wait_for_selector('[data-testid="td-qr"]', state="detached")
        page.wait_for_timeout(1200)
        page.locator(".k-toast__undo").click()
        page.wait_for_selector('[data-testid="td-qr"]')
        page.wait_for_timeout(1200)

    def tabs(page):
        page.goto(f"{base}/calibrate")
        page.wait_for_selector('[data-testid="cal-quick"]')
        page.wait_for_timeout(1000)
        page.click('[data-testid="cal-tab-detailed"]')
        page.wait_for_selector('[data-testid="cal-card"] .cal-q')
        page.wait_for_timeout(1200)
        page.click('[data-testid="cal-tab-quick"]')
        page.wait_for_timeout(1200)

    video("recording-swipe", swipe)
    video("recording-snooze-undo", snooze)
    video("recording-tab-change", tabs)
    for name in ("recording-swipe", "recording-snooze-undo", "recording-tab-change"):
        assert (out / f"{name}.webm").stat().st_size > 0
