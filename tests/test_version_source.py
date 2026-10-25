"""Every version the About and Help pages show comes from one runtime source: the package version
(`kestrel.__version__`, SPEC-INTERFACES 1.4) for Kestrel, and installed package metadata for each
component. Nothing is written into a template, a page module or a script. The pages are rendered
with a version no release has, so a hardcoded copy cannot pass by coincidence."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import kestrel
from kestrel import paths

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_FAKE = "41.2.3"
_ROOT = Path(__file__).resolve().parents[1]
_WEB = _ROOT / "kestrel" / "web"
# A dotted version number: two or three numeric parts, not part of a longer number or a path.
_VERSION = re.compile(r"(?<![\w./-])\d+\.\d+(?:\.\d+)+(?![\w-])")


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def test_about_shows_the_runtime_package_version(monkeypatch):
    real = kestrel.__version__
    with _client() as c:
        assert f">Version {real}<" in c.get("/about").text
    monkeypatch.setattr(kestrel, "__version__", _FAKE)
    with _client() as c:
        body = c.get("/about").text
    assert f">Version {_FAKE}<" in body and f"Version {real}<" not in body


def test_the_update_answer_names_the_running_version_from_the_same_source(monkeypatch):
    """The shared check reports the running version too; it reads the module attribute at request
    time, so changing it changes the answer."""
    import asyncio

    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine, reset_engine
    from kestrel.services import updater

    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()
    seen: list[str] = []

    async def fake(session, *, feed_url, current=None, transport=None):
        seen.append(current)
        return updater.UpdateState(latest_version=current, last_check_ok=True)

    monkeypatch.setattr(updater, "check_now", fake)
    monkeypatch.setattr(kestrel, "__version__", _FAKE)
    with _client() as c:
        shared = c.post("/updates/check").text
        about = c.post("/updates/check", data={"surface": "about"}).text
    assert seen == [_FAKE, _FAKE]
    assert f"running {_FAKE}" in shared and "You have the latest version" in about


def test_component_versions_are_read_from_installed_metadata(monkeypatch):
    from importlib import metadata

    from kestrel.web.pages import about

    real = metadata.version

    def fake(name: str) -> str:
        return "7.7.7" if name in ("fastapi", "sqlalchemy") else real(name)

    monkeypatch.setattr(about, "pkg_version", fake)
    rows = dict(about.component_versions())
    assert rows["FastAPI"] == "7.7.7" and rows["SQLAlchemy"] == "7.7.7"
    with _client() as c:
        body = c.get("/about").text
    assert "<td>FastAPI</td><td>7.7.7</td>" in body


def test_a_missing_component_skips_its_row_instead_of_failing_the_page(monkeypatch):
    from importlib.metadata import PackageNotFoundError

    from kestrel.web.pages import about

    def gone(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(about, "pkg_version", gone)
    labels = [label for label, _v in about.component_versions()]
    assert labels[0] == "Python" and "FastAPI" not in labels
    with _client() as c:
        assert c.get("/about").status_code == 200


def test_the_copy_for_a_bug_report_is_built_from_the_page_not_from_the_script():
    script = (_WEB / "static" / "js" / "about.js").read_text(encoding="utf-8")
    assert "about-version" in script and "ab-comp" in script
    assert not _VERSION.search(script), _VERSION.search(script)


def test_no_template_script_or_help_text_hardcodes_a_version_number():
    """The pages and their data are free of version literals; the one place a number may appear is
    the context the page module fills at request time."""
    files = [
        _WEB / "templates" / "about.html",
        _WEB / "templates" / "help.html",
        _WEB / "templates" / "partials" / "about_update_result.html",
        _WEB / "templates" / "partials" / "diagnostics_saved.html",
        _WEB / "static" / "js" / "about.js",
        _WEB / "static" / "js" / "help.js",
        _WEB / "help_articles.py",
        _WEB / "pages" / "help.py",
        _WEB / "pages" / "about.py",
    ]
    for f in files:
        text = f.read_text(encoding="utf-8")
        # PolyForm Noncommercial 1.0.0 is the license's own name, not a Kestrel version.
        text = text.replace("1.0.0", "")
        hits = _VERSION.findall(text)
        assert not hits, (f.name, hits)


def test_about_and_help_pages_show_no_version_number_other_than_the_runtime_ones(monkeypatch):
    monkeypatch.setattr(kestrel, "__version__", _FAKE)
    with _client() as c:
        about = c.get("/about").text
        helped = c.get("/help").text
    main = about.split("<main")[1].split("</main>")[0].replace("1.0.0", "")
    # Dotted numbers on the page are Kestrel's (the fake), a component's, the OS or the port.
    allowed = {_FAKE}
    for line in re.findall(r"<tr><td>[^<]+</td><td>([^<]+)</td></tr>", main):
        allowed.update(_VERSION.findall(line))
    import platform

    allowed.update(_VERSION.findall(platform.release()))
    allowed.update(_VERSION.findall(platform.mac_ver()[0]))
    stray = [v for v in _VERSION.findall(main) if v not in allowed]
    assert not stray, stray
    help_main = helped.split("<main")[1].replace("1.0.0", "")
    assert _FAKE not in help_main and not _VERSION.findall(help_main.split('id="hp-articles"')[0])
