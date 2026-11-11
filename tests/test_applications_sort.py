"""#137: the Applications table sorts every application on the server, then pages (D-PERF-3), and
the sort is remembered with the view in a cookie the server reads. Offline: a migrated SQLite file
and the app in a test client; every title and date here is invented."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import make_async_engine, reset_engine
from kestrel.db.models import Opportunity, Pursuit
from kestrel.services.tracks import forget_cache
from kestrel.web import app as app_module
from kestrel.web.pages import pursuits as page

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

N = 130  # more than one table page (100 rows)
# Pipeline order of the employment columns: to apply, applied, interviewing, closed.
RANK = {"shortlisted": 0, "applied": 1, "interviewing": 2, "rejected": 3}


def _run(fn):
    async def _go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _client() -> TestClient:
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_cache()

    async def go(s):
        await s.execute(insert(Opportunity), [
            {"track": "employment", "opp_type": "job_posting",
             "title": f"{'Zeta' if i % 2 else 'alpha'} Role {i:03d}", "org_name": "Acme",
             "url": f"https://x.example/{i}", "stage": "t2_done"}
            for i in range(N)
        ])
        await s.commit()
        ids = [r[0] for r in (await s.execute(
            Opportunity.__table__.select().with_only_columns(Opportunity.id)
            .order_by(Opportunity.id))).all()]
        statuses = list(RANK)
        rows = []
        for i, oid in enumerate(ids):
            rows.append({
                "opportunity_id": oid, "track": "employment", "status": statuses[i % 4],
                # Every fifth role has no applied date; the rest have one each day.
                "applied_at": None if i % 5 == 0 else f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}",
                "last_activity_at": f"2026-11-{1 + i % 9:02d}T10:00:00+00:00",
            })
        await s.execute(insert(Pursuit), rows)
        await s.commit()

    _run(go)
    return TestClient(app_module.build_app(worker=False))


def _rows(html: str) -> list[dict]:
    out = []
    for m in re.finditer(r'<tr class="ap-row"[^>]*data-id="(\d+)"[^>]*'
                         r'data-col="(\w+)"[^>]*data-applied="([^"]*)".*?<b>([^<]*)</b>',
                         html, re.S):
        out.append({"id": int(m.group(1)), "col": m.group(2), "applied": m.group(3),
                    "title": m.group(4)})
    return out


def _all_pages(c: TestClient, query: str) -> list[dict]:
    first = c.get(f"/pursuits?view=table&{query}").text
    pages = int(re.search(r"Page 1 of (\d+)", first).group(1))
    rows = _rows(first)
    for n in range(2, pages + 1):
        rows += _rows(c.get(f"/pursuits?view=table&{query}&page={n}").text)
    return rows


def _empties_last(values: list[str]) -> bool:
    filled = [v != "" for v in values]
    return filled == sorted(filled, reverse=True)


def test_applied_sorts_over_every_page_newest_first_and_empty_dates_are_last():
    with _client() as c:
        rows = _all_pages(c, "sort=applied&dir=-1")
    days = [r["applied"] for r in rows]
    assert len(rows) == N and len({r["id"] for r in rows}) == N  # every row once, none lost
    filled = [d for d in days if d]
    assert filled == sorted(filled, reverse=True) and len(filled) > 100
    assert _empties_last(days) and days[-1] == ""  # the empty ones fall on page 2, last


def test_applied_oldest_first_still_puts_empty_dates_last():
    with _client() as c:
        rows = _all_pages(c, "sort=applied&dir=1")
    days = [r["applied"] for r in rows]
    filled = [d for d in days if d]
    assert filled == sorted(filled) and _empties_last(days) and len(rows) == N


def test_role_sorts_case_insensitively_across_pages_both_ways():
    with _client() as c:
        up = [r["title"] for r in _all_pages(c, "sort=role&dir=1")]
        down = [r["title"] for r in _all_pages(c, "sort=role&dir=-1")]
    assert up == sorted(up, key=str.casefold) and up[0].startswith("alpha")
    assert down == sorted(down, key=str.casefold, reverse=True) and down[0].startswith("Zeta")


def test_stage_follows_the_pipeline_not_the_alphabet():
    with _client() as c:
        up = _all_pages(c, "sort=stage&dir=1")
        down = _all_pages(c, "sort=stage&dir=-1")
    ranks = [{"to_apply": 0, "applied": 1, "interviewing": 2, "closed": 3}[r["col"]] for r in up]
    assert ranks == sorted(ranks) and len(set(ranks)) == 4
    ranks = [{"to_apply": 0, "applied": 1, "interviewing": 2, "closed": 3}[r["col"]] for r in down]
    assert ranks == sorted(ranks, reverse=True)


def test_the_row_count_and_totals_do_not_change_with_the_sort():
    with _client() as c:
        plain = c.get("/pursuits?view=table").text
        sorted_ = c.get("/pursuits?view=table&sort=applied&dir=-1").text
    assert re.search(r"130 in all", plain) and re.search(r"130 in all", sorted_)
    assert len(_rows(plain)) == len(_rows(sorted_)) == page.TABLE_PAGE_SIZE


def test_the_header_announces_the_sort_the_server_drew():
    with _client() as c:
        html = c.get("/pursuits?view=table&sort=applied&dir=-1").text
    assert re.search(r'data-sort="applied" aria-sort="descending"', html)
    assert re.search(r'data-sort="role" aria-sort="none"', html)
    assert 'data-sortcol="applied" data-sortdir="-1"' in html


# ---------------------------------------------------------------------- the remembered view
def test_a_saved_sort_cookie_is_drawn_on_first_paint():
    with _client() as c:
        c.cookies.set("kestrel_pursuits_sort", "applied_desc")
        html = c.get("/pursuits?view=table").text
    assert re.search(r'data-sort="applied" aria-sort="descending"', html)
    days = [r["applied"] for r in _rows(html)]
    assert [d for d in days if d] == sorted([d for d in days if d], reverse=True)


@pytest.mark.parametrize("junk", ["", "applied", "applied_sideways", "bogus_asc", "_desc", "%00"])
def test_a_bad_sort_cookie_means_no_sort_and_no_error(junk):
    with _client() as c:
        c.cookies.set("kestrel_pursuits_sort", junk)
        resp = c.get("/pursuits?view=table")
    assert resp.status_code == 200
    assert 'data-sortcol="" data-sortdir="1"' in resp.text
    assert 'aria-sort="none"' in resp.text and "ascending" not in resp.text


def test_a_sort_in_the_url_beats_the_cookie_and_an_empty_one_clears_it():
    with _client() as c:
        c.cookies.set("kestrel_pursuits_sort", "applied_desc")
        by_role = c.get("/pursuits?view=table&sort=role&dir=1").text
        none = c.get("/pursuits?view=table&sort=&dir=1").text
    assert re.search(r'data-sort="role" aria-sort="ascending"', by_role)
    assert 'data-sortcol="" ' in none


def test_a_pager_press_keeps_the_sort_and_a_card_action_redraws_in_the_same_order():
    with _client() as c:
        second = c.get("/pursuits?view=table&sort=applied&dir=-1&page=2",
                       headers={"HX-Request": "true"}).text
    assert 'data-page="2"' in second and 'data-sortcol="applied"' in second
    days = [r["applied"] for r in _rows(second)]
    assert days[-1] == "" and _empties_last(days)


def test_the_view_cookie_is_read_for_a_missing_view_and_the_url_wins():
    with _client() as c:
        c.cookies.set("kestrel_pursuits_view", "table")
        assert 'data-testid="apps-table"' in c.get("/pursuits").text
        assert 'data-testid="apps-board-grid"' in c.get("/pursuits?view=kanban").text
        c.cookies.set("kestrel_pursuits_view", "garbage")
        assert 'data-testid="apps-board-grid"' in c.get("/pursuits").text
    with _client() as c:  # no cookie at all
        assert 'data-testid="apps-board-grid"' in c.get("/pursuits").text


def test_the_sort_script_keeps_no_client_side_row_sort():
    """The server is the one answer: the script asks for the table, it never reorders rows."""
    js = (Path(paths.web_static_dir()) / "js" / "applications.js").read_text(encoding="utf-8")
    assert "sortVal" not in js and "rows.sort" not in js
    assert "sortcol" in js and "kestrel_pursuits_sort" in js
