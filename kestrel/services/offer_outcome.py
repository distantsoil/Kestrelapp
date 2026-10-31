"""Closing an offer from the Offers screen, and undoing it (D-UI-122, D-UI-136).

"I accepted", "I declined" and "It expired" each set the offer's own status and close the pursuit
with that outcome, through `pursuit_board.move_to_status` (the one sanctioned way a card changes)
so the move has a status event. An `Outcome` row is written for the learning loop, the same row
Pursuits' outcome capture writes. Reopen puts the offer and the pursuit back and removes that row.

Nothing here contacts anyone: this records what the operator decided (hard rule 1)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import EngagementOffer, Offer, Outcome, Pursuit, Setting
from kestrel.services import pursuit_undo
from kestrel.services.pursuit_board import CLOSED_STATUSES, COLUMNS, move_to_status

# What each action does to the pursuit, per track, and which offer statuses count as open.
OUTCOMES = {
    "accepted": {"employment": "accepted", "consulting": "won"},
    "declined": {"employment": "withdrawn", "consulting": "lost"},
    "expired": {"employment": "withdrawn", "consulting": "no_response"},
}
OPEN_STATUSES = ("verbal", "written")
# The Outcome row an action here wrote carries this marker, so Reopen removes only its own row.
MARKER = "Closed from Offers"
# The stage an offer sits in while it is open, per track: where Reopen sends the pursuit.
_OPEN_STAGE = {"employment": "offer", "consulting": "negotiating"}


@dataclass
class Closed:
    track: str
    offer_id: int
    outcome: str
    restore_offer: str
    restore_pursuit: str
    token: str = ""


def model_for(track: str):
    return {"employment": Offer, "consulting": EngagementOffer}.get(track)


async def close_offer(
    session: AsyncSession, track: str, offer_id: int, outcome: str
) -> Closed | str:
    """Close the offer with `outcome` (accepted, declined or expired). Returns what Undo needs, or
    a plain sentence saying why nothing changed. Flushes; the caller commits."""
    model = model_for(track)
    if model is None or outcome not in OUTCOMES:
        return "Kestrel did not recognize that action."
    offer = await session.get(model, offer_id)
    if offer is None:
        return "That offer no longer exists."
    pursuit = await session.get(Pursuit, offer.pursuit_id)
    if pursuit is None:
        return "That offer's pursuit no longer exists."
    if offer.status not in OPEN_STATUSES:
        return "That offer is already closed."
    final = OUTCOMES[outcome][track]
    if pursuit.status in CLOSED_STATUSES.get(track, ()):
        return "That application is already closed."
    undo = Closed(track, offer_id, outcome, offer.status, pursuit.status)
    snap = await pursuit_undo.snapshot(session, pursuit)
    snap["offer"] = {"track": track, "id": offer.id, "before": offer.status, "after": outcome}
    err = await move_to_status(session, pursuit, final, note=f"Offer {outcome}")
    if err:
        return err
    offer.status = outcome
    pursuit.outcome = final
    pursuit.outcome_stage = _OPEN_STAGE[track]
    session.add(
        Outcome(
            pursuit_id=pursuit.id,
            opportunity_id=pursuit.opportunity_id,
            final_status=final,
            furthest_stage=_OPEN_STAGE[track],
            operator_retrospective=MARKER,
        )
    )
    await session.flush()
    undo.token = await pursuit_undo.save(session, snap)
    return undo


async def undo_close(session: AsyncSession, track: str, offer_id: int, token: str) -> str | None:
    """Undo this close only; a menu Reopen remains a separate deliberate action."""
    row = await session.get(Setting, pursuit_undo.PREFIX + token)
    model = model_for(track)
    offer = await session.get(model, offer_id) if model else None
    data = row.value if row is not None and isinstance(row.value, dict) else {}
    stamp = data.get("offer", {})
    if (
        offer is None
        or stamp.get("track") != track
        or stamp.get("id") != offer_id
        or offer.status != stamp.get("after")
    ):
        return "This can no longer be undone."
    restored, message = await pursuit_undo.restore(session, token, offer_close=True)
    if restored is None:
        return message
    offer.status = stamp["before"]
    await session.flush()
    return None


async def reopen_offer(
    session: AsyncSession, track: str, offer_id: int, *,
    offer_status: str | None = None, pursuit_status: str | None = None,
) -> str | None:
    """Put a closed offer back to open. Returns a sentence when nothing could change, else None.
    `offer_status` and `pursuit_status` are where Undo found them; Reopen from the menu has no
    memory of that and uses verbal and the track's offer stage."""
    model = model_for(track)
    offer = await session.get(model, offer_id) if model is not None else None
    if offer is None:
        return "That offer no longer exists."
    pursuit = await session.get(Pursuit, offer.pursuit_id)
    if pursuit is None:
        return "That offer's pursuit no longer exists."
    if offer.status in OPEN_STATUSES:
        return "That offer is not closed."
    offer.status = offer_status if offer_status in OPEN_STATUSES else "verbal"
    if pursuit.status in CLOSED_STATUSES.get(track, ()):
        # Only a stage this track's board has, and an open one: the value arrives in a link.
        known = {st for col in COLUMNS.get(track, ()) for st in col.statuses}
        known -= set(CLOSED_STATUSES.get(track, ()))
        stage = pursuit_status if pursuit_status in known else _OPEN_STAGE[track]
        err = await move_to_status(session, pursuit, stage, note="Offer reopened")
        if err:
            return err
    pursuit.outcome = pursuit.outcome_stage = pursuit.outcome_reason = None
    pursuit.last_activity_at = utcnow_iso()
    mine = (
        await session.execute(
            select(Outcome)
            .where(Outcome.pursuit_id == pursuit.id, Outcome.operator_retrospective == MARKER)
            .order_by(Outcome.id.desc())
            .limit(1)
        )
    ).scalars().first()
    if mine is not None:  # only the row the latest close wrote
        await session.delete(mine)
    await session.flush()
    return None
