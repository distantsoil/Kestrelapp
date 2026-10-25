"""Settings precedence at all four levels (SPEC.md 4.6): db > env > YAML > default."""

from __future__ import annotations

from pathlib import Path

import yaml

from kestrel import config, paths
from kestrel.db.models import Setting
from kestrel.services.setting_overrides import load_settings_with_db


async def test_precedence_default_yaml_env_db(sm, tmp_path, monkeypatch):
    # Build a custom config dir whose YAML sets max_posting_age_days=30 and OMITS the mcp block, so
    # mcp.port must fall through to the CODE DEFAULT (9110).
    cfg_dir = tmp_path / "home" / "config"
    cfg_dir.mkdir(parents=True)
    packaged = Path(str(paths.packaged_config() / "settings.yml")).read_text(encoding="utf-8")
    data = yaml.safe_load(packaged)
    data["max_posting_age_days"] = 30
    data.pop("mcp", None)
    (cfg_dir / "settings.yml").write_text(yaml.safe_dump(data), encoding="utf-8")

    monkeypatch.setenv("KESTREL_DATA_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KESTREL_MAX_POSTING_AGE_DAYS", raising=False)

    s = config.load_settings()
    assert s.mcp.port == 9110  # LEVEL 1: code default (field absent from YAML)
    assert s.max_posting_age_days == 30  # LEVEL 2: YAML over default

    monkeypatch.setenv("KESTREL_MAX_POSTING_AGE_DAYS", "20")
    assert config.load_settings().max_posting_age_days == 20  # LEVEL 3: env over YAML

    async with sm() as sess:
        sess.add(Setting(key="max_posting_age_days", value=10))
        await sess.commit()
    async with sm() as sess:
        s_db = await load_settings_with_db(sess)
    assert s_db.max_posting_age_days == 10  # LEVEL 4: setting-table over env


async def test_bootstrap_keys_are_not_taken_from_db(sm, monkeypatch):
    # log_level must resolve before the DB exists, so a setting-table row for it is ignored.
    monkeypatch.delenv("KESTREL_LOG_LEVEL", raising=False)
    async with sm() as sess:
        sess.add(Setting(key="log_level", value="DEBUG"))
        await sess.commit()
    async with sm() as sess:
        s = await load_settings_with_db(sess)
    assert s.log_level == "INFO"  # DB override for a bootstrap key is dropped


async def test_content_and_state_rows_stay_out_of_the_config_layer(sm):
    """Regression (polling cost): prep notes (up to 20k characters each), the last story build
    and dismissed asks (plus the retired guides row old installs may hold) are not configuration,
    so the config layer never reads them, and
    the doctor's sync copy reads exactly the same rows. Real ui.* and stories.* fields stay."""
    from kestrel import doctor
    from kestrel.db.base import Base
    from kestrel.db.engine import make_sync_engine
    from kestrel.services.setting_overrides import db_override_dict, is_config_key

    rows = {
        "prep_notes.12": "x" * 20000,
        "stories.last_build": {"ok": True},
        "ui.guides_dismissed": ["today"],
        "ui.asks_dismissed": ["taste"],
        "ui.scheme": "teal",
        "ui.close_to_tray": False,
        "stories.merge_threshold": 0.9,
        "max_posting_age_days": 11,
    }
    async with sm() as sess:
        sess.add_all([Setting(key=k, value=v) for k, v in rows.items()])
        await sess.commit()
    async with sm() as sess:
        got = await db_override_dict(sess)
        s = await load_settings_with_db(sess)
    assert "prep_notes" not in got
    assert got["ui"] == {"scheme": "teal", "close_to_tray": False}
    assert got["stories"] == {"merge_threshold": 0.9}
    assert s.ui.scheme == "teal" and s.stories.merge_threshold == 0.9
    assert s.max_posting_age_days == 11
    assert not is_config_key("prep_notes.3") and not is_config_key("guides")
    assert is_config_key("llm.hosted_mode.enabled")

    db = paths.db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    engine = make_sync_engine(db)
    try:
        Base.metadata.create_all(engine, tables=[Setting.__table__])
        with engine.begin() as conn:
            conn.execute(Setting.__table__.insert(), [{"key": k, "value": v}
                                                      for k, v in rows.items()])
    finally:
        engine.dispose()
    assert doctor._db_overrides_sync() == got
