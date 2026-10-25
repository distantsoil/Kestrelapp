"""What Settings shows (D-UI-106 to D-UI-109, SPEC-INTERFACES 2.6): the six sections, the plain
words for each rule, and the numbers a rule row needs. The page and its routes both read this
module so a row, its count and its "See them" list can never disagree.

Nothing here writes. Every number that comes from fewer than five observations is labeled, not
shown as if it were solid (hard rule 6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel import paths
from kestrel.config import Settings
from kestrel.db.models import Opportunity, Org, Source
from kestrel.pipeline.profile import load_profile
from kestrel.rules import helpers

log = structlog.get_logger()

# ------------------------------------------------------------------------------- sections
# (id, label, icon, group). The ids are the URL hashes; `/settings#system` and the other old
# hashes are mapped in static/js/settings.js (LEGACY).
SECTIONS: tuple[tuple[str, str, str, str], ...] = (
    ("computer", "This computer", "laptop", "SET UP"),
    ("roles", "Which roles you see", "list", "SET UP"),
    ("scoring", "Scoring and learning", "tune", "SET UP"),
    ("privacy", "Privacy and access", "lock", "YOU"),
    ("data", "Your data", "folder", "YOU"),
    ("preferences", "Preferences", "palette", "YOU"),
)
SECTION_IDS = tuple(s[0] for s in SECTIONS)
GROUPS = (
    ("SET UP", tuple(s for s in SECTIONS if s[3] == "SET UP")),
    ("YOU", tuple(s for s in SECTIONS if s[3] == "YOU")),
)

SAMPLE = 200  # "your last 200 roles"
TRACKS = ("employment", "consulting")


# ------------------------------------------------------------------------------ the rules
@dataclass(frozen=True)
class RuleDef:
    """One row of Rules that hide roles: the plain name, the filters.yml rule it switches, and
    whether it carries its own number (days old, weeks)."""

    key: str  # the id the page uses
    rule: str  # the rule id in filters.yml
    name: str
    number: str | None = None  # "age" | "weeks"
    in_profile: bool = False  # the value is edited in Profile, not here


RULES: dict[str, tuple[RuleDef, ...]] = {
    "employment": (
        RuleDef("pay", "comp_floor", "Pay below your minimum", in_profile=True),
        RuleDef("visa", "sponsorship", "Needs visa sponsorship"),
        RuleDef("clear", "clearance", "Needs a security clearance"),
        RuleDef("title", "anti_title", "Titles you skip", in_profile=True),
        RuleDef("level", "seniority_floor", "Below your level", in_profile=True),
        RuleDef("geo", "geo", "Outside your locations", in_profile=True),
        RuleDef("age", "stale", "Older than", number="age"),
    ),
    "consulting": (
        RuleDef("rate", "rate_floor", "Rate below your minimum", in_profile=True),
        RuleDef("short", "too_short", "Too short", number="weeks"),
        RuleDef("sector", "excluded_sector", "Sectors you skip", in_profile=True),
        RuleDef("geo", "geo", "Outside your locations", in_profile=True),
        RuleDef("age", "stale_trigger", "Older than", number="age"),
    ),
}
# What the switch is called in the toast and the screen-reader label.
NUMBER_LABEL = {
    "age": "Days old before a role is hidden",
    "weeks": "Weeks before a project counts as too short",
}
NUMBER_LIMITS = {"age": (1, 365), "weeks": (1, 52)}
SHORT_NAMES = {"age": "Hiding old roles", "weeks": "Hiding short projects"}


def rule_def(track: str, key: str) -> RuleDef | None:
    return next((r for r in RULES.get(track, ()) if r.key == key), None)


def weeks_from_days(days: Any) -> int:
    """Profile keeps the shortest project in days; Settings shows whole weeks (rounded up, at
    least one, never above 52)."""
    try:
        d = int(days)
    except (TypeError, ValueError):
        return 2
    return max(1, min(52, -(-d // 7)))


def money(value: Any) -> str:
    try:
        return f"${int(value):,}"
    except (TypeError, ValueError):
        return ""


def _terms(profile: Any, which: str) -> Any:
    return getattr(profile, which, None) if profile is not None else None


def _names(items: Any) -> list[str]:
    out: list[str] = []
    for it in items or []:
        name = getattr(it, "name", None) if not isinstance(it, str) else it
        if name:
            out.append(str(name))
    return out


def rule_line(track: str, key: str, settings: Settings, profile: Any) -> str:
    """The plain sentence under a rule's name, with the operator's own value in it."""
    et, ct = _terms(profile, "employment_terms"), _terms(profile, "consulting_terms")
    if key == "pay":
        m = money(getattr(et, "comp_floor_usd", None))
        return f"Your minimum is {m}." if m else "Set your minimum pay in Profile."
    if key == "rate":
        m = money(getattr(ct, "day_rate_floor", None))
        return f"Your minimum is {m} per day." if m else "Set your minimum rate in Profile."
    if key == "visa":
        return "Hides roles that say they cannot sponsor a visa."
    if key == "clear":
        return "Hides roles that require a government clearance."
    if key == "title":
        titles = _names(getattr(et, "anti_titles", None))
        if not titles:
            return "No titles are skipped yet. Add them in Profile."
        more = f", such as {titles[0]}"
        return f"Skips {len(titles)} title{'s' if len(titles) != 1 else ''}{more}."
    if key == "level":
        floor = str(getattr(et, "seniority_floor", "") or "").strip()
        return f"Hides roles below {floor.title()}." if floor else "Any level is allowed."
    if key == "sector":
        sectors = _names(getattr(ct, "excluded_sectors", None))
        if not sectors:
            return "No sectors are skipped yet. Add them in Profile."
        return "Skips " + _join(sectors[:3]) + ("." if len(sectors) <= 3 else " and more.")
    if key == "geo":
        places = geo_places(profile)
        return ("Allows " + _join(places[:4]) + ("." if len(places) <= 4 else " and more.")
                if places else "Every place is allowed.")
    if key == "age":
        return ("Hides roles posted longer ago than this. Choose 1 to 365."
                if track == "employment" else "Hides postings older than this. Choose 1 to 365.")
    if key == "short":
        return "Hides projects that end sooner than this. Choose 1 to 52."
    return ""


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def geo_places(profile: Any) -> list[str]:
    geo = getattr(profile, "geo_rules", None) if profile is not None else None
    if geo is None:
        return []
    out = [str(getattr(m, "name", "")) for m in (getattr(geo, "onsite_metros", None) or [])]
    out += [str(st) for st in (getattr(geo, "onsite_states", None) or [])]
    if getattr(geo, "remote_ok", False):
        out.append("Remote (US)")
    return [o for o in out if o]


def number_value(track: str, key: str, settings: Settings, profile: Any) -> int:
    if key == "age":
        return int(settings.max_posting_age_days if track == "employment"
                   else settings.consulting.max_trigger_age_days)
    ct = _terms(profile, "consulting_terms")
    return weeks_from_days(getattr(ct, "min_engagement_days", None))


# ------------------------------------------------------------------- what each rule hides
@dataclass
class Hidden:
    opp_id: int
    title: str
    org: str
    why: str
    stage: str
    shown: bool = False  # the operator chose Show it anyway


@dataclass
class RuleCount:
    key: str
    n: int = 0
    of_n: int = 0
    roles: list[Hidden] = field(default_factory=list)


def _age_days(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso)[:10]).date() if len(str(iso)) >= 10 else None
    except ValueError:
        return None
    return None if d is None else max(0, (datetime.now(UTC).date() - d).days)


def _pay_text(o: Opportunity) -> str:
    lo, hi = o.comp_min, o.comp_max
    unit = {"hourly": " per hour", "daily": " per day"}.get(o.comp_interval or "", "")
    if lo and hi and lo != hi:
        return f"Pay posted as {money(lo)} to {money(hi)}{unit}"
    one = hi or lo
    return f"Pay posted as {money(one)}{unit}" if one else "Pay posted below your minimum"


def why_hidden(track: str, key: str, o: Opportunity, org: Org | None, settings: Settings) -> str:
    """One plain line saying why the rule matched this role."""
    if key in ("pay", "rate"):
        return _pay_text(o).replace("Pay posted", "Rate posted" if key == "rate" else "Pay posted")
    if key == "visa":
        return "Says it cannot sponsor a visa"
    if key == "clear":
        return "Requires a security clearance"
    if key == "title":
        return f"Title matches a title you skip: {o.title or 'untitled'}"
    if key == "level":
        return "Below the level you set"
    if key == "sector":
        return f"Sector is {org.sector}" if org is not None and org.sector else "Sector you skip"
    if key == "geo":
        place = o.location_raw or "an unlisted place"
        return f"{place}, not on your list"
    if key == "age":
        d = _age_days(o.date_posted if track == "employment" else o.event_date)
        return f"Posted {d} days ago" if d is not None else "Older than your limit"
    if key == "short":
        days = (o.trigger_data or {}).get("engagement_days") if o.trigger_data else None
        return f"Lasts {weeks_from_days(days)} weeks" if days else "Shorter than your limit"
    return "A rule applied"


async def rule_counts(
    session: AsyncSession, settings: Settings, track: str, *, sample: int = SAMPLE,
    shown_ids: set[int] | None = None,
) -> dict[str, RuleCount]:
    """For each hard-blocker rule of the track, how many of the last `sample` roles it would
    hide (each rule on its own, as the old preview counted) and the roles themselves. A role
    with no posted pay, place or clearance statement is never counted: the rule has nothing to
    check (hard rule 4). Reads only."""
    shown_ids = shown_ids or set()
    filters = helpers.load_filters(str(paths.config_file("filters.yml")))
    rules = {r["id"]: r for r in (filters.get(track) or {}).get("hard_blockers", []) or []}
    opps = list(
        (
            await session.execute(
                select(Opportunity)
                .where(Opportunity.track == track, Opportunity.archived_at.is_(None))
                .order_by(Opportunity.first_seen_at.desc(), Opportunity.id.desc())
                .limit(sample)
            )
        ).scalars()
    )
    profile = load_profile(track)
    orgs: dict[int, Org | None] = {}
    sources: dict[int, Source | None] = {}
    out = {d.key: RuleCount(d.key, of_n=len(opps)) for d in RULES[track]}
    for o in opps:
        org = orgs.get(o.org_id) if o.org_id else None
        if o.org_id and o.org_id not in orgs:
            org = orgs[o.org_id] = await session.get(Org, o.org_id)
        src = sources.get(o.source_id) if o.source_id else None
        if o.source_id and o.source_id not in sources:
            src = sources[o.source_id] = await session.get(Source, o.source_id)
        ev = helpers.build_evaluator(
            opp=o, org=org, profile=profile, source=src, settings=settings
        )
        for d in RULES[track]:
            rule = rules.get(d.rule)
            if rule is None:
                continue
            try:
                hit = bool(ev.eval(rule["when"]))
            except Exception as exc:  # a bad rule counts nothing, never breaks the page
                log.warning("settings_rule_preview_error", rule=d.rule, error=str(exc))
                continue
            if hit:
                c = out[d.key]
                c.n += 1
                c.roles.append(
                    Hidden(
                        o.id, o.title or "Untitled role", o.org_name or (org.name if org else ""),
                        why_hidden(track, d.key, o, org, settings), o.stage,
                        shown=o.id in shown_ids,
                    )
                )
    return out


async def cutoff_counts(session: AsyncSession, track: str, strong: float, look: float) -> dict:
    """How the last 200 scored roles of the track would sort at these cutoffs: the latest
    tier-2 score of each. Roles with no score yet are not counted."""
    from kestrel.db.models import Evaluation

    ids = list(
        (
            await session.execute(
                select(Opportunity.id)
                .where(Opportunity.track == track, Opportunity.archived_at.is_(None))
                .order_by(Opportunity.first_seen_at.desc(), Opportunity.id.desc())
                .limit(SAMPLE)
            )
        ).scalars()
    )
    scores: list[float] = []
    if ids:
        rows = (
            await session.execute(
                select(Evaluation.opportunity_id, Evaluation.score, Evaluation.id)
                .where(
                    Evaluation.opportunity_id.in_(ids), Evaluation.tier == 2,
                    Evaluation.score.is_not(None),
                )
                .order_by(Evaluation.id)
            )
        ).all()
        latest: dict[int, float] = {}
        for oid, score, _eid in rows:
            latest[oid] = float(score)
        scores = list(latest.values())
    a = sum(1 for v in scores if v >= strong)
    b = sum(1 for v in scores if look <= v < strong)
    return {"strong": a, "look": b, "scored": len(scores), "of_n": len(ids)}


def f1(v: float) -> str:
    return f"{float(v):.1f}"


def parse_cutoff(raw: Any) -> float:
    """A cutoff typed by the operator: 0.0 to 5.0 in steps of 0.1. Plain error text."""
    s = str(raw).strip().replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        raise ValueError("Use a number from 0.0 to 5.0.") from None
    if v != v or not 0.0 <= v <= 5.0:
        raise ValueError("Use a number from 0.0 to 5.0.")
    if abs(v * 10 - round(v * 10)) > 1e-6:
        raise ValueError("Use steps of 0.1, such as 3.5.")
    return round(v, 1)


def check_cutoffs(strong: float, look: float) -> None:
    if look >= strong:
        raise ValueError("Worth a look must be lower than Strong match.")


# ------------------------------------------------------------------------------ weights
WEIGHT_LABELS = {
    "employment": (
        ("role_fit", "How well the role fits you"),
        ("domain_match", "Your field"),
        ("company_context", "The company"),
        ("comp_logistics", "Pay and logistics"),
        ("trajectory", "Career step"),
    ),
    "consulting": (
        ("engagement_fit", "How well the work fits you"),
        ("buyer_access", "Access to the buyer"),
        ("economics", "The money"),
        ("delivery_feasibility", "Can you deliver it"),
        ("strategic_value", "Strategic value"),
    ),
}


def pct(v: float) -> int:
    return int(round(float(v) * 100))


# -------------------------------------------------------------------------- notifications
# What each "Tell me about" switch turns on or off: the notification kinds Kestrel raises
# (kestrel.services.delivery reads `notify.events.<kind>`, a kind not listed is on).
NOTIFY_KINDS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "matches": ("New strong matches", "Roles that score high for you.",
                ("new_strong_match", "high_score")),
    "followups": ("Follow-ups due", "Applications waiting on your next step.",
                  ("next_action_due", "ghosting")),
    "runs": ("Run finished", "When a search run is done.", ("run_finished",)),
    "attention": ("Something needs attention", "A source, model or backup problem.",
                  ("source_disabled", "liveness_closed", "update", "model_problem",
                   "backup_problem")),
}


def notify_on(settings: Settings, group: str) -> bool:
    kinds = NOTIFY_KINDS[group][2]
    return all(settings.notify.events.get(k, True) for k in kinds)


SOUND_LABELS = (("chime", "Chime"), ("soft_click", "Soft click"), ("bell", "Bell"))
DATE_SAMPLES = date(2026, 10, 23)


TOUCH_DAYS = {
    "spring": [3, 20], "earthday": [4, 22], "longday": [6, 21], "meteors": [8, 12],
    "autumn": [9, 22], "harvest": [11, 26], "longnight": [12, 21], "newyear": [12, 31],
    "july4": [7, 4], "halloween": [10, 31], "lunarny": [2, 17], "laborday": [9, 7],
    "colors": [3, 14], "crescent": [2, 18], "lanterns": [11, 8], "winterlights": [12, 10],
}


def touch_date_labels() -> dict[str, str]:
    from kestrel.date_display import format_display

    today = date.today()
    labels = {}
    for key, (month, day) in TOUCH_DAYS.items():
        upcoming = date(today.year, month, day)
        if upcoming < today:
            upcoming = upcoming.replace(year=today.year + 1)
        labels[key] = format_display(upcoming, "short")
    return labels


def region_preview(date_format: str, time_format: str) -> str:
    from kestrel.date_display import format_display

    when = datetime(2026, 10, 23, 20, 2)
    return "Shows as: " + format_display(
        when, "region", kind="datetime", separator=", ",
        date_format=date_format, time_format=time_format,
    )


def date_format_options() -> list[tuple[str, str]]:
    """(stored value, what the select shows)."""
    from kestrel import ui_settings as u

    return [(f, "Oct 23, 2026" if f == "Mon D, YYYY" else f) for f in u.DATE_FORMATS]


def time_options() -> list[tuple[str, str]]:
    return [("12h", "12-hour"), ("24h", "24-hour")]


def quiet_choices() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    from kestrel.date_display import format_display

    def label(hhmm: str) -> str:
        return format_display(hhmm, kind="time")

    starts = [f"{h:02d}:00" for h in (20, 21, 22, 23)]
    ends = [f"{h:02d}:00" for h in (6, 7, 8, 9)]
    return [(s, label(s)) for s in starts], [(e, label(e)) for e in ends]


# ----------------------------------------------------------------------------- MCP actions
@dataclass(frozen=True)
class McpAction:
    name: str
    does: str
    writes: bool = False


# Every action the MCP server exposes (SPEC-INTERFACES 4), each in plain words. A test keeps this
# list equal to kestrel.mcp.server.TOOL_NAMES, so a new action cannot ship unlisted.
MCP_ACTIONS: tuple[McpAction, ...] = (
    McpAction("list_opportunities", "List your roles, filtered by track, stage, verdict or score."),
    McpAction("get_opportunity", "Read one role in full, with how Kestrel scored it."),
    McpAction("search_opportunities", "Find roles by meaning, such as 'warehouse planning'."),
    McpAction("get_shortlist", "Read your strong matches, best first."),
    McpAction("get_pursuits", "Read your applications, their stages and next steps."),
    McpAction("get_org", "Read what Kestrel knows about one company."),
    McpAction("get_story_bank", "Read your interview stories and the gaps in them."),
    McpAction("get_comp_intel", "Read pay figures Kestrel has collected, with how many there are."),
    McpAction("get_analytics", "Read your funnel numbers, with how many each rests on."),
    McpAction("get_run_status", "Read how the current search run is going."),
    McpAction("ingest_opportunity", "Add a role from a link or pasted text. It reads what it is "
              "given and never crawls.", writes=True),
    McpAction("trigger_scan", "Ask for a search of one of your sources. Kestrel runs it later, "
              "politely.", writes=True),
    McpAction("trigger_evaluation", "Ask Kestrel to score one role again.", writes=True),
    McpAction("set_pursuit_status", "Move one application to a new stage in your list.",
              writes=True),
    McpAction("add_note", "Add a dated note to one application.", writes=True),
)
MCP_NEVER = ("Apply for you", "Send email", "Message anyone")


# --------------------------------------------------------------------------- the help cards
# (title, icon, accent, lines, flyout body). Ported from the locked mockup; the flyout body is
# fixed text written here, never user input, so it renders as markup.
HELP: dict[str, tuple[tuple[str, str, str, tuple[str, ...], str], ...]] = {
    "computer": (
        ("Which should I pick?", "help", "info",
         ("On this computer is free and private.", "Online is faster on an older computer.",
          "Both uses each where it helps."),
         "<h4>A simple rule</h4><p>Have a graphics card? Start with On this computer. No graphics "
         "card? Try Both: quick screens stay here and the deep read goes online.</p>"
         "<h4>You can change it</h4><p>Switch any time. Nothing you have saved is lost.</p>"),
        ("Is my computer enough?", "cpu", "good",
         ("16 GB memory runs medium models.", "A graphics card makes it much faster.",
          "No card? It works, only slower."),
         "<h4>How to tell</h4><p>The first checklist step looks at your computer and says what "
         "fits.</p><h4>What to expect</h4><ul><li>8 GB memory: small models.</li><li>16 GB: medium "
         "models.</li><li>32 GB or more: large models.</li></ul><p>Without a graphics card, a "
         "first run can take several times longer.</p>"),
        ("What downloading means", "dl", "warn",
         ("Models are big files that live here.", "Each is about 3 to 10 GB.",
          "Download once. They work offline."),
         "<h4>Before you start</h4><p>Leave about 15 GB of free space. Downloads can pause and "
         "pick up where they stopped.</p><h4>Slow connection?</h4><p>Pause now and resume on a "
         "faster network.</p>"),
        ("What leaves this computer", "lock", "tier",
         ("Local models send nothing out.", "Online asks first, every time.",
          "You see what would be sent."),
         "<h4>If you pick Online or Both</h4><p>Kestrel shows the role text and profile lines it "
         "would send, plus the cost, and waits for your yes.</p><h4>What Kestrel never does</h4>"
         "<p>It never applies, emails or messages anyone for you.</p>"),
    ),
    "roles": (
        ("How rules work", "list", "info",
         ("Each rule hides roles before scoring.", "Switch off any rule you do not want.",
          "Nothing hidden is ever deleted."),
         "<h4>Where hidden roles go</h4><p>They wait in a list you can open with See them. Show "
         "it anyway brings one back into your Pipeline.</p><h4>Counts</h4><p>Counts come from "
         "your last 200 roles and update after each run.</p>"),
        ("Why some get through", "help", "warn",
         ("Kestrel keeps a role when unsure.", "Many postings leave out pay.",
          "You can still skip them yourself."),
         "<h4>Missing details</h4><p>If a posting does not list pay, a place or a clearance, the "
         "rule has nothing to check, so the role stays.</p><h4>Pay a little under your "
         "minimum</h4><p>Roles slightly below your minimum are handled by Keep near-floor roles "
         "in Profile, not by this rule.</p><h4>Unusual titles</h4><p>A title worded differently "
         "from the ones you skip can slip past. Add those words in Profile to catch it next "
         "time.</p>"),
        ("Score cutoffs", "tune", "good",
         ("Strong match is your top group.", "Worth a look is the middle group.",
          "Lower scores stay saved."),
         "<h4>Picking numbers</h4><p>Raise Strong match from if you see too many. Lower Worth a "
         "look from if good roles hide in the middle.</p><h4>Alerts</h4><p>Strong match alerts "
         "follow this cutoff, so a higher number means fewer alerts.</p>"),
        ("Changing your Profile", "profile", "tier",
         ("Pay, titles, level and places live there.", "Edit them in Profile, not here.",
          "Changes apply on your next run."),
         "<h4>Why one place</h4><p>Keeping a single copy means your rules, scores and resumes "
         "always agree.</p><h4>Check the result</h4><p>After editing, come back and watch the "
         "counts change. Roles already hidden stay hidden until the next run.</p>"),
    ),
    "scoring": (
        ("How Kestrel learns", "wand", "info",
         ("You rate roles in Calibrate.", "Kestrel looks for your pattern.",
          "More ratings give better guesses."),
         "<h4>Three stages</h4><p>First your ratings are shown to the model as examples. Next it "
         "tunes how much each part counts. With about 200 ratings it can build a sorting model of "
         "your own.</p><h4>Stays on this computer</h4><p>Learning uses only your ratings and "
         "never leaves this computer.</p>"),
        ("Suggestions, not sliders", "tune", "good",
         ("Sliders are easy to get wrong.", "Kestrel tests a change first.",
          "You say yes or not now."),
         "<h4>How it tests</h4><p>Kestrel replays your past ratings with the new weights and only "
         "suggests a change if it matches your calls better.</p><h4>Always reversible</h4><p>An "
         "accepted change shows Undo, and Restore original weights puts everything back.</p>"),
        ("Agreement", "check", "good",
         ("How often Kestrel matches your call.", "Counted on roles you rated.",
          "Shown with the number of ratings."),
         "<h4>How it is counted</h4><p>For each rated role, Kestrel checks whether its group "
         "(strong match, worth a look or skip) is the one you chose.</p><h4>Small numbers</h4>"
         "<p>With fewer than 5 ratings it says Too few to tell, because one rating would swing "
         "the result.</p>"),
        ("Forgetting is permanent", "trash", "warn",
         ("Forgotten ratings cannot come back.", "Export first if you may want them.",
          "Roles and documents are never touched."),
         "<h4>What happens next</h4><p>Kestrel starts learning again from zero as you rate roles. "
         "Weights you already accepted stay until you restore the original weights.</p>"
         "<h4>Export first</h4><p>Export my ratings saves a copy as a file you can keep.</p>"),
    ),
    "privacy": (
        ("What confidential mode hides", "eyeoff", "info",
         ("Your current employer's name.", "Replaced by the label you choose.",
          "Your own files stay unchanged."),
         "<h4>Pick a label that fits</h4><p>'A logistics company' says enough without naming "
         "anyone. Use the same label every time so documents match.</p><h4>What it does not "
         "hide</h4><p>The job title and dates stay, so the history still reads right.</p>"),
        ("Where it applies", "list", "good",
         ("Resumes Kestrel builds.", "Lists and cards in Kestrel.", "Files you export."),
         "<h4>Check before you send</h4><p>Open the preview on any resume to see the label in "
         "place.</p><h4>Backups</h4><p>Backups keep your real data so a restore is complete. "
         "Keep those files private.</p>"),
        ("Using a phone or laptop", "wifi", "warn",
         ("Scan the code with your phone.", "Works on your own network only.",
          "Off until you turn it on."),
         "<h4>How to use it</h4><p>Turn it on, scan the code or copy the address, and open it in "
         "a browser on the same Wi-Fi.</p><h4>Stay safe</h4><p>Turn it off on shared or public "
         "Wi-Fi. Kestrel stays on this computer.</p>"),
        ("AI assistants", "plug", "tier",
         ("Off until you turn it on.", "Kestrel never applies or sends anything.",
          "The assistant may use the cloud."),
         "<h4>How to connect</h4><p>Turn it on, copy the address, and paste it into the "
         "assistant's MCP settings. Then restart the assistant.</p><h4>What it can do</h4><p>"
         "Every action is listed in the card, in plain words.</p><h4>What it never does</h4><p>"
         "It never applies, emails or messages anyone for you through Kestrel. Turn it off to "
         "cut the connection.</p>"),
    ),
    "data": (
        ("When to back up", "cal", "info",
         ("Before updating or starting over.", "After you add many applications.",
          "About once a week is plenty."),
         "<h4>Where it goes</h4><p>Your browser or app saves the file to Downloads. Move a copy "
         "to cloud storage or a USB drive.</p><h4>How big</h4><p>Most backups are a few "
         "megabytes.</p>"),
        ("What restore does", "restore", "warn",
         ("Replaces what Kestrel has now.", "Saves a copy of today's data first.",
          "Needs your confirmation."),
         "<h4>If you change your mind</h4><p>Restore the saved copy the same way. It is named "
         "with today's date.</p><h4>Keys</h4><p>Online model keys are not in backups. Add them "
         "again on Models.</p>"),
        ("Importing from a file", "sheet", "good",
         ("Applications: CSV, spreadsheet, Markdown.", "Contacts: a file from LinkedIn.",
          "You check it before it is added."),
         "<h4>Applications</h4><p>Columns named Company, Role, Date and Status match on their "
         "own. Right after adding, Undo removes the rows you just added.</p><h4>Contacts</h4><p>"
         "Download your own connections from LinkedIn and choose the file. Kestrel never signs in "
         "there. Email addresses are not kept. Importing a newer file later shows who changed "
         "jobs.</p><h4>Try it first</h4><p>Both imports have a sample file.</p>"),
        ("About diagnostics files", "diag", "tier",
         ("Made only when you ask.", "No profile, resumes or keys.",
          "Share it only if you choose."),
         "<h4>When to use it</h4><p>If something is not working, download the file and attach it "
         "to a message to support. Kestrel never sends it for you.</p><h4>Starting over is "
         "safe</h4><p>Clear everything saves a backup first, and online model keys stay.</p>"),
    ),
    "preferences": (
        ("Nothing is emailed", "bell", "info",
         ("They stay on this computer.", "Nothing is sent to anyone.",
          "Kestrel never messages for you."),
         "<h4>Where they show</h4><p>The bell in Kestrel keeps a list. Computer notifications "
         "are short pop-ups from your system.</p><h4>Too many?</h4><p>Turn off the kinds you do "
         "not need. Strong matches are the most useful to keep.</p>"),
        ("Quiet hours", "moon", "warn",
         ("Pop-ups pause, nothing is lost.", "The bell still collects them.",
          "Check them when you are back."),
         "<h4>Overnight ranges</h4><p>9:00 PM to 7:00 AM works as one night.</p><h4>Runs</h4>"
         "<p>Scheduled runs still happen. Only the pop-ups and sounds are held back.</p>"),
        ("Sounds", "vol", "good",
         ("On by default, with Chime.", "Pick a sound and a volume.", "Quiet hours mute it too."),
         "<h4>Try before you pick</h4><p>Press Play to hear the sound at your chosen volume."
         "</p><h4>Quiet hours</h4><p>During quiet hours there are no pop-ups and no sounds. The "
         "bell still collects alerts.</p>"),
        ("Colors and scenes", "palette", "good",
         ("14 scenes, or None for no art.", "Change the scene daily.",
          "Still mode keeps the art quiet."),
         "<h4>Scenes</h4><p>Pick one, or turn on Change scene daily and choose which ones to "
         "rotate. Open Manage scenes or Manage touches to preview each one.</p><h4>Every color "
         "scheme is checked</h4><p>Each one passes the same readability test in light and "
         "dark.</p>"),
    ),
}

HOW_IT_WORKS = (
    "<h4>The short version</h4><p>Settings has six parts. Pick one on the left. Changes save as "
    "you make them.</p><h4>Undo</h4><p>Most changes show a message with Undo for a few "
    "seconds.</p><h4>Technical details</h4><p>Commands and addresses sit behind Show details, so "
    "you only see them if you want to.</p><h4>Where things went</h4><p>Online model keys are on "
    "Models. Your profile is on Profile.</p>"
)


# ------------------------------------------------------------------------- appearance extras
# (id, name). Preview colors are scheme tokens in tokens.css.
SCHEMES: tuple[tuple[str, str], ...] = (
    ("indigo", "Paper and Indigo"),
    ("teal", "Paper and Teal"),
    ("plum", "Paper and Plum"),
    ("graphite", "Paper and Graphite"),
    ("contrast", "High contrast"),
)
# Server touch id -> the id the scene art in static/js/scenes/seasons-*.js registers it under.
TOUCH_JS: dict[str, str] = {
    "spring": "spring", "earth_day": "earthday", "longest_day": "longday",
    "shooting_stars": "meteors", "autumn": "autumn", "harvest": "harvest",
    "longest_night": "longnight", "new_year": "newyear", "fourth_of_july": "july4",
    "halloween": "halloween", "lunar_new_year": "lunarny", "labor_day": "laborday",
    "colors_of_spring": "colors", "crescent_evenings": "crescent",
    "lantern_night": "lanterns", "winter_lights": "winterlights",
}
AVATAR_ICONS = ("initial", "bird", "leaf", "star", "sun", "mountain", "compass")
AVATAR_NAMES = {"initial": "Initial", "bird": "Bird", "leaf": "Leaf", "star": "Star",
                "sun": "Sun", "mountain": "Mountain", "compass": "Compass", "photo": "Photo"}
TEXT_SIZES = (("small", "Small"), ("default", "Default"), ("large", "Large"))
