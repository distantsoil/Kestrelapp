"""Issue #271: the score ring in the browser extension's role summary keeps the score and
"out of 5" inside its inner circle. Every corner of each text run (the glyphs' own box, a Range)
stays at least 2 px inside the circle's edge, light and dark, at the popup's own width and a
narrower one. The app's role page has its own check (tests/test_score_ring_fit_browser.py).

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1. Reuses the extension test's
seeded Kestrel, test board and unpacked extension (tests/test_extension_browser.py)."""

from __future__ import annotations

import os

import pytest

from tests import test_extension_browser as ext
from tests.test_extension_browser import KEY, _ext_page, _popup_for, _store

live = ext.live  # the extension test's fixture: seeded Kestrel, test board, unpacked extension

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

# The smallest gap, in px, from any corner of each text run to the inner circle's edge.
_FIT_JS = """() => {
  const inner = document.querySelector('.ring > span');
  if (!inner) return null;
  const c = inner.getBoundingClientRect();
  const cx = c.left + c.width / 2, cy = c.top + c.height / 2;
  const rad = Math.min(c.width, c.height) / 2;
  return [...inner.querySelectorAll('b, small')].map(el => {
    const rg = document.createRange(); rg.selectNodeContents(el);
    const r = rg.getBoundingClientRect();
    let gap = Infinity;
    for (const [x, y] of [[r.left, r.top], [r.right, r.top], [r.left, r.bottom],
                          [r.right, r.bottom]]) {
      gap = Math.min(gap, rad - Math.hypot(x - cx, y - cy));
    }
    return {t: el.textContent.trim(), gap, w: r.width, h: r.height, rad};
  });
}"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_popup_score_ring_keeps_its_text_inside(live, scheme):
    helper, _ = _ext_page(live, "options.html")
    _store(helper, {"port": live["port"], "secret": KEY})
    helper.close()
    url = f"http://127.0.0.1:{live['board']}/jobs/101"
    tab, page, errors = _popup_for(live, url, scheme, "summary")
    try:
        page.wait_for_selector(".ring b")
        for width in (380, 320):
            page.set_viewport_size({"width": width, "height": 600})
            page.wait_for_function(f"innerWidth === {width}")
            runs = page.evaluate(_FIT_JS)
            assert runs and {r["t"] for r in runs} >= {"out of 5"}, runs
            for r in runs:
                assert r["gap"] >= 2, (scheme, width, r)
        assert not errors
    finally:
        page.close()
        tab.close()
