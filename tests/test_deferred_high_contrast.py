"""Deferred color schemes: configuration, persistence, first paint and exact Undo."""
from __future__ import annotations

import pytest

from kestrel.config import UI, UI_SCHEMES
from tests.test_settings_routes import _client, _row, _undo

pytestmark = pytest.mark.usefixtures("_isolate_data_home")
SCHEMES = ['contrast']


@pytest.mark.parametrize("scheme", SCHEMES)
def test_scheme_saves_renders_on_other_pages_and_undo_restores_absent_default(scheme):
    assert UI(scheme=scheme).scheme == scheme and scheme in UI_SCHEMES
    with _client() as c:
        before = c.get("/settings/section/preferences").text
        assert f'data-st-scheme="{scheme}"' in before
        assert 'style=' not in before.split('aria-label="Color scheme"')[1].split('</div>')[0]
        r = c.post("/settings/prefs/scheme", json={"scheme": scheme}).json()
        assert r["ok"] and _row("ui.scheme") == scheme
        for url in ("/settings", "/help", "/pursuits", "/profile"):
            page = c.get(url)
            assert page.status_code == 200
            assert f'data-scheme="{scheme}"' in page.text.split('</head>')[0]
        pane = c.get("/settings/section/preferences").text
        assert f'aria-checked="true" data-st-scheme="{scheme}"' in pane
        bad = c.post("/settings/prefs/scheme", json={"scheme": "unknown"}).json()
        assert not bad["ok"] and _row("ui.scheme") == scheme
        assert _undo(c, r["undoUrl"])["ok"]
        assert _row("ui.scheme") is None
        assert 'data-scheme="indigo"' in c.get("/help").text


@pytest.mark.parametrize("scheme", SCHEMES)
def test_scheme_legacy_save_and_undo_never_overwrite_a_later_choice(scheme):
    with _client() as c:
        assert c.post("/settings/appearance", data={"scheme": scheme}).status_code == 200
        r = c.post("/settings/prefs/scheme", json={"scheme": "teal"}).json()
        assert c.post("/settings/prefs/scheme", json={"scheme": scheme}).json()["ok"]
        assert not _undo(c, r["undoUrl"])["ok"]
        assert _row("ui.scheme") == scheme
