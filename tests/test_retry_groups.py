"""Needs another try, roles that failed the same way (D-UI-550 to D-UI-556): the typed kind the
guard stores, the groups built only from it, the threshold and the exact count, the one fix per
kind, a group action that goes through the same consent as one role, no automatic model change,
and dismiss with Undo. Cloud-safe: TestClient on the real app, fictional roles, no network."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import quote

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Quarantine, Setting
from kestrel.llm import failure_kind as fk
from kestrel.llm.client import LLMClient
from kestrel.llm.guard import run_guarded
from kestrel.llm.schemas import P1Triage
from kestrel.pipeline._shared import make_quarantine_hook
from kestrel.web import retry_groups
from kestrel.web.app import build_app
from tests import clock_seed, retry_seed
from tests.test_needs_try import _online, _read, _tasks, _write

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

HX = {"HX-Request": "true"}
_URL = "http://127.0.0.1:11434/v1/chat/completions"
_MSGS = [{"role": "user", "content": "triage this"}]


# ------------------------------------------------------------------------ the stored kind
def test_a_kind_is_read_only_from_the_very_end_of_the_stored_error():
    text = fk.tag("schema validation failed", fk.UNREADABLE)
    assert fk.split_stored(text) == (fk.UNREADABLE, "schema validation failed")
    # A reply that merely contains the mark, in the middle or with an unknown value, is no kind.
    assert fk.split_stored(f"x{fk.KIND_MARK}unreadable and then more") == (
        None, f"x{fk.KIND_MARK}unreadable and then more")
    assert fk.split_stored(f"x{fk.KIND_MARK}made_up")[0] is None
    assert fk.split_stored("schema validation failed") == (None, "schema validation failed")
    # A reason that itself quotes the mark does not hide the real one at the end.
    quoted = fk.tag(f"the reply said{fk.KIND_MARK}looping", fk.UNREADABLE)
    assert fk.split_stored(quoted)[0] == fk.UNREADABLE
    assert fk.split_stored(None) == (None, "")
    assert fk.tag("plain", None) == "plain" and fk.tag("plain", "made_up") == "plain"


def test_a_kinded_reason_is_an_ordinary_string_that_carries_its_kind():
    reason = fk.KindedReason("answer was cut off", fk.OUT_OF_ROOM)
    assert reason == "answer was cut off" and f"{reason}" == "answer was cut off"
    assert fk.kind_of(reason) == fk.OUT_OF_ROOM
    assert fk.kind_of("answer was cut off") is None
    assert fk.KindedReason("x", "made_up").kind is None


def test_a_decline_with_no_proven_cause_has_no_kind():
    plain = SimpleNamespace(degenerate=False, truncated=False, reasoning_exhausted=False)
    assert fk.declined_kind(plain) is None  # a safety refusal or a provider's HTTP 400
    assert fk.declined_kind(SimpleNamespace(degenerate=True)) == fk.REPEATED
    assert fk.declined_kind(SimpleNamespace(truncated=True)) == fk.OUT_OF_ROOM
    assert fk.declined_kind(SimpleNamespace(reasoning_exhausted=True)) == fk.OUT_OF_ROOM


def _chat(content: str, finish: str = "stop") -> Response:
    return Response(200, json={
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7},
    })


class _Session:
    """What the hook writes to: it only needs `add`."""

    def __init__(self) -> None:
        self.rows: list[Quarantine] = []

    def add(self, row: Quarantine) -> None:
        self.rows.append(row)


async def _stored_error(side_effect) -> tuple[str, object]:
    session = _Session()
    hook = make_quarantine_hook(session, tier=1, opportunity_id=1)
    with respx.mock:
        respx.post(_URL).mock(side_effect=side_effect)
        res = await run_guarded(
            LLMClient(get_settings()), tier="tier1", messages=_MSGS, model_cls=P1Triage,
            on_quarantine=hook,
        )
    assert res.quarantined and len(session.rows) == 1
    return session.rows[0].error, res


async def test_an_answer_that_never_fit_the_schema_is_stored_as_unreadable():
    error, res = await _stored_error([_chat("{bad"), _chat("still bad}")])
    kind, text = fk.split_stored(error)
    assert kind == fk.UNREADABLE and text == res.error
    assert not isinstance(res.error, fk.KindedReason) or res.error.kind is not None


async def test_a_reply_cut_at_its_cap_is_stored_as_out_of_room():
    error, _ = await _stored_error([_chat("{bad"), _chat('{"verdict": "ke', finish="length")])
    assert fk.split_stored(error)[0] == fk.OUT_OF_ROOM


async def test_a_repair_that_loops_is_stored_as_looping():
    loop = '{"signals": [' + ", ".join(['"no_comp_no_location_no_team"'] * 200) + "]}"
    error, _ = await _stored_error([_chat("{bad"), _chat(loop, finish="length")])
    assert fk.split_stored(error)[0] == fk.REPEATED


async def test_a_failure_whose_text_sounds_like_a_kind_but_has_none_stores_none():
    """The kind is what the guard decided, never what the wording suggests (section 8a.2): a
    plain string reason, even one that says schema validation, is stored without a kind."""
    session = _Session()
    hook = make_quarantine_hook(session, tier=2, opportunity_id=1)
    await hook(_MSGS, "raw", "schema validation failed: verdict required", "kestrel-tier2")
    assert session.rows[0].error == "schema validation failed: verdict required"
    assert fk.split_stored(session.rows[0].error)[0] is None


async def test_the_kind_survives_a_reason_longer_than_the_cut():
    session = _Session()
    hook = make_quarantine_hook(session, tier=2, opportunity_id=1)
    await hook(_MSGS, "raw", fk.KindedReason("x" * 6000, fk.UNREADABLE), "kestrel-tier2")
    stored = session.rows[0].error
    assert len(stored) < 4000 and fk.split_stored(stored)[0] == fk.UNREADABLE


# ------------------------------------------------------------------------------- grouping
def _view(n: int, *, kind=fk.UNREADABLE, step="fit", model="kestrel-tier2", age_h=1,
          queued=False, hidden=False, opp=True, needs_you=True, stage="quarantined"):
    created = clock_seed.ago_within(
        timedelta(hours=age_h), clock_seed.local_day_start(datetime.now(UTC)),
        horizon=timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": n, "kind": kind, "step_key": step, "step_label": step.title(),
        # The stored row a real view carries: its tier is the step, and it names its role.
        "q": SimpleNamespace(created_at=created, model=model, opportunity_id=n,
                             tier={"quick": 1, "fit": 2, "deep": 3}.get(step, 5)),
        "opp": SimpleNamespace(id=n, stage=stage) if opp else None, "queued": queued,
        "hidden": hidden,
        "needs_you": needs_you,
    }


def test_three_roles_failing_the_same_way_form_one_group_with_the_exact_count():
    groups = retry_groups.build_groups([_view(i) for i in range(1, 4)])
    assert len(groups) == 1 and groups[0]["count"] == 3
    assert [m["id"] for m in groups[0]["members"]] == [1, 2, 3]  # every role is in it


def test_below_the_threshold_there_is_no_group():
    assert retry_groups.THRESHOLD == 3
    assert retry_groups.build_groups([_view(1), _view(2)]) == []


def test_a_group_splits_by_step_and_by_model_and_by_kind():
    views = [_view(1), _view(2), _view(3),
             _view(4, step="quick"), _view(5, step="quick"),  # same kind, another step
             _view(6, model="other"), _view(7, model="other"),  # another model
             _view(8, kind=fk.OUT_OF_ROOM), _view(9, kind=fk.OUT_OF_ROOM)]
    groups = retry_groups.build_groups(views)
    assert [g["count"] for g in groups] == [3]  # no two small groups are merged to reach three
    big = [_view(i) for i in range(1, 4)] + [_view(i, model="other") for i in range(4, 7)]
    assert sorted(g["count"] for g in retry_groups.build_groups(big)) == [3, 3]


def test_a_failure_with_no_kind_stays_on_its_own_however_it_reads():
    views = [_view(i, kind=None) for i in range(1, 6)]
    assert retry_groups.build_groups(views) == []
    # An unknown kind is not a kind either.
    assert retry_groups.build_groups([_view(i, kind="made_up") for i in range(1, 6)]) == []


def test_roles_already_waiting_hidden_without_a_role_or_old_are_not_counted():
    views = [_view(1), _view(2), _view(3, queued=True), _view(4, hidden=True),
             _view(5, opp=False), _view(6, age_h=30)]
    assert retry_groups.build_groups(views) == []  # exactly two qualify
    assert retry_groups.build_groups(views + [_view(7)])[0]["count"] == 3


def test_every_kind_the_guard_can_store_has_one_fix_and_it_is_a_choice():
    assert set(retry_groups.CAUSES) == set(fk.KINDS)
    labels = {kind: retry_groups.CAUSES[kind][1] for kind in fk.KINDS}
    assert labels[fk.UNREADABLE] == "Try these with a more careful model"
    assert labels[fk.OUT_OF_ROOM] == "Try these with a model that has more room"
    assert labels[fk.REPEATED] == "Try these with a different model"
    assert all("—" not in w for c in retry_groups.CAUSES.values() for w in c)


def test_a_dismissal_holds_until_the_group_grows():
    groups = retry_groups.build_groups([_view(i) for i in range(1, 4)])
    key = groups[0]["key"]
    assert retry_groups.visible(groups, {}) == groups
    assert retry_groups.visible(groups, {key: 3}) == []
    assert retry_groups.visible(groups, {key: 2}) == groups  # grew since


# ------------------------------------------------------------------------------- the page
def _rows(n: int, *, kind: str | None = fk.UNREADABLE, tier: int = 2, prefix: str = "Role"):
    error = fk.tag("schema validation failed: verdict required", kind)
    return [(f"{prefix} {i}", "Larkspur Logistics", tier, error, '{"fit": "x"', 2,
             timedelta(minutes=10 + i)) for i in range(n)]


@pytest.fixture
def make(monkeypatch):
    """Seed rows, then a client on the real app. Local models are patched; no network."""
    monkeypatch.setattr("kestrel.web.pages.needs_try._local_models",
                        lambda settings: (["kestrel-tier3:latest", "kestrel-tier2:latest"], None))
    monkeypatch.setattr("kestrel.web.pages.rescore._local_models",
                        lambda settings: (["kestrel-tier3:latest", "kestrel-tier2:latest"], None))

    async def no_hosted(_settings):
        return ("", None, [], [], {}, None)

    monkeypatch.setattr("kestrel.web.pages.needs_try._hosted_models", no_hosted)
    monkeypatch.setattr("kestrel.web.pages.rescore._hosted_models", no_hosted)
    clients: list[TestClient] = []

    def go(rows):
        ids = retry_seed.seed(rows)
        client = TestClient(build_app(worker=False))
        client.__enter__()
        clients.append(client)
        return client, ids

    yield go
    for c in clients:
        c.__exit__(None, None, None)


def _token(client, key: str) -> str:
    dialog = client.get(f"/quarantine/group/{quote(key)}/online").text
    return re.search(r'name="group_ids" value="([0-9,]+)"', dialog).group(1)


def _banner_ids(client) -> str:
    """The role ids the banner names, as its buttons carry them."""
    return re.search(r'"group_ids": "([0-9,]+)"', client.get("/quarantine").text).group(1)


def _settings_rows() -> list[tuple[str, str]]:
    async def f(s):
        rows = (await s.execute(select(Setting.key, Setting.value))).all()
        return sorted((k, json.dumps(v, sort_keys=True)) for k, v in rows)

    return _read(f)


def _group_key(client) -> str:
    html = client.get("/quarantine").text
    marker = 'data-group="'
    start = html.index(marker) + len(marker)
    return html[start:html.index('"', start)]


def test_the_banner_names_the_count_step_model_and_cause_once(make):
    client, _ = make(_rows(4))
    html = client.get("/quarantine").text
    assert html.count('data-testid="rt-group"') == 1
    assert "4 roles failed the same way in Fit score" in html
    assert "The AI’s answer couldn’t be read. All 4 used Full review model." in html
    assert "Try these with a more careful model" in html and "Show these 4" in html
    assert html.count('data-testid="rt-row"') == 4  # grouping hides no role


def test_below_the_threshold_the_page_has_no_banner(make):
    client, _ = make(_rows(2))
    html = client.get("/quarantine").text
    assert 'data-testid="rt-group"' not in html and html.count('data-testid="rt-row"') == 2


def test_failures_with_no_kind_never_show_a_banner_even_when_they_read_alike(make):
    client, _ = make(_rows(6, kind=None))
    html = client.get("/quarantine").text
    assert 'data-testid="rt-group"' not in html and html.count('data-testid="rt-row"') == 6


def test_show_these_lists_exactly_the_group_and_show_all_brings_every_role_back(make):
    rows = _rows(4) + _rows(2, kind=None, prefix="Other")
    client, ids = make(rows)
    key = _group_key(client)
    narrowed = client.get(f"/quarantine?group={quote(key)}", headers=HX).text
    shown = {int(x) for x in re.findall(r'id="rt-row-(\d+)"', narrowed)}
    assert shown == {ids[f"Role {i}"]["q"] for i in range(4)}
    assert "Showing only these roles." in narrowed and 'data-testid="rt-group-all"' in narrowed
    assert "Show these 4" not in narrowed and 'data-testid="rt-group-picked"' not in narrowed
    everyone = client.get("/quarantine", headers=HX).text
    assert everyone.count('data-testid="rt-row"') == 6
    stale = client.get("/quarantine?group=fit.unreadable.0000000000", headers=HX).text
    assert stale.count('data-testid="rt-row"') == 6  # an unknown key narrows nothing


def test_a_role_that_failed_twice_counts_once_in_the_banner_and_the_rows(make):
    client, ids = make(_rows(3))

    async def older(s):
        s.add(Quarantine(
            opportunity_id=ids["Role 0"]["opp"], tier=2, model="kestrel-tier2", prompt="[user]",
            raw_response="x", error=fk.tag("older failure", fk.UNREADABLE), attempts=2,
            created_at=clock_seed.ago(hours=5, horizon=timedelta(days=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
        ))

    _write(older)
    html = client.get("/quarantine").text
    assert "3 roles failed the same way" in html  # one row per role, so one count per role
    assert html.count('data-testid="rt-row"') == 3


def test_a_group_menu_offers_this_computer_then_online_and_changes_nothing(make):
    client, _ = make(_rows(3))
    before = _settings_rows()
    key = _group_key(client)
    menu = client.get(f"/quarantine/group/{quote(key)}/models").text
    assert f'hx-post="/quarantine/group/{key}/model"' in menu
    assert 'hx-target="#rt-card"' in menu and "On this computer, free" in menu
    assert not _tasks() and _settings_rows() == before  # opening a menu queues and saves nothing


def test_a_local_pick_queues_exactly_the_groups_roles_with_that_model(make):
    client, ids = make(_rows(3) + _rows(2, kind=None, prefix="Other"))
    key = _group_key(client)
    r = client.post(f"/quarantine/group/{quote(key)}/model",
                    data={"model": "kestrel-tier3:latest", "group_ids": _banner_ids(client)},
                    headers=HX)
    assert r.status_code == 200 and "Trying 3 roles again" in r.headers["HX-Trigger"]
    tasks = _tasks()
    assert len(tasks) == 3
    assert {p["opportunity_id"] for _, p in tasks} == {ids[f"Role {i}"]["opp"] for i in range(3)}
    assert all(p["model_override"]["model"] == "kestrel-tier3:latest" for _, p in tasks)
    attempts = {q.opportunity_id: q.attempts for q in _read(
        lambda s: _all(s, Quarantine))}
    assert all(attempts[ids[f"Role {i}"]["opp"]] == 3 for i in range(3))  # each try counted
    assert all(attempts[ids[f"Other {i}"]["opp"]] == 2 for i in range(2))  # outsiders untouched


async def _all(s, model):
    return list((await s.execute(select(model))).scalars())


def test_a_group_action_never_changes_a_setting(make):
    client, _ = make(_rows(3))
    before = {k for k, _ in _settings_rows()}
    key = _group_key(client)
    client.post(f"/quarantine/group/{quote(key)}/model",
                data={"model": "kestrel-tier3:latest", "group_ids": _banner_ids(client)},
                headers=HX)
    after = {k for k, _ in _settings_rows()}
    assert not any(k.startswith(("llm.", "hosted")) for k in after - before)
    assert before <= after


def test_a_pick_with_no_model_or_the_same_model_queues_nothing(make):
    client, _ = make(_rows(3))
    key = _group_key(client)
    for pick in ("", "same", "not-a-model"):
        client.post(f"/quarantine/group/{quote(key)}/model", data={"model": pick}, headers=HX)
    assert not _tasks()


def test_a_group_that_shrank_since_the_page_loaded_queues_nothing(make):
    client, ids = make(_rows(3))
    key = _group_key(client)

    async def drop(s):
        await s.delete(await s.get(Quarantine, ids["Role 0"]["q"]))

    _write(drop)
    r = client.post(f"/quarantine/group/{quote(key)}/model",
                    data={"model": "kestrel-tier3:latest"}, headers=HX)
    assert "Nothing was queued" in r.headers["HX-Trigger"] and not _tasks()


def test_the_group_ignores_ids_the_client_sends(make):
    client, ids = make(_rows(3) + _rows(1, kind=None, prefix="Other"))
    key = _group_key(client)
    client.post(f"/quarantine/group/{quote(key)}/model",
                data={"model": "kestrel-tier3:latest", "group_ids": _banner_ids(client) + ","
                      + str(ids["Other 0"]["opp"])}, headers=HX)
    queued = {p["opportunity_id"] for _, p in _tasks()}
    assert ids["Other 0"]["opp"] not in queued and len(queued) == 3


# ----------------------------------------------------------------------- online and consent
def test_the_online_dialog_for_a_group_says_the_count_and_what_is_sent(make, monkeypatch):
    client, _ = make(_rows(3))
    _online(monkeypatch)
    key = _group_key(client)
    body = client.get(f"/quarantine/group/{quote(key)}/online").text
    assert "Try 3 roles with an online model?" in body
    assert "What will be sent" in body and "The postings for these 3 roles" in body
    assert "Not sent:" in body and "about 3 times that here" in body
    assert f'hx-post="/quarantine/group/{key}/model"' in body and "data-k-static" in body
    assert not _tasks()  # a dialog sends nothing


def test_an_online_pick_for_a_group_waits_for_the_dialogs_confirmation(make, monkeypatch):
    client, _ = make(_rows(3))
    _online(monkeypatch)
    key = _group_key(client)
    url = f"/quarantine/group/{quote(key)}/model"
    r = client.post(url, data={"model": "hosted:vendor/model-b:free"}, headers=HX)
    assert "Confirm in the dialog first" in r.headers["HX-Trigger"] and not _tasks()
    r = client.post(url, data={"model": "hosted:vendor/model-zzz", "confirmed": "1"}, headers=HX)
    assert "not on the online list" in r.headers["HX-Trigger"] and not _tasks()
    token = _token(client, key)
    r = client.post(url, data={"model": "hosted:vendor/model-b:free", "confirmed": "1",
                               "group_ids": token}, headers=HX)
    tasks = _tasks()
    assert len(tasks) == 3
    assert all(p["model_override"]["backend"] == "openrouter" for _, p in tasks)


def test_an_online_pick_for_a_group_is_refused_when_hosted_mode_is_off(make, monkeypatch):
    client, _ = make(_rows(3))
    _online(monkeypatch, hosted_on=False)
    key = _group_key(client)
    r = client.post(f"/quarantine/group/{quote(key)}/model",
                    data={"model": "hosted:vendor/model-b:free", "confirmed": "1"}, headers=HX)
    assert "Hosted mode, which is off" in r.headers["HX-Trigger"] and not _tasks()


def test_a_group_goes_through_the_same_approval_check_as_one_role(make, monkeypatch):
    """The group action and the role action share `_enqueue`; when the approval check says no,
    nothing is queued for the group, as for a role."""
    client, ids = make(_rows(3))
    _online(monkeypatch)
    from kestrel.services import online_consent

    async def refuse(*_a, **_k):
        return "not approved"

    monkeypatch.setattr(online_consent, "approval_problem", refuse)
    key = _group_key(client)
    data = {"model": "hosted:vendor/model-b:free", "confirmed": "1",
            "group_ids": _token(client, key)}
    r = client.post(f"/quarantine/group/{quote(key)}/model", data=data, headers=HX)
    assert "Confirm the online send first" in r.headers["HX-Trigger"] and not _tasks()
    qid = ids["Role 0"]["q"]
    r = client.post(f"/quarantine/{qid}/model", data=data,
                    headers={**HX, "HX-Target": f"rt-row-{qid}"})
    assert "Confirm the online send first" in r.text and not _tasks()


# --------------------------------------------------------------------------- dismiss and undo
def test_dismiss_hides_the_banner_not_the_roles_and_undo_brings_it_back(make):
    client, _ = make(_rows(3))
    key = _group_key(client)
    r = client.post(f"/quarantine/group/{quote(key)}/dismiss", headers=HX)
    assert 'data-testid="rt-group"' not in r.text and r.text.count('data-testid="rt-row"') == 3
    toast = json.loads(r.headers["HX-Trigger"])["k-toast"]
    assert toast["message"] == "Hidden until more roles fail this way." and toast["undoUrl"]
    assert 'data-testid="rt-group"' not in client.get("/quarantine").text
    assert not _tasks()  # hiding a notice never queues or changes anything
    undo = client.post(toast["undoUrl"])
    assert undo.status_code == 204
    assert 'data-testid="rt-group"' in client.get("/quarantine").text


def test_a_hidden_banner_returns_when_the_group_grows(make):
    client, _ = make(_rows(4))
    key = _group_key(client)
    # Hidden when the group had three roles: the fourth makes it bigger, so the banner is back.
    _write(lambda s: _put(s, retry_groups.dismiss_key(key), {"count": 3, "at": "a"}))
    assert 'data-testid="rt-group"' in client.get("/quarantine").text
    _write(lambda s: _put(s, retry_groups.dismiss_key(key), {"count": 4, "at": "b"}))
    assert 'data-testid="rt-group"' not in client.get("/quarantine").text


async def _put(s, key, value):
    row = await s.get(Setting, key)
    if row is None:
        s.add(Setting(key=key, value=value))
    else:
        row.value = value


def test_undo_with_a_stale_stamp_changes_nothing(make):
    client, _ = make(_rows(3))
    key = _group_key(client)
    client.post(f"/quarantine/group/{quote(key)}/dismiss", headers=HX)
    r = client.post(f"/quarantine/group/{quote(key)}/undismiss?at=nope")
    assert 'data-ok="0"' in r.text
    assert 'data-testid="rt-group"' not in client.get("/quarantine").text


def test_hide_is_kept_as_state_not_configuration(make):
    from kestrel.services.setting_overrides import NON_CONFIG_PREFIXES

    assert retry_groups.DISMISS_PREFIX in NON_CONFIG_PREFIXES


# ------------------------------------------------------------------------------------ Today
def test_today_says_it_once_with_the_same_count_as_the_page(make):
    client, _ = make(_rows(4))
    from kestrel.analytics.today import system_asks

    async def f(s):
        return await system_asks(s, get_settings(), None)

    asks = [a for a in _read(f) if a["key"] == "quarantine"]
    assert len(asks) == 1
    assert "4 of them failed Fit score the same way: the AI’s answer couldn’t be read." in (
        asks[0]["line"])
    key = _group_key(client)
    client.post(f"/quarantine/group/{quote(key)}/dismiss", headers=HX)
    hidden = [a for a in _read(f) if a["key"] == "quarantine"]
    assert "the same way" not in hidden[0]["line"]  # a banner the person hid is not repeated


def test_a_picked_group_whose_banner_is_hidden_still_has_a_way_back(make):
    client, _ = make(_rows(3))
    key = _group_key(client)
    client.post(f"/quarantine/group/{quote(key)}/dismiss", headers=HX)
    narrowed = client.get(f"/quarantine?group={quote(key)}", headers=HX).text
    assert 'data-testid="rt-group-picked"' in narrowed and 'data-testid="rt-group-all"' in narrowed
    assert narrowed.count('data-testid="rt-row"') == 3


# ----------------------------------------------------------------- review round (issue 206)
def test_a_role_that_does_not_need_you_is_not_counted():
    views = [_view(1), _view(2), _view(3, needs_you=False)]
    assert retry_groups.build_groups(views) == []
    assert retry_groups.build_groups(views + [_view(4)])[0]["count"] == 3


def _hide_company(name: str) -> None:
    from kestrel.db.models import Org

    async def f(s):
        org = (await s.execute(select(Org).where(Org.name == name))).scalars().one()
        org.hidden_at = "2026-11-01T00:00:00Z"

    _write(f)


def test_roles_of_a_hidden_company_are_in_no_group_and_not_on_today(make):
    """Two visible roles and three from a hidden company: no banner (two is below the threshold)
    and Today's total and its group line agree."""
    rows = _rows(2, prefix="Seen") + [
        (f"Hidden {i}", "Brackenridge Mills", 2, fk.tag("schema", fk.UNREADABLE), "x", 2,
         timedelta(minutes=5 + i)) for i in range(3)]
    client, _ = make(rows)
    _hide_company("Brackenridge Mills")
    assert 'data-testid="rt-group"' not in client.get("/quarantine").text
    from kestrel.analytics.today import system_asks

    async def f(s):
        return await system_asks(s, get_settings(), None)

    ask = [a for a in _read(f) if a["key"] == "quarantine"][0]
    assert "2 postings need another try" in ask["title"] and "the same way" not in ask["line"]


def test_the_online_dialog_for_a_group_carries_the_ids_it_shows(make, monkeypatch):
    client, ids = make(_rows(3))
    _online(monkeypatch)
    key = _group_key(client)
    body = client.get(f"/quarantine/group/{quote(key)}/online").text
    carried = {int(i) for i in _token(client, key).split(",")}
    assert 'name="group_ids"' in body and "#rt-online-group" in body
    assert carried == {ids[f"Role {i}"]["opp"] for i in range(3)}


def test_a_group_that_grew_after_the_dialog_sends_only_the_roles_it_showed(make, monkeypatch):
    """The dialog said 3 roles; two more then failed the same way. Send queues those 3, never 5."""
    client, ids = make(_rows(3))
    _online(monkeypatch)
    key = _group_key(client)
    shown = _token(client, key)

    async def add(s):
        from kestrel.db.models import Opportunity

        for i in range(2):
            opp = Opportunity(track="employment", opp_type="job_posting", title=f"Late {i}",
                              org_name="Larkspur Logistics", url=f"https://example.test/late{i}",
                              stage="quarantined", pre_score=0.5, description_md="x")
            s.add(opp)
            await s.flush()
            s.add(Quarantine(opportunity_id=opp.id, tier=2, model="kestrel-tier2", prompt="[u]",
                             raw_response="x", error=fk.tag("again", fk.UNREADABLE), attempts=2))

    _write(add)
    url = f"/quarantine/group/{quote(key)}/model"
    data = {"model": "hosted:vendor/model-b:free", "confirmed": "1"}
    client.post(url, data={**data, "group_ids": shown}, headers=HX)
    queued = {p["opportunity_id"] for _, p in _tasks()}
    assert queued == {int(i) for i in shown.split(",")} and len(queued) == 3
    assert not queued & {ids.get(f"Late {i}", {}).get("opp") for i in range(2)}


def test_an_online_group_send_with_no_ids_or_unrelated_ids_sends_nothing(make, monkeypatch):
    client, _ = make(_rows(3))
    _online(monkeypatch)
    key = _group_key(client)
    url = f"/quarantine/group/{quote(key)}/model"
    data = {"model": "hosted:vendor/model-b:free", "confirmed": "1"}
    for ids in ("", "999999", "x,y"):
        r = client.post(url, data={**data, "group_ids": ids}, headers=HX)
        assert "changed after the" in r.headers["HX-Trigger"]
    assert not _tasks()


def test_the_stored_kind_line_never_reaches_a_text_matcher(monkeypatch):
    """No current needle matches a kind word, so make one that would: if the line reached the
    matcher, the tagged error would read as the planted words and the plain one would not."""
    from kestrel.analytics import today
    from kestrel.web import fit

    words = tuple(sorted(fk.KINDS))
    monkeypatch.setattr(fit, "_FAILURE_WORDS", ((words, "PLANTED"),) + tuple(fit._FAILURE_WORDS))
    monkeypatch.setattr(today, "_LENGTH_MARKS", words + tuple(today._LENGTH_MARKS))
    plain = "an unrelated failure"
    for kind in fk.KINDS:
        tagged = fk.tag(plain, kind)
        assert fit.failure_words(tagged) == fit.failure_words(plain) != "PLANTED"
        assert today.quarantine_kind(tagged) == today.quarantine_kind(plain) == "other"


def test_the_runs_data_layer_returns_the_error_without_the_kind_line(make):
    from kestrel.web.deps import sessionmaker
    from kestrel.web.pages.runs_detail import _quarantine_rows

    client, _ = make(_rows(1))

    async def f():
        async with sessionmaker()() as s:
            return await _quarantine_rows(s)

    rows = asyncio.run(f())
    assert rows and fk.KIND_MARK not in rows[0]["error"] and rows[0]["q"].error.endswith(
        fk.UNREADABLE)
    del client


@respx.mock
async def test_a_request_timeout_writes_no_quarantine_row():
    """Why a timeout has no group (D-UI-553): the guard returns a request error to the caller and
    never calls the quarantine hook, so no row, and no kind, exists for it."""
    import httpx

    session = _Session()
    hook = make_quarantine_hook(session, tier=1, opportunity_id=1)
    respx.post(_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    res = await run_guarded(
        LLMClient(get_settings()), tier="tier1", messages=_MSGS, model_cls=P1Triage,
        on_quarantine=hook,
    )
    assert not res.ok and not res.quarantined and session.rows == []


@respx.mock
async def test_a_refused_key_writes_no_quarantine_row():
    session = _Session()
    hook = make_quarantine_hook(session, tier=1, opportunity_id=1)
    respx.post(_URL).mock(return_value=Response(401, json={"error": "invalid api key"}))
    res = await run_guarded(
        LLMClient(get_settings()), tier="tier1", messages=_MSGS, model_cls=P1Triage,
        on_quarantine=hook,
    )
    assert not res.ok and not res.quarantined and session.rows == []


def test_the_group_cost_is_estimated_from_the_longest_posting(make, monkeypatch):
    """A short first posting must not set the price of a group of long ones."""
    from kestrel.llm.providers import ModelInfo
    from kestrel.web.pages.needs_try import _estimate_cost

    client, ids = make(_rows(3))
    _online(monkeypatch)

    async def shorten(s):
        from kestrel.db.models import Opportunity

        for i in range(3):
            opp = await s.get(Opportunity, ids[f"Role {i}"]["opp"])
            # The newest failure is listed first, so Role 2 leads the group: make it the short one.
            opp.description_md = "A short posting." if i == 2 else "word " * 8000

    _write(shorten)
    model_a = ModelInfo(id="vendor/model-a", name="Model A", context_length=131072,
                        prompt_usd_per_m=2.0, completion_usd_per_m=10.0)
    key = _group_key(client)
    body = client.get(f"/quarantine/group/{quote(key)}/online").text
    longest = _estimate_cost(model_a, "word " * 8000)
    assert f"Model A, {longest}" in body
    assert _estimate_cost(model_a, "A short posting.") != longest  # the two really differ
    assert "the longest of these postings" in body and "no more than about 3 times" in body


# ------------------------------------------------------------- review round 9 (issue 206)
def _seed_step(tier: int, n: int = 3, prefix: str = "Step"):
    return [(f"{prefix} {i}", "Larkspur Logistics", tier, fk.tag("schema", fk.UNREADABLE), "x", 2,
             timedelta(minutes=10 + i)) for i in range(n)]


@pytest.mark.parametrize("tier", [1, 3, 4, 5, 9, 13, 14, 15, 16])
def test_only_quick_sort_and_fit_score_can_form_a_group(make, tier):
    """Deep read and every other step: three roles failing alike show no banner, because the fix
    would re-score roles behind a claim that the failed step was fixed."""
    client, _ = make(_seed_step(tier))
    html = client.get("/quarantine").text
    assert 'data-testid="rt-group"' not in html and html.count('data-testid="rt-row"') == 3


def test_quick_sort_failures_form_no_group_because_the_fix_would_run_fit_score(make):
    """A role whose Quick sort failed was kept and moved on; a score again would run Fit score and
    spend a retry on the Quick sort row, so no banner claims to fix Quick sort."""
    client, _ = make(_seed_step(1))
    html = client.get("/quarantine").text
    assert 'data-testid="rt-group"' not in html and html.count('data-testid="rt-row"') == 3


def test_two_other_steps_are_never_lumped_into_one_group(make):
    rows = _seed_step(14, 2, "Interview") + _seed_step(13, 2, "Resume")
    client, _ = make(rows)
    html = client.get("/quarantine").text
    assert 'data-testid="rt-group"' not in html and html.count('data-testid="rt-row"') == 4


def test_a_local_pick_acts_only_on_the_roles_the_banner_named(make):
    """Three roles on the banner; two more fail after the page loaded. A free local pick queues
    the three, not five, and the toast says three."""
    client, ids = make(_rows(3))
    key = _group_key(client)
    shown = _banner_ids(client)

    async def add(s):
        from kestrel.db.models import Opportunity

        for i in range(2):
            opp = Opportunity(track="employment", opp_type="job_posting", title=f"Late {i}",
                              org_name="Larkspur Logistics", url=f"https://example.test/l{i}",
                              stage="quarantined", pre_score=0.5, description_md="x")
            s.add(opp)
            await s.flush()
            s.add(Quarantine(opportunity_id=opp.id, tier=2, model="kestrel-tier2", prompt="[u]",
                             raw_response="x", error=fk.tag("again", fk.UNREADABLE), attempts=2))

    _write(add)
    r = client.post(f"/quarantine/group/{quote(key)}/model",
                    data={"model": "kestrel-tier3:latest", "group_ids": shown}, headers=HX)
    queued = {p["opportunity_id"] for _, p in _tasks()}
    assert queued == {int(i) for i in shown.split(",")} and len(queued) == 3
    assert "Trying 3 roles again" in r.headers["HX-Trigger"]


def test_a_local_pick_with_no_ids_queues_nothing(make):
    client, _ = make(_rows(3))
    key = _group_key(client)
    for ids in ("", "999999", "x"):
        r = client.post(f"/quarantine/group/{quote(key)}/model",
                        data={"model": "kestrel-tier3:latest", "group_ids": ids}, headers=HX)
        # It says why; it never reports "Trying 0 roles".
        assert "changed after the page loaded" in r.headers["HX-Trigger"]
    assert not _tasks()


def test_the_menu_and_dialog_carry_the_roles_the_banner_named(make, monkeypatch):
    client, _ = make(_rows(3))
    _online(monkeypatch)
    key = _group_key(client)
    shown = _banner_ids(client)
    menu = client.get(f"/quarantine/group/{quote(key)}/models", params={"group_ids": shown}).text
    assert menu.count(f'"group_ids": "{shown}"') >= 2  # the local items and the online button
    one = shown.split(",")[0]
    dialog = client.get(f"/quarantine/group/{quote(key)}/online", params={"group_ids": one}).text
    assert f'name="group_ids" value="{one}"' in dialog  # only the role it was asked about


def test_only_a_step_its_own_row_can_run_again_forms_a_group(make):
    """The banner's step and what its fix runs are one thing: the row's own task
    (`row_retry.ROW_TASK`). Drive each scoring step and compare the task types actually queued."""
    from kestrel.queue import row_retry

    # Fit score is the only step a row runs again; Quick sort, Deep read and the rest form no
    # group (test_only_quick_sort_and_fit_score_can_form_a_group drives them).
    assert set(row_retry.ROW_TASK) == {2}
    for tier in row_retry.ROW_TASK:
        client, _ = make(_seed_step(tier))
        key = _group_key(client)
        client.post(f"/quarantine/group/{quote(key)}/model",
                    data={"model": "kestrel-tier3:latest", "group_ids": _banner_ids(client)},
                    headers=HX)
        types = {t for t, _ in _tasks()}
        assert types == {row_retry.ROW_TASK[tier]}, (tier, types)
        attempts = {q.attempts for q in _read(lambda s: _all(s, Quarantine))}
        assert attempts == {3}  # a retry is spent only on the step that actually ran


def test_a_failed_score_again_is_not_counted_in_a_group(make):
    """It keeps its earlier score and Today counts it apart, so a group's count never exceeds the
    scoring failures Today names."""
    from kestrel.pipeline._shared import rescore_failure_prefix

    rows = _rows(3)
    name, org, tier, _err, raw, attempts, age = rows[0]
    rows[0] = (name, org, tier, rescore_failure_prefix(7) + fk.tag("schema", fk.UNREADABLE),
               raw, attempts, age)
    client, _ = make(rows)
    assert 'data-testid="rt-group"' not in client.get("/quarantine").text  # two are not three


def test_hiding_a_banner_removes_the_markers_of_groups_that_are_gone(make):
    client, _ = make(_rows(3))
    _write(lambda s: _put(s, retry_groups.dismiss_key("fit.unreadable.deadbeef00"),
                          {"count": 3, "at": "x"}))
    key = _group_key(client)
    client.post(f"/quarantine/group/{quote(key)}/dismiss", headers=HX)
    keys = [k for k, _ in _settings_rows() if k.startswith(retry_groups.DISMISS_PREFIX)]
    assert keys == [retry_groups.dismiss_key(key)]  # the stale one went, the live one stays


@pytest.mark.parametrize("stage", ["t1_reject", "t0_pass", "t0_reject", "new"])
def test_a_fit_failure_whose_role_moved_to_a_lower_stage_is_not_in_a_group(make, stage):
    """The queue picks the task from the role's stage, not the failed row. A Fit score row whose
    role is now below Fit score would be re-run as Quick sort behind a Fit score banner."""
    client, ids = make(_rows(4))

    async def move(s):
        from kestrel.db.models import Opportunity

        for i in range(2):
            (await s.get(Opportunity, ids[f"Role {i}"]["opp"])).stage = stage

    _write(move)
    html = client.get("/quarantine").text
    assert "4 roles failed" not in html and "2 roles failed" not in html
    assert 'data-testid="rt-group"' not in html  # two left, below the threshold
    assert html.count('data-testid="rt-row"') == 4  # every role is still listed


def test_a_group_counts_a_retry_only_where_the_claimed_task_ran(make):
    from kestrel.queue import row_retry

    client, ids = make(_rows(3))
    key = _group_key(client)
    client.post(f"/quarantine/group/{quote(key)}/model",
                data={"model": "kestrel-tier3:latest", "group_ids": _banner_ids(client)},
                headers=HX)
    assert {t for t, _ in _tasks()} == {row_retry.ROW_TASK[2]}
    attempts = {q.opportunity_id: q.attempts for q in _read(lambda s: _all(s, Quarantine))}
    assert set(attempts.values()) == {3}


def test_the_group_queues_the_type_the_row_map_names_not_the_one_the_stage_routes_to(
    make, monkeypatch,
):
    """Point the row map somewhere the role's stage would never route (a Fit score row's stage
    routes to t2): the queued task follows the map, so what runs is read from the row alone."""
    from kestrel.queue import row_retry

    client, _ = make(_rows(3))
    key = _group_key(client)
    monkeypatch.setitem(row_retry.ROW_TASK, 2, "t1")
    monkeypatch.setitem(row_retry.RUNS_FROM, "t1", row_retry.RUNS_FROM["t2"])
    client.post(f"/quarantine/group/{quote(key)}/model",
                data={"model": "kestrel-tier3:latest", "group_ids": _banner_ids(client)},
                headers=HX)
    assert {t for t, _ in _tasks()} == {"t1"}
    attempts = {q.attempts for q in _read(lambda s: _all(s, Quarantine))}
    assert attempts == {3}
