"""A role nothing will score never reads "Waiting, it scores on its own". Four cases used to:
its last scoring task died after its tries (the model could not be reached), Kestrel is paused,
its task is held because the online model is out of credit, and a background-lane role while
scoring is online (the lane never scores online). Each now names the reason and the one action
that moves it forward, on the Pipeline row and the role page. The roles stay listed (hard rule
4): nothing here hides, rejects, queues or sends anything. Invented text only."""

from __future__ import annotations

import asyncio
import html as htmllib

import pytest
from sqlalchemy import func, select

from kestrel.db.models import Opportunity, Task
from kestrel.pipeline import lane
from kestrel.queue.queue import DEFER_REASON_KEY, DEFERRED_KEY, REASON_CREDIT
from kestrel.web import app as app_module
from kestrel.web.fit import EMPTY_POSTING_STAGE_REASON
from kestrel.web.unscored import stopped_words
from tests import test_pipeline_view as T

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _setup(task: dict | None, **opp_fields) -> int:
    """Seed the shared Pipeline fixture, then make role d (no score yet) carry `task`."""
    ids = asyncio.run(T._seed())
    oid = ids["d"]

    async def go(s):
        opp = await s.get(Opportunity, oid)
        for k, v in opp_fields.items():
            setattr(opp, k, v)
        if task is not None:
            s.add(Task(payload={"opportunity_id": oid, **task.pop("payload", {})}, **task))
        await s.commit()

    asyncio.run(T._with_session(go))
    return oid


def _row(oid: int) -> str:
    with T._client() as c:
        html = T._get(c, "/opportunities")
    return htmllib.unescape(T._row(html, oid))


def _role(oid: int) -> str:
    with T._client() as c:
        return htmllib.unescape(c.get(f"/opportunities/{oid}").text)


@pytest.fixture
def unpaused():
    app_module.set_paused(False)
    yield
    app_module.set_paused(False)


def test_a_dead_task_reads_stopped_with_the_cause_and_score_again(unpaused):
    oid = _setup({"type": "t1", "status": "dead", "attempts": 3,
                  "last_error": "ConnectError: connection refused"})
    row = _row(oid)
    assert 'data-scoring="dead"' in row
    assert "Kestrel could not reach the model." in row
    assert "scores on its own" not in row
    assert f"/rescore/choose?opp_ids={oid}" in row  # Score again
    page = _role(oid)
    assert 'data-unscored="dead"' in page
    assert "Kestrel could not reach the model." in page


def test_a_paused_app_reads_paused_with_resume_on_runs(unpaused):
    oid = _setup({"type": "t1", "status": "pending"})
    app_module.set_paused(True)
    row = _row(oid)
    assert 'data-scoring="app_paused"' in row
    assert 'href="/runs"' in row and "Resume on Runs" in row
    page = _role(oid)
    assert 'data-unscored="app_paused"' in page and "Resume on Runs" in page


def test_an_out_of_credit_hold_says_so_and_points_at_models(unpaused):
    oid = _setup({"type": "t2", "status": "pending",
                  "last_error": "held: the provider says the key has no credit",
                  "payload": {DEFERRED_KEY: True, DEFER_REASON_KEY: REASON_CREDIT}},
                 stage="t1_pass")
    row = _row(oid)
    assert 'data-scoring="credit"' in row
    assert "out of credit" in row and 'href="/models"' in row


def test_a_background_role_under_online_scoring_never_says_it_scores_when_idle(
    unpaused, monkeypatch
):
    oid = _setup({"type": "t1", "status": "pending", "priority": lane.LANE_PRIORITY,
                  "payload": {"lane": 1}})
    monkeypatch.setitem(app_module._run_mode, "mode", "hosted")
    row = _row(oid)
    assert 'data-scoring="local_only"' in row
    assert "scores when Kestrel is idle" not in row
    assert "only on a local model" in row and 'href="/models"' in row


def test_a_waiting_role_with_a_live_task_still_reads_waiting(unpaused):
    """The control: a queued role the worker will claim keeps its Waiting chip."""
    oid = _setup({"type": "t1", "status": "pending"})
    row = _row(oid)
    assert 'data-scoring="queued"' in row


def test_an_empty_posting_names_its_source_and_offers_the_paste(unpaused):
    """The shared seed's source is a Greenhouse board; the link's host never decides it."""
    oid = _setup(None, stage="t1_pass", stage_reason=EMPTY_POSTING_STAGE_REASON,
                 description_md=None, url="https://www.glassdoor.com/job-listing/invented-1")
    row = _row(oid)
    assert "The posting has no description. Greenhouse sent it without one" in row
    assert "Glassdoor" not in row.replace("glassdoor.com", "")
    assert f'href="/opportunities/{oid}"' in row and "Paste the description" in row


def test_a_dead_role_says_it_moves_to_needs_another_try(unpaused):
    """The worker quarantines an active role's dead scoring task (D-WORKER-5, D-WORKER-8: this
    replaced "tries once more the next time it starts"), so the row says where it goes, and
    "nothing will score it" would be false here."""
    oid = _setup({"type": "t1", "status": "dead", "attempts": 3, "last_error": "boom"})
    row = _row(oid)
    assert 'data-scoring="dead"' in row
    assert "moves it to Needs another try" in row
    assert "nothing will score it on its own" not in row
    assert "tries once more" not in row


def test_a_hidden_dead_role_never_promises_a_retry(unpaused):
    """The startup sweep skips hidden roles, so no restart will try it again."""
    oid = _setup({"type": "t1", "status": "dead", "attempts": 3, "last_error": "boom"},
                 archived_at="2026-01-01T00:00:00+00:00", archive_reason="not_for_me")
    page = _role(oid)
    assert 'data-unscored="dead_final"' in page
    assert "will not try again on its own" in page
    assert "tries once more" not in page and "after Kestrel restarted" not in page


def test_a_dead_role_at_any_active_stage_moves_to_needs_another_try(unpaused):
    """The dead-task quarantine takes an active role at any stage (D-WORKER-8: the old startup
    sweep re-queued only at t0_pass and t1_pass, so a role at `new` read dead_final), and the
    row never promises a retry Kestrel will not make."""
    oid = _setup({"type": "t1", "status": "dead", "attempts": 3, "last_error": "boom"},
                 stage="new")
    row = _row(oid)
    assert 'data-scoring="dead"' in row
    assert "moves it to Needs another try" in row and "tries once more" not in row


def test_a_role_the_sweep_already_requeued_says_nothing_will_score_it(unpaused):
    """In the orphan record at its stage: the sweep will not try again, so it is stuck."""
    from kestrel.db.models import Setting
    from kestrel.pipeline.orphans import ORPHAN_KEY, record_key

    oid = _setup({"type": "t1", "status": "dead", "attempts": 3, "last_error": "boom"})

    async def mark(s):
        s.add(Setting(key=ORPHAN_KEY, value={record_key(oid, "t0_pass"): "2026-01-01"}))
        await s.commit()

    asyncio.run(T._with_session(mark))
    row = _row(oid)
    assert 'data-scoring="stuck"' in row
    assert "nothing will score it on its own" in row
    assert "tries once more" not in row


def test_a_restore_or_reset_hold_never_says_resume(unpaused, monkeypatch):
    """A maintenance hold ends on its own; Resume cannot end it, so it is never offered."""
    oid = _setup({"type": "t1", "status": "pending"})
    monkeypatch.setitem(app_module._holds, "n", 1)
    row = _row(oid)
    assert 'data-scoring="maintenance"' in row
    assert "Resume on Runs" not in row and 'href="/runs"' not in row
    assert "restoring or resetting your data" in row
    page = _role(oid)
    assert 'data-unscored="maintenance"' in page and "Resume on Runs" not in page


def test_nothing_is_queued_or_hidden_by_reading_the_reasons(unpaused):
    """Hard rule 4 and no side effects: reading every state leaves the task table and the
    role untouched."""
    oid = _setup({"type": "t1", "status": "dead", "attempts": 3, "last_error": "boom"})

    async def snapshot(s):
        n = (await s.execute(select(func.count()).select_from(Task))).scalar_one()
        opp = await s.get(Opportunity, oid)
        return n, opp.stage, opp.archived_at

    before = asyncio.run(T._with_session(snapshot))
    _row(oid)
    _role(oid)
    assert asyncio.run(T._with_session(snapshot)) == before
    assert before[2] is None


@pytest.mark.parametrize(
    ("error", "words"),
    [
        ("httpx.ConnectError: [Errno 111] Connection refused", "could not reach the model"),
        ("model 'kestrel-tier1' not found, try pulling it first", "not installed"),
        ("HTTP 404: No endpoints found for invented/model-1", "online service does not offer"),
        ("404 model_not_found: The model does not exist", "online service does not offer"),
        ("HTTP 404 Not Found", "could not be found"),
        ("HTTP 401 Unauthorized", "did not accept the key"),
        (None, "Scoring did not finish."),
    ],
)
def test_the_cause_is_in_plain_words(error, words):
    assert words in stopped_words(error)


@pytest.mark.parametrize(
    "error",
    [
        "HTTP 404: No endpoints found for invented/model-1",
        "404 model_not_found: The model does not exist",
        "HTTP 404 Not Found",
        "HTTP 401 Unauthorized",
    ],
)
def test_an_online_error_never_blames_this_computer(error):
    assert "this computer" not in stopped_words(error)
    assert "—" not in stopped_words(error)
