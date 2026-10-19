"""JobSpy adapter: Indeed parameter-conflict + LinkedIn refusal enforced at CONFIG LOAD, and
DataFrame row -> RawPosting mapping. scrape_jobs is mocked; no live board is called from a test."""

from __future__ import annotations

import sys
import types

import pandas as pd
import pytest

from kestrel.ingest.adapters.jobspy import JobSpyAdapter
from kestrel.ingest.base import SourceConfigError

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
