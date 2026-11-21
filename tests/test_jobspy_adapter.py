"""JobSpy adapter: Indeed parameter-conflict + LinkedIn refusal enforced at CONFIG LOAD, and
DataFrame row -> RawPosting mapping. scrape_jobs is mocked; no live board is called from a test."""

from __future__ import annotations

import sys
import types

import pandas as pd
import pytest

from kestrel.ingest.adapters.jobspy import JobSpyAdapter
from kestrel.ingest.base import (
    AdapterError,
    NoticeKind,
    SourceConfigError,
    hedged_refusal,
    rate_limited,
    server_error,
)

_A = JobSpyAdapter()


def test_indeed_parameter_conflict_rejected_at_config_load():
    # hours_old AND is_remote both set for indeed is a conflict
    with pytest.raises(SourceConfigError, match="Indeed permits only ONE"):
        _A.validate_config(
            {"site_name": ["indeed"], "search_term": "vp it", "hours_old": 168, "is_remote": True}
        )


def test_indeed_single_group_is_ok():
    _A.validate_config({"site_name": ["indeed"], "search_term": "vp it", "hours_old": 168})
    _A.validate_config(
        {"site_name": ["indeed"], "search_term": "vp it", "job_type": "fulltime", "is_remote": True}
    )  # job_type+is_remote is ONE group, allowed


def test_linkedin_is_refused():
    """D-SRC-5: LinkedIn is never searched, whatever the source config says."""
    with pytest.raises(SourceConfigError, match="LinkedIn is not searched"):
        _A.validate_config({"site_name": ["indeed", "LinkedIn"], "search_term": "x"})


def test_requires_search_term():
    with pytest.raises(SourceConfigError):
        _A.validate_config({"site_name": ["indeed"]})


def test_row_to_posting_mapping():
    row = pd.Series(
        {
            "title": "VP of IT",
            "company": "Acme",
            "location": "Austin, TX",
            "job_url": "https://x/1",
            "job_url_direct": "https://apply/1",
            "description": "Lead IT.",
            "description_format": "markdown",
            "date_posted": "2026-07-20",
            "min_amount": 180000.0,
            "max_amount": 220000.0,
            "currency": "USD",
            "interval": "yearly",
            "is_remote": False,
            "job_type": "fulltime",
            "site": "indeed",
            "company_num_employees": "1001 to 5000 employees",
        }
    )
    p = _A._row_to_posting(row)
    assert p.title == "VP of IT" and p.org_name == "Acme" and p.location_raw == "Austin, TX"
    assert p.comp_min == 180000.0 and p.comp_max == 220000.0 and p.comp_interval == "year"
    assert p.description_md == "Lead IT." and p.description_html is None
    assert p.employment_type == "fulltime" and p.is_remote is False
    assert p.size_band == "1001 to 5000 employees"
    assert p.apply_url == "https://apply/1"
    assert (p.comp_interval_assumed, p.comp_currency_assumed) == (False, False)


@pytest.mark.parametrize("source, site, assumed", [
    ("description", "indeed", True), ("direct_data", "indeed", False), (None, "indeed", False),
    ("direct_data", "glassdoor", False), ("direct_data", "zip_recruiter", False),
    # Round 6: boards whose scraper fills the period or currency in itself.
    ("direct_data", "bayt", True), ("direct_data", "naukri", True),
    ("direct_data", "bdjobs", True), ("direct_data", "hellowork", True),
    ("direct_data", "google", True), ("direct_data", None, True),
])
def test_pay_read_from_the_description_has_an_assumed_period_and_currency(source, site, assumed):
    """Issue #208: JobSpy guesses the period of pay it finds in the text from the size of the
    figure and fills in USD, so neither is a stated value. Only boards whose scraper reads both
    from structured fields count as stated (round 6)."""
    row = pd.Series({"title": "Harbor planner", "job_url": "https://x/2", "min_amount": 40.0,
                     "max_amount": 50.0, "currency": "USD", "interval": "hourly",
                     "salary_source": source, "site": site})
    p = _A._row_to_posting(row)
    assert p.comp_interval == "hour"
    assert (p.comp_interval_assumed, p.comp_currency_assumed) == (assumed, assumed)


async def test_fetch_runs_scrape_and_maps(monkeypatch):
    df = pd.DataFrame(
        [
            {
                "title": "Head of IT",
                "company": "Beta",
                "location": "Remote",
                "job_url": "https://x/2",
                "description": "desc",
                "description_format": "markdown",
                "min_amount": None,
                "max_amount": None,
                "interval": None,
                "is_remote": True,
                "job_type": "fulltime",
                "site": "indeed",
            }
        ]
    )
    fake = types.ModuleType("jobspy")
    fake.scrape_jobs = lambda **kw: df
    monkeypatch.setitem(sys.modules, "jobspy", fake)
    postings = await _A.fetch({"site_name": ["indeed"], "search_term": "head of it"})
    assert (
        len(postings) == 1 and postings[0].title == "Head of IT" and postings[0].is_remote is True
    )


def test_every_parameter_kestrel_forwards_exists_in_jobspys_scrape_jobs():
    """JobSpy 1.3.0 signature check, offline: a renamed or removed parameter would otherwise
    surface as a TypeError at scan time, inside a try that reports it as a failed search."""
    import inspect

    import jobspy

    from kestrel.ingest.adapters.jobspy import _JOBSPY_PARAMS

    accepted = set(inspect.signature(jobspy.scrape_jobs).parameters)
    assert _JOBSPY_PARAMS <= accepted, sorted(_JOBSPY_PARAMS - accepted)


def test_default_boards_are_indeed_and_glassdoor_never_linkedin_or_google():
    from kestrel.ingest.adapters.jobspy import DEFAULT_SITES, _sites

    assert DEFAULT_SITES == ("indeed", "glassdoor")
    assert _sites({"site_name": ["linkedin"]}) == ["indeed", "glassdoor"]
    assert _sites({"site_name": ["google"]}) == ["google"]  # a custom source may still name it


async def test_a_failed_scrape_keeps_the_underlying_class_in_the_log(monkeypatch):
    """D-SEC-4: the stored sentence has no class name; the log still names the real failure."""
    from structlog.testing import capture_logs

    def boom(**kw):
        raise ConnectionResetError("board closed the connection")

    fake = types.ModuleType("jobspy")
    fake.scrape_jobs = boom
    monkeypatch.setitem(sys.modules, "jobspy", fake)
    with capture_logs() as logs:
        with pytest.raises(AdapterError) as caught:
            await _A.fetch({"site_name": ["indeed"], "search_term": "analyst"})
    assert "ConnectionResetError" not in str(caught.value)
    failed = [e for e in logs if e["event"] == "jobspy_scrape_failed"]
    assert len(failed) == 1
    assert failed[0]["error_type"] == "ConnectionResetError"
    assert failed[0]["error"] == "board closed the connection"


async def test_a_gulf_bayt_band_in_dollars_is_never_known_usd_per_month(sm):
    """Round 6: Bayt's "$3,000 - $4,000" arrives as USD per month, labeled direct; Kestrel
    stores both the currency and the period as not known."""
    from kestrel.db.models import Source
    from kestrel.ingest.normalize import normalize_and_store

    row = pd.Series({"title": "Port planner", "company": "Saltmarsh Docks", "job_url":
                     "https://x/bayt-1", "location": "Dubai, UAE", "description": "A role.",
                     "description_format": "markdown", "min_amount": 3000.0,
                     "max_amount": 4000.0, "currency": "USD", "interval": "monthly",
                     "salary_source": "direct_data", "site": "bayt"})
    async with sm() as s:
        src = Source(kind="jobspy", name="bayt test", track="employment", config={})
        s.add(src)
        await s.flush()
        opp = (await normalize_and_store(s, _A._row_to_posting(row), source=src)).opportunity
        assert (opp.comp_currency_known, opp.comp_interval_known) == (False, False)
async def test_a_glassdoor_search_asks_jobspy_for_the_posting_body(monkeypatch):
    """Issue 196 follow-up: Glassdoor postings arrived with no description. JobSpy 1.3.0 fetches
    a Glassdoor body only when `fetch_description` is true (its default is false), and the flag
    applies to every board in a call, so Glassdoor gets a call of its own with it and the other
    boards keep a call without it (D-WORKER-10 amended). The fake answers like JobSpy: no body
    unless asked."""
    seen: list[dict] = []

    def scrape_jobs(**kw):
        seen.append(kw)
        site = kw["site_name"][0]
        return pd.DataFrame([{
            "title": f"Invented Analyst {site}", "company": "Invented Co", "location": "Remote",
            "job_url": f"https://example.invalid/{site}/1",
            "description": "An invented posting body." if kw.get("fetch_description") else None,
            "site": site,
        }])

    fake = types.ModuleType("jobspy")
    fake.scrape_jobs = scrape_jobs
    monkeypatch.setitem(sys.modules, "jobspy", fake)
    postings = await _A.fetch({"site_name": ["indeed", "glassdoor", "zip_recruiter"],
                               "search_term": "analyst"})
    assert [(c["site_name"], c.get("fetch_description")) for c in seen] == [
        (["indeed", "zip_recruiter"], None), (["glassdoor"], True)]
    by_title = {p.title: p.description_md for p in postings}
    assert by_title["Invented Analyst glassdoor"] == "An invented posting body."
    assert by_title["Invented Analyst indeed"] is None
    assert getattr(postings, "partial_error", None) is None
    # Default boards (no site named) include Glassdoor, so the body is asked for there too.
    seen.clear()
    await _A.fetch({"search_term": "analyst"})
    assert [(c["site_name"], c.get("fetch_description")) for c in seen] == [
        (["indeed"], None), (["glassdoor"], True)]


def test_a_search_without_glassdoor_is_one_call_without_the_flag():
    """Other boards that honor the flag fetch one more page per posting, so it is only set for
    Glassdoor; a source that sets it itself is sent as one call, exactly as it asks."""
    assert _A._calls({"site_name": ["indeed"], "search_term": "x"}) == [
        _A._params({"site_name": ["indeed"], "search_term": "x"})]
    assert "fetch_description" not in _A._calls({"site_name": ["indeed"], "search_term": "x"})[0]
    own = _A._calls({"site_name": ["zip_recruiter", "glassdoor"], "search_term": "x",
                     "fetch_description": False})
    assert len(own) == 1 and own[0]["fetch_description"] is False
    assert _A._calls({"site_name": ["glassdoor"], "search_term": "x"}) == [
        {**_A._params({"site_name": ["glassdoor"], "search_term": "x"}),
         "fetch_description": True}]


async def test_one_board_failing_keeps_the_other_boards_roles_as_a_partial_run(monkeypatch):
    """A 429 from Glassdoor's own call no longer loses Indeed's roles: they are kept and the run
    is partial with the board named; both failing is still a failed fetch (partial at the
    runner, never an aborted batch)."""
    def scrape_jobs(**kw):
        if kw["site_name"] == ["glassdoor"]:
            raise RuntimeError("HTTP 429 Too Many Requests")
        return pd.DataFrame([{"title": "Invented Clerk", "company": "Invented Co",
                              "location": "Remote", "job_url": "https://example.invalid/i/1",
                              "description": "Body.", "site": "indeed"}])

    fake = types.ModuleType("jobspy")
    fake.scrape_jobs = scrape_jobs
    monkeypatch.setitem(sys.modules, "jobspy", fake)
    postings = await _A.fetch({"site_name": ["indeed", "glassdoor"], "search_term": "clerk"})
    assert [p.title for p in postings] == ["Invented Clerk"]
    # The cause of a plain exception is not proven: the shared hedged sentence, general kind.
    assert [(n.kind, n.sentence) for n in postings.notices] == [
        (NoticeKind.STOPPED, hedged_refusal("Glassdoor"))]
    assert postings.partial_error == hedged_refusal("Glassdoor")

    def down(**kw):
        raise RuntimeError("HTTP 429 Too Many Requests")

    fake.scrape_jobs = down
    with pytest.raises(AdapterError):
        await _A.fetch({"site_name": ["indeed", "glassdoor"], "search_term": "clerk"})


class _StatusError(Exception):
    """An invented board failure that carries an HTTP response, as requests' HTTPError does."""

    def __init__(self, status: int) -> None:
        super().__init__("invented")
        self.response = types.SimpleNamespace(status_code=status)


@pytest.mark.parametrize(("status", "kind", "notice"), [
    (429, NoticeKind.RATE_LIMITED, rate_limited),
    (503, NoticeKind.SERVER_ERROR, server_error),
])
async def test_a_board_failure_with_a_status_takes_its_shared_kind(monkeypatch, status, kind,
                                                                   notice):
    """A board call that failed with a 429 or a 5xx on its response gets that shared kind and
    its shared sentence; the other board's roles are kept."""
    def scrape_jobs(**kw):
        if kw["site_name"] == ["glassdoor"]:
            raise _StatusError(status)
        return pd.DataFrame([{"title": "Invented Clerk", "company": "Invented Co",
                              "location": "Remote", "job_url": "https://example.invalid/i/1",
                              "description": "Body.", "site": "indeed"}])

    fake = types.ModuleType("jobspy")
    fake.scrape_jobs = scrape_jobs
    monkeypatch.setitem(sys.modules, "jobspy", fake)
    postings = await _A.fetch({"site_name": ["indeed", "glassdoor"], "search_term": "clerk"})
    assert [p.title for p in postings] == ["Invented Clerk"]
    assert [(n.kind, n.sentence) for n in postings.notices] == [
        (kind, notice("Glassdoor").sentence)]


def test_jobspy_still_leaves_glassdoor_bodies_off_by_default():
    """The reason for the flag, read from the installed JobSpy (offline): if a later JobSpy turns
    it on by default this test says the flag is no longer needed."""
    import inspect

    import jobspy

    assert inspect.signature(jobspy.scrape_jobs).parameters["fetch_description"].default is False


async def test_a_missing_jobspy_package_is_a_clean_source_failure(monkeypatch):
    """Round 11, finding 5: the import sits inside the adapter's failure handling, as on the
    base, so a broken install fails the source with an AdapterError, not an unexpected error."""
    monkeypatch.setitem(sys.modules, "jobspy", None)  # importing it raises ImportError
    with pytest.raises(AdapterError):
        await _A.fetch({"site_name": ["indeed"], "search_term": "clerk"})

