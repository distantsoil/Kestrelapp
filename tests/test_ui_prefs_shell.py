"""Settings > Preferences reach every page through the shared shell (base.html, ui_prefs in
deps.py, the scene band macro). Each preference is proved on Today and on Pipeline, and a fresh
install (defaults) draws nothing new. Offline: nothing calls a model or a network."""

from __future__ import annotations

import asyncio
import re
from datetime import date

import pytest

from kestrel import ui_settings as u
from kestrel.db.models import Setting
from kestrel.web import deps
from tests.test_ui_shell import _client, _no_ollama, _seed  # noqa: F401  (autouse fixture)

PAGES = ["/", "/opportunities"]
_BAND = re.compile(r'<div class="k-scene" data-scene="([^"]*)" data-touch="([^"]*)" '
                   r'data-animate="([^"]*)"')


def _page(rows: list[Setting], path: str) -> str:
    # Fresh row objects every time: a row instance cannot be added to two databases.
    asyncio.run(_seed([Setting(key=r.key, value=r.value) for r in rows]))
    with _client() as c:
        return c.get(path).text


def _avatar_tag(html: str) -> str:
    return re.search(r'<a class="k-avatar[^>]*>.*?</a>', html, re.S).group(0)


@pytest.fixture(autouse=True)
def _day(monkeypatch):
    """A day inside no touch window unless a test moves it (the real date must not matter)."""
    monkeypatch.setattr(deps, "_today", lambda: date(2026, 5, 12))


def test_defaults_change_nothing(monkeypatch):
    from types import SimpleNamespace

    from kestrel.pipeline import profile as profile_mod

    monkeypatch.setattr(profile_mod, "load_profile", lambda track: SimpleNamespace(first_name=""))
    for path in PAGES:
        html = _page([], path)
        assert 'data-text-size="default"' in html
        assert 'data-reduce-motion="0"' in html
        band = _BAND.search(html)
        assert band and band.group(2) == "" and band.group(3) == "1"  # no touch, art moves
        tag = _avatar_tag(html)
        assert "k-avatar--initial" in tag and "<circle" in tag  # the plain silhouette, no letter
        assert "k-avatar__letter" not in tag


def test_text_size_reaches_other_pages():
    for path in PAGES:
        large = [Setting(key="ui.text_size", value="large")]
        small = [Setting(key="ui.text_size", value="small")]
        assert 'data-text-size="large"' in _page(large, path)
        assert 'data-text-size="small"' in _page(small, path)


def test_a_stale_text_size_reads_as_default():
    html = _page([Setting(key="ui.text_size", value="huge")], "/")
    assert 'data-text-size="default"' in html


def test_reduce_motion_reaches_other_pages_and_stills_the_band():
    for path in PAGES:
        html = _page([Setting(key="ui.reduce_motion", value=True)], path)
        assert 'data-reduce-motion="1"' in html
        assert _BAND.search(html).group(3) == "0"


def test_scene_animate_off_stills_the_band():
    for path in PAGES:
        off = [Setting(key="ui.scene_animate", value=False)]
        assert _BAND.search(_page(off, path)).group(3) == "0"


def test_scene_reaches_other_pages():
    rows = [Setting(key="ui.scene", value="coast"), Setting(key="ui.scene_daily", value=False)]
    for path in PAGES:
        assert _BAND.search(_page(rows, path)).group(1) == "coast"
    none = [Setting(key="ui.scene", value="none"), Setting(key="ui.scene_daily", value=False)]
    assert _BAND.search(_page(none, "/")).group(1) == "none"


def test_scene_rotation_picks_the_scene_of_the_day():
    rows = [Setting(key="ui.scene_daily", value=True),
            Setting(key="ui.scene_rotation", value=["coast", "lake"])]
    want = u.scene_for_day("mountains", True, ["coast", "lake"], date(2026, 5, 12))
    for path in PAGES:
        assert _BAND.search(_page(rows, path)).group(1) == want


def test_avatar_icon_reaches_the_top_bar():
    for path in PAGES:
        tag = _avatar_tag(_page([Setting(key="ui.avatar", value="bird")], path))
        assert "k-avatar--bird" in tag and "<circle" not in tag and "<path" in tag


def test_avatar_photo_uses_the_saved_photo(monkeypatch):
    from kestrel.services import profile_photo

    monkeypatch.setattr(profile_photo, "version", lambda: "123")
    tag = _avatar_tag(_page([Setting(key="ui.avatar", value="photo")], "/opportunities"))
    assert '<img src="/profile/photo?v=123"' in tag


def test_avatar_photo_without_a_file_falls_back(monkeypatch):
    from kestrel.services import profile_photo

    monkeypatch.setattr(profile_photo, "version", lambda: None)
    tag = _avatar_tag(_page([Setting(key="ui.avatar", value="photo")], "/"))
    assert "<img" not in tag and "k-avatar--initial" in tag


def test_avatar_initial_uses_the_first_name(monkeypatch):
    from types import SimpleNamespace

    from kestrel.pipeline import profile as profile_mod

    monkeypatch.setattr(
        profile_mod, "load_profile", lambda track: SimpleNamespace(first_name="ada")
    )
    for path in PAGES:
        tag = _avatar_tag(_page([Setting(key="ui.avatar", value="initial")], path))
        assert '<span class="k-avatar__letter" aria-hidden="true">A</span>' in tag


@pytest.mark.parametrize(("day", "touch"), [
    (date(2026, 10, 31), "halloween"), (date(2026, 12, 31), "newyear"),
    (date(2026, 7, 4), "july4"), (date(2026, 9, 7), "laborday"),
    (date(2026, 4, 22), "earthday"),
])
def test_touch_reaches_other_pages(monkeypatch, day, touch):
    monkeypatch.setattr(deps, "_today", lambda: day)
    for path in PAGES:
        assert _BAND.search(_page([], path)).group(2) == touch


def test_no_touch_outside_its_dates():
    assert _BAND.search(_page([], "/")).group(2) == ""


def test_touch_master_switch_off_draws_none(monkeypatch):
    monkeypatch.setattr(deps, "_today", lambda: date(2026, 10, 31))
    for path in PAGES:
        off = [Setting(key="ui.touches_enabled", value=False)]
        assert _BAND.search(_page(off, path)).group(2) == ""


def test_touch_group_and_single_switches(monkeypatch):
    monkeypatch.setattr(deps, "_today", lambda: date(2026, 10, 31))
    group = [Setting(key="ui.touch_groups_off", value=["civic"])]
    single = [Setting(key="ui.touches_off", value=["halloween"])]
    for path in PAGES:
        for rows in (group, single):
            assert _BAND.search(_page(rows, path)).group(2) == ""


def test_touch_dates_unit():
    assert u.touch_for_day(date(2026, 2, 17), True, [], []) == "lunar_new_year"
    assert u.touch_for_day(date(2040, 2, 17), True, [], []) is None  # not in the bundled table
    assert u.touch_for_day(date(2026, 11, 26), True, [], []) == "harvest"
    assert u.touch_for_day(date(2026, 12, 12), True, [], ["winter_lights"]) is None
