"""Needs another try (D-UI-104): the page, every row action, Remove and Undo, the bulk try, the
ask-first online dialog and every state of the template. Cloud-safe: TestClient on the real app,
a throwaway data directory, fictional roles (tests/retry_seed.py), no model and no network (the
model lists are patched). The live-browser parity check is tests/test_needs_try_browser.py."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db.base import iso
from kestrel.db.engine import make_async_engine
from kestrel.db.models import Evaluation, Opportunity, Quarantine, Setting, Task
from kestrel.pipeline._shared import rescore_failure_prefix
from kestrel.services.hosted_models import PickerModel
from kestrel.web.app import build_app
from kestrel.web.deps import templates
from kestrel.web.queries import failure_summary
from kestrel.web.retry_words import plain_reason, refused_key, step_for, tries_words, when_words
from tests import retry_seed

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

HX = {"HX-Request": "true"}


def _hx(qid: int) -> dict[str, str]:
    return {**HX, "HX-Target": f"rt-row-{qid}"}


def _read(fn):
    async def go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _write(fn):
    async def inner(s):
        out = await fn(s)
        await s.commit()
        return out

    return _read(inner)


def _events(resp) -> dict:
    return json.loads(resp.headers["HX-Trigger"]) if "HX-Trigger" in resp.headers else {}


def _tasks() -> list[tuple[str, dict]]:
    async def f(s):
        return [(t.type, dict(t.payload or {})) for t in (await s.execute(select(Task))).scalars()]

    return _read(f)


def _row_html(page: str, qid: int) -> str:
    start = page.index(f'id="rt-row-{qid}"')
    nxt = page.find('id="rt-row-', start + 10)
    return page[start:nxt if nxt != -1 else len(page)]


@pytest.fixture
def seeded(monkeypatch):
    """The seven mockup roles, and a client on the real app."""
    monkeypatch.setattr("kestrel.web.pages.needs_try._local_models",
                        lambda settings: (["kestrel-tier3:latest", "kestrel-tier2:latest"], None))
    monkeypatch.setattr("kestrel.web.pages.rescore._local_models",
                        lambda settings: (["kestrel-tier3:latest", "kestrel-tier2:latest"], None))

    async def no_hosted(_settings):
        return ("", None, [], [], {}, None)

    monkeypatch.setattr("kestrel.web.pages.needs_try._hosted_models", no_hosted)
    monkeypatch.setattr("kestrel.web.pages.rescore._hosted_models", no_hosted)
    ids = retry_seed.seed()
    with TestClient(build_app(worker=False)) as client:
        yield client, ids


def _hosted_on(monkeypatch, on: bool = True):
    """Settings as the page reads them, with Hosted mode acknowledged (or not)."""
    from kestrel.web.pages import needs_try

    orig = needs_try.load_settings_with_db

    async def patched(session):
        st = (await orig(session)).model_copy(deep=True)
        hm = st.llm.hosted_mode
        if on:
            hm.enabled = True
            hm.acknowledged_at = "2026-10-01T00:00:00Z"
            hm.acknowledged_terms = hm.disclosure_terms()
        return st

    monkeypatch.setattr(needs_try, "load_settings_with_db", patched)


def _online(monkeypatch, *, ids=("vendor/model-a", "vendor/model-b:free"), hosted_on=True):
    """A hosted provider with two models (one free), for the ask-first dialog and its gate."""
    from kestrel.llm.providers import ModelInfo

    _hosted_on(monkeypatch, hosted_on)

    raw = [
        ModelInfo(id=ids[0], name="Model A", context_length=131072, prompt_usd_per_m=2.0,
                  completion_usd_per_m=10.0),
        ModelInfo(id=ids[1], name="Model B", context_length=131072, prompt_usd_per_m=0.0,
                  completion_usd_per_m=0.0),
    ]
    picker = [PickerModel(id=m.id, name=m.name, context_length=m.context_length,
                          free=m.id.endswith(":free"), price="x") for m in raw]

    async def hosted(_settings):
        return ("OpenRouter", "openrouter", raw, picker, {}, None)

    monkeypatch.setattr("kestrel.web.pages.needs_try._hosted_models", hosted)
    monkeypatch.setattr("kestrel.web.pages.rescore._hosted_models", hosted)


# --------------------------------------------------------------------------- the words
def test_plain_reasons_never_show_the_stored_error():
    assert plain_reason("schema validation failed: verdict required") == (
        "The AI’s answer couldn’t be read")
    assert plain_reason("request error: ReadTimeout: timed out") == "No answer in time"
    assert plain_reason("generation loop detected, max_tokens hit") == "The AI repeated itself"
    assert plain_reason("finish_reason: length") == "The AI ran out of room"
    assert plain_reason("HTTP 401: invalid api key") == "Online service refused the key"
    assert plain_reason("HTTP 429 rate limit") == "Online service asked Kestrel to slow down"
    assert plain_reason("HTTP 400 bad request") == "Online service turned the request down"
    assert plain_reason("ConnectError: connection refused") == "Could not reach the AI"
    # Numbers inside other numbers or words are not status codes.
    assert plain_reason("schema failed near value 14290 line 4031") == (
        "The AI’s answer couldn’t be read")
    assert plain_reason("something nobody wrote down") == "Scoring did not finish"
    assert plain_reason(None) == "Scoring did not finish"
    assert refused_key("HTTP 403 unauthorized") and not refused_key("timed out")


def test_steps_when_and_tries_in_plain_words():
    assert step_for(1) == ("quick", "Quick sort")
    assert step_for(2) == ("fit", "Fit score")
    assert step_for(3) == ("deep", "Deep read")
    assert step_for(9) == ("other", "Other step") and step_for(None)[0] == "other"
    assert tries_words(1) == "1 try" and tries_words(2) == "2 tries"
    assert tries_words(0) == "1 try" and tries_words(None) == "1 try"
    now = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
    local = now.astimezone()
    assert re.fullmatch(r"\d{1,2}:\d{2} [AP]M", when_words(iso(now), now))
    yest = iso(now - timedelta(days=1))
    assert when_words(yest, now) == "Yesterday"
    assert when_words(iso(now - timedelta(days=3)), now) == (now - timedelta(days=3)).astimezone(
    ).strftime("%a")
    old = (now - timedelta(days=20)).astimezone()
    assert when_words(iso(now - timedelta(days=20)), now) == f"{old:%b} {old.day}"
    assert when_words(None) == "" and when_words("not a date") == "" and local


# ------------------------------------------------------------------------------ the page
def test_the_page_lists_every_role_in_plain_words(seeded):
    client, ids = seeded
    page = client.get("/quarantine").text
    assert "<h1>Needs another try</h1>" in page and "7 roles waiting" in page
    assert "Some roles couldn’t be scored. Nothing was dropped; they wait here." in page
    for role in retry_seed.ROWS:
        assert role[0] in page
    assert page.count('data-testid="rt-row"') == 7
    for reason in ("The AI’s answer couldn’t be read", "No answer in time",
                   "Online service refused the key", "The AI repeated itself"):
        assert reason in page
    row = _row_html(page, ids["Clinical Reporting Analyst"]["q"])
    assert "Deep read" in row and "3 tries" in row and 'href="/models"' in row
    # The visible copy has no developer words and no em dash; the raw reply sits behind details.
    visible = re.sub(r"<details.*?</details>", "", page.split("<main")[-1], flags=re.S)
    visible = re.sub(r"<script.*?</script[^>]*>|<style.*?</style[^>]*>", "", visible, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", visible).lower()
    for word in ("quarantine", "schema", "tier ", "json", "validation"):
        assert word not in text, word
    assert "—" not in page
    assert page.count("Show details") >= 7 and "What the AI said" in page


def test_filters_narrow_the_card_and_an_htmx_request_returns_the_card_alone(seeded):
    client, _ = seeded
    page = client.get("/quarantine", params={"step": "fit"}).text
    assert page.count('data-testid="rt-row"') == 3 and "Quick sort</span>" not in page.split(
        'id="rt-list"')[1].split('id="rt-empty"')[0]
    assert 'data-testid="rt-chip-fit"' in page
    chip = re.search(r'<button[^>]*data-testid="rt-chip-fit"[^>]*>', page).group(0)
    assert 'aria-pressed="true"' in chip and 'hx-target="#rt-card"' in chip
    card = client.get("/quarantine", params={"step": "deep"}, headers=HX).text
    assert card.lstrip().startswith("<section") and "<html" not in card
    assert card.count('data-testid="rt-row"') == 1
    # An unknown step is All, never an error.
    assert client.get("/quarantine", params={"step": "zzz"}).text.count(
        'data-testid="rt-row"') == 7
    counts = client.get("/quarantine/summary").text
    assert "7 roles waiting" in counts and 'id="rt-count" hx-swap-oob="true"' in counts
    for key, n in (("all", 7), ("quick", 3), ("fit", 3), ("deep", 1)):
        assert re.search(rf'data-testid="rt-chip-{key}".*?<span class="k-chip__n">{n}</span>',
                         counts, re.S), key


def test_empty_states(seeded):
    client, ids = seeded
    # A filter with nothing in it says so and points back at All.
    async def drop_deep(s):
        q = await s.get(Quarantine, ids["Clinical Reporting Analyst"]["q"])
        s.add(Evaluation(opportunity_id=q.opportunity_id, tier=3, track="employment",
                         model="m", verdict="apply", score=4.0, weighted_score=4.0,
                         created_at=iso(datetime.now(UTC) + timedelta(minutes=1))))

    _write(drop_deep)
    page = client.get("/quarantine", params={"step": "deep"}).text
    assert "Nothing in this step needs another try." in page
    assert "Choose All to see the other roles waiting." in page
    assert 'id="rt-empty" data-testid="rt-empty">' in page  # shown, not hidden
    # Nothing waiting at all.
    async def clear(s):
        for q in (await s.execute(select(Quarantine))).scalars():
            s.add(Evaluation(opportunity_id=q.opportunity_id, tier=3, track="employment",
                             model="m", verdict="apply", score=4.0, weighted_score=4.0,
                             created_at=iso(datetime.now(UTC) + timedelta(minutes=1))))

    _write(clear)
    page = client.get("/quarantine").text
    assert "Nothing needs another try." in page and "All clear" in page
    assert "Roles that can’t be scored will wait here until you decide." in page
    assert 'data-testid="rt-bulk"' in page and "disabled" in re.search(
        r'<button[^>]*data-testid="rt-bulk"[^>]*>', page, re.S).group(0)
    # What a later score resolved stays as history, never as a failure.
    assert 'data-testid="resolved-failures"' in page and "Scored since (7)" in page
    assert page.count('data-testid="rt-row"') == 0


def test_a_hidden_role_an_orphan_and_a_failed_score_again_each_read_right(seeded):
    client, ids = seeded
    async def add(s):
        opp = await s.get(Opportunity, ids["Inventory Coordinator"]["opp"])
        opp.archived_at = iso(datetime.now(UTC))
        s.add(Quarantine(opportunity_id=None, tier=9, model="m", raw_response="x",
                         error="boom", attempts=1))

    _write(add)
    page = client.get("/quarantine").text
    assert "(hidden)" in _row_html(page, ids["Inventory Coordinator"]["q"])
    orphan = page[page.index("No role attached") - 600:]
    assert "Other step" in orphan
    row = orphan.split('id="rt-row-', 1)[1]
    row = row.split('id="rt-row-', 1)[0] if 'id="rt-row-' in row else row
    assert "Try again" in row and "Remove" not in row and "Score it anyway" not in row


# ------------------------------------------------------------------------------ try again
def test_try_again_requeues_in_place_and_the_row_says_so(seeded):
    client, ids = seeded
    qid = ids["Operations Analyst"]["q"]
    r = client.post(f"/quarantine/{qid}/retry", headers=_hx(qid))
    assert r.status_code == 200
    assert f'id="rt-row-{qid}"' in r.text and "Trying again" in r.text
    assert 'data-testid="retry-queued"' in r.text and "3 tries" in r.text
    assert f'hx-get="/quarantine/{qid}/row"' in r.text and "every 4s" in r.text
    assert _events(r) == {"rt-refresh": True}
    assert ("t2", True) in [(t, p.get("requested")) for t, p in _tasks()]
    q = _read(lambda s: s.get(Quarantine, qid))
    assert q.attempts == 3 and q.raw_response.startswith('{"fit"')  # evidence untouched


def test_try_again_on_a_step_with_no_retry_explains_and_toasts(seeded):
    client, ids = seeded
    qid = ids["Clinical Reporting Analyst"]["q"]
    r = client.post(f"/quarantine/{qid}/retry", headers=_hx(qid))
    assert r.status_code == 200 and "operator-confirmed" in r.text
    ev = _events(r)
    assert ev["k-toast"]["tone"] == "warn" and "operator-confirmed" in ev["k-toast"]["message"]
    assert not _tasks()


def test_the_plain_post_still_returns_the_card_with_the_note(seeded):
    client, ids = seeded
    r = client.post("/quarantine/999999/retry")
    assert r.status_code == 200 and "No quarantine record" in r.text
    assert 'data-testid="retry-error"' in r.text and 'id="rt-card"' in r.text


# ------------------------------------------------------------------- a different model
def test_the_model_menu_offers_this_computer_first_then_online(seeded, monkeypatch):
    client, ids = seeded
    qid = ids["Operations Analyst"]["q"]
    r = client.get(f"/quarantine/{qid}/models")
    assert r.status_code == 200
    assert "On this computer, free" in r.text and "Online, uses your credit" in r.text
    assert "Deep read model" in r.text and "The careful one. A bit slower." in r.text
    assert 'name="model" value="kestrel-tier3:latest"' in r.text
    assert 'data-testid="rt-online"' not in r.text  # nothing online is set up
    assert "No online service is set up" in r.text
    _online(monkeypatch)
    r = client.get(f"/quarantine/{qid}/models")
    assert 'data-testid="rt-online"' in r.text and "Asks before sending anything." in r.text
    assert f'hx-get="/quarantine/{qid}/online"' in r.text


def test_a_local_pick_queues_that_model_for_this_role_only(seeded):
    client, ids = seeded
    qid = ids["Operations Analyst"]["q"]
    r = client.post(f"/quarantine/{qid}/model", data={"model": "kestrel-tier3:latest"},
                    headers=_hx(qid))
    assert r.status_code == 200 and "Trying again" in r.text
    ((typ, payload),) = _tasks()
    assert typ == "t2" and payload["rescore"] is True and payload["requested"] is True
    assert payload["model_override"] == {"backend": "local", "model": "kestrel-tier3:latest",
                                         "json_mode": "schema"}
    assert _read(lambda s: s.get(Quarantine, qid)).attempts == 3


def test_a_pick_that_is_not_offered_is_refused_in_the_row(seeded):
    client, ids = seeded
    qid = ids["Operations Analyst"]["q"]
    r = client.post(f"/quarantine/{qid}/model", data={"model": "not-a-model"}, headers=_hx(qid))
    assert "Not queued" in r.text and _events(r)["k-toast"]["tone"] == "warn"
    r = client.post(f"/quarantine/{qid}/model", data={"model": ""}, headers=_hx(qid))
    assert "Pick a model first." in r.text
    assert not _tasks()


def test_the_online_dialog_says_what_is_sent_and_the_cost_before_anything_is(seeded, monkeypatch):
    client, ids = seeded
    _online(monkeypatch)
    qid = ids["Operations Analyst"]["q"]
    r = client.get(f"/quarantine/{qid}/online")
    body = r.text
    assert "Try Operations Analyst with an online model?" in body
    assert "OpenRouter, using your own credit" in body
    assert "What will be sent" in body and "about 2 pages" in body  # 600 x 11 characters
    assert "Not sent:" in body and "your name, contact details and your resume file" in body
    assert 'value="hosted:vendor/model-a"' in body and "Model A, about" in body
    assert "Model B, free" in body
    assert "Cost:" in body and "Send and try" in body
    assert "data-k-static" in body  # a click outside never sends it
    assert 'hx-vals=\'{"confirmed": "1"}\'' in body
    assert not _tasks()  # opening the dialog sends nothing


def test_an_online_pick_is_queued_only_after_the_dialog_confirmed(seeded, monkeypatch):
    client, ids = seeded
    _online(monkeypatch)
    qid = ids["Operations Analyst"]["q"]
    r = client.post(f"/quarantine/{qid}/model", data={"model": "hosted:vendor/model-b:free"},
                    headers=_hx(qid))
    assert "Nothing was sent. Confirm in the dialog first." in r.text
    assert not _tasks()
    r = client.post(f"/quarantine/{qid}/model",
                    data={"model": "hosted:vendor/model-b:free", "confirmed": "1"},
                    headers=_hx(qid))
    assert "Trying again" in r.text
    ((typ, payload),) = _tasks()
    assert typ == "t2" and payload["model_override"]["model"] == "vendor/model-b:free"
    assert payload["model_override"]["backend"] == "openrouter"


def test_with_no_online_models_the_dialog_says_where_to_look(seeded):
    client, ids = seeded
    r = client.get(f"/quarantine/{ids['Operations Analyst']['q']}/online")
    assert "No online model can score a role right now. Check Models." in r.text
    assert "Send and try" in r.text and "<select" not in r.text


# --------------------------------------------------------------------------- score it anyway
def test_score_it_anyway_asks_first_then_queues_and_counts_the_try(seeded):
    client, ids = seeded
    qid = ids["Operations Analyst"]["q"]
    d = client.get(f"/quarantine/{qid}/anyway").text
    assert "Score Operations Analyst anyway?" in d
    assert "This skips your rules check" in d and "A role you would normally pass on" in d
    assert "you can still hide it from Pipeline later" in d and "Cancel" in d
    assert not _tasks()
    r = client.post(f"/quarantine/{qid}/anyway", headers=_hx(qid))
    assert r.status_code == 200 and "Trying again" in r.text
    ((typ, payload),) = _tasks()
    assert typ == "t2" and payload["requested"] is True and "model_override" not in payload
    assert _read(lambda s: s.get(Quarantine, qid)).attempts == 3


def test_score_it_anyway_on_a_role_a_rule_set_aside_records_the_choice_when_it_runs(seeded):
    client, ids = seeded
    qid = ids["Operations Analyst"]["q"]
    oid = ids["Operations Analyst"]["opp"]

    async def aside(s):
        opp = await s.get(Opportunity, oid)
        opp.stage = "t0_reject"
        opp.stage_reason = "rule:seniority_floor title below floor"

    _write(aside)
    r = client.post(f"/quarantine/{qid}/anyway", headers=_hx(qid))
    assert r.status_code == 200
    ((typ, payload),) = _tasks()
    # D-UI-74: the intent rides on a Tier 1 task; Tier 1 writes the setting row when it runs.
    assert typ == "t1" and payload["tier0_override"] is True and payload["requested"] is True
    assert payload["tier0_reason"] == "rule:seniority_floor title below floor"


# ------------------------------------------------------------------- remove and undo
def test_remove_takes_the_role_off_this_list_only_and_undo_brings_it_back(seeded):
    client, ids = seeded
    qid = ids["Supply Chain Planner"]["q"]
    oid = ids["Supply Chain Planner"]["opp"]
    before = _read(lambda s: failure_summary(s)).total
    r = client.post(f"/quarantine/{qid}/remove", headers={**HX, "HX-Target": f"rt-row-{qid}"})
    assert r.status_code == 200 and r.headers["HX-Reswap"] == "none" and r.text == ""
    ev = _events(r)
    assert ev["rt-gone"] == {"id": f"rt-row-{qid}"}
    assert ev["k-toast"]["message"] == (
        "Removed Supply Chain Planner from this list. It stays in Pipeline, not scored."
    )
    assert ev["k-toast"]["undoUrl"].startswith(f"/quarantine/{qid}/restore?at=")
    # Nothing is deleted: the row keeps its raw reply and the role is still there.
    q = _read(lambda s: s.get(Quarantine, qid))
    assert q.raw_response == "(no answer after 120 seconds)"
    assert _read(lambda s: s.get(Opportunity, oid)) is not None
    marker = _read(lambda s: s.get(Setting, f"retry_removed.{oid}"))
    assert marker is not None and "at" in marker.value
    # Gone from the list, the count and the pill; the other six stay.
    page = client.get("/quarantine").text
    assert f'id="rt-row-{qid}"' not in page and "6 roles waiting" in page
    assert _read(lambda s: failure_summary(s)).total == before - 1
    # Undo.
    r = client.post(ev["k-toast"]["undoUrl"])
    assert r.status_code == 204 and _events(r) == {"rt-reload": True}
    assert _read(lambda s: s.get(Setting, f"retry_removed.{oid}")) is None
    page = client.get("/quarantine").text
    assert f'id="rt-row-{qid}"' in page and "7 roles waiting" in page
    assert _read(lambda s: failure_summary(s)).total == before


def test_a_role_that_fails_again_after_remove_comes_back(seeded):
    client, ids = seeded
    qid = ids["Supply Chain Planner"]["q"]
    oid = ids["Supply Chain Planner"]["opp"]
    client.post(f"/quarantine/{qid}/remove")
    assert f'id="rt-row-{qid}"' not in client.get("/quarantine").text

    async def fail_again(s):
        q = Quarantine(opportunity_id=oid, tier=1, model="m", raw_response="again",
                       error="timed out", attempts=1,
                       created_at=iso(datetime.now(UTC) + timedelta(minutes=5)))
        s.add(q)
        await s.flush()
        return q.id

    new_q = _write(fail_again)
    page = client.get("/quarantine").text
    assert f'id="rt-row-{new_q}"' in page and "Supply Chain Planner" in page


def test_another_roles_newer_failure_does_not_bring_a_removed_role_back(seeded):
    """The Remove marker is per role: a newer failure of a different role says nothing about it."""
    client, ids = seeded
    qid = ids["Supply Chain Planner"]["q"]
    client.post(f"/quarantine/{qid}/remove")

    async def other_fails(s):
        s.add(Quarantine(opportunity_id=ids["Operations Analyst"]["opp"], tier=2, model="m",
                         raw_response="later", error="timed out", attempts=1,
                         created_at=iso(datetime.now(UTC) + timedelta(minutes=5))))

    _write(other_fails)
    page = client.get("/quarantine").text
    assert f'id="rt-row-{qid}"' not in page and "6 roles waiting" in page
    assert _read(lambda s: failure_summary(s)).total == 6


def test_remove_needs_a_role_and_no_route_deletes_a_row(seeded):
    client, ids = seeded

    async def orphan(s):
        q = Quarantine(opportunity_id=None, tier=9, model="m", error="x", attempts=1)
        s.add(q)
        await s.flush()
        return q.id

    oq = _write(orphan)
    r = client.post(f"/quarantine/{oq}/remove", headers=_hx(oq))
    assert "Only a failure with a role can be removed." in r.text
    assert not _read(lambda s: s.execute(select(Setting).where(
        Setting.key.like("retry_removed.%")))).scalars().all()
    qid = ids["Operations Analyst"]["q"]
    for verb in ("delete", "put", "patch"):
        for path in (f"/quarantine/{qid}", f"/quarantine/{qid}/remove",
                     f"/quarantine/{qid}/restore", f"/quarantine/{qid}/retry"):
            assert getattr(client, verb)(path).status_code in (404, 405), (verb, path)
    assert _read(lambda s: s.get(Quarantine, qid)) is not None


# --------------------------------------------------------------------------------- bulk
def test_try_all_again_queues_every_row_in_the_filter_and_swaps_only_those(seeded):
    client, ids = seeded
    r = client.post("/quarantine/bulk", data={"step": "quick"}, headers=HX)
    assert r.status_code == 200
    assert r.text.count('hx-swap-oob="outerHTML:#rt-row-') == 3
    for role in ("Supply Chain Planner", "Inventory Coordinator", "Data Quality Specialist"):
        assert f'hx-swap-oob="outerHTML:#rt-row-{ids[role]["q"]}"' in r.text
    assert "Trying again" in r.text
    ev = _events(r)
    assert ev["k-toast"]["message"] == (
        "Trying 3 roles again. Scores appear here as they finish.")
    assert ev["rt-refresh"] is True
    assert len(_tasks()) == 3


def test_try_selected_again_queues_only_the_ticked_rows(seeded):
    client, ids = seeded
    a, b = ids["Operations Analyst"]["q"], ids["Operations Manager"]["q"]
    r = client.post("/quarantine/bulk", data={"ids": [str(a), str(b)], "step": "all"}, headers=HX)
    assert r.text.count('hx-swap-oob="outerHTML:#rt-row-') == 2
    assert len(_tasks()) == 2
    # Rows already waiting are not queued twice by a second Try all again.
    r = client.post("/quarantine/bulk", data={"step": "all"}, headers=HX)
    assert len(_tasks()) == 2 + 4  # the other four with a role and a retry; Deep read explains
    assert "Trying" in _events(r)["k-toast"]["message"]


def test_bulk_with_nothing_to_try_says_so(seeded):
    client, _ = seeded
    r = client.post("/quarantine/bulk", data={"ids": ["999999"]}, headers=HX)
    assert r.text == "" and _events(r)["k-toast"]["message"] == "Nothing to try again."


# ------------------------------------------------------------------------- polling a row
def test_a_waiting_row_polls_and_folds_out_with_the_score_when_it_lands(seeded):
    client, ids = seeded
    qid = ids["Operations Analyst"]["q"]
    oid = ids["Operations Analyst"]["opp"]
    client.post(f"/quarantine/{qid}/retry", headers=_hx(qid))
    r = client.get(f"/quarantine/{qid}/row")
    assert "Trying again" in r.text and r.headers.get("HX-Trigger") is None  # still waiting

    async def score(s):
        s.add(Evaluation(opportunity_id=oid, tier=2, track="employment", model="m",
                         verdict="apply", score=4.3, weighted_score=4.3,
                         created_at=iso(datetime.now(UTC) + timedelta(minutes=1))))
        for t in (await s.execute(select(Task))).scalars():
            t.status = "done"

    _write(score)
    r = client.get(f"/quarantine/{qid}/row")
    assert r.status_code == 200 and r.text == "" and r.headers["HX-Reswap"] == "none"
    ev = _events(r)
    assert ev["rt-gone"] == {"id": f"rt-row-{qid}"}
    assert ev["k-toast"]["message"] == "Scored: Operations Analyst at Larkspur Logistics, 4.3 of 5."


def test_a_try_that_fails_again_flashes_the_row_and_says_so(seeded):
    client, ids = seeded
    qid = ids["Operations Analyst"]["q"]
    client.post(f"/quarantine/{qid}/retry", headers=_hx(qid))

    async def fail(s):
        for t in (await s.execute(select(Task))).scalars():
            t.status = "dead"

    _write(fail)
    r = client.get(f"/quarantine/{qid}/row")
    assert "rt-flash" in r.text and "Trying again" not in r.text and "3 tries" in r.text
    ev = _events(r)
    assert ev["k-toast"]["message"] == (
        "Still couldn’t score Operations Analyst. Try a different model, or score it anyway.")
    # A row that never existed is simply gone.
    r = client.get("/quarantine/999999/row")
    assert r.headers["HX-Reswap"] == "none" and "rt-gone" in _events(r)


# --------------------------------------------------------------------- template states
def _row(**kw):
    class Opp:
        id, title, org_name, org_id = 7, "Role & <b>Co</b>", "Fictional <i>Co</i>", None

    class Q:
        id, tier, model, error, attempts = 4, 2, "m", "boom", 2
        created_at, raw_response = "2026-10-15T00:00:00Z", "<script>alert(1)</script>"

    base = {"q": Q, "opp": Opp, "id": 4, "dom_id": "rt-row-4", "step_key": "fit",
            "step_label": "Fit score", "reason": "No answer in time", "refused_key": False,
            "when": "9:41 AM", "tries": "2 tries", "queued": False, "hidden": False,
            "rescore": False, "can_act": True, "error": "boom"}
    base.update(kw)
    return base


def _render(name, **ctx):
    return templates().get_template(name).render(**ctx)


def test_row_states_escape_what_a_model_or_a_posting_wrote():
    html = _render("partials/retry_row.html", row=_row())
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert "Role &amp; &lt;b&gt;Co&lt;/b&gt;" in html and "&lt;i&gt;Co&lt;/i&gt;" in html
    waiting = _render("partials/retry_row.html", row=_row(queued=True))
    assert "Trying again" in waiting and 'hx-trigger="every 4s"' in waiting
    assert 'data-testid="retry"' not in waiting  # no actions while it waits
    noted = _render("partials/retry_row.html", row=_row(refused_key=True, rescore=True,
                                                         hidden=True), note="Not queued: x")
    assert "Not queued: x" in noted and 'href="/models"' in noted
    assert "Your earlier score stands" in noted and "(hidden)" in noted
    oob = _render("partials/retry_row.html", row=_row(), oob=True, old_id="rt-row-9")
    assert 'hx-swap-oob="outerHTML:#rt-row-9"' in oob
    flashed = _render("partials/retry_row.html", row=_row(), flash=True)
    assert "rt-in rt-flash" in flashed
    assert "—" not in html + waiting + noted


def test_card_states():
    rows = [_row()]
    base = {"rows": rows, "counts": {"all": 1, "quick": 0, "fit": 1, "deep": 0},
            "filters": (("all", "All"), ("quick", "Quick sort"), ("fit", "Fit score"),
                        ("deep", "Deep read")),
            "step": "all", "waiting": 1, "resolved_rows": [], "open_more": 0, "outcome": None,
            "retry_error": None}
    html = _render("partials/retry_card.html", **base)
    assert 'id="rt-empty" data-testid="rt-empty" hidden' in html
    assert "Try all again" in html and "Select all" in html
    assert 'aria-pressed="true"' in html
    more = _render("partials/retry_card.html", **{**base, "open_more": 3,
                                                  "retry_error": "Could not retry",
                                                  "outcome": {"ok": True, "text": "Queued."}})
    assert "And 3 more older failures not shown here." in more
    assert 'data-testid="retry-error"' in more and "rt-banner is-good" in more
    hist = _render("partials/retry_card.html", **{**base, "resolved_rows": [_row()]})
    assert "Scored since (1)" in hist and "&lt;script&gt;" in hist
    empty = _render("partials/retry_card.html", **{**base, "rows": [], "waiting": 0})
    assert "Nothing needs another try." in empty and "disabled" in empty
    assert "—" not in html + more + hist + empty


def test_try_again_never_resends_to_an_online_model_without_the_dialog(seeded):
    """Hard rule 1: a failed online try is not repeated by Try again or Try all again."""
    client, ids = seeded
    qid, oid = ids["Operations Analyst"]["q"], ids["Operations Analyst"]["opp"]

    async def online_failure(s):
        t = Task(type="t2", status="done", payload={
            "opportunity_id": oid, "rescore": True,
            "model_override": {"backend": "openrouter", "model": "vendor/m"}})
        s.add(t)
        await s.flush()
        q = await s.get(Quarantine, qid)
        q.error = rescore_failure_prefix(t.id) + "timed out"

    _write(online_failure)
    before = len(_tasks())
    r = client.post(f"/quarantine/{qid}/retry", headers=_hx(qid))
    assert "That try used an online model" in r.text
    r = client.post("/quarantine/bulk", data={"ids": [str(qid)]}, headers=HX)
    assert len(_tasks()) == before


# ----------------------------------------------- the online dialog reads the real disclosure
def _confidential(on: bool = True):
    async def f(s):
        s.add(Setting(key="confidential_mode", value=on))

    _write(f)


def test_the_online_dialog_sends_list_is_the_hosted_disclosure_not_hand_written(
    seeded, monkeypatch
):
    import html

    from kestrel.config import HOSTED_TIER_SENDS

    client, ids = seeded
    _online(monkeypatch)
    body = html.unescape(client.get(f"/quarantine/{ids['Operations Analyst']['q']}/online").text)
    t2 = HOSTED_TIER_SENDS["tier2"].split("{")[0]  # the text before its first number
    assert t2[:1].upper() + t2[1:] in body
    assert "your full profile" in body and "A short summary of your skills" not in body


def test_the_dialog_says_the_employer_is_hidden_when_confidential_mode_has_one(
    seeded, monkeypatch
):
    from kestrel.docs import current_employer

    client, ids = seeded
    _online(monkeypatch)
    _confidential()

    async def one(session, **kw):
        return {"x": "y"}  # any non-empty map

    monkeypatch.setattr(current_employer, "load_employer_map", one)
    body = client.get(f"/quarantine/{ids['Operations Analyst']['q']}/online").text
    assert "your current employer is hidden before anything is sent" in body
    assert "will be held" not in body


def test_the_dialog_says_a_send_would_be_held_when_no_employer_is_chosen(seeded, monkeypatch):
    client, ids = seeded
    _online(monkeypatch)
    _confidential()
    body = client.get(f"/quarantine/{ids['Operations Analyst']['q']}/online").text
    assert 'data-testid="rt-employer-held"' in body and "This send will be held" in body
    assert "is hidden before anything is sent" not in body
    _write(lambda s: s.merge(Setting(key="confidential_mode", value=False)))
    off = client.get(f"/quarantine/{ids['Operations Analyst']['q']}/online").text
    assert "rt-employer" not in off


def test_a_held_send_shows_as_waiting_for_the_choice_not_as_a_failed_try(seeded):
    from kestrel.pipeline._shared import HOSTED_REFUSAL

    client, ids = seeded
    oid = ids["Operations Analyst"]["opp"]

    async def held(s):
        q = Quarantine(opportunity_id=oid, tier=2, model="m", error=HOSTED_REFUSAL, attempts=0,
                       created_at=iso(datetime.now(UTC) + timedelta(minutes=1)))
        s.add(q)
        await s.flush()
        return q.id

    new_id = _write(held)
    row = _row_html(client.get("/quarantine").text, new_id)
    assert "Waiting for you to choose your current employer" in row
    assert 'data-testid="rt-held"' in row and "Not sent" in row
    assert "couldn’t be read" not in row and "retry-queued" not in row
    r = client.get(f"/quarantine/{new_id}/row")  # the poll: a calm note, no flash, no warning
    toast = _events(r)["k-toast"]
    assert r.status_code == 200 and "rt-flash" not in r.text
    assert "tone" not in toast and "Held, not sent" in toast["message"]
    assert plain_reason(HOSTED_REFUSAL) == "Waiting for you to choose your current employer"


def test_an_online_pick_is_refused_server_side_when_hosted_mode_is_off(seeded, monkeypatch):
    client, ids = seeded
    _online(monkeypatch, hosted_on=False)
    qid = ids["Operations Analyst"]["q"]
    r = client.post(f"/quarantine/{qid}/model",
                    data={"model": "hosted:vendor/model-a", "confirmed": "1"}, headers=_hx(qid))
    assert "Nothing was sent. Online models need Hosted mode" in r.text and not _tasks()


def test_an_online_pick_must_be_a_model_the_dialog_lists(seeded, monkeypatch):
    client, ids = seeded
    _online(monkeypatch)
    qid = ids["Operations Analyst"]["q"]
    r = client.post(f"/quarantine/{qid}/model",
                    data={"model": "hosted:vendor/unlisted", "confirmed": "1"}, headers=_hx(qid))
    assert "That model is not on the online list" in r.text and not _tasks()


def test_a_removed_role_leaves_pipelines_scoring_failed_count(seeded):
    client, ids = seeded
    # The Tier 2 and Tier 3 failures park the role; a Tier 1 failure is promoted, not parked.
    before = client.get("/opportunities").text
    # The header and the Failed scoring chip carry the number Needs another try shows (7 roles
    # waiting); the Scoring failed group lists the four that are parked with no score.
    assert "7 failed scoring" in before and "Scoring failed · 4" in before
    assert re.search(r'data-chip="failed"[^>]*>Failed scoring<span class="k-chip__n">7<', before)
    assert "7 roles waiting" in client.get("/quarantine").text
    qid = ids["Operations Analyst"]["q"]
    removed = client.post(f"/quarantine/{qid}/remove")
    after = client.get("/opportunities").text
    assert "6 failed scoring" in after and "Scoring failed · 3" in after
    assert re.search(r'data-chip="failed"[^>]*>Failed scoring<span class="k-chip__n">6<', after)
    assert "6 roles waiting" in client.get("/quarantine").text
    client.post(_events(removed)["k-toast"]["undoUrl"])
    assert "7 failed scoring" in client.get("/opportunities").text


def test_today_rows_show_the_time_of_day_and_older_ones_the_day():
    # A fixed noon, so "five minutes ago" is always today whatever the clock says.
    now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    assert ":" in when_words(iso(now - timedelta(minutes=5)), now) and (
        when_words(iso(now - timedelta(minutes=5)), now)[-2:] in ("AM", "PM"))
    assert when_words(iso(now - timedelta(days=1, hours=1)), now) == "Yesterday"


def test_the_list_card_has_the_amber_accent_and_uses_the_shared_stacking():
    from pathlib import Path

    css = Path("kestrel/web/static/css/retry.css").read_text(encoding="utf-8")
    assert "border-left: 4px solid var(--warn)" in css
    # The help column stacks at 1300px like the mockup, via the shared k-layout rule (D-UI-134).
    assert not re.search(r"\.k-(layout|help-col)\s*\{", css)
    assert not re.search(r"#[0-9a-fA-F]{3,8}|rgba?\(", css)
    shell = Path("kestrel/web/static/css/shell.css").read_text(encoding="utf-8")
    assert "rgba(0, 0, 0, .3)" not in shell


def test_the_announcement_slot_is_not_on_this_page(seeded):
    client, _ = seeded
    assert 'id="announcement-slot"' not in client.get("/quarantine").text
    assert 'id="announcement-slot"' not in client.get("/sources").text
    assert "refreshed look" not in client.get("/").text.lower()  # removed (D-UI-157)


def test_a_tier_1_retry_runs_on_the_full_review_and_a_tier_3_one_says_escalation_is_confirmed(
    seeded,
):
    """Restores the two assertions the old page test carried (test_quarantine_resolution)."""
    client, ids = seeded
    q1 = ids["Supply Chain Planner"]["q"]  # failed at Tier 1
    r = client.post(f"/quarantine/{q1}/retry", headers=_hx(q1))
    assert "retry-queued" in r.text
    ((typ, payload),) = _tasks()
    assert typ == "t2" and payload["opportunity_id"] == ids["Supply Chain Planner"]["opp"]
    menu = client.get(f"/quarantine/{q1}/models").text
    assert "Full review model" not in menu and "Deep read model" in menu  # Try again IS the review
    q3 = ids["Clinical Reporting Analyst"]["q"]  # failed at Tier 3
    r = client.post(f"/quarantine/{q3}/retry", headers=_hx(q3))
    assert "operator-confirmed" in r.text and len(_tasks()) == 1


def test_online_dialog_mentions_training_only_for_free_models():
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("kestrel/web/templates"))
    tpl = env.get_template("partials/retry_online.html")
    base = dict(quarantine_id=1, dom_id="rt-row-1", role="Analyst", org=None,
                provider="OpenRouter", note=None, pages=None, sends="the posting",
                employer=None, credit=None, models=[{"id": "x/y", "name": "Y", "cost": "1c"}])
    assert "training" not in tpl.render(**base, any_free=False)
    assert "Free models may use what they receive for training." in tpl.render(
        **base, any_free=True)


def test_remove_undo_does_not_clear_a_later_removal(seeded):
    client, ids = seeded
    qid = ids["Supply Chain Planner"]["q"]
    oid = ids["Supply Chain Planner"]["opp"]
    first = _events(client.post(f"/quarantine/{qid}/remove"))["k-toast"]["undoUrl"]
    client.post(f"/quarantine/{qid}/remove")
    assert "This can no longer be undone." in client.post(first).text
    assert _read(lambda s: s.get(Setting, f"retry_removed.{oid}")) is not None
