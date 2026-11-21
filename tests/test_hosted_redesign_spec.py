"""Spec tests for #224 (Settings > Online models redesign), written from the interface contract
alone, without the implementation.

Invariant: nothing is sent and no consent is written except through the same forms posting the same
field names to the same routes as before; the exact disclosure text renders in exactly one place
per response; the tick label is unchanged. Cloud-safe: a migrated SQLite file, provider calls
mocked with respx, no live network.

The new-structure tests (ids, testids, labels) are expected to fail until the redesign merges; the
route and field invariants hold on both sides of it."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, reset_engine
from kestrel.llm import providers
from tests._crawl_support import stale_terms

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

TICK = "I understand what each hosted tier sends and that the provider's retention rules apply."
FAKE_KEY = "sk-or-v1-" + "0123456789abcdef" * 4
PAGES = ("/settings/hosted/review", "/settings/hosted")
_OR = "https://openrouter.ai/api/v1"

# state name -> (provider set up, hosted mode enabled, acknowledgement: None | "current" | "stale")
STATES: dict[str, tuple[bool, bool, str | None]] = {
    "no-provider": (False, False, None),
    "set-up-off-acked": (True, False, "current"),
    "set-up-off-never-acked": (True, False, None),
    "reaccept": (True, True, "stale"),
    "active": (True, True, "current"),
}
STATE_IDS = list(STATES)

H = "/settings/hosted"
# route -> field names a form on that route may carry (the same fields as before the redesign).
ALLOWED: dict[str, set[str]] = {
    f"{H}/keybox": {"provider", "op", "api_key", "return_to"},
    f"{H}/connect": {
        "provider", "api_key", "ack", "switch", "free_only", "allow_training", "return_to",
    },
    f"{H}/mode": {"action", "ack", "return_to"},
    f"{H}/balance": {"show_balance", "return_to"},
    f"{H}/tiers": {"return_to"},  # plus backend.<tier> and model.<tier>
    f"{H}/test": {"backend", "free_only", "return_to"},
    f"{H}/model": {
        "backend", "model", "protocol", "zdr", "data_collection", "return_to",
    },  # plus reasoning_effort.<name>
    f"{H}/key": {"backend", "api_key", "return_to"},  # the older single-key route
}
PREFIXED = {
    f"{H}/tiers": re.compile(r"^(backend|model)\.[a-z0-9_]+$"),
    f"{H}/model": re.compile(r"^reasoning_effort\.[A-Za-z0-9_.-]+$"),
}


# ------------------------------------------------------------------------------------- the world
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
    """Seed the stored rows the named state has, then start the app on them."""
    from kestrel.config import HostedMode
    from kestrel.web.app import build_app

    provider, enabled, ack = STATES[state]
    asyncio.run(_prepare())
    rows: dict[str, object] = {"llm.hosted_mode.enabled": enabled}
    if ack is not None:
        rows["llm.hosted_mode.acknowledged_at"] = "2026-10-15T00:00:00Z"
        rows["llm.hosted_mode.acknowledged_terms"] = (
            HostedMode().disclosure_terms() if ack == "current" else stale_terms()
        )
    if provider:
        for tier in ("tier1", "tier2"):
            rows[f"llm.hosted_tiers.{tier}.backend"] = "frontier"
            rows[f"llm.hosted_tiers.{tier}.model"] = "a/cheap"
        providers.write_secret("OPENROUTER_API_KEY", FAKE_KEY)
    _rows(rows)
    return TestClient(build_app(worker=False))


# -------------------------------------------------------------------------------- a tiny DOM
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
        "track", "wbr"}


@dataclass
class El:
    tag: str
    attrs: dict[str, str | None]
    parent: El | None = None
    children: list[El | str] = field(default_factory=list)

    def walk(self):
        yield self
        for c in self.children:
            if isinstance(c, El):
                yield from c.walk()

    def text(self) -> str:
        out: list[str] = []
        for c in self.children:
            if isinstance(c, str):
                out.append(c)
            elif c.tag not in ("script", "style"):
                out.append(c.text())
        return " ".join(out)

    def ancestors(self):
        p = self.parent
        while p is not None:
            yield p
            p = p.parent

    def has(self, name: str) -> bool:
        return name in self.attrs


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = El("#root", {})
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        el = El(tag, dict(attrs), self.cur)
        self.cur.children.append(el)
        if tag not in VOID:
            self.cur = el

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(El(tag, dict(attrs), self.cur))

    def handle_endtag(self, tag):
        node = self.cur
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.cur = node.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def _dom(html: str) -> El:
    b = _Builder()
    b.feed(html)
    b.close()
    return b.root


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _page_text(root: El) -> str:
    return _norm(root.text())


def _by_testid(root: El, testid: str) -> list[El]:
    return [e for e in root.walk() if e.attrs.get("data-testid") == testid]


def _by_id(root: El, ident: str) -> list[El]:
    return [e for e in root.walk() if e.attrs.get("id") == ident]


@dataclass
class Form:
    el: El
    route: str
    fields: dict[str, list[tuple[str, str | None, bool]]]  # name -> (type, value, checked)

    @property
    def names(self) -> set[str]:
        return set(self.fields)

    def values(self, name: str) -> list[str | None]:
        return [v for (_t, v, _c) in self.fields.get(name, [])]


def _forms(root: El) -> list[Form]:
    out: list[Form] = []
    for f in (e for e in root.walk() if e.tag == "form"):
        route = (f.attrs.get("hx-post") or f.attrs.get("action") or "").split("?")[0]
        fields: dict[str, list[tuple[str, str | None, bool]]] = {}
        for e in f.walk():
            if e.tag in ("input", "select", "textarea", "button") and e.attrs.get("name"):
                fields.setdefault(e.attrs["name"] or "", []).append(
                    (e.attrs.get("type") or e.tag, e.attrs.get("value"), e.has("checked"))
                )
        out.append(Form(f, route, fields))
    return out


def _disclosure_text(client: TestClient) -> str:
    """The locked macro rendered with the same context the pages use, as plain normalized text."""
    from kestrel.web.deps import templates
    from kestrel.web.pages.settings import _tier_sends

    async def _settings():
        from kestrel.db.engine import get_sessionmaker
        from kestrel.services.setting_overrides import load_settings_with_db

        async with get_sessionmaker()() as s:
            return await load_settings_with_db(s)

    settings = asyncio.run(_settings())
    tmpl = templates().env.from_string(
        '{% from "partials/hosted_disclosure.html" import disclosure_text with context %}'
        "{{ disclosure_text() }}"
    )
    html = tmpl.render(tier_sends=_tier_sends(settings))
    return _page_text(_dom(html))


def _get(state: str, page: str) -> tuple[TestClient, str, El]:
    c = _world(state)
    r = c.get(page)
    assert r.status_code == 200
    return c, r.text, _dom(r.text)


def _is_open(el: El) -> bool:
    """Open means no <details> from the element up to the root is collapsed."""
    return all(d.has("open") for d in [el, *el.ancestors()] if d.tag == "details")


# ------------------------------------------------------------------------------------- tests
@pytest.mark.parametrize("page", PAGES)
@pytest.mark.parametrize("state", STATE_IDS)
def test_disclosure_text_appears_exactly_once(state: str, page: str) -> None:
    c, _html, root = _get(state, page)
    want = _disclosure_text(c)
    assert len(want) > 200
    assert _page_text(root).count(want) == 1


@pytest.mark.parametrize("page", PAGES)
@pytest.mark.parametrize("state", STATE_IDS)
def test_tick_label_is_inside_each_ack_form_and_nowhere_else(state: str, page: str) -> None:
    _c, _html, root = _get(state, page)
    ack_forms = [f for f in _forms(root) if "ack" in f.names]
    for f in ack_forms:
        assert TICK in _norm(f.el.text()), f.route
    assert _page_text(root).count(TICK) == len(ack_forms)


@pytest.mark.parametrize("page", PAGES)
@pytest.mark.parametrize("state", STATE_IDS)
def test_no_form_posts_to_the_run_switch(state: str, page: str) -> None:
    _c, html, root = _get(state, page)
    assert all(f.route != f"{H}/switch" for f in _forms(root))
    assert f"{H}/switch" not in html
    assert not _by_id(root, "mode-switch")


@pytest.mark.parametrize("page", PAGES)
@pytest.mark.parametrize("state", STATE_IDS)
def test_every_form_posts_known_fields_to_a_known_route(state: str, page: str) -> None:
    _c, _html, root = _get(state, page)
    forms = _forms(root)
    assert forms
    for f in forms:
        if f.route.startswith("/models") or f.route == f"{H}/switch":
            continue  # the run switch has its own test; Models-page forms are not constrained
        assert f.route in ALLOWED, f.route
        pat = PREFIXED.get(f.route)
        extra = {n for n in f.names - ALLOWED[f.route] if pat is None or not pat.match(n)}
        assert not extra, (f.route, extra)


@pytest.mark.parametrize("state", STATE_IDS)
def test_tiers_form_keeps_backend_and_model_names(state: str) -> None:
    _c, _html, root = _get(state, "/settings/hosted/review")
    tiers = [f for f in _forms(root) if f.route == f"{H}/tiers"]
    assert len(tiers) == 1
    for tier in ("tier1", "tier2", "docsel", "frontier"):
        assert f"backend.{tier}" in tiers[0].names and f"model.{tier}" in tiers[0].names
    assert len(_by_id(root, "hosted-assignments")) == 1


@pytest.mark.parametrize("page", PAGES)
@pytest.mark.parametrize("state", STATE_IDS)
def test_connect_form_is_open_with_no_provider_and_collapsed_otherwise(
    state: str, page: str
) -> None:
    _c, _html, root = _get(state, page)
    connect = _by_testid(root, "hosted-connect")
    assert len(connect) == 1
    assert connect[0].attrs.get("id") == "hosted-connect"
    forms = [f for f in _forms(root) if f.route == f"{H}/connect"]
    assert len(forms) == 1
    assert {"provider", "api_key", "ack"} <= forms[0].names
    provider_set_up = STATES[state][0]
    assert _is_open(connect[0]) == (not provider_set_up)
    assert ("Change provider" in _page_text(root)) == provider_set_up


@pytest.mark.parametrize("page", PAGES)
@pytest.mark.parametrize("state", STATE_IDS)
def test_turn_on_and_turn_off_forms_follow_the_state(state: str, page: str) -> None:
    _c, _html, root = _get(state, page)
    provider_set_up, enabled, ack = STATES[state]
    active = enabled and ack == "current"
    modes = [f for f in _forms(root) if f.route == f"{H}/mode"]
    on = [f for f in modes if "on" in f.values("action")]
    off = [f for f in modes if "off" in f.values("action")]
    # D-UI-825: the Turn-on form renders whenever online is off, including before a provider
    # is set up (tests/test_privacy_disclosure.py requires the mode form on the fresh page).
    assert provider_set_up or state == "no-provider"
    assert len(on) == (0 if active else 1)
    assert len(off) == (1 if active else 0)
    assert len(modes) == len(on) + len(off)
    for f in on:
        entries = f.fields["ack"]
        assert any(t == "checkbox" for (t, _v, _c) in entries)
        assert any(t == "hidden" and v == "off" for (t, v, _c) in entries)
        assert not any(c for (t, _v, c) in entries if t == "checkbox")  # never pre-ticked
    page_text = _page_text(root)
    assert ("Turn on online models" in page_text) == bool(on)
    assert ("Turn off online models" in page_text) == bool(off)


@pytest.mark.parametrize("page", PAGES)
@pytest.mark.parametrize("state", STATE_IDS)
def test_hosted_mode_wording_is_gone(state: str, page: str) -> None:
    _c, html, root = _get(state, page)
    # Labels the page controls say "online". Messages built in Python (config.ONLINE_CONSENT_HOW)
    # keep "Turn on hosted mode" until #228, so the check is on controls, not on all page text.
    controls = [e for e in root.walk() if e.tag in ("button", "summary", "label", "h2", "h3")]
    for el in controls:
        assert "hosted mode" not in _norm(el.text()).lower(), _norm(el.text())
    assert "Turn on online models" in _page_text(root) or "Turn off online models" in html


@pytest.mark.parametrize("page", PAGES)
@pytest.mark.parametrize("state", STATE_IDS)
def test_disclosure_section_holds_the_one_disclosure_and_is_open_until_acknowledged(
    state: str, page: str
) -> None:
    c, _html, root = _get(state, page)
    section = _by_id(root, "hosted-disclosure")
    assert len(section) == 1
    assert section[0].attrs.get("data-testid") == "hosted-disclosure-section"
    want = _disclosure_text(c)
    assert _page_text(section[0]).count(want) == 1
    expect_open = STATES[state][2] != "current"
    assert _is_open(section[0])  # the section itself is never inside a closed fold
    folds = [
        d for d in section[0].walk() if d.tag == "details" and want[:60] in _norm(d.text())
    ]
    assert folds  # the disclosure sits in a fold
    assert folds[-1].has("open") == expect_open  # innermost fold: closed once acknowledged


@pytest.mark.parametrize("page", PAGES)
@pytest.mark.parametrize("state", STATE_IDS)
def test_advanced_is_closed_and_links_back_to_this_computer(state: str, page: str) -> None:
    _c, _html, root = _get(state, page)
    adv = _by_id(root, "hosted-advanced")
    assert len(adv) == 1 and adv[0].tag == "details"
    assert adv[0].attrs.get("data-testid") == "hosted-advanced"
    assert not adv[0].has("open")
    link = _by_testid(root, "hosted-to-computer")
    assert len(link) == 1 and link[0].tag == "a"
    assert link[0].attrs.get("href") == "/settings#computer"


@pytest.mark.parametrize("state", STATE_IDS)
def test_keys_section_and_balance_form(state: str) -> None:
    _c, _html, root = _get(state, "/settings/hosted/review")
    keys = _by_id(root, "hosted-keys")
    assert len(keys) == 1 and keys[0].attrs.get("data-testid") == "hosted-keys"
    keybox = [f for f in _forms(root) if f.route == f"{H}/keybox"]
    assert keybox
    for f in keybox:
        assert {"provider", "op"} <= f.names
    balance = [f for f in _forms(root) if f.route == f"{H}/balance"]
    for f in balance:
        assert f.names - {"return_to"} == {"show_balance"}
    # a saved OpenRouter key offers the balance toggle; none without a provider
    assert len(balance) == (1 if STATES[state][0] else 0)
    assert all("Show balance with free models too" in _norm(f.el.text()) for f in balance)


def test_paused_banner_and_jump_link_on_reaccept() -> None:
    _c, _html, root = _get("reaccept", "/settings/hosted/review")
    assert len(_by_testid(root, "hosted-review-paused")) == 1
    jump = _by_testid(root, "hosted-review-jump")
    assert len(jump) == 1 and jump[0].tag == "a"
    assert jump[0].attrs.get("href") == "#hosted-disclosure"
    assert len(_by_id(root, "hosted-disclosure")) == 1


@pytest.mark.parametrize("state", [s for s in STATE_IDS if s != "reaccept"])
def test_no_paused_banner_unless_reaccept(state: str) -> None:
    _c, _html, root = _get(state, "/settings/hosted/review")
    assert _by_testid(root, "hosted-review-paused") == []
    assert _by_testid(root, "hosted-review-jump") == []


def test_connect_round_trip_reaches_the_route_and_keeps_one_disclosure(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    c = _world("no-provider")
    root = _dom(c.get("/settings/hosted/review").text)
    form = next(f for f in _forms(root) if f.route == f"{H}/connect")
    data: dict[str, str] = {}
    for name, entries in form.fields.items():
        for typ, value, checked in entries:
            if typ == "radio":
                if checked or name not in data:
                    data[name] = value or ""
            elif typ in ("hidden", "text", "password", "search", "url"):
                data[name] = value or ""
    data["provider"] = "openrouter"
    data["api_key"] = FAKE_KEY
    data["ack"] = "on"  # the person ticks the box
    models = {
        "data": [
            {
                "id": "a/cheap",
                "context_length": 32000,
                "pricing": {"prompt": "0.0000001", "completion": "0.0000002"},
                "supported_parameters": ["structured_outputs"],
            },
            {
                "id": "b/strong",
                "context_length": 128000,
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                "supported_parameters": ["structured_outputs"],
            },
        ]
    }
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{_OR}/key").mock(
            return_value=Response(200, json={"data": {"label": "k", "is_free_tier": False}})
        )
        router.get(f"{_OR}/models").mock(return_value=Response(200, json=models))
        r = c.post(form.route, data=data)
        assert router.calls  # the provider was asked, through the mock only
    assert r.status_code == 200
    body = _dom(r.text)
    assert _page_text(body).count(_disclosure_text(c)) == 1
    assert any(f.route == f"{H}/connect" for f in _forms(body))


def test_connect_without_the_tick_sends_nothing(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    c = _world("no-provider")
    with respx.mock(assert_all_called=False) as router:
        r = c.post(
            f"{H}/connect", data={"provider": "openrouter", "api_key": FAKE_KEY, "switch": "on"}
        )
        assert not router.calls
    assert r.status_code == 200
    assert not providers.secret_present("OPENROUTER_API_KEY")
    assert _page_text(_dom(r.text)).count(_disclosure_text(c)) == 1
