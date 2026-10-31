from __future__ import annotations

import io
import json
import threading
import tracemalloc
import zipfile
from datetime import UTC, time
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.date_display import format_display
from kestrel.db.models import Contact, Org, Pursuit, ScrapeRun, Source, Task
from kestrel.pipeline.worker import Worker
from kestrel.queue.queue import TaskQueue
from kestrel.services import applied_import, xlsx_text
from kestrel.web.deps import templates
from kestrel.web.queries import org_context
from kestrel.web.routes_pages import _link
from tests.pipeline_fakes import FakeLLM
from tests.test_area_g_security import _Elements
from tests.test_hosted_mode import _fresh_app


def _shared_workbook(value: str, rows: int) -> bytes:
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/sharedStrings.xml", f'<sst xmlns="{namespace}"><si><t>{value}</t></si></sst>'
        )
        cells = '<row><c r="A1" t="s"><v>0</v></c></row>' * rows
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{namespace}"><sheetData>{cells}</sheetData></worksheet>',
        )
    return output.getvalue()


@pytest.mark.parametrize("value", ["x" * 50, '"' * 50, "é" * 50])
def test_shared_strings_respect_expanded_csv_byte_limit(monkeypatch, value):
    monkeypatch.setattr(xlsx_text, "MAX_OUTPUT_BYTES", 128, raising=False)
    with pytest.raises(xlsx_text.SpreadsheetError, match="too large"):
        xlsx_text.to_csv(_shared_workbook(value, 4))


def test_spreadsheet_output_limit_accepts_exact_boundary(monkeypatch):
    monkeypatch.setattr(xlsx_text, "MAX_OUTPUT_BYTES", 8, raising=False)
    assert xlsx_text.to_csv(_shared_workbook("é", 2)) == "é\r\né\r\n"


def test_padded_shared_strings_do_not_expand_before_output_check(monkeypatch):
    monkeypatch.setattr(xlsx_text, "MAX_OUTPUT_BYTES", 128 * 1024)
    original = _shared_workbook(" " + "x" * (64 * 1024), 2)
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
            target.writestr("xl/sharedStrings.xml", source.read("xl/sharedStrings.xml"))
            cells = "".join(
                f'<c r="{chr(65 + column // 26)}{chr(65 + column % 26)}1" t="s">'
                "<v>0</v></c>" for column in range(64)
            )
            target.writestr("xl/worksheets/sheet1.xml", (
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f"<sheetData><row>{cells}</row></sheetData></worksheet>"
            ))
    tracemalloc.start()
    try:
        with pytest.raises(xlsx_text.SpreadsheetError, match="too large"):
            xlsx_text.to_csv(output.getvalue())
        assert tracemalloc.get_traced_memory()[1] < 2 * 1024 * 1024
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,bad", "java\tscript:bad"])
def test_stored_posting_links_never_render_active_schemes(url):
    link = _link(url, None, False, {}, set(), "posting")
    rendered = templates().env.get_template("partials/role_tab_posting.html").render(
        posting_link=link, source_name="Imported", opp={"opp_type": "manual"}
    )
    parsed = _Elements()
    parsed.feed(rendered)
    assert not any(attrs.get("href") == url for _, attrs in parsed.elements)


def test_applied_import_drops_active_link_scheme():
    review = applied_import.parse_rows(
        "Company,Role,Date,Status,URL\nExample,Planner,2026-10-01,applied,javascript:alert(1)\n"
    )
    assert review.rows[0].url is None


@pytest.mark.parametrize("url", ["https://example.org/jobs/1", "HTTP://example.org/jobs/1"])
def test_http_links_are_preserved_and_confidential_links_stay_hidden(url):
    assert _link(url, None, False, {}, set(), "posting")["href"] == url
    assert _link(url, None, True, {}, {"example"}, "posting")["href"] is None


async def test_contact_link_sweep_rejects_stored_active_urls(sm):
    async with sm() as session:
        organization = Org(name="Example", normalized_name="example")
        session.add(organization)
        await session.flush()
        session.add(Contact(
            org_id=organization.id, name="Public contact", linkedin_url="javascript:alert(1)"
        ))
        await session.commit()
        context = await org_context(session, organization.id)
        assert context["contacts"][0]["linkedin_url"] is None


def test_manual_ingest_never_exposes_active_posting_url():
    client = _fresh_app()
    result = client.post("/api/opportunities/ingest", json={
        "url": "javascript:alert(1)", "text": "Planner", "org_name": "Example"
    })
    assert result.status_code == 200
    rendered = client.get(f"/opportunities/{result.json()['id']}").text
    parsed = _Elements()
    parsed.feed(rendered)
    assert not any(attrs.get("href") == "javascript:alert(1)" for _, attrs in parsed.elements)


@pytest.mark.parametrize("markdown", [False, True])
def test_applied_import_caps_rows_before_building_review(monkeypatch, markdown):
    monkeypatch.setattr(applied_import, "MAX_ROWS", 2, raising=False)
    text = ("|Company|Role|Date|Status|\n|---|---|---|---|\n" +
            "|Example|Planner|2026-10-01|applied|\n" * 3) if markdown else (
        "Company,Role,Date,Status\n" + "Example,Planner,2026-10-01,applied\n" * 3
    )
    review = applied_import.parse_rows(text)
    assert not review.rows
    assert "rows" in review.errors[0]


def test_applied_import_caps_utf8_text_bytes(monkeypatch):
    monkeypatch.setattr(applied_import, "MAX_TEXT_BYTES", 64, raising=False)
    review = applied_import.parse_rows(
        "Company,Role,Date,Status\n" + "é" * 20 + ",Planner,2026-10-01,applied\n"
    )
    assert not review.rows
    assert "large" in review.errors[0]


@pytest.mark.parametrize("path", [
    "/settings/data/applied/review", "/settings/data/applied/add", "/settings/applied/import"
])
def test_reposted_tracker_cannot_bypass_row_limit(path, monkeypatch):
    from tests.test_run_mode import _app_db

    monkeypatch.setattr(applied_import, "MAX_ROWS", 1)
    client = _fresh_app()
    text = "Company,Role,Date,Status\n" + "Example,Planner,2026-10-01,applied\n" * 2
    response = client.post(path, data={
        "text": text, "include_1": "on", "include_2": "on",
        "map_0": "company", "map_1": "title", "map_2": "date", "map_3": "status",
    })
    assert response.status_code == 200
    assert "rows" in response.text or "could not be read" in response.text

    async def pursuits(session):
        return list(await session.scalars(select(Pursuit)))

    assert not _app_db(pursuits)


def test_bounded_csv_preserves_multiline_fields_and_exact_row_limit(monkeypatch):
    monkeypatch.setattr(applied_import, "MAX_ROWS", 1)
    review = applied_import.parse_rows(
        'Company,Role,Date,Status,Notes\nExample,Planner,2026-10-01,applied,"first\nsecond"\n'
    )
    assert not review.errors and len(review.rows) == 1
    assert review.rows[0].notes == "first\nsecond"


async def test_applied_review_parses_outside_event_loop(sm, monkeypatch):
    request_thread = threading.get_ident()
    parser_threads = []
    original = applied_import.parse_rows

    def parse(text):
        parser_threads.append(threading.get_ident())
        return original(text)

    monkeypatch.setattr(applied_import, "parse_rows", parse)
    async with sm() as session:
        review = await applied_import.review(
            session, "Company,Role,Date,Status\nExample,Planner,2026-10-01,applied\n"
        )
    assert len(review.rows) == 1
    assert parser_threads and all(ident != request_thread for ident in parser_threads)


async def test_applied_review_matches_outside_event_loop(sm, monkeypatch):
    request_thread = threading.get_ident()
    match_threads = []
    original = applied_import._find_tracked

    def match(row, existing):
        match_threads.append(threading.get_ident())
        return original(row, existing)

    monkeypatch.setattr(applied_import, "_find_tracked", match)
    async with sm() as session:
        review = await applied_import.review(
            session, "Company,Role,Date,Status\nExample,Planner,2026-10-01,applied\n"
        )
    assert len(review.rows) == 1
    assert match_threads and all(ident != request_thread for ident in match_threads)


async def test_applied_read_parses_outside_event_loop(monkeypatch):
    from kestrel.web.pages import settings_data

    request_thread = threading.get_ident()
    parser_threads = []
    original = applied_import.parse_table

    async def form(**_limits):
        return {"sample": "1"}

    def parse(text):
        parser_threads.append(threading.get_ident())
        return original(text)

    monkeypatch.setattr(applied_import, "parse_table", parse)
    monkeypatch.setattr(settings_data, "_imp_card", lambda request, context: context)
    response = await settings_data.applied_read(SimpleNamespace(form=form))
    assert response["n_rows"] == 5
    assert parser_threads and all(ident != request_thread for ident in parser_threads)


@pytest.mark.parametrize("surface", ["pp_delete", "update_details", "st_rules", "tailor_guard"])
def test_hx_values_preserve_untrusted_text_without_extra_parameters(surface):
    hostile = 'value","confirmed":"1'
    contexts = {
        "pp_delete": {"story": {"id": 1, "title": "Story"}, "from": "bank", "opp": hostile},
        "update_details": {"version": hostile},
        "st_rules": {"track": "employment", "rule_rows": [{
            "def": {"key": hostile, "name": "Rule"}, "of_n": 0, "on": True, "n": 0,
        }]},
        "tailor_guard": {"ctx": {"render": {"id": 1}, "guard": {
            "over": True, "limit": 1, "pages": 2,
            "optional": [{"key": hostile, "label": "Line"}],
        }}},
    }
    environment = templates().env
    template = environment.from_string(
        '{% import "partials/tailor_macros.html" as tailor %}{{ tailor.guard(ctx) }}'
    ) if surface == "tailor_guard" else environment.get_template(f"partials/{surface}.html")
    parsed = _Elements()
    parsed.feed(template.render(**contexts[surface]))
    values = [json.loads(attrs["hx-vals"]) for _, attrs in parsed.elements if "hx-vals" in attrs]
    assert values
    assert all("confirmed" not in value for value in values)
    assert any(hostile in value.values() for value in values)


@pytest.mark.parametrize("kind", ["greenhouse", "sec_edgar"])
@pytest.mark.parametrize("recorded_status", [None, "running", "partial"])
async def test_scrape_crash_has_one_finished_history_entry(sm, monkeypatch, kind, recorded_status):
    async with sm() as session:
        source = Source(name="Example", kind=kind, track="employment", enabled=True)
        session.add(source)
        await session.flush()
        source_id = source.id
        previous = ScrapeRun(source_id=source_id, status="ok", finished_at="2026-01-01")
        session.add(previous)
        await session.commit()
        previous_id = previous.id

    async def crash(*args, **kwargs):
        if recorded_status:
            async with sm() as session:
                session.add(ScrapeRun(
                    source_id=source_id, status=recorded_status, counts={"fetched": 3},
                    finished_at="2026-10-01" if recorded_status == "partial" else None,
                ))
                await session.commit()
        raise RuntimeError("scrape interrupted")

    monkeypatch.setattr("kestrel.ingest.runner.run_source", crash)
    monkeypatch.setattr("kestrel.ingest.signal_runner.run_signal_source", crash)
    queue = TaskQueue(sm)
    await queue.enqueue("scrape", {"source_id": source_id})
    settings = get_settings()
    stats = await Worker(sm, FakeLLM(settings), settings).drain_scrapes()
    assert stats.errors == 1
    async with sm() as session:
        runs = (await session.scalars(select(ScrapeRun).order_by(ScrapeRun.id))).all()
        assert len(runs) == 2
        assert runs[0].id == previous_id and runs[0].status == "ok"
        current = runs[1]
        assert current.status == ("partial" if recorded_status == "partial" else "failed")
        assert current.finished_at
        if recorded_status:
            assert current.counts["fetched"] == 3
        if recorded_status != "partial":
            assert "scrape interrupted" in current.error
        assert (await session.get(Source, source_id)).last_run_at
        assert (await session.scalar(select(Task))).status == "dead"


@pytest.mark.parametrize("value", [time(14, 30), time(14, 30, tzinfo=UTC), "14:30:00"])
def test_time_only_local_display_preserves_clock_without_a_date(value):
    assert format_display(value, kind="time", time_style="24h", local=True) == "14:30"
    assert format_display(value, kind="time", time_style="24h", local=False) == "14:30"
