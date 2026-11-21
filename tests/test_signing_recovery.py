"""Kestrel can always recover from a signing key it cannot use, without file surgery, and the
key is never overwritten except as a deliberate, logged replacement.

A key that cannot be read, or a secrets folder that is a link, stops saves with a reason (never a
bare 500, never an unsigned pickle left behind) until the person makes a new key on Settings >
Scoring and learning, which keeps the old file under a new name. A new key never replaces one
that a racing creator made first, and its temporary file is never written through a link."""

from __future__ import annotations

import asyncio
import os
import threading

import pytest
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.db.engine import get_sessionmaker
from kestrel.learning import signing
from tests.learning_fakes import seed_dataset
from tests.symlink_probe import link_folder, symlink_or_skip

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

NEW_KEY_ROUTE = "/settings/scoring/new-key"
CARD = "/settings/scoring/learned?track=employment"
UNREADABLE = ("Kestrel cannot use its model signing key: the key file could not be read. A new "
              "key can be made on Settings, Scoring and learning.")


@pytest.fixture(autouse=True)
def _migrated():
    from kestrel.db import migrate

    migrate.run_migrations()


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _unreadable_key(monkeypatch) -> tuple[bytes, int]:
    """A key that exists but cannot be read (as a permission error would leave it): reads of
    that one file fail, and any other file, a new key included, reads as usual."""
    first = signing.load_or_create_key()
    ino = os.stat(paths.model_signing_key_path()).st_ino
    real = paths.read_regular_file

    def read(path, *a, **k):
        try:
            if os.stat(path, follow_symlinks=False).st_ino == ino:
                return None
        except OSError:
            pass
        return real(path, *a, **k)

    monkeypatch.setattr(paths, "read_regular_file", read)
    return first, ino


def _secrets_names() -> list[str]:
    return sorted(p.name for p in paths.model_signing_key_path().parent.iterdir())


# ------------------------------------------------------------------ the Scoring and learning action
def _toast(r) -> str:
    import json

    return json.loads(r.headers.get("HX-Trigger", "{}")).get("k-toast", {}).get("message", "")


def test_the_learning_tab_offers_a_new_key_and_making_one_keeps_the_old_file(monkeypatch):
    first, _ino = _unreadable_key(monkeypatch)
    with _client() as c:
        tab = c.get(CARD).text
        assert ("Kestrel cannot use its model signing key: the key file could not be read."
                in tab)
        assert "Make a new signing key" in tab
        r = c.post(NEW_KEY_ROUTE, data={"track": "employment"})
    assert r.status_code == 200
    names = _secrets_names()
    assert len(names) == 2 and "model_signing.key" in names
    (kept,) = [n for n in names if n != "model_signing.key"]
    assert kept.startswith("model_signing.key.refused-")
    assert _toast(r) == f"A new signing key was made. The old one is kept beside it as {kept}."
    assert "Make a new signing key" not in r.text  # the problem is gone
    monkeypatch.undo()
    assert (paths.model_signing_key_path().parent / kept).read_bytes() == first
    assert signing.load_key() not in (None, first)


def test_a_secrets_folder_that_is_a_link_is_set_aside_by_the_action(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "model_signing.key").write_bytes(b"k" * signing.KEY_BYTES)
    secrets = paths.model_signing_key_path().parent
    secrets.parent.mkdir(parents=True, exist_ok=True)
    link_folder(secrets, elsewhere)
    with _client() as c:
        assert "the folder that holds it is a link" in c.get(CARD).text
        r = c.post(NEW_KEY_ROUTE, data={"track": "employment"})
    assert _toast(r).startswith("A new signing key was made.")
    assert not secrets.is_symlink() and paths.own_folder(secrets) is not None
    assert signing.load_key() not in (None, b"k" * signing.KEY_BYTES)
    assert sorted(p.name for p in elsewhere.iterdir()) == ["model_signing.key"]
    assert (elsewhere / "model_signing.key").read_bytes() == b"k" * signing.KEY_BYTES


def test_a_usable_key_is_never_replaced_by_the_action():
    first = signing.load_or_create_key()
    with _client() as c:
        r = c.post(NEW_KEY_ROUTE, data={"track": "employment"})
    assert r.status_code == 200
    assert _toast(r) == "The signing key is fine, so nothing was changed."
    assert _secrets_names() == ["model_signing.key"]
    assert signing.load_key() == first


# ------------------------------------------------------------------- training with no key
def test_training_with_an_unusable_key_says_why_and_leaves_no_model_file(monkeypatch):
    async def seed():
        async with get_sessionmaker()() as s:
            await seed_dataset(s, n=250, signal=True)

    asyncio.run(seed())
    _unreadable_key(monkeypatch)
    with _client() as c:
        r = c.post("/api/learning/train?track=employment")
    assert r.status_code == 409
    assert r.json()["reason"] == UNREADABLE
    ml = paths.ml_dir()
    pickles = sorted(p.name for p in ml.glob("*.pkl")) if ml.exists() else []
    assert pickles == []


# --------------------------------------------------------------- creating a key, exclusively
def test_a_key_made_by_another_creator_first_wins_and_is_reused(monkeypatch):
    """Creator B finishes its key between this creator's check and its write: this creator
    must not replace B's key, and must sign with it."""
    theirs = b"b" * signing.KEY_BYTES
    path = paths.model_signing_key_path()
    real = signing._read_key
    calls = []

    def read(p):
        got = real(p)
        if not calls:  # the first check: no key yet, and B writes one right after
            calls.append(1)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(theirs)
        return got

    monkeypatch.setattr(signing, "_read_key", read)
    assert signing.load_or_create_key() == theirs
    assert path.read_bytes() == theirs
    assert _secrets_names() == ["model_signing.key"]


def test_racing_creators_all_end_with_the_one_key_on_disk():
    barrier = threading.Barrier(8)
    got: list[bytes] = []

    def create():
        barrier.wait()
        got.append(signing.load_or_create_key())

    threads = [threading.Thread(target=create) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    on_disk = paths.model_signing_key_path().read_bytes()
    assert len(got) == 8 and set(got) == {on_disk}
    assert _secrets_names() == ["model_signing.key"]


def test_a_link_at_the_temporary_name_is_never_written_through(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"INVENTED OUTSIDE BYTES")
    path = paths.model_signing_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    symlink_or_skip(path.with_name(f"{path.name}.{os.getpid()}.tmp"), outside)
    key = signing.load_or_create_key()
    assert outside.read_bytes() == b"INVENTED OUTSIDE BYTES"
    assert path.read_bytes() == key and not path.is_symlink()
