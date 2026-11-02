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
from datetime import datetime
from itertools import chain

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
from kestrel.urls import safe_web_url
from kestrel.web.fit import IMPORTED_REASON

# Header words accepted for each column, lowercased, punctuation and spaces removed.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "company": ("company", "organization", "organisation", "org", "employer", "client"),
    "title": ("role", "title", "position", "job", "jobtitle", "roletitle"),
    "date": ("date", "applied", "appliedon", "applieddate", "dateapplied", "submitted"),
    "status": ("status", "stage", "outcome"),
    "source": ("source", "board", "site", "via", "channel"),
    "url": ("url", "link", "posting", "jobposting", "href"),
    "location": ("location", "where", "city", "place"),
    "notes": ("notes", "note", "comments", "comment", "detail", "details"),
    "track": ("track",),
}
REQUIRED = ("company", "title", "date", "status")

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

    @property
    def importable(self) -> bool:
        """Can be ticked: no blocking problem. A row without a mapped status is importable once
        the operator picks one in the pane; the write boundary re-checks that a status exists."""
        return not self.problems

    @property
    def default_include(self) -> bool:
        return (
            self.importable
            and self.status is not None
            and not self.activity
            and self.existing_pursuit_id is None
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
    skipped: int = 0
    pursuit_ids: list[int] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    # What Undo needs (Settings > Your data): the pursuits this import created, and the hidden
    # roles it created for them. A role that already existed is never listed, so Undo cannot
    # delete one.
    created_pursuit_ids: list[int] = field(default_factory=list)
    created_opp_ids: list[int] = field(default_factory=list)


# ------------------------------------------------------------------------------------ parsing
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
    lines = (line for line in io.StringIO(text) if line.strip())
    first = next(lines, "")
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
        return row[i].strip() if i < len(row) else ""

    known = set(columns.values())
    for n, raw in enumerate(raw_rows, start=1):
        r = ImportRow(index=n)
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
            h: raw[i].strip()
            for h, i in idx.items()
            if h and h not in known and i < len(raw) and raw[i].strip()
        }
        r.activity = is_activity(r.company, r.title, r.notes)
        if not r.company:
            r.problems.append("company is empty")
        if not r.title:
            r.problems.append("title is empty")
        r.date = parse_date(r.date_raw)
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
async def _existing_pursuits(
    session: AsyncSession,
) -> list[tuple[int, str, str | None, str | None]]:
    rows = (
        await session.execute(
            select(Pursuit.id, Org.normalized_name, Opportunity.title, Opportunity.url)
            .join(Opportunity, Opportunity.id == Pursuit.opportunity_id)
            .outerjoin(Org, Org.id == Opportunity.org_id)
        )
    ).all()
    return [(pid, norm or "", title, url) for pid, norm, title, url in rows]


def _find_tracked(row: ImportRow, existing) -> tuple[int | None, str]:
    url = (row.url or "").strip().rstrip("/")
    norm = normalized_name(row.company) or row.company.lower()
    tset = title_tokens(row.title)
    for pid, org_norm, title, purl in existing:
        if url and purl and purl.rstrip("/") == url:
            return pid, f"same URL as pursuit #{pid}"
        if org_norm == norm and jaccard(tset, title_tokens(title)) > TITLE_MATCH:
            return pid, f"same org and title as pursuit #{pid}"
    return None, ""


async def review(session: AsyncSession, text: str) -> ImportReview:
    """parse_rows plus the already-tracked check against every pursuit."""
    rev = await asyncio.to_thread(parse_rows, text)
    if not rev.rows:
        return rev
    existing = await _existing_pursuits(session)

    def match_rows() -> None:
        for row in rev.rows:
            if row.problems:
                continue
            row.existing_pursuit_id, row.existing_label = _find_tracked(row, existing)

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
    h = hashlib.sha1(f"{r.company}|{r.title}|{r.date}".encode()).hexdigest()[:16]
    return f"import:{h}"


def _record_status(session: AsyncSession, p: Pursuit, status: str, when: str) -> None:
    if p.status != status:
        session.add(
            StatusEvent(pursuit_id=p.id, from_status=p.status, to_status=status, occurred_at=when)
        )
        p.status = status
    p.last_activity_at = utcnow_iso()


async def commit(
    session: AsyncSession,
    rev: ImportReview,
    choices: dict[int, RowChoice],
    *,
    commit_transaction: bool = True,
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
        status = choice.status or r.status
        if r.problems or not status or not valid_pursuit_status(r.track, status):
            result.skipped += 1
            result.problems.append(
                f"row {r.index} ({r.company}: {r.title}): "
                + ("; ".join(r.problems) if r.problems else f"status {status!r} is not valid")
            )
            continue
        when = f"{r.date}T00:00:00+00:00"
        if r.existing_pursuit_id is not None:
            p = await session.get(Pursuit, r.existing_pursuit_id)
            if p is None:
                result.skipped += 1
                result.problems.append(f"row {r.index}: pursuit #{r.existing_pursuit_id} is gone")
                continue
            _record_status(session, p, status, when)
            p.applied_at = p.applied_at or when
            p.allow_reposts = bool(choice.allow_reposts) or p.allow_reposts
            if r.notes and (p.notes_md or "").find(r.notes) < 0:
                p.notes_md = ((p.notes_md or "") + "\n\n" + r.notes).strip()
            result.updated += 1
            result.pursuit_ids.append(p.id)
            continue
        source = await _import_source(session, r.track)
        raw = RawPosting(
            external_id=_external_id(r),
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
        existing = (
            await session.execute(select(Pursuit).where(Pursuit.opportunity_id == opp.id))
        ).scalar_one_or_none()
        if existing is not None:
            _record_status(session, existing, status, when)
            existing.applied_at = existing.applied_at or when
            existing.allow_reposts = bool(choice.allow_reposts) or existing.allow_reposts
            result.updated += 1
            result.pursuit_ids.append(existing.id)
            continue
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
        result.created_pursuit_ids.append(p.id)
    if commit_transaction:
        await session.commit()
    else:
        await session.flush()
    return result


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
