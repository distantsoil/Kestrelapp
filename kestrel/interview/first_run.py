"""The first run's one automatic story build (issue 176, D-STORY-700 to D-STORY-703).

Pressing "Start searching" the first time arms a flag in the database (services/onboarding.py).
Once a finished, not stopped run has produced at least one Tier 2 employment evaluation (the only
place interview sketches come from), this asks for ONE `build_stories` task through the same
`request_build` the Build button uses. Nothing here sends anything: with Tier 2 online and no
confirmed dialog `request_build` queues nothing and the flag is spent, so a build never goes
online on its own and never retries later behind the person's back."""

from __future__ import annotations

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.db.models import Evaluation
from kestrel.services import onboarding as ob

log = structlog.get_logger()


async def _has_deep_read(session: AsyncSession) -> bool:
    n = (
        await session.execute(
            select(func.count()).select_from(Evaluation).where(
                Evaluation.tier == 2, Evaluation.track == "employment"
            )
        )
    ).scalar_one()
    return bool(n)


async def queue_first_run_stories(
    sm: async_sessionmaker[AsyncSession], *, produced_tier2: bool
) -> bool:
    """Called where a run ends, after the scoring tiers have drained. True when a build was
    queued now. A run that produced no Tier 2 read leaves the flag due for the next run.

    The task row and the "done" flag are written in ONE transaction (`request_build(commit=False)`
    then a single commit), so a kill at any moment leaves both or neither: never a spent flag
    with no build, and never a build whose flag is still due to queue a second one after it
    has run. The consent decision is the notice's own predicate, `first_run_stories_expected`
    (one decision for the copy and the queueing); `request_build` still refuses an online send
    without a confirmed dialog as a second layer. Online with no consent: nothing is queued
    and the flag is spent, so it never retries behind the person's back."""
    from kestrel.interview.story_task import request_build
    from kestrel.services.setting_overrides import load_settings_with_db

    if not produced_tier2:
        return False
    async with sm() as s:
        if not await ob.first_run_stories_due(s):
            return False
        if not await _has_deep_read(s):
            return False
        settings = await load_settings_with_db(s)
        queued = False
        if await ob.first_run_stories_expected(s, settings):
            queued = await request_build(s, commit=False)
        await ob.finish_first_run_stories(s)
        await s.commit()
    log.info("first_run_stories", queued=queued)
    return queued
