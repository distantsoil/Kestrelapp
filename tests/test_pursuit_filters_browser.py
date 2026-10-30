"""Live search on the Applications page, driven in a real browser: type to narrow, Escape to
clear, the empty state and Clear filters, Show closed, and the query surviving a switch to the
table. Server-side rendering is covered in test_pursuit_filters.py. Skips only where no browser
starts (tests/browser.py)."""

from __future__ import annotations

import asyncio
import re

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


async def _seed(rows: list[tuple[str, str, str]] | None = None) -> None:
    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        for i, (company, title, status) in enumerate(rows or _ROWS):
            org = Org(name=company, normalized_name=company.lower())
            s.add(org)
            await s.flush()
            opp = Opportunity(
                track="employment",
                opp_type="job_posting",
                title=title,
                org_id=org.id,
                org_name=company,
                location_raw="Remote (US)",
                url=f"https://x.example/{i}",
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
        page = browser.new_page(viewport={"width": 1680, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(f"pageerror:{e}"))

        page.goto(f"{base}/pursuits?view=kanban")
        cards = page.locator("#apps-board .ap-card:not([hidden])")
        count = page.get_by_test_id("filter-count")
        search = page.get_by_test_id("pursuit-search")
        # The Closed group is folded by default (D-UI-100): the count is what is on screen.
        expect(cards).to_have_count(5)
        expect(count).to_have_text("Showing 4 of 5")
        toggle = page.get_by_test_id("closed-toggle")
        expect(toggle).to_contain_text("Closed · 1")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(count).to_have_text("Showing 5 of 5")
        toggle.click()
        expect(count).to_have_text("Showing 4 of 5")

        # typing a company narrows to that one card
        search.fill("northwind")
        expect(cards).to_have_count(1)
        expect(cards).to_contain_text("Northwind Freight")
        expect(count).to_have_text("Showing 1 of 5")

        # Escape clears the box and everything returns
        search.press("Escape")
        expect(search).to_have_value("")
        expect(cards).to_have_count(5)
        expect(count).to_have_text("Showing 4 of 5")

        # nonsense shows the empty state; Clear filters restores
        search.fill("zzqxnothing")
        empty = page.get_by_test_id("filter-empty")
        expect(empty).to_be_visible()
        expect(count).to_have_text("Showing 0 of 5")
        page.get_by_test_id("clear-filters").click()
        expect(empty).to_be_hidden()
        expect(search).to_have_value("")
        expect(cards).to_have_count(5)

        # a search that is looking for a closed card opens the fold so the match can be seen
        search.fill("hooli")
        expect(page.locator(".ap-closedg")).to_have_class(re.compile("is-open"))
        expect(cards).to_have_count(1)
        expect(count).to_have_text("Showing 1 of 5")
        search.fill("")
        expect(page.locator(".ap-closedg")).not_to_have_class(re.compile("is-open"))

        # a stage chip shows only that column
        page.locator('[data-testid="stage-chip"][data-stage="applied"]').click()
        expect(cards).to_have_count(2)
        expect(page.locator('.ap-col[data-col="to_apply"]')).to_be_hidden()
        page.locator('[data-testid="stage-chip"][data-stage="all"]').click()
        expect(cards).to_have_count(5)

        # the query follows a switch to the table, in place
        search.fill("globex")
        expect(cards).to_have_count(1)
        page.get_by_test_id("view-table").click()
        page.wait_for_selector('[data-testid="apps-table"]')
        # The URL is pushed just after the swap, so wait for it rather than read it at once.
        expect(page).to_have_url(re.compile(r"q=globex.*view=table|view=table.*q=globex"))
        expect(page.get_by_test_id("pursuit-search")).to_have_value("globex")
        rows = page.locator("#apps-board tr[data-search]:not([hidden])")
        expect(rows).to_have_count(1)
        expect(rows).to_contain_text("Globex Corp")

        browser.close()
    assert errors == [], f"JS errors on page: {errors}"


_OLD = [
    # (company, title, status, days since last activity)
    ("Northwind Freight", "Head of IT", "shortlisted", 3),
    ("Globex Corp", "Platform Lead", "applied", 5),
    ("Initech", "Security Director", "applied", 20),
    ("Umbrella Labs", "Ops Manager", "applied", 60),
    ("Hooli", "Data Chief", "applied", 200),
    ("Acme Inc", "Infra Lead", "applied", 1),
    ("Wayne Corp", "Site Head", "applied", 2),
    ("Stark Labs", "Systems Chief", "applied", 4),
]


async def _seed_aged() -> None:
    from datetime import UTC, datetime, timedelta

    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    now = datetime.now(UTC)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        for i, (company, title, status, age) in enumerate(_OLD):
            org = Org(name=company, normalized_name=company.lower())
            s.add(org)
            await s.flush()
            opp = Opportunity(
                track="employment",
                opp_type="job_posting",
                title=title,
                org_id=org.id,
                org_name=company,
                location_raw="Remote (US)",
                url=f"https://x.example/{i}",
                stage="t2_done",
            )
            s.add(opp)
            await s.flush()
            when = (now - timedelta(days=age)).isoformat()
            s.add(
                Pursuit(
                    opportunity_id=opp.id,
                    track="employment",
                    status=status,
                    applied_at=when,
                    last_activity_at=when,
                )
            )
        await s.commit()
    await engine.dispose()


def test_date_range_and_column_cap_limit_a_long_board_and_the_choice_persists():
    from playwright.sync_api import expect, sync_playwright

    from kestrel.web.serve import running_server

    asyncio.run(_seed_aged())
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        page = browser.new_page(viewport={"width": 1680, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(f"pageerror:{e}"))

        page.goto(f"{base}/pursuits?view=kanban")
        cards = page.locator("#apps-board .ap-card:not([hidden])")
        count = page.get_by_test_id("filter-count")
        expect(cards).to_have_count(8)
        page.get_by_test_id("more-filters").click()

        # the date range hides cards whose last activity is older than the range
        with page.expect_response(lambda r: "/pursuits/prefs" in r.url and r.ok):
            page.get_by_test_id("range-select").select_option("30")
        expect(cards).to_have_count(6)
        expect(count).to_have_text("Showing 6 of 8")
        assert "range=30" in page.url
        # Wait for the choice to be saved before leaving (the slower Windows runner, #14 CI).
        with page.expect_response(
            lambda r: (
                "/pursuits/prefs" in r.url and r.ok and "range=7" in (r.request.post_data or "")
            )
        ):
            page.get_by_test_id("range-select").select_option("7")
        expect(cards).to_have_count(5)

        # the choice is the default next visit, with no query string
        page.goto(f"{base}/pursuits?view=kanban")
        expect(page.get_by_test_id("range-select")).to_have_value("7")
        expect(page.locator("#apps-board .ap-card:not([hidden])")).to_have_count(5)
        # All time brings everything back (and is remembered)
        page.get_by_test_id("more-filters").click()
        page.get_by_test_id("range-select").select_option("0")
        expect(cards).to_have_count(8)

        # a per-column cap shows the first N with a Show all for the rest
        page.get_by_test_id("cap-select").select_option("5")
        applied = page.locator('#apps-board .ap-card[data-col="applied"]:not([hidden])')
        expect(applied).to_have_count(5)
        more = page.locator('#apps-board [data-colmore][data-col="applied"]')
        expect(more).to_be_visible()
        expect(more).to_have_text("Show all 7")
        more.click()
        expect(applied).to_have_count(7)
        expect(more).to_have_text("Show fewer")

        browser.close()
    assert errors == [], f"JS errors on page: {errors}"


def test_an_open_offer_is_never_folded_away_as_closed():
    # An offer sits in the employment "closed" column but is not a closed status (#30 review):
    # it stays in the column itself while only rejected, withdrawn and ghosted fold.
    from playwright.sync_api import expect, sync_playwright

    from kestrel.web.serve import running_server

    asyncio.run(
        _seed(
            [
                ("Acme", "Head of IT", "offer"),
                ("Hooli", "Data Chief", "rejected"),
                ("Globex Corp", "Platform Lead", "applied"),
            ]
        )
    )
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        page = browser.new_page(viewport={"width": 1680, "height": 900})
        page.goto(f"{base}/pursuits?view=kanban")
        expect(page.locator("#apps-board .ap-card:not([hidden])")).to_have_count(3)
        expect(page.locator(".ap-closedg .ap-card")).to_have_count(1)
        expect(page.locator(".ap-closedg .ap-card", has_text="Hooli")).to_have_count(1)
        expect(page.locator(".ap-cards .ap-card", has_text="Acme").first).to_be_visible()
        expect(page.get_by_test_id("closed-toggle")).to_contain_text("Closed · 1")
        browser.close()
