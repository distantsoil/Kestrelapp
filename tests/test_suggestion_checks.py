"""#123: suggested companies on Sources are checked in the background (D-SRC-19). Offline: every
request is mocked with respx. Invariants under test: only public lookups with the descriptive
User-Agent, at most once per company per 30 days, a failed lookup is never "not found", the check
is a resumable task row, and nothing becomes a source until Add."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
from kestrel.db.models import Opportunity, Org, Pursuit, Source, Task
from kestrel.ingest.board_lookup import find_company_board, name_variants
from kestrel.ingest.orgs import normalized_name
from kestrel.services import source_suggestions as ss
from kestrel.services import tracks
from kestrel.web.app import build_app

HX = {"HX-Request": "true"}
GH_JOBS = {"jobs": [{"id": 1, "title": "Planner", "absolute_url": "https://example.test/1",
                     "location": {"name": "Remote"}, "content": "x", "updated_at": "2026-01-01"}]}


@pytest.fixture(autouse=True)
def _fresh():
    tracks.forget_cache()
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()
    yield
    tracks.forget_cache()
    asyncio.run(reset_engine())


def _run(coro):
    async def wrap():
        try:
            return await coro
        finally:
            await reset_engine()

    return asyncio.run(wrap())


def _pursuit(org: str, url: str | None = None, group: str | None = None,
             status: str = "applied", extra_copies: tuple[str, ...] = ()) -> None:
    async def go():
        async with get_sessionmaker()() as s:
            o = Org(name=org, normalized_name=normalized_name(org))
            s.add(o)
            await s.flush()
            opp = Opportunity(track="employment", opp_type="job", external_id=org, title="Role",
                              org_name=org, org_id=o.id, url=url, dedup_group_id=group)
            s.add(opp)
            await s.flush()
            s.add(Pursuit(opportunity_id=opp.id, track="employment", status=status))
            for i, copy_url in enumerate(extra_copies):
                s.add(Opportunity(track="employment", opp_type="job", external_id=f"{org}{i}",
                                  title="Role", org_name=org, org_id=o.id, url=copy_url,
                                  dedup_group_id=group, is_canonical=False))
            await s.commit()

    _run(go())


def _board(name: str) -> dict | None:
    async def go():
        async with get_sessionmaker()() as s:
            return (await ss.board_cache(s)).get(normalized_name(name))

    return _run(go())


def _tasks() -> list[Task]:
    async def go():
        async with get_sessionmaker()() as s:
            return list((await s.scalars(select(Task).where(Task.type == ss.TASK_TYPE))).all())

    return _run(go())


def _list_suggestions(track: str = "employment", names: list[str] | None = None) -> list[dict]:
    async def go():
        async with get_sessionmaker()() as s:
            return await ss.suggestions_with_boards(s, track, names or [], queue_checks=True)

    return _run(go())


# ------------------------------------------------------------------ names with parentheses
def test_a_name_with_parentheses_probes_the_plain_name_first_then_the_names_inside():
    name = "Larkspur Foods (Ridgeway Group / Quillon)"
    assert name_variants(name) == ["Larkspur Foods", "Ridgeway Group", "Quillon"]
    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "quillon" in str(request.url) and "greenhouse" in str(request.url):
            return httpx.Response(200, json=GH_JOBS)
        return httpx.Response(404, json={})

    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(side_effect=answer)

        async def go():
            async with await ss.http_client() as http:
                return await find_company_board(name, http)

        result = _run(go())
    assert result.match is not None and result.match.config == {"token": "quillon"}
    first = [u for u in seen if "larkspurfoods" in u]
    assert first and seen.index(first[0]) < min(
        i for i, u in enumerate(seen) if "quillon" in u), seen  # plain name before the alias
    assert not any("larkspurfoodsridgeway" in u for u in seen)  # never the glued slug
    assert len(seen) <= 12 + 1  # the existing request ceiling still holds


# ------------------------------------------------------------------ link first, zero requests
def test_a_board_link_on_a_duplicate_copy_marks_the_company_found_with_no_request():
    _pursuit("Harbor Lantern Freight", url="https://aggregator.example.test/job/77", group="g1",
             extra_copies=("https://job-boards.greenhouse.io/harborlantern/jobs/4567",))
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(return_value=httpx.Response(500))
        outcome = _run(ss.check_company("Harbor Lantern Freight"))
        assert mock.calls.call_count == 0
    assert outcome == "found"
    entry = _board("Harbor Lantern Freight")
    assert entry["kind"] == "greenhouse" and entry["config"] == {"token": "harborlantern"}
    assert entry["platform"] == "Greenhouse" and entry["checked_at"]
    assert _run(_source_count()) == 0  # found is not added


def _source_count():
    async def go():
        async with get_sessionmaker()() as s:
            return (await s.execute(select(func.count()).select_from(Source))).scalar_one()

    return go()


def test_the_pursued_roles_own_link_is_read_too_and_a_foreign_board_link_is_not():
    _pursuit("Cobalt Meadow Labs", url="https://jobs.lever.co/cobaltmeadow/abc")
    with respx.mock(assert_all_called=False) as mock:
        assert _run(ss.check_company("Cobalt Meadow Labs")) == "found"
        assert mock.calls.call_count == 0
    assert _board("Cobalt Meadow Labs")["kind"] == "lever"


# ------------------------------------------------------------------ results, expiry, failures
def test_a_definite_not_found_is_kept_for_30_days_and_asked_again_after(monkeypatch):
    _pursuit("Nobody Corp")
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(return_value=httpx.Response(404, json={}))
        assert _run(ss.check_company("Nobody Corp")) == "none"
        assert _list_suggestions()[0]["state"] == "none"
        assert _tasks() == []  # nothing queued while the answer is fresh
        later = ss._now() + timedelta(days=ss.EXPIRY_DAYS - 1)
        monkeypatch.setattr(ss, "_now", lambda: later)
        assert _list_suggestions()[0]["state"] == "none" and _tasks() == []
        later = ss._now() + timedelta(days=2)
        monkeypatch.setattr(ss, "_now", lambda: later)
        row = _list_suggestions()[0]
    assert row["state"] == "checking" and len(_tasks()) == 1  # expired: checked again


@pytest.mark.parametrize(
    "answer",
    [
        {"side_effect": httpx.ConnectError("down")},
        {"side_effect": httpx.ReadTimeout("slow")},
        {"return_value": httpx.Response(429)},
        {"return_value": httpx.Response(503)},
    ],
)
def test_a_failed_lookup_is_not_remembered_as_not_found_and_the_row_says_try_again(answer):
    _pursuit("Tidewater Freight")
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(**answer)
        assert _run(ss.check_company("Tidewater Freight")) == "error"
    assert _board("Tidewater Freight") is None  # no cached "not found"
    row = _list_suggestions()[0]
    assert row["state"] == "error" and row["board"] is None
    with _client() as c:
        sug = c.get("/sources").text.split('data-testid="group-sug"')[1].split(
            'data-testid="group-boards"')[0]
    assert "Could not check. Try again" in sug
    assert 'data-testid="suggestion-try-again"' in sug and 'data-testid="suggestion-add"' not in sug


def test_a_failed_check_is_retried_by_the_background_only_after_the_retry_window(monkeypatch):
    _pursuit("Tidewater Freight")
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(side_effect=httpx.ConnectError("down"))
        _run(ss.check_company("Tidewater Freight"))
    assert _list_suggestions()[0]["state"] == "error" and _tasks() == []
    later = ss._now() + timedelta(hours=ss.ERROR_RETRY_HOURS + 1)
    monkeypatch.setattr(ss, "_now", lambda: later)
    assert _list_suggestions()[0]["state"] == "checking" and len(_tasks()) == 1


# ------------------------------------------------------------------ the queued task
def _client():
    from fastapi.testclient import TestClient

    return TestClient(build_app(worker=False))


def test_an_answer_to_a_post_queues_no_check_but_a_page_view_does():
    """A refused or finished action never adds work (the contract tests count task rows)."""
    _pursuit("Tidewater Freight")
    with _client() as c:
        r = c.post("/sources/suggestions/dismiss", data={"name": "Nobody"}, headers=HX)
        assert 'data-state="checking"' in r.text and _tasks() == []
        c.get("/sources")
    assert len(_tasks()) == 1


def test_listing_suggestions_queues_one_task_per_company_and_makes_no_request():
    _pursuit("Tidewater Freight")
    _pursuit("Ironbridge Supply", status="shortlisted")
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(return_value=httpx.Response(500))
        with _client() as c:
            html = c.get("/sources").text
            c.get("/sources")  # a second view queues nothing new
        assert mock.calls.call_count == 0  # never a request in the foreground
    assert html.count('data-state="checking"') == 2
    names = sorted((t.payload or {})["name"] for t in _tasks())
    assert names == ["Ironbridge Supply", "Tidewater Freight"]
    assert _run(_source_count()) == 0  # the list never adds anything by itself


def test_a_check_cut_short_by_a_quit_is_continued_after_the_restart():
    _pursuit("Cobalt Meadow Labs", url="https://jobs.lever.co/cobaltmeadow/abc")
    _list_suggestions()  # queues the check
    assert len(_tasks()) == 1

    async def killed_mid_check():
        from kestrel.queue.queue import TaskQueue

        sm = get_sessionmaker()
        queue = TaskQueue(sm)
        leased = await queue.lease("pipeline-suggestions", types=[ss.TASK_TYPE])
        assert leased is not None
        await queue.release_all_leased()  # what a quit does to a leased task

    _run(killed_mid_check())
    assert _board("Cobalt Meadow Labs") is None and _tasks()[0].status == "pending"

    async def restart():
        return await ss.drain_checks(get_sessionmaker())

    assert _run(restart()) == 1
    assert _board("Cobalt Meadow Labs")["found"] is True
    assert [t.status for t in _tasks()] == ["done"]


def test_one_pass_checks_at_most_the_cap_and_leaves_the_rest_queued():
    for i in range(5):
        _pursuit(f"Quillfeather {i}", url=f"https://jobs.lever.co/quillfeather{i}/a")

    async def enqueue_all():
        async with get_sessionmaker()() as s:
            for i in range(5):
                await ss.enqueue_check(s, f"Quillfeather {i}")
            await s.commit()

    _run(enqueue_all())

    async def one_pass():
        return await ss.drain_checks(get_sessionmaker())

    assert _run(one_pass()) == ss.MAX_PER_PASS
    left = [t for t in _tasks() if t.status == "pending"]
    assert len(left) == 5 - ss.MAX_PER_PASS


def test_a_check_goes_out_with_the_descriptive_user_agent_and_one_request_per_probe():
    _pursuit("Nobody Corp")
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404, json={})

    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(side_effect=answer)
        _run(ss.check_company("Nobody Corp"))
    assert seen and all("Kestrel" in r.headers.get("user-agent", "") for r in seen)
    assert len(seen) <= 13

    async def client_settings():
        http = await ss.http_client()
        return http.respect_robots, http.user_agent

    robots, ua = _run(client_settings())
    assert robots is True and "Kestrel" in ua  # robots.txt is checked by the shared client


# ------------------------------------------------------------------ Check again, Add
def test_check_again_forgets_the_answer_and_queues_a_check_without_a_request():
    _pursuit("Nobody Corp")
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(return_value=httpx.Response(404, json={}))
        _run(ss.check_company("Nobody Corp"))
        before = mock.calls.call_count
        with _client() as c:
            page = c.get("/sources").text
            assert 'data-testid="suggestion-check-again"' in page
            r = c.post("/sources/suggestions/recheck", data={"name": "Nobody Corp"}, headers=HX)
        assert mock.calls.call_count == before
    assert 'data-state="checking"' in r.text and _board("Nobody Corp") is None
    assert len(_tasks()) == 1


def test_pressing_add_when_the_lookup_cannot_answer_updates_the_row_on_the_first_press():
    """#123 (b): the first response carries the row's new state; nothing is cached as not found."""
    _pursuit("Tidewater Freight")
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(side_effect=httpx.ConnectError("down"))
        with _client() as c:
            r = c.post("/sources/suggestions/add", data={"name": "Tidewater Freight"}, headers=HX)
    sug = r.text.split('data-testid="group-sug"')[1].split('data-testid="group-boards"')[0]
    assert "Could not check. Try again" in sug and 'data-testid="suggestion-add"' not in sug
    assert _board("Tidewater Freight") is None and _run(_source_count()) == 0


# ------------------------------------------------------------------ covered company matching
@pytest.mark.parametrize(
    "company,source",
    [
        ("Monarca Food Solutions (Evans Group / Palmex)", "Monarca Food Solutions"),
        ("Mindful Health Solutions (Radial)", "Mindful Health Solutions, Inc."),
        ("Brightwater Systems LLC", "Brightwater Systems"),
        ("Brightwater Systems Ltd.", "Brightwater Systems GmbH"),
        ("Orchard Lane Co", "Orchard Lane Corp"),
        ("Orchard (Lane Group) Foods", "Orchard Foods"),  # text inside the name
        ("Harbor Freight Company", "Harbor Freight LLP"),  # suffixes the org matcher lacks
        ("Willow Run Incorporated", "Willow Run BV"),
    ],
)
def test_a_company_covered_by_a_source_under_a_variant_name_is_not_suggested(company, source):
    _pursuit(company)
    assert _list_suggestions(names=[source]) == []


def test_an_uncovered_company_is_still_suggested_and_a_short_name_is_not_over_matched():
    _pursuit("Ember Hollow Inc")
    rows = _list_suggestions(names=["Quartz Ridge LLC"])
    assert [s["name"] for s in rows] == ["Ember Hollow Inc"]


def test_a_copy_leased_by_the_worker_between_the_read_and_the_write_is_left_alone(monkeypatch):
    """The foreground answer retires waiting copies of its check, but a copy the worker leased
    first keeps its lease (a second session does the leasing, so the race is real)."""
    from structlog.testing import capture_logs

    from kestrel.queue.queue import TaskQueue

    _pursuit("Nobody Corp")

    async def queue_one():
        async with get_sessionmaker()() as s:
            await ss.enqueue_check(s, "Nobody Corp")
            await s.commit()

    _run(queue_one())
    real = ss._pending_ids

    async def read_then_lease(s, name):
        ids = await real(s, name)
        leased = await TaskQueue(get_sessionmaker()).lease("other-worker", types=[ss.TASK_TYPE])
        assert leased is not None and leased.id in ids  # the worker wins the race
        return ids

    monkeypatch.setattr(ss, "_pending_ids", read_then_lease)
    with respx.mock(assert_all_called=False) as mock, capture_logs() as logs:
        mock.route().mock(return_value=httpx.Response(404, json={}))
        assert _run(ss.check_company("Nobody Corp")) == "none"
    assert [t.status for t in _tasks()] == ["leased"]  # not overwritten with done
    assert any(e["event"] == "suggestion_check_copy_already_leased" for e in logs)
