"""Final Lane C regressions: real routes and queues, isolated data, no online sends."""

from __future__ import annotations

import json

import pytest
from bs4 import BeautifulSoup

from kestrel.db.models import Opportunity, Setting, Source, Task
from kestrel.services.online_consent import approval_for
from kestrel.services.setting_overrides import load_settings_with_db
from tests.test_run_mode import _app_db, _fresh_app, _rows


def _hosted_app(*, trigger=True):
    client = _fresh_app()

    async def seed(s):
        rows = _rows(mode="hosted")
        if trigger:
            rows.update({
                "llm.hosted_tiers.trigger.backend": "frontier",
                "llm.hosted_tiers.trigger.model": "test/signal-model",
            })
        for key, value in rows.items():
            await s.merge(Setting(key=key, value=value))
        s.add(Source(name="Signals", track="consulting", kind="rss", enabled=True,
                     config={"url": "https://example.org/feed"}))

    _app_db(seed)
    return client


def _approval(tiers=("tier1", "tier2", "trigger")):
    async def read(s):
        return approval_for(await load_settings_with_db(s), tiers)

    return _app_db(read)


def test_c1_run_dialog_discloses_and_approves_trigger():
    with _hosted_app() as client:
        soup = BeautifulSoup(client.get("/runs/start/choose").text, "html.parser")
        posted = json.loads(soup.select_one('[name="online_approval"]')["value"])
        assert posted == _approval()
        assert "test/signal-model" in soup.get_text()
        assert "public filing" in soup.get_text().lower()


@pytest.mark.parametrize("url", ["/runs/start", "/runs/now", "/api/run/now?mode=hosted"])
def test_c1_every_run_route_refuses_missing_trigger_approval(url):
    with _hosted_app() as client:
        response = client.post(url, data={
            "mode": "hosted", "confirmed": "1",
            "online_approval": json.dumps(_approval(("tier1", "tier2"))),
        })
        assert response.status_code == 400


def test_c1_both_mode_discloses_its_own_trigger_destination():
    with _hosted_app() as client:
        async def seed(s):
            await s.merge(Setting(key="tracks.enabled", value=["employment", "consulting"]))
            await s.merge(Setting(key="llm.tiers.trigger.backend", value="frontier"))
            await s.merge(Setting(key="llm.tiers.trigger.model", value="different-signal-model"))

        _app_db(seed)
        soup = BeautifulSoup(client.get("/runs/start/choose").text, "html.parser")
        assert "different-signal-model" in soup.select_one('[data-sends="both"]').get_text()
        approval = soup.select_one('[name="both_online_approval"]')["value"]
        response = client.post("/runs/start", data={
            "mode": "both", "confirmed": "1", "online_approval": approval,
        })
        assert response.status_code == 200
        async def read(s):
            from sqlalchemy import select

            from kestrel.services.online_consent import RUN_TIERS, held_for_approval
            from kestrel.services.setting_overrides import PENDING_RUN_MODE_KEY

            effective = await load_settings_with_db(s, mode="both")
            expected = approval_for(effective, RUN_TIERS)
            task = (await s.execute(select(Task).where(Task.type == "scrape"))).scalar_one()
            assert task.payload["online_approval"] == expected
            run = await s.get(Setting, PENDING_RUN_MODE_KEY)
            assert run.value["online_approval"] == expected
            effective.llm.tiers["trigger"].model = "test/signal-model"
            assert held_for_approval(effective, ("trigger",), task.payload)
        _app_db(read)


def test_c1_both_refuses_a_change_to_the_unselected_hosted_destination():
    with _hosted_app() as client:
        async def seed(s):
            await s.merge(Setting(key="tracks.enabled", value=["employment", "consulting"]))
            await s.merge(Setting(key="llm.tiers.trigger.backend", value="frontier"))
            await s.merge(Setting(key="llm.tiers.trigger.model", value="different-signal-model"))
        _app_db(seed)
        soup = BeautifulSoup(client.get("/runs/start/choose").text, "html.parser")
        form = {x["name"]: x.get("value", "") for x in soup.select('input[type="hidden"]')}
        async def change(s):
            await s.merge(Setting(key="llm.tiers.trigger.model", value="test/signal-model"))
        _app_db(change)
        response = client.post("/runs/start", data={**form, "mode": "both", "confirmed": "1"})
        assert response.status_code == 400


def test_c1_local_run_does_not_accept_the_hidden_online_approval():
    with _hosted_app() as client:
        async def seed(s):
            await s.merge(Setting(key="tracks.enabled", value=["employment", "consulting"]))
        _app_db(seed)
        response = client.post("/runs/start", data={
            "mode": "local", "online_approval": json.dumps(_approval()),
        })
        assert response.status_code == 200
        async def read(s):
            from sqlalchemy import select
            task = (await s.execute(select(Task).where(Task.type == "scrape"))).scalar_one()
            assert task.payload["online_approval"] == {}
        _app_db(read)


def _held_role(*, status="pending"):
    async def seed(s):
        opp = Opportunity(track="employment", opp_type="job_posting", title="Engineer",
                          stage="t1_pass")
        s.add(opp)
        await s.flush()
        task = Task(type="t2", status=status, payload={
            "opportunity_id": opp.id, "online_approval": {"tier2": ["old-destination"]},
            "model_override": {"backend": "frontier", "model": "old-model"},
        }, last_error="held: online destination changed or was not confirmed")
        s.add(task)
        await s.flush()
        return opp.id, task.id

    return _app_db(seed)


def test_c2_held_scoring_is_visible():
    with _hosted_app(trigger=False) as client:
        oid, _tid = _held_role()
        page = client.get(f"/opportunities/{oid}").text
        assert "Waiting for your confirmation" in page
        assert "Being scored. The details appear here" not in page
        soup = BeautifulSoup(page, "html.parser")
        assert soup.select_one(".rl-main [data-testid='scoring-held']")


def test_c2_fresh_confirm_resumes_held_scoring():
    with _hosted_app(trigger=False) as client:
        oid, tid = _held_role()
        form = {"opp_ids": str(oid), "model_choice": "same",
                "online_approval": json.dumps(_approval())}
        refused = client.post("/rescore", data=form)
        assert "Confirm the online send first" in refused.text
        response = client.post("/rescore", data={**form, "confirmed": "1"})
        assert "Queued 1 role" in response.text

        async def read(s):
            task = await s.get(Task, tid)
            return task.payload, task.last_error, task.status

        payload, error, status = _app_db(read)
        assert payload["online_approval"] == _approval()
        assert "model_override" not in payload
        assert error is None and status == "pending"
        assert "Waiting for your confirmation" not in client.get(f"/opportunities/{oid}").text


def test_c2_fresh_consent_does_not_change_a_leased_task():
    with _hosted_app(trigger=False) as client:
        oid, tid = _held_role(status="leased")
        response = client.post("/rescore", data={
            "opp_ids": str(oid), "model_choice": "same", "confirmed": "1",
            "online_approval": json.dumps(_approval()),
        })
        assert "already waiting" in response.text

        async def read(s):
            return (await s.get(Task, tid)).payload

        assert _app_db(read)["online_approval"] == {"tier2": ["old-destination"]}


@pytest.mark.parametrize("url", ["/sources/check-all", "/scan/start"])
def test_c2_fresh_scan_confirmation_releases_held_scrape(url):
    from sqlalchemy import select

    with _hosted_app() as client:
        async def seed(s):
            await s.merge(Setting(key="tracks.enabled", value=["employment", "consulting"]))
            source = (await s.execute(select(Source))).scalars().first()
            task = Task(type="scrape", payload={"source_id": source.id,
                        "online_approval": {"trigger": ["old"]}},
                        last_error="held: online destination changed or was not confirmed")
            s.add(task)
            await s.flush()
            return task.id

        tid = _app_db(seed)
        page = BeautifulSoup(client.get("/sources").text, "html.parser")
        assert page.select_one('[data-testid="consent-held-note"]')
        assert not page.select_one('[data-testid="checking-note"]')
        response = client.post(url, data={
            "confirmed": "1", "online_approval": json.dumps(_approval()),
        })
        assert response.status_code == 200

        async def read(s):
            task = await s.get(Task, tid)
            return task.payload, task.last_error

        payload, error = _app_db(read)
        assert payload["online_approval"] == _approval()
        assert error is None


def test_c3_check_all_opens_disclosure_before_posting():
    with _hosted_app() as client:
        page = BeautifulSoup(client.get("/sources").text, "html.parser")
        button = page.select_one('[data-testid="check-all"]')
        assert button.get("hx-get") == "/sources/check-all/choose"
        dialog = BeautifulSoup(client.get(button["hx-get"]).text, "html.parser")
        assert "test/signal-model" in dialog.get_text()
        assert "public filing" in dialog.get_text().lower()
        form = dialog.select_one("form")
        posted = {x["name"]: x.get("value", "") for x in form.select('input[type="hidden"]')}
        assert json.loads(posted["online_approval"]) == _approval()
        assert "Confirm the online scoring first" in client.post(
            "/sources/check-all", data=posted).text
        posted["confirmed"] = "1"
        response = client.post("/sources/check-all", data=posted)
        assert "Checking 1 source" in response.headers.get("HX-Trigger", "")


async def test_c4_company_summary_stops_if_hosted_is_disabled_during_fetch(sm):
    from kestrel.research import org_summary as osum
    from tests.test_org_summary import _Client, _excerpts, _fixture, _org
    from tests.test_run_mode import _seed_rows

    org_id = await _org(sm)
    await _seed_rows(sm, _rows(mode="hosted"))
    async with sm() as s:
        settings = await load_settings_with_db(s)
        # Keep the same external destination when Hosted mode is disabled, so this
        # specifically detects a withdrawn acknowledgement rather than a model change.
        await s.merge(Setting(key="llm.tiers.tier2.backend", value="frontier"))
        await s.merge(Setting(key="llm.tiers.tier2.model", value=settings.llm.tiers["tier2"].model))
        await osum.refresh_summary(s, org_id)
        await s.commit()
    client = _Client(_fixture("valid"))
    client.settings = settings

    async def acquire(session, org, settings):
        async with sm() as s:
            await s.merge(Setting(key="llm.hosted_mode.enabled", value=False))
            await s.commit()
        return _excerpts()

    await osum.drain_org_summaries(sm, client, settings, acquire=acquire)
    assert client.calls == []
    async with sm() as s:
        assert await osum.is_held(s, org_id)


async def test_c5_single_tier_consent_refuses_inactive_hosted_mode(sm):
    from kestrel.config import get_settings
    from kestrel.services.online_consent import consent_problem, online_route

    settings = get_settings().model_copy(deep=True)
    settings.llm.tiers["tier2"].backend = "frontier"
    async with sm() as s:
        route = await online_route(s, settings, "tier2")
    assert route["online"]
    form = {"confirmed": "1", "model": route["model"], "dest": route["dest"]}
    assert "Hosted mode" in (consent_problem(route, form) or "")


@pytest.mark.parametrize("assigned", [False, True])
def test_c6_models_other_key_uses_saved_private_address_and_refuses_missing_address(
    monkeypatch, assigned,
):
    import respx

    from kestrel.llm import providers
    from kestrel.services import setup_state
    from kestrel.services.other_service import URL_ENV
    from tests.test_other_service import KEY, MODELS, URL, _client, _post

    monkeypatch.setattr(setup_state, "probe_ollama", lambda settings: (None, []))
    with _client() as client:
        _post(client)
        with respx.mock as router:
            listing = router.get(URL + "/models").respond(200, json=MODELS)
            response = client.post("/models/online/key", data={"provider": "other", "op": "test"})
            assert "connection works" in response.text
            assert listing.calls.last.request.headers["authorization"] == f"Bearer {KEY}"
        providers.write_secret(URL_ENV, "")
        if assigned:
            async def seed(s):
                await s.merge(Setting(key="llm.tiers.tier2.backend", value="other"))
            _app_db(seed)
        with respx.mock(assert_all_called=False) as router:
            response = client.post("/models/online/key", data={"provider": "other", "op": "test"})
            assert response.status_code == 200
            expected = "Set the Other service address in Settings" if assigned else (
                "Choose a provider first"
            )
            assert expected in response.text
            assert not router.calls


@pytest.mark.parametrize("reason", [
    "worker_destination", "client_destination", "hosted_off", "local_empty",
])
async def test_c7_every_scrape_consent_hold_is_leased_once_and_other_work_finishes(
    sm, monkeypatch, reason,
):
    import asyncio
    from types import SimpleNamespace

    from sqlalchemy import select

    from kestrel.config import get_settings
    from kestrel.ingest import runner, signal_runner
    from kestrel.pipeline.worker import Worker
    from tests.pipeline_fakes import FakeLLM

    settings = get_settings().model_copy(deep=True)
    settings.llm.tiers["trigger"].backend = "frontier"
    settings.llm.hosted_mode.enabled = True
    settings.llm.hosted_mode.acknowledged_at = "2026-10-25T00:00:00+00:00"
    settings.llm.hosted_mode.acknowledged_terms = settings.llm.hosted_mode.disclosure_terms()
    approval = approval_for(settings, ("trigger",))
    client_settings = settings.model_copy(deep=True)
    if reason == "worker_destination":
        settings.llm.tiers["trigger"].model = "different"
    elif reason == "client_destination":
        client_settings.llm.tiers["trigger"].model = "different"
    elif reason == "hosted_off":
        settings.llm.hosted_mode.enabled = False
    worker = Worker(sm, FakeLLM(client_settings), settings)
    if reason == "local_empty":
        worker._online_approval = approval
        approval = {}
    # Worker initialization binds the client's settings; explicitly model a stale client.
    worker._client.settings = client_settings
    async with sm() as s:
        signal = Source(name="Signal", kind="rss", track="employment", enabled=True, config={})
        jobs = Source(name="Jobs", kind="greenhouse", track="employment", enabled=True, config={})
        s.add_all([signal, jobs])
        await s.commit()
        sid, jid = signal.id, jobs.id
    await worker._queue.enqueue("scrape", {"source_id": sid, "online_approval": approval})
    await worker._queue.enqueue("scrape", {"source_id": jid})
    calls = []

    async def run(_sm, source_id, **kwargs):
        calls.append(source_id)
        return SimpleNamespace(status="ok", counts={})

    monkeypatch.setattr(runner, "run_source", run)
    monkeypatch.setattr(signal_runner, "run_signal_source", run)
    for _cycle in range(2):
        await asyncio.wait_for(worker.drain_scrapes(), timeout=10)
    async with sm() as s:
        tasks = list((await s.execute(select(Task).order_by(Task.id))).scalars())
    assert calls == [jid]
    assert tasks[0].status == "pending" and tasks[0].attempts == 0
    assert "held" in tasks[0].last_error
    assert tasks[1].status == "done"


async def test_c8_a_new_online_tier_requires_fresh_approval(sm):
    from kestrel.config import get_settings
    from kestrel.services.online_consent import approval_problem, held_for_approval

    settings = get_settings().model_copy(deep=True)
    settings.llm.tiers["tier2"].backend = "frontier"
    settings.llm.hosted_mode.enabled = True
    settings.llm.hosted_mode.acknowledged_at = "2026-10-25T00:00:00+00:00"
    settings.llm.hosted_mode.acknowledged_terms = settings.llm.hosted_mode.disclosure_terms()
    shown = approval_for(settings, ("tier1", "tier2"))
    assert set(shown) == {"tier2"}
    form = {"confirmed": "1", "online_approval": shown}
    async with sm() as s:
        assert await approval_problem(s, settings, ("tier1", "tier2"), form) is None
        settings.llm.tiers["tier1"].backend = "frontier"
        assert await approval_problem(s, settings, ("tier1", "tier2"), form)
        assert held_for_approval(settings, ("tier1",), form)
        assert not held_for_approval(settings, ("tier2",), form)
        form["online_approval"] = approval_for(settings, ("tier1", "tier2"))
        assert await approval_problem(s, settings, ("tier1", "tier2"), form) is None


async def test_c9_role_gate_has_the_same_inactive_hosted_refusal(sm):
    from kestrel.config import get_settings
    from kestrel.web.pages.role import gate, online_route

    settings = get_settings().model_copy(deep=True)
    settings.llm.tiers["tier3"].backend = "frontier"
    async with sm() as s:
        route = await online_route(s, settings, "tier3")
    assert route is not None
    assert gate(route, {}) == "ask"
    form = {"confirmed": "1", "model": route["model"], "dest": route["dest"]}
    assert "Hosted mode" in (gate(route, form) or "")


async def test_c1_local_only_approval_survives_the_quick_screen(sm):
    from kestrel.config import get_settings
    from kestrel.pipeline.tier0 import run_tier0_batch
    from kestrel.pipeline.tier1 import handle_t1
    from kestrel.pipeline.worker import TierStats, Worker
    from kestrel.services.online_consent import held_for_approval
    from tests.pipeline_fakes import FakeLLM
    from tests.test_run_model_choice import _new_opp, _scrape_run, _source, _tasks

    settings = get_settings().model_copy(deep=True)
    sid = await _source(sm, kind="jobspy")  # a search, not the company-board lane (D-SRC-12)
    run_id = await _scrape_run(sm, sid, None)
    await _new_opp(sm, sid, run_id, "VP of IT")
    await run_tier0_batch(sm, settings=settings)
    worker = Worker(sm, FakeLLM(settings), settings)
    worker.apply_settings(settings, online_approval={})
    await worker.drain_tier("t1", 1, handle_t1, TierStats())
    (task,) = await _tasks(sm, "t2")
    assert task.payload["online_approval"] == {}
    settings.llm.tiers["tier2"].backend = "frontier"
    assert held_for_approval(settings, ("tier2",), task.payload)


async def test_c1_local_only_approval_survives_a_changed_posting(sm):
    import copy

    import respx
    from sqlalchemy import select

    from kestrel import paths
    from kestrel.ingest.http import HttpClient
    from kestrel.ingest.runner import run_source
    from tests.test_run_model_choice import _GH, _source, _tasks

    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("profile.yml").write_text(
        "employment:\n  employment_terms:\n    target_titles: [VP of IT, Sales Director]\n",
        encoding="utf-8",
    )
    sid = await _source(sm)
    changed = copy.deepcopy(_GH)
    changed["jobs"][0]["content"] = "A completely changed role leading all technology operations."
    with respx.mock as router:
        listing = router.get("https://boards-api.greenhouse.io/v1/boards/meridian/jobs")
        listing.respond(200, json=_GH)
        async with HttpClient(user_agent="test", respect_robots=False) as http:
            await run_source(sm, sid, http=http)
            async with sm() as s:
                for opp in (await s.execute(select(Opportunity))).scalars():
                    opp.stage = "t2_done"
                await s.commit()
            listing.respond(200, json=changed)
            await run_source(sm, sid, http=http, online_approval={})
    (task,) = await _tasks(sm, "t2")
    assert task.payload["online_approval"] == {}


@pytest.mark.parametrize("mode", ["hosted", "local"])
def test_c1_run_dialog_approval_reaches_the_signal_worker(monkeypatch, mode):
    import asyncio
    from types import SimpleNamespace

    from sqlalchemy import select

    from kestrel.db.engine import get_sessionmaker
    from kestrel.ingest import signal_runner
    from kestrel.pipeline.worker import Worker
    from tests.pipeline_fakes import FakeLLM

    calls = []

    async def run(_sm, source_id, **kwargs):
        calls.append(source_id)
        return SimpleNamespace(status="ok", counts={})

    monkeypatch.setattr(signal_runner, "run_signal_source", run)
    with _hosted_app() as client:
        async def seed(s):
            await s.merge(Setting(key="tracks.enabled", value=["employment", "consulting"]))
        _app_db(seed)
        soup = BeautifulSoup(client.get("/runs/start/choose").text, "html.parser")
        response = client.post("/runs/start", data={
            "mode": mode, "confirmed": "1",
            "online_approval": soup.select_one('[name="online_approval"]')["value"],
        })
        assert response.status_code == 200

        async def drain():
            sm = get_sessionmaker()
            async with sm() as s:
                settings = await load_settings_with_db(s, mode=mode)
            worker = Worker(sm, FakeLLM(settings), settings)
            await worker.drain_scrapes()
            async with sm() as s:
                (task,) = (await s.execute(select(Task).where(Task.type == "scrape"))).scalars()
                assert task.status == "done"
                assert calls == [task.payload["source_id"]]
        asyncio.run(drain())


def test_r1_today_scan_dialog_posts_an_approval_that_covers_every_online_tier():
    """R1 (owner, 2026-10-28): Today's Start now never sent the destination approval the server
    checks, so ticking the box could not start a scan. The dialog now posts a snapshot covering
    tier 1, tier 2 and the trigger tier, and a checked send with it queues the scan once."""
    with _hosted_app() as client:
        soup = BeautifulSoup(client.get("/scan/choose", headers={"HX-Request": "true"}).text,
                             "html.parser")
        posted = json.loads(soup.select_one('[name="online_approval"]')["value"])
        expected = _approval()
        for tier, destinations in expected.items():
            assert set(destinations) <= set(posted.get(tier, [])), (tier, posted)
        assert soup.select_one("form[data-usual-online]") is not None
        response = client.post("/scan/start", headers={"HX-Request": "true"}, data={
            "scope": "all", "model_choice": "usual", "confirmed": "1",
            "online_approval": json.dumps(posted),
        })
        assert response.status_code == 200
        assert "Not started" not in response.text
        assert response.headers.get("HX-Trigger") == "run-refresh"

        async def read(s):
            from sqlalchemy import select

            tasks = (await s.execute(select(Task).where(Task.type == "scrape"))).scalars().all()
            assert len(tasks) == 1
            assert tasks[0].payload["online_approval"]
        _app_db(read)


@pytest.mark.parametrize(
    ("data", "says"),
    [
        ({"model_choice": "usual"}, "Confirm the online send"),
        ({"model_choice": "usual", "confirmed": "1", "online_approval": "{}"}, "changed"),
    ],
)
def test_r1_a_refused_scan_says_why_and_queues_nothing(data, says):
    with _hosted_app() as client:
        response = client.post("/scan/start", headers={"HX-Request": "true"},
                               data={"scope": "all", **data})
        assert "Not started" in response.text and says in response.text, response.text
        assert "HX-Trigger" not in response.headers

        async def read(s):
            from sqlalchemy import select

            assert not (await s.execute(select(Task).where(Task.type == "scrape"))).first()
        _app_db(read)
