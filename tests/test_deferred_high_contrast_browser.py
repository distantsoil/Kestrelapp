"""Deferred scheme behavior in real Edge, both themes and required window widths."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests import test_settings_browser as settings_browser
from tests.test_settings_browser import _KIDS, _LINES, _overlaps
from tests.test_ui_tokens import PAIRS, contrast

pytestmark = [
    pytest.mark.skipif(os.environ.get("KESTREL_LIVE_BROWSER") != "1",
                       reason="launches a real browser; set KESTREL_LIVE_BROWSER=1"),
    pytest.mark.usefixtures("_isolate_data_home"),
]
live = settings_browser.live
SCHEMES = ['contrast']


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_legacy_status_icons_and_palette_follow_high_contrast(live, theme):
    from kestrel import paths

    _base, browser = live
    ctx = browser.new_context(color_scheme=theme)
    try:
        page = ctx.new_page()
        page.set_content('<html data-scheme="contrast"><body><div class="step">'
                         '<i class="ic good">1</i><i class="ic warn">2</i>'
                         '<i class="ic bad">3</i></div><div class="palette">Search</div>'
                         '<span class="saved">Saved</span></body></html>')
        for name in ("kestrel.css", "tokens.css"):
            page.add_style_tag(path=str(Path(paths.web_static_dir()) / "css" / name))
        colors = page.locator('.step .ic').evaluate_all('''els => els.map(e => {
          const s = getComputedStyle(e);
          const hex = c => '#' + c.match(/\\d+/g).slice(0,3)
            .map(v => (+v).toString(16).padStart(2, '0')).join('');
          return [hex(s.color), hex(s.backgroundColor)];
        })''')
        for fg, bg in colors:
            assert contrast(fg, bg) >= 7, (theme, fg, bg)
        assert page.locator('.palette').evaluate('e => getComputedStyle(e).boxShadow') == "none"
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_choose_persist_contrast_layout_keyboard_and_undo(live, scheme, theme, width):
    base, browser = live
    ctx = browser.new_context(viewport={"width": width, "height": 1000}, color_scheme=theme)
    try:
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/settings#preferences")
        choice = page.locator(f'[data-st-scheme="{scheme}"]')
        choice.click()
        page.wait_for_function("s => document.documentElement.dataset.scheme === s", arg=scheme)
        page.wait_for_selector('.k-toast__undo')
        assert choice.get_attribute("aria-checked") == "true"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        for sel in ("h1", ".st-head h3", ".st-card .k-btn", ".st-swb"):
            for el in page.locator(sel).all():
                if el.is_visible():
                    assert el.evaluate(_LINES) <= 1, el.inner_text()
        for group in page.locator('.st-sws, #st-sec-preferences .st-row').all():
            if not group.is_visible():
                continue
            kids = group.evaluate(_KIDS)
            for i, a in enumerate(kids):
                for b in kids[i + 1:]:
                    assert not _overlaps(a, b), (a, b)
        # Measure effective browser tokens, not a separate palette fixture.
        tokens = page.evaluate("""() => {
          const s = getComputedStyle(document.documentElement), out = {};
          for (let i = 0; i < s.length; i++) if (s[i].startsWith('--'))
            out[s[i].slice(2)] = s.getPropertyValue(s[i]).trim();
          return out;
        }""")
        for fg, grounds in PAIRS:
            for bg in grounds:
                assert contrast(tokens[fg], tokens[bg]) >= 7, (theme, scheme, fg, bg)
        choice.focus()
        page.keyboard.press("Tab")
        focused = page.locator(':focus-visible')
        assert focused.count() == 1
        assert focused.evaluate("e => getComputedStyle(e).outlineWidth") == "3px"
        assert contrast(tokens["ink"], tokens["paper"]) >= 7
        page.locator('.k-toast__undo').last.click()
        page.wait_for_function("document.documentElement.dataset.scheme === 'indigo'")
        assert page.evaluate("localStorage.getItem('kestrel-scheme')") == "indigo"
        # Choose again, then verify both local persistence and the server's new-browser default.
        page.locator(f'[data-st-scheme="{scheme}"]').click()
        page.wait_for_function("s => document.documentElement.dataset.scheme === s", arg=scheme)
        page.reload()
        page.wait_for_function("s => document.documentElement.dataset.scheme === s", arg=scheme)
        page.goto(base + '/help')
        assert page.get_attribute('html', 'data-scheme') == scheme
        fresh = browser.new_context(viewport={"width": width, "height": 1000}, color_scheme=theme)
        try:
            other = fresh.new_page()
            other.goto(base + '/help')
            assert other.get_attribute('html', 'data-scheme') == scheme
        finally:
            fresh.close()
        assert not errors, errors
    finally:
        ctx.close()
