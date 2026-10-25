from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db.engine import make_async_engine
from kestrel.db.models import Opportunity
from kestrel.web.app import build_app
from tests import prep_seed

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


@pytest.mark.parametrize("fragment", [False, True])
async def test_saved_region_reaches_pages_and_fragments_without_restart(fragment):
    ids = await prep_seed.seed_async(with_pack=True)
    opportunity = ids["Larkspur Logistics"]["opp"]
    engine = make_async_engine(paths.db_path())
    async with async_sessionmaker(engine)() as session:
        await session.execute(update(Opportunity).where(Opportunity.id == opportunity)
                              .values(date_posted="2026-10-23"))
        await session.commit()
    await engine.dispose()
    day = date.today() + timedelta(days=5)
    with TestClient(build_app(worker=False)) as client:
        original = client.get(f"/prep/{opportunity}").text
        assert f"{day:%a, %b} {day.day}" in original
        assert client.post("/settings/ui", json={
            "key": "region.date_format", "value": "DD/MM/YYYY"}).json()["ok"]
        assert client.post("/settings/ui", json={
            "key": "region.time_format", "value": "24h"}).json()["ok"]
        headers = {"HX-Request": "true", "HX-Target": "pp-body"} if fragment else {}
        response = client.get(f"/prep/{opportunity}", headers=headers)
        assert response.status_code == 200
        if not fragment:
            assert f"{day.day:02d}/{day.month:02d}/{day.year}" in response.text
        posting = client.get(f"/opportunities/{opportunity}/posting", headers=headers)
        assert posting.status_code == 200
        assert "23/10/2026" in posting.text
        settings = client.get("/settings/section/preferences").text
        assert "20:00" in settings and "8:00 PM" not in settings
        assert "23/10/2026, 20:02" in settings
        board = client.get("/pursuits").text
        assert f"{day.day:02d}/{day.month:02d}/{day.year}" in board
        assert client.post("/settings/ui", json={
            "key": "region.date_format", "value": "MM/DD/YYYY"}).json()["ok"]
        assert f"{day:%a, %b} {day.day}" in client.get(f"/prep/{opportunity}").text
