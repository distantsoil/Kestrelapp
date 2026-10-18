"""The About page (D-UI-6): version, the GitHub and Ko-fi links, the split from Help, and the
diagnostics bundle. The bundle is the privacy-sensitive part: a tester forwards it to a reviewer,
so the tests seed a database, a log, a .env and an API key and then assert that no secret value,
no posting text and no employer name survives anywhere in the zip bytes, that quarantine raw
output is cut short, and that the temp archive is gone once the response has been sent.
Cloud-safe: a real migrated SQLite file, no model, no network (doctor is replaced by a saved
report or a stub)."""

from __future__ import annotations

import asyncio
import io
import re
import zipfile
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

import kestrel
from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import (
    DocMaster,
    DocSection,
    Opportunity,
    Quarantine,
    Run,
    ScrapeRun,
    Setting,
    Source,
)
from kestrel.services import diagnostics
from kestrel.services import setup_state as ss
from kestrel.web.pages import about as about_page

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

GITHUB = "https://github.com/distantsoil/Kestrelapp"
KOFI = "https://ko-fi.com/distantsoil"
_EMPLOYER = "Northwind Freight"
_PLACEHOLDER = "[Prior employer]"
_ENV_KEY = "sk-or-v1-0123456789abcdefghijklmnopqrstuvwxyz"
_SETTINGS_KEY = "om-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_DOTENV_VALUE = "dotenv-value-must-not-leak-9f8e7d"
_BEARER = "Bearer eyJhbGciOiJIUzI1NiJ9.leaked-token-value"
_POSTING = "Confidential VP of Infrastructure search for a logistics group"
_EXPECTED_MEMBERS = {
    "README.txt", "summary.json", "doctor.json", "settings.redacted.yml",
    "logs/kestrel.log", "quarantine.json", "packages.txt",
}


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


async def _seed(*, confidential: bool) -> None:
    """A migrated database with one row in every table the bundle counts or lists."""
    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        src = Source(track="employment", kind="jobspy", name="boards", enabled=True)
        s.add(src)
        await s.flush()
        s.add(ScrapeRun(source_id=src.id, status="partial", counts={"found": 3},
                        error=f"429 from board; {_BEARER}; last org {_EMPLOYER}"))
        s.add(Run(name="nightly", status="done", mode="local", counts={"scanned": 3}))
        s.add(Opportunity(track="employment", opp_type="job", title=_POSTING,
                          org_name=_EMPLOYER, description_md=_POSTING, stage="tier2"))
        s.add(Quarantine(tier=2, model="kestrel-tier2", error="schema: verdict missing",
                         raw_response=(f"{_POSTING} at {_EMPLOYER}. " * 60)))
        master = DocMaster(track="employment", name="Executive resume", doc_kind="resume")
        s.add(master)
        await s.flush()
        s.add(DocSection(doc_master_id=master.id, section_kind="role", sort_order=0,
                         org_label=_EMPLOYER, org_label_redacted=_PLACEHOLDER))
        if confidential:
            s.add(Setting(key="confidential_mode", value=True))
        await s.commit()
    await engine.dispose()
    forget_engine()


def _write_log_and_dotenv() -> None:
    paths.ensure_dirs()
    (paths.logs_dir() / "kestrel.log").write_text(
        "\n".join(
            [f'{{"event": "old line {i}"}}' for i in range(2100)]
            + [
                f'{{"event": "scan", "org": "{_EMPLOYER}", "auth": "{_BEARER}"}}',
                f'{{"event": "key", "x-api-key": "{_ENV_KEY}"}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    paths.dotenv_path().write_text(f"OPENMODEL_API_KEY={_DOTENV_VALUE}\n", encoding="utf-8")


def _save_doctor_report() -> None:
    from kestrel.doctor import DoctorReport

    rep = DoctorReport()
    rep.add("Python", "PASS", "3.12")
    rep.add("Ollama", "WARN", "not answering", "start ollama serve")
    ss.save_report(rep)


@pytest.fixture
def _secrets(monkeypatch):
    """An API key in the environment, one in Settings, and a .env in the data home."""
    from kestrel.services.setting_overrides import load_settings_with_db as real_load

    monkeypatch.setenv("OPENROUTER_API_KEY", _ENV_KEY)
    _write_log_and_dotenv()

    async def _settings_with_key(session, **kw):
        s = await real_load(session, **kw)
        s.llm.backends["local"].api_key = _SETTINGS_KEY
        return s

    monkeypatch.setattr(about_page, "load_settings_with_db", _settings_with_key)
    yield
    paths.dotenv_path().unlink(missing_ok=True)


def _zip(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


# ---------------------------------------------------------------------------------------- page
def test_about_renders_version_links_and_credits():
    with _client() as c:
        r = c.get("/about")
    assert r.status_code == 200
    body = r.text
    main = body.split("<main")[1]
    assert "<h1>About</h1>" in body
    assert f'id="about-version">{kestrel.__version__}<' in body
    assert "source checkout" in body and "Python 3." in body
    assert str(paths.data_home()) in body
    assert f'href="{GITHUB}" target="_blank" rel="noopener"' in body
    assert f'href="{KOFI}" target="_blank" rel="noopener"' in body
    assert KOFI in body
    assert "Kestrel is free. If it earned you an interview" in body
    assert "issues and feature requests go here" in body
    for name in ("career-ops", "JobSpy", "parametric-resume-builder"):
        assert f">{name}</a> (MIT)" in body, name
    built, inspired = body.split("<h2>Inspired by</h2>")
    assert ">JobSpy</a>" in built.split("<h2>Built on</h2>")[1]
    assert ">career-ops</a>" in inspired and ">parametric-resume-builder</a>" in inspired
    assert 'href="/about/notices"' in body
    assert 'href="/about/diagnostics"' in body
    for name, _what in diagnostics.CONTENTS:
        assert name in main, name
    for item in diagnostics.EXCLUDED:
        assert item in main, item
    assert "never applies, emails or messages anyone for you" in body
    assert "—" not in main


def test_help_keeps_its_content_and_nav_lists_both_pages():
    from kestrel.ingest.catalog import source_catalog

    with _client() as c:
        helped = c.get("/help")
        about = c.get("/about")
    assert helped.status_code == 200
    assert "<h1>Help</h1>" in helped.text
    for row in source_catalog():
        assert row.kind in helped.text, row.kind
    assert 'hx-post="/updates/check"' in helped.text
    assert 'href="/about">About</a>' in helped.text.split("<main")[1]
    for body in (helped.text, about.text):
        side = body.split('<aside class="side"')[1].split("</aside>")[0]
        assert 'href="/help"' in side and ">Help</a>" in side
        assert 'href="/about"' in side and ">About</a>" in side
        assert '{label: "About", href: "/about"}' in body  # the command palette
    assert 'href="/about" class="active"' in about.text
    assert 'href="/help" class="active"' in helped.text


# --------------------------------------------------------------------------------- diagnostics
@pytest.mark.usefixtures("_secrets")
def test_diagnostics_zip_has_the_members_and_no_secret_or_posting_text(monkeypatch):
    asyncio.run(_seed(confidential=False))
    _save_doctor_report()
    discarded: list[Path] = []
    original = diagnostics.discard_bundle

    def _spy(archive):
        discarded.append(Path(archive))
        original(archive)

    monkeypatch.setattr(diagnostics, "discard_bundle", _spy)

    with _client() as c:
        r = c.get("/about/diagnostics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/zip")
    assert re.search(
        rf'filename="kestrel-diagnostics-{re.escape(kestrel.__version__)}-\d{{8}}-\d{{6}}(-\d+)?\.zip"',
        r.headers["content-disposition"],
    )
    members = _zip(r.content)
    assert set(members) == _EXPECTED_MEMBERS

    # Nothing secret and no database file anywhere in the bytes. The posting text may survive in
    # ONE place, the truncated quarantine raw response (documented on the page), nowhere else.
    for name, data in members.items():
        for forbidden in (_ENV_KEY, _SETTINGS_KEY, _DOTENV_VALUE, _BEARER):
            assert forbidden.encode() not in data, (name, forbidden)
        assert b"SQLite format" not in data, name
        if name != "quarantine.json":
            assert _POSTING.encode() not in data, name
    assert not any(".env" in n or n.endswith(".db") or "profile" in n for n in members)

    summary = yaml.safe_load(members["summary.json"])
    assert summary["version"] == kestrel.__version__
    assert summary["frozen"] is False
    assert summary["schema_revision"] == migrate.head_revision()
    assert summary["counts"]["opportunities_by_stage"] == {"tier2": 1}
    assert summary["counts"]["quarantine"] == 1 and summary["counts"]["runs"] == 1
    assert summary["counts"]["scrape_runs_by_status"] == {"partial": 1}
    assert summary["runs"][0]["name"] == "nightly" and summary["runs"][0]["mode"] == "local"
    assert summary["scrape_runs"][0]["source_kind"] == "jobspy"
    assert "<redacted>" in summary["scrape_runs"][0]["error"]
    assert summary["hosted_mode"]["backends"]["frontier"]["api_key_env"] == "OPENROUTER_API_KEY"
    assert summary["hosted_mode"]["backends"]["frontier"]["key_present"] is True
    assert "os" in summary["hardware"]

    doctor = yaml.safe_load(members["doctor.json"])
    assert doctor["source"] == "saved report"
    assert {r["name"] for r in doctor["results"]} == {"Python", "Ollama"}
    assert doctor["results"][1]["remediation"] == "start ollama serve"

    settings = yaml.safe_load(members["settings.redacted.yml"])
    assert settings["settings"]["llm"]["backends"]["local"]["api_key"] == "<redacted>"
    assert settings["settings"]["llm"]["backends"]["frontier"]["api_key_env"] == (
        "OPENROUTER_API_KEY"
    )
    assert settings["settings"]["llm"]["hosted_mode"]["max_jd_tokens"] == 3000  # an int stays
    assert settings["environment"]["api_key_env"]["OPENROUTER_API_KEY"] is True
    assert settings["environment"]["api_key_env"]["OPENMODEL_API_KEY"] is True  # from .env
    assert "OPENROUTER_API_KEY" in settings["environment"]["set"]

    log = members["logs/kestrel.log"].decode()
    assert log.startswith("# kestrel.log: last 2000 of 2102 lines")
    assert '"old line 99"' not in log and '"old line 2099"' in log
    assert log.count("<redacted>") >= 2

    quarantine = yaml.safe_load(members["quarantine.json"])
    row = quarantine["rows"][0]
    assert row["tier"] == 2 and row["model"] == "kestrel-tier2"
    assert len(row["raw_response"]) <= diagnostics.RAW_RESPONSE_CHARS
    assert row["raw_response_truncated"] is True

    readme = members["README.txt"].decode()
    for name, _what in diagnostics.CONTENTS:
        assert name in readme
    for item in diagnostics.EXCLUDED:
        assert item in readme
    assert b"fastapi==" in members["packages.txt"]

    # The BackgroundTask ran after the response and removed the temp archive and its directory.
    assert len(discarded) == 1
    assert not discarded[0].exists()
    assert not discarded[0].parent.exists()


@pytest.mark.usefixtures("_secrets")
def test_diagnostics_redacts_the_employer_when_confidential_mode_has_a_map():
    asyncio.run(_seed(confidential=True))
    _save_doctor_report()
    with _client() as c:
        r = c.get("/about/diagnostics")
    assert r.status_code == 200
    members = _zip(r.content)
    for name, data in members.items():
        assert _EMPLOYER.encode() not in data, name
        assert _EMPLOYER.lower().encode() not in data, name
    assert _PLACEHOLDER in members["logs/kestrel.log"].decode()
    assert _PLACEHOLDER in members["quarantine.json"].decode()
    assert _PLACEHOLDER in members["summary.json"].decode()  # the scrape-run error string
    assert yaml.safe_load(members["summary.json"])["confidential_mode"] is True


@pytest.mark.usefixtures("_secrets")
def test_diagnostics_off_confidential_keeps_the_employer_and_cuts_the_raw_response():
    """Confidential mode off: the employer label is not secret and stays. The quarantine raw
    response is the one member that can quote a posting; it is cut, not dropped, and the page
    says so. The posting text never reaches the summary (no titles, no descriptions)."""
    asyncio.run(_seed(confidential=False))
    _save_doctor_report()
    with _client() as c:
        r = c.get("/about/diagnostics")
    members = _zip(r.content)
    assert _EMPLOYER in members["logs/kestrel.log"].decode()
    raw = yaml.safe_load(members["quarantine.json"])["rows"][0]["raw_response"]
    assert _POSTING in raw and len(raw) == diagnostics.RAW_RESPONSE_CHARS
    assert _POSTING.encode() not in members["summary.json"]


def test_diagnostics_runs_quick_doctor_when_no_report_was_saved(monkeypatch):
    from kestrel import doctor as doctor_mod

    calls: list[bool] = []

    def _fake(*, quick: bool = False):
        calls.append(quick)
        rep = doctor_mod.DoctorReport()
        rep.add("Database", "PASS", "at head")
        return rep

    monkeypatch.setattr(doctor_mod, "run_doctor", _fake)
    asyncio.run(_seed(confidential=False))
    with _client() as c:
        r = c.get("/about/diagnostics")
    doctor = yaml.safe_load(_zip(r.content)["doctor.json"])
    assert calls == [True]
    assert doctor["source"] == "quick run"
    assert doctor["results"] == [
        {"name": "Database", "status": "PASS", "detail": "at head", "remediation": ""}
    ]


def test_build_bundle_without_a_database_or_logs(tmp_path, monkeypatch):
    """A fresh home: no database file, no log folder, no saved report. Still a complete zip, and
    a doctor that blows up is a line in doctor.json rather than a failed download."""
    from kestrel import doctor as doctor_mod
    from kestrel.config import get_settings

    def _boom(*, quick: bool = False):
        raise RuntimeError("ollama exploded")

    monkeypatch.setattr(doctor_mod, "run_doctor", _boom)
    archive = diagnostics.build_bundle(
        get_settings(), out_dir=tmp_path, redaction={"northwind freight": _PLACEHOLDER}
    )
    assert archive.parent == tmp_path
    members = _zip(archive.read_bytes())
    assert set(members) == _EXPECTED_MEMBERS - {"logs/kestrel.log"}
    summary = yaml.safe_load(members["summary.json"])
    assert summary["counts"] == "database not initialized"
    assert summary["schema_revision"] is None
    assert yaml.safe_load(members["quarantine.json"])["rows"] == []
    doctor = yaml.safe_load(members["doctor.json"])
    assert doctor["error"] == "RuntimeError: ollama exploded" and doctor["results"] == []


def test_redact_tree_masks_secret_names_and_shaped_values_only():
    tree = {
        "api_key": "ollama",
        "api_key_env": "OPENROUTER_API_KEY",
        "shared_secret": "abc",
        "password": None,
        "max_tokens": 2048,
        "max_jd_tokens": 3000,
        "extra_headers": {"Authorization": _BEARER, "X-Title": "Kestrel"},
        "note": f"paste {_ENV_KEY} here",
        "nested": [{"token": "t"}, "plain"],
    }
    out = diagnostics.redact_tree(tree)
    assert out["api_key"] == "<redacted>"
    assert out["api_key_env"] == "OPENROUTER_API_KEY"
    assert out["shared_secret"] == "<redacted>"
    assert out["password"] is None
    assert out["max_tokens"] == 2048 and out["max_jd_tokens"] == 3000
    assert out["extra_headers"] == {"Authorization": "<redacted>", "X-Title": "Kestrel"}
    assert out["note"] == "paste <redacted> here"
    assert out["nested"] == [{"token": "<redacted>"}, "plain"]
    assert diagnostics.mask_tokens(f"x-api-key: {_SETTINGS_KEY}") == "<redacted>"


def test_discard_only_removes_a_staging_directory_it_made(tmp_path):
    keep = tmp_path / "real-data"
    keep.mkdir()
    stray = keep / "kestrel-diagnostics-x.zip"
    stray.write_bytes(b"PK")
    diagnostics.discard_bundle(stray)
    assert not stray.exists()
    assert keep.exists()


def test_diagnostics_counts_tier0_set_asides_by_rule_name_only():
    """D-UI-67: the bundle says which rules set roles aside, by rule name, and never carries
    the title or org a stored reason quotes."""
    import sqlite3

    asyncio.run(_seed(confidential=False))
    reasons = [
        f"blocker:anti_title: {_POSTING}", "blocker:anti_title: help desk",
        "blocker:geo: outside Austin", f"blocker:{_EMPLOYER} rule: x", None,
        # Finding 8: an operator-written rule id can name an employer; only shipped ids are named.
        "blocker:exclude_globex_corp: x",
    ]

    async def add() -> None:
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            s.add_all([Opportunity(track="employment", opp_type="job", title=_POSTING,
                                   org_name=_EMPLOYER, stage="t0_reject", stage_reason=why)
                       for why in reasons])
            await s.commit()
        await engine.dispose()
        forget_engine()

    asyncio.run(add())
    conn = sqlite3.connect(str(paths.db_path()))
    counts = diagnostics.tier0_rejects_by_rule(conn)
    conn.close()
    assert counts == {"anti_title": 2, "geo": 1, "custom": 2, "unknown": 1}
    with _client() as c:
        members = _zip(c.get("/about/diagnostics").content)
    summary = yaml.safe_load(members["summary.json"])
    assert summary["counts"]["tier0_rejects_by_rule"] == counts
    assert isinstance(summary["counts"]["evaluations_by_model"], dict)  # model ids per evaluation
    assert _POSTING.encode() not in members["summary.json"]
    counted = str(summary["counts"]["tier0_rejects_by_rule"])
    assert _EMPLOYER not in counted and "help desk" not in counted
    assert "globex" not in counted


# ------------------------------------------------------------------------- updates and notes
def test_about_has_a_check_for_updates_button_where_the_site_link_was():
    """The top card's link row offers Check for updates (the same /updates/check every other
    surface uses) in place of the kestrelapp.co button, with the result shown in the card."""
    with _client() as c:
        body = c.get("/about").text
    row = body.split('data-testid="site-links"')[1].split("</p>")[0]
    assert 'hx-post="/updates/check"' in row and ">Check for updates</button>" in row
    assert 'href="https://kestrelapp.co"' not in row, "the site button should be replaced here"
    assert 'href="https://ko-fi.com/distantsoil"' in row  # Ko-fi stays
    # Targets the check's response swaps into, including its out-of-band "Last checked" line;
    # without them the click renders nothing and htmx logs an error.
    assert 'id="update-result"' in body and 'id="update-last-checked"' in body


def test_about_links_the_release_notes_beside_the_version():
    with _client() as c:
        body = c.get("/about").text
    line = body.split('id="about-version"')[1].split("</div>")[0]
    assert f'href="{GITHUB}/blob/main/CHANGELOG.md"' in line and ">Release notes</a>" in line
    assert 'class="btn' not in line, "a plain text link, not a button"


def test_the_about_check_reports_through_the_shared_route():
    with _client() as c:
        r = c.post("/updates/check")
    assert r.status_code == 200 and 'data-testid="update-result"' in r.text
