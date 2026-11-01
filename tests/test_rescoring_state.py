"""Being scored again (issue #74): a role that already has a score and is queued or running for a
new one says so, with its place in line, on the role page, the Pipeline row and side panel, and
the Today card. The earlier score stays, labeled, until the new evaluation lands; then every
surface drops the notice. Task status pending reads queued, leased reads running; a task held for
the operator's approval is not in line."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from kestrel.services.online_consent import APPROVAL_HOLD
from kestrel.web.rescoring import typical_words
from tests import rescoring_helpers as rh
from tests import role_seed

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


@pytest.fixture
def ids():
    return asyncio.run(role_seed.seed_async())


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _row(html: str, oid: int) -> str:
    i = html.index(f'id="row-{oid}"')
    j = html.find('role="listitem"', i)
    return html[i:j] if j > 0 else html[i:]


# ------------------------------------------------------------------------------ the role page
def test_a_queued_score_again_says_so_and_keeps_the_earlier_score(ids):
    rh.queue(ids["strong"])
    with _client() as c:
        html = c.get(f"/opportunities/{ids['strong']}").text
    assert 'data-testid="rescoring"' in html and 'data-rescoring="queued"' in html
    assert "Being scored again" in html and "Waiting for its turn." in html
    assert "It is next in line." in html
    assert 'data-testid="earlier-score"' in html and "Earlier score" in html
    assert 'data-testid="score-holistic"' in html and 'data-testid="score-card"' in html


def test_a_running_score_again_says_scoring_now(ids):
    rh.lease(rh.queue(ids["strong"]))
    with _client() as c:
        html = c.get(f"/opportunities/{ids['strong']}").text
    assert 'data-rescoring="running"' in html and "Scoring now." in html
    assert "Waiting for its turn" not in html


def test_the_place_in_line_counts_what_the_worker_takes_first(ids):
    rh.queue(ids["second"], priority=5)       # higher priority: ahead
    rh.queue(ids["maybe"])                    # same priority, older id: ahead
    mine = rh.queue(ids["strong"])
    rh.queue(ids["second"], type="t1", priority=-1)  # lower priority: behind
    rh.lease(rh.queue(ids["lead"]))           # already running: ahead
    assert mine
    with _client() as c:
        body = c.get(f"/opportunities/{ids['strong']}/role/rescoring").json()
    assert body["state"] == "queued" and body["hint"] == "Waiting for its turn. 3 ahead of it."
    assert body["title"] == "Being scored again"


def test_the_poll_answers_none_once_the_score_has_landed(ids):
    task = rh.queue(ids["strong"])
    with _client() as c:
        url = f"/opportunities/{ids['strong']}/role/rescoring"
        assert c.get(url).json()["state"] == "queued"
        rh.land(ids["strong"], task, score=4.9)
        assert c.get(url).json() == {"state": "none"}
        html = c.get(f"/opportunities/{ids['strong']}").text
    assert 'data-testid="rescoring"' not in html and "Earlier score" not in html
    assert ">4.9<" in html


def test_a_task_held_for_approval_is_not_in_line(ids):
    rh.queue(ids["strong"], held=APPROVAL_HOLD)
    with _client() as c:
        html = c.get(f"/opportunities/{ids['strong']}").text
        body = c.get(f"/opportunities/{ids['strong']}/role/rescoring").json()
    assert 'data-testid="rescoring"' not in html and 'data-testid="scoring-held"' in html
    assert body == {"state": "none"}


def test_a_role_with_no_score_yet_reads_being_scored_with_the_same_hint(ids):
    rh.queue(ids["unscored"], type="t1")
    with _client() as c:
        html = c.get(f"/opportunities/{ids['unscored']}").text
    assert "<h3>Being scored</h3>" in html and "It is next in line." in html
    assert 'data-testid="score-anyway"' not in html


# ------------------------------------------------------------------------------ the Pipeline
def test_the_pipeline_row_and_panel_say_being_scored_again(ids):
    rh.queue(ids["strong"])
    with _client() as c:
        html = c.get("/opportunities").text
        panel = c.get(f"/opportunities/{ids['strong']}/panel").text
    row = _row(html, ids["strong"])
    assert 'data-rescoring="queued"' in row and 'data-testid="row-rescoring"' in row
    assert "Being scored again" in row and 'data-testid="row-earlier"' in row
    assert 'data-fit-score' in row  # the earlier score is still on the row
    assert 'data-rescoring="queued"' in panel and 'data-testid="panel-rescoring"' in panel
    assert "The score below is the earlier one." in panel and "Waiting for its turn." in panel


def test_the_pipeline_row_and_panel_show_running_then_nothing_once_landed(ids):
    task = rh.queue(ids["strong"])
    rh.lease(task)
    with _client() as c:
        row = _row(c.get("/opportunities").text, ids["strong"])
        panel = c.get(f"/opportunities/{ids['strong']}/panel").text
        assert 'data-rescoring="running"' in row and "Scoring now." in panel
        rh.land(ids["strong"], task, score=4.9)
        row = _row(c.get("/opportunities").text, ids["strong"])
        panel = c.get(f"/opportunities/{ids['strong']}/panel").text
    assert "data-rescoring" not in row and "row-rescoring" not in row
    assert "data-rescoring" not in panel and "4.9" in panel


def test_an_unscored_pipeline_row_gets_its_place_in_line_in_the_tip(ids):
    rh.queue(ids["unscored"], type="t1", priority=3)
    rh.queue(ids["maybe"])
    with _client() as c:
        row = _row(c.get("/opportunities").text, ids["unscored"])
    assert 'data-scoring="queued"' in row and "It is next in line." in row


# ----------------------------------------------------------------------------------- Today
def test_the_today_card_says_being_scored_again_and_redraws_when_it_moves_on(ids):
    rh.fresh_today(ids["strong"])
    task = rh.queue(ids["strong"])
    with _client() as c:
        html = c.get("/").text
        assert 'data-rescoring="queued"' in html and 'data-testid="td-rescoring"' in html
        assert "Being scored again." in html and 'data-testid="td-earlier"' in html
        have = str(ids["strong"])
        same = c.get(f"/today/matches/new?have={have}&again={have}:queued").json()
        assert same["done"] == []
        rh.lease(task)
        moved = c.get(f"/today/matches/new?have={have}&again={have}:queued").json()
        assert [d["id"] for d in moved["done"]] == [ids["strong"]]
        assert 'data-rescoring="running"' in moved["done"][0]["html"]
        rh.land(ids["strong"], task, score=4.9)
        landed = c.get(f"/today/matches/new?have={have}&again={have}:running").json()
        card = landed["done"][0]["html"]
    assert "data-rescoring" not in card and "4.9" in card


def test_typical_time_is_said_in_plain_words():
    assert typical_words(None) is None
    assert typical_words(20_000) == "under a minute"
    assert typical_words(70_000) == "about a minute"
    assert typical_words(190_000) == "about 3 minutes"


def test_the_pipeline_panel_for_a_role_with_no_score_gives_its_place_in_line(ids):
    rh.lease(rh.queue(ids["maybe"], type="t1"))
    rh.queue(ids["unscored"], type="t1")
    with _client() as c:
        panel = c.get(f"/opportunities/{ids['unscored']}/panel").text
    assert 'data-testid="panel-rescoring"' in panel and "1 ahead of it." in panel


# ----------------------------------------------------- a first score is not "again" (review)
def test_a_never_scored_role_says_being_scored_with_no_again_and_no_earlier(ids):
    rh.queue(ids["unscored"], type="t1")
    with _client() as c:
        role = c.get(f"/opportunities/{ids['unscored']}").text
        body = c.get(f"/opportunities/{ids['unscored']}/role/rescoring").json()
        row = _row(c.get("/opportunities").text, ids["unscored"])
        panel = c.get(f"/opportunities/{ids['unscored']}/panel").text
    assert body["title"] == "Being scored" and body["state"] == "queued"
    assert "It is next in line." in body["hint"]
    for text in (role, row, panel):
        assert "Being scored again" not in text and "Earlier score" not in text
        assert "scored again" not in text.lower()
    assert "Being scored" in role and "Being scored" in panel
    assert 'data-testid="rescoring"' in role and "Waiting for its turn." in role
    assert 'data-testid="panel-earlier"' not in panel and 'data-testid="row-earlier"' not in row


def test_the_state_object_only_says_again_for_a_role_with_a_tier_two_score(ids):
    rh.queue(ids["unscored"], type="t1")
    rh.queue(ids["strong"])

    async def go():
        from kestrel.db.engine import get_sessionmaker
        from kestrel.web.rescoring import rescoring_states

        async with get_sessionmaker()() as s:
            return await rescoring_states(s, [ids["unscored"], ids["strong"]])

    found = rh.run(lambda _s: go())
    assert found[ids["unscored"]].title == "Being scored" and not found[ids["unscored"]].again
    assert found[ids["strong"]].title == "Being scored again" and found[ids["strong"]].again


def test_the_place_in_line_is_one_statement_for_every_queued_role(ids):
    from sqlalchemy import event

    for oid in (ids["second"], ids["maybe"], ids["lead"]):
        rh.queue(oid, type="t1")
    rh.queue(ids["strong"])
    rh.queue(ids["unscored"])
    seen: list[tuple[str, int]] = []

    async def go(session):
        from kestrel.web.rescoring import rescoring_states

        eng = session.bind.sync_engine

        def hook(conn, cur, statement, params, ctx, many):
            seen.append((statement, 0))

        event.listen(eng, "before_cursor_execute", hook)
        try:
            return await rescoring_states(session, [ids["strong"], ids["unscored"]])
        finally:
            event.remove(eng, "before_cursor_execute", hook)

    found = rh.run(go)
    assert found[ids["strong"]].ahead == 3 and found[ids["unscored"]].ahead == 4
    lines = [st for st, _ in seen if "row_number" in st.lower()]
    assert len(lines) == 1, seen
    # The live task rows are read once; the line is ranked in the database.
    reads = [st for st, _ in seen if st.lstrip().upper().startswith("SELECT")
             and "row_number" not in st.lower() and "evaluation" not in st.lower()]
    assert len(reads) == 1, reads


def test_a_deferred_task_is_stalled_but_a_failed_one_that_will_retry_is_not(ids):
    # Credit / local-only deferral (queue.defer): it cannot run now, so the poll may stop.
    task = rh.queue(ids["strong"])
    rh.give_back(task, "defer", "Your OpenRouter credit has run out.")
    with _client() as c:
        html = c.get(f"/opportunities/{ids['strong']}").text
        body = c.get(f"/opportunities/{ids['strong']}/role/rescoring").json()
    assert body["stalled"] is True and body["state"] == "queued"
    assert body["hint"] == "Waiting to try again. Kestrel will pick it up when it can."
    assert 'data-stalled="1"' in html and 'data-testid="rescoring"' in html
    # An error that requeued the task (queue.fail) leaves last_error set but it runs next:
    # "Waiting for its turn", and the poll keeps going.
    rh.clear_tasks()
    task = rh.queue(ids["strong"])
    rh.give_back(task, "fail", "connection refused")
    with _client() as c:
        html = c.get(f"/opportunities/{ids['strong']}").text
        body = c.get(f"/opportunities/{ids['strong']}/role/rescoring").json()
    assert body["stalled"] is False and body["hint"].startswith("Waiting for its turn")
    assert 'data-stalled="0"' in html


def test_leasing_a_deferred_task_again_clears_the_stall(ids):
    from kestrel.queue.queue import DEFERRED_KEY

    task = rh.queue(ids["strong"])
    rh.give_back(task, "defer", "held")
    rh.give_back(task, "defer", "held")  # leases it again, then defers again
    with _client() as c:
        assert c.get(f"/opportunities/{ids['strong']}/role/rescoring").json()["stalled"] is True

    async def payload(s):
        from kestrel.db.models import Task

        return (await s.get(Task, task)).payload

    assert rh.run(payload).get(DEFERRED_KEY) is True


def test_a_deferred_task_is_not_ahead_of_anyone_in_line(ids):
    held = rh.queue(ids["second"], priority=5)
    rh.give_back(held, "defer", "held: background roles use local models only")
    rh.queue(ids["maybe"])
    rh.queue(ids["strong"])
    with _client() as c:
        body = c.get(f"/opportunities/{ids['strong']}/role/rescoring").json()
    assert body["hint"] == "Waiting for its turn. 1 ahead of it."


def test_the_role_poll_stops_when_stalled_and_slows_after_two_minutes():
    from pathlib import Path

    js = (Path(__file__).parents[1] / "kestrel/web/static/js/role.js").read_text()
    assert "RS_SLOW_AFTER = 120000" in js and "RS_SLOW = 15000" in js
    assert "data-stalled" in js and "d.stalled" in js
