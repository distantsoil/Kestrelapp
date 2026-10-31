"""The update checker (SPEC-INTERFACES.md 1.5, D-UPD-1..5): tells the operator a newer Kestrel
exists and what changed, and points them at the download page. It never downloads or installs
anything; the operator chooses if and when to update.

Two public promises bind this module:
1. Asking is fine; installing on its own is not. The only outbound call is a GET of the static
   feed at FEED_URL. There is deliberately no download function here.
2. Send nothing about the user. The request carries only a fixed User-Agent, Accept and an
   If-None-Match validator: no ID, no version, no query string, no cookies. The ingest
   HttpClient is not reused because its User-Agent carries the operator's contact address.

State lives in one `setting` row (STATE_KEY, listed in setting_overrides.TRANSIENT_KEYS) so a
restart keeps the last answer (hard rule 5)."""

from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import structlog
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.db.base import utcnow_iso
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Setting
from kestrel.services.notifications import NotificationService

log = structlog.get_logger()

FEED_URL = "https://kestrelapp.co/latest.json"
DOWNLOAD_PAGE = "https://kestrelapp.co/download"
_DOWNLOAD_HOSTS = frozenset({"kestrelapp.co", "www.kestrelapp.co"})
FEED_SCHEMA = 1
USER_AGENT = "Kestrel-Updater"
STATE_KEY = "update_state"
TIMEOUT_SECONDS = 10.0
FIRST_CHECK_DELAY_SECONDS = 60
MAX_RELEASES_IN_FEED = 10

# The complete set of request headers the checker may send (besides If-None-Match when an ETag
# is cached). A test pins this; adding to it needs a decision log entry.
BASE_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

_RETRY_BACKOFF_SECONDS = 1.0
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")


# --- feed contract (shared with scripts/build_latest_json.py) ---------------------------------


class Download(BaseModel):
    file: str
    url: str
    sha256: str
    signed: bool = False


class ReleaseNote(BaseModel):
    version: str
    date: str  # YYYY-MM-DD
    notes_md: str


class LatestFeed(BaseModel):
    """The `latest.json` served by kestrelapp.co. The app reads schema, version, download_page and
    releases; `downloads` is for the website's download buttons."""

    schema_: int = Field(alias="schema")
    version: str
    published_at: str
    download_page: str
    downloads: dict[str, Download] = Field(default_factory=dict)
    releases: list[ReleaseNote] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


# --- persisted state --------------------------------------------------------------------------


class UpdateState(BaseModel):
    last_checked_at: str | None = None  # ISO UTC
    etag: str | None = None
    latest_version: str | None = None
    download_page: str | None = None
    cached_feed: dict[str, Any] | None = None  # last good LatestFeed, by alias
    skipped_version: str | None = None
    remind_after: str | None = None  # ISO UTC; banner hidden until then
    last_seen_version: str | None = None  # the version the operator last ran
    notified_version: str | None = None  # tray/bell notification sent once per version
    disclosure_shown: bool = False
    upgraded_from: str | None = None  # previous version after an upgrade; clears on dismiss
    last_check_ok: bool = True  # False when the latest check could not reach the feed


# --- pure helpers -----------------------------------------------------------------------------


def parse_version(text: str) -> tuple[int, int, int, tuple[Any, ...]] | None:
    """`v1.2.3`, `1.2.3`, `1.2.3-rc.1` -> a sortable key; anything else -> None. A release sorts
    after any prerelease of the same number. No third-party dependency."""
    m = _VERSION.match(text.strip()) if isinstance(text, str) else None
    if m is None:
        return None
    major, minor, patch, pre = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
    if pre is None:
        return major, minor, patch, (1,)
    # Numeric identifiers sort numerically and below alphanumeric ones (semver 11.4).
    idents = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return major, minor, patch, (0, idents)


def is_newer(candidate: str, current: str) -> bool:
    """True only when both parse and candidate > current. Unparseable -> False (never nag)."""
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and b is not None and a > b


def pending_releases(feed: LatestFeed, current: str) -> list[ReleaseNote]:
    """Every release note newer than `current`, newest first."""
    newer = [r for r in feed.releases if is_newer(r.version, current)]
    newer.sort(key=lambda r: parse_version(r.version) or (0, 0, 0, (0, ())), reverse=True)
    return newer


def render_notes_html(notes_md: str) -> str:
    """Escape everything, then render only `### heading`, `- bullet` and paragraphs. Raw HTML in
    the notes is always shown as text."""
    out: list[str] = []
    bullets: list[str] = []
    para: list[str] = []

    def flush_bullets() -> None:
        if bullets:
            out.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    def flush_para() -> None:
        if para:
            out.append("<p>" + " ".join(para) + "</p>")
            para.clear()

    for raw in html.escape(notes_md or "").splitlines():
        line = raw.strip()
        if not line:
            flush_bullets()
            flush_para()
        elif line.startswith("### "):
            flush_bullets()
            flush_para()
            out.append(f"<h4>{line[4:].strip()}</h4>")
        elif line.startswith("- "):
            flush_para()
            bullets.append(line[2:].strip())
        else:
            flush_bullets()
            para.append(line)
    flush_bullets()
    flush_para()
    return "\n".join(out)


def safe_download_page(url: object) -> str | None:
    """The feed's download_page only if it is plain https on kestrelapp.co (or www): no query,
    fragment, userinfo, port or control characters. Anything else -> None with a warning, because
    the value ends up in an href and the feed is remote input."""
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        parts = None
    ok = (
        parts is not None
        and url.strip() == url
        and url.isprintable()
        and " " not in url
        and "\\" not in url
        and parts.scheme == "https"
        and parts.netloc.lower() in _DOWNLOAD_HOSTS  # excludes userinfo and port by equality
        and "?" not in url
        and "#" not in url
    )
    if not ok or parts is None:
        log.warning("update_download_page_rejected", value=url[:80])
        return None
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, "", ""))


def seconds_until_next_check(state: UpdateState, check_hours: int, now: datetime) -> float:
    """Seconds the update loop should wait before its first check: never less than
    FIRST_CHECK_DELAY_SECONDS, and long enough that a restart does not re-ask the feed before
    last_checked_at + check_hours."""
    wait = float(FIRST_CHECK_DELAY_SECONDS)
    if state.last_checked_at:
        due = _parse_iso(state.last_checked_at) + timedelta(hours=max(1, check_hours))
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        wait = max(wait, (due - now).total_seconds())
    return wait


def banner_version(state: UpdateState, current: str, now_iso: str) -> str | None:
    """The version to advertise in the banner, or None: hidden when not newer, skipped, or
    remind_after is still in the future."""
    latest = state.latest_version
    if not latest or not is_newer(latest, current):
        return None
    if state.skipped_version and not is_newer(latest, state.skipped_version):
        return None
    if state.remind_after and _parse_iso(state.remind_after) > _parse_iso(now_iso):
        return None
    return latest


def _parse_iso(text: str) -> datetime:
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return _EPOCH  # unreadable timestamps count as long past: never hide the banner on them
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# --- I/O --------------------------------------------------------------------------------------


async def load_state(session: AsyncSession) -> UpdateState:
    row = await session.get(Setting, STATE_KEY)
    if row is None or not isinstance(row.value, dict):
        return UpdateState()
    try:
        return UpdateState.model_validate(row.value)
    except ValidationError as exc:
        log.warning("update_state_invalid", error_type=type(exc).__name__)
        return UpdateState()


async def save_state(session: AsyncSession, state: UpdateState) -> None:
    value = state.model_dump(mode="json")
    row = await session.get(Setting, STATE_KEY)
    if row is None:
        session.add(Setting(key=STATE_KEY, value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()
    await session.commit()


async def fetch_feed(
    feed_url: str, etag: str | None, *, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[LatestFeed | None, str | None, bool]:
    """One GET. Returns (feed, etag, not_modified). 304 -> (None, etag, True). 404, timeout,
    non-2xx, bad JSON, or schema != FEED_SCHEMA -> (None, etag, False) after logging a warning.
    One retry after a short backoff on 429/5xx only. Never raises for network or parse errors."""
    headers = dict(BASE_HEADERS)
    if etag:
        headers["If-None-Match"] = etag
    try:
        async with httpx.AsyncClient(
            headers=headers, timeout=TIMEOUT_SECONDS, transport=transport, follow_redirects=True
        ) as client:
            resp = await client.get(feed_url)
            if resp.status_code == 429 or resp.status_code >= 500:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
                resp = await client.get(feed_url)
    except httpx.HTTPError as exc:
        log.warning("update_feed_failed", url=feed_url, error_type=type(exc).__name__)
        return None, etag, False
    if resp.status_code == 304:
        return None, etag, True
    if not 200 <= resp.status_code < 300:
        log.warning("update_feed_status", url=feed_url, status=resp.status_code)
        return None, etag, False
    try:
        data = resp.json()
        if not isinstance(data, dict) or data.get("schema") != FEED_SCHEMA:
            log.warning("update_feed_schema", url=feed_url, error_type="SchemaMismatch")
            return None, etag, False
        feed = LatestFeed.model_validate(data)
    except (ValueError, json.JSONDecodeError, ValidationError) as exc:
        log.warning("update_feed_invalid", url=feed_url, error_type=type(exc).__name__)
        return None, etag, False
    feed.releases = feed.releases[:MAX_RELEASES_IN_FEED]
    return feed, resp.headers.get("etag") or etag, False


async def check_now(
    session: AsyncSession,
    *,
    feed_url: str = FEED_URL,
    current: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    notify_sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> UpdateState:
    """Fetch, update and persist state; on failure keep prior state and still stamp
    last_checked_at. Sends one NotificationService notice (kind="update") the first time a given
    newer version is seen, through the app's shared sessionmaker unless `notify_sessionmaker` is
    given, and records notified_version only once that succeeds. Returns the new state."""
    if current is None:
        from kestrel import __version__ as current
    state = await load_state(session)
    feed, new_etag, not_modified = await fetch_feed(feed_url, state.etag, transport=transport)
    state.last_checked_at = utcnow_iso()
    state.last_check_ok = feed is not None or not_modified
    notify_version: str | None = None
    if feed is not None:
        old_latest = state.latest_version
        state.etag = new_etag
        state.latest_version = feed.version
        state.download_page = safe_download_page(feed.download_page)
        state.cached_feed = feed.model_dump(by_alias=True)
        if (
            state.skipped_version
            and old_latest != feed.version
            and is_newer(feed.version, state.skipped_version)
        ):
            state.skipped_version = None
    if (
        (feed is not None or not_modified)
        and state.latest_version
        and is_newer(state.latest_version, current)
        and state.notified_version != state.latest_version
    ):
        notify_version = state.latest_version
    await save_state(session, state)
    if notify_version is not None:
        try:
            await NotificationService(notify_sessionmaker or get_sessionmaker()).notify(
                "update",
                f"Kestrel {notify_version} is available",
                body="See what's new and get it from kestrelapp.co when you're ready.",
                severity="info",
            )
        except Exception as exc:  # a notice must never fail the check; the banner still shows
            log.warning("update_notify_failed", error_type=type(exc).__name__)
        else:
            state.notified_version = notify_version  # only now: a failed notice retries next check
            await save_state(session, state)
    return state


async def mark_seen(session: AsyncSession, current: str | None = None) -> str | None:
    """Called at startup. If the running version differs from last_seen_version, record it and
    return the previous value (drives the one-time "Here's what changed" card); else None."""
    if current is None:
        from kestrel import __version__ as current
    state = await load_state(session)
    if state.last_seen_version == current:
        return None
    previous = state.last_seen_version
    state.last_seen_version = current
    await save_state(session, state)
    return previous


async def skip_version(session: AsyncSession, version: str) -> None:
    state = await load_state(session)
    state.skipped_version = version
    await save_state(session, state)


async def remind_later(session: AsyncSession, hours: int = 24) -> None:
    state = await load_state(session)
    state.remind_after = (_parse_iso(utcnow_iso()) + timedelta(hours=hours)).isoformat()
    await save_state(session, state)
