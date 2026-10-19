"""Profile-generated job searches (D-SRC-2) and the enabled-tracks setting (D-UI-26). Pure
builder tests plus the rebuild's ownership rules: generated rows follow the profile, custom and
shipped rows are never touched, nothing is switched on without the operator."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from kestrel.db.models import Source
from kestrel.ingest.adapters.jobspy import JobSpyAdapter
from kestrel.ingest.generated import (
    MAX_QUERY_CHARS,
    SITES,
    build_specs,
    rebuild_generated_sources,
    title_groups,
)
from kestrel.services import profile_edit as pe
from kestrel.services import tracks

EMP = {
    "full_name": "Pat", "email": "pat@example.com",
    "employment_terms": {"target_titles": ["VP of IT", "Head of IT"],
                         "anti_titles": ["Intern", "Help Desk"]},
    "geo_rules": {"remote_ok": True, "remote_country_allowlist": ["US"],
                  "onsite_states": ["TX"],
                  "onsite_metros": [{"name": "Austin", "aliases": ["Austin"]}]},
}


@pytest.fixture(autouse=True)
def _clean():
    tracks.forget_cache()
    pe.profile_path().unlink(missing_ok=True)
    yield
    tracks.forget_cache()


def test_titles_become_one_query_with_exclusions():
    (q,) = title_groups(["VP of IT", "Head of IT"], ["Intern", "Help Desk"])
    assert q == '("VP of IT" OR "Head of IT") -Intern -"Help Desk"'


def test_a_long_title_list_is_split_and_no_query_is_too_long():
    titles = [f"Director of Function Number {i}" for i in range(20)]
    qs = title_groups(titles, ["Intern"])
    assert len(qs) > 1 and all(len(q) <= MAX_QUERY_CHARS for q in qs)
    joined = " ".join(qs)
    assert all(f'"{t}"' in joined for t in titles)


def test_employment_builds_a_metro_and_a_remote_search_that_validate():
    specs = build_specs({"employment": EMP}, ("employment",), "pat@example.com")
    names = [s.name for s in specs]
    assert names == ["Jobs near Austin", "Remote jobs"]
    for s in specs:
        JobSpyAdapter().validate_config(s.config)  # the Indeed one-of rule holds
        assert s.summary
    assert specs[0].config["location"] == "Austin, TX"
    assert specs[1].config["is_remote"] is True and "hours_old" not in specs[1].config


def test_generated_searches_skip_ziprecruiter_but_a_custom_source_may_name_it():
    # D-SRC-4: ZipRecruiter 403s every JobSpy search behind Cloudflare and JobSpy hides it.
    assert SITES == ["indeed", "glassdoor"]  # Google Jobs returns nothing through JobSpy
    specs = build_specs({"employment": EMP}, ("employment",), "pat@example.com")
    assert specs and all("zip_recruiter" not in s.config["site_name"] for s in specs)
    JobSpyAdapter().validate_config(
        {"site_name": ["zip_recruiter"], "search_term": "vp it", "hours_old": 168}
    )


def test_consulting_only_when_enabled_and_sec_only_with_an_email():
    con = {"consulting_terms": {"service_lines": ["IT integration"]}}
    blocks = {"employment": EMP, "consulting": con}
    assert all(s.track == "employment" for s in build_specs(blocks, ("employment",), "a@b.c"))
    both = build_specs(blocks, ("employment", "consulting"), "a@b.c")
    kinds = [(s.track, s.kind) for s in both]
    assert ("consulting", "jobspy") in kinds and ("consulting", "sec_edgar") in kinds
    sec = next(s for s in both if s.kind == "sec_edgar")
    assert "a@b.c" in sec.config["user_agent"]
    assert not any(s.kind == "sec_edgar" for s in build_specs(blocks, tracks.DEFAULT, None))


def test_sec_watch_is_neutral_by_default_and_titles_are_opt_in():
    """D-SRC-6 as amended by review finding 3: by default the EDGAR phrases are field-neutral
    events only, never the operator's titles (SEC sees every query beside the contact email).
    The Sources page opt-in adds up to three target titles. No shipped job title or field."""
    from kestrel.ingest.generated import SEC_NEUTRAL_QUERIES, sec_queries
    from kestrel.ingest.signal_sources.sec_edgar import SecEdgarSignalSource

    nurse = {"employment_terms": {"target_titles": ["Nurse Manager", "Charge Nurse",
                                                    "Clinical Lead", "Director of Nursing"]}}
    con = {"consulting_terms": {"service_lines": ["Clinical staffing"]}}
    both = ("employment", "consulting")
    sec = next(s for s in build_specs({"employment": nurse, "consulting": con}, both, "a@b.c")
               if s.kind == "sec_edgar")
    assert sec.config["queries"] == SEC_NEUTRAL_QUERIES
    sent = " ".join(sec.config["queries"]).lower()
    assert "nurse" not in sent and "clinical" not in sent
    SecEdgarSignalSource().validate_config({**sec.config})
    # Opted in: the titles first, then the neutral events.
    sec = next(s for s in build_specs({"employment": nurse, "consulting": con}, both, "a@b.c",
                                      sec_use_titles=True) if s.kind == "sec_edgar")
    q = sec.config["queries"]
    assert q[:3] == ['"nurse manager"', '"charge nurse"', '"clinical lead"']
    assert q[3:] == SEC_NEUTRAL_QUERIES
    SecEdgarSignalSource().validate_config({**sec.config})
    text = " ".join(q + [sec.name, sec.summary]).lower()
    for assumed in ("information", "chief information", "cyber", " it "):
        assert assumed not in f" {text} "
    # No titles: the service lines when opted in, then the neutral events alone.
    assert sec_queries([], ["Bookkeeping"], use_titles=True) == [
        '"bookkeeping"', *SEC_NEUTRAL_QUERIES]
    assert sec_queries(["Nurse Manager"], ["Bookkeeping"]) == SEC_NEUTRAL_QUERIES
    assert sec_queries([], []) == SEC_NEUTRAL_QUERIES


def test_no_titles_means_no_employment_searches():
    blank = {**EMP, "employment_terms": {"target_titles": []}}
    assert build_specs({"employment": blank}, ("employment",), None) == []


async def test_rebuild_follows_the_profile_and_never_touches_custom_rows(sm):
    pe.save_section("employment", "about", {"full_name": "Pat", "email": "pat@example.com"})
    pe.save_section("employment", "employment", {"employment_terms": EMP["employment_terms"]})
    pe.save_section("employment", "location", {"geo_rules": EMP["geo_rules"]})
    async with sm() as s:
        s.add(Source(track="employment", kind="jobspy", name="My own search",
                     config={"search_term": "x"}, enabled=True, origin="custom"))
        await s.commit()
        dry = await rebuild_generated_sources(s, dry_run=True)
        assert dry.created == ["Jobs near Austin", "Remote jobs"]
        assert (await s.execute(select(Source).where(Source.origin == "generated"))).all() == []
        r = await rebuild_generated_sources(s)
        await s.commit()
        rows = {x.name: x for x in (await s.execute(select(Source))).scalars().all()}
        assert not rows["Jobs near Austin"].enabled  # nothing scrapes until the operator says
        await rebuild_generated_sources(s, enable=True)
        await s.commit()
        # The operator moves: Austin out, Dallas in. The custom row is untouched.
        geo = {**EMP["geo_rules"], "onsite_metros": [{"name": "Dallas", "aliases": ["Dallas"]}]}
        pe.save_section("employment", "location", {"geo_rules": geo})
        r = await rebuild_generated_sources(s)
        await s.commit()
        assert r.created == ["Jobs near Dallas"] and r.removed == ["Jobs near Austin"]
        rows = {x.name: x for x in (await s.execute(select(Source))).scalars().all()}
        assert rows["Jobs near Dallas"].enabled  # a new row keeps its siblings' state
        assert rows["My own search"].config == {"search_term": "x"}
        assert rows["My own search"].origin == "custom"
        again = await rebuild_generated_sources(s)
        assert not again.changed


async def test_enabled_tracks_default_to_employment_and_never_to_none(sm):
    """Release concern 10: a new install has only the employment track; consulting is opt-in."""
    async with sm() as s:
        assert await tracks.load_enabled(s) == ("employment",)
        assert tracks.consulting_enabled() is False
        assert await tracks.save_enabled(s, ["employment", "consulting"]) == tracks.BOTH
        await s.commit()
        assert tracks.consulting_enabled() is True
        tracks.forget_cache()
        assert await tracks.load_enabled(s) == tracks.BOTH
        assert await tracks.save_enabled(s, []) == ("employment",)


def test_quit_button_appears_only_with_a_shell_and_calls_the_real_quit(monkeypatch):
    from fastapi.testclient import TestClient

    from kestrel.web import desktop_bridge
    from kestrel.web.app import build_app

    calls: list[str] = []
    monkeypatch.setattr("threading.Timer", lambda d, fn: type("T", (), {"start": lambda s: fn()})())
    with TestClient(build_app(worker=False)) as c:
        assert 'data-testid="quit-app"' not in c.get("/help").text
        assert c.post("/api/quit").json() == {"quitting": False}
        desktop_bridge.set_quit_handler(lambda: calls.append("quit"))
        try:
            assert 'data-testid="quit-app"' in c.get("/help").text
            assert c.post("/api/quit").json() == {"quitting": True}
            assert calls == ["quit"]
        finally:
            desktop_bridge.set_quit_handler(None)


def test_child_processes_never_open_a_console_window(monkeypatch):
    import subprocess
    import sys

    from kestrel.llm import profiles

    monkeypatch.setattr(sys, "platform", "win32")
    assert profiles.no_console_window() == {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    monkeypatch.setattr(sys, "platform", "linux")
    assert profiles.no_console_window() == {}


def test_reset_backs_up_then_clears_everything_but_optionally_the_keys(tmp_path):
    from kestrel import paths
    from kestrel.services.reset import reset_everything

    home = paths.data_home()
    home.mkdir(parents=True, exist_ok=True)
    (paths.config_dir()).mkdir(parents=True, exist_ok=True)
    (paths.config_dir() / "profile.yml").write_text("employment: {full_name: Pat}\n")
    paths.dotenv_path().write_text("OPENROUTER_API_KEY=x\n")
    r = reset_everything(keep_keys=True)
    assert r.backup is not None and r.backup.exists()
    assert not str(r.backup).startswith(str(home))  # the backup survives the reset
    assert paths.dotenv_path().exists() and ".env" in r.kept
    assert not (paths.config_dir() / "profile.yml").exists()
    r2 = reset_everything(keep_keys=False)
    assert not paths.dotenv_path().exists() and ".env" in r2.removed


# ------------------------------------------------------------- review fixes (D-SRC-2 addendum)
async def test_a_search_that_has_run_is_retired_not_deleted_and_the_rebuild_succeeds(sm):
    """Review finding 1: deleting a generated row that a scrape_run points at failed the whole
    rebuild under foreign keys, and the profile save swallowed it."""
    from kestrel.db.models import ScrapeRun

    pe.save_section("employment", "employment", {"employment_terms": EMP["employment_terms"]})
    pe.save_section("employment", "location", {"geo_rules": EMP["geo_rules"]})
    async with sm() as s:
        await rebuild_generated_sources(s, enable=True)
        await s.commit()
        austin = (await s.execute(select(Source).where(Source.name == "Jobs near Austin"))
                  ).scalar_one()
        s.add(ScrapeRun(source_id=austin.id, status="ok"))
        await s.commit()
        geo = {**EMP["geo_rules"], "onsite_metros": [{"name": "Dallas", "aliases": ["Dallas"]}]}
        pe.save_section("employment", "location", {"geo_rules": geo})
        r = await rebuild_generated_sources(s)
        await s.commit()
        assert r.created == ["Jobs near Dallas"] and r.removed == ["Jobs near Austin"]
        await s.refresh(austin)
        assert austin.origin == "retired" and not austin.enabled
        assert austin.name.startswith("Jobs near Austin (retired")


async def test_unsaved_example_sections_never_become_searches(sm):
    """Review finding 2: saving only About you built searches from the fictional example's
    titles and Texas metros."""
    pe.save_section("employment", "about", {"full_name": "Pat", "email": "pat@example.com"})
    async with sm() as s:
        r = await rebuild_generated_sources(s, dry_run=True)
    assert r.specs == [] and r.created == []


async def test_a_rebuild_keeps_each_rows_own_switch(sm):
    """Review finding 9: an auto-disabled search was re-enabled by an unrelated profile edit."""
    pe.save_section("employment", "employment", {"employment_terms": EMP["employment_terms"]})
    pe.save_section("employment", "location", {"geo_rules": EMP["geo_rules"]})
    async with sm() as s:
        await rebuild_generated_sources(s, enable=True)
        await s.commit()
        remote = (await s.execute(select(Source).where(Source.name == "Remote jobs"))
                  ).scalar_one()
        remote.enabled = False  # the scheduler switched it off after failures
        await s.commit()
        pe.save_section("employment", "employment", {"employment_terms": {
            **EMP["employment_terms"], "target_titles": ["CIO", "VP of IT"]}})
        await rebuild_generated_sources(s)
        await s.commit()
        await s.refresh(remote)
        assert not remote.enabled


def test_queries_carry_no_full_text_exclusions_and_metro_states_are_not_guessed():
    geo = {**EMP["geo_rules"], "onsite_states": ["TX", "WA"],
           "onsite_metros": [{"name": "Seattle", "aliases": ["Seattle"]}]}
    specs = build_specs({"employment": {**EMP, "geo_rules": geo}}, ("employment",), None)
    near = next(s for s in specs if s.name.startswith("Jobs near"))
    assert " -" not in near.config["search_term"]  # no full-text negations (finding 3)
    assert "Intern" not in near.config["search_term"]  # Tier 0's anti_title does titles only
    assert near.config["location"] == "Seattle"


def test_generated_searches_are_capped():
    from kestrel.ingest.generated import MAX_GENERATED

    many = [{"name": f"City {i}", "aliases": [f"City {i}"]} for i in range(30)]
    geo = {**EMP["geo_rules"], "onsite_metros": many}
    specs = build_specs({"employment": {**EMP, "geo_rules": geo}}, ("employment",), None)
    assert len(specs) == MAX_GENERATED and specs[0].name == "Remote jobs"


async def test_a_track_that_is_off_never_scrapes(sm):
    """Review finding 4: hidden consulting sources kept scraping."""
    from kestrel.ingest.scheduler import due_sources

    async with sm() as s:
        s.add(Source(track="consulting", kind="sam_gov", name="SAM", config={}, enabled=True))
        s.add(Source(track="employment", kind="jobspy", name="Mine", config={}, enabled=True))
        await tracks.save_enabled(s, ["employment"])
        await s.commit()
        assert [x.name for x in await due_sources(s)] == ["Mine"]
        await tracks.save_enabled(s, ["employment", "consulting"])
        await s.commit()
        assert sorted(x.name for x in await due_sources(s)) == ["Mine", "SAM"]


# ------------------------------------------------------------------- reset review fixes
async def test_in_app_reset_refuses_while_a_cycle_runs_and_deletes_nothing(sm, monkeypatch):
    from kestrel.services import reset as reset_mod
    from kestrel.web import app as app_mod

    pe.save_section("employment", "about", {"full_name": "Pat"})
    monkeypatch.setattr(reset_mod, "IDLE_WAIT_S", 0.05)
    app_mod._cycle_idle.clear()  # a cycle is mid-flight
    try:
        with pytest.raises(reset_mod.ResetBusy):
            await reset_mod.reset_in_app(sm)
    finally:
        app_mod._cycle_idle.set()
    assert pe.profile_path().exists()
    assert not app_mod.is_paused()  # put back the way it was


def test_clear_files_survives_a_locked_file(monkeypatch):
    import shutil

    from kestrel import paths
    from kestrel.services import reset as reset_mod

    docs = paths.data_dir() / "documents"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "open.pdf").write_bytes(b"%PDF")
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    (paths.config_dir() / "profile.yml").write_text("employment: {}\n")

    def locked(path, *a, **k):
        raise PermissionError("in use")

    monkeypatch.setattr(shutil, "rmtree", locked)
    removed, kept, failed = reset_mod._clear_files(keep_keys=True)
    assert "data/documents" in failed and "config/profile.yml" in removed


def test_backup_carries_the_reranker(tmp_path):
    import zipfile

    from kestrel import paths
    from kestrel.services.backup import ML_PREFIX, create_backup

    ml = paths.data_dir() / "ml"
    ml.mkdir(parents=True, exist_ok=True)
    (ml / "reranker_employment.pkl").write_bytes(b"model")
    zip_path = create_backup(tmp_path)
    assert f"{ML_PREFIX}reranker_employment.pkl" in zipfile.ZipFile(zip_path).namelist()


def test_cli_reset_refuses_a_folder_that_is_not_kestrels(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from kestrel import cli as cli_mod
    from kestrel.cli import app

    # Whether a Kestrel app happens to be open on the machine running the tests must not decide
    # this: the running-app guard refuses first, and this test is about the folder check.
    monkeypatch.setattr(cli_mod, "_app_is_running", lambda: False)
    victim = tmp_path / "Documents"
    victim.mkdir()
    (victim / "taxes.pdf").write_bytes(b"x")
    monkeypatch.setenv("KESTREL_DATA_HOME", str(victim))
    result = CliRunner().invoke(app, ["reset", "--yes"])
    assert result.exit_code == 2 and "does not look like a Kestrel data folder" in result.output
    assert (victim / "taxes.pdf").exists()


async def test_free_models_need_an_explicit_training_opt_in(sm, monkeypatch):
    """Operator's import failed with OpenRouter 404 "No endpoints found matching your data
    policy": free models plus Kestrel's data_collection deny (D-UI-32)."""
    from kestrel.config import get_settings
    from kestrel.llm import providers as prov
    from kestrel.services.hosted_connect import connect_provider

    async def ok_key(_b):
        return type("K", (), {"ok": True, "detail": ""})()

    monkeypatch.setattr(prov, "verify_key", ok_key)
    async with sm() as s:
        out = await connect_provider(
            s, get_settings(), provider="openrouter", api_key="sk-or-test", acknowledged=True,
            free_only=True, switch=False, profile=None,
        )
    assert not out.ok and "Allow providers that may train" in out.message


def test_the_data_policy_404_is_explained_in_plain_words():
    from kestrel.llm.client import ProviderError

    e = ProviderError(404, "No endpoints found matching your data policy (Free model "
                           "training)", "https://openrouter.ai/api/v1/chat/completions")
    assert "overrides your OpenRouter account setting" in str(e)
    assert "Pick a paid model" in str(e)


def test_generated_searches_ask_glassdoor_and_drop_the_google_term():
    specs = [s for s in build_specs({"employment": EMP}, ("employment",), "pat@example.com")
             if s.kind == "jobspy"]
    assert specs
    for s in specs:
        assert "glassdoor" in s.config["site_name"] and "google" not in s.config["site_name"]
        assert "google_search_term" not in s.config
        JobSpyAdapter().validate_config(s.config)
