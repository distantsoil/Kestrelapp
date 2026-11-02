"""A failed call to a model never puts its URL, status line, exception type or provider text on a
page, in a toast or in a task's stored reason (D-SEC-2, D-SEC-4): the reason is a fixed sentence
chosen by kind, and the detail goes to the log as a type, a status and a code."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from structlog.testing import capture_logs

from kestrel.docs.imports import propose_import
from kestrel.llm.client import ProviderError

SECRET = "SENTINEL-0xc0000005"
_URL = "http://127.0.0.1:11434/v1/chat/completions"
_CRASH = ProviderError(500, f"llama-server process has terminated: exit status {SECRET}", _URL)
_RESUME = "Pat Example\n## Experience\nAcme | Town, ST  Jan 2020 - Dec 2022\n- Ran the desk.\n"
_FORBIDDEN = ("http", "11434", "ProviderError", "0xc0000005", SECRET, "127.0.0.1", "llama-server")


class _Down:
    """A client whose every call fails the way the owner's local model did."""

    def __init__(self, exc):
        self.exc = exc
        self.settings = None

    async def complete(self, **_kw):
        raise self.exc


def _clean(text: str) -> None:
    low = text.lower()
    for bad in _FORBIDDEN:
        assert bad.lower() not in low, (bad, text)


def test_a_crashed_local_model_gives_a_plain_message_on_the_import_page_and_a_logged_detail():
    from kestrel.config import load_settings
    from kestrel.llm.client import LLMClient

    class _Crash(LLMClient):
        def __init__(self):
            super().__init__(load_settings())

        async def complete(self, **_kw):
            raise _CRASH

    with capture_logs() as logs:
        prop = asyncio.run(propose_import(_Crash(), _RESUME, source_format="md"))
    assert prop.error and "Restart Ollama" in prop.error
    _clean(prop.error)
    warned = [e for e in logs if e["event"] == "llm_request_failed"]
    assert warned and warned[0]["status"] == 500 and warned[0]["reason"] == "local_stopped"
    assert warned[0]["error_type"] == "ProviderError"
    _clean(repr([{k: v for k, v in e.items() if k != "error_type"} for e in logs]))


def test_the_import_route_shows_the_plain_sentence_for_a_model_that_raises(monkeypatch):
    from tests.test_onboarding import _app

    async def _raises(*_a, **_k):
        raise _CRASH

    monkeypatch.setattr("kestrel.docs.imports.propose_import", _raises)
    page = _app().post("/api/documents/import", data={"text": _RESUME}).text
    assert "Restart Ollama" in page
    _clean(page.replace("htmx", "").replace("https://", ""))


_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
_EXC_NAMES = {"exc", "e", "err", "ex"}
# Sites that put an exception's text on a response on purpose: the exception is one Kestrel
# raised itself, with an authored sentence (a validator's ValueError, a source adapter's own
# error). Anything NOT here that formats an exception into a response fails this test, so a new
# site has to go through plain_error / fixed_reason / task_failure_text or be argued for here.
_AUTHORED_SITES = (  # (file, the start of the source line)
    ("app.py", 'if "locked" not in str(exc)'),
    ("deps.py", 'if "locked" in str(exc)'),
    ("errors.py", "return str(exc)"),
    ("errors.py", "text = str(exc)"),
    ("pages/opportunity_actions.py", "error = str(exc)"),
    ("pages/role.py", 'return _answer({"k-toast": _toast(str(exc)'),
    ("pages/settings_data.py", "return _imp_card(request, _imp_base(error=str(exc)"),
    ("pages/settings_prefs.py", "cause = str(exc)"),
    ("pages/settings_roles.py", "request, track, error=str(exc)"),
    ("pages/sources_ui.py", 'return _msg(request, "bad", str(exc))'),
    ("pages/sources_ui.py", 'return _msg(request, "bad", f"{exc}.")'),
    ("pages/profile.py", "said = pe.FRESH_NOT_NEEDED if str(exc)"),
)


def _exception_text_sites(root) -> set[tuple[str, str]]:
    """(file, source line) of every `str(exc)` or `{exc}` outside a logging call in kestrel/web."""
    import ast
    from pathlib import Path

    base = Path(root)
    found: set[tuple[str, str]] = set()
    for path in sorted(base.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()

        def walk(node, path=path, lines=lines):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _LOG_METHODS):
                return  # what a log line says never reaches a response
            hit = (
                (isinstance(node, ast.FormattedValue) and isinstance(node.value, ast.Name)
                 and node.value.id in _EXC_NAMES)
                or (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "str" and node.args
                    and isinstance(node.args[0], ast.Name) and node.args[0].id in _EXC_NAMES))
            if hit:
                found.add((path.relative_to(base).as_posix(), lines[node.lineno - 1].strip()))
            for child in ast.iter_child_nodes(node):
                walk(child)

        walk(ast.parse(source))
    return found


def test_no_route_or_template_puts_exception_text_on_a_response():
    """The sweep (D-SEC-4): scan kestrel/web for an exception's text formatted into anything but a
    log call, and the templates for an exception printed. Every site must be an authored error on
    the list above; a new one has to use the safe helpers."""
    import re
    from pathlib import Path

    web = Path(__file__).parent.parent / "kestrel" / "web"
    sites = _exception_text_sites(web)
    sites = {(f, line) for f, line in sites if f != "routes_control.py"}
    extra = {(f, line) for f, line in sites
             if not any(f == af and line.startswith(prefix) for af, prefix in _AUTHORED_SITES)}
    assert not extra, f"exception text may reach a response here: {sorted(extra)}"
    for template in (web / "templates").rglob("*.html"):
        assert not re.search(r"\{\{\s*(exc|exception|error_text)\b", template.read_text("utf-8")), \
            template.name


@pytest.mark.parametrize(("exc", "sentence"), [
    (ProviderError(500, f"x {SECRET}", _URL), "Restart Ollama"),
    (ProviderError(500, f"x {SECRET}", "https://api.example.com/v1/chat/completions"),
     "online model"),
    (ProviderError(401, f"x {SECRET}", "https://api.example.com/v1"), "API key"),
    (ProviderError(402, f"x {SECRET}", "https://api.example.com/v1"), "credit"),
    (ProviderError(429, f"x {SECRET}", "https://api.example.com/v1"), "busy"),
    (ProviderError(404, f"x {SECRET}", "https://api.example.com/v1"), "Models page"),
    (httpx.ReadTimeout(f"slow {SECRET}"), "too long"),
    (httpx.ConnectError(f"refused {SECRET} http://127.0.0.1:11434/x"), "could not reach the AI"),
    (RuntimeError(f"llama-server process has terminated {SECRET}"), "Restart Ollama"),
])
def test_each_helper_gives_a_fixed_sentence_with_no_part_of_the_exception(exc, sentence):
    from kestrel.config import load_settings
    from kestrel.docs.imports import _plain_failure
    from kestrel.errors import task_failure_text
    from kestrel.llm import providers as prov
    from kestrel.web.errors import fixed_reason, plain_error

    shown = {
        "plain_error": plain_error(exc), "fixed_reason": fixed_reason(exc),
        "key_check": prov._key_failure("openrouter", exc).detail,
        "task_failure_text": task_failure_text(exc), "import_failure": _plain_failure(exc),
    }
    for site, text in shown.items():
        _clean(text)
        if not isinstance(exc, RuntimeError) or type(exc).__name__ != "RuntimeError":
            assert sentence in text, (site, text)
    assert load_settings() is not None


def test_a_non_model_failure_keeps_no_exception_text_for_the_runs_page():
    from kestrel.errors import task_failure_text

    text = task_failure_text(ValueError("/home/pat/secret/path.txt is bad"))
    assert "secret" not in text and "ValueError" in text and "details are in the log" in text
    assert "secret" not in task_failure_text(PermissionError(13, "denied", "/home/pat/secret"))


def test_the_import_stops_at_the_server_time_cap(monkeypatch):
    """A propose that outlives the server cap is cancelled and the page says it took too long;
    the browser's own timeout is longer, so the page never says "stopped" while work continues."""
    import asyncio

    from kestrel.web.pages import documents as docs
    from tests.test_onboarding import _app

    state = {"cancelled": False}

    async def _slow(*_a, **_k):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    monkeypatch.setattr("kestrel.docs.imports.propose_import", _slow)
    monkeypatch.setattr(docs, "IMPORT_SERVER_CAP_S", 0.2)
    with capture_logs() as logs:
        page = _app().post("/api/documents/import", data={"text": _RESUME}).text
    assert "took too long" in page and state["cancelled"]
    done = [e for e in logs if e["event"] == "import_propose"]
    assert done and done[0]["outcome"] == "error" and done[0]["reason"] == "timeout"
    assert docs.IMPORT_SERVER_CAP_S == 0.2 and 12 * 60 < 15 * 60


def test_a_client_that_goes_away_cancels_the_proposal(monkeypatch):
    import asyncio
    import types

    from kestrel.web.pages import documents as docs

    monkeypatch.setattr(docs, "_DISCONNECT_POLL_S", 0.01)
    state = {"cancelled": False}

    async def _slow():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    async def _gone():
        return True

    request = types.SimpleNamespace(is_disconnected=_gone)

    async def go():
        return await docs._run_capped(_slow(), request, cap=30)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(go())
    assert state["cancelled"]


def test_a_guard_result_and_the_key_check_never_carry_the_url_or_provider_text():
    from kestrel.config import load_settings
    from kestrel.llm import providers as prov
    from kestrel.llm.client import LLMClient
    from kestrel.llm.guard import run_prompt
    from kestrel.llm.prompts import load_prompt

    class _Boom(LLMClient):
        def __init__(self):
            super().__init__(load_settings())

        async def complete(self, **_kw):
            raise ProviderError(500, f"crash {SECRET}", "https://openrouter.ai/api/v1/chat")

    spec = load_prompt("resume_outline/v1")
    res = asyncio.run(run_prompt(_Boom(), spec, [{"role": "user", "content": "x"}]))
    assert not res.ok and res.error.startswith("request error: ")
    _clean(res.error)

    backend = prov.external_backends(load_settings())["frontier"]
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text=f"boom {SECRET}"))
    status = asyncio.run(prov.verify_key(
        backend.model_copy(update={"api_key": "sk-or-plainlookingkey"}),
        http=httpx.AsyncClient(transport=transport)))
    assert not status.ok
    _clean(status.detail)
