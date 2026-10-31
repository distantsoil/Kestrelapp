"""0.2.0 hands-on feedback (D-UI-14, D-UI-15, D-IMPORT-2): the window opens maximized, a stopped
Ollama no longer costs every request two seconds, a truncated model reply names the budget
instead of "invalid JSON", and the import reports its stages and can retry without re-uploading."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response

from kestrel.config import get_settings
from kestrel.llm.client import LLMClient, ModelDeclined, chat_stop_problem

ROOT = Path(__file__).resolve().parents[1]
OR_URL = "https://openrouter.ai/api/v1/chat/completions"


# ------------------------------------------------------------------- truncated replies (P7 bug)
def _length_reply(content: str = "", reasoning: int | None = 3900) -> dict:
    usage: dict = {"prompt_tokens": 2500, "completion_tokens": 4000}
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return {
        "choices": [{"finish_reason": "length", "message": {"role": "assistant",
                                                            "content": content}}],
        "usage": usage,
    }


def test_chat_stop_problem_names_the_budget_and_the_reasoning_share():
    msg = chat_stop_problem(_length_reply(), 4000)
    assert msg and "max_tokens 4000" in msg and "3900 of them reasoning" in msg
    assert "Raise the prompt's max_tokens" in msg
    assert "of them reasoning" not in (chat_stop_problem(_length_reply(reasoning=None), 4000) or "")
    stop = {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
    assert chat_stop_problem(stop, 4000) is None
    filtered = {"choices": [{"finish_reason": "content_filter", "message": {"content": None}}]}
    assert "content filter" in (chat_stop_problem(filtered) or "")
    assert chat_stop_problem({}) is None  # no choices: not this function's problem


@respx.mock
def test_a_length_stop_raises_model_declined_not_an_empty_answer(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    respx.post(OR_URL).mock(return_value=Response(200, json=_length_reply()))
    client = LLMClient(get_settings())
    with pytest.raises(ModelDeclined, match="hit max_tokens 4000"):
        asyncio.run(client.complete(tier="frontier", messages=[{"role": "user", "content": "x"}],
                                    max_tokens=4000, allow_external=True))


@respx.mock
def test_guard_quarantines_a_truncated_reply_without_a_repair_call(monkeypatch):
    """The 0.2.0 import made two calls of ~24 s each and then said "invalid JSON: Expecting
    value". A repair at the same cap cannot help, so a reply cut at its cap is asked again ONCE at
    twice the budget (D-LLM-7, not a schema repair: same messages, no echo), then quarantined with
    the real reason."""
    from kestrel.llm.guard import run_guarded
    from kestrel.llm.schemas import P7ResumeImport

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    # A real truncation: the answer was being written and ran out of room. Thinking that ate
    # the whole cap is a different case (one re-ask with reasoning off, D-IMPORT-4).
    cut = _length_reply('{"roles": [{"company_label": "Acme"', reasoning=None)
    route = respx.post(OR_URL).mock(return_value=Response(200, json=cut))
    client = LLMClient(get_settings())
    res = asyncio.run(run_guarded(client, tier="frontier", model_cls=P7ResumeImport,
                                  messages=[{"role": "user", "content": "x"}], max_tokens=4000,
                                  allow_external=True))
    assert route.call_count == 2  # the first call and the one larger-budget retry, never a third
    sent = [json.loads(c.request.content) for c in route.calls]
    assert sent[1]["max_tokens"] == 2 * sent[0]["max_tokens"]
    assert sent[1]["messages"] == sent[0]["messages"]  # a retry, not a repair
    assert not res.ok and res.quarantined
    assert "invalid JSON" not in (res.error or "") and "max_tokens" in (res.error or "")


def test_p7_prompts_are_small_and_hint_low_reasoning():
    """D-IMPORT-3 superseded the 12,000-token stopgap: P7 answers in line numbers, so each call
    needs a few thousand tokens at most and fits every tier's num_ctx with room to spare."""
    from kestrel.llm.prompts import load_prompt

    for version in ("resume_outline/v1", "resume_role/v1", "variant_suggest/v1"):
        spec = load_prompt(version)
        assert spec.max_tokens <= 3000 and spec.reasoning == "none" and spec.tier == "docsel"
    p7 = (ROOT / "PROMPTS.md").read_text(encoding="utf-8").split("## P7")[1].split("\n## ")[0]
    assert "resume_outline/v1" in p7 and "never writes resume text" in p7


# ------------------------------------------------------------------------ Ollama probe (delay)
class _CountingOllama:
    calls = 0
    timeouts: list = []
    up = False

    def __init__(self, *a, **k) -> None:
        pass

    def version(self, timeout=5.0):
        type(self).calls += 1
        type(self).timeouts.append(timeout)
        if not type(self).up:
            raise httpx.ConnectError("refused")
        return "0.12.0"

    def list_models(self, timeout=10.0):
        return ["kestrel-tier2:latest"]


@pytest.fixture
def counting_ollama(monkeypatch):
    from kestrel.services import setup_state as ss

    _CountingOllama.calls, _CountingOllama.timeouts, _CountingOllama.up = 0, [], False
    monkeypatch.setattr(ss, "OllamaClient", _CountingOllama)
    ss.forget_ollama_probe()
    yield _CountingOllama
    ss.forget_ollama_probe()


def test_a_down_ollama_is_probed_once_with_a_short_connect_timeout(counting_ollama):
    from kestrel.services import setup_state as ss

    s = get_settings()
    assert ss.probe_ollama(s) == (None, [])
    assert ss.probe_ollama(s) == (None, [])  # cached: no second two-second refusal
    assert counting_ollama.calls == 1
    t = counting_ollama.timeouts[0]
    assert isinstance(t, httpx.Timeout) and t.connect is not None and t.connect <= 0.5


def test_fresh_bypasses_the_cache_and_refreshes_it(counting_ollama):
    from kestrel.services import setup_state as ss

    s = get_settings()
    assert ss.probe_ollama(s)[0] is None
    counting_ollama.up = True
    assert ss.probe_ollama(s)[0] is None  # still the cached "down"
    assert ss.probe_ollama(s, fresh=True)[0] == "0.12.0"
    assert ss.probe_ollama(s)[0] == "0.12.0"  # the fresh answer is what is cached now
    assert counting_ollama.calls == 2


def test_status_rail_follows_the_stored_mode_not_the_idle_workers_last_cycle(monkeypatch):
    """The worker last cycled on "local"; the operator then chose Hosted. The rail must not keep
    probing a stopped Ollama on every poll."""
    from kestrel.web import routes_control

    probed: list[str] = []

    async def _probe(url: str):
        probed.append(url)
        return {"up": False, "loaded": []}

    s = get_settings()
    s.llm.mode = "hosted"
    monkeypatch.setattr(routes_control, "_probe_ollama", _probe)
    monkeypatch.setattr(routes_control, "current_run_mode", lambda: "local")

    async def _load(_session):
        return s

    monkeypatch.setattr(routes_control, "load_settings_with_db", _load)
    row = asyncio.run(routes_control._model_row(s, s.llm.mode))
    assert row["label"].startswith("Hosted mode") and not probed
    src = Path(routes_control.__file__).read_text(encoding="utf-8")
    assert "_model_row(settings, settings.llm.mode)" in src


def test_status_probe_uses_a_short_connect_timeout_and_remembers_down(monkeypatch):
    from kestrel.web import routes_control

    seen: list = []

    class _Client:
        def __init__(self, timeout=None, **_):
            seen.append(timeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(routes_control.httpx, "AsyncClient", _Client)
    routes_control.reset_status_caches()
    for _ in range(3):
        assert asyncio.run(routes_control._probe_ollama("http://127.0.0.1:11434")) == {
            "up": False, "loaded": []}
    assert len(seen) == 1 and seen[0].connect <= 0.5
    assert routes_control._PS_TTL_DOWN_S > routes_control._PS_TTL_S


# ------------------------------------------------------------------------------------ window
def test_window_opens_large_but_not_maximized_with_a_minimum_size():
    from kestrel.desktop.shell import _initial_size

    src = (ROOT / "kestrel" / "desktop" / "shell.py").read_text(encoding="utf-8")
    call = src.split("webview.create_window(")[1].split(")\n")[0]
    assert "maximized" not in call and "min_size=" in call and "_initial_size" in src
    # #26: pywebview blocks text selection app-wide unless asked, so the LAN secret and join link
    # (and every id, error and path on a page) could not be selected or copied.
    assert "text_select=True" in call
    # D-UI-87: one fixed design size (about 1680x1000), never a share of the screen, clamped to
    # the usable area (the screen less the taskbar or dock and a margin), 960x640 at least.
    assert "frameless=True" in call  # D-UI-88: no OS title bar
    assert _initial_size((3440, 1440)) == (1680, 1000)  # an ultrawide gets the same window
    assert _initial_size((3840, 2160)) == (1680, 1000)
    assert _initial_size((1920, 1080)) == (1680, 992)  # 1080 less the taskbar and a margin
    assert _initial_size((1366, 768)) == (1326, 680)  # a small laptop: the usable area
    assert _initial_size((1280, 720)) == (1240, 640)  # never below the minimum
    assert _initial_size((800, 600)) == (960, 640)
    # With the work area known (the screen less its taskbar), 40 px is kept clear on each side:
    # a 1920x1080 display with a 40 px taskbar gets the full design size.
    assert _initial_size((1920, 1080), work=(1920, 1040)) == (1680, 1000)
    assert _initial_size((1920, 1080), work=(1920, 1032)) == (1680, 992)  # a 48 px taskbar
    assert _initial_size((1366, 768), work=(1366, 728)) == (1326, 688)
    # A short screen never narrows the window: each side is clamped on its own.
    assert _initial_size((3440, 900), work=(3440, 860))[0] == 1680
    for screen in ((1920, 1080), (2560, 1600), (3440, 1440)):
        w, h = _initial_size(screen)
        assert w <= 1710 and h <= 1107  # never larger than a 15-inch laptop's default
    import sys

    saved = sys.modules.get("webview")
    sys.modules["webview"] = None  # no backend at all: the size is unknown
    try:
        assert _initial_size() == (1440, 900)
    finally:
        if saved is None:
            del sys.modules["webview"]
        else:
            sys.modules["webview"] = saved


# ------------------------------------------------------------------------- import stages/retry
def _import_client(monkeypatch, fake):
    from starlette.testclient import TestClient

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.engine import reset_engine
    from kestrel.web.app import build_app

    monkeypatch.setattr("kestrel.llm.client.LLMClient", fake)
    migrate.run_migrations(paths.db_path())
    asyncio.run(reset_engine())
    return TestClient(build_app(worker=False))


class _FailingP7:
    calls = 0

    def __init__(self, settings, http=None, **_):
        self.settings = settings

    async def complete(self, **_):
        type(self).calls += 1
        raise ModelDeclined("the reply hit max_tokens 12000 before the structured object was "
                            "complete", json.dumps(_length_reply()))


SAMPLE_MD = "# Pat Example\n\n## Acme\n\n- Cut costs 20% across 300 staff.\n"


def test_read_step_shows_the_text_and_a_processing_step_that_starts_on_load(monkeypatch):
    client = _import_client(monkeypatch, _FailingP7)
    _FailingP7.calls = 0
    r = client.post("/api/documents/import/read",
                    data={"track": "employment", "doc_kind": "resume", "text": ""},
                    files={"file": ("cv.md", SAMPLE_MD.encode(), "text/markdown")})
    assert r.status_code == 200
    html = r.text
    assert 'data-testid="import-extracted"' in html and "cv.md" in html
    assert 'class="import-result"' in html
    form = html.split('class="import-processing"')[1].split("</form>")[0]
    assert 'hx-trigger="load"' in form and 'hx-post="/api/documents/import"' in form
    assert "Cut costs 20% across 300 staff." in form  # the read text rides to step two
    assert 'name="extracted_shown" value="1"' in form and 'name="filename" value="cv.md"' in form
    assert "data-no-busy" in form and "Processing." in form
    assert _FailingP7.calls == 0  # reading never calls the model


def test_read_step_reports_an_empty_source_without_a_processing_step(monkeypatch):
    client = _import_client(monkeypatch, _FailingP7)
    r = client.post("/api/documents/import/read", data={"track": "employment", "text": "  "})
    assert r.status_code == 200
    assert "Paste some text or choose a file" in r.text and "import-processing" not in r.text


def test_a_failed_proposal_offers_a_retry_on_the_same_text(monkeypatch):
    client = _import_client(monkeypatch, _FailingP7)
    r = client.post("/api/documents/import",
                    data={"track": "employment", "doc_kind": "resume", "text": SAMPLE_MD,
                          "filename": "cv.md", "source_format": "md", "extracted_shown": "1"})
    assert r.status_code == 200
    html = r.text
    assert 'data-testid="import-error"' in html and "max_tokens 12000" in html
    assert 'data-testid="import-extracted"' not in html  # step one already showed it
    retry = html.split('class="import-processing"')[1].split("</form>")[0]
    assert 'data-testid="import-retry"' in retry and 'hx-trigger="load"' not in retry
    assert "Cut costs 20% across 300 staff." in retry
    assert 'hx-target="closest .import-result"' in retry


def test_import_form_posts_to_the_read_step_and_long_requests_skip_the_busy_cursor():
    tpl = ROOT / "kestrel" / "web" / "templates"
    docs = (tpl / "partials" / "documents_body.html").read_text(encoding="utf-8")
    form = docs.split('id="import-form"')[1].split(">")[0]
    assert 'hx-post="/api/documents/import/read"' in form and "data-no-busy" in form
    assert 'id="import-proposal" class="import-result"' in docs
    assert "htmx:xhr:progress" in docs and "Uploading" in docs and "Reading" in docs
    base = (tpl / "base.html").read_text(encoding="utf-8")
    assert "closest('[data-no-busy], [data-busy-text]')" in base


@respx.mock
def test_a_whole_object_padded_to_the_cap_is_kept_not_quarantined(monkeypatch):
    """JSON mode can finish the object and pad with whitespace until max_tokens. That reply is
    whole: it goes on to validation rather than being thrown away."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    respx.post(OR_URL).mock(
        return_value=Response(200, json=_length_reply('{"a": 1}' + "\n" * 50, reasoning=None))
    )
    res = asyncio.run(LLMClient(get_settings()).complete(
        tier="frontier", messages=[{"role": "user", "content": "x"}], max_tokens=4000,
        allow_external=True))
    assert json.loads(res.content) == {"a": 1}


def test_a_finished_setup_action_drops_the_cached_probe(counting_ollama):
    from kestrel.services import setup_state as ss

    s = get_settings()
    ss.probe_ollama(s)
    act = ss.start_action("probe-test", lambda a: None)
    act.thread.join(5)
    assert ss._probe_cache["result"] is None


# ------------------------------------------------- reasoning off, and the re-ask (D-IMPORT-4)
def test_none_is_a_switch_on_openrouter_and_the_operator_setting_still_wins():
    from kestrel.llm.client import build_request_body

    be = get_settings().llm.backends["frontier"]
    msgs = [{"role": "user", "content": "x"}]
    body = build_request_body(model="m", messages=msgs, backend=be, temperature=0,
                              max_tokens=10, reasoning_hint="none")
    assert body["reasoning"] == {"enabled": False}
    be2 = be.model_copy(deep=True)
    be2.hosted.reasoning_effort = "medium"
    body = build_request_body(model="m", messages=msgs, backend=be2, temperature=0,
                              max_tokens=10, reasoning_hint="none")
    assert body["reasoning"] == {"effort": "medium"}


def test_reasoning_exhausted_means_the_cap_went_to_thinking():
    from kestrel.llm.client import reasoning_exhausted

    assert reasoning_exhausted(_length_reply("", reasoning=3000)) is True  # 3000 of 4000, empty
    assert reasoning_exhausted(_length_reply('{"a":', reasoning=3900)) is True  # 3900 of 4000
    assert reasoning_exhausted(_length_reply('{"a":', reasoning=100)) is False  # cut, not spent
    assert reasoning_exhausted(_length_reply("", reasoning=None)) is True  # no usage, no content
    assert reasoning_exhausted({"choices": [{"finish_reason": "stop",
                                            "message": {"content": ""}}]}) is False


@respx.mock
def test_guard_re_asks_once_with_reasoning_off_then_quarantines(monkeypatch):
    """The 0.3.0 import: effort low, 3,000 tokens of thinking, no answer. The re-ask sends
    reasoning.enabled false; a second exhausted reply quarantines with the reason."""
    from kestrel.llm.guard import run_guarded
    from kestrel.llm.schemas import P7Outline

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    good = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(
        {"summary_blocks": [], "roles": [], "skill_groups": []})}}], "usage": {}}
    route = respx.post(OR_URL).mock(side_effect=[
        Response(200, json=_length_reply("", reasoning=3000)), Response(200, json=good)])
    client = LLMClient(get_settings())
    res = asyncio.run(run_guarded(client, tier="frontier", model_cls=P7Outline,
                                  messages=[{"role": "user", "content": "x"}], max_tokens=3000,
                                  allow_external=True, reasoning_hint="low"))
    assert res.ok and route.call_count == 2
    sent = [json.loads(c.request.content) for c in route.calls]
    assert sent[0]["reasoning"] == {"effort": "low"} and sent[1]["reasoning"] == {"enabled": False}

    respx.reset()
    route = respx.post(OR_URL).mock(return_value=Response(200, json=_length_reply("", 3000)))
    res = asyncio.run(run_guarded(client, tier="frontier", model_cls=P7Outline,
                                  messages=[{"role": "user", "content": "x"}], max_tokens=3000,
                                  allow_external=True, reasoning_hint="low"))
    assert not res.ok and res.quarantined and route.call_count == 2  # once off, then stop


@respx.mock
def test_no_re_ask_when_the_operator_set_an_effort_or_the_cut_was_not_reasoning(monkeypatch):
    from kestrel.llm.guard import run_guarded
    from kestrel.llm.schemas import P7Outline

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    s = get_settings().model_copy(deep=True)
    s.llm.backends["frontier"].hosted.reasoning_effort = "high"  # the operator's choice wins
    route = respx.post(OR_URL).mock(return_value=Response(200, json=_length_reply("", 3000)))
    res = asyncio.run(run_guarded(LLMClient(s), tier="frontier", model_cls=P7Outline,
                                  messages=[{"role": "user", "content": "x"}], max_tokens=3000,
                                  allow_external=True, reasoning_hint="low"))
    assert res.quarantined and route.call_count == 1
    respx.reset()
    route = respx.post(OR_URL).mock(
        return_value=Response(200, json=_length_reply('{"roles": [', reasoning=50)))
    res = asyncio.run(run_guarded(LLMClient(get_settings()), tier="frontier",
                                  model_cls=P7Outline, max_tokens=3000, allow_external=True,
                                  messages=[{"role": "user", "content": "x"}]))
    # A real truncation: no reasoning re-ask helps, but the one larger-budget retry (D-LLM-7) runs.
    assert res.quarantined and route.call_count == 2
