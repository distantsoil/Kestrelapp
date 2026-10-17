"""The Applications / Leads board (D-UI-46): four columns per track over the fine pursuit statuses,
the verb buttons that move a card, and the pure helpers the page and its tests share.

The fine status is still the stored truth (SPEC.md 4.5); a column is only a grouping of statuses.
Every move goes through `set_pursuit_status`, so a status change always writes a `status_event`.
Nothing here contacts anyone: a verb records what the operator already did."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Pursuit
from kestrel.services.pursuits import set_pursuit_status
from kestrel.web.words import CONSULTING, EMPLOYMENT

FOLLOW_UP_DAYS = 7


@dataclass(frozen=True)
class BoardColumn:
    key: str
    label: str
    statuses: tuple[str, ...]
    empty: str

    @property
    def first(self) -> str:
        """The status a dragged card takes when it lands in this column."""
        return self.statuses[0]


COLUMNS: dict[str, tuple[BoardColumn, ...]] = {
    "employment": (
        BoardColumn("to_apply", EMPLOYMENT["first_column"], ("shortlisted", "preparing"),
                    "Roles you add to your list wait here until you apply."),
        BoardColumn("applied", EMPLOYMENT["applied_done"], ("applied", "screening"),
                    "Roles you have applied for, with a follow-up date."),
        BoardColumn("interviewing", "Interviewing", ("interviewing", "final"),
                    "Roles with an interview on the calendar."),
        BoardColumn("closed", "Offer / Closed",
                    ("offer", "accepted", "rejected", "withdrawn", "ghosted"),
                    "Offers, rejections and withdrawals land here with the outcome recorded."),
    ),
    "consulting": (
        BoardColumn("to_contact", CONSULTING["first_column"], ("shortlisted", "researching"),
                    "Leads you add to your list wait here until you reach out."),
        BoardColumn("conversation", "In conversation", ("outreach_sent", "conversation"),
                    "Leads you have contacted, with a follow-up date."),
        BoardColumn("proposal", "Proposal", ("scoping", "proposal_sent", "negotiating"),
                    "Leads with a scope or proposal in play."),
        BoardColumn("closed", "Won / Closed", ("won", "lost", "no_response"),
                    "Won, lost and unanswered leads land here with the outcome recorded."),
    ),
}

# Statuses that end a pursuit: the card shows outcome capture instead of verbs.
CLOSED_STATUSES: dict[str, tuple[str, ...]] = {
    "employment": ("accepted", "rejected", "withdrawn", "ghosted"),
    "consulting": ("won", "lost", "no_response"),
}
# Statuses where a card waits on a reply: the Applied / In conversation column. Only here does a
# passed follow-up date mean silence ("N days quiet", an overdue follow-up on Today). In the
# interviewing or proposal columns a passed date is an event that happened, not silence.
# proposal_sent is included for consulting: its "Follow up on proposal" date is a follow-up too.
WAITING_STATUSES: dict[str, tuple[str, ...]] = {
    "employment": ("applied", "screening"),
    "consulting": ("outreach_sent", "conversation", "proposal_sent"),
}
# Statuses whose next_action_at is an interview date; once it passes, the card asks for the
# outcome instead of counting quiet days.
INTERVIEW_STATUSES: dict[str, tuple[str, ...]] = {
    "employment": ("interviewing", "final"),
    "consulting": (),
}
# The statuses the outcome form offers per track (existing outcome fields, D-UI-46).
OUTCOME_CHOICES: dict[str, tuple[str, ...]] = {
    "employment": ("accepted", "rejected", "withdrawn"),
    "consulting": ("won", "lost", "no_response"),
}


# How a stored status reads in this UI. The stored value never changes (`shortlisted`); only the
# words do (D-UI-51). Anything not listed reads as the status with its underscores removed.
STAGE_NAMES: dict[str, str] = {"shortlisted": EMPLOYMENT["first_column"]}
# The consulting track names the same stored statuses in its own words (D-UI-54).
CONSULTING_STAGE_NAMES: dict[str, str] = {
    "shortlisted": CONSULTING["first_column"],
    "outreach_sent": CONSULTING["applied_done"],
}


def stage_name(status: str, track: str | None = None) -> str:
    if track == "consulting" and status in CONSULTING_STAGE_NAMES:
        return CONSULTING_STAGE_NAMES[status]
    return STAGE_NAMES.get(status) or status.replace("_", " ")


def done_label(label: str) -> str:
    """The green label a verb button shows once clicked: "Applied?" -> "\u2713 Applied",
    "Reached out?" -> "\u2713 Reached out"."""
    return "\u2713 " + label.rstrip("?").strip()


def columns_for(track: str) -> tuple[BoardColumn, ...]:
    return COLUMNS.get(track, COLUMNS["employment"])


def column_key(track: str, status: str) -> str | None:
    for col in columns_for(track):
        if status in col.statuses:
            return col.key
    return None


def column_by_key(track: str, key: str) -> BoardColumn | None:
    return next((c for c in columns_for(track) if c.key == key), None)


@dataclass(frozen=True)
class Verb:
    key: str
    to_status: str | None  # None: records a follow-up only, the status stays
    next_action: str | None
    clear_next: bool = False


# verb key -> spec, per track. A verb never sends anything; it records what the operator did.
VERBS: dict[str, dict[str, Verb]] = {
    "employment": {
        "applied": Verb("applied", "applied", "Follow up"),
        "heard_back": Verb("heard_back", "screening", None, clear_next=True),
        "follow_again": Verb("follow_again", None, "Follow up"),
        "interview": Verb("interview", "interviewing", "Interview"),
        "offer": Verb("offer", "offer", None, clear_next=True),
        "rejected": Verb("rejected", "rejected", None, clear_next=True),
        "withdrew": Verb("withdrew", "withdrawn", None, clear_next=True),
    },
    "consulting": {
        "applied": Verb("applied", "outreach_sent", "Follow up"),  # "Reached out"
        "heard_back": Verb("heard_back", "conversation", None, clear_next=True),  # "They replied"
        "follow_again": Verb("follow_again", None, "Follow up"),
        "proposal": Verb("proposal", "proposal_sent", "Follow up on proposal"),
        "offer": Verb("offer", "won", None, clear_next=True),  # "Won"
        "rejected": Verb("rejected", "lost", None, clear_next=True),  # "Lost"
    },
}

# What each verb is called on the button, per track.
VERB_LABELS: dict[str, dict[str, str]] = {
    "employment": {
        "applied": EMPLOYMENT["applied_q"], "heard_back": "Heard back",
        "follow_again": "No reply, follow up again", "interview": "Interview scheduled",
        "offer": "Offer received", "rejected": "Rejected", "withdrew": "Withdrew",
    },
    "consulting": {
        "applied": CONSULTING["applied_q"], "heard_back": "They replied",
        "follow_again": "No reply, follow up again", "proposal": "Proposal sent",
        "offer": "Won", "rejected": "Lost",
    },
}


def verbs_for_card(track: str, status: str) -> list[str]:
    """The verb keys shown on a card in `status`, in button order. The primary verb is first."""
    key = column_key(track, status)
    if track == "consulting":
        if key == "to_contact":
            return ["applied"]
        if key == "conversation":
            keys = ["heard_back"] if status == "outreach_sent" else []
            return [*keys, "proposal", "follow_again"]
        if key == "proposal":
            return ["offer", "rejected", "follow_again"]
        return []
    if key == "to_apply":
        return ["applied"]
    if key == "applied":
        keys = ["heard_back"] if status == "applied" else []
        return [*keys, "follow_again", "interview"]
    if key == "interviewing":
        return ["offer", "rejected", "withdrew"]
    return []


def today() -> date:
    return date.today()


def parse_day(raw: object) -> date | None:
    """A form date (YYYY-MM-DD), or None when blank or malformed."""
    try:
        return date.fromisoformat(str(raw or "").strip()[:10]) if str(raw or "").strip() else None
    except ValueError:
        return None


def day_stamp(d: date) -> str:
    return f"{d.isoformat()}T00:00:00+00:00"


def short_date(value: str | None) -> str:
    """'Oct 9' from an ISO date or timestamp, or '' when blank."""
    d = parse_day(value)
    return f"{d.strftime('%b')} {d.day}" if d else ""


def is_waiting(p: Pursuit) -> bool:
    """Whether the card waits on a reply (the Applied / In conversation column)."""
    return p.status in WAITING_STATUSES.get(p.track, ())


def interview_passed(p: Pursuit, on: date | None = None) -> date | None:
    """The interview date when an interviewing card's date has passed, else None: the card then
    asks for the outcome ("interview was Oct 9; record the outcome"), never "N days quiet"."""
    if p.status not in INTERVIEW_STATUSES.get(p.track, ()):
        return None
    when = parse_day(p.next_action_at)
    return when if when is not None and when < (on or today()) else None


def quiet_days(p: Pursuit, on: date | None = None) -> int | None:
    """Days of silence on a waiting card (Applied / In conversation) whose follow-up date has
    passed, else None. Counted from the application (or last activity), so bumping the
    follow-up date does not reset the silence. Any other column has no silence to count."""
    if not is_waiting(p) or p.closed_at:
        return None
    on = on or today()
    due = parse_day(p.next_action_at)
    if due is None or due >= on:
        return None
    anchor = parse_day(p.applied_at) or parse_day(p.last_activity_at) or due
    return max(1, (on - anchor).days)


def order_cards(pursuits: list[Pursuit], on: date | None = None) -> list[Pursuit]:
    """Overdue follow-ups first (most overdue first), then most recent activity first."""
    on = on or today()
    recent = sorted(pursuits, key=lambda p: (p.last_activity_at or "", p.id), reverse=True)

    def overdue_key(p: Pursuit) -> tuple[int, str]:
        due = parse_day(p.next_action_at)
        late = due is not None and due < on and (is_waiting(p) or interview_passed(p, on))
        return (0, due.isoformat()) if late else (1, "")

    return sorted(recent, key=overdue_key)  # stable: ties keep the recency order


async def move_to_status(
    session, p: Pursuit, to_status: str, *, note: str | None = None,
    occurred_at: str | None = None,
) -> str | None:
    """THE one way a card changes status (a verb, the stage select, a drag). Returns an error
    string for a status the track does not have, else None. Flushes; the caller commits.

    - Writes the status_event (set_pursuit_status).
    - Entering the track's applied status (applied; outreach_sent for consulting) stamps
      applied_at if unset and, when the card has no next action, the "Applied?" default: a
      follow-up FOLLOW_UP_DAYS out, so a dragged card gets the same quiet badge and Today line
      as one moved with the button.
    - Entering a closed status sets closed_at; leaving one clears it, so a reopened card is
      open again everywhere (Today, the quiet count)."""
    from kestrel.db.enums import valid_pursuit_status

    if not valid_pursuit_status(p.track, to_status):
        return f"'{to_status}' is not a stage for {p.track} cards; nothing was changed."
    if to_status == p.status:
        if note:
            p.notes_md = ((p.notes_md or "") + "\n\n" + note).strip()
        return None
    closed = CLOSED_STATUSES.get(p.track, ())
    was_closed = p.status in closed
    await set_pursuit_status(session, p.id, to_status, note=note, occurred_at=occurred_at)
    now = utcnow_iso()
    applied_status = VERBS.get(p.track, {}).get("applied")
    if applied_status is not None and to_status == applied_status.to_status:
        if not p.applied_at:
            p.applied_at = day_stamp(parse_day(occurred_at) or today())
        if not p.next_action and not p.next_action_at:
            start = parse_day(p.applied_at) or today()
            p.next_action = applied_status.next_action
            p.next_action_at = (start + timedelta(days=FOLLOW_UP_DAYS)).isoformat()
    if to_status in closed:
        p.closed_at = p.closed_at or now
    elif was_closed:
        p.closed_at = None
    p.last_activity_at = now
    await session.flush()
    return None


async def apply_verb(
    session, p: Pursuit, verb_key: str, form: dict[str, str]
) -> str | None:
    """Apply one verb to `p`. Returns an error string, or None on success. Flushes; the caller
    commits. Status changes go through set_pursuit_status so a status_event is always written."""
    verb = VERBS.get(p.track, {}).get(verb_key)
    if verb is None:
        return f"'{verb_key}' is not available for a {p.track} pursuit."
    now = utcnow_iso()
    note = (form.get("note") or "").strip() or None
    on = today()
    event_date: date | None = None
    if verb_key == "applied":
        event_date = parse_day(form.get("applied_on")) or on
        if form.get("applied_on") and parse_day(form.get("applied_on")) is None:
            return "That date is not a valid date."
        follow = parse_day(form.get("follow_up"))
        if form.get("follow_up") and follow is None:
            return "The follow-up date is not a valid date."
        follow = follow or (event_date + timedelta(days=FOLLOW_UP_DAYS))
        doc = str(form.get("document_id") or "").strip()
        if doc:
            from kestrel.db.models import DocRender

            render = await session.get(DocRender, int(doc)) if doc.isdigit() else None
            if render is None or (
                render.opportunity_id is not None and render.opportunity_id != p.opportunity_id
            ):
                return "That document does not belong to this role."
            p.document_id = render.id
        p.applied_at = day_stamp(event_date)
        p.next_action, p.next_action_at = verb.next_action, follow.isoformat()
    elif verb_key == "follow_again":
        p.next_action = verb.next_action
        p.next_action_at = (on + timedelta(days=FOLLOW_UP_DAYS)).isoformat()
        p.last_activity_at = now
    elif verb_key in ("interview", "proposal"):
        when = parse_day(form.get("when"))
        if verb_key == "interview":
            if when is None:
                return "Choose the interview date."
            clock = str(form.get("at_time") or "").strip()
            try:
                stamp = (
                    datetime.strptime(f"{when.isoformat()} {clock}", "%Y-%m-%d %H:%M").isoformat(
                        timespec="minutes"
                    )
                    if clock
                    else when.isoformat()
                )
            except ValueError:
                return "That time is not valid."
            p.next_action, p.next_action_at = verb.next_action, stamp
        else:
            follow = parse_day(form.get("follow_up")) or (on + timedelta(days=FOLLOW_UP_DAYS))
            p.next_action, p.next_action_at = verb.next_action, follow.isoformat()
    elif verb.clear_next:
        p.next_action, p.next_action_at = None, None
    if verb.to_status is not None:
        err = await move_to_status(
            session, p, verb.to_status, note=note,
            occurred_at=day_stamp(event_date) if event_date and verb_key == "applied" else None,
        )
        if err:
            return err
    elif note:
        p.notes_md = ((p.notes_md or "") + "\n\n" + note).strip()
    p.last_activity_at = now
    await session.flush()
    return None
