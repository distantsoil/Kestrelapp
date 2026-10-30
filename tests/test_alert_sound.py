"""Quiet hours and Kestrel's own alert sound (SPEC-INTERFACES 6, D-UI-95, D-REL-7), offline.

* delivery: inside Quiet hours no pop-up goes anywhere (toast, ntfy or webhook), the rows stay
  pending, and the first cycle after the window ends sends them as the one usual summary;
* the sound: drawn in pure Python (the recipe of the old Web Audio preview), played by the app
  process once with each message a cycle delivers, never otherwise, with the platform's own
  player; every pop-up is silent so Kestrel's is the only sound; Settings > Play goes through the
  same path.

No network, no live model, no real sound (tests/conftest.py replaces `play`; the real functions
are taken at import, below). The autouse isolation gives each test its own data home."""

from __future__ import annotations

import asyncio
import io
import struct
import sys
import threading
import time as time_mod
import types
import wave
from datetime import time
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from structlog.testing import capture_logs

from kestrel import paths, ui_settings
from kestrel.config import get_settings
from kestrel.db import migrate
from kestrel.db.base import utcnow_iso
from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
from kestrel.db.models import Notification, Setting
from kestrel.services import alert_sound, delivery
from kestrel.services.delivery import deliver_pending

REAL_PLAY = alert_sound.play  # before tests/conftest.py swaps in the silent stand-in

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "kestrel" / "web" / "static"
TEMPLATES = ROOT / "kestrel" / "web" / "templates"

NIGHT = {"enabled": True, "start": "21:00", "end": "07:00"}  # one night, across midnight
DAY = {"enabled": True, "start": "09:00", "end": "17:00"}
OFF = {"enabled": False, "start": "21:00", "end": "07:00"}


@pytest.fixture(autouse=True)
def _fresh_hold_log(monkeypatch):
    monkeypatch.setattr(delivery, "_hold_logged", False)
    monkeypatch.setattr(alert_sound, "_busy_until", 0.0)  # no earlier test's sound is "playing"


def _settings(quiet: dict):
    s = get_settings()
    s.notify.enabled = True
    s.notify.toast = False  # no real desktop toast in a test
    s.notify.ntfy_topic = None
    s.notify.webhook_url = None
    s.notify.events = {}
    s.notify.per_item_scored = False
    s.notify.quiet_hours = dict(quiet)
    return s


def _at(hour: int, minute: int = 0):
    return lambda: time(hour, minute)


async def _seed(sm, n: int = 3, kind: str = "run_finished") -> None:
    async with sm() as s:
        for i in range(n):
            s.add(Notification(kind=kind, title=f"Run {i} finished", severity="info",
                               delivered_channels=[], created_at=utcnow_iso()))
        await s.commit()


async def _channels(sm) -> list:
    async with sm() as s:
        rows = (await s.execute(select(Notification).order_by(Notification.id))).scalars().all()
    return [r.delivered_channels for r in rows]


# ------------------------------------------------------------------------------------ delivery
async def test_inside_quiet_hours_nothing_is_delivered_and_rows_stay_pending(sm):
    await _seed(sm, 3)
    async with sm() as s:
        result = await deliver_pending(s, _settings(NIGHT), clock=_at(23, 30))
    assert result.sent == 0 and result.held == 3 and result.channels == []
    assert await _channels(sm) == [[], [], []]  # still pending, not marked handled


async def test_the_first_cycle_after_the_window_sends_the_usual_single_summary(sm):
    await _seed(sm, 3)
    async with sm() as s:
        held = await deliver_pending(s, _settings(NIGHT), clock=_at(3, 0))
    assert held.sent == 0 and held.held == 3
    async with sm() as s:
        released = await deliver_pending(s, _settings(NIGHT), clock=_at(7, 0))  # the end is outside
    assert released.sent == 1 and released.collapsed == 3 and released.held == 0
    assert released.title == "Kestrel: 3 notifications"
    assert all(c for c in await _channels(sm))  # every one handled now
    async with sm() as s:  # and nothing is left for the next cycle
        again = await deliver_pending(s, _settings(NIGHT), clock=_at(7, 1))
    assert again.sent == 0 and again.held == 0


@pytest.mark.parametrize(("quiet", "hour", "minute", "held"), [
    (NIGHT, 21, 0, True),    # the start is inside
    (NIGHT, 23, 59, True),   # before midnight
    (NIGHT, 0, 0, True),     # midnight itself
    (NIGHT, 6, 59, True),    # the last minute
    (NIGHT, 7, 0, False),    # the end is outside
    (NIGHT, 12, 0, False),   # the middle of the day
    (NIGHT, 20, 59, False),  # the minute before it begins
    (DAY, 9, 0, True),       # a window inside one day
    (DAY, 16, 59, True),
    (DAY, 17, 0, False),
    (DAY, 8, 59, False),
    (OFF, 23, 30, False),    # Quiet hours off: the clock does not matter
    (OFF, 3, 0, False),
])
async def test_the_window_holds_and_releases_on_the_local_clock(sm, quiet, hour, minute, held):
    await _seed(sm, 1)
    async with sm() as s:
        result = await deliver_pending(s, _settings(quiet), clock=_at(hour, minute))
    assert (result.held == 1) is held
    assert (result.sent == 0) is held


@respx.mock
async def test_every_pop_up_channel_waits_together(sm):
    """To the person the desktop toast, the phone push and the webhook are all pop-ups."""
    ntfy = respx.post("https://ntfy.sh/kestrel-test").mock(return_value=httpx.Response(200))
    hook = respx.post("https://hooks.example/kestrel").mock(return_value=httpx.Response(200))
    await _seed(sm, 2)
    settings = _settings(NIGHT)
    settings.notify.ntfy_topic = "kestrel-test"
    settings.notify.webhook_url = "https://hooks.example/kestrel"
    async with sm() as s:
        held = await deliver_pending(s, settings, clock=_at(22, 0))
    assert held.sent == 0 and not ntfy.called and not hook.called
    async with sm() as s:
        out = await deliver_pending(s, settings, clock=_at(8, 0))
    assert out.sent == 1 and out.channels == ["ntfy", "webhook"]
    assert ntfy.called and hook.called


async def test_the_desktop_toast_is_held_too(sm, monkeypatch):
    shown: list[tuple[str, str | None]] = []
    monkeypatch.setattr(delivery, "_toast", lambda title, body: shown.append((title, body)) or True)
    await _seed(sm, 1)
    settings = _settings(NIGHT)
    settings.notify.toast = True
    async with sm() as s:
        await deliver_pending(s, settings, clock=_at(23, 0))
    assert shown == []
    async with sm() as s:
        await deliver_pending(s, settings, clock=_at(9, 0))
    assert shown == [("Run 0 finished", "")]


async def test_rows_of_a_turned_off_kind_are_not_counted_as_held(sm):
    await _seed(sm, 2, kind="run_finished")
    await _seed(sm, 1, kind="source_disabled")
    settings = _settings(NIGHT)
    settings.notify.events = {"source_disabled": False}
    async with sm() as s:
        result = await deliver_pending(s, settings, clock=_at(23, 0))
    assert result.held == 2


async def test_a_held_cycle_logs_one_line_and_an_empty_one_logs_none(sm):
    with capture_logs() as logs:
        async with sm() as s:
            await deliver_pending(s, _settings(NIGHT), clock=_at(23, 0))  # nothing pending
    assert not [e for e in logs if e["event"] == "notify_held_quiet_hours"]

    await _seed(sm, 2)
    with capture_logs() as logs:
        async with sm() as s:
            await deliver_pending(s, _settings(NIGHT), clock=_at(23, 0))
        async with sm() as s:
            await deliver_pending(s, _settings(NIGHT), clock=_at(23, 1))  # the next cycle
    held = [e for e in logs if e["event"] == "notify_held_quiet_hours"]
    assert [e["log_level"] for e in held] == ["info", "debug"]  # one info line per quiet span
    assert held[0]["held"] == 2


async def test_the_default_clock_is_the_local_wall_clock(sm, monkeypatch):
    await _seed(sm, 1)
    monkeypatch.setattr(ui_settings, "local_wall_time", lambda: time(23, 0))
    async with sm() as s:
        assert (await deliver_pending(s, _settings(NIGHT))).held == 1
    monkeypatch.setattr(ui_settings, "local_wall_time", lambda: time(12, 0))
    async with sm() as s:
        assert (await deliver_pending(s, _settings(NIGHT))).sent == 1


def test_the_delivery_loop_reads_the_quiet_hours_the_settings_page_saved(monkeypatch):
    """Quiet hours and the pop-up switch are saved as setting rows; the loop used to read only
    the settings file, so neither had any effect. One tick with the window on holds the row, one
    with it off sends it."""
    from kestrel.web import app as app_mod

    migrate.run_migrations()
    now = ui_settings.local_wall_time()
    around_now = {"enabled": True,
                  "start": f"{(now.hour - 1) % 24:02d}:{now.minute:02d}",
                  "end": f"{(now.hour + 1) % 24:02d}:{now.minute:02d}"}

    async def put(quiet: dict) -> None:
        async with get_sessionmaker()() as s:
            await s.merge(Setting(key="notify.quiet_hours", value=quiet))
            await s.merge(Setting(key="notify.toast", value=False))  # no real toast
            await s.commit()

    async def seed() -> None:
        async with get_sessionmaker()() as s:
            s.add(Notification(kind="run_finished", title="Run finished", severity="info",
                               delivered_channels=[], created_at=utcnow_iso()))
            await s.commit()

    async def channels() -> list:
        async with get_sessionmaker()() as s:
            return [r.delivered_channels for r in (await s.execute(select(Notification))).scalars()]

    async def one_tick() -> None:
        stop = asyncio.Event()

        async def _wait(_stop, _seconds) -> None:
            stop.set()

        monkeypatch.setattr(app_mod, "_wait", _wait)
        await asyncio.wait_for(app_mod._delivery_loop(stop), timeout=10.0)

    async def go() -> tuple[list, list]:
        await seed()
        await put(around_now)
        await one_tick()
        held = await channels()
        await put({**around_now, "enabled": False})
        await one_tick()
        return held, await channels()

    held, released = asyncio.run(go())
    forget_engine()
    assert held == [[]]
    assert released == [["delivered"]]


# --------------------------------------------------------- the sound goes with a delivered message
class _Player:
    """Stands in for alert_sound.play: records every call (it runs on a worker thread)."""

    def __init__(self, result: bool = True, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[str, float]] = []
        self.result = result
        self.raises = raises

    def __call__(self, name: str, volume: float, **kwargs) -> bool:
        self.calls.append((name, volume))
        if self.raises is not None:
            raise self.raises
        return self.result


@pytest.fixture
def player(monkeypatch) -> _Player:
    p = _Player()
    monkeypatch.setattr(alert_sound, "play", p)
    return p


async def test_a_delivered_cycle_plays_the_chosen_sound_once(sm, player):
    await _seed(sm, 3)  # three alerts collapse into one message: one sound
    settings = _settings(DAY)
    settings.notify.sound_name = "bell"
    settings.notify.sound_volume = 0.25
    async with sm() as s:
        result = await deliver_pending(s, settings, clock=_at(20, 0))
    assert result.sent == 1 and result.sounded is True
    assert player.calls == [("bell", 0.25)]


async def test_the_default_sound_is_the_chime_at_the_default_volume(sm, player):
    await _seed(sm, 1)
    async with sm() as s:
        await deliver_pending(s, _settings(OFF), clock=_at(12, 0))
    assert player.calls == [("chime", 0.6)]


async def test_no_sound_during_quiet_hours_and_one_with_the_summary_after(sm, player):
    await _seed(sm, 2)
    async with sm() as s:
        held = await deliver_pending(s, _settings(NIGHT), clock=_at(23, 0))
    assert held.held == 2 and held.sounded is False and player.calls == []
    async with sm() as s:
        released = await deliver_pending(s, _settings(NIGHT), clock=_at(7, 0))
    assert released.sent == 1 and player.calls == [("chime", 0.6)]


async def test_no_sound_when_the_sound_is_off_but_the_message_is_delivered(sm, player):
    await _seed(sm, 1)
    settings = _settings(OFF)
    settings.notify.sound = False
    async with sm() as s:
        result = await deliver_pending(s, settings, clock=_at(12, 0))
    assert result.sent == 1 and result.sounded is False and player.calls == []


async def test_no_sound_when_nothing_was_delivered(sm, player):
    async with sm() as s:  # nothing pending
        assert (await deliver_pending(s, _settings(OFF), clock=_at(12, 0))).sent == 0
    await _seed(sm, 2, kind="source_disabled")  # pending, but the kind is turned off
    settings = _settings(OFF)
    settings.notify.events = {"source_disabled": False}
    async with sm() as s:
        result = await deliver_pending(s, settings, clock=_at(12, 0))
    assert result.sent == 0 and result.suppressed == 2
    settings.notify.enabled = False  # the master switch
    await _seed(sm, 1)
    async with sm() as s:
        assert (await deliver_pending(s, settings, clock=_at(12, 0))).sent == 0
    assert player.calls == []


async def test_a_burst_is_one_sound_and_the_next_empty_cycle_is_none(sm, player):
    await _seed(sm, 20)
    async with sm() as s:
        await deliver_pending(s, _settings(OFF), clock=_at(12, 0))
    async with sm() as s:
        await deliver_pending(s, _settings(OFF), clock=_at(12, 1))
    assert len(player.calls) == 1


async def test_the_sound_does_not_depend_on_the_computer_notifications_switch(sm, player):
    await _seed(sm, 1)
    settings = _settings(OFF)
    settings.notify.toast = False  # nothing pops up; the alert still sounds
    async with sm() as s:
        result = await deliver_pending(s, settings, clock=_at(12, 0))
    assert result.sent == 1 and result.channels == [] and len(player.calls) == 1


async def test_a_sound_that_fails_never_undoes_the_delivery(sm, monkeypatch):
    broken = _Player(raises=RuntimeError("no sound card"))
    monkeypatch.setattr(alert_sound, "play", broken)
    await _seed(sm, 2)
    async with sm() as s:
        result = await deliver_pending(s, _settings(OFF), clock=_at(12, 0))
    assert result.sent == 1 and result.sounded is False and len(broken.calls) == 1
    assert all(c for c in await _channels(sm))  # still marked handled


# -------------------------------------------------------------------------- every pop-up is silent
def _run_toast(monkeypatch, platform: str) -> list[str]:
    ran: list[list[str]] = []
    monkeypatch.setattr(delivery.sys, "platform", platform)
    monkeypatch.setattr(delivery.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(delivery.subprocess, "run", lambda argv, **kw: ran.append(argv))
    assert delivery._toast("Run finished", "4 new") is True
    return ran[0]


def test_notify_send_carries_the_freedesktop_hint_that_keeps_the_pop_up_silent(monkeypatch):
    argv = _run_toast(monkeypatch, "linux")
    assert argv[0] == "/usr/bin/notify-send"
    assert "--hint=boolean:suppress-sound:true" in argv
    assert argv.index("--hint=boolean:suppress-sound:true") < argv.index("--")  # an option
    assert argv[argv.index("--") + 1:] == ["Run finished", "4 new"]  # the text is still text


def test_the_macos_notification_names_no_sound(monkeypatch):
    argv = _run_toast(monkeypatch, "darwin")
    assert argv[0] == "/usr/bin/osascript" and "sound name" not in " ".join(argv)


@pytest.mark.parametrize("frozen", [False, True])
def test_the_windows_toast_is_silent(monkeypatch, frozen):
    seen: list[dict] = []
    fake = types.ModuleType("win11toast")
    fake.notify = lambda title, body, **kw: seen.append(kw)
    monkeypatch.setitem(sys.modules, "win11toast", fake)
    monkeypatch.setattr(delivery.sys, "platform", "win32")
    monkeypatch.setattr(paths, "is_frozen", lambda: frozen)
    assert delivery._toast("Run finished", "4 new") is True
    assert seen[0]["audio"] == {"silent": "true"}


# ------------------------------------------------------------------------------- the sound itself
def _pcm(data: bytes) -> tuple[wave._wave_params, list[int]]:
    with wave.open(io.BytesIO(data)) as w:
        n = w.getnframes()
        return w.getparams(), list(struct.unpack(f"<{n}h", w.readframes(n)))


@pytest.mark.parametrize(("name", "seconds"), [("chime", 0.88), ("soft_click", 0.122),
                                               ("bell", 1.65)])
def test_each_sound_is_a_valid_mono_wav_of_its_recipes_length(name, seconds):
    params, pcm = _pcm(alert_sound.render(name, 0.6))
    assert (params.nchannels, params.sampwidth, params.framerate) == (1, 2, 44100)
    assert abs(len(pcm) / 44100 - seconds) < 0.002
    assert max(abs(x) for x in pcm) > 1000  # it is not silence


@pytest.mark.parametrize("name", ["chime", "soft_click", "bell"])
def test_the_peak_scales_with_the_volume(name):
    peaks = {pct: max(abs(x) for x in _pcm(alert_sound.render(name, pct / 100))[1])
             for pct in (0, 25, 50, 100)}
    assert peaks[0] == 0
    assert peaks[25] * 2 == pytest.approx(peaks[50], rel=0.01)
    assert peaks[50] * 2 == pytest.approx(peaks[100], rel=0.01)
    assert peaks[100] < 0.7 * 32767  # headroom: the top of the slider is under half of full scale


@pytest.mark.parametrize(("name", "peak"), [("chime", 0.1791), ("soft_click", 0.1739),
                                            ("bell", 0.3025)])
def test_the_levels_match_the_web_audio_original(name, peak):
    """At volume 0.6 the old browser preview's samples peaked at these levels (measured in
    Chrome's OfflineAudioContext, where Chime and Bell agreed with this drawing to 2e-5 per
    sample; Soft click's triangle is band limited there, a 0.4% difference)."""
    got = max(abs(x) for x in _pcm(alert_sound.render(name, 0.6))[1]) / 32767
    assert got == pytest.approx(peak, abs=0.003)


def test_the_chime_starts_on_its_first_tone():
    """The first 100 ms are the 1046.5 Hz tone alone (the second joins at 130 ms)."""
    pcm = _pcm(alert_sound.render("chime", 1.0))[1][: 4410]
    crossings = sum(1 for a, b in zip(pcm, pcm[1:], strict=False) if a <= 0 < b or a >= 0 > b)
    assert abs(crossings - 2 * 1046.5 * 0.1) <= 3


def test_the_same_name_and_volume_always_give_the_same_bytes():
    assert alert_sound.render("bell", 0.4) == alert_sound.render("bell", 0.4)
    assert alert_sound.render("bell", 0.4) == alert_sound.render("bell", 0.4004)  # whole percents
    assert alert_sound.render("chime", 0.4) != alert_sound.render("bell", 0.4)
    assert alert_sound.render("nonsense", 0.4) == alert_sound.render("chime", 0.4)


def test_a_sound_is_drawn_once_and_kept_in_the_cache_folder(monkeypatch):
    drawn: list[tuple[str, float]] = []
    real = alert_sound.render
    monkeypatch.setattr(alert_sound, "render", lambda n, v: drawn.append((n, v)) or real(n, v))
    first = alert_sound.wav_path("soft_click", 0.3)
    assert first.parent == paths.sounds_dir() and first.parent.is_relative_to(paths.data_home())
    assert alert_sound.wav_path("soft_click", 0.3) == first and len(drawn) == 1
    assert alert_sound.wav_path("soft_click", 0.304) == first  # the same whole percent
    other = alert_sound.wav_path("soft_click", 0.5)  # another volume is another file
    assert other != first and len(drawn) == 2
    assert _pcm(first.read_bytes())[0].nchannels == 1
    assert not list(paths.sounds_dir().glob("*.tmp"))  # nothing half written is left


def test_a_truncated_leftover_is_drawn_again():
    path = alert_sound.wav_path("chime", 0.6)
    path.write_bytes(b"RIFF")
    assert alert_sound.wav_path("chime", 0.6) == path
    assert len(path.read_bytes()) > 1000 and _pcm(path.read_bytes())[1]


@pytest.mark.parametrize("damage", ["zeros", "short", "header", "sizes", "empty"])
def test_a_damaged_cached_file_is_drawn_again_not_reused(damage):
    """A power loss can leave a file of the right length full of zeros, or cut short."""
    path = alert_sound.wav_path("chime", 0.6)
    good = path.read_bytes()
    bad = {
        "zeros": bytes(len(good)),
        "short": good[: len(good) // 2],
        "header": b"RIFX" + good[4:],
        "sizes": good[:4] + (1234).to_bytes(4, "little") + good[8:],
        "empty": b"",
    }[damage]
    path.write_bytes(bad)
    assert alert_sound.wav_path("chime", 0.6) == path
    assert path.read_bytes() == good  # drawn again, byte for byte


def test_two_threads_drawing_the_same_sound_both_succeed(monkeypatch):
    """A Play press and a delivery can both need a sound that is not cached yet: each draws into a
    temporary file of its own, and neither removes the other's."""
    both_drawing = threading.Barrier(2, timeout=10)
    real = alert_sound.render

    def render_together(name, volume):
        both_drawing.wait()  # neither has finished until both are drawing
        return real(name, volume)

    monkeypatch.setattr(alert_sound, "render", render_together)
    got: list[Path] = []
    errors: list[BaseException] = []

    def draw():
        try:
            got.append(alert_sound.wav_path("soft_click", 0.5))
        except BaseException as exc:  # noqa: BLE001 (the test reports whatever a thread raised)
            errors.append(exc)

    threads = [threading.Thread(target=draw) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not errors and len(got) == 2 and got[0] == got[1]
    assert alert_sound._intact(got[0], "soft_click")
    assert not list(paths.sounds_dir().glob("*.tmp"))  # no temporary file left behind


def test_play_draws_the_file_and_hands_its_path_to_the_player(monkeypatch):
    started: list[Path] = []
    monkeypatch.setattr(alert_sound, "_start", lambda path: started.append(path) or True)
    assert REAL_PLAY("bell", 0.4) is True
    assert started == [alert_sound.wav_path("bell", 0.4)]


def test_a_volume_of_zero_plays_nothing_and_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(alert_sound, "_start", lambda path: pytest.fail("started a player"))
    assert REAL_PLAY("chime", 0) is False
    assert not paths.sounds_dir().exists()


def test_a_player_that_cannot_start_is_skipped_and_said_once_at_debug(monkeypatch):
    def broken(_path):
        raise OSError("no audio device")

    monkeypatch.setattr(alert_sound, "_start", broken)
    monkeypatch.setattr(alert_sound, "_reported", set())
    with capture_logs() as logs:
        assert REAL_PLAY("chime", 0.6) is False
        assert REAL_PLAY("chime", 0.6) is False  # and never raises
    said = [e for e in logs if e["event"] == "alert_sound_failed"]
    assert len(said) == 1 and said[0]["log_level"] == "debug"


# ----------------------------------------------------------------------- the platform's own player
class _Popen:
    """A player process that does not exit until the test lets it: proves nothing waits for it."""

    started: list[tuple[list[str], dict]] = []
    release = threading.Event()

    def __init__(self, argv, **kwargs) -> None:
        type(self).started.append((list(argv), kwargs))

    def wait(self) -> int:
        type(self).release.wait(5)
        return 0


@pytest.fixture
def popen(monkeypatch):
    _Popen.started = []
    _Popen.release = threading.Event()
    monkeypatch.setattr(alert_sound.subprocess, "Popen", _Popen)
    monkeypatch.setattr(alert_sound, "_reported", set())
    yield _Popen
    _Popen.release.set()


def _which(*present: str):
    return lambda name: f"/usr/bin/{name}" if name in present else None


def test_windows_hands_the_file_to_winsound_without_waiting(monkeypatch):
    calls: list[tuple[str, int]] = []
    fake = types.ModuleType("winsound")
    fake.SND_FILENAME, fake.SND_ASYNC, fake.SND_NODEFAULT = 0x20000, 0x1, 0x2
    fake.PlaySound = lambda sound, flags: calls.append((sound, flags))
    monkeypatch.setitem(sys.modules, "winsound", fake)
    monkeypatch.setattr(alert_sound.sys, "platform", "win32")
    path = Path("C:/Kestrel/cache/sounds/chime.wav")
    assert alert_sound._start(path) is True
    # A file, asynchronous, and NODEFAULT: if the file cannot be played Windows plays nothing,
    # never its own default sound (D-REL-7).
    assert calls == [(str(path), 0x20000 | 0x1 | 0x2)]


def test_macos_uses_afplay(monkeypatch, popen):
    monkeypatch.setattr(alert_sound.sys, "platform", "darwin")
    monkeypatch.setattr(alert_sound.shutil, "which", _which("afplay"))
    began = time_mod.monotonic()
    assert alert_sound._start(Path("/tmp/chime.wav")) is True  # returns while the player runs
    assert time_mod.monotonic() - began < 2  # the fake player only exits after 5 s: no waiting
    argv, kwargs = popen.started[0]
    assert argv == ["/usr/bin/afplay", "/tmp/chime.wav"]
    assert kwargs["stdout"] is alert_sound.subprocess.DEVNULL


def test_linux_prefers_paplay_then_aplay(monkeypatch, popen):
    monkeypatch.setattr(alert_sound.sys, "platform", "linux")
    monkeypatch.setattr(alert_sound.shutil, "which", _which("paplay", "aplay"))
    assert alert_sound._start(Path("/tmp/a.wav")) is True
    monkeypatch.setattr(alert_sound.shutil, "which", _which("aplay"))
    assert alert_sound._start(Path("/tmp/b.wav")) is True
    assert [a for a, _ in popen.started] == [["/usr/bin/paplay", "/tmp/a.wav"],
                                             ["/usr/bin/aplay", "-q", "/tmp/b.wav"]]


@pytest.mark.parametrize(("platform", "tool"), [("darwin", "afplay"), ("linux", "paplay, aplay")])
def test_a_computer_with_no_player_skips_the_sound_and_says_so_once(
        monkeypatch, popen, platform, tool):
    monkeypatch.setattr(alert_sound.sys, "platform", platform)
    monkeypatch.setattr(alert_sound.shutil, "which", _which())
    with capture_logs() as logs:
        assert alert_sound._start(Path("/tmp/a.wav")) is False
        assert alert_sound._start(Path("/tmp/a.wav")) is False
    said = [e for e in logs if e["event"] == "alert_sound_player_missing"]
    assert len(said) == 1 and said[0]["tool"] == tool and said[0]["log_level"] == "debug"
    assert popen.started == []


def test_a_player_that_exits_with_an_error_is_said_once(monkeypatch):
    monkeypatch.setattr(alert_sound, "_reported", set())

    class Failed:
        def wait(self) -> int:
            return 3

    class Fine:
        def wait(self) -> int:
            return 0

    with capture_logs() as logs:
        alert_sound._reap(Fine(), "paplay")
        alert_sound._reap(Failed(), "paplay")
        alert_sound._reap(Failed(), "paplay")
    said = [e for e in logs if e["event"] == "alert_sound_player_failed"]
    assert len(said) == 1 and said[0]["tool"] == "paplay" and said[0]["code"] == 3


def test_the_player_is_started_with_a_reaper_that_reports_its_exit(monkeypatch, popen):
    monkeypatch.setattr(alert_sound.sys, "platform", "linux")
    monkeypatch.setattr(alert_sound.shutil, "which", _which("paplay"))
    reaped: list[tuple] = []
    monkeypatch.setattr(alert_sound, "_reap", lambda proc, tool: reaped.append((type(proc), tool)))
    assert alert_sound._start(Path("/tmp/a.wav")) is True
    for _ in range(100):
        if reaped:
            break
        time_mod.sleep(0.02)
    assert reaped == [(_Popen, "paplay")]


def _frozen_linux(monkeypatch, **environ: str) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    for key in ("LD_LIBRARY_PATH", "LD_LIBRARY_PATH_ORIG"):
        monkeypatch.delenv(key, raising=False)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)


def test_a_system_program_gets_the_sessions_library_path_not_the_frozen_apps(monkeypatch):
    from kestrel.llm.profiles import system_tool_env

    _frozen_linux(monkeypatch, LD_LIBRARY_PATH="/opt/kestrel/_internal",
                  LD_LIBRARY_PATH_ORIG="/usr/lib/custom")
    assert system_tool_env()["env"]["LD_LIBRARY_PATH"] == "/usr/lib/custom"
    _frozen_linux(monkeypatch, LD_LIBRARY_PATH="/opt/kestrel/_internal")  # there was none before
    assert "LD_LIBRARY_PATH" not in system_tool_env()["env"]
    monkeypatch.setattr(sys, "frozen", False, raising=False)  # from source: nothing changes
    assert system_tool_env() == {}
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert system_tool_env() == {}


def test_the_sound_player_and_notify_send_both_run_in_that_environment(monkeypatch, popen):
    _frozen_linux(monkeypatch, LD_LIBRARY_PATH="/opt/kestrel/_internal",
                  LD_LIBRARY_PATH_ORIG="/usr/lib/custom")
    monkeypatch.setattr(alert_sound.shutil, "which", _which("paplay"))
    monkeypatch.setattr(alert_sound, "_reap", lambda proc, tool: None)
    assert alert_sound._start(Path("/tmp/a.wav")) is True
    assert popen.started[0][1]["env"]["LD_LIBRARY_PATH"] == "/usr/lib/custom"

    ran: list[dict] = []
    monkeypatch.setattr(delivery.shutil, "which", _which("notify-send"))
    monkeypatch.setattr(delivery.subprocess, "run", lambda argv, **kw: ran.append(kw))
    assert delivery._toast("Run finished", None) is True
    assert ran[0]["env"]["LD_LIBRARY_PATH"] == "/usr/lib/custom"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(alert_sound, "_now", c)
    monkeypatch.setattr(alert_sound, "_busy_until", 0.0)
    return c


def test_an_exclusive_play_is_skipped_while_a_sound_is_still_on(monkeypatch, clock):
    started: list[Path] = []
    monkeypatch.setattr(alert_sound, "_start", lambda path: started.append(path) or True)
    assert REAL_PLAY("bell", 0.5, exclusive=True) is True and len(started) == 1
    assert alert_sound.is_playing() is True
    clock.now += 1.0  # the bell lasts 1.65 s
    assert REAL_PLAY("bell", 0.5, exclusive=True) is False and len(started) == 1
    clock.now += 1.0  # 2.0 s in: over, and past the settle margin
    assert alert_sound.is_playing() is False
    assert REAL_PLAY("bell", 0.5, exclusive=True) is True and len(started) == 2


def test_an_alert_is_never_skipped_for_a_sound_that_is_on(monkeypatch, clock):
    started: list[Path] = []
    monkeypatch.setattr(alert_sound, "_start", lambda path: started.append(path) or True)
    assert REAL_PLAY("bell", 0.5) is True and REAL_PLAY("chime", 0.5) is True
    assert len(started) == 2


def test_a_sound_that_did_not_start_does_not_hold_the_next_one_off(monkeypatch, clock):
    monkeypatch.setattr(alert_sound, "_start", lambda path: False)
    assert REAL_PLAY("chime", 0.5, exclusive=True) is False
    assert alert_sound.is_playing() is False
    monkeypatch.setattr(alert_sound, "_start", lambda path: True)
    assert REAL_PLAY("chime", 0.5, exclusive=True) is True


# ------------------------------------------------------------------ Settings > Play, same path
def _client(host: str = "127.0.0.1") -> TestClient:
    from kestrel.web.app import build_app

    async def fresh() -> None:
        await reset_engine()
        for suffix in ("", "-wal", "-shm"):
            paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
        migrate.run_migrations()
        forget_engine()

    asyncio.run(fresh())
    # The peer is this computer (TestClient's own default peer is neither loopback nor a LAN one).
    return TestClient(build_app(worker=False), client=(host, 12345))


def _preview(client: TestClient, body: dict) -> dict:
    r = client.post("/settings/sound/preview", json=body)
    assert r.status_code == 200
    return r.json()


def test_play_in_settings_plays_the_sound_as_the_controls_stand(player):
    c = _client()
    assert _preview(c, {"name": "soft_click", "volume": 0.3})["ok"] is True
    assert player.calls == [("soft_click", 0.3)]  # the same call a delivered alert makes


def test_play_in_settings_ignores_quiet_hours_because_the_person_pressed_it(player):
    c = _client()
    now = ui_settings.local_wall_time()
    around = {"enabled": True, "start": f"{(now.hour - 1) % 24:02d}:{now.minute:02d}",
              "end": f"{(now.hour + 1) % 24:02d}:{now.minute:02d}"}
    assert c.post("/settings/ui", json={"key": "notify.quiet_hours", "value": around}).json()["ok"]
    assert _preview(c, {"name": "chime", "volume": 0.6})["ok"] is True
    assert player.calls == [("chime", 0.6)]


@pytest.mark.parametrize(("body", "message"), [
    ({"name": "gong", "volume": 0.5}, "Choose one of the sounds shown."),
    ({"name": "bell", "volume": "loud"}, "Volume must be a number from 0 to 1."),
    ({"name": "bell"}, "Volume must be a number from 0 to 1."),
    ({"name": "bell", "volume": 0}, "The volume is turned all the way down."),
    ({}, "Choose one of the sounds shown."),
])
def test_play_in_settings_refuses_what_it_cannot_play_in_plain_words(player, body, message):
    reply = _preview(_client(), body)
    assert reply["ok"] is False and reply["message"] == message and player.calls == []


def test_play_in_settings_works_only_on_the_computer_running_kestrel(player):
    from kestrel.web import auth

    auth.set_cached_key("s3cret-key")
    try:
        phone = _client("192.168.1.50")
        r = phone.post("/settings/sound/preview", json={"name": "chime", "volume": 0.6},
                       headers={"X-Kestrel-Key": "s3cret-key"})  # a valid key gets in, not here
    finally:
        auth.set_cached_key(None)
    assert r.status_code == 200
    assert r.json() == {"ok": False, "message": "Play works on the computer running Kestrel."}
    assert player.calls == []  # nothing sounded on the host


def test_repeated_presses_start_one_player_at_a_time(monkeypatch):
    started: list[Path] = []
    monkeypatch.setattr(alert_sound, "play", REAL_PLAY)
    monkeypatch.setattr(alert_sound, "_start", lambda path: started.append(path) or True)
    monkeypatch.setattr(alert_sound, "_busy_until", 0.0)
    now = [1000.0]
    monkeypatch.setattr(alert_sound, "_now", lambda: now[0])
    c = _client()
    first = _preview(c, {"name": "chime", "volume": 0.6})
    assert first["ok"] is True and first["played"] is True
    for _ in range(4):  # the person leans on the button
        again = _preview(c, {"name": "chime", "volume": 0.6})
        assert again["ok"] is True and again["played"] is False and again["busy"] is True
    assert len(started) == 1
    now[0] += 5.0
    assert _preview(c, {"name": "chime", "volume": 0.6})["played"] is True
    assert len(started) == 2


def test_play_in_settings_says_so_when_this_computer_cannot_play_a_sound(monkeypatch):
    monkeypatch.setattr(alert_sound, "play", _Player(result=False))
    monkeypatch.setattr(alert_sound, "_busy_until", 0.0)
    reply = _preview(_client(), {"name": "bell", "volume": 0.5})
    assert reply == {"ok": False, "message": "Kestrel could not play a sound on this computer."}


# ---------------------------------------------------------------------------- wiring, no browser
def test_the_window_only_asks_the_app_to_play():
    ui = (STATIC / "js" / "kestrel-ui.js").read_text(encoding="utf-8")
    settings = (STATIC / "js" / "settings.js").read_text(encoding="utf-8")
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "/settings/sound/preview" in settings
    for js in (ui, settings):
        for gone in ("AudioContext", "createOscillator", "KUI.sound", "KUI.alerts", "k-alerts"):
            assert gone not in js, gone
        assert chr(0x2014) not in js  # no em dash in the interface's own words
    bell = base.split('hx-get="/notifications/bell"')[1].split(">")[0]
    assert "hx-vals" not in bell and 'hx-trigger="load, every 20s"' in base


def test_the_bell_is_back_to_its_light_query_with_no_alert_header():
    c = _client()
    asyncio.run(_raise_one())
    forget_engine()
    r = c.get("/notifications/bell")
    assert r.text == "1" and "HX-Trigger" not in r.headers


async def _raise_one() -> None:
    async with get_sessionmaker()() as s:
        s.add(Notification(kind="run_finished", title="Run finished", severity="info"))
        await s.commit()
