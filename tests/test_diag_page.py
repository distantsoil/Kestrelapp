"""The page-change recorder (D-UI-204): one log line per desktop page change, numbers and two
in-app paths only, clamped, and a bad body is refused without writing anything."""

from __future__ import annotations

from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from kestrel.web.app import build_app


def test_a_page_change_is_logged_with_clamped_numbers():
    with TestClient(build_app(worker=False)) as c, capture_logs() as logs:
        r = c.post("/api/diag/page", json={
            "to": "/opportunities", "came_from": "/", "vt": True,
            "ttfb_ms": 12.6, "reveal_ms": 99999999, "hidden": False,
        })
    assert r.status_code == 200
    line = next(x for x in logs if x["event"] == "page_change")
    assert line["to"] == "/opportunities" and line["came_from"] == "/" and line["vt"] is True
    assert line["ttfb_ms"] == 13 and line["reveal_ms"] == 600000


def test_an_overlong_path_is_refused():
    with TestClient(build_app(worker=False)) as c, capture_logs() as logs:
        r = c.post("/api/diag/page", json={"to": "/" + "x" * 500})
    assert r.status_code == 422
    assert not any(x["event"] == "page_change" for x in logs)


def test_an_in_place_page_change_logs_its_fetch_and_swap_times():
    """D-UI-210: an in-place change has no navigation timing; it says so and gives its own."""
    with TestClient(build_app(worker=False)) as c, capture_logs() as logs:
        r = c.post("/api/diag/page", json={
            "to": "/profile", "came_from": "/", "vt": True, "inplace": True,
            "fetch_ms": 41.4, "swap_ms": -3,
        })
    assert r.status_code == 200
    line = next(x for x in logs if x["event"] == "page_change")
    assert line["inplace"] is True and line["fetch_ms"] == 41 and line["swap_ms"] == 0
