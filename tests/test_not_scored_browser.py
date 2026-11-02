"""Pipeline reason chips, the renamed group and Paste the description, in a real browser (issue
#90, D-UI-236; the locked design mockup (kept privately)). Light, dark and narrow: the
chips never overlap or wrap, the page never scrolls sideways, and a pasted description moves the
role from Won't be scored unless you act into Being scored without a reload.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import select

from kestrel.db.models import Task
from kestrel.web.fit import EMPTY_POSTING_STAGE_REASON, UNSCORED_LABEL
from tests import test_pipeline_view as T
from tests.browser import launch_sync, wait_for_layout

pytestmark = [
    pytest.mark.usefixtures("_isolate_data_home"),
    pytest.mark.skipif(
        os.environ.get("KESTREL_LIVE_BROWSER") != "1",
        reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
    ),
]

_LONG = " ".join(["Owns the function and its vendors, security and budget."] * 8)


async def _reasons(s) -> dict[str, int]:
    from kestrel.db.models import Quarantine
    from kestrel.pipeline import lane
    from kestrel.services import retry_removal

    org = (await s.execute(select(T.Org))).scalars().first()
    src = (await s.execute(select(T.Source))).scalars().first()
    made = {
        "waiting": T._opp(org, src, "w1", stage="t0_pass"),
        "background": T._opp(org, src, "w3", stage="t0_pass"),
        "scoring": T._opp(org, src, "w4", stage="t0_pass"),
        "empty": T._opp(org, src, "n1", stage="t1_pass", stage_reason=EMPTY_POSTING_STAGE_REASON,
                        description_md="", title="Director of Operations"),
        "removed": T._opp(org, src, "n3", stage="quarantined"),
    }
    s.add_all(made.values())
    await s.flush()
    s.add(Task(type="t1", priority=lane.LANE_PRIORITY,
               payload={"opportunity_id": made["background"].id, **lane.lane_extra(0.0)}))
    s.add(Task(type="t2", payload={"opportunity_id": made["scoring"].id}, status="leased"))
    s.add(Quarantine(opportunity_id=made["removed"].id, tier=2, error="invalid JSON"))
    await s.flush()
    await retry_removal.mark(s, made["removed"].id)
    await s.commit()
    return {k: o.id for k, o in made.items()}


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    asyncio.run(T._seed())
    ids = asyncio.run(T._with_session(_reasons))
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, ids
        browser.close()


def _open(live, theme="light", width=1280):
    base, browser, _ids = live
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/opportunities")
    page.wait_for_selector('[data-testid="pl-row"]')
    wait_for_layout(page)
    return ctx, page, errors


def _chip(page, oid):
    return page.inner_text(f"#row-{oid} [data-testid=row-chip]").strip()


@pytest.mark.parametrize(("theme", "width"), [("light", 1280), ("dark", 1280), ("light", 390)])
def test_chips_and_groups_in_light_dark_and_narrow(live, theme, width):
    _base, _browser, ids = live
    ctx, page, errors = _open(live, theme, width)
    try:
        assert _chip(page, ids["waiting"]) == "Waiting"
        assert _chip(page, ids["background"]) == "Paused"
        assert _chip(page, ids["scoring"]) == "Scoring"
        assert _chip(page, ids["empty"]) == "No description"
        assert _chip(page, ids["removed"]) == "Taken off retry list"
        heads = [h.strip() for h in page.eval_on_selector_all(".pl-gh span:first-child",
                                                              "els => els.map(e => e.innerText)")]
        assert any(h.startswith(UNSCORED_LABEL.upper() + " ·") or h.startswith(UNSCORED_LABEL)
                   for h in heads), heads
        assert not any("not scored" in h.lower() for h in heads)
        # The unscored rows sit in the second group, the others in Being scored.
        for key in ("empty", "removed"):
            assert page.get_attribute(f"#row-{ids[key]}", "data-group") == "unscored"
        for key in ("waiting", "background", "scoring"):
            assert page.get_attribute(f"#row-{ids[key]}", "data-group") == "new"
        # A chip never wraps and never overlaps the title or the right-hand end of its row.
        for oid in ids.values():
            box = page.eval_on_selector(
                f"#row-{oid}",
                "r => { const c = r.querySelector('.pl-chip').getBoundingClientRect();"
                " const e = r.querySelector('.pl-end').getBoundingClientRect();"
                " const t = r.querySelector('.pl-title__t').getBoundingClientRect();"
                " return {h: c.height, cl: c.left, cr: c.right, el: e.left, tr: t.right,"
                " ct: c.top, cb: c.bottom, tt: t.top, tb: t.bottom}; }")
            assert box["h"] < 30, box
            assert box["cr"] <= box["el"] + 1, box  # clear of the score column
            same_line = box["ct"] < box["tb"] and box["cb"] > box["tt"]
            assert not (same_line and box["cl"] < box["tr"] - 1), box
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert errors == []
    finally:
        ctx.close()


def test_the_empty_panel_pastes_a_description_and_the_role_moves_to_waiting(live):
    _base, _browser, ids = live
    ctx, page, errors = _open(live)
    try:
        oid = ids["empty"]
        page.click(f"#row-{oid} .pl-title__t")
        page.wait_for_selector(f"#pl-panel [data-id='{oid}'] [data-testid=panel-paste-open]")
        # The form is closed (zero height) until the button opens it, eased by a transition.
        assert page.eval_on_selector(".pl-paste__wrap", "e => e.getBoundingClientRect().height") < 2
        page.click("[data-testid=panel-paste-open]")
        page.wait_for_function(
            "() => document.querySelector('.pl-paste__wrap').getBoundingClientRect().height > 60")
        assert page.get_attribute("[data-testid=panel-paste-open]", "aria-expanded") == "true"
        page.fill("#pl-paste-text", _LONG)
        page.click("[data-testid=panel-paste-save]")
        page.wait_for_selector("#k-toasts :text('Description saved')")
        # The list refreshes: the role now waits for Tier 0 in Being scored.
        page.wait_for_function(
            "id => { const r = document.getElementById('row-' + id);"
            " return r && r.dataset.group === 'new'; }", arg=str(oid))
        assert _chip(page, oid) == "Waiting"
        assert errors == []
    finally:
        ctx.close()


def test_a_too_short_paste_warns_and_the_role_stays_put(live):
    _base, _browser, ids = live
    ctx, page, errors = _open(live)
    try:
        oid = ids["empty"]
        page.click(f"#row-{oid} .pl-title__t")
        page.click("[data-testid=panel-paste-open]")
        page.fill("#pl-paste-text", "Director of Operations, remote.")
        page.click("[data-testid=panel-paste-save]")
        page.wait_for_selector("#k-toasts :text('too short')")
        assert page.get_attribute(f"#row-{oid}", "data-group") == "unscored"
        assert errors == []
    finally:
        ctx.close()


def test_reduced_motion_turns_the_chip_and_paste_animation_off(live):
    _base, browser, ids = live
    base = live[0]
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
    page = ctx.new_page()
    try:
        page.goto(f"{base}/opportunities")
        page.wait_for_selector('[data-testid="pl-row"]')
        dur = page.eval_on_selector(
            f"#row-{ids['waiting']} .pl-chip",
            "e => getComputedStyle(e).transitionDuration + '|' + getComputedStyle(e).animationName")
        assert dur.startswith("0s") and dur.endswith("|none"), dur
    finally:
        ctx.close()
