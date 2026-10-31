"""D-UI-39: the Quit confirmation is an in-page modal in the app's theme, not the WebView2 (Edge)
native confirm box. Markup checks only (no browser in CI): the dialog renders only when the shell
can quit, it is labeled and keyboard accessible (Esc cancels, Tab wraps, Enter confirms), it uses
theme variables rather than fixed colors, and nothing on the quit path calls window.confirm."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.web import desktop_bridge

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

PARTIAL = "partials/quit_confirm.html"


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


@pytest.fixture
def desktop():
    desktop_bridge.set_quit_handler(lambda: None)
    yield
    desktop_bridge.set_quit_handler(None)


def _source(name: str) -> str:
    from pathlib import Path

    return (Path(paths.web_templates_dir()) / name).read_text(encoding="utf-8")


def test_no_dialog_without_a_shell():
    desktop_bridge.set_quit_handler(None)
    body = _client().get("/help").text
    assert 'data-testid="quit-confirm"' not in body and "kestrelQuit" not in body


def test_quit_button_opens_the_themed_dialog_not_a_native_confirm(desktop):
    body = _client().get("/help").text
    button = re.search(r'<button[^>]*data-testid="quit-app"[^>]*>', body, re.S)
    assert button, "quit button missing"
    assert "confirm(" not in button.group(0)
    assert "kestrelQuit.open(this)" in button.group(0)
    assert 'aria-haspopup="dialog"' in button.group(0)
    assert '<dialog id="quit-confirm"' in body
    assert 'aria-labelledby="quit-confirm-title"' in body
    assert 'aria-describedby="quit-confirm-desc"' in body
    assert "Scheduled searches stop until you open it again." in body
    assert 'data-testid="quit-cancel"' in body and 'data-testid="quit-confirm-go"' in body


def test_no_window_confirm_remains_on_the_quit_path():
    for name in ("base.html", PARTIAL):
        text = _source(name)
        assert not re.search(r"(?<![\w.])confirm\(", text), name
        assert "window.confirm" not in text, name
        assert "onbeforeunload" not in text and "beforeunload" not in text, name


def test_keyboard_handling_is_wired():
    js = _source(PARTIAL)
    assert "showModal" in js
    assert "'Escape'" in js and "addEventListener('cancel'" in js  # Esc cancels
    assert "'Enter'" in js and "quit()" in js  # Enter confirms
    assert "'Tab'" in js and "shiftKey" in js  # the focus trap wraps both ways
    assert "go.focus()" in js and "opener.focus()" in js  # focus in, and back out on cancel
    # Quit is otherwise what the button always did.
    assert "fetch('/api/quit', {method: 'POST'})" in js
    # The closing screen of the locked launch mockup (D-UI-90), not a bare line of text.
    assert "KUI.frame.closing()" in js and "innerHTML" not in js


def test_dialog_uses_theme_variables_only():
    from pathlib import Path

    text = _source(PARTIAL)
    css = (Path(paths.web_static_dir()) / "css" / "kestrel.css").read_text(encoding="utf-8")
    style = "\n".join(re.findall(r"^\.quit-confirm[^{]*\{[^}]*\}", css, re.M | re.S))
    assert style and "style=" not in text.split("<script>")[0]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", style)
    assert "white" not in style.lower() and "orange" not in style.lower()
    for var in ("var(--panel)", "var(--text)", "var(--border)", "var(--muted)"):
        assert var in style
    assert "—" not in text  # no em dashes in UI copy
