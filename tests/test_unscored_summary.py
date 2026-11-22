"""One place answers "why has this role no score?" for every role: the Runs page's Why roles
have no score card, grouped by the reason the Pipeline row shows, with the action that moves each
group forward. The diagnostics bundle carries the counts by reason, numbers only. Nothing here
hides, rejects, queues or sends a role. Invented text only."""

from __future__ import annotations

import asyncio
import html as htmllib
import io
import json
import zipfile

import pytest
from sqlalchemy import select

from kestrel.db.base import iso
from kestrel.db.models import Opportunity, Task
from kestrel.web import app as app_module
from kestrel.web.fit import EMPTY_POSTING_STAGE_REASON
from tests import test_pipeline_view as T

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_EMPTY_TITLE = "Head of Invented Platforms"


def _seed() -> dict[str, int]:
    """The shared Pipeline seed (role e is quarantined, d has no score), plus: d's last scoring
    task died, an empty posting from Glassdoor, and a role a rule set aside."""
    ids = asyncio.run(T._seed())

    async def go(s):
        org_id = (await s.get(Opportunity, ids["d"])).org_id
        src_id = (await s.get(Opportunity, ids["d"])).source_id
        s.add(Task(type="t1", status="dead", attempts=3, last_error="connection refused",
                   payload={"opportunity_id": ids["d"]}))
        empty = Opportunity(
            track="employment", opp_type="job_posting", org_id=org_id, source_id=src_id,
            title=_EMPTY_TITLE, url="https://www.glassdoor.com/job-listing/invented-2",
            stage="t1_pass", stage_reason=EMPTY_POSTING_STAGE_REASON, description_md=None,
            first_seen_at=iso(T._seen_today()))
        aside = Opportunity(
            track="employment", opp_type="job_posting", org_id=org_id, source_id=src_id,
            title="Invented Role Set Aside", url="https://jobs.lever.co/invented/3",
            stage="t0_reject", stage_reason="blocker:stale: Posting too old",
            first_seen_at=iso(T._seen_today()))
        s.add_all([empty, aside])
        await s.commit()
        return empty.id

    ids["empty"] = asyncio.run(T._with_session(go))
    return ids


@pytest.fixture(autouse=True)
def _unpaused():
    app_module.set_paused(False)
    yield
    app_module.set_paused(False)


def _card(html: str) -> str:
    start = html.index('data-testid="runs-unscored"')
    return htmllib.unescape(html[start:html.index("</section>", start)])


def test_runs_lists_every_unscored_role_under_its_reason_with_the_way_forward():
    ids = _seed()
    with T._client() as c:
        card = _card(c.get("/runs").text)
    assert 'data-reason="dead"' in card and "Kestrel could not reach the model." in card
    assert 'data-reason="failed"' in card and "Needs another try" in card
    assert 'data-reason="empty"' in card and _EMPTY_TITLE in card
    assert "Greenhouse" in card  # where it was found (the seed's source), not its link's host
    assert f'href="/opportunities/{ids["empty"]}"' in card
    assert "Set aside by your rules: 1 role" in card
    for oid in (ids["d"], ids["e"], ids["empty"]):  # every unscored role is linked
        assert f'href="/opportunities/{oid}"' in card


def test_a_paused_app_shows_resume_as_the_way_forward():
    _seed()

    async def add(s):
        waiting = select(Opportunity).where(Opportunity.stage == "t0_pass")
        for opp in (await s.execute(waiting)).scalars():
            s.add(Task(type="t1", status="pending", payload={"opportunity_id": opp.id}))
        await s.commit()

    asyncio.run(T._with_session(add))
    app_module.set_paused(True)
    with T._client() as c:
        card = _card(c.get("/runs").text)
    assert 'data-reason="app_paused"' in card
    assert 'href="/runs"' in card and "Resume on Runs" in card


def test_the_poll_fragment_does_not_rebuild_the_card():
    _seed()
    with T._client() as c:
        frag = c.get("/runs", headers={"HX-Request": "true"}).text
    assert 'data-testid="runs-unscored"' not in frag


def test_the_bundle_counts_roles_by_reason_with_no_role_text():
    _seed()
    with T._client() as c:
        r = c.get("/about/diagnostics")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        data = z.read("unscored.json")
    counts = json.loads(data)["counts"]
    assert counts["dead"] == 1 and counts["empty"] == 1 and counts["t0_reject"] == 1
    assert counts.get("failed", 0) >= 1
    assert all(isinstance(v, int) for v in counts.values())
    for text in (_EMPTY_TITLE.encode(), b"Invented Role Set Aside", T._EMPLOYER.encode(),
                 b"glassdoor"):
        assert text not in data


def test_the_bundle_lists_unscored_json_and_writes_it_inside_the_part_file(monkeypatch):
    """Disclosure: the README and the About page list unscored.json (diagnostics.CONTENTS).
    Atomic: it is written inside the .part file before the rename; nothing opens the delivered
    zip again to append to it."""
    from kestrel.services import diagnostics

    assert "unscored.json" in [name for name, _what in diagnostics.CONTENTS]
    assert "unscored.json" in diagnostics.readme_text()
    _seed()
    modes: list[tuple[str, str]] = []
    real = zipfile.ZipFile

    class Spy(real):
        def __init__(self, file, mode="r", *a, **kw):
            modes.append((str(file), mode))
            super().__init__(file, mode, *a, **kw)

    monkeypatch.setattr(zipfile, "ZipFile", Spy)
    with T._client() as c:
        about = c.get("/about").text
        r = c.get("/about/diagnostics")
    assert "unscored.json" in about
    assert r.status_code == 200
    writes = [(f, m) for f, m in modes if m != "r"]
    assert writes and all(m == "w" and f.endswith(".part") for f, m in writes), writes
    with real(io.BytesIO(r.content)) as z:
        assert json.loads(z.read("unscored.json"))["counts"]["dead"] == 1


def test_a_direct_build_says_the_counts_were_not_taken(tmp_path):
    from kestrel.config import get_settings
    from kestrel.services import diagnostics

    archive = diagnostics.build_bundle(get_settings(), out_dir=tmp_path)
    with zipfile.ZipFile(archive) as z:
        assert json.loads(z.read("unscored.json"))["counts"] == "not counted"


def test_unscored_json_keeps_only_reason_keys_and_numbers():
    from kestrel.services.diagnostics import unscored_file

    got = unscored_file({"dead": 2, "Invented Corp title": 1, "empty": "3", "held": True})
    assert got["counts"] == {"dead": 2}


def test_the_reason_query_loads_no_description_and_no_scored_role():
    """Filtered in SQL to the unscored stages, with only the columns the reading needs."""
    from sqlalchemy import event

    from kestrel.db.engine import make_async_engine
    from kestrel.web.unscored import reason_counts

    _seed()
    statements: list[str] = []

    async def go():
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from kestrel import paths

        engine = make_async_engine(paths.db_path())
        event.listen(engine.sync_engine, "before_cursor_execute",
                     lambda conn, cur, stmt, params, ctx, many: statements.append(stmt))
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await reason_counts(s)
        finally:
            await engine.dispose()

    counts = asyncio.run(go())
    assert counts["dead"] == 1
    joined = "\n".join(statements)
    assert "description_md" not in joined
    assert "previous_description_md" not in joined
    main = [st for st in statements if "json_extract(opportunity.raw" in st]
    assert main and all("opportunity.stage IN" in st for st in main), main


def test_a_failure_while_counting_still_delivers_the_bundle(monkeypatch):
    """Any error from the counts (not only a database one) leaves unscored.json saying the
    counts were not taken; the download itself succeeds."""
    from kestrel.web import unscored

    _seed()

    async def boom(_session):
        raise RuntimeError("invented counting failure")

    monkeypatch.setattr(unscored, "reason_counts", boom)
    with T._client() as c:
        r = c.get("/about/diagnostics")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert json.loads(z.read("unscored.json"))["counts"] == "not counted"
