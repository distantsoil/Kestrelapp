"""Source checks carry a stable SRC code (#174 task E): chosen once by the runner from the cause,
stored on the run, never guessed for an old run. Invented sentences only; no live board."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response
from sqlalchemy import select

from kestrel import error_codes, paths
from kestrel.db.models import ScrapeRun, Source
from kestrel.errors import RobotsDisallowed
from kestrel.ingest import failure_code as fc
from kestrel.ingest.base import AdapterError, Problem, SourceConfigError
from kestrel.ingest.http import HttpClient
from kestrel.ingest.runner import run_source

_GH = json.loads((Path(__file__).parent / "fixtures" / "greenhouse.json").read_text())
_GH_URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"


async def _source(sm, **kw) -> int:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("profile.yml").write_text(
        "employment:\n  employment_terms:\n    target_titles: [VP of IT, Sales Director]\n",
        encoding="utf-8",
    )
    fields = {"track": "employment", "kind": "greenhouse", "name": "s", "enabled": True,
              "config": {"token": "acme"}}
    fields.update(kw)
    async with sm() as s:
        src = Source(**fields)
        s.add(src)
        await s.flush()
        sid = src.id
        await s.commit()
    return sid


async def _stored_counts(sm, sid) -> dict:
    async with sm() as s:
        run = (await s.execute(select(ScrapeRun).where(ScrapeRun.source_id == sid))).scalars().one()
        return dict(run.counts or {})


PROBES = [  # real adapter wording that held a pattern the old matcher searched for
    "Workday list for acme failed at offset 500: ReadTimeout",
    "Greenhouse board robotshop could not be read",
    "Lever slug removed-co not found",
    "The site said Too Many Requests.",
    "The board answered 429 for this search",
    "The page answered 404 at the saved address",
    "Hacker News took too long to answer",
]


@pytest.mark.parametrize("sentence", PROBES)
def test_words_alone_never_choose_a_specific_code(sentence):
    assert fc.code_for_partial(sentence, fallback=fc.PARTIAL) == fc.PARTIAL
    assert fc.code_for_exception(AdapterError(sentence)) == fc.UNREADABLE


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 429, 500, 503, None])
def test_a_status_alone_never_chooses_a_code(status):
    assert fc.code_for_partial(Problem("An invented sentence", status), fallback=fc.PARTIAL) == (
        fc.PARTIAL)


def test_the_code_comes_from_the_kind_the_adapter_chose_and_only_a_kind_is_accepted():
    from kestrel.ingest.base import NoticeKind, server_error_problem

    assert fc.code_for_partial(server_error_problem("Invented board", 503),
                               fallback=fc.PARTIAL) == "SRC-003"
    from kestrel.ingest.base import daily_limit_problem

    assert fc.code_for_partial(daily_limit_problem("Invented board", 429),
                               fallback=fc.PARTIAL) == "SRC-007"
    for general in (NoticeKind.BODY_UNREAD, NoticeKind.FEEDS_OFF, NoticeKind.CAPPED,
                    NoticeKind.STOPPED):
        assert fc.code_for_kind(general) is None, general
    assert fc.code_for_partial(Problem("x", kind=NoticeKind.STOPPED), fallback=fc.PARTIAL) == (
        fc.PARTIAL)
    for bad in ("SRC-003", "rate_limited", "CHK-001", "", 5):
        with pytest.raises(TypeError):
            Problem("x", kind=bad)


def test_a_sentence_equal_to_an_exported_constant_chooses_its_code():
    from kestrel.errors import ROBOTS_SENTENCE
    from kestrel.ingest.adapters import adzuna, usajobs

    for key in (adzuna.NEEDS_KEY, usajobs.NEEDS_KEY):
        assert fc.code_for_partial(key, fallback=fc.PARTIAL) == fc.NEEDS_KEY
    assert fc.code_for_partial(ROBOTS_SENTENCE, fallback=fc.PARTIAL) == fc.ROBOTS
    # the same words with anything added are only a sentence, not the constant
    assert fc.code_for_partial(ROBOTS_SENTENCE + " Extra.", fallback=fc.PARTIAL) == fc.PARTIAL
    assert fc.code_for_partial(adzuna.NEEDS_KEY + " for acme", fallback=fc.PARTIAL) == fc.PARTIAL


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://example.invalid/jobs")
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(status, request=req))


def _caused_by(cause: BaseException, message: str = "invented message") -> AdapterError:
    try:
        raise AdapterError(message) from cause
    except AdapterError as exc:
        return exc


def test_an_adapter_error_takes_its_code_from_its_typed_cause():
    assert fc.code_for_exception(_caused_by(_http_error(503))) == fc.SERVER_ERROR
    assert fc.code_for_exception(_caused_by(_http_error(429))) == fc.RATE_LIMITED
    assert fc.code_for_exception(_caused_by(_http_error(404))) == fc.UNREADABLE  # status alone
    assert fc.code_for_exception(_caused_by(_http_error(400))) == fc.UNREADABLE
    assert fc.code_for_exception(_caused_by(httpx.ReadTimeout("x"))) == fc.TIMED_OUT
    assert fc.code_for_exception(_caused_by(RobotsDisallowed())) == fc.ROBOTS
    assert fc.code_for_exception(_caused_by(SourceConfigError("x"))) == fc.SETUP_INVALID
    assert fc.code_for_exception(_caused_by(httpx.ConnectError("x"))) == fc.UNREADABLE


def test_a_cause_is_read_even_when_it_is_the_context_not_the_cause():
    try:
        try:
            raise httpx.ReadTimeout("x")
        except httpx.ReadTimeout:
            raise AdapterError("invented message")  # noqa: B904 - read through the context
    except AdapterError as exc:
        assert fc.code_for_exception(exc) == fc.TIMED_OUT


def test_the_words_of_a_wrapped_cause_never_choose_a_code():
    exc = _caused_by(RuntimeError("Too Many Requests 429 robots moved"), "offset 500 timed out")
    assert fc.code_for_exception(exc) == fc.UNREADABLE


def test_a_bug_raised_inside_a_timeout_handler_is_unexpected_not_a_timeout():
    try:
        try:
            raise httpx.ReadTimeout("x")
        except httpx.TimeoutException:
            raise KeyError("a bug")  # noqa: B904 - the implicit context is the point
    except KeyError as exc:
        assert fc.code_for_exception(exc) == fc.UNEXPECTED


def test_a_suppressed_context_is_not_read():
    try:
        try:
            raise _http_error(503)
        except httpx.HTTPStatusError:
            raise AdapterError("invented message") from None
    except AdapterError as exc:
        assert fc.code_for_exception(exc) == fc.UNREADABLE


def test_a_non_adapter_error_with_an_explicit_cause_still_reads_the_cause():
    try:
        raise RuntimeError("x") from httpx.ReadTimeout("t")
    except RuntimeError as exc:
        assert fc.code_for_exception(exc) == fc.TIMED_OUT


def test_an_adapter_error_carrying_a_typed_sentence_takes_its_code():
    from kestrel.ingest.base import server_error_problem

    assert fc.code_for_exception(AdapterError(Problem("invented", 503))) == fc.UNREADABLE
    assert fc.code_for_exception(AdapterError(server_error_problem(
        "Invented board", 503))) == fc.SERVER_ERROR
    assert fc.code_for_exception(AdapterError("not found: x", code="SRC-008")) == fc.NOT_FOUND
    assert fc.code_for_exception(AdapterError("x", code="CHK-001")) == fc.UNREADABLE


def test_exceptions_map_by_type_not_by_text():
    assert fc.code_for_exception(SourceConfigError("bad")) == fc.SETUP_INVALID
    assert fc.code_for_exception(RobotsDisallowed()) == fc.ROBOTS
    assert fc.code_for_exception(RuntimeError("boom 429")) == fc.UNEXPECTED
    assert fc.code_for_exception(TimeoutError("x")) == fc.TIMED_OUT


def _adapter_modules():
    import importlib
    import pkgutil

    from kestrel.ingest import adapters

    return [importlib.import_module(f"{adapters.__name__}.{m.name}")
            for m in pkgutil.iter_modules(adapters.__path__)]


def _call_problem(mod, status: int):
    import inspect

    params = inspect.signature(mod._problem).parameters
    return mod._problem(status, *([{}] * (len(params) - 1)))


PROBLEM_MODULES = [m for m in _adapter_modules() if hasattr(m, "_problem")]


_BOARD = {"adzuna": "Adzuna", "arbeitnow": "Arbeitnow", "fourdayweek": "4 Day Week",
          "himalayas": "Himalayas", "hn_hiring": "Hacker News", "jobicy": "Jobicy",
          "remotive": "Remotive", "usajobs": "USAJobs", "weworkremotely": "We Work Remotely"}
# RemoteRocketship answers a 429 when the daily quota of the owner's subscription is spent: its
# own daily-limit sentence and SRC-007 (D-ERR-24 round 3), not a rate limit.
_DAILY_QUOTA_429 = {"remoterocketship"}
# A 403 on a keyed board is the key being turned down (SRC-006), its own proven sentence.
_KEYED_403 = {"adzuna", "usajobs", "remoterocketship"}


def _name(mod) -> str:
    return mod.__name__.rsplit(".", 1)[1]


def test_the_429_and_403_walk_covers_every_problem_adapter():
    assert {_name(m) for m in PROBLEM_MODULES} == set(_BOARD) | _DAILY_QUOTA_429


@pytest.mark.parametrize("mod", [m for m in PROBLEM_MODULES if _name(m) not in _DAILY_QUOTA_429],
                         ids=_name)
def test_every_adapter_words_a_429_with_the_one_shared_sentence(mod):
    from kestrel.ingest.base import NoticeKind, rate_limited_problem

    said = _call_problem(mod, 429)
    assert said == rate_limited_problem(_BOARD[_name(mod)]) and said.kind is NoticeKind.RATE_LIMITED
    assert fc.code_for_partial(said, fallback=fc.PARTIAL) == fc.RATE_LIMITED
    assert fc.code_for_exception(AdapterError(said)) == fc.RATE_LIMITED


@pytest.mark.parametrize("mod", [m for m in PROBLEM_MODULES if _name(m) not in _KEYED_403],
                         ids=_name)
def test_every_adapter_words_a_403_with_the_one_hedged_sentence_and_no_code(mod):
    from kestrel.ingest.base import hedged_refusal

    said = _call_problem(mod, 403)
    assert said == hedged_refusal(_BOARD[_name(mod)]) and said.kind is None
    assert fc.code_for_partial(said, fallback=fc.PARTIAL) == fc.PARTIAL
    assert fc.code_for_exception(AdapterError(said)) == fc.UNREADABLE


_BOARD_5XX = {**_BOARD, "remoterocketship": "RemoteRocketship"}


@pytest.mark.parametrize("mod", PROBLEM_MODULES, ids=_name)
@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_every_adapter_words_a_5xx_with_the_one_shared_sentence(mod, status):
    from kestrel.ingest.base import NoticeKind, server_error_problem

    said = _call_problem(mod, status)
    assert said == server_error_problem(_BOARD_5XX[_name(mod)], status)
    assert said.kind is NoticeKind.SERVER_ERROR


@pytest.mark.parametrize(("module", "board", "where"), [
    ("adp", "ADP", "cid-1"), ("paylocity", "Paylocity", "board-1"),
    ("rippling", "Rippling", "slug-1"), ("workable", "Workable", "slug-1")])
def test_a_detail_or_list_429_that_stops_the_fetch_uses_the_shared_words(module, board, where):
    import importlib

    from kestrel.ingest.base import rate_limited_problem

    mod = importlib.import_module(f"kestrel.ingest.adapters.{module}")
    err = mod._failure("detail", where, _http_error(429))
    assert err.args[0] == rate_limited_problem(board, 429, where=where)
    assert fc.code_for_exception(err) == fc.RATE_LIMITED
    other = mod._failure("detail", where, _http_error(404))
    assert fc.code_for_exception(other) == fc.UNREADABLE


def test_a_careers_page_403_is_the_hedged_sentence_and_other_statuses_are_said():
    from kestrel.ingest.adapters import careers_page as cp
    from kestrel.ingest.base import hedged_refusal

    assert cp._answered(403) == hedged_refusal("The careers page") == cp.PAGE_LIMITED
    assert cp.PAGE_LIMITED in cp.PAGE_REFUSALS  # the Sources link check can show it
    assert cp._answered(500) == "The careers page answered 500."


def test_a_shared_kind_cannot_be_given_free_words():
    from kestrel.ingest.base import SHARED_KINDS, Notice, NoticeKind, PostingBatch

    assert SHARED_KINDS == set(NoticeKind) - {NoticeKind.STOPPED}  # every known kind
    for kind in sorted(SHARED_KINDS):
        with pytest.raises(ValueError):
            Problem("Invented board asked to slow down", 429, kind=kind)
        with pytest.raises(ValueError):
            Notice(kind, "Invented words of its own.")
        with pytest.raises(ValueError):
            PostingBatch().note(kind, "Invented words of its own.")
        assert Notice(kind).sentence  # the kind alone takes the shared default sentence
    assert Notice(NoticeKind.STOPPED, "Invented words of its own.").sentence  # general: free
    with pytest.raises(ValueError):
        Notice(NoticeKind.STOPPED)  # the general kind has no default: it needs its words
        with pytest.raises(ValueError):
            Notice(kind, "")  # empty words are refused, never swapped for the default


def test_the_shared_guard_has_no_back_door():
    import dataclasses

    from kestrel.ingest.base import Notice, NoticeKind, rate_limited, rate_limited_problem

    with pytest.raises(ValueError):
        dataclasses.replace(rate_limited("Invented board"), sentence="Invented words.")
    plain = Problem("Invented words of its own.", 403)
    with pytest.raises(AttributeError):
        plain.kind = NoticeKind.RATE_LIMITED
    assert Notice.of(plain).kind is NoticeKind.STOPPED
    with pytest.raises(TypeError):
        Problem("x", 429, NoticeKind.RATE_LIMITED, object())  # the token is keyword-only
    assert Notice.of(rate_limited_problem("Invented board")) == rate_limited("Invented board")


def test_the_adapters_that_report_a_5xx_are_the_ones_the_test_covers():
    """A new adapter with its own `_problem` must be added here, so it is covered below."""
    names = {m.__name__.rsplit(".", 1)[1] for m in PROBLEM_MODULES}
    assert names == {"adzuna", "arbeitnow", "fourdayweek", "himalayas", "hn_hiring", "jobicy",
                     "remoterocketship", "remotive", "usajobs", "weworkremotely"}


_AGREES = {  # a specific code is allowed only beside a sentence that says the same thing
    fc.RATE_LIMITED: ("is rate limiting requests",),  # the shared builder's own words
    fc.SERVER_ERROR: ("on their side",),
    fc.DAILY_LIMIT: ("daily limit",),
    fc.NEEDS_KEY: ("API key", "app ID and app key", "key and email"),
    fc.NOT_FOUND: ("not found", "moved"),
}
_STATUSES = [400, 401, 403, 404, 410, 413, 429, 500, 502, 503, 504]


@pytest.mark.parametrize("mod", PROBLEM_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[1])
@pytest.mark.parametrize("status", _STATUSES)
def test_an_adapters_code_agrees_with_its_sentence_for_every_status(mod, status):
    sentence = _call_problem(mod, status)
    code = fc.code_for_partial(sentence, fallback=fc.PARTIAL)
    allowed = _AGREES.get(code)
    assert code == fc.PARTIAL or any(w in sentence for w in allowed), (mod.__name__, status,
                                                                      code, str(sentence))
    assert code != fc.NOT_FOUND or status in (404, 410) and "moved" in sentence  # never a search


@pytest.mark.parametrize("mod", PROBLEM_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[1])
@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_5xx_from_every_adapter_reaches_src_003(mod, status):
    sentence = _call_problem(mod, status)
    assert fc.code_for_partial(sentence, fallback=fc.PARTIAL) == fc.SERVER_ERROR
    assert fc.code_for_exception(AdapterError(sentence)) == fc.SERVER_ERROR


@pytest.mark.parametrize("mod", PROBLEM_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[1])
@pytest.mark.parametrize("status", [404, 410])
def test_a_404_never_says_the_page_was_not_found_for_a_search_board(mod, status):
    """Search boards word a 404 as a rejected search or a moved feed. Those sentences get the
    general code, because "not found" would disagree with them."""
    assert fc.code_for_partial(_call_problem(mod, status), fallback=fc.PARTIAL) == fc.PARTIAL


def test_the_remoterocketship_daily_quota_is_src_007_not_a_slow_down():
    from kestrel.ingest.adapters import remoterocketship

    sentence = remoterocketship._problem(429, {})
    assert "daily limit" in sentence
    assert fc.code_for_partial(sentence, fallback=fc.PARTIAL) == fc.DAILY_LIMIT


@pytest.mark.parametrize("name", ["adzuna", "usajobs"])
@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_is_src_006(name, status):
    import importlib

    mod = importlib.import_module(f"kestrel.ingest.adapters.{name}")
    assert fc.code_for_partial(mod._problem(status), fallback=fc.PARTIAL) == fc.NEEDS_KEY


@pytest.mark.parametrize("mod", PROBLEM_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[1])
def test_a_403_is_never_called_a_rate_limit_by_its_code(mod):
    """A 403 can be a bot or region block as well as a rate limit, so no board gives it a code,
    whatever its (hedged) sentence says. Only a 429 is a proven rate limit."""
    code = fc.code_for_partial(_call_problem(mod, 403), fallback=fc.PARTIAL)
    assert code in (fc.PARTIAL, fc.NEEDS_KEY)  # a key board's 403 is its own, proven, key refusal
    assert fc.code_for_partial(_call_problem(mod, 429), fallback=fc.PARTIAL) in (
        fc.RATE_LIMITED, fc.DAILY_LIMIT, fc.PARTIAL)


@pytest.mark.parametrize("counts", [["SRC-002"], "SRC-002", 5, ["error_code", "CHK-001"],
                                    [{"error_code": "SRC-002"}], True])
def test_counts_that_are_not_a_dict_are_no_code_and_never_raise(counts):
    assert fc.stored_code(counts) is None
    assert fc.stored_code({}) is None
    assert fc.stored_code(None) is None


async def test_a_setup_problem_stores_src_001(sm):
    sid = await _source(sm, config={})
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        outcome = await run_source(sm, sid, http=http)
    assert outcome.status == "partial"
    assert (await _stored_counts(sm, sid))["error_code"] == "SRC-001"


@respx.mock
@pytest.mark.parametrize(("status", "code"), [(429, "SRC-002"), (503, "SRC-003")])
async def test_a_429_or_5xx_stays_partial_and_stores_its_code(sm, status, code):
    respx.get(_GH_URL).mock(return_value=Response(status))
    sid = await _source(sm)
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        outcome = await run_source(sm, sid, http=http)
    assert outcome.status == "partial"  # a 429 is never "failed" (hard rule)
    assert (await _stored_counts(sm, sid))["error_code"] == code


@respx.mock
async def test_a_clean_run_stores_no_code(sm):
    respx.get(_GH_URL).mock(return_value=Response(200, json=_GH))
    sid = await _source(sm)
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        outcome = await run_source(sm, sid, http=http)
    assert outcome.status == "ok"
    assert "error_code" not in await _stored_counts(sm, sid)
    assert "error_code" not in outcome.counts


@respx.mock
async def test_an_unexpected_error_stores_src_012(sm, monkeypatch):
    import kestrel.ingest.runner as runner_mod

    class _Boom:
        discovery_only = False

        def validate_config(self, config):
            raise RuntimeError("invented failure")

    monkeypatch.setattr(runner_mod, "get_adapter", lambda kind: _Boom())
    sid = await _source(sm)
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        outcome = await run_source(sm, sid, http=http)
    assert outcome.status == "partial"
    assert (await _stored_counts(sm, sid))["error_code"] == "SRC-012"


class _FakeAdapter:
    """An adapter whose fetch does what the test says; no network."""

    discovery_only = False

    def __init__(self, fetch):
        self._fetch = fetch

    def validate_config(self, config):
        return None

    async def fetch(self, config, http):
        return await self._fetch()


async def _run_fake(sm, monkeypatch, fetch):
    import kestrel.ingest.runner as runner_mod

    monkeypatch.setattr(runner_mod, "get_adapter", lambda kind: _FakeAdapter(fetch))
    sid = await _source(sm)
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        outcome = await run_source(sm, sid, http=http)
    return outcome, await _stored_counts(sm, sid)


async def test_a_typed_5xx_partial_stops_early_with_src_003(sm, monkeypatch):
    from kestrel.ingest.base import Notice, PostingBatch, server_error_problem

    async def fetch():
        batch = PostingBatch()
        batch.add(Notice.of(server_error_problem("Invented board", 503)))
        return batch

    outcome, counts = await _run_fake(sm, monkeypatch, fetch)
    assert outcome.status == "partial" and counts["error_code"] == "SRC-003"


async def test_a_plain_partial_sentence_with_a_status_word_gets_the_general_code(sm, monkeypatch):
    from kestrel.ingest.base import NoticeKind, PostingBatch

    async def fetch():
        batch = PostingBatch()
        batch.note(NoticeKind.STOPPED, "Workday list failed at offset 500: ReadTimeout (HTTP 429)")
        return batch

    outcome, counts = await _run_fake(sm, monkeypatch, fetch)
    assert outcome.status == "partial" and counts["error_code"] == "SRC-009"


async def test_an_adapter_error_naming_a_slug_gets_the_general_code(sm, monkeypatch):
    async def fetch():
        raise AdapterError("Greenhouse board robotshop could not be read")

    outcome, counts = await _run_fake(sm, monkeypatch, fetch)
    assert outcome.status == "partial" and counts["error_code"] == "SRC-010"


async def test_a_run_row_that_is_gone_at_the_end_is_a_plain_error_not_an_attribute_error(
        sm, monkeypatch):
    """The runner reads its own run row back to close it. If that read finds nothing the run
    says so in words; it never fails on `None.status`."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from kestrel.ingest.base import PostingBatch

    real_get = AsyncSession.get

    async def get(self, entity, ident, *args, **kwargs):
        if entity is ScrapeRun:
            return None
        return await real_get(self, entity, ident, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", get)

    async def fetch():
        return PostingBatch()

    with pytest.raises(ValueError, match="scrape run .* not found"):
        await _run_fake(sm, monkeypatch, fetch)


def test_the_code_is_not_shown_twice_in_a_run_page_counts_dump():
    from kestrel.web.deps import templates

    shown = templates().env.filters["run_counts"]({"new": 3, "error_code": "SRC-002"})
    assert shown == {"new": 3}


@pytest.mark.parametrize(("status", "code"), [
    (403, "SRC-009"), (429, "SRC-002"), (500, "SRC-003"), (404, "SRC-009")])
@respx.mock
async def test_hacker_news_codes_a_later_item_error_by_its_own_sentence(status, code):
    """The partial run keeps what was read. A later item that answers 403/429 is a slow down, a
    5xx a problem on their side, and a 404 only the general code (its sentence says neither)."""
    import json as _json

    from kestrel.ingest.adapters.hn_hiring import HnHiringAdapter
    from tests.test_adapter_hn_hiring import BASE, FIX, ITEMS, _http

    respx.get(f"{BASE}/user/whoishiring.json").mock(return_value=Response(200, json=FIX["user"]))
    seen = {"n": 0}

    def reply(req):
        key = req.url.path.rsplit("/", 1)[1].removesuffix(".json")
        if key in ("900101", "900102") and seen["n"] >= 1:
            return Response(status, json={})
        if key in ("900101", "900102"):
            seen["n"] += 1
        return Response(200, text=_json.dumps(ITEMS.get(key)))

    respx.get(url__regex=rf"{BASE}/item/(\d+)\.json").mock(side_effect=reply)
    async with _http() as http:
        got = await HnHiringAdapter().fetch({}, http)
    assert [p.external_id for p in got] == ["900101"]
    assert fc.code_for_notices(got.notices, fallback=fc.PARTIAL) == code


async def _run_with_postings(sm, monkeypatch, fails_after: int | None):
    """A run of two invented postings whose store step fails for every posting
    (`fails_after=None`) or only for the second one (`fails_after=1`)."""
    import kestrel.ingest.runner as runner_mod
    from kestrel.ingest.base import PostingBatch, RawPosting

    real = runner_mod.normalize_and_store
    calls = {"n": 0}

    async def store(session, raw, **kw):
        calls["n"] += 1
        if fails_after is None or calls["n"] > fails_after:
            raise RuntimeError("invented store failure")
        return await real(session, raw, **kw)

    monkeypatch.setattr(runner_mod, "normalize_and_store", store)

    async def fetch():
        return PostingBatch([
            RawPosting(external_id=f"p{i}", url=f"https://example.invalid/{i}",
                       title="VP of IT", org_name="Invented Org", description_md="A role.")
            for i in (1, 2)])

    return await _run_fake(sm, monkeypatch, fetch)


async def test_when_every_posting_fails_to_save_the_code_does_not_say_others_were_saved(
        sm, monkeypatch):
    outcome, counts = await _run_with_postings(sm, monkeypatch, fails_after=None)
    assert outcome.counts["errors"] == 2 and outcome.counts["new"] == 0
    assert counts["error_code"] == "SRC-014"
    assert "other postings were saved" not in error_codes.get("SRC-014").fix


async def test_when_some_postings_save_the_code_may_say_the_others_were_saved(sm, monkeypatch):
    outcome, counts = await _run_with_postings(sm, monkeypatch, fails_after=1)
    assert outcome.counts["errors"] == 1 and outcome.counts["new"] == 1
    assert counts["error_code"] == "SRC-011"
    assert "other postings were saved" in error_codes.get("SRC-011").fix


# One decision: the words on a page come from the stored code, and each set of words has to say
# the same thing as the code's own meaning. These are the words each code must keep.
SAME_THING = {
    "SRC-001": ("set up", "setup"), "SRC-002": ("slow down", "rate-limited", "too many"),
    "SRC-003": ("server",), "SRC-004": ("too long", "timed out", "in time"), "SRC-005": ("rules",),
    "SRC-006": ("key",), "SRC-007": ("day", "daily"), "SRC-008": ("find", "found", "moved"),
    "SRC-009": ("finish", "stopped"), "SRC-010": ("read", "did not work"), "SRC-011": ("posting",),
    "SRC-012": ("unexpected",), "SRC-014": ("posting",),
}


def test_every_code_the_runner_can_store_has_words_on_both_pages_that_agree_with_it():
    from kestrel.analytics.run_summary import WORDS_BY_CODE
    from kestrel.web.source_view import REASON_BY_CODE

    for code, needles in SAME_THING.items():
        meaning = error_codes.get(code).meaning.lower()
        assert any(n in meaning for n in needles), (code, "meaning", meaning)
        reason = REASON_BY_CODE[code].lower()
        words = WORDS_BY_CODE[code].lower()
        assert any(n in reason for n in needles), (code, "Sources reason", reason)
        assert any(n in words for n in needles), (code, "Runs words", words)
    assert set(WORDS_BY_CODE) == set(REASON_BY_CODE)


@pytest.mark.parametrize("code", sorted(SAME_THING))
def test_runs_words_follow_the_stored_code_not_the_text_of_the_error(code):
    from kestrel.analytics.run_summary import WORDS_BY_CODE, problem_words

    # an error text that points the other way ("429" / "server error") must not win
    assert problem_words("HTTP 429 Too Many Requests 503 server error", "partial", code) == (
        WORDS_BY_CODE[code])


def test_the_adapter_errors_that_carry_not_found_say_not_found():
    """Each `AdapterError(..., code=NOT_FOUND)` is raised where its own sentence says the board
    was not found or moved, so the code and the sentence are one decision."""
    import ast
    import pkgutil

    from kestrel.ingest import adapters

    sites = []
    for mod in pkgutil.iter_modules(adapters.__path__):
        path = Path(adapters.__path__[0]) / f"{mod.name}.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "AdapterError":
                coded = any(k.arg == "code" and "NOT_FOUND" in ast.unparse(k.value)
                            for k in node.keywords)
                sites.append((mod.name, ast.unparse(node.args[0]) if node.args else "", coded))
    coded_sites = [(m, text) for m, text, coded in sites if coded]
    assert {m for m, _ in coded_sites} == {
        "breezy", "hn_hiring", "jobvite", "lever", "personio", "recruitee"}
    assert all("not found" in text or "moved" in text for _m, text in coded_sites), coded_sites
    # and the reverse: a sentence that says "not found" never goes without its code
    uncoded = [(m, t) for m, t, coded in sites if not coded and "not found" in t]
    assert uncoded == []


def test_key_daily_and_feed_sentences_are_the_shared_builders_own():
    from kestrel.ingest.adapters import adzuna, remoterocketship, teamtailor, usajobs
    from kestrel.ingest.base import (
        NoticeKind,
        daily_limit_problem,
        feeds_off,
        key_rejected_problem,
        needs_key,
    )

    for mod, board, fields in ((adzuna, "Adzuna", "app ID and app key"),
                               (usajobs, "USAJobs", "API key and email")):
        assert mod.NEEDS_KEY == needs_key(board, fields).sentence
        assert mod._problem(401) == key_rejected_problem(board, fields, 401)
        assert mod._problem(401).kind is NoticeKind.NEEDS_KEY
        assert mod.KEY_PROBLEM_PHRASE in mod.NEEDS_KEY  # the Sources card's "Needs you" phrase
    assert remoterocketship._problem(429, None) == daily_limit_problem("RemoteRocketship", 429)
    assert teamtailor.feed_off_message("jobs.example.org") == feeds_off("jobs.example.org").sentence
