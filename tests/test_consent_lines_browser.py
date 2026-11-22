"""Issue #178 in a real browser: the smaller online scoring asks list the exact correction lines.

For the profile notice's Score again, Failed scoring's Try with an online model and the story
sheet's Save and re-score, with one invented correction seeded:
1. the panel lists the correction line, and posts the digest of the lines it shows,
2. the lines block sits inside its panel with no overlap, clipping or sideways scroll, in light
   and dark at 1280 and at 390 wide, and a screenshot of each is saved (KESTREL_SHOT_DIR),
3. Keep it anyway on a role the rules set aside opens Score again, which lists the same line.

Invariant: every consent panel shows exactly what its send transmits. The model list and the
provider are invented; nothing reaches the network.

Set KESTREL_LIVE_BROWSER=1 to run (CI gate: scripts/check.py, tests/browser_map.toml)."""

from __future__ import annotations

import asyncio
import os

import pytest

from tests.browser import default_timeout_ms, launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

MODEL = "vendor/lines-model"
LINE = "I ran the night shift rota for two invented winters."
PROCESS_REQ = "Process improvement across distribution centers"  # on the seeded strong role

# The lines block and every box inside its panel: nothing past the panel's edges, nothing past
# the window, each item's text wholly inside the list (no clipping), no sideways page scroll.
FIT_JS = """(sel) => {
  const block = document.querySelector(sel);
  if (!block) return ['no lines block'];
  const out = [];
  const panel = block.closest('dialog, .k-card, .k-modal, form') || block.parentElement;
  const pb = panel.getBoundingClientRect(), bb = block.getBoundingClientRect();
  if (bb.left < pb.left - 1 || bb.right > pb.right + 1) out.push('block past its panel');
  if (bb.right > innerWidth + 1 || bb.left < -1) out.push('block past the window');
  const list = block.querySelector('[data-correction-lines]');
  const lb = list.getBoundingClientRect();
  for (const li of list.querySelectorAll('li')) {
    const b = li.getBoundingClientRect();
    if (b.right > lb.right + 1 || b.left < lb.left - 1) {
      out.push('line past the list: ' + li.textContent);
    }
    if (li.scrollWidth > li.clientWidth + 1) out.push('line clipped: ' + li.textContent);
  }
  if (document.documentElement.scrollWidth > innerWidth + 1) out.push('sideways scroll');
  return out;
}"""


@pytest.fixture
def live(monkeypatch):
    """A server on the test's own data directory: Hosted mode accepted with an online model for
    both scoring steps, an invented model list, a profile with proof points added after the roles
    were scored, a failure row for the strong role, and a browser."""
    from playwright.sync_api import sync_playwright

    from kestrel.config import HostedMode
    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import Quarantine, Setting
    from kestrel.llm.providers import ModelInfo
    from kestrel.services import hosted_balance as hb
    from kestrel.services import hosted_models as hm
    from kestrel.services import profile_readiness as pr
    from kestrel.web.serve import running_server
    from tests import role_seed

    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")

    async def fake_models(name, backend, *, refresh=False):
        return [ModelInfo(id=MODEL, name="Lines model", context_length=128000,
                          structured_outputs=True, json_mode="schema",
                          prompt_usd_per_m=1.0, completion_usd_per_m=2.0)]

    monkeypatch.setattr(hm, "fetch_models", fake_models)

    async def no_balance(backend):  # the credit lookup would call the provider
        return hb.Reading(False)

    monkeypatch.setattr(hb, "_fetch", no_balance)
    block = {
        "proof_points": [{"claim": "Cut late shipments", "evidence": "22 percent"}],
        "narrative": "An invented narrative.",
        "employment_terms": {"target_titles": ["Operations Analyst"]},
    }
    monkeypatch.setattr(pr, "operator_profile", lambda track: block)

    async def seed() -> dict[str, int]:
        ids = await role_seed.seed_async()
        async with get_sessionmaker()() as s:
            rows = {
                "llm.hosted_mode.enabled": True,
                "llm.hosted_mode.acknowledged_at": "2026-10-15T00:00:00Z",
                "llm.hosted_mode.acknowledged_terms": HostedMode().disclosure_terms(),
            }
            for t in ("tier1", "tier2", "tier3"):
                rows[f"llm.tiers.{t}.backend"] = "frontier"
                rows[f"llm.tiers.{t}.model"] = MODEL
            for key, value in rows.items():
                await s.merge(Setting(key=key, value=value))
            q = Quarantine(opportunity_id=ids["strong"], tier=2, model="m", raw_response="x",
                           error="invalid", attempts=1)
            s.add(q)
            await pr.mark_proof_points_changed(s, "employment")
            await s.commit()
            ids["quarantine"] = q.id
        return ids

    ids = asyncio.run(seed())
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, ids
        browser.close()


def _page(live, theme: str, width: int):
    from kestrel.match_text import requirement_hash

    base, browser, ids = live
    ctx = browser.new_context(viewport={"width": width, "height": 1000}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    r = ctx.request.post(
        f"{base}/opportunities/{ids['strong']}/role/correction",
        form={"h": requirement_hash(PROCESS_REQ), "reason": "not_me", "note": LINE},
        headers={"HX-Request": "true"},
    )
    assert r.ok, r.text()
    return ctx, page, errors


def _shot(page, name: str, theme: str, width: int, target=None) -> None:
    folder = os.environ.get("KESTREL_SHOT_DIR")
    if not folder:
        return
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"consent-lines-{name}-{theme}-{width}.png")
    # The panel's entrance has finished: no animation on it or its ancestors still running (the
    # page's own looping art is left alone).
    page.wait_for_function(
        """() => { const b = document.querySelector('.cl-lines'); if (!b) return true;
          for (let e = b; e; e = e.parentElement) {
            if (e.getAnimations().some(a => a.playState === 'running'
                && a.effect && a.effect.getTiming().iterations !== Infinity)) return false;
          }
          return true; }""")
    (target or page).screenshot(path=path)
    print(f"\nscreenshot: {path}")


def _check(page, sel: str) -> None:
    block = page.locator(sel)
    block.wait_for(state="visible")
    assert LINE in block.inner_text()
    bad = page.evaluate(FIT_JS, sel)
    assert bad == [], bad


def _open_profile(page, base) -> str:
    page.goto(f"{base}/profile")
    wait_for_layout(page)
    page.locator("[data-testid=thin-profile-notice] [data-testid=score-again-go]").click()
    form = page.locator("[data-testid=score-again-ask-form]")
    form.wait_for(state="visible")
    digest = form.locator("input[name=corrections_digest]").get_attribute("value")
    assert digest, "the profile ask posts no corrections digest"
    return "[data-testid=score-again-corrections]"


def _open_retry(page, base, ids) -> str:
    page.goto(f"{base}/quarantine")
    page.wait_for_selector('[data-testid="rt-row"]')
    wait_for_layout(page)
    row = page.locator(f"#rt-{ids['quarantine']}, [data-testid=rt-row]").first
    row.get_by_test_id("rt-model").click()
    # D-TEST-821: the model menu is swapped in by htmx; click its entry once it has settled.
    page.wait_for_selector('[data-testid="rt-online"]')
    wait_for_layout(page)
    page.get_by_test_id("rt-online").click()
    page.locator("#rt-online-dialog").wait_for(state="visible")
    digest = page.locator("#rt-online-corrections").get_attribute("value")
    assert digest, "the online try posts no corrections digest"
    return "#rt-online-dialog [data-testid=rt-corrections]"


def _open_story(page, base, ids) -> str:
    page.goto(f"{base}/opportunities/{ids['strong']}")
    wait_for_layout(page)
    # The sheet's Save and re-score posts here; its answer fills #rl-dialog with the ask.
    page.evaluate(
        "(url) => htmx.ajax('POST', url, {values: {situation: 'An invented situation.', "
        "rescore: '1'}, swap: 'none'})",
        f"/opportunities/{ids['strong']}/role/story",
    )
    page.locator("#rl-online").wait_for(state="visible")
    digest = page.locator("#rl-online-form input[name=corrections_digest]").get_attribute("value")
    assert digest, "the story ask posts no corrections digest"
    return "#rl-online [data-testid=rl-corrections]"


@pytest.mark.parametrize("panel", ["profile", "retry", "story"])
@pytest.mark.parametrize(("theme", "width"), [("light", 1280), ("dark", 1280), ("light", 390),
                                              ("dark", 390)])
def test_the_panel_lists_the_correction_line_and_it_fits(live, panel, theme, width):
    base, _browser, ids = live
    ctx, page, errors = _page(live, theme, width)
    try:
        if panel == "profile":
            sel = _open_profile(page, base)
        elif panel == "retry":
            sel = _open_retry(page, base, ids)
        else:
            sel = _open_story(page, base, ids)
        _check(page, sel)
        _shot(page, panel, theme, width)
        assert not errors, errors
    finally:
        ctx.close()


def test_keep_it_anyway_opens_score_again_with_the_line(live):
    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import Opportunity

    base, _browser, ids = live

    async def aside() -> None:
        async with get_sessionmaker()() as s:
            (await s.get(Opportunity, ids["strong"])).stage = "t0_reject"
            await s.commit()

    # Playwright's sync API runs a loop on this thread; the write runs on its own.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(1) as pool:
        pool.submit(asyncio.run, aside()).result()
    ctx, page, errors = _page(live, "light", 1280)
    try:
        page.goto(f"{base}/opportunities")
        wait_for_layout(page)
        page.click("[data-testid=not-fit-show]")
        # D-TEST-820: htmx binds a swapped-in row when the swap settles, not when it is inserted.
        # Wait for the row, then for the settle (wait_for_layout waits out .htmx-settling), so the
        # click cannot land on a button htmx has not processed yet.
        page.wait_for_selector("#not-fit-box [data-testid=keep-anyway]")
        wait_for_layout(page)
        row = page.locator(f"#not-fit-box .pl-row[data-id='{ids['strong']}']")
        # Give the answer its own wait, so a slow or refused request names itself and the
        # navigation that follows gets a fresh budget.
        with page.expect_response(
            lambda r: "/decide/keep-anyway" in r.url, timeout=default_timeout_ms()
        ) as kept:
            row.locator("[data-testid=keep-anyway]").click()
        assert kept.value.ok, (kept.value.status, kept.value.url)
        assert kept.value.headers.get("hx-redirect"), kept.value.headers
        page.wait_for_url("**/rescore/choose?**")
        page.locator("[data-testid=score-again-corrections]").wait_for(state="visible")
        assert LINE in page.get_by_test_id("score-again-correction-lines").inner_text()
        _shot(page, "keep-anyway", "light", 1280)
        assert not errors, errors
    finally:
        ctx.close()
