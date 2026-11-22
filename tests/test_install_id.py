"""The install ID (D-INST-1): generated once from the OS random source, kept across a restart and
an upgrade, replaced only when damaged, never the ID salt, and never logged.

All values here are invented. Nothing reaches the network."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
import structlog

from kestrel.db.models import Setting
from kestrel.services import install_id, kestrel_ids

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _fresh_db() -> None:
    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.engine import reset_engine

    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations(paths.db_path())
    asyncio.run(reset_engine())


def _run(coro_fn):
    async def go():
        from kestrel.db.engine import get_sessionmaker

        async with get_sessionmaker()() as s:
            out = await coro_fn(s)
            await s.commit()
            return out

    return asyncio.run(go())


def _stored() -> dict | None:
    """The stored record, read with plain sqlite3 from the install ID's own table (D-INST-15)."""
    import sqlite3

    from kestrel import paths

    con = sqlite3.connect(str(paths.db_path()))
    try:
        row = con.execute("SELECT install_id, created_at, version FROM install_identity "
                          "WHERE slot = 1").fetchone()
    finally:
        con.close()
    return None if row is None else {"id": row[0], "created_at": row[1], "version": row[2]}


def test_first_read_writes_a_version_4_uuid_once():
    _fresh_db()
    assert _stored() is None
    first = _run(install_id.ensure)
    assert uuid.UUID(first["id"]).version == 4
    assert first["created_at"] and first["version"]
    again = _run(install_id.ensure)
    assert again == first
    assert _stored()["id"] == first["id"]


def test_two_first_reads_at_once_agree_on_one_value():
    _fresh_db()

    async def both():
        from kestrel.db.engine import get_sessionmaker

        async def one():
            async with get_sessionmaker()() as s:
                rec = await install_id.ensure(s)
                await s.commit()
                return rec["id"]

        return await asyncio.gather(one(), one())

    a, b = asyncio.run(both())
    assert a == b == _stored()["id"]


def test_the_id_survives_a_restart_and_an_upgrade():
    _fresh_db()
    first = _run(install_id.ensure)["id"]
    # A restart: a new engine on the same file. An upgrade: migrations run again to head.
    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.engine import reset_engine

    asyncio.run(reset_engine())
    migrate.run_migrations(paths.db_path())
    asyncio.run(reset_engine())
    assert _run(install_id.ensure)["id"] == first


def test_an_existing_install_without_one_gets_one_on_its_first_start():
    _fresh_db()
    from starlette.testclient import TestClient

    from kestrel.web.app import build_app

    assert _stored() is None
    with TestClient(build_app(worker=False)):
        pass
    assert install_id.is_valid(_stored()["id"])


def test_a_damaged_value_is_replaced_and_neither_value_is_logged():
    _fresh_db()
    damaged = {"id": "00000000-not-a-uuid", "created_at": "x"}
    _plant_value(damaged["id"])
    with structlog.testing.capture_logs() as logs:
        rec = _run(install_id.ensure)
    assert install_id.is_valid(rec["id"])
    assert _stored()["id"] == rec["id"]
    text = repr(logs)
    assert "install_id_replaced" in text
    assert rec["id"] not in text and damaged["id"] not in text


def test_creation_logs_no_value():
    _fresh_db()
    with structlog.testing.capture_logs() as logs:
        rec = _run(install_id.ensure)
    text = repr(logs)
    assert "install_id_created" in text
    full = rec["id"]
    for form in (full, install_id.short_form(full), full.replace("-", "")):
        assert form not in text and form.upper() not in text


def test_the_id_is_not_the_salt_and_makes_none():
    _fresh_db()
    rec = _run(install_id.ensure)

    async def salt(s):
        return await kestrel_ids.get_salt(s)

    # Making the install ID writes no salt, and the salt (made later) is a different value.
    assert _run(salt) is None

    async def make_salt(s):
        return await kestrel_ids.get_salt(s, create=True)

    made = _run(make_salt)
    assert made and made not in rec["id"].replace("-", "")
    assert _run(install_id.ensure)["id"] == rec["id"]


def test_short_form_shape_and_it_never_reads_as_an_application_id():
    full = "7f3a91c2-0b1d-4e2f-8a3b-123456789abc"
    assert install_id.short_form(full) == "K-7F3A-91C2"
    assert kestrel_ids.normalize(install_id.short_form(full)) == ""
    assert install_id.view({"id": full}) == {"short": "K-7F3A-91C2", "full": full}


@pytest.mark.parametrize("value", [
    None, "", 5, ["x"], "7F3A91C2-0B1D-4E2F-8A3B-123456789ABC",
    "7f3a91c2-0b1d-1e2f-8a3b-123456789abc",  # version 1
    "7f3a91c2-0b1d-4e2f-7a3b-123456789abc",  # wrong variant
    "7f3a91c2-0b1d-4e2f-8a3b-123456789abc ",
])
def test_only_a_lowercase_version_4_uuid_is_valid(value):
    assert not install_id.is_valid(value)
    assert install_id.record_from({"id": value}) is None


def test_the_row_is_state_never_configuration():
    from kestrel.services.setting_overrides import STATE_KEYS, is_config_key

    assert install_id.KEY in STATE_KEYS
    assert not is_config_key(install_id.KEY)


# ------------------------------------------------------------------------------ the pages
PAGES = ("/about", "/settings/section/data")


def _forms(full: str) -> tuple[str, ...]:
    return (full, install_id.short_form(full), full.replace("-", ""),
            full.replace("-", "")[:8].upper())


@pytest.mark.parametrize("path", PAGES)
def test_this_computer_sees_the_short_and_full_id_with_copy(path):
    _fresh_db()
    from starlette.testclient import TestClient

    from kestrel.web.app import build_app

    with TestClient(build_app(worker=False), base_url="http://127.0.0.1") as c:
        page = c.get(path).text
    rec = _stored()
    short = install_id.short_form(rec["id"])
    prefix = "ab" if path == "/about" else "st"
    assert f'data-testid="{prefix}-iid-short">{short}</code>' in page
    assert f'data-testid="{prefix}-iid-full">{rec["id"]}</code>' in page
    assert f'data-copy-target="{prefix}-iid-short"' in page
    assert f'data-copy-target="{prefix}-iid-full"' in page
    assert "Shown on this computer only." not in page


@pytest.mark.parametrize("path", PAGES)
def test_another_device_never_receives_the_id(path):
    _fresh_db()
    from starlette.testclient import TestClient

    from kestrel.web import auth
    from kestrel.web.app import build_app

    rec = _run(install_id.ensure)
    auth.set_cached_key("k204-key")
    try:
        with TestClient(build_app(worker=False), base_url="http://127.0.0.1",
                        raise_server_exceptions=False, client=("192.168.1.50", 1)) as c:
            r = c.get(path, headers={"X-Kestrel-Key": "k204-key"})
    finally:
        auth.reset_cache()
    assert r.status_code == 200
    assert "Shown on this computer only." in r.text
    for form in _forms(rec["id"]):
        assert form not in r.text and form.lower() not in r.text


def test_the_salt_is_never_on_a_page():
    _fresh_db()
    from starlette.testclient import TestClient

    from kestrel.web.app import build_app

    async def make_salt(s):
        return await kestrel_ids.get_salt(s, create=True)

    salt = _run(make_salt)
    with TestClient(build_app(worker=False), base_url="http://127.0.0.1") as c:
        for path in PAGES:
            assert salt not in c.get(path).text


# ------------------------------------------------------------------------- diagnostics
def _bundle_summary(c) -> tuple[dict, str]:
    import io
    import json
    import zipfile

    r = c.get("/about/diagnostics")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        return json.loads(z.read("summary.json")), z.read("README.txt").decode("utf-8")


def test_the_saved_diagnostics_file_carries_the_full_id():
    _fresh_db()
    from starlette.testclient import TestClient

    from kestrel.web.app import build_app

    with TestClient(build_app(worker=False), base_url="http://127.0.0.1") as c:
        summary, readme = _bundle_summary(c)
    assert summary["install_id"] == _stored()["id"]
    assert "install ID" in readme


def test_confidential_mode_keeps_the_id_in_diagnostics():
    """D-INST-3: confidential mode hides the employer, not this random ID; with no employer
    chosen the bundle runs conservative, and the ID is still there."""
    _fresh_db()
    from starlette.testclient import TestClient

    from kestrel.web.app import build_app

    async def conf_on(s):
        s.add(Setting(key="confidential_mode", value=True))

    _run(conf_on)
    with TestClient(build_app(worker=False), base_url="http://127.0.0.1") as c:
        summary, _ = _bundle_summary(c)
    assert summary["confidential_mode"] is True
    assert summary["install_id"] == _stored()["id"]


def test_the_bundle_never_writes_an_id():
    _fresh_db()
    from kestrel.config import get_settings
    from kestrel.services import diagnostics

    out = diagnostics.summary(get_settings(), lambda s: s)
    assert out["install_id"] is None
    assert _stored() is None


# ------------------------------------------------------------------- hostile stored values
def _plant_raw(text: str) -> None:
    """Write the ID column's stored text as is (a hand-edited database can)."""
    _plant_value(text)


@pytest.mark.parametrize("raw", ["not json", "{", '"7f3a91c2"', "null", "[1,2]"])
def test_a_stored_value_that_is_not_a_record_is_replaced_and_the_app_starts(raw):
    _fresh_db()
    _plant_raw(raw)
    from starlette.testclient import TestClient

    from kestrel.web.app import build_app

    with TestClient(build_app(worker=False), base_url="http://127.0.0.1") as c:
        assert c.get("/about").status_code == 200
        assert c.get("/settings/section/data").status_code == 200
    assert install_id.is_valid(_stored()["id"])


def test_a_lost_insert_race_returns_the_stored_value(monkeypatch):
    """Two first reads: the second's read misses, its insert is ignored, and it returns the row
    the first one wrote, not its own."""
    _fresh_db()
    first = _run(install_id.ensure)["id"]
    real = install_id._read_raw
    calls = {"n": 0}

    async def miss_once(session):
        calls["n"] += 1
        return install_id._Stored(False) if calls["n"] == 1 else await real(session)

    monkeypatch.setattr(install_id, "_read_raw", miss_once)
    assert _run(install_id.ensure)["id"] == first
    assert _stored()["id"] == first


def test_a_lost_repair_race_returns_the_stored_value(monkeypatch):
    """Two readers find the same damaged value: only one replacement is kept and both return
    it."""
    _fresh_db()
    _plant_raw('"damaged"')
    winner = "1b2c3d4e-0000-4000-8000-000000000001"
    real = install_id._read_raw
    calls = {"n": 0}

    async def other_repairs_first(session):
        calls["n"] += 1
        raw = await real(session)
        if calls["n"] == 1:  # another reader replaces it between this read and the write
            _plant_raw(winner)
        return raw

    monkeypatch.setattr(install_id, "_read_raw", other_repairs_first)
    assert _run(install_id.ensure)["id"] == winner
    assert _stored()["id"] == winner


def _plant_value(value) -> None:
    """Write the ID column with any SQLite type but NULL (integer, real, BLOB, text), as a
    hand-edited database can. NULL is refused by the schema (test_the_schema_refuses_a_null_id)."""
    import sqlite3

    from kestrel import paths

    con = sqlite3.connect(str(paths.db_path()))
    try:
        con.execute("INSERT OR REPLACE INTO install_identity "
                    "(slot, install_id, created_at, version, updated_at) "
                    "VALUES (1, ?, NULL, NULL, '2026-01-01T00:00:00')", (value,))
        con.commit()
    finally:
        con.close()


def test_the_schema_refuses_a_null_id_and_a_second_row():
    import sqlite3

    from kestrel import paths

    _fresh_db()
    con = sqlite3.connect(str(paths.db_path()))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO install_identity (slot, install_id, updated_at) "
                        "VALUES (1, NULL, 'x')")
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO install_identity (slot, install_id, updated_at) "
                        "VALUES (2, ?, 'x')", (HERE_ID,))
    finally:
        con.close()


VALID_TEXT = "7f3a91c2-0b1d-4e2f-8a3b-123456789abc"


@pytest.mark.parametrize("value", [
    5, 1.5, 7, "5", "1e999", "[" * 100000, "x" * 5000, VALID_TEXT.upper(),
    VALID_TEXT.encode(),  # a valid ID, but stored as a BLOB: not our schema
    b"\xff\xfe\x00",
], ids=["int", "real", "int7", "text5", "huge-real", "deep", "long", "uppercase", "blob-valid",
        "blob-bad"])
def test_any_stored_type_but_a_short_text_record_is_replaced_and_the_app_starts(value):
    _fresh_db()
    _plant_value(value)
    from starlette.testclient import TestClient

    from kestrel.web.app import build_app

    with TestClient(build_app(worker=False), base_url="http://127.0.0.1") as c:
        assert c.get("/about").status_code == 200
        assert c.get("/settings/section/data").status_code == 200
    rec = _stored()
    assert install_id.is_valid(rec["id"]) and rec["id"] != "7f3a91c2-0b1d-4e2f-8a3b-123456789abc"
    assert _run(install_id.ensure)["id"] == rec["id"]  # stable once repaired


@pytest.mark.parametrize("value", [5, "x", "[" * 100000, VALID_TEXT.encode()],
                         ids=["int", "short-text", "deep", "blob-valid"])
def test_the_bundle_reads_a_damaged_value_as_none(value):
    _fresh_db()
    _plant_value(value)
    from kestrel.config import get_settings
    from kestrel.services import diagnostics

    assert diagnostics.summary(get_settings(), lambda s: s)["install_id"] is None


@pytest.mark.parametrize("sql", ["CAST(X'FFFE' AS TEXT)", "CAST(X'7B22' || X'FF' AS TEXT)"],
                         ids=["bad-utf8", "record-with-stray-byte"])
def test_text_that_is_not_utf8_is_replaced_and_stays_replaced(sql):
    import sqlite3

    from kestrel import paths

    _fresh_db()
    con = sqlite3.connect(str(paths.db_path()))
    try:
        con.execute("INSERT OR REPLACE INTO install_identity "  # noqa: S608 - test: fixed text
                    f"(slot, install_id, updated_at) VALUES (1, {sql}, 'x')")
        con.commit()
    finally:
        con.close()
    first = _run(install_id.ensure)
    assert install_id.is_valid(first["id"])
    assert _run(install_id.ensure)["id"] == first["id"]
    from kestrel.config import get_settings
    from kestrel.services import diagnostics

    assert diagnostics.summary(get_settings(), lambda s: s)["install_id"] == first["id"]


# ------------------------------------------------------------------------------- restore
SOURCE_ID = "a1a1a1a1-0000-4000-8000-00000000000a"  # the install the backup came from
HERE_ID = "b2b2b2b2-0000-4000-8000-00000000000b"  # this computer


def _set_record(full: str | None) -> None:
    import sqlite3

    from kestrel import paths

    con = sqlite3.connect(str(paths.db_path()))
    try:
        con.execute("DELETE FROM install_identity")
        if full is not None:
            con.execute("INSERT INTO install_identity (slot, install_id, updated_at) "
                        "VALUES (1, ?, '2026-01-01')", (full,))
        con.commit()
    finally:
        con.close()


def _restore(tmp_path, *, source, here):
    """A backup made while the ID was `source`, restored while it is `here`."""
    from kestrel.db.engine import forget_engine
    from kestrel.services.backup import create_backup, restore_backup

    _fresh_db()
    _set_record(source)
    archive = create_backup(tmp_path)
    _set_record(here)
    with structlog.testing.capture_logs() as logs:
        result = restore_backup(archive)
    forget_engine()
    assert result.db_restored is True
    return logs


def _loud(logs) -> str:
    return repr([e for e in logs if e.get("log_level") != "debug"])


def test_a_restore_from_another_install_keeps_this_computers_id(tmp_path):
    logs = _restore(tmp_path, source=SOURCE_ID, here=HERE_ID)
    assert _stored()["id"] == HERE_ID
    # That the IDs differed is noted at debug, with neither value in any line, debug included:
    # the log goes into the diagnostics bundle (round 6 merge review, D-INST-4).
    debug = [e for e in logs if e.get("event") == "install_id_kept_across_restore"]
    assert debug and debug[0]["log_level"] == "debug" and debug[0]["differed"] is True
    every = repr(logs)
    for full in (SOURCE_ID, HERE_ID):
        for form in (full, install_id.short_form(full), full.replace("-", "")):
            assert form not in every


def test_a_restore_onto_a_computer_with_no_id_makes_a_new_one(tmp_path):
    _restore(tmp_path, source=SOURCE_ID, here=None)
    rec = _stored()
    assert install_id.is_valid(rec["id"]) and rec["id"] != SOURCE_ID


def test_a_restore_of_its_own_backup_keeps_the_id(tmp_path):
    _restore(tmp_path, source=HERE_ID, here=HERE_ID)
    assert _stored()["id"] == HERE_ID


@pytest.mark.parametrize("hostile", [5, "x", "x" * 5000, b"\xff", '{"id": "nope"}'],
                         ids=["int", "short", "long", "blob", "bad-record"])
def test_a_hostile_id_in_the_backup_is_never_adopted(tmp_path, hostile):
    from kestrel.db.engine import forget_engine
    from kestrel.services.backup import create_backup, restore_backup

    _fresh_db()
    _plant_value(hostile)
    archive = create_backup(tmp_path)
    _set_record(HERE_ID)
    restore_backup(archive)
    forget_engine()
    assert _stored()["id"] == HERE_ID


def test_a_restore_drops_lookalike_keys_carrying_the_backups_id(tmp_path):
    import sqlite3

    from kestrel import paths
    from kestrel.db.engine import forget_engine
    from kestrel.services.backup import create_backup, restore_backup

    _fresh_db()
    _set_record(SOURCE_ID)
    con = sqlite3.connect(str(paths.db_path()))
    value = json.dumps({"id": SOURCE_ID, "created_at": None, "version": None})
    for key in ("Install.id", "install.id ", "INSTALL.ID", " install.id"):
        con.execute("INSERT INTO setting (key, value, updated_at) VALUES (?, ?, 'x')", (key, value))
    con.commit()
    con.close()
    archive = create_backup(tmp_path)
    _set_record(HERE_ID)
    restore_backup(archive)
    forget_engine()
    con = sqlite3.connect(str(paths.db_path()))
    try:
        rows = con.execute("SELECT key, value FROM setting").fetchall()
    finally:
        con.close()
    # D-INST-15: the ID lives in its own table now, so no setting row of that name is left.
    assert [k for k, _ in rows if k.strip().lower() == "install.id"] == []
    assert not any(SOURCE_ID in str(v) for _, v in rows)
    assert _stored()["id"] == HERE_ID


def test_an_unreadable_live_database_stops_the_keep_instead_of_minting_a_new_id(tmp_path):
    import sqlite3

    staged = tmp_path / "staged.db"
    con = sqlite3.connect(str(staged))
    con.execute("CREATE TABLE setting (key TEXT PRIMARY KEY, value JSON, updated_at TEXT)")
    con.commit()
    con.close()
    live = tmp_path / "live.db"
    live.write_bytes(b"this is not a database, it is invented bytes" * 100)
    with pytest.raises(sqlite3.DatabaseError):
        install_id.keep_across_restore(staged, live)


@pytest.mark.parametrize("key", [
    "install.id\t", "\ninstall.id", "install .id", "install.​id", "ｉnstall.id",
    "INSTALL.ID　", "install.i⁠d",
], ids=["tab", "newline", "nbsp", "zero-width-space", "fullwidth-i", "ideographic-space",
        "word-joiner"])
def test_a_restore_drops_unicode_lookalike_keys(tmp_path, key):
    import sqlite3

    from kestrel import paths
    from kestrel.db.engine import forget_engine
    from kestrel.services.backup import create_backup, restore_backup

    _fresh_db()
    _set_record(SOURCE_ID)
    con = sqlite3.connect(str(paths.db_path()))
    value = json.dumps({"id": SOURCE_ID, "created_at": None, "version": None})
    con.execute("INSERT INTO setting (key, value, updated_at) VALUES (?, ?, 'x')", (key, value))
    con.commit()
    con.close()
    archive = create_backup(tmp_path)
    _set_record(HERE_ID)
    restore_backup(archive)
    forget_engine()
    con = sqlite3.connect(str(paths.db_path()))
    try:
        rows = con.execute("SELECT key, value FROM setting").fetchall()
    finally:
        con.close()
    assert key not in [k for k, _ in rows]
    assert not any(SOURCE_ID in str(v) for _, v in rows)
    assert _stored()["id"] == HERE_ID


def test_canonical_key_folds_lookalikes_and_nothing_else():
    assert install_id.canonical_key("  INSTALL .​ID\t") == install_id.KEY
    assert install_id.canonical_key("install.ids") != install_id.KEY
    assert install_id.canonical_key("ui.theme") == "ui.theme"


@pytest.mark.parametrize(("key", "value", "left"), [
    ("install", {"id": SOURCE_ID}, None),
    ("Install", {"ID": SOURCE_ID, "note": "x"}, {"note": "x"}),
    ("", {"install": {"id": SOURCE_ID}, "ui": {"theme": "dark"}}, {"ui": {"theme": "dark"}}),
    ("İnstall.id", {"id": SOURCE_ID, "created_at": None, "version": None}, None),
], ids=["section", "section-lookalike", "root-section", "dotted-capital-i"])
def test_a_restore_strips_install_leaves_and_keeps_the_rest(tmp_path, key, value, left):
    """D-INST-17: install-shaped leaves go one by one; the other leaves of the row stay."""
    import sqlite3

    from kestrel import paths
    from kestrel.db.engine import forget_engine
    from kestrel.services.backup import create_backup, restore_backup

    _fresh_db()
    _set_record(SOURCE_ID)
    con = sqlite3.connect(str(paths.db_path()))
    con.execute("INSERT INTO setting (key, value, updated_at) VALUES (?, ?, 'x')",
                (key, json.dumps(value)))
    con.commit()
    con.close()
    archive = create_backup(tmp_path)
    _set_record(HERE_ID)
    restore_backup(archive)
    forget_engine()
    con = sqlite3.connect(str(paths.db_path()))
    try:
        rows = con.execute("SELECT key, value FROM setting").fetchall()
    finally:
        con.close()
    kept = {k: json.loads(v) for k, v in rows}
    assert kept.get(key, "absent") == (left or "absent")
    assert not any(SOURCE_ID in str(v) for _, v in rows)
    assert _stored()["id"] == HERE_ID


@pytest.mark.parametrize("key", ["İnstall.id", "ínstall.id", "install.íd"])
def test_canonical_key_strips_combining_marks(key):
    assert install_id.canonical_key(key) == install_id.KEY


def _restore_with_row(tmp_path, key: str, raw: str | bytes):
    """A backup holding this computer-foreign ID plus one extra row, restored here."""
    import sqlite3

    from kestrel import paths
    from kestrel.db.engine import forget_engine
    from kestrel.services.backup import create_backup, restore_backup

    _fresh_db()
    _set_record(SOURCE_ID)
    con = sqlite3.connect(str(paths.db_path()))
    con.execute("INSERT INTO setting (key, value, updated_at) VALUES (?, ?, 'x')", (key, raw))
    con.commit()
    con.close()
    archive = create_backup(tmp_path)
    _set_record(HERE_ID)
    with structlog.testing.capture_logs() as logs:
        result = restore_backup(archive)
    forget_engine()
    con = sqlite3.connect(str(paths.db_path()))
    try:
        rows = con.execute("SELECT key, CAST(value AS BLOB) FROM setting").fetchall()
    finally:
        con.close()
    return result, rows, logs


_HOLDS = '{"install": {"id": "' + SOURCE_ID + '"}'


@pytest.mark.parametrize("raw", [
    _HOLDS + ', "pad": "' + "x" * (install_id.MAX_SECTION + 1) + '"}',
    _HOLDS + ', "deep": ' + "[" * 200_000 + "]" * 200_000 + "}",
    (_HOLDS + "}").encode("utf-8") + b"\xff",
], ids=["padded-past-the-cap", "nested-too-deep", "not-utf8"])
@pytest.mark.parametrize("key", ["", "install", "INSTALL"], ids=["root", "section", "lookalike"])
def test_a_section_row_that_cannot_be_checked_is_dropped_and_counted(tmp_path, key, raw):
    """D-INST-12: uncertain never adopts. Each of these rows holds the backup's ID behind a hatch
    the check cannot see past; each is dropped, and the restore says how many."""
    result, rows, logs = _restore_with_row(tmp_path, key, raw)
    assert key not in [k for k, _ in rows]
    assert not any(SOURCE_ID.encode() in (v or b"") for _, v in rows)
    assert _stored()["id"] == HERE_ID
    assert result.settings_unchecked_dropped >= 1
    assert any(e.get("event") == "restore_install_values_removed" for e in logs), logs


def test_a_section_row_that_is_checked_and_clean_is_kept(tmp_path):
    result, rows, _logs = _restore_with_row(tmp_path, "", '{"ui": {"theme": "dark"}}')
    root = [json.loads(v) for k, v in rows if k == ""]
    assert root and root[0].get("ui", {}).get("theme") == "dark"
    assert result.settings_unchecked_dropped == 0


_DEEP = "[" * 200_000 + "]" * 200_000


def test_a_restore_and_its_preview_survive_values_nested_too_deep_to_parse(tmp_path):
    """D-INST-12 siblings: the settings preview and the follow-up draft import read the backup
    with json.loads too; a value nested past the parser's limit is skipped, never a crash."""
    import sqlite3

    from kestrel import paths
    from kestrel.db.engine import forget_engine
    from kestrel.db.models import Task
    from kestrel.services import backup
    from kestrel.services.backup_preview import preview_backup

    _fresh_db()
    _set_record(SOURCE_ID)

    async def plant(s):
        s.add(Task(type=backup.FOLLOW_UP_TYPE, payload={}, status="done"))

    _run(plant)
    con = sqlite3.connect(str(paths.db_path()))
    con.execute("UPDATE task SET payload = ? WHERE type = ?", (_DEEP, backup.FOLLOW_UP_TYPE))
    con.execute("INSERT INTO setting (key, value, updated_at) VALUES ('llm', ?, 'x')", (_DEEP,))
    con.commit()
    con.close()
    archive = backup.create_backup(tmp_path)
    _set_record(HERE_ID)
    preview_backup(archive)
    result = backup.restore_backup(archive)
    forget_engine()
    assert result.db_restored is True
    assert _stored()["id"] == HERE_ID


# ------------------------------------------------------------------- migration 0041 (D-INST-15)
def _migrate(db, direction: str, rev: str) -> None:
    from alembic import command

    from kestrel import paths
    from kestrel.db import migrate

    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        cfg = migrate._config(str(mig_dir), db)
        (command.upgrade if direction == "up" else command.downgrade)(cfg, rev)


@pytest.mark.parametrize(("stored", "moved"), [
    (json.dumps({"id": SOURCE_ID, "created_at": "2026-01-02T03:04:05+00:00", "version": "0.7.6"}),
     [(SOURCE_ID, "2026-01-02T03:04:05+00:00", "0.7.6")]),
    (json.dumps({"id": "not-a-uuid"}), []),
    ("[" * 100_000, []),
    (b"\xff", []),
], ids=["valid", "bad-id", "deep", "blob"])
def test_migration_0041_moves_a_valid_record_and_deletes_the_setting_row(tmp_path, stored, moved):
    import sqlite3

    db = tmp_path / "k.db"
    _migrate(db, "up", "0034")
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO setting (key, value, updated_at) VALUES (?, ?, 'u')",
                (install_id.KEY, stored))
    con.commit()
    con.close()
    _migrate(db, "up", "0041")
    con = sqlite3.connect(str(db))
    try:
        assert con.execute("SELECT 1 FROM setting WHERE key = ?", (install_id.KEY,)).fetchone() \
            is None
        rows = con.execute("SELECT install_id, created_at, version FROM install_identity"
                           ).fetchall()
    finally:
        con.close()
    assert rows == moved


def test_migration_0041_downgrade_puts_the_record_back(tmp_path):
    import sqlite3

    db = tmp_path / "k.db"
    _migrate(db, "up", "0041")
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO install_identity (slot, install_id, created_at, version, updated_at)"
                " VALUES (1, ?, NULL, '0.7.7', 'u')", (HERE_ID,))
    con.commit()
    con.close()
    _migrate(db, "down", "0034")
    con = sqlite3.connect(str(db))
    try:
        value = con.execute("SELECT value FROM setting WHERE key = ?", (install_id.KEY,)
                            ).fetchone()[0]
        gone = con.execute("SELECT 1 FROM sqlite_master WHERE name = 'install_identity'"
                           ).fetchone()
    finally:
        con.close()
    assert json.loads(value) == {"id": HERE_ID, "created_at": None, "version": "0.7.7"}
    assert gone is None


def test_the_backup_carries_the_identity_table(tmp_path):
    """D-INST-2: the export is a copy of the database the person keeps, so it holds the ID."""
    import sqlite3
    import zipfile

    from kestrel.services.backup import DB_NAME, create_backup

    _fresh_db()
    _set_record(HERE_ID)
    archive = create_backup(tmp_path)
    out = tmp_path / "out"
    with zipfile.ZipFile(archive) as z:
        name = next(n for n in z.namelist() if n.endswith(DB_NAME))
        z.extract(name, out)
    con = sqlite3.connect(str(out / name))
    try:
        assert con.execute("SELECT install_id FROM install_identity").fetchall() == [(HERE_ID,)]
    finally:
        con.close()


def test_a_restore_rebuilds_the_identity_table_from_our_schema(tmp_path):
    """D-INST-17, D-SEC-20: a backup whose identity table has another shape (no one-row check,
    two rows, an extra column, a trigger) is never imported; this computer's record is written
    into a table built from Kestrel's own schema."""
    import sqlite3

    from kestrel import paths
    from kestrel.db.engine import forget_engine
    from kestrel.services.backup import create_backup, restore_backup

    _fresh_db()
    con = sqlite3.connect(str(paths.db_path()))
    con.executescript(f"""
        DROP TABLE install_identity;
        CREATE TABLE install_identity (slot INTEGER, install_id TEXT, created_at TEXT,
            version TEXT, updated_at TEXT, extra TEXT);
        INSERT INTO install_identity VALUES (1, '{SOURCE_ID}', NULL, NULL, 'u', 'x');
        INSERT INTO install_identity VALUES (2, '{SOURCE_ID}', NULL, NULL, 'u', 'y');
        CREATE TRIGGER t AFTER INSERT ON install_identity BEGIN
            INSERT INTO setting (key, value, updated_at) VALUES ('ui.theme', '"x"', 'u');
        END;
    """)
    con.commit()
    con.close()
    archive = create_backup(tmp_path)
    _fresh_db()
    _set_record(HERE_ID)
    restore_backup(archive)
    forget_engine()
    con = sqlite3.connect(str(paths.db_path()))
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(install_identity)")]
        rows = con.execute("SELECT slot, install_id FROM install_identity").fetchall()
        triggers = con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' "
                               "AND tbl_name = 'install_identity'").fetchall()
    finally:
        con.close()
    assert cols == ["slot", "install_id", "created_at", "version", "updated_at"]
    assert rows == [(1, HERE_ID)] and triggers == []
