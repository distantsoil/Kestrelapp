"""run_signal_source: a missing credential DISABLES the source cleanly (never fails the batch), a
trigger signal is classified+promoted, and a posting signal is normalized onto the consulting track.
Uses fake signal sources registered under unique kinds so the real adapters are not needed."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select

from kestrel.config import get_settings
from kestrel.db.models import Notification, Opportunity, ScrapeRun, Source, Task
from kestrel.ingest.signal_base import RawSignal, SourceConfigError
from kestrel.ingest.signal_runner import run_signal_source
from kestrel.ingest.signal_sources import register
from kestrel.ingest.signal_sources.sam_gov import SignalBatch
from kestrel.ingest.triggers import classify_and_store  # noqa: F401  (ensures module import)
from tests.trigger_fakes import FakeP10LLM


class _MissingKeySource:
    kind = "faildisable"

    def validate_config(self, config: dict) -> None:
        raise SourceConfigError("requires an API key; set SAM_GOV_API_KEY")

    async def fetch(self, config: dict, client: Any) -> list[RawSignal]:  # pragma: no cover
        return []


class _TriggerSource:
    kind = "faketrigger"

    def validate_config(self, config: dict) -> None:
        return None

    async def fetch(self, config: dict, client: Any) -> list[RawSignal]:
        return [
            RawSignal(
                signal_kind="trigger",
                org_name="TrigCo",
                source_url="https://sec.gov/x",
                external_id="t-1",
                text="ORG:TrigCo EVENT:acquisition CONF:0.85 REL:1 completed acquisition",
            )
        ]


class _PostingSource:
    kind = "fakeposting"

    def validate_config(self, config: dict) -> None:
        return None

    async def fetch(self, config: dict, client: Any) -> list[RawSignal]:
        return [
            RawSignal(
                signal_kind="posting",
                org_name="GovDept",
                title="IT modernization RFP",
                source_url="https://sam.gov/opp/1",
                external_id="rfp-1",
                text="Seeking interim IT leadership for a modernization program.",
                location_raw="Austin, TX",
            )
        ]


class _QuarantineSource:
    kind = "fakequarantine"

    def validate_config(self, config: dict) -> None:
        return None

    async def fetch(self, config: dict, client: Any) -> list[RawSignal]:
        return [
            RawSignal(signal_kind="trigger", source_url="https://x/1", external_id="qz-1",
                      text="BADJSON one"),
            RawSignal(signal_kind="trigger", source_url="https://x/2", external_id="qz-2",
                      text="BADJSON two"),
        ]


class _TwoPostingSource:
    kind = "faketwopostings"

    def validate_config(self, config: dict) -> None:
        return None

    async def fetch(self, config: dict, client: Any) -> list[RawSignal]:
        return [
            RawSignal(signal_kind="posting", org_name="GovDept", title="RFP one",
                      source_url="https://sam.gov/opp/1", external_id="rfp-1", text="one"),
            RawSignal(signal_kind="posting", org_name="GovDept", title="RFP two",
                      source_url="https://sam.gov/opp/2", external_id="rfp-2", text="two"),
        ]


class _PartialFetchSource:
    """Mirrors EDGAR/SAM.gov: some queries failed, the rest were collected (`fetch_errors`)."""

    kind = "fakepartialfetch"

    def validate_config(self, config: dict) -> None:
        return None

    async def fetch(self, config: dict, client: Any) -> list[RawSignal]:
        batch = SignalBatch()
        batch.append(
            RawSignal(signal_kind="posting", org_name="GovDept", title="RFP kept",
                      source_url="https://sam.gov/opp/9", external_id="rfp-9", text="kept")
        )
        batch.fetch_errors = ["ncode '541611': 503 from https://api.sam.gov/x"]
        return batch


_FAKES = [
    _MissingKeySource(),
    _TwoPostingSource(),
    _PartialFetchSource(),
    _TriggerSource(),
    _PostingSource(),
    _QuarantineSource(),
]


@pytest.fixture(scope="module", autouse=True)
def _register_fakes():
    """Register the fake kinds for this module only and remove them afterwards. Registering at
    import time leaked them into every later test module (the source catalog listed them as
    undocumented kinds)."""
    from kestrel.ingest import signal_sources as reg

    for src in _FAKES:
        register(src)
    yield
    for src in _FAKES:
        reg._SIGNAL_SOURCES.pop(src.kind, None)


async def _mk_source(sm, kind: str) -> int:
    async with sm() as s:
        src = Source(track="consulting", kind=kind, name=f"{kind}-src", enabled=True)
        s.add(src)
        await s.flush()
        sid = src.id
        await s.commit()
    return sid


async def test_missing_key_disables_source_cleanly(sm):
    sid = await _mk_source(sm, "faildisable")
    settings = get_settings()
    outcome = await run_signal_source(
        sm, sid, http=None, client=FakeP10LLM(settings), settings=settings
    )
    assert outcome.status == "partial"  # not "failed": the batch is never aborted
    async with sm() as s:
        src = await s.get(Source, sid)
        assert src.enabled is False  # cleanly disabled
        notes = (await s.execute(select(Notification))).scalars().all()
        assert any("disabled" in n.title.lower() for n in notes)


async def test_trigger_signal_is_classified_and_promoted(sm):
    sid = await _mk_source(sm, "faketrigger")
    settings = get_settings()
    outcome = await run_signal_source(
        sm, sid, http=None, client=FakeP10LLM(settings), settings=settings
    )
    assert outcome.counts["promoted"] == 1
    async with sm() as s:
        opp = (await s.execute(select(Opportunity))).scalar_one()
        assert opp.opp_type == "trigger_event" and opp.track == "consulting"
        embeds = (
            await s.execute(select(func.count()).select_from(Task).where(Task.type == "embed"))
        ).scalar_one()
        assert embeds == 1  # promoted trigger enqueues an embed


async def test_all_quarantined_run_is_partial_and_counts_as_failure(sm):
    sid = await _mk_source(sm, "fakequarantine")
    settings = get_settings()
    outcome = await run_signal_source(
        sm, sid, http=None, client=FakeP10LLM(settings), settings=settings
    )
    assert outcome.status == "partial"  # not "ok": a run that quarantines everything is unhealthy
    assert outcome.counts["quarantined"] == 2
    async with sm() as s:
        src = await s.get(Source, sid)
        assert src.consecutive_failures == 1  # counts toward the 3-failure auto-disable


async def test_one_bad_signal_is_counted_and_the_rest_are_stored(sm, monkeypatch):
    """A signal that fails after a statement ran forces a rollback that expires the loaded Source;
    the runner must still process the remaining signals and finish the run as partial."""
    import kestrel.ingest.signal_runner as sr

    sid = await _mk_source(sm, "faketwopostings")
    settings = get_settings()
    real = sr.normalize_and_store
    calls = {"n": 0}

    async def _flaky(session, raw, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            await session.execute(select(Source))
            raise RuntimeError("boom on the first signal")
        return await real(session, raw, **kw)

    monkeypatch.setattr(sr, "normalize_and_store", _flaky)
    outcome = await run_signal_source(
        sm, sid, http=None, client=FakeP10LLM(settings), settings=settings
    )
    assert outcome.status == "partial"
    assert outcome.counts["errors"] == 1 and outcome.counts["postings"] == 1
    async with sm() as s:
        run = await s.get(ScrapeRun, outcome.scrape_run_id)
        assert run.status == "partial" and run.finished_at is not None
        titles = (await s.execute(select(Opportunity.title))).scalars().all()
        assert titles == ["RFP two"]
        src = await s.get(Source, sid)
        assert src.consecutive_failures == 0


async def test_partial_fetch_keeps_collected_signals_and_marks_run_partial(sm):
    sid = await _mk_source(sm, "fakepartialfetch")
    settings = get_settings()
    outcome = await run_signal_source(
        sm, sid, http=None, client=FakeP10LLM(settings), settings=settings
    )
    assert outcome.status == "partial"
    assert outcome.error and "541611" in outcome.error
    assert outcome.counts["postings"] == 1  # what was collected is stored, not discarded
    async with sm() as s:
        src = await s.get(Source, sid)
        assert src.consecutive_failures == 0  # data flowed: not a failure toward auto-disable


async def test_posting_signal_is_normalized_onto_consulting(sm):
    sid = await _mk_source(sm, "fakeposting")
    settings = get_settings()
    outcome = await run_signal_source(
        sm, sid, http=None, client=FakeP10LLM(settings), settings=settings
    )
    assert outcome.counts["postings"] == 1
    async with sm() as s:
        opp = (await s.execute(select(Opportunity))).scalar_one()
        assert opp.track == "consulting"
        assert opp.title == "IT modernization RFP"
