"""The settings the interface owns (SPEC.md 4.6): scene, rotation, animate, seasonal touches,
avatar, notification sound, quiet hours, region formats, text size. Defaults, validation on write,
and a stored value that no longer validates reading as the default instead of breaking the load."""

from __future__ import annotations

from datetime import date, datetime, time

import pytest
from sqlalchemy import select

from kestrel import ui_settings as u
from kestrel.config import UI, Notify, Region
from kestrel.db.models import Setting
from kestrel.services import setting_overrides as so
from kestrel.services import ui_state


# ------------------------------------------------------------------------------- the defaults
def test_defaults():
    ui, notify, region = UI(), Notify(), Region()
    assert ui.scene == "mountains" and ui.scene_daily is True and ui.scene_animate is True
    assert ui.scene_rotation == list(u.SCENES) and len(u.SCENES) == 14
    assert ui.touches_enabled is True and ui.touches_off == [] and ui.touch_groups_off == []
    assert ui.avatar == "initial" and ui.text_size == "default" and ui.reduce_motion is False
    assert notify.sound is True and notify.sound_name == "chime"
    assert 0.0 <= notify.sound_volume <= 1.0
    assert notify.quiet_hours == {"enabled": False, "start": "21:00", "end": "07:00"}
    assert region.date_format == "MM/DD/YYYY" and region.time_format == "12h"


def test_sixteen_touches_in_three_groups_and_no_festival_names():
    assert len(u.TOUCHES) == 16 and set(u.TOUCH_GROUPS) == {"seasons", "civic", "lights"}
    banned = ("christmas", "easter", "ramadan", "diwali", "holi", "hanukkah", "eid")
    assert not [t for t in u.TOUCHES if any(b in t for b in banned)]


# ------------------------------------------------------------------------ strict validation
def test_a_write_refuses_unknown_choices_with_a_plain_message():
    for fn, bad in [(u.scene, "volcano"), (u.avatar, "dragon"), (u.sound_name, "airhorn"),
                    (u.date_format, "D/M/Y"), (u.time_format, "13h"), (u.text_size, "huge"),
                    (u.flag, "yes"), (u.touches_off, ["spring", "nope"]),
                    (u.touch_groups_off, ["nope"]), (u.scene_rotation, ["mountains", "x"])]:
        with pytest.raises(ValueError):
            fn(bad)


def test_none_is_a_scene_but_not_a_rotation_member():
    assert u.scene("none") == "none"
    with pytest.raises(ValueError):
        u.scene_rotation(["none"])


def test_at_least_one_scene_stays_checked_and_order_is_the_librarys():
    with pytest.raises(ValueError, match="At least one"):
        u.scene_rotation([])
    assert u.scene_rotation(["lake", "coast", "lake"]) == ["coast", "lake"]


def test_volume_is_clamped_never_refused():
    assert u.sound_volume(2) == 1.0 and u.sound_volume(-3) == 0.0 and u.sound_volume(0.25) == 0.25
    for bad in ("loud", None, True, float("nan")):
        with pytest.raises(ValueError):
            u.sound_volume(bad)


def test_quiet_hours_validation():
    assert u.quiet_hours({"enabled": True, "start": "9:00", "end": "07:00"}) == {
        "enabled": True, "start": "09:00", "end": "07:00"
    }
    for bad in ({"start": "25:00"}, {"end": "7"}, {"enabled": "yes"}, "9 to 5", None):
        with pytest.raises(ValueError):
            u.quiet_hours(bad)


def test_quiet_hours_span_a_night_as_one_period():
    h = {"enabled": True, "start": "21:00", "end": "07:00"}
    assert u.in_quiet_hours(h, time(23, 0)) and u.in_quiet_hours(h, time(6, 59))
    assert not u.in_quiet_hours(h, time(7, 0)) and not u.in_quiet_hours(h, time(12, 0))
    assert not u.in_quiet_hours({**h, "enabled": False}, time(23, 0))
    day = {"enabled": True, "start": "09:00", "end": "17:00"}
    assert u.in_quiet_hours(day, time(12, 0)) and not u.in_quiet_hours(day, time(18, 0))
    assert not u.in_quiet_hours({"enabled": True, "start": "08:00", "end": "08:00"}, time(8, 0))


# ------------------------------------------------------------- reading stale rows (lenient)
def test_a_stored_value_that_no_longer_validates_reads_as_the_default():
    ui = UI(scene="removed-scene", scene_rotation=["gone"], scene_daily="maybe",
            touches_off=["gone"], avatar=42, text_size="xl")
    assert ui.scene == "mountains" and ui.scene_rotation == list(u.SCENES)
    assert ui.scene_daily is True and ui.touches_off == [] and ui.avatar == "initial"
    assert ui.text_size == "default"
    n = Notify(sound_name="gone", sound_volume=7, quiet_hours={"start": "nope"})
    assert n.sound_name == "chime" and n.sound_volume == 1.0
    assert n.quiet_hours == u.default_quiet_hours()
    assert Region(date_format="x", time_format="y").date_format == "MM/DD/YYYY"


async def test_setting_rows_flow_through_the_precedence_chain(sm):
    async with sm() as s:
        s.add_all([
            Setting(key="ui.scene", value="lake"),
            Setting(key="ui.scene_daily", value=False),
            Setting(key="ui.touches_off", value=["halloween"]),
            Setting(key="notify.sound_name", value="bell"),
            Setting(key="region.date_format", value="YYYY-MM-DD"),
            Setting(key="region.time_format", value="24h"),
            Setting(key="ui.scene_rotation", value=["not a scene"]),  # stale: ignored safely
        ])
        await s.commit()
        settings = await so.load_settings_with_db(s)
    assert settings.ui.scene == "lake" and settings.ui.scene_daily is False
    assert settings.ui.touches_off == ["halloween"]
    assert settings.notify.sound_name == "bell"
    assert (settings.region.date_format, settings.region.time_format) == ("YYYY-MM-DD", "24h")
    assert settings.ui.scene_rotation == list(u.SCENES)


def test_the_new_keys_count_as_configuration_rows():
    for key in ui_state.OWNED_KEYS:
        assert so.is_config_key(key), key
    assert so.is_config_key("region.date_format")
    assert not so.is_config_key("ui.board_range")  # existing page state stays state


# ----------------------------------------------------------------------------- writing rows
async def test_set_value_validates_and_upserts(sm):
    async with sm() as s:
        assert await ui_state.set_value(s, "ui.scene", "coast") == "coast"
        assert await ui_state.set_value(s, "ui.scene", "island") == "island"  # upsert, one row
        assert await ui_state.set_value(s, "notify.sound_volume", 5) == 1.0  # clamped
        assert await ui_state.set_value(s, "ui.scene_rotation", ["lake", "coast"]) == [
            "coast", "lake"
        ]
        await s.commit()
        rows = dict((await s.execute(select(Setting.key, Setting.value))).all())
    assert rows["ui.scene"] == "island" and rows["notify.sound_volume"] == 1.0
    async with sm() as s:
        with pytest.raises(ValueError):
            await ui_state.set_value(s, "ui.scene", "volcano")
        with pytest.raises(KeyError):
            await ui_state.set_value(s, "ui.not_mine", 1)


async def test_restore_defaults_removes_the_rows(sm):
    async with sm() as s:
        await ui_state.set_value(s, "ui.scene", "coast")
        await ui_state.set_value(s, "region.time_format", "24h")
        await s.commit()
        assert await ui_state.restore_defaults(s, ["ui.scene"]) == 1
        await s.commit()
        settings = await so.load_settings_with_db(s)
        assert settings.ui.scene == "mountains" and settings.region.time_format == "24h"
        assert await ui_state.restore_defaults(s) == 1
        with pytest.raises(KeyError):
            await ui_state.restore_defaults(s, ["ui.not_mine"])


# --------------------------------------------------------------------- derived behavior
def test_change_daily_is_stable_all_day_and_cycles_the_checked_set():
    rotation = ["coast", "lake", "forest"]
    d = date(2026, 10, 24)
    today = u.scene_for_day("mountains", True, rotation, d)
    assert today in rotation and today == u.scene_for_day("mountains", True, rotation, d)
    seen = [u.scene_for_day("mountains", True, rotation, date.fromordinal(d.toordinal() + i))
            for i in range(3)]
    assert sorted(seen) == sorted(rotation)  # three days, each checked scene once
    assert u.scene_for_day("mountains", False, rotation, d) == "mountains"
    assert u.scene_for_day("none", False, rotation, d) == "none"


def test_date_and_time_formats():
    d = date(2026, 10, 3)
    assert [u.format_date(d, f) for f in u.DATE_FORMATS] == [
        "10/03/2026", "10/03/26", "03/10/2026", "03/10/26", "2026-10-03", "Oct 3, 2026"
    ]
    assert u.format_date(datetime(2026, 1, 5, 9, 0), "bogus") == "01/05/2026"
    assert u.format_time(time(21, 5), "12h") == "9:05 PM" and u.format_time(time(21, 5), "24h") == (
        "21:05"
    )
    assert u.format_time(time(0, 7)) == "12:07 AM" and u.format_time(time(12, 0)) == "12:00 PM"


def test_onboarding_state_keeps_reading_through_its_own_tolerant_loader():
    """onboarding.state is state, not configuration, so a bad row never reaches Settings."""
    from kestrel.services.onboarding import KEY, OnboardingState

    assert KEY == "onboarding.state" and not so.is_config_key(KEY)
    st = OnboardingState.from_value({"step": "nonsense", "visited": ["welcome", "zzz"],
                                     "completed": "yes"})
    assert st.step == "welcome" and st.visited == ["welcome"] and st.completed is True
