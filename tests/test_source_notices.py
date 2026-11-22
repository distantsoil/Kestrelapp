"""Typed notices of a fetch that stopped early or left something out (#174 task E round 9,
D-ERR-24). Each adapter appends a Notice whose kind and sentence are chosen together; the run's
code and the health check's verdict come from the kinds only. Nothing here touches the network:
every reply is invented and served by a stand-in client."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from kestrel import doctor
from kestrel.doctor import PASS, WARN, DoctorReport
from kestrel.errors import ROBOTS_SENTENCE, RobotsDisallowed
from kestrel.ingest import failure_code as fc
from kestrel.ingest.adapters import get_adapter
from kestrel.ingest.base import (
    SHARED_KINDS,
    Notice,
    NoticeKind,
    PostingBatch,
    Unread,
    capped,
    rate_limited,
    robots_refused,
)

_SETTINGS = SimpleNamespace(
    ingest=SimpleNamespace(user_agent="Kestrel-test", request_timeout_s=1.0, respect_robots=False)
)


def _status(url: str, status: int):
    request = httpx.Request("GET", url)
    raise httpx.HTTPStatusError(
        str(status), request=request, response=httpx.Response(status, request=request)
    )


def _fail(how: str, url: str):
    """Raise the failure `how` names for `url`: robots, or an HTTP status."""
    if how == "robots":
        raise RobotsDisallowed()
    _status(url, int(how))


class _Client:
    """An HttpClient stand-in: `route(method, url)` returns the reply or raises."""

    route = staticmethod(lambda method, url: None)

    def __init__(self, *_a, **_k) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get_json(self, url, *_a, **_k):
        return self.route("GET", url)

    async def get_text(self, url, *_a, **_k):
        return self.route("GET", url)

    async def post_json(self, url, *_a, **_k):
        return self.route("POST", url)

    async def request(self, method, url, *_a, **_k):
        reply = self.route(method, url)
        status, text = reply if isinstance(reply, tuple) else (200, reply)
        return httpx.Response(status, text=text, request=httpx.Request(method, url))


def _client(route) -> _Client:
    return type("_Routed", (_Client,), {"route": staticmethod(route)})()


def _fetch(kind: str, config: dict, route) -> PostingBatch:
    return asyncio.run(get_adapter(kind).fetch(config, _client(route)))


def _notice(kind: str) -> Notice:
    """A notice of `kind`: a shared kind takes its shared sentence (the kind alone), any other
    kind an invented one."""
    k = NoticeKind(kind)
    return Notice(k) if k in SHARED_KINDS else Notice(k, "An invented sentence.")


def _kinds(batch) -> list[str]:
    return [n.kind.value for n in batch.notices]


def _code(batch) -> str:
    return fc.code_for_notices(batch.notices, fallback=fc.PARTIAL)


# ----------------------------------------------------------------------------- the model
def test_a_notice_kind_must_be_a_notice_kind_and_a_batch_takes_only_notices():
    with pytest.raises(TypeError):
        Notice("rate_limited", "x")
    with pytest.raises(TypeError):
        PostingBatch().add("a sentence")
    with pytest.raises(AttributeError):
        PostingBatch().partial_error = "a sentence"  # derived: read only


def test_notices_append_and_partial_error_keeps_every_sentence_in_order():
    batch = PostingBatch()
    assert batch.partial_error is None
    batch.add(robots_refused())
    batch.add(rate_limited("Example board", "example-co"))
    batch.add(capped("Example board", "example-co", 3, 1))
    assert _kinds(batch) == ["robots_refused", "rate_limited", "capped"]
    assert batch.partial_error == " ".join(n.sentence for n in batch.notices)
    assert batch.partial_error.startswith(ROBOTS_SENTENCE)


@pytest.mark.parametrize(
    ("kinds", "code"),
    [
        (["robots_refused"], "SRC-005"),
        (["rate_limited"], "SRC-002"),
        (["rate_limited", "rate_limited"], "SRC-002"),
        (["server_error"], "SRC-003"),
        (["needs_key"], "SRC-006"),
        (["daily_limit"], "SRC-007"),
        (["rate_limited", "capped"], "SRC-002"),  # the cap is not a cause
        (["capped"], "SRC-009"),
        (["body_unread"], "SRC-009"),
        (["feeds_off"], "SRC-009"),
        (["stopped"], "SRC-009"),
        (["robots_refused", "rate_limited"], "SRC-009"),  # two causes disagree: general
        (["robots_refused", "body_unread"], "SRC-009"),  # one names no proven cause: general
        ([], "SRC-009"),
    ],
)
def test_the_run_code_comes_from_the_kinds_and_uncertain_is_general(kinds, code):
    notices = [_notice(k) for k in kinds]
    assert fc.code_for_notices(notices, fallback=fc.PARTIAL) == code


def test_a_problem_keeps_its_kind_into_the_notice():
    from kestrel.ingest.base import rate_limited_problem

    assert Notice.of(rate_limited_problem("Invented board")) == rate_limited("Invented board")
    from kestrel.ingest.base import server_error, server_error_problem

    assert Notice.of(server_error_problem("Invented board", 503)) == server_error("Invented board")
    assert Notice.of(ROBOTS_SENTENCE) == robots_refused()
    assert Notice.of("Invented words about a 429 and robots.txt").kind is NoticeKind.STOPPED


def test_unread_takes_only_page_failure_kinds():
    unread = Unread()
    with pytest.raises(ValueError):
        unread.add(NoticeKind.CAPPED)
    assert not unread and unread.notices("Example board", "x") == []


# ----------------------------------------------------------------------------- Breezy
def _breezy(n: int, page):
    rows = [
        {
            "friendly_id": f"r{i}",
            "name": f"Analyst {i}",
            "location": {"name": "Lisbon"},
            "url": f"https://example-co.breezy.hr/p/r{i}",
        }
        for i in range(n)
    ]

    def route(_method, url):
        if url.endswith("/json"):
            return rows
        return page(int(url.rsplit("/r", 1)[1]), url)

    return route


_BODY = '<html><div class="description"><p>Invented role text.</p></div></html>'


@pytest.mark.parametrize(
    ("how", "kind", "code", "words"),
    [
        ("robots", "robots_refused", "SRC-005", ROBOTS_SENTENCE),
        ("429", "rate_limited", "SRC-002", "is rate limiting requests for example-co"),
        ("404", "body_unread", "SRC-009", "did not return the details of 1 of its roles"),
    ],
)
@pytest.mark.parametrize("adapter", ["breezy", "personio"])
def test_a_role_page_failure_is_one_notice_of_its_kind(adapter, how, kind, code, words):
    if adapter == "breezy":
        route = _breezy(1, lambda _i, url: _fail(how, url))
    else:
        route = _personio(1, lambda _i, url: _fail(how, url))
    batch = _fetch(adapter, {"slug": "example-co"}, route)
    assert len(batch) == 1 and not batch[0].description_html
    assert _kinds(batch) == [kind]
    assert words in batch.partial_error and _code(batch) == code


@pytest.mark.parametrize("adapter", ["breezy", "personio"])
def test_the_page_cap_is_a_capped_notice_only(adapter):
    make = _breezy if adapter == "breezy" else _personio
    route = make(3, lambda _i, _url: _BODY if adapter == "breezy" else _LD)
    batch = _fetch(adapter, {"slug": "example-co", "max_results": 1}, route)
    assert len(batch) == 3 and _kinds(batch) == ["capped"]
    assert "has 2 roles at example-co beyond the 1 posting pages" in batch.partial_error


@pytest.mark.parametrize("adapter", ["breezy", "personio"])
def test_robots_cap_and_unread_together_keep_all_three_notices(adapter):
    """Page 0 is refused by robots.txt, page 1 is missing, the rest are over the cap: three
    notices, every sentence kept, and the general code because the causes disagree."""
    make = _breezy if adapter == "breezy" else _personio
    route = make(4, lambda i, url: _fail("robots" if i == 0 else "404", url))
    batch = _fetch(adapter, {"slug": "example-co", "max_results": 2}, route)
    assert len(batch) == 4
    assert _kinds(batch) == ["robots_refused", "body_unread", "capped"]
    for notice in batch.notices:
        assert notice.sentence in batch.partial_error
    assert _code(batch) == "SRC-009"


@pytest.mark.parametrize("adapter", ["breezy", "personio"])
def test_a_list_429_is_rate_limiting(adapter):
    def route(_method, url):
        _status(url, 429)

    batch = _fetch(adapter, {"slug": "example-co"}, route)
    assert list(batch) == [] and _kinds(batch) == ["rate_limited"]
    assert _code(batch) == "SRC-002"


# ----------------------------------------------------------------------------- Personio
_LD = (
    '<html><script type="application/ld+json">{"@type": "JobPosting", "description": '
    '"<p>Invented role text.</p>"}</script></html>'
)


def _personio(n: int, page):
    positions = "".join(
        f"<position><id>{100 + i}</id><name>Analyst {i}</name><office>Lisbon</office></position>"
        for i in range(n)
    )
    feed = f'<?xml version="1.0"?><workzag-jobs>{positions}</workzag-jobs>'

    def route(_method, url):
        if url.endswith("/xml"):
            return feed
        return page(int(url.rsplit("/", 1)[1]) - 100, url)

    return route


# ----------------------------------------------------------------- SmartRecruiters, Workday
def _smartrecruiters(how):
    def route(_method, url):
        if "/postings?" in url:
            return {
                "content": [{"id": "1", "name": "Analyst"}, {"id": "2", "name": "Clerk"}],
                "totalFound": 2,
            }
        if url.endswith("/2"):
            _fail(how, url)
        return {"jobAd": {"sections": {}}}

    return route


def _workday(how):
    def route(method, url):
        if method == "POST":
            return {
                "total": 2,
                "jobPostings": [
                    {"title": "Analyst", "externalPath": "/job/Lisbon/Analyst_R1"},
                    {"title": "Clerk", "externalPath": "/job/Lisbon/Clerk_R2"},
                ],
            }
        if url.endswith("R2"):
            _fail(how, url)
        return {"jobPostingInfo": {"jobDescription": "<p>Invented role text.</p>"}}

    return route


@pytest.mark.parametrize(
    ("how", "kind", "code", "words"),
    [
        ("429", "rate_limited", "SRC-002", "is rate limiting requests for "),
        ("404", "body_unread", "SRC-009", "did not return the details of 1 of its roles"),
    ],
)
@pytest.mark.parametrize(
    ("adapter", "config", "route"),
    [
        ("smartrecruiters", {"company": "ExampleCo"}, _smartrecruiters),
        ("workday", {"tenant": "example", "dc": "wd5", "site": "External"}, _workday),
    ],
)
def test_a_detail_failure_is_said_in_its_own_kind(adapter, config, route, how, kind, code, words):
    """A detail 429 is rate limiting (SRC-002), never "did not return details"."""
    batch = _fetch(adapter, config, route(how))
    assert len(batch) == 1 and _kinds(batch) == [kind]
    assert words in batch.partial_error and _code(batch) == code
    assert ("did not return" in batch.partial_error) == (how != "429")


# ----------------------------------------------------------------------------- careers page
def _careers(how: str, links: int = 2):
    hrefs = "".join(f'<a href="/jobs/{i}/role-{i}">r{i}</a>' for i in range(links))

    def route(_method, url):
        if url.endswith("/jobs"):
            return f"<html><body>{hrefs}</body></html>"
        if url.endswith("/role-1"):
            _fail(how, url)
        return "<html>no data</html>"

    return route


@pytest.mark.parametrize(
    ("how", "kind", "code"),
    [
        ("robots", "robots_refused", "SRC-005"),
        ("429", "rate_limited", "SRC-002"),
        ("404", "body_unread", "SRC-009"),
    ],
)
def test_a_careers_page_link_that_failed_is_said_in_its_own_kind(how, kind, code):
    batch = _fetch("careers_page", {"url": "https://careers.example.org/jobs"}, _careers(how))
    assert _kinds(batch) == [kind] and _code(batch) == code
    assert (batch.partial_error == ROBOTS_SENTENCE) == (how == "robots")


def test_a_careers_page_list_429_is_rate_limiting():
    def route(_method, url):
        _status(url, 429)

    batch = _fetch("careers_page", {"url": "https://careers.example.org/jobs"}, route)
    assert _kinds(batch) == ["rate_limited"] and _code(batch) == "SRC-002"


# ------------------------------------------------------------------ Teamtailor, Recruitee
@pytest.mark.parametrize(
    ("how", "kind", "code"),
    [
        ("robots", "robots_refused", "SRC-005"),
        ("429", "rate_limited", "SRC-002"),
        ("404", "feeds_off", "SRC-009"),
    ],
)
def test_a_teamtailor_feed_says_why_in_its_own_kind(how, kind, code):
    def route(_method, url):
        _fail(how, url)

    batch = _fetch("teamtailor", {"domain": "careers.example.org"}, route)
    assert list(batch) == [] and _kinds(batch) == [kind] and _code(batch) == code
    assert (batch.partial_error == ROBOTS_SENTENCE) == (how == "robots")


def test_a_recruitee_429_is_rate_limiting():
    def route(_method, url):
        _status(url, 429)

    batch = _fetch("recruitee", {"slug": "example-co"}, route)
    assert _kinds(batch) == ["rate_limited"] and _code(batch) == "SRC-002"


# ----------------------------------------------------------------------------- health check
def _probe(monkeypatch, kind: str, config: dict, route) -> object:
    from kestrel.db import migrate
    from kestrel.ingest import http

    monkeypatch.setattr(migrate, "current_revision", lambda: "head")
    monkeypatch.setattr(
        doctor,
        "_enabled_ats_sources",
        lambda: [SimpleNamespace(name="Example Co", kind=kind, config=config)],
    )
    monkeypatch.setattr(
        http, "HttpClient", type("_Routed", (_Client,), {"route": staticmethod(route)})
    )
    rep = DoctorReport()
    doctor._check_ats_shapes(rep, _SETTINGS)
    (row,) = [r for r in rep.results if r.name.startswith("ATS ")]
    return row


def test_a_breezy_board_with_several_roles_and_changed_markup_is_a_shape_change(monkeypatch):
    """The probe caps itself to one page, so Breezy adds its cap notice; the page it read has no
    description where the adapter looks. The cap is ignored: CHK-020 with the shape hint."""
    route = _breezy(3, lambda _i, _url: "<html><main>Invented, laid out anew.</main></html>")
    row = _probe(monkeypatch, "breezy", {"slug": "example-co"}, route)
    assert (row.status, row.code) == (WARN, "CHK-020"), row.detail
    assert row.detail == "missing field(s): description"
    assert "shape may have changed" in row.remediation


def test_a_breezy_board_with_several_whole_roles_is_a_pass(monkeypatch):
    row = _probe(monkeypatch, "breezy", {"slug": "example-co"}, _breezy(3, lambda _i, _u: _BODY))
    assert (row.status, row.code) == (PASS, ""), row.detail


@pytest.mark.parametrize("how", ["robots", "429", "404"])
def test_a_probed_page_that_failed_beside_the_cap_is_could_not_read(monkeypatch, how):
    route = _breezy(3, lambda i, url: _fail(how, url) if i == 0 else _BODY)
    row = _probe(monkeypatch, "breezy", {"slug": "example-co"}, route)
    assert (row.status, row.code) == (WARN, "CHK-027"), row.detail
    assert "layout" not in row.remediation and "shape" not in row.remediation
    assert "max_results" not in row.detail  # the cap's own sentence is not the reason


def test_a_teamtailor_feed_turned_off_is_could_not_read(monkeypatch):
    def route(_method, url):
        _status(url, 404)

    row = _probe(monkeypatch, "teamtailor", {"domain": "careers.example.org"}, route)
    assert (row.status, row.code) == (WARN, "CHK-027"), row.detail
    assert "job feed turned off" in row.detail


# --------------------------------------------------------------- round 9 review findings
def test_a_notice_needs_a_sentence():
    with pytest.raises(ValueError):
        Notice(NoticeKind.STOPPED, " ")


@pytest.mark.parametrize("adapter", ["breezy", "personio"])
def test_after_a_429_the_roles_past_the_cap_are_still_the_cap(adapter):
    """max_results 2, five roles, the first page 429s: one more page was held back by the rate
    limit and the other three are past the cap, each said in its own kind."""
    make = _breezy if adapter == "breezy" else _personio
    route = make(5, lambda i, url: _fail("429", url) if i == 0 else _BODY)
    batch = _fetch(adapter, {"slug": "example-co", "max_results": 2}, route)
    assert _kinds(batch) == ["rate_limited", "capped"]
    assert "2 of its roles were added without a description" in batch.notices[0].sentence
    assert "has 3 roles at example-co beyond the 2 posting pages" in batch.notices[1].sentence
    assert _code(batch) == "SRC-002"


@pytest.mark.parametrize(
    ("status", "kind", "code"),
    [
        (429, "rate_limited", "SRC-002"),
        (404, "body_unread", "SRC-009"),
    ],
)
def test_a_careers_page_link_that_answered_a_status_is_said_in_its_own_kind(status, kind, code):
    route = _careers("404")

    def answered(method, url):
        return (status, "") if url.endswith("/role-1") else route(method, url)

    batch = _fetch("careers_page", {"url": "https://careers.example.org/jobs"}, answered)
    assert _kinds(batch) == [kind] and _code(batch) == code


def test_a_careers_page_list_that_answered_429_is_rate_limiting():
    batch = _fetch(
        "careers_page", {"url": "https://careers.example.org/jobs"}, lambda _m, _u: (429, "")
    )
    assert _kinds(batch) == ["rate_limited"] and _code(batch) == "SRC-002"


def test_a_remotive_429_is_rate_limited_and_a_403_has_no_code():
    from kestrel.ingest.adapters import remotive

    assert remotive._problem(429).kind is NoticeKind.RATE_LIMITED
    assert fc.code_for_partial(remotive._problem(429), fallback=fc.PARTIAL) == "SRC-002"
    assert fc.code_for_partial(remotive._problem(403), fallback=fc.PARTIAL) == fc.PARTIAL


@pytest.mark.parametrize(
    ("status", "kind", "code"),
    [
        (429, "rate_limited", "SRC-002"),
        (503, "server_error", "SRC-003"),
        (403, "stopped", "SRC-009"),
    ],
)
def test_a_jobvite_stop_is_said_in_its_own_kind(status, kind, code):
    batch = _fetch("jobvite", {"slug": "example-co"}, lambda _m, _u: (status, ""))
    assert list(batch) == [] and _kinds(batch) == [kind] and _code(batch) == code


def test_a_missing_title_beside_a_page_notice_is_still_a_shape_change(monkeypatch):
    """A page that was not read explains a missing description, never a missing title: the
    title comes from the list, so its absence is CHK-020."""

    def route(_method, url):
        if url.endswith("/json"):
            return [
                {
                    "friendly_id": "r0",
                    "location": {"name": "Lisbon"},
                    "url": "https://example-co.breezy.hr/p/r0",
                }
            ]
        _fail("404", url)

    row = _probe(monkeypatch, "breezy", {"slug": "example-co"}, route)
    assert (row.status, row.code) == (WARN, "CHK-020"), row.detail
    assert "title" in row.detail


# --------------------------------------------------------------------------- round 10
@pytest.mark.parametrize(
    ("said", "kind"),
    [
        (ROBOTS_SENTENCE, "robots_refused"),
        ("RATE", "rate_limited"),
        ("Invented item could not be read", "stopped"),
    ],
)
def test_a_later_hacker_news_item_that_failed_keeps_its_shared_words(monkeypatch, said, kind):
    """A later item fails with an AdapterError: a robots refusal or a 429 keeps its one shared
    sentence (never extra words), anything else says the rest is fetched next check."""
    from kestrel.ingest.adapters import hn_hiring
    from kestrel.ingest.base import AdapterError, RawPosting, rate_limited_problem

    said = rate_limited_problem("Hacker News") if said == "RATE" else said

    async def thread(_self, _config, _client):
        return {"kids": [1, 2]}

    async def item(_self, _client, kid):
        if kid == "2":
            raise AdapterError(said)
        return {"id": 1}

    monkeypatch.setattr(hn_hiring.HnHiringAdapter, "_thread", thread)
    monkeypatch.setattr(hn_hiring.HnHiringAdapter, "_item", item)
    monkeypatch.setattr(hn_hiring, "to_posting", lambda _i: RawPosting(external_id="1"))
    batch = asyncio.run(hn_hiring.HnHiringAdapter().fetch({}, None))
    assert len(batch) == 1 and _kinds(batch) == [kind]
    assert (batch.partial_error == str(said)) == (kind != "stopped")
