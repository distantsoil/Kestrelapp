"""Roles hidden as applied before any model read them say Not scored, never Being scored (issue
#59). The applied-history import (D-IMPORT-1) and a re-post of a role already applied for
(SPEC.md 5.6) are hidden on arrival; Tier 0 skips hidden roles, so nothing will ever score them.
Before the fix the role page, the Pipeline panel and the Applications list all said Being scored."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db.base import utcnow_iso
from kestrel.db.engine import make_async_engine
from kestrel.db.models import Opportunity, Pursuit, Task
from kestrel.web.fit import IMPORTED_REASON, hidden_unscored_why
from tests import role_seed

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _opp(stage="new", archive_reason="applied", stage_reason=IMPORTED_REASON):
    return SimpleNamespace(stage=stage, archive_reason=archive_reason, stage_reason=stage_reason)


def test_the_reason_names_the_import_or_the_pursuit_it_repeats():
    assert hidden_unscored_why(_opp()) == "Imported from your applied history."
    assert hidden_unscored_why(_opp(stage_reason="Re-post of pursuit #12")) == (
        "Re-post of a role you applied to (pursuit #12).")
    assert hidden_unscored_why(_opp(stage_reason="Re-post of hidden opportunity #7")) == (
        "Re-post of a role you hid as applied (role #7).")
    assert hidden_unscored_why(_opp(stage_reason=None)) == (
        "Hidden as applied before it was scored.")


def test_a_role_the_pipeline_will_still_score_has_no_reason():
    assert hidden_unscored_why(_opp(archive_reason=None)) is None  # active: Tier 0 takes it
    assert hidden_unscored_why(_opp(archive_reason="not_for_me")) is None
    assert hidden_unscored_why(_opp(stage="t2_done")) is None  # scored after all


def _with_session(fn):
    async def run():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                out = await fn(s)
                await s.commit()
                return out
        finally:
            await engine.dispose()

    return asyncio.run(run())


@pytest.fixture
def hidden():
    """The seeded unscored role, hidden as applied by the import, with its applied pursuit."""
    ids = asyncio.run(role_seed.seed_async())
    oid = ids["unscored"]

    async def mark(s):
        opp = await s.get(Opportunity, oid)
        opp.stage = "new"  # as the import and normalize leave it
        opp.archived_at = utcnow_iso()
        opp.archive_reason = "applied"
        opp.stage_reason = IMPORTED_REASON
        p = Pursuit(opportunity_id=oid, track=opp.track, status="applied",
                    applied_at=utcnow_iso(), last_activity_at=utcnow_iso())
        s.add(p)
        await s.flush()
        return p.id

    return oid, _with_session(mark)


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def test_the_role_page_says_not_scored_with_the_reason_and_offers_no_score_again(hidden):
    oid, _ = hidden
    with _client() as c:
        html = c.get(f"/opportunities/{oid}").text
    assert 'data-testid="not-scored"' in html
    assert "<h3>Not scored</h3>" in html
    assert "Imported from your applied history. Kestrel does not score roles you already" in html
    assert "Being scored" not in html
    assert 'data-testid="score-anyway"' not in html


def test_a_re_post_names_the_pursuit_it_repeats(hidden):
    oid, _ = hidden

    async def repost(s):
        (await s.get(Opportunity, oid)).stage_reason = "Re-post of pursuit #12"

    _with_session(repost)
    with _client() as c:
        html = c.get(f"/opportunities/{oid}").text
    assert "Re-post of a role you applied to (pursuit #12)." in html
    assert "Being scored" not in html


def test_score_again_queued_makes_it_being_scored_after_all(hidden):
    oid, _ = hidden

    async def queue(s):
        s.add(Task(type="t2", payload={"opportunity_id": oid}))

    _with_session(queue)
    with _client() as c:
        html = c.get(f"/opportunities/{oid}").text
    assert "Being scored" in html and "Imported from your applied history" not in html


def test_the_pipeline_panel_says_not_scored(hidden):
    oid, _ = hidden
    with _client() as c:
        html = c.get(f"/opportunities/{oid}/panel").text
    assert "Not scored" in html and 'data-testid="panel-hidden-why"' in html
    assert "Imported from your applied history." in html
    assert 'data-testid="panel-put-back"' in html
    assert "Being scored" not in html


def test_a_re_post_with_its_embed_task_still_says_not_scored(hidden):
    """A re-post gets an embed task (normalize), but embedding never leads to a score."""
    oid, _ = hidden

    async def repost(s):
        (await s.get(Opportunity, oid)).stage_reason = "Re-post of pursuit #12"
        s.add(Task(type="embed", payload={"opportunity_id": oid}))

    _with_session(repost)
    with _client() as c:
        panel = c.get(f"/opportunities/{oid}/panel").text
        page = c.get(f"/opportunities/{oid}").text
    assert "Re-post of a role you applied to (pursuit #12)." in panel
    assert "Re-post of a role you applied to (pursuit #12)." in page
    assert "Being scored" not in panel and "Being scored" not in page


def test_the_pipeline_hidden_list_row_says_not_scored_with_the_reason(hidden):
    oid, _ = hidden
    with _client() as c:
        html = c.get("/opportunities?hidden=1").text
    i = html.index(f'data-id="{oid}"')
    row = html[i:html.index("</article>", i) if "</article>" in html[i:] else i + 4000]
    assert 'data-scoring="hidden"' in row and ">not scored<" in row
    assert "Imported from your applied history." in row


def test_the_reason_matches_what_normalize_writes():
    """suppress_if_applied writes the re-post reason; the helper parses it back."""
    import inspect

    from kestrel.ingest import normalize

    src = inspect.getsource(normalize.suppress_if_applied)
    assert 'f"Re-post of pursuit #{pursuit_id}"' in src
    assert 'f"Re-post of hidden opportunity #{opp_id}"' in src


def test_the_applications_list_says_not_scored(hidden):
    _, pid = hidden
    with _client() as c:
        page = c.get("/pursuits").text
        panel = c.get(f"/pursuits/{pid}/panel").text
    assert "Not scored" in page and "Being scored" not in page
    assert 'title="Imported from your applied history."' in page
    assert "Not scored" in panel and "Being scored" not in panel


def test_applications_says_being_scored_once_score_again_is_queued(hidden):
    oid, pid = hidden

    async def queue(s):
        s.add(Task(type="rescore", payload={"opportunity_id": oid}))

    _with_session(queue)
    with _client() as c:
        page = c.get("/pursuits").text
    assert "Being scored" in page and "Not scored" not in page


def test_a_lead_speaks_of_leads():
    lead = SimpleNamespace(stage="new", archive_reason="applied", track="consulting",
                           stage_reason="Re-post of pursuit #3")
    assert hidden_unscored_why(lead) == "Re-post of a lead you applied to (pursuit #3)."
