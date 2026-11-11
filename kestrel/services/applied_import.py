"""Applied-history import (SPEC.md 5.6): a CSV or Markdown table of applications made before
Kestrel becomes one hidden opportunity and one pursuit per row, after a review the operator
confirms. Parsing and mapping are pure and deterministic so the review and the import both run
on the same text and agree; the database is touched only by `review` (reads: already-tracked
matches) and `commit` (writes the ticked rows).

Nothing here acts externally (hard rule 1): the rows describe applications the operator already
made by hand, and the import only records them."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import chain
from typing import NamedTuple

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.enums import PURSUIT_STATUS, TRACKS, valid_pursuit_status
from kestrel.db.models import Opportunity, Org, Pursuit, Source, StatusEvent
from kestrel.ingest.base import RawPosting
from kestrel.ingest.dedup import jaccard, title_tokens
from kestrel.ingest.normalize import normalize_and_store
from kestrel.ingest.orgs import normalized_name
from kestrel.services import kestrel_ids
from kestrel.services.csv_safe import safe_rows
from kestrel.urls import safe_web_url
from kestrel.web.fit import IMPORTED_REASON

# Header words accepted for each column, lowercased, punctuation and spaces removed.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "company": ("company", "organization", "organisation", "org", "employer", "client"),
    "title": ("role", "title", "position", "job", "jobtitle", "roletitle"),
    "date": ("date", "applied", "appliedon", "applieddate", "dateapplied", "submitted", "sent"),
    "status": ("status", "stage", "outcome"),
    "source": ("source", "board", "site", "via", "channel"),
    "url": ("url", "link", "posting", "jobposting", "href"),
    "location": ("location", "where", "city", "place"),
    "notes": ("notes", "note", "comments", "comment", "detail", "details"),
    "track": ("track",),
    "kid": ("kestrelid", "kestrelapplicationid"),
}
REQUIRED = ("company", "title", "date", "status")
# The column the review text carries so a copy with Kestrel IDs numbers rows as the person's file
# does (the review drops a row that is blank in every mapped column).
SOURCE_ROW_HEADER = "Kestrel source row"
SOURCE_ROW_KEY = "kestrelsourcerow"

# Tracker words mapped onto the employment statuses (SPEC.md 5.6). Kestrel's own status names on
# either track pass as written. Anything else is "unknown": the row waits for a choice.
STATUS_WORDS: dict[str, str] = {
    "no response": "applied",
    "noresponse": "applied",
    "acknowledged": "applied",
    "viewed": "applied",
    "submitted": "applied",
    "sent": "applied",
    "pending": "applied",
    "in conversation": "screening",
    "conversation": "screening",
    "screening call": "screening",
    "phone screen": "screening",
    "screen": "screening",
    "recruiter call": "screening",
    "interview": "interviewing",
    "interviews": "interviewing",
    "interviewing": "interviewing",
    "final": "final",
    "onsite": "final",
    "final round": "final",
    "offer": "offer",
    "offered": "offer",
    "accepted": "accepted",
    "hired": "accepted",
    "rejected": "rejected",
    "declined by them": "rejected",
    "no": "rejected",
    "closed": "rejected",
    "withdrawn": "withdrawn",
    "withdrew": "withdrawn",
    "declined": "withdrawn",
    "ghosted": "ghosted",
    "no answer": "ghosted",
}

# A row that is an activity, not an application (a TWC work-search log keeps both in one table).
ACTIVITY_COMPANY_PREFIXES = ("self", "networking", "workshop", "event", "job fair", "webinar")
ACTIVITY_MARKER = re.compile(r"activity\s*type\s*:", re.IGNORECASE)

DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%Y/%m/%d",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
)
TITLE_MATCH = 0.85
MAX_ROWS = 10_000
MAX_TEXT_BYTES = 25 * 1024 * 1024


@dataclass
class ImportRow:
    index: int  # 1-based data row number, as the pane prints it
    # 1-based data row number in the person's own file, when the review text carries it (the
    # review drops a row blank in every mapped column, so the two numbers can differ); 0 = same.
    src_row: int = 0
    company: str = ""
    title: str = ""
    date: str | None = None  # ISO date once parsed
    date_raw: str = ""
    status_raw: str = ""
    status: str | None = None  # mapped, or None when unknown
    source: str | None = None
    url: str | None = None
    location: str | None = None
    notes: str | None = None
    track: str = "employment"
    extras: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)  # blocking until corrected
    activity: bool = False  # not an application
    existing_pursuit_id: int | None = None  # already tracked
    existing_label: str = ""
    # Same org and a like title, but the dates leave it open whether this is the same application
    # (4 to 14 days apart, or a date that cannot be read). It is not an automatic match: the row
    # is not ticked by default and reads "possible duplicate"; ticking it adds a new application.
    possible_duplicate_id: int | None = None
    possible_duplicate_label: str = ""
    # The date is a month, not a day (the file's notes say "approx"): it matches by month and
    # never overwrites an exact date already in Kestrel.
    date_approx: bool = False
    # Same org and a like title as a tracked application, more than 14 days apart: a real second
    # application, imported as new (SPEC.md 5.6). The id is the earlier application.
    reapplication_of: int | None = None
    # The same row appeared earlier in the file (company, title, date and source): it collapses
    # into that row with a note and is never written twice.
    duplicate_of: int | None = None
    # What Kestrel already holds for the matched application, to show beside the file's row.
    existing_status: str | None = None
    existing_applied: str | None = None
    notes_new: bool = False  # the file's notes are not yet in the application's notes
    status_change: str | None = None  # the status the default rule would write, or None
    same_slot_other_source: bool = False  # a second application from another source, same day
    # The row's Kestrel ID (D-IMPORT-9), normalized, or "" when the cell is empty or not shaped
    # like one. `kid_state`: "" none, "matched" it named a tracked application, "unknown" no
    # application has it (the row is new, with a warning), "repeat" another row of the file
    # already uses it (the row is held until the file is fixed).
    kid: str = ""
    kid_raw: str = ""
    kid_state: str = ""
    warning: str = ""

    @property
    def bucket(self) -> str:
        """Where the review pane shows this row (SPEC.md 5.6): exceptions, not every row."""
        if self.duplicate_of is not None:
            return "repeat"
        if self.activity:
            return "activity"
        if self.problems or self.kid_state == "repeat" or self.possible_duplicate_id is not None:
            return "ask"
        if self.existing_pursuit_id is not None:
            return "differs" if (self.status_change or self.notes_new) else "in_sync"
        if self.reapplication_of is not None:
            return "reapply"
        return "new"

    @property
    def importable(self) -> bool:
        """Can be ticked: no blocking problem. A row without a mapped status is importable once
        the operator picks one in the pane; the write boundary re-checks that a status exists."""
        return not self.problems and self.duplicate_of is None and self.kid_state != "repeat"

    @property
    def default_include(self) -> bool:
        return (
            self.importable
            and self.status is not None
            and not self.activity
            and self.existing_pursuit_id is None
            and self.possible_duplicate_id is None
        )



@dataclass
class ImportReview:
    rows: list[ImportRow]
    columns: dict[str, str]  # canonical -> the header it was read from
    fmt: str  # csv | markdown
    errors: list[str] = field(default_factory=list)  # whole-file problems; no rows when set

    @property
    def missing(self) -> list[str]:
        return [c for c in REQUIRED if c not in self.columns]


@dataclass
class RowChoice:
    include: bool = False
    status: str | None = None  # overrides the mapped status
    allow_reposts: bool = False
    # Kept for callers that set it; an already-tracked row that is included always updates the
    # existing pursuit (SPEC.md 5.6), never makes a second one.
    update_existing: bool = False


@dataclass
class ImportResult:
    imported: int = 0
    updated: int = 0
    # Ticked rows whose status Kestrel held back because the application is already further along
    # or closed (SPEC.md 5.6). Their other fields were still filled in.
    kept_newer: int = 0
    skipped: int = 0
    pursuit_ids: list[int] = field(default_factory=list)
    row_pursuits: dict[int, int] = field(default_factory=dict)  # file row -> its application
    problems: list[str] = field(default_factory=list)
    # What Undo needs (Settings > Your data): the pursuits this import created, and the hidden
    # roles it created for them. A role that already existed is never listed, so Undo cannot
    # delete one.
    created_pursuit_ids: list[int] = field(default_factory=list)
    created_opp_ids: list[int] = field(default_factory=list)


# ------------------------------------------------------------------------------------ parsing
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.lower())


def map_columns(headers: list[str]) -> dict[str, str]:
    """canonical column -> the header it was read from; the first matching header wins."""
    out: dict[str, str] = {}
    for header in headers:
        k = _key(header)
        for canonical, aliases in COLUMN_ALIASES.items():
            if canonical not in out and k in aliases:
                out[canonical] = header
                break
    return out


def unguard(value: str) -> str:
    """Undo the one quote the formula-safe copy puts in front of a cell that starts with = + - @
    (D-SEC-10), so a copy the person keeps as their tracker reads back as what they wrote."""
    if value.startswith("'") and value[1:].startswith(_FORMULA_TRIGGERS):
        return value[1:]
    return value


def _md_cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.replace("\\|", "|").strip() for c in re.split(r"(?<!\\)\|", body)]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c.strip()) for c in cells if c.strip())


def parse_table(text: str) -> tuple[list[str], list[list[str]], str]:
    """(headers, rows, "markdown" | "csv"). A Markdown table is any run of `|` lines (front
    matter and prose around it are ignored); otherwise the text is read as CSV with the
    delimiter sniffed among comma, semicolon and tab. Raises ValueError with a plain reason."""
    if len(text) > MAX_TEXT_BYTES or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("That tracker is too large to read here. Split it into smaller files.")
    headers: list[str] | None = None
    rows: list[list[str]] = []
    pipe_count = 0
    for line in io.StringIO(text):
        if not line.strip().startswith("|"):
            continue
        pipe_count += 1
        cells = _md_cells(line)
        if headers is None:
            headers = cells
        elif not _is_separator(cells):
            if len(rows) >= MAX_ROWS:
                raise ValueError(f"That tracker has more than {MAX_ROWS:,} rows. Split the file.")
            rows.append(cells)
    if pipe_count >= 2:
        return headers, rows, "markdown"
    # Blank physical lines are kept past the header: inside a quoted cell they are the person's
    # own text, and between records csv.reader yields an empty row that is dropped below.
    lines = iter(io.StringIO(text))
    first = next((line for line in lines if line.strip()), "")
    if not first:
        raise ValueError("The text is empty.")
    try:
        dialect = csv.Sniffer().sniff(first, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(chain((first,), lines), dialect)
    table = []
    try:
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            if len(table) > MAX_ROWS:
                raise ValueError(f"That tracker has more than {MAX_ROWS:,} rows. Split the file.")
            table.append([cell.strip() for cell in row])
    except csv.Error as exc:
        raise ValueError(
            "That tracker could not be read as CSV. Check its fields and quotes."
        ) from exc
    if len(table) < 2:
        raise ValueError("Need a header row and at least one data row.")
    return table[0], table[1:], "csv"


def parse_date(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)  # an ISO timestamp, keep the day
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None
    return None


def map_status(word: str, track: str = "employment") -> str | None:
    s = re.sub(r"\s+", " ", (word or "").strip().lower())
    if not s:
        return None
    if valid_pursuit_status(track, s):
        return s
    if valid_pursuit_status(track, s.replace(" ", "_")):
        return s.replace(" ", "_")
    mapped = STATUS_WORDS.get(s)
    if mapped and valid_pursuit_status(track, mapped):
        return mapped
    return None


def is_activity(company: str, title: str, notes: str | None) -> bool:
    low = (company or "").strip().lower()
    if any(low.startswith(p) for p in ACTIVITY_COMPANY_PREFIXES):
        return True
    return bool(ACTIVITY_MARKER.search(f"{title or ''} {notes or ''}"))


def parse_rows(text: str) -> ImportReview:
    """Pure: the review without the database (no already-tracked matches)."""
    try:
        headers, raw_rows, fmt = parse_table(text)
    except ValueError as exc:
        return ImportReview(rows=[], columns={}, fmt="csv", errors=[str(exc)])
    columns = map_columns(headers)
    review = ImportReview(rows=[], columns=columns, fmt=fmt)
    if review.missing:
        review.errors.append(
            "Missing required column(s): "
            + ", ".join(review.missing)
            + ". Headers read: "
            + ", ".join(h for h in headers if h)
        )
        return review
    if not raw_rows:
        review.errors.append("The table has a header row but no data rows.")
        return review
    idx: dict[str, int] = {}
    for i, h in enumerate(headers):
        idx.setdefault(h, i)  # a duplicate header reads from its first column, as map_columns does

    def cell(row: list[str], canonical: str) -> str:
        header = columns.get(canonical)
        if header is None:
            return ""
        i = idx[header]
        return unguard(row[i].strip()) if i < len(row) else ""

    known = set(columns.values())
    src_col = next((i for h, i in idx.items() if _key(h) == SOURCE_ROW_KEY), None)
    if src_col is not None:
        known.add(headers[src_col])
    for n, raw in enumerate(raw_rows, start=1):
        r = ImportRow(index=n)
        if src_col is not None and src_col < len(raw) and raw[src_col].strip().isdigit():
            r.src_row = int(raw[src_col].strip())
        r.company = cell(raw, "company")
        r.title = cell(raw, "title")
        r.date_raw = cell(raw, "date")
        r.status_raw = cell(raw, "status")
        r.source = cell(raw, "source") or None
        r.url = safe_web_url(cell(raw, "url"))
        r.location = cell(raw, "location") or None
        r.notes = cell(raw, "notes") or None
        track = cell(raw, "track").lower() or "employment"
        r.track = track if track in TRACKS else "employment"
        r.extras = {
            h: unguard(raw[i].strip())
            for h, i in idx.items()
            if h and h not in known and i < len(raw) and raw[i].strip()
        }
        r.activity = is_activity(r.company, r.title, r.notes)
        if not r.company:
            r.problems.append("company is empty")
        if not r.title:
            r.problems.append("title is empty")
        r.kid_raw = cell(raw, "kid")
        r.kid = kestrel_ids.normalize(r.kid_raw)
        r.date = parse_date(r.date_raw)
        r.date_approx = bool(_APPROX.search(f"{r.notes or ''} {r.date_raw}"))
        if r.date is None:
            r.problems.append(
                f"date {r.date_raw!r} not understood (use YYYY-MM-DD)"
                if r.date_raw
                else "date is empty"
            )
        r.status = map_status(r.status_raw, r.track)
        review.rows.append(r)
    return review


# ----------------------------------------------------------------------------- the database
class _Tracked(NamedTuple):
    """One application already in Kestrel, as the matcher sees it."""

    pid: int
    org_norm: str
    title: str | None
    url: str | None
    applied: str | None
    status: str = ""
    notes: str = ""
    source: str = ""  # the board an earlier import recorded, lowercased; "" when unknown
    kid: str = ""  # the application's Kestrel ID, normalized; "" before any ID was handed out


class Match(NamedTuple):
    pid: int | None
    why: str
    ask: bool  # True: the person decides (4 to 14 days apart, or a date that cannot be read)
    reapply_of: int | None = None  # a like application more than 14 days away: a re-application


_IMPORTED_FROM = re.compile(r"^Imported from (.+?) on \d{4}-\d{2}-\d{2}", re.IGNORECASE)
_APPROX = re.compile(r"\bapprox", re.IGNORECASE)


def imported_board(description: str | None) -> str | None:
    """The board an earlier import recorded in a role's one-line description, if any."""
    m = _IMPORTED_FROM.match(description or "")
    return m.group(1).strip() if m else None


async def _existing_pursuits(session: AsyncSession) -> list[_Tracked]:
    rows = (
        await session.execute(
            select(Pursuit.id, Org.normalized_name, Opportunity.title, Opportunity.url,
                   Pursuit.applied_at, Pursuit.status, Pursuit.notes_md, Opportunity.description_md,
                   Opportunity.first_seen_at)
            .join(Opportunity, Opportunity.id == Pursuit.opportunity_id)
            .outerjoin(Org, Org.id == Opportunity.org_id)
            .order_by(Pursuit.id)
        )
    ).all()
    salt = await kestrel_ids.get_salt(session)  # never creates: a review writes nothing
    out = []
    for pid, norm, title, url, applied, status, notes, desc, seen in rows:
        m = _IMPORTED_FROM.match(desc or "")
        kid = kestrel_ids.normalize(kestrel_ids.make_id(salt, pid, seen)) if salt else ""
        out.append(_Tracked(pid, norm or "", title, url, applied, status or "", notes or "",
                            m.group(1).strip().lower() if m else "", kid))
    return out


def _find_tracked(row: ImportRow, existing: list[_Tracked]) -> Match:
    """Which tracked application is this row? `ask` False: it is the same application (same URL,
    or same org and title within 3 days, or the same month for an approximate date, or no dates to
    tell them apart) and ticking updates it. `ask` True: same org and title 4 to 14 days apart, or
    a date that cannot be read; the person decides, so the caller marks the row "possible
    duplicate". More than 14 days apart is a re-application and no match (#131, #147); the
    earlier application is reported in `reapply_of`."""
    url = (row.url or "").strip().rstrip("/")
    norm = normalized_name(row.company) or row.company.lower()
    tset = title_tokens(row.title)
    row_date = _parse_date(row.date) if row.date else None
    ask: tuple[int, str] | None = None
    reapply: int | None = None
    for t in existing:
        pid = t.pid
        if url and t.url and t.url.rstrip("/") == url:
            return Match(pid, f"same URL as pursuit #{pid}", False)
        if t.org_norm != norm or jaccard(tset, title_tokens(t.title)) <= TITLE_MATCH:
            continue
        if not row.date or not t.applied:  # nothing to compare: as before, the same application
            return Match(pid, f"same org and title as pursuit #{pid}", False)
        app_date = _parse_date(t.applied)
        if row_date is None or app_date is None:
            ask = ask or (pid, f"same org and title as pursuit #{pid}; a date could not be read")
            continue
        if row.date_approx:  # a month, not a day: compare months
            gap = abs((row_date.year - app_date.year) * 12 + row_date.month - app_date.month)
            if gap == 0:
                return Match(pid, f"same org and title, same month as pursuit #{pid}", False)
            if gap == 1:
                ask = ask or (pid, f"same org and title, an approximate date a month from "
                                   f"pursuit #{pid}")
            else:
                reapply = reapply or pid
            continue
        delta = abs((row_date - app_date).days)
        if delta <= 3:
            return Match(pid, f"same org and title, same date as pursuit #{pid}", False)
        if delta <= 14:
            ask = ask or (pid, f"same org and title, {delta} days apart from pursuit #{pid}")
        else:
            reapply = reapply or pid  # more than 14 days: a re-application, not a match
    if ask is not None:
        return Match(ask[0], ask[1], True)
    return Match(None, "", False, reapply)


def _parse_date(s: str) -> date | None:
    """Parse a plain ISO date or an ISO timestamp to a date; returns None on failure."""
    s = (s or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            return None
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


# ------------------------------------------------------------------------------ status rule
def _rank(track: str, status: str) -> int:
    order = PURSUIT_STATUS.get(track, ())
    return order.index(status) if status in order else -1


CLOSED = {"employment": {"rejected", "withdrawn", "ghosted"}, "consulting": {"lost", "no_response"}}


def decide_status(
    track: str, current: str, incoming: str, *, file_wins: bool = False
) -> str | None:
    """The status a re-import may write, or None to leave the application as it is. Furthest along
    wins; a closed application stays closed; an incoming closed status (the file learned of a
    rejection) is applied to an open one. `file_wins` is the person's per-import override. The
    default protects progress Kestrel already holds (SPEC.md 5.6)."""
    if not incoming or incoming == current:
        return None
    if file_wins:
        return incoming
    closed = CLOSED.get(track, set())
    if current in closed:
        return None
    if incoming in closed:
        return incoming
    return incoming if _rank(track, incoming) > _rank(track, current) else None


def _notes_new(row: ImportRow, notes: str) -> bool:
    return bool(row.notes) and notes.find(row.notes) < 0


def _row_key(r: ImportRow) -> tuple:
    return (
        normalized_name(r.company) or r.company.lower(),
        " ".join(r.title.lower().split()),
        r.date,
        (r.source or "").strip().lower(),
    )


async def review(session: AsyncSession, text: str) -> ImportReview:
    """parse_rows plus the already-tracked check against every pursuit. Pairing is one to one:
    each tracked application is claimed by at most one file row, so a second row for the same role
    stays its own application (or collapses into an identical row), never lost."""
    rev = await asyncio.to_thread(parse_rows, text)
    if not rev.rows:
        return rev
    existing = await _existing_pursuits(session)
    by_pid = {t.pid: t for t in existing}

    def fill(row: ImportRow, t: _Tracked, why: str) -> None:
        row.existing_pursuit_id, row.existing_label = t.pid, why
        row.existing_status, row.existing_applied = t.status, t.applied
        row.notes_new = _notes_new(row, t.notes)
        if row.status:
            row.status_change = decide_status(row.track, t.status, row.status)

    def match_rows() -> None:
        seen: dict[tuple, int] = {}
        kid_rows: dict[str, int] = {}
        by_kid: dict[str, list[_Tracked]] = {}
        for t in existing:
            if t.kid:
                by_kid.setdefault(t.kid, []).append(t)
        claimed: set[int] = set()
        known_sources = {t.source for t in existing if t.source}
        todo = []
        for row in rev.rows:
            # A Kestrel ID names the application, so a row whose only problem is its date still
            # gets the ID lookup first: Kestrel's own export has blank dates for some rows.
            id_only = bool(row.kid) and all(p.startswith("date ") for p in row.problems)
            if row.problems and not id_only:
                continue
            key = _row_key(row)
            if key in seen and not (id_only and row.date is None):
                # an identical row (same source too): one application
                row.duplicate_of = seen[key]
                continue
            if row.date is not None or not id_only:
                seen[key] = row.index
            if row.kid_raw.strip() and not row.kid:
                row.kid_state = "unknown"
                row.warning = f"{row.kid_raw.strip()!r} is not a Kestrel ID, so this row is new"
            elif row.kid:
                if row.kid in kid_rows:
                    row.kid_state = "repeat"
                    row.warning = (f"The same Kestrel ID is already on row {kid_rows[row.kid]}. "
                                   "Fix the file, then review it again")
                    continue
                kid_rows[row.kid] = row.index
                holders = by_kid.get(row.kid, [])
                if len(holders) == 1 and holders[0].pid not in claimed:
                    # The ID is trusted over the words: an edited title or company still matches.
                    row.kid_state = "matched"
                    row.problems = [p for p in row.problems if not p.startswith("date ")]
                    claimed.add(holders[0].pid)
                    fill(row, holders[0], "same Kestrel ID")
                    continue
                row.kid_state = "unknown"
                row.warning = "No application has this Kestrel ID, so this row is new"
            if row.problems:  # no application to attach the ID to: the missing date still blocks
                continue
            todo.append(row)
        # A row from the board a tracked application was imported from pairs first (tie-breaker).
        todo.sort(key=lambda r: 0 if (r.source or "").lower() in known_sources else 1)
        slots: dict[tuple, int] = {}
        for row in todo:
            if row.kid_state == "unknown":  # an ID nobody holds: new, never guessed by words
                continue
            m = _find_tracked(row, [t for t in existing if t.pid not in claimed])
            if m.ask and m.pid is not None:
                row.possible_duplicate_id, row.possible_duplicate_label = m.pid, m.why
                claimed.add(m.pid)
            elif m.pid is not None:
                claimed.add(m.pid)
                fill(row, by_pid[m.pid], m.why)
            else:
                full = _find_tracked(row, existing) if claimed else m
                row.reapplication_of = m.reapply_of or full.reapply_of
                slot = (_row_key(row)[0], _row_key(row)[1], row.date)
                # Same role, same day, another source: a second application. Either an earlier
                # file row or a tracked application already holds the slot.
                if slot in slots or (full.pid is not None and not full.ask):
                    row.same_slot_other_source = True
                slots[slot] = row.index

    await asyncio.to_thread(match_rows)
    return rev


async def _import_source(session: AsyncSession, track: str) -> Source:
    name = f"Applied history import ({track})"
    src = (
        await session.execute(select(Source).where(Source.name == name, Source.kind == "import"))
    ).scalar_one_or_none()
    if src is None:
        src = Source(track=track, kind="import", name=name, config={}, enabled=False)
        session.add(src)
        await session.flush()
    return src


def _description(r: ImportRow) -> str:
    bits = [f"Imported from {r.source} on {r.date}." if r.source else f"Imported on {r.date}."]
    if r.notes:
        bits.append(r.notes)
    for k, v in r.extras.items():
        bits.append(f"{k}: {v}")
    return " ".join(bits)


def _external_id(r: ImportRow) -> str:
    key = f"{r.company}|{r.title}|{r.date}"
    if r.same_slot_other_source:  # a second application on the same day: its own role
        key += f"|{(r.source or '').strip().lower()}|2"
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    return f"import:{h}"


async def _free_external_id(session: AsyncSession, source: Source, base: str) -> str:
    """`base`, or `base` with a counter when an application already holds it (D-IMPORT-12).
    A row the review showed as new or a re-application is a new application: it must never land
    on an application it was not shown as updating, because Undo only reverts the pursuits the
    review named."""
    candidate, n = base, 1
    while (
        await session.execute(
            select(Pursuit.id)
            .join(Opportunity, Opportunity.id == Pursuit.opportunity_id)
            .where(Opportunity.source_id == source.id, Opportunity.external_id == candidate)
            .limit(1)
        )
    ).first() is not None:
        n += 1
        candidate = f"{base}:{n}"
    return candidate


def _record_status(
    session: AsyncSession, p: Pursuit, status: str, when: str, *, file_wins: bool = False
) -> bool:
    """Apply the file's status under the furthest-along rule. True when the status was held back."""
    new = decide_status(p.track, p.status, status, file_wins=file_wins)
    if new is not None:
        session.add(
            StatusEvent(pursuit_id=p.id, from_status=p.status, to_status=new, occurred_at=when)
        )
        p.status = new
        p.last_activity_at = utcnow_iso()
    return new is None and status != p.status


async def commit(
    session: AsyncSession,
    rev: ImportReview,
    choices: dict[int, RowChoice],
    *,
    commit_transaction: bool = True,
    file_wins: bool = False,
) -> ImportResult:
    """Write the ticked rows. Re-validates every choice (hard rule: the write boundary trusts no
    form): a status not valid for the track, an unparsed date, or a row with problems is skipped
    and named. Commits once at the end."""
    result = ImportResult()
    now = utcnow_iso()
    for r in rev.rows:
        choice = choices.get(r.index)
        if choice is None or not choice.include:
            result.skipped += 1
            continue
        if r.duplicate_of is not None or r.kid_state == "repeat":  # held, never written
            result.skipped += 1
            if r.kid_state == "repeat":
                result.problems.append(f"row {r.index} ({r.company}: {r.title}): {r.warning}")
            continue
        status = choice.status or r.status
        if r.problems or not status or not valid_pursuit_status(r.track, status):
            result.skipped += 1
            result.problems.append(
                f"row {r.index} ({r.company}: {r.title}): "
                + ("; ".join(r.problems) if r.problems else f"status {status!r} is not valid")
            )
            continue
        when = f"{r.date}T00:00:00+00:00" if r.date else now
        if r.existing_pursuit_id is not None:
            p = await session.get(Pursuit, r.existing_pursuit_id)
            if p is None:
                result.skipped += 1
                result.problems.append(f"row {r.index}: pursuit #{r.existing_pursuit_id} is gone")
                continue
            result.row_pursuits[r.index] = p.id
            if _record_status(session, p, status, when, file_wins=file_wins):
                result.kept_newer += 1
            if r.date:  # an undated row (matched by its ID) never invents a date
                p.applied_at = p.applied_at or when
            p.allow_reposts = bool(choice.allow_reposts) or p.allow_reposts
            if r.notes and (p.notes_md or "").find(r.notes) < 0:
                p.notes_md = ((p.notes_md or "") + "\n\n" + r.notes).strip()
            result.updated += 1
            result.pursuit_ids.append(p.id)
            continue
        source = await _import_source(session, r.track)
        raw = RawPosting(
            external_id=await _free_external_id(session, source, _external_id(r)),
            url=r.url,
            title=r.title,
            org_name=r.company,
            location_raw=r.location,
            description_md=_description(r),
            date_posted=r.date,
        )
        res = await normalize_and_store(session, raw, source=source)
        opp = res.opportunity
        if res.action == "new":
            result.created_opp_ids.append(opp.id)
        # Hidden on arrival: this was applied for before Kestrel saw it, so it never enters
        # triage or the list (SPEC.md 5.6). No embed task: a hidden row is not scored.
        if not opp.archived_at:
            opp.archived_at = now
            opp.archive_reason = "applied"
        if opp.stage_reason is None:
            opp.stage_reason = IMPORTED_REASON
        p = Pursuit(
            opportunity_id=opp.id,
            track=r.track,
            status=status,
            applied_at=when,
            last_activity_at=now,
            notes_md=r.notes,
            allow_reposts=bool(choice.allow_reposts),
        )
        session.add(p)
        await session.flush()
        session.add(
            StatusEvent(pursuit_id=p.id, from_status=None, to_status=status, occurred_at=when)
        )
        result.imported += 1
        result.pursuit_ids.append(p.id)
        result.row_pursuits[r.index] = p.id
        result.created_pursuit_ids.append(p.id)
    if commit_transaction:
        await session.commit()
    else:
        await session.flush()
    return result


def copy_with_ids(text: str, row_ids: dict[int, str]) -> str:
    """The person's own file as CSV with a `Kestrel ID` column filled in for the rows that are an
    application in Kestrel (D-IMPORT-9). The file they chose is never edited: this is a new copy
    they may keep as their tracker. Every other cell is carried over as read (whitespace around a
    cell trimmed); a row Kestrel did not match keeps whatever the cell held. `row_ids` maps the
    1-based data row number to the ID."""
    headers, rows, _fmt = parse_table(text)
    col = next((i for i, h in enumerate(headers) if map_columns([h]).get("kid")), None)
    out_headers = list(headers)
    if col is None:
        # after the widest row, so a row with more cells than the header keeps its overflow cell
        col = max([len(out_headers), *(len(r) for r in rows)])
        out_headers += [""] * (col - len(out_headers))
        out_headers.append("Kestrel ID")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerows(safe_rows([out_headers]))  # a copy is opened in a spreadsheet (D-SEC-10)
    for n, raw in enumerate(rows, start=1):
        cells = list(raw) + [""] * (len(out_headers) - len(raw))
        if n in row_ids:
            cells[col] = row_ids[n]
        w.writerows(safe_rows([cells]))
    return out.getvalue()


def status_options(track: str) -> tuple[str, ...]:
    return PURSUIT_STATUS.get(track, ())


# ------------------------------------------------------------------ add a role found elsewhere
# D-UI-46: the Applications board's "Add a role I found elsewhere". A URL and/or pasted text goes
# through the same manual-ingest path as the browser extension (so it is normalized, deduped and
# scored through the normal queue), and the pursuit is created at once so the card appears.
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_DROP_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_BR_RE = re.compile(r"<(br|/p|/div|/li|/h[1-6]|/tr)\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_MIN_PAGE_CHARS = 200


@dataclass
class AddRoleResult:
    ok: bool
    message: str
    opportunity_id: int | None = None
    pursuit_id: int | None = None
    created: bool = False  # False when the role was already on the list


def page_text(html: str) -> tuple[str | None, str]:
    """(title, readable text) from a fetched page. Plain tag stripping, no model involved."""
    import html as html_lib

    m = _TITLE_RE.search(html)
    title = html_lib.unescape(_TAG_RE.sub("", m.group(1))).strip() if m else None
    body = _BR_RE.sub("\n", _DROP_RE.sub(" ", html))
    body = html_lib.unescape(_TAG_RE.sub(" ", body))
    lines = [re.sub(r"[ \t\r\f\v\u00a0]+", " ", ln).strip() for ln in body.splitlines()]
    return (title or None), "\n".join(ln for ln in lines if ln)


_MAX_HOPS = 5
_LOGIN_PATH = re.compile(r"(log-?in|sign-?in|authwall|/auth/|sso|captcha|checkpoint)", re.I)
_LOGIN_TEXT = re.compile(
    r"\b(sign[ -]?in|log[ -]?in|authwall|captcha|verify you are (a )?human)\b", re.I
)
# A real posting is long; a sign-in wall is short. Body text mentioning "sign in" counts only on
# a page this short, since nearly every careers page has a "Sign in" link in its header.
_LOGIN_BODY_MAX = 1500


class FetchRefused(Exception):
    """The link cannot be read politely as a posting: say why, and ask for the pasted text."""


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def looks_like_login(final_url: str, title: str | None, text: str) -> bool:
    """A sign-in, SSO or captcha page rather than a posting: the URL path says so, the title
    says so, or a short page's body does."""
    from urllib.parse import urlsplit

    parts = urlsplit(final_url)
    if _LOGIN_PATH.search(parts.path or "") or _LOGIN_PATH.search(parts.query or ""):
        return True
    if title and _LOGIN_TEXT.search(title):
        return True
    return len(text) < _LOGIN_BODY_MAX and bool(_LOGIN_TEXT.search(text))


async def fetch_posting(http, url: str) -> tuple[str, str]:
    """GET a pasted link politely: redirects are followed by hand and only within the same site
    (www. aside), robots.txt is checked on every hop (so the final host's rules apply, not just
    the first's), and a hop toward a sign-in page is refused. Returns (final_url, html)."""
    from urllib.parse import urljoin, urlsplit

    from kestrel.ingest.http import UnsafeURLError, _raise_for_status

    start = _host(url)
    current = url
    for _ in range(_MAX_HOPS + 1):
        try:
            # public_only: a pasted link never reaches this computer or a private network, on
            # the first hop or any redirect, and the page is capped at 2 MB.
            r = await http.request(
                "GET", current, check_robots=True, follow_redirects=False, public_only=True
            )
        except UnsafeURLError as exc:
            raise FetchRefused(str(exc)) from exc
        if r.is_redirect:
            location = r.headers.get("location") or ""
            nxt = urljoin(current, location)
            parts = urlsplit(nxt)
            if _LOGIN_PATH.search(f"{parts.path}?{parts.query}"):
                raise FetchRefused("That link sends you to a sign-in page")
            if _host(nxt) != start:
                raise FetchRefused(f"That link redirects to another site ({_host(nxt)})")
            current = nxt
            continue
        _raise_for_status(r)
        return current, r.text
    raise FetchRefused("That link redirects too many times")


async def add_found_role(
    session: AsyncSession,
    *,
    track: str,
    url: str | None = None,
    text: str | None = None,
    title: str | None = None,
    org_name: str | None = None,
    http=None,
) -> AddRoleResult:
    """Create the opportunity (manual-ingest path) and its pursuit in the first column. `http` is
    the polite HttpClient (descriptive User-Agent, robots.txt, rate limit); the URL is fetched at
    most once. A failed fetch is reported and any pasted text is kept; with nothing to keep, no
    opportunity is created. Pasted text wins over fetched text as the posting body."""
    from kestrel.errors import RobotsDisallowed
    from kestrel.ingest.manual import manual_ingest

    url = (url or "").strip() or None
    text = (text or "").strip() or None
    title = (title or "").strip() or None
    org_name = (org_name or "").strip() or None
    if track not in TRACKS:
        track = "employment"
    if not url and not text:
        return AddRoleResult(False, "Paste the posting text or a link to it first.")
    if url and not re.match(r"^https?://", url, re.IGNORECASE):
        return AddRoleResult(False, "The link must start with http:// or https://.")

    notes: list[str] = []
    fetched = None
    if url and http is not None:
        try:
            final_url, html = await fetch_posting(http, url)
            page_title, fetched = page_text(html)
            if looks_like_login(final_url, page_title, fetched):
                # Never save a sign-in wall ("Sign in | LinkedIn") as the posting.
                raise FetchRefused("That link opens a sign-in page, not the posting")
            title = title or page_title
            if len(fetched) < _MIN_PAGE_CHARS:
                notes.append(
                    "The page returned very little text (it may need a login or JavaScript)."
                )
                if not text:
                    fetched = None
        except FetchRefused as exc:
            notes.append(f"{exc}, so Kestrel did not read it. Copy the posting text from your "
                         "browser and paste it instead.")
            fetched = None
        except RobotsDisallowed as exc:
            notes.append(f"{exc} Copy the posting text from your browser and paste it instead.")
            fetched = None
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, PermissionError, RuntimeError,
                OSError) as exc:
            # InvalidURL is not an HTTPError; a malformed link must be a notice, never a 500.
            from kestrel.errors import task_failure_text

            notes.append(f"Could not read that page ({task_failure_text(exc)}).")
            fetched = None
    if not text and fetched is None:
        notes.append("Paste the posting text and try again.")
        return AddRoleResult(False, " ".join(notes))
    if notes:
        notes.append("Using the text you pasted." if text else "")

    opp = await manual_ingest(
        session, url=url, text=text or fetched, title=title, org_name=org_name, track=track
    )
    if opp is None:
        return AddRoleResult(False, "Nothing to add.")
    existing = (
        await session.execute(select(Pursuit).where(Pursuit.opportunity_id == opp.id))
    ).scalar_one_or_none()
    if existing is not None:
        return AddRoleResult(
            True, f"Already on your list ({existing.status.replace('_', ' ')}).",
            opp.id, existing.id, created=False,
        )
    status = PURSUIT_STATUS[opp.track][0]
    now = utcnow_iso()
    p = Pursuit(opportunity_id=opp.id, track=opp.track, status=status, last_activity_at=now)
    session.add(p)
    await session.flush()
    session.add(StatusEvent(pursuit_id=p.id, from_status=None, to_status=status, occurred_at=now))
    await session.commit()
    msg = "Added to your list. Kestrel will score it in the background."
    return AddRoleResult(True, " ".join(x for x in (*notes, msg) if x), opp.id, p.id, created=True)
