"""Being scored again in a real browser (issue #74): press Score again, see the notice with the
earlier score kept, watch it go from waiting to scoring now, and see the new score arrive on the
role page, the Pipeline row and panel, and the Today card with no reload and no blank frame.
The worker is the test: it writes the task, leases it and lands the evaluation straight in the
database. The role page polls only while the notice is on screen, through the page's own timers.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from tests import rescoring_helpers as rh
from tests import role_seed, today_seed
from tests.browser import TIMER_PROBE_JS, assert_page_remounts_clean, launch_sync, wait_for_layout

pytestmark = [
    pytest.mark.usefixtures("_isolate_data_home"),
    pytest.mark.skipif(
        os.environ.get("KESTREL_LIVE_BROWSER") != "1",
        reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
    ),
]

# Every animation frame: is the thing still there, drawn, and not faded out? `key` names what
# to watch, `score` where its number is. A frame with the card missing, empty or near-invisible
# is a blank frame.
_SAMPLER = """([cardSel, scoreSel]) => { window.__frames = []; window.__sample = true;
  const tick = () => {
    const card = document.querySelector(cardSel);
    const sc = card && card.querySelector(scoreSel);
    let op = 0;
    if (card) { op = 1; for (let e = card; e && e !== document.body; e = e.parentElement)
      op *= +getComputedStyle(e).opacity; }
    const r = card ? card.getBoundingClientRect() : {height: 0};
    window.__frames.push({has: !!card, h: r.height, op, score: sc ? sc.textContent.trim() : ''});
    if (window.__sample) requestAnimationFrame(tick); };
  requestAnimationFrame(tick); }"""


def _no_blank_frames(frames):
    assert len(frames) > 10, len(frames)
    bad = [f for f in frames if not f["has"] or f["h"] < 20 or f["op"] < 0.45
           or not f["score"].strip()]
    assert not bad, (len(bad), bad[:3])


@pytest.fixture
def role_live(monkeypatch):
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


@pytest.fixture
def today_live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    ids = asyncio.run(today_seed.seed())
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, ids
        browser.close()


def test_the_role_page_shows_the_wait_then_the_new_score_in_place(role_live):
    base, browser, ids = role_live
    oid = ids["strong"]
    task = rh.queue(oid)
    ctx = browser.new_context(viewport={"width": 1400, "height": 1000}, color_scheme="light")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(TIMER_PROBE_JS)
    try:
        page.goto(f"{base}/opportunities/{oid}")
        page.wait_for_selector("#rl-body")
        wait_for_layout(page)
        page.evaluate("window.__same_page = true")
        note = page.get_by_test_id("rescoring")
        assert "Being scored again" in note.inner_text()
        assert "Waiting for its turn" in note.inner_text()
        before = page.get_by_test_id("score-holistic").inner_text()
        assert before != "4.9"
        assert page.get_by_test_id("earlier-score").inner_text() == "Earlier score"
        # One ctx timer while the notice is up; leaving and coming back holds the same set.
        first = assert_page_remounts_clean(page, "role", "role.js", settle_ms=600)
        assert first["timers"] == 1, first
        # The worker picks it up: the line changes in place, nothing is redrawn.
        rh.lease(task)
        page.wait_for_function(
            "document.querySelector('[data-testid=rescoring]').dataset.rescoring === 'running'",
            timeout=9000)
        assert "Scoring now." in page.get_by_test_id("rescoring-hint").inner_text()
        assert page.get_by_test_id("score-holistic").inner_text() == before
        # The new evaluation lands: the card updates itself.
        page.evaluate(_SAMPLER, ['[data-testid="score-card"]', '[data-testid="score-holistic"]'])
        rh.land(oid, task, score=4.9)
        page.wait_for_function(
            "document.querySelector('[data-testid=score-holistic]')?.textContent.trim() === '4.9'",
            timeout=9000)
        page.wait_for_timeout(700)
        frames = page.evaluate("window.__sample = false; window.__frames")
        _no_blank_frames(frames)
        assert page.evaluate("window.__same_page") is True  # no reload
        assert page.get_by_test_id("rescoring").count() == 0
        assert page.get_by_test_id("earlier-score").count() == 0
        # Nothing polls any more.
        assert page.evaluate("KUI.page._stats().timers") == 0
        assert not errors, errors
    finally:
        ctx.close()


def test_leaving_the_role_page_stops_the_poll(role_live):
    base, browser, ids = role_live
    rh.queue(ids["strong"])
    ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
    page = ctx.new_page()
    page.add_init_script(TIMER_PROBE_JS)
    try:
        page.goto(f"{base}/opportunities/{ids['strong']}")
        page.wait_for_selector("#rl-body")
        assert page.evaluate("KUI.page._stats().timers") == 1
        # Another tab has no notice; no timer is left running for it.
        page.get_by_test_id("tab-posting").click()
        page.wait_for_selector("#rl-pane [data-tab='posting'], #rl-pane")
        page.wait_for_function("KUI.page._stats().timers === 0", timeout=4000)
    finally:
        ctx.close()


def test_the_pipeline_row_and_panel_update_when_the_score_lands(today_live):
    base, browser, ids = today_live
    oid = ids["strong"]
    task = rh.queue(oid)
    ctx = browser.new_context(viewport={"width": 1500, "height": 1000}, color_scheme="light")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"{base}/opportunities?open={oid}")
        page.wait_for_selector(f"#row-{oid}")
        page.wait_for_selector("#pl-panel .pl-panel__in[data-id]")
        wait_for_layout(page)
        page.evaluate("window.__same_page = true")
        row = page.locator(f"#row-{oid}")
        assert row.get_by_test_id("row-rescoring").inner_text() == "Being scored again"
        assert row.get_by_test_id("row-earlier").count() == 1
        panel = page.locator("#pl-panel")
        assert panel.get_by_test_id("panel-rescoring").is_visible()
        assert "Waiting for its turn" in panel.inner_text()
        old = row.locator("[data-fit-score]").inner_text().split("/")[0].strip()
        # Running: the list poll (10 s) redraws the row, and the open panel follows it.
        rh.lease(task)
        page.wait_for_function(
            f"document.getElementById('row-{oid}').dataset.rescoring === 'running'", timeout=16000)
        page.wait_for_function(
            "document.querySelector('#pl-panel .pl-panel__in').dataset.rescoring === 'running'",
            timeout=6000)
        assert "Scoring now." in panel.inner_text()
        page.evaluate(_SAMPLER, [f"#row-{oid}", "[data-fit-score]"])
        rh.land(oid, task, score=4.9)
        page.wait_for_function(
            f"!document.getElementById('row-{oid}').dataset.rescoring", timeout=16000)
        page.wait_for_function(
            "!document.querySelector('#pl-panel .pl-panel__in').dataset.rescoring", timeout=6000)
        page.wait_for_timeout(500)
        frames = page.evaluate("window.__sample = false; window.__frames")
        _no_blank_frames([{**f, "score": f["score"] or ""} for f in frames])
        assert row.locator("[data-fit-score]").inner_text().startswith("4.9")
        assert old != "4.9"
        assert "4.9" in panel.inner_text()
        assert page.get_by_test_id("row-rescoring").count() == 0
        assert page.evaluate("window.__same_page") is True
        assert not errors, errors
    finally:
        ctx.close()


def test_the_today_card_updates_when_the_score_lands(today_live):
    base, browser, ids = today_live
    oid = ids["strong"]
    task = rh.queue(oid)
    ctx = browser.new_context(viewport={"width": 1500, "height": 1000}, color_scheme="light")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"{base}/")
        page.wait_for_selector(f'.td-card[data-id="{oid}"]')
        wait_for_layout(page)
        page.evaluate("window.__same_page = true")
        card = page.locator(f'.td-card[data-id="{oid}"]')
        assert "Being scored again" in card.get_by_test_id("td-rescoring").inner_text()
        assert card.get_by_test_id("td-earlier").count() == 1
        rh.lease(task)
        page.wait_for_function(
            f"document.querySelector('.td-card[data-id=\"{oid}\"]')"
            ".dataset.rescoring === 'running'",
            timeout=40000)
        page.evaluate(_SAMPLER, [f'.td-card[data-id="{oid}"]', '[data-testid="td-score"]'])
        rh.land(oid, task, score=4.9)
        page.wait_for_function(
            f"!document.querySelector('.td-card[data-id=\"{oid}\"]').dataset.rescoring",
            timeout=40000)
        page.wait_for_timeout(500)
        frames = page.evaluate("window.__sample = false; window.__frames")
        _no_blank_frames(frames)
        assert card.get_by_test_id("td-score").inner_text().startswith("4.9")
        assert page.evaluate("window.__same_page") is True
        assert not errors, errors
    finally:
        ctx.close()
