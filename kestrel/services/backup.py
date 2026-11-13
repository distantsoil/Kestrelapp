"""Backup and restore of the Kestrel data directory (SPEC-INTERFACES.md, `kestrel backup` /
`kestrel restore`, and the /settings web restore action). One module shared by the CLI and the web
endpoint so the safety behavior is defined exactly once.

A backup zip holds, at these fixed locations:
  kestrel.db                                   -- a consistent snapshot of the database, at the
                                                  zip root (sqlite3's online backup API, so a
                                                  live WAL database with writers mid-transaction
                                                  still yields a readable copy of every committed
                                                  row; -wal/-shm are never copied, and only read
                                                  back from archives older than this format)
  config/*.yml                                 -- the operator's YAML config (never .env)
  documents/documents/*.pdf                    -- generated resume / capability-statement PDFs
  documents/prep_packs/*.pdf                   -- generated interview prep-pack PDFs

Restore is an IMPORT, never a file swap of the backup's database (D-SEC-20):
  1. The archive is fully validated and extracted to a TEMP staging directory first. A truncated
     zip, an unsafe path entry, or a missing kestrel.db is caught here -- the real data directory
     is never touched until staging succeeds.
  2. The staged database is copied to a temp work file, its triggers and views are dropped, and a
     backup from an OLDER Kestrel is migrated to head there; it is then opened read-only and only
     read. A backup from a NEWER Kestrel is refused.
  3. A brand-new database is built in the data folder from Kestrel's own migrations (the path a
     fresh install takes), and rows are copied into it table by table, only for the tables in
     RESTORED_TABLES and only for the columns OUR schema has. Views, triggers, unknown tables and
     the task queue are not restored; they are counted in the result.
  4. The local-only rules (D-SEC-10 to D-SEC-19) run on that new database, the CURRENT database is
     snapshotted to a timestamped kestrel.db.pre-restore-<UTC> file, and only then is the new
     database written into the live one through SQLite (safe with the app's connections open),
     and the allow-listed config files and the documents copied in.
Nothing under data_home() other than the database, config, and generated documents is ever touched
(most importantly: .env is never read, written, or included in a backup -- secrets never leave the
machine this way, hard rule).

The one secret the database itself holds, the LAN shared key (`server.shared_secret` in the
`setting` table, kestrel/web/auth.py), is deleted from the staged snapshot before it is zipped
(`_strip_secrets`), never from the live database. A restore keeps the key this machine has now
(`_read_lan_key` before the write, `_put_back_lan_key` after), so an older backup that still
carries a key cannot replace it, and a machine with no key ends with none. LAN pairing is set up
again on a different computer."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog

from kestrel import paths
from kestrel.errors import UserFacingError, yaml_where
from kestrel.services.online_consent import CONSENT_PAYLOAD_KEYS

log = structlog.get_logger()

DB_NAME = "kestrel.db"
DB_SIDE_FILES = (f"{DB_NAME}-wal", f"{DB_NAME}-shm")
CONFIG_PREFIX = "config/"
DOCS_PREFIX = "documents/"
ML_PREFIX = "ml/"
# Subdirectories of data_dir() holding generated PDFs, each mapped under DOCS_PREFIX in the zip.
DOC_SUBDIRS = ("documents", "prep_packs")

# A backup is a snapshot of a local SQLite file plus YAML and PDFs: it is never large. These caps
# exist so a crafted or mis-selected archive (a 42.zip-style bomb) cannot fill the operator's disk,
# and so a wrong file picked in the file dialog is refused rather than extracted.
MAX_ARCHIVE_MEMBERS = 20_000
MAX_MEMBER_BYTES = 2 * 1024**3  # 2 GiB: one generated PDF or the database, not a bomb
MAX_TOTAL_BYTES = 8 * 1024**3  # 8 GiB across the whole archive

# Tables every Kestrel database has. A backup whose kestrel.db parses as SQLite but is missing
# these is not ours (or is from before the table existed): refuse it rather than install it.
_REQUIRED_TABLES = frozenset({"opportunity", "task", "evaluation"})
# How many timestamped pre-restore snapshots are kept beside the database (see _backup_current_db).
PRE_RESTORE_KEEP = 3


class InvalidBackupError(UserFacingError, ValueError):
    """A zip that does not look like (or safely extract as) a Kestrel backup. Raised before any
    write to the real data directory, so refusing it leaves nothing touched."""


# Every sentence restore_backup refuses with, as a closed list (D-SEC-4). The Settings restore
# page shows one of these, or a fallback, picked by kestrel.web.errors.authored_error; it never
# copies the exception's text. Anything that varies (an entry name, a size, the missing tables)
# goes to the log where it is refused, because an archive's own names reach the page otherwise.
NOT_A_ZIP = "Not a valid zip file."
NO_DATABASE = "This does not look like a Kestrel backup (no kestrel.db found in the archive)."
UNSAFE_ENTRY = "Refusing an unsafe path entry in the backup."
TOO_MANY_ENTRIES = (
    "This archive has more entries than a Kestrel backup can have; it is not one of ours."
)
OVERSIZED_ENTRY = "Refusing an oversized entry in the backup."
OVERSIZED_TOTAL = (
    "This backup unpacks to more bytes than a Kestrel backup can be. It is not one of ours."
)
ENTRY_UNREADABLE = "Could not read a file from the backup."
DB_NOT_OPENED = "The kestrel.db inside this backup could not be opened as a database."
DB_NOT_USABLE = (
    "The kestrel.db inside this backup is not a usable SQLite database, so "
    "restoring it would destroy the current one. Your database is unchanged."
)
DB_FAILED_CHECK = (
    "The kestrel.db inside this backup failed SQLite's integrity check, so it was not "
    "restored. Your database has not been touched."
)
DB_NOT_READABLE = (
    "The kestrel.db inside this backup is not a readable database, so it was not restored. "
    "Your database has not been touched."
)
NEWER_BACKUP = (
    "This backup was made by a newer version of Kestrel, so this version cannot open it. "
    "Update Kestrel, then restore it again. Your database has not been touched."
)
NOT_KESTREL_DB = (
    "The kestrel.db inside this backup is not a Kestrel database, so it was not restored. "
    "Your database has not been touched."
)
PAGE_SIZE_DIFFERS = (
    "The kestrel.db inside this backup uses a different page size from this one, so it cannot "
    "be restored in place. Nothing was changed."
)
BACKUP_NOT_FOUND = "The backup file was not found."
DB_NOT_UPGRADED = (
    "This backup's database could not be brought up to this version of Kestrel. Nothing was "
    "changed."
)
DB_NOT_IMPORTED = (
    "The data in this backup does not fit Kestrel's own tables, so it could not be restored. "
    "Nothing was changed."
)
BACKUP_REFUSALS: tuple[str, ...] = (
    NOT_A_ZIP, NO_DATABASE, UNSAFE_ENTRY, TOO_MANY_ENTRIES, OVERSIZED_ENTRY, OVERSIZED_TOTAL,
    ENTRY_UNREADABLE, DB_NOT_OPENED, DB_NOT_USABLE, DB_FAILED_CHECK, DB_NOT_READABLE,
    NEWER_BACKUP, NOT_KESTREL_DB, PAGE_SIZE_DIFFERS, BACKUP_NOT_FOUND, DB_NOT_UPGRADED,
    DB_NOT_IMPORTED,
)
# Said for a refusal that is not on the list (never the exception's text).
RESTORE_FALLBACK = "This file could not be restored as a Kestrel backup. Nothing was changed."


# Said after a ticked restore when a model server kept this computer's address (D-SEC-16). Plain
# words, never the address.
ADDRESSES_KEPT = (
    "{n} {servers} kept the address set on this computer, because each uses a key saved "
    "here; the backup's address for them was not used."
)


LOCAL_REFUSED = (
    "This computer's settings could not be read, so its AI and connection settings were kept as "
    "they are here."
)
BACKENDS_DROPPED = (
    "{n} {servers} in the backup had a name Kestrel does not use and {were} left out."
)


def counted(n: int, one: str, many: str) -> str:
    """`1 file` / `2 files`: a count with its noun, never `file(s)` (American English copy)."""
    return f"{n} {one if n == 1 else many}"


def addresses_kept_note(n: int) -> str:
    return ADDRESSES_KEPT.format(n=n, servers="model server" if n == 1 else "model servers")


def backends_dropped_note(n: int) -> str:
    return BACKENDS_DROPPED.format(
        n=n, servers="model server" if n == 1 else "model servers",
        were="was" if n == 1 else "were",
    )


def parts_failed_note(parts: list[str]) -> str:
    """D-SEC-23: plain words for a restore that wrote the database and then failed a later part.
    Not "left as before": a part can fail after some of its files were copied."""
    names = " and ".join(f"the {p}" for p in parts)
    return (f"The database was restored, but {names} could not be restored. Restore the same "
            "backup again to try them once more.")


@dataclass
class RestoreResult:
    db_restored: bool = False
    wal_restored: bool = False
    shm_restored: bool = False
    config_files: list[str] = field(default_factory=list)
    document_files: list[str] = field(default_factory=list)
    pre_restore_backup: Path | None = None
    paths_cleared: int = 0
    rerankers_disabled: int = 0
    # D-SEC-10: a backup is hostile input. Local-only settings the backup carried that differ from
    # this computer's (the model route, keys, server and MCP switches) are kept as they are here;
    # queued work that carried an online confirmation loses it; archive entries that are not a
    # generated document or a config file are refused. Counts only, never a name or a value.
    settings_kept: int = 0
    consents_cleared: int = 0
    entries_refused: int = 0
    # D-SEC-11: the person ticked "Also restore this backup's AI and connection settings", so
    # those keys came from the backup. `settings_kept` then counts only the consent state and the
    # server switches that always stay this computer's.
    local_settings_restored: bool = False
    # D-SEC-16: with that box ticked, a model server that uses a key saved on this computer keeps
    # this computer's address, so the key is never sent to an address the backup chose. A count.
    addresses_kept: int = 0
    # D-SEC-19: the box was ticked but this computer's settings could not be read, so nothing
    # local-only came from the backup; and backup model servers whose name Kestrel never writes.
    local_settings_refused: bool = False
    backends_dropped: int = 0

    # D-SEC-20: restore is an import. What the backup held that is never imported, as counts: a
    # table Kestrel does not have (or one of ours that is not a real table there), a view or a
    # trigger, the task queue's rows, and config files whose name Kestrel does not use.
    tables_ignored: int = 0
    schema_ignored: int = 0
    tasks_not_restored: int = 0
    config_skipped: int = 0
    # D-SEC-23: the parts that failed AFTER the database was written ("config files", "generated
    # documents"). The restore is then reported as what it is: the database restored, these not.
    parts_failed: list[str] = field(default_factory=list)

    def notes(self) -> list[str]:
        """The counts in plain sentences: never a path, never a value."""
        out: list[str] = []
        if self.parts_failed:
            out.append(parts_failed_note(self.parts_failed))
        if self.paths_cleared:
            out.append(
                f"{counted(self.paths_cleared, 'stored document path', 'stored document paths')} "
                "pointed outside the data folder and "
                f"{'was' if self.paths_cleared == 1 else 'were'} cleared."
            )
        if self.rerankers_disabled:
            out.append(
                f"{counted(self.rerankers_disabled, 'learned reranker', 'learned rerankers')} "
                f"{'was' if self.rerankers_disabled == 1 else 'were'} turned off and the model "
                "file paths cleared, so scoring uses the AI model until you retrain."
            )
        if self.local_settings_restored:
            out.append("The backup's AI and connection settings were restored, as you chose.")
        if self.addresses_kept:
            out.append(addresses_kept_note(self.addresses_kept))
        if self.local_settings_refused:
            out.append(LOCAL_REFUSED)
        if self.backends_dropped:
            out.append(backends_dropped_note(self.backends_dropped))
        if self.settings_kept:
            out.append(
                f"{counted(self.settings_kept, 'setting', 'settings')} that only this computer "
                "can change "
                f"{'was' if self.settings_kept == 1 else 'were'} kept as "
                f"{'it is' if self.settings_kept == 1 else 'they are'} here."
            )
        if self.tasks_not_restored:
            jobs = counted(self.tasks_not_restored, "queued or finished job",
                           "queued or finished jobs")
            out.append(
                f"{jobs} in the backup "
                f"{'was' if self.tasks_not_restored == 1 else 'were'} not restored; Kestrel starts "
                "the work it needs again on this computer."
            )
        if self.tables_ignored or self.schema_ignored:
            n = self.tables_ignored + self.schema_ignored
            out.append(
                f"{counted(n, 'item', 'items')} in the backup's database "
                f"{'is' if n == 1 else 'are'} not part of Kestrel and "
                f"{'was' if n == 1 else 'were'} left out."
            )
        if self.config_skipped:
            out.append(
                f"{counted(self.config_skipped, 'config file', 'config files')} in the backup "
                f"{'is' if self.config_skipped == 1 else 'are'} not one Kestrel uses and "
                f"{'was' if self.config_skipped == 1 else 'were'} skipped."
            )
        if self.entries_refused:
            out.append(
                f"{counted(self.entries_refused, 'file', 'files')} in the backup "
                f"{'was' if self.entries_refused == 1 else 'were'} not a generated document or a "
                "config file and "
                f"{'was' if self.entries_refused == 1 else 'were'} skipped."
            )
        return out

    def summary_lines(self) -> list[str]:
        lines = [f"Database restored: {'yes' if self.db_restored else 'no'}."]
        lines.append(f"{counted(len(self.config_files), 'config file', 'config files')} restored.")
        lines.append(
            f"{counted(len(self.document_files), 'generated document', 'generated documents')} "
            "restored."
        )
        lines += self.notes()
        if self.pre_restore_backup is not None:
            # The file name only: no path, or any part of one, reaches a page (D-SEC-16 (d)).
            lines.append(f"Earlier data saved as {Path(str(self.pre_restore_backup)).name}.")
        return lines


# --------------------------------------------------------------------------------------- backup
def snapshot_database(src: Path, dest: Path) -> None:
    """Copy the database at `src` to `dest` through sqlite3's online backup API: a transactionally
    consistent image of every committed row, including what still sits in the WAL, taken while
    the app may be writing. A plain file copy of a WAL database can pair a stale main file with
    a WAL it does not match. Synchronous; the web route runs it in a thread."""
    source = sqlite3.connect(str(src))
    try:
        source.execute("PRAGMA busy_timeout=5000")
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _lan_setting_key() -> str:
    # Imported lazily: kestrel.web.auth is the one place the key name is defined, and the CLI's
    # backup path should not import the web layer at module load.
    from kestrel.web.auth import SETTING_KEY

    return SETTING_KEY


def _has_setting_table(con: sqlite3.Connection) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'setting'"
    ).fetchone()
    return row is not None


def _strip_secrets(snapshot: Path) -> None:
    """Delete the LAN shared key from a STAGED snapshot (never the live database) before it is
    zipped. The copy is switched out of WAL mode first so the delete lands in the main file that
    goes into the zip, not in a -wal beside it that is left behind. secure_delete zeroes only what
    THIS delete frees; the live database runs without it, so an earlier key value can still sit in
    its free pages or unused cell space, and the backup API copies pages verbatim. VACUUM then
    rebuilds the copy with no free space at all. It is safe here: the copy is a standalone temp
    file in rollback-journal mode that nothing else has open."""
    con = sqlite3.connect(str(snapshot))
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA secure_delete=ON")
        if _has_setting_table(con):
            con.execute("DELETE FROM setting WHERE key = ?", (_lan_setting_key(),))
            con.commit()
        con.execute("VACUUM")
    finally:
        con.close()


def create_backup(out_dir: str | Path | None = None) -> Path:
    """Zip a consistent snapshot of the database, the YAML config, and the generated
    documents/prep packs into a timestamped archive under out_dir (default: the data dir). Never
    includes .env or anything else under data_home()."""
    from kestrel.db import migrate

    migrate.run_migrations()  # ensure the db exists and is at head before it is copied into the zip
    out = Path(out_dir) if out_dir else paths.data_dir()
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archive = out / f"kestrel-backup-{stamp}.zip"

    db = paths.db_path()
    with (
        tempfile.TemporaryDirectory(prefix="kestrel-dbsnap-") as snap_dir,
        zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z,
    ):
        if db.exists():
            snapshot = Path(snap_dir) / DB_NAME
            snapshot_database(db, snapshot)
            _strip_secrets(snapshot)  # the LAN shared key stays on this machine
            z.write(snapshot, DB_NAME)
        cfg = paths.config_dir()
        if cfg.exists():
            for f in sorted(cfg.rglob("*")):
                if f.is_file() and f.suffix in (".yml", ".yaml"):  # never .env
                    z.write(f, f"{CONFIG_PREFIX}{f.relative_to(cfg).as_posix()}")
        for sub in DOC_SUBDIRS:
            d = paths.data_dir() / sub
            if d.exists():
                for f in sorted(d.glob("*.pdf")):
                    z.write(f, f"{DOCS_PREFIX}{sub}/{f.name}")
        # The trained reranker and its PCA (review finding 6): a reset deletes them, and
        # retraining needs enough labels to pass the promotion criteria again. Kept for
        # recovery by hand (copy back into data/ml); restore leaves this folder alone.
        # The signing key (secrets/model_signing.key) is never written to a backup and never
        # restored from one: copied-back files are unsigned here, so the loader refuses them
        # and they retrain.
        ml = paths.data_dir() / "ml"
        if ml.exists():
            for f in sorted(ml.rglob("*")):
                # The signing key is this computer's alone (D-SEC-13); it lives in secrets/,
                # never here, and a stray copy is skipped all the same.
                if f.is_file() and f.name != paths.model_signing_key_path().name:
                    z.write(f, f"{ML_PREFIX}{f.relative_to(ml).as_posix()}")
    return archive


_TEMP_PREFIX = "kestrel-backup-"


def create_backup_to_temp() -> Path:
    """Build the same archive `create_backup` writes, but in a private temp directory, for the
    web Backup page to stream and then discard. The caller owns cleanup via
    `discard_temp_backup`; a failure during the build removes the staging directory itself."""
    staging = Path(tempfile.mkdtemp(prefix=_TEMP_PREFIX))
    try:
        return create_backup(staging)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def discard_temp_backup(archive: str | Path) -> None:
    """Remove an archive from `create_backup_to_temp` and its staging directory. Only a directory
    that mkdtemp named with this module's prefix is removed wholesale; any other parent is left
    alone and just the file goes, so a mis-routed path can never delete real data."""
    p = Path(archive)
    parent = p.parent
    if parent.name.startswith(_TEMP_PREFIX) and parent.parent == Path(tempfile.gettempdir()):
        shutil.rmtree(parent, ignore_errors=True)
        return
    p.unlink(missing_ok=True)


# ------------------------------------------------------------------------------ profile export
PROFILE_NAME = "profile.yml"
PROFILE_BAK = "profile.yml.bak"
_PROFILE_INCOMING = "profile.yml.incoming"


class InvalidProfileError(UserFacingError, ValueError):
    """An uploaded profile that is not UTF-8 YAML with at least one track block that
    `load_profile` accepts. Raised before (or, for the final load check, rolled back after) any
    change to the real profile.yml, so a refused import leaves the previous profile in place."""


# The sentences import_profile refuses with, a closed list for the same reason as BACKUP_REFUSALS.
# `{line}` stands for one whole number, the YAML line (kestrel.web.errors.authored_error).
PROFILE_NOT_UTF8 = "The file is not UTF-8 text; profile.yml must be YAML."
PROFILE_NOT_YAML = "The file is not valid YAML."
PROFILE_NOT_YAML_AT = "The file is not valid YAML (line {line})."
PROFILE_NOT_A_MAPPING = "profile.yml must be a YAML mapping with track blocks."
PROFILE_NO_TRACK = (
    "No track block found; profile.yml needs an 'employment:' or 'consulting:' mapping "
    "(see profile.example.yml)."
)
PROFILE_NOT_LOADED = (
    "The profile parsed but the loader could not build a track from it; the previous "
    "profile.yml was kept."
)
PROFILE_REFUSALS: tuple[str, ...] = (
    PROFILE_NOT_UTF8, PROFILE_NOT_YAML, PROFILE_NOT_YAML_AT, PROFILE_NOT_A_MAPPING,
    PROFILE_NO_TRACK, PROFILE_NOT_LOADED,
)
PROFILE_FALLBACK = "This file could not be imported as a profile. Your profile was not changed."


@dataclass
class ProfileImportResult:
    path: Path
    tracks: list[str] = field(default_factory=list)
    previous_backup: Path | None = None


def profile_path() -> Path:
    """The operator's own profile.yml (never the shipped example). May not exist yet."""
    return paths.config_dir() / PROFILE_NAME


def export_profile() -> Path | None:
    """The operator's profile.yml for download, or None when only the shipped example exists.
    The example is deliberately not exported: it is fiction, not the operator's profile."""
    p = profile_path()
    return p if p.is_file() else None


def import_profile(data: bytes) -> ProfileImportResult:
    """Replace config/profile.yml with an uploaded one, safely.

    Order: (1) decode and parse the upload and require an `employment:` or `consulting:` mapping,
    all before touching disk; (2) write the candidate beside the real file and copy the current
    profile.yml to profile.yml.bak; (3) swap the candidate in and confirm the REAL loader
    (`kestrel.pipeline.profile.load_profile`) accepts at least one track; (4) on failure put the
    previous file back and raise. Nothing but profile.yml, profile.yml.bak and the transient
    .incoming file is ever written; .env is never read or touched."""
    import os

    import yaml

    from kestrel.db.enums import TRACKS
    from kestrel.pipeline.profile import load_profile

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidProfileError(PROFILE_NOT_UTF8) from exc
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        log.info("profile_upload_invalid_yaml", error=str(exc))
        raise InvalidProfileError(f"The file is not valid YAML{yaml_where(exc)}.") from exc
    if not isinstance(parsed, dict):
        raise InvalidProfileError(PROFILE_NOT_A_MAPPING)
    present = [t for t in TRACKS if isinstance(parsed.get(t), dict)]
    if not present:
        raise InvalidProfileError(PROFILE_NO_TRACK)

    paths.ensure_dirs()
    target = profile_path()
    bak = target.with_name(PROFILE_BAK)
    incoming = target.with_name(_PROFILE_INCOMING)
    incoming.write_text(text, encoding="utf-8")

    had_previous = target.is_file()
    if had_previous:
        shutil.copy2(target, bak)
    os.replace(incoming, target)

    loaded: list[str] = []
    for track in present:
        try:
            if load_profile(track) is not None:
                loaded.append(track)
        except (TypeError, ValueError):  # e.g. non-string keys that SimpleNamespace refuses
            continue
    if not loaded:
        if had_previous:
            shutil.copy2(bak, target)
        else:
            target.unlink(missing_ok=True)
        raise InvalidProfileError(PROFILE_NOT_LOADED)
    return ProfileImportResult(
        path=target, tracks=loaded, previous_backup=bak if had_previous else None
    )


# -------------------------------------------------------------------------------------- restore
def _is_safe_member(name: str) -> bool:
    """Reject absolute paths, drive letters, and any ".." path-traversal component."""
    posix = name.replace("\\", "/")
    if posix.startswith("/") or ":" in posix:
        return False
    return all(part != ".." for part in posix.split("/"))


# The config files a restore installs (D-SEC-20). Matched against a backup's `config/<name>` entry
# exactly, after folding case and dropping trailing dots and spaces, because Windows and macOS
# open `Settings.yml` and `settings.yml.` as the same file: settings.yml in any spelling always
# goes through the local-only merge. Anything else under config/ is skipped and counted.
CONFIG_FILES: tuple[str, ...] = (
    "settings.yml", "profile.yml", "filters.yml", "sources.yml", "profile.example.yml",
)
SETTINGS_FILE = "settings.yml"


def _config_name(member: str) -> str | None:
    """The CONFIG_FILES name a `config/...` archive entry stands for, or None."""
    rel = member[len(CONFIG_PREFIX):]
    if "/" in rel or "\\" in rel:
        return None
    folded = rel.rstrip(". ").casefold()
    return folded if folded in CONFIG_FILES else None


def config_plan(members: list[str]) -> tuple[dict[str, str], int]:
    """Which archive entry supplies each config file, and how many config entries are skipped.

    The one function that decides it, for the restore and for its preview alike, so the preview
    lists exactly the settings.yml the restore merges. Where a backup carries more than one
    spelling of a name, the exact spelling wins, then the first in sorted order; the rest are
    skipped. Returns ({canonical name: archive entry}, skipped)."""
    entries = sorted(m for m in members if m.startswith(CONFIG_PREFIX))
    plan: dict[str, str] = {}
    skipped = 0
    for member in sorted(
        entries, key=lambda m: (m != CONFIG_PREFIX + (_config_name(m) or ""), m)
    ):
        name = _config_name(member)
        if name is None or name in plan:
            skipped += 1
            continue
        plan[name] = member
    return plan, skipped


_READ_BLOCK = 1024 * 1024  # check_backup reads an entry it does not keep this much at a time
# Compression methods a restore reads (D-SEC-31). Kestrel writes only DEFLATED; zipfile bounds
# memory per read for these two. BZIP2 and LZMA inflate a whole compressed block per read, so a
# small crafted entry can take gigabytes before its CRC is checked: they are refused unread.
_READABLE_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
# Everything zipfile and its decompressors raise on a damaged or unsupported entry.
_UNREADABLE_ERRORS = (
    zipfile.BadZipFile, RuntimeError, OSError, zlib.error, EOFError, NotImplementedError,
    ValueError,
)


def _stage_backup(
    src: Path, staging: Path, *, settings_only: bool = False, database_only: bool = False
) -> int:
    """Validate the archive and extract exactly the entries this format defines into `staging`.
    Raises InvalidBackupError on anything short of a well-formed Kestrel backup; nothing outside
    `staging` is touched, so a failure here never affects the real data directory. Config files
    are staged under their canonical names (`config_plan`). Returns the number of config entries
    skipped. `database_only` (check_backup) runs every check on every entry and READS every entry
    to its end, which is where zipfile checks its CRC, but keeps only the database and its side
    files: every other entry is staged as an empty file under the name the restore would use (so a
    name that cannot be written is found too) and its bytes are read a block at a time and
    dropped, never held in memory (D-SEC-30)."""
    try:
        zf = zipfile.ZipFile(src)
    except zipfile.BadZipFile as exc:
        raise InvalidBackupError(NOT_A_ZIP) from exc
    with zf:
        names = set(zf.namelist())
        if DB_NAME not in names:
            raise InvalidBackupError(NO_DATABASE)
        candidates = [
            info
            for info in zf.infolist()
            if not info.is_dir()
            and (
                info.filename in (DB_NAME, *DB_SIDE_FILES)
                or info.filename.startswith(CONFIG_PREFIX)
                or info.filename.startswith(DOCS_PREFIX)
            )
        ]
        for info in candidates:
            if not _is_safe_member(info.filename):
                log.warning("restore_refused", why="unsafe_entry", entry=info.filename)
                raise InvalidBackupError(UNSAFE_ENTRY)
        plan, skipped = config_plan([i.filename for i in candidates])
        wanted_config = {
            member: name for name, member in plan.items()
            if not settings_only or name == SETTINGS_FILE
        }
        members = [
            info for info in candidates
            if info.filename in (DB_NAME, *DB_SIDE_FILES)
            or info.filename in wanted_config
            or (not settings_only and info.filename.startswith(DOCS_PREFIX))
        ]
        # Caps BEFORE extracting anything: zipfile reports sizes in the central directory, so a
        # decompression bomb is refused without writing a byte to the staging directory.
        if len(members) > MAX_ARCHIVE_MEMBERS:
            log.warning("restore_refused", why="too_many_entries", entries=len(members))
            raise InvalidBackupError(TOO_MANY_ENTRIES)
        total = 0
        for info in members:
            if info.compress_type not in _READABLE_COMPRESSION:
                log.warning(
                    "restore_refused", why="unsupported_compression", entry=info.filename,
                    compress_type=info.compress_type,
                )
                raise InvalidBackupError(ENTRY_UNREADABLE)
            if info.file_size > MAX_MEMBER_BYTES:
                log.warning(
                    "restore_refused", why="oversized_entry", entry=info.filename,
                    bytes=info.file_size,
                )
                raise InvalidBackupError(OVERSIZED_ENTRY)
            total += info.file_size
        if total > MAX_TOTAL_BYTES:
            log.warning("restore_refused", why="oversized_total", bytes=total)
            raise InvalidBackupError(OVERSIZED_TOTAL)
        for info in members:
            read_only = database_only and info.filename not in (DB_NAME, *DB_SIDE_FILES)
            name = wanted_config.get(info.filename)
            dest = staging / (CONFIG_PREFIX + name if name else info.filename)
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as fsrc, open(dest, "wb") as fdst:
                    if read_only:
                        while fsrc.read(_READ_BLOCK):  # the CRC is checked at the end
                            pass
                    else:
                        shutil.copyfileobj(fsrc, fdst)
            except _UNREADABLE_ERRORS as exc:
                # An encrypted or otherwise unreadable member is a bad archive, not a 500. The
                # message reaches a page, so the error text (it can name a path) goes to the log
                # only (D-SEC-1).
                log.warning(
                    "restore_entry_unreadable",
                    entry=info.filename,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                raise InvalidBackupError(ENTRY_UNREADABLE) from exc

    _validate_staged_db(staging / DB_NAME)
    return skipped


def _validate_staged_db(staged: Path) -> None:
    """Refuse a backup whose kestrel.db is not a readable SQLite database carrying our schema.

    `_stage_backup` validates the archive's STRUCTURE (it is a zip, it has a kestrel.db, no unsafe
    paths). Structure alone is not enough: a zip whose kestrel.db holds the bytes `not a database`
    passes every check above and would then be copied over a working database, reporting
    `db_restored=True`. `sqlite3.connect` succeeds on any path and only fails at the first query, so
    the check is an actual read plus an integrity pass. The real database is still untouched: this
    runs entirely inside the staging directory (review finding, restore validation)."""
    try:
        con = sqlite3.connect(f"file:{staged.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise InvalidBackupError(DB_NOT_OPENED) from exc
    try:
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as exc:
            raise InvalidBackupError(DB_NOT_USABLE) from exc
        if not row or row[0] != "ok":
            raise InvalidBackupError(DB_FAILED_CHECK)
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        revision = None
        if "alembic_version" in names:
            row = con.execute("SELECT version_num FROM alembic_version").fetchone()
            revision = row[0] if row else None
    except sqlite3.DatabaseError as exc:
        raise InvalidBackupError(DB_NOT_READABLE) from exc
    finally:
        con.close()
    # A backup made by a NEWER Kestrel is stamped with a revision this build does not know. It would
    # pass every check above, be written into the live database, and only then fail to migrate,
    # leaving an app that will not start. Refused here, while nothing real has been touched. (If
    # the shipped scripts cannot be read, `known` is None and doctor reports that separately.)
    from kestrel.db import migrate

    known = migrate.known_revisions()
    if revision is not None and known is not None and revision not in known:
        raise InvalidBackupError(NEWER_BACKUP)
    missing = sorted(_REQUIRED_TABLES - names)
    if missing:
        log.warning("restore_refused", why="missing_tables", tables=missing)
        raise InvalidBackupError(NOT_KESTREL_DB)


def _scrub_stored_paths(staged_db: Path) -> int:
    """Clear stored PDF paths that do not point inside Kestrel's own data folder (issue #150).

    The download routes already confine every path they serve or delete (`paths.confined_file`
    refuses an outside path, a `..` climb, and a symlink that leads out, and the contract test
    keeps it that way for every such route). A value that fails that check can never be served,
    but a backup is the uploader's own database, and a hostile `doc_render.pdf_path` /
    `prep_pack.pdf_path` would otherwise survive in the restored rows for every later render,
    list view and export to evaluate again. Restoring is where untrusted content enters the
    database, so a value the confinement check refuses is cleared to NULL at restore time; the
    file it points at is never read or touched. A path that IS inside the data folder (a backup
    restored on the machine it was made on) is kept, so same-machine restores lose nothing. A
    backup made on ANOTHER machine stores absolute paths from that machine; those fail the check
    and are cleared rather than kept as foreign paths -- re-rooting cross-machine paths is a
    separate feature (the private decision log, D-SEC-8), not a security hole.

    The staged database is a throwaway copy in the backup staging directory, so the write is
    confined to staging; `_write_into_live` then installs the scrubbed rows, not the hostile
    ones. Returns the number of values cleared."""
    import sqlite3

    from kestrel import paths

    try:
        con = sqlite3.connect(str(staged_db))
    except sqlite3.Error:
        return 0
    try:
        present = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        cleared = 0
        for table, folder in (
            ("doc_render", paths.documents_dir()),
            ("prep_pack", paths.prep_packs_dir()),
        ):
            if table not in present:
                continue
            for row_id, stored in con.execute(
                f"SELECT id, pdf_path FROM {table} WHERE pdf_path IS NOT NULL"  # noqa: S608  identifiers are code constants, values are bound
            ):
                if paths.confined_file(stored, folder) is None:
                    con.execute(f"UPDATE {table} SET pdf_path = NULL WHERE id = ?", (row_id,))  # noqa: S608  identifiers are code constants, values are bound
                    cleared += 1
        if cleared:
            con.commit()
        return cleared
    finally:
        con.close()


def _deactivate_rerankers(staged_db: Path) -> int:
    """Deactivate every learned reranker and clear its model file paths at restore time
    (issue #173).

    `load_active_reranker` unpickles the active row's `artifact_path` / `pca_artifact_path`, and
    loading a pickle can run arbitrary code. A backup is the uploader's own database, so a hostile
    row could name ANY file on the computer as the model; restoring is where untrusted content
    enters the database. A restored install therefore starts with no active reranker: scoring
    falls back to the LLM tier (hard rule 4) until the operator retrains on their own data.

    The rows are kept (their metrics and training history stay readable) but deactivated, and
    their artifact paths are cleared to NULL so nothing later can point at a foreign file. This is
    the primary defense; the loader's own outside-the-data-folder refusal
    (kestrel/learning/reranker.py) is the second line.

    The staged database is a throwaway copy in the backup staging directory, so the write is
    confined to staging; `_write_into_live` then installs the deactivated rows. Returns the number
    of reranker rows neutralized."""
    import sqlite3

    try:
        con = sqlite3.connect(str(staged_db))
    except sqlite3.Error:
        return 0
    try:
        present = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "ml_model" not in present:
            return 0
        cur = con.execute(
            "UPDATE ml_model SET is_active = 0, artifact_path = NULL, pca_artifact_path = NULL "
            "WHERE kind = 'reranker'"
        )
        n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        if n:
            con.commit()
        return n
    finally:
        con.close()


# Task payload keys that record an online confirmation: the one list in online_consent. A
# restored task must never carry one: a confirmation is a click on THIS computer.
_CONSENT_PAYLOAD_KEYS = CONSENT_PAYLOAD_KEYS
_PENDING_RUN_MODE_KEY = "pending_run_mode"  # setting_overrides.PENDING_RUN_MODE_KEY


def _strip_consent(value: object) -> tuple[object, bool]:
    """VALUE without any consent key at any depth, and whether anything was removed."""
    changed = False
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            if k in _CONSENT_PAYLOAD_KEYS:
                changed = True
                continue
            nv, ch = _strip_consent(v)
            out[k] = nv
            changed = changed or ch
        return out, changed
    if isinstance(value, list):
        items = [_strip_consent(v) for v in value]
        return [i[0] for i in items], any(i[1] for i in items)
    return value, False


# D-SEC-20: the tables a restore imports, as an explicit list. Every table of Kestrel's own schema
# is either here or in NOT_RESTORED_TABLES (a test keeps the two lists complete). The task queue is
# never restored: queued work (a model pull, a rebuild, an online Score again, a one-run model, a
# consent hold) is something THIS computer starts, and finished task rows are only the queue's own
# bookkeeping, so none is kept.
RESTORED_TABLES: frozenset[str] = frozenset({
    "calibration", "comp_observation", "connection", "connection_position", "contact",
    "contact_import", "doc_layout", "doc_master", "doc_render", "doc_section", "doc_slot",
    "doc_variant", "engagement_offer", "evaluation", "frontier_transmission", "interaction",
    "interview_question_set", "label", "letter", "ml_model", "mock_session", "notification",
    "offer", "opportunity", "opportunity_embedding", "org", "org_research", "org_summary",
    "outcome", "outreach_draft", "prep_pack", "profile", "pursuit", "quarantine",
    "requirement_correction", "run", "saved_view", "scrape_run", "selection_override", "setting",
    "source", "status_event", "story", "story_usage", "title_suggestion", "verdict_override",
})
NOT_RESTORED_TABLES: frozenset[str] = frozenset({"task", "alembic_version"})

# Setting rows that stand for queued work or a one-time token, never data a person expects back
# (D-SEC-20): a run's one-run model waiting for Tier 0, the queued one-shot run mode (it carried a
# confirmation), a model rebuild or re-check this computer owes, and Undo tokens from the session
# that made the backup. D-QUEUE-10: also every row that describes the queue the backup's computer
# had, which is never restored: the roles already re-queued once (`pipeline.orphan_requeue`, so a
# role whose task is gone would read "stuck" instead of getting its one retry here), the Stop press
# tied to that computer's run, its scheduler hold after Cancel scan, and a company summary Refresh
# held for an online confirmation that has no task here.
DROPPED_SETTING_KEYS = frozenset({
    _PENDING_RUN_MODE_KEY, "setup.rebuild_owed", "setup.recheck_owed",
    "pipeline.orphan_requeue", "worker.stop_request", "scheduler.scan_hold_until",
})
DROPPED_SETTING_PREFIXES = ("run_model.pending.", "undo.", "org_summary_hold.")


def _q(name: str) -> str:
    """An identifier quoted for SQLite. Only names from OUR schema reach a statement."""
    return '"' + name.replace('"', '""') + '"'


def _work_copy(
    staged: Path, workdir: Path, own: frozenset[str] = frozenset()
) -> tuple[Path, int]:
    """A private copy of the staged database that the import reads (D-SEC-20).

    The staged file is never written. The copy has the backup's own write-ahead log folded in, its
    triggers and views dropped (they are the backup's code, and are never run or kept), and, for a
    backup made by an OLDER Kestrel, is migrated to head here, so the rows are read in today's
    shape. Returns the copy and how many views and triggers the backup carried that Kestrel's own
    schema does not define (OWN: the names of ours; those are dropped too, and rebuilt by our
    migrations, never copied)."""
    workdir.mkdir(parents=True, exist_ok=True)
    work = workdir / DB_NAME
    for name in (DB_NAME, *DB_SIDE_FILES):
        if (staged.parent / name).exists():
            shutil.copyfile(staged.parent / name, workdir / name)
    try:
        con = sqlite3.connect(str(work), timeout=30)
        try:
            con.execute("PRAGMA busy_timeout=5000")
            con.execute("PRAGMA journal_mode=DELETE")  # folds a shipped -wal into the copy
            objects = con.execute(
                "SELECT type, name FROM sqlite_master WHERE type IN ('trigger', 'view')"
            ).fetchall()
            for kind, name in objects:
                what = "TRIGGER" if kind == "trigger" else "VIEW"
                con.execute(f"DROP {what} IF EXISTS {_q(name)}")
            con.commit()
            row = con.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone() if con.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'alembic_version'"
            ).fetchone() else None
        finally:
            con.close()
    except sqlite3.Error as exc:
        log.warning("restore_refused", why="work_copy", error_type=type(exc).__name__)
        raise InvalidBackupError(DB_NOT_READABLE) from exc
    from kestrel.db import migrate

    if (row[0] if row else None) != migrate.head_revision():
        try:
            migrate.run_migrations(work)
            con = sqlite3.connect(str(work), timeout=30)
            try:
                con.execute("PRAGMA journal_mode=DELETE")  # read-only from here on
            finally:
                con.close()
        # D-SEC-23: ANY failure here is the backup's data meeting a migration (a crafted older
        # database can make a step raise whatever the step's code raises: a TypeError on a BLOB,
        # an IntegrityError, an AttributeError). Only the private work copy was touched, so it is
        # always the fixed refusal with nothing changed; the type and text go to the log only.
        except Exception as exc:  # noqa: BLE001 - the boundary: refuse, never a bare 500
            log.warning("restore_refused", why="upgrade_failed", error_type=type(exc).__name__,
                        error=str(exc))
            raise InvalidBackupError(DB_NOT_UPGRADED) from exc
    return work, sum(1 for _kind, name in objects if name not in own)


@dataclass
class _Imported:
    tables_ignored: int = 0
    tasks_left: int = 0


def _import_rows(source: Path, target: Path) -> _Imported:
    """Copy the backup's rows into TARGET, a database built from Kestrel's own migrations
    (D-SEC-20). SOURCE is opened read-only and only read. Only the tables in RESTORED_TABLES are
    copied, only where the backup has them as real tables (a view or a virtual table of the same
    name is not one), and only for the columns TARGET's schema has; the column list comes from
    TARGET. Everything else in the backup is counted, never copied."""
    out = _Imported()
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        src.execute("PRAGMA query_only=ON")
        listed = src.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
        real = {
            name for kind, name, sql in listed
            if kind == "table" and not str(sql or "").lstrip().upper().startswith("CREATE VIRTUAL")
        }
        for _kind, name, _sql in listed:
            if name.startswith("sqlite_"):
                continue
            known = name in RESTORED_TABLES or name in NOT_RESTORED_TABLES
            if not known or name not in real:
                out.tables_ignored += 1
        if "task" in real:
            out.tasks_left = src.execute("SELECT COUNT(*) FROM task").fetchone()[0]
        dst = sqlite3.connect(str(target), timeout=30)
        try:
            dst.execute("PRAGMA journal_mode=WAL")
            dst.execute("PRAGMA busy_timeout=5000")
            dst.execute("PRAGMA foreign_keys=OFF")  # rows arrive table by table, in name order
            ours = {
                r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            with dst:
                for table in sorted(RESTORED_TABLES & ours & real):
                    mine = [r[1] for r in dst.execute(f"PRAGMA table_info({_q(table)})")]
                    theirs = {r[1] for r in src.execute(f"PRAGMA table_info({_q(table)})")}
                    cols = [c for c in mine if c in theirs]
                    if not cols:
                        continue
                    listing = ", ".join(_q(c) for c in cols)
                    dst.execute(f"DELETE FROM {_q(table)}")  # noqa: S608  identifiers are code constants, values are bound
                    insert = (
                        f"INSERT INTO {_q(table)} ({listing}) "  # noqa: S608  identifiers are code constants, values are bound
                        f"VALUES ({', '.join('?' for _ in cols)})"
                    )
                    cur = src.execute(f"SELECT {listing} FROM {_q(table)}")  # noqa: S608  identifiers are code constants, values are bound
                    while batch := cur.fetchmany(500):
                        dst.executemany(insert, batch)
        finally:
            dst.close()
    except sqlite3.Error as exc:
        log.warning("restore_refused", why="import_failed", error_type=type(exc).__name__,
                    error=str(exc))
        raise InvalidBackupError(DB_NOT_IMPORTED) from exc
    finally:
        src.close()
    return out


# The one kind of task row a restore brings back (D-SEC-20 amendment): a FINISHED follow-up draft,
# as inert history, because its payload is the only copy of the drafted message the Follow up
# dialog shows (D-PERF-5). Only status `done`, only the newest per application, and only the
# fields the dialog reads: never a consent key, a model or route field, or anything a worker runs.
FOLLOW_UP_TYPE = "follow_up"
FOLLOW_UP_FIELDS = ("pursuit_id", "opportunity_id", "facts", "result", "style_flags", "error",
                    "prompt_version")


def _import_follow_up_drafts(source: Path, target: Path) -> int:
    """Copy the newest finished follow-up draft of each application from SOURCE (read-only) into
    TARGET as a `done` task holding only FOLLOW_UP_FIELDS. Returns how many were copied."""
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        src.execute("PRAGMA query_only=ON")
        listed = src.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'task'"
        ).fetchone()
        if not listed or str(listed[0] or "").lstrip().upper().startswith("CREATE VIRTUAL"):
            return 0
        rows = src.execute(
            "SELECT id, payload, created_at, updated_at FROM task "
            "WHERE type = ? AND status = 'done' ORDER BY id", (FOLLOW_UP_TYPE,)
        ).fetchall()
    finally:
        src.close()
    newest: dict[int, tuple[dict, object, object]] = {}
    for _id, raw, created, updated in rows:
        try:
            payload = json.loads(raw) if isinstance(raw, str) else None
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        pid = payload.get("pursuit_id")
        if not isinstance(pid, int) or isinstance(pid, bool):
            continue
        kept = {k: payload[k] for k in FOLLOW_UP_FIELDS if k in payload}
        if not isinstance(kept.get("result"), dict) and not kept.get("error"):
            continue  # no draft to show
        newest[pid] = (kept, created, updated)  # ordered by id: the last one is the newest
    if not newest:
        return 0
    con = sqlite3.connect(str(target), timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        with con:
            for pid in sorted(newest):
                kept, created, updated = newest[pid]
                con.execute(
                    "INSERT INTO task (type, payload, status, priority, attempts, max_attempts, "
                    "created_at, updated_at) VALUES (?, ?, 'done', 0, 0, 1, ?, ?)",
                    (FOLLOW_UP_TYPE, json.dumps(kept), str(created or ""), str(updated or "")),
                )
    finally:
        con.close()
    return len(newest)


def _drop_markers(db: Path) -> None:
    """Remove the setting rows that stand for queued work or a one-time token (D-SEC-20)."""
    con = sqlite3.connect(str(db), timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        with con:
            for key in DROPPED_SETTING_KEYS:
                con.execute("DELETE FROM setting WHERE key = ?", (key,))
            for prefix in DROPPED_SETTING_PREFIXES:
                con.execute(
                    "DELETE FROM setting WHERE substr(key, 1, ?) = ?", (len(prefix), prefix)
                )
    finally:
        con.close()


def _fsync(path: Path) -> None:
    with open(path, "r+b") as fh:
        os.fsync(fh.fileno())


def _discard_db(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            path.with_name(path.name + suffix).unlink(missing_ok=True)
        except OSError as exc:  # a leftover temp file is swept by the next restore
            log.warning("restore_temp_left", file=path.name + suffix, error_type=type(exc).__name__)


def _local_only(key: str, value: object = None) -> bool:
    """Whether a setting row or leaf at KEY is local-only, whatever its shape: a section or parent
    that contains a local-only key counts (kestrel.web.settings_io.needs_local)."""
    from kestrel.web.settings_io import needs_local

    return needs_local(key, value)


def _within_local(key: str) -> bool:
    """Whether KEY is a local-only setting or inside one (not merely a section containing one)."""
    from kestrel.web.settings_io import within_local

    return within_local(key)


# D-SEC-11: what stays THIS computer's even when the person restores a backup's AI and connection
# settings. Consent is a click on this computer (the Hosted mode switch and its accepted
# disclosure), and the listening switches change what can reach this computer, so a backup never
# carries them in.
ALWAYS_KEPT_KEYS = frozenset({
    "llm.hosted_mode.enabled", "llm.hosted_mode.acknowledged_at",
    "llm.hosted_mode.acknowledged_terms",
    # Whether an online send hides the current employer is a privacy choice made here, like the
    # consent itself: a backup never lowers it, ticked or not (D-SEC-17).
    "llm.hosted_mode.redact_confidential",
})
ALWAYS_KEPT_PREFIX = ("server.",)
# Backend fields are matched by walking the path's segments (D-SEC-19), and a backup backend whose
# name could not be one segment (`a.b`) is dropped before any of this runs.
# Which key a backend reads is never a backup's to say (D-SEC-11): a restored `api_key_env` could
# name an online provider's key and point the backend at any address.
_KEY_FIELDS = frozenset({"api_key", "api_key_env"})
# A backend's own privacy controls for an online provider (whether it may keep or train on the
# prompts, zero data retention): this computer's choice, never a backup's (D-SEC-18).
_PRIVACY_FIELDS = frozenset({("hosted", "data_collection"), ("hosted", "zdr")})
# A backend name Kestrel writes: one path segment. Anything else in a backup is dropped.
_BACKEND_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def _backend_field(path: str) -> tuple[str, tuple[str, ...]] | None:
    """(backend name, the field's segments) when PATH is inside `llm.backends.<name>`."""
    parts = path.split(".")
    if len(parts) >= 3 and parts[0] == "llm" and parts[1] == "backends":
        return parts[2], tuple(parts[3:])
    return None


def _is_key_field(path: str) -> bool:
    bf = _backend_field(path)
    return bool(bf and bf[1][:1] and bf[1][0] in _KEY_FIELDS)


def _is_backend_url(path: str) -> bool:
    bf = _backend_field(path)
    return bool(bf and bf[1][:1] == ("base_url",))


def _clean_backends(backends: object) -> int:
    """Drop, in place, every backend whose name is not one plain segment. Returns the count."""
    if not isinstance(backends, dict):
        return 0
    bad = [k for k in backends if not (isinstance(k, str) and _BACKEND_NAME.match(k))]
    for k in bad:
        backends.pop(k)
    return len(bad)


def _backends_in(key: str, value: object) -> object:
    """The `llm.backends` mapping inside a row at KEY holding VALUE, if the row holds one."""
    parts = tuple(key.split(".")) if key else ()
    if parts == ("llm", "backends"):
        return value
    if parts == ("llm",):
        return value.get("backends") if isinstance(value, dict) else None
    if parts == ():
        llm = value.get("llm") if isinstance(value, dict) else None
        return llm.get("backends") if isinstance(llm, dict) else None
    return None


# The placeholder key the shipped Ollama backend sends (Ollama ignores it): not a saved key.
_PLACEHOLDER_KEYS = frozenset({"ollama"})


def _always_kept(path: str, keyed: frozenset[str] = frozenset()) -> bool:
    """Whether the leaf at PATH stays this computer's even with the opt-in ticked. KEYED names the
    backends that carry a key saved on this computer: their address stays this computer's too
    (D-SEC-16), so a backup can never point this computer's key at an address it chose."""
    if path in ALWAYS_KEPT_KEYS or path.startswith(ALWAYS_KEPT_PREFIX) or path in (
        "llm.hosted_mode", "server",
    ):
        return True
    bf = _backend_field(path)
    if bf is None:
        return False
    name, field_parts = bf
    if field_parts[:1] and field_parts[0] in _KEY_FIELDS or field_parts[:2] in _PRIVACY_FIELDS:
        return True
    return field_parts[:1] == ("base_url",) and name in keyed


def _stays_here(path: str, opt_in: bool, keyed: frozenset[str] = frozenset()) -> bool:
    """Whether the setting at PATH (a dotted key, or a container of keys) is taken from this
    computer on restore. Without the opt-in every local-only key is; with it, only the consent
    state, the listening switches, the key fields and a keyed backend's address. A section that
    only contains a local-only key (`notify`) is not taken whole: the walk descends into it."""
    if not (_within_local(path) or _within_local(path + ".x")):
        return False
    if not opt_in:
        return True
    return _always_kept(path, keyed)


def _decode(raw: object) -> object:
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return _ABSENT


def _leaf_rows(rows: dict[str, tuple[str, str]]) -> dict[str, tuple[str, str, str]]:
    """Every setting row that touches a local-only key, flattened to dotted leaves (D-SEC-16): a
    row `llm` holding a mapping becomes `llm.backends.x.base_url`, `llm.mode` and so on, so the
    filters below see leaf keys whatever shape the backup used. Returns leaf -> (JSON value,
    updated_at, the row key it came from). A more specific row wins a clash, as nesting does."""
    out: dict[str, tuple[str, str, str]] = {}
    for key in sorted(rows, key=lambda k: (k.count("."), k)):
        if not (_local_only(key) or key == "confidential_mode"):
            continue
        raw, updated = rows[key]
        value = _decode(raw)
        if isinstance(value, dict) and value:
            for leaf, v in _flat(value, key).items():
                out[leaf] = (json.dumps(v), updated, key)
        else:
            out[key] = (raw, updated, key)
    return out


def _regroup(leaves: dict[str, tuple[str, str, str]]) -> dict[str, tuple[str, str]]:
    """Rows from leaves: leaves that came from one row go back into that row (so a server row the
    Models page wrote stays one row it can delete), unless that row would overlap another row's
    key, in which case its leaves are written one per row. A leaf that is a parent of another
    leaf is dropped (it could only be a scalar standing where a section belongs)."""
    paths_ = sorted(leaves)
    leaves = {
        p: v for p, v in leaves.items() if not any(q.startswith(p + ".") for q in paths_)
    }
    origin = {p: v[2] for p, v in leaves.items()}
    while True:
        keys = set(origin.values())
        clash = {
            k for k in keys
            if any(o != k and (o.startswith(k + ".") or k.startswith(o + ".")) for o in keys)
            and any(origin[p] == k and p != k for p in origin)
        }
        if not clash:
            break
        for p in origin:
            if origin[p] in clash:
                origin[p] = p
    rows: dict[str, tuple[str, str]] = {}
    groups: dict[str, list[str]] = {}
    for p, k in origin.items():
        groups.setdefault(k, []).append(p)
    for k, members in groups.items():
        updated = max(leaves[p][1] or "" for p in members)
        if members == [k]:
            rows[k] = (leaves[k][0], updated)
            continue
        nested: dict = {}
        for p in members:
            cursor = nested
            parts = p[len(k) + 1:].split(".")
            for part in parts[:-1]:
                cursor = cursor.setdefault(part, {})
            cursor[parts[-1]] = _decode(leaves[p][0])
        rows[k] = (json.dumps(nested), updated)
    return rows


def _keep_local_settings(
    staged_db: Path, live_db: Path, opt_in: bool = False, keyed: frozenset[str] = frozenset(),
    counts: dict[str, int] | None = None, live_confidential: bool = False,
) -> int:
    """Make the STAGED database's local-only settings exactly this computer's (D-SEC-10, D-SEC-16).

    A key holder cannot change these from the app (kestrel.web.settings_io.needs_local: where the
    models run, keys, MCP and server switches, autostart and the like), so a backup must not
    change them either: it is the one path that writes the whole table. Every row that touches a
    local-only key, on either side, is flattened to leaf keys first, so a whole-section row
    (`llm`, `notify`) is filtered leaf by leaf like any other. Each local-only leaf is then this
    computer's (or absent), except with the opt-in, where only the consent state, the listening
    switches, the key fields and the address of a backend that uses a key saved here stay.
    Confidential mode is the conditional key: a backup may turn it ON, never OFF; LIVE_CONFIDENTIAL
    is this computer's EFFECTIVE value (settings.yml counts, D-SEC-18), and when it is on and the
    backup does not say on, a row holding True is written. Returns the
    number of keys whose value in the backup differed from here (a count, never a value); COUNTS
    gets "addresses" (keyed backend addresses kept from here)."""
    live_rows = _setting_rows(live_db) if _has_db_table(live_db, "setting") else {}
    con = sqlite3.connect(str(staged_db))
    try:
        if not _has_setting_table(con):
            return 0
        dropped = _drop_bad_backend_rows(con)
        staged_rows = {
            k: (v, u) for k, v, u in con.execute("SELECT key, value, updated_at FROM setting")
        }
        theirs_l = _leaf_rows(staged_rows)
        mine_l = _leaf_rows(live_rows)
        final: dict[str, tuple[str, str, str]] = {}
        kept = 0
        addresses = 0
        for path in set(theirs_l) | set(mine_l):
            mine = mine_l.get(path)
            theirs = theirs_l.get(path)
            if path == "confidential_mode":
                on_here = live_confidential or (mine is not None and _decode(mine[0]) is True)
                use_mine = on_here and (theirs is None or _decode(theirs[0]) is not True)
                if use_mine and (mine is None or _decode(mine[0]) is not True):
                    mine = ("true", datetime.now(UTC).isoformat(), "confidential_mode")
            else:
                use_mine = _local_only(path) and (not opt_in or _always_kept(path, keyed))
            pick = mine if use_mine else theirs
            if use_mine and (_decode(mine[0]) if mine else _ABSENT) != (
                _decode(theirs[0]) if theirs else _ABSENT
            ):
                kept += 1
                if opt_in and _is_backend_url(path) and theirs is not None:
                    addresses += 1
            if pick is not None:
                final[path] = pick
        if live_confidential and "confidential_mode" not in theirs_l | mine_l:
            final["confidential_mode"] = (
                "true", datetime.now(UTC).isoformat(), "confidential_mode"
            )
        rows = _regroup(final)
        with con:
            for key in staged_rows:
                if _local_only(key) or key == "confidential_mode":
                    con.execute("DELETE FROM setting WHERE key = ?", (key,))
            for key, row in rows.items():
                _put_row(con, key, row)
        if counts is not None:
            counts["addresses"] = counts.get("addresses", 0) + addresses
            counts["backends_dropped"] = counts.get("backends_dropped", 0) + dropped
        return kept
    finally:
        con.close()


def _drop_bad_backend_rows(con: sqlite3.Connection) -> int:
    """Remove from the STAGED rows every backend whose name is not one plain segment (D-SEC-19):
    inside a whole-tree, `llm` or `llm.backends` row, and a `llm.backends.<name>...` row whose
    name is not plain. Returns how many were dropped."""
    n = 0
    with con:
        for key, raw in con.execute("SELECT key, value FROM setting").fetchall():
            bf = _backend_field(key)
            if bf is not None:
                if not _BACKEND_NAME.match(bf[0]):
                    con.execute("DELETE FROM setting WHERE key = ?", (key,))
                    n += 1
                continue
            value = _decode(raw)
            dropped = _clean_backends(_backends_in(key, value))
            if dropped:
                con.execute("UPDATE setting SET value = ? WHERE key = ?", (json.dumps(value), key))
                n += dropped
    return n


def _live_settings(live_db: Path):
    """This computer's effective Settings (settings.yml, the packaged backends it lacks, the
    environment and the setting rows), or None when they cannot be loaded; read before restore.
    An invalid row that is not local-only (a bad colour scheme) does not decide anything a
    restore protects, so a second try uses only the local-only rows and confidential mode."""
    import yaml
    from pydantic import ValidationError

    from kestrel import config as config_mod
    from kestrel.services.setting_overrides import overrides_from_rows

    failures = (ValidationError, yaml.YAMLError, ValueError, TypeError, KeyError, AttributeError,
                OSError, RuntimeError, sqlite3.Error)
    try:
        rows = _setting_rows(live_db) if _has_db_table(live_db, "setting") else {}
    except sqlite3.Error as exc:
        log.warning("restore_live_settings_unreadable", error=type(exc).__name__)
        return None
    decoded = [(k, v) for k, (raw, _u) in rows.items() if (v := _decode(raw)) is not _ABSENT]
    protected = [(k, v) for k, v in decoded if _local_only(k) or k == "confidential_mode"]
    for attempt in (decoded, protected):
        try:
            return config_mod.load_settings(db_overrides=overrides_from_rows(attempt))
        except failures as exc:
            # Fail closed (D-SEC-19): with neither, the caller keeps every local-only key here.
            log.warning("restore_live_settings_unreadable", error=type(exc).__name__)
    return None


def _raw_confidential(live_db: Path, live_yml: Path) -> bool:
    """Whether confidential mode may be on here when the settings cannot be loaded (D-SEC-19):
    True unless the raw settings.yml, the environment and the row all say it is off. A value
    that cannot be read counts as on: a restore may turn it on, never off."""
    import yaml

    env = os.environ.get("KESTREL_CONFIDENTIAL_MODE")
    if env is not None and env.strip().lower() not in ("0", "false", "no", "off", ""):
        return True
    try:
        if _has_db_table(live_db, "setting"):
            row = _setting_rows(live_db).get("confidential_mode")
            if row is not None and _decode(row[0]) is not False:
                return True
        base = live_yml if live_yml.is_file() else Path(
            str(paths.packaged_config() / "settings.yml"))
        tree = yaml.safe_load(base.read_text(encoding="utf-8")) if base.is_file() else {}
    except (yaml.YAMLError, OSError, UnicodeDecodeError, sqlite3.Error):
        return True
    if tree is None:
        return False
    if not isinstance(tree, dict):
        return True
    value = tree.get("confidential_mode", False)
    return value is not False


def _keyed_backends(live_db: Path, live_yml: Path, live=None) -> frozenset[str]:
    """The backends that, on this computer, carry a saved key (an `api_key_env` or a real
    `api_key`): this computer's settings.yml (or the packaged one) with its setting rows on top.
    Read before anything is restored."""
    import yaml

    base = live_yml if live_yml.is_file() else Path(str(paths.packaged_config() / "settings.yml"))
    flat: dict[str, object] = {}
    try:
        tree = yaml.safe_load(base.read_text(encoding="utf-8")) if base.is_file() else {}
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        tree = {}
    if isinstance(tree, dict):
        flat.update(_flat(tree))
    if _has_db_table(live_db, "setting"):
        for leaf, (raw, _u, _k) in _leaf_rows(_setting_rows(live_db)).items():
            flat[leaf] = _decode(raw)
    names: set[str] = set()
    if live is not None:
        for name, b in live.llm.backends.items():
            key = (b.api_key or "").strip()
            if (b.api_key_env or "").strip() or (key and key not in _PLACEHOLDER_KEYS):
                names.add(name)
    for leaf, value in flat.items():
        bf = _backend_field(leaf)
        if not _is_key_field(leaf) or not isinstance(value, str) or not value.strip():
            continue
        if bf and bf[1][0] == "api_key" and value.strip() in _PLACEHOLDER_KEYS:
            continue
        if bf:
            names.add(bf[0])
    return frozenset(names)


def _put_row(con: sqlite3.Connection, key: str, row: tuple[str, str]) -> None:
    con.execute("DELETE FROM setting WHERE key = ?", (key,))
    con.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?)", (key, row[0], row[1])
    )


def _has_db_table(db: Path, name: str) -> bool:
    if not db.exists() or not db.stat().st_size:
        return False
    con = sqlite3.connect(str(db), timeout=30)
    try:
        return con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone() is not None
    finally:
        con.close()


def _setting_rows(db: Path) -> dict[str, tuple[str, str]]:
    con = sqlite3.connect(str(db), timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        return {
            k: (v, u) for k, v, u in con.execute("SELECT key, value, updated_at FROM setting")
        }
    finally:
        con.close()


_ABSENT = object()


def _flat(value: object, prefix: str = "") -> dict[str, object]:
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for k, v in value.items():
            out.update(_flat(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    return {prefix: value}


def _merge_local(
    path: str, theirs: object, mine: object, counter: list[int], opt_in: bool = False,
    keyed: frozenset[str] = frozenset(),
) -> object:
    """The backup's settings tree with every local-only part taken from this computer's.
    COUNTER[0] counts the keys kept; COUNTER[1], when present, the keyed backend addresses kept."""
    if path == "confidential_mode":
        if mine is True and theirs is not True:
            counter[0] += 1
            return True
        return theirs
    if path and _stays_here(path, opt_in, keyed):
        a = _flat(theirs) if theirs is not _ABSENT else {}
        b = _flat(mine) if mine is not _ABSENT else {}
        differ = sum(1 for k in set(a) | set(b) if a.get(k, _ABSENT) != b.get(k, _ABSENT))
        counter[0] += differ
        if opt_in and differ and theirs is not _ABSENT and _is_backend_url(path) \
                and len(counter) > 1:
            counter[1] += 1
        return mine
    if opt_in and theirs is _ABSENT and mine is not _ABSENT and path and (
        _within_local(path) or _within_local(path + ".x")
    ):
        # A key the backup's settings.yml does not mention is not a choice to remove it: the
        # ticked restore keeps this computer's (removing a tier's required fields would stop
        # every settings load).
        return mine
    if isinstance(theirs, dict) or isinstance(mine, dict):
        t = theirs if isinstance(theirs, dict) else {}
        m = mine if isinstance(mine, dict) else {}
        out: dict = {}
        for k in list(t) + [k for k in m if k not in t]:
            sub = f"{path}.{k}" if path else str(k)
            got = _merge_local(
                sub, t.get(k, _ABSENT), m.get(k, _ABSENT), counter, opt_in, keyed
            )
            if got is not _ABSENT:
                out[k] = got
        return out
    return theirs


def _restore_settings_yml(
    staged: Path, live: Path, opt_in: bool = False, keyed: frozenset[str] = frozenset(),
    counts: dict[str, int] | None = None,
) -> int:
    """Install the backup's settings.yml with this computer's local-only keys kept (D-SEC-10).
    The packaged default stands in for a missing live file. A backup file that is not a mapping
    never replaces the live one. A backup that differs from here in no local-only key is copied
    verbatim (comments survive); otherwise the merged mapping is written. Returns the number of
    local-only keys kept."""
    import yaml

    base = live if live.is_file() else Path(str(paths.packaged_config() / "settings.yml"))
    try:
        theirs = yaml.safe_load(staged.read_text(encoding="utf-8")) or {}
        mine = (yaml.safe_load(base.read_text(encoding="utf-8")) or {}) if base.is_file() else {}
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return 1 if live.is_file() else 0
    if not isinstance(theirs, dict) or not isinstance(mine, dict):
        return 1 if live.is_file() else 0
    dropped = _clean_backends(_backends_in("", theirs))
    if counts is not None:
        counts["backends_dropped"] = counts.get("backends_dropped", 0) + dropped
    if opt_in:
        _add_packaged_backends(theirs, mine)
    counter = [0, 0]
    merged = _merge_local("", theirs, mine, counter, opt_in, keyed)
    if counts is not None:
        counts["addresses"] = counts.get("addresses", 0) + counter[1]
    live.parent.mkdir(parents=True, exist_ok=True)
    if counter[0] == 0 and merged == theirs and not dropped:
        shutil.copy2(staged, live)
    else:
        live.write_text(
            yaml.safe_dump(merged, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
    return counter[0]


def _add_packaged_backends(theirs: dict, mine: dict) -> None:
    """D-SEC-19: a backend the backup's settings.yml names that this computer gets from the
    packaged defaults (an older settings.yml lacks it) is merged onto the packaged entry, so the
    fields kept from here (address, key, privacy) are the packaged ones, never left out: a partial
    entry would hide the packaged backend and its key. MINE gains the packaged entry for the merge
    only."""
    import yaml

    names = _backends_in("", theirs)
    if not isinstance(names, dict) or not names:
        return
    try:
        packaged = yaml.safe_load(
            (paths.packaged_config() / "settings.yml").read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return
    shipped = _backends_in("", packaged)
    if not isinstance(shipped, dict):
        return
    llm = mine.setdefault("llm", {})
    if not isinstance(llm, dict):
        return
    have = llm.setdefault("backends", {})
    if not isinstance(have, dict):
        return
    for name in names:
        if name not in have and isinstance(shipped.get(name), dict):
            have[name] = shipped[name]


def _restorable_document(rel: Path) -> Path | None:
    """Where a documents/ entry of the backup may land, or None. Only a generated PDF directly in
    one of the document folders (DOC_SUBDIRS) is ever restored, into exactly that folder: nothing
    can reach kestrel.db, settings, ml/ or any other file under the data folder (D-SEC-10)."""
    if len(rel.parts) != 2 or rel.parts[0] not in DOC_SUBDIRS:
        return None
    if rel.suffix.lower() != ".pdf" or rel.name.startswith("."):
        return None
    folder = paths.data_dir() / rel.parts[0]
    target = paths.confined_file(folder / rel.name, folder)
    # A document folder that is itself a link leading out resolves "inside" its own target, so the
    # file must also sit directly in the folder as it resolves from the data folder.
    expected = paths.data_dir().resolve() / rel.parts[0]
    if target is None or os.path.normcase(str(target.parent)) != os.path.normcase(str(expected)):
        return None
    return target


def _backup_current_db(db: Path) -> Path | None:
    """Snapshot the CURRENT db aside before restore writes anything, so a bad restore is always
    recoverable. Returns the path of the copy, or None if there was no database to preserve.

    Each restore gets its own timestamped copy: a fixed `kestrel.db.pre-restore` name meant every
    attempt (including a failed one) overwrote the safety copy of the attempt before it. The copy
    is taken through SQLite's backup API (`snapshot_database`), so it is one consistent file
    including whatever still sat in the WAL, not a main file plus side files copied at different
    moments while the app was writing."""
    if not db.exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    pre = db.with_name(f"{db.name}.pre-restore-{stamp}")
    snapshot_database(db, pre)
    # Each snapshot is a full copy of the operator's database, so they are not left to pile up:
    # the newest PRE_RESTORE_KEEP stay (the timestamp sorts by name), older ones go.
    snapshots = sorted(db.parent.glob(f"{db.name}.pre-restore-*"))
    for old in snapshots[:-PRE_RESTORE_KEEP]:
        try:
            old.unlink()
        except OSError as exc:  # a locked old copy is not worth failing a restore over
            log.warning("pre_restore_prune_failed", path=old.name, error=str(exc))
    return pre


def _write_into_live(staged: Path, db: Path) -> None:
    """Replace the live database's contents with the staged one, THROUGH SQLite.

    The previous restore deleted kestrel.db-wal/-shm and file-copied the staged db over kestrel.db.
    Inside the running app that can never work on Windows: the worker, scheduler and delivery loops
    hold pooled connections for the life of the process, and Windows refuses to delete or overwrite
    a file another handle has open (WinError 32) or has byte-range locked (WinError 33). Reproduced
    13 times out of 13 through the running app, idle or under load (R1).

    SQLite's online backup API writes every page of the staged database into the live one as an
    ordinary write transaction. SQLite does the locking, so other connections may stay open; each
    sees the restored data from its next transaction. Nothing is unlinked or overwritten on disk.
    A staged -wal shipped in the archive sits beside the staged db and is applied when it is opened.
    """
    source = sqlite3.connect(str(staged))
    try:
        target = sqlite3.connect(str(db), timeout=30)
        try:
            target.execute("PRAGMA busy_timeout=30000")
            src_page = source.execute("PRAGMA page_size").fetchone()[0]
            dst_page = target.execute("PRAGMA page_size").fetchone()[0]
            if db.stat().st_size and src_page != dst_page:
                # A WAL database cannot change page size while other connections are open, and
                # the backup would fail part way. Every Kestrel database uses SQLite's default, so
                # this only refuses a database that did not come from Kestrel.
                log.warning(
                    "restore_refused", why="page_size", backup_page=src_page, this_page=dst_page
                )
                raise InvalidBackupError(PAGE_SIZE_DIFFERS)
            source.backup(target)
            # Best effort: fold the restored pages back into the main file now. PASSIVE never waits
            # on a reader, so an open read transaction elsewhere cannot stall the restore.
            try:
                target.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass
        finally:
            target.close()
    finally:
        source.close()


def _read_lan_key(db: Path) -> tuple[str, str] | None:
    """This machine's LAN shared key row (raw JSON value, updated_at), read before a restore
    writes the database, or None when there is no database or no key."""
    if not db.exists() or not db.stat().st_size:
        return None
    con = sqlite3.connect(str(db), timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        if not _has_setting_table(con):
            return None
        row = con.execute(
            "SELECT value, updated_at FROM setting WHERE key = ?", (_lan_setting_key(),)
        ).fetchone()
    finally:
        con.close()
    return None if row is None else (row[0], row[1])


def _put_back_lan_key(db: Path, saved: tuple[str, str] | None) -> None:
    """After the restore wrote the live database: whatever key the backup carried (an older
    backup may hold another machine's) is removed, and this machine's key, if it had one, is put
    back exactly as it was. Runs before the caller's migrations, which never touch this row."""
    con = sqlite3.connect(str(db), timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        if not _has_setting_table(con):
            return
        key = _lan_setting_key()
        with con:
            con.execute("DELETE FROM setting WHERE key = ?", (key,))
            if saved is not None:
                con.execute(
                    "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, saved[0], saved[1]),
                )
    finally:
        con.close()


def check_backup(path: str | Path) -> None:
    """Refuse a backup the restore would refuse, BEFORE anything is stopped (D-QUEUE-10).

    The Settings restore stops the worker and the online lane before it restores; a refusal found
    only after that stop handed back every lease in flight for nothing. This runs the same checks
    the restore runs before it writes (the archive, every entry the restore would read, read to
    its end, the database, bringing an older one up to date on a private copy, and the import into
    a database built from Kestrel's own migrations), all in a temporary folder, and raises the
    same InvalidBackupError. Nothing real is read or written.

    INVARIANT (D-SEC-30): nothing is stopped, released or written until every entry of the backup
    has been read and validated; a backup that fails any read is refused before the loops stop.
    The restore checks everything again itself; this only decides whether to start."""
    src = Path(path)
    if not src.is_file():
        log.warning("restore_backup_missing", path=str(src))
        raise InvalidBackupError(BACKUP_NOT_FOUND)
    with tempfile.TemporaryDirectory(prefix="kestrel-check-") as tmp_str:
        tmp = Path(tmp_str)
        staging = tmp / "staged"
        staging.mkdir()
        _stage_backup(src, staging, database_only=True)
        fresh = tmp / "fresh" / DB_NAME
        fresh.parent.mkdir()
        from kestrel.db import migrate

        migrate.run_migrations(fresh)
        work, _ignored = _work_copy(staging / DB_NAME, tmp / "work", _schema_names(fresh))
        _import_rows(work, fresh)


def restore_backup(path: str | Path, *, restore_local_settings: bool = False) -> RestoreResult:
    """Restore a Kestrel backup zip into the live data directory, as an IMPORT (D-SEC-20).

    INVARIANT: nothing in a backup can change how this computer runs (local-only settings, keys,
    consent, schema objects, queued work, model files) beyond the data rows a person would expect
    to get back.

    Order: (1) stage + validate the archive in a temp directory -- a bad zip, an unsafe path, an
    oversized archive or a kestrel.db that is not a usable Kestrel database is refused with a clear
    message before anything real is touched; (2) make a private work copy of the staged database
    (triggers and views dropped, an older schema migrated to head) that is then only read; (3)
    build a NEW database in the data folder from Kestrel's own migrations and copy the allow-listed
    tables' rows into it, column by column of our schema; (4) apply the local-only rules to that
    new database and drop the queued-work markers; (5) snapshot the CURRENT database aside, then
    write the new one into the live one through SQLite and copy the config and documents. A refusal
    in steps 1 to 4 changes nothing real.

    The database is written through SQLite (`_write_into_live`), so this is safe while other
    connections are open. Work that started against the OLD data must not write into the restored
    data, so the web route stops the worker and the online lane first (D-SEC-20).

    `restore_local_settings` (D-SEC-11, default False) is the person's choice, made on the
    confirmation after seeing the values: the backup's AI and connection settings (model servers,
    online service and model per tier, MCP, notification and search destinations, update feed,
    autostart and the like) are restored too, which is what moving to a new computer needs. The
    consent state (Hosted mode switch and accepted disclosure), the listening switches, this
    computer's LAN key and the rule that a backup never turns confidential mode OFF hold either
    way, and no queued work is ever restored.
    """
    src = Path(path)
    if not src.is_file():
        # The message reaches a page; the path goes to the log only (D-SEC-1).
        log.warning("restore_backup_missing", path=str(src))
        raise InvalidBackupError(BACKUP_NOT_FOUND)

    with tempfile.TemporaryDirectory(prefix="kestrel-restore-") as tmp_str:
        staging = Path(tmp_str) / "staged"
        staging.mkdir()
        # raises InvalidBackupError; nothing real touched yet
        config_skipped = _stage_backup(src, staging)

        paths.ensure_dirs()
        db = paths.db_path()
        result = RestoreResult(config_skipped=config_skipped)
        # D-SEC-16: which backends carry a key saved here, read before anything is written, so
        # a ticked restore keeps their address from this computer in both the database and
        # settings.yml.
        # D-SEC-18: both are read from this computer's EFFECTIVE settings (settings.yml, the
        # packaged backends an older settings.yml lacks, the environment and the rows).
        live = _live_settings(db)
        live_yml = paths.config_dir() / SETTINGS_FILE
        # D-SEC-19, fail closed: when this computer's settings cannot be loaded, nothing local-only
        # comes from the backup whatever the box says, and confidential mode counts as on unless
        # the raw settings say off.
        opt_in = restore_local_settings and live is not None
        result.local_settings_refused = restore_local_settings and live is None
        keyed = _keyed_backends(db, live_yml, live) if opt_in else frozenset()
        live_confidential = (
            bool(live.confidential_mode) if live is not None else _raw_confidential(db, live_yml)
        )
        counts: dict[str, int] = {}

        staged_db = staging / DB_NAME
        result.wal_restored = (staging / DB_SIDE_FILES[0]).exists()
        result.shm_restored = (staging / DB_SIDE_FILES[1]).exists()

        # The new database: in the data folder (the swap below reads it from the same disk), from
        # Kestrel's own migrations, the path a fresh install takes; never the backup's schema.
        for stale in db.parent.glob(f"{db.name}.restore-*"):
            _discard_db(stale)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        fresh = db.with_name(f"{db.name}.restore-{stamp}")
        try:
            from kestrel.db import migrate

            migrate.run_migrations(fresh)
            work, result.schema_ignored = _work_copy(
                staged_db, Path(tmp_str) / "work", _schema_names(fresh)
            )
            imported = _import_rows(work, fresh)
            result.tables_ignored = imported.tables_ignored
            # D-SEC-20 amendment: finished follow-up drafts come back as inert history.
            drafts = _import_follow_up_drafts(work, fresh)
            result.tasks_not_restored = max(imported.tasks_left - drafts, 0)
            # A stored PDF path the confinement check refuses (issue #150) is cleared, and every
            # learned reranker is turned off with its file paths cleared (issue #173), in the new
            # database, so the live rows never hold either.
            result.paths_cleared = _scrub_stored_paths(fresh)
            result.rerankers_disabled = _deactivate_rerankers(fresh)
            # D-SEC-10: the model route, keys and server switches stay this computer's.
            result.settings_kept = _keep_local_settings(
                fresh, db, opt_in, keyed, counts, live_confidential
            )
            result.local_settings_restored = opt_in
            _drop_markers(fresh)
            # This machine's LAN key, never the backup's.
            _put_back_lan_key(fresh, _read_lan_key(db))
            migrate.stamp_saved_by(fresh)
            _checkpoint(fresh)
            _fsync(fresh)
            result.pre_restore_backup = _backup_current_db(db)
            _write_into_live(fresh, db)
            result.db_restored = True
        finally:
            _discard_db(fresh)

        # D-SEC-23: from here the database is restored. A failure copying the config files or
        # the documents must not report "Restore failed" (the caller would skip resetting its
        # caches over a database that WAS replaced): each part is tried on its own, and one that
        # fails is logged and named in the result.
        try:
            _restore_config_files(staging, result, opt_in, keyed, counts)
        except Exception as exc:  # noqa: BLE001 - reported as a part not restored, never raised
            log.warning("restore_part_failed", part="config", error_type=type(exc).__name__,
                        error=str(exc))
            result.parts_failed.append("config files")
        result.addresses_kept = counts.get("addresses", 0)
        result.backends_dropped = counts.get("backends_dropped", 0)
        try:
            _restore_documents(staging, result)
        except Exception as exc:  # noqa: BLE001 - reported as a part not restored, never raised
            log.warning("restore_part_failed", part="documents", error_type=type(exc).__name__,
                        error=str(exc))
            result.parts_failed.append("generated documents")

    return result


def _restore_config_files(
    staging: Path, result: RestoreResult, opt_in: bool, keyed: frozenset[str],
    counts: dict[str, int],
) -> None:
    """Install the staged config files under the names Kestrel uses (settings.yml through the
    local-only merge). Runs after the database write (D-SEC-23)."""
    staged_cfg = staging / CONFIG_PREFIX.rstrip("/")
    if staged_cfg.exists():
        cfg_dir = paths.config_dir()
        cfg_dir.mkdir(parents=True, exist_ok=True)
        for name in CONFIG_FILES:  # staged under these names only (config_plan)
            f = staged_cfg / name
            if not f.is_file():
                continue
            target = paths.confined_file(cfg_dir / name, cfg_dir)
            if target is None:
                result.entries_refused += 1
                continue
            if name == SETTINGS_FILE:
                result.settings_kept += _restore_settings_yml(
                    f, target, opt_in, keyed, counts
                )
            else:
                shutil.copy2(f, target)
            result.config_files.append(name)


def _restore_documents(staging: Path, result: RestoreResult) -> None:
    """Copy the staged generated documents into the data folder. Runs after the database write
    (D-SEC-23)."""
    staged_docs = staging / DOCS_PREFIX.rstrip("/")
    if staged_docs.exists():
        for f in sorted(staged_docs.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(staged_docs)
            target = _restorable_document(rel)
            if target is None:
                log.warning("restore_entry_skipped", why="not_a_document")
                result.entries_refused += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            result.document_files.append(rel.as_posix())


def _schema_names(db: Path) -> frozenset[str]:
    """The trigger and view names Kestrel's own migrations create (read from a database built from
    them)."""
    con = sqlite3.connect(str(db), timeout=30)
    try:
        return frozenset(
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('trigger', 'view')"
            )
        )
    finally:
        con.close()


def _checkpoint(db: Path) -> None:
    """Fold the new database's write-ahead log into its main file before it is synced and read."""
    con = sqlite3.connect(str(db), timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
