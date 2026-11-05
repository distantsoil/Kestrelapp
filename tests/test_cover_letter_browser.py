"""The cover letter sheet in a real browser (D-UI-239): Send and draft is released only by the tick,
the tabs switch, a traced claim shows its source, Discard asks first, and nothing overlaps at the
three window sizes in light and dark. The model call is faked; nothing reaches the network.

Gated like the other live-browser tests: set KESTREL_LIVE_BROWSER=1."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from tests import role_seed
from tests.browser import launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

BODY = (
    "Dear Hiring Team,\n\n"
    "I rebuilt the demand forecast across three distribution centers and late shipments fell "
    "22 percent.\n\nI am a team player who brings synergy to every site."
)
OVERLAP = """() => {
  const root = document.querySelector('#rl-letter.is-open');
  const els = [...root.querySelectorAll('h3,p,li,span,a,button,label,b,u,mark,div')]
    .filter(e => e.offsetParent !== null && e.getBoundingClientRect().width > 0
      && getComputedStyle(e).display !== 'inline' && getComputedStyle(e).visibility !== 'hidden'
      && [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()));
  const bad = [];
  for (const e of els) {
    const ox = getComputedStyle(e).overflowX;
    if (e.scrollWidth > e.clientWidth + 1 && !['auto', 'scroll'].includes(ox))
      bad.push('clip ' + e.textContent.trim().slice(0, 30));
  }
  // Only what the person can see counts: a rectangle is cut to the scrolling or clipping
  // ancestors it sits in, and text scrolled out of sight is not overlapping anything.
  const seen = e => {
    let R = e.getBoundingClientRect();
    let left = R.left, top = R.top, right = R.right, bottom = R.bottom;
    for (let p = e.parentElement; p && p !== document.body; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (/(auto|scroll|hidden|clip)/.test(s.overflowX + s.overflowY)) {
        const q = p.getBoundingClientRect();
        left = Math.max(left, q.left); top = Math.max(top, q.top);
        right = Math.min(right, q.right); bottom = Math.min(bottom, q.bottom);
      }
    }
    return right > left && bottom > top ? {left, top, right, bottom} : null;
  };
  const r = els.map(e => [e, seen(e)]).filter(x => x[1]);
  for (let i = 0; i < r.length; i++) for (let j = i + 1; j < r.length; j++) {
    const [a, A] = r[i], [b, B] = r[j];
    if (a.contains(b) || b.contains(a)) continue;
    if (Math.min(A.right, B.right) - Math.max(A.left, B.left) > 2
        && Math.min(A.bottom, B.bottom) - Math.max(A.top, B.top) > 2)
      bad.push(a.textContent.trim().slice(0, 25) + ' <> ' + b.textContent.trim().slice(0, 25));
  }
  return {bad, sideways: document.documentElement.scrollWidth > window.innerWidth + 1};
}"""


@pytest.fixture
def live(monkeypatch):
    from playwright.sync_api import sync_playwright

    from kestrel.config import HostedMode
    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import Setting
    from kestrel.llm.client import ChatResult, LLMClient
    from kestrel.services import profile_edit as pe
    from kestrel.web.serve import running_server

    ids = asyncio.run(role_seed.seed_async())
    pe.add_proof_points("employment", [{
        "key": "pp_forecast",
        "claim": "Rebuilt the demand forecast across three distribution centers",
        "evidence": "Weekly review with each center lead",
        "metric": "Late shipments fell 22 percent"}])

    async def go_online() -> None:
        async with get_sessionmaker()() as s:
            s.add_all([
                Setting(key="llm.hosted_mode.enabled", value=True),
                Setting(key="llm.hosted_mode.acknowledged_at", value="2026-10-15T00:00:00Z"),
                Setting(key="llm.hosted_mode.acknowledged_terms",
                        value=HostedMode().disclosure_terms()),
                Setting(key="llm.tiers.tier3.backend", value="frontier"),
                Setting(key="llm.tiers.tier3.model", value="vendor/letter-model"),
            ])
            await s.commit()

    asyncio.run(go_online())
    sent: list[int] = []

    async def complete(self, **kw):
        if "cover letter" not in str(kw.get("messages")).lower():
            return ChatResult(content="{}", model="vendor/letter-model")  # the worker's other jobs
        sent.append(1)
        payload = {"body_md": BODY, "word_count": len(BODY.split()),
                   "claims_used": ["pp_forecast"], "opening_alternatives": []}
        return ChatResult(content=json.dumps(payload), model="vendor/letter-model")

    monkeypatch.setattr(LLMClient, "complete", complete)
    # The worker runs the queued draft (only it does); the sheet polls until the draft is saved.
    with running_server(worker=True) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, ids, sent
        browser.close()


@pytest.mark.parametrize(
    ("scheme", "width", "height"), [("light", 1440, 900), ("dark", 1440, 900), ("light", 390, 844)]
)
def test_the_sheet_gates_the_send_switches_tabs_and_asks_before_discarding(
    live, scheme, width, height
):
    base, browser, ids, sent = live
    ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/opportunities/{ids['strong']}/documents")
    page.wait_for_selector('[data-testid="new-cover-letter"]')
    page.locator('[data-testid="new-cover-letter"]').click()
    page.wait_for_selector('[data-testid="cover-letter-ask"]')
    send = page.locator('[data-testid="rl-send"]')
    assert send.is_enabled() and not sent
    wait_for_layout(page)
    assert page.evaluate(OVERLAP) == {"bad": [], "sideways": False}
    # pressed without the tick: a hint, focus on the box, and nothing is posted or drafted
    posts: list[str] = []
    page.on("request", lambda rq: posts.append(rq.url) if rq.method == "POST" else None)
    assert not page.locator("[data-cl-tick].is-on").count()
    send.click()
    page.wait_for_selector("[data-cl-tick].is-on")
    wait_for_layout(page)
    assert page.locator("[data-cl-tick]").inner_text() == "Tick the box to send"
    assert page.evaluate("document.activeElement.matches('[data-cl-ack]')")
    assert not posts and not sent
    assert page.evaluate(OVERLAP) == {"bad": [], "sideways": False}
    page.locator("[data-cl-ack]").check()
    assert not page.locator("[data-cl-tick].is-on").count()
    send.click()
    page.wait_for_selector('[data-testid="cover-letter-draft"]')
    assert sent, "the draft was asked for only after the confirmed form"
    wait_for_layout(page)
    assert page.evaluate(OVERLAP) == {"bad": [], "sideways": False}
    # a traced claim shows its source on a click
    page.locator('.cl-claim[data-cl-key="pp_forecast"]').first.click()
    assert page.locator('.cl-src.is-on[data-cl-src="pp_forecast"]').is_visible()
    # the tabs switch
    page.locator('[data-cl-tab="edit"]').click()
    assert page.locator("[data-cl-text]").is_visible()
    page.locator('[data-cl-tab="preview"]').click()
    page.wait_for_selector(".cl-pane.is-on .rl-pvframe")
    wait_for_layout(page)
    assert page.evaluate(OVERLAP) == {"bad": [], "sideways": False}
    # Discard asks first: the draft is still there after the first click
    # the action bar is there in every tab, with the primary that belongs to it
    bar = page.locator('[data-testid="cover-letter-actions"]')
    page.locator('[data-cl-tab="check"]').click()
    assert bar.locator(".k-btn--primary:visible").inner_text() == "Edit text"
    for tid in ("cover-letter-preview-go", "cover-letter-redraft", "cover-letter-discard"):
        assert page.locator(f'[data-testid="{tid}"]').is_visible()
    wait_for_layout(page)
    assert page.evaluate(OVERLAP) == {"bad": [], "sideways": False}
    page.locator('[data-testid="cover-letter-edit"]').click()
    assert bar.locator(".k-btn--primary:visible").inner_text() == "Save"
    page.locator('[data-testid="cover-letter-preview-go"]').click()
    assert bar.locator(".k-btn--primary:visible").inner_text() == "Download PDF"
    assert page.locator('[data-testid="cover-letter-edit"]').is_visible()
    wait_for_layout(page)
    assert page.evaluate(OVERLAP) == {"bad": [], "sideways": False}
    # Draft again asks first and keeps the draft when declined
    page.locator('[data-testid="cover-letter-redraft"]').click()
    page.wait_for_selector('[data-cl-confirm="redraft"].is-on')
    page.locator("[data-cl-confirm].is-on [data-cl-keep]").click()
    page.locator('[data-cl-tab="edit"]').click()
    assert page.locator('[data-testid="cover-letter-save"]').is_visible()
    page.locator('[data-testid="cover-letter-discard"]').click()
    page.wait_for_selector('[data-cl-confirm="discard"].is-on')
    page.locator("[data-cl-confirm].is-on [data-cl-keep]").click()
    assert page.locator('[data-testid="cover-letter-draft"]').is_visible()
    page.locator('[data-testid="cover-letter-discard"]').click()
    page.locator('[data-testid="cover-letter-discard-yes"]').click()
    page.wait_for_selector("#rl-letter.is-open", state="detached")
    assert not errors
    ctx.close()


def test_draft_again_online_shows_the_ask_first_panel_and_sends_nothing_until_ticked(live):
    base, browser, ids, sent = live
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.goto(f"{base}/opportunities/{ids['strong']}/documents")
    page.locator('[data-testid="new-cover-letter"]').click()
    page.wait_for_selector('[data-testid="cover-letter-ask"]')
    page.locator("[data-cl-ack]").check()
    page.locator('[data-testid="rl-send"]').click()
    page.wait_for_selector('[data-testid="cover-letter-draft"]')
    first = len(sent)
    page.locator('[data-testid="cover-letter-redraft"]').click()
    page.locator('[data-testid="cover-letter-redraft-yes"]').click()
    page.wait_for_selector('[data-testid="cover-letter-ask"]')
    assert len(sent) == first
    page.locator('[data-testid="rl-send"]').click()
    page.wait_for_selector("[data-cl-tick].is-on")
    assert len(sent) == first
    page.locator("[data-cl-ack]").check()
    page.locator('[data-testid="rl-send"]').click()
    page.wait_for_selector('[data-testid="cover-letter-draft"]')
    assert len(sent) > first
    ctx.close()


@pytest.mark.parametrize(("width", "height"), [(1440, 900), (390, 844)])
def test_preview_keeps_the_action_bar_in_view_and_the_text_off_the_page_edge(
    live, width, height
):
    base, browser, ids, sent = live
    ctx = browser.new_context(viewport={"width": width, "height": height})
    page = ctx.new_page()
    page.goto(f"{base}/opportunities/{ids['strong']}/documents")
    page.locator('[data-testid="new-cover-letter"]').click()
    page.wait_for_selector('[data-testid="cover-letter-ask"]')
    page.locator("[data-cl-ack]").check()
    page.locator('[data-testid="rl-send"]').click()
    page.wait_for_selector('[data-testid="cover-letter-draft"]')
    for tab in ("check", "edit", "preview"):
        page.locator(f'[data-cl-tab="{tab}"]').click()
        wait_for_layout(page)
        # the action bar's own buttons are within the window with no scrolling, in every tab
        box = page.locator('[data-testid="cover-letter-actions"]').bounding_box()
        assert box is not None and box["y"] >= 0 and box["y"] + box["height"] <= height + 1, tab
    page.wait_for_selector(".cl-pane.is-on .rl-pvframe")
    wait_for_layout(page)
    dl = page.locator('[data-testid="cover-letter-download"]')
    b = dl.bounding_box()
    assert dl.is_visible() and b is not None and 0 <= b["y"] and b["y"] + b["height"] <= height
    # the page is scaled to the pane, not wider than it
    stage = page.locator(".cl-pane.is-on [data-rl-stage]").bounding_box()
    pg = page.locator(".cl-pane.is-on [data-rl-page]").bounding_box()
    assert pg["width"] <= stage["width"] + 1
    inset = page.evaluate("""() => {
      const d = document.querySelector('.cl-pane.is-on iframe').contentDocument;
      const p = d.querySelector('p');
      return {left: p.getBoundingClientRect().left, right: d.documentElement.clientWidth
        - p.getBoundingClientRect().right,
        closing: d.querySelector('.closing') ? d.querySelector('.closing').innerText : ''};
    }""")
    assert inset["left"] > 0 and inset["right"] > 0
    assert inset["closing"].startswith("Sincerely,")
    ctx.close()
