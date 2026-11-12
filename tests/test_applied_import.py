"""Applied-history import (SPEC.md 5.6, D-IMPORT-1): parse a tracker table, review it, write
hidden opportunities plus pursuits, and hide re-posts of applied roles on arrival."""

from __future__ import annotations

import csv
import html as html_lib
import io
import re

import pytest
from sqlalchemy import select

from kestrel.db.models import Opportunity, Pursuit, Source, StatusEvent
from kestrel.ingest.base import RawPosting
from kestrel.ingest.normalize import normalize_and_store
from kestrel.services import applied_import as svc
from kestrel.services import kestrel_ids
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


async def _seed_acme(sm) -> None:
    async with sm() as s:
        rev = await svc.review(s, TRACKER_CSV)
        await svc.commit(s, rev, {1: svc.RowChoice(include=True)})  # Acme, applied 2026-09-30


def _again(day: str, url: str = "") -> str:
    head = "Company,Role,Date,Status,Link"
    return f"{head}\nAcme Corp,VP Information Technology,{day},applied,{url}\n"


async def test_reimport_same_application_within_three_days_is_a_match(sm):
    await _seed_acme(sm)
    async with sm() as s:
        row = (await svc.review(s, _again("2026-10-03"))).rows[0]
    assert row.existing_pursuit_id is not None and row.possible_duplicate_id is None
    assert not row.default_include


async def test_reimport_four_to_fourteen_days_apart_asks_instead_of_matching(sm):
    await _seed_acme(sm)
    for day, gap in (("2026-10-04", 4), ("2026-10-14", 14)):
        async with sm() as s:
            rev = await svc.review(s, _again(day))
        row = rev.rows[0]
        assert row.existing_pursuit_id is None  # not silently the same application
        assert row.possible_duplicate_id is not None
        assert f"{gap} days apart" in row.possible_duplicate_label
        assert not row.default_include  # the person chooses; it is not ticked for them
    async with sm() as s:  # ticking it makes a new application, it does not update the old one
        rev = await svc.review(s, _again("2026-10-04"))
        result = await svc.commit(s, rev, {1: svc.RowChoice(include=True)})
    assert (result.imported, result.updated) == (1, 0)


async def test_reimport_more_than_fourteen_days_apart_is_a_reapplication(sm):
    await _seed_acme(sm)
    async with sm() as s:
        row = (await svc.review(s, _again("2026-10-15"))).rows[0]
    assert row.existing_pursuit_id is None and row.possible_duplicate_id is None
    assert row.default_include


async def test_reimport_same_url_is_a_match_whatever_the_date(sm):
    await _seed_acme(sm)
    async with sm() as s:
        row = (await svc.review(s, _again("2027-03-01", "https://acme.example/jobs/1"))).rows[0]
    assert row.existing_pursuit_id is not None and "same URL" in row.existing_label


async def test_reimport_with_an_unreadable_tracked_date_asks(sm):
    await _seed_acme(sm)
    async with sm() as s:
        p = (await s.execute(select(Pursuit))).scalars().one()
        p.applied_at = "sometime in the fall"
        await s.commit()
    async with sm() as s:
        row = (await svc.review(s, _again("2026-10-03"))).rows[0]
    assert row.existing_pursuit_id is None
    assert row.possible_duplicate_id is not None and "could not be read" in (
        row.possible_duplicate_label
    )


def test_the_import_page_names_a_possible_duplicate_and_does_not_tick_it(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    first = "Company,Role,Date,Status\nAcme Corp,VP Information Technology,2026-09-30,applied\n"
    cols = {"map_0": "company", "map_1": "title", "map_2": "date", "map_3": "status"}
    first_review = client.post("/settings/data/applied/review", data={"text": first, **cols})
    assert 'name="include_1"' in first_review.text
    normalized = (
        "company,role,date,status\nAcme Corp,VP Information Technology,2026-09-30,applied\n"
    )
    client.post("/settings/data/applied/add", data={"text": normalized, "include_1": "on"})
    again = "Company,Role,Date,Status\nAcme Corp,VP Information Technology,2026-10-06,applied\n"
    page = client.post("/settings/data/applied/review", data={"text": again, **cols}).text
    assert "Possible duplicate" in page
    assert 'name="include_1"' in page
    assert "checked" not in page.split('name="include_1"')[1].split(">")[0]


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
    assert 'data-testid="st-imp"' in page and "Import past applications" in page
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
    assert "VP Information Technology" in board
    # The re-post choice lives in each card's panel (D-UI-100).
    ids = re.findall(r'class="ap-row"[^>]*data-id="(\d+)"', board)
    assert any(
        'name="allow" value="on" checked' in client.get(f"/pursuits/{i}/panel").text for i in ids
    )
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
    pid = int(re.search(r'class="ap-row"[^>]*data-id="(\d+)"', board).group(1))
    panel = lambda: client.get(f"/pursuits/{pid}/panel").text  # noqa: E731
    assert 'name="allow" value="on" checked' not in panel()
    assert f"/pursuits/{pid}/reposts" in panel()
    assert client.post(f"/pursuits/{pid}/reposts", data={"allow": "on"}).status_code == 204
    assert 'name="allow" value="on" checked' in panel()
    assert client.post(f"/pursuits/{pid}/reposts", data={}).status_code == 204
    assert 'name="allow" value="on" checked' not in panel()
    assert client.post("/pursuits/999999/reposts", data={"allow": "on"}).status_code == 404


# ------------------------------------------------------- safe re-import (#147, SPEC.md 5.6)
HEAD = "Company,Role,Date,Status,Source,Notes\n"


def _file(*rows: str) -> str:
    return HEAD + "\n".join(rows) + "\n"


async def _seed(sm, text: str, ticks: tuple[int, ...] = (1,)) -> None:
    async with sm() as s:
        rev = await svc.review(s, text)
        await svc.commit(s, rev, {i: svc.RowChoice(include=True) for i in ticks})


async def _pursuits(sm) -> list[Pursuit]:
    async with sm() as s:
        return list((await s.execute(select(Pursuit).order_by(Pursuit.id))).scalars())


async def test_an_approximate_date_matches_by_month_and_never_overwrites_an_exact_date(sm):
    await _seed(sm, _file("Acme Corp,Planner,2026-09-19,applied,Board,"))
    again = _file("Acme Corp,Planner,2026-09-01,interviewing,Board,approx date")
    async with sm() as s:
        rev = await svc.review(s, again)
        row = rev.rows[0]
        assert row.date_approx and row.existing_pursuit_id is not None
        assert "same month" in row.existing_label
        await svc.commit(s, rev, {1: svc.RowChoice(include=True)})
    (p,) = await _pursuits(sm)
    assert p.applied_at == "2026-09-19T00:00:00+00:00"  # the exact date stays
    assert p.status == "interviewing"
    other_month = _file("Acme Corp,Planner,2026-12-01,applied,Board,approx date")
    async with sm() as s:
        row = (await svc.review(s, other_month)).rows[0]
    assert row.existing_pursuit_id is None and row.reapplication_of is not None
    assert row.bucket == "reapply"


async def test_each_tracked_application_pairs_with_one_file_row_and_source_breaks_the_tie(sm):
    await _seed(sm, _file("Acme Corp,Planner,2026-09-30,applied,LinkedIn,"))
    two = _file(
        "Acme Corp,Planner,2026-09-30,applied,Company site,",
        "Acme Corp,Planner,2026-09-30,screening,LinkedIn,",
    )
    async with sm() as s:
        rev = await svc.review(s, two)
        site, board = rev.rows
        assert board.existing_pursuit_id is not None  # the row from the same source pairs
        assert site.existing_pursuit_id is None and site.same_slot_other_source
        assert site.bucket == "new"
        result = await svc.commit(s, rev, {1: svc.RowChoice(True), 2: svc.RowChoice(True)})
    assert (result.imported, result.updated) == (1, 1)
    ps = await _pursuits(sm)
    assert len(ps) == 2 and sorted(p.status for p in ps) == ["applied", "screening"]


async def test_two_file_rows_for_one_new_role_from_two_sources_make_two_applications(sm):
    two = _file(
        "Acme Corp,Planner,2026-09-30,applied,LinkedIn,",
        "Acme Corp,Planner,2026-09-30,applied,Company site,",
    )
    async with sm() as s:
        rev = await svc.review(s, two)
        result = await svc.commit(s, rev, {1: svc.RowChoice(True), 2: svc.RowChoice(True)})
    assert (result.imported, result.updated) == (2, 0)
    assert len(await _pursuits(sm)) == 2


async def test_an_identical_row_collapses_into_the_first_with_a_note(sm):
    dup = _file(
        "Acme Corp,Planner,2026-09-30,applied,LinkedIn,",
        "Acme Corp,Planner,2026-09-30,applied,LinkedIn,",
    )
    async with sm() as s:
        rev = await svc.review(s, dup)
        first, second = rev.rows
        assert second.duplicate_of == 1 and second.bucket == "repeat"
        assert not second.importable and not second.default_include and first.default_include
        result = await svc.commit(s, rev, {1: svc.RowChoice(True), 2: svc.RowChoice(True)})
    assert result.imported == 1 and len(await _pursuits(sm)) == 1


@pytest.mark.parametrize(
    ("kestrel", "file_status", "file_wins", "expected"),
    [
        ("interviewing", "applied", False, "interviewing"),  # Kestrel ahead: kept
        ("applied", "interviewing", False, "interviewing"),  # Kestrel behind: advanced
        ("rejected", "interviewing", False, "rejected"),  # closed is final
        ("withdrawn", "applied", False, "withdrawn"),
        ("applied", "rejected", False, "rejected"),  # the file learned of an outcome
        ("interviewing", "applied", True, "applied"),  # the person said the file wins
        ("rejected", "applied", True, "applied"),
    ],
)
async def test_a_reimport_never_moves_progress_backwards_unless_the_file_wins(
    sm, kestrel, file_status, file_wins, expected
):
    await _seed(sm, _file(f"Acme Corp,Planner,2026-09-30,{kestrel},Board,"))
    async with sm() as s:
        rev = await svc.review(s, _file(f"Acme Corp,Planner,2026-09-30,{file_status},Board,"))
        result = await svc.commit(s, rev, {1: svc.RowChoice(True)}, file_wins=file_wins)
    (p,) = await _pursuits(sm)
    assert p.status == expected
    held = expected != file_status
    assert result.kept_newer == (1 if held else 0)


async def test_an_application_missing_from_the_file_is_left_alone(sm):
    await _seed(sm, _file("Acme Corp,Planner,2026-09-30,applied,Board,",
                          "Birch Co,Analyst,2026-09-01,screening,Board,"), ticks=(1, 2))
    async with sm() as s:
        rev = await svc.review(s, _file("Acme Corp,Planner,2026-09-30,applied,Board,"))
        await svc.commit(s, rev, {1: svc.RowChoice(True)})
    ps = await _pursuits(sm)
    assert len(ps) == 2 and "screening" in {p.status for p in ps}


async def test_review_sorts_rows_into_buckets_and_counts_what_is_in_sync(sm):
    await _seed(sm, _file(
        "Acme Corp,Planner,2026-09-30,interviewing,Board,",
        "Birch Co,Analyst,2026-09-01,applied,Board,",
        "Cedar Ltd,Buyer,2026-08-01,applied,Board,",
    ), ticks=(1, 2, 3))
    text = _file(
        "Acme Corp,Planner,2026-09-30,applied,Board,",  # Kestrel is ahead: nothing to change
        "Birch Co,Analyst,2026-09-01,screening,Board,",  # the file is ahead: differs
        "Cedar Ltd,Buyer,2026-08-08,applied,Board,",  # 7 days: your call
        "Cedar Ltd,Buyer,2026-11-01,applied,Board,",  # months later: a re-application
        "Dune Inc,Clerk,2026-10-01,applied,Board,",  # not found: new
        "Self - Resume,Refresh,2026-10-02,completed,Board,activity type: cv",
    )
    async with sm() as s:
        rev = await svc.review(s, text)
    assert [r.bucket for r in rev.rows] == [
        "in_sync", "differs", "ask", "reapply", "new", "activity"
    ]
    assert rev.rows[1].status_change == "screening" and rev.rows[1].existing_status == "applied"
    assert not rev.rows[1].default_include  # a matched row is never ticked for the person


async def test_unseen_notes_make_a_matched_row_differ(sm):
    await _seed(sm, _file("Acme Corp,Planner,2026-09-30,applied,Board,first note"))
    async with sm() as s:
        rev = await svc.review(s, _file("Acme Corp,Planner,2026-09-30,applied,Board,new note"))
    assert rev.rows[0].bucket == "differs" and rev.rows[0].notes_new


# ------------------------------------------------ the page: one-action undo, all or nothing
def _db_rows(sql: str) -> list[tuple]:
    import sqlite3

    from kestrel import paths

    con = sqlite3.connect(paths.db_path())
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _add(client, csv_text: str, **form):
    cols = {"map_0": "company", "map_1": "title", "map_2": "date", "map_3": "status",
            "map_4": "source"}
    norm = client.post("/settings/data/applied/review", data={"text": csv_text, **cols})
    assert norm.status_code == 200
    plain = "company,role,date,status,source\n" + csv_text.split("\n", 1)[1]
    return client.post("/settings/data/applied/add", data={"text": plain, **form}), plain


def _undo_url(response) -> str:
    import json

    return json.loads(response.headers["HX-Trigger"])["k-toast"]["undoUrl"]


def test_one_undo_puts_back_every_update_a_reimport_made_and_removes_what_it_added(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    first = "Company,Role,Date,Status,Source\nAcme Corp,Planner,2026-09-30,applied,Board\n"
    _add(client, first, include_1="on")
    before = _db_rows("select id, status, applied_at, notes_md from pursuit")
    events_before = _db_rows("select count(*) from status_event")
    second = (
        "Company,Role,Date,Status,Source\n"
        "Acme Corp,Planner,2026-09-30,interviewing,Board\n"
        "Birch Co,Analyst,2026-10-02,applied,Board\n"
    )
    resp, _ = _add(client, second, include_1="on", include_2="on")
    assert [r[1] for r in _db_rows("select id, status from pursuit order by id")] == [
        "interviewing", "applied"]
    undo = client.post(_undo_url(resp))
    assert undo.status_code == 200
    assert _db_rows("select id, status, applied_at, notes_md from pursuit") == before
    assert _db_rows("select count(*) from status_event") == events_before
    assert "can no longer be undone" in client.post(_undo_url(resp)).text  # used up


def test_the_file_wins_switch_is_opt_in_through_the_form(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    first = "Company,Role,Date,Status,Source\nAcme Corp,Planner,2026-09-30,interviewing,Board\n"
    _add(client, first, include_1="on")
    behind = "Company,Role,Date,Status,Source\nAcme Corp,Planner,2026-09-30,applied,Board\n"
    resp, _ = _add(client, behind, include_1="on")
    assert _db_rows("select status from pursuit") == [("interviewing",)]
    assert "kept at a further stage" in json_message(resp)
    _add(client, behind, include_1="on", file_wins="on")
    assert _db_rows("select status from pursuit") == [("applied",)]


def json_message(response) -> str:
    import json

    return json.loads(response.headers["HX-Trigger"])["k-toast"]["message"]


def test_a_failed_import_writes_nothing_and_sends_nothing(monkeypatch):
    import socket

    from sqlalchemy.exc import OperationalError

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    _add(client, "Company,Role,Date,Status,Source\nAcme Corp,Planner,2026-09-30,applied,Board\n",
         include_1="on")
    snapshot = (_db_rows("select * from pursuit"), _db_rows("select count(*) from opportunity"),
                _db_rows("select count(*) from status_event"))
    real = svc.normalize_and_store
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OperationalError("insert", {}, Exception("disk full"))
        return await real(*a, **k)

    attempts: list[object] = []
    real_connect = socket.socket.connect

    def watch(self, address):
        if isinstance(address, tuple) and address and address[0] not in ("127.0.0.1", "::1"):
            attempts.append(address)
        return real_connect(self, address)

    monkeypatch.setattr(svc, "normalize_and_store", flaky)
    monkeypatch.setattr(socket.socket, "connect", watch)
    text = ("Company,Role,Date,Status,Source\nAcme Corp,Planner,2026-09-30,interviewing,Board\n"
            "Birch Co,Analyst,2026-10-02,applied,Board\nCedar Ltd,Buyer,2026-10-03,applied,Board\n")
    resp, _ = _add(client, text, include_1="on", include_2="on", include_3="on")
    assert "Nothing was added" in resp.text
    assert (_db_rows("select * from pursuit"), _db_rows("select count(*) from opportunity"),
            _db_rows("select count(*) from status_event")) == snapshot
    assert attempts == []


# --------------------------------------------------- the Kestrel ID column (M2, D-IMPORT-9)
async def _issue_ids(sm) -> dict[int, str]:
    async with sm() as s:
        ids = await kestrel_ids.ids_by_pursuit(s, create=True)
        await s.commit()
    return ids


def test_a_kestrel_id_is_stable_opaque_and_not_reused_with_a_row_number():
    a = kestrel_ids.make_id("salt-one", 7, "2026-09-30T10:00:00+00:00")
    assert a == kestrel_ids.make_id("salt-one", 7, "2026-09-30T10:00:00+00:00")
    assert a != kestrel_ids.make_id("salt-two", 7, "2026-09-30T10:00:00+00:00")  # per install
    assert a != kestrel_ids.make_id("salt-one", 7, "2026-10-01T09:00:00+00:00")  # row reused
    assert re.fullmatch(r"K-[A-Z2-7]{5}-[A-Z2-7]{5}", a)
    assert kestrel_ids.normalize(a.lower().replace("-", " ")) == kestrel_ids.normalize(a) != ""
    assert kestrel_ids.normalize("12345") == "" and kestrel_ids.normalize(None) == ""


async def test_a_review_writes_no_salt_and_finds_no_id_before_one_was_handed_out(sm):
    await _seed(sm, _file("Acme Corp,Planner,2026-09-30,applied,Board,"))
    async with sm() as s:
        await svc.review(s, "Company,Role,Date,Status,Kestrel ID\nAcme Corp,Planner,2026-09-30,"
                            "applied,K-AAAAA-BBBBB\n")
        assert await kestrel_ids.get_salt(s) is None  # nothing was written by reviewing


async def test_a_row_with_a_known_id_matches_it_even_after_the_words_changed(sm):
    await _seed(sm, _file("Acme Corp,Planner,2026-09-30,applied,Board,"))
    (pid, kid), = (await _issue_ids(sm)).items()
    text = ("Company,Role,Date,Status,Kestrel ID\n"
            f"Acme Corporation Ltd,Senior Planner II,2027-01-15,screening,{kid.lower()}\n")
    async with sm() as s:
        rev = await svc.review(s, text)
        row = rev.rows[0]
        assert row.existing_pursuit_id == pid and row.kid_state == "matched"
        assert row.existing_label == "same Kestrel ID" and row.bucket == "differs"
        result = await svc.commit(s, rev, {1: svc.RowChoice(True)})
    assert (result.imported, result.updated) == (0, 1)
    (p,) = await _pursuits(sm)
    assert p.status == "screening" and p.applied_at == "2026-09-30T00:00:00+00:00"


async def test_an_unknown_id_is_a_new_application_with_a_warning_not_a_guess_by_words(sm):
    await _seed(sm, _file("Acme Corp,Planner,2026-09-30,applied,Board,"))
    await _issue_ids(sm)
    text = ("Company,Role,Date,Status,Kestrel ID\n"
            "Acme Corp,Planner,2026-09-30,applied,K-ZZZZZ-ZZZZZ\n"
            "Birch Co,Analyst,2026-09-01,applied,not-an-id\n")
    async with sm() as s:
        rev = await svc.review(s, text)
    first, second = rev.rows
    assert first.existing_pursuit_id is None and first.kid_state == "unknown"
    assert first.bucket == "new" and "No application has this Kestrel ID" in first.warning
    assert second.kid_state == "unknown" and "not a Kestrel ID" in second.warning


async def test_a_blank_or_removed_id_falls_back_to_matching_by_role_and_date(sm):
    await _seed(sm, _file("Acme Corp,Planner,2026-09-30,applied,Board,"))
    await _issue_ids(sm)
    text = "Company,Role,Date,Status,Kestrel ID\nAcme Corp,Planner,2026-09-30,applied,\n"
    async with sm() as s:
        row = (await svc.review(s, text)).rows[0]
    assert row.existing_pursuit_id is not None and row.kid_state == ""


async def test_the_same_id_on_two_rows_holds_the_second_until_the_file_is_fixed(sm):
    await _seed(sm, _file("Acme Corp,Planner,2026-09-30,applied,Board,"))
    (_, kid), = (await _issue_ids(sm)).items()
    text = ("Company,Role,Date,Status,Kestrel ID\n"
            f"Acme Corp,Planner,2026-09-30,applied,{kid}\n"
            f"Birch Co,Analyst,2026-10-02,applied,{kid}\n")
    async with sm() as s:
        rev = await svc.review(s, text)
        first, second = rev.rows
        assert first.kid_state == "matched" and second.kid_state == "repeat"
        assert second.bucket == "ask" and not second.importable and "row 1" in second.warning
        result = await svc.commit(s, rev, {1: svc.RowChoice(True), 2: svc.RowChoice(True)})
    assert result.imported == 0 and len(await _pursuits(sm)) == 1


def test_the_copy_adds_the_id_column_keeps_every_other_cell_and_leaves_the_input_alone():
    original = (
        "Company,Role,Date,Status,My tag\nAcme,Planner,2026-09-30,applied,hot\n"
        "Birch,Clerk,2026-10-01,applied,\n"
    )
    kept = str(original)
    out = svc.copy_with_ids(original, {1: "K-AAAAA-BBBBB"})
    assert original == kept
    lines = out.strip().splitlines()
    assert lines[0] == "Company,Role,Date,Status,My tag,Kestrel ID"
    assert lines[1] == "Acme,Planner,2026-09-30,applied,hot,K-AAAAA-BBBBB"
    assert lines[2] == "Birch,Clerk,2026-10-01,applied,,"  # no application: the cell stays empty
    again = svc.copy_with_ids(out, {2: "K-CCCCC-DDDDD"})  # a present column is filled, not doubled
    assert again.strip().splitlines()[0].count("Kestrel ID") == 1
    assert "K-AAAAA-BBBBB" in again and "K-CCCCC-DDDDD" in again


def test_the_copy_with_ids_defuses_formulas_in_every_cell_it_carries_over():
    original = (
        "Company,Role,Date,Status,My tag\n"
        "=HYPERLINK(http://x.example),Planner,2026-09-30,applied,-2+3\n"
    )
    out = svc.copy_with_ids(original, {1: "K-AAAAA-BBBBB"})
    _h, rows, _ = svc.parse_table(out)
    assert rows[0][0] == "'=HYPERLINK(http://x.example)" and rows[0][4] == "'-2+3"
    assert rows[0][5] == "K-AAAAA-BBBBB"
    assert svc.copy_with_ids(out, {})  # a copy of a copy does not stack quotes
    assert "''" not in svc.copy_with_ids(out, {})


def _import_file(client, text: str, **ticks):
    """The page's own steps for a file with any columns: read, review with the columns as read,
    then Add. Returns the Add response."""
    headers, _rows, _ = svc.parse_table(text)
    found = svc.map_columns(headers)
    by_header = {h: c for c, h in found.items()}
    cols = {f"map_{i}": by_header.get(h, "") for i, h in enumerate(headers)}
    review = client.post("/settings/data/applied/review", data={"text": text, **cols})
    assert review.status_code == 200
    norm = re.search(r'name="text" value="([^"]*)"', review.text)
    import html as _html

    norm_text = _html.unescape(norm.group(1))
    return client.post(
        "/settings/data/applied/add",
        data={"text": norm_text, "orig": text, **{f"include_{k}": "on" for k in ticks}},
    )


def test_round_trip_export_add_a_row_by_hand_import_creates_only_the_new_row(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    first = ("Company,Role,Date,Status,Source\nAcme Corp,Planner,2026-09-30,applied,Board\n"
             "Birch Co,Analyst,2026-10-02,screening,Board\n")
    _import_file(client, first, **{"1": 1, "2": 1})
    exported = client.get("/analytics/export/applications.csv").text.lstrip("﻿")
    assert client.get("/analytics/export/applications.csv").text.lstrip("﻿") == exported
    rows = list(csv.reader(io.StringIO(exported)))
    assert rows[0][-1] == "Kestrel ID" and all(r[-1] for r in rows[1:])
    # Edit one title by hand, remove another row's ID, and add a brand new row.
    rows[1][0] = "Planner (senior)"
    rows[2][-1] = ""
    rows.append(["Buyer", "Fernhill Mills", "applied", "2026-10-09", "", "", "", ""])
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    done = _import_file(client, out.getvalue(), **{"1": 1, "2": 1, "3": 1})
    assert done.status_code == 200
    assert _db_rows("select count(*) from pursuit") == [(3,)]  # only the new row was created


def test_a_key_holder_sees_the_copy_download_drawn_disabled(monkeypatch):
    """D-SEC-9 (Fable C5): the copy link carries the route's own test, so a key holder gets a
    disabled button and the note, not a link that answers 403."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from kestrel.web.deps import templates

    monkeypatch.setitem(templates().env.globals, "is_local", lambda request: False)
    client = _fresh_app()
    done = _import_file(client, "Company,Role,Date,Status\nAcme Corp,Planner,2026-09-30,applied\n",
                        **{"1": 1})
    assert "/settings/data/applied/copy/" not in done.text
    assert 'data-testid="local-only-download"' in done.text
    assert "Only this computer can download this." in done.text


def test_the_copy_with_ids_is_offered_after_an_import_and_is_gone_after_undo(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    text = ("Company,Role,Date,Status,My tag\nAcme Corp,Planner,2026-09-30,applied,hot\n"
            "Self,Resume refresh,2026-10-02,completed,activity type: cv\n")
    done = _import_file(client, text, **{"1": 1})
    assert 'data-testid="st-imp-copy"' in done.text and "was not changed" in done.text
    link = re.search(r'href="(/settings/data/applied/copy/[^"]+)"', done.text).group(1)
    got = client.get(link)
    assert got.status_code == 200 and "attachment" in got.headers["content-disposition"]
    assert got.headers["cache-control"] == "no-store"
    lines = got.text.lstrip("﻿").strip().splitlines()
    assert lines[0] == "Company,Role,Date,Status,My tag,Kestrel ID"
    assert re.fullmatch(r"Acme Corp,Planner,2026-09-30,applied,hot,K-[A-Z2-7]{5}-[A-Z2-7]{5}",
                        lines[1])
    assert lines[2].startswith("Self,Resume refresh") and lines[2].endswith(",")  # skipped row
    assert client.post(_undo_url(done)).status_code == 200
    assert client.get(link).status_code == 404  # its IDs named applications that are gone


def test_the_copy_of_a_file_with_a_non_latin_name_downloads_with_a_safe_header(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    text = "Company,Role,Date,Status\nAcme Corp,Planner,2026-09-30,applied\n"
    cols = {f"map_{i}": c for i, c in enumerate(["company", "title", "date", "status"])}
    review = client.post("/settings/data/applied/review", data={"text": text, **cols})
    norm = html_lib.unescape(re.search(r'name="text" value="([^"]*)"', review.text).group(1))
    done = client.post("/settings/data/applied/add", data={
        "text": norm, "orig": text, "file_name": "\u5fdc\u52df.csv", "include_1": "on"})
    link = re.search(r'href="(/settings/data/applied/copy/[^"]+)"', done.text).group(1)
    got = client.get(link)
    assert got.status_code == 200
    cd = got.headers["content-disposition"]
    assert cd.startswith("attachment; filename=\"") and cd.isascii()
    assert "filename*=UTF-8''%E5%BF%9C%E5%8B%9F-with-ids.csv" in cd


def _db_exec(sql: str) -> None:
    import sqlite3

    from kestrel import paths

    con = sqlite3.connect(paths.db_path())
    try:
        con.execute(sql)
        con.commit()
    finally:
        con.close()


_STATE_SQL = (
    "select id, status, applied_at, notes_md, last_activity_at from pursuit order by id",
    "select count(*) from opportunity",
    "select count(*) from status_event",
)


def _state() -> list:
    return [_db_rows(q) for q in _STATE_SQL]


def test_a_row_shown_as_a_reapplication_makes_a_new_application_and_one_undo_removes_it(
    monkeypatch,
):
    """INVARIANT: one Undo restores exactly what the import changed; a row the review showed as
    new never silently updates another application."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    head = "Company,Role,Date,Status,Source\n"
    _import_file(client, head + "Acme Corp,Planner,2026-09-01,applied,Board\n", **{"1": 1})
    # The person corrects the date in Kestrel; the old file now reads as a later application.
    _db_exec("update pursuit set applied_at = '2026-10-20T00:00:00+00:00'")
    before = _state()
    done = _import_file(
        client, head + "Acme Corp,Planner,2026-09-01,interviewing,Board\n", **{"1": 1})
    assert done.status_code == 200
    assert _db_rows("select count(*) from pursuit") == [(2,)]
    assert _db_rows("select status, applied_at from pursuit where id = 1") == [
        ("applied", "2026-10-20T00:00:00+00:00")]
    assert client.post(_undo_url(done)).status_code == 200
    assert _state() == before


def test_a_mistyped_kestrel_id_makes_a_new_application_and_one_undo_removes_it(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    head = "Company,Role,Date,Status,Source,Kestrel ID\n"
    _import_file(client, head + "Acme Corp,Planner,2026-09-01,applied,Board,\n", **{"1": 1})
    before = _state()
    done = _import_file(
        client, head + "Acme Corp,Planner,2026-09-01,interviewing,Board,K-ZZZZZ-ZZZZZ\n",
        **{"1": 1})
    assert done.status_code == 200
    assert _db_rows("select count(*) from pursuit") == [(2,)]
    assert _db_rows("select status from pursuit where id = 1") == [("applied",)]
    assert client.post(_undo_url(done)).status_code == 200
    assert _state() == before


async def test_an_id_match_needs_no_date_so_an_undated_pursuit_round_trips(sm):
    await _seed(sm, _file("Acme Corp,Planner,2026-09-30,applied,Board,"))
    (pid, kid), = (await _issue_ids(sm)).items()
    async with sm() as s:
        (await s.get(Pursuit, pid)).applied_at = None
        await s.commit()
    text = ("Company,Role,Date,Status,Kestrel ID\n"
            f"Acme Corp,Planner,,interviewing,{kid}\n"
            f"Acme Corp,Planner,,applied,K-ZZZZZ-ZZZZZ\n")
    async with sm() as s:
        rev = await svc.review(s, text)
        row, unknown = rev.rows
        assert row.existing_pursuit_id == pid and row.kid_state == "matched"
        assert not row.problems and row.bucket != "ask"
        assert unknown.problems  # an unknown ID still needs a date to be a new application
        result = await svc.commit(s, rev, {1: svc.RowChoice(True), 2: svc.RowChoice(True)})
    assert (result.imported, result.updated) == (0, 1)
    (p,) = await _pursuits(sm)
    assert p.status == "interviewing" and p.applied_at is None


# ---------------------------------------------------------------- padded rows (#147 acceptance)
def _padded_tracker(rows: int, width: int = 13_029) -> str:
    """A notes-app table plugin pads every row with spaces to a fixed width; invented rows."""
    head = (
        "| Company | Role | Date | Source | Status | Notes |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
    )
    lines = []
    for i in range(rows):
        day = 1 + i % 28
        row = f"| Firm{i:03d} Ltd | Analyst {i:03d} | 2026-09-{day:02d} | Board | applied | n |"
        lines.append(row + " " * (width - len(row) - 2) + "|")
    return head + "\n".join(lines) + "\n"


def test_a_padded_13kb_per_row_tracker_parses_within_the_existing_limits():
    text = _padded_tracker(348)
    assert len(text) > 4_000_000 and len(text.encode()) < svc.MAX_TEXT_BYTES
    rev = svc.parse_rows(text)
    assert not rev.errors and len(rev.rows) == 348
    assert rev.rows[0].company == "Firm000 Ltd" and rev.rows[347].title == "Analyst 347"


def test_a_padded_tracker_reviews_through_the_form_and_an_oversize_one_is_refused(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    review = client.post("/settings/applied/review", data={"text": _padded_tracker(348)}).text
    assert review.count('data-testid="applied-row"') == 348
    # Over the cap (25 MB) it is refused with a plain line, not read.
    huge = "| a |\n| - |\n" + "| " + "x" * (svc.MAX_TEXT_BYTES + 10) + " |\n"
    refused = client.post("/settings/applied/review", data={"text": huge}).text
    assert "larger than 25 MB" in refused and 'data-testid="applied-row"' not in refused


def test_the_copy_puts_each_id_on_its_own_row_when_a_row_is_blank_in_every_mapped_column(
    monkeypatch,
):
    """The review drops a row that is blank in every column the person mapped; the copy numbers
    rows from their own file. The memo-only row must not shift the IDs of the rows after it."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    text = ("Company,Role,Applied,Status,Memo\n"
            "Alpha Co,Engineer,2026-01-02,applied,\n"
            ",,,,week two starts here\n"
            "Beta Co,Analyst,2026-01-09,applied,\n"
            "Gamma Co,Planner,2026-01-10,rejected,\n")
    cols = {"map_0": "company", "map_1": "title", "map_2": "date", "map_3": "status", "map_4": ""}
    review = client.post("/settings/data/applied/review", data={"text": text, **cols})
    norm = html_lib.unescape(re.search(r'name="text" value="([^"]*)"', review.text).group(1))
    done = client.post("/settings/data/applied/add", data={
        "text": norm, "orig": text, "include_1": "on", "include_2": "on", "include_3": "on"})
    link = re.search(r'href="(/settings/data/applied/copy/[^"]+)"', done.text).group(1)
    rows = list(csv.reader(io.StringIO(client.get(link).text.lstrip("﻿"))))
    by_company = {r[0]: r[-1] for r in rows[1:]}
    assert by_company[""] == ""  # the memo row is not an application
    assert all(by_company[c].startswith("K-") for c in ("Alpha Co", "Beta Co", "Gamma Co"))
    assert len({by_company[c] for c in ("Alpha Co", "Beta Co", "Gamma Co")}) == 3


def test_a_formula_safe_copy_reads_back_as_the_original_text():
    original = ('Company,Role,Date,Status,Notes\n'
                '@Home Inc,Planner,2026-09-30,applied,"- called recruiter\n- sent thanks"\n'
                'Birch,Clerk,2026-10-01,applied,+1 referral\n')
    copy = svc.copy_with_ids(original, {1: "K-AAAAA-BBBBB"})
    assert "'@Home Inc" in copy  # still defused for the spreadsheet
    back = svc.parse_rows(copy)
    assert [r.company for r in back.rows] == ["@Home Inc", "Birch"]
    assert back.rows[0].notes == "- called recruiter\n- sent thanks"
    assert back.rows[1].notes == "+1 referral"


def test_the_copy_keeps_a_blank_line_inside_a_cell_and_an_overflow_cell():
    original = ('Company,Role,Date,Status,Notes\n'
                'Acme,Planner,2026-09-30,applied,"para one\n\npara two"\n'
                'Birch,Clerk,2026-10-01,applied,called,then emailed\n')
    copy = svc.copy_with_ids(original, {1: "K-AAAAA-BBBBB", 2: "K-CCCCC-DDDDD"})
    _h, rows, _ = svc.parse_table(copy)
    assert rows[0][4] == "para one\n\npara two"
    assert rows[1][4] == "called" and rows[1][5] == "then emailed"
    assert "K-CCCCC-DDDDD" in rows[1] and rows[1].index("K-CCCCC-DDDDD") > 5


def test_a_copy_older_than_a_day_is_removed_not_served(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    text = "Company,Role,Date,Status\nAcme Corp,Planner,2026-09-30,applied\n"
    done = _import_file(client, text, **{"1": 1})
    link = re.search(r'href="(/settings/data/applied/copy/[^"]+)"', done.text).group(1)
    assert client.get(link).status_code == 200
    _db_exec("update setting set updated_at = '2020-01-01T00:00:00+00:00' "
             "where key like 'applied.copy.%'")
    assert client.get(link).status_code == 404
    assert _db_rows("select count(*) from setting where key like 'applied.copy.%'") == [(0,)]


def test_the_copy_row_is_never_read_as_configuration():
    from kestrel.services.setting_overrides import config_rows_select

    # excluded in SQL, so the file's megabytes are never even fetched on a config read
    assert "applied.copy." in str(config_rows_select().compile(
        compile_kwargs={"literal_binds": True}))


def test_undo_still_works_after_background_work_checked_liveness_and_embedded_the_roles(
    monkeypatch,
):
    """The hourly liveness pass and the embed resweep touch the roles an import created while the
    Undo is on offer. Neither is the person's change, so neither may refuse the Undo or leave the
    hidden roles behind."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    text = "Company,Role,Date,Status\nAcme Corp,Planner,2026-09-30,applied\n"
    done = _import_file(client, text, **{"1": 1})
    assert _db_rows("select count(*) from opportunity") == [(1,)]
    _db_exec("update opportunity set liveness = 'unknown', "
             "liveness_checked_at = '2026-11-01T00:00:00+00:00', last_seen_at = '2026-11-01'")
    _db_exec("insert into opportunity_embedding (opportunity_id, model, vector, dims, text_hash) "
             "select id, 'm', x'00000000', 1, 'h' from opportunity")
    undo = client.post(_undo_url(done))
    assert undo.status_code == 200 and "no longer" not in undo.text
    assert _db_rows("select count(*) from pursuit") == [(0,)]
    assert _db_rows("select count(*) from opportunity") == [(0,)]
