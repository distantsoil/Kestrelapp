"""A scraped posting with no body reads Not scored with the reason (issue #66), on the role
page, the Pipeline panel and the Pipeline row. It is not in Scoring failed and offers no Retry."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db.engine import make_async_engine
from kestrel.db.models import Opportunity
from kestrel.web.fit import EMPTY_POSTING_STAGE_REASON, EMPTY_POSTING_WHY
from tests import role_seed

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


@pytest.fixture
def empty():
    oid = asyncio.run(role_seed.seed_async())["unscored"]

    async def mark():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                opp = await s.get(Opportunity, oid)
                opp.stage, opp.stage_reason = "t1_pass", EMPTY_POSTING_STAGE_REASON
                opp.archived_at = opp.archive_reason = None
                opp.description_md = ""
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(mark())
    return oid


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def test_the_role_page_says_not_scored_with_the_reason(empty):
    with _client() as c:
        html = c.get(f"/opportunities/{empty}").text
    assert "<h3>Not scored</h3>" in html and EMPTY_POSTING_WHY in html
    assert "Being scored" not in html and "Scoring failed" not in html
    assert "Put it back" not in html
    # The operator can force a score on a short posting they judge sufficient.
    assert 'data-testid="score-anyway"' in html and "Score anyway" in html


def test_the_panel_says_not_scored_and_offers_neither_retry_nor_put_back(empty):
    with _client() as c:
        html = c.get(f"/opportunities/{empty}/panel").text
    assert "Not scored" in html and EMPTY_POSTING_WHY in html
    assert "Being scored" not in html and "Scoring failed" not in html
    assert 'data-testid="panel-put-back"' not in html and "applied for" not in html
    assert 'data-testid="panel-score-anyway"' in html and "Score anyway" in html
    assert "Open Needs another try" not in html


def test_the_pipeline_row_names_the_reason_and_needs_no_retry(empty):
    with _client() as c:
        html = c.get("/opportunities").text
    i = html.index(f'data-id="{empty}"')
    row = html[i:i + 4000]
    assert 'data-scoring="hidden"' in row and EMPTY_POSTING_WHY in row


def test_the_pipeline_groups_it_as_not_scored_not_being_scored(empty):
    with _client() as c:
        html = c.get("/opportunities").text
    assert 'data-group="unscored"' in html and "Not scored · 1" in html
    assert 'data-group="new"' not in html and "Being scored" not in html


def test_the_orphan_sweep_leaves_it_alone(empty):
    from kestrel.pipeline.orphans import requeue_orphans

    async def go():
        engine = make_async_engine(paths.db_path())
        try:
            return await requeue_orphans(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    out = asyncio.run(go())
    assert out["requeued"] == 0 and out["stuck"] == 0
