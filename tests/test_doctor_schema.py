"""Preflight proves each tier model honors response_format with thinking off (Ollama #14645). A
model that answers in prose FAILS and blocks; a model that answers the probe JSON passes."""

from __future__ import annotations

import json

from kestrel.config import get_settings
from kestrel.doctor import FAIL, PASS, DoctorReport, _check_schema


class _Client:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def chat(self, model, messages, **kw):
        self.calls.append({"model": model, **kw})
        return self.reply

    @staticmethod
    def strip_think(text: str) -> str:
        return text


def test_schema_probe_passes_on_matching_json():
    s = get_settings()
    present = {s.llm.tiers["tier1"].model}
    client = _Client(json.dumps({"answer": "ready", "n": 7}))
    rep = DoctorReport()
    _check_schema(rep, client, s, present)
    rows = [r for r in rep.results if r.name.startswith("schema honored")]
    assert rows and all(r.status == PASS for r in rows)
    sent = client.calls[0]
    assert sent["reasoning_effort"] == "none"
    assert sent["response_format"]["type"] == "json_schema"


def test_schema_probe_fails_and_blocks_on_prose():
    s = get_settings()
    present = {s.llm.tiers["tier1"].model}
    rep = DoctorReport()
    _check_schema(rep, _Client("Sure! The answer is ready."), s, present)
    row = next(r for r in rep.results if r.name.startswith("schema honored"))
    assert row.status == FAIL and row.blocking and "14645" in row.remediation
    assert rep.has_blocking_failure
