"""Tests for the six opportunity-detail actions added in kestrel/web/pages/opportunity_actions.py:
Shortlist, Tailor, Research, Find contacts, Prep pack, Archive. Escalate to Tier 3 and the override
control are pre-existing and untouched here.

Follows the pattern in tests/test_web_api.py: a throwaway engine seeds a per-test sqlite file, the
cached engine is forgotten so TestClient's app builds its own on its own loop, and post-request DB
assertions open a brand-new engine (never the cached one) to avoid the cross-loop aiosqlite trap.

kestrel.web.app._PAGE_MODULES does not list "opportunity_actions" (wiring the module list is the
parent's job, not this module's -- kestrel/web/app.py is out of bounds here), so _client() patches
the tuple for the duration of each test via monkeypatch rather than editing the file. This still
exercises the real build_app(): TrustedHostMiddleware, static mount, every other router.

research_org and discover_contacts are network-touching (they may fetch public pages). Every test
here monkeypatches them at their import site in opportunity_actions, so no test ever calls a live
endpoint; acquire_excerpts runs for real but is only ever invoked with http=None (the documented
fully-offline path), so it never touches the network either."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import Contact, DocMaster, Opportunity, Org, OrgResearch, PrepPack, Pursuit

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _wipe_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = paths.db_path().with_name(paths.db_path().name + suffix)
        p.unlink(missing_ok=True)


async def _seed() -> dict[str, int]:
    await reset_engine()
    _wipe_db()
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        org = Org(name="Beacon Robotics", normalized_name="beacon robotics")
        s.add(org)
        await s.flush()

        opp = Opportunity(
            track="employment", opp_type="job_posting", title="Head of Data",
            org_id=org.id, org_name=org.name, url="https://x/employment-1",
            stage="t2_done", ghost_score=0.1,
        )
        s.add(opp)

        consulting_opp = Opportunity(
            track="consulting", opp_type="rfp", title="Fractional CTO engagement",
            org_id=org.id, org_name=org.name, url="https://x/consulting-1",
            stage="t2_done", ghost_score=0.1,
        )
        s.add(consulting_opp)

        master = DocMaster(track="employment", name="Standard Resume", doc_kind="resume")
        s.add(master)

        await s.commit()
        ids = {
            "org_id": org.id,
            "opp_id": opp.id,
            "consulting_opp_id": consulting_opp.id,
            "master_id": master.id,
        }
    await engine.dispose()
    forget_engine()  # TestClient builds its own engine on its own loop
    return ids


async def _pursuit(opp_id: int) -> Pursuit | None:
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        row = (
            await s.execute(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
        ).scalar_one_or_none()
    await engine.dispose()
    return row


async def _pursuit_count(opp_id: int) -> int:
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        n = (
            await s.execute(
                select(func.count()).select_from(Pursuit).where(Pursuit.opportunity_id == opp_id)
            )
        ).scalar_one()
    await engine.dispose()
    return n


async def _prep_pack_count(opp_id: int) -> int:
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        n = (
            await s.execute(
                select(func.count()).select_from(PrepPack).where(
                    PrepPack.opportunity_id == opp_id
                )
            )
        ).scalar_one()
    await engine.dispose()
    return n


async def _archived_at(opp_id: int) -> str | None:
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        opp = await s.get(Opportunity, opp_id)
    await engine.dispose()
    return opp.archived_at if opp else None


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from kestrel.web import app as app_module

    if "opportunity_actions" not in app_module._PAGE_MODULES:
        monkeypatch.setattr(
            app_module, "_PAGE_MODULES", app_module._PAGE_MODULES + ("opportunity_actions",)
        )
    return TestClient(app_module.build_app(worker=False))


# ============================================================================ 1. Shortlist
def test_shortlist_creates_pursuit_and_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = asyncio.run(_seed())
    with _client(monkeypatch) as c:
        r1 = c.post(f"/opportunities/{ids['opp_id']}/shortlist")
        assert r1.status_code == 200
        assert "Added to your shortlist" in r1.text

        r2 = c.post(f"/opportunities/{ids['opp_id']}/shortlist")  # second click: no duplicate
        assert r2.status_code == 200
        assert "Already on your shortlist" in r2.text

    assert asyncio.run(_pursuit_count(ids["opp_id"])) == 1
    row = asyncio.run(_pursuit(ids["opp_id"]))
    assert row is not None
    assert row.track == "employment"
    assert row.status == "shortlisted"


def test_shortlist_uses_the_consulting_track_status_not_employment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PURSUIT_STATUS differs per track; the route must look up the track's own first status
    rather than assuming the employment value applies everywhere."""
    from kestrel.db.enums import PURSUIT_STATUS

    ids = asyncio.run(_seed())
    with _client(monkeypatch) as c:
        r = c.post(f"/opportunities/{ids['consulting_opp_id']}/shortlist")
        assert r.status_code == 200

    row = asyncio.run(_pursuit(ids["consulting_opp_id"]))
    assert row is not None
    assert row.track == "consulting"
    assert row.status == PURSUIT_STATUS["consulting"][0]


# ============================================================================ 2. Tailor
def test_tailor_lists_masters_for_the_opportunitys_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = asyncio.run(_seed())
    with _client(monkeypatch) as c:
        r = c.get(f"/opportunities/{ids['opp_id']}/tailor")
        assert r.status_code == 200
        assert "Standard Resume" in r.text
        assert (
            f"/renders/new?master_id={ids['master_id']}&opportunity_id={ids['opp_id']}" in r.text
        )

        # The consulting opportunity has no matching-track master seeded.
        r2 = c.get(f"/opportunities/{ids['consulting_opp_id']}/tailor")
        assert r2.status_code == 200
        assert "No active document masters" in r2.text


# ============================================================================ 3. Research
def test_research_queues_and_returns_without_fetching(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = asyncio.run(_seed())

    async def fake_research_org(session, client, org, *, settings, http=None, operator_paste=None):
        return OrgResearch(
            org_id=org.id, summary_md="ok",
            signals=[{"category": "hiring", "statement": "opening a new office"}],
            insufficient_sources=False,
        )

    monkeypatch.setattr(
        "kestrel.research.jobs.research_org", fake_research_org
    )
    with _client(monkeypatch) as c:
        r = c.post(f"/opportunities/{ids['opp_id']}/research")
        assert r.status_code == 200
        assert "Queued" in r.text and "Organization tab" in r.text


def test_research_backend_failure_cannot_block_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = asyncio.run(_seed())

    async def fake_research_org(*args, **kwargs):
        raise RuntimeError("model backend unreachable")

    monkeypatch.setattr(
        "kestrel.research.jobs.research_org", fake_research_org
    )
    with _client(monkeypatch) as c:
        r = c.post(f"/opportunities/{ids['opp_id']}/research")
        assert r.status_code == 200
        assert "Queued" in r.text
        assert "model backend unreachable" not in r.text
        assert "Research failed" not in r.text


def test_research_without_a_linked_org_renders_inline_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_seed())
    ids2 = asyncio.run(_seed_no_org())

    async def unreachable(*args, **kwargs):
        raise AssertionError("research_org must not be called with no linked org")

    monkeypatch.setattr("kestrel.research.jobs.research_org", unreachable)
    with _client(monkeypatch) as c:
        r = c.post(f"/opportunities/{ids2['opp_id']}/research")
        assert r.status_code == 200
        assert "No organization is linked" in r.text


async def _seed_no_org() -> dict[str, int]:
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        opp = Opportunity(
            track="employment", opp_type="job_posting", title="No Org Role",
            url="https://x/no-org", stage="t2_done", ghost_score=0.1,
        )
        s.add(opp)
        await s.commit()
        opp_id = opp.id
    await engine.dispose()
    return {"opp_id": opp_id}


# ============================================================================ 4. Find contacts
def test_find_contacts_queues_without_fetching(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = asyncio.run(_seed())

    async def fake_discover_contacts(session, client, org, excerpts, *, opportunity=None,
                                      make_drafts=True):
        return [Contact(org_id=org.id, name="Jane Doe", contact_type="hiring_manager")]

    monkeypatch.setattr(
        "kestrel.research.jobs.discover_contacts", fake_discover_contacts
    )
    with _client(monkeypatch) as c:
        r = c.post(f"/opportunities/{ids['opp_id']}/contacts")
        assert r.status_code == 200
        assert "Queued" in r.text and "Organization tab" in r.text


def test_requests_do_not_run_or_log_model_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import structlog

    ids = asyncio.run(_seed())

    async def boom(*args, **kwargs):
        raise RuntimeError("model backend unreachable")

    monkeypatch.setattr("kestrel.research.jobs.research_org", boom)
    monkeypatch.setattr("kestrel.research.jobs.discover_contacts", boom)
    with _client(monkeypatch) as c, structlog.testing.capture_logs() as logs:
        c.post(f"/opportunities/{ids['opp_id']}/research")
        c.post(f"/opportunities/{ids['opp_id']}/contacts")
    assert not any(e["event"] in ("research_failed", "find_contacts_failed") for e in logs)


def test_contact_backend_failure_cannot_block_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = asyncio.run(_seed())

    async def fake_discover_contacts(*args, **kwargs):
        raise RuntimeError("quarantined: schema mismatch")

    monkeypatch.setattr(
        "kestrel.research.jobs.discover_contacts", fake_discover_contacts
    )
    with _client(monkeypatch) as c:
        r = c.post(f"/opportunities/{ids['opp_id']}/contacts")
        assert r.status_code == 200
        assert "Queued" in r.text
        assert "quarantined: schema mismatch" not in r.text


# ============================================================================ 5. Prep pack
def test_prep_pack_builds_and_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = asyncio.run(_seed())
    with _client(monkeypatch) as c:
        r = c.post(f"/opportunities/{ids['opp_id']}/prep")
        assert r.status_code == 200
        assert "Your interview prep is ready." in r.text
        assert 'data-testid="open-interview-prep"' in r.text and "btn primary" in r.text

    assert asyncio.run(_prep_pack_count(ids["opp_id"])) == 1


def test_prep_links_survive_other_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = asyncio.run(_seed())
    with _client(monkeypatch) as c:
        assert c.post(f"/opportunities/{ids['opp_id']}/prep").status_code == 200
        r = c.post(f"/opportunities/{ids['opp_id']}/shortlist")
        assert r.status_code == 200 and "Added to your shortlist" in r.text
        assert f'href="/mock/{ids["opp_id"]}"' in r.text
        assert 'data-testid="open-interview-prep"' in r.text


# ============================================================================ 6. Archive
def test_archive_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = asyncio.run(_seed())
    with _client(monkeypatch) as c:
        r1 = c.post(f"/opportunities/{ids['opp_id']}/archive")
        assert r1.status_code == 200 and "Hidden" in r1.text

        r2 = c.post(f"/opportunities/{ids['opp_id']}/archive")  # idempotent second click
        assert r2.status_code == 200 and "Already hidden" in r2.text

        r3 = c.post(f"/opportunities/{ids['opp_id']}/unarchive")
        assert r3.status_code == 200 and "Unhidden" in r3.text

        r4 = c.post(f"/opportunities/{ids['opp_id']}/unarchive")  # idempotent second click
        assert r4.status_code == 200 and "Not hidden" in r4.text

    assert asyncio.run(_archived_at(ids["opp_id"])) is None


# ============================================================================ never-500 guard
def test_unknown_opportunity_never_500s_on_any_action(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_seed())
    bogus = 999999
    with _client(monkeypatch) as c:
        calls = [
            ("post", f"/opportunities/{bogus}/shortlist"),
            ("get", f"/opportunities/{bogus}/tailor"),
            ("post", f"/opportunities/{bogus}/research"),
            ("post", f"/opportunities/{bogus}/contacts"),
            ("post", f"/opportunities/{bogus}/prep"),
            ("post", f"/opportunities/{bogus}/archive"),
            ("post", f"/opportunities/{bogus}/unarchive"),
        ]
        for method, path in calls:
            r = getattr(c, method)(path)
            assert r.status_code == 200, f"{method} {path} -> {r.status_code}"
            assert "not found" in r.text.lower(), f"{method} {path} -> {r.text}"


def test_detail_page_shows_document_masters_on_first_render(monkeypatch):
    """The actions partial needs `masters`, which get_detail does not supply. Without the merge in
    routes_pages the FIRST render of every opportunity claimed there were no document masters, and
    the claim silently corrected itself as soon as the operator clicked any button. A false negative
    that repairs itself on interaction is the kind a test has to catch, because a human clicks
    before they read."""
    ids = asyncio.run(_seed())  # seeds one active employment DocMaster
    with _client(monkeypatch) as c:
        r = c.get(f"/opportunities/{ids['opp_id']}")
    assert r.status_code == 200
    assert "No active document masters" not in r.text
    with _client(monkeypatch) as c:
        c.post(f"/opportunities/{ids['opp_id']}/role/shortlist")
        tab = c.get(f"/opportunities/{ids['opp_id']}/tab/documents")
    assert "/renders/new?master_id=" in tab.text


def test_detail_page_keeps_confidential_redaction_after_the_actions_merge(monkeypatch):
    """The actions context deliberately does NOT return opp/org. Merging those in would overwrite
    the rows the detail route has already redacted for confidential mode and put the real employer
    name back on the page, which is a privacy regression rather than a cosmetic one."""
    from kestrel.web.pages.opportunity_actions import action_context

    ids = asyncio.run(_seed())

    async def _keys():
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            ctx = await action_context(s, ids["opp_id"])
        await engine.dispose()
        return set(ctx)

    keys = asyncio.run(_keys())
    assert keys == {"pursuit", "pursuit_column", "masters"}
    assert not keys & {"opp", "org"}


def test_archived_opportunities_leave_the_active_pipeline(monkeypatch):
    """Archive promises "hidden from the active pipeline". Filtering only the list query would
    leave archived items still flowing through Tier 0 into the tiers, burning LLM calls on work the
    operator explicitly set aside, and still showing on the dashboard's "awaiting you" panel."""
    import inspect

    from kestrel.analytics.today import review_queue
    from kestrel.pipeline import tier0

    assert "archived_at.is_(None)" in inspect.getsource(tier0.run_tier0_batch), (
        "Tier 0 still sweeps archived opportunities"
    )
    # The landing page is Today (kestrel.web.pages.dashboard re-exports it); its review queue is
    # built by kestrel.analytics.today.review_queue.
    assert "archived_at.is_(None)" in inspect.getsource(review_queue), (
        "Today's review queue still surfaces hidden opportunities"
    )


def test_research_action_leaves_the_shared_service_to_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = asyncio.run(_seed())
    seen: list[str] = []

    import kestrel.services.org_research as svc

    real = svc.run_org_research

    async def spy(session, org, **kw):
        seen.append(org.name)
        return await real(session, org, **kw)

    monkeypatch.setattr("kestrel.services.org_research.run_org_research", spy)

    async def fake_research_org(session, client, org, *, settings, http=None, operator_paste=None):
        return OrgResearch(org_id=org.id, summary_md=None, signals=[], insufficient_sources=True)

    monkeypatch.setattr("kestrel.research.jobs.research_org", fake_research_org)
    with _client(monkeypatch) as c:
        r = c.post(f"/opportunities/{ids['opp_id']}/research")
        assert r.status_code == 200
        assert "Queued" in r.text
    assert seen == []
