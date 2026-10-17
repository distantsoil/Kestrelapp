"""Liveness checking (SPEC-MODULES 6.3). A cadence task re-fetches the URL of every shortlisted and
applied opportunity (default every 3 days). An HTTP 404/410, a page whose text says the role is no
longer open, or an ATS that no longer returns the id marks the opportunity closed; a shortlisted
opportunity going closed raises a notification, so the operator never prepares for a role that no
longer exists, and the close date feeds the ghost model.

Only public pages, politely (constraint 7): fetching goes through the shared HttpClient, which sends
a descriptive User-Agent, respects robots.txt, and rate-limits. This module never acts on the
operator's behalf; it reads a public page and records what it finds."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx
import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.db.base import iso, utcnow_iso
from kestrel.db.models import Notification, Opportunity, Pursuit
from kestrel.intelligence.ghost import recompute_ghost

log = structlog.get_logger()

_CLOSED_PHRASES = (
    "no longer accepting",
    "position filled",
    "this job is closed",
    "this position has been filled",
    "no longer available",
)
_CLOSED_STATUS = (404, 410)


def page_says_closed(body: str) -> bool:
    low = (body or "").lower()
    return any(phrase in low for phrase in _CLOSED_PHRASES)


async def _pursuit_for(session: AsyncSession, opp_id: int) -> Pursuit | None:
    return (
        await session.execute(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
    ).scalar_one_or_none()


async def check_liveness(
    session: AsyncSession, http: Any, opp: Opportunity, *, notify: bool = True
) -> str:
    """Re-fetch the opportunity URL and update its liveness. Returns the new liveness. A transition
    to `closed` on a shortlisted/pursued opportunity writes a notification. A failed fetch that is
    not a definitive 404/410 leaves liveness unchanged (a transient error is not a closed role)."""
    now = utcnow_iso()
    previous = opp.liveness
    new_state = previous

    if not opp.url:
        opp.liveness = "unknown"
        opp.liveness_checked_at = now
        return "unknown"

    try:
        body = await http.get_text(opp.url, check_robots=True)
        new_state = "closed" if page_says_closed(body) else "live"
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in _CLOSED_STATUS:
            new_state = "closed"
        else:
            # A 5xx/429/403 is a transient failure, not a closed role: leave liveness unchanged.
            log.debug("liveness_transient", url=opp.url, status=status)
            return previous
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        log.debug("liveness_fetch_failed", url=opp.url, error=str(exc))
        return previous

    opp.liveness = new_state
    opp.liveness_checked_at = now

    if notify and new_state == "closed" and previous != "closed":
        pursuit = await _pursuit_for(session, opp.id)
        if pursuit is not None:  # only a shortlisted/pursued opportunity is worth interrupting for
            session.add(
                Notification(
                    kind="liveness_closed",
                    severity="warning",
                    title=f"Closed: {opp.title or opp.url}",
                    body=(
                        f"{opp.org_name or 'This opportunity'} appears to have closed "
                        "(the posting is no longer live). Stop preparing for it."
                    ),
                    opportunity_id=opp.id,
                    pursuit_id=pursuit.id,
                    delivered_channels=[],
                )
            )
    return new_state


async def due_for_liveness(
    session: AsyncSession, *, cadence_days: int = 3, now: str | None = None, limit: int = 200
) -> list[Opportunity]:
    """Shortlisted/pursued opportunities (those with a pursuit) that are not already closed and were
    last checked longer ago than the cadence (or never checked). These are what a liveness run
    re-fetches; every unit is an opportunity row, so a killed run simply re-selects what is due."""
    cutoff = datetime.fromisoformat((now or utcnow_iso()).replace("Z", "+00:00")) - timedelta(
        days=cadence_days
    )
    # The due filter runs in SQL, BEFORE the limit, oldest check first (never checked ahead of
    # everything): limiting first and filtering in Python would leave every pursued opportunity
    # past the first `limit` rowids permanently unchecked. Stored timestamps are fixed-width ISO
    # UTC (db.base.iso), so the string comparison is a correct coarse filter; the exact parsed
    # comparison below still decides.
    cutoff_iso = iso(cutoff)
    rows = (
        (
            await session.execute(
                select(Opportunity)
                .join(Pursuit, Pursuit.opportunity_id == Opportunity.id)
                .where(
                    Opportunity.liveness != "closed",
                    or_(
                        Opportunity.liveness_checked_at.is_(None),
                        Opportunity.liveness_checked_at < cutoff_iso,
                    ),
                )
                .order_by(Opportunity.liveness_checked_at.asc().nulls_first(), Opportunity.id)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    def _due(checked_at: str | None) -> bool:
        # Compare parsed datetimes, never raw ISO strings: a microsecond-bearing timestamp sorts
        # before a microsecond-free one lexically ('.' > '+'). A timestamp that will not parse is
        # treated as due (re-check rather than skip).
        if not checked_at:
            return True
        try:
            return datetime.fromisoformat(checked_at.replace("Z", "+00:00")) < cutoff
        except ValueError:
            return True

    return [o for o in rows if _due(o.liveness_checked_at)]


async def run_liveness_cycle(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Any, http: Any
) -> dict:
    """One liveness pass for the serve loop: re-check everything due (per
    settings.liveness.cadence_days) and fold the result into the ghost score. Each opportunity is
    its own transaction, so a kill mid-cycle loses nothing but the item in flight and the next pass
    re-selects what is still due (hard rule 5). One item failing is counted and logged; it never
    stops the cycle."""
    cadence_days = int(settings.liveness.cadence_days)
    counts = {"due": 0, "checked": 0, "closed": 0, "errors": 0}
    async with sessionmaker() as session:
        due_ids = [o.id for o in await due_for_liveness(session, cadence_days=cadence_days)]
    counts["due"] = len(due_ids)
    for opp_id in due_ids:
        try:
            async with sessionmaker() as session:
                opp = await session.get(Opportunity, opp_id)
                if opp is None:
                    continue
                state = await check_liveness(session, http, opp)
                await recompute_ghost(session, opp)
                await session.commit()
            counts["checked"] += 1
            if state == "closed":
                counts["closed"] += 1
        except Exception as exc:  # one bad URL/row never stops the cadence pass
            counts["errors"] += 1
            log.warning("liveness_item_error", opportunity_id=opp_id, error=str(exc))
    return counts
