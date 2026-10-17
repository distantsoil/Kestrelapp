"""Phase 15 MCP gate: tools/list returns exactly the expected tool names (a 200 from an SSE endpoint
is NOT proof it is the right server, so we assert on the tool list), and the read tools return
correct data against seeded fixtures. Plus the extension's two endpoints (ingest + lookup). Offline:
no live model except where a tool needs an embedder, which those tests avoid."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.base import utcnow_iso
from kestrel.db.engine import make_async_engine, reset_engine
from kestrel.db.models import CompObservation, Evaluation, Opportunity, Org, Source
from kestrel.mcp.server import TOOL_NAMES, build_mcp

_eng = None


@pytest.fixture(autouse=True)
def _dispose_engine():
    yield
    global _eng
    if _eng is not None:
        asyncio.run(_eng.dispose())
        _eng = None
    asyncio.run(reset_engine())


def _wipe_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)


async def _seed():
    global _eng
    if _eng is not None:
        await _eng.dispose()
    await reset_engine()
    _wipe_db()
    migrate.run_migrations()
    _eng = make_async_engine(paths.db_path())
    sm = async_sessionmaker(_eng, expire_on_commit=False)
    async with sm() as s:
        org = Org(name="Acme", normalized_name="acme", size_band="mid")
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        s.add_all([org, src])
        await s.flush()
        opp = Opportunity(
            track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
            title="VP of IT", org_name="Acme", url="https://x/1", stage="t2_done",
            first_seen_at=utcnow_iso(),
        )
        s.add(opp)
        await s.flush()
        s.add(Evaluation(opportunity_id=opp.id, tier=2, track="employment", verdict="apply",
                         verdict_model="apply", score=4.3, weighted_score=4.2, needs_review=False,
                         dimensions={}, blocks={}, blockers=[],
                         legitimacy={"status": "clean", "signals": []}))
        for m in (200000, 210000, 220000):  # only 3 -> comp intel must report insufficient
            s.add(CompObservation(normalized_title="vp of it", geo_bucket="CA", org_size_band="mid",
                                  min=m, max=m, currency="USD", interval="year", source="posting",
                                  observed_at=utcnow_iso()))
        await s.commit()
    await _eng.dispose()
    await reset_engine()


def test_tools_list_matches_spec():
    from fastmcp import Client

    async def _run():
        async with Client(build_mcp()) as c:
            return sorted(t.name for t in await c.list_tools())

    assert asyncio.run(_run()) == sorted(TOOL_NAMES)


def test_read_tools_return_seeded_data():
    from fastmcp import Client

    async def _run():
        await _seed()
        async with Client(build_mcp()) as c:
            shortlist = (await c.call_tool("get_shortlist", {})).data
            comp = (await c.call_tool(
                "get_comp_intel", {"title": "VP of IT", "geo_bucket": "CA", "org_size_band": "mid"}
            )).data
            opp = (await c.call_tool("get_opportunity", {"opportunity_id": 1})).data
        return shortlist, comp, opp

    shortlist, comp, opp = asyncio.run(_run())
    # get_shortlist returns the seeded apply verdict.
    assert len(shortlist) == 1 and shortlist[0]["verdict"] == "apply"
    assert shortlist[0]["title"] == "VP of IT"
    # get_comp_intel reports the sample size and refuses below five (constraint 6).
    assert comp["n"] == 3 and comp["sufficient"] is False
    assert "insufficient" in (comp["insufficient_message"] or "").lower()
    # get_opportunity returns the full record with its evaluation.
    assert opp["id"] == 1 and opp["verdict"] == "apply"


def test_extension_endpoints_ingest_and_lookup():
    from starlette.testclient import TestClient

    from kestrel.web.app import build_app

    asyncio.run(_seed())
    asyncio.run(reset_engine())
    client = TestClient(build_app(worker=False))
    # capture: a page the operator is viewing enters the pipeline.
    r = client.post("/api/opportunities/ingest",
                    data=json.dumps({"url": "https://x/new-job", "text": "Head of IT\nLead IT."}),
                    headers={"content-type": "application/json"})
    assert r.status_code == 200 and r.json()["stage"] == "new"
    # seen-badge: lookup returns the known seeded URL, not the unknown one.
    r2 = client.post("/api/opportunities/lookup",
                     data=json.dumps({"urls": ["https://x/1", "https://x/never"]}),
                     headers={"content-type": "application/json"})
    known = r2.json()
    assert "https://x/1" in known and "https://x/never" not in known


def test_extension_manifest_is_valid_mv3():
    manifest = json.loads((Path("extension") / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    # loopback-only host permissions (privacy): no broad host in the required set.
    assert manifest["host_permissions"] == ["http://127.0.0.1/*", "http://localhost/*"]
    assert "background" in manifest and "content_scripts" in manifest


def test_extension_javascript_parses():
    """The extension ships as source with no build step, so a syntax error would reach the operator
    and fail silently inside a service worker. Nothing else in the suite executes this JS, and the
    port-discovery rewrite (AFTERACTION 3.9) touched all of it. Skipped where node is absent so a
    machine without it is not blocked; CI and the build box both have it."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available to parse-check the extension")
    for name in ("background.js", "options.js", "content.js"):
        path = Path("extension") / name
        proc = subprocess.run(
            [node, "--check", str(path)], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, f"{name} does not parse:\n{proc.stderr}"
