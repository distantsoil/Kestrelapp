"""Run one source end to end: create a scrape_run, fetch via the adapter, normalize+store each
posting, and enqueue an embed task (and t2 on a material change) IN THE SAME transaction as the
opportunity. Committing per posting makes the embed-for-every-opportunity invariant durable and
resumable (a crash mid-source keeps the postings already stored and their embed tasks; hard rule 5).

Any recoverable error makes the run `partial` with a clear message and NEVER aborts the batch
(SPEC.md 5.1); three consecutive failures auto-disable the source and record a notification
(SPEC.md 6.5)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Notification, ScrapeRun, Source, Task
from kestrel.ingest import board_discovery, employer_links
from kestrel.ingest.adapters import get_adapter
from kestrel.ingest.base import AdapterError, SourceConfigError
from kestrel.ingest.http import HttpClient
from kestrel.ingest.normalize import normalize_and_store
from kestrel.pipeline.tasks import enqueue_once
from kestrel.services import run_model

log = structlog.get_logger()


@dataclass
class RunOutcome:
    scrape_run_id: int
    status: str
    counts: dict
    error: str | None


async def run_source(
    sessionmaker: async_sessionmaker[AsyncSession],
    source_id: int,
    *,
    http: HttpClient,
    model_override: dict | None = None,
    model_label: str | None = None,
    online_approval: dict | None = None,
) -> RunOutcome:
    """`model_override` is the one-run model choice (D-UI-72, amended): recorded on this
    scrape_run, attached to each role this run stores at stage `new` (a marker Tier 0 consumes,
    kestrel.services.run_model) and put on the Tier 2 task of a posting it re-reads as changed.
    It reaches Tier 2 only; Tier 1 stays on the configured model."""
    counts: dict = {"fetched": 0, "new": 0, "reseen": 0, "changed": 0, "errors": 0}
    run_model.stamp(counts, model_override, model_label)
    async with sessionmaker() as session:
        source = await session.get(Source, source_id)
        if source is None:
            raise ValueError(f"source {source_id} not found")
        run = ScrapeRun(
            source_id=source.id, status="running", started_at=utcnow_iso(), counts=dict(counts)
        )
        session.add(run)
        await session.commit()  # persist the scrape_run before any posting work
        run_id = run.id
        # Plain copies for logging: a rollback below expires every loaded instance, and reading an
        # expired attribute on an AsyncSession outside a greenlet raises MissingGreenlet.
        source_name, source_kind = source.name, source.kind
        error: str | None = None
        postings = None

        try:
            adapter = get_adapter(source_kind)
            if adapter is None:
                raise AdapterError(f"no adapter registered for kind '{source_kind}'")
            adapter.validate_config(source.config)
            postings = await adapter.fetch(source.config, http)
            counts["fetched"] = len(postings)
            if getattr(adapter, "discovery_only", False):
                # A link source (RemoteRocketship, D-SRC-14): the fetch holds only employer URLs,
                # which are never stored. Boards are found through D-SRC-7; the rest are read
                # from the employer's own page, and those postings are what get stored below.
                # A fetch that stopped early with links in hand (a 429) makes the run partial
                # without counting a failure of the source.
                stopped_early = getattr(postings, "partial_error", None)
                links, postings = postings, None  # a failing resolve must store no bare link
                postings, link_counts = await employer_links.resolve(
                    session, links, source=source, http=http
                )
                counts.update(link_counts)
                if stopped_early:
                    error = stopped_early
            # Postings per board (JobSpy tags each with its site), so the Sources page can say
            # when a board found nothing instead of hiding it in the total (D-SRC-4).
            sites = ((p.raw or {}).get("site") for p in postings)
            by_site = Counter(site for site in sites if site)
            if by_site:
                counts["by_site"] = dict(by_site)
        except (SourceConfigError, AdapterError) as exc:  # expected recoverable failure
            error = f"{type(exc).__name__}: {exc}"
            source.consecutive_failures += 1
        except Exception as exc:  # unexpected: never abort the batch, but LOG it (may be a bug)
            log.error(
                "scrape_unexpected_error", source=source_name, kind=source_kind, error=str(exc)
            )
            error = f"{type(exc).__name__}: {exc}"
            source.consecutive_failures += 1

        if postings is not None:
            from kestrel.services.online_consent import read_approval

            for raw in postings:
                try:
                    res = await normalize_and_store(session, raw, source=source, scrape_run=run)
                    await session.flush()
                    if model_override and res.opportunity.stage == "new":
                        # This run is processing the role right now: its pick goes with it.
                        await run_model.attach(session, res.opportunity.id, model_override)
                    if res.enqueue_embed:
                        session.add(
                            Task(type="embed", payload={"opportunity_id": res.opportunity.id})
                        )
                    if res.requeue_t2:
                        # Idempotent: never stack a second live t2 task on an opportunity that
                        # already has one queued. The Tier 2 handler is additionally guarded by
                        # content_version so even a duplicate can never double-evaluate.
                        await enqueue_once(
                            session,
                            "t2",
                            res.opportunity.id,
                            extra={
                                "changed": True,
                                "version": res.opportunity.version,
                                **({"model_override": dict(model_override)}
                                   if model_override else {}),
                                **({"online_approval": read_approval(online_approval)}
                                   if online_approval is not None else {}),
                            },
                        )
                    await session.commit()  # opportunity + its embed task, atomic and durable
                    key = (
                        "new"
                        if res.action == "new"
                        else "changed"
                        if res.action == "reseen_changed"
                        else "reseen"
                    )
                    counts[key] += 1
                    if res.suppressed:
                        # Hidden on arrival as a re-post of an applied pursuit (SPEC.md 5.6);
                        # still a stored row, so "new" counts it too.
                        counts["suppressed"] = counts.get("suppressed", 0) + 1
                except Exception as exc:  # one bad posting never fails the whole source
                    await session.rollback()
                    # The rollback expired `source` and `run`; reload them under the greenlet so
                    # the next posting (and the bookkeeping below) can read them again.
                    await session.refresh(source)
                    await session.refresh(run)
                    counts["errors"] += 1
                    error = f"{type(exc).__name__}: {exc}"
                    log.warning("normalize_error", source=source_name, error=str(exc))
            stored = counts["new"] + counts["reseen"] + counts["changed"]
            if stored == 0 and counts["errors"] > 0:
                # every posting failed: a failure, not a benign success
                source.consecutive_failures += 1
            else:  # any success, an empty one included, clears the failing state (D-UI-33)
                source.consecutive_failures = 0

        if postings and source_kind == "jobspy":
            # A search's postings name company boards to watch (D-SRC-7). Finding none, or
            # failing to, never changes how the run itself went.
            try:
                counts["boards_found"] = await board_discovery.discover_boards(
                    session, postings, search=source, http=http
                )
                await session.commit()
            except Exception as exc:
                await session.rollback()
                await session.refresh(source)
                await session.refresh(run)
                log.warning("board_discovery_failed", source=source_name, error=str(exc))

        status = "partial" if (error or counts["errors"]) else "ok"
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

    return RunOutcome(run_id, status, counts, error)
