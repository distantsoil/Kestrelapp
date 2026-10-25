"""Analytics page (D-UI-101): the numbers for one range, every figure beside its sample size, the
states of the template, the exports and Rewrite summary. Cloud-safe: a throwaway database, fictional
roles, no model, no network. The live-browser parity check is tests/test_analytics_browser.py."""

from __future__ import annotations

import asyncio
import csv
import io
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.analytics import report as rpt
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import (
    Evaluation,
    Opportunity,
    Org,
    Outcome,
    Pursuit,
    Setting,
    Source,
    StatusEvent,
)
from kestrel.web.app import build_app
from tests import retry_seed

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
HX = {"HX-Request": "true"}


def _iso(days_ago: float, now: datetime = NOW) -> str:
    return (now - timedelta(days=days_ago)).isoformat()


class Seeder:
    """Fictional roles and applications written through a session."""

    def __init__(self, s, track: str = "employment", now: datetime = NOW) -> None:
        self.s, self.track, self.now, self.i = s, track, now, 0
        self.org: Org | None = None
        self.src: Source | None = None

    async def _base(self) -> None:
        if self.org is None:
            self.org = (await self.s.execute(
                select(Org).where(Org.normalized_name == "larkspur"))).scalar_one_or_none()
            if self.org is None:
                self.org = Org(name="Larkspur Logistics", normalized_name="larkspur",
                               size_band="200-2000", ownership="private")
                self.s.add(self.org)
            self.src = (await self.s.execute(select(Source).where(
                Source.track == self.track, Source.name == "Career pages"))).scalar_one_or_none()
            if self.src is None:
                self.src = Source(track=self.track, kind="greenhouse", name="Career pages",
                                  enabled=True)
                self.s.add(self.src)
            await self.s.flush()

    async def role(self, *, stage="t2_done", seen=5.0, score: float | None = None,
                   title="Operations Analyst", read=True, **extra) -> Opportunity:
        await self._base()
        self.i += 1
        opp = Opportunity(
            track=self.track, opp_type="job_posting", org_id=self.org.id,
            source_id=self.src.id, title=title, org_name=self.org.name,
            url=f"https://example.test/{self.i}", stage=stage,
            first_seen_at=_iso(seen, self.now), remote_type=extra.pop("remote_type", "remote"),
            **extra,
        )
        self.s.add(opp)
        await self.s.flush()
        if read or score is not None:
            self.s.add(Evaluation(
                opportunity_id=opp.id, tier=2, track=self.track, verdict="apply",
                verdict_model="apply", score=score if score is not None else 4.0,
                weighted_score=4.0, needs_review=False, dimensions={}, blocks={}, blockers=[],
                legitimacy={"status": "clean", "signals": []},
            ))
        return opp

    async def pursuit(self, status: str, *, sent_days_ago: float | None = 10.0,
                      reply_days: int | None = None, score: float | None = None,
                      shortlisted_days_ago: float | None = None, **role_kw) -> Pursuit:
        role_kw.setdefault("seen", (sent_days_ago or 5) + 1)
        opp = await self.role(score=score, **role_kw)
        p = Pursuit(
            opportunity_id=opp.id, track=self.track, status=status,
            applied_at=_iso(sent_days_ago, self.now) if sent_days_ago is not None else None,
        )
        self.s.add(p)
        await self.s.flush()
        if reply_days is not None:
            self.s.add(Outcome(pursuit_id=p.id, opportunity_id=opp.id, final_status=status,
                               furthest_stage="applied", days_to_first_response=reply_days))
        if shortlisted_days_ago is not None:
            self.s.add(StatusEvent(pursuit_id=p.id, to_status="shortlisted",
                                   occurred_at=_iso(shortlisted_days_ago, self.now)))
        await self.s.flush()
        return p


# ===================================================================== the numbers
async def test_where_they_stand_adds_up_to_everything_sent(sm):
    async with sm() as s:
        sd = Seeder(s)
        await sd.pursuit("offer", reply_days=3)
        await sd.pursuit("interviewing", reply_days=2)
        await sd.pursuit("screening", reply_days=2)
        await sd.pursuit("applied", sent_days_ago=3)  # waiting, recent
        await sd.pursuit("applied", sent_days_ago=30)  # silent past 14 days, no status yet
        await sd.pursuit("ghosted", sent_days_ago=40)
        await sd.pursuit("rejected", reply_days=6)
        await sd.pursuit("withdrawn", sent_days_ago=12)
        await sd.pursuit("shortlisted", sent_days_ago=None, seen=2)  # not sent: not in the bar
        await s.commit()
    async with sm() as s:
        r = await rpt.build_report(s, "employment", "all", now=NOW)
    seg = {x["key"]: x["n"] for x in r["standing"]["segments"]}
    assert seg == {"offer": 1, "iv": 2, "wait": 1, "rej": 1, "ghost": 2, "wd": 1}
    assert r["standing"]["total"] == sum(seg.values()) == 8  # adds up to everything sent
    assert r["standing"]["short_n"] == 9 and r["standing"]["not_sent"] == 1


async def test_a_range_counts_only_what_was_sent_in_it_and_tiles_compare_the_previous_period(sm):
    async with sm() as s:
        sd = Seeder(s)
        for _ in range(3):
            await sd.pursuit("applied", sent_days_ago=10)
        for _ in range(2):
            await sd.pursuit("applied", sent_days_ago=45)  # the previous 30 days
        await sd.pursuit("applied", sent_days_ago=70)
        await s.commit()
    async with sm() as s:
        r30 = await rpt.build_report(s, "employment", "30", now=NOW)
        r90 = await rpt.build_report(s, "employment", "90", now=NOW)
        rall = await rpt.build_report(s, "employment", "all", now=NOW)
    applied = {r["range"]: next(t for t in r["tiles"] if t["label"] == "Applied")
               for r in (r30, r90, rall)}
    assert applied["30"]["n"] == 3 and applied["30"]["prev"] == 2
    assert applied["90"]["n"] == 6 and applied["90"]["prev"] == 0  # nothing 90 to 180 days back
    assert applied["all"]["n"] == 6 and applied["all"]["prev"] is None  # no comparison for all


async def test_a_tile_whose_base_is_under_five_is_dashed_and_says_so(sm):
    async with sm() as s:
        sd = Seeder(s)
        for _ in range(3):
            await sd.pursuit("applied", sent_days_ago=5)
        await s.commit()
    async with sm() as s:
        r = await rpt.build_report(s, "employment", "90", now=NOW)
    tiles = {t["label"]: t for t in r["tiles"]}
    assert tiles["Roles found"]["few"] is False  # no base: never dashed
    for label in ("Shortlisted", "Applied", "Replies", "Interviews"):
        assert tiles[label]["few"] is True and tiles[label]["base"] == 3, label
    async with sm() as s:
        sd = Seeder(s)
        for _ in range(4):
            await sd.pursuit("applied", sent_days_ago=5)
        await s.commit()
    async with sm() as s:
        r = await rpt.build_report(s, "employment", "90", now=NOW)
    tiles = {t["label"]: t for t in r["tiles"]}
    assert tiles["Applied"]["few"] is False and tiles["Applied"]["base"] == 7  # shortlisted: 7
    assert tiles["Replies"]["few"] is False and tiles["Replies"]["n"] == 0


async def test_the_funnel_has_two_scales_and_hatches_steps_after_a_base_under_five(sm):
    async with sm() as s:
        sd = Seeder(s)
        for stage in ("new", "t0_reject", "t1_pass", "t2_done", "t2_done", "t2_done"):
            await sd.role(stage=stage, read=stage == "t2_done")
        for _ in range(3):
            await sd.pursuit("applied", sent_days_ago=5, reply_days=4)
        await s.commit()
    async with sm() as s:
        r = await rpt.build_report(s, "employment", "90", now=NOW)
    a = {x["label"]: x for x in r["funnel"]["a"]}
    b = {x["label"]: x for x in r["funnel"]["b"]}
    assert [x["label"] for x in r["funnel"]["a"]] == [
        "Found", "Passed your rules", "Passed quick check", "Fully read"]
    assert a["Found"]["n"] == 9 and a["Found"]["base"] is None  # its own scale
    assert b["Shortlisted"]["n"] == 3 and b["Shortlisted"]["base"] is None
    assert b["Applied"]["n"] == 3 and b["Applied"]["pct"] == 100
    assert b["Applied"]["few"] is True  # three shortlisted: the percent is not reliable
    assert b["First reply"]["n"] == 3 and b["First reply"]["few"] is True
    assert a["Passed your rules"]["few"] is False  # nine found


async def test_replies_by_fit_score_waits_until_every_band_holds_five(sm):
    async with sm() as s:
        sd = Seeder(s)
        for k in range(6):
            await sd.pursuit("applied", score=4.7, reply_days=3 if k < 3 else None)
        for _ in range(3):
            await sd.pursuit("applied", score=4.2)
        await s.commit()
    async with sm() as s:
        r = await rpt.build_report(s, "employment", "90", now=NOW)
    rows = {x["label"]: x for x in r["bands"]["rows"]}
    assert rows["4.5 to 5.0"]["reply"] == {"n": 3, "base": 6, "few": False, "pct": 50}
    assert rows["4.0 to 4.4"]["reply"]["few"] is True and rows["4.0 to 4.4"]["n"] == 3
    assert r["bands"]["comparable"] is False
    async with sm() as s:
        sd = Seeder(s)
        for _ in range(2):
            await sd.pursuit("applied", score=4.2)  # the 4.0 band now holds five
        await s.commit()
    async with sm() as s:
        r = await rpt.build_report(s, "employment", "90", now=NOW)
    assert r["bands"]["comparable"] is True


async def test_reply_timing_excludes_the_unanswered_and_buckets_the_wait_by_age(sm):
    async with sm() as s:
        sd = Seeder(s)
        for d in (2, 4, 6, 8):
            await sd.pursuit("interviewing", reply_days=d)
        await sd.pursuit("applied", sent_days_ago=3)
        await sd.pursuit("applied", sent_days_ago=9)
        await sd.pursuit("applied", sent_days_ago=21)
        await s.commit()
    async with sm() as s:
        few_r = await rpt.build_report(s, "employment", "90", now=NOW)
        async with sm() as s2:
            await Seeder(s2).pursuit("interviewing", reply_days=10)
            await s2.commit()
        r = await rpt.build_report(s, "employment", "90", now=NOW)
    assert few_r["timing"]["median"] is None and few_r["timing"]["n"] == 4  # four: too few
    assert r["timing"]["median"] == 6 and r["timing"]["n"] == 5  # unanswered are not in it
    assert r["timing"]["waiting"] == {"lt7": 1, "7to14": 1, "gt14": 1, "total": 3}
    assert r["timing"]["oldest"]["days"] == 21


async def test_breakdown_tabs_and_the_consulting_trigger_tab(sm):
    async with sm() as s:
        sd = Seeder(s)
        await sd.pursuit("applied", title="Senior Director of Operations", reply_days=2)
        await sd.pursuit("applied", title="Operations Manager")
        await s.commit()
    async with sm() as s:
        r = await rpt.build_report(s, "employment", "90", now=NOW)
    assert list(r["tabs"]) == ["src", "work", "level", "size", "own", "remote"]
    levels = {x["name"]: x for x in r["tabs"]["level"]["rows"]}
    assert levels["Director"]["sent"] == 1 and levels["Manager"]["sent"] == 1
    assert r["tabs"]["src"]["rows"][0]["name"] == "Career pages"
    assert r["tabs"]["src"]["rows"][0]["few"] is True  # two sent: no rate
    assert r["tabs"]["work"]["rows"][0]["name"] == rpt.NOT_STATED
    async with sm() as s:
        sd = Seeder(s, track="consulting")
        await sd.role(trigger_data={"event_type": "leadership_change"})
        await s.commit()
    async with sm() as s:
        r = await rpt.build_report(s, "consulting", "90", now=NOW)
    assert "trig" in r["tabs"]
    assert r["tabs"]["trig"]["rows"][0]["name"] == "leadership change"
    assert r["words"]["tiles"][2] == "Reached out"


async def test_plain_summary_says_nothing_is_reliable_with_too_few_and_names_the_counts(sm):
    async with sm() as s:
        sd = Seeder(s)
        await sd.pursuit("applied", sent_days_ago=20)
        await s.commit()
    async with sm() as s:
        r = await rpt.build_report(s, "employment", "90", now=NOW)
    plain = rpt.plain_summary(r, {"n_outcomes": 0, "discriminating": None})
    assert "fewer than 5" in plain["not_working"] and "only 1 application" in plain["not_working"]
    assert "1 application has had no reply for over 14 days" in plain["worth"]
    assert "Kestrel found 1" in plain["working"]
    assert "—" not in " ".join(plain.values())


async def test_an_empty_install_still_builds_every_card(sm):
    async with sm() as s:
        r = await rpt.build_report(s, "employment", "90", now=NOW)
    assert r["counts"] == {"found": 0, "read": 0, "short": 0, "sent": 0}
    assert r["standing"]["total"] == 0 and r["bands"]["rows"] == []
    assert r["timing"]["oldest"] is None and len(r["pace"]["bars"]) >= 1


# ===================================================================== the page
pytestmark_web = pytest.mark.usefixtures("_isolate_data_home")


def _read(fn):
    async def go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _seed_web(n_sent: int = 7) -> None:
    """A throwaway data home with `n_sent` applications dated relative to the real clock."""
    async def go():
        await reset_engine()
        retry_seed._wipe_db()
        migrate.run_migrations()
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            now = datetime.now(UTC)
            sd = Seeder(s, now=now)
            for k in range(n_sent):
                await sd.pursuit("interviewing" if k % 2 else "applied", sent_days_ago=3 + k * 2,
                                 reply_days=4 if k % 2 else None, score=4.6 if k < 4 else 4.1)
            await s.commit()
        await engine.dispose()
        forget_engine()

    asyncio.run(go())


@pytest.fixture
def web(_isolate_data_home, monkeypatch):
    async def _none(*a, **k):
        return None

    from kestrel.web import routes_control

    monkeypatch.setattr(routes_control, "_probe_ollama", _none, raising=False)
    _seed_web()
    with TestClient(build_app(worker=False)) as c:
        yield c


def test_the_page_renders_every_card_and_the_range_defaults_to_ninety_days(web):
    page = web.get("/analytics").text
    for tid in ("an-summary", "an-kpis", "an-standing", "an-funnel", "an-bands", "an-sources",
                "an-timing", "an-pace", "an-models-card"):
        assert f'data-testid="{tid}"' in page, tid
    assert 'data-testid="an-range-90"' in page
    assert re.search(r'class="is-on"[^>]*data-testid="an-range-90"', page)
    assert "Last 30 days" in page and "All time" in page
    assert "Where your applications stand" in page and "How Kestrel narrows the field" in page
    assert "Working</b>" in page and "Not working</b>" in page and "Worth changing</b>" in page
    # Converted screen: the legacy stylesheet is gone, the page sheet is in.
    assert "/static/css/kestrel.css" not in page and "/static/css/pages/analytics.css" in page
    assert "—" not in page


def test_a_range_change_swaps_only_the_body_and_an_unknown_range_is_ninety(web):
    part = web.get("/analytics?range=30", headers=HX).text
    assert "<html" not in part and 'id="honest-summary"' in part
    assert re.search(r'class="is-on"[^>]*data-testid="an-range-30"', part)
    assert "last 30 days" in part
    odd = web.get("/analytics?range=forever", headers=HX).text
    assert re.search(r'class="is-on"[^>]*data-testid="an-range-90"', odd)


def test_table_view_marks_the_stack_and_keeps_every_table(web):
    page = web.get("/analytics?range=90&view=table").text
    assert 'class="an-stack is-table"' in page
    assert 'aria-pressed="true" data-testid="an-table-view"' in page
    assert page.count('class="an-table"') >= 3  # standing, funnel and pace each have a table
    plain = web.get("/analytics").text
    assert 'class="an-stack"' in plain and 'class="an-stack is-table"' not in plain


def test_seven_applications_show_rates_and_under_five_say_too_few(web):
    page = web.get("/analytics").text
    assert "Not enough data yet" not in page  # every tile rests on seven or more
    # Source: seven sent, so a rate with its count; the funnel's reply step has 3 of 7.
    assert re.search(r"\(\d+ of 7\)", page)
    # The band of 4.1 scores holds three: Too few (3) with one footnote for the card.
    bands = page[page.index('data-testid="an-bands"'):page.index('data-testid="an-sources"')]
    assert "Too few (3)" in bands and bands.count('class="an-fewnote"') == 1
    assert "Too early to compare" in bands


def test_nothing_is_reliable_yet_on_an_empty_install(_isolate_data_home):
    _seed_web(0)
    with TestClient(build_app(worker=False)) as c:
        page = c.get("/analytics").text
    assert "Nothing is reliable yet" in page
    assert 'data-testid="rewrite-summary"' not in page
    assert "fewer than 5" in page
    assert page.count("Not enough data yet") >= 1


def test_the_page_never_calls_the_model_on_a_visit(web, monkeypatch):
    seen: list[object] = []
    from kestrel.analytics.summary import honest_summary

    async def spy(session, client, track, **kw):
        seen.append(client)
        return await honest_summary(session, None, track, **kw)

    monkeypatch.setattr("kestrel.web.pages.analytics.honest_summary", spy)
    assert web.get("/analytics").status_code == 200
    assert seen == [None]


def test_rewrite_summary_asks_the_model_saves_it_and_says_when(web, monkeypatch):
    from kestrel.analytics.metrics import collect_metrics
    from kestrel.analytics.summary import AnalyticsSummary

    calls: list[dict] = []

    async def fake(session, client, track, *, period=None):
        calls.append({"client": client, "period": period})
        return AnalyticsSummary(
            track=track, metrics=await collect_metrics(session, track), discriminating=True,
            insufficient_data=False, summary_md="Replies came from 3 of 7 applications.",
            headline_finding="x", one_thing_to_change="Follow up sooner.",
            evidence_for_it="2 had no reply for 14 days.", sample_size_warnings=["n=7"],
            from_model=True,
        )

    monkeypatch.setattr("kestrel.web.pages.analytics.honest_summary", fake)
    r = web.post("/analytics/narrative?track=employment&range=30")
    assert r.status_code == 200 and 'id="honest-summary"' in r.text
    assert calls[-1]["client"] is not None  # the one place the page asks a model
    assert calls[-1]["period"]["range"] == "last 30 days"  # the narrative names the same period
    assert "Replies came from 3 of 7 applications." in r.text and "Follow up sooner." in r.text
    assert "Updated today" in r.text and "It can be wrong, so check the numbers." in r.text
    saved = _read(lambda s: s.get(Setting, "analytics.narrative.employment.30"))
    assert saved.value["one_thing"] == "Follow up sooner."
    # It is still there on the next visit, with no model call, and only for that range.
    page = web.get("/analytics?range=30").text
    assert "Follow up sooner." in page and "Updated today" in page
    assert "Follow up sooner." not in web.get("/analytics?range=90").text


def test_a_model_that_gives_nothing_usable_leaves_the_numbers_and_says_so(web, monkeypatch):
    from kestrel.analytics.metrics import collect_metrics
    from kestrel.analytics.summary import AnalyticsSummary

    async def fake(session, client, track, *, period=None):
        return AnalyticsSummary(track=track, metrics=await collect_metrics(session, track),
                                discriminating=None, insufficient_data=False, summary_md="x",
                                headline_finding="x")

    monkeypatch.setattr("kestrel.web.pages.analytics.honest_summary", fake)
    r = web.post("/analytics/narrative?track=employment&range=90")
    assert r.status_code == 200 and 'data-testid="an-written"' not in r.text
    assert _read(lambda s: s.get(Setting, "analytics.narrative.employment.90")) is None


def test_the_summary_form_shows_its_own_status_and_skips_the_busy_cursor(web, monkeypatch):
    from kestrel.analytics.summary import AnalyticsSummary

    async def fake(session, client, track, **kw):
        from kestrel.analytics.metrics import collect_metrics
        return AnalyticsSummary(track=track, metrics=await collect_metrics(session, track),
                                discriminating=True, insufficient_data=False, summary_md="x",
                                headline_finding="x")

    monkeypatch.setattr("kestrel.web.pages.analytics.honest_summary", fake)
    page = web.get("/analytics").text
    form = page.split('hx-post="/analytics/narrative')[1].split("</form>")[0]
    assert "data-no-busy" in form and 'class="import-status"' in form and 'class="spin"' in form
    assert "range=90" in page.split('hx-post="/analytics/narrative')[1][:80]


# ===================================================================== exports
def test_the_numbers_csv_carries_every_figure_with_its_base(web):
    r = web.get("/analytics/export/numbers.csv?range=90")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert re.search(r'attachment; filename="kestrel-analytics-\d{4}-\d{2}-\d{2}\.csv"',
                     r.headers["content-disposition"])
    rows = list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))
    assert rows[0] == ["Section", "Label", "Value", "Out of", "Note"]
    sections = {row[0] for row in rows}
    assert {"Counts", "Where they stand", "Replies by fit score", "Reply timing",
            "Pace"} <= sections
    stand = [row for row in rows if row[0] == "Where they stand"]
    assert sum(int(x[2]) for x in stand) == int(stand[0][3]) == 7  # adds up to everything sent
    few = [row for row in rows if row[0] == "Replies by fit score" and row[4] == "Too few"]
    assert few  # under five carries "Too few", never a percent


def test_the_applications_csv_lists_every_application_with_its_history(web):
    r = web.get("/analytics/export/applications.csv")
    rows = list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))
    assert rows[0] == ["Role", "Company", "Status", "Sent", "Outcome", "Closed", "History"]
    assert len(rows) == 8 and rows[1][1] == "Larkspur Logistics"
    assert "kestrel-applications-" in r.headers["content-disposition"]


@pytest.mark.parametrize("surface", ["page", "numbers", "pdf"])
@pytest.mark.parametrize("confidential", [True, False])
def test_employer_source_and_saved_narrative_are_redacted_at_display(
    web, monkeypatch, surface, confidential,
):
    from kestrel.docs import render
    from kestrel.privacy import Employer, EmployerMap

    employer = "Secret Employer"
    source_name = f"{employer} jobs (Greenhouse)"
    narrative = {
        "summary_md": f"Summary about {employer}.",
        "one_thing": f"Review {employer}.",
        "evidence": f"Evidence from {employer}.",
        "warnings": [f"Few observations at {employer}."],
    }

    async def seed(session):
        source = (await session.execute(select(Source))).scalars().first()
        source.name = source_name
        await session.commit()
        return source.id

    source_id = _read(seed)
    key = "analytics.narrative.employment.90"
    _write_setting(key, narrative)

    async def privacy(session):
        return confidential, EmployerMap([Employer(name=employer, shown_as="your employer")])

    monkeypatch.setattr("kestrel.web.pages.analytics.confidential_once", privacy)
    rendered = []

    async def pdf(html):
        rendered.append(html)
        return render.RenderedPdf(pdf=b"%PDF-1.4 fake", page_count=1)

    monkeypatch.setattr(render, "render_pdf", pdf)
    route = {"page": "/analytics", "numbers": "/analytics/export/numbers.csv",
             "pdf": "/analytics/export/summary.pdf"}[surface]
    response = web.get(route + "?range=90")
    assert response.status_code == 200
    text = rendered[0] if surface == "pdf" else response.text
    shown_as = "your employer" if confidential else employer
    assert f"{shown_as} jobs (Greenhouse)" in text
    if surface != "numbers":
        for field in (narrative["summary_md"], narrative["one_thing"], narrative["evidence"],
                      narrative["warnings"][0]):
            assert field.replace(employer, shown_as) in text
    else:
        rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
        source_row = next(row for row in rows if row[1] == f"{shown_as} jobs (Greenhouse)")
        assert source_row[2:4] == ["3", "7"]
    if confidential:
        assert employer not in text
    assert _read(lambda session: session.get(Source, source_id)).name == source_name
    assert _read(lambda session: session.get(Setting, key)).value == narrative


def test_the_pdf_export_renders_locally_and_says_so_when_edge_is_missing(web, monkeypatch):
    from kestrel.docs import render

    seen: list[str] = []

    async def fake_pdf(html: str):
        seen.append(html)
        return render.RenderedPdf(pdf=b"%PDF-1.4 fake", page_count=1)

    monkeypatch.setattr(render, "render_pdf", fake_pdf)
    r = web.get("/analytics/export/summary.pdf?range=90")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert "kestrel-analytics-" in r.headers["content-disposition"]
    html = seen[0]
    assert "<style>" in html and ".an-card" in html  # the sheets are inlined
    assert 'data-testid="rewrite-summary"' not in html and 'id="an-range"' not in html
    assert "<title></title>" in html  # nothing named in the document properties

    async def boom(html: str):
        raise RuntimeError("no browser here")

    monkeypatch.setattr(render, "render_pdf", boom)
    r = web.get("/analytics/export/summary.pdf")
    assert r.status_code == 503 and "Edge" in r.text


def test_the_oldest_role_is_redacted_in_confidential_mode(web, monkeypatch):
    from kestrel.privacy import Employer, EmployerMap

    async def conf(session):
        return True, EmployerMap([
            Employer(name="Larkspur Logistics", shown_as="A logistics company"),
        ])

    monkeypatch.setattr("kestrel.web.pages.analytics.confidential_once", conf)
    page = web.get("/analytics").text
    csv_text = web.get("/analytics/export/applications.csv").text
    assert "Larkspur Logistics" not in page and "A logistics company" in page
    assert "Larkspur Logistics" not in csv_text and "A logistics company" in csv_text


# ===================================================================== the stylesheet
_CSS = Path(paths.web_static_dir()) / "css" / "pages" / "analytics.css"
_TEMPLATES = Path(paths.web_templates_dir())


def test_the_page_stylesheet_uses_theme_tokens_only():
    css = re.sub(r"/\*.*?\*/", "", _CSS.read_text(encoding="utf-8"), flags=re.S)
    assert not re.search(r"#[0-9A-Fa-f]{3,8}\b", css), "hex color in analytics.css"
    assert not re.search(r"\b(?:rgb|rgba|hsl|hsla|oklch|oklab)\(", css)
    assert "@container (max-width: 1100px)" in css and "@container (max-width: 880px)" in css


def test_every_status_segment_has_a_label_a_count_and_a_table_row(web):
    page = web.get("/analytics").text
    standing = page[page.index('data-testid="an-standing"'):page.index('data-testid="an-funnel"')]
    for label in ("Offer", "Interviewing", "Waiting for a reply", "Turned down",
                  "No reply after 14 days", "You withdrew"):
        assert standing.count(label) >= 2, label  # legend and table row


def test_templates_use_no_color_literals():
    for name in ("analytics.html", "partials/analytics_body.html",
                 "partials/analytics_summary.html", "analytics_print.html"):
        text = (_TEMPLATES / name).read_text(encoding="utf-8")
        assert not re.search(r"#[0-9A-Fa-f]{6}\b|rgba?\(", text), name


# ===================================================================== review fixes
def _hosted_tier2(monkeypatch, on: bool = True):
    """Settings as the page reads them, with tier 2 on an online backend and Hosted mode on."""
    from kestrel.web.pages import analytics as page

    orig = page.load_settings_with_db

    async def patched(session):
        st = (await orig(session)).model_copy(deep=True)
        if on:
            st.llm.tiers["tier2"] = st.llm.tiers["tier2"].model_copy(
                update={"backend": "frontier", "model": "vendor/model-a"})
            hm = st.llm.hosted_mode
            hm.enabled = True
            hm.acknowledged_at = "2026-10-01T00:00:00Z"
            hm.acknowledged_terms = hm.disclosure_terms()
        return st

    monkeypatch.setattr(page, "load_settings_with_db", patched)


def _dest():
    from kestrel.config import get_settings

    return f"frontier|{get_settings().llm.backends['frontier'].base_url}|vendor/model-a"


def _spy_summary(monkeypatch):
    from kestrel.analytics.metrics import collect_metrics
    from kestrel.analytics.summary import AnalyticsSummary

    calls: list[object] = []

    async def fake(session, client, track, *, period=None):
        calls.append(client)
        return AnalyticsSummary(
            track=track, metrics=await collect_metrics(session, track), discriminating=True,
            insufficient_data=False, summary_md="Written.", headline_finding="x",
            one_thing_to_change="Follow up.", from_model=client is not None,
        )

    monkeypatch.setattr("kestrel.web.pages.analytics.honest_summary", fake)
    return calls


def test_rewrite_summary_on_an_online_model_asks_first_and_states_what_is_sent(web, monkeypatch):
    import html

    from kestrel.config import HOSTED_TIER_SENDS

    _hosted_tier2(monkeypatch)
    _spy_summary(monkeypatch)
    page = web.get("/analytics").text
    assert 'hx-get="/analytics/narrative/online' in page
    assert 'hx-post="/analytics/narrative' not in page
    body = html.unescape(web.get("/analytics/narrative/online?track=employment&range=90").text)
    t2 = HOSTED_TIER_SENDS["tier2"].split("{")[0]
    assert t2[:1].upper() + t2[1:] in body  # the disclosure the operator acknowledged
    assert 'data-testid="an-sends-page"' in body and "Not sent:" in body
    assert "vendor/model-a" in body and 'data-testid="an-send"' in body


def test_an_online_rewrite_is_refused_server_side_without_the_confirmation(web, monkeypatch):
    _hosted_tier2(monkeypatch)
    calls = _spy_summary(monkeypatch)
    r = web.post("/analytics/narrative?track=employment&range=90")
    assert r.status_code == 200 and "Nothing was sent. Confirm in the dialog first." in r.text
    assert [c for c in calls if c is not None] == []  # the model was never asked
    r = web.post("/analytics/narrative?track=employment&range=90",
                 data={"confirmed": "1", "model": "vendor/model-a", "dest": _dest()})
    assert [c for c in calls if c is not None] != [] and "Written." in r.text


def test_the_online_dialog_honors_confidential_mode(web, monkeypatch):
    from kestrel.docs import current_employer

    _hosted_tier2(monkeypatch)
    _write_setting("confidential_mode", True)
    held = web.get("/analytics/narrative/online?track=employment&range=90").text
    assert 'data-testid="an-employer-held"' in held and 'data-testid="an-send"' not in held

    async def one(session, **kw):
        return {"x": "y"}

    monkeypatch.setattr(current_employer, "load_employer_map", one)
    hidden = web.get("/analytics/narrative/online?track=employment&range=90").text
    assert "your current employer is hidden before anything is sent" in hidden
    assert 'data-testid="an-send"' in hidden


def test_a_local_model_rewrites_at_once_with_no_dialog(web, monkeypatch):
    _hosted_tier2(monkeypatch, on=False)
    calls = _spy_summary(monkeypatch)
    page = web.get("/analytics").text
    assert 'hx-post="/analytics/narrative' in page and "/analytics/narrative/online" not in page
    web.post("/analytics/narrative?track=employment&range=90")
    assert [c for c in calls if c is not None] != []


def test_an_answer_with_nothing_to_change_is_saved_and_the_line_is_hidden(web, monkeypatch):
    from kestrel.analytics.metrics import collect_metrics
    from kestrel.analytics.summary import AnalyticsSummary

    async def fake(session, client, track, *, period=None):
        return AnalyticsSummary(
            track=track, metrics=await collect_metrics(session, track), discriminating=True,
            insufficient_data=False, summary_md="All steady.", headline_finding="x",
            one_thing_to_change="", from_model=True,
        )

    monkeypatch.setattr("kestrel.web.pages.analytics.honest_summary", fake)
    r = web.post("/analytics/narrative?track=employment&range=90")
    assert "All steady." in r.text and "One thing to change" not in r.text
    assert "did not give a usable answer" not in r.text
    assert _read(lambda s: s.get(Setting, "analytics.narrative.employment.90")) is not None


def test_a_not_discriminating_finding_is_said_once(web, monkeypatch):
    from kestrel.analytics.metrics import collect_metrics
    from kestrel.analytics.summary import AnalyticsSummary

    lead = "The score is not discriminating: bands convert alike."

    async def fake(session, client, track, *, period=None):
        return AnalyticsSummary(
            track=track, metrics=await collect_metrics(session, track), discriminating=False,
            insufficient_data=False, summary_md=f"{lead}\n\nBands look alike.",
            headline_finding=lead, one_thing_to_change="Review the score.", from_model=True,
        )

    monkeypatch.setattr("kestrel.web.pages.analytics.honest_summary", fake)
    r = web.post("/analytics/narrative?track=employment&range=90")
    assert r.text.count(lead) == 1 and "Bands look alike." in r.text


def _oklab_pair(a: str, b: str) -> float:
    """The smallest OKLab distance (x100) between two sRGB colors across normal vision and
    protan, deutan and tritan simulations (Machado et al., severity 1.0)."""
    import numpy as np

    sims = [np.eye(3),
            np.array([[.152286, 1.052583, -.204868], [.114503, .786281, .099216],
                      [-.003882, -.048116, 1.051998]]),
            np.array([[.367322, .860646, -.227968], [.280085, .672501, .047413],
                      [-.011820, .042940, .968881]]),
            np.array([[1.255528, -.076749, -.178779], [-.078411, .930809, .147602],
                      [.004733, .691367, .303900]])]
    m1 = np.array([[.4122214708, .5363325363, .0514459929],
                   [.2119034982, .6806995451, .1073969566],
                   [.0883024619, .2817188376, .6299787005]])
    m2 = np.array([[.2104542553, .7936177850, -.0040720468],
                   [1.9779984951, -2.4285922050, .4505937099],
                   [.0259040371, .7827717662, -.8086757660]])

    def lab(hex_: str, sim) -> np.ndarray:
        c = np.array([int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)])
        lin = np.where(c <= .04045, c / 12.92, ((c + .055) / 1.055) ** 2.4)
        return m2 @ np.cbrt(m1 @ np.clip(sim @ lin, 0, 1))

    return min(float(np.linalg.norm(lab(a, s) - lab(b, s))) * 100 for s in sims)


def test_interviewing_and_no_reply_after_14_days_stay_apart_for_every_kind_of_vision():
    import re

    css = (Path(rpt.__file__).parents[1] / "web/static/css/tokens.css").read_text("utf-8")
    page = (Path(rpt.__file__).parents[1] / "web/static/css/pages/analytics.css").read_text("utf-8")
    assert "--an-ghost: var(--muted)" in page and "--an-iv: var(--brand)" in page

    def token(block: str, name: str) -> str:
        return re.search(rf"--{name}:\s*(#[0-9A-Fa-f]{{6}})", block).group(1)

    root = css.split("@media (prefers-color-scheme: dark)")[0]
    dark = css.split(':root:root:root[data-theme="dark"] {')[1].split("}")[0]
    for block in (root, dark):
        assert _oklab_pair(token(block, "brand"), token(block, "muted")) >= 8


def _write_setting(key, value):
    async def go():
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            await s.merge(Setting(key=key, value=value))
            await s.commit()
        await engine.dispose()

    asyncio.run(go())


def test_a_held_send_reads_as_not_sent_never_as_no_usable_answer(web, monkeypatch):
    """The send is refused after the dialog (confidential mode, no employer chosen): the page
    says nothing was sent, not that the model gave no usable answer."""
    from kestrel.analytics import summary as summ
    from kestrel.pipeline import _shared
    from kestrel.pipeline._shared import HOSTED_REFUSAL

    _hosted_tier2(monkeypatch)
    monkeypatch.setattr(summ, "_MIN_OUTCOMES", 0)

    async def refused(*a, **kw):
        return _shared.GuardResult(ok=False, quarantined=True, error=HOSTED_REFUSAL)

    monkeypatch.setattr(_shared, "run_prompt_hosted", refused)
    r = web.post("/analytics/narrative?track=employment&range=90",
                 data={"confirmed": "1", "model": "vendor/model-a", "dest": _dest()})
    assert "Not sent: choose your current employer in Settings first" in r.text
    assert "did not give a usable answer" not in r.text


@pytest.mark.parametrize("path", ["numbers.csv", "summary.pdf", "applications.csv"])
def test_every_export_is_refused_in_confidential_mode_with_no_employer_set(web, path):
    """Nothing could be hidden, so no export of any kind (final re-review B1)."""
    _write_setting("confidential_mode", True)
    r = web.get(f"/analytics/export/{path}?track=employment&range=90")
    assert r.status_code == 409 and "no employer is set" in r.text
