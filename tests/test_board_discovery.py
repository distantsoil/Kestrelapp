"""Company boards found in the job searches (D-SRC-7, SPEC.md 5.1): an Indeed or Google posting
whose link goes to a company's own Greenhouse, Lever, Ashby, Workday or SmartRecruiters board
becomes a source for that company, watched like the search that found it. Cloud-safe: no network,
the JobSpy adapter is replaced by a stub that returns RawPostings."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from kestrel.db.models import Source
from kestrel.ingest import board_discovery as bd
from kestrel.ingest.base import RawPosting
from kestrel.ingest.http import HttpClient
from kestrel.ingest.runner import run_source


@pytest.mark.parametrize(
    ("url", "kind", "config"),
    [
        ("https://boards.greenhouse.io/northwind/jobs/4012345", "greenhouse",
         {"token": "northwind"}),
        ("https://job-boards.greenhouse.io/northwind/jobs/4012345?gh_src=abc", "greenhouse",
         {"token": "northwind"}),
        ("https://boards.greenhouse.io/embed/job_app?for=northwind&token=4012345", "greenhouse",
         {"token": "northwind"}),
        ("https://jobs.lever.co/ridgeline/5b1c-uuid/apply", "lever", {"company": "ridgeline"}),
        ("https://jobs.ashbyhq.com/Cascade/2f6e-uuid", "ashby", {"token": "Cascade"}),
        ("https://acme.wd5.myworkdayjobs.com/en-US/External/job/Austin-TX/Analyst_R123",
         "workday", {"tenant": "acme", "dc": "wd5", "site": "External"}),
        ("https://acme.wd1.myworkdayjobs.com/AcmeCareers/job/Remote/Nurse_R9", "workday",
         {"tenant": "acme", "dc": "wd1", "site": "AcmeCareers"}),
        ("https://jobs.smartrecruiters.com/BlueHarbor/743999-teacher", "smartrecruiters",
         {"company": "BlueHarbor"}),
    ],
)
def test_a_company_board_link_names_its_platform_and_company(url, kind, config):
    ref = bd.board_from_url(url)
    assert ref is not None and (ref.kind, ref.config) == (kind, config)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "https://www.indeed.com/viewjob?jk=abc123",
        "https://www.google.com/search?q=jobs",
        "https://boards.greenhouse.io/embed/job_app?token=4012345",  # no company named
        "https://boards.greenhouse.io/",
        "https://jobs.lever.co/",
        "https://acme.wd5.myworkdayjobs.com/en-US/",  # no site
        "https://jobs.eu.lever.co/ridgeline/5b1c",  # EU hosts use other APIs the adapters lack
        "https://evil.example/boards.greenhouse.io/northwind/jobs/1",
        "https://boards.greenhouse.io.evil.example/northwind/jobs/1",
        "not a url",
    ],
)
def test_anything_else_is_not_a_company_board(url):
    assert bd.board_from_url(url) is None


def _posting(n: int, url: str | None, apply_url: str | None = None, org: str = "Northwind"):
    return RawPosting(external_id=f"jp-{n}", url=url, apply_url=apply_url,
                      title=f"Role {n}", org_name=org, raw={"site": "indeed"})


async def _search(sm, *, enabled: bool = True, track: str = "employment") -> Source:
    async with sm() as s:
        src = Source(track=track, kind="jobspy", name="Jobs near Springfield",
                     config={"search_term": "x"}, enabled=enabled, origin="generated")
        s.add(src)
        await s.commit()
        return src


async def _sources(sm) -> list[Source]:
    async with sm() as s:
        return list((await s.execute(select(Source).order_by(Source.id))).scalars().all())


async def test_found_boards_become_sources_watched_like_the_search(sm):
    search = await _search(sm)
    postings = [
        _posting(1, "https://www.indeed.com/viewjob?jk=1",
                 "https://boards.greenhouse.io/northwind/jobs/1"),
        _posting(2, "https://www.indeed.com/viewjob?jk=2",
                 "https://boards.greenhouse.io/northwind/jobs/2"),  # same board once
        _posting(3, "https://jobs.lever.co/ridgeline/abc", None, org="Ridgeline Utilities"),
        _posting(4, "https://www.indeed.com/viewjob?jk=4", None),  # no board link
    ]
    async with sm() as s:
        found = await bd.discover_boards(s, postings, search=await s.get(Source, search.id))
        await s.commit()
    assert found == 2
    rows = [r for r in await _sources(sm) if r.origin == bd.DISCOVERED]
    assert [(r.kind, r.config, r.enabled, r.track) for r in rows] == [
        ("greenhouse", {"token": "northwind"}, True, "employment"),
        ("lever", {"company": "ridgeline"}, True, "employment"),
    ]
    assert rows[0].name == "Northwind jobs (Greenhouse)"
    assert rows[1].name == "Ridgeline Utilities jobs (Lever)"
    assert all(r.cadence_hours == 24 for r in rows)


async def test_a_search_that_is_off_finds_boards_that_start_off(sm):
    search = await _search(sm, enabled=False)
    async with sm() as s:
        await bd.discover_boards(
            s, [_posting(1, None, "https://jobs.ashbyhq.com/Cascade/1", org="Cascade")],
            search=await s.get(Source, search.id),
        )
        await s.commit()
    (row,) = [r for r in await _sources(sm) if r.origin == bd.DISCOVERED]
    assert row.enabled is False


async def test_a_board_already_watched_or_turned_off_is_never_added_again(sm):
    """A company board the operator added by hand, or a found one they turned off, is the
    operator's word: discovery neither duplicates it nor turns it back on. Greenhouse and Lever
    names match ignoring case; Ashby and SmartRecruiters names are case sensitive."""
    search = await _search(sm)
    async with sm() as s:
        s.add_all([
            Source(track="employment", kind="greenhouse", name="Northwind jobs (Greenhouse)",
                   config={"token": "Northwind"}, enabled=True, origin="custom"),
            Source(track="employment", kind="lever", name="Ridgeline jobs (Lever)",
                   config={"company": "ridgeline"}, enabled=False, origin=bd.DISCOVERED),
        ])
        await s.commit()
        found = await bd.discover_boards(s, [
            _posting(1, None, "https://boards.greenhouse.io/northwind/jobs/1"),
            _posting(2, None, "https://jobs.lever.co/ridgeline/2", org="Ridgeline"),
            _posting(3, None, "https://jobs.ashbyhq.com/cascade/3", org="Cascade"),
        ], search=await s.get(Source, search.id))
        await s.commit()
    assert found == 1
    rows = await _sources(sm)
    assert [(r.kind, r.enabled) for r in rows if r.kind == "lever"] == [("lever", False)]
    assert [r.config for r in rows if r.kind == "greenhouse"] == [{"token": "Northwind"}]
    assert [r.config for r in rows if r.kind == "ashby"] == [
        {"token": "cascade", "company_name": "Cascade"}
    ]


async def test_boards_are_found_per_track_and_capped(sm, monkeypatch):
    """At most MAX_DISCOVERED found boards per track (hard rule 7: every one is fetched once a
    day). Past the cap nothing more is added; the count says how many were left out."""
    monkeypatch.setattr(bd, "MAX_DISCOVERED", 2)
    search = await _search(sm)
    postings = [
        _posting(i, None, f"https://boards.greenhouse.io/company{i}/jobs/{i}", org=f"Co {i}")
        for i in range(4)
    ]
    async with sm() as s:
        found = await bd.discover_boards(s, postings, search=await s.get(Source, search.id))
        await s.commit()
        assert found == 2
        assert await bd.discover_boards(s, postings, search=await s.get(Source, search.id)) == 0
        await s.commit()
    rows = [r for r in await _sources(sm) if r.origin == bd.DISCOVERED]
    assert [r.config["token"] for r in rows] == ["company0", "company1"]


class _StubJobSpy:
    kind = "jobspy"

    def __init__(self, postings):
        self.postings = postings

    def validate_config(self, config):
        return None

    async def fetch(self, config, http):
        return self.postings


async def test_a_search_run_finds_boards_and_a_discovery_failure_never_fails_the_run(
    sm, monkeypatch
):
    import kestrel.ingest.runner as runner_mod

    search = await _search(sm)
    stub = _StubJobSpy([
        _posting(1, "https://www.indeed.com/viewjob?jk=1",
                 "https://acme.wd5.myworkdayjobs.com/en-US/External/job/X/Y_R1", org="Acme"),
    ])
    monkeypatch.setattr(runner_mod, "get_adapter", lambda kind: stub)
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        outcome = await run_source(sm, search.id, http=http)
    assert outcome.status == "ok" and outcome.counts["boards_found"] == 1
    assert outcome.counts["by_site"] == {"indeed": 1}
    (row,) = [r for r in await _sources(sm) if r.origin == bd.DISCOVERED]
    assert (row.kind, row.config) == (
        "workday", {"tenant": "acme", "dc": "wd5", "site": "External", "company": "Acme"}
    )

    async def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(runner_mod.board_discovery, "discover_boards", boom)
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        again = await run_source(sm, search.id, http=http)
    assert again.status == "ok" and again.counts["new"] + again.counts["reseen"] == 1


async def test_only_job_searches_look_for_boards(sm, monkeypatch):
    """A company board's own postings link to itself; only a JobSpy search discovers."""
    import kestrel.ingest.runner as runner_mod

    async with sm() as s:
        gh = Source(track="employment", kind="greenhouse", name="gh",
                    config={"token": "northwind"}, enabled=True, origin="custom")
        s.add(gh)
        await s.commit()
    stub = _StubJobSpy([_posting(1, "https://boards.greenhouse.io/other/jobs/1", org="Other")])
    stub.kind = "greenhouse"
    monkeypatch.setattr(runner_mod, "get_adapter", lambda kind: stub)
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        outcome = await run_source(sm, gh.id, http=http)
    assert "boards_found" not in outcome.counts
    assert [r.origin for r in await _sources(sm)] == ["custom"]


# ------------------------------------------------------------------ grnh.se short links
async def _discover_with_http(sm, postings):
    search = await _search(sm)
    async with HttpClient(user_agent="test", respect_robots=False) as http, sm() as s:
        found = await bd.discover_boards(
            s, postings, search=await s.get(Source, search.id), http=http
        )
        await s.commit()
    return found


async def test_a_greenhouse_short_link_is_followed_one_hop_to_its_board(sm):
    import httpx
    import respx

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get("https://grnh.se/abc123").mock(return_value=httpx.Response(
            302, headers={"location": "https://boards.greenhouse.io/northwind/jobs/4012345"}))
        found = await _discover_with_http(sm, [_posting(1, "https://grnh.se/abc123")])
    assert found == 1 and route.call_count == 1
    (row,) = [r for r in await _sources(sm) if r.origin == bd.DISCOVERED]
    assert (row.kind, row.config) == ("greenhouse", {"token": "northwind"})


async def test_a_short_link_that_fails_or_leads_elsewhere_is_skipped_quietly(sm):
    import httpx
    import respx

    with respx.mock() as mock:
        mock.get("https://grnh.se/gone").mock(return_value=httpx.Response(404))
        mock.get("https://grnh.se/other").mock(return_value=httpx.Response(
            301, headers={"location": "https://example.com/careers"}))
        mock.get("https://grnh.se/boom").mock(side_effect=httpx.ConnectError("down"))
        found = await _discover_with_http(sm, [
            _posting(1, "https://grnh.se/gone"), _posting(2, "https://grnh.se/other"),
            _posting(3, "https://grnh.se/boom"),
        ])
    assert found == 0


async def test_short_link_resolutions_are_capped_per_run_and_never_repeated(sm):
    import httpx
    import respx

    with respx.mock() as mock:
        route = mock.get(url__regex=r"https://grnh\.se/.*").mock(
            return_value=httpx.Response(404))
        postings = [_posting(i, f"https://grnh.se/l{i}") for i in range(30)]
        postings.append(_posting(99, "https://grnh.se/l0"))
        await _discover_with_http(sm, postings)
    assert route.call_count == bd.MAX_SHORT_LINKS


async def test_without_a_client_a_short_link_is_just_not_a_board(sm):
    search = await _search(sm)
    async with sm() as s:
        found = await bd.discover_boards(
            s, [_posting(1, "https://grnh.se/abc123")], search=await s.get(Source, search.id)
        )
    assert found == 0
