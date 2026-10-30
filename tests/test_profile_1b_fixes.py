"""Owner-reported Profile fixes O18 and O19 (the private overhaul notes):
the avatar updates in place, and the current employer is found for a role that has not ended.
O17 (the paginated preview) is a page behavior, covered in tests/test_profile_browser.py.
Offline, on the per-test data home from conftest."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import update

from kestrel.docs.current_employer import is_open_ended
from tests import profile_seed
from tests.test_profile_review_fixes import _png


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


@pytest.fixture
def client():
    from kestrel.web.app import build_app

    master_id = profile_seed.seed()
    with TestClient(build_app(worker=False)) as c:
        c.master_id = master_id
        yield c


# ------------------------------------------------------------------------------------ O18
def test_the_top_bar_avatar_is_a_named_target(client):
    soup = _soup(client.get("/profile?tab=about").text)
    assert soup.select_one("a.k-avatar#k-topavatar") is not None


def test_photo_upload_and_remove_also_swap_the_top_bar_avatar_in_place(client):
    r = client.post("/profile/photo", files={"photo": ("me.png", _png(), "image/png")})
    oob = _soup(r.text).select_one("#k-topavatar[hx-swap-oob]")
    assert oob is not None, "the reply must carry an out-of-band top bar avatar"
    assert oob.select_one("img[src^='/profile/photo?v=']") is not None
    assert oob["data-avatar"] == "photo"
    r = client.post("/profile/photo/delete")
    oob = _soup(r.text).select_one("#k-topavatar[hx-swap-oob]")
    assert oob is not None and oob["data-avatar"] == "initial"
    assert oob.select_one("img") is None and oob.get_text(strip=True) == "J"


def test_the_avatar_fragment_route_renders_the_current_picture(client):
    client.post("/profile/photo", files={"photo": ("me.png", _png(), "image/png")})
    r = client.get("/ui/avatar")
    assert r.status_code == 200
    el = _soup(r.text).select_one("#k-topavatar")
    assert el is not None and el.select_one("img[src^='/profile/photo?v=']") is not None
    assert "hx-swap-oob" not in r.text


def test_choosing_an_icon_in_settings_is_reflected_by_the_fragment(client):
    r = client.post("/settings/ui", json={"key": "ui.avatar", "value": "star"})
    assert r.status_code == 200
    el = _soup(client.get("/ui/avatar").text).select_one("#k-topavatar")
    assert el["data-avatar"] == "star" and el.select_one("svg") is not None


# ------------------------------------------------------------------------------------ O19
def test_a_role_ending_in_the_future_is_current():
    today = dt.date(2026, 10, 26)
    assert is_open_ended("Present") and is_open_ended("") and is_open_ended(None)
    assert is_open_ended("Dec 2026", today=today)
    assert is_open_ended("December 2027", today=today)
    assert is_open_ended("2027", today=today)
    assert is_open_ended("12/2026", today=today)
    assert is_open_ended("2026-12", today=today)
    assert is_open_ended("Oct 2026", today=today)  # this month is not over yet
    assert not is_open_ended("Sep 2026", today=today)
    assert not is_open_ended("2019", today=today)
    assert not is_open_ended("Mar 2021", today=today)
    assert not is_open_ended("sometime", today=today)  # unreadable stays not current


def _set_end_date(end: str) -> None:
    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import DocSection

    async def go():
        async with get_sessionmaker()() as s:
            await s.execute(update(DocSection).values(end_date=end))
            await s.commit()

    asyncio.run(go())


def test_confidential_hide_is_filled_from_a_role_that_ends_in_the_future(client):
    year = dt.date.today().year + 1
    _set_end_date(f"Dec {year}")
    html = client.get("/settings/section/privacy").text
    field = _soup(html).select_one('[data-testid="st-conf-employer"]')
    assert field["value"] == "A regional food distributor"


def test_an_empty_hide_field_says_where_the_value_comes_from(client):
    _set_end_date("Mar 2019")
    soup = _soup(client.get("/settings/section/privacy").text)
    assert soup.select_one('[data-testid="st-conf-employer"]')["value"] == ""
    hint = soup.select_one('[data-testid="conf-employer-empty"]')
    assert hint is not None and "Documents" in hint.get_text()
    assert hint.select_one("a[href^='/documents']") is not None


def test_an_empty_headline_says_the_import_does_not_fill_it(client):
    soup = _soup(client.get("/profile?tab=about").text)
    assert soup.select_one('[data-testid="headline-hint"]') is None  # the seed has one typed
    client.post("/profile/employment/about", data={"first_name": "Jordan", "headline": ""})
    soup = _soup(client.get("/profile?tab=about").text)
    hint = soup.select_one('[data-testid="headline-hint"]')
    assert hint is not None and "resume import" in hint.get_text()
