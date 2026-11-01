"""Issue #72 / D-UI-229: on Profile > Documents the "Import a resume or capability statement" panel
sits inline in the LEFT column (.pf-dmain), directly under Optional sections, at every width
(including the stacked layout), not at the bottom of the page and not in a sheet."""

from __future__ import annotations

import os
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from tests import profile_seed

_VOID = {"meta", "link", "input", "br", "hr", "img", "source", "path", "circle", "rect", "line",
         "polyline", "use", "col", "area", "base", "wbr"}


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, dict]] = []
        self.parents: dict[str, list[tuple[str, str]]] = {}
        self.order: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        ident = a.get("id") or ""
        cls = a.get("class") or ""
        keys = [f"#{ident}"] if ident else []
        keys += [f".{c}" for c in cls.split()]
        for k in keys:
            self.parents[k] = [(t, (d.get("id") or "") and f"#{d['id']}" or d.get("class") or "")
                               for t, d in self.stack]
            self.order.append(k)
        if tag not in _VOID:
            self.stack.append((tag, a))

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        while self.stack:
            t, _ = self.stack.pop()
            if t == tag:
                break


def _documents_html() -> str:
    from kestrel.web.app import build_app

    profile_seed.seed()
    with TestClient(build_app(worker=False)) as c:
        return c.get("/documents").text


def test_import_panel_is_inline_in_the_left_column_right_after_optional_sections():
    html = _documents_html()
    tree = _Tree()
    tree.feed(html)
    chain = lambda key: [c for _t, c in tree.parents[key]]  # noqa: E731
    imp, opt = chain("#pf-import"), chain("#optional-sections")
    # Same parent as Optional sections: the left column (.pf-dmain) inside the grid.
    assert imp == opt and imp[-1] == "pf-dmain" and "pf-dgrid" in imp
    assert "pf-dprev" not in imp
    assert tree.order.index("#optional-sections") < tree.order.index("#pf-import")
    assert "#import-form" in tree.order and "#import-proposal" in tree.order
    # Nothing but the optional-sections block sits between them.
    assert html.index('id="optional-sections"') < html.index('id="pf-import"')
    assert html.index('id="pf-import"') < html.index('class="pf-dprev"')


@pytest.mark.skipif(os.environ.get("KESTREL_LIVE_BROWSER") != "1",
                    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1")
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [390, 1000, 1200, 1280, 1680, 1920])
def test_import_panel_sits_under_optional_sections_in_the_left_column(width, theme):
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server
    from tests.browser import launch_sync, wait_for_layout

    master_id = profile_seed.seed()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        page = browser.new_page(viewport={"width": width, "height": 1300}, color_scheme=theme)
        page.goto(f"{base}/documents?master_id={master_id}")
        page.wait_for_selector("#pf-import")
        wait_for_layout(page)
        m = page.evaluate("""() => {
          const r = s => document.querySelector(s).getBoundingClientRect();
          const imp = document.querySelector('#pf-import');
          const clipped = [...imp.querySelectorAll('h3, p, label, button, select, textarea')]
            .filter(e => e.getBoundingClientRect().right > imp.getBoundingClientRect().right + 1)
            .map(e => e.tagName);
          const wide = [...document.querySelectorAll('.pf-dmain *')]
            .filter(e => e.offsetParent && !e.closest('.pf-seg') &&
              e.getBoundingClientRect().width > 0 &&
              e.getBoundingClientRect().right > document.querySelector('.pf-dmain')
                .getBoundingClientRect().right + 1).map(e => e.tagName + '.' + e.className);
          const kind = document.querySelector('[data-testid=doc-delete-btn]');
          return {imp: r('#pf-import'), opt: r('#optional-sections'), main: r('.pf-dmain'),
                  prev: r('.pf-dprev'),
                  seg: [...document.querySelectorAll('.pf-seg')]
                    .map(e => e.scrollWidth - e.clientWidth), wide,
                  del: kind && kind.getBoundingClientRect().height,
                  clipped, sx: document.documentElement.scrollWidth - innerWidth}; }""")
        browser.close()
    assert abs(m["imp"]["left"] - m["main"]["left"]) <= 1
    assert abs(m["imp"]["width"] - m["main"]["width"]) <= 1
    assert m["imp"]["top"] >= m["opt"]["bottom"] - 1  # below Optional sections
    assert m["imp"]["bottom"] <= m["main"]["bottom"] + 1  # inside the left column
    assert m["clipped"] == [] and m["sx"] <= 1
    if width >= 1000:  # the Look pickers fit without their own sideways scroll
        assert all(d <= 1 for d in m["seg"]), m["seg"]
    assert m["wide"] == [], m["wide"]  # nothing in the editor column sticks out of it
    if width >= 1200:  # D-UI-230: editor left, live preview right, tops aligned
        assert m["prev"]["left"] >= m["main"]["right"], (width, m["prev"], m["main"])
        assert abs(m["prev"]["top"] - m["main"]["top"]) <= 4
    else:  # stacked: the preview sits under the editor
        assert m["prev"]["top"] >= m["main"]["bottom"] - 1
        assert abs(m["prev"]["left"] - m["main"]["left"]) <= 1
