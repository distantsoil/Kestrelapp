"""Confidential mode reaches the JSON API and MCP, not just the HTML (AFTERACTION 1.3).

The HTML views redacted the operator's current employer while `/api/opportunities` and every MCP
read tool returned the real name. SPEC-INTERFACES says the UI is a CONSUMER of the API and not a
privileged path, so the two disagreeing was the bug: anything that asked the API, including the
browser extension, the CLI, and any MCP client, saw through the redaction the UI promised.

MCP matters most here. An MCP client is an LLM agent, so it is the last surface on which the
employer the operator is confidentially leaving should appear.

The redaction map comes from the operator's own DocSection rows (`org_label` ->
`org_label_redacted`), so these tests seed one exactly as the document engine would.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import (
    DocMaster,
    DocSection,
    Evaluation,
    Opportunity,
    Org,
    Pursuit,
    Setting,
)

_EMPLOYER = "Northwind Freight"
_PLACEHOLDER = "[Confidential]"


async def _seed(*, confidential: bool) -> dict[str, int]:
    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        org = Org(name=_EMPLOYER, normalized_name=_EMPLOYER.lower())
        s.add(org)
        await s.flush()
        opp = Opportunity(
            track="employment", opp_type="job_posting", title="VP of IT",
            org_id=org.id, org_name=_EMPLOYER, url="https://x/conf-1", stage="t2_done",
        )
        s.add(opp)
        await s.flush()
        s.add(
            Evaluation(
                opportunity_id=opp.id, tier=2, track="employment", verdict="apply",
                verdict_model="apply", score=4.5, weighted_score=4.4, needs_review=False,
                dimensions={}, blocks={}, blockers=[],
                legitimacy={"status": "clean", "signals": []},
            )
        )
        s.add(Pursuit(opportunity_id=opp.id, track="employment", status="shortlisted"))
        # The redaction map is the operator's own: a doc section that labels their current employer
        # and gives the placeholder they chose for it in the resume.
        master = DocMaster(track="employment", name="Resume", doc_kind="resume")
        s.add(master)
        await s.flush()
        s.add(
            DocSection(
                doc_master_id=master.id, section_kind="experience", sort_order=0,
                org_label=_EMPLOYER, org_label_redacted=_PLACEHOLDER,
            )
        )
        if confidential:
            s.add(Setting(key="confidential_mode", value=True))
        await s.commit()
        ids = {"opp": opp.id, "org": org.id}
    await engine.dispose()
    forget_engine()
    return ids


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _mcp_call(tool: str, args: dict):
    from fastmcp import Client

    from kestrel.mcp.server import build_mcp

    async def _run():
        async with Client(build_mcp()) as c:
            return (await c.call_tool(tool, args)).data

    return asyncio.run(_run())


# ------------------------------------------------------------------------------------- the API


def test_api_list_redacts_the_current_employer():
    ids = asyncio.run(_seed(confidential=True))
    with _client() as c:
        body = c.get("/api/opportunities").json()
    orgs = [r["org"] for r in body["rows"]]
    assert _EMPLOYER not in orgs, "the JSON API leaked the employer the UI redacts"
    assert _PLACEHOLDER in orgs
    assert ids["opp"] in [r["id"] for r in body["rows"]]


def test_api_detail_redacts_the_current_employer():
    ids = asyncio.run(_seed(confidential=True))
    with _client() as c:
        body = c.get(f"/api/opportunities/{ids['opp']}").json()
    assert body["org"] == _PLACEHOLDER


def test_the_api_and_the_html_agree():
    """The actual defect: the UI is a consumer of this API, so if the page says [Confidential] and
    the API says Northwind Freight, one of them is lying to the operator."""
    asyncio.run(_seed(confidential=True))
    with _client() as c:
        page = c.get("/opportunities").text
        api = c.get("/api/opportunities").json()
    assert _EMPLOYER not in page
    assert _EMPLOYER not in str(api)


def test_confidential_mode_off_returns_the_real_name():
    # A redactor that always redacts is not a feature, it is a bug with good intentions.
    asyncio.run(_seed(confidential=False))
    with _client() as c:
        body = c.get("/api/opportunities").json()
    assert _EMPLOYER in [r["org"] for r in body["rows"]]


# ------------------------------------------------------------------------------------- the MCP


# search_opportunities is deliberately excluded: it embeds the query with the real model, so
# exercising it here would make a live network call. Its results flow through the same
# _opp_summary serializer that list_opportunities and get_shortlist use, which IS covered.
@pytest.mark.parametrize(
    "tool,args",
    [
        ("list_opportunities", {}),
        ("get_shortlist", {}),
        ("get_pursuits", {}),
    ],
)
def test_mcp_read_tools_redact_the_current_employer(tool, args):
    asyncio.run(_seed(confidential=True))
    data = _mcp_call(tool, args)
    assert _EMPLOYER not in str(data), f"MCP tool {tool} leaked the current employer to the agent"
    assert _PLACEHOLDER in str(data)


def test_mcp_get_opportunity_redacts():
    ids = asyncio.run(_seed(confidential=True))
    data = _mcp_call("get_opportunity", {"opportunity_id": ids["opp"]})
    assert data["org"] == _PLACEHOLDER


def test_mcp_get_org_redacts():
    ids = asyncio.run(_seed(confidential=True))
    data = _mcp_call("get_org", {"org_id": ids["org"]})
    assert data["name"] == _PLACEHOLDER


def test_mcp_returns_the_real_name_when_confidential_mode_is_off():
    asyncio.run(_seed(confidential=False))
    assert _EMPLOYER in str(_mcp_call("get_shortlist", {}))


def test_an_unrelated_org_is_never_redacted():
    """Redaction is keyed to the operator's OWN employer label. Blanking every org name would
    destroy the product while looking like a privacy win."""
    asyncio.run(_seed(confidential=True))
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)

    async def _add_other():
        async with sm() as s:
            other = Org(name="Beacon Robotics", normalized_name="beacon robotics")
            s.add(other)
            await s.flush()
            s.add(
                Opportunity(
                    track="employment", opp_type="job_posting", title="Head of Data",
                    org_id=other.id, org_name="Beacon Robotics", url="https://x/other",
                    stage="t2_done",
                )
            )
            await s.commit()
        await engine.dispose()
        forget_engine()

    asyncio.run(_add_other())
    with _client() as c:
        body = c.get("/api/opportunities").json()
    orgs = [r["org"] for r in body["rows"]]
    assert "Beacon Robotics" in orgs
    assert _EMPLOYER not in orgs


# --------------------------------------------------------- the leaks a fresh reviewer found

def test_mcp_search_results_are_redacted():
    """search_opportunities returns SearchHit.__dict__ directly rather than going through
    _opp_summary, which made it the one read tool that leaked the employer outright. The embedding
    call is stubbed so no network is touched."""
    import kestrel.mcp.server as srv

    ids = asyncio.run(_seed(confidential=True))

    class _StubEmbed:
        _http = None

        async def embed(self, texts, **kw):
            return [[0.1] * 8 for _ in texts]

    async def _stub_client():
        return _StubEmbed()

    async def _stub_search(session, client, query, track=None, limit=10):
        from kestrel.intelligence.search import SearchHit

        return [
            SearchHit(
                opportunity_id=ids["opp"], title=f"VP of IT at {_EMPLOYER}",
                org_name=_EMPLOYER, track="employment", score=0.9,
                verdict="apply", eval_score=4.5,
            )
        ]

    original_client = srv._client
    srv._client = _stub_client
    try:
        import kestrel.intelligence.search as search_mod

        original_search = search_mod.search_opportunities
        search_mod.search_opportunities = _stub_search
        try:
            data = _mcp_call("search_opportunities", {"query": "IT leadership"})
        finally:
            search_mod.search_opportunities = original_search
    finally:
        srv._client = original_client

    assert _EMPLOYER not in str(data), "search_opportunities leaked the current employer"


def test_free_text_fields_are_redacted_not_just_the_org_field():
    """Redacting `org` while the same response carries the employer throughout the title and the
    job description is redaction theater. Tier 3 already redacts this exact text before sending."""
    ids = asyncio.run(_seed(confidential=True))
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)

    async def _add_text():
        async with sm() as s:
            opp = await s.get(Opportunity, ids["opp"])
            opp.title = f"VP of IT at {_EMPLOYER}"
            opp.description_md = f"{_EMPLOYER} is hiring. {_EMPLOYER} is a freight company."
            await s.commit()
        await engine.dispose()
        forget_engine()

    asyncio.run(_add_text())

    with _client() as c:
        body = c.get("/api/opportunities").json()
    assert _EMPLOYER not in str(body), "the API leaked the employer in the title"

    data = _mcp_call("get_opportunity", {"opportunity_id": ids["opp"]})
    assert _EMPLOYER not in str(data), "MCP leaked the employer in title or description_md"
    assert _PLACEHOLDER in data["description_md"]


def _add_prose(ids: dict[str, int]) -> None:
    """Fill the Tier 2 prose, the org research, and a contact with the employer's name, the way a
    real evaluation and a real research pass do."""
    from kestrel.db.models import Contact, OrgResearch

    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)

    async def _run():
        async with sm() as s:
            ev = (
                await s.execute(
                    select(Evaluation).where(Evaluation.opportunity_id == ids["opp"])
                )
            ).scalar_one()
            ev.blocks = {
                "a_role_summary": {"what_this_actually_is": f"Running IT at {_EMPLOYER}."}
            }
            ev.dimensions = {"fit": {"score": 4.0, "why": f"{_EMPLOYER} needs a modernizer."}}
            ev.blockers = [{"kind": "comp", "detail": f"{_EMPLOYER} pays below band."}]
            ev.legitimacy = {"status": "clean", "signals": [f"{_EMPLOYER} is a real company"]}
            s.add(OrgResearch(org_id=ids["org"], summary_md=f"{_EMPLOYER} is a freight company.",
                              signals=[f"{_EMPLOYER} raised a round"], unknowns=["CTO tenure"]))
            s.add(Contact(org_id=ids["org"], name="A. Person",
                          title=f"VP Engineering at {_EMPLOYER}", contact_type="hiring_manager"))
            await s.commit()
        await engine.dispose()
        forget_engine()

    asyncio.run(_run())


def test_redact_json_redacts_every_string_leaf_without_mutating_the_input():
    from kestrel.privacy import from_pairs
    from kestrel.web.redact import redact_json

    mapping = from_pairs([(_EMPLOYER, _PLACEHOLDER)])
    original = {
        "why": f"{_EMPLOYER} needs help", "score": 4.0, "n": None,
        "items": [f"at {_EMPLOYER}", 3, {"deep": f"{_EMPLOYER}!"}],
    }
    out = redact_json(original, mapping, enabled=True)
    assert _EMPLOYER not in str(out) and _PLACEHOLDER in out["why"]
    assert out["score"] == 4.0 and out["n"] is None and out["items"][1] == 3
    assert _EMPLOYER in original["why"], "the input was mutated"
    assert redact_json(original, mapping, enabled=False) is original


def test_api_detail_redacts_the_tier2_prose():
    """Redacting `org` while `dimensions[*].why`, `blockers[*].detail`, and `legitimacy` name
    the employer in full sentences was the remaining leak on the detail endpoint."""
    ids = asyncio.run(_seed(confidential=True))
    _add_prose(ids)
    with _client() as c:
        body = c.get(f"/api/opportunities/{ids['opp']}").json()
    assert _EMPLOYER not in str(body), "the JSON detail leaked the employer in the Tier 2 prose"
    assert _PLACEHOLDER in body["dimensions"]["fit"]["why"]
    assert body["dimensions"]["fit"]["score"] == 4.0  # non-string leaves are untouched


def test_row_expand_partial_redacts_the_tier2_prose():
    ids = asyncio.run(_seed(confidential=True))
    _add_prose(ids)
    with _client() as c:
        html = c.get(f"/opportunities/{ids['opp']}/expand", headers={"HX-Request": "true"}).text
    assert _EMPLOYER not in html, "the row expansion rendered the employer from ev.blocks"
    assert _PLACEHOLDER in html


def test_mcp_get_opportunity_redacts_the_tier2_prose():
    ids = asyncio.run(_seed(confidential=True))
    _add_prose(ids)
    data = _mcp_call("get_opportunity", {"opportunity_id": ids["opp"]})
    assert _EMPLOYER not in str(data), "MCP leaked the employer in blocks/dimensions/legitimacy"
    assert _PLACEHOLDER in data["blocks"]["a_role_summary"]["what_this_actually_is"]


def test_mcp_get_org_redacts_research_and_contacts():
    ids = asyncio.run(_seed(confidential=True))
    _add_prose(ids)
    data = _mcp_call("get_org", {"org_id": ids["org"]})
    assert _EMPLOYER not in str(data), "MCP get_org leaked the employer in research or contacts"
    assert _PLACEHOLDER in data["research"]["summary_md"]
    assert _PLACEHOLDER in data["contacts"][0]["title"]


@pytest.mark.parametrize(
    "variant",
    ["Northwind Freight Inc.", "Northwind Freight, LLC", "NORTHWIND FREIGHT", "Northwind  Freight"],
)
def test_org_name_variants_still_redact(variant):
    """A real org table holds the same employer spelled differently by every board. An exact
    lowercase match let all of these through."""
    from kestrel.privacy import from_pairs
    from kestrel.web.redact import redact_name

    mapping = from_pairs([(_EMPLOYER, _PLACEHOLDER)])
    assert redact_name(variant, mapping, enabled=True) == _PLACEHOLDER


def test_confidential_on_with_no_map_is_reported_as_inert():
    """The control is driven by the operator's own doc sections. On a fresh install the checkbox is
    on, the copy promises the employer is hidden, and nothing is redacted. A privacy control that
    appears enabled and does nothing is worse than one that is plainly off, so it must be surfaced.
    """
    from kestrel.web.redact import confidential_is_inert

    async def _seed_no_map():
        await reset_engine()
        for suffix in ("", "-wal", "-shm"):
            paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
        migrate.run_migrations()
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            s.add(Setting(key="confidential_mode", value=True))
            await s.commit()
            inert = await confidential_is_inert(s)
        await engine.dispose()
        forget_engine()
        return inert

    assert asyncio.run(_seed_no_map()) is True

    with _client() as c:
        page = c.get("/settings").text
    assert "redacting nothing" in page, "Settings does not warn that the control is inert"


def test_a_working_map_is_not_reported_as_inert():
    from kestrel.web.redact import confidential_is_inert

    asyncio.run(_seed(confidential=True))
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)

    async def _check():
        async with sm() as s:
            out = await confidential_is_inert(s)
        await engine.dispose()
        forget_engine()
        return out

    assert asyncio.run(_check()) is False
