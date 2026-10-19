"""D-LIC-1: the third-party notices cover the runtime closure, never dev tools, and carry the
license text the MIT/BSD/Apache libraries require."""

from __future__ import annotations

import re

from kestrel.services import notices


def test_runtime_closure_has_runtime_deps_and_no_dev_tools():
    names = {d.metadata["Name"].lower() for d in notices.runtime_closure()}
    assert {"python-jobspy", "fastapi", "pystray"} <= names
    assert not names & {"pytest", "ruff", "pyinstaller", "respx", "kestrel"}


def test_notices_carry_jobspy_license_text_and_copyright():
    text = notices.build_notices()
    section = text.split("python-jobspy ", 1)[1].split("=" * 78, 2)
    assert "License: MIT" in section[0]
    assert "Copyright (c) 2023 Cullen Watson" in text
    assert "Permission is hereby granted" in text


def test_notices_route_serves_text(monkeypatch):
    from tests.test_hosted_mode import _fresh_app

    monkeypatch.setattr(notices, "build_notices", lambda *a: "stub notices\n")
    r = _fresh_app().get("/about/notices")
    assert r.status_code == 200 and r.text == "stub notices\n"
    assert r.headers["content-type"].startswith("text/plain")


def test_bundled_file_wins_over_a_live_build(monkeypatch, tmp_path):
    path = tmp_path / "n.txt"
    path.write_text("from the build\n", encoding="utf-8")

    class _F:
        def __truediv__(self, name):
            return path

    monkeypatch.setattr("importlib.resources.files", lambda pkg: _F())
    assert notices.notices_text() == "from the build\n"


def test_no_notices_file_is_committed_and_no_source_counts_as_a_license():
    from importlib.resources import files

    assert not (files("kestrel.resources") / notices.NOTICES_FILENAME).is_file()
    for dist in notices.runtime_closure():
        for fname, _body in notices._license_texts(dist):
            assert not fname.endswith((".py", ".pyc")), (dist.metadata["Name"], fname)


def test_spec_bundles_notices_and_ships_lgpl_as_plain_files():
    from tests.test_packaging import PACKAGING

    spec = (PACKAGING / "kestrel.spec").read_text(encoding="utf-8")
    assert "write_notices(" in spec and '"kestrel/resources"' in spec
    # loaded by path: the package __init__ never runs in the build process
    assert not re.search(r"^\s*(from|import)\s+kestrel\b", spec, re.MULTILINE)
    assert "len(_closure) < 50" in spec
    assert 'module_collection_mode={"pystray": "py", "Xlib": "py"}' in spec
