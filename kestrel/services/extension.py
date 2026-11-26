"""The browser extension's side of Kestrel (issue #186, SPEC-INTERFACES 5, D-EXT-740 to D-EXT-769).

One place for the rules the extension's routes and the app's Settings > Browser extension card
share: which roles are waiting for a description, whether a page the person sent looks like a bot
check rather than a posting, whether its title matches the role they picked, the three feature
switches, and the "last seen" note. Nothing here fetches anything or talks to any site: the
extension hands Kestrel the text of the page the person already has open, and only when they click.

Page text is hostile input (D-SEC-20). It is read as a string, measured, and either refused or
stored through ingest.normalize.attach_description; no field of it is ever adopted as anything but
the description text of the one role the person chose.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import and_
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Opportunity, Setting
from kestrel.web.fit import EMPTY_POSTING_STAGE_REASON

# ---------------------------------------------------------------------------- feature switches
# One value each, stored in Kestrel, edited from the extension's options page or the app's
# Settings > Browser extension card (scope 4.4, 4.5). Default on.
FEATURES: tuple[str, ...] = ("badges", "waiting", "toolbar_count")
FEATURE_PREFIX = "extension."
LAST_SEEN_KEY = "extension.last_seen"
LAST_SEEN_EVERY_S = 60  # write the note at most once a minute

# The store pages (scope 4.5). A listing that is not live yet stays empty, and its button opens the
# Help page that explains loading the extension by hand. The Chrome listing is live (D-EXT-960);
# the value is the exact listing URL with no query, pinned by tests/test_settings_extension.py.
STORE_URLS: dict[str, str] = {
    "chrome": ("https://chromewebstore.google.com/detail/kestrel-capture/"
               "fdnolbohdjgelmfbhkbedlbjckfebaib"),
    "firefox": "",
}

# ---------------------------------------------------------------------------- check pages
# Scope section 5: a page under this many words, or with any of these phrases, is a check page or
# a fragment, never a posting. One list, a test per phrase (tests/test_extension_api.py).
MIN_WORDS = 80
CHECK_PAGE_PHRASES: tuple[str, ...] = (
    "verify you are human",
    "are you a robot",
    "checking your browser",
    "enable javascript and cookies",
    "just a moment",
    "access denied",
    "complete the security check",
    "press and hold",
    "captcha",
)
# The most text one Read stores, the same cap as the extension's extractor.
MAX_CHARS = 40000
FIRST_LINES = 6

_WORD = re.compile(r"\w+", re.UNICODE)
_BOARD_SUFFIX = re.compile(r"\s[|\-–—·:]\s.*$")


def word_count(text: str) -> int:
    return len(_WORD.findall(text or ""))


# A check page is short. A real posting often names a phrase in passing ("This site is protected
# by reCAPTCHA" under an apply form), so a phrase only marks a page this short as a check page;
# a longer page with one is read as the posting it is (uncertainty never rejects, D-EXT-768).
CHECK_PAGE_MAX_WORDS = 300


def page_problem(text: str) -> str | None:
    """"check_page", "too_short" or None for the text of a page the person sent. A phrase is
    checked first: a check page is short too, and "finish the check" is the useful answer."""
    low = " ".join((text or "").lower().split())
    words = word_count(text)
    if words <= CHECK_PAGE_MAX_WORDS and any(phrase in low for phrase in CHECK_PAGE_PHRASES):
        return "check_page"
    if words < MIN_WORDS:
        return "too_short"
    return None


_LINKEDIN = ("linkedin.com", "linkedin.cn", "lnkd.in")


def is_linkedin(url: str | None) -> bool:
    """LinkedIn is never read (D-SRC-5): a LinkedIn posting is pasted by hand in Kestrel."""
    host = host_of(url)
    return any(host == d or host.endswith("." + d) for d in _LINKEDIN)


def first_lines(text: str, count: int = FIRST_LINES) -> str:
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]
    return "\n".join([line for line in lines if line][:count])


def _title_words(title: str) -> list[str]:
    plain = _BOARD_SUFFIX.sub("", " ".join((title or "").split()))
    return [w.lower() for w in _WORD.findall(plain)]


def titles_match(role_title: str | None, page_title: str | None) -> bool:
    """Scope section 5: both titles lowercased, punctuation and a board suffix ("| Indeed")
    stripped; a match when every word of the role's title is in the page's title, or when the
    words overlap by at least 0.6 of the role's. A miss warns; it never blocks the save."""
    role = _title_words(role_title or "")
    page = set(_title_words(page_title or ""))
    if not role or not page:
        return False
    hits = sum(1 for w in role if w in page)
    return hits == len(role) or hits / len(role) >= 0.6


# ---------------------------------------------------------------------------- waiting roles
def waiting_clause():
    """SQL for "waiting for a description": a role Tier 2 parked because its posting had no body
    (D-SRC-18), still active. The one definition every extension route uses; is_waiting is its
    Python twin."""
    return and_(
        Opportunity.stage == "t1_pass",
        Opportunity.stage_reason == EMPTY_POSTING_STAGE_REASON,
        Opportunity.archived_at.is_(None),
    )


def is_waiting(opp: Any) -> bool:
    return (
        getattr(opp, "stage", None) == "t1_pass"
        and getattr(opp, "stage_reason", None) == EMPTY_POSTING_STAGE_REASON
        and not getattr(opp, "archived_at", None)
    )


def host_of(url: str | None) -> str:
    """The host of a role's posting URL, lowercased, a leading www. dropped; "" when unusable."""
    try:
        host = (urlsplit(url or "").hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def same_site(a: str, b: str) -> bool:
    """The one rule for "from this site" (D-EXT-774): the two hosts are equal once lowercased and
    a leading www. and trailing dot are dropped (host_of). uk.indeed.com is not indeed.com, and a
    role on greenhouse.io is not from a company's boards.greenhouse.io page. The extension's
    toolbar count applies the same equality to the hosts `config` lists (background.js)."""
    return bool(a) and a == b


_LABELS = {
    "indeed": "Indeed", "greenhouse": "Greenhouse", "lever": "Lever", "ashbyhq": "Ashby",
    "myworkdayjobs": "Workday", "smartrecruiters": "SmartRecruiters", "glassdoor": "Glassdoor",
}


def source_label(host: str) -> str:
    for part in reversed(host.split(".")):
        if part in _LABELS:
            return _LABELS[part]
    return host


# ---------------------------------------------------------------------------- switches
async def features(session: AsyncSession) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for name in FEATURES:
        row = await session.get(Setting, FEATURE_PREFIX + name)
        out[name] = row.value if row is not None and isinstance(row.value, bool) else True
    return out


async def set_features(session: AsyncSession, changes: dict[str, bool]) -> dict[str, bool]:
    """Store the switches named in `changes` (only the three known names, only booleans: the
    caller validated the body). The caller commits."""
    for name, value in changes.items():
        if name not in FEATURES or not isinstance(value, bool):
            continue
        row = await session.get(Setting, FEATURE_PREFIX + name)
        if row is None:
            session.add(Setting(key=FEATURE_PREFIX + name, value=value))
        else:
            row.value = value
    await session.flush()
    return await features(session)


# ---------------------------------------------------------------------------- last seen
def browser_family(user_agent: str | None) -> str:
    """The browser family only: the full User-Agent is never stored (scope 6.1 item 9). Edge's
    string also names Chrome and Chrome's names Safari, so the order matters."""
    ua = user_agent or ""
    if "Edg/" in ua:
        return "Edge"
    if "Firefox/" in ua:
        return "Firefox"
    if "Chrome/" in ua or "Chromium/" in ua:
        return "Chrome"
    if "Safari/" in ua:
        return "Safari"
    return "Other"


_last_write: dict[str, float] = {"at": 0.0}


def _now() -> float:
    """This module's clock (tests patch it here, never the global one; D-TEST-2)."""
    return datetime.now(UTC).timestamp()


async def note_seen(session: AsyncSession, user_agent: str | None) -> bool:
    """Record when the extension last called and from which browser family, at most once a
    minute. Returns True when it wrote. The caller commits, then calls seen_saved(): the minute
    is counted from a note that was saved, so a commit that failed (a busy database) never holds
    back the next call's note (issue #265)."""
    if _now() - _last_write["at"] < LAST_SEEN_EVERY_S:
        return False
    value = {"at": utcnow_iso(), "browser": browser_family(user_agent)}
    row = await session.get(Setting, LAST_SEEN_KEY)
    if row is None:
        session.add(Setting(key=LAST_SEEN_KEY, value=value))
    else:
        row.value = value
    return True


def seen_saved() -> None:
    """The note note_seen wrote is committed: start the once-a-minute limit from now."""
    _last_write["at"] = _now()


def reset_seen_throttle() -> None:
    """Tests: forget the in-process throttle so each test sets its own precondition."""
    _last_write["at"] = 0.0


async def last_seen(session: AsyncSession) -> dict[str, str] | None:
    row = await session.get(Setting, LAST_SEEN_KEY)
    value = row.value if row is not None else None
    if not isinstance(value, dict):
        return None
    at, browser = value.get("at"), value.get("browser")
    if not isinstance(at, str) or not isinstance(browser, str):
        return None
    return {"at": at, "browser": browser}
