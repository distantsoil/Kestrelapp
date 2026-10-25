"""Sources page redesign (D-SRC-3) and the consulting switch (D-UI-28). Offline: every HTTP call
is mocked with respx against the captured board fixtures; no model, no live endpoint."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
from kestrel.db.models import ScrapeRun, Source
from kestrel.ingest.board_lookup import candidate_slugs
from kestrel.ingest.generated import rebuild_generated_sources
from kestrel.services import profile_edit as pe
from kestrel.services import tracks

FIXTURES = Path(__file__).parent / "fixtures"
EMP_TERMS = {"target_titles": ["VP of IT", "Head of IT"], "anti_titles": ["Intern"]}
GEO = {"remote_ok": True, "remote_country_allowlist": ["US"], "onsite_states": ["TX"],
       "onsite_metros": [{"name": "Austin", "aliases": ["Austin"]}]}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from kestrel.web import routes_control

    async def _down(*a, **k):
        return None

    monkeypatch.setattr(routes_control, "_probe_ollama", _down, raising=False)
    tracks.forget_cache()
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()
    yield
    tracks.forget_cache()
    asyncio.run(reset_engine())

INDEED_EX = "Indeed: Operations Analyst, Columbus OH"


def _both():
    """A new install is employment-only; these tests need the consulting track on."""
    from kestrel.db.engine import get_sessionmaker

    async def _go():
        async with get_sessionmaker()() as s:
            await tracks.save_enabled(s, tracks.BOTH)
            await s.commit()

    _run(_go())


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _run(coro):
    async def _wrap():
        try:
            return await coro
        finally:
            await reset_engine()

    return asyncio.run(_wrap())


async def _rows() -> dict[str, Source]:
    async with get_sessionmaker()() as s:
        return {r.name: r for r in (await s.execute(select(Source))).scalars().all()}


def _profile(consulting: bool = False) -> None:
    pe.save_section("employment", "about", {"full_name": "Pat", "email": "pat@example.com"})
    pe.save_section("employment", "employment", {"employment_terms": EMP_TERMS})
    pe.save_section("employment", "location", {"geo_rules": GEO})
    if consulting:
        pe.save_section("consulting", "about", {"full_name": "Pat", "email": "pat@example.com"})
        pe.save_section("consulting", "consulting",
                        {"consulting_terms": {"service_lines": ["IT integration"]}})


async def _build(enable: bool | None = None) -> None:
    async with get_sessionmaker()() as s:
        await rebuild_generated_sources(s, enable=enable)
        await s.commit()


def _without_details(html: str) -> str:
    return re.sub(r"<details.*?</details>", "", html, flags=re.S)


# ------------------------------------------------------------------------------ your searches
def test_searches_are_listed_by_summary_with_no_query_text_by_default():
    _profile()
    _run(_build(enable=True))
    with _client() as c:
        html = c.get("/sources").text
        sid = _run(_rows())["Jobs near Austin"].id
        drawer = c.get(f"/sources/{sid}/drawer").text
    assert 'data-testid="your-searches"' in html
    assert "within 50 miles of Austin, TX" in html and "Remote VP of IT" in html
    section = html.split('data-testid="group-boards"')[1].split('data-testid="group-ats"')[0]
    assert section.count('data-testid="source-row"') == 2
    assert "Intern" not in section and "search_term" not in section  # query only in details
    assert "From your profile" in section  # the origin tag
    details = drawer.split('data-testid="drawer-details"')[1]
    assert "Show details" in drawer and "search_term" in details


def test_no_searches_points_at_the_profile():
    with _client() as c:
        html = c.get("/sources").text
    assert 'data-testid="no-searches"' in html and "/profile?tab=employment" in html


def test_the_switch_turns_only_generated_searches_on_and_off():
    _profile()

    async def _setup():
        await _build()
        async with get_sessionmaker()() as s:
            s.add(Source(track="employment", kind="greenhouse", name="Mine",
                         config={"token": "acme"}, enabled=True, origin="custom"))
            s.add(Source(track="employment", kind="lever", name="Mine off",
                         config={"company": "acme"}, enabled=False, origin="custom"))
            await s.commit()

    _run(_setup())
    with _client() as c:
        r = c.post("/sources/searches", data={"enable": "1"})
        assert r.status_code == 200 and "Turn searching off" in r.text
        rows = _run(_rows())
        assert rows["Jobs near Austin"].enabled and rows["Remote jobs"].enabled
        assert rows["Mine"].enabled and not rows["Mine off"].enabled
        r = c.post("/sources/searches", data={"enable": "0"})
        assert "Turn searching on" in r.text
    rows = _run(_rows())
    assert not rows["Jobs near Austin"].enabled and not rows["Remote jobs"].enabled
    assert rows["Mine"].enabled and not rows["Mine off"].enabled


def test_editing_a_generated_search_makes_it_custom_and_a_rebuild_leaves_it_alone():
    _profile()
    _run(_build())
    row = _run(_rows())["Jobs near Austin"]
    config = {**row.config, "search_term": '"CIO"', "distance": 25}
    with _client() as c:
        r = c.post(f"/sources/{row.id}", data={
            "name": row.name, "track": "employment", "kind": "jobspy",
            "cadence_hours": "12", "config": json.dumps(config),
        })
        assert r.status_code == 200
        assert "no longer follows your profile" in c.get(f"/sources/{row.id}/drawer").text
    edited = _run(_rows())["Jobs near Austin"]
    assert edited.origin == "custom" and edited.config["search_term"] == '"CIO"'
    _run(_build())  # a profile save rebuilds
    _run(_build(enable=True))
    rows = _run(_rows())
    same_name = [r for r in rows.values() if r.name == "Jobs near Austin"]
    assert len(same_name) == 1 and same_name[0].config["distance"] == 25
    assert same_name[0].origin == "custom" and not same_name[0].enabled


def test_rows_added_by_hand_are_custom():
    with _client() as c:
        r = c.post("/sources", data={
            "name": "Acme board", "track": "employment", "kind": "greenhouse",
            "cadence_hours": "24", "config": '{"token": "acme"}',
        })
        assert r.status_code == 200
    assert _run(_rows())["Acme board"].origin == "custom"


def test_boards_found_in_searches_are_marked_and_counted_and_switch_off_alone():
    """D-SRC-7: a found board sits with the other sources, says where it came from, and turns
    off on its own row without touching the searches."""
    async def _seed():
        async with get_sessionmaker()() as s:
            s.add(Source(track="employment", kind="greenhouse", name="Northwind jobs (Greenhouse)",
                         config={"token": "northwind"}, enabled=True, origin="discovered"))
            await s.commit()

    with _client() as c:
        _run(_seed())
        page = c.get("/sources").text
        assert 'data-testid="found-boards">1 company board' in page
        assert "found in your searches." in page
        assert "Found by Kestrel" in page.split('data-testid="group-ats"')[1]
        sid = _run(_rows())["Northwind jobs (Greenhouse)"].id
        assert c.post(f"/sources/{sid}/toggle").status_code == 200
    row = _run(_rows())["Northwind jobs (Greenhouse)"]
    assert row.enabled is False and row.origin == "discovered"


# ----------------------------------------------------------------------- company board lookup
def test_candidate_slugs_drop_suffixes_and_keep_case_where_it_matters():
    slugs = candidate_slugs("Acme Robotics, Inc.")
    assert slugs["greenhouse"] == ["acmerobotics", "acme-robotics"]
    assert slugs["lever"] == ["acmerobotics", "acme-robotics"]
    assert slugs["ashby"][0] == "AcmeRobotics"
    assert "AcmeRobotics" in slugs["smartrecruiters"]
    assert candidate_slugs("LLC") == {}


def test_company_board_lookup_finds_a_lever_board_and_adds_it():
    lever = json.loads((FIXTURES / "lever.json").read_text(encoding="utf-8"))
    with respx.mock(assert_all_called=False) as mock:
        hit = mock.get("https://api.lever.co/v0/postings/acme?mode=json").mock(
            return_value=httpx.Response(200, json=lever))
        mock.route().mock(return_value=httpx.Response(404, json={}))
        with _client() as c:
            r = c.post("/sources/company-board", data={"company": "Acme, LLC"})
    assert r.status_code == 200
    assert "Found a Lever board called acme with 2 open jobs" in r.text
    assert "switched off" in r.text  # the operator confirms the company (review finding 12)
    assert hit.called
    ua = hit.calls.last.request.headers["user-agent"]
    assert ua.startswith("Kestrel/")  # the descriptive agent (hard rule 7)
    row = _run(_rows())["Acme jobs (Lever)"]
    assert row.kind == "lever" and row.config == {"company": "acme"}
    assert row.origin == "custom" and row.track == "employment" and not row.enabled


def test_company_board_lookup_failure_says_so_and_adds_nothing():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route().mock(return_value=httpx.Response(404, json={}))
        with _client() as c:
            r = c.post("/sources/company-board", data={"company": "Nobody Corp"})
    assert r.status_code == 200
    assert "No public Greenhouse, Lever, Ashby or SmartRecruiters board was found" in r.text
    assert 'data-testid="add-source"' in r.text  # Add a source is right there
    assert 0 < route.call_count <= 12  # bounded
    assert _run(_rows()) == {}


def test_a_greenhouse_board_with_no_jobs_is_not_a_match():
    gh = json.loads((FIXTURES / "greenhouse.json").read_text(encoding="utf-8"))
    url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url).mock(return_value=httpx.Response(200, json={"jobs": []}))
        mock.route().mock(return_value=httpx.Response(404))
        with _client() as c:
            r = c.post("/sources/company-board", data={"company": "Acme"})
    assert "No public" in r.text
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url).mock(return_value=httpx.Response(200, json=gh))
        mock.route().mock(return_value=httpx.Response(404))
        with _client() as c:
            r = c.post("/sources/company-board", data={"company": "Acme"})
    assert "Found a Greenhouse board called acme" in r.text
    assert _run(_rows())["Acme jobs (Greenhouse)"].config == {"token": "acme"}


# ------------------------------------------------------------------------------------ SAM.gov
def test_sam_gov_help_is_present_and_the_key_is_saved_to_env_then_enabled():
    from kestrel.config import get_secret
    from kestrel.web.app import _seed_shipped_sources

    _run(_seed_shipped_sources())
    _both()
    with _client() as c:
        html = c.get("/sources").text
        assert 'data-testid="sam-help"' in html
        assert "Login.gov" in html and "Account Details" in html and "API key" in html
        assert "federal contract" in html
        r = c.post("/sources/sam-gov/key", data={"api_key": ""})
        assert r.status_code == 400
        r = c.post("/sources/sam-gov/key", data={"api_key": "sam-test-key-1234"})
        assert r.status_code == 200 and "Key saved" in r.text
        assert "sam-test-key-1234" not in r.text  # never echoed
    env = paths.dotenv_path().read_text(encoding="utf-8")
    assert "SAM_GOV_API_KEY=sam-test-key-1234" in env
    assert get_secret("SAM_GOV_API_KEY") == "sam-test-key-1234"
    sam = _run(_rows())["SAM.gov federal IT contracts"]
    assert sam.enabled and sam.kind == "sam_gov"


# ----------------------------------------------------------------------- RemoteRocketship (#38)
_RRS_KEY = "rrs-test-key-98765432"


def test_remoterocketship_card_is_on_the_page_and_lean():
    with _client() as c:
        html = c.get("/sources").text
    card = html.split('data-testid="rrs-card"', 1)[1].split("<!-- /rrs -->", 1)[0]
    assert "RemoteRocketship" in card and "own paid subscription" in card
    assert 'type="password"' in card and 'href="/help#remoterocketship"' in card
    assert "—" not in card and ".env" not in card
    assert 'data-testid="rrs-switch"' in card
    assert _RRS_KEY not in html


def test_remoterocketship_saving_a_key_writes_env_and_never_echoes_it(caplog, capsys):
    from kestrel.config import get_secret

    with _client() as c:
        r = c.post("/sources/remoterocketship/key", data={"api_key": ""})
        assert r.status_code == 200 and "Paste your key first" in r.text
        r = c.post("/sources/remoterocketship/key", data={"api_key": _RRS_KEY})
        assert r.status_code == 200 and "Key saved" in r.text
        assert _RRS_KEY not in r.text and _RRS_KEY not in c.get("/sources").text
        assert _RRS_KEY[-4:] in r.text  # only a masked fingerprint
    assert f"REMOTEROCKETSHIP_API_KEY={_RRS_KEY}" in paths.dotenv_path().read_text("utf-8")
    assert get_secret("REMOTEROCKETSHIP_API_KEY") == _RRS_KEY
    out = capsys.readouterr()
    assert _RRS_KEY not in caplog.text and _RRS_KEY not in out.out + out.err
    assert "RemoteRocketship" not in _run(_rows())  # saving a key alone adds no source


def test_remoterocketship_turn_on_without_a_key_is_refused():
    with _client() as c:
        r = c.post("/sources/remoterocketship/toggle", data={"on": "1"})
    assert r.status_code == 200 and "Add your key first" in r.text
    assert "RemoteRocketship" not in _run(_rows())


def test_remoterocketship_turn_on_creates_then_turn_off_disables_the_source():
    with _client() as c:
        c.post("/sources/remoterocketship/key", data={"api_key": _RRS_KEY})
        r = c.post("/sources/remoterocketship/toggle", data={"on": "1"})
        assert r.status_code == 200 and "Turn off" in r.text
        row = _run(_rows())["RemoteRocketship"]
        assert row.kind == "remoterocketship" and row.enabled and row.track == "employment"
        assert "api_key" not in (row.config or {}) and _RRS_KEY not in json.dumps(row.config)
        c.post("/sources/remoterocketship/toggle", data={"on": "0"})
        assert not _run(_rows())["RemoteRocketship"].enabled
        c.post("/sources/remoterocketship/toggle", data={"on": "1"})
        rows = [r for r in _run(_rows()).values() if r.kind == "remoterocketship"]
        assert len(rows) == 1 and rows[0].enabled  # re-enabled, not duplicated


def test_remoterocketship_run_now_queues_a_run(monkeypatch):
    from kestrel.web.pages import sources as page

    calls: list[int] = []

    async def _fake(source_id, override=None, label=None):
        calls.append(source_id)
        return "ok: {}"

    monkeypatch.setattr(page, "_trigger_run", _fake)
    with _client() as c:
        c.post("/sources/remoterocketship/key", data={"api_key": _RRS_KEY})
        c.post("/sources/remoterocketship/toggle", data={"on": "1"})
        sid = _run(_rows())["RemoteRocketship"].id
        html = c.get("/sources").text
        assert f'hx-post="/sources/{sid}/run-now"' in html.split("<!-- /rrs -->", 1)[0]
        r = c.post(f"/sources/{sid}/run-now", headers={"HX-Request": "true"})
        assert r.status_code == 200
    assert calls == [sid]


def test_remoterocketship_write_paths_never_500(monkeypatch):
    from kestrel.llm import providers

    def _boom(name, value):
        raise OSError("disk full: " + value)

    monkeypatch.setattr(providers, "write_secret", _boom)
    with _client() as c:
        r = c.post("/sources/remoterocketship/key", data={"api_key": _RRS_KEY})
    assert r.status_code == 200 and _RRS_KEY not in r.text


# --------------------------------------------------------------- retired example sources
def test_retired_examples_are_deleted_at_startup_only_when_untouched():
    from kestrel.web.app import _seed_shipped_sources

    async def _setup():
        async with get_sessionmaker()() as s:
            s.add_all([
                Source(track="employment", kind="jobspy", name=INDEED_EX,
                       config={}, enabled=False, origin="shipped"),
                Source(track="employment", kind="greenhouse", name="Greenhouse — example board",
                       config={"token": "x"}, enabled=True, origin="shipped"),
                Source(track="employment", kind="lever", name="Lever — example board",
                       config={"company": "x"}, enabled=False, origin="shipped",
                       last_run_at="2026-10-01T00:00:00"),
                Source(track="consulting", kind="sec_edgar",
                       name="SEC EDGAR — 8-K trigger events", config={}, enabled=False,
                       origin="custom"),
                Source(track="consulting", kind="rss", name="Company newsroom / layoff RSS",
                       config={}, enabled=False, origin="shipped"),
                Source(track="consulting", kind="sam_gov", name="SAM.gov — IT services RFPs",
                       config={"api_key_env": "SAM_GOV_API_KEY"}, enabled=False,
                       origin="shipped"),
            ])
            await s.flush()
            rss = (await s.execute(select(Source).where(Source.kind == "rss"))).scalar_one()
            s.add(ScrapeRun(source_id=rss.id, status="ok"))
            await s.commit()
        await _seed_shipped_sources()
        await _seed_shipped_sources()
        return await _rows()

    # An install seeded before D-SRC-3 has the old examples in its own sources.yml.
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    (paths.config_dir() / "sources.yml").write_text(
        'employment:\n  - {kind: jobspy, name: "' + INDEED_EX + '", config: {}}\n'
        'consulting:\n  - {kind: sam_gov, name: "SAM.gov — IT services RFPs", config: {}}\n',
        encoding="utf-8",
    )
    rows = _run(_setup())
    assert INDEED_EX not in rows  # untouched: gone, and not re-seeded
    assert "Greenhouse — example board" in rows  # enabled
    assert "Lever — example board" in rows  # has run
    assert "SEC EDGAR — 8-K trigger events" in rows  # edited
    assert "Company newsroom / layoff RSS" in rows  # has a scrape run
    assert "SAM.gov federal IT contracts" in rows and "SAM.gov — IT services RFPs" not in rows


def test_the_shipped_file_carries_only_sam_gov():
    from kestrel.ingest.sources import load_source_specs

    specs = load_source_specs()
    assert [(s["kind"], s["enabled"]) for s in specs] == [("sam_gov", False)]


# ------------------------------------------------------------------- consulting switch (D-UI-28)
def test_consulting_off_hides_it_everywhere_and_on_brings_it_back():
    from kestrel.web.app import _seed_shipped_sources

    _run(_seed_shipped_sources())
    _both()
    with _client() as c:
        assert 'class="track-switch"' in c.get("/help").text
        assert 'data-tab="consulting"' in c.get("/profile").text
        assert 'data-track="consulting"' in c.get("/profile?tab=about").text
        src = c.get("/sources?track=consulting").text
        assert "SAM.gov federal IT contracts" in src and 'data-testid="sam-help"' in src
        assert "SAM.gov federal IT contracts" not in c.get("/sources?track=employment").text
        assert 'data-testid="st-tracks"' in c.get("/settings").text

        r = c.post("/settings/tracks", data={"tracks": ["employment"]})
        assert r.status_code == 200 and r.headers.get("HX-Refresh") == "true"
        assert 'class="track-switch"' not in c.get("/help").text
        assert "kestrel-track" in c.get("/help").text
        assert 'data-tab="consulting"' not in c.get("/profile").text
        assert 'data-track="consulting"' not in c.get("/profile?tab=about").text
        assert c.get("/profile?tab=consulting").status_code == 200  # the route still works
        src = c.get("/sources?track=consulting").text  # a switched-off track never shows
        assert "SAM.gov" not in src and "pe-portfolio" not in src
        assert '<option value="sam_gov"' not in src

        c.post("/settings/tracks", data={"tracks": ["employment", "consulting"]})
        assert 'class="track-switch"' in c.get("/help").text
        assert 'data-tab="consulting"' in c.get("/profile").text
        assert "SAM.gov federal IT contracts" in c.get("/sources?track=consulting").text
    assert "SAM.gov federal IT contracts" in _run(_rows())  # the data never left


def test_tracks_need_at_least_one():
    with _client() as c:
        r = c.post("/settings/tracks", data={})
    assert r.status_code == 400 and "Pick at least one track." in r.text
    assert tracks.enabled_tracks_cached() == ("employment",)  # a new install: employment only


def test_saving_tracks_rebuilds_the_generated_searches():
    _profile(consulting=True)
    _both()
    _run(_build())
    names = set(_run(_rows()))
    assert "Fractional and interim roles" in names
    with _client() as c:
        c.post("/settings/tracks", data={"tracks": ["employment"]})
        names = set(_run(_rows()))
        assert "Fractional and interim roles" not in names and "Jobs near Austin" in names
        c.post("/settings/tracks", data={"tracks": ["employment", "consulting"]})
    names = set(_run(_rows()))
    assert "Fractional and interim roles" in names
    assert "SEC filings that may signal a need for your work" in names


def test_a_job_search_row_shows_postings_per_board_and_flags_a_silent_one():
    from kestrel.db.models import Opportunity
    from kestrel.ingest.adapters.jobspy import DEFAULT_SITES

    assert DEFAULT_SITES == ("indeed", "glassdoor")
    _profile()
    _run(_build(enable=True))

    async def _seed():
        async with get_sessionmaker()() as s:
            src = (await s.execute(select(Source).where(Source.kind == "jobspy"))).scalars().first()
            run = ScrapeRun(source_id=src.id, status="ok", started_at="2026-10-18T10:00:00+00:00")
            s.add(run)
            await s.flush()
            for i in range(3):
                s.add(Opportunity(
                    track="employment", opp_type="job", source_id=src.id, scrape_run_id=run.id,
                    external_id=f"x{i}", title=f"t{i}", org_name="Org",
                    last_seen_at="2026-10-18T10:05:00+00:00", raw={"site": "indeed"},
                ))
            s.add(Opportunity(  # seen in an earlier run: not this run's
                track="employment", opp_type="job", source_id=src.id, external_id="old",
                title="old", org_name="Org", last_seen_at="2026-10-01T10:05:00+00:00",
                raw={"site": "glassdoor"},
            ))
            await s.commit()

    _run(_seed())
    with _client() as c:
        html = c.get("/sources").text
        sid = _run(_rows())["Jobs near Austin"].id
        drawer = c.get(f"/sources/{sid}/drawer").text
    assert "Indeed 3, Glassdoor 0" in " ".join(drawer.split())
    assert "Glassdoor found nothing this check." in html
    assert "Indeed found nothing" not in html and "Indeed found nothing" not in drawer
