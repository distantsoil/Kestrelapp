"""The Analytics page's numbers for one track and one date range (D-UI-101, SPEC-INTERFACES 2.1).

One range for the whole page: 30 days, 90 days or all time. Everything is counted from one shared
cohort so the tiles, the bar, the funnel and the tables agree with each other:

  * Roles (found, passed the rules, quick check, fully read): opportunities first seen in range.
  * Pursuits (shortlisted onward): pursuits dated in range. A pursuit's date is when it was sent
    (`applied_at`), else when it was first shortlisted (its first status event), else its last
    activity, else when the role was first seen. "Applications sent in this period, and everything
    that has happened to them since."

Every figure travels with the count it rests on, and `few()` is the single place that decides a
base is too small to show a rate (hard rule 6: under five is labeled, never rendered as solid).
Pure aggregation over the operator's own data; no model call, no network."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.analytics.funnel import _STAGE_RANK, _furthest_step
from kestrel.db.models import (
    Evaluation,
    Opportunity,
    Org,
    Outcome,
    Pursuit,
    Source,
    StatusEvent,
)
from kestrel.rules.helpers import seniority_rank

MIN_N = 5  # hard rule 6: a rate over fewer than five is labeled, not shown
QUIET_DAYS = 14  # "No reply after 14 days"
RANGES: dict[str, int | None] = {"30": 30, "90": 90, "all": None}
DEFAULT_RANGE = "90"
_WEEKS_SHOWN = 26  # the pace chart never grows past half a year of bars

# Funnel indices (kestrel.analytics.funnel.STEPS): 4 shortlisted, 5 sent, 6 first reply,
# 7 screening/scoping, 8 interview, 9 final/proposal, 10 offer/won.
_SENT, _REPLY = 5, 6

_WORDS: dict[str, dict[str, Any]] = {
    "employment": {
        "noun": "role", "nouns": "roles", "sent_noun": "application", "sent_nouns": "applications",
        "range_noun": "applications",
        "tiles": ("Roles found", "Shortlisted", "Applied", "Replies", "Interviews"),
        "interview_step": 8,
        "standing": (
            ("offer", "Offer"), ("iv", "Interviewing"), ("wait", "Waiting for a reply"),
            ("rej", "Turned down"), ("ghost", "No reply after 14 days"), ("wd", "You withdrew"),
        ),
        "standing_title": "Where your applications stand",
        "sent_verb": "applied", "short_verb": "shortlisted", "not_sent": "not applied to yet",
        "funnel_b": (
            ("Shortlisted", 4), ("Applied", 5), ("First reply", 6), ("Screening", 7),
            ("Interview", 8), ("Final round", 9), ("Offer", 10),
        ),
        "band_cols": ("Applied", "Got a reply", "Got an interview"),
        "band_step": 8,
        "breakdown_title": "Where roles come from",
        "breakdown_cols": ("Found", "Shortlisted", "Applied"),
        "pace_title": "Applications sent each week",
    },
    "consulting": {
        "noun": "lead", "nouns": "leads", "sent_noun": "lead contacted",
        "sent_nouns": "leads contacted", "range_noun": "leads contacted",
        "tiles": ("Leads found", "Added to leads", "Reached out", "Replies", "Proposals"),
        "interview_step": 9,
        "standing": (
            ("offer", "Won"), ("iv", "In conversation"), ("wait", "Waiting for a reply"),
            ("rej", "Lost"), ("ghost", "No reply after 14 days"), ("wd", "You stepped back"),
        ),
        "standing_title": "Where your leads stand",
        "sent_verb": "reached out", "short_verb": "added to leads", "not_sent": "not contacted yet",
        "funnel_b": (
            ("Added to leads", 4), ("Reached out", 5), ("They replied", 6), ("Scoping", 7),
            ("Proposal sent", 9), ("Won", 10),
        ),
        "band_cols": ("Reached out", "Got a reply", "Got to scoping"),
        "band_step": 7,
        "breakdown_title": "Where leads come from",
        "breakdown_cols": ("Found", "Added", "Reached out"),
        "pace_title": "Leads contacted each week",
    },
}

_OFFER = {"employment": {"offer", "accepted"}, "consulting": {"won"}}
_IN_TALKS = {
    "employment": {"screening", "interviewing", "final"},
    "consulting": {"conversation", "scoping", "proposal_sent", "negotiating"},
}
_TURNED_DOWN = {"employment": {"rejected"}, "consulting": {"lost"}}
_WITHDREW = {"employment": {"withdrawn"}, "consulting": set()}
_GHOSTED = {"employment": {"ghosted"}, "consulting": {"no_response"}}

_LEVELS = (  # (highest seniority rank, label)
    (0, "Individual contributor"), (1, "Lead"), (3, "Manager"), (5, "Director"),
    (7, "VP"), (99, "Executive"),
)
_BANDS = (  # (floor, label); a score lands in the first band whose floor it reaches
    (4.5, "4.5 to 5.0"), (4.0, "4.0 to 4.4"), (3.5, "3.5 to 3.9"), (0.0, "Under 3.5"),
)
NOT_STATED = "Not stated"
_ZERO_ROW = {"found": 0, "short": 0, "sent": 0, "replied": 0}


def words(track: str) -> dict[str, Any]:
    return _WORDS.get(track, _WORDS["employment"])


def few(n: int) -> bool:
    """True when a base of n is too small to show a rate (hard rule 6)."""
    return n < MIN_N


def pct(part: int, whole: int) -> int | None:
    return round(part / whole * 100) if whole else None


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def range_label(range_key: str) -> str:
    days = RANGES.get(range_key)
    return "all time" if days is None else f"last {days} days"


def normalize_range(value: str | None) -> str:
    return value if value in RANGES else DEFAULT_RANGE


@dataclass
class _Window:
    start: datetime | None
    end: datetime

    def has(self, when: datetime | None) -> bool:
        if when is None:
            return False
        return (self.start is None or when >= self.start) and when < self.end


@dataclass
class _P:
    """One pursuit with everything the page needs to place it."""
    id: int
    opp: Opportunity
    org: Org | None
    src: Source | None
    status: str
    when: datetime  # the date that places it in a range (see the module docstring)
    sent_at: datetime | None
    shortlisted_at: datetime | None
    reached: int  # furthest funnel step it proves (kestrel.analytics.funnel)
    replied: bool
    reply_days: int | None
    score: float | None

    @property
    def sent(self) -> bool:
        return self.reached >= _SENT


@dataclass
class _Data:
    track: str
    now: datetime
    opps: list[tuple[Opportunity, Org | None, Source | None]]
    read_ids: set[int]
    pursuits: list[_P]


async def _load(session: AsyncSession, track: str, now: datetime) -> _Data:
    opps = [
        (o, org, src)
        for o, org, src in (
            await session.execute(
                select(Opportunity, Org, Source)
                .join(Org, Org.id == Opportunity.org_id, isouter=True)
                .join(Source, Source.id == Opportunity.source_id, isouter=True)
                .where(Opportunity.track == track)
            )
        ).all()
    ]
    read_ids = set(
        (
            await session.execute(
                select(Evaluation.opportunity_id)
                .where(Evaluation.tier == 2, Evaluation.track == track)
                .distinct()
            )
        ).scalars()
    )
    scores: dict[int, float] = {}
    for opp_id, score in (
        await session.execute(
            select(Evaluation.opportunity_id, Evaluation.score)
            .where(Evaluation.tier == 2, Evaluation.track == track)
            .order_by(Evaluation.id)
        )
    ).all():
        if score is not None:
            scores[opp_id] = score  # the latest evaluation wins
    first_event = {
        pid: _dt(at)
        for pid, at in (
            await session.execute(
                select(StatusEvent.pursuit_id, func.min(StatusEvent.occurred_at)).group_by(
                    StatusEvent.pursuit_id
                )
            )
        ).all()
    }
    opp_by_id = {o.id: (o, org, src) for o, org, src in opps}
    outcomes: dict[int, Outcome] = {}
    for oc in (await session.execute(select(Outcome).order_by(Outcome.id))).scalars():
        outcomes[oc.pursuit_id] = oc  # the latest outcome per pursuit
    pursuits: list[_P] = []
    for p in (await session.execute(select(Pursuit).where(Pursuit.track == track))).scalars():
        found = opp_by_id.get(p.opportunity_id)
        if found is None:
            continue
        opp, org, src = found
        oc = outcomes.get(p.id)
        responded = oc is not None and oc.days_to_first_response is not None
        reached = _furthest_step(p.status, oc.furthest_stage if oc else None, responded, track)
        sent_at = _dt(p.applied_at)
        shortlisted_at = first_event.get(p.id)
        when = (
            sent_at or shortlisted_at or _dt(p.last_activity_at) or _dt(opp.first_seen_at) or now
        )
        pursuits.append(
            _P(
                id=p.id, opp=opp, org=org, src=src, status=p.status, when=when, sent_at=sent_at,
                shortlisted_at=shortlisted_at, reached=reached, replied=reached >= _REPLY,
                reply_days=oc.days_to_first_response if oc else None,
                score=scores.get(opp.id),
            )
        )
    return _Data(track=track, now=now, opps=opps, read_ids=read_ids, pursuits=pursuits)


# ------------------------------------------------------------------------------------ counting
def _roles_in(data: _Data, win: _Window) -> list[tuple[Opportunity, Org | None, Source | None]]:
    return [t for t in data.opps if win.has(_dt(t[0].first_seen_at))]


def _role_steps(data: _Data, roles) -> list[int]:
    ranks = [_STAGE_RANK.get(o.stage or "new", 0) for o, _org, _src in roles]
    return [
        len(roles),
        sum(1 for r in ranks if r >= 1),
        sum(1 for r in ranks if r >= 2),
        sum(1 for o, _org, _src in roles if o.id in data.read_ids),
    ]


def _cohort(data: _Data, win: _Window) -> list[_P]:
    return [p for p in data.pursuits if win.has(p.when)]


def _tile_counts(data: _Data, win: _Window) -> list[int]:
    w = words(data.track)
    roles = _roles_in(data, win)
    cohort = _cohort(data, win)
    sent = [p for p in cohort if p.sent]
    return [
        len(roles),
        len(cohort),
        len(sent),
        sum(1 for p in sent if p.replied),
        sum(1 for p in sent if p.reached >= w["interview_step"]),
    ]


def _standing_key(p: _P, track: str, now: datetime) -> str:
    s = p.status
    if s in _OFFER[track]:
        return "offer"
    if s in _IN_TALKS[track]:
        return "iv"
    if s in _TURNED_DOWN[track]:
        return "rej"
    if s in _WITHDREW[track]:
        return "wd"
    if s in _GHOSTED[track]:
        return "ghost"
    if p.replied:
        return "iv"  # an answer came back and the status has not moved on yet
    anchor = p.sent_at or p.when
    return "ghost" if (now - anchor).days > QUIET_DAYS else "wait"


def _level(title: str | None) -> str:
    rank = seniority_rank(title)
    if rank < 0:
        return NOT_STATED
    return next(label for top, label in _LEVELS if rank <= top)


def _clean(value: Any) -> str:
    text = str(value).strip() if value is not None else ""
    return NOT_STATED if text.lower() in ("", "unknown", "none") else text.replace("_", " ")


_BREAKDOWNS = (  # (key, tab label, how a role is grouped)
    ("src", "Source", lambda o, org, src: _clean(src.name if src else None)),
    ("work", "Work type", lambda o, org, src: _clean(o.employment_type)),
    ("level", "Level", lambda o, org, src: _level(o.title)),
    ("size", "Company size", lambda o, org, src: _clean(org.size_band if org else None)),
    ("own", "Ownership", lambda o, org, src: _clean(org.ownership if org else None)),
    ("remote", "Remote type", lambda o, org, src: _clean(o.remote_type)),
    (
        "trig", "Trigger type",
        lambda o, org, src: _clean(
            (o.trigger_data or {}).get("event_type") if isinstance(o.trigger_data, dict) else None
        ),
    ),
)


def breakdown_tabs(track: str) -> list[tuple[str, str]]:
    return [(k, label) for k, label, _fn in _BREAKDOWNS if k != "trig" or track == "consulting"]


# ------------------------------------------------------------------------------------- report
async def build_report(
    session: AsyncSession, track: str, range_key: str = DEFAULT_RANGE, *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Everything the page draws, as plain dicts. `n` / `base` always sit beside a rate."""
    track = track if track in _WORDS else "employment"
    range_key = normalize_range(range_key)
    now = now or datetime.now(UTC)
    w = words(track)
    days = RANGES[range_key]
    win = _Window(now - timedelta(days=days) if days else None, now + timedelta(seconds=1))
    prev = (
        _Window(win.start - timedelta(days=days), win.start) if days and win.start else None
    )
    data = await _load(session, track, now)

    roles = _roles_in(data, win)
    role_steps = _role_steps(data, roles)
    cohort = _cohort(data, win)
    sent = [p for p in cohort if p.sent]
    counts = _tile_counts(data, win)
    prev_counts = _tile_counts(data, prev) if prev else None

    # --- tiles: a tile whose base is under five is dashed (hard rule 6)
    bases = [None, role_steps[3], counts[1], counts[2], counts[2]]
    subs = [
        f"{role_steps[3]:,} fully read",
        _of(counts[1], role_steps[3], "of fully read"),
        _of(counts[2], counts[1], "of " + w["short_verb"]),
        _of(counts[3], counts[2], "of " + w["sent_verb"]),
        _of(counts[4], counts[2], "of " + w["sent_verb"]),
    ]
    tiles = []
    for i, label in enumerate(w["tiles"]):
        base = bases[i]
        tiles.append({
            "label": label, "n": counts[i], "base": base,
            "few": base is not None and few(base), "sub": subs[i],
            "prev": prev_counts[i] if prev_counts else None,
            "help": "A reply is any answer from a person, including a no. Automatic "
                    "acknowledgments do not count." if i == 3 else "",
        })

    # --- where they stand: adds up to everything sent in range
    keys = {k: 0 for k, _l in w["standing"]}
    for p in sent:
        keys[_standing_key(p, track, now)] += 1
    standing = {
        "title": w["standing_title"], "total": len(sent), "few": few(len(sent)),
        "sent_verb": w["sent_verb"], "short_n": len(cohort),
        "not_sent": max(0, len(cohort) - len(sent)), "short_verb": w["short_verb"],
        "not_sent_label": w["not_sent"],
        "segments": [
            {"key": k, "label": label, "n": keys[k], "share": pct(keys[k], len(sent))}
            for k, label in w["standing"]
        ],
    }

    # --- the funnel, two groups with their own scales
    a_labels = ("Found", "Passed your rules", "Passed quick check", "Fully read")
    group_a = _steps(list(zip(a_labels, role_steps, strict=True)))
    b_counts = [(label, sum(1 for p in cohort if p.reached >= step)) for label, step in
                w["funnel_b"]]
    group_b = _steps(b_counts)

    # --- replies by fit score (sent cohort only; a table, because stages overlap)
    band_rows = []
    for floor, label in _BANDS:
        in_band = [
            p for p in sent if p.score is not None
            and (p.score >= floor) and not any(p.score >= f for f, _l in _BANDS if f > floor)
        ]
        n = len(in_band)
        if not n:
            continue
        replied = sum(1 for p in in_band if p.replied)
        deep = sum(1 for p in in_band if p.reached >= w["band_step"])
        band_rows.append({
            "label": label, "n": n,
            "reply": _cell(replied, n), "deep": _cell(deep, n),
        })
    scored = sum(r["n"] for r in band_rows)
    comparable = len(band_rows) >= 2 and all(not few(r["n"]) for r in band_rows)
    bands = {
        "rows": band_rows, "cols": w["band_cols"], "scored": scored,
        "unscored": len(sent) - scored, "comparable": comparable, "sent": len(sent),
    }

    # --- where roles come from, one tab per grouping
    tabs: dict[str, dict[str, Any]] = {}
    for key, label, fn in _BREAKDOWNS:
        if key == "trig" and track != "consulting":
            continue
        rows: dict[str, dict[str, int]] = {}

        for o, org, src in roles:
            rows.setdefault(fn(o, org, src), dict(_ZERO_ROW))["found"] += 1
        for p in cohort:
            r = rows.setdefault(fn(p.opp, p.org, p.src), dict(_ZERO_ROW))
            r["short"] += 1
            if p.sent:
                r["sent"] += 1
                r["replied"] += int(p.replied)
        out = []
        for name, r in sorted(rows.items(), key=lambda kv: (-kv[1]["found"], kv[0])):
            out.append({"name": name, **r, "few": few(r["sent"]),
                        "rate": pct(r["replied"], r["sent"])})
        tabs[key] = {"label": label, "rows": out, "any_few": any(r["few"] for r in out)}

    # --- reply timing
    reply_days = [p.reply_days for p in sent if p.replied and p.reply_days is not None]
    waiting = [
        p for p in sent
        if not p.replied and _standing_key(p, track, now) in ("wait", "ghost")
    ]

    def age(p: _P) -> int:
        return max(0, (now - (p.sent_at or p.when)).days)

    ages = [age(p) for p in waiting]
    oldest = max(waiting, key=age, default=None)
    timing = {
        "median": round(median(reply_days)) if len(reply_days) >= MIN_N else None,
        "n": len(reply_days),
        "waiting": {
            "lt7": sum(1 for a in ages if a < 7),
            "7to14": sum(1 for a in ages if 7 <= a <= QUIET_DAYS),
            "gt14": sum(1 for a in ages if a > QUIET_DAYS),
            "total": len(ages),
        },
        "oldest": (
            {"title": oldest.opp.title or "Untitled", "org": oldest.opp.org_name or "",
             "days": age(oldest), "pursuit_id": oldest.id, "opportunity_id": oldest.opp.id}
            if oldest else None
        ),
    }

    # --- pace: per-week bars, and how long shortlist to sent takes
    pace = _pace(sent, win, now)
    lag = [
        (p.sent_at - p.shortlisted_at).days for p in sent
        if p.sent_at and p.shortlisted_at and p.sent_at >= p.shortlisted_at
    ]
    pace["to_sent_median"] = round(median(lag)) if len(lag) >= MIN_N else None
    pace["to_sent_n"] = len(lag)

    return {
        "track": track, "range": range_key, "range_label": range_label(range_key),
        "words": w, "tiles": tiles, "standing": standing,
        "funnel": {"a": group_a, "b": group_b},
        "bands": bands, "tabs": tabs, "timing": timing, "pace": pace,
        "counts": {"found": role_steps[0], "read": role_steps[3], "short": len(cohort),
                   "sent": len(sent)},
        "quiet": {"n": sum(1 for p in waiting if age(p) > QUIET_DAYS)},
        "generated_at": now.isoformat(),
    }


def _of(part: int, whole: int, tail: str) -> str:
    share = pct(part, whole)
    return f"{share}% {tail}" if share is not None and not few(whole) else f"{part:,} so far"


def _cell(part: int, base: int) -> dict[str, Any]:
    return {"n": part, "base": base, "few": few(base), "pct": pct(part, base)}


def _steps(pairs: list[tuple[str, int]]) -> list[dict[str, Any]]:
    """Funnel rows: each carries the count of the step before as its base. A step whose base is
    under five is hatched and says "too few" instead of a percent."""
    top = max((n for _l, n in pairs), default=0)
    out = []
    for i, (label, n) in enumerate(pairs):
        base = pairs[i - 1][1] if i else None
        out.append({
            "label": label, "n": n, "base": base,
            "pct": pct(n, base) if base is not None else None,
            "few": base is not None and few(base),
            "width": round(max(n / top * 100, 1.5 if n else 0), 1) if top else 0,
            "prev_label": pairs[i - 1][0] if i else "",
        })
    return out


def _pace(sent: list[_P], win: _Window, now: datetime) -> dict[str, Any]:
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0,
                                                           microsecond=0)
    dated = [p.sent_at or p.when for p in sent]
    first = win.start or (min(dated) if dated else now)
    weeks = max(1, min(_WEEKS_SHOWN, (monday - (first - timedelta(days=first.weekday()))).days // 7
                       + 1))
    starts = [monday - timedelta(weeks=weeks - 1 - i) for i in range(weeks)]
    from kestrel.date_display import format_display

    bars = [{"start": s, "label": format_display(s, "short"), "n": 0} for s in starts]
    for d in dated:
        for b in reversed(bars):
            if d >= b["start"]:
                b["n"] += 1
                break
    top = max((b["n"] for b in bars), default=0)
    for b in bars:
        b["height"] = round(b["n"] / top * 100) if top else 0
    return {"bars": bars, "total": sum(b["n"] for b in bars), "few": few(len(dated))}


# ----------------------------------------------------------------------------- plain summary
def plain_summary(report: dict[str, Any], svo: dict[str, Any]) -> dict[str, str]:
    """Working, Not working and Worth changing, built in code from the report's own counts so the
    page always has an honest answer without a model. A model-written summary adds to this, never
    replaces it (hard rule 6). Every claim carries its count."""
    w = report["words"]
    c = report["counts"]
    sent = c["sent"]
    nouns, sent_nouns = w["nouns"], w["sent_nouns"]
    if c["found"] == 0 and c["short"] == 0:
        working = f"Kestrel has not found any {nouns} in this period yet."
    else:
        working = (
            f"Kestrel found {c['found']:,} {nouns} and fully read {c['read']:,}, and you "
            f"{w['short_verb']} {c['short']:,}."
        )
    n_out = svo.get("n_outcomes", 0)
    if few(sent):
        not_working = (
            f"Too early to say anything else. There {'is' if sent == 1 else 'are'} "
            f"{'only ' if sent else ''}{sent} {w['sent_noun'] if sent == 1 else sent_nouns} in "
            f"this period, fewer than {MIN_N}, so this page has no reliable numbers yet."
        )
    elif svo.get("discriminating") is False:
        not_working = (
            f"The score is not telling good {nouns} from weak ones yet: replies came back at "
            f"about the same rate across score ranges (from {n_out} recorded outcomes)."
        )
    elif not report["bands"]["comparable"]:
        not_working = (
            f"Too early to compare score ranges or sources. Each group needs at least {MIN_N} "
            f"{sent_nouns}, and {sent} in total is spread thin."
        )
    else:
        not_working = (
            f"Nothing stands out as broken in {sent} {sent_nouns}. Groups under {MIN_N} are "
            "not compared."
        )
    quiet = report["quiet"]["n"]
    if quiet:
        worth = (
            f"{quiet} {w['sent_noun'] if quiet == 1 else sent_nouns} "
            f"{'has' if quiet == 1 else 'have'} had no reply for over {QUIET_DAYS} days. Follow "
            "up, or mark them closed."
        )
    elif few(sent):
        like = "Add the leads you like" if report["track"] == "consulting" else (
            "Shortlist the roles you like")
        worth = (
            f"{like}. "
            f"The numbers start to mean something after about {MIN_N} {sent_nouns}."
        )
    else:
        worth = "No single change stands out yet. Check back after more replies come in."
    return {"working": working, "not_working": not_working, "worth": worth,
            "quiet_link": "1" if quiet else ""}


def period_facts(report: dict[str, Any]) -> dict[str, Any]:
    """The range's own numbers for the P12 call, so the narrative names the same period and the
    same counts the page shows (every figure with its base)."""
    return {
        "track": report["track"], "range": report["range_label"], "counts": report["counts"],
        "standing": {s["label"]: s["n"] for s in report["standing"]["segments"]},
        "tiles": [{"label": t["label"], "n": t["n"], "base": t["base"]} for t in report["tiles"]],
        "reply_timing": {"median_days": report["timing"]["median"], "n": report["timing"]["n"]},
        "minimum_base_for_a_rate": MIN_N,
    }
