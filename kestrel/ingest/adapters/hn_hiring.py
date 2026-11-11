"""Hacker News "Who is hiring?" (official HN API, no key): the monthly thread, one posting per
top-level comment.

GET https://hacker-news.firebaseio.com/v0/user/whoishiring.json -> {submitted: [item ids]}
GET https://hacker-news.firebaseio.com/v0/item/{id}.json -> a story ({title, kids, time}) or a
comment ({text (HTML), time, deleted?, dead?}); a missing item answers `null`.
The account also posts "Who wants to be hired?", so the thread is picked by its title. The first
line of a comment is asked to read `Company | Role | Location | REMOTE | ...`; it is split on `|`
without any model, the whole comment is the description, and a link in it is the apply link. Only
`.json` URLs are requested, which is all that robots.txt allows (the file uses a wildcard Python's
parser cannot read, so no in-client check is made, as for Remotive). 429/5xx are a partial run.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from kestrel.ingest.base import AdapterError, PostingBatch, RawPosting, SourceConfigError

log = structlog.get_logger(__name__)

API_BASE = "https://hacker-news.firebaseio.com/v0"
USER = "whoishiring"
ITEM_URL = "https://news.ycombinator.com/item?id="
SOURCE_NAME = "Hacker News"
SOURCE_URL = "https://news.ycombinator.com"
DEFAULT_MAX_COMMENTS = 100
MAX_COMMENTS_LIMIT = 500
DISCOVERY_LOOKS = 8  # how many of the account's newest items to read when finding the thread
_RATE = 4.0
_ID = re.compile(r"\d{1,12}")
_THREAD_TITLE = re.compile(r"Ask HN: Who is hiring\?", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_HREF = re.compile(r'href="([^"]+)"', re.IGNORECASE)
_REMOTE = re.compile(r"\bremote\b", re.IGNORECASE)
_ONSITE = re.compile(r"\bon-?site\b", re.IGNORECASE)
_HYBRID = re.compile(r"\bhybrid\b", re.IGNORECASE)
_NO_REMOTE = re.compile(
    r"\b(no|not|non|without|never)[- ]remote\b|\bremote[:\s]+(no|not|none)\b|\bremote\s+not\b",
    re.IGNORECASE)
_ORG_URL = re.compile(r"\s*\(\s*(https?://|www\.)[^)]*\)?\s*", re.IGNORECASE)
_TYPE = re.compile(
    r"\b(full[- ]?time|part[- ]?time|contract(or)?|intern(ship)?|freelance)\b", re.IGNORECASE)
_NOT_ROLE = re.compile(
    r"^(remote\b.*|on-?site\b.*|hybrid\b.*|visa\b.*|relocation\b.*|\$.*|equity\b.*)$",
    re.IGNORECASE)


# A word that names a job. A segment that starts with a work style, a visa or equity word is still
# the role when it carries one ("Remote Senior Backend Engineer", "Equity Research Analyst").
_ROLE_WORD = re.compile(
    r"\b(engineers?|engineering|developers?|programmers?|architects?|managers?|directors?|"
    r"designers?|analysts?|scientists?|leads?|heads?|officers?|specialists?|consultants?|"
    r"administrators?|recruiters?|researchers?|writers?|editors?|accountants?|technicians?|"
    r"coordinators?|associates?|assistants?|representatives?|strategists?|producers?|"
    r"interns?|sre|devops|swe|vp|cto|ceo|founder|marketer|nurse|mechanic|plumber|teacher)\b",
    re.IGNORECASE)


def _style_only(segment: str) -> bool:
    """A work style, pay, visa or equity note that is not itself a job title."""
    return bool(_NOT_ROLE.match(segment)) and not _ROLE_WORD.search(segment)


def valid_id(value: Any) -> str | None:
    """The id as a string when it is a plain whole number (the only thing allowed in a URL)."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    return str(value) if _ID.fullmatch(str(value)) else None


def _first_line(text_html: str) -> str:
    head = re.split(r"<p>|<br\s*/?>|\n", text_html, maxsplit=1, flags=re.IGNORECASE)[0]
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub("", head))).strip()


def _apply_link(text_html: str) -> str | None:
    for found in _HREF.findall(text_html):
        link = html.unescape(found)
        if link.lower().startswith(("https://", "http://")):
            return link
    return None


def to_posting(item: dict) -> RawPosting | None:
    """One top-level comment as a RawPosting; None for a deleted, dead, empty or non-comment."""
    if not isinstance(item, dict) or item.get("type") != "comment":
        return None
    if item.get("deleted") or item.get("dead"):
        return None
    item_id = valid_id(item.get("id"))
    text = item.get("text")
    if item_id is None or not isinstance(text, str) or not text.strip():
        return None
    header = _first_line(text)
    parts = [p.strip() for p in header.split("|") if p.strip()]
    org = (_ORG_URL.sub(" ", parts[0]).strip()[:200] or None) if len(parts) >= 2 else None
    rest = parts[1:] if len(parts) >= 2 else parts
    # Role and location are the first two segments that are not a work style, pay, visa or an
    # employment type: the title is one segment, the location is the next one when present.
    roles = [p for p in rest if not _style_only(p) and not _TYPE.fullmatch(p)]
    # The title is the first segment that names a job, wherever it sits ("City | Role | Onsite");
    # with none that does, the first segment, as before. The place is the next segment.
    lead = next((p for p in roles if _ROLE_WORD.search(p)), roles[0] if roles else None)
    title = (lead if len(parts) >= 2 and lead is not None else header)[:200] or None
    others = [p for p in roles if p is not lead]
    place = others[0][:200] if len(parts) >= 2 and lead is not None and others else None
    negated = any(_NO_REMOTE.search(p) for p in rest)
    remote_part = next(
        (p for p in rest if _style_only(p) and _REMOTE.search(_NO_REMOTE.sub(" ", p))), None)
    employment = next((p for p in rest if _TYPE.fullmatch(p)), None)
    if remote_part:
        is_remote: bool | None = True
    elif negated or any(_ONSITE.search(p) for p in rest):
        is_remote = False
    else:
        is_remote = None
    hybrid = any(_HYBRID.search(p) for p in rest)
    location = place or (remote_part[:200] if remote_part else None)
    link = f"{ITEM_URL}{item_id}"
    posted = item.get("time")
    date = None
    if isinstance(posted, (int, float)) and not isinstance(posted, bool) and posted > 0:
        try:
            date = datetime.fromtimestamp(posted, tz=UTC).date().isoformat()
        except (OverflowError, OSError, ValueError):
            date = None  # a time no calendar holds: the comment is kept without a date
    return RawPosting(
        external_id=item_id,
        url=link,
        apply_url=_apply_link(text) or link,
        title=title,
        org_name=org,
        location_raw=location,
        description_html=text,
        employment_type=employment,
        is_remote=is_remote,
        remote_hint="Hybrid" if hybrid and not remote_part else None,
        date_posted=date,
        raw={"via": SOURCE_NAME, "via_url": SOURCE_URL, "header": header[:300]},
    )


def _problem(status: int) -> str:
    if status in (403, 429):
        return "Hacker News asked Kestrel to slow down; the rest is fetched on the next check"
    if status >= 500:
        return "Hacker News had a problem on their side; the rest is fetched on the next check"
    return f"Hacker News answered with an unexpected status ({status})"


class _Slow(Exception):
    """A 403/429/5xx: stop and keep what was collected."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _Unreachable(AdapterError):
    """A transport failure: a failed run when nothing was collected, else a partial one."""


class HnHiringAdapter:
    kind = "hn_hiring"

    def validate_config(self, config: dict) -> None:
        value = config.get("max_comments", DEFAULT_MAX_COMMENTS)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= MAX_COMMENTS_LIMIT
        ):
            raise SourceConfigError(
                f"'max_comments' must be a whole number from 1 to {MAX_COMMENTS_LIMIT}")
        thread = config.get("thread_id")
        if thread is not None and (isinstance(thread, bool) or valid_id(thread) is None):
            raise SourceConfigError("'thread_id' must be the number in the thread's address")

    async def fetch(self, config: dict, client: Any) -> PostingBatch:
        self.validate_config(config)
        limit = config.get("max_comments", DEFAULT_MAX_COMMENTS)
        batch = PostingBatch()
        try:
            story = await self._thread(config, client)
            if story is None:
                batch.partial_error = "No Who is hiring? thread was found on Hacker News yet"
                return batch
            seen: set[str] = set()
            kids = story.get("kids")
            for kid in (kids if isinstance(kids, list) else []):
                if len(seen) >= limit:
                    break
                kid_id = valid_id(kid)
                if kid_id is None or kid_id in seen:
                    continue
                seen.add(kid_id)
                item = await self._item(client, kid_id) or {}
                try:
                    posting = to_posting(item)
                except (ValueError, TypeError, OverflowError, OSError, AttributeError) as exc:
                    log.warning("hn_hiring_record_skipped", error=type(exc).__name__)
                    continue  # one odd comment must not lose the others
                if posting is not None:
                    batch.append(posting)
        except _Slow as slow:
            batch.partial_error = slow.message
        except _Unreachable as gone:
            if not batch:
                raise AdapterError(str(gone)) from gone
            batch.partial_error = f"{gone}; the rest is fetched on the next check"
        except AdapterError as err:
            if not batch:
                raise
            # a later item answered with an error or something unreadable: keep what was read
            batch.partial_error = f"{err}; the rest is fetched on the next check"
        return batch

    async def _thread(self, config: dict, client: Any) -> dict | None:
        explicit = valid_id(config.get("thread_id"))
        if explicit is not None:
            story = await self._item(client, explicit)
            if not isinstance(story, dict) or story.get("type") != "story":
                raise AdapterError("That Hacker News thread was not found")
            return story
        user = await self._get(client, f"{API_BASE}/user/{USER}.json")
        if not isinstance(user, dict) or not isinstance(user.get("submitted"), list):
            raise AdapterError(
                "Hacker News sent a reply Kestrel could not read; the format may have changed")
        for raw_id in user["submitted"][:DISCOVERY_LOOKS]:
            item_id = valid_id(raw_id)
            story = await self._item(client, item_id) if item_id else None
            if (isinstance(story, dict) and story.get("type") == "story"
                    and not story.get("dead") and not story.get("deleted")
                    and _THREAD_TITLE.match(str(story.get("title") or ""))):
                return story
        return None

    async def _item(self, client: Any, item_id: str) -> dict | None:
        data = await self._get(client, f"{API_BASE}/item/{item_id}.json")
        return data if isinstance(data, dict) else None

    async def _get(self, client: Any, url: str) -> Any:
        try:
            # No check_robots: robots.txt allows only `*.json`, which the stdlib parser cannot read.
            r = await client.request("GET", url, rate_per_sec=_RATE, retries=1)
        except httpx.HTTPStatusError as exc:
            r = exc.response
        except httpx.HTTPError as exc:
            log.warning("hn_hiring_unreachable", error=type(exc).__name__)
            raise _Unreachable("Could not reach Hacker News; try again later") from exc
        if r.status_code != 200:
            log.warning("hn_hiring_error", status=r.status_code)
            if r.status_code in (403, 429) or r.status_code >= 500:
                raise _Slow(_problem(r.status_code))
            raise AdapterError(_problem(r.status_code))
        try:
            return r.json()
        except ValueError as exc:
            raise AdapterError(
                "Hacker News sent a reply Kestrel could not read; the format may have changed"
            ) from exc
