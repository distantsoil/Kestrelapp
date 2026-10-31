"""First name and last name (D-UI-117): the split, the profile file, the loader, the resume header
and the greeting. `first_name` is required; `last_name` may be empty; `full_name` is derived."""

from __future__ import annotations

import pytest
import yaml

from kestrel.pipeline.profile import load_profile
from kestrel.services import names
from kestrel.services import profile_edit as pe


@pytest.mark.parametrize(
    ("full", "first", "last"),
    [("Jordan A. Rivera", "Jordan", "A. Rivera"), ("Cher", "Cher", ""),
     ("  Maria   de la Cruz ", "Maria", "de la Cruz"), ("", "", ""), (None, "", ""),
     ("Pat Example", "Pat", "Example")],
)
def test_split_on_the_first_space(full, first, last):
    assert names.split_full_name(full) == (first, last)


def test_join_trims_and_skips_an_empty_last_name():
    assert names.join_full_name("Pat", "Example") == "Pat Example"
    assert names.join_full_name(" Pat ", "") == "Pat"
    assert names.join_full_name(None, None) == ""


def test_normalize_prefers_the_two_fields_and_rederives_full_name():
    out = names.normalize_name_fields({"first_name": "Pat", "last_name": "Q", "full_name": "old"})
    assert out["full_name"] == "Pat Q"


def test_normalize_reads_a_legacy_full_name_and_keeps_it_as_written():
    out = names.normalize_name_fields({"full_name": "Jordan A. Rivera"})
    assert (out["first_name"], out["last_name"], out["full_name"]) == (
        "Jordan", "A. Rivera", "Jordan A. Rivera"
    )


def test_normalize_invents_nothing_for_a_block_without_a_name():
    block = {"track": "employment"}
    assert names.normalize_name_fields(block) == block and "first_name" not in block


def test_the_greeting_uses_the_first_name():
    assert names.greeting_name({"first_name": "Pat", "last_name": "Example"}) == "Pat"
    assert names.greeting_name({"full_name": "Jordan A. Rivera"}) == "Jordan"
    assert names.greeting_name({}) == "" and names.greeting_name(None) == ""


# ------------------------------------------------------------------- the profile file / form
def test_first_name_is_required_on_the_about_tab():
    with pytest.raises(pe.ProfileEditError, match="first name"):
        pe.section_from_form("about", {"last_name": "Example"})
    with pytest.raises(pe.ProfileEditError):
        pe.section_from_form("about", {})


def test_the_about_form_writes_both_names_and_the_derived_full_name():
    values = pe.section_from_form("about", {"first_name": " Pat ", "last_name": "Example"})
    assert (values["first_name"], values["last_name"], values["full_name"]) == (
        "Pat", "Example", "Pat Example"
    )
    pe.save_section("employment", "about", values)
    data = yaml.safe_load(pe.profile_path().read_text(encoding="utf-8"))["employment"]
    assert data["first_name"] == "Pat" and data["last_name"] == "Example"
    assert data["full_name"] == "Pat Example"
    loaded = load_profile("employment")
    assert (loaded.first_name, loaded.last_name, loaded.full_name) == (
        "Pat", "Example", "Pat Example"
    )


def test_a_last_name_may_be_empty():
    values = pe.section_from_form("about", {"first_name": "Cher"})
    assert values["last_name"] == "" and values["full_name"] == "Cher"


def test_an_older_form_that_posts_one_full_name_is_split():
    values = pe.section_from_form("about", {"full_name": "Jordan A. Rivera"})
    assert (values["first_name"], values["last_name"]) == ("Jordan", "A. Rivera")


def test_a_legacy_profile_file_loads_with_both_names_and_the_original(tmp_path):
    pe.profile_path().parent.mkdir(parents=True, exist_ok=True)
    pe.profile_path().write_text(
        "employment:\n  track: employment\n  full_name: Maria de la Cruz\n", encoding="utf-8"
    )
    loaded = load_profile("employment")
    assert (loaded.first_name, loaded.last_name) == ("Maria", "de la Cruz")
    assert loaded.full_name == "Maria de la Cruz"
    state = pe.load_state()
    assert state.tracks["employment"]["first_name"] == "Maria"
    assert pe.about_missing(state) == []  # the legacy name counts as a first name


def test_about_missing_follows_the_first_name():
    pe.save_section("employment", "about", pe.section_from_form("about", {"first_name": "Pat"}))
    assert pe.about_missing() == []
    pe.profile_path().write_text("employment:\n  track: employment\n  last_name: X\n",
                                 encoding="utf-8")
    assert pe.about_missing() == ["employment"]


def test_the_shipped_example_is_fictional_and_split():
    from kestrel import paths

    data = yaml.safe_load(paths.config_file("profile.example.yml").read_text(encoding="utf-8"))
    for track in ("employment", "consulting"):
        assert data[track]["first_name"] == "Jordan" and data[track]["last_name"] == "Rivera"
        assert "full_name" not in data[track]


# -------------------------------------------------------------------------- the resume header
def test_the_resume_header_prints_both_names():
    from types import SimpleNamespace

    from kestrel.docs.catalog import document_header

    pe.save_section("employment", "about", pe.section_from_form(
        "about", {"first_name": "Pat", "last_name": "Example", "email": "pat@example.com"}))
    header = document_header(SimpleNamespace(track="employment", header={}))
    assert header["name"] == "Pat Example" and header["email"] == "pat@example.com"


def test_the_resume_header_reads_a_legacy_full_name_too():
    from types import SimpleNamespace

    from kestrel.docs.catalog import document_header

    pe.profile_path().parent.mkdir(parents=True, exist_ok=True)
    pe.profile_path().write_text(
        "employment:\n  track: employment\n  full_name: Pat Example\n", encoding="utf-8"
    )
    assert document_header(SimpleNamespace(track="employment", header={}))["name"] == "Pat Example"
