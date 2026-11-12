"""D-PERF-3 item 2: the Applications board draws every open card and holds back only the Closed
ones beyond 100 per column behind "Show more", and the table draws 100 rows a page. Nothing is
dropped: totals, chips and counts cover every pursuit, and Show more returns exactly the rest.
Offline: a migrated SQLite file and the app in a test client."""

from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import make_async_engine, reset_engine
from kestrel.db.models import Opportunity, Pursuit
from kestrel.services import pursuit_board as pb
from kestrel.services.tracks import forget_cache
from kestrel.web import app as app_module
from kestrel.web.pages import pursuits as page

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

CLOSED_STATUSES = pb.FOLDED_STATUSES["employment"]  # rejected, withdrawn, ghosted


def _run(fn):
    async def _go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _fresh() -> TestClient:
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_cache()
    return TestClient(app_module.build_app(worker=False))


def _seed_many(open_n: int, closed_n: int) -> None:
    """`open_n` shortlisted and `closed_n` closed pursuits (rejected, withdrawn, ghosted)."""

    async def go(s):
        total = open_n + closed_n
        await s.execute(insert(Opportunity), [
            {"track": "employment", "opp_type": "job_posting", "title": f"Role {i:04d}",
             "org_name": "Acme", "url": f"https://x.example/{i}", "stage": "t2_done",
             "description_md": "Own the work. " * 200}
            for i in range(total)
        ])
        await s.commit()
        ids = [r[0] for r in (await s.execute(Opportunity.__table__.select().with_only_columns(
            Opportunity.id).order_by(Opportunity.id))).all()]
        rows = []
        for i, oid in enumerate(ids):
            status = "shortlisted" if i < open_n else CLOSED_STATUSES[i % len(CLOSED_STATUSES)]
            rows.append({"opportunity_id": oid, "track": "employment", "status": status,
                         "last_activity_at": f"2026-11-{1 + i % 9:02d}T10:00:00+00:00"})
        await s.execute(insert(Pursuit), rows)
        await s.commit()

    _run(go)


def _after(html: str) -> str:
    """The cursor the Show more button carries, as it is in the URL."""
    return re.search(r'closed-more"[^>]*hx-get="[^"]*[?&]after=([^"&]*)', html).group(1)


def _cards(html: str) -> list[str]:
    return re.findall(r'data-testid="pcard" data-id="(\d+)"', html)


def test_a_long_closed_list_is_cut_at_100_a_column_and_open_cards_are_never_held_back():
    c = _fresh()
    _seed_many(open_n=130, closed_n=250)
    with c:
        html = c.get("/pursuits").text
    cards = _cards(html)
    open_cards = re.findall(r'data-testid="pcard"[^>]*data-folded="0"', html)
    assert len(open_cards) == 130  # every active card, past the cap
    folded = re.findall(r'data-testid="pcard"[^>]*data-folded="1"', html)
    assert len(folded) == page.FOLD_CAP == 100
    assert len(cards) == 230
    # The header says the truth, not the number drawn.
    assert "Closed · 250" in html
    assert re.search(r'data-testid="closed-more"[^>]*data-extra="150"', html)
    assert "Show 150 more closed" in html
    assert "130 open · 250 closed" in re.sub(r"\s+", " ", html)
    assert "Showing 380 of 380" in html


def test_a_short_closed_list_has_no_show_more():
    c = _fresh()
    _seed_many(open_n=3, closed_n=40)
    with c:
        html = c.get("/pursuits").text
    assert "closed-more" not in html
    assert len(_cards(html)) == 43


def test_show_more_returns_one_page_a_click_with_no_gap_and_no_repeat():
    """D-PERF-4: each press draws at most FOLD_CAP closed cards (the first load's page size) and
    brings the button for the next page, keyset and all, until nothing is left."""
    c = _fresh()
    _seed_many(open_n=5, closed_n=330)
    with c:
        page1 = c.get("/pursuits").text
        first = _cards(page1)
        seen, sizes, extras, html = list(first), [], [], page1
        while "closed-more" in html:
            extras.append(int(re.search(
                r'data-testid="closed-more"[^>]*data-extra="(\d+)"', html).group(1)))
            more = c.get("/pursuits/closed?col=closed&track=employment&after=" + _after(html))
            assert more.status_code == 200
            assert 'data-folded="0"' not in more.text  # only closed cards come back
            html = more.text
            got = _cards(html)
            sizes.append(len(got))
            seen += got
    assert sizes == [100, 100, 30]  # capped per press, never the whole rest at once
    assert extras == [230, 130, 30]  # each button names what is still behind it
    assert len(seen) == len(set(seen)) == 335  # every pursuit exactly once


def test_show_more_for_an_unknown_column_is_a_404():
    c = _fresh()
    with c:
        assert c.get("/pursuits/closed?col=nope&track=employment").status_code == 404


def test_table_is_paged_and_pages_cover_every_row_once():
    c = _fresh()
    _seed_many(open_n=120, closed_n=135)
    seen: list[str] = []
    with c:
        p1 = c.get("/pursuits?view=table")
        assert 'data-testid="table-pager"' in p1.text
        assert "Page 1 of 3" in p1.text and "255 in all" in p1.text
        for n in (1, 2, 3):
            html = c.get(f"/pursuits?view=table&page={n}").text
            rows = re.findall(r'data-testid="prow" data-id="(\d+)"', html)
            assert len(rows) == (100 if n < 3 else 55)
            first_ord = int(re.search(r'data-testid="prow"[^>]*data-ord="(\d+)"', html).group(1))
            assert first_ord == (n - 1) * 100 + 1  # the sort-back order continues across pages
            seen += rows
        beyond = c.get("/pursuits?view=table&page=99").text  # clamped, never an empty page
        assert "Page 3 of 3" in beyond
    assert len(seen) == len(set(seen)) == 255


def test_table_with_one_page_has_no_pager():
    c = _fresh()
    _seed_many(open_n=4, closed_n=3)
    with c:
        html = c.get("/pursuits?view=table").text
    assert "table-pager" not in html
    assert len(re.findall(r'data-testid="prow"', html)) == 7


def test_the_html_is_smaller_and_the_chips_still_count_everything():
    c = _fresh()
    _seed_many(open_n=20, closed_n=600)
    with c:
        html = c.get("/pursuits").text
    chip = re.search(r'data-stage="all" data-total="(\d+)">All<span class="k-chip__n">(\d+)<', html)
    assert chip and chip.group(1) == chip.group(2) == "620"
    col = re.search(r'data-testid="apps-col" data-col="closed".*?data-total="(\d+)"', html, re.S)
    assert col and col.group(1) == "600"
    assert len(_cards(html)) == 120  # 20 open and 100 closed, not 620


def test_board_actions_still_answer_with_the_same_board(monkeypatch):
    """An action's reply (the board plus the rail, out of band) carries the same cap."""
    c = _fresh()
    _seed_many(open_n=3, closed_n=150)
    with c:
        pid = _cards(c.get("/pursuits").text)[0]
        r = c.post(f"/pursuits/{pid}/priority", headers={"HX-Request": "true"})
        assert r.status_code == 200
        assert len(re.findall(r'data-folded="1"', r.text)) == page.FOLD_CAP
        assert "closed-more" in r.text


# ---------------------------------------------------------- search and filters run over everything
def _titles() -> dict[int, str]:
    async def go(s):
        from sqlalchemy import select

        rows = (await s.execute(
            select(Pursuit.id, Opportunity.title).join(
                Opportunity, Opportunity.id == Pursuit.opportunity_id))).all()
        return {pid: title for pid, title in rows}

    return _run(go)


def _hit(title: str, q: str) -> bool:
    """The search box's rule: every word of the query is somewhere in the title."""
    return all(w in title.lower() for w in q.split())


def _query_param(**kw) -> str:
    from urllib.parse import urlencode

    return urlencode(kw)


def test_search_finds_a_closed_card_that_is_not_among_the_first_100():
    c = _fresh()
    _seed_many(open_n=5, closed_n=230)
    titles = _titles()
    with c:
        drawn = set(_cards(c.get("/pursuits").text))
        hidden = next(pid for pid in titles if str(pid) not in drawn)
        html = c.get("/pursuits?" + _query_param(q=titles[hidden].lower())).text
    assert str(hidden) in _cards(html)
    assert "closed-more" not in html  # one match: nothing left behind a button
    assert "Closed · 1" in html and "1 open · " not in html
    assert "data-fkey=" in html


def test_a_filter_finds_a_closed_card_that_is_not_among_the_first_100():
    c = _fresh()
    _seed_many(open_n=5, closed_n=230)
    titles = _titles()
    with c:
        drawn = set(_cards(c.get("/pursuits").text))
        hidden = next(pid for pid in titles if str(pid) not in drawn)

        async def mark(s):
            p = await s.get(Pursuit, hidden)
            p.priority = 1
            await s.commit()

        _run(mark)
        html = c.get("/pursuits?pri=1").text
    assert _cards(html) == [str(hidden)]


def test_search_on_the_table_pages_the_matches_and_finds_a_row_from_page_two():
    c = _fresh()
    _seed_many(open_n=120, closed_n=135)
    titles = _titles()
    with c:
        p2 = c.get("/pursuits?view=table&page=2").text
        row = re.findall(r'data-testid="prow" data-id="(\d+)"', p2)[0]
        found = c.get("/pursuits?" + _query_param(view="table", q=titles[int(row)].lower())).text
        # Role 01: every title with that prefix; paged as matches, not as the whole table.
        wide = c.get("/pursuits?view=table&q=role+01").text
    assert re.findall(r'data-testid="prow" data-id="(\d+)"', found) == [row]
    assert "table-pager" not in found
    expected = [p for p, t in titles.items() if _hit(t, "role 01")]
    got = re.findall(r'data-testid="prow" data-id="(\d+)"', wide)
    assert len(got) == min(100, len(expected))
    assert len(expected) > 100  # the seed makes this a paged result
    assert f"of {len(expected)} matching" in wide


def test_show_more_closed_pages_the_matches_not_the_whole_list():
    c = _fresh()
    _seed_many(open_n=3, closed_n=330)
    titles = _titles()
    matching = {p for p, t in titles.items() if _hit(t, "role 02")}
    closed = {p for p in matching if int(titles[p].split()[1]) >= 3}
    with c:
        html = c.get("/pursuits?q=role+02").text
        shown = set(int(i) for i in _cards(html))
        rest = c.get("/pursuits/closed?col=closed&track=employment&q=role+02&after="
                     + _after(html)).text
    left = set(int(i) for i in _cards(rest))
    assert shown | left == matching
    assert not shown & left
    assert len(closed) > 100
    assert re.search(rf'data-testid="closed-more"[^>]*data-extra="{len(closed) - 100}"', html)
    assert f"Closed · {len(closed)}" in html


def test_chips_and_counts_say_how_many_match():
    c = _fresh()
    _seed_many(open_n=30, closed_n=200)
    titles = _titles()
    n = len([t for t in titles.values() if _hit(t, "role 01")])
    with c:
        html = c.get("/pursuits?q=role+01").text
    assert re.search(r'data-chip-totals="[^"]*&#34;all&#34;:\s*' + str(n), html)
    assert f'data-total="{n}"' in html
    assert f"Showing {n} of {n}" in html


# ------------------------------------- a card action keeps the page; Show more is a keyset
def test_a_card_action_on_table_page_two_redraws_page_two_with_the_same_search():
    c = _fresh()
    _seed_many(open_n=120, closed_n=20)
    with c:
        p2 = c.get("/pursuits?view=table&page=2").text
        row = re.findall(r'data-testid="prow" data-id="(\d+)"', p2)[0]
        r = c.post(f"/pursuits/{row}/priority", data={"page": "2", "view": "table"},
                   headers={"HX-Request": "true"})
        assert r.status_code == 200
        assert "Page 2 of 2" in r.text and row in r.text
        # Without the page it is page one, which is what the script no longer lets happen.
        r1 = c.post(f"/pursuits/{row}/priority", data={"view": "table"},
                    headers={"HX-Request": "true"})
        assert "Page 1 of 2" in r1.text


def test_show_more_closed_neither_skips_nor_repeats_when_a_card_moves_in_between():
    c = _fresh()
    _seed_many(open_n=3, closed_n=230)
    with c:
        first = c.get("/pursuits").text
        shown = _cards(first)
        after = _after(first)
        # Between the draw and the click a card shown moves back to open, and one that was
        # still behind the button is deleted from the closed group by the same move.
        every = set(str(p) for p in _titles())
        behind = sorted(every - set(shown), key=int)
        moved_shown, moved_behind = int(shown[0]), int(behind[0])

        async def move(s):
            for pid in (moved_shown, moved_behind):
                p = await s.get(Pursuit, pid)
                p.status = "shortlisted"
            await s.commit()

        _run(move)
        html = c.get("/pursuits/closed?col=closed&track=employment&after=" + after).text
        rest = _cards(html)
        while "closed-more" in html:  # one page a press (D-PERF-4): follow every page
            html = c.get("/pursuits/closed?col=closed&track=employment&after="
                         + _after(html)).text
            rest += _cards(html)
    assert len(rest) == len(set(rest))
    assert not set(rest) & set(shown)
    still_behind = {p for p in behind if int(p) != moved_behind and int(p) > 3}
    assert set(rest) >= still_behind - {str(moved_shown)}
    assert str(moved_behind) not in rest
