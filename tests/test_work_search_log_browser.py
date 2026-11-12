"""The work search log sheet on Applications in a real browser (D-EXPORT-1): the button opens the
sheet, a quick pick or a date change re-counts, the count equals the rows of the saved file, and
nothing overlaps or scrolls sideways in light, dark and narrow.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1. Set KESTREL_SHOT_DIR to keep
a screenshot of each state."""

from __future__ import annotations

import csv
import io
import os
import re
from pathlib import Path

import pytest

from tests import apps_seed
from tests.browser import launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

_KIDS = """el => Array.from(el.querySelectorAll('*')).filter(e => e.offsetParent !== null
    && e.children.length === 0 && (e.textContent || '').trim())
  .map(e => { const b = e.getBoundingClientRect();
    return {t: e.textContent.trim().slice(0, 30), l: b.left, r: b.right, tp: b.top,
            bt: b.bottom}; })"""


def _shot(page, name: str) -> None:
    out = os.environ.get("KESTREL_SHOT_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(out) / f"{name}.png"))


def _overlaps(a, b) -> bool:
    return a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1 and a["tp"] < b["bt"] - 1 \
        and b["tp"] < a["bt"] - 1


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    apps_seed.seed("employment")
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


def _open(live, theme: str, width: int):
    base, browser = live
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/pursuits?track=employment")
    page.wait_for_selector('[data-testid="pcard"]')
    wait_for_layout(page)
    page.click('[data-testid="work-search-log"]')
    page.wait_for_selector('[data-testid="wsl-count"]')
    page.wait_for_timeout(500)  # the sheet's own entrance
    return ctx, page, errors


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 1280), ("light", 390)])
def test_the_sheet_opens_counts_and_does_not_overlap(live, theme, width):
    ctx, page, errors = _open(live, theme, width)
    try:
        _shot(page, f"wsl-{theme}-{width}")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        kids = page.evaluate(_KIDS, page.query_selector("#ap-wsl"))
        for i, a in enumerate(kids):
            for b in kids[i + 1:]:
                assert not _overlaps(a, b), (a, b)
        box = page.eval_on_selector("#ap-wsl", "e => {const b = e.getBoundingClientRect();"
                                    " return [b.left, b.right, innerWidth]}")
        assert box[0] >= -1 and box[1] <= box[2] + 1, box
        assert not errors, errors
    finally:
        ctx.close()


def test_a_date_change_recounts_and_the_count_equals_the_saved_rows(live):
    ctx, page, errors = _open(live, "light", 1280)
    try:
        page.fill('[data-testid="wsl-from"]', "2000-01-01")
        page.fill('[data-testid="wsl-to"]', "2099-12-31")
        page.dispatch_event('[data-testid="wsl-to"]', "change")
        page.wait_for_function(
            "document.querySelector('[data-testid=wsl-count]').textContent.includes('Jan 1')")
        text = page.inner_text('[data-testid="wsl-count"]')
        n = int(re.match(r"(\d+) application", text).group(1))
        _shot(page, "wsl-range-counted")
        href = page.get_attribute('[data-testid="wsl-save"]', "href")
        body = page.evaluate("async h => (await fetch(h)).text()", href)
        rows = list(csv.reader(io.StringIO(body.lstrip("﻿"))))
        assert n >= 1, n
        dated = [r for r in rows[1:] if r[2]]
        assert len(dated) == n
        page.click('[data-testid="wsl-pick-last_week"]')
        page.wait_for_function(
            "document.querySelector('[data-testid=wsl-from]').value !== '2000-01-01'")
        assert not errors, errors
    finally:
        ctx.close()


def _withhold_one_application() -> None:
    """Move one applied role onto a source whose terms forbid passing it on (D-SRC-19)."""
    import sqlite3

    from kestrel import paths

    con = sqlite3.connect(paths.db_path())
    try:
        row = {"track": "employment", "kind": "remotive", "name": "Remote board", "config": "{}"}
        for _cid, col, typ, notnull, default, _pk in con.execute("pragma table_info(source)"):
            if notnull and default is None and col not in row and col != "id":
                row[col] = 0 if typ.upper().startswith(("INT", "BOOL", "FLOAT")) else ""
        names = ", ".join(row)
        con.execute(f"insert into source ({names}) values ({', '.join('?' * len(row))})",
                    list(row.values()))
        sid = con.execute("select max(id) from source").fetchone()[0]
        con.execute(
            "update opportunity set source_id = ? where id = (select opportunity_id from pursuit "
            "where applied_at is not null and status in ('applied','screening','interviewing') "
            "order by id limit 1)", (sid,))
        con.commit()
    finally:
        con.close()


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 1280), ("light", 390)])
def test_the_left_out_notice_is_in_words_and_does_not_overlap(live, theme, width):
    _withhold_one_application()
    ctx, page, errors = _open(live, theme, width)
    try:
        page.fill('[data-testid="wsl-from"]', "2000-01-01")
        page.fill('[data-testid="wsl-to"]', "2099-12-31")
        page.dispatch_event('[data-testid="wsl-to"]', "change")
        page.wait_for_selector('[data-testid="wsl-withheld"]')
        page.wait_for_timeout(400)
        text = page.inner_text('[data-testid="wsl-withheld"]')
        assert "left out of this file" in text and "do not allow passing it on" in text
        _shot(page, f"wsl-left-out-{theme}-{width}")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        kids = page.evaluate(_KIDS, page.query_selector("#ap-wsl"))
        for i, a in enumerate(kids):
            for b in kids[i + 1:]:
                assert not _overlaps(a, b), (a, b)
        assert not errors, errors
    finally:
        ctx.close()


def test_focus_stays_on_the_date_just_entered_through_the_swap(live):
    """Fable C4: changing From and tabbing to To replaces both inputs; the keyboard keeps its
    place (htmx returns focus by id) instead of falling to the page."""
    ctx, page, errors = _open(live, "light", 1280)
    try:
        page.fill('[data-testid="wsl-from"]', "2000-01-01")
        page.focus('[data-testid="wsl-from"]')
        page.keyboard.press("Tab")
        page.wait_for_function(
            "document.querySelector('[data-testid=wsl-count]').textContent.includes('Jan 1')")
        page.wait_for_function("() => !document.querySelector('.htmx-request')")
        assert page.evaluate("document.activeElement.getAttribute('data-testid')") in (
            "wsl-to", "wsl-from")
        assert page.evaluate("document.activeElement.tagName") == "INPUT"
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 1280), ("light", 390)])
def test_a_backwards_range_keeps_the_typed_dates_and_does_not_overlap(live, theme, width):
    """Fable C8: the message appears beside the dates just typed, and the sheet stays clean."""
    ctx, page, errors = _open(live, theme, width)
    try:
        page.fill('[data-testid="wsl-from"]', "2026-05-10")
        page.wait_for_function("() => !document.querySelector('.htmx-request')")
        page.wait_for_timeout(400)  # the first change's own swap replaces the inputs
        page.fill('[data-testid="wsl-to"]', "2026-05-04")
        page.dispatch_event('[data-testid="wsl-to"]', "change")
        page.wait_for_selector('[data-testid="wsl-error"]')
        page.wait_for_timeout(300)
        assert page.input_value('[data-testid="wsl-from"]') == "2026-05-10"
        assert page.input_value('[data-testid="wsl-to"]') == "2026-05-04"
        _shot(page, f"wsl-bad-range-{theme}-{width}")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
        assert not errors, errors
    finally:
        ctx.close()
