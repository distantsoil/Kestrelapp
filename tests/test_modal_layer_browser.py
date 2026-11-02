"""Modals and sheets live in a body-level layer while open (#87, #94, D-UI-235).

A modal rendered deep in a page (or swapped in by htmx) used to sit under an ancestor stacking
context, so the body-level scrim painted over it: dimmed, dead to the mouse. The fix moves every
modal and slide-over into #k-layer when it opens. These tests hit-test with elementFromPoint
(the mouse), not the keyboard, and sample frames while a dialog opens and closes.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest

from tests import prep_seed, profile_seed, tailor_seed
from tests.browser import TIMER_PROBE_JS, assert_page_remounts_clean, launch_sync, wait_for_layout

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

# What is under the middle of an element: the element or a child of it counts as "on top".
_TOP_AT_CENTER = """sel => {
  const el = document.querySelector(sel);
  if (!el) return {found: false};
  const b = el.getBoundingClientRect();
  const hit = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
  return {found: true, onTop: !!hit && el.contains(hit),
          hit: hit ? (hit.id || hit.className || hit.tagName) : null,
          w: b.width, h: b.height};
}"""

# requestAnimationFrame sampler: per frame the scrim and the modal's painted opacity.
_SAMPLER = """sel => {
  window.__frames = [];
  const scrim = document.getElementById('k-scrim');
  let on = true;
  window.__stopFrames = () => { on = false; return window.__frames; };
  const tick = () => {
    const el = document.querySelector(sel);
    const so = scrim ? +getComputedStyle(scrim).opacity : 0;
    const sv = scrim ? getComputedStyle(scrim).visibility : 'hidden';
    const cs = el ? getComputedStyle(el) : null;
    window.__frames.push({scrim: sv === 'hidden' ? 0 : so,
                          modal: cs && cs.visibility !== 'hidden' ? +cs.opacity : 0});
    if (on) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}"""


def _bad_frames(frames: list[dict]) -> list[dict]:
    """A frame where the scrim clearly dims the page but the modal is not there: the grey
    screen with nothing on it."""
    return [f for f in frames if f["scrim"] > 0.5 and f["modal"] < 0.05]


# ----------------------------------------------------------------------------- #87
@pytest.fixture
def profile_live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    master_id = profile_seed.seed()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, master_id
        browser.close()


def _route(**extra) -> dict:
    return {"online": True, "provider": "Test provider", "model": "test/model",
            "dest": "test-destination", "sends": "your resume text", "employer": "visible",
            "hosted_active": True, **extra}


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_online_consent_dialog_is_on_top_and_clickable(profile_live, monkeypatch, scheme):
    """#87: Send and assess sat under the scrim. The button's center is the button, the dialog is
    in the body-level layer, and after Send the dialog shows progress until the answer lands."""
    monkeypatch.setattr("kestrel.services.online_consent.online_route",
                        AsyncMock(return_value=_route()))
    base, browser, master_id = profile_live
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme=scheme)
    page = ctx.new_page()
    held: list = []

    def handler(route):
        if "confirmed=1" in (route.request.post_data or ""):
            held.append(route)  # answered later by the test: the "model is thinking" window
        else:
            route.continue_()

    page.route("**/documents/*/assess", handler)
    try:
        page.goto(f"{base}/documents?master_id={master_id}")
        page.wait_for_selector("#assessment", state="attached")
        wait_for_layout(page)
        page.evaluate(_SAMPLER, f"#doc-assess-{master_id}")
        page.evaluate(
            "id => htmx.ajax('POST', '/documents/' + id + '/assess',"
            " {target: '#assessment', swap: 'innerHTML', values: {}})", master_id)
        dlg = f"#doc-assess-{master_id}"
        page.wait_for_selector(f"{dlg}.is-open")
        wait_for_layout(page)
        assert page.evaluate(f"document.querySelector('{dlg}').parentElement.id") == "k-layer"
        send = '[data-testid="doc-online-send"]'
        top = page.evaluate(_TOP_AT_CENTER, send)
        assert top["found"] and top["onTop"], top  # the mouse reaches Send, not the scrim
        cancel = page.evaluate(_TOP_AT_CENTER, f"{dlg} .k-modal__foot .k-btn:not(.k-btn--primary)")
        assert cancel["onTop"], cancel
        frames = page.evaluate("window.__stopFrames()")
        assert len(frames) > 5
        assert _bad_frames(frames) == [], _bad_frames(frames)[:3]

        page.click(send)  # a real mouse click
        for _ in range(50):
            if held:
                break
            page.wait_for_timeout(50)
        assert held, "Send did not post"
        # Progress, in the dialog, until the result arrives: both buttons off, a status line.
        assert page.is_visible(f"{dlg}.is-open")
        assert page.is_disabled(send)
        assert page.locator(f"{dlg} .k-modal__foot .k-btn").first.is_disabled()
        status = page.locator(f"{dlg} .busy-status")
        assert status.count() == 1 and "Waiting for the answer" in status.inner_text()
        assert page.is_visible(f"{dlg}.is-open"), "the dialog must stay up while it runs"
        held[0].fulfill(status=200, content_type="text/html",
                        body='<p data-testid="done">Assessment result</p>')
        page.wait_for_selector('[data-testid="done"]')
        page.wait_for_selector(dlg, state="detached", timeout=3000)
        assert page.evaluate("document.getElementById('k-scrim').classList.contains('is-on')") \
            is False
        assert page.evaluate("document.getElementById('k-layer').children.length") == 0
    finally:
        ctx.close()


# ----------------------------------------------------------------------------- #94
@pytest.fixture
def prep_live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    ids = prep_seed.seed(with_pack=True)
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, ids
        browser.close()


def _prep_page(prep_live, scheme="light", height=900, probe=False):
    base, browser, ids = prep_live
    opp = ids["Larkspur Logistics"]["opp"]
    ctx = browser.new_context(viewport={"width": 1280, "height": height}, color_scheme=scheme)
    page = ctx.new_page()
    if probe:
        page.add_init_script(TIMER_PROBE_JS)
    page.goto(f"{base}/prep/{opp}")
    page.wait_for_selector("#pp-layout")
    wait_for_layout(page)
    return ctx, page


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_story_sheet_opens_visible_and_clickable_every_time(prep_live, scheme):
    """#94: the second open left the scrim on with no sheet. Open, cancel (mouse), open again,
    three times: each time the sheet is visible and the mouse reaches it; no Escape."""
    ctx, page = _prep_page(prep_live, scheme)
    try:
        for n in range(3):
            page.evaluate(_SAMPLER, "#pp-story-sheet")
            page.click('[data-testid="pp-have-story"]')
            page.wait_for_selector("#pp-story-sheet.is-open", timeout=3000)
            wait_for_layout(page)
            assert page.evaluate("document.querySelector('#pp-story-sheet').parentElement.id") \
                == "k-layer", n
            top = page.evaluate(_TOP_AT_CENTER, "#pp-story-sheet")
            assert top["onTop"] and top["h"] > 100, (n, top)
            assert page.evaluate(_TOP_AT_CENTER, '[data-testid="pp-sheet-save"]')["onTop"], n
            page.click('[data-testid="pp-sheet-cancel"]')
            page.wait_for_selector("#pp-story-sheet", state="hidden", timeout=3000)
            page.wait_for_timeout(450)
            frames = page.evaluate("window.__stopFrames()")
            assert _bad_frames(frames) == [], (n, _bad_frames(frames)[:3])
            assert page.evaluate("document.getElementById('k-scrim').classList.contains('is-on')") \
                is False, n
            assert page.evaluate("document.documentElement.classList.contains('k-scroll-held')") \
                is False, n
            assert page.evaluate("document.querySelectorAll('[data-k-inert]').length") == 0, n
    finally:
        ctx.close()


def test_saving_a_story_does_not_move_the_page(prep_live):
    """#94: focus without preventScroll and a sheet placed against the page sent Save to the
    bottom. A scrolled page stays where it was."""
    ctx, page = _prep_page(prep_live, height=420)
    try:
        # Bring the link into view first, so Playwright's own click does not scroll the page.
        page.evaluate("document.querySelector('[data-testid=\"pp-have-story\"]')"
                      ".scrollIntoView({block: 'center'})")
        page.wait_for_timeout(200)
        before = page.evaluate("window.scrollY")
        assert before > 20, "the page must be scrollable for this test to mean anything"
        page.click('[data-testid="pp-have-story"]')
        page.wait_for_selector("#pp-story-sheet.is-open")
        wait_for_layout(page)
        page.fill('textarea[name="situation"]', "A short situation.")
        page.click('[data-testid="pp-sheet-save"]')
        page.wait_for_selector("#pp-story-sheet", state="hidden", timeout=5000)
        page.wait_for_timeout(500)
        assert abs(page.evaluate("window.scrollY") - before) <= 2
    finally:
        ctx.close()


def test_an_open_sheet_goes_back_with_its_page(prep_live):
    """The page lifecycle stays leak-free: unmounting Prep with a sheet open closes it and hands
    the layer back (nothing left under <body>, scrim and lock off); the remount is the same."""
    ctx, page = _prep_page(prep_live, probe=True)
    try:
        first = assert_page_remounts_clean(page, "prep", "prep.js", settle_ms=600)
        page.click('[data-testid="pp-have-story"]')
        page.wait_for_selector("#pp-story-sheet.is-open")
        page.evaluate("KUI.page.unmount()")
        assert page.evaluate("document.getElementById('k-layer').children.length") == 0
        assert page.evaluate("document.getElementById('k-scrim').classList.contains('is-on')") \
            is False
        assert page.evaluate("document.documentElement.classList.contains('k-scroll-held')") \
            is False
        assert page.evaluate("KUI.page.mount()") is True
        page.wait_for_timeout(700)
        assert page.evaluate("KUI.page._stats()") == first
    finally:
        ctx.close()


# ----------------------------------------------------------------------------- every modal
_OPEN_EVERY_MODAL = """async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const out = [];
  const modals = Array.from(document.querySelectorAll('.k-modal')).filter(m => m.id);
  for (const el of modals) {
    const home = el.parentElement;
    KUI.openModal(el);
    await wait(380);
    const r = el.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    const inLayer = el.parentElement && el.parentElement.id === 'k-layer';
    const onTop = !!hit && el.contains(hit);
    KUI.closeModal();
    await wait(600);
    out.push({id: el.id, inLayer, onTop, hit: hit ? (hit.id || hit.className) : null,
              back: el.parentElement === home,
              clean: !document.getElementById('k-scrim').classList.contains('is-on')
                && !document.documentElement.classList.contains('k-scroll-held')
                && document.querySelectorAll('[data-k-inert]').length === 0});
  }
  return out;
}"""


def _assert_every_modal(page, minimum: int) -> None:
    results = page.evaluate(_OPEN_EVERY_MODAL)
    assert len(results) >= minimum, results
    for r in results:
        assert r["inLayer"] and r["onTop"] and r["back"] and r["clean"], r


def test_every_modal_on_the_profile_documents_page_opens_on_top(profile_live):
    base, browser, master_id = profile_live
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{base}/documents?master_id={master_id}")
    page.wait_for_selector("#preview [data-pf-page] iframe")
    wait_for_layout(page)
    _assert_every_modal(page, 1)
    page.close()


def test_every_modal_on_the_prep_page_opens_on_top(prep_live):
    ctx, page = _prep_page(prep_live)
    try:
        page.click('[data-testid="pp-have-story"]')  # brings the sheet in
        page.wait_for_selector("#pp-story-sheet.is-open")
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        _assert_every_modal(page, 1)
    finally:
        ctx.close()


def test_every_modal_on_the_settings_page_opens_on_top():
    from playwright.sync_api import sync_playwright

    from kestrel.db import migrate
    from kestrel.web.serve import running_server

    migrate.run_migrations()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"{base}/settings")
        page.wait_for_selector('[data-testid="st-nav-computer"]')
        wait_for_layout(page)
        _assert_every_modal(page, 0)
        browser.close()


def test_every_modal_on_the_tailor_page_opens_on_top():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    ids = tailor_seed.seed(config=None)
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"{base}/renders/{ids['render']}")
        page.wait_for_selector("#tl-frame")
        wait_for_layout(page)
        _assert_every_modal(page, 1)
        browser.close()


# ------------------------------------------------------------ the layer's htmx handling (#87)
_MAKE_HOST = """() => {
  const host = document.createElement('div');
  host.className = 'box';
  host.setAttribute('hx-target', 'closest .box');
  host.innerHTML = '<input name="a"><div class="k-modal" id="t-m" role="dialog" aria-modal="true"'
    + ' aria-hidden="true"><div class="k-modal__body"><button type="button">x</button></div></div>';
  document.body.appendChild(host);
  KUI.openModal('#t-m');
  const m = document.getElementById('t-m');
  return {target: m.getAttribute('hx-target'), host: host.id, layered: m.parentElement.id};
}"""


def test_a_relative_hx_target_keeps_pointing_at_the_original_ancestor(profile_live):
    """hx-target="closest .box" on a page wrapper means the wrapper. After the modal moves to the
    body-level layer it must still mean that wrapper (resolved to an absolute selector)."""
    base, browser, master_id = profile_live
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{base}/documents?master_id={master_id}")
    page.wait_for_selector("#preview [data-pf-page] iframe")
    got = page.evaluate(_MAKE_HOST)
    assert got["layered"] == "k-layer" and got["host"]
    assert got["target"] == "#" + got["host"], got
    page.close()


_RETURNED_OVERLAY = """async style => {
  const box = document.createElement('div');
  box.id = 't-box';
  box.setAttribute('hx-swap', style);
  box.innerHTML = '<div class="k-modal" id="t-ret" role="dialog" aria-modal="true"'
    + ' aria-hidden="true"><div class="k-modal__body"><button type="button">x</button></div></div>';
  document.body.appendChild(box);
  KUI.openModal('#t-ret'); await new Promise(r => setTimeout(r, 400));
  KUI.closeModal(); await new Promise(r => setTimeout(r, 700));  // handed back, marked returned
  const returned = document.getElementById('t-ret').hasAttribute('data-k-returned');
  const ev = new CustomEvent('htmx:beforeSwap', {detail: {
    target: box, elt: box, shouldSwap: true, serverResponse: '<p>x</p>',
    xhr: {getResponseHeader: () => null}}});
  document.dispatchEvent(ev);
  return {returned, stillThere: !!document.getElementById('t-ret')};
}"""


@pytest.mark.parametrize("style,removed", [
    ("innerHTML", True), ("outerHTML", True), ("beforeend", False), ("afterbegin", False),
    ("none", False)])
def test_a_returned_overlay_goes_only_when_the_swap_replaces_its_container(
        profile_live, style, removed):
    base, browser, master_id = profile_live
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{base}/documents?master_id={master_id}")
    page.wait_for_selector("#preview [data-pf-page] iframe")
    got = page.evaluate(_RETURNED_OVERLAY, style)
    assert got["returned"] is True
    assert got["stillThere"] is (not removed), (style, got)
    page.close()
