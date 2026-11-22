"""D-TEST-801 (issue #236): tests/browser.py art_visible_left hides layers to measure the scene art
and must leave the page as it found it, whatever happens, and must not be fooled by a visibility
transition. Runs on a small page of its own (no server); skips when no browser starts."""

from __future__ import annotations

import pytest

from tests.browser import art_visible_left, launch_sync

PAGE = """<!doctype html><meta charset=utf-8>
<style>
  body { margin: 0; background: #fff; }
  .k-scene { position: relative; width: 300px; height: 120px; }
  .k-scene__art { position: absolute; left: 20px; top: 10px; width: 100px; height: 60px;
                  background: #c00; transition: visibility 3s linear; }
  .k-scene__touch { left: 200px; width: 30px; background: #00c; }
  #plain, #styled { position: absolute; left: 40px; top: 20px; width: 20px; height: 20px;
                    background: #0a0; transition: visibility 3s linear; }
</style>
<div class="k-scene" data-animate="1">
  <div class="k-scene__art"></div>
  <div class="k-scene__art k-scene__touch"></div>
  <div id="plain"></div>
  <div id="styled" style="opacity: 0.99"></div>
</div>"""

STATE_JS = """() => ({
  plain: document.getElementById('plain').getAttribute('style'),
  styled: (document.getElementById('styled').getAttribute('style') || '').replace(/;$/, ''),
  art: document.querySelector('.k-scene__art:not(.k-scene__touch)').getAttribute('style'),
  touch: document.querySelector('.k-scene__touch').getAttribute('style'),
  animate: document.querySelector('.k-scene').getAttribute('data-animate'),
  stray: document.querySelectorAll('[data-k-style], [data-was-animate]').length,
})"""

UNTOUCHED = {"plain": None, "styled": "opacity: 0.99", "art": None, "touch": None,
             "animate": "1", "stray": 0}


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = launch_sync(p)
        page = browser.new_page(viewport={"width": 400, "height": 200})
        page.set_content(PAGE)
        yield page
        browser.close()


def test_the_page_is_left_exactly_as_found_no_empty_style_attribute(page):
    assert page.evaluate(STATE_JS) == UNTOUCHED
    left = art_visible_left(page)
    assert left is not None
    assert page.evaluate(STATE_JS) == UNTOUCHED  # no style="" on an element that had none


def test_a_screenshot_error_still_restores_the_page(page, monkeypatch):
    real = page.screenshot
    calls = []

    def _second_one_fails(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("screenshot failed")
        return real(*args, **kwargs)

    monkeypatch.setattr(page, "screenshot", _second_one_fails)
    with pytest.raises(RuntimeError, match="screenshot failed"):
        art_visible_left(page)
    assert len(calls) == 2
    assert page.evaluate(STATE_JS) == UNTOUCHED  # nothing is left hidden


def test_a_visibility_transition_cannot_hide_the_art_from_the_measure(page):
    # The art fades its visibility over 3 s. Without `transition: none` on the hidden art the
    # "hidden" shot still shows it, the two shots match and the helper reports no art at all.
    left = art_visible_left(page)
    assert left is not None and 19 <= left <= 21, left
