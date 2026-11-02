"""Release round R1: keys in Settings only (paste, Test, Save; the key never in a response or a
log, only its last four characters), no UI string that sends the user to `.env` or names a key
variable outside a Show details toggle, OpenRouter listed first with "Free models available",
the onboarding local step's buttons and details, and the Linux WebKit panel with the imports
mocked. Provider calls are mocked with respx."""

from __future__ import annotations

import logging
import re

import pytest
import respx
from httpx import Response
from structlog.testing import capture_logs

from kestrel import paths
from kestrel.desktop import linux_check
from kestrel.llm import providers as prov
from tests.test_hosted_mode import _fresh_app
from tests.test_onboarding import HX, _app, _jump, machine  # noqa: F401 - machine is autouse

_OR = "https://openrouter.ai/api/v1"
KEY = "sk-or-v1-abcdefghijklmnop7Q9z"


def _outside_details(html: str) -> str:
    return re.sub(r"<details\b.*?</details>", "", html, flags=re.DOTALL)


def _assert_no_key_plumbing(html: str) -> None:
    visible = _outside_details(html)
    assert ".env" not in visible
    assert not re.search(r"[A-Z]+_API_KEY", visible), re.findall(r"[A-Z]+_API_KEY", visible)


# ------------------------------------------------------------------------------- Your keys
def test_key_test_then_save_never_echoes_or_logs_the_key(monkeypatch, caplog):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    caplog.set_level(logging.DEBUG)
    with capture_logs() as logs, respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{_OR}/key").mock(
            return_value=Response(200, json={"data": {"label": "k", "is_free_tier": True}})
        )
        r = client.post("/settings/hosted/keybox",
                        data={"provider": "openrouter", "op": "test", "api_key": KEY})
        assert route.calls[0].request.headers["authorization"] == f"Bearer {KEY}"
    assert r.status_code == 200
    assert 'data-key-ok="true"' in r.text and "This key works with OpenRouter" in r.text
    assert "free tier" in r.text and "Press Save to keep it" in r.text
    assert KEY not in r.text
    assert not paths.dotenv_path().exists()  # Test writes nothing

    with capture_logs() as more:
        r = client.post("/settings/hosted/keybox",
                        data={"provider": "openrouter", "op": "save", "api_key": KEY})
    assert r.status_code == 200 and KEY not in r.text
    assert "OpenRouter key saved, ending in 7Q9z" in r.text
    assert 'data-testid="key-status-openrouter">Key saved, ends in <span class="mono">7Q9z' \
        in r.text
    assert prov.secret_value("OPENROUTER_API_KEY") == KEY
    for entry in [*logs, *more]:
        assert KEY not in repr(entry)
    assert KEY not in caplog.text

    # A blank Test checks the saved key; the page still shows only the last four.
    with respx.mock as mock:
        mock.get(f"{_OR}/key").mock(return_value=Response(200, json={"data": {}}))
        r = client.post("/settings/hosted/keybox", data={"provider": "openrouter", "op": "test"})
    assert "Your saved key works with OpenRouter" in r.text and KEY not in r.text
    assert KEY not in client.get("/settings/hosted").text

    r = client.post("/settings/hosted/keybox", data={"provider": "openrouter", "op": "remove"})
    assert "OpenRouter key removed." in r.text
    assert not prov.secret_present("OPENROUTER_API_KEY")


def test_a_rejected_key_says_so_and_saves_nothing(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    with respx.mock as mock:
        mock.get(f"{_OR}/key").mock(return_value=Response(401, json={"error": {"message": "no"}}))
        r = client.post("/settings/hosted/keybox",
                        data={"provider": "openrouter", "op": "test", "api_key": KEY})
    assert 'data-key-ok="false"' in r.text and "did not accept this key" in r.text
    assert "Nothing was saved" in r.text and KEY not in r.text
    assert not prov.secret_present("OPENROUTER_API_KEY")
    _assert_no_key_plumbing(r.text)
    # Nothing pasted, nothing saved: a plain instruction, no network call.
    r = client.post("/settings/hosted/keybox", data={"provider": "openrouter", "op": "test"})
    assert "Paste your OpenRouter key first." in r.text
    assert "Choose a provider." in client.post(
        "/settings/hosted/keybox", data={"provider": "nope", "op": "test"}
    ).text


# --------------------------------------------------------------------- no .env outside details
def test_settings_hosted_names_no_key_file_or_variable_outside_details(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    body = client.get("/settings/hosted").text
    _assert_no_key_plumbing(body)
    details = body.split('data-testid="key-details"')[1]
    assert ".env" in details and "OPENROUTER_API_KEY" in details  # power users still find it
    _assert_no_key_plumbing(client.get("/settings").text)
    system = client.get("/settings/system").text
    _assert_no_key_plumbing(system)
    assert "kestrel setup" not in _outside_details(system)  # terminal commands under details
    assert "kestrel setup --hosted" in system


def test_messages_in_code_send_the_user_to_settings_not_env():
    from kestrel.llm import client as llm_client

    assert ".env" not in llm_client._HTTP_HINTS[401]
    assert "Settings > Hosted" in llm_client._HTTP_HINTS[401]
    from kestrel import doctor

    assert doctor._backend_label("frontier") == "OpenRouter"


def test_onboarding_model_step_names_no_key_file_and_lists_openrouter_first():
    c = _app()
    _jump("models")
    r = c.post("/onboarding/models/mode", data={"mode": "hosted"}, headers=HX)
    _assert_no_key_plumbing(r.text)
    ids = re.findall(r'<li data-provider="([a-z]+)"', r.text)
    assert ids[0] == "openrouter" and len(ids) == len(prov.PRESETS)
    assert 'data-testid="free-models-openrouter"' in r.text
    first_li = r.text.split('<li data-provider="openrouter">')[1].split("</li>")[0]
    assert "Free models available" in first_li
    radios = re.findall(r'name="provider" value="([a-z]+)"', r.text)
    assert radios[0] == "openrouter"
    assert r.text.count("Free models available") == 2  # the key list and the radio, nowhere else


def test_settings_hosted_lists_openrouter_first_with_free_models(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    body = _fresh_app().get("/settings/hosted").text
    rows = re.findall(r'<form class="key-row" data-provider="([a-z]+)"', body)
    assert rows[0] == "openrouter" and set(rows) == set(prov.PRESETS)
    connect = body.split('data-testid="hosted-connect"')[1]
    radios = re.findall(r'name="provider" value="([a-z]+)"', connect)
    assert radios[0] == "openrouter"
    assert 'data-testid="free-models-openrouter"' in body
    assert "free-models-openai" not in body


# ------------------------------------------------------------------------ onboarding local
def test_onboarding_local_step_has_buttons_and_commands_only_under_details():
    c = _app()
    _jump("models")
    r = c.post("/onboarding/models/mode", data={"mode": "local"}, headers=HX)
    assert 'data-testid="download-ollama"' in r.text
    assert 'hx-post="/onboarding/models/action/check"' in r.text
    visible = _outside_details(r.text)
    for cmd in ("ollama serve", "ollama pull", "ollama create", "curl -fsSL", "winget install"):
        assert cmd not in visible, cmd
    assert "ollama serve" in r.text  # still there for power users
    r = c.post("/onboarding/models/action/check", headers=HX)
    assert r.status_code == 200 and 'data-testid="ob-local"' in r.text
    r = c.post("/onboarding/models/action/pull", headers=HX)
    assert "Ollama is not running" in r.text  # the autouse machine has Ollama down


# ------------------------------------------------------------------------------- Linux panel
class _FakeGi:
    def __init__(self, ok: set[str]) -> None:
        self.ok = ok

    def require_version(self, ns: str, version: str) -> None:
        if f"{ns} {version}" not in self.ok:
            raise ValueError(f"Namespace {ns} not available for version {version}")


def _imports(monkeypatch, table: dict[str, object]) -> None:
    def fake(name: str):
        value = table.get(name, ImportError(f"No module named {name!r}"))
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(linux_check, "_import", fake)


def test_linux_detection_names_what_is_missing(monkeypatch):
    _imports(monkeypatch, {})
    gap = linux_check.detect("linux")
    assert gap is not None and "PyGObject" in gap.missing
    distros = [d for d, _ in gap.install]
    assert distros[0].startswith("Debian") and "Fedora" in distros and distros[2].startswith("Arch")
    assert all(cmd.startswith("sudo ") for _, cmd in gap.install)

    gi = _FakeGi({"Gtk 3.0"})
    _imports(monkeypatch, {"gi": gi, "gi.repository.Gtk": object()})
    gap = linux_check.detect("linux")
    assert gap is not None and "WebKit2GTK" in gap.missing
    assert "WebKit2 4.1" in gap.detail and "WebKit2 4.0" in gap.detail

    gi = _FakeGi({"Gtk 3.0", "WebKit2 4.0"})
    _imports(monkeypatch, {"gi": gi, "gi.repository.Gtk": object(),
                           "gi.repository.WebKit2": object()})
    assert linux_check.detect("linux") is None

    _imports(monkeypatch, {"PyQt6.QtWebEngineWidgets": object()})  # Qt works instead
    assert linux_check.detect("linux") is None
    _imports(monkeypatch, {})
    assert linux_check.detect("win32") is None and linux_check.detect("darwin") is None


@pytest.fixture
def no_gap():
    linux_check.clear()
    yield
    linux_check.clear()


def test_browser_fallback_shows_the_panel_with_installs_under_details(monkeypatch, no_gap):
    client = _fresh_app()
    assert 'data-testid="linux-webkit"' not in client.get("/").text  # no fallback: no panel
    _imports(monkeypatch, {})
    assert linux_check.record_fallback("WebViewException: no backend", "darwin") is None
    gap = linux_check.record_fallback("WebViewException: no backend", "linux")
    assert gap is not None and "no backend" in gap.detail
    body = client.get("/").text
    assert 'data-testid="linux-webkit"' in body
    assert "Kestrel opened in your browser because its own window could not open" in body
    visible = _outside_details(body.split('data-testid="linux-webkit"')[1].split("</div>")[0])
    assert "sudo" not in visible
    details = body.split('data-testid="linux-webkit-details"')[1]
    assert "sudo apt install" in details and "sudo dnf install" in details
    assert "sudo pacman -S" in details
    assert "add the missing piece" in body  # a from-source run: the install brings the window

    # The packaged AppImage carries no GTK binding: the panel does not promise an install fixes it.
    gap.frozen = True
    body = client.get("/").text
    assert "this build of Kestrel uses your browser as its window" in body
    assert "add the missing piece" not in body
    assert "Running Kestrel from its source code instead?" in body


def test_onboarding_shows_the_linux_panel_too(monkeypatch, no_gap):
    _imports(monkeypatch, {})
    linux_check.record_fallback("no backend", "linux")
    c = _app()
    assert 'data-testid="linux-webkit"' in c.get("/onboarding").text


def test_saving_a_pasted_sentence_as_a_key_is_refused_and_writes_nothing(monkeypatch):
    """#62 on Settings: the Your keys Save and the older key form apply the setup key check."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    sentence = "this is half a paragraph of prose \u2014 not a key"
    with respx.mock(assert_all_called=False) as mock:
        r = client.post("/settings/hosted/keybox",
                        data={"provider": "openrouter", "op": "save", "api_key": sentence})
        assert not mock.calls
    assert r.status_code == 200 and 'data-key-ok="false"' in r.text
    assert "does not look like an API key" in r.text and "Nothing was saved" in r.text
    assert sentence not in r.text and not paths.dotenv_path().exists()
    r = client.post("/settings/hosted/key", data={"backend": "frontier", "api_key": sentence})
    assert "does not look like an API key" in r.text and sentence not in r.text
    assert not paths.dotenv_path().exists()
