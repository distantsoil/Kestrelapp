"""Code-scanning "Information exposure through an exception" alerts (D-SEC-4): a response built
straight from a failure shows one of Kestrel's own sentences, never a piece of the exception.

Each test makes the failure carry a secret path, then checks the page has the sentence and not the
path. Offline: TestClient on the real app, a throwaway data home, no browser, no model, no network.
"""

from __future__ import annotations

import asyncio
import errno
import html
import re
import sqlite3
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from kestrel.errors import UserFacingError
from kestrel.web.errors import GENERIC, OS_DEFAULT, authored_error, fixed_reason
from tests.test_analytics_page import web  # noqa: F401  (fixture)
from tests.test_hosted_mode import _fresh_app

SECRET = r"C:\Users\someone\AppData\secret-dir\kestrel.key"
HX = {"HX-Request": "true"}


def _json(client: TestClient, path: str, body: dict):
    return client.post(path, json=body)


# ------------------------------------------------------------------------------ fixed_reason
@pytest.mark.parametrize(
    "exc, expected",
    [
        (PermissionError(errno.EACCES, "Permission denied", SECRET), "permission was denied"),
        (FileNotFoundError(errno.ENOENT, "No such file", SECRET), "the file was not found"),
        (OSError(errno.ENOSPC, "No space left", SECRET), "there is no space left on the disk"),
        (OSError(errno.EROFS, "Read-only", SECRET), "the disk is read-only"),
        (OSError(errno.EIO, "I/O error", SECRET), OS_DEFAULT),
        (OSError(SECRET), OS_DEFAULT),
        (TimeoutError(SECRET), "it took too long"),
        (zipfile.BadZipFile(SECRET), "the file is not a usable zip archive"),
        (sqlite3.OperationalError(SECRET), "the database could not be read"),
        (OperationalError(SECRET, {}, Exception(SECRET)), "the database could not be read"),
        (RuntimeError(SECRET), GENERIC),
        (ValueError(SECRET), GENERIC),
        (UserFacingError(SECRET), GENERIC),  # authored errors use plain_error or authored_error
    ],
)
def test_fixed_reason_is_a_sentence_of_ours_and_never_the_exceptions_text(exc, expected):
    said = fixed_reason(exc)
    assert said == expected
    assert SECRET not in said and "secret-dir" not in said


def test_authored_error_picks_from_the_list_and_never_copies_the_exception():
    sentences = ("Choose a smaller photo.", "Choose a PNG.")
    assert authored_error(ValueError("Choose a PNG."), sentences, "fallback") == "Choose a PNG."
    said = authored_error(ValueError(SECRET), sentences, "fallback")
    assert said == "fallback" and SECRET not in said
    # near misses do not pass either: only an exact entry of the list is shown
    assert authored_error(ValueError("Choose a PNG. " + SECRET), sentences, "fb") == "fb"
    assert authored_error(ValueError("choose a png."), sentences, "fb") == "fb"


# ------------------------------------------------------------------------------ PDF sinks
def test_analytics_pdf_failure_shows_no_exception_text(web, monkeypatch):  # noqa: F811
    from kestrel.docs import render

    async def boom(html: str):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(render, "render_pdf", boom)
    r = web.get("/analytics/export/summary.pdf")
    assert r.status_code == 503 and "needs Microsoft Edge" in r.text
    assert SECRET not in r.text and "secret-dir" not in r.text
    assert r.text.endswith(GENERIC)


async def test_prep_pdf_failure_shows_no_exception_text(monkeypatch):
    from kestrel.db.models import PrepPack
    from tests import prep_seed
    from tests.test_prep_screen import _all, _client

    await prep_seed.seed_async(with_pack=True)

    async def boom(html):
        raise PermissionError(errno.EACCES, "Permission denied", SECRET)

    monkeypatch.setattr("kestrel.docs.render.render_pdf", boom)
    pack = (await _all(PrepPack))[0]
    with _client() as c:
        r = c.post(f"/prep/{pack.id}/pdf")
    assert 'data-testid="pp-pdf-error"' in r.text and "Edge" in r.text
    assert "permission was denied" in r.text
    assert SECRET not in r.text and "secret-dir" not in r.text


async def test_tailor_pdf_failure_shows_no_exception_text(monkeypatch):
    from tests import tailor_seed
    from tests.test_tailor_screen import _client

    ids = await tailor_seed.seed_async(config={"stale": True})

    async def boom(session, settings, render):
        raise OSError(errno.ENOSPC, "No space left", SECRET)

    monkeypatch.setattr("kestrel.docs.service.refresh_pdf", boom)
    with _client() as c:
        r = c.get(f"/renders/{ids['render']}/download")
    assert r.status_code == 503
    assert r.text == "Could not make the PDF: there is no space left on the disk"


def test_data_backup_failure_shows_no_exception_text(monkeypatch):
    from kestrel.services import backup as backup_service

    def boom(*_a, **_k):
        raise PermissionError(errno.EACCES, "Permission denied", SECRET)

    monkeypatch.setattr(backup_service, "create_backup_to_temp", boom)
    r = _fresh_app().get("/settings/data/backup")
    assert r.status_code == 500
    assert r.text == "Could not build the backup: permission was denied"


# ------------------------------------------------------------------------------ bench promote
def test_bench_promote_save_failure_shows_no_exception_text(monkeypatch):
    from kestrel.web.pages import models_ui

    async def boom(*_a, **_k):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(models_ui, "switch_local", boom)
    r = _fresh_app().post("/bench/promote", data={"tier": "tier2", "model": "gemma3:12b"})
    assert "Could not save: " + GENERIC in r.text
    assert SECRET not in r.text and "secret-dir" not in r.text


def test_bench_promote_online_list_failure_names_the_provider_and_nothing_else(monkeypatch):
    from types import SimpleNamespace

    from kestrel.services import hosted_models as hm
    from kestrel.web.pages import models_ui

    async def boom(*_a, **_k):
        raise hm.ModelListError(f"The list failed at {SECRET}")

    monkeypatch.setattr(models_ui, "connected_backend", lambda s: ("openrouter", SimpleNamespace()))
    monkeypatch.setattr(models_ui, "online_problem", lambda s: None)
    monkeypatch.setattr(hm, "fetch_models", boom)
    r = _fresh_app().post("/bench/promote", data={"tier": "tier2", "model": "hosted:vendor/x"})
    assert "The model list from openrouter could not be fetched." in r.text
    assert "Settings &gt; Hosted" in r.text
    assert SECRET not in r.text and "secret-dir" not in r.text


# ------------------------------------------------------------------------------ run now
def test_run_now_says_why_hosted_cannot_run_from_the_settings_not_an_exception():
    client = _fresh_app()
    r = client.post("/api/run/now?mode=hosted&confirmed=1")
    assert r.status_code == 400 and "acknowledged" in r.json()["reason"]
    r = client.post("/runs/now", data={"mode": "hosted", "confirmed": "1"})
    assert r.status_code == 400 and "Not queued: " in r.text and "acknowledged" in r.text
    assert "Hosted" in r.text  # the sentence points at Settings > Hosted


def test_run_now_reads_the_reason_again_from_the_settings_never_from_the_exception(monkeypatch):
    """When loading the settings for an online run raises, the page says what the settings say
    (or the fixed sentence if they changed meanwhile), and the exception's text goes nowhere."""
    from kestrel.config import HostedModeError
    from kestrel.services import setting_overrides as so
    from kestrel.web import routes_control, routes_pages

    real_load = so.load_settings_with_db

    async def load(session, **kw):
        if kw.get("mode") in ("hosted", "both"):
            raise HostedModeError(SECRET)
        return await real_load(session, **kw)

    reasons: list[str | None] = ["the key is missing under Settings > Hosted", None]

    async def reason(session, mode):
        return reasons[0]

    monkeypatch.setattr(routes_control, "load_settings_with_db", load)
    monkeypatch.setattr(routes_pages, "load_settings_with_db", load)
    monkeypatch.setattr(routes_control, "hosted_run_problem", reason)
    monkeypatch.setattr(so, "hosted_run_problem", reason)
    client = _fresh_app()
    for _ in range(2):
        shown = reasons[0] or so.HOSTED_CHANGED
        r = client.post("/api/run/now?mode=hosted&confirmed=1")
        assert r.status_code == 400 and r.json()["reason"] == shown
        r = client.post("/runs/now", data={"mode": "hosted", "confirmed": "1"})
        assert r.status_code == 400 and html.escape(shown) in r.text
        assert SECRET not in r.text and "secret-dir" not in r.text
        reasons.reverse()  # the second round: the settings no longer give a reason


def test_run_now_race_between_load_and_queue_is_a_fixed_sentence(monkeypatch):
    """The settings can change after they were loaded; what queueing raises is not shown."""
    from kestrel.config import HostedModeError
    from kestrel.services import setting_overrides as so
    from kestrel.web import routes_control

    async def raced(session, mode, *, online_approval=None):
        raise HostedModeError(SECRET)

    monkeypatch.setattr(routes_control, "queue_run_mode", raced)
    monkeypatch.setattr(so, "queue_run_mode", raced)
    client = _fresh_app()
    r = client.post("/api/run/now", params={"mode": "local"})
    assert r.status_code == 400 and r.json()["reason"] == so.HOSTED_CHANGED
    r = client.post("/runs/now", data={"mode": "local"})
    assert html.escape(so.HOSTED_CHANGED) in r.text
    assert SECRET not in r.text and "secret-dir" not in r.text


def test_runs_start_never_shows_an_exceptions_text_when_hosted_cannot_run(monkeypatch):
    from kestrel.config import HostedModeError
    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import Source
    from kestrel.services import setting_overrides as so

    client = _fresh_app()

    async def seed():
        async with get_sessionmaker()() as s:
            s.add(Source(track="employment", kind="greenhouse", name="Acme", enabled=True,
                         config={"board": "acme"}))
            await s.commit()

    asyncio.run(seed())
    # An online run that cannot run is refused with a sentence, and nothing is queued.
    r = client.post("/runs/start", data={"mode": "hosted", "confirmed": "1"})
    assert r.status_code in (400, 409) and SECRET not in r.text

    async def raced(session, mode, *, online_approval=None):
        raise HostedModeError(SECRET)

    monkeypatch.setattr("kestrel.config.hosted_mode_problem", lambda settings: None)
    monkeypatch.setattr(so, "queue_run_mode", raced)
    r = client.post("/runs/start", data={"mode": "local"})
    assert r.status_code == 409 and so.HOSTED_CHANGED in r.text
    assert SECRET not in r.text and "secret-dir" not in r.text


# ------------------------------------------------------------------------------ profile undo
def test_profile_undo_answers_from_a_sentence_for_each_refusal(monkeypatch):
    from kestrel.services import profile_edit as pe

    monkeypatch.setattr(pe, "undo_token", lambda: "0123456789abcdef")
    client = _fresh_app()
    data = {"token": "0123456789abcdef"}

    def refuse_with(exc):
        def _raise():
            raise exc

        monkeypatch.setattr(pe, "restore_backup", _raise)
        return client.post("/profile/undo", data=data)

    r = refuse_with(pe.NoEarlierVersion(pe.NO_EARLIER_VERSION))
    assert r.status_code == 409 and r.text == pe.NO_EARLIER_VERSION
    r = refuse_with(pe.EarlierVersionUnreadable(pe.EARLIER_VERSION_UNREADABLE))
    assert r.status_code == 409 and r.text == pe.EARLIER_VERSION_UNREADABLE
    r = refuse_with(pe.ProfileEditError(SECRET))
    assert r.status_code == 409 and r.text == "The earlier version could not be restored."
    r = refuse_with(OSError(errno.EACCES, "Permission denied", SECRET))
    assert r.status_code == 500 and SECRET not in r.text


def test_restore_backup_raises_the_typed_refusals(tmp_path, monkeypatch):
    from kestrel import paths
    from kestrel.services import profile_edit as pe

    target = tmp_path / "profile.yml"
    monkeypatch.setattr(pe, "profile_path", lambda: target)
    with pytest.raises(pe.NoEarlierVersion, match="no earlier version"):
        pe.restore_backup()
    assert issubclass(pe.NoEarlierVersion, pe.ProfileEditError)
    assert paths.data_home()  # the isolated data home is in use


# ------------------------------------------------------------------------------ settings.io
def test_a_refused_ui_value_says_a_sentence_for_the_key_and_no_exception_text(monkeypatch):
    from kestrel.services import ui_state
    from kestrel.web.pages.settings_prefs import REFUSED_FALLBACK, REFUSED_VALUE

    client = _fresh_app()
    bad = _json(client, "/settings/ui", {"key": "ui.text_size", "value": "huge"}).json()
    assert not bad["ok"] and bad["message"] == REFUSED_VALUE["ui.text_size"]
    assert "must be one of" in bad["message"]
    none_left = _json(client, "/settings/ui", {"key": "ui.scene_rotation", "value": []}).json()
    assert none_left["message"] == "Keep at least one scene checked."

    def hostile(key, value):
        raise ValueError(SECRET)

    monkeypatch.setattr(ui_state, "validate", hostile)
    said = _json(client, "/settings/ui", {"key": "ui.text_size", "value": "large"}).json()
    assert said["message"] == REFUSED_VALUE["ui.text_size"] and SECRET not in said["message"]
    hostile_key = _json(client, "/settings/ui", {"key": "notify.future_key", "value": 1}).json()
    assert "not one Kestrel can change" in hostile_key["message"]
    assert REFUSED_FALLBACK  # a key without an entry reads this


def test_every_key_the_interface_owns_has_its_own_refusal_sentence():
    from kestrel.services import ui_state
    from kestrel.web.pages.settings_prefs import REFUSED_VALUE

    assert set(ui_state.OWNED_KEYS) <= set(REFUSED_VALUE), (
        sorted(set(ui_state.OWNED_KEYS) - set(REFUSED_VALUE))
    )
    assert all("—" not in text for text in REFUSED_VALUE.values())  # no em dashes in the copy


def test_a_ui_save_that_fails_in_the_database_shows_no_exception_text(monkeypatch):
    from kestrel.services import ui_state

    async def boom(*_a, **_k):
        raise OperationalError(SECRET, {}, Exception(SECRET))

    monkeypatch.setattr(ui_state, "set_value", boom)
    said = _json(_fresh_app(), "/settings/ui", {"key": "ui.text_size", "value": "large"}).json()
    assert said["message"] == "Could not save that: the database could not be read"


def test_a_refused_photo_says_its_own_sentence_and_a_surprise_says_the_fallback(monkeypatch):
    from kestrel.services import profile_photo
    from kestrel.web.pages.settings_prefs import PHOTO_REFUSED

    client = _fresh_app()
    files = {"photo": ("me.png", b"not a picture", "image/png")}
    said = client.post("/settings/prefs/avatar/photo", files=files).json()
    assert said["message"] == profile_photo.NOT_A_PICTURE

    async def boom(data):
        raise profile_photo.PhotoError(SECRET)

    monkeypatch.setattr(profile_photo, "save_async", boom)
    said = client.post("/settings/prefs/avatar/photo", files=files).json()
    assert said["message"] == PHOTO_REFUSED and SECRET not in said["message"]


def test_the_photo_refusals_list_holds_every_sentence_save_can_raise():
    from kestrel.services import profile_photo as pp

    src = (__import__("pathlib").Path(pp.__file__)).read_text(encoding="utf-8")
    raised = re.findall(r"raise PhotoError\(([^)]*)\)", src)
    assert raised and all(arg in {"TOO_BIG", "TOO_BIG_PIXELS", "NOT_A_PICTURE", "NOT_SAVED"}
                          for arg in raised), raised
    assert set(pp.REFUSALS) == {pp.TOO_BIG, pp.TOO_BIG_PIXELS, pp.NOT_A_PICTURE, pp.NOT_SAVED}


def test_the_weeks_undo_failure_is_a_fixed_sentence(monkeypatch):
    from kestrel.services import profile_edit as pe
    from kestrel.web import settings_io as io
    from kestrel.web.pages import settings_roles

    def boom(*_a, **_k):
        raise pe.ProfileEditError(SECRET)

    monkeypatch.setattr(pe, "save_section", boom)
    monkeypatch.setattr(settings_roles, "load_profile", lambda track: None)
    client = _fresh_app()
    # The undo only runs while the profile still holds what the save wrote (cur == now).
    r = client.post("/settings/roles/weeks/undo?prev=14&now=None")
    assert r.json()["message"] == io.SAVE_FAILED
    assert SECRET not in r.text


def test_the_access_undo_failure_shows_an_authored_sentence_or_the_fallback(monkeypatch):
    from kestrel.services import lan_access
    from kestrel.web.pages.settings_privacy import LAN_UNDO_FAILED

    client = _fresh_app()
    monkeypatch.setattr(lan_access, "file_value", lambda: True)

    def refused(fixed):
        def _raise(value):
            raise lan_access.LanAccessError(fixed)

        monkeypatch.setattr(lan_access, "set_bind_all", _raise)
        return client.post("/settings/privacy/access/undo?was=0&now=1").json()["message"]

    assert refused(lan_access.NOT_YAML) == lan_access.NOT_YAML
    said = refused(f"settings.yml could not be read: {SECRET}")
    assert said == LAN_UNDO_FAILED and SECRET not in said
    assert set(lan_access.REFUSALS) == {
        lan_access.SERVER_SHAPE, lan_access.NOT_YAML, lan_access.NOT_A_MAPPING,
        lan_access.NOT_VERIFIED,
    }


# ------------------------------------------------------------------------------ main's remaining
# alerts (D-SEC-5): the same rule for the sinks the first pass did not reach.
PLANTED = "/srv/secret/key"


def _nothing_of_it(text: str) -> None:
    assert PLANTED not in text and "/srv/secret" not in text


def _unreadable_upload(page_module, monkeypatch):
    """Make reading an uploaded file fail with an OS error that names PLANTED."""
    import contextlib

    @contextlib.asynccontextmanager
    async def closing(upload):
        raise PermissionError(errno.EACCES, "Permission denied", PLANTED)
        yield upload  # pragma: no cover

    monkeypatch.setattr(page_module, "closing_upload", closing)


def _zip_upload():
    return {"backup_file": ("b.zip", b"PK", "application/zip")}


# --- Settings > Your data: restore
def test_restore_upload_read_failure_shows_a_fixed_reason(monkeypatch):
    from kestrel.web.pages import settings as settings_page

    _unreadable_upload(settings_page, monkeypatch)
    r = _fresh_app().post("/settings/restore", files=_zip_upload())
    assert "Could not read the upload: permission was denied" in r.text
    _nothing_of_it(r.text)


def test_restore_refusal_shows_the_listed_sentence_or_the_fallback(monkeypatch):
    from kestrel.services import backup as bs

    client = _fresh_app()

    def refused(text: str) -> str:
        def _raise(path):
            raise bs.InvalidBackupError(text)

        monkeypatch.setattr(bs, "restore_backup", _raise)
        return client.post("/settings/restore", files=_zip_upload()).text

    assert html.escape(bs.NEWER_BACKUP) in refused(bs.NEWER_BACKUP)
    said = refused(f"Refusing an unsafe path entry in the backup: {PLANTED}")
    assert html.escape(bs.RESTORE_FALLBACK) in said
    _nothing_of_it(said)


@pytest.mark.parametrize(
    "exc, reason",
    [
        (PermissionError(errno.EACCES, "Permission denied", PLANTED), "permission was denied"),
        (sqlite3.DatabaseError(PLANTED), "the database could not be read"),
        (zipfile.BadZipFile(PLANTED), "the file is not a usable zip archive"),
    ],
)
def test_restore_failure_shows_a_fixed_reason(monkeypatch, exc, reason):
    from kestrel.services import backup as bs

    def broken(path):
        raise exc

    monkeypatch.setattr(bs, "restore_backup", broken)
    r = _fresh_app().post("/settings/restore", files=_zip_upload())
    assert f"Restore failed: {reason}" in r.text
    _nothing_of_it(r.text)


def test_restore_never_quotes_an_archive_entry_name(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("kestrel.db", b"x")
        z.writestr(f"config/{PLANTED}/../../evil.yml", "pwn: true")
    r = _fresh_app().post(
        "/settings/restore",
        files={"backup_file": ("evil.zip", evil.read_bytes(), "application/zip")},
    )
    assert "Refusing an unsafe path entry in the backup." in r.text
    _nothing_of_it(r.text)


# --- Backup page
def test_backup_download_failure_shows_a_fixed_reason(monkeypatch):
    from kestrel.services import backup as bs

    def boom(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left", PLANTED)

    monkeypatch.setattr(bs, "create_backup_to_temp", boom)
    r = _fresh_app().get("/backup/download")
    assert r.status_code == 500
    assert r.text == (
        '<div class="warnbox">Could not build the backup: there is no space left on the disk</div>'
    )


def test_profile_upload_read_failure_shows_a_fixed_reason(monkeypatch):
    from kestrel.web.pages import backup as backup_page

    _unreadable_upload(backup_page, monkeypatch)
    r = _fresh_app().post(
        "/backup/import-profile", files={"profile_file": ("profile.yml", b"x", "text/yaml")}
    )
    assert "Could not read the upload: permission was denied" in r.text
    _nothing_of_it(r.text)


def test_profile_import_refusal_shows_the_listed_sentence_or_the_fallback(monkeypatch):
    from kestrel.services import backup as bs

    client = _fresh_app()
    files = {"profile_file": ("profile.yml", b"a: 1\n", "text/yaml")}

    def refused(text: str) -> str:
        def _raise(data):
            raise bs.InvalidProfileError(text)

        monkeypatch.setattr(bs, "import_profile", _raise)
        return client.post("/backup/import-profile", files=files).text

    assert html.escape(bs.PROFILE_NO_TRACK) in refused(bs.PROFILE_NO_TRACK)
    said = refused(f"The profile could not be read from {PLANTED}")
    assert html.escape(bs.PROFILE_FALLBACK) in said
    _nothing_of_it(said)


def test_profile_import_names_the_yaml_line_and_only_the_line():
    r = _fresh_app().post(
        "/backup/import-profile",
        files={"profile_file": ("profile.yml", b"employment: [unclosed\n", "text/yaml")},
    )
    assert re.search(r"The file is not valid YAML \(line \d{1,6}\)\.", r.text)
    assert "unclosed" not in r.text  # the parser quotes the line; the page does not


def test_profile_import_write_failure_shows_a_fixed_reason(monkeypatch):
    from kestrel.services import backup as bs

    def boom(data):
        raise OSError(errno.EROFS, "Read-only file system", PLANTED)

    monkeypatch.setattr(bs, "import_profile", boom)
    r = _fresh_app().post(
        "/backup/import-profile", files={"profile_file": ("profile.yml", b"a: 1\n", "text/yaml")}
    )
    assert "Import failed: the disk is read-only" in r.text
    _nothing_of_it(r.text)


def test_the_refusal_lists_hold_every_sentence_restore_and_import_can_raise():
    import inspect

    from kestrel.services import backup as bs

    src = inspect.getsource(bs)
    backup_args = re.findall(r"raise InvalidBackupError\((\w+)\)", src)
    assert len(backup_args) == src.count("raise InvalidBackupError(")  # no inline text
    assert {getattr(bs, name) for name in backup_args} <= set(bs.BACKUP_REFUSALS)
    profile_args = re.findall(r"raise InvalidProfileError\((\w+)\)", src)
    inline = src.count("raise InvalidProfileError(") - len(profile_args)
    assert inline == 1  # the YAML sentence, whose only variable part is the line number
    assert {getattr(bs, name) for name in profile_args} <= set(bs.PROFILE_REFUSALS)
    assert bs.PROFILE_NOT_YAML_AT.format(line=3) == "The file is not valid YAML (line 3)."


# --- Setup: record the hardware profile
@pytest.mark.parametrize("models_panel", [False, True])
def test_setup_profile_failure_shows_a_fixed_reason(monkeypatch, models_panel):
    """`_fail` builds the response two ways (the Models panel swap and the plain status code)."""
    from kestrel.services import setup_state as ss

    def boom(name, reason=""):
        raise PermissionError(errno.EACCES, "Permission denied", PLANTED)

    monkeypatch.setattr(ss, "record_profile", boom)
    client = _fresh_app()
    sentence = "could not record the profile: permission was denied"
    if models_panel:
        r = client.post("/setup/profile", data={"profile": "x", "from": "models"}, headers=HX)
        assert r.status_code == 200
        assert r.text == f'<p role="status" class="bad">{sentence}</p>'
    else:
        r = client.post("/setup/profile", data={"profile": "x"})
        assert r.status_code == 500 and r.text == sentence
    _nothing_of_it(r.text)


# --- Documents: accept an import
def _accept(monkeypatch, accept):
    from kestrel.docs import imports
    from kestrel.llm.schemas import P7ResumeImport

    monkeypatch.setattr(
        P7ResumeImport, "model_validate_json", classmethod(lambda cls, text: object())
    )
    monkeypatch.setattr(imports, "accept_import", accept)
    return _fresh_app().post(
        "/api/documents/import/accept",
        data={"proposal_json": "{}", "text": "x", "track": "employment", "doc_kind": "resume"},
    )


def test_import_rejection_is_one_fixed_sentence(monkeypatch):
    from kestrel.docs.imports import ImportRejected
    from kestrel.web.pages.documents import IMPORT_REJECTED

    async def refuse(*_a, **_k):
        raise ImportRejected(f"placed line is not in the source: {PLANTED!r}")

    r = _accept(monkeypatch, refuse)
    assert html.escape(IMPORT_REJECTED) in r.text
    _nothing_of_it(r.text)


def test_import_accept_failure_shows_a_fixed_reason(monkeypatch):
    async def boom(*_a, **_k):
        raise OperationalError(PLANTED, {}, Exception(PLANTED))

    r = _accept(monkeypatch, boom)
    assert "Could not accept the import: the database could not be read" in r.text
    _nothing_of_it(r.text)


def test_import_accept_with_a_bad_proposal_shows_no_input_value():
    """pydantic's error text quotes the input; the page must not."""
    r = _fresh_app().post(
        "/api/documents/import/accept",
        data={"proposal_json": PLANTED, "text": "x", "track": "employment", "doc_kind": "resume"},
    )
    assert "Could not accept the import: " + GENERIC in r.text
    _nothing_of_it(r.text)


# --- About: diagnostics download
def test_diagnostics_download_failure_shows_a_fixed_reason(monkeypatch):
    from kestrel.services import diagnostics

    def boom(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left", PLANTED)

    monkeypatch.setattr(diagnostics, "build_bundle_to_temp", boom)
    r = _fresh_app().get("/about/diagnostics")
    assert r.status_code == 500
    assert "Could not build the diagnostics bundle: there is no space left on the disk" in r.text
    _nothing_of_it(r.text)


# --- Score again
def test_score_again_failure_shows_a_fixed_reason(monkeypatch):
    from kestrel.web.pages import rescore

    async def boom(*_a, **_k):
        raise PermissionError(errno.EACCES, "Permission denied", PLANTED)

    monkeypatch.setattr(rescore, "enqueue_rescore", boom)
    r = _fresh_app().post("/rescore", data={"opp_ids": "1", "model_choice": "same"})
    assert "Not queued: permission was denied" in r.text
    _nothing_of_it(r.text)


def test_score_again_model_list_failure_names_the_provider_and_nothing_else(monkeypatch):
    from types import SimpleNamespace

    from kestrel.llm import providers as prov
    from kestrel.services import hosted_models as hm
    from kestrel.web.pages import rescore

    async def boom(*_a, **_k):
        raise hm.ModelListError(f"The list failed at {PLANTED}")

    preset = next(iter(prov.PRESETS.values()))
    backend = SimpleNamespace(api_key="k", api_key_env="SOME_API_KEY")
    settings = SimpleNamespace(llm=SimpleNamespace(hosted_mode=SimpleNamespace(active=True)))
    monkeypatch.setattr(hm, "catalog_backend", lambda s: (preset["backend"], backend))
    monkeypatch.setattr(hm, "fetch_models", boom)
    override, _label, why = asyncio.run(rescore._override_for(settings, "hosted:vendor/x"))
    assert override is None
    assert why == (
        f"The model list from {preset['label']} could not be fetched. Check the key and your "
        "connection under Settings > Hosted."
    )
    _nothing_of_it(why)


# --- Role page: the decision panel
def test_decision_panel_for_a_vanished_role_does_not_echo_the_callers_error(monkeypatch):
    from kestrel.web import routes_pages
    from kestrel.web.pages import opportunity_actions as oa
    from tests.test_pursuit_board import _fresh, _seed

    async def bad(*_a, **_k):
        raise UserFacingError(f"Could not write {PLANTED}")

    async def gone(session, opp_id):
        return None

    monkeypatch.setattr(oa, "add_to_list", bad)
    monkeypatch.setattr(routes_pages, "decision_context", gone)
    client = _fresh()
    opp_id, _pid = _seed(track="employment", status="shortlisted")
    r = client.post(f"/opportunities/{opp_id}/add")
    assert "Opportunity not found." in r.text
    _nothing_of_it(r.text)


# --- Model check promote: tuning a model
def _gpu_machine_with(monkeypatch, tag: str) -> None:
    """The Models screen tests' world: a GPU machine, Ollama up, the base models present."""
    from kestrel.services import setup_state as ss
    from tests.test_models_screen import GPU, _with_tags

    monkeypatch.setattr(ss, "detected_hardware", lambda: GPU)
    _with_tags(monkeypatch, tag)


def test_bench_promote_tuning_failure_shows_no_exception_text(monkeypatch):
    import httpx
    import respx

    _gpu_machine_with(monkeypatch, "qwen3:14b")
    client = _fresh_app()
    with respx.mock as router:
        router.post("http://127.0.0.1:11434/api/create").mock(
            side_effect=httpx.ConnectError(PLANTED)
        )
        r = client.post("/bench/promote", data={"tier": "tier2", "model": "qwen3:14b"})
    assert "could not tune qwen3:14b for this step: " + GENERIC in r.text
    _nothing_of_it(r.text)


def test_bench_promote_modelfile_failure_shows_no_exception_text(monkeypatch):
    from kestrel.llm import profiles

    def boom(*_a, **_k):
        raise ValueError(PLANTED)

    _gpu_machine_with(monkeypatch, "qwen3:14b")
    monkeypatch.setattr(profiles, "render_modelfile", boom)
    r = _fresh_app().post("/bench/promote", data={"tier": "tier2", "model": "qwen3:14b"})
    assert "cannot tune a model for this step on this computer: " + GENERIC in r.text
    _nothing_of_it(r.text)


# ------------------------------------------------------------------------------ the guard
def test_the_flagged_response_builders_no_longer_pass_plain_error_into_a_response():
    """The scanner follows `exc` through `plain_error` into the body. These files build their
    bodies from `fixed_reason`, a constant or a setting, so the helper is not called there."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "kestrel" / "web"
    for rel in ("routes_pages.py", "routes_control.py", "pages/runs_page.py", "pages/backup.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "plain_error" not in text, rel
