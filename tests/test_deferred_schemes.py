"""Deferred color schemes: configuration, persistence, first paint and exact Undo."""
from __future__ import annotations

import pytest

from kestrel.config import UI, UI_SCHEMES
from tests.test_settings_routes import _client, _row, _undo

pytestmark = pytest.mark.usefixtures("_isolate_data_home")
SCHEMES = ['plum', 'graphite']


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


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_all_text_colors_including_statuses_and_avatar_pass_aa(theme, scheme):
    from tests.test_ui_tokens import PAIRS, _tokens, contrast

    tokens = _tokens(theme, scheme)
    pairs = PAIRS + [("good", ("paper", "bg", "good-bg")),
                     ("warn", ("paper", "bg", "warn-bg")),
                     ("bad", ("paper", "bg", "bad-bg")),
                     ("brand-ink", ("brand-2",)), ("paper", ("brand", "ink")),
                     ("tg3", ("tg3i",))]
    for fg, grounds in pairs:
        for bg in grounds:
            assert contrast(tokens[fg], tokens[bg]) >= 4.5, (theme, scheme, fg, bg)
