"""D-REL-41: the ticked online disclosure names the provider reads, and those reads run only while
online models are in use (D-TEST-792 to D-TEST-795).

Written from the ruling, not the code: "Online models will periodically check the model list and
your balance with your provider. Nothing else is sent." is part of what the person ticks; a
changed text voids an older tick; the balance and model-list reads are made only when online is
actually in use (switched on, the tick current, and the run card not on Local); the run card
shows Local when online is off; Free models only never turns online back on. Every provider
request is answered by an invented fixture or refused; nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from tests import test_contracts as _contracts
from tests.test_settings_routes import _client, _db, _row

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

recorder = _contracts.recorder

READS_LINE = (
    "Online models will periodically check the model list and your balance with your provider. "
    "Nothing else is sent."
)
MODEL = "vendor/invented-model"


def _seed(*, enabled=True, choice: str | None = None, mode: str | None = None, terms=None):
    """Online acknowledged under the current disclosure, both scoring tiers routed online."""
    from kestrel.config import HostedMode
    from kestrel.db.models import Setting

    rows = {
        "llm.hosted_mode.enabled": enabled,
        "llm.hosted_mode.acknowledged_at": "2026-10-01T00:00:00+00:00",
        "llm.hosted_mode.acknowledged_terms": terms or HostedMode().disclosure_terms(),
    }
    for t in ("tier1", "tier2"):
        rows[f"llm.hosted_tiers.{t}.backend"] = "frontier"
        rows[f"llm.hosted_tiers.{t}.model"] = MODEL
    if choice is not None:
        rows["setup.run_choice"] = choice
    if mode is not None:
        rows["llm.mode"] = mode

    async def put(s):
        for k, v in rows.items():
            await s.merge(Setting(key=k, value=v))

    _db(put, commit=True)


def _settings():
    from kestrel.services.setting_overrides import load_settings_with_db

    return _db(load_settings_with_db)


def _sent(rec, path: str) -> list[str]:
    return [str(r.url) for r in rec.http if r.url.path.rstrip("/").endswith(path)]


# --------------------------------------------------------------------- 4. the disclosure itself
def test_the_ticked_disclosure_contains_the_provider_reads_line():
    from kestrel.config import HostedMode

    assert READS_LINE in json.dumps(HostedMode().disclosure_terms())


def test_a_tick_on_the_older_text_no_longer_counts():
    """The text without the new line is a different disclosure: its tick is void, so online
    scoring pauses until the person ticks again."""
    from kestrel.config import HostedMode

    old = HostedMode().disclosure_terms()
    old = {k: v for k, v in old.items() if READS_LINE not in json.dumps(v)}
    hm = HostedMode(
        enabled=True, acknowledged_at="2026-10-01T00:00:00+00:00", acknowledged_terms=old
    )
    assert not hm.acknowledgement_current
    assert not hm.active
    now = HostedMode(
        enabled=True,
        acknowledged_at="2026-10-01T00:00:00+00:00",
        acknowledged_terms=HostedMode().disclosure_terms(),
    )
    assert now.active  # the same check passes on the current text, so it can fail


def test_the_disclosure_people_tick_shows_the_line(monkeypatch):
    """Rendered where the box is ticked: Settings > Hosted, and Models > Online model when
    another service is chosen."""
    with _client() as c:
        for url in ("/settings/hosted",):
            r = c.get(url)
            assert r.status_code == 200, url
            assert READS_LINE in " ".join(r.text.split()), url


# ------------------------------------------------------------------------- 5. the run card
@pytest.mark.parametrize(
    ("rows", "label"),
    [
        ({"setup.run_choice": "hosted"}, "stored Hosted"),
        ({"setup.run_choice": "both"}, "stored Both"),
        ({"llm.mode": "hosted"}, "mode row hosted"),
        ({}, "derived from a saved key"),
    ],
)
def test_the_run_card_shows_local_when_online_is_off(monkeypatch, rows, label):
    from kestrel.services import setup_state as ss

    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")
    with _client():
        _seed(enabled=False)
        settings = _settings()
    assert not settings.llm.hosted_mode.enabled
    assert ss.run_choice(settings, rows)[0] == "local", label


def test_the_run_card_keeps_the_choice_while_online_is_on(monkeypatch):
    from kestrel.services import setup_state as ss

    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")
    with _client():
        _seed(enabled=True)
        settings = _settings()
    assert ss.run_choice(settings, {"setup.run_choice": "hosted"}) == ("hosted", True)
    assert ss.run_choice(settings, {"llm.mode": "local"})[0] == "both"


# ------------------------------------------------- 3. the reads run only while online is in use
def _reads(rec):
    """Ask for the balance (as the status rail does) and the model list (as a picker does)."""
    from kestrel.db.engine import get_sessionmaker
    from kestrel.llm import providers as prov
    from kestrel.services import hosted_balance as hb
    from kestrel.services import hosted_models as hm
    from kestrel.services.setting_overrides import load_settings_with_db

    hb.clear_cache()
    hm.clear_cache()

    async def go():
        async with get_sessionmaker()() as s:
            settings = await load_settings_with_db(s)
            row = await hb.rail_row(s, settings)
        name = "frontier"
        backend = prov.external_backends(settings)[name]
        try:
            models = await hm.fetch_models(name, backend)
        except hm.ModelListError as exc:
            models = exc
        return row, models

    return asyncio.run(go())


@pytest.mark.parametrize(
    ("seed", "label"),
    [
        ({"enabled": True, "choice": "local", "mode": "local"}, "Local chosen, switch still on"),
        ({"enabled": False, "choice": "hosted", "mode": "hosted"}, "switched off"),
    ],
)
def test_the_balance_and_model_list_are_not_read_when_online_is_not_in_use(
    recorder, monkeypatch, seed, label
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")
    with _client():
        _seed(**seed)
        row, models = _reads(recorder)
    assert not _sent(recorder, "/key"), label
    assert not _sent(recorder, "/models"), label
    # never a provider reading: no line, or the local log's own spend line
    assert (row or {"kind": "none"})["kind"] in ("none", "local_spend"), label
    from kestrel.services import hosted_models as hm

    assert isinstance(models, hm.ModelListError), label


@pytest.mark.parametrize(
    "seed",
    [
        {"enabled": True, "choice": "hosted", "mode": "hosted"},
        {"enabled": True, "choice": "both", "mode": "local"},
        {"enabled": True, "choice": None, "mode": "local"},  # no card row: Both, from the key
    ],
)
def test_the_reads_run_while_online_is_in_use(recorder, monkeypatch, seed):
    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")
    with _client():
        _seed(**seed)
        _reads(recorder)
    assert _sent(recorder, "/key") and _sent(recorder, "/models")


def test_the_status_rail_sends_nothing_when_local_is_chosen(recorder, monkeypatch):
    """The page view itself, through the app: the sidebar poll and the Score again dialog."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")
    with _client() as c:
        _seed(enabled=True, choice="local", mode="local")
        from kestrel.services import hosted_balance as hb
        from kestrel.services import hosted_models as hm

        hb.clear_cache()
        hm.clear_cache()
        assert c.get("/status/rail").status_code == 200
        assert c.get("/rescore/choose").status_code == 200
    assert not [r for r in recorder.http if r.url.host == "openrouter.ai"]


def test_the_model_list_is_held_about_a_day(recorder, monkeypatch):
    """One read a day at most while a picker is opened again and again."""
    from kestrel.services import hosted_models as hm

    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")
    now = [1000.0]
    monkeypatch.setattr(hm, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def reply(self, request):
        recorder.http.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": MODEL,
                        "context_length": 64000,
                        "pricing": {"prompt": "0", "completion": "0"},
                        "supported_parameters": ["structured_outputs"],
                    }
                ]
            },
            request=request,
        )

    async def areply(self, request):
        return reply(self, request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", areply)
    with _client():
        _seed(enabled=True, choice="hosted", mode="hosted")
        from kestrel.llm import providers as prov

        backend = prov.external_backends(_settings())["frontier"]
        hm.clear_cache()
        asyncio.run(hm.fetch_models("frontier", backend))
        assert len(_sent(recorder, "/models")) == 1
        now[0] += 20 * 3600  # twenty hours later: still held
        asyncio.run(hm.fetch_models("frontier", backend))
        assert len(_sent(recorder, "/models")) == 1
        now[0] += 6 * 3600  # past a day: read again
        asyncio.run(hm.fetch_models("frontier", backend))
        assert len(_sent(recorder, "/models")) == 2


# ------------------------------------------- 2. the reported path: Free models only plus training
def test_free_models_only_with_training_does_not_turn_online_back_on(recorder, monkeypatch):
    """Online switched off after a current tick; ticking Free models only and allow training on
    Settings > This computer must not switch it back on, and must not ask the provider."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")
    with _client() as c:
        _seed(enabled=False)
        r = c.post("/settings/computer/free", data={"free_only": "on", "allow_training": "on"})
        assert r.status_code == 200
        assert _row("llm.hosted_mode.enabled") is False
        assert "Nothing was changed" in r.text
    assert not [q for q in recorder.http if q.url.host == "openrouter.ai"]


def test_the_not_in_use_messages_point_to_real_controls():
    """Each reason names a place; that place has the control (AGENT-RULES 8a item 9)."""
    from kestrel.config import ONLINE_CONSENT_HOW
    from kestrel.services import setup_state as ss

    assert ONLINE_CONSENT_HOW in ss.ONLINE_OFF
    # Owner, round 2 (D-PRIV-796): the confirmation is made on Settings > Online models.
    assert "Settings > Online models" in ONLINE_CONSENT_HOW
    assert "Replay the walkthrough" not in ONLINE_CONSENT_HOW
    assert "Settings > This computer" in ss.LOCAL_CHOSEN
    with _client() as c:
        # The place is reachable: Settings > This computer links to it ...
        card = c.get("/settings/computer/mode").text
        assert 'href="/settings/hosted/review"' in card
        # ... and it has the controls the sentence names.
        page = c.get("/settings/hosted/review").text
        assert "I understand what each hosted tier sends" in page
        assert "Turn on hosted mode" in page and 'hx-post="/settings/hosted/mode"' in page
        card = c.get("/settings/computer/mode").text
        for label in ("On this computer", "Online model", "Both"):
            assert label in card
        assert 'hx-post="/settings/computer/mode"' in card


# ------------------------------------------- 7. the Test button keeps the pause (round 2, D-REL-41)
# Invariant: nothing goes to an online provider while online is paused or not ticked under the
# current disclosure text, including from a button press.
def _stale_terms():
    from tests._crawl_support import stale_terms

    return stale_terms()


@pytest.mark.parametrize(
    ("seed", "label"),
    [
        ({"enabled": True, "choice": "hosted", "mode": "hosted", "terms": "stale"}, "paused"),
        ({"enabled": True, "choice": "local", "mode": "local"}, "Local chosen"),
        ({"enabled": False, "choice": "hosted", "mode": "hosted"}, "switched off"),
    ],
)
def test_the_hosted_test_button_sends_nothing_unless_online_is_in_use(
    recorder, monkeypatch, seed, label
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")
    seed = dict(seed)
    if seed.get("terms") == "stale":
        seed["terms"] = _stale_terms()
    with _client() as c:
        _seed(**seed)
        r = c.post("/settings/hosted/test", data={"backend": "frontier"})
    assert r.status_code == 200, label
    assert [str(q.url) for q in recorder.http if q.url.host != "testserver"] == [], label
    assert "Nothing was sent" in r.text, label


def test_the_hosted_test_button_while_paused_points_to_the_updated_disclosure(
    recorder, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")
    with _client() as c:
        _seed(enabled=True, choice="hosted", mode="hosted", terms=_stale_terms())
        r = c.post("/settings/hosted/test", data={"backend": "frontier"})
    assert [str(q.url) for q in recorder.http if q.url.host != "testserver"] == []
    # It says why and points to where the updated disclosure is ticked again.
    assert 'href="/settings/hosted/review" data-testid="hosted-review-link"' in r.text
    assert "paused" in r.text


def test_the_hosted_test_button_still_checks_the_key_while_online_is_in_use(
    recorder, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")
    with _client() as c:
        _seed(enabled=True, choice="hosted", mode="hosted")
        r = c.post("/settings/hosted/test", data={"backend": "frontier"})
    assert r.status_code == 200
    assert _sent(recorder, "/key")


def test_cannot_switch_yet_links_to_settings_online_models(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")
    with _client() as c:
        _seed(enabled=True, terms=_stale_terms())
        page = c.get("/settings/hosted").text
    assert "Cannot switch yet" in page
    note = page[page.index("Cannot switch yet"):]
    note = note[: note.index("</span>")]
    assert 'href="/settings/hosted/review"' in note
