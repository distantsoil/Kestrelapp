"""D-UI-240 in a real browser, for the four ask-first dialogs that moved to the shared "Tick the
box to send" hint without a browser test of their own: Score again, Run now, Models (change a
step's model) and Model check (Advanced).

For each one, the same four facts:
1. pressed unticked, the button sends nothing (no request is made),
2. "Tick the box to send" shows and focus moves to the box,
3. ticking the box and pressing again goes (the request is made),
4. the hint leaves by fading (opacity passes through values between 1 and 0 on animation
   frames, never a snap), and with the OS "reduce motion" setting it hides at once.

Invariant: nothing is sent to an online model without the ticked box. The model list and the
provider are invented; nothing reaches the network.

Set KESTREL_LIVE_BROWSER=1 to run (CI gate: scripts/check.py, tests/browser_map.toml)."""

from __future__ import annotations

import asyncio
import os

import pytest

from tests.browser import launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

MODEL = "vendor/contract-model"
HINT_TEXT = "Tick the box to send"

# Samples the hint on every animation frame from now on: opacity, visibility, and the time.
TRACE_JS = """(sel) => {
  const el = document.querySelector(sel);
  window.__trace = [];
  const t0 = performance.now();
  const tick = () => {
    const cs = getComputedStyle(el);
    window.__trace.push({t: performance.now() - t0, o: parseFloat(cs.opacity), v: cs.visibility});
    if (performance.now() - t0 < 900) requestAnimationFrame(tick);
    else window.__traceDone = true;
  };
  window.__traceDone = false;
  requestAnimationFrame(tick);
}"""


@pytest.fixture
def live(monkeypatch):
    """A server on the test's own data directory with Hosted mode accepted and a provider key,
    an invented model list, and a browser. Roles, a search and a stored check are seeded."""
    from playwright.sync_api import sync_playwright
    from sqlalchemy import select

    from kestrel.config import HostedMode
    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import Setting, Source
    from kestrel.llm import providers as prov
    from kestrel.llm.profiles import Hardware
    from kestrel.services import hosted_models as hm
    from kestrel.services import setup_state as ss
    from kestrel.web.pages import rescore
    from kestrel.web.serve import running_server
    from tests import role_seed

    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")

    async def fake_models(name, backend, *, refresh=False):
        return [
            prov.ModelInfo(
                id=MODEL,
                name="Contract model",
                context_length=128000,
                structured_outputs=True,
                json_mode="schema",
            )
        ]

    monkeypatch.setattr(hm, "fetch_models", fake_models)
    monkeypatch.setattr(rescore, "_local_models", lambda settings: (["local-one:latest"], None))
    monkeypatch.setattr(
        ss,
        "detected_hardware",
        lambda: Hardware(os="windows", gpu_vendor="nvidia", vram_gb=12.0, ram_gb=32.0),
    )
    monkeypatch.setattr(
        ss, "probe_ollama", lambda settings, fresh=False: ("0.5.0", ["local-one:latest"])
    )

    async def seed() -> dict[str, int]:
        ids = await role_seed.seed_async()
        async with get_sessionmaker()() as s:
            rows = {
                "llm.hosted_mode.enabled": True,
                "llm.hosted_mode.acknowledged_at": "2026-10-15T00:00:00Z",
                "llm.hosted_mode.acknowledged_terms": HostedMode().disclosure_terms(),
            }
            for key, value in rows.items():
                await s.merge(Setting(key=key, value=value))
            source = (
                (await s.execute(select(Source).limit(1)))
                .scalars()
                .first()
            )
            await s.commit()
            ids["source"] = source.id
        return ids

    ids = asyncio.run(seed())
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser, ids
        browser.close()


def _open(live, path: str, *, reduced: bool = False, width: int = 1280):
    base, browser, ids = live
    ctx = browser.new_context(
        viewport={"width": width, "height": 1100},
        color_scheme="light",
        reduced_motion="reduce" if reduced else "no-preference",
    )
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    posts: list[str] = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
    page.goto(base + path.format(**ids))
    wait_for_layout(page)
    return ctx, page, errors, posts


def _unticked_press(page, send, box, hint, posts):
    """Facts 1 and 2: nothing is sent, the hint shows, focus is on the box."""
    from playwright.sync_api import expect

    before = len(posts)
    expect(send).to_be_enabled()
    assert float(send.evaluate("el => getComputedStyle(el).opacity")) == 1.0
    send.click()
    wait_for_layout(page)  # settled, not slept: the hint fades in
    assert len(posts) == before, f"an unticked press sent a request: {posts[before:]}"
    expect(hint).to_be_visible()
    assert hint.inner_text().strip() == HINT_TEXT
    assert box.evaluate("el => el === document.activeElement"), "focus did not move to the box"


def _tick_and_trace(page, box, hint_selector):
    """Fact 4: tick the box while sampling the hint on every frame; the samples."""
    page.evaluate(TRACE_JS, hint_selector)
    box.check()
    page.wait_for_function("window.__traceDone === true")
    return page.evaluate("window.__trace")


def _fades(trace) -> None:
    """The hint left by fading: some frame between the ends, and it ended hidden."""
    ops = [round(s["o"], 3) for s in trace]
    assert ops[-1] == 0 and trace[-1]["v"] == "hidden", ops[-5:]
    assert any(0.05 < o < 0.95 for o in ops), f"the hint snapped away, opacities: {ops[:40]}"
    assert ops == sorted(ops, reverse=True) or max(ops) > 0.5, ops[:40]


def _hides_at_once(trace) -> None:
    """With reduce motion the hint hides on the next frame, with no frame in between."""
    ops = [round(s["o"], 3) for s in trace]
    assert ops[-1] == 0, ops
    assert not any(0.02 < o < 0.98 for o in ops), f"reduce motion still animated: {ops[:40]}"
    first_hidden = next(i for i, o in enumerate(ops) if o == 0)
    assert trace[first_hidden]["t"] < 120, trace[first_hidden]


# ------------------------------------------------------------------------------ Score again
SCORE_AGAIN = "/rescore/choose?opp_ids={strong}&page=1&model=" + MODEL + "&next=/"


def _score_again(live, reduced=False):
    ctx, page, errors, posts = _open(live, SCORE_AGAIN, reduced=reduced)
    dlg = page.locator('[data-testid="score-again-dialog"]')
    return (
        ctx,
        page,
        errors,
        posts,
        dlg.locator('[data-testid="score-again-go"]'),
        dlg.locator('[data-testid="score-again-ok"]'),
        "#score-again-ok-hint",
    )


def test_score_again_dialog_asks_before_an_online_pick_and_fades_the_hint(live):
    ctx, page, errors, posts, send, box, hint_sel = _score_again(live)
    try:
        hint = page.locator(hint_sel)
        _unticked_press(page, send, box, hint, posts)
        _fades(_tick_and_trace(page, box, hint_sel))
        with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/rescore")):
            send.click()
        assert not errors, errors
    finally:
        ctx.close()


def test_score_again_with_usual_models_online_needs_the_box_for_the_usual_pick(live, monkeypatch):
    """The usual models are online and "Same model" is picked: the box is required, an unticked
    press shows the hint and sends nothing (no POST), as in Run now."""
    from kestrel.web.pages import rescore

    monkeypatch.setattr(rescore, "tier2_where", lambda settings: ("online", True))
    ctx, page, errors, posts = _open(live, "/rescore/choose?opp_ids={strong}&page=1&next=/")
    dlg = page.locator('[data-testid="score-again-dialog"]')
    send = dlg.locator('[data-testid="score-again-go"]')
    box = dlg.locator('[data-testid="score-again-ok"]')
    hint_sel = "#score-again-ok-hint"
    try:
        assert dlg.locator('[data-testid="score-again-same"]').is_checked()
        assert box.count() == 1, "the consent box is missing when the usual models are online"
        _unticked_press(page, send, box, page.locator(hint_sel), posts)
        assert not [u for u in posts if u.endswith("/rescore")], posts
        assert not errors, errors
    finally:
        ctx.close()


def test_score_again_hint_hides_at_once_with_reduced_motion(live):
    ctx, page, errors, posts, send, box, hint_sel = _score_again(live, reduced=True)
    try:
        _unticked_press(page, send, box, page.locator(hint_sel), posts)
        _hides_at_once(_tick_and_trace(page, box, hint_sel))
        assert not errors, errors
    finally:
        ctx.close()


# ------------------------------------------------------------------------------ Run now
RUN_NOW = "/sources/{source}/run-now/choose?page=1"


def _pick_hosted_model(page, testid: str) -> None:
    page.locator(f'[data-testid="{testid}-input"]').click()
    page.locator('[role="option"]', has_text="Contract model").first.click()


def _run_now(live, reduced=False):
    ctx, page, errors, posts = _open(live, RUN_NOW, reduced=reduced)
    dlg = page.locator('[data-testid="run-now-dialog"]')
    dlg.locator('[data-testid="run-now-other"]').check()
    _pick_hosted_model(page, "run-now-pick")
    return (
        ctx,
        page,
        errors,
        posts,
        dlg.locator('[data-testid="run-now-go"]'),
        dlg.locator('[data-testid="run-now-ok"]'),
        "#run-now-ok-hint",
    )


def test_run_now_dialog_asks_before_an_online_pick_and_fades_the_hint(live):
    ctx, page, errors, posts, send, box, hint_sel = _run_now(live)
    try:
        _unticked_press(page, send, box, page.locator(hint_sel), posts)
        _fades(_tick_and_trace(page, box, hint_sel))
        with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/run-now")):
            send.click()
        assert not errors, errors
    finally:
        ctx.close()


def test_run_now_hint_hides_at_once_with_reduced_motion(live):
    ctx, page, errors, posts, send, box, hint_sel = _run_now(live, reduced=True)
    try:
        _unticked_press(page, send, box, page.locator(hint_sel), posts)
        _hides_at_once(_tick_and_trace(page, box, hint_sel))
        assert not errors, errors
    finally:
        ctx.close()


# ------------------------------------------------------------------------------ Models change
def _models_change(live, reduced=False):
    ctx, page, errors, posts = _open(live, "/models", reduced=reduced)
    page.get_by_test_id("mo-change-tier2").click()
    page.wait_for_selector("#mo-change-form")
    page.get_by_test_id("mo-where-online").click()
    page.wait_for_selector("[data-testid='mo-online-list'], [data-testid='mo-online-refused']")
    form = page.locator("#mo-change-form")
    return form, ctx, page, errors, posts


def _pick_in_change_list(page) -> None:
    page.locator("[data-testid='mo-online-list']").get_by_text("Contract model").first.click()


def test_models_change_asks_before_an_online_pick_and_fades_the_hint(live):
    form, ctx, page, errors, posts = _models_change(live)
    try:
        _pick_in_change_list(page)
        send = form.get_by_test_id("mo-change-save")
        box = form.get_by_test_id("mo-confirm-online")
        _unticked_press(page, send, box, page.locator("#mo-confirm-online-hint"), posts)
        _fades(_tick_and_trace(page, box, "#mo-confirm-online-hint"))
        with page.expect_request(lambda r: r.method == "POST" and "/models/change/" in r.url):
            send.click()
        assert not errors, errors
    finally:
        ctx.close()


def test_models_change_hint_hides_at_once_with_reduced_motion(live):
    form, ctx, page, errors, posts = _models_change(live, reduced=True)
    try:
        _pick_in_change_list(page)
        send = form.get_by_test_id("mo-change-save")
        box = form.get_by_test_id("mo-confirm-online")
        _unticked_press(page, send, box, page.locator("#mo-confirm-online-hint"), posts)
        _hides_at_once(_tick_and_trace(page, box, "#mo-confirm-online-hint"))
        assert not errors, errors
    finally:
        ctx.close()


# ------------------------------------------------------------------------------ Model check
def _bench_advanced(live, reduced=False):
    ctx, page, errors, posts = _open(live, "/bench?hosted=1", reduced=reduced)
    adv = page.locator("#ck-advanced")
    adv.locator("summary").click()
    form = adv.locator("form", has=page.locator('[data-testid="bench-run-btn"]'))
    form.locator('select[name="backend"]').select_option("frontier")
    form.locator('select[name="tier"]').select_option("tier2")
    _pick_hosted_model(page, "bench-run-model")
    return form, ctx, page, errors, posts


def test_bench_advanced_asks_before_an_online_pick_and_fades_the_hint(live):
    form, ctx, page, errors, posts = _bench_advanced(live)
    try:
        send = form.get_by_test_id("bench-run-btn")
        box = form.get_by_test_id("ck-adv-confirm")
        _unticked_press(page, send, box, page.locator("#ck-adv-confirm-hint"), posts)
        _fades(_tick_and_trace(page, box, "#ck-adv-confirm-hint"))
        with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/bench/run")):
            send.click()
        assert not errors, errors
    finally:
        ctx.close()


def test_bench_advanced_hint_hides_at_once_with_reduced_motion(live):
    form, ctx, page, errors, posts = _bench_advanced(live, reduced=True)
    try:
        send = form.get_by_test_id("bench-run-btn")
        box = form.get_by_test_id("ck-adv-confirm")
        _unticked_press(page, send, box, page.locator("#ck-adv-confirm-hint"), posts)
        _hides_at_once(_tick_and_trace(page, box, "#ck-adv-confirm-hint"))
        assert not errors, errors
    finally:
        ctx.close()
