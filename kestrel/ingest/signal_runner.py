"""Run one consulting signal source end to end (SPEC.md 5.2/5.3), the trigger-track sibling of
runner.run_source. Creates a scrape_run, fetches via the signal adapter, and per RawSignal either
classifies+accumulates a trigger or normalizes a posting, committing per signal so a kill mid-run
resumes cleanly (hard rule 5).

Failure semantics match Phase 3: a recoverable fetch error makes the run `partial` and never aborts
the batch; three consecutive failures auto-disable the source. A missing credential (SAM.gov has no
free key configured) is surfaced by validate_config as SourceConfigError and DISABLES the source
with a clear notification rather than failing the run."""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.config import Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Notification, ScrapeRun, Source
from kestrel.ingest.base import RawPosting
from kestrel.ingest.normalize import normalize_and_store
from kestrel.ingest.signal_base import AdapterError, RawSignal, SourceConfigError
from kestrel.ingest.signal_sources import get_signal_source
from kestrel.ingest.triggers import classify_and_store
from kestrel.llm.client import LLMClient
from kestrel.pipeline.tasks import enqueue_once

log = structlog.get_logger()

# The consulting Tier 0 comp rule normalizes to a day rate; a SAM.gov RFP rarely states one.
_POSTING_OPP_TYPE = "rfp"


@dataclass
class SignalRunOutcome:
    scrape_run_id: int
    status: str
    counts: dict
    error: str | None


def _posting_from_signal(signal: RawSignal) -> RawPosting:
    return RawPosting(
        external_id=signal.external_id,
        url=signal.source_url,
        title=signal.title,
        org_name=signal.org_name,
        org_url=signal.org_url,
        location_raw=signal.location_raw,
        description_md=signal.text,
        comp_min=signal.comp_min,
        comp_max=signal.comp_max,
        comp_currency=signal.comp_currency,
        comp_interval=signal.comp_interval,
        sector=None,
        raw=signal.raw,
    )


async def run_signal_source(
    sessionmaker: async_sessionmaker[AsyncSession],
    source_id: int,
    *,
    http,
    client: LLMClient,
    settings: Settings,
) -> SignalRunOutcome:
    counts = {
        "fetched": 0,
        "promoted": 0,
        "accumulated": 0,
        "reseen": 0,
        "postings": 0,
        "quarantined": 0,
        "errors": 0,
    }
    async with sessionmaker() as session:
        source = await session.get(Source, source_id)
        if source is None:
            raise ValueError(f"source {source_id} not found")
        run = ScrapeRun(source_id=source.id, status="running", started_at=utcnow_iso())
        session.add(run)
        await session.commit()
        run_id = run.id
        # Plain copies for logging: a rollback below expires every loaded instance, and reading an
        # expired attribute on an AsyncSession outside a greenlet raises MissingGreenlet.
        source_name, source_kind = source.name, source.kind
        error: str | None = None
        signals: list[RawSignal] | None = None

        adapter = get_signal_source(source_kind)
        try:
            if adapter is None:
                raise AdapterError(f"no signal source registered for kind '{source_kind}'")
            adapter.validate_config(source.config or {})
            signals = await adapter.fetch(source.config or {}, http)
            counts["fetched"] = len(signals)
            # An adapter that lost SOME queries (EDGAR, SAM.gov) still returns what it collected
            # and reports the failures on the batch; those make the run partial, never discarded.
            fetch_errors = [str(e) for e in (getattr(signals, "fetch_errors", None) or [])]
            if fetch_errors:
                error = "partial fetch: " + "; ".join(fetch_errors)
        except SourceConfigError as exc:
            # A missing credential / dead config: DISABLE the source cleanly, never fail the batch.
            source.enabled = False
            error = f"disabled: {exc}"
            session.add(
                Notification(
                    kind="source_disabled",
                    title=f"Source '{source.name}' disabled: {exc}",
                    severity="warning",
                    delivered_channels=[],
                )
            )
            run = await session.get(ScrapeRun, run_id)
            run.status = "partial"
            run.error = error
            run.counts = dict(counts)
            run.finished_at = utcnow_iso()
            source.last_run_at = utcnow_iso()
            await session.commit()
            return SignalRunOutcome(run_id, "partial", counts, error)
        except AdapterError as exc:
            error = f"{type(exc).__name__}: {exc}"
            source.consecutive_failures += 1
        except Exception as exc:  # unexpected: log, never abort the batch
            log.error("signal_fetch_error", source=source_name, kind=source_kind, error=str(exc))
            error = f"{type(exc).__name__}: {exc}"
            source.consecutive_failures += 1

        if signals is not None:
            for signal in signals:
                try:
                    if signal.signal_kind == "posting":
                        res = await normalize_and_store(
                            session, _posting_from_signal(signal), source=source, scrape_run=run
                        )
                        await session.flush()
                        if res.enqueue_embed:
                            await enqueue_once(session, "embed", res.opportunity.id)
                        counts["postings"] += 1
                    else:
                        out = await classify_and_store(
                            session, client, signal, source=source, settings=settings
                        )
                        await session.flush()
                        if out.action in ("promoted_strong", "promoted_compounded"):
                            if out.opportunity_id is not None:
                                await enqueue_once(session, "embed", out.opportunity_id)
                            counts["promoted"] += 1
                        elif out.action == "accumulated":
                            counts["accumulated"] += 1
                        elif out.action == "reseen":
                            counts["reseen"] += 1
                        elif out.action == "quarantined":
                            counts["quarantined"] += 1
                        elif out.action == "error":
                            counts["errors"] += 1
                            error = out.error
                    await session.commit()
                except Exception as exc:  # one bad signal never fails the whole source
                    await session.rollback()
                    # The rollback expired `source` and `run`; reload them under the greenlet so
                    # the next signal (and the bookkeeping below) can read them again.
                    await session.refresh(source)
                    await session.refresh(run)
                    counts["errors"] += 1
                    error = f"{type(exc).__name__}: {exc}"
                    log.warning("signal_error", source=source_name, error=str(exc))
            processed = (
                counts["promoted"] + counts["accumulated"] + counts["reseen"] + counts["postings"]
            )
            if processed > 0:
                source.consecutive_failures = 0
            elif counts["errors"] > 0 or counts["quarantined"] > 0:
                # Nothing was successfully processed and signals failed or quarantined: a failure,
                # not a benign success. A model/prompt that quarantines EVERYTHING must trip the
                # 3-consecutive-failures auto-disable, not look healthy.
                source.consecutive_failures += 1
            else:  # an empty success clears the failing state too (D-UI-33)
                source.consecutive_failures = 0

        status = "partial" if (error or counts["errors"] or counts["quarantined"]) else "ok"
        if source.consecutive_failures >= 3 and source.enabled:
            source.enabled = False
            session.add(
                Notification(
                    kind="source_disabled",
                    title=f"Source '{source.name}' auto-disabled after 3 consecutive failures",
                    severity="warning",
                    delivered_channels=[],
                )
            )
        run = await session.get(ScrapeRun, run_id)
        run.status = status
        run.error = error
        run.counts = dict(counts)
        run.finished_at = utcnow_iso()
        source.last_run_at = utcnow_iso()
        await session.commit()

    return SignalRunOutcome(run_id, status, counts, error)
