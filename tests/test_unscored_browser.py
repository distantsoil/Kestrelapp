"""The reasons a role has no score, in a real browser: the Pipeline row's source pill and reason
line, the role page's reason, and the Runs card, in light, dark and at 390 pixels. Nothing
scrolls sideways and no new text is clipped. Set KESTREL_SHOT_DIR to keep screenshots.
Invented text only."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from kestrel.db.models import Quarantine, Task
from kestrel.pipeline import worker as worker_mod
from kestrel.web.fit import DEAD_TASK_MARK, EMPTY_POSTING_STAGE_REASON
from tests import test_pipeline_view as T
from tests.browser import launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = [
    pytest.mark.usefixtures("_isolate_data_home"),
    pytest.mark.skipif(
        os.environ.get("KESTREL_LIVE_BROWSER") != "1",
        reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
    ),
]

_CASES = [("light", 1440), ("dark", 1440), ("light", 390), ("dark", 390)]


async def _reasons(s) -> dict[str, int]:
    org = (await s.execute(select(T.Org))).scalars().first()
    src = (await s.execute(select(T.Source))).scalars().first()
    search = T.Source(track="employment", kind="jobspy", name="Invented search",
                      config={"site_name": ["indeed", "glassdoor"]}, enabled=True)
    s.add(search)
    await s.flush()
    made = {
        "empty": T._opp(org, search, "u1", stage="t1_pass",
                        stage_reason=EMPTY_POSTING_STAGE_REASON, description_md="",
                        title="Director of Invented Infrastructure", raw={"site": "glassdoor"},
                        url="https://www.glassdoor.com/job-listing/invented-u1"),
        "dead": T._opp(org, src, "u2", stage="t0_pass", title="Head of Invented Systems",
                       url="https://www.indeed.com/viewjob?jk=invented-u2"),
        # Issue 196 follow-ups (D-WORKER-6, D-WORKER-9): waiting on a closed engine, and a role
        # whose dead task was quarantined.
        "engine": T._opp(org, src, "u3", stage="t0_pass", title="Lead Invented Planner",
                         url="https://www.indeed.com/viewjob?jk=invented-u3"),
        "failed": T._opp(org, src, "u4", stage="quarantined",
                         title="Manager of Invented Operations",
                         url="https://www.indeed.com/viewjob?jk=invented-u4"),
    }
    s.add_all(made.values())
    await s.flush()
    s.add(Task(type="t1", status="dead", attempts=3, last_error="connection refused",
               payload={"opportunity_id": made["dead"].id}))
    s.add(Task(type="t1", status="pending", payload={"opportunity_id": made["engine"].id}))
    # Its preparation for sorting died too (D-WORKER-6 amended, round 4): said, never silent.
    s.add(Task(type="embed", status="dead", attempts=3, last_error="connection refused",
               payload={"opportunity_id": made["dead"].id}))
    s.add(Quarantine(opportunity_id=made["failed"].id, tier=1, attempts=3,
                       error=DEAD_TASK_MARK + "httpx.ConnectError: connection refused"))
    await s.commit()
    return {k: o.id for k, o in made.items()}


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    asyncio.run(T._seed())
    ids = asyncio.run(T._with_session(_reasons))
    worker_mod._ENGINE_WAIT["tier1"] = worker_mod.ENGINE_DOWN  # conftest clears it after
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, ids
        browser.close()


_CLIPPED = """(sel) => [...document.querySelectorAll(sel)].filter(e => {
  const r = e.getBoundingClientRect();
  return r.width > 0 && (e.scrollWidth > e.clientWidth + 1 || r.right > innerWidth + 1);
}).map(e => e.className || e.tagName)"""


def _visit(live, path, theme, width, ready, shot):
    base, browser, _ids = live
    ctx = browser.new_context(viewport={"width": width, "height": 900 if width > 400 else 844},
                              color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base + path)
    page.wait_for_selector(ready)
    wait_for_layout(page)
    wide = page.evaluate("document.documentElement.scrollWidth - innerWidth")
    out = os.environ.get("KESTREL_SHOT_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.locator(ready).first.scroll_into_view_if_needed()
        page.screenshot(path=str(Path(out) / f"{shot}-{theme}-{width}.png"), full_page=True)
    return ctx, page, errors, wide


@pytest.mark.parametrize(("theme", "width"), _CASES)
def test_pipeline_row_shows_the_board_and_the_reason(live, theme, width):
    ids = live[2]
    ctx, page, errors, wide = _visit(live, "/opportunities", theme, width,
                                     f"#row-{ids['empty']}", "pipeline")
    try:
        assert not errors and wide <= 0
        row = page.locator(f"#row-{ids['empty']}")
        assert row.locator("[data-testid=row-source]").inner_text().strip() == "Glassdoor"
        assert "Glassdoor sent it without one" in row.inner_text()
        assert row.locator("[data-testid=row-unscored-action]").is_visible()
        dead = page.locator(f"#row-{ids['dead']}")
        assert dead.locator("[data-testid=row-chip]").inner_text().strip() == "Stopped"
        engine = page.locator(f"#row-{ids['engine']}")
        assert engine.locator("[data-testid=row-chip]").inner_text().strip() == "Paused"
        assert ("Kestrel cannot reach the AI engine on this computer, so scoring waits."
                in engine.inner_text())
        assert engine.locator("[data-testid=row-unscored-action]").inner_text() == "Open Models"
        failed = page.locator(f"#row-{ids['failed']}")
        why = failed.locator("[data-testid=row-why]").inner_text().strip()
        assert why.startswith("Kestrel could not reach the model.")
        assert page.evaluate(_CLIPPED, ".pl-tag--src, .pl-why") == []
    finally:
        ctx.close()


@pytest.mark.parametrize(("theme", "width"), _CASES)
def test_role_page_says_why_it_stopped(live, theme, width):
    ids = live[2]
    ctx, page, errors, wide = _visit(live, f"/opportunities/{ids['dead']}", theme, width,
                                     "[data-testid=eval-unscored]", "role")
    try:
        assert not errors and wide <= 0
        text = page.inner_text("[data-testid=eval-unscored]")
        assert "Kestrel could not reach the model." in text
        assert "could not prepare this role for sorting" in page.inner_text(
            "[data-testid=eval-vector]")
        assert page.evaluate(_CLIPPED, "[data-testid=eval-unscored], [data-testid=eval-vector]") \
            == []
    finally:
        ctx.close()


@pytest.mark.parametrize(("theme", "width"), _CASES)
def test_runs_card_lists_the_reasons(live, theme, width):
    ctx, page, errors, wide = _visit(live, "/runs", theme, width,
                                     "[data-testid=runs-unscored]", "runs")
    try:
        assert not errors and wide <= 0
        reasons = page.eval_on_selector_all(
            "[data-testid=rn-why]", "els => els.map(e => e.dataset.reason)")
        assert "empty" in reasons and "dead" in reasons and "engine_down" in reasons
        failed = page.locator("[data-testid=rn-why][data-reason=failed]").inner_text()
        # Each role's reason is a sentence of its own, with the cause in plain words (D-WORKER-9).
        assert "Kestrel could not reach the model." in failed
        assert "could not be prepared for sorting" in page.inner_text(
            "[data-testid=rn-why-vector]")
        assert "· scoring did not finish" not in failed
        assert page.evaluate(_CLIPPED, ".rn-why__head, .rn-why__tip, .rn-why__roles li, "
                                       "[data-testid=rn-why-vector] p") == []
    finally:
        ctx.close()
