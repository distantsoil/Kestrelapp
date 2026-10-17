"""The Profile page (D-UI-21): Documents as a tab beside editors for the personal block, the
employment and consulting terms and the location rules, all writing profile.yml through the
real loader. Offline, on the per-test data home from conftest."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, reset_engine
from kestrel.pipeline.profile import load_profile
from kestrel.services import profile_edit as pe


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _consulting_on() -> None:
    """A new install is employment-only; the tests of the consulting tabs turn the track on."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel.db.engine import make_async_engine
    from kestrel.services.tracks import BOTH, save_enabled

    async def _go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                await save_enabled(s, BOTH)
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(_go())


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from kestrel.web import routes_control

    async def _down(*a, **k):
        return None

    monkeypatch.setattr(routes_control, "_probe_ollama", _down, raising=False)
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()
    pe.profile_path().unlink(missing_ok=True)
    yield
    asyncio.run(reset_engine())


# ------------------------------------------------------------------------------- the service
def test_no_profile_starts_blank_with_the_example_only_as_placeholders():
    """Release round review, finding 1: the example's terms are hints, never starting values,
    so saving an untouched tab stores nothing that filters."""
    st = pe.load_state()
    assert st.exists is False
    emp = st.tracks["employment"]
    assert emp.get("full_name") in (None, "")  # the fictional person is never shown
    assert emp["employment_terms"] == {}  # nothing copied from the example
    assert emp["geo_rules"] == {"remote_ok": True}
    assert emp["is_active"] is True
    assert st.examples["employment"]["employment_terms"]["target_titles"]  # placeholder source


def test_first_save_writes_only_what_was_entered_and_the_loader_accepts_it():
    form = {"full_name": "Pat Example", "email": "pat@example.com", "headline": "VP of IT",
            "must_have": "budget ownership\nsecurity mandate", "is_active": "on"}
    pe.save_section("employment", "about", pe.section_from_form("about", form))
    data = yaml.safe_load(pe.profile_path().read_text(encoding="utf-8"))
    assert set(data) == {"employment"}  # consulting stays absent until it is saved
    emp = data["employment"]
    assert emp["full_name"] == "Pat Example" and emp["must_have"] == ["budget ownership",
                                                                       "security mandate"]
    assert "narrative" not in emp and "proof_points" not in emp  # nothing fictional copied
    assert emp["requires_sponsorship"] is False
    assert load_profile("employment").full_name == "Pat Example"
    assert load_profile("consulting") is None


def test_saving_a_section_merges_and_keeps_the_rest_of_the_block_and_a_bak():
    pe.profile_path().write_text(
        "employment:\n  track: employment\n  full_name: Pat\n  narrative: long text\n"
        "  employment_terms:\n    target_titles: [VP of IT]\n    comp_floor_usd: 150000\n",
        encoding="utf-8",
    )
    form = {"target_titles": "Head of IT\nVP of IT", "anti_titles": "Intern",
            "seniority_floor": "director", "comp_floor_usd": "$190,000", "type_internship": "on"}
    pe.save_section("employment", "employment", pe.section_from_form("employment", form))
    data = yaml.safe_load(pe.profile_path().read_text(encoding="utf-8"))["employment"]
    assert data["narrative"] == "long text" and data["full_name"] == "Pat"
    terms = data["employment_terms"]
    assert terms["target_titles"] == ["Head of IT", "VP of IT"]
    assert terms["comp_floor_usd"] == 190000 and terms["seniority_floor"] == "director"
    assert terms["excluded_employment_types"] == ["internship"]
    assert terms["comp_target_usd"] is None  # blank clears, and the rule sees None: no floor
    assert pe.profile_path().with_name("profile.yml.bak").read_text(encoding="utf-8").startswith(
        "employment:"
    )


def test_bad_numbers_and_unknown_floors_are_refused_before_any_write():
    with pytest.raises(pe.ProfileEditError, match="whole number"):
        pe.section_from_form("employment", {"seniority_floor": "vp", "comp_floor_usd": "lots"})
    with pytest.raises(pe.ProfileEditError, match="seniority floor"):
        pe.section_from_form("employment", {"seniority_floor": "boss"})
    with pytest.raises(pe.ProfileEditError, match="negative"):
        pe.section_from_form("consulting", {"day_rate_floor": "-5"})
    assert not pe.profile_path().exists()


def test_metros_round_trip_and_the_name_is_always_an_alias():
    metros = pe.parse_metros("Austin: Round Rock, Cedar Park\nHouston\n: nothing\n")
    assert metros == [
        {"name": "Austin", "aliases": ["Austin", "Round Rock", "Cedar Park"]},
        {"name": "Houston", "aliases": ["Houston"]},
    ]
    text = pe.metros_text({"geo_rules": {"onsite_metros": metros}})
    assert text == "Austin: Round Rock, Cedar Park\nHouston"
    geo = pe.section_from_form("location", {"remote_ok": "on", "onsite_states": "TX\nOK",
                                            "onsite_metros": text,
                                            "travel_days_per_month_max": "6"})["geo_rules"]
    assert geo["remote_ok"] is True and geo["hybrid_requires_metro"] is False
    assert geo["onsite_states"] == ["TX", "OK"] and geo["travel_days_per_month_max"] == 6
    assert geo["onsite_metros"] == metros


def test_a_file_the_loader_refuses_is_rolled_back(monkeypatch):
    pe.profile_path().write_text("employment:\n  track: employment\n  full_name: Pat\n",
                                 encoding="utf-8")
    monkeypatch.setattr("kestrel.pipeline.profile.load_profile", lambda track: None)
    with pytest.raises(pe.ProfileEditError, match="previous file was kept"):
        pe.save_section("employment", "about", {"full_name": "Someone Else"})
    assert "Pat" in pe.profile_path().read_text(encoding="utf-8")


# ------------------------------------------------------------------------------------ the page
def test_profile_page_has_the_tabs_and_documents_opens_as_one_of_them():
    _consulting_on()
    with _client() as c:
        html = c.get("/profile").text
        assert ">Profile</h1>" in html
        for key in ("documents", "about", "employment", "consulting", "location"):
            assert f'data-tab="{key}"' in html, key
        about_tag = html.split('data-tab="about"')[1].split(">")[0]
        assert 'aria-current="page"' in about_tag
        assert 'data-testid="no-profile"' in html and 'data-testid="profile-about"' in html
        docs = c.get("/documents").text
        assert ">Profile</h1>" in docs and 'id="import-form"' in docs
        assert 'data-tab="documents"' in docs and 'data-testid="profile-about"' not in docs
        # The sidebar: one item, Profile, active for both paths.
        for body in (html, docs):
            side = body.split('<aside class="side"')[1].split("</aside>")[0]
            assert ">Profile</a>" in side and ">Documents</a>" not in side


def test_each_tab_renders_its_form_with_the_starting_terms():
    _consulting_on()
    with _client() as c:
        emp = c.get("/profile?tab=employment").text
        assert 'data-testid="profile-employment"' in emp
        assert 'placeholder="Operations Analyst' in emp  # the example as a hint only
        con = c.get("/profile?tab=consulting").text
        assert 'data-testid="profile-consulting"' in con and 'name="day_rate_floor"' in con
        assert 'data-testid="consulting-how"' in con and "trigger events" in con
        assert "capability statement" in con
        loc = c.get("/profile?tab=location&track=consulting").text
        assert 'data-testid="profile-location"' in loc and 'name="onsite_metros"' in loc
        picker = loc.split('data-testid="profile-track-switch"')[1].split("</div>")[0]
        assert 'aria-selected="true"\n         class="active" data-track="consulting"' in picker
        assert "@click=\"setTrack('consulting')\"" in picker and ">Consulting</a>" in picker
        # Employment terms are the employment track's whatever the query says (finding 1).
        emp2 = c.get("/profile?tab=employment&track=consulting").text
        assert 'name="target_titles"' in emp2 and 'placeholder="Operations Analyst' in emp2
        assert c.post("/profile/consulting/employment", data={}).status_code == 404


def test_profile_track_tabs_are_the_track_switch_and_the_nav_agrees():
    """D-UI-66: Employment and Consulting on Profile write the same remembered track as the nav
    switch, and the nav always shows the track the page edits (the reported bug: Employment
    moved the nav to Consulting)."""
    _consulting_on()
    with _client() as c:
        for t in ("employment", "consulting"):
            page = c.get(f"/profile?tab={t}").text  # redirected to name its track
            tabs = page.split('class="tabs" role="tablist"')[1].split("</div>")[0]
            assert f"href=\"/profile?tab={t}&track={t}\"" in tabs.replace("&amp;", "&")
            assert f"@click=\"setTrack('{t}')\"" in tabs
            switch = page.split('class="track-switch"')[1].split("</div>")[0]
            selected = switch.split('aria-selected="true"')[0].rsplit("data-tab-track=", 1)[1]
            assert selected.startswith(f'"{t}"'), (t, selected)
        # About you follows the remembered tab when the URL names none.
        c.cookies.set("kestrel-track", "consulting")
        about = c.get("/profile?tab=about").text
        picker = about.split('data-testid="profile-track-switch"')[1].split("</div>")[0]
        assert 'class="active" data-track="consulting"' in picker


def test_saving_from_the_page_writes_the_file_and_shows_it_on_the_next_load():
    with _client() as c:
        r = c.post("/profile/employment/about",
                   data={"full_name": "Pat Example", "is_active": "on", "must_avoid": "staffing"})
        assert r.status_code == 200
        assert 'data-testid="profile-saved"' in r.text and 'value="Pat Example"' in r.text
        html = c.get("/profile").text
        assert 'data-testid="no-profile"' not in html and 'value="Pat Example"' in html
        r = c.post("/profile/employment/employment",
                   data={"seniority_floor": "vp", "comp_floor_usd": "x"})
        assert 'data-testid="profile-problem"' in r.text and "whole number" in r.text
        assert "comp_floor_usd" not in yaml.safe_load(
            pe.profile_path().read_text(encoding="utf-8")
        )["employment"].get("employment_terms", {})
        assert c.post("/profile/employment/nothing", data={}).status_code == 404


def test_the_worker_picks_up_a_saved_profile_without_a_restart(tmp_path):
    from kestrel.pipeline.worker import Worker

    class _Client:
        settings = None

    w = Worker.__new__(Worker)
    w._client = _Client()
    w._profiles = {"employment": None, "consulting": None}
    w.refresh_profiles()
    before = w._profiles["employment"]
    pe.save_section("employment", "about", {"full_name": "Pat Example"})
    w.refresh_profiles()
    after = w._profiles["employment"]
    assert after is not before and after.full_name == "Pat Example"
    w.refresh_profiles()
    assert w._profiles["employment"] is after  # unchanged file: no re-read


def test_saving_about_keeps_the_example_hints_and_the_not_saved_label_on_the_other_tabs():
    """Operator (D-UI-25): after the first save, the Employment tab's desired titles vanished.
    The other tabs keep the example's titles as placeholder hints and say they are not saved,
    but never hold them as values (release round review, finding 1)."""
    pe.save_section("employment", "about", {"full_name": "Pat Example"})
    st = pe.load_state()
    assert st.present == ("employment",)
    assert st.tracks["employment"]["employment_terms"] == {}
    assert st.from_example["employment"] == ("employment_terms", "geo_rules")
    with _client() as c:
        emp = c.get("/profile?tab=employment").text
        assert 'placeholder="Operations Analyst' in emp and 'data-testid="from-example"' in emp
        assert ">Operations Analyst" not in emp  # not a textarea value
        r = c.post("/profile/employment/employment",
                   data={"target_titles": "Head of IT", "anti_titles": "Intern",
                         "seniority_floor": "director"})
        assert 'data-testid="from-example"' not in r.text
        assert 'data-testid="from-example"' not in c.get("/profile?tab=employment").text
        assert 'data-testid="from-example"' in c.get("/profile?tab=location").text
    data = yaml.safe_load(pe.profile_path().read_text(encoding="utf-8"))["employment"]
    assert data["employment_terms"]["target_titles"] == ["Head of IT"]
    assert "geo_rules" not in data  # still unsaved, still only shown


def test_a_blank_number_clears_the_stored_one():
    pe.profile_path().write_text(
        "employment:\n  track: employment\n  employment_terms:\n    comp_floor_usd: 150000\n",
        encoding="utf-8",
    )
    form = {"seniority_floor": "director", "comp_floor_usd": ""}
    pe.save_section("employment", "employment", pe.section_from_form("employment", form))
    terms = yaml.safe_load(pe.profile_path().read_text(encoding="utf-8"))["employment"][
        "employment_terms"]
    assert terms["comp_floor_usd"] is None
    assert load_profile("employment").employment_terms.comp_floor_usd is None


def test_an_unreadable_profile_blocks_every_save_and_says_so():
    pe.profile_path().write_text("employment: [unclosed\n", encoding="utf-8")
    st = pe.load_state()
    assert st.exists and st.problem and "not valid YAML" in st.problem
    with pytest.raises(pe.ProfileEditError, match="not valid YAML"):
        pe.save_section("employment", "about", {"full_name": "Pat"})
    assert pe.profile_path().read_text(encoding="utf-8").startswith("employment: [unclosed")
    with _client() as c:
        html = c.get("/profile").text
        assert 'data-testid="profile-unreadable"' in html
        assert '<button class="btn primary" type="submit" disabled>' in html
        r = c.post("/profile/employment/about", data={"full_name": "Pat"})
        assert 'data-testid="profile-problem"' in r.text
    assert "Pat" not in pe.profile_path().read_text(encoding="utf-8")


def test_overflowing_numbers_are_refused_not_a_500():
    with pytest.raises(pe.ProfileEditError, match="whole number"):
        pe.section_from_form("employment", {"seniority_floor": "vp", "comp_floor_usd": "1e400"})
    with pytest.raises(pe.ProfileEditError, match="whole number"):
        pe.section_from_form("consulting", {"day_rate_floor": "inf"})


def test_about_does_not_offer_a_switch_nothing_reads():
    with _client() as c:
        html = c.get("/profile?tab=about").text
    assert 'name="is_active"' not in html


def test_about_you_is_first_and_an_empty_one_is_flagged_on_every_tab():
    with _client() as c:
        html = c.get("/profile").text
        tabs = html.split('role="tablist"')[1].split("</div>")[0]
        assert tabs.index('data-tab="about"') < tabs.index('data-tab="documents"')
        assert 'data-testid="about-missing"' in html
        assert 'data-testid="about-missing"' in c.get("/documents").text
        c.post("/profile/employment/about", data={"full_name": "Pat Example"})
        assert 'data-testid="about-missing"' not in c.get("/profile?tab=employment").text
        # A consulting block without a name is flagged on the consulting tabs only, never on
        # the employment ones (operator: the warning seemed to come and go between tabs).
        pe.save_section("consulting", "consulting", {"consulting_terms": {}})
        assert 'data-testid="about-missing"' not in c.get("/profile?tab=employment").text
        assert 'data-testid="about-missing"' not in c.get("/profile?tab=about").text
        html = c.get("/profile?tab=consulting").text
        assert 'href="/profile?tab=about&track=consulting"' in html
        assert "About you (consulting) has no name yet" in html
        # And no page is ever served from the browser cache.
        assert c.get("/profile").headers["cache-control"] == "no-store"
        assert c.get("/documents").headers["cache-control"] == "no-store"
        assert "no-store" not in c.get("/static/css/kestrel.css").headers.get("cache-control", "")


def test_sponsorship_wording_says_what_the_box_does_and_the_form_posts_to_profile():
    with _client() as c:
        about = c.get("/profile?tab=about").text
        assert "Check this if you need an employer to sponsor your visa." in about
        assert 'name="is_active"' not in about
        # The onboarding renders the same form into the wizard (tests/test_onboarding.py); on
        # the Profile page it still posts to the Profile route and swaps its own section.
        assert 'hx-post="/profile/employment/about"' in about
        assert 'hx-target="#profile-section"' in about and "Save and continue" not in about


def test_the_help_panel_on_documents_collapses():
    body = (Path(paths.web_templates_dir()) / "partials" / "documents_body.html").read_text(
        encoding="utf-8"
    )
    # Collapsed by default (D-UI-29): the operator opens it when wanted.
    assert '<details class="help panel" data-testid="doc-help">' in body
    assert "(click to expand)" in body


def test_renders_take_the_name_and_contact_line_from_about_you():
    """An imported resume has no header of its own; the PDF went out with no name (operator
    report). The About you block fills it; the master's own header still wins; the shipped
    example's fictional person never appears."""
    from kestrel.db.models import DocMaster
    from kestrel.docs.catalog import RenderDoc, document_header
    from kestrel.docs.render import render_html

    master = DocMaster(track="employment", name="Imported resume", doc_kind="resume",
                       header={}, summary_variants=[], skill_groups=[])
    assert document_header(master) == {}  # no profile.yml: nothing, not Jordan Rivera
    pe.save_section("employment", "about", pe.section_from_form("about", {
        "full_name": "Pat Example", "email": "pat@example.com", "phone": "512-555-0100",
        "location": "Austin, TX", "headline": "VP of IT"}))
    h = document_header(master)
    assert h == {"name": "Pat Example", "email": "pat@example.com", "phone": "512-555-0100",
                 "location": "Austin, TX", "headline": "VP of IT"}
    html = render_html(RenderDoc(header=h, blocks=[]))
    assert "Pat Example" in html and "VP of IT" in html and "512-555-0100" in html
    master.header = {"name": "P. Example", "links": ["example.com"]}
    h = document_header(master)
    assert h["name"] == "P. Example" and h["links"] == ["example.com"]
    assert h["email"] == "pat@example.com"
    # The consulting master reads the consulting block, which has no name yet.
    con = DocMaster(track="consulting", name="cap", doc_kind="capability_statement",
                    header={}, summary_variants=[], skill_groups=[])
    assert document_header(con) == {}
