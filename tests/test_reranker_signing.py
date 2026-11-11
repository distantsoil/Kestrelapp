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
