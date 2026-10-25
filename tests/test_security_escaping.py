"""Code-scanning fixes: form values echoed in an error body are escaped, and the assess
progress panel never assigns user text through innerHTML. Offline."""

from __future__ import annotations

from pathlib import Path

from tests.test_documents_review_fixes import _client
from tests.test_pursuit_board import _fresh, _seed
from tests.test_stream3_flow import _seed_tailor
from tests.test_web_phase13 import _dispose_engines  # noqa: F401  (autouse fixture)

PAYLOAD = "<script>alert(1)</script>"


def test_swap_error_answers_in_plain_text():
    render_id, _ = _seed_tailor()
    c = _client()
    r = c.post(f"/renders/{render_id}/swap", data={"slot_key": PAYLOAD, "variant_key": "x"})
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("text/plain")  # text, so it never runs
    assert PAYLOAD in r.text


def test_wording_error_answers_in_plain_text():
    render_id, _ = _seed_tailor()
    c = _client()
    r = c.post(f"/renders/{render_id}/wording", data={"slot_key": PAYLOAD, "text": "my words"})
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("text/plain")  # text, so it never runs
    assert PAYLOAD in r.text


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



# ------------------------------------------------------------------ WP-08 (#21): remaining sinks
def _raise_secret(*_a, **_k):
    raise RuntimeError(SECRET_PATH)


async def _araise_secret(*_a, **_k):
    raise RuntimeError(SECRET_PATH)


def test_msg_param_never_echoes_arbitrary_text():
    c = _fresh()
    r = c.get("/settings/system?msg=Your+account+is+locked+call+555-0100")
    assert "555-0100" not in r.text and "account is locked" not in r.text
    r = c.get("/settings?msg=Your+account+is+locked+call+555-0100")
    assert "555-0100" not in r.text and 'data-testid="flash"' not in r.text


def test_msg_param_renders_only_a_known_code():
    from kestrel.web.flash import FLASH_MESSAGES

    code = "reset_confirm_needed"
    c = _fresh()
    r = c.get(f"/settings/system?msg={code}")
    assert FLASH_MESSAGES[code] in r.text
    r = c.get(f"/settings?msg={code}")
    assert FLASH_MESSAGES[code] in r.text and 'data-testid="flash"' in r.text


def test_flash_messages_have_no_em_dash_and_redirects_carry_codes_only():
    from kestrel.web.flash import FLASH_MESSAGES

    assert all("—" not in m for m in FLASH_MESSAGES.values())
    r = _fresh().post("/setup/mode", data={"mode": "local"}, follow_redirects=False)
    assert r.headers["location"].split("?msg=")[1].split("#")[0] in FLASH_MESSAGES


def test_user_facing_error_text_is_shown_and_others_are_not():
    from kestrel.errors import UserFacingError
    from kestrel.web.errors import plain_error

    assert plain_error(UserFacingError("Pick a model first.")) == "Pick a model first."
    assert SECRET_PATH not in plain_error(ValueError(SECRET_PATH))


def test_render_review_failure_shows_no_exception_text(monkeypatch):
    from kestrel.docs import service

    monkeypatch.setattr(service, "render_document", _araise_secret)
    r = _fresh().get("/renders/new?master_id=1")
    assert "secret-dir" not in r.text and SECRET_PATH not in r.text


def test_run_page_load_failure_shows_no_exception_text(monkeypatch):
    from kestrel.db.models import Run
    from kestrel.web.pages import runs_detail

    _fresh()

    class _S:
        async def get(self, *_a, **_k):
            raise RuntimeError(SECRET_PATH)

    class _Ctx:
        async def __aenter__(self):
            return _S()

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(runs_detail, "sessionmaker", lambda: (lambda: _Ctx()))
    assert Run
    r = _fresh().get("/runs/1")
    assert "secret-dir" not in r.text


def test_prep_build_failure_shows_no_exception_text(monkeypatch):
    from kestrel.interview import prep as prep_mod

    monkeypatch.setattr(prep_mod, "assemble_prep_pack", _araise_secret)
    c = _fresh()
    opp_id, _pid = _seed(track="employment", status="shortlisted")
    r = c.post(f"/prep/{opp_id}/build")
    assert "secret-dir" not in r.text


def test_convert_errors_carry_no_library_text():
    import pytest

    from kestrel.docs.convert import ConvertError, convert_to_markdown

    with pytest.raises(ConvertError) as err:
        convert_to_markdown(b"PK\x03\x04 not really a docx", filename="resume.docx")
    assert "File is not a zip file" not in str(err.value)  # the library's own text


def test_hosted_key_write_refusal_is_authored(monkeypatch):
    from kestrel.llm import providers as prov

    monkeypatch.setattr(prov, "write_secret", lambda *_a, **_k: (_ for _ in ()).throw(
        ValueError(SECRET_PATH)))
    r = _fresh().post("/settings/hosted/key", data={"backend": "openrouter", "api_key": "k"})
    assert "secret-dir" not in r.text


def test_flash_redirect_drops_an_unknown_code():
    # CodeQL review on the PR: form input built the code (mode_<choice>); only known codes may ride
    # into the redirect URL.
    from kestrel.web.flash import FLASH_MESSAGES, flash_redirect

    known = next(iter(FLASH_MESSAGES))
    assert flash_redirect("/settings", known, "system").headers["location"].endswith(
        f"?msg={known}#system")
    assert flash_redirect("/settings", "mode_<x>", "system").headers["location"] == (
        "/settings#system")
