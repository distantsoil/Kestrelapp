"""The Local / Hosted switch and the per-run choice (SPEC.md 6.4, D-HOSTED-2).

  1. Resolution: mode "hosted" overlays only the tiers present in llm.hosted_tiers, keeping the
     local tier's other fields; embed and any unassigned tier stay local; "local" leaves tiers
     untouched.
  2. Refusal: an explicit "hosted" without the acknowledgement (or without tier1 and tier2
     assigned) raises HostedModeError with the reason; a STORED "hosted" that cannot run falls
     back to local instead of breaking every settings load.
  3. Legacy rows: `llm.tiers.tier1.backend=frontier` still routes, because the overlay is
     additive and the client gate is unchanged.
  4. Worker.run(mode=...) resolves the tier set for the whole run (the fake records which model
     answered), updates the client too, and writes the mode on the Run row.
  5. Run now: POST /api/run/now and the Runs page form queue a one-shot mode that is consumed
     exactly once; "hosted" is refused with the reason until it can run; /api/status and the
     strip show it.
  6. CLI: --hosted / --local are mutually exclusive, --hosted is refused with the reason, --local
     runs and prints the mode.
No network, no model: a fake client and respx-free routes."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from kestrel import config as config_mod
from kestrel.config import (
    HOSTED_MAX_TOKENS,
    HostedMode,
    HostedModeError,
    get_settings,
    hosted_mode_problem,
    load_settings,
)
from kestrel.db.models import Evaluation, Opportunity, Run, Setting, Source
from kestrel.llm import providers as prov
from kestrel.pipeline.worker import Worker
from kestrel.services.setting_overrides import (
    PENDING_RUN_MODE_KEY,
    load_settings_with_db,
    peek_run_mode,
    queue_run_mode,
    take_run_mode,
)
from tests.pipeline_fakes import FakeLLM

_ACK = "2026-10-12T10:00:00+00:00"
_HOSTED_T1 = "qwen/qwen3-8b:free"
_HOSTED_T2 = "anthropic/claude-sonnet-4.6"
def _recent() -> str:
    return (datetime.now(UTC) - timedelta(days=3)).date().isoformat()


def _rows(*, ack: bool = True, enabled: bool = True, tiers=("tier1", "tier2"), mode=None) -> dict:
    """Flat setting rows the way Settings > Hosted and `kestrel setup --hosted` write them."""
    rows: dict = {"llm.hosted_mode.enabled": enabled}
    if ack:
        rows["llm.hosted_mode.acknowledged_at"] = _ACK
        rows["llm.hosted_mode.acknowledged_terms"] = HostedMode().disclosure_terms()
    models = {"tier1": _HOSTED_T1, "tier2": _HOSTED_T2, "docsel": _HOSTED_T1}
    for t in tiers:
        rows[f"llm.hosted_tiers.{t}.backend"] = "frontier"
        rows[f"llm.hosted_tiers.{t}.model"] = models[t]
    if mode:
        rows["llm.mode"] = mode
    return rows


def _nested(flat: dict) -> dict:
    from kestrel.services.setting_overrides import nest_overrides

    return nest_overrides(flat)


# ------------------------------------------------------------------------------ 1. resolution
def test_hosted_mode_overlays_only_assigned_tiers_and_keeps_local_fields():
    local = get_settings()
    s = load_settings(db_overrides=_nested(_rows()), mode="hosted")
    assert s.llm.mode == "hosted"
    assert s.llm.tiers["tier1"].backend == "frontier" and s.llm.tiers["tier1"].model == _HOSTED_T1
    assert s.llm.tiers["tier2"].backend == "frontier" and s.llm.tiers["tier2"].model == _HOSTED_T2
    # the local tier's temperature and timeout survive the overlay; max_tokens takes the hosted
    # default when the hosted entry sets none (D-LLM-1), and the local set is untouched
    assert s.llm.tiers["tier2"].max_tokens == HOSTED_MAX_TOKENS["tier2"]
    assert local.llm.tiers["tier2"].max_tokens == 4000
    assert s.llm.tiers["tier2"].timeout_s == local.llm.tiers["tier2"].timeout_s
    assert s.llm.tiers["tier1"].temperature == local.llm.tiers["tier1"].temperature
    # unassigned tiers stay local: embed always, docsel/tier3/trigger here
    for tier in ("embed", "docsel", "tier3", "trigger"):
        assert s.llm.tiers[tier] == local.llm.tiers[tier], tier
    # the hosted set is still readable on its own
    assert set(s.llm.hosted_tiers) == {"tier1", "tier2"}
    # the client gate sees an active hosted mode
    rows = {r.tier: r for r in prov.tier_status(s)}
    assert rows["tier1"].external and rows["tier1"].in_use
    assert not rows["docsel"].external


def test_local_mode_leaves_tiers_untouched_whatever_is_assigned():
    local = get_settings()
    s = load_settings(db_overrides=_nested(_rows(mode="hosted")), mode="local")
    assert s.llm.mode == "local"
    assert s.llm.tiers == local.llm.tiers
    assert s.llm.hosted_tiers["tier1"].model == _HOSTED_T1  # kept, not used
    stored = load_settings(db_overrides=_nested(_rows(mode="hosted")))
    assert stored.llm.mode == "hosted" and stored.llm.tiers["tier1"].backend == "frontier"
    default = load_settings(db_overrides=_nested(_rows()))
    assert default.llm.mode == "local" and default.llm.tiers["tier1"].backend == "local"


def test_assignment_status_reads_the_hosted_set_not_the_effective_tiers():
    s = load_settings(db_overrides=_nested(_rows(tiers=("tier1",))))
    rows = {r.tier: r for r in prov.assignment_status(s)}
    assert set(rows) == {"tier1", "tier2", "tier3", "docsel", "trigger", "frontier"}
    assert rows["tier1"].external and rows["tier1"].assigned and not rows["tier1"].in_use
    assert not rows["tier2"].assigned and not rows["tier2"].external
    assert rows["frontier"].external and rows["frontier"].in_use  # hosted in either mode
    hosted = load_settings(
        db_overrides=_nested(_rows(tiers=("tier1", "tier2"), mode="hosted"))
    )
    assert all(r.in_use for r in prov.assignment_status(hosted) if r.external)


def test_unknown_mode_is_refused_by_name():
    with pytest.raises(ValueError, match="unknown run mode"):
        load_settings(mode="cloud")


# --------------------------------------------------------------------------------- 2. refusal
def test_explicit_hosted_is_refused_with_the_reason():
    with pytest.raises(HostedModeError, match="not been acknowledged"):
        load_settings(db_overrides=_nested(_rows(ack=False)), mode="hosted")
    with pytest.raises(HostedModeError, match="turned off"):
        load_settings(db_overrides=_nested(_rows(enabled=False)), mode="hosted")
    with pytest.raises(HostedModeError, match="tier2"):
        load_settings(db_overrides=_nested(_rows(tiers=("tier1",))), mode="hosted")
    with pytest.raises(HostedModeError, match="tier1 and tier2"):
        load_settings(db_overrides=_nested(_rows(tiers=())), mode="hosted")
    assert hosted_mode_problem(load_settings(db_overrides=_nested(_rows()))) is None


def test_stored_hosted_that_cannot_run_falls_back_to_local():
    """A row saying hosted after the assignment went away must not break every settings load;
    the effective mode is local and hosted_mode_problem says why."""
    s = load_settings(db_overrides=_nested(_rows(ack=False, mode="hosted")))
    assert s.llm.mode == "local" and s.llm.tiers["tier1"].backend == "local"
    assert "acknowledged" in (hosted_mode_problem(s) or "")
    s = load_settings(db_overrides=_nested(_rows(tiers=("tier1",), mode="hosted")))
    assert s.llm.mode == "local" and s.llm.tiers["tier1"].backend == "local"


# ----------------------------------------------------------------------------- 3. legacy rows
def test_legacy_tier_rows_still_route():
    """Rows written before D-HOSTED-2 named the effective tier directly; they keep working in
    either mode because the overlay only adds, never resets, and the gate is unchanged."""
    legacy = {
        "llm.hosted_mode.enabled": True,
        "llm.hosted_mode.acknowledged_at": _ACK,
        "llm.hosted_mode.acknowledged_terms": HostedMode().disclosure_terms(),
        "llm.tiers.tier1.backend": "frontier",
        "llm.tiers.tier1.model": _HOSTED_T1,
    }
    s = load_settings(db_overrides=_nested(legacy))
    assert s.llm.mode == "local" and s.llm.hosted_tiers == {}
    assert s.llm.tiers["tier1"].backend == "frontier" and s.llm.tiers["tier1"].model == _HOSTED_T1
    assert {r.tier for r in prov.tier_status(s) if r.in_use} == {"tier1", "frontier"}


# -------------------------------------------------------------------------------- 4. the worker
async def _seed_rows(sm, rows: dict) -> None:
    async with sm() as s:
        for k, v in rows.items():
            s.add(Setting(key=k, value=v))
        await s.commit()


async def _seed_opps(sm, n: int = 3) -> None:
    async with sm() as s:
        src = Source(track="employment", kind="greenhouse", name="emp", enabled=True)
        s.add(src)
        await s.flush()
        for i in range(n):
            s.add(
                Opportunity(
                    track="employment", opp_type="job_posting", source_id=src.id,
                    title=f"VP of IT #{i}", comp_max=250000, comp_source="parsed",
                    remote_type="hybrid", state="OH", city="Columbus", location_raw="Columbus, OH",
                    date_posted=_recent(), seen_count=1, ghost_score=0.1, stage="new",
                    description_md="Own IT leadership, budget and vendor accountability.",
                )
            )
        await s.commit()


async def _models_by_tier(sm) -> dict[int, set[str]]:
    async with sm() as s:
        evals = (await s.execute(select(Evaluation))).scalars().all()
    out: dict[int, set[str]] = {}
    for e in evals:
        out.setdefault(e.tier, set()).add(e.model or "")
    return out


async def test_worker_run_hosted_uses_the_hosted_models_and_records_the_mode(sm):
    await _seed_rows(sm, _rows())
    await _seed_opps(sm)
    settings = get_settings()  # YAML only: local tiers, no acknowledgement in this object
    fake = FakeLLM(settings)
    worker = Worker(sm, fake, settings)
    stats = await worker.run(mode="hosted")
    assert stats.tier1.completed == 3 and stats.tier2.completed == 3
    # the client was updated too, not only the worker's copy
    assert fake.settings is worker._settings and fake.settings.llm.mode == "hosted"
    models = await _models_by_tier(sm)
    assert models[1] == {_HOSTED_T1} and models[2] == {_HOSTED_T2}
    async with sm() as s:
        run = (await s.execute(select(Run).order_by(Run.id.desc()))).scalars().first()
    assert run.status == "done" and run.mode == "hosted"


async def test_worker_run_default_records_the_settings_mode_and_local_models(sm):
    await _seed_rows(sm, _rows())  # assigned but the stored mode is local
    await _seed_opps(sm, n=2)
    settings = get_settings()
    local_t1 = settings.llm.tiers["tier1"].model
    await Worker(sm, FakeLLM(settings), settings).run()
    models = await _models_by_tier(sm)
    assert models[1] == {local_t1}
    async with sm() as s:
        run = (await s.execute(select(Run).order_by(Run.id.desc()))).scalars().first()
    assert run.mode == "local"
    # an explicit local run against a stored hosted mode is local for that run only
    await _seed_rows(sm, {"llm.mode": "hosted"})
    await _seed_opps(sm, n=1)
    await Worker(sm, FakeLLM(settings), settings).run(mode="local")
    async with sm() as s:
        run = (await s.execute(select(Run).order_by(Run.id.desc()))).scalars().first()
        assert run.mode == "local"
        assert (await load_settings_with_db(s)).llm.mode == "hosted"  # the row is untouched


async def test_worker_run_hosted_is_refused_before_any_run_row(sm):
    await _seed_rows(sm, _rows(ack=False))
    settings = get_settings()
    with pytest.raises(HostedModeError, match="acknowledged"):
        await Worker(sm, FakeLLM(settings), settings).run(mode="hosted")
    async with sm() as s:
        assert (await s.execute(select(Run))).scalars().all() == []


# ---------------------------------------------------------------------------------- 5. run now
async def test_queued_mode_is_consumed_exactly_once(sm):
    await _seed_rows(sm, _rows())
    async with sm() as s:
        assert await peek_run_mode(s) is None and await take_run_mode(s) is None
        await queue_run_mode(s, "hosted")
        await s.commit()
    async with sm() as s:
        assert await peek_run_mode(s) == "hosted"
        # the queue row is never part of the config layer
        assert (await load_settings_with_db(s)).llm.mode == "local"
    async with sm() as s:
        assert await take_run_mode(s) == "hosted"
    async with sm() as s:
        assert await take_run_mode(s) is None
        assert await s.get(Setting, PENDING_RUN_MODE_KEY) is None
    # consumed with the mode it names: the cycle's settings carry the overlay
    async with sm() as s:
        await queue_run_mode(s, "hosted")
        await s.commit()
    async with sm() as s:
        settings = await load_settings_with_db(s, mode=await take_run_mode(s))
        assert settings.llm.mode == "hosted" and settings.llm.tiers["tier1"].backend == "frontier"


async def test_queue_survives_the_loop_consuming_the_row_mid_request(sm):
    """The race: a request loads the queue row, the serve loop's take_run_mode deletes it, then
    the request writes. A load-then-set would UPDATE a vanished row (StaleDataError at commit,
    a 500); the upsert lands the new value either way and the stale copy is refreshed."""
    async with sm() as s:
        await queue_run_mode(s, "local")
        await s.commit()
    async with sm() as s:
        loaded = await s.get(Setting, PENDING_RUN_MODE_KEY)  # in this session's identity map
        assert loaded is not None and loaded.value == "local"
        async with sm() as other:  # the loop consumes it underneath
            assert await take_run_mode(other) == "local"
        await queue_run_mode(s, "local")
        await s.commit()  # no StaleDataError
        assert await peek_run_mode(s) == "local"
    async with sm() as s:
        assert await take_run_mode(s) == "local"
        assert await take_run_mode(s) is None


async def test_queue_refuses_hosted_that_cannot_run_and_unknown_modes(sm):
    await _seed_rows(sm, _rows(ack=False))
    async with sm() as s:
        with pytest.raises(HostedModeError, match="acknowledged"):
            await queue_run_mode(s, "hosted")
        with pytest.raises(ValueError, match="unknown run mode"):
            await queue_run_mode(s, "cloud")
        await queue_run_mode(s, "local")
        await s.commit()
    async with sm() as s:
        assert await take_run_mode(s) == "local"


def _fresh_app():
    from starlette.testclient import TestClient

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.engine import reset_engine
    from kestrel.web.app import build_app

    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations(paths.db_path())
    asyncio.run(reset_engine())
    return TestClient(build_app(worker=False))


def _app_db(fn):
    async def _go():
        from kestrel.db.engine import get_sessionmaker

        async with get_sessionmaker()() as s:
            out = await fn(s)
            await s.commit()
            return out

    return asyncio.run(_go())


def test_run_now_routes_queue_a_one_shot_mode(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()

    # nothing queued, local by default, hosted unavailable with the reason
    st = client.get("/api/status").json()
    assert st["mode"] == "local" and st["pending_mode"] is None
    strip = client.get("/runs/strip").text
    assert "idle · local models" in strip and "next cycle" not in strip
    sheet = client.get("/runs/start/choose").text  # "This run only" (D-UI-103)
    assert 'name="mode" value="hosted"' in sheet and "Not available: " in sheet
    assert 'value="hosted" disabled' in sheet
    assert "—" not in sheet and "—" not in client.get("/runs").text  # no em dashes in the copy

    # hosted cannot be queued yet; nothing is written
    r = client.post("/api/run/now?mode=hosted&confirmed=1")
    assert r.status_code == 400 and "acknowledged" in r.json()["reason"]
    assert client.get("/api/status").json()["pending_mode"] is None
    r = client.post("/api/run/now?mode=cloud")
    assert r.status_code == 400 and "unknown mode" in r.json()["reason"]
    r = client.post("/runs/now", data={"mode": "hosted", "confirmed": "1"})
    assert "Not queued" in r.text and "acknowledged" in r.text

    # default mode queues local
    r = client.post("/api/run/now")
    assert r.status_code == 200 and r.json() == {"queued": True, "mode": "local"}
    assert client.get("/api/status").json()["pending_mode"] == "local"
    assert "next cycle: local" in client.get("/runs/strip").text

    # with the acknowledgement and both assignments, hosted queues (replacing the local request)
    async def seed(s):
        for k, v in _rows().items():
            s.add(Setting(key=k, value=v))

    _app_db(seed)

    async def approval(s):
        from kestrel.services.online_consent import approval_for

        return approval_for(await load_settings_with_db(s, mode="hosted"), ("tier1", "tier2"))

    shown = json.dumps(_app_db(approval))
    # A confirmation without the destinations it showed, or naming others, queues nothing.
    r = client.post("/runs/now", data={"mode": "hosted", "confirmed": "1"})
    assert "Queued" not in r.text and "changed since the dialog opened" in r.text
    stale = json.dumps({"tier1": ["frontier|https://elsewhere.example/v1|x"]})
    r = client.post("/runs/now", data={"mode": "hosted", "confirmed": "1",
                                       "online_approval": stale})
    assert "Queued" not in r.text
    r = client.post("/runs/now", data={"mode": "hosted", "confirmed": "1",
                                       "online_approval": shown})
    assert "Queued" in r.text and "<b>hosted</b>" in r.text
    assert client.get("/api/status").json()["pending_mode"] == "hosted"
    assert "next cycle: hosted" in client.get("/runs/strip").text
    assert "Next run only: <b>Online</b>" in client.get("/runs").text

    # the loop's consumer takes it once
    assert _app_db(take_run_mode) == "hosted"
    assert _app_db(take_run_mode) is None
    assert client.get("/api/status").json()["pending_mode"] is None

    # the Runs list shows the mode per run
    async def add_runs(s):
        s.add(Run(name="pipeline", status="done", mode="hosted", counts={}))
        s.add(Run(name="pipeline", status="done", mode=None, counts={}))

    _app_db(add_runs)
    page = client.get("/runs/history?all=1").text
    assert 'rn-md is-online' in page and ">Online</span>" in page  # a hosted run carries its tag
    assert page.count('data-testid="rn-run"') == 2  # a run with no recorded mode has no tag


# --------------------------------------------------------------------------------------- 6. CLI
def test_cli_flags_are_mutually_exclusive_and_hosted_is_refused_with_the_reason(monkeypatch):
    from kestrel.cli import app

    runner = CliRunner()
    for cmd in ("run", "scan"):
        r = runner.invoke(app, [cmd, "--hosted", "--local"])
        assert r.exit_code == 2, r.output
        assert "mutually exclusive" in r.output
    r = runner.invoke(app, ["run", "--hosted"])
    assert r.exit_code == 1, r.output
    assert "cannot run on hosted models: the Hosted mode disclosure has not been acknowledged" in (
        r.output
    )
    r = runner.invoke(app, ["scan", "--hosted", "--all"])
    assert r.exit_code == 1, r.output
    assert "cannot scan on hosted models" in r.output
    # --local on an empty database runs the pipeline on nothing and names the mode
    r = runner.invoke(app, ["run", "--local"])
    assert r.exit_code == 0, r.output
    assert "run mode: local" in r.output and "done." in r.output
    assert "—" not in r.output


def test_cli_mode_flag_helper():
    from kestrel.cli import _mode_flag

    assert _mode_flag(False, False) is None
    assert _mode_flag(True, False) == "hosted"
    assert _mode_flag(False, True) == "local"
    import typer

    with pytest.raises(typer.BadParameter):
        _mode_flag(True, True)


def test_modes_constant_matches_the_literal():
    assert config_mod.MODES == ("local", "hosted")
    assert config_mod.HOSTED_REQUIRED_TIERS == ("tier1", "tier2")


async def test_a_task_confirmed_for_another_destination_is_held_once_not_looped(sm):
    """D-UI-178: work stamped with an approval runs only on the destinations it names. A changed
    destination holds it (pending, reason kept) and the drain finishes instead of leasing the
    same held task again forever; unstamped work under the accepted disclosure still runs."""
    import asyncio as _asyncio

    from kestrel.db.models import Task
    from kestrel.pipeline._shared import HandlerOutcome
    from kestrel.pipeline.worker import TierStats

    await _seed_rows(sm, _rows(mode="hosted"))
    async with sm() as s:
        settings = await load_settings_with_db(s)
    worker = Worker(sm, FakeLLM(settings), settings)
    calls: list[int] = []

    async def handler(_sm, _client, _settings, *, opportunity_id, **_kw):
        calls.append(opportunity_id)
        return HandlerOutcome("done", 0)

    stale = {"tier1": ["frontier|https://elsewhere.example/v1|other"]}
    await worker._queue.enqueue("t1", {"opportunity_id": 1, "online_approval": stale})
    await worker._queue.enqueue("t1", {"opportunity_id": 2})
    await _asyncio.wait_for(worker.drain_tier("t1", 1, handler, TierStats()), timeout=20)
    async with sm() as s:
        rows = {t.payload["opportunity_id"]: t
                for t in (await s.execute(select(Task))).scalars().all()}
    assert calls == [2]
    assert rows[1].status == "pending" and "held" in rows[1].last_error
