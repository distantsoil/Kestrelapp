"""The check before a restore reads every entry of the backup (D-SEC-30).

INVARIANT: nothing is stopped, released or written until every entry of the backup has been read
and validated; a backup that fails any read is refused before the loops stop. All text is
invented; nothing touches the network."""

from __future__ import annotations

import asyncio
import struct
import zipfile

import httpx
import pytest

from kestrel.db.engine import get_sessionmaker, reset_engine
from kestrel.services import backup as bk
from kestrel.web import app as app_mod
from tests.test_restore_import import _a_backup, _rows, _this_computer
from tests.test_restore_queue_final import _fresh, loops  # noqa: F401  (the stub work loops)

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

PAYLOAD = b"INVENTED-DOCUMENT-BYTES-" * 40
DOC = "documents/documents/resume-invented.pdf"


def _with_document(archive, out, *, name: str = DOC):
    with zipfile.ZipFile(archive) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zout:
        for info in zin.infolist():
            zout.writestr(info, zin.read(info.filename))
        zout.writestr(name, PAYLOAD)
    return out


def _flipped(archive, out):
    """A stored document with one byte changed, so its CRC no longer matches."""
    _with_document(archive, out)
    raw = bytearray(out.read_bytes())
    at = raw.find(PAYLOAD)
    assert at > 0
    raw[at + 5] ^= 0xFF
    out.write_bytes(bytes(raw))
    return out


def _encrypted(archive, out, *, name: str = DOC):
    """The same archive with the entry marked as encrypted (general purpose bit 0), in its own
    header and in the central directory. Nothing can read it without a password."""
    _with_document(archive, out, name=name)
    with zipfile.ZipFile(out) as zf:
        offset = zf.getinfo(name).header_offset
    raw = bytearray(out.read_bytes())
    assert raw[offset:offset + 4] == b"PK\x03\x04"
    struct.pack_into("<H", raw, offset + 6, struct.unpack_from("<H", raw, offset + 6)[0] | 1)
    central = raw.find(b"PK\x01\x02")
    marked = 0
    while central != -1:
        n_name = struct.unpack_from("<H", raw, central + 28)[0]
        if bytes(raw[central + 46:central + 46 + n_name]) == name.encode():
            struct.pack_into(
                "<H", raw, central + 8, struct.unpack_from("<H", raw, central + 8)[0] | 1
            )
            marked += 1
        central = raw.find(b"PK\x01\x02", central + 4)
    assert marked == 1
    out.write_bytes(bytes(raw))
    with zipfile.ZipFile(out) as zf, pytest.raises(RuntimeError):
        zf.read(name)  # the fixture really is unreadable
    return out


def test_a_document_that_fails_its_crc_is_refused_by_the_check(tmp_path):
    bad = _flipped(_a_backup(tmp_path), tmp_path / "bad.zip")
    _this_computer()
    before = _rows()
    with pytest.raises(bk.InvalidBackupError) as exc:
        bk.check_backup(bad)
    assert str(exc.value) == bk.ENTRY_UNREADABLE
    assert _rows() == before


def test_an_encrypted_document_is_refused_by_the_check(tmp_path):
    bad = _encrypted(_a_backup(tmp_path), tmp_path / "enc.zip")
    with pytest.raises(bk.InvalidBackupError) as exc:
        bk.check_backup(bad)
    assert str(exc.value) == bk.ENTRY_UNREADABLE


def test_an_encrypted_entry_the_restore_never_reads_refuses_nothing(tmp_path):
    """`config/note.yml` is not a file a restore installs, so neither the check nor the restore
    opens it: the two agree here as well."""
    odd = _encrypted(_a_backup(tmp_path), tmp_path / "odd.zip", name="config/note.yml")
    _this_computer()
    bk.check_backup(odd)
    assert bk.restore_backup(odd).db_restored is True


def test_the_check_and_the_restore_agree_on_an_unreadable_entry(tmp_path):
    archive = _a_backup(tmp_path)
    for bad in (_flipped(archive, tmp_path / "f.zip"), _encrypted(archive, tmp_path / "e.zip")):
        with pytest.raises(bk.InvalidBackupError) as checked:
            bk.check_backup(bad)
        with pytest.raises(bk.InvalidBackupError) as restored:
            bk.restore_backup(bad)
        assert str(checked.value) == str(restored.value) == bk.ENTRY_UNREADABLE


def test_the_check_keeps_no_document_bytes(tmp_path):
    good = _with_document(_a_backup(tmp_path), tmp_path / "good.zip")
    checked, full = tmp_path / "checked", tmp_path / "full"
    checked.mkdir()
    full.mkdir()
    bk._stage_backup(good, checked, database_only=True)
    bk._stage_backup(good, full)
    assert (full / DOC).read_bytes() == PAYLOAD
    assert (checked / DOC).read_bytes() == b""
    assert (checked / bk.DB_NAME).stat().st_size == (full / bk.DB_NAME).stat().st_size > 0


def test_a_backup_with_readable_documents_passes_the_check_and_restores(tmp_path):
    good = _with_document(_a_backup(tmp_path), tmp_path / "good.zip")
    _this_computer()
    bk.check_backup(good)
    result = bk.restore_backup(good)
    assert result.db_restored is True
    assert result.document_files == ["documents/resume-invented.pdf"]


async def test_an_upload_with_an_unreadable_document_stops_no_loop(loops, tmp_path):  # noqa: F811
    from kestrel.queue.queue import TaskQueue
    from kestrel.web.app import build_app

    bad = await asyncio.to_thread(lambda: _flipped(_a_backup(tmp_path), tmp_path / "bad.zip"))
    await _fresh()
    q = TaskQueue(get_sessionmaker())
    await q.enqueue("t2", {"opportunity_id": 41})
    leased = await q.lease("worker-invented", types=["t2"])
    assert leased is not None
    stops: list[str] = []
    real_stop = app_mod._graceful_worker_shutdown

    async def recorded(*args, **kwargs):
        stops.append("stopped")
        return await real_stop(*args, **kwargs)

    app_mod._spawn_work_loops(loops["app_stop"])
    await asyncio.sleep(0.01)
    _own, worker, lane = app_mod._current_work_loops()
    transport = httpx.ASGITransport(app=build_app(worker=False), client=("127.0.0.1", 5000))
    try:
        app_mod._graceful_worker_shutdown = recorded
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            r = await c.post(
                "/settings/restore",
                files={"backup_file": ("b.zip", bad.read_bytes(), "application/zip")},
            )
    finally:
        app_mod._graceful_worker_shutdown = real_stop
    assert r.status_code == 200, r.text
    assert bk.ENTRY_UNREADABLE in r.text
    assert stops == []
    _own2, worker2, lane2 = app_mod._current_work_loops()
    assert worker2 is worker and lane2 is lane
    assert not worker.done() and not lane.done()
    assert loops["events"] == []
    still = await q.get(leased.id)
    assert (still.status, still.worker_id) == ("leased", "worker-invented")
    await reset_engine()


# D-SEC-31: only STORED and DEFLATED entries are read; every decompressor error is a refusal.


def _compressed_document(archive, out, method: int, data: bytes = PAYLOAD):
    with zipfile.ZipFile(archive) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zout:
        for info in zin.infolist():
            zout.writestr(info, zin.read(info.filename))
        zout.writestr(DOC, data, compress_type=method)
    return out


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_a_bzip2_or_lzma_entry_is_refused_unread_with_bounded_memory(tmp_path, method):
    import tracemalloc

    big = b"\0" * (64 * 1024 * 1024)  # compresses to a few KB; would inflate in one read
    bad = _compressed_document(_a_backup(tmp_path), tmp_path / "bomb.zip", method, big)
    del big
    tracemalloc.start()
    try:
        with pytest.raises(bk.InvalidBackupError) as exc:
            bk.check_backup(bad)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert str(exc.value) == bk.ENTRY_UNREADABLE
    assert peak < 16 * 1024 * 1024


def test_a_corrupt_deflate_stream_is_a_refusal_not_a_raw_error(tmp_path):
    data = bytes(range(256)) * 64
    bad = _compressed_document(_a_backup(tmp_path), tmp_path / "z.zip", zipfile.ZIP_DEFLATED, data)
    with zipfile.ZipFile(bad) as zf:
        info = zf.getinfo(DOC)
    raw = bytearray(bad.read_bytes())
    start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    raw[start] = 0xFF  # invalid deflate block type
    bad.write_bytes(bytes(raw))
    with pytest.raises(bk.InvalidBackupError) as exc:
        bk.check_backup(bad)
    assert str(exc.value) == bk.ENTRY_UNREADABLE
    with pytest.raises(bk.InvalidBackupError):
        bk.restore_backup(bad)
