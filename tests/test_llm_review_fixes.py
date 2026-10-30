"""Regression tests for the hosted-LLM review findings: array bounds and provider 400s, the
penalty routing rule, unknown model capabilities, a voided disclosure acknowledgement, the hosted
description budget, strict versus lenient JSON location, the loop detector, the accounting of a
looping attempt, the toast stack, and reasoning off on OpenRouter. Every provider call is
respx-mocked or a pure body build; nothing reaches a live endpoint."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import respx
from httpx import Response

from kestrel import paths
from kestrel.config import HostedModelCaps, get_settings, hosted_mode_problem
from kestrel.llm.client import (
    ChatResult,
    ExternalBackendError,
    LLMClient,
    ModelDeclined,
    build_request_body,
    json_schema_for,
)
from kestrel.llm.guard import (
    DEGENERATE_REPEAT_LIMIT,
    degenerate_repeat,
    loop_limit_for,
    parse_and_validate,
    run_guarded,
)
from kestrel.llm.schemas import P1Triage, P2Legitimacy
from kestrel.pipeline import _shared

OR_URL = "https://openrouter.ai/api/v1/chat/completions"
_MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "triage this"}]
_VALID = {"verdict": "keep", "confidence": 0.8, "reason": "plausible", "signals": ["level_match"]}


def _or_backend(**hosted):
    be = get_settings().llm.backends["frontier"].model_copy(deep=True)
    for k, v in hosted.items():
        setattr(be.hosted, k, v)
    return be


def _body(backend, model="vendor/model", caps=None, hint=None, schema_cls=P1Triage):
    return build_request_body(
        model=model, messages=[dict(m) for m in _MSGS], backend=backend, temperature=0.0,
        max_tokens=300, schema=json_schema_for(schema_cls), schema_name="t_v1", caps=caps,
        reasoning_hint=hint,
    )


# ------------------------------------------------------------------- 1. array bounds and 400s
def test_array_bounds_only_reach_openrouter_families_known_to_take_them():
    be = _or_backend()
    for model in ("anthropic/claude-sonnet-4.6", "meta-llama/llama-3.3-70b-instruct"):
        schema = _body(be, model, schema_cls=P2Legitimacy)["response_format"]["json_schema"]
        assert "maxItems" not in json.dumps(schema), model
    for model in ("openai/gpt-4.1", "google/gemini-2.5-pro"):
        schema = _body(be, model, schema_cls=P2Legitimacy)["response_format"]["json_schema"]
        assert "maxItems" in json.dumps(schema), model
    oa = get_settings().llm.backends["frontier"].model_copy(update={"provider": "openai"})
    body = _body(oa, "gpt-4.1", schema_cls=P2Legitimacy)
    assert "maxItems" not in json.dumps(body["response_format"])


@respx.mock
def test_a_400_on_a_structured_call_quarantines_with_the_provider_text(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(OR_URL).mock(return_value=Response(400, json={
        "error": {"message": "maxItems must be 0 or 1"}}))
    seen = {}

    async def hook(messages, raw, error, model):
        seen.update(raw=raw, error=error)

    res = asyncio.run(run_guarded(LLMClient(get_settings()), tier="frontier", messages=_MSGS,
                                  model_cls=P1Triage, allow_external=True, on_quarantine=hook))
    assert not res.ok and res.quarantined and route.call_count == 1  # no repair, no retry
    assert "HTTP 400" in seen["error"] and "maxItems must be 0 or 1" in seen["error"]
    assert "maxItems must be 0 or 1" in seen["raw"]


# ------------------------------------------------------------------------------- 2. penalty
def test_penalty_is_sent_only_when_it_cannot_route_a_strict_request_nowhere():
    be = _or_backend(repetition_penalty=1.1, require_parameters=True)
    lists_penalty_only = HostedModelCaps(json_mode="schema", params=["repetition_penalty"])
    assert "repetition_penalty" not in _body(be, caps=lists_penalty_only)
    both = HostedModelCaps(json_mode="schema",
                           params=["repetition_penalty", "structured_outputs"])
    assert _body(be, caps=both)["repetition_penalty"] == 1.1
    assert "repetition_penalty" not in _body(be, caps=None)
    json_mode = HostedModelCaps(json_mode="json_object", params=["response_format"])
    body = _body(be, caps=json_mode)  # require_parameters is off for JSON mode
    assert body["provider"]["require_parameters"] is False and body["repetition_penalty"] == 1.1


# ------------------------------------------------------------------------ 3. unknown caps
def test_unknown_caps_are_sent_as_json_mode_without_require_parameters():
    body = _body(_or_backend(require_parameters=True), caps=HostedModelCaps(json_mode="unknown"))
    assert body["response_format"] == {"type": "json_object"}
    assert body["provider"]["require_parameters"] is False


async def test_assignment_with_no_cached_list_fetches_or_records_unknown(sm, monkeypatch):
    from kestrel.llm import providers as prov
    from kestrel.services import hosted_models as hm

    hm.clear_cache()
    settings = get_settings()
    calls = []

    async def failing(name, backend, *, refresh=False):
        calls.append(name)
        raise hm.ModelListError("offline")

    monkeypatch.setattr(hm, "fetch_models", failing)
    async with sm() as s:
        caps = await hm.caps_for_picks(s, settings, {"tier1": ("frontier", "x/never-listed")})
    assert calls == ["frontier"] and caps["x/never-listed"]["json_mode"] == "unknown"

    jm = prov.ModelInfo(id="x/json", context_length=65536, json_mode="json_object",
                        supported_parameters=["response_format"])

    async def listing(name, backend, *, refresh=False):
        return [jm]

    monkeypatch.setattr(hm, "fetch_models", listing)
    async with sm() as s:
        caps = await hm.caps_for_picks(s, settings, {"tier2": ("frontier", "x/json")})
    assert caps["x/json"]["json_mode"] == "json_object"
    assert caps["x/json"]["context_length"] == 65536


def test_cli_setup_rows_carry_caps_and_the_accepted_disclosure():
    from kestrel import setup as setup_mod
    from kestrel.llm import providers as prov

    settings = get_settings()
    pick = prov.ModelInfo(id="vendor/m", context_length=131072, json_mode="schema",
                          supported_parameters=["structured_outputs"])
    picks = {t: pick for t in setup_mod.PICK_TIERS}
    rows = setup_mod.hosted_rows("frontier", settings.llm.backends["frontier"], picks, "now",
                                 terms=settings.llm.hosted_mode.disclosure_terms())
    assert rows["llm.hosted_model_caps"]["vendor/m"]["json_mode"] == "schema"
    assert rows["llm.hosted_mode.acknowledged_terms"] == (
        settings.llm.hosted_mode.disclosure_terms()
    )


# -------------------------------------------------------------- 4. voided acknowledgement
def _hosted_tier1():
    s = get_settings()
    s.llm.tiers["tier1"].backend = "frontier"
    s.llm.tiers["tier1"].model = "vendor/model"
    s.llm.hosted_mode.enabled = True
    s.llm.hosted_mode.acknowledged_at = "2026-10-01T00:00:00Z"
    return s


@respx.mock
def test_an_acknowledgement_of_an_older_disclosure_is_void_until_accepted_again(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(OR_URL).mock(return_value=Response(200, json={
        "choices": [{"finish_reason": "stop",
                     "message": {"role": "assistant", "content": json.dumps(_VALID)}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
    s = _hosted_tier1()
    hm = s.llm.hosted_mode
    old = hm.model_copy(update={"jd_ceiling_tokens": 3000}).disclosure_terms()
    hm.acknowledged_terms = old  # accepted when the ceiling was 3000; it is 16000 now
    assert old != hm.disclosure_terms() and not hm.active
    assert "changed since you accepted it" in (hosted_mode_problem(s) or "")
    with pytest.raises(ExternalBackendError, match="changed since you accepted it"):
        asyncio.run(LLMClient(s).complete(tier="tier1", messages=_MSGS))
    assert not route.called
    hm.acknowledged_terms = None  # an acknowledgement written before terms were recorded
    assert not hm.active
    hm.acknowledged_terms = hm.disclosure_terms()  # accepted again
    assert hm.active
    asyncio.run(LLMClient(s).complete(tier="tier1", messages=_MSGS))
    assert route.call_count == 1


def test_disclosure_head_matches_the_pipeline_head():
    from kestrel.config import HOSTED_TIER1_HEAD_TOKENS
    from kestrel.pipeline.context import TIER1_HEAD_TOKENS

    assert HOSTED_TIER1_HEAD_TOKENS == TIER1_HEAD_TOKENS


# -------------------------------------------------------------------------- 5. budget math
def _hosted_tier2(ctx: int | None, out: int = 8000):
    s = get_settings()
    s.llm.tiers["tier2"].backend = "frontier"
    s.llm.tiers["tier2"].model = "vendor/model"
    s.llm.tiers["tier2"].max_tokens = out
    s.llm.hosted_mode.enabled = True
    s.llm.hosted_mode.acknowledged_at = "now"
    s.llm.hosted_mode.acknowledged_terms = s.llm.hosted_mode.disclosure_terms()
    if ctx is not None:
        s.llm.hosted_model_caps["vendor/model"] = HostedModelCaps(context_length=ctx)
    return s


def test_budget_never_exceeds_the_window_and_keeps_a_tokenizer_margin():
    s = _hosted_tier2(16384)
    n = _shared.jd_budget(s, "tier2", 6000)
    assert n == int((16384 - 6000 - 8000) * 0.85)  # below the floor: the window wins
    assert n + _shared.HOSTED_PROMPT_OVERHEAD + 8000 <= 16384
    # The floor applies when the window really holds it.
    s = _hosted_tier2(17500)
    assert _shared.jd_budget(s, "tier2", 6000) == 3000
    s = _hosted_tier2(1_000_000)
    assert _shared.jd_budget(s, "tier2", 6000) == 16000  # the disclosed ceiling
    s = _hosted_tier2(8000)
    assert _shared.jd_budget(s, "tier2", 6000) == 1  # nothing fits: never the floor
    # The frontier escalation is hosted in either mode: `external` forces the sizing.
    s = get_settings()
    s.llm.hosted_model_caps[s.llm.tiers["frontier"].model] = HostedModelCaps(context_length=16384)
    assert _shared.jd_budget(s, "frontier", 6000) == 6000  # not hosted mode: local budget
    assert _shared.jd_budget(s, "frontier", 6000, external=True) < 6000


async def test_tier3_preview_sizes_the_description_with_jd_budget(sm, monkeypatch):
    from kestrel.pipeline import tier3
    from tests.test_tier3 import _seed_opp

    seen = {}

    def fake_budget(settings, tier, default, *, external=None):
        seen.update(tier=tier, default=default, external=external)
        return 40

    monkeypatch.setattr(tier3, "jd_budget", fake_budget)
    async with sm() as s:
        oid = await _seed_opp(s, desc="word " * 4000 + "Salary 250000 USD.")
    async with sm() as s:
        preview = await tier3.build_escalation_preview(s, get_settings(), oid)
    assert seen == {"tier": "frontier", "default": get_settings().pipeline.max_jd_tokens,
                    "external": True}
    assert preview.messages and "word " * 200 not in preview.messages[-1]["content"]


# ------------------------------------------------------------- 6. strict versus lenient JSON
class _Fake:
    """A client with a fixed JSON mode and a scripted reply list."""

    def __init__(self, mode: str, replies: list) -> None:
        self.settings = get_settings()
        self.mode = mode
        self.replies = list(replies)
        self.calls = 0

    def json_mode_for(self, tier):
        return self.mode

    def reasoning_hint_applies(self, tier):
        return False

    async def complete(self, **kw):
        self.calls += 1
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return ChatResult(content=r, prompt_tokens=10, completion_tokens=20, cost_usd=0.01)


_PROSE = "Sure! Here is the result: " + json.dumps(_VALID) + " Hope that helps."


def test_prose_around_json_fails_strict_modes_and_passes_prompt_mode():
    assert parse_and_validate(_PROSE, P1Triage, lenient=False)[0] is None
    assert parse_and_validate(_PROSE, P1Triage, lenient=True)[0] is not None
    for mode in ("schema", "json_object"):
        fake = _Fake(mode, [_PROSE, _PROSE])
        res = asyncio.run(run_guarded(fake, tier="tier1", messages=_MSGS, model_cls=P1Triage))
        # Attempt 1 is a failed attempt; the repair is lenient and lands.
        assert res.ok and res.repaired and fake.calls == 2, mode
    fake = _Fake("prompt", [_PROSE])
    res = asyncio.run(run_guarded(fake, tier="tier1", messages=_MSGS, model_cls=P1Triage))
    assert res.ok and not res.repaired and fake.calls == 1


# ------------------------------------------------------------------------- 7. loop detector
def test_the_same_tag_across_many_objects_is_not_a_loop():
    variants = [{"key": f"v{i}", "text": "t", "tags": ["leadership", f"t{i}"], "derived": False}
                for i in range(40)]
    reply = json.dumps({"variants": variants})
    assert degenerate_repeat(reply) is None
    looping = json.dumps({"signals": ["comp_unstated"] * (DEGENERATE_REPEAT_LIMIT + 1)})
    assert "generation loop" in (degenerate_repeat(looping) or "")
    assert loop_limit_for("resume_role.v1") > DEGENERATE_REPEAT_LIMIT
    assert loop_limit_for("triage.v1") == DEGENERATE_REPEAT_LIMIT


@respx.mock
def test_reasoning_exhaustion_is_checked_before_the_loop_detector(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    looping = '{"signals": [' + ", ".join(['"x"'] * 40)
    respx.post(OR_URL).mock(return_value=Response(200, json={
        "choices": [{"finish_reason": "length",
                     "message": {"role": "assistant", "content": looping}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 8000,
                  "completion_tokens_details": {"reasoning_tokens": 7000}}}))
    with pytest.raises(ModelDeclined) as info:
        asyncio.run(LLMClient(get_settings()).complete(
            tier="frontier", messages=_MSGS, schema=json_schema_for(P1Triage),
            allow_external=True))
    assert info.value.reasoning_exhausted and not info.value.degenerate
    assert info.value.completion_tokens == 8000  # billed, so carried for the record


# ---------------------------------------------------------- 8. accounting of a looping reply
def test_a_looping_attempt_is_billed_and_kept_as_the_raw_response():
    loop = '{"signals": [' + ", ".join(['"x"'] * 30)
    declined = ModelDeclined("loop", "{}", degenerate=True, content=loop, prompt_tokens=100,
                             completion_tokens=900, cost_usd=0.5)
    fake = _Fake("schema", [declined, "not json"])
    seen = {}

    async def hook(messages, raw, error, model):
        seen["raw"] = raw

    res = asyncio.run(run_guarded(fake, tier="tier1", messages=_MSGS, model_cls=P1Triage,
                                  on_quarantine=hook))
    assert res.quarantined and res.prompt_tokens == 110 and res.completion_tokens == 920
    assert res.cost_usd == pytest.approx(0.51)
    assert seen["raw"].startswith(loop) and seen["raw"].endswith("--- repair attempt ---\nnot json")


# ----------------------------------------------------------------------------- 9. toasts
_TOAST_STACK = r"""
function el(id) {
  const attrs = {}; const linkAttrs = {}; const texts = [];
  const link = {
    set textContent(v) { texts.push(v); },
    setAttribute: (k, v) => { linkAttrs[k] = v; }, removeAttribute: (k) => { delete linkAttrs[k]; },
    getAttribute: (k) => linkAttrs[k],
  };
  const node = {
    id, hidden: true, texts, attrs,
    querySelector: (q) => (q.indexOf('[href]') >= 0 && !linkAttrs.href ? null : link),
    setAttribute: (k, v) => { attrs[k] = v; }, removeAttribute: (k) => { delete attrs[k]; },
    hasAttribute: (k) => k in attrs,
    closest: (q) => (q === '#' + id ? node : null),
  };
  return node;
}
const sigAttrs = {};
const sig = {
  hasAttribute: (k) => k in sigAttrs,
  getAttribute: (k) => (k in sigAttrs ? sigAttrs[k] : null),
};
const main = el('kestrel-toast'); const note = el('kestrel-toast-note');
const nodes = {'scan-signal': sig, 'kestrel-toast': main, 'kestrel-toast-note': note};
const listeners = {}; const winListeners = {};
const store = {};
global.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
};
global.document = {
  getElementById: (id) => nodes[id] || null,
  addEventListener: (ev, fn) => { listeners[ev] = fn; },
  activeElement: null,
};
const kui = [];
global.window = { addEventListener: (ev, fn) => { winListeners[ev] = fn; },
                  KUI: { toast: (t, o) => { kui.push([t, o.timeout]); } } };
global.requestAnimationFrame = (f) => f();
const timers = [];
global.setTimeout = (f, ms) => { if (ms >= 1000) { timers.push(ms); return 1; } f(); return 0; };
global.clearTimeout = () => {};
eval(SCRIPT);
function poll(s) { Object.assign(sigAttrs, s); listeners['htmx:afterSettle'](); }
poll({'data-ready': '1', 'data-event': '1', 'data-scanning': '0', 'data-new': '0'});
poll({'data-event': '2', 'data-new': '3'});
const scanShown = 'data-show' in main.attrs;
window.kestrelToast('Saved');
const afterPlain = {scanStill: 'data-show' in main.attrs, scanTexts: main.texts.length,
                    noteShown: 'data-show' in note.attrs, timers: timers.slice(), kui: kui};
// Escape on the focused scan toast dismisses it and records the dismissal for other tabs.
document.activeElement = main;
listeners['keydown']({key: 'Escape'});
const afterEsc = {scanShown: 'data-show' in main.attrs, dismissed: store['kestrel-scan-dismissed'],
                  seen: store['kestrel-scan-event']};
// Another tab dismissing the event this tab shows clears it here.
poll({'data-event': '3', 'data-new': '1'});
winListeners['storage']({key: 'kestrel-scan-dismissed', newValue: '3'});
console.log(JSON.stringify({scanShown, afterPlain, afterEsc,
                            otherTab: 'data-show' in main.attrs}));
"""


def test_plain_toasts_stack_under_the_scan_toast_and_dismissal_is_shared(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    html = (Path(paths.web_templates_dir()) / "partials" / "toast.html").read_text("utf-8")
    # One toast system (the locked mockups): the second box is gone; plain toasts use the shared
    # region (KUI.toast), where the scan toast also sits.
    assert 'id="kestrel-toast-note"' not in html and "localStorage" in html
    assert "sessionStorage" not in html
    script = html.split("<script>")[1].split("</script>")[0]
    js = tmp_path / "toast_stack.js"
    js.write_text(f"const SCRIPT = {json.dumps(script)};\n{_TOAST_STACK}", encoding="utf-8")
    out = subprocess.run([node, str(js)], capture_output=True, text=True, timeout=30, check=True)
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["scanShown"] is True
    # The plain toast went to the shared region with its own timer; the scan toast is untouched.
    assert got["afterPlain"] == {"scanStill": True, "scanTexts": 1, "noteShown": False,
                                 "timers": [], "kui": [["Saved", 6000]]}
    assert got["afterEsc"] == {"scanShown": False, "dismissed": "2", "seen": "2"}
    assert got["otherTab"] is False


# --------------------------------------------------------------- 11. reasoning off on OpenRouter
def test_reasoning_off_uses_effort_none_where_the_model_allows_it():
    be = _or_backend()
    allows = HostedModelCaps(json_mode="schema", params=["structured_outputs", "reasoning"],
                             reasoning_mandatory=False)
    assert _body(be, caps=allows, hint="none")["reasoning"] == {"effort": "none"}
    mandatory = allows.model_copy(update={"reasoning_mandatory": True})
    assert _body(be, caps=mandatory, hint="none")["reasoning"] == {"enabled": False}
    assert _body(be, caps=None, hint="none")["reasoning"] == {"enabled": False}
