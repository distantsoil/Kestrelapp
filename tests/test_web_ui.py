"""GATE (Phase 6): drive the real review UI in a browser. Seed fixture data, load /opportunities,
filter, open a detail, submit an override, and confirm the verdict_override row exists and the UI
reflects it — with no uncaught JS errors on any page.

The server runs on its own thread/loop with the app-wide engine; this test seeds and verifies with
its OWN throwaway engine against the same WAL database file, so no engine is shared across loops
(the cross-loop aiosqlite trap). Skips cleanly if Edge/Playwright cannot launch (e.g. cloud CI)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import make_async_engine, reset_engine
from kestrel.db.models import Evaluation, Opportunity, Org, Source, VerdictOverride
from tests.browser import launch_sync

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _wipe_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)


async def _seed() -> int:
    await reset_engine()  # release any cached app engine so the server creates its own on its loop
    _wipe_db()  # fresh db: _isolate_data_home gives this test its OWN app-db file (function-scoped)
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        org = Org(name="Acme Corp", normalized_name="acme corp", sector="technology")
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        s.add_all([org, src])
        await s.flush()
        opp = Opportunity(
            track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
            title="VP of Information Technology", org_name="Acme Corp", url="https://x/1",
            description_md="Own IT strategy and security for a services firm.", stage="t2_done",
            ghost_score=0.1, comp_min=200000, comp_max=250000, comp_currency="USD",
            comp_interval="year",
        )
        s.add(opp)
        await s.flush()
        opp_id = opp.id
        s.add(
            Evaluation(
                opportunity_id=opp.id, tier=2, track="employment", verdict="apply",
                verdict_model="apply", score=4.3, weighted_score=4.2, needs_review=False,
                dimensions={"role_fit": {"score": 4.5, "why": "strong ownership"},
                            "domain_match": {"score": 4.0, "why": "good overlap"}},
                blocks={"a_role_summary": {"what_this_actually_is": "A real IT leadership role."}},
                blockers=[], legitimacy={"status": "clean", "signals": []},
            )
        )
        await s.commit()
    await engine.dispose()
    return opp_id


async def _override_count(opp_id: int) -> int:
    engine = make_async_engine(paths.db_path())
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        n = (
            await s.execute(
                select(func.count())
                .select_from(VerdictOverride)
                .where(VerdictOverride.opportunity_id == opp_id)
            )
        ).scalar_one()
    await engine.dispose()
    return n


def test_review_ui_end_to_end():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    opp_id = asyncio.run(_seed())

    with running_server(worker=False) as base:
        with sync_playwright() as p:
            browser = launch_sync(p)
            page = browser.new_page()
            errors: list[str] = []

            def _console(m):
                if m.type == "error":
                    errors.append(f"console:{m.text}")

            page.on("console", _console)
            page.on("pageerror", lambda e: errors.append(f"pageerror:{e}"))

            # 1. the triage list shows the seeded opportunity as a striped row
            page.goto(f"{base}/opportunities")
            page.wait_for_selector("text=VP of Information Technology")
            assert "A real IT leadership role." in page.content()  # the summary line

            # 2. a chip narrows server-side (Your call removes it; clicking again restores)
            page.click("[data-chip=needs_review]")
            page.wait_for_selector("text=No opportunities match these chips")
            page.click("[data-chip=needs_review]")
            page.wait_for_selector("text=VP of Information Technology")

            # 3. a row click opens the drawer with the fit card and the two primary actions
            page.click(f"#row-{opp_id}")
            page.wait_for_selector("#drawer-card")
            assert "of 5" in page.inner_text("[data-testid=fit-line]")
            assert page.is_visible("[data-testid=add-to-list]")
            assert page.is_visible("[data-testid=not-for-me]")

            # 4. Add to my list: the panel and the row both change, and the agree label lands
            page.click("[data-testid=add-to-list]")
            page.wait_for_selector("[data-testid=on-my-list]")
            page.wait_for_selector(f"#row-{opp_id} [data-testid=on-shortlist]")
            assert "on my shortlist" in page.inner_text(f"#row-{opp_id} [data-testid=on-shortlist]")

            # 5. the full page is one click away
            page.click("[data-testid=drawer-open]")
            page.wait_for_url(f"**/opportunities/{opp_id}")

            browser.close()

    # 5. the agree label from Add to my list exists, and the UI reflected it (above)
    assert asyncio.run(_override_count(opp_id)) == 1
    assert errors == [], f"JS errors on page: {errors}"
