"""Live search on the Applications page, driven in a real browser: type to narrow, Escape to
clear, the empty state and Clear filters, Show closed, and the query surviving a switch to the
table. Server-side rendering is covered in test_pursuit_filters.py. Skips only where no browser
starts (tests/browser.py)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import make_async_engine, reset_engine
from kestrel.db.models import Opportunity, Org, Pursuit
from tests.browser import launch_sync

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_ROWS = [
    ("Northwind Freight", "Head of IT", "shortlisted"),
    ("Globex Corp", "Platform Lead", "applied"),
    ("Initech", "Security Director", "interviewing"),
    ("Umbrella Labs", "Ops Manager", "applied"),
    ("Hooli", "Data Chief", "rejected"),
]


async def _seed() -> None:
    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        for i, (company, title, status) in enumerate(_ROWS):
            org = Org(name=company, normalized_name=company.lower())
            s.add(org)
            await s.flush()
            opp = Opportunity(
                track="employment", opp_type="job_posting", title=title, org_id=org.id,
                org_name=company, location_raw="Remote (US)", url=f"https://x.example/{i}",
                stage="t2_done",
            )
            s.add(opp)
            await s.flush()
            s.add(Pursuit(opportunity_id=opp.id, track="employment", status=status))
        await s.commit()
    await engine.dispose()


def test_applications_live_search_in_a_browser():
    from playwright.sync_api import expect, sync_playwright

    from kestrel.web.serve import running_server

    asyncio.run(_seed())
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(f"pageerror:{e}"))

        page.goto(f"{base}/pursuits?view=kanban")
        cards = page.locator("#board .pcard")
        count = page.get_by_test_id("filter-count")
        search = page.get_by_test_id("pursuit-search")
        expect(cards.locator("visible=true")).to_have_count(5)
        expect(count).to_have_text("Showing 5 of 5")

        # typing a company narrows to that one card
        search.fill("northwind")
        expect(cards.locator("visible=true")).to_have_count(1)
        expect(cards.locator("visible=true")).to_contain_text("Northwind Freight")
        expect(count).to_have_text("Showing 1 of 5")

        # Escape clears the box and everything returns
        search.press("Escape")
        expect(search).to_have_value("")
        expect(cards.locator("visible=true")).to_have_count(5)
        expect(count).to_have_text("Showing 5 of 5")

        # nonsense shows the empty state; Clear filters restores
        search.fill("zzqxnothing")
        empty = page.get_by_test_id("filter-empty")
        expect(empty).to_be_visible()
        expect(count).to_have_text("Showing 0 of 5")
        page.get_by_test_id("clear-filters").click()
        expect(empty).to_be_hidden()
        expect(search).to_have_value("")
        expect(cards.locator("visible=true")).to_have_count(5)

        # Show closed off hides the rejected card
        page.get_by_test_id("show-closed").uncheck()
        expect(cards.locator("visible=true")).to_have_count(4)
        expect(count).to_have_text("Showing 4 of 5")
        expect(page.locator("#board .pcard", has_text="Hooli")).to_be_hidden()
        page.get_by_test_id("show-closed").check()
        expect(cards.locator("visible=true")).to_have_count(5)

        # the query follows a switch to the table
        search.fill("globex")
        expect(cards.locator("visible=true")).to_have_count(1)
        page.get_by_role("link", name="Table").click()
        page.wait_for_url("**view=table**")
        assert "q=globex" in page.url
        expect(page.get_by_test_id("pursuit-search")).to_have_value("globex")
        rows = page.locator("#board tr[data-search]")
        expect(rows.locator("visible=true")).to_have_count(1)
        expect(rows.locator("visible=true")).to_contain_text("Globex Corp")

        browser.close()
    assert errors == [], f"JS errors on page: {errors}"
