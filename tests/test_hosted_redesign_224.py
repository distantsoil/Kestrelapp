"""#224: Settings > Online models was redesigned; nothing about consent may change with it.

The invariant: nothing is sent and no consent is written except through the same forms posting
the same field names to the same routes as before the redesign; the disclosure text renders in
exactly one place per response; the tick label is unchanged. The only form removed from the page
is the one posting /settings/hosted/switch (owner decision). Checked in every state the page has:
no provider, provider set up but off, paused by a changed disclosure, and online in use, on both
GET /settings/hosted (the panel) and GET /settings/hosted/review (the page).
Cloud-safe: a migrated SQLite file, no network (page views make no provider request)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, reset_engine
from kestrel.llm import providers
from tests._crawl_support import stale_terms

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

# Invented: an OpenRouter-shaped key and a model id that belong to nothing real.
FAKE_KEY = "sk-or-v1-" + "0123456789abcdef" * 4
MODEL = "vendor/invented-model"
TICK = "I understand what each hosted tier sends and that the provider's retention rules apply."
# The first sentence of the disclosure macro (partials/hosted_disclosure.html, D-REL-41 wording).
DISCLOSURE_HEAD = "Read before turning this on. Hosted mode sends opportunity and profile text"
URLS = ("/settings/hosted", "/settings/hosted/review")

# Every route a form on this page posted to at the base (1ed8608e), with the field names each
# form carried. /settings/hosted/switch is the one the owner removed.
CONNECT_FIELDS = {"provider", "api_key", "ack", "switch", "free_only", "allow_training"}
MODE_ON_FIELDS = {"action", "ack"}
MODE_OFF_FIELDS = {"action"}
ALLOWED_ROUTES = {
    "/settings/hosted/keybox", "/settings/hosted/balance", "/settings/hosted/connect",
    "/settings/hosted/mode", "/settings/hosted/tiers", "/settings/hosted/test",
    "/settings/hosted/model",
}


async def _prepare() -> None:
    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()


def _rows(rows: dict[str, object]) -> None:
    con = sqlite3.connect(paths.db_path())
    try:
        for k, v in rows.items():
            con.execute(
                "INSERT OR REPLACE INTO setting(key, value, updated_at) VALUES (?, ?, ?)",
                (k, json.dumps(v), "2026-11-04T00:00:00+00:00"),
            )
        con.commit()
    finally:
        con.close()


def _world(state: str) -> TestClient:
    """Seed the stored rows for one state of the page, then start the app on them."""
    from kestrel.config import HostedMode
    from kestrel.web.app import build_app

    asyncio.run(_prepare())
    rows: dict[str, object] = {}
    if state != "fresh":
        for t in ("tier1", "tier2"):
            rows[f"llm.hosted_tiers.{t}.backend"] = "frontier"
            rows[f"llm.hosted_tiers.{t}.model"] = MODEL
        providers.write_secret("OPENROUTER_API_KEY", FAKE_KEY)
    if state in ("paused", "active"):
        rows["llm.hosted_mode.enabled"] = True
        rows["llm.hosted_mode.acknowledged_at"] = "2026-10-15T00:00:00Z"
        rows["llm.hosted_mode.acknowledged_terms"] = (
            stale_terms() if state == "paused" else HostedMode().disclosure_terms())
    if state == "active":
        rows["llm.mode"] = "hosted"
        rows["setup.run_choice"] = "hosted"
    _rows(rows)
    return TestClient(build_app(worker=False))


class _Forms(HTMLParser):
    """Every form on the page: its hx-post route and the names of its fields, plus the text of
    each label that holds an ack checkbox."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []
        self._form: dict | None = None
        self._label: list[str] | None = None
        self._label_has_ack = False
        self.ack_labels: list[str] = []
        self.ack_checked = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._form = {"route": a.get("hx-post") or a.get("action") or "", "fields": set(),
                          "attrs": a, "values": {}}
            self.forms.append(self._form)
        elif tag in ("input", "select", "textarea", "button") and a.get("name"):
            if self._form is not None:
                self._form["fields"].add(a["name"])
                if a.get("type") == "hidden":
                    self._form["values"].setdefault(a["name"], []).append(a.get("value"))
            if a["name"] == "ack" and a.get("type") == "checkbox":
                self._label_has_ack = True
                if "checked" in a:
                    self.ack_checked += 1
        elif tag == "label":
            self._label, self._label_has_ack = [], False

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "label" and self._label is not None:
            if self._label_has_ack:
                self.ack_labels.append(" ".join("".join(self._label).split()))
            self._label = None

    def handle_data(self, data):
        if self._label is not None:
            self._label.append(data)


def _parse(html: str) -> _Forms:
    p = _Forms()
    p.feed(html)
    return p


STATES = ("fresh", "configured", "paused", "active")


@pytest.mark.parametrize("state", STATES)
def test_the_disclosure_renders_exactly_once_in_every_state(state: str) -> None:
    c = _world(state)
    for url in URLS:
        html = c.get(url).text
        assert html.count(DISCLOSURE_HEAD) == 1, (state, url, html.count(DISCLOSURE_HEAD))
        assert html.count("Keys stay on this machine and are never sent anywhere but the "
                          "provider.") == 1, (state, url)


@pytest.mark.parametrize("state", STATES)
def test_every_form_posts_the_same_fields_to_the_same_routes(state: str) -> None:
    c = _world(state)
    for url in URLS:
        page = _parse(c.get(url).text)
        routes = {f["route"] for f in page.forms}
        assert routes <= ALLOWED_ROUTES, (state, url, routes - ALLOWED_ROUTES)
        connect = [f for f in page.forms if f["route"] == "/settings/hosted/connect"]
        assert len(connect) == 1 and connect[0]["fields"] == CONNECT_FIELDS, (state, url)
        modes = [f for f in page.forms if f["route"] == "/settings/hosted/mode"]
        # Online on: one Turn-off form. Online off: one Turn-on form, the hidden "off" before the
        # checkbox, as before.
        on = state == "active"
        want_action, want_fields = (["off"], MODE_OFF_FIELDS) if on else (["on"], MODE_ON_FIELDS)
        assert [f["values"].get("action") for f in modes] == [want_action], (state, url)
        assert modes[0]["fields"] == want_fields, (state, url)
        assert modes[0]["values"].get("ack") == (None if on else ["off"]), (state, url)
        by_route: dict[str, list[set[str]]] = {}
        for f in page.forms:
            by_route.setdefault(f["route"], []).append(f["fields"])
        keybox = by_route["/settings/hosted/keybox"]
        assert keybox and all(fs == {"provider", "api_key", "op"} for fs in keybox), keybox
        balance = by_route.get("/settings/hosted/balance", [])
        assert all(fs == {"show_balance"} for fs in balance), balance
        assert len(balance) == (0 if state == "fresh" else 1), (state, url)  # OpenRouter key only
        (tiers,) = by_route["/settings/hosted/tiers"]
        assert all(n.split(".")[0] in ("backend", "model") for n in tiers), tiers
        assert {"backend.tier1", "model.tier1", "backend.tier2", "model.tier2",
                "backend.frontier", "model.frontier"} <= tiers
        for f in page.forms:
            assert f["attrs"].get("hx-target") == "#hosted-panel", f
        # The tick label is unchanged wherever the ack box is, and it is never pre-ticked.
        assert page.ack_labels and all(lab == TICK for lab in page.ack_labels), page.ack_labels
        assert page.ack_checked == 0


@pytest.mark.parametrize("state", STATES)
def test_the_run_switch_form_is_gone_from_the_page(state: str) -> None:
    c = _world(state)
    for url in URLS:
        html = c.get(url).text
        assert "/settings/hosted/switch" not in html, (state, url)
        assert 'id="mode-switch"' not in html, (state, url)


def test_connect_is_open_until_a_provider_is_set_up() -> None:
    import re

    def connect_open(html: str) -> bool:
        tag = re.search(r'<details id="hosted-connect"[^>]*>', html)
        assert tag is not None
        return " open" in tag.group(0)

    html = _world("fresh").get("/settings/hosted/review").text
    assert connect_open(html) and "Connect a provider" in html
    for state in ("configured", "paused", "active"):
        html = _world(state).get("/settings/hosted/review").text
        assert not connect_open(html) and "Change provider" in html, state


def test_connect_carries_return_to_when_set_and_nothing_else_new() -> None:
    # Coming from Setup, Connect posts one more field, return_to, as it did before the redesign.
    c = _world("fresh")
    page = _parse(c.get("/settings/hosted?return_to=setup").text)
    connect = [f for f in page.forms if f["route"] == "/settings/hosted/connect"]
    assert len(connect) == 1 and connect[0]["fields"] == CONNECT_FIELDS | {"return_to"}


@pytest.mark.parametrize(("state", "data"), [
    ("fresh", {"action": "on", "ack": "off"}),
    ("configured", {"action": "on", "ack": "off"}),
    ("active", {"action": "off"}),
])
def test_a_posted_answer_also_shows_the_disclosure_once_and_no_switch(state, data) -> None:
    # Every POST under /settings/hosted answers with the same panel; the rule holds there too.
    c = _world(state)
    html = c.post("/settings/hosted/mode", data=data).text
    assert html.count(DISCLOSURE_HEAD) == 1, (state, html.count(DISCLOSURE_HEAD))
    assert "/settings/hosted/switch" not in html
    page = _parse(html)
    assert page.ack_checked == 0 and all(lab == TICK for lab in page.ack_labels)


@pytest.mark.parametrize("state", STATES)
def test_the_diagram_moves_only_while_online_models_are_on(state: str) -> None:
    html = _world(state).get("/settings/hosted/review").text
    flow = html.split('data-testid="hosted-flow"')[1].split("</figure>")[0]
    moving = "ho-f-line--on" in flow
    assert moving is (state == "active"), state
    # Both forms of the picture carry the same spoken label.
    assert flow.count('role="img"') == 2


def test_balance_toggletips_links_and_lanes_are_there() -> None:
    import re

    html = _world("configured").get("/settings/hosted/review").text
    # Providers without a balance read get a no-script toggletip: a <details> with a summary.
    for pid in ("anthropic", "openai"):
        tip = re.search(rf'<details[^>]*data-testid="balance-tip-{pid}"[^>]*>\s*<summary', html)
        assert tip, pid
    assert 'data-testid="balance-tip-openrouter"' not in html
    assert "Show balance with free models too" in html
    assert re.search(r'href="/settings#computer"[^>]*data-testid="hosted-to-computer"', html)
    assert re.search(r'href="/models#mo-network-card"[^>]*data-testid="hosted-to-network"', html)
    # The lanes map and its narrow form are decorative; the tier rows say the same in words.
    for testid in ("hosted-lanes", "hosted-lanes-compact"):
        assert re.search(rf'aria-hidden="true"[^>]*data-testid="{testid}"', html), testid
