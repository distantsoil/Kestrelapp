"""The source-adapter contract. An adapter maps ONE platform's payload (with its counter-intuitive
field names, SPEC.md Appendix A) into a list of canonical RawPosting objects. It never touches the
database or the shared canonicalization; normalize.py does that. This clean seam is what lets the
ATS adapters (Greenhouse, Lever, Ashby, SmartRecruiters, Workday) and the JobSpy
board adapter be written independently."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

import httpx

from kestrel.errors import ROBOTS_SENTENCE, UserFacingError


class SourceConfigError(UserFacingError, ValueError):
    """A source config is structurally invalid. Raised at config load / source creation, never at
    scan time, so a dead config never silently produces an empty run."""


# A board id that is spliced into a URL path (after a fixed host). One path segment: no slash, no
# query or fragment mark, no userinfo or port, no whitespace, not a dot segment (D-SEC-10).
_SEGMENT = re.compile(r"[^\s/\\?#@:%]{1,100}")


def path_segment(config: dict, key: str, what: str) -> str:
    """CONFIG[KEY] as one safe URL path segment, or SourceConfigError naming WHAT."""
    value = config.get(key)
    if not isinstance(value, str) or value in (".", "..") or not _SEGMENT.fullmatch(value):
        raise SourceConfigError(f"{what} is not valid: use only the board's own id, with no "
                                "slash, question mark, at sign, colon or space")
    return value


class NoticeKind(StrEnum):
    """Why a fetch stopped early or left something out, chosen where it happened. The kind, not
    the words, decides the error code of a run (`failure_code.code_for_kind`) and whether a
    health check reads it as a cause (D-ERR-24 round 9)."""

    ROBOTS_REFUSED = "robots_refused"  # robots.txt disallowed a page Kestrel needed
    RATE_LIMITED = "rate_limited"  # a 429: the site asked Kestrel to slow down
    SERVER_ERROR = "server_error"  # a 5xx: a problem on the site's side
    NEEDS_KEY = "needs_key"  # a key is missing or was turned down
    DAILY_LIMIT = "daily_limit"  # a daily quota is used up
    BODY_UNREAD = "body_unread"  # a role's own page or detail failed for another reason
    FEEDS_OFF = "feeds_off"  # the site has its public job feed turned off
    CAPPED = "capped"  # Kestrel stopped at its own limit; nothing went wrong
    STOPPED = "stopped"  # any other reason, said in the adapter's own words (the general code)


# The kinds whose sentence is shared by every adapter: a Notice or a Problem of one of these kinds
# takes its words only from the builders below (or, built with the kind alone, from SHARED_DEFAULT),
# so no board can word a robots refusal or a 429 its own way (D-ERR-24 round 10).
SHARED_KINDS = frozenset(NoticeKind) - {NoticeKind.STOPPED}  # every known kind; only the
# general kind (`stopped`) takes an adapter's own words (D-ERR-24 round 11)
_BUILT = object()  # the private token the shared builders pass; nothing else holds it
_UNSET = "\x00unset"  # Notice's default sentence; compared by identity, so "" is still refused


def _rate_sentence(board: str, where: str | None = None, count: int = 0,
                   kept: bool = False) -> str:
    tail = _outcome(count, kept) if count else ""
    return f"{board} is rate limiting requests{_at(where)}.{tail} Kestrel will try again next run."


def _outcome(count: int, kept: bool) -> str:
    roles = "1 of its roles was" if count == 1 else f"{count} of its roles were"
    done = "added without a description" if kept else "left out this run"
    return f" {roles} {done}."


def _at(where: str | None) -> str:
    return f" for {where}" if where else ""


def _server_sentence(board: str, where: str | None = None) -> str:
    return f"{board} had a problem on their side{_at(where)}. Kestrel will try again next run."


def _unread_sentence(board: str, where: str | None, count: int, kept: bool) -> str:
    at = f" at {where}" if where else ""
    done = "added without a description" if kept else "left out this run"
    return (f"{board} did not return the details of {count} of its roles{at}. They were {done}. "
            "Kestrel will try again next run.")


def _key_sentence(board: str, fields: str) -> str:
    return (f"{board} needs your {fields}. Add them on the Sources page under Sources that need "
            "your own key")


def _key_rejected_sentence(board: str, fields: str) -> str:
    return (f"{board} did not accept your {fields}. Save them again on the Sources page under "
            "Sources that need your own key")


def _daily_sentence(board: str, cap: int | None = None) -> str:
    if cap is None:
        return f"{board}'s daily limit was reached. Kestrel will fetch again tomorrow."
    return (f"{board} has a daily limit of {cap} fetches, and today's are used. Kestrel will "
            "fetch again tomorrow.")


def _feeds_off_sentence(domain: str) -> str:
    return (f"{domain} has its public job feed turned off, so Kestrel cannot read its roles. "
            "Open the roles you want and add each with One posting.")


SHARED_DEFAULT = {
    NoticeKind.ROBOTS_REFUSED: ROBOTS_SENTENCE,
    NoticeKind.RATE_LIMITED: _rate_sentence("The site"),
    NoticeKind.SERVER_ERROR: _server_sentence("The site"),
    NoticeKind.NEEDS_KEY: ("This source needs your own key. Add it on the Sources page under "
                           "Sources that need your own key"),
    NoticeKind.DAILY_LIMIT: _daily_sentence("This source"),
    NoticeKind.BODY_UNREAD: ("The site did not return the details of some of its roles. Kestrel "
                             "will try again next run."),
    NoticeKind.FEEDS_OFF: _feeds_off_sentence("The site"),
    NoticeKind.CAPPED: ("Kestrel stopped at its own limit for one run; the rest is fetched on the "
                        "next check"),
}


def _check_shared(kind: object, via: object) -> None:
    if kind in SHARED_KINDS and via is not _BUILT:
        raise ValueError(f"a {kind} sentence comes only from the shared builders in base.py")


class Problem(str):
    """A sentence an adapter wrote about a failure, with the kind of failure the adapter chose
    for it where it chose the sentence (`kind`, or None for the general one) and the HTTP status
    it answered (`status`, for logs only). It is a plain `str` to every reader. failure_code
    reads `kind`, never the words and never the status (D-ERR-20, D-ERR-24 round 9)."""

    status: int | None
    _kind: NoticeKind | None

    def __new__(
        cls, text: str, status: int | None = None, kind: NoticeKind | None = None, *,
        _via: object = None,
    ) -> Problem:
        if kind is not None and not isinstance(kind, NoticeKind):
            raise TypeError("Problem kind must be a NoticeKind")
        _check_shared(kind, _via)
        obj = super().__new__(cls, text)
        obj.status = status
        obj._kind = kind
        return obj

    @property
    def kind(self) -> NoticeKind | None:
        """The kind chosen with the words; read only, so it can never be swapped afterwards."""
        return self._kind

    @property
    def code(self) -> str | None:
        """The SRC code of `kind` (None for the general code); never chosen apart from it."""
        from kestrel.ingest import failure_code

        return failure_code.code_for_kind(self.kind)


@dataclass(frozen=True)
class Notice:
    """One reason a fetch stopped early or left something out: its kind and the sentence the
    person reads, chosen in one statement so they cannot disagree."""

    kind: NoticeKind
    sentence: str = _UNSET  # a shared kind built with the kind alone takes SHARED_DEFAULT

    def __post_init__(self) -> None:
        if not isinstance(self.kind, NoticeKind):
            raise TypeError("Notice kind must be a NoticeKind")
        if self.kind in SHARED_KINDS and self.sentence is _UNSET:
            object.__setattr__(self, "sentence", SHARED_DEFAULT[self.kind])
        elif self.kind in SHARED_KINDS:  # free words, also through dataclasses.replace
            _check_shared(self.kind, None)
        elif self.sentence is _UNSET:  # the general kind has no default: it needs its words
            raise ValueError("a general Notice needs the adapter's own sentence")
        if not isinstance(self.sentence, str) or not self.sentence.strip():
            raise ValueError("a Notice needs a sentence the person can read")

    @classmethod
    def of(cls, message: str) -> Notice:
        """The notice for a sentence an adapter wrote: a `Problem` keeps the kind it was given
        (a shared kind's Problem could only be built by a shared builder), the shared robots
        sentence is a robots refusal, and anything else is the general kind."""
        kind = kind_of(message) or NoticeKind.STOPPED
        if kind is NoticeKind.ROBOTS_REFUSED and message == ROBOTS_SENTENCE:
            return cls(kind)
        if isinstance(message, Problem) and message.kind in SHARED_KINDS:
            return _built(kind, str(message))  # only a shared builder can make such a Problem
        return cls(kind, str(message))


def _built(kind: NoticeKind, sentence: str) -> Notice:
    """A shared-kind Notice whose words a shared builder made. It bypasses __init__ (and so
    the refusal of free words), which only the builders in this module do."""
    notice = object.__new__(Notice)
    object.__setattr__(notice, "kind", kind)
    object.__setattr__(notice, "sentence", sentence)
    return notice


def kind_of(message: object) -> NoticeKind | None:
    """The kind an adapter gave a sentence: a Problem's own kind, or the shared robots sentence
    (an exported constant, matched by equality). None when the adapter named none."""
    kind = getattr(message, "kind", None)
    if isinstance(kind, NoticeKind):
        return kind
    if isinstance(message, str) and message == ROBOTS_SENTENCE:
        return NoticeKind.ROBOTS_REFUSED
    return None


class AdapterError(Exception):
    """A recoverable fetch failure (HTTP error, bad slug, upstream shape change). The run becomes
    `partial` with this message; it NEVER aborts the batch (hard rule: uncertainty escalates)."""

    def __init__(self, *args: object, code: str | None = None) -> None:
        super().__init__(*args)
        self.code = code  # a registered SRC code the raising adapter chose; None is the general one


class ShapeError(AdapterError):
    """The reply arrived but is not laid out the way the adapter reads it. Raised where the
    sentence says so ("had an unexpected shape"), so a health check can name the cause."""


@dataclass
class RawPosting:
    """Canonical intermediate. Adapters fill what their payload provides; None means unknown.
    Exactly one of description_html / description_md is expected (normalize prefers md, else
    unescapes+markdownifies html)."""

    external_id: str | None = None
    url: str | None = None
    apply_url: str | None = None
    title: str | None = None
    org_name: str | None = None
    org_url: str | None = None
    location_raw: str | None = None
    description_html: str | None = None
    description_md: str | None = None
    employment_type: str | None = None
    is_remote: bool | None = None
    remote_hint: str | None = None  # workplaceType / "Remote"|"Hybrid"|"On-site"
    comp_min: float | None = None
    comp_max: float | None = None
    comp_currency: str | None = None
    comp_interval: str | None = None  # 'year' | 'month' | 'week' | 'day' | 'hour'
    # True when the adapter filled the interval or currency in itself (a board that sends no
    # pay period, a currency implied by the search country, a period guessed from the size of
    # the figure). A default is not a stated value: normalize stores it as not known (#208).
    comp_interval_assumed: bool = False
    comp_currency_assumed: bool = False
    # The board's own estimate (Glassdoor's p10-p90 pay band), not pay the employer posted:
    # stored with comp_source 'inferred' so it is labelled an estimate everywhere (issue #66).
    comp_is_estimate: bool = False
    date_posted: str | None = None  # ISO date/datetime string
    # Org attribute hints inferred from the payload (Greenhouse metadata[], JobSpy company_* labels,
    # etc.). Left None rather than guessed; four consulting Tier 0 rules key off these.
    size_band: str | None = None
    sector: str | None = None
    ownership: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class PostingBatch(list):
    """The postings one fetch collected (a plain list to every consumer) plus `notices`: every
    reason the fetch stopped early, left something out or found nothing it could ask for (a key
    not saved yet, a 429, a robots refusal, its own cap). Adapters append, never overwrite, so
    no reason hides another. The runner records the run as `partial` with their sentences and
    keeps the postings already collected; it does not count a failure of the source, so it
    cannot auto-disable it."""

    notices: list[Notice]

    def __init__(self, items: Any = ()) -> None:
        super().__init__(items)
        self.notices = []

    def note(self, kind: NoticeKind, sentence: str) -> None:
        """Add one reason, its kind and its sentence chosen together."""
        self.notices.append(Notice(kind, sentence))

    def add(self, notice: Notice) -> None:
        if not isinstance(notice, Notice):
            raise TypeError("PostingBatch.add takes a Notice")
        self.notices.append(notice)

    @property
    def partial_error(self) -> str | None:
        """Every notice's sentence, in the order they were added; None when there is none. Read
        only: an adapter adds a `Notice` instead of writing words here."""
        return " ".join(n.sentence for n in self.notices) or None


def failure_kind(exc: BaseException) -> NoticeKind:
    """The kind of one page or detail that could not be read, from the exception's type only:
    a robots refusal, a 429, a 5xx, or anything else (a 404, a network error) as unread."""
    if isinstance(exc, PermissionError):
        return NoticeKind.ROBOTS_REFUSED
    if isinstance(exc, httpx.HTTPStatusError):
        return status_kind(exc.response.status_code)
    return NoticeKind.BODY_UNREAD


def status_kind(status: int) -> NoticeKind:
    """The kind of a page that answered `status` instead of 200."""
    if status == 429:
        return NoticeKind.RATE_LIMITED
    return NoticeKind.BODY_UNREAD


def robots_refused() -> Notice:
    """The one robots sentence for every adapter (SRC-005)."""
    return Notice(NoticeKind.ROBOTS_REFUSED)


def rate_limited(board: str, where: str | None = None, count: int = 0, *,
                 kept: bool = False) -> Notice:
    """The one rate limiting sentence (a 429, SRC-002): the board, where, and what happened to
    the roles it held back (`count`; `kept` when they were added without a description)."""
    return _built(NoticeKind.RATE_LIMITED, _rate_sentence(board, where, count, kept))


def rate_limited_problem(board: str, status: int = 429, where: str | None = None) -> Problem:
    """The shared rate limiting sentence as a Problem, for a `_problem` branch that answers a
    429 (it can also be raised in an AdapterError)."""
    return Problem(_rate_sentence(board, where), status, NoticeKind.RATE_LIMITED, _via=_BUILT)


def server_error(board: str, where: str | None = None) -> Notice:
    """The one sentence for a 5xx (SRC-003)."""
    return _built(NoticeKind.SERVER_ERROR, _server_sentence(board, where))


def server_error_problem(board: str, status: int, where: str | None = None) -> Problem:
    """The shared 5xx sentence as a Problem, for a `_problem` branch (or an AdapterError)."""
    return Problem(_server_sentence(board, where), status, NoticeKind.SERVER_ERROR, _via=_BUILT)


def hedged_refusal(board: str, where: str | None = None) -> str:
    """The one sentence for a 403 and other refusals whose cause is not proven: it may be a rate
    limit or the site being unavailable, so it says both and carries the general code."""
    return (f"{board} is rate limiting or unavailable{_at(where)}. Kestrel will try again next "
            "run.")


def hedged_problem(board: str, status: int, where: str | None = None) -> Problem:
    """`hedged_refusal` as a Problem with no kind (the general code)."""
    return Problem(hedged_refusal(board, where), status)


def body_unread(board: str, where: str | None, count: int, *, kept: bool = False) -> Notice:
    """The one sentence for roles whose own page or detail failed for a reason that is not a
    429 or robots.txt (the general code)."""
    return _built(NoticeKind.BODY_UNREAD, _unread_sentence(board, where, count, kept))


def body_empty(board: str, empty: int, total: int) -> Notice:
    """The one sentence for a board whose new roles came with no description in a share that is
    well above its usual. It is a `STOPPED` notice (the general code, the adapter's own words,
    not a kind with a shared sentence): nothing proves a block, so no specific kind is claimed.
    It states the count and its sample size, and says "may" (D-SRC-641)."""
    return Notice(NoticeKind.STOPPED, f"{board}: {empty} of {total} new roles had no "
                  "description in this run. The site may be blocking automated reads. Worth "
                  "checking: open a posting and paste its description.")


def details_not_read(board: str, count: int, where: str) -> Notice:
    """The notice of an adapter that left out roles whose own detail could not be read, so the
    run is partial and says so instead of returning fewer roles in silence (D-ERR-24)."""
    return body_unread(board, where, count)


def capped(board: str, where: str, count: int, limit: int) -> Notice:
    """Kestrel's own page cap: `count` roles were added without a description beyond the
    `limit` pages it reads in one run. Not a failure; a health check ignores it."""
    roles = "1 role" if count == 1 else f"{count} roles"
    return _built(NoticeKind.CAPPED, f"{board} has {roles} at {where} beyond the {limit} posting "
                  "pages Kestrel reads in one run. They were added without a description. Raise "
                  "max_results on the source to read more.")


def request_limit(board: str) -> Notice:
    """Kestrel's own cap on requests per check (a search board): the rest comes next check."""
    return _built(NoticeKind.CAPPED, f"{board}: stopped at the limit of requests per check; the "
                  "rest is fetched on the next check")


def links_capped(total: int, limit: int) -> Notice:
    """Kestrel's own cap on the job pages one careers page links to."""
    return _built(NoticeKind.CAPPED, f"This page links to {total} job pages. Kestrel reads at "
                  f"most {limit} a run, so {total - limit} were left for later.")


def needs_key(board: str, fields: str) -> Notice:
    """The board needs the person's own key (SRC-006); `fields` names what to save."""
    return _built(NoticeKind.NEEDS_KEY, _key_sentence(board, fields))


def needs_key_sentence(board: str, fields: str) -> str:
    return _key_sentence(board, fields)


def key_rejected_problem(board: str, fields: str, status: int) -> Problem:
    """The board turned the saved key down (SRC-006)."""
    return Problem(_key_rejected_sentence(board, fields), status, NoticeKind.NEEDS_KEY,
                   _via=_BUILT)


def daily_limit(board: str, cap: int | None = None) -> Notice:
    """A daily limit is used up (SRC-007): the provider's (`cap` None) or the source's own."""
    return _built(NoticeKind.DAILY_LIMIT, _daily_sentence(board, cap))


def daily_limit_problem(board: str, status: int) -> Problem:
    return Problem(_daily_sentence(board), status, NoticeKind.DAILY_LIMIT, _via=_BUILT)


def feeds_off(domain: str) -> Notice:
    """The site has its public job feed turned off (the general code)."""
    return _built(NoticeKind.FEEDS_OFF, _feeds_off_sentence(domain))


class Unread:
    """Pages or details that could not be read, counted by the kind each failed with, so a run
    says every cause in that cause's own words."""

    _ORDER = (NoticeKind.ROBOTS_REFUSED, NoticeKind.RATE_LIMITED, NoticeKind.BODY_UNREAD)

    def __init__(self) -> None:
        self.counts: Counter[NoticeKind] = Counter()

    def add(self, kind: NoticeKind) -> None:
        if kind not in self._ORDER:
            raise ValueError(f"not a page failure kind: {kind}")
        self.counts[kind] += 1

    def __bool__(self) -> bool:
        return bool(self.counts)

    def notices(self, board: str, where: str | None, *, kept: bool = False) -> list[Notice]:
        out: list[Notice] = []
        for kind in self._ORDER:
            n = self.counts[kind]
            if not n:
                continue
            if kind is NoticeKind.ROBOTS_REFUSED:
                out.append(robots_refused())
            elif kind is NoticeKind.RATE_LIMITED:
                out.append(rate_limited(board, where, n, kept=kept))
            else:
                out.append(body_unread(board, where, n, kept=kept))
        return out


@runtime_checkable
class SourceAdapter(Protocol):
    """kind matches source.kind. validate_config is PURE (structural) and raises SourceConfigError.
    fetch performs the network I/O via the shared client and returns canonical postings."""

    kind: str

    def validate_config(self, config: dict) -> None: ...

    async def fetch(self, config: dict, client: Any) -> list[RawPosting]: ...
