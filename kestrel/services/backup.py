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
  2. The CURRENT database (+ -wal/-shm) is copied aside to kestrel.db.pre-restore before anything
     is overwritten, so a bad restore is always recoverable.
  3. Only then are the db/config/documents replaced from the staged copy.
Nothing under data_home() other than the database, config, and generated documents is ever touched
(most importantly: .env is never read, written, or included in a backup -- secrets never leave the
machine this way, hard rule)."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from kestrel import paths

DB_NAME = "kestrel.db"
DB_SIDE_FILES = (f"{DB_NAME}-wal", f"{DB_NAME}-shm")
CONFIG_PREFIX = "config/"
DOCS_PREFIX = "documents/"
ML_PREFIX = "ml/"
# Subdirectories of data_dir() holding generated PDFs, each mapped under DOCS_PREFIX in the zip.
DOC_SUBDIRS = ("documents", "prep_packs")


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
            lines.append("database WAL restored")
        if self.shm_restored:
            lines.append("database SHM restored")
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
        for info in members:
            dest = staging / info.filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as fsrc, open(dest, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)


def _backup_current_db(db: Path) -> Path | None:
    """Copy the CURRENT db (+ wal/shm) aside before restore overwrites anything, so a bad restore
    is always recoverable. Returns the path of the .pre-restore copy, or None if there was no
    existing database to preserve."""
    if not db.exists():
        return None
    pre = db.with_name(db.name + ".pre-restore")
    shutil.copy2(db, pre)
    for suffix in ("-wal", "-shm"):
        side = Path(f"{db}{suffix}")
        if side.exists():
            shutil.copy2(side, Path(f"{pre}{suffix}"))
    return pre


def restore_backup(path: str | Path) -> RestoreResult:
    """Restore a Kestrel backup zip into the live data directory.

    Safety order: (1) stage + validate the archive in a temp directory -- a bad zip is refused with
    a clear message before anything real is touched; (2) copy the CURRENT database aside to
    kestrel.db.pre-restore (+ -wal/-shm); (3) only then replace the db/config/documents from the
    staged copy. The data directory is never left half-written: any failure in step 1 happens
    entirely outside it, and step 2 guarantees step 3's overwrite is reversible.
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

        # A restored db that predates the current WAL/SHM must not inherit stale side files that
        # do not belong to it, unless the archive supplies its own (copied in below).
        for suffix in ("-wal", "-shm"):
            stale = Path(f"{db}{suffix}")
            if stale.exists():
                stale.unlink()

        staged_db = staging / DB_NAME
        if staged_db.exists():
            shutil.copy2(staged_db, db)
            result.db_restored = True
        for name, suffix, flag in ((DB_SIDE_FILES[0], "-wal", "wal_restored"),
                                    (DB_SIDE_FILES[1], "-shm", "shm_restored")):
            staged_side = staging / name
            if staged_side.exists():
                shutil.copy2(staged_side, Path(f"{db}{suffix}"))
                setattr(result, flag, True)

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
