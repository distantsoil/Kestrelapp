"""Every retry runs the failed row's own step (#206 round 11, D-UI-790).

One table drives every entry point that can start a retry (per-row Try again, the Runs card's Try
again, Try all again, Try with a different model on this computer and online, Score it anyway,
Score again naming the row, and the banner's group fix on this computer and online) for a failure
at each tier, and checks the queued task against the failed row: its type is the row's own step,
never the role's current stage. A row whose step cannot be run again here queues nothing and spends
no retry. Invented roles only; no network, no model."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import quote

import pytest
from sqlalchemy import select, update

from kestrel.db.models import Opportunity, Quarantine
from kestrel.llm import failure_kind as fk
from kestrel.web import retry_groups
from tests.test_needs_try import _online, _read, _tasks, _write
from tests.test_retry_groups import make  # noqa: F401  (the shared seeded-client fixture)

HX = {"HX-Request": "true"}
LOCAL = "kestrel-tier3:latest"
ONLINE = "hosted:vendor/model-b:free"
STEP_KEY = {1: "quick", 2: "fit", 3: "deep", 5: "other"}

# (case, the failed row's tier, the role's stage now, the task a retry of that row must queue).
# Only Fit score can be run again from a row: Quick sort already kept the role (hard rule 4),
# Deep read is operator-confirmed escalation, other steps have no task here, and a Fit score row
# whose role has moved on since (here, set aside by a later Quick sort) is not re-run behind it.
CASES = [
    ("quick-sort-kept", 1, "t1_pass", None),
    ("quick-sort-scored-since", 1, "t2_done", None),
    ("fit-score-parked", 2, "quarantined", "t2"),
    ("fit-score-scored", 2, "t2_done", "t2"),
    ("fit-score-moved-on", 2, "t1_reject", None),
    ("deep-read", 3, "t2_done", None),
    ("other-step", 5, "t2_done", None),
]


def _post_retry(client, ids, _key):
    qid = ids["Role 0"]["q"]
    client.post(f"/quarantine/{qid}/retry", headers={**HX, "HX-Target": f"rt-row-{qid}"})


def _post_runs_retry(client, ids, _key):
    client.post(f"/runs/quarantine/{ids['Role 0']['q']}/retry", headers=HX)


def _post_bulk(client, ids, _key):
    client.post("/quarantine/bulk", data={"ids": [str(ids["Role 0"]["q"])]}, headers=HX)


def _post_model_local(client, ids, _key):
    qid = ids["Role 0"]["q"]
    client.post(f"/quarantine/{qid}/model", data={"model": LOCAL},
                headers={**HX, "HX-Target": f"rt-row-{qid}"})


def _post_model_online(client, ids, _key):
    qid = ids["Role 0"]["q"]
    client.post(f"/quarantine/{qid}/model", data={"model": ONLINE, "confirmed": "1"},
                headers={**HX, "HX-Target": f"rt-row-{qid}"})


def _post_anyway(client, ids, _key):
    qid = ids["Role 0"]["q"]
    client.post(f"/quarantine/{qid}/anyway", headers={**HX, "HX-Target": f"rt-row-{qid}"})


def _post_rescore_naming_row(client, ids, _key):
    client.post("/rescore", data={"opp_ids": str(ids["Role 0"]["opp"]), "model_choice": "other",
                                  "model": LOCAL, "quarantine_id": str(ids["Role 0"]["q"])},
                headers=HX)


def _all_opps(ids) -> str:
    return ",".join(str(ids[f"Role {i}"]["opp"]) for i in range(3))


def _post_group_local(client, ids, key):
    client.post(f"/quarantine/group/{quote(key)}/model",
                data={"model": LOCAL, "group_ids": _all_opps(ids)}, headers=HX)


def _post_group_online(client, ids, key):
    client.post(f"/quarantine/group/{quote(key)}/model",
                data={"model": ONLINE, "confirmed": "1", "group_ids": _all_opps(ids)},
                headers=HX)


# (entry point, whether it acts on the whole group of three, whether it is an online pick)
ENTRY_POINTS = [
    ("try-again", _post_retry, False, False),
    ("runs-try-again", _post_runs_retry, False, False),
    ("try-all-again", _post_bulk, False, False),
    ("different-model-local", _post_model_local, False, False),
    ("different-model-online", _post_model_online, False, True),
    ("score-it-anyway", _post_anyway, False, False),
    ("score-again-naming-row", _post_rescore_naming_row, False, False),
    ("banner-fix-local", _post_group_local, True, False),
    ("banner-fix-online", _post_group_online, True, True),
]


def _rows(tier: int):
    error = fk.tag("schema validation failed: verdict required", fk.UNREADABLE)
    return [(f"Role {i}", "Larkspur Logistics", tier, error, '{"fit": "x"', 2,
             timedelta(minutes=10 + i)) for i in range(3)]


def _set_stage(ids, stage: str) -> None:
    async def f(s):
        opp_ids = [ids[f"Role {i}"]["opp"] for i in range(3)]
        await s.execute(update(Opportunity).where(Opportunity.id.in_(opp_ids)).values(stage=stage))

    _write(f)


def _attempts() -> dict[int, int]:
    async def f(s):
        rows = (await s.execute(select(Quarantine))).scalars()
        return {q.opportunity_id: q.attempts for q in rows}

    return _read(f)


@pytest.mark.parametrize("entry", ENTRY_POINTS, ids=[e[0] for e in ENTRY_POINTS])
@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_every_retry_runs_the_failed_rows_own_step_or_nothing(make, monkeypatch, case, entry):  # noqa: F811
    _name, tier, stage, expected = case
    _entry, post, whole_group, online = entry
    client, ids = make(_rows(tier))
    _set_stage(ids, stage)
    if online:
        _online(monkeypatch)
    key = retry_groups.group_key(STEP_KEY[tier], fk.UNREADABLE, "kestrel-tier2")
    targets = ({ids[f"Role {i}"]["opp"] for i in range(3)} if whole_group
               else {ids["Role 0"]["opp"]})
    before = _attempts()
    post(client, ids, key)
    tasks = _tasks()
    after = _attempts()
    # A refusal (expected None) queues nothing and keeps every row's tries; otherwise exactly the
    # targets are queued as the row's own step, and each spends one try.
    ran = targets if expected is not None else set()
    queued = {p["opportunity_id"]: t for t, p in tasks}
    assert len(tasks) == len(ran), tasks
    assert queued == {oid: expected for oid in ran}, queued
    assert after == {oid: before[oid] + (1 if oid in ran else 0) for oid in before}
