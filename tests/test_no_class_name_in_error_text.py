"""User-facing failure text never names a Python exception class (D-SEC-4, D-UI-263).

The sentence shown to the person is Kestrel's own words; the class name belongs in the log and the
stored diagnosis, not the sentence. Each test triggers the failure with a sample exception, asserts
the shown text has no class name, and (where it previously did) that the type is still in the log.
A source guard fails the build if the leak pattern returns anywhere in kestrel/.
"""

from __future__ import annotations

import asyncio
import pathlib
import re

import pytest
import respx
from httpx import Response
from structlog.testing import capture_logs

from kestrel.config import BackendConfig


def _seed_settings(text: str | None = None) -> None:
    from kestrel import paths

    target = paths.config_dir() / "settings.yml"
    if text is None:
        text = "server:\n  bind_all: false\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_task_failure_text_fallback_has_no_class_name():
    from kestrel.errors import failure_record, shown_failure, task_failure_text

    exc = RuntimeError("boom at C:/x")
    shown = task_failure_text(exc)
    assert "RuntimeError" not in shown
    assert "an unexpected problem" in shown
    stored = failure_record(exc)
    assert "RuntimeError" in stored
    assert "RuntimeError" not in shown_failure(stored)


def test_plain_error_fallback_has_no_class_name():
    from kestrel.web.errors import plain_error

    exc = RuntimeError("boom at C:/x")
    shown = plain_error(exc)
    assert "RuntimeError" not in shown
    assert "an unexpected problem" in shown
    assert "C:/x" not in shown


def test_lan_read_failure_has_no_class_name(_isolate_data_home):
    from kestrel import paths
    from kestrel.services import lan_access

    paths.ensure_dirs()
    (paths.config_dir() / "settings.yml").write_bytes(b"\xff\xfe\x00 not utf-8")
    with capture_logs() as logs:
        with pytest.raises(lan_access.LanAccessError) as ei:
            lan_access._read_text()
    assert "could not be read" in str(ei.value)
    assert "UnicodeDecodeError" not in str(ei.value)
    read = [e for e in logs if e["event"] == "settings_read_failed"]
    assert read and read[0]["error_type"] == "UnicodeDecodeError"


def test_lan_write_failure_has_no_class_name(_isolate_data_home, monkeypatch):
    import os

    from kestrel.services import lan_access

    _seed_settings()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with capture_logs() as logs:
        with pytest.raises(lan_access.LanAccessError) as ei:
            lan_access.set_bind_all(True)
    assert "could not be written" in str(ei.value)
    assert "OSError" not in str(ei.value)
    failed = [e for e in logs if e["event"] == "settings_write_failed"]
    assert failed and failed[0]["error_type"] == "OSError"


def test_convert_docx_unreadable_has_no_class_name():
    from kestrel.docs.convert import ConvertError, _convert_docx

    with capture_logs() as logs:
        with pytest.raises(ConvertError) as ei:
            _convert_docx(b"not a real docx", source_format="docx")
    expected = "Could not read the .docx file. It may be damaged or not a Word document."
    assert str(ei.value) == expected
    bad = [e for e in logs if e["event"] == "convert_docx_unreadable"]
    assert bad and bad[0].get("error_type")


def test_convert_pdf_unreadable_has_no_class_name():
    from kestrel.docs.convert import ConvertError, _convert_pdf

    with capture_logs() as logs:
        with pytest.raises(ConvertError) as ei:
            _convert_pdf(b"not a real pdf")
    assert str(ei.value) == "Could not read the PDF. It may be damaged."
    bad = [e for e in logs if e["event"] == "convert_pdf_unreadable"]
    assert bad and bad[0].get("error_type")


def test_convert_soffice_failed_has_no_class_name(monkeypatch):
    import subprocess

    from kestrel.docs import convert
    from kestrel.docs.convert import ConvertError

    monkeypatch.setattr(convert, "_soffice", lambda: "/usr/bin/soffice")

    def boom(*a, **k):
        raise OSError("soffice crashed")

    monkeypatch.setattr(subprocess, "run", boom)
    with capture_logs() as logs:
        with pytest.raises(ConvertError) as ei:
            convert._convert_doc(b"not a doc", "resume.doc")
    assert str(ei.value) == "LibreOffice could not convert 'resume.doc'."
    bad = [e for e in logs if e["event"] == "convert_soffice_failed"]
    assert bad and bad[0].get("error_type") == "OSError"


@respx.mock
def test_verify_key_unexpected_response_has_no_class_name():
    from kestrel.llm import providers as prov

    respx.get("https://api.openai.com/v1/models").mock(
        return_value=Response(200, json=[1, 2, 3]))
    b = BackendConfig(
        base_url="https://api.openai.com/v1", provider="openai", api_key="sk-test-123")
    with capture_logs() as logs:
        key = asyncio.run(prov.verify_key(b))
    assert key.ok is False
    assert "unexpected provider response" in key.detail
    assert "AttributeError" not in key.detail
    log = [e for e in logs if e["event"] == "key_check_unexpected_response"]
    assert log and log[0].get("error_type") == "AttributeError"


def test_guard_no_paren_class_name_in_shown_text():
    root = pathlib.Path(__file__).resolve().parent.parent / "kestrel"
    bad = re.compile(r"\(\{type\([A-Za-z_][A-Za-z0-9_]*\)\.__name__\}\)")
    hits = [str(p) for p in root.rglob("*.py") if bad.search(p.read_text(encoding="utf-8"))]
    assert hits == []
