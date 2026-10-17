"""Loop-resistant repair, reasoning-off repairs, Tier 1 reasoning off, preflight vs doctor
(D-LLM-5). From the operator's hosted diagnostics: Tier 2 replies looped inside
legitimacy.signals until the 8,000-token cap, and the repair looped again with reasoning on.
Every provider call is respx-mocked or a pure request-body build; nothing reaches a live
endpoint."""

from __future__ import annotations

import asyncio
import json

import respx
from httpx import Response

from kestrel.config import HostedModelCaps, TierConfig, apply_mode, get_settings
from kestrel.llm.client import LLMClient, build_request_body, json_schema_for
from kestrel.llm.guard import loop_site, run_guarded, run_prompt
from kestrel.llm.prompts import load_prompt
from kestrel.llm.schemas import P1Triage, P2CLegitimacy, P2Legitimacy

OR_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "nvidia/nemotron-test:free"
_MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "evaluate this"}]
_VALID_P1 = {"verdict": "keep", "confidence": 0.8, "reason": "plausible",
             "signals": ["level_match"]}
_LEGIT_VALID = {"status": "suspect", "confidence": 0.6, "signals": ["no_company_detail"]}
_LEGIT_LOOP = (
    '{"status": "suspect", "confidence": 0.6, "signals": ["no_company_detail", '
    + ", ".join(['"no_comp_no_location_no_team"'] * 200)
)
_P1_LOOP = (
    '{"verdict": "keep", "confidence": 0.7, "reason": "x", "signals": ["level_match", '
    + ", ".join(['"comp_unstated"'] * 60)
)


def _reply(content: str, finish: str = "stop", reasoning: int | None = None,
           used: int = 1200) -> Response:
    usage: dict = {"prompt_tokens": 900, "completion_tokens": used}
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return Response(200, json={
        "choices": [{"finish_reason": finish,
                     "message": {"role": "assistant", "content": content}}],
        "usage": usage,
    })


def _hosted(params: list[str] | None = None, *, mandatory: bool | None = False, **hosted):
    """Hosted mode on, tier1 and tier2 on OpenRouter with the hosted budgets (D-LLM-1)."""
    s = get_settings().model_copy(deep=True)
    for tier in ("tier1", "tier2"):
        s.llm.hosted_tiers[tier] = TierConfig(backend="frontier", model=MODEL)
    s.llm.mode = "hosted"
    hm = s.llm.hosted_mode
    hm.enabled = True
    hm.acknowledged_at = "2026-10-15T10:00:00+00:00"
    hm.acknowledged_terms = hm.disclosure_terms()
    for k, v in hosted.items():
        setattr(s.llm.backends["frontier"].hosted, k, v)
    s.llm.hosted_model_caps[MODEL] = HostedModelCaps(
        json_mode="schema",
        params=params if params is not None else ["structured_outputs", "reasoning"],
        reasoning_mandatory=mandatory,
    )
    s = apply_mode(s)
    assert s.llm.mode == "hosted" and s.llm.tiers["tier2"].max_tokens == 8000
    return s


def _sent(route, i: int) -> dict:
    return json.loads(route.calls[i].request.content)


def _guard(s, *, tier="tier2", model_cls=P2Legitimacy, schema_name="evaluate_v1", **kw):
    return asyncio.run(run_guarded(LLMClient(s), tier=tier, messages=_MSGS, model_cls=model_cls,
                                   schema_name=schema_name, **kw))


# ------------------------------------------------------------ 1. repair reasoning is off
@respx.mock
def test_repair_after_a_loop_runs_with_reasoning_off(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(OR_URL).mock(side_effect=[
        _reply(_LEGIT_LOOP, finish="length", reasoning=1000, used=8000),
        _reply(json.dumps(_LEGIT_VALID)),
    ])
    # The prompt's own hint was "low": the first attempt carries it, the repair must not.
    res = _guard(_hosted(), reasoning_hint="low")
    assert res.ok and res.repaired and route.call_count == 2
    assert _sent(route, 0)["reasoning"] == {"effort": "low"}
    assert _sent(route, 1)["reasoning"] == {"effort": "none"}


@respx.mock
def test_repair_after_reasoning_exhausted_runs_with_reasoning_off(monkeypatch):
    """Attempt 1 spent the cap thinking; the re-ask (reasoning off) answered with bad JSON; the
    repair of THAT reply stays reasoning off instead of going back to the prompt's hint."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(OR_URL).mock(side_effect=[
        _reply("", finish="length", reasoning=8000, used=8000),
        _reply('{"status": "suspect"'),
        _reply(json.dumps(_LEGIT_VALID)),
    ])
    res = _guard(_hosted(), reasoning_hint="low")
    assert res.ok and res.repaired and route.call_count == 3
    assert _sent(route, 0)["reasoning"] == {"effort": "low"}
    assert _sent(route, 1)["reasoning"] == {"effort": "none"}
    assert _sent(route, 2)["reasoning"] == {"effort": "none"}


@respx.mock
def test_an_ordinary_repair_keeps_the_attempts_own_hint_and_budget(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(OR_URL).mock(side_effect=[
        _reply('{"status": "suspect"'), _reply(json.dumps(_LEGIT_VALID))])
    res = _guard(_hosted(), reasoning_hint="low", repair_max_tokens=2500)
    assert res.ok and route.call_count == 2
    repair = _sent(route, 1)
    assert repair["reasoning"] == {"effort": "low"} and repair["max_tokens"] == 8000
    assert "looped" not in repair["messages"][-1]["content"]


# ------------------------------------------------------------ 2. loop-resistant repair
@respx.mock
def test_loop_repair_names_field_and_value_and_caps_max_tokens(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(OR_URL).mock(side_effect=[
        _reply(_LEGIT_LOOP, finish="length", used=8000), _reply(json.dumps(_LEGIT_VALID))])
    res = _guard(_hosted(), repair_max_tokens=2500)
    assert res.ok
    first, repair = _sent(route, 0), _sent(route, 1)
    assert first["max_tokens"] == 8000 and repair["max_tokens"] == 2500
    text = repair["messages"][-1]["content"]
    assert '"no_comp_no_location_no_team"' in text and "`signals`" in text
    assert ("List each flag at most once; at most 3 legitimacy flags; stop after the closing "
            "brace") in text
    assert "—" not in text


@respx.mock
def test_a_second_loop_fails_fast_and_quarantines_with_both_replies(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(OR_URL).mock(side_effect=[
        _reply(_LEGIT_LOOP, finish="length", used=8000),
        _reply(_LEGIT_LOOP, finish="length", used=2500),
    ])
    seen = {}

    async def hook(messages, raw, error, model):
        seen.update(raw=raw, error=error)

    res = _guard(_hosted(), repair_max_tokens=2500, on_quarantine=hook)
    assert not res.ok and res.quarantined and route.call_count == 2
    assert _sent(route, 1)["max_tokens"] == 2500
    assert "--- repair attempt ---" in seen["raw"] and "generation loop" in seen["error"]


@respx.mock
def test_hosted_loop_repair_never_caps_below_the_prompts_own_budget(monkeypatch):
    """D-LLM-6: on a hosted tier the loop repair runs at the larger of the prompt's
    repair_max_tokens and its own max_tokens. P2 declares 4000 (its legitimate reply runs 2,500
    to 3,000 tokens); a 2,500 cap cut good replies off on free Nemotron."""
    from kestrel.llm.prompts import load_prompt

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    spec = load_prompt("evaluate/v1")
    assert spec.max_tokens == 4000 and spec.repair_max_tokens == 2500
    route = respx.post(OR_URL).mock(side_effect=[
        _reply(_LEGIT_LOOP, finish="length", used=8000), _reply(json.dumps(_LEGIT_VALID))])
    res = _guard(_hosted(), max_tokens=spec.max_tokens, repair_max_tokens=spec.repair_max_tokens)
    assert res.ok
    first, repair = _sent(route, 0), _sent(route, 1)
    assert first["max_tokens"] == 8000  # the hosted floor (D-LLM-1)
    assert repair["max_tokens"] == 4000  # not 2500: half the cap, never below the prompt's


def test_loop_site_reports_the_dotted_field_path():
    nested = '{"blocks": {}, "legitimacy": ' + _LEGIT_LOOP
    site = loop_site(nested)
    assert site is not None and site.field == "legitimacy.signals"
    assert site.value == "no_comp_no_location_no_team" and site.count == 200
    assert loop_site(json.dumps({"legitimacy": _LEGIT_VALID})) is None


def test_evaluation_prompts_declare_a_schema_sized_repair_budget():
    assert load_prompt("evaluate/v1").repair_max_tokens == 2500
    assert load_prompt("evaluate_consulting/v1").repair_max_tokens == 3000
    for version in ("triage/v1", "triage_consulting/v1"):
        assert load_prompt(version).repair_max_tokens == 600


@respx.mock
def test_run_prompt_threads_the_repair_budget(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(OR_URL).mock(side_effect=[
        _reply(_P1_LOOP, finish="length", used=1200), _reply(json.dumps(_VALID_P1))])
    spec = load_prompt("triage/v1")
    res = asyncio.run(run_prompt(LLMClient(_hosted()), spec, _MSGS))
    assert res.ok and _sent(route, 1)["max_tokens"] == 600
    assert "List each value at most once" in _sent(route, 1)["messages"][-1]["content"]


@respx.mock
def test_repair_cap_is_not_applied_while_the_operator_keeps_reasoning_on(monkeypatch):
    """An operator-set effort wins over the hint, so reasoning stays on and a small cap would be
    eaten by thinking: the repair keeps the tier budget."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(OR_URL).mock(side_effect=[
        _reply(_LEGIT_LOOP, finish="length", used=8000), _reply(json.dumps(_LEGIT_VALID))])
    res = _guard(_hosted(reasoning_effort="high"), repair_max_tokens=2500)
    assert res.ok and _sent(route, 1)["max_tokens"] == 8000
    assert _sent(route, 1)["reasoning"] == {"effort": "high"}


# ------------------------------------------------------------ 2b. frequency_penalty
def _or_body(params, *, repair: bool, **hosted):
    be = get_settings().llm.backends["frontier"].model_copy(deep=True)
    for k, v in hosted.items():
        setattr(be.hosted, k, v)
    return build_request_body(
        model=MODEL, messages=[dict(m) for m in _MSGS], backend=be, temperature=0.0,
        max_tokens=300, schema=json_schema_for(P1Triage), schema_name="triage_v1",
        caps=HostedModelCaps(json_mode="schema", params=params), repair=repair,
    )


def test_frequency_penalty_only_on_repair_and_only_when_listed():
    listed = ["structured_outputs", "frequency_penalty"]
    assert "frequency_penalty" not in _or_body(listed, repair=False)
    body = _or_body(listed, repair=True)
    assert body["frequency_penalty"] == 0.3 and "repetition_penalty" not in body
    assert body["provider"]["require_parameters"] is True  # never relaxed for a penalty
    neither = _or_body(["structured_outputs"], repair=True)
    assert "frequency_penalty" not in neither and "repetition_penalty" not in neither
    assert neither["provider"]["require_parameters"] is True
    # repetition_penalty listed: that one rides (as before), frequency_penalty is not added.
    rep = _or_body(["structured_outputs", "repetition_penalty", "frequency_penalty"],
                   repair=True)
    assert rep["repetition_penalty"] == 1.05 and "frequency_penalty" not in rep
    # Off by setting.
    off = _or_body(listed, repair=True, repair_frequency_penalty=None)
    assert "frequency_penalty" not in off


@respx.mock
def test_the_loop_repair_request_carries_frequency_penalty_when_listed(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(OR_URL).mock(side_effect=[
        _reply(_LEGIT_LOOP, finish="length", used=8000), _reply(json.dumps(_LEGIT_VALID))])
    s = _hosted(["structured_outputs", "reasoning", "frequency_penalty"])
    assert _guard(s).ok
    assert "frequency_penalty" not in _sent(route, 0)
    assert _sent(route, 1)["frequency_penalty"] == 0.3


# ------------------------------------------------------------ 3. dedupe and cap
def test_p2_and_p2c_legitimacy_flags_are_deduped_and_capped():
    legit = P2Legitimacy.model_validate({"status": "suspect", "confidence": 0.5, "signals": [
        "no_company_detail", "no_comp_no_location_no_team", "no_company_detail",
        "generic_boilerplate", "mlm_pattern", "commission_only", "reposted_repeatedly",
        "personal_email_contact", "upfront_cost_or_purchase", "unlimited_earning_language"]})
    assert legit.signals[:3] == ["no_company_detail", "no_comp_no_location_no_team",
                                 "generic_boilerplate"]
    assert len(legit.signals) == 8 and len(set(legit.signals)) == 8
    c = P2CLegitimacy.model_validate({"status": "clean", "confidence": 1,
                                      "signals": ["thin"] * 500 + [f"s{i}" for i in range(20)]})
    assert c.signals[0] == "thin" and len(c.signals) == 8 and len(set(c.signals)) == 8


# ------------------------------------------------------------ 4. Tier 1 reasoning off
@respx.mock
def test_hosted_tier1_first_attempt_runs_reasoning_off_on_a_reasoning_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(OR_URL).mock(return_value=_reply(json.dumps(_VALID_P1)))
    res = _guard(_hosted(), tier="tier1", model_cls=P1Triage, schema_name="triage_v1")
    assert res.ok and route.call_count == 1
    assert _sent(route, 0)["reasoning"] == {"effort": "none"}


def test_tier1_reasoning_off_only_where_it_is_safe_and_wanted():
    assert LLMClient(_hosted()).default_reasoning_hint("tier1") == "none"
    assert LLMClient(_hosted()).default_reasoning_hint("tier2") is None
    # The knob, an operator effort, a mandatory or unknown record, a non-reasoning model.
    assert LLMClient(_hosted(tier1_reasoning_off=False)).default_reasoning_hint("tier1") is None
    assert LLMClient(_hosted(reasoning_effort="low")).default_reasoning_hint("tier1") is None
    assert LLMClient(_hosted(mandatory=True)).default_reasoning_hint("tier1") is None
    assert LLMClient(_hosted(mandatory=None)).default_reasoning_hint("tier1") is None
    assert LLMClient(_hosted(["structured_outputs"])).default_reasoning_hint("tier1") is None
    # Local mode: nothing hosted, nothing changes.
    assert LLMClient(get_settings()).default_reasoning_hint("tier1") is None
    assert get_settings().llm.backends["frontier"].hosted.tier1_reasoning_off is True


@respx.mock
def test_a_prompt_hint_wins_over_the_tier1_default(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(OR_URL).mock(return_value=_reply(json.dumps(_VALID_P1)))
    _guard(_hosted(), tier="tier1", model_cls=P1Triage, schema_name="triage_v1",
           reasoning_hint="low")
    assert _sent(route, 0)["reasoning"] == {"effort": "low"}


# ------------------------------------------------------------ 5. preflight agrees with doctor
def _write_hosted_rows():
    from sqlalchemy.orm import Session

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.engine import make_sync_engine
    from kestrel.db.models import Setting

    migrate.run_migrations(paths.db_path())
    s = get_settings()
    rows = {
        "llm.mode": "hosted",
        "llm.hosted_mode.enabled": True,
        "llm.hosted_mode.acknowledged_at": "2026-10-15T10:00:00+00:00",
        "llm.hosted_mode.acknowledged_terms": s.llm.hosted_mode.disclosure_terms(),
        "llm.hosted_tiers": {
            t: {"backend": "frontier", "model": MODEL}
            for t in s.llm.tiers if t != "embed"
        },
    }
    rows["llm.tiers.embed.backend"] = "inprocess"
    engine = make_sync_engine(paths.db_path())
    with Session(engine) as session:
        for k, v in rows.items():
            session.add(Setting(key=k, value=v))
        session.commit()
    engine.dispose()


def test_preflight_reads_the_saved_hosted_routing_even_inside_a_loop():
    """Launch preflight, Verify and the diagnostics bundle read the same setting rows; inside a
    running loop the old async read fell back to YAML, saw every tier on Ollama and FAILed."""
    from kestrel import doctor as d
    from kestrel.llm.profiles import ollama_needed

    _write_hosted_rows()

    async def inside_loop():
        rep = d.DoctorReport()
        return rep, d._settings_for_doctor(rep)

    rep, s = asyncio.run(inside_loop())
    assert rep.routing_known and not [r for r in rep.results if r.name == "Settings layer"]
    assert s.llm.mode == "hosted" and not ollama_needed(s)


def test_hosted_only_preflight_never_fails_on_ollama(monkeypatch):
    from kestrel import doctor as d

    _write_hosted_rows()

    def refused(self, *a, **k):
        raise ConnectionRefusedError(10061, "actively refused")

    monkeypatch.setattr(d.OllamaClient, "version", refused)
    monkeypatch.setattr(d.OllamaClient, "list_models", refused)
    monkeypatch.setattr(d, "_check_browser", lambda rep: rep.add("Browser (PDF)", d.PASS, "x"))
    rep = d.run_doctor(quick=True)
    rows = {r.name: r for r in rep.results}
    assert rows["Ollama reachable"].status == d.INFO
    assert rows["Ollama reachable"].detail == d.OLLAMA_NOT_NEEDED
    assert not any(r.status == d.FAIL and r.name in d.OLLAMA_ROWS for r in rep.results)


def test_unknown_routing_makes_an_unreachable_ollama_a_warning(monkeypatch):
    from kestrel import doctor as d

    def broken():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(d, "_db_overrides_sync", broken)
    rep = d.DoctorReport()
    d._settings_for_doctor(rep)
    assert not rep.routing_known

    class Down:
        native_url = "http://127.0.0.1:11434"

        def version(self):
            raise ConnectionRefusedError(10061, "actively refused")

    assert d._check_ollama(rep, Down()) is False
    row = next(r for r in rep.results if r.name == "Ollama reachable")
    assert row.status == d.WARN and not rep.has_blocking_failure


# ------------------------------------------------------------ review: verified off shape only
@respx.mock
def test_repair_cap_applies_only_on_the_verified_effort_none_shape(monkeypatch):
    """A record marking reasoning mandatory (or missing) sends `enabled: false`, which the model
    may ignore and keep thinking: the repair keeps the full budget instead of a cap sized for the
    bare answer. The verified shape (effort none, optional reasoning) still gets the cap."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(OR_URL).mock(side_effect=[
        _reply(_LEGIT_LOOP, finish="length", used=8000), _reply(json.dumps(_LEGIT_VALID))] * 3)
    for i, mandatory in enumerate((True, None)):
        assert _guard(_hosted(mandatory=mandatory), repair_max_tokens=2500).ok
        repair = _sent(route, 2 * i + 1)
        assert repair["reasoning"] == {"enabled": False}
        assert repair["max_tokens"] == 8000
    assert _guard(_hosted(), repair_max_tokens=2500).ok
    assert _sent(route, 5)["reasoning"] == {"effort": "none"}
    assert _sent(route, 5)["max_tokens"] == 2500

def test_tier1_default_hint_ignores_include_reasoning_alone():
    """`include_reasoning` alone would make the body fall back to `enabled: false`: Tier 1 gets
    no reasoning field at all rather than an unverified switch."""
    s = _hosted(["structured_outputs", "include_reasoning"])
    assert LLMClient(s).default_reasoning_hint("tier1") is None
    body = build_request_body(
        backend=s.llm.backends["frontier"], model=MODEL, messages=_MSGS, temperature=0.1,
        max_tokens=100, schema=None, reasoning_hint=LLMClient(s).default_reasoning_hint("tier1"),
        caps=s.llm.hosted_model_caps[MODEL])
    assert "reasoning" not in body
