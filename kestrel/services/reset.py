"""Start over from nothing (D-UI-27).

Two callers: the onboarding restart's "clear everything" choice, and `kestrel-cli reset` for
the operator validating a build from a truly fresh state. Both must leave the machine looking
like a first install, and both take a backup zip first so a reset is never the last word.

What goes: the database (opportunities, pursuits, documents, sources, settings rows), the
operator's config (profile.yml and the seeded YAML), logs, renders and every other file under
the data home. What can stay: `.env`, which holds API keys; `keep_keys=True` keeps it, because
re-entering a key is the one step a returning user should not have to repeat. The backup itself
is written OUTSIDE the data home, so the reset cannot delete it.

`reset_everything` is the CLI's file-level reset: the app must not be running (the database
file is open), and the CLI checks the instance lock. `reset_in_app` is the onboarding restart's
"clear everything" (D-UI-31): the app IS running, so instead of deleting the database file it
pauses the worker, empties every table in one transaction, clears the operator's files and the
in-process caches, re-seeds the shipped config, sources and layouts, and resumes the worker.

What `reset_in_app` keeps, on purpose: the machine's model setup (hardware_profile.json, the
rendered Modelfiles and the last doctor report), because the models are still in Ollama and
rebuilding them would cost the operator a long download for nothing; and the logs. The run mode
and hosted assignments are setting rows, so they go, and the onboarding asks again."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from kestrel import paths

log = structlog.get_logger()


@dataclass
class ResetResult:
    backup: Path | None
    removed: list[str]
    kept: list[str]
    failed: list[str] = field(default_factory=list)  # files that could not be removed


class ResetBusy(RuntimeError):
    """A worker cycle is still running; nothing was deleted."""


def backup_dir() -> Path:
    """Next to the data home, never inside it."""
    return paths.data_home().parent / "Kestrel-backups"


def reset_everything(*, keep_keys: bool = True, backup: bool = True) -> ResetResult:
    from kestrel.services.backup import create_backup

    home = paths.data_home()
    zip_path: Path | None = None
    if backup and home.exists():
        out = backup_dir()
        out.mkdir(parents=True, exist_ok=True)
        try:
            zip_path = create_backup(out)
        except Exception as exc:  # no backup, no reset: the operator's data is not ours to lose
            raise RuntimeError(f"Backup before reset failed, nothing was deleted: {exc}") from exc

    removed: list[str] = []
    kept: list[str] = []
    if home.exists():
        env = paths.dotenv_path()
        # The database folder first: if anything still holds kestrel.db open, the reset stops
        # there with the config and keys untouched, instead of half way (review finding 8).
        for entry in sorted(home.iterdir(), key=lambda e: (e.name != "data", e.name)):
            if keep_keys and entry.resolve() == Path(env).resolve():
                kept.append(entry.name)
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink(missing_ok=True)
            removed.append(entry.name)
    log.info("reset_everything", removed=removed, kept=kept, backup=str(zip_path or ""))
    return ResetResult(backup=zip_path, removed=removed, kept=kept)


IDLE_WAIT_S = 90.0

# Files under the config dir that are the operator's or seeded from the package. The machine's
# hardware_profile.json and models/ (rendered Modelfiles) are not in this list and stay.
_CONFIG_SUFFIXES = (".yml", ".yaml")
_CONFIG_EXTRA = ("profile.yml.bak", "profile.yml.editing", "profile.yml.incoming")
# Generated output under the data dir: renders, prep packs, the reranker and its PCA.
_DATA_SUBDIRS = ("documents", "prep_packs", "ml")


def _clear_files(keep_keys: bool) -> tuple[list[str], list[str], list[str]]:
    """Remove the operator's files. Never raises for one file: a PDF open in a viewer on Windows
    cannot be deleted, and one locked file must not leave the app half reset (review finding
    5). Returns removed, kept, and the ones that could not be removed."""
    removed: list[str] = []
    kept: list[str] = []
    failed: list[str] = []

    def _gone(label: str, fn) -> None:
        try:
            fn()
            removed.append(label)
        except OSError as exc:
            log.warning("reset_file_kept", path=label, error=str(exc))
            failed.append(label)

    cfg = paths.config_dir()
    if cfg.exists():
        for f in sorted(cfg.iterdir()):
            if f.is_file() and (f.suffix in _CONFIG_SUFFIXES or f.name in _CONFIG_EXTRA):
                _gone(f"config/{f.name}", lambda f=f: f.unlink(missing_ok=True))
            else:
                kept.append(f"config/{f.name}")
    for sub in _DATA_SUBDIRS:
        d = paths.data_dir() / sub
        if d.exists():
            _gone(f"data/{sub}", lambda d=d: shutil.rmtree(d))
    # Full copies of the database that restore leaves beside it (timestamped, or the older fixed
    # name). A reset that promises to delete every opportunity and pursuit must not leave them; the
    # backup it wrote first, outside the data folder, is the recovery path.
    db = paths.db_path()
    if db.parent.exists():
        for f in sorted(db.parent.glob(f"{db.name}.pre-restore*")):
            if f.is_file():
                _gone(f"data/{f.name}", lambda f=f: f.unlink(missing_ok=True))
    env = paths.dotenv_path()
    if env.exists():
        if keep_keys:
            kept.append(env.name)
        else:
            _gone(env.name, env.unlink)
    return removed, kept, failed


def _forget_caches() -> None:
    """Process-global caches that would otherwise answer from the data just deleted."""
    from kestrel.config import load_dotenv_into_environ
    from kestrel.services import hosted_models, setup_state, tracks
    from kestrel.web.deps import set_ui_scheme_cache
    from kestrel.web.routes_control import reset_status_caches

    load_dotenv_into_environ()  # a removed .env empties the key store
    tracks.forget_cache()
    set_ui_scheme_cache(None)
    setup_state.forget_ollama_probe()
    reset_status_caches()
    hosted_models.clear_cache()  # a model list fetched with a key that no longer exists


async def _empty_tables(sessionmaker) -> int:
    """Delete every row of every table except alembic_version, children first, in one
    transaction: either all of it goes or none of it does."""
    from kestrel.db import models as _models  # noqa: F401  (registers every table)
    from kestrel.db.base import Base

    tables = list(reversed(Base.metadata.sorted_tables))
    async with sessionmaker() as s:
        for table in tables:
            await s.execute(table.delete())
        await s.commit()
    return len(tables)


async def reset_in_app(sessionmaker, *, keep_keys: bool = True) -> ResetResult:
    """Clear everything while the app runs (D-UI-31). Backup first, outside the data home, and
    no backup means no reset. The worker is paused for the duration and put back the way it was;
    a unit of work already in flight when the rows go fails on its own and is logged, which is
    harmless because there is nothing left for it to finish."""
    from kestrel.services.backup import create_backup
    from kestrel.web import app as app_mod

    out = backup_dir()
    try:
        out.mkdir(parents=True, exist_ok=True)
        zip_path = await asyncio.to_thread(create_backup, out)
    except Exception as exc:  # no backup, no reset
        raise RuntimeError(f"Backup before reset failed, nothing was deleted: {exc}") from exc

    lock = app_mod.maintenance_lock()
    if lock.locked():
        raise ResetBusy("A restore or reset is already running. Nothing was deleted.")
    removed: list[str] = []
    kept: list[str] = []
    failed: list[str] = []
    # A counted hold, not "remember whether it was paused and put that back": an overlapping
    # restore, or a tray Resume, used to unpause the worker while the tables were being emptied.
    async with lock, app_mod.worker_held():
        try:
            # A cycle that started before the pause keeps writing until it ends (review finding
            # 7). Wait for it; if it is still running after a minute and a half, refuse with
            # nothing deleted rather than empty the tables under it.
            if not await app_mod.wait_for_idle_cycle(IDLE_WAIT_S):
                raise ResetBusy(
                    "Kestrel is in the middle of a run. Nothing was deleted. Try again in a minute."
                )
            n_tables = await _empty_tables(sessionmaker)
            removed.append(f"{n_tables} tables emptied")
            r, k, f = await asyncio.to_thread(_clear_files, keep_keys)
            removed += r
            kept += k
            failed += f
        finally:
            # Whatever happened after the backup, the app is put back into a working state: the
            # seeded config, sources, layouts and fresh caches (review finding 5).
            await asyncio.to_thread(paths.seed_config)
            _forget_caches()
            await app_mod._seed_shipped_sources()
            await app_mod._seed_shipped_layouts()
            await app_mod._load_enabled_tracks()
    log.info("reset_in_app", removed=removed, kept=kept, failed=failed, backup=str(zip_path))
    return ResetResult(backup=zip_path, removed=removed, kept=kept, failed=failed)
