"""Migration 0023: first and last name, hidden companies, the connections-import tables.

Up and down on a database that already holds data, so the name split is tested on real rows and
the downgrade is proven to leave the earlier schema (and the kept full_name) intact."""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command

from kestrel import paths
from kestrel.db import migrate

REV = "0023"
PREV = "0022"


def _cols(db, table: str) -> set[str]:
    con = sqlite3.connect(db)
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def _tables(db) -> set[str]:
    con = sqlite3.connect(db)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def _go(db, direction: str, rev: str) -> None:
    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        cfg = migrate._config(str(mig_dir), db)
        (command.upgrade if direction == "up" else command.downgrade)(cfg, rev)


def test_the_head_is_this_migration_or_a_later_one():
    # Later migrations stack on this one (0024 adds engagement_offer.deadline); revision ids are
    # zero padded, so string order is revision order.
    assert migrate.head_revision() >= REV


def test_upgrade_splits_existing_names_on_the_first_space_and_keeps_the_original(tmp_path):
    db = tmp_path / "m.db"
    _go(db, "up", PREV)
    con = sqlite3.connect(db)
    for i, name in enumerate(
        ["Jordan A. Rivera", "Cher", "  Maria   de la Cruz ", None, ""], start=1
    ):
        con.execute(
            "INSERT INTO profile (id, track, is_active, full_name, requires_sponsorship,"
            " updated_at) VALUES (?, 'employment', 1, ?, 0, 'x')",
            (i, name),
        )
    con.commit()
    con.close()

    _go(db, "up", "head")
    assert migrate.current_revision(db) == migrate.head_revision()
    con = sqlite3.connect(db)
    try:
        rows = {
            r[0]: r[1:]
            for r in con.execute("SELECT id, first_name, last_name, full_name FROM profile")
        }
    finally:
        con.close()
    assert rows[1] == ("Jordan", "A. Rivera", "Jordan A. Rivera")
    assert rows[2] == ("Cher", None, "Cher")  # one word: no last name
    assert rows[3] == ("Maria", "de la Cruz", "  Maria   de la Cruz ")  # original text kept
    assert rows[4] == (None, None, None)
    assert rows[5] == (None, None, "")


def test_upgrade_adds_the_new_schema(tmp_path):
    db = tmp_path / "m.db"
    migrate.run_migrations(db)
    assert {"first_name", "last_name"} <= _cols(db, "profile")
    assert "hidden_at" in _cols(db, "org")
    assert {"contact_import", "connection_position", "org_summary"} <= _tables(db)
    assert {"org_id", "sentences", "tags", "sources", "model", "prompt_version", "generated_at",
            "insufficient_sources", "source_count", "status"} == _cols(db, "org_summary") - {"id"}
    assert {"contact_id", "company_name", "org_id", "match", "title", "connected_on",
            "first_import_id", "last_import_id", "is_current"} <= _cols(db, "connection_position")
    assert {"imported_at", "file_name", "row_count", "n_new", "n_changed", "n_missing",
            "kept_missing"} <= _cols(db, "contact_import")
    # No email column anywhere in the import tables (D-UI-131).
    for table in ("contact_import", "connection_position"):
        assert not [c for c in _cols(db, table) if "mail" in c]


def test_downgrade_removes_it_and_keeps_full_name_then_upgrades_again(tmp_path):
    db = tmp_path / "m.db"
    _go(db, "up", PREV)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO profile (id, track, is_active, full_name, requires_sponsorship, updated_at)"
        " VALUES (1, 'employment', 1, 'Pat Example', 0, 'x')"
    )
    con.commit()
    con.close()
    _go(db, "up", "head")
    con = sqlite3.connect(db)
    con.execute("INSERT INTO org (id, name, normalized_name, hidden_at, first_seen_at,"
                " last_seen_at, signals) VALUES (1, 'Acme', 'acme', 't', 't', 't', '[]')")
    con.commit()
    con.close()

    _go(db, "down", PREV)
    assert migrate.current_revision(db) == PREV
    assert not ({"first_name", "last_name"} & _cols(db, "profile"))
    assert "hidden_at" not in _cols(db, "org")
    assert not ({"contact_import", "connection_position", "org_summary"} & _tables(db))
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT full_name FROM profile").fetchall() == [("Pat Example",)]
        assert con.execute("SELECT name FROM org").fetchall() == [("Acme",)]
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        con.close()

    _go(db, "up", "head")
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT first_name, last_name FROM profile").fetchall() == [
            ("Pat", "Example")
        ]
    finally:
        con.close()


def test_downgrade_with_import_rows_present(tmp_path):
    db = tmp_path / "m.db"
    migrate.run_migrations(db)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO contact_import (id, imported_at) VALUES (1, 't')")
    con.execute("INSERT INTO contact (id, name, relationship, created_at) "
                "VALUES (1, 'Pell Ashgrove', 'known', 't')")
    con.execute("INSERT INTO connection_position (contact_id, match, is_current, "
                "first_import_id, last_import_id) VALUES (1, 'none', 1, 1, 1)")
    con.commit()
    con.close()
    _go(db, "down", PREV)
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT name FROM contact").fetchall() == [("Pell Ashgrove",)]
    finally:
        con.close()


def test_org_summary_is_one_row_per_org_and_downgrades_with_rows(tmp_path):
    db = tmp_path / "m.db"
    migrate.run_migrations(db)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO org (id, name, normalized_name, first_seen_at, last_seen_at, signals)"
                " VALUES (1, 'Acme', 'acme', 't', 't', '[]')")
    con.execute("INSERT INTO org_summary (org_id, sentences, tags, sources, generated_at)"
                " VALUES (1, '[]', '[]', '[]', 't')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO org_summary (org_id, sentences, tags, sources, generated_at)"
                    " VALUES (1, '[]', '[]', '[]', 't')")
    con.commit()
    assert con.execute("SELECT status, source_count, insufficient_sources FROM org_summary"
                       ).fetchall() == [("ok", 0, 0)]
    con.close()
    _go(db, "down", PREV)
    assert "org_summary" not in _tables(db)
    _go(db, "up", "head")
    assert "org_summary" in _tables(db)


@pytest.mark.parametrize("name", ["contact_import", "connection_position", "org_summary"])
def test_models_match_the_migrated_tables(tmp_path, name):
    """The ORM declares exactly the columns the migration creates."""
    from kestrel.db.models import Base

    db = tmp_path / "m.db"
    migrate.run_migrations(db)
    assert {c.name for c in Base.metadata.tables[name].columns} == _cols(db, name)


def _fk_on(db):
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _seed_children(db) -> None:
    con = _fk_on(db)
    con.execute("INSERT INTO org (id, name, normalized_name, first_seen_at, last_seen_at, signals)"
                " VALUES (1, 'Acme', 'acme', 't', 't', '[]')")
    con.execute("INSERT INTO opportunity (id, track, opp_type, org_id, title, url, stage,"
                " first_seen_at, last_seen_at, seen_count, ghost_score, version, is_canonical,"
                " liveness, liveness_strikes, remote_type, comp_source, pre_score,"
                " description_tokens) VALUES (1, 'employment', 'job_posting', 1, 'Role',"
                " 'u', 'new', 't', 't', 1, 0, 1, 1, 'unchecked', 0, 'unknown', 'none', 0, 0)")
    con.execute("INSERT INTO contact (id, org_id, name, relationship, created_at)"
                " VALUES (1, 1, 'Zed Rowe', 'known', 't')")
    con.execute("INSERT INTO interaction (contact_id, kind, occurred_at) VALUES (1, 'call', 't')")
    con.commit()
    con.close()


def test_the_upgrade_and_downgrade_work_with_rows_that_reference_org(tmp_path):
    """Regression: the downgrade copied `org` (batch mode) and failed on DROP TABLE org with
    foreign keys on, whenever an opportunity pointed at an organization."""
    db = tmp_path / "m.db"
    _go(db, "up", PREV)
    _seed_children(db)
    _go(db, "up", "head")  # the upgrade must not copy the table either
    con = _fk_on(db)
    con.execute("UPDATE org SET hidden_at = 't' WHERE id = 1")
    con.commit()
    con.close()
    _go(db, "down", PREV)
    assert "hidden_at" not in _cols(db, "org")
    con = _fk_on(db)
    try:
        assert con.execute("SELECT title FROM opportunity").fetchall() == [("Role",)]
        assert con.execute("SELECT name FROM org").fetchall() == [("Acme",)]
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        con.close()
    _go(db, "up", "head")
    assert "hidden_at" in _cols(db, "org")


def test_contacts_from_the_older_importer_are_adopted_and_their_email_erased(tmp_path):
    db = tmp_path / "m.db"
    _go(db, "up", PREV)
    con = _fk_on(db)
    con.execute("INSERT INTO org (id, name, normalized_name, first_seen_at, last_seen_at, signals)"
                " VALUES (1, 'Acme Corp', 'acme', 't', 't', '[]')")
    con.execute("INSERT INTO contact (id, org_id, name, title, email, source, relationship,"
                " created_at) VALUES (1, 1, 'Zed Rowe', 'Lead', 'zed@mail.invalid',"
                " 'linkedin_csv', 'known', 't')")
    con.execute("INSERT INTO contact (id, org_id, name, source, relationship, created_at)"
                " VALUES (2, NULL, 'Pat Public', 'research', 'none', 't')")
    con.execute("INSERT INTO contact (id, name, email, source, relationship, created_at)"
                " VALUES (3, 'Ro Hand', 'ro@mail.invalid', 'manual', 'none', 't')")
    con.commit()
    con.close()
    _go(db, "up", "head")
    con = _fk_on(db)
    try:
        rows = {r[0]: r[1:] for r in con.execute("SELECT id, source, email FROM contact")}
        assert rows[1] == ("linkedin_import", None)  # adopted; the stored email is gone
        assert rows[2] == ("research", None)
        assert rows[3] == ("manual", "ro@mail.invalid")  # not ours: left alone
        pos = con.execute("SELECT contact_id, company_name, org_id, match, title, is_current"
                          " FROM connection_position").fetchall()
        assert pos == [(1, "Acme Corp", 1, "sure", "Lead", 1)]
        assert "zed@mail.invalid" not in "\n".join(con.iterdump())
    finally:
        con.close()


async def test_adopted_contacts_are_diffed_matched_and_removable(tmp_path):
    """After the migration, the first new import pairs with the adopted contact (no duplicate),
    warm paths find it, and Remove all contacts removes it."""
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel.db.engine import make_async_engine
    from kestrel.db.models import Contact
    from kestrel.research import connections as cx

    db = tmp_path / "m.db"
    _go(db, "up", PREV)
    con = _fk_on(db)
    con.execute("INSERT INTO org (id, name, normalized_name, first_seen_at, last_seen_at, signals)"
                " VALUES (1, 'Acme Corp', 'acme', 't', 't', '[]')")
    con.execute("INSERT INTO contact (id, org_id, name, title, email, source, relationship,"
                " created_at) VALUES (1, 1, 'Zed Rowe', 'Lead', 'zed@mail.invalid',"
                " 'linkedin_csv', 'known', 't')")
    con.execute("INSERT INTO connection (contact_id, how_known, strength) VALUES (1, 'LinkedIn',"
                " 'known')")
    con.commit()
    con.close()
    _go(db, "up", "head")
    engine = make_async_engine(db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sm() as s:
            paths_before = await cx.connection_paths(s, 1)
            assert [p.name for p in paths_before] == ["Zed Rowe"]
            file = cx.parse_connections_csv(
                "First Name,Last Name,Company,Position,Connected On\n"
                "Zed,Rowe,Acme Corp,Lead,03 Mar 2023\n"
            )
            prev = await cx.preview_import(s, file)
            assert prev.new == [] and prev.missing == [] and prev.n_unchanged == 1
            batch = await cx.apply_import(s, file, keep_missing=False)
            assert (batch.n_new, batch.n_missing) == (0, 0)
            assert (await s.execute(select(func.count()).select_from(Contact))).scalar() == 1
            assert await cx.imported_contact_count(s) == 1
            result = await cx.remove_all_imported(s)
            assert result.removed == 1
            assert (await s.execute(select(func.count()).select_from(Contact))).scalar() == 0
    finally:
        await engine.dispose()


async def test_down_then_up_re_adopts_import_contacts_so_the_next_import_does_not_duplicate(
    tmp_path,
):
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel.db.engine import make_async_engine
    from kestrel.db.models import Contact
    from kestrel.research import connections as cx

    csv = (
        "First Name,Last Name,Company,Position,Connected On\n"
        "Zed,Rowe,Acme Corp,Lead,03 Mar 2023\nAda,Park,,,\n"
    )
    db = tmp_path / "m.db"
    migrate.run_migrations(db)
    engine = make_async_engine(db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sm() as s:
            await cx.apply_import(s, cx.parse_connections_csv(csv))
    finally:
        await engine.dispose()
    _go(db, "down", PREV)  # drops the positions, keeps the contacts
    _go(db, "up", "head")
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT count(*) FROM contact").fetchone() == (2,)
        assert con.execute("SELECT count(*) FROM connection_position").fetchone() == (2,)
    finally:
        con.close()
    engine = make_async_engine(db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sm() as s:
            prev = await cx.preview_import(s, cx.parse_connections_csv(csv))
            assert prev.new == [] and prev.missing == []
            await cx.apply_import(s, cx.parse_connections_csv(csv), keep_missing=False)
            assert (await s.execute(select(func.count()).select_from(Contact))).scalar() == 2
    finally:
        await engine.dispose()
