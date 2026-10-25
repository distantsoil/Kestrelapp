"""The LinkedIn connections import, phases 1 and 2 (D-UI-115, D-UI-131, SPEC-MODULES 2.3).

The operator downloads their own Connections.csv from LinkedIn and gives it to Kestrel. Kestrel
never signs in to LinkedIn and never looks anyone up. This module:

* parses the file (`parse_connections_csv`), skipping the "Notes:" preamble a real export starts
  with. **The Email Address column is never read:** only the cells for first name, last name,
  company, position and connected-on are taken by header position, so no email can reach a
  return value, the database or a log line. The profile URL column is not read either, since the
  spec stores name, company, position and connected-on only;
* diffs the file against the previous import (`preview_import`): new people, people whose company
  or position changed, and people not in this file. Nothing is written by a preview;
* applies it in one transaction (`apply_import`): phase 1 creates and updates `contact` rows
  (`source` = `linkedin_import`) with their current `connection_position`; phase 2 keeps an
  earlier position when a person's company changes, which is what "used to work here" reads;
* matches company names to organizations (Sure or Possible, never applied silently) and
  remembers the operator's rulings (`confirm_match`, `set_match`, `reject_match`);
* answers `connection_paths(org)`: who works there now and who used to, as far as imports show;
* removes every imported row and nothing else (`remove_all_imported`).

Functions that write commit on success and roll back on error, so each is one transaction."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date

import structlog
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import (
    Connection,
    ConnectionPosition,
    Contact,
    ContactImport,
    Interaction,
    Org,
    OutreachDraft,
)
from kestrel.errors import UserFacingError

log = structlog.get_logger()

SOURCE = "linkedin_import"
MAX_FIELD = 200
MAX_ROWS = 100_000

MATCH_SURE = "sure"
MATCH_POSSIBLE = "possible"
MATCH_MANUAL = "manual"
MATCH_NONE = "none"


class ConnectionsFileError(UserFacingError, ValueError):
    """The file is not a LinkedIn connections export Kestrel can read."""


# ---------------------------------------------------------------------------------- parsing
@dataclass(frozen=True)
class ParsedConnection:
    """One row of the file. There is no email field, by design (D-UI-131)."""

    first_name: str
    last_name: str
    company: str | None
    position: str | None
    connected_on: str | None  # ISO date, or None when the file's date could not be read

    @property
    def name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p)


@dataclass
class ParsedFile:
    rows: list[ParsedConnection] = field(default_factory=list)
    skipped_no_name: int = 0  # rows with no name at all: counted, not stored


_WANTED = {
    "first name": "first_name",
    "last name": "last_name",
    "company": "company",
    "position": "position",
    "connected on": "connected_on",
}
# Month names are matched from this table, never with strptime's %b, which follows the
# computer's locale ("mar" is "mars" in French) and would silently lose every date.
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
_DMY = re.compile(r"^(\d{1,2})[ -]([A-Za-z]{3,9})\.?,?[ -](\d{4})$")
_MDY = re.compile(r"^([A-Za-z]{3,9})\.? (\d{1,2}),? (\d{4})$")


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return " ".join(row[idx].split())[:MAX_FIELD]


def parse_connected_on(text: str | None) -> str | None:
    """The file's "04 Mar 2023" (and a few other spellings) as an ISO date, else None."""
    raw = " ".join((text or "").split())
    if not raw:
        return None
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        pass
    if m := _DMY.match(raw):
        day, month, year = m.group(1), m.group(2), m.group(3)
    elif m := _MDY.match(raw):
        month, day, year = m.group(1), m.group(2), m.group(3)
    else:
        return None
    number = _MONTHS.get(month[:3].lower())
    if number is None:
        return None
    try:
        return date(int(year), number, int(day)).isoformat()
    except ValueError:
        return None


def _find_header(lines: list[str]) -> tuple[int, dict[str, int]] | None:
    """The header line's index and the column index of each wanted column. A header is the
    first line whose own cells include both "First Name" and "Company": the preamble is a
    single long cell, so prose that merely mentions those words never matches."""
    for i, line in enumerate(lines):
        try:
            cells = next(csv.reader([line]))
        except (csv.Error, StopIteration):
            continue
        names = [c.strip().lower() for c in cells]
        if "first name" in names and "company" in names:
            cols = {}
            for idx, name in enumerate(names):
                key = _WANTED.get(name)
                if key and key not in cols:
                    cols[key] = idx
            return i, cols
    return None


def parse_connections_csv(csv_text: str) -> ParsedFile:
    """Read a LinkedIn Connections.csv. Raises `ConnectionsFileError` when no header row is
    found. The Email Address and URL columns are never read (see the module note)."""
    text = (csv_text or "").lstrip("﻿")
    lines = text.splitlines(keepends=True)
    found = _find_header(lines)
    if found is None:
        raise ConnectionsFileError(
            "That file does not look like a LinkedIn connections export. It needs a header row "
            "with First Name and Company columns."
        )
    header_idx, cols = found
    # The rest of the ORIGINAL text, line endings intact: a quoted field can hold a newline or a
    # separator that splitlines() also splits on, and re-joining with "\n" would change it.
    body = "".join(lines[header_idx + 1 :])
    out = ParsedFile()
    try:
        for row in csv.reader(io.StringIO(body)):
            if not row or not any(c.strip() for c in row):
                continue
            if len(out.rows) + out.skipped_no_name >= MAX_ROWS:
                # Never truncate: with "remove people not in this file" a cut-off file would
                # delete everyone past the cutoff.
                raise ConnectionsFileError(
                    f"That file has more than {MAX_ROWS:,} connections, which is more than "
                    "Kestrel imports at once. Nothing was imported."
                )
            first = _cell(row, cols.get("first_name"))
            last = _cell(row, cols.get("last_name"))
            if not (first or last):
                out.skipped_no_name += 1
                continue
            out.rows.append(
                ParsedConnection(
                    first_name=first,
                    last_name=last,
                    company=_cell(row, cols.get("company")) or None,
                    position=_cell(row, cols.get("position")) or None,
                    connected_on=parse_connected_on(_cell(row, cols.get("connected_on"))),
                )
            )
    except csv.Error as exc:
        # The message of a csv error can quote file text; say it in our own words.
        log.warning("connections_csv_unreadable", error_type=type(exc).__name__)
        raise ConnectionsFileError("That file could not be read as a CSV.") from exc
    return out


# ------------------------------------------------------------------------ company matching
_LEGAL_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "ltd", "limited", "co", "corp", "corporation", "company",
    "plc", "lp", "llp", "gmbh", "pty",
})
_NONWORD = re.compile(r"[^a-z0-9]+")


def company_tokens(name: str | None) -> tuple[str, ...]:
    """A company name as comparable words: lower case, "&" read as "and", punctuation dropped,
    a leading "the" and trailing legal suffixes (Inc, LLC, Co, Corp, ...) ignored."""
    text = (name or "").lower().replace("&", " and ").replace("'", "").replace("’", "")
    words = [w for w in _NONWORD.sub(" ", text).split() if w]
    while len(words) > 1 and words[0] == "the":
        words.pop(0)
    while len(words) > 1 and (words[-1] in _LEGAL_SUFFIXES or words[-1] == "and"):
        words.pop()
    return tuple(words)


def company_key(name: str | None) -> str:
    return " ".join(company_tokens(name))


def _contains(longer: tuple[str, ...], shorter: tuple[str, ...]) -> bool:
    n = len(shorter)
    return n > 0 and any(longer[i : i + n] == shorter for i in range(len(longer) - n + 1))


def _is_possible(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """One name contains the other as whole words, or both open with the same two words."""
    if not a or not b or a == b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(" ".join(shorter)) >= 3 and _contains(longer, shorter):
        return True
    return len(a) >= 2 and len(b) >= 2 and a[:2] == b[:2]


@dataclass(frozen=True)
class CompanyMatch:
    """How one company name from the file maps to an organization."""

    company_name: str
    match: str  # sure | possible | manual | none
    org_id: int | None
    org_name: str | None = None
    decided: bool = False  # the operator already ruled on this name; the ruling is reused
    # The operator chose an organization for this name and it has since been deleted. The name is
    # left unmatched and flagged, never matched again behind their back.
    ruling_lost: bool = False

    @property
    def needs_confirmation(self) -> bool:
        return self.match == MATCH_POSSIBLE

    @property
    def needs_attention(self) -> bool:
        return self.needs_confirmation or self.ruling_lost


class _OrgIndex:
    """Organizations as token tuples, for Sure and Possible matching."""

    def __init__(self, orgs: list[tuple[int, str]]) -> None:
        self.names = {oid: name for oid, name in orgs}
        self.tokens = [(oid, company_tokens(name)) for oid, name in sorted(orgs)]

    def best(self, company: str) -> tuple[str, int | None]:
        tokens = company_tokens(company)
        if not tokens:
            return MATCH_NONE, None
        for oid, otoks in self.tokens:
            if otoks == tokens:
                return MATCH_SURE, oid
        best: tuple[int, int] | None = None  # (length gap, org id): closest name, then oldest org
        for oid, otoks in self.tokens:
            if _is_possible(tokens, otoks):
                cand = (abs(len(tokens) - len(otoks)), oid)
                if best is None or cand < best:
                    best = cand
        if best is not None:
            return MATCH_POSSIBLE, best[1]
        return MATCH_NONE, None


async def _org_index(session: AsyncSession) -> _OrgIndex:
    rows = (await session.execute(select(Org.id, Org.name))).all()
    return _OrgIndex([(int(i), str(n)) for i, n in rows])


async def _decided(session: AsyncSession) -> dict[str, tuple[str, int | None]]:
    """The operator's earlier rulings by company key: (match, org_id), newest wins."""
    rows = (
        await session.execute(
            select(
                ConnectionPosition.company_name,
                ConnectionPosition.match,
                ConnectionPosition.org_id,
            )
            .where(ConnectionPosition.operator_decided.is_(True))
            .order_by(ConnectionPosition.id.asc())
        )
    ).all()
    out: dict[str, tuple[str, int | None]] = {}
    for name, match, org_id in rows:
        key = company_key(name)
        if key:
            out[key] = (match, org_id)
    return out


def _resolve(
    company: str | None,
    index: _OrgIndex,
    decided: dict[str, tuple[str, int | None]],
) -> CompanyMatch:
    if not company or not company_key(company):
        return CompanyMatch(company or "", MATCH_NONE, None)
    ruling = decided.get(company_key(company))
    if ruling is not None:
        match, org_id = ruling
        if match == MATCH_MANUAL and org_id in index.names:
            return CompanyMatch(company, MATCH_MANUAL, org_id, index.names[org_id], True)
        if match == MATCH_NONE:
            return CompanyMatch(company, MATCH_NONE, None, None, True)
        if match == MATCH_MANUAL:  # the chosen organization no longer exists
            return CompanyMatch(company, MATCH_NONE, None, None, False, True)
    match, org_id = index.best(company)
    return CompanyMatch(company, match, org_id, index.names.get(org_id) if org_id else None)


# --------------------------------------------------------------------------------- the diff
@dataclass(frozen=True)
class Person:
    name: str
    company: str | None
    position: str | None
    connected_on: str | None


@dataclass(frozen=True)
class ChangedPerson:
    name: str
    old_company: str | None
    old_position: str | None
    new_company: str | None
    new_position: str | None
    connected_on: str | None


@dataclass
class ImportPreview:
    """What applying a file would do. `companies` are the distinct company names in the file with
    their match, so a Possible one can be shown for the operator to confirm."""

    row_count: int = 0
    skipped_no_name: int = 0
    new: list[Person] = field(default_factory=list)
    changed: list[ChangedPerson] = field(default_factory=list)
    missing: list[Person] = field(default_factory=list)
    n_unchanged: int = 0
    n_no_company: int = 0
    companies: list[CompanyMatch] = field(default_factory=list)
    first_import: bool = True


def _person_key(name: str, connected_on: str | None) -> tuple[str, str]:
    return (" ".join(name.lower().split()), connected_on or "")


def _same_text(a: str | None, b: str | None) -> bool:
    return " ".join((a or "").lower().split()) == " ".join((b or "").lower().split())


@dataclass
class _Stored:
    contact: Contact
    position: ConnectionPosition


async def _stored_people(session: AsyncSession) -> dict[tuple[str, str], list[_Stored]]:
    rows = (
        await session.execute(
            select(Contact, ConnectionPosition)
            .join(ConnectionPosition, ConnectionPosition.contact_id == Contact.id)
            .where(Contact.source == SOURCE, ConnectionPosition.is_current.is_(True))
            .order_by(Contact.id.asc())
        )
    ).all()
    out: dict[tuple[str, str], list[_Stored]] = {}
    for contact, pos in rows:
        out.setdefault(_person_key(contact.name, pos.connected_on), []).append(
            _Stored(contact, pos)
        )
    return out


def _pair(
    parsed: ParsedFile, stored: dict[tuple[str, str], list[_Stored]]
) -> tuple[list[tuple[ParsedConnection, _Stored | None]], list[_Stored]]:
    """Each file row with its stored counterpart or None, and the stored people no row claimed.

    First by name plus connected-on date (the first unused one when two people share both). A row
    that found nobody then falls back to the same name when either side has no readable date, or
    when both are at the same company, so a date that became readable (or was never stored, as
    for contacts from the older importer) does not turn a person into a duplicate plus a
    "missing" one, which the remove option would then delete."""
    pool = {k: list(v) for k, v in stored.items()}
    paired: list[tuple[ParsedConnection, _Stored | None]] = []
    for row in parsed.rows:
        bucket = pool.get(_person_key(row.name, row.connected_on))
        paired.append((row, bucket.pop(0) if bucket else None))
    # The unclaimed stored people, indexed by name, so each row looks at its own name only.
    by_name: dict[str, list[_Stored]] = {}
    for bucket in pool.values():
        for cand in bucket:
            by_name.setdefault(_person_key(cand.contact.name, None)[0], []).append(cand)
    claimed: set[int] = set()
    for i, (row, found) in enumerate(paired):
        if found is not None:
            continue
        key = company_key(row.company)
        fits = [
            c for c in by_name.get(_person_key(row.name, None)[0], [])
            if id(c) not in claimed
            and (not c.position.connected_on or not row.connected_on
                 or key == company_key(c.position.company_name))
        ]
        # Exactly one candidate or none: with several, which one this row is cannot be told,
        # and merging two people is worse than adding one (the new row is just a new person).
        if len(fits) == 1:
            claimed.add(id(fits[0]))
            paired[i] = (row, fits[0])
    leftover = [s for bucket in pool.values() for s in bucket if id(s) not in claimed]
    return paired, leftover


def _is_changed(row: ParsedConnection, stored: _Stored) -> bool:
    pos = stored.position
    return company_key(row.company) != company_key(pos.company_name) or not _same_text(
        row.position, pos.title
    )


async def preview_import(session: AsyncSession, parsed: ParsedFile) -> ImportPreview:
    """The diff against the previous import. Writes nothing."""
    stored = await _stored_people(session)
    paired, leftover = _pair(parsed, stored)
    index = await _org_index(session)
    decided = await _decided(session)
    prev = (await session.execute(select(func.count()).select_from(ContactImport))).scalar_one()
    out = ImportPreview(
        row_count=len(parsed.rows), skipped_no_name=parsed.skipped_no_name, first_import=prev == 0
    )
    seen: dict[str, CompanyMatch] = {}
    for row, existing in paired:
        person = Person(row.name, row.company, row.position, row.connected_on)
        if not company_key(row.company):
            out.n_no_company += 1
        else:
            seen.setdefault(company_key(row.company), _resolve(row.company, index, decided))
        if existing is None:
            out.new.append(person)
        elif _is_changed(row, existing):
            out.changed.append(
                ChangedPerson(
                    row.name, existing.position.company_name, existing.position.title,
                    row.company, row.position, row.connected_on,
                )
            )
        else:
            out.n_unchanged += 1
    for s in leftover:
        out.missing.append(
            Person(s.contact.name, s.position.company_name, s.position.title,
                   s.position.connected_on)
        )
    out.companies = sorted(seen.values(), key=lambda c: c.company_name.lower())
    return out


# -------------------------------------------------------------------------------- applying
KEPT_SOURCE = "linkedin_import_kept"


def not_imported_connection():
    """SQL clause: a contact the company made public, never an imported connection. Every MCP
    tool, export, API route and online prompt context reads contacts through this."""
    return func.coalesce(Contact.source, "").not_in((SOURCE, KEPT_SOURCE))


async def _operator_written(session: AsyncSession, contact_ids: list[int]) -> set[int]:
    """Ids among `contact_ids` that carry something the operator wrote: an interaction, an
    outreach draft, notes, or a last-contacted date. These are never deleted by an import or by
    Remove all contacts."""
    out: set[int] = set()
    for start in range(0, len(contact_ids), 500):
        chunk = contact_ids[start : start + 500]
        out.update((await session.execute(
            select(Interaction.contact_id).where(Interaction.contact_id.in_(chunk)))).scalars())
        out.update((await session.execute(
            select(OutreachDraft.contact_id).where(OutreachDraft.contact_id.in_(chunk)))).scalars())
        out.update((await session.execute(
            select(Contact.id).where(
                Contact.id.in_(chunk),
                or_(func.coalesce(Contact.notes, "") != "", Contact.last_contacted_at.is_not(None)),
            ))).scalars())
    return out


async def _remove_contacts(
    session: AsyncSession, contact_ids: list[int], *, detach_kept: bool
) -> tuple[int, int]:
    """Delete bare imported contacts (and their positions and connection rows). A contact with
    operator-written records is never deleted: `detach_kept` False leaves it exactly as it is
    (a person missing from a newer file); True drops its import rows and re-labels it as no
    longer an import (Remove all contacts). Returns (removed, kept)."""
    if not contact_ids:
        return 0, 0
    written = await _operator_written(session, contact_ids)
    bare = [i for i in contact_ids if i not in written]
    for start in range(0, len(bare), 500):
        chunk = bare[start : start + 500]
        await session.execute(
            delete(ConnectionPosition).where(ConnectionPosition.contact_id.in_(chunk))
        )
        await session.execute(delete(Connection).where(Connection.contact_id.in_(chunk)))
        await session.execute(delete(Contact).where(Contact.id.in_(chunk)))
    if detach_kept and written:
        kept = list(written)
        for start in range(0, len(kept), 500):
            chunk = kept[start : start + 500]
            await session.execute(
                delete(ConnectionPosition).where(ConnectionPosition.contact_id.in_(chunk))
            )
            await session.execute(delete(Connection).where(Connection.contact_id.in_(chunk)))
            await session.execute(
                update(Contact).where(Contact.id.in_(chunk)).values(source=KEPT_SOURCE)
            )
    return len(bare), len(written)


async def apply_import(
    session: AsyncSession,
    parsed: ParsedFile,
    *,
    file_name: str | None = None,
    keep_missing: bool = True,
    commit: bool = True,
) -> ContactImport:
    """Write the file: new people, changed positions (the earlier one is kept as history), and
    the chosen handling of people not in the file. One transaction; nothing is stored on error.

    `keep_missing` True leaves people who are not in the file as they were; False removes them
    and their positions, except anyone with something the operator wrote (an interaction, a
    draft, notes), who stays. The file itself is not kept. `commit` False flushes only and leaves
    the transaction to the caller (who rolls back on error)."""
    try:
        stored = await _stored_people(session)
        paired, leftover = _pair(parsed, stored)
        index = await _org_index(session)
        decided = await _decided(session)

        batch = ContactImport(
            imported_at=utcnow_iso(),
            file_name=" ".join(str(file_name).split())[:MAX_FIELD] if file_name else None,
            row_count=len(parsed.rows),
            kept_missing=keep_missing,
        )
        session.add(batch)
        await session.flush()
        n_new = n_changed = 0

        for row, existing in paired:
            m = _resolve(row.company, index, decided)
            linked_org = m.org_id if m.match in (MATCH_SURE, MATCH_MANUAL) else None
            if existing is None:
                contact = Contact(
                    name=row.name, title=row.position, org_id=linked_org, source=SOURCE,
                    relationship="known", email=None,
                )
                session.add(contact)
                await session.flush()
                session.add(Connection(contact_id=contact.id, how_known="LinkedIn",
                                       strength="known"))
                session.add(_position(contact.id, row, m, batch.id))
                n_new += 1
                continue
            pos = existing.position
            if pos.connected_on is None and row.connected_on:
                pos.connected_on = row.connected_on  # a date that was not known before
            if not _is_changed(row, existing):
                pos.last_import_id = batch.id
                continue
            n_changed += 1
            if company_key(row.company) == company_key(pos.company_name):
                # Same company, new title: correct the current row in place. No history row,
                # since the person has not left.
                pos.title = row.position
                pos.company_name = row.company
                pos.last_import_id = batch.id
                existing.contact.title = row.position
                continue
            # A new company: the earlier position stays as history (is_current false, with the
            # import that last saw it), and a new current row starts here.
            pos.is_current = False
            session.add(_position(existing.contact.id, row, m, batch.id))
            existing.contact.title = row.position
            existing.contact.org_id = linked_org

        if not keep_missing:
            await _remove_contacts(session, [s.contact.id for s in leftover], detach_kept=False)
        batch.n_new, batch.n_changed, batch.n_missing = n_new, n_changed, len(leftover)
        if commit:
            await session.commit()
        else:
            await session.flush()
    except Exception:
        if commit:
            await session.rollback()
        raise
    # Counts only: no name, company or email ever goes to the log (D-UI-131).
    log.info(
        "connections_import_applied", rows=batch.row_count, new=batch.n_new,
        changed=batch.n_changed, missing=batch.n_missing, kept_missing=keep_missing,
    )
    return batch


def _position(
    contact_id: int, row: ParsedConnection, m: CompanyMatch, import_id: int
) -> ConnectionPosition:
    return ConnectionPosition(
        contact_id=contact_id,
        company_name=row.company,
        org_id=m.org_id,
        match=m.match,
        operator_decided=m.decided,
        title=row.position,
        connected_on=row.connected_on,
        first_import_id=import_id,
        last_import_id=import_id,
        is_current=True,
    )


# ------------------------------------------------------------------- the operator's rulings
async def _positions_for(session: AsyncSession, company_name: str) -> list[ConnectionPosition]:
    key = company_key(company_name)
    if not key:
        return []
    rows = (
        await session.execute(
            select(ConnectionPosition)
            .join(Contact, Contact.id == ConnectionPosition.contact_id)
            .where(Contact.source == SOURCE, ConnectionPosition.company_name.is_not(None))
        )
    ).scalars().all()
    return [p for p in rows if company_key(p.company_name) == key]


async def _rule(
    session: AsyncSession, company_name: str, match: str, org_id: int | None
) -> int:
    positions = await _positions_for(session, company_name)
    contact_ids = {p.contact_id for p in positions if p.is_current}
    contacts = {}
    if contact_ids:
        contacts = {
            c.id: c
            for c in (
                await session.execute(select(Contact).where(Contact.id.in_(contact_ids)))
            ).scalars()
        }
    for p in positions:
        p.match, p.org_id, p.operator_decided = match, org_id, True
        if p.is_current and p.contact_id in contacts:
            contacts[p.contact_id].org_id = org_id
    await session.commit()
    return len(positions)


async def confirm_match(session: AsyncSession, company_name: str) -> int:
    """The operator confirms the proposed organization for a company name (a Possible match
    becomes Manual). Returns how many positions changed; 0 when there is nothing to confirm."""
    positions = await _positions_for(session, company_name)
    proposed = next((p.org_id for p in positions if p.org_id is not None), None)
    if proposed is None:
        return 0
    return await _rule(session, company_name, MATCH_MANUAL, proposed)


async def set_match(session: AsyncSession, company_name: str, org_id: int) -> int:
    """The operator picks a different organization for a company name."""
    if await session.get(Org, org_id) is None:
        raise ValueError("That company is not in the list.")
    return await _rule(session, company_name, MATCH_MANUAL, org_id)


async def reject_match(session: AsyncSession, company_name: str) -> int:
    """Not a match: the company name maps to no organization, and later imports remember it."""
    return await _rule(session, company_name, MATCH_NONE, None)


async def adopt_org(
    session: AsyncSession, company_name: str, org_id: int, *, commit: bool = True
) -> int:
    """Link a company name to an organization as a Sure match without the operator's say-so,
    for callers that create the organization from the name itself (the one-step wrapper in
    research/graph.py). Never overrides a ruling the operator made. Returns positions changed."""
    positions = [p for p in await _positions_for(session, company_name)
                 if not p.operator_decided and p.match != MATCH_SURE]
    for p in positions:
        p.match, p.org_id = MATCH_SURE, org_id
        if p.is_current:
            contact = await session.get(Contact, p.contact_id)
            if contact is not None:
                contact.org_id = org_id
    if commit:
        await session.commit()
    else:
        await session.flush()
    return len(positions)


async def rematch_undecided(session: AsyncSession) -> int:
    """Match again the company names nobody has ruled on, for organizations that arrived after
    the import (an unmatched company is kept for later). A Sure match links the contact; a
    Possible one is proposed for the operator to confirm. Returns how many positions changed."""
    index = await _org_index(session)
    rows = (
        await session.execute(
            select(ConnectionPosition, Contact)
            .join(Contact, Contact.id == ConnectionPosition.contact_id)
            .where(
                Contact.source == SOURCE,
                ConnectionPosition.operator_decided.is_(False),
                ConnectionPosition.company_name.is_not(None),
            )
        )
    ).all()
    changed = 0
    for pos, contact in rows:
        match, org_id = index.best(pos.company_name or "")
        # Only ever improves a position: an existing link is never erased or downgraded, and a
        # name that still matches nothing is left as it is.
        if match == MATCH_NONE or pos.match == MATCH_SURE:
            continue
        if (match, org_id) == (pos.match, pos.org_id):
            continue
        if pos.match == MATCH_POSSIBLE and match != MATCH_SURE:
            continue
        pos.match, pos.org_id = match, org_id
        if pos.is_current and match == MATCH_SURE and contact.org_id is None:
            contact.org_id = org_id
        changed += 1
    await session.commit()
    return changed


# --------------------------------------------------------------------------- warm paths
WORKS_HERE = "works_here"
USED_TO_WORK_HERE = "used_to_work_here"


@dataclass(frozen=True)
class ConnectionPath:
    kind: str  # works_here | used_to_work_here
    contact_id: int
    name: str
    title: str | None
    company_name: str | None
    match: str
    confirmed: bool  # False for a Possible match the operator has not confirmed
    connected_on: str | None
    imported_at: str | None  # when the file that last showed this position was imported


async def connection_paths(
    session: AsyncSession, org_id: int, *, limit: int = 50
) -> list[ConnectionPath]:
    """The only two warm paths (SPEC-MODULES 2.3): connections who work at the organization now,
    then connections who used to, as far as earlier imports show. Confirmed matches come before
    Possible ones. A person who works there now is never also listed as having worked there."""
    rows = (
        await session.execute(
            select(Contact, ConnectionPosition, ContactImport.imported_at)
            .join(ConnectionPosition, ConnectionPosition.contact_id == Contact.id)
            .join(
                ContactImport,
                ContactImport.id == ConnectionPosition.last_import_id,
                isouter=True,
            )
            .where(Contact.source == SOURCE, ConnectionPosition.org_id == org_id)
            .order_by(ConnectionPosition.id.desc())
        )
    ).all()
    now_ids = {c.id for c, p, _ in rows if p.is_current}
    out: list[ConnectionPath] = []
    listed_past: set[int] = set()
    for contact, pos, imported_at in rows:
        if pos.is_current:
            kind = WORKS_HERE
        elif contact.id in now_ids or contact.id in listed_past:
            continue
        else:
            kind = USED_TO_WORK_HERE
            listed_past.add(contact.id)
        out.append(
            ConnectionPath(
                kind=kind, contact_id=contact.id, name=contact.name, title=pos.title,
                company_name=pos.company_name, match=pos.match,
                confirmed=pos.match != MATCH_POSSIBLE, connected_on=pos.connected_on,
                imported_at=imported_at,
            )
        )
    out.sort(key=lambda p: (p.kind != WORKS_HERE, not p.confirmed, p.name.lower()))
    return out[:limit]


# ----------------------------------------------------------------------------- removal
async def imported_contact_count(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(Contact).where(Contact.source == SOURCE)
        )
    ).scalar_one()


@dataclass(frozen=True)
class RemoveResult:
    removed: int  # imported contacts deleted
    kept: int  # contacts with operator-written records, kept and no longer marked as imported


async def remove_all_imported(session: AsyncSession) -> RemoveResult:
    """Remove all contacts: every imported contact with its positions, connection rows and the
    import history. Nothing the operator wrote is deleted: a contact with an interaction, an
    outreach draft or notes stays (re-labelled so it is no longer an import) and is counted in
    `kept`, so the screen can say so. Public research contacts and every other row are
    untouched."""
    try:
        ids = list(
            (await session.execute(select(Contact.id).where(Contact.source == SOURCE))).scalars()
        )
        removed, kept = await _remove_contacts(session, ids, detach_kept=True)
        await session.execute(delete(ContactImport))
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    log.info("connections_removed", contacts=removed, kept=kept)
    return RemoveResult(removed, kept)
