"""Issue #93: the inline marks on a draft story sheet, in a real browser, light and dark. The marks
sit under the field they belong to, read "Kestrel will not rewrite your story", and never overlap
or run past the sheet. A story the person wrote shows none.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests import prep_seed
from tests.browser import launch_sync, wait_for_layout

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

_FLAGS = [
    {"kind": "style", "rule": "banned_word", "field": "situation", "phrase": "robust"},
    {"kind": "ungrounded", "field": "result", "phrase": "$9M"},
    {"kind": "question", "field": "", "phrase": "What pushback did you get?"},
]


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    ids = prep_seed.seed(with_pack=True)
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, ids
        browser.close()


def _add(title: str, edited: bool) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel import paths
    from kestrel.db.engine import make_async_engine
    from kestrel.db.models import Story

    async def go():
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine)() as session:
            session.add(Story(
                title=title, situation="A robust start.", action="We acted.",
                result="Saved $9M.", is_canonical=True, operator_edited=edited,
                style_flags=_FLAGS,
            ))
            await session.commit()
        await engine.dispose()

    with ThreadPoolExecutor(max_workers=1) as ex:
        ex.submit(asyncio.run, go()).result()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_a_draft_sheet_shows_marks_under_its_fields_and_nothing_overlaps(live, theme):
    base, browser, _ = live
    _add("Draft with marks", edited=False)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.add_init_script(f"try{{localStorage.setItem('kestrel-theme','{theme}')}}catch(e){{}}")
    page.goto(base + "/stories")
    page.wait_for_selector('[data-testid="pp-merge-go"]')
    page.click('[data-testid="pp-merge-go"]')
    page.wait_for_selector("#pp-story-sheet.is-open")
    wait_for_layout(page)
    marks = page.query_selector_all('[data-testid="pp-style-mark"]')
    assert len(marks) == 3
    text = page.inner_text("#pp-story-sheet")
    assert "robust" in text and "$9M" in text and "What pushback did you get?" in text
    assert "Kestrel will not rewrite your story" in text
    boxes = page.evaluate(
        """() => Array.from(document.querySelectorAll(
             '#pp-story-sheet [data-testid="pp-style-mark"]'))
             .map(e => e.getBoundingClientRect().toJSON())"""
    )
    sheet = page.eval_on_selector("#pp-story-sheet", "e => e.getBoundingClientRect().toJSON()")
    for i, a in enumerate(boxes):
        assert a["left"] >= sheet["left"] - 1 and a["right"] <= sheet["right"] + 1
        for b in boxes[i + 1:]:
            assert a["bottom"] <= b["top"] + 1 or b["bottom"] <= a["top"] + 1 \
                or a["right"] <= b["left"] or b["right"] <= a["left"]
    page.close()


def test_a_story_the_person_wrote_shows_no_marks(live):
    base, browser, _ = live
    _add("Mine", edited=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(base + "/stories")
    page.wait_for_selector('[data-testid="pp-story-card"]')
    page.click('[data-testid="pp-story-card"] .pp-scard__open')
    page.wait_for_selector("#pp-story-sheet.is-open")
    assert page.query_selector('[data-testid="pp-style-mark"]') is None
    page.close()
