"""Page lifecycle, group 1 (D-UI-210 stage 2): About, Analytics, Help, Calibrate, Models and Model
check run on KUI.page. Cloud-safe: the server renders the pages (no network, no model) and the
scripts are read as text; the live mount / unmount / mount leak tests are in each page's
*_browser.py."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kestrel import paths

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_JS = Path(paths.web_static_dir()) / "js"

# path, page name, the script the page loads (Model check also loads the shared picker script)
_PAGES = [
    ("/about", "about", ["about.js"]),
    ("/analytics", "analytics", ["analytics.js"]),
    ("/help", "help", ["help.js"]),
    ("/calibrate", "calibrate", ["calibrate.js"]),
    ("/models", "models", ["models.js"]),
    ("/bench", "bench", ["model_combo.js", "bench.js"]),
]


def _client() -> TestClient:
    from kestrel.db import migrate
    from kestrel.web.app import build_app

    migrate.run_migrations()
    return TestClient(build_app(worker=False))


def _code(name: str) -> str:
    """A script without its comments, so the checks below read only what runs."""
    text = (_JS / name).read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def test_each_page_names_itself_and_loads_its_script_after_the_shared_ui():
    with _client() as c:
        for path, name, scripts in _PAGES:
            body = c.get(path).text
            assert f'<main class="container" data-k-page="{name}">' in body, path
            ui = body.index("/static/js/kestrel-ui.js")
            alpine = body.index("/static/js/alpine.min.js")
            for script in scripts:
                tag = re.search(
                    rf'<script src="[^"]*/static/js/{script}[^"]*" defer></script>', body)
                assert tag, (path, script)
                # After kestrel-ui.js (so KUI.page.define exists as the script runs), before Alpine.
                assert ui < tag.start() < alpine, (path, script)


def test_each_page_script_defines_its_page_and_starts_nothing_at_top_level():
    for _path, name, scripts in _PAGES:
        for script in scripts:
            if script == "model_combo.js":
                continue  # shared singleton, see the next test
            code = _code(script)
            defines = (f"KUI.page.define('{name}'", f'KUI.page.define("{name}"')
            assert any(d in code for d in defines), script
            # Every listener, timer and observer goes through the mount context.
            for bare in ("addEventListener", "setInterval", "setTimeout", "clearTimeout",
                         "clearInterval", "DOMContentLoaded", "MutationObserver",
                         "ResizeObserver", "pushState", "replaceState"):
                assert bare not in code, (script, bare)


def test_the_shared_picker_script_is_a_singleton_that_binds_each_picker_once():
    code = _code("model_combo.js")
    # One definition however often the macro emits the script; the one htmx listener sits in it.
    assert code.count("if (!window.keCombo)") == 1
    assert code.count('document.addEventListener("htmx:load"') == 1
    # A picker is bound once per element (a WeakSet), not by a flag in its markup, and a scan may
    # be limited to the swapped element.
    assert "WeakSet" in code and "bound.has(r)" in code
    assert ":not([data-ready])" not in code
    assert "scan: function (scope)" in code
    # It does not use the page lifecycle: it belongs to no page.
    assert "KUI.page" not in code


def test_the_picker_macro_still_emits_the_script_beside_each_picker():
    text = (Path(paths.web_templates_dir()) / "partials" / "model_picker.html").read_text("utf-8")
    assert "js/model_combo.js" in text
