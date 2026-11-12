"""Restore is an import (D-SEC-20).

INVARIANT: nothing in a backup can change how this computer runs (local-only settings, keys,
consent, schema objects, queued work, model files) beyond the data rows a person would expect to
get back.

The backup's database is only read. A new database is built from Kestrel's own migrations and the
rows of an explicit list of tables are copied into it, column by column of OUR schema. So a view or
a trigger the backup carries never reaches the live database, a `setting` that is not a real table
brings nothing, the task queue is never restored, and a config file is installed only under a name
Kestrel uses, with settings.yml in any spelling going through the local-only merge. All data here is
invented; nothing touches the network."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import zipfile

import pytest
import yaml

from kestrel import config as config_mod
from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, reset_engine
from kestrel.services import backup as bk
from kestrel.services.backup import create_backup, restore_backup

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

FAR = "https://far-away.example/v1"
HOSTILE_LLM = {
    "backends": {"farserver": {"base_url": FAR, "api_key_env": "OPENROUTER_API_KEY",
                               "external": True}},
    "hosted_mode": {"enabled": True, "acknowledged_at": "2026-01-01T00:00:00Z",
                    "redact_confidential": False},
}
NOW = "2026-01-01T00:00:00+00:00"


# ------------------------------------------------------------------------------------- helpers
def _fresh_db() -> None:
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()


def _this_computer() -> None:
    """A computer with the packaged settings.yml, confidential mode on and its own LAN key."""
    _fresh_db()
    paths.seed_config()
    shutil.copy2(str(paths.packaged_config() / "settings.yml"),
                 paths.config_dir() / "settings.yml")
    _exec("INSERT INTO setting (key, value, updated_at) VALUES ('confidential_mode', 'true', ?)",
          (NOW,))
    _exec("INSERT INTO setting (key, value, updated_at) VALUES ('server.shared_secret', ?, ?)",
          (json.dumps("this-computers-own-key"), NOW))


def _exec(sql: str, args: tuple = ()) -> list:
    con = sqlite3.connect(str(paths.db_path()))
    try:
        rows = con.execute(sql, args).fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def _rows() -> dict:
    return {k: json.loads(v) for k, v in _exec("SELECT key, value FROM setting")}


def _effective():
    from kestrel.services.setting_overrides import overrides_from_rows

    return config_mod.load_settings(db_overrides=overrides_from_rows(_rows().items()))


def _schema(db) -> set[tuple[str, str, str]]:
    con = sqlite3.connect(str(db))
    try:
        return {
            (t, n, s or "") for t, n, s in con.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        con.close()


def _rezip(archive, out, mutate_db=None, extra=None, drop=()):
    work = out.parent / (out.stem + "-unpacked")
    work.mkdir()
    with zipfile.ZipFile(archive) as z:
        z.extractall(work)
    if mutate_db:
        con = sqlite3.connect(str(work / "kestrel.db"))
        mutate_db(con)
        con.commit()
        con.close()
    with zipfile.ZipFile(out, "w") as z:
        for f in sorted(work.rglob("*")):
            name = f.relative_to(work).as_posix()
            if f.is_file() and name not in drop:
                z.write(f, name)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return out


def _a_backup(tmp_path):
    _fresh_db()
    paths.seed_config()
    return create_backup(tmp_path)


# --------------------------------------------------------------------------- schema objects
def test_a_view_named_setting_brings_nothing_and_local_only_rules_hold(tmp_path):
    """The backup renames `setting` and puts a view in its place that serves a whole `llm` row,
    confidential mode off and a LAN key the uploader knows. None of it is restored."""
    archive = _a_backup(tmp_path)

    def mutate(con):
        con.execute("ALTER TABLE setting RENAME TO setting_kept_aside")
        con.execute("CREATE VIEW setting AS SELECT key, value, updated_at FROM setting_kept_aside")
        for key, value in (("llm", HOSTILE_LLM), ("confidential_mode", False),
                           ("server.shared_secret", "a-key-the-uploader-knows")):
            con.execute("INSERT INTO setting_kept_aside (key, value, updated_at) VALUES (?, ?, ?)",
                        (key, json.dumps(value), NOW))

    crafted = _rezip(archive, tmp_path / "view.zip", mutate_db=mutate)
    _this_computer()

    result = restore_backup(crafted)  # the box is NOT ticked

    s = _effective()
    assert "farserver" not in s.llm.backends
    assert s.llm.hosted_mode.enabled is False
    assert s.confidential_mode is True
    assert _rows()["server.shared_secret"] == "this-computers-own-key"
    names = {n for _t, n, _s in _schema(paths.db_path())}
    assert "setting_kept_aside" not in names
    assert ("table", "setting") in {(t, n) for t, n, _s in _schema(paths.db_path())}
    assert result.tables_ignored == 1 and result.schema_ignored == 1  # the extra table, the view


def test_a_trigger_in_the_backup_is_never_installed(tmp_path):
    """A trigger that would rewrite local-only rows on the first ordinary write after the restore
    does not exist in the live database, and that write changes no setting."""
    archive = _a_backup(tmp_path)

    def mutate(con):
        con.execute(
            "CREATE TRIGGER planted AFTER INSERT ON status_event BEGIN "
            "INSERT OR REPLACE INTO setting (key, value, updated_at) VALUES "
            f"('llm', '{json.dumps(HOSTILE_LLM)}', '{NOW}'); "
            "INSERT OR REPLACE INTO setting (key, value, updated_at) VALUES "
            f"('confidential_mode', 'false', '{NOW}'); END"
        )

    crafted = _rezip(archive, tmp_path / "trigger.zip", mutate_db=mutate)
    _this_computer()

    result = restore_backup(crafted)

    triggers = {n for t, n, _s in _schema(paths.db_path()) if t == "trigger"}
    assert "planted" not in triggers
    con = sqlite3.connect(str(paths.db_path()))
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("INSERT INTO status_event (pursuit_id, to_status, occurred_at) "
                "VALUES (1, 'applied', ?)", (NOW,))
    con.commit()
    con.close()
    s = _effective()
    assert "farserver" not in s.llm.backends and s.confidential_mode is True
    assert result.schema_ignored == 1


def test_the_restored_database_has_exactly_the_schema_a_fresh_install_has(tmp_path):
    archive = _a_backup(tmp_path)

    def mutate(con):
        con.execute("CREATE TABLE stowaway (x TEXT)")
        con.execute("INSERT INTO stowaway VALUES ('invented')")
        con.execute("CREATE VIEW peek AS SELECT * FROM org")
        con.execute("CREATE INDEX planted_index ON org (name)")
        con.execute("ALTER TABLE org ADD COLUMN extra_column TEXT")

    crafted = _rezip(archive, tmp_path / "extra.zip", mutate_db=mutate)
    _this_computer()
    reference = tmp_path / "reference.db"
    migrate.run_migrations(reference)

    result = restore_backup(crafted)

    assert _schema(paths.db_path()) == _schema(reference)
    assert result.tables_ignored == 1 and result.schema_ignored == 1
    joined = " ".join(result.summary_lines())
    assert "2 items in the backup's database are not part of Kestrel and were left out." in joined


def test_the_restored_tables_list_covers_every_table_of_the_schema():
    """Every table is classified on purpose: a new table must be added to one list or the other."""
    import kestrel.db.models  # noqa: F401
    from kestrel.db.base import Base

    assert bk.RESTORED_TABLES | (bk.NOT_RESTORED_TABLES - {"alembic_version"}) == set(
        Base.metadata.tables)
    assert not bk.RESTORED_TABLES & bk.NOT_RESTORED_TABLES
    assert "task" in bk.NOT_RESTORED_TABLES


# ------------------------------------------------------------------------------ queued work
def _add_task(type_: str, payload: dict, status: str = "pending") -> None:
    _exec("INSERT INTO task (type, payload, status, priority, attempts, max_attempts, created_at, "
          "updated_at) VALUES (?, ?, ?, 0, 0, 3, ?, ?)", (type_, json.dumps(payload), status,
                                                         NOW, NOW))


def test_no_queued_work_and_no_run_marker_comes_from_a_backup(tmp_path):
    """A model pull from a registry the backup names, a rebuild of a tier's model, a Score again
    on a model the backup chose, a held (deferred) consent task, a one-run model marker, an owed
    rebuild and an Undo token: none of them is in the restored database."""
    _fresh_db()
    paths.seed_config()
    _add_task("ollama_pull", {"tag": "registry.invented.example/x/y:latest", "batch": "b"})
    _add_task("ollama_create", {"name": "kestrel-tier2", "tier": "tier2",
                                "base": "registry.invented.example/x/y:latest",
                                "modelfile": "FROM registry.invented.example/x/y:latest",
                                "profile": "invented", "batch": "b"})
    _add_task("t2", {"opportunity_id": 7, "rescore": True,
                     "model_override": {"backend": "frontier", "model": "vendor/invented"}})
    _add_task("t2", {"opportunity_id": 8, "rescore": True, "deferred": True,
                     "defer_reason": "corrections"})
    _add_task("t1", {"opportunity_id": 9}, status="done")
    for key, value in (("run_model.pending.7", {"backend": "frontier", "model": "x"}),
                       ("setup.rebuild_owed", True), ("setup.recheck_owed", True),
                       ("undo.correction.abc", {"note": "invented"}),
                       ("ui.scheme", "dark")):
        _exec("INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?)",
              (key, json.dumps(value), NOW))
    archive = create_backup(tmp_path)
    _this_computer()

    result = restore_backup(archive)

    assert _exec("SELECT COUNT(*) FROM task") == [(0,)]
    rows = _rows()
    for gone in ("run_model.pending.7", "setup.rebuild_owed", "setup.recheck_owed",
                 "undo.correction.abc"):
        assert gone not in rows
    assert rows["ui.scheme"] == "dark"  # ordinary data rows do come back
    assert result.tasks_not_restored == 5
    assert "5 queued or finished jobs in the backup were not restored" in " ".join(
        result.summary_lines())


# -------------------------------------------------------------------- older and newer schema
def test_a_backup_from_an_older_schema_is_migrated_and_restored(tmp_path):
    older = tmp_path / "older"
    older.mkdir()
    db = older / "kestrel.db"
    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        from alembic import command

        command.upgrade(migrate._config(str(mig_dir), db), "0031")
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO org (name, normalized_name, first_seen_at, last_seen_at) VALUES "
                "('Invented Older Org', 'invented older org', ?, ?)", (NOW, NOW))
    con.commit()
    con.close()
    archive = tmp_path / "older.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.write(db, "kestrel.db")
    _this_computer()

    result = restore_backup(archive)

    assert result.db_restored
    assert _exec("SELECT name FROM org") == [("Invented Older Org",)]
    assert _exec("SELECT version_num FROM alembic_version") == [(migrate.head_revision(),)]
    assert db.read_bytes()  # the work was done on a copy


def test_a_backup_that_cannot_be_brought_up_to_date_changes_nothing(tmp_path):
    """An old-schema backup whose migration fails (here: `setting` is a view the migrations
    cannot alter) is refused before the live database is touched."""
    older = tmp_path / "older"
    older.mkdir()
    db = older / "kestrel.db"
    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        from alembic import command

        command.upgrade(migrate._config(str(mig_dir), db), "0031")
    con = sqlite3.connect(str(db))
    con.execute("DROP TABLE opportunity")  # a later migration alters it
    con.execute("CREATE TABLE opportunity (id INTEGER PRIMARY KEY)")
    con.execute("DROP TABLE task")
    con.commit()
    con.close()
    archive = tmp_path / "broken.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.write(db, "kestrel.db")
    _this_computer()
    before = _schema(paths.db_path()), _rows()

    with pytest.raises(bk.InvalidBackupError):
        restore_backup(archive)

    assert (_schema(paths.db_path()), _rows()) == before
    assert not list(paths.db_path().parent.glob("kestrel.db.restore-*"))


# ---------------------------------------------------------------------------- config files
HOSTILE_YML = yaml.safe_dump({"max_posting_age_days": 17, "llm": {
    "hosted_mode": {"enabled": True, "acknowledged_at": "2026-01-01T00:00:00Z",
                    "redact_confidential": False},
    "backends": {"farserver": {"base_url": FAR, "api_key_env": "OPENROUTER_API_KEY",
                               "external": True}},
}})


@pytest.mark.parametrize("entry", ["config/Settings.yml", "config/SETTINGS.YML",
                                   "config/settings.yml.", "config/settings.yml "])
def test_settings_yml_in_any_spelling_goes_through_the_merge(tmp_path, entry):
    archive = _a_backup(tmp_path)
    crafted = _rezip(archive, tmp_path / "case.zip", extra={entry: HOSTILE_YML},
                     drop=("config/settings.yml",))
    _this_computer()

    result = restore_backup(crafted)

    cfg = paths.config_dir()
    assert sorted(p.name for p in cfg.iterdir() if p.name.lower().startswith("settings")) == [
        "settings.yml"]
    merged = yaml.safe_load((cfg / "settings.yml").read_text(encoding="utf-8"))
    assert merged["max_posting_age_days"] == 17  # the file was used, through the merge
    assert "settings.yml" in result.config_files
    assert "farserver" not in merged["llm"].get("backends", {})
    assert merged["llm"].get("hosted_mode", {}).get("enabled") is not True
    assert result.settings_kept >= 1


def test_the_preview_reads_exactly_the_settings_file_the_restore_merges(tmp_path):
    from kestrel.services.backup_preview import preview_backup

    archive = _a_backup(tmp_path)
    crafted = _rezip(archive, tmp_path / "case.zip", extra={"config/Settings.yml": HOSTILE_YML},
                     drop=("config/settings.yml",))

    preview = preview_backup(crafted)

    assert any(i.label == "farserver" for i in preview.servers)


def test_a_config_file_kestrel_does_not_use_is_skipped_and_counted(tmp_path):
    archive = _a_backup(tmp_path)
    crafted = _rezip(archive, tmp_path / "extra.zip", extra={
        "config/hardware_profile.yml": "profile: invented\n",
        "config/SETTIN~1.YML": HOSTILE_YML,
        "config/models/x.yml": "a: 1\n",
    })
    _this_computer()

    result = restore_backup(crafted)

    cfg = paths.config_dir()
    assert not (cfg / "hardware_profile.yml").exists()
    assert not (cfg / "SETTIN~1.YML").exists()
    assert not (cfg / "models" / "x.yml").exists()
    assert result.config_skipped == 3
    assert "3 config files in the backup are not one Kestrel uses" in " ".join(
        result.summary_lines())


def test_config_plan_prefers_the_exact_spelling():
    plan, skipped = bk.config_plan(["config/Settings.yml", "config/settings.yml",
                                    "config/filters.yml", "config/other.yml"])
    assert plan == {"settings.yml": "config/settings.yml", "filters.yml": "config/filters.yml"}
    assert skipped == 2


# ---------------------------------------------------------------------------- result copy
def test_the_result_names_no_path_and_uses_no_plural_shortcut(tmp_path):
    archive = _a_backup(tmp_path)
    _this_computer()

    result = restore_backup(archive)

    joined = " ".join(result.summary_lines())
    assert result.pre_restore_backup is not None
    assert str(result.pre_restore_backup.parent) not in joined
    assert result.pre_restore_backup.name in joined
    assert "(s)" not in joined
