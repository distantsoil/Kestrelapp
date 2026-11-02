"""Offers in a real browser (UI overhaul parity check against the locked mockup,
the locked design mockup (kept privately)). Light and dark at 1280 and 1680: no overlap,
no wrapping titles or buttons, no horizontal scroll, on every tab and in the empty state; then the
interactions the mockup demos (the menu and Undo, stars and weights, the Record sheet, the
walk-away lock and the draft).

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1. The browser is the Edge
already on the machine, else a Playwright Chromium (tests/browser.py). Set KESTREL_OFFERS_SHOTS to
a directory to also save a screenshot of every state."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests import offers_seed
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

# One line of text: every client rect of the element's text sits at the same height.
_LINES = LINES_JS

_BOXES = """sel => Array.from(document.querySelectorAll(sel)).filter(e => e.offsetParent !== null)
  .map(e => { const b = e.getBoundingClientRect();
    return {t: (e.textContent || '').trim().slice(0, 30), l: b.left, r: b.right, tp: b.top,
            bt: b.bottom}; })"""

_SHOTS = os.environ.get("KESTREL_OFFERS_SHOTS")


def _overlaps(a, b) -> bool:
    return a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1 and a["tp"] < b["bt"] - 1 \
        and b["tp"] < a["bt"] - 1


def _shot(page, name: str) -> None:
    if _SHOTS:
        Path(_SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(_SHOTS) / f"{name}.png"), full_page=True)


async def _no_draft(_s, _client, pid, *, walk_away, scenarios=None, ask=None, **_more):
    from kestrel.negotiation.scripts import ScriptResult

    scenario = (scenarios or ["counter_below_target"])[0]
    return ScriptResult(
        situation_read="A fair offer, a little under your target.",
        walk_away=walk_away,
        scripts=[{
            "scenario": scenario,
            "script": (
                "Thank you for the offer. I am excited about the team.\n\n"
                f"I would like to ask for {ask or 'a base closer to my target'}. I raised "
                "throughput 22% across two distribution centers and rebuilt our forecasting in "
                "SQL.\n\nIf we can reach that number, I am ready to accept."
            ),
            "justification_basis": "Your throughput result.",
            "likely_pushback": "The band is firm.",
            "response_to_pushback": "Ask about signing pay.",
            "supported": True,
        }],
        leverage_inventory=["A second offer"], risks=["Pushback on level"],
    )


@pytest.fixture
def live(monkeypatch):
    """A server on the test's own throwaway data directory and a browser."""
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    monkeypatch.setattr("kestrel.web.pages.offers.draft_scripts", _no_draft)
    monkeypatch.setattr(
        "kestrel.web.pages.offers.load_profile",
        lambda track: __import__("types").SimpleNamespace(
            proof_points=[{"claim": "raised throughput 22% across two distribution centers",
                           "evidence": "Resume, Meadowbrook Foods, bullet 2"}],
            employment_terms=__import__("types").SimpleNamespace(
                comp_floor_usd=120000, comp_target_usd=150000),
            consulting_terms=__import__("types").SimpleNamespace(
                day_rate_floor=450, day_rate_target=600),
        ),
    )
    ids = offers_seed.seed(third=False)
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, ids
        browser.close()


@pytest.fixture
def live_with_pursuit(monkeypatch):
    """The same, plus one more application with no offer yet, for the Record sheet to pick."""
    import asyncio

    from playwright.sync_api import sync_playwright
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel import paths
    from kestrel.db.engine import forget_engine, make_async_engine
    from kestrel.db.models import Opportunity, Org, Pursuit, Source
    from kestrel.web.serve import running_server

    monkeypatch.setattr("kestrel.web.pages.offers.draft_scripts", _no_draft)
    ids = offers_seed.seed(third=False)

    async def add_pursuit():
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            src = await s.get(Source, 1)
            org = Org(name="Harbor Point Analytics", normalized_name="harbor point analytics")
            s.add(org)
            await s.flush()
            opp = Opportunity(track="employment", opp_type="job_posting", source_id=src.id,
                              org_id=org.id, title="Operations Program Manager", stage="t3_pass")
            s.add(opp)
            await s.flush()
            s.add(Pursuit(opportunity_id=opp.id, track="employment", status="applied"))
            await s.commit()
        await engine.dispose()
        forget_engine()

    asyncio.run(add_pursuit())
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, ids
        browser.close()


def _page(live, theme: str, width: int, path: str = "/offers", wait: str = ".of-pane"):
    base, browser, _ids = live
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}{path}")
    page.wait_for_selector(wait)
    wait_for_layout(page)
    return ctx, page, errors


def _clean(page, extra_one_line=()) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
    assert page.evaluate("document.querySelector('.main, main').scrollWidth <= "
                         "document.querySelector('.main, main').clientWidth + 1") is True
    for sel in ("h1", ".of-lede", ".k-pill", ".k-chip", ".of-page .k-btn", ".of-tab",
                ".of-total b", ".of-card__who b", ".of-dl", ".of-band__tk", ".of-band__simt",
                ".of-bench > span:first-child:not(.of-insuf)",
                *extra_one_line):
        for el in page.query_selector_all(sel):
            if el.is_visible():
                assert page.evaluate(_LINES, el) <= 1, (sel, el.inner_text()[:40])
    for group in (".of-band__tk, .of-band__simt", ".of-card .k-pill, .of-card .of-mono"):
        boxes = page.evaluate(_BOXES, group)
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                assert not _overlaps(a, b), (group, a["t"], b["t"])


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_every_tab_is_clean(live, theme, width):
    ctx, page, errors = _page(live, theme, width, wait='[data-testid="of-card"]')
    try:
        _clean(page)
        _shot(page, f"compare-{theme}-{width}")
        for tab, key in (("negotiate", "of-negotiate"), ("history", "of-history")):
            page.click(f'[data-testid="of-tab-{tab}"]')
            page.wait_for_selector(f'[data-testid="{key}"]')
            wait_for_layout(page)
            _clean(page, (".of-hrow .k-pill",))
            _shot(page, f"{tab}-{theme}-{width}")
        assert errors == []
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_consulting_compare_is_clean(live, theme, width):
    ctx, page, errors = _page(live, theme, width, "/offers?track=consulting",
                              '[data-testid="of-card"]')
    try:
        assert "Engagement offers" in page.inner_text("h1")
        _clean(page)
        _shot(page, f"consulting-{theme}-{width}")
        assert errors == []
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_the_empty_state_is_clean(monkeypatch, theme, width):
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    offers_seed.seed(consulting=False, past=False)
    # Take the offers away, keep the pursuits and the corpus: the "no offers yet" view.
    import asyncio

    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel import paths
    from kestrel.db.engine import forget_engine, make_async_engine
    from kestrel.db.models import Offer

    async def wipe():
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine)() as s:
            await s.execute(delete(Offer))
            await s.commit()
        await engine.dispose()
        forget_engine()

    asyncio.run(wipe())
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
        page = ctx.new_page()
        page.goto(f"{base}/offers")
        page.wait_for_selector('[data-testid="of-empty"]')
        wait_for_layout(page)
        try:
            _clean(page, (".of-gh span",))
            assert page.is_hidden('[data-testid="of-record"]')  # the empty state has its own
            text = page.inner_text('[data-testid="of-market"]')
            assert "from 12 similar roles" in text and "Too few to tell (3)" in text
            _shot(page, f"empty-{theme}-{width}")
        finally:
            ctx.close()
            browser.close()


def _wipe_offers() -> None:
    """Take the offers away, keep the pursuits: the "no offers yet" view."""
    import asyncio

    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel import paths
    from kestrel.db.engine import forget_engine, make_async_engine
    from kestrel.db.models import Offer

    async def wipe():
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine)() as s:
            await s.execute(delete(Offer))
            await s.commit()
        await engine.dispose()
        forget_engine()

    asyncio.run(wipe())


@pytest.fixture
def live_empty(monkeypatch):
    """A server with no offers recorded, a Profile floor and target, and a stand-in model."""
    import types

    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    monkeypatch.setattr("kestrel.web.pages.offers.draft_scripts", _no_draft)
    monkeypatch.setattr(
        "kestrel.web.pages.offers.load_profile",
        lambda track: types.SimpleNamespace(
            proof_points=[],
            employment_terms=types.SimpleNamespace(comp_floor_usd=120000, comp_target_usd=150000),
            consulting_terms=types.SimpleNamespace(day_rate_floor=450, day_rate_target=600),
        ),
    )
    offers_seed.seed(consulting=False, past=False)
    _wipe_offers()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, None
        browser.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [390, 1280, 1680])
def test_negotiate_with_no_offer_is_a_clean_preparation_page_and_drafts(live_empty, theme, width):
    ctx, page, errors = _page(live_empty, theme, width, "/offers?tab=compare",
                              '[data-testid="of-empty"]')
    try:
        # From Compare, the Negotiate tab swaps in its own page, not the same card.
        page.click('[data-testid="of-tab-negotiate"]')
        page.wait_for_selector('[data-testid="of-negotiate-prep"]')
        wait_for_layout(page)
        assert page.is_hidden('[data-testid="of-market"]') or \
            page.query_selector('[data-testid="of-market"]') is None
        assert "$120k" in page.inner_text('[data-testid="of-prep-floor"]')
        assert "$150k" in page.inner_text('[data-testid="of-prep-target"]')
        _clean(page, (".of-nums-list dd b",))
        _shot(page, f"negotiate-empty-{theme}-{width}")
        cards = page.query_selector_all('[data-testid="of-prep-sit"]')
        assert [c.is_enabled() for c in cards].count(True) == 1
        assert "Record an offer to draft this" in cards[0 if not cards[0].is_enabled() else 1] \
            .inner_text()
        # The draft stays locked until the walk-away is typed; then the one card writes a draft.
        # The Profile floor starts the walk-away, so the draft is open; the operator can change it.
        assert page.input_value('[data-testid="of-walk"]') == "$120,000"
        assert page.get_attribute('[data-testid="of-draft-wrap"]', "data-locked") == "0"
        page.fill('[data-testid="of-walk"]', "140000")
        page.dispatch_event('[data-testid="of-walk"]', "change")
        assert page.get_attribute('[data-testid="of-draft-wrap"]', "data-locked") == "0"
        page.click('[data-testid="of-prep-sit"][data-open="1"]')
        page.wait_for_selector('[data-testid="of-dbody"]')
        wait_for_layout(page)
        assert "Draft only" in page.inner_text('[data-testid="of-draft"]')
        assert "Kestrel never sends it" in page.inner_text('[data-testid="of-draft"]')
        assert page.query_selector('[data-of-copy]') is not None
        _clean(page, (".of-nums-list dd b",))
        _shot(page, f"negotiate-empty-draft-{theme}-{width}")
        assert errors == []
    finally:
        ctx.close()


def test_the_preparation_page_rises_in_and_stops_for_reduced_motion(live_empty):
    base, browser, _ = live_empty
    for reduce, expect in (("no-preference", True), ("reduce", False)):
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, reduced_motion=reduce)
        page = ctx.new_page()
        try:
            page.goto(f"{base}/offers?tab=compare")
            page.wait_for_selector('[data-testid="of-empty"]')
            page.click('[data-testid="of-tab-negotiate"]')
            page.wait_for_selector('[data-testid="of-negotiate-prep"]')
            # The pane rise class is added by KUI.paneIn for 400ms; hold it to read the rule.
            names = page.evaluate(
                "(() => { const p = document.querySelector('#of-pane');"
                " p.classList.add('k-pane-in');"
                " return Array.from(document.querySelectorAll('.of-np'))"
                ".map(e => getComputedStyle(e).animationName); })()")
            assert len(names) == 3
            assert all((n != "none") is expect for n in names), (reduce, names)
        finally:
            ctx.close()


def test_menu_outcome_undo_stars_and_weights(live):
    ctx, page, errors = _page(live, "light", 1680, wait='[data-testid="of-card"]')
    try:
        cards = page.query_selector_all('[data-testid="of-card"]')
        assert len(cards) == 2
        assert page.inner_text('[data-testid="of-count"]') == "2 open"
        assert "Ahead on" in page.inner_text('[data-testid="of-summary"]')
        # Stars: the operator's own rating, swapped in place.
        page.click('[data-row="level_scope"] .of-stars button:nth-child(4)')
        page.wait_for_function(
            "document.querySelectorAll('[data-row=\"level_scope\"] .of-stars button.is-on').length "
            ">= 4")
        # A weight moves its word and the summary. The stars' swap settles first: under load
        # the slider read before it settled was an element htmx had not processed yet.
        page.wait_for_function("() => !document.querySelector("
                               "'.htmx-request, .htmx-swapping, .htmx-settling, .htmx-added')")
        slider = page.query_selector('[data-row="total_first_year"] .of-w')
        with page.expect_response(lambda response: response.url.endswith("/offers/weights")
                                  and response.request.method == "POST"):
            slider.evaluate("el => { el.value = 0; el.dispatchEvent(new Event('input', "
                            "{bubbles: true})); }")
        assert page.inner_text('[data-row="total_first_year"] em') == "Ignore"
        wait_for_layout(page)
        # The menu, and I accepted with Undo.
        first = page.query_selector('[data-testid="of-menu-btn"]')
        first.click()
        page.click('[data-testid="of-accepted"]')
        page.wait_for_selector(".k-toast")
        assert "Marked accepted" in page.inner_text(".k-toasts")
        assert page.inner_text('[data-testid="of-count"]') == "1 open"
        assert page.query_selector(".of-card.is-closed") is not None
        page.click(".k-toast__undo")
        # The count rides along out of band, so wait for the body itself to come back.
        page.wait_for_function("!document.querySelector('.of-card.is-closed')")
        assert page.inner_text('[data-testid="of-count"]') == "2 open"
        assert errors == []
    finally:
        ctx.close()


def test_record_sheet_adds_a_third_offer(live_with_pursuit):
    live = live_with_pursuit
    ctx, page, errors = _page(live, "light", 1680, wait='[data-testid="of-card"]')
    try:
        page.click('[data-testid="of-record"]')
        page.wait_for_selector("#of-sheet.is-open")
        _shot(page, "sheet-light-1680")
        assert "Leave unknown amounts blank. Zero means zero." in page.inner_text("#of-sheet")
        page.select_option(
            '[data-testid="of-pursuit"]',
            label="Harbor Point Analytics · Operations Program Manager")
        page.fill('input[name="base"]', "150000")
        page.fill('input[name="bonus_target_pct"]', "5")
        page.fill('input[name="signing"]', "10000")
        page.click('[data-testid="of-save"]')
        page.wait_for_function(
            "document.querySelectorAll('[data-testid=\"of-card\"]').length === 3")
        assert page.query_selector("#of-sheet.is-open") is None
        assert page.inner_text('[data-testid="of-count"]') == "3 open"
        assert errors == []
    finally:
        ctx.close()


def test_negotiate_lock_ask_and_draft(live):
    ctx, page, errors = _page(live, "light", 1680, "/offers?tab=negotiate",
                              '[data-testid="of-negotiate"]')
    try:
        # Locked until the operator sets a walk-away.
        assert page.is_visible('[data-testid="of-locked"]')
        assert page.is_hidden('[data-testid="of-draft"]')
        _shot(page, "negotiate-locked")
        page.fill('[data-testid="of-walk"]', "140000")
        page.dispatch_event('[data-testid="of-walk"]', "change")
        assert page.is_hidden('[data-testid="of-locked"]')
        # An ask below the walk-away is refused and put back.
        page.fill('[data-testid="of-ask"]', "100000")
        page.dispatch_event('[data-testid="of-ask"]', "change")
        assert page.input_value('[data-testid="of-ask"]') == ""
        page.fill('[data-testid="of-ask"]', "150000")
        page.dispatch_event('[data-testid="of-ask"]', "change")
        page.click('[data-sit="equity_vs_cash"]')
        page.wait_for_selector('[data-testid="of-dbody"]')
        body = page.inner_text('[data-testid="of-dbody"]')
        assert "150000" in body
        # The proof point is highlighted, with its source on hover.
        claim = page.query_selector(".of-claim")
        assert claim is not None and "Meadowbrook" in claim.get_attribute("title")
        text = page.inner_text("#of-draft")
        assert "Draft only · never sent" in text and "Kestrel never sends it" in text
        assert page.query_selector('a[href^="mailto:"]') is None
        _shot(page, "negotiate-drafted")
        page.click('[data-testid="of-edit"]')
        assert page.get_attribute('[data-testid="of-dbody"]', "contenteditable") == "true"
        assert errors == []
    finally:
        ctx.close()


def test_the_page_mounts_unmounts_and_mounts_again_with_one_set_of_everything(live):
    """The page lifecycle (D-UI-210): Offers unmounted and mounted again in place starts exactly
    what it started on the document load and leaves nothing running in between. After it, one
    click on a card's menu button opens the menu (a doubled listener opens it and closes it again
    in the same click), Escape closes it, and a page that is unmounted no longer answers."""
    base, browser, _ids = live
    ctx = browser.new_context(viewport={"width": 1680, "height": 900}, color_scheme="light")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(TIMER_PROBE_JS)
    try:
        page.goto(f"{base}/offers")
        page.wait_for_selector('[data-testid="of-card"]')
        wait_for_layout(page)
        first = assert_page_remounts_clean(page, "offers", "offers.js", settle_ms=600)
        assert first["timers"] == 0 and first["observers"] == 0, first
        btn = page.locator('[data-testid="of-menu-btn"]').first
        menu = "#" + btn.get_attribute("data-of-menu")
        btn.click()
        assert page.is_visible(menu)
        assert btn.get_attribute("aria-expanded") == "true"
        page.keyboard.press("Escape")
        assert page.is_hidden(menu)
        # A weight still moves its word once the page is mounted again.
        slider = page.query_selector('[data-row="total_first_year"] .of-w')
        with page.expect_response(lambda r: r.url.endswith("/offers/weights")
                                  and r.request.method == "POST"):
            slider.evaluate("el => { el.value = 0; el.dispatchEvent(new Event('input', "
                            "{bubbles: true})); }")
        assert page.inner_text('[data-row="total_first_year"] em') == "Ignore"
        # Unmounted, the menu button does nothing: its listener went with the page.
        page.evaluate("KUI.page.unmount()")
        btn.click()
        assert page.is_hidden(menu)
        assert errors == []
    finally:
        ctx.close()
