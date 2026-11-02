"""#102: every import request logs exactly one outcome line (event, outcome, a fixed reason code,
duration, file type, line count and, for a proposal, the outline counts), with no resume text,
file name or path in it. Fast failures used to leave no trace at all."""

from __future__ import annotations

import httpx
from structlog.testing import capture_logs

from tests.test_onboarding import _app

_TEXT = "Pat Example\n## Experience\nAcme | Town, ST  Jan 2020 – Dec 2022\n- Ran the secretword.\n"


def _lines(logs, event):
    return [e for e in logs if e["event"] == event]


def test_a_refused_import_logs_its_outcome_and_reason_without_the_file_name():
    c = _app()
    with capture_logs() as logs:
        c.post("/api/documents/import/read", data={"text": ""})
        c.post("/api/documents/import", data={"text": "   "})
        c.post("/api/documents/import/read",
               files={"file": ("private-name.xyz", b"data", "application/octet-stream")})
    reads = _lines(logs, "import_read")
    assert [(e["outcome"], e["reason"]) for e in reads] == [
        ("refused", "empty_text"), ("refused", "unsupported_type")]
    proposes = _lines(logs, "import_propose")
    assert [(e["outcome"], e["reason"]) for e in proposes] == [("refused", "empty_text")]
    assert all(e["log_level"] == "warning" and isinstance(e["ms"], int) for e in reads + proposes)
    assert reads[1]["file_type"] == "xyz"
    assert "private-name" not in repr(logs)


def test_a_model_error_and_a_good_import_each_log_one_outcome_line(monkeypatch):
    c = _app()

    async def _down(*_a, **_k):
        raise httpx.ConnectError("connection refused to http://secret.example/x")

    monkeypatch.setattr("kestrel.docs.imports.propose_import", _down)
    with capture_logs() as logs:
        r = c.post("/api/documents/import", data={"text": _TEXT})
    assert "could not reach the model" in r.text
    (line,) = _lines(logs, "import_propose")
    assert line["outcome"] == "error" and line["reason"] == "model_error"
    assert line["log_level"] == "warning" and line["file_type"] == "paste" and line["lines"] == 4
    assert "secretword" not in repr(line) and "secret.example" not in repr(line)

    from kestrel.docs.imports import ImportFlags, ImportProposal

    class _Role:
        pass

    async def _good(*_a, **_k):
        value = type("V", (), {"roles": [_Role(), _Role()], "model_dump_json": lambda s: "{}"})()
        flags = ImportFlags(unplaced=["x"], missed_roles=["y"], reasked=True)
        return ImportProposal(value=value, flags=flags)

    monkeypatch.setattr("kestrel.docs.imports.propose_import", _good)
    monkeypatch.setattr("kestrel.web.pages.documents._current_offer", lambda *_a: [])
    with capture_logs() as logs:
        c.post("/api/documents/import", data={"text": _TEXT})
    (ok,) = _lines(logs, "import_propose")
    assert ok["outcome"] == "ok" and ok["log_level"] == "info"
    assert (ok["roles"], ok["reasked"], ok["missed_roles"], ok["unplaced"]) == (2, True, 1, 1)


def test_a_cancelled_propose_request_still_logs_its_outcome():
    """The old request line was written only after the handler returned, so a request the
    browser abandoned left nothing. The outcome is logged from a finally."""
    import asyncio
    import types

    import pytest

    from kestrel.web.pages.documents import import_markdown, import_read

    class _Req:
        state = types.SimpleNamespace()

        async def form(self):
            raise asyncio.CancelledError

    for route, stage in ((import_markdown, "propose"), (import_read, "read")):
        with capture_logs() as logs, pytest.raises(asyncio.CancelledError):
            asyncio.run(route(_Req()))
        (start,) = _lines(logs, "import_started")
        (done,) = _lines(logs, f"import_{stage}")
        assert start["stage"] == stage
        got = (done["outcome"], done["reason"], done["status"])
        assert got == ("cancelled", "client_gone", 499)


def test_a_cross_origin_refusal_and_an_unreadable_form_each_log_a_line():
    c = _app()
    with capture_logs() as logs:
        r = c.post("/api/documents/import", data={"text": _TEXT},
                   headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    (line,) = _lines(logs, "import_refused")
    assert (line["outcome"], line["reason"], line["status"]) == ("refused", "cross_origin", 403)
    assert "evil.example" not in repr(logs)

    with capture_logs() as logs:
        r = c.post("/api/documents/import", data={"text": "x" * 1_200_000})  # over the field cap
    assert r.status_code == 400
    (bad,) = _lines(logs, "import_propose")
    assert (bad["outcome"], bad["reason"], bad["status"]) == ("refused", "bad_form", 400)
