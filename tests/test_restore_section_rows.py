"""D-SEC-16: a restore filters setting rows by their leaf keys, whatever shape the backup used.

Invariants proven here:
- A restore never changes a local-only setting unless the opt-in box is ticked, whatever shape
  the backup's setting rows take (a whole `llm` or `notify` section row included), and never
  restores api_key_env, consent keys or hosted acceptance from a backup.
- Nothing is sent online with this computer's saved key to an address the person did not set on
  this computer: with the box ticked, a backend that uses a key saved here keeps this computer's
  address, in the database and in settings.yml.

Invented addresses only (`.example` and documentation ranges); nothing is ever contacted."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from kestrel import config as config_mod
from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, reset_engine
from kestrel.services.backup import create_backup, restore_backup

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

EVIL = "https://evil.example/v1"
HOOK = "https://hooks.example/abc/INVENTEDTOKEN"
REAL_FRONTIER = "https://openrouter.ai/api/v1"  # the packaged default for that backend

# The crafted rows of the review finding: a whole `llm` section that adds a backend at an internet
# address reading this computer's online key, switches Hosted mode on with its terms accepted and
# picks the online mode; and a whole `notify` section that moves the notice webhook.
LLM_SECTION = {
    "mode": "hosted",
    "backends": {"evil": {"base_url": EVIL, "api_key_env": "OPENROUTER_API_KEY",
                          "external": True}},
    "hosted_mode": {"enabled": True, "acknowledged_at": "2026-01-01T00:00:00Z",
                    "acknowledged_terms": "v1"},
}
NOTIFY_SECTION = {"webhook_url": HOOK, "toast": False}


def _wipe_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = paths.db_path().with_name(paths.db_path().name + suffix)
        p.unlink(missing_ok=True)


def _fresh_db() -> None:
    asyncio.run(reset_engine())
    _wipe_db()
    migrate.run_migrations()
    forget_engine()


def _put(key: str, value) -> None:
    con = sqlite3.connect(str(paths.db_path()))
    try:
        con.execute("DELETE FROM setting WHERE key = ?", (key,))
        con.execute("INSERT INTO setting (key, value, updated_at) VALUES (?, ?, '2026-01-01')",
                    (key, json.dumps(value)))
        con.commit()
    finally:
        con.close()


def _rows() -> dict:
    con = sqlite3.connect(str(paths.db_path()))
    try:
        return {k: json.loads(v) for k, v in con.execute("SELECT key, value FROM setting")}
    finally:
        con.close()


def _effective() -> config_mod.Settings:
    """What the app would load from the restored data: settings.yml with the rows on top."""
    from kestrel.services.setting_overrides import overrides_from_rows

    return config_mod.load_settings(db_overrides=overrides_from_rows(_rows().items()))


def _crafted_archive(tmp_path):
    _fresh_db()
    paths.seed_config()
    _put("llm", LLM_SECTION)
    _put("notify", NOTIFY_SECTION)
    return create_backup(tmp_path)


def _this_computer() -> None:
    import shutil

    _fresh_db()
    paths.seed_config()
    shutil.copy2(str(paths.packaged_config() / "settings.yml"), paths.config_dir() / "settings.yml")
    _put("llm.mode", "local")


def _assert_nothing_local_came_in(s: config_mod.Settings) -> None:
    assert "evil" not in s.llm.backends
    assert s.llm.mode == "local"
    assert s.llm.hosted_mode.enabled is False
    assert not s.llm.hosted_mode.acknowledged_at
    assert not s.notify.webhook_url


def test_whole_section_rows_are_local_only():
    from kestrel.web.settings_io import needs_local

    for key in ("llm", "notify", "server", "research", "ingest", "mcp", "models", "profile",
                "updates", "ui", "llm.hosted_mode", "llm.backends", "", "notify.webhook_url.x",
                "ui.autostart.extra", "hosted_mode"):
        assert needs_local(key, {}), key
    for key in ("notify.toast", "notify.events.run_done", "ui.scheme", "thresholds.apply",
                "scoring.weights.title", "learning.enabled"):
        assert not needs_local(key, True), key


def test_unticked_restore_drops_the_crafted_section_rows(tmp_path):
    archive = _crafted_archive(tmp_path)
    _this_computer()
    result = restore_backup(archive)
    _assert_nothing_local_came_in(_effective())
    rows = _rows()
    assert "llm" not in rows
    # The part of `notify` that is not local-only is still the backup's.
    assert rows.get("notify") == {"toast": False}
    assert _effective().notify.toast is False
    assert result.settings_kept > 0
    assert EVIL not in " ".join(result.summary_lines())


def test_ticked_restore_takes_the_section_but_never_the_key_or_the_consent(tmp_path):
    archive = _crafted_archive(tmp_path)
    _this_computer()
    restore_backup(archive, restore_local_settings=True)
    s = _effective()
    # Ticked: the AI and connection settings are the backup's (the stored mode is "hosted";
    # it cannot run, and loads as local, because the consent below was not restored)...
    assert _rows()["llm"]["mode"] == "hosted" and s.llm.mode == "local"
    assert s.llm.backends["evil"].base_url == EVIL
    assert s.notify.webhook_url == HOOK
    # ...but never which key a backend reads, and never the Hosted mode consent.
    assert s.llm.backends["evil"].api_key_env is None
    assert s.llm.hosted_mode.enabled is False
    assert not s.llm.hosted_mode.acknowledged_at
    assert not getattr(s.llm.hosted_mode, "acknowledged_terms", None)


def test_the_preview_lists_what_a_section_row_would_set(tmp_path):
    from kestrel.services.backup_preview import preview_backup

    archive = _crafted_archive(tmp_path)
    _this_computer()
    pv = preview_backup(archive)
    servers = " ".join(f"{i.label} {i.value}" for i in pv.servers)
    assert "evil" in servers and "evil.example" in servers and pv.any_internet
    dest = " ".join(f"{i.label} {i.value}" for i in pv.destinations)
    assert "Notification webhook" in dest and "INVENTEDTOKEN" not in dest
    assert any(i.label == "Where the models run" and i.value == "hosted" for i in pv.switches)
    assert _rows().get("llm.mode") == "local"  # previewing applied nothing


def test_a_backup_cannot_point_this_computers_key_at_its_own_address(tmp_path):
    """The packaged `frontier` backend reads this computer's OPENROUTER_API_KEY. A ticked
    restore of a row that moves its address must keep this computer's address."""
    _fresh_db()
    paths.seed_config()
    _put("llm.backends.frontier.base_url", EVIL)
    _put("llm.backends.lan_box", {"base_url": "http://192.168.7.7:8000/v1"})
    archive = create_backup(tmp_path)
    _this_computer()
    _put("llm.backends.lan_box", {"base_url": "http://192.168.7.8:8000/v1",
                                  "api_key_env": "KESTREL_LAN_KEY_LAN_BOX"})
    result = restore_backup(archive, restore_local_settings=True)
    s = _effective()
    assert s.llm.backends["frontier"].base_url == REAL_FRONTIER
    assert s.llm.backends["frontier"].api_key_env == "OPENROUTER_API_KEY"
    assert s.llm.backends["lan_box"].base_url == "http://192.168.7.8:8000/v1"
    assert s.llm.backends["lan_box"].api_key_env == "KESTREL_LAN_KEY_LAN_BOX"
    assert result.addresses_kept == 2
    text = " ".join(result.summary_lines())
    assert "kept the address set on this computer" in text
    assert "evil.example" not in text and "192.168" not in text


def test_a_keyless_backend_still_takes_the_backups_address(tmp_path):
    """Moving computers: a server with no key saved here takes the backup's address."""
    _fresh_db()
    paths.seed_config()
    _put("llm.backends.local.base_url", "http://192.168.7.9:11434/v1")
    archive = create_backup(tmp_path)
    _this_computer()
    result = restore_backup(archive, restore_local_settings=True)
    assert _effective().llm.backends["local"].base_url == "http://192.168.7.9:11434/v1"
    assert result.addresses_kept == 0


def test_settings_yml_cannot_move_a_keyed_backend_either(tmp_path):
    import yaml

    _fresh_db()
    paths.seed_config()
    yml = paths.config_dir() / "settings.yml"
    crafted = yaml.safe_load(yml.read_text(encoding="utf-8"))
    crafted["llm"]["backends"]["frontier"]["base_url"] = EVIL
    yml.write_text(yaml.safe_dump(crafted), encoding="utf-8")
    archive = create_backup(tmp_path)
    _this_computer()
    result = restore_backup(archive, restore_local_settings=True)
    got = yaml.safe_load(yml.read_text(encoding="utf-8"))
    assert got["llm"]["backends"]["frontier"]["base_url"] == REAL_FRONTIER
    assert got["llm"]["backends"]["frontier"]["api_key_env"] == "OPENROUTER_API_KEY"
    assert result.addresses_kept == 1


def test_a_server_row_written_by_the_models_page_stays_one_row(tmp_path):
    """Regrouping keeps the shape the Models page deletes by (`llm.backends.<name>`)."""
    _fresh_db()
    paths.seed_config()
    _put("llm.backends.lan_box", {"base_url": "http://192.168.7.7:8000/v1", "json_mode": "none"})
    archive = create_backup(tmp_path)
    _this_computer()
    restore_backup(archive, restore_local_settings=True)
    rows = _rows()
    assert rows["llm.backends.lan_box"] == {"base_url": "http://192.168.7.7:8000/v1",
                                            "json_mode": "none"}
    assert not any(k.startswith("llm.backends.lan_box.") for k in rows)


# D-SEC-17: whether an online send hides the current employer is this computer's choice, like the
# Hosted mode consent: a backup never lowers it, in any row shape, ticked or not.
@pytest.mark.parametrize("ticked", [False, True])
@pytest.mark.parametrize("shape", ["leaf", "section", "llm"])
def test_a_backup_never_lowers_redaction_of_online_sends(tmp_path, ticked, shape):
    _fresh_db()
    paths.seed_config()
    if shape == "leaf":
        _put("llm.hosted_mode.redact_confidential", False)
    elif shape == "section":
        _put("llm.hosted_mode", {"redact_confidential": False})
    else:
        _put("llm", {"hosted_mode": {"redact_confidential": False}})
    archive = create_backup(tmp_path)
    _this_computer()
    restore_backup(archive, restore_local_settings=ticked)
    assert _effective().llm.hosted_mode.redact_confidential is True


def test_settings_yml_cannot_lower_redaction_either(tmp_path):
    import yaml

    _fresh_db()
    paths.seed_config()
    yml = paths.config_dir() / "settings.yml"
    crafted = yaml.safe_load(yml.read_text(encoding="utf-8"))
    crafted["llm"].setdefault("hosted_mode", {})["redact_confidential"] = False
    yml.write_text(yaml.safe_dump(crafted), encoding="utf-8")
    archive = create_backup(tmp_path)
    _this_computer()
    restore_backup(archive, restore_local_settings=True)
    got = yaml.safe_load(yml.read_text(encoding="utf-8"))
    assert (got["llm"].get("hosted_mode") or {}).get("redact_confidential", True) is True


def test_the_preview_says_redaction_is_kept_and_does_not_count_it(tmp_path):
    from kestrel.services.backup_preview import preview_backup

    _this_computer()
    plain = preview_backup(create_backup(tmp_path / "plain"))
    _put("llm.hosted_mode.redact_confidential", False)
    archive = create_backup(tmp_path)
    _this_computer()
    pv = preview_backup(archive)
    assert pv.other_count == plain.other_count  # never counted as something the backup sets
    assert any("hide your current employer" in line for line in pv.kept_here)


# D-SEC-18: what "this computer's" means is read from its EFFECTIVE settings, not the raw file.
def _seed_without(name: str) -> None:
    """An older settings.yml that predates a packaged backend (config adds it at load)."""
    import yaml

    yml = paths.config_dir() / "settings.yml"
    tree = yaml.safe_load(yml.read_text(encoding="utf-8"))
    tree["llm"]["backends"].pop(name)
    yml.write_text(yaml.safe_dump(tree), encoding="utf-8")


def test_a_packaged_backend_missing_from_an_older_settings_yml_still_keeps_its_address(tmp_path):
    _fresh_db()
    paths.seed_config()
    _seed_without("anthropic")
    _put("llm.backends.anthropic.base_url", EVIL)
    archive = create_backup(tmp_path)
    _this_computer()
    _seed_without("anthropic")
    shipped = _effective().llm.backends["anthropic"]
    assert shipped.api_key_env  # this computer's saved key name, from the packaged backend
    result = restore_backup(archive, restore_local_settings=True)
    got = _effective().llm.backends["anthropic"]
    assert got.base_url == shipped.base_url != EVIL
    assert result.addresses_kept == 1


def test_a_backup_settings_yml_without_an_llm_section_keeps_this_computers(tmp_path):
    import yaml

    _fresh_db()
    paths.seed_config()
    yml = paths.config_dir() / "settings.yml"
    tree = yaml.safe_load(yml.read_text(encoding="utf-8"))
    tree.pop("llm")
    yml.write_text(yaml.safe_dump(tree), encoding="utf-8")
    _put("llm", {"backends": {"anthropic": {"base_url": EVIL}}})
    archive = create_backup(tmp_path)
    _this_computer()
    _seed_without("anthropic")
    restore_backup(archive, restore_local_settings=True)
    s = _effective()  # loads: no tier lost its required fields
    assert s.llm.backends["anthropic"].base_url != EVIL


@pytest.mark.parametrize("ticked", [False, True])
def test_confidential_mode_on_only_in_settings_yml_is_never_turned_off(tmp_path, ticked):
    import yaml

    _fresh_db()
    paths.seed_config()
    _put("confidential_mode", False)
    archive = create_backup(tmp_path)
    _this_computer()
    yml = paths.config_dir() / "settings.yml"
    tree = yaml.safe_load(yml.read_text(encoding="utf-8"))
    tree["confidential_mode"] = True
    yml.write_text(yaml.safe_dump(tree), encoding="utf-8")
    assert _effective().confidential_mode is True
    restore_backup(archive, restore_local_settings=ticked)
    assert _effective().confidential_mode is True


def test_a_backup_may_still_turn_confidential_mode_on(tmp_path):
    _fresh_db()
    paths.seed_config()
    _put("confidential_mode", True)
    archive = create_backup(tmp_path)
    _this_computer()
    restore_backup(archive)
    assert _effective().confidential_mode is True


@pytest.mark.parametrize("shape", ["leaf", "section"])
def test_a_backup_never_loosens_a_backends_privacy_controls(tmp_path, shape):
    _fresh_db()
    paths.seed_config()
    if shape == "leaf":
        _put("llm.backends.frontier.hosted.data_collection", "allow")
        _put("llm.backends.frontier.hosted.zdr", False)
    else:
        _put("llm.backends.frontier", {"hosted": {"data_collection": "allow", "zdr": False}})
    archive = create_backup(tmp_path)
    _this_computer()
    _put("llm.backends.frontier.hosted.zdr", True)
    restore_backup(archive, restore_local_settings=True)
    hosted = _effective().llm.backends["frontier"].hosted
    assert hosted.data_collection == "deny" and hosted.zdr is True


def test_settings_yml_cannot_loosen_a_backends_privacy_controls(tmp_path):
    import yaml

    _fresh_db()
    paths.seed_config()
    yml = paths.config_dir() / "settings.yml"
    tree = yaml.safe_load(yml.read_text(encoding="utf-8"))
    tree["llm"]["backends"]["frontier"]["hosted"]["data_collection"] = "allow"
    yml.write_text(yaml.safe_dump(tree), encoding="utf-8")
    archive = create_backup(tmp_path)
    _this_computer()
    restore_backup(archive, restore_local_settings=True)
    got = yaml.safe_load(yml.read_text(encoding="utf-8"))
    assert got["llm"]["backends"]["frontier"]["hosted"]["data_collection"] == "deny"


# D-SEC-19: fail closed, plain backend names, and packaged backends never left half written.
def _edit_yml(fn) -> None:
    import yaml

    yml = paths.config_dir() / "settings.yml"
    tree = yaml.safe_load(yml.read_text(encoding="utf-8"))
    fn(tree)
    yml.write_text(yaml.safe_dump(tree), encoding="utf-8")


def test_unloadable_settings_here_still_never_turn_confidential_mode_off(tmp_path):
    _fresh_db()
    paths.seed_config()
    _put("confidential_mode", False)
    archive = create_backup(tmp_path)
    _this_computer()
    _edit_yml(lambda t: t.__setitem__("confidential_mode", True))
    _put("thresholds.apply", "not-a-number")  # this computer's settings no longer load
    restore_backup(archive)
    assert _rows()["confidential_mode"] is True


def test_an_invalid_row_that_is_not_local_only_does_not_block_the_opt_in(tmp_path):
    _fresh_db()
    paths.seed_config()
    _put("llm.mode", "both")
    archive = create_backup(tmp_path)
    _this_computer()
    _put("ui.scheme", "not-a-scheme")
    result = restore_backup(archive, restore_local_settings=True)
    assert result.local_settings_restored and _rows()["llm.mode"] == "both"


def test_unloadable_settings_here_refuse_the_opt_in(tmp_path):
    _fresh_db()
    paths.seed_config()
    _put("llm.backends.anthropic.base_url", EVIL)
    _put("llm.mode", "both")
    archive = create_backup(tmp_path)
    _this_computer()
    _seed_without("anthropic")
    _put("mcp.port", "not-a-number")  # a local-only row: this computer's settings cannot load
    result = restore_backup(archive, restore_local_settings=True)
    rows = _rows()
    assert "llm.backends.anthropic.base_url" not in rows and rows["llm.mode"] == "local"
    assert result.local_settings_refused and not result.local_settings_restored
    assert "could not be read" in " ".join(result.summary_lines())


@pytest.mark.parametrize("ticked", [False, True])
def test_a_corrupt_settings_yml_here_does_not_stop_a_restore(tmp_path, ticked):
    _fresh_db()
    paths.seed_config()
    _put("ui.scheme", "dark")
    archive = create_backup(tmp_path)
    _this_computer()
    (paths.config_dir() / "settings.yml").write_text("llm: [unclosed\n", encoding="utf-8")
    result = restore_backup(archive, restore_local_settings=ticked)
    assert result.db_restored and _rows()["ui.scheme"] == "dark"
    assert _rows()["confidential_mode"] is True  # unknown here, so treated as on
    assert result.local_settings_refused is ticked


DOTTED = {"base_url": EVIL, "api_key_env": "OPENROUTER_API_KEY", "external": True,
          "hosted": {"zdr": False}}


@pytest.mark.parametrize("ticked", [False, True])
def test_a_backend_name_that_is_not_one_segment_is_dropped_from_settings_yml(tmp_path, ticked):
    _fresh_db()
    paths.seed_config()
    _edit_yml(lambda t: t["llm"]["backends"].__setitem__("a.b", dict(DOTTED)))
    archive = create_backup(tmp_path)
    _this_computer()
    result = restore_backup(archive, restore_local_settings=ticked)
    s = _effective()
    assert "a.b" not in s.llm.backends and "a" not in s.llm.backends
    assert not any(b.base_url == EVIL for b in s.llm.backends.values())
    assert result.backends_dropped == 1
    assert "left out" in " ".join(result.summary_lines())


@pytest.mark.parametrize("key,value", [
    ("llm.backends", {"a.b": DOTTED}),
    ("llm", {"backends": {"a.b": DOTTED, "x y": DOTTED}}),
])
def test_a_backend_name_that_is_not_one_segment_is_dropped_from_rows(tmp_path, key, value):
    _fresh_db()
    paths.seed_config()
    _put(key, value)
    archive = create_backup(tmp_path)
    _this_computer()
    result = restore_backup(archive, restore_local_settings=True)
    s = _effective()
    assert not any(b.base_url == EVIL for b in s.llm.backends.values())
    assert result.backends_dropped == len(_dict_of(value))


def _dict_of(value: dict) -> dict:
    return value.get("backends", value)


def test_the_preview_never_lists_a_backend_it_would_drop(tmp_path):
    from kestrel.services.backup_preview import preview_backup

    _fresh_db()
    paths.seed_config()
    _edit_yml(lambda t: t["llm"]["backends"].__setitem__("a.b", dict(DOTTED)))
    _put("llm.backends", {"c.d": DOTTED})
    archive = create_backup(tmp_path)
    _this_computer()
    pv = preview_backup(archive)
    assert not any("evil.example" in i.value for i in pv.servers)


def test_a_ticked_restore_onto_an_older_settings_yml_keeps_the_packaged_backend(tmp_path):
    _fresh_db()
    paths.seed_config()
    archive = create_backup(tmp_path)
    _this_computer()
    _seed_without("anthropic")
    before = _effective().llm.backends["anthropic"]
    restore_backup(archive, restore_local_settings=True)
    after = _effective().llm.backends["anthropic"]
    assert (after.base_url, after.api_key_env) == (before.base_url, before.api_key_env)
