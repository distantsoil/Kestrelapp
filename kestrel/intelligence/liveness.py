"""Liveness checking (SPEC-MODULES 6.3). A cadence task re-fetches the URL of every shortlisted and
applied opportunity (default every 3 days). An HTTP 404/410 or a page whose text says the role is
no longer open is an "expired" read; a posting is marked closed only on the SECOND consecutive
expired read (D-INTEL-1), the first being a strike that is re-read after a short interval. A
shortlisted opportunity going closed raises a notification that says why, so the operator never
prepares for a role that no longer exists, and the close date feeds the ghost model.

Only public pages, politely (constraint 7): fetching goes through the shared HttpClient, which sends
a descriptive User-Agent, respects robots.txt, and rate-limits. This module never acts on the
operator's behalf; it reads a public page and records what it finds."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx
import structlog
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.db.base import iso, utcnow_iso
from kestrel.db.models import Notification, Opportunity, Pursuit
from kestrel.ingest.http import UnsafeURLError
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

# Why a posting closed, in the words the Posting tab and the notification use (no em dashes).
CLOSED_REASON_WORDS = {
    "http_404": "The posting page returned not found twice",
    "http_410": "The posting page reported it was removed twice",
    "page_text": "The posting page said the role is filled twice",
    "ats_missing": "The job board no longer lists the role",
}


def closed_reason_text(reason: str | None) -> str:
    """The plain-English reason a posting closed, or "" when none was recorded."""
    return CLOSED_REASON_WORDS.get(reason or "", "")


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
    """Re-fetch the opportunity URL and update its liveness. Returns the new liveness.

    An "expired" read (404/410 or a page saying the role is closed) is a strike on the first
    occurrence and closes the posting on the second consecutive one, recording the reason; a live
    read resets the count. A failed fetch that is not a definitive 404/410 (a 5xx, 429, 403 or a
    network error) leaves liveness AND the strike count unchanged (a transient error is not a
    closed role, and it does not forgive an earlier expired read either). A close on a
    shortlisted/pursued opportunity writes a notification that says why."""
    now = utcnow_iso()
    previous = opp.liveness

    if not opp.url:
        opp.liveness = "unknown"
        opp.liveness_checked_at = now
        return "unknown"

    expired: str | None = None  # the closed_reason this read would give, when it is expired
    try:
        body = await http.get_text(opp.url, check_robots=True, public_only=True)
        if page_says_closed(body):
            expired = "page_text"
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in _CLOSED_STATUS:
            expired = f"http_{status}"
        else:
            # A 5xx/429/403 is a transient failure, not a closed role: leave liveness unchanged.
            log.debug("liveness_transient", url=opp.url, status=status)
            return previous
    except (httpx.HTTPError, httpx.InvalidURL, UnsafeURLError) as exc:
        log.debug("liveness_fetch_failed", url=opp.url, error=str(exc))
        return previous

    opp.liveness_checked_at = now
    if expired is None:  # live: forgive any earlier strike and any earlier close
        opp.liveness = "live"
        opp.liveness_strikes = 0
        opp.closed_reason = None
        return "live"

    if previous == "closed":  # already closed: stays closed, no second notification
        opp.closed_reason = opp.closed_reason or expired
        return "closed"

    if (opp.liveness_strikes or 0) < 1:  # the first expired read is a strike, not a close
        opp.liveness_strikes = 1
        log.info("liveness_strike", opportunity_id=opp.id, reason=expired)
        return previous

    opp.liveness = "closed"
    opp.liveness_strikes = 2
    opp.closed_reason = expired
    if notify:
        pursuit = await _pursuit_for(session, opp.id)
        if pursuit is not None:  # only a shortlisted/pursued opportunity is worth interrupting for
            session.add(
                Notification(
                    kind="liveness_closed",
                    severity="warning",
                    title=f"Closed: {opp.title or opp.url}",
                    body=(
                        f"{opp.org_name or 'This opportunity'} appears to have closed. "
                        f"{closed_reason_text(expired)}. Stop preparing for it."
                    ),
                    opportunity_id=opp.id,
                    pursuit_id=pursuit.id,
                    delivered_channels=[],
                )
            )
    return "closed"


async def due_for_liveness(
    session: AsyncSession,
    *,
    cadence_days: int = 3,
    recheck_hours: int = 24,
    now: str | None = None,
    limit: int = 200,
) -> list[Opportunity]:
    """Shortlisted/pursued opportunities (those with a pursuit) that are not already closed and were
    last checked longer ago than the cadence (or never checked). A row with one strike (one expired
    read) is due again after the shorter `recheck_hours`, so a real close is confirmed in a day
    rather than three. These are what a liveness run re-fetches; every unit is an opportunity row,
    so a killed run simply re-selects what is due."""
    clock = datetime.fromisoformat((now or utcnow_iso()).replace("Z", "+00:00"))
    cutoff = clock - timedelta(days=cadence_days)
    strike_cutoff = clock - timedelta(hours=recheck_hours)
    # The due filter runs in SQL, BEFORE the limit, oldest check first (never checked ahead of
    # everything): limiting first and filtering in Python would leave every pursued opportunity
    # past the first `limit` rowids permanently unchecked. Stored timestamps are fixed-width ISO
    # UTC (db.base.iso), so the string comparison is a correct coarse filter; the exact parsed
    # comparison below still decides.
    cutoff_iso = iso(cutoff)
    strike_cutoff_iso = iso(strike_cutoff)
    striked = Opportunity.liveness_strikes >= 1
    rows = (
        (
            await session.execute(
                select(Opportunity)
                .join(Pursuit, Pursuit.opportunity_id == Opportunity.id)
                .where(
                    Opportunity.liveness != "closed",
                    or_(
                        Opportunity.liveness_checked_at.is_(None),
                        and_(~striked, Opportunity.liveness_checked_at < cutoff_iso),
                        and_(striked, Opportunity.liveness_checked_at < strike_cutoff_iso),
                    ),
                )
                .order_by(Opportunity.liveness_checked_at.asc().nulls_first(), Opportunity.id)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    def _due(checked_at: str | None, strikes: int) -> bool:
        # Compare parsed datetimes, never raw ISO strings: a microsecond-bearing timestamp sorts
        # before a microsecond-free one lexically ('.' > '+'). A timestamp that will not parse is
        # treated as due (re-check rather than skip).
        if not checked_at:
            return True
        try:
            checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
            return checked < (strike_cutoff if strikes >= 1 else cutoff)
        except ValueError:
            return True

    return [o for o in rows if _due(o.liveness_checked_at, o.liveness_strikes or 0)]


async def run_liveness_cycle(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Any, http: Any
) -> dict:
    """One liveness pass for the serve loop: re-check everything due (per
    settings.liveness.cadence_days, or settings.liveness.recheck_hours for a row with one expired
    read) and fold the result into the ghost score. Each opportunity is
    its own transaction, so a kill mid-cycle loses nothing but the item in flight and the next pass
    re-selects what is still due (hard rule 5). One item failing is counted and logged; it never
    stops the cycle."""
    cadence_days = int(settings.liveness.cadence_days)
    recheck_hours = int(settings.liveness.recheck_hours)
    counts = {"due": 0, "checked": 0, "closed": 0, "errors": 0}
    async with sessionmaker() as session:
        due_ids = [o.id for o in await due_for_liveness(
            session, cadence_days=cadence_days, recheck_hours=recheck_hours
        )]
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
