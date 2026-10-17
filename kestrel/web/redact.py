"""Confidential-mode redaction for the read surfaces (SPEC-INTERFACES privacy, CLAUDE.md, D-PRIV-1).

When confidential mode is on, the operator's current employer is shown as their "Show it as" text
in list views, the JSON API and MCP. The map and the matching rules live in one place:
`kestrel.docs.current_employer.load_employer_map` builds a `kestrel.privacy.EmployerMap` from the
operator's current marks (or, with none, the inferred current role), and every surface here, the
renders and the model sends use that same object."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.privacy import EmployerMap


async def redaction_map(session: AsyncSession) -> EmployerMap:
    """The current employer map (marks, else the fail-closed inference). Falsy when empty."""
    from kestrel.docs.current_employer import load_employer_map

    return await load_employer_map(session)


def redact_name(name: str | None, mapping: EmployerMap, *, enabled: bool) -> str | None:
    """A whole org-name field: the employer under any spelling ingestion treats as the same
    company (normalized_name, so "Northwind Freight, LLC" and "NORTHWIND FREIGHT" agree with
    deduplication), else a whole-word substitution."""
    if not enabled or not name or not mapping:
        return name
    return mapping.redact_name(name)


def redact_text(text: str | None, mapping: EmployerMap, *, enabled: bool) -> str | None:
    """Every occurrence of the current employer inside free text (title, description, prose)."""
    if not enabled or not text or not mapping:
        return text
    return mapping.redact(text)


def redact_json(value: Any, mapping: EmployerMap, *, enabled: bool) -> Any:
    """Every string leaf of a JSON-shaped value, recursively. Returns a new structure; the input
    (often an ORM attribute) is never mutated."""
    if not enabled or not mapping:
        return value
    return mapping.redact_json(value)


async def confidential_is_inert(session: AsyncSession) -> bool:
    """True when confidential mode is ON but the redaction map is EMPTY, so it redacts nothing.

    The map comes from the operator's own doc sections, so on a fresh install (or before they fill
    in the resume) the checkbox is on, the Settings copy promises the employer is hidden, and every
    surface still shows the real name. A privacy control that appears enabled and does nothing is
    worse than one that is plainly off. `pipeline/tier3.py` already refuses to transmit in this
    state; the read surfaces cannot refuse to render, so they surface it instead. Settings shows a
    warning and `kestrel doctor` reports it.
    """
    enabled, mapping = await confidential(session)
    return enabled and not mapping


async def confidential(session: AsyncSession) -> tuple[bool, EmployerMap]:
    """(enabled, redaction map) for the current request, so any surface can redact the current
    employer with one call. Confidential mode must redact everywhere, not just the pipeline list."""
    from kestrel.services.setting_overrides import load_settings_with_db

    settings = await load_settings_with_db(session)
    if not settings.confidential_mode:
        return False, EmployerMap()
    return True, await redaction_map(session)


_ONCE_KEY = "kestrel.confidential"


async def confidential_once(session: AsyncSession) -> tuple[bool, EmployerMap]:
    """`confidential(session)`, computed once per session and reused (R7): a polled panel that
    redacts titles, search names and the Review list reads the settings and builds the map one
    time per request, not once per block. Only for read paths; a route that changes the mode
    calls `confidential` itself."""
    cached = session.info.get(_ONCE_KEY)
    if cached is None:
        cached = await confidential(session)
        session.info[_ONCE_KEY] = cached
    return cached
