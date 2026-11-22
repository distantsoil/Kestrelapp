"""D-REL-41 (owner-approved bar): a saved online key never leaves in a diagnostics bundle.

A known, invented OpenRouter key is saved the way the app saves one (providers.write_secret, the
data-home .env), an older log line is seeded with it in the shapes a leak would take (bare, in a
Bearer header, JSON-escaped inside a dumped request), and the bundle is built through the route the
desktop app uses (POST /about/diagnostics/save). The key must appear nowhere in plaintext: not in
the zip's raw bytes, not in any file inside it, and not in anything logged while it was built.
Cloud-safe: a migrated SQLite file, no network."""

from __future__ import annotations

import asyncio
import json
import logging
import zipfile
from pathlib import Path

import pytest
import structlog
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, reset_engine
from kestrel.llm import providers

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

# Invented for this test, one per key shape a provider issues (OpenRouter sk-or-, Anthropic
# sk-ant-, OpenAI-style sk-); none belongs to an account. Each is saved under its own variable.
SHAPES = {
    "sk-or": ("OPENROUTER_API_KEY", "sk-or-v1-" + "0123456789abcdef" * 4),
    "sk-ant": ("ANTHROPIC_API_KEY", "sk-ant-api03-" + "Zq7Xw2Vb9Nc4Md8Lk3Jh6Gf1" * 3),
    "sk": ("OPENAI_API_KEY", "sk-" + "Rt5Yu8Io1Pa4Sd7Fg0Hj3Kl6" * 2),
}
# The seeded lines are found again in the bundle by these markers, so dropping the log fails.
MARKERS = ("provider_read", "Authorization: Bearer", '"dump"')
REDACTED = "<redacted>"


async def _prepare() -> None:
    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()


@pytest.fixture
def downloads(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "Downloads"
    folder.mkdir()
    monkeypatch.setattr(paths, "downloads_dir", lambda: folder)
    return folder


def _seed_old_log(key: str) -> tuple[Path, int]:
    """An earlier log that leaked the key three ways; the bundle carries logs, so it must scrub."""
    logs = paths.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / "kestrel.log"
    lines = [
        json.dumps({"event": "provider_read", "key": key}),
        f"GET https://example.invalid/v1/key Authorization: Bearer {key}",
        json.dumps({"event": "dump", "raw": json.dumps({"headers": {"x": key}})}),
    ]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log, log.stat().st_size


def _fragments(key: str) -> tuple[str, ...]:
    """The key and the parts a split or partial leak would still show."""
    return (key, key[len(key) // 3:], key[-20:])


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_saved_key_appears_nowhere_in_the_bundle_or_the_logs(shape, downloads, caplog) -> None:
    from kestrel.web.app import build_app

    env_name, key = SHAPES[shape]

    def _leaks(text: str) -> list[str]:
        return [f for f in _fragments(key) if f in text]

    asyncio.run(_prepare())
    providers.write_secret(env_name, key)
    # The key really is saved where the app keeps it, so the test seeds what it claims to.
    assert key in paths.dotenv_path().read_text(encoding="utf-8")
    old_log, seeded = _seed_old_log(key)
    before = {p: p.stat().st_size for p in paths.logs_dir().rglob("*") if p.is_file()}

    caplog.set_level(logging.DEBUG)
    with structlog.testing.capture_logs() as captured:
        r = TestClient(build_app(worker=False)).post("/about/diagnostics/save")
    assert r.status_code == 200 and "Saved to" in r.text, r.text[:400]
    assert _leaks(r.text) == []

    archives = sorted(downloads.glob("kestrel-diagnostics-*.zip"))
    assert len(archives) == 1
    archive = archives[0]
    assert _leaks(archive.read_bytes().decode("latin-1")) == []
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        assert names, "the bundle is empty, so the check below proves nothing"
        bundled = ""
        for name in names:
            body = z.read(name).decode("utf-8", errors="replace")
            assert _leaks(body) == [], name
            assert _leaks(body.replace("\\/", "/")) == [], name
            bundled += body
    # The seeded lines reached the bundle, masked where the key was: dropping the log, or a
    # scrub that blanks the whole line, fails here rather than passing on an empty bundle.
    for marker in MARKERS:
        lines = [ln for ln in bundled.splitlines() if marker in ln]
        assert lines, f"the seeded log line with {marker!r} is not in the bundle"
        assert all(REDACTED in ln for ln in lines), (marker, lines)

    # Everything logged while the bundle was built: structlog events, stdlib records, and the
    # bytes any log file gained (the seeded lines themselves are the old leak, not this run's).
    assert _leaks(json.dumps(captured, default=str)) == []
    assert _leaks(caplog.text) == []
    for path in paths.logs_dir().rglob("*"):
        if not path.is_file():
            continue
        start = seeded if path == old_log else before.get(path, 0)
        with path.open("rb") as fh:
            fh.seek(start)
            assert _leaks(fh.read().decode("utf-8", errors="replace")) == [], path
