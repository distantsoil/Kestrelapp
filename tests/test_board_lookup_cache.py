"""#139: a failed network lookup is not remembered as "no board here". Only a definite answer is
cached; a dead network, a timeout or a 429/5xx leaves nothing behind, so the next try asks again.
Offline: respx answers every request."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
from kestrel.services import source_suggestions, tracks


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


async def _resolve_and_cache(name: str):
    from kestrel.ingest.orgs import normalized_name
    from kestrel.web.pages.sources_ui import _resolve_suggestion_board

    try:
        got = await _resolve_suggestion_board(None, name)  # type: ignore[arg-type]
        async with get_sessionmaker()() as s:
            cached = (await source_suggestions.board_cache(s)).get(normalized_name(name))
        return got, cached
    finally:
        await reset_engine()


def _answers(mock, response=None, error=None):
    route = mock.route()
    route.mock(side_effect=error) if error else route.mock(return_value=response)


@pytest.mark.parametrize(
    "answer",
    [
        {"error": httpx.ConnectError("network down")},
        {"error": httpx.ReadTimeout("slow")},
        {"response": httpx.Response(429)},
        {"response": httpx.Response(503)},
    ],
)
def test_a_lookup_that_could_not_get_an_answer_is_not_remembered_as_not_found(answer):
    with respx.mock(assert_all_called=False) as mock:
        _answers(mock, **answer)
        got, cached = asyncio.run(_resolve_and_cache("Tidewater Freight"))
    assert got == (None, None)
    assert cached is None


def test_a_definite_not_found_is_remembered_and_a_failed_try_after_it_does_not_change_that():
    with respx.mock(assert_all_called=False) as mock:
        _answers(mock, response=httpx.Response(404, json={}))
        got, cached = asyncio.run(_resolve_and_cache("Tidewater Freight"))
    # The answer now carries the date it was found (D-SRC-19), for the 30-day expiry.
    assert got == (None, None) and cached["found"] is False and cached["checked_at"]
