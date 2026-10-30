"""Settings > This computer > Online model: the Free models only switch. Every provider call is
mocked with respx; nothing reaches the network."""

from __future__ import annotations

import respx
from httpx import Response

from tests.test_hosted_connect import _OR, _mock_openrouter
from tests.test_settings_routes import _client, _db, _row

_FREE_MODELS = {"data": [
    {"id": "meta/free-a:free", "context_length": 64000,
     "pricing": {"prompt": "0", "completion": "0"},
     "supported_parameters": ["structured_outputs"]},
    {"id": "b/strong", "context_length": 128000,
     "pricing": {"prompt": "0.000003", "completion": "0.000015"},
     "supported_parameters": ["structured_outputs"]},
]}


def _acknowledge() -> None:
    from kestrel.config import HostedMode
    from kestrel.db.models import Setting

    async def f(s):
        s.add(Setting(key="llm.hosted_mode.acknowledged_at", value="2026-10-01T00:00:00+00:00"))
        s.add(Setting(key="llm.hosted_mode.acknowledged_terms",
                      value=HostedMode().disclosure_terms()))

    _db(f, commit=True)


def _post(c, **data):
    return c.post("/settings/computer/free", data=data)


def test_card_shows_the_switch_with_the_saved_state(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-ok")
    with _client() as c:
        off = c.get("/settings/computer/mode").text
        assert 'data-testid="st-free-switch"' in off and "Free models only" in off
        assert "role=\"switch\"" in off and "—" not in off
        assert "checked" not in off.split('id="st-free-sw"')[1].split(">")[0]
        from kestrel.db.models import Setting

        async def put(s):
            s.add(Setting(key="llm.hosted_mode.free_only", value=True))

        _db(put, commit=True)
        on = c.get("/settings/computer/mode").text
        assert "checked" in on.split('id="st-free-sw"')[1].split(">")[0]
        assert 'id="st-free-fold"' in on and "is-on" in on.split('id="st-free-fold"')[0][-60:]


def test_on_without_the_tick_is_refused_and_writes_nothing(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-ok")
    with _client() as c, respx.mock(assert_all_called=False) as router:
        _acknowledge()
        route = router.get(f"{_OR}/models").mock(return_value=Response(200, json=_FREE_MODELS))
        r = _post(c, free_only="on")
        assert "Nothing was changed" in r.text and "train" in r.text
        assert "HX-Trigger" not in r.headers
        assert "checked" in r.text.split('id="st-free-sw"')[1].split(">")[0]  # not reset
        assert _row("llm.hosted_mode.free_only") is None
        assert _row("llm.hosted_tiers.tier1.model") is None
        assert not route.called


def test_on_with_the_tick_connects_through_the_one_path(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-ok")
    with _client() as c, respx.mock as router:
        _acknowledge()
        _mock_openrouter(router)
        router.get(f"{_OR}/models").mock(return_value=Response(200, json=_FREE_MODELS))
        r = _post(c, free_only="on", allow_training="on")
        assert "meta/free-a:free" in r.text and 'class="st-note st-note--good"' in r.text
        assert _row("llm.hosted_mode.free_only") is True
        assert _row("llm.backends.frontier.hosted.data_collection") == "allow"
        assert _row("llm.hosted_tiers.tier1.model") == "meta/free-a:free"
        assert _row("llm.mode") != "hosted"  # run mode is left alone


def test_on_before_what_is_sent_is_approved_changes_nothing(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-ok")
    with _client() as c:
        r = _post(c, free_only="on", allow_training="on")
        assert "Approve what is sent" in r.text and "Nothing was changed" in r.text
        assert _row("llm.hosted_mode.free_only") is None


def test_off_stores_off_and_keeps_the_picks(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-ok")
    with _client() as c, respx.mock as router:
        _acknowledge()
        _mock_openrouter(router)
        router.get(f"{_OR}/models").mock(return_value=Response(200, json=_FREE_MODELS))
        _post(c, free_only="on", allow_training="on")
        r = _post(c)
        assert "Your current models stay; change them on Models." in r.text
        assert _row("llm.hosted_mode.free_only") is False
        assert _row("llm.hosted_tiers.tier1.model") == "meta/free-a:free"
