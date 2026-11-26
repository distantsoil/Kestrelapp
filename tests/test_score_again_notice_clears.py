"""Issue #253: the Today and Profile notice "N roles were scored before your proof points" goes
away. A role whose Score again already ran since the proof points changed (and wrote no new score:
a posting with no text is skipped, a failed attempt keeps the old score) is not offered again, so
pressing Score again cannot bring the same notice back forever. Dismiss hides the notice until the
proof points change again. Invented data only; no network."""

from __future__ import annotations

import asyncio

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
from kestrel.db.models import Evaluation, Opportunity, Setting, Task
from kestrel.services import profile_edit as pe
from kestrel.services import profile_readiness as pr

pytestmark = pytest.mark.usefixtures("example_operator_profile")

OLD = "2020-01-01T00:00:00Z"
STAMP = "2021-06-01T00:00:00Z"
AFTER = "2021-06-02T00:00:00Z"
BEFORE = "2021-05-01T00:00:00Z"
NOTICE = 'data-next-step="score_again"'


@pytest.fixture
def fresh():
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()
    pe.profile_path().unlink(missing_ok=True)
    pe.profile_path().write_text(yaml.safe_dump({"employment": {
        "first_name": "Sam",
        "proof_points": [{"key": "p1", "claim": "Cut churn 20%", "evidence": "Q3",
                          "metric": None}],
        "employment_terms": {"target_titles": ["Operations lead"]},
    }}), "utf-8")
    yield
    asyncio.run(reset_engine())


def _seed(tasks: dict[str, tuple[str, str] | None], *, stamp: str = STAMP) -> dict[str, int]:
    """One scored role per name, scored before `stamp`; `tasks[name]` is (status, created_at) of
    a Score again task for it, or None."""
    async def go():
        ids = {}
        async with get_sessionmaker()() as s:
            for name, task in tasks.items():
                o = Opportunity(track="employment", opp_type="job_posting", title=name,
                                stage="t2_done", seen_count=1, description_md="Run the floor.")
                s.add(o)
                await s.flush()
                s.add(Evaluation(opportunity_id=o.id, tier=2, track="employment",
                                 created_at=OLD))
                if task is not None:
                    status, at = task
                    payload = {"opportunity_id": o.id, "rescore": True}
                    if status == "auto":  # an automatic task (a changed posting), not Score again
                        status, payload = "done", {"opportunity_id": o.id, "changed": True}
                    s.add(Task(type="t2", status=status, created_at=at, updated_at=at,
                               payload=payload))
                ids[name] = o.id
            s.add(Setting(key=pr.KEY, value={"employment": stamp}))
            await s.commit()
        return ids
    return asyncio.run(go())


def _readiness():
    async def go():
        async with get_sessionmaker()() as s:
            return await pr.load_readiness(s, "employment")
    return asyncio.run(go())


def _set_stamp(stamp: str) -> None:
    async def go():
        async with get_sessionmaker()() as s:
            row = await s.get(Setting, pr.KEY)
            row.value = {"employment": stamp}
            await s.commit()
    asyncio.run(go())


def _task_count() -> int:
    async def go():
        async with get_sessionmaker()() as s:
            return (await s.execute(select(func.count()).select_from(Task))).scalar_one()
    return asyncio.run(go())


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def test_a_role_whose_score_again_already_ran_is_not_offered_again(fresh):
    ids = _seed({
        "never": None,
        "done_after": ("done", AFTER),
        "dead_after": ("dead", AFTER),
        "done_before": ("done", BEFORE),
        "auto_after": ("auto", AFTER),
    })
    r = _readiness()
    assert r.next_step == pr.SCORE_AGAIN
    # A score again that ran before the proof points changed does not count; one that ran
    # after them (finished or given up) does, so the role is not queued again. An automatic
    # task never counts: it may have skipped without reading the new proof points.
    assert sorted(r.role_ids) == sorted([ids["never"], ids["done_before"], ids["auto_after"]])
    assert r.roles_scored_before_proof_points == 3


def test_the_notice_clears_once_every_role_has_had_its_score_again(fresh):
    _seed({"a": ("done", AFTER), "b": ("dead", AFTER)})
    r = _readiness()
    assert r.next_step == pr.READY and not r.show_notice
    with _client() as c:
        assert NOTICE not in c.get("/").text


def test_the_notice_offers_dismiss(fresh):
    _seed({"a": None})
    with _client() as c:
        for url in ("/", "/profile"):
            html = c.get(url).text
            assert NOTICE in html, url
            assert 'data-testid="score-again-dismiss"' in html, url
            assert 'action="/api/profile/score-again/dismiss"' in html, url


def test_dismiss_hides_the_notice_until_the_proof_points_change_again(fresh):
    _seed({"a": None, "b": None})
    with _client() as c:
        assert NOTICE in c.get("/").text
        r = c.post("/api/profile/score-again/dismiss",
                   data={"track": "employment", "back": "today", "stamp": STAMP},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/?track=employment"
        assert NOTICE not in c.get("/").text
        assert NOTICE not in c.get("/profile").text
        # Nothing was queued by a dismiss; the roles are still counted, only the notice hides.
        assert _task_count() == 0
        r = _readiness()
        assert r.next_step == pr.READY and r.roles_scored_before_proof_points == 2
    _set_stamp("2022-01-01T00:00:00Z")  # proof points changed again
    with _client() as c:
        assert NOTICE in c.get("/").text


def test_dismiss_in_place_answers_with_an_empty_notice(fresh):
    _seed({"a": None})
    with _client() as c:
        r = c.post("/api/profile/score-again/dismiss",
                   data={"track": "employment", "back": "today", "stamp": STAMP},
                   headers={"HX-Request": "true"})
        assert r.status_code == 200
        assert NOTICE not in r.text and "score-again-go" not in r.text


def test_dismiss_keeps_each_track_apart_and_refuses_a_forged_track(fresh):
    _seed({"a": None})
    with _client() as c:
        r = c.post("/api/profile/score-again/dismiss",
                   data={"track": "nonsense", "back": "//evil.example", "stamp": STAMP},
                   follow_redirects=False)
        assert r.headers["location"] == "/?track=employment"

    async def stored():
        async with get_sessionmaker()() as s:
            row = await s.get(Setting, pr.DISMISSED_KEY)
            return row.value if row else None
    # A forged track falls back to employment, as Score again does; only that track is stored.
    assert asyncio.run(stored()) == {"employment": STAMP}


# ------------------------------------------------- review round 1 (Codex): prune and old stamps
NEWER = "2022-01-01T00:00:00Z"


def test_a_pressed_score_again_still_counts_after_prune_removes_its_tasks(fresh):
    from sqlalchemy import update

    from kestrel.services.prune import prune

    _seed({"a": None, "b": None})
    with _client() as c:
        r = c.post("/api/profile/score-again", data={"track": "employment", "back": "today"},
                   follow_redirects=False)
        assert r.headers["location"] == "/?track=employment&rescoring=2"

    async def ran_then_pruned():
        sm = get_sessionmaker()
        async with sm() as s:  # both ran and wrote no new score (a skipped stub, a failure)
            await s.execute(update(Task).values(status="done", updated_at=OLD))
            await s.commit()
        await prune(sm)
    asyncio.run(ran_then_pruned())
    assert _task_count() == 0  # the task rows that said "already tried" are gone
    r = _readiness()
    assert r.next_step == pr.READY and r.role_ids == [], r
    with _client() as c:
        assert NOTICE not in c.get("/").text


def test_a_new_proof_points_change_offers_the_tried_roles_again(fresh):
    _seed({"a": None})
    with _client() as c:
        c.post("/api/profile/score-again", data={"track": "employment", "back": "today"},
               follow_redirects=False)
    _set_stamp(NEWER)  # the press was for the old stamp; the role is stale again for this one
    async def finish():
        async with get_sessionmaker()() as s:
            for t in (await s.execute(select(Task))).scalars():
                await s.delete(t)
            await s.commit()
    asyncio.run(finish())
    r = _readiness()
    assert r.next_step == pr.SCORE_AGAIN and len(r.role_ids) == 1, r


def test_the_notice_carries_the_stamp_it_was_shown_for(fresh):
    _seed({"a": None})
    with _client() as c:
        for url in ("/", "/profile"):
            assert f'name="stamp" value="{STAMP}"' in c.get(url).text, url


def test_a_dismiss_pressed_on_an_older_stamp_does_not_hide_a_newer_one(fresh):
    _seed({"a": None})
    _set_stamp(NEWER)  # proof points changed again after the page showed STAMP
    with _client() as c:
        c.post("/api/profile/score-again/dismiss",
               data={"track": "employment", "back": "today", "stamp": STAMP},
               follow_redirects=False)
        assert NOTICE in c.get("/").text


def test_a_dismiss_with_no_stamp_hides_nothing(fresh):
    _seed({"a": None})
    with _client() as c:
        c.post("/api/profile/score-again/dismiss",
               data={"track": "employment", "back": "today"}, follow_redirects=False)
        assert NOTICE in c.get("/").text


# ------------------------------------------------- review round 2 (Codex): any Score again path
def test_a_score_again_from_anywhere_still_counts_after_prune(fresh):
    """The role page, Pipeline, Needs another try and the notice all queue through
    queue.rescore.enqueue_rescore; a Score again from any of them is remembered past prune."""
    from sqlalchemy import update

    from kestrel.queue.rescore import enqueue_rescore
    from kestrel.services.prune import prune

    ids = _seed({"a": None, "b": None})

    async def queue_ran_pruned():
        sm = get_sessionmaker()
        async with sm() as s:  # e.g. the role page's Score again, not the notice
            await enqueue_rescore(s, [ids["a"], ids["b"]])
            await s.commit()
        async with sm() as s:  # both ran and wrote no new score
            await s.execute(update(Task).values(status="done", updated_at=OLD))
            await s.commit()
        await prune(sm)
    asyncio.run(queue_ran_pruned())
    assert _task_count() == 0
    r = _readiness()
    assert r.next_step == pr.READY and r.role_ids == [], r


# ------------------------------------------ review round 3 (Codex): bookkeeping never aborts
def _store_tried(value) -> None:
    async def go():
        async with get_sessionmaker()() as s:
            s.add(Setting(key=pr.TRIED_KEY, value=value))
            await s.commit()
    asyncio.run(go())


def test_a_malformed_tried_record_never_aborts_an_enqueue(fresh):
    from kestrel.queue.rescore import enqueue_rescore

    ids = _seed({"a": None, "b": None})
    _store_tried({"employment": {"stamp": STAMP, "ids": 42}})  # valid JSON, wrong shape

    async def queue():
        async with get_sessionmaker()() as s:
            result = await enqueue_rescore(s, [ids["a"], ids["b"]])
            await s.commit()
            return result
    result = asyncio.run(queue())
    assert sorted(result.queued) == sorted([ids["a"], ids["b"]])
    assert _task_count() == 2  # both tasks were kept


def test_a_malformed_tried_record_reads_as_empty_and_the_pages_load(fresh):
    _seed({"a": None})
    for bad in ({"employment": {"stamp": STAMP, "ids": 42}},
                {"employment": {"stamp": STAMP, "ids": ["x", None, 3.5]}},
                {"employment": "not a record"}, ["a", "list"]):
        async def reset():
            async with get_sessionmaker()() as s:
                row = await s.get(Setting, pr.TRIED_KEY)
                if row is not None:
                    await s.delete(row)
                    await s.commit()
        asyncio.run(reset())
        _store_tried(bad)
        r = _readiness()
        assert r.next_step == pr.SCORE_AGAIN and len(r.role_ids) == 1, (bad, r)
        with _client() as c:
            assert c.get("/").status_code == 200, bad



def test_a_failing_tried_write_never_aborts_an_enqueue(fresh, monkeypatch):
    from kestrel.queue.rescore import enqueue_rescore

    ids = _seed({"a": None})

    async def boom(*_a, **_k):
        raise TypeError("an unexpected stored shape")
    monkeypatch.setattr(pr, "record_tried", boom)

    async def queue():
        async with get_sessionmaker()() as s:
            result = await enqueue_rescore(s, [ids["a"]])
            await s.commit()
            return result
    assert asyncio.run(queue()).queued == [ids["a"]]
    assert _task_count() == 1
