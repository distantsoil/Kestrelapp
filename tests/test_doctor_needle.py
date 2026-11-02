"""The doctor's context needle test: it reads the reply for the code anywhere in it, gives a
chatty model room to answer, treats a server that is not ready as a warning, and blames the
context window only for a real answer that lacks the code (D-IMPORT-6)."""

from __future__ import annotations

import httpx

from kestrel import doctor
from kestrel.config import load_settings


class _Client:
    def __init__(self, behavior):
        self.behavior, self.max_tokens = behavior, None

    def chat(self, model, messages, *, max_tokens=None, **_kw):
        self.max_tokens = max_tokens
        return self.behavior(messages[0]["content"])

    @staticmethod
    def strip_think(text):
        return text


def _run(behavior):
    s = load_settings()
    models = {s.llm.tiers[t].model for t in doctor.CTX_TIERS
              if not s.llm.backends[s.llm.tiers[t].backend].external}
    rep = doctor.DoctorReport()
    client = _Client(behavior)
    doctor._check_needle(rep, client, s, set(models))
    needles = [r for r in rep.results if r.name.startswith("needle ")]
    assert needles
    return needles, client


def _code_in(prompt: str) -> str:
    return prompt.split("SECRET CODE: ", 1)[1].split("\n", 1)[0].strip()


def test_a_chatty_reply_that_contains_the_code_passes():
    needles, client = _run(lambda p: f"Sure! The secret code you gave me is {_code_in(p)}, ok?")
    assert all(r.status == doctor.PASS for r in needles)
    assert client.max_tokens >= 256


def test_a_server_error_or_timeout_is_a_warning_with_the_not_ready_remedy():
    def _500(_p):
        req = httpx.Request("POST", "http://127.0.0.1:11434/v1/chat/completions")
        raise httpx.HTTPStatusError("boom", request=req, response=httpx.Response(500, request=req))

    def _slow(_p):
        raise httpx.ReadTimeout("slow")

    for behavior in (_500, _slow):
        needles, _ = _run(behavior)
        for r in needles:
            assert r.status == doctor.WARN and not r.blocking
            assert "model not ready yet, try again after setup finishes" in r.remediation
            assert "num_ctx" not in r.remediation


def test_a_real_answer_without_the_code_still_fails_with_the_context_advice():
    needles, _ = _run(lambda _p: "I am sorry, I do not see any code in this message.")
    for r in needles:
        assert r.status == doctor.FAIL and r.blocking and "larger num_ctx" in r.remediation


def test_an_empty_reply_is_not_blamed_on_the_context_window():
    needles, _ = _run(lambda _p: "")
    for r in needles:
        assert r.status == doctor.WARN and "num_ctx" not in r.remediation


def test_a_reply_cut_off_at_the_token_cap_without_the_code_is_a_warning_not_a_context_failure():
    needles, _ = _run(lambda _p: "Let me think about that. " * 80)  # about the whole budget
    for r in needles:
        assert r.status == doctor.WARN and not r.blocking
        assert "cut off" in r.detail and "num_ctx" not in r.remediation
