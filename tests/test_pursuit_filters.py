"""Live search and filters on the Applications page: the controls, the per-card search text, the
count, the empty state, the URL pre-fill, and confidential mode never leaking the employer into
what search matches. Rendering only; the filtering itself runs in the browser."""

from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import DocMaster, DocSection, Opportunity, Org, Pursuit, Setting
from kestrel.services.tracks import forget_cache
from kestrel.web import app as app_module

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_EMPLOYER = "Northwind Freight"
_SHOWN_AS = "My current employer"


async def _seed(*, confidential: bool = False) -> None:
    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_cache()
    engine = make_async_engine(paths.db_path())
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        org = Org(name=_EMPLOYER, normalized_name=_EMPLOYER.lower())
        other = Org(name="Globex Corp", normalized_name="globex corp")
        s.add_all([org, other])
        await s.flush()
        rows = [
            (org, "Head of IT", "shortlisted", "Austin, TX",
             f"Referred by a friend at {_EMPLOYER}"),
            (other, "Platform Lead", "applied", "Remote (US)",
             "Call back after the holiday"),
            (other, "Ops Manager", "rejected", "Denver, CO", None),
        ]
        for i, (o, title, status, loc, notes) in enumerate(rows):
            opp = Opportunity(
                track="employment", opp_type="job_posting", title=title, org_id=o.id,
                org_name=o.name, location_raw=loc, url=f"https://x.example/{i}", stage="t2_done",
            )
            s.add(opp)
            await s.flush()
            s.add(Pursuit(opportunity_id=opp.id, track="employment", status=status,
                          notes_md=notes))
        if confidential:
            master = DocMaster(track="employment", name="Resume", doc_kind="resume")
            s.add(master)
            await s.flush()
            s.add(DocSection(doc_master_id=master.id, section_kind="experience", sort_order=0,
                             org_label=_EMPLOYER, org_label_redacted=_SHOWN_AS))
            s.add(Setting(key="confidential_mode", value=True))
        await s.commit()
    await engine.dispose()
    forget_engine()


def _client(**kw) -> TestClient:
    asyncio.run(_seed(**kw))
    return TestClient(app_module.build_app(worker=False))


def _searches(html: str) -> list[str]:
    return re.findall(r'data-search="([^"]*)"', html)


@pytest.mark.parametrize("view", ["kanban", "table"])
def test_both_views_render_the_search_filters_count_and_empty_state(view):
    with _client() as c:
        html = c.get(f"/pursuits?view={view}").text
    for marker in ("pursuit-search", "show-closed", "stage-chip", "filter-count",
                   "filter-empty", "clear-filters"):
        assert f'data-testid="{marker}"' in html, marker
    assert "Showing 3 of 3" in html
    assert "Nothing matches" in html
    assert len(_searches(html)) == 3
    assert ("pursuit-sort" in html) is (view == "table")


def test_search_text_covers_company_title_location_and_notes_without_punctuation():
    with _client() as c:
        html = c.get("/pursuits").text
    blob = " | ".join(_searches(html))
    assert "globex corp platform lead remote us call back after the holiday" in blob
    assert "austin tx" in blob


def test_q_and_filters_prefill_from_the_url():
    with _client() as c:
        html = c.get("/pursuits?view=table&q=remote+us&stage=applied&closed=0&sort=company").text
    assert 'value="remote us"' in html
    assert '"showClosed": false' in html
    assert '"stages": ["applied"]' in html
    assert '<option value="company" selected>' in html


def test_confidential_mode_keeps_the_employer_out_of_what_search_matches():
    with _client(confidential=True) as c:
        for view in ("kanban", "table"):
            html = c.get(f"/pursuits?view={view}").text
            assert _EMPLOYER.lower() not in " ".join(_searches(html))
            assert _EMPLOYER not in html
            assert _SHOWN_AS.lower() in " ".join(_searches(html))


def test_without_confidential_mode_the_real_employer_is_searchable():
    with _client() as c:
        html = c.get("/pursuits").text
    assert "northwind freight" in " ".join(_searches(html))


# ------------------------------------------------------------ limits for a long list (#30)
def _stored(key: str):
    from sqlalchemy import select

    async def go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                stmt = select(Setting).where(Setting.key == key)
                row = (await s.execute(stmt)).scalar_one_or_none()
                return None if row is None else row.value
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _store(key: str, value) -> None:
    async def go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                s.add(Setting(key=key, value=value))
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(go())


@pytest.mark.parametrize("view", ["kanban", "table"])
def test_every_item_carries_its_applied_and_updated_dates(view):
    with _client() as c:
        html = c.get(f"/pursuits?view={view}").text
    updated = re.findall(r'data-updated="([^"]*)"', html)
    assert len(updated) == 3
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}|", u) for u in updated)
    assert len(re.findall(r'data-applied="([^"]*)"', html)) == 3


def test_the_range_control_offers_seven_fourteen_thirty_ninety_and_all():
    with _client() as c:
        html = c.get("/pursuits").text
    select = html.split('data-testid="range-select"')[1].split("</select>")[0]
    assert re.findall(r'<option value="(\d+)"', select) == ["7", "14", "30", "90", "0"]
    assert '"range": "0"' in html  # nothing is hidden until the user chooses


def test_range_prefills_from_the_url_then_the_saved_default():
    with _client() as c:
        assert '"range": "30"' in c.get("/pursuits?range=30").text
        assert c.post("/pursuits/prefs", data={"range": "14", "cap": "25"}).status_code == 200
        assert _stored("ui.board_range") == "14" and _stored("ui.board_cap") == "25"
        html = c.get("/pursuits").text
        assert '"range": "14"' in html and '"cap": "25"' in html
        assert '"range": "90"' in c.get("/pursuits?range=90").text  # the URL wins


def test_prefs_ignore_values_that_are_not_offered():
    with _client() as c:
        r = c.post("/pursuits/prefs", data={"range": "365", "cap": "banana"})
        assert r.status_code == 200
    assert _stored("ui.board_range") is None and _stored("ui.board_cap") is None


def test_the_closed_column_folds_and_each_column_can_show_all():
    with _client() as c:
        html = c.get("/pursuits").text
    assert 'data-testid="closed-toggle"' in html
    assert html.count('data-testid="show-all"') == 4  # one per column
    assert 'data-testid="cap-select"' in html
    assert '"closedOpen": false' in html


def test_the_closed_column_state_prefills_from_the_url():
    with _client() as c:
        assert '"closedOpen": true' in c.get("/pursuits?open=closed").text


def test_confidential_mode_does_not_put_the_employer_in_the_new_attributes():
    with _client(confidential=True) as c:
        html = c.get("/pursuits").text
    for attr in ("data-updated", "data-applied"):
        assert _EMPLOYER not in " ".join(re.findall(rf'{attr}="([^"]*)"', html))
    assert _EMPLOYER not in html
