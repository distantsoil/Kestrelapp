"""Review round 1: the Tier 3 egress record (D-RR1-5) and closing uploads (D-RR1-4).

Each test was written against the code before its fix and failed there.

- P3-1: a payload that reached the provider on the first attempt stays `egressed`, even when the
  guard's one reasoning-off re-ask then fails to connect.
- P3-2: a reset or aborted connection is not proof that nothing was sent.
- P3-3: the Tier 3 refusal names something the operator can actually change.
- P3-4: every upload route closes the uploaded file, or its temp file holds an OS handle until
  garbage collection (a sharing violation on Windows).
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


# ------------------------------------------------------------------------------------- P3-1
class _ExhaustedThenUnreachable:
    """First attempt: accepted and billed by the provider, but the reasoning spent the whole cap
    (the one declined reply the guard re-asks with reasoning off). Second attempt: the
    connection is refused."""

    def __init__(self) -> None:
        self.calls = 0

    def reasoning_hint_applies(self, tier: str) -> bool:
        return True

    async def complete(self, **kw):
        from kestrel.llm.client import ModelDeclined

        self.calls += 1
        if self.calls == 1:
            raise ModelDeclined("reasoning spent the cap", "{}", reasoning_exhausted=True)
        raise httpx.ConnectError("provider unreachable")


async def test_p3_1_a_reask_that_cannot_connect_still_counts_the_first_send():
    from kestrel.llm.guard import run_guarded
    from kestrel.pipeline import tier3
    from tests.test_review_round1_failing_first import _p3_model

    client = _ExhaustedThenUnreachable()
    res = await run_guarded(
        client,
        tier=tier3._FRONTIER_TIER,
        messages=[{"role": "user", "content": "x"}],
        model_cls=_p3_model(),
        allow_external=True,
    )
    assert client.calls == 2, "the guard did not make its reasoning-off re-ask"
    assert res.ok is False
    assert res.egressed is True, (
        "the first attempt reached the provider; a refused re-ask does not un-send it, and "
        "egressed=False would let the operator re-send the same payload"
    )


# ------------------------------------------------------------------------------------- P3-2
@pytest.mark.parametrize("exc", [ConnectionResetError("reset"), ConnectionAbortedError("aborted")])
def test_p3_2_a_dropped_connection_is_not_proof_nothing_was_sent(exc):
    from kestrel.llm.client import _never_left

    assert _never_left(exc) is False, (
        f"{type(exc).__name__} can arrive after the provider has the whole request"
    )


def test_p3_2_connect_phase_failures_still_never_left():
    import socket

    from kestrel.llm.client import _never_left

    for exc in (
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("slow connect"),
        ConnectionRefusedError("refused"),
        socket.gaierror("dns"),
    ):
        assert _never_left(exc) is True, type(exc).__name__


# ------------------------------------------------------------------------------------- P3-3
async def test_p3_3_the_refusal_names_what_the_operator_can_change(sm, monkeypatch):
    from kestrel.config import get_settings
    from kestrel.db.models import FrontierTransmission
    from kestrel.pipeline import tier3
    from tests.test_tier3 import _RecordingClient, _seed_opp

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    async with sm() as s:
        oid = await _seed_opp(s)
    settings = get_settings()
    async with sm() as s:
        preview = await tier3.build_escalation_preview(s, settings, oid)
        s.add(
            FrontierTransmission(
                opportunity_id=oid, provider="frontier", base_url=preview.base_url,
                model=preview.model, payload=preview.messages,
                payload_sha256=preview.confirmation_token, status="failed",
                error="request error: ProviderError: 500", egressed=True,
            )
        )
        await s.commit()
    client = _RecordingClient(settings)
    async with sm() as s:
        r = await tier3.transmit_escalation(
            s, client, settings, oid, confirmation_token=preview.confirmation_token
        )
    assert r.sent is False and client.calls == []
    err = (r.error or "").lower()
    assert "offer" not in err, "Tier 3 escalates an opportunity; there is no offer to edit"
    assert "fresh preview" not in err
    assert "profile" in err, "the refusal must name something that produces a new payload"
    assert "—" not in (r.error or "")


# ------------------------------------------------------------------------------------- P3-4
def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


@pytest.fixture
def closed_uploads(monkeypatch) -> list[str]:
    closed: list[str] = []
    original = UploadFile.close

    async def spy(self):
        closed.append(self.filename or "")
        await original(self)

    monkeypatch.setattr(UploadFile, "close", spy)
    return closed


def test_p3_4_restore_closes_the_uploaded_backup(closed_uploads):
    with _client() as c:
        r = c.post(
            "/settings/restore",
            files={"backup_file": ("not-a-backup.zip", b"not a zip", "application/zip")},
        )
    assert r.status_code == 200
    assert "not-a-backup.zip" in closed_uploads, "the restore upload was never closed"


def test_p3_4_profile_import_closes_the_uploaded_file(closed_uploads):
    with _client() as c:
        r = c.post(
            "/backup/import-profile",
            files={"profile_file": ("profile.yml", b"::: not yaml", "application/yaml")},
        )
    assert r.status_code == 200
    assert "profile.yml" in closed_uploads, "the profile upload was never closed"


def test_p3_4_document_import_closes_the_uploaded_file(closed_uploads):
    with _client() as c:
        r = c.post(
            "/api/documents/import/read",
            files={"file": ("resume.md", b"# Test Operator\n\nSome text.\n", "text/markdown")},
        )
    assert r.status_code == 200
    assert "resume.md" in closed_uploads, "the document upload was never closed"


async def test_p3_4_the_helper_closes_even_when_the_upload_is_refused():
    from kestrel.web.uploads import read_upload

    closed = []

    class _Upload:
        size = 10_000

        async def read(self, n: int = -1) -> bytes:
            raise AssertionError("a declared-oversize upload must not be read")

        async def close(self) -> None:
            closed.append(True)

    class _TooLarge(Exception):
        pass

    with pytest.raises(_TooLarge):
        await read_upload(_Upload(), limit=100, too_large=_TooLarge)
    assert closed == [True]


def test_p3_4_applied_tracker_import_closes_the_uploaded_file(closed_uploads):
    with _client() as c:
        r = c.post(
            "/settings/applied/review",
            files={"file": ("tracker.csv", b"company,title\nAcme,Analyst\n", "text/csv")},
        )
    assert r.status_code == 200
    assert "tracker.csv" in closed_uploads, "the applied-tracker upload was never closed"


async def test_p3_4_the_helper_closes_even_when_the_request_is_cancelled():
    """Under a cancelled anyio scope every unshielded await raises again, so a plain `await
    upload.close()` in the `finally` would itself be cancelled and leave the handle open."""
    import anyio

    from kestrel.web.uploads import read_upload

    closed = []

    class _Upload:
        size = None

        async def read(self, n: int = -1) -> bytes:
            await anyio.sleep(0)
            return b""

        async def close(self) -> None:
            await anyio.sleep(0)
            closed.append(True)

    with anyio.CancelScope() as scope:
        scope.cancel()
        await read_upload(_Upload())
    assert closed == [True], "a cancelled request left the upload open"


async def test_p3_3_a_retry_in_flight_is_recorded_as_possibly_sent(sm, monkeypatch):
    """While a retried send is in flight its row is `pending`. If the process dies there, the
    row must not still say egressed=False from the earlier connect failure."""
    from sqlalchemy import select

    from kestrel.config import get_settings
    from kestrel.db.models import FrontierTransmission
    from kestrel.pipeline import tier3
    from tests.test_tier3 import _seed_opp

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    async with sm() as s:
        oid = await _seed_opp(s)
    settings = get_settings()
    async with sm() as s:
        preview = await tier3.build_escalation_preview(s, settings, oid)
        s.add(
            FrontierTransmission(
                opportunity_id=oid, provider="frontier", base_url=preview.base_url,
                model=preview.model, payload=preview.messages,
                payload_sha256=preview.confirmation_token, status="failed",
                error="request error: ConnectError: refused", egressed=False,
            )
        )
        await s.commit()

    seen: list[tuple[str, bool]] = []

    class _Inspects:
        settings_ = settings

        async def complete(self, **kw):
            async with sm() as s2:
                row = (await s2.execute(select(FrontierTransmission))).scalars().one()
                seen.append((row.status, row.egressed))
            raise httpx.ConnectError("still refused")

    async with sm() as s:
        await tier3.transmit_escalation(
            s, _Inspects(), settings, oid, confirmation_token=preview.confirmation_token
        )
    assert seen == [("pending", True)], f"row during the retried send: {seen}"


async def test_p3_4_a_close_that_fails_does_not_mask_the_read():
    """A failing close is logged, never raised: the upload was read and the request must finish.
    (The first version of this helper logged through stdlib logging with structlog-style keyword
    arguments, so the handler itself raised TypeError exactly when a close failed.)"""
    from kestrel.web.uploads import read_upload

    class _Upload:
        filename = "stuck.zip"

        async def read(self, n: int = -1) -> bytes:
            return b"data"

        async def close(self) -> None:
            raise OSError("the file is in use")

    assert await read_upload(_Upload()) == b"data"
