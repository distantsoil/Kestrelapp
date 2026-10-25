"""Wording fixes from the drift brief: the Warm paths empty state (A14)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import make_async_engine, reset_engine
from kestrel.db.models import Org
from kestrel.web.app import build_app

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


async def _seed_org() -> int:
    await reset_engine()
    migrate.run_migrations()
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(make_async_engine(paths.db_path()), expire_on_commit=False)
    async with sm() as s:
        org = Org(name="Northwind Freight", normalized_name="northwind freight")
        s.add(org)
        await s.commit()
        return org.id


def test_a14_people_you_know_empty_state_points_to_the_import():
    """D-UI-123 replaced the Warm paths empty state: with no import the section invites one and
    names LinkedIn, and the D-UI-131 disclosure line appears only once people were imported."""
    org_id = asyncio.run(_seed_org())
    with TestClient(build_app(worker=False)) as c:
        text = c.get(f"/orgs/{org_id}").text
    assert "Import your LinkedIn connections to see people you know at this company" in text
    assert "Because you imported these connections from LinkedIn" not in text
    assert "No warm paths found yet." not in text
