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

Restore is safety-first (CLAUDE.md "resumability is not optional" + the explicit ask here):
  1. The archive is fully validated and extracted to a TEMP staging directory first. A truncated
     zip, an unsafe path entry, or a missing kestrel.db is caught here -- the real data directory
     is never touched until staging succeeds.
  2. The CURRENT database is snapshotted to a timestamped kestrel.db.pre-restore-<UTC> file before
     anything is written, so a bad restore is always recoverable.
  3. Only then is the staged db written into the live one through SQLite (safe with the app's
     connections open) and the config and documents copied in.
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

import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog

from kestrel import paths

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


class InvalidBackupError(ValueError):
    """A zip that does not look like (or safely extract as) a Kestrel backup. Raised before any
    write to the real data directory, so refusing it leaves nothing touched."""


@dataclass
class RestoreResult:
    db_restored: bool = False
    wal_restored: bool = False
    shm_restored: bool = False
    config_files: list[str] = field(default_factory=list)
    document_files: list[str] = field(default_factory=list)
    pre_restore_backup: Path | None = None

    def summary_lines(self) -> list[str]:
        lines = [f"database restored: {self.db_restored}"]
        if self.wal_restored:
            lines.append("the backup's write-ahead log was applied")
        lines.append(f"{len(self.config_files)} config file(s) restored")
        lines.append(f"{len(self.document_files)} generated document file(s) restored")
        if self.pre_restore_backup is not None:
            lines.append(f"previous database preserved at {self.pre_restore_backup}")
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
        ml = paths.data_dir() / "ml"
        if ml.exists():
            for f in sorted(ml.rglob("*")):
                if f.is_file():
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


class InvalidProfileError(ValueError):
    """An uploaded profile that is not UTF-8 YAML with at least one track block that
    `load_profile` accepts. Raised before (or, for the final load check, rolled back after) any
    change to the real profile.yml, so a refused import leaves the previous profile in place."""


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
        raise InvalidProfileError("The file is not UTF-8 text; profile.yml must be YAML.") from exc
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise InvalidProfileError(f"The file is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise InvalidProfileError("profile.yml must be a YAML mapping with track blocks.")
    present = [t for t in TRACKS if isinstance(parsed.get(t), dict)]
    if not present:
        raise InvalidProfileError(
            "No track block found; profile.yml needs an 'employment:' or 'consulting:' mapping "
            "(see profile.example.yml)."
        )

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
        raise InvalidProfileError(
            "The profile parsed but the loader could not build a track from it; the previous "
            "profile.yml was kept."
        )
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


def _stage_backup(src: Path, staging: Path) -> None:
    """Validate the archive and extract exactly the entries this format defines into `staging`.
    Raises InvalidBackupError on anything short of a well-formed Kestrel backup; nothing outside
    `staging` is touched, so a failure here never affects the real data directory."""
    try:
        zf = zipfile.ZipFile(src)
    except zipfile.BadZipFile as exc:
        raise InvalidBackupError(f"Not a valid zip file: {src}") from exc
    with zf:
        names = set(zf.namelist())
        if DB_NAME not in names:
            raise InvalidBackupError(
                "This does not look like a Kestrel backup (no kestrel.db found in the archive)."
            )
        members = [
            info
            for info in zf.infolist()
            if not info.is_dir()
            and (
                info.filename in (DB_NAME, *DB_SIDE_FILES)
                or info.filename.startswith(CONFIG_PREFIX)
                or info.filename.startswith(DOCS_PREFIX)
            )
        ]
        for info in members:
            if not _is_safe_member(info.filename):
                raise InvalidBackupError(
                    f"Refusing an unsafe path entry in the backup: {info.filename}"
                )
        # Caps BEFORE extracting anything: zipfile reports sizes in the central directory, so a
        # decompression bomb is refused without writing a byte to the staging directory.
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise InvalidBackupError(
                "This archive has more entries than a Kestrel backup can have; "
                "it is not one of ours."
            )
        total = 0
        for info in members:
            if info.file_size > MAX_MEMBER_BYTES:
                raise InvalidBackupError(
                    f"Refusing an oversized entry in the backup: {info.filename} "
                    f"({info.file_size:,} bytes unpacked)"
                )
            total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise InvalidBackupError(
                f"This backup unpacks to {total:,} bytes, more than a Kestrel backup can be. "
                "It is not one of ours."
            )
        for info in members:
            dest = staging / info.filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zf.open(info) as fsrc, open(dest, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst)
            except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
                # An encrypted or otherwise unreadable member is a bad archive, not a 500.
                raise InvalidBackupError(
                    f"Could not read {info.filename} from the backup: {exc}"
                ) from exc

    _validate_staged_db(staging / DB_NAME)


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
        raise InvalidBackupError(
            "The kestrel.db inside this backup could not be opened as a database."
        ) from exc
    try:
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as exc:
            raise InvalidBackupError(
                "The kestrel.db inside this backup is not a usable SQLite database, so "
                "restoring it would destroy the current one. Your database is unchanged."
            ) from exc
        if not row or row[0] != "ok":
            raise InvalidBackupError(
                "The kestrel.db inside this backup failed SQLite's integrity check, so it was not "
                "restored. Your database has not been touched."
            )
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        revision = None
        if "alembic_version" in names:
            row = con.execute("SELECT version_num FROM alembic_version").fetchone()
            revision = row[0] if row else None
    except sqlite3.DatabaseError as exc:
        raise InvalidBackupError(
            "The kestrel.db inside this backup is not a readable database, so it was not restored. "
            "Your database has not been touched."
        ) from exc
    finally:
        con.close()
    # A backup made by a NEWER Kestrel is stamped with a revision this build does not know. It would
    # pass every check above, be written into the live database, and only then fail to migrate,
    # leaving an app that will not start. Refused here, while nothing real has been touched. (If
    # the shipped scripts cannot be read, `known` is None and doctor reports that separately.)
    from kestrel.db import migrate

    known = migrate.known_revisions()
    if revision is not None and known is not None and revision not in known:
        raise InvalidBackupError(
            "This backup was made by a newer version of Kestrel, so this version cannot open it. "
            "Update Kestrel, then restore it again. Your database has not been touched."
        )
    missing = sorted(_REQUIRED_TABLES - names)
    if missing:
        raise InvalidBackupError(
            "The kestrel.db inside this backup is not a Kestrel database (missing tables: "
            f"{', '.join(missing)}), so it was not restored. Your database has not been touched."
        )


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
                raise InvalidBackupError(
                    f"The kestrel.db inside this backup uses a {src_page}-byte page size and this "
                    f"one uses {dst_page}, so it cannot be restored in place. Nothing was changed."
                )
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


def restore_backup(path: str | Path) -> RestoreResult:
    """Restore a Kestrel backup zip into the live data directory.

    Safety order: (1) stage + validate the archive in a temp directory -- a bad zip, an unsafe
    path, an oversized archive or a kestrel.db that is not a usable Kestrel database is refused with
    a clear message before anything real is touched; (2) snapshot the CURRENT database aside to a
    timestamped kestrel.db.pre-restore-<UTC> file; (3) only then write the staged db into the live
    one and copy the config and documents. Any failure in step 1 happens entirely outside the data
    directory, and step 2 makes step 3 reversible.

    The database is written through SQLite (`_write_into_live`), so this is safe while other
    connections are open, which inside the app they always are. What it cannot do is stop work
    that started against the OLD data from writing into the restored data, so the web route pauses
    the worker and waits for its cycle to finish first. The restored database may be on an older
    schema; the caller runs migrations afterwards.
    """
    src = Path(path)
    if not src.is_file():
        raise InvalidBackupError(f"No such backup file: {src}")

    with tempfile.TemporaryDirectory(prefix="kestrel-restore-") as tmp_str:
        staging = Path(tmp_str)
        _stage_backup(src, staging)  # raises InvalidBackupError; nothing real touched yet

        paths.ensure_dirs()
        db = paths.db_path()
        result = RestoreResult(pre_restore_backup=_backup_current_db(db))

        staged_db = staging / DB_NAME
        if staged_db.exists():
            # Recorded before the write: opening the staged db applies (and may then remove) its
            # own -wal/-shm, which travel inside the archive beside it.
            result.wal_restored = (staging / DB_SIDE_FILES[0]).exists()
            result.shm_restored = (staging / DB_SIDE_FILES[1]).exists()
            machine_key = _read_lan_key(db)  # this machine's LAN key wins over the backup's
            _write_into_live(staged_db, db)
            _put_back_lan_key(db, machine_key)
            result.db_restored = True

        staged_cfg = staging / CONFIG_PREFIX.rstrip("/")
        if staged_cfg.exists():
            cfg_dir = paths.config_dir()
            cfg_dir.mkdir(parents=True, exist_ok=True)
            for f in sorted(staged_cfg.rglob("*")):
                if f.is_file():
                    rel = f.relative_to(staged_cfg)
                    target = cfg_dir / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, target)
                    result.config_files.append(rel.as_posix())

        staged_docs = staging / DOCS_PREFIX.rstrip("/")
        if staged_docs.exists():
            for f in sorted(staged_docs.rglob("*")):
                if f.is_file():
                    rel = f.relative_to(staged_docs)
                    target = paths.data_dir() / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, target)
                    result.document_files.append(rel.as_posix())

    return result
