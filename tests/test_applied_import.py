"""Applied-history import (SPEC.md 5.6, D-IMPORT-1): parse a tracker table, review it, write
hidden opportunities plus pursuits, and hide re-posts of applied roles on arrival."""

from __future__ import annotations

import re

from sqlalchemy import select

from kestrel.db.models import Opportunity, Pursuit, Source, StatusEvent
from kestrel.ingest.base import RawPosting
from kestrel.ingest.normalize import normalize_and_store
from kestrel.services import applied_import as svc
from tests.test_hosted_mode import _fresh_app

# A trimmed replica of a notes-app tracker: front matter and callouts above the table, a
# separator row, an activity row, an unknown status word, and a re-posted aggregator listing.
TRACKER_MD = """---
type: job-search
title: Job Applications (data)
---

# Job Applications

> [!tip] To add an application
> Add a new row to the table below. **No quotes needed.**

| Company | Role | Date | Source | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| Acme Corp | VP Information Technology | 2026-09-30 | LinkedIn | No response | Easy Apply, v12 |
| Northwind | Director of IT | 2026-10-02 | Company site | In conversation | Screen 10/08 |
| Self — Resume Update | Profile refresh | 2026-10-03 | LinkedIn | Completed | activity type: cv |
| Jobgether | Sr. Director, Technology & Systems | 2026-10-11 | LinkedIn | Completed | Aggregator |
| Padagis | VP Information Technology | 10/13/2026 | Company site | Rejected | Confirmed |
"""

TRACKER_CSV = (
    "Employer,Position,Applied on,Status,Link\n"
    "Acme Corp,VP Information Technology,2026-09-30,acknowledged,https://acme.example/jobs/1\n"
    "Contoso,Head of IT,not a date,applied,\n"
)


# ------------------------------------------------------------------------------- parsing
def test_markdown_tracker_parses_around_front_matter_and_callouts():
    rev = svc.parse_rows(TRACKER_MD)
    assert rev.fmt == "markdown" and not rev.errors
    assert rev.columns == {
        "company": "Company",
        "title": "Role",
        "date": "Date",
        "source": "Source",
        "status": "Status",
        "notes": "Notes",
    }
    assert [r.index for r in rev.rows] == [1, 2, 3, 4, 5]
    acme, nw, self_row, jobgether, padagis = rev.rows
    assert (acme.status, acme.date, acme.source) == ("applied", "2026-09-30", "LinkedIn")
    assert nw.status == "screening"
    assert self_row.activity and not self_row.default_include
    assert jobgether.status is None and jobgether.importable  # "Completed" waits for a choice
    assert not jobgether.default_include
    assert padagis.date == "2026-10-13" and padagis.status == "rejected"
    assert acme.default_include and padagis.default_include


def test_csv_tracker_maps_aliases_and_flags_a_bad_date():
    rev = svc.parse_rows(TRACKER_CSV)
    assert rev.fmt == "csv" and not rev.errors
    assert rev.columns["company"] == "Employer" and rev.columns["url"] == "Link"
    good, bad = rev.rows
    assert good.status == "applied" and good.url == "https://acme.example/jobs/1"
    assert bad.problems == ["date 'not a date' not understood (use YYYY-MM-DD)"]
    assert not bad.importable


def test_missing_required_column_is_one_clear_error():
    rev = svc.parse_rows("Company,Role\nAcme,CTO\n")
    assert rev.rows == [] and rev.errors
    assert "Missing required column(s): date, status" in rev.errors[0]
    header_only = svc.parse_rows("| Company | Role | Date | Status |\n|---|---|---|---|\n")
    assert header_only.rows == [] and "no data rows" in header_only.errors[0]
    dup = svc.parse_rows("Date,Company,Role,Status,Date\n2026-01-02,Acme,CTO,applied,bad\n")
    assert dup.rows[0].date == "2026-01-02"  # a duplicate header reads its first column


def test_status_mapping_accepts_kestrel_names_and_tracker_words():
    assert svc.map_status("Interviewing") == "interviewing"
    assert svc.map_status("No Response") == "applied"
    assert svc.map_status("in   conversation") == "screening"
    assert svc.map_status("proposal sent", "consulting") == "proposal_sent"
    assert svc.map_status("Completed") is None
    assert svc.map_status("") is None


def test_unknown_columns_are_kept_in_extras_never_dropped():
    rev = svc.parse_rows(
        "Company,Role,Date,Status,Resume version\nAcme,CTO,2026-01-02,applied,v14\n"
    )
    assert rev.rows[0].extras == {"Resume version": "v14"}


# ------------------------------------------------------------------------------- writing
async def test_commit_writes_hidden_opportunity_and_pursuit_per_ticked_row(sm):
    async with sm() as s:
        rev = await svc.review(s, TRACKER_MD)
        choices = {
            1: svc.RowChoice(include=True),
            2: svc.RowChoice(include=True, allow_reposts=True),
            3: svc.RowChoice(include=False),
            4: svc.RowChoice(include=True, status="applied"),  # the operator's pick
            5: svc.RowChoice(include=True),
        }
        result = await svc.commit(s, rev, choices)
    assert (result.imported, result.updated, result.skipped) == (4, 0, 1)
    assert result.problems == []
    async with sm() as s:
        pursuits = (await s.execute(select(Pursuit).order_by(Pursuit.id))).scalars().all()
        opps = {o.id: o for o in (await s.execute(select(Opportunity))).scalars().all()}
        events = (await s.execute(select(StatusEvent))).scalars().all()
        src = (await s.execute(select(Source).where(Source.kind == "import"))).scalars().one()
    assert len(pursuits) == 4 and len(events) == 4
    by_key = {(opps[p.opportunity_id].org_name, opps[p.opportunity_id].title): p for p in pursuits}
    acme = by_key[("Acme Corp", "VP Information Technology")]
    assert acme.status == "applied" and acme.applied_at == "2026-09-30T00:00:00+00:00"
    assert by_key[("Northwind", "Director of IT")].allow_reposts is True
    assert by_key[("Jobgether", "Sr. Director, Technology & Systems")].status == "applied"
    # Padagis shares Acme's title; a different org is a different pursuit, never merged.
    assert by_key[("Padagis", "VP Information Technology")].status == "rejected"
    for o in opps.values():
        assert o.archived_at and o.archive_reason == "applied"
        assert o.source_id == src.id and o.stage == "new"
        assert o.description_md.startswith("Imported from")
    assert src.enabled is False  # never scheduled


async def test_commit_refuses_a_status_the_form_invented(sm):
    async with sm() as s:
        rev = await svc.review(s, TRACKER_CSV)
        result = await svc.commit(
            s,
            rev,
            {1: svc.RowChoice(include=True, status="promoted_to_ceo"), 2: svc.RowChoice(True)},
        )
    assert result.imported == 0 and result.skipped == 2
    assert "status 'promoted_to_ceo' is not valid" in result.problems[0]
    assert "not understood" in result.problems[1]


async def test_reviewing_again_marks_rows_already_tracked_and_can_update_them(sm):
    async with sm() as s:
        rev = await svc.review(s, TRACKER_CSV)
        await svc.commit(s, rev, {1: svc.RowChoice(include=True)})
    again = (
        "Company,Role,Date,Status\nAcme Corp,VP Information Technology,2026-09-30,interviewing\n"
    )
    async with sm() as s:
        rev = await svc.review(s, again)
        row = rev.rows[0]
        assert row.existing_pursuit_id is not None and "same org and title" in row.existing_label
        assert not row.default_include
        result = await svc.commit(s, rev, {1: svc.RowChoice(include=True)})  # include = update
    assert (result.imported, result.updated) == (0, 1)
    async with sm() as s:
        p = (await s.execute(select(Pursuit))).scalars().one()
        events = (await s.execute(select(StatusEvent))).scalars().all()
    assert p.status == "interviewing" and len(events) == 2


# ------------------------------------------------------------------- re-post suppression
async def _scrape_source(s, name="gh") -> Source:
    src = Source(name=name, kind="greenhouse", track="employment", enabled=True, config={})
    s.add(src)
    await s.flush()
    return src


async def test_a_new_posting_of_an_applied_role_is_hidden_on_arrival(sm):
    async with sm() as s:
        rev = await svc.review(s, TRACKER_CSV)
        await svc.commit(s, rev, {1: svc.RowChoice(include=True)})
    async with sm() as s:
        src = await _scrape_source(s)
        res = await normalize_and_store(
            s,
            RawPosting(
                external_id="gh-1",
                url="https://boards.example/acme/1",
                title="VP, Information Technology",
                org_name="Acme Corp",
                description_md="Lead IT for a growing company. " * 20,
            ),
            source=src,
        )
        await s.commit()
        opp = res.opportunity
        pursuit_id = (await s.execute(select(Pursuit.id))).scalar_one()
    assert res.action == "new" and res.suppressed is True
    assert opp.archived_at and opp.archive_reason == "applied"
    assert opp.stage_reason == f"Re-post of pursuit #{pursuit_id}"


async def test_allow_reposts_lets_the_listing_surface(sm):
    async with sm() as s:
        rev = await svc.review(s, TRACKER_CSV)
        await svc.commit(s, rev, {1: svc.RowChoice(include=True, allow_reposts=True)})
    async with sm() as s:
        src = await _scrape_source(s)
        res = await normalize_and_store(
            s,
            RawPosting(
                external_id="gh-2",
                title="VP Information Technology",
                org_name="Acme Corp",
                description_md="Lead IT. " * 30,
            ),
            source=src,
        )
        await s.commit()
    assert res.suppressed is False and res.opportunity.archived_at is None


async def test_an_unrelated_posting_at_the_same_org_is_not_hidden(sm):
    async with sm() as s:
        rev = await svc.review(s, TRACKER_CSV)
        await svc.commit(s, rev, {1: svc.RowChoice(include=True)})
    async with sm() as s:
        src = await _scrape_source(s)
        res = await normalize_and_store(
            s,
            RawPosting(
                external_id="gh-3",
                title="Senior Accountant",
                org_name="Acme Corp",
                description_md="Close the books. " * 30,
            ),
            source=src,
        )
        await s.commit()
    assert res.suppressed is False and res.opportunity.archived_at is None


# ------------------------------------------------------------------------------- the page
def test_settings_has_an_applied_tab_with_instructions(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    page = client.get("/settings").text
    assert ">Applied</button>" in page and 'hx-get="/settings/applied"' in page
    panel = client.get("/settings/applied").text
    assert "How to format the file" in panel and 'data-testid="applied-form"' in panel
    assert "no response" in panel and '<td class="mono">applied</td>' in panel
    assert "—" not in panel.replace("Self —", "")


def test_review_then_import_through_the_form(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    review = client.post("/settings/applied/review", data={"text": TRACKER_MD}).text
    assert (
        'data-testid="applied-review"' in review and review.count('data-testid="applied-row"') == 5
    )
    assert 'name="include_1" value="on" checked' in review
    assert 'name="include_2" value="on" checked' in review
    assert (
        'name="include_3" value="on" ' in review
        and "checked" not in review.split('name="include_3"')[1].split("</td>")[0]
    )  # the activity row is listed, never pre-ticked
    assert "not an application" in review
    assert 'name="status_4"' in review  # "Completed" needs a choice
    assert 'name="include_4" value="on" ' in review
    assert "disabled" not in review.split('name="include_4"')[1].split(">")[0]
    done = client.post(
        "/settings/applied/import",
        data={
            "text": TRACKER_MD,
            "include_1": "on",
            "include_4": "on",
            "status_4": "applied",
            "include_5": "on",
            "reposts_5": "on",
        },
    ).text
    assert 'data-testid="applied-result"' in done and "<b>3</b> new pursuit" in done
    # Ticked without a status chosen: skipped and named, nothing invented.
    skipped = client.post(
        "/settings/applied/import", data={"text": TRACKER_MD, "include_4": "on"}
    ).text
    assert "<b>0</b> new pursuit" in skipped and "row 4" in skipped
    board = client.get("/pursuits?view=table").text
    assert "VP Information Technology" in board and 'name="allow" value="on" checked' in board
    # The hidden rows are reachable, with their reason, and absent from the active list.
    hidden = client.get("/opportunities?hidden=1").text
    assert "Acme Corp" in hidden
    assert "Acme Corp" not in client.get("/opportunities").text


def test_upload_and_empty_submissions(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    empty = client.post("/settings/applied/review", data={"text": "   "}).text
    assert "Paste the table or choose a file first" in empty
    uploaded = client.post(
        "/settings/applied/review",
        files={"file": ("apps.csv", TRACKER_CSV.encode("utf-8-sig"), "text/csv")},
    ).text
    assert review_rows(uploaded) == 2


def review_rows(html: str) -> int:
    return html.count('data-testid="applied-row"')


def test_pursuit_repost_toggle_round_trips(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    client.post("/settings/applied/import", data={"text": TRACKER_CSV, "include_1": "on"})
    board = client.get("/pursuits?view=table").text
    assert 'name="allow" value="on" checked' not in board and "/reposts" in board
    pid = int(re.search(r"/pursuits/(\d+)/reposts", board).group(1))
    assert client.post(f"/pursuits/{pid}/reposts", data={"allow": "on"}).status_code == 204
    assert 'name="allow" value="on" checked' in client.get("/pursuits?view=table").text
    assert client.post(f"/pursuits/{pid}/reposts", data={}).status_code == 204
    assert 'name="allow" value="on" checked' not in client.get("/pursuits?view=table").text
    assert client.post("/pursuits/999999/reposts", data={"allow": "on"}).status_code == 404
