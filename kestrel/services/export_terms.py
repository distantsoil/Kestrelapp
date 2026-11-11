"""Source terms that limit what Kestrel hands on (D-SRC-19).

Remotive's legal notice lets apps share its jobs further only with a link back and Remotive named
as the source, and it forbids passing them to other job sites. Kestrel keeps the safe side of that:
a posting that came from Remotive never leaves this computer through an export or a share feature
(the applications file, the ratings export, the command-line export, the diagnostics bundle, the
MCP tools and the read API). The test is the posting's SOURCE KIND, never a name or a URL.

A role seen on Remotive and on another board is two rows in one dedup group. The other board's row
is its own posting and exports as itself; a record that points at the Remotive row (an application,
a rating) is exported from the other board's row, or left out when there is none.

Also holds the credit shown on a posting (the board's name with a link), for the three boards whose
terms ask for it."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Opportunity, Source

# Source kinds whose postings are never exported or shared (Remotive's terms, REFERENCES.md).
NO_EXPORT_KINDS: frozenset[str] = frozenset({"remotive"})

RESTRICTED_SENTENCE = (
    "This posting comes from a source whose terms do not allow passing it on, so it is not "
    "available here. Open it in Kestrel."
)

# kind -> (name shown, the board's home page). Remotive, Arbeitnow and Jobicy ask for a visible
# link back; the posting's own link points at the board too, and is used first when it is shown.
CREDIT: dict[str, tuple[str, str]] = {
    "remotive": ("Remotive", "https://remotive.com"),
    "arbeitnow": ("Arbeitnow", "https://www.arbeitnow.com"),
    "jobicy": ("Jobicy", "https://jobicy.com"),
    "fourdayweek": ("4 Day Week", "https://4dayweek.io"),
}

# The same list as SQL text, for the diagnostics bundle's raw sqlite query.
NO_EXPORT_SQL = "(" + ",".join(f"'{k}'" for k in sorted(NO_EXPORT_KINDS)) + ")"


def shareable_clause() -> ColumnElement[bool]:
    """WHERE clause: the opportunity may be exported (it is not from a no-export source)."""
    restricted = select(Source.id).where(Source.kind.in_(NO_EXPORT_KINDS))
    return or_(Opportunity.source_id.is_(None), Opportunity.source_id.not_in(restricted))


async def restricted_ids(session: AsyncSession, opp_ids: Iterable[int]) -> set[int]:
    """Which of these opportunity ids came from a no-export source."""
    ids = sorted({i for i in opp_ids if i is not None})
    if not ids:
        return set()
    rows = await session.execute(
        select(Opportunity.id)
        .join(Source, Source.id == Opportunity.source_id)
        .where(Opportunity.id.in_(ids), Source.kind.in_(NO_EXPORT_KINDS))
    )
    return set(rows.scalars())


async def export_copies(
    session: AsyncSession, opp_ids: Iterable[int]
) -> dict[int, Opportunity | None]:
    """For each opportunity id, the row an export may read its fields from: the row itself, the
    best other-board row of the same dedup group when it came from a no-export source, or None
    when it must be left out."""
    ids = sorted({i for i in opp_ids if i is not None})
    out: dict[int, Opportunity | None] = {}
    if not ids:
        return out
    bad = await restricted_ids(session, ids)
    rows = (await session.execute(select(Opportunity).where(Opportunity.id.in_(ids)))).scalars()
    by_id = {o.id: o for o in rows}
    for i in ids:
        opp = by_id.get(i)
        if opp is None:
            out[i] = None
        elif i not in bad:
            out[i] = opp
        else:
            out[i] = await _other_board_copy(session, opp)
    return out


async def _other_board_copy(session: AsyncSession, opp: Opportunity) -> Opportunity | None:
    if not opp.dedup_group_id:
        return None
    members = (
        await session.execute(
            select(Opportunity, Source.kind)
            .join(Source, Source.id == Opportunity.source_id, isouter=True)
            .where(
                Opportunity.dedup_group_id == opp.dedup_group_id,
                Opportunity.id != opp.id,
                Opportunity.track == opp.track,
            )
        )
    ).all()
    from kestrel.ingest.dedup import source_rank  # lazy: keeps the diagnostics bundle light

    usable = [(m, kind) for m, kind in members if kind not in NO_EXPORT_KINDS]
    if not usable:
        return None
    best, _kind = min(usable, key=lambda mk: (source_rank(mk[1]), mk[0].id))
    return best


def credit_for(kind: str | None) -> tuple[str, str] | None:
    """(name, home page) of the board a posting must credit, or None for any other source."""
    return CREDIT.get(kind or "")
