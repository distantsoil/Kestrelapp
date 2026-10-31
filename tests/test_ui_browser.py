"""Real-browser checks for the design system (UI overhaul, Phase 1): the effective cascade with
both stylesheets loaded, and overlay behavior (synchronous centering, Escape, focus return,
stacking, modality). Gated like the other live-browser test: set KESTREL_LIVE_BROWSER=1.
Headless Chromium is found through Playwright's own install, or /opt/pw-browsers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kestrel import paths
from kestrel.web import deps
from tests.browser import launch_sync, wait_for_layout

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

_STATIC = Path(paths.web_static_dir())

_PAGE = """<!doctype html>
<html lang="en" data-theme="light"><head><meta charset="utf-8">
{% for sheet in sheets %}<link rel="stylesheet" href="{{ base }}/css/{{ sheet }}">
{% endfor %}</head>
<body class="k-win" style="display:block">
{% import "ui/macros.html" as m %}
<main id="page">
  <button id="t-fly" data-k-flyout="#fly1">Open flyout</button>
  <button id="t-so" data-k-slideover="#so1">Open slide-over</button>
  <button id="t-md" data-k-modal="#md1">Open modal</button>
  <button id="t-so-tall" data-k-slideover="#so2">Open tall slide-over</button>
  {{ m.segmented("mode", [("a", "A"), ("b", "B")], "a") }}
  {{ m.stepper("n", 2, 0, 3) }}
</main>
{% call m.flyout("fly1", "Flyout one") %}<p>Adds something.</p>{% endcall %}
{% call m.flyout("fly2", "Flyout two") %}<p>Over the slide-over.</p>{% endcall %}
{% call m.slide_over("so1", "Details") %}
  <button id="t-fly2" data-k-flyout="#fly2">More</button><p>Body</p>{% endcall %}
{% call m.slide_over("so2", "Tall") %}{% for i in range(80) %}<p>Line {{ i }}</p>{% endfor %}
{% endcall %}
{% call m.modal("md1", "Confirm", [{"label": "Cancel", "close": True}, {"label": "Go"}]) %}
  <input id="md-in" aria-label="x">{% endcall %}
{% for f in scripts %}<script src="{{ base }}/js/{{ f }}"></script>
{% endfor %}
</body></html>"""


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        # The shared launcher (Edge, then the Playwright Chromium on any OS) as every other
        # browser test uses; its own Linux-only lookup skipped all of these on Windows.
        b = launch_sync(p)
        yield b
        b.close()


@pytest.fixture
def page(browser, tmp_path):
    from test_ui_tokens import _sheet_order

    env = deps.templates().env
    html = env.from_string(_PAGE).render(
        sheets=_sheet_order(), base=_STATIC.as_uri(), scripts=["kestrel-ui.js"])
    f = tmp_path / "page.html"
    f.write_text(html, encoding="utf-8")
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(f.as_uri())
    yield pg
    assert not errors, errors
    ctx.close()


def _norm(v: str) -> str:
    return v.strip().lower().replace(" ", "")


@pytest.mark.parametrize("scheme", [None, "teal", "plum", "graphite"])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_computed_tokens_equal_tokens_css_with_both_sheets_loaded(page, theme, scheme):
    from test_ui_tokens import _token_values

    page.evaluate(
        "([t, s]) => { const h = document.documentElement; h.setAttribute('data-theme', t);"
        " if (s) h.setAttribute('data-scheme', s); else h.removeAttribute('data-scheme'); }",
        [theme, scheme],
    )
    want = _token_values(theme, scheme or "indigo")
    for name in ("paper", "ink", "bg", "line", "muted", "good", "bad", "brand", "soft"):
        got = page.evaluate(
            "n => getComputedStyle(document.documentElement).getPropertyValue('--' + n)", name)
        assert _norm(got) == _norm(want[name]), (theme, scheme, name, got, want[name])
    # The legacy alias resolves through the new --paper.
    panel = page.evaluate(
        "() => getComputedStyle(document.documentElement).getPropertyValue('--panel')")
    assert _norm(panel) == _norm(want["paper"])


def _center_gap(page, sel: str) -> float:
    return page.evaluate(
        "s => { const r = document.querySelector(s).getBoundingClientRect();"
        " return Math.abs((r.top + r.bottom) / 2 - innerHeight / 2); }", sel)


@pytest.mark.parametrize(("trigger", "overlay"), [("#t-fly", "#fly1"), ("#t-so", "#so1")])
def test_right_overlays_are_centered_from_the_first_frame(page, trigger, overlay):
    # Measured in the same task as the click and in the next frame, then after the slide-in
    # settles: the vertical center never moves.
    first = page.evaluate(
        """([t, s]) => new Promise(res => {
          document.querySelector(t).click();
          const r0 = document.querySelector(s).getBoundingClientRect();
          const same = Math.abs((r0.top + r0.bottom) / 2 - innerHeight / 2);
          requestAnimationFrame(() => {
            const r = document.querySelector(s).getBoundingClientRect();
            res([same, Math.abs((r.top + r.bottom) / 2 - innerHeight / 2)]);
          });
        })""", [trigger, overlay])
    assert first[0] <= 1 and first[1] <= 1, first
    wait_for_layout(page)
    assert _center_gap(page, overlay) <= 1
    box = page.locator(overlay).bounding_box()
    assert box["x"] + box["width"] >= 1280 - 14  # hugs the right edge


def test_taller_than_the_window_fills_it_and_scrolls_inside(page):
    page.click("#t-so-tall")
    wait_for_layout(page)
    box = page.locator("#so2").bounding_box()
    assert box["y"] >= 11 and box["y"] + box["height"] <= 800 - 11
    body_scrolls = page.evaluate(
        "() => { const b = document.querySelector('#so2 .k-slideover__body');"
        " return b.scrollHeight > b.clientHeight; }")
    assert body_scrolls


def test_escape_closes_and_focus_returns_to_the_trigger(page):
    page.click("#t-fly")
    assert page.evaluate("document.getElementById('fly1').classList.contains('is-open')")
    page.keyboard.press("Escape")
    assert not page.evaluate("document.getElementById('fly1').classList.contains('is-open')")
    assert page.evaluate("document.activeElement.id") == "t-fly"


def test_outside_click_closes_a_flyout(page):
    page.click("#t-fly")
    page.mouse.click(60, 400)
    assert not page.evaluate("document.getElementById('fly1').classList.contains('is-open')")


def test_flyout_stacks_over_a_slide_over_and_leaves_it_open(page):
    page.click("#t-so")
    assert page.evaluate("document.getElementById('page').inert")  # modal: page behind is inert
    page.click("#t-fly2")
    state = page.evaluate(
        "() => ['so1', 'fly2'].map(i => document.getElementById(i).classList.contains('is-open'))")
    assert state == [True, True]
    assert not page.evaluate("document.getElementById('fly2').inert")
    page.keyboard.press("Escape")  # closes the flyout, the topmost
    state = page.evaluate(
        "() => ['so1', 'fly2'].map(i => document.getElementById(i).classList.contains('is-open'))")
    assert state == [True, False]
    assert page.evaluate("document.activeElement.id") == "t-fly2"
    page.keyboard.press("Escape")
    assert not page.evaluate("document.getElementById('so1').classList.contains('is-open')")
    assert not page.evaluate("document.getElementById('page').inert")
    assert page.evaluate("document.activeElement.id") == "t-so"


def test_modal_traps_tab_and_restores_the_page(page):
    page.click("#t-md")
    seen = set()
    for _ in range(6):
        page.keyboard.press("Tab")
        seen.add(page.evaluate("document.activeElement.closest('#md1') !== null"))
    assert seen == {True}  # focus never leaves the dialog
    page.keyboard.press("Escape")
    assert not page.evaluate("document.getElementById('page').inert")


def test_segmented_value_submits_and_stepper_clamps(page):
    page.click(".k-seg button[value='b']")
    assert page.evaluate("document.querySelector('.k-seg input[type=hidden]').value") == "b"
    page.click(".k-stepper [data-k-step='1']")  # 2 -> 3, the max
    assert page.evaluate("document.querySelector('.k-stepper input').value") == "3"
    assert page.evaluate("document.querySelector(\"[data-k-step='1']\").disabled")


def test_toast_with_undo_calls_back_and_dismisses(page):
    page.evaluate("window.__u = 0; KUI.toast('Moved', {undo: () => { window.__u += 1; }})")
    page.click(".k-toast__undo")
    assert page.evaluate("window.__u") == 1
    page.locator(".k-toast").wait_for(state="detached")
    assert page.locator(".k-toast").count() == 0


def test_layout_wait_finishes_finite_animations_and_ignores_infinite_ones(page):
    page.evaluate("""() => {
        window.finite = document.querySelector('#page').animate(
            [{opacity: 0.5}, {opacity: 1}], {duration: 200});
        window.infinite = document.querySelector('#t-fly').animate(
            [{opacity: 0.5}, {opacity: 1}], {duration: 200, iterations: Infinity});
    }""")
    wait_for_layout(page)
    assert page.evaluate("window.finite.playState") == "finished"
    assert page.evaluate("window.infinite.playState") == "running"
    assert page.evaluate("document.fonts.status") == "loaded"
