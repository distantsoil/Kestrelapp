"""What the quick round shows (issue 171, D-UI-272): the card, the done screen and the Today
strip's words. Read-only; every write is in routes_quick.py. Nothing here calls a model, and
Kestrel's score is never read: a card carries only what the posting says."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Settings
from kestrel.learning import quick
from kestrel.learning.active import UncertainItem
from kestrel.privacy import EmployerMap
from kestrel.web import calibrate_view as cv
from kestrel.web.pipeline_view import posted_words
from kestrel.web.redact import confidential_once, redact_name, redact_text

_STYLE = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}


@dataclass
class QuickCard:
    opp_id: int
    org: str
    initials: str
    mono: int  # 0 to 3: which tinted badge (cal-mono--N)
    title: str
    where: str
    style: str
    pay: str  # "Pay not listed" when there is none
    pay_listed: bool
    posted: str
    url: str  # an http(s) link or "" (Open posting is then left out)


def initials(name: str, fallback: str = "?") -> str:
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name or "") if w]
    if not words:
        return fallback.upper()[:1] or "?"
    return "".join(w[0] for w in words[:2]).upper()


def safe_url(url: str | None) -> str:
    """Open posting opens the original link in the browser and nothing else: only http(s)."""
    u = (url or "").strip()
    return u if u.lower().startswith(("http://", "https://")) else ""


async def build_card(
    s: AsyncSession,
    item: UncertainItem,
    *,
    conf: tuple[bool, EmployerMap] | None = None,
) -> QuickCard:
    """One card. In Confidential mode the current employer is shown as its "Show it as" text in
    the organization line, the title and the place, like every other list view."""
    opp = item.opportunity
    enabled, mapping = conf if conf is not None else await confidential_once(s)
    org = redact_name(await cv.org_name(s, opp), mapping, enabled=enabled) or ""
    where = opp.location_raw or ", ".join(p for p in (opp.city, opp.state) if p)
    where = redact_text(where, mapping, enabled=enabled) or ""
    pay = cv.pay_line(opp)
    return QuickCard(
        opp_id=opp.id,
        org=org,
        initials=initials(org, opp.title or "?"),
        mono=len(org or "") % 4,
        title=redact_text(opp.title, mapping, enabled=enabled) or "(untitled)",
        where=where or "Place not listed",
        style=_STYLE.get(opp.remote_type or "", "Work style not listed"),
        pay=pay,
        pay_listed=pay != "Pay not listed",
        posted=posted_words(opp.date_posted or opp.first_seen_at),
        url=safe_url(opp.url),
    )


async def build_cards(s: AsyncSession, items: list[UncertainItem]) -> list[QuickCard]:
    conf = await confidential_once(s)
    return [await build_card(s, it, conf=conf) for it in items]


# ----------------------------------------------------------------------------- the done screen
@dataclass
class Unlock:
    title: str
    detail: str
    at: int
    state: str  # "hit" | "next" | "later"
    left: int


def _clamp(value: int) -> int:
    return max(0, min(value, 1000))


@dataclass
class Done:
    total: int
    yes: int  # every yes, the top picks included
    top: int
    no: int
    unsure: int
    line: str
    unlocks: list[Unlock]
    remaining: int  # roles still available for another round (capped at one round)
    meter: quick.Meter


def done_line(total: int, settings: Settings, examples: int | None = -1) -> str:
    """D-UI-273: only once the few-shot pool (`quick.examples_count`, None when examples are
    off) reaches 15 does the screen say Kestrel re-ranks with them. -1 counts it by `total`."""
    shots = total if examples == -1 else examples
    if shots is None:
        return "Your answers are saved. Change any answer later in Calibrate, under Detailed."
    if shots >= settings.learning.min_labels_for_few_shot:
        return (
            "Kestrel re-ranks your pipeline with these on the next run. Change any answer "
            "later in Calibrate, under Detailed."
        )
    return (
        f"Your answers are saved. At {settings.learning.min_labels_for_few_shot}, Kestrel starts "
        "ranking new roles with them. Change any answer later in Calibrate, under Detailed."
    )


def build_done(
    *, total: int, yes: int, top: int, no: int, unsure: int, remaining: int, settings: Settings,
    examples: int | None = -1,
) -> Done:
    """`examples` is `quick.examples_count` (-1: counted by `total`): the few-shot unlock is
    reached by that pool and is left out when examples are off."""
    lr = settings.learning
    shots = total if examples == -1 else examples
    rows = [
        *((("Ranks new roles using your examples", "", lr.min_labels_for_few_shot, shots),)
          if shots is not None else ()),
        ("Suggests a scoring tune-up", "You approve any change before it applies.",
         lr.min_labels_for_calibration, total),
        ("Learns your ranking", "A sorting model built only from your answers.",
         lr.min_labels_for_reranker, total),
    ]
    unlocks: list[Unlock] = []
    found_next = False
    for title, detail, at, have in rows:
        if have >= at:
            unlocks.append(Unlock(title, detail, at, "hit", 0))
        elif not found_next:
            found_next = True
            unlocks.append(Unlock(title, detail, at, "next", at - have))
        else:
            unlocks.append(Unlock(title, detail, at, "later", at - have))
    top, yes = _clamp(top), _clamp(yes)
    return Done(
        total=total, yes=yes + top, top=top, no=_clamp(no), unsure=_clamp(unsure),
        line=done_line(total, settings, examples), unlocks=unlocks, remaining=remaining,
        meter=quick.meter_for(total, settings, examples),
    )
