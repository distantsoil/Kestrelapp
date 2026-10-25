"""Every destructive test helper refuses to run unless KESTREL_DATA_HOME is a marked throwaway
directory that is NOT the real data home (nor inside it). A seed helper once wiped an operator's
real database because it ran from a plain shell, outside pytest, with the variable unset.

The "real" data home is faked to a temp directory holding a sentinel database, so these tests
never go near an actual one."""

from __future__ import annotations

import asyncio

import pytest

from kestrel import paths
from tests import profile_seed, retry_seed, role_seed, tailor_seed, throwaway, today_seed


@pytest.fixture
def fake_real(tmp_path, monkeypatch):
    """A fake 'real' data home with a sentinel database (and -wal), marked or not per test."""
    real = tmp_path / "RealKestrel"
    (real / "data").mkdir(parents=True)
    for name in ("kestrel.db", "kestrel.db-wal", "kestrel.db-shm"):
        (real / "data" / name).write_bytes(b"SENTINEL")
    monkeypatch.setattr(paths, "default_data_home", lambda: real)
    monkeypatch.setattr(throwaway, "real_data_home", lambda: real)
    return real


def _intact(real) -> bool:
    return all(
        (real / "data" / n).read_bytes() == b"SENTINEL"
        for n in ("kestrel.db", "kestrel.db-wal", "kestrel.db-shm")
    )


def _helpers():
    return [
        ("tailor_seed", lambda: asyncio.run(tailor_seed.seed_async())),
        ("today_seed", lambda: asyncio.run(today_seed.seed())),
        ("retry_seed", lambda: asyncio.run(retry_seed.seed_async())),
        ("role_seed", lambda: asyncio.run(role_seed.seed_async())),
        ("profile_seed", lambda: asyncio.run(profile_seed.seed_async())),
        ("retry_wipe", retry_seed._wipe_db),
        ("role_wipe", role_seed._wipe_db),
    ]


@pytest.mark.parametrize("where", ["unset", "real", "inside_real", "inside_real_marked"])
def test_helpers_refuse_before_touching_anything(where, fake_real, monkeypatch):
    if where == "unset":
        monkeypatch.delenv("KESTREL_DATA_HOME", raising=False)
    elif where == "real":
        throwaway.mark(fake_real)  # even a marked real home is refused
        monkeypatch.setenv("KESTREL_DATA_HOME", str(fake_real))
    else:
        inside = fake_real / "nested"
        inside.mkdir()
        if where == "inside_real_marked":
            throwaway.mark(inside)
        monkeypatch.setenv("KESTREL_DATA_HOME", str(inside))
    before = sorted(p.name for p in fake_real.rglob("*"))
    for name, run in _helpers():
        with pytest.raises(RuntimeError, match="KESTREL_DATA_HOME"):
            run()
        assert _intact(fake_real), name
    # Nothing was created or removed either, apart from the directory the test itself made.
    after = sorted(p.name for p in fake_real.rglob("*") if p.name != "nested")
    assert after == [n for n in before if n != "nested"]


def test_a_marked_temp_home_outside_the_real_one_is_allowed(fake_real, tmp_path, monkeypatch):
    home = tmp_path / "scratch"
    home.mkdir()
    throwaway.mark(home)
    monkeypatch.setenv("KESTREL_DATA_HOME", str(home))
    throwaway.require_throwaway_home("test")  # does not raise
    (home / "data").mkdir()
    paths.db_path().write_bytes(b"x")
    retry_seed._wipe_db()
    assert not paths.db_path().exists()
    assert _intact(fake_real)


def test_an_unmarked_temp_home_is_refused(fake_real, tmp_path, monkeypatch):
    home = tmp_path / "unmarked"
    home.mkdir()
    monkeypatch.setenv("KESTREL_DATA_HOME", str(home))
    with pytest.raises(RuntimeError, match="KESTREL_DATA_HOME"):
        throwaway.require_throwaway_home("test")


def test_the_seed_still_works_under_the_pytest_fixture():
    ids = asyncio.run(tailor_seed.seed_async())
    assert ids
