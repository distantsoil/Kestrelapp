"""The networking graph (SPEC-MODULES 2.3): warm-path finding, a plain graph query over
`contact` and `connection` with no ML. The LinkedIn CSV import (a first-party export the operator
downloads themselves; no scraping, no automation) lives in `kestrel.research.connections`;
`import_linkedin_csv` here is its one-step wrapper. The current warm-path query for an
organization is `connections.connection_paths` (works here now, used to work here); the four
tiers below predate D-UI-115 and the last two are deferred.

Warm-path tiers, ranked:
1. Direct connections currently at the target org.
2. Direct connections formerly at the target org.
3. Direct connections at a company that shares a PE investor with the target org.
4. Connections who share a former employer with a known decision maker at the target org.

Each contact is surfaced once, in its best (lowest-numbered) tier."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Connection, Contact, Interaction, Org
from kestrel.ingest.orgs import normalized_name, resolve_org
from kestrel.research import connections

_DECISION_MAKER_TYPES = ("exec", "hiring_manager", "board", "pe_operator")
_INVESTOR_CATEGORIES = ("pe_investment", "acquisition")


@dataclass
class WarmPath:
    tier: int
    contact: Contact
    reason: str


async def import_linkedin_csv(session: AsyncSession, csv_text: str) -> int:
    """Import a LinkedIn "Connections.csv" first-party export in one step, as before D-UI-115:
    every person with a company becomes a contact at that company's organization (created when
    it does not exist yet, so the warm paths below see them), and importing the same file again
    refreshes rather than duplicates. Returns how many people in the file have a company, or 0
    when the text is not a connections export.

    It runs on the connections importer (`kestrel.research.connections`), so the Email Address
    column is never read, earlier positions are kept as history and a person is stored once. The
    interface previews the diff first (`connections.preview_import`) and does not create
    organizations; this wrapper keeps the one-step contract."""
    try:
        parsed = connections.parse_connections_csv(csv_text)
    except connections.ConnectionsFileError:
        return 0
    # One transaction: the organizations, the import and the links commit together or not at all.
    try:
        resolved: dict[str, int] = {}
        for company in sorted({r.company for r in parsed.rows if r.company}):
            resolved[company] = (await resolve_org(session, name=company, domain=None)).id
        await connections.apply_import(session, parsed, commit=False)
        # The old contract: a company's organization is the one resolve_org names (it also folds
        # "Holdings" and "Group"), whatever the Sure/Possible rule made of it. Link those, unless
        # the operator already ruled on the name.
        for company, org_id in resolved.items():
            await connections.adopt_org(session, company, org_id, commit=False)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return sum(1 for r in parsed.rows if r.company)


def _norm_map(values: list | None) -> dict[str, str]:
    """Normalized-name -> first original display text, for fuzzy former-employer/org matching."""
    out: dict[str, str] = {}
    for v in values or []:
        if not v or not isinstance(v, str):
            continue
        n = normalized_name(v)
        if n and n not in out:
            out[n] = v
    return out


def _investor_tokens(org: Org | None) -> dict[str, str]:
    """org.signals entries categorized as pe_investment/acquisition, keyed by the lowercased
    investor `firm` name mapped to its display value. A shared key between two orgs' sets is a
    genuine PE portfolio overlap (tier 3). The token is ONLY the explicit `firm` identity; a
    `statement` or `source_url` fallback would let two orgs that merely both got 'acquired'
    register as sharing an investor -- a fabricated relationship. Better tier 3 stays silent than
    fires falsely (an absent warm path is invisible; a wrong one wastes the operator's time)."""
    tokens: dict[str, str] = {}
    if org is None or not org.signals:
        return tokens
    for sig in org.signals:
        if not isinstance(sig, dict) or sig.get("category") not in _INVESTOR_CATEGORIES:
            continue
        firm = sig.get("firm")
        if not firm or not isinstance(firm, str) or not firm.strip():
            continue
        key = firm.strip().lower()
        if key not in tokens:
            tokens[key] = firm.strip()
    return tokens


async def warm_paths(
    session: AsyncSession, target_org_id: int, *, limit: int = 25
) -> list[WarmPath]:
    target_org = await session.get(Org, target_org_id)
    if target_org is None:
        return []

    connected_contacts = (
        (
            await session.execute(
                select(Contact).join(Connection, Connection.contact_id == Contact.id).distinct()
            )
        )
        .scalars()
        .all()
    )
    connected_ids = {c.id for c in connected_contacts}

    decision_makers = [
        dm
        for dm in (
            (
                await session.execute(
                    select(Contact).where(
                        Contact.org_id == target_org_id,
                        Contact.contact_type.in_(_DECISION_MAKER_TYPES),
                        connections.not_imported_connection(),
                    )
                )
            )
            .scalars()
            .all()
        )
        if dm.id not in connected_ids
    ]
    dm_employer_maps = [
        (dm, _norm_map(list(dm.former_orgs or []) + [target_org.name])) for dm in decision_makers
    ]

    target_keys = {normalized_name(target_org.name)}
    if target_org.normalized_name:
        target_keys.add(target_org.normalized_name)
    target_tokens = _investor_tokens(target_org)

    org_cache: dict[int, Org | None] = {}

    async def _org(org_id: int | None) -> Org | None:
        if org_id is None:
            return None
        if org_id not in org_cache:
            org_cache[org_id] = await session.get(Org, org_id)
        return org_cache[org_id]

    assigned: dict[int, WarmPath] = {}

    for contact in connected_contacts:
        if contact.id in assigned:
            continue

        if contact.org_id == target_org_id:
            assigned[contact.id] = WarmPath(
                tier=1, contact=contact, reason=f"Currently at {target_org.name}"
            )
            continue

        former_map = _norm_map(contact.former_orgs)
        if target_keys & set(former_map):
            assigned[contact.id] = WarmPath(
                tier=2, contact=contact, reason=f"Formerly at {target_org.name}"
            )
            continue

        conn_org = await _org(contact.org_id)
        conn_tokens = _investor_tokens(conn_org)
        shared_investor = set(conn_tokens) & set(target_tokens)
        if shared_investor:
            key = sorted(shared_investor)[0]
            firm = conn_tokens.get(key) or target_tokens.get(key) or key
            assigned[contact.id] = WarmPath(
                tier=3,
                contact=contact,
                reason=f"Shares an investor ({firm}) with {target_org.name}",
            )
            continue

        for dm, emp_map in dm_employer_maps:
            shared_employer = set(former_map) & set(emp_map)
            if shared_employer:
                key = sorted(shared_employer)[0]
                employer = former_map.get(key) or emp_map.get(key) or key
                assigned[contact.id] = WarmPath(
                    tier=4,
                    contact=contact,
                    reason=f"Shares a former employer ({employer}) with {dm.name}",
                )
                break

    ordered = sorted(assigned.values(), key=lambda w: w.tier)
    return ordered[:limit]


async def record_interaction(
    session: AsyncSession,
    contact_id: int,
    kind: str,
    *,
    opportunity_id: int | None = None,
    direction: str | None = None,
    summary: str | None = None,
    outcome: str | None = None,
) -> Interaction:
    """Append-only interaction log (SPEC-MODULES 2.3)."""
    interaction = Interaction(
        contact_id=contact_id,
        opportunity_id=opportunity_id,
        kind=kind,
        direction=direction,
        summary=summary,
        outcome=outcome,
    )
    session.add(interaction)
    await session.commit()
    return interaction
