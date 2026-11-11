"""A key holder never releases the out-of-credit hold on an online key (D-SEC-10, D-LLM-8).

Every route that releases the hold when the operator asks to score or run now (Score again, Try
again, Score anyway, Try all again, a story or research with Score again, Score everything, Run
now) releases it only for a request from this computer. The hold is in memory; no network."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kestrel.llm import credit
from kestrel.web import auth

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

KEY = "invented-key-for-the-hold-test"
ROUTES = (
    "/rescore", "/quarantine/1/retry", "/quarantine/1/anyway", "/quarantine/bulk",
    "/opportunities/1/role/story", "/opportunities/1/role/research",
    "/sources/score-everything", "/runs/start",
)


def _client(peer: str) -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False), base_url="http://127.0.0.1",
                      raise_server_exceptions=False, client=(peer, 12345))


@pytest.fixture
def held():
    credit.clear()
    credit.mark_out_of_credit("frontier", "openrouter", "invented-api-key")
    yield
    credit.clear()


@pytest.mark.parametrize("path", ROUTES)
def test_a_key_holder_posting_it_keeps_the_hold(path, held):
    auth.set_cached_key(KEY)
    with _client("192.168.1.50") as c:
        c.post(path, data={"opp_ids": "1", "model": "same", "rescore": "1"},
               headers={"X-Kestrel-Key": KEY})
    assert credit.any_held(), f"{path} released the hold for a key holder"


def test_this_computer_releases_it(held):
    with _client("127.0.0.1") as c:
        c.post("/rescore", data={"opp_ids": "1", "model": "same"})
    assert not credit.any_held()
