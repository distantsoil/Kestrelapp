"""Messages and Undo (D-SEC-4): what a refusal says is one of Kestrel's own sentences for the real
cause, never the text of an exception.

* `authored_error` with a `{line}` slot (a YAML line number is the only thing it rebuilds),
* the shortest-project Undo shows why the profile could not be saved,
* a refused Settings value says the sentence for what was wrong with it,
* Undo of a Profile save when the earlier version cannot be read (the real path, no stand-in).

Offline: TestClient on the real app, a throwaway data home, no browser, no model, no network."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from kestrel import paths
from kestrel.services import profile_edit as pe
from kestrel.web import settings_io as io
from kestrel.web.errors import authored_error
from kestrel.web.pages import settings_prefs as prefs
from tests.test_hosted_mode import _fresh_app

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

SECRET = r"C:\Users\someone\AppData\secret-dir\kestrel.key"


# ------------------------------------------------------------------------------ authored_error
SLOTTED = ("profile.yml is not valid YAML.", "profile.yml is not valid YAML (line {line}).",
           "{line} lines were skipped.")


@pytest.mark.parametrize(("text", "shown"), [
    ("profile.yml is not valid YAML.", "profile.yml is not valid YAML."),
    ("profile.yml is not valid YAML (line 12).", "profile.yml is not valid YAML (line 12)."),
    ("profile.yml is not valid YAML (line 1).", "profile.yml is not valid YAML (line 1)."),
    ("profile.yml is not valid YAML (line 007).", "profile.yml is not valid YAML (line 7)."),
    ("3 lines were skipped.", "3 lines were skipped."),  # a slot at the start
])
def test_a_line_number_is_the_one_thing_authored_error_rebuilds(text, shown):
    assert authored_error(ValueError(text), SLOTTED, "fallback") == shown


@pytest.mark.parametrize("text", [
    "profile.yml is not valid YAML (line ).",
    "profile.yml is not valid YAML (line 12a).",
    "profile.yml is not valid YAML (line -3).",
    "profile.yml is not valid YAML (line 1234567).",  # more than six digits
    "profile.yml is not valid YAML (line \uff11\uff12).",  # full-width digits are not digits here
    f"profile.yml is not valid YAML (line {SECRET}).",
    "profile.yml is not valid YAML (line 12). " + SECRET,
    f"{SECRET} is not valid YAML (line 12).",
    "(line 12).",
])
def test_anything_else_in_the_slot_shows_the_fallback(text):
    said = authored_error(ValueError(text), SLOTTED, "fallback")
    assert said == "fallback" and SECRET not in said


# ----------------------------------------------------------------------- the weeks Undo, by cause
def _write_profile(text: str) -> Path:
    paths.ensure_dirs()
    target = pe.profile_path()
    target.write_text(text, encoding="utf-8")
    return target


GOOD = yaml.safe_dump({"consulting": {"track": "consulting", "is_active": True,
                                      "consulting_terms": {"min_engagement_days": 14}}})


def _weeks_undo(client):
    # The Undo only runs while the profile still holds what the save wrote: 14 days.
    return client.post("/settings/roles/weeks/undo?prev=7&now=14").json()


def test_a_profile_that_is_not_valid_yaml_says_so_with_its_line(monkeypatch):
    client = _fresh_app()
    target = _write_profile(GOOD)
    from kestrel.web.pages import settings_roles

    monkeypatch.setattr(settings_roles, "load_profile", _profile_at(14))
    target.write_text("consulting: [unclosed\nnext: 1\n", encoding="utf-8")
    said = _weeks_undo(client)
    assert said["ok"] is False
    assert re.fullmatch(r"profile\.yml is not valid YAML \(line \d+\)\.", said["message"]), said
    assert "unclosed" not in said["message"]  # the parser quotes the file; we do not


def test_a_profile_the_loader_refuses_says_the_previous_file_was_kept(monkeypatch):
    client = _fresh_app()
    target = _write_profile(GOOD)
    from kestrel.pipeline import profile as pipeline_profile
    from kestrel.web.pages import settings_roles

    monkeypatch.setattr(settings_roles, "load_profile", _profile_at(14))
    monkeypatch.setattr(pipeline_profile, "load_profile", lambda track: None)
    before = target.read_bytes()
    said = _weeks_undo(client)
    assert said == {"ok": False, "message": pe.NOT_LOADED}
    assert target.read_bytes() == before  # and it did: the previous file is back in place


def test_a_profile_that_cannot_be_opened_says_why_in_our_words(monkeypatch):
    client = _fresh_app()
    target = _write_profile(GOOD)
    from kestrel.web.pages import settings_roles

    monkeypatch.setattr(settings_roles, "load_profile", _profile_at(14))
    real = Path.read_text

    def denied(self, *args, **kwargs):
        if self == target:
            raise PermissionError(13, "Permission denied", SECRET)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    said = _weeks_undo(client)
    assert said == {"ok": False, "message": pe.UNREADABLE_DENIED}
    assert SECRET not in said["message"]


def test_a_failure_that_is_not_one_of_ours_shows_the_fixed_sentence(monkeypatch):
    client = _fresh_app()
    _write_profile(GOOD)
    from kestrel.web.pages import settings_roles

    monkeypatch.setattr(settings_roles, "load_profile", _profile_at(14))

    def boom(*_a, **_k):
        raise pe.ProfileEditError(f"{SECRET} is not valid YAML (line 3).")

    monkeypatch.setattr(pe, "save_section", boom)
    said = _weeks_undo(client)
    assert said == {"ok": False, "message": io.SAVE_FAILED}


def _profile_at(days: int):
    from types import SimpleNamespace

    ns = SimpleNamespace(consulting_terms=SimpleNamespace(min_engagement_days=days))
    return lambda track: ns


def test_every_save_refusal_sentence_is_raised_where_it_is_written_down():
    """The list the Undo may show is the list of sentences profile_edit raises."""
    src = Path(pe.__file__).read_text(encoding="utf-8")
    assert "raise ProfileEditError(NOT_LOADED)" in src
    assert 'f"{path.name} is not valid YAML{yaml_where(exc)}."' in src
    assert 'f"{path.name} could not be read: {plain_os_reason(exc)}"' in src
    assert pe.NOT_YAML_AT.replace("{line}", "5") == "profile.yml is not valid YAML (line 5)."


# --------------------------------------------------------------- a refused value, by its cause
def _save(client, key, value):
    return client.post("/settings/ui", json={"key": key, "value": value}).json()


@pytest.mark.parametrize(("value", "message"), [
    ([], "Keep at least one scene checked."),
    (["atlantis"], prefs.SCENES_NOT_LISTED),
    (["mountains", "atlantis"], prefs.SCENES_NOT_LISTED),
    ("mountains", prefs.SCENES_NOT_LISTED),
    (None, prefs.SCENES_NOT_LISTED),
])
def test_a_scene_list_is_refused_for_its_own_reason(value, message):
    said = _save(_fresh_app(), "ui.scene_rotation", value)
    assert said == {"ok": False, "message": message}
    assert "atlantis" not in said["message"]


QUIET_TIMES = prefs.REFUSED_VALUE["notify.quiet_hours"]


@pytest.mark.parametrize(("value", "message"), [
    ({"enabled": "yes", "start": "21:00", "end": "07:00"}, prefs.QUIET_NOT_ON_OFF),
    ({"enabled": 1, "start": "21:00", "end": "07:00"}, prefs.QUIET_NOT_ON_OFF),
    ({"enabled": True, "start": "9pm", "end": "07:00"}, QUIET_TIMES),
    ({"enabled": True, "start": "21:00", "end": "25:00"}, QUIET_TIMES),
    ("21:00-07:00", QUIET_TIMES),
])
def test_quiet_hours_are_refused_for_their_own_reason(value, message):
    assert _save(_fresh_app(), "notify.quiet_hours", value) == {"ok": False, "message": message}


def test_a_good_value_for_each_of_those_keys_still_saves():
    client = _fresh_app()
    assert _save(client, "ui.scene_rotation", ["mountains", "coast"])["ok"] is True
    good = {"enabled": True, "start": "21:00", "end": "07:00"}
    assert _save(client, "notify.quiet_hours", good)["ok"] is True


def test_the_new_sentences_have_no_em_dash():
    for sentence in (prefs.SCENES_NOT_LISTED, prefs.QUIET_NOT_ON_OFF, prefs.SOUND_LOCAL_ONLY,
                     prefs.SOUND_OFF_VOLUME, prefs.SOUND_NO_PLAYER, pe.NOT_LOADED, pe.NOT_YAML_AT):
        assert chr(0x2014) not in sentence


# ---------------------------------------------- Undo of a Profile save, earlier version unreadable
def test_restore_backup_with_an_earlier_version_the_loader_cannot_read(monkeypatch):
    """The real path: profile.yml.bak is there but is not valid YAML. Undo is refused with the
    typed error, and profile.yml is exactly what it was."""
    target = _write_profile(GOOD)
    bak = target.with_name(pe.PROFILE_BAK)
    bak.write_text("consulting: [unclosed\n", encoding="utf-8")
    before = target.read_bytes()
    with pytest.raises(pe.EarlierVersionUnreadable) as caught:
        pe.restore_backup()
    assert str(caught.value) == pe.EARLIER_VERSION_UNREADABLE
    assert isinstance(caught.value, pe.ProfileEditError)
    assert target.read_bytes() == before
    assert not target.with_name("profile.yml.editing").exists()  # no half-copied file left


def test_restore_backup_with_no_current_file_leaves_none_behind():
    target = pe.profile_path()
    paths.ensure_dirs()
    target.with_name(pe.PROFILE_BAK).write_text("consulting: [unclosed\n", encoding="utf-8")
    target.unlink(missing_ok=True)
    with pytest.raises(pe.EarlierVersionUnreadable):
        pe.restore_backup()
    assert not target.exists()


def test_profile_undo_answers_409_with_the_unreadable_sentence_for_the_real_failure():
    client = _fresh_app()
    target = _write_profile(GOOD)
    target.with_name(pe.PROFILE_BAK).write_text("consulting: [unclosed\n", encoding="utf-8")
    r = client.post("/profile/undo", data={"token": pe.undo_token()})
    assert r.status_code == 409 and r.text == pe.EARLIER_VERSION_UNREADABLE
    assert yaml.safe_load(target.read_text(encoding="utf-8")) == yaml.safe_load(GOOD)
