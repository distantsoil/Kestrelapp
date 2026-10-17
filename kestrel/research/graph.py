"""The networking graph (SPEC-MODULES 2.3): LinkedIn CSV import (a legitimate first-party export
the operator downloads themselves; no scraping, no automation) and warm-path finding, a plain
graph query over `contact` and `connection` with no ML.

Warm-path tiers, ranked:
1. Direct connections currently at the target org.
2. Direct connections formerly at the target org.
3. Direct connections at a company that shares a PE investor with the target org.
4. Connections who share a former employer with a known decision maker at the target org.

Each contact is surfaced once, in its best (lowest-numbered) tier."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Connection, Contact, Interaction, Org
from kestrel.ingest.orgs import normalized_name, resolve_org

_DECISION_MAKER_TYPES = ("exec", "hiring_manager", "board", "pe_operator")
_INVESTOR_CATEGORIES = ("pe_investment", "acquisition")


@dataclass
class WarmPath:
    tier: int
    contact: Contact
    reason: str


def _find_header_index(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        if "First Name" in line and "Company" in line:
            return i
    return None


async def import_linkedin_csv(session: AsyncSession, csv_text: str) -> int:
    """Parse a LinkedIn "Connections.csv" first-party export. Real exports prepend a few
    "Notes:" preamble lines and a blank line before the actual header row; skip everything up to
    and including the line that names both "First Name" and "Company" columns, then parse the rest
    with csv.DictReader so quoted fields (embedded commas) are handled correctly. Robust to a
    missing/renamed column: DictReader.get() returns None rather than raising.

    Idempotent: a row is upserted on (source='linkedin_csv', normalized name, org), so re-importing
    the same export refreshes title/email and never duplicates a Contact or its Connection."""
    text = csv_text.lstrip("﻿")
    lines = text.splitlines()
    header_idx = _find_header_index(lines)
    if header_idx is None:
        return 0
    body = "\n".join(lines[header_idx:])
    reader = csv.DictReader(io.StringIO(body))

    count = 0
    for row in reader:
        if not row:
            continue
        company = (row.get("Company") or "").strip()
        if not company:
            continue
        first = (row.get("First Name") or "").strip()
        last = (row.get("Last Name") or "").strip()
        name = " ".join(f"{first} {last}".split())
        if not name:
            continue
        email = (row.get("Email Address") or "").strip() or None
        position = (row.get("Position") or "").strip() or None

        org = await resolve_org(session, name=company, domain=None)
        contact = (
            await session.execute(
                select(Contact)
                .where(
                    Contact.source == "linkedin_csv",
                    Contact.org_id == org.id,
                    func.lower(Contact.name) == name.lower(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if contact is None:
            contact = Contact(
                org_id=org.id,
                name=name,
                title=position,
                email=email,
                source="linkedin_csv",
                relationship="known",
            )
            session.add(contact)
            await session.flush()  # need contact.id for the Connection FK
        else:  # the export is first-party and current: refresh what it states, keep the rest
            contact.title = position or contact.title
            contact.email = email or contact.email
        has_connection = (
            await session.execute(
                select(Connection.id).where(Connection.contact_id == contact.id).limit(1)
            )
        ).scalar_one_or_none()
        if has_connection is None:
            session.add(Connection(contact_id=contact.id, how_known="LinkedIn", strength="known"))
        count += 1

    await session.commit()
    return count


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
