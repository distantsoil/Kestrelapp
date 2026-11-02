"""#112: Ollama's model server dies loading a model while another is loaded (HTTP 500 "llama-server
process has terminated", 0xc0000005). The client unloads the other loaded models (memory only:
no delete, no pull) and retries once; a second failure raises ModelLoadCrashed. respx-mocked."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from httpx import Response

from kestrel.config import get_settings
from kestrel.llm.client import LLMClient, ModelLoadCrashed, ProviderError

BASE = "http://127.0.0.1:11434"
CHAT = f"{BASE}/v1/chat/completions"
CRASH = Response(
    500, text='{"error":"llama-server process has terminated: exit status 0xc0000005"}')
OK = Response(200, json={"choices": [{"message": {"content": "{}"}}], "usage": {}})
MSGS = [{"role": "user", "content": "SECRET PROMPT TEXT"}]


def _mock_ps(mock):
    ps = mock.get(f"{BASE}/api/ps").mock(return_value=Response(200, json={"models": [
        {"name": "qwen3:8b", "model": "qwen3:8b"},
        {"name": "kestrel-tier2:latest", "model": "kestrel-tier2:latest"},
    ]}))
    gen = mock.post(f"{BASE}/api/generate").mock(return_value=Response(200, json={}))
    return ps, gen


async def test_a_load_crash_unloads_the_other_models_and_the_retry_succeeds():
    with respx.mock(assert_all_called=False) as mock:
        chat = mock.post(CHAT).mock(side_effect=[CRASH, OK])
        ps, gen = _mock_ps(mock)
        out = await LLMClient(get_settings()).complete(tier="tier1", messages=MSGS)
    assert out is not None
    assert chat.call_count == 2 and ps.call_count == 1
    sent = [json.loads(c.request.content) for c in gen.calls]
    assert sent == [{"model": "qwen3:8b", "keep_alive": 0},
                    {"model": "kestrel-tier2:latest", "keep_alive": 0}]
    # Nothing is deleted or pulled, ever.
    assert all(c.request.url.path in ("/api/ps", "/api/generate", "/v1/chat/completions")
               for c in mock.calls)


async def test_a_second_crash_raises_the_load_crash_error_with_the_plain_message():
    with respx.mock(assert_all_called=False) as mock:
        chat = mock.post(CHAT).mock(side_effect=[CRASH, CRASH])
        _mock_ps(mock)
        with pytest.raises(ModelLoadCrashed) as err:
            await LLMClient(get_settings()).complete(tier="tier1", messages=MSGS)
    assert chat.call_count == 2  # one retry, no more
    assert isinstance(err.value, ProviderError)
    assert "The local AI stopped while loading the model" in err.value.message
    assert "restart Ollama" in err.value.message
    assert "SECRET PROMPT TEXT" not in str(err.value)


async def test_a_different_500_is_not_retried_this_way():
    with respx.mock(assert_all_called=False) as mock:
        chat = mock.post(CHAT).mock(return_value=Response(500, text="internal error"))
        ps, gen = _mock_ps(mock)
        with pytest.raises((ProviderError, httpx.HTTPStatusError)) as err:
            await LLMClient(get_settings()).complete(tier="tier1", messages=MSGS)
    assert not isinstance(err.value, ModelLoadCrashed)
    assert chat.call_count == 1 and ps.call_count == 0 and gen.call_count == 0


async def test_the_crash_log_names_the_model_and_status_never_the_prompt(capsys):
    import structlog

    with structlog.testing.capture_logs() as logs, respx.mock(assert_all_called=False) as mock:
        mock.post(CHAT).mock(side_effect=[CRASH, OK])
        _mock_ps(mock)
        await LLMClient(get_settings()).complete(tier="tier1", messages=MSGS)
    crash = next(e for e in logs if e["event"] == "ollama_load_crash")
    assert crash["status"] == 500 and crash["model"]
    assert "SECRET PROMPT TEXT" not in json.dumps(logs, default=str)


# ------------------------------------------------------------- the doctor and the worker
class _SyncClient:
    """A fake OllamaClient for the doctor's probes: records the order of unloads and calls."""

    def __init__(self, fail_first: bool = False):
        self.events: list[tuple] = []
        self.fail_first = fail_first

    def unload_others(self, keep=None, timeout=30.0):
        self.events.append(("unload", keep))
        return 1

    def chat_with_usage(self, model, messages, **kw):
        self.events.append(("chat", model))
        if self.fail_first:
            self.fail_first = False
            req = httpx.Request("POST", "http://x/v1/chat/completions")
            raise httpx.HTTPStatusError(
                "500", request=req, response=httpx.Response(
                    500, request=req, text="llama-server process has terminated: 0xc0000005"))
        return "hi", 2

    @staticmethod
    def strip_think(text):
        return text


def _probe(client):
    from kestrel.doctor import DoctorReport, _check_thinking, _ProbeClient

    s = get_settings()
    present = {s.llm.tiers[t].model for t in ("tier1", "tier2", "docsel")}
    rep = DoctorReport()
    _check_thinking(rep, _ProbeClient(client), s, present)
    return rep, present


def test_the_doctor_unloads_the_other_models_before_each_models_probe():
    client = _SyncClient()
    rep, present = _probe(client)
    assert len(present) >= 2
    chats = [e for e in client.events if e[0] == "chat"]
    for i, e in enumerate(client.events):
        if e[0] == "chat":
            assert client.events[i - 1] == ("unload", e[1]), client.events
    assert len(chats) == len(present)
    assert all(r.status == "PASS" for r in rep.results)


def test_a_load_crash_on_the_first_probe_then_success_is_ok_not_fail():
    client = _SyncClient(fail_first=True)
    rep, _ = _probe(client)
    assert all(r.status == "PASS" for r in rep.results), rep.results
    assert ("unload", None) in client.events  # everything unloaded, then retried once


@respx.mock
def test_thinking_arriving_in_content_with_no_tags_and_an_empty_reasoning_field_fails():
    """The 4B Thinking build's shape on the owner's machine: prose in content, 32 of 32 tokens,
    no <think> tags, `reasoning` empty."""
    from kestrel.doctor import FAIL, DoctorReport, _check_thinking
    from kestrel.llm.client import OllamaClient

    s = get_settings()
    present = {s.llm.tiers["tier1"].model}
    respx.post(CHAT).mock(return_value=Response(200, json={
        "choices": [{"message": {
            "content": "Hmm, the user asked me to reply with just the word hi, so I should",
            "reasoning": ""}, "finish_reason": "length"}],
        "usage": {"completion_tokens": 32}}))
    rep = DoctorReport()
    _check_thinking(rep, OllamaClient(f"{BASE}/v1", BASE), s, present)
    row = next(r for r in rep.results if r.name.startswith("thinking off"))
    assert row.status == FAIL


@respx.mock
async def test_a_tier_switch_unloads_the_previous_tier_model_once():
    from kestrel.pipeline.worker import Worker

    gen = respx.post(f"{BASE}/api/generate").mock(return_value=Response(200, json={}))
    w = Worker.__new__(Worker)
    w._settings = get_settings()
    pending = {"t1": 3, "t2": 2}

    async def count(task_type):
        return pending[task_type]

    w._pending_count = count
    await w._unload_previous_tier("t1")  # nothing was loaded before
    assert gen.call_count == 0
    await w._unload_previous_tier("t1")  # same tier again: nothing
    assert gen.call_count == 0
    await w._unload_previous_tier("t2")  # t1 -> t2: unload the tier1 model, once
    assert gen.call_count == 1
    sent = json.loads(gen.calls.last.request.content)
    assert sent == {"model": w._settings.llm.tiers["tier1"].model, "keep_alive": 0}
    await w._unload_previous_tier("t2")
    assert gen.call_count == 1


@respx.mock
async def test_a_tier_with_no_queued_work_is_not_the_next_tier():
    from kestrel.pipeline.worker import Worker

    gen = respx.post(f"{BASE}/api/generate").mock(return_value=Response(200, json={}))
    w = Worker.__new__(Worker)
    w._settings = get_settings()
    pending = {"t1": 1, "t2": 0}

    async def count(task_type):
        return pending[task_type]

    w._pending_count = count
    await w._unload_previous_tier("t1")
    await w._unload_previous_tier("t2")  # t2 has nothing queued: nothing to switch to
    assert gen.call_count == 0
    pending["t2"] = 1
    await w._unload_previous_tier("t2")
    assert gen.call_count == 1


def test_the_crash_signature_matches_any_runner_name_and_only_a_500():
    from kestrel.llm.client import is_load_crash

    assert is_load_crash(500, "runner process has terminated: exit status 2")
    assert is_load_crash(500, "llama-server process has terminated: exit status 0xc0000005")
    assert is_load_crash(500, "exit status 0xC0000005")
    assert not is_load_crash(500, "internal error")
    assert not is_load_crash(503, "process has terminated")


def test_loaded_names_compare_with_and_without_latest():
    from kestrel.llm.client import same_model

    assert same_model("kestrel-tier1:latest", "kestrel-tier1")
    assert same_model("qwen3:8b", "qwen3:8b")
    assert not same_model("qwen3:8b", "qwen3:14b") and not same_model("x", None)


def test_the_lone_close_strip_stops_at_the_first_close_tag():
    from kestrel.llm.client import strip_think_text

    assert strip_think_text("reasoning</think>the answer mentions </think> literally") == (
        "the answer mentions </think> literally")


@respx.mock
async def test_the_recovery_never_unloads_a_model_another_request_is_using():
    import asyncio

    gate = asyncio.Event()
    state = {"crashed": False}

    async def chat(request):
        model = json.loads(request.content)["model"]
        if model == "kestrel-tier1":
            await gate.wait()  # request A is mid-flight on kestrel-tier1
            return OK
        if not state["crashed"]:
            state["crashed"] = True
            return CRASH
        return OK

    respx.post(CHAT).mock(side_effect=chat)
    respx.get(f"{BASE}/api/ps").mock(return_value=Response(200, json={"models": [
        {"model": "kestrel-tier1:latest"}, {"model": "qwen3:8b"}]}))
    gen = respx.post(f"{BASE}/api/generate").mock(return_value=Response(200, json={}))
    client = LLMClient(get_settings())
    a = asyncio.create_task(client.complete(tier="tier1", messages=MSGS))
    await asyncio.sleep(0.05)
    await client.complete(tier="tier2", messages=MSGS)  # B crashes, recovers
    unloaded = [json.loads(c.request.content)["model"] for c in gen.calls]
    assert unloaded == ["qwen3:8b"], unloaded  # kestrel-tier1 (with :latest) is in use
    gate.set()
    await a


# ------------------------------------------------------ the doctor while a run is active
def test_the_doctor_skips_the_probes_and_warns_while_a_run_is_active(monkeypatch):
    from kestrel import doctor
    from kestrel.doctor import WARN, DoctorReport

    called = []
    monkeypatch.setattr(doctor, "_run_is_active", lambda: True)
    for name in ("_check_needle", "_check_thinking", "_check_schema"):
        monkeypatch.setattr(doctor, name, lambda *a, _n=name, **k: called.append(_n))
    rep = DoctorReport()
    doctor._run_probes(rep, object(), get_settings(), set())
    assert called == []
    row = rep.results[0]
    assert row.status == WARN and row.detail == "A run is going; check again when it finishes."


def test_the_doctor_probes_when_no_run_is_active(monkeypatch):
    from kestrel import doctor
    from kestrel.doctor import DoctorReport

    called = []
    monkeypatch.setattr(doctor, "_run_is_active", lambda: False)
    for name in ("_check_needle", "_check_thinking", "_check_schema"):
        monkeypatch.setattr(doctor, name, lambda *a, _n=name, **k: called.append(_n))
    doctor._run_probes(DoctorReport(), object(), get_settings(), set())
    assert called == ["_check_needle", "_check_thinking", "_check_schema"]
