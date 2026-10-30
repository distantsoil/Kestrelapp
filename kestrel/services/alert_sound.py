"""Kestrel's own alert sound (SPEC-INTERFACES 6, D-UI-95, owner rule D-REL-7: never the system's).

The three sounds (Chime, Soft click, Bell) are drawn here in pure Python (standard library
`math`, `struct` and `wave`; no dependency) into a small mono WAV, once per sound and volume,
kept in the cache folder (`paths.sounds_dir()`), and played by the app process with the
platform's own player:

* Windows: `winsound.PlaySound(path, SND_FILENAME | SND_ASYNC)` (standard library),
* macOS: `afplay` (built in),
* Linux: `paplay`, else `aplay`.

Playing never waits: Windows hands the file to the sound system, the others start the player and
move on. A computer with no player skips the sound and says so once, at debug, like a missing
desktop-notification tool (kestrel.services.delivery). Nothing here touches the network.

THE RECIPE is a port of the synthesis the Settings preview used to run in the browser (the Web
Audio code that lived in `static/js/settings.js` and then `KUI.sound.play`; see git history before
this module): per tone an oscillator (sine, or triangle for the click's first tone) times an
envelope that is 0 at the start, rises linearly to the tone's peak in 10 ms, then falls
exponentially to 0.0001 at the tone's end, with the oscillator stopping 50 ms after that; all
tones sum into one gain of volume x 0.5. A change to the recipe must bump `RECIPE` so a cached
file from an older one is not reused."""

from __future__ import annotations

import asyncio
import contextlib
import io
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import NamedTuple

import structlog

from kestrel import paths

log = structlog.get_logger()

RECIPE = 1  # part of the cached file's name: bump when the sounds below change
SAMPLE_RATE = 44100
LEVEL = 0.5  # headroom: volume 1 is half of full scale (the preview's master gain)
ATTACK = 0.01  # seconds from silence to a tone's peak
FLOOR = 0.0001  # where the exponential fall ends
TAIL = 0.05  # seconds an oscillator runs past its envelope's end
DEFAULT_SOUND = "chime"


class Tone(NamedTuple):
    freq: float  # Hz
    start: float  # seconds after the sound begins
    dur: float  # seconds from start to the end of the envelope
    shape: str  # "sine" or "triangle"
    peak: float  # envelope peak, before the master gain


SOUNDS: dict[str, tuple[Tone, ...]] = {
    "chime": (
        Tone(1046.5, 0.0, 0.5, "sine", 0.6),
        Tone(1568.0, 0.13, 0.7, "sine", 0.5),
    ),
    "soft_click": (
        Tone(1800.0, 0.0, 0.05, "triangle", 0.6),
        Tone(900.0, 0.012, 0.06, "sine", 0.4),
    ),
    "bell": (
        Tone(880.0, 0.0, 1.6, "sine", 1.0 * 0.7),
        Tone(1760.0, 0.0, 1.6, "sine", 0.35 * 0.7),
        Tone(2640.0, 0.0, 1.6, "sine", 0.18 * 0.7),
        Tone(1318.5, 0.0, 1.6, "sine", 0.25 * 0.7),
    ),
}


def percent(volume: float) -> int:
    """A volume from 0 to 1 as the whole percent the slider offers (0 to 100); bad input is 0."""
    try:
        v = float(volume)
    except (TypeError, ValueError):
        return 0
    if v != v:  # NaN
        return 0
    return int(round(min(1.0, max(0.0, v)) * 100))


def _envelope(t: float, tone: Tone) -> float:
    """The tone's gain `t` seconds after its start."""
    if t < ATTACK:
        return tone.peak * t / ATTACK
    if t <= tone.dur:
        return tone.peak * (FLOOR / tone.peak) ** ((t - ATTACK) / (tone.dur - ATTACK))
    return FLOOR


def _oscillator(kind: str, cycles: float) -> float:
    """One oscillator sample at `cycles` periods since it started (phase zero, like Web Audio)."""
    s = math.sin(2.0 * math.pi * cycles)
    if kind == "triangle":
        return (2.0 / math.pi) * math.asin(s)
    return s


def seconds(name: str) -> float:
    """How long the sound lasts, in seconds."""
    return max(t.start + t.dur + TAIL for t in SOUNDS.get(name, SOUNDS[DEFAULT_SOUND]))


def _frames(tones: tuple[Tone, ...]) -> int:
    return int(math.ceil(max(t.start + t.dur + TAIL for t in tones) * SAMPLE_RATE))


def render(name: str, volume: float) -> bytes:
    """The sound as the bytes of a mono 16-bit WAV at `volume` (0 to 1, whole percents). The same
    name and volume always give the same bytes. A name that is not one of the three is Chime."""
    tones = SOUNDS.get(name, SOUNDS[DEFAULT_SOUND])
    gain = LEVEL * percent(volume) / 100.0
    total = _frames(tones)
    mix = [0.0] * total
    for tone in tones:
        first = int(round(tone.start * SAMPLE_RATE))
        last = min(total, int(round((tone.start + tone.dur + TAIL) * SAMPLE_RATE)) + 1)
        for i in range(first, last):
            t = max(0.0, i / SAMPLE_RATE - tone.start)
            mix[i] += _oscillator(tone.shape, tone.freq * t) * _envelope(t, tone)
    pcm = [int(round(max(-1.0, min(1.0, v * gain)) * 32767)) for v in mix]
    frames = struct.pack(f"<{len(pcm)}h", *pcm)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(frames)
    return out.getvalue()


def _intact(path: Path, key: str) -> bool:
    """Whether `path` is a complete WAV of this sound: the RIFF/WAVE header, its two sizes agreeing
    with the file, and the exact length the recipe draws. A file a power loss left zero filled or
    cut short fails this, and is drawn again."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            head = f.read(44)
    except OSError:
        return False
    if len(head) < 44 or size != 44 + 2 * _frames(SOUNDS[key]):
        return False
    return (
        head[0:4] == b"RIFF" and head[8:12] == b"WAVE" and head[12:16] == b"fmt "
        and head[36:40] == b"data"
        and struct.unpack("<I", head[4:8])[0] == size - 8
        and struct.unpack("<I", head[40:44])[0] == size - 44
    )


def wav_path(name: str, volume: float) -> Path:
    """The cached WAV for this sound and volume, drawn now if it is not there yet (or is a
    damaged leftover). Written to a temporary file of its own in the same folder and renamed over
    the real name, so a crash never leaves half a file under it and two threads drawing the same
    sound (a Play press and a delivery) never touch each other's file."""
    key = name if name in SOUNDS else DEFAULT_SOUND
    folder = paths.sounds_dir()
    target = folder / f"{key}-r{RECIPE}-{percent(volume):03d}.wav"
    if _intact(target, key):
        return target
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=folder, prefix=f"{target.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(render(key, volume))
        os.replace(tmp, target)
    except OSError:
        if _intact(target, key):  # the other thread's copy landed first and is in use: keep it
            return target
        raise
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()  # our own temporary file only; a no-op once it has been renamed
    return target


# Reported once per process, at debug, like kestrel.services.delivery._missing_tools_logged: a
# computer with no player must not fill the log with a line per alert.
_reported: set[str] = set()


def _once(key: str, event: str, *, level: str = "debug", **fields: object) -> None:
    if key not in _reported:
        _reported.add(key)
        getattr(log, level)(event, **fields)


def _player_argv(path: Path) -> list[str] | None:
    """The command that plays `path` on macOS or Linux, or None when this computer has none."""
    if sys.platform == "darwin":
        tool = shutil.which("afplay")
        if tool is None:
            _once("afplay", "alert_sound_player_missing", tool="afplay")
            return None
        return [tool, str(path)]
    for name, flags in (("paplay", []), ("aplay", ["-q"])):
        tool = shutil.which(name)
        if tool is not None:
            return [tool, *flags, str(path)]
    _once("paplay,aplay", "alert_sound_player_missing", tool="paplay, aplay")
    return None


def _reap(proc: subprocess.Popen, tool: str) -> None:
    """Collect a finished player off to the side, and say once if it failed (no sound server)."""
    code = proc.wait()
    if code:
        _once(f"exit:{tool}", "alert_sound_player_failed", level="info", tool=tool, code=code)


def _start(path: Path) -> bool:
    """Start playing `path` and return at once. True when a player was started."""
    if sys.platform == "win32":
        import winsound

        # SND_ASYNC returns at once; the sound system plays the file on its own thread.
        # SND_NODEFAULT: if the file cannot be played, play nothing. Without it Windows falls back
        # to its own default sound, which is exactly what Kestrel must never make (D-REL-7).
        winsound.PlaySound(
            str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
        )
        return True
    argv = _player_argv(path)
    if argv is None:
        return False
    from kestrel.llm.profiles import no_console_window, system_tool_env

    proc = subprocess.Popen(  # noqa: S603 (a fixed player and a path Kestrel wrote)
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **no_console_window(),
        **system_tool_env(),
    )
    # Nobody waits for the sound, but the child must still be collected, off to the side.
    threading.Thread(
        target=_reap, args=(proc, Path(argv[0]).name), name="alert-sound-reaper", daemon=True
    ).start()
    return True


# While a sound is on (or being made ready), a Play press does not start another: repeated
# presses must never pile players up. An alert is never skipped for it (`exclusive` is Play's).
_lock = threading.Lock()
_busy_until = 0.0  # time.monotonic() when the sound now playing ends; infinity while it is prepared
_SETTLE = 0.25  # seconds past a sound's end that a player is allowed to take to finish


def _now() -> float:
    return time.monotonic()


def is_playing() -> bool:
    """Whether a sound Kestrel started is still playing (or being made ready)."""
    return _now() < _busy_until


def play(name: str, volume: float, *, exclusive: bool = False) -> bool:
    """Play Kestrel's sound `name` at `volume` (0 to 1) without waiting for it to finish. Returns
    True when it was started. A volume of 0 is a choice, not a failure: nothing plays. With
    `exclusive` (Settings > Play) nothing starts while an earlier sound is still on. A failure to
    draw or start the sound is logged once at debug and skipped, never raised: a sound must not
    break a delivery cycle. Blocking work (drawing the file the first time, starting the player):
    run it off the event loop, as `play_async` does."""
    global _busy_until
    if percent(volume) <= 0:
        return False
    with _lock:
        if exclusive and _now() < _busy_until:
            return False
        before = _busy_until
        _busy_until = math.inf  # being made ready: an exclusive caller waits for it
    started = False
    try:
        started = _start(wav_path(name, volume))
    except (OSError, RuntimeError, ValueError) as exc:
        _once(f"failed:{type(exc).__name__}", "alert_sound_failed", error=str(exc))
    finally:
        with _lock:
            if started:
                _busy_until = _now() + seconds(name) + _SETTLE
            else:
                _busy_until = 0.0 if before == math.inf else before
    return started


async def play_async(name: str, volume: float, *, exclusive: bool = False) -> bool:
    """`play`, off the event loop (uvicorn's loop also runs the worker and every request)."""
    if exclusive:
        return await asyncio.to_thread(play, name, volume, exclusive=True)
    return await asyncio.to_thread(play, name, volume)
