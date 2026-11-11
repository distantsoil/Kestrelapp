"""D-PERF-2: the parsed settings are reused while nothing that feeds them changed, and a change
through any writer shows on the very next load with no restart (SPEC 4.6). Cloud-safe."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import config, paths
from kestrel.db import migrate
from kestrel.db.engine import make_async_engine, reset_engine
from kestrel.db.models import Setting

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


@pytest.fixture
def user_yml() -> Path:
    """The operator's own settings.yml, seeded from the packaged one."""
    paths.ensure_dirs()
    live = paths.config_dir() / "settings.yml"
    shutil.copyfile(Path(str(paths.packaged_config() / "settings.yml")), live)
    config._parsed["entry"] = None
    return live


@pytest.fixture
def db():
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    yield async_sessionmaker(engine, expire_on_commit=False)
    asyncio.run(engine.dispose())


def test_the_second_load_does_not_parse_again(user_yml, monkeypatch):
    first = config.load_settings()
    calls: list[int] = []
    real = config.Settings.__init__

    def counting(self, *a, **kw):
        calls.append(1)
        real(self, *a, **kw)

    monkeypatch.setattr(config.Settings, "__init__", counting)
    second = config.load_settings()
    assert calls == []
    assert second == first and second is not first


def test_a_caller_cannot_change_the_cache(user_yml):
    a = config.load_settings()
    a.log_level = "ERROR"
    a.thresholds.apply = 1.0
    a.llm.tiers.clear()
    b = config.load_settings()
    assert b.log_level != "ERROR" and b.thresholds.apply != 1.0 and b.llm.tiers
    assert b == config.load_settings()


def test_a_hand_edit_of_settings_yml_shows_on_the_next_load(user_yml):
    assert config.load_settings().log_level == "INFO"
    user_yml.write_text(user_yml.read_text("utf-8") + "\nlog_level: DEBUG\n", encoding="utf-8")
    assert config.load_settings().log_level == "DEBUG"


def test_an_edit_that_keeps_size_and_timestamp_still_shows(user_yml):
    base = user_yml.read_text("utf-8") + "\nlog_level: INFO\n"
    user_yml.write_text(base, encoding="utf-8")
    stamp = user_yml.stat()
    assert config.load_settings().log_level == "INFO"
    user_yml.write_text(base.replace("INFO", "WARN"), encoding="utf-8")  # same length
    os.utime(user_yml, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert config.load_settings().log_level == "WARN"


def test_the_app_writer_and_its_undo_show_on_the_next_load(user_yml):
    from kestrel.services import lan_access

    assert config.load_settings().server.bind_all is False
    lan_access.set_bind_all(True)
    assert config.load_settings().server.bind_all is True
    lan_access.set_bind_all(False)  # Undo
    assert config.load_settings().server.bind_all is False


def test_a_restored_backup_shows_on_the_next_load(user_yml, tmp_path):
    from kestrel.services import backup

    base = user_yml.read_text("utf-8") + "\nlog_level: INFO\n"
    user_yml.write_text(base, encoding="utf-8")
    assert config.load_settings().log_level == "INFO"
    staged = tmp_path / "settings.yml"
    staged.write_text(base.replace("INFO", "WARN"), encoding="utf-8")  # the same length
    os.utime(staged, ns=(user_yml.stat().st_atime_ns, user_yml.stat().st_mtime_ns))
    backup._restore_settings_yml(staged, user_yml)  # copy2 keeps the staged file's timestamps
    assert config.load_settings().log_level == "WARN"


def test_the_environment_shows_on_the_next_load(user_yml, monkeypatch):
    assert config.load_settings().log_level == "INFO"
    monkeypatch.setenv("KESTREL_LOG_LEVEL", "ERROR")
    assert config.load_settings().log_level == "ERROR"
    monkeypatch.delenv("KESTREL_LOG_LEVEL")
    assert config.load_settings().log_level == "INFO"


def test_a_dotenv_change_shows_on_the_next_load(user_yml, monkeypatch):
    monkeypatch.delenv("KESTREL_LOG_LEVEL", raising=False)
    assert config.load_settings().log_level == "INFO"
    paths.dotenv_path().write_text("KESTREL_LOG_LEVEL=ERROR\n", encoding="utf-8")
    try:
        assert config.load_settings().log_level == "ERROR"
    finally:
        paths.dotenv_path().unlink()
        config.load_dotenv_into_environ()
    assert config.load_settings().log_level == "INFO"


def test_a_database_override_shows_on_the_next_load_set_changed_and_removed(user_yml, db):
    from kestrel.services.setting_overrides import load_settings_with_db

    async def go():
        async with db() as s:
            assert (await load_settings_with_db(s)).max_posting_age_days == 45
            s.add(Setting(key="max_posting_age_days", value=10))
            await s.commit()
            assert (await load_settings_with_db(s)).max_posting_age_days == 10
        async with db() as s:  # a second session, as the worker loop is
            row = await s.get(Setting, "max_posting_age_days")
            row.value = 20
            await s.commit()
        async with db() as s:
            assert (await load_settings_with_db(s)).max_posting_age_days == 20
            await s.delete(await s.get(Setting, "max_posting_age_days"))
            await s.commit()
            assert (await load_settings_with_db(s)).max_posting_age_days == 45

    asyncio.run(go())


def test_the_settings_page_writer_shows_on_the_next_load(user_yml, db):
    from kestrel.services.setting_overrides import load_settings_with_db
    from kestrel.web.pages.settings import _set, _unset

    async def go():
        async with db() as s:
            before = (await load_settings_with_db(s)).thresholds.apply
            await _set(s, "thresholds.apply", 4.9)
            await s.commit()
            assert (await load_settings_with_db(s)).thresholds.apply == 4.9
            await _unset(s, "thresholds.apply")
            await s.commit()
            assert (await load_settings_with_db(s)).thresholds.apply == before

    asyncio.run(go())


def test_the_worker_cycle_reads_a_change_made_between_cycles(user_yml, db):
    """The serve loop calls load_settings_with_db once per cycle on a fresh session."""
    from kestrel.services.setting_overrides import load_settings_with_db

    async def cycle() -> int:
        async with db() as s:
            return (await load_settings_with_db(s)).max_posting_age_days

    async def go():
        assert await cycle() == 45
        async with db() as s:
            s.add(Setting(key="max_posting_age_days", value=7))
            await s.commit()
        assert await cycle() == 7

    asyncio.run(go())


def test_mode_is_resolved_per_call_not_cached(user_yml):
    local = config.load_settings(mode="local")
    assert local.llm.mode == "local"
    with pytest.raises(config.HostedModeError):
        config.load_settings(mode="hosted")  # an explicit hosted that cannot run still refuses
    assert config.load_settings().llm.mode == config.load_settings(mode="local").llm.mode
