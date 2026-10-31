"""Document actions ask before sending text to an online model."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from kestrel.config import HostedMode
from kestrel.db.models import DocSlot, Setting
from tests.test_documents_editor import _app


class _NeverCalled:
    def __init__(self, *_args, **_kwargs) -> None:
        raise AssertionError("the model must not be called before document consent")


def _enable_document_hosted() -> None:
    from kestrel.db.engine import get_sessionmaker

    async def seed() -> None:
        async with get_sessionmaker()() as session:
            session.add_all([
                Setting(key="llm.hosted_mode.enabled", value=True),
                Setting(key="llm.hosted_mode.acknowledged_at", value="2026-10-15T00:00:00Z"),
                Setting(key="llm.hosted_mode.acknowledged_terms",
                        value=HostedMode().disclosure_terms()),
                Setting(key="llm.tiers.docsel.backend", value="frontier"),
                Setting(key="llm.tiers.docsel.model", value="vendor/docsel"),
                Setting(key="llm.tiers.tier2.backend", value="frontier"),
                Setting(key="llm.tiers.tier2.model", value="vendor/assess"),
            ])
            await session.commit()

    asyncio.run(seed())


def _slot_id() -> int:
    from kestrel.db.engine import get_sessionmaker

    async def read() -> int:
        async with get_sessionmaker()() as session:
            return int((await session.execute(select(DocSlot.id))).scalar_one())

    return asyncio.run(read())


def test_online_suggestion_asks_and_rejects_stale_destination(monkeypatch) -> None:
    monkeypatch.setattr("kestrel.llm.client.LLMClient", _NeverCalled)
    client, _master_id = _app(monkeypatch)
    _enable_document_hosted()
    slot_id = _slot_id()

    ask = client.post(f"/documents/slot/{slot_id}/suggest")
    assert ask.status_code == 200
    assert 'data-testid="doc-online-send"' in ask.text
    assert "vendor/docsel" in ask.text
    assert "class=\"import-processing\"" not in ask.text

    stale = client.post(
        f"/documents/slot/{slot_id}/suggest",
        data={"confirmed": "1", "model": "vendor/docsel", "dest": "stale|https://old|vendor/docsel"},
    )
    assert "model or online service changed" in stale.text


def test_online_import_does_not_auto_start(monkeypatch) -> None:
    monkeypatch.setattr("kestrel.llm.client.LLMClient", _NeverCalled)
    client, _master_id = _app(monkeypatch)
    _enable_document_hosted()

    response = client.post("/api/documents/import/read", data={"text": "Led a team."})
    assert response.status_code == 200
    assert 'data-testid="doc-online-send"' in response.text
    assert 'class="import-processing"' not in response.text
    assert 'hx-trigger="load"' not in response.text


def test_online_current_assessment_requires_confirmation(monkeypatch) -> None:
    monkeypatch.setattr("kestrel.llm.client.LLMClient", _NeverCalled)
    client, master_id = _app(monkeypatch)
    _enable_document_hosted()

    response = client.post(f"/documents/{master_id}/assess")
    assert response.status_code == 200
    assert 'data-testid="doc-online-send"' in response.text


def test_online_assessment_rejects_stale_destination(monkeypatch) -> None:
    monkeypatch.setattr("kestrel.llm.client.LLMClient", _NeverCalled)
    client, master_id = _app(monkeypatch)
    _enable_document_hosted()

    response = client.post(
        f"/documents/{master_id}/assess",
        data={
            "confirmed": "1", "model_choice": "", "model": "vendor/assess",
            "dest": "stale|https://old|vendor/assess",
        },
    )
    assert "model or online service changed" in response.text


def test_online_import_rejects_stale_destination(monkeypatch) -> None:
    monkeypatch.setattr("kestrel.llm.client.LLMClient", _NeverCalled)
    client, _master_id = _app(monkeypatch)
    _enable_document_hosted()

    response = client.post(
        "/api/documents/import",
        data={
            "text": "Led a team.", "confirmed": "1", "model": "vendor/docsel",
            "dest": "stale|https://old|vendor/docsel",
        },
    )
    assert "model or online service changed" in response.text


def test_local_import_keeps_automatic_processing(monkeypatch) -> None:
    monkeypatch.setattr("kestrel.llm.client.LLMClient", _NeverCalled)
    client, _master_id = _app(monkeypatch)

    response = client.post("/api/documents/import/read", data={"text": "Led a team."})
    assert response.status_code == 200
    assert 'class="import-processing"' in response.text
    assert 'hx-trigger="load"' in response.text
