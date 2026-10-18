"""Regression tests for the 2026-09-30 review round. Each test names the defect it
pins: strict hosted schemas, the replay guard covering failed sends, a crashed run never left
`running`, scheduled scrape tasks actually executed, and a per-tier timeout that reaches the
transport. No live calls."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response
from sqlalchemy import select

from kestrel.config import BackendConfig, HostedOptions, get_settings
from kestrel.db.models import FrontierTransmission, Run, ScrapeRun, Source, Task
from kestrel.llm.client import LLMClient, build_request_body, json_schema_for, strict_schema
from kestrel.llm.schemas import P2Eval
from kestrel.pipeline import tier3
from kestrel.pipeline.worker import Worker
from kestrel.queue.queue import TaskQueue
from tests.pipeline_fakes import FakeLLM


# ------------------------------------------------------------------------ strict hosted schema
def _walk_objects(node, path="$"):
    if isinstance(node, dict):
        if isinstance(node.get("properties"), dict):
            yield path, node
        for k, v in node.items():
            yield from _walk_objects(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_objects(v, f"{path}[{i}]")


def test_hosted_schema_is_strict_compliant_for_the_real_tier2_schema():
    """OpenAI-strict endpoints reject a schema whose objects are missing a property from
    `required` or lack additionalProperties:false. The Tier 3 prompt uses the P2 schema, which has
    optional fields; the hosted path must rewrite them into the strict dialect."""
    backend = BackendConfig(
        base_url="https://openrouter.ai/api/v1", provider="openrouter", external=True,
        thinking_control="none", hosted=HostedOptions(),
    )
    body = build_request_body(
        model="m", messages=[{"role": "user", "content": "x"}], backend=backend,
        temperature=0.0, max_tokens=10, schema=json_schema_for(P2Eval), schema_name="t3",
    )
    js = body["response_format"]["json_schema"]
    assert js["strict"] is True
    objects = list(_walk_objects(js["schema"]))
    assert len(objects) >= 5
    for path, obj in objects:
        assert obj.get("additionalProperties") is False, path
        assert sorted(obj["required"]) == sorted(obj["properties"]), path


def test_strict_schema_turns_optional_into_nullable_and_keeps_nullable_as_is():
    out = strict_schema(
        {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
            "required": ["a"],
        }
    )
    assert out["required"] == ["a", "b"] and out["additionalProperties"] is False
    assert out["properties"]["a"] == {"type": "string"}
    assert out["properties"]["b"] == {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    # Applying it twice is a no-op (no double wrapping).
    assert strict_schema(out) == out


# ------------------------------------------------------------------------------ replay guard
async def test_replay_guard_refuses_after_a_failed_send_too(sm, monkeypatch):
    """A failed or quarantined transmission already put the payload on the wire; the same token
    must not send it again. Only a fresh preview and confirmation may."""
    from tests.test_tier3 import _RecordingClient, _seed_opp

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    async with sm() as s:
        oid = await _seed_opp(s)
    settings = get_settings()
    async with sm() as s:
        preview = await tier3.build_escalation_preview(s, settings, oid)
        s.add(
            FrontierTransmission(
                opportunity_id=oid, provider="frontier", base_url=preview.base_url,
                model=preview.model, payload=preview.messages,
                payload_sha256=preview.confirmation_token, status="failed", error="HTTP 500",
            )
        )
        await s.commit()
    client = _RecordingClient(settings)
    async with sm() as s:
        r = await tier3.transmit_escalation(
            s, client, settings, oid, confirmation_token=preview.confirmation_token
        )
    # The refusal must not tell the operator to "open a fresh preview": the confirmation token is
    # deterministic, so a fresh preview produces the same token and the advice cannot work
    # (review). Assert the behaviour -- refused, nothing sent -- not the exact wording.
    assert r.sent is False, "a payload the provider already received was sent again"
    assert r.error, "a refusal must explain itself"
    assert "fresh preview" not in r.error, (
        "the refusal still suggests a fresh preview, which cannot help when the token is the same"
    )
    assert client.calls == []


# --------------------------------------------------------------------------- run never stuck
async def test_crashed_run_is_marked_failed_not_left_running(sm, monkeypatch):
    settings = get_settings()
    worker = Worker(sm, FakeLLM(settings), settings)

    async def boom(*a, **k):
        raise RuntimeError("filters.yml exploded")

    monkeypatch.setattr("kestrel.pipeline.worker.run_tier0_batch", boom)
    with pytest.raises(RuntimeError):
        await worker.run()
    async with sm() as s:
        run = (await s.execute(select(Run))).scalars().one()
    assert run.status == "failed" and run.finished_at
    assert "filters.yml exploded" in run.counts["error"]


# ---------------------------------------------------------------------- scrape tasks execute
_GH = json.loads((Path(__file__).parent / "fixtures" / "greenhouse.json").read_text())


@respx.mock
async def test_scrape_task_is_leased_executed_and_completed(sm):
    """The scheduler, tray and MCP enqueue `scrape`; the worker must consume it (it did not: the
    tasks sat pending forever and suppressed every re-enqueue)."""
    respx.get("https://boards-api.greenhouse.io/v1/boards/m/jobs").mock(
        return_value=Response(200, json=_GH)
    )
    async with sm() as s:
        src = Source(name="gh", kind="greenhouse", track="employment", enabled=True,
                     config={"token": "m"})
        s.add(src)
        await s.commit()
        sid = src.id
    q = TaskQueue(sm)
    await q.enqueue("scrape", {"source_id": sid})
    await q.enqueue("scrape", {"source_id": 999_999})  # a vanished source must not wedge the drain
    settings = get_settings()
    settings.ingest.respect_robots = False
    worker = Worker(sm, FakeLLM(settings), settings)
    stats = await worker.drain_scrapes()
    assert stats.completed == 1 and stats.errors == 1
    async with sm() as s:
        tasks = (await s.execute(select(Task).where(Task.type == "scrape"))).scalars().all()
        assert sorted(t.status for t in tasks) == ["dead", "done"]
        runs = (await s.execute(select(ScrapeRun))).scalars().all()
        assert len(runs) == 1 and runs[0].status == "ok" and runs[0].counts["new"] >= 1


# ------------------------------------------------------------------------ per-tier timeout
@respx.mock
async def test_tier_timeout_reaches_the_transport():
    seen: list[float | None] = []

    def _capture(request: httpx.Request):
        seen.append(request.extensions.get("timeout", {}).get("read"))
        return Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    respx.post("http://x/v1/chat/completions").mock(side_effect=_capture)
    s = get_settings()
    s.llm.backends["local"].base_url = "http://x/v1"
    s.llm.tiers["tier2"].timeout_s = 321.0
    async with httpx.AsyncClient() as http:
        client = LLMClient(s, http=http)
        await client.complete(tier="tier2", messages=[{"role": "user", "content": "x"}])
        await client.complete(tier="tier1", messages=[{"role": "user", "content": "x"}])
        await client.complete(
            tier="tier1", messages=[{"role": "user", "content": "x"}], timeout=7.0
        )
    assert seen == [321.0, 180.0, 7.0]


# ------------------------------------------------------------- tunnel: public hostnames
def test_public_hostname_origin_and_host_are_accepted_only_when_listed(monkeypatch):
    """docs/WEBAPP.md: through a Cloudflare Tunnel the request reaches loopback with the public
    hostname as Host, and HTMX POSTs carry it as Origin. Listed exact https hostnames pass both
    guards; anything else (an http origin, an unlisted host) is still refused."""
    from starlette.testclient import TestClient

    from kestrel.db import migrate
    from kestrel.db.engine import reset_engine
    from kestrel.web.app import _is_own_origin, build_app

    assert _is_own_origin("https://k.example.com", public_hostnames=("k.example.com",))
    assert not _is_own_origin("http://k.example.com", public_hostnames=("k.example.com",))
    assert not _is_own_origin("https://evil.example.com", public_hostnames=("k.example.com",))
    assert not _is_own_origin("https://k.example.com")  # not listed: refused

    monkeypatch.setenv("KESTREL_SERVER__PUBLIC_HOSTNAMES", '["k.example.com"]')
    from kestrel import paths

    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations(paths.db_path())
    import asyncio as _asyncio

    _asyncio.run(reset_engine())
    client = TestClient(build_app(worker=False))
    ok = client.post("/settings", data={}, headers={"Origin": "https://k.example.com"})
    assert ok.status_code == 200
    bad = client.post("/settings", data={}, headers={"Origin": "https://evil.example.com"})
    assert bad.status_code == 403
    host_ok = client.get("/health", headers={"Host": "k.example.com"})
    assert host_ok.status_code == 200
    host_bad = client.get("/health", headers={"Host": "evil.example.com"})
    assert host_bad.status_code == 400


# ------------------------------------------------------------- liveness cadence in serve loop
async def test_serve_loop_asks_liveness_once_an_hour(monkeypatch):
    from kestrel.web import app as app_mod

    calls: list[int] = []

    async def fake_cycle(sm, settings, http):
        calls.append(1)
        return {"due": 0, "checked": 0, "closed": 0, "errors": 0}

    app_mod._liveness_state["last"] = None
    settings = get_settings()
    assert await app_mod._maybe_run_liveness(None, settings, fake_cycle) is not None
    assert await app_mod._maybe_run_liveness(None, settings, fake_cycle) is None  # within the hour
    assert calls == [1]

    async def boom(sm, settings, http):
        raise RuntimeError("network down")

    app_mod._liveness_state["last"] = None
    assert await app_mod._maybe_run_liveness(None, settings, boom) is None  # never raises
