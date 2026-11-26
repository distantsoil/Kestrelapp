"""Issue #182 scope B on Runs: a run's page shows, for each search in the run, the share of new
roles that came without a description, per board, with its sample size; under five roles it says
"too few to tell" (hard rule 6), and a jump carries the same plain warning the Sources page shows.
The stored counts are rebuilt through our own schema, so a malformed entry shows nothing.
Invented fixture runs only; no network."""

from __future__ import annotations

import asyncio
import html

import pytest
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
from kestrel.db.models import Run, ScrapeRun, Source

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

START = "2026-01-05T10:00:00+00:00"
END = "2026-01-05T11:00:00+00:00"


def _seed(bodies_by_source: dict[str, object]) -> int:
    async def go():
        await reset_engine()
        for suffix in ("", "-wal", "-shm"):
            paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
        migrate.run_migrations()
        forget_engine()
        async with get_sessionmaker()() as s:
            run = Run(name="Scheduled run", started_at=START, finished_at=END, status="done",
                      counts={})
            s.add(run)
            for name, bodies in bodies_by_source.items():
                src = Source(track="employment", kind="jobspy", name=name, enabled=True)
                s.add(src)
                await s.flush()
                counts = {"new": 20}
                if bodies is not None:
                    counts["bodies"] = bodies
                s.add(ScrapeRun(source_id=src.id, started_at="2026-01-05T10:05:00+00:00",
                                finished_at="2026-01-05T10:06:00+00:00", status="ok",
                                counts=counts))
            await s.commit()
            rid = run.id
        forget_engine()
        return rid
    return asyncio.run(go())


def _page(run_id: int) -> str:
    from kestrel.web.app import build_app

    with TestClient(build_app(worker=False)) as c:
        r = c.get(f"/runs/{run_id}")
    assert r.status_code == 200
    return html.unescape(r.text)


def test_each_search_shows_its_boards_share_and_the_jump_warning():
    rid = _seed({
        "Board search": {
            "glassdoor": {"new": 20, "empty": 14, "state": "jump"},
            "indeed": {"new": 12, "empty": 1, "state": "ok"},
            "google": {"new": 2, "empty": 2, "state": "jump"},  # under 5: too few to tell
        },
    })
    body = _page(rid)
    assert 'data-testid="run-bodies"' in body
    assert "No description in this run:" in body
    assert "Glassdoor 14 of 20 new roles had no description" in body
    assert "Indeed 1 of 12 new roles had no description" in body
    assert "Google too few to tell (2 new roles)" in body
    # The jump warning is the Sources page's own sentence, for the board that jumped only.
    assert ("Glassdoor: 14 of 20 new roles had no description in this run. The site may be "
            "blocking automated reads.") in body
    assert body.count('data-testid="run-body-warning"') == 1


def test_a_search_with_no_body_counts_or_a_malformed_entry_shows_nothing():
    rid = _seed({
        "Company board": None,
        "Odd search": {"indeed": {"new": "20", "empty": 3}, "glassdoor": [1, 2]},
    })
    body = _page(rid)
    assert 'data-testid="run-bodies"' not in body
    assert "No description in this run:" not in body


def test_the_label_is_one_constant():
    from kestrel.web.pages import runs_detail

    assert getattr(runs_detail, "RUN_BODIES_LABEL", None) == "No description in this run:"
