"""Work search log export (D-EXPORT-1): every application in a date range, whatever became of it.

Invented roles only; fixed dates, and the page's own clock alias is patched. No network."""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import Opportunity, Org, Pursuit, Setting, Source, StatusEvent
from kestrel.services.work_search_log import collect, pick_range
from kestrel.web import auth
from kestrel.web.app import build_app
from tests import retry_seed

PATH = "/analytics/export/work-search-log.csv"
MON = date(2026, 5, 4)  # a Monday


class Seed:
    def __init__(self, s) -> None:
        self.s, self.n = s, 0
        self.org = Org(name="Larkspur Logistics", normalized_name="larkspur")
        self.src = Source(track="employment", kind="greenhouse", name="Career pages", enabled=True)
        s.add_all([self.org, self.src])

    async def add(self, status, applied, title="Operations Analyst", events=(), track="employment"):
        await self.s.flush()
        self.n += 1
        opp = Opportunity(track=track, opp_type="job_posting", org_id=self.org.id,
                          source_id=self.src.id, title=title, org_name=self.org.name,
                          url=f"https://example.test/{self.n}", stage="t2_done")
        self.s.add(opp)
        await self.s.flush()
        p = Pursuit(opportunity_id=opp.id, track=track, status=status, applied_at=applied)
        self.s.add(p)
        await self.s.flush()
        for frm, to in events:
            self.s.add(StatusEvent(pursuit_id=p.id, from_status=frm, to_status=to))
        await self.s.flush()
        return p


def _seed(fn) -> None:
    async def go():
        await reset_engine()
        retry_seed._wipe_db()
        migrate.run_migrations()
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            await fn(Seed(s))
            await s.commit()
        await engine.dispose()
        forget_engine()

    asyncio.run(go())


async def _standard(sd: Seed) -> None:
    d = lambda n: f"2026-05-{n:02d}T09:30:00+00:00"  # noqa: E731
    await sd.add("applied", d(4), "Analyst Monday")
    await sd.add("rejected", d(6), "Analyst Rejected")
    await sd.add("withdrawn", d(7), "Analyst Withdrawn")
    await sd.add("ghosted", d(10), "Analyst Ghosted Sunday")
    await sd.add("accepted", d(8), "Analyst Accepted")
    await sd.add("interviewing", d(3), "Before The Range")
    await sd.add("offer", d(11), "After The Range")
    await sd.add("shortlisted", None, "Only Shortlisted")
    await sd.add("preparing", None, "Only Preparing")
    await sd.add("applied", None, "Dateless Open")
    await sd.add("withdrawn", None, "Closed From Shortlist", events=[("shortlisted", "withdrawn")])
    await sd.add("rejected", None, "Rejected Dateless",
                 events=[("applied", "screening"), ("screening", "rejected")])
    await sd.add("applied", d(5), "Consulting Twin", track="consulting")


@pytest.fixture
def web(_isolate_data_home, monkeypatch):
    _seed(_standard)
    monkeypatch.setattr("kestrel.web.pages.work_search_log._today", lambda: date(2026, 5, 6))
    with TestClient(build_app(worker=False)) as c:
        yield c


def _rows(r):
    return list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))


def test_the_range_includes_and_excludes_by_applied_date(web):
    r = web.get(PATH, params={"from": "2026-05-04", "to": "2026-05-10"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert "2026-05-04-to-2026-05-10" in r.headers["content-disposition"]
    rows = _rows(r)
    assert rows[0] == ["Role", "Company", "Applied"]
    titles = [x[0] for x in rows[1:]]
    assert titles[:5] == ["Analyst Monday", "Analyst Rejected", "Analyst Withdrawn",
                          "Analyst Accepted", "Analyst Ghosted Sunday"]  # oldest first
    assert "Before The Range" not in titles and "After The Range" not in titles
    assert rows[1] == ["Analyst Monday", "Larkspur Logistics", "2026-05-04"]


def test_closed_and_rejected_roles_are_kept_and_shortlisted_ones_are_not(web):
    titles = [x[0] for x in _rows(web.get(PATH, params={"from": "2026-05-01", "to": "2026-05-31"}))]
    for kept in ("Analyst Rejected", "Analyst Withdrawn", "Analyst Ghosted Sunday",
                 "Analyst Accepted"):
        assert kept in titles
    for left in ("Only Shortlisted", "Only Preparing", "Closed From Shortlist", "Consulting Twin"):
        assert left not in titles


def test_a_missing_date_goes_in_a_date_unknown_block_not_dropped(web):
    rows = _rows(web.get(PATH, params={"from": "2026-05-04", "to": "2026-05-10"}))
    at = [x[0] for x in rows].index("Date unknown")
    assert [x[0] for x in rows[at + 1:]] == ["Dateless Open", "Rejected Dateless"]
    assert all(x[2] == "" for x in rows[at + 1:])


def test_a_backdated_application_uses_the_stored_date(web):
    async def more(sd):
        await _standard(sd)
        await sd.add("applied", "2026-04-27T00:00:00+00:00", "Logged Late")

    _seed(more)
    last = _rows(web.get(PATH, params={"from": "2026-04-27", "to": "2026-04-30"}))
    assert last[1] == ["Logged Late", "Larkspur Logistics", "2026-04-27"]


def test_the_count_in_the_sheet_equals_the_rows_in_the_file(web):
    q = {"from": "2026-05-04", "to": "2026-05-10"}
    page = web.get("/work-search-log/panel", params=q).text
    n = len(_rows(web.get(PATH, params=q))) - 2 - 2  # header, "Date unknown" line, two unknown
    assert f"<b>{n} applications</b>, May 4 to May 10" in page
    assert "2 more with no applied date" in page
    assert f"{PATH}?from=2026-05-04&amp;to=2026-05-10" in page


def test_the_sheet_defaults_to_this_week_and_quick_picks_move_the_range(web):
    page = web.get("/work-search-log/panel").text
    assert 'value="2026-05-04"' in page and 'value="2026-05-10"' in page
    assert 'aria-pressed="true" data-testid="wsl-pick-this_week"' in page
    typed = web.get("/work-search-log/panel",
                    params={"from": "2026-05-01", "to": "2026-05-02"}).text
    assert 'aria-pressed="true"' not in typed
    last = web.get("/work-search-log/panel", params={"pick": "last_week"}).text
    assert 'value="2026-04-27"' in last and 'value="2026-05-03"' in last
    month = web.get("/work-search-log/panel", params={"pick": "this_month"}).text
    assert 'value="2026-05-01"' in month and 'value="2026-05-06"' in month


def test_a_backwards_range_is_refused_in_words(web):
    r = web.get(PATH, params={"from": "2026-05-10", "to": "2026-05-04"})
    assert r.status_code == 400 and "on or before" in r.text
    page = web.get("/work-search-log/panel", params={"from": "2026-05-10", "to": "2026-05-04"}).text
    assert 'data-testid="wsl-error"' in page and PATH not in page


def test_a_backwards_range_keeps_the_dates_that_were_typed(web):
    """Fable C8: the message sits beside the dates the person typed, not this week's."""
    page = web.get("/work-search-log/panel", params={"from": "2026-05-10", "to": "2026-05-04"}).text
    assert 'name="from" value="2026-05-10"' in page and 'name="to" value="2026-05-04"' in page
    assert "2026-05-06" not in page  # the test clock's today
    half = web.get("/work-search-log/panel", params={"from": "2026-05-10", "to": "nope"}).text
    assert 'name="from" value="2026-05-10"' in half and 'name="to" value=""' in half


def test_a_key_holder_is_refused_the_download(monkeypatch):
    migrate.run_migrations()
    auth.set_cached_key("k136-key")
    try:
        with TestClient(build_app(worker=False), base_url="http://127.0.0.1",
                        raise_server_exceptions=False, client=("192.168.1.50", 1)) as c:
            r = c.get(PATH, headers={"X-Kestrel-Key": "k136-key"})
        assert r.status_code == 403 and r.text == "Only this computer can download this."
    finally:
        auth.reset_cache()


def test_a_key_holder_sees_the_save_button_drawn_disabled_with_the_note(web, monkeypatch):
    """D-SEC-9: the page draws the control by the same test the route uses (Fable C5)."""
    params = {"from": "2026-05-04", "to": "2026-05-10"}
    local = web.get("/work-search-log/panel", params=params).text
    assert PATH in local and "local-only-download" not in local
    from kestrel.web.deps import templates

    monkeypatch.setitem(templates().env.globals, "is_local", lambda request: False)
    held = web.get("/work-search-log/panel", params=params).text
    assert PATH not in held
    assert 'data-testid="local-only-download"' in held and "disabled" in held
    assert "Only this computer can download this." in held


def test_formula_cells_are_neutralized(web):
    async def evil(sd):
        await sd.add("applied", "2026-05-05T00:00:00+00:00", "=HYPERLINK(\"x\")")

    _seed(evil)
    rows = _rows(web.get(PATH, params={"from": "2026-05-04", "to": "2026-05-10"}))
    assert rows[1][0] == "'=HYPERLINK(\"x\")"


def test_confidential_mode_redacts_and_never_holds_the_export(web, monkeypatch):
    async def conf(s):
        from kestrel.privacy import Employer, EmployerMap

        return True, EmployerMap(
            [Employer(name="Larkspur Logistics", shown_as="A logistics company")])

    monkeypatch.setattr("kestrel.web.pages.analytics.confidential_once", conf)
    monkeypatch.setattr("kestrel.web.pages.work_search_log.confidential_once", conf)
    text = web.get(PATH, params={"from": "2026-05-04", "to": "2026-05-10"})
    assert text.status_code == 200 and "Larkspur Logistics" not in text.text
    assert "A logistics company" in text.text


def test_confidential_with_no_employer_warns_but_still_exports(web):
    async def go():
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            await s.merge(Setting(key="confidential_mode", value=True))
            await s.commit()
        await engine.dispose()

    asyncio.run(go())
    r = web.get(PATH, params={"from": "2026-05-04", "to": "2026-05-10"})
    assert r.status_code == 200 and "Analyst Monday" in r.text
    assert 'data-testid="wsl-warn"' in web.get("/work-search-log/panel").text


def test_the_button_is_on_applications_for_employment_only(web):
    page = web.get("/pursuits?track=employment").text
    assert 'data-testid="work-search-log"' in page and "Export work search log" in page
    assert 'id="ap-wsl"' in page
    assert 'data-testid="work-search-log"' not in web.get("/pursuits?track=consulting").text


def test_collect_reads_the_stored_role_whatever_its_stage(_isolate_data_home):
    async def seed(sd):
        p = await sd.add("rejected", "2026-05-05T00:00:00+00:00", "Posting Gone")
        (await sd.s.get(Opportunity, p.opportunity_id)).stage = "closed"

    _seed(seed)

    async def read():
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            return await collect(s, date(2026, 5, 4), date(2026, 5, 10))

    log = asyncio.run(read())
    assert [r.role for r in log.rows] == ["Posting Gone"]


async def _with_withheld(sd: Seed) -> None:
    d = lambda n: f"2026-05-{n:02d}T09:30:00+00:00"  # noqa: E731
    await sd.add("applied", d(5), "Shareable Role")
    sd.src = Source(track="employment", kind="remotive", name="Remote board", enabled=True)
    sd.s.add(sd.src)
    await sd.add("applied", d(6), "Withheld Role")
    await sd.add("applied", d(7), "Withheld Role Two")
    await sd.add("applied", d(20), "Withheld Outside The Range")


def test_applications_left_out_for_a_source_that_forbids_export_are_counted_in_words(
    _isolate_data_home, monkeypatch
):
    _seed(_with_withheld)
    monkeypatch.setattr("kestrel.web.pages.work_search_log._today", lambda: date(2026, 5, 6))
    with TestClient(build_app(worker=False)) as c:
        got = c.get("/analytics/export/work-search-log.csv",
                    params={"from": "2026-05-04", "to": "2026-05-10"})
        assert [r[0] for r in _rows(got)[1:]] == ["Shareable Role"]  # the exclusion is kept
        sheet = c.get("/work-search-log/panel", params={"from": "2026-05-04", "to": "2026-05-10"})
        assert 'data-testid="wsl-withheld"' in sheet.text
        assert "2 applications are left out of this file" in sheet.text
        assert "do not allow passing it on" in sheet.text
        other = c.get("/work-search-log/panel", params={"from": "2026-05-01", "to": "2026-05-03"})
        assert 'data-testid="wsl-withheld"' not in other.text  # nothing in that range was held


def test_quick_picks_are_monday_to_sunday():
    assert pick_range("this_week", date(2026, 5, 6)) == (date(2026, 5, 4), date(2026, 5, 10))
    assert pick_range("last_week", date(2026, 5, 6)) == (date(2026, 4, 27), date(2026, 5, 3))
    assert pick_range("junk", date(2026, 5, 10)) == (date(2026, 5, 4), date(2026, 5, 10))
