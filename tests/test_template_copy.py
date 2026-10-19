"""Template copy rules: no em dashes in visible text (CLAUDE.md output style)."""

from __future__ import annotations

import re
from pathlib import Path

from kestrel import paths


def test_no_em_dash_in_visible_template_text():
    bad = []
    for f in Path(paths.web_templates_dir()).rglob("*.html"):
        text = re.sub(r"\{#.*?#\}", "", f.read_text(encoding="utf-8"), flags=re.S)
        if "—" in text or "&mdash;" in text:
            bad.append(f.name)
    assert not bad, bad


def test_iso_to_local_returns_input_on_bad_timestamp():
    from kestrel.web.deps import _iso_to_local

    assert _iso_to_local("not a date") == "not a date"
    assert _iso_to_local(None) == ""
