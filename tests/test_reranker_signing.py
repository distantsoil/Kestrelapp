"""Signed reranker files (D-SEC-13): every saved artifact carries an HMAC under a per-install key,
and the loader verifies it before any unpickling. A changed file, a file from another install, an
unsigned legacy row and a missing key are all refused with the notice, and scoring falls back to
the LLM tier (nothing is dropped, hard rule 4). The key is never in a backup or restored."""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
import zipfile
from pathlib import Path

import pytest
import sklearn
from sqlalchemy import select

from kestrel import paths
from kestrel.config import get_settings
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, reset_engine
from kestrel.db.models import MlModel, Notification
from kestrel.learning import reranker as rr
from kestrel.learning import signing
from kestrel.services.backup import create_backup, restore_backup
from tests.learning_fakes import craft_reranker_inert_artifact, seed_dataset
from tests.symlink_probe import link_folder, needs_dir_fd, symlink_or_skip

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


async def _trained(sm) -> MlModel:
    async with sm() as s:
        await seed_dataset(s, n=250, signal=True)
    async with sm() as s:
        result = await rr.train_reranker(s, "employment", get_settings())
        assert result.promoted
        return await s.get(MlModel, result.ml_model_id)


def _append_byte(path: str) -> None:
    with open(path, "ab") as fh:
        fh.write(b"\x00")


def _read(path: str) -> bytes:
    return Path(path).read_bytes()


async def _refusals(s) -> list[str]:
    rows = (
        await s.execute(select(Notification).where(Notification.kind == "reranker_refused"))
    ).scalars()
    return [r.title for r in rows]


async def test_normal_train_save_and_load_works_and_rows_are_signed(sm):
    ml = await _trained(sm)
    assert ml.artifact_signature and ml.pca_artifact_signature
    assert ml.artifact_signature == signing.sign_file(ml.artifact_path)
    async with sm() as s:
        loaded = await rr.load_active_reranker(s, "employment")
        assert loaded is not None and loaded.fitted.pca is not None
        assert await _refusals(s) == []


async def test_a_tampered_file_is_refused(sm):
    ml = await _trained(sm)
    _append_byte(ml.artifact_path)
    async with sm() as s:
        assert await rr.load_active_reranker(s, "employment") is None
        notes = await _refusals(s)
    assert len(notes) == 1 and "signature" in notes[0]


async def test_a_tampered_pca_file_is_refused(sm):
    ml = await _trained(sm)
    craft_reranker_inert_artifact(ml.pca_artifact_path)  # valid pickle, but not what was signed
    async with sm() as s:
        assert await rr.load_active_reranker(s, "employment") is None
        assert len(await _refusals(s)) == 1


async def test_a_file_signed_with_another_installs_key_is_refused(sm):
    ml = await _trained(sm)
    other_key = os.urandom(signing.KEY_BYTES)
    paths.model_signing_key_path().write_bytes(other_key)  # this install now has a different key
    async with sm() as s:
        assert await rr.load_active_reranker(s, "employment") is None
        assert len(await _refusals(s)) == 1
    # Signed under the foreign key, a file verifies only for that key, not for this install's.
    foreign = signing.sign_bytes(_read(ml.artifact_path), os.urandom(32))
    assert signing.read_verified(ml.artifact_path, foreign) is None


async def test_an_unsigned_legacy_row_is_refused_with_the_notice(sm):
    ml = await _trained(sm)
    async with sm() as s:
        row = await s.get(MlModel, ml.id)
        row.artifact_signature = None
        row.pca_artifact_signature = None
        await s.commit()
        assert await rr.load_active_reranker(s, "employment") is None
        notes = await _refusals(s)
    assert len(notes) == 1 and "before model files were signed" in notes[0]
    assert "retrains" in notes[0]


async def test_a_missing_key_refuses_and_does_not_create_one(sm):
    await _trained(sm)
    paths.model_signing_key_path().unlink()
    async with sm() as s:
        assert await rr.load_active_reranker(s, "employment") is None
        assert len(await _refusals(s)) == 1
    assert not paths.model_signing_key_path().exists()


async def test_the_sklearn_version_check_still_works(sm):
    ml = await _trained(sm)
    async with sm() as s:
        row = await s.get(MlModel, ml.id)
        row.sklearn_version = "0.0.0-not-installed"
        await s.commit()
        assert await rr.load_active_reranker(s, "employment") is None
        notes = await _refusals(s)
    assert len(notes) == 1 and "sklearn" in notes[0]
    assert sklearn.__version__ != "0.0.0-not-installed"


async def test_the_calibrate_panel_does_not_call_an_unverifiable_reranker_loadable(sm):
    ml = await _trained(sm)
    async with sm() as s:
        row = await s.get(MlModel, ml.id)
        assert rr._artifact_is_loadable(row) is True
        row.pca_artifact_signature = "0" * 64
        assert rr._artifact_is_loadable(row) is False


async def test_a_refused_row_is_told_once_a_day_not_once_per_load(sm, monkeypatch):
    """The loader runs per Tier 1 task, per drift check and per status read: an upgraded install
    with an unsigned active row gets one notice, not one per load (D-LEARN-10)."""
    from datetime import timedelta

    ml = await _trained(sm)
    async with sm() as s:
        row = await s.get(MlModel, ml.id)
        row.artifact_signature = None
        row.pca_artifact_signature = None
        await s.commit()
    for _ in range(4):
        async with sm() as s:
            assert await rr.load_active_reranker(s, "employment") is None
    async with sm() as s:
        await rr.check_drift(s, "employment", get_settings())
        assert len(await _refusals(s)) == 1
    later = rr._utcnow() + timedelta(days=2)
    monkeypatch.setattr(rr, "_utcnow", lambda: later)
    async with sm() as s:
        assert await rr.load_active_reranker(s, "employment") is None
    async with sm() as s:
        notes = await _refusals(s)
    # The next day the notice is due again (its created_at is real time, so a day later reads
    # the earlier one as old).
    assert len(notes) == 2


async def test_settings_learning_does_not_call_an_unverifiable_reranker_in_use(sm):
    """Settings > Scoring and learning reads the same check as Calibrate (D-SEC-13)."""
    from kestrel.learning import ledger

    ml = await _trained(sm)
    async with sm() as s:
        row = await s.get(MlModel, ml.id)
        assert ledger._artifact_loadable(row) is True
        row.artifact_signature = None
        assert ledger._artifact_loadable(row) is False


def test_the_key_is_32_random_bytes_created_once_and_owner_only():
    first = signing.load_or_create_key()
    assert len(first) == signing.KEY_BYTES
    assert signing.load_or_create_key() == first  # created once, then reused
    path = paths.model_signing_key_path()
    assert path.parent == paths.secrets_dir() and paths.secrets_dir().parent == paths.data_dir()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_the_key_file_is_owner_only_on_posix():
    signing.load_or_create_key()
    assert stat.S_IMODE(paths.model_signing_key_path().stat().st_mode) == 0o600


def _fresh_db() -> None:
    forget_engine()
    for suffix in ("", "-wal", "-shm"):
        p = paths.db_path().with_name(paths.db_path().name + suffix)
        p.unlink(missing_ok=True)
    migrate.run_migrations()


def test_the_key_never_appears_in_a_backup(tmp_path):
    _fresh_db()
    key = signing.load_or_create_key()
    (paths.ml_dir()).mkdir(parents=True, exist_ok=True)
    (paths.ml_dir() / "reranker_x.pkl").write_bytes(b"model")
    (paths.ml_dir() / paths.model_signing_key_path().name).write_bytes(key)  # a stray copy
    archive = create_backup(tmp_path)
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        assert "ml/reranker_x.pkl" in names  # the folder is still backed up
        assert not any("model_signing" in n or n.startswith("secrets") for n in names)
        for n in names:
            if n.endswith(".db"):
                continue
            assert key not in z.read(n)


def test_a_backup_containing_a_key_file_does_not_install_it(tmp_path):
    _fresh_db()
    mine = signing.load_or_create_key()
    archive = create_backup(tmp_path)
    planted = os.urandom(32)
    crafted = tmp_path / "crafted.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(crafted, "w") as dst:
        for info in src.infolist():
            dst.writestr(info.filename, src.read(info.filename))
        dst.writestr("secrets/model_signing.key", planted)
        dst.writestr("ml/model_signing.key", planted)
        dst.writestr("documents/secrets/model_signing.key", planted)
    restore_backup(crafted)
    assert paths.model_signing_key_path().read_bytes() == mine  # this computer's key is unchanged
    assert not (paths.ml_dir() / "model_signing.key").exists()
    con = sqlite3.connect(str(paths.db_path()))
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()


async def test_a_restore_without_a_local_key_does_not_adopt_the_backups(tmp_path):
    await reset_engine()
    _fresh_db()
    signing.load_or_create_key()
    archive = create_backup(tmp_path)
    paths.model_signing_key_path().unlink()
    crafted = tmp_path / "c.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(crafted, "w") as dst:
        for info in src.infolist():
            dst.writestr(info.filename, src.read(info.filename))
        dst.writestr("secrets/model_signing.key", os.urandom(32))
    restore_backup(crafted)
    assert not paths.model_signing_key_path().exists()


def test_the_signature_column_pair_exists_after_migration():
    _fresh_db()
    con = sqlite3.connect(str(paths.db_path()))
    cols = {r[1] for r in con.execute("PRAGMA table_info(ml_model)")}
    con.close()
    assert {"artifact_signature", "pca_artifact_signature"} <= cols


def _build_on(monkeypatch):
    from unittest.mock import AsyncMock

    from kestrel.learning import training

    settings = get_settings().model_copy(deep=True)
    settings.learning.build_model = True
    monkeypatch.setattr(training, "load_settings_with_db", AsyncMock(return_value=settings))
    return training


async def test_a_verified_active_model_is_in_use_so_nothing_is_queued(sm, monkeypatch):
    training = _build_on(monkeypatch)
    await _trained(sm)
    async with sm() as s:
        assert "already in use" in await training.request_training(s, "employment",
                                                                   automatic=True)


async def test_a_refused_model_retrains_on_its_own_once_then_waits_like_any_other(sm,
                                                                                  monkeypatch):
    # The notice says scoring uses the LLM tier "until it retrains": the automatic path must
    # queue that retrain (it used to stop at "already in use" because the refused row is active),
    # and a retrain that is not promoted must not repeat every cycle.
    from kestrel.db.models import Task

    training = _build_on(monkeypatch)
    ml = await _trained(sm)
    async with sm() as s:
        row = await s.get(MlModel, ml.id)
        row.artifact_signature = None
        await s.commit()
        assert "queued" in await training.request_training(s, "employment", automatic=True)
        task = (await s.execute(select(Task).where(Task.type == training.TASK_TYPE))).scalar_one()
        assert task.payload["replaces_refused"] == ml.id
        task.status = "done"  # the retrain ran and was not promoted: the old row stays active
        await s.commit()
        assert "Waiting for 50 more" in await training.request_training(s, "employment",
                                                                        automatic=True)


async def test_a_refused_model_skips_the_wait_once_ever_not_after_every_ordinary_retrain(
        sm, monkeypatch):
    # The skip is once per refused model row, ever: an ordinary +50 retrain that carries no
    # marker must not re-arm it, or a refused row that stays active retrains twice per cycle.
    from kestrel.db.models import Task

    training = _build_on(monkeypatch)
    ml = await _trained(sm)
    async with sm() as s:
        row = await s.get(MlModel, ml.id)
        row.artifact_signature = None
        await s.commit()
        assert "queued" in await training.request_training(s, "employment", automatic=True)
        marker = (await s.execute(select(Task).where(Task.type == training.TASK_TYPE))
                  ).scalar_one()
        assert marker.payload["replaces_refused"] == ml.id
        marker.status = "done"  # not promoted: the refused row stays active
        await s.commit()
    async with sm() as s:
        await seed_dataset(s, n=50, seed=7, base_id_tag="more")
    async with sm() as s:
        assert "queued" in await training.request_training(s, "employment", automatic=True)
        tasks = (await s.execute(select(Task).where(Task.type == training.TASK_TYPE)
                                 .order_by(Task.id))).scalars().all()
        assert len(tasks) == 2 and "replaces_refused" not in tasks[1].payload
        tasks[1].status = "done"  # the +50 retrain was not promoted either
        await s.commit()
        assert "Waiting for 50 more" in await training.request_training(s, "employment",
                                                                        automatic=True)
        count = (await s.execute(select(Task).where(Task.type == training.TASK_TYPE))
                 ).scalars().all()
        assert len(count) == 2


class _Fitted:
    """A picklable stand-in for FittedFeatures: _save_model reads only feature_names()."""

    def feature_names(self) -> list[str]:
        return ["f0"]


def _data_files() -> list[Path]:
    return sorted(p for p in paths.data_dir().rglob("*") if p.is_file())


def _escaped() -> list[Path]:
    return list(paths.data_dir().rglob("escape*"))


def _make_ml_subfolders(*names: str) -> None:
    for name in names:
        (paths.ml_dir() / name).mkdir(parents=True)


def _in_ml_folder(name: str) -> Path:
    p = Path(name).resolve()
    assert p.parent == paths.ml_dir().resolve()
    return p


async def test_a_track_that_is_not_one_of_the_two_never_names_a_file(sm):
    """The reranker file name takes its track from a fixed table (D-SEC-42): a traversal value
    is refused before anything is written, inside the ml folder or outside it."""
    from kestrel.db.enums import TRACKS

    assert set(rr._FILE_TRACKS) == set(TRACKS)
    assert all(rr._FILE_TRACKS[t] == t for t in TRACKS)
    # Without the table these folders would let the name climb out of the ml folder.
    _make_ml_subfolders("reranker_x", "features_x")
    before = _data_files()
    async with sm() as s:
        with pytest.raises(ValueError):
            await rr._save_model(
                s, track="x/../../escape", model={"m": 1}, fitted=_Fitted(), n_labeled=0,
                metrics={}, promoted=False, promoted_by="auto",
            )
        rows = (await s.execute(select(MlModel))).scalars().all()
    assert _data_files() == before
    assert not _escaped()
    assert rows == []


async def test_both_tracks_still_name_their_files_inside_the_ml_folder(sm):
    for track in ("employment", "consulting"):
        async with sm() as s:
            ml_id = await rr._save_model(
                s, track=track, model={"m": 1}, fitted=_Fitted(), n_labeled=0,
                metrics={}, promoted=False, promoted_by="auto",
            )
            await s.commit()
            row = await s.get(MlModel, ml_id)
        for name in (row.artifact_path, row.pca_artifact_path):
            p = _in_ml_folder(name)
            assert p.name.startswith(("reranker_" + track, "features_" + track))


# ----------------------------------------------------- stored model paths stay in the ml folder
async def _point_row_at(sm, ml_id: int, model: Path, pca: Path) -> MlModel:
    async with sm() as s:
        row = await s.get(MlModel, ml_id)
        row.artifact_path, row.pca_artifact_path = str(model), str(pca)
        await s.commit()
        return row


async def test_a_signed_copy_outside_the_ml_folder_is_never_read(sm):
    """A row moved to a validly signed copy elsewhere in the data folder is refused by the
    loader, the status panel and the retrain check alike: only the ml folder is read."""
    from kestrel.learning import training

    ml = await _trained(sm)
    other = paths.data_dir() / "elsewhere"
    other.mkdir()
    model, pca = other / "m.pkl", other / "p.pkl"
    model.write_bytes(_read(ml.artifact_path))
    pca.write_bytes(_read(ml.pca_artifact_path))
    row = await _point_row_at(sm, ml.id, model, pca)
    assert not rr._artifact_is_loadable(row)
    assert not training._verifies(row)
    async with sm() as s:
        assert await rr.load_active_reranker(s, "employment") is None
        notes = await _refusals(s)
    # Refused for where the file is, before its signature is even checked.
    assert len(notes) == 1 and "model folder" in notes[0]


async def test_a_link_in_the_ml_folder_to_a_file_outside_is_never_read(sm, tmp_path):
    ml = await _trained(sm)
    outside = tmp_path / "outside.pkl"
    outside.write_bytes(_read(ml.artifact_path))
    link = paths.ml_dir() / "reranker_employment_linked.pkl"
    symlink_or_skip(link, outside)
    assert signing.read_model_file(ml.artifact_path) is not None  # the real file still reads
    assert signing.read_model_file(link) is None
    row = await _point_row_at(sm, ml.id, link, Path(ml.pca_artifact_path))
    assert signing.read_verified(link, row.artifact_signature) is None
    assert not rr._artifact_is_loadable(row)


def test_a_model_file_that_is_too_large_is_not_read(monkeypatch):
    ml = paths.ml_dir()
    ml.mkdir(parents=True, exist_ok=True)
    big = ml / "reranker_employment_big.pkl"
    big.write_bytes(b"z" * 64)
    assert signing.read_model_file(big) == b"z" * 64
    monkeypatch.setattr(signing, "MAX_ARTIFACT_BYTES", 63)
    assert signing.read_model_file(big) is None


def _not_a_file(path: Path) -> None:
    """A pipe where the OS has them (it never yields a writer here, so reading it as a file
    would wait forever), else a folder: either way, not a regular file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(os, "mkfifo"):
        os.mkfifo(path)
    else:
        path.mkdir()


def test_a_pipe_in_place_of_a_model_file_is_not_read():
    pipe = paths.ml_dir() / "reranker_employment_pipe.pkl"
    _not_a_file(pipe)
    assert signing.read_model_file(pipe) is None


def test_no_model_file_is_read_through_an_ml_folder_that_is_a_link(tmp_path):
    real = tmp_path / "real-ml"
    real.mkdir()
    (real / "reranker_employment_v1.pkl").write_bytes(b"model")
    ml = paths.ml_dir()
    ml.parent.mkdir(parents=True, exist_ok=True)
    link_folder(ml, real)
    assert signing.read_model_file(ml / "reranker_employment_v1.pkl") is None


def test_a_pipe_in_place_of_the_signing_key_is_not_read():
    key = paths.model_signing_key_path()
    _not_a_file(key)
    assert signing.load_key() is None
    assert signing.read_verified(paths.ml_dir() / "reranker_employment_v1.pkl", "00") is None


def test_a_signing_key_that_is_a_link_is_not_used(tmp_path):
    outside = tmp_path / "planted.key"
    outside.write_bytes(os.urandom(signing.KEY_BYTES))
    key = paths.model_signing_key_path()
    key.parent.mkdir(parents=True, exist_ok=True)
    symlink_or_skip(key, outside)
    assert signing.load_key() is None


def test_a_signing_key_of_the_wrong_size_is_not_used():
    key = paths.model_signing_key_path()
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_bytes(b"k" * (signing.KEY_BYTES * 4))
    assert signing.load_key() is None
    key.write_bytes(b"k" * signing.KEY_BYTES)
    assert signing.load_key() == b"k" * signing.KEY_BYTES


@needs_dir_fd
def test_an_ml_folder_swapped_for_a_link_after_the_check_is_not_followed(monkeypatch, tmp_path):
    """The folder passed the check, then became a link: listing, reading and removing work
    relative to the folder opened without following a link, so they reach nothing through it."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "reranker_employment_v1.pkl").write_bytes(b"not kestrel's")
    ml = paths.ml_dir()
    ml.parent.mkdir(parents=True, exist_ok=True)
    link_folder(ml, elsewhere)
    monkeypatch.setattr(paths, "ml_folder", lambda: ml)  # as if checked before the swap
    assert paths.ml_entries() == []
    assert paths.read_ml_file("reranker_employment_v1.pkl", 1024) is None
    assert paths.remove_ml_file("reranker_employment_v1.pkl") is False
    assert (elsewhere / "reranker_employment_v1.pkl").read_bytes() == b"not kestrel's"


def test_a_hard_link_to_a_file_outside_is_not_read_or_backed_up(tmp_path):
    """A second name for an outside file, placed in the ml folder, is a regular file; it is
    still refused, because every file Kestrel writes has exactly one name."""
    from kestrel.services.backup import ML_PREFIX, create_backup

    private = tmp_path / "private.txt"
    private.write_bytes(b"invented private bytes")
    ml = paths.ml_dir()
    ml.mkdir(parents=True, exist_ok=True)
    try:
        os.link(private, ml / "reranker_employment_hard.pkl")
    except OSError as exc:
        raise AssertionError(f"this volume cannot make a hard link: {exc}") from exc
    assert signing.read_model_file(ml / "reranker_employment_hard.pkl") is None
    with zipfile.ZipFile(create_backup(tmp_path / "out")) as z:
        assert f"{ML_PREFIX}reranker_employment_hard.pkl" not in z.namelist()


def test_only_exact_ascii_model_file_names_are_kestrels():
    assert paths.is_model_file_name("reranker_employment_v3.pkl")
    assert paths.is_model_file_name("features_consulting_v1.pkl")
    for name in ("Reranker_employment_v3.PKL", "reranker_\udcff.pkl", "reranker_x.pkl.bak",
                 "notes.bin", "../reranker_x.pkl", "sub/reranker_x.pkl", "."):
        assert not paths.is_model_file_name(name), name


def test_a_hard_linked_signing_key_is_used_and_never_replaced():
    """A second name for the key (a snapshot or copy tool's hard link) is still this install's
    key: it is read, and nothing replaces it."""
    key_path = paths.model_signing_key_path()
    first = signing.load_or_create_key()
    os.link(key_path, key_path.with_name("key-snapshot"))
    assert signing.load_key() == first
    assert signing.load_or_create_key() == first
    assert key_path.read_bytes() == first


def test_a_refused_signing_key_is_set_aside_with_a_warning_never_overwritten(tmp_path):
    from structlog.testing import capture_logs

    outside = tmp_path / "planted.key"
    outside.write_bytes(b"p" * signing.KEY_BYTES)
    key_path = paths.model_signing_key_path()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    symlink_or_skip(key_path, outside)
    with capture_logs() as logs:
        assert signing.load_key() is None
        new = signing.load_or_create_key()
    refused = [e for e in logs if e["event"] == "model_signing_key_refused"]
    replaced = [e for e in logs if e["event"] == "model_signing_key_replaced"]
    assert refused and refused[0]["log_level"] == "warning" and refused[0]["reason"] == "a link"
    assert len(replaced) == 1 and replaced[0]["log_level"] == "warning"
    kept = key_path.with_name(replaced[0]["kept_as"])
    assert kept.is_symlink() and outside.read_bytes() == b"p" * signing.KEY_BYTES
    assert not key_path.is_symlink() and key_path.read_bytes() == new != b"p" * signing.KEY_BYTES


def test_a_secrets_folder_that_is_a_link_is_left_alone_and_no_key_is_written(tmp_path):
    from structlog.testing import capture_logs

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "model_signing.key").write_bytes(b"short")
    secrets = paths.model_signing_key_path().parent
    secrets.parent.mkdir(parents=True, exist_ok=True)
    link_folder(secrets, elsewhere)
    with capture_logs() as logs, pytest.raises(PermissionError):
        signing.load_or_create_key()
    assert sorted(p.name for p in elsewhere.iterdir()) == ["model_signing.key"]
    assert (elsewhere / "model_signing.key").read_bytes() == b"short"
    assert any(e["event"] == "model_signing_key_not_replaced" and e["log_level"] == "warning"
               for e in logs)


def test_a_key_that_cannot_be_read_for_a_moment_is_kept_not_replaced(monkeypatch):
    """A passing read error is not a bad key: nothing is renamed or written, and the save that
    needed the key fails and says so."""
    first = signing.load_or_create_key()
    monkeypatch.setattr(paths, "read_regular_file", lambda *a, **k: None)
    with pytest.raises(PermissionError):
        signing.load_or_create_key()
    monkeypatch.undo()
    assert sorted(p.name for p in paths.model_signing_key_path().parent.iterdir()) == [
        "model_signing.key"]
    assert signing.load_key() == first


@needs_dir_fd
def test_a_download_folder_swapped_for_a_link_after_the_check_is_not_followed(
    monkeypatch, tmp_path
):
    """The prep packs folder becomes a link to another folder after every check by path has
    passed (simulated by standing in for this module's own checks): the download route still
    serves none of the outside file's bytes, because the read opens the folder without
    following a link."""
    import asyncio
    import shutil

    from fastapi.testclient import TestClient

    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import PrepPack
    from kestrel.web.app import build_app
    from tests import role_seed

    ids = asyncio.run(role_seed.seed_async())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a.pdf").write_bytes(b"INVENTED OUTSIDE BYTES")
    packs = paths.prep_packs_dir()
    packs.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(packs, ignore_errors=True)
    link_folder(packs, elsewhere)
    stored = packs / "a.pdf"

    async def add():
        async with get_sessionmaker()() as s:
            pack = PrepPack(opportunity_id=ids["strong"], content_md="x", pdf_path=str(stored))
            s.add(pack)
            await s.commit()
            return pack.id

    pack_id = asyncio.run(add())
    # Every check by path ran while the folder was still a folder; it became a link only after.
    # Only this module's own names are stood in for, never the global os.path.
    real_base = packs.parent.resolve() / packs.name
    monkeypatch.setattr(paths, "stored_prep_pdf", lambda value: stored)
    monkeypatch.setattr(paths, "own_folder", lambda folder: real_base)
    monkeypatch.setattr(paths, "_realpath", lambda p, *a, **k: str(real_base), raising=False)
    with TestClient(build_app(worker=False)) as c:
        r = c.get(f"/prep/{pack_id}/download")
    assert b"INVENTED OUTSIDE BYTES" not in r.content, "served the outside file"
    assert r.status_code == 404
