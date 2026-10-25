"""Model check (D-DRIFT-11): the benchmark as a queued, resumable background task. Offline: a
scripted runner or a fake model client stands in for every model."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from kestrel.bench import check
from kestrel.bench.check import StepResult
from kestrel.config import load_settings
from kestrel.db.models import Label, Opportunity, Setting, Task
from tests.test_bench import (
    GOOD_MODEL,
    WRONG_MODEL,
    FakeBenchClient,
    _consulting_rows,
    _employment_rows,
    _write_goldset,
)


@pytest.fixture
def settings():
    s = load_settings()
    s.llm.tiers["tier1"] = s.llm.tiers["tier1"].model_copy(update={"model": "cur-quick"})
    s.llm.tiers["tier2"] = s.llm.tiers["tier2"].model_copy(update={"model": "cur-rate"})
    return s


async def _rate(sm, n: int) -> None:
    async with sm() as s:
        for i in range(n):
            o = Opportunity(track="employment", opp_type="posting", title=f"t{i}")
            s.add(o)
            await s.flush()
            s.add(Label(opportunity_id=o.id, track="employment", score=4.0, verdict="apply"))
        await s.commit()


def _res(step, model, agree, *, n=20, online=False, ms=100.0) -> StepResult:
    return StepResult(step, model, online, n, agree, ms, 1.0)


class Scripted:
    """A runner that answers from a table and records every call."""

    def __init__(self, table: dict, *, fail: set | None = None) -> None:
        self.table, self.calls, self.fail = table, [], fail or set()

    async def __call__(self, step, model, settings, backend=None):
        self.calls.append((step, model))
        if (step, model) in self.fail:
            raise RuntimeError("model went away")
        return self.table.get((step, model)) or _res(step, model, 0.50)


TABLE = {
    ("quick_screen", "cur-quick"): _res("quick_screen", "cur-quick", 0.80),
    ("quick_screen", "alt-quick"): _res("quick_screen", "alt-quick", 0.80),
    ("rating", "cur-rate"): _res("rating", "cur-rate", 0.60),
    ("rating", "alt-rate"): _res("rating", "alt-rate", 0.66, ms=200.0),
}


async def _queue(sm, **kw):
    async with sm() as s:
        return await check.queue_check(s, **kw)


async def _the_task(sm) -> Task:
    async with sm() as s:
        return (await s.execute(select(Task).where(Task.type == "bench"))).scalar_one()


# ---------------------------------------------------------------------------- queueing
async def test_too_few_ratings_refuses_and_queues_nothing(sm):
    await _rate(sm, 4)
    res = await _queue(sm)
    assert not res.queued and res.reason == "too_few_ratings" and "4" in res.message
    async with sm() as s:
        assert (await s.execute(select(Task))).scalars().all() == []


async def test_check_is_one_task_row_and_only_one_at_a_time(sm):
    await _rate(sm, 5)
    res = await _queue(sm)
    assert res.queued and res.task_id
    task = await _the_task(sm)
    assert task.type == "bench" and task.status == "pending"
    assert task.payload["steps"] == ["quick_screen", "rating"]
    again = await _queue(sm)
    assert not again.queued and again.reason == "already_running"


async def test_deeper_read_is_opt_in_and_bad_steps_are_refused(sm):
    await _rate(sm, 5)
    assert not (await _queue(sm, steps=["nonsense"])).queued
    assert (await _queue(sm, deeper=True)).queued
    assert (await _the_task(sm)).payload["steps"] == ["quick_screen", "rating", "deeper_read"]


# ---------------------------------------------------------------------------- the run
async def test_worker_runs_the_task_and_stores_a_recommendation(sm, settings):
    await _rate(sm, 5)
    await _queue(sm)
    runner = Scripted(TABLE)
    n = await check.drain_checks(
        sm, settings, make_client=None, runner=runner,
        installed=lambda s: ["cur-quick", "alt-quick", "alt-rate", "nomic-embed-text"],
    )
    assert n == 1
    # one tier drains before the next starts; the embedding model is never a candidate
    steps_seen = [c[0] for c in runner.calls]
    assert steps_seen == sorted(steps_seen, key=["quick_screen", "rating"].index)
    assert all("embed" not in m for _, m in runner.calls)
    task = await _the_task(sm)
    assert task.status == "done" and task.payload["progress"]["done"] == len(runner.calls)
    async with sm() as s:
        st = await check.status(s)
    latest = st["latest"]
    assert latest["summary"] == "Kestrel recommends one change. Everything else stays as it is."
    by = {v["step"]: v for v in latest["steps"]}
    assert not by["quick_screen"]["change"]
    assert by["rating"]["change"] and by["rating"]["points"] == 6
    assert "Switch Rating to alt-rate" in by["rating"]["verdict"]
    assert "6 more roles in every 100, from 20 roles in the answer key" in by["rating"]["verdict"]
    assert "a little slower" in by["rating"]["verdict"]
    assert "—" not in by["rating"]["verdict"]


async def test_resume_skips_finished_models(sm, settings):
    await _rate(sm, 5)
    await _queue(sm, models={"quick_screen": ["cur-quick"], "rating": ["cur-rate", "alt-rate"]})
    boom = Scripted(TABLE)
    task = await _the_task(sm)

    class Killed(BaseException):
        pass

    async def dies_on_rating_alt(step, model, s, backend=None):
        if (step, model) == ("rating", "alt-rate"):
            raise Killed
        return await boom(step, model, s, backend)

    with pytest.raises(Killed):
        await check.run_check(sm, task, settings, dies_on_rating_alt, [])
    mid = await _the_task(sm)
    assert [(r["step"], r["model"]) for r in mid.payload["results"]] == [
        ("quick_screen", "cur-quick"), ("rating", "cur-rate")]
    assert mid.payload["progress"]["total"] == 3 and mid.payload["progress"]["done"] == 2

    resumed = Scripted(TABLE)
    out = await check.run_check(sm, mid, settings, resumed, [])
    assert out.status == "done" and resumed.calls == [("rating", "alt-rate")]


async def test_killed_lease_is_reclaimed_and_resumed_by_the_queue(sm, settings):
    from kestrel.queue.queue import TaskQueue

    await _rate(sm, 5)
    await _queue(sm, models={"rating": ["cur-rate", "alt-rate"]}, steps=["rating"])
    q = TaskQueue(sm)
    leased = await q.lease("w1", types=["bench"], lease_seconds=300)
    assert leased is not None
    assert await q.release_all_leased() == 1  # a clean restart gives the task back
    runner = Scripted(TABLE)
    assert await check.drain_checks(sm, settings, make_client=None, runner=runner,
                                    installed=lambda s: []) == 1


async def test_cancel_stops_before_the_next_model(sm, settings):
    await _rate(sm, 5)
    await _queue(sm, steps=["rating"], models={"rating": ["cur-rate", "alt-rate"]})
    task = await _the_task(sm)

    class Cancelling(Scripted):
        async def __call__(self, step, model, s, backend=None):
            out = await super().__call__(step, model, s, backend)
            async with sm() as sess:
                assert await check.cancel_check(sess) == 1
            return out

    runner = Cancelling(TABLE)
    out = await check.run_check(sm, task, settings, runner, [])
    assert out.status == "cancelled" and runner.calls == [("rating", "cur-rate")]
    async with sm() as s:
        st = await check.status(s)
        assert st["state"] == "cancelled" and not st["live"]
        assert await check.list_checks(s) == []  # a cancelled check stores no result


async def test_a_model_that_fails_is_skipped_not_fatal(sm, settings):
    await _rate(sm, 5)
    await _queue(sm, steps=["rating"], models={"rating": ["cur-rate", "alt-rate"]})
    runner = Scripted(TABLE, fail={("rating", "alt-rate")})
    assert await check.drain_checks(sm, settings, make_client=None, runner=runner,
                                    installed=lambda s: []) == 1
    async with sm() as s:
        latest = (await check.list_checks(s))[0]
    assert latest["changes"] == 0 and latest["steps"][0]["verdict"].startswith("Keep cur-rate")


async def test_an_unexpected_error_retries_then_dead_letters_with_the_reason(sm, settings):
    await _rate(sm, 5)
    await _queue(sm, steps=["rating"])

    async def broken(step, model, s, backend=None):
        raise KeyError("bug")

    assert await check.drain_checks(sm, settings, make_client=None, runner=broken,
                                    installed=lambda s: []) == 0
    task = await _the_task(sm)
    assert task.status == "dead" and "KeyError" in task.last_error
    async with sm() as s:
        st = await check.status(s)
    assert st["state"] == "failed" and "KeyError" in st["error"]


async def test_history_keeps_the_latest_checks(sm, settings):
    await _rate(sm, 5)
    for _ in range(check.HISTORY_KEEP + 2):
        await _queue(sm, steps=["rating"], models={"rating": ["cur-rate"]})
        await check.drain_checks(sm, settings, make_client=None, runner=Scripted(TABLE),
                                 installed=lambda s: [])
    async with sm() as s:
        assert len(await check.list_checks(s)) == check.HISTORY_KEEP
        assert (await s.get(Setting, check.HISTORY_KEY)) is not None


# ---------------------------------------------------------------------------- recommendation
def test_local_is_preferred_unless_online_is_clearly_better():
    rows = [
        _res("rating", "cur", 0.50),
        _res("rating", "loc", 0.60),
        _res("rating", "web", 0.63, online=True),
    ]
    v = check.recommend_step("rating", rows, "cur")
    assert v.best["model"] == "loc" and v.points == 10  # 3 points better is not enough

    rows[2] = _res("rating", "web", 0.70, online=True)  # 10 over the best local option
    v = check.recommend_step("rating", rows, "cur")
    assert v.best["model"] == "web" and v.best_local["model"] == "loc"
    assert "uses your key" in v.verdict


def test_under_five_rated_roles_is_too_few_never_a_result():
    v = check.recommend_step("rating", [_res("rating", "cur", 0.9, n=4),
                                        _res("rating", "alt", 0.1, n=4)], "cur")
    assert not v.enough and not v.change and "Too few" in v.verdict
    assert check.build_summary([v])[0] == (
        "Too few roles in the answer key to check your models yet.")


def test_no_change_when_the_current_model_is_best():
    v = check.recommend_step("rating", [_res("rating", "cur", 0.7), _res("rating", "alt", 0.6)],
                             "cur")
    assert not v.change and v.verdict.startswith("Keep cur")
    assert check.build_summary([v]) == ("No changes recommended.", 0)


def test_candidates_are_current_first_without_embedding_models(settings):
    got = check.candidates_for(settings, "rating", ["a", "cur-rate", "b", "nomic-embed", "c", "d"],
                               online=[{"model": "web", "backend": "hosted"}])
    assert got[0] == ("cur-rate", None) and len(got) == 4
    assert all("embed" not in m for m, _ in got)


# ---------------------------------------------------------------------------- the real steps
async def test_default_runner_measures_both_steps_with_a_mocked_model(tmp_path, settings):
    _write_goldset(tmp_path, "employment", _employment_rows())
    _write_goldset(tmp_path, "consulting", _consulting_rows())

    class Client(FakeBenchClient):
        async def complete(self, *, tier, messages, schema_name="response", **kw):
            if tier == "tier1":
                from kestrel.llm.client import ChatResult

                text = "\n".join(m.get("content", "") for m in messages)
                keep = "[MARK:1.5:pass]" not in text and "[MARK:1.8:pass]" not in text
                body = {"verdict": "keep" if keep else "kill", "confidence": 0.9,
                        "reason": "r", "signals": []}
                return ChatResult(content=json.dumps(body), prompt_tokens=1, completion_tokens=1,
                                  latency_ms=10, model=self.settings.llm.tiers[tier].model)
            return await super().complete(tier=tier, messages=messages,
                                          schema_name=schema_name, **kw)

    run = check.default_runner(lambda s: Client(s), gold_dir=tmp_path)
    quick = await run("quick_screen", GOOD_MODEL, settings)
    assert quick.n == 4 and quick.agreement == 1.0 and not quick.online
    rating = await run("rating", GOOD_MODEL, settings)
    assert rating.n == 4 and rating.agreement == 1.0
    wrong = await run("rating", WRONG_MODEL, settings)
    assert wrong.agreement is not None and wrong.agreement < 1.0


# ---------------------------------------------------------------------------- the routes
def test_bench_run_queues_the_task_instead_of_the_old_no_model_call_notice(monkeypatch):
    from kestrel.web.pages import bench
    from tests.test_hosted_mode import _fresh_app

    monkeypatch.setattr(bench, "_available_models", lambda settings: ([], None))
    client = _fresh_app()
    r = client.post("/bench/run", data={"tier": "tier2"})
    assert r.status_code == 200
    # no ratings yet: refused with the reason, nothing queued, never a 500 or a CLI pointer
    assert "Rate at least 5 roles first" in r.text and "kestrel bench" not in r.text
    st = client.get("/bench/check/status").json()
    assert st["state"] == "idle" and st["live"] is False and st["history"] == []


def test_check_routes_queue_report_progress_and_cancel():
    import asyncio

    from kestrel.db.engine import get_sessionmaker
    from tests.test_hosted_mode import _fresh_app

    client = _fresh_app()

    async def rate():
        await _rate(get_sessionmaker(), 5)

    asyncio.run(rate())
    q = client.post("/bench/check", json={"steps": ["rating"]})
    assert q.status_code == 200 and q.json()["queued"] is True
    again = client.post("/bench/check", json={})
    assert again.status_code == 409 and again.json()["reason"] == "already_running"
    st = client.get("/bench/check/status").json()
    assert st["live"] is True and st["state"] == "waiting"
    assert client.post("/bench/check/cancel").json() == {"cancelled": 1}
    assert client.get("/bench/check/status").json()["state"] == "cancelled"


# ---------------------------------------------------------------------------- review fixes
def _with_hosted_backend(settings):
    s = settings.model_copy(deep=True)
    local = s.llm.backends["local"]
    s.llm.backends["hosted"] = local.model_copy(update={"external": True})
    return s


async def test_each_candidate_runs_on_its_own_backend_and_is_labeled_by_it(tmp_path, settings):
    """An online candidate is sent to its own backend, never the tier's local one."""
    settings = _with_hosted_backend(settings)
    _write_goldset(tmp_path, "employment", _employment_rows())
    _write_goldset(tmp_path, "consulting", _consulting_rows())
    hits: list[tuple[str, str, str]] = []

    class Client(FakeBenchClient):
        async def complete(self, *, tier, messages, **kw):
            tcfg = self.settings.llm.tiers[tier]
            hits.append((tier, tcfg.model, tcfg.backend))
            return await super().complete(tier=tier, messages=messages, **kw)

    run = check.default_runner(lambda s: Client(s), gold_dir=tmp_path)
    local = await run("rating", GOOD_MODEL, settings, None)
    web = await run("rating", "web-model", settings, "hosted")
    assert not local.online and local.backend == settings.llm.tiers["tier2"].backend
    assert web.online and web.backend == "hosted"
    assert {b for _, m, b in hits if m == "web-model"} == {"hosted"}
    assert {b for _, m, b in hits if m == GOOD_MODEL} == {settings.llm.tiers["tier2"].backend}
    with pytest.raises(ValueError):
        await run("rating", "x", settings, "nonexistent")


async def test_same_model_on_two_backends_are_two_results(sm, settings, monkeypatch):
    # The scripted drain below is about the two results, not the queue's online gate (which has
    # its own tests): the fake "hosted" backend has no key, so the gate is stepped over here.
    monkeypatch.setattr(check, "online_refusal", lambda *a, **k: None)
    settings = _with_hosted_backend(settings)
    await _rate(sm, 5)
    picked = {"rating": [{"model": "cur-rate", "backend": "hosted"}]}
    # The operator's dialog names the destination (D-UI-178); the drain re-checks it per candidate.
    approval = check.approval_for_candidates(settings, ["rating"], None, picked)
    assert approval, "the hosted candidate must need an approval for this test to mean anything"
    await _queue(sm, steps=["rating"], online=picked, settings=settings, online_approval=approval)
    seen = []

    async def runner(step, model, s, backend=None):
        seen.append((model, backend))
        online = backend == "hosted"
        return StepResult(step, model, online, 20, 0.9 if online else 0.5, 10.0, 1.0,
                          backend=backend or "local")

    await check.drain_checks(sm, settings, make_client=None, runner=runner,
                             installed=lambda s: [])
    assert seen == [("cur-rate", None), ("cur-rate", "hosted")]
    async with sm() as s:
        latest = (await check.list_checks(s))[0]
    assert latest["steps"][0]["best"]["backend"] == "hosted"
    assert latest["steps"][0]["current"]["backend"] == "local"


async def test_a_quarantined_quick_screen_counts_as_keep(tmp_path, settings):
    rows = _employment_rows()
    _write_goldset(tmp_path, "employment", rows)
    _write_goldset(tmp_path, "consulting", _consulting_rows())

    class Garbage(FakeBenchClient):
        async def complete(self, **kw):
            from kestrel.llm.client import ChatResult

            return ChatResult(content="not json", prompt_tokens=1, completion_tokens=1,
                              latency_ms=1, model="m")

    run = check.default_runner(lambda s: Garbage(s), gold_dir=tmp_path)
    out = await run("quick_screen", "m", settings, None)
    # every quarantined screen said "keep": right for the keep roles, wrong only for the passes
    keeps = sum(1 for r in rows + _consulting_rows() if r["gold_verdict"] != "pass")
    assert out.n == 4 and out.quarantined == 4 and out.agreement == keeps / 4


async def test_rating_agreement_is_over_every_role_not_only_parsed_ones(tmp_path, settings):
    from tests.test_bench import BAD_MODEL

    _write_goldset(tmp_path, "employment", _employment_rows())
    _write_goldset(tmp_path, "consulting", _consulting_rows())
    run = check.default_runner(lambda s: FakeBenchClient(s), gold_dir=tmp_path)
    bad = await run("rating", BAD_MODEL, settings, None)
    assert bad.n == 4 and bad.agreement == 0.0  # nothing parsed is 0 of 4, not "no data"


async def test_httpx_errors_record_no_result_and_the_check_continues(sm, settings):
    import httpx

    await _rate(sm, 5)
    await _queue(sm, steps=["rating"], models={"rating": ["cur-rate", "alt-rate"]})

    async def runner(step, model, s, backend=None):
        if model == "alt-rate":
            raise httpx.ConnectError("down")
        return _res(step, model, 0.5)

    assert await check.drain_checks(sm, settings, make_client=None, runner=runner,
                                    installed=lambda s: []) == 1
    async with sm() as s:
        assert len(await check.list_checks(s)) == 1


async def test_cancel_during_the_last_model_stays_cancelled_and_stores_nothing(sm, settings):
    await _rate(sm, 5)
    await _queue(sm, steps=["rating"], models={"rating": ["cur-rate"]})

    async def runner(step, model, s, backend=None):
        async with sm() as sess:
            await check.cancel_check(sess)
        return _res(step, model, 0.5)

    assert await check.drain_checks(sm, settings, make_client=None, runner=runner,
                                    installed=lambda s: []) == 0
    assert (await _the_task(sm)).status == "cancelled"
    async with sm() as s:
        assert await check.list_checks(s) == []


async def test_stop_gives_the_task_back_with_its_results_kept(sm, settings):
    import asyncio

    await _rate(sm, 5)
    await _queue(sm, steps=["rating"], models={"rating": ["cur-rate", "alt-rate"]})
    stop = asyncio.Event()

    async def runner(step, model, s, backend=None):
        stop.set()
        return _res(step, model, 0.5)

    await check.drain_checks(sm, settings, make_client=None, runner=runner, stop=stop,
                             installed=lambda s: [])
    task = await _the_task(sm)
    assert task.status == "pending" and len(task.payload["results"]) == 1


def test_the_check_is_a_drained_step_of_the_worker_not_a_parallel_task():
    from pathlib import Path

    src = Path(check.__file__).read_text(encoding="utf-8")
    app = (Path(check.__file__).parents[1] / "web" / "app.py").read_text(encoding="utf-8")
    assert "create_task(" not in src.split("async def drain_checks")[1].split("beat =")[0]
    assert "kick_checks" not in src and "kick_checks" not in app
    assert "await drain_checks(" in app


async def test_check_reports_both_counts_separately(sm, settings):
    await _rate(sm, 7)
    await _queue(sm, steps=["rating"], models={"rating": ["cur-rate"]})
    await check.drain_checks(sm, settings, make_client=None, runner=Scripted(TABLE),
                             installed=lambda s: [])
    async with sm() as s:
        latest = (await check.list_checks(s))[0]
    assert latest["rated_roles"] == 7 and latest["answer_key_roles"] == 20


async def test_bench_run_form_honors_the_picked_tier_and_model(sm, monkeypatch):
    from kestrel.web.pages import bench

    await _rate(sm, 5)
    async with sm() as s:
        args = await bench._form_check_args(s, "tier2", "other-model", "local")
        assert args["steps"] == ["rating"]
        assert args["models"]["rating"][1] == {"model": "other-model", "backend": "local"}
        assert await bench._form_check_args(s, "", "", "") == {}


async def test_a_late_cancel_is_not_overwritten(sm, settings, monkeypatch):
    await _rate(sm, 5)
    await _queue(sm, steps=["rating"], models={"rating": ["cur-rate"]})
    real = check.rated_count

    async def cancel_then_count(session):
        async with sm() as other:
            await check.cancel_check(other)  # lands after the last model, before the commit
        return await real(session)

    monkeypatch.setattr(check, "rated_count", cancel_then_count)
    task = await _the_task(sm)
    out = await check.run_check(sm, task, settings, Scripted(TABLE), [])
    assert out.status == "cancelled"
    assert (await _the_task(sm)).status == "cancelled"
    async with sm() as s:
        assert await check.list_checks(s) == []


async def test_a_hosted_pick_with_no_provider_is_refused_not_run_locally(sm, monkeypatch):
    from kestrel.web.pages import bench

    monkeypatch.setattr(bench.hm, "catalog_backend", lambda settings: None)

    await _rate(sm, 5)
    async with sm() as s:
        args = await bench._form_check_args(s, "tier2", "hosted:some/model", "local")
    assert "Connect an online provider" in args["error"] and "steps" not in args
