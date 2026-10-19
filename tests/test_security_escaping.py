"""Code-scanning fixes: form values echoed in an error body are escaped, and the assess
progress panel never assigns user text through innerHTML. Offline."""

from __future__ import annotations

from pathlib import Path

from tests.test_documents_review_fixes import _client
from tests.test_pursuit_board import _fresh, _seed
from tests.test_stream3_flow import _seed_tailor
from tests.test_web_phase13 import _dispose_engines  # noqa: F401  (autouse fixture)

PAYLOAD = "<script>alert(1)</script>"


def test_swap_error_escapes_the_slot_and_variant_keys():
    render_id, _ = _seed_tailor()
    c = _client()
    r = c.post(f"/renders/{render_id}/swap", data={"slot_key": PAYLOAD, "variant_key": "x"})
    assert r.status_code == 400
    assert PAYLOAD not in r.text and "&lt;script&gt;" in r.text


def test_wording_error_escapes_the_slot_key():
    render_id, _ = _seed_tailor()
    c = _client()
    r = c.post(f"/renders/{render_id}/wording", data={"slot_key": PAYLOAD, "text": "my words"})
    assert r.status_code == 400
    assert PAYLOAD not in r.text and "&lt;script&gt;" in r.text


def test_pursuit_status_error_escapes_the_stage_on_a_plain_post():
    c = _fresh()
    _, pid = _seed()
    r = c.post(f"/pursuits/{pid}/status", data={"to_status": PAYLOAD})
    assert r.status_code == 400
    assert PAYLOAD not in r.text and "&lt;script&gt;" in r.text


def test_assess_progress_panel_does_not_build_html_from_the_model_name():
    src = (Path(__file__).resolve().parents[1]
           / "kestrel/web/templates/partials/assess_model.html").read_text(encoding="utf-8")
    start = src.index("htmx:beforeRequest")
    assert "innerHTML" not in src[start:src.index("dlg.remove()", start)]


# ------------------------------------------------------------------ exception text in responses
SECRET_PATH = r"C:\Users\someone\AppData\secret-dir\kestrel.key"


def _boom(*_a, **_k):
    raise PermissionError(13, "Permission denied", SECRET_PATH)


def test_backup_download_failure_shows_no_file_path(monkeypatch):
    from kestrel.services import backup as backup_service

    monkeypatch.setattr(backup_service, "create_backup_to_temp", _boom)
    r = _client().get("/backup/download")
    assert r.status_code == 500
    assert "secret-dir" not in r.text and "permission was denied" in r.text


def test_diagnostics_download_failure_shows_no_file_path(monkeypatch):
    from kestrel.services import diagnostics

    monkeypatch.setattr(diagnostics, "build_bundle_to_temp", _boom)
    r = _client().get("/about/diagnostics")
    assert "secret-dir" not in r.text


def test_setup_profile_failure_escapes_and_hides_the_path(monkeypatch):
    from kestrel.services import setup_state as ss

    r = _client().post("/setup/profile", data={"profile": PAYLOAD})
    assert PAYLOAD not in r.text
    monkeypatch.setattr(ss, "record_profile", _boom)
    r = _client().post("/setup/profile", data={"profile": "x"})
    assert r.status_code == 500 and "secret-dir" not in r.text


def test_add_to_list_failure_shows_no_exception_text(monkeypatch):
    from kestrel.web.pages import opportunity_actions as oa

    async def _bad(*_a, **_k):
        raise RuntimeError(SECRET_PATH)

    monkeypatch.setattr(oa, "add_to_list", _bad)
    c = _fresh()
    opp_id, _pid = _seed(track="employment", status="shortlisted")
    r = c.post(f"/opportunities/{opp_id}/add")
    assert "secret-dir" not in r.text


def test_corrupt_backup_message_does_not_name_the_upload_path(tmp_path):
    import pytest

    from kestrel.services.backup import InvalidBackupError, restore_backup

    bad = tmp_path / "private-folder-name.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(InvalidBackupError) as err:
        restore_backup(bad)
    assert "private-folder-name" not in str(err.value)


def test_plain_error_never_carries_a_path():
    from kestrel.web.errors import plain_error

    assert SECRET_PATH not in plain_error(PermissionError(13, "Permission denied", SECRET_PATH))
    assert SECRET_PATH not in plain_error(OSError(5, "I/O error", SECRET_PATH))
    assert SECRET_PATH not in plain_error(RuntimeError(SECRET_PATH))
    assert plain_error(RuntimeError(SECRET_PATH)) == (
        "an unexpected problem (RuntimeError); the details are in the log"
    )


def test_unknown_setup_profile_names_it_without_the_exception_text():
    r = _client().post("/setup/profile", data={"profile": "<b>nope</b>"})
    assert r.status_code == 400
    assert r.text == "Unknown hardware profile: &lt;b&gt;nope&lt;/b&gt;."

