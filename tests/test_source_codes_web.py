"""The SRC code on the Sources page (#174 task E): the row, the drawer and Show details. Invented
sources only; every request is local."""

from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
from kestrel.db.models import ScrapeRun, Source
from kestrel.services import tracks
from kestrel.web import source_view as sv
from tests import clock_seed

RATE_TEXT = "The board answered 429 Too Many Requests"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from kestrel.web import routes_control

    async def _down(*a, **k):
        return {"up": False, "loaded": []}

    monkeypatch.setattr(routes_control, "_probe_ollama", _down, raising=False)
    tracks.forget_cache()
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()
    yield
    tracks.forget_cache()
    asyncio.run(reset_engine())


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _iso(**ago) -> str:
    return clock_seed.ago(**ago).isoformat()


def _src(**kw) -> Source:
    base = dict(id=1, track="employment", kind="greenhouse", name="Pinecrest Labs", config={},
                enabled=True, cadence_hours=24, consecutive_failures=0, origin="custom",
                last_run_at=_iso(hours=2))
    base.update(kw)
    return Source(**base)


def _run_row(status, error=None, counts=None) -> ScrapeRun:
    return ScrapeRun(source_id=1, status=status, error=error, counts=counts or {},
                     started_at=_iso(hours=1))


def _add(status, error, counts, **kw) -> int:
    row = dict(track="employment", kind="greenhouse", name="Pinecrest Labs",
               config={"token": "pine"}, enabled=True, origin="custom",
               last_run_at=_iso(hours=1))
    row.update(kw)

    async def _go():
        try:
            async with get_sessionmaker()() as s:
                src = Source(**row)
                s.add(src)
                await s.flush()
                s.add(ScrapeRun(source_id=src.id, status=status, error=error, counts=counts,
                                started_at=_iso(hours=1)))
                await s.commit()
                return src.id
        finally:
            await reset_engine()

    return asyncio.run(_go())


def _row_html(html: str, sid: int) -> str:
    return html.split(f'id="src-row-{sid}"')[1].split('data-testid="source-details"')[0]


def test_a_view_carries_the_code_stored_with_its_last_problem():
    v = sv.build_view(_src(), [_run_row("partial", RATE_TEXT, {"error_code": "SRC-002"})])
    assert (v.health, v.code) == ("w", "SRC-002")


def test_an_old_run_with_no_stored_code_shows_none():
    v = sv.build_view(_src(), [_run_row("partial", RATE_TEXT, {"fetched": 3})])
    assert (v.health, v.code) == ("w", "")


def test_an_unregistered_stored_code_shows_none():
    v = sv.build_view(_src(), [_run_row("partial", RATE_TEXT, {"error_code": "SRC-999"})])
    assert v.code == ""


def test_a_healthy_source_shows_no_code():
    v = sv.build_view(_src(), [_run_row("ok", None, {"fetched": 4, "new": 1})])
    assert (v.health, v.code) == ("g", "")


def test_a_paused_source_shows_no_code():
    v = sv.build_view(_src(enabled=False),
                      [_run_row("partial", RATE_TEXT, {"error_code": "SRC-002"})])
    assert (v.health, v.code) == ("m", "")


def test_a_search_with_no_boards_left_carries_src_013_even_when_off():
    from kestrel.ingest.adapters.jobspy import NO_BOARDS_KEY

    off = sv.build_view(_src(kind="jobspy", enabled=False, config={NO_BOARDS_KEY: True}), [])
    on = sv.build_view(_src(kind="jobspy", config={NO_BOARDS_KEY: True}), [])
    assert (off.code, on.code) == ("SRC-013", "SRC-013")


def test_the_row_shows_the_code_beside_the_sentence():
    sid = _add("partial", RATE_TEXT, {"fetched": 3, "error_code": "SRC-002"})
    with _client() as c:
        row = _row_html(c.get("/sources").text, sid)
    assert 'data-testid="source-code">Code SRC-002<' in row
    assert "Site asked us to slow down. Retrying later." in row  # the sentence stays whole


def test_the_row_of_an_old_run_shows_no_code():
    sid = _add("partial", RATE_TEXT, {"fetched": 3})
    with _client() as c:
        row = _row_html(c.get("/sources").text, sid)
    assert "source-code" not in row and "Code SRC" not in row


def test_the_drawer_shows_the_code_under_the_sentence_and_first_in_details():
    sid = _add("failed", "HTTP 404 Not Found", {"error_code": "SRC-008"}, consecutive_failures=2)
    with _client() as c:
        html = c.get(f"/sources/{sid}/drawer").text
    assert re.search(r'data-testid="drawer-code">Code SRC-008<', html)
    pre = html.split('<pre class="src-pre">')[1].split("</pre>")[0]
    assert pre.splitlines()[0] == "Code SRC-008"
    assert "could not find this page or board at its saved address" in html


def test_the_drawer_of_an_old_run_has_no_code_line():
    sid = _add("failed", "HTTP 404 Not Found", {}, consecutive_failures=2)
    with _client() as c:
        html = c.get(f"/sources/{sid}/drawer").text
    assert "drawer-code" not in html
    pre = html.split('<pre class="src-pre">')[1].split("</pre>")[0]
    assert "Code SRC" not in pre


def test_a_restored_backup_style_code_from_another_area_shows_no_code():
    for stored in ("CHK-001", "RUN-001", "STA-001"):
        v = sv.build_view(_src(), [_run_row("partial", RATE_TEXT, {"error_code": stored})])
        assert (v.health, v.code) == ("w", ""), stored


STORABLE = [f"SRC-{n:03d}" for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14)]
CONFLICTING_TEXT = "HTTP 429 Too Many Requests; 404 Not Found; moved; slow down"


@pytest.mark.parametrize("status", ["partial", "failed"])
@pytest.mark.parametrize("code", STORABLE)
def test_the_reason_comes_from_the_stored_code_whatever_the_text_says(code, status):
    v = sv.build_view(_src(), [_run_row(status, CONFLICTING_TEXT, {"error_code": code})])
    assert v.reason == sv.REASON_BY_CODE[code] and v.code == code
    assert v.raw == CONFLICTING_TEXT  # the raw status stays behind Show details


def test_a_daily_limit_on_a_failed_run_is_red_beside_its_own_words():
    """RemoteRocketship's quota: a failed run with SRC-007 reads "checks again tomorrow", not
    "tries again next check" beside a code that says tomorrow. It is red, as on Runs: a failed
    check counts toward the switch-off after three (D-ERR-24 round 5)."""
    v = sv.build_view(_src(consecutive_failures=2),
                      [_run_row("failed", "limit", {"error_code": "SRC-007"})])
    assert (v.health, v.code) == ("b", "SRC-007")
    assert "checks again tomorrow" in v.reason and "next check" not in v.reason


def test_a_remotive_cap_partial_run_agrees_too():
    v = sv.build_view(_src(), [_run_row("partial", "Remotive asks apps to fetch at most 4 times",
                                        {"error_code": "SRC-007"})])
    assert (v.health, v.code) == ("w", "SRC-007") and "checks again tomorrow" in v.reason


@pytest.mark.parametrize("code", ["SRC-002", "SRC-007", "SRC-003", "SRC-010"])
def test_a_failed_run_is_red_in_the_sparkline_whatever_its_code(code):
    assert sv.run_char(_run_row("failed", "x", {"error_code": code})) == "b"


@pytest.mark.parametrize("code", ["SRC-002", "SRC-007", "SRC-009"])
def test_a_partial_run_is_amber_in_the_sparkline(code):
    assert sv.run_char(_run_row("partial", "x", {"error_code": code})) == "w"


def test_a_failed_bar_is_red_whatever_its_words():
    """A failed SRC-010 whose stored text says "slow down" is red: the text is not read."""
    assert sv.run_char(_run_row("failed", "The site said slow down, HTTP 429",
                                {"error_code": "SRC-010"})) == "b"


@pytest.mark.parametrize(("status", "health"), [("failed", "b"), ("partial", "w")])
def test_a_row_with_no_code_reads_its_text_only_for_a_partial_check(status, health):
    """A row saved before codes: "slow down" in a failed check's text does not make it amber.
    The dot and the bar are red for a failed check and amber for a partial one, as on Runs."""
    row = _run_row(status, "The site said slow down, HTTP 429", {})
    assert sv.run_char(row) == health
    v = sv.build_view(_src(consecutive_failures=1), [row])
    assert v.health == health
    assert (v.reason == sv.SLOWED_REASON) == (status == "partial"), v.reason


@pytest.mark.parametrize(("status", "code", "repeated", "health"), [
    ("failed", "SRC-002", 0, "b"),
    ("failed", "SRC-002", 2, "b"),
    ("partial", "SRC-002", 0, "w"),
    ("partial", "SRC-002", 2, "w"),
    ("partial", "SRC-007", 2, "w"),
    ("partial", "SRC-003", 0, "w"),
    ("partial", "SRC-003", 2, "w"),  # earlier failures do not recolor a partial check
])
def test_the_source_row_tone_follows_status_and_code(status, code, repeated, health):
    v = sv.build_view(_src(consecutive_failures=repeated),
                      [_run_row(status, "The site said slow down", {"error_code": code})])
    assert (v.health, v.code, v.reason) == (health, code, sv.REASON_BY_CODE[code])


def test_a_row_with_no_code_keeps_the_older_reading_of_its_text():
    v = sv.build_view(_src(), [_run_row("partial", "HTTP 429 Too Many Requests", {})])
    assert (v.health, v.reason, v.code) == ("w", sv.SLOWED_REASON, "")
