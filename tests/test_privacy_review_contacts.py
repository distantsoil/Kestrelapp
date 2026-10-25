"""B1: an imported LinkedIn connection never reaches an MCP tool or a context builder."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db.engine import make_async_engine, reset_engine
from kestrel.db.models import Contact
from kestrel.mcp.server import build_mcp
from tests.test_mcp import _seed


def test_mcp_get_org_hides_imported_connections():
    from fastmcp import Client

    async def _run():
        await _seed()
        eng = make_async_engine(paths.db_path())
        sm = async_sessionmaker(eng, expire_on_commit=False)
        async with sm() as s:
            s.add_all([
                Contact(org_id=1, name="Public Person", title="CTO", contact_type="decision_maker",
                        source="company_site"),
                Contact(org_id=1, name="Imported Friend", title="Eng", contact_type="peer",
                        source="linkedin_import"),
                Contact(org_id=1, name="Kept Friend", title="Eng", contact_type="peer",
                        source="linkedin_import_kept"),
                Contact(org_id=1, name="Manual Person", title="Eng", contact_type="peer",
                        source=None),
            ])
            await s.commit()
        await eng.dispose()
        await reset_engine()
        async with Client(build_mcp()) as c:
            return (await c.call_tool("get_org", {"org_id": 1})).data

    out = asyncio.run(_run())
    names = {c["name"] for c in out["contacts"]}
    assert "Imported Friend" not in names and "Kept Friend" not in names
    assert {"Public Person", "Manual Person"} <= names
    assert "Imported Friend" not in str(out)
