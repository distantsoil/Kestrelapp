"""Onboarding in a real browser (UI overhaul parity check against the locked mockup,
the locked design mockup (kept privately)). Light and dark at 1280 and 1680, every one
of the six steps: no overlap, no wrapped titles, buttons or pills, no sideways scroll, the help
column beside the step, and the footer inside the window; then the interactions the mockup demos
(modes, fold, chips, the primary's gate, the near-minimum switch).

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1. The browser is the Edge
already on the machine, else a Playwright Chromium (tests/browser.py). Set KESTREL_SHOT_DIR to a
folder to also save a screenshot of every case. The server runs on the test's own throwaway data
directory, never the real one."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

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

_KIDS = """el => Array.from(el.children).filter(e => e.offsetParent !== null)
  .map(e => { const b = e.getBoundingClientRect();
    return {t: (e.textContent || '').trim().slice(0, 30), l: b.left, r: b.right, tp: b.top,
            bt: b.bottom}; })"""

STEPS = ("welcome", "models", "about", "resume", "wants", "searches")


def _overlaps(a, b) -> bool:
    return a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1 and a["tp"] < b["bt"] - 1 \
        and b["tp"] < a["bt"] - 1


def _seed() -> None:
    """A first-run install that has answered everything: both tracks, a name, a resume, terms
    and places, so every step shows its filled state and the searches step has real lines."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.engine import make_async_engine
    from kestrel.db.models import DocMaster
    from kestrel.services import onboarding as ob
    from kestrel.services import profile_edit as pe
    from kestrel.services import setup_state as ss
    from kestrel.services import tracks as tracks_svc

    migrate.run_migrations(paths.db_path())
    about = {"first_name": "Jordan", "last_name": "Rivera", "email": "jordan@example.com",
             "location": "Columbus, OH"}
    for track in tracks_svc.BOTH:
        pe.save_section(track, "about", pe.section_from_form("about", about))
    pe.save_section("employment", "employment", pe.section_from_form("employment", {
        "target_titles": "Operations Analyst\nProcess Analyst", "comp_floor_usd": "65000",
        "mode_remote": "on", "seniority_floor": "", "comp_floor_margin_pct": "0"}))
    pe.save_section("consulting", "consulting", pe.section_from_form("consulting", {
        "service_lines": "Process audits\nInterim lead", "day_rate_floor": "650",
        "min_engagement_days": "20"}))
    for track in tracks_svc.BOTH:
        pe.save_section(track, "location", pe.section_from_form("location", {
            "onsite_metros": "Columbus, OH\nDublin, OH", "remote_ok": "on",
            "remote_country_allowlist": "US"}))

    async def go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                await tracks_svc.save_enabled(s, list(tracks_svc.BOTH))
                s.add(DocMaster(track="employment", name="Imported resume", doc_kind="resume"))
                state = ob.OnboardingState(visited=list(ob.STEP_KEYS[:4]), skipped=[])
                await ob.save(s, state)
                from kestrel.db.models import Setting

                s.add(Setting(key=ss.MODE_KEY, value="local"))
                s.add(Setting(key=ss.CHOICE_KEY, value="both"))
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(go())


def _in_thread(fn, *args, **kwargs) -> None:
    """Run a sync helper that calls asyncio.run on its own thread: Playwright's sync API keeps an
    event loop running on the test thread."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(fn, *args, **kwargs).result()


@pytest.fixture
def live(monkeypatch):
    """A server on the test's own throwaway data directory (the autouse _isolate_data_home gives
    each test a fresh one) with Ollama faked as down and a 4080, and a browser."""
    from playwright.sync_api import sync_playwright

    from kestrel.llm import profiles
    from kestrel.services import setup_state as ss
    from kestrel.services import tracks as tracks_svc
    from kestrel.web.serve import running_server
    from tests.test_setup_page import HW_4080, FakeOllama

    monkeypatch.setattr(profiles, "detect_hardware", lambda: HW_4080)
    monkeypatch.setattr(ss, "OllamaClient", FakeOllama)
    FakeOllama.state = {"up": False, "tags": []}
    ss.forget_hardware()
    ss.reset_actions()
    tracks_svc.forget_cache()
    _seed()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


def _jump(step: str, **fields) -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel import paths
    from kestrel.db.engine import make_async_engine
    from kestrel.services import onboarding as ob

    async def go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                state = await ob.load(s) or ob.OnboardingState()
                state.step = step
                for k, v in fields.items():
                    setattr(state, k, v)
                await ob.save(s, state)
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(go())


_WIDE = """body => { const r = body.getBoundingClientRect(); const out = [];
  for (const e of body.querySelectorAll('*')) { const b = e.getBoundingClientRect();
    if (b.width > 0 && b.right > r.right + 1) out.push(e.tagName + '.' + e.className + ' '
      + Math.round(b.right - r.right)); }
  return [body.scrollWidth, body.clientWidth, ...out.slice(0, 8)]; }"""


def _open(live, theme: str, width: int, step: str, probe: bool = False):
    base, browser = live
    _in_thread(_jump, step)
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    page.add_init_script("window.__errs=[];addEventListener('error',e=>__errs.push(e.message))")
    if probe:
        page.add_init_script(TIMER_PROBE_JS)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/onboarding")
    page.wait_for_selector(f'#ob-step[data-step="{step}"]')
    wait_for_layout(page)
    return ctx, page, errors


def _clear_chips(page, name: str) -> None:
    sel = f'[data-pf-chips="{name}"] [data-pf-chip-remove]'
    for _ in range(20):
        first = page.query_selector(sel)
        if first is None:
            return
        first.click()
    raise AssertionError(f"chips of {name} would not clear: " + str(page.evaluate("window.__errs")))


def _shot(page, theme, width, step, suffix="") -> None:
    out = os.environ.get("KESTREL_SHOT_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(out) / f"onboarding-{step}{suffix}-{theme}-{width}.png"))


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
@pytest.mark.parametrize("step", STEPS)
def test_no_overlap_no_wrapping_no_sideways_scroll(live, theme, width, step):
    ctx, page, errors = _open(live, theme, width, step)
    try:
        _shot(page, theme, width, step)
        assert not errors, errors
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        body = page.query_selector(".ob-body")
        # Measure the settled layout: a spinner's rotation and the pane's slide-in change the
        # scroll box from frame to frame (clipped by overflow-x, so never visible to the operator).
        page.emulate_media(reduced_motion="reduce")
        page.wait_for_timeout(50)
        assert page.evaluate("e => e.scrollWidth <= e.clientWidth + 1", body) is True, (
            page.evaluate(_WIDE, body))
        # No sidebar and no top bar: the wizard is the whole window.
        assert page.is_visible(".side") is False and page.is_visible(".topbar") is False
        wiz = page.evaluate("""() => { const b = document.querySelector('.ob-wiz')
          .getBoundingClientRect(); return [b.left, b.right, b.top, b.bottom, innerWidth]; }""")
        assert wiz[0] <= 1 and wiz[1] >= wiz[4] - 1
        # Titles, step pill, progress labels, buttons and pills stay on one line.
        for sel in ("h1", ".ob-step", ".ob-pl", ".ob-pc", ".ob-foot .k-btn", ".ob-title p",
                    ".k-pill", ".ob-c h4", ".ob-sec", ".ob-pane .k-btn", ".k-hcard h3"):
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    assert page.evaluate(_LINES, el) <= 1, (sel, theme, width, el.inner_text()[:40])
        # No two siblings in these rows overlap.
        for container in (".ob-brand", ".ob-prog", ".ob-foot", ".ob-ch", ".ob-hw", ".ob-flow",
                          ".ob-road", ".ob-keyrow", ".ob-fields", ".ob-two", ".ob-srch"):
            for group in page.query_selector_all(container):
                if not group.is_visible():
                    continue
                kids = page.evaluate(_KIDS, group)
                for i, a in enumerate(kids):
                    for b in kids[i + 1:]:
                        assert not _overlaps(a, b), (container, a, b, theme, width)
        # The help column sits beside the step at both widths (310px or 360px wide), top-aligned
        # with the first card, and the footer is inside the window.
        main = page.evaluate(_KIDS, page.query_selector(".ob-pane"))
        main_box = page.evaluate("""() => { const b = document.querySelector('.ob-main')
          .getBoundingClientRect(); const h = document.querySelector('.ob-help')
          .getBoundingClientRect(); return {ml: b.left, mr: b.right, mt: b.top, hl: h.left,
          hr: h.right, ht: h.top, hw: h.width}; }""")
        assert main_box["hl"] >= main_box["mr"] - 1, main_box
        assert main_box["hw"] <= 365 and main_box["ht"] <= main_box["mt"] + 4, main_box
        assert main
        foot = page.evaluate("""() => { const b = document.querySelector('.ob-foot')
          .getBoundingClientRect(); return [b.top, b.bottom, innerHeight]; }""")
        assert foot[1] <= foot[2] + 1 and foot[0] >= 0
        # Progress row: six items, one current, with the step pill agreeing.
        items = page.query_selector_all(".ob-pi")
        assert len(items) == 6
        assert page.inner_text(".ob-step") == f"Step {STEPS.index(step) + 1} of 6"
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_models_cards_fold_open_and_the_online_card_gates_connect(live, theme):
    ctx, page, errors = _open(live, theme, 1280, "models")
    try:
        # Both is chosen in the seed: the local box, the online card and the flow card are shown.
        for tid in ("ob-local-card", "ob-online-card", "ob-flow"):
            assert page.is_visible(f'[data-testid="{tid}"]'), tid
        _shot(page, theme, 1280, "models", "-both")
        connect = page.query_selector("#ob-connect-btn")
        assert connect.is_disabled()
        assert "Paste your key first" in page.inner_text("[data-ob-connect-hint]")
        page.fill("#ob-key", "sk-or-test-key")
        assert "Tick the box first" in page.inner_text("[data-ob-connect-hint]")
        page.check("#ob-ack")
        assert page.query_selector("#ob-connect-btn").is_enabled()
        # The key link follows the chosen service.
        page.check('input[name="provider"][value="openai"]', force=True)
        shown = page.evaluate("""() => Array.from(document.querySelectorAll('.ob-keylinks li'))
          .filter(l => !l.hidden).map(l => l.dataset.provider)""")
        assert shown == ["openai"]
        # The primary waits for a connected key on Both.
        assert page.query_selector('[data-testid="ob-next"]').is_disabled()
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_free_only_needs_the_training_tick_and_a_refusal_scrolls_into_view(live, theme):
    ctx, page, errors = _open(live, theme, 1280, "models")
    try:
        page.check('input[name="provider"][value="openrouter"]', force=True)
        page.fill("#ob-key", "sk-or-test-key")
        page.check("#ob-ack")
        page.evaluate("document.querySelector('[data-ob-more]').open = true")
        page.check('input[name="free_only"]')
        # Free-only without the training tick is refused by the server: say so before the click.
        assert page.query_selector("#ob-connect-btn").is_disabled()
        assert "Allow providers that may train" in page.inner_text("[data-ob-connect-hint]")
        page.check('input[name="allow_training"]')
        assert page.query_selector("#ob-connect-btn").is_enabled()
        # Force the refused pair past the hint: the result is brought into view, and what was
        # ticked stays ticked, so the redraw never looks like nothing happened.
        page.uncheck('input[name="allow_training"]')
        page.evaluate("document.querySelector('[data-ob-connect]').requestSubmit()")
        page.wait_for_selector('[data-testid="ob-connect-message"][data-connect-ok="false"]')
        wait_for_layout(page)
        in_view = page.evaluate("""() => {
          const m = document.querySelector('[data-testid="ob-connect-message"]');
          const r = m.getBoundingClientRect();
          return r.top >= 0 && r.bottom <= innerHeight;
        }""")
        assert in_view
        assert page.is_checked("#ob-ack") and page.is_checked('input[name="free_only"]')
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_about_you_gate_tracks_summary_and_the_more_details_fold(live, theme):
    ctx, page, errors = _open(live, theme, 1280, "about")
    try:
        assert page.query_selector('[data-testid="ob-next"]').is_enabled()  # a name is saved
        page.fill("#ob-first", "")
        assert page.query_selector('[data-testid="ob-next"]').is_disabled()
        assert "Add your first name first" in page.inner_text("[data-ob-hint]")
        page.fill("#ob-first", "Jordan")
        assert page.query_selector('[data-testid="ob-next"]').is_enabled()
        # Both tracks are on in the seed; untick one and the summary follows.
        page.uncheck('[data-testid="ob-track-consulting"]', force=True)
        assert "Jobs only" in page.inner_text("[data-ob-tsum]")
        page.uncheck('[data-testid="ob-track-employment"]', force=True)
        assert page.query_selector('[data-testid="ob-next"]').is_disabled()
        assert "Pick at least one" in page.inner_text("[data-ob-hint]")
        page.check('[data-testid="ob-track-employment"]', force=True)
        # Add more details unfolds Headline, Phone and the disabled LinkedIn field.
        assert not page.is_visible("#ob-head")
        page.click('[data-ob-fold="ob-more-about"]')
        page.wait_for_selector("#ob-head")
        wait_for_layout(page)
        assert page.is_visible("#ob-head") and page.is_visible("#ob-li")
        assert page.is_disabled("#ob-li")
        _shot(page, theme, 1280, "about", "-open")
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_wants_chips_gate_switch_and_the_live_preview(live, theme):
    ctx, page, errors = _open(live, theme, 1280, "wants")
    try:
        assert page.query_selector('[data-testid="ob-next"]').is_enabled()  # seeded answers
        page.click('[data-ob-fold="ob-more-jobs"]')
        page.click('[data-ob-fold="ob-more-cons"]')
        page.wait_for_selector("#ob-more-jobs")
        page.wait_for_selector("#ob-more-cons")
        wait_for_layout(page)
        _shot(page, theme, 1280, "wants", "-open")
        # Remove every title: the gate closes with the server's hint.
        _clear_chips(page, "target_titles")
        assert page.query_selector('[data-testid="ob-next"]').is_disabled()
        assert "Add a job title first" in page.inner_text("[data-ob-hint]")
        page.fill("#ob-titles", "Operations Analyst")
        page.press("#ob-titles", "Enter")
        assert page.query_selector('[data-testid="ob-next"]').is_enabled()
        assert "Operations Analyst" in page.inner_text("[data-ob-preview]")
        # The near-minimum choice unfolds with the switch.
        assert not page.is_visible("#ob-margin .k-seg")
        page.check("#ob-under", force=True)
        page.wait_for_selector("#ob-margin .k-seg")
        wait_for_layout(page)
        assert page.is_visible("#ob-margin .k-seg")
        # Places or remote: with neither the gate closes.
        _clear_chips(page, "onsite_metros")
        page.uncheck("#ob-remote", force=True)
        assert page.query_selector('[data-testid="ob-next"]').is_disabled()
        assert "Add a place or turn on remote first" in page.inner_text("[data-ob-hint]")
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_searches_lists_the_plain_lines_and_start_is_the_only_primary(live, theme):
    ctx, page, errors = _open(live, theme, 1680, "searches")
    try:
        assert page.is_visible('[data-testid="ob-search-list"]')
        assert len(page.query_selector_all(".ob-srch")) >= 3
        assert page.inner_text('[data-testid="ob-start-searching"]') == "Start searching"
        assert len(page.query_selector_all(".k-btn--primary")) == 1
        assert "Nothing is searching yet" in page.inner_text('[data-testid="ob-search-state"]')
        _shot(page, theme, 1680, "searches", "-full")
        # Edit opens the step it came from, and the footer says where Save goes.
        page.click('[data-testid="ob-edit"]')
        page.wait_for_selector('#ob-step[data-step="wants"]')
        assert "Save and go back to searches" in page.inner_text('[data-testid="ob-next"]')
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("step", ["about", "resume", "wants"])
def test_the_page_mounts_unmounts_and_mounts_again_with_one_set_of_everything(live, step):
    """The page lifecycle (D-UI-210): onboarding mounts Profile's form behavior and its own as one
    page. Unmounted and mounted again in place it starts exactly what it started on the document
    load (chip observers, the drop zone, the leave guard) and leaves nothing running in between.
    After it a fold opens with one press (a doubled listener would open and close it) and the
    leave guard is the page's again."""
    ctx, page, errors = _open(live, "light", 1280, step, probe=True)
    try:
        first = assert_page_remounts_clean(page, "onboarding", "onboarding.js", settle_ms=800)
        assert first["observers"] >= 2  # Profile's preview fit and the chip pop at least
        assert page.evaluate("typeof window.kLeaveGuard") == "function"
        fold = {"about": "ob-more-about", "resume": "ob-paste", "wants": "ob-more-jobs"}[step]
        page.click(f'[data-ob-fold="{fold}"]')
        page.wait_for_selector(f"#{fold}.on")
        assert page.get_attribute(f'[data-ob-fold="{fold}"]', "aria-expanded") == "true"
        page.evaluate("KUI.page.unmount()")
        assert page.evaluate("typeof window.kLeaveGuard") == "undefined"
        assert not errors, errors
    finally:
        ctx.close()


# Every animation frame from the press until the new step has settled: the most visible step
# pane's opacity, how many panes are in the frame, the arriving pane's own opacity, and how many
# animations run inside the leaving copy (its own slide only).
_PANE_FRAMES = """() => { window.__f = []; let n = 0;
  const seen = p => { let o = 1;
    for (let e = p; e && e !== document.body; e = e.parentElement)
      o *= +getComputedStyle(e).opacity;
    return o; };
  const tick = () => {
    const panes = [...document.querySelectorAll('#ob-step .ob-pane')];
    const fresh = document.querySelector('#ob-step .ob-pane:not(.ob-leaving)');
    const copy = document.querySelector('#ob-step .ob-leaving');
    window.__f.push([panes.length ? Math.max(...panes.map(seen)) : 0, panes.length,
                     fresh ? seen(fresh) : 0,
                     copy ? copy.getAnimations({subtree: true}).length : 0]);
    if (++n < 75) requestAnimationFrame(tick); };
  requestAnimationFrame(tick); }"""


def test_a_step_change_never_leaves_the_wizard_blank(live):
    """Owner, 2026-10-30 (videos on macOS and Windows): between steps the content area went blank.
    The old pane faded fully out (htmx "swap:360ms") before the new one faded in from nothing.
    The mockup (go(), obOutF/obInF) runs both at once: the old pane leaves on top while the new
    one arrives. Sampled every frame, Next and then Back: some pane is always well visible, both
    are on screen together for most of the slide, the new one really fades in, nothing inside
    the leaving copy animates again (a chip's pop), and once the slide ends one pane is left."""
    ctx, page, errors = _open(live, "light", 1400, "welcome")
    try:
        for button, step in (("ob-next", "models"), ("ob-back", "welcome")):
            # An animated chip in the old pane: in the leaving copy it must stay still.
            page.evaluate("""() => { const c = document.createElement('span');
              c.className = 'chip ob-pop'; c.textContent = 'x';
              document.querySelector('#ob-step .ob-main').appendChild(c); }""")
            page.evaluate(_PANE_FRAMES)
            page.click(f'[data-testid="{button}"]')
            page.wait_for_selector(f'#ob-step[data-step="{step}"]')
            page.wait_for_timeout(1400)
            frames = page.evaluate("window.__f")
            assert len(frames) >= 30, frames
            dimmest = min(f[0] for f in frames)
            assert dimmest >= 0.5, (button, [round(f[0], 2) for f in frames[:40]])
            together = sum(1 for f in frames if f[1] == 2)
            assert together >= 10, (button, [f[1] for f in frames[:40]])
            assert min(f[2] for f in frames if f[1] == 2) < 0.6, (button, frames[:20])
            assert max(f[3] for f in frames) <= 1, (button, [f[3] for f in frames[:40]])
            assert frames[-1][1] == 1, (button, frames[-6:])
            assert page.evaluate("document.querySelectorAll('[id=\"ob-step\"]').length") == 1
        assert not errors, errors
    finally:
        ctx.close()


def test_an_edit_link_from_searches_slides_back(live):
    """The mockup's Edit on the Searches step goes back to that step (goto: go(g, -1))."""
    ctx, page, errors = _open(live, "light", 1400, "searches")
    try:
        edit = page.locator('form[action^="/onboarding/edit/"] button').first
        edit.click()
        page.wait_for_function(
            "document.querySelector('#ob-step').getAttribute('data-step') !== 'searches'")
        assert page.get_attribute("#ob-step", "data-ob-dir") == "b"
        assert not errors, errors
    finally:
        ctx.close()


def test_with_reduced_motion_a_step_change_is_instant_and_leaves_one_pane(live):
    """Reduced motion stops every animation, so the leaving pane must not linger over the new
    one."""
    base, browser = live
    _in_thread(_jump, "welcome")
    ctx = browser.new_context(viewport={"width": 1400, "height": 900}, reduced_motion="reduce")
    try:
        page = ctx.new_page()
        page.goto(f"{base}/onboarding")
        page.wait_for_selector('#ob-step[data-step="welcome"]')
        page.click('[data-testid="ob-next"]')
        page.wait_for_selector('#ob-step[data-step="models"]')
        assert page.evaluate("document.querySelectorAll('#ob-step .ob-pane').length") == 1
    finally:
        ctx.close()
