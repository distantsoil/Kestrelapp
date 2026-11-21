"""The install ID (D-INST-1, D-INST-2): one random value per install, for bug reports and, later,
for following data when it moves between machines.

A UUIDv4 from the OS random source, stored in the one row of its own table `install_identity`
(slot 1: install_id, created_at, version, updated_at; D-INST-15), never in `setting`. This module
is the only code that reads or writes that table. It is written once: at startup, or by the first
read that finds none. It is never derived from the hardware, the person, `ids.salt` or anything
else, so it says nothing about anyone.

The invariant this module serves: the ID never leaves the computer unless the person copies it.
Only the callers listed in tests/sql_row_guard.py CALLERS may call this module (About, Settings >
Your data, the diagnostics bundle, the startup step and a restore), and nothing here logs its
value. `KEY` is the old setting key, kept so a restore can strip it from a backup's settings."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import structlog
from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

import kestrel
from kestrel.db.base import utcnow_iso

log = structlog.get_logger()

KEY = "install.id"
# A version 4 UUID, lowercase canonical form, the only shape this module stores or accepts.
_UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def is_valid(value: object) -> bool:
    """Whether `value` is a lowercase canonical UUIDv4 string."""
    return isinstance(value, str) and bool(_UUID4.match(value))


def short_form(full: str) -> str:
    """`K-` and the first eight hex digits in two groups of four, uppercased: `K-7F3A-91C2`.
    Ten base32 characters make an application's Kestrel ID (D-IMPORT-9), so neither shape reads
    as the other."""
    hexpart = full.replace("-", "")[:8].upper()
    return f"K-{hexpart[:4]}-{hexpart[4:]}"


def _new_record() -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "version": kestrel.__version__,
    }


def record_from(value: object) -> dict[str, Any] | None:
    """The stored record when it is well formed, else None. Tolerates nothing else: the row is
    written only by this module, so any other shape is damage, not an older format."""
    if not isinstance(value, dict) or not is_valid(value.get("id")):
        return None
    created = value.get("created_at")
    version = value.get("version")
    return {
        "id": value["id"],
        "created_at": created if isinstance(created, str) else None,
        "version": version if isinstance(version, str) else None,
    }


# The longest stored ID text this module will parse, in bytes; anything longer is damage and is
# never parsed (D-SEC-20). The other columns are clamped to this length, never rejected.
MAX_STORED = 512


class _Stored:
    """The row as SQLite holds it: whether it exists, the ID column's storage type and bytes,
    and the other two columns."""

    def __init__(self, exists: bool, kind: str | None = None, value: object = None,
                 created: object = None, version: object = None) -> None:
        self.exists, self.kind, self.value = exists, kind, value
        self.created, self.version = created, version


# The table holds one row (D-INST-15); the ID column is read as bytes, so text that is not UTF-8
# cannot raise in the driver.
_SELECT = ("SELECT typeof(install_id), CAST(install_id AS BLOB), created_at, version "
           "FROM install_identity WHERE slot = 1")


async def _read_raw(session: AsyncSession) -> _Stored:
    """The row exactly as stored, never decoded by the ORM or the driver."""
    row = (await session.execute(text(_SELECT))).first()
    return _Stored(False) if row is None else _Stored(True, *row)


def _clamp(value: object) -> str | None:
    return value[:MAX_STORED] if isinstance(value, str) else None


def _decode(stored: _Stored) -> dict[str, Any] | None:
    """The record, read through an allowlist: only a TEXT ID of at most MAX_STORED bytes that is
    a lowercase version 4 UUID is kept. Any other type (integer, real, NULL, BLOB), a longer text
    or text that is not UTF-8 is damage."""
    if not stored.exists or stored.kind != "text" or not isinstance(stored.value, bytes):
        return None
    if len(stored.value) > MAX_STORED:
        return None
    try:
        full = stored.value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not is_valid(full):
        return None
    return {"id": full, "created_at": _clamp(stored.created), "version": _clamp(stored.version)}


async def ensure(session: AsyncSession) -> dict[str, Any]:
    """The install's record, writing it first when there is none or the stored one is damaged.
    Every write is conditional (insert-or-ignore; a repair only replaces the exact bytes it read)
    and is followed by a re-read, so two readers at once return the one stored value. The stored
    value is imported, never adopted: anything but a well-formed record is replaced (D-SEC-20).
    The caller commits."""
    stored = await _read_raw(session)
    rec = _decode(stored)
    if rec is not None:
        return rec
    fresh = _new_record()
    params = {"i": fresh["id"], "c": fresh["created_at"], "v": fresh["version"],
              "u": utcnow_iso()}
    if not stored.exists:
        result = await session.execute(
            text("INSERT INTO install_identity (slot, install_id, created_at, version, "
                 "updated_at) VALUES (1, :i, :c, :v, :u) ON CONFLICT(slot) DO NOTHING"),
            params,
        )
        if cast(CursorResult, result).rowcount == 1:
            log.info("install_id_created")
    else:
        # Damaged: replace it only if no one else has. Neither value is logged (D-INST-2).
        # The exact bytes and type that were read (`IS` also matches NULL).
        result = await session.execute(
            text("UPDATE install_identity SET install_id = :i, created_at = :c, version = :v, "
                 "updated_at = :u WHERE slot = 1 AND CAST(install_id AS BLOB) IS :old "
                 "AND typeof(install_id) = :kind"),
            {**params, "old": stored.value, "kind": stored.kind},
        )
        if cast(CursorResult, result).rowcount == 1:
            log.warning("install_id_replaced", reason="stored value is not a version 4 UUID")
    return _decode(await _read_raw(session)) or {}


# ------------------------------------------------------------- files outside the live session
_SELECT_SYNC = _SELECT


# Characters a lookalike key may add: every Unicode space, line or paragraph separator, control
# and format character (tab, newline, NBSP, zero-width space, word joiner and the like).
_INVISIBLE = frozenset({"Zs", "Zl", "Zp", "Cc", "Cf"})


def canonical_key(key: str) -> str:
    """A setting key as compared for lookalikes: casefolded, then NFKD with every combining mark
    removed ("İnstall.id", an accent), then every whitespace and invisible character removed. A
    key whose canonical form is KEY reads as the install ID's old setting key."""
    folded = unicodedata.normalize("NFKD", key.casefold())
    return "".join(ch for ch in folded
                   if not ch.isspace() and not unicodedata.combining(ch)
                   and unicodedata.category(ch) not in _INVISIBLE)


# The longest section row (the root row, an "install" row) a restore will read to strip the
# key from it. Settings sections are a few kilobytes; anything longer is not one.
MAX_SECTION = 1_000_000


def _strip_key_leaves(row_key: str, value: object) -> int:
    """Remove, in place, every leaf of a section row's mapping whose dotted path (row key first)
    reads as KEY. Iterative, so any depth that parsed is walked. Returns how many it removed."""
    removed = 0
    stack: list[tuple[tuple[str, ...], object]] = [((row_key,) if row_key else (), value)]
    while stack:
        parts, node = stack.pop()
        if not isinstance(node, dict):
            continue
        for k in list(node):
            path = (*parts, str(k))
            child = node[k]
            if isinstance(child, dict) and child:
                stack.append((path, child))
            elif canonical_key(".".join(path)) == KEY:
                del node[k]
                removed += 1
    return removed


def _row_action(key: str, raw: bytes | None) -> tuple[str, bytes | None, int]:
    """What a restore does with one staged setting row (D-INST-1, D-INST-17): ("delete", None, n)
    for a row keyed as KEY or a lookalike (n = 1), or a section row that cannot be checked all
    the way down (n = 0: longer than MAX_SECTION, nested too deep to parse, not UTF-8; uncertain
    never adopts); ("rewrite", new_value, n) for a section row (root, "install", a lookalike)
    with n install-shaped leaves stripped and other leaves kept; ("keep", None, 0) otherwise."""
    canon = canonical_key(key)
    if canon == KEY:
        return "delete", None, 1
    if not (canon == "" or KEY.startswith(canon + ".")) or raw is None:
        return "keep", None, 0
    if len(raw) > MAX_SECTION:
        return "delete", None, 0
    try:
        text_ = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "delete", None, 0
    try:
        value = json.loads(text_)
    except RecursionError:
        return "delete", None, 0
    except ValueError:  # not JSON: no settings reader finds a key inside it
        return "keep", None, 0
    n = _strip_key_leaves(key, value)
    if not n:
        return "keep", None, 0
    if value == {}:
        return "delete", None, n
    return "rewrite", json.dumps(value).encode("utf-8"), n


def read_record_sync(con: sqlite3.Connection, *, strict: bool = False) -> dict[str, Any] | None:
    """The record in a database opened with plain sqlite3 (the diagnostics bundle, a restore),
    through the same typed allowlist; None when there is none or it is damaged. Read only.
    `strict` lets a database error through (a restore must not mint a new ID because this
    computer's database could not be read)."""
    try:
        row = con.execute(_SELECT_SYNC).fetchone()
    except sqlite3.Error:
        if strict:
            raise
        return None
    return _decode(_Stored(True, *row)) if row is not None else None


class RestoreCounts:
    """What keep_across_restore removed from the backup's settings, as counts only."""

    def __init__(self) -> None:
        self.stripped = 0  # install-shaped leaves and rows removed
        self.unchecked = 0  # section rows dropped because they could not be checked


def keep_across_restore(staged_db: Path, live_db: Path,
                        source_db: Path | None = None) -> RestoreCounts:
    """A restore keeps this computer's install ID (D-INST-1, D-INST-15): the staged database's
    `install_identity` is rebuilt from our schema with this computer's record, read through the
    allowlist (with none here, a new one), and whatever the backup carried is never adopted. The
    backup's settings lose every install-shaped value: a row keyed as the old setting key (or a
    lookalike) is deleted, and inside a section row only the install-shaped leaves are removed,
    the rest kept (D-INST-17). Counts only, never a key or a value, are returned and logged.
    SOURCE_DB, the backup's own copy, is read only to note at debug that its ID differed (never
    the value, never adopted), once it passed the allowlist."""
    here = None
    if live_db.exists() and live_db.stat().st_size:
        con = sqlite3.connect(str(live_db), timeout=30)
        try:
            here = read_record_sync(con, strict=True)
        finally:
            con.close()
    counts = RestoreCounts()
    con = sqlite3.connect(str(staged_db), timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        keep = here or _new_record()
        with con:
            con.execute("DELETE FROM install_identity")
            con.execute(
                "INSERT INTO install_identity (slot, install_id, created_at, version, updated_at) "
                "VALUES (1, ?, ?, ?, ?)",
                (keep["id"], keep["created_at"], keep["version"], utcnow_iso()))
            # Keys and values are read as bytes, so none can raise.
            rows = con.execute(
                "SELECT CAST(key AS BLOB), CAST(value AS BLOB) FROM setting").fetchall()
            for raw_key, raw_value in rows:
                if not isinstance(raw_key, bytes):
                    continue
                action, new, n = _row_action(raw_key.decode("utf-8", "replace"), raw_value)
                if action == "delete":
                    con.execute("DELETE FROM setting WHERE CAST(key AS BLOB) = ?", (raw_key,))
                    counts.unchecked += n == 0
                elif action == "rewrite":
                    con.execute("UPDATE setting SET value = ? WHERE CAST(key AS BLOB) = ?",
                                (new.decode("utf-8") if new else None, raw_key))
                counts.stripped += n
    finally:
        con.close()
    source = None
    if source_db is not None and source_db.exists():
        con = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            source = read_record_sync(con)
        finally:
            con.close()
    if source is not None and source["id"] != keep["id"]:
        # That the IDs differed, never either value: the log is in the diagnostics bundle the
        # person shares, and only this computer's ID belongs there (D-INST-4).
        log.debug("install_id_kept_across_restore", differed=True)
    if counts.stripped or counts.unchecked:
        log.warning("restore_install_values_removed", stripped=counts.stripped,
                    unchecked=counts.unchecked)
    return counts


def view(rec: dict[str, Any]) -> dict[str, str]:
    """What a page shows: the short form and the full value."""
    full = rec.get("id") or ""
    return {"short": short_form(full) if full else "", "full": full}
