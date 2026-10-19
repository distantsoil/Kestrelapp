"""Release round review (11 findings): one regression test (or a small group) per finding.
Cloud-safe: a migrated SQLite file per test, fixtures and mocks only, no model and no network."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from kestrel import paths
from kestrel.config import get_settings
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
from kestrel.db.models import (
    DocMaster,
    Opportunity,
    Pursuit,
    Setting,
    Source,
    Story,
    StoryUsage,
    Task,
)
from kestrel.services import profile_edit as pe

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "kestrel"
_RECENT = "2026-10-14"


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


@pytest.fixture
def fresh(monkeypatch):
    """A migrated database at the per-test data home and no profile.yml, as test_profile_page."""
    from kestrel.services import tracks
    from kestrel.web import routes_control

    async def _down(*a, **k):
        return None

    monkeypatch.setattr(routes_control, "_probe_ollama", _down, raising=False)
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()
    tracks.forget_cache()
    pe.profile_path().unlink(missing_ok=True)
    yield
    asyncio.run(reset_engine())
    tracks.forget_cache()


def _run(fn):
    async def go():
        async with get_sessionmaker()() as s:
            return await fn(s)

    return asyncio.run(go())


# ============================================================ 1. the example hides no jobs
def test_1_a_freshly_saved_default_profile_passes_seattle_onsite_and_account_executive(fresh):
    from kestrel.pipeline.profile import load_profile
    from kestrel.pipeline.tier0 import evaluate_tier0
    from kestrel.rules import helpers

    state = pe.load_state()
    emp = state.tracks["employment"]
    # The forms start blank; the example is only a placeholder source.
    assert emp["employment_terms"] == {} and emp["geo_rules"] == {"remote_ok": True}
    # Save every tab exactly as the untouched forms submit them.
    pe.save_section("employment", "about", pe.section_from_form("about", {"full_name": "Pat"}))
    pe.save_section("employment", "employment",
                    pe.section_from_form("employment", {"seniority_floor": ""}))
    pe.save_section("employment", "location",
                    pe.section_from_form("location", {"remote_ok": "on"}))
    profile = load_profile("employment")
    filters = helpers.load_filters(str(paths.config_file("filters.yml")))

    def passes(**kw) -> bool:
        opp = SimpleNamespace(
            description_md="Own the weekly report.", comp_max=None, comp_source="none",
            date_posted=_RECENT, event_date=None, seen_count=1, ghost_score=0.1, url=None,
            trigger_data=None, employment_type=None, is_remote=None, country="US", **kw)
        return evaluate_tier0(opp=opp, org=None, profile=profile, source=None,
                              settings=get_settings(), filters=filters,
                              track="employment").passed

    assert passes(title="Warehouse Supervisor", remote_type="onsite", state="WA",
                  city="Seattle", location_raw="Seattle, WA")
    assert passes(title="Account Executive", remote_type="hybrid", state="TX", city="Austin",
                  location_raw="Austin, TX")


def test_1_the_shipped_example_filters_nothing_and_the_page_never_saves_it(fresh):
    data = yaml.safe_load((PKG / "resources/config/profile.example.yml").read_text("utf-8"))
    for track in ("employment", "consulting"):
        geo = data[track]["geo_rules"]
        assert geo["onsite_states"] == [] and geo["onsite_metros"] == []
        assert geo["hybrid_requires_metro"] is False
    terms = data["employment"]["employment_terms"]
    assert terms["anti_titles"] == [] and terms["excluded_employment_types"] == []
    assert terms["seniority_floor"] in ("", None) and terms["comp_floor_usd"] in ("", None)
    cterms = data["consulting"]["consulting_terms"]
    assert cterms["day_rate_floor"] in ("", None) and cterms["min_engagement_days"] in ("", None)
    assert cterms["excluded_sectors"] == []
    with _client() as c:
        loc = c.get("/profile?tab=location").text
    assert "Columbus" not in loc and "e.g. TX" not in loc


def test_1_no_on_site_places_listed_means_no_location_filter():
    from kestrel.rules.helpers import geo_evaluate

    rules = {"remote_country_allowlist": [], "onsite_states": [], "onsite_metros": []}
    onsite = SimpleNamespace(remote_type="onsite", country="US", state="WA", city="Seattle",
                             location_raw="Seattle, WA")
    assert geo_evaluate(onsite, rules) == (True, False)
    # A listed state still filters, and a remote role outside a typed country list still fails.
    assert geo_evaluate(onsite, {**rules, "onsite_states": ["OH"]})[0] is False
    abroad = SimpleNamespace(remote_type="remote", country="DE", state=None, city=None,
                             location_raw="Remote, Germany")
    assert geo_evaluate(abroad, {**rules, "remote_country_allowlist": ["US"]})[0] is False


# ============================================================== 2. LinkedIn never reachable
@pytest.mark.parametrize("config", [
    {"search_term": "analyst"},
    {"search_term": "analyst", "site_name": None},
    {"search_term": "analyst", "site_name": []},
    {"search_term": "analyst", "site_name": ""},
])
def test_2_a_missing_site_list_is_kestrels_default_boards_never_none(config):
    from kestrel.ingest.adapters.jobspy import DEFAULT_SITES, JobSpyAdapter

    params = JobSpyAdapter()._params(config)
    assert params["site_name"] == list(DEFAULT_SITES) and "linkedin" not in params["site_name"]


async def test_2_the_scrape_is_called_with_an_explicit_board_list(monkeypatch):
    import jobspy

    from kestrel.ingest.adapters.jobspy import JobSpyAdapter

    seen: dict = {}

    def fake_scrape(**kw):
        seen.update(kw)
        return None

    monkeypatch.setattr(jobspy, "scrape_jobs", fake_scrape)
    await JobSpyAdapter().fetch({"search_term": "analyst"})
    assert seen["site_name"] == ["indeed", "glassdoor"]


def test_2_linkedin_is_refused_anywhere_in_the_list():
    from kestrel.ingest.adapters.jobspy import JobSpyAdapter
    from kestrel.ingest.base import SourceConfigError

    for sites in (["linkedin"], ["indeed", "LinkedIn"], "linkedin"):
        with pytest.raises(SourceConfigError, match="LinkedIn"):
            JobSpyAdapter().validate_config({"search_term": "x", "site_name": sites})


def test_2_a_linkedin_only_source_stays_off_says_why_and_offers_the_default_boards(fresh):
    from kestrel.ingest.adapters.jobspy import NO_BOARDS_KEY, JobSpyAdapter
    from kestrel.ingest.base import SourceConfigError
    from kestrel.ingest.sources import retire_linkedin_sources

    async def seed(s):
        src = Source(track="employment", kind="jobspy", name="Old LinkedIn search",
                     config={"search_term": "analyst", "site_name": ["linkedin"]}, enabled=True,
                     origin="custom")
        s.add(src)
        await s.flush()
        await retire_linkedin_sources(s)
        await s.commit()
        return src.id

    sid = _run(seed)
    src = _run(lambda s: s.get(Source, sid))
    assert src.enabled is False and src.config[NO_BOARDS_KEY] is True
    with pytest.raises(SourceConfigError, match="default boards"):
        JobSpyAdapter().validate_config(src.config)  # a run-now or scheduled scan refuses
    with _client() as c:
        page = c.get("/sources").text
        assert 'data-testid="no-boards"' in page and "Use Kestrel's default boards" in page
        refused = c.post(f"/sources/{sid}/toggle").text
        assert "default boards" in refused
        assert _run(lambda s: s.get(Source, sid)).enabled is False  # Turn on did not turn it on
        c.post(f"/sources/{sid}/default-boards")
    src = _run(lambda s: s.get(Source, sid))
    assert src.enabled is True and src.config["site_name"] == ["indeed", "glassdoor"]
    assert NO_BOARDS_KEY not in src.config


# ================================================================ 3. orphans per (role, stage)
async def test_3_a_role_that_moved_on_gets_its_own_retry_at_the_new_stage(sm):
    from kestrel.pipeline.orphans import requeue_orphans, stuck_ids

    async with sm() as s:
        o = Opportunity(track="employment", opp_type="job_posting", title="Analyst",
                        url="https://x/a", stage="t0_pass", pre_score=0.5)
        s.add(o)
        await s.commit()
        oid = o.id
    assert (await requeue_orphans(sm))["requeued"] == 1
    # Tier 1 ran and passed; its t2 task then died, leaving the role orphaned at t1_pass.
    async with sm() as s:
        for t in (await s.execute(select(Task))).scalars():
            t.status = "done"
        (await s.get(Opportunity, oid)).stage = "t1_pass"
        await s.commit()
        assert await stuck_ids(s) == set()  # the t0_pass entry is not evidence about t1_pass
    assert await requeue_orphans(sm) == {"requeued": 1, "stuck": 0}
    async with sm() as s:
        live = (await s.execute(select(Task).where(Task.status == "pending"))).scalars().all()
        assert [t.type for t in live] == ["t2"]
        record = (await s.get(Setting, "pipeline.orphan_requeue")).value
        assert set(record) == {f"{oid}:t1_pass"}  # the old stage's entry was dropped


# ============================================================ 4. enqueue_once is race-safe
async def test_4_two_processes_cannot_both_queue_the_same_live_task(sm, monkeypatch):
    from kestrel.pipeline import tasks

    async with sm() as s:
        o = Opportunity(track="employment", opp_type="job_posting", title="A", url="https://x/b")
        s.add(o)
        await s.commit()
        oid = o.id
    async with sm() as s:
        assert await tasks.enqueue_once(s, "t1", oid) is True
        await s.commit()
    # The other process's check ran before this row existed: blind the check and insert again.
    monkeypatch.setattr(tasks, "_LIVE", ("nothing-is-live",))
    async with sm() as s:
        assert await tasks.enqueue_once(s, "t1", oid) is False
        await s.commit()
    async with sm() as s:
        n = (await s.execute(select(func.count()).select_from(Task))).scalar_one()
        assert n == 1
        s.add(Task(type="t1", payload={"opportunity_id": oid}))  # a plain insert is refused too
        with pytest.raises(IntegrityError):
            await s.commit()
    # A finished task does not block the next one.
    monkeypatch.setattr(tasks, "_LIVE", ("pending", "leased"))
    async with sm() as s:
        for t in (await s.execute(select(Task))).scalars():
            t.status = "done"
        await s.commit()
        assert await tasks.enqueue_once(s, "t1", oid) is True
        await s.commit()


def test_4_the_migration_cancels_existing_duplicates_before_the_index(tmp_path):
    import sqlite3

    from alembic import command

    db = tmp_path / "dupes.db"
    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        command.upgrade(migrate._config(str(mig_dir), db), "0013")
    conn = sqlite3.connect(db)
    for _ in range(2):
        conn.execute(
            "INSERT INTO task (type, payload, status, priority, attempts, max_attempts, "
            "created_at, updated_at) VALUES ('t2', '{\"opportunity_id\": 7}', 'pending', 0, 0, "
            "3, '2026-10-16', '2026-10-16')")
    conn.commit()
    conn.close()
    migrate.run_migrations(db)
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT status FROM task ORDER BY id").fetchall()
    conn.close()
    assert rows == [("pending",), ("cancelled",)]


# ================================================= 5 and 6. model tasks: cancel and restarts
URL = "http://127.0.0.1:11434"


async def test_5_cancel_closes_a_silent_stream_at_once_and_reads_stopping_until_then(sm):
    from kestrel.services import model_tasks as mt

    mt.reset()
    closed = asyncio.Event()

    async def silent_body():
        yield b'{"status": "pulling manifest"}\n'
        try:
            await asyncio.sleep(3600)  # Ollama verifying a big layer: nothing for minutes
            yield b""
        finally:
            closed.set()

    async with sm() as s:
        await mt.queue_pulls(s, ["qwen3:14b"])
    with respx.mock as mock:
        mock.post(f"{URL}/api/pull").mock(
            return_value=httpx.Response(200, content=silent_body()))
        drain = asyncio.create_task(mt.drain_once(sm, URL))
        for _ in range(200):
            if mt._running and any(p.status == "pulling manifest" for p in mt._progress.values()):
                break
            await asyncio.sleep(0.01)
        (tid,) = mt._running
        async with sm() as s:
            assert await mt.cancel_all(s) == 1
        # No await since the cancel: the stream has not had a chance to close yet.
        assert mt.stopping() is True
        row = SimpleNamespace(id=tid, type=mt.PULL_TYPE, status="cancelled", attempts=1,
                              last_error=mt.CANCELLED_NOTE, payload={"tag": "qwen3:14b"})
        assert mt._row_view(row)["step"] == "Stopping…"
        assert await asyncio.wait_for(drain, timeout=2.0) is True  # not the 600 s read timeout
    assert closed.is_set() and mt.stopping() is False
    async with sm() as s:
        snap = await mt.snapshot(s)
    assert snap["items"][0]["step"] == "Cancelled" and snap["stopping"] is False
    mt.reset()


async def test_6_restarts_do_not_use_up_a_downloads_attempts(sm):
    from kestrel.queue.queue import RESTART_REFUND_TYPES, TaskQueue
    from kestrel.services import model_tasks as mt

    assert set(RESTART_REFUND_TYPES) == set(mt.TYPES)
    async with sm() as s:
        await mt.queue_pulls(s, ["qwen3:14b"])
    q = TaskQueue(sm)
    for _ in range(mt.MAX_ATTEMPTS + 3):  # more restarts than attempts
        task = await q.lease("a-process-that-dies", types=list(mt.TYPES))
        assert task is not None
        await mt.release_stale(sm)  # the next start
        task = await q.lease("another", types=list(mt.TYPES))
        await q.release_all_leased()  # a clean quit
        task = await q.lease("third", types=list(mt.TYPES), lease_seconds=-1)
        await q.reclaim_expired()  # a stale lease found by the worker's startup reclaim
    async with sm() as s:
        (t,) = (await s.execute(select(Task))).scalars().all()
    assert t.status == "pending" and t.attempts == 0
    # Only a real failure counts.
    task = await q.lease("w", types=list(mt.TYPES))
    assert await q.fail(task.id, "boom", worker_id="w") == "pending"
    async with sm() as s:
        assert (await s.get(Task, t.id)).attempts == 1


async def test_6_a_scoring_task_still_spends_its_attempt_on_a_restart(sm):
    from kestrel.queue.queue import TaskQueue

    q = TaskQueue(sm)
    await q.enqueue("t1", {"opportunity_id": 1})
    await q.lease("w", types=["t1"])
    await q.release_all_leased()
    async with sm() as s:
        (t,) = (await s.execute(select(Task))).scalars().all()
    assert t.attempts == 1


# ============================================ 7. any consulting artifact keeps both tracks
@pytest.mark.parametrize("artifact", ["source", "pursuit", "capability", "profile"])
def test_7_any_consulting_artifact_pins_both_tracks_on_a_worker_free_start(fresh, artifact):
    from kestrel.services import tracks

    async def seed(s):
        if artifact == "source":
            s.add(Source(track="consulting", kind="rss", name="News", config={}, enabled=True))
        elif artifact == "pursuit":
            o = Opportunity(track="employment", opp_type="job_posting", title="T",
                            url="https://x/p")
            s.add(o)
            await s.flush()
            s.add(Pursuit(opportunity_id=o.id, track="consulting", status="research"))
        elif artifact == "capability":
            s.add(DocMaster(track="consulting", doc_kind="capability_statement",
                            name="Capability statement"))
        await s.commit()

    _run(seed)
    if artifact == "profile":
        pe.profile_path().write_text(
            "consulting:\n  track: consulting\n  full_name: Pat\n", encoding="utf-8")
    with _client():  # worker=False: the check still runs on startup
        pass
    row = _run(lambda s: s.get(Setting, tracks.KEY))
    assert row is not None and row.value == ["employment", "consulting"]
    assert tracks.enabled_tracks_cached() == ("employment", "consulting")


def test_7_a_new_install_with_nothing_consulting_stays_employment_only(fresh):
    from kestrel.services import tracks

    _run(lambda s: _add_and_commit(
        s, Source(track="consulting", kind="rss", name="Off", config={}, enabled=False)))
    with _client():
        pass
    assert _run(lambda s: s.get(Setting, tracks.KEY)) is None
    assert tracks.enabled_tracks_cached() == ("employment",)


async def _add_and_commit(s, row):
    s.add(row)
    await s.commit()


# ============================================================================== 8. keys
async def test_8_a_short_key_never_shows_its_tail_and_a_long_one_shows_four(monkeypatch):
    from kestrel.llm import providers as prov
    from kestrel.web.pages import settings as settings_page

    monkeypatch.setattr(prov, "write_secret", lambda name, value: None)
    backend = prov.external_backends(get_settings())["frontier"]
    short = await settings_page._keybox_op("save", backend, "OpenRouter", "sk-12345")
    assert short["ok"] and "ending in" not in short["text"] and "2345" not in short["text"]
    long = await settings_page._keybox_op("save", backend, "OpenRouter", "sk-or-v1-abcdefgh9876")
    assert "ending in 9876" in long["text"]


def test_8_the_bundle_masks_every_saved_key_value_whatever_its_shape(monkeypatch):
    from kestrel import config
    from kestrel.services import diagnostics as diag

    odd = "plainwordsecret42"  # matches no token pattern
    assert not diag.looks_like_secret(odd)
    monkeypatch.setattr(config, "load_dotenv_into_environ", lambda: False)
    monkeypatch.setitem(config._dotenv_state, "secrets", {"ACME_API_KEY": odd})
    scrub = diag.make_scrubber(None, diag.saved_secret_values())
    out = scrub(f"request failed with key={odd} for user")
    assert odd not in out and diag.REDACTED in out


def test_8_the_key_file_write_is_flushed_and_dotenv_is_pinned(monkeypatch):
    import os

    from kestrel.llm import providers as prov

    synced: list[int] = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd)))
    prov.write_secret("OPENROUTER_API_KEY", "sk-or-v1-0000000000000000")
    assert synced  # the rewritten .env reached the disk
    prov.write_secret("OPENROUTER_API_KEY", "")
    assert re.search(r'"python-dotenv>=1\.2\.2,<2"', (ROOT / "pyproject.toml").read_text())


def test_8_the_test_button_copy_says_it_checks_the_key():
    text = (PKG / "web/templates/partials/hosted_keys.html").read_text("utf-8")
    assert "checks the key with the provider" in text and "or model list" not in text


# ==================================================================== 9. Ollama elsewhere
def test_9_a_remote_ollama_is_named_on_the_steps_and_buttons(monkeypatch):
    import dataclasses

    from kestrel.llm import profiles
    from kestrel.services import setup_state as ss

    assert ss.ollama_remote_host("http://127.0.0.1:11434") is None
    assert ss.ollama_remote_host("http://localhost:11434") is None
    assert ss.ollama_remote_host("http://[::1]:11434") is None
    assert ss.ollama_remote_host("http://gpu-box.lan:11434") == "gpu-box.lan"
    hw = profiles.Hardware(os="linux", gpu_vendor="nvidia", vram_gb=16.0, ram_gb=64.0)
    cat = profiles.load_catalog()
    base = ss.SetupState(
        choice="local", choice_explicit=True, hardware=hw, hardware_line="", recommended="",
        recommended_reason="", ollama_version="0.9.0", ollama_url="http://gpu-box.lan:11434",
        profile=cat.default_profile, profile_recorded=False, profile_reason="it fits",
        base_tags=["qwen3:4b"], missing_tags=["qwen3:4b"], derived_names=["kestrel-tier1"],
        missing_derived=["kestrel-tier1"], verified="none", verified_detail="",
        hosted_keys={}, hosted_acknowledged=False, hosted_problem=None, profile_yml=True,
        first_run=False,
    )
    steps = {s.key: s for s in ss.build_steps(base, cat)}
    assert steps["ollama"].title == "Ollama at gpu-box.lan"
    assert steps["pull"].action_label == "Download on gpu-box.lan"
    no_bases = dataclasses.replace(base, missing_tags=[])
    assert {s.key: s for s in ss.build_steps(no_bases, cat)}["build"].action_label == (
        "Build on gpu-box.lan")
    down = dataclasses.replace(base, ollama_version=None)
    step = {s.key: s for s in ss.build_steps(down, cat)}["ollama"]
    assert "gpu-box.lan" in step.title and step.link is None  # no "install it here" link
    local = dataclasses.replace(base, ollama_url="http://127.0.0.1:11434")
    steps = {s.key: s for s in ss.build_steps(local, cat)}
    assert steps["pull"].action_label == "Pull 1 model" and steps["ollama"].title == (
        "Ollama is running")
    doc = ss.__doc__ or ""
    assert "acts on this machine only" not in doc


# ======================================================== 10. one question normalization
def test_10_question_key_strips_collapses_and_casefolds():
    from kestrel.interview.stories import question_key

    assert question_key("  Tell me about a\n  TURNAROUND.  ") == question_key(
        "tell me about a turnaround.")


async def test_10_dismiss_matches_a_usage_whose_question_differs_only_in_spacing_or_case(sm):
    from kestrel.interview.stories import DISMISSED_KEY, consolidate_stories, dismiss_draft
    from tests.test_interview_stories import _add_eval, _FakeClient, _seed_opp, _sketch

    opp = await _seed_opp(sm)
    await _add_eval(sm, opp, [_sketch("Tell me about a turnaround.", "delta")])
    async with sm() as s:
        first = await consolidate_stories(s, _FakeClient(), get_settings())
    (story_id,) = first.created_story_ids
    async with sm() as s:
        (u,) = (await s.execute(select(StoryUsage))).scalars().all()
        u.question = "  tell me about a   TURNAROUND. "
        await s.commit()
    async with sm() as s:
        assert await dismiss_draft(s, story_id) is True
    async with sm() as s:
        assert (await s.get(Setting, DISMISSED_KEY)).value  # the sketch is remembered
        assert await s.get(Story, story_id) is None


# ================================================================ 11. words on the screen
def test_11_generated_lead_titles_have_no_em_dash():
    for rel in ("ingest/triggers.py", "ingest/signal_sources/sec_edgar.py",
                "ingest/signal_sources/pe_portfolio.py"):
        src = (PKG / rel).read_text("utf-8")
        assert not re.search(r'title=f"[^"]*—', src), rel


def test_11_history_names_the_provider_in_plain_words():
    from kestrel.web.queries import provider_display

    assert provider_display("https://openrouter.ai/api/v1", "frontier") == "OpenRouter"
    assert provider_display("https://api.anthropic.com/v1", "frontier") == "Anthropic"
    assert provider_display("https://llm.example.org/v1", "frontier") == "llm.example.org"
    src = (PKG / "web/queries.py").read_text("utf-8")
    assert '"Tier 3 transmission"' not in src and "Deeper read sent to" in src
    rail = (PKG / "web/templates/partials/status_rail.html").read_text("utf-8")
    assert "Tier 3" not in rail


def test_11_consulting_copy_and_prompts_assume_no_field():
    from kestrel.web.pages.onboarding import CONSULTING_PLAIN

    assert "CIO" not in CONSULTING_PLAIN and "CTO" not in CONSULTING_PLAIN
    for name in ("triage_consulting.v1.md", "evaluate_consulting.v1.md",
                 "engagement_outline.v1.md"):
        text = (PKG / "llm/prompts" / name).read_text("utf-8")
        assert not re.search(r"\bCIO\b|\bCTO\b", text), name
        assert "independent consultant or contractor" in text, name
    prompts_md = (ROOT / "PROMPTS.md").read_text("utf-8")
    assert "fractional CIO/CTO" not in prompts_md and "The CIO just resigned" not in prompts_md


def test_11_no_texas_in_shipped_examples():
    from kestrel.docs.sections import FIELDS

    examples = [ex for _label, fields in FIELDS.values() for *_x, ex in fields]
    assert "State of Texas" not in examples
    loc = (PKG / "web/templates/partials/profile_location.html").read_text("utf-8")
    assert "e.g. TX" not in loc
