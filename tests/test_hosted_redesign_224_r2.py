"""#224 round 2 (Fable review r1, deferred items 1 to 3): Settings > Online models says what the
stored state does.

1. A legacy `llm.tiers.<tier>.backend = <online backend>` row is named on the page with a control
   that posts the existing POST /settings/hosted/switch mode=local, which removes the row
   (`_unroute_legacy_external_tiers`) as the removed Run Kestrel on switch did.
2. With On this computer chosen and online models on, every send is held (D-PRIV-798,
   `setup_state.online_send_held`), so the diagram does not move or say "Sending now." and the
   status line does not say a Run now choice sends.
3. While online models are off nothing leaves this machine: the page does not claim a deeper read
   does (the deeper-read route refuses while off).

Invariant: these are words and one form on an existing route; nothing new is sent, the run card
choice is never written by the legacy control, and a page view writes nothing. Invented data only.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from html import unescape

from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.config import HostedMode
from kestrel.llm import providers
from tests.test_hosted_redesign_224 import FAKE_KEY, MODEL, _prepare, _rows

ACK = "2026-10-15T00:00:00Z"
LEGACY_MODEL = "invented/legacy-model"


def _app(extra: dict[str, object]) -> TestClient:
    from kestrel.web.app import build_app

    asyncio.run(_prepare())
    rows: dict[str, object] = {}
    for t in ("tier1", "tier2"):
        rows[f"llm.hosted_tiers.{t}.backend"] = "frontier"
        rows[f"llm.hosted_tiers.{t}.model"] = MODEL
    providers.write_secret("OPENROUTER_API_KEY", FAKE_KEY)
    rows.update(extra)
    _rows(rows)
    return TestClient(build_app(worker=False))


def _on(choice: str, mode: str) -> dict[str, object]:
    return {
        "llm.hosted_mode.enabled": True,
        "llm.hosted_mode.acknowledged_at": ACK,
        "llm.hosted_mode.acknowledged_terms": HostedMode().disclosure_terms(),
        "llm.mode": mode,
        "setup.run_choice": choice,
    }


def _stored() -> dict[str, object]:
    con = sqlite3.connect(paths.db_path())
    try:
        return {k: json.loads(v) for k, v in con.execute("SELECT key, value FROM setting")}
    finally:
        con.close()


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def _flow(html: str) -> str:
    return html.split('data-testid="hosted-flow"')[1].split("</figure>")[0]


def _status(html: str) -> str:
    return _text(html.split('data-testid="hosted-ack-status"')[1].split("</p>")[0])


def _held_now() -> str | None:
    from kestrel.services import setup_state
    from kestrel.services.setting_overrides import load_settings_with_db
    from kestrel.web.deps import sessionmaker

    async def go():
        async with sessionmaker()() as s:
            return setup_state.online_send_held(await load_settings_with_db(s))

    return asyncio.run(go())


# ------------------------------------------------------------------------- item 1: legacy row
def _legacy(choice: str, mode: str) -> TestClient:
    return _app({
        **_on(choice, mode),
        "llm.tiers.tier1.backend": "frontier",
        "llm.tiers.tier1.model": LEGACY_MODEL,
    })


def _legacy_form(html: str) -> str:
    warn = html.split('data-testid="hosted-legacy-row"')[1].split("</div>")[0]
    forms = re.findall(r"<form\b[^>]*>.*?</form>", warn, re.S)
    assert len(forms) == 1, warn
    return forms[0]


def test_legacy_row_warning_offers_the_existing_switch_route() -> None:
    for url in ("/settings/hosted", "/settings/hosted/review"):
        html = _legacy("both", "local").get(url).text
        form = _legacy_form(html)
        assert 'hx-post="/settings/hosted/switch"' in form
        names = re.findall(r'name="([^"]+)"', form)
        assert names == ["mode"]
        assert re.search(r'name="mode" value="local"', form)
        assert "<button" in form and 'type="submit"' in form
        assert "frontier/" + LEGACY_MODEL in _text(html)


def test_pressing_it_removes_the_row_and_keeps_the_run_choice() -> None:
    client = _legacy("both", "local")
    before = _stored()
    r = client.post("/settings/hosted/switch", data={"mode": "local"})
    assert r.status_code == 200
    assert "Also moved back to the local model: tier1" in r.text
    after = _stored()
    assert "llm.tiers.tier1.backend" not in after and "llm.tiers.tier1.model" not in after
    # Only the legacy rows went: run card, mode, tick and online set are unchanged.
    for k in ("setup.run_choice", "llm.mode", "llm.hosted_mode.enabled",
              "llm.hosted_mode.acknowledged_at", "llm.hosted_mode.acknowledged_terms",
              "llm.hosted_tiers.tier1.backend", "llm.hosted_tiers.tier2.model"):
        assert after[k] == before[k], k
    assert 'data-testid="hosted-legacy-row"' not in client.get("/settings/hosted").text


def test_on_an_online_run_the_control_says_it_switches_to_local() -> None:
    form = _legacy_form(_legacy("hosted", "hosted").get("/settings/hosted/review").text)
    assert "local" in _text(form).lower() and "run" in _text(form).lower()


def test_a_page_view_with_a_legacy_row_writes_nothing() -> None:
    client = _legacy("both", "local")
    before = _stored()
    client.get("/settings/hosted")
    client.get("/settings/hosted/review")
    assert _stored() == before


# ------------------------------------------------------------- item 2: On this computer chosen
def test_held_on_this_computer_the_diagram_is_still_and_says_held() -> None:
    client = _app(_on("local", "local"))
    assert _held_now() is not None  # the owning function holds every send here (D-PRIV-798)
    for url in ("/settings/hosted", "/settings/hosted/review"):
        html = client.get(url).text
        flow = _flow(html)
        assert "ho-f-line--on" not in flow
        assert not re.search(r'<figure class="[^"]*\bis-on\b[^"]*" data-testid="hosted-flow"', html)
        assert "Sending now" not in flow
        assert "Settings &gt; This computer" in flow or "Settings > This computer" in flow
        status = _status(html)
        assert "Run now" not in status
        assert "This computer" in status


def test_not_held_the_diagram_still_moves_and_says_sending() -> None:
    client = _app(_on("hosted", "hosted"))
    assert _held_now() is None
    html = client.get("/settings/hosted/review").text
    assert "ho-f-line--on" in _flow(html) and "Sending now." in _flow(html)
    assert "Run now" in _status(html)


# ----------------------------------------------------------------- item 3: off says nothing leaves
def test_off_the_page_never_says_a_deeper_read_leaves() -> None:
    off = {
        "llm.hosted_mode.enabled": False,
        "llm.hosted_mode.acknowledged_at": ACK,
        "llm.hosted_mode.acknowledged_terms": HostedMode().disclosure_terms(),
    }
    for extra in ({}, off):
        client = _app(extra)
        for url in ("/settings/hosted", "/settings/hosted/review"):
            html = client.get(url).text
            text = _text(html)
            assert "the only thing that leaves it" not in text
            assert "deeper read you preview and confirm" not in text
            assert "Nothing is sent until you turn online models on." in text
            assert "nothing leaves it" in _status(html)
