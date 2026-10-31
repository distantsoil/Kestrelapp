from __future__ import annotations

import csv
import io
import json
import threading
import zipfile
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from starlette.datastructures import UploadFile

from kestrel.db.models import Setting
from kestrel.services import xlsx_text
from kestrel.web.deps import templates
from tests.test_run_mode import _app_db, _fresh_app, _rows

MODEL = "x'}><img src=x onerror=alert(1)>"


class _Elements(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))


@pytest.mark.parametrize(
    "dialog",
    [
        "analytics_online",
        "pp_online",
        "retry_online",
        "retry_anyway",
        "role_online",
        "role_deeper",
        "runs_choose",
    ],
)
def test_online_dialog_model_ids_are_inert(dialog: str) -> None:
    route = {
        "native": True,
        "model": MODEL,
        "dest": MODEL,
        "provider": MODEL,
        "sends": "Resume text",
        "practice_sends": "Your answers",
        "employer": "off",
        "free": False,
        "models": [{"id": MODEL, "name": MODEL, "cost": "Free"}],
    }
    rendered = (
        templates()
        .env.get_template(f"partials/{dialog}.html")
        .render(
            route=route,
            provider=MODEL,
            model=MODEL,
            service=MODEL,
            hosted=True,
            employer="off",
            sends="Resume text",
            track="employment",
            range="all",
            opp_id=1,
            opp={"id": 1},
            quarantine_id=2,
            dom_id="card-2",
            role="Planner",
            org="Example",
            extra={},
            url="/opportunities/1/role/research",
            action="research",
            kind="company",
            error=None,
            preview={"model": MODEL, "prompt_tokens_estimate": 1, "confirmation_token": "token"},
            payload="{}",
            models=route["models"],
            usual=MODEL,
            sends_both=[],
        )
    )
    parsed = _Elements()
    parsed.feed(rendered)
    assert not any(tag == "img" for tag, _attrs in parsed.elements)
    assert not any(key.startswith("onerror") for _tag, attrs in parsed.elements for key in attrs)
    for _tag, attrs in parsed.elements:
        if "hx-vals" in attrs:
            values = json.loads(attrs["hx-vals"])
            assert values["confirmed"] == "1"
            if dialog == "analytics_online":
                assert values["model"] == MODEL


@pytest.mark.parametrize("macro", ["button", "chip", "switch", "modal", "settings_switch"])
def test_attribute_mappings_escape_values(macro: str) -> None:
    bodies = {
        "button": '{{ ui.button("Save", attrs=attrs) }}',
        "chip": '{{ ui.chip("Topic", attrs=attrs) }}',
        "switch": '{{ ui.switch("on", "On", attrs=attrs) }}',
        "modal": '{% call ui.modal("test", "Test", [{"label":"Go", "attrs":attrs}]) %}'
        "Body{% endcall %}",
        "settings_switch": '{{ st.sw("on", "On", True, attrs) }}',
    }
    rendered = (
        templates()
        .env.from_string(
            '{% import "ui/macros.html" as ui %}{% import "partials/st_macros.html" as st %}'
            + bodies[macro]
        )
        .render(attrs={"data-value": '"' + MODEL})
    )
    parsed = _Elements()
    parsed.feed(rendered)
    assert not any(tag == "img" for tag, _attrs in parsed.elements)
    assert [attrs["data-value"] for _tag, attrs in parsed.elements if "data-value" in attrs] == [
        '"' + MODEL
    ]


@pytest.mark.parametrize("path", ["/api/run/now", "/runs/now"])
def test_implicit_online_run_requires_confirmation(path: str) -> None:
    client = _fresh_app()

    async def seed(session):
        session.add_all(
            [Setting(key=key, value=value) for key, value in _rows(mode="hosted").items()]
        )

    _app_db(seed)
    refused = client.post(path)
    assert refused.status_code == 400
    assert client.get("/api/status").json()["pending_mode"] is None

    async def shown(session):
        from kestrel.services.online_consent import approval_for
        from kestrel.services.setting_overrides import load_settings_with_db

        return approval_for(await load_settings_with_db(session, mode="hosted"),
                            ("tier1", "tier2"))

    approval = json.dumps(_app_db(shown))
    # A confirmation that names no destination is still refused (D-UI-178).
    unbound = client.post(path, data={"confirmed": "1"})
    assert unbound.status_code == 400
    assert client.get("/api/status").json()["pending_mode"] is None
    approved = client.post(path, data={"confirmed": "1", "online_approval": approval})
    assert approved.status_code == 200
    assert client.get("/api/status").json()["pending_mode"] == "hosted"


def _workbook(*references: str) -> bytes:
    rows = "".join(
        f'<row><c r="{reference}" t="inlineStr"><is><t>Title</t></is></c></row>'
        for reference in references
    )
    sheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{rows}</sheetData></worksheet>"
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return output.getvalue()


@pytest.mark.parametrize("reference", ["ZZZZ1", "XFE1"])
def test_spreadsheet_refuses_columns_past_excel_limit(reference: str, monkeypatch) -> None:
    def no_cell_read(_node):
        pytest.fail("An out-of-range column must be refused before reading or expanding its row")

    monkeypatch.setattr(xlsx_text, "_text", no_cell_read)
    with pytest.raises(xlsx_text.SpreadsheetError, match="too many columns"):
        xlsx_text.to_csv(_workbook(reference, "A2"))


def test_spreadsheet_accepts_last_excel_column() -> None:
    rows = list(csv.reader(io.StringIO(xlsx_text.to_csv(_workbook("XFD1", "A2")))))
    assert len(rows[0]) == 16_384
    assert rows[0][-1] == "Title"


def test_spreadsheet_caps_total_expanded_cells(monkeypatch) -> None:
    monkeypatch.setattr(xlsx_text, "MAX_CELLS", 5)
    assert xlsx_text.to_csv(_workbook("C1", "B2")) == ",,Title\r\n,Title\r\n"
    with pytest.raises(xlsx_text.SpreadsheetError, match="too many cells"):
        xlsx_text.to_csv(_workbook("C1", "C2"))


async def test_spreadsheet_conversion_runs_outside_request_thread(monkeypatch) -> None:
    from kestrel.web.pages import settings_data

    request_thread = threading.get_ident()
    converted_threads = []
    upload = UploadFile(io.BytesIO(_workbook("A1", "A2")), filename="tracker.xlsx")

    async def form(**_limits):
        return {"file": upload}

    def convert(data):
        converted_threads.append(threading.get_ident())
        return "Title\nPlanner\n"

    monkeypatch.setattr(xlsx_text, "to_csv", convert)
    text, name, error = await settings_data._file_text(SimpleNamespace(form=form))
    assert (text, name, error) == ("Title\nPlanner\n", "tracker.xlsx", "")
    assert len(converted_threads) == 1 and converted_threads[0] != request_thread
